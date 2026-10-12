"""Macro-event calendar stored locally. Underpins the Phase-3 event-study
engine (``event_study``) that will land next: pull OHLCV windows around each
stored timestamp and compute hit rate / median / p25-p75 across the sample.
"""

from __future__ import annotations

import json
from datetime import datetime
from importlib import resources
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY


def _store_guard(b: Builder, rt: Runtime) -> bool:
    if rt.store is None:
        b.miss("store", "tradedesk", "config", "db_path is empty; set TRADEDESK_DB_PATH to enable events")
        return False
    return True


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Add a macro / scheduled event to the local calendar. Pass either `ts_ms` (unix "
            "milliseconds, UTC) or `date` + `time_utc` (ISO 'YYYY-MM-DD' and 'HH:MM'). `kind` is "
            "free-form (fomc / cpi / nfp / halving / listing / …). Idempotent: duplicates with "
            "the same (ts, kind, label) are ignored."
        ),
    )
    async def events_add(
        kind: Annotated[str, Field(min_length=1, max_length=40)],
        label: Annotated[str, Field(min_length=1, max_length=200)],
        impact: Literal["low", "medium", "high"] = "medium",
        ts_ms: Annotated[int | None, Field(description="Unix milliseconds (UTC). Use this OR date+time_utc.")] = None,
        date: Annotated[str | None, Field(description="ISO date 'YYYY-MM-DD' (UTC).")] = None,
        time_utc: Annotated[str | None, Field(description="'HH:MM' UTC. Defaults to 13:30.")] = None,
        meta: Annotated[dict[str, Any] | None, Field(description="Arbitrary JSON extras.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        if ts_ms is not None:
            ts = int(ts_ms) // 1000
        elif date:
            tt = (time_utc or "13:30").strip()
            try:
                dt = datetime.strptime(f"{date} {tt}", "%Y-%m-%d %H:%M")
            except ValueError:
                b.miss("date", "tradedesk", "data_unavailable", "could not parse date+time_utc")
                return b.build(None, ok=False)
            ts = int(dt.replace(tzinfo=rt.now().tzinfo).timestamp())
        else:
            b.miss("ts", "tradedesk", "data_unavailable", "provide ts_ms OR date (+time_utc)")
            return b.build(None, ok=False)
        row_id = await rt.store.event_insert(ts=ts, kind=kind, label=label, impact=impact, meta=meta)
        return b.build({"id": row_id, "inserted": row_id > 0, "ts": ts, "kind": kind, "label": label})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "List events from the local calendar. Filter by `kind`, `impact`, and a UTC date "
            "window (`since_date` / `until_date` as 'YYYY-MM-DD'). Ordered oldest-first."
        ),
    )
    async def events_list(
        kind: Annotated[str | None, Field(description="Filter by kind (e.g. 'fomc').")] = None,
        impact: Annotated[Literal["low", "medium", "high"] | None, Field(description="Filter by impact.")] = None,
        since_date: Annotated[str | None, Field(description="'YYYY-MM-DD' (UTC) lower bound.")] = None,
        until_date: Annotated[str | None, Field(description="'YYYY-MM-DD' (UTC) upper bound.")] = None,
        limit: Annotated[int, Field(ge=1, le=5000)] = 500,
    ) -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        since_ts = until_ts = None
        if since_date:
            try:
                since_ts = int(datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=rt.now().tzinfo).timestamp())
            except ValueError:
                b.miss("since_date", "tradedesk", "data_unavailable", "invalid date")
                return b.build(None, ok=False)
        if until_date:
            try:
                until_ts = int(datetime.strptime(until_date + " 23:59", "%Y-%m-%d %H:%M").replace(tzinfo=rt.now().tzinfo).timestamp())
            except ValueError:
                b.miss("until_date", "tradedesk", "data_unavailable", "invalid date")
                return b.build(None, ok=False)
        rows = await rt.store.events_list(kind=kind, impact=impact, since_ts=since_ts, until_ts=until_ts, limit=limit)
        return b.build({"count": len(rows), "events": rows})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Load the bundled historical events seed (FOMC / CPI / NFP ~2024-2026) into the "
            "local store. Idempotent — rows with the same (ts, kind, label) are ignored. 2026 "
            "Fed dates in the seed are PROJECTED; verify against the official Fed/BLS calendars."
        ),
    )
    async def events_seed_load() -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        try:
            with resources.files("tradedesk.data").joinpath("events_seed.json").open("r", encoding="utf-8") as f:
                seed = json.load(f)
        except (FileNotFoundError, ValueError, OSError) as e:
            b.miss("seed", "tradedesk", "data_unavailable", f"{type(e).__name__}: {e}")
            return b.build(None, ok=False)
        rows = seed.get("events") or []
        before = await rt.store.event_count()
        inserted = await rt.store.events_insert_many(rows)
        total = await rt.store.event_count()
        b.warn("2026 Fed dates in the seed are projected; verify against the official calendar")
        return b.build(
            {
                "candidates": len(rows),
                "inserted": inserted,
                "already_present": len(rows) - inserted,
                "events_before": before,
                "events_after": total,
                "meta": seed.get("_meta"),
            }
        )
