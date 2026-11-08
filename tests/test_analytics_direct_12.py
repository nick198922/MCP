"""Direct tests for ten analytics helpers that had no mention in tests/ (BACKLOG 6.9, card item 207).

They feed the demo reconciliation and demo reports (`demo_execution.day_of`, `demo_rr_threshold.day_key`), the
Polymarket calibration screen (`pm_calibration.json_list`), the course-module reports S28 / S40
(`s28_modules.close_at`, `s40_modules.close_at`), the event and listing reports (`event_straddle.day_of`,
`pre_event_vol.day_of`, `pre_event_vol.date_of`, `listing_drift.day_of`) and the regime-side filter of the demo
setups (`regime_side.side_sign`). Four of them share the name `day_of`, which is why the word-based coverage audit
counted them together; two return a UTC day number and two the 00:00 UTC ms of the day, and each is pinned here
to its own form. References are real samples from tests/fixtures/ (demo trades, Gamma markets, Binance klines,
the ForexFactory calendar) and calendar facts (day numbers via `date` subtraction, epoch seconds via
`calendar.timegm`); no output of the code under test is reused as an expected value.
"""

from __future__ import annotations

import calendar
import json
import math
from datetime import date, datetime
from pathlib import Path

import numpy as np

from tradedesk.analytics import event_straddle, listing_drift, pre_event_vol
from tradedesk.analytics import s28_modules as s28
from tradedesk.analytics import s40_modules as s40
from tradedesk.analytics.demo_execution import day_of as demo_day_of
from tradedesk.analytics.demo_execution import summarise
from tradedesk.analytics.demo_rr_threshold import day_key
from tradedesk.analytics.pm_calibration import classify_market, json_list, resolved_outcome
from tradedesk.analytics.regime_side import side_sign, split

FIX = Path(__file__).parent / "fixtures"
HOUR = 3_600_000
DAY = 24 * HOUR
EPOCH = date(1970, 1, 1)


def _utc_ms(*ymdhm: int) -> int:
    """Unix ms of a UTC wall-clock time (calendar.timegm: no time-zone database involved)."""
    return calendar.timegm((*ymdhm, *([0] * (6 - len(ymdhm))))) * 1000


def _day_number(d: date) -> int:
    return (d - EPOCH).days


def _demo_trades() -> list[dict]:
    return json.loads((FIX / "demo_trades_live_2026-10-24.json").read_text())["trades"]


def _klines(name: str, key: str) -> tuple[np.ndarray, np.ndarray, list]:
    rows = json.loads((FIX / name).read_text())[key]
    t = np.array([int(r[0]) for r in rows], dtype=np.int64)
    c = np.array([float(r[4]) for r in rows])
    return t, c, rows


# ------------------------------------------------------------------ demo_execution.day_of / demo_rr_threshold.day_key


def test_demo_day_of_clusters_real_demo_trades_by_utc_day() -> None:
    trades = _demo_trades()
    days = [demo_day_of(t["signal_ts"]) for t in trades]
    # the 18 real demo trades (Bybit Demo, 21-24.10.2026): signals at 16:00 on 21.10, then 00:00 .. 16:00 on 22.10,
    # 04:00 .. 16:00 on 23.10 and 00:00 / 04:00 on 24.10 (4h setups)
    by_day = {d: days.count(d) for d in sorted(set(days))}
    assert by_day == {
        _day_number(date(2026, 10, 21)): 3,
        _day_number(date(2026, 10, 22)): 7,
        _day_number(date(2026, 10, 23)): 4,
        _day_number(date(2026, 10, 24)): 4,
    }
    # a signal exactly at 00:00 UTC belongs to the day it opens, not the one before
    assert trades[3]["signal_ts"] == _utc_ms(2026, 10, 22) and days[3] == _day_number(date(2026, 10, 22))
    assert demo_day_of(_utc_ms(2026, 10, 22) - 1) == _day_number(date(2026, 10, 21))
    # summarise clusters the reconciliation intervals by exactly these days
    r = summarise(trades, "r_multiple", n_boot=200)
    picked = [t for t in trades if t.get("r_multiple") is not None]
    assert r["n"] == len(picked) and r["n_days"] == len({demo_day_of(t["signal_ts"]) for t in picked})


