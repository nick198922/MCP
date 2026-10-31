"""Paper carry "A v2": frozen strategy A plus the leg-vanish exit rule (pure: no I/O, time is a parameter).

Item 8c of docs/ideas/IDEA-20261031-structural-edges-depth.md; rule (iii) of
docs/verification/2026-10-31-carry-a-delisting.md. The frozen book A (``collector/carry_paper_engine.py``, tag
``cc-paper``, registration IDEA-20261017-forward-paper-carry) is not changed: A v2 is a shadow version computed from
A's own records. It keeps A's entries and A's exit and adds one exit: close the position (sell the spot; the perp
leg stays at its last price) at the 8 h cycle that completes ``A_V2_N_CYCLES`` consecutive cycles with no funding
payment (and, when perp bars are supplied, no perp bar close) while the spot still has a price.

Returns are fractions of slot notional; ``*_usd`` values are USD for one slot, as in the engine's records.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .carry_delisting import STEP_MS, missing_cycles

A_V2_VERSION = "A v2"
A_V2_TAG = "cc-paper-v2"
A_V2_N_CYCLES = 2
A_V2_DESCRIPTION = (
    "A v2 = frozen A (same entries, same exit at trailing APR <= 0) plus one exit: sell the spot leg (the perp leg at "
    "its last price) at the cycle that completes 2 consecutive 8h cycles with no funding payment and no perp bar while "
    "the spot still trades (rule (iii) of docs/verification/2026-10-31-carry-a-delisting.md). It is protection against losing the hedge leg, not a proven "
    "improvement: in the 2024-10..2026-10 backtest the perp-leg delisting cost A -27.3 pp a year on capital "
    "[-57.1; -5.4] (survivorship, confirmed only with the registered, pessimistic last-perp-close settlement; -6.1 pp "
    "[-19.4; +6.0] when settled at spot), and the rule adds +7.3 pp a year [-5.4; +20.6], a CI that contains 0 "
    "(16 events, +3.0 pp without the largest one, positive only after two data fixes made after the first run). The "
    "argument for the rule is the mechanism (an unhedged spot position is outside A's mandate), not that estimate. "
    "Shadow book: computed from A's records, no order and no separate engine; a slot freed by the rule is not re-used "
    "(v2 entries = A entries), so v2 can differ from A only on positions whose leg vanished."
)
A_V2_LIMITS = (
    "The frozen engine does not store perp bars, and the live 4h klines of a delisted Binance perp keep returning flat "
    "bars (MITO stayed at 0.00919 for 6 days), so the shadow uses the funding condition only. Funding is the history as "
    "finally recorded for the trade (a payment published late counts as present: the shadow never exits on a hole of "
    "our own feed). The exit is priced at the first engine mark at or after the rule cycle (spot at that mark, perp at "
    "its last recorded price, A's exit costs); the frozen engine holds a position without funding aside, so this mark "
    "is usually one cycle (8h) after the rule fires. A position whose exit is due but that has no mark since is "
    "listed as unpriced and left out of both totals."
)


def leg_vanished(
    f_ts: Sequence[int],
    t: int,
    spot: float | None,
    *,
    perp_close_ts: Sequence[int] | None = None,
    n_cycles: int = A_V2_N_CYCLES,
    step_ms: int = STEP_MS,
) -> bool:
    """Rule (iii) at decision time ``t``: the ``n_cycles`` 8 h cycles ``(t - k*step, t - (k-1)*step]`` before ``t``
    hold no funding payment (``f_ts``) and no perp bar close (``perp_close_ts``; pass only bars that traded, a
    zero-volume bar is not a bar; ``None`` = not observed, funding only), and the spot has a price at ``t``.
    Uses only data at or before ``t``."""
    if not spot:
        return False
    return missing_cycles(f_ts, perp_close_ts or [], t, step_ms=step_ms, max_n=n_cycles) >= n_cycles


def vanish_rule_ts(
    f_ts: Sequence[int],
    open_ts: int,
    end_ts: int,
    *,
    perp_close_ts: Sequence[int] | None = None,
    n_cycles: int = A_V2_N_CYCLES,
    step_ms: int = STEP_MS,
) -> int | None:
    """First decision time ``open_ts + k*step`` (k >= ``n_cycles``, at most ``end_ts``) at which the leg counts as
    vanished (the spot condition is the caller's), else None. Only cycles whose windows all lie after the entry are
    checked: the position was opened with a live leg. A leg that stops paying at cycle k (no payment in cycle k's
    window) fires the rule at cycle k + n_cycles - 1."""
    sorted_f = sorted(int(x) for x in f_ts)
    bars = sorted(int(x) for x in perp_close_ts) if perp_close_ts is not None else []
    k = n_cycles
    while open_ts + k * step_ms <= end_ts:
        t = open_ts + k * step_ms
        if missing_cycles(sorted_f, bars, t, step_ms=step_ms, max_n=n_cycles) >= n_cycles:
            return t
        k += 1
    return None


def a_v2_shadow(
    positions: Sequence[Mapping[str, Any]],
    funding_ts: Mapping[str, Sequence[int]],
    marks: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    now_ms: int,
    side_cost: float,
    decision_delay_ms: int,
    n_cycles: int = A_V2_N_CYCLES,
    step_ms: int = STEP_MS,
) -> dict[str, Any]:
    """A v2 next to the frozen A book, from A's own records.

    ``positions``: engine rows (``trade_id``, ``symbol``, ``status``, ``opened_ts``, ``closed_ts``, ``slot_notional``,
    ``state`` with ``spot_e`` and ``fees``, ``trade`` with ``pnl_usd`` for a closed one). ``funding_ts``: trade_id ->
    payment timestamps accrued by the trade. ``marks``: trade_id -> mark rows (``cycle_ts``, ``payload`` with
    ``pnl_usd`` and ``spot``), any order. ``side_cost``: A's cost of one side of both legs (fraction of notional).
    v1 P&L of an open position = its latest mark (entry costs only, as ``paper_carry_status``), or -fees without one.
    """
    rows: list[dict[str, Any]] = []
    v1_total = v2_total = 0.0
    unpriced = 0
    for pos in positions:
        tid, n = pos["trade_id"], float(pos["slot_notional"])
        closed = pos["status"] == "closed"
        ms = sorted(marks.get(tid) or [], key=lambda m: int(m["cycle_ts"]))
        if closed:
            v1 = float(pos["trade"]["pnl_usd"])
        else:
            v1 = float(ms[-1]["payload"]["pnl_usd"]) if ms else -float(pos["state"].get("fees") or 0.0) * n
        end = int(pos["closed_ts"]) if closed else now_ms
        fired = vanish_rule_ts(funding_ts.get(tid) or [], int(pos["opened_ts"]), end, n_cycles=n_cycles, step_ms=step_ms)
        row: dict[str, Any] = {
            "trade_id": tid, "symbol": pos["symbol"], "opened_ts": int(pos["opened_ts"]), "v1_status": pos["status"],
            "v1_exit_ts": int(pos["closed_ts"]) if closed else None, "v1_pnl_usd": v1, "rule_ts": fired,
        }  # fmt: skip
        if fired is None:
            row |= {"v2_status": "same_as_v1", "v2_exit_ts": row["v1_exit_ts"], "v2_pnl_usd": v1}
        else:
            at = [int(m["cycle_ts"]) + decision_delay_ms for m in ms]
            after = [m for m, ts in zip(ms, at, strict=True) if ts >= fired and (not closed or ts < end)]
            if after:
                m = after[0]
                p = m["payload"]
                exit_cost = side_cost * float(p["spot"]) / float(pos["state"]["spot_e"]) * n
                row |= {
                    "v2_status": "rule_exit", "v2_exit_ts": int(m["cycle_ts"]) + decision_delay_ms,
                    "v2_pnl_usd": float(p["pnl_usd"]) - exit_cost,
                }  # fmt: skip
            elif closed:
                row |= {"v2_status": "rule_at_v1_exit", "v2_exit_ts": end, "v2_pnl_usd": v1}
            else:
                row |= {"v2_status": "exit_due_unpriced", "v2_exit_ts": None, "v2_pnl_usd": None}
        if row["v2_pnl_usd"] is None:
            unpriced += 1
        else:
            v1_total += v1
            v2_total += row["v2_pnl_usd"]
        rows.append(row)
    return {
        "version": A_V2_VERSION,
        "tag": A_V2_TAG,
        "n_cycles": n_cycles,
        "positions": rows,
        "rule_fired": sum(1 for x in rows if x["rule_ts"] is not None),
        "rule_exits": sum(1 for x in rows if x["v2_status"] == "rule_exit"),
        "unpriced": unpriced,
        "v1_pnl_usd": v1_total,
        "v2_pnl_usd": v2_total,
        "diff_usd": v2_total - v1_total,
    }
