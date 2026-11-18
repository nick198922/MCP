"""Places of the demo's open-positions cap held by resting (unfilled) limit entries.

Accounting for IDEA-20261030-demo-setups-depth, «лімітки S17 і S28 у ліміті 25 позицій» (audit 17.11): the guard
counts a ``pending`` entry as an open position, and an S17 limit on 1d may rest up to 90 days. Pure functions, no I/O;
times are unix ms and every cut-off is a parameter, so a result as of ``t`` never depends on rows after ``t``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

DAY_MS = 86_400_000
HOUR_MS = 3_600_000

__all__ = [
    "DAY_MS",
    "HOUR_MS",
    "Interval",
    "Evaluation",
    "pending_interval",
    "parse_open_count",
    "count_active",
    "slot_days",
    "freed_upper",
    "admit_freed",
    "fill_by_age",
]

_OPEN_RE = re.compile(r"^(\d+) open positions = max")


@dataclass(frozen=True)
class Interval:
    """A resting entry: unfilled from ``start`` (inclusive) to ``end`` (exclusive); ``filled`` if it ended by a fill."""

    start: int
    end: int
    filled: bool


@dataclass(frozen=True)
class Evaluation:
    """A signal stopped by the open-positions cap: decision time, ``n_open`` from the guard text, demo rank keys."""

    ts: int
    link_id: str
    n_open: int
    score: int
    rr: float


def pending_interval(
    sent_ms: int | None, signal_ms: int | None, opened_ms: int | None, closed_ms: int | None, cutoff_ms: int
) -> Interval | None:
    """The unfilled interval of one demo row as of ``cutoff_ms``.

    Start: the first ``sent`` audit time, else the ``signal`` audit time; ``None`` if neither is before the cut-off.
    End: the fill (``opened_ms``, a partial fill included), else ``closed_ms`` of a row that never filled (missed or
    cancelled), else the cut-off; any time at or after the cut-off has not happened yet."""
    start = sent_ms if sent_ms is not None else signal_ms
    if start is None or start >= cutoff_ms:
        return None
    if opened_ms is not None and opened_ms < cutoff_ms:
        return Interval(start, max(start, opened_ms), True)
    if opened_ms is None and closed_ms is not None and closed_ms < cutoff_ms:
        return Interval(start, max(start, closed_ms), False)
    return Interval(start, cutoff_ms, False)


def parse_open_count(violations: Sequence[str]) -> int | None:
    """N of the guard's «N open positions = max» violation, ``None`` if the list has none."""
    for v in violations:
        m = _OPEN_RE.match(str(v))
        if m:
            return int(m.group(1))
    return None


def count_active(intervals: Sequence[Interval], t: int) -> int:
    """Intervals with ``start <= t < end``."""
    return sum(1 for iv in intervals if iv.start <= t < iv.end)


def slot_days(intervals: Sequence[Interval], t0: int, t1: int) -> float:
    """Place-days: total overlap of the intervals with ``[t0, t1)`` in days."""
    tot = 0
    for iv in intervals:
        lo, hi = max(iv.start, t0), min(iv.end, t1)
        if hi > lo:
            tot += hi - lo
    return tot / DAY_MS


def freed_upper(evals: Sequence[Evaluation], intervals: Sequence[Interval], cap: int) -> list[str]:
    """Variant A: every evaluation whose open count without the resting entries, ``n_open - P(ts)``, is below ``cap``."""
    return [e.link_id for e in sorted(evals, key=lambda e: e.ts) if e.n_open - count_active(intervals, e.ts) < cap]


def admit_freed(
    evals: Sequence[Evaluation], intervals: Sequence[Interval], cap: int, hold_ms: int | None, bucket_ms: int = 300_000
) -> list[str]:
    """Variants B / C / D: evaluations that would have entered without the resting entries.

    Evaluations are grouped into cycles by ``ts // bucket_ms``; in a cycle the free places are
    ``cap - (n_open - P) - held``, where ``n_open`` is the largest count of the cycle, ``P`` the resting entries active
    at the cycle's first time and ``held`` the admitted hypothetical trades still holding a place (each holds it for
    ``hold_ms``; ``None`` = none hold, variant B). Admission in the demo's order: score down, then reward/risk down.
    Processed forward in time, so admissions before ``t`` do not depend on evaluations or interval ends after ``t``."""
    cycles: dict[int, list[Evaluation]] = {}
    for e in evals:
        cycles.setdefault(e.ts // bucket_ms, []).append(e)
    held: list[int] = []  # release times of admitted hypothetical trades
    out: list[str] = []
    for key in sorted(cycles):
        group = cycles[key]
        t = min(e.ts for e in group)
        n_open = max(e.n_open for e in group)
        held = [r for r in held if r > t]
        free = cap - (n_open - count_active(intervals, t)) - len(held)
        if free <= 0:
            continue
        for e in sorted(group, key=lambda e: (-e.score, -e.rr, e.ts, e.link_id))[:free]:
            out.append(e.link_id)
            if hold_ms is not None:
                held.append(e.ts + hold_ms)
    return out


def fill_by_age(intervals: Sequence[Interval], ages_ms: Sequence[int], cutoff_ms: int) -> list[dict[str, int]]:
    """Fill by age of resting entries: for each age ``a``, filled within ``a``, known unfilled at ``a`` (ended unfilled,
    filled later than ``a``, or still resting at the cut-off with age >= ``a``) and censored (still resting, younger)."""
    out = []
    for a in ages_ms:
        filled = unfilled = censored = 0
        for iv in intervals:
            if iv.filled and iv.end - iv.start <= a:
                filled += 1
            elif iv.filled or iv.end < cutoff_ms or cutoff_ms - iv.start >= a:
                unfilled += 1
            else:
                censored += 1
        out.append({"age_ms": int(a), "filled": filled, "unfilled": unfilled, "censored": censored})
    return out
