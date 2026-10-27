"""NR7 narrow-range breakout (T. Crabel, "Day Trading with Short Term Price Patterns and Opening Range Breakout", 1990):
an NR7 day is a UTC day whose high-low range is the narrowest of the last 7 days (itself included). The next day the
source brackets the NR7 day with entry stops above its high and below its low. Long on the first 1h close above the NR7
high, short on the first 1h close below the NR7 low, one signal a day, the hour closing at 24:00 cannot signal.

Sieve adaptation (entry is a bar close, not a stop order). Defaults added by the harvester (the source gives no exit
in the pages read): the stop is the opposite side of the NR7 day (stop-and-reverse level) and the exit is the next
00:00 UTC (the close of the breakout day). The 7 days must be complete and consecutive (24 hourly bars each). Source
parameters (7), no variants."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars
from tradedesk.analytics.strategy_sieve import Signal

NAME = "nr7_breakout"
SOURCE = "T. Crabel (1990), NR7 narrow range day (https://www.luxalgo.com/library/concept/nr4-nr7-narrow-range-bars.md)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"n": 7}


def signals(bars: Bars, n: int = 7) -> list[Signal]:
    n = int(n)
    if bars.bar_ms != HOUR_MS or len(bars) == 0:
        return []
    cnt_full = DAY_MS // HOUR_MS
    day = bars.t // DAY_MS
    starts = np.flatnonzero(np.concatenate([[True], day[1:] != day[:-1]]))
    ends = np.concatenate([starts[1:], [len(bars)]])
    dh = np.maximum.reduceat(bars.h, starts)
    dl = np.minimum.reduceat(bars.low, starts)
    dday = day[starts]
    full = (ends - starts == cnt_full) & (bars.t[starts] == dday * DAY_MS)
    rng = dh - dl
    out: list[Signal] = []
    for g in range(n, len(starts)):
        j = g - 1  # the candidate NR7 day is the day before day g
        win = slice(j - n + 1, j + 1)
        if not (np.all(full[win]) and dday[g] - dday[j - n + 1] == n and rng[j] > 0):
            continue
        if rng[j] > rng[win].min():
            continue
        hi, lo = float(dh[j]), float(dl[j])
        a, b = int(starts[g]), int(ends[g])
        seg = bars.c[a:b]
        hit = np.flatnonzero((seg > hi) | (seg < lo))
        if hit.size == 0:
            continue
        i = a + int(hit[0])
        t_ms = bars.close_time(i)
        hold_ms = (int(dday[g]) + 1) * DAY_MS - t_ms
        if hold_ms <= 0:
            continue
        e = float(bars.c[i])
        if e > hi:
            out.append(Signal(t_ms, "long", e, lo, None, hold_ms))
        else:
            out.append(Signal(t_ms, "short", e, hi, None, hold_ms))
    return out
