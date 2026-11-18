"""Compact default answers (BACKLOG «великі відповіді за замовчуванням», standing card items 379, 381 and 382).

The default call of `market_flow`, `hyperliquid_funding_history`, `funding_spread_scan`, `funding_carry_scan` and
`market_markets` stays small (<= 8 000 characters of JSON) so it does not flood a chat or an agent's context; the full
per-bar / per-payment series comes only with `include_series=true`, the full scan (every row, the per-venue breakdown,
per-fetch evidence) or every full market row with `include_details=true`. Fakes only (tests/conftest.py, the captured
market_flow responses of 2026-10-28). No network.
"""

from __future__ import annotations

import json

import ccxt

from conftest import NOW, FakeEx, State, make_transports
from test_market_flow import _flow_rt
from test_server import call
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

MAX_DEFAULT_CHARS = 8_000


def _size(env: dict) -> int:
    return len(json.dumps(env, ensure_ascii=False))


async def test_market_flow_default_is_compact_and_series_is_opt_in():
    rt = _flow_rt()
    srv, _ = build_server(rt)
    default = await call(srv, "market_flow", base="BTC")  # 1h, 7 days: the call a model makes without arguments
    full = await call(srv, "market_flow", base="BTC", include_series=True)
    await rt.aclose()
    assert default["ok"] and full["ok"]
    assert "series" not in default["data"] and default["data"]["summary"]["24h"] is not None
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    assert len(full["data"]["series"]) == 7 * 24  # every closed 1h bar of the window
    assert {k: v for k, v in full["data"].items() if k != "series"} == default["data"]  # same summaries either way
    assert _size(full) > MAX_DEFAULT_CHARS


async def test_hyperliquid_funding_history_default_is_compact_and_entries_are_opt_in(server):
    default = await call(server, "hyperliquid_funding_history", coin="BTC")  # 30 days of hourly payments
    full = await call(server, "hyperliquid_funding_history", coin="BTC", include_series=True)
    assert default["ok"] and full["ok"]
    assert "entries" not in default["data"] and default["data"]["stats"]["count"] > 0
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    entries = full["data"]["entries"]
    assert len(entries) == full["data"]["stats"]["count"] == default["data"]["stats"]["count"]
    assert all(set(e) == {"ts_ms", "rate"} for e in entries)
    assert full["data"]["stats"] == default["data"]["stats"]


# funding_spread_scan: 22 USDT perps on both venues, like the live default scan of 18.11 (20 candidates, ~60 per-symbol
# fetches). Tickers from that scan; rates, marks and caps are made up but of the live size (4h / 8h funding, marks with
# 4-8 significant digits) so the JSON is about as long as the live one per row.
SPREAD_BASES = [
    "ZKC", "INIT", "BARD", "ZIL", "IRYS", "C", "ZKP", "ALPINE", "ZORA", "CTR", "FLUX",
    "LA", "PHAROS", "HUMA", "AIO", "ARC", "BSV", "API3", "WIF", "1000PEPE", "ORDI", "TIA",
]  # fmt: skip


class _SpreadEx(FakeEx):
    async def fetch_funding_rates(self, symbols=None):
        bybit = self.id == "bybit"
        out = {}
        for i, base in enumerate(SPREAD_BASES):
            sym = f"{base}/USDT:USDT"
            rate = (-0.00071 if bybit else 0.00033) * (1 + i / 9) + 0.0000137 * i
            mark = round(0.0173 * (i + 1) ** 1.7 * (1.00031 if bybit else 1.0), 8)
            iv = "4h" if bybit else None  # Binance's bulk call has no interval: fetch_funding_intervals supplies it
            out[sym] = {"symbol": sym, "fundingRate": rate, "interval": iv, "markPrice": mark, "nextFundingTimestamp": None}
        return out

    async def fetch_funding_intervals(self, symbols=None):
        return {
            f"{b}/USDT:USDT": {"symbol": f"{b}/USDT:USDT", "interval": "8h" if i % 3 else "4h", "info": {"adjustedFundingRateCap": "0.02"}}
            for i, b in enumerate(SPREAD_BASES)
        }

    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
        k = 1 + SPREAD_BASES.index(symbol.split("/")[0]) / 7  # a different trailing spread per base: ~7..26 % APR
        rows = await super().fetch_funding_rate_history(symbol, since, limit, params)
        low = 0.00004 * k if self.id == "bybit" else 0.0
        return [{**x, "fundingRate": x["fundingRate"] * k + 0.0000031 * k * k - low} for x in rows]


def _spread_rt(state: State, ex: type[FakeEx] | None = None) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=ex or _SpreadEx,
    )
    rt.now = lambda: NOW
    return rt


