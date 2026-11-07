"""Direct tests for eleven analytics helpers that had no mention in tests/ (BACKLOG 6.9, card item 203).

They feed forwards, journals and reports: the "JSON number" rule of the Deribit option chain and of the perps / market
tools (`options.json_num`), the ET calendar of the Polymarket touch markets and of the PM gap forward
(`pm_touch.et_midnight`, `et_time`, `et_date`, `title_direction`), the Pendle expiry (`pendle.parse_expiry`), the
Coinalyze liquidation timestamps (`liquidations_agg.parse_ts`), the point-in-time universe month of the liquidation
flush report (`liquidation_flush.month_start`), the S14 trend gate of the setup journal (`course_s14_ema.trend_ok`)
and two sieve specs (`elder_impulse_green.macd_hist`, `turn_of_month.is_second_to_last_day`). References are real
samples from tests/fixtures/, calendar facts (UTC offsets of US DST, epoch seconds via `calendar.timegm`) or
hand-computed in the comments; no output of the code under test is reused as an expected value.
"""

from __future__ import annotations

import calendar
import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from tradedesk.analytics.course_s14_ema import Bars, Ind, Params, indicators, trend_ok
from tradedesk.analytics.liquidation_flush import month_start
from tradedesk.analytics.liquidations_agg import ROW_ERRORS, TS_MAX, TS_MIN, parse_ts
from tradedesk.analytics.options import json_num
from tradedesk.analytics.pendle import parse_expiry
from tradedesk.analytics.pm_touch import et_date, et_midnight, et_time, title_direction
from tradedesk.analytics.sieve_specs.elder_impulse_green import macd_hist
from tradedesk.analytics.sieve_specs.turn_of_month import is_second_to_last_day

FIX = Path(__file__).parent / "fixtures"
HOUR = 3_600_000
DAY = 24 * HOUR


def _utc(*ymdhm: int) -> int:
    """Unix seconds of a UTC wall-clock time (calendar.timegm: no time-zone database involved)."""
    return calendar.timegm((*ymdhm, *([0] * (6 - len(ymdhm)))))


def _line_bars(c) -> Bars:
    c = np.asarray(c, float)
    return Bars(np.arange(c.size, dtype=np.int64) * HOUR, c.copy(), c + 0.5, c - 0.5, c.copy(), HOUR)


# ------------------------------------------------------------------ options.json_num


def test_json_num_on_a_real_deribit_row_numbers_as_they_came_else_none() -> None:
    rows = json.loads((FIX / "deribit_options_sample.json").read_text())["result"]
    r0, r2 = rows[0], rows[2]
    assert json_num(r0["underlying_price"]) == 82766.74
    assert json_num(r0["interest_rate"]) == 0.0  # a zero is a number, not an absent field
    ts = json_num(r0["creation_timestamp"])
    assert ts == 1791637657428 and isinstance(ts, int)  # ints stay ints
    assert r2["last"] is None and json_num(r2["last"]) is None  # Deribit sends null for a contract with no trade
    for bad in ("82766.74", True, False, [1.0], {"v": 1.0}, b"1"):
        assert json_num(bad) is None
    assert json_num(np.float64(1.5)) == 1.5  # np.float64 is a float subclass


# ------------------------------------------------------------------ pm_touch.et_midnight / et_time / et_date


def test_et_midnight_on_a_real_touch_event_and_across_dst_switches() -> None:
    ev = json.loads((FIX / "polymarket_touch_event_2026-10-16.json").read_text())
    end = int(datetime.fromisoformat(ev["endDate"].replace("Z", "+00:00")).timestamp())
    # the October 14 market ends at 00:00 ET of October 15 = 04:00 UTC (EDT, UTC-4)
    assert et_midnight(date(2026, 10, 15)) == end == _utc(2026, 10, 15, 4)
    # 2026 DST ends on Sunday Nov 1 (EDT -> EST): Nov 1 is 25 h long, Nov 2 starts at 05:00 UTC
    assert et_midnight(date(2026, 11, 1)) == _utc(2026, 11, 1, 4)
    assert et_midnight(date(2026, 11, 2)) == _utc(2026, 11, 2, 5)
    # 2026 DST starts on Sunday Mar 8 (EST -> EDT): Mar 8 is 23 h long
    assert et_midnight(date(2026, 3, 8)) == _utc(2026, 3, 8, 5)
    assert et_midnight(date(2026, 3, 9)) - et_midnight(date(2026, 3, 8)) == 23 * 3600


def test_et_time_wall_clock_new_york_to_unix_seconds() -> None:
    assert et_time(date(2026, 10, 14), 9, 30) == _utc(2026, 10, 14, 13, 30)  # EDT
    assert et_time(date(2026, 11, 2), 9, 30) == _utc(2026, 11, 2, 14, 30)  # EST
    assert et_time(date(2026, 10, 14), 23, 59) == _utc(2026, 10, 15, 3, 59)  # the next UTC date
    assert et_time(date(2026, 10, 14), 0, 0) == _utc(2026, 10, 14, 4)
    # 01:30 happens twice on Nov 1; without a fold argument the first (EDT) one is meant
    assert et_time(date(2026, 11, 1), 1, 30) == _utc(2026, 11, 1, 5, 30)


