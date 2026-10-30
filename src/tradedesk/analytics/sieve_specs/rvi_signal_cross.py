"""Relative Vigor Index signal-line cross (J. Ehlers' RVI, LuxAlgo concept page): V = (C-O + 2(C-O)[1] + 2(C-O)[2] +
(C-O)[3]) / 6, D = (H-L + 2(H-L)[1] + 2(H-L)[2] + (H-L)[3]) / 6, RVI = sum(V, 10) / sum(D, 10), signal = (RVI + 2 RVI[1]
+ 2 RVI[2] + RVI[3]) / 6. RVI crossing above its signal line is a long, below a short, at the close of the cross bar.
Period 10 and the 1-2-2-1 weights are the page's own. 4h and 1d bars.

Defaults added by the harvester (fixed before any run, no variants): the page gives no numeric trading rule (it says to
favour crosses in the direction of the prevailing trend, without defining the trend, so no trend filter is used), no
stop and no exit, so the stop is ``stop_atr`` = 2.0 x ATR14 and the exit is by time after ``hold_bars`` = 20 bars. Bars
with zero D-sum are skipped (no value). The page warns the indicator carries weaker information on 24-hour markets and
whipsaws in quiet ranges."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "rvi_signal_cross"
SOURCE = "Relative Vigor Index (J. Ehlers), LuxAlgo (https://www.luxalgo.com/library/concept/relative-vigor-index.md), period 10"
TIMEFRAMES = (4, 24)
MANAGEMENT = "strategy"
PARAMS = {"period": 10, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, period: int = 10, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n = int(period)
    m = len(bars)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    co = bars.c - bars.o
    hl = bars.h - bars.low
    v = np.full(m, np.nan)
    d = np.full(m, np.nan)
    for i in range(3, m):
        if pos[i] >= 3:
            v[i] = (co[i] + 2 * co[i - 1] + 2 * co[i - 2] + co[i - 3]) / 6.0
            d[i] = (hl[i] + 2 * hl[i - 1] + 2 * hl[i - 2] + hl[i - 3]) / 6.0
    rvi = np.full(m, np.nan)
    for i in range(m):
        if pos[i] >= 3 + n - 1:
            sd = float(d[i - n + 1 : i + 1].sum())
            if sd > 0:
                rvi[i] = float(v[i - n + 1 : i + 1].sum()) / sd
    sig = np.full(m, np.nan)
    for i in range(3, m):
        sig[i] = (rvi[i] + 2 * rvi[i - 1] + 2 * rvi[i - 2] + rvi[i - 3]) / 6.0  # NaN while any input is NaN
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(4, m):
        r, s, rp, sp = float(rvi[i]), float(sig[i]), float(rvi[i - 1]), float(sig[i - 1])
        if not (math.isfinite(r) and math.isfinite(s) and math.isfinite(rp) and math.isfinite(sp)):
            continue
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        c = float(bars.c[i])
        if r > s and rp <= sp:
            if c - stop_atr * ai > 0:
                out.append(Signal(bars.close_time(i), "long", c, c - stop_atr * ai, None, hold_ms))
        elif r < s and rp >= sp:
            out.append(Signal(bars.close_time(i), "short", c, c + stop_atr * ai, None, hold_ms))
    return out
