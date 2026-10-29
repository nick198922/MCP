"""Raschke's "Anti" setup as tested on The Right Trader page "Anti Setup (Raschke)": trade with the trend when the
stochastic stretches against it and then hooks back. Daily bars.

Indicators: slow %K (14, 3): fast %K = (close - lowest low 14) / (highest high 14 - lowest low 14) x 100 over the last
14 bars including the current one, smoothed by SMA3; EMA20 of the close (seeded with the mean of the first 20 closes);
ATR14 (Wilder). Levels 20 / 80.

Long: the trend filter is "close above a rising EMA20" (EMA20 today above EMA20 five bars ago). Stretch: slow %K <= 20
while the filter holds (it can last several bars; a bar with %K <= 20 but a false filter ends it). Trigger: the first
later close where slow %K crosses back above 20 (the previous bar was in the stretch and <= 20, this bar > 20) and the
filter still holds on that close. Entry at that close. Stop: the lowest low from the first stretch bar through the entry
bar (exactly on it; the page says "one tick below", no tick size). The trade is skipped when the entry-to-stop distance
exceeds 2 x ATR14. Target: 2R. Short: mirror (close below a falling EMA20, %K >= 80, crosses back below 80, stop at the
highest high of the stretch).

Defaults added by the harvester (fixed before any run, no variants): none beyond the page's own: it exits "at the close
of the eighth session after entry" (``hold_bars`` = 8) and uses a 2R target. The page is the site's "test version" of
Raschke's setup and says it has no published win rate; the original (Street Smarts) uses different mechanics.
"""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "anti_setup_raschke"
SOURCE = "L. B. Raschke, Anti setup; The Right Trader, Anti Setup (https://therighttrader.com/learn/trading-strategies/anti-setup)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 14, "k_smooth": 3, "zone": 20.0, "ema_n": 20, "slope_lag": 5, "atr_cap": 2.0, "rr": 2.0, "hold_bars": 8}


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


def signals(
    bars: Bars,
    n: int = 14,
    k_smooth: int = 3,
    zone: float = 20.0,
    ema_n: int = 20,
    slope_lag: int = 5,
    atr_cap: float = 2.0,
    rr: float = 2.0,
    hold_bars: int = 8,
) -> list[Signal]:
    lag = int(slope_lag)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()  # mirrored %K is 100 - %K, rising EMA <-> falling EMA
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        k = slow_k(b, int(n), int(k_smooth), pos)
        e20 = ema(b.c, int(ema_n), pos)
        active = False
        ll = math.inf
        for i in range(lag, len(b)):
            ki, ei, ep = float(k[i]), float(e20[i]), float(e20[i - lag])
            if not (math.isfinite(ki) and math.isfinite(ei) and math.isfinite(ep)):
                active = False
                continue
            trend = float(b.c[i]) > ei and ei > ep
            if active and ki > zone:
                active = False
                ll = min(ll, float(b.low[i]))
                ai, e = float(a[i]), float(b.c[i])
                tg = e + rr * (e - ll)
                if trend and math.isfinite(ai) and ai > 0 and ll < e and sgn * ll > 0 and sgn * tg > 0 and e - ll <= atr_cap * ai:
                    out.append(Signal(b.close_time(i), side, sgn * e, sgn * ll, sgn * tg, hold_ms))
            elif ki <= zone and trend:
                ll = float(b.low[i]) if not active else min(ll, float(b.low[i]))
                active = True
            else:
                active = False
    return out
