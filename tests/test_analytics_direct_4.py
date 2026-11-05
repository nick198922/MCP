"""Direct tests for ten small analytics helpers that had no mention in tests/ (BACKLOG 6.9, card item 126).

References are hand-computed on synthetic data; edge cases follow each function.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics.flow_model import accuracy, lag_diff, predict_linear, r2_oos, standardize
from tradedesk.analytics.funding_crowding import ols_beta
from tradedesk.analytics.funding_settlement import scale_factor
from tradedesk.analytics.instruments import parse_expiry_token
from tradedesk.analytics.strangle_manage import tau_years
from tradedesk.analytics.xs_factors import two_sided_p

DAY_MS = 86_400_000

# ------------------------------------------------------------------ flow_model


def test_lag_diff_values_and_nan_head() -> None:
    out = lag_diff(np.array([1.0, 4.0, 9.0, 16.0]), 2)
    assert np.isnan(out[:2]).all()
    assert out[2:].tolist() == [8.0, 12.0]  # 9-1, 16-4


def test_lag_diff_lag_one_and_short_input() -> None:
    assert lag_diff([5, 7, 4], 1)[1:].tolist() == [2.0, -3.0]
    # lag >= length: nothing to difference, all NaN
    assert np.isnan(lag_diff(np.array([1.0, 2.0]), 2)).all()
    assert lag_diff(np.array([]), 1).size == 0


def test_standardize_uses_train_moments_and_guards_zero_variance() -> None:
    train = np.array([[1.0, 10.0], [3.0, 10.0]])
    other = np.array([[2.0, 12.0]])
    tr, ot = standardize(train, other)
    # col 0: mu 2, sd 1; col 1: constant, sd falls back to 1 (mu 10)
    assert tr.tolist() == [[-1.0, 0.0], [1.0, 0.0]]
    assert ot.tolist() == [[0.0, 2.0]]


def test_standardize_without_others_returns_only_train() -> None:
    out = standardize(np.array([[0.0], [2.0]]))
    assert len(out) == 1
    assert out[0].ravel().tolist() == [-1.0, 1.0]


def test_predict_linear_intercept_is_last_weight() -> None:
    w = np.array([2.0, -1.0, 0.5])
    X = np.array([[1.0, 2.0], [3.0, 4.0]])
    # 2*1 - 2 + 0.5 = 0.5 ; 2*3 - 4 + 0.5 = 2.5
    assert predict_linear(w, X).tolist() == [0.5, 2.5]


def test_r2_oos_reference_points() -> None:
    y = np.array([1.0, 2.0, 3.0])
    assert r2_oos(y, y) == pytest.approx(1.0)
    assert r2_oos(y, np.full(3, 2.0)) == pytest.approx(0.0)  # predicting the mean
    # sse = 1, sst = 2
    assert r2_oos(y, np.array([1.0, 2.0, 4.0])) == pytest.approx(0.5)
    # worse than the mean goes negative: sse = 8, sst = 2 -> -3
    assert r2_oos(y, np.array([3.0, 2.0, 1.0])) == pytest.approx(-3.0)
    assert r2_oos(y, np.array([3.0, 4.0, 5.0])) == pytest.approx(-5.0)  # sse 12


def test_r2_oos_constant_target_is_nan() -> None:
    assert math.isnan(r2_oos(np.array([2.0, 2.0, 2.0]), np.array([1.0, 2.0, 3.0])))


def test_accuracy_threshold_is_inclusive_at_half() -> None:
    y = np.array([1, 0, 1, 0])
    p = np.array([0.5, 0.4, 0.3, 0.6])
    # predicted 1,0,0,1 vs actual 1,0,1,0 -> 2 of 4
    assert accuracy(y, p) == pytest.approx(0.5)
    assert accuracy(np.array([1, 0]), np.array([0.5, 0.49])) == 1.0
    assert accuracy(np.array([1, 1]), np.array([0.1, 0.2])) == 0.0


# ------------------------------------------------------------- funding_settlement


def test_scale_factor_modes() -> None:
    assert scale_factor("scaled", 4) == 0.5
    assert scale_factor("scaled", 1) == 0.125
    assert scale_factor("scaled", 8) == 1.0
    assert scale_factor("unscaled", 4) == 1.0
    assert scale_factor("anything-else", 4) == 1.0


# ------------------------------------------------------------- funding_crowding


def test_ols_beta_slope_ignores_intercept() -> None:
    assert ols_beta([2.0, 4.0, 6.0], [1.0, 2.0, 3.0]) == pytest.approx(2.0)
    assert ols_beta([5.0, 7.0, 9.0], [1.0, 2.0, 3.0]) == pytest.approx(2.0)
    assert ols_beta([3.0, 2.0, 1.0], [1.0, 2.0, 3.0]) == pytest.approx(-1.0)
    # xs=[0,1,2], ys=[0,2,1]: sxy = (-1)(-1)+0+(1)(0)=1, sxx=2 -> 0.5
    assert ols_beta([0.0, 2.0, 1.0], [0.0, 1.0, 2.0]) == pytest.approx(0.5)


def test_ols_beta_constant_x_is_zero() -> None:
    assert ols_beta([1.0, 2.0, 3.0], [4.0, 4.0, 4.0]) == 0.0


def test_ols_beta_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        ols_beta([1.0, 2.0, 3.0], [1.0, 2.0, 4.0, 5.0])


# ------------------------------------------------------------------ xs_factors


def test_two_sided_p_reference_values() -> None:
    assert two_sided_p(0.0) == pytest.approx(1.0)
    assert two_sided_p(1.0) == pytest.approx(0.3173105078629141)
    assert two_sided_p(1.959964) == pytest.approx(0.05, abs=1e-6)
    assert two_sided_p(-1.0) == two_sided_p(1.0)
    assert two_sided_p(40.0) == pytest.approx(0.0, abs=1e-300)


# --------------------------------------------------------------- strangle_manage


def test_tau_years_is_days_over_365() -> None:
    assert tau_years(365 * DAY_MS, 0) == pytest.approx(1.0)
    assert tau_years(100 * DAY_MS + 73 * DAY_MS, 100 * DAY_MS) == pytest.approx(0.2)


def test_tau_years_clamped_at_zero_after_expiry() -> None:
    assert tau_years(1_000, 1_000) == 0.0
    assert tau_years(1_000, 5_000) == 0.0


# ------------------------------------------------------------------ instruments


def test_parse_expiry_token_is_0800_utc() -> None:
    assert parse_expiry_token("26MAR27") == datetime(2027, 3, 26, 8, tzinfo=UTC)
    assert parse_expiry_token("5JAN26") == datetime(2026, 1, 5, 8, tzinfo=UTC)
    assert parse_expiry_token("05JAN26") == datetime(2026, 1, 5, 8, tzinfo=UTC)


def test_parse_expiry_token_case_insensitive_and_leap_day() -> None:
    assert parse_expiry_token("29feb28") == datetime(2028, 2, 29, 8, tzinfo=UTC)


@pytest.mark.parametrize("bad", ["", "2026", "1XXX26", "31FEB26", "29FEB27", "0JAN26", "26MAR2027", "26-MAR-27"])
def test_parse_expiry_token_rejects_malformed(bad: str) -> None:
    assert parse_expiry_token(bad) is None
