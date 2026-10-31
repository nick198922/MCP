"""Item 10c of IDEA-20261030-demo-setups-depth: is the "fresh range edge" of S3 (10a) a feature of its own or a proxy
for the stop distance, the reward/risk, the age of the edge and the width of the range?

Pure functions (no I/O): the age of a range edge, within-group quantile codes, a stratified fresh-minus-far contrast
with a cluster bootstrap, and a design matrix of dummies for an OLS adjustment (fitted with
``spot_perp_flow.ols_cluster_cov``). R values are in units of the trade's risk; nothing here knows about time except
through the bar index passed in.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np


def edge_age(low: np.ndarray, s: int, w: int) -> int:
    """Bars from the latest bar of the window ``low[s - w : s]`` whose low equals the window minimum (the range edge)
    to the sweep bar ``s``: 1 if the edge was set on bar s - 1, w if only on bar s - w. Uses bars < s only."""
    if w < 1 or s < w or s > len(low):
        raise ValueError("need w >= 1 and w <= s <= len(low)")
    win = np.asarray(low[s - w : s], dtype=float)
    j = int(np.flatnonzero(win == win.min())[-1])
    return w - j


def quantile_codes(x: Sequence[float], groups: Sequence[Any], q: int = 5) -> np.ndarray:
    """Quantile bin (0 .. q - 1) of every x within its group; cut points are the group's (k / q)-quantiles, ties go to
    the upper bin (so heavily tied values may leave some bins empty). NaN gets -1."""
    xa = np.asarray(x, dtype=float)
    ga = np.asarray(groups)
    out = np.full(xa.size, -1, dtype=np.int64)
    for g in np.unique(ga):
        m = (ga == g) & np.isfinite(xa)
        if not m.any():
            continue
        cuts = np.quantile(xa[m], np.arange(1, q) / q)
        out[m] = np.searchsorted(cuts, xa[m], side="right")
    return out


def _cell_table(values: np.ndarray, fresh: np.ndarray, far: np.ndarray, strata: np.ndarray, cl: np.ndarray) -> tuple[np.ndarray, int]:
    """Per cluster x stratum: [sum fresh, n fresh, sum far, n far] -> array (k clusters, S strata, 4)."""
    _, si = np.unique(strata, return_inverse=True)
    _, ci = np.unique(cl, return_inverse=True)
    k, n_s = int(ci.max()) + 1, int(si.max()) + 1
    t = np.zeros((k, n_s, 4))
    np.add.at(t, (ci[fresh], si[fresh], 0), values[fresh])
    np.add.at(t, (ci[fresh], si[fresh], 1), 1.0)
    np.add.at(t, (ci[far], si[far], 2), values[far])
    np.add.at(t, (ci[far], si[far], 3), 1.0)
    return t, k


def _weighted_delta(cells: np.ndarray) -> float:
    """cells (S, 4) summed over clusters -> sum_s w_s (mean_f - mean_r) / sum_s w_s, w_s = n_f n_r / (n_f + n_r)."""
    sf, nf, sr, nr = cells[:, 0], cells[:, 1], cells[:, 2], cells[:, 3]
    ok = (nf > 0) & (nr > 0)
    if not ok.any():
        return math.nan
    w = nf[ok] * nr[ok] / (nf[ok] + nr[ok])
    d = sf[ok] / nf[ok] - sr[ok] / nr[ok]
    return float(np.sum(w * d) / np.sum(w))


def stratified_diff(values: Sequence[float], fresh: Sequence[bool], far: Sequence[bool], strata: Sequence[Any]) -> float:
    """Fresh-minus-far difference of means within each stratum, averaged with weights n_f n_r / (n_f + n_r) (strata
    lacking either class drop out). One stratum gives the plain difference of means."""
    v = np.asarray(values, dtype=float)
    f, r = np.asarray(fresh, dtype=bool), np.asarray(far, dtype=bool)
    t, _ = _cell_table(v, f, r, np.asarray(strata), np.zeros(v.size, dtype=np.int64))
    return _weighted_delta(t.sum(axis=0))


def stratified_diff_ci(
    values: Sequence[float],
    fresh: Sequence[bool],
    far: Sequence[bool],
    strata: Sequence[Any],
    clusters: Sequence[Any],
    *,
    n_boot: int = 10_000,
    seed: int = 20261030,
) -> dict[str, float]:
    """``stratified_diff`` with a 95 % CI, bootstrap SE and two-sided p (H0: 0), resampling whole clusters."""
    v = np.asarray(values, dtype=float)
    f, r = np.asarray(fresh, dtype=bool), np.asarray(far, dtype=bool)
    keep = (f | r) & np.isfinite(v)
    v, f, r = v[keep], f[keep], r[keep]
    st, cl = np.asarray(strata)[keep], np.asarray(clusters)[keep]
    if f.sum() < 2 or r.sum() < 2:
        return {"d": math.nan, "lo": math.nan, "hi": math.nan, "p": math.nan, "se": math.nan}
    t, k = _cell_table(v, f, r, st, cl)
    d = _weighted_delta(t.sum(axis=0))
    rng = np.random.default_rng(seed)
    flat = t.reshape(k, -1)
    out = np.empty(n_boot)
    for b in range(n_boot):
        cnt = np.bincount(rng.integers(0, k, k), minlength=k).astype(float)
        out[b] = _weighted_delta((cnt @ flat).reshape(t.shape[1], 4))
    out = out[np.isfinite(out)]
    p = min(1.0, 2 * min((np.sum(out <= 0) + 1) / (out.size + 1), (np.sum(out >= 0) + 1) / (out.size + 1)))
    return {
        "d": d,
        "lo": float(np.quantile(out, 0.025)),
        "hi": float(np.quantile(out, 0.975)),
        "p": float(p),
        "se": float(out.std()),
    }


def dummies(codes: Sequence[Any]) -> np.ndarray:
    """0/1 columns for every level of ``codes`` but the first (sorted); levels absent from the data give no column."""
    ca = np.asarray(codes)
    lev = np.unique(ca)
    return (ca[:, None] == lev[None, 1:]).astype(float)


def nested_dummies(groups: Sequence[Any], codes: Sequence[Any]) -> np.ndarray:
    """0/1 columns for every (group, code) but the lowest code present in each group: quantile bins within a timeframe
    whose group main effect is carried by other columns (no collinearity with the group dummies)."""
    ga, ca = np.asarray(groups), np.asarray(codes)
    cols: list[np.ndarray] = []
    for g in np.unique(ga):
        m = ga == g
        for c in np.unique(ca[m])[1:]:
            cols.append((m & (ca == c)).astype(float))
    return np.column_stack(cols) if cols else np.zeros((ga.size, 0))


def design(
    fresh: Sequence[bool], factors: Sequence[Sequence[Any]], nested: Sequence[tuple[Sequence[Any], Sequence[Any]]] = ()
) -> np.ndarray:
    """[1, fresh, dummies(factor 1), ..., nested_dummies(group, code) ...]; the coefficient of interest is column 1."""
    f = np.asarray(fresh, dtype=float)
    cols = [np.ones(f.size), f] + [dummies(c) for c in factors] + [nested_dummies(g, c) for g, c in nested]
    return np.column_stack(cols)
