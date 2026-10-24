"""Bounded DCA / martingale bot (IDEA-20261024-dca-martingale-bots-sieve, step 1): buy after a 24 h fall (or short
after a 24 h rise), take a small profit, stop at the depth of the last safety order. The sieve trades one position per
symbol and never adds to it, so this measures a bot that has only its first order and a hard total stop, not a bot
that averages down and waits (its liquidation is hidden by the stop).

Rules on closed 1h bars, fixed before the first run (the critic's clarification 5):

- m_i = c_i / c_j - 1 (the 24 h move, decimal), j = the bar that opened exactly ``lookback_h`` hours before bar i;
  undefined (NaN) when that bar is missing or lies before a data gap > 48 h (``course_s1_spot.segments``).
- condition at bar i: m_i <= -move for ``direction`` = 1 (long after a fall), m_i >= +move for ``direction`` = -1
  (short after a rise).
- a signal only on the first crossing: the condition is true at bar i, the move is defined at bar i - 1 and the
  condition was false there (no repeat every hour while the move stays beyond the threshold; the next signal needs the
  move to come back inside the threshold first).
- entry = close of bar i; target = entry x (1 + direction x target), stop = entry x (1 - direction x stop), both as
  fractions of the entry; time exit after ``hold_h`` hours.

Registered variants (one registry row each): S-DCA-1 = defaults (long, target 1.5 %, stop 12 %), S-DCA-2 =
``stop=0.06``, S-DCA-3 = ``direction=-1`` (short mirror).
"""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s1_spot import seg_start, segments
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars
from tradedesk.analytics.strategy_sieve import Signal

NAME = "dca_martingale"
SOURCE = "docs/ideas/IDEA-20261024-dca-martingale-bots-sieve.md (Bybit DCA / martingale bot, first order + total stop)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {
    "direction": 1,
    "move": 0.04,
    "lookback_h": 24,
    "target": 0.015,
    "stop": 0.12,
    "hold_h": 168,
}


def move_24h(bars: Bars, lookback_h: int = 24) -> np.ndarray:
    """c_i / c_j - 1 where bar j opened exactly ``lookback_h`` hours before bar i and lies in the same segment; NaN
    when that bar is missing (a hole in the hourly data) or in an earlier segment."""
    c = np.asarray(bars.c, dtype=float)
    t = np.asarray(bars.t, dtype=np.int64)
    n = c.size
    out = np.full(n, np.nan)
    if n == 0:
        return out
    want = t - int(lookback_h) * HOUR_MS
    j = np.searchsorted(t, want)
    jc = np.minimum(j, n - 1)
    start = seg_start(segments(t))
    ok = (j < n) & (t[jc] == want) & (jc >= start)
    idx = np.flatnonzero(ok)
    out[idx] = c[idx] / c[jc[idx]] - 1.0
    return out


def signals(
    bars: Bars,
    direction: int = 1,
    move: float = 0.04,
    lookback_h: int = 24,
    target: float = 0.015,
    stop: float = 0.12,
    hold_h: int = 168,
) -> list[Signal]:
    n = len(bars)
    d = int(direction)
    if n == 0 or d not in (1, -1) or not (0 < target and 0 < stop < 1 and move > 0):
        return []
    m = move_24h(bars, lookback_h)
    defined = np.isfinite(m)
    with np.errstate(invalid="ignore"):
        cond = defined & (m <= -move) if d == 1 else defined & (m >= move)
    prev_defined = np.concatenate([[False], defined[:-1]])
    prev_cond = np.concatenate([[False], cond[:-1]])
    hits = np.flatnonzero(cond & prev_defined & ~prev_cond)
    side = "long" if d == 1 else "short"
    hold_ms = int(hold_h) * HOUR_MS
    out: list[Signal] = []
    for i in hits:
        e = float(bars.c[i])
        out.append(Signal(bars.close_time(int(i)), side, e, e * (1 - d * stop), e * (1 + d * target), hold_ms))
    return out
