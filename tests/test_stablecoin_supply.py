"""stablecoin_supply (DefiLlama stablecoins.llama.fi) on a SYNTHETIC sample of the documented shape behind MockTransport.

The fixture is not a real capture (see its `_note`); numbers are round so every expected value can be checked by hand.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from mcp import Client

from test_server import call
from tradedesk.analytics.chain_metrics import stablecoin_asset, stablecoin_total
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIX = json.loads((Path(__file__).parent / "fixtures" / "defillama_stablecoins_sample_2026-11-05.json").read_text())
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)  # latest chart point is 2026-11-05 00:00 UTC
SRC = "defillama:stablecoins"


def default_handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/stablecoins":
        assert req.url.params.get("includePrices") == "true"
        return httpx.Response(200, json=FIX["stablecoins"])
    if req.url.path == "/stablecoincharts/all":
        return httpx.Response(200, json=FIX["stablecoincharts_all"])
    return httpx.Response(404, text="not mocked")


def make_server(handler=default_handler):
    rt = Runtime(Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0), transports={SRC: httpx.MockTransport(handler)})
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    return srv


async def test_stablecoin_supply_happy():
    env = await call(make_server(), "stablecoin_supply")
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    tot = d["total"]
    # latest 299.0e9 USD-pegged + 0.46e9 EUR-pegged (in USD); 7 days earlier 297.25e9 + 0.46e9; 30 days earlier 291.5e9 + 0.46e9
    assert tot["usd"] == pytest.approx(299.46e9)
    assert tot["change_7d"] == pytest.approx(299.46 / 297.71 - 1, abs=1e-6)
    assert tot["change_30d"] == pytest.approx(299.46 / 291.96 - 1, abs=1e-6)
    assert tot["age_days"] == pytest.approx(0.5) and tot["stale"] is False
    usdt, usdc = d["usdt"], d["usdc"]
    assert usdt["defillama_id"] == "1" and usdt["usd"] == pytest.approx(183.5e9)  # matched by id, not the "usdt " look-alike
    assert usdt["change_1d"] == pytest.approx(183.5 / 183.2 - 1, abs=1e-6)
    assert usdt["change_7d"] == pytest.approx(183.5 / 182.0 - 1, abs=1e-6)
    assert usdt["change_30d"] == pytest.approx(183.5 / 178.0 - 1, abs=1e-6)
    assert usdt["price"] == pytest.approx(1.0002)
    assert usdc["defillama_id"] == "2" and usdc["usd"] == pytest.approx(76e9)
    assert usdc["change_7d"] == pytest.approx(76 / 77 - 1, abs=1e-6) and usdc["change_7d"] < 0
    assert usdc["change_30d"] == pytest.approx(76 / 74 - 1, abs=1e-6)
    assert {e["provider"] for e in env["evidence"]} == {SRC} and len(env["evidence"]) == 2
    assert any("not a signal" in w for w in env["warnings"])


async def test_stablecoin_supply_provider_failure():
    env = await call(make_server(lambda req: httpx.Response(503, text="down")), "stablecoin_supply")
    assert env["ok"] is False and env["data"] is None
    assert {m["field"] for m in env["missing"]} == {"total", "usdt_usdc"}
    assert all(m["provider"] == SRC and m["reason"] == "backend_error" for m in env["missing"])


async def test_stablecoin_supply_partial_failure_keeps_the_other_panel():
    def chart_down(req):
        if req.url.path == "/stablecoincharts/all":
            return httpx.Response(500, text="boom")
        return default_handler(req)

    env = await call(make_server(chart_down), "stablecoin_supply")
    assert env["ok"] is True and env["data"]["total"] is None and env["data"]["usdt"]["usd"] == pytest.approx(183.5e9)
    assert [m["field"] for m in env["missing"]] == ["total"]


async def test_stablecoin_supply_unexpected_shapes():
    def odd(req):
        if req.url.path == "/stablecoins":
            return httpx.Response(200, json=[{"symbol": "USDT"}])  # list instead of {"peggedAssets": [...]}
        return httpx.Response(200, json=[{"date": "x", "totalCirculatingUSD": {"peggedUSD": 1.0}}, "junk"])

    env = await call(make_server(odd), "stablecoin_supply")
    assert env["ok"] is False
    reasons = {m["field"]: m["reason"] for m in env["missing"]}
    assert reasons == {"usdt_usdc": "backend_error", "total": "data_unavailable"}


async def test_provider_slims_payloads():
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0), transports={SRC: httpx.MockTransport(default_handler)})
    try:
        assets = (await rt.defillama.stablecoin_assets()).data
        chart = (await rt.defillama.stablecoin_chart_all()).data
    finally:
        await rt.aclose()
    assert len(assets) == 5 and all("chainCirculating" not in a and "chains" not in a for a in assets)
    assert len(chart) == 37 and set(chart[0]) == {"date", "totalCirculatingUSD"}


def test_stablecoin_helpers_edge_cases():
    day = 86400
    t0 = 1_790_726_400
    # bad dates (non-numeric, inf, missing) and empty USD dicts are skipped; numeric dates are accepted too
    rows = [
        {"date": "nope", "totalCirculatingUSD": {"peggedUSD": 1.0}},
        {"date": "inf", "totalCirculatingUSD": {"peggedUSD": 1.0}},
        {"totalCirculatingUSD": {"peggedUSD": 1.0}},
        {"date": str(t0), "totalCirculatingUSD": {}},
        {"date": t0 + day, "totalCirculatingUSD": {"peggedUSD": 100.0, "peggedEUR": "x"}},
    ]
    s = stablecoin_total(rows, t0 + 5 * day)
    assert s["usd"] == 100.0 and s["as_of_ts"] == t0 + day and s["change_7d"] is None and s["stale"] is True
    assert stablecoin_total([]) is None
    assets = FIX["stablecoins"]["peggedAssets"]
    assert stablecoin_asset(assets, "EURC") is None  # not USD-pegged
    assert stablecoin_asset(assets, "NEWUSD") is None  # not a known stablecoin id
    only_fake = [a for a in assets if a["id"] == "999"]
    assert stablecoin_asset(only_fake, "usdt") is None  # symbol "usdt " alone does not make it Tether


def test_stablecoin_asset_matches_by_id_not_symbol():
    assets = FIX["stablecoins"]["peggedAssets"]
    # a look-alike bigger than the real Tether still loses: only id "1" / gecko_id "tether" count
    inflated = [dict(a, circulating={"peggedUSD": 9e12}) if a["id"] == "999" else a for a in assets]
    assert stablecoin_asset(inflated, "USDT")["defillama_id"] == "1"
    # gecko_id alone is enough (id renumbered upstream), and a real USDC is never picked for USDT
    renumbered = [dict(a, id="5000") if a["id"] == "2" else a for a in assets]
    usdc = stablecoin_asset(renumbered, "usdc")
    assert usdc["defillama_id"] == "5000" and usdc["usd"] == pytest.approx(76e9)
    assert stablecoin_asset([a for a in assets if a["id"] == "2"], "USDT") is None


async def test_stablecoin_supply_lookalike_only_goes_to_missing():
    def fake_usdt(req):
        if req.url.path == "/stablecoins":
            rows = [a for a in FIX["stablecoins"]["peggedAssets"] if a["id"] != "1"]  # real Tether absent
            return httpx.Response(200, json={"peggedAssets": rows})
        return default_handler(req)

    env = await call(make_server(fake_usdt), "stablecoin_supply")
    assert env["ok"] is True and env["data"]["usdt"] is None and env["data"]["usdc"]["defillama_id"] == "2"
    assert [(m["field"], m["reason"]) for m in env["missing"]] == [("usdt", "data_unavailable")]


async def test_stablecoin_supply_survives_runtime_aclose():
    seen = []

    def counting(req):
        seen.append(req.url.path)
        return default_handler(req)

    async def tool(c):
        res = await c.call_tool("stablecoin_supply", {})
        return res.structured_content or json.loads(res.content[0].text)

    rt = Runtime(Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0), transports={SRC: httpx.MockTransport(counting)})
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    try:
        # one MCP session (its lifespan would close the runtime on exit, so the checks run inside it)
        async with Client(srv) as c:
            assert (await tool(c))["ok"] is True
            client = rt.defillama.stablecoins._client_obj
            assert client is not None and not client.is_closed
            await rt.aclose()  # what each MCP session's lifespan does on exit
            assert client.is_closed and rt.defillama.stablecoins._client_obj is None
            env = await tool(c)  # a fresh client is created lazily: real requests again, not a closed-client error
        assert env["ok"] is True and env["missing"] == [] and env["data"]["usdt"]["defillama_id"] == "1"
        assert len(seen) == 4 and client.is_closed
    finally:
        await rt.aclose()
