"""Pure helpers for the assistant snapshot (docs/ideas/IDEA-20261018-assistant.md): which expiries the brief and the options
builder use, and the brief's view of the paper recommendation state."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from .instruments import parse_expiry_token

WEEK_MIN_DAYS = 2.0  # the weekly expiry shown in the brief must be at least this far away (skip the one expiring today)
WEEK_MAX_DAYS = 10.0  # ... and no further than this: otherwise there is no "weekly" Friday listed
MONTH_MIN_DAYS = 25.0  # the "monthly" expiry is the last-Friday one at least this far away
# the "nearest" expiry of the options builder: rule 8 bans buys under 24 h to delivery (rules_check.BUY_BAN_HOURS), plus
# 1 h so the chain of a 10-minute snapshot is still outside that zone for a while after it is written
NEAR_MIN_DAYS = 25.0 / 24.0
# the paper recommendation's state (collector job assistant_paper, one decision a day after 09:00 UTC): a decision older
# than this is no longer «today's» (the 08:00 Kyiv brief normally sees yesterday's, ~20 h old; 36 h = one missed day)
PAPER_MAX_AGE_HOURS = 36.0
PAPER_STATES = ("actual", "changed", "no_longer_actual")
PAPER_ACTIONS = ("open", "hold", "none")
PAPER_TRIGGERS = ("chain", "regime", "event")
PAPER_PICK_MAX_LEN = 40


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


def _int_or_none(x: Any) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def paper_rec_view(state: Any, now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    """The brief's view of the paper recommendation: ``(data, None)``, or ``(None, reason)`` when there is nothing usable.

    ``state`` is the parsed state file of the collector job ``assistant_paper`` (``collector.assistant_paper``: one
    decision a day after 09:00 UTC; ``state`` is ``actual`` / ``changed`` / ``no_longer_actual``). Only known keys and
    values are kept; ``pick`` stays an untrusted string (capped, escaped at render time). A decision older than
    PAPER_MAX_AGE_HOURS at ``now`` is not shown as current. The recommendation is a hypothesis: the method showed no
    edge (H1 rejected 18.10), so the data carries ``label: hypothesis``.
    """
    if not isinstance(state, dict):
        return None, "no paper state file yet (collector job assistant_paper off or not run)"
    st = state.get("state")
    if st not in PAPER_STATES:
        return None, f"unknown paper state {str(st)[:PAPER_PICK_MAX_LEN]!r}"
    try:
        asof = datetime.strptime(str(state.get("asof_utc")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None, "paper state without a readable asof_utc"
    age = (now - asof).total_seconds() / 3600
    if age > PAPER_MAX_AGE_HOURS:
        return None, f"last paper decision {age:.0f} h old (over {PAPER_MAX_AGE_HOURS:.0f} h): job assistant_paper seems stopped"
    pick = state.get("pick")
    action, trigger = state.get("action"), state.get("trigger")
    return {
        "label": "hypothesis",
        "state": st,
        "action": action if action in PAPER_ACTIONS else None,
        "trigger": trigger if trigger in PAPER_TRIGGERS else None,
        "pick": pick[:PAPER_PICK_MAX_LEN] if isinstance(pick, str) and pick else None,
        "asof_utc": asof.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_hours": round(age, 2),
        "trade_id": _int_or_none(state.get("trade_id")),
        "standing_trade": _int_or_none(state.get("standing_trade")),
    }, None