async def test_funding_spread_scan_default_is_compact_and_details_are_opt_in(state):
    rt = _spread_rt(state)
    try:
        srv, _ = build_server(rt)
        default = await call(srv, "funding_spread_scan")  # the call a model makes without arguments: 20 candidates
        full = await call(srv, "funding_spread_scan", include_details=True)
    finally:
        await rt.aclose()
    assert default["ok"] and full["ok"], (default["missing"], full["missing"])
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    assert _size(full) > MAX_DEFAULT_CHARS
    rows = full["data"]["rows"]
    assert len(rows) == full["data"]["summary"]["scanned"] == 20
    assert all(set(x["venues"]) == {"binanceusdm", "bybit"} for x in rows)
    # the compact rows are the top 10 full rows (sorted by trailing spread) without `venues`; same numbers and summary
    assert default["data"]["rows"] == [{k: v for k, v in x.items() if k != "venues"} for x in rows[:10]]
    assert {k: v for k, v in default["data"].items() if k != "rows"} == {k: v for k, v in full["data"].items() if k != "rows"}
    assert "IRYS" in default["data"]["summary"]["tradable_now"]  # the summary covers every scanned base, not only the top 10
    assert any("compact answer: top 10 of 20 rows" in w for w in default["warnings"])
    assert not any("compact answer" in w for w in full["warnings"])
    # evidence: one item per fetch in full, one per (provider, call) by default, with the fetch count kept
    full_calls = [(e["provider"], e["tool"]) for e in full["evidence"]]
    merged = {(e["provider"], e["tool"]): e for e in default["evidence"]}
    assert len(merged) == len(default["evidence"]) == len(set(full_calls)) < len(full_calls)
    for key, e in merged.items():
        n = full_calls.count(key)
        assert e["args"].get("fetches", 1) == n and e["fetched_at"]
    hist = merged[("ccxt:bybit", "fetch_funding_rate_history")]["args"]
    assert hist["fetches"] == hist["symbols"] == 20 and "symbol" not in hist and "since_ms" in hist


class _SpreadExTiaDown(_SpreadEx):
    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
        if self.id == "bybit" and symbol == "TIA/USDT:USDT":
            raise ccxt.NetworkError("bybit down for TIA")
        return await super().fetch_funding_rate_history(symbol, since, limit, params)


async def test_funding_spread_scan_compact_still_reports_a_provider_failure(state):
    rt = _spread_rt(state, _SpreadExTiaDown)
    try:
        srv, _ = build_server(rt)
        env = await call(srv, "funding_spread_scan")
    finally:
        await rt.aclose()
    # the failed leg goes to `missing` (merged evidence does not hide it); the scan answers from the other bases
    assert env["ok"], env["missing"]
    assert [m["field"] for m in env["missing"]] == ["funding_history:bybit:TIA/USDT:USDT"]
    assert env["data"]["summary"]["scanned"] == 19 and "TIA" not in {x["base"] for x in env["data"]["rows"]}
    assert len(env["data"]["rows"]) == 10 and _size(env) <= MAX_DEFAULT_CHARS


# funding_carry_scan (item 382): the same 22 bases, each a Binance USDT perp with a Binance USDT spot market, so the
# default scan (20 candidates, require_spot) makes 20 history fetches like the live one.
class _CarryEx(_SpreadEx):
    async def load_markets(self):
        out = {}
        for base in SPREAD_BASES:
            for sym, kind in ((f"{base}/USDT", "spot"), (f"{base}/USDT:USDT", "swap")):
                out[sym] = {"id": f"{base}USDT", "symbol": sym, "base": base, "quote": "USDT", "type": kind, "active": True}
        return out


async def test_funding_carry_scan_default_is_compact_and_details_are_opt_in(state):
    rt = _spread_rt(state, _CarryEx)
    try:
        srv, _ = build_server(rt)
        default = await call(srv, "funding_carry_scan")  # the call a model makes without arguments: 20 candidates
        full = await call(srv, "funding_carry_scan", include_details=True)
    finally:
        await rt.aclose()
    assert default["ok"] and full["ok"], (default["missing"], full["missing"])
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    assert _size(full) > MAX_DEFAULT_CHARS
    rows = full["data"]["rows"]
    assert len(rows) == full["data"]["summary"]["scanned"] == 20
    # the compact rows are the top 10 full rows (sorted by trailing APR) unchanged; same summary and thresholds
    assert default["data"]["rows"] == rows[:10]
    assert {k: v for k, v in default["data"].items() if k != "rows"} == {k: v for k, v in full["data"].items() if k != "rows"}
    above = default["data"]["summary"]["above_entry"]
    assert len(above) > 10 and set(above) - {x["base"] for x in default["data"]["rows"]}  # summary covers every row
    assert any("compact answer: top 10 of 20 rows" in w for w in default["warnings"])
    assert not any("compact answer" in w for w in full["warnings"])
    full_calls = [(e["provider"], e["tool"]) for e in full["evidence"]]
    merged = {(e["provider"], e["tool"]): e for e in default["evidence"]}
    assert len(merged) == len(default["evidence"]) == len(set(full_calls)) < len(full_calls)
    for key, e in merged.items():
        assert e["args"].get("fetches", 1) == full_calls.count(key) and e["fetched_at"]
    hist = merged[("ccxt:binanceusdm", "fetch_funding_rate_history")]["args"]
    assert hist["fetches"] == hist["symbols"] == 20 and "symbol" not in hist