def test_day_key_is_the_utc_day_number_of_a_demo_decision() -> None:
    t0 = _demo_trades()[0]
    # opened 2026-10-21 20:02:33 UTC, closed 2026-10-22 09:02:37 UTC
    assert day_key(t0["opened_ts"]) == _day_number(date(2026, 10, 21))
    assert day_key(t0["closed_ts"]) == _day_number(date(2026, 10, 22))
    assert day_key(0) == 0 and day_key(DAY - 1) == 0 and day_key(DAY) == 1
    assert day_key(_utc_ms(2024, 2, 29, 23, 59)) == _day_number(date(2024, 2, 29))
    assert isinstance(day_key(t0["opened_ts"]), int)


# ------------------------------------------------------------------ pm_calibration.json_list


def test_json_list_reads_gamma_string_arrays_of_real_resolved_markets() -> None:
    markets = json.loads((FIX / "polymarket_touch_closed_markets_2026-11-04.json").read_text())["markets"]
    m = markets["632870"]
    assert isinstance(m["outcomes"], str)  # Gamma sends these arrays as JSON text
    assert json_list(m["outcomes"]) == ["Yes", "No"]
    assert json_list(m["outcomePrices"]) == ["1", "0"]  # strings stay strings; resolved_outcome converts them
    toks = json_list(m["clobTokenIds"])
    assert len(toks) == 2 and all(isinstance(x, str) and x.isdigit() and len(x) > 60 for x in toks)  # no int / float
    assert resolved_outcome(m["outcomePrices"]) == 1 and resolved_outcome(markets["632885"]["outcomePrices"]) == 0
    already = ["Yes", "No"]
    assert json_list(already) is already  # a decoded list is passed through as is
    for bad in ("", "Yes,No", '{"a": 1}', '"Yes"', "null", "1", None, 1, ("Yes", "No"), b'["Yes"]'):
        assert json_list(bad) is None
    assert json_list("[]") == []  # empty but a list: classify_market then rejects it as "tokens"
    assert classify_market({"id": "e"}, {**m, "outcomes": "[]"})["status"] == "tokens"


# ------------------------------------------------------------------ s28_modules.close_at / s40_modules.close_at


