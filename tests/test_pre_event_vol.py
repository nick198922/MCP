"""Pre-event DVOL ramp analytics: windows without look-ahead, merging, holidays, controls, gates, verdicts."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest

from tradedesk.analytics.pre_event_vol import (
    DAY_MS,
    HOUR_MS,
    control_ok,
    daily_closes,
    event_minus_control,
    hourly_level,
    hourly_window,
    is_quiet_excluded,
    level_profile,
    merge_events,
    ms_of,
    pick_controls,
    precision_gate,
    straddle_vega_frac,
    thesis_verdict,
    trading_verdict,
    us_federal_holidays,
    window_change,
)

T0 = ms_of(date(2024, 6, 12))  # Wednesday


def linear(n_days: int = 200, start: int = T0 - 100 * DAY_MS, slope: float = 0.001) -> dict[int, float]:
    return {start + i * DAY_MS: 0.5 + slope * i for i in range(n_days)}


def test_daily_closes_floors_to_day():
    c = daily_closes([[T0 + 5, 0.4], [T0 + DAY_MS, 0.41]])
    assert c == {T0: 0.4, T0 + DAY_MS: 0.41}


def test_window_change_analytic():
    c = linear()
    # close(T0-1) - close(T0-4) = 3 days * slope
    assert window_change(c, T0) == pytest.approx(0.003)
    assert window_change({}, T0) is None


def test_window_change_ignores_data_after_window_end():
    c = linear()
    base = window_change(c, T0)
    c2 = dict(c)
    for d in c2:
        if d >= T0:  # candles closing after T0 00:00
            c2[d] = 9.0
    assert window_change(c2, T0) == base
    c3 = dict(c)
    c3[T0 - DAY_MS] += 0.05  # the last candle that closes at T0 00:00 does matter
    assert window_change(c3, T0) != base


def test_level_profile():
    c = linear()
    prof = level_profile(c, T0, (-5, 0, 3))
    assert prof[-5] == pytest.approx(0.0)
    assert prof[0] == pytest.approx(0.005)
    assert prof[3] == pytest.approx(0.008)


def test_hourly_window_completeness():
    # real cache convention: key = candle open + 1 h = moment the close is known (verify_pm_threshold.py)
    start, end = T0 + 9 * HOUR_MS, T0 + 12 * HOUR_MS
    opens = [start - HOUR_MS + i * HOUR_MS for i in range(4)]  # candles opened 08:00..11:00 -> closes 09:00..12:00
    h = {o + HOUR_MS: 0.5 + 0.01 * i for i, o in enumerate(opens)}
    assert hourly_level(h, start) == pytest.approx(0.5) and hourly_level(h, end) == pytest.approx(0.53)
    w = hourly_window(h, start, end)
    assert w["complete"] and w["hours"] == 4 and w["change"] == pytest.approx(0.03)
    h[start - HOUR_MS] = 9.9  # a close known before the window start is not part of the window
    assert hourly_window(h, start, end)["change"] == pytest.approx(0.03)
    h[end + HOUR_MS] = 9.9  # nor one known after the window end
    assert hourly_window(h, start, end)["change"] == pytest.approx(0.03)
    del h[start]
    w = hourly_window(h, start, end)
    assert not w["complete"] and w["hours"] == 3 and w["change"] is None


def test_hourly_level_matches_daily_close_on_real_sample():
    """Deribit BTC DVOL 2025-10-01/02 from the research caches: daily close of d == hourly level at d + 24 h."""
    blob = json.loads((Path(__file__).parent / "fixtures" / "deribit_dvol_btc_daily_vs_hourly_2026-10-17.json").read_text())
    closes = daily_closes(blob["daily"])
    hourly = {int(t) * 1000: float(v) for t, v in blob["hourly"]}  # as scripts/verify_pre_event_vol.load_hourly
    assert len(closes) == 2
    for d, v in closes.items():
        assert hourly_level(hourly, d + DAY_MS) == pytest.approx(v, abs=1e-12)
        assert hourly_level(hourly, d + DAY_MS - HOUR_MS) != pytest.approx(v, abs=1e-6)  # the open-time reading is off


def test_merge_events_chains_within_gap():
    d = ms_of(date(2024, 1, 5))
    ev = [(d, "nfp"), (d + 3 * DAY_MS, "cpi"), (d + 6 * DAY_MS, "fomc"), (d + 10 * DAY_MS, "cpi"), (d + 10 * DAY_MS + 5, "nfp")]
    g = merge_events(ev)
    assert [x["day"] for x in g] == [d, d + 10 * DAY_MS]
    assert g[0]["kind"] == "multi" and g[0]["kinds"] == ["cpi", "fomc", "nfp"] and len(g[0]["days"]) == 3
    assert g[1]["kind"] == "multi"
    assert merge_events([(d, "cpi"), (d + 4 * DAY_MS, "nfp")])[1]["kind"] == "nfp"


def test_us_holidays_known_dates():
    h = us_federal_holidays(2022)
    assert date(2022, 1, 17) in h  # MLK
    assert date(2022, 6, 20) in h  # Juneteenth observed (Sunday 19th)
    assert date(2022, 7, 4) in h
    assert date(2022, 11, 24) in h  # Thanksgiving
    assert date(2022, 12, 26) in h  # Christmas observed
    assert date(2021, 12, 31) in us_federal_holidays(2021)  # New Year 2022 (Saturday) observed
    assert date(2023, 5, 29) in us_federal_holidays(2023)  # Memorial
    assert is_quiet_excluded(date(2023, 12, 27)) and is_quiet_excluded(date(2024, 1, 1))
    assert not is_quiet_excluded(date(2024, 6, 12))


def test_control_ok_blocks_events_and_holidays():
    c = linear()

    def has(d: int) -> bool:
        return window_change(c, d) is not None

    assert not control_ok(T0 + 7 * DAY_MS, set(), has)  # 2024-06-19 = Juneteenth
    tc = T0 + 14 * DAY_MS
    assert control_ok(tc, set(), has)
    assert not control_ok(tc, {tc + DAY_MS}, has)  # event one day after the window
    assert not control_ok(tc, {tc - 4 * DAY_MS}, has)  # event one day before the window start
    assert control_ok(tc, {tc + 2 * DAY_MS, tc - 5 * DAY_MS}, has)
    july4 = ms_of(date(2024, 7, 4))
    assert not control_ok(july4 + 2 * DAY_MS, set(), has)  # window holds Independence Day
    assert not control_ok(T0 + 400 * DAY_MS, set(), has)  # no data


def test_pick_controls_order_and_limit():
    ok_all = lambda d: True  # noqa: E731
    cs = pick_controls(T0, ok_all, n=6, max_weeks=8)
    assert cs == [T0 - 7 * DAY_MS, T0 + 7 * DAY_MS, T0 - 14 * DAY_MS, T0 + 14 * DAY_MS, T0 - 21 * DAY_MS, T0 + 21 * DAY_MS]
    only_late = lambda d: d > T0 + 40 * DAY_MS  # noqa: E731
    assert pick_controls(T0, only_late, n=6, max_weeks=8) == [T0 + 7 * k * DAY_MS for k in (6, 7, 8)]


def test_event_minus_control():
    assert event_minus_control(0.03, [0.01, None, 0.02, 0.0]) == pytest.approx(0.02)
    assert event_minus_control(0.03, [0.01, 0.02]) is None
    assert event_minus_control(None, [0.0] * 6) is None


def test_precision_gate_analytic():
    dx = [0.05, -0.05] * 50  # SD = 0.05 * sqrt(100/99)
    g = precision_gate(dx)
    sd = 0.05 * math.sqrt(100 / 99)
    assert g["sd"] == pytest.approx(sd)
    assert g["half_width"] == pytest.approx(1.96 * sd / 10)
    assert g["mde"] == pytest.approx(2.8 * sd / 10)
    assert g["passed"] is True  # 0.0098 <= 0.010 and 0.0141 <= 0.015
    assert precision_gate([0.06, -0.06] * 50)["passed"] is False


def test_thesis_verdict_mapping():
    assert thesis_verdict(0.01, (0.002, 0.02)) == "✓"
    assert thesis_verdict(0.0, (-0.006, 0.004)) == "✗"
    assert thesis_verdict(0.003, (-0.004, 0.012)) == "≈"
    assert thesis_verdict(-0.001, (-0.01, 0.008)) == "◯"


def test_trading_verdict():
    kw = dict(sign_fomc=0.02, sign_cpi=0.01, mean_drop_top3=0.012, mean_late=0.01)
    assert trading_verdict(0.016, (0.004, 0.028), **kw)["passed"]
    r = trading_verdict(0.010, (0.001, 0.019), **kw)
    assert not r["passed"] and r["reasons"] == ["mean_below_threshold"]
    r = trading_verdict(0.02, (0.01, 0.03), sign_fomc=0.02, sign_cpi=-0.01, mean_drop_top3=0.004, mean_late=-0.001)
    assert set(r["reasons"]) == {"fomc_cpi_sign_differs", "drop_top3_below_min", "late_period_not_positive"}


def test_straddle_vega():
    # card: ~0.23 % of spot per vol point for a 30-day ATM straddle
    assert straddle_vega_frac(30.0) == pytest.approx(0.00229, abs=2e-5)
