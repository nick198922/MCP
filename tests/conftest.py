"""Shared test doubles: fake Deribit / Hyperliquid HTTP transports and fake ccxt exchanges."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import ccxt
import httpx
import pytest

from tradedesk.analytics.options import black76_price
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

UTC = timezone.utc
NOW = datetime(2026, 10, 10, 13, 7, tzinfo=UTC)
SPOT = 82_750.0

# real rows (fetched 2026-10-10) for the futures curve
FUTURES = [
    {"instrument_name": "BTC-27NOV26", "mark_price": 83355.29, "estimated_delivery_price": 82757.52, "open_interest": 37508140, "volume_usd": 1698320.0, "base_currency": "BTC", "quote_currency": "USD"},
    {"instrument_name": "BTC-24SEP27", "mark_price": 87040.56, "estimated_delivery_price": 82757.52, "open_interest": 4820540, "volume_usd": 146840.0, "base_currency": "BTC", "quote_currency": "USD"},
    {"instrument_name": "BTC-30OCT26", "mark_price": 83023.43, "estimated_delivery_price": 82757.52, "open_interest": 137297990, "volume_usd": 7596310.0, "base_currency": "BTC", "quote_currency": "USD"},
    {"instrument_name": "BTC-PERPETUAL", "mark_price": 82779.62, "estimated_delivery_price": 82757.52, "open_interest": 831497380, "volume_usd": 1.0e8, "funding_8h": 0.0001, "current_funding": 0.0, "base_currency": "BTC", "quote_currency": "USD"},
]

SMILE = dict(base=0.40, skew=-0.25, curv=1.2)
EXPIRIES = [(datetime(2026, 10, 30, 8, tzinfo=UTC), 1.002), (datetime(2026, 11, 27, 8, tzinfo=UTC), 1.006), (datetime(2027, 3, 26, 8, tzinfo=UTC), 1.024)]


def option_book() -> list[dict]:
    out = []
    for exp, fwd_mult in EXPIRIES:
        F = SPOT * fwd_mult
        T = (exp - NOW).total_seconds() / (365 * 86400)
        tok = f"{exp.day}{exp.strftime('%b').upper()}{exp.year % 100:02d}"
        for k in range(50_000, 130_001, 5_000):
            x = math.log(k / F)
            iv = SMILE["base"] + SMILE["skew"] * x + SMILE["curv"] * x * x
            for is_call in (True, False):
                price_usd = black76_price(F, k, T, iv, is_call)
                out.append({
                    "instrument_name": f"BTC-{tok}-{k}-{'C' if is_call else 'P'}",
                    "mark_iv": iv * 100, "mark_price": price_usd / F, "underlying_price": F,
                    "estimated_delivery_price": SPOT, "open_interest": 20.0 if abs(x) < 0.15 else 3.0,
                    "volume": 1.0, "base_currency": "BTC", "quote_currency": "BTC",
                })
    return out


def hl_payload() -> list:
    names = [("BTC", 40, "0.0000125", "82700", "82750", "1500", "9e9"), ("ETH", 25, "-0.00002", "2400", "2410", "40000", "3e9"), ("DOGE", 10, "0.00005", "0.2", "0.21", "1e7", "5e8")]
    meta = {"universe": [{"name": n, "szDecimals": 3, "maxLeverage": lev} for n, lev, *_ in names]}
    ctxs = [{"funding": f, "openInterest": oi, "prevDayPx": prev, "markPx": mark, "oraclePx": mark, "premium": "0.0001", "dayNtlVlm": vol}
            for _, _, f, prev, mark, oi, vol in names]
    return [meta, ctxs]


class State:
    def __init__(self) -> None:
        self.deribit_status = 200
        self.calls: list[str] = []


def make_transports(state: State) -> dict:
    def deribit(req: httpx.Request) -> httpx.Response:
        state.calls.append(req.url.path)
        if state.deribit_status != 200:
            return httpx.Response(state.deribit_status, text="down")
        kind = req.url.params.get("kind")
        if req.url.path.endswith("get_book_summary_by_currency"):
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": option_book() if kind == "option" else FUTURES})
        return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"code": 13020, "message": "instrument_not_found"}})

    def hyperliquid(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body["type"] == "metaAndAssetCtxs":
            return httpx.Response(200, json=hl_payload())
        return httpx.Response(200, json=[])

    return {"deribit": httpx.MockTransport(deribit), "hyperliquid": httpx.MockTransport(hyperliquid)}


class FakeEx:
    def __init__(self, ex_id: str) -> None:
        self.id = ex_id
        self.has = {"fetchOpenInterest": ex_id != "okx"}

    async def fetch_funding_rate(self, symbol):
        if self.id == "okx":
            raise ccxt.RateLimitExceeded("okx 429 too many requests")
        rate = {"binanceusdm": 0.0001, "bybit": 0.00008}[self.id]
        return {"symbol": symbol, "fundingRate": rate, "interval": "8h", "markPrice": 82760.0, "indexPrice": 82750.0, "nextFundingTimestamp": 1791648000000}

    async def fetch_open_interest(self, symbol):
        return {"openInterestAmount": 1000.0, "openInterestValue": 82_750_000.0 if self.id == "bybit" else 120_000_000.0}

    async def fetch_ticker(self, symbol):
        if self.id == "bybit":
            raise ccxt.AuthenticationError("bad key")
        return {"symbol": symbol, "last": 82750.0, "bid": 82749.5, "ask": 82750.5, "high": 84000.0, "low": 81000.0, "baseVolume": 1e5, "quoteVolume": 8e9, "percentage": 1.2, "timestamp": 1791637668000}

    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=100):
        price, rows = 80000.0, []
        for i in range(limit):
            price *= 1 + (0.004 if i % 2 else -0.003)
            rows.append([1791000000000 + i * 3600_000, price, price * 1.001, price * 0.999, price, 10.0])
        return rows

    async def fetch_order_book(self, symbol, limit=100):
        bids = [[82750 - i * 5.0, 2.0] for i in range(limit)]
        asks = [[82751 + i * 5.0, 2.0] for i in range(limit)]
        return {"bids": bids, "asks": asks}

    async def close(self):
        pass


@pytest.fixture
def state() -> State:
    return State()


@pytest.fixture
def rt(state: State) -> Runtime:
    runtime = Runtime(Settings(http_retries=1, cache_scale=0.0), transports=make_transports(state), exchange_factory=FakeEx)
    runtime.now = lambda: NOW
    return runtime


@pytest.fixture
def server(rt: Runtime):
    srv, _ = build_server(rt)
    return srv
