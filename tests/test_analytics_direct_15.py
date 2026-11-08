"""Direct tests for the analytics helpers left by the strict check of BACKLOG 6.9 (card item 252).

The strict check (the function name as an identifier in test code, not a word in a comment) left nine public
functions without a direct test: ``dex.num``, ``option_xvenue.num``, ``expiry_pin.distance``, ``macro_regimes.rolling``,
``option_replay.atm_strike``, ``options.npdf``, ``pm_gap_report.primary_early_payout``, ``hull_suite_flip.wma`` and
``smart_money.mark_price``. Every expected value below is an analytic reference (the standard normal density, the
linearly weighted mean written out term by term) or worked out by hand in the comments; no output of the code under test
is reused as an expected value. Edge cases pin the behaviour the code has today (src/ was not changed by item 252; the
OverflowError of ``dex.num`` was fixed later, by item 260).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics import dex, option_xvenue
from tradedesk.analytics.expiry_pin import distance, in_band
from tradedesk.analytics.macro_regimes import rolling
from tradedesk.analytics.option_replay import atm_strike
from tradedesk.analytics.options import black76_greeks, ncdf, npdf
from tradedesk.analytics.pm_gap_report import primary_early_payout
from tradedesk.analytics.sieve_specs.hull_suite_flip import wma
from tradedesk.analytics.smart_money import mark_price

H = 3_600
T = 1_800_000_000  # an arbitrary entry time, unix seconds
INV_SQRT_2PI = 0.3989422804014327  # 1 / sqrt(2 pi)


# --- dex.num / option_xvenue.num ------------------------------------------------------------------------------------


def test_dex_num_parses_numbers_and_numeric_strings():
    assert dex.num(2) == 2.0 and isinstance(dex.num(2), float)
    assert dex.num(-3.25) == -3.25  # sign kept (price changes are negative)
    assert dex.num(0) == 0.0 and dex.num("0") == 0.0
    assert dex.num("0.1497") == pytest.approx(0.1497)  # DexScreener sends priceUsd as a string
    assert dex.num(" 2.5\n") == 2.5  # float() strips surrounding whitespace
    assert dex.num("1e3") == 1000.0


def test_dex_num_rejects_non_finite_and_bad_types():
    for bad in (None, True, False, "", "abc", "NaN", "inf", "-inf", "1e400", float("nan"), float("inf"), [], {}, [1]):
        assert dex.num(bad) is None, bad


def test_dex_num_takes_an_integer_too_large_for_a_float_as_none():
    # Review 2026-11-07-analytics-direct-tests-15: json.loads turns a 400-digit literal into a Python int and float()
    # raises OverflowError on it (a JSON "1e400" arrives as float inf and was handled already); num now catches it
    # like option_xvenue.num instead of raising out of the tool (card item 260).
    assert dex.num(10**400) is None and dex.num(-(10**400)) is None
    assert dex.num(10**300) == 1e300 and dex.num(-(10**300)) == -1e300  # still representable: kept, sign too


def test_option_xvenue_num_keeps_only_strictly_positive_finite():
    assert option_xvenue.num(1) == 1.0 and option_xvenue.num("0.0215") == pytest.approx(0.0215)
    # Venues send 0 / "0" / "" for an empty book side: all None, as are negatives and non-finite values.
    for bad in (None, True, 0, "0", 0.0, "", -1, "-0.5", float("nan"), float("inf"), float("-inf"), "abc", [], {}):
        assert option_xvenue.num(bad) is None, bad
    assert option_xvenue.num(5e-324) == 5e-324  # the smallest positive float is still > 0


def test_option_xvenue_num_takes_an_integer_too_large_for_a_float_as_none():
    # Review 2026-11-07-analytics-direct-tests-15: json.loads turns a 400-digit literal into a Python int and float()
    # raises OverflowError on it; num catches it like a malformed value instead of raising out of the tool.
    assert option_xvenue.num(10**400) is None and option_xvenue.num(-(10**400)) is None
    assert option_xvenue.num(10**300) == 1e300  # still representable: kept


# --- expiry_pin.distance --------------------------------------------------------------------------------------------


def test_distance_is_absolute_relative_gap():
    # |110 - 100| / 100 = 0.1 and |90 - 100| / 100 = 0.1: symmetric around spot.
    assert distance(110.0, 100.0) == pytest.approx(0.1, abs=1e-15)
    assert distance(90.0, 100.0) == pytest.approx(0.1, abs=1e-15)
    assert distance(100.0, 100.0) == 0.0
    # BTC-like numbers: strike 105,000 vs spot 101,500 -> 3,500 / 101,500 = 0.034483 (3.45 %).
    assert distance(105_000.0, 101_500.0) == pytest.approx(3_500 / 101_500, rel=1e-12)
    # in_band is built on it, both edges inclusive.
    assert in_band(110.0, 100.0, 0.1, 0.2) and in_band(80.0, 100.0, 0.1, 0.2)
    assert not in_band(100.5, 100.0, 0.01, 0.2)


def test_distance_edge_cases_as_written():
    # No guard: spot 0 divides by zero; a negative spot gives a negative "distance". Callers pass a positive spot.
    with pytest.raises(ZeroDivisionError):
        distance(100.0, 0.0)
    assert distance(10.0, -100.0) == pytest.approx(-1.1)  # |10 + 100| / -100
    assert math.isnan(distance(float("nan"), 100.0))


# --- macro_regimes.rolling ------------------------------------------------------------------------------------------


def _sum0(a: list[np.ndarray]) -> float:
    return float(a[0].sum())


def test_rolling_is_causal_and_drops_missing_rows():
    x = [1.0, 2.0, 3.0, 4.0, None, 6.0]
    out = rolling([x], _sum0, window=3, min_valid=2)
    # i = 0, 1: window not full -> None; i = 2: 1+2+3 = 6; i = 3: 2+3+4 = 9; i = 4: rows 2, 3 (4 is missing) = 7;
    # i = 5: rows 3, 5 = 4 + 6 = 10.
    assert out == [None, None, 6.0, 9.0, 7.0, 10.0]
    # Changing a later value never changes an earlier output.
    y = x[:5] + [1000.0]
    assert rolling([y], _sum0, window=3, min_valid=2)[:5] == out[:5]


def test_rolling_min_valid_and_joint_missing():
    a = [1.0, None, 3.0, 4.0]
    b = [10.0, 20.0, None, 40.0]

    def spread(c: list[np.ndarray]) -> float:
        return float((c[1] - c[0]).sum())

    # Window 4 at i = 3 covers all rows; only rows 0 and 3 have both columns: (10 - 1) + (40 - 4) = 45.
    assert rolling([a, b], spread, window=4, min_valid=2) == [None, None, None, 45.0]
    # Two complete rows < min_valid 3 -> None.
    assert rolling([a, b], spread, window=4, min_valid=3) == [None, None, None, None]
    # The stat receives one float array per column, already aligned on the kept rows.
    seen: list[list[np.ndarray]] = []
    rolling([a, b], lambda c: seen.append(c) or 0.0, window=4, min_valid=1)
    assert len(seen) == 1 and [list(c) for c in seen[0]] == [[1.0, 4.0], [10.0, 40.0]]
    assert seen[0][0].dtype == float


def test_rolling_empty_and_short_input():
    assert rolling([[]], _sum0) == []
    assert rolling([[1.0, 2.0]], _sum0, window=3, min_valid=1) == [None, None]
    with pytest.raises(IndexError):  # no column at all: cols[0] does not exist
        rolling([], _sum0)


# --- option_replay.atm_strike ---------------------------------------------------------------------------------------


def _exp(F: float, strikes: dict[float, tuple[float, float]]) -> dict:
    return {"F": F, "strikes": {k: {"put_delta": pd, "mark_iv": iv} for k, (pd, iv) in strikes.items()}}


def test_atm_strike_nearest_forward_with_black76_delta():
    # Analytic reference: F = K = 100, T = 30/365, sigma = 0.5 -> d1 = sigma sqrt(T) / 2 = 0.071667, call delta
    # N(d1) = 0.52857, put delta = N(d1) - 1 = -0.47143; 1 + put delta = 0.52857 is inside [0.40, 0.60].
    T_ = 30 / 365
    pd_atm = black76_greeks(100.0, 100.0, T_, 0.5, False)["delta"]
    assert pd_atm == pytest.approx(ncdf(0.5 * 0.5 * math.sqrt(T_)) - 1.0, abs=1e-12)
    exp = _exp(101.0, {95.0: (-0.35, 0.52), 100.0: (pd_atm, 0.50), 105.0: (-0.62, 0.49)})
    assert atm_strike(exp) == (100.0, 0.50)


def test_atm_strike_tie_band_edges_and_empty():
    # Equal distance to 95 and 105 from F = 100: the lower strike wins.
    exp = _exp(100.0, {95.0: (-0.45, 0.55), 105.0: (-0.55, 0.45)})
    assert atm_strike(exp) == (95.0, 0.55)
    # Band edges inclusive: put delta -0.60 -> call delta 0.40; -0.40 -> 0.60.
    assert atm_strike(_exp(100.0, {100.0: (-0.60, 0.5)})) == (100.0, 0.5)
    assert atm_strike(_exp(100.0, {100.0: (-0.40, 0.5)})) == (100.0, 0.5)
    # Just outside: nearest strike exists but its delta is not ATM -> None (no fall-back to another strike).
    assert atm_strike(_exp(100.0, {100.0: (-0.61, 0.5), 110.0: (-0.5, 0.5)})) is None
    assert atm_strike(_exp(100.0, {100.0: (-0.39, 0.5)})) is None
    # Custom band and integer strike / iv come back as floats.
    out = atm_strike(_exp(100.0, {100: (-0.3, 1)}), band=(0.6, 0.8))
    assert out == (100.0, 1.0) and all(isinstance(v, float) for v in out)
    assert atm_strike({"F": 100.0, "strikes": {}}) is None


# --- options.npdf ---------------------------------------------------------------------------------------------------


def test_npdf_matches_standard_normal_density():
    assert npdf(0.0) == pytest.approx(INV_SQRT_2PI, rel=1e-15)
    assert npdf(1.0) == pytest.approx(0.24197072451914337, rel=1e-14)  # exp(-1/2) / sqrt(2 pi)
    assert npdf(2.0) == pytest.approx(0.05399096651318806, rel=1e-14)  # exp(-2) / sqrt(2 pi)
    for x in (0.3, 1.7, 4.2):
        assert npdf(-x) == npdf(x)  # even function
    # Normalised: trapezoid over [-10, 10] integrates to 1.
    xs = np.linspace(-10.0, 10.0, 20_001)
    ys = np.array([npdf(float(v)) for v in xs])
    assert float(np.sum((ys[1:] + ys[:-1]) * 0.5 * np.diff(xs))) == pytest.approx(1.0, abs=1e-9)
    # It is the derivative of ncdf: central difference at 0.7.
    h = 1e-5
    assert (ncdf(0.7 + h) - ncdf(0.7 - h)) / (2 * h) == pytest.approx(npdf(0.7), rel=1e-8)


def test_npdf_tails_and_bad_input():
    assert npdf(40.0) == 0.0  # exp(-800) underflows to zero, no error
    assert npdf(float("inf")) == 0.0
    assert math.isnan(npdf(float("nan")))
    with pytest.raises(TypeError):
        npdf(None)  # type: ignore[arg-type]


# --- pm_gap_report.primary_early_payout -----------------------------------------------------------------------------


def _pm(mid, src: str, clv: float | None, pnl: float | None, day: str, ts: int = T, week: str = "2026-W45") -> dict:
    return {"market_id": mid, "src_24h": src, "clv_24h": clv, "pnl": pnl, "ts": ts, "week_entry": week, "day": day}


def test_primary_early_payout_hand_values():
    pm = [
        _pm("m1", "ok", 0.10, 0.40, "d1"),  # closed exactly at entry + 24 h with a result: early, value = pnl 0.40
        _pm("m2", "missing", None, -0.30, "d1"),  # closed at entry + 10 h with a result: early (and missing), -0.30
        _pm("m3", "ok", 0.05, 0.60, "d2", ts=T + 5 * H),  # closed 1 s after its own +24 h: keeps clv 0.05
        _pm("m4", "missing", None, None, "d3"),  # no close time stored, no result: stays missing, without close_ts
        _pm("m5", "missing", None, 0.70, "d3"),  # closed after +24 h: stays missing (has a close time)
        _pm("m6", "ok", -0.02, None, "d4"),  # closed early but no settled result: keeps clv -0.02
        _pm("m7", "n/a", None, 0.90, "d5"),  # entered < 26 h before the end: out of both variants
    ]
    outcomes = [
        {"market_id": "m1", "closed_ts": T + 24 * H},
        {"market_id": "m2", "closed_ts": T + 10 * H},
        {"market_id": "m3", "closed_ts": T + 5 * H + 24 * H + 1},
        {"market_id": "m4", "closed_ts": None},
        {"market_id": "m5", "closed_ts": T + 30 * H},
        {"market_id": "m6", "closed_ts": T + 2 * H},
        {"market_id": "m7", "closed_ts": T + H},
        {"market_id": "m9"},  # an outcome row without the column (before schema 38)
    ]
    out = primary_early_payout(pm, outcomes)
    assert out["sources"] == {"missing": 3, "n/a": 1, "ok": 3}
    assert list(out["sources"]) == ["missing", "n/a", "ok"]  # sorted by status
    assert out["defined"] == 6  # ok + missing; n/a excluded
    assert out["early_resolved"] == 2
    assert out["early_resolved_share"] == pytest.approx(2 / 6)
    assert out["early_resolved_missing"] == 1
    assert out["missing_left"] == 2
    assert out["missing_without_close_ts"] == 1
    # Second-variant +24 h CLV: [0.40, -0.30, 0.05, -0.02] -> 0.13 / 4 = 0.0325; one entry week -> no CI.
    s = out["clv_24h"]
    assert (s["n"], s["n_clusters"], s["n_days"]) == (4, 1, 3)
    assert s["mean"] == pytest.approx(0.0325, abs=1e-12)
    assert s["lo"] is None and s["hi"] is None


def test_primary_early_payout_ids_and_timestamps_are_coerced():
    # Integer market ids match string ids of the outcome rows; a string closed_ts is read as an int.
    pm = [_pm(7, "ok", 0.10, -0.50, "d1"), _pm(8, "ok", 0.20, 0.50, "d2", week="2026-W46")]
    out = primary_early_payout(pm, [{"market_id": "7", "closed_ts": str(T + 24 * H)}])
    assert out["early_resolved"] == 1
    # Values [-0.50, 0.20] in two entry weeks -> mean -0.15 and a bootstrap CI within the observed range.
    s = out["clv_24h"]
    assert s["n"] == 2 and s["n_clusters"] == 2 and s["mean"] == pytest.approx(-0.15, abs=1e-12)
    assert -0.50 - 1e-12 <= s["lo"] <= s["mean"] <= s["hi"] <= 0.20 + 1e-12


def test_primary_early_payout_without_close_times_and_empty():
    pm = [_pm("a", "ok", 0.10, 0.40, "d1"), _pm("b", "ok", -0.04, 0.0, "d2"), _pm("c", "missing", None, 1.0, "d2")]
    out = primary_early_payout(pm, [])  # nothing recorded yet: every position keeps its first-variant value
    assert out["early_resolved"] == 0 and out["early_resolved_share"] == 0.0
    assert out["missing_left"] == 1 and out["missing_without_close_ts"] == 1
    assert out["clv_24h"]["n"] == 2 and out["clv_24h"]["mean"] == pytest.approx(0.03, abs=1e-12)  # (0.10 - 0.04) / 2
    empty = primary_early_payout([], [{"market_id": "a", "closed_ts": T}])
    assert empty["defined"] == 0 and empty["early_resolved_share"] is None and empty["sources"] == {}
    assert empty["clv_24h"]["n"] == 0 and empty["clv_24h"]["mean"] is None


# --- hull_suite_flip.wma --------------------------------------------------------------------------------------------


def test_wma_hand_values():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    out = wma(x, 3, np.arange(5))
    # Weights 3 (newest), 2, 1 over 6: i = 2 -> (3*3 + 2*2 + 1*1) / 6 = 14/6; i = 3 -> 20/6; i = 4 -> 26/6.
    assert np.isnan(out[:2]).all()
    assert out[2:] == pytest.approx([14 / 6, 20 / 6, 26 / 6], rel=1e-14)
    # n = 1 is the series itself.
    assert wma(x, 1, np.arange(5)) == pytest.approx(x)
    # A constant series has WMA equal to the constant.
    assert wma(np.full(10, 7.5), 4, np.arange(10))[3:] == pytest.approx(np.full(7, 7.5))


def test_wma_segment_restart_and_nan_window():
    # Two segments of three bars each (pos restarts at the gap): n = 3 is defined only at the third bar of each.
    x = np.array([1.0, 2.0, 3.0, 10.0, 20.0, 30.0])
    pos = np.array([0, 1, 2, 0, 1, 2])
    out = wma(x, 3, pos)
    assert np.isnan(out[[0, 1, 3, 4]]).all()
    assert out[2] == pytest.approx(14 / 6) and out[5] == pytest.approx((3 * 30 + 2 * 20 + 10) / 6)
    # A NaN input poisons exactly the windows that contain it.
    y = np.array([1.0, 2.0, np.nan, 4.0, 5.0, 6.0, 7.0])
    o = wma(y, 2, np.arange(7))
    assert np.isnan(o[[0, 2, 3]]).all()
    assert o[1] == pytest.approx((2 * 2 + 1) / 3) and o[4:] == pytest.approx([14 / 3, 17 / 3, 20 / 3])


def test_wma_short_series():
    out = wma(np.array([1.0, 2.0]), 3, np.arange(2))
    assert out.shape == (2,) and np.isnan(out).all()
    assert wma(np.array([]), 3, np.array([], dtype=int)).size == 0


# --- smart_money.mark_price -----------------------------------------------------------------------------------------


def test_mark_price_is_notional_over_size():
    # 0.5 BTC long worth $50,000 -> $100,000; a short's value may come signed, the price is still positive.
    assert mark_price({"size": 0.5, "value_usd": 50_000.0, "side": "long"}) == pytest.approx(100_000.0)
    assert mark_price({"size": 2, "value_usd": -7_000, "side": "short"}) == pytest.approx(3_500.0)
    assert mark_price({"size": "4", "value_usd": "10"}) == pytest.approx(2.5)  # numeric strings accepted


def test_mark_price_without_size_or_value():
    for p in ({}, {"size": 0}, {"size": None, "value_usd": 100.0}, {"size": 0.0, "value_usd": 100.0}):
        assert mark_price(p) is None
    assert mark_price({"size": 3.0}) == 0.0  # no value: price 0, not None
    assert mark_price({"size": 3.0, "value_usd": None}) == 0.0
    with pytest.raises(ValueError):
        mark_price({"size": "abc", "value_usd": 1.0})
