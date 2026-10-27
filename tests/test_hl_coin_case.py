"""Hyperliquid coin names are case-sensitive (kPEPE, not KPEPE): the provider must keep the universe spelling."""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import NOW, FakeEx, State, hl_payload, make_transports
from test_server import call
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

UNIVERSE = ["BTC", "kPEPE", "kBONK", "kLUNC"]
DELISTED = {"kLUNC"}


def _runtime(state: State, seen: list[str], *, fail: bool = False, meta_fail: bool = False, meta_calls: list[int] | None = None) -> Runtime:
    base = make_transports(state)
    inner = base["hyperliquid"]

    def hl(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if fail:
            return httpx.Response(500, text="down")
        if body["type"] == "metaAndAssetCtxs":
            if meta_calls is not None:
                meta_calls.append(1)
            if meta_fail:
                return httpx.Response(500, text="meta down")
            meta, ctxs = hl_payload()
            meta["universe"] = [
                {"name": n, "szDecimals": 3, "maxLeverage": 10, **({"isDelisted": True} if n in DELISTED else {})} for n in UNIVERSE
            ]
            return httpx.Response(200, json=[meta, [ctxs[0]] * len(UNIVERSE)])
        coin = body["coin"] if "coin" in body else body["req"]["coin"]
        seen.append(coin)
        if coin not in UNIVERSE:  # real HL returns nothing for an unknown (wrong-case) name
            return httpx.Response(200, json=[])
        return inner.handler(req)

    base["hyperliquid"] = httpx.MockTransport(hl)
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=""), transports=base, exchange_factory=FakeEx)
    rt.now = lambda: NOW
    return rt


@pytest.mark.parametrize("typed", ["kPEPE", "KPEPE", "kpepe"])
async def test_resolve_keeps_universe_spelling(state, typed):
    rt = _runtime(state, [])
    try:
        assert await rt.hyperliquid.resolve_coin(typed) == "kPEPE"
        assert await rt.hyperliquid.resolve_coin("btc") == "BTC"
        assert await rt.hyperliquid.resolve_coin("nope") == "NOPE"  # unknown: old behaviour (upper-case)
    finally:
        await rt.aclose()


@pytest.mark.parametrize("typed", ["xyz:SP500", "XYZ:SP500", "xyz:sp500"])
async def test_builder_dex_coin_keeps_lower_case_prefix(state, typed):
    rt = _runtime(state, [])
    try:
        assert await rt.hyperliquid.lookup_coin(typed) == ("xyz:SP500", "dex")
    finally:
        await rt.aclose()


async def test_resolve_matches_delisted_coin(state):
    rt = _runtime(state, [])
    try:
        assert await rt.hyperliquid.lookup_coin("klunc") == ("kLUNC", "exact")
    finally:
        await rt.aclose()


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("hyperliquid_candles", {}),
        ("hyperliquid_book", {}),
        ("hyperliquid_trades", {}),
        ("hyperliquid_funding_history", {"days_back": 7}),
    ],
)
async def test_tools_work_for_k_coin(state, tool, args):
    seen: list[str] = []
    calls: list[int] = []
    rt = _runtime(state, seen, meta_calls=calls)
    try:
        server, _ = build_server(rt)
        env = await call(server, tool, coin="KPEPE", **args)
        assert env["ok"] is True
        assert env["data"]["coin"] == "kPEPE"
        assert set(seen) == {"kPEPE"}
        assert len(calls) == 1  # name table fetched once, even with cache_scale=0 and pagination
        assert not env["warnings"]
    finally:
        await rt.aclose()


async def test_delisted_coin_history_still_works(state):
    seen: list[str] = []
    rt = _runtime(state, seen)
    try:
        server, _ = build_server(rt)
        env = await call(server, "hyperliquid_funding_history", coin="klunc", days_back=7)
        assert env["ok"] is True
        assert env["data"]["coin"] == "kLUNC"
        assert set(seen) == {"kLUNC"}
    finally:
        await rt.aclose()


async def test_meta_down_but_main_endpoint_works(state):
    seen: list[str] = []
    calls: list[int] = []
    rt = _runtime(state, seen, meta_fail=True, meta_calls=calls)
    try:
        server, _ = build_server(rt)
        env = await call(server, "hyperliquid_candles", coin="btc")
        assert env["ok"] is True
        assert env["data"]["coin"] == "BTC"
        assert set(seen) == {"BTC"}
        assert any("universe unavailable" in w for w in env["warnings"])
        after_first = len(calls)  # includes the HTTP retry attempt
        await call(server, "hyperliquid_book", coin="btc")
        assert len(calls) == after_first  # failed fetch is not retried on every call
    finally:
        await rt.aclose()


async def test_unknown_coin_warns(state):
    rt = _runtime(state, [])
    try:
        server, _ = build_server(rt)
        env = await call(server, "hyperliquid_candles", coin="nope")
        assert env["ok"] is False
        assert any("not found" in w for w in env["warnings"])
    finally:
        await rt.aclose()


async def test_perps_funding_history_hl_leg_uses_k_coin_name(state):
    seen: list[str] = []
    rt = _runtime(state, seen)
    try:
        server, _ = build_server(rt)
        env = await call(server, "perps_funding_history", base="kPEPE", exchanges=["binanceusdm"], days_back=7)
        assert "kPEPE" in set(seen)
        assert set(seen) == {"kPEPE"}
        assert any(row["venue"] == "hyperliquid" for row in env["data"]["venues"]) if env["data"] else True
    finally:
        await rt.aclose()


async def test_k_coin_provider_failure_still_returns_envelope(state):
    rt = _runtime(state, [], fail=True)
    try:
        server, _ = build_server(rt)
        for tool in ("hyperliquid_candles", "hyperliquid_book", "hyperliquid_trades", "hyperliquid_funding_history"):
            env = await call(server, tool, coin="kPEPE")
            assert env["ok"] is False
            assert env["missing"]
    finally:
        await rt.aclose()
