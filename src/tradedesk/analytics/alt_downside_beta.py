"""Asymmetric (downside vs upside) beta of an alt index to BTC and a beta-hedged "short alts / long BTC" basket that is
held only while BTC is below its moving average.

Pure functions (no I/O). Time is passed in as UTC day indices (``ms // DAY_MS``); returns are decimals (0.01 = 1 %).
Idea card: docs/ideas/IDEA-20261015-alt-downside-beta-regime-basket.md (section "Попередня реєстрація").

Conventions (registered):
- the universe at Sunday ``t`` uses bars strictly before ``t`` only (age, median turnover, one symbol per asset);
- a daily return of day ``d`` is ``close[d] / close[d - 1] - 1`` (Binance daily candle, 00:00-24:00 UTC);
- the basket return is per unit of alt-leg notional: each alt gets ``1/n`` short, BTC gets ``beta`` long.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from .funding_crowding import Bars, Candidate, Pick, WeekResult, median_turnover, ols_beta

# ---------------------------------------------------------------- point-in-time universe


def universe_at(
    bars: Mapping[str, Bars],
    day: int,
    *,
    n: int,
    asset_of: Mapping[str, str],
    starts: Mapping[str, Mapping[int, int]],
    min_age: int = 60,
    min_median_qv: float = 5e6,
    exclude: Sequence[str] = ("BTCUSDT",),
) -> list[Candidate]:
    """Top-``n`` symbols by median daily quote volume over the 30 days before ``day`` (largest first).

    A symbol qualifies with a bar on ``day - 1``, a continuous run of bars of at least ``min_age`` days ending there
    (``starts[sym]`` is ``run_start_map``: each value depends on earlier bars only) and median turnover >= the floor.
    One symbol per asset (``asset_of``): the one with the larger median turnover. Nothing at or after ``day`` is read."""
    best: dict[str, Candidate] = {}
    for s, b in bars.items():
        if s in exclude or (day - 1) not in b.close:
            continue
        rs = starts[s].get(day - 1)
        if rs is None or day - rs < min_age:
            continue
        mq = median_turnover(b, day)
        if mq is None or mq < min_median_qv:
            continue
        a = asset_of.get(s, s)
        c = Candidate(s, 0.0, mq, day - rs)
        if a not in best or mq > best[a].median_qv or (mq == best[a].median_qv and s < best[a].symbol):
            best[a] = c
    return sorted(best.values(), key=lambda c: (-c.median_qv, c.symbol))[:n]


def day_return(b: Bars, d: int) -> float | None:
    """Close-to-close return of day ``d``; None without bars on ``d`` and ``d - 1``."""
    if d in b.close and (d - 1) in b.close:
        return b.close[d] / b.close[d - 1] - 1.0
    return None


def index_return(members: Sequence[str], bars: Mapping[str, Bars], d: int) -> tuple[float | None, int]:
    """Equal-weight mean of the members' day-``d`` returns (members without both bars are skipped): (mean, n used)."""
    rs = [r for s in members if (r := day_return(bars[s], d)) is not None]
    return (statistics.fmean(rs) if rs else None), len(rs)


# ---------------------------------------------------------------- regimes


def below_sma(btc: Bars, day: int, window: int = 50) -> bool | None:
    """True when the BTC close of ``day - 1`` is below the mean close of days ``day - window .. day - 1``.

    None with fewer than 90 % of the window's closes or without the close of ``day - 1``."""
    cl = [btc.close[d] for d in range(day - window, day) if d in btc.close]
    if len(cl) < window * 0.9 or (day - 1) not in btc.close:
        return None
    return btc.close[day - 1] < statistics.fmean(cl)


def negative_week(btc: Bars, day: int) -> bool | None:
    """True when BTC fell over the 7 days before ``day``: close(day - 1) / close(day - 8) < 1."""
    if (day - 1) not in btc.close or (day - 8) not in btc.close:
        return None
    return btc.close[day - 1] < btc.close[day - 8]


def episodes(flags: Sequence[bool]) -> list[int]:
    """Lengths of the runs of consecutive True values."""
    out: list[int] = []
    run = 0
    for f in flags:
        if f:
            run += 1
        elif run:
            out.append(run)
            run = 0
    if run:
        out.append(run)
    return out


# ---------------------------------------------------------------- regressions


def ols_hac(y: Sequence[float], x: Sequence[Sequence[float]], lag: int) -> tuple[np.ndarray, np.ndarray]:
    """OLS of ``y`` on the columns ``x`` (pass a column of ones for the intercept) with a Newey-West (Bartlett)
    covariance of the coefficients: (coef, cov)."""
    yy = np.asarray(y, dtype=float)
    xx = np.column_stack([np.asarray(c, dtype=float) for c in x])
    n = len(yy)
    xtx_inv = np.linalg.inv(xx.T @ xx)
    coef = xtx_inv @ xx.T @ yy
    u = (yy - xx @ coef)[:, None] * xx
    s = u.T @ u
    for k in range(1, min(lag, n - 1) + 1):
        g = u[k:].T @ u[:-k]
        s += (1.0 - k / (lag + 1.0)) * (g + g.T)
    return coef, xtx_inv @ s @ xtx_inv


