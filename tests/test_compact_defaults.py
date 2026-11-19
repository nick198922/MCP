"""Compact default answers (BACKLOG «великі відповіді за замовчуванням», standing card items 379, 381, 382 and 390).

The default call of `market_flow`, `hyperliquid_funding_history`, `funding_spread_scan`, `funding_carry_scan`,
`market_markets`, `polymarket_options_gap` and `market_ohlcv` stays small (<= 8 000 characters of JSON) so it does not
flood a chat or an agent's context; the full per-bar / per-payment / per-candle series comes only with
`include_series=true`, the full scan (every row, the per-venue breakdown, per-fetch evidence) or every full market row
with `include_details=true`. Fakes only (tests/conftest.py, the captured market_flow responses of 2026-10-28). No network.
"""

from __future__ import annotations

import json

import ccxt
import httpx

from conftest import NOW, FakeEx, State, make_transports
from test_market_flow import _flow_rt
from test_pm_gap import END_ABOVE, _book, _market, iso
from test_server import call
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools.pm_gap import COMPACT_FIELDS

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


# polymarket_options_gap (item 390): 24 standard BTC "above $K on October 29" markets with strikes inside the fake
# Deribit strikes (50-130k) and deep books, so every one is listed (min_net_edge=-1) as in a busy live scan of 19.11
# (16-17 listed rows of ~730 characters, 15 evidence items).
PM_STRIKES = list(range(60_000, 108_000, 2_000))
PM_QUESTION = "Will the price of Bitcoin be above ${k:,} on October 29?"
# ~190 characters, three times a live question: the compact row cuts it to 80, so the answer's size does not follow it
PM_LONG_QUESTION = "Bitcoin price market (Binance BTC/USDT 1 minute candle, noon ET close), " * 2 + PM_QUESTION


def _pm_gamma(question: str):
    def handler(req: httpx.Request) -> httpx.Response:
        markets = [_market(f"g{k}", question.format(k=k), f"Y{k}", f"N{k}", endDate=iso(END_ABOVE)) for k in PM_STRIKES]
        event = {"id": "1", "slug": "bitcoin-above-on-october-29", "markets": markets}
        return httpx.Response(200, json={"events": [event], "next_cursor": None})  # the same event under both tags

    return handler


def _pm_clob(req: httpx.Request) -> httpx.Response:
    books = []
    for i, k in enumerate(PM_STRIKES):
        p = round(0.95 - 0.037 * i, 3)  # YES mid falls with the strike
        books.append(_book(f"Y{k}", [(round(p - 0.01, 3), 20_000)], [(round(p + 0.01, 3), 20_000)]))
        books.append(_book(f"N{k}", [(round(0.99 - p, 3), 20_000)], [(round(1.01 - p, 3), 20_000)]))
    wanted = {x["token_id"] for x in json.loads(req.content)}
    return httpx.Response(200, json=[x for x in books if x["asset_id"] in wanted])


def _pm_rt(state: State, question: str = PM_QUESTION) -> Runtime:
    tr = make_transports(state)
    tr["polymarket:gamma"] = httpx.MockTransport(_pm_gamma(question))
    tr["polymarket:clob"] = httpx.MockTransport(_pm_clob)
    rt = Runtime(Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=""), transports=tr, exchange_factory=FakeEx)
    rt.now = lambda: NOW
    return rt


PM_ARGS = {"max_days": 30, "min_net_edge": -1}  # every market of the fake scan listed: a large typical answer


async def test_polymarket_options_gap_default_is_compact_and_details_are_opt_in(state):
    rt = _pm_rt(state)
    try:
        srv, _ = build_server(rt)
        default = await call(srv, "polymarket_options_gap", **PM_ARGS)
        full = await call(srv, "polymarket_options_gap", **PM_ARGS, include_details=True)
        wide = await call(srv, "polymarket_options_gap", **PM_ARGS, include_nonstandard=True)
    finally:
        await rt.aclose()
    assert default["ok"] and full["ok"] and not default["missing"], default["missing"]
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    assert _size(full) > MAX_DEFAULT_CHARS
    rows = full["data"]["markets"]
    assert len(rows) == full["data"]["count"] == default["data"]["count"] == len(PM_STRIKES)
    # the compact rows are the top 10 full rows (by net_edge) cut to the key fields: same numbers, same counts
    # (plus `flags`, [] for these standard covered markets, in place of model_note / rules_standard / model_covered)
    assert default["data"]["markets"] == [{k: x[k] for k in COMPACT_FIELDS} | {"flags": []} for x in rows[:10]]
    # `shown` tells a reader of `data` alone that only 10 of `count` markets are listed
    assert (default["data"]["shown"], full["data"]["shown"]) == (10, len(PM_STRIKES))
    same = {"markets", "shown"}
    assert {k: v for k, v in default["data"].items() if k not in same} == {k: v for k, v in full["data"].items() if k not in same}
    assert default["data"]["underlying"]["BTC"] == {"spot": rows[0]["spot"], "dvol": rows[0]["dvol"]}
    # include_nonstandard: the same compact rows (no per-row booleans), and the answer stays small
    assert wide["data"]["markets"] == default["data"]["markets"]
    assert _size(wide) <= MAX_DEFAULT_CHARS, _size(wide)
    # the risk-neutral / no-backtest warnings stay in the compact answer
    assert any("RISK-NEUTRAL" in w and "no backtest" in w for w in default["warnings"])
    assert any(f"compact answer: top 10 of {len(PM_STRIKES)} listed markets" in w for w in default["warnings"])
    assert not any("compact answer" in w for w in full["warnings"])
    full_calls = [(e["provider"], e["tool"]) for e in full["evidence"]]
    merged = {(e["provider"], e["tool"]): e for e in default["evidence"]}
    assert len(merged) == len(default["evidence"]) == len(set(full_calls)) < len(full_calls)
    for key, e in merged.items():
        assert e["args"].get("fetches", 1) == full_calls.count(key) and e["fetched_at"]


