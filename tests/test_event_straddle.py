"""Event-day straddle analytics: entry quote without look-ahead, payoff, costs, controls, statistics."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics.event_straddle import (
    DAY_MS,
    HOUR_MS,
    blocked_days,
    cluster_bootstrap,
    cost_frac,
    delivery_fee,
    drop_top,
    entry_quote,
    expiry_code,
    group_events,
    iso_week,
    mae_short,
    pick_controls,
    power_gate,
    straddle_x,
    straddle_x_inverse,
)
from tradedesk.analytics.put_skew import inverse_price, year_frac

DAY = int(datetime(2024, 6, 12, tzinfo=UTC).timestamp() * 1000)
T0 = DAY + 9 * HOUR_MS
EXP = DAY + DAY_MS + 8 * HOUR_MS
S = 50_000.0


def pr(K: float, call: bool, ts: int, *, iv: float = 0.5, idx: float = S, exp: int = EXP, direction: str = "buy", edge: float = 0.0):
    """A print whose price and mark are Black-76 inverse prices at F = index (r = 1); ``edge`` = price - mark in BTC."""
    T = year_frac(exp, ts)
    mark = inverse_price(idx, K, T, iv, call)
    name = f"{expiry_code(exp)}{int(K)}-{'C' if call else 'P'}"
    return {
        "instrument_name": name,
        "timestamp": ts,
        "iv": iv * 100,
        "price": mark + edge,
        "mark_price": mark,
        "index_price": idx,
        "direction": direction,
        "amount": 1.0,
    }


def base_trades():
    tr = []
    for k, ts in enumerate(range(T0 - 90 * 60_000, T0 + 1, 10 * 60_000)):
        tr.append(pr(50_000, True, ts, direction="sell" if k % 2 else "buy"))
        tr.append(pr(50_000, False, ts + 1, direction="sell" if k % 2 else "buy"))
        tr.append(pr(51_000, True, ts + 2))
    return tr


def test_expiry_code():
    assert expiry_code(EXP) == "BTC-13JUN24-"


def test_entry_quote_matches_black76_atm_straddle():
    q = entry_quote(base_trades(), DAY)
    assert q is not None and q["covered"]
    assert q["K"] == 50_000 and q["spot"] == pytest.approx(S)
    assert q["ratio"] == pytest.approx(1.0, abs=1e-4)
    assert q["iv_call"] == pytest.approx(0.5, abs=1e-4) and q["iv_put"] == pytest.approx(0.5, abs=1e-4)
    T = (EXP - T0) / (365 * DAY_MS)
    # ATM Black-76 straddle on F = K: 2 F (2 N(sigma sqrt(T) / 2) - 1)
    n = 0.5 * (1 + math.erf(0.5 * math.sqrt(T) / 2 / math.sqrt(2)))
    assert q["premium"] == pytest.approx(2 * S * (2 * n - 1), rel=1e-4)
    assert q["T"] == pytest.approx(T)
    assert q["hs_sell"] == pytest.approx(0.0, abs=1e-6)


def test_entry_quote_ignores_prints_after_entry_time():
    """No look-ahead: prints after t0 (other IV, index, strikes) leave the quote unchanged."""
    tr = base_trades()
    q0 = entry_quote(tr, DAY)
    later = [pr(50_000, True, T0 + 60_000, iv=1.5, idx=60_000), pr(50_000, False, T0 + 1, iv=0.1, idx=40_000), pr(49_000, True, T0 + 5)]
    later += [pr(49_000, False, T0 + 6)]
    q1 = entry_quote(tr + later, DAY)
    assert q1 == q0


def test_entry_quote_ignores_prints_older_than_window():
    tr = base_trades()
    old = [pr(50_000, True, T0 - 3 * HOUR_MS, iv=1.5), pr(50_000, False, T0 - 3 * HOUR_MS, iv=1.5)]
    assert entry_quote(tr + old, DAY) == entry_quote(tr, DAY)


def test_entry_quote_needs_both_legs_and_near_strike():
    only_calls = [pr(50_000, True, T0 - 60_000)]
    q = entry_quote(only_calls, DAY)
    assert q is not None and not q["covered"] and q["reason"] == "no_strike_with_both_legs"
    far = [pr(52_000, True, T0 - 60_000), pr(52_000, False, T0 - 60_000)]
    q = entry_quote(far, DAY)
    assert not q["covered"] and q["reason"] == "strike_too_far"
    assert entry_quote([], DAY) is None


def test_entry_quote_empirical_half_spread():
    tr = [
        pr(50_000, True, T0 - 60_000, direction="sell", edge=-0.0002),
        pr(50_000, False, T0 - 50_000, direction="sell", edge=-0.0001),
        pr(50_000, True, T0 - 40_000, direction="buy", edge=0.0003),
        pr(50_000, False, T0 - 30_000, direction="buy", edge=0.0001),
    ]
    q = entry_quote(tr, DAY)
    assert q["hs_sell"] == pytest.approx(0.0003 * S)
    assert q["hs_buy"] == pytest.approx(0.0004 * S)


def test_straddle_payoffs_and_fees():
    q = {"K": 50_000.0, "spot": 50_000.0, "premium": 1_500.0, "premium_btc": 0.03, "fees": 30.0, "hs_sell": 20.0, "hs_buy": None}
    assert straddle_x(q, 51_000) == pytest.approx(0.01)
    assert straddle_x(q, 47_000) == pytest.approx(-0.03)
    assert straddle_x_inverse(q, 51_000) == pytest.approx(0.03 - 1_000 / 51_000)
    assert delivery_fee(50_000, 51_000) == pytest.approx(7.65)
    assert delivery_fee(50_000, 50_010) == pytest.approx(1.25)  # cap 12.5 % of intrinsic
    assert delivery_fee(50_000, 50_000) == 0.0
    assert cost_frac(q, 51_000, model="zero", direction=1) == 0.0
    assert cost_frac(q, 51_000, model="base", direction=1) == pytest.approx(0.0015 + 7.65 / 50_000)
    assert cost_frac(q, 51_000, model="stress", direction=1) == pytest.approx(0.0030 + 7.65 / 50_000)
    assert cost_frac(q, 51_000, model="empirical", direction=1) == pytest.approx((20 + 30 + 7.65) / 50_000)
    assert cost_frac(q, 51_000, model="empirical", direction=-1, fallback_hs=40.0) == pytest.approx((40 + 30 + 7.65) / 50_000)
    with pytest.raises(ValueError):
        cost_frac(q, 51_000, model="nope", direction=1)


def test_mae_short_uses_only_holding_window():
    q = {"K": 50_000.0, "spot": 50_000.0, "premium": 1_000.0, "t0": T0, "expiry": EXP}
    path = [(T0 - 1, 40_000.0), (T0 + HOUR_MS, 52_000.0), (T0 + 2 * HOUR_MS, 49_500.0), (EXP + 1, 70_000.0)]
    assert mae_short(path, q) == pytest.approx((2_000 - 1_000) / 50_000)
    assert mae_short([(T0 - 1, 1.0)], q) is None


def test_group_events_multi_and_blocked_days():
    g = group_events([(DAY + 12 * HOUR_MS, "cpi"), (DAY + 18 * HOUR_MS, "fomc"), (DAY + 2 * DAY_MS, "nfp")])
    assert g == {DAY: "multi", DAY + 2 * DAY_MS: "nfp"}
    b = blocked_days([DAY + 5 * HOUR_MS])
    assert b == {DAY - DAY_MS, DAY, DAY + DAY_MS}


def test_pick_controls_nearest_same_weekday_skipping_blocked_and_invalid():
    w = 7 * DAY_MS
    valid = {DAY + k * w for k in range(-8, 9)} - {DAY - w}
    blocked = {DAY + w}
    c = pick_controls(DAY, valid, blocked)
    assert c == [DAY - 2 * w, DAY + 2 * w, DAY - 3 * w, DAY + 3 * w]
    assert pick_controls(DAY, set(), set()) == []
    assert all((x - DAY) % w == 0 for x in pick_controls(DAY, valid, blocked, n=10))


def test_cluster_bootstrap_and_power_gate():
    rows = [(iso_week(DAY + i * DAY_MS), 0.01) for i in range(30)]
    r = cluster_bootstrap(rows, n_boot=500)
    assert r["mean"] == pytest.approx(0.01) and r["ci95"] == pytest.approx((0.01, 0.01))
    rows = [(f"w{i}", (-1) ** i * 0.01) for i in range(200)]
    r = cluster_bootstrap(rows, n_boot=2000)
    assert r["ci95"][0] < 0 < r["ci95"][1]
    assert r["se"] == pytest.approx(0.01 / math.sqrt(200), rel=0.15)
    g = power_gate([(-1) ** i * 0.01 for i in range(150)])
    assert g["n"] == 150 and g["sd"] == pytest.approx(0.01, rel=0.01)
    assert g["mde"] == pytest.approx(2.8 * g["sd"] / math.sqrt(150))
    assert g["passed"]
    assert not power_gate([(-1) ** i * 0.01 for i in range(100)])["passed"]  # n < 120
    assert not power_gate([(-1) ** i * 0.03 for i in range(150)])["passed"]  # MDE 0.69 %


def test_drop_top():
    rows = [("a", 0.05), ("b", -0.04), ("c", 0.01), ("d", 0.03), ("e", -0.001)]
    assert [r[0] for r in drop_top(rows, 2)] == ["c", "d", "e"]
    assert [r[0] for r in drop_top(rows, 2, direction=1)] == ["b", "c", "e"]
    assert [r[0] for r in drop_top(rows, 2, direction=-1)] == ["a", "c", "d"]
