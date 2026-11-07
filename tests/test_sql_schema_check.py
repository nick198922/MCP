"""scripts/sql_schema_check.py (items 157, 175, 200 of docs/ideas/IDEA-20261102-standing-improvements.md): SELECT literals
vs schema."""

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


CONSTS = """
TABLE = "journal"
COLS = ("id", "symbol", "pnl")
TAG = "src:demo"
BAD = "symbl"
"""

IMPORTING = """
from consts import COLS, TABLE
import consts as cm


def read(con, days):
    con.execute(f"SELECT {', '.join(COLS)} FROM {TABLE} WHERE symbol LIKE '%{cm.TAG}%'")
    con.execute(f"SELECT {cm.BAD} FROM {TABLE}")
    con.execute(f"SELECT id FROM journal WHERE opened_ts IN ({','.join('?' * len(days))})")
"""


def test_fstring_from_constants_of_another_module():
    sources = {"scripts/consts.py": CONSTS, "scripts/reader.py": IMPORTING}
    qs = sc.extract_queries(IMPORTING, "scripts/reader.py", {"consts": CONSTS})
    assert [(q.line, q.sql) for q in qs] == [
        (7, "SELECT id, symbol, pnl FROM journal WHERE symbol LIKE '%src:demo%'"),
        (8, "SELECT symbl FROM journal"),
        (9, "SELECT id FROM journal WHERE opened_ts IN (?)"),  # a run of placeholders: one repetition
    ]
    assert all(q.expanded for q in qs)
    rep = sc.check(sources, MIGRATIONS)
    assert {(f.query.line, f.kind) for f in rep.findings if f.kind != "ok"} == {(8, "missing column")}
    assert sc.module_name("scripts/consts.py") == "consts"
    assert sc.module_name("src/tradedesk/store/db.py") == "tradedesk.store.db"
    assert sc.module_name("src/tradedesk/store/__init__.py") == "tradedesk.store"
    without = sc.extract_queries(IMPORTING, "scripts/reader.py")  # the other module is not scanned
    assert {q.why for q in without if q.dynamic} == {"module consts not scanned"}


CONCAT = """
class Store:
    @staticmethod
    def _gaps(col):
        \"\"\"How many parts are missing.\"\"\"
        return f"(length({col}) - length(replace({col}, ',', '')))"

    def rows(self, con, status, symbol, tag):
        where: list[str] = []
        for col, v in (("symbol", symbol), ("pnl", status)):
            if v:
                where.append(f"{col} = ?")
        if tag:
            where.append("symbl LIKE ?")
        sql = "SELECT * FROM journal" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id LIMIT ?"
        con.execute(sql)
        con.execute("SELECT id FROM journal WHERE " + self._gaps("symbol") + " = 0")
"""


def test_concatenation_with_appended_where_and_helper_call():
    qs = sc.extract_queries(CONCAT, "s.py")
    assert sorted(q.sql for q in qs if q.line == 15) == [
        "SELECT * FROM journal ORDER BY id LIMIT ?",
        "SELECT * FROM journal WHERE symbl LIKE ? AND symbol = ? AND pnl = ? ORDER BY id LIMIT ?",
    ]  # every appended condition at once: a superset, so each column is checked
    assert [q.sql for q in qs if q.line == 17] == ["SELECT id FROM journal WHERE (length(symbol) - length(replace(symbol, ',', ''))) = 0"]
    rep = sc.check({"s.py": CONCAT}, MIGRATIONS)
    assert {(f.query.line, f.kind) for f in rep.findings if f.kind != "ok"} == {(15, "missing column")}
    assert not rep.dynamic


TRULY_DYNAMIC = """
def helper(col):
    if col:
        return col
    return "id"


def read(con, spec, table, extra):
    con.execute(f"SELECT {spec.ts_sql} FROM {spec.table}")
    con.execute(f"SELECT COUNT(*) FROM {table}")
    where = ["id = ?"]
    where.extend(extra)
    con.execute("SELECT * FROM journal WHERE " + " AND ".join(where))
    con.execute("SELECT pnl FROM " + helper("journal"))
    con.execute(f"SELECT id FROM journal WHERE symbol IN ({','.join('x' * len(extra))})")
"""


def test_truly_dynamic_stays_skipped_with_reason():
    rep = sc.check({"d.py": TRULY_DYNAMIC}, MIGRATIONS)
    assert not rep.findings
    assert [(q.line, q.why) for q in rep.dynamic] == [
        (9, "spec.ts_sql"),  # attribute of an object
        (10, "parameter table"),
        (13, "where.extend()"),  # a list extended by arbitrary items
        (14, "helper() is more than one return"),
        (15, "'x' * len(extra)"),  # only `?`, commas and spaces count as a placeholder run
    ]


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