async def test_polymarket_options_gap_compact_size_does_not_follow_long_questions(state):
    rt = _pm_rt(state, PM_LONG_QUESTION)
    try:
        srv, _ = build_server(rt)
        default = await call(srv, "polymarket_options_gap", **PM_ARGS)
        wide = await call(srv, "polymarket_options_gap", **PM_ARGS, include_nonstandard=True)
        full = await call(srv, "polymarket_options_gap", **PM_ARGS, include_details=True)
    finally:
        await rt.aclose()
    assert len(full["data"]["markets"]) == default["data"]["count"] == len(PM_STRIKES)  # the long text still parses
    for env in (default, wide):
        assert env["ok"] and _size(env) <= MAX_DEFAULT_CHARS, _size(env)
        for x in env["data"]["markets"]:
            assert len(x["question"]) == 80 and x["question"].endswith("…")
            assert PM_LONG_QUESTION.format(k=int(x["strike"])).startswith(x["question"][:-1])
    # the full row keeps the whole question
    assert all(x["question"] == PM_LONG_QUESTION.format(k=int(x["strike"])) for x in full["data"]["markets"])


async def test_polymarket_options_gap_compact_still_reports_a_provider_failure(state):
    state.deribit_status = 503
    rt = _pm_rt(state)
    try:
        srv, _ = build_server(rt)
        env = await call(srv, "polymarket_options_gap", **PM_ARGS)
    finally:
        await rt.aclose()
    # no smile, no model probability: nothing listed, and the Deribit failure is in `missing` despite merged evidence
    assert env["ok"] and env["data"]["markets"] == [] and env["data"]["with_model_probability"] == 0
    assert {m["field"] for m in env["missing"]} >= {"option_book:BTC", "option_book:ETH"}
    assert all(m["provider"] == "deribit" for m in env["missing"]) and _size(env) <= MAX_DEFAULT_CHARS


# market_ohlcv (item 390): the default 200 candles of the fake venue (floats of the live length, ~13 000 characters in all)
async def test_market_ohlcv_default_is_compact_and_series_is_opt_in(server):
    args = {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT"}  # 1h, limit 200: the call a model makes
    default = await call(server, "market_ohlcv", **args)
    full = await call(server, "market_ohlcv", **args, include_series=True)
    short = await call(server, "market_ohlcv", **args, limit=10)
    assert default["ok"] and full["ok"] and short["ok"]
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    assert _size(full) > MAX_DEFAULT_CHARS
    rows = full["data"]["rows"]
    assert len(rows) == full["data"]["summary"]["candles"] == 200
    # the latest 10 candles by default; the summary covers all 200 either way
    assert default["data"]["rows"] == rows[-10:]
    assert default["data"]["summary"] == full["data"]["summary"]
    s = full["data"]["summary"]
    assert (s["first_ts_ms"], s["last_ts_ms"]) == (rows[0][0], rows[-1][0])
    assert s["high"] == max(x[2] for x in rows) and s["low"] == min(x[3] for x in rows)
    assert any("compact answer: the latest 10 of 200 candles" in w for w in default["warnings"])
    assert not any("compact answer" in w for w in full["warnings"])
    # ten candles or fewer: every candle, no compact warning
    assert len(short["data"]["rows"]) == 10 and not any("compact answer" in w for w in short["warnings"])


async def test_market_ohlcv_compact_still_reports_a_provider_failure(server):
    env = await call(server, "market_ohlcv", exchange="binanceusdm", symbol="DOWN/USDT:USDT")
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["provider"]) for m in env["missing"]] == [("ohlcv", "ccxt:binanceusdm")]
