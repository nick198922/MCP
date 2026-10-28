"""Inside-bar breakout of the mother bar (price-action classic; rules from the LuxAlgo article "Inside Bar Breakouts").
Strict inside bar: the completed bar's high is below the previous bar's (the mother bar's) high and its low above the
mother's low. Long when the bar right after the inside bar closes above the mother-bar high, short when it closes below
the mother-bar low; the stop is the opposite end of the mother bar (the article's "one testable rule"). Daily bars.

Sieve adaptation: the article offers "intrabar trigger or completed-bar close" and leaves the choice open; a close is
the only form the sieve can enter, with no buffer (the article's $0.10 offset is an example, not a rule). Its "cancel
after a defined number of bars" is read as: only the bar immediately after the inside bar can trigger. Defaults added:
the article gives no target or time exit, so no target and a time exit after ``hold_bars`` = 10 bars. No trend or
volatility filter (the article lists them as optional context without a definition). Signals need three consecutive
bars of one segment (mother, inside, breakout)."""

from __future__ import annotations

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "inside_bar_breakout"
SOURCE = "LuxAlgo, Inside Bar Breakouts: Tight Risk, Big Reward (https://www.luxalgo.com/blog/inside-bar-breakouts-tight-risk-big-reward/)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"hold_bars": 10}


def signals(bars: Bars, hold_bars: int = 10) -> list[Signal]:
    pos = segment_pos(bars.t)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(2, len(bars)):
        if pos[i] < 2:
            continue
        mh, ml = float(bars.h[i - 2]), float(bars.low[i - 2])
        if not (bars.h[i - 1] < mh and bars.low[i - 1] > ml):
            continue
        e = float(bars.c[i])
        if e > mh:
            out.append(Signal(bars.close_time(i), "long", e, ml, None, hold_ms))
        elif e < ml:
            out.append(Signal(bars.close_time(i), "short", e, mh, None, hold_ms))
    return out
