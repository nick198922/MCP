from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tradedesk.store import SCHEMA_VERSION, Store


@pytest.fixture
async def store() -> Store:
    s = Store(":memory:")
    await s.init()
    yield s
    await s.aclose()


async def test_schema_bootstrap(store: Store):
    assert await store.version() == SCHEMA_VERSION
    for t in ("atm_term", "funding_hist", "basis_hist", "snapshots"):
        assert await store.table_count(t) == 0


async def test_atm_term_write_read_replaces_on_duplicate(store: Store):
    rows = [
        {"expiry": "2026-10-30T08:00:00+00:00", "days": 20.0, "forward": 83000.0, "atm_iv": 0.42, "rr25": -0.03, "fly25": 0.009},
        {"expiry": "2026-11-27T08:00:00+00:00", "days": 48.0, "forward": 83500.0, "atm_iv": 0.46, "rr25": -0.025, "fly25": 0.012},
    ]
    inserted = await store.insert_atm_term(ts=1_000_000, currency="BTC", rows=rows)
    assert inserted == 2
    assert await store.table_count("atm_term") == 2
    series = await store.atm_term_series("BTC", "2026-10-30T08:00:00+00:00", 0, 2_000_000)
    assert len(series) == 1 and series[0]["atm_iv"] == 0.42

    # Same (ts, currency, expiry) is REPLACEd, not duplicated.
    rows[0]["atm_iv"] = 0.44
    await store.insert_atm_term(ts=1_000_000, currency="BTC", rows=rows[:1])
    assert await store.table_count("atm_term") == 2
    series = await store.atm_term_series("BTC", "2026-10-30T08:00:00+00:00", 0, 2_000_000)
    assert series[0]["atm_iv"] == 0.44


async def test_funding_and_basis_round_trip(store: Store):
    await store.insert_funding(
        ts=1_000_000,
        rows=[
            {"venue": "binanceusdm", "base": "BTC", "rate_per_interval": 0.0001, "interval_hours": 8.0, "annualized": 0.1095},
            {"venue": "bybit", "base": "BTC", "rate_per_interval": 0.00008, "interval_hours": 8.0, "annualized": 0.0876},
        ],
    )
    s = await store.funding_series("binanceusdm", "BTC", 0, 2_000_000)
    assert len(s) == 1 and s[0]["annualized"] == pytest.approx(0.1095)

    await store.insert_basis(
        ts=1_000_000,
        currency="BTC",
        rows=[{"expiry": "2026-12-25T08:00:00+00:00", "days": 76.0, "basis_pct": 0.011, "annualized_simple": 0.053}],
    )
    b = await store.basis_series("BTC", "2026-12-25T08:00:00+00:00", 0, 2_000_000)
    assert len(b) == 1 and b[0]["basis_pct"] == pytest.approx(0.011)


async def test_snapshot_json_round_trip(store: Store):
    sid = await store.insert_snapshot(ts=1_000_000, kind="iv_surface", payload={"currency": "BTC", "atm": 0.42})
    assert sid > 0
    assert await store.table_count("snapshots") == 1


