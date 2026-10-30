"""SSL Channel flip (the TradingView / ProRealCode "SSL Channel" indicator): smaHigh = SMA20 of highs, smaLow = SMA20 of
lows (current bar included). State Hlv = +1 when close > smaHigh, -1 when close < smaLow, otherwise the previous state.
The indicator's arrows fire when its two lines cross, which happens exactly when the state flips from -1 to +1 (long)
or from +1 to -1 (short). Entry at the close of the flip bar. 4h and 1d bars. Period 20 is the page's own.

Defaults added by the harvester (fixed before any run, no variants): the source defines no entry beyond the arrows, no
stop and no exit (the author "would use another filter"), so the stop is ``stop_atr`` = 2.0 x ATR14 and the exit is
by time after ``hold_bars`` = 20 bars. The first state of a segment (from 0) is not a flip, so it gives no signal. The
page's ``type = 0`` average is read as a simple moving average (the page does not define it)."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "ssl_channel_flip"
SOURCE = "SSL Channel indicator, ProRealCode (https://www.prorealcode.com/prorealtime-indicators/ssl-channel/), period 20"
TIMEFRAMES = (4, 24)
MANAGEMENT = "strategy"
PARAMS = {"period": 20, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, period: int = 20, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n = int(period)
    m = len(bars)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    state = 0
    for i in range(m):
        if pos[i] == 0:
            state = 0
        if pos[i] < n - 1:
            continue
        sh = float(bars.h[i - n + 1 : i + 1].mean())
        sl = float(bars.low[i - n + 1 : i + 1].mean())
        c = float(bars.c[i])
        prev = state
        if c > sh:
            state = 1
        elif c < sl:
            state = -1
        if state == prev or prev == 0:
            continue
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        if state == 1:
            if c - stop_atr * ai > 0:
                out.append(Signal(bars.close_time(i), "long", c, c - stop_atr * ai, None, hold_ms))
        else:
            out.append(Signal(bars.close_time(i), "short", c, c + stop_atr * ai, None, hold_ms))
    return out
