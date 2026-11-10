"""Checkpoint 2026-11-30 report: pure rules (analytics/checkpoint.py) and the read-only script on a small fixture DB."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

from tradedesk.analytics import checkpoint as cp
from tradedesk.analytics.carry_rules import DECISION_DELAY_MS
from tradedesk.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import checkpoint_report as script  # noqa: E402

H = 3_600_000
DAY = 86_400_000
T = cp._ms(2026, 10, 18)  # a carry entry cycle
NOW = cp._ms(2026, 11, 2, 12)
V9 = cp._ms(2026, 10, 31, 10)  # after S12's v9 epoch
BENCH = json.loads((Path(__file__).resolve().parent.parent / "docs/paper/bench.json").read_text(encoding="utf-8"))


def _trade(i: int, setup: str, r: float, *, tf: str = "4h", opened: int | None = None, version: str | None = None, **kw):
    o = opened if opened is not None else V9 + i * H
    row = {"link_id": f"{setup}-{i}", "setup": setup, "timeframe": tf, "status": "closed", "signal_ts": o - 8 * H}
    return row | {"opened_ts": o, "closed_ts": o + H, "r_multiple": r, "version": version} | kw


def _row(rows, key):
    return next(r for r in rows if r["key"] == key)


# ------------------------------------------------------------------------------------------------------ demo


def test_decision_band_takes_largest_n_prime_and_the_wider_end():
    s28 = next(s for s in cp.DEMO if s.setup == "S28").bands
    assert cp.decision_band(s28, 45)["n_ref"] == 30
    b = cp.decision_band(s28, 120)
    assert b["n_ref"] == 100 and b["decision"] == (-0.350, 0.091)  # rolling lower end, i.i.d. upper end
    assert cp.decision_band(s28, 3)["n_ref"] == 30  # below every n: shown, verdict stays «рано»


def test_demo_labels_early_consistent_and_no():
    s3 = next(s for s in cp.DEMO if s.setup == "S3")
    early = cp.demo_forward(s3, [_trade(i, "S3", 0.0) for i in range(29)], NOW)[0]
    assert early["n"] == 29 and early["verdict"] == cp.EARLY
    ok = cp.demo_forward(s3, [_trade(i, "S3", -0.1) for i in range(30)], NOW)[0]
    assert ok["verdict"] == cp.OK and ok["band"]["n_ref"] == 30 and ok["metric"] == pytest.approx(-0.1)
    no = cp.demo_forward(s3, [_trade(i, "S3", -0.8) for i in range(55)], NOW)[0]
    assert (no["verdict"], no["side"], no["band"]["n_ref"]) == (cp.NO, "нижче", 50)
    assert any("лише позначка" in x for x in no["notes"])  # S3 between 30 and 99: a flag, not a decision
    edge = cp.demo_forward(s3, [_trade(i, "S3", -0.6) for i in range(30)], NOW)[0]  # inside rolling, below i.i.d.
    assert edge["verdict"] == cp.OK and any("на межі" in x for x in edge["notes"])


def test_demo_keeps_the_current_version_and_timeframe_only():
    s12 = next(s for s in cp.DEMO if s.setup == "S12")
    rows = [_trade(i, "S12", 0.1, version="v9") for i in range(3)] + [_trade(9, "S12", 5.0, version="v7")]
    r = cp.demo_forward(s12, rows, NOW)[0]
    assert r["n"] == 3 and any("інших версій" in x for x in r["notes"])
    s17 = next(s for s in cp.DEMO if s.setup == "S17")
    rows = [_trade(i, "S17", 0.2, tf="1d", rest_until=V9) for i in range(2)] + [_trade(5, "S17", 0.2, tf="4h", rest_until=V9)]
    assert cp.demo_forward(s17, rows, NOW)[0]["n"] == 2
    s3 = next(s for s in cp.DEMO if s.setup == "S3")  # S3: every version, the old break-even rule only described
    old = [_trade(i, "S3", 0.0, opened=cp._ms(2026, 10, 21, 20)) for i in range(4)]
    r = cp.demo_forward(s3, old + [_trade(i, "S3", 0.0) for i in range(5)], NOW)[0]
    assert r["n"] == 9 and any("старим правилом беззбитку" in x and ": 4," in x for x in r["notes"])


def test_s28_first_30_floor_and_fill_share():
    s28 = next(s for s in cp.DEMO if s.setup == "S28")
    first = [_trade(i, "S28", -0.5, rest_until=V9, opened=V9 + i * H) for i in range(30)]
    later = [_trade(30 + i, "S28", 0.5, rest_until=V9, opened=V9 + (30 + i) * H) for i in range(4)]
    rows = cp.demo_forward(s28, later + first, NOW)  # order of the input does not matter: by entry time
    main, fill = rows
    assert main["n"] == 34 and main["verdict"] == cp.OK  # whole sample: general rule, band n' 30
    assert any("перших 30" in x and "-0.500" in x and "зняти S28" in x for x in main["notes"])
    assert fill["key"] == "demo:S28:fill" and fill["n"] == 34 and fill["side"] == "вище"  # all filled: CI ≈ [0.90; 1] ∌ 61 %
    missed = [
        {"link_id": f"m{i}", "setup": "S28", "timeframe": "4h", "status": "missed", "signal_ts": V9, "rest_until": V9 + H}
        for i in range(40)
    ]
    fill = cp.fill_forward(s28, missed[:20] + first[:10], NOW)
    assert (fill["n"], fill["verdict"], fill["side"]) == (30, cp.NO, "нижче")  # 10 / 30, CI excludes 61 %
    pending = [dict(m, rest_until=NOW + DAY) for m in missed]  # deadline not passed: not counted
    assert cp.fill_forward(s28, pending, NOW)["n"] == 0


def test_s28_first_30_filled_waits_while_one_is_still_open():
    s28 = next(s for s in cp.DEMO if s.setup == "S28")
    first = [_trade(i, "S28", -0.5, rest_until=V9, opened=V9 + i * H) for i in range(30)]
    first[3] = dict(first[3], status="open", closed_ts=None, r_multiple=None)  # an early filled trade still open
    later = [_trade(30 + i, "S28", 0.5, rest_until=V9, opened=V9 + (30 + i) * H) for i in range(4)]
    main = cp.demo_forward(s28, first + later, NOW)[0]
    assert main["n"] == 33  # whole sample: closed trades only
    assert any("ще відкрито (або без R) 1" in x for x in main["notes"])
    assert not any("за часом входу:" in x for x in main["notes"])  # the 31st trade does not stand in for the open one


def test_fill_share_counts_closing_as_filled_and_notes_stale_pending():
    s28 = next(s for s in cp.DEMO if s.setup == "S28")
    filled = [_trade(i, "S28", 0.0, rest_until=V9) for i in range(10)]
    closing = [dict(t, status="closing", closed_ts=None, r_multiple=None) for t in filled]
    assert (f := cp.fill_forward(s28, closing, NOW))["n"] == 10 and f["metric"] == pytest.approx(1.0)
    base = {"setup": "S28", "timeframe": "4h", "status": "pending", "signal_ts": V9}
    stale = [base | {"link_id": f"p{i}", "rest_until": V9 + H} for i in range(3)]
    fresh = [base | {"link_id": "q", "rest_until": NOW + DAY}]
    f = cp.fill_forward(s28, filled + stale + fresh, NOW)
    assert f["n"] == 10 and f["metric"] == pytest.approx(1.0)  # pending stays out of the share
    assert any("pending з минулим строком" in x and x.endswith(": 3") for x in f["notes"])
    assert not any("pending" in x for x in cp.fill_forward(s28, filled + fresh, NOW)["notes"])


def test_fill_share_full_fill_is_above_the_passport():
    s28 = next(s for s in cp.DEMO if s.setup == "S28")
    first = [_trade(i, "S28", 0.0, rest_until=V9) for i in range(40)]
    f = cp.fill_forward(s28, first, NOW)
    assert (f["verdict"], f["side"]) == (cp.NO, "вище")


# ------------------------------------------------------------------------------------------------------ carry


def _pos(tid: str, *, opened: int = T, closed: int | None = None, funding: float = 0.002, runup: float = 0.1, reason="signal"):
    state = {"entry_apr": 0.3, "spot_e": 1.0, "fees": 0.003, "funding": funding}
    trade = None
    if closed is not None:
        trade = {"open_ts": opened, "close_ts": closed, "entry_apr": 0.3, "funding": funding, "pnl_usd": 5.0, "max_runup": runup}
    return {
        "trade_id": tid, "strategy": "A", "symbol": f"{tid}/USDT:USDT", "status": "closed" if closed else "open", "opened_ts": opened,
        "closed_ts": closed, "slot_notional": 2000.0, "state": state, "trade": trade, "exit_reason": reason if closed else None,
    }  # fmt: skip


def _mark(tid: str, k: int, *, runup: float = 0.1, funding_usd: float = 1.0, opened: int = T):
    p = {"pnl_usd": 1.0 * k, "spot": 1.0, "funding_usd": funding_usd, "max_runup": runup}
    return {"cycle_ts": opened + k * 8 * H, "kind": "mark", "trade_id": tid, "payload": p}


def _cycles(n: int, ok: bool = True):
    return [{"cycle_ts": T + k * 8 * H, "status": "done", "payload": {"on_time": ok, "complete": True}} for k in range(n)]


def test_carry_a_g_event_gives_no_and_other_criteria_wait():
    pos = [_pos("X"), _pos("Y", closed=T + 10 * DAY)]
    marks = [_mark("X", 1, runup=0.1), _mark("X", 2, runup=0.6)]  # < 0.25 -> >= 0.50 between two cycles
    logs = {"mark": marks, "side": [], "close": [], "event": []}
    r = cp.carry_forward("A", pos, logs, _cycles(40), BENCH["A"]["bench"], NOW)
    crit = {c["id"]: c["status"] for c in r["criteria"]}
    assert crit["г"] == cp.FAIL and crit["б"] == cp.UNVERIFIABLE and crit["д"] == cp.PASS and r["verdict"] == cp.NO
    slow = [_mark("X", 1, runup=0.1), _mark("X", 2, runup=0.3), _mark("X", 3, runup=0.6)]  # crossing in two steps: no event
    r = cp.carry_forward("A", pos, {**logs, "mark": slow}, _cycles(40), BENCH["A"]["bench"], NOW)
    g = next(c for c in r["criteria"] if c["id"] == "г")
    assert g["status"] == cp.UNVERIFIABLE and "подій" in g["text"] and r["verdict"] == cp.EARLY  # < 10 trades


def test_carry_v_band_costs_and_b_exemption():
    pos = [_pos(f"P{i}", closed=T + 10 * DAY) for i in range(10)]  # realized 0.002 / (10 / 365) = 7.3 %: inside [2.8; 28.1] %
    sides = [
        {"cycle_ts": T, "kind": "side", "trade_id": "P0", "payload": {"actual_cost": 0.002, "model_cost": 0.0015, "spread_bps": 3.0}}
        for _ in range(20)
    ]
    logs = {"side": sides, "mark": [], "close": [], "event": []}
    r = cp.carry_forward("A", pos, logs, _cycles(40), BENCH["A"]["bench"], NOW)
    crit = {c["id"]: c["status"] for c in r["criteria"]}
    assert crit["б"] == cp.PASS and crit["в"] == cp.PASS and crit["г"] == cp.PASS and r["verdict"] == cp.OK
    deferred = [{"cycle_ts": T, "kind": "event", "trade_id": "P0", "payload": {"type": "funding_payment_missing"}}]
    r = cp.carry_forward("A", pos, {**logs, "event": deferred}, _cycles(40), BENCH["A"]["bench"], NOW)
    assert next(c for c in r["criteria"] if c["id"] == "в")["status"] == cp.UNVERIFIABLE  # 9 left
    low = [_pos(f"P{i}", closed=T + 10 * DAY, funding=0.0) for i in range(10)]
    r = cp.carry_forward("A", low, logs, _cycles(40), BENCH["A"]["bench"], NOW)
    assert next(c for c in r["criteria"] if c["id"] == "в")["status"] == cp.FAIL and r["verdict"] == cp.NO
    b = [dict(p, strategy="B", trade=dict(p["trade"], entry_spread=0.5)) for p in low]
    r = cp.carry_forward("B", b, logs, _cycles(40), BENCH["B"]["bench"], NOW)
    assert next(c for c in r["criteria"] if c["id"] == "в")["status"] == cp.FAIL and r["verdict"] != cp.NO  # exempt for B
    late = cp.carry_forward("A", pos, logs, _cycles(40), BENCH["A"]["bench"], T + 5 * DAY)  # as of an earlier date
    assert late["n"] == 10 and next(c for c in late["criteria"] if c["id"] == "в")["status"] == cp.UNVERIFIABLE


def test_carry_d_counts_flagged_cycles_as_incomplete_and_says_why():
    cyc = _cycles(40)
    for c in cyc[:10]:  # on time, but the engine flagged a held funding gap
        c["payload"] = {"on_time": True, "complete": False, "flags": [{"issue": "funding_gap_confirmed", "symbol": "M"}]}
    for c in cyc[10:13]:  # our own fetch cut short: split from the other details
        flag = {"issue": "funding_payment_missing", "detail": "funding_history_incomplete", "symbol": "M"}
        c["payload"] = {"on_time": True, "complete": False, "flags": [flag]}
    c = cyc[13]
    c["payload"] = {"on_time": True, "complete": False, "flags": [{"issue": "funding_payment_missing", "detail": "gap", "symbol": "M"}]}
    r = cp.carry_forward("A", [_pos("X")], {"mark": [], "side": [], "close": [], "event": []}, cyc, BENCH["A"]["bench"], NOW)
    d = next(c for c in r["criteria"] if c["id"] == "д")
    assert d["status"] == cp.FAIL and "з повними даними 26 (65 %" in d["text"] and "лише вчасно 40" in d["text"]
    assert "funding_gap_confirmed 10" in d["text"]
    assert "funding_payment_missing / funding_history_incomplete 3" in d["text"] and "funding_payment_missing / gap 1" in d["text"]
    note = next(x for x in r["notes"] if "з повними даними" in x)
    assert "exploratory" in note and "біржа не опублікувала" not in note and "рішення кванта" not in note


def test_carry_b_prints_sides_without_a_book_and_keeps_the_rule():
    side = {"cycle_ts": T, "kind": "side", "trade_id": "P0"}
    book = [side | {"payload": {"actual_cost": 0.002, "model_cost": 0.0015, "spread_bps": 3.0}} for _ in range(20)]
    none = [side | {"payload": {"actual_cost": None, "model_cost": 0.0015}} for _ in range(5)]
    logs = {"side": book + none, "mark": [], "close": [], "event": []}
    b = next(c for c in cp.carry_forward("A", [_pos("X")], logs, _cycles(40), BENCH["A"]["bench"], NOW)["criteria"] if c["id"] == "б")
    assert b["status"] == cp.PASS and "сторін з книгою 20 з 25" in b["text"] and "без глибини 0 %" in b["text"]
    assert "без книги 5" in b["text"]
    logs = {**logs, "side": book[:19] + none}  # the minimum stays on sides with a book: 19 -> ◯
    b = next(c for c in cp.carry_forward("A", [_pos("X")], logs, _cycles(40), BENCH["A"]["bench"], NOW)["criteria"] if c["id"] == "б")
    assert b["status"] == cp.UNVERIFIABLE and "без книги 5" in b["text"]


def test_carry_d_counts_skipped_cycles_without_a_reason():
    cyc = _cycles(42) + [  # skipped cycles stay in the denominator: 42 / 45 = 93 % -> not passed, as before
        {"cycle_ts": T + (42 + k) * 8 * H, "status": "skipped", "reason": why, "payload": {}}
        for k, why in enumerate(["missed: engine did not run in time", None, "  "])
    ]
    logs = {"mark": [], "side": [], "close": [], "event": []}
    d = next(c for c in cp.carry_forward("A", [_pos("X")], logs, cyc, BENCH["A"]["bench"], NOW)["criteria"] if c["id"] == "д")
    assert "пропущених циклів 3, з них без причини в журналі 2" in d["text"]
    assert d["status"] == cp.FAIL and "циклів 45, вчасно (≤ 30 хв) і з повними даними 42 (93 %" in d["text"]
    with_reasons = [dict(c, reason="missed") if c["status"] == "skipped" else c for c in cyc]
    d2 = next(c for c in cp.carry_forward("A", [_pos("X")], logs, with_reasons, BENCH["A"]["bench"], NOW)["criteria"] if c["id"] == "д")
    assert d2["status"] == d["status"] and "без причини в журналі 0" in d2["text"]  # the verdict does not depend on it


def test_carry_v_ignores_the_deferred_share_and_d_warns_about_it():
    # registration (4): (v) is ◯ only when < 10 trades are left; a deferred share above 20 % is a warning next to (d)
    pos = [_pos(f"P{i}", closed=T + 10 * DAY) for i in range(13)]  # each 7.3 %: inside the bench band
    deferred = [{"cycle_ts": T, "kind": "event", "trade_id": f"P{i}", "payload": {"type": "funding_payment_missing"}} for i in range(3)]
    logs = {"side": [], "mark": [], "close": [], "event": deferred}
    r = cp.carry_forward("A", pos, logs, _cycles(40), BENCH["A"]["bench"], NOW)
    v = next(c for c in r["criteria"] if c["id"] == "в")
    d = next(c for c in r["criteria"] if c["id"] == "д")
    assert v["status"] == cp.PASS and "без відкладених) 10, у смузі бенчу 10" in v["text"]  # 3 of 13 deferred (23 %)
    assert d["status"] == cp.PASS and "частка відкладених угод 23 % (3 з 13)" in d["text"] and "ознака проблеми надійності" in d["text"]
    few = cp.carry_forward("A", pos[:12], logs, _cycles(40), BENCH["A"]["bench"], NOW)  # 9 left after the deferred: ◯
    assert next(c for c in few["criteria"] if c["id"] == "в")["status"] == cp.UNVERIFIABLE


def test_carry_g_counts_a_labelled_forced_close_proxy():
    pos = [_pos("X", closed=T + 10 * DAY), _pos("Y"), _pos("Z", closed=T + 2 * DAY)]
    cyc = _cycles(40)
    cyc[5]["payload"] = {  # 40 h after entry: X is open
        "on_time": True,
        "complete": False,
        "flags": [{"symbol": "X/USDT:USDT", "issue": "open_position_data_unavailable"}],
    }
    cyc[20]["payload"] = {  # Z closed by then: not counted for Z
        "on_time": True,
        "complete": False,
        "flags": [{"symbol": "Z/USDT:USDT", "issue": "open_position_data_unavailable"}],
    }
    gap = [{"cycle_ts": T + 16 * H, "kind": "event", "trade_id": "Y", "payload": {"type": "funding_gap_confirmed", "symbol": "Y"}}]
    logs = {"side": [], "mark": [], "close": [], "event": gap}
    r = cp.carry_forward("A", pos, logs, cyc, BENCH["A"]["bench"], NOW)
    g = next(c for c in r["criteria"] if c["id"] == "г")
    assert "проксі примусового закриття: 2 угоди за" in g["text"]
    assert "open_position_data_unavailable 1, funding_gap_confirmed 1" in g["text"]
    b = [dict(p, strategy="B") for p in pos]
    r = cp.carry_forward("B", b, logs, cyc, BENCH["B"]["bench"], NOW)
    g = next(c for c in r["criteria"] if c["id"] == "г")
    assert g["status"] == cp.UNVERIFIABLE and "не визначається" in g["text"] and "проксі примусового закриття: 2 угоди за" in g["text"]


@pytest.mark.parametrize(
    ("n", "text"),
    [(0, "0 угод"), (1, "1 угода"), (2, "2 угоди"), (4, "4 угоди"), (5, "5 угод"), (11, "11 угод"), (12, "12 угод")]
    + [(14, "14 угод"), (20, "20 угод"), (21, "21 угода"), (22, "22 угоди"), (24, "24 угоди"), (111, "111 угод"), (101, "101 угода")],
)
def test_ua_trades_agrees_the_count_form(n, text):
    assert cp.ua_trades(n) == text


def test_a_v2_independent_scan_agrees_with_the_shadow():
    pays = [{"cycle_ts": T + k * 8 * H, "trade_id": "V", "payload": {"payment_ts": T + k * 8 * H}} for k in (1, 2, 3)]
    marks = [_mark("V", k) for k in range(1, 12)]
    r1, r2 = cp.a_v2_forward([_pos("V")], pays, marks, NOW, side_cost=0.002, decision_delay_ms=DECISION_DELAY_MS)
    assert r1["counters"] == {"подій зникнення funding": 1, "пропусків": 0, "зайвих виходів": 0} and r1["verdict"] == cp.OK
    assert r2["verdict"] == cp.EARLY and r2["counters"]["подій без остаточного Δ"] == 1  # A itself still open
    assert cp.vanish_independent([T + 8 * H, T + 16 * H, T + 24 * H], T, NOW) == T + 40 * H
    assert cp.vanish_independent([T + k * 8 * H for k in range(1, 100)], T, NOW) is None


# ------------------------------------------------------------------------------------- embargo and condor

PM_ALLOWED = {
    "позицій", "тижнів входу (кластери)", "днів розв'язання", "з них уже минуло", "частки close / late / missing",
    "частка missing на +24 год", "частка n/a на +24 год",
}  # fmt: skip


def test_polymarket_exposes_counters_only(monkeypatch):
    secret = 0.987654321

    def fake_v1(rows, outcomes, now_s, t0):
        return {
            "positions": {"n": 7, "entry_weeks": 2, "resolution_days": 5, "resolution_days_ended": 3, "edge_median": secret},
            "sources": {"close_shares": {"close": 0.5, "late": 0.25, "missing": 0.25}, "h24_share_missing": 0.1},
            "sd_per_position": {"h24": {"n": 7, "sd": secret}}, "results": {"mean": secret}, "mean_clv": secret,
        }  # fmt: skip

    def fake_v2(rows, outcomes, now_s, t0, *, v1_t0):
        return {
            "positions": {"n": 2, "entry_weeks": 1, "resolution_days": 1, "edge_median": secret},
            "sources": {"h24_share_na": 0.5, "h24_share_missing": 0.0}, "power": {"sd_position": secret, "mde_now": secret},
            "sd_per_position": {"h24": {"sd": secret}},
        }  # fmt: skip

    monkeypatch.setattr(cp, "weekly_descriptive", fake_v1)
    monkeypatch.setattr(cp, "weekly_v2g", fake_v2)
    rows = cp.polymarket_forward([{"ts": 1}], [], NOW)
    assert [r["key"] for r in rows] == ["pm:v1", "pm:v2g"]
    for r in rows:
        assert r["embargo"] and r["verdict"] == cp.EARLY and r["metric"] is None and r["band"] is None
        assert set(r["counters"]) <= PM_ALLOWED
        assert "987" not in repr(r)
    text = cp.render({"now_ms": NOW, "forwards": rows, "notes": []})
    assert "987" not in text and "закрито" in text and "2027-02-23 18:20" in text and "2027-03-07 04:20" in text
    # the verdict functions are not even imported by the report's module
    assert not any(hasattr(cp, f) for f in ("final_metrics", "final_metrics_v2g", "final_metrics_v2g_own"))


def test_condor_before_t0_reads_nothing_and_pnl_stays_closed():
    a, b = cp.condor_forward(None, [{"role": "hold_lp", "mark": 123.0}], NOW)
    assert a["verdict"] == b["verdict"] == cp.EARLY and b["embargo"] and a["metric"] is None
    t0 = date(2026, 11, 6)
    a, b = cp.condor_forward(t0, [], cp._ms(2026, 11, 30))
    assert a["counters"] == {"п'ятниць з 8, що минули": 4, "валідних входів BTC": 0} and a["verdict"] == cp.EARLY
    assert b["counters"] == {"п'ятниць з 52": 4} and b["metric"] is None and b["verdict"] == cp.EARLY
    assert cp.condor_final_ms(t0) == cp._ms(2026, 12, 31, 10)  # last buy-back (Thu T0 + 7 w + 6 d, 10:00) is after 31.12 00:00
    assert cp.condor_final_ms(date(2026, 10, 30)) == cp._ms(2026, 12, 31)  # an earlier T0: the 31.12 floor decides


# ------------------------------------------------------------------------------------------- assistant_paper


def _rec(opened: int, expiry: int, *, source: str = "method", trigger: str | None = "chain", status: str = "open"):
    return {"opened_ts": opened // 1000, "expiry_ts": expiry // 1000, "status": status, "source": source, "trigger": trigger}


REC = [
    _rec(cp._ms(2026, 10, 20, 9), cp._ms(2026, 10, 23, 8), status="settled"),
    _rec(cp._ms(2026, 10, 25, 9), cp._ms(2026, 10, 30, 8)),  # 49 h after the previous expiry: one gap; expired, not settled
    _rec(cp._ms(2026, 10, 30, 9), cp._ms(2026, 12, 25, 8), trigger="regime"),  # 1 h after: no gap; standing
    _rec(cp._ms(2026, 10, 21, 9), cp._ms(2026, 10, 24, 8), source="manual", trigger=None),
    _rec(cp._ms(2026, 11, 3, 9), cp._ms(2026, 12, 25, 8)),  # after NOW: not counted
]


def test_assistant_paper_counters_and_state():
    st = {"date": "2026-11-02", "asof_utc": "2026-11-02T09:05:00Z", "state": "actual", "action": "hold"}
    r = cp.assistant_paper_forward(REC, st, NOW)
    assert (r["n"], r["verdict"], r["metric"], r["band"]) == (3, cp.COUNTS_ONLY, None, None)
    c = r["counters"]
    assert c["з них ланцюг (chain, порівнянні з H1) / за тригером (regime, event)"] == "2 / 1"
    assert c["ручних (manual, поза форвардом)"] == 1 and c["чинних (до експірації)"] == 1
    assert c["експірованих / з них ще не розраховано"] == "2 / 1"
    assert c["перша / остання угода методу"] == "2026-10-20 / 2026-10-30"
    assert c["проміжків без чинної угоди методу > 1 доби"] == "1 (разом 2.0 дн.)"
    assert c["останнє денне рішення (файл стану)"] == "2026-11-02, actual / hold, 3 год тому"
    assert any("assistant_paper_status" in x and "не закриває" in x for x in r["notes"])
    assert not any("стоїть" in x for x in r["notes"])
    old = cp.assistant_paper_forward(REC, st | {"asof_utc": "2026-10-30T09:00:00Z"}, NOW)
    assert any("старше 36 год" in x for x in old["notes"])
    later = cp.assistant_paper_forward(REC, st, cp._ms(2026, 11, 1))  # the state file is newer than the report's date
    assert "останнє денне рішення (файл стану)" not in later["counters"] and any("новіший" in x for x in later["notes"])
    assert cp._uncovered([(0, 10), (30, 40)], 0, 50) == [20, 10]  # between the spans and after the last one


def test_assistant_paper_empty_store_and_no_state_file():
    r = cp.assistant_paper_forward([], None, NOW)
    assert r["n"] == 0 and r["counters"]["угод методу"] == 0 and r["verdict"] == cp.COUNTS_ONLY
    assert "перша / остання угода методу" not in r["counters"]
    assert any("файлу стану завдання немає" in x for x in r["notes"])
    assert cp.assistant_paper_forward([], {"asof_utc": "not a date"}, NOW)["counters"].get("останнє денне рішення (файл стану)") is None


# ------------------------------------------------------------------------------------------------ the script


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "fixture.sqlite")

    async def go() -> None:
        st = Store(db)
        await st.init()
        await st.aclose()

    asyncio.run(go())
    con = sqlite3.connect(db)
    demo_cols = "link_id, venue, symbol, side, setup, timeframe, signal_ts, entry, stop, target, qty, leverage, risk_usd, comment, status"
    for i in range(31):
        o = V9 + i * H
        con.execute(
            f"INSERT INTO demo_trade ({demo_cols}, opened_ts, closed_ts, r_multiple, version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"s3-{i}",
                "bybit-demo",
                "X/USDT:USDT",
                "long",
                "S3",
                "4h",
                o - 8 * H,
                1,
                0.9,
                1.2,
                1,
                5,
                100,
                "c",
                "closed",
                o,
                o + H,
                -0.05,
                "v3",
            ),
        )
    for p in (_pos("Y", closed=T + 10 * DAY),):
        con.execute(
            "INSERT INTO cp_position (trade_id, strategy, symbol, status, opened_ts, closed_ts, slot_notional, state, trade, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                p["trade_id"],
                "A",
                p["symbol"],
                p["status"],
                p["opened_ts"],
                p["closed_ts"],
                2000.0,
                json.dumps(p["state"]),
                json.dumps(p["trade"]),
                "signal",
            ),
        )
    for c in _cycles(35):
        con.execute(
            "INSERT INTO cp_cycle (strategy, cycle_ts, status, finished_ms, payload) VALUES ('A', ?, 'done', ?, ?)",
            (c["cycle_ts"], c["cycle_ts"] + 60_000, json.dumps(c["payload"])),
        )
    con.commit()
    con.close()
    return db


def test_script_reads_the_fixture_read_only_and_writes_out(tmp_path):
    db = _db(tmp_path)
    before = Path(db).read_bytes()
    with pytest.raises(sqlite3.OperationalError):  # the loader's connection cannot write
        script.connect_ro(db).execute("DELETE FROM demo_trade")
    out = tmp_path / "rep" / "checkpoint.md"
    assert script.main(["--db", db, "--date", "2026-11-02", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert Path(db).read_bytes() == before  # nothing written to the DB
    assert "Демо S3 v3 (4h + 1d) | 31 | -0.050 |" in text and "узгоджується" in text
    for name in ("Демо S40", "Папір carry A (cc-paper)", "Папір carry B (pp-paper)", "A v2", "Polymarket v1", "v2-g", "Кондор"):
        assert name in text
    assert "(д) пройдено" in text  # 35 cycles on time
    assert "T0 ще не зафіксовано" in text  # 2026-11-02 is before the condor T0: its table is not read


def test_script_does_not_touch_the_condor_table_before_t0(tmp_path, monkeypatch):
    import condor_forward_costs as cfc

    def boom(*a, **kw):
        raise AssertionError("option_leg_snapshot read before T0")

    monkeypatch.setattr(cfc, "detect_t0", boom)
    monkeypatch.setattr(cfc, "load_rows", boom)
    db = _db(tmp_path)
    assert script.main(["--db", db, "--date", "2026-11-02"]) == 0
    calls = []
    monkeypatch.setattr(cfc, "detect_t0", lambda path, now: date(2026, 11, 6))
    monkeypatch.setattr(cfc, "load_rows", lambda path, fridays, *, with_hold: calls.append((list(fridays), with_hold)) or [])
    inp = script.load_inputs(db, cp._ms(2026, 11, 20, 12))  # after T0, before part (a) may be decided
    assert inp["condor_t0"] == date(2026, 11, 6) and calls == [([date(2026, 11, 6), date(2026, 11, 13), date(2026, 11, 20)], False)]


def test_script_loads_the_skip_reason_of_cycles(tmp_path):
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    for k, why in ((35, "missed: engine did not run in time"), (36, None)):
        con.execute(
            "INSERT INTO cp_cycle (strategy, cycle_ts, status, reason, payload) VALUES ('A', ?, 'skipped', ?, '{}')", (T + k * 8 * H, why)
        )
    con.commit()
    con.close()
    cycles = script.load_inputs(db, NOW)["carry"]["A"]["cycles"]
    assert [c["reason"] for c in cycles if c["status"] == "skipped"] == ["missed: engine did not run in time", None]
    text = cp.render(cp.build(script.load_inputs(db, NOW), NOW))
    assert "пропущених циклів 2, з них без причини в журналі 1" in text


def test_script_shows_assistant_paper_counters_without_pnl(tmp_path):
    db = _db(tmp_path)
    empty = cp.render(cp.build(script.load_inputs(db, NOW), NOW))  # the table exists, no rows, no state file
    assert "Папір assistant_paper" in empty and "- угод методу: 0" in empty and "файлу стану завдання немає" in empty
    con = sqlite3.connect(db)
    cols = (
        "opened_ts, currency, structure, expiry_ts, spot, forward, t_years, legs, premium_per_btc, entry_fees_per_btc, "
        "max_loss_per_btc, risk_basis, size_btc, deposit_usd, risk_pct, risk_usd, within_risk, status, source, trigger, pnl_usd"
    )
    for r in REC:
        con.execute(
            f"INSERT INTO rec_paper ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["opened_ts"], "BTC", "long_straddle", r["expiry_ts"], 1, 1, 0.1, "[]", 1, 0, 1, "max_loss", 0.1, 1e4, 1, 100, 1)
            + (r["status"], r["source"], r["trigger"], 12345.678),
        )
    con.commit()
    con.close()
    state = {"date": "2026-11-02", "asof_utc": "2026-11-02T09:05:00Z", "state": "changed", "action": "hold", "rv30": 0.4321}
    (tmp_path / "assistant").mkdir()
    (tmp_path / "assistant" / "paper_state.json").write_text(json.dumps(state), encoding="utf-8")
    before = Path(db).read_bytes()
    inp = script.load_inputs(db, NOW)
    assert all("pnl_usd" not in r for r in inp["rec_paper"]) and inp["paper_state"]["state"] == "changed"
    text = cp.render(cp.build(inp, NOW))
    assert Path(db).read_bytes() == before
    assert "| Папір assistant_paper (опціонні рекомендації методу) | 3 | — | — | лише лічильники |" in text
    assert "- угод методу: 3" in text and "2026-11-02, changed / hold, 3 год тому" in text
    assert "12345" not in text and "0.4321" not in text  # no P&L, nothing beyond the whitelisted state keys


CARD = "# IDEA-x\n\nСтатус: {}\n\n## Форвард\n- **Теги:** `idea:{}`, `setup:S45`; шаблон `idea:<slug>`.\n"


def test_forward_cards_take_the_first_status_line_and_every_idea_tag():
    texts = {
        "IDEA-20261028-hl-oi-cap-state-forward.md": CARD.format("forward (2026-10-28, критик)", "hl-oi-cap"),
        "IDEA-20261101-done.md": CARD.format("rejected", "done") + "\nСтатус: forward\n",  # a later status line is history
        "IDEA-20261015-touch.md": CARD.format("forward — було inconclusive", "touch"),
        "notes.md": CARD.format("forward", "notes"),  # not a card file
    }
    cards = cp.forward_cards(texts)
    assert [c["slug"] for c in cards] == ["hl-oi-cap-state-forward", "touch"]
    assert cards[0]["tags"] == {"idea:hl-oi-cap-state-forward", "idea:hl-oi-cap"}  # the registered short tag and the slug
    assert cards[1]["tags"] == {"idea:touch"}


def _plan(i: int, tags, *, verdict="enter", status="open", created=NOW // 1000 - 3600, resolved=None, **kw):
    return {"id": i, "created_ts": created, "resolved_ts": resolved, "verdict": verdict, "status": status, "tags": tags} | kw


def test_idea_plans_counters_per_card_without_r():
    cards = [{"slug": "a-card", "tags": frozenset({"idea:a-card", "idea:a"})}, {"slug": "b-card", "tags": frozenset({"idea:b-card"})}]
    now_s = NOW // 1000
    plans = [
        _plan(1, ["idea:a"], status="resolved", resolved=now_s - 60, r_result=7.777),
        _plan(2, ["idea:a-card", "idea:b-card"], verdict="wait"),  # two cards: once in the total
        _plan(3, ["idea:a"], status="resolved", resolved=now_s + 60),  # resolved after the report moment: not «оцінено»
        _plan(4, ["idea:a"], created=now_s + 60),  # created after the report moment: not counted
        _plan(5, ["idea:typo"]),  # an idea tag of no forward card
        _plan(6, ["src:demo-shadow"]),
    ]
    r = cp.idea_plans_forward(cards, plans, NOW)
    assert (r["key"], r["n"], r["verdict"], r["metric"], r["band"]) == ("ideas:plans", 3, cp.COUNTS_ONLY, None, None)
    assert r["counters"] == {
        "a-card": "планів 3, «входити» 2, оцінено 1",
        "b-card": "планів 1, «входити» 0, оцінено 0",
        "планів з тегом idea: без картки forward": 1,
    }
    assert any("≈ 30 планів «входити»" in x and "2 — не набрано" in x for x in r["notes"])
    text = cp.render({"now_ms": NOW, "forwards": [r], "notes": []})
    assert "| Картки forward: плани за тегами idea: | 3 | — | — | лише лічильники |" in text and "7.777" not in text
    empty = cp.idea_plans_forward(cards, [], NOW)
    assert empty["n"] == 0 and empty["counters"]["b-card"] == "планів 0, «входити» 0, оцінено 0"  # named even without plans
    assert any("не знайдено" in x for x in cp.idea_plans_forward([], plans, NOW)["notes"])


def test_script_lists_every_forward_card_of_the_repo(tmp_path):
    """Every card with ``Статус: forward`` in docs/ideas gets a line, with the tag it registered (if shorter)."""
    cards = {c["slug"]: c["tags"] for c in script.load_idea_cards()}
    on_disk = [
        p.name
        for p in script.IDEAS.glob("IDEA-*.md")
        if p.read_text(encoding="utf-8").split("Статус:", 1)[-1].lstrip().startswith("forward")
    ]
    assert len(cards) == len(on_disk) >= 20
    assert "idea:hl-oi-cap" in cards["hl-oi-cap-state-forward"] and "idea:hl-liq-magnet" in cards["hl-whale-liquidation-magnet"]
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    cols = "created_ts, exchange, symbol, instrument, side, entry_type, entry, stop, target, expires_ts, verdict, tags, status, r_result"
    con.execute(
        f"INSERT INTO trade_plan ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            NOW // 1000 - 3600,
            "bybit",
            "X/USDT:USDT",
            "perp",
            "short",
            "market",
            1,
            1.1,
            0.9,
            NOW // 1000,
            "enter",
            '["idea:hl-oi-cap"]',
            "resolved",
            9.876,
        ),
    )
    con.commit()
    con.close()
    inp = script.load_inputs(db, NOW)
    assert inp["plans"] and all("r_result" not in p for p in inp["plans"])
    text = cp.render(cp.build(inp, NOW))
    assert "- hl-oi-cap-state-forward: планів 1, «входити» 1, оцінено 0" in text  # resolved_ts not set: not «оцінено»
    assert "- pm-weather-forecast-vs-price: планів 0" in text and "9.876" not in text


BYBIT, CG, OI = "bybit-deribit-daily-option-dislocation-forward", "coingecko-trending-attention-fade-forward", "hl-oi-cap-state-forward"


def test_journal_row_counts_observations_and_lists_forwards_without_collection():
    """Item 312: the cards' own journals by counters only; no journal and no plan, an empty, absent or silent journal
    = «форвард без збору»; a failed query is named, not counted as empty."""
    now_s = NOW // 1000
    slugs = (
        BYBIT,
        CG,
        OI,
        "polymarket-vs-options-forward",
        "forward-paper-carry",
        "eth-exit-queue-forward",
        "alt-etp-launch-sell-the-news",
    )
    cards = [{"slug": s, "tags": frozenset({f"idea:{s}"})} for s in slugs]
    first = cp._ms(2026, 10, 23, 3) // 1000
    journals = {
        BYBIT: {"n": 412, "first": first, "last": now_s - 3600},
        CG: {"n": 60, "first": first, "last": now_s - 3 * 86_400},  # silent for three days
        OI: {"absent": True},
        "polymarket-vs-options-forward": {"n": 0, "first": None, "last": None},
        "forward-paper-carry": {"error": "OperationalError"},
    }
    plans = [
        _plan(1, ["idea:alt-etp-launch-sell-the-news"], r_result=7.777),
        _plan(2, ["idea:eth-exit-queue-forward"], created=now_s + 60),  # after the report moment: not counted
    ]
    r = script.journal_row(cards, journals, plans, NOW)
    assert (r["key"], r["n"], r["verdict"], r["metric"], r["band"]) == ("ideas:journals", 2, cp.COUNTS_ONLY, None, None)
    c = r["counters"]
    assert c[BYBIT].startswith("журнал option_xvenue_snapshot: годинних знімків BTC 412, з 2026-10-23 03:00 до 2026-11-02 11:00 UTC")
    assert c[BYBIT].endswith("вердикт: кілл не раніше 2026-11-22, остаточний не раніше 2027-01-21")  # first + 30 / + 90 days
    assert c["polymarket-vs-options-forward"].startswith("журнал pm_gap_snapshot: годинних знімків від T0 0;")
    assert c["forward-paper-carry"].startswith("журнал cp_cycle: запит до cp_cycle: OperationalError")
    assert c[OI].startswith("журналу hl_oi_cap_snapshot у цій БД нема")
    assert c["форвард без збору (4)"] == (
        f"{CG} (останній запис coingecko_trending 2026-10-30 12:00 UTC); {OI} (журналу hl_oi_cap_snapshot у БД нема); "
        "polymarket-vs-options-forward (журнал pm_gap_snapshot порожній); eth-exit-queue-forward (журналу колектора нема, планів idea: 0)"
    )
    assert "alt-etp-launch-sell-the-news" not in str(c)  # no journal of its own, but a plan: collecting, shown by the plan row
    text = cp.render({"now_ms": NOW, "forwards": [r], "notes": []})
    assert "| Картки forward: власні журнали | 2 | — | — | лише лічильники |" in text and "7.777" not in text
    unread = script.journal_row(cards, None, [], NOW)["counters"]  # the DB could not be read
    assert unread[BYBIT].startswith("журнал option_xvenue_snapshot: БД не прочитано") and unread["форвард без збору (2)"]
    with_plan = script.journal_row(cards[:2], journals, [_plan(3, [f"idea:{CG}"])], NOW)  # a silent journal, but a plan
    assert with_plan["n"] == 2 and not any(k.startswith("форвард без збору") for k in with_plan["counters"])
    assert any("не знайдено" in x for x in script.journal_row([], {}, [], NOW)["notes"])


def test_every_card_journal_belongs_to_a_card_of_the_repo():
    for slug, j in script.CARD_JOURNALS.items():
        assert list(script.IDEAS.glob(f"IDEA-*-{slug}.md")), slug
        assert j.sql.count("?") == 2 and j.sql.startswith("SELECT COUNT("), slug  # counters only, since T0 up to the report moment


def test_script_counts_card_journals_read_only(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    base = cp._ms(2026, 11, 1, 10) // 1000
    cols = "ts, currency, expiry_ts, tau_hours, strike, opt, b_bid, mode"
    rows = [(base + h * 3600, "BTC", base + 86_400, 10.0, k, "C", 0.4321, "hourly") for h in range(3) for k in (100_000.0, 101_000.0)]
    rows += [(base, "ETH", base + 86_400, 10.0, 4000.0, "C", 0.4321, "hourly")]  # ETH: descriptive, not counted
    rows += [(base + 60, "BTC", base + 86_400, 10.0, 100_000.0, "C", 0.4321, "minute")]  # minute mode: not the hourly sample
    rows += [(NOW // 1000 + 3600, "BTC", NOW // 1000 + 86_400, 10.0, 100_000.0, "C", 0.4321, "hourly")]  # after the report moment
    con.executemany(f"INSERT INTO option_xvenue_snapshot ({cols}) VALUES (?,?,?,?,?,?,?,?)", rows)
    con.executemany(
        "INSERT INTO coingecko_trending (ts, rank, coin_id, price_usd, fetched_ts) VALUES (?,?,?,?,?)",
        [(cp._ms(2026, 11, 1, 20) // 1000 + h * 3600, k, f"c{k}", 0.4321, 0) for h in range(2) for k in (1, 2)],
    )
    con.commit()
    con.close()
    before = Path(db).read_bytes()
    monkeypatch.setattr(script, "time", type("Clock", (), {"time": staticmethod(lambda: NOW / 1000)}))
    assert script.main(["--db", db]) == 0
    text = capsys.readouterr().out
    assert Path(db).read_bytes() == before  # read-only
    assert f"- {BYBIT}: журнал option_xvenue_snapshot: годинних знімків BTC 3, з 2026-11-01 10:00 до 2026-11-01 12:00 UTC;" in text
    assert f"- {CG}: журнал coingecko_trending: годинних знімків trending 2, з 2026-11-01 20:00 до 2026-11-01 21:00 UTC;" in text
    assert "вердикт: крок 1 (тест) ≈ 2027-01-30" in text  # 01.11 + 90 days
    # the fixture's 35 carry cycles stopped on 2026-10-29 08:00: silent for days, so «без збору» like the empty journals
    idle = next(ln for ln in text.splitlines() if ln.startswith("- форвард без збору ("))
    assert (
        "forward-paper-carry (останній запис cp_cycle 2026-10-29 08:00 UTC)" in idle
        and f"{OI} (журнал hl_oi_cap_snapshot порожній)" in idle
    )
    assert BYBIT not in idle and CG not in idle and "4321" not in text  # no price of a journal is read
    assert "| Картки forward: власні журнали | 2 |" in text


def test_script_survives_a_missing_db(tmp_path):
    inp = script.load_inputs(str(tmp_path / "none.sqlite"), NOW)
    assert any("не відкрито" in e for e in inp["errors"])
    text = cp.render(cp.build(inp, NOW))
    assert "Примітка: БД" in text and "Демо S3" in text


# Every table a forward block of the report reads. A DB of an older schema may lack one, a fresh one has it empty.
FORWARD_TABLES = script.FORWARD_TABLES
BLOCKS = (
    "Демо S3", "Демо S40", "Папір carry A (cc-paper)", "Папір carry B (pp-paper)", "A v2", "Polymarket v1", "v2-g", "Кондор",
    "Папір assistant_paper", "Картки forward",
)  # fmt: skip
AFTER_T0 = cp._ms(2026, 11, 20, 12)  # the condor branch reads option_leg_snapshot only after its T0


@pytest.mark.parametrize("how", ["drop", "empty"])
@pytest.mark.parametrize("table", FORWARD_TABLES)
def test_script_survives_a_missing_or_empty_forward_table(tmp_path, monkeypatch, capsys, table, how):
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    assert script._tables(con) >= set(FORWARD_TABLES)  # the migrations create them all: the guard covers real tables
    con.execute(f"DROP TABLE {table}" if how == "drop" else f"DELETE FROM {table}")
    con.commit()
    con.close()
    before = Path(db).read_bytes()
    for argv, wall_ms in ((["--date", "2026-11-02"], NOW), ([], AFTER_T0)):
        monkeypatch.setattr(script, "time", type("Clock", (), {"time": staticmethod(lambda w=wall_ms: w / 1000)}))
        assert script.main(["--db", db, *argv]) == 0
        text = capsys.readouterr().out
        for name in BLOCKS:  # every other block is still printed
            assert name in text
        demo = "| 0 |" if table == "demo_trade" else "| 31 | -0.050 |"
        assert f"Демо S3 v3 (4h + 1d) {demo}" in text  # the dropped / emptied block shows no data, the others keep theirs
        assert ("(д) пройдено" in text) == (table != "cp_cycle")
        # an absent table is named as absent (not as a failed query, not silently as zero rows); an empty one is not
        missing = [ln for ln in text.splitlines() if "таблиці немає в БД" in ln]
        assert missing == ([f"Примітка: {table}: таблиці немає в БД (старіша схема чи інша БД), блок без даних"] if how == "drop" else [])
        assert "запит не вдався" not in text  # after T0 the condor loader is not even called without its table
        assert "T0 ще не зафіксовано" in text
    assert Path(db).read_bytes() == before  # read-only (mode=ro): nothing written


def test_script_without_the_schema_names_failed_queries_not_absent_tables(tmp_path):
    db = tmp_path / "garbage.sqlite"
    db.write_bytes(b"not a sqlite database" * 100)  # opens read-only, but no query (not even sqlite_master) succeeds
    inp = script.load_inputs(str(db), AFTER_T0)
    assert not any("таблиці немає" in e for e in inp["errors"])  # unknown schema: no claim that a table is absent
    assert any(e.startswith("demo_trade: запит не вдався") for e in inp["errors"])
    assert any(e.startswith("option_leg_snapshot: запит не вдався") for e in inp["errors"])  # the condor branch still tries
    text = cp.render(cp.build(inp, AFTER_T0))
    for name in BLOCKS:
        assert name in text


def test_at_date_is_end_of_day_and_capped():
    assert script.at_date("2026-11-01", NOW) == cp._ms(2026, 11, 2) - 1
    assert script.at_date("2027-03-01", NOW) == NOW  # a future date cannot open an embargo
    assert script.at_date(None, NOW) == NOW
