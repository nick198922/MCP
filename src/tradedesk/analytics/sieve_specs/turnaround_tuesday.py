"""Turnaround Tuesday (StrategyQuant "Tuesday Reversal Strategy", a rule set built on the long-discussed idea that a
weak Monday is followed by a bounce): on a Monday whose close is more than 1 % below its open, with the close above
the previous bar's SMA200, buy; the source buys at Tuesday's open, exits when the close is above the close two bars
earlier or after 10 daily bars, and uses no stop. Long only.

Crypto has no session, so Monday is the UTC day and Tuesday's open (00:00 UTC) is the same price as Monday's close:
the entry is the close of the Monday bar, the same price as in the source's rule.

Defaults added by the harvester (fixed before any run, no variants): the exit "close above the close two bars
earlier" needs future bars, so the exit is by time after ``hold_bars`` = 10 bars (the source's own limit); the source
has no stop, so ``stop_atr`` = 2.0 x ATR14 below the entry (harvester default). The source tests US stocks; the effect
is not shown for crypto."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import DAY_MS, Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "turnaround_tuesday"
SOURCE = "StrategyQuant, Tuesday Reversal Strategy: Tested on Nasdaq 100 and S&P 500 Stocks (https://strategyquant.com/blog/tuesday-reversal-strategy-tested-on-nasdaq-100-and-sp-500-stocks/)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"drop": 0.01, "sma": 200, "stop_atr": 2.0, "hold_bars": 10}


def signals(bars: Bars, drop: float = 0.01, sma: int = 200, stop_atr: float = 2.0, hold_bars: int = 10) -> list[Signal]:
    sma = int(sma)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(len(bars)):
        if pos[i] < sma:
            continue
        if (int(bars.t[i]) // DAY_MS + 3) % 7 != 0:  # 1970-01-01 was a Thursday; 0 = Monday
            continue
        ai = float(a[i])
        e = float(bars.c[i])
        if not (math.isfinite(ai) and ai > 0 and e < (1.0 - drop) * float(bars.o[i])):
            continue
        prev_sma = float(bars.c[i - sma : i].mean())  # SMA of the previous bar, as in the source
        if e > prev_sma and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
    return out
