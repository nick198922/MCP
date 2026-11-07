"""CLAUDE.md invariant 1 for the store tools when the SQLite file itself is broken or locked (item 218).

Sibling of test_tools_no_store.py (same tool scan, arguments, failing transports, no real network). The server starts on
a good store (the lifespan opens it), then every store tool is called through a real MCP client while the database is
  - `random_bytes`: a file of random bytes (not SQLite at all): `Store.init()` raises `DatabaseError: file is not a
    database`;
  - `truncated`: a valid, migrated database with rows, cut to half its size: `Store.init()` raises `DatabaseError:
    database disk image is malformed`;
  - `locked`: the server's own open store while another connection holds `BEGIN EXCLUSIVE` (the collector or a script
    writing). In WAL mode readers still see the last committed data, so reads work and every write fails with
    `OperationalError: database is locked` after the busy timeout (set to 50 ms here, 5 s in production).
A tool must return the envelope, never an error result, and a failed store must show in `missing`, not as empty data.
"""

from __future__ import annotations

import json
import random
import sqlite3

import pytest
from mcp import Client

from test_tools_never_raise import ARGS, SKIP
from test_tools_no_store import NO_QUERY_WITH_ARGS, STORE_TOOLS, _runtime
from tradedesk.server import build_server
from tradedesk.store import Store

CORRUPT_MODES = ("random_bytes", "truncated")

# Store tools that do not open the store with the test ARGS (see test_corrupt_store_file_goes_to_missing).
NOT_OPENING = {"range_setup", "system_health"}

# Rows created through the tools before the lock, so that the close/disable/link tools (id=1 in ARGS) reach their write.
SEED = ("journal_add", "positions_add", "rules_add", "plan_add")

# Tools that write to the store with the test ARGS and so must report the lock (the seed above gives them a row with
# id 1; rules_add is not here: the seeded rule has its name, so it stops at the duplicate check before writing). Reads
# keep working under a WAL write lock, so the other store tools answer from the last committed data.
LOCKED_WRITERS = {
    "events_seed_load",
    "journal_add",
    "journal_close",
    "plan_add",
    "plan_link",
    "positions_add",
    "positions_close",
    "rules_disable",
    "rules_presets_load",
}


def _payload(res) -> dict | str:
    data = res.structured_content
    if data is None:
        data = json.loads(res.content[0].text)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return data


def _store_misses(env: dict) -> list[str]:
    return [m["detail"] for m in env["missing"] if m["field"] == "store" and m["reason"] == "backend_error"]


