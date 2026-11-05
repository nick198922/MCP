"""Schema guard of the checkpoint reports (item 88 of docs/ideas/IDEA-20261102-standing-improvements.md).

The reports read the collector DB with SQL in strings: ``scripts/checkpoint_report.py``, ``scripts/collector_health.py``
(with the SQL fragments of ``analytics/collector_health.SPECS``), ``scripts/pm_gap_paper_report.py`` and
``scripts/condor_forward_costs.py``. Several of them guard a read with "table exists" or keep only the columns a DB has,
so a renamed table or column would give a silent empty report rather than an error. Here an empty DB is built by the
production migrations (``Store.init``) and:

* every plain SQL literal of those scripts (found by walking their AST) is compiled with ``EXPLAIN`` (SQLite resolves
  tables and columns at prepare time, rows are not needed); tables named only in a string (``PRAGMA table_info(x)``,
  ``sqlite_master ... name = 'x'``) must exist too;
* every f-string query must be listed in :data:`DYNAMIC` with the test that verifies it at run time (a new one fails
  until it is listed);
* ``SELECT *`` reads are listed in :data:`SKIP`: their table is compiled, but the columns are read later as dict keys
  by the pure analytics, not in SQL, so they are not checked here (shown as skipped with the reason).
"""

from __future__ import annotations

import ast
import asyncio
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradedesk.analytics import collector_health as ch
from tradedesk.analytics.condor_forward import EARLIEST_T0, cost_fridays
from tradedesk.store import Store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import checkpoint_report as cr  # noqa: E402
import collector_health as chs  # noqa: E402
import condor_forward_costs as cfc  # noqa: E402
import pm_gap_paper_report as pgr  # noqa: E402

