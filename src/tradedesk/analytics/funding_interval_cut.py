"""Binance funding-interval changes (8h -> 4h -> 1h cuts and back) as event triggers; pure functions, no I/O.

Idea card: docs/ideas/IDEA-20261025-funding-interval-cut-stress.md. Funding rows are ``[calc_time_ms, interval_h, rate]``
from the data.binance.vision ``fundingRate`` archive (rate as a decimal per interval, 0.0001 = 0.01 %).

Causality: an interval change is *known* only at the first settlement whose gap to the previous settlement equals the
new interval (the settlement times themselves reveal the new schedule). The archive's interval column is used to find
the candidate, never as the time stamp, so a decision at ``t_known`` uses rows with ``calc_time <= t_known`` only.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Hashable, Sequence
from dataclasses import dataclass

HOUR_MS = 3_600_000
DAY_MS = 86_400_000


@dataclass(frozen=True)
class IntervalChange:
    """One change of the funding interval of a symbol.

    ``t_label``: first row carrying the new interval; ``t_known``: first row (from ``t_label`` on) whose gap to the
    previous row equals the new interval; ``trigger_rate``: rate of the last row with the old interval (the
    settlement that precedes the change, usually at the cap/floor)."""

    t_label: int
    t_known: int
    h_prev: float
    h_new: float
    trigger_rate: float

    @property
    def kind(self) -> str:
        return "cut" if self.h_new < self.h_prev else "lengthen"


def detect_interval_changes(rows: Sequence[Sequence[float]], *, tol_h: float = 0.25, max_lag_rows: int = 2) -> list[IntervalChange]:
    """All interval changes in time-sorted funding ``rows``.

    A change at row ``i`` (interval differs from row ``i-1``) is confirmed at the first row ``j`` in ``i .. i+max_lag_rows``
    with ``|gap(j-1, j) - h_new| <= tol_h`` hours and the interval still ``h_new`` on rows ``i..j``; unconfirmed changes
    are dropped. The event for ``j`` depends on ``rows[: j + 1]`` only."""
    out: list[IntervalChange] = []
    n = len(rows)
    for i in range(1, n):
        hp, h = float(rows[i - 1][1]), float(rows[i][1])
        if h <= 0 or hp <= 0 or h == hp:
            continue
        for j in range(i, min(n, i + max_lag_rows + 1)):
            if float(rows[j][1]) != h:
                break
            gap_h = (float(rows[j][0]) - float(rows[j - 1][0])) / HOUR_MS
            if abs(gap_h - h) <= tol_h:
                out.append(IntervalChange(int(rows[i][0]), int(rows[j][0]), hp, h, float(rows[i - 1][2])))
                break
    return out


def dedup_changes(
    changes: Sequence[IntervalChange], *, first_row_ms: int | None, quiet_ms: int = 14 * DAY_MS, min_age_ms: int = 14 * DAY_MS
) -> list[IntervalChange]:
    """Keep changes with no earlier change of the symbol in the ``quiet_ms`` before ``t_known`` and, when the symbol's
    listing time ``first_row_ms`` is known (``None`` = listed before the archive window), at least ``min_age_ms`` after it.

    A cut is blocked by any earlier change (cut or lengthening); a lengthening only by an earlier lengthening (every
    normalisation follows a cut, so blocking it by the cut would leave no H3b events)."""
    srt = sorted(changes, key=lambda c: c.t_known)
    kept: list[IntervalChange] = []
    for k, c in enumerate(srt):
        if first_row_ms is not None and c.t_known - first_row_ms < min_age_ms:
            continue
        prev = [p for p in srt[:k] if c.kind == "cut" or p.kind == "lengthen"]
        if any(c.t_known - p.t_known < quiet_ms for p in prev):
            continue
        kept.append(c)
    return kept


def classify(c: IntervalChange, *, stress_rate: float = 0.001) -> str:
    """Hypothesis group: ``H1`` (stress cut, negative rate), ``H2`` (stress cut, positive), ``H3a`` (cut, |rate| below
    ``stress_rate``), ``H3b`` (lengthening)."""
    if c.kind == "lengthen":
        return "H3b"
    if abs(c.trigger_rate) < stress_rate:
        return "H3a"
    return "H1" if c.trigger_rate < 0 else "H2"


def side_for(c: IntervalChange) -> int:
    """+1 (long) when the trigger rate is negative (crowded shorts), -1 otherwise."""
    return 1 if c.trigger_rate < 0 else -1


def bar_index_containing(open_times: Sequence[int], t_ms: int, bar_ms: int) -> int | None:
    """Index of the bar ``[open, open + bar_ms)`` containing ``t_ms`` in sorted ``open_times``; ``None`` if absent."""
    k = bisect.bisect_right(open_times, t_ms) - 1
    if k < 0 or not (open_times[k] <= t_ms < open_times[k] + bar_ms):
        return None
    return k


def window_returns(closes: Sequence[float], k: int, *, back: Sequence[int], fwd: Sequence[int]) -> dict[str, float] | None:
    """Simple returns around bar ``k`` (entry at ``closes[k]``): ``b<n> = C[k]/C[k-n] - 1``, ``f<n> = C[k+n]/C[k] - 1``.

    ``None`` if any needed bar is missing or a price is not positive (callers pass contiguous bars)."""
    need = [k - b for b in back] + [k + f for f in fwd] + [k]
    if min(need) < 0 or max(need) >= len(closes) or any(closes[i] <= 0 for i in need):
        return None
    out = {f"b{b}": closes[k] / closes[k - b] - 1.0 for b in back}
    out.update({f"f{f}": closes[k + f] / closes[k] - 1.0 for f in fwd})
    return out


def funding_pnl(rows: Sequence[Sequence[float]], t_from: int, t_to: int, side: int) -> float:
    """Funding received by a position of ``side`` (+1 long) on unit notional for settlements in ``(t_from, t_to]``.

    A positive rate means longs pay shorts, so the long receives ``-rate``."""
    times = [int(r[0]) for r in rows]
    lo, hi = bisect.bisect_right(times, t_from), bisect.bisect_right(times, t_to)
    return -side * math.fsum(float(r[2]) for r in rows[lo:hi])


@dataclass(frozen=True)
class Candidate:
    key: Hashable
    b1: float
    b3: float
    fwd: float


def match_controls(b1: float, b3: float, pool: Sequence[Candidate], *, k: int = 5, max_db3: float = 0.05) -> list[Candidate]:
    """``k`` candidates nearest to the target's trailing returns (``|Δb3| + |Δb1|``), only those with ``|Δb3| <= max_db3``.

    Ties are broken by ``str(key)`` so the choice is deterministic."""
    ok = [c for c in pool if abs(c.b3 - b3) <= max_db3]
    ok.sort(key=lambda c: (abs(c.b3 - b3) + abs(c.b1 - b1), str(c.key)))
    return ok[:k]


def iso_week(t_ms: int) -> str:
    """ISO year-week label of a UTC millisecond time (cluster key)."""
    import datetime as _dt

    y, w, _ = _dt.datetime.fromtimestamp(t_ms / 1000, tz=_dt.UTC).isocalendar()
    return f"{y}-W{w:02d}"


def sd(xs: Sequence[float]) -> float | None:
    """Sample standard deviation (n-1); ``None`` below two values."""
    n = len(xs)
    if n < 2:
        return None
    m = math.fsum(xs) / n
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (n - 1))


def design_effect(se_cluster: float | None, sd_iid: float | None, n: int) -> float | None:
    """``(se_cluster / (sd / sqrt(n)))**2``: variance inflation of the mean from clustering; ``n / deff`` = effective n."""
    if se_cluster is None or not sd_iid or n <= 0:
        return None
    return (se_cluster / (sd_iid / math.sqrt(n))) ** 2
