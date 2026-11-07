"""Invariant 1 / 5: provider failure -> envelope with `missing`, never an exception (item 258).

Tools whose provider path had no dedicated failure test (audit 2026-11-08): the generic outage sweeps
(test_tools_never_raise / test_tools_provider_outage) call market_regime and range_detect with the default
source='store', so their live ccxt path never met a failing venue; market_trades, calendar_expiries and
strategy_backtest_momentum had only happy-path tests. FakeEx raises ccxt.NetworkError for any `DOWN/...` OHLCV
symbol and ccxt.RateLimitExceeded for every okx call. Found on the way: market_regime(source='live') raised on a bar
with None prices (the venue answered, one row was broken); it now drops the row like range_detect does.
"""

from __future__ import annotations

import pytest

from conftest import NOW, FakeEx, make_transports
from test_server import call
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools import calendar

DOWN = "DOWN/USDT:USDT"


def _failed(env: dict, provider: str, reasons: tuple[str, ...]) -> None:
    assert env["ok"] is False and env["data"] is None
    assert any(m["provider"] == provider and m["reason"] in reasons for m in env["missing"]), env["missing"]


@pytest.mark.parametrize("tool", ["market_regime", "range_detect"])
async def test_live_source_venue_down_returns_envelope(server, tool):
    env = await call(server, tool, venue="binanceusdm", symbol=DOWN, timeframe="1h", source="live")
    _failed(env, "ccxt:binanceusdm", ("network", "backend_error"))


class _NoneBarEx(FakeEx):
    """The venue answers, but one 1h bar carries None prices (a broken row inside a 200 answer)."""

    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=100, since=None, params=None):
        rows = await super().fetch_ohlcv(symbol, timeframe, limit, since, params)
        rows[5] = [rows[5][0], None, None, None, None, None]
        return rows


async def test_market_regime_live_drops_a_bar_with_none_prices_instead_of_raising(state):
    # Before item 258 market_regime ran float(None) on the row and the exception escaped the tool.
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=""), transports=make_transports(state), exchange_factory=_NoneBarEx)
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    env = await call(srv, "market_regime", venue="binanceusdm", symbol="BTC/USDT:USDT", lookback_bars=50, source="live")
    assert env["ok"] is True and env["data"]["count"] == 49
    assert any("1 malformed bars" in w for w in env["warnings"])


async def test_market_trades_rate_limited_venue_returns_envelope(server):
    env = await call(server, "market_trades", exchange="okx", symbol="BTC/USDT:USDT", limit=50)
    _failed(env, "ccxt:okx", ("rate_limited",))


async def test_strategy_backtest_momentum_venue_down_returns_envelope(server):
    env = await call(server, "strategy_backtest_momentum", exchange="binanceusdm", symbol=DOWN, timeframe="1h", limit=200)
    _failed(env, "ccxt:binanceusdm", ("network", "backend_error"))


async def test_calendar_expiries_deribit_down_keeps_the_rest(server, state, monkeypatch):
    # One macro event inside the window (the shipped list is edited by hand, so the test pins its own).
    event = {"date": "2026-10-14", "time_utc": "12:30", "kind": "cpi", "label": "US CPI", "impact": "high"}
    monkeypatch.setattr(calendar, "_load_macro", lambda: {"events": [event]})
    state.deribit_status = 503
    env = await call(server, "calendar_expiries", currencies=["BTC", "ETH"], horizon_days=30, funding_hours_ahead=24)
    assert {m["field"] for m in env["missing"] if m["provider"] == "deribit"} == {"expiries:BTC", "expiries:ETH"}
    assert all(m["reason"] in ("backend_error", "network") for m in env["missing"] if m["provider"] == "deribit")
    # Deribit down: no expiries, but the computed funding schedule and the macro list still come back.
    assert env["ok"] is True and env["data"]["deribit_expiries"] == {}
    assert env["data"]["funding_schedule"] and [e["label"] for e in env["data"]["macro_events"]] == ["US CPI"]


async def test_calendar_expiries_deribit_down_and_no_macro_is_not_ok(server, state, monkeypatch):
    monkeypatch.setattr(calendar, "_load_macro", lambda: {"events": []})
    state.deribit_status = 503
    env = await call(server, "calendar_expiries", currencies=["BTC"], horizon_days=7)
    _failed(env, "deribit", ("backend_error", "network"))
