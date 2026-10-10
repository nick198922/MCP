from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.orderbook import book_stats, walk_book
from ..analytics.trades import summarise as summarise_trades
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r, timeframe_seconds

EX = Annotated[str, Field(description="ccxt exchange id: binance, binanceusdm (USDT perps), bybit, okx, kraken, coinbase ...")]
SYM = Annotated[str, Field(description="ccxt unified symbol. Spot 'BTC/USDT'; linear perp 'BTC/USDT:USDT'")]
MarketType = Literal["spot", "swap", "future", "option", "any"]


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
        data = {
            k: t.get(k) for k in ("symbol", "last", "bid", "ask", "high", "low", "baseVolume", "quoteVolume", "percentage", "timestamp")
        }
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
        data: dict[str, Any] = {
            "symbol": symbol,
            "stats": stats,
            "bids_top": bids[:10],
            "asks_top": asks[:10],
            "levels_loaded": [len(bids), len(asks)],
        }
        if notional_usd and stats["mid"]:
            data["impact"] = {
                "buy": walk_book(asks, notional_usd, stats["mid"], "buy"),
                "sell": walk_book(bids, notional_usd, stats["mid"], "sell"),
            }
            if data["impact"]["buy"]["unfilled_usd"] > 0 or data["impact"]["sell"]["unfilled_usd"] > 0:
                b.warn(f"only {len(bids)}/{len(asks)} levels loaded; raise `depth` to size this order")
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Batch tickers for several symbols on one exchange (ccxt fetch_tickers). "
            "Returns slim ticker rows sorted by 24h quote volume (desc), the top 3 movers by 24h percent change, "
            "and spread_bps when bid/ask are present. Pass `symbols` to limit the batch; omit for the exchange default set "
            "(some venues reject that — the error is reported in `missing`)."
        ),
    )
    async def market_tickers(
        exchange: EX,
        symbols: Annotated[list[str] | None, Field(description="ccxt unified symbols. Omit for the full default set.")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Max rows kept after sorting by quote volume.")] = 50,
    ) -> dict[str, Any]:
        b = Builder()
        tk = await b.guard("tickers", f"ccxt:{exchange}", rt.exchanges.tickers(exchange, symbols))
        if not tk:
            if tk is not None:
                b.miss("tickers", f"ccxt:{exchange}", "data_unavailable", "empty result")
            return b.build(None, ok=False)
        rows: list[dict[str, Any]] = []
        for sym, t in tk.items():
            if not isinstance(t, dict):
                continue
            bid, ask = t.get("bid"), t.get("ask")
            row = {
                "symbol": sym,
                "last": t.get("last"),
                "bid": bid,
                "ask": ask,
                "high": t.get("high"),
                "low": t.get("low"),
                "baseVolume": t.get("baseVolume"),
                "quoteVolume": t.get("quoteVolume"),
                "percentage": t.get("percentage"),
                "timestamp": t.get("timestamp"),
            }
            if bid and ask:
                row["spread_bps"] = r((ask - bid) / ((ask + bid) / 2) * 1e4, 3)
            rows.append(row)
        rows.sort(key=lambda x: x.get("quoteVolume") or 0.0, reverse=True)
        with_pct = [x for x in rows if isinstance(x.get("percentage"), (int, float))]
        top_movers = sorted(with_pct, key=lambda x: abs(x["percentage"]), reverse=True)[:3]
        data = {
            "count": len(rows),
            "returned": min(limit, len(rows)),
            "tickers": rows[:limit],
            "top_movers": [{"symbol": x["symbol"], "percentage": x["percentage"], "last": x["last"]} for x in top_movers],
        }
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Search tradable markets on one exchange (ccxt load_markets). Filter by substring (`query`, case-insensitive on symbol/id/base/quote), "
            "`market_type` (spot/swap/future/option/any), and `active_only`. Returns slim rows: symbol, type, base, quote, settle, linear/inverse, "
            "contractSize, active, plus expiry/strike/optionType when the venue lists them. Markets rarely change (cached ~1h)."
        ),
    )
    async def market_markets(
        exchange: EX,
        query: Annotated[str | None, Field(description="Substring match on symbol/id/base/quote (case-insensitive).")] = None,
        market_type: MarketType = "any",
        active_only: bool = True,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        b = Builder()
        mk = await b.guard("markets", f"ccxt:{exchange}", rt.exchanges.markets(exchange))
        if not mk:
            if mk is not None:
                b.miss("markets", f"ccxt:{exchange}", "data_unavailable", "empty result")
            return b.build(None, ok=False)
        q = (query or "").lower().strip() or None
        picked: list[dict[str, Any]] = []
        for sym, m in mk.items():
            if not isinstance(m, dict):
                continue
            if active_only and m.get("active") is False:
                continue
            if market_type != "any" and m.get("type") != market_type:
                continue
            if q:
                hay = " ".join(str(m.get(k) or "") for k in ("symbol", "id", "base", "quote")).lower()
                if q not in hay:
                    continue
            row = {
                "symbol": m.get("symbol") or sym,
                "id": m.get("id"),
                "type": m.get("type"),
                "base": m.get("base"),
                "quote": m.get("quote"),
                "settle": m.get("settle"),
                "linear": m.get("linear"),
                "inverse": m.get("inverse"),
                "contractSize": m.get("contractSize"),
                "active": m.get("active"),
            }
            if m.get("expiry"):
                row["expiry"] = m.get("expiry")
                row["expiryDatetime"] = m.get("expiryDatetime")
            if m.get("strike") is not None:
                row["strike"] = m.get("strike")
            if m.get("optionType"):
                row["optionType"] = m.get("optionType")
            picked.append(row)
        picked.sort(key=lambda x: str(x["symbol"]))
        if len(picked) > limit:
            b.warn(f"matched {len(picked)} markets; returning first {limit}. Narrow `query` or raise `limit`.")
        return b.build({"total_markets": len(mk), "matched": len(picked), "markets": picked[:limit]})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Recent public trades (ccxt fetch_trades) with aggressor-signed flow: buy/sell counts and notional, delta, "
            "imbalance in [-1, 1] (positive = net taker buys), VWAP for all / buys / sells, cumulative delta end value, "
            "and the largest `top_n` prints. `side` from ccxt is the taker side; venues that do not expose it are reported in `warnings`."
        ),
    )
    async def market_trades(
        exchange: EX,
        symbol: SYM,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
        top_n: Annotated[int, Field(ge=1, le=50)] = 5,
    ) -> dict[str, Any]:
        b = Builder()
        trades = await b.guard("trades", f"ccxt:{exchange}", rt.exchanges.trades(exchange, symbol, limit))
        if not trades:
            if trades is not None:
                b.miss("trades", f"ccxt:{exchange}", "data_unavailable", "empty result")
            return b.build(None, ok=False)
        with_side = sum(1 for t in trades if isinstance(t, dict) and t.get("side"))
        summary = summarise_trades(trades, top_n=top_n)
        if summary.get("count", 0) == 0:
            b.miss("trades", f"ccxt:{exchange}", "data_unavailable", "no usable trades (missing price/amount/side)")
            return b.build(None, ok=False)
        if with_side < len(trades):
            b.warn(f"{len(trades) - with_side}/{len(trades)} trades had no aggressor side and were skipped")
        return b.build({"symbol": symbol, "fetched": len(trades), "summary": summary})
