"""Code in src/tradedesk, scripts/ and tests/ runs on the oldest Python that pyproject.toml declares.

Item 158 of docs/ideas/IDEA-20261102-standing-improvements.md. The server or a laptop may run a newer
Python than `requires-python`, and then 3.12+ syntax or stdlib names work there and break a clean
install on 3.11. Two stdlib-only checks:

* syntax: `ast.parse(..., feature_version=MIN)` rejects `type X = ...` and `def f[T]`; it does NOT
  reject PEP 701 f-strings (3.12: reused quotes, backslashes, comments, line breaks inside `{...}`),
  so on 3.12+ a tokenizer pass looks for those. On 3.11 the parser itself is the check.
* names: stdlib attributes, modules and keyword arguments added after MIN, found with the AST
  (import aliases are resolved; a few distinctive pathlib methods are matched by name only).
  `test_feature_tables_match_this_python` checks every table entry against the running interpreter,
  so CI on 3.11, 3.12 and 3.13 verifies the "added in" versions instead of trusting this file.
"""

import ast
import importlib
import inspect
import io
import re
import sys
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
FILES = (
    sorted((ROOT / "src" / "tradedesk").rglob("*.py"))
    + sorted((ROOT / "scripts").rglob("*.py"))
    # tests/ too (item 178): a 3.12+ test fails to import on a clean 3.11 and hides real failures.
    # tests/fixtures/ holds recorded data, not code we run.
    + sorted(p for p in TESTS.rglob("*.py") if not p.is_relative_to(TESTS / "fixtures"))
)


def _min_python() -> tuple[int, int]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^requires-python\s*=\s*">=\s*(\d+)\.(\d+)"', text, re.MULTILINE)
    assert m, "requires-python must be a plain '>=X.Y' bound"
    return int(m.group(1)), int(m.group(2))


MIN = _min_python()

# Stdlib names added after 3.11: dotted name -> first version that has it.
NEWER_NAMES: dict[str, tuple[int, int]] = {
    "itertools.batched": (3, 12),
    "math.sumprod": (3, 12),
    "os.path.splitroot": (3, 12),
    "os.path.isjunction": (3, 12),
    "pathlib.Path.walk": (3, 12),
    "pathlib.Path.is_junction": (3, 12),
    "pathlib.Path.with_segments": (3, 12),
    "pathlib.PurePath.with_segments": (3, 12),
    "typing.override": (3, 12),
    "typing.TypeAliasType": (3, 12),
    "types.get_original_bases": (3, 12),
    "collections.abc.Buffer": (3, 12),
    "calendar.Month": (3, 12),
    "calendar.Day": (3, 12),
    "logging.getHandlerByName": (3, 12),
    "logging.getHandlerNames": (3, 12),
    "sys.monitoring": (3, 12),
    "inspect.markcoroutinefunction": (3, 12),
    "asyncio.eager_task_factory": (3, 12),
    "asyncio.create_eager_task_factory": (3, 12),
    "random.binomialvariate": (3, 12),
    "sqlite3.LEGACY_TRANSACTION_CONTROL": (3, 12),
    "copy.replace": (3, 13),
    "warnings.deprecated": (3, 13),
    "typing.ReadOnly": (3, 13),
    "typing.TypeIs": (3, 13),
    "typing.NoDefault": (3, 13),
    "typing.get_protocol_members": (3, 13),
    "typing.is_protocol": (3, 13),
    "math.fma": (3, 13),
    "statistics.kde": (3, 13),
    "statistics.kde_random": (3, 13),
    "base64.z85encode": (3, 13),
    "base64.z85decode": (3, 13),
    "os.process_cpu_count": (3, 13),
    "queue.ShutDown": (3, 13),
    "queue.Queue.shutdown": (3, 13),
    "asyncio.QueueShutDown": (3, 13),
    "asyncio.Queue.shutdown": (3, 13),
    "pathlib.Path.from_uri": (3, 13),
    "pathlib.Path.full_match": (3, 13),
    "pathlib.PurePath.full_match": (3, 13),
    "re.PatternError": (3, 13),
    "glob.translate": (3, 13),
    "enum.EnumDict": (3, 13),
    "dbm.sqlite3": (3, 13),
    "builtins.PythonFinalizationError": (3, 13),
}

# Method names distinctive enough to flag on any receiver the AST cannot resolve (`p.walk()`).
# `os.walk` / `ast.walk` resolve to a module and are not flagged.
METHOD_NAMES: dict[str, str] = {
    "walk": "pathlib.Path.walk",
    "is_junction": "pathlib.Path.is_junction",
    "with_segments": "pathlib.Path.with_segments",
    "full_match": "pathlib.Path.full_match",
}

