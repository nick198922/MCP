"""Item 8p: pooled announcement-entry event study (analytics/carry_entry_pool.py)."""

from __future__ import annotations

import pytest

from tradedesk.analytics.carry_entry_pool import (
    DAY_MS,
    announcement_units,
    cluster_bootstrap_mean,
    control_symbols,
    first_trade_in,
    mde80,
    top_contributor,
    unit_delta,
)

D = DAY_MS


def test_units_merge_window_and_period():
    index = {"A": [10 * D, 25 * D, 41 * D, 200 * D], "B": [5 * D], "C": [-3 * D, 400 * D]}
    u = announcement_units(index, 0, 300 * D, perp_end={"A": 50 * D, "B": 4 * D}, data_end_ms=220 * D)
    by = [(x["symbol"], x["pub"] // D, x["n_ann"], x["win_end"] // D, x["has_window"]) for x in u]
    # A: 10 and 25 merge (<= 30 d), 41 starts a new unit (31 d after 10); window capped by perp end 50 / data end 220
    assert by == [("B", 5, 1, 4, False), ("A", 10, 2, 40, True), ("A", 41, 1, 50, True), ("A", 200, 1, 50, False)]
    assert all(x["cluster"] == x["pub"] for x in u)


def test_units_are_causal_in_later_announcements():
    base = {"A": [10 * D], "B": [20 * D]}
    later = {"A": [10 * D, 30 * D, 90 * D], "B": [20 * D, 100 * D], "C": [95 * D]}
    t = 60 * D
    a = [x for x in announcement_units(base, 0, 300 * D) if x["pub"] <= t]
    b = [x for x in announcement_units(later, 0, 300 * D) if x["pub"] <= t]
    for x in (a, b):
        for y in x:
            y.pop("n_ann")  # merged count of a later publication is bookkeeping, not part of the window
    assert a == b


def test_control_symbols_horizon():
    index = {"A": [100 * D], "B": [100 * D + 181 * D], "C": [100 * D - 180 * D], "D": []}
    assert control_symbols(index, ["A", "B", "C", "D", "E"], "A", 100 * D) == ["B", "D", "E"]


def test_first_trade_in():
    tr = [{"open_ts": 5}, {"open_ts": 3}, {"open_ts": 9}]
    assert first_trade_in(tr, 4, 10)["open_ts"] == 5
    assert first_trade_in(tr, 10, 20) is None


def test_unit_delta_and_mde():
    assert unit_delta(0.1, [0.2, 0.0, 0.4]) == pytest.approx(-0.1)
    assert unit_delta(0.1, []) is None
    assert mde80(0.5, 16) == pytest.approx(0.35)
    assert mde80(0.5, 0) is None


def test_cluster_bootstrap_constant_and_sign():
    r = cluster_bootstrap_mean([-0.2] * 6, [1, 1, 2, 2, 3, 3], n_boot=500)
    assert r["mean"] == pytest.approx(-0.2) and r["ci"] == pytest.approx([-0.2, -0.2]) and r["p_ge0"] == 0.0
    assert r["n"] == 6 and r["k"] == 3


def test_cluster_bootstrap_variance_matches_cluster_means():
    # two clusters of equal size with means -1 and +1: replicate mean = (#draws of +1 cluster - #draws of -1) / 2
    vals = [-1.0, -1.0, 1.0, 1.0]
    r = cluster_bootstrap_mean(vals, ["a", "a", "b", "b"], n_boot=20_000, seed=1)
    assert r["mean"] == 0.0
    assert r["ci"] == pytest.approx([-1.0, 1.0])
    assert r["p_ge0"] == pytest.approx(0.75, abs=0.02)  # P(mean >= 0) = P(at least one 'b' of 2 draws) = 3/4
    with pytest.raises(ValueError):
        cluster_bootstrap_mean([1.0], [1, 2])
    assert cluster_bootstrap_mean([], [])["mean"] is None


def test_top_contributor():
    assert top_contributor([-0.5, 0.1, -0.2, -0.4], ["x", "y", "z", "z"]) == "z"
    assert top_contributor([0.5, 0.1, -0.2], ["x", "y", "z"]) == "x"
    assert top_contributor([], []) is None
