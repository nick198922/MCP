"""Tests for tradedesk.analytics.pm_longdated (IDEA-20261025-pm-longdated-tail-no, Step 0)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics.pm_longdated import (
    DAY_S,
    design_effect,
    eligible_long,
    fresh_trade_q,
    hold_days,
    in_tail,
    long_lived,
    mde_from_se,
    month_key,
    null_power,
    simulate_null_outcomes,
    t0_sched,
    tail_bucket,
)

T = 1_760_000_000  # 2025-10-09 08:53:20 UTC


def _m(start: int, closed: int, T_: int = T) -> dict:
    return {"T": T_, "start": start, "closed": closed}


def test_t0_is_scheduled_end_minus_days():
    assert t0_sched(T, 30) == T - 30 * DAY_S


def test_long_lived_boundary():
    assert long_lived(_m(T - 60 * DAY_S, T), 60)
    assert not long_lived(_m(T - 60 * DAY_S + 1, T), 60)


def test_eligible_uses_close_only_to_know_open_at_t0():
    t0 = t0_sched(T, 30)
    # listed 8 days before t0, resolved early (YES) 1 day after t0: stays in the sample
    assert eligible_long(_m(t0 - 8 * DAY_S, t0 + DAY_S), t0)
    # closed at t0: not open at t0
    assert not eligible_long(_m(t0 - 8 * DAY_S, t0), t0)
    # listed only 6 days before t0
    assert not eligible_long(_m(t0 - 6 * DAY_S, T), t0)


def test_tail_and_buckets():
    assert in_tail(0.03) and in_tail(0.30) and not in_tail(0.029) and not in_tail(None)
    assert tail_bucket(0.03) == "0.03-0.08"
    assert tail_bucket(0.08) == "0.08-0.15"
    assert tail_bucket(0.30) == "0.15-0.30"
    assert tail_bucket(0.31) is None


def test_fresh_trade_q_age_and_yes_equivalent():
    t0 = T
    trades = [
        {"timestamp": t0 - 3 * DAY_S, "price": 0.9, "asset": "no"},
        {"timestamp": t0 - 5 * DAY_S, "price": 0.2, "asset": "yes"},
    ]
    lt = fresh_trade_q(trades, t0, "yes")
    assert lt is not None and lt["yes_px"] == pytest.approx(0.1) and lt["age"] == 3 * DAY_S
    assert fresh_trade_q(trades, t0, "yes", max_age_days=2) is None


def test_fresh_trade_q_ignores_prints_after_t0():
    """Results at t0 do not change when data after t0 changes."""
    t0 = T
    base = [{"timestamp": t0 - DAY_S, "price": 0.12, "asset": "yes"}]
    a = fresh_trade_q(base, t0, "yes")
    b = fresh_trade_q([*base, {"timestamp": t0 + 1, "price": 0.99, "asset": "yes"}], t0, "yes")
    assert a == b


def test_month_key():
    assert month_key(T) == "2025-10"


def test_simulate_null_negrisk_one_winner_and_frequency():
    rng = np.random.default_rng(1)
    qs = {"a": 0.5, "b": 0.3, "c": 0.1}
    wins = {"a": 0, "b": 0, "c": 0}
    n = 20_000
    for _ in range(n):
        y = simulate_null_outcomes(qs, True, rng)
        assert sum(y.values()) <= 1
        for k in y:
            wins[k] += y[k]
    for k, q in qs.items():
        assert wins[k] / n == pytest.approx(q, abs=0.015)


def test_simulate_null_negrisk_overround_normalised():
    rng = np.random.default_rng(2)
    qs = {"a": 0.8, "b": 0.4}  # sum 1.2 -> exactly one winner with p = q / 1.2
    n = 10_000
    a = sum(simulate_null_outcomes(qs, True, rng)["a"] for _ in range(n))
    assert a / n == pytest.approx(0.8 / 1.2, abs=0.015)


def test_null_power_binary_matches_bernoulli_se():
    # 400 independent binary events with q = 0.1: SD of q - y = 0.3, SE = 0.3 / 20 = 0.015
    evs = [{"event_id": str(i), "month": f"2025-{i % 12 + 1:02d}", "neg_risk": False, "qs": {"m": 0.1}, "tail": ["m"]} for i in range(400)]
    pw = null_power(evs, n_sim=300, seed=3)
    assert pw["sd_event"] == pytest.approx(0.3, rel=0.05)
    assert pw["se_sim"] == pytest.approx(0.015, rel=0.15)
    assert pw["se_cluster"] == pytest.approx(0.015, rel=0.15)
    assert pw["n_eff_sim"] == pytest.approx(400, rel=0.3)


def test_null_power_negrisk_tail_markets_average_within_event():
    # one event, two tail options of a neg-risk event with q = 0.1 each: event gap = 0.1 - (y1 + y2) / 2
    evs = [
        {
            "event_id": str(i),
            "month": "2025-01" if i % 2 else "2025-02",
            "neg_risk": True,
            "qs": {"a": 0.1, "b": 0.1, "c": 0.8},
            "tail": ["a", "b"],
        }
        for i in range(300)
    ]
    pw = null_power(evs, n_sim=200, seed=4)
    # Var((y1 + y2) / 2) with one winner: P(win in {a,b}) = 0.2 -> values 0.5 w.p. 0.2 -> var = 0.25 * 0.2 * 0.8 = 0.04
    assert pw["sd_event"] == pytest.approx(math.sqrt(0.04), rel=0.07)


def test_null_power_too_few():
    assert null_power([], n_sim=5)["sd_event"] is None


def test_design_effect_kish():
    assert design_effect([10, 10], 0.0) == 1.0
    assert design_effect([10, 10], 0.1) == pytest.approx(1.9)
    assert design_effect([1, 3], 0.5) == pytest.approx(1 + (10 / 4 - 1) * 0.5)


def test_mde_and_hold_days():
    assert mde_from_se(0.01) == pytest.approx(0.028)
    assert hold_days(0, 15 * DAY_S) == 15.0
    assert hold_days(10, 0) == 0.0