class _CarryExTiaDown(_CarryEx):
    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
        if self.id == "binanceusdm" and symbol == "TIA/USDT:USDT":
            raise ccxt.NetworkError("binanceusdm down for TIA")
        return await super().fetch_funding_rate_history(symbol, since, limit, params)


async def test_funding_carry_scan_compact_still_reports_a_provider_failure(state):
    rt = _spread_rt(state, _CarryExTiaDown)
    try:
        srv, _ = build_server(rt)
        env = await call(srv, "funding_carry_scan")
    finally:
        await rt.aclose()
    # the failed history goes to `missing` (merged evidence does not hide it); TIA keeps a row from its current funding
    assert env["ok"], env["missing"]
    assert [m["field"] for m in env["missing"]] == ["funding_history:TIA/USDT:USDT"]
    assert env["data"]["summary"]["scanned"] == 20 and "TIA" not in {x["base"] for x in env["data"]["rows"]}
    assert len(env["data"]["rows"]) == 10 and _size(env) <= MAX_DEFAULT_CHARS


# market_markets (item 382): 120 USDT perps plus a dated future for every tenth base, slim fields as ccxt gives them for
# binanceusdm, so the default call (no query, limit 100) returns about as many characters per row as the live one.
MARKET_BASES = [f"T{i:03d}" for i in range(120)]


class _ManyMarketsEx(FakeEx):
    async def load_markets(self):
        out = {}
        for i, base in enumerate(MARKET_BASES):
            sym = f"{base}/USDT:USDT"
            row = {"id": f"{base}USDT", "symbol": sym, "base": base, "quote": "USDT", "settle": "USDT", "type": "swap"}
            out[sym] = {**row, "active": True, "linear": True, "inverse": False, "contractSize": 1.0}
            if i % 10 == 0:
                fut = f"{sym}-261226"
                out[fut] = {
                    **out[sym],
                    "id": f"{base}USDT_261226",
                    "symbol": fut,
                    "type": "future",
                    "expiry": 1798272000000,
                    "expiryDatetime": "2026-12-26T08:00:00.000Z",
                }
        return out


async def test_market_markets_default_is_compact_and_details_are_opt_in(state):
    rt = _spread_rt(state, _ManyMarketsEx)
    try:
        srv, _ = build_server(rt)
        default = await call(srv, "market_markets", exchange="binanceusdm")  # no filter, limit 100
        full = await call(srv, "market_markets", exchange="binanceusdm", include_details=True)
        narrow = await call(srv, "market_markets", exchange="binanceusdm", query="T00", market_type="swap")
    finally:
        await rt.aclose()
    assert default["ok"] and full["ok"] and narrow["ok"]
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    assert _size(full) > MAX_DEFAULT_CHARS
    rows = full["data"]["markets"]
    assert len(rows) == 100 and full["data"]["matched"] == default["data"]["matched"] == 132
    # every returned symbol by default, in the order of the full rows; the first 10 full rows unchanged
    assert default["data"]["symbols"] == [x["symbol"] for x in rows]
    assert default["data"]["markets"] == rows[:10]
    assert default["data"]["total_markets"] == full["data"]["total_markets"] == 132
    assert "symbols" not in full["data"]
    assert any("compact answer: full rows for the first 10 of 100" in w for w in default["warnings"])
    assert not any("compact answer" in w for w in full["warnings"])
    assert any("matched 132 markets; returning first 100" in w for w in default["warnings"] + full["warnings"])
    # ten matches or fewer: every full row, no compact warning
    assert len(narrow["data"]["markets"]) == len(narrow["data"]["symbols"]) == 10
    assert not any("compact answer" in w for w in narrow["warnings"])


class _MarketsDownEx(_ManyMarketsEx):
    async def load_markets(self):
        raise ccxt.NetworkError("binanceusdm markets down")


async def test_market_markets_compact_still_reports_a_provider_failure(state):
    rt = _spread_rt(state, _MarketsDownEx)
    try:
        srv, _ = build_server(rt)
        env = await call(srv, "market_markets", exchange="binanceusdm")
    finally:
        await rt.aclose()
    assert env["ok"] is False and env["data"] is None
    assert [m["field"] for m in env["missing"]] == ["markets"]