async def test_migration_block_is_atomic_and_retried_after_a_failure(tmp_path, monkeypatch):
    from tradedesk.store import schema

    path = tmp_path / "half.sqlite"
    s = Store(path)
    await s.init()
    await s.aclose()

    v = SCHEMA_VERSION + 1
    monkeypatch.setitem(
        schema.MIGRATIONS,
        v,
        ["ALTER TABLE atm_term ADD COLUMN probe_a REAL", "ALTER TABLE no_such_table ADD COLUMN probe_b REAL"],
    )
    s = Store(path)
    with pytest.raises(Exception, match="no_such_table"):
        await s.init()
    assert s._conn is None  # failed init leaves no half-open connection behind

    s = Store(path)
    monkeypatch.setitem(schema.MIGRATIONS, v, ["SELECT 1"])
    await s.init()
    try:
        assert await s.version() == v
        cur = await s.conn.execute("PRAGMA table_info(atm_term)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "probe_a" not in cols  # the first ALTER of the failed block was rolled back

        monkeypatch.setitem(schema.MIGRATIONS, v + 1, ["ALTER TABLE atm_term ADD COLUMN probe_a REAL"])
        await s.aclose()
        await s.init()  # the same ALTER now applies cleanly: no "duplicate column"
        assert await s.version() == v + 1
    finally:
        await s.aclose()


_FAILING_WRITES = {
    "event_insert_duplicate": ("event_insert", {"ts": 1, "kind": "cpi", "label": "seed", "ignore_duplicate": False}),
    "rule_insert_duplicate": (
        "rule_insert",
        {"name": "seed", "expr": "1 > 0", "message": None, "cooldown_seconds": 60, "enabled": True, "created_ts": 1},
    ),
    "journal_insert_not_null": ("journal_insert", {"opened_ts": 1, "symbol": None, "side": "long", "size": 1.0, "entry": 1.0}),
    "position_insert_not_null": (
        "position_insert",
        {
            "opened_ts": 1, "instrument": None, "kind": "perp", "side": "long", "size": 1.0, "entry": None, "mark": None,
            "delta": None, "gamma": None, "vega": None, "theta": None, "tags": None,
        },
    ),
}  # fmt: skip


@pytest.mark.parametrize("case", sorted(_FAILING_WRITES))
async def test_failed_write_rolls_back_and_releases_the_lock(tmp_path, case):
    """A write that fails with IntegrityError closes its implicit transaction: another connection (the collector) can
    write at once, and the store's next write goes through."""
    import sqlite3

    path = tmp_path / "rb.sqlite"
    s = Store(path)
    await s.init()
    try:
        await s.event_insert(ts=1, kind="cpi", label="seed")
        await s.rule_insert(name="seed", expr="1 > 0", message=None, cooldown_seconds=60, enabled=True, created_ts=1)
        method, kwargs = _FAILING_WRITES[case]
        with pytest.raises(sqlite3.IntegrityError):
            await getattr(s, method)(**kwargs)
        assert s.conn.in_transaction is False
        other = sqlite3.connect(path, timeout=0.1)  # a second writer; without the rollback: "database is locked"
        try:
            other.execute("INSERT INTO events (ts, kind, label) VALUES (2, 'cpi', 'other')")
            other.commit()
        finally:
            other.close()
        assert await s.event_insert(ts=3, kind="cpi", label="next") > 0
        assert await s.event_count() == 3
    finally:
        await s.aclose()


async def test_write_block_is_all_or_nothing(store: Store):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        async with store._write() as conn:
            await conn.execute("INSERT INTO events (ts, kind, label) VALUES (1, 'cpi', 'a')")
            await conn.execute("INSERT INTO events (ts, kind, label) VALUES (1, 'cpi', 'a')")
    assert store.conn.in_transaction is False
    assert await store.event_count() == 0  # the first INSERT of the failed block is rolled back too
    assert await store.event_insert(ts=1, kind="cpi", label="a") > 0


async def test_failed_write_does_not_undo_another_tasks_pending_write(store: Store):
    """Writes share one connection: the rollback of a failing write must not drop rows another task has written but
    not yet committed (the write lock keeps the two transactions apart)."""
    import asyncio
    import sqlite3

    await store.event_insert(ts=1, kind="cpi", label="seed")
    inside = asyncio.Event()

    async def slow_writer() -> None:
        async with store._write() as conn:
            await conn.execute("INSERT INTO events (ts, kind, label) VALUES (2, 'cpi', 'pending')")
            inside.set()
            await asyncio.sleep(0.05)  # the failing write below runs now, if it is not kept out

    async def failing_writer() -> None:
        await inside.wait()
        with pytest.raises(sqlite3.IntegrityError):
            await store.event_insert(ts=1, kind="cpi", label="seed", ignore_duplicate=False)

    await asyncio.gather(slow_writer(), failing_writer())
    assert store.conn.in_transaction is False
    assert [e["label"] for e in await store.events_list()] == ["seed", "pending"]


async def test_cancelled_write_is_rolled_back_even_if_its_statement_was_still_queued(tmp_path):
    """aiosqlite runs statements one by one in its own thread. Cancelling a write while its INSERT still waits behind a
    slow query does not drop the INSERT: it runs later and opens a transaction. The rollback must be queued after it,
    or the next write commits the cancelled row (and the file stays write-locked until then)."""
    import asyncio

    s = Store(tmp_path / "cancel.sqlite")
    await s.init()
    try:
        slow = asyncio.create_task(
            s.conn.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) SELECT SUM(x) FROM c")
        )
        await asyncio.sleep(0)  # the slow SELECT is queued first and keeps the worker thread busy

        async def cancelled_writer() -> None:
            async with s._write() as conn:
                await conn.execute("INSERT INTO events (ts, kind, label) VALUES (1, 'cpi', 'cancelled')")

        w = asyncio.create_task(cancelled_writer())
        for _ in range(3):
            await asyncio.sleep(0)  # let the writer queue its INSERT behind the slow SELECT
        assert not slow.done(), "the slow query finished before the cancel: the test would not cover the queued case"
        w.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w
        await slow
        assert s.conn.in_transaction is False
        assert await s.event_insert(ts=2, kind="cpi", label="next") > 0  # commits whatever the connection holds
        assert [e["label"] for e in await s.events_list()] == ["next"]
    finally:
        await s.aclose()


