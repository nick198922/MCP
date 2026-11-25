"""Registered 30-day analysis of the Deribit <-> Bybit crossing (analytics/option_xvenue_verdict.py): analytic cases."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.option_xvenue_verdict import (
    build_episodes,
    decision_cutoff,
    decision_rows,
    frequency,
    km_median,
    leg_gap_bounds,
    paper_trade,
    persistence,
    row_measure,
    snapshot_leg_gaps,
    snapshot_outcome,
    snapshot_outcomes,
    sync_class,
    verdict_30d,
    wilson,
)


def _row(**kw):
    base = {
        "ts": 1_000,
        "expiry_ts": 50_000,
        "strike": 100_000.0,
        "opt": "C",
        "tau_hours": 10.0,
        "d_index": 100_000.0,
        "b_index": 100_000.0,  # g = 0: no shift whatever the delta
        "d_underlying": 100_000.0,
        "b_underlying": 100_000.0,
        "d_mark_iv": 0.4,
        "d_bid": 0.002,
        "d_ask": 0.003,  # $300
        "d_bid_size": 1.0,
        "d_ask_size": 1.0,
        "b_bid": 500.0,
        "b_ask": 600.0,
        "b_bid_size": 1.0,
        "b_ask_size": 1.0,
        "d_ts_ms": 1_000_000,
        "b_ts_ms": 1_002_000,
        "d_recv_ms": 1_003_000,
        "b_recv_ms": 1_003_500,
        "reg_atm": 1,
        "mode": "hourly",
        "currency": "BTC",
        "d_instrument": "BTC-X-100000-C",
        "b_symbol": "BTC-X-100000-C-USDT",
    }
    return base | kw


def test_cutoff_is_thirty_days_after_first_row():
    assert decision_cutoff(1_792_758_032) == 1_792_758_032 + 30 * 86_400


def test_sync_classes():
    assert sync_class(_row()) == "A"
    assert sync_class(_row(b_ts_ms=1_010_000)) == "B"
    assert sync_class(_row(b_ts_ms=1_061_000)) == "C"
    assert sync_class(_row(b_ts_ms=None)) == "C"
    assert sync_class(_row(), "recv") == "A"
    assert sync_class(_row(b_recv_ms=None), "recv") is None  # cached / no leg: out of A_r, not substituted


def test_row_measure_threshold_and_fees():
    # gross 500 - 300 = 200, fees 0.03 % + 0.03 % of 100k = 60 -> net 140 = 0.14 % < 0.15 %
    m = row_measure(_row())
    assert m["sides"]["b_over_d"]["net_frac"] == pytest.approx(0.0014)
    assert not m["hit"]
    m = row_measure(_row(b_bid=520.0))  # net 160 -> 0.16 %
    assert m["hit"] and m["sides"]["b_over_d"]["hit"]
    assert not row_measure(_row(b_bid=520.0), fee_bybit=0.0006)["hit"]  # net 130
    assert not row_measure(_row(b_bid=520.0, b_bid_size=0.05))["hit"]  # depth 0.05 < 0.1
    assert row_measure(_row(b_bid=520.0, b_bid_size=0.05), min_depth=0.0)["hit"]


def test_row_measure_bybit_cap():
    # cheap option: d_ask $10, b_bid 210: gross 200; primary fees 60 -> 0.14 %; cap: Bybit fee min(30, 0.07 x 210 = 14.7)
    r = _row(d_ask=0.0001, b_bid=210.0)
    assert not row_measure(r)["hit"]
    m = row_measure(r, bybit_cap=0.07)
    assert m["sides"]["b_over_d"]["net_frac"] == pytest.approx((200 - 30 - 14.7) / 100_000)
    assert m["hit"]


def test_row_measure_index_anomaly_and_unmeasurable():
    assert row_measure(_row(b_index=102_000.0)) is None  # |g| = 2 % > 1 %
    assert row_measure(_row(d_index=None)) is None
    assert row_measure(_row(d_mark_iv=None, b_mark_iv=None)) is None


def test_row_measure_shift_removes_index_basis():
    # Bybit index +0.15 %: a call quoted higher by delta x g x F is not a crossing after the shift
    raw = _row(b_index=100_150.0, b_bid=520.0)
    assert row_measure(raw, gap_source="none")["hit"]
    m = row_measure(raw)
    assert m["shift_usd"] < 0 and not m["hit"]


def test_snapshot_outcome_only_eligible_rows():
    rows = [_row(b_bid=520.0, b_ts_ms=1_010_000), _row(strike=101_000.0)]  # the hit is class B
    a = lambda r: sync_class(r) == "A"  # noqa: E731
    assert snapshot_outcome(rows, a, row_measure) == (True, False)
    assert snapshot_outcome(rows, lambda r: sync_class(r) == "B", row_measure) == (True, True)
    assert snapshot_outcome([_row(d_index=None)], a, row_measure) == (False, False)


def test_frequency_and_wilson():
    out = [(d, True, i == 0) for d in range(30) for i in range(18)]  # one hit per day: p = 1/18
    f = frequency(out, reps=500)
    assert (f["n"], f["k"], f["days"]) == (540, 30, 30)
    assert f["p"] == pytest.approx(1 / 18)
    assert f["boot"] == pytest.approx((1 / 18, 1 / 18))  # identical days: no bootstrap spread
    zero = frequency([(d, True, False) for d in range(30)], reps=200)
    assert zero["p"] == 0 and zero["boot"] == (0.0, 0.0)
    z2 = 1.959964**2
    assert zero["wilson_days"][1] == pytest.approx(z2 / (30 + z2))
    assert wilson(0.5, 0) is None
    assert frequency([(0, False, False)])["p"] is None


def test_cutoff_is_enforced_by_the_outcome_pipeline():
    # the same function the script uses (snapshot_outcomes -> decision_rows) must drop every row at/after the cut-off:
    # crossings placed there must not reach p, and moving the cut-off past them must (so the test is not circular)
    cutoff = decision_cutoff(0, 1)
    a = lambda r: sync_class(r) == "A"  # noqa: E731
    before = [_row(ts=t, expiry_ts=86_400) for t in range(0, cutoff, 3_600)]
    minute_hit = [_row(ts=1_800, expiry_ts=86_400, mode="minute", b_bid=520.0)]  # minute rows never count for p
    after = [_row(ts=t, expiry_ts=2 * 86_400, b_bid=520.0) for t in range(cutoff, cutoff + 6 * 3_600, 3_600)]
    rows = before + minute_hit + after
    out = snapshot_outcomes(rows, cutoff, a, row_measure)
    assert len(out) == 24 and all(t < cutoff for t, *_ in out)
    f = frequency([(day, d, h) for _, day, d, h in out], reps=200)
    assert (f["n"], f["k"]) == (24, 0)
    later = snapshot_outcomes(rows, cutoff + 6 * 3_600, a, row_measure)
    assert sum(h for *_, h in later) == 6  # the post-cut-off crossings are real and would be counted
    changed = before + minute_hit + [r | {"b_bid": 900.0, "d_bid": 0.0001} for r in after]
    assert snapshot_outcomes(changed, cutoff, a, row_measure) == out
    assert decision_rows(rows, cutoff) == before + minute_hit
    with pytest.raises(ValueError):
        snapshot_outcomes(rows, cutoff, a, row_measure, strikes="atm")


def test_snapshot_outcomes_strike_sets():
    # five strikes, Deribit forward 100k: registered ATM +- 1 = 99k..101k; a crossing on 102k counts only in "all"
    snap = [_row(strike=k, b_bid=520.0 if k == 102_000.0 else 500.0) for k in (98_000.0, 99_000.0, 100_000.0, 101_000.0, 102_000.0)]
    a = lambda r: sync_class(r) == "A"  # noqa: E731
    assert snapshot_outcomes(snap, 10**9, a, row_measure) == [(1_000, 50_000, True, False)]
    assert snapshot_outcomes(snap, 10**9, a, row_measure, strikes="all") == [(1_000, 50_000, True, True)]


def test_snapshot_leg_gaps_counts_legs_not_rows():
    ks = (98_000.0, 99_000.0, 100_000.0, 101_000.0, 102_000.0)
    snap = [_row(strike=k) for k in ks]
    assert snapshot_leg_gaps(snap) == {"rows": 5, "d_missing": 0, "b_missing": 0, "d_missing_atm": 0}
    # Bybit forward 100.2k -> ATM +- 1 = 99k..101k; Deribit legs missing on 100k (inside) and 102k (outside)
    snap = [
        _row(
            strike=k,
            b_underlying=100_200.0,
            d_instrument=None if k in (100_000.0, 102_000.0) else "BTC-X",
            b_symbol=None if k == 98_000.0 else "B",
        )
        for k in ks
    ]
    assert snapshot_leg_gaps(snap) == {"rows": 5, "d_missing": 2, "b_missing": 1, "d_missing_atm": 1}
    # Bybit forward 100.6k shifts the window to 100k..102k: both missing legs inside
    snap = [r | {"b_underlying": 100_600.0} for r in snap]
    assert snapshot_leg_gaps(snap)["d_missing_atm"] == 2
    # no Bybit leg at all: fall back to the Deribit forward (100k)
    snap = [_row(strike=k, b_symbol=None, d_instrument=None if k == 99_000.0 else "D") for k in ks]
    assert snapshot_leg_gaps(snap) == {"rows": 5, "d_missing": 1, "b_missing": 5, "d_missing_atm": 1}
    assert snapshot_leg_gaps([])["d_missing_atm"] == 0


def test_leg_gap_bounds_without_and_worst_case():
    outc = [(1, "d1", True, False), (2, "d1", True, False), (3, "d2", False, False), (4, "d2", True, True), (5, "d2", True, False)]
    lb = leg_gap_bounds(outc, {2, 3}, reps=200)
    assert (lb["without"]["n"], lb["without"]["k"]) == (3, 1)  # snapshots 1, 4, 5
    assert lb["worst"] == {"n": 4, "k": 2, "p": 0.5}  # denominator unchanged; 2 becomes a crossing, 3 stays out
    assert lb["affected_in_denom"] == 1
    zero = leg_gap_bounds([(t, 0, True, False) for t in range(10)], set(), reps=50)
    assert zero["worst"]["k"] == 0 and zero["without"]["n"] == 10


def _f(p, n=540):
    return {"n": n, "k": None, "p": p}


def test_verdict_30d_branches():
    lo = _f(0.0)
    assert verdict_30d(_f(0.0, 299), lo, lo, lo, lo)["branch"] == "collect_to_90d"
    v = verdict_30d(lo, lo, lo, _f(0.005), _f(0.0))
    assert v["status"] == "rejected"
    v = verdict_30d(lo, lo, lo, _f(0.02), _f(0.0))  # A_r reaches 1 %
    assert v["status"] == "inconclusive" and "A_r" in v["reason"]
    v = verdict_30d(lo, lo, lo, _f(0.5, 100), _f(0.0))  # A_r too small: does not block
    assert v["status"] == "rejected" and "мало даних" in v["blockers"][0]["note"]
    v = verdict_30d(lo, lo, lo, _f(0.0), _f(0.01))  # the 7 % cap reaches 1 %
    assert v["status"] == "inconclusive" and "cap7" in v["reason"]
    hi = _f(0.06)
    assert verdict_30d(hi, hi, hi, lo, lo)["branch"] == "minute_mode"
    assert verdict_30d(hi, _f(0.04), hi, lo, lo)["branch"] == "collect_to_90d"
    assert verdict_30d(_f(0.03), lo, lo, lo, lo)["status"] == "forward"


def test_persistence():
    seq = [(1, 10, {"a", "b"}), (2, 10, {"a"}), (3, 11, {"a"}), (4, 11, set())]
    assert persistence(seq) == (3, 1)  # t1: a again, b not; t2: next is another expiry; t3: a not


def test_episodes_event_censoring_and_kind():
    obs = [(0, False, True), (60, True, True), (120, True, False), (180, False, True)]
    (e,) = build_episodes(obs)
    assert e == {"start": 60, "kind": "M", "event": True, "d_right": 120, "d_left": 60}
    # detected by an hourly snapshot, next observation an hour later: end unknown -> right-censored at the last crossing
    (e,) = build_episodes([(0, True, True), (3600, False, True)])
    assert (e["kind"], e["event"], e["d_right"]) == ("H", False, 0)
    # a crossing on a non-registered strike does not open an episode; data run out -> censored
    assert build_episodes([(0, True, False), (60, False, True)]) == []
    (e,) = build_episodes([(0, True, True), (60, True, True)])
    assert (e["event"], e["d_left"]) == (False, 60)


def test_km_median():
    assert km_median([1, 2, 3], [True, True, True]) == 2
    assert km_median([1, 2, 3, 4], [True, False, True, True]) == 3  # S: 0.75 at 1, 0.375 at 3
    assert km_median([5, 6], [False, False]) is None


def test_paper_trade_round_trip_and_conservative_fill():
    entry = _row(b_bid=500.0)
    exit_ = _row(b_ask=400.0, d_bid=0.0035)
    t = paper_trade(entry, exit_, "b_over_d", max_ask_b=600.0, max_ask_d_usd=300.0, last_index_d=1e5, last_index_b=1e5)
    assert t["gross_usd"] == pytest.approx(150.0)  # (500 - 400) + (350 - 300)
    assert t["pnl_usd"] == pytest.approx(30.0) and not t["conservative"]  # 4 legs x 30
    # no exit snapshot: buy back Bybit at max(2 x 600, intrinsic 0), sell Deribit at 0
    t = paper_trade(entry, None, "b_over_d", max_ask_b=600.0, max_ask_d_usd=300.0, last_index_d=1e5, last_index_b=1e5)
    assert t["gross_usd"] == pytest.approx(500 - 1200 + 0 - 300) and t["conservative"]
    # d_over_b: sell Deribit at bid 0.002 (200), buy Bybit at ask 150; exit Deribit ask 0.001 (100), Bybit bid 160
    t = paper_trade(
        _row(b_ask=150.0),
        _row(d_ask=0.001, b_bid=160.0),
        "d_over_b",
        max_ask_b=None,
        max_ask_d_usd=None,
        last_index_d=1e5,
        last_index_b=1e5,
    )
    assert t["gross_usd"] == pytest.approx(100 + 10)
    cap = paper_trade(
        _row(b_ask=150.0),
        _row(d_ask=0.001, b_bid=160.0),
        "d_over_b",
        max_ask_b=None,
        max_ask_d_usd=None,
        last_index_d=1e5,
        last_index_b=1e5,
        bybit_cap=0.07,
    )
    assert cap["fees_usd"] == pytest.approx(30 + 30 + 0.07 * 150 + 0.07 * 160)
    assert math.isfinite(cap["pnl_frac"])
