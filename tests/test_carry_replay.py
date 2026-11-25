"""Replay of the paper carry engine's journal with the backtest code (criterion (a), analytics/carry_replay.py).

The journals are written by the real engine (``run_cycle``) on a fixture feed of synthetic markets, so a correct
engine must replay to 100 % and a corrupted record must be caught.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from carry_synth import DAY, H8, markets_a, markets_b
from tradedesk.analytics import carry_replay as cr
from tradedesk.analytics.carry import _Leg, _Market
from tradedesk.analytics.carry_rules import DECISION_DELAY_MS, carry_open
from tradedesk.collector.carry_paper_engine import PARAMS_A, PARAMS_B, CarryParams, FeedData, run_cycle
from tradedesk.core.envelope import Evidence, Fetched
from tradedesk.core.errors import ProviderError
from tradedesk.store import Store

LB = 7 * DAY
H4 = 4 * 3_600_000


class Feed:
    """Historical fixture feed; ``drop`` hides symbols for one cycle (the engine sets a held one aside)."""

    def __init__(self, markets: dict[str, Any], strategy: str) -> None:
        self.markets, self.strategy = markets, strategy
        self.drop: set[str] = set()

    async def load(
        self, params: CarryParams, decision_ts: int, open_positions: list[dict[str, Any]], *, held_only: bool = False
    ) -> FeedData:
        mk = {s: d for s, d in self.markets.items() if s not in self.drop}
        spot = {s: (s.replace("USDT:USDT", "USDT"), 1) for s in mk} if self.strategy == "A" else {}
        return FeedData(markets=mk, spot_symbols=spot, universe_size=len(mk))

    async def book(self, venue: str, symbol: str) -> dict[str, Any] | None:
        return {"bids": [[99.9, 50.0], [99.0, 500.0]], "asks": [[100.1, 50.0], [101.0, 500.0]], "timestamp": None}


def _first_funding(mk: dict[str, Any], strategy: str) -> int:
    if strategy == "A":
        return min(d["funding"][0]["ts_ms"] for d in mk.values())
    return min(max(d["a"]["funding"][0]["ts_ms"], d["b"]["funding"][0]["ts_ms"]) for d in mk.values())


async def _journal(store: Store, strategy: str) -> dict[str, Any]:
    async def rows(sql: str) -> list[dict[str, Any]]:
        cur = await store.conn.execute(sql, (strategy,))
        return [dict(r) for r in await cur.fetchall()]

    return cr.journal_from_rows(
        await rows("SELECT * FROM cp_cycle WHERE strategy = ? ORDER BY cycle_ts"),
        await rows("SELECT * FROM cp_log WHERE strategy = ? ORDER BY id"),
        await rows("SELECT * FROM cp_position WHERE strategy = ? ORDER BY opened_ts, trade_id"),
    )


def _funding_rows(mk: dict[str, Any], strategy: str) -> dict[tuple[str, str], list[tuple[int, float]]]:
    if strategy == "A":
        return {(cr.PERP_VENUE, s): [(e["ts_ms"], e["rate"]) for e in d["funding"]] for s, d in mk.items()}
    out = {}
    for s, d in mk.items():
        out[(cr.PERP_VENUE, s)] = [(e["ts_ms"], e["rate"]) for e in d["a"]["funding"]]
        out[(cr.BYBIT, s)] = [(e["ts_ms"], e["rate"]) for e in d["b"]["funding"]]
    return out


async def _run(
    strategy: str, days: int = 45, stop_last: int = 0, drop_held_at: int | None = None, db: Any = None, params: CarryParams | None = None
) -> tuple[dict, CarryParams, dict, int]:
    """Drive the real engine over the synthetic segment; returns (markets, params, journal, last cycle ts). With ``db``
    (a file) the store stays on disk and ``cp_funding`` gets the source history, as the live feed would store it."""
    if strategy == "A":
        mk, params = markets_a(days=days), params or replace(PARAMS_A, slots=3, entry_apr=0.10)
    else:
        mk, params = markets_b(days=days), params or replace(PARAMS_B, slots=3, entry_apr=0.10, stop_move=0.2)
    feed = Feed(mk, strategy)
    s = Store(db or ":memory:")
    await s.init()
    if db is not None:
        for (venue, sym), rows in _funding_rows(mk, strategy).items():
            await s.cp_funding_put(venue, sym, rows, covered_from=None, covered_to=None)
    try:
        start = (_first_funding(mk, strategy) + LB) // H8 * H8 + H8
        cycles = list(range(start, start + (days - 9) * 3 * H8, H8))
        for i, c_ts in enumerate(cycles):
            feed.drop = set()
            if drop_held_at is not None and i == drop_held_at:
                feed.drop = {p["symbol"] for p in await s.cp_positions(strategy, "open")}
            stopped = "test stop" if i >= len(cycles) - stop_last else None
            out = await run_cycle(s, feed, params, c_ts, clock=lambda c=c_ts: c + DECISION_DELAY_MS, entries_stopped=stopped)
            assert out["status"] == "done", out
        return mk, params, await _journal(s, strategy), cycles[-1]
    finally:
        await s.aclose()


@pytest.fixture(scope="module")
def runs() -> dict[str, Any]:
    import asyncio

    return {
        "A": asyncio.run(_run("A", stop_last=12)),
        "B": asyncio.run(_run("B", drop_held_at=40)),
    }


def _stop_from(journal: dict[str, Any]) -> dict[str, int]:
    """The synthetic A run stops entries in its last cycles: allow exactly those (the registered date is 2026-11-12)."""
    first = min((c["cycle_ts"] for c in journal["cycles"] if c["payload"].get("entries_stopped")), default=None)
    return {} if first is None else {"A": first}


def _a1(run: tuple, *, funding: bool = True, journal: dict | None = None, **kw: Any) -> dict[str, Any]:
    mk, params, j, last = run
    j = j if journal is None else journal
    kw.setdefault("as_of_ms", last)
    kw.setdefault("entries_stop_from", _stop_from(j))
    rows = _funding_rows(mk, params.strategy) if funding else None
    return cr.replay_a1(cr.rules_from(params), j, funding_rows=kw.pop("funding_rows", rows), **kw)


# ---------- tolerance ----------


def test_close_enough_registered_tolerance():
    assert cr.close_enough(1.0, 1.0 + 0.9e-6)
    assert not cr.close_enough(1.0, 1.0 + 1.1e-6)
    assert cr.close_enough(0.0, 5e-10)  # absolute floor near zero (fractions of notional)
    assert not cr.close_enough(0.0, 2e-9)
    assert cr.close_enough(None, None) and not cr.close_enough(None, 0.0)
    # prices and quantities get no floor: a 1e-4 price off by 1e-5 relative is a difference
    assert cr.close_enough(1e-4, 1e-4 * (1 + 9e-6)) and not cr.close_enough(1e-4, 1e-4 * (1 + 9e-6), abs_tol=0.0)


# ---------- (a.1) ----------


@pytest.mark.parametrize("strategy", ["A", "B"])
def test_engine_journal_replays_to_100_percent(runs, strategy):
    out = _a1(runs[strategy])
    _, _, journal, _ = runs[strategy]
    assert out["mismatches"] == [] and out["unverifiable"] == []
    assert out["passed"] and out["status"] == "pass"
    d = out["decisions"]
    assert d["total"] == d["matched"] > 0
    assert d["by_action"]["open"][0] > 0 and d["by_action"]["close"][0] > 0 and d["by_action"]["hold"][0] > 0
    assert out["accounting"]["total"] == out["accounting"]["matched"] > 0
    assert out["apr_vs_cp_funding"]["total"] == out["apr_vs_cp_funding"]["matched"] > 0
    assert out["payments_vs_cp_funding"]["total"] == out["payments_vs_cp_funding"]["matched"] > 0
    assert out["trades"]["closed"] > 0 and out["grid_holes"] == []
    assert out["cycles"] == {"done": len(journal["cycles"])}


def test_stopped_entries_and_aside_are_replayed(runs):
    _, _, journal, _ = runs["A"]
    stopped = [c for c in journal["cycles"] if c["payload"].get("entries_stopped")]
    assert len(stopped) == 12 and all(not any(d["action"] == "open" for d in c["payload"]["decisions"]) for c in stopped)
    assert _a1(runs["A"])["passed"]
    _, _, journal_b, _ = runs["B"]
    assert [c for c in journal_b["cycles"] if cr.aside_symbols(c["payload"])], "the B scenario must set a held position aside"
    out = _a1(runs["B"])
    assert out["passed"]
    assert out["aside"] and {x["issue"] for x in out["aside"]} == {"open_position_data_unavailable"}  # listed, cycle by cycle


def test_entries_stopped_only_where_the_owner_allowed_it(runs):
    # A's stop before the allowed date (the registered 2026-11-12 is after the synthetic period): a mismatch
    out = _a1(runs["A"], entries_stop_from=cr.ENTRIES_STOP_FROM)
    assert not out["passed"] and any(m["field"] == "entries_stopped_not_allowed" for m in out["mismatches"])
    # B may never stop entries
    _, _, journal, _ = runs["B"]
    bad = copy.deepcopy(journal)
    bad["cycles"][5]["payload"]["entries_stopped"] = "config bug"
    out = _a1(runs["B"], journal=bad)
    assert not out["passed"] and any(m["field"] == "entries_stopped_not_allowed" for m in out["mismatches"])
    # an A cycle after the allowed date without the flag is listed
    _, _, ja, _ = runs["A"]
    out = _a1(runs["A"], entries_stop_from={"A": ja["cycles"][0]["cycle_ts"]})
    assert len(out["cycles_after_stop_without_flag"]) == len(ja["cycles"]) - 12


def test_empty_or_skipped_journal_is_not_a_pass(runs):
    _, params, journal, last = runs["B"]
    rules = cr.rules_from(params)
    empty = cr.replay_a1(rules, {"cycles": [], "logs": [], "positions": []}, as_of_ms=last, funding_rows={})
    assert not empty["passed"] and empty["status"] == "unverifiable" and "no_done_cycle" in empty["unverifiable"]
    assert cr.verdict(empty, None) == "◯"
    skipped = {"cycles": [{**c, "status": "skipped", "payload": {}} for c in journal["cycles"]], "logs": [], "positions": []}
    out = cr.replay_a1(rules, skipped, as_of_ms=last, funding_rows={})
    assert not out["passed"] and "no_decision" in out["unverifiable"]
    assert not _a1(runs["B"], funding=False)["passed"]  # without cp_funding (a.1-F) / (a.1-P) do not run: not a pass


def test_decision_time_and_as_of_cycle_are_checked(runs):
    _, _, journal, last = runs["B"]
    bad = copy.deepcopy(journal)
    bad["cycles"][3]["payload"]["decision_ts"] += 3_600_000  # a decision an hour after the funding time: look-ahead
    out = _a1(runs["B"], journal=bad)
    assert any(m["field"] == "decision_ts" for m in out["mismatches"]) and not out["passed"]
    out = _a1(runs["B"], as_of_ms=last + H8)  # the last expected cycle is not recorded
    assert out["grid_holes"] == [last + H8] and "as_of_cycle_not_recorded" in out["unverifiable"] and not out["passed"]


def _first(journal: dict[str, Any], action: str) -> tuple[dict[str, Any], dict[str, Any]]:
    for c in journal["cycles"]:
        for d in c["payload"]["decisions"]:
            if d["action"] == action:
                return c, d
    raise AssertionError(action)


@pytest.mark.parametrize("strategy", ["A", "B"])
def test_corrupted_decision_is_detected(runs, strategy):
    _, _, journal, _ = runs[strategy]
    bad = copy.deepcopy(journal)
    c, d = _first(bad, "hold")
    d["action"], d["reason"] = "close", "apr<=exit"  # the engine claims a close its own inputs do not support
    out = _a1(runs[strategy], journal=bad)
    assert not out["passed"]
    assert any(m["kind"] == "decision" and m["cycle_ts"] == c["cycle_ts"] and m["symbol"] == d["symbol"] for m in out["mismatches"])
    assert cr.verdict(out, None) == "✗"


def test_duplicate_decision_is_detected(runs):
    _, _, journal, _ = runs["A"]
    bad = copy.deepcopy(journal)
    c, d = _first(bad, "open")
    c["payload"]["decisions"].append(dict(d))  # two opens of one symbol would fold into one key
    out = _a1(runs["A"], journal=bad)
    assert any(m["field"] == "duplicate_open" for m in out["mismatches"]) and not out["passed"]


def test_corrupted_candidate_apr_is_detected(runs):
    _, params, journal, _ = runs["A"]
    bad = copy.deepcopy(journal)
    c, d = _first(bad, "open")
    for cand in c["payload"]["candidates"]:
        if cand["symbol"] == d["symbol"]:
            cand["trailing_apr"] = params.entry_apr / 2  # recorded input below the entry threshold, decision says open
    out = _a1(runs["A"], journal=bad)
    assert any(m["kind"] == "decision" and m["symbol"] == d["symbol"] for m in out["mismatches"])


@pytest.mark.parametrize("strategy", ["A", "B"])
def test_corrupted_funding_row_is_detected(runs, strategy):
    _, _, journal, _ = runs[strategy]
    bad = copy.deepcopy(journal)
    row = next(r for r in bad["logs"] if r["kind"] == "funding")
    row["payload"]["rate"] *= 1.01  # the journaled payment differs from what the trade accrued
    out = _a1(runs[strategy], journal=bad)
    kinds = {m["kind"] for m in out["mismatches"]}
    assert not out["passed"]
    assert "funding_row" in kinds  # usd no longer equals rate x price / entry x notional
    assert "payments_vs_cp_funding" in kinds  # and the rate differs from the stored history


def test_missing_mark_row_is_detected(runs):
    _, _, journal, _ = runs["B"]
    bad = copy.deepcopy(journal)
    i = next(i for i, r in enumerate(bad["logs"]) if r["kind"] == "mark")
    del bad["logs"][i]
    out = _a1(runs["B"], journal=bad)
    assert any(m["field"] == "mark_or_close_row" for m in out["mismatches"])


def test_journal_and_position_completeness(runs):
    _, _, journal, _ = runs["B"]
    # an orphan side row, a close with one side, and a closed trade whose cp_position row still says open
    bad = copy.deepcopy(journal)
    c0 = bad["cycles"][2]["cycle_ts"]
    bad["logs"].append({"kind": "side", "cycle_ts": c0, "trade_id": "B:GHOST:1", "payload": {"event": "open", "venue": "bybit"}})
    i = next(i for i, r in enumerate(bad["logs"]) if r["kind"] == "side" and r["payload"]["event"] == "close")
    del bad["logs"][i]
    pos = next(p for p in bad["positions"] if p["status"] == "closed")
    pos["status"] = "open"
    out = _a1(runs["B"], journal=bad)
    fields = {m["field"] for m in out["mismatches"]}
    assert {"orphan_open_side_rows", "side_rows", "cp_position"} <= fields and not out["passed"]
    # a mark row in a cycle that is not done, and a mark plus a close of one trade in one cycle
    bad = copy.deepcopy(journal)
    mk_row = next(r for r in bad["logs"] if r["kind"] == "mark")
    bad["logs"].append({**copy.deepcopy(mk_row), "kind": "close"})
    out = _a1(runs["B"], journal=bad)
    assert "mark_and_close_row" in {m["field"] for m in out["mismatches"]}
    bad = copy.deepcopy(journal)
    bad["cycles"][10]["status"] = "skipped"
    out = _a1(runs["B"], journal=bad)
    assert any(m["field"].endswith("_row_outside_done_cycle") for m in out["mismatches"])


def test_gap_confirmed_exempts_only_its_own_cycle(runs):
    mk, params, journal, _ = runs["A"]
    ix = cr.index_logs(journal["logs"])
    by_tid: dict[str, list[int]] = {}
    for c, tid in ix.mark:
        by_tid.setdefault(tid, []).append(c)
    tid, cs = next((k, sorted(v)) for k, v in by_tid.items() if len(v) >= 3)
    c1, c2 = cs[0], cs[2]
    sym = tid.split(":", 1)[1].rsplit(":", 1)[0]  # 'A:<symbol>:<cycle_ts>'
    bad = copy.deepcopy(journal)
    bad["logs"].append({"kind": "event", "cycle_ts": c1, "trade_id": tid, "payload": {"type": "funding_gap_confirmed"}})
    rows = _funding_rows(mk, "A")
    win = [(c - H8 + DECISION_DELAY_MS, c + DECISION_DELAY_MS) for c in (c1, c2)]
    rows[(cr.PERP_VENUE, sym)] = [(ts, r) for ts, r in rows[(cr.PERP_VENUE, sym)] if not any(a < ts <= b for a, b in win)]
    out = _a1(runs["A"], journal=bad, funding_rows=rows)
    pay = {m["cycle_ts"] for m in out["mismatches"] if m["kind"] == "payments_vs_cp_funding" and m["symbol"] == sym}
    assert c1 not in pay and c2 in pay  # the event exempts its own cycle, not the trade's whole life
    assert out["payments_vs_cp_funding"]["exempt_by_reason"].get("funding_gap_confirmed") == 1


def test_replay_before_t_ignores_later_journal(runs):
    """No look-ahead: results as of t do not change when the journal after t changes."""
    _, _, journal, _ = runs["B"]
    t = journal["cycles"][len(journal["cycles"]) // 2]["cycle_ts"]
    later = copy.deepcopy(journal)
    for c in later["cycles"]:
        if c["cycle_ts"] > t:
            c["payload"]["decisions"] = []
            c["payload"]["candidates"] = []
    for r in later["logs"]:
        if r["cycle_ts"] > t and r["kind"] == "funding":
            r["payload"]["rate"] = 1.0
    a = _a1(runs["B"], as_of_ms=t)
    b = _a1(runs["B"], journal=later, as_of_ms=t)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    assert a["passed"]


# ---------- (a.2) ----------


def _objs(mk: dict[str, Any], strategy: str) -> dict[str, Any]:
    if strategy == "A":
        return {s: _Market(d, H4) for s, d in mk.items()}
    return {s: (_Leg(d["a"], H4), _Leg(d["b"], H4)) for s, d in mk.items()}


OK_GUARDS = {"funding": "ok", "prices": "ok"}


def _guards(mk_engine: dict, mk_download: dict, strategy: str, journal: dict, books: dict, listed: set[str] | None = None) -> dict:
    """The cross-symbol guards as the runner computes them (window [first cycle - lookback, last decision])."""
    done = [c for c in journal["cycles"] if c["status"] == "done"]
    fi = cr.funding_integrity(
        _funding_rows(mk_engine, strategy), _funding_rows(mk_download, strategy),
        lo=int(done[0]["cycle_ts"]) - LB, hi=int(done[-1]["cycle_ts"]) + DECISION_DELAY_MS, listed=listed,
    )  # fmt: skip
    pi = cr.price_integrity(strategy, done, books, cr.index_logs(journal["logs"]), _objs(mk_download, strategy), listed=listed)
    return {"funding": fi["state"], "prices": pi["state"], "fi": fi, "pi": pi}


def _a2(
    mk_engine: dict,
    mk_download: dict,
    params: CarryParams,
    journal: dict,
    last: int,
    strategy: str,
    listed: set[str] | None = None,
    guards: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """(a.2) over every cycle; ``guards`` None: computed from the data as the runner does."""
    rules = cr.rules_from(params)
    a1 = cr.replay_a1(rules, journal, as_of_ms=last, keep_books=True, entries_stop_from=_stop_from(journal))
    ix = cr.index_logs(journal["logs"])
    if guards is None:
        g = _guards(mk_engine, mk_download, strategy, journal, a1["books"], listed)
        guards = {"funding": g["funding"], "prices": g["prices"]}
    data = cr.a2_data(
        _objs(mk_download, strategy), _funding_rows(mk_download, strategy), _funding_rows(mk_engine, strategy), listed=listed,
        guards=guards,
    )  # fmt: skip
    rows: list[dict[str, Any]] = []
    for c in journal["cycles"]:
        rows += [{**r, "cycle_ts": c["cycle_ts"]} for r in cr.replay_a2_cycle(rules, c, a1["books"][c["cycle_ts"]], ix, data)]
    return rows


@pytest.mark.parametrize("strategy", ["A", "B"])
def test_a2_on_the_same_history_matches(runs, strategy):
    mk, params, journal, last = runs[strategy]
    rows = _a2(mk, mk, params, journal, last, strategy)
    s = cr.summarize_a2(rows, skipped_cycles=0)
    if strategy == "A":
        assert s["rate"] == 1.0 and s["passed"]
    else:  # the aside cycle: the engine holds the position aside, the backtest alone may close it
        assert s["divergences_by_class"].keys() <= {"data_hold"} and s["passed"]


def _open_row(rows: list[dict[str, Any]], c_ts: int, sym: str) -> dict[str, Any]:
    return next(r for r in rows if r["cycle_ts"] == c_ts and r["symbol"] == sym and r["action"] == "open")


def test_a2_rewritten_funding_history_is_unexplained(runs):
    """A feed bug (the whole stored history differs) is never 'funding_data', even though the engine's own funding
    removes the divergence."""
    mk, params, journal, last = runs["A"]
    c, d = _first(journal, "open")
    revised = copy.deepcopy(mk)
    for e in revised[d["symbol"]]["funding"]:
        e["rate"] = -abs(e["rate"]) - 0.001
    rows = _a2(mk, revised, params, journal, last, "A")
    r = _open_row(rows, c["cycle_ts"], d["symbol"])
    assert not r["match"] and r["facts"]["resolved_by"]["funding"] and r["facts"]["funding"]["n_diff"] > cr.FUNDING_DIFF_MAX
    assert r["class"] == "unexplained"


def test_a2_one_revised_payment_is_funding_data(runs):
    """One payment of one symbol differs: 'funding_data' only while the cross-symbol funding guard is 'ok'."""
    mk, params, journal, last = runs["A"]
    c, d = _first(journal, "open")
    t = c["cycle_ts"] + DECISION_DELAY_MS
    revised = copy.deepcopy(mk)
    e = next(e for e in revised[d["symbol"]]["funding"] if t - LB < e["ts_ms"] <= t)
    e["rate"] = -1.0  # one payment revised by the venue
    rows = _a2(mk, revised, params, journal, last, "A", guards=OK_GUARDS)
    r = _open_row(rows, c["cycle_ts"], d["symbol"])
    assert r["class"] == "funding_data" and r["facts"]["funding"]["n_diff"] == 1
    assert cr.summarize_a2(rows, 0)["divergences_by_class"].get("unexplained", 0) == 0
    rows = _a2(mk, revised, params, journal, last, "A", guards={"funding": "unknown", "prices": "ok"})
    assert _open_row(rows, c["cycle_ts"], d["symbol"])["class"] == "not_checked"  # guard not decidable: never a pass


def test_a2_same_corrupted_latest_payment_in_many_symbols_is_unexplained(runs):
    """Review R1: a feed bug that corrupts the latest payment of >= 3 % of the symbols (each symbol: 1 differing
    payment, which the per-symbol rule accepts) trips the funding guard, so the divergence is 'unexplained'."""
    mk, params, journal, last = runs["A"]
    c, d = _first(journal, "open")
    t = c["cycle_ts"] + DECISION_DELAY_MS
    revised = copy.deepcopy(mk)
    e = max((e for e in revised[d["symbol"]]["funding"] if e["ts_ms"] <= t), key=lambda e: e["ts_ms"])
    e["rate"] = -1.0  # the latest payment before the decision, corrupted
    rules = cr.rules_from(params)
    a1 = cr.replay_a1(rules, journal, as_of_ms=last, keep_books=True, entries_stop_from=_stop_from(journal))
    g = _guards(mk, revised, "A", journal, a1["books"])
    assert g["fi"]["legs_with_difference"] == 1 and g["fi"]["compared"] == len(mk) == 12  # 1 / 12 = 8 % >= 3 %
    assert g["funding"] == "tripped" and g["prices"] == "ok"
    rows = _a2(mk, revised, params, journal, last, "A")
    r = _open_row(rows, c["cycle_ts"], d["symbol"])
    assert r["facts"]["funding"]["explains"] and r["facts"]["resolved_by"]["funding"]  # the per-symbol rule alone passes
    assert r["class"] == "unexplained" and r["facts"]["guards"]["funding"] == "tripped"
    assert not {x["class"] for x in rows if not x["match"]} & {"funding_data", "funding_and_price_data"}
    assert cr.verdict({"status": "pass"}, cr.summarize_a2(rows, 0)) == "✗"


def test_guard_shares_and_denominators():
    """Registered thresholds: > 2 % of compared funding legs, > 1 % of compared recorded prices; nothing compared or too
    much not compared is 'unknown' (never a silent pass)."""
    assert cr.guard_state(2, 100, 0, cr.FUNDING_LEG_SHARE_MAX) == "ok"  # exactly 2 %: not more than
    assert cr.guard_state(3, 100, 0, cr.FUNDING_LEG_SHARE_MAX) == "tripped"
    assert cr.guard_state(0, 0, 0, cr.FUNDING_LEG_SHARE_MAX) == "unknown"
    assert cr.guard_state(1, 100, 2, cr.FUNDING_LEG_SHARE_MAX) == "unknown"  # (1 + 2) / 102 > 2 %: cannot be shown small
    assert cr.guard_state(0, 1000, 10, cr.PRICE_SHARE_MAX) == "ok" and cr.guard_state(11, 1000, 0, cr.PRICE_SHARE_MAX) == "tripped"
    # funding legs: 100 symbols, the same latest payment corrupted in 3 of them
    t = 100 * H8
    eng = {(cr.PERP_VENUE, f"S{i}"): [(t - k * H8, 0.0001) for k in range(30)] for i in range(100)}
    dl = copy.deepcopy(eng)
    for i in range(3):
        dl[(cr.PERP_VENUE, f"S{i}")][0] = (t, -0.01)
    fi = cr.funding_integrity(eng, dl, lo=t - 20 * H8, hi=t)
    assert (fi["legs"], fi["compared"], fi["legs_with_difference"], fi["state"]) == (100, 100, 3, "tripped")
    for i in range(1, 3):
        dl[(cr.PERP_VENUE, f"S{i}")][0] = (t, 0.0001)
    assert cr.funding_integrity(eng, dl, lo=t - 20 * H8, hi=t)["state"] == "ok"
    # a missing download is not compared; a symbol not listed now leaves the denominator; outside the window: not a leg
    del dl[(cr.PERP_VENUE, "S50")], dl[(cr.PERP_VENUE, "S51")]
    fi = cr.funding_integrity(eng, dl, lo=t - 20 * H8, hi=t)
    assert (fi["not_compared"], fi["state"]) == (2, "unknown")  # (1 + 2) / 100 > 2 %
    fi = cr.funding_integrity(eng, dl, lo=t - 20 * H8, hi=t, listed={f"S{i}" for i in range(100)} - {"S50", "S51"})
    assert (fi["legs"], fi["not_listed"], fi["state"]) == (98, 2, "ok")
    assert cr.funding_integrity(eng, dl, lo=t + H8, hi=t + 2 * H8)["legs"] == 0
    assert cr.funding_integrity(eng, {}, lo=t - 20 * H8, hi=t)["state"] == "unknown"  # nothing downloaded


def test_funding_guard_per_venue():
    """Third review, remark 2: a bug on ONE venue below the pooled limit still trips the guard (each venue's share is
    checked too); a venue with legs but nothing compared is 'unknown'; a venue without legs plays no role."""
    t = 100 * H8
    eng = {(v, f"S{i}"): [(t - k * H8, 0.0001) for k in range(30)] for v in ("binance", "bybit") for i in range(100)}
    dl = copy.deepcopy(eng)
    for i in range(3):  # 3 of 100 Bybit legs (3 % > 2 %), 3 of 200 legs pooled (1.5 % <= 2 %)
        dl[("bybit", f"S{i}")][0] = (t, -0.01)
    fi = cr.funding_integrity(eng, dl, lo=t - 20 * H8, hi=t)
    assert (fi["legs_with_difference"], fi["compared"], fi["pooled_state"]) == (3, 200, "ok")
    assert fi["by_venue"]["bybit"]["legs_with_difference"] == 3 and fi["by_venue"]["bybit"]["state"] == "tripped"
    assert fi["by_venue"]["binance"]["state"] == "ok" and fi["state"] == "tripped"
    assert fi["by_venue"]["bybit"]["share"] == pytest.approx(0.03)
    # 2 of 100 on one venue: exactly the limit, not more -> ok everywhere
    dl[("bybit", "S2")][0] = (t, 0.0001)
    fi = cr.funding_integrity(eng, dl, lo=t - 20 * H8, hi=t)
    assert fi["by_venue"]["bybit"]["state"] == "ok" and fi["state"] == "ok"
    # a third venue with one leg and no download: pooled (2 + 1) / 201 <= 2 % is ok, but that venue is 'unknown'
    eng[("okx", "S0")] = eng[("bybit", "S0")]
    fi = cr.funding_integrity(eng, dl, lo=t - 20 * H8, hi=t)
    assert fi["pooled_state"] == "ok" and fi["by_venue"]["okx"]["share"] is None
    assert fi["by_venue"]["okx"]["state"] == "unknown" and fi["state"] == "unknown"
    # a venue whose legs all fall outside the window has no entry and does not change the state
    eng[("okx", "S0")] = [(t + 5 * H8, 0.0001)]
    fi = cr.funding_integrity(eng, dl, lo=t - 20 * H8, hi=t)
    assert "okx" not in fi["by_venue"] and fi["state"] == "ok"
    # tripped on one venue outranks unknown on another
    dl[("bybit", "S2")][0] = (t, -0.01)
    eng[("okx", "S0")] = eng[("bybit", "S0")]
    assert cr.funding_integrity(eng, dl, lo=t - 20 * H8, hi=t)["state"] == "tripped"


def test_a2_missed_stop_is_unexplained(runs):
    """The engine's bars lacked the high that triggers B's stop: the recorded extremes 'explain' the hold, but the
    downloaded extreme is higher than the recorded one, which is never a data revision."""
    mk, params, journal, last = runs["B"]
    rules = cr.rules_from(params)
    a1 = cr.replay_a1(rules, journal, as_of_ms=last, keep_books=True, entries_stop_from={})
    c, sym = next(
        (c, d["symbol"]) for c in journal["cycles"][5:] for d in c["payload"]["decisions"]
        if d["action"] == "hold" and d["symbol"] in a1["books"][c["cycle_ts"]]["book"]
    )  # fmt: skip
    p = a1["books"][c["cycle_ts"]]["book"][sym]
    t = c["cycle_ts"] + DECISION_DELAY_MS
    dl = copy.deepcopy(mk)
    bar = next(b for b in dl[sym]["a"]["bars"] if t - H8 < b["ts"] + H4 <= t)
    bar["high"] = p["a_e"] * (1 + 2 * params.stop_move)
    rows = _a2(mk, dl, params, journal, last, "B")
    bad = [r for r in rows if r["cycle_ts"] == c["cycle_ts"] and r["symbol"] == sym and not r["match"]]
    assert bad and all(r["class"] == "unexplained" for r in bad)
    assert bad[0]["facts"]["resolved_by"]["prices"] and bad[0]["facts"]["extremes"]["missed"]


def test_a2_revised_close_is_price_data(runs):
    mk, params, journal, last = runs["B"]
    c, d = _first(journal, "open")
    dl = copy.deepcopy(mk)
    bar = next(b for b in dl[d["symbol"]]["a"]["bars"] if b["ts"] + H4 == c["cycle_ts"])
    bar["close"] *= 1.05  # the bar read at t revised: the pair's price gap now exceeds max_entry_gap
    rows = _a2(mk, dl, params, journal, last, "B")
    at_c = [r for r in rows if r["cycle_ts"] == c["cycle_ts"] and not r["match"]]
    assert _open_row(rows, c["cycle_ts"], d["symbol"])["class"] == "price_data"
    assert at_c and {r["class"] for r in at_c} == {"price_data"}  # a slot taken by another symbol follows its cause


def test_a2_systematic_price_source_difference_is_unexplained(runs):
    """Review R1, prices: if the engine's prices differ from the downloaded close for > 1 % of ALL recorded prices (here:
    another price source for leg a), the revised close that removes a divergence is no 'price_data' any more."""
    mk, params, journal, last = runs["B"]
    c, d = _first(journal, "open")
    dl = copy.deepcopy(mk)
    for x in dl.values():
        for b in x["a"]["bars"]:
            b["close"] *= 1 + 1e-4  # leg a from another source: every recorded leg-a price differs by 1e-4
    bar = next(b for b in dl[d["symbol"]]["a"]["bars"] if b["ts"] + H4 == c["cycle_ts"])
    bar["close"] *= 1.05
    rules = cr.rules_from(params)
    a1 = cr.replay_a1(rules, journal, as_of_ms=last, keep_books=True, entries_stop_from=_stop_from(journal))
    g = _guards(mk, dl, "B", journal, a1["books"])
    assert g["prices"] == "tripped" and g["pi"]["share"] > 0.4 and g["pi"]["not_compared"] == 0
    rows = _a2(mk, dl, params, journal, last, "B")
    r = _open_row(rows, c["cycle_ts"], d["symbol"])
    assert r["class"] == "unexplained" and r["facts"]["guards"]["prices"] == "tripped"
    assert not {x["class"] for x in rows if not x["match"]} & {"price_data", "funding_and_price_data"}
    # the same revised close alone: a handful of recorded prices differ, far below 1 %
    one = copy.deepcopy(mk)
    next(b for b in one[d["symbol"]]["a"]["bars"] if b["ts"] + H4 == c["cycle_ts"])["close"] *= 1.05
    g = _guards(mk, one, "B", journal, a1["books"])
    assert g["prices"] == "ok" and 0 < g["pi"]["differ"] and g["pi"]["share"] <= cr.PRICE_SHARE_MAX


def _slot_case(slots: int) -> list[dict[str, Any]]:
    """A one-cycle A book holding R; the download has ONE revised payment of R (its trailing APR turns negative, so the
    backtest closes R), Y pays well on both sources and the engine recorded no Y candidate."""
    t0 = 1_700_000_000_000 // H8 * H8
    c_ts = t0 + 10 * DAY
    t = c_ts + DECISION_DELAY_MS

    def market(rate: float) -> dict[str, Any]:
        bars = [{"ts": t0 + i * H4, "close": 100.0, "high": 100.0, "low": 100.0} for i in range(12 * 6)]
        return {"funding": [{"ts_ms": t0 + i * H8 + 300, "rate": rate} for i in range(12 * 3)], "spot": bars, "perp": bars}

    engine = {"R": market(0.0003), "Y": market(0.001)}
    dl = copy.deepcopy(engine)
    dl["R"]["funding"][18]["rate"] = -1.0  # inside (t - 7 d, t], before R's entry
    rules = cr.Rules(
        "A", slots=slots, entry_apr=0.10, exit_apr=0.0, side_cost=0.001, slot_notional=2000.0, capital_factor=1.5, lookback_ms=LB
    )
    eng_rows = _funding_rows(engine, "A")
    apr_r = cr.funding_legs(eng_rows)[(cr.PERP_VENUE, "R")].apr(t, LB)
    p = carry_open("R", t - 2 * DAY, apr_r, 100.0, 100.0, 0.001)
    before = {"book": {"R": p}, "tids": {"R": "A:R:1"}}
    logs = [{"kind": "mark", "cycle_ts": c_ts, "trade_id": "A:R:1", "payload": {"spot": 100.0, "perp": 100.0, "max_runup": 0.0}}]
    logs += [
        {"kind": "funding", "cycle_ts": c_ts, "trade_id": "A:R:1",
         "payload": {"venue": cr.PERP_VENUE, "payment_ts": e["ts_ms"], "rate": e["rate"], "mark": 100.0}}
        for e in engine["R"]["funding"] if t - 2 * DAY < e["ts_ms"] <= t
    ]  # fmt: skip
    cycle = {
        "cycle_ts": c_ts, "status": "done",
        "payload": {
            "decision_ts": t, "flags": [], "decisions": [{"symbol": "R", "action": "hold", "reason": "apr>exit", "apr": apr_r}],
            "candidates": [{"symbol": "R", "trailing_apr": apr_r, "spot": 100.0, "perp": 100.0}],
        },
    }  # fmt: skip
    data = cr.a2_data({s: _Market(x, H4) for s, x in dl.items()}, _funding_rows(dl, "A"), eng_rows, guards=OK_GUARDS)
    return cr.replay_a2_cycle(rules, cycle, before, cr.index_logs(logs), data)


def test_a2_slot_competition_inherits_the_root_class():
    """Real competition (1 slot): the backtest closes R on the revised payment and gives its slot to Y. R is
    'funding_data'; Y has no difference of its own and the re-run with only R on the engine's observations removes it."""
    rows = {(r["symbol"], r["action"]): r for r in _slot_case(slots=1) if not r["match"]}
    assert set(rows) == {("R", "hold"), ("R", "close"), ("Y", "open")}
    assert rows[("R", "close")]["class"] == "funding_data"
    y = rows[("Y", "open")]
    assert y["class"] == "funding_data" and y["facts"]["slot_competition"] == ["R"]
    s = cr.summarize_a2([{**r, "cycle_ts": 1} for r in rows.values()], 0)
    assert s["slot_competition"] == {"rows": 1, "several_roots": 0}


