from __future__ import annotations

import json
from dataclasses import replace

from mcp import Client

from conftest import NOW, FakeEx, make_transports, wait_until
from tradedesk.collector.scheduler import (
    PeriodicJob,
    Scheduler,
    _collect_atm_term,
    _collect_basis,
    _collect_funding,
    _evaluate_alerts,
    _retention_sweep,
)
from tradedesk.config import Settings
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


async def _rt_with_memory_store(state) -> Runtime:
    settings = Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=":memory:", collector_enabled=True)
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    return rt


async def test_atm_term_job_writes_rows(state):
    rt = await _rt_with_memory_store(state)
    try:
        written = await _collect_atm_term(rt)
        # Fake Deribit returns the same option_book for BTC and ETH → 3 expiries × 2 currencies.
        assert written == 6
        assert await rt.store.table_count("atm_term") == 6
        series = await rt.store.atm_term_series("BTC", "2026-10-30T08:00:00+00:00", 0, 2_000_000_000)
        assert len(series) == 1 and series[0]["atm_iv"] > 0
    finally:
        await rt.aclose()


async def test_scheduler_runs_once_and_stops(state):
    rt = await _rt_with_memory_store(state)
    # Fast period just to confirm the loop ticks once; test will stop immediately.
    sched = Scheduler(rt, [PeriodicJob(name="atm_term", period_seconds=60, fn=_collect_atm_term)])
    try:
        await sched.start()
        # Wait for the first iteration to complete.
        await wait_until(lambda: sched.jobs[0].metrics.runs > 0)
        assert sched.jobs[0].metrics.runs == 1
        assert sched.jobs[0].metrics.successes == 1
        assert sched.jobs[0].metrics.failures == 0
        assert sched.jobs[0].metrics.last_rows_written == 6
    finally:
        await sched.stop()
        await rt.aclose()


async def test_scheduler_captures_failure(state):
    rt = await _rt_with_memory_store(state)

    async def boom(_: Runtime) -> int:
        raise RuntimeError("nope")

    sched = Scheduler(rt, [PeriodicJob(name="boom", period_seconds=60, fn=boom)])
    try:
        await sched.start()
        await wait_until(lambda: sched.jobs[0].metrics.runs > 0)
        m = sched.jobs[0].metrics
        assert m.runs == 1 and m.failures == 1 and m.successes == 0
        assert m.last_error and "nope" in m.last_error
    finally:
        await sched.stop()
        await rt.aclose()


async def test_scheduler_keeps_last_failure_after_a_success(state, monkeypatch):
    # Live 2026-11-02: liquidations:bybit and :okx showed failures=7 of 8 runs with last_error None, because the
    # one success after them cleared the reason. last_failure keeps it.
    rt = await _rt_with_memory_store(state)
    outcomes = [RuntimeError("ConnectionClosedError: keepalive ping timeout"), 5]

    async def flaky(_: Runtime) -> int:
        out = outcomes.pop(0) if outcomes else 0
        if isinstance(out, Exception):
            raise out
        return out

    sched = Scheduler(rt, [PeriodicJob(name="flaky", period_seconds=0, fn=flaky)])
    try:
        await sched.start()
        await wait_until(lambda: sched.jobs[0].metrics.successes > 0)
        m = sched.jobs[0].metrics
        assert m.failures == 1 and m.successes >= 1
        assert m.last_error is None  # the latest run succeeded
        assert m.last_failure and "keepalive ping timeout" in m.last_failure and m.last_failure_ts is not None
        # system_health is where the owner looks for the reason: both fields must reach collector[].
        monkeypatch.setattr("tradedesk.collector.build_default_scheduler", lambda _rt: sched)
        srv, _ = build_server(rt)
        async with Client(srv) as c:
            res = await c.call_tool("system_health", {})
        data = res.structured_content or json.loads(res.content[0].text)
        data = data.get("result", data)
        (row,) = [j for j in data["data"]["collector"] if j["name"] == "flaky"]
        assert "keepalive ping timeout" in row["last_failure"] and row["last_failure_ts"] == m.last_failure_ts
    finally:
        await sched.stop()
        await rt.aclose()


