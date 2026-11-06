"""End-to-end check of CLAUDE.md invariant 1: a tool never raises when its providers fail.

Every registered tool is called through a real MCP client against a server whose every HTTP transport answers 500 (or
times out) and whose every ccxt exchange raises. A tool that raises shows up as an `isError` result from the SDK; a tool
that keeps the invariant returns the envelope (`ok` true or false). The store is a fresh SQLite file in tmp_path, so the
local-journal tools run too; no keys are configured (key-gated providers report `missing: config`).
"""

from __future__ import annotations

import json
from datetime import timedelta

import ccxt
import httpx
import pytest
from mcp import Client

from conftest import NOW
from test_server import EXPECTED_TOOLS
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

# Minimal valid arguments for every tool with required parameters (tools without required parameters get {}).
_LEGS = [
    {"kind": "call", "side": "long", "size": 1.0, "strike": 83000.0, "entry_premium": 1500.0},
    {"kind": "put", "side": "long", "size": 1.0, "strike": 83000.0, "entry_premium": 1500.0},
]
_PREMIUMS = {"spot": 83000.0, "days": 20.0, "iv": 0.4}
ARGS: dict[str, dict] = {
    "assistant_paper_open": {"structure": "long_straddle"},
    "carry_paper_close": {"id": 1, "reason": "manual"},
    "carry_paper_open": {"base": "BTC", "long_venue": "binanceusdm", "short_venue": "bybit"},
    "chain_metrics": {"chain": "Ethereum"},
    "chart_markup": {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "coin_snapshot": {"coin_id": "bitcoin"},
    "coinalyze_funding_history": {"symbol": "BTC"},
    "coinalyze_long_short_history": {"symbol": "BTC"},
    "coinalyze_oi_history": {"symbol": "BTC"},
    "contract_risk": {"chain": "ethereum", "address": "0x" + "ab" * 20},
    "deribit_max_pain": {"expiry": "27NOV26"},
    "deribit_option_chain": {"expiry": "27NOV26"},
    "dex_pairs": {"addresses": ["0x" + "cd" * 20]},
    "event_study": {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "events_add": {"kind": "fomc", "label": "FOMC test"},
    "fib_retracement": {"p0": 80000.0, "l_point": 70000.0},
    "funding_zscore": {"venue": "binanceusdm"},
    "hyperliquid_book": {"coin": "BTC"},
    "hyperliquid_candles": {"coin": "BTC"},
    "hyperliquid_funding_history": {"coin": "BTC"},
    "hyperliquid_trades": {"coin": "BTC"},
    "journal_add": {"symbol": "BTC/USDT:USDT", "side": "long", "size": 0.1, "entry": 82000.0},
    "journal_close": {"id": 1, "exit": 83000.0},
    "liquidations_history": {"symbol": "BTC"},
    "market_markets": {"exchange": "binanceusdm"},
    "market_ohlcv": {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "market_orderbook": {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "market_regime": {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "market_ticker": {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "market_tickers": {"exchange": "binanceusdm"},
    "market_trades": {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "odds_devig": {"odds": [1.9, 1.9]},
    "ohlcv_history": {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "option_calc": {"forward": 83000.0, "strike": 85000.0, "days": 20.0, "iv": 0.4},
    "pendle_market": {"market": "0x" + "ef" * 20},
    "pendle_yield_curve": {"asset": "USDe"},
    "plan_add": {
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "instrument": "perp",
        "side": "long",
        "entry_type": "limit",
        "entry": 82000.0,
        "stop": 80000.0,
        "target": 86000.0,
        "expires_ts": int((NOW + timedelta(days=1)).timestamp()),
        "verdict": "wait",
    },
    "plan_link": {"id": 1, "journal_id": 1},
    "polymarket_history": {"token_id": "TOK_YES"},
    "polymarket_market": {"market_id": "516700"},
    "positions_add": {"instrument": "BTC-PERPETUAL", "kind": "perp", "side": "long", "size": 0.1},
    "positions_close": {"id": 1},
    "prob_from_options": {"expiry": "27NOV26", "level": 90000.0},
    "range_detect": {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "range_setup": {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "range_setup_scan": {"venue": "binanceusdm", "symbols": ["BTC/USDT:USDT"]},
    "rules_add": {"name": "funding hot", "expr": 'funding("binanceusdm","BTC") > 0.30'},
    "rules_disable": {"id": 1},
    "strategy_backtest_momentum": {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "strategy_cash_and_carry": {"spot_price": 83000.0, "futures_price": 84000.0, "days": 30.0, "iv": 0.4},
    "strategy_funding_carry": {"long_mark": 83000.0, "short_mark": 83010.0, "days": 30.0, "iv": 0.4},
    "strategy_iron_condor": {
        "long_put_strike": 70000.0,
        "short_put_strike": 75000.0,
        "short_call_strike": 90000.0,
        "long_call_strike": 95000.0,
        "long_put_premium": 200.0,
        "short_put_premium": 600.0,
        "short_call_premium": 600.0,
        "long_call_premium": 200.0,
        **_PREMIUMS,
    },
    "strategy_payoff": {"legs": _LEGS, **_PREMIUMS},
    "strategy_risk_reversal": {"call_strike": 90000.0, "put_strike": 75000.0, "call_premium": 800.0, "put_premium": 900.0, **_PREMIUMS},
    "strategy_scenario_matrix": {"legs": _LEGS, "spot": 83000.0},
    "strategy_straddle": {"strike": 83000.0, "call_premium": 1500.0, "put_premium": 1500.0, **_PREMIUMS},
    "strategy_strangle": {"call_strike": 90000.0, "put_strike": 75000.0, "call_premium": 800.0, "put_premium": 900.0, **_PREMIUMS},
    "touch_probability": {"spot": 83000.0, "barrier": 90000.0, "days": 20.0, "iv": 0.4},
}

# Tools not called here, with the reason. Empty: with a temp SQLite store and no keys every tool can be called.
SKIP: dict[str, str] = {}

# Tools that still raise when providers fail; each entry is listed in docs/changes/2026-11-06-tools-never-raise.md.
KNOWN_RAISERS: dict[str, str] = {}


class _AllTransports(dict):
    """Hands the same failing transport to every Http the Runtime builds, whatever its name (no real network)."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        super().__init__(any=transport)
        self._transport = transport

    def get(self, key, default=None):  # noqa: ARG002 - dict.get signature
        return self._transport


class _FailingEx:
    """A ccxt exchange whose every call raises (`has` is empty so capability checks pass through to the call)."""

    def __init__(self, ex_id: str, exc: type[Exception]) -> None:
        self.id = ex_id
        self.has: dict = {}
        self._exc = exc

    def __getattr__(self, name: str):
        async def fail(*_a, **_k):
            raise self._exc(f"{self.id} {name}: injected failure")

        return fail

    async def close(self) -> None:
        pass


def _runtime(tmp_path, mode: str) -> tuple[Runtime, list[str]]:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.url.host}{req.url.path}")
        if mode == "timeout":
            raise httpx.ReadTimeout("injected timeout", request=req)
        return httpx.Response(500, text="injected outage")

    exc = ccxt.RequestTimeout if mode == "timeout" else ccxt.ExchangeNotAvailable
    # http_retries=0: the last attempt fails the same way with or without retries, and every retry of a slow-bucket
    # provider (ForexFactory 0.5/s, CoinGecko 0.5/s) would cost real seconds.
    rt = Runtime(
        Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / "never_raise.sqlite")),
        transports=_AllTransports(httpx.MockTransport(handler)),
        exchange_factory=lambda ex_id: _FailingEx(ex_id, exc),
    )
    rt.now = lambda: NOW
    return rt, calls


async def _call_every_tool(srv) -> tuple[int, int, dict[str, str], dict[str, str]]:
    """Call each registered tool once; returns (listed, called, raised {name: error}, no_envelope {name: payload})."""
    raised: dict[str, str] = {}
    no_envelope: dict[str, str] = {}
    called = 0
    async with Client(srv) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        for name in sorted(tools):
            if name in SKIP:
                continue
            required = set(tools[name].input_schema.get("required", []))
            args = ARGS.get(name, {})
            assert required <= set(args), f"{name}: add minimal arguments for {sorted(required - set(args))} to ARGS"
            res = await c.call_tool(name, args)
            called += 1
            text = res.content[0].text if res.content else ""
            if res.is_error:  # the SDK turns an exception escaping the tool into an error result
                raised[name] = text[:300]
                continue
            data = res.structured_content
            if data is None:
                data = json.loads(text)
            if set(data) == {"result"}:
                data = data["result"]
            if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
                no_envelope[name] = str(data)[:200]
    return len(tools), called, raised, no_envelope


@pytest.mark.parametrize("mode", ["http500", "timeout"])
async def test_no_tool_raises_when_every_provider_fails(tmp_path, mode):
    rt, calls = _runtime(tmp_path, mode)
    srv, _ = build_server(rt)
    listed, called, raised, no_envelope = await _call_every_tool(srv)

    assert listed >= len(EXPECTED_TOOLS)
    assert called == listed - len(SKIP)
    assert calls, "no request reached the failing transport"
    assert set(raised) == set(KNOWN_RAISERS), f"tools that raised: {raised}"
    assert not no_envelope, f"tools without an envelope: {no_envelope}"


async def test_the_check_catches_a_tool_that_raises(tmp_path):
    """Self-check: a tool whose provider error escapes is reported, so a green run above means something."""
    rt, _ = _runtime(tmp_path, "http500")
    srv, _ = build_server(rt)

    @srv.tool(name="zz_unguarded_probe")
    async def zz_unguarded_probe() -> dict:
        await rt.deribit.http.get_json("public/get_index_price", params={"index_name": "btc_usd"})  # no Builder.guard
        return {"ok": True}

    listed, called, raised, no_envelope = await _call_every_tool(srv)
    assert set(raised) == {"zz_unguarded_probe"} | set(KNOWN_RAISERS)
    assert called == listed - len(SKIP) and not no_envelope


def test_lists_name_registered_tools_only():
    assert set(SKIP) <= EXPECTED_TOOLS and set(KNOWN_RAISERS) <= EXPECTED_TOOLS
    assert set(ARGS) <= EXPECTED_TOOLS
