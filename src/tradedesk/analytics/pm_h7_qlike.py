"""H7'' - the weekend / hour-of-day profile on the paper's IV model scored on realized variance (QLIKE).

IDEA-20261031-structural-edges-depth, item 8l. For a snapshot at ``ts`` of a market ending at ``end`` with Deribit expiry
``E`` and smile IV ``sigma``, the target window is the whole UTC hours inside [ts; end): H = [ceil(ts); floor(end)).
Two forecasts of the realized variance y = sum of squared 1h log returns over H:

    flat     h_f = sigma^2 * |H| / year
    profile  h_p = s * h_f,  s = [W(H) / |H|] / [W(ts, E) / (E - ts)]   (the item-8k share-of-expiry multiplier on H)

QLIKE loss ``L(h, y) = y / h - ln(y / h) - 1``; the paired difference flat minus profile is

    dL = (y / (c * h_f)) * (1 - 1 / s) - ln s

for a common level factor ``c`` applied to both forecasts (no ``ln y``, so y = 0 is allowed). It is linear in y, so with
E[y] = q * c * s_true * h_f the expectation is q * (s - 1) - ln s if the profile is right (q = level ratio): the level
error enters to first order (q - 1)(s - 1) while the effect itself is second order, ~ (s - 1)^2 / 2. The registered
neutralisation divides s by its weighted mean ``m`` (computed from forecasts only), so the first-order term cancels in
the weighted mean when the level ratio is constant.

Pure functions, no I/O; time in unix seconds.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from .pm_h7_iv import Profile, window_weight_exact
from .pm_threshold import HOUR_S, YEAR_S


def whole_hours(ts: float, end_ts: float) -> tuple[int, int] | None:
    """[ceil(ts); floor(end)) on whole UTC hours; None when it holds less than one hour."""
    h0 = math.ceil(ts / HOUR_S) * HOUR_S
    h1 = math.floor(end_ts / HOUR_S) * HOUR_S
    if h1 - h0 < HOUR_S:
        return None
    return int(h0), int(h1)


def window_var_scale(profile: Profile, ts: float, h0: float, h1: float, expiry_ts: float, shift_h: int = 0) -> float | None:
    """Item-8k variance multiplier taken on the target window [h0; h1): [W(H)/|H|] / [W(ts, E)/(E - ts)].

    None when the window is empty, starts before ts, the expiry is before the window's end, or a weight is not positive.
    """
    if h1 <= h0 or h0 < ts or expiry_ts < h1:
        return None
    w_h = window_weight_exact(profile, h0, h1, shift_h)
    w_e = window_weight_exact(profile, ts, expiry_ts, shift_h)
    if w_h <= 0 or w_e <= 0:
        return None
    return (w_h / ((h1 - h0) / HOUR_S)) / (w_e / ((expiry_ts - ts) / HOUR_S))


def flat_var(iv: float, h0: float, h1: float) -> float:
    """Flat-IV forecast of the window variance: sigma^2 * (h1 - h0) / year (decimal IV, e.g. 0.45)."""
    return iv * iv * (h1 - h0) / YEAR_S


def realized_var(returns: Mapping[int, float], h0: int, h1: int) -> float | None:
    """Sum of squared log returns of the 1h bars opening in [h0; h1) (``returns``: bar open -> log return).

    None when any bar of the window is missing. Reads nothing outside the window (causal in its inputs).
    """
    tot = 0.0
    for t in range(int(h0), int(h1), HOUR_S):
        r = returns.get(t)
        if r is None:
            return None
        tot += r * r
    return tot


def qlike(h: float, y: float) -> float:
    """QLIKE loss y/h - ln(y/h) - 1 (>= 0, zero at h = y); needs y > 0 and h > 0."""
    if h <= 0 or y <= 0:
        raise ValueError("qlike needs h > 0 and y > 0")
    u = y / h
    return u - math.log(u) - 1.0


def qlike_diff(y: float, h_flat: float, s: float, c: float = 1.0) -> float:
    """Paired QLIKE difference L(c*h_flat, y) - L(c*s*h_flat, y); positive = the profile forecast is better."""
    if h_flat <= 0 or s <= 0 or c <= 0 or y < 0:
        raise ValueError("need h_flat > 0, s > 0, c > 0, y >= 0")
    return (y / (c * h_flat)) * (1.0 - 1.0 / s) - math.log(s)


def expected_gain(s: float) -> float:
    """E[dL] when the profile is right and the level is known: s - 1 - ln s (>= 0)."""
    return s - 1.0 - math.log(s)


def expected_diff_level(s: float, q: float, profile_true: bool = True) -> float:
    """E[dL] at level ratio q = c_true / c_used: q(s - 1) - ln s (profile right) or q(1 - 1/s) - ln s (flat right)."""
    return q * (s - 1.0) - math.log(s) if profile_true else q * (1.0 - 1.0 / s) - math.log(s)


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    if not values or len(values) != len(weights):
        raise ValueError("values and weights must be non-empty and of equal length")
    tw = sum(weights)
    if tw <= 0:
        raise ValueError("weights must sum to a positive number")
    return sum(v * w for v, w in zip(values, weights, strict=True)) / tw


def market_weights(market_of_row: Sequence[str]) -> list[float]:
    """Row weights of the primary statistic (mean over markets of the market mean): 1 / (rows of its market * markets)."""
    cnt: dict[str, int] = {}
    for m in market_of_row:
        cnt[m] = cnt.get(m, 0) + 1
    n_m = len(cnt)
    return [1.0 / (cnt[m] * n_m) for m in market_of_row]


def renormalize(scales: Sequence[float], weights: Sequence[float]) -> tuple[list[float], float]:
    """s' = s / m with m the weighted mean of s, so that sum w (s' - 1) = 0; returns (s', m)."""
    m = weighted_mean(scales, weights)
    return [s / m for s in scales], m


def break_even_level(scales: Sequence[float], weights: Sequence[float]) -> float | None:
    """q* = sum w ln s / sum w (s - 1): the level ratio at which the expected dL (profile right) changes sign."""
    den = sum(w * (s - 1.0) for s, w in zip(scales, weights, strict=True))
    if den == 0:
        return None
    return sum(w * math.log(s) for s, w in zip(scales, weights, strict=True)) / den


def weeks_needed(sd_week: float, target: float, k: float = 2.8) -> int | None:
    """Clusters N with k * sd_week / sqrt(N) <= target."""
    if sd_week <= 0 or target <= 0:
        return None
    return math.ceil((k * sd_week / target) ** 2)
