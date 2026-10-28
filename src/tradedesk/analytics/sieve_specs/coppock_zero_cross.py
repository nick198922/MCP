"""Coppock Curve zero cross (E. Coppock, 1962; rules per StockCharts ChartSchool): Coppock = 10-period WMA (weights
1..10, newest heaviest) of (14-period ROC + 11-period ROC), ROC = 100 x (close / close n bars ago - 1). "A buy signal
triggers with a cross into positive territory": the curve goes from <= 0 to > 0 at the close, long; the source also
gives the cross into negative territory as the sell signal, which is used here as a short entry (cross from >= 0 to
< 0). Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the original timeframe is monthly and the sieve
has no monthly bars, so the same periods (14, 11, 10) are applied to daily bars (the page's own daily example uses
(20, 10, 10) on QQQ; the formula's numbers were taken). The source gives no stop and no exit (Coppock did not use sells),
so the stop is ``stop_atr`` = 2.0 x ATR14 from the entry and the exit is by time after ``hold_bars`` = 20 bars. The
short side is the commonly used reading of the source, not Coppock's own rule."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "coppock_zero_cross"
SOURCE = "E. S. Coppock (1962), Coppock Curve; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/coppock-curve)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"roc_a": 14, "roc_b": 11, "wma": 10, "stop_atr": 2.0, "hold_bars": 20}


def coppock(c: np.ndarray, pos: np.ndarray, roc_a: int, roc_b: int, wma: int) -> np.ndarray:
    """Coppock curve per bar, NaN until defined inside the segment."""
    m = c.size
    out = np.full(m, np.nan)
    w = np.arange(1, wma + 1, dtype=float)
    ws = float(w.sum())
    s = np.full(m, np.nan)
    for i in range(m):
        if pos[i] >= max(roc_a, roc_b) and c[i - roc_a] > 0 and c[i - roc_b] > 0:
            s[i] = 100.0 * (c[i] / c[i - roc_a] - 1.0) + 100.0 * (c[i] / c[i - roc_b] - 1.0)
    for i in range(m):
        if pos[i] >= max(roc_a, roc_b) + wma - 1:
            win = s[i - wma + 1 : i + 1]
            if np.all(np.isfinite(win)):
                out[i] = float(np.dot(win, w) / ws)
    return out


def signals(bars: Bars, roc_a: int = 14, roc_b: int = 11, wma: int = 10, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    k = coppock(bars.c, pos, int(roc_a), int(roc_b), int(wma))
    hold = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        ai = float(a[i])
        if not (np.isfinite(k[i]) and np.isfinite(k[i - 1]) and np.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        if k[i] > 0 and k[i - 1] <= 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold))
        elif k[i] < 0 and k[i - 1] >= 0:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold))
    return out
