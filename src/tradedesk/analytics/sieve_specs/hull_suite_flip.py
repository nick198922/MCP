"""Hull Suite colour flip (InSilico's TradingView "Hull Suite", HMA mode, length 55 = the code's own "swing" setting):
HULL = WMA(2 x WMA(close, 27) - WMA(close, 55), 7) (Hull moving average; half = floor(55 / 2), root = floor(sqrt(55))).
The band is green when HULL > HULL two bars back, red otherwise. A turn to green is a long, a turn to red a short, at
the close of the turning bar. 4h and 1d bars.

Defaults added by the harvester (fixed before any run, no variants): the indicator gives no trading rule, no stop and no
exit. Using the colour turn as the entry follows third-party guides (long bias on green, short bias on red); the exit
is by time after ``hold_bars`` = 20 bars (the opposite turn needs future bars) and the stop is ``stop_atr`` = 2.0 x
ATR14. The source's THMA / EHMA modes are not used. The Hull value needs 60 bars of a segment before the first state."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "hull_suite_flip"
SOURCE = "InSilico, Hull Suite (TradingView hg92pFwS; Pine copy https://www.prorealcode.com/topic/hull-suite-gael), length 55"
TIMEFRAMES = (4, 24)
MANAGEMENT = "strategy"
PARAMS = {"length": 55, "stop_atr": 2.0, "hold_bars": 20}


def wma(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Linearly weighted MA (newest weight n); NaN until a full window of finite values inside the segment."""
    out = np.full(x.size, np.nan)
    if x.size < n:
        return out
    kern = np.arange(n, 0, -1, dtype=float)  # lag 0 gets weight n
    conv = np.convolve(x, kern)[: x.size] / (n * (n + 1) / 2.0)
    ok = pos >= n - 1
    out[ok] = conv[ok]
    return out


def signals(bars: Bars, length: int = 55, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n = int(length)
    half = n // 2
    root = int(math.floor(math.sqrt(n)))
    m = len(bars)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    raw = 2.0 * wma(bars.c, half, pos) - wma(bars.c, n, pos)
    # restart the second WMA's window inside the segment: raw is NaN before pos n-1, NaN propagates through the sum
    raw_pos = np.where(np.isfinite(raw), pos - (n - 1), 0)
    hull = wma(np.where(np.isfinite(raw), raw, np.nan), root, raw_pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(3, m):
        h0, h2, h1, h3 = float(hull[i]), float(hull[i - 2]), float(hull[i - 1]), float(hull[i - 3])
        if not (math.isfinite(h0) and math.isfinite(h2) and math.isfinite(h1) and math.isfinite(h3)):
            continue
        up, up_prev = h0 > h2, h1 > h3
        if up == up_prev:
            continue
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        c = float(bars.c[i])
        if up:
            if c - stop_atr * ai > 0:
                out.append(Signal(bars.close_time(i), "long", c, c - stop_atr * ai, None, hold_ms))
        else:
            out.append(Signal(bars.close_time(i), "short", c, c + stop_atr * ai, None, hold_ms))
    return out
