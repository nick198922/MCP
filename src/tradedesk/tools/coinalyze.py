from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.liquidations_agg import aggregated_symbol, api_interval, summarise
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Historical long/short liquidations of a perpetual, aggregated across exchanges (Coinalyze, needs the "
            "COINALYZE_API_KEY setting). Per interval: long (forced sells of longs), short (forced buys of shorts), "
            "total, net = long - short, long_share in [0, 1]; plus totals and the peak interval. Units: USD by "
            "default, or base-asset coins with in_usd=false. Intervals are UTC-aligned daily or hourly. Coinalyze "
            "keeps hourly data only for a limited recent window (older ranges come back empty), and its exchange "
            "coverage and USD conversion are its own, so totals will not match a single venue's liquidation feed "
            "(see liquidations_summary for our own live-collected stream). Liquidation counts are exchange-reported "
            "and understate forced closes on venues that publish only the largest order per second."
        ),
    )
    async def liquidations_history(
        symbol: Annotated[str, Field(description="Base asset ticker, e.g. 'BTC', 'ETH', 'SOL'.")],
        interval: Annotated[Literal["daily", "hourly"], Field(description="Bucket size.")] = "daily",
        days: Annotated[int, Field(ge=1, le=365, description="Look-back window in days (hourly is capped at 30 days).")] = 30,
        margin: Annotated[
            Literal["usdt", "usd"], Field(description="'usdt' = USDT-margined perps (default), 'usd' = coin-margined perps.")
        ] = "usdt",
        in_usd: Annotated[bool, Field(description="Convert quantities to USD (true) or return base-asset units (false).")] = True,
    ) -> dict[str, Any]:
        b = Builder()
        sym = aggregated_symbol(symbol, margin)
        if sym is None:
            b.miss("symbol", "coinalyze", "invalid_argument", "symbol must be a plain ticker like 'BTC'")
            return b.build(None, ok=False)
        capped = interval == "hourly" and days > 30
        span_days = 30 if capped else days
        step = 3600 if interval == "hourly" else 86_400
        end = int(rt.now().timestamp())
        start = end - span_days * 86_400
        rows = await b.guard(
            "liquidations",
            "coinalyze",
            rt.coinalyze.liquidation_history(sym, api_interval(interval), start // step * step, end, convert_to_usd=in_usd),
        )
        if rows is None:
            return b.build(None, ok=False)
        data = summarise(rows, interval)
        if data is None:
            b.miss("liquidations", "coinalyze", "data_unavailable", f"no liquidation history for {sym} in the range")
            return b.build(None, ok=False)
        data.update({"symbol": sym, "interval": interval, "unit": "USD" if in_usd else symbol.upper(), "days": span_days})
        if capped:
            b.warn("hourly history is capped at 30 days; days was reduced to 30.")
        if data["totals"]["gaps"]:
            b.warn(f"{data['totals']['gaps']} gap(s) in the series (missing intervals); totals cover only returned rows.")
        b.warn(
            "Aggregated across the exchanges Coinalyze covers; the current (last) interval is still open. "
            "Not a trading signal by itself: no backtest of liquidation spikes is attached to this tool."
        )
        return b.build(data)
