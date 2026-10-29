"""Dual Thrust (M. Chalek; QuantConnect research "Dual Thrust trading algorithm", intraday on 1h bars).

Range = max(HH - LC, HC - LL) over the last N = 4 daily bars (HH highest high, LC lowest close, HC highest close, LL
lowest low). Upper trigger = today's open + K1 x range, lower trigger = today's open - K2 x range, K1 = K2 = 0.5.
Price at or above the upper trigger: long; below the lower trigger: short (a stop-and-reverse system).

Crypto has no session: the day is the UTC day, today's open is the 00:00 UTC open, the 4 previous UTC days must be
complete (24 hourly bars each, consecutive) and today's bar is not part of the range (the page does not say whether it
is excluded; excluding it is the causal reading). Same day mechanics as ``williams_vol_breakout``: entry at the close
of the first 1h bar of the day that closes beyond a trigger (one signal a day; the bar closing at 24:00 cannot signal), stop = the
opposite trigger (the source reverses there).

Defaults added by the harvester (fixed before any run, no variants): the source holds until the opposite trigger and
has no time exit; the sieve cannot reverse, so the position is closed at the next 00:00 UTC open if the stop is not
hit. The page notes tunable K1 and K2 and N = 4 is its demonstration value; no optimisation."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars
from tradedesk.analytics.strategy_sieve import Signal

NAME = "dual_thrust"
SOURCE = "M. Chalek, Dual Thrust; N=4, K1=K2=0.5 (https://www.quantconnect.com/research/15258/dual-thrust-trading-algorithm/)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"n_days": 4, "k1": 0.5, "k2": 0.5}


def signals(bars: Bars, n_days: int = 4, k1: float = 0.5, k2: float = 0.5) -> list[Signal]:
    if bars.bar_ms != HOUR_MS or len(bars) == 0:
        return []
    nd = int(n_days)
    day = bars.t // DAY_MS
    starts = np.flatnonzero(np.concatenate([[True], day[1:] != day[:-1]]))
    ends = np.concatenate([starts[1:], [len(bars)]])
    cnt = ends - starts
    dday = day[starts]
    dh = np.maximum.reduceat(bars.h, starts)
    dl = np.minimum.reduceat(bars.low, starts)
    dhc = np.maximum.reduceat(bars.c, starts)
    dlc = np.minimum.reduceat(bars.c, starts)
    full = cnt == DAY_MS // HOUR_MS
    out: list[Signal] = []
    for g in range(nd, starts.size):
        a, b = int(starts[g]), int(ends[g])
        if bars.t[a] != dday[g] * DAY_MS:
            continue
        w = slice(g - nd, g)
        if not (full[w].all() and dday[g] - dday[g - nd] == nd):
            continue
        rng = max(float(dh[w].max() - dlc[w].min()), float(dhc[w].max() - dl[w].min()))
        if not rng > 0:
            continue
        o = float(bars.o[a])
        up, lo = o + k1 * rng, o - k2 * rng
        if lo <= 0:
            continue
        seg = bars.c[a:b]
        hit = np.flatnonzero((seg >= up) | (seg < lo))
        if hit.size == 0:
            continue
        i = a + int(hit[0])
        t_ms = bars.close_time(i)
        hold_ms = (int(dday[g]) + 1) * DAY_MS - t_ms
        if hold_ms <= 0:
            continue
        e = float(bars.c[i])
        if e >= up:
            out.append(Signal(t_ms, "long", e, lo, None, hold_ms))
        else:
            out.append(Signal(t_ms, "short", e, up, None, hold_ms))
    return out
