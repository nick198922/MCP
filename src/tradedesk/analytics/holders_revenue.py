"""Holders-revenue collapse events and their follow-up returns (research for IDEA-20261015-holders-revenue-collapse-lag).

Pure functions, no I/O. Time is a UTC day index (``unix_seconds // 86400``); revenue in USD per day; returns are
decimal fractions of the entry notional (0.05 = 5 %).

Point in time: the signal on day ``d`` uses revenue for days ``<= d`` only; with ``lag`` days of publication delay the
position is entered at the close of day ``d + lag``. Returns are measured from the close of the entry day to the close
of ``entry + horizon``.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

DAY_S = 86_400


# ----------------------------------------------------------------------------- revenue series


def to_daily(points: Iterable[Sequence[float]]) -> dict[int, float]:
    """DefiLlama ``totalDataChart`` rows ``[unix_seconds, usd]`` -> ``{day: usd}`` (rows of the same day are summed)."""
    out: dict[int, float] = {}
    for p in points:
        if len(p) < 2 or p[1] is None:
            continue
        d = int(p[0]) // DAY_S
        out[d] = out.get(d, 0.0) + float(p[1])
    return out


def combine(series: Iterable[Mapping[int, float]]) -> dict[int, float]:
    """Sum several protocol series of one token; a day is present when any component has a row for it."""
    out: dict[int, float] = {}
    for s in series:
        for d, v in s.items():
            out[d] = out.get(d, 0.0) + v
    return out


@dataclass(frozen=True)
class DropRule:
    window: int = 30  # days in each of the two compared windows
    drop: float = 0.5  # signal when current <= (1 - drop) * prior
    min_prior: float = 100_000.0  # USD, prior-window sum
    min_history: int = 180  # days from the first non-zero row to the signal day
    regular_days: int = 90  # look-back for the regularity filter, ending at the prior window's last day
    min_nonzero: int = 12  # non-zero days required in that look-back (excludes monthly / one-off payers)
    refractory: int = 60  # min days between two events of one token
    lag: int = 1  # publication delay: entry at the close of signal_day + lag


@dataclass(frozen=True)
class RevenueEvent:
    signal_day: int  # last day of the current window
    entry_day: int  # signal_day + lag; position opened at this day's close
    current: float  # USD, current window
    prior: float  # USD, prior window
    ratio: float  # current / prior


def window_state(series: Mapping[int, float], day: int, rule: DropRule) -> tuple[float, float, int] | None:
    """``(current, prior, nonzero_days_in_regularity_lookback)`` on ``day`` or None when any day of the two windows has
    no row (missing data is not a zero)."""
    w = rule.window
    cur = prior = 0.0
    for k in range(2 * w):
        d = day - k
        if d not in series:
            return None
        if k < w:
            cur += series[d]
        else:
            prior += series[d]
    end_prior = day - w
    nz = sum(1 for d in range(end_prior - rule.regular_days + 1, end_prior + 1) if series.get(d, 0.0) > 0)
    return cur, prior, nz


def qualifies(series: Mapping[int, float], day: int, rule: DropRule, first_nonzero: int | None) -> RevenueEvent | None:
    """The event that fires on ``day`` (ignoring the refractory period), or None."""
    if first_nonzero is None or day - first_nonzero < rule.min_history:
        return None
    st = window_state(series, day, rule)
    if st is None:
        return None
    cur, prior, nz = st
    if prior < rule.min_prior or nz < rule.min_nonzero:
        return None
    if cur > (1.0 - rule.drop) * prior:
        return None
    return RevenueEvent(day, day + rule.lag, cur, prior, cur / prior)


def drop_events(
    series: Mapping[int, float],
    rule: DropRule,
    *,
    first_day: int,
    last_day: int,
    tradable: Callable[[int], bool] | None = None,
) -> list[RevenueEvent]:
    """Events on signal days in ``[first_day, last_day]``, at least ``rule.refractory`` days apart.

    ``tradable(entry_day)`` filters days on which the position could not be opened (no perp yet); a non-tradable day
    neither fires nor starts a refractory period. Only rows on or before each signal day are read.
    """
    first = min((d for d, v in series.items() if v > 0), default=None)
    out: list[RevenueEvent] = []
    blocked_until = -(10**9)
    for day in range(first_day, last_day + 1):
        if day < blocked_until:
            continue
        ev = qualifies(series, day, rule, first if first is not None and first <= day else None)
        if ev is None or (tradable is not None and not tradable(ev.entry_day)):
            continue
        out.append(ev)
        blocked_until = day + rule.refractory
    return out


# ----------------------------------------------------------------------------- prices and returns


def log_returns(closes: Mapping[int, float], start: int, end: int) -> dict[int, float]:
    """Daily log returns ``ln(c[d] / c[d-1])`` for ``d`` in ``(start, end]`` where both closes exist."""
    out = {}
    for d in range(start + 1, end + 1):
        a, b = closes.get(d - 1), closes.get(d)
        if a and b and a > 0 and b > 0:
            out[d] = math.log(b / a)
    return out


def beta(token: Mapping[int, float], btc: Mapping[int, float], end_day: int, *, days: int = 60, min_obs: int = 25) -> float | None:
    """OLS beta of token daily log returns on BTC's over ``(end_day - days, end_day]``; None if fewer than ``min_obs``."""
    rt = log_returns(token, end_day - days, end_day)
    rb = log_returns(btc, end_day - days, end_day)
    common = sorted(rt.keys() & rb.keys())
    if len(common) < min_obs:
        return None
    x = [rb[d] for d in common]
    y = [rt[d] for d in common]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    vx = sum((a - mx) ** 2 for a in x)
    if vx <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / vx


