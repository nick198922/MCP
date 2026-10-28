"""Pring's Know Sure Thing, signal-line crossover (M. Pring; daily settings and rules per StockCharts ChartSchool):
KST = 1 x SMA10(ROC10) + 2 x SMA10(ROC15) + 3 x SMA10(ROC20) + 4 x SMA15(ROC30); the signal line is SMA9 of KST. KST
crossing above its signal line is bullish (long), crossing below is bearish (short); the page calls the signal-line and
centerline crossovers "usually the most robust". Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the page gives no stop and no exit, so the exit is by
time after ``hold_bars`` = 20 bars and the stop is ``stop_atr`` = 2.0 x ATR14 from the entry. Only the signal-line cross is
traded (the zero-line reading is not). The page's scans add a KST sign filter (KST > 0 for the long cross, KST < 0 for
the short cross); it is NOT applied here, every signal-line cross is traded, so this is "KST signal cross without a sign
filter". ROC is the percent change over n bars. Similar family to ``coppock_zero_cross``
(ROC-based momentum) but a different construction and trigger."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "kst_signal_cross"
SOURCE = "M. Pring, Know Sure Thing; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/prings-know-sure-thing-kst)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"sig": 9, "stop_atr": 2.0, "hold_bars": 20}
LEGS = ((10, 10, 1.0), (15, 10, 2.0), (20, 10, 3.0), (30, 15, 4.0))  # (ROC length, SMA length, weight)


def roc(c: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    out = np.full(c.size, np.nan)
    for i in range(c.size):
        if pos[i] >= n and c[i - n] != 0:
            out[i] = 100.0 * (c[i] / c[i - n] - 1.0)
    return out


def sma_valid(x: np.ndarray, n: int) -> np.ndarray:
    """Mean of the last ``n`` values; NaN unless all are finite (a window across a segment start holds NaN)."""
    out = np.full(x.size, np.nan)
    for i in range(n - 1, x.size):
        w = x[i - n + 1 : i + 1]
        if np.all(np.isfinite(w)):
            out[i] = float(w.mean())
    return out


def signals(bars: Bars, sig: int = 9, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    m = len(bars)
    if m == 0:
        return []
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    kst = np.zeros(m)
    for r_n, s_n, w in LEGS:
        kst = kst + w * sma_valid(roc(bars.c, r_n, pos), s_n)
    signal = sma_valid(kst, int(sig))
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, m):
        k_i, k_p, s_i, s_p = float(kst[i]), float(kst[i - 1]), float(signal[i]), float(signal[i - 1])
        if not all(math.isfinite(v) for v in (k_i, k_p, s_i, s_p)):
            continue
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        if k_i > s_i and k_p <= s_p and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif k_i < s_i and k_p >= s_p:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
