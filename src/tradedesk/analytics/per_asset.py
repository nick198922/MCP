"""Per-asset checks for setups pooled over many assets (quant.md point 5a, IDEA-20261017-per-asset-recheck).

Pure functions, no I/O. Inputs are per-trade arrays: ``values`` (R net per trade, R = return on the trade's planned
risk, not on capital), ``assets`` (label per trade), ``clusters`` (UTC date per trade) and, for persistence, an
in-sample mask. Time enters only through that mask, so results on in-sample trades never depend on out-of-sample ones.

- ``asset_stats``: n, mean, sd and a normal 95 % CI per asset.
- ``heterogeneity``: Cochran's Q with the pooled within-asset variance (and with per-asset variances), I^2, and the
  DerSimonian-Laird tau (SD of the true per-asset means, in R).
- ``permutation_q``: the same Q against asset labels permuted within each date (keeps every asset's trade count on
  every date, so common market days and the era an asset traded in are controlled for).
- ``persistence``: rank assets by the in-sample mean, then compare the out-of-sample mean of the top group with the
  rest (joint date-cluster bootstrap), plus Spearman rho of in-sample vs out-of-sample asset means.
- ``tau_ci_qprofile`` / ``top_fraction_uplift`` / ``mde``: what the tests can exclude, i.e. a CI for tau, the implied
  true mean of the best quarter of assets (mean + 1.271 tau) and the minimum detectable difference of a test.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any

import numpy as np

from tradedesk.analytics.liquidation_flush import cluster_bootstrap_ci
from tradedesk.analytics.session_stop_hunt import cluster_bootstrap_diff

Z95 = 1.959963984540054


def chi2_sf(x: float, df: int) -> float:
    """Survival function of the chi-square distribution, P(X >= x), via the regularized upper incomplete gamma."""
    if df <= 0:
        raise ValueError("df must be positive")
    if x <= 0:
        return 1.0
    a, z = df / 2.0, x / 2.0
    log_pref = -z + a * math.log(z) - math.lgamma(a)
    if z < a + 1.0:  # series for the lower part P(a, z)
        ap, term = a, 1.0 / a
        total = term
        for _ in range(10_000):
            ap += 1.0
            term *= z / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(log_pref))
    tiny = 1e-300  # Lentz continued fraction for Q(a, z)
    b = z + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 10_000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return min(1.0, math.exp(log_pref) * h)


@dataclass(frozen=True)
class AssetStat:
    asset: Any
    n: int
    mean: float
    sd: float
    ci: tuple[float, float] | None  # mean +- 1.96 sd/sqrt(n); None with n < 2


def _codes(assets: Sequence[Any]) -> tuple[list[Any], np.ndarray]:
    keys = sorted(set(assets), key=str)
    pos = {k: i for i, k in enumerate(keys)}
    return keys, np.fromiter((pos[a] for a in assets), dtype=np.int64, count=len(assets))


def asset_stats(values: Sequence[float], assets: Sequence[Any], *, min_n: int = 1) -> list[AssetStat]:
    """Per-asset n, mean, sample sd and normal 95 % CI, for assets with at least ``min_n`` trades (sorted by label)."""
    v = np.asarray(values, dtype=float)
    keys, c = _codes(assets)
    out: list[AssetStat] = []
    for i, k in enumerate(keys):
        x = v[c == i]
        if x.size < min_n:
            continue
        m = float(x.mean())
        sd = float(x.std(ddof=1)) if x.size > 1 else float("nan")
        ci = (m - Z95 * sd / math.sqrt(x.size), m + Z95 * sd / math.sqrt(x.size)) if x.size > 1 else None
        out.append(AssetStat(k, int(x.size), m, sd, ci))
    return out


def _q_pooled(v: np.ndarray, codes: np.ndarray, keep: np.ndarray) -> float:
    """Q = sum n_i (m_i - m)^2 / s_w^2 over assets flagged in ``keep`` (s_w^2: pooled within-asset variance)."""
    k = keep.size
    n = np.bincount(codes, minlength=k).astype(float)[keep]
    s = np.bincount(codes, weights=v, minlength=k)[keep]
    ss = np.bincount(codes, weights=v * v, minlength=k)[keep]
    big_n = n.sum()
    if keep.sum() < 2 or big_n - keep.sum() <= 0:
        return float("nan")
    between = float(np.sum(s * s / n) - s.sum() ** 2 / big_n)
    within = float(ss.sum() - np.sum(s * s / n))
    s2w = within / (big_n - keep.sum())
    return between / s2w if s2w > 0 else float("nan")


def heterogeneity(values: Sequence[float], assets: Sequence[Any], *, min_n: int = 30) -> dict[str, Any]:
    """Cochran's Q across assets with >= ``min_n`` trades.

    q / p / i2: Q with weights n_i / s_w^2 (pooled within-asset variance), p from chi-square(k - 1), I^2 =
    max(0, (Q - df) / Q). q_sep / p_sep / tau: Q with weights n_i / s_i^2 and the DerSimonian-Laird between-asset SD.
    """
    v = np.asarray(values, dtype=float)
    keys, c = _codes(assets)
    n_all = np.bincount(c, minlength=len(keys))
    keep = n_all >= max(2, min_n)
    k = int(keep.sum())
    res: dict[str, Any] = {"k": k, "n": int(n_all[keep].sum()), "df": k - 1}
    if k < 2:
        return {**res, "q": float("nan"), "p": None, "i2": float("nan"), "q_sep": float("nan"), "p_sep": None, "tau": float("nan")}
    q = _q_pooled(v, c, keep)
    st = {s.asset: s for s in asset_stats(v, assets, min_n=max(2, min_n))}
    m = np.array([st[keys[i]].mean for i in np.flatnonzero(keep)])
    w = np.array([st[keys[i]].n / st[keys[i]].sd ** 2 if st[keys[i]].sd > 0 else np.nan for i in np.flatnonzero(keep)])
    ok = np.isfinite(w)
    m, w = m[ok], w[ok]
    mw = float(np.sum(w * m) / np.sum(w)) if w.size else float("nan")
    q_sep = float(np.sum(w * (m - mw) ** 2)) if w.size else float("nan")
    df_sep = int(w.size) - 1
    denom = float(np.sum(w) - np.sum(w * w) / np.sum(w)) if w.size else float("nan")
    tau2 = max(0.0, (q_sep - df_sep) / denom) if w.size > 1 and denom > 0 else float("nan")
    return {
        **res,
        "q": q,
        "p": chi2_sf(q, k - 1) if math.isfinite(q) else None,
        "i2": max(0.0, (q - (k - 1)) / q) if math.isfinite(q) and q > 0 else 0.0,
        "q_sep": q_sep,
        "p_sep": chi2_sf(q_sep, df_sep) if df_sep > 0 and math.isfinite(q_sep) else None,
        "tau": math.sqrt(tau2) if math.isfinite(tau2) else float("nan"),
    }


def cluster_robust_variances(
    values: Sequence[float], assets: Sequence[Any], clusters: Sequence[Any], *, min_n: int = 30
) -> list[tuple[Any, float, float, float]]:
    """Per-asset (asset, mean, cluster-robust variance of the mean, iid variance of the mean) with clusters (e.g. ISO
    weeks) taken within each asset: se_i^2 = G/(G-1) * sum_g (S_g - n_g m_i)^2 / n_i^2. Assets with n < max(2, min_n),
    fewer than two clusters or a zero variance are skipped. The variances can be passed to ``tau_ci_qprofile``."""
    v = np.asarray(values, dtype=float)
    keys, c = _codes(assets)
    _, g = _codes(clusters)
    out: list[tuple[Any, float, float, float]] = []
    for i in range(len(keys)):
        sel = c == i
        n = int(sel.sum())
        if n < max(2, min_n):
            continue
        x, gg = v[sel], g[sel]
        m = float(x.mean())
        _, inv = np.unique(gg, return_inverse=True)
        n_g = np.bincount(inv).astype(float)
        s_g = np.bincount(inv, weights=x)
        big_g = n_g.size
        if big_g < 2:
            continue
        se2 = big_g / (big_g - 1) * float(np.sum((s_g - n_g * m) ** 2)) / n**2
        if se2 <= 0:
            continue
        out.append((keys[i], m, se2, float(x.var(ddof=1)) / n))
    return out


def cluster_robust_q(values: Sequence[float], assets: Sequence[Any], clusters: Sequence[Any], *, min_n: int = 30) -> dict[str, Any]:
    """Cochran's Q with cluster-robust per-asset standard errors (clusters, e.g. ISO weeks, taken within each asset):
    se_i^2 = G/(G-1) * sum_g (S_g - n_g m_i)^2 / n_i^2. Overlapping trades of one asset (the same move tested by
    several zones) then do not count as independent. With one trade per cluster this equals Q with per-asset
    variances (``heterogeneity(...)["q_sep"]``). Returns q, df, p, i2 and the mean design effect (se_cl / se_iid)^2.
    """
    rows = cluster_robust_variances(values, assets, clusters, min_n=min_n)
    m_l = [m for _, m, _, _ in rows]
    w_l = [1.0 / se2 for _, _, se2, _ in rows]
    deff = [se2 / vi if vi > 0 else float("nan") for _, _, se2, vi in rows]
    k = len(m_l)
    if k < 2:
        return {"k": k, "q": float("nan"), "df": k - 1, "p": None, "i2": float("nan"), "deff": float("nan")}
    m_a, w_a = np.array(m_l), np.array(w_l)
    mw = float(np.sum(w_a * m_a) / np.sum(w_a))
    q = float(np.sum(w_a * (m_a - mw) ** 2))
    return {
        "k": k,
        "q": q,
        "df": k - 1,
        "p": chi2_sf(q, k - 1),
        "i2": max(0.0, (q - (k - 1)) / q) if q > 0 else 0.0,
        "deff": float(np.nanmean(deff)),
    }


def permute_within(codes: np.ndarray, clusters: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Asset codes shuffled among the trades of each cluster (date); counts per (cluster, asset) are preserved."""
    base = np.argsort(clusters, kind="stable")
    perm = np.lexsort((rng.random(codes.size), clusters))
    out = np.empty_like(codes)
    out[perm] = codes[base]
    return out