def test_close_at_on_real_binance_1h_klines() -> None:
    t, c, rows = _klines("market_flow_btc_1h_2026-10-28.json", "binance_fapi_klines")
    assert all(int(r[6]) + 1 == int(r[0]) + HOUR for r in rows)  # Binance close time = open + 1h - 1 ms
    for k in (0, 1, len(rows) // 2, len(rows) - 1):
        end = int(rows[k][6]) + 1  # the ms the bar ends at
        assert s28.close_at(t, c, end) == float(rows[k][4])
        assert s40.close_at(t, c, end) == float(rows[k][4])  # default bar 1h
    assert s28.close_at(t, c, 1793023200000 + HOUR) == 80385.40  # first bar, value read off the raw row
    first_open, last_end = int(t[0]), int(t[-1]) + HOUR
    for f in (s28.close_at, s40.close_at):
        assert math.isnan(f(t, c, first_open))  # that bar opened before the sample
        assert math.isnan(f(t, c, last_end + HOUR))  # after the last bar
        assert math.isnan(f(t, c, int(t[3]) + HOUR + 1))  # not on a bar end
    # a missing bar is NaN, not the neighbour's close
    gap = np.delete(np.arange(t.size), 5)
    tg, cg = t[gap], c[gap]
    assert math.isnan(s28.close_at(tg, cg, int(t[5]) + HOUR)) and math.isnan(s40.close_at(tg, cg, int(t[5]) + HOUR))
    assert s28.close_at(tg, cg, int(t[6]) + HOUR) == c[6]


def test_s40_close_at_with_daily_bars_of_a_real_sample() -> None:
    t, c, rows = _klines("cycle_regime_btc_1d_2026-10-28.json", "binance_perp_klines_1d")
    assert int(t[0]) == _utc_ms(2026, 7, 22)  # short_start_ms of the capture
    assert s40.close_at(t, c, _utc_ms(2026, 7, 23), DAY) == float(rows[0][4]) == 66082.00
    assert math.isnan(s40.close_at(t, c, _utc_ms(2026, 7, 23)))  # 1h bars assumed: no bar opened at 22.07 23:00
    assert math.isnan(s40.close_at(t, c, _utc_ms(2026, 7, 23, 12), DAY))


# ------------------------------------------------------------------ event_straddle / pre_event_vol / listing_drift


def test_day_start_floors_real_calendar_events_to_00_utc() -> None:
    rows = json.loads((FIX / "forexfactory_thisweek_2026-10-18.json").read_text())["thisweek"]
    by_title = {(r["country"], r["title"], r["date"][:10]): r["date"] for r in rows}
    claims = int(datetime.fromisoformat(by_title[("USD", "Unemployment Claims", "2026-10-22")]).timestamp() * 1000)
    gdp = int(datetime.fromisoformat(by_title[("CNY", "GDP q/y", "2026-10-18")]).timestamp() * 1000)
    assert claims == _utc_ms(2026, 10, 22, 12, 30)  # 08:30 EDT
    assert gdp == _utc_ms(2026, 10, 19, 2)  # 22:00 EDT on 18.10 is already 19.10 in UTC
    for f in (event_straddle.day_of, pre_event_vol.day_of):
        assert f(claims) == _utc_ms(2026, 10, 22)
        assert f(gdp) == _utc_ms(2026, 10, 19)  # the UTC date, not the ET one
        assert f(_utc_ms(2026, 10, 22)) == _utc_ms(2026, 10, 22) and f(_utc_ms(2026, 10, 22) - 1) == _utc_ms(2026, 10, 21)
        assert f(float(claims) + 0.7) == _utc_ms(2026, 10, 22) and isinstance(f(float(claims)), int)
    # the same instant as a UTC day number (listing_drift) is the day start / DAY
    assert listing_drift.day_of(claims) == _day_number(date(2026, 10, 22)) == event_straddle.day_of(claims) // DAY
    assert listing_drift.day_of(float(gdp)) == _day_number(date(2026, 10, 19))
    assert listing_drift.day_of(np.int64(DAY - 1)) == 0


def test_date_of_on_real_daily_kline_opens() -> None:
    d = json.loads((FIX / "cycle_regime_btc_1d_2026-10-28.json").read_text())
    assert pre_event_vol.date_of(d["spot_start_ms"]) == date(2025, 5, 1)
    assert pre_event_vol.date_of(d["end_ms"]) == date(2026, 10, 28)  # the capture day
    opens = [int(r[0]) for r in d["binance_spot_klines_1d"]]
    got = [pre_event_vol.date_of(t) for t in opens]
    assert all((b - a).days == 1 for a, b in zip(got, got[1:], strict=False))  # consecutive calendar days
    assert got[0] == date(2025, 5, 1) and got[-1] == date(2026, 10, 27)
    assert pre_event_vol.date_of(_utc_ms(2024, 2, 29) + DAY - 1) == date(2024, 2, 29)
    assert pre_event_vol.date_of(np.int64(DAY - 1)) == EPOCH and pre_event_vol.date_of(DAY) == date(1970, 1, 2)


# ------------------------------------------------------------------ regime_side.side_sign


def test_side_sign_of_real_demo_sides_and_split() -> None:
    trades = _demo_trades()
    sides = [t["side"] for t in trades]
    assert set(sides) <= {"long", "short"}
    assert side_sign("long") == 1 and side_sign("short") == -1
    assert side_sign(np.array(["long"])[0]) == 1  # numpy string elements, as the report script passes them
    signs = [side_sign(s) for s in sides]
    assert signs.count(1) == sides.count("long") and signs.count(-1) == sides.count("short")
    # under a bearish regime (-1) shorts are kept and longs dropped; under 0 nobody is filtered
    kept, dropped = split(sides, [-1] * len(sides))
    assert kept.tolist() == [s == "short" for s in sides] and dropped.tolist() == [s == "long" for s in sides]
    kept0, dropped0 = split(sides, [0] * len(sides))
    assert not kept0.any() and not dropped0.any()
