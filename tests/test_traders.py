"""Strong traders' public accounts: Hyperliquid leaderboard and positions, Polymarket leaderboard and positions,
the daily snapshot and position-change jobs (fixtures: real responses of 2026-10-15, Polymarket data-api v2 of
2026-11-05, with made-up addresses)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from conftest import NOW, FakeEx, make_transports
from tradedesk.collector.scheduler import (
    TRADER_POSITIONS_REWRITE_USD,
    _collect_trader_positions,
    _collect_traders,
    build_default_scheduler,
)
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.providers.hyperliquid import parse_leaderboard, parse_positions
from tradedesk.runtime import Runtime

FIX = Path(__file__).parent / "fixtures"
HL_BOARD = json.loads((FIX / "hyperliquid_leaderboard_sample_2026-10-15.json").read_text())
HL_STATE = json.loads((FIX / "hyperliquid_clearinghouse_state_2026-10-15.json").read_text())
PM_V2 = json.loads((FIX / "polymarket_data_v2_2026-11-05.json").read_text())
PM_BOARD = PM_V2["leaderboard"]  # first page: 2 rows and a real next_cursor
PM_POS = PM_V2["positions"]  # 2 open >= $100, 1 open < $100, 1 REDEEMABLE
PM_V1_BOARD = json.loads((FIX / "polymarket_leaderboard_sample_2026-10-15.json").read_text())  # retired v1 shape
PM_LAST_PAGE = {"data": [], "pagination": {"limit": 50, "offset": 50, "has_more": False, "next_cursor": None}}


def test_parse_leaderboard_filters_and_reads_windows():
    rows = parse_leaderboard(HL_BOARD, 500_000)
    assert [r["account"] for r in rows] == ["0x" + "0" * 39 + "1", "0x" + "0" * 39 + "2"]  # the small account is dropped
    r = rows[0]
    assert r["name"] == "whale-one" and r["account_value"] > 500_000
    assert {"pnl_day", "pnl_week", "pnl_month", "pnl_all", "roi_month", "vlm_month"} <= set(r)
    assert len(parse_leaderboard(HL_BOARD)) == 3


def test_parse_leaderboard_keeps_listed_small_accounts():
    small = "0x" + "0" * 39 + "3"
    rows = parse_leaderboard(HL_BOARD, 500_000, keep={small})
    assert [r["account"] for r in rows][-1] == small and len(rows) == 3
    assert rows[-1]["account_value"] == pytest.approx(0.000008)


@pytest.mark.parametrize(
    ("seed_day", "seed_value", "kept"),
    [
        ("2026-08-11", 600_000.0, True),  # NOW - 60 days: still inside the window
        ("2026-08-10", 600_000.0, False),  # NOW - 61 days: dropped
        ("2026-10-09", 100_000.0, False),  # stored recently but already below the floor: no endless chaining
    ],
)
async def test_hyperliquid_snapshot_keeps_accounts_that_left_the_universe(state, seed_day, seed_value, kept):
    """Survivorship (smart-money report 2026-10-17): an account worth >= $500k on a stored day of the last 60 days is
    still written after its value fell (the fixture's third account holds $0.000008); older or never-qualified ones are not."""
    small = "0x" + "0" * 39 + "3"
    rt = _runtime(state, pm_fail=True)
    try:
        await rt.store.init()
        await rt.store.insert_trader_snapshot(seed_day, 0, "hyperliquid", [{"account": small, "account_value": seed_value}])
        assert await _collect_traders(rt) == (3 if kept else 2)
        cur = await rt.store.conn.execute("SELECT account_value FROM trader_snapshot WHERE day = '2026-10-10' AND account = ?", (small,))
        got = await cur.fetchone()
        assert (got is not None) is kept
        assert small not in await rt.store.tracked_traders("hyperliquid", 5, rt.settings.traders_min_account_usd)  # not watched
    finally:
        await rt.aclose()


async def test_tracked_traders_skips_a_kept_account_below_the_floor_even_with_the_top_pnl(state):
    """Review 2026-11-06-traders-survivorship, remark 3: a kept account with the highest month and all-time PnL of the
    latest day but a value below the floor is not watched; with the floor at 0 the same row heads both rankings, so the
    floor (not the PnL) is what keeps it out."""
    small = "0x" + "0" * 39 + "3"
    rt = _runtime(state, pm_fail=True)
    try:
        await rt.store.init()
        await rt.store.insert_trader_snapshot("2026-08-11", 0, "hyperliquid", [{"account": small, "account_value": 600_000.0}])
        assert await _collect_traders(rt) == 3  # written on 2026-10-10 as a kept account
        top = {"account": small, "account_value": 100_000.0, "pnl_month": 1e12, "pnl_all": 1e12}
        await rt.store.insert_trader_snapshot("2026-10-10", 0, "hyperliquid", [top])
        floor = rt.settings.traders_min_account_usd
        watched = await rt.store.tracked_traders("hyperliquid", 5, floor)
        assert small not in watched and len(watched) == 2  # the two accounts above the floor
        assert (await rt.store.tracked_traders("hyperliquid", 1, 0.0)) == [small]
    finally:
        await rt.aclose()


def test_parse_positions_sides_and_units():
    pos = parse_positions(HL_STATE)
    assert [(p["market"], p["side"]) for p in pos] == [("BTC", "long"), ("ETH", "short")]
    assert all(p["size"] > 0 and p["value_usd"] > 0 for p in pos)


def _runtime(
    state, *, hl_fail=False, pm_fail=False, pm_status=500, pm_body=None, pm_routes=None, hl_positions=None, settings=None
) -> Runtime:
    """`pm_routes`: path -> handler overriding a Polymarket data-api route; the dict is read per request, so a test
    can change a route after a baseline run."""
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
            return httpx.Response(pm_status, text="down")
        if pm_body is not None:
            return httpx.Response(200, json=pm_body)
        if "offset" in req.url.params:  # like the live v2: offset is rejected, pages are cursor-only
            return httpx.Response(400, json={"error": "'offset' is not a query param on this API"})
        if pm_routes and req.url.path in pm_routes:
            return pm_routes[req.url.path](req)
        if req.url.path == "/v2/leaderboard":
            p = req.url.params
            calls.append(f"pm:{p['category']}:{p['time_period']}:{'next' if 'cursor' in p else 'first'}")
            if "cursor" not in p:
                return httpx.Response(200, json=PM_BOARD)
            if p["cursor"] != PM_BOARD["pagination"]["next_cursor"]:
                return httpx.Response(400, json={"error": "bad cursor"})
            return httpx.Response(200, json=PM_LAST_PAGE)
        if req.url.path == "/v2/positions":
            calls.append(f"pm:positions:{req.url.params['filter_type']}:{req.url.params['filter_amount']}")
            return httpx.Response(200, json=PM_POS)
        return httpx.Response(404)

    base["hyperliquid:stats"] = httpx.MockTransport(stats)
    base["hyperliquid"] = httpx.MockTransport(hl)
    base["polymarket:data"] = httpx.MockTransport(pm)
    s = settings or Settings(
        http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=":memory:", collector_enabled=True, traders_top_n=5
    )
    rt = Runtime(s, transports=base, exchange_factory=FakeEx)
    for h in (rt.hyperliquid.stats, rt.polymarket.data):  # their 1 and 3 req/s production limits only make the test wait
        h._limiter = TokenBucket(1_000.0, 1_000)
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
        assert first == 2 * 2 + 2 * 2  # 2 HL accounts x 2 positions; 2 Polymarket wallets x 2 open positions >= $100
        # nothing changed: only the HL positions >= $2.5M (both fixture ones, $5.8M and $40M) are re-written
        rt.now = lambda: NOW + timedelta(hours=1)
        assert await _collect_trader_positions(rt) == 2 * 2
        cur = await rt.store.conn.execute("SELECT venue, COUNT(*) FROM trader_positions GROUP BY venue ORDER BY venue")
        assert [tuple(r) for r in await cur.fetchall()] == [("hyperliquid", 8), ("polymarket", 4)]
        cur = await rt.store.conn.execute("SELECT DISTINCT side FROM trader_positions WHERE venue = 'polymarket'")
        assert {r[0] for r in await cur.fetchall()} == {"No", "Grizzlies"}  # the REDEEMABLE and the < $100 rows dropped
    finally:
        await rt.aclose()


async def test_polymarket_v2_fields_and_cursor_walk(state):
    """data-api v2 (v1 answers HTTP 410 since 2026-11-02): rows are mapped from the snake_case fields, every ranking
    follows `next_cursor` until it is null, and no request carries `offset` (v2 rejects it with HTTP 400)."""
    rt = _runtime(state)
    try:
        await rt.store.init()
        await _collect_traders(rt)
        board = PM_BOARD["data"][0]
        cur = await rt.store.conn.execute(
            "SELECT venue, name, pnl_month, pnl_all, vlm_month FROM trader_snapshot WHERE account = ? ORDER BY venue",
            (board["user_id"],),
        )
        rows = [tuple(r) for r in await cur.fetchall()]
        # the same first page answers every ranking here, so each pnl_* is the fixture's month pnl
        assert rows == [(v, "user1", board["pnl"], board["pnl"], board["volume"]) for v in ("polymarket", "polymarket_crypto")]
        lb_calls = [c for c in rt._calls if c.startswith("pm:") and ":positions" not in c]  # type: ignore[attr-defined]
        assert len(lb_calls) == 2 * 4 * 2  # 2 categories x 4 rankings x (first page + the page behind next_cursor)
        assert {c.split(":")[1] for c in lb_calls} == {"overall", "crypto"}
        assert {c.split(":")[3] for c in lb_calls} == {"first", "next"}

        await _collect_trader_positions(rt)
        assert "pm:positions:CASH:100.0" in rt._calls  # type: ignore[attr-defined]
        top = PM_POS["data"][0]
        cur = await rt.store.conn.execute(
            "SELECT market, side, size, entry_px, value_usd, upnl, title FROM trader_positions "
            "WHERE venue = 'polymarket' AND market = ? LIMIT 1",
            (top["token_id"],),
        )
        got = tuple(await cur.fetchone())
        want = (top["token_id"], top["outcome"], top["current_size"], top["avg_price"], top["current_value"])
        assert got[:5] == want and got[5] == pytest.approx(top["unrealized_pnl"]) and got[6] == top["title"]
    finally:
        await rt.aclose()


async def test_polymarket_retired_v1_or_bad_shape_is_a_provider_error(state):
    """The live v1 answer (HTTP 410) leaves Polymarket out and Hyperliquid written; a bare v1-style list on a v2 route
    is a ProviderError, not rows read from the wrong fields."""
    rt = _runtime(state, pm_fail=True, pm_status=410)
    try:
        await rt.store.init()
        assert await _collect_traders(rt) == 2
        cur = await rt.store.conn.execute("SELECT DISTINCT venue FROM trader_snapshot")
        assert {r[0] for r in await cur.fetchall()} == {"hyperliquid"}
    finally:
        await rt.aclose()

    rt = _runtime(state, pm_body=PM_V1_BOARD)
    try:
        with pytest.raises(ProviderError, match="unexpected shape"):
            await rt.polymarket.leaderboard(period="MONTH")
        with pytest.raises(ProviderError, match="unexpected shape"):
            await rt.polymarket.positions("0x" + "0" * 39 + "c")
    finally:
        await rt.aclose()


async def test_polymarket_body_without_envelope_closes_nothing(state):
    """An HTTP-200 body without the v2 envelope (`{}`, `{"error": …}`, `data` without `pagination`) is a ProviderError,
    not an empty page: read as "no open positions" it would log every stored position of the wallet as closed."""
    routes: dict = {}
    hl_state = json.loads(json.dumps(HL_STATE))
    rt = _runtime(state, pm_routes=routes, hl_positions=hl_state)
    try:
        await rt.store.init()
        await _collect_traders(rt)
        assert await _collect_trader_positions(rt) == 8  # baseline: 2 HL x 2, 2 Polymarket wallets x 2
        for bad in ({}, {"error": "x"}, {"data": [], "error": "x"}):
            routes["/v2/positions"] = lambda req, bad=bad: httpx.Response(200, json=bad)
            with pytest.raises(ProviderError, match="unexpected shape at /v2/positions"):
                await rt.polymarket.positions("0x" + "0" * 39 + "b")
        hl_state["assetPositions"] = []  # Hyperliquid accounts went flat in the same run
        assert await _collect_trader_positions(rt) == 2 * 2  # only the Hyperliquid closes; no RuntimeError
        cur = await rt.store.conn.execute("SELECT venue, side FROM trader_positions WHERE side = 'closed'")
        assert {r[0] for r in await cur.fetchall()} == {"hyperliquid"}
        cur = await rt.store.conn.execute("SELECT COUNT(*) FROM trader_positions WHERE venue = 'polymarket'")
        assert (await cur.fetchone())[0] == 4  # the baseline rows only
    finally:
        await rt.aclose()


async def test_polymarket_leaderboard_walk_is_capped_by_traders_pm_pages(state):
    """The live v2 leaderboard never ends its cursor (40+ pages per ranking on 05.11), so `traders_pm_pages` alone
    stops the walk: exactly that many requests per (category, ranking), each passing the previous page's cursor."""
    seen: list[tuple[str, str, str | None]] = []

    def endless(req: httpx.Request) -> httpx.Response:
        p = req.url.params
        n = len(seen)
        seen.append((p["category"], p["time_period"], p.get("cursor")))
        row = dict(PM_BOARD["data"][0], user_id=f"0x{n:040x}", rank=n + 1)
        # a backstop at 6 pages per ranking, so an uncapped walk fails the count below instead of hanging the test
        cursor = f"c{n}" if sum((c, t) == (p["category"], p["time_period"]) for c, t, _ in seen) < 6 else None
        return httpx.Response(200, json={"data": [row], "pagination": {"has_more": cursor is not None, "next_cursor": cursor}})

    s = Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=":memory:", collector_enabled=True, traders_pm_pages=3)
    rt = _runtime(state, pm_routes={"/v2/leaderboard": endless}, settings=s)
    try:
        await rt.store.init()
        assert await _collect_traders(rt) == 2 + 2 * 4 * 3  # HL 2; one new wallet per page, 2 categories x 4 x 3 pages
        rankings = {(c, t) for c, t, _ in seen}
        assert rankings == {(c, t) for c in ("overall", "crypto") for t in ("day", "week", "month", "all")}
        for key in rankings:
            idx = [i for i, (c, t, _) in enumerate(seen) if (c, t) == key]
            assert len(idx) == 3
            assert [seen[i][2] for i in idx] == [None, f"c{idx[0]}", f"c{idx[1]}"]  # the cursor chain is followed
    finally:
        await rt.aclose()


