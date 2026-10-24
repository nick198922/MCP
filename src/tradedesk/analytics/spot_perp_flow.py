"""Spot-vs-perp taker flow divergence after large daily moves (IDEA-20261024-spot-vs-perp-flow-divergence).

Pure functions, no I/O. A symbol's data is a set of numpy arrays on one contiguous grid of UTC days; every trailing
statistic for day t uses days t-window .. t-1 only (the day's own flow D_t is known at its close). Returns, costs and
funding are decimals (0.01 = 1 %).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

DAY_MS = 86_400_000


def imbalance(base_volume: Sequence[float], taker_buy_base: Sequence[float]) -> np.ndarray:
    """Aggressor imbalance (2 * takerBuyBase - volume) / volume in [-1, 1]; nan where volume <= 0 or missing."""
    v = np.asarray(base_volume, dtype=float)
    b = np.asarray(taker_buy_base, dtype=float)
    out = np.full(v.shape, np.nan)
    ok = np.isfinite(v) & np.isfinite(b) & (v > 0)
    out[ok] = (2.0 * b[ok] - v[ok]) / v[ok]
    return out


def simple_returns(close: Sequence[float]) -> np.ndarray:
    """r[t] = close[t] / close[t-1] - 1; nan at t = 0 and wherever either close is missing."""
    c = np.asarray(close, dtype=float)
    r = np.full(c.shape, np.nan)
    if c.size > 1:
        with np.errstate(divide="ignore", invalid="ignore"):
            r[1:] = c[1:] / c[:-1] - 1.0
    r[~np.isfinite(r)] = np.nan
    return r


def forward_return(open_: Sequence[float], close: Sequence[float], h: int) -> np.ndarray:
    """f[t] = close[t+h] / open[t+1] - 1: enter at the next day's open, exit at the close h days after t."""
    o = np.asarray(open_, dtype=float)
    c = np.asarray(close, dtype=float)
    f = np.full(o.shape, np.nan)
    n = o.size
    if n > h:
        with np.errstate(divide="ignore", invalid="ignore"):
            f[: n - h] = c[h:] / o[1 : n - h + 1] - 1.0
    f[~np.isfinite(f)] = np.nan
    return f


def trailing_mean_sd(x: Sequence[float], window: int, min_obs: int) -> tuple[np.ndarray, np.ndarray]:
    """Mean and sample SD (ddof=1) of the finite values in x[t-window : t] (days before t); nan below ``min_obs``."""
    a = np.asarray(x, dtype=float)
    mean = np.full(a.shape, np.nan)
    sd = np.full(a.shape, np.nan)
    for t in range(a.size):
        w = a[max(0, t - window) : t]
        w = w[np.isfinite(w)]
        if w.size >= max(min_obs, 2):
            mean[t] = float(w.mean())
            sd[t] = float(w.std(ddof=1))
    return mean, sd


def trailing_beta(y: Sequence[float], x: Sequence[float], window: int, min_obs: int) -> np.ndarray:
    """OLS slope of y on x over days t-window .. t-1 (pairs where both are finite); nan below ``min_obs``."""
    ya = np.asarray(y, dtype=float)
    xa = np.asarray(x, dtype=float)
    out = np.full(ya.shape, np.nan)
    for t in range(ya.size):
        lo = max(0, t - window)
        wy, wx = ya[lo:t], xa[lo:t]
        ok = np.isfinite(wy) & np.isfinite(wx)
        if ok.sum() < max(min_obs, 3):
            continue
        xv, yv = wx[ok], wy[ok]
        vx = float(((xv - xv.mean()) ** 2).sum())
        if vx > 0:
            out[t] = float(((xv - xv.mean()) * (yv - yv.mean())).sum()) / vx
    return out


def rolling_z(x: Sequence[float], window: int, min_obs: int) -> np.ndarray:
    """(x[t] - mean(x[t-window : t])) / sd(x[t-window : t]): standardised against the prior window only."""
    a = np.asarray(x, dtype=float)
    m, s = trailing_mean_sd(a, window, min_obs)
    out = np.full(a.shape, np.nan)
    ok = np.isfinite(a) & np.isfinite(m) & np.isfinite(s) & (s > 0)
    out[ok] = (a[ok] - m[ok]) / s[ok]
    return out


