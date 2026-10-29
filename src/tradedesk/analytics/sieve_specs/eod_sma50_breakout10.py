"""End-of-day trend breakout, as written on The Right Trader's page "End-of-Day Trading Strategy" (daily bars). Long:
the 50-day SMA of closes is at least 1 % above its value 10 bars earlier, the close is above it and above the highest
high of the previous 10 bars. Short: the SMA is at least 1 % below its value 10 bars earlier, the close is below it and
below the lowest low of the previous 10 bars. Stop 2 x ATR14 (Wilder) from the entry, no profit target.

Readings and adaptations (chosen before any run):
- entry at the signal bar's close instead of the next session's open (the sieve enters on the signal bar's close);
- the source exits on a close back through the SMA50 (needs future bars) and gives no time limit, so the default is
  a time exit after 20 bars, the stop stays at 2 ATR14;
- the 10-bar breakout window excludes the signal bar ("prior 10 sessions"); the 1 % slope is measured against the
  older SMA50 value; every bar that meets the conditions is a signal (the sieve keeps one position per symbol)."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "eod_sma50_breakout10"
SOURCE = "The Right Trader, End-of-Day Trading Strategy, https://therighttrader.com/learn/trading-strategies/end-of-day-trading-strategy"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"sma_n": 50, "slope_bars": 10, "slope_pct": 0.01, "break_n": 10, "stop_atr": 2.0, "hold_bars": 20}


def signals(
    bars: Bars,
    sma_n: int = 50,
    slope_bars: int = 10,
    slope_pct: float = 0.01,
    break_n: int = 10,
    stop_atr: float = 2.0,
    hold_bars: int = 20,
) -> list[Signal]:
    sma_n, slope_bars, break_n = int(sma_n), int(slope_bars), int(break_n)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    n = len(bars)
    sma = np.full(n, np.nan)
    for i in range(n):
        if pos[i] >= sma_n - 1:
            sma[i] = float(bars.c[i - sma_n + 1 : i + 1].mean())
    out: list[Signal] = []
    for i in range(n):
        ai = float(a[i])
        if pos[i] < sma_n - 1 + slope_bars or pos[i] < break_n or not (np.isfinite(ai) and ai > 0):
            continue
        s, s0 = float(sma[i]), float(sma[i - slope_bars])
        c = float(bars.c[i])
        hh = float(bars.h[i - break_n : i].max())
        ll = float(bars.low[i - break_n : i].min())
        hold = int(hold_bars) * bars.bar_ms
        if s >= s0 * (1.0 + slope_pct) and c > s and c > hh:
            out.append(Signal(bars.close_time(i), "long", c, c - stop_atr * ai, None, hold))
        elif s <= s0 * (1.0 - slope_pct) and c < s and c < ll:
            out.append(Signal(bars.close_time(i), "short", c, c + stop_atr * ai, None, hold))
    return out
