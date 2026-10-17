"""Tests for analytics/course_s13_drawdown.py (S13, step 6 of IDEA-20261016-course-setups-crypto)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics.course_s13_drawdown import (
    ath_cross_events,
    cell_stats,
    cluster_bootstrap_mean,
    drawdown,
    episodes,
    forward_return,
    local_cross_events,
    net_return,
    path_after,
    rolling_max,
    running_max,
    shift_null,
    sma,
    state_days,
)

NAN = np.nan


def test_running_and_rolling_max_skip_missing_days() -> None:
    c = np.array([NAN, 10.0, 12.0, NAN, 11.0, 9.0, 13.0])
    assert np.isnan(running_max(c)[0])
    assert running_max(c)[1:].tolist() == [10, 12, 12, 12, 12, 13]
    r = rolling_max(c, 3)
    assert np.isnan(r[0])
    assert r[1:].tolist() == [10, 12, 12, 12, 11, 13]
    assert drawdown(np.array([9.0]), np.array([12.0]))[0] == pytest.approx(-0.25)


def test_sma_needs_enough_closes() -> None:
    c = np.arange(1.0, 11.0)
    s = sma(c, 4, min_valid=4)
    assert np.isnan(s[:3]).all()
    assert s[3] == pytest.approx(2.5)
    assert s[9] == pytest.approx(8.5)
    c2 = c.copy()
    c2[8] = NAN
    assert np.isnan(sma(c2, 4, min_valid=4)[9])
    assert sma(c2, 4, min_valid=3)[9] == pytest.approx((7 + 8 + 10) / 3)


def test_ath_cross_one_event_per_episode() -> None:
    # max 100 -> -12 % (event) -> -20 % (same episode) -> back to -5 % -> -11 % again (no new max: no event)
    # -> new max 110 -> -10.9 % (event)
    c = np.array([100, 95, 88, 80, 95, 89, 105, 110, 98, 97.0])
    assert ath_cross_events(c, 0.10).tolist() == [2, 8]
    assert ath_cross_events(c, 0.15).tolist() == [3]
    assert ath_cross_events(c, 0.30).tolist() == []


def test_ath_cross_start_needs_a_real_crossing() -> None:
    c = np.array([100, 85, 84, 92, 86, 120.0, 100.0])
    # already at -15 % on the start day 2: not an event; recovers to -8 % and crosses again on day 4
    assert ath_cross_events(c, 0.10, start=2).tolist() == [4, 6]
    # crossings before start do not use up the first event
    assert ath_cross_events(c, 0.10, start=0).tolist() == [1, 6]
    assert ath_cross_events(c, 0.15, start=2).tolist() == [6]


def test_local_cross_rearm_and_cooldown() -> None:
    c = np.full(40, 100.0)
    c[5] = 89  # -11 % from the 3-day max: event
    c[6] = 99  # recovers above -5 %: re-armed, but cooldown 10 days blocks the next crossing
    c[8] = 89
    c[20] = 89  # re-armed and past the cooldown: event
    assert local_cross_events(c, 0.10, window=3, rearm=0.5, cooldown=10).tolist() == [5, 20]
    assert local_cross_events(c, 0.10, window=3, rearm=0.5, cooldown=1).tolist() == [5, 8, 20]


def test_state_days() -> None:
    c = np.array([100, 95, 88, 80, 95, 89, 105.0])
    assert state_days(c, 0.10).tolist() == [2, 3, 5]
    assert state_days(c, 0.10, start=3).tolist() == [3, 5]


def test_events_before_t_do_not_depend_on_later_data() -> None:
    rng = np.random.default_rng(1)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.04, 800)))
    cut = 500
    c2 = c.copy()
    c2[cut + 1 :] = c2[cut + 1 :] * np.exp(rng.normal(0, 0.3, 800 - cut - 1))
    c2[cut + 10] = NAN
    for th in (0.10, 0.15, 0.30):
        a, b = ath_cross_events(c, th, start=20), ath_cross_events(c2, th, start=20)
        assert a[a <= cut].tolist() == b[b <= cut].tolist()
        a, b = local_cross_events(c, th, start=20), local_cross_events(c2, th, start=20)
        assert a[a <= cut].tolist() == b[b <= cut].tolist()
        a, b = state_days(c, th), state_days(c2, th)
        assert a[a <= cut].tolist() == b[b <= cut].tolist()
    np.testing.assert_array_equal(running_max(c)[: cut + 1], running_max(c2)[: cut + 1])
    np.testing.assert_array_equal(rolling_max(c, 90)[: cut + 1], rolling_max(c2, 90)[: cut + 1])
    np.testing.assert_array_equal(sma(c, 200)[: cut + 1], sma(c2, 200)[: cut + 1])
    # forward returns ending at or before the cut are unchanged too
    g1, _ = forward_return(c, 30)
    g2, _ = forward_return(c2, 30)
    np.testing.assert_array_equal(g1[: cut - 30 + 1], g2[: cut - 30 + 1])


def test_forward_return_gaps_and_end() -> None:
    c = np.array([100, 110, NAN, NAN, 120, 130.0])
    g, ratio = forward_return(c, 2, max_gap=1)
    assert g[0] == pytest.approx(0.10)  # day 2 missing -> day 1 close (gap 1)
    assert np.isnan(g[1])  # day 3 missing, day 2 missing -> gap 2 > 1
    assert g[2] != g[2]  # entry day missing
    assert g[3] != g[3]
    assert np.isnan(g[4]) and np.isnan(g[5])  # past the end
    assert ratio[0] == pytest.approx(1.10)
    g3, _ = forward_return(c, 3, max_gap=3)
    assert g3[0] == pytest.approx(0.10)  # day 3 missing -> last close <= day 3 is day 1
    assert g3[1] == pytest.approx(120 / 110 - 1)


def test_net_return_costs_both_sides() -> None:
    # entry 100, exit 110, 0.12 % per side: 0.10 - 0.0012 * (1 + 1.1)
    assert net_return(np.array([0.10]), 0.0012)[0] == pytest.approx(0.10 - 0.0012 * 2.1)
    assert net_return(np.array([0.0]), 0.0012)[0] == pytest.approx(-0.0024)


def test_path_after() -> None:
    c = np.array([100, 90, 95, NAN, 120, 80.0])
    mae, rec = path_after(c, 0, 3, 115.0)
    assert mae == pytest.approx(-0.10) and rec is False
    mae, rec = path_after(c, 0, 4, 115.0)
    assert rec is True
    assert path_after(c, 0, 5, 200.0)[0] == pytest.approx(-0.20)


def test_episodes_merge_overlapping_windows() -> None:
    assert episodes(np.array([0, 10, 40, 69, 70, 200]), 30).tolist() == [0, 0, 1, 1, 1, 2]
    assert episodes(np.array([], dtype=int), 30).tolist() == []


def test_cluster_bootstrap_mean() -> None:
    v = np.array([1.0, 1.0, 1.0, 3.0])
    out = cluster_bootstrap_mean(v, np.array([0, 0, 0, 1]), n_boot=2000)
    assert out["mean"] == pytest.approx(1.5)
    assert out["ci"][0] == pytest.approx(1.0) and out["ci"][1] == pytest.approx(3.0)
    assert cluster_bootstrap_mean(v, np.zeros(4), n_boot=10)["ci"] is None


def test_shift_null_is_exact_rotation() -> None:
    v = np.arange(10.0)
    null = shift_null(v, np.array([0, 1]), min_shift=2)
    # shifts 2..8: positions {s, s+1} mod 10
    exp = [(s + (s + 1) % 10) / 2 for s in range(2, 9)]
    assert null.tolist() == pytest.approx(exp)
    # the mean over all shifts (including 0 and 1, 9) equals the overall mean
    full = shift_null(v, np.array([0, 1]), min_shift=0)
    assert full[:-1].mean() == pytest.approx(v.mean())


def test_cell_stats_detects_a_planted_effect_and_not_a_null() -> None:
    rng = np.random.default_rng(7)
    n = 3000
    net = rng.normal(0.0, 0.05, n)
    ev = np.arange(100, 2900, 97)
    eligible = np.ones(n, dtype=bool)
    eligible[:50] = False
    net[ev] += 0.08
    st = cell_stats(net, ev, eligible, 30, n_boot=2000)
    assert st["n"] == ev.size and st["n_ep"] == ev.size  # spacing 97 >= 30: all separate
    assert st["delta"] == pytest.approx(net[ev].mean() - net[50:].mean())
    assert st["p"] < 0.01 and st["ci_delta"][0] > 0
    assert st["mde"] == pytest.approx(2.8 * st["se"])
    net[ev] -= 0.08
    st0 = cell_stats(net, ev, eligible, 30, n_boot=2000)
    assert st0["p"] > 0.05 and st0["ci_delta"][0] < 0 < st0["ci_delta"][1]
    # events outside the eligible mask or with a NaN return are dropped
    net[ev[0]] = NAN
    st1 = cell_stats(net, np.concatenate([[10], ev]), eligible, 30, n_boot=200)
    assert st1["n"] == ev.size - 1


def test_cell_stats_empty() -> None:
    st = cell_stats(np.zeros(100), np.array([], dtype=int), np.ones(100, dtype=bool), 30)
    assert st["n"] == 0 and st["p"] is None
