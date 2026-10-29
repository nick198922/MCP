"""HLHB (freqtrade-strategies ``hlhb.py``, a bot strategy from the Babypips "HLHB" forex system), 4h: long when, on the
same bar, RSI(10) crosses above 50, EMA5 crosses above EMA10, and ADX(14) > 25. The RSI is computed on
(open + close) / 2 (the file calls it ``hl2``), Wilder's smoothing; EMAs on the close. The file has long entries only and
its exit is the mirror (RSI crosses below 50, EMA5 below EMA10, ADX > 25); that mirror is used here as the short entry
(the original system is a stop-and-reverse). The ``volume > 0`` guard is dropped (the sieve has no volume).

Defaults added by the harvester (fixed before any run, no variants): the file's exit is the opposite signal (needs future
bars) and its stoploss (-32 %) and ROI table are hyperopt output on 4h, not part of the idea; so the stop is
``stop_atr`` = 2.0 x ATR14 and the exit is by time after ``hold_bars`` = 20 bars (about 3.3 days). All three conditions
on the same bar is the literal reading of the file; signals are rare. ADX is Wilder's (as in TA-Lib), same
calculation as the ``adx_di_cross`` spec."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "hlhb_rsi_ema_adx"
SOURCE = "freqtrade-strategies, hlhb.py (https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/hlhb.py)"
TIMEFRAMES = (4,)
MANAGEMENT = "strategy"
PARAMS = {"rsi_n": 10, "fast": 5, "slow": 10, "adx_n": 14, "adx_min": 25.0, "stop_atr": 2.0, "hold_bars": 20}


def rsi_wilder(c: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Wilder's RSI seeded with the simple mean of a segment's first ``n`` changes; NaN before."""
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


def adx_wilder(b: Bars, pos: np.ndarray, n: int) -> np.ndarray:
    """ADX (Wilder), segment-aware; NaN until defined."""
    m = len(b)
    adx = np.full(m, np.nan)
    s_tr = s_p = s_m = 0.0
    dxs: list[float] = []
    adx_prev = math.nan
    for i in range(m):
        p = int(pos[i])
        if p == 0:
            s_tr = s_p = s_m = 0.0
            dxs = []
            adx_prev = math.nan
            continue
        up = float(b.h[i] - b.h[i - 1])
        dn = float(b.low[i - 1] - b.low[i])
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        pc = float(b.c[i - 1])
        tr = max(float(b.h[i] - b.low[i]), abs(float(b.h[i]) - pc), abs(float(b.low[i]) - pc))
        if p <= n:
            s_tr += tr
            s_p += pdm
            s_m += mdm
            if p < n:
                continue
        else:
            s_tr = s_tr - s_tr / n + tr
            s_p = s_p - s_p / n + pdm
            s_m = s_m - s_m / n + mdm
        if s_tr <= 0:
            continue
        pdi = 100.0 * s_p / s_tr
        mdi = 100.0 * s_m / s_tr
        tot = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / tot if tot > 0 else 0.0
        if p < 2 * n - 1:
            dxs.append(dx)
        elif p == 2 * n - 1:
            dxs.append(dx)
            adx_prev = sum(dxs) / len(dxs)
            adx[i] = adx_prev
        elif math.isfinite(adx_prev):
            adx_prev = (adx_prev * (n - 1) + dx) / n
            adx[i] = adx_prev
    return adx


def signals(
    bars: Bars,
    rsi_n: int = 10,
    fast: int = 5,
    slow: int = 10,
    adx_n: int = 14,
    adx_min: float = 25.0,
    stop_atr: float = 2.0,
    hold_bars: int = 20,
) -> list[Signal]:
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    r = rsi_wilder((bars.o + bars.c) / 2.0, int(rsi_n), pos)
    ef = ema(bars.c, int(fast), pos)
    es = ema(bars.c, int(slow), pos)
    adx = adx_wilder(bars, pos, int(adx_n))
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        ai = float(a[i])
        vals = (float(r[i]), float(r[i - 1]), float(ef[i]), float(ef[i - 1]), float(es[i]), float(es[i - 1]), float(adx[i]))
        if not (math.isfinite(ai) and ai > 0 and all(math.isfinite(v) for v in vals)):
            continue
        if adx[i] <= adx_min:
            continue
        e = float(bars.c[i])
        if r[i] > 50 >= r[i - 1] and ef[i] > es[i] and ef[i - 1] <= es[i - 1]:
            if e - stop_atr * ai > 0:
                out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif r[i] < 50 <= r[i - 1] and ef[i] < es[i] and ef[i - 1] >= es[i - 1]:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