def test_a2_engine_dropping_a_symbol_with_a_free_slot_stays_unexplained():
    """Engine bug (2 slots): Y vanished from the engine's universe with no flag and no exclusion although a slot was
    free. The same root R in the cycle does not explain it: the re-run with R on the engine's data still opens Y."""
    rows = {(r["symbol"], r["action"]): r for r in _slot_case(slots=2) if not r["match"]}
    assert rows[("R", "close")]["class"] == "funding_data"
    y = rows[("Y", "open")]
    assert y["class"] == "unexplained" and "slot_competition" not in y["facts"]


def test_a2_missing_symbol_is_not_checked_unless_delisted(runs):
    mk, params, journal, last = runs["A"]
    c, d = _first(journal, "open")
    gone = {s: v for s, v in mk.items() if s != d["symbol"]}
    rows = _a2(mk, gone, params, journal, last, "A")
    assert {r["class"] for r in rows if not r["match"] and r["symbol"] == d["symbol"]} == {"not_checked"}
    s = cr.summarize_a2(rows, 0)
    assert s.get("incomplete") and cr.verdict({"status": "pass"}, s) == "◯"
    rows = _a2(mk, gone, params, journal, last, "A", listed=set(gone))
    assert {r["class"] for r in rows if not r["match"] and r["symbol"] == d["symbol"]} == {"not_in_downloaded_history"}


