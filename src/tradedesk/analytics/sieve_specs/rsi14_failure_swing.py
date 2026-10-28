"""RSI(14) bullish / bearish failure swing (J. W. Wilder, "New Concepts in Technical Trading Systems", 1978; rule wording
from StockCharts ChartSchool, RSI). Wilder RSI with period 14 (first averages are simple means, then
``(prev * 13 + current) / 14``); overbought above 70, oversold below 30.

Bullish failure swing (long): RSI drops below 30, bounces above it, pulls back while staying above 30, then breaks the
prior (bounce) high. The signal is the first close where RSI exceeds the bounce high after a pullback. Bearish failure
swing (short) is the exact mirror (RSI of negated prices equals 100 - RSI). Daily bars.

Source wording is loose; the harvester's reading, fixed before any run: the bounce high is the running maximum of RSI
since it first closed above 30; a pullback is any close with RSI below that maximum; an RSI close at or below 30 before
the break voids the pattern and starts a new one; no expiry (the source names none). Defaults added: the source gives no
stop and no exit, so ``stop_atr`` = 2.0 x ATR14 from the entry and the exit is by time after ``hold_bars`` = 20 bars."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "rsi14_failure_swing"
SOURCE = "J. W. Wilder (1978), RSI; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/relative-strength-index-rsi)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 14, "lo": 30.0, "stop_atr": 2.0, "hold_bars": 20}


def rsi_wilder(c: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    out = np.full(c.size, np.nan)
    ag = al = 0.0
    for i in range(1, c.size):
        p = int(pos[i])
        if p == 0:
            ag = al = 0.0
            continue
        d = float(c[i] - c[i - 1])
        g, ls = max(d, 0.0), max(-d, 0.0)
        if p <= n:
            ag += g
            al += ls
            if p == n:
                ag, al = ag / n, al / n
            else:
                continue
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + ls) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def signals(bars: Bars, n: int = 14, lo: float = 30.0, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n = int(n)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        r = rsi_wilder(b.c, n, pos)
        state, peak, pulled = 0, 0.0, False  # 0 idle, 1 below lo, 2 bounced above lo
        for i in range(len(b)):
            ri = float(r[i])
            if pos[i] == 0 or not math.isfinite(ri):
                state = 0
                continue
            if ri <= lo:
                state = 1
                continue
            if state == 1:
                state, peak, pulled = 2, ri, False
            elif state == 2:
                if ri < peak:
                    pulled = True
                elif ri > peak:
                    if pulled:
                        ai = float(a[i])
                        e = float(b.c[i])
                        if math.isfinite(ai) and ai > 0 and (side == "short" or e - stop_atr * ai > 0):
                            out.append(Signal(b.close_time(i), side, sgn * e, sgn * (e - stop_atr * ai), None, hold_ms))
                        state = 0
                    else:
                        peak = ri
    return out
