"""Camarilla pivot H3 / L3 fade (N. Scott's Camarilla equation; rules per LuxAlgo "Camarilla"): with R = previous UTC day
high - low and C = previous close, H3 = C + 1.1 R / 4, H4 = C + 1.1 R / 2, L3 = C - 1.1 R / 4, L4 = C - 1.1 R / 2. On a
day that opens between L3 and H3, sell a rejection of H3 (the stop beyond H4), buy a rejection of L3 (the stop beyond
L4), expecting a rotation back across the range. 1h bars, the day is the UTC day.

Choices made here (the source leaves them open), fixed before any run, no variants:
- Rejection = a 1h bar whose high reaches H3 (low reaches L3) and whose close is back inside (below H3 / above L3);
  entry at that close; the first such bar per side per day; H4 (L4) must not have been touched yet that day.
- Stop exactly at H4 (L4). The source says "beyond"; no buffer is invented.
- Target: the previous close C (the middle of the ladder: every Camarilla level is measured from C; the source gives
  no fixed target, one EA spec uses the pivot). Skipped when the entry is already beyond C.
- Exit by time at 00:00 UTC of the next day (intraday rule).
- The previous day must be complete (24 hourly bars) and consecutive; the day's first bar must be the 00:00 hour."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars
from tradedesk.analytics.strategy_sieve import Signal

NAME = "camarilla_h3_l3_fade"
SOURCE = "N. Scott, Camarilla equation; rules per LuxAlgo (https://www.luxalgo.com/library/concept/camarilla/)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"mult": 1.1}


def signals(bars: Bars, mult: float = 1.1) -> list[Signal]:
    n = len(bars)
    if bars.bar_ms != HOUR_MS or n == 0:
        return []
    day = bars.t // DAY_MS
    starts = np.flatnonzero(np.concatenate([[True], day[1:] != day[:-1]]))
    ends = np.concatenate([starts[1:], [n]])
    out: list[Signal] = []
    for g in range(1, starts.size):
        a, b = int(starts[g]), int(ends[g])
        pa, pb = int(starts[g - 1]), int(ends[g - 1])
        if pb - pa != 24 or int(day[a]) - int(day[pa]) != 1 or int(bars.t[a]) != int(day[a]) * DAY_MS:
            continue
        rng = float(bars.h[pa:pb].max() - bars.low[pa:pb].min())
        c0 = float(bars.c[pb - 1])
        if not rng > 0:
            continue
        h3, h4 = c0 + mult * rng / 4.0, c0 + mult * rng / 2.0
        l3, l4 = c0 - mult * rng / 4.0, c0 - mult * rng / 2.0
        if not (l3 < float(bars.o[a]) < h3) or l4 <= 0:
            continue
        done_s = done_l = False
        hi = -np.inf
        lo = np.inf
        for i in range(a, b):
            hi = max(hi, float(bars.h[i]))
            lo = min(lo, float(bars.low[i]))
            t_ms = bars.close_time(i)
            hold_ms = (int(day[a]) + 1) * DAY_MS - t_ms
            if hold_ms <= 0:
                continue
            e = float(bars.c[i])
            if not done_s and bars.h[i] >= h3 and e < h3 and hi < h4:
                done_s = True
                if e > c0:
                    out.append(Signal(t_ms, "short", e, h4, c0, hold_ms))
            if not done_l and bars.low[i] <= l3 and e > l3 and lo > l4:
                done_l = True
                if e < c0:
                    out.append(Signal(t_ms, "long", e, l4, c0, hold_ms))
    return out
