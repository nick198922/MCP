"""Check the SELECT queries written as string literals in scripts/*.py and src/tradedesk/ against the SQLite schema.

Read-only, stdlib only (``ast`` + ``re`` + ``sqlite3``). The schema is built in an in-memory database from the project's
own migrations: ``MIGRATIONS`` of src/tradedesk/store/schema.py is read with ``ast.literal_eval`` (nothing is imported)
and its statements are applied in version order, as ``Store.init()`` does. Queries are collected from the sources with
``ast`` (no code is executed): a string constant counts as a query when it starts with ``SELECT`` or ``WITH ... AS (``
(any case) and has a ``FROM``. Each static query is prepared with ``EXPLAIN`` against the empty schema, so SQLite itself
resolves tables, aliases, joins and subqueries; missing bindings (``?``, ``:name``) are expected and count as success.

Categories:

- ``missing table`` / ``missing column``: SQLite answers ``no such table`` / ``no such column`` (exit code 1);
- ``other database``: a schema-qualified table (``labels.addresses``) of a remote engine such as Dune, information;
- ``other error``: any other preparation error (syntax, unknown function, a string that only looks like SQL), information;
- ``skipped (dynamic)``: the text is built at run time (f-string, ``+`` or ``%`` with other values, ``.format``,
  ``{name}``/``%s`` placeholders) and is not checked; listed with the reason;
- ``skipped (multiple statements)``: a script for ``executescript``, not checked.

A file whose own literals hold ``CREATE TABLE`` (a script with its own database file, scripts/copy_vaults.py) is checked
against the migrated schema plus those tables.

Limits: only literals are seen (a query assembled from constants elsewhere is either skipped or not found); a reader
of another database file that does not create its tables shows up as ``missing table``; ``INSERT ... SELECT`` and DDL are not
checked; columns added by the code at run time (not by a migration) are not known.

    uv run python scripts/sql_schema_check.py [--root .] [--out ~/.cache/tradedesk-agents/tmp/sql_schema_check.md]
"""

from __future__ import annotations

import argparse
import ast
import re
import sqlite3
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_FILE = "src/tradedesk/store/schema.py"
SCOPES = {"scripts": ("scripts", "*.py"), "src": ("src/tradedesk", "**/*.py")}

_SQL_START = re.compile(r"^\s*(SELECT\b|WITH\s+(RECURSIVE\s+)?\w+\s*(\([^)]*\)\s*)?AS\s*\()", re.IGNORECASE)
_FROM = re.compile(r"\bFROM\b", re.IGNORECASE)
_CREATE_TABLE = re.compile(r"^\s*CREATE\s+(TEMP\s+|TEMPORARY\s+)?TABLE\b", re.IGNORECASE | re.MULTILINE)
_QUALIFIED = re.compile(r"no such table: (?!main\.|temp\.)\w+\.\w+")
_PLACEHOLDER = re.compile(r"\{[A-Za-z_]\w*(\.\w+)*\}|\{\}|\{\d+\}|%[sdr]|%\([A-Za-z_]\w*\)")


def looks_like_select(text: str) -> bool:
    """True when a string reads as a SELECT (or WITH ... AS ( ... ) query with a FROM. Pure."""
    return bool(_SQL_START.match(text) and _FROM.search(text))


@dataclass(frozen=True)
class Query:
    file: str
    line: int
    sql: str
    dynamic: str | None = None  # reason when the text is built at run time; None for a static literal


def _fstring_text(node: ast.JoinedStr) -> str:
    return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{…}" for v in node.values)


def extract_queries(source: str, file: str = "<src>") -> list[Query]:
    """SELECT queries written as string literals in one Python source, static or marked dynamic. Raises SyntaxError. Pure."""
    tree = ast.parse(source, filename=file)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    out: list[Query] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = _fstring_text(node)
            if looks_like_select(text):
                out.append(Query(file, node.lineno, text, "f-string"))
            continue
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        up = parent.get(node)
        if isinstance(up, ast.JoinedStr | ast.Expr):  # a piece of an f-string (seen above) or a docstring
            continue
        text = node.value
        if not looks_like_select(text):
            continue
        reason = None
        if isinstance(up, ast.BinOp) and isinstance(up.op, ast.Add | ast.Mod):
            reason = "concatenated" if isinstance(up.op, ast.Add) else "%-formatted"
        elif isinstance(up, ast.Attribute) and up.attr in {"format", "format_map", "replace"}:
            reason = f".{up.attr}()"
        elif _PLACEHOLDER.search(text):
            reason = "placeholder"
        out.append(Query(file, node.lineno, text, reason))
    return sorted(out, key=lambda q: q.line)