def test_a2_aside_position_keeps_its_slot():
    """A position the engine set aside holds its slot in (a.2) too: the backtest closing it must not let another symbol
    open in the replay (that open would have no explanation)."""
    t0 = 1_700_000_000_000 // H8 * H8

    def market(rate: float) -> dict[str, Any]:
        bars = [{"ts": t0 + i * H4, "close": 100.0, "high": 100.0, "low": 100.0} for i in range(12 * 6)]
        return {"funding": [{"ts_ms": t0 + i * H8 + 300, "rate": rate} for i in range(12 * 3)], "spot": bars, "perp": bars}

    raw = {"X": market(-0.001), "Y": market(0.001)}
    c_ts = t0 + 10 * DAY
    t = c_ts + DECISION_DELAY_MS
    rules = cr.Rules("A", slots=1, entry_apr=0.10, exit_apr=0.0, side_cost=0.001, slot_notional=2000.0, capital_factor=1.5, lookback_ms=LB)
    p = carry_open("X", t - 2 * DAY, 0.2, 100.0, 100.0, 0.001)
    before = {"book": {"X": p}, "tids": {"X": "A:X:1"}}
    cycle = {
        "cycle_ts": c_ts, "status": "done",
        "payload": {"decision_ts": t, "flags": [{"symbol": "X", "issue": "funding_payment_missing"}], "decisions": [], "candidates": []},
    }  # fmt: skip
    data = cr.a2_data({s: _Market(d, H4) for s, d in raw.items()}, _funding_rows(raw, "A"), _funding_rows(raw, "A"))
    rows = cr.replay_a2_cycle(rules, cycle, before, cr.index_logs([]), data)
    assert {r["symbol"] for r in rows} == {"X"}
    assert {(r["action"], r["class"]) for r in rows} == {("hold", "data_hold"), ("close", "data_hold")}


