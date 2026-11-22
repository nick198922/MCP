"""Review leftovers, second batch (standing card item 256, BACKLOG 6.52).

1. strategy_backtest_momentum warned about bars dropped for a non-numeric field, and its minimum of bars counted the
   bars left after the drop (review 2026-11-06-wrong-type-fields, second round, remarks 2(a) and 3). The tool was
   removed 2026-11-18 (docs/changes/2026-11-18-mcp-remove-six.md) and its two tests with it.
2. deribit_vrp warns when DVOL paging stops on a continuation of the wrong type (same review, remark 2(b)).
3. A funding-history row dropped for a non-finite or non-numeric rate is named (venue, symbol, count) in a warning of
   the perps tools instead of leaving a silent hole (found 07.11 during item 254).
"""

from __future__ import annotations

import json
import math
from typing import Any

import httpx
import pytest
from mcp import Client

from conftest import NOW, FakeEx, State, make_transports
from test_review_leftovers_nonfinite import CCXT_HIST, HL, _clean_and_broken
from test_tools_provider_outage import as_envelope
from test_tools_wrong_type_fields import _failure
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


def _runtime(tmp_path, *, transports: dict | None = None) -> Runtime:
    rt = Runtime(
        Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / "leftovers2.sqlite")),
        transports=transports or make_transports(State()),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    return rt


async def _call(rt: Runtime, name: str, args: dict) -> Any:
    srv, _ = build_server(rt)
    try:
        async with Client(srv) as c:
            return await c.call_tool(name, args)
    finally:
        await rt.aclose()


def _vrp_transports(continuation: Any) -> dict:
    """The conftest transports with Deribit's DVOL answer carrying `continuation` on every page."""
    tr = make_transports(State())
    inner = tr["deribit"].handler

    def deribit(req: httpx.Request) -> httpx.Response:
        resp = inner(req)
        if not req.url.path.endswith("get_volatility_index_data"):
            return resp
        body = json.loads(resp.content)
        body["result"]["continuation"] = continuation
        return httpx.Response(200, json=body)

    tr["deribit"] = httpx.MockTransport(deribit)
    return tr


@pytest.mark.parametrize("continuation", ["abc", True, [123]])
async def test_deribit_vrp_warns_on_a_malformed_continuation(tmp_path, continuation):
    args = {"currency": "BTC", "history_days": 200}
    clean = as_envelope(await _call(_runtime(tmp_path), "deribit_vrp", args))
    res = await _call(_runtime(tmp_path, transports=_vrp_transports(continuation)), "deribit_vrp", args)
    assert _failure(res) is None
    env = as_envelope(res)
    assert env["ok"] is clean["ok"] is True
    assert not any("malformed continuation" in w for w in clean["warnings"])
    assert any("malformed continuation" in w for w in env["warnings"]), env["warnings"]
    assert env["data"]["history"] == clean["data"]["history"]  # the first page already covers the window here


async def test_deribit_vrp_numeric_continuation_is_not_flagged(tmp_path):
    # a numeric string continuation at or before the window start ends paging normally, without a warning
    res = await _call(_runtime(tmp_path, transports=_vrp_transports("1")), "deribit_vrp", {"currency": "BTC", "history_days": 200})
    env = as_envelope(res)
    assert env["ok"] is True
    assert not any("malformed continuation" in w for w in env["warnings"])


@pytest.mark.parametrize(
    ("name", "field", "label"),
    [
        ("perps_funding_history", CCXT_HIST.format("binanceusdm"), "binanceusdm BTC/USDT:USDT"),
        ("perps_funding_history", f"{HL}.fundingRate", "hyperliquid BTC"),
        ("hyperliquid_funding_history", f"{HL}.fundingRate", "hyperliquid "),
        ("funding_carry_scan", CCXT_HIST.format("binanceusdm"), "binanceusdm "),
        ("funding_spread_scan", CCXT_HIST.format("bybit"), "bybit "),
    ],
)
@pytest.mark.parametrize("value", [math.nan, "abc"])
async def test_perps_tools_name_a_dropped_funding_row(tmp_path, name, field, label, value):
    clean, res = await _clean_and_broken(tmp_path, name, field, value, "last")
    assert _failure(res) is None
    env = as_envelope(res)
    assert env["ok"] is clean["ok"] is True
    assert not any("funding rows dropped" in w for w in clean["warnings"]), clean["warnings"]
    named = [w for w in env["warnings"] if w.startswith(label) and ": 1 funding rows dropped" in w]
    assert named, env["warnings"]
