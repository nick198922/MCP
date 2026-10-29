"""Stochastic Pop (J. Bernstein; the exact test rules are those of The Right Trader page "Stochastic Pop"): a momentum
entry when the slow stochastic leaves its quiet zone, not a fade of the 80 / 20 bands. Daily bars.

Slow %K (14, 3, 3 as on the page): fast %K = (close - lowest low 14) / (highest high 14 - lowest low 14) x 100 over the
last 14 bars including the current one, slow %K = SMA3 of fast %K (the trigger is slow %K, not a %D cross).

Long ("bullish pop") on this close when all hold: the previous slow %K <= 80 and the current one > 80; the five bars
before the signal bar all had slow %K inside [20, 80]; ADX14 >= 20 and +DI14 > -DI14 (Wilder). Short: slow %K moves from
>= 20 to < 20, same five-bar quiet window, ADX14 >= 20 and -DI14 > +DI14. Entry at the signal bar's close. Stop: below the
signal bar's low (long) / above its high (short), exactly on the extreme (the page says "below / above", no tick size).
Target: 2R (twice the entry-to-stop distance). One signal per cross: the five-bar quiet window is the page's re-arming
rule ("a new pop requires slow %K back inside 20 to 80 for five bars"). Skip rule of the page: "if the last two pops both
closed back inside 20 to 80 within three bars, skip the next pop"; read as: the two latest events (either side) that met
every entry condition above, whether skipped or not, each had a slow %K back in [20, 80] on at least one of the next three
bars.

Defaults added by the harvester (fixed before any run, no variants): the page's early exit ("a later bar closes with slow
%K at or below 80", mirrored for shorts) needs future bars, so the exit is by time after ``hold_bars`` = 20 bars (the
stop and the 2R target still apply). ADX / DI are Wilder's, the first ADX is the mean of the first 14 DX values.
"""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "stochastic_pop"
SOURCE = (
    "J. Bernstein, Stochastic Pop; The Right Trader, Stochastic Pop (https://therighttrader.com/learn/indicator-strategies/stochastic-pop)"
)
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {
    "n": 14,
    "k_smooth": 3,
    "hi": 80.0,
    "lo": 20.0,
    "quiet": 5,
    "adx_n": 14,
    "adx_min": 20.0,
    "rr": 2.0,
    "skip_bars": 3,
    "hold_bars": 20,
}


def slow_k(b: Bars, n: int, ks: int, pos: np.ndarray) -> np.ndarray:
    m = len(b)
    fast = np.full(m, np.nan)
    for i in range(m):
        if pos[i] >= n - 1:
            hh = float(b.h[i - n + 1 : i + 1].max())
            ll = float(b.low[i - n + 1 : i + 1].min())
            if hh > ll:
                fast[i] = (float(b.c[i]) - ll) / (hh - ll) * 100.0
    k = np.full(m, np.nan)
    for i in range(m):
        if pos[i] >= n - 1 + ks - 1:
            k[i] = float(fast[i - ks + 1 : i + 1].mean())
    return k


def dmi(b: Bars, pos: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """+DI, -DI and ADX (Wilder), segment-aware; NaN until defined."""
    m = len(b)
    pdi = np.full(m, np.nan)
    mdi = np.full(m, np.nan)
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
        pdi[i] = 100.0 * s_p / s_tr
        mdi[i] = 100.0 * s_m / s_tr
        tot = pdi[i] + mdi[i]
        dx = 100.0 * abs(pdi[i] - mdi[i]) / tot if tot > 0 else 0.0
        if p < 2 * n - 1:
            dxs.append(dx)
        elif p == 2 * n - 1:
            dxs.append(dx)
            adx_prev = sum(dxs) / len(dxs)
            adx[i] = adx_prev
        elif math.isfinite(adx_prev):
            adx_prev = (adx_prev * (n - 1) + dx) / n
            adx[i] = adx_prev
    return pdi, mdi, adx


def signals(
    bars: Bars,
    n: int = 14,
    k_smooth: int = 3,
    hi: float = 80.0,
    lo: float = 20.0,
    quiet: int = 5,
    adx_n: int = 14,
    adx_min: float = 20.0,
    rr: float = 2.0,
    skip_bars: int = 3,
    hold_bars: int = 20,
) -> list[Signal]:
    quiet, skip_bars = int(quiet), int(skip_bars)
    pos = segment_pos(bars.t)
    k = slow_k(bars, int(n), int(k_smooth), pos)
    pdi, mdi, adx = dmi(bars, pos, int(adx_n))
    hold_ms = int(hold_bars) * bars.bar_ms
    events: list[tuple[int, int]] = []  # (bar index, +1 long / -1 short) of every pop that met the entry conditions
    out: list[Signal] = []
    for i in range(quiet + 1, len(bars)):
        win = k[i - quiet : i]
        cur, prev = float(k[i]), float(k[i - 1])
        if not (math.isfinite(cur) and bool(np.isfinite(win).all()) and math.isfinite(float(adx[i]))):
            continue
        if not bool(((win >= lo) & (win <= hi)).all()) or float(adx[i]) < adx_min:
            continue
        if prev <= hi < cur and pdi[i] > mdi[i]:
            side = 1
        elif prev >= lo > cur and mdi[i] > pdi[i]:
            side = -1
        else:
            continue
        skip = False
        if len(events) >= 2:
            skip = True
            for j, _side in events[-2:]:
                nxt = k[j + 1 : j + 1 + skip_bars]  # j + skip_bars < i: the quiet window keeps events >= 6 bars apart
                skip = skip and bool(((nxt >= lo) & (nxt <= hi)).any())
        events.append((i, side))
        if skip:
            continue
        e = float(bars.c[i])
        if side > 0:
            st = float(bars.low[i])
            if not 0 < st < e:
                continue
            out.append(Signal(bars.close_time(i), "long", e, st, e + rr * (e - st), hold_ms))
        else:
            st = float(bars.h[i])
            if not (st > e and e - rr * (st - e) > 0):
                continue
            out.append(Signal(bars.close_time(i), "short", e, st, e - rr * (st - e), hold_ms))
    return out
