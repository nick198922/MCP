"""Path "efficiency" of the S3 range before the sweep, and the statistics of its registered test.

IDEA-20261030-demo-setups-depth, item «S3, „ефективність“ рейнджу як вага» (registration «Попередня реєстрація — S3,
ефективність рейнджу»). Pure functions, no I/O.

* ``efficiency_ratio``: Kaufman's efficiency ratio of the ``w`` close-to-close changes of bars s - w .. s - 1 (the
  range window of the sweep bar s): |c[s-1] - c[s-w-1]| / sum |c[k] - c[k-1]|. 1 = a straight line, 0 = back where it
  started. Uses closes before bar s only.
* ``variance_ratio``: Lo & MacKinlay (1988) overlapping variance ratio VR(q) of the ``n`` one-bar log returns before
  bar s, with their bias corrections. Below 1 = mean reversion, above 1 = trending.
* Tertiles, Spearman's rho, the cluster-robust SE of a difference of two group means, cluster bootstraps and a
  within-cell permutation placebo. Clusters are integer labels (e.g. the UTC month of the entry).
"""

from __future__ import annotations

import math

import numpy as np

Z_975 = 1.959963984540054
Z_80 = 0.8416212335729143


def _segment_ok(pos: np.ndarray, s: int, k: int) -> bool:
    """Bars s - k .. s - 1 exist and lie in one segment (``segment_pos`` restarts at 0 after a hole)."""
    return s - k >= 0 and s - 1 < pos.size and int(pos[s - 1]) >= k - 1


def efficiency_ratio(c: np.ndarray, pos: np.ndarray, s: int, w: int = 30) -> float:
    """Kaufman ER of the ``w`` close changes ending at bar s - 1 (closes s - w - 1 .. s - 1); NaN if undefined."""
    if not _segment_ok(pos, s, w + 1):
        return math.nan
    x = np.asarray(c[s - w - 1 : s], dtype=float)
    path = float(np.abs(np.diff(x)).sum())
    if not (path > 0 and np.all(np.isfinite(x))):
        return math.nan
    return abs(float(x[-1] - x[0])) / path


def variance_ratio(c: np.ndarray, pos: np.ndarray, s: int, n: int = 120, q: int = 4) -> float:
    """Lo-MacKinlay overlapping VR(q) of the ``n`` log returns of bars s - n .. s - 1 (closes s - n - 1 .. s - 1)."""
    if not _segment_ok(pos, s, n + 1) or n < 2 * q:
        return math.nan
    x = np.asarray(c[s - n - 1 : s], dtype=float)
    if not (np.all(np.isfinite(x)) and np.all(x > 0)):
        return math.nan
    r = np.diff(np.log(x))
    mu = float(r.mean())
    var_a = float(((r - mu) ** 2).sum()) / (n - 1)
    if not var_a > 0:
        return math.nan
    sums = np.convolve(r, np.ones(q), mode="valid")  # n - q + 1 overlapping q-bar sums
    m = q * (n - q + 1) * (1 - q / n)
    var_c = float(((sums - q * mu) ** 2).sum()) / m
    return var_c / var_a


