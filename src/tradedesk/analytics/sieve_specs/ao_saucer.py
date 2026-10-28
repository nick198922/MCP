"""Awesome Oscillator saucer (B. Williams; formula and pattern per LuxAlgo, "Awesome Oscillator"): AO = SMA5(median price)
- SMA34(median price), median price = (high + low) / 2. Bullish saucer: the histogram is above zero, two consecutive falling
bars are followed by a rising bar -> long on that bar's close. Bearish saucer (the mirror, which the source describes only as
"bearish mirror"): the histogram is below zero, two consecutive rising bars followed by a falling bar -> short. Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the source gives no stop and no exit, so the stop is
``stop_atr`` = 2.0 x ATR14 from the entry and the exit is by time after ``hold_bars`` = 20 bars. "The histogram is above
zero" is read as all four bars of the pattern (i-3 .. i) being above zero (below for the short). The source's optional
filters (Alligator, fractal breakouts) are not applied; zero-line cross and twin peaks are not traded."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "ao_saucer"
SOURCE = "B. Williams, Awesome Oscillator; LuxAlgo (https://www.luxalgo.com/library/concept/awesome-oscillator.md)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"fast": 5, "slow": 34, "stop_atr": 2.0, "hold_bars": 20}


def sma(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    out = np.full(x.size, np.nan)
    for i in range(x.size):
        if pos[i] >= n - 1:
            out[i] = float(np.mean(x[i - n + 1 : i + 1]))
    return out


def signals(bars: Bars, fast: int = 5, slow: int = 34, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    m = len(bars)
    if m == 0:
        return []
    pos = segment_pos(bars.t)
    mp = (bars.h + bars.low) / 2.0
    ao = sma(mp, int(fast), pos) - sma(mp, int(slow), pos)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(3, m):
        w = ao[i - 3 : i + 1]
        ai = float(a[i])
        if not (np.all(np.isfinite(w)) and math.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        if np.all(w > 0) and w[1] < w[0] and w[2] < w[1] and w[3] > w[2] and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif np.all(w < 0) and w[1] > w[0] and w[2] > w[1] and w[3] < w[2]:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
