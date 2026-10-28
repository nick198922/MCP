"""Tests for analytics.strangle_manage (long strangle, sell the far strike after a move)."""

from __future__ import annotations

import copy
import math

import pytest

from tradedesk.analytics.options import black76_price
from tradedesk.analytics.strangle_manage import (
    DAY_MS,
    Params,
    block_draws,
    build_episode,
    choose_series,
    control_short,
    cvar,
    dedup,
    fee_usd,
    find_trigger,
    h1_pnl,
    h2_verdict,
    leg_value,
    mde,
    nontrigger,
    pct_ci,
    pick_strike,
)

H9 = 9 * 3_600_000
EXP = 20 * DAY_MS + 8 * 3_600_000  # expiry 08:00 UTC of day 20


def surface(day: int, spot: float, iv: float = 0.6, strikes: list[float] | None = None) -> dict:
    ks = strikes if strikes is not None else [spot * x for x in (0.90, 0.92, 0.97, 1.0, 1.03, 1.08, 1.10, 1.12)]
    return {
        "day": day * DAY_MS,
        "t_ms": day * DAY_MS + H9,
        "spot": spot,
        "expiries": {EXP: {"F": spot, "strikes": {round(k, 6): {"mark_iv": iv} for k in ks}}},
    }


def path(spots: list[float], iv: float = 0.6, grid: list[float] | None = None) -> list[dict]:
    g = grid or [x * 1000.0 for x in range(80, 121)]
    return [surface(i, s, iv, g) for i, s in enumerate(spots)]


def test_leg_value_is_black76_usd():
    v = leg_value(100.0, 100.0, 0.05, 0.5, True, 100.0)
    assert v == pytest.approx(black76_price(100.0, 100.0, 0.05, 0.5, True))
    # ATM approximation 0.4 * sigma * sqrt(T) * F
    assert v == pytest.approx(0.4 * 0.5 * math.sqrt(0.05) * 100.0, rel=0.01)


def test_fee_cap():
    p = Params()
    assert fee_usd(1000.0, 100_000.0, p) == pytest.approx(30.0)
    assert fee_usd(100.0, 100_000.0, p) == pytest.approx(12.5)


def test_pick_strike_band_and_tie():
    ks = [90.0, 92.0, 93.0, 97.0]
    assert pick_strike(ks, 100.0, 0.92, 0.015) == 92.0
    assert pick_strike(ks, 100.0, 0.95, 0.015) is None
    assert pick_strike([91.0, 93.0], 100.0, 0.92, 0.015) == 91.0  # tie -> lower strike


def test_choose_series_band_and_mid():
    exps = {14 * DAY_MS + 8 * 3_600_000: 1, 21 * DAY_MS: 1, 30 * DAY_MS: 1}
    assert choose_series(exps, 0, (14, 21), 17.5) == 14 * DAY_MS + 8 * 3_600_000
    assert choose_series(exps, 0, (22, 25), 23) is None


def test_find_trigger_first_crossing_and_side():
    assert find_trigger([100, 101, 102, 103.5, 90], 0, 5, 0.03) == (3, "up")
    assert find_trigger([100, 99, 96.9], 0, 5, 0.03) == (2, "down")
    assert find_trigger([100, 101, 102, 101, 100, 99, 50], 0, 5, 0.03) is None  # day 6 is outside the window
    assert find_trigger([100, None, 96.0], 0, 5, 0.03) == (2, "down")


def test_episode_flat_iv_ratio_equals_model():
    # flat IV, no smile: R must equal the unchanged-IV model, so dR = 0
    days = path([100_000, 101_000, 104_000, 104_000, 104_000, 104_000, 104_000])
    ep = build_episode(days, 0, Params())
    assert ep["status"] == "ok"
    assert ep["side"] == "up" and ep["j"] == 2
    assert ep["K_c"] == 103_000.0 and ep["K_s"] == 108_000.0
    assert ep["dR"] == pytest.approx(0.0, abs=1e-12)
    tau_t = (EXP - H9) / (365 * DAY_MS)
    tau_d = (EXP - 2 * DAY_MS - H9) / (365 * DAY_MS)
    vb = black76_price(100_000, 103_000, tau_t, 0.6, True)
    vs = black76_price(104_000, 108_000, tau_d, 0.6, True)
    assert ep["R"] == pytest.approx(vs / vb)


