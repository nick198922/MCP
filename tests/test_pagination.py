from __future__ import annotations

import json

import ccxt.async_support as ccxt
import httpx
import pytest

from conftest import FakeEx, make_transports
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.runtime import Runtime

H8 = 8 * 3_600_000
START = 1_780_000_000_000


class _Capped(FakeEx):
    """Funding history that returns at most 10 rows per call, like a venue page cap."""

    rows = [{"timestamp": START + i * H8, "fundingRate": 0.0001 * (1 + i % 3)} for i in range(90)]
    fail_after: int | None = None
    calls = 0

    async def fetch_funding_rate_history(self, symbol, since=None, limit=500):
        # Like Bybit through ccxt: the request covers a time window of `limit` intervals from `since`, capped at 10 rows.
        type(self).calls += 1
        if self.fail_after is not None and type(self).calls > self.fail_after:
            raise ccxt.ExchangeNotAvailable("down")
        lo = since if since is not None else 0
        return [r for r in self.rows if lo <= r["timestamp"] < lo + limit * 3_600_000][:10]


def _rt(state, factory) -> Runtime:
    return Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=""), transports=make_transports(state), exchange_factory=factory)


@pytest.fixture(autouse=True)
def _reset():
    _Capped.calls, _Capped.fail_after = 0, None
    yield


async def test_ccxt_funding_range_walks_all_pages(state):
    rt = _rt(state, _Capped)
    try:
        f = await rt.exchanges.funding_rate_history_range("bybit", "BTC/USDT:USDT", START, START + 100 * H8)
        assert f.data["complete"] is True
        assert [r["timestamp"] for r in f.data["rows"]] == [r["timestamp"] for r in _Capped.rows]
        assert f.evidence.args["pages"] >= 9
    finally:
        await rt.aclose()


async def test_ccxt_funding_range_page_limit_marks_incomplete(state):
    rt = _rt(state, _Capped)
    try:
        f = await rt.exchanges.funding_rate_history_range("bybit", "BTC/USDT:USDT", START, START + 100 * H8, max_pages=3)
        assert f.data["complete"] is False
        assert len(f.data["rows"]) == 30
    finally:
        await rt.aclose()


async def test_ccxt_funding_range_later_failure_keeps_rows_first_failure_raises(state):
    rt = _rt(state, _Capped)
    try:
        _Capped.fail_after = 2
        f = await rt.exchanges.funding_rate_history_range("bybit", "BTC/USDT:USDT", START, START + 100 * H8)
        assert f.data["complete"] is False and len(f.data["rows"]) == 20
        _Capped.calls, _Capped.fail_after = 0, 0
        with pytest.raises(ProviderError):
            await rt.exchanges.funding_rate_history_range("bybit", "BTC/USDT:USDT", START, START + 100 * H8)
    finally:
        await rt.aclose()


async def test_ccxt_funding_range_hops_over_empty_window_before_listing(state):
    rt = _rt(state, _Capped)
    try:
        since = START - 45 * 86_400_000  # 45 days before the first row: the first windows come back empty
        f = await rt.exchanges.funding_rate_history_range("bybit", "BTC/USDT:USDT", since, START + 100 * H8)
        assert f.data["complete"] is True
        assert [r["timestamp"] for r in f.data["rows"]] == [r["timestamp"] for r in _Capped.rows]  # nothing skipped
    finally:
        await rt.aclose()


async def test_hyperliquid_funding_range_walks_500_row_pages(state):
    hour = 3_600_000
    total = [{"coin": "BTC", "fundingRate": "0.00001", "premium": "0", "time": START + i * hour} for i in range(1200)]

    def hl(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        start, end = body["startTime"], body.get("endTime", 10**15)
        return httpx.Response(200, json=[r for r in total if start <= r["time"] <= end][:500])

    tr = make_transports(state)
    tr["hyperliquid"] = httpx.MockTransport(hl)
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=""), transports=tr, exchange_factory=FakeEx)
    try:
        f = await rt.hyperliquid.funding_history_range("BTC", START, START + 1200 * hour)
        assert f.data["complete"] is True
        assert len(f.data["rows"]) == 1200
        assert f.evidence.args["pages"] == 3
    finally:
        await rt.aclose()
