"""Spot direction gate for the catalyst option scalp (IDEA-20261022-catalyst-option-scalp).

Before any option data is loaded, the registered test asks whether a direction signal over the US-open window is
right often enough (>= 55 %) to make buying a short-dated call/put worth pricing at all.

Pure functions: no I/O, time passed in. ``bars`` maps a 5-minute bar's OPEN time (epoch seconds, Binance key) to
``(open, close)``. The price at time ``t`` is the open of the bar that opens at ``t``; the signal only uses bars that
have closed by the entry time (no 5-minute lookahead: the close of the bar opening at the entry is never used).
Returns are natural-log decimals (0.01 = 1 %).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
BAR_S = 300

Bars = Mapping[int, tuple[float, float]]


def us_open_utc(d: date) -> datetime:
    """NYSE open (09:30 America/New_York) of calendar date ``d`` in UTC, DST-aware (13:30 summer, 14:30 winter)."""
    return datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY).astimezone(UTC)


def et_time_utc(d: date, hour: int, minute: int) -> datetime:
    """Wall-clock time ``hour:minute`` America/New_York on date ``d``, in UTC."""
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=NY).astimezone(UTC)


def is_trading_day(d: date, holidays: Iterable[date]) -> bool:
    """Mon-Fri and not a full-day exchange holiday."""
    return d.weekday() < 5 and d not in set(holidays)


def reversion_direction(morning: float, threshold: float) -> int:
    """Primary signal S1: fade the morning move. +1 (long / call) if ``morning <= -threshold``, -1 (short / put) if
    ``morning >= +threshold``, else 0 (no trade). ``threshold`` is a log-return decimal (0.01 = 1 %)."""
    if morning <= -threshold:
        return 1
    if morning >= threshold:
        return -1
    return 0


@dataclass(frozen=True)
class DirectionEvent:
    event: datetime  # the catalyst time T (UTC)
    entry: datetime  # T - h
    exit: datetime  # T + k
    morning: float  # ln(close of last bar closed by entry / open of the 00:00 UTC bar of entry's day)
    ret: float  # ln(open at exit / open at entry)
    direction: int  # +1 long, -1 short, 0 no trade
    hit: bool  # direction != 0 and direction * ret > 0 (a zero return is a miss)


def direction_event(bars: Bars, event: datetime, h_hours: float, k_hours: float, threshold: float) -> DirectionEvent | None:
    """Morning-reversion event around catalyst time ``event``: entry ``h_hours`` before, exit ``k_hours`` after.

    Uses only: the open of the 00:00 UTC bar of the entry day, the close of the bar ``[entry - 5m, entry)``, the open
    of the bar at ``entry`` and the open of the bar at ``exit``. Returns ``None`` if any of them is missing, if the
    entry/exit is not on the 5-minute grid, or if the morning window is empty (entry at or before 00:05 UTC).
    """
    entry = event - timedelta(hours=h_hours)
    exit_ = event + timedelta(hours=k_hours)
    e, x = int(entry.timestamp()), int(exit_.timestamp())
    if e % BAR_S or x % BAR_S:
        return None
    midnight = int(datetime(entry.year, entry.month, entry.day, tzinfo=UTC).timestamp())
    if e - BAR_S <= midnight:
        return None
    b0, bs, be, bx = bars.get(midnight), bars.get(e - BAR_S), bars.get(e), bars.get(x)
    if b0 is None or bs is None or be is None or bx is None:
        return None
    morning = math.log(bs[1] / b0[0])
    ret = math.log(bx[0] / be[0])
    direction = reversion_direction(morning, threshold)
    return DirectionEvent(event, entry, exit_, morning, ret, direction, direction != 0 and direction * ret > 0)


def wilson_ci(hits: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score interval for a proportion; ``(nan, nan)`` for n = 0."""
    if n <= 0:
        return (math.nan, math.nan)
    p = hits / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (centre - half, centre + half)


@dataclass(frozen=True)
class HitStats:
    n: int
    hits: int
    rate: float
    lo: float
    hi: float
    mean_signed: float  # mean of direction * ret (log decimal)
    mean_lo: float
    mean_hi: float


def hit_stats(events: Sequence[DirectionEvent]) -> HitStats:
    """Hit rate with Wilson 95 % CI and mean signed log-return with a normal 95 % CI, over trades (direction != 0)."""
    tr = [ev for ev in events if ev.direction != 0]
    n = len(tr)
    hits = sum(1 for ev in tr if ev.hit)
    lo, hi = wilson_ci(hits, n)
    signed = [ev.direction * ev.ret for ev in tr]
    if n >= 2:
        m = statistics.fmean(signed)
        se = statistics.stdev(signed) / math.sqrt(n)
        mlo, mhi = m - 1.959964 * se, m + 1.959964 * se
    else:
        m = signed[0] if signed else math.nan
        mlo = mhi = math.nan
    return HitStats(n, hits, hits / n if n else math.nan, lo, hi, m, mlo, mhi)


def gate_passes(is_: HitStats, oos: HitStats, whole: HitStats, min_rate: float = 0.55) -> tuple[bool, list[str]]:
    """Registered gate: hit rate >= ``min_rate`` on IS, OOS and the whole sample, and the OOS Wilson lower bound > 50 %.

    Returns ``(passed, reasons_for_failure)``.
    """
    reasons: list[str] = []
    for name, s in (("IS", is_), ("OOS", oos), ("all", whole)):
        if not s.n or not s.rate >= min_rate:
            reasons.append(f"{name} hit {s.rate:.3f} < {min_rate:.2f} (n={s.n})")
    if not oos.n or not oos.lo > 0.5:
        reasons.append(f"OOS Wilson lower bound {oos.lo:.3f} <= 0.50")
    return (not reasons, reasons)
