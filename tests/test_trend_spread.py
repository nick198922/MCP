"""Tests for analytics.trend_spread (playbook rule P3 on history)."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics import trend_spread as ts
from tradedesk.analytics.option_ideas import fee, trend
from tradedesk.analytics.options import black76_price

D0 = 1_700_006_400_000 - (1_700_006_400_000 % ts.DAY_MS)  # a day start
EXP = D0 + 14 * ts.DAY_MS + 8 * ts.HOUR_MS


def surf(day: int, spot: float = 100.0, iv: float = 0.5, expiry: int = EXP) -> dict:
    t = day + 9 * ts.HOUR_MS
    T = (expiry - t) / (365 * ts.DAY_MS)
    strikes = {float(k): {"mark_iv": iv, "sell_iv": iv - 0.02, "buy_iv": iv + 0.02, "n_put": 1} for k in range(40, 201)}
    return {"day": day, "t_ms": t, "spot": spot, "expiries": {expiry: {"F": spot, "T": T, "ratio": 1.0, "strikes": strikes}}}


def test_trend_at_matches_trend_and_ignores_later_bars():
    stamps = [D0 + i * ts.DAY_MS for i in range(260)]
    closes = [100.0 + i for i in range(260)]
    entry = stamps[230] + 9 * ts.HOUR_MS  # bars 0..229 are closed
    assert ts.trend_at(stamps, closes, entry) == trend(closes[:230]) == "bull"
    changed = closes[:230] + [1.0] * 30  # a crash after the entry must not change the label
    assert ts.trend_at(stamps, changed, entry) == "bull"
    assert ts.trend_at(stamps[:100], closes[:100], entry) == "neutral"  # not enough history


@pytest.mark.parametrize("bearish", [False, True])
def test_plan_spread_strikes_and_deltas(bearish):
    p, why = ts.plan_spread(surf(D0), EXP, bearish)
    assert why == "" and p is not None
    assert abs(abs(p["delta_long"]) - 0.40) < 0.02 and abs(abs(p["delta_short"]) - 0.15) < 0.02
    if bearish:
        assert p["K_short"] < p["K_long"] and p["delta_long"] < 0 and not p["is_call"]
    else:
        assert p["K_short"] > p["K_long"] and p["delta_long"] > 0 and p["is_call"]
    assert p["width"] == abs(p["K_long"] - p["K_short"])


def test_plan_spread_no_fresh_strike():
    s = surf(D0)
    s["expiries"][EXP]["strikes"] = {k: v for k, v in s["expiries"][EXP]["strikes"].items() if k <= 106}
    p, why = ts.plan_spread(s, EXP, bearish=False)
    assert p is None and why == "short leg: no fresh strike"
    assert ts.plan_spread(s, EXP + 1, bearish=False) == (None, "no expiry")


def test_entry_debit_analytic():
    p, _ = ts.plan_spread(surf(D0), EXP, bearish=False)
    iv_l, iv_s, fb = ts.exec_ivs(p, "mark", 0.015)
    assert (iv_l, iv_s, fb) == pytest.approx((0.515, 0.485, 0))
    pl = black76_price(100.0, p["K_long"], p["T"], 0.515, True)
    ps = black76_price(100.0, p["K_short"], p["T"], 0.485, True)
    assert ts.entry_debit(p, iv_l, iv_s) == pytest.approx(pl - ps + fee(100.0, pl) + fee(100.0, ps))
    assert ts.exec_ivs(p, "side")[:2] == pytest.approx((0.52, 0.48))
    q = dict(p, buy_long=None)
    assert ts.exec_ivs(q, "side", 0.015) == pytest.approx((0.515, 0.48, 1))
    assert ts.entry_debit(p, 0.005, 0.4) is None


def test_settle_bounds_and_fees():
    p = {"bearish": False, "K_long": 100.0, "K_short": 110.0, "width": 10.0, "spot": 100.0, "expiry": EXP}
    r = ts.settle(p, 4.0, 90.0)
    assert r["pnl"] == pytest.approx(-4.0) and r["r"] == pytest.approx(-1.0)
    r = ts.settle(p, 4.0, 200.0)  # both ITM: payoff = width, delivery fee 0.015 % of price on each leg
    assert r["pnl"] == pytest.approx(10.0 - 2 * 0.00015 * 200.0 - 4.0)
    assert ts.delivery_fees(p, 100.01) == pytest.approx(0.125 * 0.01)  # capped at 12.5 % of intrinsic
    b = {"bearish": True, "K_long": 100.0, "K_short": 90.0, "width": 10.0, "spot": 100.0, "expiry": EXP}
    assert ts.spread_payoff(b, 95.0) == pytest.approx(5.0) and ts.spread_payoff(b, 50.0) == pytest.approx(10.0)


def test_managed_exits_and_no_lookahead():
    p, _ = ts.plan_spread(surf(D0), EXP, bearish=False)
    debit = ts.entry_debit(p, *ts.exec_ivs(p)[:2])
    days = {D0: surf(D0)}
    assert ts.managed(days, p, debit, 120.0) == ts.settle(p, debit, 120.0)  # no later surfaces: to expiry
    d3 = D0 + 3 * ts.DAY_MS
    days[d3] = surf(d3, spot=p["K_short"] + 0.5)  # just past the sold strike, far from 70 % of max profit
    r = ts.managed(days, p, debit, 80.0)
    assert r["exit"] == "beyond_short" and r["exit_ms"] == d3 + 9 * ts.HOUR_MS
    v = ts.close_value(p, days[d3])
    assert r["pnl"] == pytest.approx(v - debit) and v - debit < 0.7 * (p["width"] - debit)
    assert ts.managed(days, p, debit, 80.0, beyond_short=False)["exit"] == "expiry"
    days[d3] = surf(d3, spot=150.0)
    assert ts.managed(days, p, debit, 80.0, beyond_short=False, tp=0.70)["exit"] == "tp"
    days[d3] = surf(d3, spot=p["K_short"] + 0.5)
    days[D0 + 5 * ts.DAY_MS] = surf(D0 + 5 * ts.DAY_MS, spot=10.0)  # after the exit: must not matter
    assert ts.managed(days, p, debit, 80.0) == r
    flat = {D0: surf(D0), D0 + 2 * ts.DAY_MS: surf(D0 + 2 * ts.DAY_MS, spot=100.0)}
    assert ts.managed(flat, p, debit, 100.0)["exit"] == "expiry"


def test_episodes():
    w = 7 * ts.DAY_MS
    labels = [(0, "bull"), (w, "bull"), (2 * w, "neutral"), (3 * w, "bull"), (4 * w, "bear"), (5 * w, "bear"), (7 * w, "bear")]
    assert ts.episodes(labels) == {0: 0, w: 0, 3 * w: 1, 4 * w: 2, 5 * w: 2, 7 * w: 3}


def test_holm_adjust():
    adj = ts.holm_adjust({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adj == pytest.approx({"a": 0.03, "c": 0.06, "b": 0.06})
    assert ts.holm_adjust({"a": 0.6, "b": 0.9}) == pytest.approx({"a": 1.0, "b": 1.0})


def test_cluster_bootstrap():
    b = ts.cluster_bootstrap([0.5] * 6, [1, 1, 2, 2, 3, 3], n_boot=500)
    assert b["mean"] == pytest.approx(0.5) and b["ci"] == pytest.approx((0.5, 0.5)) and b["p"] == 0.0
    assert b["n_clusters"] == 3 and b["se"] == pytest.approx(0.0)
    b = ts.cluster_bootstrap([1.0, -1.0, 1.0, -1.0], ["a", "a", "b", "b"], n_boot=500)
    assert b["ci"] == pytest.approx((0.0, 0.0))  # each cluster averages to zero
    assert math.isnan(ts.cluster_bootstrap([], [])["mean"])
