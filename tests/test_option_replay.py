"""Tests for the historical option-structure replay (assistant recommendation method)."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.option_replay import (
    build_legs,
    mean_ci,
    method_summary,
    non_overlapping,
    payoff_rel,
    quantile,
    recommend,
    replay_hedged,
    replay_structure,
    replay_unhedged,
    round_trip_fees,
    rv_at,
    spearman,
    strike_roles,
    structure_premium,
    summarize_replay,
    window_starts,
)
from tradedesk.analytics.options import ncdf

STRADDLE = [(1.0, True, 1.0, 0.5), (1.0, False, 1.0, 0.5)]


def test_atm_straddle_premium_matches_closed_form() -> None:
    t = 40 / 365
    # Black-76 ATM straddle / F = 2 * (2 N(sigma sqrt(T) / 2) - 1)
    expected = 2 * (2 * ncdf(0.5 * math.sqrt(t) / 2) - 1)
    assert structure_premium(STRADDLE, t) == pytest.approx(expected, rel=1e-9)
    short = [(k, c, -q, iv) for k, c, q, iv in STRADDLE]
    assert structure_premium(short, t) == pytest.approx(-expected, rel=1e-9)


def test_payoff_and_fees() -> None:
    assert payoff_rel(STRADDLE, 1.1) == pytest.approx(0.1)
    assert payoff_rel(STRADDLE, 0.8) == pytest.approx(0.2)
    condor = [(1.1, True, -1, 0.4), (0.9, False, -1, 0.4), (1.2, True, 1, 0.4), (0.8, False, 1, 0.4)]
    assert payoff_rel(condor, 1.0) == 0.0
    assert payoff_rel(condor, 1.5) == pytest.approx(-0.1)  # capped at wing width
    assert round_trip_fees(condor, 0.0003) == pytest.approx(4 * 2 * 0.0003)


def test_unhedged_flat_path_loses_premium_and_fees() -> None:
    prem = structure_premium(STRADDLE, 40 / 365)
    assert replay_unhedged(STRADDLE, prem, 1.0) == pytest.approx(-prem - 4 * 0.0003)


def test_hedged_flat_path_without_hedge_fee_equals_unhedged() -> None:
    prem = structure_premium(STRADDLE, 40 / 365)
    path = [1.0] * 41
    assert replay_hedged(STRADDLE, prem, path, hedge_fee=0.0) == pytest.approx(replay_unhedged(STRADDLE, prem, 1.0))


def test_hedge_removes_direction_of_deep_itm_call() -> None:
    leg = [(0.01, True, 1.0, 0.3)]  # delta ~ 1: the hedge is short ~1 unit
    t = 30 / 365
    prem = structure_premium(leg, t)
    trend = [1.0 + 0.01 * d for d in range(31)]  # +30 % straight line
    unh = replay_unhedged(leg, prem, trend[-1], fee_per_leg=0.0)
    hed = replay_hedged(leg, prem, trend, fee_per_leg=0.0, hedge_fee=0.0)
    assert unh == pytest.approx(0.30, abs=1e-6)
    assert hed == pytest.approx(0.0, abs=1e-6)


def test_hedged_straddle_gains_when_realized_vol_exceeds_implied() -> None:
    lo_iv = [(1.0, True, 1.0, 0.10), (1.0, False, 1.0, 0.10)]
    t = 30 / 365
    prem = structure_premium(lo_iv, t)
    zigzag = [1.0 + (0.05 if d % 2 else 0.0) for d in range(31)]  # ~ 5 % daily moves, ends at +0 ... +5 %
    calm = [1.0] * 31
    assert replay_hedged(lo_iv, prem, zigzag, hedge_fee=0.0) > 0
    assert replay_hedged(lo_iv, prem, calm, hedge_fee=0.0) < 0
    with pytest.raises(ValueError):
        replay_hedged(lo_iv, prem, [1.0])


def test_rv_and_windows() -> None:
    closes = [100 * math.exp(0.01 * i) for i in range(100)]
    assert rv_at(closes, 30) == pytest.approx(math.sqrt(365) * 0.01)
    assert rv_at(closes, 29) is None
    st = window_starts(80, 40, lambda s: True)
    assert st[0] == 30 and st[-1] == 40  # complete by close 80
    assert non_overlapping(list(range(0, 101)), 40) == [0, 40, 80]
    assert non_overlapping(list(range(0, 101)), 40, offset=5) == [5, 45, 85]
    assert non_overlapping([], 40) == []


def test_stats_helpers() -> None:
    assert quantile([1, 2, 3, 4, 5], 0.5) == 3
    assert quantile([0, 10], 0.05) == pytest.approx(0.5)
    m, lo, hi = mean_ci([1.0, 2.0, 3.0])
    assert m == 2.0 and lo == pytest.approx(2 - 1.96 / math.sqrt(3)) and hi == pytest.approx(2 + 1.96 / math.sqrt(3))
    assert mean_ci([1.0]) is None
    assert spearman([(1, 10), (2, 20), (3, 25), (4, 100)]) == pytest.approx(1.0)
    assert spearman([(1, 3), (2, 2), (3, 1)]) == pytest.approx(-1.0)
    s = summarize_replay([-0.1, 0.2, 0.3, -0.05], [0.2, -0.1])
    assert s["n"] == 4 and s["win"] == 0.5 and s["mean"] == pytest.approx(0.0875)
    assert s["score"] == pytest.approx(s["mean"] / abs(s["q05"]))


def test_recommend_requires_positive_mean_and_ci() -> None:
    sums = {
        "a": {"n": 10, "mean": 0.05, "ci_lo": 0.01, "score": 0.5},
        "b": {"n": 10, "mean": 0.08, "ci_lo": -0.01, "score": 0.9},
        "c": {"n": 10, "mean": -0.02, "ci_lo": -0.05, "score": -0.2},
    }
    assert recommend(sums) == "a"
    assert recommend(sums, require_ci=False) == "b"
    assert recommend({"c": sums["c"]}) is None


def test_method_is_causal() -> None:
    """Results computed with history up to close t do not change when closes after t change."""
    closes = [100 * math.exp(0.02 * math.sin(i / 3.0) + 0.001 * i) for i in range(400)]
    end, h = 250, 40
    starts = window_starts(end, h, lambda s: True)
    legs = [(1.0, True, 1.0, 0.4), (1.0, False, 1.0, 0.4)]
    before = method_summary(legs, closes, h, starts)
    before_h = method_summary(legs, closes, h, starts, hedged=True)
    changed = closes[: end + 1] + [c * 3.0 for c in closes[end + 1 :]]
    assert method_summary(legs, changed, h, starts) == before
    assert method_summary(legs, changed, h, starts, hedged=True) == before_h
    assert max(starts) + h == end


def test_replay_structure_keys_and_skips_incomplete() -> None:
    closes = [100.0] * 50
    res = replay_structure(STRADDLE, closes, 40, [0, 5, 10, 20])
    assert sorted(res) == [0, 5]


def _surface() -> dict:
    from tradedesk.analytics.options import black76_greeks

    F, T, iv = 100.0, 40 / 365, 0.5
    strikes = {}
    for k in range(60, 145, 5):
        strikes[float(k)] = {"mark_iv": iv, "put_delta": black76_greeks(F, k, T, iv, False)["delta"]}
    return {"F": F, "T": T, "strikes": strikes}


def test_strike_roles_and_legs() -> None:
    exp = _surface()
    roles = strike_roles(exp)
    assert roles["atm"][0] == 100.0
    assert roles["p25"][0] < 100 < roles["c25"][0]
    assert roles["p10"][0] < roles["p25"][0] and roles["c10"][0] > roles["c25"][0]
    absl, rel = build_legs("iron_condor", roles, 100.0, spread_vol=0.01)
    assert len(absl) == 4
    # sold legs at mark - 1 vp, bought at mark + 1 vp
    assert {round(x[3], 4) for x in absl if x[2] < 0} == {0.49}
    assert {round(x[3], 4) for x in absl if x[2] > 0} == {0.51}
    assert rel[0][0] == pytest.approx(absl[0][0] / 100.0)
    assert build_legs("long_straddle", {"c25": roles["c25"]}, 100.0) is None