def test_et_date_of_a_real_end_date_and_late_utc_hours() -> None:
    ev = json.loads((FIX / "polymarket_touch_event_2026-10-16.json").read_text())
    end = int(datetime.fromisoformat(ev["endDate"].replace("Z", "+00:00")).timestamp())
    assert ev["slug"].endswith("october-14-2026")
    assert et_date(end - 1) == date(2026, 10, 14)  # the market's day (as `classify_touch_market` reads it)
    assert et_date(end) == date(2026, 10, 15)
    assert et_date(_utc(2026, 11, 2, 4, 30)) == date(2026, 11, 1)  # 23:30 EST
    assert et_date(_utc(2026, 11, 2, 5)) == date(2026, 11, 2)


# ------------------------------------------------------------------ pm_touch.title_direction


def test_title_direction_from_the_ladder_arrow_of_real_markets() -> None:
    ev = json.loads((FIX / "polymarket_touch_event_2026-10-16.json").read_text())
    by_title = {m["groupItemTitle"]: title_direction(m["groupItemTitle"]) for m in ev["markets"]}
    assert by_title["↑ 86,000"] == "up" and by_title["↓ 81,000"] == "down"
    for m in ev["markets"]:  # the arrow agrees with the question of every real market of the event
        assert title_direction(m["groupItemTitle"]) == ("up" if " reach " in m["question"] else "down")
    assert title_direction("  ↓ 81,000") == "down"  # leading spaces are stripped
    for t in ("86,000", "", None, "up ↑"):
        assert title_direction(t) is None


# ------------------------------------------------------------------ pendle.parse_expiry


def test_parse_expiry_on_real_pendle_expiries_naive_is_utc() -> None:
    markets = json.loads((FIX / "pendle_markets_all_2026-10-18.json").read_text())["markets"]
    exp = markets[0]["expiry"]
    assert exp == "2026-11-05T00:00:00.000Z"
    assert parse_expiry(exp) == datetime(2026, 11, 5, tzinfo=UTC)
    assert all(parse_expiry(m["expiry"]).tzinfo is not None for m in markets)
    assert parse_expiry("2026-11-05T00:00:00") == datetime(2026, 11, 5, tzinfo=UTC)
    # an explicit offset is kept, the instant is the same as 2026-11-05T00:00Z
    assert parse_expiry("2026-11-05T02:00:00+02:00").timestamp() == _utc(2026, 11, 5)
    for bad in (None, 1_793_836_800, "", "next thursday", b"2026-11-05"):
        assert parse_expiry(bad) is None


# ------------------------------------------------------------------ liquidations_agg.parse_ts


def test_parse_ts_unix_seconds_to_iso_with_range_and_type_checks() -> None:
    assert parse_ts(1_700_000_000) == (1_700_000_000, "2023-11-14T22:13:20+00:00")
    assert parse_ts(1_700_000_000.9) == (1_700_000_000, "2023-11-14T22:13:20+00:00")  # truncated like int()
    assert parse_ts("1700000000")[0] == 1_700_000_000  # int() accepts a digit string
    assert parse_ts(TS_MIN) == (TS_MIN, "2009-01-01T00:00:00+00:00")
    assert TS_MIN == _utc(2009, 1, 1) and TS_MAX == _utc(2100, 1, 1)
    for bad in (TS_MIN - 1, TS_MAX, 1_700_000_000_000, True, None, math.nan, math.inf, "1.7e9", [1]):
        with pytest.raises(ROW_ERRORS):  # every failure is one the caller catches to drop the row
            parse_ts(bad)


# ------------------------------------------------------------------ liquidation_flush.month_start


def test_month_start_floors_ms_to_the_utc_month() -> None:
    mar24 = _utc(2024, 3, 1) * 1000
    assert mar24 == 1_709_251_200_000
    assert month_start(_utc(2024, 3, 12, 15, 30) * 1000 + 123) == mar24
    assert month_start(mar24) == mar24  # a month start is its own month
    assert month_start(mar24 - 1) == _utc(2024, 2, 1) * 1000  # Feb 29 23:59:59.999 of a leap year
    assert month_start(_utc(2024, 1, 1) * 1000 - 1) == _utc(2023, 12, 1) * 1000  # across the year
    assert month_start(0) == 0


# ------------------------------------------------------------------ course_s14_ema.trend_ok