async def test_polymarket_positions_keep_only_open_status(state):
    """A v2 row whose `status` is not OPEN (here CLOSED, `redeemable` false, worth >= $100) is not logged."""
    closed = dict(PM_POS["data"][0], token_id="777", status="CLOSED", redeemable=False, current_value=500.0)
    body = dict(PM_POS, data=[*PM_POS["data"], closed])
    rt = _runtime(state, pm_routes={"/v2/positions": lambda req: httpx.Response(200, json=body)})
    try:
        await rt.store.init()
        await _collect_traders(rt)
        assert await _collect_trader_positions(rt) == 2 * 2 + 2 * 2  # as without the CLOSED row
        cur = await rt.store.conn.execute("SELECT COUNT(*) FROM trader_positions WHERE market = '777'")
        assert (await cur.fetchone())[0] == 0
    finally:
        await rt.aclose()


async def test_flat_account_and_one_venue_down(state):
    rt = _runtime(state, pm_fail=True, hl_positions={"assetPositions": [], "marginSummary": {}})
    try:
        await rt.store.init()
        assert await _collect_traders(rt) == 2  # Polymarket down: Hyperliquid still written
        assert await _collect_trader_positions(rt) == 0  # flat accounts: state kept, no rows
        assert await rt.store.table_count("trader_positions") == 0
        assert await rt.store.table_count("trader_position_state") == 2
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


