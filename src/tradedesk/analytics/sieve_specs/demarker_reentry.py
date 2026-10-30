"""DeMarker (T. DeMark) re-entry from the extreme zones. DeMax = max(high - previous high, 0), DeMin = max(previous low -
low, 0), DeM = SMA14(DeMax) / (SMA14(DeMax) + SMA14(DeMin)). Long when DeM was below 0.3 and closes back above it;
short when DeM was above 0.7 and closes back below it (the rule of the broker guides: "wait for the line to cross back
inside the 0.3 / 0.7 boundaries" rather than trade the first touch). Period 14 and the 0.3 / 0.7 zones are the pages'
own. 4h and 1d bars.

Defaults added by the harvester (fixed before any run, no variants): the sources give no stop and no exit (they say
stops go behind the recent swing, which has no number), so the stop is ``stop_atr`` = 2.0 x ATR14 and the exit is by
time after ``hold_bars`` = 20 bars. The divergence and 0.5-cross readings of the pages are not used. Sources are broker
education pages without backtests; the guides warn that DeMarker pegs in strong trends."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "demarker_reentry"
SOURCE = "T. DeMark, DeMarker; Capital.com (https://capital.com/en-gb/learn/technical-analysis/demarker-indicator) and Forextester, period 14, zones 0.3 / 0.7"
TIMEFRAMES = (4, 24)
MANAGEMENT = "strategy"
PARAMS = {"period": 14, "lo": 0.3, "hi": 0.7, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, period: int = 14, lo: float = 0.3, hi: float = 0.7, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n = int(period)
    m = len(bars)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    demax = np.zeros(m)
    demin = np.zeros(m)
    if m > 1:
        demax[1:] = np.maximum(bars.h[1:] - bars.h[:-1], 0.0)
        demin[1:] = np.maximum(bars.low[:-1] - bars.low[1:], 0.0)
    dem = np.full(m, np.nan)
    for i in range(m):
        if pos[i] < n:  # n DeMax values need bars 1..n of the segment
            continue
        sx = float(demax[i - n + 1 : i + 1].sum())
        sn = float(demin[i - n + 1 : i + 1].sum())
        if sx + sn > 0:
            dem[i] = sx / (sx + sn)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, m):
        d, dp = float(dem[i]), float(dem[i - 1])
        if not (math.isfinite(d) and math.isfinite(dp)):
            continue
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        c = float(bars.c[i])
        if dp < lo <= d:
            if c - stop_atr * ai > 0:
                out.append(Signal(bars.close_time(i), "long", c, c - stop_atr * ai, None, hold_ms))
        elif dp > hi >= d:
            out.append(Signal(bars.close_time(i), "short", c, c + stop_atr * ai, None, hold_ms))
    return out