def simple_return(closes: Mapping[int, float], a: int, b: int) -> float | None:
    pa, pb = closes.get(a), closes.get(b)
    if not pa or not pb or pa <= 0:
        return None
    return pb / pa - 1.0


def hedged_return(token: Mapping[int, float], btc: Mapping[int, float], entry: int, exit_: int, b: float) -> float | None:
    """Token return minus ``b`` x BTC return over (entry close, exit close]: the long-token / short-BTC excess.

    A short-token / long-BTC position earns the negative of this (before costs and funding)."""
    rt, rb = simple_return(token, entry, exit_), simple_return(btc, entry, exit_)
    if rt is None or rb is None:
        return None
    return rt - b * rb


def max_adverse_short(highs: Mapping[int, float], entry_close: float, entry: int, exit_: int) -> float | None:
    """Largest rise of the token's daily high above the entry close during (entry, exit]: the short leg's worst run-up."""
    hs = [highs[d] for d in range(entry + 1, exit_ + 1) if d in highs]
    if not hs or entry_close <= 0:
        return None
    return max(hs) / entry_close - 1.0


def funding_sum(records: Sequence[tuple[int, float]], start_s: int, end_s: int) -> float:
    """Sum of funding rates with timestamps (unix seconds) in (start, end]; longs pay when positive."""
    return sum(r for t, r in records if start_s < t <= end_s)


@dataclass(frozen=True)
class LegResult:
    """One token over one holding window: beta to BTC, hedged excess return, funding sum, cost per side."""

    beta: float
    ar: float  # r_token - beta * r_btc
    funding: float  # sum of the token's funding rates over the hold (longs pay when positive)
    side_cost: float  # taker fee + slippage per side, fraction of notional


def pair_net(event: LegResult, controls: Sequence[LegResult], *, btc_funding: float, btc_side_cost: float) -> tuple[float, float]:
    """Short the event token, long an equal-weight basket of controls, net BTC hedge ``beta_e - mean(beta_c)``.

    Returns ``(gross, net)`` per unit notional of the token leg: gross = mean(AR_c) - AR_e; net subtracts a round trip
    on each leg (token, basket, |net BTC|), adds the funding the short token receives, the basket's funding paid and
    the net BTC leg's funding.
    """
    if not controls:
        raise ValueError("at least one control")
    ar_c = statistics.fmean(c.ar for c in controls)
    b_c = statistics.fmean(c.beta for c in controls)
    f_c = statistics.fmean(c.funding for c in controls)
    cost_c = statistics.fmean(c.side_cost for c in controls)
    gross = ar_c - event.ar
    b_net = event.beta - b_c
    costs = 2 * event.side_cost + 2 * cost_c + abs(b_net) * 2 * btc_side_cost
    net = gross - costs + event.funding - f_c - b_net * btc_funding
    return gross, net


