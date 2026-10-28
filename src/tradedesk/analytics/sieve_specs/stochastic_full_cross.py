"""Full Stochastic Oscillator (14, 3, 3) signal-line cross in the extreme zones (G. Lane; formulas, defaults and levels
from StockCharts ChartSchool). Fast %K = (close - lowest low 14) / (highest high 14 - lowest low 14) x 100 over the
last 14 bars including the current one; Full %K = SMA3 of fast %K; Full %D = SMA3 of Full %K. Overbought 80, oversold 20.

Long: %K crosses above %D on this close while %K is below 20 (oversold). Short: %K crosses below %D while %K is above
80. Daily bars. The source gives no formal entry rule ("a signal line cross and/or a move back above 20 ... earliest
entry"); the cross inside the zone is the harvester's reading, fixed before any run. The source's advice to trade only
with the bigger trend is not applied (no filter named). Defaults added: no stop in the source, so ``stop_atr`` = 2.0 x
ATR14 from the entry; no exit, so time exit after ``hold_bars`` = 20 bars."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "stochastic_full_cross"
SOURCE = "G. Lane, Stochastic Oscillator; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/stochastic-oscillator-fast-slow-and-full)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 14, "k_smooth": 3, "d_smooth": 3, "zone": 20.0, "stop_atr": 2.0, "hold_bars": 20}


def stoch(b: Bars, n: int, ks: int, ds: int, pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    m = len(b)
    fast = np.full(m, np.nan)
    for i in range(m):
        if pos[i] >= n - 1:
            hh = float(b.h[i - n + 1 : i + 1].max())
            ll = float(b.low[i - n + 1 : i + 1].min())
            if hh > ll:
                fast[i] = (float(b.c[i]) - ll) / (hh - ll) * 100.0
    k = np.full(m, np.nan)
    d = np.full(m, np.nan)
    for i in range(m):
        if pos[i] >= n - 1 + ks - 1:
            k[i] = float(fast[i - ks + 1 : i + 1].mean())
        if pos[i] >= n - 1 + ks - 1 + ds - 1:
            d[i] = float(k[i - ds + 1 : i + 1].mean())
    return k, d


def signals(
    bars: Bars, n: int = 14, k_smooth: int = 3, d_smooth: int = 3, zone: float = 20.0, stop_atr: float = 2.0, hold_bars: int = 20
) -> list[Signal]:
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()  # mirrored %K is 100 - %K
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        k, d = stoch(b, int(n), int(k_smooth), int(d_smooth), pos)
        for i in range(1, len(b)):
            if not all(math.isfinite(float(x)) for x in (k[i], d[i], k[i - 1], d[i - 1])):
                continue
            ai = float(a[i])
            if k[i] > d[i] and k[i - 1] <= d[i - 1] and k[i] < zone and math.isfinite(ai) and ai > 0:
                if side == "long" and float(b.c[i]) - stop_atr * ai <= 0:
                    continue
                e = float(b.c[i])
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * (e - stop_atr * ai), None, hold_ms))
    return out
