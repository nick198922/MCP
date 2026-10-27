"""Turtle Soup (L. Connors, L. B. Raschke, "Street Smarts: High Probability Short-Term Trading Strategies", 1995/96): a
failed 20-day breakout. Long setup: today's low makes a new 20-day low (under the lowest low of the previous 20 bars)
and that previous 20-day low was set at least 4 bars earlier. The source then buys with a stop a few ticks above the
previous 20-day low, with the protective stop one tick under today's low. Short is the mirror image.

Sieve adaptation (entry is a bar close, no stop orders): the signal fires at the close of the bar that made the new low
if that close is back above the previous 20-day low, i.e. the buy stop of the source would have been touched on the same
bar. The stop is the signal bar's low (source: one tick below), no target. Defaults added by the harvester: the source
trails the stop, so the exit is a time exit after ``hold_bars`` = 20 daily bars. Daily bars. Source parameters, no
variants."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "turtle_soup"
SOURCE = "L. Connors, L. B. Raschke, Street Smarts (1995): Turtle Soup, 20-day low, prior low at least 4 days old (https://www.luxalgo.com/library/concept/turtle-soup/)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 20, "min_age": 4, "hold_bars": 20}


def signals(bars: Bars, n: int = 20, min_age: int = 4, hold_bars: int = 20) -> list[Signal]:
    n = int(n)
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        for i in range(len(b)):
            if pos[i] < n:
                continue
            w = b.low[i - n : i]  # previous n bars, not bar i
            prev_low = float(w.min())
            age = n - int(np.flatnonzero(w == prev_low)[-1])  # bars since the most recent occurrence
            lo_i, e = float(b.low[i]), float(b.c[i])
            if lo_i < prev_low and age >= min_age and e > prev_low and e > lo_i:
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * lo_i, None, int(hold_bars) * b.bar_ms))
    return out
