"""Direct tests for analytics/calendar.py; reference values computed by hand from the 2026 calendar.

2026 facts used: 2026-11-05 is a Thursday; Fridays in Nov 2026 are 6, 13, 20, 27; in Dec 4, 11, 18, 25;
in Sep 4, 11, 18, 25; in Mar 6, 13, 20, 27; Jul 31 2026 is a Friday (last day of the month).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tradedesk.analytics.calendar import _last_friday, classify_expiry, filter_macro, funding_schedule

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("year", "month", "day"),
    [(2026, 11, 27), (2026, 12, 25), (2026, 9, 25), (2026, 3, 27), (2026, 7, 31)],
)
def test_last_friday(year, month, day):
    assert _last_friday(year, month) == day


@pytest.mark.parametrize(
    ("exp", "kind"),
    [
        (datetime(2026, 12, 25, 8, tzinfo=UTC), "quarterly"),
        (datetime(2026, 9, 25, 8, tzinfo=UTC), "quarterly"),
        (datetime(2026, 3, 27, 8, tzinfo=UTC), "quarterly"),
        (datetime(2026, 11, 27, 8, tzinfo=UTC), "monthly"),
        (datetime(2026, 7, 31, 8, tzinfo=UTC), "monthly"),  # last day of month is the last Friday
        (datetime(2026, 11, 13, 8, tzinfo=UTC), "weekly"),
        (datetime(2026, 12, 18, 8, tzinfo=UTC), "weekly"),  # Dec Friday, but not the last one
        (datetime(2026, 11, 5, 8, tzinfo=UTC), "daily"),  # Thursday
        (datetime(2026, 11, 27, 9, tzinfo=UTC), "daily"),  # Friday, not 08:00
        (datetime(2026, 12, 25, 16, tzinfo=UTC), "daily"),
    ],
)
def test_classify_expiry(exp, kind):
    assert classify_expiry(exp, NOW) == kind


def test_funding_schedule_mixed_venues():
    now = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
    out = funding_schedule(now, 10)  # window ends 13:30
    # 8h venues pay once (08:00), hyperliquid hourly 04:00..13:00 -> 3 + 10 entries
    assert len(out) == 13
    assert out[0] == {"venue": "hyperliquid", "ts": "2026-11-05T04:00:00+00:00", "interval_hours": 1}
    at_8 = [r for r in out if r["ts"] == "2026-11-05T08:00:00+00:00"]
    assert [r["venue"] for r in at_8] == ["binanceusdm", "bybit", "hyperliquid", "okx"]
    assert {r["interval_hours"] for r in at_8 if r["venue"] != "hyperliquid"} == {8}
    assert out[-1]["ts"] == "2026-11-05T13:00:00+00:00"
    assert [r["ts"] for r in out] == sorted(r["ts"] for r in out)


def test_funding_schedule_now_on_boundary_is_exclusive_end_inclusive():
    now = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
    out = funding_schedule(now, 8)  # end 16:00
    eight_h = [r for r in out if r["interval_hours"] == 8]
    assert {r["ts"] for r in eight_h} == {"2026-11-05T16:00:00+00:00"}  # 08:00 == now is skipped
    hl = [r["ts"] for r in out if r["venue"] == "hyperliquid"]
    assert hl[0] == "2026-11-05T09:00:00+00:00"
    assert hl[-1] == "2026-11-05T16:00:00+00:00"
    assert len(hl) == 8


def test_funding_schedule_hourly_venue_capped_at_24h():
    now = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
    out = funding_schedule(now, 48)
    assert sum(r["venue"] == "hyperliquid" for r in out) == 24
    # 8h payouts at 08:00, 16:00, 24:00, ... 48:00 = 6 slots x 3 venues
    assert sum(r["interval_hours"] == 8 for r in out) == 18
    assert len(out) == 42


def test_funding_schedule_zero_horizon_is_empty():
    assert funding_schedule(datetime(2026, 11, 5, 3, 30, tzinfo=UTC), 0) == []


def test_filter_macro_window_bounds_and_fields():
    events = [
        {"date": "2026-11-05", "time_utc": "11:59", "name": "past"},
        {"date": "2026-11-05", "time_utc": "12:00", "name": "at-now"},
        {"date": "2026-11-06", "time_utc": "12:30", "name": "inside"},
        {"date": "2026-11-07", "time_utc": "12:00", "name": "at-end"},
        {"date": "2026-11-07", "time_utc": "12:01", "name": "after"},
    ]
    out = filter_macro(events, NOW, 2)
    assert [e["name"] for e in out] == ["at-now", "inside", "at-end"]
    assert [e["days_ahead"] for e in out] == [0.0, 1.02, 2.0]  # 24.5h / 24 = 1.0208
    assert out[1]["ts"] == "2026-11-06T12:30:00+00:00"
    assert out[1]["time_utc"] == "12:30"  # original keys preserved


def test_filter_macro_defaults_time_skips_bad_rows_and_sorts():
    events = [
        {"date": "2026-11-06", "time_utc": "18:00", "name": "late"},
        {"date": "2026-11-06", "name": "no-time"},  # defaults to 00:00 -> 12h ahead
        {"date": "2026-11-06", "time_utc": None, "name": "none-time"},
        {"date": "not-a-date", "time_utc": "10:00", "name": "bad-date"},
        {"date": "2026-11-06", "time_utc": "25:00", "name": "bad-time"},
        {"time_utc": "10:00", "name": "no-date"},
    ]
    out = filter_macro(events, NOW, 3)
    assert [e["name"] for e in out] == ["no-time", "none-time", "late"]
    assert out[0]["days_ahead"] == 0.5
    assert out[2]["days_ahead"] == 1.25


def test_filter_macro_empty_and_zero_horizon():
    assert filter_macro([], NOW, 7) == []
    ev = [{"date": "2026-11-05", "time_utc": "12:00"}, {"date": "2026-11-05", "time_utc": "12:01"}]
    assert [e["time_utc"] for e in filter_macro(ev, NOW, 0)] == ["12:00"]
