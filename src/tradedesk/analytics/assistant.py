"""Pure helpers for the assistant snapshot (docs/ideas/IDEA-20261018-assistant.md): which expiries the brief and the options builder use."""

from __future__ import annotations

from datetime import date, timedelta

from .instruments import parse_expiry_token

WEEK_MIN_DAYS = 2.0  # the weekly expiry shown in the brief must be at least this far away (skip the one expiring today)
WEEK_MAX_DAYS = 10.0  # ... and no further than this: otherwise there is no "weekly" Friday listed
MONTH_MIN_DAYS = 25.0  # the "monthly" expiry is the last-Friday one at least this far away
# the "nearest" expiry of the options builder: rule 8 bans buys under 24 h to delivery (rules_check.BUY_BAN_HOURS), plus
# 1 h so the chain of a 10-minute snapshot is still outside that zone for a while after it is written
NEAR_MIN_DAYS = 25.0 / 24.0


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


def pick_near_expiry(expiries: list[dict]) -> str | None:
    """The nearest expiry token at least NEAR_MIN_DAYS away (any listed expiry: daily, weekly or monthly), else None.

    The options builder checks rule 8's 1-3 day zone on it; an expiry nearer than that is never a buy under the rule.
    """
    rows = [x for x in expiries if isinstance(x.get("days"), (int, float)) and isinstance(x.get("expiry"), str)]
    rows = [x for x in rows if x["days"] >= NEAR_MIN_DAYS and parse_expiry_token(x["expiry"]) is not None]
    return min(rows, key=lambda x: x["days"])["expiry"] if rows else None
