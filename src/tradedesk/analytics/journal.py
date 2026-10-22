"""Pure helpers for the trading journal: PnL, R-multiple, summary stats and the weekly review (time passed in)."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

WEEK_S = 7 * 86400.0
SMALL_SAMPLE = 30  # fewer closed trades than this: a description, too few to judge a win share or an average


def pnl(side: str, size: float, entry: float, exit_: float) -> float:
    """Signed PnL in quote currency. Fees are not modelled here."""
    sign = 1.0 if side == "long" else -1.0 if side == "short" else 0.0
    return sign * size * (exit_ - entry)


def r_multiple(side: str, entry: float, stop: float | None, exit_: float) -> float | None:
    """R multiple = PnL per unit measured in stop-distance units.

    Long with ``stop < entry`` → risk = entry - stop; return (exit - entry) / risk.
    Short with ``stop > entry`` → risk = stop - entry; return (entry - exit) / risk.
    ``None`` whenever stop is missing or the direction/stop sign is incoherent.
    """
    if stop is None:
        return None
    if side == "long":
        risk = entry - stop
        if risk <= 0:
            return None
        return (exit_ - entry) / risk
    if side == "short":
        risk = stop - entry
        if risk <= 0:
            return None
        return (entry - exit_) / risk
    return None


def outcome_of(pnl_value: float, *, cancelled: bool = False) -> str:
    if cancelled:
        return "cancelled"
    if pnl_value > 0:
        return "win"
    if pnl_value < 0:
        return "loss"
    return "break_even"


def summary(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Win-rate, expectancy, avg/median R, best/worst R, total PnL over closed trades.

    ``trades`` is an iterable of dicts shaped like a ``journal`` row. Open or
    cancelled trades are ignored.
    """
    closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
    n = len(closed)
    if n == 0:
        return {"closed_trades": 0}
    wins = [t for t in closed if (t.get("outcome") or "") == "win"]
    losses = [t for t in closed if (t.get("outcome") or "") == "loss"]
    pnls = [float(t["pnl"]) for t in closed]
    rs = [float(t["r_multiple"]) for t in closed if t.get("r_multiple") is not None]
    rs_sorted = sorted(rs)
    median_r = rs_sorted[len(rs_sorted) // 2] if rs_sorted else None
    avg_win = (sum(float(t["pnl"]) for t in wins) / len(wins)) if wins else None
    avg_loss = (sum(float(t["pnl"]) for t in losses) / len(losses)) if losses else None
    return {
        "closed_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n,
        "total_pnl": sum(pnls),
        "expectancy_per_trade": sum(pnls) / n,
        "avg_win_pnl": avg_win,
        "avg_loss_pnl": avg_loss,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "median_r": median_r,
        "max_r": max(rs) if rs else None,
        "min_r": min(rs) if rs else None,
        "r_count": len(rs),
    }


def _num(x: object) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def week_start(ts: float) -> float:
    """Monday 00:00 UTC (unix s) of the ISO week holding ``ts``."""
    d = datetime.fromtimestamp(ts, UTC)
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _closed_rows(trades: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Closed rows with a finite P&L and a close time (the opening time when a closed row has none), as copies with
    ``ts`` = that time, a numeric ``pnl``, ``r_multiple`` only when finite and ``outcome`` filled from the P&L when
    missing. Anything else (open, cancelled, odd values) is left out."""
    out = []
    for t in trades:
        pnl_v = _num(t.get("pnl"))
        ts = _num(t.get("closed_ts"))
        ts = _num(t.get("opened_ts")) if ts is None else ts
        if t.get("status") != "closed" or pnl_v is None or ts is None:
            continue
        out.append(
            {
                **t,
                "ts": ts,
                "pnl": pnl_v,
                "r_multiple": _num(t.get("r_multiple")),
                "outcome": t.get("outcome") if t.get("outcome") in ("win", "loss", "break_even") else outcome_of(pnl_v),
            }
        )
    return out


def week_stats(trades: Iterable[dict[str, Any]], start_ts: float, end_ts: float) -> dict[str, Any]:
    """The closed trades of [start_ts, end_ts) by close time: summary() of them (closed_trades, wins, win_rate,
    total_pnl, expectancy_per_trade, avg_r and r_count over the rows with a recorded R, i.e. a stop), plus
    ``best`` / ``worst`` (the row with the highest / lowest P&L, None when nothing closed), ``by_source``
    [{source, n, pnl}] by the row's ``source`` key ('?' without one), most trades first, and ``small_sample``
    (fewer than SMALL_SAMPLE closed trades)."""
    xs = sorted((x for x in _closed_rows(trades) if start_ts <= x["ts"] < end_ts), key=lambda x: (x["ts"], str(x.get("id"))))
    s = summary(xs)
    src: dict[str, list[float]] = {}
    for x in xs:
        src.setdefault(str(x.get("source") or "?"), []).append(x["pnl"])
    return {
        **s,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "best": max(xs, key=lambda x: x["pnl"]) if xs else None,
        "worst": min(xs, key=lambda x: x["pnl"]) if xs else None,
        "by_source": sorted(
            ({"source": k, "n": len(v), "pnl": sum(v)} for k, v in src.items()), key=lambda r: (-r["n"], -r["pnl"], r["source"])
        ),
        "small_sample": s["closed_trades"] < SMALL_SAMPLE,
    }


def weekly_review(trades: Iterable[dict[str, Any]], *, now_ts: float) -> dict[str, Any]:
    """Weekly review of journal trades (pass the owner's real trades only) by ISO week (UTC) of the close:
    {'last': week_stats of the previous completed week, 'this': of the week holding ``now_ts`` (Monday 00:00 UTC
    to the next Monday), 'latest': week_stats of the most recent earlier week with a closed trade, or None}.
    Win share, P&L and R describe what happened; with fewer than SMALL_SAMPLE trades they show no edge either way.
    P&L is the journal's (fees as the venue or /close recorded them); R exists only where a stop was recorded."""
    xs = _closed_rows(trades)
    this_start = week_start(now_ts)
    last_start = this_start - WEEK_S
    earlier = [x["ts"] for x in xs if x["ts"] < last_start]
    latest = None
    if earlier:
        ws = week_start(max(earlier))
        latest = week_stats(xs, ws, ws + WEEK_S)
    return {"last": week_stats(xs, last_start, this_start), "this": week_stats(xs, this_start, this_start + WEEK_S), "latest": latest}
