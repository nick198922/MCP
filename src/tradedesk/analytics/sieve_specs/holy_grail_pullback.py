"""Raschke's "Holy Grail" trend pullback (L. B. Raschke, L. Connors, "Street Smarts", 1995; rules per
tradingsetupsreview.com): a trend is on when ADX14 is above 30 and rising; in an uptrend (+DI14 > -DI14) price retraces
to the 20-period EMA and the trader buys above the high of the bar that touches it; shorts mirror this (-DI14 > +DI14,
the bar touches the EMA from below, sell below its low). Daily bars.

Adaptations and defaults added by the harvester (fixed before any run, no variants):
- The source enters with a stop order beyond the touching bar; the sieve enters at a bar close. The signal is the touch
  bar itself, entry at its close, and the bar must close back on the trend side of the EMA (long: low <= EMA20 <
  close; short: high >= EMA20 > close), which is the rejection the buy-stop trigger would approximate. It is the
  first touch: the previous bar did not touch the EMA (its low was above the previous EMA for a long, high below for a
  short).
- Direction comes from +DI vs -DI (the source says the trend direction is up / down without naming the test).
- Stop: the far side of the touching bar (long: its low, short: its high), as in the sources that give one.
- The source gives no exit (ADX not falling below 30 is mentioned): exit by time after ``hold_bars`` = 20 bars.
- ADX / DI as in Wilder (see ``adx_di_cross``), EMA20 seeded by the SMA."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "holy_grail_pullback"
SOURCE = "L. B. Raschke, L. Connors, Street Smarts (1995); rules per https://tradingsetupsreview.com/the-holy-grail-trading-setup"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 14, "adx_min": 30.0, "ema_n": 20, "hold_bars": 20}


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


def signals(bars: Bars, n: int = 14, adx_min: float = 30.0, ema_n: int = 20, hold_bars: int = 20) -> list[Signal]:
    n, ema_n = int(n), int(ema_n)
    pos = segment_pos(bars.t)
    pdi, mdi, adx = dmi(bars, pos, n)
    e20 = ema(bars.c, ema_n, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        if not (math.isfinite(float(adx[i])) and math.isfinite(float(adx[i - 1])) and math.isfinite(float(e20[i - 1]))):
            continue
        if not (adx[i] > adx_min and adx[i] > adx[i - 1]):
            continue
        e = float(bars.c[i])
        ev = float(e20[i])
        if pdi[i] > mdi[i]:
            if bars.low[i] <= ev < e and bars.low[i - 1] > e20[i - 1] and bars.low[i] > 0:
                out.append(Signal(bars.close_time(i), "long", e, float(bars.low[i]), None, hold_ms))
        elif mdi[i] > pdi[i]:
            if bars.h[i] >= ev > e and bars.h[i - 1] < e20[i - 1]:
                out.append(Signal(bars.close_time(i), "short", e, float(bars.h[i]), None, hold_ms))
    return out
