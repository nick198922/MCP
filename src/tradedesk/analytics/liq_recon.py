"""Reconcile Coinalyze liquidation history with our own WebSocket liquidation rows (card IDEA-20261017-doubletop-to-ours,
item 4c; runner scripts/verify_coinalyze_liq.py, report docs/verification/2026-11-28-coinalyze-liq-check.md).

Pure functions, no I/O. Units: coin quantities (base asset), USD, unix seconds; shares and ratios are decimals.

Side convention: our ``liquidations.side`` is the TAKER side of the force order (``sell`` = a long was liquidated,
``buy`` = a short was liquidated; Bybit's position side is flipped by the parser), Coinalyze's ``l`` / ``s`` are long
and short liquidations, so ``sell`` -> ``long`` and ``buy`` -> ``short`` (:data:`OUR_SIDE`).

Comparison rules (fixed 2026-11-28 before any Coinalyze number was compared with our rows):

* A bucket is compared only where our stream was connected: a day needs all 24 hourly journal rows, at least
  :data:`MIN_DAY_SHARE` of its seconds connected and no hour below :data:`MIN_HOUR_SHARE`; an hour needs at least
  :data:`MIN_FULL_HOUR_S` connected seconds.
* Our quantity is compared with ``coverage x Coinalyze`` (``coverage`` = connected share of the bucket; 1 for a fully
  connected hour), i.e. a stream that misses 5 % of the day is expected to hold 95 % of the venue's flow.
* A bucket is informative when ours or the expectation exceeds ``eps`` (a few lot sizes); it agrees when
  ``|ours - expected| <= max(eps, band x expected)``.
* Marks (:func:`stream_verdict`): ◯ below ``min_n`` informative buckets; ✓ when the pooled coverage-adjusted ratio
  sum(ours) / sum(expected) lies in [:data:`RATIO_FULL_LO`, :data:`RATIO_FULL_HI`] and at least ``share_full`` of the
  informative buckets agree. Otherwise, for a complete stream (Bybit): ≈ when the pooled ratio lies in
  [:data:`RATIO_NEAR_LO`, :data:`RATIO_NEAR_HI`] and the bucket correlation is at least :data:`CORR_NEAR`, else ✗.
  For a lower-bound stream (Binance, at most one push per symbol per second): ≈ when ours exceeds the expectation
  (beyond the band) on at most :data:`ABOVE_MAX_SHARE` of the informative buckets and the correlation is at least
  :data:`CORR_LOWER_BOUND` (Coinalyze holds at least our lower bound and moves with it), else ✗.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

DAY = 86_400
HOUR = 3_600
OUR_SIDE = {"sell": "long", "buy": "short"}  # taker side of the force order -> liquidated position (Coinalyze l / s)
SIDES = ("long", "short")

MIN_DAY_SHARE = 0.90  # connected share of a day
MIN_HOUR_SHARE = 0.60  # no hour of a qualifying day below this connected share
MIN_FULL_HOUR_S = 3_540.0  # a "fully connected" hour: at most 60 s without a connection
DAILY_BAND, HOURLY_BAND = 0.10, 0.05  # relative tolerance of one bucket
DAILY_SHARE_FULL, HOURLY_SHARE_FULL = 0.80, 0.90  # share of informative buckets that must agree for ✓
DAILY_MIN_N, HOURLY_MIN_N = 10, 30  # informative buckets needed for a mark
RATIO_FULL_LO, RATIO_FULL_HI = 0.95, 1.05
RATIO_NEAR_LO, RATIO_NEAR_HI = 0.80, 1.25
CORR_NEAR = 0.90
CORR_LOWER_BOUND = 0.80
ABOVE_MAX_SHARE = 0.10
EPS_COINS = {"BTC": 0.005, "ETH": 0.05, "SOL": 0.5}  # a few lot sizes of each base asset


def bucket_events(events: Iterable[tuple[int, str, float, float]], step: int, until: int | None = None) -> dict[int, dict[str, float]]:
    """``(ts, our_side, qty_coins, notional_usd)`` rows -> ``{bucket_start: {long, short, long_usd, short_usd, n}}``
    in ``step``-second UTC buckets. Rows at or after ``until`` (when given) and rows with an unknown side are ignored."""
    out: dict[int, dict[str, float]] = {}
    for ts, side, qty, usd in events:
        pos = OUR_SIDE.get(side)
        if pos is None or (until is not None and ts >= until):
            continue
        b = out.setdefault(ts // step * step, {"long": 0.0, "short": 0.0, "long_usd": 0.0, "short_usd": 0.0, "n": 0})
        b[pos] += qty
        b[f"{pos}_usd"] += usd
        b["n"] += 1
    return out


def hour_coverage(rows: Iterable[tuple[int, float]]) -> dict[int, float]:
    """Journal rows ``(hour_ts, connected_s)`` -> connected seconds per UTC hour (repeated hours summed, capped at 3600)."""
    out: dict[int, float] = {}
    for h, s in rows:
        k = h // HOUR * HOUR
        out[k] = min(float(HOUR), out.get(k, 0.0) + float(s))
    return out


def qualifying_days(
    hours: Mapping[int, float],
    first_day: int,
    last_day: int,
    min_day_share: float = MIN_DAY_SHARE,
    min_hour_share: float = MIN_HOUR_SHARE,
) -> dict[int, float]:
    """UTC days in [first_day, last_day] (day starts) whose 24 hours are all journaled, with at least ``min_day_share``
    of the day connected and no hour below ``min_hour_share`` -> connected share of the day."""
    out: dict[int, float] = {}
    for d in range(first_day // DAY * DAY, last_day + 1, DAY):
        hs = [hours.get(d + i * HOUR) for i in range(24)]
        if any(h is None for h in hs):
            continue
        vals = [float(h) for h in hs if h is not None]
        share = sum(vals) / DAY
        if share >= min_day_share and min(vals) >= min_hour_share * HOUR:
            out[d] = share
    return out


def qualifying_hours(hours: Mapping[int, float], end: int, min_connected_s: float = MIN_FULL_HOUR_S) -> dict[int, float]:
    """Hours that end at or before ``end`` with at least ``min_connected_s`` connected -> connected share (<= 1)."""
    return {h: s / HOUR for h, s in hours.items() if h + HOUR <= end and s >= min_connected_s}


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)


def compare_side(
    ours: Mapping[int, float], ref: Mapping[int, float], coverage: Mapping[int, float], eps: float, band: float
) -> dict[str, Any]:
    """Our quantity per bucket against ``coverage x ref`` over the buckets present in both ``coverage`` and ``ref``
    (a bucket we have no row for is our zero). Returns n, informative, agree, above (ours beyond the band over the
    expectation), sums, ``ratio`` = sum(ours) / sum(ref), ``ratio_cov`` = sum(ours) / sum(expected), the median
    per-bucket ratio over buckets with expectation > eps, the Pearson correlation of ours vs expected, first / last."""
    keys = sorted(k for k in coverage if k in ref)
    o = [float(ours.get(k, 0.0)) for k in keys]
    e = [float(ref[k]) * float(coverage[k]) for k in keys]
    r = [float(ref[k]) for k in keys]
    informative = agree = above = 0
    for a, x in zip(o, e, strict=True):
        if max(a, x) <= eps:
            continue
        informative += 1
        tol = max(eps, band * x)
        agree += abs(a - x) <= tol
        above += a - x > tol
    ratios = [a / x for a, x in zip(o, e, strict=True) if x > eps]
    so, se, sr = sum(o), sum(e), sum(r)
    return {
        "n": len(keys),
        "informative": informative,
        "agree": agree,
        "above": above,
        "share": agree / informative if informative else None,
        "above_share": above / informative if informative else None,
        "sum_ours": so,
        "sum_ref": sr,
        "sum_expected": se,
        "ratio": so / sr if sr > 0 else None,
        "ratio_cov": so / se if se > 0 else None,
        "median_ratio": statistics.median(ratios) if ratios else None,
        "corr": _pearson(o, e),
        "first": keys[0] if keys else None,
        "last": keys[-1] if keys else None,
    }


def stream_verdict(stats: Mapping[str, Any], *, lower_bound: bool, min_n: int, share_full: float) -> str:
    """Mark of one venue x symbol x side comparison (rules in the module docstring)."""
    inf, share, ratio, corr = stats.get("informative") or 0, stats.get("share"), stats.get("ratio_cov"), stats.get("corr")
    if inf < min_n or share is None or ratio is None:
        return "◯"
    if RATIO_FULL_LO <= ratio <= RATIO_FULL_HI and share >= share_full:
        return "✓"
    if corr is None:
        return "✗"
    if lower_bound:
        above = stats.get("above_share") or 0.0
        return "≈" if above <= ABOVE_MAX_SHARE and corr >= CORR_LOWER_BOUND else "✗"
    return "≈" if RATIO_NEAR_LO <= ratio <= RATIO_NEAR_HI and corr >= CORR_NEAR else "✗"


def side_test(
    ours: Mapping[int, Mapping[str, float]], ref: Mapping[int, Mapping[str, float]], keys: Iterable[int], eps: float
) -> dict[str, int]:
    """Is our long/short mapping the one that fits Coinalyze? Per bucket where the reference sides differ by more than
    ``eps``, compare |ours_long - ref_long| + |ours_short - ref_short| (direct) with the crossed sum (swapped).
    Returns the counts of informative buckets and of those where the direct / swapped assignment is closer."""
    n = direct = swapped = 0
    for k in keys:
        if k not in ref:
            continue
        rl, rs = float(ref[k].get("long", 0.0)), float(ref[k].get("short", 0.0))
        if abs(rl - rs) <= eps:
            continue
        b = ours.get(k) or {}
        ol, os_ = float(b.get("long", 0.0)), float(b.get("short", 0.0))
        d, s = abs(ol - rl) + abs(os_ - rs), abs(ol - rs) + abs(os_ - rl)
        n += 1
        direct += d < s
        swapped += s < d
    return {"informative": n, "direct_closer": direct, "swapped_closer": swapped}


def long_share_corr(
    ours: Mapping[int, Mapping[str, float]], ref: Mapping[int, Mapping[str, float]], keys: Iterable[int], eps: float
) -> dict[str, Any]:
    """Pearson correlation of the long share long / (long + short), ours against the reference, over buckets in
    ``keys`` where both totals exceed ``eps``. Unlike :func:`side_test` it stays informative when our stream holds only
    a fraction of the reference (a lower bound): a swapped side shows as a strongly negative correlation. Added
    2026-11-28 after the first run (exploratory)."""
    xs: list[float] = []
    ys: list[float] = []
    for k in keys:
        b, r = ours.get(k) or {}, ref.get(k)
        if r is None:
            continue
        ot = float(b.get("long", 0.0)) + float(b.get("short", 0.0))
        rt = float(r.get("long", 0.0)) + float(r.get("short", 0.0))
        if ot > eps and rt > eps:
            xs.append(float(b.get("long", 0.0)) / ot)
            ys.append(float(r.get("long", 0.0)) / rt)
    return {"n": len(xs), "corr": _pearson(xs, ys)}


def throttle_per_second(events: Iterable[tuple[int, str, float, float]]) -> list[tuple[int, str, float, float]]:
    """Keep only the LAST row of each whole second (input order), as a stream that pushes at most one liquidation
    snapshot per symbol per second would (Binance ``!forceOrder@arr``). Applied to a complete stream (Bybit) it shows
    how much such a throttle loses. Added 2026-11-28 after the first run (exploratory)."""
    last: dict[int, tuple[int, str, float, float]] = {}
    for e in events:
        last[int(e[0])] = e
    return [last[t] for t in sorted(last)]


def sum_hours_by_day(hourly: Mapping[int, float], days: Iterable[int]) -> dict[int, float]:
    """Sum of 24 hourly values for each day that has all 24 hours in ``hourly`` (days with a missing hour are left out)."""
    out: dict[int, float] = {}
    for d in days:
        hs = [hourly.get(d + i * HOUR) for i in range(24)]
        if all(h is not None for h in hs):
            out[d] = sum(float(h) for h in hs if h is not None)
    return out


def implied_prices(usd: Mapping[int, float], coins: Mapping[int, float], min_coins: float) -> dict[int, float]:
    """USD / coins per bucket present in both, where coins exceed ``min_coins`` (the price Coinalyze converted at)."""
    return {k: float(usd[k]) / float(coins[k]) for k in usd if k in coins and float(coins[k]) > min_coins}
