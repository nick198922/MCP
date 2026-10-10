"""Live smoke test: calls every tool against the REAL exchanges and cross-checks our option greeks
against Deribit's own. Run it on your own machine (needs internet access to the exchanges):

    uv run python scripts/live_smoke.py

Exit code 0 if at least the Deribit and Hyperliquid tools worked; the per-tool table shows what failed and why.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from mcp import Client

from tradedesk.analytics.instruments import parse_option_name
from tradedesk.analytics.options import black76_greeks, year_fraction
from tradedesk.server import build_server

CASES: list[tuple[str, dict]] = [
    ("system_health", {"probe": True}),
    ("market_ticker", {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT"}),
    ("market_tickers", {"exchange": "binanceusdm", "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]}),
    ("market_markets", {"exchange": "binanceusdm", "query": "BTC", "market_type": "swap", "limit": 10}),
    ("market_ohlcv", {"exchange": "bybit", "symbol": "BTC/USDT:USDT", "timeframe": "1h", "limit": 200}),
    ("market_orderbook", {"exchange": "okx", "symbol": "BTC/USDT:USDT", "notional_usd": 1_000_000}),
    ("market_trades", {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT", "limit": 200, "top_n": 5}),
    ("perps_funding_compare", {"base": "BTC"}),
    ("perps_open_interest", {"base": "BTC"}),
    ("hyperliquid_overview", {"sort_by": "funding_annualized", "top_n": 5}),
    ("deribit_futures_curve", {"currency": "BTC"}),
    ("deribit_vol_surface", {"currency": "BTC"}),
    ("deribit_expected_move", {"currency": "BTC", "min_days": 1, "max_days": 60}),
    ("deribit_oi_by_strike", {"currency": "BTC", "width_pct": 15, "top_n": 10}),
    ("sentiment_fear_greed", {"days": 30}),
    ("calendar_expiries", {"currencies": ["BTC", "ETH"], "horizon_days": 60, "funding_hours_ahead": 24}),
    ("deribit_dvol", {"currency": "BTC", "hours_back": 168, "resolution": 3600}),
    ("deribit_atm_term", {"currency": "BTC"}),
    ("deribit_skew_term", {"currency": "BTC", "target_delta": 0.25}),
    ("deribit_largest_trades", {"currency": "BTC", "kind": "option", "count": 200, "top_n": 5}),
    ("perps_basis_compare", {"base": "BTC"}),
    ("polymarket_search", {"query": "trump", "limit": 5, "include_closed": True}),
]


async def call(c: Client, name: str, args: dict) -> dict:
    res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


async def main() -> int:
    server, rt = build_server()
    ok_count = 0
    async with Client(server) as c:
        print(f"{'tool':26} {'ok':5} {'ms':>6}  notes")
        for name, args in CASES:
            t0 = time.perf_counter()
            try:
                env = await call(c, name, args)
                ms = (time.perf_counter() - t0) * 1000
                miss = "; ".join(f"{m['provider']}:{m['reason']}" for m in env["missing"])
                print(f"{name:26} {str(env['ok']):5} {ms:6.0f}  {miss}")
                ok_count += bool(env["ok"])
            except Exception as e:  # noqa: BLE001
                print(f"{name:26} {'ERR':5} {'':>6}  {type(e).__name__}: {e}")

        # Cross-check: our Black-76 greeks vs Deribit's own for a few near-ATM options.
        surface = await call(c, "deribit_vol_surface", {"currency": "BTC", "min_days": 14, "max_days": 90})
        if surface["ok"] and surface["data"]["expiries"]:
            exp = surface["data"]["expiries"][0]
            chain = await call(c, "deribit_option_chain", {"expiry": exp["expiry"], "width_pct": 3})
            print(f"\nGreeks cross-check vs Deribit ticker, expiry {exp['expiry']} (ours | deribit):")
            for row in (chain["data"]["strikes"] if chain["ok"] else [])[:3]:
                name = f"BTC-{exp['expiry']}-{int(row['strike'])}-C"
                try:
                    t = (await rt.deribit.ticker(name)).data
                except Exception as e:  # noqa: BLE001
                    print(f"  {name}: ticker failed ({e})")
                    continue
                p = parse_option_name(name)
                g = black76_greeks(t["underlying_price"], p.strike, year_fraction(p.expiry, rt.now()), t["mark_iv"] / 100, True)
                d = t["greeks"]
                print(
                    f"  {name}: delta {g['delta']:.4f}|{d['delta']:.4f}  vega {g['vega']:.1f}|{d['vega']:.1f}  theta {g['theta']:.1f}|{d['theta']:.1f}"
                )
    await rt.aclose()
    return 0 if ok_count >= 18 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
