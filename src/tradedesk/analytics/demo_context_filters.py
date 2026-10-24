"""Context filters for the demo trader's setups (IDEA-20261023-demo-context-filters). Pure functions, no I/O.

Every flag is causal: the value at bar ``i`` (or at a time ``t``) uses bars whose close is at or before it only.

- H1 ``btc_block_flags``: BTC made a new low (high) of the last ``n`` bars on the bar or on one of the ``lag`` bars
  before it; an alt long (short) decided at that bar close is skipped.
- H2 ``cluster_sizes`` / ``cap_per_group``: at most ``cap`` same-direction entries decided at one bar close, the rest
  dropped by the live score, ties by reward/risk.
- H3 ``repeat_flags``: a sweep event preceded by another sweep event of the same side within the last ``m`` bars.
- H4 ``trend_dirs`` / ``score_points``: the live confidence score of ``setup_signals.score`` for every bar at once.
- ``cluster_boot_group_sums``: a bootstrap over clusters (entry dates) of the per-group sums and counts, so that any
  contrast of group means (kept minus rejected, capped minus all) keeps the dependence between trades of one date.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from .course_s14_ema import Bars, ema, segment_pos


def new_extreme_flags(low: np.ndarray, high: np.ndarray, n: int, pos: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """``new_low[i]``: low[i] is strictly below the lowest low of bars i-n .. i-1; ``new_high`` mirrored. False while the
    bar has fewer than ``n`` earlier bars in its segment (``pos``, see ``segment_pos``)."""
    k = low.size
    nl = np.zeros(k, dtype=bool)
    nh = np.zeros(k, dtype=bool)
    if n < 1 or k <= n:
        return nl, nh
    w_lo = np.lib.stride_tricks.sliding_window_view(low, n)[:-1].min(axis=1)  # min of low[i-n:i] for i = n .. k-1
    w_hi = np.lib.stride_tricks.sliding_window_view(high, n)[:-1].max(axis=1)
    nl[n:] = low[n:] < w_lo
    nh[n:] = high[n:] > w_hi
    if pos is not None:
        ok = pos >= n
        nl &= ok
        nh &= ok
    return nl, nh


def _any_recent(flag: np.ndarray, lag: int) -> np.ndarray:
    """out[i] = any(flag[i-lag .. i])."""
    out = flag.copy()
    for d in range(1, lag + 1):
        out[d:] |= flag[:-d]
    return out


def btc_block_flags(btc: Bars, n: int, lag: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """(block_long, block_short) per BTC bar: a new ``n``-bar low (high) on the bar or one of the ``lag`` bars before."""
    pos = segment_pos(btc.t)
    nl, nh = new_extreme_flags(btc.low, btc.h, n, pos)
    return _any_recent(nl, lag), _any_recent(nh, lag)


def bar_at_close(b: Bars, t_ms: int) -> int:
    """Index of the bar of ``b`` whose close time equals ``t_ms``, else -1."""
    i = int(np.searchsorted(b.t, t_ms - b.bar_ms))
    return i if i < len(b) and int(b.t[i]) + b.bar_ms == t_ms else -1


def last_closed(b: Bars, t_ms: int) -> int:
    """Index of the last bar of ``b`` closed at or before ``t_ms``, else -1."""
    return int(np.searchsorted(b.t + b.bar_ms, t_ms, side="right")) - 1


def trend_dirs(b: Bars) -> np.ndarray:
    """``setup_signals.trend_dir`` on every prefix b[: i + 1] at once: +1 close above EMA34 and EMA21 rising, -1 the
    mirror, 0 otherwise or with fewer than 60 bars."""
    n = len(b)
    out = np.zeros(n, dtype=np.int64)
    if n < 2:
        return out
    pos = segment_pos(b.t)
    e21, e34 = ema(b.c, 21, pos), ema(b.c, 34, pos)
    prev21 = np.concatenate([[np.nan], e21[:-1]])
    ok = np.isfinite(e21) & np.isfinite(prev21) & np.isfinite(e34) & (np.arange(n) >= 59)
    with np.errstate(invalid="ignore"):
        up = ok & (b.c > e34) & (e21 > prev21)
        dn = ok & (b.c < e34) & (e21 < prev21)
    out[up] = 1
    out[dn] = -1
    return out


def score_points(coin_trend: int, btc_trend: int, side: str, rr_after_fees: float, is_btc: bool) -> int:
    """Points of ``setup_signals.score``: coin daily trend agrees, BTC daily trend agrees (not for BTC itself),
    reward/risk after fees >= 2, and >= 3."""
    want = 1 if side == "long" else -1
    pts = int(coin_trend == want)
    if not is_btc:
        pts += int(btc_trend == want)
    pts += int(rr_after_fees >= 2) + int(rr_after_fees >= 3)
    return pts


def repeat_flags(event_bars: Sequence[int] | np.ndarray, m: int) -> np.ndarray:
    """For sorted event bar indices, True where another event lies in [bar - m, bar - 1] (an earlier sweep of the same
    edge side within ``m`` bars). Events on the same bar do not count as earlier."""
    e = np.asarray(event_bars, dtype=np.int64)
    if e.size == 0:
        return np.zeros(0, dtype=bool)
    first = np.searchsorted(e, e - m, side="left")  # first event at or after bar - m
    before = np.searchsorted(e, e, side="left")  # events strictly before this bar
    return before > first


def cluster_sizes(keys: Sequence[Any]) -> np.ndarray:
    """Number of rows that share each row's key (e.g. (entry time, side))."""
    cnt: dict[Any, int] = {}
    for k in keys:
        cnt[k] = cnt.get(k, 0) + 1
    return np.asarray([cnt[k] for k in keys], dtype=np.int64)