def local_ddl(source: str) -> list[str]:
    """Static string literals with ``CREATE TABLE`` in one source: a script that keeps its own database. Pure."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and _CREATE_TABLE.search(n.value)]


def migrations_from_source(text: str) -> dict[int, list[str]]:
    """``MIGRATIONS`` of schema.py read as a literal (no import, no execution). Raises ValueError when absent. Pure."""
    for node in ast.parse(text).body:
        target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0] if isinstance(node, ast.Assign) else None
        if isinstance(target, ast.Name) and target.id == "MIGRATIONS" and node.value is not None:
            return ast.literal_eval(node.value)
    raise ValueError("MIGRATIONS not found")


def build_schema(migrations: dict[int, list[str]]) -> sqlite3.Connection:
    """An in-memory database with every migration applied in version order (no disk I/O)."""
    conn = sqlite3.connect(":memory:")
    for v in sorted(migrations):
        for stmt in migrations[v]:
            conn.execute(stmt)
    return conn


@dataclass(frozen=True)
class Finding:
    query: Query
    kind: str  # ok | missing table | missing column | other error | multiple statements
    detail: str = ""


def check_query(conn: sqlite3.Connection, sql: str) -> tuple[str, str]:
    """(kind, detail) of one static query prepared with EXPLAIN against ``conn``. Never raises."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            conn.execute("EXPLAIN " + sql)
    except sqlite3.ProgrammingError as e:
        msg = str(e)
        if "binding" in msg.lower():  # prepared fine, only the parameters are absent
            return "ok", ""
        if "one statement" in msg:
            return "multiple statements", msg
        return "other error", msg
    except sqlite3.Error as e:
        msg = str(e)
        if _QUALIFIED.match(msg):  # `labels.addresses`: a remote engine's schema (Dune), not an SQLite table
            return "other database", msg
        if msg.startswith("no such table"):
            return "missing table", msg
        if msg.startswith("no such column"):
            return "missing column", msg
        return "other error", msg
    return "ok", ""


@dataclass
class Report:
    tables: int = 0
    files: int = 0
    findings: list[Finding] = field(default_factory=list)
    dynamic: list[Query] = field(default_factory=list)
    unparsable: list[str] = field(default_factory=list)
    local_schema: list[str] = field(default_factory=list)  # files checked with their own CREATE TABLE added

    def of(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    @property
    def failed(self) -> bool:
        return any(f.kind in {"missing table", "missing column"} for f in self.findings)


def check(sources: dict[str, str], migrations: dict[int, list[str]]) -> Report:
    """``sources`` = {relative path: Python text}. Only in-memory databases, no file I/O.

    A file whose literals create tables of their own (``CREATE TABLE``) is checked against the migrated schema plus
    those tables: such a script keeps its own database file (scripts/copy_vaults.py)."""
    conn = build_schema(migrations)
    try:
        rep = Report(files=len(sources))
        rep.tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        for path in sorted(sources):
            try:
                queries = extract_queries(sources[path], path)
            except SyntaxError:
                rep.unparsable.append(path)
                continue
            if not queries:
                continue
            own = local_ddl(sources[path]) if path != SCHEMA_FILE else []
            target = build_schema(migrations) if own else conn
            try:
                for ddl in own:
                    try:
                        target.executescript(ddl)
                    except sqlite3.Error:
                        pass  # a fragment or a template: the queries that need it will show up as missing
                if own:
                    rep.local_schema.append(path)
                for q in queries:
                    if q.dynamic:
                        rep.dynamic.append(q)
                        continue
                    kind, detail = check_query(target, q.sql)
                    rep.findings.append(Finding(q, kind, detail))
            finally:
                if target is not conn:
                    target.close()
        return rep
    finally:
        conn.close()


def _one_line(sql: str, width: int = 110) -> str:
    s = " ".join(sql.split())
    return s if len(s) <= width else s[: width - 1] + "…"


def render(rep: Report) -> str:
    static = len(rep.findings)
    lines = [
        f"# sql_schema_check: {rep.tables} tables in the migrated schema, {rep.files} files, "
        f"{static + len(rep.dynamic)} SELECT literals ({static} static, {len(rep.dynamic)} dynamic)",
        "",
        f"ok: {len(rep.of('ok'))}",
    ]
    if rep.local_schema:
        lines.append("checked with their own CREATE TABLE added: " + ", ".join(rep.local_schema))
    for kind in ("missing table", "missing column", "other database", "other error", "multiple statements"):
        found = rep.of(kind)
        lines += ["", f"## {kind} ({len(found)}):"]
        lines += [f"  {f.query.file}:{f.query.line}: {f.detail}\n    {_one_line(f.query.sql)}" for f in found] or ["  -"]
    lines += ["", f"## skipped (dynamic, not checked) ({len(rep.dynamic)}):"]
    lines += [f"  {q.file}:{q.line} [{q.dynamic}] {_one_line(q.sql, 90)}" for q in rep.dynamic] or ["  -"]
    if rep.unparsable:
        lines += ["", f"## unparsable ({len(rep.unparsable)}):"] + [f"  {p}" for p in rep.unparsable]
    return "\n".join(lines) + "\n"


def load_sources(root: Path) -> dict[str, str]:
    """{path relative to root: text} of scripts/*.py and src/tradedesk/**/*.py."""
    out: dict[str, str] = {}
    for rel, pattern in SCOPES.values():
        base = root / rel
        if base.is_dir():
            for p in sorted(base.glob(pattern)):
                if "__pycache__" not in p.parts and p.is_file():
                    out[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="repository root")
    ap.add_argument("--out", type=Path, help="also write the report to this file")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    migrations = migrations_from_source((root / SCHEMA_FILE).read_text(encoding="utf-8"))
    report = check(load_sources(root), migrations)
    text = render(report)
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
