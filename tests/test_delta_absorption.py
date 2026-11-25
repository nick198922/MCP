"""Delta absorption on the S3 signal bar (IDEA-20261113-x-traders, batch 5.7): analytic expectations, causality and a
real Binance USD-M 1h taker-kline sample."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from tradedesk.analytics import delta_absorption as da

H = da.HOUR_MS
T0 = 1_700_006_400_000  # 2023-11-15 00:00 UTC, on the 4h and 1d grids
FIX = Path(__file__).parent / "fixtures" / "binance_vision_um1h_taker_btcusdt_2024-03-01.json"


def rows(n: int, start: int = T0 - 40 * 24 * H, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = start + H * np.arange(n)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.r_[100.0, c[:-1]]
    hi, lo = np.maximum(o, c) * 1.002, np.minimum(o, c) * 0.998
    v = rng.uniform(10, 20, n)
    tb = v * rng.uniform(0.3, 0.7, n)
    tq = tb * (hi + lo) / 2
    return np.column_stack([t, o, hi, lo, c, v, v * c, tb, tq])


def test_bar_delta_analytic() -> None:
    t = T0 + H * np.arange(4)
    base = np.array([10.0, 20.0, 30.0, 40.0])
    tbb = np.array([10.0, 0.0, 15.0, 20.0])
    # B = 45, V = 100 -> 2 * 0.45 - 1 = -0.10
    assert da.bar_delta(t, base, tbb, T0, T0 + 4 * H) == pytest.approx(-0.10)
    assert da.oriented(-0.10, "long") == pytest.approx(-0.10)
    assert da.oriented(-0.10, "short") == pytest.approx(0.10)
    assert da.absorption(-0.10) and not da.absorption(0.10) and not da.absorption(0.0) and not da.absorption(math.nan)
    # one buying-only hour: +1; one selling-only hour: -1
    assert da.bar_delta(t, base, tbb, T0, T0 + H) == pytest.approx(1.0)
    assert da.bar_delta(t, base, tbb, T0 + H, T0 + 2 * H) == pytest.approx(-1.0)


def test_bar_delta_requires_every_hour_and_valid_values() -> None:
    t = T0 + H * np.array([0, 1, 3, 4])  # hour 2 missing
    base = np.full(4, 10.0)
    tbb = np.full(4, 5.0)
    assert math.isnan(da.bar_delta(t, base, tbb, T0, T0 + 4 * H))
    t = T0 + H * np.arange(4)
    assert math.isnan(da.bar_delta(t, base, np.array([5.0, 5.0, 11.0, 5.0]), T0, T0 + 4 * H))  # taker buy > volume
    assert math.isnan(da.bar_delta(t, np.zeros(4), np.zeros(4), T0, T0 + 4 * H))  # no volume
    assert math.isnan(da.bar_delta(t, base, tbb, T0, T0 + 3 * H + 1))  # not whole hours


def test_placebo_window_and_pick() -> None:
    assert da.placebo_k_range(4 * H) == (36, 168)
    assert da.placebo_k_range(24 * H) == (6, 28)
    r = rows(40 * 24)
    t, v, tb = r[:, 0].astype(np.int64), r[:, 5], r[:, 7]
    sig = T0
    c = da.candidate_deltas(t, v, tb, sig, 24 * H, "long")
    assert c.size == 23
    # candidate j is the bar k = 6 + j days before the signal bar
    assert c[0] == pytest.approx(da.bar_delta(t, v, tb, sig - 6 * 24 * H, sig - 5 * 24 * H))
    assert c[-1] == pytest.approx(da.bar_delta(t, v, tb, sig - 28 * 24 * H, sig - 27 * 24 * H))
    cs = da.candidate_deltas(t, v, tb, sig, 24 * H, "short")
    assert np.allclose(cs, -c)
    keys = ["a", "b"]
    p1 = da.placebo_pick([c, np.full(3, math.nan)], keys, 50, 7)
    p2 = da.placebo_pick([c, np.full(3, math.nan)], keys, 50, 7)
    assert np.array_equal(p1[0], p2[0]) and np.all(np.isin(p1[0], c))
    assert np.all(np.isnan(p1[1]))
    assert da.excess_delta(0.1, np.array([0.0, 0.2, math.nan, 0.4])) == pytest.approx(0.1 - 0.2)
    assert math.isnan(da.excess_delta(0.1, np.array([math.nan])))


def test_extreme_hour_and_body() -> None:
    t = T0 + H * np.arange(4)
    low = np.array([99.0, 97.0, 98.0, 99.5])
    high = np.array([101.0, 100.0, 102.5, 101.0])
    base = np.full(4, 10.0)
    tbb = np.array([5.0, 2.0, 9.0, 5.0])
    # long: the hour with the lowest low is hour 1, delta 2 * 0.2 - 1 = -0.6
    assert da.extreme_hour_delta(t, low, base, tbb, T0, T0 + 4 * H, "long") == pytest.approx(-0.6)
    # short: the hour with the highest high is hour 2, delta +0.8, oriented -0.8
    assert da.extreme_hour_delta(t, -high, base, tbb, T0, T0 + 4 * H, "short") == pytest.approx(-0.8)
    assert da.oriented_body(100.0, 102.0, "long") == pytest.approx(0.02)
    assert da.oriented_body(100.0, 102.0, "short") == pytest.approx(-0.02)
    assert math.isnan(da.oriented_body(0.0, 1.0, "long"))


def test_stratified_delta() -> None:
    v = np.array([1.0, 3.0, 10.0, 12.0])
    flag = np.array([True, False, True, False])
    strata = np.array([0, 0, 1, 1])
    # stratum means 2 and 11: flagged residuals -1 and -1
    assert da.stratified_delta(v, flag, strata) == pytest.approx(-1.0)
    assert da.stratified_resid(v, strata).tolist() == [-1.0, 1.0, -1.0, 1.0]
    assert math.isnan(da.stratified_delta(v, np.zeros(4, dtype=bool), strata))
    # baseline needs >= half of the candidate bars
    assert math.isnan(da.excess_delta(0.1, np.array([0.2, math.nan, math.nan])))
    assert da.excess_delta(0.1, np.array([0.2, 0.0, math.nan])) == pytest.approx(0.0)


def test_matched_flags_keep_counts_per_stratum() -> None:
    x = np.array([0.3, -0.1, 0.2, math.nan, -0.5, 0.0])
    s = np.array([0, 0, 0, 1, 1, 1])
    f = np.array([True, True, False, True, False, False])
    m = da.matched_flags(x, s, f)
    assert m.tolist() == [False, True, True, False, True, False]
    assert [int(m[s == g].sum()) for g in (0, 1)] == [int(f[s == g].sum()) for g in (0, 1)]


def test_stratified_var_factor_and_bootstrap() -> None:
    # flag spread evenly over equal strata: the stratified estimator costs nothing (factor 1)
    flag = np.array([True, False] * 50)
    strata = np.repeat([0, 1], 50)
    assert da.stratified_var_factor(flag, strata) == pytest.approx(1.0)
    # 40 of 50 and 10 of 50 flagged: (0.64 (1/40 - 1/50) + 0.04 (1/10 - 1/50)) / (1/50 - 1/100) = 0.64
    flag2 = np.r_[np.ones(40, bool), np.zeros(10, bool), np.ones(10, bool), np.zeros(40, bool)]
    assert da.stratified_var_factor(flag2, strata) == pytest.approx(0.64)
    # a constant shift of flagged trades inside every stratum is recovered exactly; CI brackets it
    rng = np.random.default_rng(5)
    n = 4000
    s = rng.integers(0, 5, n)
    f = rng.random(n) < 0.5
    v = s * 1.0 + 0.3 * f + rng.normal(0, 1, n)
    cl = rng.integers(0, 200, n)
    r = da.stratified_delta_ci(v, f, s, cl, n_boot=400, seed=1)
    assert r["d"] == pytest.approx(da.stratified_delta(v, f, s))
    assert r["lo"] < r["d"] < r["hi"]
    # flagged minus stratum mean = (1 - share) * shift ≈ 0.15
    assert r["d"] == pytest.approx(0.15, abs=0.06) and r["p"] < 0.01
    # strata absorb a confound: flagged trades sit in the high strata, nothing inside a stratum
    v2 = s * 1.0 + rng.normal(0, 1, n)
    f2 = (rng.random(n) < 0.5) & (s >= 2)
    assert float(v2[f2].mean() - v2.mean()) > 0.5  # the plain Δ sees the confound
    r2 = da.stratified_delta_ci(v2, f2, s, cl, n_boot=300, seed=2)
    assert abs(r2["d"]) < 0.1 and r2["lo"] < 0 < r2["hi"]


def test_bootstrap_p_has_plus_one_correction() -> None:
    # every replicate is above 0 (a huge shift): p = 2 (1 + 0) / (1 + B), never 0
    rng = np.random.default_rng(3)
    n = 2000
    s = rng.integers(0, 4, n)
    f = rng.random(n) < 0.5
    v = 5.0 * f + rng.normal(0, 0.1, n)
    r = da.stratified_delta_ci(v, f, s, rng.integers(0, 100, n), n_boot=199, seed=4)
    assert r["p"] == pytest.approx(2 / 200)


def test_covariate_adjusted_removes_linear_imbalance() -> None:
    # R depends on a covariate x that is higher among flagged trades inside every stratum; no flag effect.
    rng = np.random.default_rng(11)
    n = 6000
    s = rng.integers(0, 6, n)
    g = (s >= 3).astype(int)  # two "timeframes", strata 0-2 and 3-5
    f = rng.random(n) < 0.6
    x = rng.normal(0, 1, n) + 0.5 * f
    slope = np.where(g == 0, 0.4, -0.2)
    v = s * 0.3 + slope * x + rng.normal(0, 0.05, n)
    assert da.stratified_delta(v, f, s) == pytest.approx(0.4 * 0.5 * 0.4 * 0.5 - 0.2 * 0.5 * 0.4 * 0.5, abs=0.02)
    adj, betas = da.covariate_adjusted(v, x[:, None], s, g)
    assert betas[0][0] == pytest.approx(0.4, abs=0.01) and betas[1][0] == pytest.approx(-0.2, abs=0.01)
    assert abs(da.stratified_delta(adj, f, s)) < 0.005
    # stratum means are unchanged
    for c in range(6):
        assert adj[s == c].mean() == pytest.approx(v[s == c].mean())
    # a true flag shift survives the adjustment
    adj2, _ = da.covariate_adjusted(v + 0.3 * f, x[:, None], s, g)
    assert da.stratified_delta(adj2, f, s) == pytest.approx(0.3 * 0.4, abs=0.02)
    # rows outside strata or with a non-finite covariate are nan
    x2 = x.copy()
    x2[0] = math.nan
    s2 = s.copy()
    s2[1] = -1
    adj3, _ = da.covariate_adjusted(v, x2[:, None], s2, g)
    assert math.isnan(adj3[0]) and math.isnan(adj3[1]) and np.isfinite(adj3[2:]).all()


@pytest.mark.parametrize(
    ("mde_prior", "all_c", "d", "p1", "mde", "hi", "want"),
    [
        (0.30, True, 0.2, 0.001, 0.03, 0.3, "◯"),  # gate first, whatever the rest
        (math.nan, True, 0.2, 0.001, 0.03, 0.3, "◯"),
        (0.05, True, 0.08, 0.001, 0.06, 0.12, "✓"),
        (0.05, False, 0.04, 0.01, 0.06, 0.08, "≈"),
        (0.05, False, 0.03, 0.10, 0.06, 0.07, "◯"),  # underpowered and +0.05R not excluded
        (0.05, False, 0.01, 0.30, 0.06, 0.049, "✗"),  # underpowered but the CI excludes +0.05R
        (0.05, False, -0.04, 0.99, 0.053, -0.003, "✗"),  # the review case: CI excludes 0 on the negative side
        (0.05, False, 0.02, 0.20, 0.049, 0.07, "✗"),  # power to see +0.05R, not seen
        (0.05, False, 0.02, 0.20, 0.05, 0.07, "✗"),  # MDE exactly at the interest is not "> interest"
        (0.05, False, 0.02, 0.20, math.nan, 0.07, "◯"),
    ],
)
def test_verdict_rule_is_exhaustive_and_ordered(
    mde_prior: float, all_c: bool, d: float, p1: float, mde: float, hi: float, want: str
) -> None:
    assert da.verdict(mde_prior, all_c, d, p1, mde, hi) == want


def test_features_before_t_ignore_rows_after_t() -> None:
    r = rows(40 * 24, seed=3)
    t, v, tb = r[:, 0].astype(np.int64), r[:, 5].copy(), r[:, 7].copy()
    sig, bar = T0 - 3 * 24 * H, 24 * H
    t_in = sig + bar
    before = (
        da.bar_delta(t, v, tb, sig, t_in),
        da.candidate_deltas(t, v, tb, sig, bar, "long"),
        da.extreme_hour_delta(t, r[:, 3], v, tb, sig, t_in, "long"),
    )
    later = t >= t_in
    v2, tb2, low2 = v.copy(), tb.copy(), r[:, 3].copy()
    v2[later] *= 7.0
    tb2[later] = 0.0
    low2[later] = 1.0
    after = (
        da.bar_delta(t, v2, tb2, sig, t_in),
        da.candidate_deltas(t, v2, tb2, sig, bar, "long"),
        da.extreme_hour_delta(t, low2, v2, tb2, sig, t_in, "long"),
    )
    assert after[0] == before[0] and np.array_equal(after[1], before[1], equal_nan=True) and after[2] == before[2]
    keep = ~later
    cut = (
        da.bar_delta(t[keep], v[keep], tb[keep], sig, t_in),
        da.candidate_deltas(t[keep], v[keep], tb[keep], sig, bar, "long"),
    )
    assert cut[0] == before[0] and np.array_equal(cut[1], before[1], equal_nan=True)


def test_real_sample_units() -> None:
    """Binance USD-M BTCUSDT 1h klines with taker columns (data.binance.vision monthly archive 2024-03, first day),
    rows as ``parse_klines_taker_zip`` returns them."""
    doc = json.loads(FIX.read_text())
    r = np.array(doc["rows"], dtype=float)
    assert r.shape == (24, 9)
    assert np.all(np.diff(r[:, 0]) == H) and int(r[0, 0]) % (24 * H) == 0
    # taker buy is a part of the volume, in the same (base) units; quote / base is a price inside the bar
    assert np.all((r[:, da.TB_] >= 0) & (r[:, da.TB_] <= r[:, da.V_]))
    assert np.all(da.taker_vwap_inside(r))
    px = r[:, da.QV_] / r[:, da.V_]
    assert np.all((px >= r[:, da.L_]) & (px <= r[:, da.H_]))
    d = da.bar_delta(r[:, 0].astype(np.int64), r[:, da.V_], r[:, da.TB_], int(r[0, 0]), int(r[0, 0]) + 24 * H)
    assert -0.2 < d < 0.2  # a whole-day BTC delta is a small imbalance
    assert d == pytest.approx(2 * r[:, da.TB_].sum() / r[:, da.V_].sum() - 1)