def cap_per_group(keys: Sequence[Any], score: Sequence[float], rr: Sequence[float], cap: int) -> np.ndarray:
    """Keep mask: within each key the ``cap`` rows with the highest score, ties by the higher reward/risk, then by the
    earlier row (deterministic)."""
    groups: dict[Any, list[int]] = {}
    for i, k in enumerate(keys):
        groups.setdefault(k, []).append(i)
    keep = np.zeros(len(keys), dtype=bool)
    for idx in groups.values():
        idx.sort(key=lambda i: (-score[i], -rr[i], i))
        keep[idx[:cap]] = True
    return keep


def daily_sums(values: np.ndarray, days: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(sorted unique days, sum of values per day)."""
    u, inv = np.unique(days, return_inverse=True)
    return u, np.bincount(inv, weights=values, minlength=u.size)


def cluster_boot_group_sums(
    values: np.ndarray,
    groups: np.ndarray,
    clusters: np.ndarray,
    n_groups: int,
    *,
    n_boot: int = 10_000,
    seed: int = 20261023,
    chunk: int = 500,
) -> tuple[np.ndarray, np.ndarray]:
    """Bootstrap of whole clusters with replacement; returns (sums, counts), each (n_boot, n_groups): the sum of values
    and the number of rows of each group in each replicate. Rows with a group outside 0 .. n_groups-1 are ignored, but
    their clusters still take part in the draw."""
    v = np.asarray(values, dtype=float)
    g = np.asarray(groups, dtype=np.int64)
    uc, ci = np.unique(np.asarray(clusters), return_inverse=True)
    d = uc.size
    ok = (g >= 0) & (g < n_groups)
    s = np.zeros((d, n_groups))
    c = np.zeros((d, n_groups))
    np.add.at(s, (ci[ok], g[ok]), v[ok])
    np.add.at(c, (ci[ok], g[ok]), 1.0)
    rng = np.random.default_rng(seed)
    out_s = np.empty((n_boot, n_groups))
    out_c = np.empty((n_boot, n_groups))
    for a in range(0, n_boot, chunk):
        k = min(chunk, n_boot - a)
        idx = rng.integers(0, d, size=(k, d))
        w = np.zeros((k, d))
        np.add.at(w, (np.repeat(np.arange(k), d), idx.ravel()), 1.0)
        out_s[a : a + k] = w @ s
        out_c[a : a + k] = w @ c
    return out_s, out_c


def boot_p_two_sided(b: np.ndarray) -> float:
    """Two-sided bootstrap p-value of 'the statistic is 0' from its bootstrap distribution (+1 correction)."""
    b = b[np.isfinite(b)]
    if b.size == 0:
        return float("nan")
    lo = (np.sum(b <= 0) + 1) / (b.size + 1)
    hi = (np.sum(b >= 0) + 1) / (b.size + 1)
    return float(min(1.0, 2 * min(lo, hi)))
