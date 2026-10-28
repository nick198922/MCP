"""Vortex Indicator crossover (E. Botes, D. Siepman, 2010; rules per StockCharts ChartSchool): +VM = |high - prior low|,
-VM = |low - prior high|, TR = max(high - low, |high - prior close|, |low - prior close|); +VI14 = 14-bar sum of +VM /
14-bar sum of TR, -VI14 likewise. "A bullish signal triggers when the positive trend indicator crosses above the
negative trend indicator": +VI14 crosses above -VI14 goes long at the close, -VI14 crosses above +VI14 goes short.
Daily bars. The source's optional confirmations (+VM above 1, the 0.90 / 1.10 thresholds) are not applied.

Defaults added by the harvester (fixed before any run, no variants): the source gives no stop and no exit other than the
opposite signal (needs future bars), so the stop is ``stop_atr`` = 2.0 x ATR14 from the entry and the exit is by time
after ``hold_bars`` = 20 bars. Period 14 is the source's usual setting."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "vortex_cross"
SOURCE = "E. Botes, D. Siepman (2010), Vortex Indicator; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/vortex-indicator)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 14, "stop_atr": 2.0, "hold_bars": 20}


def vortex(b: Bars, pos: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """(+VI, -VI) per bar, NaN until ``n`` bars with a prior bar exist in the segment."""
    m = len(b)
    vp = np.full(m, np.nan)
    vm = np.full(m, np.nan)
    if m < 2:
        return vp, vm
    pm = np.abs(b.h[1:] - b.low[:-1])
    mm = np.abs(b.low[1:] - b.h[:-1])
    tr = np.maximum.reduce([b.h[1:] - b.low[1:], np.abs(b.h[1:] - b.c[:-1]), np.abs(b.low[1:] - b.c[:-1])])
    for i in range(m):
        if pos[i] < n:
            continue
        s = float(tr[i - n : i].sum())  # entries i-n..i-1 are bars i-n+1..i
        if s > 0:
            vp[i] = float(pm[i - n : i].sum()) / s
            vm[i] = float(mm[i - n : i].sum()) / s
    return vp, vm


def signals(bars: Bars, n: int = 14, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    vp, vm = vortex(bars, pos, int(n))
    out: list[Signal] = []
    for i in range(1, len(bars)):
        ai = float(a[i])
        if not (np.isfinite(vp[i]) and np.isfinite(vp[i - 1]) and np.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        hold = int(hold_bars) * bars.bar_ms
        if vp[i] > vm[i] and vp[i - 1] <= vm[i - 1]:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold))
        elif vm[i] > vp[i] and vm[i - 1] <= vp[i - 1]:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold))
    return out
