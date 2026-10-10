import json

import pytest
from mcp import Client

EXPECTED_TOOLS = {
    "system_health",
    "market_ticker",
    "market_ohlcv",
    "market_orderbook",
    "perps_funding_compare",
    "perps_open_interest",
    "hyperliquid_overview",
    "deribit_futures_curve",
    "deribit_vol_surface",
    "deribit_option_chain",
    "deribit_max_pain",
    "deribit_gamma_exposure",
    "prob_from_options",
    "odds_devig",
    "option_calc",
    "touch_probability",
}


async def call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content
    if data is None:
        data = json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


async def test_tool_surface(server):
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert EXPECTED_TOOLS <= names


async def test_funding_compare_normalises_intervals_and_degrades(server):
    env = await call(server, "perps_funding_compare", base="BTC")
    assert env["ok"]
    by = {v["venue"]: v for v in env["data"]["venues"]}
    assert by["binanceusdm"]["annualized"] == pytest.approx(0.1095)
    assert by["hyperliquid"]["annualized"] == pytest.approx(0.1095)  # 1.25e-5 per HOUR == 1e-4 per 8h
    assert by["deribit"]["annualized"] == pytest.approx(0.1095)
    assert "okx" not in by
    assert any(m["provider"] == "ccxt:okx" and m["reason"] == "rate_limited" for m in env["missing"])
    assert env["data"]["widest_spread"]["short_venue"] in by
    assert {e["provider"] for e in env["evidence"]} >= {"ccxt:binanceusdm", "hyperliquid", "deribit"}


async def test_open_interest(server):
    env = await call(server, "perps_open_interest", base="BTC")
    d = env["data"]
    venues = {v["venue"] for v in d["venues"]}
    assert {"binanceusdm", "bybit", "hyperliquid", "deribit"} <= venues
    assert d["total_oi_usd"] == pytest.approx(sum(v["oi_usd"] for v in d["venues"]))
    assert d["venues"][0]["oi_usd"] >= d["venues"][-1]["oi_usd"]


async def test_hyperliquid_overview_filters_and_sorts(server):
    env = await call(server, "hyperliquid_overview", sort_by="funding_annualized", min_oi_usd=1e6)
    coins = [c["coin"] for c in env["data"]["coins"]]
    assert coins[0] == "DOGE" and coins[-1] == "ETH"
    env2 = await call(server, "hyperliquid_overview", min_oi_usd=1e9)
    assert env2["data"]["coins"] == [] or all(c["open_interest_usd"] >= 1e9 for c in env2["data"]["coins"])


async def test_futures_curve_basis(server):
    env = await call(server, "deribit_futures_curve", currency="BTC")
    d = env["data"]
    nov = next(f for f in d["futures"] if f["expiry"] == "27NOV26")
    assert nov["basis_pct"] == pytest.approx(0.7223, abs=0.001)
    assert nov["days"] == pytest.approx(47.79, abs=0.02)
    assert nov["annualized_simple"] == pytest.approx(0.007223 * 365 / 47.787, abs=0.0005)
    assert nov["annualized_log"] < nov["annualized_simple"]
    assert [f["days"] for f in d["futures"]] == sorted(f["days"] for f in d["futures"])
    assert d["perpetual"]["funding_annualized"] == pytest.approx(0.1095)


async def test_vol_surface_recovers_input_smile(server):
    env = await call(server, "deribit_vol_surface", currency="BTC")
    exps = env["data"]["expiries"]
    assert [e["expiry"] for e in exps] == ["30OCT26", "27NOV26", "26MAR27"]
    for e in exps:
        assert e["atm_iv"] == pytest.approx(0.40, abs=0.005)
        assert e["rr25"] < 0 and e["fly25"] > 0
        assert e["expected_move_1sd_pct"] > 0
    assert exps[0]["days"] == pytest.approx(19.8, abs=0.1)


async def test_option_chain(server):
    env = await call(server, "deribit_option_chain", expiry="27nov26", width_pct=10)
    rows = env["data"]["strikes"]
    assert rows and all(r["call"] and r["put"] for r in rows)
    atm = min(rows, key=lambda r: abs(r["strike"] - env["data"]["forward"]))
    assert 0.4 < atm["call"]["delta"] < 0.6 and -0.6 < atm["put"]["delta"] < -0.4
    assert atm["call"]["vega"] > 0 and atm["call"]["theta"] < 0


