"""LiveFeed (read-only ccxt providers) driving the paper engine, with a fake venue."""

from __future__ import annotations

import json

import ccxt
import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.carry_rules import DECISION_DELAY_MS, cycle_grid
from tradedesk.collector.carry_paper_engine import PARAMS_A, PARAMS_B, run_cycle
from tradedesk.collector.carry_paper_feed import LiveFeed, Pacer, universe_a, universe_b
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

NOW_MS = int(NOW.timestamp() * 1000)
CYCLE = cycle_grid(NOW_MS)
H8 = 8 * 3_600_000


def _perp(base: str, underlying: str = "COIN") -> dict:
    sym = f"{base}/USDT:USDT"
    return {
        "id": f"{base}USDT",
        "symbol": sym,
        "type": "swap",
        "swap": True,
        "active": True,
        "info": {"contractType": "PERPETUAL", "underlyingType": underlying},
    }


def _spot(base: str) -> dict:
    return {"id": f"{base}USDT", "symbol": f"{base}/USDT", "type": "spot", "active": True, "info": {}}


class LiveEx(FakeEx):
    async def load_markets(self):
        if self.id == "binance":
            return {f"{b}/USDT": _spot(b) for b in ("BTC", "WIF")}
        out = {f"{b}/USDT:USDT": _perp(b) for b in ("BTC", "WIF", "NOSPOT")}
        out["XAU/USDT:USDT"] = _perp("XAU", "COMMODITY")
        return out

    cycle = CYCLE
    history_calls: list[tuple[str, str, int | None]] = []

    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
        type(self).history_calls.append((self.id, symbol, since))
        until = (params or {}).get("until")
        hot = symbol.startswith("WIF") and self.id == "binanceusdm"
        rate = 0.0008 if hot else 0.0001
        rows, ts = [], (type(self).cycle // H8) * H8 + 3
        while ts >= (since or 0) and len(rows) < limit:
            if until is None or ts <= until:
                rows.append({"symbol": symbol, "fundingRate": rate, "timestamp": ts})
            ts -= H8
        return sorted(rows, key=lambda r: r["timestamp"])


def _rt(factory, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "live.sqlite")),
        transports=make_transports({}),
        exchange_factory=factory,
    )
    rt.now = lambda: NOW
    return rt


def test_universe_rules_match_the_backtest_scripts():
    perps = {f"{b}/USDT:USDT": _perp(b) for b in ("BTC", "1000PEPE", "USDC", "NOSPOT")}
    perps["XAU/USDT:USDT"] = _perp("XAU", "COMMODITY")
    perps["BTC/USDT:USDT-261225"] = {"info": {"contractType": "CURRENT_QUARTER", "underlyingType": "COIN"}}
    spots = {f"{b}/USDT": _spot(b) for b in ("BTC", "PEPE", "USDC")}
    uni, excluded = universe_a(perps, spots)
    assert uni == {"BTC/USDT:USDT": ("BTC/USDT", 1), "1000PEPE/USDT:USDT": ("PEPE/USDT", 1000)}
    assert {(x["symbol"], x["reason"]) for x in excluded} == {
        ("XAU/USDT:USDT", "tradfi"),
        ("USDC/USDT:USDT", "no_spot"),
        ("NOSPOT/USDT:USDT", "no_spot"),
    }
    b_uni, b_excl = universe_b(perps, {"BTC/USDT:USDT": {"swap": True}, "XAU/USDT:USDT": {"swap": True}})
    assert b_uni == ["BTC/USDT:USDT"] and {x["reason"] for x in b_excl} == {"tradfi", "no_second_venue"}


