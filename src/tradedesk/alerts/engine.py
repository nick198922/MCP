"""Rule evaluation.

A rule has an ``id``, a ``metric`` describing where to read from, a ``when``
clause (``op``, ``value``), an optional ``cooldown_seconds`` and ``message``
template. The evaluator reads the latest stored row for that metric and, if
the comparison succeeds and the cooldown has elapsed, returns a FiredAlert.
Side effects — writing to the store and delivering through channels — happen
in the scheduler job, not here, so the engine stays pure and easy to test.
"""

from __future__ import annotations

import json
import logging
import operator
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..store import Store

log = logging.getLogger(__name__)

OPS = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
}

# Reserved ``health:`` rule ids already warned about in this process (see load_rules).
_WARNED_RESERVED_IDS: set[str] = set()
# Unreadable stored metric values already warned about in this process (see _warn_once).
_WARNED_DATA: set[str] = set()


@dataclass(frozen=True)
class Rule:
    id: str
    enabled: bool
    metric: dict[str, Any]
    when: dict[str, Any]
    cooldown_seconds: int
    message: str = ""


@dataclass(frozen=True)
class FiredAlert:
    rule_id: str
    ts: int
    value: float
    threshold: float
    op: str
    metric: dict[str, Any]
    message: str

    def payload(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "ts": self.ts,
            "value": self.value,
            "threshold": self.threshold,
            "op": self.op,
            "metric": self.metric,
            "message": self.message,
        }


