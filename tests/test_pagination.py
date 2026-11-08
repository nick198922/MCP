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
    return Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=factory,
    )


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
        assert f.data["truncated"] is True and f.data["dropped_rows"] == 0
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


FAKE_END = 1791637200000  # last funding row of FakeEx.fetch_funding_rate_history


def _corrupt(bad):
    """FakeEx whose 6th funding row carries ``timestamp=bad`` (the other rows are FakeEx's own)."""

    class Corrupt(FakeEx):
        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            rows = [dict(x) for x in await super().fetch_funding_rate_history(symbol, since, limit, params)]
            if len(rows) > 5:
                rows[5]["timestamp"] = bad
            return rows

    return Corrupt


@pytest.mark.parametrize("venue", ["binanceusdm", "bybit"])
@pytest.mark.parametrize("bad", [None, "abc"])
async def test_ccxt_funding_range_malformed_timestamp_marks_incomplete(state, venue, bad):
    since, until = FAKE_END - 100 * H8, FAKE_END + H8
    rt = _rt(state, FakeEx)
    try:
        clean = await rt.exchanges.funding_rate_history_range(venue, "BTC/USDT:USDT", since, until)
    finally:
        await rt.aclose()
    assert clean.data["complete"] is True and clean.evidence.args["dropped_rows"] == 0
    assert clean.data["truncated"] is False and clean.data["dropped_rows"] == 0
    assert len(clean.data["rows"]) == 90

    rt = _rt(state, _corrupt(bad))
    try:
        f = await rt.exchanges.funding_rate_history_range(venue, "BTC/USDT:USDT", since, until)
    finally:
        await rt.aclose()
    assert f.data["complete"] is False
    assert f.data["truncated"] is False  # the walk finished: the venue's data is defective, not our request
    assert f.evidence.args["dropped_rows"] == f.data["dropped_rows"] == 1
    # the valid rows are exactly the clean ones minus the corrupted row
    assert f.data["rows"] == [x for i, x in enumerate(clean.data["rows"]) if i != 5]


@pytest.mark.parametrize("venue", ["binanceusdm", "bybit"])
@pytest.mark.parametrize("junk", [None, "row", 7, ["x"]])
async def test_ccxt_funding_range_non_dict_row_is_dropped_not_raised(state, venue, junk):
    class Junk(FakeEx):
        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            rows = [dict(x) for x in await super().fetch_funding_rate_history(symbol, since, limit, params)]
            return rows[:5] + [junk] + rows[5:] if len(rows) > 5 else rows  # junk on the data page only

    since, until = FAKE_END - 100 * H8, FAKE_END + H8
    rt = _rt(state, FakeEx)
    try:
        clean = await rt.exchanges.funding_rate_history_range(venue, "BTC/USDT:USDT", since, until)
    finally:
        await rt.aclose()
    rt = _rt(state, Junk)
    try:
        f = await rt.exchanges.funding_rate_history_range(venue, "BTC/USDT:USDT", since, until)
    finally:
        await rt.aclose()
    assert f.data["rows"] == clean.data["rows"]  # the valid rows and their numbers are unchanged
    assert f.data["complete"] is False and f.data["truncated"] is False and f.data["dropped_rows"] == 1


async def test_ccxt_funding_range_dropped_row_does_not_end_a_trusted_walk_early(state):
    class FromSince(FakeEx):  # like Binance: a page is the first `limit` rows from `since`; its first row is broken
        rows = [{"timestamp": START + i * H8, "fundingRate": 0.0001} for i in range(250)]

        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            page = [dict(r) for r in self.rows if r["timestamp"] >= (since or 0)][:limit]
            if page:
                page[0]["timestamp"] = "abc"
            return page

    rt = _rt(state, FromSince)
    try:
        # okx's page is 100 rows: pages of 100 / 100 / 50 raw rows, one broken in each; a short page is judged by the
        # raw size, so the 99 valid rows of a full page do not end the walk
        f = await rt.exchanges.funding_rate_history_range("okx", "BTC/USDT:USDT", START, START + 300 * H8, trust_short_page=True)
    finally:
        await rt.aclose()
    assert f.evidence.args["pages"] == 3
    assert len(f.data["rows"]) == 247
    assert f.data["complete"] is False and f.evidence.args["dropped_rows"] == 3


async def test_perps_funding_history_warns_on_dropped_ccxt_rows(state):
    from mcp import Client

    from conftest import NOW
    from tradedesk.server import build_server

    rt = _rt(state, _corrupt("abc"))
    rt.now = lambda: NOW
    try:
        srv, _ = build_server(rt)
        async with Client(srv) as c:
            res = await c.call_tool("perps_funding_history", {"base": "BTC", "exchanges": ["binanceusdm"], "include_hyperliquid": False})
        env = res.structured_content
        assert env["ok"]
        assert any("binanceusdm funding history incomplete" in w and "malformed timestamp" in w for w in env["warnings"])
        ev = next(e for e in env["evidence"] if e["provider"] == "ccxt:binanceusdm" and e["tool"] == "fetch_funding_rate_history")
        assert ev["args"]["dropped_rows"] == 1
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
    rt = Runtime(Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=""), transports=tr, exchange_factory=FakeEx)
    try:
        f = await rt.hyperliquid.funding_history_range("BTC", START, START + 1200 * hour)
        assert f.data["complete"] is True
        assert len(f.data["rows"]) == 1200
        assert f.evidence.args["pages"] == 3
    finally:
        await rt.aclose()


async def test_hyperliquid_funding_range_malformed_time_does_not_truncate(state):
    # one row with a non-integer time on the first full page must not end the walk at 499 rows with complete=True
    hour = 3_600_000
    total = [{"coin": "BTC", "fundingRate": "0.00001", "premium": "0", "time": START + i * hour} for i in range(1200)]
    bad = dict(total[10], time="abc")
    flag = dict(total[20], time=True)  # a bool is not a timestamp either

    def hl(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body["type"] != "fundingHistory":
            return httpx.Response(200, json=[])
        start, end = body["startTime"], body.get("endTime", 10**15)
        page = [r for r in total if start <= r["time"] <= end][:500]
        return httpx.Response(200, json=[bad if r is total[10] else flag if r is total[20] else r for r in page])

    tr = make_transports(state)
    tr["hyperliquid"] = httpx.MockTransport(hl)
    rt = Runtime(Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=""), transports=tr, exchange_factory=FakeEx)
    try:
        f = await rt.hyperliquid.funding_history_range("BTC", START, START + 1200 * hour)
        times = [x["time"] for x in f.data["rows"]]
        assert len(times) == 1198 and times[-1] == START + 1199 * hour
        assert all(isinstance(t, int) and not isinstance(t, bool) for t in times)
        assert f.data["complete"] is False
        assert f.evidence.args["dropped_rows"] == 2
        assert f.evidence.args["pages"] == 3
    finally:
        await rt.aclose()


async def test_hyperliquid_funding_range_full_page_without_valid_time_is_incomplete(state):
    def hl(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body["type"] != "fundingHistory":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"coin": "BTC", "fundingRate": "0.00001", "time": "abc"}] * 500)

    tr = make_transports(state)
    tr["hyperliquid"] = httpx.MockTransport(hl)
    rt = Runtime(Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=""), transports=tr, exchange_factory=FakeEx)
    try:
        f = await rt.hyperliquid.funding_history_range("BTC", START, START + 1200 * 3_600_000)
        assert f.data == {"rows": [], "complete": False}
        assert f.evidence.args["pages"] == 1
    finally:
        await rt.aclose()
