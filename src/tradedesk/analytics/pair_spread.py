"""Pair spread mean reversion between close substitutes (two perps), pure functions.

Everything works on a common daily grid: arrays indexed by day (row ``t``), ``nan`` where a symbol has no bar.
Opens are the 00:00 UTC prices of day ``t``; closes the 24:00 UTC prices; ``funding[t]`` is the sum of funding rates
settled during day ``t`` (decimal per unit of notional, positive = longs pay).

Pair formation (``form_pairs``) only sees the window it is given; the runner passes rows strictly before the formation
day. ``rolling_z`` at day ``t`` uses closes up to ``t``; ``simulate_pair`` decides at closes and trades at the next
open, so a trade that has exited by day ``t`` does not depend on data after ``t``.

Returns are per unit of mean leg notional: the legs carry ``w_a + w_b = 2``, so one pair "notional" is one leg.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np


def pairwise_corr(x: np.ndarray, *, min_obs: int = 3) -> np.ndarray:
    """Pearson correlation of every column pair over the rows where both are finite (``nan`` below ``min_obs``)."""
    a = np.asarray(x, dtype=float)
    m = np.isfinite(a).astype(float)
    z = np.where(m > 0, a, 0.0)
    n = m.T @ m
    sx = z.T @ m  # sx[i, j] = sum of x_i over rows where both i and j are finite
    sxx = (z * z).T @ m
    sxy = z.T @ z
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = sxy - sx * sx.T / n
        vx = sxx - sx * sx / n
        r = cov / np.sqrt(vx * vx.T)
    r[n < min_obs] = np.nan
    return np.clip(r, -1.0, 1.0)


def ols_slope(y: np.ndarray, x: np.ndarray) -> float:
    """OLS slope of y on x (with intercept) over rows where both are finite; ``nan`` with < 3 rows or zero var."""
    yy, xx = np.asarray(y, dtype=float), np.asarray(x, dtype=float)
    ok = np.isfinite(yy) & np.isfinite(xx)
    if ok.sum() < 3:
        return float("nan")
    xs, ys = xx[ok] - xx[ok].mean(), yy[ok] - yy[ok].mean()
    v = float(np.dot(xs, xs))
    return float(np.dot(xs, ys) / v) if v > 0 else float("nan")


def residuals(rets: np.ndarray, mkt: np.ndarray) -> np.ndarray:
    """Columns of ``rets`` minus alpha + beta * mkt (OLS per column over rows where both are finite)."""
    r = np.asarray(rets, dtype=float)
    m = np.asarray(mkt, dtype=float)
    out = np.full_like(r, np.nan)
    for j in range(r.shape[1]):
        ok = np.isfinite(r[:, j]) & np.isfinite(m)
        if ok.sum() < 3:
            continue
        b = ols_slope(r[ok, j], m[ok])
        if not math.isfinite(b):
            continue
        a = r[ok, j].mean() - b * m[ok].mean()
        out[ok, j] = r[ok, j] - a - b * m[ok]
    return out


def average_linkage(dist: np.ndarray, cut: float) -> list[int]:
    """Agglomerative average-linkage clustering; merges while the closest cluster distance is <= ``cut``.

    ``nan`` distances count as +inf. Returns a label per item (0..k-1, in order of first appearance)."""
    d = np.array(dist, dtype=float)
    n = d.shape[0]
    d[~np.isfinite(d)] = np.inf
    np.fill_diagonal(d, np.inf)
    size = np.ones(n)
    alive = np.ones(n, dtype=bool)
    parent = list(range(n))
    while alive.sum() > 1:
        sub = np.where(alive[:, None] & alive[None, :], d, np.inf)
        k = int(np.argmin(sub))
        i, j = divmod(k, n)
        if not sub[i, j] <= cut:
            break
        new = (size[i] * d[i] + size[j] * d[j]) / (size[i] + size[j])  # Lance-Williams for average linkage
        d[i, :] = new
        d[:, i] = new
        d[i, i] = np.inf
        d[j, :] = np.inf
        d[:, j] = np.inf
        size[i] += size[j]
        alive[j] = False
        parent[j] = i
    root = []
    for x in range(n):
        while parent[x] != x:
            x = parent[x]
        root.append(x)
    labels: dict[int, int] = {}
    return [labels.setdefault(r, len(labels)) for r in root]


@dataclass(frozen=True)
class Pair:
    a: int  # column index (first leg)
    b: int
    rho: float  # return correlation over the formation window
    rho_e: float  # residual (ex-BTC) correlation
    h: float  # clipped hedge ratio: OLS slope of r_a on r_b


def hedge_weights(h: float) -> tuple[float, float]:
    """Leg notionals (w_a, w_b) with w_b / w_a = h and w_a + w_b = 2."""
    return 2.0 / (1.0 + h), 2.0 * h / (1.0 + h)


@dataclass(frozen=True)
class Formation:
    pairs: list[Pair]
    labels: list[int]
    rho: np.ndarray
    rho_e: np.ndarray


def form_pairs(
    rets: np.ndarray,
    mkt: np.ndarray,
    *,
    rho_min: float = 0.80,
    rho_max: float = 0.98,
    cut: float = 0.70,
    h_clip: tuple[float, float] = (0.5, 2.0),
) -> Formation:
    """H1 pairs from a formation window of daily log returns (rows = days before the formation day, columns = the
    admitted symbols) and the market return ``mkt`` of the same rows: average-linkage clusters on 1 - rho_e (cut
    ``cut``), and per cluster the single pair with rho in [rho_min, rho_max) and the highest rho_e."""
    rho = pairwise_corr(rets)
    rho_e = pairwise_corr(residuals(rets, mkt))
    n = rho.shape[0]
    labels = average_linkage(1.0 - rho_e, cut) if n else []
    best: dict[int, tuple[float, int, int]] = {}
    for i in range(n):
        for j in range(i + 1, n):
            if labels[i] != labels[j]:
                continue
            r, re = rho[i, j], rho_e[i, j]
            if not (math.isfinite(r) and math.isfinite(re)) or not rho_min <= r < rho_max:
                continue
            cur = best.get(labels[i])
            if cur is None or re > cur[0]:
                best[labels[i]] = (re, i, j)
    pairs = []
    for c in sorted(best):
        re, i, j = best[c]
        h = ols_slope(rets[:, i], rets[:, j])
        h = min(max(h if math.isfinite(h) else 1.0, h_clip[0]), h_clip[1])
        pairs.append(Pair(i, j, float(rho[i, j]), float(re), h))
    return Formation(pairs, labels, rho, rho_e)


def _greedy_disjoint(cands: Sequence[tuple[int, int]], k: int) -> list[tuple[int, int]]:
    used: set[int] = set()
    out: list[tuple[int, int]] = []
    for i, j in cands:
        if len(out) >= k:
            break
        if i in used or j in used:
            continue
        used.update((i, j))
        out.append((i, j))
    return out


def control_low_corr(rho: np.ndarray, k: int, rng: np.random.Generator, *, rho_hi: float = 0.50) -> list[tuple[int, int]]:
    """Up to ``k`` disjoint random pairs with rho < ``rho_hi`` (H3a)."""
    n = rho.shape[0]
    cands = [(i, j) for i in range(n) for j in range(i + 1, n) if math.isfinite(rho[i, j]) and rho[i, j] < rho_hi]
    order = rng.permutation(len(cands)) if cands else []
    return _greedy_disjoint([cands[t] for t in order], k)


def control_cross_sector(
    rho: np.ndarray,
    rho_e: np.ndarray,
    labels: Sequence[int],
    k: int,
    *,
    rho_min: float = 0.80,
    rho_max: float = 0.98,
    rho_e_max: float = 0.10,
) -> list[tuple[int, int]]:
    """Up to ``k`` disjoint pairs with high rho but different clusters and rho_e < ``rho_e_max``, by descending rho (H3b)."""
    n = rho.shape[0]
    cands = [
        (float(rho[i, j]), i, j)
        for i in range(n)
        for j in range(i + 1, n)
        if labels[i] != labels[j] and math.isfinite(rho[i, j]) and rho_min <= rho[i, j] < rho_max and rho_e[i, j] < rho_e_max
    ]
    cands.sort(key=lambda c: (-c[0], c[1], c[2]))
    return _greedy_disjoint([(i, j) for _, i, j in cands], k)


def rolling_z(s: np.ndarray, *, window: int = 60, min_obs: int = 55) -> np.ndarray:
    """z_t = (s_t - mean) / sd over s_{t-window+1..t} (finite values, sample sd); ``nan`` if s_t is missing."""
    x = np.asarray(s, dtype=float)
    out = np.full(x.size, np.nan)
    for t in range(x.size):
        if not math.isfinite(x[t]):
            continue
        w = x[max(0, t - window + 1) : t + 1]
        w = w[np.isfinite(w)]
        if w.size < min_obs:
            continue
        sd = float(w.std(ddof=1))
        if sd > 0:
            out[t] = (x[t] - float(w.mean())) / sd
    return out


@dataclass(frozen=True)
class Params:
    z_in: float = 2.0
    z_out: float = 0.5
    z_stop: float = 4.0
    pnl_stop: float = -0.15
    max_hold: int = 10
    stops: bool = True


@dataclass(frozen=True)
class Trade:
    signal: int  # day of the signal close
    entry: int  # day whose open is the entry
    exit: int  # day whose open is the exit (or one past the last close when truncated)
    side: int  # +1 long A / short B, -1 the opposite
    price: float  # price P&L per unit of mean leg notional
    funding: float  # funding P&L, same unit
    reason: str  # target | stop | time | end
    truncated: bool  # a leg had no exit open and was closed at its last earlier close

    def net(self, cost: float) -> float:
        return self.price + self.funding - cost


def _exit_px(o: np.ndarray, c: np.ndarray, entry: int, x: int) -> tuple[float, bool]:
    if x < o.size and math.isfinite(o[x]):
        return float(o[x]), False
    for t in range(min(x, c.size) - 1, entry - 1, -1):
        if math.isfinite(c[t]):
            return float(c[t]), True
    return float(o[entry]), True


def close_trade(
    oa: np.ndarray, ca: np.ndarray, ob: np.ndarray, cb: np.ndarray, fa: np.ndarray, fb: np.ndarray,
    *, w_a: float, w_b: float, signal: int, entry: int, exit: int, side: int, reason: str,
) -> Trade:  # fmt: skip
    """P&L of a pair position opened at the open of ``entry`` and closed at the open of ``exit``."""
    pa, ta = _exit_px(oa, ca, entry, exit)
    pb, tb = _exit_px(ob, cb, entry, exit)
    price = side * (w_a * (pa / oa[entry] - 1.0) - w_b * (pb / ob[entry] - 1.0))
    hi = min(exit, fa.size)
    fsa = float(np.nansum(fa[entry:hi]))
    fsb = float(np.nansum(fb[entry:hi]))
    funding = side * (-w_a * fsa + w_b * fsb)  # the long leg pays the rate, the short leg receives it
    return Trade(signal, entry, exit, side, float(price), funding, reason, ta or tb)


def simulate_pair(
    oa: np.ndarray, ca: np.ndarray, ob: np.ndarray, cb: np.ndarray, fa: np.ndarray, fb: np.ndarray, z: np.ndarray,
    *, w_a: float, w_b: float, sig_lo: int, sig_hi: int, free_from: int = 0, p: Params = Params(),
) -> tuple[list[Trade], int]:  # fmt: skip
    """Non-overlapping trades of one pair from signals on closes of days [sig_lo, sig_hi); entry at the next open.

    Returns the trades and the first day whose close may signal again (the last exit day), so a pair reselected the
    next month continues without overlap."""
    T = z.size
    trades: list[Trade] = []
    d0 = max(sig_lo, free_from)
    while d0 < sig_hi:
        zd = z[d0]
        e = d0 + 1
        if not (math.isfinite(zd) and abs(zd) >= p.z_in) or e >= T or not (math.isfinite(oa[e]) and math.isfinite(ob[e])):
            d0 += 1
            continue
        side = -1 if zd > 0 else 1
        x, reason = e + p.max_hold, "time"
        for d in range(e, min(e + p.max_hold, T)):
            if not (math.isfinite(ca[d]) and math.isfinite(cb[d])):
                continue
            zz = z[d]
            pnl = side * (w_a * (ca[d] / oa[e] - 1.0) - w_b * (cb[d] / ob[e] - 1.0))
            if math.isfinite(zz) and abs(zz) <= p.z_out:
                x, reason = d + 1, "target"
                break
            if p.stops and ((math.isfinite(zz) and abs(zz) >= p.z_stop) or pnl <= p.pnl_stop):
                x, reason = d + 1, "stop"
                break
        if x >= T:
            x, reason = T, "end"
        trades.append(close_trade(oa, ca, ob, cb, fa, fb, w_a=w_a, w_b=w_b, signal=d0, entry=e, exit=x, side=side, reason=reason))
        free_from = d0 = x  # x >= d0 + 2: the next signal may come at the close of the exit day
    return trades, free_from


def cluster_se(values: Sequence[float], clusters: Sequence[Any]) -> float:
    """Cluster-robust SE of the mean: sqrt(G/(G-1) * sum_g (S_g - n_g m)^2) / n."""
    v = np.asarray(values, dtype=float)
    n = v.size
    if n < 2:
        return float("nan")
    keys = {c: i for i, c in enumerate(dict.fromkeys(clusters))}
    g = np.fromiter((keys[c] for c in clusters), dtype=np.int64, count=n)
    G = len(keys)
    if G < 2:
        return float("nan")
    m = v.mean()
    s = np.bincount(g, weights=v - m, minlength=G)
    return math.sqrt(G / (G - 1) * float(np.sum(s * s))) / n


def cluster_bootstrap_ci(
    values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261025, alpha: float = 0.05
) -> tuple[float, float]:
    """Percentile CI of the trade-weighted mean, resampling whole clusters with replacement."""
    v = np.asarray(values, dtype=float)
    if v.size == 0:
        return float("nan"), float("nan")
    keys = {c: i for i, c in enumerate(dict.fromkeys(clusters))}
    g = np.fromiter((keys[c] for c in clusters), dtype=np.int64, count=v.size)
    G = len(keys)
    s = np.bincount(g, weights=v, minlength=G)
    k = np.bincount(g, minlength=G).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, G, size=(n_boot, G))
    means = s[idx].sum(axis=1) / k[idx].sum(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))
