import json

import pytest
from mcp import Client

EXPECTED_TOOLS = {
    "system_health",
    "market_ticker",
    "market_tickers",
    "market_markets",
    "market_ohlcv",
    "market_orderbook",
    "market_trades",
    "perps_funding_compare",
    "perps_open_interest",
    "hyperliquid_overview",
    "deribit_futures_curve",
    "deribit_vol_surface",
    "deribit_option_chain",
    "deribit_max_pain",
    "deribit_gamma_exposure",
    "deribit_expected_move",
    "deribit_oi_by_strike",
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
        assert "strikes_used" in e and e["strikes_used"] >= 3
        assert e["thin_wing_25d"] is False  # fixture has OI 20 on central strikes, 3 on tails -> default threshold 1.0 passes
    assert exps[0]["days"] == pytest.approx(19.8, abs=0.1)


async def test_vol_surface_min_oi_filter(server):
    env = await call(server, "deribit_vol_surface", currency="BTC", min_oi=10)
    strikes_each = [e["strikes_used"] for e in env["data"]["expiries"]]
    env2 = await call(server, "deribit_vol_surface", currency="BTC", min_oi=0)
    strikes_each0 = [e["strikes_used"] for e in env2["data"]["expiries"]]
    # the fixture has OI=20 for central strikes, 3 for tails; min_oi=10 drops the tails
    assert all(a < b for a, b in zip(strikes_each, strikes_each0, strict=True))

    env_hi = await call(server, "deribit_vol_surface", currency="BTC", min_oi=100)
    assert all(e.get("note", "").startswith("fewer than 3") for e in env_hi["data"]["expiries"])


async def test_vol_surface_thin_wing_flag(server):
    env = await call(server, "deribit_vol_surface", currency="BTC", thin_oi_threshold=25)
    exps = env["data"]["expiries"]
    assert all(e["thin_wing_25d"] is True for e in exps)
    assert any("thin 25d wing" in w for w in env["warnings"])


async def test_vol_surface_spread_filter_drops_unquoted(server):
    # the fixture has no bid/ask; max_spread_pct=any positive drops every strike
    env = await call(server, "deribit_vol_surface", currency="BTC", max_spread_pct=50)
    assert all(e.get("note", "").startswith("fewer than 3") for e in env["data"]["expiries"])


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


async def test_expected_move_breakevens_bracket_forward(server):
    env = await call(server, "deribit_expected_move", currency="BTC")
    exps = env["data"]["expiries"]
    assert [e["expiry"] for e in exps] == ["30OCT26", "27NOV26", "26MAR27"]
    for e in exps:
        assert e["straddle_usd"] > 0
        assert e["breakeven_lo"] < e["forward"] < e["breakeven_hi"]
        assert e["sigma_lo"] < e["forward"] < e["sigma_hi"]
        assert 0 < e["sigma_move_pct"] < 100
    # 1-expiry filter
    one = await call(server, "deribit_expected_move", currency="BTC", expiry="27NOV26")
    assert len(one["data"]["expiries"]) == 1 and one["data"]["expiries"][0]["expiry"] == "27NOV26"


async def test_oi_by_strike_walls(server):
    env = await call(server, "deribit_oi_by_strike", currency="BTC", width_pct=15, top_n=5)
    d = env["data"]
    assert d["total_call_oi"] > 0 and d["total_put_oi"] > 0 and d["band_strikes"] > 0
    assert len(d["top_overall"]) == 5 and len(d["top_call_walls"]) == 5
    tops = [w["total_oi"] for w in d["top_overall"]]
    assert tops == sorted(tops, reverse=True)
    # strikes nearest the forward dominate in the synthetic fixture (OI=20 for |logm|<0.15, 3 otherwise)
    atm_near = [w for w in d["top_overall"] if abs(w["distance_pct"]) < 15]
    assert atm_near, "expected close-to-forward strikes among the top walls"
    one = await call(server, "deribit_oi_by_strike", currency="BTC", expiry="30OCT26")
    assert one["data"]["expiry"] == "30OCT26" and one["data"]["forward"] > 0


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


async def test_market_tickers_sorts_and_picks_top_movers(server):
    env = await call(server, "market_tickers", exchange="binanceusdm")
    d = env["data"]
    syms = [t["symbol"] for t in d["tickers"]]
    assert syms[0] == "BTC/USDT:USDT"  # highest quoteVolume
    assert d["count"] == 4
    vols = [t["quoteVolume"] for t in d["tickers"]]
    assert vols == sorted(vols, reverse=True)
    movers = {m["symbol"] for m in d["top_movers"]}
    assert "SOL/USDT:USDT" in movers and "DOGE/USDT:USDT" in movers
    env2 = await call(server, "market_tickers", exchange="binanceusdm", symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"])
    assert {t["symbol"] for t in env2["data"]["tickers"]} == {"BTC/USDT:USDT", "ETH/USDT:USDT"}


async def test_market_tickers_unsupported_is_graceful(server):
    env = await call(server, "market_tickers", exchange="kraken")
    assert env["ok"] is False
    assert any(m["reason"] == "data_unavailable" for m in env["missing"])


async def test_market_markets_filters(server):
    env = await call(server, "market_markets", exchange="binanceusdm", market_type="swap")
    syms = [m["symbol"] for m in env["data"]["markets"]]
    assert "BTC/USDT:USDT" in syms and "ETH/USDT:USDT" in syms and "BTC/USDT" not in syms
    assert all(m["type"] == "swap" for m in env["data"]["markets"])

    env2 = await call(server, "market_markets", exchange="binanceusdm", query="btc")
    assert all("BTC" in m["symbol"].upper() for m in env2["data"]["markets"])

    env3 = await call(server, "market_markets", exchange="binanceusdm", active_only=False, query="dead")
    assert any(m["active"] is False for m in env3["data"]["markets"])


async def test_market_trades_flow(server):
    env = await call(server, "market_trades", exchange="binanceusdm", symbol="BTC/USDT:USDT", limit=50)
    s = env["data"]["summary"]
    assert s["count"] == 50
    assert s["buy_count"] + s["sell_count"] == 50
    assert s["imbalance"] is not None and -1 <= s["imbalance"] <= 1
    assert s["vwap"] > 0 and s["buy_vwap"] > 0 and s["sell_vwap"] > 0
    assert s["top_trades"][0]["amount"] == pytest.approx(5.0)  # the whale print
    assert len(s["cvd_series"]) == 50


async def test_market_trades_missing_side_is_graceful(server):
    env = await call(server, "market_trades", exchange="coinbase", symbol="BTC/USDT", limit=20)
    assert env["ok"] is False
    assert any(m["reason"] == "data_unavailable" for m in env["missing"])


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
