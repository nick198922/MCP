"""Engulfing bar, range version (LuxAlgo, "Engulfing Bar"): the bar trades below the previous bar's low and closes above the
previous bar's high -> long on its close; the bar trades above the previous high and closes below the previous low -> short.
The source states that entries "commonly trigger on the engulfing bar's close", and the stop goes beyond the engulfing bar's
opposite end (below its low for a long, above its high for a short). Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the source states no target and no time exit, so the
exit is by time after ``hold_bars`` = 20 bars. The stop is the bar's extreme exactly, with no buffer. The source's optional
filters (range above average, close in the top / bottom third, location at a swing extreme, volume) are presented as options
and not applied; the candlestick (real-body) version is not used. The source's "opposite engulfing bar" exit cue needs future
bars and is not used."""

from __future__ import annotations

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.strategy_sieve import Signal

NAME = "engulfing_bar"
SOURCE = "LuxAlgo, Engulfing Bar (https://www.luxalgo.com/library/concept/engulfing-bar.md)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"hold_bars": 20}


def signals(bars: Bars, hold_bars: int = 20) -> list[Signal]:
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        if bars.t[i] - bars.t[i - 1] != bars.bar_ms:  # the previous bar must be the adjacent one
            continue
        e, hi, lo = float(bars.c[i]), float(bars.h[i]), float(bars.low[i])
        ph, pl = float(bars.h[i - 1]), float(bars.low[i - 1])
        if lo < pl and e > ph and e > lo:
            out.append(Signal(bars.close_time(i), "long", e, lo, None, hold_ms))
        elif hi > ph and e < pl and e < hi:
            out.append(Signal(bars.close_time(i), "short", e, hi, None, hold_ms))
    return out
