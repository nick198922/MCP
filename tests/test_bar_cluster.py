"""Tests for tradedesk.analytics.bar_cluster (signals of one bar as one bet)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics import bar_cluster as bc

H4 = 4 * 3_600_000
W = 7 * 86_400_000


def test_bar_sizes_and_groups() -> None:
    t = [0, H4, H4, 2 * H4, 2 * H4, 2 * H4, 2 * H4]
    s = bc.bar_sizes(t)
    assert s.tolist() == [1, 2, 2, 4, 4, 4, 4]
    assert bc.size_group(s).tolist() == [1, 2, 2, 3, 3, 3, 3]
    assert bc.size_group(s, top=4).tolist() == [1, 2, 2, 4, 4, 4, 4]
    assert bc.size_histogram(t) == {1: 1, 2: 1, 4: 1}
    assert bc.bar_sizes([]).size == 0
    assert bc.size_histogram([]) == {}
    with pytest.raises(ValueError):
        bc.size_group([0, 1])


def test_bar_sizes_causal() -> None:
    """Sizes of trades entered before t do not change when trades after t are added or changed."""
    base = [0, 0, H4, 3 * H4]
    t_cut = 3 * H4
    more = [*base, 3 * H4, 3 * H4, 5 * H4, 5 * H4]
    a, b = bc.bar_sizes(base), bc.bar_sizes(more)
    early = np.asarray(base) < t_cut
    assert a[early].tolist() == b[: len(base)][early].tolist()


def test_week_buckets_zero_weeks_and_causal() -> None:
    start = 1_000
    t = [start, start + 1, start + 2 * W + 5]
    c = bc.week_buckets(t, start, start + 3 * W, W)
    assert c.tolist() == [2, 0, 1]
    # partial last week counted; trades outside ignored
    assert bc.week_buckets([start - 1, start + 3 * W], start, start + 3 * W + 1, W).tolist() == [0, 0, 0, 1]
    # weeks before t unaffected by later trades
    c2 = bc.week_buckets([*t, start + 2 * W + 9, start + 2 * W + 10], start, start + 3 * W, W)
    assert c2[:2].tolist() == c[:2].tolist()
    with pytest.raises(ValueError):
        bc.week_buckets(t, 5, 5, W)


def test_held_ms_and_chain_change() -> None:
    assert bc.held_ms([0, 10], [5, 30]) == 25
    with pytest.raises(ValueError):
        bc.held_ms([10], [5])
    rem, add = bc.chain_change([True, True, False, False], [True, False, True, False])
    assert rem.tolist() == [False, True, False, False]
    assert add.tolist() == [False, False, True, False]


def test_cap_boundary_ties() -> None:
    t = [0, 0, 0, H4, H4, H4, 2 * H4]
    score = [2, 2, 2, 2, 2, 2, 2]
    rr = [3.0, 2.0, 2.0, 3.0, 2.5, 2.0, 1.0]
    # bar 0: 2nd and 3rd tie (2, 2.0); bar H4: no tie at the boundary; bar 2*H4 below cap
    assert bc.cap_boundary_ties(t, score, rr, 2) == 1
    assert bc.cap_boundary_ties(t, score, rr, 3) == 0


def test_boot_se_mean_iid_matches_analytic() -> None:
    rng = np.random.default_rng(1)
    v = rng.normal(0.0, 2.0, 4000)
    se = bc.boot_se_mean(v, np.arange(v.size), n_boot=2000, seed=3)
    assert se == pytest.approx(2.0 / math.sqrt(4000), rel=0.1)
    assert math.isnan(bc.boot_se_mean([1.0], [0]))


def test_boot_se_diff_properties() -> None:
    rng = np.random.default_rng(2)
    n = 3000
    v = rng.normal(0.0, 1.0, n)
    a = np.zeros(n, dtype=bool)
    a[: n // 2] = True
    b = ~a
    cl = np.arange(n)
    se = bc.boot_se_diff(v, a, b, cl, n_boot=2000, seed=5)
    assert se == pytest.approx(math.sqrt(1 / (n // 2) + 1 / (n - n // 2)), rel=0.1)
    # shift invariance and scaling
    assert bc.boot_se_diff(v + 7.0, a, b, cl, n_boot=2000, seed=5) == pytest.approx(se, rel=1e-9)
    assert bc.boot_se_diff(3 * v, a, b, cl, n_boot=2000, seed=5) == pytest.approx(3 * se, rel=1e-9)
    # identical masks: the paired difference is exactly zero in every resample
    assert bc.boot_se_diff(v, a, a, cl, n_boot=500, seed=5) == pytest.approx(0.0, abs=1e-12)
    # empty group
    assert math.isnan(bc.boot_se_diff(v, a, np.zeros(n, dtype=bool), cl))


def test_boot_se_diff_subset_scales_with_dropped_share() -> None:
    """Capped chain = uncapped minus a share f of trades: SE of the paired difference ~ f * SE(dropped - kept)."""
    rng = np.random.default_rng(4)
    n = 4000
    v = rng.normal(0.0, 1.0, n)
    full = np.ones(n, dtype=bool)
    kept = np.ones(n, dtype=bool)
    kept[:400] = False  # f = 0.1
    se = bc.boot_se_diff(v, kept, full, np.arange(n), n_boot=2000, seed=6)
    expect = 0.1 * math.sqrt(1 / 400 + 1 / 3600)
    assert se == pytest.approx(expect, rel=0.15)


def test_mde_and_design_effect() -> None:
    assert bc.mde80(0.1) == pytest.approx(0.28016, abs=1e-4)
    assert math.isnan(bc.mde80(math.nan))
    assert bc.design_effect(0.2, 1.0, 100) == pytest.approx(4.0)
    assert math.isnan(bc.design_effect(0.2, 0.0, 100))
