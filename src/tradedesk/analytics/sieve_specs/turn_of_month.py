"""Turn-of-the-month effect (J. Lakonishok, S. Smidt, "Are Seasonal Anomalies Real? A Ninety-Year Perspective", Review
of Financial Studies 1988; trading form as in Quantpedia: buy the close one day before month end, sell at the close of
the third trading day of the new month). Long only: buy at the close of the second-to-last UTC calendar day of a month,
hold 4 daily bars (the last day of the month and the first three days of the next).

Crypto trades every day, so "trading day" is the UTC calendar day. The date test uses only the bar's own timestamp, no
future bars. Defaults added by the harvester (the source has no stop): ``stop_atr`` = 2.0 x ATR14 below the entry.
Source parameters, no variants."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "turn_of_month"
SOURCE = "Lakonishok & Smidt (1988), turn of the month, last day + first 3 days (https://quantpedia.com/strategies/turn-of-the-month-in-equity-indexes)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"hold_bars": 4, "stop_atr": 2.0}


def is_second_to_last_day(t_ms: int) -> bool:
    """True if the UTC day of ``t_ms`` is the second-to-last calendar day of its month."""
    d = np.datetime64(int(t_ms), "ms").astype("datetime64[D]")
    month = d.astype("datetime64[M]")
    return bool(
        (d + np.timedelta64(1, "D")).astype("datetime64[M]") == month and (d + np.timedelta64(2, "D")).astype("datetime64[M]") != month
    )


def signals(bars: Bars, hold_bars: int = 4, stop_atr: float = 2.0) -> list[Signal]:
    if bars.bar_ms != DAY_MS:
        return []
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(len(bars)):
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0 and is_second_to_last_day(int(bars.t[i]))):
            continue
        e = float(bars.c[i])
        if e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
    return out
