"""Faber's 10-month SMA timing rule (M. Faber, "A Quantitative Approach to Tactical Asset Allocation", 2007, updated
2013; rule as summarised by CXO Advisory): at the end of each month hold the asset if its price is above the simple
moving average of the last 10 month-end prices (the current month-end and the 9 before), otherwise stay in cash. Held
one month, re-tested at the next month end. Long only (the source goes to cash, never short).

Daily bars. The month end is the close of the last daily bar of a calendar month (00:00 UTC of the 1st); the SMA is the
mean of 10 consecutive month-end closes inside one data segment (a symbol needs 10 months of history). A signal at each
month end whose close is above that SMA; hold to the close of the next month's last day.

Defaults added by the harvester (fixed before any run, no variants): the source has no stop, so ``stop_atr`` = 2.0 x
ATR14 below the entry (a tight stop for a ~30 day hold: the test measures "above SMA10m with a 2 ATR stop", not the
paper's stop-less rule). The source is for asset-class indices; the sieve applies it to every crypto perpetual."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "faber_sma10m"
SOURCE = "M. Faber, A Quantitative Approach to Tactical Asset Allocation (2007/2013); rule per https://www.cxoadvisory.com/technical-trading/long-term-outperformance-from-trends-defined-by-moving-averages"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"months": 10, "stop_atr": 2.0}


def month_of_day(days: np.ndarray) -> np.ndarray:
    """Calendar month index (months since 1970-01) of UTC day numbers."""
    return np.asarray(days, dtype=np.int64).astype("datetime64[D]").astype("datetime64[M]").astype(np.int64)


def month_start_day(months: np.ndarray) -> np.ndarray:
    """UTC day number of the 1st of each month index."""
    return np.asarray(months, dtype=np.int64).astype("datetime64[M]").astype("datetime64[D]").astype(np.int64)


def signals(bars: Bars, months: int = 10, stop_atr: float = 2.0) -> list[Signal]:
    months = int(months)
    if len(bars) == 0:
        return []
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    open_day = bars.t // DAY_MS
    close_day = (bars.t + bars.bar_ms) // DAY_MS
    m_open, m_close = month_of_day(open_day), month_of_day(close_day)
    exit_ms = month_start_day(m_close + 1) * DAY_MS  # close of the next month's last day
    ends = np.flatnonzero(m_close != m_open)  # last daily bar of each calendar month
    out: list[Signal] = []
    for k in range(months - 1, ends.size):
        i = int(ends[k])
        j = int(ends[k - months + 1])
        if int(m_open[i]) - int(m_open[j]) != months - 1 or int(pos[i]) < i - j:
            continue  # 10 consecutive month ends inside one segment required
        s = float(np.mean(bars.c[ends[k - months + 1 : k + 1]]))
        ai = float(a[i])
        e = float(bars.c[i])
        if not (math.isfinite(ai) and ai > 0 and e > s and e - stop_atr * ai > 0):
            continue
        t_ms = bars.close_time(i)
        hold_ms = int(exit_ms[i]) - t_ms
        if hold_ms > 0:
            out.append(Signal(t_ms, "long", e, e - stop_atr * ai, None, hold_ms))
    return out