async def _broken_file(tmp_path, mode: str):
    path = tmp_path / f"{mode}.sqlite"
    if mode == "random_bytes":
        path.write_bytes(random.Random(218).randbytes(64 * 1024))
    else:
        good = Store(path)
        await good.init()
        await good.journal_insert(opened_ts=1, symbol="BTC/USDT:USDT", side="long", size=0.1, entry=82000.0)
        await good.aclose()  # checkpoints the WAL into the main file
        assert not path.with_name(f"{path.name}-wal").exists()
        size = path.stat().st_size
        with path.open("r+b") as f:
            f.truncate(size // 2)
    return path


async def _call_all(c, names) -> tuple[dict[str, str], dict[str, str], dict[str, dict]]:
    tools = {t.name: t for t in (await c.list_tools()).tools}
    raised: dict[str, str] = {}
    no_envelope: dict[str, str] = {}
    envelopes: dict[str, dict] = {}
    for name in sorted(names):
        required = set(tools[name].input_schema.get("required", []))
        args = ARGS.get(name, {})
        assert required <= set(args), f"{name}: add minimal arguments for {sorted(required - set(args))} to ARGS"
        res = await c.call_tool(name, args)
        if res.is_error:
            raised[name] = (res.content[0].text if res.content else "")[:300]
            continue
        data = _payload(res)
        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            no_envelope[name] = str(data)[:200]
        else:
            envelopes[name] = data
    return raised, no_envelope, envelopes


@pytest.mark.parametrize("mode", CORRUPT_MODES)
async def test_corrupt_store_file_goes_to_missing(tmp_path, mode):
    path = await _broken_file(tmp_path, mode)
    rt = await _runtime(tmp_path, "empty")
    srv, _ = build_server(rt)
    async with Client(srv) as c:
        await rt.store.aclose()
        rt.store = Store(path)  # what the tools see when the file is damaged under a running server
        raised, no_envelope, envelopes = await _call_all(c, STORE_TOOLS - set(SKIP))
    assert not raised, f"tools that raised: {raised}"
    assert not no_envelope, f"tools without an envelope: {no_envelope}"
    assert len(envelopes) == len(STORE_TOOLS - set(SKIP))
    expected = "file is not a database" if mode == "random_bytes" else "database disk image is malformed"
    failed = {n for n, env in envelopes.items() if any(expected in d for d in _store_misses(env))}
    # The tools open the store through `tools._util.store_ready` before anything else, so even those that return
    # before their first query on a healthy store (NO_QUERY_WITH_ARGS) report it here. Two do not open it: range_setup
    # reads live bars with the test ARGS (its store source: test below), and system_health only reads an open store and
    # shows this never-opened one as `not_initialised` (in production the lifespan has opened it, and a query error on
    # it is `missing: store`, see test_tools_no_store.py).
    assert failed == set(envelopes) - NOT_OPENING, f"no `missing: store` from: {sorted(set(envelopes) - NOT_OPENING - failed)}"
    assert all(envelopes[n]["ok"] is False for n in failed)
    assert envelopes["system_health"]["ok"] is True
    assert envelopes["system_health"]["data"]["store"]["status"] == "not_initialised"


@pytest.mark.parametrize("mode", CORRUPT_MODES)
async def test_range_setup_store_source_corrupt(tmp_path, mode):
    path = await _broken_file(tmp_path, mode)
    rt = await _runtime(tmp_path, "empty")
    srv, _ = build_server(rt)
    async with Client(srv) as c:
        await rt.store.aclose()
        rt.store = Store(path)
        res = await c.call_tool("range_setup", {**ARGS["range_setup"], "source": "store"})
    assert not res.is_error, res.content[0].text[:300] if res.content else ""
    data = _payload(res)
    assert data["ok"] is False and _store_misses(data)


async def test_locked_store_writes_go_to_missing(tmp_path):
    rt = await _runtime(tmp_path, "empty")
    srv, _ = build_server(rt)
    other = None
    try:
        async with Client(srv) as c:
            for name in SEED:
                seeded = _payload(await c.call_tool(name, ARGS[name]))
                assert not _store_misses(seeded), f"{name} did not seed: {seeded}"
            await rt.store.conn.execute("PRAGMA busy_timeout = 50")  # production waits 5 s (sqlite3 default)
            other = sqlite3.connect(rt.store.path, isolation_level=None)
            other.execute("BEGIN EXCLUSIVE")
            raised, no_envelope, envelopes = await _call_all(c, STORE_TOOLS - set(SKIP))
            other.execute("ROLLBACK")
            # The failed writes were rolled back: the store is usable again once the lock is gone.
            after = _payload(await c.call_tool("journal_add", ARGS["journal_add"]))
    finally:
        if other is not None:
            other.close()
    assert not raised, f"tools that raised: {raised}"
    assert not no_envelope, f"tools without an envelope: {no_envelope}"
    locked = {n for n, env in envelopes.items() if any("database is locked" in d for d in _store_misses(env))}
    assert locked == LOCKED_WRITERS
    assert all(envelopes[n]["ok"] is False for n in locked)
    other_errors = {n: _store_misses(env) for n, env in envelopes.items() if n not in locked and _store_misses(env)}
    assert not other_errors, f"store errors other than the lock: {other_errors}"
    assert envelopes["journal_list"]["ok"] is True and len(envelopes["journal_list"]["data"]["trades"]) == 1  # reads work
    assert after["ok"] is True and not _store_misses(after)
    assert LOCKED_WRITERS <= STORE_TOOLS and not LOCKED_WRITERS & NO_QUERY_WITH_ARGS - {"events_add"}
