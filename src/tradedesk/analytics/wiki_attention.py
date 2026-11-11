"""Attention shock from Wikipedia pageviews and the cross-sectional statistics of its weekly test (pure, no I/O).

Idea card: docs/ideas/IDEA-20261111-wikipedia-attention-xs-spread.md. Days are UTC day indices (unix days); a views
series is ``{day: views}`` where an absent day inside the article's life means zero views (the Pageviews API omits
zero days). Returns are decimal fractions (0.01 = 1 %).
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Mapping, Sequence
from statistics import NormalDist

# Mean of the standardised rank score in the top quintile minus the bottom quintile for a continuous uniform rank:
# u ~ U(0,1) has SD 1/sqrt(12); quintile means 0.9 and 0.1 -> (0.9 - 0.1) * sqrt(12).
QUINTILE_Z_GAP = 0.8 * math.sqrt(12.0)


def window_mean(views: Mapping[int, int], first: int, last: int) -> float:
    """Mean daily views over days ``first..last`` inclusive, absent days counted as zero."""
    n = last - first + 1
    if n <= 0:
        raise ValueError("empty window")
    return sum(views.get(d, 0) for d in range(first, last + 1)) / n


def window_median(views: Mapping[int, int], first: int, last: int) -> float:
    if last < first:
        raise ValueError("empty window")
    return float(statistics.median(views.get(d, 0) for d in range(first, last + 1)))


def attention_shock(
    views: Mapping[int, int], signal_day: int, *, lag: int = 1, window: int = 7, base: int = 90, first_day: int | None = None
) -> float | None:
    """A = ln((mean views over the last ``window`` days + 1) / (median over the ``base`` days before them + 1)).

    The last day used is ``signal_day - lag`` (lag 1: the signal formed on day t uses views up to day t-1 inclusive),
    so nothing on or after ``signal_day - lag + 1`` can change the value. ``first_day`` is the first day the series can
    have data (article creation / start of the API); None if the window plus base is not fully covered by it.
    The +1 keeps the ratio defined for tiny articles (median 0)."""
    last = signal_day - lag
    w0 = last - window + 1
    b1 = w0 - 1
    b0 = b1 - base + 1
    if first_day is not None and b0 < first_day:
        return None
    return math.log((window_mean(views, w0, last) + 1.0) / (window_median(views, b0, b1) + 1.0))


def spike_days(views: Mapping[int, int], *, mult: float = 20.0, base: int = 90, min_base: float = 1.0) -> list[int]:
    """Days whose views exceed ``mult`` x the median of the ``base`` days before them (bot/outlier screen; the median
    is floored at ``min_base`` so a near-empty article does not flag every visit). Uses only past days per day."""
    if not views:
        return []
    lo, hi = min(views), max(views)
    out = []
    for d in range(lo + base, hi + 1):
        v = views.get(d, 0)
        if v > mult * max(min_base, window_median(views, d - base, d - 1)):
            out.append(d)
    return out


def ranks(xs: Sequence[float]) -> list[float]:
    """Ranks 1..n, ties get their average rank."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def rank_z(xs: Sequence[float]) -> list[float]:
    """Cross-sectional ranks standardised to mean 0 and (population) SD 1."""
    r = ranks(xs)
    m = statistics.fmean(r)
    sd = statistics.pstdev(r)
    if sd == 0:
        return [0.0] * len(r)
    return [(x - m) / sd for x in r]


def ols_slope(y: Sequence[float], x: Sequence[float]) -> float:
    if len(y) != len(x) or len(y) < 2:
        raise ValueError("need two or more paired points")
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx == 0:
        raise ValueError("constant regressor")
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / sxx


def rank_ic(signal: Sequence[float], y: Sequence[float]) -> float:
    """Spearman correlation (Pearson on average ranks)."""
    rs, ry = ranks(signal), ranks(y)
    return statistics.correlation(rs, ry) if statistics.pstdev(rs) > 0 and statistics.pstdev(ry) > 0 else 0.0


def quintile_spread(signal: Sequence[float], y: Sequence[float], q: int = 5) -> float:
    """Equal-weight mean of ``y`` in the top 1/q by signal minus the bottom 1/q (k = max(1, n // q) names a side;
    ties broken by input order)."""
    n = len(signal)
    if n != len(y) or n < 2:
        raise ValueError("need two or more paired points")
    k = max(1, n // q)
    order = sorted(range(n), key=lambda i: signal[i])
    return statistics.fmean(y[i] for i in order[-k:]) - statistics.fmean(y[i] for i in order[:k])


def placebo_sd(y: Sequence[float], n_perm: int, rng: random.Random, q: int = 5) -> tuple[float, float, float]:
    """SD over random signals (a permutation of ranks) of: the slope of ``y`` on the rank z-score, the rank IC and the
    quintile spread. Uses only ``y`` (no signal), so it can be computed before any signal-vs-return mean is seen."""
    n = len(y)
    base = list(range(n))
    sl, ic, sp = [], [], []
    for _ in range(n_perm):
        rng.shuffle(base)
        sig = [float(v) for v in base]
        z = rank_z(sig)
        sl.append(ols_slope(y, z))
        ic.append(rank_ic(sig, y))
        sp.append(quintile_spread(sig, y, q))
    return statistics.stdev(sl), statistics.stdev(ic), statistics.stdev(sp)


def z_two_sided(alpha: float = 0.05, m: int = 1) -> float:
    """Critical z of a two-sided test at level alpha with a Bonferroni/Holm-first-step split over m hypotheses."""
    return NormalDist().inv_cdf(1.0 - alpha / (2.0 * m))


def mde(se: float, *, alpha: float = 0.05, m: int = 1, power: float = 0.8) -> float:
    """Minimum detectable effect of a two-sided z-test: (z_crit + z_power) * SE."""
    return (z_two_sided(alpha, m) + NormalDist().inv_cdf(power)) * se


def beta_ols(asset: Sequence[float], market: Sequence[float]) -> float:
    """OLS beta of ``asset`` daily returns on ``market`` daily returns."""
    return ols_slope(asset, market)
