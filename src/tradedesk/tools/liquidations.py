"""Summary over the locally-stored liquidation feed."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Rolling liquidation summary over the local store. Needs the liquidations collector "
            "running (TRADEDESK_COLLECT_LIQUIDATIONS_ENABLED=1). `window_minutes` picks the "
            "trailing window; `venue` / `symbol` optionally narrow it. "
            "Side convention follows the taker in the force-order: 'sell' = long got liquidated "
            "(downside flush), 'buy' = short got liquidated (upside squeeze). Reports total USD "
            "notional, longs/shorts USD split, imbalance (buy − sell)/total, top-N by notional, "
            "and a per-symbol breakdown. Phase-2 collector covers Binance USD-M only."
        ),
    )
    async def liquidations_summary(
        window_minutes: Annotated[int, Field(ge=1, le=1440, description="Trailing window in minutes.")] = 60,
        venue: Annotated[str | None, Field(description="Optional venue filter (e.g. 'binanceusdm').")] = None,
        symbol: Annotated[str | None, Field(description="Optional symbol filter, exact match (e.g. 'BTCUSDT').")] = None,
        top_n: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        b = Builder()
        if rt.store is None:
            b.miss("store", "tradedesk", "config", "db_path is empty; set TRADEDESK_DB_PATH to enable liquidations")
            return b.build(None, ok=False)
        try:
            await rt.store.init()
        except Exception as e:  # noqa: BLE001
            b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
            return b.build(None, ok=False)
        now_ts = int(rt.now().timestamp())
        since_ts = now_ts - window_minutes * 60
        rows = await rt.store.liquidations_in_window(since_ts=since_ts, until_ts=now_ts, venue=venue, symbol=symbol)
        if not rows:
            return b.build(
                {
                    "window_minutes": window_minutes,
                    "venue": venue,
                    "symbol": symbol,
                    "count": 0,
                    "total_notional_usd": 0.0,
                    "buy_notional_usd": 0.0,
                    "sell_notional_usd": 0.0,
                    "imbalance": None,
                    "per_symbol": [],
                    "top": [],
                },
                ok=True,
            )

        total = sum(row["notional_usd"] for row in rows)
        buys = sum(row["notional_usd"] for row in rows if row["side"] == "buy")
        sells = sum(row["notional_usd"] for row in rows if row["side"] == "sell")
        imbalance = (buys - sells) / total if total else None

        by_sym: dict[str, dict[str, Any]] = {}
        for row in rows:
            agg = by_sym.setdefault(row["symbol"], {"symbol": row["symbol"], "count": 0, "buy_usd": 0.0, "sell_usd": 0.0, "total_usd": 0.0})
            agg["count"] += 1
            agg["total_usd"] += row["notional_usd"]
            agg["buy_usd" if row["side"] == "buy" else "sell_usd"] += row["notional_usd"]
        per_symbol = sorted(by_sym.values(), key=lambda x: x["total_usd"], reverse=True)

        top = sorted(rows, key=lambda x: x["notional_usd"], reverse=True)[:top_n]

        return b.build(
            {
                "window_minutes": window_minutes,
                "venue": venue,
                "symbol": symbol,
                "count": len(rows),
                "total_notional_usd": r(total, 0),
                "buy_notional_usd": r(buys, 0),
                "sell_notional_usd": r(sells, 0),
                "imbalance": r(imbalance, 4) if imbalance is not None else None,
                "per_symbol": [
                    {
                        "symbol": s["symbol"],
                        "count": s["count"],
                        "total_usd": r(s["total_usd"], 0),
                        "buy_usd": r(s["buy_usd"], 0),
                        "sell_usd": r(s["sell_usd"], 0),
                    }
                    for s in per_symbol[: top_n * 2]
                ],
                "top": [
                    {
                        "ts": t["ts"],
                        "venue": t["venue"],
                        "symbol": t["symbol"],
                        "side": t["side"],
                        "qty": r(t["qty"], 6),
                        "price": r(t["price"], 6),
                        "notional_usd": r(t["notional_usd"], 0),
                    }
                    for t in top
                ],
            }
        )
