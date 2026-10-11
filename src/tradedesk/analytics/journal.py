"""Pure helpers for the trading journal: PnL, R-multiple and summary stats."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


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