def test_trend_ok_on_a_rising_line_needs_trend_bars_closes_above_the_mid_ema() -> None:
    # c = 100 + i: the mid EMA (21) is seeded at bar 20 with mean(100 .. 120) = 110 < 120, after that it lags the line by
    # 10 and rises, so above_run[k] = k - 19 from bar 20 on; the default trend_bars = 10 first holds at bar 29
    b = _line_bars(100.0 + np.arange(40))
    ind = indicators(b, Params())
    assert [trend_ok(ind, k, Params()) for k in (20, 28, 29, 39)] == [False, False, True, True]
    # trend_bars = 1: bar 20 has a close above the mid EMA but no previous mid EMA to rise from
    p1 = Params(trend_bars=1)
    assert trend_ok(ind, 20, p1) is False and trend_ok(ind, 21, p1) is True
    # a falling line never closes above its mid EMA
    down = indicators(_line_bars(200.0 - np.arange(40)), Params())
    assert not any(trend_ok(down, k, p1) for k in range(40))


def test_trend_ok_flat_mid_ema_and_segment_start_are_no_trend() -> None:
    z = np.zeros(3)
    p = Params(trend_bars=2)

    def ind(pos, e_mid) -> Ind:
        return Ind(np.array(pos), z, np.array(e_mid, float), z, z, np.array([0, 5, 5]), np.full(3, -1), np.zeros(3, bool))

    assert trend_ok(ind([0, 1, 2], [1.0, 1.0, 2.0]), 2, p) is True
    assert trend_ok(ind([0, 1, 2], [1.0, 1.0, 1.0]), 2, p) is False  # the mid EMA must rise, not stay flat
    assert trend_ok(ind([0, 1, 0], [1.0, 1.0, 2.0]), 2, p) is False  # first bar of a new segment
    assert trend_ok(ind([0, 1, 2], [1.0, 1.0, 2.0]), 0, p) is False


# ------------------------------------------------------------------ elder_impulse_green.macd_hist


def test_macd_hist_by_hand_and_zero_on_a_line() -> None:
    # fast 2 (k = 2/3), slow 3 (k = 1/2), signal 2 (k = 2/3) on 0, 0, 0, 0, 3, 3:
    # EMA2 = -, 0, 0, 0, 2, 8/3; EMA3 = -, -, 0, 0, 3/2, 9/4; MACD = -, -, 0, 0, 1/2, 5/12
    # signal seeded at position 3 with mean(0, 0) = 0, then 1/2 * 2/3 = 1/3, 5/12 * 2/3 + 1/3 * 1/3 = 7/18
    # histogram = MACD - signal = -, -, -, 0, 1/6, 1/36
    x = [0.0, 0.0, 0.0, 0.0, 3.0, 3.0]
    out = macd_hist(_line_bars(x), np.arange(6), 2, 3, 2)
    np.testing.assert_allclose(out, [np.nan, np.nan, np.nan, 0.0, 1 / 6, 1 / 36], rtol=1e-12, atol=1e-15)
    # a second segment restarts every EMA: the same values again
    out = macd_hist(_line_bars(x + x), np.concatenate([np.arange(6), np.arange(6)]), 2, 3, 2)
    np.testing.assert_allclose(out, [np.nan, np.nan, np.nan, 0.0, 1 / 6, 1 / 36] * 2, rtol=1e-12, atol=1e-15)
    # defaults 12 / 26 / 9 on a line: each SMA-seeded EMA lags it by (n - 1) / 2 x slope, so MACD is the constant
    # 7 x slope from position 25, the signal equals it from its seed at position 33 and the histogram is 0
    c = 50.0 + 0.5 * np.arange(60)
    out = macd_hist(_line_bars(c), np.arange(60))
    assert np.isnan(out[:33]).all()
    np.testing.assert_allclose(out[33:], 0.0, atol=1e-9)


# ------------------------------------------------------------------ turn_of_month.is_second_to_last_day


def test_is_second_to_last_day_of_the_utc_month() -> None:
    def ms(d: date, hour: int = 12) -> int:
        return _utc(d.year, d.month, d.day, hour) * 1000

    assert is_second_to_last_day(ms(date(2026, 10, 30)))
    assert not is_second_to_last_day(ms(date(2026, 10, 31))) and not is_second_to_last_day(ms(date(2026, 10, 29)))
    assert is_second_to_last_day(ms(date(2024, 2, 28)))  # leap February has 29 days
    assert not is_second_to_last_day(ms(date(2024, 2, 27)))
    assert is_second_to_last_day(ms(date(2023, 2, 27)))  # a 28-day February
    assert is_second_to_last_day(ms(date(2026, 12, 30)))  # across the year
    assert is_second_to_last_day(ms(date(2026, 9, 29)))  # a 30-day month
    # the whole UTC day counts, from 00:00 to 23:59:59.999
    day0 = ms(date(2026, 10, 30), 0)
    assert is_second_to_last_day(day0) and is_second_to_last_day(day0 + DAY - 1)
    assert not is_second_to_last_day(day0 - 1) and not is_second_to_last_day(day0 + DAY)
    # exactly one day per month over a year of daily bars
    start = datetime(2026, 1, 1, tzinfo=UTC)
    hits = [start + timedelta(days=k) for k in range(365) if is_second_to_last_day(int((start + timedelta(days=k)).timestamp() * 1000))]
    assert [d.month for d in hits] == list(range(1, 13))
