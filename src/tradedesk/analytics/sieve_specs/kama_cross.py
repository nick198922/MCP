"""Kaufman Adaptive Moving Average cross (P. Kaufman; rules from the StockCharts ChartSchool KAMA page): KAMA(10, 2, 30).
ER = |close - close[10]| / sum of |close - prior close| over the last 10 bars, SC = (ER * (2/3 - 2/31) + 2/31)^2,
KAMA = prior KAMA + SC * (close - prior KAMA); the first KAMA is the simple mean of the first 10 closes. The page: "a buy
signal materializes when price moves above KAMA", a sell signal when it moves below. Long on the first close above KAMA
after a close at or below it, short on the mirror.

Defaults added by the harvester (the page gives no stop or exit): stop ``stop_atr`` = 2.0 ATR14, time exit after
``hold_bars`` = 20 bars. Not applied: the page's scan trend filter (KAMA(10,5,30) against its SMA50), its liquidity
filters and its optional whipsaw filters (hold for N days, percentage band): no values are given for them. The first
``warmup`` = 50 bars of a segment are skipped so the seed does not matter. Daily bars."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "kama_cross"
SOURCE = "StockCharts ChartSchool, Kaufman's Adaptive Moving Average (KAMA)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"er_n": 10, "fast": 2, "slow": 30, "stop_atr": 2.0, "hold_bars": 20, "warmup": 50}


def kama(c: np.ndarray, pos: np.ndarray, er_n: int, fast: int, slow: int) -> np.ndarray:
    """KAMA seeded at segment position ``er_n - 1`` with the mean of the first ``er_n`` closes; NaN before."""
    out = np.full(c.size, np.nan)
    fsc, ssc = 2.0 / (fast + 1), 2.0 / (slow + 1)
    for i in range(c.size):
        p = int(pos[i])
        if p < er_n - 1:
            continue
        if p == er_n - 1:
            out[i] = float(np.mean(c[i - er_n + 1 : i + 1]))
            continue
        change = abs(float(c[i] - c[i - er_n]))
        vol = float(np.sum(np.abs(np.diff(c[i - er_n : i + 1]))))
        er = change / vol if vol > 0 else 0.0
        sc = (er * (fsc - ssc) + ssc) ** 2
        out[i] = out[i - 1] + sc * (float(c[i]) - out[i - 1])
    return out


def signals(
    bars: Bars, er_n: int = 10, fast: int = 2, slow: int = 30, stop_atr: float = 2.0, hold_bars: int = 20, warmup: int = 50
) -> list[Signal]:
    pos = segment_pos(bars.t)
    k = kama(bars.c, pos, int(er_n), int(fast), int(slow))
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        if pos[i] < int(warmup) or not (np.isfinite(k[i]) and np.isfinite(k[i - 1])):
            continue
        ai = float(a[i])
        if not (np.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        if bars.c[i - 1] <= k[i - 1] and e > k[i]:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif bars.c[i - 1] >= k[i - 1] and e < k[i]:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