def test_a2_ignores_download_after_t(runs):
    """No look-ahead in (a.2): cutting the downloaded history after t does not change the rows of the cycle at t."""
    mk, params, journal, last = runs["B"]
    c = journal["cycles"][len(journal["cycles"]) // 2]
    t = c["cycle_ts"] + DECISION_DELAY_MS
    cut = copy.deepcopy(mk)
    for d in cut.values():
        for leg in ("a", "b"):
            d[leg]["funding"] = [e for e in d[leg]["funding"] if e["ts_ms"] <= t]
            d[leg]["bars"] = [b for b in d[leg]["bars"] if b["ts"] + H4 <= t]
    revised = copy.deepcopy(mk)  # make the cycle diverge, so the facts are computed too
    _, d0 = _first(journal, "open")
    for e in revised[d0["symbol"]]["a"]["funding"]:
        e["rate"] = -e["rate"]
    cut_rev = copy.deepcopy(cut)
    cut_rev[d0["symbol"]]["a"]["funding"] = [e for e in revised[d0["symbol"]]["a"]["funding"] if e["ts_ms"] <= t]
    # the cross-symbol guards are a property of the whole registered window (computed once, over all cycles), not an
    # input of the step at t: fixed here so that only the step's own inputs are cut
    for full, part in ((mk, cut), (revised, cut_rev)):
        a = [r for r in _a2(mk, full, params, journal, last, "B", guards=OK_GUARDS) if r["cycle_ts"] == c["cycle_ts"]]
        b = [r for r in _a2(mk, part, params, journal, last, "B", guards=OK_GUARDS) if r["cycle_ts"] == c["cycle_ts"]]
        assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_classify_rules():
    pl = {"flags": [{"symbol": "X", "issue": "funding_payment_missing"}, {"symbol": "Y", "issue": "bars_or_funding_unavailable"}]}
    f_ok = {"explains": True}
    p_ok = {"explains": True, "stale": []}
    yes = {"funding": True, "prices": True, "both": True}
    base = {
        "in_engine_universe": True, "downloaded": True, "resolved_by": yes, "funding": f_ok, "prices": p_ok, "extremes": {},
        "guards": OK_GUARDS,
    }  # fmt: skip

    def cls(sym: str, payload: dict[str, Any] = pl, **kw: Any) -> str:
        return cr.classify({"symbol": sym}, payload, {**base, **kw})

    assert cls("X") == "data_hold"
    assert cls("Y") == "engine_data_unavailable"
    # missing from the engine's universe: a new listing only when the download has no payment before t - lookback
    assert cls("Z", in_engine_universe=False, new_listing=True) == "not_in_engine_universe"
    assert cls("Z", in_engine_universe=False, new_listing=False) == "unexplained"
    assert cls("Z", in_engine_universe=False, new_listing=None) == "not_checked"
    # missing from our download: delisted only when the venue no longer lists it
    assert cls("Z", downloaded=False, listed=False) == "not_in_downloaded_history"
    assert cls("Z", downloaded=False, listed=True) == "not_checked" and cls("Z", downloaded=False, listed=None) == "not_checked"
    assert cls("Z") == "funding_data"
    assert cls("Z", resolved_by={"prices": True}) == "price_data"
    assert cls("Z", resolved_by={"both": True}) == "funding_and_price_data"
    assert cls("Z", resolved_by={}) == "unexplained"
    # a re-run alone never explains: the concrete input difference must be there
    assert cls("Z", funding={"explains": False}) == "price_data"
    assert cls("Z", funding={"explains": False}, prices={"explains": False, "stale": []}) == "unexplained"
    assert cls("Z", extremes={"missed": [("max_up", 0.1, 0.6)]}) == "unexplained"
    assert cls("Z", resolved_by={"prices": True}, prices={"explains": True, "stale": [("a", 1.0, 1.1)]}) == "unexplained"
    assert cls("Z", resolved_by={"prices": True}, extremes={"above": [("max_up", 0.6, 0.1)]}) == "unexplained"
    venue_wide = {"flags": [{"issue": "provider_missing", "field": "funding_intervals:bybit"}]}
    assert cls("Z", venue_wide, resolved_by={}) == "unexplained"
    named = {"flags": [{"issue": "provider_missing", "field": "ohlcv:bybit:Z"}]}
    assert cls("Z", named, resolved_by={}) == "engine_data_unavailable"
    # exact symbol match (review remark 2): '1000PEPE/...' does not name 'PEPE/...'; A's spot leg names its perp
    pepe = {"flags": [{"issue": "provider_missing", "field": "ohlcv:bybit:1000PEPE/USDT:USDT"}]}
    assert cls("PEPE/USDT:USDT", pepe, resolved_by={}) == "unexplained"
    assert cls("1000PEPE/USDT:USDT", pepe, resolved_by={}) == "engine_data_unavailable"
    spot = {"flags": [{"issue": "provider_missing", "field": "ohlcv:binance:PEPE/USDT"}]}
    assert cls("1000PEPE/USDT:USDT", spot, resolved_by={}, spot_symbol="PEPE/USDT") == "engine_data_unavailable"
    assert cls("1000PEPE/USDT:USDT", spot, resolved_by={}) == "unexplained"
    # cross-symbol guards (review R1): tripped -> unexplained, unknown or missing -> not_checked; another class may apply
    assert cls("Z", guards={"funding": "tripped", "prices": "ok"}, resolved_by={"funding": True}) == "unexplained"
    assert cls("Z", guards={"funding": "tripped", "prices": "ok"}) == "price_data"
    assert cls("Z", guards={"funding": "ok", "prices": "tripped"}, resolved_by={"both": True}) == "unexplained"
    assert cls("Z", guards={"funding": "unknown", "prices": "ok"}, resolved_by={"funding": True}) == "not_checked"
    assert cls("Z", guards=None) == "not_checked"
    assert cls("Z", guards={"funding": "tripped", "prices": "unknown"}) == "unexplained"  # a bug sign outranks ◯
    # no downloaded bar at t for a recorded price (second review remark 5, narrowed in the third review remark 3): only a
    # data class of a divergence the engine's PRICES remove becomes not_checked; every 'unexplained' stays 'unexplained'
    gap = {"explains": False, "stale": [], "download_gap": ["a"]}
    assert cls("Z", prices=gap) == "not_checked"  # funding_data, but the prices also remove it: the bar is unverifiable
    assert cls("Z", prices=gap, resolved_by={"funding": True, "prices": True}) == "not_checked"
    assert cls("Z", prices=gap, resolved_by={"funding": True, "both": True}) == "not_checked"
    assert cls("Z", prices=gap, resolved_by={}) == "unexplained"
    assert cls("Z", prices=gap, resolved_by={"prices": True}) == "unexplained"  # only the prices remove it: stays ✗
    assert cls("Z", prices=gap, resolved_by={"both": True}) == "unexplained"
    assert cls("Z", prices=gap, resolved_by={"funding": True, "prices": True}, funding={"explains": False}) == "unexplained"
    assert cls("Z", prices=gap, resolved_by={"funding": True}) == "funding_data"  # the prices play no role
    tripped = {"funding": "tripped", "prices": "ok"}
    assert cls("Z", prices=gap, resolved_by={"funding": True, "prices": True}, guards=tripped) == "unexplained"
    for kw in ({}, {"resolved_by": {"prices": True}}, {"resolved_by": {"funding": True, "prices": True}, "guards": tripped}):
        # each of these is 'unexplained' without the gap too (the draft's ✗ stays ✗)
        no_gap = {"explains": False, "stale": []}
        assert cls("Z", prices=no_gap, **{"resolved_by": {}, **kw}) == "unexplained"


def test_input_difference_facts():
    def leg(rows: list[tuple[int, float]]) -> _Leg:
        return _Leg({"funding": [{"ts_ms": a, "rate": b} for a, b in rows], "bars": []}, H4)

    t = 100 * H8
    base = [(t - i * H8, 0.0001) for i in range(30)]
    d = cr.payment_diffs(leg(base), leg(base[1:] + [(t - 3 * H8 + 1, 0.0001)]), t, LB)
    assert d["missing_in_engine"] == [t] and d["extra_in_engine"] == [t - 3 * H8 + 1] and d["rate_diff"] == []
    halved = [(ts, r / 2) for ts, r in base]  # a normalisation bug: every rate in one ratio
    data = cr.A2Data({}, {(cr.PERP_VENUE, "S"): leg(base)}, {(cr.PERP_VENUE, "S"): leg(halved)})
    f = cr.funding_facts("A", data, "S", t, LB)
    assert f["n_diff"] > cr.FUNDING_DIFF_MAX and not f["explains"]
    two = [(ts, r / 2 if i < 2 else r) for i, (ts, r) in enumerate(base)]  # two rates in one ratio: systematic
    f = cr.funding_facts("A", cr.A2Data({}, {(cr.PERP_VENUE, "S"): leg(base)}, {(cr.PERP_VENUE, "S"): leg(two)}), "S", t, LB)
    assert f["n_diff"] == 2 and f["systematic"] and not f["explains"]
    # prices: the close read at t, a revised close, a stale one (the engine kept the previous bar), no bar at all
    bars = [{"ts": t - (k + 1) * H4, "close": 100.0 + k, "high": 101.0 + k, "low": 99.0 + k} for k in reversed(range(6))]
    obj = (_Leg({"funding": [], "bars": bars}, H4), _Leg({"funding": [], "bars": bars}, H4))
    pf = cr.price_facts("B", obj, [("a", 100.0), ("b", 100.5)], t)
    assert pf["same"] == 1 and pf["diff"] == [("b", 100.5, 100.0)] and pf["explains"]
    pf = cr.price_facts("B", obj, [("a", 101.0)], t)
    assert pf["stale"] and not pf["explains"]
    assert cr.price_facts("B", obj, [("a", 100.0)], t + 3 * H4)["download_gap"] == ["a"]


def test_funding_integrity_table():
    eng = {("v", "S"): [(1, 0.1), (2, 0.2), (4, 0.4)], ("v", "E"): [(1, 0.0)]}
    dl = {("v", "S"): [(1, 0.1), (2, 0.25), (3, 0.3), (4, 0.4)], ("v", "D"): [(1, 0.0)]}
    out = cr.funding_integrity(eng, dl)
    assert (out["compared"], out["missing_in_engine"], out["extra_in_engine"], out["rate_diff"]) == (1, 1, 0, 1)
    assert out["only_in_engine"] == 1 and out["only_in_download"] == 1 and out["worst"] == [["v", "S", 1, 0, 1]]
    # E is a leg of the engine's universe that was not downloaded: in the denominator, never silently left out
    assert (out["legs"], out["not_compared"], out["legs_with_difference"], out["share"], out["state"]) == (2, 1, 1, 1.0, "tripped")


def test_summary_and_verdict_mapping():
    def row(action: str, match: bool, cls: str | None = None) -> dict[str, Any]:
        return {"symbol": "X", "action": action, "match": match, "class": cls}

    rows = [row("hold", True)] * 18 + [row("open", False, "funding_data"), row("open", True)]
    s = cr.summarize_a2(rows, 0)
    assert s["rate"] == pytest.approx(0.95) and s["passed"] and s["open_close_rate"] == pytest.approx(0.5)
    ok_a1 = {"status": "pass", "passed": True}
    assert cr.verdict(ok_a1, s) == "≈"  # registered rate passes, entries/exits alone below 90 %: (a) NOT passed
    assert cr.verdict(ok_a1, cr.summarize_a2([row("open", True)] * 10, 0)) == "✓"
    assert cr.verdict(ok_a1, cr.summarize_a2([row("open", True)] * 9 + [row("open", False, "unexplained")], 0)) == "✗"
    assert cr.verdict(ok_a1, cr.summarize_a2([row("hold", True)] * 10, 0)) == "◯"  # no open / close to rate
    assert cr.verdict(ok_a1, None) == "◯"
    assert cr.verdict(ok_a1, {**s, "incomplete": True}) == "◯"  # our download covered too little
    assert cr.verdict({"status": "fail", "passed": False}, s) == "✗"
    assert cr.verdict({"status": "unverifiable", "passed": False}, s) == "◯"
    unexpl = cr.summarize_a2([row("open", False, "unexplained")], 0)
    assert cr.verdict({"status": "unverifiable", "passed": False}, {**unexpl, "incomplete": True}) == "✗"  # a bug is a bug
    # listed separately: set-aside positions the backtest would have closed by a STOP; several-root slot competition
    stop = {**row("close", False, "data_hold"), "cycle_ts": 7, "replay": {"kind": "stop"}, "facts": {}}
    signal = {**row("close", False, "data_hold"), "cycle_ts": 8, "replay": {"kind": "signal"}, "facts": {}}
    multi = {**row("open", False, "funding_data"), "facts": {"slot_competition": ["R1", "R2"]}}
    s = cr.summarize_a2([stop, signal, multi], 0)
    assert s["data_hold_stops"] == [[7, "X"]] and s["slot_competition"] == {"rows": 1, "several_roots": 1}


# ---------- runner ----------


class _FakeExchanges:
    """Serves the synthetic source through the provider methods the runner calls (funding history, 4h bars, markets)."""

    def __init__(
        self, mk: dict[str, Any], strategy: str, *, fail_bars: str | None = None, rate_limit: bool = False, incomplete: bool = False
    ) -> None:
        self.mk, self.strategy, self.fail_bars, self.rate_limit = mk, strategy, fail_bars, rate_limit
        self.incomplete = incomplete
        self.calls = 0

    def _ev(self, **args: Any) -> Evidence:
        return Evidence(provider="fake", tool="test", args=args)

    async def markets(self, venue: str) -> Fetched:
        return Fetched({s: {"active": True} for s in self.mk}, self._ev())

    async def funding_rate_history_range(self, venue: str, sym: str, since: int, until: int, **_: Any) -> Fetched:
        self.calls += 1
        if self.rate_limit:
            raise ProviderError(f"ccxt:{venue}", "rate_limited", "429 Too Many Requests")
        d = self.mk[sym] if self.strategy == "A" else self.mk[sym]["a" if venue == cr.PERP_VENUE else "b"]
        rows = [{"timestamp": e["ts_ms"], "fundingRate": e["rate"]} for e in d["funding"] if since <= e["ts_ms"] <= until]
        if self.incomplete:  # the provider dropped a malformed row
            return Fetched({"rows": rows[1:], "complete": False, "truncated": False, "dropped_rows": 1}, self._ev(pages=1))
        return Fetched({"rows": rows, "complete": True, "truncated": False, "dropped_rows": 0}, self._ev(pages=1))

    async def ohlcv(self, venue: str, sym: str, tf: str, limit: int, since: int, until: int) -> Fetched:
        self.calls += 1
        if self.strategy == "A":
            perp = sym.replace("/USDT", "/USDT:USDT") if venue == cr.SPOT_VENUE else sym
            if perp == self.fail_bars:
                raise ProviderError(f"ccxt:{venue}", "network", "boom")
            bars = self.mk[perp]["spot" if venue == cr.SPOT_VENUE else "perp"]
        else:
            bars = self.mk[sym]["a" if venue == cr.PERP_VENUE else "b"]["bars"]
        rows = [[b["ts"], b["close"], b.get("high", b["close"]), b.get("low", b["close"]), b["close"], 1.0] for b in bars]
        return Fetched([r for r in rows if since <= r[0] <= until][:limit], self._ev())


def _runner() -> Any:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import verify_carry_paper_replay as v

    return v


class _Clock:
    """Fake time for ``Pace``: sleeping moves the clock."""

    def __init__(self, now_ms: int) -> None:
        self.t = now_ms
        self.slept: list[float] = []

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += int(s * 1000)

    def now(self) -> int:
        return self.t

    def mono(self) -> float:
        return self.t / 1000


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory) -> dict[str, Any]:
    import asyncio

    v = _runner()
    out = {}
    for strategy in ("A", "B"):
        db = tmp_path_factory.mktemp(f"db{strategy}") / "paper.sqlite"
        mk, _, _, last = asyncio.run(_run(strategy, days=30, db=db, params=v._params(strategy)))
        out[strategy] = (db, mk, last)
    return out


