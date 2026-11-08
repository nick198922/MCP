"""Guard for CLAUDE.md invariant 1: no tool raises when every provider is down.

Builds a Runtime whose every HTTP transport fails (HTTP 500, or a timeout) and whose every ccxt call raises (exchange
not available, or a request timeout), then calls each tool registered by build_server() once with minimal valid
arguments taken from its input schema. Checks, per tool:
- no exception escapes (the MCP call is not an error) and the answer is an envelope (ok / data / missing / ...);
- a tool that tried a provider reports it: ok=false or a non-empty `missing`.
A tool that made no provider call at all (calculators, the local store) only has to return an envelope.

SKIP: tools whose required arguments cannot be guessed from the schema (reason given). KNOWN: violations found and not
fixed yet (each one also in docs/BACKLOG.md); a stale entry fails the test, so fixed tools leave the list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import ccxt
import httpx
import pytest
from mcp import Client

from conftest import NOW
from tradedesk.config import Settings
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FAILURES = ("http_500", "timeout")
ENVELOPE_KEYS = {"ok", "data", "evidence", "missing", "warnings"}

# Required arguments by parameter name (shared by all tools); per-tool overrides in ARGS.
STRINGS = {
    "exchange": "binanceusdm",
    "venue": "binanceusdm",
    "symbol": "BTC/USDT:USDT",
    "coin": "BTC",
    "expiry": "27NOV26",
    "market_id": "516700",
    "token_id": "TOK_YES",
    "coin_id": "bitcoin",
    "base": "BTC",
    "chain": "ethereum",
    "address": "0x" + "1" * 40,
    "market": "0x" + "2" * 40,
    "asset": "sUSDe",
    "instrument": "BTC/USDT:USDT",
    "name": "outage-guard",
    "expr": 'funding("binanceusdm","BTC") > 0.30',
    "kind": "fomc",
    "label": "FOMC rate decision",
}
NUMBERS = {"level": 90_000.0, "barrier": 90_000.0, "days": 30.0, "iv": 0.5, "size": 1.0, "p0": 80_000.0, "l_point": 90_000.0}
ARRAYS = {
    "odds": [1.9, 1.9],
    "symbols": ["BTC/USDT:USDT"],
    "addresses": ["0x" + "1" * 40],
    "legs": [{"kind": "call", "side": "long", "size": 1, "strike": 82_000, "entry_premium": 2_000}],
}
ARGS: dict[str, dict[str, Any]] = {
    "carry_paper_open": {"long_venue": "binanceusdm", "short_venue": "bybit"},
    # Coinalyze takes a bare coin; with a dummy key (failing_runtime) the call reaches the failing transport.
    "coinalyze_funding_history": {"symbol": "BTC"},
    "coinalyze_long_short_history": {"symbol": "BTC"},
    "coinalyze_oi_history": {"symbol": "BTC"},
    "liquidations_history": {"symbol": "BTC"},
    "plan_add": {"entry": 82_750.0, "stop": 80_000.0, "target": 90_000.0, "expires_ts": int(NOW.timestamp()) + 86_400},
    "strategy_cash_and_carry": {"spot_price": 82_750.0, "futures_price": 83_500.0},
    "strategy_funding_carry": {"long_mark": 82_750.0, "short_mark": 82_760.0},
}

# Tools whose required arguments cannot be guessed: none so far. Format: {"tool": "reason"}.
SKIP: dict[str, str] = {}

# Violations found and not fixed yet (also listed in docs/BACKLOG.md). Format: {"tool": "what is wrong"}.
KNOWN: dict[str, str] = {}


class _HasAll(dict):
    """ccxt `has` map that claims every capability, so tools reach the failing call instead of a capability check."""

    def get(self, key, default=None):
        return True

    def __getitem__(self, key):
        return True


class DownExchange:
    """A ccxt exchange whose every async method raises; counts the attempts."""

    def __init__(self, ex_id: str, mode: str, calls: list[str]) -> None:
        self.id = ex_id
        self.has = _HasAll()
        self._mode = mode
        self._calls = calls

    async def close(self) -> None:
        pass

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def fail(*args, **kwargs):
            self._calls.append(f"ccxt:{self.id}.{name}")
            if self._mode == "timeout":
                raise ccxt.RequestTimeout(f"{self.id} GET https://example.invalid request timed out")
            raise ccxt.ExchangeNotAvailable(f"{self.id} GET https://example.invalid 503 Service Unavailable")

        return fail


class _AllTransports(dict):
    """Gives the same failing transport to every Http client the Runtime builds, so nothing reaches the real network."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        super().__init__({"*": transport})  # non-empty: Runtime treats an empty mapping as "no transports"
        self._transport = transport

    def get(self, key, default=None):
        return self._transport


