"""The Strat 2-1-2 reversal (R. Smith, The Strat; rules from the LuxAlgo article "The Strat"). Bars are compared only with
the bar before: 1 = inside (high below the prior high and low above the prior low), 2U = takes out the prior high only
(high above it, low not below), 2D = takes out the prior low only; ties count as not broken. A reversal is a directional
bar, an inside bar, then a directional bar the other way: 2D-1-2U is long, 2U-1-2D is short. Per the article the trade
triggers on the break of the inside bar, the stop is the other side of the inside bar, the first target is the extreme
of the bar before the inside bar (the high of the 2D bar for a long, its low for the 2U bar for a short).

Sieve adaptation: the entry is the close of the breaking (third) bar, not an intrabar break. A signal whose close is
already at or beyond the target is skipped. Default added by the harvester: the article gives no time exit, so
``hold_bars`` = 20 bars. The article says no independent test of the combos exists. Daily bars."""

from __future__ import annotations

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "strat_212_reversal"
SOURCE = "R. Smith, The Strat; LuxAlgo, The Strat (https://www.luxalgo.com/library/concept/the-strat.md)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"hold_bars": 20}


def signals(bars: Bars, hold_bars: int = 20) -> list[Signal]:
    pos = segment_pos(bars.t)
    hold_ms = int(hold_bars) * bars.bar_ms
    h, lo, c = bars.h, bars.low, bars.c
    out: list[Signal] = []
    for i in range(3, len(bars)):
        if pos[i] < 3:
            continue
        a, b = i - 2, i - 1  # directional bar, inside bar
        if not (h[b] < h[a] and lo[b] > lo[a]):
            continue
        # the directional bar must itself be a 2 relative to the bar before it
        e = float(c[i])
        a_2d = lo[a] < lo[a - 1] and h[a] <= h[a - 1]
        a_2u = h[a] > h[a - 1] and lo[a] >= lo[a - 1]
        i_2u = h[i] > h[b] and lo[i] >= lo[b]
        i_2d = lo[i] < lo[b] and h[i] <= h[b]
        if a_2d and i_2u and e < h[a]:
            out.append(Signal(bars.close_time(i), "long", e, float(lo[b]), float(h[a]), hold_ms))
        elif a_2u and i_2d and e > lo[a]:
            out.append(Signal(bars.close_time(i), "short", e, float(h[b]), float(lo[a]), hold_ms))
    return out
