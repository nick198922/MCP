"""H7' - the weekend / hour-of-day variance profile on the paper's IV model (IDEA-20261031-structural-edges-depth, 8k).

The paper's model probability uses the Deribit smile IV of the first expiry E at or after the market's end and spreads
that expiry's variance flat over time: the variance of the market's window [ts; end) is sigma^2 * (end - ts). With a
profile w (``pm_modules.intraday_profile``) the same total variance sigma^2 * (E - ts) is shared out by weight instead:

    var_window = sigma^2 * (E - ts) * W(ts, end) / W(ts, E),   W(a, b) = integral of w over [a; b) in average hours

so the variance multiplier relative to the model is ``s = [W(ts, end) / (end - ts)] / [W(ts, E) / (E - ts)]`` and the
volatility multiplier is ``k = sqrt(s)``, applied with ``pm_gap_paper.vol_scaled_prob`` (keeps the smile-slope term).
This is a share of the expiry's weight, not a raw weight sum: when E = end, s = 1 and nothing changes.

Pure functions, no I/O; time in unix seconds; the profile is estimated elsewhere on data before the test period.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from .pm_gap_paper import vol_scaled_prob
from .pm_modules import cell_of
from .pm_threshold import HOUR_S, YEAR_S

Profile = Mapping[tuple[int, bool], float]


def window_weight_exact(profile: Profile, t0: float, t1: float, shift_h: int = 0) -> float:
    """Integral of the profile weight over [t0; t1) in units of an average hour (partial hours pro rata).

    The weight of a moment t is the weight of the cell of its hour (UTC hour, weekend flag), looked up at
    ``t + shift_h`` hours (placebo). A flat profile (all 1) gives the window length in hours.
    """
    if t1 <= t0:
        return 0.0
    off = shift_h * HOUR_S
    tot = 0.0
    t = float(t0)
    while t < t1:
        h_start = math.floor(t / HOUR_S) * HOUR_S
        seg_end = min(float(t1), h_start + HOUR_S)
        tot += profile[cell_of(int(h_start) + off)] * (seg_end - t) / HOUR_S
        t = seg_end
    return tot


def profile_var_scale(profile: Profile, ts: float, end_ts: float, expiry_ts: float, shift_h: int = 0) -> float | None:
    """Variance multiplier s of the market window [ts; end) relative to the flat-IV model (see module doc).

    None when the window is empty, the expiry is before the market's end, or a weight sum is not positive.
    """
    if end_ts <= ts or expiry_ts < end_ts:
        return None
    w_m = window_weight_exact(profile, ts, end_ts, shift_h)
    w_e = window_weight_exact(profile, ts, expiry_ts, shift_h)
    if w_m <= 0 or w_e <= 0:
        return None
    h_m, h_e = (end_ts - ts) / HOUR_S, (expiry_ts - ts) / HOUR_S
    return (w_m / h_m) / (w_e / h_e)


def profile_prob(
    profile: Profile,
    kind: str,
    p_model: float,
    iv: float | None,
    forward: float | None,
    strike: float,
    ts: int,
    end_ts: int,
    expiry_ts: int,
    shift_h: int = 0,
) -> float | None:
    """The model probability with the window's variance reweighted by the profile; None when it cannot be formed."""
    s = profile_var_scale(profile, ts, end_ts, expiry_ts, shift_h)
    if s is None:
        return None
    return vol_scaled_prob(kind, p_model, iv, forward, strike, (end_ts - ts) / YEAR_S, math.sqrt(s))


def gate_terms(p_base: Sequence[float], p_prof: Sequence[float]) -> dict[str, float]:
    """Outcome-free power terms of one market from its snapshots' base and profile probabilities.

    ``d`` = mean (p_base - p_prof); ``D`` = mean (p_base - p_prof)^2 = expected Brier gain if the profile model is true
    (and the expected loss if the base is true); ``pbar`` = mean p_base; ``var0`` = 4 d^2 pbar (1 - pbar), the variance
    of the market's Brier difference under the base model (the outcome is shared by all its snapshots).
    """
    if not p_base or len(p_base) != len(p_prof):
        raise ValueError("p_base and p_prof must be non-empty and of equal length")
    n = len(p_base)
    d = sum(b - p for b, p in zip(p_base, p_prof, strict=True)) / n
    big_d = sum((b - p) ** 2 for b, p in zip(p_base, p_prof, strict=True)) / n
    pbar = sum(p_base) / n
    return {"d": d, "D": big_d, "pbar": pbar, "var0": 4.0 * d * d * pbar * (1.0 - pbar)}


def gate_mde(terms: Sequence[Mapping[str, float]], k: float = 2.8) -> dict[str, float | None]:
    """Target effect (mean D) and the optimistic null MDE = k * sqrt(sum var0) / n over independent markets."""
    n = len(terms)
    if n == 0:
        return {"n": 0, "target": None, "se0": None, "mde0": None}
    se0 = math.sqrt(sum(t["var0"] for t in terms)) / n
    return {"n": n, "target": sum(t["D"] for t in terms) / n, "se0": se0, "mde0": k * se0}


def n_for_gate(target: float, se0: float, n: int, k: float = 2.8) -> int | None:
    """Markets needed for k * SE0 <= target when SE0 scales as 1 / sqrt(n) at the same composition."""
    if target <= 0 or se0 <= 0 or n <= 0:
        return None
    return math.ceil(n * (k * se0 / target) ** 2)