def test_episode_iv_reaction_raises_ratio():
    days = path([100_000, 97_000, 96_500, 96_500, 96_500, 96_500, 96_500])
    days[1]["expiries"][EXP]["strikes"][92_000.0]["mark_iv"] = 0.66
    ep = build_episode(days, 0, Params())
    assert ep["side"] == "down" and ep["K_s"] == 92_000.0
    assert ep["iv_s"] - ep["iv_mod"] == pytest.approx(0.06)
    assert ep["dR"] > 0 and ep["R"] > ep["R_mod"]
    assert ep["R_net"] < ep["R"]


def test_episode_missing_sell_mark():
    g = [x * 1000.0 for x in range(80, 121) if x not in (91, 92, 93)]
    days = path([100_000, 96_000, 96_000, 96_000, 96_000, 96_000, 96_000], grid=g)
    assert build_episode(days, 0, Params())["status"] == "no_sell_mark"


def test_episode_causal_future_data_does_not_change_h2():
    days = path([100_000, 101_000, 104_000, 104_000, 104_000, 104_000, 104_000, 104_000])
    base = build_episode(days, 0, Params())
    later = copy.deepcopy(days)
    for d in later[3:]:
        d["spot"] *= 0.5
        for e in d["expiries"].values():
            for s in e["strikes"].values():
                s["mark_iv"] = 2.0
    assert build_episode(later, 0, Params()) == base


def test_h1_pnl_identities():
    p = Params(cost_vp=0.0, fee_btc=0.0)
    days = path([100_000, 101_000, 104_000, 104_000, 104_000, 104_000, 104_000])
    ep = build_episode(days, 0, p)
    s_exp = 120_000.0
    r = h1_pnl(ep, days[ep["j"]], s_exp, p)
    # g - a is the short leg; at 120k the short call 108k pays 12k
    assert (r["g"] - r["a"]) * ep["S_T"] == pytest.approx(ep["V_s"] - 12_000.0)
    # hold: two long legs, call 103k pays 17k at 120k
    assert r["a"] * ep["S_T"] == pytest.approx(-ep["V_p"] - ep["V_c"] + 17_000.0)
    assert r["short"] * ep["S_D"] == pytest.approx(ep["V_s"] - 12_000.0)


def test_nontrigger_and_control_short():
    spots = [100.0, 101.0, 100.5, 102.0, 101.0, 100.0, 104.0]
    assert nontrigger(spots, 5, 5, 0.03)
    assert not nontrigger(spots, 6, 5, 0.03)  # 104 / 100.5 - 1 > 3 %
    assert not nontrigger(spots, 2, 5, 0.03)  # not enough history
    day = surface(7, 100_000, 0.5, [x * 1000.0 for x in range(80, 121)])
    p = Params(cost_vp=0.0, fee_btc=0.0)
    v = control_short(day, "down", 0.95, lambda e: 100_000.0, p)
    tau = (EXP - 7 * DAY_MS - H9) / (365 * DAY_MS)
    assert v == pytest.approx(black76_price(100_000, 95_000, tau, 0.5, False) / 100_000)
    assert control_short(day, "down", 0.95, lambda e: None, p) is None


def test_dedup_cvar_bootstrap_mde_verdict():
    rows = [{"D": 1, "x": 1.0}, {"D": 1, "x": 3.0}, {"D": 2, "x": None}, {"D": 2, "x": 5.0}]
    d = dedup(rows, lambda r: r["D"], ("x",))
    assert [(r["key"], r["n_ep"], r["x"]) for r in d] == [(1, 2, 2.0), (2, 2, 5.0)]
    assert cvar([float(i) for i in range(100)], 0.05) == pytest.approx(2.0)
    assert cvar([5.0, 1.0], 0.05) == 1.0
    draws = block_draws([1.0] * 50, lambda v: sum(v) / len(v), block=5, n_boot=200)
    assert pct_ci(draws) == (1.0, 1.0)
    assert mde(0.4, 100) == pytest.approx(0.112)
    assert h2_verdict(0.95, (0.80, 1.10), coverage=0.9, n=100) == "✓"
    assert h2_verdict(0.80, (0.70, 0.89), coverage=0.9, n=100) == "✗"
    assert h2_verdict(0.88, (0.70, 1.00), coverage=0.9, n=100) == "≈"
    assert h2_verdict(0.95, (0.80, 1.10), coverage=0.5, n=100) == "◯"
    assert h2_verdict(0.95, (0.80, 1.10), coverage=0.9, n=20) == "◯"
