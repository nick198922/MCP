"""Williams %R turn from an extreme (L. Williams; scans per StockCharts ChartSchool Williams %R): %R = (HH14 - close) /
(HH14 - LL14) x -100. The source's two scans: "turns up from oversold" = close above SMA200, %R below -80 twenty days
ago, and %R crossing above -50 now; "turns down from overbought" = close below SMA200, %R above -20 twenty days ago,
and %R crossing below -50 now. Long and short at the close. Daily bars; the literal "twenty days ago" is the single
bar 20 bars back (not "any time in the last 20").

Defaults added by the harvester (fixed before any run, no variants): the source gives no stop, target or exit, so the
stop is ``stop_atr`` = 2.0 x ATR14 from the entry and the exit is by time after ``hold_bars`` = 20 bars (the same
horizon as the lookback of the scan)."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "williams_r_scan"
SOURCE = "L. Williams, %R; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/williams-r)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 14, "sma": 200, "back": 20, "oversold": -80.0, "mid": -50.0, "stop_atr": 2.0, "hold_bars": 20}


def signals(
    bars: Bars,
    n: int = 14,
    sma: int = 200,
    back: int = 20,
    oversold: float = -80.0,
    mid: float = -50.0,
    stop_atr: float = 2.0,
    hold_bars: int = 20,
) -> list[Signal]:
    n, sma, back = int(n), int(sma), int(back)
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()  # mirrored %R' = -100 - %R, so the short scan becomes the long one
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        m = len(b)
        r = np.full(m, np.nan)
        for i in range(m):
            if pos[i] < n - 1:
                continue
            hh = float(b.h[i - n + 1 : i + 1].max())
            ll = float(b.low[i - n + 1 : i + 1].min())
            if hh > ll:
                r[i] = (hh - float(b.c[i])) / (hh - ll) * -100.0
        for i in range(m):
            if pos[i] < max(sma - 1, n - 1 + back):
                continue
            ai = float(a[i])
            if not (np.isfinite(ai) and ai > 0 and np.isfinite(r[i]) and np.isfinite(r[i - 1]) and np.isfinite(r[i - back])):
                continue
            ma = float(b.c[i - sma + 1 : i + 1].mean())
            if b.c[i] > ma and r[i - back] < oversold and r[i] > mid and r[i - 1] <= mid:
                e = float(b.c[i])
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * (e - stop_atr * ai), None, int(hold_bars) * b.bar_ms))
    return out