SCRIPTS = ("checkpoint_report", "collector_health", "pm_gap_paper_report", "condor_forward_costs")
SQL_START = re.compile(r"^\s*(SELECT|WITH|PRAGMA|INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
NOW = datetime(2026, 11, 30, 12, tzinfo=UTC)  # after the condor T0, so checkpoint_report reads every part

# f-string queries (literal parts, "{}" for each placeholder) -> the test below that runs them on the empty schema.
DYNAMIC: dict[tuple[str, str], str] = {
    ("collector_health", "SELECT MIN({}), MAX({}) FROM {} WHERE {}"): "test_collector_health_specs",
    ("collector_health", "SELECT DISTINCT ({}) / {} FROM {} WHERE {} AND {} >= ? AND {} <= ?"): "test_collector_health_specs",
    ("pm_gap_paper_report", "SELECT {} FROM pm_gap_snapshot ORDER BY ts, market_id"): "test_pm_gap_columns",
    (
        "condor_forward_costs",
        "SELECT * FROM option_leg_snapshot WHERE day IN ({}) AND currency IN ('BTC', 'ETH') AND role IN ({})",
    ): "test_condor_loaders",
}

# (script, table) of every ``SELECT *`` read -> why its columns are not verified here.
SKIP: dict[tuple[str, str], str] = {
    ("checkpoint_report", "demo_trade"): "SELECT *: columns read as dict keys by analytics.checkpoint.build, not in SQL",
    ("checkpoint_report", "cp_position"): "SELECT *: columns (and the state / trade JSON) read by analytics.checkpoint.build",
    ("pm_gap_paper_report", "pm_gap_outcome"): "SELECT *: outcome columns read as dict keys by analytics.pm_gap_report",
    ("condor_forward_costs", "option_leg_snapshot"): "SELECT *: leg columns read as dict keys by analytics.condor_forward",
}


def _sql_literals(script: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """(plain SQL string literals, f-string queries as literal parts with "{}") of ``scripts/<script>.py``."""
    tree = ast.parse((ROOT / "scripts" / f"{script}.py").read_text(encoding="utf-8"))
    in_fstring: set[int] = set()
    dynamic: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            in_fstring.update(id(v) for v in node.values)
            key = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            if SQL_START.match(key):
                dynamic.append((node.lineno, key))
    plain = [
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in in_fstring and SQL_START.match(n.value)
    ]
    return plain, dynamic


PLAIN = [(s, line, sql) for s in SCRIPTS for line, sql in _sql_literals(s)[0]]
FOUND_DYNAMIC = {(s, key) for s in SCRIPTS for _, key in _sql_literals(s)[1]}
STAR = re.compile(r"\bSELECT\s+\*\s+FROM\s+(\w+)", re.IGNORECASE)


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("schema") / "empty.sqlite"

    async def go() -> None:
        st = Store(path)  # the production migrations, nothing else
        await st.init()
        await st.aclose()

    asyncio.run(go())
    return path


@pytest.fixture(scope="module")
def con(db: Path):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    yield c
    c.close()


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_scanner_finds_the_report_queries():
    """The AST walk sees the queries (a broken scanner would make every check below vacuous)."""
    by_script = {s: [sql for s2, _, sql in PLAIN if s2 == s] for s in SCRIPTS}
    assert any("FROM cp_log" in q for q in by_script["checkpoint_report"])
    assert any("FROM rec_paper" in q for q in by_script["checkpoint_report"])
    assert any("PRAGMA table_info(pm_gap_snapshot)" in q for q in by_script["pm_gap_paper_report"])
    assert any("FROM option_leg_snapshot" in q for q in by_script["condor_forward_costs"])
    assert len(PLAIN) >= 8


@pytest.mark.parametrize(("script", "line", "sql"), PLAIN, ids=[f"{s}:{line}" for s, line, _ in PLAIN])
def test_plain_query_compiles_on_the_schema(con, script, line, sql):
    try:
        con.execute(f"EXPLAIN {sql}", [None] * sql.count("?"))
    except sqlite3.Error as e:
        pytest.fail(f"scripts/{script}.py:{line}: {e} in {sql!r}")
    tables = _tables(con)
    named = re.findall(r"table_info\((\w+)\)", sql) + re.findall(r"\bname\s*=\s*'(\w+)'", sql)
    missing = [t for t in named if t not in tables]
    assert not missing, f"scripts/{script}.py:{line}: no table {missing} in the schema"


def test_every_dynamic_query_is_listed():
    assert FOUND_DYNAMIC == set(DYNAMIC), "f-string SQL added or changed: list it in DYNAMIC with the test that runs it"


def test_every_select_star_is_listed():
    found = {(s, t) for s, _, sql in PLAIN for t in STAR.findall(sql)} | {(s, t) for s, key in FOUND_DYNAMIC for t in STAR.findall(key)}
    assert found == set(SKIP), "SELECT * added or removed: list it in SKIP with the reason"


@pytest.mark.parametrize(("script", "table"), sorted(SKIP), ids=[f"{s}:{t}" for s, t in sorted(SKIP)])
def test_select_star_columns(con, script, table):
    assert table in _tables(con)
    pytest.skip(f"scripts/{script}.py {table}: {SKIP[(script, table)]}")


def test_collector_health_specs(db, con):
    """Every spec (switched on or not): table, group column and the script's two composed queries on the real schema."""
    tables = _tables(con)
    for spec in ch.SPECS:
        assert spec.table in tables, f"{spec.name}: no table {spec.table}"
        if spec.group_col:
            assert spec.group_col in _columns(con, spec.table), f"{spec.name}: no column {spec.table}.{spec.group_col}"
        for g in spec.groups:
            r = chs.series(con, spec, g, int(NOW.timestamp()), ch.WINDOW_DAYS)
            assert r["status"] == "never", f"{spec.name}:{g}: {r.get('error')} (ts_sql={spec.ts_sql!r}, where={spec.where!r})"


def test_pm_gap_columns(db, con):
    """``load`` keeps only the columns the DB has: a column of COLS missing from the schema would read as None silently."""
    assert set(pgr.COLS) <= _columns(con, "pm_gap_snapshot"), sorted(set(pgr.COLS) - _columns(con, "pm_gap_snapshot"))
    con.execute(f"EXPLAIN SELECT {', '.join(pgr.COLS)} FROM pm_gap_snapshot ORDER BY ts, market_id")
    assert pgr.load(str(db)) == ([], [])


def test_condor_loaders(db):
    fridays = cost_fridays(EARLIEST_T0)
    assert cfc.load_rows(db, fridays, with_hold=True) == []
    assert cfc.load_rows(db, fridays, with_hold=False) == []
    assert cfc.detect_t0(db, NOW) is None


def test_checkpoint_loaders(db):
    """``load_inputs`` turns a failed query into an ``errors`` line: none on the real schema, every part read."""
    inp = cr.load_inputs(str(db), int(NOW.timestamp() * 1000))
    assert not [e for e in inp["errors"] if "запит" in e or "БД" in e], inp["errors"]
    assert inp["demo_trades"] == [] and inp["rec_paper"] == [] and inp["pm_rows"] == []
    assert set(inp["carry"]) == {"A", "B"} and inp["condor_t0"] is None