def permutation_q(
    values: Sequence[float], assets: Sequence[Any], clusters: Sequence[Any], *, min_n: int = 30, n_perm: int = 2000, seed: int = 20261017
) -> dict[str, Any]:
    """Pooled-variance Q against within-date permutations of asset labels. p = (1 + #{Q* >= Q}) / (1 + n_perm)."""
    v = np.asarray(values, dtype=float)
    keys, c = _codes(assets)
    _, cl = _codes(clusters)
    keep = np.bincount(c, minlength=len(keys)) >= max(2, min_n)
    if keep.sum() < 2:
        return {"q": float("nan"), "p": None, "null_mean": float("nan"), "null_p95": float("nan"), "n_perm": 0}
    q = _q_pooled(v, c, keep)
    rng = np.random.default_rng(seed)
    null = np.array([_q_pooled(v, permute_within(c, cl, rng), keep) for _ in range(n_perm)])
    return {
        "q": q,
        "p": float((1 + np.sum(null >= q - 1e-12)) / (1 + n_perm)),
        "null_mean": float(np.mean(null)),
        "null_p95": float(np.quantile(null, 0.95)),
        "n_perm": n_perm,
    }


def _ranks(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="stable")
    r = np.empty(x.size)
    r[order] = np.arange(1, x.size + 1, dtype=float)
    for val in np.unique(x):  # average ranks for ties
        m = x == val
        if m.sum() > 1:
            r[m] = r[m].mean()
    return r


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman rank correlation (average ranks for ties); nan with fewer than 3 pairs."""
    a, b = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if a.size < 3:
        return float("nan")
    ra, rb = _ranks(a), _ranks(b)
    ra, rb = ra - ra.mean(), rb - rb.mean()
    den = math.sqrt(float(np.sum(ra * ra) * np.sum(rb * rb)))
    return float(np.sum(ra * rb) / den) if den > 0 else float("nan")


def persistence(
    values: Sequence[float],
    assets: Sequence[Any],
    clusters: Sequence[Any],
    is_mask: Sequence[bool],
    *,
    min_n_is: int = 20,
    top_frac: float = 0.25,
    top_n: int | None = None,
    min_n_oos_rho: int = 10,
    n_boot: int = 10_000,
    seed: int = 20261017,
) -> dict[str, Any]:
    """Rank assets with >= ``min_n_is`` in-sample trades by in-sample mean (ties: more trades first); the top
    ceil(k * top_frac) (or ``top_n``) form the top group. Out of sample: mean(top) - mean(rest of the ranked assets),
    trade-weighted, joint date-cluster bootstrap (CI, one-sided p for > 0); also top vs every other OOS trade, the top
    group's own OOS mean with CI, and Spearman rho of IS vs OOS means (assets with >= ``min_n_oos_rho`` OOS trades).
    """
    v = np.asarray(values, dtype=float)
    a = np.asarray(assets, dtype=object)
    cl = np.asarray(clusters, dtype=object)
    ism = np.asarray(is_mask, dtype=bool)
    is_stats = asset_stats(v[ism], list(a[ism]), min_n=min_n_is)
    ranked = sorted(is_stats, key=lambda s: (-s.mean, -s.n, str(s.asset)))
    k = len(ranked)
    n_top = min(k, top_n) if top_n is not None else math.ceil(k * top_frac)
    top = [s.asset for s in ranked[:n_top]]
    rest = [s.asset for s in ranked[n_top:]]
    oos = ~ism
    in_top = np.isin(a, top) & oos
    in_rest = np.isin(a, rest) & oos
    others = oos & ~np.isin(a, top)
    d = cluster_bootstrap_diff(v[in_top].tolist(), cl[in_top].tolist(), v[in_rest].tolist(), cl[in_rest].tolist(), n_boot=n_boot, seed=seed)
    d_all = cluster_bootstrap_diff(
        v[in_top].tolist(), cl[in_top].tolist(), v[others].tolist(), cl[others].tolist(), n_boot=n_boot, seed=seed
    )
    top_mean = float(v[in_top].mean()) if in_top.any() else float("nan")
    top_ci = cluster_bootstrap_ci(v[in_top].tolist(), cl[in_top].tolist(), n_boot=n_boot) if in_top.any() else None
    oos_stats = {s.asset: s for s in asset_stats(v[oos], list(a[oos]), min_n=min_n_oos_rho)} if oos.any() else {}
    pairs = [(s.mean, oos_stats[s.asset].mean) for s in ranked if s.asset in oos_stats]
    return {
        "k": k,
        "top": top,
        "is_ranked": [(s.asset, s.n, s.mean) for s in ranked],
        "n_top_oos": int(in_top.sum()),
        "n_rest_oos": int(in_rest.sum()),
        "n_top_assets_oos": int(len(set(a[in_top]))),
        "top_is_mean": float(v[np.isin(a, top) & ism].mean()) if top else float("nan"),
        "rest_is_mean": float(v[np.isin(a, rest) & ism].mean()) if rest else float("nan"),
        "top_oos_mean": top_mean,
        "top_oos_ci": top_ci,
        "rest_oos_mean": float(v[in_rest].mean()) if in_rest.any() else float("nan"),
        "diff": d["diff"],
        "diff_ci": d["ci"],
        "diff_p": d["p"],
        "diff_vs_all": d_all["diff"],
        "diff_vs_all_ci": d_all["ci"],
        "rho": spearman([p[0] for p in pairs], [p[1] for p in pairs]),
        "rho_n": len(pairs),
    }


def chi2_ppf(p: float, df: int) -> float:
    """Quantile of the chi-square distribution: x with P(X <= x) = p (bisection on ``chi2_sf``)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    lo, hi = 0.0, float(df) + 10.0 * math.sqrt(2.0 * df) + 50.0
    while chi2_sf(hi, df) > 1.0 - p:
        hi *= 2.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if chi2_sf(mid, df) > 1.0 - p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12 * max(1.0, hi):
            break
    return 0.5 * (lo + hi)


