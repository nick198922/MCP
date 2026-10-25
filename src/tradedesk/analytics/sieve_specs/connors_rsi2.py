"""Connors RSI(2) with the 200-day trend filter (L. Connors, C. Alvarez, "Short Term Trading Strategies That Work",
2008, the 2-period RSI chapter): long when the close is above its 200-day SMA and Wilder's RSI(2) closes below 5,
short when below the SMA200 and RSI(2) above 95; the source exits on the first close beyond the 5-day SMA and uses no
stop.

The sieve exits only by a stop, a fixed target or time, so the SMA5 exit is approximated by a target at the SMA5 of the
signal bar plus a time exit after ``hold_bars`` days, and the stop is the setup-hunt stage-5 disaster stop
2 * sigma_d * sqrt(hold_bars) (sigma_d: sample std of the last ``vol_n`` daily log returns), which also sets the R unit.
Parameters registered in IDEA-20261017-setup-hunt before the run; no variants."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "connors_rsi2"
SOURCE = "L. Connors, C. Alvarez, Short Term Trading Strategies That Work (2008), 2-period RSI with the 200-day MA"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"rsi_n": 2, "trend_n": 200, "exit_n": 5, "lo": 5.0, "hi": 95.0, "hold_bars": 10, "vol_n": 60, "stop_sigma": 2.0}


def sma(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Trailing simple mean of ``n`` values inside a segment; NaN before."""
    out = np.full(x.size, np.nan)
    for i in range(x.size):
        if pos[i] >= n - 1:
            out[i] = float(np.mean(x[i - n + 1 : i + 1]))
    return out


def rsi_wilder(c: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Wilder's RSI: average gain and loss seeded with the simple mean of a segment's first ``n`` changes, then
    smoothed (avg = (avg * (n - 1) + x) / n). No loss -> 100 (no change at all -> 50). NaN before the seed."""
    out = np.full(c.size, np.nan)
    ag = al = 0.0
    for i in range(c.size):
        p = pos[i]
        if p == 0:
            ag = al = 0.0
            continue
        d = float(c[i] - c[i - 1])
        g, lo = max(d, 0.0), max(-d, 0.0)
        if p <= n:
            ag += g
            al += lo
            if p < n:
                continue
            ag /= n
            al /= n
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + lo) / n
        out[i] = 50.0 if ag == 0.0 and al == 0.0 else (100.0 if al == 0.0 else 100.0 - 100.0 / (1.0 + ag / al))
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


def signals(
    bars: Bars,
    rsi_n: int = 2,
    trend_n: int = 200,
    exit_n: int = 5,
    lo: float = 5.0,
    hi: float = 95.0,
    hold_bars: int = 10,
    vol_n: int = 60,
    stop_sigma: float = 2.0,
) -> list[Signal]:
    pos = segment_pos(bars.t)
    c = bars.c
    r = rsi_wilder(c, int(rsi_n), pos)
    trend = sma(c, int(trend_n), pos)
    ex = sma(c, int(exit_n), pos)
    sd = sigma_d(c, int(vol_n), pos)
    width = float(stop_sigma) * math.sqrt(int(hold_bars))
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(c.size):
        e = float(c[i])
        ri, tr, xi, si = float(r[i]), float(trend[i]), float(ex[i]), float(sd[i])
        if not (math.isfinite(ri) and math.isfinite(tr) and math.isfinite(xi) and math.isfinite(si) and si > 0):
            continue
        d = width * si * e
        if d >= e:
            continue  # a 2-sigma move over the hold beyond 100 %: no meaningful long stop, skipped on both sides
        if e > tr and ri < lo and e < xi:
            out.append(Signal(bars.close_time(i), "long", e, e - d, xi, hold_ms))
        elif e < tr and ri > hi and e > xi:
            out.append(Signal(bars.close_time(i), "short", e, e + d, xi, hold_ms))
    return out
