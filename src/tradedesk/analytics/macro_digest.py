"""Pure text builders for the Telegram macro digest: weekly overview and "starts soon" reminders.

No I/O; `now` is a parameter. Input is the `events` list of `macro_calendar.build_macro_calendar` (already filtered to
high impact, text cleaned). Output is light markdown for `scripts/tg_send.py` (`#` headings, `-` bullets, `**bold**`).
Event titles and numbers are untrusted third-party text: markup characters are stripped again here, and the text is
data only. Times are shown in Europe/Kyiv. Not a forecast and not a price signal.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
_MARKUP = re.compile(r"[*`#<>\[\]_]")


def _kyiv() -> ZoneInfo:
    try:
        return ZoneInfo("Europe/Kyiv")
    except ZoneInfoNotFoundError:  # older tzdata
        return ZoneInfo("Europe/Kiev")


def _safe(value: Any, limit: int = 80) -> str:
    return " ".join(_MARKUP.sub(" ", str(value or "")).split())[:limit]


def _ts(e: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(e["ts_utc"])


def week_bounds(now: datetime) -> tuple[datetime, datetime]:
    """UTC bounds [start, end) of the Mon-Sun week (Kyiv calendar) the digest covers: the current week on a Monday,
    otherwise the coming one (Sunday evening digest -> the week that starts tomorrow)."""
    ky = _kyiv()
    local = now.astimezone(ky)
    ahead = (7 - local.weekday()) % 7
    start_local = (local + timedelta(days=ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = (start_local + timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def event_key(e: dict[str, Any]) -> str:
    """Stable id of an event for de-duplicating reminders."""
    return f"{e.get('ts_utc')}|{e.get('country')}|{e.get('title')}"


def _line(e: dict[str, Any], *, with_day: bool) -> str:
    local = _ts(e).astimezone(_kyiv())
    when = f"{_DAYS[local.weekday()]} {local:%d.%m} {local:%H:%M}" if with_day else f"{local:%H:%M}"
    approx = " (≈ час приблизний, сід)" if e.get("source") == "seed" else ""
    nums = []
    if e.get("forecast"):
        nums.append(f"прогноз {_safe(e['forecast'], 40)}")
    if e.get("previous"):
        nums.append(f"попереднє {_safe(e['previous'], 40)}")
    tail = f" — {', '.join(nums)}" if nums else ""
    return f"- {when} Київ · {_safe(e.get('country'), 8)} · **{_safe(e.get('title'))}**{tail}{approx}"


def format_week_digest(events: list[dict[str, Any]], now: datetime, notes: list[str] | None = None) -> str:
    """Weekly overview of high-impact events in the Mon-Sun week of `week_bounds(now)`, Kyiv time."""
    start, end = week_bounds(now)
    ky = _kyiv()
    week = sorted((e for e in events if start <= _ts(e) < end), key=lambda e: (e["ts_utc"], e.get("title", "")))
    first = start.astimezone(ky)
    last = first + timedelta(days=6)  # local calendar date: a DST week is 167 or 169 hours long
    out = [f"# Макро-тиждень {first:%d.%m}–{last:%d.%m}", ""]
    if not week:
        out.append("Подій високого впливу (США, рішення ECB/BoJ/BoE) у календарі немає.")
    else:
        day = None
        for e in week:
            local = _ts(e).astimezone(ky).date()
            if local != day:
                day = local
                out += ["", f"**{_DAYS[local.weekday()]} {local:%d.%m}**"]
            out.append(_line(e, with_day=False))
    out += ["", "Джерело: ForexFactory (час подій за Києвом). Дані не є сигналом чи прогнозом ціни."]
    out += [f"⚠ {_safe(n, 160)}" for n in notes or []]
    return "\n".join(out).strip() + "\n"


def due_reminders(
    events: list[dict[str, Any]], now: datetime, window_minutes: int = 60, sent: set[str] | None = None
) -> list[dict[str, Any]]:
    """Feed events starting in (now, now + window]; seed events (approximate time) and already sent ones are skipped."""
    sent = sent or set()
    horizon = now + timedelta(minutes=window_minutes)
    return [
        e
        for e in sorted(events, key=lambda e: e["ts_utc"])
        if e.get("source") == "forexfactory" and now < _ts(e) <= horizon and event_key(e) not in sent
    ]


def format_reminder(due: list[dict[str, Any]], now: datetime) -> str | None:
    """Reminder text for events from `due_reminders`; None when there is nothing to send."""
    if not due:
        return None
    out = ["# Скоро макро-подія", ""]
    for e in due:
        mins = max(1, round((_ts(e) - now).total_seconds() / 60))
        out.append(_line(e, with_day=False) + f" (через ~{mins} хв)")
    out += ["", "Високий вплив: навколо релізу можливий сплеск волатильності. Це нагадування, не сигнал."]
    return "\n".join(out) + "\n"