def short_vs_btc_net(event: LegResult, *, btc_funding: float, btc_side_cost: float) -> tuple[float, float]:
    """Short the token, long ``beta`` BTC: ``(gross, net)`` per unit token notional (gross = -AR)."""
    gross = -event.ar
    costs = 2 * event.side_cost + abs(event.beta) * 2 * btc_side_cost
    return gross, gross - costs + event.funding - event.beta * btc_funding


# ----------------------------------------------------------------------------- matching


def excluded_from_control_pool(own_entry_days: Iterable[int], entry: int, *, lookback: int = 60) -> bool:
    """True if a candidate control had its own event entry in ``[entry - lookback, entry]``.

    Only past (and same-day) events exclude a candidate: excluding candidates with *future* events would use
    information not available at ``entry`` (and would remove future underperformers from the control basket)."""
    return any(entry - lookback <= d <= entry for d in own_entry_days)


def nearest_controls(target: float, candidates: Mapping[str, float], k: int) -> list[str]:
    """Up to ``k`` candidate names whose value is closest to ``target`` (ties broken by name)."""
    return [n for n, _ in sorted(candidates.items(), key=lambda kv: (abs(kv[1] - target), kv[0]))[:k]]


def rank_controls(target: Sequence[float], candidates: Mapping[str, Sequence[float]], k: int) -> list[str]:
    """Up to ``k`` candidates nearest to ``target`` by the sum of absolute rank differences over all features.

    Ranks are taken within ``candidates`` plus the target (average rank for ties), so features on different scales
    weigh the same."""
    names = list(candidates)
    if not names:
        return []
    dims = len(target)
    dist = dict.fromkeys(names, 0.0)
    for j in range(dims):
        vals = [(target[j], "\x00target")] + [(candidates[n][j], n) for n in names]
        vals.sort(key=lambda t: t[0])
        rank: dict[str, float] = {}
        i = 0
        while i < len(vals):
            m = i
            while m + 1 < len(vals) and vals[m + 1][0] == vals[i][0]:
                m += 1
            for q in range(i, m + 1):
                rank[vals[q][1]] = (i + m) / 2.0
            i = m + 1
        for n in names:
            dist[n] += abs(rank[n] - rank["\x00target"])
    return sorted(names, key=lambda n: (dist[n], n))[:k]


# ----------------------------------------------------------------------------- statistics


def cluster_se(values: Sequence[float], clusters: Sequence[Sequence[object]]) -> float:
    """Standard error of the mean with multi-way clustering (Cameron-Gelbach-Miller).

    ``clusters[j][i]`` is observation i's id in dimension j. With two dimensions the variance is
    V(a) + V(b) - V(a x b); one dimension gives the usual cluster-robust SE; a negative total is floored at the
    largest one-way variance.
    """
    n = len(values)
    if n < 2:
        return float("nan")
    m = statistics.fmean(values)
    e = [v - m for v in values]

    def v_of(keys: Sequence[object]) -> float:
        sums: dict[object, float] = {}
        for k, x in zip(keys, e, strict=True):
            sums[k] = sums.get(k, 0.0) + x
        g = len(sums)
        adj = g / (g - 1) if g > 1 else 1.0
        return adj * sum(s * s for s in sums.values()) / (n * n)

    if len(clusters) == 1:
        return math.sqrt(v_of(clusters[0]))
    if len(clusters) != 2:
        raise ValueError("one or two clustering dimensions")
    va, vb = v_of(clusters[0]), v_of(clusters[1])
    vab = v_of([(a, b) for a, b in zip(clusters[0], clusters[1], strict=True)])
    v = va + vb - vab
    if v <= 0:
        v = max(va, vb)
    return math.sqrt(v)


def cluster_bootstrap_mean_ci(
    values: Sequence[float], clusters: Sequence[object], *, reps: int = 5000, seed: int = 11, level: float = 0.95
) -> tuple[float, float] | None:
    """Percentile CI of the mean, resampling whole clusters."""
    groups: dict[object, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        groups.setdefault(c, []).append(v)
    keys = list(groups)
    if len(keys) < 3:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(reps):
        tot, cnt = 0.0, 0
        for _ in keys:
            g = groups[keys[rng.randrange(len(keys))]]
            tot += sum(g)
            cnt += len(g)
        means.append(tot / cnt)
    means.sort()
    lo = means[int((1 - level) / 2 * reps)]
    hi = means[min(reps - 1, int((1 + level) / 2 * reps))]
    return lo, hi
