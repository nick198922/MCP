"""Strong traders' public accounts: Hyperliquid leaderboard and positions, Polymarket leaderboard and positions,
the daily snapshot and position-change jobs (fixtures: real responses of 2026-10-15 with made-up addresses)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conftest import NOW, FakeEx, make_transports
from tradedesk.collector.scheduler import _collect_trader_positions, _collect_traders, build_default_scheduler
from tradedesk.config import Settings
from tradedesk.providers.hyperliquid import parse_leaderboard, parse_positions
from tradedesk.runtime import Runtime

FIX = Path(__file__).parent / "fixtures"
HL_BOARD = json.loads((FIX / "hyperliquid_leaderboard_sample_2026-10-15.json").read_text())
HL_STATE = json.loads((FIX / "hyperliquid_clearinghouse_state_2026-10-15.json").read_text())
PM_BOARD = json.loads((FIX / "polymarket_leaderboard_sample_2026-10-15.json").read_text())
PM_POS = json.loads((FIX / "polymarket_positions_sample_2026-10-15.json").read_text())


def test_parse_leaderboard_filters_and_reads_windows():
    rows = parse_leaderboard(HL_BOARD, 500_000)
    assert [r["account"] for r in rows] == ["0x" + "0" * 39 + "1", "0x" + "0" * 39 + "2"]  # the small account is dropped
    r = rows[0]
    assert r["name"] == "whale-one" and r["account_value"] > 500_000
    assert {"pnl_day", "pnl_week", "pnl_month", "pnl_all", "roi_month", "vlm_month"} <= set(r)
    assert len(parse_leaderboard(HL_BOARD)) == 3


def test_parse_positions_sides_and_units():
    pos = parse_positions(HL_STATE)
    assert [(p["market"], p["side"]) for p in pos] == [("BTC", "long"), ("ETH", "short")]
    assert all(p["size"] > 0 and p["value_usd"] > 0 for p in pos)


def _runtime(state, *, hl_fail=False, pm_fail=False, hl_positions=None, settings=None) -> Runtime:
    calls: list[str] = []

    def stats(req: httpx.Request) -> httpx.Response:
        calls.append("stats")
        return httpx.Response(503, text="down") if hl_fail else httpx.Response(200, json=HL_BOARD)

    base = make_transports(state)
    inner = base["hyperliquid"]

    def hl(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("type") == "clearinghouseState":
            calls.append(f"state:{body['user']}")
            return httpx.Response(200, json=hl_positions if hl_positions is not None else HL_STATE)
        return inner.handler(req)

    def pm(req: httpx.Request) -> httpx.Response:
        if pm_fail:
            return httpx.Response(500, text="down")
        if req.url.path == "/v1/leaderboard":
            calls.append(f"pm:{req.url.params['timePeriod']}:{req.url.params['offset']}")
            return httpx.Response(200, json=PM_BOARD if req.url.params["offset"] == "0" else [])
        if req.url.path == "/positions":
            calls.append("pm:positions")
            return httpx.Response(200, json=PM_POS)
        return httpx.Response(404)

    base["hyperliquid:stats"] = httpx.MockTransport(stats)
    base["hyperliquid"] = httpx.MockTransport(hl)
    base["polymarket:data"] = httpx.MockTransport(pm)
    s = settings or Settings(http_retries=1, cache_scale=0.0, db_path=":memory:", collector_enabled=True, traders_top_n=5)
    rt = Runtime(s, transports=base, exchange_factory=FakeEx)
    rt.now = lambda: NOW
    rt._calls = calls  # type: ignore[attr-defined]
    return rt


async def test_daily_snapshot_and_positions_written_only_on_change(state):
    rt = _runtime(state)
    try:
        await rt.store.init()
        assert await _collect_traders(rt) == 2 + 2 + 2  # HL (2 big accounts), Polymarket overall and crypto (2 each)
        assert await rt.store.table_count("trader_snapshot") == 6
        cur = await rt.store.conn.execute("SELECT account, rank_month FROM trader_snapshot WHERE venue = 'hyperliquid' ORDER BY rank_month")
        assert [r[1] for r in await cur.fetchall()] == [1, 2]
        first = await _collect_trader_positions(rt)
        assert first == 2 * 2 + 2 * 2  # 2 HL accounts x 2 positions; 2 Polymarket wallets x 2 open positions (resolved one dropped)
        assert await _collect_trader_positions(rt) == 0  # nothing changed
        cur = await rt.store.conn.execute("SELECT DISTINCT side FROM trader_positions WHERE venue = 'polymarket'")
        assert {r[0] for r in await cur.fetchall()} == {"Yes"}
    finally:
        await rt.aclose()


async def test_flat_account_and_one_venue_down(state):
    rt = _runtime(state, pm_fail=True, hl_positions={"assetPositions": [], "marginSummary": {}})
    try:
        await rt.store.init()
        assert await _collect_traders(rt) == 2  # Polymarket down: Hyperliquid still written
        assert await _collect_trader_positions(rt) == 2  # one 'flat' row per HL account
        cur = await rt.store.conn.execute("SELECT DISTINCT side, market FROM trader_positions")
        assert [tuple(r) for r in await cur.fetchall()] == [("flat", "")]
    finally:
        await rt.aclose()
    rt = _runtime(state, hl_fail=True, pm_fail=True)
    try:
        await rt.store.init()
        with pytest.raises(RuntimeError):
            await _collect_traders(rt)
        assert await _collect_trader_positions(rt) == 0  # no snapshot yet: nobody to watch
    finally:
        await rt.aclose()


def test_traders_jobs_are_scheduled(state):
    on = _runtime(state)
    off = _runtime(state, settings=Settings(db_path=":memory:", collector_enabled=True, collect_traders_enabled=False))
    assert {"traders", "trader_positions"} <= {j.name for j in build_default_scheduler(on).jobs}
    assert not {"traders", "trader_positions"} & {j.name for j in build_default_scheduler(off).jobs}
