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
import operator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..store import Store

OPS = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
}


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
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    out: list[Rule] = []
    for row in doc.get("rules") or []:
        if not isinstance(row, dict):
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


async def _latest_metric(store: "Store", metric: dict[str, Any], *, now_ts: int) -> float | None:
    kind = metric.get("kind")
    if kind == "funding":
        series = await store.funding_series(metric["venue"], metric["base"], 0, now_ts)
        return series[-1]["annualized"] if series else None
    if kind == "atm_iv":
        series = await store.atm_term_series(metric["currency"], metric["expiry"], 0, now_ts)
        return series[-1]["atm_iv"] if series else None
    if kind == "basis":
        series = await store.basis_series(metric["currency"], metric["expiry"], 0, now_ts)
        return series[-1]["basis_pct"] if series else None
    return None


def _format_message(template: str, value: float, threshold: float) -> str:
    if not template:
        return ""
    try:
        return template.format(value=value, value_pct=value * 100, threshold=threshold, threshold_pct=threshold * 100)
    except (KeyError, IndexError, ValueError):
        return template


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
        value = await _latest_metric(store, r.metric, now_ts=now_ts)
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
        try:
            compiled = compile_expr(row["expr"])
            result = await evaluate(compiled, {"names": {}, "calls": calls})
        except DslError:
            continue
        except Exception:  # noqa: BLE001 — a user expression should not crash the scheduler
            continue
        if not result:
            continue
        try:
            value_f = 1.0 if isinstance(result, bool) else float(result)
        except (TypeError, ValueError):
            value_f = 1.0
        msg = row.get("message") or ""
        try:
            msg = msg.format(value=value_f, value_pct=value_f * 100)
        except (KeyError, IndexError, ValueError):
            pass
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