def failing_runtime(mode: str, db_path: str, calls: list[str]) -> Runtime:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"http:{req.url.host}{req.url.path}")
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=req)
        return httpx.Response(500, text="down")

    # A dummy Coinalyze key (never a real one): without a key the tools answer `missing: config` before any request.
    settings = Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=db_path, coinalyze_api_key="outage-test")
    # Production rate limits (GoPlus 0.2 req/s, ...) would only make the failing fake calls wait in real time.
    with patch("tradedesk.core.http.TokenBucket", lambda rate, burst: TokenBucket(1_000.0, 1_000)):
        rt = Runtime(
            settings,
            transports=_AllTransports(httpx.MockTransport(handler)),
            exchange_factory=lambda ex_id: DownExchange(ex_id, mode, calls),
        )
    rt.now = lambda: NOW
    return rt


def minimal_args(tool) -> dict[str, Any]:
    schema = tool.input_schema or {}
    props = schema.get("properties", {})
    override = ARGS.get(tool.name, {})
    args: dict[str, Any] = {}
    for p in schema.get("required", []):
        spec = props.get(p, {})
        if p in override:
            args[p] = override[p]
        elif "enum" in spec:
            args[p] = spec["enum"][0]
        elif spec.get("type") == "string":
            args[p] = STRINGS[p]
        elif spec.get("type") == "integer":
            args[p] = 1
        elif spec.get("type") == "number":
            args[p] = NUMBERS.get(p, 1_000.0)
        elif spec.get("type") == "array":
            args[p] = ARRAYS[p]
        else:
            raise KeyError(f"{tool.name}.{p}: no minimal value for schema {spec}")
    return args


def as_envelope(res) -> Any:
    data = res.structured_content
    if data is None:
        try:
            data = json.loads(res.content[0].text)
        except (IndexError, AttributeError, ValueError):
            return None
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return data


@dataclass
class Outcome:
    names: set[str] = field(default_factory=set)  # every registered tool
    called: int = 0
    attempted: int = 0  # tools that made at least one provider call
    violations: dict[str, str] = field(default_factory=dict)
    leaks: list[str] = field(default_factory=list)  # requests that bypassed the failing transports


async def run_all(mode: str, db_path: str) -> Outcome:
    """Calls every registered tool once against a Runtime whose providers all fail."""
    out = Outcome()
    calls: list[str] = []

    async def no_real_network(transport, request):
        out.leaks.append(f"{request.method} {request.url.host}{request.url.path}")
        calls.append("leak")
        raise httpx.ConnectError("real network is off in this test", request=request)

    srv, _ = build_server(failing_runtime(mode, db_path, calls))
    with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", no_real_network):
        async with Client(srv) as c:
            tools = (await c.list_tools()).tools
            out.names = {t.name for t in tools}
            for tool in sorted(tools, key=lambda t: t.name):
                if tool.name in SKIP:
                    continue
                calls.clear()
                res = await c.call_tool(tool.name, minimal_args(tool))
                out.called += 1
                out.attempted += bool(calls)
                env = as_envelope(res)
                if res.is_error:
                    text = res.content[0].text if res.content else ""
                    out.violations[tool.name] = f"raised: {text[:200]}"
                elif not isinstance(env, dict) or not ENVELOPE_KEYS <= set(env):
                    out.violations[tool.name] = f"not an envelope: {str(env)[:200]}"
                elif calls and env["ok"] and not env["missing"]:
                    out.violations[tool.name] = f"ok=true and no missing after {len(calls)} failed provider calls"
    return out


@pytest.mark.parametrize("mode", FAILURES)
async def test_no_tool_raises_when_every_provider_is_down(mode, tmp_path):
    out = await run_all(mode, str(tmp_path / "outage.sqlite"))
    assert out.leaks == []  # every request went to the failing transports, none to a real venue
    assert out.called >= 100  # the guard really walked the registry
    assert out.attempted >= 50  # and the failing providers were really hit
    assert set(SKIP) - out.names == set(), "SKIP names a tool that is not registered"
    new = {name: why for name, why in out.violations.items() if name not in KNOWN}
    assert new == {}, "fix these tools with Builder.guard, or (if not clear-cut) list them in KNOWN and docs/BACKLOG.md"
    assert set(KNOWN) - set(out.violations) == set(), "these tools are fixed: remove them from KNOWN"
