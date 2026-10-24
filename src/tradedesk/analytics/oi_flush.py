"""Open-interest confirmation of liquidation flushes (IDEA-20261024-oi-confirmed-liquidation-flush).

Pure functions, no I/O. Rates and changes are decimals (-0.02 = -2 %). Times are epoch milliseconds UTC.

Archive semantics (``data.binance.vision`` daily ``metrics``) changed between 2024-03-03 14:30 and 2024-03-04 00:45 UTC
(measured on 6 383 flush events with a full lag grid, docs/verification/2026-10-24-oi-confirmed-liquidation-flush.md:
value / OI of a row matched the 1m close at the stamp in 91 % of events before the switch and the close five minutes
after the stamp in 63 % afterwards, e.g. the 2025-10-10 cascade). The switch time 2024-03-04 00:00 UTC (the daily file
boundary) is an ASSUMPTION inside that window, not checked on raw rows; verify it before relying on rows of
2024-03-03. The pre-switch rule is pinned by a synthetic test only (no real pre-2024-03 archive sample in fixtures).
Before the switch the row stamped ``create_time`` is a snapshot taken AT ``create_time``; from the switch on it is
taken at ``create_time + 5 min``. Every function here works on OBSERVATION times (``archive_obs_times``) so a caller
cannot use a snapshot before it existed."""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence

import numpy as np

MIN_MS = 60_000
OBS_LAG_MS = 5 * MIN_MS


SEMANTICS_SWITCH_MS = 1_709_510_400_000  # 2024-03-04 00:00 UTC: assumed; events bracket it in (03-03 14:30, 03-04 00:45]


def observation_times(create_ms: Sequence[float] | np.ndarray, lag_ms: int = OBS_LAG_MS) -> np.ndarray:
    """Observation time of each archive row with one fixed lag: ``create_time + lag``."""
    return np.asarray(create_ms, dtype=np.int64) + int(lag_ms)


def archive_obs_times(create_ms: Sequence[float] | np.ndarray, *, switch_ms: int = SEMANTICS_SWITCH_MS) -> np.ndarray:
    """Observation time of archive ``metrics`` rows: ``create_time`` before ``switch_ms``, ``create_time + 5 min`` after.

    The result stays sorted (rows before the switch end at 23:55 of the last old day, observed then; the first new row
    00:00 is observed at 00:05)."""
    c = np.asarray(create_ms, dtype=np.int64)
    return c + np.where(c >= switch_ms, OBS_LAG_MS, 0).astype(np.int64)


def oi_at(obs_ts: np.ndarray, oi: np.ndarray, t: int, *, asof: int) -> float | None:
    """OI snapshot observed exactly at ``t``; None if missing, non-positive, or ``t`` is after the decision time ``asof``."""
    if t > asof:
        return None
    i = int(np.searchsorted(obs_ts, t, side="left"))
    if i >= obs_ts.size or int(obs_ts[i]) != t:
        return None
    v = float(oi[i])
    return v if v > 0 else None


def oi_change(obs_ts: np.ndarray, oi: np.ndarray, t_end: int, *, lookback_ms: int, asof: int) -> float | None:
    """``OI(t_end) / OI(t_end - lookback) - 1`` from snapshots observed exactly at those times and not after ``asof``.

    For a flush bar ``[t0, t0 + 5m)`` the card's ΔOI is ``t_end = t0 + 5m`` and ``lookback = 10m`` (OI at the end of the
    bar against OI five minutes before the bar opened). Returns None when either snapshot is missing."""
    a = oi_at(obs_ts, oi, t_end - lookback_ms, asof=asof)
    b = oi_at(obs_ts, oi, t_end, asof=asof)
    if a is None or b is None:
        return None
    return b / a - 1.0


def basket(d_oi: float | None, *, a_max: float = -0.02, b_min: float = -0.005) -> str | None:
    """'A' (forced: ΔOI <= a_max), 'B' (informational: ΔOI > b_min), None for the gap between them or missing ΔOI."""
    if d_oi is None or not math.isfinite(d_oi):
        return None
    if d_oi <= a_max:
        return "A"
    if d_oi > b_min:
        return "B"
    return None


def units_consistent(oi: float, oi_value: float, ref_price: float, *, tol: float = 0.10) -> bool:
    """True if ``oi_value / oi`` (implied price, USDT per OI unit) is within ``tol`` of the kline price.

    A mismatch flags a contract multiplier change, a relaunched contract or a broken row; ΔOI in coins is then unusable."""
    if not (oi > 0 and oi_value > 0 and ref_price > 0):
        return False
    return abs(oi_value / oi / ref_price - 1.0) <= tol


