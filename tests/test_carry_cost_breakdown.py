"""Tests of analytics.carry_cost_breakdown: analytic expectations on synthetic books and cycles, and causality."""

from __future__ import annotations

import pytest

from tradedesk.analytics.carry_cost_breakdown import (
    by_group,
    coin_permutation,
    concentration,
    concentration_reading,
    cost_block,
    payment_published,
    reliability,
    side_records,
    size_curve,
    time_split,
)
from tradedesk.analytics.carry_report import cost_realism
from tradedesk.analytics.carry_rules import leg_execution

H8 = 8 * 3_600_000
T0 = 1_762_000_000_000 // H8 * H8


def book(half_spread: float, mid: float = 100.0, qty: float = 1e6) -> dict:
    """A one-level-per-side book: the walk of any notional within ``qty`` fills at the best price."""
    return {"bids": [[mid * (1 - half_spread), qty]], "asks": [[mid * (1 + half_spread), qty]]}


def side_row(coin: str, leg: str, event: str, half_spread: float, *, k: int = 0, fee: float = 0.001, notional: float = 1000.0, bk=None):
    side = ("buy" if event == "open" else "sell") if leg == "spot" else ("sell" if event == "open" else "buy")
    ex = leg_execution(bk if bk is not None else book(half_spread), side, notional, fee=fee, model_slippage=0.0005, cycle_ts=T0 + k * H8)
    payload = {
        "trade_id": f"A:{coin}/USDT:USDT:{k}",
        "event": event,
        "leg": leg,
        "venue": "binance" if leg == "spot" else "binanceusdm",
        "symbol": f"{coin}/USDT" if leg == "spot" else f"{coin}/USDT:USDT",
        "perp_symbol": f"{coin}/USDT:USDT",
        "model_price": 100.0,
        **ex,
    }
    return {"cycle_ts": T0 + k * H8, "trade_id": payload["trade_id"], "payload": payload}


def test_cost_block_ratio_and_components():
    rows = [side_row("X", "spot", "open", 0.001), side_row("X", "perp", "open", 0.003)]
    recs = side_records(rows, as_of_ms=T0)
    b = cost_block(recs)
    # actual = fee 0.001 + half spread: 0.002 and 0.004; model 0.0015 each -> ratio 0.003 / 0.0015 = 2
    assert b["n"] == 2 and b["ratio"] == pytest.approx(2.0)
    assert b["mean_half_spread"] == pytest.approx(0.002) and b["mean_impact"] == pytest.approx(0.0, abs=1e-12)
    assert b["excess_sum"] == pytest.approx(0.003)
    assert b["ratio"] == pytest.approx(cost_realism([r["payload"] for r in rows])["ratio"])


def test_no_book_and_depth_short_excluded_from_ratio():
    thin = book(0.001, qty=1.0)  # $100 of depth per side, notional $1000
    rows = [side_row("X", "spot", "open", 0.001), side_row("Y", "spot", "open", 0.0, bk={}), side_row("Z", "perp", "open", 0.0, bk=thin)]
    b = cost_block(side_records(rows, as_of_ms=T0))
    assert (b["sides"], b["with_book"], b["n"], b["depth_short"]) == (3, 2, 1, 1)
    assert b["ratio"] == pytest.approx(0.002 / 0.0015)


def test_by_group_leg_event():
    rows = [side_row("X", "spot", "open", 0.001), side_row("X", "spot", "close", 0.002), side_row("X", "perp", "open", 0.0005)]
    g = by_group(side_records(rows, as_of_ms=T0), lambda x: (x["leg"], x["event"]))
    assert g[("spot", "close")]["ratio"] == pytest.approx(0.003 / 0.0015)
    assert g[("perp", "open")]["ratio"] == pytest.approx(1.0)


def test_concentration_few_coins_and_broad():
    rows = [side_row("BAD", leg, "open", 0.02) for leg in ("spot", "perp")]
    rows += [side_row(c, leg, "open", 0.0005) for c in ("A1", "A2", "A3") for leg in ("spot", "perp")]
    conc = concentration(side_records(rows, as_of_ms=T0))
    assert conc["ranked"][0] == "BAD"
    assert conc["without_top"][1] == pytest.approx(1.0)
    assert conc["leave_one_out"]["BAD"] == pytest.approx(1.0)
    assert concentration_reading(conc) == "few_coins"
    wide = [side_row(c, leg, "open", 0.003) for c in ("A1", "A2", "A3", "A4") for leg in ("spot", "perp")]
    cw = concentration(side_records(wide, as_of_ms=T0))
    assert cw["above_threshold"] == 4 and concentration_reading(cw) == "broad"


