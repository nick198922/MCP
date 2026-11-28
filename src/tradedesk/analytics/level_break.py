"""Level break of one venue's daily series against another's at a known date (pure, no I/O).

Built for card IDEA-20261017-doubletop-to-ours, item 4c: did Coinalyze's Bybit ``.6`` liquidations jump relative to
Binance ``.A`` when Bybit replaced its 1-per-second ``liquidation`` topic with ``allLiquidation`` (2025-02-20)?

Days are integers (unix seconds // 86400). Per day the series is ``y = log(L_b / OI_b) - log(L_a / OI_a)`` with ``OI``
taken on the PREVIOUS day (exposure at the day's start), so ``y(d)`` depends only on days ``d`` and ``d - 1``. The
statistic at a split day ``s`` is ``median(y, after) - median(y, before)`` over two windows of ``width`` days that
skip ``gap_before`` days before ``s`` and ``gap_after`` days from ``s``; it reads no day outside them.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from statistics import median

DAY = 86_400


def day_of(ts: int) -> int:
    return int(ts) // DAY


def gaps(days: Iterable[int]) -> list[tuple[int, int]]:
    """Missing day ranges (inclusive) between the first and the last present day."""
    s = sorted(set(days))
    return [(a + 1, b - 1) for a, b in zip(s, s[1:], strict=False) if b - a > 1]


def log_ratio_series(
    liq_a: Mapping[int, float], oi_a: Mapping[int, float], liq_b: Mapping[int, float], oi_b: Mapping[int, float]
) -> tuple[dict[int, float], dict[str, int]]:
    """``y(d)`` on days where all four inputs are present and positive; counts of the dropped days by reason."""
    y: dict[int, float] = {}
    dropped = {"missing": 0, "zero_liq": 0, "bad_oi": 0}
    for d in sorted(set(liq_a) | set(liq_b)):
        la, lb, oa, ob = liq_a.get(d), liq_b.get(d), oi_a.get(d - 1), oi_b.get(d - 1)
        if la is None or lb is None or oa is None or ob is None:
            dropped["missing"] += 1
        elif not (oa > 0 and ob > 0 and math.isfinite(oa) and math.isfinite(ob)):
            dropped["bad_oi"] += 1
        elif not (la > 0 and lb > 0 and math.isfinite(la) and math.isfinite(lb)):
            dropped["zero_liq"] += 1
        else:
            y[d] = math.log(lb / ob) - math.log(la / oa)
    return y, dropped


def windows(split: int, width: int, gap_before: int, gap_after: int) -> tuple[range, range]:
    """Before = ``[split - gap_before - width, split - gap_before)``; after = ``[split + gap_after, ... + width)``."""
    b_end = split - gap_before
    a_start = split + gap_after
    return range(b_end - width, b_end), range(a_start, a_start + width)


@dataclass(frozen=True)
class Split:
    split: int
    stat: float | None  # median(after) - median(before); None when a window is too thin
    n_before: int
    n_after: int


def split_stat(y: Mapping[int, float], split: int, width: int, gap_before: int, gap_after: int, min_frac: float) -> Split:
    before, after = windows(split, width, gap_before, gap_after)
    vb = [y[d] for d in before if d in y]
    va = [y[d] for d in after if d in y]
    ok = len(vb) >= min_frac * width and len(va) >= min_frac * width and vb and va
    return Split(split, (median(va) - median(vb)) if ok else None, len(vb), len(va))


def pooled_stat(
    series: list[Mapping[int, float]], split: int, width: int, gap_before: int, gap_after: int, min_frac: float
) -> float | None:
    """Mean over coins of :func:`split_stat`; None unless every coin has both windows filled enough."""
    stats = [split_stat(y, split, width, gap_before, gap_after, min_frac).stat for y in series]
    return None if not stats or any(s is None for s in stats) else sum(stats) / len(stats)  # type: ignore[misc]


def placebo(
    series: list[Mapping[int, float]], true_split: int, width: int, gap_before: int, gap_after: int, min_frac: float
) -> list[tuple[int, float]]:
    """The pooled statistic at every other split whose whole span ``[s - gap_before - width, s + gap_after + width)``
    leaves out the true transition ``[true_split - gap_before, true_split + gap_after)``, so the real change never
    leaks into the null. Splits with a too-thin window are left out. Neighbouring splits share most days, so the
    list is a descriptive null, not independent draws (about span / (2 * width + gaps) independent ones)."""
    days = [d for y in series for d in y]
    if not days:
        return []
    lo, hi = min(days), max(days)
    out = []
    for s in range(lo + gap_before + width, hi - gap_after - width + 2):
        start, end = s - gap_before - width, s + gap_after + width
        if end > true_split - gap_before and start < true_split + gap_after:
            continue
        v = pooled_stat(series, s, width, gap_before, gap_after, min_frac)
        if v is not None:
            out.append((s, v))
    return out


def sum_ratio_change(
    liq_a: Mapping[int, float],
    oi_a: Mapping[int, float],
    liq_b: Mapping[int, float],
    oi_b: Mapping[int, float],
    before: range,
    after: range,
) -> float | None:
    """Secondary, cascade-weighted: change of ``log(sum L_b / sum OI_b) - log(sum L_a / sum OI_a)`` from the before
    window to the after window, over days where all four inputs are present (OI of the previous day)."""

    def level(days: range) -> float | None:
        keep = [d for d in days if d in liq_a and d in liq_b and (d - 1) in oi_a and (d - 1) in oi_b]
        la, lb = sum(liq_a[d] for d in keep), sum(liq_b[d] for d in keep)
        oa, ob = sum(oi_a[d - 1] for d in keep), sum(oi_b[d - 1] for d in keep)
        if min(la, lb, oa, ob) <= 0:
            return None
        return math.log(lb / ob) - math.log(la / oa)

    lb_, la_ = level(before), level(after)
    return None if lb_ is None or la_ is None else la_ - lb_


def log_intensity(liq: Mapping[int, float], oi: Mapping[int, float]) -> dict[int, float]:
    """One venue alone: ``log(L(d) / OI(d - 1))`` on days with both positive (the single-venue diagnostic)."""
    out = {}
    for d, v in liq.items():
        o = oi.get(d - 1)
        if o is not None and o > 0 and v > 0 and math.isfinite(o) and math.isfinite(v):
            out[d] = math.log(v / o)
    return out


def block_bootstrap(
    series: list[Mapping[int, float]], before: range, after: range, block: int, reps: int, seed: int
) -> tuple[float, float, float]:
    """Moving-block bootstrap of the mean over ``series`` (coins) of ``median(after) - median(before)``.

    Each window is resampled in calendar-day blocks; the SAME day blocks are used for every coin, so the coins'
    same-day correlation is kept (independent resampling would make the pooled CI too narrow). Days a coin lacks
    are skipped for that coin. Returns (2.5 % quantile, 97.5 % quantile, standard deviation)."""
    rng = random.Random(seed)

    def pick(days: range) -> list[int]:
        n = len(days)
        b = max(1, min(block, n))
        out: list[int] = []
        while len(out) < n:
            i = rng.randrange(n - b + 1)
            out.extend(days[i : i + b])
        return out[:n]

    stats = []
    for _ in range(reps):
        db, da = pick(before), pick(after)
        diffs = []
        for y in series:
            vb, va = [y[d] for d in db if d in y], [y[d] for d in da if d in y]
            if vb and va:
                diffs.append(median(va) - median(vb))
        if diffs:
            stats.append(sum(diffs) / len(diffs))
    if not stats:
        return math.nan, math.nan, math.nan
    stats.sort()
    reps = len(stats)
    mean = sum(stats) / reps
    sd = math.sqrt(sum((s - mean) ** 2 for s in stats) / max(1, reps - 1))
    return _q(stats, 0.025), _q(stats, 0.975), sd


def _q(sorted_v: list[float], p: float) -> float:
    if not sorted_v:
        return math.nan
    k = (len(sorted_v) - 1) * p
    f = math.floor(k)
    c = min(f + 1, len(sorted_v) - 1)
    return sorted_v[f] + (sorted_v[c] - sorted_v[f]) * (k - f)


def placebo_rank(stat: float, null: list[float]) -> tuple[float, float]:
    """One-sided ``P(null >= stat)`` and two-sided ``P(|null| >= |stat|)``, both with the +1 correction."""
    n = len(null)
    up = (1 + sum(1 for v in null if v >= stat)) / (1 + n)
    two = (1 + sum(1 for v in null if abs(v) >= abs(stat))) / (1 + n)
    return up, two


@dataclass(frozen=True)
class Thresholds:
    material: float = math.log(1.5)  # a thinning that keeps at most 2/3 of the level
    equivalence: float = math.log(1.25)  # "no break": the CI excludes a change beyond +-25 %
    alpha: float = 0.05
    max_dropped_frac: float = 0.10


def verdict(
    pooled: float,
    ci: tuple[float, float],
    p_up: float,
    p_two: float,
    per_coin: Mapping[str, float],
    wide_sign_agrees: bool,
    dropped_frac: float,
    venue_a_extreme: bool,
    th: Thresholds = Thresholds(),
) -> tuple[str, str]:
    """Registered decision rule (card item 4c): ('break' | 'no_break' | 'other_break' | 'inconclusive', reason)."""
    lo, hi = ci
    if dropped_frac > th.max_dropped_frac:
        return "inconclusive", f"dropped days {dropped_frac:.0%} > {th.max_dropped_frac:.0%}"
    if venue_a_extreme:
        return "inconclusive", "the reference venue alone breaks at the split: the ratio cannot be pinned on venue b"
    same_sign_pos = all(v > 0 for v in per_coin.values())
    if pooled >= th.material and lo > 0 and p_up <= th.alpha and same_sign_pos and wide_sign_agrees:
        return "break", "venue b's level rose at the split beyond material size, CI > 0, beyond placebo"
    if hi < 0 and p_two <= th.alpha and all(v < 0 for v in per_coin.values()):
        return "other_break", "significant fall, not the thinning hypothesis; levels still not comparable"
    if -th.equivalence < lo and hi < th.equivalence and p_two > th.alpha:
        return "no_break", "CI inside the equivalence band and the split is ordinary against the placebo"
    return "inconclusive", "neither a material break nor equivalence"
