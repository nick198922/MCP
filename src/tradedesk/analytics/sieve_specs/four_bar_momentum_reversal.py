"""4 Bar Momentum Reversal (StockSharp API example 0480, which also has a TradingView counterpart): count consecutive bars
whose close is below the close ``lookback`` bars earlier; the count resets to zero on any bar that does not. When the count
reaches ``buy_threshold`` the strategy buys (long only). Source defaults: ``Lookback`` = 4, ``BuyThreshold`` = 4. The source
exit is a close above the previous bar's high; no stop.

Source parameters, used unchanged: lookback 4, threshold 4. A signal is emitted on every bar where the count is at least the
threshold (the source buys whenever flat); the engine keeps one position per symbol.

Defaults added by the harvester (fixed before any run, no variants): the source default is 30-minute candles, which the sieve
does not have; daily bars are used (the TradingView version is a daily mean-reversion strategy, per a search snippet, not
read). The exit "close above the previous high" needs future bars, so a time exit after ``hold_bars`` = 20 bars (20 days) is
used (the source typically exits within days: the test measures "after 4 bars of closes below the close 4 bars earlier, hold
20 days"). The source has no stop, so ``stop_atr`` = 2.0 x ATR14. The source's StartTime / EndTime window is not used."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "four_bar_momentum_reversal"
SOURCE = (
    "StockSharp API example 0480 Four Bar Momentum Reversal (https://doc.stocksharp.com/de/api-examples/0480_Four_Bar_Momentum_Reversal)"
)
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"lookback": 4, "buy_threshold": 4, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, lookback: int = 4, buy_threshold: int = 4, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    lb, thr = int(lookback), int(buy_threshold)
    m = len(bars)
    if m == 0:
        return []
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    count = 0
    for i in range(m):
        if pos[i] < lb:
            count = 0
            continue
        if bars.c[i] < bars.c[i - lb]:
            count += 1
        else:
            count = 0
        ai = float(a[i])
        if count >= thr and np.isfinite(ai) and ai > 0:
            e = float(bars.c[i])
            if e - stop_atr * ai > 0:
                out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
    return out
