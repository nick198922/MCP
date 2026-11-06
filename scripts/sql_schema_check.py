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
- ``expanded``: an f-string or ``+`` chain whose fragments are all constants (module constants, loop variables over a
  constant list/tuple, conditional expressions, ``", ".join(COLS)``, ``COLS.split()``) is expanded into every variant
  and each variant is checked like a static query (a ``missing`` finding names the variant's line);
- ``skipped (dynamic)``: the text is built at run time from something arbitrary (parameter, attribute, call result, list
  built in a loop, ``%``, ``.format``, ``{name}``/``%s`` placeholders) and is not checked; listed with the reason and the
  first unresolved expression;
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
    dynamic: str | None = None  # reason when the text is built at run time; None for a checkable query
    why: str = ""  # for a dynamic query: what could not be resolved to constants
    expanded: bool = False  # one variant of a query built from constant fragments


def _fstring_text(node: ast.JoinedStr) -> str:
    return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{…}" for v in node.values)


class _Unresolved(Exception):
    """A fragment is not a constant (parameter, attribute, call result ...)."""


MAX_VARIANTS = 64
_MAX_DEPTH = 8


def _str_value(v: object) -> str | None:
    return v if isinstance(v, str) else str(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


class _Resolver:
    """Expands an expression built only from constants into the list of strings it can take. Pure, executes nothing.

    Understood: string constants, ``+``, f-strings, conditional expressions (both branches), names bound to such
    expressions (module level or in the enclosing function), loop variables over a constant list/tuple (also a
    module-level one; tuple targets stay correlated), ``sep.join(seq)`` and ``text.split()`` of constant sequences,
    comprehensions over a constant sequence (the ``if`` filter is ignored: a superset). Anything else raises
    ``_Unresolved`` with the offending expression."""

    def __init__(self, tree: ast.Module, parent: dict[ast.AST, ast.AST]):
        self.parent = parent
        self.module: dict[str, ast.expr] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                self.module[node.targets[0].id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                self.module[node.target.id] = node.value

    def _func(self, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        while node in self.parent:
            node = self.parent[node]
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return node
        return None

    def _loops(self, node: ast.AST) -> list[tuple[ast.expr, ast.expr]]:
        """(target, iterable) of every for-loop / comprehension whose body holds ``node``."""
        out: list[tuple[ast.expr, ast.expr]] = []
        child = node
        while child in self.parent:
            up = self.parent[child]
            if isinstance(up, ast.For | ast.AsyncFor) and child is not up.iter and child is not up.target:
                out.append((up.target, up.iter))
            elif isinstance(up, ast.ListComp | ast.SetComp | ast.GeneratorExp) and not isinstance(child, ast.comprehension):
                out += [(g.target, g.iter) for g in up.generators]
            child = up
        return out

    def _lookup(self, name: str, scope: ast.FunctionDef | ast.AsyncFunctionDef | None) -> list[ast.expr]:
        if scope is not None:
            a = scope.args
            params = {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]} | {x.arg for x in (a.vararg, a.kwarg) if x}
            if name in params:
                raise _Unresolved(f"parameter {name}")
            found = []
            for n in ast.walk(scope):
                if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
                    raise _Unresolved(f"{name} is extended in place")
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
                    found.append(n.value)
            if found:
                return found
        if name in self.module:
            return [self.module[name]]
        raise _Unresolved(f"name {name}")

    def strings(
        self, node: ast.expr, binding: dict[str, str | None], scope: ast.FunctionDef | ast.AsyncFunctionDef | None, depth: int = 0
    ) -> list[str]:
        if depth > _MAX_DEPTH:
            raise _Unresolved("too deep")
        if isinstance(node, ast.Constant):
            v = _str_value(node.value)
            if v is None:
                raise _Unresolved(ast.unparse(node))
            return [v]
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.FormattedValue):
                    if v.conversion != -1 or v.format_spec is not None:
                        raise _Unresolved(ast.unparse(v))
                    v = v.value  # noqa: PLW2901
                parts.append(self.strings(v, binding, scope, depth + 1))
            return self._product(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._product([self.strings(node.left, binding, scope, depth + 1), self.strings(node.right, binding, scope, depth + 1)])
        if isinstance(node, ast.IfExp):
            return self.strings(node.body, binding, scope, depth + 1) + self.strings(node.orelse, binding, scope, depth + 1)
        if isinstance(node, ast.Name):
            if node.id in binding:
                v = binding[node.id]
                if v is None:
                    raise _Unresolved(f"loop value {node.id}")
                return [v]
            return [s for value in self._lookup(node.id, scope) for s in self.strings(value, binding, scope, depth + 1)]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and len(node.args) == 1
            and not node.keywords
        ):
            seps = self.strings(node.func.value, binding, scope, depth + 1)
            items = self.sequence(node.args[0], binding, scope, depth + 1)
            return [sep.join(items) for sep in seps]
        raise _Unresolved(ast.unparse(node))

    @staticmethod
    def _product(parts: list[list[str]]) -> list[str]:
        out = [""]
        for alternatives in parts:
            out = [a + b for a in out for b in alternatives]
            if len(out) > MAX_VARIANTS:
                raise _Unresolved("too many variants")
        return out

    def sequence(
        self, node: ast.expr, binding: dict[str, str | None], scope: ast.FunctionDef | ast.AsyncFunctionDef | None, depth: int = 0
    ) -> list[str]:
        """The strings of a constant list/tuple (or the words of ``"a b c".split()``), single-valued elements."""
        if depth > _MAX_DEPTH:
            raise _Unresolved("too deep")
        if isinstance(node, ast.List | ast.Tuple | ast.Set):
            out = []
            for e in node.elts:
                alts = self.strings(e, binding, scope, depth + 1)
                if len(alts) != 1:
                    raise _Unresolved("element with several values")
                out.append(alts[0])
            return out
        if isinstance(node, ast.Name):
            vals = self._lookup(node.id, scope)
            if len(vals) != 1:
                raise _Unresolved(f"{node.id} is assigned several times")
            return self.sequence(vals[0], binding, scope, depth + 1)
        if isinstance(node, ast.ListComp | ast.GeneratorExp) and len(node.generators) == 1:
            gen = node.generators[0]
            if isinstance(gen.target, ast.Name) and isinstance(node.elt, ast.Name) and node.elt.id == gen.target.id:
                return self.sequence(gen.iter, binding, scope, depth + 1)  # the `if` filter is ignored: a superset
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "split" and not node.args:
            texts = self.strings(node.func.value, binding, scope, depth + 1)
            if len(texts) == 1:
                return texts[0].split()
        raise _Unresolved(ast.unparse(node))

    def rows(self, target: ast.expr, it: ast.expr, scope: ast.FunctionDef | ast.AsyncFunctionDef | None) -> list[dict[str, str | None]]:
        """The bindings a loop gives its target names over a constant iterable (tuple targets stay correlated)."""
        seq = it
        if isinstance(seq, ast.Name):
            vals = self._lookup(seq.id, scope)
            if len(vals) != 1:
                raise _Unresolved(f"{seq.id} is assigned several times")
            seq = vals[0]
        if not isinstance(seq, ast.List | ast.Tuple):
            raise _Unresolved(f"loop over {ast.unparse(it)}")
        if isinstance(target, ast.Name):
            names = [target.id]
        elif isinstance(target, ast.Tuple) and all(isinstance(e, ast.Name) for e in target.elts):
            names = [e.id for e in target.elts if isinstance(e, ast.Name)]
        else:
            raise _Unresolved("loop target")
        out = []
        for elt in seq.elts:
            items = elt.elts if isinstance(elt, ast.Tuple) and isinstance(target, ast.Tuple) else [elt]
            if len(items) != len(names):
                raise _Unresolved("loop element shape")
            out.append({n: _str_value(i.value) if isinstance(i, ast.Constant) else None for n, i in zip(names, items, strict=True)})
        return out

    def variants(self, top: ast.expr) -> list[str]:
        """Every SQL text ``top`` can take given the constants around it. Raises ``_Unresolved``."""
        scope = self._func(top)
        bindings: list[dict[str, str | None]] = [{}]
        for target, it in self._loops(top):
            rows = self.rows(target, it, scope)
            bindings = [{**b, **r} for b in bindings for r in rows]
            if len(bindings) > MAX_VARIANTS * 4:
                raise _Unresolved("too many loop combinations")
        out: list[str] = []
        for b in bindings:
            for s in self.strings(top, b, scope):
                if s not in out:
                    out.append(s)
        if len(out) > MAX_VARIANTS:
            raise _Unresolved("too many variants")
        return out


def extract_queries(source: str, file: str = "<src>") -> list[Query]:
    """SELECT queries written as string literals in one Python source: static, expanded from constants, or dynamic.

    A query built from constant fragments only (f-string or ``+`` over module constants, loop variables of a constant
    list, conditional expressions) comes back as one ``Query(expanded=True)`` per variant. Raises SyntaxError. Pure."""
    tree = ast.parse(source, filename=file)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    resolver = _Resolver(tree, parent)
    out: list[Query] = []
    seen: set[int] = set()
    for node in ast.walk(tree):
        reason: str | None
        if isinstance(node, ast.JoinedStr):
            text = _fstring_text(node)
            if not looks_like_select(text):
                continue
            reason = "f-string"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
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
        else:
            continue
        if reason is None:
            out.append(Query(file, node.lineno, text))
            continue
        if reason in {"f-string", "concatenated"}:
            top: ast.AST = node
            while isinstance(parent.get(top), ast.BinOp) and isinstance(parent[top].op, ast.Add):  # type: ignore[attr-defined]
                top = parent[top]
            if id(top) in seen:
                continue
            seen.add(id(top))
            try:
                variants = resolver.variants(top)  # type: ignore[arg-type]
            except _Unresolved as e:
                out.append(Query(file, node.lineno, text, reason, str(e)))
                continue
            out += [Query(file, node.lineno, v, None, "", True) for v in variants]
            continue
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
    expanded_sites: set[tuple[str, int]] = field(default_factory=set)  # dynamic literals expanded from constants and checked
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
                    if q.expanded:
                        rep.expanded_sites.add((q.file, q.line))
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
    variants = sum(1 for f in rep.findings if f.query.expanded)
    expanded = len(rep.expanded_sites)
    static = len(rep.findings) - variants
    lines = [
        f"# sql_schema_check: {rep.tables} tables in the migrated schema, {rep.files} files, "
        f"{static + expanded + len(rep.dynamic)} SELECT literals ({static} static, {expanded} expanded from constants "
        f"into {variants} variants, {len(rep.dynamic)} dynamic)",
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
    lines += [
        f"  {q.file}:{q.line} [{q.dynamic}: {q.why}] {_one_line(q.sql, 90)}"
        if q.why
        else f"  {q.file}:{q.line} [{q.dynamic}] {_one_line(q.sql, 90)}"
        for q in rep.dynamic
    ] or ["  -"]
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
