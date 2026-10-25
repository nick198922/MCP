"""Bollinger Band mean reversion (J. Bollinger, "Bollinger on Bollinger Bands", 2001: 20-period SMA, bands at 2
population standard deviations): long on a close below the lower band, short on a close above the upper band, exit at
the middle band; the source gives no stop.

The sieve exits only by a stop, a fixed target or time, so the middle-band exit is a target at the SMA20 of the signal
bar plus a time exit after ``hold_bars`` days, and the stop is the setup-hunt stage-5 disaster stop
2 * sigma_d * sqrt(hold_bars) (sigma_d: sample std of the last ``vol_n`` daily log returns), which also sets the R unit.
Parameters registered in IDEA-20261017-setup-hunt before the run; no variants."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "bollinger_reversion"
SOURCE = "J. Bollinger, Bollinger on Bollinger Bands (2001): SMA20 +- 2 population sd, revert to the middle band"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 20, "k": 2.0, "hold_bars": 20, "vol_n": 60, "stop_sigma": 2.0}


def bands(c: np.ndarray, n: int, k: float, pos: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(middle, lower, upper): trailing SMA of ``n`` closes and +- ``k`` population sd (ddof 0); NaN before."""
    mid = np.full(c.size, np.nan)
    sd = np.full(c.size, np.nan)
    for i in range(c.size):
        if pos[i] >= n - 1:
            w = c[i - n + 1 : i + 1]
            mid[i] = float(np.mean(w))
            sd[i] = float(np.std(w))
    return mid, mid - k * sd, mid + k * sd


def sigma_d(c: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Sample std (ddof 1) of the last ``n`` log close-to-close returns inside a segment; NaN before."""
    out = np.full(c.size, np.nan)
    lr = np.full(c.size, np.nan)
    lr[1:] = np.log(c[1:] / c[:-1])
    for i in range(c.size):
        if pos[i] >= n:
            out[i] = float(np.std(lr[i - n + 1 : i + 1], ddof=1))
    return out


def signals(bars: Bars, n: int = 20, k: float = 2.0, hold_bars: int = 20, vol_n: int = 60, stop_sigma: float = 2.0) -> list[Signal]:
    pos = segment_pos(bars.t)
    c = bars.c
    mid, lower, upper = bands(c, int(n), float(k), pos)
    sd = sigma_d(c, int(vol_n), pos)
    width = float(stop_sigma) * math.sqrt(int(hold_bars))
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(c.size):
        e = float(c[i])
        m, lo, up, si = float(mid[i]), float(lower[i]), float(upper[i]), float(sd[i])
        if not (math.isfinite(m) and math.isfinite(si) and si > 0 and up > lo):
            continue
        d = width * si * e
        if d >= e:
            continue  # a 2-sigma move over the hold beyond 100 %: no meaningful long stop, skipped on both sides
        if e < lo:
            out.append(Signal(bars.close_time(i), "long", e, e - d, m, hold_ms))
        elif e > up:
            out.append(Signal(bars.close_time(i), "short", e, e + d, m, hold_ms))
    return out
