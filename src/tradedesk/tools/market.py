from __future__ import annotations

import math
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.orderbook import book_stats, walk_book
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r, timeframe_seconds

EX = Annotated[str, Field(description="ccxt exchange id: binance, binanceusdm (USDT perps), bybit, okx, kraken, coinbase ...")]
SYM = Annotated[str, Field(description="ccxt unified symbol. Spot 'BTC/USDT'; linear perp 'BTC/USDT:USDT'")]


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description="Latest price, bid/ask, 24h high/low/volume/change for one symbol on one exchange (via ccxt).",
    )
    async def market_ticker(exchange: EX, symbol: SYM) -> dict[str, Any]:
        b = Builder()
        t = await b.guard("ticker", f"ccxt:{exchange}", rt.exchanges.ticker(exchange, symbol))
        if t is None:
            return b.build(None, ok=False)
        data = {k: t.get(k) for k in ("symbol", "last", "bid", "ask", "high", "low", "baseVolume", "quoteVolume", "percentage", "timestamp")}
        if t.get("bid") and t.get("ask"):
            data["spread_bps"] = r((t["ask"] - t["bid"]) / ((t["ask"] + t["bid"]) / 2) * 1e4, 3)
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "OHLCV candles (oldest first, columns: ts_ms, open, high, low, close, volume) plus summary: "
            "period return and annualised close-to-close realised volatility. timeframe like 1m,5m,1h,4h,1d."
        ),
    )
    async def market_ohlcv(
        exchange: EX,
        symbol: SYM,
        timeframe: str = "1h",
        limit: Annotated[int, Field(ge=2, le=1000)] = 200,
    ) -> dict[str, Any]:
        b = Builder()
        try:
            tf_sec = timeframe_seconds(timeframe)
        except ValueError as e:
            b.miss("timeframe", "tradedesk", "config", str(e))
            return b.build(None, ok=False)
        rows = await b.guard("ohlcv", f"ccxt:{exchange}", rt.exchanges.ohlcv(exchange, symbol, timeframe, limit))
        if not rows:
            if rows is not None:
                b.miss("ohlcv", f"ccxt:{exchange}", "data_unavailable", "empty result")
            return b.build(None, ok=False)
        closes = [c[4] for c in rows if c[4]]
        summary: dict[str, Any] = {"candles": len(rows), "first_close": closes[0], "last_close": closes[-1]}
        summary["return_pct"] = r((closes[-1] / closes[0] - 1) * 100, 3)
        if len(closes) >= 10:
            lr = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
            mean = sum(lr) / len(lr)
            var = sum((x - mean) ** 2 for x in lr) / (len(lr) - 1)
            summary["realized_vol_annualized"] = r(math.sqrt(var) * math.sqrt(365 * 86400 / tf_sec), 4)
        return b.build({"columns": ["ts_ms", "open", "high", "low", "close", "volume"], "rows": rows, "summary": summary})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Order book snapshot: top levels, mid, spread (bps), bid/ask depth imbalance within 0.1/0.5/1 % of mid. "
            "If notional_usd is given, also estimates average fill price and slippage (bps vs mid) for a market buy and sell of that size."
        ),
    )
    async def market_orderbook(
        exchange: EX,
        symbol: SYM,
        depth: Annotated[int, Field(ge=5, le=500)] = 100,
        notional_usd: Annotated[float | None, Field(gt=0, description="Order size in quote currency (USD/USDT)")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        ob = await b.guard("orderbook", f"ccxt:{exchange}", rt.exchanges.order_book(exchange, symbol, depth))
        if ob is None:
            return b.build(None, ok=False)
        bids, asks = ob.get("bids", []), ob.get("asks", [])
        stats = book_stats(bids, asks)
        data: dict[str, Any] = {"symbol": symbol, "stats": stats, "bids_top": bids[:10], "asks_top": asks[:10], "levels_loaded": [len(bids), len(asks)]}
        if notional_usd and stats["mid"]:
            data["impact"] = {
                "buy": walk_book(asks, notional_usd, stats["mid"], "buy"),
                "sell": walk_book(bids, notional_usd, stats["mid"], "sell"),
            }
            if data["impact"]["buy"]["unfilled_usd"] > 0 or data["impact"]["sell"]["unfilled_usd"] > 0:
                b.warn(f"only {len(bids)}/{len(asks)} levels loaded; raise `depth` to size this order")
        return b.build(data)
