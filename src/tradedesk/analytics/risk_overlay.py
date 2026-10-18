"""Exposure overlays for a long spot position: volatility targeting and a Donchian trend ensemble.

Research code for ``IDEA-20261018-risk-overlay-vol-target-trend``. Pure functions on numpy arrays, no I/O. Index
``d`` is a daily bar; ``closes[d]`` is the close of day d (the price at 00:00 UTC of day d + 1). A signal at index d
uses closes up to and including d; the position held over day j is ``signal[j - lag]`` (registered lag 2: known at
close j - 2, executed at close j - 1). Rates and returns are decimals (0.10 = 10 %); volatility is annualised with
sqrt(365). Returns are on capital (spot, no leverage unless a cap above 1 is passed); cash earns nothing.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from tradedesk.analytics.per_asset import chi2_sf

ANN = 365.0


# ----------------------------------------------------------------------------- signals


def log_returns(closes: Sequence[float]) -> np.ndarray:
    """r[d] = ln(C[d] / C[d-1]); r[0] = nan."""
    c = np.asarray(closes, dtype=float)
    r = np.full(c.size, np.nan)
    if c.size > 1:
        r[1:] = np.log(c[1:] / c[:-1])
    return r


def ewma_vol(r: Sequence[float], lam: float = 0.94, *, init_n: int = 20, ann: float = ANN) -> np.ndarray:
    """RiskMetrics EWMA volatility, annualised: v[d] = lam * v[d-1] + (1 - lam) * r[d]^2 (zero mean).

    r[0] is ignored (nan). The recursion starts at index ``init_n`` with v = mean(r[1..init_n]^2); earlier values are
    nan. The value at d uses returns up to and including d."""
    x = np.asarray(r, dtype=float)
    out = np.full(x.size, np.nan)
    if x.size <= init_n:
        return out
    v = float(np.mean(x[1 : init_n + 1] ** 2))
    out[init_n] = math.sqrt(ann * v)
    for d in range(init_n + 1, x.size):
        v = lam * v + (1.0 - lam) * x[d] * x[d]
        out[d] = math.sqrt(ann * v)
    return out


def rolling_vol(r: Sequence[float], window: int | None = 365, *, min_n: int = 200, ann: float = ANN) -> np.ndarray:
    """Realised volatility (sample SD, ddof 1) of the last ``window`` returns up to and including d, annualised.

    ``window=None`` gives an expanding window from the first return. nan until ``min_n`` returns exist."""
    x = np.asarray(r, dtype=float)
    out = np.full(x.size, np.nan)
    if x.size < 2:
        return out
    v = x[1:]
    cs = np.concatenate([[0.0], np.cumsum(v)])
    cs2 = np.concatenate([[0.0], np.cumsum(v * v)])
    for d in range(1, x.size):
        hi = d  # returns v[0 .. d-1] correspond to r[1 .. d]
        lo = 0 if window is None else max(0, hi - window)
        n = hi - lo
        if n < max(2, min_n):
            continue
        s, s2 = cs[hi] - cs[lo], cs2[hi] - cs2[lo]
        var = (s2 - s * s / n) / (n - 1)
        out[d] = math.sqrt(max(var, 0.0) * ann)
    return out


def vol_target_signal(sigma_target: Sequence[float] | float, sigma_hat: Sequence[float], cap: float = 1.0) -> np.ndarray:
    """clip(sigma_target / sigma_hat, 0, cap); nan where either input is nan or sigma_hat <= 0."""
    h = np.asarray(sigma_hat, dtype=float)
    t = np.broadcast_to(np.asarray(sigma_target, dtype=float), h.shape)
    out = np.full(h.shape, np.nan)
    ok = np.isfinite(h) & np.isfinite(t) & (h > 0)
    out[ok] = np.clip(t[ok] / h[ok], 0.0, cap)
    return out


def donchian_state(closes: Sequence[float], n: int) -> np.ndarray:
    """Long/flat state of an n-day Donchian channel on closes: 1 when C[d] > max(C[d-n..d-1]), 0 when C[d] <
    min(C[d-n..d-1]), otherwise the previous state (0 before the first breakout). nan for d < n."""
    c = np.asarray(closes, dtype=float)
    out = np.full(c.size, np.nan)
    st = 0.0
    for d in range(n, c.size):
        win = c[d - n : d]
        if c[d] > win.max():
            st = 1.0
        elif c[d] < win.min():
            st = 0.0
        out[d] = st
    return out


def donchian_fraction(closes: Sequence[float], ns: Sequence[int] = (20, 50, 100, 200)) -> np.ndarray:
    """Share of the channels in ``ns`` that are in the long state (nan until the longest channel is defined)."""
    st = np.vstack([donchian_state(closes, n) for n in ns])
    return st.mean(axis=0)


def positions(signal: Sequence[float], lag: int = 2) -> np.ndarray:
    """Weight held over day j = signal[j - lag] (nan for j < lag)."""
    s = np.asarray(signal, dtype=float)
    w = np.full(s.size, np.nan)
    if lag < s.size:
        w[lag:] = s[: s.size - lag]
    return w


# ----------------------------------------------------------------------------- P&L


def drifted(w: np.ndarray, R: np.ndarray) -> np.ndarray:
    """Weight after one day of return R starting from weight w: w (1 + R) / (1 + w R)."""
    return w * (1.0 + R) / (1.0 + w * R)


def net_returns(
    target: Sequence[float],
    R: Sequence[float],
    *,
    fee: float = 0.001,
    band: float = 0.0,
    perp_fee: float = 0.0005,
    funding: Sequence[float] | None = None,
    w0: float = 0.0,
) -> dict[str, np.ndarray]:
    """Daily net return on capital of a long position rebalanced at the start of each day to ``target[j]``.

    The part of the weight above 1 is a perp leg: it pays ``perp_fee`` per unit of its turnover and the day's funding
    ``funding[j]`` (decimal, sum of the day's settlements; positive = longs pay) on its notional. The part up to 1 is
    spot and pays ``fee`` per unit of turnover. With ``band`` > 0 the position is moved to the target only when
    |target - drifted weight| > band. The book starts at weight ``w0`` before day 0. Inputs must be finite.
    Returns ``{"ret", "held", "turnover"}``."""
    t = np.asarray(target, dtype=float)
    r = np.asarray(R, dtype=float)
    f = np.zeros(t.size) if funding is None else np.asarray(funding, dtype=float)
    if band <= 0:
        held = t.copy()
        prev = np.empty(t.size)
        prev[0] = w0
        if t.size > 1:
            prev[1:] = drifted(held[:-1], r[:-1])
    else:
        held = np.empty(t.size)
        prev = np.empty(t.size)
        cur = w0
        for j in range(t.size):
            prev[j] = cur
            held[j] = t[j] if abs(t[j] - cur) > band else cur
            cur = float(drifted(np.array(held[j]), np.array(r[j])))
    spot_now, spot_prev = np.minimum(held, 1.0), np.minimum(prev, 1.0)
    perp_now, perp_prev = np.maximum(held - 1.0, 0.0), np.maximum(prev - 1.0, 0.0)
    cost = fee * np.abs(spot_now - spot_prev) + perp_fee * np.abs(perp_now - perp_prev)
    ret = held * r - cost - perp_now * f
    return {"ret": ret, "held": held, "turnover": np.abs(held - prev)}


# ----------------------------------------------------------------------------- metrics


def sharpe(p: Sequence[float], ann: float = ANN) -> float:
    x = np.asarray(p, dtype=float)
    if x.size < 3:
        return float("nan")
    sd = float(x.std(ddof=1))
    return float(x.mean()) / sd * math.sqrt(ann) if sd > 0 else float("nan")


def max_drawdown(p: Sequence[float]) -> float:
    """Largest peak-to-trough fall of the compounded equity curve 1 -> prod(1 + p), as a positive fraction."""
    x = np.asarray(p, dtype=float)
    if x.size == 0:
        return 0.0
    eq = np.concatenate([[1.0], np.cumprod(1.0 + x)])
    peak = np.maximum.accumulate(eq)
    return float(np.max(1.0 - eq / peak))


def cvar(x: Sequence[float], q: float = 0.05) -> float:
    """Expected shortfall as a positive loss: -mean of the ceil(q n) smallest values."""
    a = np.sort(np.asarray(x, dtype=float))
    if a.size == 0:
        return float("nan")
    k = max(1, math.ceil(q * a.size))
    return float(-a[:k].mean())


def period_returns(p: Sequence[float], keys: Sequence[Any]) -> tuple[list[Any], np.ndarray]:
    """Compounded return of ``p`` over consecutive runs of equal ``keys`` (e.g. calendar months), in order."""
    x = np.asarray(p, dtype=float)
    out_k: list[Any] = []
    out_v: list[float] = []
    acc, cur = 1.0, None
    for k, v in zip(keys, x, strict=True):
        if cur is not None and k != cur:
            out_k.append(cur)
            out_v.append(acc - 1.0)
            acc = 1.0
        cur = k
        acc *= 1.0 + v
    if cur is not None:
        out_k.append(cur)
        out_v.append(acc - 1.0)
    return out_k, np.array(out_v)


# ----------------------------------------------------------------------------- inference


def block_indices(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """Moving-block bootstrap: ceil(n / block) random blocks of consecutive indices, truncated to n."""
    k = -(-n // block)
    starts = rng.integers(0, n - block + 1, size=k)
    return (starts[:, None] + np.arange(block)[None, :]).ravel()[:n]


def bootstrap(n: int, stat: Callable[[np.ndarray], float], *, block: int, reps: int = 2000, seed: int = 20261018) -> dict[str, Any]:
    """Point ``stat(arange(n))``, percentile 95 % CI, SE and one-sided p = (1 + #{stat* <= 0}) / (1 + reps)."""
    point = float(stat(np.arange(n)))
    res: dict[str, Any] = {"n": n, "stat": point, "ci95": None, "se": None, "p": None, "draws": None}
    if n < max(3, block + 1):
        return res
    rng = np.random.default_rng(seed)
    vals = np.array([stat(block_indices(n, block, rng)) for _ in range(reps)], dtype=float)
    vals = vals[np.isfinite(vals)]
    if vals.size < 10:
        return res
    res["ci95"] = (float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975)))
    res["se"] = float(vals.std(ddof=1))
    res["p"] = float((1 + np.sum(vals <= 0)) / (1 + vals.size))
    res["draws"] = vals
    return res


