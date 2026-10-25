"""Expiry pinning with a mirror-strike control (IDEA-20261025-deribit-expiry-pin-mirror-control).

Pure functions only (no I/O); every time is a parameter. Distances and Delta are decimals of the spot S0.

K* = the strike of one expiry with the largest cumulative gross traded volume (calls + puts) known at T0.
M = 2 S0 - K* is the mirror strike at the same distance on the other side of the spot. The statistic
Delta = (|settle - M| - |settle - K*|) / S0 is positive when the settlement lands closer to K* than to M;
without pinning (and without drift) its expectation is 0 by symmetry. Delta = 2 clip(u, -d, d), where d =
|K* - S0| / S0 and u is the move of the settlement from S0 towards K* in units of S0.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from tradedesk.analytics.gex_flow import FlowBook, parse_option


def strike_volume(book: FlowBook, expiry_ms: int) -> dict[float, float]:
    """Cumulative gross volume (BTC, calls + puts) per strike of one expiry from the book's series."""
    out: dict[float, float] = {}
    for name, st in book.series.items():
        s = parse_option(name)
        if s is None or s.expiry_ms != expiry_ms or st.gross <= 0:
            continue
        out[s.strike] = out.get(s.strike, 0.0) + st.gross
    return out


@dataclass(frozen=True)
class PinRow:
    t0_ms: int
    expiry_ms: int
    spot: float
    spot_age_ms: int
    k_star: float
    share: float  # volume of K* / total volume of the expiry (concentration)
    k_atm: float  # traded strike of the expiry nearest to the spot
    total: float  # total gross volume of the expiry, BTC
    n_strikes: int


def pick_k_star(vol: Mapping[float, float], spot: float) -> tuple[float, float] | None:
    """(K*, share of K* in total volume); ties broken by distance to ``spot``, then by the lower strike."""
    tot = sum(v for v in vol.values() if v > 0)
    if tot <= 0:
        return None
    k = min((k for k, v in vol.items() if v > 0), key=lambda k: (-vol[k], abs(k - spot), k))
    return k, vol[k] / tot


def pin_row(book: FlowBook, t0_ms: int, expiry_ms: int) -> PinRow | None:
    """K* of ``expiry_ms`` at ``t0_ms`` from the book. Raises ValueError if the book holds prints after t0."""
    if book.last_ts > t0_ms:
        raise ValueError("book contains prints after t0")
    if book.spot <= 0:
        return None
    vol = strike_volume(book, expiry_ms)
    ks = pick_k_star(vol, book.spot)
    if ks is None:
        return None
    k_atm = min(vol, key=lambda k: (abs(k - book.spot), k))
    return PinRow(t0_ms, expiry_ms, book.spot, t0_ms - book.spot_ts, ks[0], ks[1], k_atm, sum(vol.values()), len(vol))


def distance(k: float, spot: float) -> float:
    return abs(k - spot) / spot


def in_band(k: float, spot: float, lo: float, hi: float) -> bool:
    return lo <= distance(k, spot) <= hi


def mirror_delta(k: float, spot: float, settle: float) -> float:
    """(|settle - M| - |settle - K|) / spot with M = 2 spot - K."""
    m = 2.0 * spot - k
    return (abs(settle - m) - abs(settle - k)) / spot


def balanced_mean(values: Sequence[float] | np.ndarray, above: Sequence[bool] | np.ndarray) -> float:
    """Half the mean of the K*-above-spot rows plus half the mean of the below rows (nan if a side is empty)."""
    v = np.asarray(values, dtype=float)
    a = np.asarray(above, dtype=bool)
    if not a.any() or a.all():
        return math.nan
    return 0.5 * float(v[a].mean()) + 0.5 * float(v[~a].mean())


def cluster_bootstrap(
    stat: Callable[[np.ndarray], float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261025
) -> np.ndarray:
    """Bootstrap distribution of ``stat(indices)``, resampling whole clusters with replacement."""
    groups: dict[Any, list[int]] = {}
    for i, c in enumerate(clusters):
        groups.setdefault(c, []).append(i)
    idx = [np.asarray(g, dtype=int) for g in groups.values()]
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.integers(0, len(idx), len(idx))
        out[b] = stat(np.concatenate([idx[j] for j in pick]))
    return out


def summarize_boot(point: float, boot: np.ndarray) -> dict[str, float]:
    """Point, SE (SD of the bootstrap), percentile 95 % CI and the normal two-sided p of point / SE."""
    b = boot[np.isfinite(boot)]
    se = float(b.std(ddof=1)) if b.size > 1 else math.nan
    lo, hi = (float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5))) if b.size else (math.nan, math.nan)
    p = math.erfc(abs(point) / se / math.sqrt(2.0)) if se and se > 0 and math.isfinite(point) else math.nan
    return {"point": point, "se": se, "lo": lo, "hi": hi, "p": p}


def holm(pvals: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values (monotone, capped at 1), in the input order."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    run = 0.0
    for r, i in enumerate(order):
        run = max(run, min(1.0, (m - r) * pvals[i]))
        adj[i] = run
    return adj


def mde(se: float, z: float = 2.8) -> float:
    """Minimum detectable effect at 5 % two-sided and 80 % power (z = 1.96 + 0.84)."""
    return z * se


def ols_cluster(y: Sequence[float], X: Sequence[Sequence[float]], clusters: Sequence[Any]) -> tuple[np.ndarray, np.ndarray]:
    """OLS coefficients and cluster-robust (CR1) standard errors; ``X`` includes the constant column."""
    Xm = np.asarray(X, dtype=float)
    yv = np.asarray(y, dtype=float)
    n, k = Xm.shape
    beta = np.linalg.lstsq(Xm, yv, rcond=None)[0]
    e = yv - Xm @ beta
    bread = np.linalg.inv(Xm.T @ Xm)
    meat = np.zeros((k, k))
    groups: dict[Any, list[int]] = {}
    for i, c in enumerate(clusters):
        groups.setdefault(c, []).append(i)
    for ii in groups.values():
        s = Xm[ii].T @ e[ii]
        meat += np.outer(s, s)
    g = len(groups)
    adj = g / (g - 1) * (n - 1) / (n - k) if g > 1 and n > k else 1.0
    cov = adj * bread @ meat @ bread
    return beta, np.sqrt(np.clip(np.diag(cov), 0.0, None))


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def attach_placebo(rows: Sequence[Mapping[str, Any]]) -> list[float | None]:
    """K* of the previous calendar day's row (its own expiry) for each row, None if that day has no row.

    Rows carry ``d`` (ISO date of D) and ``k_star``; uses only the row of D - 1 (earlier data).
    """
    by_d = {str(r["d"]): r for r in rows}
    out: list[float | None] = []
    for r in rows:
        prev = date.fromordinal(date.fromisoformat(str(r["d"])).toordinal() - 1).isoformat()
        p = by_d.get(prev)
        out.append(float(p["k_star"]) if p is not None else None)
    return out
