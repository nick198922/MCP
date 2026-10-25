"""SMA 50/200 cross, both sides (the "golden cross" / "death cross" of chart-pattern folklore): long when the 50-day SMA
crosses above the 200-day SMA, short when it crosses below; the classic rule holds until the opposite cross, no stop.

The sieve exits only by a stop, a fixed target or time, so "until the opposite cross" is approximated by a time exit
after ``hold_bars`` = 50 days (the fast window), and the stop is the setup-hunt stage-5 disaster stop
2 * sigma_d * sqrt(hold_bars) (sigma_d: sample std of the last ``vol_n`` daily log returns), which also sets the R unit.
Parameters registered in IDEA-20261017-setup-hunt before the run; no variants."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "sma_cross_50_200"
SOURCE = "Golden / death cross: SMA50 crossing SMA200 on daily bars (classic technical-analysis rule, both sides)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"fast": 50, "slow": 200, "hold_bars": 50, "vol_n": 60, "stop_sigma": 2.0}


def sma(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Trailing simple mean of ``n`` values inside a segment; NaN before."""
    out = np.full(x.size, np.nan)
    for i in range(x.size):
        if pos[i] >= n - 1:
            out[i] = float(np.mean(x[i - n + 1 : i + 1]))
    return out


def sigma_d(c: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Sample std (ddof 1) of the last ``n`` log close-to-close returns inside a segment; NaN before."""
    out = np.full(c.size, np.nan)
    lr = np.full(c.size, np.nan)
    lr[1:] = np.log(c[1:] / c[:-1])
    for i in range(c.size):
        if pos[i] >= n:
            out[i] = float(np.std(lr[i - n + 1 : i + 1], ddof=1))
    return out


def signals(bars: Bars, fast: int = 50, slow: int = 200, hold_bars: int = 50, vol_n: int = 60, stop_sigma: float = 2.0) -> list[Signal]:
    pos = segment_pos(bars.t)
    c = bars.c
    f, s = sma(c, int(fast), pos), sma(c, int(slow), pos)
    sd = sigma_d(c, int(vol_n), pos)
    width = float(stop_sigma) * math.sqrt(int(hold_bars))
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, c.size):
        if pos[i] < 1:
            continue
        a0, b0, a1, b1, si = float(f[i - 1]), float(s[i - 1]), float(f[i]), float(s[i]), float(sd[i])
        if not all(math.isfinite(v) for v in (a0, b0, a1, b1, si)) or si <= 0:
            continue
        e = float(c[i])
        d = width * si * e
        if d >= e:
            continue  # a 2-sigma move over the hold beyond 100 %: no meaningful long stop, skipped on both sides
        if a1 > b1 and a0 <= b0:
            out.append(Signal(bars.close_time(i), "long", e, e - d, None, hold_ms))
        elif a1 < b1 and a0 >= b0:
            out.append(Signal(bars.close_time(i), "short", e, e + d, None, hold_ms))
    return out