def _gen_q(y: np.ndarray, var: np.ndarray, tau2: float) -> tuple[float, float, float]:
    """Generalized Q(tau^2) = sum w_i (y_i - mu)^2 with w_i = 1 / (var_i + tau^2); returns (Q, mu, se(mu))."""
    w = 1.0 / (var + tau2)
    mu = float(np.sum(w * y) / np.sum(w))
    return float(np.sum(w * (y - mu) ** 2)), mu, 1.0 / math.sqrt(float(np.sum(w)))


def tau_ci_qprofile(means: Sequence[float], variances: Sequence[float], *, level: float = 0.95) -> dict[str, Any]:
    """Q-profile confidence interval for tau (Viechtbauer 2007): the between-asset SD of the true per-asset means.

    ``means`` are per-asset means, ``variances`` their sampling variances (sd_i^2 / n_i). Q(tau^2) decreases in tau^2;
    the bounds solve Q(tau^2) = chi2_{k-1}((1 + level) / 2) (lower) and chi2_{k-1}((1 - level) / 2) (upper), floored
    at 0. Also returns the random-effects mean and its SE at tau_upper. The bounds are only as good as the variances:
    iid per-trade variances are biased when trades of one asset are correlated, in either direction (positive
    correlation inflates tau, negative correlation or shared market days deflate it), so neither bound is conservative
    by construction; compare with cluster-robust variances (``cluster_robust_variances``).
    """
    y = np.asarray(means, dtype=float)
    var = np.asarray(variances, dtype=float)
    ok = np.isfinite(y) & np.isfinite(var) & (var > 0)
    y, var = y[ok], var[ok]
    k = int(y.size)
    if k < 2:
        return {"k": k, "tau_lo": float("nan"), "tau_hi": float("nan"), "mu_hi_tau": float("nan"), "se_mu_hi_tau": float("nan")}
    a = 1.0 - level

    def solve(target: float) -> float:
        if _gen_q(y, var, 0.0)[0] <= target:
            return 0.0
        hi = max(float(np.var(y, ddof=1)), float(np.max(var)), 1e-12)
        while _gen_q(y, var, hi)[0] > target:
            hi *= 2.0
        lo = 0.0
        for _ in range(200):
            mid = 0.5 * (lo + hi)
            if _gen_q(y, var, mid)[0] > target:
                lo = mid
            else:
                hi = mid
            if hi - lo < 1e-14 * max(1.0, hi):
                break
        return 0.5 * (lo + hi)

    t2_lo = solve(chi2_ppf(1.0 - a / 2.0, k - 1))
    t2_hi = solve(chi2_ppf(a / 2.0, k - 1))
    _, mu, se = _gen_q(y, var, t2_hi)
    return {"k": k, "tau_lo": math.sqrt(t2_lo), "tau_hi": math.sqrt(t2_hi), "mu_hi_tau": mu, "se_mu_hi_tau": se}


