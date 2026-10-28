"""Taker-flow imbalance from the Binance archive ``metrics`` taker ratio (IDEA-20261028-taker-imbalance-8h-continuation).

Pure functions, no I/O; time is always a parameter (epoch ms). Rates and returns are decimals (0.0005 = 5 bp).

The archive ratio ``sum_taker_long_short_vol_ratio`` is taker buy volume / taker sell volume over one 5-minute
interval. Checked against 1m kline taker volumes of BTCUSDT (2024-03-02..05, 2026-09-20): before 2024-03-04 the row
stamped ``ct`` covers ``[ct - 5 min, ct)``; from 2024-03-04 it covers ``[ct, ct + 5 min)``. In both regimes the
interval ends at the observation time of ``analytics.oi_flush.archive_obs_times``, so "rows observed <= t" never use
trades at or after ``t``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

H_MS = 3_600_000
M5_MS = 300_000


def imbalance(ratio: Sequence[float] | np.ndarray) -> np.ndarray:
    """(r - 1) / (r + 1) for a buy/sell ratio r: in [-1, 1], 0 = balanced; NaN for missing or non-positive r."""
    r = np.asarray(ratio, dtype=float)
    out = np.full(r.shape, np.nan)
    ok = np.isfinite(r) & (r > 0)
    out[ok] = (r[ok] - 1.0) / (r[ok] + 1.0)
    return out


def window_mean(
    obs_ms: np.ndarray,
    values: np.ndarray,
    t_ms: int,
    *,
    hours: float = 4.0,
    min_bars: int = 36,
    weights: np.ndarray | None = None,
) -> float:
    """Mean of ``values`` over rows observed in ``(t - hours, t]`` (``obs_ms`` sorted); weighted if ``weights`` given.

    Rows with a non-finite value (or non-finite / non-positive weight) are skipped; NaN with fewer than ``min_bars``
    usable rows. Nothing observed after ``t`` is read."""
    lo = int(np.searchsorted(obs_ms, t_ms - int(hours * H_MS), side="right"))
    hi = int(np.searchsorted(obs_ms, t_ms, side="right"))
    v = values[lo:hi]
    ok = np.isfinite(v)
    if weights is not None:
        w = weights[lo:hi]
        ok &= np.isfinite(w) & (w > 0)
        if ok.sum() < min_bars:
            return float("nan")
        return float(np.sum(v[ok] * w[ok]) / np.sum(w[ok]))
    if ok.sum() < min_bars:
        return float("nan")
    return float(np.mean(v[ok]))


def standardize_prior(raw: Sequence[float] | np.ndarray, *, lookback: int = 90, min_obs: int = 60) -> np.ndarray:
    """z[k] = raw[k] / SD(raw[k - lookback : k]) (sample SD of the finite prior values only; no demeaning).

    NaN when raw[k] is missing or fewer than ``min_obs`` prior values exist. 90 windows = 30 days at 3 windows/day."""
    x = np.asarray(raw, dtype=float)
    out = np.full(x.shape, np.nan)
    for k in range(x.size):
        if not math.isfinite(x[k]):
            continue
        h = x[max(0, k - lookback) : k]
        h = h[np.isfinite(h)]
        if h.size < min_obs:
            continue
        sd = float(np.std(h, ddof=1))
        if sd > 0:
            out[k] = x[k] / sd
    return out


def rolling_beta(y: np.ndarray, x: np.ndarray, at: Sequence[int], *, window: int, min_obs: int) -> np.ndarray:
    """OLS beta of ``y`` on ``x`` over the ``window`` observations ending before index ``a`` (``y[a - window : a]``).

    Pairs with a non-finite member are skipped; NaN with fewer than ``min_obs`` pairs or zero variance of ``x``."""
    ok = np.isfinite(y) & np.isfinite(x)
    yy, xx = np.where(ok, y, 0.0), np.where(ok, x, 0.0)
    c = [np.concatenate([[0.0], np.cumsum(a)]) for a in (ok.astype(float), xx, yy, xx * yy, xx * xx)]
    out = np.full(len(at), np.nan)
    for j, a in enumerate(at):
        lo, hi = max(0, a - window), max(0, min(a, y.size))
        n, sx, sy, sxy, sxx = (cc[hi] - cc[lo] for cc in c)
        if n < min_obs:
            continue
        var = sxx - sx * sx / n
        if var > 0:
            out[j] = (sxy - sx * sy / n) / var
    return out


def xs_residual(s: Sequence[float], x: Sequence[float]) -> np.ndarray:
    """Residual of a cross-sectional OLS of ``s`` on ``x`` with intercept (H3: imbalance net of own return)."""
    a, b = np.asarray(s, dtype=float), np.asarray(x, dtype=float)
    X = np.column_stack([np.ones(b.size), b])
    coef, *_ = np.linalg.lstsq(X, a, rcond=None)
    return a - X @ coef


def legs(signal: Mapping[str, float], k: int = 3) -> tuple[list[str], list[str]]:
    """(longs, shorts): the ``k`` highest and ``k`` lowest finite signals (ties broken by symbol name)."""
    xs = sorted((v, s) for s, v in signal.items() if math.isfinite(v))
    if len(xs) < 2 * k:
        return [], []
    return [s for _, s in xs[-k:]], [s for _, s in xs[:k]]


@dataclass(frozen=True)
class Costs:
    """Registered execution costs per fill (decimals): maker 2 bp, taker 5 bp, slippage 1 bp, 30 % maker misses."""

    maker: float = 0.0002
    taker: float = 0.0005
    slip: float = 0.0001
    p_miss: float = 0.30

    def fill_e1(self) -> float:
        """E1 (card model): expected cost of one fill = (1 - p) maker + p taker + slippage."""
        return (1 - self.p_miss) * self.maker + self.p_miss * self.taker + self.slip

    def round_trip_e1(self) -> float:
        return 2 * self.fill_e1()

    def taker_round_trip(self) -> float:
        return 2 * (self.taker + self.slip)


def fill_e2(side: int, bar_open: float, bar_high: float, bar_low: float, bar_close: float, c: Costs) -> tuple[float, float]:
    """E2 (bar model, adverse selection): limit at the bar open, ``side`` +1 buys / -1 sells.

    A buy fills at the open only if the bar trades strictly below it (a sell: strictly above), paying the maker fee;
    otherwise the order is taken at the bar close with slippage and the taker fee. Returns (price, fee)."""
    if (side > 0 and bar_low < bar_open) or (side < 0 and bar_high > bar_open):
        return bar_open, c.maker
    return bar_close * (1 + side * c.slip), c.taker


def leg_return_e2(side: int, entry_bar: Sequence[float], exit_bar: Sequence[float], c: Costs) -> float:
    """Net raw return of a ``side`` (+1 long / -1 short) leg entered in ``entry_bar`` and closed in ``exit_bar``.

    Bars are ``(open, high, low, close)``; entry and exit are both E2 limit orders (exit side = -side)."""
    p0, f0 = fill_e2(side, *entry_bar, c)
    p1, f1 = fill_e2(-side, *exit_bar, c)
    return side * (p1 / p0 - 1.0) - f0 - f1


def funding_sum(calc_ms: np.ndarray, rates: np.ndarray, t0_ms: int, t1_ms: int) -> float:
    """Sum of funding rates settled in ``(t0, t1]`` (paid by longs when positive); ``calc_ms`` sorted."""
    lo = int(np.searchsorted(calc_ms, t0_ms, side="right"))
    hi = int(np.searchsorted(calc_ms, t1_ms, side="right"))
    return float(np.sum(rates[lo:hi]))


def block_bootstrap_mean_ci(
    xs: Sequence[float], *, block: int, n_boot: int = 5000, seed: int = 20261028, alpha: float = 0.05
) -> tuple[float, float]:
    """Percentile CI of the mean by the moving-block bootstrap (circular blocks of ``block`` consecutive values)."""
    x = np.asarray(xs, dtype=float)
    n = x.size
    if n < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    nb = -(-n // block)
    ext = np.concatenate([x, x[:block]])
    cs = np.concatenate([[0.0], np.cumsum(ext)])
    means = np.empty(n_boot)
    for b in range(n_boot):
        st = rng.integers(0, n, nb)
        tot = cs[st + block] - cs[st]
        # the last block is cut so that exactly n values enter the mean
        last = n - (nb - 1) * block
        tot[-1] = cs[st[-1] + last] - cs[st[-1]]
        means[b] = tot.sum() / n
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def cochran_q(means: Sequence[float], ses: Sequence[float]) -> tuple[float, int, float]:
    """(Q, df, I^2) for per-group means with standard errors (inverse-variance weights)."""
    m, s = np.asarray(means, dtype=float), np.asarray(ses, dtype=float)
    ok = np.isfinite(m) & np.isfinite(s) & (s > 0)
    m, w = m[ok], 1.0 / s[ok] ** 2
    if m.size < 2:
        return float("nan"), 0, float("nan")
    mu = float(np.sum(w * m) / np.sum(w))
    q = float(np.sum(w * (m - mu) ** 2))
    df = m.size - 1
    return q, df, max(0.0, (q - df) / q) if q > 0 else 0.0
