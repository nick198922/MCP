"""Direct tests for eleven analytics helpers that had no mention in tests/ (BACKLOG 6.9, card item 199).

They feed the numbers of research reports and the trade-plan journal: the S12 H2 early entry's hour lookup
(`s12_modules.touch_hours`, `first_touch_hour`), the S16 H7 time exit's window high (`s16_modules.window_high`), the
session stop-hunt's BOS fractal (`session_stop_hunt.last_fractal_low`), the fixed-effect design of the fresh-listing
confounder regression (`fresh_confounders.dummies`, `nested_dummies`), the stored expiry of a trade plan
(`trade_plan.usable_expires`) and the indicators of three sieve specs (`bill_williams_fractal_gator.smma`,
`stochastic_full_cross.stoch`, `freqtrade_rsi_tema_bb.ema_from`, `tema`). References are hand-computed in the
comments or analytical (TEMA has no lag on a straight line); no output of the code under test is reused as an
expected value.
"""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.fresh_confounders import dummies, nested_dummies
from tradedesk.analytics.s12_modules import first_touch_hour, touch_hours
from tradedesk.analytics.s16_modules import window_high
from tradedesk.analytics.session_stop_hunt import last_fractal_low
from tradedesk.analytics.sieve_specs.bill_williams_fractal_gator import smma
from tradedesk.analytics.sieve_specs.freqtrade_rsi_tema_bb import ema_from, tema
from tradedesk.analytics.sieve_specs.stochastic_full_cross import stoch
from tradedesk.analytics.trade_plan import usable_expires

HOUR = 3_600_000


def _hours(h, low, *, t=None) -> Bars:
    """1h bars; only high and low matter here (open = close = the midpoint)."""
    h, low = np.asarray(h, float), np.asarray(low, float)
    t = np.arange(h.size, dtype=np.int64) * HOUR if t is None else np.asarray(t, dtype=np.int64) * HOUR
    mid = (h + low) / 2
    return Bars(t, mid, h, low, mid.copy(), HOUR)


# ------------------------------------------------------------------ s12_modules.touch_hours


def test_touch_hours_index_of_the_first_hour_only_when_all_hours_exist() -> None:
    full = _hours(np.full(8, 2.0), np.ones(8))  # hours 0 .. 7
    assert touch_hours(full, 0, 4 * HOUR) == 0
    assert touch_hours(full, 4 * HOUR, 4 * HOUR) == 4
    assert touch_hours(full, 6 * HOUR, 4 * HOUR) == -1  # only hours 6 and 7 left
    gap = _hours(np.full(8, 2.0), np.ones(8), t=[0, 1, 2, 4, 5, 6, 7, 8])  # hour 3 missing
    assert touch_hours(gap, 0, 4 * HOUR) == -1
    assert touch_hours(gap, 4 * HOUR, 4 * HOUR) == 3  # an index into the path, not an hour count
    assert touch_hours(gap, 5 * HOUR, 4 * HOUR) == 4  # hours 5 .. 8


# ------------------------------------------------------------------ s12_modules.first_touch_hour


def test_first_touch_hour_first_low_at_or_below_mid_inside_the_bar() -> None:
    path = _hours(np.full(8, 10.0), [5.0, 9.0, 9.0, 9.0, 9.0, 8.5, 8.0, 7.0])
    assert first_touch_hour(path, 4, 4, 8.0) == 6  # low 8.0 == mid counts
    assert first_touch_hour(path, 4, 4, 7.5) == 7
    assert first_touch_hour(path, 4, 4, 9.0) == 4
    assert first_touch_hour(path, 4, 4, 6.0) == -1  # the low 5 of hour 0 is outside the bar
    assert first_touch_hour(path, 4, 2, 7.5) == -1  # only hours 4 and 5


# ------------------------------------------------------------------ s16_modules.window_high