async def test_positions_are_a_change_log(state):
    rt = _runtime(state)
    try:
        await rt.store.init()
        st = rt.store
        a = {"market": "BTC", "side": "long", "size": 1.0, "value_usd": 80_000.0}
        b = {"market": "ETH", "side": "short", "size": 10.0, "value_usd": 24_000.0}
        assert await st.record_trader_positions(100, "hyperliquid", "0xa", [a, b]) == 2  # baseline
        assert await st.record_trader_positions(200, "hyperliquid", "0xa", [dict(a, value_usd=81_000.0), b]) == 0  # value only
        c = {"market": "SOL", "side": "long", "size": 50.0}
        assert await st.record_trader_positions(300, "hyperliquid", "0xa", [dict(a, size=1.5), c]) == 3
        cur = await st.conn.execute("SELECT market, side, size FROM trader_positions WHERE ts = 300 ORDER BY market")
        assert [tuple(r) for r in await cur.fetchall()] == [("BTC", "long", 1.5), ("ETH", "closed", 0.0), ("SOL", "long", 50.0)]
        assert await st.record_trader_positions(400, "hyperliquid", "0xa", []) == 2  # everything closed
    finally:
        await rt.aclose()


# ---------- schema 42: per-run rows of big HL positions and poll markers (hl-whale-liquidation-magnet, recount rev. 3/4)


