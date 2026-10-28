"""Short after consecutive closes above the prior high (TradingView open-source strategy "[SHORT ONLY] Consecutive
Close>High[1] Mean Reversion Strategy", with the same logic in the StockSharp example "Consecutive Close High1 Mean
Reversion"): the bull counter increases by one on each bar whose close is above the previous bar's high and resets
only on a bar that closes below the previous bar's low (a bar that does neither leaves it unchanged); go short when
the counter reaches ``threshold`` = 3 (and on any bar after it while it stays there).

Readings and defaults chosen by the harvester before any run. The reset rule is taken from the StockSharp code
(``if close > prevHigh: count++; if close < prevLow: count = 0``); the TradingView page could not be read, so its
reset ("bearish bar") is unverified. The optional EMA trend filter (close below EMA; the StockSharp code applies it
with default period 50 although its text says 200) is dropped, so this tests the unfiltered short. The StockSharp
default timeframe is 4h; the sieve has only 1d/1h, so daily bars are used. The exit of both
sources (close below the prior low) needs future bars, so: stop ``stop_atr`` = 2.0 ATR14 above the entry (the sources
have none) and a time exit after ``hold_bars`` = 20 bars. Short only, as in the sources; the sieve skips signals while
a position is open. Daily bars."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "consecutive_high_close_short"
SOURCE = "TradingView, [SHORT ONLY] Consecutive Close>High[1] Mean Reversion Strategy (https://www.tradingview.com/script/caPewFnR-SHORT-ONLY-Consecutive-Close-High-1-Mean-Reversion-Strategy); StockSharp example 1601"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"threshold": 3, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, threshold: int = 3, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    count = 0
    for i in range(len(bars)):
        if pos[i] == 0:
            count = 0
            continue
        if bars.c[i] > bars.h[i - 1]:
            count += 1
        elif bars.c[i] < bars.low[i - 1]:
            count = 0
        ai = float(a[i])
        if count >= int(threshold) and np.isfinite(ai) and ai > 0:
            e = float(bars.c[i])
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
