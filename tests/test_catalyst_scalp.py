import math
from datetime import UTC, date, datetime, timedelta

import pytest

from tradedesk.analytics.catalyst_scalp import (
    BAR_S,
    DirectionEvent,
    direction_event,
    et_time_utc,
    gate_passes,
    hit_stats,
    is_trading_day,
    reversion_direction,
    us_open_utc,
    wilson_ci,
)


def _bars(day: date, price) -> dict[int, tuple[float, float]]:
    """One UTC day of 5m bars; open = price(t), close = price(t + 5m)."""
    t0 = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    return {t: (price(t), price(t + BAR_S)) for t in range(t0, t0 + 86400, BAR_S)}


def test_us_open_dst():
    assert us_open_utc(date(2026, 7, 15)) == datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    assert us_open_utc(date(2026, 1, 15)) == datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
    # US DST 2026 starts Sunday 8 March: Friday 6 March winter, Monday 9 March summer
    assert us_open_utc(date(2026, 3, 6)).hour == 14
    assert us_open_utc(date(2026, 3, 9)).hour == 13
    assert et_time_utc(date(2026, 7, 15), 8, 30) == datetime(2026, 7, 15, 12, 30, tzinfo=UTC)


def test_trading_day():
    hol = [date(2026, 7, 3)]
    assert is_trading_day(date(2026, 7, 2), hol)
    assert not is_trading_day(date(2026, 7, 3), hol)
    assert not is_trading_day(date(2026, 7, 4), hol)  # Saturday


def test_reversion_direction():
    assert reversion_direction(-0.01, 0.01) == 1
    assert reversion_direction(0.0123, 0.01) == -1
    assert reversion_direction(0.005, 0.01) == 0
    assert reversion_direction(-0.0099, 0.01) == 0


def test_direction_event_analytic():
    day = date(2026, 7, 15)  # T = 13:30 UTC, entry 09:30, exit 15:30
    entry = int(datetime(2026, 7, 15, 9, 30, tzinfo=UTC).timestamp())

    def price(t):  # 100 at 00:00 UTC drifting down, 98 from 09:30, 99 from the 15:30 exit
        if t < entry:
            return 100.0 * math.exp(-0.01 * (t - entry + 34200) / 34200)
        return 98.0 if t < entry + 6 * 3600 else 99.0

    ev = direction_event(_bars(day, price), us_open_utc(day), 4, 2, 0.01)
    assert ev is not None
    assert ev.entry == datetime(2026, 7, 15, 9, 30, tzinfo=UTC)
    assert ev.exit == datetime(2026, 7, 15, 15, 30, tzinfo=UTC)
    # morning: close of bar 09:25 (= price at 09:30 = 98) over the open of the 00:00 bar (= 100)
    assert ev.morning == pytest.approx(math.log(0.98))
    assert ev.ret == pytest.approx(math.log(99 / 98))
    assert ev.direction == 1 and ev.hit


def test_no_lookahead():
    """Changing anything at or after the entry (incl. the close of the bar opening at entry) leaves the signal
    unchanged; changing bars after the exit open leaves the whole event unchanged."""
    day = date(2026, 1, 15)  # T = 14:30 UTC, entry 10:30, exit 16:30
    t_entry = int(datetime(2026, 1, 15, 10, 30, tzinfo=UTC).timestamp())
    t_exit = t_entry + 6 * 3600
    base = _bars(day, lambda t: 100.0 + (t % 7919) / 1000)
    ev = direction_event(base, us_open_utc(day), 4, 2, 0.0)
    assert ev is not None

    after_entry = dict(base)
    for t, (o, c) in base.items():
        if t == t_entry:
            after_entry[t] = (o, c * 1.5)  # close of the entry bar = 5 minutes into the future
        elif t > t_entry:
            after_entry[t] = (o * 1.3, c * 1.3)
    ev2 = direction_event(after_entry, us_open_utc(day), 4, 2, 0.0)
    assert ev2 is not None
    assert ev2.morning == ev.morning and ev2.direction == ev.direction

    after_exit = dict(base)
    for t, (o, c) in base.items():
        if t == t_exit:
            after_exit[t] = (o, c * 2)
        elif t > t_exit:
            after_exit[t] = (o * 2, c * 2)
    assert direction_event(after_exit, us_open_utc(day), 4, 2, 0.0) == ev


def test_missing_bar_and_grid():
    day = date(2026, 7, 15)
    bars = _bars(day, lambda t: 100.0)
    del bars[int(datetime(2026, 7, 15, 15, 30, tzinfo=UTC).timestamp())]
    assert direction_event(bars, us_open_utc(day), 4, 2, 0.01) is None
    assert direction_event(_bars(day, lambda t: 100.0), us_open_utc(day), 4.01, 2, 0.01) is None


def test_wilson_reference():
    lo, hi = wilson_ci(55, 100)
    assert lo == pytest.approx(0.4524, abs=1e-3)
    assert hi == pytest.approx(0.6439, abs=1e-3)
    assert all(math.isnan(v) for v in wilson_ci(0, 0))


def _ev(direction: int, ret: float) -> DirectionEvent:
    t = datetime(2026, 1, 1, tzinfo=UTC)
    return DirectionEvent(t, t, t + timedelta(hours=1), 0.0, ret, direction, direction != 0 and direction * ret > 0)


def test_hit_stats_and_gate():
    evs = [_ev(1, 0.01)] * 60 + [_ev(-1, 0.01)] * 40 + [_ev(0, 0.05)] * 7
    s = hit_stats(evs)
    assert (s.n, s.hits) == (100, 60)
    assert s.rate == pytest.approx(0.6)
    assert s.mean_signed == pytest.approx(0.6 * 0.01 - 0.4 * 0.01)
    ok, why = gate_passes(s, s, s)
    assert ok and not why
    weak = hit_stats([_ev(1, 0.01)] * 54 + [_ev(1, -0.01)] * 46)
    ok, why = gate_passes(s, weak, s)
    assert not ok and len(why) == 2  # OOS rate < 55 % and OOS lower bound <= 50 %
    ok, why = gate_passes(s, hit_stats([]), s)
    assert not ok
