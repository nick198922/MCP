"""Mayer Multiple extremes (T. Mayer): multiple = close / SMA200(close). Per Glassnode, readings above 2.4 have marked
overbought / euphoric phases and below 0.8 oversold / capitulation. The rule here: the first close with multiple > 2.4
(previous close <= 2.4) -> short; the first close with multiple < 0.8 (previous >= 0.8) -> long. Daily bars.

Defaults and readings added by the harvester (fixed before any run, no variants): the source states the thresholds
as zones and says price spends most of the time between them, but gives no trade rule, stop or exit. The fade
direction (short overbought, long oversold) and the first-crossing trigger are the harvester's reading; stop =
``stop_atr`` = 2.0 x ATR14, time exit ``hold_bars`` = 20 bars. The thresholds were derived on Bitcoin's history (the
source says use on other assets is approximate); the spec cannot see the symbol, so it runs on the whole universe."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "mayer_multiple_fade"
SOURCE = "T. Mayer, Mayer Multiple; thresholds per Glassnode (https://studio.glassnode.com/charts/mayer-multiple)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 200, "high": 2.4, "low": 0.8, "stop_atr": 2.0, "hold_bars": 20}


def mayer(bars: Bars, pos: np.ndarray, n: int) -> np.ndarray:
    """close / SMA(n) of closes including the current bar; NaN until a segment has n bars."""
    out = np.full(len(bars), np.nan)
    cs = np.concatenate([[0.0], np.cumsum(bars.c)])
    for i in range(len(bars)):
        if pos[i] >= n - 1:
            out[i] = bars.c[i] / ((cs[i + 1] - cs[i + 1 - n]) / n)
    return out


def signals(bars: Bars, n: int = 200, high: float = 2.4, low: float = 0.8, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n = int(n)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    mm = mayer(bars, pos, n)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        if not (math.isfinite(float(mm[i])) and math.isfinite(float(mm[i - 1]))):
            continue
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        if mm[i] > high and mm[i - 1] <= high:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
        elif mm[i] < low and mm[i - 1] >= low and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
    return out
