"""Paper engine for the two carry strategies: equivalence with the backtests, persistence, and no-order guarantees."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from carry_synth import DAY, H4, H8, markets_a, markets_b
from tradedesk.analytics.carry import simulate_carry_portfolio, simulate_perp_spread_portfolio
from tradedesk.analytics.carry_rules import DECISION_DELAY_MS, cycle_grid, leg_execution
from tradedesk.collector.carry_paper_engine import PARAMS_A, PARAMS_B, CarryParams, FeedData, run_cycle, run_due
from tradedesk.store import Store

LB = 7 * DAY
SLOTS = 3


class FixtureFeed:
    """Serves historical data: every lookup in the shared rule code is as-of the decision time, so the full series is safe."""

    def __init__(self, markets: dict[str, Any], strategy: str, fail: bool = False) -> None:
        self.markets, self.strategy, self.fail = markets, strategy, fail
        self.books: list[tuple[str, str]] = []

    async def load(self, params: CarryParams, decision_ts: int, open_positions: list[dict[str, Any]]) -> FeedData:
        if self.fail:
            raise RuntimeError("venue down")
        spot = {s: (s.replace("USDT:USDT", "USDT"), 1) for s in self.markets} if self.strategy == "A" else {}
        return FeedData(markets=self.markets, spot_symbols=spot, universe_size=len(self.markets))

    async def book(self, venue: str, symbol: str) -> dict[str, Any] | None:
        self.books.append((venue, symbol))
        return {"bids": [[99.9, 50.0], [99.0, 500.0]], "asks": [[100.1, 50.0], [101.0, 500.0]], "timestamp": None}


def _grid(first_funding_ts: int, last_funding_ts: int) -> list[int]:
    start = (first_funding_ts + LB) // H8 * H8 + H8 + DECISION_DELAY_MS
    return list(range(start, last_funding_ts + 1, H8))


async def _drive(store: Store, feed: FixtureFeed, params: CarryParams, times: list[int]) -> None:
    for t in times:
        out = await run_cycle(store, feed, params, t - DECISION_DELAY_MS, clock=lambda t=t: t)
        assert out["status"] == "done", out


def _same(engine_trade: dict[str, Any], bt: dict[str, Any], keys: tuple[str, ...]) -> None:
    for k in keys:
        if isinstance(bt[k], float):
            assert engine_trade[k] == pytest.approx(bt[k], rel=1e-9, abs=1e-12), k
        else:
            assert engine_trade[k] == bt[k], k


@pytest.fixture
async def store():
    s = Store(":memory:")
    await s.init()
    yield s
    await s.aclose()


async def test_engine_a_equals_backtest(store):
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    bt = simulate_carry_portfolio(mk, slots=SLOTS, entry_apr=0.10)
    f = [e["ts_ms"] for d in mk.values() for e in d["funding"]]
    times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), max(f))
    feed = FixtureFeed(mk, "A")
    await _drive(store, feed, params, times)

    closed = [r["trade"] for r in await store.cp_positions("A", "closed")]
    bt_closed = [t for t in bt["trades"] if not t["open_at_end"]]
    assert len(bt_closed) >= 3  # the segment is not vacuous
    key = lambda t: (t["open_ts"], t["symbol"])  # noqa: E731
    assert [(t["symbol"], t["open_ts"], t["close_ts"]) for t in sorted(closed, key=key)] == [
        (t["symbol"], t["open_ts"], t["close_ts"]) for t in sorted(bt_closed, key=key)
    ]
    for e, b in zip(sorted(closed, key=key), sorted(bt_closed, key=key), strict=True):
        _same(e, b, ("funding", "basis", "fees", "pnl", "entry_apr", "max_runup", "max_premium_rise"))
        assert e["pnl_usd"] == pytest.approx(b["pnl"] * params.slot_notional)
    still_open = await store.cp_positions("A", "open")
    assert sorted((r["state"]["symbol"], r["state"]["open_ts"]) for r in still_open) == sorted(
        (t["symbol"], t["open_ts"]) for t in bt["trades"] if t["open_at_end"]
    )


async def test_engine_b_equals_backtest_with_stops(store):
    mk = markets_b(days=110)
    params = replace(PARAMS_B, slots=SLOTS, entry_apr=0.10, stop_move=0.2)
    bt = simulate_perp_spread_portfolio(mk, slots=SLOTS, entry_apr=0.10, max_entry_gap=0.02, stop_move=0.2)
    firsts = [max(d["a"]["funding"][0]["ts_ms"], d["b"]["funding"][0]["ts_ms"]) for d in mk.values()]
    end = max(min(d["a"]["funding"][-1]["ts_ms"], d["b"]["funding"][-1]["ts_ms"]) for d in mk.values())
    feed = FixtureFeed(mk, "B")
    await _drive(store, feed, params, _grid(min(firsts), end))

    closed = [r["trade"] for r in await store.cp_positions("B", "closed")]
    bt_closed = [t for t in bt["trades"] if not t["open_at_end"]]
    assert len(bt_closed) >= 3 and any(t["stopped"] for t in bt_closed)
    key = lambda t: (t["open_ts"], t["symbol"])  # noqa: E731
    for e, b in zip(sorted(closed, key=key), sorted(bt_closed, key=key), strict=True):
        _same(
            e,
            b,
            ("symbol", "open_ts", "close_ts", "dir", "funding", "price_pnl", "fees", "pnl", "stopped", "max_up", "max_down", "max_gap"),
        )
        assert e["exit_reason"] == ("stop" if b["stopped"] else "signal")
    assert len(closed) == len(bt_closed)


async def test_records_funding_sides_marks_and_journal(store):
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    f = [e["ts_ms"] for d in mk.values() for e in d["funding"]]
    feed = FixtureFeed(mk, "A")
    await _drive(store, feed, params, _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), max(f)))

    closed = await store.cp_positions("A", "closed")
    assert closed
    for r in closed:
        tid, rec = r["trade_id"], r["trade"]
        funding = await store.cp_logs("A", "funding", trade_id=tid)
        assert sum(x["payload"]["usd"] for x in funding) == pytest.approx(rec["funding_usd"])
        sides = await store.cp_logs("A", "side", trade_id=tid)
        assert [(s["payload"]["event"], s["payload"]["leg"], s["payload"]["side"]) for s in sides] == [
            ("open", "spot", "buy"),
            ("open", "perp", "sell"),
            ("close", "spot", "sell"),
            ("close", "perp", "buy"),
        ]
        p = sides[0]["payload"]
        assert p["notional_usd"] == params.slot_notional and p["bid"] == 99.9 and p["ask"] == 100.1 and p["book_available"]
        assert r["journal_id"] is not None
    assert await store.table_count("journal") == len(closed)
    j = await store.journal_list(tag="cc-paper")
    assert len(j) == len(closed) and all(x["symbol"].startswith("CC:") for x in j)
    marks = await store.cp_logs("A", "mark")
    assert marks and {"basis_usd", "funding_usd", "costs_usd", "dist_to_liq_2x", "trailing_apr"} <= set(marks[0]["payload"])
    from types import SimpleNamespace

    from tradedesk.tools.paper_carry import book_view

    view = await book_view(SimpleNamespace(store=store), "A", max(f), True)
    assert view["closed"]["n"] == len(closed) and len(view["closed_trades"]) == len(closed)
    n_sides = 2 * len(await store.cp_positions("A"))  # open sides of every position + close sides of the closed ones
    assert view["cost_realism"]["with_book"] == n_sides + 2 * len(closed)
    held = [o for o in view["open"] if "mark" in o]
    assert not view["open"] or held and {"funding_usd", "pnl_usd", "dist_to_liq_2x"} <= set(held[0]["mark"])
    assert view["pnl_usd"]["realised"] == pytest.approx(sum(r["trade"]["pnl_usd"] for r in closed), abs=0.01)
    assert view["engine"]["on_time_share"] == 1.0
    cycles = await store.cp_cycles("A", 1)
    c = cycles[0]
    assert c["git_sha"] and c["params_hash"] == params.hash() and c["payload"]["on_time"] and c["payload"]["decisions"] is not None


async def test_book_view_uses_the_latest_mark_past_5000_log_rows(store):
    # Regression: book_view read cp_logs(key, "mark") = the OLDEST 5000 rows, so a long-held position showed a stale mark.
    from types import SimpleNamespace

    from tradedesk.tools.paper_carry import _iso, book_view

    t0, n = 1_700_000_000_000 // H8 * H8, 5_200
    last_ts = t0 + (n - 1) * H8

    def pos(tid: str) -> dict[str, Any]:
        return {
            "trade_id": tid, "strategy": "A", "symbol": tid, "status": "open", "opened_ts": t0,
            "slot_notional": 2000.0, "state": {"entry_apr": 0.25},
        }  # fmt: skip

    def log(kind: str, tid: str | None, ts: int, payload: dict[str, Any]) -> dict[str, Any]:
        return {"strategy": "A", "cycle_ts": ts, "kind": kind, "trade_id": tid, "payload": payload}

    side = {"actual_cost": 0.0012, "model_cost": 0.001, "spread_bps": 2.0, "depth_short": False}
    logs = [log("mark", "A-EARLY", t0, {"pnl_usd": -5.25, "funding_usd": 1.0})]
    for i in range(n):
        ts = t0 + i * H8
        logs += [
            log("mark", "A-LONG", ts, {"pnl_usd": i + 0.5, "funding_usd": float(i)}),
            log("side", "A-LONG", ts, side),
            log("event", None, ts, {"type": "funding_interval_change"}),
        ]
    logs.append(log("mark", "B-OTHER", last_ts, {"pnl_usd": 999.0}) | {"strategy": "B"})
    cycle = {"strategy": "A", "cycle_ts": last_ts, "status": "done", "payload": {"on_time": True}}
    assert await store.cp_cycle_commit(cycle, [pos("A-EARLY"), pos("A-LONG")], logs)
    assert len(await store.cp_logs("A", "mark")) == 5_000  # the default cap still applies to the generic reader

    latest = await store.cp_latest_marks("A")
    assert [(m["trade_id"], m["payload"]["pnl_usd"], m["cycle_ts"]) for m in latest] == [
        ("A-EARLY", -5.25, t0),
        ("A-LONG", n - 1 + 0.5, last_ts),
    ]
    view = await book_view(SimpleNamespace(store=store), "A", last_ts + H8, False)  # type: ignore[arg-type]
    by = {o["trade_id"]: o for o in view["open"]}
    assert by["A-LONG"]["mark"]["pnl_usd"] == pytest.approx(n - 1 + 0.5)  # the old reader gave 4998.5
    assert by["A-LONG"]["last_mark_at"] == _iso(last_ts)
    assert by["A-EARLY"]["mark"] == {"pnl_usd": -5.25, "funding_usd": 1.0}
    assert view["pnl_usd"]["open_marked_at_last_cycle"] == pytest.approx(n - 1 + 0.5 - 5.25, abs=0.01)
    assert view["cost_realism"]["sides"] == n and view["cost_realism"]["with_book"] == n  # every side, not the oldest 5000
    assert len(await store.cp_logs("A", "event", limit=None)) == n


async def test_open_pnl_counts_entry_costs_of_positions_opened_in_the_last_cycle(store):
    # Regression: a position opened in the last cycle has no mark row yet and counted as 0 in open_marked_at_last_cycle,
    # though its entry costs (state.fees, a fraction of notional) are already paid. Marked positions keep their mark.
    from types import SimpleNamespace

    from tradedesk.tools.paper_carry import book_view

    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)
    feed = FixtureFeed(mk, "A")
    i = 0
    while not await store.cp_positions("A", "open"):
        await _drive(store, feed, params, [times[i]])
        i += 1
    rows = await store.cp_positions("A", "open")
    assert not await store.cp_latest_marks("A")  # all opened in this very cycle: nothing marked yet
    entry_costs = sum(r["state"]["fees"] * r["slot_notional"] for r in rows)
    assert entry_costs > 0
    view = await book_view(SimpleNamespace(store=store), "A", times[i - 1], False)  # type: ignore[arg-type]
    assert view["pnl_usd"]["open_marked_at_last_cycle"] == pytest.approx(-entry_costs, abs=0.01)
    assert view["pnl_usd"]["total"] == pytest.approx(-entry_costs, abs=0.01)

    # One marked position (its mark already includes its own entry costs) and one opened in the last cycle.
    t0 = times[i] - DECISION_DELAY_MS
    pos = {"strategy": "B", "status": "open", "opened_ts": t0, "slot_notional": 2000.0}
    marked = pos | {"trade_id": "B-OLD", "symbol": "OLD", "state": {"dir": 1, "fees": 0.0011}}
    fresh = pos | {"trade_id": "B-NEW", "symbol": "NEW", "state": {"dir": 1, "fees": 0.0015}}
    mark = {"strategy": "B", "cycle_ts": t0, "kind": "mark", "trade_id": "B-OLD", "payload": {"pnl_usd": 7.5}}
    assert await store.cp_cycle_commit({"strategy": "B", "cycle_ts": t0, "status": "done", "payload": {}}, [marked, fresh], [mark])
    view_b = await book_view(SimpleNamespace(store=store), "B", t0 + H8, False)  # type: ignore[arg-type]
    assert view_b["pnl_usd"]["open_marked_at_last_cycle"] == pytest.approx(7.5 - 0.0015 * 2000.0, abs=0.01)


async def test_cycle_is_idempotent_and_failure_is_logged(store):
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    t = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)[0]
    feed = FixtureFeed(mk, "A")
    assert (await run_cycle(store, feed, params, t - DECISION_DELAY_MS, clock=lambda: t))["status"] == "done"
    assert (await run_cycle(store, feed, params, t - DECISION_DELAY_MS, clock=lambda: t))["status"] == "exists"
    t2 = t + H8
    out = await run_cycle(store, FixtureFeed(mk, "A", fail=True), params, t2 - DECISION_DELAY_MS, clock=lambda: t2)
    assert out["status"] == "failed" and "venue down" in out["error"]
    assert await store.cp_cycle_get("A", t2 - DECISION_DELAY_MS) is None  # unwritten: the next tick retries it
    events = await store.cp_logs("A", "event")
    assert events[-1]["payload"]["type"] == "cycle_failed"


async def test_run_due_records_missed_cycles_as_skips(store):
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    t = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)[0]
    feed = FixtureFeed(mk, "A")
    assert (await run_due(store, feed, params, clock=lambda: t))["status"] == "done"
    assert (await run_due(store, feed, params, clock=lambda: t + 600_000))["status"] == "idle"
    later = t + 3 * H8 + 60_000
    out = await run_due(store, feed, params, clock=lambda: later)
    assert out["status"] == "done" and out["cycle_ts"] == cycle_grid(later)
    rows = {c["cycle_ts"]: c for c in await store.cp_cycles("A")}
    skipped = [c for c in rows.values() if c["status"] == "skipped"]
    assert len(skipped) == 2 and all("missed" in c["reason"] for c in skipped)


async def test_run_due_skip_reason_names_failed_attempts(store):
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    t = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)[0]
    feed, down = FixtureFeed(mk, "A"), FixtureFeed(mk, "A", fail=True)
    assert (await run_due(store, feed, params, clock=lambda: t))["status"] == "done"
    failed_cycle = t + H8 - DECISION_DELAY_MS
    for k in range(3):  # three job ticks inside the next cycle, all failing
        out = await run_due(store, down, params, clock=lambda k=k: t + H8 + k * 600_000)
        assert out["status"] == "failed" and out["cycle_ts"] == failed_cycle
    later = t + 3 * H8 + 60_000  # the engine recovers two cycles later: one failed cycle, one never attempted
    assert (await run_due(store, feed, params, clock=lambda: later))["status"] == "done"
    rows = {c["cycle_ts"]: c for c in await store.cp_cycles("A")}
    failed, missed = rows[failed_cycle], rows[failed_cycle + H8]
    assert failed["status"] == "skipped" and failed["reason"] == "failed: 3 attempts, last error: RuntimeError: venue down"
    assert missed["status"] == "skipped" and missed["reason"] == "missed: engine did not run in time"


def _known_at(series: list[dict[str, Any]], t: int, bar_ms: int) -> list[dict[str, Any]]:
    """Funding events paid at or before ``t``; bars closed at or before ``t`` (bar ``ts`` is its open)."""
    return [e for e in series if ("ts_ms" in e and e["ts_ms"] <= t) or ("ts" in e and e["ts"] + bar_ms <= t)]


class TruncatedFeed(FixtureFeed):
    """Serves only what was knowable at the decision time: the source cut to data <= t on every series."""

    async def load(self, params: CarryParams, decision_ts: int, open_positions: list[dict[str, Any]]) -> FeedData:
        data = await super().load(params, decision_ts, open_positions)
        t, bm = decision_ts, params.bar_ms
        if self.strategy == "A":
            data.markets = {s: {k: _known_at(v, t, bm) for k, v in d.items()} for s, d in data.markets.items()}
        else:
            data.markets = {
                s: {leg: {k: _known_at(v, t, bm) for k, v in d[leg].items()} for leg in ("a", "b")} for s, d in data.markets.items()
            }
        return data


async def _snapshot(store: Store, strategy: str) -> dict[str, Any]:
    logs = {
        k: [(x["cycle_ts"], x["trade_id"], x["payload"]) for x in await store.cp_logs(strategy, k, limit=None)]
        for k in ("side", "funding", "mark", "close", "event")
    }
    return {
        "cycles": [(c["cycle_ts"], c["status"], c["payload"]) for c in await store.cp_cycles(strategy, 10_000)],
        "positions": [(p["trade_id"], p["status"], p["state"], p["trade"]) for p in await store.cp_positions(strategy)],
        "logs": logs,
    }


@pytest.mark.parametrize("strategy", ["A", "B"])
async def test_no_lookahead_truncated_source_gives_the_same_cycles(strategy):
    # The engine run on a source cut to data <= decision time must record exactly what it records on the full source:
    # same cycles (ranking, candidates, decisions, flags), positions, funding, marks and closes.
    if strategy == "A":
        mk = markets_a(days=110)
        params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
        times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), max(e["ts_ms"] for d in mk.values() for e in d["funding"]))
    else:
        mk = markets_b(days=110)
        params = replace(PARAMS_B, slots=SLOTS, entry_apr=0.10, stop_move=0.2)
        firsts = [max(d["a"]["funding"][0]["ts_ms"], d["b"]["funding"][0]["ts_ms"]) for d in mk.values()]
        end = max(min(d["a"]["funding"][-1]["ts_ms"], d["b"]["funding"][-1]["ts_ms"]) for d in mk.values())
        times = _grid(min(firsts), end)
    snaps = []
    for feed in (FixtureFeed(mk, strategy), TruncatedFeed(mk, strategy)):
        s = Store(":memory:")
        await s.init()
        try:
            await _drive(s, feed, params, times)
            snaps.append(await _snapshot(s, strategy))
        finally:
            await s.aclose()
    full, cut = snaps
    assert any(p[1] == "closed" for p in full["positions"])  # the segment is not vacuous
    if strategy == "B":
        assert any(p[3] and p[3]["stopped"] for p in full["positions"])  # stops read bar extremes: covered too
    assert cut == full


async def test_exchange_interval_change_is_an_event(store):
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)
    # run until a position is held, then change the venue's reported interval
    feed = FixtureFeed(mk, "A")
    i = 0
    while not await store.cp_positions("A", "open"):
        await _drive(store, feed, params, [times[i]])
        i += 1
    sym = (await store.cp_positions("A", "open"))[0]["symbol"]

    class Changed(FixtureFeed):
        async def load(self, params, decision_ts, open_positions):
            d = await super().load(params, decision_ts, open_positions)
            d.current = {sym: {"interval_hours": 8.0}}
            return d

    await _drive(store, Changed(mk, "A"), params, [times[i]])

    class Changed4(Changed):
        async def load(self, params, decision_ts, open_positions):
            d = await FixtureFeed.load(self, params, decision_ts, open_positions)
            d.current = {sym: {"interval_hours": 4.0}}
            return d

    await _drive(store, Changed4(mk, "A"), params, [times[i + 1]])
    ev = [e["payload"] for e in await store.cp_logs("A", "event") if e["payload"]["type"] == "funding_interval_change"]
    assert ev and ev[0]["old"] == 8.0 and ev[0]["new"] == 4.0


async def test_interval_change_across_a_skipped_cycle_is_an_event(store):
    # Regression: the previous intervals came from the last cycle of any status; a skipped cycle has an empty payload,
    # so a change across a skip was never logged. They now come from the last cycle with status 'done'.
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)
    feed = FixtureFeed(mk, "A")
    i = 0
    while not await store.cp_positions("A", "open"):
        await _drive(store, feed, params, [times[i]])
        i += 1
    sym = (await store.cp_positions("A", "open"))[0]["symbol"]

    def reporting(hours: float) -> FixtureFeed:
        class Reported(FixtureFeed):
            async def load(self, params, decision_ts, open_positions):
                d = await super().load(params, decision_ts, open_positions)
                d.current = {sym: {"interval_hours": hours}}
                return d

        return Reported(mk, "A")

    await _drive(store, reporting(8.0), params, [times[i]])
    skip = {"strategy": "A", "cycle_ts": times[i + 1] - DECISION_DELAY_MS, "status": "skipped", "reason": "missed", "payload": {}}
    assert await store.cp_cycle_commit(skip, [], [])
    assert (await store.cp_cycles("A", 1))[0]["status"] == "skipped"
    assert (await store.cp_cycles("A", 1, status="done"))[0]["cycle_ts"] == times[i] - DECISION_DELAY_MS
    await _drive(store, reporting(4.0), params, [times[i + 2]])
    ev = [e for e in await store.cp_logs("A", "event") if e["payload"]["type"] == "funding_interval_change"]
    assert [(e["cycle_ts"], e["payload"]["old"], e["payload"]["new"]) for e in ev] == [(times[i + 2] - DECISION_DELAY_MS, 8.0, 4.0)]


class DroppingFeed(FixtureFeed):
    """Serves the series without the payments scheduled at the given cycle (late publication / a hole in the history)."""

    def __init__(self, markets: dict[str, Any], strategy: str, drop_cycle_ts: int | None = None) -> None:
        super().__init__(markets, strategy)
        self.drop_cycle_ts = drop_cycle_ts
        self.dropped: set[int] = set()

    async def load(self, params: CarryParams, decision_ts: int, open_positions: list[dict[str, Any]]) -> FeedData:
        data = await super().load(params, decision_ts, open_positions)
        if self.drop_cycle_ts != decision_ts - DECISION_DELAY_MS:
            return data
        lo = self.drop_cycle_ts - 60_000
        keep = {}
        for sym, d in data.markets.items():
            self.dropped |= {e["ts_ms"] for e in d["funding"] if e["ts_ms"] >= lo}
            keep[sym] = {**d, "funding": [e for e in d["funding"] if e["ts_ms"] < lo]}
        data.markets = keep
        return data


async def test_missed_funding_payment_holds_the_position_and_is_settled_next_cycle(store):
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)
    feed = FixtureFeed(mk, "A")
    i = 0
    while not await store.cp_positions("A", "open"):
        await _drive(store, feed, params, [times[i]])
        i += 1
    held = {r["symbol"]: r for r in await store.cp_positions("A", "open")}
    bad_cycle = times[i] - DECISION_DELAY_MS
    drop = DroppingFeed(mk, "A", drop_cycle_ts=bad_cycle)
    await _drive(store, drop, params, [times[i]])
    cyc = await store.cp_cycle_get("A", bad_cycle)
    flagged = {f["symbol"] for f in cyc["payload"]["flags"] if f["issue"] == "funding_payment_missing"}
    assert flagged == set(held) and cyc["payload"]["complete"] is False
    events = [e["payload"] for e in await store.cp_logs("A", "event") if e["payload"]["type"] == "funding_payment_missing"]
    assert {e["symbol"] for e in events} == set(held)
    for r in await store.cp_positions("A", "open"):
        if r["symbol"] in held:
            assert r["state"]["last"] == held[r["symbol"]]["state"]["last"]  # bookkeeping did not advance past the gap
            assert r["state"]["funding"] == held[r["symbol"]]["state"]["funding"]
    assert all(r["status"] == "open" for r in await store.cp_positions("A") if r["symbol"] in held and r["opened_ts"] <= bad_cycle)

    await _drive(store, drop, params, [times[i + 1]])  # the payment is in the history again
    sym = next(iter(held))
    paid = {x["payload"]["payment_ts"] for x in await store.cp_logs("A", "funding", trade_id=held[sym]["trade_id"])}
    assert paid & drop.dropped  # the late payment was accrued once the history had it


class PermanentHoleFeed(FixtureFeed):
    """One payment (``hole_ts``) never appears in the history, on any cycle: the venue will not publish it."""

    def __init__(self, markets: dict[str, Any], strategy: str, hole_ts: int) -> None:
        super().__init__(markets, strategy)
        self.hole_ts = hole_ts

    async def load(self, params: CarryParams, decision_ts: int, open_positions: list[dict[str, Any]]) -> FeedData:
        data = await super().load(params, decision_ts, open_positions)
        for sym, d in list(data.markets.items()):
            data.markets[sym] = {**d, "funding": [e for e in d["funding"] if abs(e["ts_ms"] - self.hole_ts) > 60_000]}
        return data


async def test_persistent_funding_gap_is_confirmed_after_three_cycles_and_settles(store):
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)
    feed = FixtureFeed(mk, "A")
    i = 0
    while not await store.cp_positions("A", "open"):
        await _drive(store, feed, params, [times[i]])
        i += 1
    held = {r["symbol"]: r for r in await store.cp_positions("A", "open")}
    hole = PermanentHoleFeed(mk, "A", hole_ts=times[i] - DECISION_DELAY_MS)
    await _drive(store, hole, params, [times[i]])
    gaps = {r["symbol"]: r["state"].get("gap_cycles") for r in await store.cp_positions("A", "open") if r["symbol"] in held}
    assert set(gaps.values()) == {1}
    await _drive(store, hole, params, [times[i + 1]])
    assert {r["state"].get("gap_cycles") for r in await store.cp_positions("A", "open") if r["symbol"] in held} == {2}
    # the position stays held for exactly GAP_CONFIRM_CYCLES - 1 cycles, then settles
    await _drive(store, hole, params, [times[i + 2]])
    events = [e["payload"] for e in await store.cp_logs("A", "event") if e["payload"]["type"] == "funding_gap_confirmed"]
    assert {e["symbol"] for e in events} == set(held)
    assert all(e["missing_payments"] == 1 and e["cycles"] == 3 for e in events)
    cyc = await store.cp_cycle_get("A", times[i + 2] - DECISION_DELAY_MS)
    assert {f["symbol"] for f in cyc["payload"]["flags"] if f["issue"] == "funding_gap_confirmed"} == set(held)
    now_open = {r["symbol"]: r for r in await store.cp_positions("A", "open")}
    closed = {r["symbol"]: r for r in await store.cp_positions("A", "closed")}
    for sym, was in held.items():
        if sym in now_open:
            assert "gap_cycles" not in now_open[sym]["state"]
            assert now_open[sym]["state"]["last"] > was["state"]["last"]  # settled in full, not hanging
        else:
            assert closed[sym]["trade"]["funding_gap_confirmed"] == 1


class IncompleteHoleFeed(PermanentHoleFeed):
    """Like PermanentHoleFeed, but our own fetch is flagged incomplete (page limit, 429/418)."""

    async def load(self, params: CarryParams, decision_ts: int, open_positions: list[dict[str, Any]]) -> FeedData:
        data = await super().load(params, decision_ts, open_positions)
        data.flags += [{"symbol": s, "issue": "funding_history_incomplete"} for s in data.markets]
        return data


async def test_incomplete_fetch_holds_without_counting_towards_gap_confirmation(store):
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)
    feed = FixtureFeed(mk, "A")
    i = 0
    while not await store.cp_positions("A", "open"):
        await _drive(store, feed, params, [times[i]])
        i += 1
    held = {r["symbol"]: r for r in await store.cp_positions("A", "open")}
    bad = IncompleteHoleFeed(mk, "A", hole_ts=times[i] - DECISION_DELAY_MS)
    for n in range(4):
        await _drive(store, bad, params, [times[i + n]])
        for r in await store.cp_positions("A", "open"):
            if r["symbol"] in held:
                assert not r["state"].get("gap_cycles")
                assert r["state"]["last"] == held[r["symbol"]]["state"]["last"]  # held, never settled
    assert not [e for e in await store.cp_logs("A", "event") if e["payload"]["type"] == "funding_gap_confirmed"]


async def test_recovered_gap_clears_the_gap_counter(store):
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)
    feed = FixtureFeed(mk, "A")
    i = 0
    while not await store.cp_positions("A", "open"):
        await _drive(store, feed, params, [times[i]])
        i += 1
    held = {r["symbol"] for r in await store.cp_positions("A", "open")}
    await _drive(store, DroppingFeed(mk, "A", drop_cycle_ts=times[i] - DECISION_DELAY_MS), params, [times[i]])
    await _drive(store, feed, params, [times[i + 1]])  # the payment is back
    for r in await store.cp_positions("A", "open"):
        if r["symbol"] in held:
            assert "gap_cycles" not in r["state"]


class MidWindowHoleFeed(FixtureFeed):
    """Funding at ``interval_h`` spacing; at ``hole_cycle`` the payment ``gap_ms`` before it is missing (mid-window)."""

    def __init__(self, markets: dict[str, Any], interval_h: int, hole_cycle: int | None = None, report: bool = False) -> None:
        super().__init__(markets, "A")
        self.step, self.hole_cycle, self.report = interval_h * 3_600_000, hole_cycle, report

    async def load(self, params: CarryParams, decision_ts: int, open_positions: list[dict[str, Any]]) -> FeedData:
        data = await super().load(params, decision_ts, open_positions)
        cycle = decision_ts - DECISION_DELAY_MS
        src, data.markets = data.markets, {}  # never mutate the shared fixture
        for sym, d in src.items():
            f = d["funding"]
            if self.step == H4:  # derive a 4h series from the 8h one: same total funding
                f = [x for e in f for x in ({**e, "rate": e["rate"] / 2}, {"ts_ms": e["ts_ms"] + H4, "rate": e["rate"] / 2})]
            if cycle == self.hole_cycle:
                target = cycle - self.step if self.step == H8 else cycle - H4
                f = [e for e in f if abs(e["ts_ms"] - target) > 60_000]
            data.markets[sym] = {**d, "funding": f}
        if self.report:
            data.current = {s: {"interval_hours": self.step / 3_600_000} for s in data.markets}
        return data


@pytest.mark.parametrize(("interval_h", "skip_before", "report"), [(4, False, False), (8, True, False), (4, False, True)])
async def test_mid_window_payment_hole_holds_the_position(store, interval_h, skip_before, report):
    from tradedesk.collector.carry_paper_engine import _interval_hours  # noqa: F401  (the helper under scrutiny stays importable)

    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)
    ok = MidWindowHoleFeed(mk, interval_h, report=report)
    i = 0
    while not await store.cp_positions("A", "open"):
        await _drive(store, ok, params, [times[i]])
        i += 1
    held = {r["symbol"]: r["state"]["last"] for r in await store.cp_positions("A", "open")}
    j = i + 1 if skip_before else i  # 8h: a missed cycle widens the window to two payments; 4h: the window already has two
    bad = times[j] - DECISION_DELAY_MS
    await _drive(store, MidWindowHoleFeed(mk, interval_h, hole_cycle=bad, report=report), params, [times[j]])
    cyc = await store.cp_cycle_get("A", bad)
    flagged = {f["symbol"]: f["issue"] for f in cyc["payload"]["flags"]}
    assert flagged and cyc["payload"]["complete"] is False
    for r in await store.cp_positions("A", "open"):
        if r["symbol"] in flagged:
            assert r["state"]["last"] == held[r["symbol"]]  # not advanced past the hole

    # the hole fills: both payments accrue and the total matches an uninterrupted run
    await _drive(store, ok, params, [times[j + 1]])
    twin = Store(":memory:")
    await twin.init()
    try:
        k = 0
        while k <= j + 1:
            await _drive(twin, ok, params, [times[k]])
            k += 1
        sym = next(iter(flagged))
        a = await store.cp_positions("A")
        b = await twin.cp_positions("A")
        fa = {(r["symbol"], r["opened_ts"]): r["state"]["funding"] for r in a}
        fb = {(r["symbol"], r["opened_ts"]): r["state"]["funding"] for r in b}
        assert sym and fa == pytest.approx(fb)
    finally:
        await twin.aclose()


async def test_cycle_on_time_uses_the_finish_time(store):
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    cycle_ts = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), 10**15)[0] - DECISION_DELAY_MS
    ticks = iter([cycle_ts + DECISION_DELAY_MS, cycle_ts + 31 * 60_000])  # starts inside the 30 min window, finishes outside
    out = await run_cycle(store, FixtureFeed(mk, "A"), params, cycle_ts, clock=lambda: next(ticks))
    assert out["status"] == "done"
    p = (await store.cp_cycle_get("A", cycle_ts))["payload"]
    assert p["late_ms"] == DECISION_DELAY_MS and p["finish_late_ms"] == 31 * 60_000 and p["on_time"] is False


def test_cost_realism_reports_thin_book_sides_separately():
    from tradedesk.analytics.carry_report import cost_realism

    side = lambda a, m, short=False: {"actual_cost": a, "model_cost": m, "depth_short": short, "spread_bps": 2.0}  # noqa: E731
    out = cost_realism([side(0.002, 0.001), side(0.004, 0.001), side(0.0001, 0.001, True), {"actual_cost": None}])
    assert out["sides"] == 4 and out["with_book"] == 3 and out["n_full_depth"] == 2
    assert out["ratio"] == pytest.approx(3.0) and out["ratio_p90"] == pytest.approx(4.0)
    assert out["thin_book"]["n"] == 1 and out["thin_book"]["ratio"] == pytest.approx(0.1)  # not mixed into the main ratio
    assert out["share_depth_short"] == pytest.approx(1 / 3)


def test_funding_gap_issue():
    from tradedesk.analytics.carry_rules import funding_gap_issue

    base = 1_700_000_000_000 // H8 * H8
    pays = [base + k * H8 + 3 for k in range(4)]
    assert (
        funding_gap_issue(pays[:3], pays[0] + DECISION_DELAY_MS, pays[3] + DECISION_DELAY_MS, pays[3], 8.0) == "scheduled_payment_missing"
    )
    assert funding_gap_issue(pays, pays[0] + DECISION_DELAY_MS, pays[1] + DECISION_DELAY_MS, pays[1], 8.0) is None
    hole = [pays[0], pays[3]]
    assert funding_gap_issue(hole, pays[0] + DECISION_DELAY_MS, pays[3] + DECISION_DELAY_MS, pays[3], 8.0) == "payment_gap"


# Backtest output on seeded synthetic series, hashed at commit 82cb78d (before carry_rules.py was extracted from
# carry.py; the refactored code was compared with the old code on these inputs, byte-for-byte equal).
BACKTEST_PINS = [
    ("A", 1, {"slots": 3, "entry_apr": 0.10}, "9896eb6fe5638e71"),
    ("A", 5, {"slots": 3, "entry_apr": 0.10}, "d0d2a3560a076f5c"),
    ("B", 2, {"slots": 3, "entry_apr": 0.05, "stop_move": 0.5, "max_entry_gap": 0.02}, "c5e2dcd5a515b1e0"),
    ("B", 2, {"slots": 3, "entry_apr": 0.05}, "b728f0758d202017"),
    ("B", 7, {"slots": 3, "entry_apr": 0.05, "stop_move": 0.5, "max_entry_gap": 0.02}, "c9cdcf79026eb712"),
]


@pytest.mark.parametrize(("kind", "seed", "kw", "digest"), BACKTEST_PINS)
def test_backtest_output_is_pinned(kind, seed, kw, digest):
    import hashlib
    import json

    if kind == "A":
        out = simulate_carry_portfolio(markets_a(seed=seed, days=110), **kw)
    else:
        out = simulate_perp_spread_portfolio(markets_b(seed=seed, days=110), **kw)
    got = hashlib.sha256(json.dumps(_canon(out), sort_keys=True, default=str).encode()).hexdigest()[:16]
    assert got == digest and out["trades"]


def _canon(o):
    """Floats rounded to 10 significant digits before hashing: Python 3.12's sum() of floats is compensated
    (Neumaier), so the last bits differ from 3.11 while the backtest itself is the same."""
    if isinstance(o, float):
        return float(f"{o:.10g}")
    if isinstance(o, dict):
        return {k: _canon(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_canon(v) for v in o]
    return o


def test_leg_execution_vwap_cost_and_depth_shortfall():
    book = {"bids": [[99.0, 10.0]], "asks": [[101.0, 5.0], [102.0, 100.0]], "timestamp": 1_000_250}
    ex = leg_execution(book, "buy", 1000.0, fee=0.0005, model_slippage=0.0005, cycle_ts=1_000_000)
    mid = 100.0
    assert ex["mid"] == mid and ex["spread_bps"] == pytest.approx(200.0)
    # 505 USD at 101, 495 USD at 102 -> qty 5 + 4.853..., vwap = 1000 / qty
    qty = 5.0 + 495.0 / 102.0
    assert ex["vwap"] == pytest.approx(1000.0 / qty)
    assert ex["actual_cost"] == pytest.approx(0.0005 + (1000.0 / qty - mid) / mid)
    assert ex["model_cost"] == pytest.approx(0.001) and not ex["depth_short"] and ex["book_latency_ms"] == 250
    thin = leg_execution({"bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]]}, "sell", 500.0, fee=0.0005, model_slippage=0.0005, cycle_ts=0)
    assert thin["depth_short"] and thin["unfilled_usd"] == pytest.approx(500.0 - 99.0)
    assert leg_execution(None, "buy", 1000.0, fee=0.0005, model_slippage=0.0005, cycle_ts=0)["book_available"] is False


def test_cycle_grid_waits_for_the_decision_delay():
    t = 1_700_000_000_000 // H8 * H8
    assert cycle_grid(t + DECISION_DELAY_MS) == t
    assert cycle_grid(t + DECISION_DELAY_MS - 1) == t - H8
    assert cycle_grid(t + 2 * H8 - 1) == t + H8


def test_registered_variants():
    assert (PARAMS_A.slots, PARAMS_A.entry_apr, PARAMS_A.exit_apr, PARAMS_A.stop_move, PARAMS_A.slot_notional) == (
        5,
        0.20,
        0.0,
        None,
        2000.0,
    )
    assert (PARAMS_B.slots, PARAMS_B.entry_apr, PARAMS_B.max_entry_gap, PARAMS_B.stop_move, PARAMS_B.stop_penalty) == (
        5,
        0.10,
        0.02,
        0.5,
        0.01,
    )
    assert PARAMS_A.tag == "cc-paper" and PARAMS_B.tag == "pp-paper"


def test_engine_code_has_no_order_calls():
    root = Path(__file__).parent.parent / "src" / "tradedesk"
    files = [
        root / "collector" / "carry_paper_engine.py",
        root / "collector" / "carry_paper_feed.py",
        root / "collector" / "market_data.py",
        root / "analytics" / "carry_rules.py",
        root / "tools" / "paper_carry.py",
        Path(__file__).parent.parent / "scripts" / "paper_carry_once.py",
    ]
    banned = re.compile(r"create_order|place_order|cancel_order|edit_order|set_leverage|withdraw|fetch_balance|api_?key|secret", re.I)
    for f in files:
        assert not banned.search(f.read_text()), f.name