def _patch(monkeypatch: Any, fake: Any) -> None:
    import tradedesk.server

    monkeypatch.setattr(tradedesk.server, "build_server", lambda: (None, SimpleNamespace(exchanges=fake)))


@pytest.mark.parametrize("strategy", ["A", "B"])
def test_runner_a1_a2_on_a_synthetic_db(tmp_path, monkeypatch, synth_db, strategy):
    """The runner's stages on an engine-written DB file (read-only), the provider layer faked: a1 and a2 pass."""
    import asyncio

    v = _runner()
    db, mk, last = synth_db[strategy]
    params = v._params(strategy)  # the registered variant: the runner replays with it
    counts = v.counts(str(db), last)
    assert counts[strategy]["done"] > 0 and counts[strategy]["params_hash"] == [params.hash()]
    a1 = v.run_a1(str(db), last)
    assert a1[strategy]["passed"] and a1[strategy]["decisions"]["by_action"]["open"][0] > 0
    fake = _FakeExchanges(mk, strategy)
    _patch(monkeypatch, fake)

    async def no_wait(self: Any, venue: str, kind: str) -> None:
        return None

    monkeypatch.setattr(v.Pace, "wait", no_wait)
    (tmp_path / "cache").mkdir()
    res = asyncio.run(v.run_a2(str(db), last, tmp_path / "cache", None))
    s = res["a2"][strategy]["summary"]
    assert s["rate"] == 1.0 and s["passed"] and not s.get("incomplete") and res["verdict"][strategy] == "✓"
    assert res["a2"][strategy]["coverage"]["must"]["rate"] == 1.0
    assert res["a2"][strategy]["cp_funding_integrity"]["legs_with_difference"] == 0
    assert res["a2"][strategy]["guards"] == {"funding": "ok", "prices": "ok"}
    pi = res["a2"][strategy]["price_integrity"]
    assert pi["compared"] == pi["recorded"] > 0 and pi["differ"] == 0
    assert set(res["code_sha256"]) == set(v.CODE_FILES) and not res["timing_sample"]
    calls = fake.calls
    asyncio.run(v.run_a2(str(db), last, tmp_path / "cache", None))
    assert fake.calls == calls  # the second run reads the per-file cache only


