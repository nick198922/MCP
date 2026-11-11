from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics import range_efficiency as re_


def _pos(n: int) -> np.ndarray:
    return np.arange(n)


def test_er_straight_line_is_one() -> None:
    c = np.arange(1.0, 50.0)
    assert re_.efficiency_ratio(c, _pos(c.size), 40, w=30) == pytest.approx(1.0)


def test_er_zigzag_back_to_start_is_zero() -> None:
    c = np.array([10.0, 11.0] * 20)  # 40 bars; 30 changes from an even index return to 10 or 11
    assert re_.efficiency_ratio(c, _pos(c.size), 33, w=30) == pytest.approx(0.0)


def test_er_known_value() -> None:
    # closes 0..4 window of w=4 changes: 10, 12, 11, 13, 12 -> |12 - 10| / (2 + 1 + 2 + 1) = 1/3
    c = np.array([10.0, 12.0, 11.0, 13.0, 12.0, 99.0])
    assert re_.efficiency_ratio(c, _pos(c.size), 5, w=4) == pytest.approx(1 / 3)


def test_er_needs_w_plus_one_closes_in_one_segment() -> None:
    c = np.arange(1.0, 40.0)
    assert math.isnan(re_.efficiency_ratio(c, _pos(c.size), 30, w=30))  # closes -1 .. 29 do not exist
    assert math.isfinite(re_.efficiency_ratio(c, _pos(c.size), 31, w=30))
    pos = _pos(c.size).copy()
    pos[5:] = np.arange(c.size - 5)  # a hole before bar 5: segment restarts
    assert math.isnan(re_.efficiency_ratio(c, pos, 35, w=30))  # needs bar 4
    assert math.isfinite(re_.efficiency_ratio(c, pos, 36, w=30))


def test_er_flat_is_nan() -> None:
    c = np.full(40, 5.0)
    assert math.isnan(re_.efficiency_ratio(c, _pos(40), 35, w=30))


def test_features_do_not_look_at_bar_s_or_later() -> None:
    rng = np.random.default_rng(1)
    c = np.exp(np.cumsum(rng.normal(0, 0.01, 300))) * 100
    s = 200
    er, vr = re_.efficiency_ratio(c, _pos(300), s), re_.variance_ratio(c, _pos(300), s)
    c2 = c.copy()
    c2[s:] = c2[s:] * rng.uniform(0.5, 2.0, 300 - s)
    assert re_.efficiency_ratio(c2, _pos(300), s) == er
    assert re_.variance_ratio(c2, _pos(300), s) == vr
    c3 = c.copy()
    c3[s - 1] *= 1.1  # the last range close does matter
    assert re_.efficiency_ratio(c3, _pos(300), s) != er


def test_vr_alternating_is_zero() -> None:
    c = np.exp(np.cumsum(np.array([0.0] + [0.01, -0.01] * 70)))  # mean log return 0 over an even window
    assert re_.variance_ratio(c, _pos(c.size), 140, n=120, q=4) == pytest.approx(0.0, abs=1e-12)


def test_vr_random_walk_near_one_and_trend_above() -> None:
    rng = np.random.default_rng(7)
    vals = []
    for _ in range(400):
        c = np.exp(np.cumsum(rng.normal(0, 0.01, 130)))
        vals.append(re_.variance_ratio(c, _pos(130), 125))
    assert np.mean(vals) == pytest.approx(1.0, abs=0.05)
    trend = []
    for _ in range(200):  # AR(1) returns with phi = 0.5: VR(4) = 1 + 2 * (3/4 phi + 2/4 phi^2 + 1/4 phi^3) ~ 2.03
        e = rng.normal(0, 0.01, 130)
        r = np.zeros(130)
        for k in range(1, 130):
            r[k] = 0.5 * r[k - 1] + e[k]
        trend.append(re_.variance_ratio(np.exp(np.cumsum(r)), _pos(130), 125))
    assert np.mean(trend) > 1.6


def test_tertiles() -> None:
    x = np.arange(1.0, 10.0)
    lo, hi = re_.tertile_cuts(x)
    assert (lo, hi) == pytest.approx((11 / 3, 19 / 3))
    k = re_.tertile_codes(np.array([1.0, 4.0, 7.0, math.nan]), lo, hi)
    assert k.tolist() == [1, 2, 3, 0]
    k2 = re_.tertile_codes(np.array([1.0, 1.0]), np.array([0.5, 1.0]), np.array([0.8, 2.0]))
    assert k2.tolist() == [3, 1]


