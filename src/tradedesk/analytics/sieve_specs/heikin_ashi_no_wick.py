"""Heikin Ashi colour flip without a wick, filtered by SMA50, as stated on The Right Trader page "Heikin Ashi Trading
Strategy" (daily bars, regular OHLC).

HA Close = (O + H + L + C) / 4, HA Open = (previous HA Open + previous HA Close) / 2, HA High / HA Low = the highest /
lowest of the regular high / low and HA open and close. Long: the previous HA bar is bearish, the signal bar is bullish
with no lower wick (HA low equals HA open), and the regular close is above the 50-day SMA of regular closes. Stop: the
signal day's regular low. Short: mirror (previous HA bar bullish, signal bar bearish with no upper wick, close below
SMA50, stop at the signal day's regular high). No profit target.

Adaptation: the page enters at the next session's regular open and skips the trade if that open is already through the
stop; the sieve enters at the close of the signal bar (on a 24/7 market the next open is that close). A signal whose
stop is not strictly beyond the entry is dropped.

Defaults added by the harvester (fixed before any run, no variants): the page exits "at the next regular open after the
first later HA bar of the opposite color", a condition on later bars, so the exit is by time after ``hold_bars`` = 20
bars with the signal-bar stop in place (a time exit is only a variation on the page). The page gives no seed for the
first HA bar; the first bar of each data segment seeds HA Open = (O + C) / 2 and HA Close as above, and signals need
``warmup`` = 50 bars into the segment (SMA50). The no-wick test is the page's exact equality on the floats; no
tolerance is added. The 2 x ATR14 stop variant is not applied. Sizing (1 % of equity) is the sieve's R."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "heikin_ashi_no_wick"
SOURCE = "The Right Trader, Heikin Ashi Trading Strategy (https://therighttrader.com/learn/trading-strategies/heikin-ashi-trading-strategy)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"sma_n": 50, "hold_bars": 20}


def signals(bars: Bars, sma_n: int = 50, hold_bars: int = 20) -> list[Signal]:
    n = int(sma_n)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        m = len(b)
        hac = (b.o + b.h + b.low + b.c) / 4.0
        hao = np.full(m, np.nan)
        for i in range(m):
            hao[i] = (b.o[i] + b.c[i]) / 2.0 if pos[i] == 0 else (hao[i - 1] + hac[i - 1]) / 2.0
        hal = np.minimum(b.low, np.minimum(hao, hac))
        for i in range(1, m):
            if pos[i] < n:
                continue
            sma = float(b.c[i - n + 1 : i + 1].mean())
            prev_bear = hac[i - 1] < hao[i - 1]
            bull = hac[i] > hao[i]
            if prev_bear and bull and hal[i] == hao[i] and b.c[i] > sma:
                entry, stop = float(b.c[i]), float(b.low[i])
                if stop < entry and sgn * stop > 0:
                    out.append(Signal(b.close_time(i), side, sgn * entry, sgn * stop, None, hold_ms))
    return out
