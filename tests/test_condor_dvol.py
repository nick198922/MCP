"""Tests for analytics.condor_dvol (playbook rule P1' on history)."""

from __future__ import annotations

import pytest

from tradedesk.analytics import condor_dvol as cd
from tradedesk.analytics.option_condor import exec_legs
from tradedesk.analytics.option_ideas import fee
from tradedesk.analytics.options import black76_price

DAY = cd.DAY_MS
HOUR = 3_600_000
D0 = 1_700_006_400_000 - (1_700_006_400_000 % DAY)  # a day start
EXP = D0 + 14 * DAY + 8 * HOUR


def surf(day: int, spot: float = 100.0, iv: float = 0.5, expiry: int = EXP, lo: int = 40, hi: int = 200) -> dict:
    t = day + 9 * HOUR
    T = (expiry - t) / (365 * DAY)
    strikes = {float(k): {"mark_iv": iv, "sell_iv": iv - 0.02, "buy_iv": iv + 0.02} for k in range(lo, hi + 1)}
    return {"day": day, "t_ms": t, "spot": spot, "expiries": {expiry: {"F": spot, "T": T, "ratio": 1.0, "strikes": strikes}}}


def test_entry_percentiles_match_vrp_rank_and_ignore_later_candles():
    up = [(D0 + i * DAY, 0.40 + 0.001 * i) for i in range(400)]
    p = cd.entry_dvol_percentiles(up)
    assert min(p) == D0 + 366 * DAY  # first rankable candle (index 365) is known on the next day
    assert all(v == pytest.approx(1.0) for v in p.values())  # a new high above all 365 previous values
    down = [(t, 1.0 - v) for t, v in up]
    assert all(v == pytest.approx(0.0) for v in cd.entry_dvol_percentiles(down).values())
    # candle 380 = median of the previous 365 (values 15..379 -> rank 182 / 365)
    mid = [*up[:380], (up[380][0], up[197][1] - 1e-9), *up[381:]]
    assert cd.entry_dvol_percentiles(mid)[up[380][0] + DAY] == pytest.approx(182 / 365)
    # no look-ahead: changing candles after the one known at entry leaves that entry's percentile unchanged
    entry = up[370][0] + DAY
    later = [*up[:371], *((t, 5.0) for t, _ in up[371:])]
    assert cd.entry_dvol_percentiles(later)[entry] == p[entry]


def test_plan_p1_strikes_deltas_and_wings():
    p, why = cd.plan_p1(surf(D0), EXP)
    assert why == "" and p is not None
    K = p["K"]
    assert K["lp"] < K["sp"] < 100.0 < K["sc"] < K["lc"]
    assert abs(p["delta"]["sp"] + 0.15) < 0.02 and abs(p["delta"]["sc"] - 0.15) < 0.02
    assert K["sp"] - K["lp"] == 3.0 and K["lc"] - K["sc"] == 3.0  # +3 % of spot 100 on integer strikes
    assert p["width"] == 3.0 and p["target"]["lp"] == pytest.approx(K["sp"] - 3.0)
    wide = cd.plan_p1(surf(D0), EXP, wing=0.05)[0]
    assert wide is not None and wide["width"] == 5.0


def test_plan_p1_missing_legs():
    s = surf(D0)
    K_sp = cd.plan_p1(s, EXP)[0]["K"]["sp"]
    s["expiries"][EXP]["strikes"] = {k: v for k, v in s["expiries"][EXP]["strikes"].items() if k >= K_sp - 1}
    assert cd.plan_p1(s, EXP) == (None, "long put: no fresh strike")  # nearest wing 2 away from target > 1.5 band
    s = surf(D0)
    s["expiries"][EXP]["strikes"] = {k: v for k, v in s["expiries"][EXP]["strikes"].items() if k <= 106}
    assert cd.plan_p1(s, EXP) == (None, "short call: no fresh strike")
    assert cd.plan_p1(surf(D0), EXP + 1) == (None, "no expiry")
    assert cd.wing_strike({"strikes": {90.0: {}, 91.0: {}}}, 90.0, 100.0, 0.03, 0.015, False) is None  # nothing below


