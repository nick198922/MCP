"""Pretty Good Oscillator (M. Johnson; LuxAlgo library "Pretty Good Oscillator", daily bars).

PGO = (close - SMA(close, n)) / EMA(true range, n), the distance of the close from its average in units of average
range. Johnson's long-term breakout system: long when PGO moves above +3.0, short when it moves below -3.0; exit when
PGO returns through zero. The author's own period is 89 bars (14 or 21 are platform conventions); 89 is used, not
tuned. Entry at the close of the first bar where PGO crosses a threshold (previous bar inside the band).

Defaults added by the harvester (fixed before any run, no variants): the page has no stop (stop = 2.0 x ATR14 from the
entry) and its exit (PGO back through zero) is a later event the sieve cannot express, so the position is closed after
20 bars (the harvester default time exit). The EMA of true range is the TradingView-style EMA seeded with the simple
mean (the page says "exponential" and notes some implementations use a simple average)."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "pretty_good_oscillator"
SOURCE = "M. Johnson, Pretty Good Oscillator, n=89, +-3.0 breakout (https://www.luxalgo.com/library/concept/pretty-good-oscillator/)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 89, "level": 3.0, "stop_atr": 2.0, "hold_bars": 20}


def pgo(bars: Bars, n: int, pos: np.ndarray) -> np.ndarray:
    tr = np.empty(len(bars))
    for i in range(len(bars)):
        r = bars.h[i] - bars.low[i]
        if pos[i] > 0:
            pc = bars.c[i - 1]
            r = max(r, abs(bars.h[i] - pc), abs(bars.low[i] - pc))
        tr[i] = r
    avg_tr = ema(tr, n, pos)
    out = np.full(len(bars), np.nan)
    for i in range(n - 1, len(bars)):
        if pos[i] >= n - 1 and avg_tr[i] > 0:
            out[i] = (bars.c[i] - float(bars.c[i - n + 1 : i + 1].mean())) / avg_tr[i]
    return out


def signals(bars: Bars, n: int = 89, level: float = 3.0, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    pos = segment_pos(bars.t)
    p = pgo(bars, int(n), pos)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        v, w, ai = float(p[i]), float(p[i - 1]), float(a[i])
        if not (np.isfinite(v) and np.isfinite(w) and np.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        if v > level >= w:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif v < -level <= w:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