@dataclass(frozen=True)
class TwoSlope:
    a: float
    b_dn: float  # sum over the BTC lags used
    b_up: float
    delta: float  # b_dn - b_up
    se_delta: float
    se_b_dn: float
    se_b_up: float
    n: int

    def ci(self, z: float = 1.96) -> tuple[float, float]:
        return self.delta - z * self.se_delta, self.delta + z * self.se_delta

    def mde(self, z: float = 2.8) -> float:
        return z * self.se_delta


def two_slope(y: Sequence[float], x: Sequence[float], *, lag: int = 5, x_prev: Sequence[float] | None = None) -> TwoSlope:
    """``y = a + b_dn * min(x, 0) + b_up * max(x, 0) [+ lagged terms of x_prev] + e`` with Newey-West SEs.

    With ``x_prev`` (Dimson) the reported slopes are the sums over lag 0 and lag 1."""
    cols: list[list[float]] = [[1.0] * len(y), [min(v, 0.0) for v in x], [max(v, 0.0) for v in x]]
    if x_prev is not None:
        cols += [[min(v, 0.0) for v in x_prev], [max(v, 0.0) for v in x_prev]]
    coef, cov = ols_hac(y, cols, lag)
    k = len(cols)
    c_dn = np.zeros(k)
    c_up = np.zeros(k)
    c_dn[1] = 1.0
    c_up[2] = 1.0
    if x_prev is not None:
        c_dn[3] = 1.0
        c_up[4] = 1.0
    c_d = c_dn - c_up

    def se(c: np.ndarray) -> float:
        return float(math.sqrt(max(float(c @ cov @ c), 0.0)))

    return TwoSlope(float(coef[0]), float(c_dn @ coef), float(c_up @ coef), float(c_d @ coef), se(c_d), se(c_dn), se(c_up), len(y))


def trailing_index_downside_beta(
    members: Sequence[str], bars: Mapping[str, Bars], btc: Bars, day: int, *, window: int = 60, min_days: int = 40
) -> float | None:
    """``b_dn`` of the members' equal-weight daily index on BTC over days ``day - window .. day - 1`` (plain OLS)."""
    ys, xs = [], []
    for d in range(day - window, day):
        r, k = index_return(members, bars, d)
        rb = day_return(btc, d)
        if r is not None and rb is not None:
            ys.append(r)
            xs.append(rb)
    if len(ys) < min_days or sum(v < 0 for v in xs) < 10 or sum(v > 0 for v in xs) < 10:
        return None
    coef, _ = ols_hac(ys, [[1.0] * len(ys), [min(v, 0.0) for v in xs], [max(v, 0.0) for v in xs]], 0)
    return float(coef[1])


# ---------------------------------------------------------------- basket


BTC = "BTCUSDT"


def hedge_pick(members: Sequence[str], beta: float | None, *, btc: str = BTC, min_members: int = 20) -> Pick:
    """Short ``members`` equally (1/n each) and long ``beta`` of BTC, per unit of alt-leg notional.

    Expressed for ``funding_crowding.simulate`` (which puts 0.5 * scale on each side): long_scale = 2 * beta,
    short_scale = 2. Flat (empty pick) with fewer than ``min_members`` or no usable beta."""
    if len(members) < min_members or beta is None or not math.isfinite(beta) or beta <= 0:
        return Pick([], [])
    return Pick([btc], list(members), 2.0 * beta, 2.0)


def mean_beta(betas: Sequence[float | None]) -> float | None:
    """Mean of the defined betas (the beta of an equal-weight basket of those members); None if none defined."""
    v = [b for b in betas if b is not None]
    return statistics.fmean(v) if v else None


@dataclass(frozen=True)
class H3:
    a: float
    b_dn: float
    b_up: float
    price: float  # mean weekly price return of the hedged short basket
    intercept: float  # -a
    convexity: float  # (b_dn - b_up) / 2 * mean |r_btc|
    drift: float  # mean((beta_t - (b_dn + b_up) / 2) * r_btc)
    n: int

    @property
    def drift_share(self) -> float | None:
        return self.drift / self.price if self.price > 0 else None


def decompose(r_alt: Sequence[float], r_btc: Sequence[float], beta: Sequence[float]) -> H3:
    """Exact decomposition of the mean price return of ``-r_alt + beta * r_btc`` via the two-slope OLS of r_alt on r_btc.

    mean = -a + (b_dn - b_up) / 2 * mean|r_btc| + mean((beta - (b_dn + b_up) / 2) * r_btc) (OLS with intercept)."""
    n = len(r_alt)
    coef, _ = ols_hac(r_alt, [[1.0] * n, [min(v, 0.0) for v in r_btc], [max(v, 0.0) for v in r_btc]], 0)
    a, bd, bu = (float(v) for v in coef)
    price = statistics.fmean(-ra + b * rb for ra, rb, b in zip(r_alt, r_btc, beta, strict=True))
    conv = (bd - bu) / 2.0 * statistics.fmean(abs(v) for v in r_btc)
    drift = statistics.fmean((b - (bd + bu) / 2.0) * rb for rb, b in zip(r_btc, beta, strict=True))
    return H3(a, bd, bu, price, -a, conv, drift, n)