def symbol_features(
    open_: Sequence[float],
    close: Sequence[float],
    d: Sequence[float],
    btc_r: Sequence[float] | None,
    *,
    sd_window: int = 30,
    sd_min: int = 25,
    z_window: int = 90,
    z_min: int = 60,
    beta_window: int = 90,
    beta_min: int = 60,
) -> dict[str, np.ndarray]:
    """Per-day features of one symbol: r, sd30 (prior days), z of D (prior window), beta to BTC (prior window; 1.0 for
    BTC itself, i.e. ``btc_r`` None), forward returns f1 / f3 from the next open."""
    r = simple_returns(close)
    _, sd = trailing_mean_sd(r, sd_window, sd_min)
    z = rolling_z(d, z_window, z_min)
    beta = np.ones(r.shape) if btc_r is None else trailing_beta(r, btc_r, beta_window, beta_min)
    return {"r": r, "sd30": sd, "z": z, "beta": beta, "f1": forward_return(open_, close, 1), "f3": forward_return(open_, close, 3)}


def event_mask(r: Sequence[float], sd: Sequence[float], k: float) -> np.ndarray:
    """|r_t| >= k * SD_t (both finite, SD > 0)."""
    ra = np.asarray(r, dtype=float)
    sa = np.asarray(sd, dtype=float)
    ok = np.isfinite(ra) & np.isfinite(sa) & (sa > 0)
    out = np.zeros(ra.shape, dtype=bool)
    out[ok] = np.abs(ra[ok]) >= k * sa[ok]
    return out


def expanding_bin_labels(days: Sequence[int], values: Sequence[float], *, nbins: int = 3, min_prior: int = 150) -> np.ndarray:
    """Quantile bin 0..nbins-1 of each value against all values on strictly earlier days (expanding window).

    Cut points are the i/nbins quantiles of the prior values; a value equal to a cut goes to the lower bin. Rows
    with fewer than ``min_prior`` prior values (burn-in) or a nan value get -1. Labels for day t never depend on
    values of days >= t other than its own."""
    dd = np.asarray(days, dtype=np.int64)
    v = np.asarray(values, dtype=float)
    out = np.full(v.shape, -1, dtype=np.int64)
    order = np.argsort(dd, kind="stable")
    sd, sv = dd[order], v[order]
    qs = [i / nbins for i in range(1, nbins)]
    start = 0
    prior: list[float] = []
    while start < sd.size:
        end = start
        while end < sd.size and sd[end] == sd[start]:
            end += 1
        if len(prior) >= min_prior:
            cuts = np.quantile(np.asarray(prior), qs)
            seg = sv[start:end]
            lab = np.searchsorted(cuts, seg, side="left")
            lab[~np.isfinite(seg)] = -1
            out[order[start:end]] = lab
        prior.extend(x for x in sv[start:end] if math.isfinite(x))
        start = end
    return out


def week_of(day: int) -> int:
    """Monday-based week index of an epoch day (epoch day 0, 1970-01-01, was a Thursday)."""
    return (int(day) + 3) // 7


def funding_sum(times_ms: Sequence[float], rates: Sequence[float], start_ms: int, end_ms: int) -> float:
    """Sum of funding rates settled at times in (start_ms, end_ms]; ``times_ms`` must be ascending."""
    t = np.asarray(times_ms, dtype=float)
    r = np.asarray(rates, dtype=float)
    lo = int(np.searchsorted(t, start_ms, side="right"))
    hi = int(np.searchsorted(t, end_ms, side="right"))
    return float(r[lo:hi].sum())


def last_funding(times_ms: Sequence[float], rates: Sequence[float], at_ms: int) -> float:
    """Rate of the last settlement at or before ``at_ms``; nan if none."""
    t = np.asarray(times_ms, dtype=float)
    i = int(np.searchsorted(t, at_ms, side="right")) - 1
    return float(rates[i]) if i >= 0 else float("nan")


