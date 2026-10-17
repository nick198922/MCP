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

    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
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
        f = await rt.exchanges.funding_rate_history_range("okx", "BTC/USDT:USDT", START, START + 100 * H8)
        assert f.data["complete"] is True
        assert [r["timestamp"] for r in f.data["rows"]] == [r["timestamp"] for r in _Capped.rows]
        assert f.evidence.args["pages"] >= 9
    finally:
        await rt.aclose()


async def test_ccxt_funding_range_page_limit_marks_incomplete(state):
    rt = _rt(state, _Capped)
    try:
        f = await rt.exchanges.funding_rate_history_range("okx", "BTC/USDT:USDT", START, START + 100 * H8, max_pages=3)
        assert f.data["complete"] is False
        assert len(f.data["rows"]) == 30
    finally:
        await rt.aclose()


async def test_ccxt_funding_range_later_failure_keeps_rows_first_failure_raises(state):
    rt = _rt(state, _Capped)
    try:
        _Capped.fail_after = 2
        f = await rt.exchanges.funding_rate_history_range("okx", "BTC/USDT:USDT", START, START + 100 * H8)
        assert f.data["complete"] is False and len(f.data["rows"]) == 20
        _Capped.calls, _Capped.fail_after = 0, 0
        with pytest.raises(ProviderError):
            await rt.exchanges.funding_rate_history_range("okx", "BTC/USDT:USDT", START, START + 100 * H8)
    finally:
        await rt.aclose()


async def test_ccxt_funding_range_hops_over_empty_window_before_listing(state):
    rt = _rt(state, _Capped)
    try:
        since = START - 45 * 86_400_000  # 45 days before the first row: the first windows come back empty
        f = await rt.exchanges.funding_rate_history_range("okx", "BTC/USDT:USDT", since, START + 100 * H8)
        assert f.data["complete"] is True
        assert [r["timestamp"] for r in f.data["rows"]] == [r["timestamp"] for r in _Capped.rows]  # nothing skipped
    finally:
        await rt.aclose()


class _BybitNewest(FakeEx):
    """Like Bybit: only the newest ``limit`` rows of [since, until] come back; without ``until`` the window is cut short."""

    rows = [{"timestamp": START + i * H8, "fundingRate": 0.0001} for i in range(450)]
    seen_until: list = []

    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
        until = (params or {}).get("until")
        type(self).seen_until.append(until)
        if until is None:
            until = (since or 0) + limit * 3_600_000  # ccxt's implicit window: too short for 8h rows
        win = [r for r in self.rows if (since or 0) <= r["timestamp"] <= until]
        return win[-limit:]


async def test_bybit_funding_range_walks_backward_from_end_time_without_holes(state):
    _BybitNewest.seen_until = []
    rt = _rt(state, _BybitNewest)
    try:
        f = await rt.exchanges.funding_rate_history_range("bybit", "BTC/USDT:USDT", START, START + 450 * H8)
        assert f.data["complete"] is True
        assert [r["timestamp"] for r in f.data["rows"]] == [r["timestamp"] for r in _BybitNewest.rows]  # all 450, no holes
        assert all(u is not None for u in _BybitNewest.seen_until)  # endTime always explicit
        assert f.evidence.args["pages"] == 3
    finally:
        await rt.aclose()


async def test_bybit_funding_range_first_page_failure_raises(state):
    class Down(_BybitNewest):
        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            raise ccxt.ExchangeNotAvailable("down")

    rt = _rt(state, Down)
    try:
        with pytest.raises(ProviderError):
            await rt.exchanges.funding_rate_history_range("bybit", "BTC/USDT:USDT", START, START + 450 * H8)
    finally:
        await rt.aclose()


async def test_bybit_funding_range_page_limit_and_later_failure_mark_incomplete(state):
    rt = _rt(state, _BybitNewest)
    try:
        f = await rt.exchanges.funding_rate_history_range("bybit", "BTC/USDT:USDT", START, START + 450 * H8, max_pages=2)
        assert f.data["complete"] is False and len(f.data["rows"]) == 400
        assert [r["timestamp"] for r in f.data["rows"]] == [r["timestamp"] for r in _BybitNewest.rows[-400:]]  # newest, contiguous
    finally:
        await rt.aclose()

    class FailsLater(_BybitNewest):
        n = 0

        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            type(self).n += 1
            if type(self).n > 1:
                raise ccxt.ExchangeNotAvailable("down")
            return await super().fetch_funding_rate_history(symbol, since, limit, params)

    rt = _rt(state, FailsLater)
    try:
        f = await rt.exchanges.funding_rate_history_range("bybit", "BTC/USDT:USDT", START, START + 450 * H8)
        assert f.data["complete"] is False and len(f.data["rows"]) == 200
    finally:
        await rt.aclose()


async def test_bybit_full_page_outside_the_window_or_oldest_first_is_not_complete(state):
    class Oldest(_BybitNewest):  # ignores endTime and returns the oldest rows first
        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            return [r for r in self.rows if r["timestamp"] >= (since or 0)][:limit]

    class Elsewhere(_BybitNewest):  # a full page, none of it inside [since, end]
        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            return [{"timestamp": START - 10**9 - i, "fundingRate": 0.0} for i in range(limit)]

    for cls in (Oldest, Elsewhere):
        rt = _rt(state, cls)
        try:
            f = await rt.exchanges.funding_rate_history_range("bybit", "BTC/USDT:USDT", START, START + 450 * H8)
            assert f.data["complete"] is False
        finally:
            await rt.aclose()


async def test_hyperliquid_funding_range_walks_500_row_pages(state):
    hour = 3_600_000
    total = [{"coin": "BTC", "fundingRate": "0.00001", "premium": "0", "time": START + i * hour} for i in range(1200)]

    def hl(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body["type"] != "fundingHistory":
            return httpx.Response(200, json=[])
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
