"""12-month time-series momentum (T. Moskowitz, Y. H. Ooi, L. H. Pedersen, "Time Series Momentum", JFE 2012): at
each month end go long an asset whose return over the past 12 months is positive and short one whose return is
negative, hold one month, rebalance monthly. Daily bars; the month end is the close of the last daily bar of a calendar
month (00:00 UTC of the 1st), the 12-month return is close-to-close over 365 days (crypto trades every day).

The paper scales positions by ex-ante volatility and uses no stop. Here the R unit is volatility-scaled instead: the
setup-hunt stage-5 disaster stop 2 * sigma_d * sqrt(hold days) (sigma_d: sample std of the last ``vol_n`` daily log
returns), and the hold runs to the close of the next month's last day. Excess return over cash is not subtracted
(funding is charged by the sieve). Parameters registered in IDEA-20261017-setup-hunt before the run; no variants."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "tsmom_12m"
SOURCE = "T. Moskowitz, Y. H. Ooi, L. H. Pedersen, Time Series Momentum, JFE 104 (2012): 12-month lookback, 1-month hold"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"lookback_days": 365, "vol_n": 60, "stop_sigma": 2.0, "max_lag_days": 3}


def month_of_day(days: np.ndarray) -> np.ndarray:
    """Calendar month index (months since 1970-01) of UTC day numbers."""
    return np.asarray(days, dtype=np.int64).astype("datetime64[D]").astype("datetime64[M]").astype(np.int64)


def month_start_day(months: np.ndarray) -> np.ndarray:
    """UTC day number of the 1st of each month index."""
    return np.asarray(months, dtype=np.int64).astype("datetime64[M]").astype("datetime64[D]").astype(np.int64)


def sigma_d(c: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Sample std (ddof 1) of the last ``n`` log close-to-close returns inside a segment; NaN before."""
    out = np.full(c.size, np.nan)
    lr = np.full(c.size, np.nan)
    lr[1:] = np.log(c[1:] / c[:-1])
    for i in range(c.size):
        if pos[i] >= n:
            out[i] = float(np.std(lr[i - n + 1 : i + 1], ddof=1))
    return out


def signals(bars: Bars, lookback_days: int = 365, vol_n: int = 60, stop_sigma: float = 2.0, max_lag_days: int = 3) -> list[Signal]:
    if len(bars) == 0:
        return []
    pos = segment_pos(bars.t)
    c = bars.c
    open_day = bars.t // DAY_MS
    close_day = (bars.t + bars.bar_ms) // DAY_MS
    m_open, m_close = month_of_day(open_day), month_of_day(close_day)
    # the exit: the close of the next month's last day = 00:00 UTC of the 1st of the month after it
    exit_ms = month_start_day(m_close + 1) * DAY_MS
    sd = sigma_d(c, int(vol_n), pos)
    look_ms = int(lookback_days) * DAY_MS
    out: list[Signal] = []
    for i in range(c.size):
        if m_close[i] == m_open[i]:
            continue  # not the last daily bar of a month
        j = int(np.searchsorted(bars.t, int(bars.t[i]) - look_ms))
        if j >= i or int(bars.t[j]) - (int(bars.t[i]) - look_ms) > int(max_lag_days) * DAY_MS or pos[i] < i - j:
            continue  # no bar opening 365 days back (within the lag) in the same segment
        si = float(sd[i])
        if not (math.isfinite(si) and si > 0):
            continue
        e = float(c[i])
        ret = e / float(c[j]) - 1.0
        if ret == 0.0:
            continue
        t_ms = bars.close_time(i)
        hold_ms = int(exit_ms[i]) - t_ms
        d = float(stop_sigma) * si * math.sqrt(hold_ms / DAY_MS) * e
        if hold_ms <= 0 or d >= e:
            continue  # a 2-sigma move over the hold beyond 100 %: no meaningful long stop, skipped on both sides
        if ret > 0:
            out.append(Signal(t_ms, "long", e, e - d, None, hold_ms))
        else:
            out.append(Signal(t_ms, "short", e, e + d, None, hold_ms))
    return out
