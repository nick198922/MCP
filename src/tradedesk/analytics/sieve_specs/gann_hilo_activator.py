"""Gann HiLo Activator (R. Krausz, Gann swing trading; Schwab thinkorswim Learning Center, "HiLoActivator", daily bars).

Two lines: the 3-bar simple average of highs and the 3-bar simple average of lows. A close above the average high of
the previous bar turns the state bullish (the activator plots the average low, a trailing stop under price); a close
below the average low of the previous bar turns it bearish. The page calls it "an entry signal and a trailing stop".
Rule coded: the bar whose close flips the state is the signal; long on a flip to bullish, short on a flip to bearish;
entry at that close; stop = the activator line that becomes the stop (the 3-bar average low of the previous bar for a
long, the average high for a short). The lines of the previous bar are used so the bar's own range does not enter its
own test (causal and unambiguous).

Defaults added by the harvester (fixed before any run, no variants): the page gives no target and no time exit, and the
trailing stop cannot follow the position in the sieve, so the stop is frozen at entry and the position is closed after
20 bars if the stop is not hit (the flip to the opposite state, the source's real exit, is a later event). The first
state of a data segment is not a signal; only a flip from the opposite state is. The page does not name the averaging
method ("moving averages"); Krausz's description and most implementations use simple averages."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "gann_hilo_activator"
SOURCE = "R. Krausz, Gann HiLo Activator (https://toslc.thinkorswim.com/center/reference/Tech-Indicators/studies-library/G-L/HiLoActivator)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"length": 3, "hold_bars": 20}


def sma(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Simple average of the last ``n`` values; NaN until the segment holds ``n`` bars."""
    out = np.full(x.size, np.nan)
    for i in range(n - 1, x.size):
        if pos[i] >= n - 1:
            out[i] = float(x[i - n + 1 : i + 1].mean())
    return out


def signals(bars: Bars, length: int = 3, hold_bars: int = 20) -> list[Signal]:
    n = int(length)
    pos = segment_pos(bars.t)
    avg_h = sma(bars.h, n, pos)
    avg_l = sma(bars.low, n, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    state = 0
    for i in range(1, len(bars)):
        if pos[i] == 0:
            state = 0
            continue
        ah, al = float(avg_h[i - 1]), float(avg_l[i - 1])
        if not (np.isfinite(ah) and np.isfinite(al)):
            continue
        c = float(bars.c[i])
        new = state
        if c > ah:
            new = 1
        elif c < al:
            new = -1
        if new != state and state != 0:
            if new == 1:
                out.append(Signal(bars.close_time(i), "long", c, al, None, hold_ms))
            else:
                out.append(Signal(bars.close_time(i), "short", c, ah, None, hold_ms))
        state = new
    return out
