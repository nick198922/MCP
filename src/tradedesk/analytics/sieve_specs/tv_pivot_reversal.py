"""TradingView built-in "Pivot Reversal Strategy" (Pine v5 code as published by Marketcalls and the TradingView page):
``leftBars`` = 4, ``rightBars`` = 2. ``ta.pivothigh`` confirms a swing high only ``rightBars`` bars after it, so the
level is known causally at bar p + 2. From the confirmation of a pivot high a buy-stop sits one tick above it
(``le`` stays true until a bar's high exceeds the level or a newer pivot high replaces it); a confirmed pivot low arms a
sell-stop one tick below it, symmetrically. Both orders reverse the position, there is no other exit. Daily bars.

Adaptation: the source enters with a stop order at the level. The sieve enters at the close of the bar, so the signal is
the bar whose high first exceeds the armed level, taken only if that bar also **closes** beyond the level (a wick that
closes back inside consumes the arm and gives no signal). A pivot is a high strictly above the 4 bars before it and not
below the 2 bars after it (Pine's tie rule is not on the pages; this is the harvester's reading). The tick is dropped.

Defaults added by the harvester (fixed before any run, no variants): the source has no stop, target or exit (it
reverses on the opposite pivot, which needs future bars), so ``stop_atr`` = 2.0 ATR14 and ``hold_bars`` = 20; timeframe
1d (the page: any)."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "tv_pivot_reversal"
SOURCE = "TradingView built-in Pivot Reversal Strategy (https://www.tradingview.com/support/solutions/43000594526-pivot-reversal-strategy/), code via https://www.marketcalls.in/amibroker/pivot-reversal-strategy-tradingview-pinescript-to-amibroker-afl-conversion.html"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"left": 4, "right": 2, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, left: int = 4, right: int = 2, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    left, right = int(left), int(right)
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()  # mirrored high = -low: pivot low becomes pivot high
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        level = np.nan
        armed = False
        for i in range(len(b)):
            if pos[i] == 0:
                armed = False
            if pos[i] < left + right:
                continue
            p = i - right
            hp = float(b.h[p])
            if hp > float(b.h[p - left : p].max()) and hp >= float(b.h[p + 1 : i + 1].max()):
                level, armed = hp, True  # confirmed pivot high (known at bar i)
                continue
            if armed and float(b.h[i]) > level:
                armed = False
                ai = float(a[i])
                if b.c[i] > level and np.isfinite(ai) and ai > 0:
                    e = float(b.c[i])
                    out.append(Signal(b.close_time(i), side, sgn * e, sgn * (e - stop_atr * ai), None, int(hold_bars) * b.bar_ms))
    return out
