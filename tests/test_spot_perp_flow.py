"""Spot-vs-perp taker flow divergence: archive parser with taker columns and pure analytics."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from tradedesk.analytics.spot_perp_flow import (
    cluster_bootstrap_combo,
    cluster_se_combo,
    combo_estimate,
    contrast,
    drop_top_clusters_combo,
    event_mask,
    expanding_bin_labels,
    forward_return,
    funding_sum,
    imbalance,
    last_funding,
    ols_cluster_cov,
    rolling_z,
    shuffle_within_groups,
    simple_returns,
    symbol_features,
    trade_cost,
    trailing_beta,
    trailing_mean_sd,
    week_of,
)
from tradedesk.providers.binance_vision import parse_klines_taker_zip, parse_klines_zip

FIX = Path(__file__).parent / "fixtures"


def test_parse_taker_perp_with_header_real_sample():
    blob = (FIX / "binance_vision_FTTUSDT-1d-2022-11_2026-10-14.zip").read_bytes()
    rows = parse_klines_taker_zip(blob)
    assert len(rows) == 30
    first = rows[0]
    assert first[0] == 1667260800000  # 2022-11-01 00:00 UTC
    assert first[1:5] == [26.077, 26.418, 25.52, 25.83]
    assert first[5] == pytest.approx(596839.7)
    assert first[6] == pytest.approx(15524988.8887)
    assert first[7] == pytest.approx(262044.2)
    assert first[8] == pytest.approx(6817550.7163)
    # the existing 7-column format is unchanged and agrees on the shared columns
    old = parse_klines_zip(blob)
    assert all(len(r) == 7 for r in old)
    assert [r[:7] for r in rows] == old


def test_parse_taker_spot_without_header_real_sample():
    rows = parse_klines_taker_zip((FIX / "binance_vision_ARBUSDT-1d-2024-03_sk1d_m_2026-10-15.zip").read_bytes())
    assert len(rows) == 31
    assert rows[0][0] == 1709251200000
    assert rows[0][7] == pytest.approx(28293273.0)
    assert rows[0][8] == pytest.approx(55967818.78596)
    assert all(r[0] % 86_400_000 == 0 for r in rows)
    assert all(0 <= r[7] <= r[5] for r in rows)


def test_parse_taker_spot_microseconds_real_sample():
    files = sorted(FIX.glob("binance_vision_BTCUSDT-1d-2025-03_spot_taker_*.zip"))
    assert files, "fixture saved by scripts/verify_spot_perp_flow.py --stage probe"
    rows = parse_klines_taker_zip(files[0].read_bytes())
    assert len(rows) == 31
    assert rows[0][0] == 1740787200000  # 2025-03-01 00:00 UTC, converted from microseconds
    assert all(r[0] % 86_400_000 == 0 for r in rows)
    assert all(0 < r[7] < r[5] for r in rows)


def test_imbalance_analytic():
    i = imbalance([100.0, 50.0, 0.0, float("nan")], [75.0, 25.0, 0.0, 1.0])
    assert i[0] == pytest.approx(0.5)
    assert i[1] == pytest.approx(0.0)
    assert math.isnan(i[2]) and math.isnan(i[3])


def test_returns_and_forward():
    close = [100.0, 110.0, 99.0, 99.0]
    open_ = [100.0, 101.0, 110.0, 100.0]
    r = simple_returns(close)
    assert math.isnan(r[0])
    assert r[1] == pytest.approx(0.10) and r[2] == pytest.approx(-0.10)
    f1 = forward_return(open_, close, 1)
    assert f1[0] == pytest.approx(110.0 / 101.0 - 1)  # open t+1 -> close t+1
    assert f1[2] == pytest.approx(99.0 / 100.0 - 1)
    assert math.isnan(f1[3])
    f3 = forward_return(open_, close, 3)
    assert f3[0] == pytest.approx(99.0 / 101.0 - 1)
    assert np.isnan(f3[1:]).all()


def test_trailing_stats_use_prior_days_only():
    x = np.array([1.0, 2.0, 3.0, 4.0, 100.0])
    m, s = trailing_mean_sd(x, 3, 2)
    assert math.isnan(m[0]) and math.isnan(m[1])
    assert m[2] == pytest.approx(1.5)
    assert m[4] == pytest.approx(3.0) and s[4] == pytest.approx(1.0)
    z = rolling_z(x, 3, 3)
    assert z[4] == pytest.approx((100.0 - 3.0) / 1.0)


def test_trailing_beta_exact():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.02, 200)
    y = 1.7 * x
    b = trailing_beta(y, x, 90, 60)
    assert np.isnan(b[:60]).all()
    assert b[100] == pytest.approx(1.7)


def test_features_do_not_change_when_future_data_changes():
    rng = np.random.default_rng(7)
    n = 300
    close = 100 * np.cumprod(1 + rng.normal(0, 0.03, n))
    open_ = np.r_[100.0, close[:-1]]
    d = rng.normal(0, 0.05, n)
    btc = rng.normal(0, 0.02, n)
    a = symbol_features(open_, close, d, btc)
    t = 200
    close2, d2, btc2 = close.copy(), d.copy(), btc.copy()
    close2[t + 1 :] *= 3.0
    d2[t + 1 :] = 9.0
    btc2[t + 1 :] = 0.5
    b = symbol_features(np.r_[100.0, close2[:-1]], close2, d2, btc2)
    for k in ("r", "sd30", "z", "beta"):
        np.testing.assert_allclose(a[k][: t + 1], b[k][: t + 1], equal_nan=True)
    # labels of earlier days do not depend on later values
    days = np.repeat(np.arange(100), 3)
    v = rng.normal(size=days.size)
    la = expanding_bin_labels(days, v, min_prior=30)
    v2 = v.copy()
    v2[days > 60] = -50.0
    lb = expanding_bin_labels(days, v2, min_prior=30)
    np.testing.assert_array_equal(la[days <= 60], lb[days <= 60])


def test_event_mask():
    m = event_mask([0.03, -0.05, 0.01, float("nan")], [0.02, 0.02, 0.02, 0.02], 1.5)
    assert m.tolist() == [True, True, False, False]


def test_expanding_bins_analytic():
    # 3 prior days with values 0..8 -> tercile cuts at 2.667 and 5.333
    days = [0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3]
    vals = [0, 1, 2, 3, 4, 5, 6, 7, 8, 1.0, 4.0, 8.0]
    lab = expanding_bin_labels(days, vals, nbins=3, min_prior=9)
    assert lab[:9].tolist() == [-1] * 9
    assert lab[9:].tolist() == [0, 1, 2]


def test_week_of_monday_start():
    # 2024-01-01 (Monday) = epoch day 19723; Sunday 2024-01-07 same week; Monday 2024-01-08 next
    assert week_of(19723) == week_of(19729)
    assert week_of(19730) == week_of(19723) + 1


def test_funding_window():
    t = [0, 8, 16, 24, 32]
    r = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert funding_sum(t, r, 0, 24) == pytest.approx(0.9)  # (0, 24]
    assert last_funding(t, r, 16) == pytest.approx(0.3)
    assert math.isnan(last_funding(t, r, -1))


def test_trade_cost():
    assert trade_cost(1.2, is_major=False, hedged=True) == pytest.approx(0.0011 + 0.001 + 1.2 * 0.0015)
    assert trade_cost(1.0, is_major=True, hedged=False) == pytest.approx(0.0015)


def test_combo_se_and_bootstrap_iid_limit():
    rng = np.random.default_rng(3)
    n = 4000
    g = rng.integers(0, 3, n)
    v = rng.normal(0, 1, n) + np.where(g == 2, 0.5, 0.0)
    cl = list(range(n))  # every row its own cluster -> CR1 ~ iid
    w = {2: 1.0, 0: -1.0}
    est = combo_estimate(v, g, w)
    se = cluster_se_combo(v, g, cl, w)
    assert se["se"] == pytest.approx(se["se_iid"], rel=0.03)
    assert se["se"] == pytest.approx(math.sqrt(2 / (n / 3)), rel=0.08)
    bs = cluster_bootstrap_combo(v, g, cl, w, n_boot=2000)
    assert bs["est"] == pytest.approx(est)
    assert (bs["hi"] - bs["lo"]) / 3.92 == pytest.approx(se["se"], rel=0.1)
    assert bs["lo"] > 0


def test_cluster_se_grows_with_common_shocks():
    rng = np.random.default_rng(4)
    weeks = np.repeat(np.arange(200), 20)
    shock = rng.normal(0, 1, 200)[weeks]
    g = np.tile([0, 2], 2000)
    v = shock * np.where(g == 2, 1.0, -1.0) + rng.normal(0, 0.2, weeks.size)
    se = cluster_se_combo(v, g, weeks.tolist(), {2: 1.0, 0: -1.0})
    assert se["se"] > 3 * se["se_iid"]


def test_drop_top_clusters():
    v = [10.0, 0.0, 1.0, 0.0]
    g = [2, 0, 2, 0]
    cl = ["a", "a", "b", "b"]
    assert combo_estimate(v, g, {2: 1.0, 0: -1.0}) == pytest.approx(5.5)
    assert drop_top_clusters_combo(v, g, cl, {2: 1.0, 0: -1.0}, 1) == pytest.approx(1.0)


def test_shuffle_within_groups_preserves_group_multisets():
    rng = np.random.default_rng(5)
    v = np.arange(12, dtype=float)
    grp = [0, 0, 0, 1, 1, 1, 1, 2, 2, 3, 3, 3]
    out = shuffle_within_groups(v, grp, rng)
    for k in set(grp):
        idx = [i for i, x in enumerate(grp) if x == k]
        assert sorted(out[idx]) == sorted(v[idx])


def test_ols_contrast_recovers_coefficients():
    rng = np.random.default_rng(6)
    n = 3000
    top = rng.integers(0, 2, n).astype(float)
    bot = (1 - top) * rng.integers(0, 2, n)
    x = rng.normal(size=n)
    y = 0.01 + 0.02 * top - 0.01 * bot + 0.5 * x + rng.normal(0, 0.01, n)
    X = np.column_stack([np.ones(n), top, bot, x])
    fit = ols_cluster_cov(y, X, list(range(n)))
    c = contrast(fit, [0, 1, -1, 0])
    assert c["est"] == pytest.approx(0.03, abs=0.002)
    assert c["p"] < 1e-6
