"""Tests of the carry cost calibration (analytic expectations, no network)."""

from __future__ import annotations

import pytest

from tradedesk.analytics.carry_costs import (
    OLD_MODEL,
    WEEK_MS,
    CostModel,
    block_bootstrap_apr,
    calibrate,
    cluster_bootstrap_mean,
    cochran_q,
    decompose_side,
    leg_type,
    max_drawdown,
    risk_summary,
    week_start,
    weekly_changes,
)
from tradedesk.analytics.carry_rules import leg_execution

MON = 1_792_368_000_000  # 2026-10-19 00:00 UTC, a Monday
DAY = 86_400_000


def _side(side: str = "buy", **kw):
    book = {"bids": [[99.0, 10.0], [98.0, 100.0]], "asks": [[101.0, 10.0], [102.0, 100.0]]}
    base = {"side": side, "fee": 0.001, "model_cost": 0.0015, "model_price": 100.0, "notional_usd": 2020.0, "book": book}
    base.update(kw)
    base.setdefault("venue", "binance")
    base.setdefault("leg", "spot")
    return base


def test_week_start_is_monday():
    assert week_start(MON) == MON
    assert week_start(MON + 6 * DAY + 1) == MON
    assert week_start(MON - 1) == MON - WEEK_MS


def test_leg_type():
    assert leg_type("binance", "spot") == "spot_binance"
    assert leg_type("binanceusdm", "perp") == "perp_binance"
    assert leg_type("binanceusdm", "short") == "perp_binance"
    assert leg_type("bybit", "long") == "perp_bybit"


def test_decompose_buy_two_levels():
    # $1010 at 101 (10 units) + $1010 at 102 (9.90196 units): vwap = 2020 / 19.90196
    d = decompose_side(_side("buy"))
    vwap = 2020 / (10 + 1010 / 102)
    assert d["half_spread"] == pytest.approx(0.01)
    assert d["slippage"] == pytest.approx((vwap - 100) / 100)
    assert d["impact"] == pytest.approx((vwap - 100) / 100 - 0.01)
    assert d["actual_cost"] == pytest.approx(0.001 + (vwap - 100) / 100)
    assert d["timing"] == pytest.approx(0.0)  # model price = mid
    assert not d["depth_short"]


def test_decompose_matches_registered_actual_cost():
    s = _side("sell", model_price=101.0)
    rec = leg_execution(s["book"], "sell", 2020.0, fee=0.001, model_slippage=0.0005, cycle_ts=0)
    d = decompose_side(s)
    assert d["actual_cost"] == pytest.approx(rec["actual_cost"])
    assert d["fee"] + d["half_spread"] + d["impact"] == pytest.approx(rec["actual_cost"])
    # selling at mid 100 against a model price of 101 loses 1/101
    assert d["timing"] == pytest.approx(1 / 101)


def test_decompose_timing_sanity_and_missing_book():
    assert decompose_side(_side(model_price=50.0))["timing"] is None  # 100 % off: unit mismatch
    assert decompose_side(_side(book={"bids": [], "asks": []})) is None


def test_decompose_capacity_rewalk():
    d = decompose_side(_side("buy"), notional_usd=100_000.0)
    assert d["depth_short"] and d["unfilled_usd"] == pytest.approx(100_000 - 1010 - 10200)


def test_cluster_bootstrap_constant_and_weights():
    r = cluster_bootstrap_mean([0.002] * 6, ["a", "a", "b", "b", "c", "c"], n_boot=200)
    assert r["mean"] == pytest.approx(0.002) and r["lo"] == pytest.approx(0.002) and r["hi"] == pytest.approx(0.002)
    r = cluster_bootstrap_mean([1.0, 1.0, 1.0, 4.0], ["a", "a", "a", "b"], n_boot=500)
    assert r["mean"] == pytest.approx(7 / 4) and r["clusters"] == 2
    assert r["lo"] == pytest.approx(1.0) and r["hi"] == pytest.approx(4.0)
    assert cluster_bootstrap_mean([], [])["mean"] is None