def trade_cost(
    beta: float, *, is_major: bool, hedged: bool, fee_rt: float = 0.0011, slip_alt: float = 0.0005, slip_major: float = 0.0002
) -> float:
    """Round-trip cost of one event trade: fees + 2 x slippage on the main leg, plus |beta| x (fees + 2 x major
    slippage) on the BTC hedge leg when ``hedged`` (decimal of notional)."""
    main = fee_rt + 2.0 * (slip_major if is_major else slip_alt)
    hedge = abs(beta) * (fee_rt + 2.0 * slip_major) if hedged else 0.0
    return main + hedge


def _cluster_sums(values: np.ndarray, groups: np.ndarray, clusters: Sequence[Any], keys: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
    ck = sorted(set(clusters), key=str)
    pos = {c: i for i, c in enumerate(ck)}
    ci = np.fromiter((pos[c] for c in clusters), dtype=np.int64, count=len(clusters))
    S = np.zeros((len(ck), len(keys)))
    N = np.zeros((len(ck), len(keys)))
    for j, k in enumerate(keys):
        m = groups == k
        S[:, j] = np.bincount(ci[m], weights=values[m], minlength=len(ck))
        N[:, j] = np.bincount(ci[m], minlength=len(ck))
    return S, N


def combo_estimate(values: Sequence[float], groups: Sequence[int], weights: Mapping[int, float]) -> float:
    """sum_k w_k * mean(values | group k); nan if a weighted group is empty."""
    v = np.asarray(values, dtype=float)
    g = np.asarray(groups)
    out = 0.0
    for k, w in weights.items():
        m = g == k
        if not m.any():
            return float("nan")
        out += w * float(v[m].mean())
    return out


def cluster_se_combo(
    values: Sequence[float], groups: Sequence[int], clusters: Sequence[Any], weights: Mapping[int, float]
) -> dict[str, Any]:
    """Cluster-robust (CR1) SE of sum_k w_k * mean_k: influence w_k (y_i - m_k) / n_k summed within clusters.

    Group means are used only for centring and are not returned (stage-0 power gate)."""
    v = np.asarray(values, dtype=float)
    g = np.asarray(groups)
    keys = list(weights)
    infl = np.zeros(v.shape)
    ns = {}
    for k in keys:
        m = g == k
        ns[k] = int(m.sum())
        if ns[k] == 0:
            return {"se": float("nan"), "n": ns, "clusters": 0}
        infl[m] = weights[k] * (v[m] - v[m].mean()) / ns[k]
    use = np.isin(g, keys)
    cl = [c for c, u in zip(clusters, use, strict=True) if u]
    ck = sorted(set(cl), key=str)
    pos = {c: i for i, c in enumerate(ck)}
    ci = np.fromiter((pos[c] for c in cl), dtype=np.int64, count=len(cl))
    sums = np.bincount(ci, weights=infl[use], minlength=len(ck))
    G = len(ck)
    var = G / (G - 1) * float((sums**2).sum()) if G > 1 else float("nan")
    iid = sum(weights[k] ** 2 * float(v[g == k].var(ddof=1)) / ns[k] for k in keys if ns[k] > 1)
    return {"se": math.sqrt(var), "se_iid": math.sqrt(iid), "n": ns, "clusters": G}


def cluster_bootstrap_combo(
    values: Sequence[float],
    groups: Sequence[int],
    clusters: Sequence[Any],
    weights: Mapping[int, float],
    *,
    n_boot: int = 10_000,
    seed: int = 20261024,
    level: float = 0.95,
) -> dict[str, Any]:
    """Cluster (e.g. ISO week) bootstrap of sum_k w_k * mean_k: estimate, percentile CI, one-sided p = share <= 0."""
    v = np.asarray(values, dtype=float)
    g = np.asarray(groups)
    keys = list(weights)
    use = np.isin(g, keys)
    cl = [c for c, u in zip(clusters, use, strict=True) if u]
    est = combo_estimate(v[use], g[use], weights)
    if not math.isfinite(est) or len(set(cl)) < 2:
        return {"est": est, "lo": float("nan"), "hi": float("nan"), "p": float("nan"), "clusters": len(set(cl)), "n": int(use.sum())}
    S, N = _cluster_sums(v[use], g[use], cl, keys)
    G = S.shape[0]
    rng = np.random.default_rng(seed)
    W = rng.multinomial(G, np.full(G, 1.0 / G), size=n_boot).astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        bs = sum(w * (W @ S[:, j]) / (W @ N[:, j]) for j, w in enumerate(weights.values()))
    bs = bs[np.isfinite(bs)]
    a = (1.0 - level) / 2.0
    return {
        "est": est,
        "lo": float(np.quantile(bs, a)),
        "hi": float(np.quantile(bs, 1.0 - a)),
        "p": float((bs <= 0).mean()),
        "clusters": G,
        "n": int(use.sum()),
    }


def drop_top_clusters_combo(
    values: Sequence[float], groups: Sequence[int], clusters: Sequence[Any], weights: Mapping[int, float], k: int
) -> float:
    """sum_k w_k * mean_k after removing the k clusters with the largest positive contribution to it."""
    v = np.asarray(values, dtype=float)
    g = np.asarray(groups)
    keys = list(weights)
    use = np.isin(g, keys)
    cl = [c for c, u in zip(clusters, use, strict=True) if u]
    ck = sorted(set(cl), key=str)
    S, N = _cluster_sums(v[use], g[use], cl, keys)
    n = N.sum(axis=0)
    contrib = sum(w * S[:, j] / n[j] for j, w in enumerate(weights.values()))
    drop = set(np.argsort(-contrib)[:k].tolist())
    keep = np.array([i not in drop for i in range(len(ck))])
    with np.errstate(divide="ignore", invalid="ignore"):
        return float(sum(w * S[keep, j].sum() / N[keep, j].sum() for j, w in enumerate(weights.values())))


def shuffle_within_groups(values: Sequence[float], groups: Sequence[Any], rng: np.random.Generator) -> np.ndarray:
    """Values permuted among the rows of each group (e.g. symbols of one day); group sizes are preserved."""
    v = np.asarray(values, dtype=float)
    keys = sorted(set(groups), key=str)
    pos = {k: i for i, k in enumerate(keys)}
    gi = np.fromiter((pos[x] for x in groups), dtype=np.int64, count=len(groups))
    base = np.argsort(gi, kind="stable")
    perm = np.lexsort((rng.random(v.size), gi))
    out = np.empty_like(v)
    out[base] = v[perm]
    return out


def ols_cluster_cov(y: Sequence[float], X: Sequence[Sequence[float]], clusters: Sequence[Any]) -> dict[str, Any]:
    """OLS with CR1 cluster-robust covariance (returned in full so that contrasts can be tested)."""
    Xa = np.asarray(X, dtype=float)
    ya = np.asarray(y, dtype=float)
    bread = np.linalg.inv(Xa.T @ Xa)
    beta = bread @ (Xa.T @ ya)
    res = ya - Xa @ beta
    n, k = Xa.shape
    ck = sorted(set(clusters), key=str)
    pos = {c: i for i, c in enumerate(ck)}
    ci = np.fromiter((pos[c] for c in clusters), dtype=np.int64, count=len(clusters))
    sc = np.zeros((len(ck), k))
    np.add.at(sc, ci, Xa * res[:, None])
    G = len(ck)
    corr = G / (G - 1) * (n - 1) / (n - k) if G > 1 and n > k else 1.0
    cov = corr * bread @ (sc.T @ sc) @ bread
    return {"coef": beta, "cov": cov, "n": n, "clusters": G}


def contrast(fit: Mapping[str, Any], c: Sequence[float]) -> dict[str, float]:
    """Estimate, SE and two-sided normal p of the linear contrast c' beta of an ``ols_cluster_cov`` fit."""
    ca = np.asarray(c, dtype=float)
    est = float(ca @ fit["coef"])
    se = math.sqrt(float(ca @ fit["cov"] @ ca))
    p = math.erfc(abs(est / se) / math.sqrt(2.0)) if se > 0 else float("nan")
    return {"est": est, "se": se, "p": p}
