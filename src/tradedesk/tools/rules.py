"""Rules DSL management (Phase 3.1).

Create, list, test and disable boolean rules evaluated by the alerts
scheduler job against the local history. Expressions use the metric
functions in :mod:`tradedesk.rules.metrics`.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..core.envelope import Builder
from ..rules import METRICS, DslError, compile_expr, evaluate
from ..rules.metrics import MetricContext, build_metrics
from ..runtime import Runtime
from ._util import READ_ONLY


def _store_guard(b: Builder, rt: Runtime) -> bool:
    if rt.store is None:
        b.miss("store", "tradedesk", "config", "db_path is empty; set TRADEDESK_DB_PATH to enable rules")
        return False
    return True


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,  # writes a row to the local store only
        description=(
            "Create a DSL rule in the local store. The expression is a safe boolean mini-language "
            "over metric functions (see the output of rules_test with no args for the list). The "
            "rule fires when the expression evaluates truthy AND its cooldown has elapsed since the "
            "last fire. `name` must be unique; use rules_disable to turn it off instead of deleting."
        ),
    )
    async def rules_add(
        name: Annotated[str, Field(min_length=1, max_length=120)],
        expr: Annotated[str, Field(min_length=1, description='e.g. funding("binanceusdm","BTC") > 0.30')],
        message: Annotated[str | None, Field(description="Message with {value} / {value_pct} placeholders.")] = None,
        cooldown_seconds: Annotated[int, Field(ge=60, le=86_400)] = 1800,
        enabled: bool = True,
    ) -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        try:
            compile_expr(expr)
        except DslError as e:
            b.miss("expr", "tradedesk", "data_unavailable", str(e))
            return b.build(None, ok=False)
        rid = await rt.store.rule_insert(
            name=name,
            expr=expr,
            message=message,
            cooldown_seconds=cooldown_seconds,
            enabled=enabled,
            created_ts=int(rt.now().timestamp()),
        )
        return b.build({"id": rid, "name": name, "enabled": enabled})

    @server.tool(
        annotations=READ_ONLY,
        description="List DSL rules in the local store, optionally filtered to enabled ones.",
    )
    async def rules_list(enabled_only: bool = False) -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        rows = await rt.store.rules_list(enabled_only=enabled_only)
        return b.build({"count": len(rows), "rules": rows})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Test a DSL expression WITHOUT storing it. Pass `expr` to evaluate now; omit it to "
            "get the list of available metric functions. Returns the evaluated value and parse "
            "diagnostics; useful for drafting rules."
        ),
    )
    async def rules_test(
        expr: Annotated[str | None, Field(description="Expression to validate and evaluate.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        if expr is None:
            return b.build({"available_metrics": list(METRICS)})
        try:
            compiled = compile_expr(expr)
        except DslError as e:
            b.miss("expr", "tradedesk", "data_unavailable", str(e))
            return b.build({"expr": expr, "parsed": False}, ok=False)
        now_ts = int(rt.now().timestamp())
        ctx = MetricContext(store=rt.store, now_ts=now_ts)
        calls = build_metrics(ctx)
        try:
            result = await evaluate(compiled, {"names": {}, "calls": calls})
        except Exception as e:  # noqa: BLE001
            b.miss("eval", "tradedesk", "data_unavailable", f"{type(e).__name__}: {e}")
            return b.build({"expr": expr, "parsed": True, "evaluated": False})
        return b.build({"expr": expr, "parsed": True, "evaluated": True, "value": result, "truthy": bool(result)})

    @server.tool(
        annotations=READ_ONLY,  # writes to the local store only
        description="Enable, disable or delete a DSL rule in the local store.",
    )
    async def rules_disable(
        id: Annotated[int, Field(ge=1, description="Rule id from rules_add / rules_list.")],
        enable: Annotated[bool, Field(description="True to re-enable, False to disable.")] = False,
        delete: Annotated[bool, Field(description="If true, delete the row instead.")] = False,
    ) -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        existing = await rt.store.rule_get(id)
        if existing is None:
            b.miss("rule", "store", "data_unavailable", f"rule id={id} not found")
            return b.build(None, ok=False)
        if delete:
            await rt.store.rule_delete(id)
            return b.build({"id": id, "deleted": True})
        now_ts = int(rt.now().timestamp())
        await rt.store.rule_set_enabled(id, enabled=enable, ts=now_ts)
        return b.build(await rt.store.rule_get(id))

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Install the bundled rule presets (funding extremes, funding z-score, HL↔Deribit "
            "crypto-carry, liquidation cascades + imbalance). Each preset is a complete DSL "
            "rule validated at compile time. Idempotent via UNIQUE(name) — existing rules are "
            "skipped. Use `only=['name1','name2']` to pick a subset; `enable_disabled=true` "
            "forces presets flagged `enabled: false` in the seed to be enabled on install."
        ),
    )
    async def rules_presets_load(
        only: Annotated[list[str] | None, Field(description="Install just these preset names.")] = None,
        enable_disabled: Annotated[bool, Field(description="Enable presets whose seed `enabled` is false.")] = False,
    ) -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        try:
            with resources.files("tradedesk.data").joinpath("rules_presets.json").open("r", encoding="utf-8") as f:
                seed = json.load(f)
        except (FileNotFoundError, ValueError, OSError) as e:
            b.miss("presets", "tradedesk", "data_unavailable", f"{type(e).__name__}: {e}")
            return b.build(None, ok=False)

        presets = seed.get("presets") or []
        selected: list[dict[str, Any]] = []
        for row in presets:
            if only and row.get("name") not in only:
                continue
            selected.append(row)

        now_ts = int(rt.now().timestamp())
        inserted = 0
        skipped: list[str] = []
        failed: list[dict[str, Any]] = []
        for row in selected:
            name = row.get("name") or ""
            expr = row.get("expr") or ""
            try:
                compile_expr(expr)
            except DslError as e:
                failed.append({"name": name, "reason": str(e)})
                continue
            # UNIQUE(name) — rule_insert raises IntegrityError on dup, so skip existing names.
            existing = await rt.store.rules_list()
            if any(r["name"] == name for r in existing):
                skipped.append(name)
                continue
            enabled = bool(row.get("enabled", True)) or enable_disabled
            await rt.store.rule_insert(
                name=name,
                expr=expr,
                message=row.get("message"),
                cooldown_seconds=int(row.get("cooldown_seconds", 1800)),
                enabled=enabled,
                created_ts=now_ts,
            )
            inserted += 1
        return b.build(
            {
                "candidates": len(selected),
                "inserted": inserted,
                "already_present": skipped,
                "failed": failed,
                "meta": seed.get("_meta"),
            }
        )
