"""Signals the demo trader selected but did not execute, against their shadow plans (BACKLOG 0.5, owner 23.10, item a):
what R would the entries that did not fill ('missed'), the signals left out by score or daily caps ('skipped') and the
signals the guard refused ('refused': R:R after fees, a coin already held, ...) have made, grouped by reason, next to
the shadow plans of the trades the demo did fill ('taken', the reference).

Pure functions, no I/O, time is a parameter (``now_ms``: decisions after it are ignored).

Inputs are rows of the store (read by the caller over a read-only connection):
- ``demo_trade``: a 'missed' row (the maker entry was cancelled with nothing filled) links its shadow plan by
  ``plan_id``; its reason is the ``note`` (``execution.orders`` wording). Filled rows ('open', 'closing', 'closed') with
  a ``plan_id`` give the reference group.
- ``exec_audit`` kind 'skipped' (score below the minimum, a daily cap, a stronger signal): its reason is the first of
  ``payload.violations``. Its shadow plan (``trade_plan`` tag ``src:demo-shadow`` + ``demo:skipped``) is written in the
  same cycle with the same clock, so it is found by (decision second, symbol, side, setup, timeframe); side and
  timeframe come from the link id ``td-<setup>-<l|s>-<tf>-<base>-<signal s>``.
- ``exec_audit`` kind 'refused' (a guard rule, or nothing sent because the quote failed): the same reason and the same
  plan lookup (the demo trader tags these plans ``demo:skipped`` too). A refusal with a level error or a stop farther
  than the guard's maximum gets no shadow plan by design (``execution.demo_trader._shadow``, ``NO_PLAN_RULES``): it is
  left out of the rows and only counted (``groups.refused.excluded``), so 'no_plan' keeps meaning a plan that is lost.
  The reason is the first violation, so a signal refused for two rules is grouped under the first in guard order.

R is the unit of ``demo_vs_backtest`` (``r_plain``, no break-even): the live risk unit |fill - stop| + taker fee on both
prices, a maker fee on the entry and on a target exit, taker plus slippage on a stop or a time exit
(``stop_management.Fees``). ``r_plan`` is the shadow plan's own stored ``r_result`` (planned risk, flat 0.05 % taker on
both sides), kept for the cross-check.

Caveats: a shadow plan is a market entry at the decision price (up to 2 hours after the signal-bar close, the price the
guard sized from), scored by the collector on Bybit 1h candles from the next full hour with the stop winning a bar that
touches both, without break-even; the demo enters at a maker quote and moves its stop to break-even, so the shadow R is
the plain setup, not what the demo would have booked. Below ``MIN_TRADES`` resolved plans in a group the text says
'замало для висновку'.
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .demo_vs_backtest import MIN_TRADES
from .smart_money import t975
from .stop_management import Fees

GROUPS = ("missed", "skipped", "refused", "taken")
FILLED = ("open", "closing", "closed")
AUDIT_GROUPS = ("skipped", "refused")
# execution.demo_trader._shadow writes no plan when a guard violation is a level error ('levels: stop and target must be
# on opposite sides of the entry') or a stop too far ('stop 12.0% from the entry > 10%'): such a refusal has nothing to
# score. Matched on the guard wording, so a venue error that merely mentions a stop is not taken for it.
NO_PLAN_RULES = re.compile(r"^(?:levels\b|stop \S+ from the entry > )")
SHADOW_TAG = "src:demo-shadow"
R_OUTCOMES = ("target", "stop", "expired")
WEEK_MS = 7 * 86_400_000
NO_REASON = "no reason recorded"
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?%?(?=.*[<>=])")  # a measured value: a number with a comparison after it
_SYMBOL = re.compile(r"\b[A-Z0-9]{1,20}/[A-Z0-9]{1,10}(?::[A-Z0-9]{1,10})?")


@dataclass(frozen=True)
class ShadowRow:
    group: str  # one of GROUPS
    reason: str  # reason_key of the note / first violation; '' for 'taken'
    example: str  # the raw message (untrusted, cut): the dashboard labels the group from it
    link_id: str | None
    symbol: str
    setup: str
    timeframe: str
    side: str
    decided_ts: int  # unix ms of the decision (exec_audit ts; demo_trade opened_ts, else signal_ts)
    plan_id: int | None
    status: str  # 'resolved' | 'open' (the plan's horizon has not resolved yet) | 'no_plan' (no shadow plan found)
    outcome: str | None = None  # target | stop | expired | not_filled
    r_plan: float | None = None  # trade_plan.r_result as stored
    r: float | None = None  # live risk unit, no break-even (demo_vs_backtest.r_plain conventions)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def reason_key(msg: object) -> str:
    """One guard / selection / order message as a group key: measured values (numbers with a comparison after them)
    become 'N', pair symbols '<symbol>', so 'score 1 < 2' and 'score 0 < 2' group as 'score N < 2' while the rule's
    threshold stays."""
    s = " ".join(str(msg or "").split())[:200]
    if not s:
        return NO_REASON
    return _NUM.sub("N", _SYMBOL.sub("<symbol>", s))


def shadow_r(side: str, fill: float, stop: float, exit_: float, outcome: str, fees: Fees = Fees()) -> float | None:
    """R of a resolved shadow plan in the live risk unit (``stop_management.simulate_stop_rule`` conventions): maker
    entry, maker target, taker plus slippage on a stop or an expiry; None for a non-positive risk or another outcome."""
    if outcome not in R_OUTCOMES or side not in ("long", "short"):
        return None
    sgn = 1.0 if side == "long" else -1.0
    if sgn * (fill - stop) <= 0:
        return None
    ru = abs(fill - stop) + fees.risk_taker * (abs(fill) + abs(stop))
    fee_out = fees.maker if outcome == "target" else fees.taker_exit
    return (sgn * (exit_ - fill) - fees.maker * abs(fill) - fee_out * abs(exit_)) / ru


def parse_link(link_id: object) -> tuple[str, str, str] | None:
    """(setup, side, timeframe) of a demo link id ``td-<setup>-<l|s>-<tf>-<base>-<signal s>``, else None."""
    parts = str(link_id or "").split("-")
    if len(parts) < 5 or parts[0] != "td" or parts[2] not in ("l", "s"):
        return None
    return parts[1], "long" if parts[2] == "l" else "short", parts[3]


def _tags(plan: Mapping[str, Any]) -> list[str]:
    raw = plan.get("tags")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [str(x) for x in raw] if isinstance(raw, list) else []


def _tag(tags: list[str], prefix: str) -> str | None:
    return next((t[len(prefix) :] for t in tags if t.startswith(prefix)), None)


def _f(x: object) -> float | None:
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def _violations(payload: object) -> list[Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    vs = payload.get("violations") if isinstance(payload, dict) else None
    return vs if isinstance(vs, list) else []


def planless_refusal(violations: Sequence[object]) -> bool:
    """True when the demo trader writes no shadow plan for a refusal with these violations (``NO_PLAN_RULES``)."""
    return any(NO_PLAN_RULES.match(" ".join(str(v).split())) for v in violations)


def _decided(a: Mapping[str, Any], now_ms: int) -> bool:
    return a.get("kind") in AUDIT_GROUPS and isinstance(a.get("ts"), int) and int(a["ts"]) <= now_ms


def _row(group: str, msg: object, base: dict[str, Any], plan: Mapping[str, Any] | None, fees: Fees) -> ShadowRow:
    reason = "" if group == "taken" else reason_key(msg)
    example = "" if group == "taken" else " ".join(str(msg or "").split())[:200]
    if plan is None:
        return ShadowRow(group, reason, example, **base, plan_id=None, status="no_plan")
    pid = plan.get("id")
    plan_id = int(pid) if isinstance(pid, int) else None
    if plan.get("status") != "resolved":
        return ShadowRow(group, reason, example, **base, plan_id=plan_id, status="open")
    outcome = None if plan.get("outcome") is None else str(plan.get("outcome"))
    fill, stop, exit_ = _f(plan.get("fill")), _f(plan.get("stop")), _f(plan.get("exit"))
    side = str(plan.get("side") or base["side"])
    r = None
    if outcome is not None and fill is not None and stop is not None and exit_ is not None:
        r = shadow_r(side, fill, stop, exit_, outcome, fees)
    return ShadowRow(
        group, reason, example, **base, plan_id=plan_id, status="resolved", outcome=outcome, r_plan=_f(plan.get("r_result")), r=r
    )


def match_rows(
    trades: Sequence[Mapping[str, Any]],
    audit: Sequence[Mapping[str, Any]],
    plans: Sequence[Mapping[str, Any]],
    *,
    now_ms: int,
    fees: Fees = Fees(),
) -> list[ShadowRow]:
    """Every missed demo entry, skipped or refused signal and filled demo trade decided by ``now_ms``, with its shadow
    plan (module docstring); refusals that get no plan by design are left out (``planless_refusals``). ``plans``:
    trade_plan rows (only the demo shadow ones are used); ``audit``: exec_audit rows (only kinds 'skipped' and
    'refused'). Odd rows are skipped, never raised on."""
    return _match(trades, audit, plans, now_ms, fees)[0]


def planless_refusals(audit: Sequence[Mapping[str, Any]], plans: Sequence[Mapping[str, Any]], *, now_ms: int) -> list[int]:
    """Decision times (unix ms) of the refusals decided by ``now_ms`` that have no shadow plan by design: a level error
    or a stop too far (``NO_PLAN_RULES``) and no plan found."""
    return _match([], audit, plans, now_ms, Fees())[1]


def _match(
    trades: Sequence[Mapping[str, Any]],
    audit: Sequence[Mapping[str, Any]],
    plans: Sequence[Mapping[str, Any]],
    now_ms: int,
    fees: Fees,
) -> tuple[list[ShadowRow], list[int]]:
    by_id: dict[int, Mapping[str, Any]] = {}
    by_key: dict[tuple[int, str, str, str, str], Mapping[str, Any]] = {}
    for p in plans:
        tags = _tags(p)
        if SHADOW_TAG not in tags or not isinstance(p.get("id"), int):
            continue
        by_id[int(p["id"])] = p
        if "demo:skipped" in tags and isinstance(p.get("created_ts"), int):
            key = (int(p["created_ts"]), str(p.get("symbol")), str(p.get("side")), _tag(tags, "setup:") or "", _tag(tags, "tf:") or "")
            by_key.setdefault(key, p)
    out: list[ShadowRow] = []
    for t in trades:
        status = t.get("status")
        if status != "missed" and status not in FILLED:
            continue
        ts = t.get("opened_ts") if isinstance(t.get("opened_ts"), int) else t.get("signal_ts")
        if not isinstance(ts, int) or ts > now_ms:
            continue
        base = {
            "link_id": str(t.get("link_id")),
            "symbol": str(t.get("symbol")),
            "setup": str(t.get("setup")),
            "timeframe": str(t.get("timeframe")),
            "side": str(t.get("side")),
            "decided_ts": ts,
        }
        pid = t.get("plan_id")
        plan = by_id.get(pid) if isinstance(pid, int) else None
        out.append(_row("missed" if status == "missed" else "taken", t.get("note"), base, plan, fees))
    planless: list[int] = []
    for a in audit:
        if not _decided(a, now_ms):
            continue
        ts, kind = int(a["ts"]), str(a["kind"])
        link = parse_link(a.get("link_id"))
        setup, side, tf = link if link else (str(a.get("setup")), "", "")
        base = {
            "link_id": None if a.get("link_id") is None else str(a.get("link_id")),
            "symbol": str(a.get("symbol")),
            "setup": setup,
            "timeframe": tf,
            "side": side,
            "decided_ts": ts,
        }
        vs = _violations(a.get("payload"))
        plan = by_key.get((ts // 1000, base["symbol"], side, setup, tf)) if link else None
        if kind == "refused" and plan is None and planless_refusal(vs):
            planless.append(ts)
            continue
        out.append(_row(kind, vs[0] if vs else None, base, plan, fees))
    return out, planless


def _stats(rows: Sequence[ShadowRow], now_ms: int, min_n: int) -> dict[str, Any]:
    rs = [float(x.r) for x in rows if x.r is not None]
    rp = [float(x.r_plan) for x in rows if x.r is not None and x.r_plan is not None]
    out: dict[str, Any] = {
        "n": len(rows),
        "week": sum(1 for x in rows if x.decided_ts >= now_ms - WEEK_MS),
        "resolved": sum(1 for x in rows if x.status == "resolved"),
        "open": sum(1 for x in rows if x.status == "open"),
        "no_plan": sum(1 for x in rows if x.status == "no_plan"),
        "outcomes": {k: sum(1 for x in rows if x.outcome == k) for k in (*R_OUTCOMES, "not_filled")},
        "n_r": len(rs),
        "mean_r": statistics.fmean(rs) if rs else None,
        "ci95_r": None,
        "win_share": sum(1 for v in rs if v > 0) / len(rs) if rs else None,
        "sum_r": sum(rs) if rs else None,
        "mean_r_plan": statistics.fmean(rp) if rp else None,
        "enough": len(rs) >= min_n,
        "min_n": min_n,
        "last_ts": max((x.decided_ts for x in rows), default=None),
    }
    if len(rs) >= 2:
        h = t975(len(rs) - 1) * statistics.stdev(rs) / len(rs) ** 0.5
        out["ci95_r"] = [out["mean_r"] - h, out["mean_r"] + h]
    return out


def summary(rows: Sequence[ShadowRow], *, now_ms: int, min_n: int = MIN_TRADES) -> dict[str, Any]:
    """Per group (missed, skipped, refused, taken) and per (group, reason): counts (all, last 7 days, resolved, open, without a
    plan), outcomes, the shadow R (mean, t 95 % CI from 2 values, share above 0, sum) and the stored plan R over the same
    plans; 'enough' from ``min_n`` resolved plans with R. Reasons sorted by count. A Ukrainian conclusion per group says
    'замало для висновку' below ``min_n``."""
    groups: dict[str, Any] = {}
    for g in GROUPS:
        part = [x for x in rows if x.group == g]
        s = _stats(part, now_ms, min_n)
        if s["enough"]:
            ci = s["ci95_r"]
            tail = f" [{ci[0]:+.2f}; {ci[1]:+.2f}]" if ci else ""
            s["conclusion"] = f"середній R тіньових планів {s['mean_r']:+.2f}{tail} на {s['n_r']} планах"
        else:
            s["conclusion"] = f"замало для висновку: тіньових планів з R {s['n_r']}, потрібно ≥ {min_n}"
        reasons: dict[str, list[ShadowRow]] = {}
        for x in part:
            reasons.setdefault(x.reason, []).append(x)
        s["reasons"] = sorted(
            ({"reason": k, "example": v[0].example, **_stats(v, now_ms, min_n)} for k, v in reasons.items() if g != "taken"),
            key=lambda d: (-d["n"], d["reason"]),
        )
        groups[g] = s
    return {"now_ms": now_ms, "min_n": min_n, "groups": groups}


def not_executed(
    trades: Sequence[Mapping[str, Any]],
    audit: Sequence[Mapping[str, Any]],
    plans: Sequence[Mapping[str, Any]],
    *,
    now_ms: int,
    min_n: int = MIN_TRADES,
    fees: Fees = Fees(),
) -> dict[str, Any]:
    """``summary`` of ``match_rows`` plus the rows themselves (``rows``, dicts) and, under ``groups.refused.excluded``,
    the count of refusals without a plan by design (all and last 7 days: ``n``, ``week``)."""
    rows, planless = _match(trades, audit, plans, now_ms, fees)
    out = summary(rows, now_ms=now_ms, min_n=min_n)
    out["groups"]["refused"]["excluded"] = {"n": len(planless), "week": sum(1 for ts in planless if ts >= now_ms - WEEK_MS)}
    out["rows"] = [x.as_dict() for x in rows]
    return out
