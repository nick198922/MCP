"""Elder Impulse System entry (A. Elder; rules per StockCharts ChartSchool). A bar is green when EMA13(close) and the
MACD(12, 26, 9) histogram are both higher than on the previous bar, red when both are lower. Entry: the bar on which the
system turns green (previous bar not green) while the long-term trend is bullish -> long; turns red (previous not red)
while the long-term trend is bearish -> short. Long-term trend: MACD(1, 65, 1) = close - EMA65(close), positive = up
(the page: the weekly check can use a 65-day EMA, five times the 13-day EMA). Daily bars; "the page's daily buy
signals that occur when the weekly trend is not clearly up are ignored".

Defaults and readings added by the harvester (fixed before any run, no variants): the page gives no stop and no exit
beyond "enter cautiously but exit fast" -> stop ``stop_atr`` = 2.0 x ATR14, time exit ``hold_bars`` = 20 bars. The
trigger on the *first* green (red) bar is the harvester's reading of "the daily system turns green". The signal EMA of
the histogram is seeded as in ``macd_signal_cross`` (mean of the first 9 MACD values)."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "elder_impulse_green"
SOURCE = "A. Elder, Impulse System; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/chart-analysis/chart-types/elder-impulse-system)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"ema_n": 13, "trend_n": 65, "stop_atr": 2.0, "hold_bars": 20}


def macd_hist(bars: Bars, pos: np.ndarray, fast: int = 12, slow: int = 26, sig: int = 9) -> np.ndarray:
    macd = ema(bars.c, fast, pos) - ema(bars.c, slow, pos)
    m = len(bars)
    signal = np.full(m, np.nan)
    k = 2.0 / (sig + 1)
    first = slow - 1
    for i in range(m):
        p = int(pos[i])
        if p < first + sig - 1:
            continue
        if p == first + sig - 1:
            signal[i] = float(macd[i - sig + 1 : i + 1].mean())
        else:
            signal[i] = float(macd[i]) * k + float(signal[i - 1]) * (1 - k)
    return macd - signal


def signals(bars: Bars, ema_n: int = 13, trend_n: int = 65, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    m = len(bars)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    e13 = ema(bars.c, int(ema_n), pos)
    hist = macd_hist(bars, pos)
    trend = bars.c - ema(bars.c, int(trend_n), pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    green = np.zeros(m, dtype=bool)
    red = np.zeros(m, dtype=bool)
    for i in range(1, m):
        if not all(math.isfinite(float(x)) for x in (e13[i], e13[i - 1], hist[i], hist[i - 1])):
            continue
        green[i] = bool(e13[i] > e13[i - 1] and hist[i] > hist[i - 1])
        red[i] = bool(e13[i] < e13[i - 1] and hist[i] < hist[i - 1])
    out: list[Signal] = []
    for i in range(2, m):
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0 and math.isfinite(float(trend[i]))):
            continue
        e = float(bars.c[i])
        if green[i] and not green[i - 1] and trend[i] > 0 and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif red[i] and not red[i - 1] and trend[i] < 0:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
