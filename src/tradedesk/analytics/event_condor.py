"""Playbook rules P11 / P9 (card IDEA-20261114-options-playbook, item 4.6): trades through a US macro release.

P11: sell an iron condor at 09:00 UTC on the day of an FOMC / CPI / NFP release, on the first expiry after the
release (in practice the next-morning daily, 08:00 UTC), held to delivery. P9: buying short-dated vega (an ATM
straddle on that same expiry) through the release loses after costs. Control: weekdays with no release in
[entry - 24 h, entry + 48 h] (``macro_calendar`` -> ``high_impact_within_48h`` false and no release the day before).

Pure functions, no I/O; time is a parameter (epoch ms, UTC). Pricing, P&L and statistics are reused from
`condor_dvol`, `straddle_p5`, `calendar_p10`, `condor_25d` and `trend_spread`.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .trend_spread import DAY_MS

HOUR_MS = 3_600_000
ENTRY_HOUR = 9  # entry on the 09:00 UTC surface
MAX_DTE_DAYS = 1.5  # first expiry after the release, at most this many days after entry (base, T0 entry)
CONTROL_BEFORE_H = 24.0
CONTROL_AFTER_H = 48.0  # = macro_calendar.CHECK_WINDOW_HOURS: the flag high_impact_within_48h


def event_entry_days(events_ms: Iterable[int], *, lag_days: int = 0, entry_hour: int = ENTRY_HOUR) -> dict[int, list[int]]:
    """Entry day (00:00 UTC ms) -> releases it trades through: the release's UTC day minus ``lag_days``.

    A release at or before that day's ``entry_hour`` is not tradable before the fact and is skipped (none in the CSV:
    08:30 / 14:00 ET are 12:30-19:00 UTC). Several releases on one entry day give one entry (one trade).
    """
    out: dict[int, list[int]] = {}
    for e in sorted(set(events_ms)):
        day = e // DAY_MS * DAY_MS - lag_days * DAY_MS
        if e <= day + entry_hour * HOUR_MS:
            continue
        out.setdefault(day, []).append(e)
    return out


def expiry_after(snap: Mapping[str, Any], after_ms: int, *, max_days: float = MAX_DTE_DAYS) -> tuple[int | None, str]:
    """(expiry, reason): the earliest expiry on the surface strictly after ``after_ms`` whose time from the surface's
    ``t_ms`` is at most ``max_days``; (None, reason) if there is none."""
    later = sorted(e for e in snap["expiries"] if e > after_ms)
    if not later:
        return None, "no expiry after the event"
    e = later[0]
    if (e - snap["t_ms"]) / DAY_MS > max_days + 1e-9:
        return None, "first expiry after the event too far"
    return e, ""


def control_days(
    days: Iterable[int],
    events_ms: Sequence[int],
    *,
    weekdays: Iterable[int] = (0, 1, 2, 3, 4),
    before_h: float = CONTROL_BEFORE_H,
    after_h: float = CONTROL_AFTER_H,
    entry_hour: int = ENTRY_HOUR,
) -> list[int]:
    """Entry days (00:00 UTC ms) on ``weekdays`` (Mon = 0) with no release in [entry - before_h, entry + after_h].

    Whether a day qualifies depends only on releases within ``after_h`` hours of its own entry, so releases later
    than that never change it.
    """
    ev = sorted(events_ms)
    wd = set(weekdays)
    out = []
    for d in sorted(set(days)):
        if datetime.fromtimestamp(d / 1000, UTC).weekday() not in wd:
            continue
        t = d + entry_hour * HOUR_MS
        lo, hi = t - int(before_h * HOUR_MS), t + int(after_h * HOUR_MS)
        i = bisect_left(ev, lo)
        if i < len(ev) and ev[i] <= hi:
            continue
        out.append(d)
    return out


def playbook_labels(h1: str, h3: str) -> dict[str, str]:
    """Playbook labels registered in card 4.6 from the mechanical verdicts (✓ / ≈ / ✗ / ◯) of H1 and H3.

    P11 (sell the condor through the release): ✓ only for ✓ H1; ✗ H1 -> "✗" (to "do not"); else "◯".
    P9 (do not buy short vega through the release): ✗ H3 (the bought straddle loses after costs) -> "✓ risk rule";
    ✓ H3 -> "refuted"; else "unchanged".
    """
    p11 = {"✓": "✓", "✗": "✗"}.get(h1, "◯")
    p9 = {"✗": "✓ risk rule", "✓": "refuted"}.get(h3, "unchanged")
    return {"P11": p11, "P9": p9}
