"""Strong traders' public accounts: Hyperliquid leaderboard and positions, Polymarket leaderboard and positions,
the daily snapshot and position-change jobs (fixtures: real responses of 2026-10-15, Polymarket data-api v2 of
2026-11-05, with made-up addresses)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conftest import NOW, FakeEx, make_transports
from tradedesk.collector.scheduler import _collect_trader_positions, _collect_traders, build_default_scheduler
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
        assert await _collect_trader_positions(rt) == 0  # nothing changed
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
