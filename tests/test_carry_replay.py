"""Replay of the paper carry engine's journal with the backtest code (criterion (a), analytics/carry_replay.py).

The journals are written by the real engine (``run_cycle``) on a fixture feed of synthetic markets, so a correct
engine must replay to 100 % and a corrupted record must be caught.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
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


def _a2(
    mk_engine: dict, mk_download: dict, params: CarryParams, journal: dict, last: int, strategy: str, listed: set[str] | None = None
) -> list[dict[str, Any]]:
    rules = cr.rules_from(params)
    a1 = cr.replay_a1(rules, journal, as_of_ms=last, keep_books=True, entries_stop_from=_stop_from(journal))
    ix = cr.index_logs(journal["logs"])
    data = cr.a2_data(_objs(mk_download, strategy), _funding_rows(mk_download, strategy), _funding_rows(mk_engine, strategy), listed=listed)
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
    mk, params, journal, last = runs["A"]
    c, d = _first(journal, "open")
    t = c["cycle_ts"] + DECISION_DELAY_MS
    revised = copy.deepcopy(mk)
    e = next(e for e in revised[d["symbol"]]["funding"] if t - LB < e["ts_ms"] <= t)
    e["rate"] = -1.0  # one payment revised by the venue
    rows = _a2(mk, revised, params, journal, last, "A")
    r = _open_row(rows, c["cycle_ts"], d["symbol"])
    assert r["class"] == "funding_data" and r["facts"]["funding"]["n_diff"] == 1
    assert cr.summarize_a2(rows, 0)["divergences_by_class"].get("unexplained", 0) == 0


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
    for full, part in ((mk, cut), (revised, cut_rev)):
        a = [r for r in _a2(mk, full, params, journal, last, "B") if r["cycle_ts"] == c["cycle_ts"]]
        b = [r for r in _a2(mk, part, params, journal, last, "B") if r["cycle_ts"] == c["cycle_ts"]]
        assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_classify_rules():
    pl = {"flags": [{"symbol": "X", "issue": "funding_payment_missing"}, {"symbol": "Y", "issue": "bars_or_funding_unavailable"}]}
    f_ok = {"explains": True}
    p_ok = {"explains": True, "stale": []}
    yes = {"funding": True, "prices": True, "both": True}
    base = {"in_engine_universe": True, "downloaded": True, "resolved_by": yes, "funding": f_ok, "prices": p_ok, "extremes": {}}

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


# ---------- runner ----------


class _FakeExchanges:
    """Serves the synthetic source through the provider methods the runner calls (funding history, 4h bars, markets)."""

    def __init__(self, mk: dict[str, Any], strategy: str, *, fail_bars: str | None = None, rate_limit: bool = False) -> None:
        self.mk, self.strategy, self.fail_bars, self.rate_limit = mk, strategy, fail_bars, rate_limit
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
    from types import SimpleNamespace

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
    assert res["a2"][strategy]["cp_funding_integrity"]["symbols_with_difference"] == 0
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
