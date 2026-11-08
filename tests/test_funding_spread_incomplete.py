"""funding_spread_scan marks a pair whose leg's funding history came back incomplete (complete=False)."""

from __future__ import annotations

from typing import Any

from conftest import NOW, FakeEx, State, make_transports
from test_server import call
from tradedesk.config import Settings
from tradedesk.core.envelope import Fetched
from tradedesk.core.errors import ProviderError
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


def _runtime(state: State) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    return rt


def _cut_ccxt(rt: Runtime, venue: str) -> None:
    orig = rt.exchanges.funding_rate_history_range

    async def patched(ex_id: str, *a: Any, **kw: Any) -> Fetched:
        f = await orig(ex_id, *a, **kw)
        if ex_id == venue:
            return Fetched({**f.data, "complete": False, "truncated": True}, f.evidence)
        return f

    rt.exchanges.funding_rate_history_range = patched  # type: ignore[method-assign]


def _cut_hl(rt: Runtime) -> None:
    orig = rt.hyperliquid.funding_history_range

    async def patched(*a: Any, **kw: Any) -> Fetched:
        f = await orig(*a, **kw)
        return Fetched({**f.data, "complete": False}, f.evidence)

    rt.hyperliquid.funding_history_range = patched  # type: ignore[method-assign]


async def test_complete_history_has_no_flag(state) -> None:
    rt = _runtime(state)
    try:
        server, _ = build_server(rt)
        env = await call(server, "funding_spread_scan", bases=["BTC"], entry_apr=0.01)
        assert env["ok"], env["missing"]
        (row,) = env["data"]["rows"]
        assert "incomplete_history" not in row["flags"] and row["tradable_now"] is True
        assert not any("funding history incomplete" in w for w in env["warnings"])
    finally:
        await rt.aclose()


async def test_incomplete_ccxt_leg_flags_pair_and_blocks_tradable(state) -> None:
    rt = _runtime(state)
    _cut_ccxt(rt, "bybit")
    try:
        server, _ = build_server(rt)
        env = await call(server, "funding_spread_scan", bases=["BTC"], entry_apr=0.01)
        assert env["ok"], env["missing"]
        (row,) = env["data"]["rows"]
        assert row["status"] == "above_entry"  # same numbers as the complete run, only the verdict changes
        assert "incomplete_history" in row["flags"] and row["tradable_now"] is False
        assert env["data"]["summary"]["tradable_now"] == []
        assert any("bybit BTC/USDT:USDT: funding history incomplete" in w for w in env["warnings"])
        assert not any(w.startswith("binanceusdm BTC") for w in env["warnings"])
    finally:
        await rt.aclose()


async def test_incomplete_hyperliquid_leg_flags_pair(state) -> None:
    rt = _runtime(state)
    _cut_hl(rt)
    try:
        server, _ = build_server(rt)
        env = await call(server, "funding_spread_scan", venues=["binanceusdm", "hyperliquid"], bases=["BTC"], entry_apr=0.0)
        assert env["ok"], env["missing"]
        (row,) = env["data"]["rows"]
        assert "incomplete_history" in row["flags"] and row["tradable_now"] is False
        assert any("hyperliquid BTC/USDT:USDT: funding history incomplete" in w for w in env["warnings"])
    finally:
        await rt.aclose()


async def test_incomplete_leg_outside_the_pair_does_not_flag(state) -> None:
    rt = _runtime(state)
    _cut_ccxt(rt, "bybit")
    try:
        server, _ = build_server(rt)
        env = await call(server, "funding_spread_scan", venues=["binanceusdm", "bybit", "hyperliquid"], bases=["BTC"], entry_apr=0.0)
        assert env["ok"], env["missing"]
        (row,) = env["data"]["rows"]
        # in the fake BTC pairs binanceusdm with hyperliquid; bybit's partial history is warned about but not flagged
        assert {row["short_venue"], row["long_venue"]} == {"binanceusdm", "hyperliquid"}
        assert any("bybit BTC/USDT:USDT: funding history incomplete" in w for w in env["warnings"])
        assert "incomplete_history" not in row["flags"] and row["tradable_now"] is True
    finally:
        await rt.aclose()


async def test_funding_history_failure_goes_to_missing(state) -> None:
    rt = _runtime(state)

    async def boom(*a: Any, **kw: Any) -> Fetched:
        raise ProviderError("ccxt:bybit", "network", "down")

    rt.exchanges.funding_rate_history_range = boom  # type: ignore[method-assign]
    try:
        server, _ = build_server(rt)
        env = await call(server, "funding_spread_scan", bases=["BTC"], entry_apr=0.01)
        assert env["ok"] is False
        assert any(m["field"].startswith("funding_history:") for m in env["missing"])
        assert not any("funding history incomplete" in w for w in env["warnings"])
    finally:
        await rt.aclose()
