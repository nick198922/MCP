"""Pure helpers of the trader-call evaluation (card IDEA-20261113-x-traders, item 3.3в): local-drift benchmark, placebo
time pools, survivorship break-even, within-block label permutation and the cost of a market.

Returns are decimals on notional (0.01 = 1 %), in the quote currency of the market. No I/O; time is a parameter.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence

from tradedesk.analytics.trade_replay import Bar
from tradedesk.analytics.trader_eval import between_ss, cluster_bootstrap_ci

H_MS = 3_600_000
D_MS = 86_400_000
COST_PERP = 0.0011 + 2 * 0.0002  # taker 0.055 % x 2 + slippage 0.02 % per side = 0.15 % round trip
COST_SPOT = 0.0020 + 2 * 0.0005  # taker 0.10 % x 2 + slippage 0.05 % per side = 0.30 % round trip (spot and */BTC)


def is_perp(market: str) -> bool:
    """``venue:SYMBOL`` of a linear perpetual (ccxt ``BASE/QUOTE:SETTLE``)."""
    return ":" in market.split(":", 1)[-1]


def cost_of(market: str) -> float:
    """Registered round-trip cost of a market (card 3.3в, p. 7): perps 0.15 %, spot and ``*/BTC`` 0.30 %."""
    return COST_PERP if is_perp(market) else COST_SPOT


def first_hour_after(t0_ms: int) -> int:
    """Open time of the first 1h bar after the one that contains ``t0_ms`` (that bar is never used, card 3.3в p. 3)."""
    return t0_ms // H_MS * H_MS + H_MS


def hold_return(by_t: Mapping[int, Bar], side: str, h_ms: int, *, hold_h: int, cost: float) -> float | None:
    """Net return of a market hold of ``side``: in at the open of bar ``h_ms``, out at the close of bar
    ``h_ms + (hold_h - 1) h``; None if either bar is missing."""
    a, b = by_t.get(h_ms), by_t.get(h_ms + (hold_h - 1) * H_MS)
    if a is None or b is None:
        return None
    g = b.close / a.open - 1.0
    return (g if side == "long" else -g) - cost


def local_drift(
    by_t: Mapping[int, Bar], side: str, t0_ms: int, *, cost: float, half_days: int = 30, hold_h: int = 168
) -> tuple[float, float]:
    """Local drift of the same side (card 3.3, metric p. 7): mean of ``hold_return`` over every hour h in
    [t0 − ``half_days``, t0 + ``half_days``] (hour grid of t0) with a full hold in the data. Returns (mean, share of
    hours used); (nan, 0) if none. A benchmark, not a decision rule: it reads data after t0 on purpose."""
    base = t0_ms // H_MS * H_MS
    hours = range(base - half_days * D_MS, base + half_days * D_MS + 1, H_MS)
    xs = [x for h in hours if (x := hold_return(by_t, side, h, hold_h=hold_h, cost=cost)) is not None]
    return (math.fsum(xs) / len(xs) if xs else float("nan")), len(xs) / len(hours)


def pool_starts(times: Sequence[int], blocks: Sequence[tuple[int, int]], margin_ms: int) -> list[int]:
    """Bar open times usable as a random placebo start: inside a block [lo, hi] and at least ``margin_ms`` before the
    last bar of that block on this market (the pool is clipped by the market's own bars). ``times`` sorted."""
    out: list[int] = []
    for lo, hi in blocks:
        a, b = bisect_left(times, lo), bisect_right(times, hi)
        if b <= a:
            continue
        last = times[b - 1]
        out += [t for t in times[a:b] if t + margin_ms <= last]
    return out


def window_starts(pool: Sequence[int], center_ms: int, half_ms: int) -> tuple[int, int]:
    """Index range [a, b) of ``pool`` (sorted) within [center − half, center + half] (placebo P2)."""
    return bisect_left(pool, center_ms - half_ms), bisect_right(pool, center_ms + half_ms)


def short_row_evaluable(fill_t: int | None, filled: bool, status: str, last_bar_t: int, hold_ms: int) -> bool:
    """Rule for a row whose bars stop early (card 3.3в, p. 2): evaluated if the order expired unfilled in a covered
    window, or it filled at f and f + hold is not later than the last bar (the whole hold window is in the data,
    however the trade ended). Independent of the trade's outcome."""
    if not filled:
        return status == "no_fill"
    return fill_t is not None and fill_t + hold_ms <= last_bar_t


def breakeven_mean(n: int, mean: float, k: int) -> float:
    """Equal loss x on notional for each of ``k`` rows without data that brings the mean of n + k rows to 0:
    (n · mean − k · x) / (n + k) = 0 → x = n · mean / k."""
    if k <= 0:
        raise ValueError("k must be positive")
    return n * mean / k


def breakeven_ci_low(
    values: Sequence[float],
    clusters: Sequence[int],
    extra_clusters: Sequence[int],
    *,
    step: float = 0.001,
    max_x: float = 1.0,
    ci: Callable[[Sequence[float], Sequence[int]], tuple[float, float]] | None = None,
) -> float | None:
    """Smallest x on the grid ``step`` (0.001 = 0.1 p.p.) such that adding a loss −x for each extra row (to its own
    cluster) gives a CI lower bound <= 0. The bound falls monotonically in x for a fixed resampling seed, so a
    bisection on the grid equals the linear search. None if even ``max_x`` keeps it above 0."""
    f = ci or (lambda v, c: cluster_bootstrap_ci(v, c, n_boot=10_000, seed=20261123))
    cl = list(clusters) + list(extra_clusters)

    def low(i: int) -> float:
        return f(list(values) + [-i * step] * len(extra_clusters), cl)[0]

    if low(0) <= 0:
        return 0.0
    hi = round(max_x / step)
    if low(hi) > 0:
        return None
    lo = 0  # low(lo) > 0, low(hi) <= 0
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if low(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return hi * step


def label_permutation_p_within(
    values: Sequence[float], labels: Sequence[str], blocks: Sequence[int], *, n: int = 2000, seed: int = 20261123
) -> float:
    """p of the between-label spread (``between_ss``) against shuffling the labels only within each block (ISO week):
    keeps the time structure, breaks the label → value link."""
    actual = between_ss(values, labels)
    idx: dict[int, list[int]] = {}
    for i, b in enumerate(blocks):
        idx.setdefault(b, []).append(i)
    rnd = random.Random(seed)
    lab = list(labels)
    hits = 0
    for _ in range(n):
        for ids in idx.values():
            sub = [lab[i] for i in ids]
            rnd.shuffle(sub)
            for i, s in zip(ids, sub, strict=True):
                lab[i] = s
        if between_ss(values, lab) >= actual - 1e-12:
            hits += 1
    return (1 + hits) / (1 + n)


def quantile(xs: Sequence[float], q: float) -> float:
    """Lower empirical quantile (the value at index floor(q · (n − 1)) of the sorted sample); nan if empty."""
    s = sorted(x for x in xs if not math.isnan(x))
    return s[int(q * (len(s) - 1))] if s else float("nan")
