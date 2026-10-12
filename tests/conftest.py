"""Shared test doubles: fake Deribit / Hyperliquid HTTP transports and fake ccxt exchanges."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import ccxt
import httpx
import pytest

from tradedesk.analytics.options import black76_price
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

UTC = UTC
NOW = datetime(2026, 10, 10, 13, 7, tzinfo=UTC)
SPOT = 82_750.0

# real rows (fetched 2026-10-10) for the futures curve
FUTURES = [
    {
        "instrument_name": "BTC-27NOV26",
        "mark_price": 83355.29,
        "estimated_delivery_price": 82757.52,
        "open_interest": 37508140,
        "volume_usd": 1698320.0,
        "base_currency": "BTC",
        "quote_currency": "USD",
    },
    {
        "instrument_name": "BTC-24SEP27",
        "mark_price": 87040.56,
        "estimated_delivery_price": 82757.52,
        "open_interest": 4820540,
        "volume_usd": 146840.0,
        "base_currency": "BTC",
        "quote_currency": "USD",
    },
    {
        "instrument_name": "BTC-30OCT26",
        "mark_price": 83023.43,
        "estimated_delivery_price": 82757.52,
        "open_interest": 137297990,
        "volume_usd": 7596310.0,
        "base_currency": "BTC",
        "quote_currency": "USD",
    },
    {
        "instrument_name": "BTC-PERPETUAL",
        "mark_price": 82779.62,
        "estimated_delivery_price": 82757.52,
        "open_interest": 831497380,
        "volume_usd": 1.0e8,
        "funding_8h": 0.0001,
        "current_funding": 0.0,
        "base_currency": "BTC",
        "quote_currency": "USD",
    },
]

SMILE = dict(base=0.40, skew=-0.25, curv=1.2)
EXPIRIES = [
    (datetime(2026, 10, 30, 8, tzinfo=UTC), 1.002),
    (datetime(2026, 11, 27, 8, tzinfo=UTC), 1.006),
    (datetime(2027, 3, 26, 8, tzinfo=UTC), 1.024),
]


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
                out.append(
                    {
                        "instrument_name": f"BTC-{tok}-{k}-{'C' if is_call else 'P'}",
                        "mark_iv": iv * 100,
                        "mark_price": price_usd / F,
                        "underlying_price": F,
                        "estimated_delivery_price": SPOT,
                        "open_interest": 20.0 if abs(x) < 0.15 else 3.0,
                        "volume": 1.0,
                        "base_currency": "BTC",
                        "quote_currency": "BTC",
                    }
                )
    return out


def hl_payload() -> list:
    names = [
        ("BTC", 40, "0.0000125", "82700", "82750", "1500", "9e9"),
        ("ETH", 25, "-0.00002", "2400", "2410", "40000", "3e9"),
        ("DOGE", 10, "0.00005", "0.2", "0.21", "1e7", "5e8"),
    ]
    meta = {"universe": [{"name": n, "szDecimals": 3, "maxLeverage": lev} for n, lev, *_ in names]}
    ctxs = [
        {"funding": f, "openInterest": oi, "prevDayPx": prev, "markPx": mark, "oraclePx": mark, "premium": "0.0001", "dayNtlVlm": vol}
        for _, _, f, prev, mark, oi, vol in names
    ]
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
        if req.url.path.endswith("get_last_trades_by_currency"):
            trades = []
            for i in range(10):
                is_block = i < 2  # first two are block prints
                amount = 10.0 + i * 5
                price = 0.05 + i * 0.001  # BTC-native
                trades.append(
                    {
                        "trade_id": f"T{i}",
                        "instrument_name": f"BTC-30OCT26-{80000 + i * 1000}-C",
                        "direction": "buy" if i % 2 == 0 else "sell",
                        "amount": amount,
                        "price": price,
                        "mark_price": price * 0.99,
                        "index_price": 82750.0,
                        "iv": 45.0 + i,
                        "timestamp": 1791637620000 - i * 60_000,
                        "block_trade_id": f"BLK{i}" if is_block else None,
                    }
                )
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"trades": trades, "has_more": False}})
        if req.url.path.endswith("get_volatility_index_data"):
            start_ms = int(req.url.params.get("start_timestamp"))
            end_ms = int(req.url.params.get("end_timestamp"))
            step_ms = int(req.url.params.get("resolution")) * 1000
            rows = []
            t = start_ms
            v = 45.0
            while t <= end_ms:
                v += 0.1  # slow upward drift
                rows.append([t, v, v + 0.5, v - 0.5, v + 0.2])
                t += step_ms
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"data": rows, "continuation": None}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"code": 13020, "message": "instrument_not_found"}})

    def hyperliquid(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        t = body["type"]
        if t == "metaAndAssetCtxs":
            return httpx.Response(200, json=hl_payload())
        if t == "l2Book":
            levels = [
                [{"px": str(82750.0 - i), "sz": str(1.0 + i * 0.1), "n": 3 + i} for i in range(20)],
                [{"px": str(82751.0 + i), "sz": str(1.0 + i * 0.1), "n": 2 + i} for i in range(20)],
            ]
            return httpx.Response(200, json={"coin": body["coin"], "time": 1791640000000, "levels": levels})
        if t == "recentTrades":
            rows = []
            for i in range(15):
                side = "B" if i % 3 != 2 else "A"
                px = 82750.0 + i * 0.1
                sz = 0.1 if i != 0 else 2.0
                rows.append(
                    {
                        "coin": body["coin"],
                        "side": side,
                        "px": str(px),
                        "sz": str(sz),
                        "time": 1791640000000 + i * 500,
                        "hash": f"0x{i:064x}",
                        "tid": i,
                    }
                )
            return httpx.Response(200, json=rows)
        if t == "candleSnapshot":
            req_ = body["req"]
            start = req_.get("startTime", 1791540000000)
            rows = []
            price = 82000.0
            for i in range(24):
                price *= 1 + (0.005 if i % 2 else -0.004)
                t0 = start + i * 3_600_000
                rows.append(
                    {
                        "t": t0,
                        "T": t0 + 3_599_999,
                        "s": req_["coin"],
                        "i": req_["interval"],
                        "o": str(price * 0.999),
                        "c": str(price),
                        "h": str(price * 1.002),
                        "l": str(price * 0.998),
                        "v": "1500.0",
                        "n": 1000,
                    }
                )
            return httpx.Response(200, json=rows)
        if t == "fundingHistory":
            coin = body["coin"]
            start = body.get("startTime", 1791040000000)
            rows = []
            for i in range(168):  # 7 days hourly
                rate = (1e-5 if i % 2 == 0 else -5e-6) + (i * 1e-7)  # alternating sign, drifting
                rows.append({"coin": coin, "fundingRate": str(rate), "premium": "0", "time": start + i * 3_600_000})
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json=[])

    def binance_futures(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("globalLongShortAccountRatio"):
            rows = [
                {
                    "symbol": "BTCUSDT",
                    "longAccount": f"{0.60 + i * 0.001:.4f}",
                    "shortAccount": f"{0.40 - i * 0.001:.4f}",
                    "longShortRatio": f"{(0.60 + i * 0.001) / (0.40 - i * 0.001):.4f}",
                    "timestamp": 1791640800000 + i * 3_600_000,
                }
                for i in range(48)
            ]
            return httpx.Response(200, json=rows)
        if path.endswith("openInterestHist"):
            rows = [
                {
                    "symbol": "BTCUSDT",
                    "sumOpenInterest": f"{92000 + i * 10}",
                    "sumOpenInterestValue": f"{(92000 + i * 10) * 82800}",
                    "CMCCirculatingSupply": "20095900",
                    "timestamp": 1791640800000 + i * 3_600_000,
                }
                for i in range(48)
            ]
            return httpx.Response(200, json=rows)
        return httpx.Response(404, text="not mocked")

    def bybit(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("account-ratio"):
            rows = [
                {
                    "symbol": "BTCUSDT",
                    "buyRatio": f"{0.62 + i * 0.001:.4f}",
                    "sellRatio": f"{0.38 - i * 0.001:.4f}",
                    "timestamp": str(1791640800000 + i * 3_600_000),
                }
                for i in range(48)
            ]
            return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": rows}})
        if path.endswith("open-interest"):
            rows = [
                {
                    "openInterest": f"{55000 + i * 20}",
                    "singleOpenInterest": f"{(55000 + i * 20) / 2}",
                    "timestamp": str(1791640800000 + i * 3_600_000),
                }
                for i in range(48)
            ]
            return httpx.Response(
                200, json={"retCode": 0, "retMsg": "OK", "result": {"symbol": "BTCUSDT", "category": "linear", "list": rows}}
            )
        return httpx.Response(404, text="not mocked")

    def polymarket_gamma(req: httpx.Request) -> httpx.Response:
        sample_market = {
            "id": "516700",
            "slug": "will-btc-close-above-100k-by-2026-12-31",
            "question": "Will BTC close above $100k by 2026-12-31?",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.62","0.40"]',
            "clobTokenIds": '["TOK_YES","TOK_NO"]',
            "endDate": "2026-12-31T23:59:00Z",
            "active": True,
            "closed": False,
            "volumeNum": 1_250_000.0,
            "liquidityNum": 85_000.0,
        }
        if req.url.path == "/public-search":
            return httpx.Response(200, json={"events": [{"id": "E1", "markets": [sample_market]}], "pagination": {}})
        if req.url.path == "/markets":
            market = {
                "id": "516700",
                "slug": "will-btc-close-above-100k-by-2026-12-31",
                "question": "Will BTC close above $100k by 2026-12-31?",
                "outcomes": '["Yes","No"]',
                "outcomePrices": '["0.62","0.40"]',
                "clobTokenIds": '["TOK_YES","TOK_NO"]',
                "endDate": "2026-12-31T23:59:00Z",
                "active": True,
                "closed": False,
                "volumeNum": 1_250_000.0,
                "liquidityNum": 85_000.0,
            }
            return httpx.Response(200, json=[market])
        if req.url.path.startswith("/markets/"):
            mid = req.url.path[len("/markets/") :]
            return httpx.Response(
                200,
                json={
                    "id": mid,
                    "slug": "will-btc-close-above-100k-by-2026-12-31",
                    "question": "Will BTC close above $100k by 2026-12-31?",
                    "outcomes": '["Yes","No"]',
                    "outcomePrices": '["0.62","0.40"]',
                    "clobTokenIds": '["TOK_YES","TOK_NO"]',
                    "endDate": "2026-12-31T23:59:00Z",
                    "active": True,
                    "closed": False,
                    "volumeNum": 1_250_000.0,
                    "liquidityNum": 85_000.0,
                },
            )
        return httpx.Response(404, text="not mocked")

    def polymarket_clob(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/book":
            tok = req.url.params.get("token_id") or ""
            # Yes mid 0.62 / No mid 0.38
            if tok == "TOK_YES":
                bids = [{"price": "0.618", "size": "500"}, {"price": "0.615", "size": "300"}]
                asks = [{"price": "0.622", "size": "400"}, {"price": "0.625", "size": "200"}]
            else:
                bids = [{"price": "0.378", "size": "450"}, {"price": "0.375", "size": "250"}]
                asks = [{"price": "0.382", "size": "350"}, {"price": "0.385", "size": "180"}]
            return httpx.Response(200, json={"market": tok, "bids": bids, "asks": asks})
        if req.url.path == "/prices-history":
            tok = req.url.params.get("market") or ""
            base = 0.55 if tok == "TOK_YES" else 0.45
            now_ms = 1791637620_000
            hour = 3_600_000
            rows = [{"t": now_ms - (23 - i) * hour, "p": base + 0.003 * i} for i in range(24)]
            return httpx.Response(200, json={"history": rows})
        return httpx.Response(404, text="not mocked")

    def coingecko(req: httpx.Request) -> httpx.Response:
        path = req.url.path.split("/api/v3", 1)[-1]
        if path.startswith("/coins/markets"):
            rows = [
                {
                    "id": "bitcoin",
                    "symbol": "btc",
                    "name": "Bitcoin",
                    "current_price": 82750.0,
                    "market_cap": 1_630_000_000_000,
                    "fully_diluted_valuation": 1_740_000_000_000,
                    "total_volume": 45_000_000_000,
                    "circulating_supply": 19_700_000,
                    "market_cap_rank": 1,
                    "price_change_percentage_24h": 1.2,
                    "price_change_percentage_24h_in_currency": 1.2,
                    "price_change_percentage_7d_in_currency": -3.1,
                    "price_change_percentage_30d_in_currency": 8.5,
                },
                {
                    "id": "ethereum",
                    "symbol": "eth",
                    "name": "Ethereum",
                    "current_price": 2410.0,
                    "market_cap": 290_000_000_000,
                    "fully_diluted_valuation": 290_000_000_000,
                    "total_volume": 18_000_000_000,
                    "circulating_supply": 120_300_000,
                    "market_cap_rank": 2,
                    "price_change_percentage_24h": -3.4,
                    "price_change_percentage_24h_in_currency": -3.4,
                    "price_change_percentage_7d_in_currency": -5.2,
                    "price_change_percentage_30d_in_currency": 2.0,
                },
            ]
            return httpx.Response(200, json=rows)
        if path.startswith("/coins/"):
            coin_id = path.split("/coins/")[1].split("/")[0]
            md = {
                "current_price": {"usd": 82750.0},
                "market_cap": {"usd": 1_630_000_000_000},
                "fully_diluted_valuation": {"usd": 1_740_000_000_000},
                "total_volume": {"usd": 45_000_000_000},
                "ath": {"usd": 108000.0},
                "ath_change_percentage": {"usd": -23.4},
                "atl": {"usd": 67.81},
                "atl_change_percentage": {"usd": 122000.0},
                "circulating_supply": 19_700_000,
                "total_supply": 19_700_000,
                "max_supply": 21_000_000,
                "price_change_percentage_24h": 1.2,
                "price_change_percentage_7d": -3.1,
                "price_change_percentage_30d": 8.5,
                "market_cap_rank": 1,
                "last_updated": "2026-10-10T13:07:00.000Z",
            }
            return httpx.Response(200, json={"id": coin_id, "symbol": coin_id[:3], "name": coin_id.capitalize(), "market_data": md})
        if path == "/global":
            data = {
                "active_cryptocurrencies": 14_500,
                "upcoming_icos": 0,
                "ongoing_icos": 50,
                "ended_icos": 3_500,
                "markets": 1_050,
                "total_market_cap": {"usd": 2_500_000_000_000, "btc": 30_000_000},
                "total_volume": {"usd": 100_000_000_000, "btc": 1_200_000},
                "market_cap_percentage": {"btc": 55.5, "eth": 15.2, "sol": 3.1},
                "market_cap_change_percentage_24h_usd": 0.75,
                "updated_at": 1_791_637_620,
            }
            return httpx.Response(200, json={"data": data})
        return httpx.Response(404, text="not mocked")

    def alternative_me(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/fng/":
            with open(Path(__file__).parent / "fixtures" / "fear_greed_sample.json") as f:
                body = json.load(f)
            limit = int(req.url.params.get("limit") or len(body["data"]))
            body["data"] = body["data"][:limit]
            return httpx.Response(200, json=body)
        return httpx.Response(404, text="not mocked")

    return {
        "deribit": httpx.MockTransport(deribit),
        "hyperliquid": httpx.MockTransport(hyperliquid),
        "binance_futures": httpx.MockTransport(binance_futures),
        "bybit": httpx.MockTransport(bybit),
        "alternative_me": httpx.MockTransport(alternative_me),
        "polymarket:gamma": httpx.MockTransport(polymarket_gamma),
        "polymarket:clob": httpx.MockTransport(polymarket_clob),
        "coingecko": httpx.MockTransport(coingecko),
    }


class FakeEx:
    def __init__(self, ex_id: str) -> None:
        self.id = ex_id
        self.has = {
            "fetchOpenInterest": ex_id != "okx",
            "fetchTickers": ex_id != "kraken",
            "fetchFundingRateHistory": ex_id != "coinbase",
        }

    async def fetch_funding_rate(self, symbol):
        if self.id == "okx":
            raise ccxt.RateLimitExceeded("okx 429 too many requests")
        rate = {"binanceusdm": 0.0001, "bybit": 0.00008}[self.id]
        return {
            "symbol": symbol,
            "fundingRate": rate,
            "interval": "8h",
            "markPrice": 82760.0,
            "indexPrice": 82750.0,
            "nextFundingTimestamp": 1791648000000,
        }

    async def fetch_open_interest(self, symbol):
        return {"openInterestAmount": 1000.0, "openInterestValue": 82_750_000.0 if self.id == "bybit" else 120_000_000.0}

    async def fetch_ticker(self, symbol):
        if self.id == "bybit":
            raise ccxt.AuthenticationError("bad key")
        return {
            "symbol": symbol,
            "last": 82750.0,
            "bid": 82749.5,
            "ask": 82750.5,
            "high": 84000.0,
            "low": 81000.0,
            "baseVolume": 1e5,
            "quoteVolume": 8e9,
            "percentage": 1.2,
            "timestamp": 1791637668000,
        }

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

    async def fetch_funding_rates(self, symbols=None):
        if self.id == "okx":
            raise ccxt.RateLimitExceeded("okx 429 too many requests")
        rows = {"BTC/USDT:USDT": 0.0001, "ETH/USDT:USDT": 0.0003, "SOL/USDT:USDT": -0.0001, "XAU/USDT:USDT": 0.002, "BTC/USDT": 0.5}
        return {
            sym: {"symbol": sym, "fundingRate": rate, "interval": "8h", "nextFundingTimestamp": 1791648000000} for sym, rate in rows.items()
        }

    async def fetch_funding_rate_history(self, symbol, since=None, limit=500):
        if self.id == "okx":
            raise ccxt.RateLimitExceeded("okx 429 too many requests")
        end = 1791637200000  # 2026-10-10 12:00 UTC — anchor before NOW used in tests
        interval_ms = 8 * 3_600_000
        base_rate = {"binanceusdm": 0.0001, "bybit": 0.00008}.get(self.id, 0.00005)
        rows = []
        for i in range(90):
            ts = end - (89 - i) * interval_ms  # 90 entries, 8h apart
            if since is not None and ts < since:
                continue
            rate = base_rate + ((-1) ** i) * 0.00001 * (i % 7)  # noisy sign flips
            rows.append({"symbol": symbol, "fundingRate": rate, "timestamp": ts, "datetime": None})
            if len(rows) >= limit:
                break
        return rows

    async def fetch_tickers(self, symbols=None):
        if self.id == "kraken":
            raise ccxt.NotSupported("kraken has no fetchTickers")
        if self.id == "okx":
            raise ccxt.RateLimitExceeded("okx 429 too many requests")
        catalog = {
            "BTC/USDT:USDT": {
                "last": 82750.0,
                "bid": 82749.5,
                "ask": 82750.5,
                "high": 84000.0,
                "low": 81000.0,
                "baseVolume": 1e5,
                "quoteVolume": 8e9,
                "percentage": 1.2,
                "timestamp": 1791637668000,
            },
            "ETH/USDT:USDT": {
                "last": 2410.0,
                "bid": 2409.8,
                "ask": 2410.2,
                "high": 2500.0,
                "low": 2380.0,
                "baseVolume": 2e6,
                "quoteVolume": 4.8e9,
                "percentage": -3.4,
                "timestamp": 1791637668000,
            },
            "SOL/USDT:USDT": {
                "last": 180.5,
                "bid": 180.4,
                "ask": 180.6,
                "high": 190.0,
                "low": 170.0,
                "baseVolume": 5e5,
                "quoteVolume": 1e8,
                "percentage": 8.1,
                "timestamp": 1791637668000,
            },
            "DOGE/USDT:USDT": {
                "last": 0.21,
                "bid": 0.209,
                "ask": 0.211,
                "high": 0.22,
                "low": 0.19,
                "baseVolume": 1e9,
                "quoteVolume": 2e8,
                "percentage": -5.0,
                "timestamp": 1791637668000,
            },
        }
        wanted = symbols or list(catalog)
        return {s: {"symbol": s, **catalog[s]} for s in wanted if s in catalog}

    async def load_markets(self):
        base = {
            "BTC/USDT": {
                "id": "BTCUSDT",
                "symbol": "BTC/USDT",
                "base": "BTC",
                "quote": "USDT",
                "settle": "USDT",
                "type": "spot",
                "active": True,
                "linear": None,
                "inverse": None,
                "contractSize": None,
            },
            "BTC/USDT:USDT": {
                "id": "BTCUSDT",
                "symbol": "BTC/USDT:USDT",
                "base": "BTC",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "active": True,
                "linear": True,
                "inverse": False,
                "contractSize": 1.0,
            },
            "BTC/USD:BTC-271231": {
                "id": "BTCUSD_271231",
                "symbol": "BTC/USD:BTC-271231",
                "base": "BTC",
                "quote": "USD",
                "settle": "BTC",
                "type": "future",
                "active": True,
                "linear": False,
                "inverse": True,
                "contractSize": 100.0,
                "expiry": 1830297600000,
                "expiryDatetime": "2027-12-31T08:00:00.000Z",
            },
            "ETH/USDT:USDT": {
                "id": "ETHUSDT",
                "symbol": "ETH/USDT:USDT",
                "base": "ETH",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "active": True,
                "linear": True,
                "inverse": False,
                "contractSize": 1.0,
            },
            "XAU/USDT:USDT": {
                "id": "XAUUSDT",
                "symbol": "XAU/USDT:USDT",
                "base": "XAU",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "active": True,
                "linear": True,
                "inverse": False,
                "contractSize": 1.0,
                "info": {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "COMMODITY"},
            },
            "DEAD/USDT": {
                "id": "DEADUSDT",
                "symbol": "DEAD/USDT",
                "base": "DEAD",
                "quote": "USDT",
                "settle": None,
                "type": "spot",
                "active": False,
                "linear": None,
                "inverse": None,
                "contractSize": None,
            },
        }
        return base

    async def fetch_trades(self, symbol, since=None, limit=200):
        if self.id == "okx":
            raise ccxt.RateLimitExceeded("okx 429 too many requests")
        rows = []
        t0 = 1791637668000
        for i in range(limit):
            side = "buy" if (i % 3 != 2) else "sell"
            price = 82750.0 + (0.5 if side == "buy" else -0.5) + i * 0.01
            amount = 0.1 + (i % 7) * 0.05
            if i == 0:
                amount = 5.0  # a whale print to top the chart
                side = "buy"
            if i == 1 and limit > 1:
                amount = 4.0
                side = "sell"
            rows.append(
                {
                    "id": str(i),
                    "timestamp": t0 + i * 50,
                    "symbol": symbol,
                    "side": side,
                    "price": price,
                    "amount": amount,
                    "cost": price * amount,
                }
            )
        if self.id == "coinbase":
            for r in rows:  # some venues don't publish aggressor side
                r.pop("side", None)
        return rows

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