async def test_build_default_scheduler_respects_flag(state):
    tr = make_transports(state)
    # Disabled by default.
    rt_off = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=":memory:"), transports=tr, exchange_factory=FakeEx
    )
    assert rt_off.store is not None
    await rt_off.store.init()
    from tradedesk.collector import build_default_scheduler  # noqa: PLC0415

    assert build_default_scheduler(rt_off) is None
    await rt_off.aclose()

    # Enabled but no store path → still None.
    rt_no_db = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path="", collector_enabled=True),
        transports=tr,
        exchange_factory=FakeEx,
    )
    assert rt_no_db.store is None
    assert build_default_scheduler(rt_no_db) is None
    await rt_no_db.aclose()

    # Keep the Settings-defaults contract explicit.
    _ = replace  # silence unused-import complaints when the file is edited later


async def test_funding_job_writes_rows(state):
    rt = await _rt_with_memory_store(state)
    try:
        written = await _collect_funding(rt)
        # FakeEx: binanceusdm + bybit yield funding (OKX raises); HL provides BTC+ETH;
        # Deribit perpetual has funding_8h in the FUTURES fixture → 1 row per currency.
        # Expect something non-trivial for BTC and ETH, plus HL + Deribit rows.
        assert written >= 4
        snap = await rt.store.funding_series("binanceusdm", "BTC", 0, 2_000_000_000)
        assert len(snap) == 1 and snap[0]["annualized"] != 0
    finally:
        await rt.aclose()


async def test_basis_job_writes_rows(state):
    rt = await _rt_with_memory_store(state)
    try:
        written = await _collect_basis(rt)
        # Fake Deribit FUTURES fixture (3 dated + 1 perp) is returned for both BTC and ETH
        # in the fake transport → 3 × 2 = 6 dated rows total.
        assert written == 6
        assert await rt.store.table_count("basis_hist") == 6
    finally:
        await rt.aclose()


async def test_evaluate_alerts_fires_and_writes(state, tmp_path):
    rules_path = tmp_path / "rules.json"
    import json

    rules_path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "id": "funding_hot",
                        "enabled": True,
                        "cooldown_seconds": 600,
                        "metric": {"kind": "funding", "venue": "binanceusdm", "base": "BTC"},
                        "when": {"op": ">", "value": 0.30},
                        "message": "hot {value_pct:.1f}%",
                    }
                ]
            }
        )
    )
    settings = Settings(
        http_retries=1,
        http_retry_backoff=0.0,
        cache_scale=0.0,
        db_path=":memory:",
        collector_enabled=True,
        alerts_enabled=True,
        alerts_rules_path=str(rules_path),
    )
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    try:
        # Seed a funding row above threshold at NOW.
        await rt.store.insert_funding(
            ts=int(NOW.timestamp()),
            rows=[
                {
                    "venue": "binanceusdm",
                    "base": "BTC",
                    "rate_per_interval": 0.0003,
                    "interval_hours": 8.0,
                    "annualized": 0.32,
                }
            ],
        )
        rows = await _evaluate_alerts(rt)
        assert rows == 1
        recent = await rt.store.recent_alerts()
        assert len(recent) == 1
        assert recent[0]["rule_id"] == "funding_hot"
        assert recent[0]["channel"] == "log"
        assert recent[0]["delivered"] is True

        # Second evaluation within the cooldown must not create a new row.
        rows2 = await _evaluate_alerts(rt)
        assert rows2 == 0
    finally:
        await rt.aclose()


async def test_retention_sweep_deletes_old_rows(state):
    settings = Settings(
        http_retries=1,
        http_retry_backoff=0.0,
        cache_scale=0.0,
        db_path=":memory:",
        collector_enabled=True,
        retention_atm_term_days=5,
        retention_funding_days=0,  # 0 disables prune for that table
        retention_basis_days=0,
    )
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    try:
        now_ts = int(rt.now().timestamp())
        rows = [
            {"expiry": "2026-10-30T08:00:00+00:00", "days": 20.0, "forward": 83000.0, "atm_iv": 0.42},
        ]
        await rt.store.insert_atm_term(ts=now_ts - 10 * 86_400, currency="BTC", rows=rows)  # 10 days ago
        await rt.store.insert_atm_term(ts=now_ts - 1 * 86_400, currency="BTC", rows=rows)  # 1 day ago
        assert await rt.store.table_count("atm_term") == 2
        deleted = await _retention_sweep(rt)
        assert deleted == 1
        assert await rt.store.table_count("atm_term") == 1
    finally:
        await rt.aclose()


