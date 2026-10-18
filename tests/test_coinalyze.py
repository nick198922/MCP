"""liquidations_history (Coinalyze): response shape from the real sample of 2026-10-17 behind MockTransport."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from mcp import Client

from tradedesk.analytics.liquidations_agg import aggregated_symbol, api_interval, summarise
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIX = json.loads(
    (Path(__file__).parent / "fixtures/doubletop/coinalyze__liquidation_history_BTCUSDT_PERP.A_daily_2026-10-17.json").read_text()
)
SAMPLE = FIX["calls"][1]["response"]  # 2024-01-01..2024-01-06, six daily rows
SECRET = "k3y-SECRET-0123"
NOW = datetime(2024, 1, 7, 0, 0, tzinfo=UTC)


async def call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content
    if data is None:
        data = json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


def make_server(handler=None, key=SECRET):
    seen: list[httpx.Request] = []

    def default(req):
        seen.append(req)
        return httpx.Response(200, json=[{"symbol": req.url.params["symbols"], "history": SAMPLE[0]["history"]}])

    h = handler or default
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, coinalyze_api_key=key), transports={"coinalyze": httpx.MockTransport(h)})
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    return srv, seen


def test_summarise_totals_and_shares():
    s = summarise(SAMPLE[0]["history"], "daily")
    assert s["totals"]["intervals"] == 6 and s["totals"]["gaps"] == 0
    longs = sum(h["l"] for h in SAMPLE[0]["history"])
    shorts = sum(h["s"] for h in SAMPLE[0]["history"])
    assert s["totals"]["long"] == pytest.approx(longs)
    assert s["totals"]["net"] == pytest.approx(longs - shorts)
    assert s["totals"]["long_share"] == pytest.approx(longs / (longs + shorts))
    assert s["peak"]["t"].startswith("2024-01-03")  # 534.4 + 192.1 is the largest day
    assert s["rows"][0]["long_share"] == pytest.approx(35.649 / (35.649 + 465.542))


def test_summarise_edge_cases():
    assert summarise([], "daily") is None
    assert summarise([{"t": "x"}, {"t": 1, "l": -1, "s": 0}], "daily") is None
    s = summarise([{"t": 0, "l": 0, "s": 0}, {"t": 3 * 86400, "l": 1, "s": 1}], "daily", limit=5)
    assert s["rows"][0]["long_share"] is None and s["totals"]["gaps"] == 1
    assert summarise(SAMPLE[0]["history"], "daily", limit=2)["totals"]["intervals"] == 2


def test_symbol_and_interval_helpers():
    assert aggregated_symbol("btc") == "BTCUSDT_PERP.A"
    assert aggregated_symbol("ETH", "usd") == "ETHUSD_PERP.A"
    assert aggregated_symbol("../x") is None and aggregated_symbol("BTC", "eur") is None
    assert api_interval("hourly") == "1hour"
    with pytest.raises(ValueError):
        api_interval("weekly")


async def test_liquidations_history_happy_and_key_not_leaked():
    srv, seen = make_server()
    env = await call(srv, "liquidations_history", symbol="btc", days=6)
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert d["symbol"] == "BTCUSDT_PERP.A" and d["interval"] == "daily" and d["unit"] == "USD"
    assert len(d["rows"]) == 6 and d["totals"]["intervals"] == 6
    req = seen[0]
    assert req.headers["api_key"] == SECRET
    assert req.url.params["symbols"] == "BTCUSDT_PERP.A" and req.url.params["interval"] == "daily"
    assert req.url.params["convert_to_usd"] == "true"
    assert int(req.url.params["to"]) == int(NOW.timestamp())
    assert SECRET not in json.dumps(env) and SECRET not in str(req.url)
    # '.A' is Binance's exchange code, not an all-exchange sum (docs/reviews/2026-10-18-doubletop-parity-1b.md)
    assert any("Binance only" in w for w in env["warnings"])


async def test_liquidations_history_hourly_cap_and_base_units():
    srv, seen = make_server()
    env = await call(srv, "liquidations_history", symbol="ETH", interval="hourly", days=90, in_usd=False)
    assert env["ok"] is True and env["data"]["days"] == 30 and env["data"]["unit"] == "ETH"
    assert any("capped" in w for w in env["warnings"])
    assert seen[0].url.params["interval"] == "1hour" and seen[0].url.params["convert_to_usd"] == "false"
    assert int(seen[0].url.params["to"]) - int(seen[0].url.params["from"]) <= 30 * 86400 + 3600


async def test_liquidations_history_provider_failure():
    srv, _ = make_server(lambda req: httpx.Response(503, text="down"))
    env = await call(srv, "liquidations_history", symbol="BTC")
    assert env["ok"] is False and env["data"] is None and env["missing"][0]["provider"] == "coinalyze"


async def test_liquidations_history_bad_key_is_auth_missing():
    srv, _ = make_server(lambda req: httpx.Response(401, text="invalid api key"))
    env = await call(srv, "liquidations_history", symbol="BTC")
    assert env["ok"] is False and env["missing"][0]["reason"] == "auth"


async def test_liquidations_history_empty_range():
    srv, _ = make_server(lambda req: httpx.Response(200, json=[]))
    env = await call(srv, "liquidations_history", symbol="BTC")
    assert env["ok"] is False and env["missing"][0]["reason"] == "data_unavailable"


async def test_liquidations_history_missing_key_makes_no_request():
    srv, seen = make_server(key=None)
    env = await call(srv, "liquidations_history", symbol="BTC")
    assert env["ok"] is False
    assert env["missing"][0]["reason"] == "config" and "COINALYZE_API_KEY" in env["missing"][0]["detail"]
    assert seen == []


async def test_liquidations_history_invalid_symbol():
    srv, seen = make_server()
    env = await call(srv, "liquidations_history", symbol="BTC/../x")
    assert env["ok"] is False and env["missing"][0]["reason"] == "invalid_argument" and seen == []
