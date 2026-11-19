"""Compact default answers (BACKLOG «великі відповіді за замовчуванням», standing card items 379, 381, 382, 390, 405, 406).

The default call of `market_flow`, `hyperliquid_funding_history`, `funding_spread_scan`, `funding_carry_scan`,
`market_markets`, `polymarket_options_gap`, `market_ohlcv`, `cycle_regime`, `polymarket_search`, `market_tickers`,
`bybit_option_chain`, `demo_status` and `system_health` (without the collector) stays small (<= 8 000 characters of JSON) so it does not flood a chat or an
agent's context; the full per-bar / per-payment / per-candle series comes only with `include_series=true`, the full scan
(every row, the per-venue breakdown, per-fetch evidence), every full market / ticker row, every day of the cycle history,
every full search result or every health field with `include_details=true`. Fakes only (tests/conftest.py, the captured
market_flow responses of 2026-10-28, the Binance daily rows of 2026-10-28, the Gamma search answer of 2026-11-19).
No network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import ccxt
import httpx
from mcp import Client

from conftest import NOW, FakeEx, State, make_transports
from test_bybit_option_chain import chain_rt
from test_cycle_regime import FX_NOW as CYCLE_FX_NOW
from test_cycle_regime import DownEx as CycleDownEx
from test_cycle_regime import FixtureEx as CycleFixtureEx
from test_cycle_regime import _down as _cycle_down
from test_cycle_regime import _fx_handler as _cycle_fx
from test_cycle_regime import _rt as _cycle_rt
from test_demo_status import V9 as DEMO_V9
from test_demo_status import _row as _demo_row
from test_demo_status import demo_envs, demo_rows
from test_demo_status import seed as demo_seed
from test_market_flow import _flow_rt
from test_pm_gap import END_ABOVE, _book, _market, iso
from test_server import EXPECTED_TOOLS, call
from tradedesk.analytics import checkpoint as cp
from tradedesk.collector.scheduler import PeriodicJob, Scheduler
from tradedesk.config import Settings
from tradedesk.providers.liquidations import StreamStats
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools.pm_gap import COMPACT_FIELDS
from tradedesk.tools.system import JOB_NO_FAILURES, PROVIDER_NO_ERRORS, PROVIDER_UNUSED

MAX_DEFAULT_CHARS = 8_000
FIXTURES = Path(__file__).parent / "fixtures"


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


# cycle_regime (item 405): the real Binance responses of 28.10 (tests/fixtures/cycle_regime_btc_1d_2026-10-28.json), the
# default call (BTC, 90 days of history) about as long as the live one of 19.11 (12 443 characters before this change).
async def _cycle_envs(*args: dict, spot=_cycle_fx, futures=_cycle_fx, ex=CycleFixtureEx) -> list[dict]:
    rt = _cycle_rt(spot=spot, futures=futures, ex=ex)
    rt.now = lambda: CYCLE_FX_NOW
    try:
        srv, _ = build_server(rt)
        return [await call(srv, "cycle_regime", **a) for a in args]
    finally:
        await rt.aclose()


async def test_cycle_regime_default_is_compact_and_details_are_opt_in():
    default, full, short = await _cycle_envs({}, {"include_details": True}, {"history_days": 5})
    assert default["ok"] and full["ok"] and short["ok"] and not default["missing"]
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    assert _size(full) > MAX_DEFAULT_CHARS
    d, f = default["data"], full["data"]
    # the latest 10 of 90 days; the summary covers all 90 either way (the counts of test_cycle_regime's fixture test)
    assert len(f["history"]) == 90 and d["history"] == f["history"][-10:]
    assert d["history_summary"] == f["history_summary"]
    s = f["history_summary"]
    assert s["state_days"] == {"accumulation": 50, "growth": 5, "decline": 35} and s["days"] == 90
    assert (s["first_date"], s["last_date"]) == ("2026-07-30", "2026-10-27") == (f["history"][0]["date"], d["history"][-1]["date"])
    # transitions without their changes; closest (= the first transition) keeps them in full
    assert len(f["transitions"]) == 4 and all(t["changes"] for t in f["transitions"])
    assert d["transitions"] == [{k: v for k, v in t.items() if k != "changes"} for t in f["transitions"]]
    assert d["closest"] == f["closest"] == f["transitions"][0]
    same = {"history", "transitions"}
    assert {k: v for k, v in d.items() if k not in same} == {k: v for k, v in f.items() if k not in same}
    # one evidence item per provider call already (each paginated range is one item): nothing to merge
    calls = [(e["provider"], e["tool"]) for e in default["evidence"]]
    assert len(calls) == len(set(calls)) == 4 and sorted(calls) == sorted((e["provider"], e["tool"]) for e in full["evidence"])
    # the context / not-backtested caveats stay in the compact answer
    assert any("not a signal" in w and "NOT backtested" in w for w in default["warnings"])
    assert any("compact answer: the latest 10 of 90 days of history" in w and "transitions" in w for w in default["warnings"])
    assert not any("compact answer" in w for w in full["warnings"])
    # ten days or fewer: every day listed, only the transitions are cut
    assert len(short["data"]["history"]) == 5 and short["data"]["history_summary"]["days"] == 5
    assert [w for w in short["warnings"] if "compact answer" in w] == [
        "compact answer: transitions without their changes (closest keeps them); include_details=true for all"
    ]


async def test_cycle_regime_compact_still_reports_a_provider_failure():
    (env,) = await _cycle_envs({}, futures=_cycle_down, ex=CycleDownEx)  # perp, premium index and funding down
    assert env["ok"] and {m["field"] for m in env["missing"]} == {"perp_klines", "premium_index", "funding"}
    assert env["data"]["state"] is None and env["data"]["transitions"] == [] and len(env["data"]["history"]) == 10
    days = env["data"]["history_summary"]["state_days"]  # a day the price alone cannot decide is counted as undecided
    assert sum(days.values()) == 90 and days["undecided"] > 0 and env["data"]["history"][-1]["state"] is None
    assert _size(env) <= MAX_DEFAULT_CHARS, _size(env)
    (env,) = await _cycle_envs({}, spot=_cycle_down)  # no spot: no answer at all, the envelope still comes back
    assert env["ok"] is False and env["data"] is None and "spot_klines" in {m["field"] for m in env["missing"]}


# polymarket_search (item 405): the real Gamma /public-search?q=bitcoin answer of 19.11 (5 events, 115 markets, trimmed to
# the fields the tool reads); the default call (limit 20) about as long as the live one (12 2xx characters before).
PM_SEARCH_FX = json.loads((FIXTURES / "polymarket_public_search_bitcoin_2026-11-19.json").read_text(encoding="utf-8"))


def _search_rt(state: State, payload: dict | None = None, status: int = 200) -> Runtime:
    tr = make_transports(state)
    body = payload or {k: v for k, v in PM_SEARCH_FX.items() if not k.startswith("_")}
    tr["polymarket:gamma"] = httpx.MockTransport(lambda req: httpx.Response(status, json=body))
    rt = Runtime(Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=""), transports=tr, exchange_factory=FakeEx)
    rt.now = lambda: NOW
    return rt


async def _search_envs(rt: Runtime, *args: dict) -> list[dict]:
    try:
        srv, _ = build_server(rt)
        return [await call(srv, "polymarket_search", query="bitcoin", **a) for a in args]
    finally:
        await rt.aclose()


async def test_polymarket_search_default_is_compact_and_details_are_opt_in(state):
    default, full, wide, wide_full = await _search_envs(
        _search_rt(state), {}, {"include_details": True}, {"limit": 100}, {"limit": 100, "include_details": True}
    )
    assert default["ok"] and full["ok"] and wide["ok"] and not default["missing"]
    for env in (default, wide):
        assert _size(env) <= MAX_DEFAULT_CHARS, _size(env)
    assert _size(full) > MAX_DEFAULT_CHARS
    rows = full["data"]["markets"]
    assert len(rows) == full["data"]["count"] == default["data"]["count"] == 20 and wide_full["data"]["count"] > 20
    assert (default["data"]["shown"], full["data"]["shown"], wide["data"]["shown"]) == (10, 20, 10)
    # the compact rows are the first 10 full rows without slug and token ids: same ids, prices, volumes and flags
    slim = [
        {k: v for k, v in x.items() if k != "slug"} | {"outcomes": [{k: v for k, v in o.items() if k != "token_id"} for o in x["outcomes"]]}
        for x in rows[:10]
    ]
    assert default["data"]["markets"] == slim and wide["data"]["markets"] == slim
    assert all(o["token_id"] for x in rows for o in x["outcomes"])  # the full rows keep the token ids
    assert default["evidence"][0]["tool"] == "public-search" and len(default["evidence"]) == 1  # one fetch: nothing to merge
    assert any("compact answer: first 10 of 20 markets" in w for w in default["warnings"])
    assert any("compact answer: first 10 of " in w for w in wide["warnings"])
    assert not any("compact answer" in w for w in full["warnings"] + wide_full["warnings"])


async def test_polymarket_search_compact_cuts_long_questions(state):
    long_q = "Will the price of Bitcoin, as reported by the resolution source named in the rules, " * 4 + "close above $100k?"
    market = {**PM_SEARCH_FX["events"][0]["markets"][0], "question": long_q, "active": True, "closed": False}
    payload = {"events": [{"id": "E1", "markets": [market] * 12}], "pagination": {}}
    default, full = await _search_envs(_search_rt(state, payload), {}, {"include_details": True})
    assert len(long_q) > 300 and default["data"]["count"] == full["data"]["count"] == 12
    assert all(len(x["question"]) == 160 and long_q.startswith(x["question"][:-1]) for x in default["data"]["markets"])
    assert all(x["question"] == long_q for x in full["data"]["markets"])
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)


async def test_polymarket_search_compact_still_reports_a_provider_failure(state):
    (env,) = await _search_envs(_search_rt(state, status=502), {})
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["provider"]) for m in env["missing"]] == [("search", "polymarket:gamma")]


# market_tickers (item 406): 60 USDT perps with fields of the live length (the live default call on binanceusdm, limit 50,
# was 10 969 characters on 19.11, ~209 per row).
TICKER_BASES = [f"TK{i:02d}" for i in range(60)]


class _ManyTickersEx(FakeEx):
    async def fetch_tickers(self, symbols=None):
        out = {}
        for i, base in enumerate(TICKER_BASES):
            sym = f"{base}/USDT:USDT"
            last = round(0.0173 * (i + 1) ** 1.7 * 1.000731, 6)
            out[sym] = {
                "symbol": sym,
                "last": last,
                "bid": round(last * 0.99993, 6),
                "ask": round(last * 1.00007, 6),
                "high": round(last * 1.0412, 6),
                "low": round(last * 0.9617, 6),
                "baseVolume": round(91_234_567.123 / (i + 1), 3),
                "quoteVolume": round(3_456_789_012.3456 / (i + 1) ** 1.3, 4),
                "percentage": round(((-1) ** i) * (0.317 + 0.4123 * i), 3),
                "timestamp": 1791637668000 + 17 * i,
            }
        return {k: v for k, v in out.items() if not symbols or k in symbols}


async def test_market_tickers_default_is_compact_and_details_are_opt_in(state):
    rt = _spread_rt(state, _ManyTickersEx)
    try:
        srv, _ = build_server(rt)
        default = await call(srv, "market_tickers", exchange="binanceusdm")  # the call a model makes: limit 50
        full = await call(srv, "market_tickers", exchange="binanceusdm", include_details=True)
        few = await call(srv, "market_tickers", exchange="binanceusdm", symbols=["TK01/USDT:USDT", "TK02/USDT:USDT"])
    finally:
        await rt.aclose()
    assert default["ok"] and full["ok"] and few["ok"]
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    assert _size(full) > MAX_DEFAULT_CHARS
    rows = full["data"]["tickers"]
    assert len(rows) == full["data"]["returned"] == default["data"]["returned"] == 50
    assert full["data"]["count"] == default["data"]["count"] == 60
    # every returned symbol by default, in volume order; the top 10 full rows unchanged; the same movers (of all 60)
    assert default["data"]["symbols"] == [x["symbol"] for x in rows]
    assert default["data"]["tickers"] == rows[:10]
    assert default["data"]["top_movers"] == full["data"]["top_movers"]
    assert [x["symbol"] for x in full["data"]["top_movers"]] == ["TK59/USDT:USDT", "TK58/USDT:USDT", "TK57/USDT:USDT"]
    assert "symbols" not in full["data"]
    assert any("compact answer: full rows for the top 10 of 50 returned tickers" in w for w in default["warnings"])
    assert not any("compact answer" in w for w in full["warnings"])
    # ten rows or fewer: every full row, no compact warning
    assert len(few["data"]["tickers"]) == len(few["data"]["symbols"]) == 2
    assert not any("compact answer" in w for w in few["warnings"])


async def test_market_tickers_compact_still_reports_a_provider_failure(server):
    env = await call(server, "market_tickers", exchange="okx")  # the fake okx answers fetch_tickers with a 429
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["provider"], m["reason"]) for m in env["missing"]] == [("tickers", "ccxt:okx", "rate_limited")]


# system_health (item 406): the compact report leaves out only implied values, so the full one is rebuilt from it.
def _expand_health(data: dict) -> dict:
    """The full system_health `data` rebuilt from the compact one (without `tools`, which becomes a count)."""
    h = data["health"]
    providers = {n: dict(PROVIDER_UNUSED) for n in h["unknown"]}
    providers |= {n: {**PROVIDER_NO_ERRORS, **p} for n, p in h["providers"].items()}
    jobs = None
    if data["collector"] is not None:
        jobs = []
        for j in data["collector"]:
            out = {**JOB_NO_FAILURES, "successes": j["runs"] - j["failures"], **j}
            if "rows_dropped_total" not in j:
                out |= {"rows_dropped_total": 0, "last_rows_dropped": None if j["runs"] == 0 else 0}
            jobs.append(out)
    rest = {k: v for k, v in data.items() if k not in ("health", "collector", "tool_count")}
    return rest | {"health": {"summary": h["summary"], "providers": dict(sorted(providers.items()))}, "collector": jobs}


def _without_tools(data: dict) -> dict:
    return {k: v for k, v in data.items() if k != "tools"}


async def test_system_health_default_is_compact_and_lossless(server):
    # the audit's setting (14.11: 9.1k characters): no collector, every provider registered, some of them called
    for name, args in (("deribit_vol_surface", {}), ("perps_funding_compare", {"coin": "BTC"}), ("market_tickers", {"exchange": "okx"})):
        await call(server, name, **args)
    default = await call(server, "system_health")
    full = await call(server, "system_health", include_details=True)
    probe = await call(server, "system_health", probe=True)
    assert default["ok"] and full["ok"] and probe["ok"]
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    d, f = default["data"], full["data"]
    assert d["tool_count"] == len(f["tools"]) == len(EXPECTED_TOOLS) and "tools" not in d
    assert _expand_health(d) == _without_tools(f)  # nothing but implied values left out
    provs = f["health"]["providers"]
    assert set(d["health"]["unknown"]) == {n for n, p in provs.items() if p["calls"] == 0} != set()
    assert d["health"]["providers"]["ccxt:okx"] == provs["ccxt:okx"]  # a provider with a failure keeps its full row
    assert d["health"]["summary"] == f["health"]["summary"]
    assert any("compact answer (not a fault)" in w for w in default["warnings"])
    # include_details and probe=true: the full report as before, no compact note
    assert set(f) == set(probe["data"]) == {"version", "health", "tools", "store", "collector"}
    assert not any("compact answer" in w for w in full["warnings"] + probe["warnings"])
    assert all(set(p) == set(PROVIDER_UNUSED) for p in probe["data"]["health"]["providers"].values())


# The live collector of 19.11: 23 periodic jobs and 3 liquidation streams (system_health was 19 096 characters with it).
LIVE_JOBS = [
    ("atm_term", 300), ("funding", 3600), ("basis", 300), ("retention", 86400), ("traders", 86400),
    ("trader_positions", 3600), ("supply", 86400), ("paper_carry", 600), ("assistant_snapshot", 600), ("pm_gap", 3600),
    ("assistant_paper", 3600), ("plan_resolve", 3600), ("forecast_log", 3600), ("radar_archive", 3600),
    ("option_xvenue", 60), ("coinalyze_history", 3600), ("tradfi_spread", 60), ("hl_oi_cap", 600),
    ("flow_snapshot", 600), ("cycle_regime_log", 300), ("coingecko_trending", 600), ("option_legs", 600), ("alerts", 60),
    ("liquidations:binance", 5), ("liquidations:bybit", 5), ("liquidations:okx", 5),
]  # fmt: skip


async def _never(_rt) -> int:
    await asyncio.Event().wait()  # the scheduler starts with the session: keep the metrics as set below
    return 0


def _live_scheduler(rt: Runtime) -> Scheduler:
    jobs = [PeriodicJob(name=n, period_seconds=p, fn=_never) for n, p in LIVE_JOBS]
    for i, j in enumerate(jobs):
        m = j.metrics
        m.runs = m.successes = 1 + 7 * i
        m.last_run_ts, m.last_duration_ms = 1795097219.683238 - 13.7 * i, 189.30315971374512 * (i + 1)
        m.last_rows_written, m.last_rows_dropped = 24 * i, 0
        if j.name.startswith("liquidations:"):
            m.stream = StreamStats(connected=True, connects=7, reconnects=2, failures=3, frames=158, rows=38)
            m.stream.last_failure = "ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout; no close frame received"
            m.stream.last_failure_ts, m.stream.last_connect_ts = 1795095766.399628, 1795096926.733353
            m.stream.last_message_ts, m.stream.last_row_ts = 1795097269.720114, 1795097207.876428
            m.stream.connected_since = m.stream.last_connect_ts
    # one job failed once and then recovered; one dropped malformed rows
    bad = jobs[1].metrics
    bad.failures, bad.successes = 1, bad.runs - 1
    bad.last_failure, bad.last_failure_ts = "TimeoutError: binanceusdm fetch_funding_rate_history", 1795094515.4
    jobs[2].metrics.last_rows_dropped = jobs[2].metrics.rows_dropped_total = 3
    return Scheduler(rt, jobs)


async def test_system_health_compact_keeps_collector_diagnostics(state, monkeypatch):
    rt = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=":memory:", collector_enabled=True),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    names = sorted(rt.health.snapshot()["providers"])
    for i, name in enumerate(names[:19]):  # 19 providers called (2 with failures), the rest never: as live on 19.11
        rt.health.ok(name, 100.0 + 37.3 * i)
    rt.health.error(names[3], "backend_error", 'HTTP 503: {"error":"request timed out","code":"request_timeout"}')
    rt.health.error(names[5], "data_unavailable", "HTTP 404: <html><head><title>404 Not Found</title></head></html>")
    rt.health.ok(names[5], 787.1)
    sched = _live_scheduler(rt)
    monkeypatch.setattr("tradedesk.collector.build_default_scheduler", lambda _rt: sched)
    srv, _ = build_server(rt)
    try:
        async with Client(srv) as c:  # one session: the metrics stay put between the calls
            default = _payload(await c.call_tool("system_health", {}))
            full = _payload(await c.call_tool("system_health", {"include_details": True}))
    finally:
        await sched.stop()
        await rt.aclose()
    d, f = default["data"], full["data"]
    assert _expand_health(d) == _without_tools(f)  # lossless
    assert len(d["collector"]) == len(f["collector"]) == len(LIVE_JOBS)
    # collector[].stream (what standing item 8a reads) is the same object in both reports
    assert [j["stream"] for j in d["collector"]] == [j["stream"] for j in f["collector"]]
    rows = {j["name"]: j for j in d["collector"]}
    assert rows["liquidations:bybit"]["stream"]["failures"] == 3 and "keepalive" in rows["liquidations:bybit"]["stream"]["last_failure"]
    # the failed job keeps its failure fields, the dropping job its counts; a healthy job keeps its timing and rows
    assert rows["funding"]["last_failure"].startswith("TimeoutError") and rows["funding"]["last_error"] is None
    assert (rows["basis"]["last_rows_dropped"], rows["basis"]["rows_dropped_total"]) == (3, 3)
    assert set(rows["atm_term"]) == {
        "name",
        "period_seconds",
        "runs",
        "failures",
        "last_run_ts",
        "last_duration_ms",
        "last_rows_written",
        "stream",
    }
    assert d["health"]["providers"][names[3]]["last_detail"].startswith("HTTP 503")  # failures keep their full rows
    assert d["health"]["providers"][names[5]]["status"] == "ok" and d["health"]["providers"][names[5]]["failures"] == 1
    # about half the full report (9 734 of 18 580 here); still above 8 000 with 26 jobs, because every collector counter
    # that is not implied is kept: the 8 000 bound holds without the collector (the test above), not with it
    assert _size(default) <= 10_000 and _size(default) < 0.55 * _size(full), (_size(default), _size(full))


def _payload(res) -> dict:
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


async def test_system_health_compact_still_reports_a_provider_failure(server, state):
    state.deribit_status = 503
    env = await call(server, "deribit_vol_surface")
    assert env["ok"] is False and env["missing"][0]["provider"] == "deribit"
    h = await call(server, "system_health")
    full = await call(server, "system_health", include_details=True)
    deribit = h["data"]["health"]["providers"]["deribit"]
    assert deribit == full["data"]["health"]["providers"]["deribit"]  # the full row: status, reason, detail
    assert deribit["status"] == "down" and deribit["consecutive_failures"] >= 1 and deribit["last_reason"] in ("backend_error", "network")
    assert h["data"]["health"]["summary"]["down"] >= 1 and _size(h) <= MAX_DEFAULT_CHARS


async def test_bybit_option_chain_default_is_compact_and_details_are_opt_in():
    rt = chain_rt()
    srv, _ = build_server(rt)
    default = await call(srv, "bybit_option_chain")  # 3 nearest expiries, ATM +- 3 strikes
    full = await call(srv, "bybit_option_chain", include_details=True)  # every expiry and strike of the sample
    await rt.aclose()
    assert default["ok"] and full["ok"]
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    assert _size(full) > MAX_DEFAULT_CHARS
    assert len(default["data"]["expiries"]) == 3 and len(full["data"]["expiries"]) == 4
    # the per-expiry summary is the same either way; only the strike rows and their fields differ
    strip = [{k: v for k, v in e.items() if k != "strikes"} for e in default["data"]["expiries"]]
    assert strip == [{k: v for k, v in e.items() if k != "strikes"} for e in full["data"]["expiries"][:3]]


# demo_status (item 409): 40 open positions and 240 closed trades with notes on every setup (the live store of 19.11 had
# 23 open and 232 closed), so the default answer is bounded by its own cut, not by a small sample.
async def test_demo_status_default_is_compact_and_details_are_opt_in(tmp_path):
    rows = [{**x, "link_id": f"{x['link_id']}-{k}"} for k in range(7) for x in demo_rows() if x["status"] == "closed"]
    rows += [_demo_row(f"op{i}", s.setup, "open", DEMO_V9 + i * 3_600_000) for i, s in enumerate(cp.DEMO * 5)][:40]
    db = str(tmp_path / "demo.sqlite")
    await demo_seed(db, rows)
    default, full = await demo_envs(db, {}, {"include_details": True})
    assert default["ok"] and full["ok"]
    assert _size(default) <= MAX_DEFAULT_CHARS, _size(default)
    assert _size(full) > MAX_DEFAULT_CHARS
    assert default["data"]["open_count"] == full["data"]["open_count"] == len(full["data"]["open"]) == 40
    assert len(default["data"]["open"]) == 10 and len(full["data"]["closed_recent"]) == 50
    # the per-setup numbers, the S12 rule and the notes are the same either way; only lists and per-setup text differ
    slim = [{k: v for k, v in s.items() if k not in ("name", "source", "notes")} for s in full["data"]["setups"]]
    for s in slim:
        s["band"] = {k: v for k, v in s["band"].items() if k != "rolling"}
        if "fill" in s:
            s["fill"] = {k: v for k, v in s["fill"].items() if k != "notes"}
    assert default["data"]["setups"] == slim
    assert default["data"]["s12_stop_rule"] == full["data"]["s12_stop_rule"] and default["data"]["notes"] == full["data"]["notes"]
