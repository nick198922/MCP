"""Volatility System (Freqtrade ``futures/VolatilitySystem.py``, "based on a TradingView script"; the idea is Wilder's
volatility system): on 3-hour bars, go long when the close-to-close change exceeds ``mult`` x ATR14 of the previous bar,
short when the fall exceeds it. The source enters on every bar where the condition holds, sizes by a fixed 2x leverage
and reverses on the opposite signal; it has no stop (``stoploss = -1``) and no target (``minimal_roi = {"0": 100}``).

Source parameters, used unchanged: 3-hour candles, ATR period 14, multiplier 2.0, the ATR of the bar before the signal
bar (``atr.shift(1)``).

Sieve adaptation: the source resamples a 1h dataframe to 3h and merges the values back; here the rule runs directly on
the 3h bars (``TIMEFRAMES = (3,)``), so a signal is on the close of the 3h bar. The source's exit, the opposite signal,
needs future bars, and its adding to a position cannot be described.

Defaults added by the harvester (the source has none): stop ``stop_atr`` = 2.0 x ATR14 of the signal bar from the entry,
and a time exit after ``hold_bars`` = 20 bars (60 hours). No variants."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "freqtrade_volatility_system"
SOURCE = "Freqtrade strategies repo, futures/VolatilitySystem.py (https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/futures/VolatilitySystem.py)"
TIMEFRAMES = (3,)
MANAGEMENT = "strategy"
PARAMS = {"atr_n": 14, "mult": 2.0, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, atr_n: int = 14, mult: float = 2.0, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        a = atr(b, int(atr_n), pos)
        a14 = atr(b, 14, pos)
        for i in range(1, len(b)):
            prev_atr = float(a[i - 1])
            ai = float(a14[i])
            if not (np.isfinite(prev_atr) and prev_atr > 0 and np.isfinite(ai) and ai > 0):
                continue
            if float(b.c[i] - b.c[i - 1]) > mult * prev_atr:
                e = float(b.c[i])
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * (e - stop_atr * ai), None, int(hold_bars) * b.bar_ms))
    return out