def test_window_high_over_bars_opening_in_half_open_window() -> None:
    path = _hours([1.0, 5.0, 3.0, 4.0, 9.0, 6.0], np.zeros(6))
    assert window_high(path, HOUR, 4 * HOUR) == 5.0  # bars 1, 2, 3; bar 4 opens at t1
    assert window_high(path, HOUR, 4 * HOUR + 1) == 9.0  # bar 4 opens before t1
    assert window_high(path, HOUR + HOUR // 2, 4 * HOUR) == 4.0  # bars 2, 3
    assert math.isnan(window_high(path, 2 * HOUR, 2 * HOUR))
    assert math.isnan(window_high(path, 6 * HOUR, 9 * HOUR))


# ------------------------------------------------------------------ session_stop_hunt.last_fractal_low


def test_last_fractal_low_before_the_extreme_confirmed_by_n_right_bars() -> None:
    fr = np.array([3, 10, 15], dtype=np.int64)
    assert last_fractal_low(fr, 16, 17) == 15  # 15 + 2 <= 17
    assert last_fractal_low(fr, 16, 16) == 10  # 15 needs bar 17 to close its right side
    assert last_fractal_low(fr, 15, 100) == 10  # strictly before the extreme
    assert last_fractal_low(fr, 15, 100, floor=10) == 10
    assert last_fractal_low(fr, 15, 100, floor=11) is None  # the last one is below the floor
    assert last_fractal_low(fr, 3, 100) is None
    assert last_fractal_low(fr, 16, 15, n=0) == 15
    assert last_fractal_low(np.zeros(0, dtype=np.int64), 16, 17) is None


# ------------------------------------------------------------------ fresh_confounders.dummies / nested_dummies


def test_dummies_one_column_per_level_but_the_first_sorted() -> None:
    np.testing.assert_array_equal(dummies(["b", "a", "c", "a"]), [[1, 0], [0, 0], [0, 1], [0, 0]])
    np.testing.assert_array_equal(dummies([2, 1, 2]), [[1], [0], [1]])
    assert dummies(["x", "x", "x"]).shape == (3, 0)


def test_nested_dummies_baseline_is_the_lowest_code_present_in_each_group() -> None:
    groups = ["1h", "1h", "1h", "4h", "4h"]
    codes = [0, 1, 2, 1, 2]  # 4h has no bin 0: its baseline is bin 1
    nd = nested_dummies(groups, codes)
    np.testing.assert_array_equal(nd, [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 0], [0, 0, 1]])
    # with the intercept and the group dummy the design has full column rank (no collinearity)
    x = np.column_stack([np.ones(5), dummies(groups), nd])
    assert np.linalg.matrix_rank(x) == x.shape[1] == 5
    assert nested_dummies(["a", "b"], [3, 4]).shape == (2, 0)


# ------------------------------------------------------------------ trade_plan.usable_expires


def test_usable_expires_int_seconds_or_none_for_non_numbers() -> None:
    assert usable_expires(1_700_000_000) == 1_700_000_000
    assert usable_expires(1_700_000_000.9) == 1_700_000_000  # truncated like int()
    assert usable_expires(np.float64(1.7e9)) == 1_700_000_000
    for bad in ("1700000000", b"1700000000", None, True, math.nan, math.inf, [1]):
        assert usable_expires(bad) is None


# ------------------------------------------------------------------ bill_williams_fractal_gator.smma


def test_smma_seeded_with_sma_then_wilder_smoothing_per_segment() -> None:
    # n = 3: seed mean(1, 2, 3) = 2, then (2 * 2 + 4) / 3 = 8/3, (8/3 * 2 + 5) / 3 = 31/9
    out = smma(np.array([1.0, 2.0, 3.0, 4.0, 5.0]), 3, np.arange(5))
    np.testing.assert_allclose(out, [np.nan, np.nan, 2.0, 8 / 3, 31 / 9], rtol=1e-12)
    # a new segment at bar 3 forgets the old one: seed mean(10, 20, 30) = 20, then (20 * 2 + 40) / 3
    out = smma(np.array([1.0, 2.0, 3.0, 10.0, 20.0, 30.0, 40.0]), 3, np.array([0, 1, 2, 0, 1, 2, 3]))
    np.testing.assert_allclose(out, [np.nan, np.nan, 2.0, np.nan, np.nan, 20.0, 80 / 3], rtol=1e-12)


# ------------------------------------------------------------------ stochastic_full_cross.stoch


