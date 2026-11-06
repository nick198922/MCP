"""Direct tests for ten analytics helpers that had no mention in tests/ (BACKLOG 6.9, card item 177).

They feed the assistant's paper journal (regime hysteresis of `rec_method`, strike roles of `option_replay`), the radar
archive and its snapshot view (`radar`), the put-skew coverage rule (`put_skew.wing_put_prints`), the hourly flow
snapshot journal (`flow_snapshot`) and the wing block of the options tools (`options.wing_quality`). References are
hand-computed on synthetic data in the comments; surfaces carry explicit marks and deltas, so no Black-76 output is
reused as an expected value.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics.flow_snapshot import DEPTH_KEYS, fnum, merge_depth
from tradedesk.analytics.option_replay import strike_by_delta
from tradedesk.analytics.options import OptionRow, Smile, wing_quality
from tradedesk.analytics.put_skew import wing_put_prints
from tradedesk.analytics.radar import pick_rows, ref_ms, smile_at
from tradedesk.analytics.rec_method import bucket, regime_confirmed, regime_label

DAY = 86_400_000

# ------------------------------------------------------------------ rec_method.regime_label / regime_confirmed


def test_regime_label_names_the_rv30_bucket() -> None:
    assert regime_label(None) is None
    assert regime_label(0) == "rv30_b0"
    assert regime_label(3) == "rv30_b3"
    # buckets: <25 %, 25-40 %, 40-60 %, >=60 %; RV30 = 30 % is in bucket 1
    assert regime_label(bucket(0.30)) == "rv30_b1"


def test_regime_confirmed_needs_a_new_bucket() -> None:
    assert regime_confirmed("rv30_b1", None, 0.50, None) is False
    assert regime_confirmed("rv30_b1", "rv30_b1", 0.50, "rv30_b2") is False


def test_regime_confirmed_two_days_in_a_row_skip_the_margin() -> None:
    # 0.405 is only 0.5 vol points above the 40 % edge, but yesterday's decision was already in bucket 2
    assert regime_confirmed("rv30_b1", "rv30_b2", 0.405, "rv30_b2") is True
    assert regime_confirmed("rv30_b1", "rv30_b2", 0.405, "rv30_b1") is False


def test_regime_confirmed_margin_up_and_down() -> None:
    # up from b1 (25-40 %): RV30 must be >= 0.40 + 0.02
    assert regime_confirmed("rv30_b1", "rv30_b2", 0.43, None) is True  # 3 points above the edge
    assert regime_confirmed("rv30_b1", "rv30_b2", 0.415, None) is False  # 1.5 points
    # down from b2 (40-60 %): RV30 must be <= 0.40 - 0.02
    assert regime_confirmed("rv30_b2", "rv30_b1", 0.37, None) is True
    assert regime_confirmed("rv30_b2", "rv30_b1", 0.39, None) is False
    # a jump over a whole bucket is measured from the old bucket's edge (0.25): 0.41 - 0.25 = 0.16 >= 0.02
    assert regime_confirmed("rv30_b0", "rv30_b2", 0.41, None) is True


def test_regime_confirmed_does_not_suppress_when_the_margin_is_unknown() -> None:
    assert regime_confirmed(None, "rv30_b2", 0.41, None) is True  # no standing regime
    assert regime_confirmed("legacy", "rv30_b2", 0.41, None) is True  # unparsable label
    assert regime_confirmed("rv30_bx", "rv30_b2", 0.41, None) is True
    assert regime_confirmed("rv30_b1", "rv30_b2", None, None) is True  # no RV30


# ------------------------------------------------------------------ option_replay.strike_by_delta


def _exp(points: dict[float, tuple[float, float]], forward: float = 100.0) -> dict:
    # {strike: (put delta, mark iv)} -> one expiry of a put_skew.build_day surface
    return {"F": forward, "strikes": {k: {"put_delta": pd, "mark_iv": iv} for k, (pd, iv) in points.items()}}


def test_strike_by_delta_picks_closest_delta_on_its_side_of_the_forward() -> None:
    exp = _exp(
        {
            80.0: (-0.07, 0.70),
            85.0: (-0.14, 0.66),
            90.0: (-0.24, 0.60),
            95.0: (-0.30, 0.57),
            100.0: (-0.50, 0.55),
            105.0: (-0.70, 0.54),  # call delta 0.30
            110.0: (-0.76, 0.53),  # call delta 0.24
            120.0: (-0.92, 0.56),  # call delta 0.08
        }
    )
    # 25-delta put, band [0.18, 0.32]: |0.24 - 0.25| = 0.01 beats |0.30 - 0.25| = 0.05
    assert strike_by_delta(exp, 0.25, False, (0.18, 0.32)) == (90.0, 0.60)
    # 25-delta call: call delta = 1 + put delta; 0.24 (K=110) beats 0.30 (K=105)
    assert strike_by_delta(exp, 0.25, True, (0.18, 0.32)) == (110.0, 0.53)
    # 10-delta put, band [0.05, 0.15]: 0.07 (diff 0.03) beats 0.14 (diff 0.04)
    assert strike_by_delta(exp, 0.10, False, (0.05, 0.15)) == (80.0, 0.70)
    assert strike_by_delta(exp, 0.10, True, (0.05, 0.15)) == (120.0, 0.56)
    # nothing in the band -> None
    assert strike_by_delta(exp, 0.40, False, (0.38, 0.45)) is None


def test_strike_by_delta_ignores_the_wrong_side_and_ties_to_the_lower_strike() -> None:
    # K=95 has call delta 0.25 exactly but sits below the forward: a call is never taken there
    exp = _exp({95.0: (-0.75, 0.60), 100.0: (-0.50, 0.55), 108.0: (-0.80, 0.52)})
    assert strike_by_delta(exp, 0.25, True, (0.18, 0.32)) == (108.0, 0.52)
    # K=105 has |put delta| 0.25 but sits above the forward: a put is never taken there
    exp = _exp({92.0: (-0.20, 0.61), 105.0: (-0.25, 0.54)})
    assert strike_by_delta(exp, 0.25, False, (0.18, 0.32)) == (92.0, 0.61)
    # equal delta distance -> the lower strike (key = (distance, strike))
    exp = _exp({88.0: (-0.25, 0.63), 90.0: (-0.25, 0.60)})
    assert strike_by_delta(exp, 0.25, False, (0.18, 0.32)) == (88.0, 0.63)


# ------------------------------------------------------------------ put_skew.wing_put_prints


def test_wing_put_prints_sums_put_prints_in_the_delta_band_inclusive() -> None:
    x = {
        "strikes": {
            85.0: {"put_delta": -0.10, "n_put": 9},  # out
            88.0: {"put_delta": -0.20, "n_put": 3},  # lower edge: in
            90.0: {"put_delta": -0.25, "n_put": 4},
            93.0: {"put_delta": -0.30, "n_put": 5},  # upper edge: in
            96.0: {"put_delta": -0.35, "n_put": 7},  # out
        }
    }
    assert wing_put_prints(x, (0.20, 0.30)) == 3 + 4 + 5
    assert wing_put_prints(x, (0.40, 0.60)) == 0
    assert wing_put_prints({"strikes": {}}, (0.20, 0.30)) == 0


# ------------------------------------------------------------------ radar.ref_ms / smile_at / pick_rows


def test_ref_ms_is_19_utc_of_the_day() -> None:
    day = int(datetime(2026, 11, 6, tzinfo=UTC).timestamp() * 1000)
    assert ref_ms(day) == int(datetime(2026, 11, 6, 19, tzinfo=UTC).timestamp() * 1000)
    assert ref_ms(day) - day == 19 * 3_600_000


# label -> (strike, put delta); ATM is K = F = 100 (marks and deltas are set by hand, not from Black-76)
_SMILE_GRID = {
    "10P": (80.0, -0.10),
    "25P": (90.0, -0.25),
    "35P": (95.0, -0.35),
    "ATM": (100.0, -0.50),
    "35C": (105.0, -0.65),
    "25C": (110.0, -0.75),
    "10C": (120.0, -0.90),
}
_IV_20D = {"10P": 0.80, "25P": 0.70, "35P": 0.65, "ATM": 0.60, "35C": 0.58, "25C": 0.57, "10C": 0.60}
_IV_40D = {"10P": 0.60, "25P": 0.50, "35P": 0.45, "ATM": 0.40, "35C": 0.38, "25C": 0.37, "10C": 0.40}


def _surface_exp(days: float, ivs: dict[str, float]) -> dict:
    strikes = {k: {"put_delta": pd, "mark_iv": ivs[label]} for label, (k, pd) in _SMILE_GRID.items()}
    return {"F": 100.0, "T": days / 365.0, "strikes": strikes}


def test_smile_at_interpolates_total_variance_between_bracketing_expiries() -> None:
    snap = {"expiries": {1: _surface_exp(20, _IV_20D), 2: _surface_exp(40, _IV_40D)}}
    out = smile_at(snap)  # 30-day tenor
    assert list(out) == list(_SMILE_GRID)
    for label in _SMILE_GRID:
        v0, v1 = _IV_20D[label], _IV_40D[label]
        # w(30) = v0^2 * 20 + (v1^2 * 40 - v0^2 * 20) * (30 - 20) / (40 - 20) = 10 v0^2 + 20 v1^2  (in days)
        # iv = sqrt(w / 30) = sqrt((v0^2 + 2 v1^2) / 3)
        assert out[label] == pytest.approx(math.sqrt((v0 * v0 + 2 * v1 * v1) / 3), rel=1e-12)
    assert out["ATM"] == pytest.approx(math.sqrt((0.36 + 0.32) / 3), rel=1e-12)  # 0.47610...


def test_smile_at_exact_expiry_and_unbracketed_tenor() -> None:
    snap = {"expiries": {1: _surface_exp(20, _IV_20D), 2: _surface_exp(40, _IV_40D)}}
    # a tenor equal to an expiry: that expiry's own marks
    assert smile_at(snap, 40) == pytest.approx(_IV_40D, rel=1e-12)
    # beyond the last expiry or before the first: no extrapolation
    assert smile_at(snap, 50) == dict.fromkeys(_SMILE_GRID)
    assert smile_at(snap, 10) == dict.fromkeys(_SMILE_GRID)


def _archive_row(day: str, ts: int, atm30: float) -> dict:
    # the Store.radar_list shape (atm_7d ... atm_180d columns)
    return {"day": day, "ts": ts, "spot": 70_000.0, "atm_30d": atm30, "put_skew_25": 0.03, "rr_25": -0.02}


def test_pick_rows_newest_due_row_and_the_one_a_week_earlier() -> None:
    rows = [
        _archive_row("2026-10-29", 1, 0.41),
        _archive_row("2026-11-04", 2, 0.42),
        _archive_row("2026-11-05", 3, 0.43),
        _archive_row("2026-11-07", 4, 0.99),  # after the latest due day: ignored
        {"ts": 5},  # malformed (no day): skipped
        {"day": "2026-13-01", "ts": 6},  # malformed date: skipped
    ]
    missed: list[tuple[str, str]] = []
    # 21:00 UTC on 6.11 is past 19:00 + 60 min, so the latest due day is 6.11 and the oldest accepted 4.11
    now_row, week_row = pick_rows(rows, datetime(2026, 11, 6, 21, tzinfo=UTC), lambda f, r: missed.append((f, r)))
    assert now_row is not None and now_row["day"] == "2026-11-05"
    assert now_row["atm"]["30"] == 0.43 and now_row["atm"]["7"] is None
    assert week_row is not None and week_row["day"] == "2026-10-29"
    assert missed == []


def test_pick_rows_reports_a_stale_archive_and_a_missing_week_row() -> None:
    missed: list[tuple[str, str]] = []
    # 19:30 UTC on 6.11 is inside the 60-minute lag: the latest due day is 5.11, the oldest accepted 3.11
    now = datetime(2026, 11, 6, 19, 30, tzinfo=UTC)
    assert pick_rows([_archive_row("2026-11-01", 1, 0.4)], now, lambda f, r: missed.append((f, r))) == (None, None)
    assert missed == [("surface", "no radar archive row from 2026-11-03 to 2026-11-05 (job radar_archive off or behind)")]
    missed.clear()
    # the 6.11 row is not yet due at 19:30, so «now» is 4.11 and the week row 28.10 is absent
    now_row, week_row = pick_rows(
        [_archive_row("2026-11-04", 1, 0.4), _archive_row("2026-11-06", 2, 0.5)], now, lambda f, r: missed.append((f, r))
    )
    assert now_row is not None and now_row["day"] == "2026-11-04"
    assert week_row is None
    assert missed == [("surface_week", "no radar archive row for 2026-10-28")]


# ------------------------------------------------------------------ flow_snapshot.fnum / merge_depth


def test_fnum_keeps_finite_numbers_only() -> None:
    assert fnum("1.5") == 1.5
    assert fnum("-2") == -2.0
    assert fnum(0) == 0.0
    assert fnum(3) == 3.0 and isinstance(fnum(3), float)
    for bad in (None, True, False, "abc", [1], {}, float("nan"), float("inf"), "-inf"):
        assert fnum(bad) is None, bad


def test_merge_depth_first_mid_and_band_farthest_reach() -> None:
    fine = dict.fromkeys(DEPTH_KEYS) | {"mid": 100.0, "reach_bid": 0.002, "reach_ask": 0.0015, "bid_usd_10bp": 5.0}
    coarse = dict.fromkeys(DEPTH_KEYS) | {
        "mid": 99.9,
        "reach_bid": 0.02,
        "bid_usd_10bp": 7.0,
        "bid_usd_50bp": 20.0,
        "ask_usd_100bp": 30.0,
    }
    out = merge_depth([fine, coarse])
    assert list(out) == list(DEPTH_KEYS)
    assert out["mid"] == 100.0  # the finest book's mid
    assert out["reach_bid"] == 0.02 and out["reach_ask"] == 0.0015  # farthest reach per side
    assert out["bid_usd_10bp"] == 5.0  # the first book that has the band
    assert out["bid_usd_50bp"] == 20.0 and out["ask_usd_100bp"] == 30.0
    assert out["ask_usd_10bp"] is None and out["bid_usd_100bp"] is None and out["ask_usd_50bp"] is None


def test_merge_depth_takes_mid_from_the_first_book_that_has_one() -> None:
    assert merge_depth([]) == dict.fromkeys(DEPTH_KEYS)
    out = merge_depth([dict.fromkeys(DEPTH_KEYS), {"mid": 50.0, "reach_bid": 0.01}])
    assert out["mid"] == 50.0 and out["reach_bid"] == 0.01 and out["reach_ask"] is None


# ------------------------------------------------------------------ options.wing_quality


def _row(strike: float, is_call: bool, delta: float | None, oi: float, spread: float | None, quoted: bool = True) -> OptionRow:
    return OptionRow(
        instrument=f"BTC-X-{int(strike)}-{'C' if is_call else 'P'}",
        expiry=datetime(2027, 2, 5, 8, tzinfo=UTC),
        strike=strike,
        is_call=is_call,
        iv=0.5,
        forward=100.0,
        index=100.0,
        T=0.25,
        mark_price_native=None,
        mark_price_usd=None,
        open_interest=oi,
        volume=0.0,
        bid_price_native=0.01 if quoted else None,
        ask_price_native=0.012,
        spread_pct=spread,
        delta=delta,
    )


def test_wing_quality_reports_the_strikes_nearest_the_wing_deltas() -> None:
    # flat 50 % smile, T = 0.25, F = 100: call deltas N(d1) run from 0.94 (K=70) to 0.18 (K=130), covering 0.25 and 0.75
    smile = Smile(100.0, 0.25, [70.0, 100.0, 130.0], [0.5, 0.5, 0.5])
    rows = [
        _row(105.0, True, 0.40, 10.0, 2.0),
        _row(110.0, True, 0.27, 12.0, 3.0),  # |0.27 - 0.25| = 0.02: the call wing
        _row(115.0, True, 0.20, 8.0, 4.0),  # 0.05
        _row(112.0, True, None, 99.0, 1.0),  # no delta: skipped
        _row(95.0, False, -0.30, 6.0, 5.0),  # call-equivalent 0.70: |0.70 - 0.75| = 0.05
        _row(90.0, False, -0.22, 7.0, None, quoted=False),  # 0.78: 0.03, the put wing
    ]
    out = wing_quality(rows, smile, 0.25)
    assert out == {
        "call": {"strike": 110.0, "oi": 12.0, "spread_pct": 3.0, "iv": 0.5, "quoted_both_sides": True},
        "put": {"strike": 90.0, "oi": 7.0, "spread_pct": None, "iv": 0.5, "quoted_both_sides": False},
    }
    # no put rows at all: the put side is None, the call side stays
    calls_only = wing_quality(rows[:4], smile, 0.25)
    assert calls_only is not None and calls_only["put"] is None and calls_only["call"]["strike"] == 110.0


def test_wing_quality_none_when_the_smile_does_not_reach_the_delta() -> None:
    # strikes 98-102 at 50 %: call deltas only 0.52-0.58, neither 0.25 nor 0.75 is quoted
    smile = Smile(100.0, 0.25, [98.0, 100.0, 102.0], [0.5, 0.5, 0.5])
    assert wing_quality([_row(110.0, True, 0.27, 1.0, 1.0)], smile, 0.25) is None
