"""Halloween indicator / "sell in May" (S. Bouman, B. Jacobsen, American Economic Review 2002): hold risk assets only
from November to April. Rule here: long at the close of the last bar of October (= 00:00 UTC on 1 November), exit at
00:00 UTC on 1 May. Daily bars. Long only: the sources describe summer returns as close to zero, not negative, so no
short is derived.

Defaults added by the harvester (fixed before any run, no variants): LuxAlgo says there is no official cutoff
(studies shift the boundaries by days); the classic dates 1 Nov to 1 May are used. The sources give no stop ->
``stop_atr`` = 2.0 x ATR14 (a tight stop for a ~181-day hold, so the test measures "Nov-Apr with a 2 ATR stop", not
buy-and-hold). The original evidence is equity indices (37 countries); for crypto only ~6 seasons exist in the data, so
the verdict is expected to be "too few trades"."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "halloween_nov_apr"
SOURCE = (
    "S. Bouman, B. Jacobsen, The Halloween Indicator, Sell in May and Go Away: Another Puzzle, AER 2002 "
    "(https://pure.eur.nl/en/publications/the-halloween-indicator-sell-in-may-and-go-away-another-puzzle/); dates per LuxAlgo "
    "(https://www.luxalgo.com/library/concept/sell-in-may.md)"
)
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"stop_atr": 2.0}


def signals(bars: Bars, stop_atr: float = 2.0) -> list[Signal]:
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    out: list[Signal] = []
    for i in range(len(bars)):
        t_close = int(bars.close_time(i))
        close_dt = np.datetime64(t_close, "ms")
        month = close_dt.astype("datetime64[M]")
        if close_dt != month.astype("datetime64[ms]") or int(month.astype(int) % 12) != 10:  # 1 Nov 00:00 UTC
            continue
        ai = float(a[i])
        e = float(bars.c[i])
        if not (math.isfinite(ai) and ai > 0 and e - stop_atr * ai > 0):
            continue
        end = int((month + np.timedelta64(6, "M")).astype("datetime64[ms]").astype(np.int64))  # 1 May 00:00 UTC
        out.append(Signal(t_close, "long", e, e - stop_atr * ai, None, end - t_close))
    return out
