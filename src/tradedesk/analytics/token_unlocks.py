"""Scheduled token unlocks and the short-into-unlock trade (research for IDEA-20261015-token-unlock-short-drift).

Pure functions, no I/O. Time is a UTC day index (``unix_seconds // 86400``); token amounts are in tokens; fractions and
returns are decimals (0.02 = 2 %).

The schedule is DefiLlama's documented emission schedule (``documentedData``): per allocation section, the cumulative
number of unlocked tokens per day. A day's scheduled unlock is the sum over sections of the increment on that day. The
schedule is a forecast published in advance (vesting terms), so the unlock size on day ``T`` is known at ``T - 10``;
the denominator (circulating supply) is taken at the entry day, so nothing after the entry is read.

Returns reuse :mod:`tradedesk.analytics.holders_revenue` (beta, hedged return, funding sum, clustered errors).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

DAY_S = 86_400

# DefiLlama category of allocations that are unlocked but explicitly not circulating (treasuries, reserves).
NONCIRCULATING = "noncirculating"


@dataclass(frozen=True)
class Section:
    """One allocation of a token schedule: label, DefiLlama category and cumulative unlocked tokens by day."""

    label: str
    category: str
    points: tuple[tuple[int, float], ...]  # (day, cumulative unlocked), sorted by day

    def at(self, day: int) -> float:
        """Cumulative unlocked on ``day`` (last point on or before it; 0 before the first point)."""
        lo, hi = 0, len(self.points)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.points[mid][0] <= day:
                lo = mid + 1
            else:
                hi = mid
        return self.points[lo - 1][1] if lo > 0 else 0.0


def parse_sections(emission: Mapping[str, object]) -> list[Section]:
    """Sections of a DefiLlama ``emissions/<slug>`` document (``documentedData.data``) with their categories.

    Labels are matched to ``categories`` case-insensitively; a label in no category is ``"uncategorized"``. Points of
    one day are collapsed to the last one; rows without a numeric ``unlocked`` are skipped."""
    doc = emission.get("documentedData")
    rows = doc.get("data") if isinstance(doc, Mapping) else None
    if not isinstance(rows, list):
        return []
    cats: dict[str, str] = {}
    raw_cats = emission.get("categories")
    if isinstance(raw_cats, Mapping):
        for cat, labels in raw_cats.items():
            for lab in labels if isinstance(labels, list) else []:
                cats[str(lab).strip().lower()] = str(cat)
    out = []
    for r in rows:
        if not isinstance(r, Mapping):
            continue
        label = str(r.get("label") or "")
        pts: dict[int, float] = {}
        for p in r.get("data") or []:
            if not isinstance(p, Mapping):
                continue
            ts, u = p.get("timestamp"), p.get("unlocked")
            if not isinstance(ts, (int, float)) or not isinstance(u, (int, float)):
                continue
            pts[int(ts) // DAY_S] = float(u)
        if pts:
            out.append(Section(label, cats.get(label.strip().lower(), "uncategorized"), tuple(sorted(pts.items()))))
    return out


def circulating(sections: Iterable[Section], day: int, *, exclude: Iterable[str] = (NONCIRCULATING,)) -> float:
    """Scheduled circulating supply on ``day``: unlocked tokens of all sections outside the excluded categories."""
    ex = set(exclude)
    return sum(s.at(day) for s in sections if s.category not in ex)


def _dense(s: Section, lo: int, hi: int) -> list[float]:
    """``[s.at(d) for d in range(lo, hi + 1)]`` in one pass over the points."""
    out = []
    j, cur = 0, 0.0
    pts = s.points
    while j < len(pts) and pts[j][0] < lo:
        cur = pts[j][1]
        j += 1
    for d in range(lo, hi + 1):
        while j < len(pts) and pts[j][0] <= d:
            cur = pts[j][1]
            j += 1
        out.append(cur)
    return out


@dataclass(frozen=True)
class DayUnlock:
    day: int
    amount: float  # tokens unlocked on ``day`` over included sections (cliff + linear increments)
    cliff: float  # part of ``amount`` from cliff jumps (see ``unlocks_by_day``)
    insiders: float  # part of ``amount`` in categories ``insiders`` / ``privateSale``
    labels: tuple[str, ...] = field(default=())


def unlocks_by_day(
    sections: Sequence[Section],
    *,
    exclude: Iterable[str] = (NONCIRCULATING,),
    cliff_ratio: float = 10.0,
    first_day: int | None = None,
    last_day: int | None = None,
) -> dict[int, DayUnlock]:
    """Scheduled unlocks per day: the increment of each included section's cumulative series on that day.

    A section's increment on day ``d`` is ``at(d) - at(d - 1)``; it is a *cliff* when it exceeds ``cliff_ratio`` times
    the larger of the section's increments on ``d - 1`` and ``d - 2`` (linear vesting moves every day by about the
    same amount; only days up to ``d`` are read). Negative increments (schedule corrections, burns) are ignored."""
    ex = set(exclude)
    acc: dict[int, list[float]] = {}
    labs: dict[int, list[str]] = {}
    for s in sections:
        if s.category in ex or not s.points:
            continue
        lo = s.points[0][0] if first_day is None else max(first_day, s.points[0][0])
        hi = s.points[-1][0] if last_day is None else min(last_day, s.points[-1][0])
        if hi < lo:
            continue
        base = lo - 3
        dense = _dense(s, base, hi)  # dense[i] = at(base + i)
        for d in range(lo, hi + 1):
            i = d - base
            inc = dense[i] - dense[i - 1]
            if inc <= 0:
                continue
            nb = max(dense[i - 1] - dense[i - 2], dense[i - 2] - dense[i - 3], 0.0)
            is_cliff = inc > cliff_ratio * nb
            a = acc.setdefault(d, [0.0, 0.0, 0.0])
            a[0] += inc
            if is_cliff:
                a[1] += inc
                labs.setdefault(d, []).append(s.label)
            if s.category in ("insiders", "privateSale"):
                a[2] += inc
    return {d: DayUnlock(d, a[0], a[1], a[2], tuple(labs.get(d, ()))) for d, a in sorted(acc.items())}


@dataclass(frozen=True)
class UnlockEvent:
    day: int  # T: the unlock day
    entry: int  # T + entry_offset (position opened at this day's close)
    exit: int  # T + exit_offset (closed at this day's close)
    amount: float  # tokens unlocked on T
    circ: float  # scheduled circulating supply on the entry day
    frac: float  # amount / circ
    cliff_frac: float  # cliff part / circ
    insider_frac: float  # insiders + privateSale part / circ


def select_events(
    unlocks: Mapping[int, DayUnlock],
    circ_at: Callable[[int], float],
    *,
    min_frac: float,
    first_day: int,
    last_day: int,
    entry_offset: int = -10,
    exit_offset: int = 2,
    tradable: Callable[[int, int], bool] | None = None,
    use_cliff: bool = False,
) -> list[UnlockEvent]:
    """Unlock days ``T`` in ``[first_day, last_day]`` whose size is ``>= min_frac`` of the circulating supply on the
    entry day ``T + entry_offset`` (``use_cliff``: the cliff part only).

    ``tradable(entry, T)`` drops days on which the position could not be opened. An event whose entry falls inside
    the holding period ``[entry, exit]`` of the previous kept event of the same token is skipped (no overlapping
    trades on one token). Reads ``unlocks`` only for the day ``T`` itself and ``circ_at`` only at the entry day."""
    out: list[UnlockEvent] = []
    busy_until = -(10**9)
    for t in sorted(unlocks):
        if t < first_day or t > last_day:
            continue
        u = unlocks[t]
        entry, exit_ = t + entry_offset, t + exit_offset
        if entry <= busy_until:
            continue
        c = circ_at(entry)
        if c <= 0:
            continue
        size = u.cliff if use_cliff else u.amount
        if size / c < min_frac:
            continue
        if tradable is not None and not tradable(entry, t):
            continue
        out.append(UnlockEvent(t, entry, exit_, u.amount, c, u.amount / c, u.cliff / c, u.insiders / c))
        busy_until = exit_
    return out


def placebo_days(
    candidates: Iterable[int],
    blocked: Iterable[int],
    *,
    entry_offset: int = -10,
    exit_offset: int = 2,
    step: int = 13,
) -> list[int]:
    """Placebo unlock days: a grid over ``candidates`` (sorted; at least ``step`` days apart) whose window
    ``[d + entry_offset, d + exit_offset]`` does not overlap the window of any ``blocked`` day (all scheduled unlocks
    of the token, of any size)."""
    bl = sorted(set(blocked))
    out: list[int] = []
    last = -(10**9)
    for d in sorted(set(candidates)):
        if d - last < step:
            continue
        lo, hi = d + entry_offset, d + exit_offset
        if any(b + entry_offset <= hi and lo <= b + exit_offset for b in bl):
            continue
        out.append(d)
        last = d
    return out


def stopped_short_return(
    highs: Mapping[int, float], closes: Mapping[int, float], entry: int, exit_: int, stop: float
) -> tuple[float, int] | None:
    """Short from the entry close with a stop ``stop`` above it (0.3 = +30 %): ``(token return, exit day)``.

    The stop fills at the stop level on the first day whose high reaches it (gaps through the stop are filled at
    the day's open-insensitive stop price, so the loss is understated on gap days); otherwise the position closes
    at ``exit_``. The return is the token's price change (a short earns its negative)."""
    p0 = closes.get(entry)
    if not p0 or p0 <= 0:
        return None
    lvl = p0 * (1.0 + stop)
    for d in range(entry + 1, exit_ + 1):
        h = highs.get(d)
        if h is not None and h >= lvl:
            return stop, d
    pe = closes.get(exit_)
    if not pe:
        return None
    return pe / p0 - 1.0, exit_


def slippage_for_volume(median_daily_usd: float | None) -> float:
    """Pre-registered slippage per side by the token perp's median daily USD volume over the 30 days to entry."""
    if median_daily_usd is None or median_daily_usd < 5e6:
        return 0.0015
    if median_daily_usd < 5e7:
        return 0.0010
    return 0.0005