def test_entry_credit_analytic():
    p, _ = cd.plan_p1(surf(D0), EXP)
    legs, fb = exec_legs(p, "mark", 0.015)
    assert fb == 0
    want = 0.0
    for K, is_call, q, iv in legs:
        assert iv == pytest.approx(0.485 if q < 0 else 0.515)
        px = black76_price(100.0, K, p["T"], iv, is_call)
        want += -q * px - fee(100.0, px)
    assert cd.entry_credit(p, legs) == pytest.approx(want)
    assert 0 < want < p["width"]
    assert cd.entry_credit(p, [(*leg[:3], 0.005) for leg in legs]) is None
    assert cd.entry_credit(dict(p, width=1e-6), legs) is None  # credit must stay below the width


def test_settle_bounds_and_delivery_fees():
    p = {"K": {"lp": 85.0, "sp": 90.0, "sc": 110.0, "lc": 115.0}, "width": 5.0, "spot": 100.0, "expiry": EXP}
    r = cd.settle(p, 1.5, 100.0)
    assert r["pnl"] == pytest.approx(1.5) and r["r"] == pytest.approx(1.5 / 3.5) and r["max_loss"] == pytest.approx(3.5)
    r = cd.settle(p, 1.5, 80.0)  # both puts ITM: payoff -width, fee 0.015 % of 80 on each
    assert r["pnl"] == pytest.approx(1.5 - 5.0 - 2 * 0.00015 * 80.0)
    assert r["r"] < -1.0 and r["pnl_spot"] == pytest.approx(r["pnl"] / 100.0)
    assert cd.settle(p, 1.5, 112.0)["pnl"] == pytest.approx(1.5 - 2.0 - 0.00015 * 112.0)
    assert cd.delivery_fees(p, 110.01) == pytest.approx(0.125 * 0.01)  # capped at 12.5 % of intrinsic
    assert cd.condor_payoff(p, 200.0) == pytest.approx(-5.0) and cd.condor_payoff(p, 87.0) == pytest.approx(-3.0)


def test_managed_exits_and_no_lookahead():
    p, _ = cd.plan_p1(surf(D0), EXP)
    credit = cd.entry_credit(p, exec_legs(p, "mark")[0])
    days = {D0: surf(D0)}
    assert cd.managed(days, p, credit, 100.0) == cd.settle(p, credit, 100.0)  # no later surfaces: to expiry
    d3 = D0 + 3 * DAY
    days[d3] = surf(d3, spot=p["K"]["sp"] - 0.5)  # spot reached the sold put
    r = cd.managed(days, p, credit, 80.0)
    assert r["exit"] == "breach" and r["exit_ms"] == d3 + 9 * HOUR
    assert r["pnl"] == pytest.approx(credit - cd.close_cost(p, days[d3]))
    assert cd.managed(days, p, credit, 100.0, breach=False)["exit"] == "expiry"
    days[D0 + 5 * DAY] = surf(D0 + 5 * DAY, spot=10.0)  # after the exit: must not matter
    assert cd.managed(days, p, credit, 80.0) == r
    calm = {D0: surf(D0), d3: surf(d3, iv=0.05)}  # vol collapsed: almost all of the credit is kept
    t = cd.managed(calm, p, credit, 100.0)
    assert t["exit"] == "tp" and t["pnl"] >= 0.8 * credit


def test_cluster_bootstrap_diff():
    vals = [1.0, 1.0, 1.0, 0.0, 0.0, 0.0]
    cl = [0, 0, 1, 2, 3, 3]
    a = [True, True, True, False, False, False]
    b = cd.cluster_bootstrap_diff(vals, cl, a, n_boot=500)
    assert b["diff"] == pytest.approx(1.0) and b["ci"] == pytest.approx((1.0, 1.0)) and b["p"] == 0.0
    assert (b["k_a"], b["k_b"]) == (2, 2)
    with pytest.raises(ValueError):
        cd.cluster_bootstrap_diff(vals, [0, 0, 1, 1, 3, 3], a, n_boot=10)
    assert cd.cluster_bootstrap_diff(vals, cl, [True] * 6, n_boot=10)["ci"] is None
    noisy = cd.cluster_bootstrap_diff([0.5, -0.5, 0.4, -0.4], [0, 1, 2, 3], [True, True, False, False], n_boot=2000)
    assert noisy["ci"][0] < 0 < noisy["ci"][1] and noisy["p"] > 0.5
