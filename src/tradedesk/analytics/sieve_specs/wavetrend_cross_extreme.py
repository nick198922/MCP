"""WaveTrend Oscillator (LazyBear's TradingView port of a TradeStation / MetaTrader indicator) with the extreme-zone
cross rule: ap = (high + low + close) / 3, esa = EMA10(ap), d = EMA10(|ap - esa|), ci = (ap - esa) / (0.015 * d),
wt1 = EMA21(ci), wt2 = SMA4(wt1). Long when wt1 crosses above wt2 while both lines are below -60 (oversold); short when
wt1 crosses below wt2 while both are above +60 (overbought). Daily bars, entry at the close of the cross bar.

Parameters are the widely ported ones (channel 10, average 21, constant 0.015, signal SMA4, level 60 as the first
overbought / oversold band; the second band 53 that some pages add is not used). Sources disagree whether the cross
needs only wt1 or both lines in the zone; the LuxAlgo wording "both lines" is taken. Mid-range crosses are not traded
(the pages say to skip or downgrade them, with no number).

Defaults added by the harvester (fixed before any run, no variants): the pages give no stop, target or exit, so
``stop_atr`` = 2.0 ATR14 and ``hold_bars`` = 20; timeframe 1d (the indicator is used on all of them). The EMAs are seeded
with the simple mean of the first n valid values of a data segment (TradingView style)."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "wavetrend_cross_extreme"
SOURCE = "LazyBear WaveTrend Oscillator; formula and rules via https://www.prorealcode.com/prorealtime-indicators/wave-trend-oscillator/ and https://www.luxalgo.com/library/concept/wave-trend-oscillator.md"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n1": 10, "n2": 21, "sig": 4, "level": 60.0, "stop_atr": 2.0, "hold_bars": 20}


def _ema_valid(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """EMA over a series that may start with NaN inside a segment: seeded with the simple mean of the first ``n``
    consecutive valid values, NaN before; a new segment (pos == 0) restarts."""
    out = np.full(x.size, np.nan)
    k = 2.0 / (n + 1)
    prev = np.nan
    cnt = 0
    acc = 0.0
    for i in range(x.size):
        if pos[i] == 0:
            prev, cnt, acc = np.nan, 0, 0.0
        v = float(x[i])
        if not np.isfinite(v):
            continue
        if np.isfinite(prev):
            prev = v * k + prev * (1 - k)
            out[i] = prev
            continue
        acc += v
        cnt += 1
        if cnt == n:
            prev = acc / n
            out[i] = prev
    return out


def signals(
    bars: Bars, n1: int = 10, n2: int = 21, sig: int = 4, level: float = 60.0, stop_atr: float = 2.0, hold_bars: int = 20
) -> list[Signal]:
    sig = int(sig)
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()  # mirrored hlc3 = -hlc3: overbought becomes oversold
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        ap = (b.h + b.low + b.c) / 3.0
        esa = _ema_valid(ap, int(n1), pos)
        d = _ema_valid(np.abs(ap - esa), int(n1), pos)
        with np.errstate(divide="ignore", invalid="ignore"):
            ci = (ap - esa) / (0.015 * d)
        ci[~np.isfinite(ci)] = np.nan
        wt1 = _ema_valid(ci, int(n2), pos)
        wt2 = np.full(len(b), np.nan)
        for i in range(sig - 1, len(b)):
            w = wt1[i - sig + 1 : i + 1]
            if np.all(np.isfinite(w)) and pos[i] >= sig - 1:
                wt2[i] = float(w.mean())
        for i in range(1, len(b)):
            if pos[i] == 0:
                continue
            w1, w2, p1, p2 = float(wt1[i]), float(wt2[i]), float(wt1[i - 1]), float(wt2[i - 1])
            if not (np.isfinite(w1) and np.isfinite(w2) and np.isfinite(p1) and np.isfinite(p2)):
                continue
            ai = float(a[i])
            if p1 <= p2 and w1 > w2 and w1 < -level and w2 < -level and np.isfinite(ai) and ai > 0:
                e = float(b.c[i])
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * (e - stop_atr * ai), None, int(hold_bars) * b.bar_ms))
    return out
