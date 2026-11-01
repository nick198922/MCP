"""Modules of setup S17 (retest of an impulsively broken POB, 1d) for item 8 / 7b of IDEA-20261030-demo-setups-depth.

Registered in the card section «Попередня реєстрація модулів S17» before the first run (critic A took H1, H5, H6):

- H1 ``trough_depth``: depth of the drop behind the POB in ATR14 of the POB, the minimum taken over the impulse bar q
  up to the bar before the break k (no bar at or after the break's close is read);
- H5 ``reclaim_entry``: instead of the resting limit, a market entry at the close of the first 1h bar after the touch
  that closes back at or beyond the level (within 24 hours, no stop touched first); ``taker_entry_cost`` is the extra
  fee of that market entry in R;
- H6 is the passport's "placed one bar later" limit (``setup_passport.limit_trade`` with a shifted window), no code;
- H8 / H10 diagnostics: ``target_before_fill`` and ``cluster_weights``.

Pure functions, no I/O. Bars are ``course_s14_ema.Bars`` in the long price space (shorts are mirrored by the caller).
R is in units of the trade's risk (``stop_management.risk_unit``), not of capital; fees are decimal fractions.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.stop_management import Fees, risk_unit

__all__ = [
    "cluster_weights",
    "first_reach",
    "link_parents",
    "max_drawdown",
    "parent_uid",
    "reclaim_entry",
    "taker_entry_cost",
    "target_before_fill",
    "trough_depth",
]

RECLAIM_WINDOW_MS = 24 * 3_600_000


def trough_depth(b: Bars, atr: np.ndarray, p: int, q: int, k: int) -> tuple[float, int]:
    """(depth, bars from p to the trough): (close[p] - min(low[q .. k-1])) / ATR[p] of ``b`` (long frame).

    Reads bars p and q .. k-1 only (the trough is known before the break bar k closes). NaN if ATR[p] is not positive
    or the range is empty."""
    a = float(atr[p])
    if not (k > q >= 0 and math.isfinite(a) and a > 0):
        return math.nan, -1
    lows = b.low[q:k]
    j = int(np.argmin(lows))
    return (float(b.c[p]) - float(lows[j])) / a, q + j - p


def reclaim_entry(path: Bars, k_fill: int, level: float, stop: float, window_ms: int = RECLAIM_WINDOW_MS) -> tuple[int, float] | None:
    """H5: the first 1h bar m of ``path`` (long frame) from the fill bar ``k_fill`` with an open before
    open[k_fill] + ``window_ms`` that closes at or above ``level``; (index m, close of m) or None.

    None also if any bar k_fill .. m traded at or below ``stop`` (the setup failed before the reclaim; a bar that
    touches the stop and closes back above the level counts as failed too). Bars after m are never read."""
    t_end = int(path.t[k_fill]) + window_ms
    for m in range(k_fill, len(path)):
        if int(path.t[m]) >= t_end:
            return None
        if float(path.low[m]) <= stop:
            return None
        c = float(path.c[m])
        if c >= level:
            return m, c
    return None


def taker_entry_cost(entry: float, stop: float, fees: Fees) -> float:
    """Extra cost in R of a market entry over the maker entry that ``simulate_stop_rule`` charges:
    (taker_exit - maker) * |entry| / risk_unit(entry, stop)."""
    return (fees.taker_exit - fees.maker) * abs(entry) / risk_unit(entry, stop, fees)


def target_before_fill(path: Bars, t0: int, t_fill: int, target: float) -> bool:
    """H8: did a 1h bar opening in [t0, t_fill) reach ``target`` (high >= target, long frame) before the fill bar?"""
    i0, i1 = int(np.searchsorted(path.t, t0)), int(np.searchsorted(path.t, t_fill))
    return bool(i1 > i0 and float(np.max(path.h[i0:i1])) >= target)


def first_reach(path: Bars, t0: int, t1: int, target: float) -> int:
    """H8: open time of the first 1h bar opening in [t0, t1) whose high reaches ``target`` (long frame); -1 if none.
    Bars outside [t0, t1) are never read."""
    i0, i1 = int(np.searchsorted(path.t, t0)), int(np.searchsorted(path.t, t1))
    hit = np.flatnonzero(path.h[i0:i1] >= target)
    return int(path.t[i0 + int(hit[0])]) if hit.size else -1


def cluster_weights(side: Sequence[str], day: Sequence[int]) -> np.ndarray:
    """H10: risk weight 1 / sqrt(k), k = number of placements of the same side on the same UTC day."""
    keys = list(zip(side, day, strict=True))
    cnt = Counter(keys)
    return np.array([1.0 / math.sqrt(cnt[x]) for x in keys])


def parent_uid(key: str, stop_pct: float, digits: int = 9) -> str:
    """Identifier of an S17 signal: ``key`` (sym|tf|side|t0) plus the stop distance rounded to ``digits``.

    One break bar often breaks several POBs, so ``key`` alone is shared by siblings. A placebo copies its parent's
    geometry scaled to another price (entry and stop times c_r / c_sig), so its stop_pct equals the parent's and the
    pair (pkey, stop_pct) names the parent."""
    return f"{key}|{round(float(stop_pct), digits):.{digits}f}"


def link_parents(parent_ids: Sequence[str], eligible: Sequence[bool], child_ids: Sequence[str]) -> np.ndarray:
    """Index of each child's parent among the ``eligible`` parent rows (e.g. the chained placements); -1 if no
    eligible parent has the child's id or more than one does (an ambiguous link is dropped, never guessed)."""
    first: dict[str, int] = {}
    many: set[str] = set()
    for i, (u, ok) in enumerate(zip(parent_ids, eligible, strict=True)):
        if not ok:
            continue
        if u in first:
            many.add(u)
        else:
            first[u] = i
    return np.array([-1 if u in many else first.get(u, -1) for u in child_ids], dtype=np.int64)


def max_drawdown(r_in_time_order: Sequence[float]) -> float:
    """Largest peak-to-trough fall of the cumulative sum of R (a non-negative number of R)."""
    c = np.cumsum(np.asarray(r_in_time_order, dtype=float))
    if c.size == 0:
        return 0.0
    peak = np.maximum.accumulate(np.concatenate([[0.0], c]))[1:]
    return float(np.max(peak - c))
