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
from ._util import READ_ONLY, STORE_ERRORS, drop_corrupt_rows, finite_json, first_nonfinite, store_failed, store_ready

# Numeric columns of a stored rule (`enabled` is turned into a bool by the store); another writer can leave a string, a
# blob or inf in any of them.
RULE_NUMS = ("cooldown_seconds", "created_ts", "disabled_ts")


def _preset_ok(x: Any) -> bool:
    """True when a seed preset is an object with a non-empty text `name`, a text `message` (or none), a boolean `enabled`
    (or none) and an integer `cooldown_seconds` within rules_add's 60 s to 1 day (or none). A cooldown of "abc" / inf
    raised out of int(), 1e308 overflowed the SQLite INTEGER binding; `expr` is checked by compile_expr (-> `failed`)."""
    if not isinstance(x, dict) or not isinstance(x.get("name"), str) or not x["name"].strip():
        return False
    cd = x.get("cooldown_seconds", 1800)
    if not isinstance(cd, int) or isinstance(cd, bool) or not 60 <= cd <= 86_400:
        return False
    return isinstance(x.get("message"), str | None) and isinstance(x.get("enabled", True), bool)


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,  # writes a row to the local store only
        description=(
            "Create a DSL rule in the local store. The expression is a safe boolean mini-language "
            "over metric functions (see the output of rules_test with no args for the list). The "
            "rule fires when the expression evaluates truthy AND its cooldown has elapsed since the "
            "last fire. `name` must be unique (a taken name returns invalid_input on `name`); use rules_disable to turn it off instead of deleting."
        ),
    )
    async def rules_add(
        name: Annotated[str, Field(min_length=1, max_length=120, description="Unique rule name (1-120 chars), used in alerts.")],
        expr: Annotated[str, Field(min_length=1, description='e.g. funding("binanceusdm","BTC") > 0.30')],
        message: Annotated[str | None, Field(description="Message with {value} / {value_pct} placeholders.")] = None,
        cooldown_seconds: Annotated[
            int, Field(ge=60, le=86_400, description="Minimum seconds between two fires of this rule (60 s to 1 day).")
        ] = 1800,
        enabled: Annotated[bool, Field(description="Store the rule as active; false keeps it saved but silent.")] = True,
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "rules"):
            return b.build(None, ok=False)
        try:
            try:
                compile_expr(expr)
            except DslError as e:
                b.miss("expr", "tradedesk", "data_unavailable", str(e))
                return b.build(None, ok=False)
            dup = next((r for r in await rt.store.rules_list() if r["name"] == name), None)
            if dup is not None:
                b.miss("name", "tradedesk", "invalid_input", f"rule name already exists (id={dup['id']})")
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
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,
        description="List DSL rules in the local store, optionally filtered to enabled ones.",
    )
    async def rules_list(
        enabled_only: Annotated[bool, Field(description="Return only enabled rules; false lists all.")] = False,
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "rules"):
            return b.build(None, ok=False)
        try:
            rows = await rt.store.rules_list(enabled_only=enabled_only)
            # An invalid UTF-8 blob in name / expr / message failed the whole reply, and inf in a numeric cell came back as
            # Infinity: such a rule is skipped with a warning (another writer); an all-malformed store is not "no rules".
            stored = len(rows)
            rows = drop_corrupt_rows(b, rows, "rule", RULE_NUMS, required=("cooldown_seconds", "created_ts"))
            if stored and not rows:
                b.miss("rules", "store", "data_unavailable", f"all {stored} matching stored rules are malformed")
                return b.build({"count": 0, "rules": []}, ok=False)
            return b.build({"count": len(rows), "rules": rows})
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Test a DSL expression WITHOUT storing it. Pass `expr` to evaluate now; omit it to "
            "get the list of available metric functions. Returns the evaluated value and parse "
            "diagnostics; useful for drafting rules. An expression that fails to evaluate, or whose value is NaN / inf "
            "(e.g. an overflow or a non-finite stored metric), answers ok=false with the reason in `missing`."
        ),
    )
    async def rules_test(
        expr: Annotated[str | None, Field(description="Expression to validate and evaluate.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "rules"):
            return b.build(None, ok=False)
        try:
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
            except STORE_ERRORS:
                raise  # a failing store query is `missing: store` (below), not an evaluation error
            except Exception as e:  # noqa: BLE001
                b.miss("eval", "tradedesk", "data_unavailable", f"{type(e).__name__}: {e}")
                return b.build({"expr": expr, "parsed": True, "evaluated": False}, ok=False)
            if first_nonfinite(result) is not None:
                # An inf / 1e400 literal, an overflow (1e308 * 10) or a non-finite stored value came back as Infinity /
                # NaN with ok=true; a 400-digit int is beyond what a JSON reader parses as a number.
                b.miss("value", "tradedesk", "data_unavailable", "the expression evaluated to NaN, inf or a number beyond the float range")
                return b.build({"expr": expr, "parsed": True, "evaluated": True, "value": None, "truthy": bool(result)}, ok=False)
            return b.build({"expr": expr, "parsed": True, "evaluated": True, "value": result, "truthy": bool(result)})
        except STORE_ERRORS as e:
            return store_failed(b, e)

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
        if not await store_ready(b, rt, "rules"):
            return b.build(None, ok=False)
        try:
            existing = await rt.store.rule_get(id)
            if existing is None:
                b.miss("rule", "store", "data_unavailable", f"rule id={id} not found")
                return b.build(None, ok=False)
            if delete:
                await rt.store.rule_delete(id)
                return b.build({"id": id, "deleted": True})
            now_ts = int(rt.now().timestamp())
            await rt.store.rule_set_enabled(id, enabled=enable, ts=now_ts)
            row = await rt.store.rule_get(id)
            # The switch reads no stored value, so a corrupt rule is still switched; but a blob cell failed the reply after
            # the write, and inf came back as Infinity: the stored row is then left out of the reply.
            if row is not None and not drop_corrupt_rows(Builder(), [row], "rule", RULE_NUMS, required=("cooldown_seconds", "created_ts")):
                b.warn(f"rule id={id} was {'enabled' if enable else 'disabled'}, but its stored row is malformed, so it is not returned")
                return b.build({"id": id, "enabled": enable})
            return b.build(row)
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Install the bundled rule presets (funding extremes, funding z-score, HL↔Deribit "
            "crypto-carry, liquidation cascades + imbalance). Each preset is a complete DSL "
            "rule validated at compile time. Idempotent via UNIQUE(name) — existing rules are "
            "skipped. Use `only=['name1','name2']` to pick a subset; `enable_disabled=true` "
            "forces presets flagged `enabled: false` in the seed to be enabled on install. "
            "IMPORTANT: thresholds in the presets are starting-point values picked by the model "
            "from general crypto-derivatives intuition — they are NOT backtested or research-"
            "sourced. Tune them against your own history before trusting the alerts."
        ),
    )
    async def rules_presets_load(
        only: Annotated[list[str] | None, Field(description="Install just these preset names.")] = None,
        enable_disabled: Annotated[bool, Field(description="Enable presets whose seed `enabled` is false.")] = False,
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "rules"):
            return b.build(None, ok=False)
        try:
            try:
                with resources.files("tradedesk.data").joinpath("rules_presets.json").open("r", encoding="utf-8") as f:
                    seed = json.load(f)
            except (FileNotFoundError, ValueError, OSError) as e:
                b.miss("presets", "tradedesk", "data_unavailable", f"{type(e).__name__}: {e}")
                return b.build(None, ok=False)

            # A seed that is not {"presets": [...]} raised AttributeError / TypeError; presets that cannot be stored are skipped.
            presets = seed.get("presets") if isinstance(seed, dict) else None
            if not isinstance(presets, list) or not presets:
                b.miss("presets", "tradedesk", "data_unavailable", "the seed is not an object with a non-empty `presets` list")
                return b.build(None, ok=False)
            usable = [row for row in presets if _preset_ok(row)]
            if len(usable) < len(presets):
                b.warn(
                    f"{len(presets) - len(usable)} seed presets skipped: not an object with a text name / message, a boolean "
                    "enabled and an integer cooldown_seconds in 60-86400"
                )
            if not usable:
                b.miss("presets", "tradedesk", "data_unavailable", f"all {len(presets)} seed presets are malformed")
                return b.build(None, ok=False)
            meta = seed.get("_meta")
            if not finite_json(meta):  # NaN / Infinity in the seed's _meta came back as such in the reply
                b.warn("the seed's _meta holds NaN or Infinity; it is not returned")
                meta = None
            selected: list[dict[str, Any]] = []
            for row in usable:
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
                    "meta": meta,
                }
            )
        except STORE_ERRORS as e:
            return store_failed(b, e)
