"""Tests for analytics.event_condor (card IDEA-20261114-options-playbook, item 4.6)."""

from __future__ import annotations

from datetime import UTC, datetime

from tradedesk.analytics.event_condor import (
    DAY_MS,
    HOUR_MS,
    control_days,
    event_entry_days,
    expiry_after,
    playbook_labels,
)


def ms(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> int:
    return int(datetime(y, m, d, h, mi, tzinfo=UTC).timestamp() * 1000)


CPI = ms(2024, 3, 12, 12, 30)  # Tuesday
FOMC = ms(2024, 3, 20, 18, 0)  # Wednesday


def test_event_entry_days_same_day_and_lag() -> None:
    assert event_entry_days([CPI, FOMC]) == {ms(2024, 3, 12): [CPI], ms(2024, 3, 20): [FOMC]}
    assert event_entry_days([CPI], lag_days=1) == {ms(2024, 3, 11): [CPI]}


def test_event_entry_days_skips_release_before_entry_and_merges_one_day() -> None:
    early = ms(2024, 3, 12, 8, 0)  # before the 09:00 entry of its own day
    assert event_entry_days([early]) == {}
    late = ms(2024, 3, 12, 18, 0)
    assert event_entry_days([late, CPI, CPI]) == {ms(2024, 3, 12): [CPI, late]}


def snap(t_ms: int, expiries: list[int]) -> dict:
    return {"t_ms": t_ms, "expiries": {e: {"strikes": {}} for e in expiries}}


def test_expiry_after_picks_first_expiry_after_release() -> None:
    t = ms(2024, 3, 12, 9)
    same_morning = ms(2024, 3, 12, 8)  # already expired relative to the release
    nxt = ms(2024, 3, 13, 8)
    s = snap(t, [same_morning, nxt, ms(2024, 3, 14, 8)])
    assert expiry_after(s, CPI) == (nxt, "")
    assert abs((nxt - t) / DAY_MS - 23 / 24) < 1e-12


def test_expiry_after_limits_dte() -> None:
    t = ms(2024, 3, 12, 9)
    s = snap(t, [ms(2024, 3, 15, 8)])  # 2.96 d away
    assert expiry_after(s, CPI) == (None, "first expiry after the event too far")
    assert expiry_after(s, CPI, max_days=3.0)[0] == ms(2024, 3, 15, 8)
    assert expiry_after(snap(t, [ms(2024, 3, 12, 8)]), CPI) == (None, "no expiry after the event")


def test_control_days_excludes_window_and_weekends() -> None:
    days = [ms(2024, 3, d) for d in range(8, 19)]  # Fri 8 .. Mon 18
    got = control_days(days, [CPI])
    # CPI Tue 12 12:30: excluded entries with 09:00 in [CPI - 48 h, CPI + 24 h] -> Sun 10 .. Wed 13
    # (Sat / Sun are weekends anyway); Mon 11 (event +27.5 h) and Tue 12 (+3.5 h) and Wed 13 (-20.5 h) out.
    assert got == [ms(2024, 3, 8), ms(2024, 3, 14), ms(2024, 3, 15), ms(2024, 3, 18)]
    assert control_days(days, [CPI], weekdays=(1, 2, 3)) == [ms(2024, 3, 14)]


def test_control_days_boundaries_inclusive() -> None:
    d = ms(2024, 3, 14)  # Thursday
    t = d + 9 * HOUR_MS
    assert control_days([d], [t + 48 * HOUR_MS]) == []
    assert control_days([d], [t + 48 * HOUR_MS + 1]) == [d]
    assert control_days([d], [t - 24 * HOUR_MS]) == []
    assert control_days([d], [t - 24 * HOUR_MS - 1]) == [d]


def test_no_lookahead_later_events_do_not_change_earlier_results() -> None:
    days = [ms(2024, 3, 1) + k * DAY_MS for k in range(40)]
    base_events = [CPI, FOMC]
    later = [ms(2024, 4, 3, 12, 30), ms(2024, 4, 10, 18, 0)]
    cut = ms(2024, 3, 25)  # every later release is > 48 h after the 09:00 entry of any day before the cut
    a = [d for d in control_days(days, base_events) if d < cut]
    b = [d for d in control_days(days, base_events + later) if d < cut]
    assert a == b
    ea = {d: v for d, v in event_entry_days(base_events).items() if d < cut}
    eb = {d: v for d, v in event_entry_days(base_events + later).items() if d < cut}
    assert ea == eb


def test_playbook_labels() -> None:
    assert playbook_labels("✓", "✗") == {"P11": "✓", "P9": "✓ risk rule"}
    assert playbook_labels("✗", "✓") == {"P11": "✗", "P9": "refuted"}
    assert playbook_labels("≈", "◯") == {"P11": "◯", "P9": "unchanged"}
    assert playbook_labels("◯", "≈") == {"P11": "◯", "P9": "unchanged"}
