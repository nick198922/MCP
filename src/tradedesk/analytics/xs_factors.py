"""Cross-sectional price factors of USDT-M perps: weekly rank IC of R7, M28 (skip 7), MAX30 and IVOL30.

Pure functions (no I/O). Time is passed in as UTC day indices (``ms // DAY_MS``); the rank day ``t`` is a Monday and a
signal uses only bars of days ``< t`` (the close of day ``t - 1`` is the price at ``t`` 00:00 UTC). Returns are
decimals (0.01 = 1 %). Idea card: docs/ideas/IDEA-20261018-xs-factor-ic-panel.md (section "Попередня реєстрація").

Bars are ``funding_crowding.Bars``; the universe and the weekly leg (open ``t`` -> close ``t + 6``, truncated at the
last close for a symbol delisted inside the week) are reused from ``funding_crowding`` unchanged.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

from .funding_crowding import Bars

SIGNALS = ("R7", "M28", "MAX30", "IVOL30")


def close_near(bars: Bars, day: int, *, tol: int = 3) -> float | None:
    """Close of ``day`` or of the nearest earlier day within ``tol`` days (never a later day); None if there is none."""
    for d in range(day, day - tol - 1, -1):
        if d in bars.close:
            return bars.close[d]
    return None


def window_return(bars: Bars, t: int, start_lag: int, end_lag: int, *, tol: int = 3) -> float | None:
    """C(t - end_lag) / C(t - start_lag) - 1 with ``start_lag > end_lag >= 1`` (closes of days before ``t`` only)."""
    if end_lag < 1 or start_lag <= end_lag:
        raise ValueError("need start_lag > end_lag >= 1")
    a = close_near(bars, t - start_lag, tol=tol)
    b = close_near(bars, t - end_lag, tol=tol)
    if a is None or b is None or a <= 0:
        return None
    return b / a - 1.0


def daily_returns(bars: Bars, t: int, window: int = 30) -> dict[int, float]:
    """Close-to-close returns of days ``d`` in ``[t - window, t - 1]`` where days ``d`` and ``d - 1`` both have a close."""
    out: dict[int, float] = {}
    for d in range(t - window, t):
        if d in bars.close and d - 1 in bars.close and bars.close[d - 1] > 0:
            out[d] = bars.close[d] / bars.close[d - 1] - 1.0
    return out


def max_return(bars: Bars, t: int, *, window: int = 30, min_days: int = 20) -> float | None:
    """MAX30: the largest daily return in the window; None with fewer than ``min_days`` returns."""
    r = daily_returns(bars, t, window)
    return max(r.values()) if len(r) >= min_days else None


def beta_ivol(bars: Bars, btc: Bars, t: int, *, window: int = 30, min_days: int = 20) -> tuple[float, float] | None:
    """(beta, IVOL) from OLS of the symbol's daily returns on BTC's over the window; IVOL = SD of residuals (ddof 2).

    Daily decimals (IVOL 0.03 = 3 % a day). None with fewer than ``min_days`` common days or a constant BTC series."""
    ra, rb = daily_returns(bars, t, window), daily_returns(btc, t, window)
    days = sorted(set(ra) & set(rb))
    if len(days) < min_days:
        return None
    y = [ra[d] for d in days]
    x = [rb[d] for d in days]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx <= 0:
        return None
    b = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True)) / sxx
    a = my - b * mx
    sse = sum((yi - a - b * xi) ** 2 for xi, yi in zip(x, y, strict=True))
    return b, math.sqrt(max(sse, 0.0) / (len(days) - 2))


@dataclass(frozen=True)
class Signals:
    r7: float
    m28: float
    max30: float
    ivol30: float
    beta30: float

    def get(self, name: str) -> float:
        return {"R7": self.r7, "M28": self.m28, "MAX30": self.max30, "IVOL30": self.ivol30}[name]


def signals_at(bars: Bars, btc: Bars, t: int) -> Signals | None:
    """All four registered signals at rank day ``t`` (data of days < t only); None if any is undefined."""
    r7 = window_return(bars, t, 8, 1)
    m28 = window_return(bars, t, 29, 8)
    mx = max_return(bars, t)
    bi = beta_ivol(bars, btc, t)
    if r7 is None or m28 is None or mx is None or bi is None:
        return None
    return Signals(r7, m28, mx, bi[1], bi[0])


# ---------------------------------------------------------------- cross-sectional statistics


def rank_z(xs: Sequence[float]) -> np.ndarray:
    """z-scores of average ranks (population SD); zeros when all values are equal."""
    a = np.asarray(xs, dtype=float)
    order = np.argsort(a, kind="stable")
    r = np.empty(a.size)
    r[order] = np.arange(1, a.size + 1, dtype=float)
    vals, inv, cnt = np.unique(a, return_inverse=True, return_counts=True)
    if vals.size < a.size:  # average ranks for ties
        sums = np.bincount(inv, weights=r)
        r = (sums / cnt)[inv]
    sd = r.std()
    return (r - r.mean()) / sd if sd > 0 else np.zeros(a.size)


def rank_ic(signal: Sequence[float], returns: Sequence[float]) -> float:
    """Spearman correlation (average ranks)."""
    a, b = rank_z(signal), rank_z(returns)
    return float(np.mean(a * b)) if a.size else float("nan")


def residualize(y: np.ndarray, controls: Sequence[np.ndarray]) -> np.ndarray:
    """Residual of OLS of ``y`` on [1, controls...]."""
    x = np.column_stack([np.ones(y.size), *controls]) if controls else np.ones((y.size, 1))
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    return y - x @ coef


def partial_rank_ic(signal: Sequence[float], returns: Sequence[float], controls: Sequence[Sequence[float]]) -> float:
    """Pearson correlation of rank-z(signal) and rank-z(returns) after both are residualised on rank-z(controls)."""
    cz = [rank_z(c) for c in controls]
    a = residualize(rank_z(signal), cz)
    b = residualize(rank_z(returns), cz)
    den = math.sqrt(float(np.sum(a * a) * np.sum(b * b)))
    return float(np.sum(a * b) / den) if den > 0 else 0.0


def ic_products(signal: Sequence[float], returns: Sequence[float]) -> np.ndarray:
    """Per-symbol z(rank signal) * z(rank return); their mean over the week is the Spearman IC (asset contributions)."""
    return rank_z(signal) * rank_z(returns)


def placebo_mean_ics(weeks: Sequence[tuple[Sequence[float], Sequence[float]]], n_perm: int, *, seed: int = 7) -> np.ndarray:
    """Mean weekly IC under ``n_perm`` shuffles of the signal across symbols inside every week.

    ``weeks``: (signal, returns) per week. The shuffled-signal IC equals the correlation of permuted signal ranks with
    return ranks, so ranks are computed once per week."""
    rng = np.random.default_rng(seed)
    acc = np.zeros(n_perm)
    for sig, ret in weeks:
        zs, zr = rank_z(sig), rank_z(ret)
        n = zs.size
        idx = np.argsort(rng.random((n_perm, n)), axis=1)
        acc += (zs[idx] @ zr) / n
    return acc / max(len(weeks), 1)


def holm_z(m: int, *, alpha: float = 0.05, power: float = 0.8) -> float:
    """z multiplier of the MDE for the strictest Holm step over ``m`` two-sided tests: z_{1 - alpha/(2m)} + z_power."""
    nd = NormalDist()
    return nd.inv_cdf(1.0 - alpha / (2.0 * m)) + nd.inv_cdf(power)


def two_sided_p(t: float) -> float:
    return math.erfc(abs(t) / math.sqrt(2.0))


def without_top(xs: Sequence[float], k: int, sign: float) -> list[float]:
    """``xs`` without the ``k`` values largest in the direction ``sign`` (+1: largest, -1: smallest)."""
    order = sorted(range(len(xs)), key=lambda i: -sign * xs[i])
    drop = set(order[:k])
    return [x for i, x in enumerate(xs) if i not in drop]


def rolling_means(xs: Sequence[float], window: int) -> list[float]:
    """Means of every window of ``window`` consecutive values."""
    if len(xs) < window:
        return []
    c = np.cumsum(np.concatenate([[0.0], np.asarray(xs, dtype=float)]))
    return list((c[window:] - c[:-window]) / window)


def oriented_signal(sig: Mapping[str, float], sign: float) -> dict[str, float]:
    """Signal for a basket that longs the low end: ``-sign * s`` (sign +1 -> long the high-signal symbols)."""
    return {k: -sign * v for k, v in sig.items()}
