from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from tradedesk.analytics.macro_calendar import build_macro_calendar
from tradedesk.analytics.macro_digest import (
    due_reminders,
    event_key,
    format_reminder,
    format_week_digest,
    week_bounds,
)

FIX = Path(__file__).parent / "fixtures" / "forexfactory_thisweek_2026-10-18.json"
SUNDAY = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)  # 20:00 Kyiv (EEST, UTC+3)


def _rows() -> list[dict]:
    return [
        {
            "title": "CPI m/m",
            "country": "USD",
            "date": "2026-10-21T08:30:00-04:00",
            "impact": "High",
            "forecast": "0.3%",
            "previous": "0.4%",
        },
        {
            "title": "Core PCE **x** <b>",
            "country": "USD",
            "date": "2026-10-23T08:30:00-04:00",
            "impact": "High",
            "forecast": "",
            "previous": "",
        },
        {"title": "Low thing", "country": "USD", "date": "2026-10-21T10:00:00-04:00", "impact": "Low", "forecast": "", "previous": ""},
    ]


def test_week_bounds_sunday_evening_is_next_kyiv_week():
    start, end = week_bounds(SUNDAY)
    assert start == datetime(2026, 10, 18, 21, 0, tzinfo=UTC)  # Mon 19.10 00:00 Kyiv
    assert end == datetime(2026, 10, 25, 22, 0, tzinfo=UTC)  # Mon 26.10 00:00 Kyiv, after the DST change (EET)
    assert week_bounds(datetime(2026, 10, 19, 5, 0, tzinfo=UTC))[0] == start  # Monday: the current week


def test_week_digest_lists_events_in_kyiv_time_without_markup():
    ev = build_macro_calendar(_rows(), [], SUNDAY, 10)["events"]
    text = format_week_digest(ev, SUNDAY)
    assert "# Макро-тиждень 19.10–25.10" in text
    assert "Ср 21.10" in text and "15:30 Київ" in text  # 08:30 EDT = 15:30 Kyiv
    assert "прогноз 0.3%" in text and "попереднє 0.4%" in text
    assert "Low thing" not in text
    assert "<b>" not in text and "**x**" not in text


def test_week_digest_title_in_spring_dst_week():
    text = format_week_digest([], datetime(2026, 3, 22, 18, 0, tzinfo=UTC))
    assert "# Макро-тиждень 23.03–29.03" in text


def test_week_digest_real_capture_has_only_seed_event_or_empty():
    cap = json.loads(FIX.read_text())
    seed = [{"ts": int(datetime(2026, 10, 28, 18, 0, tzinfo=UTC).timestamp()), "kind": "fomc", "label": "FOMC decision", "impact": "high"}]
    ev = build_macro_calendar(cap["thisweek"], seed, datetime(2026, 10, 18, 6, 30, tzinfo=UTC), 14)["events"]
    text = format_week_digest(ev, SUNDAY)  # week 19-25 Oct: the capture has no high-impact USD/CB row
    assert "немає" in text
    text2 = format_week_digest(ev, datetime(2026, 10, 25, 17, 0, tzinfo=UTC), notes=["note"])  # week 26.10-01.11
    assert "FOMC decision" in text2 and "приблизний" in text2 and "⚠ note" in text2


def test_reminders_window_dedupe_and_seed_skipped():
    ev = build_macro_calendar(_rows(), [], SUNDAY, 10)["events"]
    now = datetime(2026, 10, 21, 11, 45, tzinfo=UTC)  # 14:45 Kyiv, CPI at 12:30 UTC
    due = due_reminders(ev, now, 60)
    assert [e["title"] for e in due] == ["CPI m/m"]
    txt = format_reminder(due, now)
    assert txt and "через ~45 хв" in txt
    assert due_reminders(ev, now, 60, {event_key(due[0])}) == []
    assert due_reminders(ev, now, 30) == []
    assert format_reminder([], now) is None
    seed_ev = [{**ev[0], "source": "seed"}]
    assert due_reminders(seed_ev, now, 60) == []


def test_empty_week_names_the_next_big_event():
    now = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)  # Sunday evening: the digest covers 19.10-25.10
    fomc = {"ts_utc": "2026-10-28T18:00:00+00:00", "country": "USD", "title": "FOMC Statement", "source": "seed"}
    text = format_week_digest([fomc], now)
    assert "немає" in text and "Наступна велика подія:" in text
    assert "ср 28.10 20:00 київ" in text.lower() and "FOMC Statement" in text
    assert "Наступна" not in format_week_digest([], now)
