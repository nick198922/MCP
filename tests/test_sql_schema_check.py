"""scripts/sql_schema_check.py (item 157 of docs/ideas/IDEA-20261102-standing-improvements.md): SELECT literals vs schema."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sql_schema_check as sc  # noqa: E402

MIGRATIONS = {
    1: ["CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, symbol TEXT, opened_ts INTEGER)"],
    2: ["ALTER TABLE journal ADD COLUMN pnl REAL"],
}

SOURCE = '''
"""Select what you need from the menu: a docstring, not a query."""

def read(con, table, col):
    ok = con.execute("SELECT id, symbol, pnl FROM journal WHERE opened_ts >= ? ORDER BY id").fetchall()
    joined = con.execute(
        "SELECT j.symbol, COUNT(*) FROM journal j "
        "JOIN (SELECT id FROM journal WHERE pnl > :x) k ON k.id = j.id GROUP BY 1"
    )
    no_table = con.execute("select * from journl")
    no_column = con.execute("SELECT symbl FROM journal")
    dyn_f = con.execute(f"SELECT {col} FROM {table}")
    dyn_add = con.execute("SELECT * FROM jornal WHERE " + col + " = ?")
    dyn_fmt = con.execute("SELECT x FROM {t}".format(t=table))
    dyn_tpl = "SELECT %s FROM nowhere"
    cte = con.execute("WITH last AS (SELECT max(opened_ts) AS ts FROM journal) SELECT ts FROM last")
    remote = "SELECT name FROM labels.addresses WHERE blockchain = 'ethereum'"
    prose = "selected rows are shown below"
    return ok, joined, no_table, no_column, dyn_f, dyn_add, dyn_fmt, dyn_tpl, cte, remote, prose
'''


def _by_line(rep: sc.Report) -> dict[int, str]:
    return {f.query.line: f.kind for f in rep.findings}


def test_extract_queries_static_and_dynamic():
    qs = sc.extract_queries(SOURCE, "demo.py")
    static = [q for q in qs if q.dynamic is None]
    dynamic = {q.line: q.dynamic for q in qs if q.dynamic}
    assert [q.line for q in static] == [5, 7, 10, 11, 16, 17]
    assert "JOIN (SELECT id FROM journal" in static[1].sql  # implicit concatenation is one literal
    assert dynamic == {12: "f-string", 13: "concatenated", 14: ".format()", 15: "placeholder"}
    assert not any("menu" in q.sql or "selected" in q.sql for q in qs)


def test_check_classifies_ok_missing_table_column_and_skips_dynamic():
    rep = sc.check({"demo.py": SOURCE}, MIGRATIONS)
    kinds = _by_line(rep)
    assert kinds[5] == "ok"  # column added by a later migration, unbound `?`
    assert kinds[7] == "ok"  # alias, join, subquery, named parameter
    assert kinds[16] == "ok"  # CTE
    assert kinds[10] == "missing table"
    assert kinds[11] == "missing column"
    assert kinds[17] == "other database"
    assert {q.line for q in rep.dynamic} == {12, 13, 14, 15}  # never prepared, so the typo in line 13 is not reported
    assert rep.failed
    assert rep.tables == 1
    text = sc.render(rep)
    assert "## missing table (1):" in text and "no such table: journl" in text
    assert "## missing column (1):" in text and "no such column: symbl" in text
    assert "demo.py:12 [f-string: parameter col]" in text


EXPANDABLE = """
TABLES = [("journal", "opened_ts"), ("journl", "ts")]
COLS = "id symbol pnl"
BAD_COLS = ("id", "symbl")


def read(con, arbitrary, status):
    for table, col in TABLES:
        con.execute(f"SELECT max({col}), max(rowid) FROM {table}")
    for t in ("journal", "journal"):
        con.execute(f"SELECT count(*) FROM {t}")
    sel = [c for c in COLS.split() if c]
    con.execute(f"SELECT {', '.join(sel)} FROM journal ORDER BY id")
    rv = "pnl" if status else "NULL AS pnl"
    con.execute(f"SELECT id, {rv} FROM journal")
    con.execute(f"SELECT {', '.join(BAD_COLS)} FROM journal")
    con.execute("SELECT * FROM journal" + (" WHERE id = ?" if status else "") + " ORDER BY id")
    con.execute(f"SELECT * FROM {arbitrary}")
"""


def test_constant_fragments_are_expanded_and_checked():
    qs = sc.extract_queries(EXPANDABLE, "x.py")
    assert sum(1 for q in qs if q.expanded) == 2 + 1 + 1 + 2 + 1 + 2  # table loop, repeated t (deduped), join, rv, bad join, concat
    assert [(q.line, q.why) for q in qs if q.dynamic] == [(18, "parameter arbitrary")]
    assert sorted(q.sql for q in qs if q.line == 9) == [
        "SELECT max(opened_ts), max(rowid) FROM journal",
        "SELECT max(ts), max(rowid) FROM journl",
    ]  # the tuple target keeps table and column correlated
    rep = sc.check({"x.py": EXPANDABLE}, MIGRATIONS)
    bad = {(f.query.line, f.kind) for f in rep.findings if f.kind != "ok"}
    assert bad == {(9, "missing table"), (16, "missing column")}  # a typo in a constant fragment is found
    assert rep.failed
    assert len(rep.expanded_sites) == 6 and [q.line for q in rep.dynamic] == [18]
    assert "6 expanded from constants" in sc.render(rep)


def test_clean_source_does_not_fail():
    rep = sc.check({"ok.py": 'Q = "SELECT symbol FROM journal WHERE id = ?"\n'}, MIGRATIONS)
    assert not rep.failed and [f.kind for f in rep.findings] == ["ok"]


def test_own_database_tables_are_added_per_file():
    own = 'DDL = """\nCREATE TABLE IF NOT EXISTS book (vault TEXT, cash REAL);\n"""\nQ = "SELECT cash FROM book WHERE vault = ?"\n'
    other = 'Q = "SELECT cash FROM book"\n'
    rep = sc.check({"a.py": own, "b.py": other}, MIGRATIONS)
    assert {f.query.file: f.kind for f in rep.findings} == {"a.py": "ok", "b.py": "missing table"}  # not leaked to b.py
    assert rep.local_schema == ["a.py"]


def test_multiple_statements_and_syntax_error_are_not_failures():
    src = 'A = "SELECT id FROM journal; SELECT 1 FROM journal"\nB = "SELECT id FROM journal WHERE"\n'
    rep = sc.check({"m.py": src}, MIGRATIONS)
    assert [f.kind for f in rep.findings] == ["multiple statements", "other error"]
    assert not rep.failed


def test_unparsable_file_is_listed():
    rep = sc.check({"bad.py": "def (:\n"}, MIGRATIONS)
    assert rep.unparsable == ["bad.py"] and not rep.findings


def test_repo_migrations_read_without_import():
    text = (ROOT / sc.SCHEMA_FILE).read_text(encoding="utf-8")
    migrations = sc.migrations_from_source(text)
    assert max(migrations) == int(text.split("SCHEMA_VERSION = ", 1)[1].split()[0])
    conn = sc.build_schema(migrations)
    try:
        assert sc.check_query(conn, "SELECT id, symbol FROM journal WHERE id = ?") == ("ok", "")
    finally:
        conn.close()
