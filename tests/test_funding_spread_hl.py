"""funding_spread_scan with Hyperliquid as a venue: hourly funding, kPEPE-style name matching, provider failure."""

from __future__ import annotations

import httpx
import pytest

from conftest import NOW, FakeEx, State, make_transports
from test_server import call
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools.perps import hl_symbol


def test_hl_symbol_uses_cex_naming() -> None:
    assert hl_symbol("kPEPE") == "1000PEPE/USDT:USDT"
    assert hl_symbol("BTC") == "BTC/USDT:USDT"


def _runtime(state: State, *, hl_down: bool = False) -> Runtime:
    tr = make_transports(state)
    if hl_down:
        tr["hyperliquid"] = httpx.MockTransport(lambda req: httpx.Response(500, text="down"))
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=""), transports=tr, exchange_factory=FakeEx)
    rt.now = lambda: NOW
    return rt


async def test_spread_scan_with_hyperliquid(state) -> None:
    rt = _runtime(state)
    try:
        server, _ = build_server(rt)
        env = await call(server, "funding_spread_scan", venues=["binanceusdm", "hyperliquid"], bases=["BTC"], entry_apr=0.0)
        assert env["ok"], env["missing"]
        assert any("Backtest" in w for w in env["warnings"])
        (row,) = env["data"]["rows"]
        hl = row["venues"]["hyperliquid"]
        assert hl["interval_hours"] == 1.0
        assert hl["current_rate"] == pytest.approx(0.0000125)
        assert hl["current_apr"] == pytest.approx(0.0000125 * 8760)  # hourly rate x 8760, decimal
        assert hl["cap"] == pytest.approx(0.04)
        assert hl["mark"] == pytest.approx(82750.0)
        assert hl["trailing_apr"] is not None
        assert {row["short_venue"], row["long_venue"]} == {"binanceusdm", "hyperliquid"}
        assert "interval_mismatch" in row["flags"]
    finally:
        await rt.aclose()


async def test_spread_scan_candidates_include_hyperliquid(state) -> None:
    rt = _runtime(state)
    try:
        server, _ = build_server(rt)
        env = await call(server, "funding_spread_scan", venues=["binanceusdm", "hyperliquid"], entry_apr=0.0)
        assert env["ok"], env["missing"]
        assert {x["base"] for x in env["data"]["rows"]} == {"BTC", "ETH"}  # DOGE is HL-only in the fake, SOL CEX-only
    finally:
        await rt.aclose()


async def test_spread_scan_hyperliquid_down_still_returns(state) -> None:
    rt = _runtime(state, hl_down=True)
    try:
        server, _ = build_server(rt)
        env = await call(server, "funding_spread_scan", venues=["binanceusdm", "hyperliquid"], bases=["BTC"])
        assert env["ok"] is False
        assert any(m["field"].endswith(":hyperliquid") for m in env["missing"])
        # with a third venue the scan still answers from the working pair
        env3 = await call(server, "funding_spread_scan", venues=["binanceusdm", "bybit", "hyperliquid"], bases=["BTC"])
        assert env3["ok"], env3["missing"]
        assert any(m["field"].endswith(":hyperliquid") for m in env3["missing"])
        assert set(env3["data"]["rows"][0]["venues"]) == {"binanceusdm", "bybit"}
    finally:
        await rt.aclose()
