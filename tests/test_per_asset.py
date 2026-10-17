"""Tests for analytics.per_asset (IDEA-20261017-per-asset-recheck): analytic expectations and no look-ahead."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics.per_asset import (
    asset_stats,
    chi2_ppf,
    chi2_sf,
    cluster_robust_q,
    heterogeneity,
    mde,
    permutation_q,
    permute_within,
    persistence,
    quantile_bins,
    spearman,
    tau_ci_qprofile,
    top_fraction_uplift,
)


def test_chi2_sf_reference_values() -> None:
    for x in (0.5, 2.0, 7.3, 40.0):
        assert chi2_sf(x, 2) == pytest.approx(math.exp(-x / 2), rel=1e-12)  # df = 2 is exponential
    assert chi2_sf(3.841458820694124, 1) == pytest.approx(0.05, abs=1e-9)
    assert chi2_sf(18.307038053275146, 10) == pytest.approx(0.05, abs=1e-9)
    assert chi2_sf(124.3421134, 100) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(0.0, 3) == 1.0
    assert chi2_sf(1e4, 5) < 1e-100


def test_asset_stats_mean_sd_ci() -> None:
    st = asset_stats([1.0, 3.0, 5.0, 10.0], ["a", "a", "a", "b"], min_n=1)
    a, b = st
    assert (a.asset, a.n, a.mean, a.sd) == ("a", 3, 3.0, 2.0)
    half = 1.959963984540054 * 2.0 / math.sqrt(3)
    assert a.ci == pytest.approx((3.0 - half, 3.0 + half))
    assert b.n == 1 and b.ci is None
    assert [s.asset for s in asset_stats([1.0, 3.0, 5.0, 10.0], ["a", "a", "a", "b"], min_n=2)] == ["a"]


def test_heterogeneity_by_hand() -> None:
    # a: [0, 2] mean 1; b: [2, 4] mean 3; grand mean 2; between SS 4, within SS 4, s_w^2 = 4 / 2 = 2 -> Q = 2
    h = heterogeneity([0.0, 2.0, 2.0, 4.0], ["a", "a", "b", "b"], min_n=2)
    assert h["k"] == 2 and h["df"] == 1
    assert h["q"] == pytest.approx(2.0)
    assert h["i2"] == pytest.approx(0.5)
    assert h["p"] == pytest.approx(chi2_sf(2.0, 1))
    # per-asset variances are both 2 -> weights n/s^2 = 1, the same Q
    assert h["q_sep"] == pytest.approx(2.0)
    # DL: tau^2 = (Q - df) / (sum w - sum w^2 / sum w) = 1 / (2 - 1) = 1
    assert h["tau"] == pytest.approx(1.0)


def test_heterogeneity_identical_assets_is_zero() -> None:
    x = [-1.0, 2.0, -1.0, 0.5]
    h = heterogeneity(x * 3, ["a"] * 4 + ["b"] * 4 + ["c"] * 4, min_n=4)
    assert h["q"] == pytest.approx(0.0, abs=1e-12)
    assert h["i2"] == 0.0
    assert h["p"] == pytest.approx(1.0)
    assert h["tau"] == 0.0


def test_heterogeneity_min_n_filters_assets() -> None:
    h = heterogeneity([0.0, 2.0, 2.0, 4.0, 9.0], ["a", "a", "b", "b", "c"], min_n=2)
    assert h["k"] == 2 and h["n"] == 4
    assert heterogeneity([1.0, 2.0], ["a", "b"], min_n=2)["p"] is None


def test_cluster_robust_q_matches_q_sep_and_ignores_duplicates() -> None:
    rng = np.random.default_rng(6)
    v = list(rng.normal(size=120) + np.repeat([0.0, 0.3, -0.3], 40))
    a = ["a"] * 40 + ["b"] * 40 + ["c"] * 40
    g = list(range(120))  # one trade per cluster
    h = heterogeneity(v, a, min_n=30)
    q = cluster_robust_q(v, a, g, min_n=30)
    assert q["k"] == 3 and q["df"] == 2
    assert q["q"] == pytest.approx(h["q_sep"], rel=1e-12)
    assert q["deff"] == pytest.approx(1.0)
    # every trade duplicated inside its own cluster: the naive Q doubles, the cluster-robust Q does not move
    v2, a2, g2 = v + v, a + a, g + g
    assert heterogeneity(v2, a2, min_n=30)["q_sep"] == pytest.approx(2 * h["q_sep"], rel=0.05)
    q2 = cluster_robust_q(v2, a2, g2, min_n=30)
    assert q2["q"] == pytest.approx(q["q"], rel=1e-9)
    assert q2["deff"] == pytest.approx(2.0, rel=0.05)


def test_permute_within_keeps_counts_per_date() -> None:
    rng = np.random.default_rng(1)
    codes = rng.integers(0, 5, 500)
    days = rng.integers(0, 40, 500)
    out = permute_within(codes, days, rng)
    for d in range(40):
        assert sorted(out[days == d]) == sorted(codes[days == d])
    assert not np.array_equal(out, codes)


def test_permutation_q_no_shuffle_possible_gives_p_one() -> None:
    # every date has trades of one asset only: the permutation changes nothing, Q* = Q always
    rng = np.random.default_rng(2)
    v = rng.normal(size=200)
    assets = ["a"] * 100 + ["b"] * 100
    days = list(range(200))
    r = permutation_q(v, assets, days, min_n=30, n_perm=200)
    assert r["p"] == pytest.approx(1.0)


def test_permutation_q_detects_a_real_asset_effect_and_not_a_fake_one() -> None:
    rng = np.random.default_rng(3)
    days = np.repeat(np.arange(150), 4)
    assets = np.tile(np.array(["a", "b", "c", "d"]), 150)
    base = rng.normal(size=600) + np.repeat(rng.normal(size=150), 4)  # common date shocks
    eff = {"a": 0.8, "b": 0.0, "c": 0.0, "d": -0.8}
    v = base + np.array([eff[x] for x in assets])
    assert permutation_q(v, list(assets), list(days), min_n=30, n_perm=300)["p"] < 0.01
    r0 = permutation_q(base, list(assets), list(days), min_n=30, n_perm=300)
    assert r0["p"] > 0.01


def test_persistence_finds_persistent_assets() -> None:
    rng = np.random.default_rng(4)
    names = [f"s{i}" for i in range(12)]
    eff = {n: (1.0 if i < 3 else 0.0) for i, n in enumerate(names)}
    rows = []
    for d in range(400):
        for n in rng.choice(names, 3, replace=False):
            rows.append((eff[n] + rng.normal(scale=0.5), n, d, d < 250))
    v, a, c, m = (list(x) for x in zip(*rows, strict=True))
    r = persistence(v, a, c, m, min_n_is=20, n_boot=2000)
    assert r["k"] == 12 and len(r["top"]) == 3
    assert set(r["top"]) == {"s0", "s1", "s2"}
    assert r["diff"] == pytest.approx(1.0, abs=0.2)
    assert r["diff_ci"][0] > 0 and r["diff_p"] < 0.01
    assert r["rho"] > 0.3
    assert len(persistence(v, a, c, m, min_n_is=20, top_n=5, n_boot=500)["top"]) == 5


def test_persistence_ranking_ignores_data_after_split() -> None:
    """Results on in-sample trades must not change when out-of-sample data change (no look-ahead)."""
    rng = np.random.default_rng(5)
    names = [f"s{i}" for i in range(10)]
    a = list(rng.choice(names, 800))
    c = list(rng.integers(0, 300, 800))
    m = [d < 200 for d in c]
    v1 = rng.normal(size=800)
    v2 = v1.copy()
    oos = ~np.asarray(m)
    v2[oos] = rng.normal(loc=5.0, size=int(oos.sum()))  # replace every OOS value
    r1 = persistence(v1, a, c, m, n_boot=200)
    r2 = persistence(v2, a, c, m, n_boot=200)
    assert r1["top"] == r2["top"]
    assert r1["is_ranked"] == r2["is_ranked"]
    assert r1["top_is_mean"] == r2["top_is_mean"]
    # dropping the OOS trades entirely leaves the ranking unchanged too
    keep = np.asarray(m)
    r3 = persistence(v1[keep], list(np.asarray(a)[keep]), list(np.asarray(c)[keep]), [True] * int(keep.sum()), n_boot=200)
    assert r3["is_ranked"] == r1["is_ranked"]
    assert math.isnan(r3["top_oos_mean"])


def test_spearman_and_quantile_bins() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9486832980505138)
    b = quantile_bins([0.5, 1.0, 1.5, 3.0, float("nan")], [1.0, 2.0])
    assert b.tolist() == [0, 0, 1, 2, -1]


def test_chi2_ppf_inverts_sf() -> None:
    assert chi2_ppf(0.95, 1) == pytest.approx(3.841458820694124, rel=1e-9)
    assert chi2_ppf(0.95, 2) == pytest.approx(-2 * math.log(0.05), rel=1e-9)
    assert chi2_ppf(0.025, 4) == pytest.approx(0.4844185570879305, rel=1e-8)
    assert chi2_ppf(0.975, 4) == pytest.approx(11.143286781877793, rel=1e-9)
    for df in (3, 52, 200):
        assert chi2_sf(chi2_ppf(0.3, df), df) == pytest.approx(0.7, abs=1e-9)


def test_tau_ci_qprofile_equal_variances_closed_form() -> None:
    # equal sampling variances v: Q(tau^2) = S / (v + tau^2), so tau^2 bound = S / chi2_quantile - v
    y = [-2.0, -1.0, 0.0, 1.0, 2.0]
    v = 0.1
    r = tau_ci_qprofile(y, [v] * 5)
    s = 10.0
    assert r["k"] == 5
    assert r["tau_hi"] ** 2 == pytest.approx(s / 0.4844185570879305 - v, rel=1e-8)
    assert r["tau_lo"] ** 2 == pytest.approx(s / 11.143286781877793 - v, rel=1e-8)
    assert r["mu_hi_tau"] == pytest.approx(0.0, abs=1e-12)
    assert r["se_mu_hi_tau"] == pytest.approx(math.sqrt((v + r["tau_hi"] ** 2) / 5), rel=1e-9)


def test_tau_ci_qprofile_identical_means_and_coverage() -> None:
    r = tau_ci_qprofile([0.3] * 6, [0.01, 0.02, 0.03, 0.01, 0.05, 0.02])
    assert r["tau_lo"] == 0.0 and r["tau_hi"] == 0.0 and r["mu_hi_tau"] == pytest.approx(0.3)
    # coverage of the true tau over simulated meta-analyses
    rng = np.random.default_rng(7)
    tau, hits, reps = 0.2, 0, 400
    for _ in range(reps):
        var = rng.uniform(0.005, 0.05, 30)
        y = rng.normal(0.0, tau, 30) + rng.normal(0.0, np.sqrt(var))
        c = tau_ci_qprofile(y, var)
        hits += c["tau_lo"] <= tau <= c["tau_hi"]
    assert 0.91 <= hits / reps <= 0.99


def test_top_fraction_uplift_and_mde() -> None:
    assert top_fraction_uplift(0.5) == pytest.approx(math.sqrt(2 / math.pi), rel=1e-9)
    assert top_fraction_uplift(0.25) == pytest.approx(1.2711, abs=1e-4)
    rng = np.random.default_rng(8)
    z = np.sort(rng.normal(size=400_000))
    assert z[-100_000:].mean() == pytest.approx(top_fraction_uplift(0.25), abs=0.01)
    assert mde(1.0) == pytest.approx(1.6448536 + 0.8416212, rel=1e-6)
    assert mde(0.1, one_sided=False) == pytest.approx(0.1 * (1.9599640 + 0.8416212), rel=1e-6)
