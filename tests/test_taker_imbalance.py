import math

import numpy as np
import pytest

from tradedesk.analytics.taker_imbalance import (
    H_MS,
    M5_MS,
    Costs,
    block_bootstrap_mean_ci,
    cochran_q,
    fill_e2,
    funding_sum,
    imbalance,
    leg_return_e2,
    legs,
    rolling_beta,
    standardize_prior,
    window_mean,
    xs_residual,
)


def test_imbalance_values():
    out = imbalance([1.0, 3.0, 0.0, float("nan"), 1 / 3])
    assert out[0] == 0.0
    assert out[1] == pytest.approx(0.5)
    assert math.isnan(out[2]) and math.isnan(out[3])
    assert out[4] == pytest.approx(-0.5)


def _grid(n=200):
    obs = np.arange(n, dtype=np.int64) * M5_MS
    return obs, np.arange(n, dtype=float)


def test_window_mean_bounds_and_min_bars():
    obs, v = _grid()
    t = int(obs[100])
    # (t - 4h, t] = indices 53..100 -> 48 rows, mean 76.5
    assert window_mean(obs, v, t) == pytest.approx(76.5)
    v2 = v.copy()
    v2[60:90] = np.nan  # 18 usable rows left < 36
    assert math.isnan(window_mean(obs, v2, t))
    w = np.ones_like(v)
    w[100] = 3.0
    assert window_mean(obs, v, t, weights=w) == pytest.approx((76.5 * 48 - 100 + 300) / 50)


def test_window_mean_ignores_data_after_t():
    obs, v = _grid()
    t = int(obs[100])
    a = window_mean(obs, v, t)
    v2 = v.copy()
    v2[101:] = 1e9
    assert window_mean(obs, v2, t) == a
    # a row observed at t + 5 min changes nothing; the row observed exactly at t is included
    v3 = v.copy()
    v3[100] += 48.0
    assert window_mean(obs, v3, t) == pytest.approx(a + 1.0)
    assert window_mean(obs, v, t + M5_MS - 1) == a


def test_standardize_prior_uses_only_past():
    raw = np.array([1.0, -1.0] * 50 + [5.0])
    z = standardize_prior(raw, lookback=90, min_obs=60)
    sd = np.std(raw[10:100], ddof=1)
    assert z[100] == pytest.approx(5.0 / sd)
    assert np.all(np.isnan(z[:60]))
    raw2 = raw.copy()
    raw2[100] = -7.0
    z2 = standardize_prior(raw2, lookback=90, min_obs=60)
    np.testing.assert_array_equal(z[:100], z2[:100])


def test_rolling_beta_exact_and_causal():
    rng = np.random.default_rng(1)
    x = rng.normal(size=300)
    y = 2.0 * x + 0.1
    b = rolling_beta(y, x, [100, 300], window=100, min_obs=50)
    assert b == pytest.approx([2.0, 2.0])
    y2 = y.copy()
    y2[100:] = -y2[100:]
    assert rolling_beta(y2, x, [100], window=100, min_obs=50)[0] == pytest.approx(2.0)
    assert math.isnan(rolling_beta(y, x, [10], window=100, min_obs=50)[0])


def test_xs_residual_orthogonal():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert xs_residual(1 + 2 * x, x) == pytest.approx(np.zeros(5), abs=1e-12)
    s = np.array([0.3, -1.0, 2.0, 0.1, 0.5])
    e = xs_residual(s, x)
    assert float(np.dot(e, x - x.mean())) == pytest.approx(0.0, abs=1e-12)
    assert float(e.sum()) == pytest.approx(0.0, abs=1e-12)


def test_legs():
    sig = {"A": 1.0, "B": 5.0, "C": -2.0, "D": 3.0, "E": float("nan"), "F": 0.0, "G": -1.0}
    lo, sh = legs(sig, 3)
    assert set(lo) == {"B", "D", "A"} and set(sh) == {"C", "G", "F"}
    assert legs({"A": 1.0, "B": 2.0}, 3) == ([], [])


def test_costs_e1():
    c = Costs()
    assert c.fill_e1() == pytest.approx(0.00039)  # 0.7 * 2 + 0.3 * 5 + 1 = 3.9 bp
    assert c.round_trip_e1() == pytest.approx(0.00078)
    assert c.taker_round_trip() == pytest.approx(0.0012)


def test_fill_e2_adverse_selection():
    c = Costs()
    assert fill_e2(+1, 100.0, 101.0, 99.0, 100.5, c) == (100.0, c.maker)
    px, fee = fill_e2(+1, 100.0, 102.0, 100.0, 101.0, c)  # never below the open -> chase at the close
    assert px == pytest.approx(101.0 * 1.0001) and fee == c.taker
    px, fee = fill_e2(-1, 100.0, 100.0, 98.0, 99.0, c)
    assert px == pytest.approx(99.0 * 0.9999) and fee == c.taker
    # long: maker entry at 100, maker exit at 110 -> +10 % minus two maker fees
    r = leg_return_e2(+1, (100.0, 101.0, 99.0, 100.0), (110.0, 111.0, 109.0, 110.0), c)
    assert r == pytest.approx(0.10 - 2 * c.maker)
    r = leg_return_e2(-1, (100.0, 101.0, 99.0, 100.0), (110.0, 111.0, 109.0, 110.0), c)
    assert r == pytest.approx(-0.10 - 2 * c.maker)


def test_funding_sum_half_open():
    calc = np.array([0, 8, 16, 24], dtype=np.int64) * H_MS
    rates = np.array([1e-4, 2e-4, 3e-4, 4e-4])
    assert funding_sum(calc, rates, 0, 8 * H_MS) == pytest.approx(2e-4)
    assert funding_sum(calc, rates, 5 * 60_000, 8 * H_MS + 5 * 60_000) == pytest.approx(2e-4)
    assert funding_sum(calc, rates, 8 * H_MS + 1, 16 * H_MS - 1) == 0.0


def test_block_bootstrap_constant_and_coverage():
    assert block_bootstrap_mean_ci([0.5] * 40, block=4, n_boot=200) == pytest.approx((0.5, 0.5))
    rng = np.random.default_rng(3)
    x = rng.normal(0.0, 1.0, 2000)
    lo, hi = block_bootstrap_mean_ci(x, block=10, n_boot=1000)
    se = 1 / math.sqrt(2000)
    assert lo < x.mean() < hi
    assert (hi - lo) == pytest.approx(2 * 1.96 * se, rel=0.25)


def test_cochran_q():
    q, df, i2 = cochran_q([1.0, 1.0, 1.0], [0.1, 0.2, 0.3])
    assert q == pytest.approx(0.0) and df == 2 and i2 == 0.0
    q, df, i2 = cochran_q([0.0, 1.0], [0.1, 0.1])
    assert q == pytest.approx(50.0) and i2 == pytest.approx(49 / 50)