def _cluster_sums(values: Sequence[float], clusters: Sequence[Hashable], weights: float) -> dict[Hashable, float]:
    out: dict[Hashable, float] = {}
    for v, c in zip(values, clusters, strict=True):
        out[c] = out.get(c, 0.0) + v * weights
    return out


def cluster_se_mean(values: Sequence[float], clusters: Sequence[Hashable]) -> float | None:
    """Cluster-robust standard error of a sample mean (CR1: CR0 times G/(G-1)); None with fewer than 2 clusters."""
    n = len(values)
    if n == 0:
        return None
    m = math.fsum(values) / n
    sums = _cluster_sums([v - m for v in values], clusters, 1.0 / n)
    g = len(sums)
    if g < 2:
        return None
    return math.sqrt(math.fsum(s * s for s in sums.values()) * g / (g - 1))


def cluster_se_diff(
    values_a: Sequence[float], clusters_a: Sequence[Hashable], values_b: Sequence[float], clusters_b: Sequence[Hashable]
) -> float | None:
    """Cluster-robust SE of ``mean(A) - mean(B)`` when A and B share clusters (e.g. both have events on one day).

    Influence of an A observation is ``(x - mean_A) / n_A``, of a B observation ``-(x - mean_B) / n_B``; they are summed
    within each cluster, so the covariance between the two means on shared days is included. CR1 small-sample factor."""
    na, nb = len(values_a), len(values_b)
    if na == 0 or nb == 0:
        return None
    ma, mb = math.fsum(values_a) / na, math.fsum(values_b) / nb
    sums = _cluster_sums([v - ma for v in values_a], clusters_a, 1.0 / na)
    for c, s in _cluster_sums([v - mb for v in values_b], clusters_b, -1.0 / nb).items():
        sums[c] = sums.get(c, 0.0) + s
    g = len(sums)
    if g < 2:
        return None
    return math.sqrt(math.fsum(s * s for s in sums.values()) * g / (g - 1))


def cluster_bootstrap_diff(
    values_a: Sequence[float],
    clusters_a: Sequence[Hashable],
    values_b: Sequence[float],
    clusters_b: Sequence[Hashable],
    *,
    n_boot: int = 10_000,
    seed: int = 7,
) -> np.ndarray:
    """Bootstrap distribution of ``mean(A) - mean(B)``, resampling whole clusters (days) jointly for both groups.

    A resample in which one group has no observation is dropped (the array can be shorter than ``n_boot``)."""
    keys = sorted(set(clusters_a) | set(clusters_b), key=str)
    if not keys or not values_a or not values_b:
        return np.array([])
    pos = {k: i for i, k in enumerate(keys)}
    g = len(keys)
    sa, ca, sb, cb = np.zeros(g), np.zeros(g), np.zeros(g), np.zeros(g)
    for v, c in zip(values_a, clusters_a, strict=True):
        sa[pos[c]] += v
        ca[pos[c]] += 1
    for v, c in zip(values_b, clusters_b, strict=True):
        sb[pos[c]] += v
        cb[pos[c]] += 1
    rng = np.random.default_rng(seed)
    out = []
    chunk = max(1, 2_000_000 // g)
    for lo in range(0, n_boot, chunk):
        idx = rng.integers(0, g, size=(min(n_boot, lo + chunk) - lo, g))
        na, nb = ca[idx].sum(axis=1), cb[idx].sum(axis=1)
        ok = (na > 0) & (nb > 0)
        out.append(sa[idx].sum(axis=1)[ok] / na[ok] - sb[idx].sum(axis=1)[ok] / nb[ok])
    return np.concatenate(out)


def mde(se: float | None, *, z_alpha: float = 1.959964, z_power: float = 0.841621) -> float | None:
    """Minimum detectable effect at two-sided 5 % and 80 % power: ``(z_alpha + z_power) * se`` (≈ 2.8 se)."""
    return None if se is None else (z_alpha + z_power) * se


def effective_cluster_sd(se: float | None, n_clusters: int) -> float | None:
    """``se * sqrt(G)``: the per-cluster SD implied by a cluster-robust SE (comparable to the card's 6.7 % estimate)."""
    return None if se is None or n_clusters <= 0 else se * math.sqrt(n_clusters)
