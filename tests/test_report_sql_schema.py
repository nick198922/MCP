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
* ``SELECT *`` reads are listed in :data:`STAR_READERS` with the code that reads their rows later as dict keys (item 97):
  every literal key of ``row["col"]`` / ``row.get("col")`` there (found by walking the AST) must be a column of the
  table. What the walk cannot reach (the engine's JSON inside cp_position) is in :data:`STAR_UNVERIFIED`, shown as
  skipped with the reason. The bindings are listed by hand: rows passed on under a name not listed are not seen.
"""

from __future__ import annotations

import ast
import asyncio
import re
import sqlite3
import sys
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Reader:
    """Where the rows of a ``SELECT *`` are read by key (item 97). ``func``: dotted name of the function in ``path``
    (None = the whole module). ``names``: variables holding one row; ``maps``: variables holding ``{role: row}``
    (``legs[k]["col"]``). ``sources`` None: every read on ``names`` in the scope; else only inside a ``for`` loop or a
    comprehension whose target is one of ``names`` and whose iterable mentions one of ``sources`` (the name is reused
    for other dicts elsewhere in the function)."""

    path: str
    func: str | None
    names: frozenset[str]
    maps: frozenset[str] = frozenset()
    sources: frozenset[str] | None = None


def _r(path: str, func: str | None, names: str, maps: str = "", sources: str | None = None) -> Reader:
    return Reader(path, func, frozenset(names.split()), frozenset(maps.split()), None if sources is None else frozenset(sources.split()))


CP = "src/tradedesk/analytics/checkpoint.py"
# (script, table) of every ``SELECT *`` read -> the code that reads its rows by key (``row["col"]``, ``row.get("col")``).
STAR_READERS: dict[tuple[str, str], tuple[Reader, ...]] = {
    ("checkpoint_report", "demo_trade"): (
        _r(CP, "_versioned", "row"),
        _r(CP, "_closed_r", "r"),
        _r(CP, "demo_forward", "t"),
        _r(CP, "fill_forward", "t"),  # with its inner past(t)
    ),
    ("checkpoint_report", "cp_position"): (
        _r("scripts/checkpoint_report.py", "load_carry", "d"),
        _r(CP, "_asof", "p q"),
        _r(CP, "carry_forward", "p", sources="pos marked"),  # p is also a cycle payload there
        _r(CP, "a_v2_forward", "p", sources="pos"),
        _r("src/tradedesk/analytics/carry_v2.py", "a_v2_shadow", "pos"),
    ),
    ("pm_gap_paper_report", "pm_gap_outcome"): (
        _r("src/tradedesk/analytics/pm_gap_report.py", None, "o", sources="outcome_rows ended_out"),
    ),
    ("condor_forward_costs", "option_leg_snapshot"): (
        _r("src/tradedesk/analytics/condor_forward.py", None, "r row", maps="legs entry"),
        _r(CP, "condor_forward", "r", sources="rows"),  # r is also an output row there
    ),
}

# What the AST walk cannot reach: shown as skipped with the reason.
STAR_UNVERIFIED: dict[tuple[str, str], str] = {
    ("checkpoint_report", "cp_position"): (
        "keys inside the JSON columns state / trade (spot_e, fees, pnl_usd, open_ts, close_ts, funding, entry_apr, "
        "entry_spread) are written by the carry paper engine, not by the schema"
    ),
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
    assert found == set(STAR_READERS), "SELECT * added or removed: list it in STAR_READERS with the code reading its rows"


def _scope(path: str, func: str | None) -> ast.AST:
    node: ast.AST = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for part in func.split(".") if func else ():
        hits = [n for n in ast.iter_child_nodes(node) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == part]
        assert hits, f"{path}: no function {func} (renamed? update STAR_READERS)"
        node = hits[0]
    return node


def _key_read(node: ast.AST, names: frozenset[str], maps: frozenset[str]) -> str | None:
    """The column of ``row["col"]`` / ``row.get("col")`` (``row`` in ``names``, or ``m[...]`` with ``m`` in ``maps``)."""
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        base, key = node.value, node.slice
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
        base, key = node.func.value, node.args[0]
    else:
        return None
    if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
        return None
    if isinstance(base, ast.Name) and base.id in names:
        return key.value
    if isinstance(base, ast.Subscript) and isinstance(base.value, ast.Name) and base.value.id in maps:
        return key.value
    return None


COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


def _row_scopes(scope: ast.AST, rd: Reader) -> list[ast.AST]:
    """Subtrees where ``rd.names`` hold a row: the whole scope, or the loops / comprehensions over ``rd.sources``."""
    if rd.sources is None:
        return [scope]

    def binds(target: ast.AST, it: ast.AST) -> bool:
        over = {n.id for n in ast.walk(it) if isinstance(n, ast.Name)}
        return isinstance(target, ast.Name) and target.id in rd.names and bool(over & rd.sources)

    out: list[ast.AST] = []
    for n in ast.walk(scope):
        if isinstance(n, ast.For) and binds(n.target, n.iter):
            out += n.body
        elif isinstance(n, COMPREHENSIONS) and any(binds(g.target, g.iter) for g in n.generators):
            out.append(n)
    return out


def star_keys(rd: Reader) -> dict[str, int]:
    """Column -> first line where ``rd`` reads it."""
    found: dict[str, int] = {}
    for sub in _row_scopes(_scope(rd.path, rd.func), rd):
        for n in ast.walk(sub):
            if (k := _key_read(n, rd.names, rd.maps)) is not None:
                found.setdefault(k, n.lineno)
    return found


def test_star_key_scanner_on_a_sample():
    """The walk sees reads on names, ``.get``, maps of rows, loops over a source only, and ignores stores and non-literals."""
    src = (
        "def f(rows, other):\n"
        "    for r in rows:\n"
        "        a = r['x'] or r.get('y') or legs[k]['z'] or legs[k].get('w')\n"
        "    for r in other:\n"
        "        b = r['not_a_row']\n"
        "    out = [r['c'] for r in sorted(rows)]\n"
        "    r['stored'] = 1\n"
        "    return r[key], r.get(key)\n"
    )
    tree = ast.parse(src)
    rd = _r("x.py", None, "r", maps="legs", sources="rows")
    keys = {k for s in _row_scopes(tree, rd) for n in ast.walk(s) if (k := _key_read(n, rd.names, rd.maps))}
    assert keys == {"x", "y", "z", "w", "c"}
    whole = _r("x.py", None, "r")
    assert {k for n in ast.walk(tree) if (k := _key_read(n, whole.names, whole.maps))} == {"x", "y", "not_a_row", "c"}


@pytest.mark.parametrize(("script", "table"), sorted(STAR_READERS), ids=[f"{s}:{t}" for s, t in sorted(STAR_READERS)])
def test_select_star_columns(con, script, table):
    """Every key the report code reads from a ``SELECT *`` row is a column of the migrated table: a renamed or dropped
    column would read as None (``.get``) or raise ``KeyError`` in the checkpoint report."""
    assert table in _tables(con)
    cols = _columns(con, table)
    bad = []
    for rd in STAR_READERS[(script, table)]:
        keys = star_keys(rd)
        assert keys, f"{rd.path}:{rd.func}: no key read on {sorted(rd.names | rd.maps)} (renamed? update STAR_READERS)"
        bad += [f"{rd.path}:{line} {table}.{k}" for k, line in sorted(keys.items()) if k not in cols]
    assert not bad, f"keys read from scripts/{script}.py SELECT * {table} that are not columns: {bad}"


@pytest.mark.parametrize(("script", "table"), sorted(STAR_UNVERIFIED), ids=[f"{s}:{t}" for s, t in sorted(STAR_UNVERIFIED)])
def test_select_star_unverified(script, table):
    assert (script, table) in STAR_READERS
    pytest.skip(f"scripts/{script}.py {table}: {STAR_UNVERIFIED[(script, table)]}")


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