def test_runner_failed_download_of_a_candidate_is_not_checked(tmp_path, monkeypatch, synth_db):
    import asyncio

    v = _runner()
    db, mk, last = synth_db["A"]
    j = v.load_journal(str(db), "A")
    _, d = _first(j, "open")
    _patch(monkeypatch, _FakeExchanges(mk, "A", fail_bars=d["symbol"]))

    async def no_wait(self: Any, venue: str, kind: str) -> None:
        return None

    monkeypatch.setattr(v.Pace, "wait", no_wait)
    res = asyncio.run(v.run_a2(str(db), last, tmp_path, None))
    s = res["a2"]["A"]["summary"]
    assert s["incomplete"] and not s["passed"] and res["verdict"]["A"] == "◯"
    assert d["symbol"] in res["a2"]["A"]["coverage"]["must"]["lacking"]


def test_runner_incomplete_funding_download_is_not_cached(tmp_path, synth_db):
    """Review remark 3: a download with dropped rows is a failed download (not checked), never cached as complete."""
    import asyncio

    v = _runner()
    db, mk, last = synth_db["A"]
    sym = sorted(mk)[0]
    clock = _Clock(last + H8 + 3_600_000)
    pace = v.Pace(None, sleep=clock.sleep, now_ms=clock.now, mono=clock.mono)
    rt = SimpleNamespace(exchanges=_FakeExchanges(mk, "A", incomplete=True))
    got = asyncio.run(v.fetch_funding(rt, pace, tmp_path, cr.PERP_VENUE, sym, last - 20 * DAY, last))
    assert got is None and list(tmp_path.iterdir()) == []
    rt = SimpleNamespace(exchanges=_FakeExchanges(mk, "A"))
    assert asyncio.run(v.fetch_funding(rt, pace, tmp_path, cr.PERP_VENUE, sym, last - 20 * DAY, last))
    assert len(list(tmp_path.iterdir())) == 1