def _stoch_bars() -> Bars:
    h = [11.0, 12.0, 13.0, 12.0, 14.0]
    low = [9.0, 10.0, 10.0, 8.0, 9.0]
    c = [10.0, 11.0, 12.0, 9.0, 13.0]
    return Bars(np.arange(5, dtype=np.int64) * HOUR, np.array(c), np.array(h), np.array(low), np.array(c), HOUR)


def test_stoch_full_k_and_d_by_hand() -> None:
    # n = 2 fast %K: bar 1 (11 - 9) / 3 = 200/3 %, bar 2 (12 - 10) / 3 = 200/3, bar 3 (9 - 8) / 5 = 20, bar 4 (13 - 8) / 6
    # = 250/3; %K = 2-bar mean of fast from bar 2, %D = 2-bar mean of %K from bar 3
    k, d = stoch(_stoch_bars(), 2, 2, 2, np.arange(5))
    np.testing.assert_allclose(k, [np.nan, np.nan, 200 / 3, 130 / 3, 155 / 3], rtol=1e-12)
    np.testing.assert_allclose(d, [np.nan, np.nan, np.nan, 55.0, 47.5], rtol=1e-12)


def test_stoch_mirrored_is_100_minus_k_and_flat_window_is_nan() -> None:
    k, _ = stoch(_stoch_bars(), 2, 2, 2, np.arange(5))
    km, _ = stoch(_stoch_bars().mirrored(), 2, 2, 2, np.arange(5))
    np.testing.assert_allclose(km, 100.0 - k, rtol=1e-12)  # the short side of the spec reads the mirrored bars
    flat = np.full(4, 50.0)
    k, d = stoch(Bars(np.arange(4, dtype=np.int64) * HOUR, flat, flat, flat, flat, HOUR), 2, 1, 1, np.arange(4))
    assert np.isnan(k).all() and np.isnan(d).all()  # highest high == lowest low


# ------------------------------------------------------------------ freqtrade_rsi_tema_bb.ema_from / tema


def test_ema_from_seeds_at_skip_and_lags_a_line_by_n_minus_1_over_2() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    # n = 2, k = 2/3: seed mean(1, 2) = 1.5, then 3 * 2/3 + 1.5 / 3 = 2.5, ...: the line minus (n - 1) / 2
    np.testing.assert_allclose(ema_from(x, 2, np.arange(6), 0), [np.nan, 1.5, 2.5, 3.5, 4.5, 5.5], rtol=1e-12)
    # skip 2: the first two values are ignored, seed mean(3, 4) = 3.5 at position 3
    np.testing.assert_allclose(ema_from(x, 2, np.arange(6), 2), [np.nan, np.nan, np.nan, 3.5, 4.5, 5.5], rtol=1e-12)


def test_tema_has_no_lag_on_a_line_and_matches_a_hand_step() -> None:
    # e1 = c - L, e2 = c - 2L, e3 = c - 3L on a line (SMA seed = steady-state lag L), so 3e1 - 3e2 + e3 = c exactly;
    # the first value is at segment position 3 (n - 1)
    c = 10.0 + 2.0 * np.arange(10)
    out = tema(c, 3, np.arange(10))
    assert np.isnan(out[:6]).all()
    np.testing.assert_allclose(out[6:], c[6:], rtol=1e-12)
    # two segments restart independently (n = 2: first value at position 3)
    c2 = np.array([10.0, 12.0, 14.0, 16.0, 100.0, 101.0, 102.0, 103.0])
    out = tema(c2, 2, np.array([0, 1, 2, 3, 0, 1, 2, 3]))
    np.testing.assert_allclose(out, [np.nan] * 3 + [16.0] + [np.nan] * 3 + [103.0], rtol=1e-12)
    # a step 0, 0, 0, 3, 3 with n = 2 (k = 2/3): e1 = 2, 8/3; e2 = 4/3, 20/9; e3 = 2/3, 46/27 at bars 3, 4
    out = tema(np.array([0.0, 0.0, 0.0, 3.0, 3.0]), 2, np.arange(5))
    np.testing.assert_allclose(out, [np.nan, np.nan, np.nan, 8 / 3, 82 / 27], rtol=1e-12)
