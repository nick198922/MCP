"""TRIX signal-line crossover (J. Hutson, TRIX 1983; settings 15 and 9 and rules per StockCharts ChartSchool): TRIX is the
1-period percent change of a triple-smoothed EMA of the close (EMA15 of EMA15 of EMA15); the signal line is the EMA9 of
TRIX. TRIX crossing above the signal line is bullish (long), crossing below is bearish (short). Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the source gives no stop and no exit, so the exit is
by time after ``hold_bars`` = 20 bars and the stop is ``stop_atr`` = 2.0 x ATR14 from the entry. Each EMA is seeded with
the simple mean of its first ``n`` valid values of a segment (same seeding as the ``ema`` helper, applied to the chain).
The zero-line reading of the source is not traded; only the signal-line cross. The source page itself gives no
inventor, and the 1983 date comes from general knowledge, not from the page."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "trix_signal_cross"
SOURCE = "J. Hutson, TRIX; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/trix)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 15, "sig": 9, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, n: int = 15, sig: int = 9, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n, sig = int(n), int(sig)
    m = len(bars)
    if m == 0:
        return []
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    # each further EMA starts (n - 1) bars later: shift the segment position so the helper's seed window is valid
    e1 = ema(bars.c, n, pos)
    e2 = ema(e1, n, pos - (n - 1))
    e3 = ema(e2, n, pos - 2 * (n - 1))
    trix = np.full(m, np.nan)
    for i in range(1, m):
        if pos[i] >= 3 * (n - 1) + 1 and math.isfinite(float(e3[i])) and math.isfinite(float(e3[i - 1])) and e3[i - 1] != 0:
            trix[i] = float(e3[i]) / float(e3[i - 1]) - 1.0
    signal = ema(trix, sig, pos - (3 * (n - 1) + 1))
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, m):
        t_i, t_p, s_i, s_p = float(trix[i]), float(trix[i - 1]), float(signal[i]), float(signal[i - 1])
        if not all(math.isfinite(v) for v in (t_i, t_p, s_i, s_p)):
            continue
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        if t_i > s_i and t_p <= s_p and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif t_i < s_i and t_p >= s_p:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