def tertile_cuts(x: np.ndarray) -> tuple[float, float]:
    """The 1/3 and 2/3 quantiles of the finite values of ``x`` (linear interpolation)."""
    v = np.asarray(x, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return math.nan, math.nan
    q1, q2 = np.quantile(v, [1 / 3, 2 / 3])
    return float(q1), float(q2)


def tertile_codes(x: np.ndarray, lo: np.ndarray | float, hi: np.ndarray | float) -> np.ndarray:
    """1 if x <= lo, 3 if x > hi, else 2; 0 where x is not finite. ``lo`` / ``hi`` may be per-row arrays."""
    v = np.asarray(x, dtype=float)
    lo_a = np.broadcast_to(np.asarray(lo, dtype=float), v.shape)
    hi_a = np.broadcast_to(np.asarray(hi, dtype=float), v.shape)
    out = np.where(v <= lo_a, 1, np.where(v > hi_a, 3, 2)).astype(np.int64)
    out[~np.isfinite(v)] = 0
    return out


def rankdata(x: np.ndarray) -> np.ndarray:
    """Ranks 1..n with ties given their average rank."""
    v = np.asarray(x, dtype=float)
    o = np.argsort(v, kind="mergesort")
    sv = v[o]
    r = np.empty(v.size, dtype=float)
    i = 0
    n = v.size
    while i < n:
        j = i
        while j + 1 < n and sv[j + 1] == sv[i]:
            j += 1
        r[o[i : j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman's rho of the pairs where both are finite (Pearson of average ranks)."""
    xa, ya = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    ok = np.isfinite(xa) & np.isfinite(ya)
    if ok.sum() < 3:
        return math.nan
    rx, ry = rankdata(xa[ok]), rankdata(ya[ok])
    rx -= rx.mean()
    ry -= ry.mean()
    den = math.sqrt(float((rx * rx).sum()) * float((ry * ry).sum()))
    return float((rx * ry).sum()) / den if den > 0 else math.nan


def within_percentile(x: np.ndarray, group: np.ndarray) -> np.ndarray:
    """Average rank of x inside its group divided by the group's size (NaN stays NaN)."""
    v = np.asarray(x, dtype=float)
    g = np.asarray(group)
    out = np.full(v.size, math.nan)
    for k in np.unique(g):
        m = (g == k) & np.isfinite(v)
        if m.any():
            out[m] = rankdata(v[m]) / m.sum()
    return out


def cluster_se_diff(y: np.ndarray, a: np.ndarray, b: np.ndarray, cl: np.ndarray) -> float:
    """Cluster-robust SE of mean(y[a]) - mean(y[b]) (Liang-Zeger with the C / (C - 1) correction).

    Each cluster contributes the sum of its residuals from the group means, divided by the group sizes; the two
    groups share the cluster labels, so their covariance inside a cluster is included. Without clustering (one row per
    cluster) this is sqrt(sum e_a^2 / n_a^2 + sum e_b^2 / n_b^2) * sqrt(C / (C - 1))."""
    yv = np.asarray(y, dtype=float)
    am, bm = np.asarray(a, dtype=bool), np.asarray(b, dtype=bool)
    na, nb = int(am.sum()), int(bm.sum())
    if na < 2 or nb < 2:
        return math.nan
    e = np.zeros(yv.size)
    e[am] = (yv[am] - yv[am].mean()) / na
    e[bm] -= (yv[bm] - yv[bm].mean()) / nb
    use = am | bm
    labels, inv = np.unique(np.asarray(cl)[use], return_inverse=True)
    c = labels.size
    if c < 2:
        return math.nan
    per = np.bincount(inv, weights=e[use], minlength=c)
    return math.sqrt(c / (c - 1) * float((per * per).sum()))


def mde80(se: float) -> float:
    """Minimum detectable effect at 80 % power, two-sided 5 %: (z_0.975 + z_0.8) * SE."""
    return (Z_975 + Z_80) * se


def boot_diff(y: np.ndarray, a: np.ndarray, b: np.ndarray, cl: np.ndarray, n_boot: int = 10_000, seed: int = 0) -> dict[str, float]:
    """mean(y[a]) - mean(y[b]) with a 95 % percentile CI from resampling whole clusters (multinomial weights) and the
    one-sided p = share of replicates <= 0 (for H: difference > 0)."""
    yv = np.asarray(y, dtype=float)
    am, bm = np.asarray(a, dtype=bool), np.asarray(b, dtype=bool)
    use = am | bm
    labels, inv = np.unique(np.asarray(cl)[use], return_inverse=True)
    c = labels.size
    ya, ins_a = yv[use], am[use]
    sa = np.bincount(inv, weights=np.where(ins_a, ya, 0.0), minlength=c)
    na = np.bincount(inv, weights=ins_a.astype(float), minlength=c)
    sb = np.bincount(inv, weights=np.where(~ins_a, ya, 0.0), minlength=c)
    nb = np.bincount(inv, weights=(~ins_a).astype(float), minlength=c)
    est = float(sa.sum() / na.sum() - sb.sum() / nb.sum()) if na.sum() and nb.sum() else math.nan
    if c < 2 or not math.isfinite(est):
        return {"est": est, "lo": math.nan, "hi": math.nan, "p": math.nan, "n_a": int(am.sum()), "n_b": int(bm.sum()), "clusters": c}
    rng = np.random.default_rng(seed)
    w = rng.multinomial(c, np.full(c, 1.0 / c), size=n_boot).astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        d = (w @ sa) / (w @ na) - (w @ sb) / (w @ nb)
    d = d[np.isfinite(d)]
    lo, hi = np.quantile(d, [0.025, 0.975])
    return {
        "est": est,
        "lo": float(lo),
        "hi": float(hi),
        "p": float((d <= 0).mean()),
        "n_a": int(am.sum()),
        "n_b": int(bm.sum()),
        "clusters": c,
    }


def boot_mean(y: np.ndarray, cl: np.ndarray, n_boot: int = 10_000, seed: int = 0) -> dict[str, float]:
    """Mean of y with a 95 % cluster-bootstrap percentile CI."""
    yv = np.asarray(y, dtype=float)
    labels, inv = np.unique(np.asarray(cl), return_inverse=True)
    c = labels.size
    s = np.bincount(inv, weights=yv, minlength=c)
    n = np.bincount(inv, minlength=c).astype(float)
    est = float(yv.mean()) if yv.size else math.nan
    if c < 2:
        return {"est": est, "lo": math.nan, "hi": math.nan, "n": int(yv.size)}
    rng = np.random.default_rng(seed)
    w = rng.multinomial(c, np.full(c, 1.0 / c), size=n_boot).astype(float)
    m = (w @ s) / (w @ n)
    lo, hi = np.quantile(m, [0.025, 0.975])
    return {"est": est, "lo": float(lo), "hi": float(hi), "n": int(yv.size)}


def boot_spearman(x: np.ndarray, y: np.ndarray, cl: np.ndarray, n_boot: int = 2_000, seed: int = 0) -> dict[str, float]:
    """Spearman's rho with a 95 % percentile CI from resampling whole clusters (ranks recomputed per replicate)."""
    xv, yv, cv = np.asarray(x, dtype=float), np.asarray(y, dtype=float), np.asarray(cl)
    ok = np.isfinite(xv) & np.isfinite(yv)
    xv, yv, cv = xv[ok], yv[ok], cv[ok]
    est = spearman(xv, yv)
    labels, inv = np.unique(cv, return_inverse=True)
    c = labels.size
    if c < 2:
        return {"est": est, "lo": math.nan, "hi": math.nan, "n": int(xv.size)}
    rows = [np.flatnonzero(inv == k) for k in range(c)]
    rng = np.random.default_rng(seed)
    rs = np.empty(n_boot)
    for i in range(n_boot):
        idx = np.concatenate([rows[k] for k in rng.integers(0, c, c)])
        rs[i] = spearman(xv[idx], yv[idx])
    rs = rs[np.isfinite(rs)]
    lo, hi = np.quantile(rs, [0.025, 0.975])
    return {"est": est, "lo": float(lo), "hi": float(hi), "n": int(xv.size)}


def placebo_diff(
    x: np.ndarray, y: np.ndarray, cell: np.ndarray, lo: np.ndarray, hi: np.ndarray, n_perm: int = 2_000, seed: int = 0
) -> np.ndarray:
    """T1 - T3 difference of mean y after permuting x inside each cell (rows with finite x only; ``lo`` / ``hi`` are
    the per-row tertile cuts, constant inside a cell when the cell includes the timeframe). Returns the permuted
    differences."""
    xv, yv = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    ok = np.isfinite(xv) & np.isfinite(yv)
    xv, yv = xv[ok], yv[ok]
    lo_v = np.broadcast_to(np.asarray(lo, dtype=float), ok.shape)[ok]
    hi_v = np.broadcast_to(np.asarray(hi, dtype=float), ok.shape)[ok]
    cv = np.asarray(cell)[ok]
    groups = [np.flatnonzero(cv == k) for k in np.unique(cv)]
    rng = np.random.default_rng(seed)
    out = np.empty(n_perm)
    xp = xv.copy()
    for i in range(n_perm):
        for g in groups:
            xp[g] = xv[g[rng.permutation(g.size)]]
        k = tertile_codes(xp, lo_v, hi_v)
        a, b = k == 1, k == 3
        out[i] = yv[a].mean() - yv[b].mean() if a.any() and b.any() else math.nan
    return out