def _coin(i: int) -> dict:
    return {
        "id": f"coin-{i}",
        "symbol": f"c{i}",
        "name": f"Coin {i}",
        "market_cap_rank": i,
        "current_price": 1.5,
        "market_cap": 1_500_000.0,
        "fully_diluted_valuation": None,
        "total_volume": 10_000.0,
        "circulating_supply": 1_000_000.0,
        "total_supply": 2_000_000.0,
        "max_supply": None,
        "last_updated": "2026-10-10T13:00:00.000Z",
    }


async def _rt_with_coingecko_pages(state, pages: dict[int, object]) -> Runtime:
    """A runtime whose CoinGecko /coins/markets answers by page: a list of rows, or an int HTTP error status."""
    import httpx  # noqa: PLC0415

    def cg(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        answer = pages.get(page, [])
        if isinstance(answer, int):
            return httpx.Response(answer, text="down")
        return httpx.Response(200, json=answer)

    transports = make_transports(state)
    transports["coingecko"] = httpx.MockTransport(cg)
    settings = Settings(
        http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=":memory:", collector_enabled=True, collect_supply_pages=3,
        collect_supply_extra_ids_path="/nonexistent",
    )  # fmt: skip
    rt = Runtime(settings, transports=transports, exchange_factory=FakeEx)
    rt.coingecko.http._limiter = TokenBucket(1_000.0, 1_000)  # the keyless 0.5 req/s limit only slows the test (~8 s)
    rt.now = lambda: NOW
    await rt.store.init()
    return rt


async def test_supply_job_writes_daily_snapshot_idempotently(state):
    from tradedesk.collector.scheduler import _collect_supply  # noqa: PLC0415

    rt = await _rt_with_coingecko_pages(state, {1: [_coin(1), _coin(2), {"id": "", "symbol": "x"}], 2: [_coin(3)]})
    try:
        assert await _collect_supply(rt) == 3  # page 3 is empty: stop there; the row without an id is skipped
        assert await _collect_supply(rt) == 3  # same day again: replaced, not duplicated
        assert await rt.store.table_count("supply_snapshot") == 3
        cur = await rt.store.conn.execute("SELECT day, symbol, circulating_supply, fdv FROM supply_snapshot WHERE coin_id = 'coin-2'")
        assert tuple(await cur.fetchone()) == ("2026-10-10", "C2", 1_000_000.0, None)
    finally:
        await rt.aclose()


async def test_supply_job_keeps_pages_fetched_before_a_failure(state):
    import pytest  # noqa: PLC0415

    from tradedesk.collector.scheduler import _collect_supply  # noqa: PLC0415
    from tradedesk.core.errors import ProviderError  # noqa: PLC0415

    rt = await _rt_with_coingecko_pages(state, {1: [_coin(1)], 2: 500})
    try:
        assert await _collect_supply(rt) == 1
    finally:
        await rt.aclose()
    rt = await _rt_with_coingecko_pages(state, {1: 500})
    try:
        with pytest.raises(ProviderError):
            await _collect_supply(rt)
        assert await rt.store.table_count("supply_snapshot") == 0
    finally:
        await rt.aclose()


def test_supply_job_is_scheduled_with_the_collector(state):
    from tradedesk.collector.scheduler import build_default_scheduler  # noqa: PLC0415

    on = Runtime(Settings(db_path=":memory:", collector_enabled=True), transports=make_transports(state), exchange_factory=FakeEx)
    off = Runtime(
        Settings(db_path=":memory:", collector_enabled=True, collect_supply_enabled=False),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    assert "supply" in [j.name for j in build_default_scheduler(on).jobs]
    assert "supply" not in [j.name for j in build_default_scheduler(off).jobs]


async def test_supply_job_adds_extra_ids_and_global_totals(state, tmp_path):
    import httpx  # noqa: PLC0415

    from tradedesk.collector.scheduler import _collect_supply  # noqa: PLC0415

    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("# comment\ncoin-1\ncoin-50  # outside the top pages\ncoin-60\n\ncoin-50\n")
    asked: list[str] = []

    def cg(req: httpx.Request) -> httpx.Response:
        path = req.url.path.split("/api/v3", 1)[-1]
        if path == "/global":
            data = {
                "total_market_cap": {"usd": 2_500_000_000_000.0},
                "total_volume": {"usd": 90_000_000_000.0},
                "market_cap_percentage": {"btc": 57.5, "eth": 11.2},
                "market_cap_change_percentage_24h_usd": -1.25,
                "active_cryptocurrencies": 17_000,
            }
            return httpx.Response(200, json={"data": data})
        if "ids" in req.url.params:
            asked.append(req.url.params["ids"])
            wanted = req.url.params["ids"].split(",")
            return httpx.Response(200, json=[_coin(int(i.split("-")[1])) for i in wanted if i != "coin-60"])  # 60: delisted
        page = int(req.url.params.get("page", "1"))
        return httpx.Response(200, json=[_coin(1), _coin(2)] if page == 1 else [])

    transports = make_transports(state)
    transports["coingecko"] = httpx.MockTransport(cg)
    settings = Settings(
        http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=":memory:", collector_enabled=True,
        collect_supply_pages=2, collect_supply_extra_ids_path=str(ids_file),
    )  # fmt: skip
    rt = Runtime(settings, transports=transports, exchange_factory=FakeEx)
    rt.coingecko.http._limiter = TokenBucket(1_000.0, 1_000)
    rt.now = lambda: NOW
    await rt.store.init()
    try:
        assert await _collect_supply(rt) == 3 + 1  # coin-1, coin-2 from the pages, coin-50 extra; plus the global row
        assert asked == ["coin-50,coin-60"]  # coin-1 is already in the pages; the duplicate line is read once
        assert await rt.store.table_count("supply_snapshot") == 3
        cur = await rt.store.conn.execute(
            "SELECT day, total_market_cap, btc_dominance, eth_dominance, mcap_change_24h, active_cryptocurrencies FROM global_snapshot"
        )
        import pytest  # noqa: PLC0415

        assert tuple(await cur.fetchone()) == pytest.approx(("2026-10-10", 2_500_000_000_000.0, 0.575, 0.112, -0.0125, 17_000))
    finally:
        await rt.aclose()


async def test_supply_job_survives_extra_and_global_failures(state, tmp_path):
    import httpx  # noqa: PLC0415

    from tradedesk.collector.scheduler import _collect_supply  # noqa: PLC0415

    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("coin-50\n")

    def cg(req: httpx.Request) -> httpx.Response:
        path = req.url.path.split("/api/v3", 1)[-1]
        if path == "/global" or "ids" in req.url.params:
            return httpx.Response(500, text="down")
        page = int(req.url.params.get("page", "1"))
        return httpx.Response(200, json=[_coin(1)] if page == 1 else [])

    transports = make_transports(state)
    transports["coingecko"] = httpx.MockTransport(cg)
    settings = Settings(
        http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=":memory:", collector_enabled=True,
        collect_supply_pages=1, collect_supply_extra_ids_path=str(ids_file),
    )  # fmt: skip
    rt = Runtime(settings, transports=transports, exchange_factory=FakeEx)
    rt.coingecko.http._limiter = TokenBucket(1_000.0, 1_000)
    rt.now = lambda: NOW
    await rt.store.init()
    try:
        assert await _collect_supply(rt) == 1  # the top page is still written
        assert await rt.store.table_count("global_snapshot") == 0
    finally:
        await rt.aclose()


def test_supply_extra_ids_file(tmp_path):
    from tradedesk.collector.scheduler import _supply_extra_ids  # noqa: PLC0415

    assert _supply_extra_ids(str(tmp_path / "missing.txt")) == []
    shipped = _supply_extra_ids("")  # the repo's deploy/coingecko_extra_ids.txt
    assert len(shipped) > 100 and all(" " not in i and "#" not in i for i in shipped)
