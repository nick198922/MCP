"""Lunar-phase strategy (LuxAlgo "Moon Phases Strategy", TradingView): the cycle is a fixed 29.530588853 days counted
from a reference new moon; the default rule buys on the full moon and sells (goes short) on the new moon. Here: at the
close of the daily (UTC) bar that contains a full moon go long, at the close of the bar that contains a new moon go
short; each position is held until the close of the bar of the next event (about 14.8 days, so the strategy is nearly
always in the market). Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the page gives no default reference date, no stop
and no exit. The reference is the astronomical new moon of 2000-01-06 18:14 UTC; events are ref + k x P (new) and
ref + (k + 1/2) x P (full) with the page's P, so the mean-cycle approximation drifts from the true phase by up to about
half a day (the page itself accepts this on higher timeframes). The exit is the next event (known in advance from the
calendar, so it is a time exit, ``hold_ms`` differs per signal) and the stop is ``stop_atr`` = 2.0 x ATR14. The page's
inverted setting (buy new moon) is the mirror of this one and is not run. The source claims no edge (its own backtests
show about 50 % profitability). The sieve gives every symbol the same dates, so the trades are strongly clustered."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "moon_phase_cycle"
SOURCE = "LuxAlgo, Moon Phases Strategy (https://www.luxalgo.com/library/indicator/moon-phases-strategy/), cycle 29.530588853 d"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"stop_atr": 2.0}

REF_NEW_MOON_MS = 947_182_440_000  # 2000-01-06 18:14 UTC
CYCLE_MS = 29.530588853 * 86_400_000.0


def signals(bars: Bars, stop_atr: float = 2.0) -> list[Signal]:
    m = len(bars)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    half = CYCLE_MS / 2.0
    step = bars.bar_ms
    out: list[Signal] = []
    for i in range(m):
        k = math.ceil((float(bars.t[i]) - REF_NEW_MOON_MS) / half)  # first event at or after the bar open
        ev = REF_NEW_MOON_MS + k * half
        if not ev < float(bars.t[i]) + step:
            continue
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        nxt = REF_NEW_MOON_MS + (k + 1) * half
        exit_close = (math.floor(nxt / step) + 1) * step
        close_t = bars.close_time(i)
        hold_ms = int(exit_close - close_t)
        if hold_ms <= 0:
            continue
        c = float(bars.c[i])
        if k % 2 == 1:  # full moon: long
            if c - stop_atr * ai > 0:
                out.append(Signal(close_t, "long", c, c - stop_atr * ai, None, hold_ms))
        else:  # new moon: short
            out.append(Signal(close_t, "short", c, c + stop_atr * ai, None, hold_ms))
    return out