def test_size_curve_top_of_book_and_impact():
    # asks: $500 at 101, then deep at 103 (mid 100): $1000 fills half at 101 and half at 103
    bk = {"bids": [[99.0, 1e6]], "asks": [[101.0, 500.0 / 101.0], [103.0, 1e6]]}
    recs = side_records([side_row("X", "spot", "open", 0.0, bk=bk)], as_of_ms=T0)
    curve = {c["multiplier"]: c for c in size_curve(recs)}
    assert curve[0.0]["mean_actual"] == pytest.approx(0.001 + 0.01)
    assert curve[0.5]["mean_actual"] == pytest.approx(0.001 + 0.01)
    assert curve[1.0]["mean_actual"] == pytest.approx(recs[0]["actual_cost"])
    assert curve[2.0]["mean_actual"] > curve[1.0]["mean_actual"] > curve[0.5]["mean_actual"]


def test_coin_permutation_separates_and_pools():
    rows = [side_row("LO", "spot", "open", 0.0005, k=i) for i in range(6)] + [side_row("HI", "spot", "open", 0.005, k=i) for i in range(6)]
    res = coin_permutation(side_records(rows, as_of_ms=T0 + 10 * H8), n_perm=2000, seed=1)
    assert res["p"] < 0.05
    same = [side_row(c, "spot", "open", 0.001, k=i) for c in ("A", "B") for i in range(6)]
    assert coin_permutation(side_records(same, as_of_ms=T0 + 10 * H8), n_perm=200, seed=1)["p"] == pytest.approx(1.0)


def test_time_split_halves():
    rows = [side_row("X", "spot", "open", 0.001, k=i) for i in range(4)]
    ts = time_split(side_records(rows, as_of_ms=T0 + 10 * H8))
    assert ts["cut_ts"] == T0 + 2 * H8 and ts["first"]["n"] == 2 and ts["second"]["n"] == 2
    assert ts["persistence"] is None


def cycle(k: int, *, on_time=True, flags=()):
    return {"cycle_ts": T0 + k * H8, "status": "done", "payload": {"on_time": on_time, "complete": not flags, "flags": list(flags)}}


def test_reliability_split_and_without():
    fa = {"symbol": "WIF/USDT:USDT", "issue": "funding_payment_missing", "detail": "scheduled_payment_missing"}
    fb = {"symbol": "MITO/USDT:USDT", "issue": "funding_gap_confirmed", "detail": "scheduled_payment_missing"}
    cyc = [cycle(0), cycle(1), cycle(2, flags=[fa]), cycle(3, flags=[fa, fb])]
    r = reliability(cyc, as_of_ms=T0 + 3 * H8)
    ka, kb = "funding_payment_missing / scheduled_payment_missing", "funding_gap_confirmed / scheduled_payment_missing"
    assert (r["cycles"], r["good"]) == (4, 2) and r["share_good"] == pytest.approx(0.5)
    assert r["incomplete_by_issue"] == {kb: 1, ka: 2}
    assert r["incomplete_by_coin"] == {"MITO": {kb: 1}, "WIF": {ka: 2}}
    assert r["share_good_without"][ka] == pytest.approx(0.75) and r["share_good_without"][kb] == pytest.approx(0.5)


def test_payment_published_later_or_never():
    flags = [
        {"cycle_ts": T0, "symbol": "WIF/USDT:USDT", "key": "funding_payment_missing / scheduled_payment_missing"},
        {"cycle_ts": T0, "symbol": "MITO/USDT:USDT", "key": "funding_payment_missing / scheduled_payment_missing"},
        {"cycle_ts": T0, "symbol": "X/USDT:USDT", "key": "funding_payment_missing / payment_gap"},
    ]
    h4 = 4 * 3_600_000
    funding = {"WIF/USDT:USDT": [T0 - 2 * h4, T0 - h4, T0 + 5], "MITO/USDT:USDT": [T0 - 2 * H8, T0 - H8]}
    out = payment_published(flags, funding)
    assert [x["status"] for x in out] == ["published_later", "never_stored"]
    assert out[0]["interval_h_before"] == pytest.approx(4.0) and out[1]["interval_h_before"] == pytest.approx(8.0)


def test_results_before_t_do_not_change_with_later_data():
    rows = [side_row("X", "spot", "open", 0.001, k=0), side_row("Y", "perp", "open", 0.002, k=1)]
    later = rows + [side_row("Z", "spot", "close", 0.05, k=5)]
    t = T0 + 2 * H8
    assert cost_block(side_records(rows, as_of_ms=t)) == cost_block(side_records(later, as_of_ms=t))
    cyc = [cycle(0), cycle(1, flags=[{"symbol": "X/USDT:USDT", "issue": "i"}])]
    assert reliability(cyc, as_of_ms=t) == reliability(cyc + [cycle(5, flags=[{"symbol": "Z/USDT:USDT", "issue": "j"}])], as_of_ms=t)