# Keyword arguments added after 3.11: (callable, keyword, since, witness). A callable starting with "."
# is a method matched by name on an unresolved receiver. The witness proves the version on this
# interpreter: "sig:<dotted>" -> the keyword is in its signature; "attr:<dotted>" -> the attribute exists
# (sqlite3.connect has no introspectable signature).
NEWER_KWARGS: list[tuple[str, str, tuple[int, int], str]] = [
    ("shutil.rmtree", "onexc", (3, 12), "sig:shutil.rmtree"),
    ("tempfile.NamedTemporaryFile", "delete_on_close", (3, 12), "sig:tempfile.NamedTemporaryFile"),
    ("math.nextafter", "steps", (3, 12), "sig:math.nextafter"),
    ("sqlite3.connect", "autocommit", (3, 12), "attr:sqlite3.Connection.autocommit"),
    (".relative_to", "walk_up", (3, 12), "sig:pathlib.PurePath.relative_to"),
    (".glob", "case_sensitive", (3, 12), "sig:pathlib.Path.glob"),
    (".rglob", "case_sensitive", (3, 12), "sig:pathlib.Path.rglob"),
    ("itertools.batched", "strict", (3, 13), "sig:itertools.batched"),
]

# (path relative to the repository, finding key) -> why it stays. Empty: the 06.11 scans (src/ + scripts/,
# item 158; tests/, item 178) found nothing.
ALLOWED: dict[tuple[str, str], str] = {}

_MISSING = object()


def _resolve_runtime(dotted: str):
    """Object at `dotted` on this interpreter, or _MISSING."""
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for name in parts[i:]:
            obj = getattr(obj, name, _MISSING)
            if obj is _MISSING:
                return _MISSING
        return obj
    return _MISSING


# ---------------------------------------------------------------- syntax


def _quote(token: str) -> str:
    return token.lstrip("rRfFbBuU")


def _clashes(inner: str, outer: str) -> bool:
    """3.11 ends the outer f-string at its own quote, wherever it appears in a replacement field."""
    return inner[0] == outer if len(outer) == 1 else inner.startswith(outer)


def fstring_issues(source: str) -> list[tuple[int, str]]:
    """PEP 701 f-string forms that 3.11 rejects. Needs the 3.12+ tokenizer; returns [] before 3.12."""
    if sys.version_info < (3, 12):
        return []  # the 3.11 parser itself rejects these forms
    issues: list[tuple[int, str]] = []
    stack: list[tuple[str, int]] = []  # (quote, start line) of the enclosing f-strings
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.FSTRING_END:
            stack.pop()
            continue
        if stack:
            line = tok.start[0]
            if tok.type == tokenize.FSTRING_MIDDLE:
                if len(stack) > 1 and "\\" in tok.string:
                    issues.append((line, "syntax:f-string backslash in replacement field"))
                continue
            if any(len(q) == 1 and line != start for q, start in stack):
                issues.append((line, "syntax:f-string replacement field spans lines"))
            if tok.type in (tokenize.STRING, tokenize.FSTRING_START):
                q = _quote(tok.string)
                if any(_clashes(q, outer) for outer, _ in stack):
                    issues.append((line, "syntax:f-string reuses its quote in a replacement field"))
                if tok.type == tokenize.STRING and "\\" in tok.string:
                    issues.append((line, "syntax:f-string backslash in replacement field"))
            elif tok.type == tokenize.COMMENT:
                issues.append((line, "syntax:f-string comment in replacement field"))
        if tok.type == tokenize.FSTRING_START:
            stack.append((_quote(tok.string), tok.start[0]))
    return sorted(set(issues))


# An f-string token starts with an f / rf / fr prefix right after a word boundary (any case).
_FSTRING_PREFIX = re.compile(r"\b(?:f|rf|fr)['\"]", re.IGNORECASE)


def syntax_issues(source: str, version: tuple[int, int] = MIN) -> list[tuple[int, str]]:
    try:
        ast.parse(source, feature_version=version)
    except SyntaxError as e:
        return [(e.lineno or 0, f"syntax:{e.msg}")]
    return fstring_issues(source) if _FSTRING_PREFIX.search(source) else []


# ---------------------------------------------------------------- names


