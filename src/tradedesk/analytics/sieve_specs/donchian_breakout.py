"""Donchian channel breakout (the classic "turtle" entry): the first close above the highest high of the previous ``n``
bars goes long (below the lowest low: short), stop ``stop_atr`` ATR14 from the entry, no target, time exit after
``hold_bars`` bars. An example spec without a target; the parameters are the textbook ones, not fitted."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "donchian_breakout"
SOURCE = "R. Donchian / Turtle rules, textbook 20-bar channel breakout"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 20, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, n: int = 20, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n = int(n)
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        above_prev = False
        for i in range(len(b)):
            if pos[i] < n:
                above_prev = False
                continue
            hh = float(b.h[i - n : i].max())  # previous n bars, not bar i
            above = bool(b.c[i] > hh)
            ai = float(a[i])
            if above and not above_prev and np.isfinite(ai) and ai > 0:
                e = float(b.c[i])
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * (e - stop_atr * ai), None, int(hold_bars) * b.bar_ms))
            above_prev = above
    return out
