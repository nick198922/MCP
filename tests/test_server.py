import json
import math

import pytest
from mcp import Client

EXPECTED_TOOLS = {
    "system_health",
    "market_ticker",
    "market_tickers",
    "market_markets",
    "market_ohlcv",
    "chart_markup",
    "market_orderbook",
    "market_trades",
    "perps_funding_compare",
    "perps_funding_history",
    "funding_carry_scan",
    "funding_spread_scan",
    "perps_long_short_ratio",
    "perps_oi_history",
    "perps_open_interest",
    "hyperliquid_overview",
    "hyperliquid_candles",
    "hyperliquid_book",
    "hyperliquid_trades",
    "hyperliquid_funding_history",
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
    "sentiment_fear_greed",
    "dex_pairs",
    "chain_metrics",
    "pendle_fixed_yields",
    "pendle_market",
    "pendle_yield_curve",
    "liquidations_history",
    "calendar_expiries",
    "macro_calendar",
    "upbit_notices",
    "deribit_dvol",
    "deribit_vrp",
    "deribit_atm_term",
    "deribit_skew_term",
    "deribit_largest_trades",
    "perps_basis_compare",
    "polymarket_search",
    "polymarket_market",
    "polymarket_history",
    "coin_snapshot",
    "global_market",
    "top_coins",
    "iv_rank",
    "funding_zscore",
    "skew_percentile",
    "alerts_recent",
    "ohlcv_history",
    "market_regime",
    "range_detect",
    "range_setup",
    "range_setup_scan",
    "events_add",
    "events_list",
    "events_seed_load",
    "event_study",
    "journal_add",
    "journal_list",
    "journal_close",
    "journal_stats",
    "carry_paper_open",
    "carry_paper_status",
    "carry_paper_close",
    "paper_carry_status",
    "positions_add",
    "positions_list",
    "positions_close",
    "positions_risk",
    "liquidations_summary",
    "rules_add",
    "rules_list",
    "rules_test",
    "rules_disable",
    "rules_presets_load",
    "strategy_payoff",
    "strategy_straddle",
    "strategy_strangle",
    "strategy_risk_reversal",
    "strategy_iron_condor",
    "strategy_cash_and_carry",
    "strategy_funding_carry",
    "strategy_backtest_momentum",
    "strategy_scenario_matrix",
    "fib_retracement",
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


async def test_token_deep_analysis_prompt(server):
    async with Client(server) as c:
        names = {p.name for p in (await c.list_prompts()).prompts}
        assert "token_deep_analysis" in names
        res = await c.get_prompt("token_deep_analysis", {"project": "SOL"})
        text = res.messages[0].content.text
    assert "SOL" in text and "[НАЗВАНИЕ ПРОЕКТА" not in text
    assert "tradedesk-mcp" in text  # the hint block is appended


async def test_fomc_playbook_prompt(server):
    async with Client(server) as c:
        names = {p.name for p in (await c.list_prompts()).prompts}
        assert {"fomc_playbook", "pre_trade_checklist", "morning_briefing"} <= names
        res = await c.get_prompt("fomc_playbook", {"symbol": "BTC/USDT:USDT", "event_date": "2026-12-16"})
        text = res.messages[0].content.text
    assert "BTC/USDT:USDT" in text
    assert "2026-12-16" in text
    assert "{{SYMBOL}}" not in text  # all placeholders substituted


async def test_pre_trade_checklist_prompt(server):
    async with Client(server) as c:
        res = await c.get_prompt("pre_trade_checklist", {"symbol": "ETH/USDT:USDT", "side": "short", "size": "2 ETH"})
        text = res.messages[0].content.text
    assert "ETH/USDT:USDT" in text and "short" in text and "2 ETH" in text
    assert "{{" not in text  # every placeholder filled, defaults included


async def test_pre_trade_checklist_sizing_and_journal_review(server):
    async with Client(server) as c:
        res = await c.get_prompt("pre_trade_checklist", {"symbol": "SOL/USDT:USDT", "account": "$10000", "risk_pct": "0.5"})
        plan = res.messages[0].content.text
        res = await c.get_prompt("journal_review", {"days": "60"})
        review = res.messages[0].content.text
    assert "0.5 % від $10000" in plan and "journal_add" in plan and "touch_probability" in plan
    assert "upbit_notices" in plan
    assert "60 днів" in review and "journal_stats" in review and "{{" not in review


async def test_morning_briefing_prompt(server):
    async with Client(server) as c:
        res = await c.get_prompt("morning_briefing", {})
        text = res.messages[0].content.text
    # Prompt has no template placeholders — just make sure it renders and references our tools.
    assert "coin_snapshot" in text and "perps_funding_compare" in text


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


async def test_funding_history_cross_venue(server):
    env = await call(server, "perps_funding_history", base="BTC", days_back=30)
    assert env["ok"]
    by = {v["venue"]: v for v in env["data"]["venues"]}
    # ccxt venues produce 8h intervals; HL produces 1h
    assert by["binanceusdm"]["interval_hours"] == pytest.approx(8.0, abs=0.01)
    assert by["hyperliquid"]["interval_hours"] == pytest.approx(1.0, abs=0.01)
    # okx is set to raise rate_limited in the fake transport -> ends up in missing
    assert "okx" not in by
    assert any("okx" in m["provider"] for m in env["missing"])
    # highest_carry: Binance has larger base rate (0.0001) than Bybit (0.00008)
    cross = env["data"]["cross_venue"]
    assert cross["highest_carry"]["venue"] == "binanceusdm"
    assert cross["lowest_carry"]["venue"] in ("bybit", "hyperliquid")
    assert cross["biggest_abs_z"] is not None
    # no series by default
    assert "series" not in by["binanceusdm"]

    env2 = await call(server, "perps_funding_history", base="BTC", days_back=30, include_series=True, include_hyperliquid=False)
    by2 = {v["venue"]: v for v in env2["data"]["venues"]}
    assert "hyperliquid" not in by2
    assert "series" in by2["binanceusdm"] and len(by2["binanceusdm"]["series"]) > 0


async def test_funding_carry_scan_candidates_from_bulk(server):
    env = await call(server, "funding_carry_scan", venue="binanceusdm", candidates=5, require_spot=False)
    assert env["ok"]
    assert any("not risk-free" in w for w in env["warnings"])
    rows = env["data"]["rows"]
    # bulk fake has BTC/ETH/SOL crypto perps, a TradFi perp (XAU) and a spot symbol; the last two are skipped
    assert {x["symbol"] for x in rows} == {"BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"}
    eth = next(x for x in rows if x["base"] == "ETH")
    assert eth["current_apr"] == pytest.approx(0.0003 * 3 * 365, rel=1e-3)
    btc = next(x for x in rows if x["base"] == "BTC")
    assert btc["spot_available"] is True
    assert btc["trailing_apr"] == pytest.approx(0.0001 * 3 * 365, rel=0.15)
    assert btc["status"] == "between"  # fake base rate 0.0001 per 8h -> ~11 % APR, between 0 % and the 20 % default
    trailing = [x["trailing_apr"] for x in rows]
    assert trailing == sorted(trailing, reverse=True)
    sol = next(x for x in rows if x["base"] == "SOL")
    # Binance omits the interval in bulk rows; SOL is on 4h there, so 6 payments a day, not 3
    assert sol["interval_hours"] == 4.0
    assert sol["current_apr"] == pytest.approx(-0.0001 * 6 * 365, rel=1e-3)


async def test_funding_carry_scan_candidate_filters(server):
    spot_only = await call(server, "funding_carry_scan", venue="binanceusdm", candidates=5)
    assert [x["base"] for x in spot_only["data"]["rows"]] == ["BTC"]  # only BTC has a spot market in the fake
    everything = await call(server, "funding_carry_scan", venue="binanceusdm", candidates=5, crypto_only=False, require_spot=False)
    xau = next(x for x in everything["data"]["rows"] if x["base"] == "XAU")
    assert xau["tradfi"] is True
    assert xau["spot_available"] is False


async def test_funding_carry_scan_explicit_bases_and_thresholds(server):
    env = await call(server, "funding_carry_scan", venue="binanceusdm", bases=["btc"], entry_apr=0.05, exit_apr=0.0)
    assert env["ok"]
    (row,) = env["data"]["rows"]
    assert row["status"] == "above_entry"
    assert row["breakeven_days"] == pytest.approx(0.003 / (row["trailing_apr"] / 365), rel=0.01)
    assert env["data"]["summary"]["above_entry_with_spot"] == ["BTC"]


async def test_funding_carry_scan_provider_failure(server):
    env = await call(server, "funding_carry_scan", venue="okx")
    assert env["ok"] is False
    assert any(m["field"].startswith("funding_rates") for m in env["missing"])
    env2 = await call(server, "funding_carry_scan", venue="okx", bases=["BTC"])
    assert env2["ok"] is False
    assert any(m["field"].startswith("funding_history:") for m in env2["missing"])


async def test_funding_history_warns_when_venue_history_is_shorter(server):
    # the fake ccxt history spans 30 days; asking for 60 must say so instead of passing it off as the full window
    env = await call(server, "perps_funding_history", base="BTC", days_back=60, include_hyperliquid=False)
    assert env["ok"]
    assert any("later than requested" in w and "binanceusdm" in w for w in env["warnings"])


async def test_funding_spread_scan_candidates(server):
    env = await call(server, "funding_spread_scan", entry_apr=0.01)
    assert env["ok"], env["missing"]
    assert any("Backtest" in w for w in env["warnings"])
    rows = {x["base"]: x for x in env["data"]["rows"]}
    assert set(rows) == {"BTC", "ETH", "SOL"}  # XAU is on one venue only and TradFi
    btc = rows["BTC"]
    # fake history: Binance 0.0001 vs Bybit 0.00008 per 8h -> short Binance, long Bybit, ~2.2 % APR
    assert (btc["short_venue"], btc["long_venue"]) == ("binanceusdm", "bybit")
    assert btc["trailing_spread_apr"] == pytest.approx(0.00002 * 3 * 365, rel=0.15)
    assert btc["status"] == "above_entry" and btc["tradable_now"] is True
    assert btc["roundtrip_cost"] == pytest.approx(2 * (0.0005 + 0.00055 + 0.001))
    assert abs(btc["price_gap"]) < 0.001
    assert btc["venues"]["binanceusdm"]["cap"] == pytest.approx(0.003)
    # SOL's current funding points the other way (Binance -0.0001 per 4h, Bybit +0.0002 per 8h)
    assert "current_spread_reversed" in rows["SOL"]["flags"] and rows["SOL"]["tradable_now"] is False
    assert "interval_mismatch" in rows["SOL"]["flags"]
    assert "BTC" in env["data"]["summary"]["tradable_now"] and "SOL" not in env["data"]["summary"]["tradable_now"]


async def test_funding_spread_scan_explicit_bases_and_failures(server):
    env = await call(server, "funding_spread_scan", bases=["btc"], entry_apr=0.5)
    (row,) = env["data"]["rows"]
    assert row["status"] == "between" and row["tradable_now"] is False
    bad = await call(server, "funding_spread_scan", venues=["okx", "binanceusdm"])
    assert bad["ok"] is False
    assert any(m["field"] == "funding_rates:okx" for m in bad["missing"])
    one = await call(server, "funding_spread_scan", venues=["bybit", "bybit"])
    assert one["ok"] is False and one["missing"][0]["field"] == "venues"


async def test_long_short_ratio_cross_venue(server):
    env = await call(server, "perps_long_short_ratio", base="BTC", period="1h", limit=48)
    assert env["ok"]
    by = {v["venue"]: v for v in env["data"]["venues"]}
    assert "binanceusdm" in by and "bybit" in by
    # fixture: both venues long-leaning (binance 0.60..0.65, bybit 0.62..0.67)
    for v in by.values():
        assert v["latest"]["long"] > 0.5
        assert v["ratio_stats"]["count"] == 48
        assert v["ratio_stats"]["z_score_latest"] is not None
    cross = env["data"]["cross_venue"]
    # bybit starts a bit more long-biased in the fixture
    assert cross["most_long"]["venue"] == "bybit"
    assert cross["long_fraction_divergence"] is not None


async def test_oi_history_cross_venue(server):
    env = await call(server, "perps_oi_history", base="BTC", period="1h", limit=48, include_series=True)
    d = env["data"]
    by = {v["venue"]: v for v in d["venues"]}
    assert by["binanceusdm"]["latest"]["oi_usd"] > 0  # Binance exposes USD
    assert by["bybit"]["latest"]["oi_usd"] is None  # Bybit does not
    assert by["binanceusdm"]["base_stats"]["change_24h"] > 0  # fixture is rising
    assert len(by["binanceusdm"]["series"]) == 48
    assert d["cross_venue"]["biggest_gain_24h"]["venue"] in ("binanceusdm", "bybit")


async def test_open_interest(server):
    env = await call(server, "perps_open_interest", base="BTC")
    d = env["data"]
    venues = {v["venue"] for v in d["venues"]}
    assert {"binanceusdm", "bybit", "hyperliquid", "deribit"} <= venues
    assert d["total_oi_usd"] == pytest.approx(sum(v["oi_usd"] for v in d["venues"]))
    assert d["venues"][0]["oi_usd"] >= d["venues"][-1]["oi_usd"]


async def test_open_interest_fallback_via_mark(state):
    from conftest import NOW, FakeEx, make_transports
    from tradedesk.config import Settings
    from tradedesk.runtime import Runtime
    from tradedesk.server import build_server

    class FakeExNoOIValue(FakeEx):
        async def fetch_open_interest(self, symbol):
            return {"openInterestAmount": 1000.0, "openInterestValue": None}

    rt2 = Runtime(Settings(http_retries=1, cache_scale=0.0), transports=make_transports(state), exchange_factory=FakeExNoOIValue)
    rt2.now = lambda: NOW
    srv, _ = build_server(rt2)
    env = await call(srv, "perps_open_interest", base="BTC", exchanges=["binanceusdm"])
    row = next(v for v in env["data"]["venues"] if v["venue"] == "binanceusdm")
    assert row["oi_usd"] == pytest.approx(1000.0 * 82750.0)
    assert any("estimated as amount * last price" in w for w in env["warnings"])


async def test_hyperliquid_candles_has_realized_vol(server):
    env = await call(server, "hyperliquid_candles", coin="BTC", interval="1h", hours_back=24)
    d = env["data"]
    assert d["coin"] == "BTC" and d["interval"] == "1h"
    assert len(d["rows"]) == 24
    assert d["summary"]["realized_vol_annualized"] > 0
    assert d["summary"]["first_close"] > 0 and d["summary"]["last_close"] > 0


async def test_hyperliquid_book_stats_and_impact(server):
    env = await call(server, "hyperliquid_book", coin="BTC", notional_usd=200_000)
    d = env["data"]
    assert d["stats"]["mid"] > 0 and d["stats"]["spread_bps"] > 0
    assert d["levels_loaded"] == [20, 20]
    assert d["impact"]["buy"]["slippage_bps_vs_mid"] > 0
    assert d["impact"]["sell"]["slippage_bps_vs_mid"] > 0


async def test_hyperliquid_trades_flow(server):
    env = await call(server, "hyperliquid_trades", coin="BTC", top_n=3)
    s = env["data"]["summary"]
    assert s["count"] == 15 and s["buy_count"] + s["sell_count"] == 15
    assert s["vwap"] > 0
    assert -1 <= s["imbalance"] <= 1
    assert s["top_trades"][0]["amount"] == pytest.approx(2.0)  # the big initial B print


async def test_hyperliquid_funding_history_stats(server):
    env = await call(server, "hyperliquid_funding_history", coin="BTC", days_back=7)
    s = env["data"]["stats"]
    assert s["count"] == 168
    assert s["interval_hours"] == 1.0
    assert s["mean_rate"] is not None
    assert s["carry_annualized"] == pytest.approx(s["mean_rate"] * 8760)
    assert s["polarity_flips"] >= 1  # fixture alternates sign heavily
    assert s["z_score_latest"] is not None
    assert 0.0 <= s["percentile_latest"] <= 1.0


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


async def test_sentiment_fear_greed(server):
    env = await call(server, "sentiment_fear_greed", days=30)
    d = env["data"]
    assert d["count"] == 30
    assert d["latest"]["value"] == 27 and d["latest"]["zone"] == "fear"
    assert d["min"] == 20 and d["max"] == 80
    assert d["mean_7d"] is not None and d["mean_30d"] is not None
    # i=0 is today; i=7 is 7 days ago. Fixture values[7] = 66 → change_7d = 27 - 66 = -39
    assert d["change_7d"] == -39
    assert d["change_30d"] is None or isinstance(d["change_30d"], int)
    shares = d["zone_share"]
    assert abs(sum(shares.values()) - 1.0) < 1e-9
    assert len(d["series"]) == 30 and d["series"][0]["ts"] < d["series"][-1]["ts"]


async def test_sentiment_fear_greed_provider_failure(state):
    import httpx

    from conftest import NOW, FakeEx, make_transports
    from tradedesk.config import Settings
    from tradedesk.runtime import Runtime
    from tradedesk.server import build_server

    tr = make_transports(state)
    tr["alternative_me"] = httpx.MockTransport(lambda req: httpx.Response(503, text="down"))
    rt2 = Runtime(Settings(http_retries=1, cache_scale=0.0), transports=tr, exchange_factory=FakeEx)
    rt2.now = lambda: NOW
    srv, _ = build_server(rt2)
    env = await call(srv, "sentiment_fear_greed", days=30)
    assert env["ok"] is False
    assert any(m["provider"] == "alternative_me" for m in env["missing"])


async def test_calendar_expiries(server):
    env = await call(server, "calendar_expiries", currencies=["BTC"], horizon_days=180, funding_hours_ahead=24)
    d = env["data"]
    btc = d["deribit_expiries"]["BTC"]
    kinds = {e["kind"] for e in btc}
    assert "monthly" in kinds and "quarterly" in kinds
    # fixture expiries: 2026-10-30 (monthly), 2026-11-27 (monthly), 2027-03-26 (quarterly)
    assert len(btc) == 3
    assert btc[0]["days_to_expiry"] < btc[-1]["days_to_expiry"]

    fs = d["funding_schedule"]
    venues = {r["venue"] for r in fs}
    assert {"binanceusdm", "bybit", "okx", "hyperliquid"} <= venues
    assert all(r["ts"] > d["now"] for r in fs)

    macro = d["macro_events"]
    assert len(macro) >= 2  # CPI 2026-10-15 and FOMC 2026-10-28 within 180d of 2026-10-10
    assert all(0 <= m["days_ahead"] <= 180 for m in macro)
    assert {m["kind"] for m in macro} & {"cpi", "fomc", "nfp"}


async def test_calendar_classify_expiry():
    from datetime import UTC, datetime

    from tradedesk.analytics.calendar import classify_expiry

    now = datetime(2026, 10, 10, 13, tzinfo=UTC)
    assert classify_expiry(datetime(2026, 10, 30, 8, tzinfo=UTC), now) == "monthly"
    assert classify_expiry(datetime(2027, 3, 26, 8, tzinfo=UTC), now) == "quarterly"
    assert classify_expiry(datetime(2026, 10, 16, 8, tzinfo=UTC), now) == "weekly"  # third Friday, not last
    assert classify_expiry(datetime(2026, 10, 11, 8, tzinfo=UTC), now) == "daily"  # Sunday


async def test_deribit_vrp(server):
    env = await call(server, "deribit_vrp", currency="BTC", history_days=200)
    assert env["ok"], env["missing"]
    d = env["data"]
    assert any("Backtest" in w for w in env["warnings"])
    assert d["history"]["days"] >= 190
    cur = d["current"]
    # fake spot zigzags +-1.5 % a day -> realized ~ 0.015 * sqrt(365) ~ 28.7 %; fake DVOL drifts up from 45 %
    assert cur["rv_30d"] == pytest.approx(0.015 * math.sqrt(365), rel=0.02)
    assert cur["dvol"] > cur["rv_30d"]
    assert cur["dvol_percentile"] > 0.9  # rising series -> latest is near the top
    assert d["premium"]["share_dvol_above_realized"] == pytest.approx(1.0)
    hs = d["hedged_short_straddle"]
    assert hs["mean_pnl_all_starts"] > 0
    assert hs["non_overlapping"]["n"] >= 4
    assert "by_dvol_level" not in d  # 200 days < 365-day percentile window
    env2 = await call(server, "deribit_vrp", currency="BTC", history_days=500)
    levels = env2["data"]["by_dvol_level"]
    assert len(levels) == 5 and sum(1 for x in levels if x["current"]) == 1
    assert env2["data"]["current"]["dvol_percentile_365d"] == pytest.approx(1.0)  # fake DVOL only rises


async def test_deribit_dvol(server):
    env = await call(server, "deribit_dvol", currency="BTC", hours_back=168, resolution=3600)
    d = env["data"]
    assert d["currency"] == "BTC"
    assert d["resolution_seconds"] == 3600
    assert d["count"] >= 168
    # fixture has +0.1/hr drift → change_7d_pts positive and large
    assert d["change_7d_pts"] > 10
    assert d["mean_pct"] > 0 and d["max_pct"] >= d["min_pct"]
    assert d["latest"]["value_pct"] >= d["mean_pct"]  # rising series


async def test_deribit_atm_term(server):
    env = await call(server, "deribit_atm_term", currency="BTC", min_days=0, max_days=400)
    d = env["data"]
    assert d["currency"] == "BTC"
    assert len(d["expiries"]) == 3  # fixture: 30OCT26, 27NOV26, 26MAR27
    assert d["expiries"][0]["days"] < d["expiries"][-1]["days"]
    # SMILE["base"] = 0.40 → each ATM IV should be close to 0.40
    for row in d["expiries"]:
        assert 0.35 < row["atm_iv"] < 0.45
        assert row["forward"] > 0 and row["strikes"] >= 3
    assert d["shape"] in ("contango", "backwardation", "flat")


async def test_coin_snapshot(server):
    env = await call(server, "coin_snapshot", coin_id="bitcoin")
    d = env["data"]
    assert d["id"] == "bitcoin" and d["symbol"] == "bit"  # fake uses first-3 as symbol
    assert d["price"] == 82750.0
    assert d["max_supply"] == 21_000_000
    assert d["ath_pct_vs_current"] == -23.4
    assert d["change_24h_pct"] == 1.2 and d["change_30d_pct"] == 8.5
    assert d["market_cap_rank"] == 1


async def test_global_market(server):
    env = await call(server, "global_market")
    d = env["data"]
    assert d["btc_dominance_pct"] == 55.5
    assert d["eth_dominance_pct"] == 15.2
    assert d["altcoin_dominance_pct"] == pytest.approx(100 - 55.5 - 15.2, abs=1e-6)
    assert d["active_coins"] == 14500
    assert d["total_mcap_usd"] == 2_500_000_000_000


async def test_top_coins(server):
    env = await call(server, "top_coins", limit=50)
    d = env["data"]
    assert d["count"] == 2 and d["coins"][0]["id"] == "bitcoin"
    assert d["coins"][0]["rank"] == 1
    assert d["coins"][1]["change_24h_pct"] == -3.4


async def test_coingecko_provider_failure(state):
    import httpx

    from conftest import NOW, FakeEx, make_transports
    from tradedesk.config import Settings
    from tradedesk.runtime import Runtime
    from tradedesk.server import build_server

    tr = make_transports(state)
    tr["coingecko"] = httpx.MockTransport(lambda req: httpx.Response(503, text="down"))
    rt2 = Runtime(Settings(http_retries=1, cache_scale=0.0), transports=tr, exchange_factory=FakeEx)
    rt2.now = lambda: NOW
    srv, _ = build_server(rt2)
    env = await call(srv, "coin_snapshot", coin_id="bitcoin")
    assert env["ok"] is False
    assert any(m["provider"] == "coingecko" for m in env["missing"])


async def test_polymarket_search(server):
    env = await call(server, "polymarket_search", query="btc", limit=5)
    d = env["data"]
    assert d["count"] == 1
    m = d["markets"][0]
    assert m["slug"].startswith("will-btc")
    assert m["active"] is True
    # raw prices sum to 1.02 (0.62 + 0.40) → de-vigged sums to 1.0
    s = sum(o["price_devigged"] for o in m["outcomes"])
    assert abs(s - 1.0) < 1e-9
    assert m["volume_usd"] > 0


async def test_polymarket_market_with_books(server):
    env = await call(server, "polymarket_market", market_id="516700")
    m = env["data"]["market"]
    yes = next(o for o in m["outcomes"] if o["name"] == "Yes")
    assert yes["book"]["best_bid"] == 0.618 and yes["book"]["best_ask"] == 0.622
    assert yes["book"]["mid"] == 0.62 and yes["book"]["spread"] == pytest.approx(0.004)


async def test_polymarket_history(server):
    env = await call(server, "polymarket_history", token_id="TOK_YES", interval="1d")
    d = env["data"]
    assert d["count"] == 24
    assert d["latest"]["p"] > d["series"][0]["p"]  # fixture is monotonically rising
    assert d["change_pct"] > 0


async def test_polymarket_provider_failure(state):
    import httpx

    from conftest import NOW, FakeEx, make_transports
    from tradedesk.config import Settings
    from tradedesk.runtime import Runtime
    from tradedesk.server import build_server

    tr = make_transports(state)
    tr["polymarket:gamma"] = httpx.MockTransport(lambda req: httpx.Response(502, text="down"))
    rt2 = Runtime(Settings(http_retries=1, cache_scale=0.0), transports=tr, exchange_factory=FakeEx)
    rt2.now = lambda: NOW
    srv, _ = build_server(rt2)
    env = await call(srv, "polymarket_search", query="x")
    assert env["ok"] is False
    assert any(m["provider"] == "polymarket:gamma" for m in env["missing"])


async def test_perps_basis_compare(server):
    env = await call(server, "perps_basis_compare", base="BTC")
    d = env["data"]
    venues = {p["venue"] for p in d["perps"]}
    assert {"binanceusdm", "hyperliquid", "deribit"} <= venues
    for p in d["perps"]:
        assert p["mark"] is not None and p["index"] is not None
        assert p["premium_bps"] is not None
    # perps sorted by premium_bps desc
    bps = [p["premium_bps"] for p in d["perps"]]
    assert bps == sorted(bps, reverse=True)
    s = d["summary"]
    assert s["cross_venue_spread_bps"] >= 0
    assert s["highest_premium"]["venue"] == d["perps"][0]["venue"]
    # Deribit futures fixture has one dated contract
    assert len(d["deribit_dated_futures"]) >= 1
    assert s["nearest_dated_annualized"] is not None


async def test_deribit_largest_trades(server):
    env = await call(server, "deribit_largest_trades", currency="BTC", kind="option", count=50, top_n=5)
    d = env["data"]
    assert d["kind"] == "option"
    s = d["summary"]
    assert s["fetched"] == 10 and s["kept"] == 10
    assert s["block_count"] == 2 and 0 < s["block_share"] < 1
    # fixture: 5 buy / 5 sell; largest trade is the last (biggest amount) which is odd-index → sell
    assert s["buy_notional_usd"] > 0 and s["sell_notional_usd"] > 0
    top = d["top_trades"]
    assert len(top) == 5
    assert top[0]["notional_usd"] >= top[-1]["notional_usd"]

    env_blk = await call(server, "deribit_largest_trades", currency="BTC", kind="option", count=50, only_blocks=True, top_n=5)
    assert env_blk["data"]["summary"]["kept"] == 2
    assert all(t["is_block"] for t in env_blk["data"]["top_trades"])


async def test_deribit_skew_term(server):
    env = await call(server, "deribit_skew_term", currency="BTC", target_delta=0.25, min_days=0, max_days=400)
    d = env["data"]
    assert d["target_delta"] == 0.25
    assert len(d["expiries"]) == 3
    # SMILE['skew'] = -0.25 (negative) → puts richer → RR negative on every expiry
    for row in d["expiries"]:
        assert row["rr"] < 0
        assert row["fly"] > 0  # curv > 0 → wings above ATM
    assert d["most_extreme"]["rr"] <= min(r["rr"] for r in d["expiries"])
    assert d["rr_shape"] in ("flattening", "steepening", "stable")


async def test_chart_markup_tool(server):
    env = await call(server, "chart_markup", exchange="binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h", limit=300)
    assert env["ok"] is True
    d = env["data"]
    assert d["bars"] == 300 and d["price"] > 0 and d["atr14"] > 0
    assert set(d["emas"]) == {"trend_13_21_34", "daily_4h_100_200", "weekly_1d_100_200"}
    assert d["emas"]["trend_13_21_34"]["ema34"] is not None
    assert d["volume_profile"]["val"] <= d["volume_profile"]["poc"] <= d["volume_profile"]["vah"]
    assert "asia_session" in d  # 1h chart: the session block is present (may be null outside the window)
    assert any("not signals" in w for w in env["warnings"])


async def test_chart_markup_provider_failure(server):
    env = await call(server, "chart_markup", exchange="binanceusdm", symbol="DOWN/USDT:USDT", timeframe="4h")
    assert env["ok"] is False and env["missing"][0]["reason"] == "network"


async def test_chart_markup_bad_timeframe(server):
    env = await call(server, "chart_markup", exchange="binanceusdm", symbol="BTC/USDT:USDT", timeframe="4x")
    assert env["ok"] is False and env["missing"][0]["reason"] == "config"
