"""Playbook rule P9, second half (card IDEA-20261114-options-playbook, item 4.7): bought vega through a US macro
release on an expiry at least 3 days after it, sold the next morning.

P9 says: do not hold bought short-dated vega through FOMC / CPI / NFP; if vega is bought at all, take an expiry
3-5 days after the release and exit before it or the morning after. Item 4.6 tested the first half (the daily
straddle through the release loses after costs). Here: the ATM straddle on the earliest expiry at least
``min_gap_days`` after the release, bought at 09:00 UTC on the release day and sold on the 09:00 surface
``hold_days`` later; compared with the daily straddle held to delivery (same day) and with the same trade on days
without a release.

Pure functions, no I/O; time is a parameter (epoch ms, UTC). Pricing reuses `straddle_p5` / `calendar_p10`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .put_skew import iv_at_strike
from .straddle_p5 import sell_early
from .trend_spread import DAY_MS

HOUR_MS = 3_600_000
MIN_GAP_DAYS = 3.0  # P9: "expiry >= 3-5 days after the release" (the lower end)
MAX_DTE_DAYS = 10.0  # the next Friday weekly is always within this from a weekday 09:00 entry
HOLD_DAYS = 1  # P9: "exit before the release or the morning after"
CONTROL_ANCHOR_HOUR = 13  # pseudo-release time of a control day (releases fall in 12:30-19:00 UTC)


def expiry_after_gap(
    snap: Mapping[str, Any], anchor_ms: int, *, min_gap_days: float = MIN_GAP_DAYS, max_dte_days: float = MAX_DTE_DAYS
) -> tuple[int | None, str]:
    """(expiry, reason): the earliest expiry on the surface at least ``min_gap_days`` after ``anchor_ms`` (the release),
    at most ``max_dte_days`` from the surface time ``t_ms``; (None, reason) if there is none."""
    lo = anchor_ms + int(round(min_gap_days * DAY_MS))
    later = sorted(e for e in snap["expiries"] if e >= lo)
    if not later:
        return None, "no expiry far enough after the event"
    e = later[0]
    if (e - snap["t_ms"]) / DAY_MS > max_dte_days + 1e-9:
        return None, "first expiry after the gap too far"
    return e, ""


def control_anchor(day_ms: int, *, hour: int = CONTROL_ANCHOR_HOUR) -> int:
    """Pseudo-release time of a control day (00:00 UTC ms): ``hour`` UTC of that day."""
    return day_ms + hour * HOUR_MS


def exit_snapshot(snaps: Mapping[int, Mapping[str, Any]], entry_day: int, hold_days: int = HOLD_DAYS) -> Mapping[str, Any] | None:
    """The surface of the day ``hold_days`` after ``entry_day`` (keys: 00:00 UTC ms), or None. Only that one day is read."""
    if hold_days < 1:
        raise ValueError("hold_days must be >= 1")
    return snaps.get(entry_day + hold_days * DAY_MS)


def hold_outcome(
    plan: dict[str, Any],
    debit: float,
    snaps: Mapping[int, Mapping[str, Any]],
    *,
    hold_days: int = HOLD_DAYS,
    hs: float = 0.015,
    arb_rule: str = "replace",
) -> tuple[dict[str, Any] | None, str]:
    """Sell the straddle on the 09:00 surface ``hold_days`` after entry (`straddle_p5.sell_early`: mark - hs, Bybit taker
    fee per leg). (result, reason); None if that surface is missing, is at or after the expiry, or lacks the expiry."""
    ex = exit_snapshot(snaps, plan["day"], hold_days)
    if ex is None:
        return None, "no exit surface"
    if ex["t_ms"] >= plan["expiry"]["m"]:
        return None, "exit at or after expiry"
    res = sell_early(plan, debit, dict(ex), hs=hs, arb_rule=arb_rule)
    if res is None:
        return None, "no exit mark"
    return {**res, "hold_days": hold_days}, ""


def iv_change(plan: Mapping[str, Any], exit_snap: Mapping[str, Any] | None) -> float | None:
    """Mark IV at the straddle strike on the exit surface minus the entry mark IV (descriptive: the vol crush)."""
    if exit_snap is None:
        return None
    x = exit_snap["expiries"].get(plan["expiry"]["m"])
    if x is None:
        return None
    iv = iv_at_strike(x, plan["K"])[0]
    return None if iv is None else iv - plan["iv"]


def paired(a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]], key: str = "r") -> list[dict[str, Any]]:
    """Per-day difference a - b of ``key`` (as "r") and of "pnl_spot" when both have it, on the days both samples
    traded (sorted by day)."""
    bm = {t["day"]: t for t in b}
    out = []
    for t in sorted(a, key=lambda t: t["day"]):
        u = bm.get(t["day"])
        if u is None:
            continue
        row = {"day": t["day"], "r": t[key] - u[key], "a": t[key], "b": u[key]}
        if "pnl_spot" in t and "pnl_spot" in u:
            row["pnl_spot"] = t["pnl_spot"] - u["pnl_spot"]
        out.append(row)
    return out


def stratified_diff(a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]], stratum: Any, key: str = "r") -> dict[str, Any]:
    """Mean of ``key`` in a minus b within each stratum (``stratum(trade)``, e.g. the weekday), weighted by a's count;
    strata missing from b are dropped. Removes a difference in the strata mix (e.g. days to expiry by weekday)."""
    ga: dict[Any, list[float]] = {}
    gb: dict[Any, list[float]] = {}
    for t in a:
        ga.setdefault(stratum(t), []).append(float(t[key]))
    for t in b:
        gb.setdefault(stratum(t), []).append(float(t[key]))
    rows = {s: (len(v), sum(v) / len(v), len(gb[s]), sum(gb[s]) / len(gb[s])) for s, v in ga.items() if s in gb}
    n = sum(r[0] for r in rows.values())
    diff = sum(r[0] * (r[1] - r[3]) for r in rows.values()) / n if n else float("nan")
    return {"diff": diff, "n_a": n, "dropped": len(a) - n, "strata": rows}


def p9_labels(h1: str, h2: str, h3: str) -> dict[str, str]:
    """Playbook wording registered in card 4.7 from the mechanical verdicts (✓ / ≈ / ✗ / ◯) of H1-H3.

    H1 (the 3-5 day straddle through the release, sold next morning): ✗ -> "loses" (P9 widened: no bought vega through
    the release at 3-5 days either); ✓ -> "new card" (an edge for the buyer is a new hypothesis, never a rule here);
    else "insufficient". H2 (3-5 days minus the daily straddle): ✓ -> "better than daily"; ✗ -> "not better"; else
    "insufficient". H3 (release minus control): ✗ -> "crush costs"; ✓ -> "release pays the buyer"; else "insufficient".
    """
    return {
        "H1": {"✗": "loses", "✓": "new card"}.get(h1, "insufficient"),
        "H2": {"✓": "better than daily", "✗": "not better"}.get(h2, "insufficient"),
        "H3": {"✗": "crush costs", "✓": "release pays the buyer"}.get(h3, "insufficient"),
    }
