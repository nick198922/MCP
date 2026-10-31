import numpy as np
import pytest

from tradedesk.analytics import fresh_confounders as fc
from tradedesk.analytics.spot_perp_flow import ols_cluster_cov


def test_edge_age_counts_bars_back_to_the_latest_minimum() -> None:
    low = np.array([5.0, 1.0, 3.0, 1.0, 4.0, 2.0, 0.5])
    # window low[1:6] = [1, 3, 1, 4, 2]: minimum 1 last at index 3 -> 6 - 3 = 3 bars before the sweep bar 6
    assert fc.edge_age(low, 6, 5) == 3
    # the edge set on the bar just before the sweep has age 1; on the first bar of the window, age w
    assert fc.edge_age(np.array([3.0, 2.0, 1.0, 0.0]), 3, 3) == 1
    assert fc.edge_age(np.array([1.0, 2.0, 3.0, 0.0]), 3, 3) == 3
    with pytest.raises(ValueError):
        fc.edge_age(low, 2, 5)


def test_edge_age_ignores_bars_from_the_sweep_on() -> None:
    rng = np.random.default_rng(1)
    low = rng.normal(100, 1, 60)
    s, w = 40, 30
    a = fc.edge_age(low, s, w)
    later = low.copy()
    later[s:] = rng.normal(90, 5, later.size - s)  # the sweep bar and everything after it changes
    assert fc.edge_age(later, s, w) == a


def test_quantile_codes_within_groups() -> None:
    x = np.r_[np.arange(10.0), np.arange(100.0, 110.0)]
    g = np.array(["a"] * 10 + ["b"] * 10)
    c = fc.quantile_codes(x, g, q=5)
    # each group of 10 splits into 5 bins of 2, independently of the other group's level
    assert list(c[:10]) == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]
    assert list(c[10:]) == list(c[:10])
    assert fc.quantile_codes([np.nan, 1.0], ["a", "a"], q=2)[0] == -1


def test_stratified_diff_removes_a_pure_confounder() -> None:
    # R depends only on the stratum; fresh trades sit mostly in the good stratum -> raw diff > 0, stratified diff 0
    strata = np.array([0] * 100 + [1] * 100)
    v = np.where(strata == 1, 1.0, -1.0)
    fresh = np.zeros(200, dtype=bool)
    fresh[:20] = True  # 20 fresh in the bad stratum
    fresh[100:180] = True  # 80 fresh in the good stratum
    far = ~fresh
    raw = v[fresh].mean() - v[far].mean()
    assert raw == pytest.approx(0.6 - (-0.6))
    assert fc.stratified_diff(v, fresh, far, strata) == pytest.approx(0.0)
    # one stratum = the plain difference of means
    assert fc.stratified_diff(v, fresh, far, np.zeros(200)) == pytest.approx(raw)


def test_stratified_diff_weights_and_constant_shift() -> None:
    # within-stratum shift of +0.5 for fresh everywhere -> 0.5 whatever the weights
    strata = np.repeat([0, 1, 2], 50)
    rng = np.random.default_rng(3)
    fresh = rng.random(150) < 0.3
    v = strata * 2.0 + 0.5 * fresh
    assert fc.stratified_diff(v, fresh, ~fresh, strata) == pytest.approx(0.5)
    r = fc.stratified_diff_ci(v, fresh, ~fresh, strata, np.arange(150) // 10, n_boot=300, seed=1)
    assert r["d"] == pytest.approx(0.5)
    assert r["lo"] == pytest.approx(0.5) and r["hi"] == pytest.approx(0.5)


def test_stratified_diff_ci_covers_zero_under_the_null() -> None:
    rng = np.random.default_rng(7)
    n = 4000
    strata = rng.integers(0, 5, n)
    fresh = rng.random(n) < 0.3
    v = rng.normal(0, 1, n) + 0.2 * strata
    r = fc.stratified_diff_ci(v, fresh, ~fresh, strata, np.arange(n) // 20, n_boot=500, seed=2)
    assert r["lo"] < 0 < r["hi"]
    assert r["se"] == pytest.approx(np.sqrt(1 / fresh.sum() + 1 / (~fresh).sum()), rel=0.25)


def test_design_recovers_the_adjusted_fresh_effect() -> None:
    rng = np.random.default_rng(11)
    n = 6000
    tf = rng.choice(["2h", "8h"], n)
    q = rng.integers(0, 5, n)
    fresh = rng.random(n) < 0.2 + 0.1 * q  # fresh more likely in high bins
    y = 0.3 * q + 0.1 * fresh + (tf == "8h") * 0.2 + rng.normal(0, 0.5, n)
    X = fc.design(fresh, [tf], [(tf, q)])
    assert X.shape[1] == 2 + 1 + 2 * 4
    assert np.linalg.matrix_rank(X) == X.shape[1]
    fit = ols_cluster_cov(y, X, np.arange(n) // 30)
    assert fit["coef"][1] == pytest.approx(0.1, abs=0.04)
    # without the confounder the fresh coefficient is biased upward
    fit0 = ols_cluster_cov(y, fc.design(fresh, [tf]), np.arange(n) // 30)
    assert fit0["coef"][1] > 0.2
