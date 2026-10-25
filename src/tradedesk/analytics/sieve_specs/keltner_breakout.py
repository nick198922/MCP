"""Keltner channel breakout (C. Keltner, 1960; the modern EMA20 +- 2 ATR10 version popularised by L. Raschke): long on
the first daily close above EMA20 + 2 * ATR10, short on the first close below EMA20 - 2 * ATR10. The stop is the middle
line (EMA20 of the signal bar, the channel's own exit level), no target, time exit after ``hold_bars`` = 20 days (the
EMA window). Wilder ATR, EMA seeded with the simple mean (``course_s14_ema``). Parameters registered in
IDEA-20261017-setup-hunt before the run; no variants."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, atr, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "keltner_breakout"
SOURCE = "C. Keltner (1960) / L. Raschke: channel EMA20 +- 2 x ATR10, breakout of the band, stop at the middle line"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"ema_n": 20, "atr_n": 10, "k": 2.0, "hold_bars": 20}


def signals(bars: Bars, ema_n: int = 20, atr_n: int = 10, k: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    pos = segment_pos(bars.t)
    c = bars.c
    mid = ema(c, int(ema_n), pos)
    a = atr(bars, int(atr_n), pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    above_prev = below_prev = prev_ok = False  # prev_ok: the previous bar of the segment had a channel
    for i in range(c.size):
        m, ai = float(mid[i]), float(a[i])
        if pos[i] == 0 or not (math.isfinite(m) and math.isfinite(ai) and ai > 0):
            above_prev = below_prev = prev_ok = False
            continue
        e = float(c[i])
        above, below = e > m + k * ai, e < m - k * ai
        if prev_ok and above and not above_prev:
            out.append(Signal(bars.close_time(i), "long", e, m, None, hold_ms))
        elif prev_ok and below and not below_prev:
            out.append(Signal(bars.close_time(i), "short", e, m, None, hold_ms))
        above_prev, below_prev, prev_ok = above, below, True
    return out
