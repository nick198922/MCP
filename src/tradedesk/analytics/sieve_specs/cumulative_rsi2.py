"""Connors cumulative RSI(2) (L. Connors, C. Alvarez, "Short Term Trading Strategies That Work", 2008, chapter 9; rules
as stated in the FXCodeBase "Cumulative RSI signal" description of that chapter): trade only when the close is above its
200-day SMA; go long when the cumulative RSI(2) (the sum of the last 2 daily Wilder RSI(2) readings) crosses below 35.
The source exits when RSI(2) crosses above 65. Long only, daily bars.

"Crosses below" is taken literally: the sum was >= 35 on the previous bar and is < 35 on this one.

Defaults added by the harvester (fixed before any run, no variants): the exit RSI(2) > 65 needs future bars, so the exit
is by time after ``hold_bars`` = 20 bars (the book's holds are a few days; 20 is the harvester's fixed default for an
inexpressible exit), and the source has no stop, so ``stop_atr`` = 2.0 x ATR14 below the entry. Wilder's RSI seeded with
the simple mean of a segment's first 2 changes. Related to ``connors_rsi2`` (single RSI(2) < 5 with an SMA5 exit) but a
different entry and a different exit."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "cumulative_rsi2"
SOURCE = "L. Connors, C. Alvarez, Short Term Trading Strategies That Work (2008), ch. 9; rules per https://docs.gehtsoftusa.com/fxcodebase-backup/backup/custom-signals/c/cumulative-rsi-signal/"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"rsi_n": 2, "cum_n": 2, "below": 35.0, "trend_n": 200, "stop_atr": 2.0, "hold_bars": 20}


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


def sma(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Trailing simple mean of ``n`` values inside a segment; NaN before."""
    out = np.full(x.size, np.nan)
    for i in range(x.size):
        if pos[i] >= n - 1:
            out[i] = float(np.mean(x[i - n + 1 : i + 1]))
    return out


def signals(
    bars: Bars,
    rsi_n: int = 2,
    cum_n: int = 2,
    below: float = 35.0,
    trend_n: int = 200,
    stop_atr: float = 2.0,
    hold_bars: int = 20,
) -> list[Signal]:
    cum_n = int(cum_n)
    m = len(bars)
    pos = segment_pos(bars.t)
    r = rsi_wilder(bars.c, int(rsi_n), pos)
    cum = np.full(m, np.nan)
    for i in range(cum_n - 1, m):
        w = r[i - cum_n + 1 : i + 1]
        if np.all(np.isfinite(w)):
            cum[i] = float(w.sum())
    trend = sma(bars.c, int(trend_n), pos)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, m):
        ci, cp, ti, ai = float(cum[i]), float(cum[i - 1]), float(trend[i]), float(a[i])
        if not (math.isfinite(ci) and math.isfinite(cp) and math.isfinite(ti) and math.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        if e > ti and cp >= below and ci < below and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
    return out
