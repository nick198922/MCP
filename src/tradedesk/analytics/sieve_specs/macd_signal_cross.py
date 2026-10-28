"""MACD signal-line crossover (G. Appel; settings 12, 26, 9 per StockCharts ChartSchool): MACD = EMA12(close) -
EMA26(close), signal = EMA9 of MACD. MACD crossing above the signal line is bullish (long), crossing below is bearish
(short). Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the source gives no stop and no exit other than
the opposite crossover (needs future bars, not testable), so the exit is by time after ``hold_bars`` = 20 bars and the
stop is ``stop_atr`` = 2.0 x ATR14 from the entry. The signal EMA is seeded with the simple mean of the first 9 MACD
values of a segment (same seeding as the ``ema`` helper). The source describes the bullish / bearish reading only in
secondary sources; the crossover itself is the textbook rule."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "macd_signal_cross"
SOURCE = "G. Appel, MACD; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/macd-moving-average-convergence-divergence-oscillator)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"fast": 12, "slow": 26, "sig": 9, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, fast: int = 12, slow: int = 26, sig: int = 9, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    fast, slow, sig = int(fast), int(slow), int(sig)
    m = len(bars)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    macd = ema(bars.c, fast, pos) - ema(bars.c, slow, pos)
    signal = np.full(m, np.nan)
    k = 2.0 / (sig + 1)
    first = slow - 1  # first segment position with a MACD value
    for i in range(m):
        p = int(pos[i])
        if p < first + sig - 1:
            continue
        if p == first + sig - 1:
            signal[i] = float(macd[i - sig + 1 : i + 1].mean())
        else:
            signal[i] = float(macd[i]) * k + float(signal[i - 1]) * (1 - k)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, m):
        if not (math.isfinite(float(signal[i])) and math.isfinite(float(signal[i - 1]))):
            continue
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        up = macd[i] > signal[i] and macd[i - 1] <= signal[i - 1]
        dn = macd[i] < signal[i] and macd[i - 1] >= signal[i - 1]
        if up and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif dn:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
