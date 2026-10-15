"""Weekend-vol analytics: windows, realized variance, pooled ratios, bootstrap, gate, hedge, entry without look-ahead."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics.event_straddle import entry_quote, expiry_code
from tradedesk.analytics.put_skew import inverse_price, year_frac
from tradedesk.analytics.weekend_vol import (
    DAY_MS,
    HOUR_MS,
    block_bootstrap,
    event_weeks,
    h0_gate,
    hedged_short_straddle,
    median_weekly_diff,
    pooled_ratio,
    prices_from_bars,
    ratio_diff,
    straddle_delta,
    week_windows,
    weekend_share,
    window_extremes,
    window_rv,
)


def ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


TUE = ms(2024, 6, 11)  # a Tuesday


def test_week_windows_are_40_hours_on_the_right_days():
    w = week_windows(TUE)
    assert w["ctrl"] == (ms(2024, 6, 11, 16), ms(2024, 6, 13, 8))
    assert w["wkd"] == (ms(2024, 6, 14, 16), ms(2024, 6, 16, 8))
    for a, b in w.values():
        assert b - a == 40 * HOUR_MS
    assert datetime.fromtimestamp(w["wkd"][0] / 1000, UTC).strftime("%a") == "Fri"


def test_prices_and_window_rv_analytic():
    # constant 1 % hourly log return: RV of 40 hours = 40 * 0.01^2
    bars = [[TUE + i * HOUR_MS, 0, 0, 0, 100.0 * math.exp(0.01 * (i + 1))] for i in range(24 * 7)]
    p = prices_from_bars(bars)
    assert p[TUE + HOUR_MS] == pytest.approx(100.0 * math.exp(0.01))
    a, b = week_windows(TUE)["ctrl"]
    assert window_rv(p, a, b) == pytest.approx(40 * 1e-4)
    # alternating +/-2 %: RV = n * 0.0004 regardless of sign
    alt = {TUE + i * HOUR_MS: 100.0 * math.exp(0.02 * (i % 2)) for i in range(50)}
    assert window_rv(alt, TUE, TUE + 10 * HOUR_MS) == pytest.approx(10 * 4e-4)


def test_window_rv_gap_and_bad_window():
    p = {TUE + i * HOUR_MS: 100.0 for i in range(10) if i != 5}
    assert window_rv(p, TUE, TUE + 9 * HOUR_MS) is None
    assert window_rv(p, TUE, TUE + 4 * HOUR_MS) == 0.0
    with pytest.raises(ValueError):
        window_rv(p, TUE, TUE + 90 * 60_000)


def test_rv_before_t_unchanged_when_data_after_t_changes():
    """No look-ahead: a window ending at t ignores any price after t."""
    p = {TUE + i * HOUR_MS: 100.0 + math.sin(i) for i in range(200)}
    a, b = week_windows(TUE)["ctrl"]
    r0 = window_rv(p, a, b)
    q = dict(p)
    for t in q:
        if t > b:
            q[t] *= 3.0
    assert window_rv(q, a, b) == r0


def test_weekend_share_proportional_for_constant_vol():
    mon = TUE - DAY_MS
    p = {mon + i * HOUR_MS: 100.0 * math.exp(0.01 * (i % 2)) for i in range(24 * 9)}
    wk, week = weekend_share(p, mon)
    assert wk / week == pytest.approx(2 / 7)


def test_event_weeks_maps_weekdays_to_tuesday():
    wed, fri, mon = TUE + DAY_MS, TUE + 3 * DAY_MS, TUE - DAY_MS
    assert event_weeks([wed + 13 * HOUR_MS], (0, 1, 2)) == {TUE}
    assert event_weeks([fri], (0, 1, 2)) == set()
    assert event_weeks([fri], (3,)) == {TUE}
    assert event_weeks([mon], (0, 1, 2, 3)) == set()


def test_pooled_ratios_and_median_estimator():
    rows = [(2.0, 4.0, 1.0, 2.0), (6.0, 4.0, 3.0, 2.0)]
    assert pooled_ratio([(a, b) for a, b, _, _ in rows]) == pytest.approx(1.0)
    assert ratio_diff(rows) == pytest.approx(0.0)
    assert ratio_diff([(1.0, 1.0, 0.5, 1.0)]) == pytest.approx(0.5)
    assert median_weekly_diff([(1.0, 1.0, 0.5, 1.0), (1.0, 1.0, 1.0, 1.0), (1.0, 1.0, 0.0, 1.0)]) == pytest.approx(0.5)


def test_block_bootstrap_constant_and_noise():
    rows = [(1.0, 2.0)] * 50
    b = block_bootstrap(rows, pooled_ratio, n_boot=300)
    assert b["stat"] == pytest.approx(0.5) and b["ci95"] == pytest.approx((0.5, 0.5))
    rows = [(1.0 + (-1) ** i * 0.5, 1.0) for i in range(400)]
    b = block_bootstrap(rows, pooled_ratio, n_boot=2000)
    assert b["ci95"][0] < 1.0 < b["ci95"][1]
    assert b["se"] == pytest.approx(0.5 / math.sqrt(400), rel=0.15)


def test_h0_gate():
    yearly = {"2021": 0.7, "2022": 0.75, "2023": 0.8, "2024": 0.6, "2025": 0.7, "2026": 0.78}
    assert h0_gate(0.72, 0.85, yearly)["passed"]
    assert not h0_gate(0.80, 0.85, yearly)["passed"]
    assert not h0_gate(0.72, 1.01, yearly)["passed"]
    two_bad = dict(yearly, **{"2024": 0.9})
    g = h0_gate(0.72, 0.85, two_bad)
    assert g["years_passed"] == 4 and not g["passed"]


def test_straddle_delta():
    assert straddle_delta(100.0, 100.0, 1e-9, 0.5) == pytest.approx(0.0, abs=1e-3)
    T = 40 / (24 * 365)
    d1 = 0.5 * 0.5 * math.sqrt(T)
    assert straddle_delta(100.0, 100.0, T, 0.5) == pytest.approx(math.erf(d1 / math.sqrt(2)))
    assert straddle_delta(120.0, 100.0, 0.0, 0.5) == 1.0
    assert straddle_delta(80.0, 100.0, 0.0, 0.5) == -1.0


def test_hedged_short_straddle_flat_path_and_trend():
    t0, exp = ms(2024, 6, 14, 16), ms(2024, 6, 16, 8)
    q = {"t0": t0, "expiry": exp, "K": 100.0, "iv_call": 0.5, "iv_put": 0.5, "premium": 2.0, "spot": 100.0}
    flat = {t: 100.0 for t in range(t0, exp + 1, HOUR_MS)}
    assert hedged_short_straddle(q, flat, 100.0, fee=0.0) == pytest.approx(0.02)
    # a steady rally: the hedge (long delta) earns part of the straddle loss back
    up = {t: 100.0 + 10.0 * (t - t0) / (exp - t0) for t in range(t0, exp + 1, HOUR_MS)}
    hedged = hedged_short_straddle(q, up, 110.0, fee=0.0)
    assert -0.08 < hedged < 0.02
    assert hedged_short_straddle(q, {t0: 100.0}, 110.0) is None


def test_window_extremes():
    bars = [[TUE + i * HOUR_MS, 0, 100 + i, 90 - i, 95] for i in range(10)]
    assert window_extremes(bars, TUE + 2 * HOUR_MS, TUE + 5 * HOUR_MS) == (86, 104)
    assert window_extremes(bars, TUE + 20 * HOUR_MS, TUE + 30 * HOUR_MS) is None


# ----------------------------------------------------------------------------- entry 16:00 on a 2-day expiry

FRI = ms(2024, 6, 14)
T0 = FRI + 16 * HOUR_MS
EXP = FRI + 2 * DAY_MS + 8 * HOUR_MS
S = 60_000.0


def pr(K: float, call: bool, ts: int, iv: float = 0.4, idx: float = S) -> dict:
    mark = inverse_price(idx, K, year_frac(EXP, ts), iv, call)
    return {
        "instrument_name": f"{expiry_code(EXP)}{int(K)}-{'C' if call else 'P'}",
        "timestamp": ts,
        "iv": iv * 100,
        "price": mark,
        "mark_price": mark,
        "index_price": idx,
        "direction": "sell",
        "amount": 1.0,
    }


def test_entry_quote_two_day_expiry_at_16():
    tr = [pr(60_000, c, T0 - k * 600_000) for k in range(1, 6) for c in (True, False)]
    q = entry_quote(tr, FRI, entry_hour=16, expiry_days=2)
    assert q["covered"] and q["expiry"] == EXP and q["t0"] == T0
    assert q["T"] == pytest.approx(40 / (24 * 365))
    assert q["iv_call"] == pytest.approx(0.4, abs=1e-4)
    # prints after 16:00 (wild IV / index, new strikes) do not change the entry
    later = [pr(60_000, True, T0 + 1, iv=1.5, idx=70_000), pr(59_000, True, T0 + 2), pr(59_000, False, T0 + 3)]
    assert entry_quote(tr + later, FRI, entry_hour=16, expiry_days=2) == q
    # the default (next-day expiry) does not see the Sunday contract
    assert not entry_quote(tr, FRI, entry_hour=16)["covered"]
