"""Connors three-day high/low, short side (L. Connors, "High Probability ETF Trading", 2009; rules from the EdgeRater
article "Connors ETF Strategy 1B: Three Day High/Low Short Entry"). Short when the close is below its 200-day SMA and
above its 5-day SMA and the highs and the lows both rose on each of the last three days (high and low of today above
yesterday's, yesterday's above the day before's, that above the one before it: four bars in a chain). The long entry is
the mirror image (close above SMA200 and below SMA5, three days of lower highs and lower lows); the article refers to a
previous article for it, so the long side is the stated "reverse" and not read from the page.

Defaults added by the harvester: the article gives no stop and no exit, it only measures fixed holds of 1-10 bars and
says 5 was best, so ``hold_bars`` = 5 (the article's own number, not ours) and the stop is 2.0 ATR14. Entry is the
close of the signal bar, as the article's event is on the close. Not applied: its optional filter of a falling SMA200.
Daily bars."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "connors_3day_hl"
SOURCE = "L. Connors, High Probability ETF Trading (2009); EdgeRater, Three Day High/Low Short Entry (https://edgerater.com/blog/connors-etf-strategy-1b-three-day-highlow-short-entry)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"trend_n": 200, "exit_n": 5, "stop_atr": 2.0, "hold_bars": 5}


def sma(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Trailing simple mean of ``n`` values inside a segment; NaN before."""
    out = np.full(x.size, np.nan)
    for i in range(x.size):
        if pos[i] >= n - 1:
            out[i] = float(np.mean(x[i - n + 1 : i + 1]))
    return out


def signals(bars: Bars, trend_n: int = 200, exit_n: int = 5, stop_atr: float = 2.0, hold_bars: int = 5) -> list[Signal]:
    pos = segment_pos(bars.t)
    c, h, lo = bars.c, bars.h, bars.low
    trend = sma(c, int(trend_n), pos)
    fast = sma(c, int(exit_n), pos)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(3, len(bars)):
        tr, fa, ai = float(trend[i]), float(fast[i]), float(a[i])
        if not (np.isfinite(tr) and np.isfinite(fa) and np.isfinite(ai) and ai > 0) or pos[i] < 3:
            continue
        up = all(h[j] > h[j - 1] and lo[j] > lo[j - 1] for j in (i - 2, i - 1, i))
        down = all(h[j] < h[j - 1] and lo[j] < lo[j - 1] for j in (i - 2, i - 1, i))
        e = float(c[i])
        if up and e < tr and e > fa:
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
        elif down and e > tr and e < fa:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
    return out
