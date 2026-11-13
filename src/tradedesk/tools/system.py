from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .. import __version__
from ..core.envelope import Builder
from ..providers.exchanges import PERP_EXCHANGES
from ..runtime import Runtime
from ._util import READ_ONLY, STORE_ERRORS


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Health of every data provider (status ok/degraded/down, last error class, latency). "
            "With probe=true it first makes one cheap live call to each provider so the report is fresh "
            "instead of reflecting only past traffic. Call this first when results look empty or stale. "
            "collector[] (one entry per background job, null when the collector is off): runs, failures, last error, "
            "and last_rows_dropped / rows_dropped_total - malformed provider rows or numeric fields the job dropped "
            "or stored as NULL (NaN, inf, text, out of bounds) in its latest run / since the server started (a count, "
            "not a fraction; in-memory, reset on restart). "
            "collector[].stream (WebSocket jobs liquidations:*, else null): connects, reconnects, failures, "
            "last_failure, last_message_ts (any frame, heartbeat replies included) and last_row_ts (last liquidation). "
            "Expected, not a fault: liquidations:bybit covers only BTC/ETH/SOL and has 30-60 min without a row several "
            "times a day in calm hours (server data 2026-11-10..12: none of 16 such pauses was explained by logged "
            "stream failures). A fresh last_message_ts with an old last_row_ts most likely means a quiet market, but it "
            "is not proof: heartbeat replies keep last_message_ts (and the hourly frames count) fresh even if the venue "
            "silently stopped the subscription. A rejected subscribe is counted in failures, a silent loss is not seen "
            "here, so a long silence while other venues keep logging liquidations deserves a check. A few stream "
            "failures an hour (keepalive timeout, handshake timeout, reset; often Bybit and OKX together) reconnect "
            "within about a minute each; a dead link shows as a stale last_message_ts or a growing consecutive_failures."
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
            except STORE_ERRORS as e:  # locked, unreadable or vanished file: report it, keep the rest of the health report
                b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
                store_info = {"path": rt.store.path, "status": "error"}
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
                    "last_failure": m.last_failure,
                    "last_failure_ts": m.last_failure_ts,
                    # Malformed provider rows / fields dropped or stored as NULL (collector/bad_rows.py).
                    "last_rows_dropped": m.last_rows_dropped,
                    "rows_dropped_total": m.rows_dropped_total,
                    # WebSocket jobs: connects / reconnects / failures inside runs, last_message_ts vs last_row_ts.
                    "stream": m.stream.as_dict() if m.stream is not None else None,
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