def active_net(results: Sequence[WeekResult]) -> list[tuple[int, float]]:
    """(day, net) of the periods with a position; the closing cost charged by ``simulate`` to the first flat period
    after an episode is moved back to the last period of that episode, so an episode carries its full round trip."""
    out: list[tuple[int, float]] = []
    prev_active = False
    for r in results:
        active = r.n_side > 0
        if active:
            out.append((r.day, r.net))
        elif prev_active and r.cost:
            out[-1] = (out[-1][0], out[-1][1] - r.cost)
        prev_active = active
    return out


def path_mae(
    members: Sequence[str], bars: Mapping[str, Bars], btc: Bars, beta: float, day: int, *, hold: int = 7, btc_sym: str = BTC
) -> float | None:
    """Worst cumulative P&L of the hedged short basket at the daily closes of ``day .. day + hold - 1`` (from the
    open of ``day``; a member without a close carries its last earlier close). Per unit of alt-leg notional."""
    ms = [s for s in members if day in bars[s].open]
    if not ms or day not in btc.open:
        return None
    worst = 0.0
    last = {s: bars[s].open[day] for s in ms}
    last_b = btc.open[day]
    for d in range(day, day + hold):
        for s in ms:
            if d in bars[s].close:
                last[s] = bars[s].close[d]
        if d in btc.close:
            last_b = btc.close[d]
        alt = statistics.fmean(last[s] / bars[s].open[day] - 1.0 for s in ms)
        worst = min(worst, -alt + beta * (last_b / btc.open[day] - 1.0))
    return worst


# ---------------------------------------------------------------- inference


def block_bootstrap_mean_ci(
    xs: Sequence[float], *, block: int, n_boot: int = 10_000, seed: int = 7, alpha: float = 0.05
) -> tuple[float, float]:
    """Percentile CI of the mean by the moving-block bootstrap with blocks of ``block`` consecutive values."""
    n = len(xs)
    block = max(1, min(block, n))
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        acc: list[float] = []
        while len(acc) < n:
            i = rng.randrange(n - block + 1)
            acc.extend(xs[i : i + block])
        means.append(statistics.fmean(acc[:n]))
    means.sort()
    return means[int(math.floor(alpha / 2 * n_boot))], means[min(n_boot - 1, int(math.ceil((1 - alpha / 2) * n_boot)) - 1)]


def diff_means(xs: Sequence[float], flags: Sequence[bool]) -> float | None:
    """mean(xs | flag) - mean(xs | not flag); None when a group is empty."""
    a = [x for x, f in zip(xs, flags, strict=True) if f]
    b = [x for x, f in zip(xs, flags, strict=True) if not f]
    if not a or not b:
        return None
    return statistics.fmean(a) - statistics.fmean(b)


def block_bootstrap_diff_ci(
    xs: Sequence[float], flags: Sequence[bool], *, block: int, n_boot: int = 10_000, seed: int = 7, alpha: float = 0.05
) -> tuple[float, float]:
    """Moving-block bootstrap CI of ``diff_means`` resampling (value, flag) pairs together in blocks."""
    n = len(xs)
    block = max(1, min(block, n))
    pairs = list(zip(xs, flags, strict=True))
    rng = random.Random(seed)
    out = []
    while len(out) < n_boot:
        acc: list[tuple[float, bool]] = []
        while len(acc) < n:
            i = rng.randrange(n - block + 1)
            acc.extend(pairs[i : i + block])
        d = diff_means([p[0] for p in acc[:n]], [p[1] for p in acc[:n]])
        if d is not None:
            out.append(d)
    out.sort()
    return out[int(math.floor(alpha / 2 * n_boot))], out[min(n_boot - 1, int(math.ceil((1 - alpha / 2) * n_boot)) - 1)]


def circular_shift_test(xs: Sequence[float], flags: Sequence[bool]) -> tuple[float, float, list[float]]:
    """Regime-label null that keeps the autocorrelation of the regime: every non-trivial circular shift of ``flags``.

    Returns (observed diff, one-sided p = (1 + #{shift >= observed}) / n, shift stats)."""
    obs = diff_means(xs, flags)
    if obs is None:
        return float("nan"), float("nan"), []
    n = len(xs)
    stats = []
    for k in range(1, n):
        f = list(flags[-k:]) + list(flags[:-k])
        d = diff_means(xs, f)
        if d is not None:
            stats.append(d)
    p = (1 + sum(s >= obs for s in stats)) / (1 + len(stats))
    return obs, p, stats


def beta_of(ys: Sequence[float], xs: Sequence[float]) -> float:
    """Plain OLS slope (re-export for scripts)."""
    return ols_beta(ys, xs)
