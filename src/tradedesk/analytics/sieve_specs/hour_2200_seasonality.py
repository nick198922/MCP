"""Intraday seasonality in Bitcoin (Quantpedia, from Padysak and Vojtko, "Seasonality, Trend-following, and Mean
reversion in Bitcoin"): open a long at 22:00 UTC and hold it for two hours (closed at 00:00 UTC). Long only. The
paper's data are BTC on Gemini, 2015-2022; the rule is a time-of-day effect, with 22:00 and 23:00 UTC the strongest
hours.

The sieve entry is the close of the 21:00 UTC hourly bar, which is the 22:00 UTC price. The spec sees only bars, not the
symbol, so it runs on every symbol of the universe, not only BTC (a wider test of the same time-of-day idea).

Default added by the harvester: the source gives no stop, so ``stop_atr`` = 2.0 x ATR14 of the hourly bars below the
entry (harvester default). The hold of two hours is the source's own."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "hour_2200_seasonality"
SOURCE = "Quantpedia, Intraday Seasonality in Bitcoin; Padysak, Vojtko (https://quantpedia.com/strategies/intraday-seasonality-in-bitcoin)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"entry_hour": 22, "hold_hours": 2, "stop_atr": 2.0}


def signals(bars: Bars, entry_hour: int = 22, hold_hours: int = 2, stop_atr: float = 2.0) -> list[Signal]:
    if bars.bar_ms != HOUR_MS:
        return []
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    out: list[Signal] = []
    for i in range(len(bars)):
        t_close = bars.close_time(i)
        if (t_close // HOUR_MS) % 24 != int(entry_hour):
            continue
        ai = float(a[i])
        e = float(bars.c[i])
        if math.isfinite(ai) and ai > 0 and e - stop_atr * ai > 0:
            out.append(Signal(t_close, "long", e, e - stop_atr * ai, None, int(hold_hours) * HOUR_MS))
    return out
