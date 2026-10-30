"""Trendline touch as stated on The Right Trader page "Trendline Trading Strategy" (daily chart, 5-bar pivots).

Swing low: "a swing low has two higher lows on each side" (strict, 5-bar pivot); swing high mirrors with two lower
highs per side. Uptrend line: the straight line through two rising swing lows, extended forward without adding a third
point. Touch bar: its low meets or crosses the line and "its close finishes back on the trend side" (close at or above
the line). Long. Stop: "touch low minus 0.5 % of that low". Target: 2R from the entry (R = entry - stop). Time stop: if
neither target nor stop is hit within ten bars, exit at that close. Short: mirror with two falling swing highs, stop at
"touch high plus 0.5 % of that high".

Causality: a 5-bar pivot at bar j is known only at the close of bar j + 2, so the line uses only pivots with
j + 2 < i (confirmed on an earlier bar than the touch bar). The page notes this lag and gives no rule for it.

Readings and defaults (fixed before any run, no variants):
- The line goes through the two most recent confirmed swing lows (in the same data segment) and is used only if the
  later one is higher; if the latest pair is falling, there is no uptrend line. The page says "two rising swing lows"
  without saying which two.
- The line is not cancelled by earlier closes below it (the page only lists a later close through the line as an exit
  after entry); every touch bar that satisfies the rule signals, the sieve skips signals while a trade is open.
- Adaptation: the page places a buy stop one tick above the touch bar's high after the close, cancelled after two bars;
  the sieve enters at the touch bar's close. Tick size is not given, so no tick is added.
- The page also exits on any later close through the line; that is a condition on later bars, so only the 2R target,
  the stop and the ten-bar time stop are applied. The optional 50-day average filter is not applied. Sizing (1 % of
  equity) is the sieve's R. A signal needs 4 completed bars in the segment before the first pivot."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "rt_trendline_touch"
SOURCE = "The Right Trader, Trendline Trading Strategy (https://therighttrader.com/learn/trading-strategies/trendline-trading-strategy)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"buffer": 0.005, "target_r": 2.0, "hold_bars": 10}


def signals(bars: Bars, buffer: float = 0.005, target_r: float = 2.0, hold_bars: int = 10) -> list[Signal]:
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        low, c = b.low, b.c
        pivots: list[int] = []  # swing lows confirmed at an earlier bar than the current one
        for i in range(len(b)):
            if pos[i] == 0:
                pivots = []
            if len(pivots) >= 2:
                j1, j2 = pivots[-2], pivots[-1]
                p1, p2 = float(low[j1]), float(low[j2])
                if p2 > p1:
                    line = p2 + (p2 - p1) / (j2 - j1) * (i - j2)
                    lo, cl = float(low[i]), float(c[i])
                    if lo <= line <= cl and math.isfinite(line):
                        stop = lo - buffer * abs(lo)
                        if stop < cl:
                            risk = cl - stop
                            real_stop = sgn * stop
                            if real_stop > 0:
                                out.append(Signal(b.close_time(i), side, sgn * cl, real_stop, sgn * (cl + target_r * risk), hold_ms))
            j = i - 2  # pivot candidate confirmed by bar i (usable from bar i + 1)
            if j >= 2 and pos[i] >= 4:
                lj = low[j]
                if lj < low[j - 1] and lj < low[j - 2] and lj < low[j + 1] and lj < low[j + 2]:
                    pivots.append(j)
    return out
