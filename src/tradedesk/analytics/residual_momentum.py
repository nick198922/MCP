"""Residual (BTC-beta-cleaned) momentum of USDT-M perps and its short leg.

Pure functions (no I/O). Time is a UTC day index ``t`` (a Monday); a signal uses only closes of days ``< t``. Returns
are decimals (0.01 = 1 %). Idea card: docs/ideas/IDEA-20261029-residual-momentum-short-leg.md ("Попередня реєстрація").

RM (Blitz-Huij-Martens style, one factor = BTC): beta from the OLS of daily returns on BTC's over the estimation window
``[t - skip - est; t - skip - 1]`` (default ``[t-91; t-8]``); residual ``e_d = r_d - beta * r_BTC,d`` (no intercept, so a
coin's own drift stays in the signal); RM = sum of ``e_d`` over the formation window ``[t - skip - form; t - skip - 1]``
divided by the SD of ``e_d`` over the estimation window. The last ``skip`` days are left out (weekly reversal).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .funding_crowding import Bars
from .xs_factors import daily_returns, rank_z


def ols_slope(x: Sequence[float], y: Sequence[float]) -> float | None:
    """OLS slope of ``y`` on ``x`` (with intercept); None with < 3 points or a constant ``x``."""
    if len(x) != len(y) or len(x) < 3:
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx <= 0:
        return None
    return sum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True)) / sxx


@dataclass(frozen=True)
class ResidualMomentum:
    rm: float  # sum of formation-window residuals / SD of estimation-window residuals (unitless)
    beta: float  # BTC beta over the estimation window
    resid_sd: float  # SD (ddof 1) of daily residuals over the estimation window, daily decimal
    n_est: int  # residual days in the estimation window
    n_form: int  # residual days in the formation window


def residual_momentum(
    bars: Bars,
    btc: Bars,
    t: int,
    *,
    form: int = 28,
    skip: int = 7,
    est: int = 84,
    min_est: int = 60,
    min_form: int = 20,
) -> ResidualMomentum | None:
    """RM at rank day ``t`` from days ``[t - skip - est; t - skip - 1]`` only; None if a window is too thin.

    ``form`` must not exceed ``est`` (the formation window lies inside the estimation window)."""
    if form > est or skip < 0:
        raise ValueError("need form <= est and skip >= 0")
    end = t - skip  # first day left out
    ra, rb = daily_returns(bars, end, est), daily_returns(btc, end, est)
    days = sorted(set(ra) & set(rb))
    if len(days) < min_est:
        return None
    b = ols_slope([rb[d] for d in days], [ra[d] for d in days])
    if b is None:
        return None
    e = {d: ra[d] - b * rb[d] for d in days}
    sd = statistics.stdev(e.values())
    if not sd > 1e-12:
        return None
    fd = [d for d in days if d >= end - form]
    if len(fd) < min_form:
        return None
    return ResidualMomentum(sum(e[d] for d in fd) / sd, b, sd, len(days), len(fd))


def hedge_beta(bars: Bars, btc: Bars, t: int, *, window: int = 91, min_days: int = 60) -> float | None:
    """BTC beta for the forward hedge: OLS slope of daily returns of days ``[t - window; t - 1]``."""
    ra, rb = daily_returns(bars, t, window), daily_returns(btc, t, window)
    days = sorted(set(ra) & set(rb))
    if len(days) < min_days:
        return None
    return ols_slope([rb[d] for d in days], [ra[d] for d in days])


# ---------------------------------------------------------------- weighted cross-sectional statistics


def inverse_vol_weights(vols: Sequence[float]) -> np.ndarray:
    """Weights proportional to 1 / vol, summing to 1."""
    v = np.asarray(vols, dtype=float)
    if v.size == 0 or np.any(~(v > 0)):
        raise ValueError("vols must be positive")
    w = 1.0 / v
    return w / w.sum()


def _wls_resid(y: np.ndarray, controls: Sequence[np.ndarray], w: np.ndarray) -> np.ndarray:
    x = np.column_stack([np.ones(y.size), *controls])
    sw = np.sqrt(w)
    coef, *_ = np.linalg.lstsq(x * sw[:, None], y * sw, rcond=None)
    return y - x @ coef


def weighted_partial_rank_ic(
    signal: Sequence[float], returns: Sequence[float], controls: Sequence[Sequence[float]], weights: Sequence[float]
) -> float:
    """Weighted correlation of rank-z(signal) and rank-z(returns) after WLS residualisation of both on [1, rank-z
    controls] with the same weights. Equal weights give ``xs_factors.partial_rank_ic``; no controls give a weighted
    Spearman correlation."""
    w = np.asarray(weights, dtype=float)
    w = w / w.sum()
    cz = [rank_z(c) for c in controls]
    a = _wls_resid(rank_z(signal), cz, w)
    b = _wls_resid(rank_z(returns), cz, w)
    den = math.sqrt(float(np.sum(w * a * a) * np.sum(w * b * b)))
    return float(np.sum(w * a * b) / den) if den > 0 else 0.0


# ---------------------------------------------------------------- staggered book, placebo


def staggered_book(paths: Sequence[Sequence[float]], hold: int) -> list[float]:
    """Weekly return of a book that forms one cohort per week and holds each for ``hold`` weeks (1 / hold of capital
    per cohort). ``paths[i][k]`` is the return of the cohort formed in week ``i`` during its ``k``-th week. Output
    week ``j`` (from ``hold - 1``, when the book is full) = mean over ``k`` of ``paths[j - k][k]``."""
    if hold < 1:
        raise ValueError("hold >= 1")
    out = []
    for j in range(hold - 1, len(paths)):
        out.append(sum(paths[j - k][k] for k in range(hold)) / hold)
    return out


def placebo_shift_means(ic_matrix: np.ndarray, n_perm: int, *, seed: int = 7) -> np.ndarray:
    """Mean weekly IC when every week's signal is replaced by the signal of a randomly shifted earlier week.

    ``ic_matrix[t, j]``: IC of the signal of week ``t - shift_j`` against the returns of week ``t`` (nan when the
    shifted week does not exist). Each permutation draws one shift per week uniformly; the mean ignores nans."""
    m = np.asarray(ic_matrix, dtype=float)
    rng = np.random.default_rng(seed)
    n_w, n_s = m.shape
    pick = rng.integers(0, n_s, size=(n_perm, n_w))
    vals = m[np.arange(n_w)[None, :], pick]
    ok = ~np.isnan(vals)
    return np.where(ok, vals, 0.0).sum(axis=1) / np.maximum(ok.sum(axis=1), 1)
