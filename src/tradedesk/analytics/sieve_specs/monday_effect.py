"""Monday effect in Bitcoin (C. Caporale, A. Plastun, "The day of the week effect in the cryptocurrency market",
Finance Research Letters 2019; trading form per Quantpedia): open a long position at the start of Monday and close it at
the end of the day. Daily bars; Monday is the UTC day.

Crypto trades continuously, so the start of Monday is the 00:00 UTC open, the same price as the close of the Sunday bar:
the signal is the close of the Sunday bar, the hold is exactly one day.

Defaults added by the harvester (fixed before any run, no variants): the source has no stop, so ``stop_atr`` = 2.0 x
ATR14 below the entry. The source finds the effect for Bitcoin only (not the other three coins it tested) and says the
returns are in most cases not statistically different from random trading; the sieve applies the rule to the whole
universe, since a specification does not see the symbol. Long only, as in the source."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import DAY_MS, Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "monday_effect"
SOURCE = "Caporale, Plastun (2019), The day of the week effect in the cryptocurrency market; Quantpedia (https://quantpedia.com/the-day-of-the-week-effect-in-the-crypto-currency-market/)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"stop_atr": 2.0}


def signals(bars: Bars, stop_atr: float = 2.0) -> list[Signal]:
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    out: list[Signal] = []
    for i in range(len(bars)):
        if (int(bars.t[i]) // DAY_MS + 3) % 7 != 6:  # 1970-01-01 was a Thursday; 6 = Sunday, its close opens Monday
            continue
        ai = float(a[i])
        e = float(bars.c[i])
        if math.isfinite(ai) and ai > 0 and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, DAY_MS))
    return out
