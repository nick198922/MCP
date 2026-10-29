"""Wyckoff spring (long) and upthrust after distribution, UTAD (short), as written on The Right Trader's page
"Wyckoff Trading Strategy": daily bars. Box = highest high and lowest low of the 20 bars before the trigger bar, height
at most 12 %. Spring: the prior move is down at least 15 % from the 60-bar high, the bar's low is under the box low
and it closes back inside the box. UTAD is the mirror (up at least 15 % from the 60-bar low, high above the box high,
close back inside). Stop 1 x ATR14 beyond the trigger bar's extreme, target = box edge on the far side +- box height,
time exit after 30 bars (the page's own limit).

Readings and adaptations (the page leaves them open, chosen before any run):
- entry at the close of the trigger bar instead of the next day's open (the sieve enters on the signal bar's close);
- the box excludes the trigger bar; the 60-bar high / low is taken over the 60 bars before the trigger bar;
- "has fallen at least 15 %" is measured on the previous bar's close against that 60-bar high (rise: against the
  60-bar low); the box height is (high - low) / low;
- "closes back inside" means box low <= close <= box high;
- the page's failure exit (a close back through the trigger extreme) needs future bars and is not coded; the stop
  and the 30-bar exit stay."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "wyckoff_spring_utad"
SOURCE = "The Right Trader, Wyckoff Trading Strategy, https://therighttrader.com/learn/trading-strategies/wyckoff-trading-strategy"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"box_n": 20, "move_n": 60, "move_pct": 0.15, "max_height": 0.12, "stop_atr": 1.0, "hold_bars": 30}


def signals(
    bars: Bars,
    box_n: int = 20,
    move_n: int = 60,
    move_pct: float = 0.15,
    max_height: float = 0.12,
    stop_atr: float = 1.0,
    hold_bars: int = 30,
) -> list[Signal]:
    box_n, move_n = int(box_n), int(move_n)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    out: list[Signal] = []
    need = max(box_n, move_n)
    for i in range(len(bars)):
        ai = float(a[i])
        if pos[i] < need or not (np.isfinite(ai) and ai > 0):
            continue
        hh = float(bars.h[i - box_n : i].max())
        ll = float(bars.low[i - box_n : i].min())
        if not (ll > 0 and (hh - ll) / ll <= max_height):
            continue
        height = hh - ll
        prev_c = float(bars.c[i - 1])
        c = float(bars.c[i])
        if not (ll <= c <= hh):
            continue
        hi60 = float(bars.h[i - move_n : i].max())
        lo60 = float(bars.low[i - move_n : i].min())
        if bars.low[i] < ll and prev_c <= hi60 * (1.0 - move_pct):
            stop = float(bars.low[i]) - stop_atr * ai
            target = hh + height
            out.append(Signal(bars.close_time(i), "long", c, stop, target, int(hold_bars) * bars.bar_ms))
        if bars.h[i] > hh and prev_c >= lo60 * (1.0 + move_pct):
            stop = float(bars.h[i]) + stop_atr * ai
            target = ll - height
            out.append(Signal(bars.close_time(i), "short", c, stop, target, int(hold_bars) * bars.bar_ms))
    return out
