"""Tom DeMark TD Sequential, Setup phase (DeMark, "The New Science of Technical Analysis", 1994). TD Buy Setup: a
bearish price flip (close[f-1] > close[f-5] and close[f] < close[f-4]) starts the count at 1 on bar f; nine consecutive
closes, each below the close four bars earlier, complete the setup (bar 9). It is *perfected* when the low of bar 8 or bar 9 is below the lows of bars 6 and 7. The completed,
perfected buy setup is an exhaustion of a down move -> long at the close of bar 9; the sell setup is mirrored -> short.
Daily bars.

Formulas follow the ProRealCode TD Sequential indicator code (read as text, not run) and LuxAlgo's description. Defaults added by the harvester (fixed before any run, no variants): the sources give no
entry, stop or exit (DeMark's own risk level and the 13-bar Countdown are not described there; a completed 9 is "not a
signal in itself" per LuxAlgo, so the spec tests exactly the 9 + perfection reading); stop ``stop_atr`` = 2.0 x ATR14,
time exit ``hold_bars`` = 20 bars. Strict perfection: the lows of bar 8 or 9 only, later lows are not used (they need
future bars). The count must be uninterrupted (the ProRealCode code does not enforce this; DeMark and LuxAlgo do)."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "td_setup9"
SOURCE = (
    "T. DeMark, TD Sequential (1994); formulas per ProRealCode (https://www.prorealcode.com/prorealtime-indicators/td-sequential-setup/) "
    "and LuxAlgo (https://www.luxalgo.com/library/concept/td-sequential/)"
)
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()  # long on b = TD buy setup
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        c, lo = b.c, b.low
        for i in range(13, len(b)):
            if pos[i] < 13:
                continue
            # price flip on bar f = i - 8: close[f-1] > close[f-5]; then 9 bars f..i with close < close 4 bars earlier
            f = i - 8
            if not c[f - 1] > c[f - 5]:
                continue
            if not all(c[j] < c[j - 4] for j in range(f, i + 1)):
                continue
            perfect = (lo[i] < lo[i - 3] and lo[i] < lo[i - 2]) or (lo[i - 1] < lo[i - 3] and lo[i - 1] < lo[i - 2])
            ai = float(a[i])
            if not (perfect and math.isfinite(ai) and ai > 0):
                continue
            e = float(c[i])
            st = e - stop_atr * ai
            if sgn * st > 0 or sgn < 0:
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * st, None, hold_ms))
    return out