def load_rules(path: str | Path, *, default_cooldown: int = 1800) -> list[Rule]:
    """Rules from the JSON file. An id with the reserved ``health:`` prefix is skipped with a warning: provider-health
    alerts restore their state from stored rows with that prefix, so a user rule there would corrupt it. (DSL rules
    from the ``rules`` table always get ``db:<name>`` and cannot collide.)"""
    from .health import RULE_PREFIX  # noqa: PLC0415 — health imports this module

    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    out: list[Rule] = []
    for row in doc.get("rules") or []:
        if not isinstance(row, dict):
            continue
        rule_id = str(row.get("id", ""))
        if rule_id.startswith(RULE_PREFIX):
            # load_rules runs on every collector tick: warn once per id per process, keep skipping every time.
            if rule_id not in _WARNED_RESERVED_IDS:
                _WARNED_RESERVED_IDS.add(rule_id)
                log.warning("alert rule %r skipped: the id prefix %r is reserved for provider-health alerts", row.get("id"), RULE_PREFIX)
            continue
        try:
            out.append(
                Rule(
                    id=str(row["id"]),
                    enabled=bool(row.get("enabled", True)),
                    metric=row["metric"],
                    when=row["when"],
                    cooldown_seconds=int(row.get("cooldown_seconds", default_cooldown)),
                    message=str(row.get("message", "")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _warn_once(key: str, msg: str, *args: Any) -> None:
    """Log a data warning once per key per process: the alerts job runs every tick and would repeat it each time."""
    if key not in _WARNED_DATA:
        _WARNED_DATA.add(key)
        log.warning(msg, *args)


def _stored_number(v: Any, rule_id: str, col: str) -> float | None:
    """A stored metric value as a float; text / blob of another writer is "no value" with a logged warning (item 300:
    the comparison of a string with the threshold raised TypeError out of the alerts job)."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        _warn_once(
            f"{rule_id}\x00{col}",
            "alert rule %r: the latest stored %s is not a number (%s); read as no value",
            rule_id,
            col,
            type(v).__name__,
        )
        return None


async def _latest_metric(store: "Store", metric: dict[str, Any], *, now_ts: int, rule_id: str = "") -> float | None:
    kind = metric.get("kind")
    if kind == "funding":
        series = await store.funding_series(metric["venue"], metric["base"], 0, now_ts)
        return _stored_number(series[-1]["annualized"], rule_id, "funding annualized") if series else None
    if kind == "atm_iv":
        series = await store.atm_term_series(metric["currency"], metric["expiry"], 0, now_ts)
        return _stored_number(series[-1]["atm_iv"], rule_id, "atm_iv") if series else None
    if kind == "basis":
        series = await store.basis_series(metric["currency"], metric["expiry"], 0, now_ts)
        return _stored_number(series[-1]["basis_pct"], rule_id, "basis_pct") if series else None
    return None


_HUGE_SPEC = re.compile(r"\d{4}")  # a width / precision of 1000+ in a format spec


def _safe_format(template: str, **values: float) -> str:
    """``template.format(**values)``, or the template unchanged when it cannot format: an unknown or positional field,
    ``{value[0]}`` / ``{value.x}`` (TypeError / AttributeError raised out of the alerts job before item 299), or a
    format spec with a width / precision of 1000+ or a nested field (``{value:999999999}`` built a gigabyte string)."""
    try:
        for _, _, spec, _ in string.Formatter().parse(template):
            if spec and ("{" in spec or _HUGE_SPEC.search(spec)):
                return template
        return template.format(**values)
    except (KeyError, IndexError, ValueError, TypeError, AttributeError):
        return template


def _format_message(template: str, value: float, threshold: float) -> str:
    if not template:
        return ""
    return _safe_format(template, value=value, value_pct=value * 100, threshold=threshold, threshold_pct=threshold * 100)


async def evaluate_rules(rules: list[Rule], store: "Store", *, now_ts: int) -> list[FiredAlert]:
    """Return the alerts that should fire NOW (cooldowns already respected).

    Alerts are NOT persisted here; the scheduler job writes them.
    """
    fired: list[FiredAlert] = []
    for r in rules:
        if not r.enabled:
            continue
        op_fn = OPS.get(r.when.get("op"))
        threshold = r.when.get("value")
        if op_fn is None or threshold is None:
            continue
        value = await _latest_metric(store, r.metric, now_ts=now_ts, rule_id=r.id)
        if value is None:
            continue
        if not op_fn(value, threshold):
            continue
        last = await store.last_alert_ts(r.id)
        if last is not None and (now_ts - last) < r.cooldown_seconds:
            continue
        fired.append(
            FiredAlert(
                rule_id=r.id,
                ts=now_ts,
                value=float(value),
                threshold=float(threshold),
                op=r.when["op"],
                metric=r.metric,
                message=_format_message(r.message, float(value), float(threshold)),
            )
        )
    return fired


async def evaluate_dsl_rules(store: "Store", *, now_ts: int) -> list[FiredAlert]:
    """Evaluate DSL rules stored in the ``rules`` table (Phase 3.1).

    A rule fires when its boolean expression evaluates truthy AND the
    per-rule cooldown has elapsed since its last stored alert.
    """
    from ..rules.dsl import DslError, compile_expr, evaluate  # noqa: PLC0415
    from ..rules.metrics import MetricContext, build_metrics  # noqa: PLC0415

    ctx = MetricContext(store=store, now_ts=now_ts)
    calls = build_metrics(ctx)
    stored = await store.rules_list(enabled_only=True)
    fired: list[FiredAlert] = []
    for row in stored:
        rule_id = f"db:{row['name']}"
        last = await store.last_alert_ts(rule_id)
        if last is not None and (now_ts - last) < int(row["cooldown_seconds"]):
            continue
        ctx.notes.clear()
        try:
            compiled = compile_expr(row["expr"])
            result = await evaluate(compiled, {"names": {}, "calls": calls})
        except DslError as e:
            # A refused expression (too long / deep, a size bound, a stored rule another writer left) was skipped silently
            # on every tick; it is logged once per rule now (item 301).
            _warn_once(f"{rule_id}\x00expr", "DSL rule %r skipped: expression error: %s", rule_id, e)
            continue
        except Exception:  # noqa: BLE001 — e.g. None > 0.3 while a metric has no value: data, not the expression
            continue
        finally:
            # A text / blob stored metric reads as "no value" (the rule does not fire); before item 300 that was silent.
            for note in dict.fromkeys(ctx.notes):
                _warn_once(f"{rule_id}\x00{note}", "DSL rule %r: %s", rule_id, note)
        if not result:
            continue
        try:
            value_f = 1.0 if isinstance(result, bool) else float(result)
        except (TypeError, ValueError):
            value_f = 1.0
        except OverflowError:  # an integer beyond the float range (e.g. 9 ** 999) raised out of the alerts job
            continue
        msg = _safe_format(row.get("message") or "", value=value_f, value_pct=value_f * 100)
        fired.append(
            FiredAlert(
                rule_id=rule_id,
                ts=now_ts,
                value=value_f,
                threshold=0.0,
                op="dsl",
                metric={"expr": row["expr"], "name": row["name"]},
                message=msg,
            )
        )
    return fired
