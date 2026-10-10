from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .. import __version__
from ..core.envelope import Builder
from ..providers.exchanges import PERP_EXCHANGES
from ..runtime import Runtime
from ._util import READ_ONLY


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Health of every data provider (status ok/degraded/down, last error class, latency). "
            "With probe=true it first makes one cheap live call to each provider so the report is fresh "
            "instead of reflecting only past traffic. Call this first when results look empty or stale."
        ),
    )
    async def system_health(
        probe: Annotated[bool, Field(description="Make a live test call to each provider first")] = False,
    ) -> dict[str, Any]:
        b = Builder()
        if probe:
            probes = [
                b.guard("probe", "deribit", rt.deribit.book_summary("BTC", "future")),
                b.guard("probe", "hyperliquid", rt.hyperliquid.asset_contexts()),
                *[b.guard("probe", f"ccxt:{ex}", rt.exchanges.ticker(ex, "BTC/USDT:USDT")) for ex in PERP_EXCHANGES],
            ]
            await asyncio.gather(*probes)
        try:
            tools = sorted(t.name for t in (await server.list_tools()))
        except Exception:  # noqa: BLE001 - listing is informational only
            tools = []
        store_info: dict[str, Any] | None = None
        if rt.store is not None:
            try:
                store_info = {
                    "path": rt.store.path,
                    "version": await rt.store.version(),
                    "rows": {t: await rt.store.table_count(t) for t in ("atm_term", "funding_hist", "basis_hist", "snapshots")},
                }
            except RuntimeError:
                store_info = {"path": rt.store.path, "status": "not_initialised"}
        collector_info: list[dict[str, Any]] | None = None
        if rt.scheduler is not None:
            collector_info = [
                {
                    "name": m.name,
                    "period_seconds": m.period_seconds,
                    "runs": m.runs,
                    "successes": m.successes,
                    "failures": m.failures,
                    "last_run_ts": m.last_run_ts,
                    "last_duration_ms": m.last_duration_ms,
                    "last_rows_written": m.last_rows_written,
                    "last_error": m.last_error,
                }
                for m in rt.scheduler.metrics()
            ]
        return b.build(
            {
                "version": __version__,
                "health": rt.health.snapshot(),
                "tools": tools,
                "store": store_info,
                "collector": collector_info,
            },
            ok=True,
        )
