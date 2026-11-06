"""End-to-end check of CLAUDE.md invariant 1 for the tools that read or write the local SQLite store.

Sibling of test_tools_never_raise.py (same arguments, failing transports and ccxt stubs, so no real network): every
registered tool whose body touches `rt.store` (directly or through a module-level helper in `tools/`) is called through
a real MCP client while the store is
  - `none`: switched off (`db_path=""` -> `rt.store is None`);
  - `empty`: a freshly migrated database with no rows;
  - `missing_dir`: `db_path` inside a directory that does not exist yet;
  - `unreachable`: `db_path` under a regular file, so `Store.init()` raises (the server starts on a good store, then
    the tools see one that cannot be opened; in production the lifespan opens the store first, so the closest real
    case is an sqlite error at query time: locked file, disk I/O, full disk).
A tool must return the envelope (`ok` true or false), never an error result.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import ccxt
import httpx
import pytest
from mcp import Client

from conftest import NOW
from test_server import EXPECTED_TOOLS
from test_tools_never_raise import ARGS, SKIP, _AllTransports, _FailingEx
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.store import Store

TOOLS_DIR = Path(__file__).resolve().parents[1] / "src" / "tradedesk" / "tools"
MODES = ("none", "empty", "missing_dir", "unreachable")

# Tools that still raise in a store mode; each is listed in docs/changes/2026-11-06-tools-no-store.md and BACKLOG.
# `unreachable`: these call `await rt.store.init()` outside a try (the history/liquidations tools already catch it).
KNOWN_RAISERS: dict[str, set[str]] = {mode: set() for mode in MODES}
KNOWN_RAISERS["unreachable"] = {
    "assistant_paper_open",
    "assistant_paper_status",
    "carry_paper_close",
    "carry_paper_open",
    "carry_paper_status",
    "event_study",
    "events_add",
    "events_list",
    "events_seed_load",
    "forecast_log_status",
    "journal_add",
    "journal_close",
    "journal_list",
    "journal_stats",
    "paper_carry_status",
    "plan_add",
    "plan_link",
    "plan_list",
    "plan_review",
    "positions_add",
    "positions_close",
    "positions_list",
    "positions_risk",
    "rules_add",
    "rules_disable",
    "rules_list",
    "rules_presets_load",
    "rules_test",
}


def _touches_store(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Attribute) and n.attr == "store" and isinstance(n.value, ast.Name) and n.value.id == "rt" for n in ast.walk(node)
    )


def _called_names(node: ast.AST) -> set[str]:
    return {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def _is_tool(fn: ast.AST) -> bool:
    return any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool" for d in getattr(fn, "decorator_list", [])
    )


def store_tools() -> set[str]:
    """Names of registered tools in `tools/` that use `rt.store`, directly or via module-level helpers (any module)."""
    trees = [ast.parse(p.read_text(encoding="utf-8")) for p in sorted(TOOLS_DIR.glob("*.py"))]
    helpers = {  # module-level and nested (inside register) helpers alike
        n.name: n
        for tree in trees
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name != "register" and not _is_tool(n)
    }
    using = {name for name, n in helpers.items() if _touches_store(n)}
    grown = True
    while grown:  # helpers that call a store-using helper use the store too
        new = {name for name, n in helpers.items() if name not in using and _called_names(n) & using}
        using |= new
        grown = bool(new)
    found: set[str] = set()
    for tree in trees:
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and _is_tool(fn):
                if _touches_store(fn) or _called_names(fn) & using:
                    found.add(fn.name)
    return found


STORE_TOOLS = store_tools()


async def _runtime(tmp_path, mode: str) -> Runtime:
    if mode == "none":
        db_path = ""
    elif mode == "missing_dir":
        db_path = str(tmp_path / "not" / "there" / "yet.sqlite")
    else:
        db_path = str(tmp_path / "store.sqlite")
    if mode == "empty":
        pre = Store(db_path)
        await pre.init()  # migrated, no rows
        await pre.aclose()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="injected outage")

    rt = Runtime(
        Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=db_path),
        transports=_AllTransports(httpx.MockTransport(handler)),
        exchange_factory=lambda ex_id: _FailingEx(ex_id, ccxt.ExchangeNotAvailable),
    )
    rt.now = lambda: NOW
    return rt


async def _call_store_tools(tmp_path, mode: str) -> tuple[int, dict[str, str], dict[str, str]]:
    """Call each store tool once; returns (called, raised {name: error}, no_envelope {name: payload})."""
    rt = await _runtime(tmp_path, mode)
    srv, _ = build_server(rt)
    raised: dict[str, str] = {}
    no_envelope: dict[str, str] = {}
    called = 0
    async with Client(srv) as c:
        if mode == "unreachable":
            blocker = tmp_path / "a_file"
            blocker.write_text("not a directory")
            await rt.store.aclose()
            rt.store = Store(blocker / "store.sqlite")  # init() cannot create the parent: it is a file
        tools = {t.name: t for t in (await c.list_tools()).tools}
        for name in sorted(STORE_TOOLS):
            if name in SKIP:
                continue
            required = set(tools[name].input_schema.get("required", []))
            args = ARGS.get(name, {})
            assert required <= set(args), f"{name}: add minimal arguments for {sorted(required - set(args))} to ARGS"
            res = await c.call_tool(name, args)
            called += 1
            text = res.content[0].text if res.content else ""
            if res.is_error:
                raised[name] = text[:300]
                continue
            data = res.structured_content
            if data is None:
                data = json.loads(text)
            if set(data) == {"result"}:
                data = data["result"]
            if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
                no_envelope[name] = str(data)[:200]
    return called, raised, no_envelope


@pytest.mark.parametrize("mode", MODES)
async def test_no_store_tool_raises(tmp_path, mode):
    called, raised, no_envelope = await _call_store_tools(tmp_path, mode)
    assert called == len(STORE_TOOLS - set(SKIP))
    assert set(raised) == KNOWN_RAISERS[mode], f"tools that raised: {raised}"
    assert not no_envelope, f"tools without an envelope: {no_envelope}"


async def test_store_off_is_reported_in_missing(tmp_path):
    """With the store off, the journal tools say so in `missing` instead of returning an empty list."""
    rt = await _runtime(tmp_path, "none")
    srv, _ = build_server(rt)
    async with Client(srv) as c:
        res = await c.call_tool("journal_list", {})
    data = res.structured_content
    data = data.get("result", data)
    assert data["ok"] is False
    assert [(m["provider"], m["reason"]) for m in data["missing"]] == [("tradedesk", "config")]


def test_store_tool_discovery():
    """The AST scan finds the store tools (a broken scan would make the test above vacuous)."""
    assert STORE_TOOLS <= EXPECTED_TOOLS
    assert {"journal_add", "journal_list", "rules_add", "positions_list", "plan_add", "liquidations_summary"} <= STORE_TOOLS
    assert len(STORE_TOOLS) >= 35
    assert set().union(*KNOWN_RAISERS.values()) <= STORE_TOOLS