# --- every write goes through Store._write() (source scan of store/db.py) ---------------------------

_TX_METHODS = frozenset({"_migrate", "_write"})  # the only places that may commit() / rollback()
_DML = re.compile(r"\s*(?:WITH\b.*?\b)?(INSERT|UPDATE|DELETE|REPLACE)\b", re.I | re.S)
_EXECUTE = frozenset({"execute", "executemany", "executescript"})


def _is_write_block(node: ast.AST) -> bool:
    return isinstance(node, ast.AsyncWith) and any(
        isinstance(i.context_expr, ast.Call)
        and isinstance(i.context_expr.func, ast.Attribute)
        and i.context_expr.func.attr == "_write"  # self._write() in the store, store._write() / st._write() in scripts
        for i in node.items
    )


def _sql_text(node: ast.AST) -> str:
    return "".join(n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str))


def write_discipline_problems(source: str) -> list[str]:
    """Problems in a store module's or script's source; pure. (1) ``commit()`` / ``rollback()`` outside ``_migrate`` /
    ``_write``; (2) an ``execute*`` call outside ``async with <store>._write()`` whose SQL starts with
    INSERT/UPDATE/DELETE/REPLACE (any case, also after ``WITH``), or whose SQL is a bare name (``sql``) in a function
    that holds such a literal. Reads (SELECT) before the write block are fine. Only top-level functions and class
    methods are scanned (nested functions as part of their outer one). Not caught: DML built entirely from names in a
    function with no DML literal, statements at module level."""
    tree = ast.parse(source)
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        funcs += [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    problems: list[str] = []
    for fn in funcs:
        if fn.name in _TX_METHODS:
            continue
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        problems += [f"{fn.name}:{c.lineno} {c.func.attr}()" for c in calls if c.func.attr in ("commit", "rollback")]
        writes = any(isinstance(n, ast.Constant) and isinstance(n.value, str) and _DML.match(n.value) for n in ast.walk(fn))
        inside = {id(c) for blk in ast.walk(fn) if _is_write_block(blk) for c in ast.walk(blk)}
        for c in calls:
            if c.func.attr not in _EXECUTE or id(c) in inside or not c.args:
                continue
            sql = _sql_text(c.args[0])
            if _DML.match(sql) or (not sql and writes):
                problems.append(f"{fn.name}:{c.lineno} {c.func.attr}() outside _write()")
    return problems


def test_write_discipline_checker_on_fixtures():
    ok = (
        "class S:\n"
        "    async def _write(self):\n        await self.conn.commit()\n        await self.conn.rollback()\n"
        "    async def put(self):\n        await self.conn.execute('SELECT x FROM t')\n        async with self._write() as conn:\n"
        "            await conn.execute(f'INSERT INTO t VALUES ({1})')\n"
        "    async def get(self):\n        return await self.conn.execute('SELECT 1')\n"
    )
    assert write_discipline_problems(ok) == []
    bad = (
        "class S:\n"
        "    async def put(self):\n        await self.conn.execute('INSERT INTO t VALUES (1)')\n        await self.conn.commit()\n"
        "    async def upd(self):\n        sql = 'UPDATE t SET x = 1'\n        async with self._tx_lock:\n"
        "            await self.conn.execute(sql)\n"
        "async def script(st):\n        await st.conn.execute('delete from t')\n"
        "        await st.conn.execute('WITH x AS (SELECT 1) insert into t SELECT * FROM x')\n"
        "        async with st._write() as conn:\n            await conn.execute('update t set x = 2')\n"
    )
    assert write_discipline_problems(bad) == [
        "script:10 execute() outside _write()",
        "script:11 execute() outside _write()",
        "put:4 commit()",
        "put:3 execute() outside _write()",
        "upd:8 execute() outside _write()",
    ]


# Scripts that write outside <store>._write(): "file" (whole file) or "file:function" -> (problems found, reason).
# The count is exact, so a new raw write in one of these places still fails the test.
_SCRIPT_WRITES_ALLOWED: dict[str, tuple[int, str]] = {
    "copy_vaults.py": (
        8,
        "not the Store: its own paper-book SQLite file; load() and step() run inside `with con:` in run(), which "
        "commits, or rolls back on any error",
    ),
    "hyperliquid_import.py:mark_airdrop": (
        2,
        "not the Store: a synchronous sqlite3 connection in a one-off owner command (mark-airdrop); an error ends the "
        "process, and closing the connection discards the open transaction",
    ),
    "bybit_import.py:store_options": (
        1,
        "the nested one() helper only runs SELECTs (its SQL arrives as a name); every write there goes through the "
        "nested write() helper, i.e. st._write(), or a Store method",
    ),
}


def test_every_store_write_goes_through_write_block():
    import tradedesk.store.db as db

    source = Path(db.__file__).read_text(encoding="utf-8")
    assert "async def _write(" in source
    assert write_discipline_problems(source) == []

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    found: dict[str, list[str]] = {}
    for path in sorted(scripts.rglob("*.py")):
        name = path.relative_to(scripts).as_posix()
        for p in write_discipline_problems(path.read_text(encoding="utf-8")):
            fn = p.split(":", 1)[0]
            key = f"{name}:{fn}" if f"{name}:{fn}" in _SCRIPT_WRITES_ALLOWED else name
            found.setdefault(key, []).append(p)
    unexplained = {k: v for k, v in found.items() if k not in _SCRIPT_WRITES_ALLOWED}
    assert unexplained == {}, "raw writes in scripts/: use a Store method or `async with store._write()`"
    assert {k: len(v) for k, v in found.items()} == {k: n for k, (n, _) in _SCRIPT_WRITES_ALLOWED.items()}


async def test_concurrent_inits_on_one_file_apply_each_migration_once(tmp_path):
    # Several processes (the service, a backfill script, parallel test workers) may open a fresh or older database at
    # the same time: the version must be read under the write lock, or the second one repeats an applied ALTER.
    import asyncio

    path = tmp_path / "shared.sqlite"
    stores = [Store(path) for _ in range(6)]
    try:
        await asyncio.gather(*(s.init() for s in stores))
        assert {await s.version() for s in stores} == {SCHEMA_VERSION}
    finally:
        for s in stores:
            await s.aclose()
