"""UT Bot Alerts (TradingView open-source indicator by QuantNomad / Yo_adriiiiaan, after HPotter's ATR trailing stop):
an ATR trailing stop on the close, ``nLoss`` = ``key`` x ATR(10) (Wilder), with the original defaults key = 1 and
ATR period 10. The stop only ratchets in the trend's direction (up while the close stays above it, down while below) and
resets to close -/+ nLoss when the close crosses it. Buy when the close crosses above the stop, sell (short) when it
crosses below. The Heikin Ashi option of the script is off (its default). Long and short, 4h bars (the script has no
timeframe; 4h is the harvester's choice, fixed before any run).

Defaults added by the harvester (fixed before any run, no variants): the source exits on the opposite signal (needs
future bars) so the exit is by time after ``hold_bars`` = 20 bars; the stop is the trailing-stop line at the signal bar
(= the close -/+ nLoss, 1 ATR10 from the entry), which is the script's own stop level. On the first valid bar of a
segment the stop starts at close -/+ nLoss on the long side (no signal). The page used (an MQL5 port) describes the four
branches in words, not the exact max / min expressions; the standard form is used."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "ut_bot_alerts"
SOURCE = "QuantNomad / Yo_adriiiiaan, UT Bot Alerts (TradingView); defaults per https://www.mql5.com/zh/market/product/166242 and https://www.luxalgo.com/library/indicator/n8ss8BID-ut-bot-alerts/"
TIMEFRAMES = (4,)
MANAGEMENT = "strategy"
PARAMS = {"key": 1.0, "atr_n": 10, "hold_bars": 20}


def signals(bars: Bars, key: float = 1.0, atr_n: int = 10, hold_bars: int = 20) -> list[Signal]:
    m = len(bars)
    pos = segment_pos(bars.t)
    a = atr(bars, int(atr_n), pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    stop = np.full(m, np.nan)
    for i in range(m):
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        n_loss = key * ai
        c = float(bars.c[i])
        prev = float(stop[i - 1]) if i > 0 and pos[i] > 0 and math.isfinite(float(stop[i - 1])) else math.nan
        if not math.isfinite(prev):
            stop[i] = c - n_loss
            continue
        c1 = float(bars.c[i - 1])
        if c > prev and c1 > prev:
            stop[i] = max(prev, c - n_loss)
        elif c < prev and c1 < prev:
            stop[i] = min(prev, c + n_loss)
        elif c > prev:
            stop[i] = c - n_loss
        else:
            stop[i] = c + n_loss
        crossed_up = c > stop[i] and c1 <= prev
        crossed_dn = c < stop[i] and c1 >= prev
        if crossed_up and 0 < stop[i] < c:
            out.append(Signal(bars.close_time(i), "long", c, float(stop[i]), None, hold_ms))
        elif crossed_dn and stop[i] > c:
            out.append(Signal(bars.close_time(i), "short", c, float(stop[i]), None, hold_ms))
    return out
