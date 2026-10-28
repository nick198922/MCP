"""London breakout of the Asian range (rules from The Right Trader, "London Breakout Strategy"). Asian range: high and
low of the bars opening from 00:00 UTC up to, not including, 07:00 UTC. Entry window 07:00-10:00 UTC: the first close
above the Asian high (long) or below the Asian low (short); one trade per UTC day. Stop at the far side of the range,
target 1R from the entry close (entry +/- stop distance), time exit at 12:00 UTC. Filter: skip the day if the range
height is under 0.25 or over 0.80 times the daily ATR14.

Sieve adaptation: the source works on 15-minute bars; the sieve sees 1h bars, so the range is 7 hourly bars, the window
is the hourly bars opening 07:00, 08:00 and 09:00 (closes 08:00-10:00) and the exit is the 12:00 close. Defaults added by
the harvester: the source does not say which daily ATR; the ATR14 of the previous complete UTC day is used (Wilder, daily
bars resampled from the 1h bars), so the filter uses no data from the signal day. Days missing any of the 7 range hours,
or without a previous-day ATR, are skipped. The source gives no stop beyond the range far side and no win rate."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars, atr, resample, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "london_breakout_asia_range"
SOURCE = "The Right Trader, London Breakout Strategy (https://therighttrader.com/learn/trading-strategies/london-breakout-strategy)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"range_end": 7, "entry_end": 10, "exit_hour": 12, "min_atr": 0.25, "max_atr": 0.80}


def signals(
    bars: Bars, range_end: int = 7, entry_end: int = 10, exit_hour: int = 12, min_atr: float = 0.25, max_atr: float = 0.80
) -> list[Signal]:
    if bars.bar_ms != HOUR_MS or len(bars) == 0:
        return []
    daily = resample(bars, 24)
    datr = atr(daily, 14, segment_pos(daily.t))
    atr_by_day = {int(daily.t[k]) // DAY_MS: float(datr[k]) for k in range(len(daily))}
    day = bars.t // DAY_MS
    starts = np.flatnonzero(np.concatenate([[True], day[1:] != day[:-1]]))
    ends = np.concatenate([starts[1:], [len(bars)]])
    out: list[Signal] = []
    for s, e_ in zip(starts, ends, strict=True):
        d = int(day[s])
        hrs = (bars.t[s:e_] - d * DAY_MS) // HOUR_MS
        rng_idx = np.flatnonzero(hrs < range_end)
        if rng_idx.size != range_end or int(hrs[rng_idx[0]]) != 0 or int(hrs[rng_idx[-1]]) != range_end - 1:
            continue
        hi = float(bars.h[s + rng_idx].max())
        lo = float(bars.low[s + rng_idx].min())
        da = atr_by_day.get(d - 1, math.nan)
        if not (math.isfinite(da) and da > 0 and min_atr * da <= hi - lo <= max_atr * da):
            continue
        for j in np.flatnonzero((hrs >= range_end) & (hrs < entry_end)):
            i = int(s + j)
            c = float(bars.c[i])
            if not (c > hi or c < lo):
                continue
            t_ms = bars.close_time(i)
            hold_ms = d * DAY_MS + exit_hour * HOUR_MS - t_ms
            if hold_ms > 0:
                if c > hi:
                    out.append(Signal(t_ms, "long", c, lo, c + (c - lo), hold_ms))
                else:
                    out.append(Signal(t_ms, "short", c, hi, c - (hi - c), hold_ms))
            break
    return out
