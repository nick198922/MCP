"""Volty Expan Close (TradingView built-in strategy "Volty Expan Close Strategy", Kaufman-style volatility expansion):
the channel is the previous close +/- ``num_atrs`` x the average true range of ``length`` bars (defaults 0.75 and 5,
the script's own). A bar that moves above the previous bar's upper line goes long, below the lower line goes short
(the script is stop-and-reverse, short and long). Daily bars (the script has no timeframe; daily is the harvester's
choice, fixed before any run).

Adaptation: the script places stop orders at the previous close +/- the channel offset and is filled intrabar; the sieve
enters at a bar's close, so the signal is the bar whose CLOSE is beyond the previous bar's line (a stricter version of
the same event). The average true range is the simple mean of the true range over ``length`` bars ending at the
previous bar; the two pages read do not show the code or name the averaging, so that is the harvester's reading from the
built-in script (unverified). Defaults added by the harvester (fixed before any run, no variants): the source reverses
on the opposite signal (needs future bars) so the stop is ``stop_atr`` = 2.0 x ATR14 and the exit is by time after
``hold_bars`` = 5 bars (the Oanda Japan description puts the average hold at about 5 bars; it is an outcome, not a
rule)."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "volty_expan_close"
SOURCE = "TradingView, Volty Expan Close Strategy (https://in.tradingview.com/script/mPztR6gR-Volty-Expan-Close-Strategy); logic summary https://www.oanda.jp/lab-education/?p=34388"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"length": 5, "num_atrs": 0.75, "stop_atr": 2.0, "hold_bars": 5}


def signals(bars: Bars, length: int = 5, num_atrs: float = 0.75, stop_atr: float = 2.0, hold_bars: int = 5) -> list[Signal]:
    n = int(length)
    m = len(bars)
    pos = segment_pos(bars.t)
    a14 = atr(bars, 14, pos)
    tr = np.full(m, np.nan)
    for i in range(1, m):
        if pos[i] > 0:
            pc = float(bars.c[i - 1])
            tr[i] = max(float(bars.h[i] - bars.low[i]), abs(float(bars.h[i]) - pc), abs(float(bars.low[i]) - pc))
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(n + 1, m):
        if pos[i] < n + 1:
            continue
        ai = float(a14[i])
        off = num_atrs * float(tr[i - n : i].mean())  # TR of bars i-n .. i-1: known at the previous close
        if not (math.isfinite(ai) and ai > 0 and math.isfinite(off) and off > 0):
            continue
        e = float(bars.c[i])
        prev_c = float(bars.c[i - 1])
        if e > prev_c + off:
            if e - stop_atr * ai > 0:
                out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif e < prev_c - off:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
