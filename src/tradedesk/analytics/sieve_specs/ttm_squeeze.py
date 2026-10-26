"""TTM Squeeze (John Carter): volatility compression followed by an expansion in the direction of momentum.

Source rules (StockCharts ChartSchool "TTM Squeeze"): the squeeze is ON while the Bollinger Bands (20, 2 sigma) lie
entirely inside the Keltner Channel (20 bars, 1.5 ATR); it is OFF otherwise. Momentum is a linear regression (length 20)
of delta = close - ((highest high 20 + lowest low 20) / 2 + SMA20) / 2. Carter buys on the first bar with the squeeze
off after one or more bars with it on (the "fire") when momentum is above zero and rising, and sells short when it is
below zero and falling. Daily bars, long and short.

Choices made here (the source leaves them open), fixed before any run, no variants:
- Keltner centre is the SMA20 of the close and the band is 1.5 x Wilder ATR20. This is the ATR-based (Raschke)
  variant, a deliberate deviation from the source, which says Carter uses the original 1960 Keltner formula; chosen
  before any run. Bollinger uses the population standard deviation (as TradingView).
- The linear regression value is the end point of the least-squares line through the last 20 deltas.
- The source exits when momentum turns back toward zero (needs future bars; not testable) and gives no stop. Exit by
  time after ``hold_bars`` = 10 bars (the article: post-squeeze moves last about 8-10 bars), stop ``stop_atr`` = 2.0 x
  ATR14 (harvester default)."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "ttm_squeeze"
SOURCE = "J. Carter, TTM Squeeze; rules per StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/ttm-squeeze)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 20, "bb_k": 2.0, "kc_k": 1.5, "stop_atr": 2.0, "hold_bars": 10}


def signals(bars: Bars, n: int = 20, bb_k: float = 2.0, kc_k: float = 1.5, stop_atr: float = 2.0, hold_bars: int = 10) -> list[Signal]:
    n = int(n)
    m = len(bars)
    pos = segment_pos(bars.t)
    a14 = atr(bars, 14, pos)
    an = atr(bars, n, pos)
    delta = np.full(m, np.nan)
    squeeze = np.zeros(m, dtype=bool)
    for i in range(m):
        if pos[i] < n - 1:
            continue
        c = bars.c[i - n + 1 : i + 1]
        sma = float(c.mean())
        sd = float(c.std())
        mid = (float(bars.h[i - n + 1 : i + 1].max()) + float(bars.low[i - n + 1 : i + 1].min())) / 2.0
        delta[i] = float(bars.c[i]) - (mid + sma) / 2.0
        ani = float(an[i])
        if math.isfinite(ani):
            squeeze[i] = (sma + bb_k * sd < sma + kc_k * ani) and (sma - bb_k * sd > sma - kc_k * ani)
    x = np.arange(n, dtype=float)
    xc = x - x.mean()
    sxx = float((xc * xc).sum())
    mom = np.full(m, np.nan)
    for i in range(m):
        if pos[i] < 2 * n - 2:
            continue
        y = delta[i - n + 1 : i + 1]
        mom[i] = float(y.mean()) + float((xc * (y - y.mean())).sum()) / sxx * xc[-1]
    out: list[Signal] = []
    for i in range(1, m):
        if not (squeeze[i - 1] and not squeeze[i]):
            continue
        if not (math.isfinite(float(mom[i])) and math.isfinite(float(mom[i - 1]))):
            continue
        ai = float(a14[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        hold_ms = int(hold_bars) * bars.bar_ms
        if mom[i] > 0 and mom[i] > mom[i - 1] and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif mom[i] < 0 and mom[i] < mom[i - 1]:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
