"""Cross-sectional volume and liquidity features of USDT-M perps: Amihud illiquidity, volume shock, dollar volume.

Pure functions (no I/O). Time is passed in as UTC day indices (``ms // DAY_MS``); the rank day ``t`` is a Monday and a
feature uses only bars of days ``< t``. Quote volume ``qv`` is in USD per day; returns are decimals (0.01 = 1 %).
Idea card: docs/ideas/IDEA-20261025-xs-volume-liquidity-factors.md (section "Попередня реєстрація").

The universe, the weekly leg and the rank statistics are reused from ``funding_crowding`` and ``xs_factors``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence

import numpy as np

from .funding_crowding import Bars
from .xs_factors import rank_ic, rank_z, residualize

FEATURES = ("AMI30", "VS", "DV30")


def amihud(bars: Bars, t: int, *, window: int = 30, min_days: int = 20) -> float | None:
    """Mean of ``|C_d / C_{d-1} - 1| / qv_d`` over days ``d`` in ``[t - window, t - 1]`` with both closes and qv > 0.

    Units: decimal return per USD of daily quote volume. None with fewer than ``min_days`` valid days."""
    vals = []
    for d in range(t - window, t):
        q = bars.qv.get(d)
        if q is None or q <= 0 or d not in bars.close or d - 1 not in bars.close or bars.close[d - 1] <= 0:
            continue
        vals.append(abs(bars.close[d] / bars.close[d - 1] - 1.0) / q)
    return statistics.fmean(vals) if len(vals) >= min_days else None


def _mean_qv(bars: Bars, lo: int, hi: int) -> tuple[float, int]:
    """(mean qv, count) over days ``lo <= d < hi`` that have a positive qv; (nan, 0) when there are none."""
    v = [bars.qv[d] for d in range(lo, hi) if bars.qv.get(d, 0.0) > 0]
    return (statistics.fmean(v), len(v)) if v else (float("nan"), 0)


def volume_shock(bars: Bars, t: int, *, short: int = 7, long: int = 90, min_short: int = 5, min_long: int = 30) -> float | None:
    """VS = ln(mean qv over ``[t - short, t - 1]`` / mean qv over ``[t - short - long, t - short - 1]``)."""
    s, ns = _mean_qv(bars, t - short, t)
    b, nb = _mean_qv(bars, t - short - long, t - short)
    if ns < min_short or nb < min_long or s <= 0 or b <= 0:
        return None
    return math.log(s / b)


def dollar_volume(bars: Bars, t: int, *, window: int = 30, min_days: int = 20) -> float | None:
    """DV30 = ln(mean qv over ``[t - window, t - 1]``), ln of USD per day."""
    m, n = _mean_qv(bars, t - window, t)
    return math.log(m) if n >= min_days and m > 0 else None


def features_at(bars: Bars, t: int) -> dict[str, float | None]:
    """The registered features plus the descriptive Amihud windows (14 and 60 days) at rank day ``t``."""
    return {
        "AMI30": amihud(bars, t),
        "VS": volume_shock(bars, t),
        "DV30": dollar_volume(bars, t),
        "AMI14": amihud(bars, t, window=14, min_days=10),
        "AMI60": amihud(bars, t, window=60, min_days=40),
    }


def shift_placebo_mean_ics(
    signals: Sequence[Mapping[str, float]], returns: Sequence[Mapping[str, float]], shifts: Sequence[int], *, min_common: int = 20
) -> list[float]:
    """Mean weekly rank IC when the signal of week ``(w + k) mod n`` is paired with the returns of week ``w``.

    Symbols are matched by name; a week with fewer than ``min_common`` common symbols is skipped. One value per shift
    (nan when no week qualifies). The shift keeps the cross-sectional structure of the signal but breaks its timing."""
    n = len(signals)
    if len(returns) != n:
        raise ValueError("signals and returns need the same number of weeks")
    out = []
    for k in shifts:
        ics = []
        for w in range(n):
            sig, ret = signals[(w + k) % n], returns[w]
            common = sorted(set(sig) & set(ret))
            if len(common) >= min_common:
                ics.append(rank_ic([sig[s] for s in common], [ret[s] for s in common]))
        out.append(statistics.fmean(ics) if ics else float("nan"))
    return out


def partial_residuals(
    signal: Sequence[float], returns: Sequence[float], controls: Sequence[Sequence[float]]
) -> tuple[np.ndarray, np.ndarray]:
    """Unit-norm residuals (a, b) of rank-z(signal) and rank-z(returns) on [1, rank-z(controls)]; ``a @ b`` is
    ``xs_factors.partial_rank_ic``. Zero vectors when a residual is constant."""
    cz = [rank_z(c) for c in controls]
    out = []
    for v in (signal, returns):
        r = residualize(rank_z(v), cz)
        nrm = float(np.sqrt(np.sum(r * r)))
        out.append(r / nrm if nrm > 0 else np.zeros(r.size))
    return out[0], out[1]


def partial_ic_products(signal: Sequence[float], returns: Sequence[float], controls: Sequence[Sequence[float]]) -> np.ndarray:
    """Per-symbol contributions ``n * a_i * b_i`` whose mean over the week is the partial rank IC (asset analysis)."""
    a, b = partial_residuals(signal, returns, controls)
    return a.size * a * b


def placebo_corr_mean(pairs: Sequence[tuple[np.ndarray, np.ndarray]], n_perm: int, *, seed: int = 7) -> np.ndarray:
    """Mean over weeks of ``a[perm] @ b`` for unit-norm residual pairs under ``n_perm`` within-week permutations."""
    rng = np.random.default_rng(seed)
    acc = np.zeros(n_perm)
    for a, b in pairs:
        idx = np.argsort(rng.random((n_perm, a.size)), axis=1)
        acc += a[idx] @ b
    return acc / max(len(pairs), 1)


def quantile_spread(signal: Sequence[float], returns: Sequence[float], *, q: int = 5) -> float:
    """Mean return of the top ``1/q`` of the signal minus the bottom ``1/q`` (equal weights, gross, one period)."""
    if len(signal) != len(returns) or len(signal) < 2 * q:
        raise ValueError("need equal lengths and at least 2q values")
    z = rank_z(signal)
    order = np.argsort(z, kind="stable")
    k = len(signal) // q
    r = np.asarray(returns, dtype=float)
    return float(r[order[-k:]].mean() - r[order[:k]].mean())