def test_cochran_q_identical_and_spread():
    r = cochran_q([0.1, 0.1, 0.1], [0.01, 0.01, 0.01])
    assert r["q"] == pytest.approx(0.0) and r["i2"] == 0.0
    r = cochran_q([0.0, 1.0], [0.1, 0.1])  # pooled 0.5, Q = 2 x 0.25 / 0.01 = 50
    assert r["q"] == pytest.approx(50.0) and r["i2"] == pytest.approx(49 / 50)


def test_cost_model_side_cost_and_sim_slippage():
    assert OLD_MODEL.side_cost(["spot_binance", "perp_binance"]) == pytest.approx(0.0025)
    m = CostModel(fees=dict(OLD_MODEL.fees), slippage={"spot_binance": 0.002, "perp_binance": 0.0006, "perp_bybit": 0.001}, timing=0.0004)
    s = m.sim_slippage(["spot_binance", "perp_binance"])
    # the simulators' side cost spot_fee + perp_fee + 2 x slippage equals the model's
    assert 0.001 + 0.0005 + 2 * s == pytest.approx(m.side_cost(["spot_binance", "perp_binance"]))


def test_calibrate_minimums_and_as_of():
    sides = [
        {"cycle_ts": i, "symbol": f"S{i % 10}", "trade_id": str(i), "leg_type": "perp_bybit", "slippage": 0.001, "timing": 0.0}
        for i in range(25)
    ]
    sides += [{"cycle_ts": 1, "symbol": "X", "trade_id": "x", "leg_type": "spot_binance", "slippage": 0.003, "timing": 0.0}]
    c = calibrate(sides, as_of_ms=100, n_boot=100)
    assert c["perp_bybit"]["enough"] and c["perp_bybit"]["main"] == pytest.approx(0.001)
    assert not c["spot_binance"]["enough"] and c["spot_binance"]["stress"] == pytest.approx(0.003)
    # sides after as_of are ignored
    assert calibrate(sides, as_of_ms=9, n_boot=100)["perp_bybit"]["n"] == 10


def test_calibrate_no_lookahead():
    base = [
        {"cycle_ts": i * DAY, "symbol": f"S{i}", "trade_id": str(i), "leg_type": "perp_binance", "slippage": 0.001 * i, "timing": 0.0}
        for i in range(12)
    ]
    later = [dict(s, slippage=s["slippage"] + (0.05 if s["cycle_ts"] > 6 * DAY else 0.0)) for s in base]
    assert calibrate(base, as_of_ms=6 * DAY, n_boot=200) == calibrate(later, as_of_ms=6 * DAY, n_boot=200)


def test_weekly_changes_and_drawdown():
    eq = [(MON, 0.01), (MON + DAY, 0.03), (MON + WEEK_MS, 0.0), (MON + WEEK_MS + DAY, -0.01), (MON + 2 * WEEK_MS, 0.02)]
    wk = weekly_changes(eq)
    assert [w for w, _ in wk] == [MON, MON + WEEK_MS, MON + 2 * WEEK_MS]
    assert [v for _, v in wk] == pytest.approx([0.03, -0.04, 0.03])
    assert max_drawdown(eq) == pytest.approx(0.04)


def test_weekly_changes_no_lookahead():
    eq = [(MON + i * DAY, 0.001 * i) for i in range(30)]
    changed = eq[:15] + [(t, v - 1.0) for t, v in eq[15:]]
    cut = week_start(MON + 15 * DAY)
    assert [x for x in weekly_changes(eq) if x[0] < cut] == [x for x in weekly_changes(changed) if x[0] < cut]


def test_block_bootstrap_constant():
    r = block_bootstrap_apr([0.001] * 20, n_boot=200)
    assert r["apr"] == pytest.approx(0.001 * 52.1775)
    assert r["lo"] == pytest.approx(r["apr"]) and r["hi"] == pytest.approx(r["apr"])


def test_risk_summary_on_capital():
    eq = [(MON + i * WEEK_MS, 0.01 * (i + 1)) for i in range(52)]
    r = risk_summary(eq, years=1.0, capital_factor=1.5, n_boot=200)
    assert r["apr_on_notional"] == pytest.approx(0.52)
    assert r["apr_on_capital"] == pytest.approx(0.52 / 1.5)
    assert r["max_dd_on_capital"] == pytest.approx(0.0)
    assert r["worst_week_on_capital"] == pytest.approx(0.01 / 1.5)
