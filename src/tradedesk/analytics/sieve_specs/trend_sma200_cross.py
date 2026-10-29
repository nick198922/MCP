"""Basic 200-day trend following, as written on The Right Trader's page "Trend Following Strategy" (daily bars). Long:
the first daily close above the 200-day SMA after a close below it; short: the first close below it after a close above.
Initial stop 3 x ATR14 (Wilder) from the entry, no profit target; the source exits on a close back through the average.

Readings and adaptations (chosen before any run):
- the source's exit (a close back through the SMA200) needs future bars, so the default is a time exit after 20 bars;
  the 3 ATR14 stop stays; the page does not say if the stop trails, it is kept fixed;
- "after a close below it" means the previous bar's close was below the previous bar's SMA200 (not equal);
- entry at the signal bar's close (the page leaves the fill open); the optional flat-average filter is not applied
  (the page does not define "flat")."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "trend_sma200_cross"
SOURCE = "The Right Trader, Trend Following Strategy, https://therighttrader.com/learn/trading-strategies/trend-following-strategy"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"sma_n": 200, "stop_atr": 3.0, "hold_bars": 20}


def signals(bars: Bars, sma_n: int = 200, stop_atr: float = 3.0, hold_bars: int = 20) -> list[Signal]:
    sma_n = int(sma_n)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    n = len(bars)
    sma = np.full(n, np.nan)
    for i in range(n):
        if pos[i] >= sma_n - 1:
            sma[i] = float(bars.c[i - sma_n + 1 : i + 1].mean())
    out: list[Signal] = []
    hold = int(hold_bars) * bars.bar_ms
    for i in range(1, n):
        ai = float(a[i])
        if pos[i] < sma_n or not (np.isfinite(ai) and ai > 0):
            continue
        c, pc = float(bars.c[i]), float(bars.c[i - 1])
        if c > sma[i] and pc < sma[i - 1]:
            out.append(Signal(bars.close_time(i), "long", c, c - stop_atr * ai, None, hold))
        elif c < sma[i] and pc > sma[i - 1]:
            out.append(Signal(bars.close_time(i), "short", c, c + stop_atr * ai, None, hold))
    return out
