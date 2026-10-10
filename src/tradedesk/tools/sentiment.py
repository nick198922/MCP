from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.sentiment import summarise_fear_greed
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Crypto Fear & Greed index from alternative.me, daily values on a 0..100 scale "
            "(0 = extreme fear, 100 = extreme greed). Returns latest value with its zone, "
            "7d/30d mean, series min/max, point-changes vs 7/30 days ago, and the share of "
            "days spent in each zone. CRYPTO sentiment only (not SPX/VIX)."
        ),
    )
    async def sentiment_fear_greed(
        days: Annotated[int, Field(ge=1, le=365, description="How many trailing daily readings to pull.")] = 30,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await b.guard("fng", "alternative_me", rt.alternative_me.fear_greed(days))
        if not rows:
            if rows is not None:
                b.miss("fng", "alternative_me", "data_unavailable", "empty series")
            return b.build(None, ok=False)
        summary = summarise_fear_greed(rows, now_ts=int(rt.now().timestamp()))
        if summary.get("count", 0) == 0:
            b.miss("fng", "alternative_me", "data_unavailable", "no parseable readings")
            return b.build(None, ok=False)
        return b.build({"days_requested": days, **summary})
