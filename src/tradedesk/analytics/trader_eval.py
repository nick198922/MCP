"""Evaluation of a public trader's calls on intraday bars against placebo trades of the same geometry.

Pure functions (no I/O; time is passed in as epoch milliseconds). Rules, fixed in the pre-registration of
docs/ideas/IDEA-20261113-x-traders.md (item 3.1) and chosen to be conservative where a bar hides the order of events:

* the call's moment ``start_ms`` is the tweet time; the replay starts on the first bar opening at or after it;
* entry (``plan_entry``): a disclosure of an open position enters at market (the open of the first bar). Otherwise the
  zone edge nearest to the price at the call decides: price inside the zone, or a single price within ``mkt_tol`` of
  it → market; long with price above the zone → limit at ``zone_high``; long with price below → stop order (breakout)
  at ``zone_low``; shorts mirror. ``style="limit"`` always rests a limit at the near edge (long ``zone_high``, short
  ``zone_low``), as the daily tracker does (a zone already passed fills at the first open);
* a limit fills on the first bar reaching it at ``min(open, entry)`` for a long; a stop order at ``max(open, entry)``;
* the stop counts from the fill bar, the target from the next bar; a bar reaching both is a stop (worst case);
  a stop on the wrong side of the fill is dropped (no R), a target on the wrong side is ignored;
* no exit within ``hold_ms`` after the fill bar → close of the last bar opening inside the hold window;
* an author's exit (``author=("stop", t)``) books the stop price; ``("close", t)`` books the close of the bar holding
  ``t``; if our bars show no fill before ``t``, the author's word that he was in counts: the fill is the planned entry;
* R = move in the call's direction / planned risk |planned entry − stop|; ``cost_frac`` (round trip, fraction of price)
  is converted to R the same way. Unfilled calls count as 0 R in per-call means (no trade, no cost).

Placebo trades keep the call's geometry as offsets from the price at the call (``relative_plan``) and place it at
another time and/or side (``placed_plan``). Statistics: ISO-week cluster bootstrap, permutation p-value, Holm, MDE,
Cochran's Q / I², label permutation test. Returns are decimals (0.05 = +5 %) in the direction of the call.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from tradedesk.analytics.trade_replay import Bar

SNOWFLAKE_EPOCH_MS = 1_288_834_974_657
WEEK_MS = 7 * 86_400_000
MONDAY_OFFSET_MS = 3 * 86_400_000  # 1970-01-01 was a Thursday; weeks start on Monday 00:00 UTC (1970-01-05 = week 1)
Z95_ONE_SIDED = 1.6448536269514722
Z80 = 0.8416212335729143


def snowflake_ms(tweet_id: int | str) -> int:
    """Creation time (epoch ms) encoded in an X/Twitter snowflake id."""
    return (int(tweet_id) >> 22) + SNOWFLAKE_EPOCH_MS


def week_of(ms: int) -> int:
    """Index of the ISO week (Monday 00:00 UTC start) holding ``ms``."""
    return (ms + MONDAY_OFFSET_MS) // WEEK_MS


@dataclass(frozen=True)
class Plan:
    side: str  # long | short
    kind: str  # market | limit | stop
    entry: float | None  # None for market
    stop: float | None
    target: float | None


def plan_entry(
    side: str,
    zone_low: float,
    zone_high: float,
    ref: float,
    *,
    disclosure: bool,
    stop: float | None,
    target: float | None,
    mkt_tol: float = 0.005,
    style: str = "nearest",
) -> Plan:
    """Order type and price of a call given the price ``ref`` at the call (open of the first bar after it)."""
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")
    lo, hi = min(zone_low, zone_high), max(zone_low, zone_high)
    near = hi if side == "long" else lo
    if disclosure:
        return Plan(side, "market", None, stop, target)
    if style == "limit":
        return Plan(side, "limit", near, stop, target)
    if lo <= ref <= hi or (lo == hi and abs(ref / lo - 1.0) <= mkt_tol):
        return Plan(side, "market", None, stop, target)
    long = side == "long"
    above = ref > hi
    if long:
        return Plan(side, "limit", hi, stop, target) if above else Plan(side, "stop", lo, stop, target)
    return Plan(side, "limit", lo, stop, target) if not above else Plan(side, "stop", hi, stop, target)


@dataclass(frozen=True)
class Trade:
    status: str  # no_data | no_fill | pending | stop | target | time | open | author_stop | author_close
    fill_t: int | None = None
    fill: float | None = None
    exit_t: int | None = None
    exit: float | None = None
    risk: float | None = None  # |planned entry - stop|, price units; None = no valid stop
    ret: float | None = None  # gross return in the call's direction
    r_gross: float | None = None
    r_net: float | None = None
    ret_net: float | None = None  # ret - cost_frac (return on notional)

    @property
    def filled(self) -> bool:
        return self.fill is not None


def _ret(long: bool, entry: float, px: float) -> float:
    return px / entry - 1.0 if long else 1.0 - px / entry


def simulate(
    bars: Sequence[Bar],
    plan: Plan,
    *,
    start_ms: int,
    fill_ms: int,
    hold_ms: int,
    cost_frac: float,
    cancel_ms: int | None = None,
    author: tuple[str, int] | None = None,
    sorted_bars: bool = False,
) -> Trade:
    """Replay ``plan`` on ``bars`` (any order unless ``sorted_bars``; bars before ``start_ms`` are ignored)."""
    w = list(bars) if sorted_bars else sorted(bars, key=lambda b: b.t)
    i0 = bisect_left([b.t for b in w], start_ms) if not sorted_bars else _first_at(w, start_ms)
    w = w[i0:]
    if not w:
        return Trade(status="no_data")
    long = plan.side == "long"
    window_end = start_ms + fill_ms if cancel_ms is None else min(start_ms + fill_ms, cancel_ms)
    if author is not None:
        window_end = min(window_end, author[1] + 1)  # a fill must precede the author's exit
    fi: int | None
    if plan.kind == "market":
        fi, fill = 0, w[0].open
    else:
        e = plan.entry
        assert e is not None
        fi = None
        for i, b in enumerate(w):
            if b.t >= window_end:
                break
            limit_hit = b.low <= e if long else b.high >= e
            stop_hit = b.high >= e if long else b.low <= e
            if (plan.kind == "limit" and limit_hit) or (plan.kind == "stop" and stop_hit):
                fi = i
                break
        if fi is None:
            if author is not None:
                fi, fill = 0, e  # the author says he was in; book the planned entry
            else:
                return Trade(status="no_fill" if w[-1].t >= window_end else "pending")
        elif plan.kind == "limit":
            fill = min(w[fi].open, e) if long else max(w[fi].open, e)
        else:
            fill = max(w[fi].open, e) if long else min(w[fi].open, e)
    planned = fill if plan.entry is None else plan.entry
    stop = plan.stop
    if stop is not None and (stop >= min(fill, planned) if long else stop <= max(fill, planned)):
        stop = None
    target = plan.target
    if target is not None and (target <= fill if long else target >= fill):
        target = None
    risk = abs(planned - stop) if stop is not None else None

    def done(status: str, exit_t: int, px: float) -> Trade:
        ret = _ret(long, fill, px)
        r_g = ret * fill / risk if risk else None
        r_n = (ret - cost_frac) * fill / risk if risk else None
        return Trade(status, w[fi].t, fill, exit_t, px, risk, ret, r_g, r_n, ret - cost_frac)

    if author is not None:
        how, t = author
        if how == "stop":
            if stop is None:
                return Trade(status="author_stop", fill_t=w[fi].t, fill=fill, exit_t=t, risk=None)
            return done("author_stop", t, stop)
        k = bisect_right([b.t for b in w], t) - 1
        k = max(k, fi)
        return done("author_close", w[k].t, w[k].close)
    end = w[fi].t + hold_ms
    last = fi
    for i in range(fi, len(w)):
        b = w[i]
        if b.t >= end:
            break
        last = i
        if stop is not None and (b.low <= stop if long else b.high >= stop):
            px = stop if i == fi else (min(b.open, stop) if long else max(b.open, stop))
            return done("stop", b.t, px)
        if i > fi and target is not None and (b.high >= target if long else b.low <= target):
            return done("target", b.t, target)
    covered = w[-1].t >= end
    return done("time" if covered else "open", w[last].t, w[last].close)


def _first_at(w: Sequence[Bar], t: int) -> int:
    lo, hi = 0, len(w)
    while lo < hi:
        mid = (lo + hi) // 2
        if w[mid].t < t:
            lo = mid + 1
        else:
            hi = mid
    return lo


@dataclass(frozen=True)
class RelPlan:
    """A plan as signed offsets from the price at the call, in the call's favourable direction (+ = profit side)."""

    kind: str
    entry_off: float | None
    stop_off: float | None
    target_off: float | None


