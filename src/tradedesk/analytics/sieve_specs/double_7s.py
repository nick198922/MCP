"""Connors "Double 7's" (L. Connors, C. Alvarez, "Short Term Trading Strategies That Work", 2008; rule text per the
StrategyQuant write-up): if the close is above the 200-day SMA and is the lowest close of the last 7 days (including
today), buy at the close; the source sells at the close when the close is the highest of the last 7 days. No stop, no
time exit in the source. Long only.

Defaults added by the harvester (fixed before any run, no variants): the exit "close is the highest of the last 7
closes" moves with future bars, so it is approximated by a fixed target at signal time, the highest close of the 7
bars ending at the signal bar (the bar's own close is the lowest of them); a trade that does not reach it within
``hold_bars`` = 20 bars (harvester default) is closed by time. Stop ``stop_atr`` = 2.0 x ATR14 below the entry
(harvester default; the source has none). Daily bars, UTC day. The source tests US index ETFs; crypto is not tested
there."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "double_7s"
SOURCE = "L. Connors, C. Alvarez, Short Term Trading Strategies That Work (2008); https://strategyquant.com/blog/larry-connors-double-7-strategy-tested-on-spy-and-8-other-markets/"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 7, "sma": 200, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, n: int = 7, sma: int = 200, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n, sma = int(n), int(sma)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(len(bars)):
        if pos[i] < sma - 1:
            continue
        e = float(bars.c[i])
        win = bars.c[i - n + 1 : i + 1]
        ai = float(a[i])
        if not (math.isfinite(ai) and ai > 0):
            continue
        if e > float(bars.c[i - sma + 1 : i + 1].mean()) and e <= float(win.min()):
            tgt = float(win.max())
            if tgt > e and e - stop_atr * ai > 0:
                out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, tgt, hold_ms))
    return out
