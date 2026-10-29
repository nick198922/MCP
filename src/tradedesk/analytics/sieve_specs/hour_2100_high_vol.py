"""Bitcoin seasonality in a high-volatility regime (Quantpedia, "The Seasonality of Bitcoin"): buy at 21:00 UTC and sell
at 23:00 UTC (a two-hour hold), only on days classed "High Volatility". The class is checked at 00:00 UTC: the 30-day
historical volatility of hourly bars (sample standard deviation of 720 hourly log returns ending at that 00:00 close) is
higher than its moving median over the trailing 365 days. Long only, hourly bars.

Readings and defaults by the harvester (fixed before any run, no variants): the trailing median is taken over the 00:00
UTC values of the last 365 days (the page says "recalculated as a moving median", not at which frequency), requiring at
least 300 of them; a day whose 720-hour window has a hole in the bars is skipped. The source has no stop, so
``stop_atr`` = 2.0 x ATR14 of hourly bars below the entry (as in ``hour_2200_seasonality``, which is the same idea at
22:00 with no regime filter and is a different row). The entry is the 21:00 UTC close of the bar that opened at 20:00.
The page is about BTC; the spec does not see the symbol, so it runs on the whole universe."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "hour_2100_high_vol"
SOURCE = "Quantpedia, The Seasonality of Bitcoin (https://quantpedia.com/the-seasonality-of-bitcoin/)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"entry_hour": 21, "hold_hours": 2, "vol_hours": 720, "median_days": 365, "min_days": 300, "stop_atr": 2.0}


def signals(
    bars: Bars,
    entry_hour: int = 21,
    hold_hours: int = 2,
    vol_hours: int = 720,
    median_days: int = 365,
    min_days: int = 300,
    stop_atr: float = 2.0,
) -> list[Signal]:
    if bars.bar_ms != HOUR_MS or len(bars) <= vol_hours + 1:
        return []
    m = len(bars)
    w = int(vol_hours)
    lr = np.zeros(m)
    lr[1:] = np.log(bars.c[1:] / bars.c[:-1])
    s1 = np.concatenate([[0.0], np.cumsum(lr)])
    s2 = np.concatenate([[0.0], np.cumsum(lr * lr)])
    close_t = bars.t + HOUR_MS
    bnd = np.flatnonzero(close_t % DAY_MS == 0)  # bars closing at 00:00 UTC
    vol = np.full(bnd.size, np.nan)
    for k, j in enumerate(bnd):
        j = int(j)
        if j - w < 0 or int(bars.t[j] - bars.t[j - w]) != w * HOUR_MS:
            continue  # window needs bars j-w .. j with no hole (returns j-w+1 .. j)
        sw1 = s1[j + 1] - s1[j - w + 1]
        sw2 = s2[j + 1] - s2[j - w + 1]
        var = (sw2 - sw1 * sw1 / w) / (w - 1)
        if var > 0:
            vol[k] = math.sqrt(var)
    bnd_t = close_t[bnd]
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    out: list[Signal] = []
    for i in range(m):
        t_close = int(close_t[i])
        if (t_close // HOUR_MS) % 24 != int(entry_hour):
            continue
        j_t = t_close - int(entry_hour) * HOUR_MS  # 00:00 UTC of the same day
        k = int(np.searchsorted(bnd_t, j_t))
        if k >= bnd.size or int(bnd_t[k]) != j_t or not math.isfinite(float(vol[k])):
            continue
        lo = int(np.searchsorted(bnd_t, j_t - int(median_days) * DAY_MS, side="right"))
        hist = vol[lo : k + 1]
        hist = hist[np.isfinite(hist)]
        if hist.size < int(min_days) or not vol[k] > float(np.median(hist)):
            continue
        ai = float(a[i])
        e = float(bars.c[i])
        if math.isfinite(ai) and ai > 0 and e - stop_atr * ai > 0:
            out.append(Signal(t_close, "long", e, e - stop_atr * ai, None, int(hold_hours) * HOUR_MS))
    return out
