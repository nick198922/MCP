"""Supertrend flip (O. Seban; formula as in the TA-Lib reference and the common TradingView default, ATR period 10,
multiplier 3): bands hl2 -+ 3 x ATR10 (Wilder), the upper band only ratchets down and the lower band only up while the
close stays on the same side; the trend turns up when the close exceeds the previous final upper band and down when it
falls under the previous final lower band. A flip to up goes long, a flip to down goes short, at the close of the flip
bar. Daily bars.

Defaults added by the harvester: the source exits on the opposite flip, which the sieve cannot express, so the exit is
a time exit after ``hold_bars`` = 20 daily bars; the stop is the Supertrend line of the signal bar (the final opposite
band), which is the indicator's own stop level. The first bars of a segment set the initial band and trend (up) without
a signal. Source parameters, no variants."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "supertrend_flip"
SOURCE = "O. Seban's Supertrend, ATR 10 x 3 (https://ta-lib.org/functions/supertrend.html)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"atr_n": 10, "mult": 3.0, "hold_bars": 20}


def signals(bars: Bars, atr_n: int = 10, mult: float = 3.0, hold_bars: int = 20) -> list[Signal]:
    pos = segment_pos(bars.t)
    a = atr(bars, int(atr_n), pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    fu = fl = math.nan
    trend = 1
    started = False
    for i in range(len(bars)):
        ai = float(a[i])
        if pos[i] == 0 or not math.isfinite(ai):
            started = False
            continue
        hl2 = (float(bars.h[i]) + float(bars.low[i])) / 2.0
        bu, bl = hl2 + mult * ai, hl2 - mult * ai
        c = float(bars.c[i])
        if not started:
            fu, fl, trend, started = bu, bl, 1, True
            continue
        pc = float(bars.c[i - 1])
        fu = bu if (bu < fu or pc > fu) else fu
        fl = bl if (bl > fl or pc < fl) else fl
        prev = trend
        if trend == -1 and c > fu:
            trend = 1
        elif trend == 1 and c < fl:
            trend = -1
        if trend != prev:
            # the line after the flip is the band on the opposite side of the new trend
            if trend == 1 and fl < c:
                out.append(Signal(bars.close_time(i), "long", c, fl, None, hold_ms))
            elif trend == -1 and fu > c:
                out.append(Signal(bars.close_time(i), "short", c, fu, None, hold_ms))
    return out
