"""Tests for analytics.option_condor (short iron condors, structural-edges-depth item 7)."""

from __future__ import annotations

import copy
import math
from statistics import NormalDist

import pytest

from tradedesk.analytics.option_condor import (
    block_bootstrap_mean,
    capital_paths,
    cvar,
    dvol_size,
    exec_legs,
    nearest_fresh,
    permutation_band,
    plan_condor,
    ruin_gates,
    scaled_minus_equal,
    settle_condor,
    strike_for_delta,
)
from tradedesk.analytics.put_skew import inverse_price, trailing_percentile

DAY = 86_400_000
HOUR = 3_600_000
D0 = 1_700_006_400_000 - (1_700_006_400_000 % DAY)  # some UTC midnight
S = 50_000.0


def flat_snap(day: int, expiry: int, sigma: float, *, step: float = 500.0, lo: float = 0.70, hi: float = 1.30, sell=None, buy=None) -> dict:
    t = day + 9 * HOUR
    T = (expiry - t) / (365.0 * DAY)
    strikes = {}
    k = math.ceil(lo * S / step) * step
    while k <= hi * S:
        strikes[k] = {"mark_iv": sigma, "sell_iv": sell, "buy_iv": buy, "put_delta": 0.0, "n": 1, "n_put": 1, "mark_age_h": 0.1}
        k += step
    return {"day": day, "t_ms": t, "spot": S, "expiries": {expiry: {"ratio": 1.0, "F": S, "T": T, "n_prints": 10, "strikes": strikes}}}


def analytic_delta_strike(F: float, sigma: float, T: float, put_delta: float) -> float:
    d1 = NormalDist().inv_cdf(1.0 + put_delta)  # put delta = N(d1) - 1
    return F * math.exp(-d1 * sigma * math.sqrt(T) + 0.5 * sigma * sigma * T)


def test_strike_for_delta_matches_flat_black76() -> None:
    e = D0 + 7 * DAY + 8 * HOUR
    snap = flat_snap(D0, e, 0.50)
    x = snap["expiries"][e]
    kp = strike_for_delta(x, -0.10, False)
    kc = strike_for_delta(x, 0.10, True)
    assert kp == pytest.approx(analytic_delta_strike(S, 0.5, x["T"], -0.10), rel=1e-9)
    assert kc == pytest.approx(analytic_delta_strike(S, 0.5, x["T"], -0.90), rel=1e-9)  # call 0.10 = put -0.90
    assert kp < S < kc


def test_nearest_fresh_band() -> None:
    x = {"F": S, "strikes": {48_000.0: {}, 52_000.0: {}}}
    assert nearest_fresh(x, 48_400.0, S, 0.015) == 48_000.0
    assert nearest_fresh(x, 48_750.0, S, 0.015) == 48_000.0  # 750 = 1.5 % of 50 000
    assert nearest_fresh(x, 49_000.0, S, 0.015) is None  # 1000 > 750
    assert nearest_fresh({"F": S, "strikes": {}}, S, S, 0.015) is None


def test_plan_condor_legs_and_floor() -> None:
    e = D0 + 7 * DAY + 8 * HOUR
    p, why = plan_condor(flat_snap(D0, e, 0.50), e)
    assert why == ""
    assert p["K"]["lp"] == pytest.approx(0.85 * S) and p["K"]["lc"] == pytest.approx(1.15 * S)
    assert not p["floor"]["lp"] and not p["floor"]["lc"]
    t = p["target"]["sp"]
    assert abs(p["K"]["sp"] - t) <= 250.0 + 1e-9  # nearest of a 500 grid
    # very high vol: the 10-delta put lies beyond 0.85 S -> wing floored at K_sp - w/3 S
    p2, why2 = plan_condor(flat_snap(D0, e, 1.60, lo=0.4, hi=1.9), e)
    assert why2 == ""
    assert p2["floor"]["lp"] and p2["K"]["lp"] == pytest.approx(p2["K"]["sp"] - 0.05 * S, abs=250.0)
    # no strikes far enough -> long put leg missing
    p3, why3 = plan_condor(flat_snap(D0, e, 0.50, lo=0.9, hi=1.3), e)
    assert p3 is None and why3 == "long put: no fresh strike"


def test_exec_legs_models_and_fallback() -> None:
    e = D0 + 7 * DAY + 8 * HOUR
    p, _ = plan_condor(flat_snap(D0, e, 0.50, sell=0.48, buy=None), e)
    legs, fb = exec_legs(p, "side")
    ivs = {(K, q): iv for K, _, q, iv in legs}
    assert fb == 2  # both bought legs fall back to mark + 1.5 vp
    assert ivs[(p["K"]["sp"], -1.0)] == 0.48 and ivs[(p["K"]["lp"], 1.0)] == pytest.approx(0.515)
    assert exec_legs(p, "side_only") == ([], 0)
    legs_m, fb_m = exec_legs(p, "mark")
    assert fb_m == 0 and [iv for *_, q, iv in legs_m if q < 0] == pytest.approx([0.485, 0.485])


