"""Triple EMA crossover 4 / 9 / 18 (The Right Trader, "Triple Moving Average Crossover", daily bars).

Exponential averages of the close with lengths 4, 9 and 18. Long: EMA4 above EMA9 and EMA9 above EMA18, on the first
close that completes that order after a bar that was not in that order; fill at that close. Short: the mirror order
(EMA4 below EMA9 below EMA18), first close that completes it. Stop: 2 x ATR14 (Wilder) from the entry close, below a
long and above a short. No profit target. Daily bars.

Readings and defaults (fixed before any run, no variants):
- The page exits when EMA4 crosses back through EMA9, or at the stop, whichever comes first. The cross-back is a condition
  on later bars; the sieve cannot describe it, so a time exit after ``hold_bars`` = 20 bars is added (the 20-bar default;
  the stop of the page stays).
- EMAs are seeded with the mean of the first n closes (the page does not say). The page's 1 % risk sizing is not modelled.
- The page publishes no win rate. Related trend entries already in the catalog use two lines (``carver_emac_16_64``,
  ``sma_cross_50_200``, ``macd_signal_cross``); this one needs three lines in order."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, atr, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "triple_ema_4_9_18"
SOURCE = "The Right Trader, Triple Moving Average Crossover (https://therighttrader.com/learn/trading-strategies/triple-moving-average-crossover)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"fast": 4, "mid": 9, "slow": 18, "atr_n": 14, "stop_atr": 2.0, "hold_bars": 20}


def signals(
    bars: Bars, fast: int = 4, mid: int = 9, slow: int = 18, atr_n: int = 14, stop_atr: float = 2.0, hold_bars: int = 20
) -> list[Signal]:
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        ef, em, es = ema(b.c, int(fast), pos), ema(b.c, int(mid), pos), ema(b.c, int(slow), pos)
        a = atr(b, int(atr_n), pos)
        for i in range(1, len(b)):
            vals = (float(ef[i]), float(em[i]), float(es[i]), float(ef[i - 1]), float(em[i - 1]), float(es[i - 1]), float(a[i]))
            if not all(math.isfinite(v) for v in vals):
                continue
            now = vals[0] > vals[1] > vals[2]
            before = vals[3] > vals[4] > vals[5]
            if now and not before and vals[6] > 0:
                e = float(b.c[i])
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * (e - stop_atr * vals[6]), None, hold_ms))
    return out