def block_permute(x: Sequence[float], block: int, rng: np.random.Generator) -> np.ndarray:
    """Series cut into consecutive blocks of ``block`` (last one shorter) and the blocks put in random order."""
    a = np.asarray(x)
    blocks = [a[i : i + block] for i in range(0, a.size, block)]
    order = rng.permutation(len(blocks))
    return np.concatenate([blocks[i] for i in order]) if blocks else a.copy()


def placebo_p(observed: float, null: Sequence[float]) -> float:
    """(1 + #{null >= observed}) / (1 + n): share of placebo draws at least as good as the observed statistic."""
    z = np.asarray(null, dtype=float)
    z = z[np.isfinite(z)]
    return float((1 + np.sum(z >= observed - 1e-15)) / (1 + z.size))


def holm(pvals: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values (monotone, capped at 1), in input order."""
    p = np.asarray(pvals, dtype=float)
    m = p.size
    order = np.argsort(p, kind="stable")
    adj = np.empty(m)
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * p[i]))
        adj[i] = run
    return [float(v) for v in adj]


def cochran_q(estimates: Sequence[float], ses: Sequence[float]) -> dict[str, float]:
    """Cochran's Q with inverse-variance weights, chi-square(k - 1) p and I^2 = max(0, (Q - df) / Q)."""
    y = np.asarray(estimates, dtype=float)
    s = np.asarray(ses, dtype=float)
    ok = np.isfinite(y) & np.isfinite(s) & (s > 0)
    y, s = y[ok], s[ok]
    k = int(y.size)
    if k < 2:
        return {"k": k, "q": float("nan"), "df": k - 1, "p": float("nan"), "i2": float("nan"), "pooled": float("nan")}
    w = 1.0 / s**2
    mu = float(np.sum(w * y) / np.sum(w))
    q = float(np.sum(w * (y - mu) ** 2))
    return {"k": k, "q": q, "df": k - 1, "p": chi2_sf(q, k - 1), "i2": max(0.0, (q - (k - 1)) / q) if q > 0 else 0.0, "pooled": mu}


def mde(se: float | None, k: float = 2.8) -> float:
    """Minimum detectable effect at 80 % power, two-sided 5 % (2.8 standard errors)."""
    return float("nan") if se is None else k * se


# ----------------------------------------------------------------------------- registered verdict rules


def h1_verdict(
    holm_pass: Sequence[bool], ci_upper: Sequence[float], placebo_ps: Sequence[float], oos: Sequence[float], *, alpha: float = 0.05
) -> str:
    """Registered H1 verdict for one asset from its two tail endpoints (T1 CVaR, T2 MaxDD).

    ✓ both pass Holm, both placebo p <= alpha and both OOS estimates > 0; ✗ neither passes Holm or a CI lies wholly
    below 0; ≈ otherwise."""
    if any(u < 0 for u in ci_upper) or not any(holm_pass):
        return "✗"
    if all(holm_pass) and all(p <= alpha for p in placebo_ps) and all(o > 0 for o in oos):
        return "✓"
    return "≈"


def sr_verdict(
    stat: float, ci95: tuple[float, float] | None, oos: float, placebo: float, mde_value: float, *, gate: float = 0.30, alpha: float = 0.05
) -> str:
    """Registered verdict for dSR: ◯ when MDE > gate (or no CI); ✓ CI lower > 0, OOS same sign, placebo p <= alpha;
    ✗ (CI includes 0 and point < 0.1) or OOS of the opposite sign; ≈ otherwise."""
    if ci95 is None or not math.isfinite(mde_value) or mde_value > gate:
        return "◯"
    if ci95[0] > 0 and oos * stat > 0 and placebo <= alpha:
        return "✓"
    if (ci95[0] <= 0 <= ci95[1] and stat < 0.1) or oos * stat < 0:
        return "✗"
    return "≈"


def card_status(
    btc: str, eth: str, *, btc_placebo: Sequence[float], btc_oos: Sequence[float], btc_points: Sequence[float], eth_points: Sequence[float]
) -> str:
    """Registered card status: confirmed (BTC ✓ and ETH ✓/≈); rejected (BTC ✗, both BTC placebo p > 0.05, both BTC
    OOS < 0, or ETH point estimates both of the opposite sign to BTC's); inconclusive otherwise."""
    opposite = all(b * e < 0 for b, e in zip(btc_points, eth_points, strict=True))
    if btc == "✗" or all(p > 0.05 for p in btc_placebo) or all(o < 0 for o in btc_oos) or opposite:
        return "rejected"
    if btc == "✓" and eth in ("✓", "≈"):
        return "confirmed"
    return "inconclusive"
