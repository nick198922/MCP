"""Guard for CLAUDE.md invariant 1 on absurd inputs: no tool raises when the caller sends nonsense.

A model in chat easily mixes seconds with milliseconds or sends a zero / negative / huge limit or an empty symbol. Every
tool registered by build_server() runs on the working fake transports from conftest.py (plus a local store); for each
time-, limit- and symbol-like parameter it is called once per absurd value, with the other required arguments minimal
(tests/test_tools_provider_outage.py). A value outside the parameter's schema bounds is skipped: the MCP layer rejects
it before the tool runs. Checks, per call: no exception escapes (the MCP call is not an error) and the answer is an
envelope (ok / data / missing / ...). Whether the tool answers ok=false or clamps the value is its own business.

The second test covers BACKLOG 6.18's sibling: an event whose ts_ms does not fit a datetime must not break event_study.

KNOWN: violations found and not fixed yet (each one also in docs/BACKLOG.md 6.19); a stale entry fails the test.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from mcp import Client

from conftest import NOW, FakeEx, State, make_transports
from test_tools_provider_outage import ENVELOPE_KEYS, SKIP, as_envelope, minimal_args
from tradedesk.config import Settings
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

NOW_S = int(NOW.timestamp())
HUGE_TS = 10**18  # fits SQLite INTEGER, overflows datetime both as seconds and as milliseconds

# Unix-time parameters: seconds unless the name ends in `_ms`.
TIME_PARAM = re.compile(r"(^|_)ts(_ms)?$")
TIME_S = [0, -1, NOW_S * 1000, HUGE_TS]  # NOW_S * 1000: milliseconds passed for seconds
TIME_MS = [0, -1, NOW_S, NOW_S * 1_000_000, HUGE_TS]  # seconds and microseconds passed for milliseconds
# Counts, windows and durations.
SIZE_PARAM = re.compile(r"(limit|top_n|count|depth|candidates|page|bars|points|fidelity|lookback|days|hours|minutes|weeks)")
SIZE_INT = [0, -1, 10**12]
SIZE_NUM = [0.0, -1.0, 1e12]
# Symbol-like strings (enums are skipped: the schema already pins them).
STRING_PARAMS = {
    "symbol",
    "coin",
    "base",
    "currency",
    "exchange",
    "venue",
    "expiry",
    "market_id",
    "token_id",
    "coin_id",
    "asset",
    "instrument",
    "chain",
    "address",
    "market",
    "timeframe",
    "interval",
    "period",
    "query",
    "since_date",
    "until_date",
    "date",
    "time_utc",
    "event_kind",
    "spot_venue",
    "price_venue",
    "vs_currency",
}
STRINGS = ["", "   ", "NOT/A:SYMBOL", "x" * 300]
LIST_PARAMS = {"symbols", "exchanges", "venues", "bases", "currencies", "addresses"}
LISTS: list[list[str]] = [[], [""]]

# Violations found and not fixed yet (also listed in docs/BACKLOG.md 6.19). Format: {"tool.param": "what is wrong"}.
KNOWN: dict[str, str] = {}


def _spec(raw: dict[str, Any]) -> dict[str, Any]:
    """A property's schema with an `anyOf: [T, null]` folded into one dict (bounds live on the non-null member)."""
    spec = dict(raw)
    for alt in raw.get("anyOf", []):
        if alt.get("type") != "null":
            spec.update(alt)
    return spec


def _fits(spec: dict[str, Any], value: Any) -> bool:
    """True when `value` passes the JSON-schema bounds the MCP layer checks before the tool runs."""
    if isinstance(value, str):
        return spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", len(value))
    if isinstance(value, list):
        return True
    if "minimum" in spec and value < spec["minimum"]:
        return False
    if "maximum" in spec and value > spec["maximum"]:
        return False
    if "exclusiveMinimum" in spec and value <= spec["exclusiveMinimum"]:
        return False
    return not ("exclusiveMaximum" in spec and value >= spec["exclusiveMaximum"])


def absurd_values(name: str, raw: dict[str, Any]) -> list[Any]:
    """Absurd values for one parameter, or [] when it is not time-, size- or symbol-like."""
    spec = _spec(raw)
    if "enum" in spec:
        return []
    kind = spec.get("type")
    values: list[Any]
    if kind == "integer" and TIME_PARAM.search(name):
        values = list(TIME_MS if name.endswith("_ms") else TIME_S)
    elif kind in ("integer", "number") and SIZE_PARAM.search(name):
        values = list(SIZE_INT if kind == "integer" else SIZE_NUM)
        values += [b for b in (spec.get("minimum"), spec.get("maximum")) if b is not None]  # the edges are valid input too
    elif kind == "string" and name in STRING_PARAMS:
        values = list(STRINGS)
    elif kind == "array" and name in LIST_PARAMS and "enum" not in spec.get("items", {}):
        values = list(LISTS)
    else:
        return []
    out: list[Any] = []
    for v in values:
        if v not in out and _fits(spec, v):
            out.append(v)
    return out


