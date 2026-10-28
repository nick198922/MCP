"""3-Bar Low (TradingView open-source script "3-Bar Low Strategy" by Botnet101, Jan 2025): a mean-reversion dip buy.
Long when the close is below the lowest low of the previous three bars (``close < lowest(low, 3)[1]``). The source is long only; its optional filter (close above EMA200) is off by default and not
applied; the source exit is ``close > highest(high, 7)[1]``; no stop. Daily bars (the source says only "various timeframes").

Source parameters, used unchanged: lookback 3 for the entry. A signal is emitted on every bar where the condition holds
(the source enters whenever flat); the engine keeps one position per symbol.

Defaults added by the harvester (fixed before any run, no variants): daily timeframe (the source names none); the source
exit (close above the highest high of the previous 7 bars) moves with future bars, so a time exit after ``hold_bars`` = 20
bars (20 days) is used instead; the source has no stop, so ``stop_atr`` = 2.0 x ATR14. The 20-day hold is much longer than
the typical 1-5 day mean-reversion exit of the source, so the test measures "after a 3-bar breakdown, hold 20 days"."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "three_bar_low"
SOURCE = "Botnet101, 3-Bar Low Strategy, TradingView (https://www.tradingview.com/script/jLmMUl3o-3-Bar-Low-Strategy)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 3, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, n: int = 3, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n = int(n)
    m = len(bars)
    if m == 0:
        return []
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(n, m):
        if pos[i] < n:
            continue
        ai = float(a[i])
        if not (np.isfinite(ai) and ai > 0):
            continue
        ll = float(bars.low[i - n : i].min())  # previous n bars, not bar i
        e = float(bars.c[i])
        if e < ll and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
    return out
