"""RSI + TEMA vs Bollinger middle (Freqtrade ``futures/FSampleStrategy.py``, 1h, ``can_short = True``): long when RSI
crosses above 30, TEMA(9) is at or below the Bollinger middle band and TEMA is rising; short when RSI crosses above 70,
TEMA(9) is above the Bollinger middle band and TEMA is falling (the source's short entry uses "RSI crosses above 70",
not below, and it is taken as written). Stop 5 % (``stoploss = -0.05``), no target (``minimal_roi = {"0": 1}``), exit on
the opposite signal. The volume > 0 guards of the source only reject empty candles and are not modelled.

Source parameters, used unchanged: RSI thresholds 30 / 70, TEMA period 9, Bollinger middle = SMA20 of the typical price
(h + l + c) / 3 (qtpylib), stop 5 %, 1h bars. RSI is TA-Lib's default (period 14, Wilder); the source passes no period,
so 14 is the library default. "Crosses above" is: previous value at or below the level, now above. TEMA = 3 EMA1 - 3 EMA2
+ EMA3, each EMA seeded with the simple mean of its first ``n`` values (TA-Lib). "Rising / falling" is TEMA against its
previous bar.

Sieve adaptation: the source exits on the opposite signal, which needs future bars.

Default added by the harvester (the source has none): time exit after ``hold_bars`` = 20 bars (20 hours). No variants."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "freqtrade_rsi_tema_bb"
SOURCE = "Freqtrade strategies repo, futures/FSampleStrategy.py (https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/futures/FSampleStrategy.py)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"rsi_n": 14, "long_rsi": 30.0, "short_rsi": 70.0, "tema_n": 9, "bb_n": 20, "stop_pct": 0.05, "hold_bars": 20}


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
        g, ls = max(d, 0.0), max(-d, 0.0)
        if p <= n:
            ag += g
            al += ls
            if p < n:
                continue
            ag /= n
            al /= n
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + ls) / n
        out[i] = 50.0 if ag == 0.0 and al == 0.0 else (100.0 if al == 0.0 else 100.0 - 100.0 / (1.0 + ag / al))
    return out


def ema_from(x: np.ndarray, n: int, pos: np.ndarray, skip: int) -> np.ndarray:
    """EMA of ``x`` whose first valid value is at segment position ``skip``; seeded with the mean of the first ``n``."""
    out = np.full(x.size, np.nan)
    k = 2.0 / (n + 1)
    prev = np.nan
    acc = 0.0
    for i in range(x.size):
        q = int(pos[i]) - skip
        if q < 0:
            continue
        if q == 0:
            acc = 0.0
            prev = np.nan
        if q < n:
            acc += float(x[i])
            if q == n - 1:
                prev = acc / n
                out[i] = prev
            continue
        prev = float(x[i]) * k + prev * (1.0 - k)
        out[i] = prev
    return out


def tema(c: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    e1 = ema_from(c, n, pos, 0)
    e2 = ema_from(e1, n, pos, n - 1)
    e3 = ema_from(e2, n, pos, 2 * (n - 1))
    return 3.0 * e1 - 3.0 * e2 + e3


def signals(
    bars: Bars,
    rsi_n: int = 14,
    long_rsi: float = 30.0,
    short_rsi: float = 70.0,
    tema_n: int = 9,
    bb_n: int = 20,
    stop_pct: float = 0.05,
    hold_bars: int = 20,
) -> list[Signal]:
    pos = segment_pos(bars.t)
    rs = rsi_wilder(bars.c, int(rsi_n), pos)
    te = tema(bars.c, int(tema_n), pos)
    tp = (bars.h + bars.low + bars.c) / 3.0
    nb = int(bb_n)
    mid = np.full(len(bars), np.nan)
    for i in range(len(bars)):
        if pos[i] >= nb - 1:
            mid[i] = float(np.mean(tp[i - nb + 1 : i + 1]))
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        if not (np.isfinite(rs[i]) and np.isfinite(rs[i - 1]) and np.isfinite(te[i]) and np.isfinite(te[i - 1]) and np.isfinite(mid[i])):
            continue
        e = float(bars.c[i])
        if rs[i - 1] <= long_rsi < rs[i] and te[i] <= mid[i] and te[i] > te[i - 1]:
            out.append(Signal(bars.close_time(i), "long", e, e * (1.0 - stop_pct), None, hold_ms))
        if rs[i - 1] <= short_rsi < rs[i] and te[i] > mid[i] and te[i] < te[i - 1]:
            out.append(Signal(bars.close_time(i), "short", e, e * (1.0 + stop_pct), None, hold_ms))
    return out