async def _log_rows(st, account: str = "0xa") -> list[dict]:
    cur = await st.conn.execute(
        "SELECT ts, account, market, side, size, value_usd, liq_px FROM trader_positions WHERE account = ? ORDER BY ts, market",
        (account,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def test_big_positions_are_rewritten_every_run_and_the_diff_still_works(state):
    """(i): an unchanged position worth >= the floor gets a row at every run with its current values (fresh liq_px);
    an unchanged smaller one does not. The next run's diff still sees real changes and closes, and the venue without
    the floor (Polymarket) stays a pure change log."""
    rt = _runtime(state)
    try:
        await rt.store.init()
        st, floor = rt.store, 2_500_000.0
        big = {"market": "ETH", "side": "short", "size": 1000.0, "value_usd": 3_000_000.0, "liq_px": 3300.0}
        small = {"market": "SOL", "side": "long", "size": 100.0, "value_usd": 20_000.0, "liq_px": None}
        assert await st.record_trader_positions(100, "hyperliquid", "0xa", [big, small], rewrite_min_usd=floor) == 2  # baseline
        moved = dict(big, value_usd=2_900_000.0, liq_px=3250.0)  # same side and size, the mark and liq_px moved
        assert (
            await st.record_trader_positions(200, "hyperliquid", "0xa", [moved, dict(small, value_usd=21_000.0)], rewrite_min_usd=floor)
            == 1
        )
        rows = await _log_rows(st)
        assert [(r["ts"], r["market"], r["liq_px"], r["value_usd"]) for r in rows if r["ts"] == 200] == [(200, "ETH", 3250.0, 2_900_000.0)]
        # below the floor at this run: no repeated row, even though it was big before
        assert (
            await st.record_trader_positions(300, "hyperliquid", "0xa", [dict(big, value_usd=2_400_000.0), small], rewrite_min_usd=floor)
            == 0
        )
        # a real change and a close next to a repeat: the diff compares with the stored last full set as before
        grown = dict(big, size=1500.0, value_usd=4_500_000.0, liq_px=3100.0)
        assert await st.record_trader_positions(400, "hyperliquid", "0xa", [grown], rewrite_min_usd=floor) == 2
        rows = await _log_rows(st)
        assert [(r["market"], r["side"], r["size"]) for r in rows if r["ts"] == 400] == [("ETH", "short", 1500.0), ("SOL", "closed", 0.0)]
        assert await st.record_trader_positions(500, "hyperliquid", "0xa", [grown], rewrite_min_usd=floor) == 1  # repeat only
        assert await st.record_trader_positions(600, "hyperliquid", "0xa", [], rewrite_min_usd=floor) == 1  # the close
        # no floor (Polymarket): an unchanged position worth millions is not re-written
        pm = {"market": "777", "side": "Yes", "size": 5e6, "value_usd": 4e6}
        assert await st.record_trader_positions(100, "polymarket", "0xp", [pm]) == 1
        assert await st.record_trader_positions(200, "polymarket", "0xp", [pm]) == 0
        # the boundary of the collector's floor: exactly $2.5M is re-written on an unchanged run, a dollar less is not
        at = dict(big, value_usd=2_500_000.0)
        below = dict(small, value_usd=2_499_999.0)
        floor = TRADER_POSITIONS_REWRITE_USD
        assert await st.record_trader_positions(100, "hyperliquid", "0xedge", [at, below], rewrite_min_usd=floor) == 2  # baseline
        assert await st.record_trader_positions(200, "hyperliquid", "0xedge", [at, below], rewrite_min_usd=floor) == 1
        assert [r["market"] for r in await _log_rows(st, "0xedge") if r["ts"] == 200] == ["ETH"]
    finally:
        await rt.aclose()


async def test_a_repeated_row_changes_only_liq_px_and_row_time_in_both_replays(state):
    """(i): replaying a log with repeated rows (smart_money.replay_positions, hl_liq_magnet.replay_account) gives the
    same positions, sides and sizes as without them; only liq_px (and the current value) and the row's ts move."""
    from tradedesk.analytics.hl_liq_magnet import replay_account  # noqa: PLC0415
    from tradedesk.analytics.smart_money import position_changes, replay_positions  # noqa: PLC0415

    rt = _runtime(state)
    try:
        await rt.store.init()
        st, floor = rt.store, 2_500_000.0
        big = {"market": "ETH", "side": "short", "size": 1000.0, "value_usd": 3_000_000.0, "liq_px": 3300.0}
        small = {"market": "SOL", "side": "long", "size": 100.0, "value_usd": 20_000.0, "liq_px": 50.0}
        await st.record_trader_positions(100, "hyperliquid", "0xa", [big, small], rewrite_min_usd=floor)
        # exact size drifts below the 4-significant-digit change granularity: still "unchanged", written as a repeat
        await st.record_trader_positions(200, "hyperliquid", "0xa", [dict(big, size=1000.3, liq_px=3250.0), small], rewrite_min_usd=floor)
        rows = await _log_rows(st)
        assert {r["ts"] for r in rows} == {100, 200}
        without = [r for r in rows if r["ts"] == 100]

        s1, s2 = replay_positions(rows, 100)["0xa"], replay_positions(without, 100)["0xa"]
        assert set(s1) == set(s2) == {"ETH", "SOL"}
        assert {m: p["side"] for m, p in s1.items()} == {m: p["side"] for m, p in s2.items()}
        assert s1["ETH"]["size"] == pytest.approx(s2["ETH"]["size"], rel=1e-3)  # the same at 4 significant digits
        assert (s1["ETH"]["liq_px"], s2["ETH"]["liq_px"], s1["ETH"]["ts"]) == (3250.0, 3300.0, 200)
        assert s1["SOL"] == s2["SOL"]
        assert position_changes({"0xa": s2}, {"0xa": s1}, ["0xa"]) == []  # a repeat is not a trade

        groups: dict[int, list] = {}
        for r in rows:
            groups.setdefault(r["ts"], []).append(r)
        (t1, st1, since1), (t2, st2, since2) = replay_account(sorted(groups.items()), 100)
        assert (t1, t2) == (100, 200) and set(st1) == set(st2) == {"ETH", "SOL"}
        assert st2["ETH"]["side"] == st1["ETH"]["side"] and st2["SOL"] == st1["SOL"]
        assert (st1["ETH"]["liq_px"], st2["ETH"]["liq_px"]) == (3300.0, 3250.0)
        assert since1 == {"ETH": 100, "SOL": 100} and since2 == {"ETH": 200, "SOL": 100}  # the repeat makes liq_px fresh
    finally:
        await rt.aclose()


HL_A, HL_B = "0x" + "0" * 39 + "1", "0x" + "0" * 39 + "2"  # the fixture's two watched Hyperliquid accounts


async def _markers(rt) -> dict[str, int]:
    cur = await rt.store.conn.execute("SELECT ts, venue, account, ok FROM trader_polls ORDER BY account")
    rows = [tuple(r) for r in await cur.fetchall()]
    assert {(t, v) for t, v, _, _ in rows} <= {(int(NOW.timestamp()), "hyperliquid")}  # the run's ts, HL only
    return {a: ok for _, _, a, ok in rows}


def _spy_hl(rt, on_call):
    """Wrap the HL clearinghouseState request: ``on_call(account)`` runs (and may raise) before the real request."""
    real = rt.hyperliquid.clearinghouse_state

    async def spy(user: str):
        await on_call(user)
        return await real(user)

    rt.hyperliquid.clearinghouse_state = spy


async def test_poll_markers_exist_before_the_first_request_and_turn_ok_after_success(state):
    rt = _runtime(state)
    try:
        await rt.store.init()
        await _collect_traders(rt)
        seen: list[dict[str, int]] = []

        async def look(user: str) -> None:
            if not seen:
                seen.append(await _markers(rt))

        _spy_hl(rt, look)
        assert await _collect_trader_positions(rt) == 2 * 2 + 2 * 2
        assert seen == [{HL_A: 0, HL_B: 0}]  # the whole intended list, not ok, before any request
        assert await _markers(rt) == {HL_A: 1, HL_B: 1}
        cur = await rt.store.conn.execute("SELECT DISTINCT ts FROM trader_positions WHERE venue = 'hyperliquid'")
        assert [r[0] for r in await cur.fetchall()] == [int(NOW.timestamp())]  # rows and markers share the run's ts
    finally:
        await rt.aclose()


async def test_a_failed_request_keeps_its_marker_not_ok(state):
    rt = _runtime(state)
    try:
        await rt.store.init()
        await _collect_traders(rt)

        async def fail_b(user: str) -> None:
            if user == HL_B:
                raise ProviderError("hyperliquid", "backend_error", "down")

        _spy_hl(rt, fail_b)
        assert await _collect_trader_positions(rt) == 2 + 2 * 2  # HL_A's two positions and Polymarket
        assert await _markers(rt) == {HL_A: 1, HL_B: 0}
        cur = await rt.store.conn.execute("SELECT COUNT(*) FROM trader_positions WHERE account = ?", (HL_B,))
        assert (await cur.fetchone())[0] == 0
    finally:
        await rt.aclose()


async def test_accounts_a_stopped_run_never_reached_keep_their_marker_not_ok(state):
    rt = _runtime(state)
    try:
        await rt.store.init()
        await _collect_traders(rt)
        calls: list[str] = []

        async def stop_at_second(user: str) -> None:
            calls.append(user)
            if len(calls) == 2:
                raise asyncio.CancelledError  # the run is cancelled mid-way (service stop)

        _spy_hl(rt, stop_at_second)
        with pytest.raises(asyncio.CancelledError):
            await _collect_trader_positions(rt)
        assert sorted(calls) == [HL_A, HL_B]
        assert await _markers(rt) == {calls[0]: 1, calls[1]: 0}
        assert await rt.store.table_count("trader_polls") == 2

        # cancelled before its first request: every intended account stays not ok
        rt.now = lambda: NOW + timedelta(hours=1)
        calls.clear()

        async def stop_at_once(user: str) -> None:
            calls.append(user)
            raise asyncio.CancelledError

        _spy_hl(rt, stop_at_once)
        with pytest.raises(asyncio.CancelledError):
            await _collect_trader_positions(rt)
        cur = await rt.store.conn.execute(
            "SELECT account, ok FROM trader_polls WHERE ts = ? ORDER BY account", (int(NOW.timestamp()) + 3600,)
        )
        assert [tuple(r) for r in await cur.fetchall()] == [(HL_A, 0), (HL_B, 0)] and len(calls) == 1
    finally:
        await rt.aclose()


async def test_a_marker_turns_ok_only_after_the_account_rows_are_written(state):
    """(ii): a failed position-row write stops the run with that account not ok (else it would read as "polled, no big
    position"); the account written before it is ok, the one never reached stays not ok."""
    rt = _runtime(state)
    try:
        await rt.store.init()
        await _collect_traders(rt)
        third = {"account": "0x" + "0" * 39 + "4", "account_value": 600_000.0, "pnl_month": -1e12, "pnl_all": -1e12}
        await rt.store.insert_trader_snapshot("2026-10-10", 0, "hyperliquid", [third])  # ranked last in both lists
        order = await rt.store.tracked_traders("hyperliquid", rt.settings.traders_top_n, rt.settings.traders_min_account_usd)
        assert len(order) == 3 and order[2] == third["account"]
        first, second, later = order
        real = rt.store.record_trader_positions

        async def fail_second(ts, venue, account, positions, **kwargs):
            if account == second:
                raise sqlite3.OperationalError("database is locked")
            return await real(ts, venue, account, positions, **kwargs)

        rt.store.record_trader_positions = fail_second
        with pytest.raises(sqlite3.OperationalError):
            await _collect_trader_positions(rt)
        assert await _markers(rt) == {first: 1, second: 0, later: 0}
        cur = await rt.store.conn.execute("SELECT DISTINCT account FROM trader_positions")
        assert [r[0] for r in await cur.fetchall()] == [first]
    finally:
        await rt.aclose()


async def test_a_failed_marker_write_polls_nobody(state):
    rt = _runtime(state)
    try:
        await rt.store.init()
        await _collect_traders(rt)
        calls: list[str] = []

        async def note(user: str) -> None:
            calls.append(user)

        async def broken(ts, venue, accounts):
            raise sqlite3.OperationalError("database is locked")

        _spy_hl(rt, note)
        rt.store.start_trader_polls = broken
        before = len(rt._calls)  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError, match="poll markers not written, nobody polled"):
            await _collect_trader_positions(rt)
        assert calls == [] and rt._calls[before:] == []  # type: ignore[attr-defined]  # no HL and no Polymarket request
        assert await rt.store.table_count("trader_positions") == 0
        assert await rt.store.table_count("trader_polls") == 0
    finally:
        await rt.aclose()


async def test_poll_markers_are_not_prunable(state):
    """The recount needs >= 60 days of markers: the collector's retention cannot delete them."""
    rt = _runtime(state)
    try:
        await rt.store.init()
        with pytest.raises(ValueError, match="unknown table"):
            await rt.store.prune("trader_polls", 10**12)
    finally:
        await rt.aclose()


async def test_migration_42_adds_trader_polls_to_a_v41_store(tmp_path):
    from tradedesk.store import SCHEMA_VERSION, Store  # noqa: PLC0415
    from tradedesk.store.schema import MIGRATIONS  # noqa: PLC0415

    path = tmp_path / "v41.sqlite"
    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if v <= 41:
            for stmt in MIGRATIONS[v]:
                con.execute(stmt)
    con.execute("PRAGMA user_version = 41")
    con.execute(
        "INSERT INTO trader_positions (ts, venue, account, market, side, size) VALUES (1, 'hyperliquid', '0xa', 'BTC', 'long', 1.0)"
    )
    con.commit()
    con.close()
    s = Store(path)
    await s.init()
    try:
        assert await s.version() == SCHEMA_VERSION >= 42
        assert await s.table_count("trader_positions") == 1  # the old log is kept
        assert await s.start_trader_polls(5, "hyperliquid", ["0xa", "0xb"]) == 2
        await s.set_trader_poll_ok(5, "hyperliquid", "0xb")
        cur = await s.conn.execute("SELECT account, ok FROM trader_polls ORDER BY account")
        assert [tuple(r) for r in await cur.fetchall()] == [("0xa", 0), ("0xb", 1)]
    finally:
        await s.aclose()
