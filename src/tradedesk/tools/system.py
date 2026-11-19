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

# The compact report (item 406) leaves out only fields whose value is implied, so it loses nothing:
# a provider registered but never called (every field 0 / null) is named in health.unknown, ...
PROVIDER_UNUSED = {
    "status": "unknown",
    "calls": 0,
    "failures": 0,
    "consecutive_failures": 0,
    "last_ok_at": None,
    "last_error_at": None,
    "last_reason": None,
    "last_detail": None,
    "latency_ms_ewma": None,
}
# ... a provider without a failure omits its error fields (these values) ...
PROVIDER_NO_ERRORS = {"failures": 0, "consecutive_failures": 0, "last_error_at": None, "last_reason": None, "last_detail": None}
# ... and a collector job without a failure its null failure fields; `stream` is always shown as is.
JOB_NO_FAILURES = {"last_error": None, "last_failure": None, "last_failure_ts": None}


def _same(row: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(k in row and row[k] == v and type(row[k]) is type(v) for k, v in expected.items())


def _compact_health(snapshot: dict[str, Any]) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    unknown: list[str] = []
    for name, p in snapshot["providers"].items():
        if set(p) == set(PROVIDER_UNUSED) and _same(p, PROVIDER_UNUSED):
            unknown.append(name)
        elif _same(p, PROVIDER_NO_ERRORS):
            providers[name] = {k: v for k, v in p.items() if k not in PROVIDER_NO_ERRORS}
        else:
            providers[name] = p
    return {"summary": snapshot["summary"], "providers": providers, "unknown": unknown}


def _compact_job(row: dict[str, Any]) -> dict[str, Any]:
    drop: set[str] = set()
    if row["failures"] == 0 and _same(row, JOB_NO_FAILURES):
        drop |= set(JOB_NO_FAILURES)
    if row["successes"] == row["runs"] - row["failures"]:
        drop.add("successes")
    # last_rows_dropped is null before the first run, then the latest run's count
    if _same(row, {"rows_dropped_total": 0}) and row["last_rows_dropped"] == (None if row["runs"] == 0 else 0):
        drop |= {"last_rows_dropped", "rows_dropped_total"}
    return {k: v for k, v in row.items() if k not in drop}


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
            "collector[].stream (WebSocket jobs liquidations:* and book_walls:*, else null): connects, reconnects, failures, "
            "last_failure, last_message_ts (any frame, heartbeat replies included) and last_row_ts (last liquidation). "
            "Expected, not a fault: liquidations:bybit covers only BTC/ETH/SOL and has 30-60 min without a row several "
            "times a day in calm hours (server data 2026-11-10..12: none of 16 such pauses was explained by logged "
            "stream failures). A fresh last_message_ts with an old last_row_ts most likely means a quiet market, but it "
            "is not proof: heartbeat replies keep last_message_ts (and the hourly frames count) fresh even if the venue "
            "silently stopped the subscription. A rejected subscribe is counted in failures, a silent loss is not seen "
            "here, so a long silence while other venues keep logging liquidations deserves a check. A few stream "
            "failures an hour (keepalive timeout, handshake timeout, reset; often Bybit and OKX together) reconnect "
            "within about a minute each; a dead link shows as a stale last_message_ts or a growing consecutive_failures. "
            "book_walls:* jobs (order-book walls collector) show the same counters per feed: "
            "stream = {depth: {...}, trades: {...}} for binance, {book: {...}} for bybit, binance_spot and coinbase. "
            "By default the report is compact but loses nothing: only fields whose value is implied are left out. "
            "health.unknown names the providers never called since the server started (status unknown, every counter 0, "
            "every time null); a provider without failures omits failures, consecutive_failures (0) and last_error_at, "
            "last_reason, last_detail (null); a provider with any failure keeps its full row. A collector job omits "
            "successes when it equals runs - failures, its null last_error / last_failure / last_failure_ts while "
            "failures = 0, and last_rows_dropped / rows_dropped_total while nothing was dropped; collector[].stream is "
            "always shown in full. `tools` (the tool names) is replaced by tool_count. include_details=true, or "
            "probe=true, returns the full report with every field."
        ),
    )
    async def system_health(
        probe: Annotated[bool, Field(description="Make a live test call to each provider first")] = False,
        include_details: Annotated[
            bool,
            Field(
                description=(
                    "Return every field (all providers, every collector counter, the tool names); false (default) = "
                    "a compact report that leaves out only implied values (0 / null / never-called providers). "
                    "probe=true always returns the full report."
                )
            ),
        ] = False,
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
        if include_details or probe:
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
        b.warn(
            "compact answer (not a fault): implied 0 / null fields and never-called providers (health.unknown) left out, "
            "tool names as tool_count; include_details=true for every field"
        )
        return b.build(
            {
                "version": __version__,
                "health": _compact_health(rt.health.snapshot()),
                "tool_count": len(tools),
                "store": store_info,
                "collector": None if collector_info is None else [_compact_job(x) for x in collector_info],
            },
            ok=True,
        )
