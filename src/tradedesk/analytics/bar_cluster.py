"""Signals of one bar as one bet: accounting helpers for clusters of trades entered on the same signal bar.

Pure functions, no I/O. Times are epoch milliseconds passed in by the caller. R values are multiples of the trade's
risk (decimal, after fees). Used by ``scripts/verify_s39_bar_cluster.py`` (IDEA-20261030-demo-setups-depth, item
«S39, сигнали однієї свічки як одна ставка»).

Every function is causal in the sense the tests pin: a value attached to a trade or a week before time t depends only
on trades entered before t (a bar's size counts only the trades of that same entry time).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from typing import Any

import numpy as np

Z_975 = 1.959963984540054
Z_80 = 0.8416212335729143


def bar_sizes(t_in: Sequence[int]) -> np.ndarray:
    """For each trade, the number of trades in the sample entered at the same time (its signal bar's cluster size)."""
    t = np.asarray(t_in, dtype=np.int64)
    if t.size == 0:
        return np.zeros(0, dtype=np.int64)
    _, inv, cnt = np.unique(t, return_inverse=True, return_counts=True)
    return cnt[inv].astype(np.int64)


def size_group(sizes: Sequence[int], top: int = 3) -> np.ndarray:
    """Group label per trade: the cluster size, with every size >= ``top`` pooled into ``top`` (1, 2, >= 3 by default)."""
    s = np.asarray(sizes, dtype=np.int64)
    if s.size and s.min() < 1:
        raise ValueError("cluster sizes start at 1")
    return np.minimum(s, top)


def size_histogram(t_in: Sequence[int]) -> dict[int, int]:
    """Number of bars (distinct entry times) by how many trades entered on them."""
    t = np.asarray(t_in, dtype=np.int64)
    if t.size == 0:
        return {}
    _, cnt = np.unique(t, return_counts=True)
    return dict(sorted(Counter(int(c) for c in cnt).items()))


def week_buckets(t_in: Sequence[int], start_ms: int, end_ms: int, week_ms: int) -> np.ndarray:
    """Counts of trades per week bucket [start + k*week, start + (k+1)*week) for every whole or partial week up to
    ``end_ms`` (empty weeks included as zeros); trades outside [start, end) are ignored."""
    if end_ms <= start_ms or week_ms <= 0:
        raise ValueError("need end_ms > start_ms and week_ms > 0")
    t = np.asarray(t_in, dtype=np.int64)
    t = t[(t >= start_ms) & (t < end_ms)]
    n_weeks = int(math.ceil((end_ms - start_ms) / week_ms))
    return np.bincount((t - start_ms) // week_ms, minlength=n_weeks).astype(np.int64)


def held_ms(t_in: Sequence[int], exit_ms: Sequence[int]) -> int:
    """Total position-time (ms) of the trades: sum of exit - entry (negative spans are an input error)."""
    t = np.asarray(t_in, dtype=np.int64)
    x = np.asarray(exit_ms, dtype=np.int64)
    if t.shape != x.shape:
        raise ValueError("t_in and exit_ms differ in length")
    d = x - t
    if d.size and d.min() < 0:
        raise ValueError("exit before entry")
    return int(d.sum())


def chain_change(keep_base: Sequence[bool], keep_alt: Sequence[bool]) -> tuple[np.ndarray, np.ndarray]:
    """Trades the alternative chain removes (kept in base, not in alt) and adds (kept in alt, not in base)."""
    a = np.asarray(keep_base, dtype=bool)
    b = np.asarray(keep_alt, dtype=bool)
    if a.shape != b.shape:
        raise ValueError("masks differ in length")
    return a & ~b, b & ~a


def cap_boundary_ties(t_in: Sequence[int], score: Sequence[float], rr: Sequence[float], cap: int) -> int:
    """Bars with more than ``cap`` trades where the demo's order (higher score, then higher R:R) ties exactly at the
    cap boundary, i.e. the cap-th and (cap+1)-th trade have equal (score, R:R): there a secondary key decides."""
    t = np.asarray(t_in, dtype=np.int64)
    s = np.asarray(score, dtype=float)
    r = np.asarray(rr, dtype=float)
    ties = 0
    for v in np.unique(t):
        m = np.flatnonzero(t == v)
        if m.size <= cap:
            continue
        keys = sorted((-s[i], -r[i]) for i in m)
        if keys[cap - 1] == keys[cap]:
            ties += 1
    return ties


def _groups(clusters: np.ndarray) -> list[np.ndarray]:
    _, inv = np.unique(clusters, return_inverse=True)
    order = np.argsort(inv, kind="stable")
    cuts = np.flatnonzero(np.diff(inv[order])) + 1
    return np.split(order, cuts)


def boot_se_mean(values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261030) -> float:
    """Cluster-bootstrap standard error of the mean of ``values`` (whole clusters resampled). Only the spread is
    returned, never the mean (the pre-registration stage prints SEs for the power gate)."""
    v = np.asarray(values, dtype=float)
    c = np.asarray(clusters)
    if v.size < 2 or np.unique(c).size < 2:
        return math.nan
    groups = _groups(c)
    sums = np.array([v[g].sum() for g in groups])
    cnts = np.array([g.size for g in groups], dtype=float)
    rng = np.random.default_rng(seed)
    k = len(groups)
    out = np.empty(n_boot)
    for i in range(n_boot):
        p = rng.integers(0, k, k)
        out[i] = sums[p].sum() / cnts[p].sum()
    return float(out.std())


def boot_se_diff(
    values: Sequence[float],
    in_a: Sequence[bool],
    in_b: Sequence[bool],
    clusters: Sequence[Any],
    *,
    n_boot: int = 10_000,
    seed: int = 20261030,
) -> float:
    """Cluster-bootstrap standard error of mean(values[a]) - mean(values[b]); the masks may overlap (a paired contrast
    such as capped vs uncapped chain: both means are taken on the same resampled clusters). Only the spread is
    returned. Shift-invariant: adding a constant to every value leaves it unchanged."""
    v = np.asarray(values, dtype=float)
    a = np.asarray(in_a, dtype=bool)
    b = np.asarray(in_b, dtype=bool)
    keep = a | b
    v, a, b, c = v[keep], a[keep], b[keep], np.asarray(clusters)[keep]
    if a.sum() < 2 or b.sum() < 2 or np.unique(c).size < 2:
        return math.nan
    groups = _groups(c)
    sa = np.array([v[g][a[g]].sum() for g in groups])
    na = np.array([a[g].sum() for g in groups], dtype=float)
    sb = np.array([v[g][b[g]].sum() for g in groups])
    nb = np.array([b[g].sum() for g in groups], dtype=float)
    rng = np.random.default_rng(seed)
    k = len(groups)
    out = np.empty(n_boot)
    for i in range(n_boot):
        p = rng.integers(0, k, k)
        x, y = na[p].sum(), nb[p].sum()
        out[i] = sa[p].sum() / x - sb[p].sum() / y if x > 0 and y > 0 else math.nan
    out = out[np.isfinite(out)]
    return float(out.std()) if out.size > 1 else math.nan


def mde80(se: float) -> float:
    """Minimum detectable effect at 80 % power, two-sided 5 % test: (z_0.975 + z_0.80) * SE."""
    return (Z_975 + Z_80) * se if math.isfinite(se) else math.nan


def design_effect(se_clustered: float, sd: float, n: int) -> float:
    """(clustered SE / i.i.d. SE)^2 with i.i.d. SE = sd / sqrt(n); n / deff is the effective number of trades."""
    if n < 2 or not (math.isfinite(se_clustered) and math.isfinite(sd)) or sd <= 0:
        return math.nan
    return (se_clustered / (sd / math.sqrt(n))) ** 2
