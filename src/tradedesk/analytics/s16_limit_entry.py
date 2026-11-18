"""S16 entry by a limit on the pullback to the broken range edge (IDEA-20261030-demo-setups-depth, audit 14.11).

The S16 base enters at the close of the bar that closes back inside the range. The course says "enter on any pullback
after the return", so this module rests a buy limit (long price space; shorts are mirrored) at the broken edge ``lr``
for a fixed number of signal bars after the return bar, with the base stop and target. Unfilled signals score 0R.

The placebo keeps the fill frequency and the timing but drops the price condition: a market entry at the open of a
random 1h bar of the same window. All functions are pure: prices, times and draws are parameters; no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.setup_passport import limit_trade
from tradedesk.analytics.stop_management import Fees, StopRule, simulate_stop_rule

HOUR_MS = 3_600_000
WINDOW_BARS = 3  # the limit rests 3 signal bars after the return bar (registered)


def retest_limit(
    path: Bars,
    t_in: int,
    bar_ms: int,
    level: float,
    stop: float,
    target: float,
    hold_ms: int,
    rule: StopRule,
    fees: Fees,
    bars: int = WINDOW_BARS,
) -> tuple[float | None, int]:
    """(R net, open time of the fill hour) of a buy limit at ``level`` resting over the 1h ``path`` bars opening in
    [t_in, t_in + bars * bar_ms); (None, -1) if unfilled or cancelled (a bar reaching ``target`` before the fill);
    (None, t) if filled without a path. Fill by a trade through the level; the fill hour is pessimistic
    (``setup_passport.limit_trade``). Reads bars from ``t_in`` up to the exit only."""
    if not (stop < level < target):
        return None, -1
    o, tk = limit_trade(path, t_in, t_in + bars * bar_ms, level, stop, target, hold_ms, rule, fees, True, True)
    return (o.r_net if o is not None else None), tk


def delayed_entry(
    path: Bars,
    t_in: int,
    d_hours: int,
    stop: float,
    target: float,
    hold_ms: int,
    rule: StopRule,
    fees: Fees,
) -> float | None:
    """Placebo R net of a market entry at the open of the 1h bar opening at ``t_in + d_hours`` h (long price space).
    0.0 (cancelled) if a bar before it reaches the stop or the target, or its open is not strictly between them; None if
    that bar does not exist (a hole) or the trade has no path. Held ``hold_ms`` from the entry hour."""
    te = t_in + d_hours * HOUR_MS
    i0 = int(np.searchsorted(path.t, t_in))
    ie = int(np.searchsorted(path.t, te))
    if ie >= len(path) or int(path.t[ie]) != te:
        return None
    if ie > i0 and (float(path.low[i0:ie].min()) <= stop or float(path.h[i0:ie].max()) >= target):
        return 0.0
    px = float(path.o[ie])
    if not (stop < px < target):
        return 0.0
    o = simulate_stop_rule(path, te, px, stop, target, hold_ms, rule, fees=fees)
    return o.r_net if o is not None else None


def placebo_signal_r(
    by_delay: Sequence[float | None],
    u_sel: Sequence[float],
    u_delay: Sequence[float],
    fill_freq: float,
    delay_pool: Sequence[int],
) -> float:
    """Placebo R of one signal: the mean over draws j of [u_sel[j] < fill_freq] * by_delay[d_j], with d_j the
    ``u_delay[j]`` quantile of ``delay_pool`` (the empirical fill hours of the limit); draws whose R is None (no bar or
    path) are left out; NaN if none is left."""
    pool = np.sort(np.asarray(delay_pool, dtype=np.int64))
    vals: list[float] = []
    for us, ud in zip(u_sel, u_delay, strict=True):
        if us >= fill_freq:
            vals.append(0.0)
            continue
        if pool.size == 0:
            continue
        d = int(pool[min(pool.size - 1, int(ud * pool.size))])
        r = by_delay[d] if 0 <= d < len(by_delay) else None
        if r is not None and math.isfinite(r):
            vals.append(float(r))
    return float(np.mean(vals)) if vals else math.nan


def rescored(score_base: int, rr_base: float, rr_new: float) -> int:
    """The demo score with the R:R points (R:R >= 2, R:R >= 3; ``setup_passport.score_points``) recomputed at a new
    entry; the trend points stay."""
    return int(score_base) - int(rr_base >= 2) - int(rr_base >= 3) + int(rr_new >= 2) + int(rr_new >= 3)


def demo_eligible(rr: float, stop_pct: float, score: int, min_rr: float, max_stop: float, min_score: int) -> bool:
    """The demo's filter at placement: R:R after fees, stop distance (fraction of price) and score."""
    return bool(rr >= min_rr and stop_pct <= max_stop and score >= min_score)


def cluster_se(values: Sequence[float], clusters: Sequence[object]) -> float:
    """Cluster-robust (CR1) standard error of the mean of ``values``: sqrt(k / (k - 1) * sum_c (sum_{i in c}
    (v_i - mean))^2) / n with k clusters. Returns only the dispersion (the gate reads no mean). NaN if k < 2."""
    v = np.asarray(values, dtype=float)
    c = np.asarray(clusters)
    if v.size < 2:
        return math.nan
    _, inv = np.unique(c, return_inverse=True)
    k = int(inv.max()) + 1
    if k < 2:
        return math.nan
    s = np.bincount(inv, weights=v - v.mean(), minlength=k)
    return float(math.sqrt(k / (k - 1) * float(np.sum(s * s))) / v.size)


def mde80(se: float, alpha_one_sided: float, power: float = 0.80) -> float:
    """Minimum detectable effect: (z_{1 - alpha} + z_{power}) * se."""
    nd = NormalDist()
    return float((nd.inv_cdf(1.0 - alpha_one_sided) + nd.inv_cdf(power)) * se)