def top_fraction_uplift(frac: float = 0.25) -> float:
    """E[Z | Z above its (1 - frac) quantile] for Z ~ N(0, 1), i.e. phi(z) / frac: how many tau the best ``frac`` of
    assets sit above the mean when true per-asset means are normal (1.271 for a quarter, 0.798 for a half)."""
    if not 0.0 < frac < 1.0:
        raise ValueError("frac must be in (0, 1)")
    nd = NormalDist()
    return nd.pdf(nd.inv_cdf(1.0 - frac)) / frac


def mde(se: float, *, alpha: float = 0.05, power: float = 0.8, one_sided: bool = True) -> float:
    """Minimum detectable effect of a z-test with standard error ``se``: (z_{1-alpha(/2)} + z_power) * se."""
    nd = NormalDist()
    za = nd.inv_cdf(1.0 - alpha) if one_sided else nd.inv_cdf(1.0 - alpha / 2.0)
    return (za + nd.inv_cdf(power)) * se


def quantile_bins(x: Sequence[float], cuts: Sequence[float]) -> np.ndarray:
    """Bin index 0..len(cuts) for each x given ascending cut points (x <= cut goes to the lower bin); -1 for nan."""
    a = np.asarray(x, dtype=float)
    out = np.searchsorted(np.asarray(cuts, dtype=float), a, side="left").astype(np.int64)
    out[~np.isfinite(a)] = -1
    return out
