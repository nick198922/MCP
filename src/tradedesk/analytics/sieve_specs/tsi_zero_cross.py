"""True Strength Index zero-line cross (W. Blau, TSI; StockCharts ChartSchool): PC = close - previous close; TSI = 100 x
EMA13(EMA25(PC)) / EMA13(EMA25(|PC|)), EMAs seeded with the simple mean (the page says they start from an SMA). A cross
of TSI above zero is bullish (long), below zero bearish (short). Settings 25 / 13 are the page's own. Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the page gives no stop and no exit other than the
opposite cross (needs future bars), so the stop is ``stop_atr`` = 2.0 x ATR14 and the exit is by time after
``hold_bars`` = 20 bars. The page calls the signal-line cross the most common signal and the zero cross the other one,
without naming a signal period for 25 / 13 (examples use 7 and 10); the zero cross is taken because it needs no invented
period. The page sets no overbought / oversold levels for 25 / 13, so none is used."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "tsi_zero_cross"
SOURCE = "W. Blau, True Strength Index; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/true-strength-index.md)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"long_n": 25, "short_n": 13, "stop_atr": 2.0, "hold_bars": 20}


def ema_valid(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """EMA over the finite values of ``x`` (NaN before the first valid one), seeded with the simple mean of the first
    ``n`` valid values of a segment; NaN until seeded; resets at the start of each segment."""
    out = np.full(x.size, np.nan)
    k = 2.0 / (n + 1)
    prev = math.nan
    acc = 0.0
    cnt = 0
    for i in range(x.size):
        if pos[i] == 0:
            prev = math.nan
            acc = 0.0
            cnt = 0
        xi = float(x[i])
        if not math.isfinite(xi):
            continue
        if cnt < n:
            acc += xi
            cnt += 1
            if cnt == n:
                prev = acc / n
                out[i] = prev
            continue
        prev = xi * k + prev * (1 - k)
        out[i] = prev
    return out


def signals(bars: Bars, long_n: int = 25, short_n: int = 13, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    m = len(bars)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    pc = np.full(m, np.nan)
    for i in range(1, m):
        if pos[i] > 0:
            pc[i] = bars.c[i] - bars.c[i - 1]
    num = ema_valid(ema_valid(pc, int(long_n), pos), int(short_n), pos)
    den = ema_valid(ema_valid(np.abs(pc), int(long_n), pos), int(short_n), pos)
    tsi = np.full(m, np.nan)
    ok = np.isfinite(num) & np.isfinite(den) & (den > 0)
    tsi[ok] = 100.0 * num[ok] / den[ok]
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, m):
        if not (math.isfinite(float(tsi[i])) and math.isfinite(float(tsi[i - 1]))):
            continue
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        if tsi[i] > 0 >= tsi[i - 1]:
            if e - stop_atr * ai > 0:
                out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif tsi[i] < 0 <= tsi[i - 1]:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
