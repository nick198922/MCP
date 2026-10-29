"""Monthly floor-pivot cross (The Right Trader, "Pivot Point Trading Strategy", daily bars).

Levels from the previous calendar month (UTC), fixed for the whole month: P = (H + L + C) / 3, R1 = 2P - L,
S1 = 2P - H, with H and L the month's highest high and lowest low and C its last daily close. Long: a daily close above
P while the previous daily close was below P; stop = the signal day's low; target R1. Short: a daily close below P
while the previous close was above P; stop = the signal day's high; target S1. Chop filter: if at least three of the last
10 daily closes are above P and at least three below, skip. Exit after 15 trading days if neither stop nor target hit.

Readings and defaults (fixed before any run, no variants):
- The page enters at the next session's open; the sieve enters at the close of the signal day (a 24/7 market has no
  overnight gap, so the open is the previous close), and the "skip if the open is already through the stop" rule has no
  effect.
- "The last 10 daily closes" include the signal day's close. A signal needs those 10 daily bars to be consecutive.
- A signal is skipped when its target is not beyond the entry (R1 at or below the close for a long, S1 at or above it
  for a short) or the stop is not on the loss side of the entry.
- The previous month must be complete (every calendar day present) for the levels to exist.
- The page also exits at the next open after a daily close back through P (a condition on later bars); not coded, the
  stop, the target and the 15-day limit stay (``hold_bars`` = 15, from the page). The page publishes no win rate."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, Bars
from tradedesk.analytics.strategy_sieve import Signal

NAME = "monthly_floor_pivot_cross"
SOURCE = "The Right Trader, Pivot Point Trading Strategy (https://therighttrader.com/learn/trading-strategies/pivot-point-trading-strategy)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"chop_window": 10, "chop_min": 3, "hold_bars": 15}


def month_of_day(days: np.ndarray) -> np.ndarray:
    """Calendar month index (months since 1970-01) of UTC day numbers."""
    return np.asarray(days, dtype=np.int64).astype("datetime64[D]").astype("datetime64[M]").astype(np.int64)


def month_start_day(months: np.ndarray) -> np.ndarray:
    """UTC day number of the 1st of each month index."""
    return np.asarray(months, dtype=np.int64).astype("datetime64[M]").astype("datetime64[D]").astype(np.int64)


def signals(bars: Bars, chop_window: int = 10, chop_min: int = 3, hold_bars: int = 15) -> list[Signal]:
    n = len(bars)
    if n < 2 or bars.bar_ms != DAY_MS:
        return []
    cw = int(chop_window)
    hold_ms = int(hold_bars) * DAY_MS
    day = bars.t // DAY_MS
    month = month_of_day(day)
    starts = np.flatnonzero(np.concatenate([[True], month[1:] != month[:-1]]))
    ends = np.concatenate([starts[1:], [n]])
    out: list[Signal] = []
    for g in range(1, starts.size):
        a, b = int(starts[g]), int(ends[g])
        pa, pb = int(starts[g - 1]), int(ends[g - 1])
        pm = int(month[pa])
        if int(month[a]) - pm != 1:
            continue
        days_in_prev = int(month_start_day(np.array([pm + 1]))[0] - month_start_day(np.array([pm]))[0])
        if pb - pa != days_in_prev or int(day[pb - 1]) - int(day[pa]) != days_in_prev - 1:
            continue
        hi, lo, cl = float(bars.h[pa:pb].max()), float(bars.low[pa:pb].min()), float(bars.c[pb - 1])
        p = (hi + lo + cl) / 3.0
        r1, s1 = 2.0 * p - lo, 2.0 * p - hi
        for i in range(max(a, cw - 1, 1), b):
            if int(day[i]) - int(day[i - cw + 1]) != cw - 1:
                continue
            c, c0 = float(bars.c[i]), float(bars.c[i - 1])
            w = bars.c[i - cw + 1 : i + 1]
            if int(np.sum(w > p)) >= chop_min and int(np.sum(w < p)) >= chop_min:
                continue
            t_ms = bars.close_time(i)
            if c > p > c0:
                stop = float(bars.low[i])
                if stop < c < r1:
                    out.append(Signal(t_ms, "long", c, stop, r1, hold_ms))
            elif c < p < c0:
                stop = float(bars.h[i])
                if s1 < c < stop:
                    out.append(Signal(t_ms, "short", c, stop, s1, hold_ms))
    return out