def _aliases(tree: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    out[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    return None


def name_issues(tree: ast.AST) -> list[tuple[int, str]]:
    """Uses of stdlib names / keyword arguments newer than MIN."""
    newer = {k for k, v in NEWER_NAMES.items() if v > MIN}
    kwargs = [(c, kw) for c, kw, v, _ in NEWER_KWARGS if v > MIN]
    aliases = _aliases(tree)
    issues: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            issues += [(node.lineno, f"name:{a.name}") for a in node.names if a.name in newer]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            issues += [(node.lineno, f"name:{node.module}.{a.name}") for a in node.names if f"{node.module}.{a.name}" in newer]
        elif isinstance(node, ast.Name) and node.id not in aliases and f"builtins.{node.id}" in newer:
            issues.append((node.lineno, f"name:builtins.{node.id}"))
        elif isinstance(node, ast.Attribute):
            full = _dotted(node, aliases)
            if full in newer:
                issues.append((node.lineno, f"name:{full}"))
            elif full is None and _dotted(node.value, aliases) is None and METHOD_NAMES.get(node.attr) in newer:
                issues.append((node.lineno, f"name:{METHOD_NAMES[node.attr]}"))
        elif isinstance(node, ast.Call) and node.keywords:
            func = _dotted(node.func, aliases)
            if func is None and isinstance(node.func, ast.Attribute) and _dotted(node.func.value, aliases) is None:
                func = "." + node.func.attr
            used = {k.arg for k in node.keywords}
            issues += [(node.lineno, f"kwarg:{c}({kw}=)") for c, kw in kwargs if c == func and kw in used]
    return sorted(set(issues))


def _required_words() -> list[set[str]]:
    """Per table entry, words that must all occur in a source for name_issues to flag it there.

    A resolved name needs its module's top-level name (in the import) and its last part; a method
    matched by name needs that name; a keyword argument needs the keyword (and the module, if resolved).
    Lets the scan skip the AST walk for most files without changing what it finds.
    """
    out: list[set[str]] = []
    for dotted, since in NEWER_NAMES.items():
        if since > MIN:
            parts = dotted.split(".")
            out.append({parts[-1]} if parts[0] == "builtins" else {parts[0], parts[-1]})
    out += [{name} for name, dotted in METHOD_NAMES.items() if NEWER_NAMES[dotted] > MIN]
    for call, kw, since, _ in NEWER_KWARGS:
        if since > MIN:
            out.append({kw} if call.startswith(".") else {kw, call.split(".")[0]})
    return out


_REQUIRED_WORDS = _required_words()


def file_issues(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    issues = syntax_issues(source)
    if issues:
        return issues
    words = set(re.findall(r"\w+", source))
    if not any(need <= words for need in _REQUIRED_WORDS):
        return []
    return name_issues(ast.parse(source))


# ---------------------------------------------------------------- tests


def test_min_python_matches_ruff_target():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'target-version = "py{MIN[0]}{MIN[1]}"' in text
    assert MIN == (3, 11)  # the tables below list what came after 3.11; extend them if this moves


@pytest.mark.parametrize("dotted", sorted(NEWER_NAMES))
def test_feature_tables_match_this_python(dotted):
    present = _resolve_runtime(dotted) is not _MISSING
    assert present == (sys.version_info >= NEWER_NAMES[dotted]), f"{dotted}: added in {NEWER_NAMES[dotted]}?"


@pytest.mark.parametrize(("call", "kw", "since", "witness"), NEWER_KWARGS)
def test_kwarg_table_matches_this_python(call, kw, since, witness):
    how, dotted = witness.split(":", 1)
    obj = _resolve_runtime(dotted)
    if how == "attr":
        present = obj is not _MISSING
    else:
        present = obj is not _MISSING and kw in inspect.signature(obj).parameters
    assert present == (sys.version_info >= since), f"{call}({kw}=): added in {since}?"


def test_method_names_are_in_the_name_table():
    assert set(METHOD_NAMES.values()) <= set(NEWER_NAMES)


NEWER_SYNTAX = {
    "type_statement": "type Pair = tuple[int, int]\n",
    "generic_function": "def first[T](xs: list[T]) -> T:\n    return xs[0]\n",
    "generic_class": "class Box[T]:\n    pass\n",
    "same_quote": 'name = "x"\ns = f"{", ".join([name])}"\n',
    "same_quote_single": "s = f'{d['k']}'\n",
    "backslash": "xs = []\ns = f\"{'\\n'.join(xs)}\"\n",
    "nested_backslash": "x = 1\ns = f\"{f'{x}\\t'}\"\n",
    "comment": 'x = 1\ns = f"{x  # why\n}"\n',
    "multiline_field": 's = f"{1 +\n 2}"\n',
    "nested_same_triple": 'x = 1\ns = f"""{f"""{x}"""}"""\n',
}

VALID_311 = {
    "plain": 'x = 1\ns = f"{x!r:>{x}} {{literal}} \\n"\n',
    "other_quote": "d = {}\ns = f\"{d['k']}\"\n",
    "nested_other_quote": "x = 1\ns = f\"{f'{x}'}\"\n",
    "triple_outer": 'x = 1\ns = f"""{"a"} {x}\n"""\n',
    "triple_multiline_field": 'x = 1\ns = f"""{\nx\n}"""\n',
    "raw_and_bytes": 'x = 1\ns = rf"\\d{x}" + str(rb"\\d")\n',
    "except_star": "try:\n    pass\nexcept* ValueError:\n    pass\n",
    "implicit_concat": 'x = 1\ns = (f"{x}"\n     "tail")\n',
}


@pytest.mark.parametrize("name", sorted(NEWER_SYNTAX))
def test_newer_syntax_is_caught(name):
    assert syntax_issues(NEWER_SYNTAX[name]), name


@pytest.mark.parametrize("name", sorted(VALID_311))
def test_311_syntax_passes(name):
    assert syntax_issues(VALID_311[name]) == [], name


NAMES_SOURCE = """
import itertools, os, ast, shutil
import pathlib as pl
from typing import override
from copy import replace as _replace
from pathlib import Path
import dbm.sqlite3

for chunk in itertools.batched(range(4), 2):
    pass
for _ in os.walk("."):
    pass
ast.walk(ast.parse(""))
pl.Path(".").walk()
Path.from_uri("file:///tmp")
pathlib_path = Path(".")
pathlib_path.full_match("*.py")
shutil.rmtree("x", onexc=print)
pathlib_path.relative_to("/", walk_up=True)
shutil.rmtree("x", ignore_errors=True)
raise PythonFinalizationError
"""


def test_name_detector_on_fixture():
    found = name_issues(ast.parse(NAMES_SOURCE))
    assert found == [
        (4, "name:typing.override"),
        (5, "name:copy.replace"),
        (7, "name:dbm.sqlite3"),
        (9, "name:itertools.batched"),
        (14, "name:pathlib.Path.walk"),
        (15, "name:pathlib.Path.from_uri"),
        (17, "name:pathlib.Path.full_match"),
        (18, "kwarg:shutil.rmtree(onexc=)"),
        (19, "kwarg:.relative_to(walk_up=)"),
        (21, "name:builtins.PythonFinalizationError"),
    ]


def test_scope_is_the_whole_tree():
    assert len(FILES) > 500
    assert ROOT / "src" / "tradedesk" / "server.py" in FILES
    assert ROOT / "scripts" / "research" / "vault_sizing.py" in FILES
    assert Path(__file__).resolve() in FILES
    assert ROOT / "tests" / "conftest.py" in FILES
    assert not [p for p in FILES if p.is_relative_to(TESTS / "fixtures")]


def test_word_prefilter_keeps_every_fixture_hit(tmp_path):
    """file_issues (with the word prefilter) finds what name_issues finds on the full AST."""
    path = tmp_path / "m.py"
    path.write_text(NAMES_SOURCE, encoding="utf-8")
    assert file_issues(path) == name_issues(ast.parse(NAMES_SOURCE))
    tree = ast.parse(NAMES_SOURCE)
    imports = "\n".join(ast.get_source_segment(NAMES_SOURCE, s) for s in tree.body if isinstance(s, ast.Import | ast.ImportFrom))
    for stmt in tree.body:  # each statement alone after the imports, and alone without them
        body = ast.get_source_segment(NAMES_SOURCE, stmt)
        for source in (f"{imports}\n{body}\n", f"{body}\n"):
            path.write_text(source, encoding="utf-8")
            assert file_issues(path) == name_issues(ast.parse(source)), source


# One test per file so xdist spreads the parsing (~10 s of CPU in total).
@pytest.mark.parametrize("path", FILES, ids=[p.relative_to(ROOT).as_posix() for p in FILES])
def test_file_runs_on_min_python(path):
    rel = path.relative_to(ROOT).as_posix()
    bad = [f"{rel}:{line}: {key}" for line, key in file_issues(path) if (rel, key) not in ALLOWED]
    assert not bad, f"code newer than Python {MIN[0]}.{MIN[1]} (fix it or add to ALLOWED with a reason): {bad}"


def test_allowlist_is_not_stale():
    for (rel, key), reason in ALLOWED.items():
        assert reason.strip(), rel
        assert key in {k for _, k in file_issues(ROOT / rel)}, f"{rel}: {key} no longer found, drop it from ALLOWED"
