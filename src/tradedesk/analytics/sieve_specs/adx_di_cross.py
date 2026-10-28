"""Wilder's ADX / directional movement system (J. W. Wilder, "New Concepts in Technical Trading Systems", 1978; rules per
StockCharts ChartSchool): buy when +DI14 crosses above -DI14 while ADX14 is above 25, with the initial stop at the
signal day's low; sell short when -DI14 crosses above +DI14 with ADX14 above 25, stop at the signal day's high. Daily bars.

Calculation as in the source (Wilder smoothing: first value is the sum of 14 periods, then prior - prior / 14 + current;
the first ADX is the mean of 14 DX values, then (prior x 13 + DX) / 14).

Defaults added by the harvester (fixed before any run, no variants): the source gives no profit target or exit
(trailing stop, Parabolic SAR) so the exit is by time after ``hold_bars`` = 20 bars; the threshold 25 is the source's
"strong trend" level (it notes 20 is used by many). The source keeps the signal valid while the signal-day low holds;
here only the crossover bar itself signals."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "adx_di_cross"
SOURCE = "J. W. Wilder, New Concepts in Technical Trading Systems (1978); StockCharts ChartSchool ADX (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/average-directional-index-adx)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 14, "adx_min": 25.0, "hold_bars": 20}


def dmi(b: Bars, pos: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """+DI, -DI and ADX (Wilder), segment-aware; NaN until defined."""
    m = len(b)
    pdi = np.full(m, np.nan)
    mdi = np.full(m, np.nan)
    adx = np.full(m, np.nan)
    s_tr = s_p = s_m = 0.0
    dxs: list[float] = []
    adx_prev = math.nan
    for i in range(m):
        p = int(pos[i])
        if p == 0:
            s_tr = s_p = s_m = 0.0
            dxs = []
            adx_prev = math.nan
            continue
        up = float(b.h[i] - b.h[i - 1])
        dn = float(b.low[i - 1] - b.low[i])
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        pc = float(b.c[i - 1])
        tr = max(float(b.h[i] - b.low[i]), abs(float(b.h[i]) - pc), abs(float(b.low[i]) - pc))
        if p <= n:
            s_tr += tr
            s_p += pdm
            s_m += mdm
            if p < n:
                continue
        else:
            s_tr = s_tr - s_tr / n + tr
            s_p = s_p - s_p / n + pdm
            s_m = s_m - s_m / n + mdm
        if s_tr <= 0:
            continue
        pdi[i] = 100.0 * s_p / s_tr
        mdi[i] = 100.0 * s_m / s_tr
        tot = pdi[i] + mdi[i]
        dx = 100.0 * abs(pdi[i] - mdi[i]) / tot if tot > 0 else 0.0
        if p < 2 * n - 1:
            dxs.append(dx)
        elif p == 2 * n - 1:
            dxs.append(dx)
            adx_prev = sum(dxs) / len(dxs)
            adx[i] = adx_prev
        elif math.isfinite(adx_prev):
            adx_prev = (adx_prev * (n - 1) + dx) / n
            adx[i] = adx_prev
    return pdi, mdi, adx


def signals(bars: Bars, n: int = 14, adx_min: float = 25.0, hold_bars: int = 20) -> list[Signal]:
    n = int(n)
    pos = segment_pos(bars.t)
    pdi, mdi, adx = dmi(bars, pos, n)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        if not (math.isfinite(float(adx[i])) and math.isfinite(float(pdi[i - 1])) and math.isfinite(float(mdi[i - 1]))):
            continue
        if adx[i] <= adx_min:
            continue
        e = float(bars.c[i])
        if pdi[i] > mdi[i] and pdi[i - 1] <= mdi[i - 1]:
            st = float(bars.low[i])
            if 0 < st < e:
                out.append(Signal(bars.close_time(i), "long", e, st, None, hold_ms))
        elif mdi[i] > pdi[i] and mdi[i - 1] <= pdi[i - 1]:
            st = float(bars.h[i])
            if st > e:
                out.append(Signal(bars.close_time(i), "short", e, st, None, hold_ms))
    return out
