"""Range fade at the edges of a 20-bar box (The Right Trader, "Range Trading Strategy", daily chart).

Range: the highest high and lowest low of the 20 bars before the signal bar. Zones: the top zone is the range high down
to range high - 0.25 ATR14, the bottom zone the range low up to range low + 0.25 ATR14. The range is valid only after
two bars of the window touched the top zone and two touched the bottom zone. Long: the signal bar's low enters the
bottom zone and its close finishes back above that zone; entry at that close; stop = range low - 0.5 ATR14; target the
midpoint of the range. Short: mirror (the high enters the top zone and the close finishes back below it, stop = range
high + 0.5 ATR14, target the midpoint). Daily bars.

Readings and defaults (fixed before any run, no variants):
- The range is built from the 20 bars before the signal bar, not including it (the sibling pages of the same site say
  "exclude today"; this page does not say). A touch is a bar of that window whose low is at or below the bottom zone's
  upper edge (high at or above the top zone's lower edge). ATR14 is the signal bar's own value.
- "No close in the window has finished outside the high or low" is true by construction (closes lie inside the bars'
  highs and lows) and is not coded.
- The page exits at the midpoint or, if still in, on a close outside the range (a condition on later bars); the sieve
  cannot describe a close-based exit, so the stop and the target stay and a time exit after ``hold_bars`` = 20 bars is
  added (the page has no time limit). A signal whose entry is already at or beyond the midpoint is skipped (no target).
- The page publishes no win rate."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "range_fade_atr_zones"
SOURCE = "The Right Trader, Range Trading Strategy (https://therighttrader.com/learn/trading-strategies/range-trading-strategy)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 20, "atr_n": 14, "zone": 0.25, "stop_atr": 0.5, "touches": 2, "hold_bars": 20}


def signals(
    bars: Bars,
    n: int = 20,
    atr_n: int = 14,
    zone: float = 0.25,
    stop_atr: float = 0.5,
    touches: int = 2,
    hold_bars: int = 20,
) -> list[Signal]:
    n, touches = int(n), int(touches)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        a = atr(b, int(atr_n), pos)
        for i in range(n, len(b)):
            ar = float(a[i])
            if pos[i] < n or not (math.isfinite(ar) and ar > 0):
                continue
            wh, wl = b.h[i - n : i], b.low[i - n : i]
            hi, lo = float(wh.max()), float(wl.min())
            top, bot = hi - zone * ar, lo + zone * ar
            if int(np.sum(wh >= top)) < touches or int(np.sum(wl <= bot)) < touches:
                continue
            e = float(b.c[i])
            if not (b.low[i] <= bot and e > bot):
                continue
            stop, target = lo - stop_atr * ar, (hi + lo) / 2.0
            if stop < e < target:
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * stop, sgn * target, hold_ms))
    return out