def working_runtime(db_path: str) -> Runtime:
    settings = Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=db_path)
    # Production rate limits (CoinGecko 0.5 req/s, ForexFactory 0.5 req/s, ...) would only make ~1000 fake calls wait.
    with patch("tradedesk.core.http.TokenBucket", lambda rate, burst: TokenBucket(1_000.0, 1_000)):
        rt = Runtime(settings, transports=make_transports(State()), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    return rt


def check(res) -> str | None:
    """None when the call returned an envelope; else what went wrong."""
    if res.is_error:
        return f"raised: {(res.content[0].text if res.content else '')[:200]}"
    env = as_envelope(res)
    if not isinstance(env, dict) or not ENVELOPE_KEYS <= set(env):
        return f"not an envelope: {str(env)[:200]}"
    return None


async def _no_real_network(transport, request):
    raise httpx.ConnectError("real network is off in this test", request=request)


async def test_no_tool_raises_on_absurd_input(tmp_path):
    calls = 0
    tools_hit: set[str] = set()
    violations: dict[str, str] = {}
    srv, _ = build_server(working_runtime(str(tmp_path / "bad-input.sqlite")))
    with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _no_real_network):
        async with Client(srv) as c:
            for tool in sorted((await c.list_tools()).tools, key=lambda t: t.name):
                if tool.name in SKIP:
                    continue
                base = minimal_args(tool)
                for param, raw in sorted((tool.input_schema or {}).get("properties", {}).items()):
                    for value in absurd_values(param, raw):
                        res = await c.call_tool(tool.name, {**base, param: value})
                        calls += 1
                        tools_hit.add(tool.name)
                        why = check(res)
                        if why is not None:
                            violations.setdefault(f"{tool.name}.{param}", f"{param}={str(value)[:40]!r}: {why}")
    assert calls >= 500  # the guard really walked the registry
    assert len(tools_hit) >= 80
    new = {key: why for key, why in violations.items() if key not in KNOWN}
    assert new == {}, "validate these inputs (invalid_input in `missing`), or list them in KNOWN and docs/BACKLOG.md 6.19"
    assert set(KNOWN) - set(violations) == set(), "these inputs are handled now: remove them from KNOWN"


@pytest.mark.parametrize("ts_ms", [HUGE_TS, 2**63 - 1, -(10**18)])
async def test_event_study_survives_event_ts_out_of_datetime_range(tmp_path, ts_ms):
    """events_add -> event_study with a ts_ms that no datetime holds: both calls answer with an envelope."""
    rt = working_runtime(str(tmp_path / "events.sqlite"))
    srv, _ = build_server(rt)
    t0 = (NOW_S - 30 * 86_400) * 1000
    bars = [[t0 + i * 3_600_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0] for i in range(-12, 13)]
    async with Client(srv) as c:
        await rt.store.init()
        await rt.store.insert_ohlcv_batch("binanceusdm", "BTC/USDT:USDT", "1h", bars)
        ok = await c.call_tool("events_add", {"kind": "fomc", "label": "real one", "ts_ms": t0})
        assert check(ok) is None and as_envelope(ok)["ok"]
        bad = await c.call_tool("events_add", {"kind": "fomc", "label": "absurd", "ts_ms": ts_ms})
        assert check(bad) is None
        env = as_envelope(bad)
        assert env["ok"] is False and env["missing"][0]["reason"] == "invalid_input"
        # A row written before events_add validated its input (or by another writer) must not break the study either.
        await rt.store.event_insert(ts=ts_ms // 1000, kind="fomc", label="legacy absurd", impact="medium", meta=None)
        study = await c.call_tool("event_study", {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT", "event_kind": "fomc"})
        assert check(study) is None
        env = as_envelope(study)
        assert env["ok"] and env["data"]["n_events_requested"] == 1  # the real event is studied, the absurd one skipped
        assert any("outside the datetime range" in w for w in env["warnings"])
        listed = await c.call_tool("events_list", {"kind": "fomc"})
        assert check(listed) is None


async def test_touch_probability_absurd_days_is_invalid_input():
    rt = working_runtime("")
    srv, _ = build_server(rt)
    async with Client(srv) as c:
        res = await c.call_tool("touch_probability", {"spot": 82_750.0, "barrier": 90_000.0, "days": 1e12, "iv": 0.5})
        assert check(res) is None
        env = as_envelope(res)
        assert env["ok"] is False and env["missing"][0]["reason"] == "invalid_input"
        ok = await c.call_tool("touch_probability", {"spot": 82_750.0, "barrier": 90_000.0, "days": 30.0, "iv": 0.5})
        assert as_envelope(ok)["ok"] and 0 < as_envelope(ok)["data"]["analytic"] < 1
