"""Feasibility checks for free historical token-supply sources (coverage, cross-source gaps, point-in-time revisions).

Pure functions, no I/O. Supplies are token counts; gaps are decimal fractions (0.02 = 2 %). Dates are ``datetime.date``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

_MULT_PREFIX = re.compile(r"^(?:1000+|1M)(?=[A-Z])")


def normalize_perp_base(base: str) -> str:
    """Perp base -> underlying token symbol: drops contract multipliers (``1000PEPE`` -> ``PEPE``,
    ``1000000BABYDOGE`` -> ``BABYDOGE``, ``1MBABYDOGE`` -> ``BABYDOGE``) but keeps real tickers (``1INCH``)."""
    return _MULT_PREFIX.sub("", base.strip().upper())


def quarter_sample_dates(first_year: int, last: date) -> list[date]:
    """First Sunday of every calendar quarter from ``first_year``-Q1 up to and including ``last``."""
    out: list[date] = []
    y, m = first_year, 1
    while True:
        d = date(y, m, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)  # weekday(): Monday=0 ... Sunday=6
        if d > last:
            return out
        out.append(d)
        m += 3
        if m > 12:
            y, m = y + 1, 1


def coverage_fraction(window_dates: Sequence[date], dates_with_supply: Iterable[date]) -> float | None:
    """Share of the dates in the token's window on which the source has a positive supply; None if the window is empty."""
    if not window_dates:
        return None
    have = set(dates_with_supply)
    return sum(1 for d in window_dates if d in have) / len(window_dates)


def rel_gap(value: float | None, reference: float | None) -> float | None:
    """|value / reference - 1|; None if either side is missing or not positive."""
    if value is None or reference is None:
        return None
    if not (value > 0 and reference > 0) or math.isinf(value) or math.isinf(reference):
        return None
    return abs(value / reference - 1.0)


@dataclass(frozen=True)
class GapSummary:
    n: int
    median: float | None
    p90: float | None
    share_gt_5pct: float | None
    share_gt_10pct: float | None


def _quantile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile of a sorted, non-empty sequence."""
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def summarize_gaps(gaps: Iterable[float | None]) -> GapSummary:
    """Median / p90 and the shares above 5 % and 10 % of the non-missing gaps."""
    xs = sorted(g for g in gaps if g is not None)
    if not xs:
        return GapSummary(0, None, None, None, None)
    n = len(xs)
    return GapSummary(
        n=n,
        median=_quantile(xs, 0.5),
        p90=_quantile(xs, 0.9),
        share_gt_5pct=sum(1 for g in xs if g > 0.05) / n,
        share_gt_10pct=sum(1 for g in xs if g > 0.10) / n,
    )


def resolve_symbol(candidates: Sequence[Mapping[str, float | None]], ref_price: float | None) -> int | None:
    """Index of the candidate (same ticker, different coins) that is the perp's underlying.

    With a reference perp price: among candidates priced within a factor of 2 of it, the largest market cap (wrapped
    or bridged clones trade at the same price as the real coin, so the closest price alone can pick a clone); ties and
    candidates without a market cap fall back to the closest price. Without a reference: the largest market cap.
    Candidates carry ``price`` and ``market_cap``; None if nothing qualifies."""
    if not candidates:
        return None
    if ref_price is not None and ref_price > 0:
        best, best_key = None, None
        for i, c in enumerate(candidates):
            p = c.get("price")
            if p is None or p <= 0:
                continue
            dist = abs(math.log(p / ref_price))
            if dist > math.log(2.0):
                continue
            key = (c.get("market_cap") or 0.0, -dist)
            if best_key is None or key > best_key:
                best, best_key = i, key
        return best
    ranked = [(c.get("market_cap") or 0.0, i) for i, c in enumerate(candidates)]
    cap, idx = max(ranked)
    return idx if cap > 0 else None


def asof(series: Mapping[date, float], t: date) -> float | None:
    """Last value dated on or before ``t`` (a snapshot series read as it was known at ``t``).

    Not used by the stage-0 feasibility script (``scripts/verify_free_supply_history.py``): it is kept for a future
    supply-history loader. Its lookahead test guards only this helper, not the stage-0 numbers, and it cannot catch
    a source that itself serves later values under an earlier date (see the 2023-03-19 CMC anomaly in the report).
    """
    best: date | None = None
    for d in series:
        if d <= t and (best is None or d > best):
            best = d
    return None if best is None else series[best]


def pit_verdict(summary: GapSummary, *, min_pairs: int = 20, max_median: float = 0.01, max_share_gt_5: float = 0.10) -> str:
    """'pass' / 'fail' / 'unverifiable' for a revision test (today's value vs an archived copy taken at the time)."""
    if summary.n < min_pairs or summary.median is None or summary.share_gt_5pct is None:
        return "unverifiable"
    return "pass" if summary.median <= max_median and summary.share_gt_5pct <= max_share_gt_5 else "fail"