def test_runner_timing_sample_is_never_a_result(tmp_path, monkeypatch, synth_db):
    """Review remark 4: --max-symbols is a timing sample: (a.2) incomplete and the verdict forced to ◯."""
    import asyncio

    v = _runner()
    db, mk, last = synth_db["B"]
    _patch(monkeypatch, _FakeExchanges(mk, "B"))

    async def no_wait(self: Any, venue: str, kind: str) -> None:
        return None

    monkeypatch.setattr(v.Pace, "wait", no_wait)
    res = asyncio.run(v.run_a2(str(db), last, tmp_path, 3))
    assert res["timing_sample"] and res["verdict"] == {"A": "◯", "B": "◯"}
    assert any("timing sample" in x for x in res["a2"]["B"]["summary"]["incomplete_reasons"])


def test_runner_rate_limit_pauses_then_stops(tmp_path, monkeypatch, synth_db):
    import asyncio

    v = _runner()
    db, mk, last = synth_db["A"]
    fake = _FakeExchanges(mk, "A", rate_limit=True)
    _patch(monkeypatch, fake)
    clock = _Clock(last + H8 + 3_600_000)  # outside the engine's window
    pace = v.Pace(None, sleep=clock.sleep, now_ms=clock.now, mono=clock.mono)
    res = asyncio.run(v.run_a2(str(db), last, tmp_path, None, pace=pace))
    assert fake.calls == 2  # one pause after the first 429, the second stops the run: no third request
    assert v.RATE_LIMIT_PAUSE_S in clock.slept
    assert res["a2_stopped"] and res["a2"] == {"A": None, "B": None} and res["verdict"] == {"A": "◯", "B": "◯"}