def _settle(delivery: float, sigma: float = 0.5) -> tuple[dict, dict]:
    e = D0 + 7 * DAY + 8 * HOUR
    snap = flat_snap(D0, e, sigma)
    p, _ = plan_condor(snap, e)
    legs, _ = exec_legs(p, "mark", hs=0.0)
    r = settle_condor({D0: snap}, p, legs, delivery, option_fee=0.0, delivery_fee=0.0)
    return p, r


def test_settle_condor_analytic_payoffs() -> None:
    p, r = _settle(S)
    x = flat_snap(D0, p["expiry"], 0.5)["expiries"][p["expiry"]]
    credit = sum(
        -q * inverse_price(S, p["K"][k], x["T"], 0.5, c)
        for k, c, q in (("lp", False, 1), ("sp", False, -1), ("sc", True, -1), ("lc", True, 1))
    )
    assert r["premium"] == pytest.approx(credit)
    assert r["pnl"] == pytest.approx(credit)  # all legs expire worthless
    assert r["max_loss"] == pytest.approx(p["width"] / S - credit)
    _, r_crash = _settle(0.5 * S)  # below the long put: loss capped at the put-side width
    assert r_crash["pnl"] == pytest.approx(credit - (p["K"]["sp"] - p["K"]["lp"]) / S)
    assert r_crash["pnl"] >= -r_crash["max_loss"] - 1e-12
    assert r_crash["payoff_call"] == 0.0 and r_crash["payoff_put"] < 0


def test_settle_fees_and_no_lookahead() -> None:
    e = D0 + 7 * DAY + 8 * HOUR
    snap = flat_snap(D0, e, 0.5)
    p, _ = plan_condor(snap, e)
    legs, _ = exec_legs(p, "mark")
    later = flat_snap(D0 + 2 * DAY, e, 0.5)
    r1 = settle_condor({D0: snap, D0 + 2 * DAY: later}, p, legs, S)
    later2 = copy.deepcopy(later)
    for v in later2["expiries"][e]["strikes"].values():
        v["mark_iv"] = 1.5
    later2["spot"] = 0.8 * S
    r2 = settle_condor({D0: snap, D0 + 2 * DAY: later2}, p, legs, S)
    assert r1["pnl"] == pytest.approx(r2["pnl"])  # hold-to-delivery P&L ignores later marks
    assert r1["pnl"] == pytest.approx(r1["premium"] - r1["costs"])
    assert r1["costs"] == pytest.approx(4 * 0.0003, rel=0.5)  # 4 legs, fee capped at 12.5 % of cheap wings
    assert settle_condor({D0: snap}, p, [(1.0, True, -1.0, 0.005)], S) is None


def test_dvol_sizes_have_no_lookahead() -> None:
    series = [(D0 + i * DAY, 0.4 + 0.001 * ((i * 37) % 101)) for i in range(400)]
    pct = trailing_percentile(series, window_ms=365 * DAY, min_obs=180)
    changed = series[:300] + [(t, 9.9) for t, _ in series[300:]]
    pct2 = trailing_percentile(changed, window_ms=365 * DAY, min_obs=180)
    for t, _ in series[:300]:
        assert pct.get(t) == pct2.get(t)
    assert [dvol_size(v) for v in (0.0, 0.39, 0.4, 0.79, 0.8, 1.0)] == [0.5, 0.5, 1.0, 1.0, 1.5, 1.5]


def test_scaled_minus_equal_and_permutation() -> None:
    assert scaled_minus_equal([0.01, -0.02], [1.0, 1.0], 1.0) == [0.0, 0.0]
    d = scaled_minus_equal([0.01, -0.02], [1.5, 0.5], 1.0)
    assert d == pytest.approx([0.005, 0.01])
    lo, hi = permutation_band([0.01, -0.02, 0.0, 0.03], [0.5, 1.0, 1.5, 1.0], 1.0, n_perm=200)
    assert lo <= hi


def test_block_bootstrap_mean_reference() -> None:
    r = block_bootstrap_mean([0.01] * 50, n_boot=500)
    assert r["mean"] == pytest.approx(0.01) and r["ci"] == pytest.approx((0.01, 0.01)) and r["p"] == 0.0
    r2 = block_bootstrap_mean([1.0, -1.0] * 100, block=1, n_boot=2000)
    assert abs(r2["mean"]) < 1e-12 and r2["p"] > 0.5 and r2["ci"][0] < 0 < r2["ci"][1]


def test_ruin_gates_reference() -> None:
    assert ruin_gates(-0.1065, 0.0040) == {"d": True, "d_prime": True}  # weekly-straddle-vrp numbers
    assert ruin_gates(-0.06, 0.005) == {"d": False, "d_prime": False}  # 0.06 < 16 * 0.005
    assert ruin_gates(-0.06, -0.001)["d_prime"]  # no positive mean to recover with


def test_cvar_and_capital_paths() -> None:
    assert cvar([-0.05, -0.01, 0.0, 0.02] * 5, 0.05) == pytest.approx(-0.05)
    c = capital_paths([0.01] * 30, n_paths=100)
    assert c["p_dd"] == 0.0 and c["median"] == pytest.approx(1.01**52 - 1.0)
    c2 = capital_paths([-0.01] * 30, n_paths=50)
    assert c2["p_dd"] == 1.0  # 1 - 0.99^52 = 41 % drawdown
