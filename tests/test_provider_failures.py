"""Invariant 1 / 5: a provider failure never escapes a tool; the envelope comes back with `ok` and `missing` set.

Covers tools that had only happy-path tests (audit 2026-11-02, docs/changes/2026-11-02-provider-failure-tests.md).
"""

from __future__ import annotations

import httpx
import pytest

from conftest import NOW, FakeEx, State, make_transports
from test_server import call
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


def _down(req: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="down")


def _server_with_down(state: State, *names: str):
    tr = make_transports(state)
    for name in names:
        tr[name] = httpx.MockTransport(_down)
    rt = Runtime(Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0), transports=tr, exchange_factory=FakeEx)
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    return srv


def _assert_failed(env: dict, provider: str) -> None:
    assert env["ok"] is False and env["data"] is None
    assert any(m["provider"] == provider and m["reason"] in ("backend_error", "network") for m in env["missing"])


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("deribit_futures_curve", {"currency": "BTC"}),
        ("deribit_dvol", {"currency": "BTC", "hours_back": 24}),
        ("deribit_largest_trades", {"currency": "BTC", "kind": "option", "count": 50}),
    ],
)
async def test_deribit_down_returns_envelope(server, state, tool, args):
    state.deribit_status = 503
    _assert_failed(await call(server, tool, **args), "deribit")


async def test_hyperliquid_overview_down_returns_envelope(state):
    srv = _server_with_down(state, "hyperliquid")
    _assert_failed(await call(srv, "hyperliquid_overview"), "hyperliquid")


@pytest.mark.parametrize(("tool", "args"), [("global_market", {}), ("top_coins", {"limit": 10})])
async def test_coingecko_down_returns_envelope(state, tool, args):
    srv = _server_with_down(state, "coingecko")
    _assert_failed(await call(srv, tool, **args), "coingecko")


async def test_polymarket_market_gamma_down_returns_envelope(state):
    srv = _server_with_down(state, "polymarket:gamma")
    _assert_failed(await call(srv, "polymarket_market", market_id="516700"), "polymarket:gamma")


async def test_polymarket_market_clob_down_keeps_card_without_books(state):
    srv = _server_with_down(state, "polymarket:clob")
    env = await call(srv, "polymarket_market", market_id="516700")
    assert env["ok"] is True and env["data"]["market"]["slug"].startswith("will-btc")
    assert all("book" not in o for o in env["data"]["market"]["outcomes"])
    assert sum(m["provider"] == "polymarket:clob" for m in env["missing"]) == 2  # one per outcome book


async def test_polymarket_history_clob_down_returns_envelope(state):
    srv = _server_with_down(state, "polymarket:clob")
    _assert_failed(await call(srv, "polymarket_history", token_id="TOK_YES"), "polymarket:clob")