async def test_bad_expiry_is_reported_not_raised(server):
    env = await call(server, "deribit_option_chain", expiry="1JAN30")
    assert env["ok"] is False
    assert "Available" in env["missing"][0]["detail"] or "no listed expiry" in env["missing"][0]["detail"]


async def test_probabilities_consistent(server):
    up = await call(server, "prob_from_options", expiry="27NOV26", level=90000, kind="above")
    dn = await call(server, "prob_from_options", expiry="27NOV26", level=90000, kind="below")
    tc = await call(server, "prob_from_options", expiry="27NOV26", level=90000, kind="touch")
    assert up["data"]["probability"] + dn["data"]["probability"] == pytest.approx(1.0, abs=1e-3)
    assert tc["data"]["probability"] > up["data"]["probability"]
    assert 0 < up["data"]["probability"] < 0.5
    far = await call(server, "prob_from_options", expiry="27NOV26", level=300000, kind="above")
    assert any("outside the quoted strikes" in w for w in far["warnings"])


async def test_max_pain_and_gex(server):
    mp = await call(server, "deribit_max_pain", expiry="30OCT26")
    assert mp["ok"] and 50000 <= mp["data"]["max_pain"] <= 130000 and len(mp["data"]["top_oi_strikes"]) == 5
    gx = await call(server, "deribit_gamma_exposure", currency="BTC")
    assert gx["ok"] and gx["data"]["regime"] and "Heuristic" in gx["data"]["notes"]
    assert gx["data"]["profile"]


async def test_market_tools(server):
    t = await call(server, "market_ticker", exchange="binanceusdm", symbol="BTC/USDT:USDT")
    assert t["data"]["last"] == 82750.0 and t["data"]["spread_bps"] == pytest.approx(0.1208, abs=0.01)
    o = await call(server, "market_ohlcv", exchange="binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h", limit=100)
    assert o["data"]["summary"]["realized_vol_annualized"] > 0 and len(o["data"]["rows"]) == 100
    bad = await call(server, "market_ohlcv", exchange="binanceusdm", symbol="BTC/USDT:USDT", timeframe="1x")
    assert bad["ok"] is False
    ob = await call(server, "market_orderbook", exchange="binanceusdm", symbol="BTC/USDT:USDT", depth=50, notional_usd=500_000)
    assert ob["data"]["impact"]["buy"]["slippage_bps_vs_mid"] > 0 and ob["data"]["impact"]["sell"]["slippage_bps_vs_mid"] > 0


async def test_exchange_failure_is_graceful(server):
    env = await call(server, "market_ticker", exchange="bybit", symbol="BTC/USDT:USDT")
    assert env["ok"] is False and env["missing"][0]["reason"] == "auth"


async def test_deribit_outage_does_not_crash_tools(server, state):
    state.deribit_status = 503
    env = await call(server, "deribit_vol_surface")
    assert env["ok"] is False and env["missing"][0]["provider"] == "deribit"
    assert env["missing"][0]["reason"] in ("backend_error", "network")
    h = await call(server, "system_health")
    assert h["data"]["health"]["providers"]["deribit"]["status"] == "down"


async def test_system_health_probe(server):
    env = await call(server, "system_health", probe=True)
    provs = env["data"]["health"]["providers"]
    assert provs["deribit"]["status"] == "ok" and provs["hyperliquid"]["status"] == "ok"
    assert provs["ccxt:bybit"]["status"] == "down"  # fake bybit ticker raises auth error
    assert env["data"]["tools"]


async def test_quant_tools(server):
    d = await call(server, "odds_devig", odds=[1.9, 1.9])
    assert d["data"]["margin"] == pytest.approx(0.052632, abs=1e-5) and d["data"]["fair_probs"] == [0.5, 0.5]
    bad = await call(server, "odds_devig", odds=[1.9, 0.9])
    assert bad["ok"] is False
    oc = await call(server, "option_calc", forward=100.0, strike=100.0, days=30, iv=0.5, kind="call")
    assert oc["data"]["delta"] == pytest.approx(0.5, abs=0.03) and oc["data"]["price_usd"] > 0
    tp = await call(server, "touch_probability", spot=100, barrier=120, days=30, iv=0.6)
    assert tp["data"]["analytic"] == pytest.approx(tp["data"]["monte_carlo"], abs=0.02)