def test_pace_counts_pages_and_waits_for_the_engine():
    import asyncio

    v = _runner()
    boundary = 2_000 * H8
    clock = _Clock(boundary + 10 * 60_000)  # 10 min after a funding time: inside the engine's window
    answers = iter([False, False, True])
    pace = v.Pace(lambda c: next(answers), sleep=clock.sleep, now_ms=clock.now, mono=clock.mono)

    async def go() -> None:
        await pace.wait("binanceusdm", "funding")
        assert clock.slept[0] == pytest.approx(20 * 60 + 5)  # to 30 min after the funding time
        assert clock.slept[1:] == [60, 60]  # then until both books' cycle is done
        clock.slept.clear()
        pace.note_pages("binanceusdm", "funding", 2)  # a 2-page symbol
        await pace.wait("binanceusdm", "funding")
        assert clock.slept == [pytest.approx(6.0)]  # one 3 s gap per page

    asyncio.run(go())
    assert v._retry_after_s("Retry-After: 120") == 120.0


def test_spot_mapping_comes_from_the_journal():
    v = _runner()

    def side(leg: str, sym: str, mult: int) -> dict[str, Any]:
        return {
            "kind": "side",
            "cycle_ts": 1,
            "trade_id": "A:1000PEPE/USDT:USDT:1",
            "payload": {"event": "open", "leg": leg, "symbol": sym, "contract_multiplier": mult},
        }

    logs = [side("spot", "PEPE/USDT", 1000), side("perp", "1000PEPE/USDT:USDT", 1)]
    assert v.spot_map(logs) == {"1000PEPE/USDT:USDT": ("PEPE/USDT", 1000)}


# ---------- runner cache key / empty bars / atomic write (interim review, finding 2 and remarks 3-4) ----------

_SYMS = ("币安人生/USDT:USDT", "牛来/USDT:USDT")  # both map to "_USDT_USDT" in _cache_file
_SINCE = 2_000 * H8
_UNTIL = _SINCE + 3 * DAY


class _PerSymbol:
    """Provider methods whose answer differs by symbol: funding rate and bar close = 1, 2, ... by the symbol's index."""

    def __init__(self, *, empty_bars: bool = False) -> None:
        self.empty_bars = empty_bars
        self.calls: list[str] = []

    def _x(self, sym: str) -> float:
        return float(_SYMS.index(sym) + 1)

    async def funding_rate_history_range(self, venue: str, sym: str, since: int, until: int, **_: Any) -> Fetched:
        self.calls.append(sym)
        rows = [{"timestamp": t, "fundingRate": self._x(sym) * 1e-4} for t in range(since, until + 1, H8)]
        ev = Evidence(provider="fake", tool="test", args={"pages": 1})
        return Fetched({"rows": rows, "complete": True, "truncated": False, "dropped_rows": 0}, ev)

    async def ohlcv(self, venue: str, sym: str, tf: str, limit: int, since: int, until: int) -> Fetched:
        self.calls.append(sym)
        c = self._x(sym)
        rows = [] if self.empty_bars else [[t, c, c, c, c, 1.0] for t in range(since, until + 1, 4 * 3_600_000)]
        return Fetched(rows[:limit], Evidence(provider="fake", tool="test", args={}))


def _pace(v: Any) -> Any:
    clock = _Clock(_UNTIL + H8 + 3_600_000)  # outside the engine's window
    return v.Pace(None, sleep=clock.sleep, now_ms=clock.now, mono=clock.mono)


@pytest.mark.parametrize("kind", ["funding", "bars"])
def test_runner_cache_is_keyed_by_symbol(tmp_path, kind):
    """Finding 2: two non-ASCII symbols with one _cache_file name each get their own download and their own cache file."""
    import asyncio

    v = _runner()
    fetch, field = (v.fetch_funding, "rate") if kind == "funding" else (v.fetch_bars, "close")
    venue = cr.PERP_VENUE
    fake = _PerSymbol()
    rt, pace = SimpleNamespace(exchanges=fake), _pace(v)
    for _ in range(2):  # the second round reads the cache only
        got = [asyncio.run(fetch(rt, pace, tmp_path, venue, s, _SINCE, _UNTIL)) for s in _SYMS]
        assert {r[field] for r in got[0]} == {1e-4 if kind == "funding" else 1.0}
        assert {r[field] for r in got[1]} == {2e-4 if kind == "funding" else 2.0}
    assert fake.calls == list(_SYMS) and len(list(tmp_path.iterdir())) == 2


def test_runner_empty_bars_are_unavailable_and_not_cached(tmp_path):
    """Remark 3: an empty bars answer (XMR/USDT spot in the interim run) is unavailable, never cached as a success."""
    import asyncio

    v = _runner()
    rt, pace = SimpleNamespace(exchanges=_PerSymbol(empty_bars=True)), _pace(v)
    assert asyncio.run(v.fetch_bars(rt, pace, tmp_path, cr.SPOT_VENUE, _SYMS[0], _SINCE, _UNTIL)) is None
    assert list(tmp_path.iterdir()) == []
    rt.exchanges.empty_bars = False  # the next run downloads again
    assert asyncio.run(v.fetch_bars(rt, pace, tmp_path, cr.SPOT_VENUE, _SYMS[0], _SINCE, _UNTIL))
    assert rt.exchanges.calls == [_SYMS[0]] * 2


@pytest.mark.parametrize("kind", ["funding", "bars"])
def test_runner_cut_short_cache_write_leaves_no_broken_file(tmp_path, monkeypatch, kind):
    """Remark 4: a write cut short (disk full, killed) leaves no cache file, and the next run downloads cleanly."""
    import asyncio
    from pathlib import Path

    v = _runner()
    fetch = v.fetch_funding if kind == "funding" else v.fetch_bars
    rt, pace = SimpleNamespace(exchanges=_PerSymbol()), _pace(v)
    real = Path.write_text

    def cut_short(self: Path, text: str, *a: Any, **k: Any) -> int:
        real(self, text[: len(text) // 2], *a, **k)
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_text", cut_short)
    with pytest.raises(OSError):
        asyncio.run(fetch(rt, pace, tmp_path, cr.PERP_VENUE, _SYMS[1], _SINCE, _UNTIL))
    assert list(tmp_path.iterdir()) == []
    monkeypatch.undo()
    got = asyncio.run(fetch(rt, pace, tmp_path, cr.PERP_VENUE, _SYMS[1], _SINCE, _UNTIL))
    assert got and got == asyncio.run(fetch(rt, pace, tmp_path, cr.PERP_VENUE, _SYMS[1], _SINCE, _UNTIL))
    assert len(rt.exchanges.calls) == 2 and [p.suffix for p in tmp_path.iterdir()] == [".json"]


class _EmptySpot(_FakeExchanges):
    """The synthetic source, but the spot venue answers one symbol with an empty list (like XMR/USDT)."""

    def __init__(self, mk: dict[str, Any], empty: str) -> None:
        super().__init__(mk, "A")
        self.empty = empty

    async def ohlcv(self, venue: str, sym: str, tf: str, limit: int, since: int, until: int) -> Fetched:
        if venue == cr.SPOT_VENUE and sym.replace("/USDT", "/USDT:USDT") == self.empty:
            self.calls += 1
            return Fetched([], self._ev())
        return await super().ohlcv(venue, sym, tf, limit, since, until)


def test_runner_empty_spot_bars_of_a_candidate_are_lacking_not_cached(tmp_path, monkeypatch, synth_db):
    """Remark 3 in the run: the symbol still ends in ``lacking`` (◯, no crash), and no ``[]`` file is left in the cache."""
    import asyncio

    v = _runner()
    db, mk, last = synth_db["A"]
    _, d = _first(v.load_journal(str(db), "A"), "open")
    _patch(monkeypatch, _EmptySpot(mk, d["symbol"]))

    async def no_wait(self: Any, venue: str, kind: str) -> None:
        return None

    monkeypatch.setattr(v.Pace, "wait", no_wait)
    res = asyncio.run(v.run_a2(str(db), last, tmp_path, None))
    assert res["verdict"]["A"] == "◯" and res["a2"]["A"]["summary"]["incomplete"]
    assert d["symbol"] in res["a2"]["A"]["coverage"]["must"]["lacking"]
    files = list(tmp_path.iterdir())
    assert files and all(json.loads(p.read_text()) != [] for p in files)