async def test_live_feed_runs_both_engines_end_to_end(tmp_path):
    rt = _rt(LiveEx, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    try:
        feed = LiveFeed(rt, workers=2)
        for params, tag in ((PARAMS_A, "cc-paper"), (PARAMS_B, "pp-paper")):
            out = await run_cycle(rt.store, feed, params, CYCLE, clock=lambda: CYCLE + DECISION_DELAY_MS)
            assert out["status"] == "done", out
            assert out["opened"] == ["WIF/USDT:USDT"], out
            (pos,) = await rt.store.cp_positions(params.strategy, "open")
            sides = await rt.store.cp_logs(params.strategy, "side", trade_id=pos["trade_id"])
            assert len(sides) == 2 and all(s["payload"]["book_available"] and s["payload"]["actual_cost"] is not None for s in sides)
            assert all(s["payload"]["book_latency_ms"] is None and s["payload"]["model_cost"] > 0 for s in sides)
            cyc = await rt.store.cp_cycle_get(params.strategy, CYCLE)
            assert cyc["payload"]["excluded_counts"].get("tradfi") == 1 and cyc["payload"]["candidates"][0]["symbol"] == "WIF/USDT:USDT"
            assert tag in params.tag
        (b_pos,) = await rt.store.cp_positions("B", "open")
        assert b_pos["state"]["dir"] == -1  # Binance (A) pays more: short A / long B
        a_sides = await rt.store.cp_logs("A", "side")
        assert [(s["payload"]["leg"], s["payload"]["side"], s["payload"]["venue"]) for s in a_sides] == [
            ("spot", "buy", "binance"),
            ("perp", "sell", "binanceusdm"),
        ]
    finally:
        await rt.aclose()


async def test_steady_state_cycle_requests_only_new_funding_rows(tmp_path):
    from collections import Counter

    class Ex(LiveEx):
        history_calls: list = []

    rt = _rt(Ex, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    pacer = Pacer(max_calls=400, window_s=300.0)
    try:
        feed = LiveFeed(rt, workers=2, pacer=pacer)
        for params in (PARAMS_A, PARAMS_B):
            assert (await run_cycle(rt.store, feed, params, CYCLE, clock=lambda: CYCLE + DECISION_DELAY_MS))["status"] == "done"
        first = [c for c in Ex.history_calls if c[0] == "binanceusdm"]
        assert max(Counter(s for _, s, _ in first).values()) == 1  # A and B share one Binance request per symbol
        assert pacer.total == len(first)

        Ex.cycle = CYCLE + H8
        Ex.history_calls.clear()
        before = pacer.total
        nxt = CYCLE + H8
        for params in (PARAMS_A, PARAMS_B):
            assert (await run_cycle(rt.store, feed, params, nxt, clock=lambda: nxt + DECISION_DELAY_MS))["status"] == "done"
        binance = [c for c in Ex.history_calls if c[0] == "binanceusdm"]
        assert binance and max(Counter(s for _, s, _ in binance).values()) == 1  # one request per symbol, A and B together
        assert pacer.total - before == len(binance)  # every Binance history request went through the pacer
        assert all(since >= CYCLE for _, _, since in Ex.history_calls)  # nothing older than the last stored payment is refetched
        assert len(Ex.history_calls) <= 3 * 2 + 3 * 2  # steady state: a handful of requests, not the 8-day history again
    finally:
        await rt.aclose()


async def test_pacer_waits_when_the_window_budget_is_spent():
    now = [0.0]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    p = Pacer(max_calls=3, window_s=10.0, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        await p.acquire()
    assert slept == []
    await p.acquire()  # the 4th call must wait until the 1st leaves the window
    assert slept and sum(slept) == pytest.approx(10.0, abs=0.05)
    p.note(5)  # extra pages already spent
    await p.acquire()
    assert len(slept) >= 2 and p.total == 3 + 1 + 5 + 1


async def test_funding_history_gap_in_live_feed_is_flagged_and_position_held(tmp_path):
    class Ex(LiveEx):
        history_calls: list = []

    rt = _rt(Ex, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    try:
        feed = LiveFeed(rt, workers=2, pacer=Pacer(10**6))
        assert (await run_cycle(rt.store, feed, PARAMS_A, CYCLE, clock=lambda: CYCLE + DECISION_DELAY_MS))["status"] == "done"
        Ex.cycle = CYCLE  # the venue has not published the next payment yet
        nxt = CYCLE + H8
        assert (await run_cycle(rt.store, feed, PARAMS_A, nxt, clock=lambda: nxt + DECISION_DELAY_MS))["status"] == "done"
        cyc = await rt.store.cp_cycle_get("A", nxt)
        assert [f["symbol"] for f in cyc["payload"]["flags"] if f["issue"] == "funding_payment_missing"] == ["WIF/USDT:USDT"]
        (pos,) = await rt.store.cp_positions("A", "open")
        assert pos["state"]["last"] == CYCLE + DECISION_DELAY_MS
        Ex.cycle = nxt  # published now: next cycle settles both payments
        nxt2 = CYCLE + 2 * H8
        Ex.cycle = nxt2
        assert (await run_cycle(rt.store, feed, PARAMS_A, nxt2, clock=lambda: nxt2 + DECISION_DELAY_MS))["status"] == "done"
        (pos,) = await rt.store.cp_positions("A", "open")
        assert pos["state"]["last"] == nxt2 + DECISION_DELAY_MS
        paid = await rt.store.cp_logs("A", "funding", trade_id=pos["trade_id"])
        assert len(paid) == 2  # both payments accrued, none lost
    finally:
        await rt.aclose()


async def test_held_symbol_window_is_reloaded_and_a_persistent_gap_is_confirmed(tmp_path):
    hole = CYCLE + H8 + 3  # this payment is never published

    class Ex(LiveEx):
        history_calls: list = []

        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            rows = await super().fetch_funding_rate_history(symbol, since, limit, params)
            return [r for r in rows if r["timestamp"] != hole]

    rt = _rt(Ex, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    try:
        feed = LiveFeed(rt, workers=2, pacer=Pacer(10**6))
        assert (await run_cycle(rt.store, feed, PARAMS_A, CYCLE, clock=lambda: CYCLE + DECISION_DELAY_MS))["status"] == "done"
        opened_last = CYCLE + DECISION_DELAY_MS
        for n in (1, 2, 3):
            cyc_ts = CYCLE + n * H8
            Ex.cycle = cyc_ts
            Ex.history_calls.clear()
            assert (await run_cycle(rt.store, feed, PARAMS_A, cyc_ts, clock=lambda c=cyc_ts: c + DECISION_DELAY_MS))["status"] == "done"
            wif = [s for v, sym, s in Ex.history_calls if v == "binanceusdm" and sym == "WIF/USDT:USDT"]
            if n == 1:
                assert wif and min(wif) > opened_last - H8  # first miss: ordinary incremental fetch
            if n == 2:
                assert any(s is not None and s <= opened_last - H8 + 1 for s in wif)  # held: window re-read behind `last`
        events = [e["payload"] for e in await rt.store.cp_logs("A", "event") if e["payload"]["type"] == "funding_gap_confirmed"]
        assert [e["symbol"] for e in events] == ["WIF/USDT:USDT"] and events[0]["missing_payments"] == 1
        (pos,) = await rt.store.cp_positions("A", "open")
        assert pos["state"]["last"] == CYCLE + 3 * H8 + DECISION_DELAY_MS and "gap_cycles" not in pos["state"]
    finally:
        await rt.aclose()


async def test_late_published_payment_behind_a_stored_later_one_is_reloaded_and_settled(tmp_path):
    hole = CYCLE + H8 + 3  # hidden on cycles 1 and 2 (a later payment is stored meanwhile), published on cycle 3

    class Ex(LiveEx):
        history_calls: list = []

        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            rows = await super().fetch_funding_rate_history(symbol, since, limit, params)
            if type(self).cycle < CYCLE + 3 * H8:
                rows = [r for r in rows if r["timestamp"] != hole]
            return rows

    rt = _rt(Ex, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    try:
        feed = LiveFeed(rt, workers=2, pacer=Pacer(10**6))
        assert (await run_cycle(rt.store, feed, PARAMS_A, CYCLE, clock=lambda: CYCLE + DECISION_DELAY_MS))["status"] == "done"
        for n in (1, 2, 3):
            cyc_ts = CYCLE + n * H8
            Ex.cycle = cyc_ts
            assert (await run_cycle(rt.store, feed, PARAMS_A, cyc_ts, clock=lambda c=cyc_ts: c + DECISION_DELAY_MS))["status"] == "done"
            (pos,) = await rt.store.cp_positions("A", "open")
            if n < 3:
                assert pos["state"]["gap_cycles"] == n  # held, nothing settled
        assert not [e for e in await rt.store.cp_logs("A", "event") if e["payload"]["type"] == "funding_gap_confirmed"]
        paid = {x["payload"]["payment_ts"] for x in await rt.store.cp_logs("A", "funding", trade_id=pos["trade_id"])}
        assert hole in paid and CYCLE + 2 * H8 + 3 in paid  # the late payment is in the P&L
        assert "gap_cycles" not in pos["state"] and pos["state"]["last"] == CYCLE + 3 * H8 + DECISION_DELAY_MS
    finally:
        await rt.aclose()


async def test_live_feed_provider_failure_is_logged_not_raised(tmp_path):
    class Down(LiveEx):
        async def load_markets(self):
            raise ccxt.NetworkError("venue down")

    rt = _rt(Down, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    try:
        out = await run_cycle(rt.store, LiveFeed(rt), PARAMS_A, CYCLE, clock=lambda: CYCLE + DECISION_DELAY_MS)
        assert out["status"] == "failed" and "market lists unavailable" in out["error"]
        assert await rt.store.cp_cycle_get("A", CYCLE) is None
        (ev,) = await rt.store.cp_logs("A", "event")
        assert ev["payload"]["type"] == "cycle_failed"
    finally:
        await rt.aclose()


async def test_missing_book_is_recorded_without_failing_the_cycle(tmp_path):
    class NoBook(LiveEx):
        async def fetch_order_book(self, symbol, limit=100):
            raise ccxt.NetworkError("book down")

    rt = _rt(NoBook, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    try:
        out = await run_cycle(rt.store, LiveFeed(rt), PARAMS_A, CYCLE, clock=lambda: CYCLE + DECISION_DELAY_MS)
        assert out["status"] == "done" and out["opened"] == ["WIF/USDT:USDT"]
        sides = await rt.store.cp_logs("A", "side")
        assert sides and all(s["payload"]["book_available"] is False for s in sides)
    finally:
        await rt.aclose()


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


async def test_paper_carry_status_tool_happy_path(tmp_path):
    rt = _rt(LiveEx, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    srv, _ = build_server(rt)
    try:
        empty = await _call(srv, "paper_carry_status")
        assert empty["ok"] is True and empty["data"]["A"]["engine"]["cycles_done"] == 0 and empty["data"]["A"]["open"] == []
        await rt.store.init()  # the client session's lifespan closed the shared store (invariant 7); reopen
        for params in (PARAMS_A, PARAMS_B):
            assert (await run_cycle(rt.store, LiveFeed(rt), params, CYCLE, clock=lambda: CYCLE + DECISION_DELAY_MS))["status"] == "done"
        res = await _call(srv, "paper_carry_status")
        assert res["ok"] is True, res
        a, b = res["data"]["A"], res["data"]["B"]
        assert a["variant"]["tag"] == "cc-paper" and b["variant"]["stop_move"] == 0.5
        assert a["engine"]["cycles_done"] == 1 and a["engine"]["last_cycle_on_time"] is True and a["free_slots"] == 4
        (pos,) = a["open"]
        assert pos["symbol"] == "WIF/USDT:USDT" and 0 < pos["held_days"] < 1 and "mark" not in pos  # no mark before the 2nd cycle
        assert a["cost_realism"]["sides"] == 2 and a["cost_realism"]["with_book"] == 2 and a["cost_realism"]["ratio"] is not None
        assert a["pnl_usd"]["portfolio_notional_usd"] == 10_000 and a["closed"] == {"n": 0}
        assert b["open"][0]["direction"].startswith("short A")
        assert any("descriptive only" in w and "+6.5 %" in w for w in res["warnings"])  # honest backtest result and limits
        one = await _call(srv, "paper_carry_status", strategy="B", include_closed=True)
        assert set(one["data"]) == {"B"} and one["data"]["B"]["closed_trades"] == []
    finally:
        await rt.aclose()


async def test_paper_carry_status_without_store_returns_an_envelope():
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=""), transports=make_transports({}), exchange_factory=LiveEx)
    srv, _ = build_server(rt)
    try:
        res = await _call(srv, "paper_carry_status")
        assert res["ok"] is False and res["missing"][0]["field"] == "store" and res["warnings"]
    finally:
        await rt.aclose()


async def test_paper_carry_job_is_opt_in(tmp_path):
    from tradedesk.collector import build_default_scheduler

    base = dict(http_retries=1, cache_scale=0.0, db_path=":memory:", collector_enabled=True)
    off = Runtime(Settings(**base), transports=make_transports({}), exchange_factory=LiveEx)
    on = Runtime(Settings(**base, paper_carry_enabled=True), transports=make_transports({}), exchange_factory=LiveEx)
    try:
        assert "paper_carry" not in [j.name for j in build_default_scheduler(off).jobs]
        sched = build_default_scheduler(on)
        job = next(j for j in sched.jobs if j.name == "paper_carry")
        assert job.period_seconds == 600
    finally:
        await off.aclose()
        await on.aclose()


@pytest.mark.parametrize("strategy", ["A", "B"])
def test_params_hash_is_stable(strategy):
    from tradedesk.collector.carry_paper_engine import PARAMS

    assert PARAMS[strategy].hash() == PARAMS[strategy].hash() and len(PARAMS[strategy].hash()) == 12
