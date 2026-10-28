"""Carver's EMAC(16, 64) trend rule (R. Carver, "Advanced Futures Trading Strategies", Harriman House 2023, strategy #8;
rule as re-created in QuantConnect research "futures fast trend following with trend strength"): EMAC = EMA(16) -
EMA(64); "when the EMAC is positive, the instrument is in an uptrend and when the EMAC is negative, a downtrend". The
source holds a continuously sized position by the sign (and strength) of the volatility-normalised forecast and has no
stop-loss.

Sieve adaptation: the sieve has one entry per signal, so the trade is opened on the day EMAC changes sign: previous
EMAC at or below zero and now above zero is a long, previous at or above zero and now below zero is a short, on the
daily close. Forecast scaling, volatility targeting and the position buffer are position sizing, not direction, and are
not described. The exit (sign flip) needs future bars.

Source parameters, used unchanged: spans 16 and 64 (slow = 4 x fast). EMAs are seeded with the simple mean of the first
``n`` closes of a segment.

Defaults added by the harvester (the source has none): stop ``stop_atr`` = 2.0 x ATR14 from the entry, time exit after
``hold_bars`` = 20 daily bars. No variants."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "carver_emac_16_64"
SOURCE = "R. Carver, Advanced Futures Trading Strategies (2023), strategy #8; rule per QuantConnect research (https://www.quantconnect.com/research/15875/futures-fast-trend-following-with-trend-strength/)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"fast": 16, "slow": 64, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, fast: int = 16, slow: int = 64, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    pos = segment_pos(bars.t)
    d = ema(bars.c, int(fast), pos) - ema(bars.c, int(slow), pos)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        if not (np.isfinite(d[i]) and np.isfinite(d[i - 1])):
            continue
        ai = float(a[i])
        if not (np.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        if d[i - 1] <= 0 < d[i]:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif d[i - 1] >= 0 > d[i]:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
