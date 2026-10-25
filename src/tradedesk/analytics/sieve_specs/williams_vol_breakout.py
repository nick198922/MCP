"""Larry Williams volatility breakout (L. Williams, "Long-Term Secrets to Short-Term Trading", 1999): buy when the
price rises above today's open + k x yesterday's range (high - low), sell short below today's open - k x yesterday's
range, k = 0.5, exit on the next day's open.

Crypto has no session, so the day is the UTC day: the open is the 00:00 UTC open, yesterday is the previous UTC day,
which must be complete (24 hourly bars). The source enters with a stop order inside the day; the sieve enters at a bar
close, so the entry is the close of the first 1h bar of the day that closes beyond a level (at most one signal a day,
the hour closing at 24:00 cannot signal). Exit by time at the next day's open (00:00 UTC). The stop is the opposite
breakout level (the stop-and-reverse point of the symmetric system). Parameters registered in IDEA-20261017-setup-hunt
before the run; no variants."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars
from tradedesk.analytics.strategy_sieve import Signal

NAME = "williams_vol_breakout"
SOURCE = "L. Williams, Long-Term Secrets to Short-Term Trading (1999): open +- 0.5 x yesterday's range, exit next open"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"k": 0.5}


def day_levels(bars: Bars, k: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Per UTC day present in ``bars`` (1h): (first bar index, end index, lower level, upper level). The levels are NaN
    when the day's first bar is not the 00:00 hour or the previous UTC day is missing or incomplete."""
    n = len(bars)
    if n == 0:
        z = np.zeros(0, dtype=np.int64)
        return z, z, np.zeros(0), np.zeros(0)
    day = bars.t // DAY_MS
    starts = np.flatnonzero(np.concatenate([[True], day[1:] != day[:-1]]))
    ends = np.concatenate([starts[1:], [n]])
    cnt = ends - starts
    dh = np.maximum.reduceat(bars.h, starts)
    dl = np.minimum.reduceat(bars.low, starts)
    dday = day[starts]
    first_ok = bars.t[starts] == dday * DAY_MS
    prev_ok = np.concatenate([[False], (np.diff(dday) == 1) & (cnt[:-1] == DAY_MS // HOUR_MS)])
    rng = np.concatenate([[np.nan], dh[:-1] - dl[:-1]])
    ok = first_ok & prev_ok & (rng > 0)
    o = bars.o[starts]
    lower = np.where(ok, o - k * rng, np.nan)
    upper = np.where(ok, o + k * rng, np.nan)
    return starts, ends, lower, upper


def signals(bars: Bars, k: float = 0.5) -> list[Signal]:
    if bars.bar_ms != HOUR_MS:
        return []
    starts, ends, lower, upper = day_levels(bars, float(k))
    out: list[Signal] = []
    for g in range(starts.size):
        lo, up = float(lower[g]), float(upper[g])
        if not (np.isfinite(lo) and np.isfinite(up) and lo > 0):
            continue
        a, b = int(starts[g]), int(ends[g])
        seg = bars.c[a:b]
        hit = np.flatnonzero((seg > up) | (seg < lo))
        if hit.size == 0:
            continue
        i = a + int(hit[0])
        t_ms = bars.close_time(i)
        hold_ms = (int(bars.t[a]) // DAY_MS + 1) * DAY_MS - t_ms
        if hold_ms <= 0:
            continue
        e = float(bars.c[i])
        if e > up:
            out.append(Signal(t_ms, "long", e, lo, None, hold_ms))
        else:
            out.append(Signal(t_ms, "short", e, up, None, hold_ms))
    return out
