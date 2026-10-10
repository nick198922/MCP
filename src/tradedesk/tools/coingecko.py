from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Coin snapshot from CoinGecko by coin id (e.g. 'bitcoin', 'ethereum', 'solana'): "
            "price, market cap, FDV, circulating/total/max supply, ATH / ATL and distance from "
            "them (as %), 24h / 7d / 30d price change. USD unless the caller changes vs_currency "
            "(decimals, 10 = 10 %). Call with the CoinGecko id, not the ticker."
        ),
    )
    async def coin_snapshot(
        coin_id: Annotated[str, Field(description="CoinGecko coin id, e.g. 'bitcoin' (not 'BTC').")],
        vs_currency: Annotated[str, Field(description="Fiat/base, 'usd' by default.")] = "usd",
    ) -> dict[str, Any]:
        b = Builder()
        raw = await b.guard(f"coin:{coin_id}", "coingecko", rt.coingecko.coin(coin_id))
        if raw is None:
            return b.build(None, ok=False)
        md = raw.get("market_data") or {}

        def one(key: str) -> float | None:
            v = md.get(key)
            return _f(v.get(vs_currency)) if isinstance(v, dict) else None

        price = one("current_price")
        ath = one("ath")
        atl = one("atl")
        return b.build(
            {
                "id": raw.get("id"),
                "symbol": raw.get("symbol"),
                "name": raw.get("name"),
                "vs_currency": vs_currency,
                "price": r(price, 6),
                "market_cap": r(one("market_cap"), 0),
                "fdv": r(one("fully_diluted_valuation"), 0),
                "volume_24h": r(one("total_volume"), 0),
                "circulating_supply": _f(md.get("circulating_supply")),
                "total_supply": _f(md.get("total_supply")),
                "max_supply": _f(md.get("max_supply")),
                "ath": r(ath, 6),
                "ath_pct_vs_current": r(_f(md.get("ath_change_percentage", {}).get(vs_currency)), 2),
                "atl": r(atl, 6),
                "atl_pct_vs_current": r(_f(md.get("atl_change_percentage", {}).get(vs_currency)), 2),
                "change_24h_pct": r(_f(md.get("price_change_percentage_24h")), 3),
                "change_7d_pct": r(_f(md.get("price_change_percentage_7d")), 3),
                "change_30d_pct": r(_f(md.get("price_change_percentage_30d")), 3),
                "market_cap_rank": md.get("market_cap_rank") or raw.get("market_cap_rank"),
                "last_updated": md.get("last_updated"),
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Crypto market overview via CoinGecko /global: total market cap and 24h volume (USD), "
            "BTC / ETH / altcoin dominance (as %), number of active coins and markets, 24h total "
            "market cap change (%). altcoin_dominance = 100 - btc_dominance - eth_dominance."
        ),
    )
    async def global_market() -> dict[str, Any]:
        b = Builder()
        g = await b.guard("global", "coingecko", rt.coingecko.global_market())
        if g is None:
            return b.build(None, ok=False)
        mcap = (g.get("total_market_cap") or {}).get("usd")
        vol = (g.get("total_volume") or {}).get("usd")
        dom = g.get("market_cap_percentage") or {}
        btc_dom = _f(dom.get("btc"))
        eth_dom = _f(dom.get("eth"))
        alt_dom = (100.0 - (btc_dom or 0) - (eth_dom or 0)) if btc_dom is not None and eth_dom is not None else None
        return b.build(
            {
                "total_mcap_usd": r(_f(mcap), 0),
                "total_volume_usd": r(_f(vol), 0),
                "btc_dominance_pct": r(btc_dom, 3),
                "eth_dominance_pct": r(eth_dom, 3),
                "altcoin_dominance_pct": r(alt_dom, 3),
                "mcap_change_24h_pct": r(_f(g.get("market_cap_change_percentage_24h_usd")), 3),
                "active_coins": g.get("active_cryptocurrencies"),
                "upcoming_icos": g.get("upcoming_icos"),
                "ongoing_icos": g.get("ongoing_icos"),
                "ended_icos": g.get("ended_icos"),
                "markets": g.get("markets"),
                "updated_at": g.get("updated_at"),
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Top coins by market cap via CoinGecko /coins/markets. Each row has price, 24h/7d/30d "
            "price change (%), market cap, FDV, 24h volume, circulating supply and rank. Use for "
            "ranking screens; `limit` is clamped to 250 per CoinGecko free-tier limits per page."
        ),
    )
    async def top_coins(
        limit: Annotated[int, Field(ge=1, le=250, description="Rows to return (max 250 per page).")] = 20,
        vs_currency: str = "usd",
        page: Annotated[int, Field(ge=1, le=20)] = 1,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await b.guard("markets", "coingecko", rt.coingecko.coins_markets(vs_currency, limit, page))
        if not rows:
            if rows is not None:
                b.miss("markets", "coingecko", "data_unavailable", "empty response")
            return b.build(None, ok=False)
        out = []
        for coin in rows:
            if not isinstance(coin, dict):
                continue
            out.append(
                {
                    "rank": coin.get("market_cap_rank"),
                    "id": coin.get("id"),
                    "symbol": coin.get("symbol"),
                    "name": coin.get("name"),
                    "price": _f(coin.get("current_price")),
                    "market_cap": _f(coin.get("market_cap")),
                    "fdv": _f(coin.get("fully_diluted_valuation")),
                    "volume_24h": _f(coin.get("total_volume")),
                    "circulating_supply": _f(coin.get("circulating_supply")),
                    "change_24h_pct": r(
                        _f(coin.get("price_change_percentage_24h_in_currency") or coin.get("price_change_percentage_24h")), 3
                    ),
                    "change_7d_pct": r(_f(coin.get("price_change_percentage_7d_in_currency")), 3),
                    "change_30d_pct": r(_f(coin.get("price_change_percentage_30d_in_currency")), 3),
                }
            )
        return b.build({"vs_currency": vs_currency, "page": page, "count": len(out), "coins": out})
