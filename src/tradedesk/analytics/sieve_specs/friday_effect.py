"""Friday effect in Bitcoin (T. Vidal, Cointelegraph, "New Analysis Finds That Mondays Are the Best Days to Buy Bitcoin",
2020-01-18): buy at the closing price on the day before and sell at the closing price of the target day, UTC days; for
Friday that is buy Thursday's close, sell Friday's close. The author's one-year sample (2019 to 13 Jan 2020) shows Friday
as the best day (average +1.1 %); his longer sample (2013-2020) favours Monday instead, and Friday is not significant
there. Long only.

Daily bars; Thursday's bar closes at 00:00 UTC Friday, so the signal is the close of the Thursday bar and the hold is
exactly one day.

Defaults added by the harvester (fixed before any run, no variants): the source has no stop, so ``stop_atr`` = 2.0 x
ATR14 below the entry. The source is BTC only; the sieve applies it to the whole universe, since a specification does
not see the symbol. The weekday is chosen from a short sample that the author himself treats as the less reliable one;
the test is a check of that claim on a wider and later sample, not an endorsement. Sibling of ``monday_effect``."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import DAY_MS, Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "friday_effect"
SOURCE = "T. Vidal, Cointelegraph, New Analysis Finds That Mondays Are the Best Days to Buy Bitcoin (2020) (https://cointelegraph.com/markets/new-analysis-finds-that-mondays-are-the-best-days-to-buy-bitcoin)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"stop_atr": 2.0}


def signals(bars: Bars, stop_atr: float = 2.0) -> list[Signal]:
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    out: list[Signal] = []
    for i in range(len(bars)):
        if (int(bars.t[i]) // DAY_MS + 3) % 7 != 3:  # 1970-01-01 was a Thursday; 3 = Thursday, its close opens Friday
            continue
        ai = float(a[i])
        e = float(bars.c[i])
        if math.isfinite(ai) and ai > 0 and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, DAY_MS))
    return out
