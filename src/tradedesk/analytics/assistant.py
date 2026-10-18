"""Pure helpers for the assistant snapshot (docs/ideas/IDEA-20261018-assistant.md): which expiries the brief uses."""

from __future__ import annotations

from datetime import date, timedelta

from .instruments import parse_expiry_token

WEEK_MIN_DAYS = 2.0  # the weekly expiry shown in the brief must be at least this far away (skip the one expiring today)
WEEK_MAX_DAYS = 10.0  # ... and no further than this: otherwise there is no "weekly" Friday listed
MONTH_MIN_DAYS = 25.0  # the "monthly" expiry is the last-Friday one at least this far away


def is_last_friday(d: date) -> bool:
    """True when `d` is the last Friday of its month (Deribit monthly expiries)."""
    return d.weekday() == 4 and (d + timedelta(days=7)).month != d.month


def pick_expiries(expiries: list[dict]) -> tuple[str | None, str | None]:
    """(week, month) expiry tokens from ``[{"expiry": "27NOV26", "days": 39.7}, ...]``.

    week = the nearest Friday expiry WEEK_MIN_DAYS..WEEK_MAX_DAYS away (Deribit also lists dailies, which are not
    "the week"), else None; month = the nearest last-Friday expiry at least MONTH_MIN_DAYS away, falling back to the
    nearest expiry that far away when no last-Friday one is listed.
    """
    rows = sorted(
        (x for x in expiries if isinstance(x.get("days"), (int, float)) and isinstance(x.get("expiry"), str)),
        key=lambda x: x["days"],
    )
    week = None
    for x in rows:
        if not WEEK_MIN_DAYS <= x["days"] <= WEEK_MAX_DAYS:
            continue
        dt = parse_expiry_token(x["expiry"])
        if dt is not None and dt.weekday() == 4:
            week = x["expiry"]
            break
    far = [x for x in rows if x["days"] >= MONTH_MIN_DAYS]
    month = None
    for x in far:
        dt = parse_expiry_token(x["expiry"])
        if dt is not None and is_last_friday(dt.date()):
            month = x["expiry"]
            break
    if month is None and far:
        month = far[0]["expiry"]
    return week, month