def test_spearman() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert re_.spearman(x, x**3) == pytest.approx(1.0)
    assert re_.spearman(x, -x) == pytest.approx(-1.0)
    assert re_.rankdata(np.array([3.0, 1.0, 3.0, 2.0])).tolist() == [3.5, 1.0, 3.5, 2.0]
    # textbook: d = rank differences [0, 0, 1, -1, 0] -> 1 - 6 * 2 / (5 * 24) = 0.9
    assert re_.spearman(x, np.array([1.0, 2.0, 4.0, 3.0, 5.0])) == pytest.approx(0.9)


def test_within_percentile() -> None:
    p = re_.within_percentile(np.array([1.0, 2.0, 10.0, 20.0, 30.0]), np.array(["a", "a", "b", "b", "b"]))
    assert p.tolist() == pytest.approx([0.5, 1.0, 1 / 3, 2 / 3, 1.0])


def test_cluster_se_diff_one_row_per_cluster_matches_welch_form() -> None:
    rng = np.random.default_rng(3)
    y = rng.normal(0, 1, 200)
    a = np.zeros(200, dtype=bool)
    a[:80] = True
    b = ~a
    se = re_.cluster_se_diff(y, a, b, np.arange(200))
    ea, eb = y[a] - y[a].mean(), y[b] - y[b].mean()
    expect = math.sqrt((ea @ ea / 80**2 + eb @ eb / 120**2) * 200 / 199)
    assert se == pytest.approx(expect)
    assert re_.mde80(se) == pytest.approx(2.8015852 * se, rel=1e-6)


def test_cluster_se_diff_grows_with_common_cluster_shocks() -> None:
    rng = np.random.default_rng(4)
    cl = np.repeat(np.arange(20), 50)
    y_iid = rng.normal(0, 1, 1000)
    a = rng.random(1000) < 0.5
    shock = rng.normal(0, 1, 20)[cl]
    a_side = np.where(a, 1.0, -1.0)  # a shock that moves group a and b apart inside the cluster
    se_iid = re_.cluster_se_diff(y_iid, a, ~a, cl)
    se_cl = re_.cluster_se_diff(y_iid + shock * a_side, a, ~a, cl)
    assert se_cl > 2 * se_iid


def test_boot_diff_and_mean() -> None:
    rng = np.random.default_rng(5)
    cl = np.repeat(np.arange(40), 25)
    a = rng.random(1000) < 0.5
    y = rng.normal(0, 1, 1000) + np.where(a, 0.5, 0.0)
    r = re_.boot_diff(y, a, ~a, cl, n_boot=2000, seed=1)
    assert r["est"] == pytest.approx(y[a].mean() - y[~a].mean())
    assert r["lo"] < r["est"] < r["hi"] and r["lo"] > 0 and r["p"] < 0.01
    assert r["clusters"] == 40
    m = re_.boot_mean(y, cl, n_boot=2000, seed=1)
    assert m["lo"] < y.mean() < m["hi"]


def test_boot_spearman_and_placebo() -> None:
    rng = np.random.default_rng(6)
    n = 600
    x = rng.random(n)
    y = -x + rng.normal(0, 0.3, n)
    cl = np.repeat(np.arange(30), 20)
    r = re_.boot_spearman(x, y, cl, n_boot=300, seed=2)
    assert r["hi"] < 0
    lo, hi = re_.tertile_cuts(x)
    k = re_.tertile_codes(x, lo, hi)
    obs = y[k == 1].mean() - y[k == 3].mean()
    perm = re_.placebo_diff(x, y, cl, np.full(n, lo), np.full(n, hi), n_perm=200, seed=3)
    assert perm.size == 200
    assert abs(float(np.mean(perm))) < 0.1 and (perm >= obs).mean() == 0.0
    # a cell with a single row keeps its x: the permutation never moves rows across cells
    one = re_.placebo_diff(x[:3], y[:3], np.array([0, 1, 2]), np.full(3, lo), np.full(3, hi), n_perm=5, seed=0)
    k3 = re_.tertile_codes(x[:3], lo, hi)
    if (k3 == 1).any() and (k3 == 3).any():
        assert np.allclose(one, y[:3][k3 == 1].mean() - y[:3][k3 == 3].mean())
