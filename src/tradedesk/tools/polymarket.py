from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.polymarket import book_stats, normalise_market
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Polymarket search by free-text query over markets (Gamma API). Returns the "
            "top markets by volume with question, outcomes with raw and de-vigged prices "
            "(prices sum to 1 after de-vig), USD volume and liquidity, end date and active/"
            "closed flags. Yes-outcome price ~ implied probability. active_only=True keeps "
            "only currently tradable markets."
        ),
    )
    async def polymarket_search(
        query: Annotated[str | None, Field(description="Free-text search (e.g. 'bitcoin').")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        active_only: bool = True,
        include_closed: Annotated[bool, Field(description="Also return markets that already resolved.")] = False,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await b.guard("search", "polymarket:gamma", rt.polymarket.search(query, limit, active_only, include_closed))
        if rows is None:
            return b.build(None, ok=False)
        out = [normalise_market(m) for m in rows if isinstance(m, dict)]
        return b.build({"query": query, "count": len(out), "markets": out})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Polymarket market card by numeric id or slug: metadata plus a mid/spread/depth "
            "snapshot from the CLOB order book for each outcome. `price_devigged` is each "
            "outcome's price rescaled so the outcomes sum to 1. For binary markets the mid of "
            "the Yes-outcome book is a cleaner probability estimate than the last trade price."
        ),
    )
    async def polymarket_market(
        market_id: Annotated[str, Field(description="Numeric market id (e.g. '516700') or slug (e.g. 'will-x-happen').")],
    ) -> dict[str, Any]:
        b = Builder()
        raw = await b.guard("market", "polymarket:gamma", rt.polymarket.market(market_id))
        if raw is None:
            return b.build(None, ok=False)
        m = normalise_market(raw)

        async def fetch_book(token: str) -> tuple[str, dict[str, Any] | None]:
            book = await b.guard(f"book:{token[:8]}", "polymarket:clob", rt.polymarket.book(token))
            return token, (book_stats(book) if book else None)

        tokens = [o["token_id"] for o in m["outcomes"] if o.get("token_id")]
        if tokens:
            books = dict(await asyncio.gather(*[fetch_book(t) for t in tokens]))
            for o in m["outcomes"]:
                stats = books.get(o.get("token_id"))
                if stats:
                    o["book"] = {
                        "best_bid": r(stats["best_bid"], 4),
                        "best_ask": r(stats["best_ask"], 4),
                        "mid": r(stats["mid"], 4),
                        "spread": r(stats["spread"], 4),
                        "bid_depth": r(stats["bid_depth"], 2),
                        "ask_depth": r(stats["ask_depth"], 2),
                    }
        return b.build({"market": m})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Polymarket price history for ONE outcome (identified by its CLOB token id). "
            "Interval controls the window: '1h', '6h', '1d', '1w', '1m', 'max'. fidelity sets "
            "the sampling period in minutes (optional; API picks a default). Returns the raw "
            "series (t, p) plus latest/mean/min/max/stdev and change_pct vs first point."
        ),
    )
    async def polymarket_history(
        token_id: Annotated[str, Field(description="clobTokenId of the outcome (from polymarket_market).")],
        interval: Literal["1h", "6h", "1d", "1w", "1m", "max"] = "1d",
        fidelity: Annotated[int | None, Field(ge=1, le=1440, description="Sampling period in minutes.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await b.guard("history", "polymarket:clob", rt.polymarket.price_history(token_id, interval, fidelity))
        if not rows:
            if rows is not None:
                b.miss("history", "polymarket:clob", "data_unavailable", "empty history")
            return b.build(None, ok=False)
        series: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            try:
                series.append({"t": int(item["t"]), "p": float(item["p"])})
            except (KeyError, TypeError, ValueError):
                continue
        if not series:
            b.miss("history", "polymarket:clob", "data_unavailable", "no parseable points")
            return b.build(None, ok=False)
        series.sort(key=lambda x: x["t"])
        prices = [s["p"] for s in series]
        n = len(prices)
        mean = sum(prices) / n
        var = sum((p - mean) ** 2 for p in prices) / (n - 1) if n > 1 else 0.0
        import math  # noqa: PLC0415

        stdev = math.sqrt(var)
        latest, first = prices[-1], prices[0]
        change_pct = (latest / first - 1) if first > 0 else None
        return b.build(
            {
                "token_id": token_id,
                "interval": interval,
                "count": n,
                "latest": {"t": series[-1]["t"], "p": r(latest, 4)},
                "mean": r(mean, 4),
                "stdev": r(stdev, 4),
                "min": r(min(prices), 4),
                "max": r(max(prices), 4),
                "change_pct": r(change_pct, 4) if change_pct is not None else None,
                "series": [{"t": s["t"], "p": r(s["p"], 4)} for s in series],
            }
        )
