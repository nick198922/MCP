"""Aroon trend start (T. Chande, 1995; rules per StockCharts ChartSchool Aroon): Aroon-Up = 100 * (25 - bars since the
25-bar high) / 25 and Aroon-Down likewise for the low (window = the last 26 bars incl. the current one, ties go to the
most recent bar). The source's emerging-uptrend stages: Up moves above Down, Up above 50 and Down below 50, Up reaches
100. Here the entry is the first close where Up > 50 and Down < 50 (stage 2, which implies stage 1); mirrored for a
short (Down > 50 and Up < 50). Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the source gives no stop and no exit rule, so the
stop is ``stop_atr`` = 2.0 x ATR14 from the entry and the exit is by time after ``hold_bars`` = 20 bars. The source says
the stages do not always occur in a fixed order and does not define one trigger; stage 2 was picked as the single
well-defined condition."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "aroon_trend_start"
SOURCE = "T. Chande (1995), Aroon; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/aroon)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 25, "level": 50.0, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, n: int = 25, level: float = 50.0, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n = int(n)
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        prev_on = False
        for i in range(len(b)):
            if pos[i] < n:
                prev_on = False
                continue
            hi = b.h[i - n : i + 1][::-1]
            lo = b.low[i - n : i + 1][::-1]
            up = 100.0 * (n - int(np.argmax(hi))) / n
            dn = 100.0 * (n - int(np.argmin(lo))) / n
            on = bool(up > level and dn < level)
            ai = float(a[i])
            if on and not prev_on and np.isfinite(ai) and ai > 0:
                e = float(b.c[i])
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * (e - stop_atr * ai), None, int(hold_bars) * b.bar_ms))
            prev_on = on
    return out
