"""Invariant 1 / 5: a provider failure never escapes a tool; the envelope comes back with `ok` and `missing` set.

Covers tools that had only happy-path tests (audit 2026-11-02, docs/changes/2026-11-02-provider-failure-tests.md;
the remaining 13 in docs/changes/2026-11-03-provider-failure-tests-2.md).
"""

from __future__ import annotations

import ccxt
import httpx
import pytest

from conftest import NOW, FakeEx, State, make_transports
from test_server import call
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


def _down(req: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="down")


class _BrokenEx(FakeEx):
    """FakeEx whose order book and market list fail like a venue outage (ccxt NetworkError / ExchangeError)."""

    async def fetch_order_book(self, symbol, limit=100):
        raise ccxt.NetworkError("venue down")

    async def load_markets(self):
        raise ccxt.ExchangeError("503 Service Unavailable")


def _server_with_down(state: State, *names: str, factory=FakeEx):
    tr = make_transports(state)
    for name in names:
        tr[name] = httpx.MockTransport(_down)
    rt = Runtime(Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0), transports=tr, exchange_factory=factory)
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
        ("deribit_largest_trades", {"currency": "BTC", "kind": "future", "count": 50}),
        # The option-book path (_chain), as in deribit_vol_surface.
        ("deribit_option_chain", {"expiry": "27NOV26", "currency": "BTC"}),
        ("prob_from_options", {"expiry": "27NOV26", "level": 90000, "kind": "above", "currency": "BTC"}),
        ("deribit_expected_move", {"currency": "BTC"}),
        ("deribit_oi_by_strike", {"currency": "BTC"}),
        ("deribit_atm_term", {"currency": "BTC"}),
        ("deribit_skew_term", {"currency": "BTC"}),
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


# --- ccxt single-venue tools (audit 2026-11-03, item 23) ---


async def test_market_ohlcv_venue_down_returns_envelope(server):
    env = await call(server, "market_ohlcv", exchange="binanceusdm", symbol="DOWN/USDT:USDT", timeframe="1h", limit=50)
    _assert_failed(env, "ccxt:binanceusdm")


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("market_orderbook", {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT", "depth": 50, "notional_usd": 100_000}),
        ("market_markets", {"exchange": "binanceusdm", "market_type": "swap"}),
    ],
)
async def test_ccxt_venue_down_returns_envelope(state, tool, args):
    srv = _server_with_down(state, factory=_BrokenEx)
    _assert_failed(await call(srv, tool, **args), "ccxt:binanceusdm")


# --- cross-venue perps_* tools: one venue down -> it lands in `missing`, the rest still answer ---


def _venues(env: dict, key: str = "venues") -> set[str]:
    return {v["venue"] for v in env["data"][key]}


@pytest.mark.parametrize("tool", ["perps_long_short_ratio", "perps_oi_history"])
async def test_perps_history_one_venue_down_keeps_the_other(state, tool):
    srv = _server_with_down(state, "bybit")
    env = await call(srv, tool, base="BTC", period="1h", limit=48)
    assert env["ok"] is True and _venues(env) == {"binanceusdm"}
    assert any(m["provider"] == "bybit" and m["reason"] == "backend_error" for m in env["missing"])


@pytest.mark.parametrize("tool", ["perps_long_short_ratio", "perps_oi_history"])
async def test_perps_history_all_venues_down_returns_envelope(state, tool):
    srv = _server_with_down(state, "bybit", "binance_futures")
    env = await call(srv, tool, base="BTC", period="1h", limit=48)
    _assert_failed(env, "bybit")
    _assert_failed(env, "binance_futures")


async def test_perps_open_interest_partial_failure(state):
    srv = _server_with_down(state, "hyperliquid", "deribit")
    env = await call(srv, "perps_open_interest", base="BTC")
    assert env["ok"] is True and _venues(env) == {"binanceusdm", "bybit"}
    failed = {m["provider"] for m in env["missing"]}
    assert {"hyperliquid", "deribit", "ccxt:okx"} <= failed  # FakeEx okx has no fetchOpenInterest
    assert env["data"]["total_oi_usd"] == pytest.approx(sum(v["oi_usd"] for v in env["data"]["venues"]))


async def test_perps_open_interest_all_down_returns_envelope(state):
    srv = _server_with_down(state, "hyperliquid", "deribit")
    env = await call(srv, "perps_open_interest", base="BTC", exchanges=["okx"])
    _assert_failed(env, "hyperliquid")
    _assert_failed(env, "deribit")


async def test_perps_basis_compare_partial_failure(state):
    srv = _server_with_down(state, "hyperliquid", "deribit")
    env = await call(srv, "perps_basis_compare", base="BTC")
    assert env["ok"] is True and _venues(env, "perps") == {"binanceusdm", "bybit"}
    assert env["data"]["deribit_dated_futures"] == []
    assert env["data"]["summary"]["nearest_dated_annualized"] is None
    failed = {m["provider"]: m["reason"] for m in env["missing"]}
    assert failed["hyperliquid"] == failed["deribit"] == "backend_error"
    assert failed["ccxt:okx"] == "rate_limited"  # FakeEx okx answers 429


async def test_perps_basis_compare_all_down_returns_envelope(state):
    srv = _server_with_down(state, "hyperliquid", "deribit")
    env = await call(srv, "perps_basis_compare", base="BTC", exchanges=["okx"])
    _assert_failed(env, "hyperliquid")
    _assert_failed(env, "deribit")