def relative_plan(plan: Plan, ref: float) -> RelPlan:
    d = 1.0 if plan.side == "long" else -1.0

    def off(p: float | None) -> float | None:
        return None if p is None else d * (p / ref - 1.0)

    return RelPlan(plan.kind, off(plan.entry), off(plan.stop), off(plan.target))


def placed_plan(rel: RelPlan, side: str, ref: float) -> Plan:
    """The geometry of ``rel`` placed at price ``ref`` on ``side`` (a flipped side mirrors every offset)."""
    d = 1.0 if side == "long" else -1.0

    def px(o: float | None) -> float | None:
        return None if o is None else ref * (1.0 + d * o)

    return Plan(side, rel.kind, px(rel.entry_off), px(rel.stop_off), px(rel.target_off))


def per_call_r(trades: Sequence[Trade]) -> list[float]:
    """R per call for calls with a valid stop: unfilled = 0 R; filled without R (no valid stop) are dropped."""
    out = []
    for t in trades:
        if t.status in ("no_fill",):
            out.append(0.0)
        elif t.filled and t.r_net is not None:
            out.append(t.r_net)
    return out


def mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs) if xs else float("nan")


def cluster_bootstrap_ci(
    values: Sequence[float], clusters: Sequence[int], *, n_boot: int = 10_000, seed: int = 20261114, alpha: float = 0.05
) -> tuple[float, float]:
    """Percentile CI of the pooled mean, resampling whole clusters with replacement."""
    if len(values) != len(clusters) or not values:
        raise ValueError("values and clusters must be non-empty and of equal length")
    groups: dict[int, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        groups.setdefault(c, []).append(v)
    sums = [(math.fsum(g), len(g)) for g in groups.values()]
    rnd = random.Random(seed)
    k = len(sums)
    stats = []
    for _ in range(n_boot):
        s = n = 0.0
        for _ in range(k):
            a, b = sums[rnd.randrange(k)]
            s += a
            n += b
        stats.append(s / n)
    stats.sort()
    return stats[int(alpha / 2 * n_boot)], stats[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]


def permutation_p(actual: float, placebo: Sequence[float]) -> float:
    """One-sided p: share of placebo statistics at least as large as ``actual`` (with the +1 correction)."""
    return (1 + sum(1 for x in placebo if x >= actual)) / (1 + len(placebo))


def holm(pvals: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Holm step-down rejections, in the input order."""
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    m = len(pvals)
    rej = [False] * m
    for k, i in enumerate(order):
        if pvals[i] < alpha / (m - k):
            rej[i] = True
        else:
            break
    return rej


def mde(sd: float, n_eff: float, *, z_alpha: float = Z95_ONE_SIDED, z_power: float = Z80) -> float:
    """Minimum detectable mean (one-sided α 0.05, power 0.8 by default) for ``n_eff`` independent units."""
    return (z_alpha + z_power) * sd / math.sqrt(n_eff)


def power(effect: float, sd: float, n_eff: float, *, z_alpha: float = Z95_ONE_SIDED) -> float:
    """Normal-approximation power of a one-sided test to detect ``effect``."""
    z = effect / (sd / math.sqrt(n_eff)) - z_alpha
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def cochran_q(means: Sequence[float], ses: Sequence[float]) -> tuple[float, int, float]:
    """Cochran's Q, degrees of freedom and I² (0..1) for per-group means with standard errors."""
    w = [1.0 / s**2 for s in ses]
    pooled = math.fsum(wi * m for wi, m in zip(w, means, strict=True)) / math.fsum(w)
    q = math.fsum(wi * (m - pooled) ** 2 for wi, m in zip(w, means, strict=True))
    df = len(means) - 1
    i2 = max(0.0, (q - df) / q) if q > 0 else 0.0
    return q, df, i2


def between_ss(values: Sequence[float], labels: Sequence[str]) -> float:
    """Between-group sum of squares n_g (mean_g - grand)^2."""
    grand = mean(values)
    groups: dict[str, list[float]] = {}
    for v, g in zip(values, labels, strict=True):
        groups.setdefault(g, []).append(v)
    return math.fsum(len(g) * (mean(g) - grand) ** 2 for g in groups.values())


def label_permutation_p(values: Sequence[float], labels: Sequence[str], *, n: int = 2000, seed: int = 20261114) -> float:
    """p-value of the between-group spread against random relabelling of the same values."""
    actual = between_ss(values, labels)
    rnd = random.Random(seed)
    lab = list(labels)
    hits = 0
    for _ in range(n):
        rnd.shuffle(lab)
        if between_ss(values, lab) >= actual - 1e-12:
            hits += 1
    return (1 + hits) / (1 + n)


@dataclass(frozen=True)
class Verdict:
    mark: str  # ✓ ≈ ✗ ◯
    reason: str


def verdict(
    *,
    n_calls: int,
    n_filled: int,
    mean_r: float,
    ci_low: float,
    p_placebo: float,
    p_placebo_mech: float,
    halves_positive: bool,
    without_recovered_positive: bool,
    min_calls: int = 30,
    min_filled: int = 20,
    alpha: float = 0.05,
    alpha_holm_safe: float = 0.0125,
) -> Verdict:
    """Pre-registered verdict rules (card item 3.1)."""
    if n_calls < min_calls or n_filled < min_filled:
        return Verdict("◯", f"coverage gate: {n_calls} calls (< {min_calls}) or {n_filled} filled (< {min_filled})")
    if not mean_r > 0 or p_placebo >= alpha:
        return Verdict("✗", "mean R net <= 0 or not better than placebo (p >= 0.05)")
    checks = {
        "CI lower bound > 0": ci_low > 0,
        "placebo p < 0.0125 (Holm-safe for 4 traders)": p_placebo < alpha_holm_safe,
        "mechanical placebo p < 0.05": p_placebo_mech < alpha,
        "both time halves > 0": halves_positive,
        "> 0 without recovered rows": without_recovered_positive,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if not failed:
        return Verdict("✓", "all pre-registered conditions hold")
    return Verdict("≈", "failed: " + "; ".join(failed))


def sample_starts(bar_times: Sequence[int], lo_ms: int, hi_ms: int, k: int, rnd: random.Random) -> list[int]:
    """``k`` bar open times drawn uniformly (with replacement) from ``bar_times`` within [lo_ms, hi_ms]."""
    a, b = bisect_left(bar_times, lo_ms), bisect_right(bar_times, hi_ms)
    if b <= a:
        return []
    return [bar_times[rnd.randrange(a, b)] for _ in range(k)]
