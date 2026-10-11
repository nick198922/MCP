"""Simple event-driven backtest engine.

Signals are a per-bar sequence of *target* positions in ``{-1, 0, +1}``:

* ``+1`` — fully long at the next bar's open
* ``-1`` — fully short at the next bar's open
* ``0``  — flat

Everything happens on bar boundaries. When the target position changes we charge
``fee_bps`` + ``slippage_bps`` on the notional change (so a flip from −1 → +1
pays 2x). Returns within a bar use the bar's close/open; the next-bar execution
means we never peek at the signal's own bar close.

Deliberately small: no funding, no borrow costs, no multiple symbols, no
compounding re-sizing. Enough for walk-forward sanity checks over a single
instrument, which is all Phase 3.5 asks for.
"""

from __future__ import annotations

import math
from typing import Any


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _normalise_bars(bars: list[Any]) -> list[dict[str, float]]:
    """Accept ccxt-style [[ts_ms, o, h, l, c, v], …] or dict rows."""
    rows: list[dict[str, float]] = []
    for b in bars:
        if isinstance(b, (list, tuple)) and len(b) >= 5:
            rows.append({"ts": int(b[0]), "open": float(b[1]), "high": float(b[2]), "low": float(b[3]), "close": float(b[4])})
        elif isinstance(b, dict) and "close" in b:
            rows.append(
                {
                    "ts": int(b.get("ts") or b.get("timestamp") or 0),
                    "open": float(b.get("open") or b["close"]),
                    "high": float(b.get("high") or b["close"]),
                    "low": float(b.get("low") or b["close"]),
                    "close": float(b["close"]),
                }
            )
    return rows


def signals_momentum(bars: list[Any], lookback: int = 20, threshold_pct: float = 2.0) -> list[int]:
    """Simple N-bar momentum breakout.

    long when ``close / close[t-lookback] - 1 >= threshold_pct/100``
    short when the opposite inequality holds, flat otherwise. Returns one
    position per bar aligned with ``bars`` (first ``lookback`` entries = 0).
    """
    rows = _normalise_bars(bars)
    out = [0] * len(rows)
    thr = threshold_pct / 100.0
    for i in range(lookback, len(rows)):
        prev = rows[i - lookback]["close"]
        if prev <= 0:
            continue
        ret = rows[i]["close"] / prev - 1.0
        if ret >= thr:
            out[i] = 1
        elif ret <= -thr:
            out[i] = -1
    return out


def _stats(returns: list[float], *, bars_per_year: float) -> dict[str, Any]:
    n = len(returns)
    if n == 0:
        return {"count": 0}
    total = 1.0
    for r in returns:
        total *= 1.0 + r
    mean = sum(returns) / n
    if n > 1:
        var = sum((r - mean) ** 2 for r in returns) / (n - 1)
        sd = math.sqrt(var)
    else:
        sd = 0.0
    negatives = [r for r in returns if r < 0]
    downside_sd = math.sqrt(sum(r * r for r in negatives) / n) if negatives else 0.0
    sharpe = (mean / sd * math.sqrt(bars_per_year)) if sd > 0 else None
    sortino = (mean / downside_sd * math.sqrt(bars_per_year)) if downside_sd > 0 else None

    equity = [1.0]
    for r in returns:
        equity.append(equity[-1] * (1.0 + r))
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (v - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
    return {
        "count": n,
        "total_return": total - 1.0,
        "annualized_return": (total ** (bars_per_year / n) - 1.0) if n > 0 else None,
        "mean_bar_return": mean,
        "stdev_bar_return": sd,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "best_bar": max(returns),
        "worst_bar": min(returns),
        "bars_per_year": bars_per_year,
    }


def run_backtest(
    bars: list[Any],
    positions: list[int],
    *,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    bars_per_year: float = 365 * 24,  # hourly default
    initial_equity: float = 1.0,
) -> dict[str, Any]:
    """Walk through ``bars`` executing ``positions[i]`` at ``bars[i+1].open``.

    Returns per-bar returns, equity curve, trade list and summary stats. The
    final bar never trades (no next-bar open to fill against) — we simply mark
    the previous position's close-to-close return.
    """
    rows = _normalise_bars(bars)
    if len(positions) != len(rows):
        raise ValueError(f"positions length {len(positions)} != bars length {len(rows)}")
    cost_per_turn = (fee_bps + slippage_bps) / 10_000.0

    equity = [initial_equity]
    returns: list[float] = []
    trades: list[dict[str, Any]] = []
    current_pos = 0
    entry_price = None
    entry_ts = None
    for i in range(len(rows) - 1):
        target = positions[i]
        # Trading happens at next bar's open.
        exec_price = rows[i + 1]["open"]
        bar_cost = 0.0
        if target != current_pos:
            delta = abs(target - current_pos)
            bar_cost = cost_per_turn * delta
            equity[-1] *= 1.0 - bar_cost
            if current_pos != 0 and entry_price is not None:
                side = "long" if current_pos > 0 else "short"
                pnl_ret = current_pos * (exec_price / entry_price - 1.0)
                trades.append(
                    {
                        "side": side,
                        "entry_ts": entry_ts,
                        "entry_price": entry_price,
                        "exit_ts": rows[i + 1]["ts"],
                        "exit_price": exec_price,
                        "return_pct": pnl_ret,
                    }
                )
                entry_price = None
                entry_ts = None
            if target != 0:
                entry_price = exec_price
                entry_ts = rows[i + 1]["ts"]
            current_pos = target
        # Mark-to-market from executed open to next close, net of any cost on this bar.
        mkt = current_pos * (rows[i + 1]["close"] / rows[i + 1]["open"] - 1.0) if current_pos != 0 else 0.0
        bar_ret = (1.0 - bar_cost) * (1.0 + mkt) - 1.0
        returns.append(bar_ret)
        equity.append(equity[-1] * (1.0 + mkt))
    # Close any open position at the final bar's close.
    if current_pos != 0 and entry_price is not None:
        last = rows[-1]
        pnl_ret = current_pos * (last["close"] / entry_price - 1.0)
        side = "long" if current_pos > 0 else "short"
        equity[-1] *= 1.0 - cost_per_turn  # exit cost
        trades.append(
            {
                "side": side,
                "entry_ts": entry_ts,
                "entry_price": entry_price,
                "exit_ts": last["ts"],
                "exit_price": last["close"],
                "return_pct": pnl_ret,
            }
        )

    stats = _stats(returns, bars_per_year=bars_per_year)
    stats["num_trades"] = len(trades)
    # Overwrite total/annualised return with the cost-aware equity figure so
    # the summary row is consistent with equity_final.
    gross = equity[-1] / initial_equity
    stats["total_return"] = gross - 1.0
    stats["annualized_return"] = (gross ** (bars_per_year / len(returns)) - 1.0) if returns else None
    stats["win_rate"] = (sum(1 for t in trades if t["return_pct"] > 0) / len(trades)) if trades else None
    stats["avg_trade_return"] = (sum(t["return_pct"] for t in trades) / len(trades)) if trades else None
    return {
        "stats": stats,
        "equity_final": equity[-1],
        "equity_curve": equity,
        "returns": returns,
        "trades": trades,
    }
