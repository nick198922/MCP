"""Pure analytics of the Polymarket favourites study (analytic references, no network)."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.pm_favorites import (
    FAV_BASKET,
    awaiting_payout,
    cochran_q,
    fav_outcome,
    fav_return,
    favourite,
    in_basket,
    last_print,
    normalised_quotes,
    price_mde,
    state_at,
    worst_losing_streak,
)

YES, NO = "tY", "tN"


def test_favourite_side_and_basket():
    assert favourite(0.93) == ("yes", 0.93)
    side, p = favourite(0.06)
    assert side == "no" and p == pytest.approx(0.94)
    assert favourite(0.5) == ("yes", 0.5)
    assert in_basket(0.88, FAV_BASKET) and in_basket(0.97, FAV_BASKET)
    assert not in_basket(0.8799, FAV_BASKET) and not in_basket(0.9701, FAV_BASKET)
    assert fav_outcome("yes", 1) == 1 and fav_outcome("no", 1) == 0 and fav_outcome("no", 0) == 1


def test_normalised_quotes_overround_removed():
    # three options quoted 0.60 / 0.30 / 0.30 (sum 1.2) -> 0.5 / 0.25 / 0.25
    n = normalised_quotes({"a": 0.60, "b": 0.30, "c": 0.30})
    assert n == pytest.approx({"a": 0.5, "b": 0.25, "c": 0.25})
    assert normalised_quotes({"a": 0.6, "b": None}) is None  # a missing quote of an open option -> skip the event
    assert normalised_quotes({"a": 0.6}) is None


def test_last_print_favourite_equivalent_and_strict():
    trades = [
        {"timestamp": 100, "price": 0.95, "asset": YES},
        {"timestamp": 200, "price": 0.04, "asset": NO},  # NO at 0.04 = YES at 0.96
        {"timestamp": 400, "price": 0.50, "asset": YES},  # after t0
    ]
    lp = last_print(trades, 300, YES, "yes")
    assert lp == {"t": 200, "age": 100, "px": pytest.approx(0.96)}
    strict = last_print(trades, 300, YES, "yes", fav_token=YES)
    assert strict["t"] == 100 and strict["px"] == pytest.approx(0.95)
    no_side = last_print(trades, 300, YES, "no")
    assert no_side["px"] == pytest.approx(0.04)


def test_state_at_ignores_data_after_t0():
    t0 = 10_000
    pts = [{"t": t0 - 600, "p": 0.07}, {"t": t0 - 120, "p": 0.06}]
    trades = [{"timestamp": t0 - 3000, "price": 0.93, "asset": NO}]
    base = state_at(pts, trades, t0, yes_token=YES, no_token=NO)
    assert base["side"] == "no" and base["p_f"] == pytest.approx(0.94)
    assert base["last_px"] == pytest.approx(0.93) and base["fresh"] and base["fresh_strict"]
    later_pts = pts + [{"t": t0 - 30, "p": 0.5}, {"t": t0 + 600, "p": 0.99}]
    later_tr = trades + [{"timestamp": t0 + 1, "price": 0.01, "asset": NO}, {"timestamp": t0 + 900, "price": 0.5, "asset": YES}]
    assert state_at(later_pts, later_tr, t0, yes_token=YES, no_token=NO) == base
    stale = state_at(pts, [{"timestamp": t0 - 7201, "price": 0.93, "asset": NO}], t0, yes_token=YES, no_token=NO)
    assert not stale["fresh"]
    assert state_at([{"t": t0 - 2000, "p": 0.06}], trades, t0, yes_token=YES, no_token=NO) is None  # quote older than 15 min


def test_state_at_override_uses_normalised_quote():
    t0 = 5_000
    s = state_at([{"t": t0 - 100, "p": 0.10}], [], t0, yes_token=YES, no_token=NO, q_override=0.05)
    assert s["q_raw"] == pytest.approx(0.10) and s["p_f"] == pytest.approx(0.95) and not s["fresh"]


def test_fav_return_analytic():
    # entry = max(0.93 + 0.01, 0.945) = 0.945, no fee: win -> 1/0.945 - 1, loss -> -1
    w = fav_return(0.93, 1, half_spread=0.01, last_fav_px=0.945)
    assert w["entry"] == pytest.approx(0.945) and w["ret"] == pytest.approx(1 / 0.945 - 1)
    assert fav_return(0.93, 0)["ret"] == pytest.approx(-1.0)
    # fee 0.0175 * (0.94 * 0.06) per share on entry 0.94
    f = fav_return(0.93, 1, fee_rate=0.0175)
    fee = 0.0175 * 0.94 * 0.06
    assert f["fee"] == pytest.approx(fee) and f["ret"] == pytest.approx(1 / (0.94 + fee) - 1)
    # break-even: expected return is zero when the win rate equals the entry cost
    p_win = 0.94
    assert p_win * fav_return(0.93, 1)["ret"] + (1 - p_win) * fav_return(0.93, 0)["ret"] == pytest.approx(0.0)


def test_price_mde_analytic():
    # 100 single-market events at p = 0.9: sd 0.3, SE = 0.3 / 10, MDE = 2.8 * 0.03
    r = price_mde([[0.9]] * 100)
    assert r["n"] == 100 and r["se"] == pytest.approx(0.03) and r["mde"] == pytest.approx(0.084)
    # two markets per event: full correlation keeps sd 0.3, independence halves the variance
    full = price_mde([[0.9, 0.9]] * 100, within="full")
    ind = price_mde([[0.9, 0.9]] * 100, within="indep")
    assert full["se"] == pytest.approx(0.03)
    assert ind["se"] == pytest.approx(0.03 / math.sqrt(2))
    assert price_mde([])["mde"] == math.inf
    with pytest.raises(ValueError):
        price_mde([[0.9]], within="x")


def test_cochran_q_homogeneous_and_heterogeneous():
    same = cochran_q([0.01, 0.01, 0.01], [0.005, 0.005, 0.005])
    assert same["Q"] == pytest.approx(0.0) and same["I2"] == 0.0 and same["df"] == 2
    het = cochran_q([0.0, 0.1], [0.01, 0.01])
    # pooled 0.05, Q = 2 * (0.05 / 0.01)^2 = 50, I2 = (50 - 1) / 50
    assert het["pooled"] == pytest.approx(0.05) and het["Q"] == pytest.approx(50.0) and het["I2"] == pytest.approx(0.98)


def test_awaiting_payout_and_streak():
    assert awaiting_payout("end_date", 1000, 900)
    assert not awaiting_payout("end_date", 1000, 1100)
    assert not awaiting_payout("game_start", 1000, 900)
    assert worst_losing_streak([1, 0, 0, 1, 0, 0, 0, 1]) == 3
    assert worst_losing_streak([1, 1]) == 0
