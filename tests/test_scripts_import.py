"""Import smoke test for ``scripts/*.py`` (item 120 of IDEA-20261102-standing-improvements).

After the deduplication rounds nothing guaranteed that every script still imports: a helper removed from
``scripts/common_helpers.py`` or a renamed ``tradedesk`` function would surface only when the script ran (for a forward
report, at night). Two checks, neither runs a script's ``main``, touches the network, a database or a key:

1. Static (every script, ``ast`` only): the file parses; every ``from common_helpers import X``, ``from <sibling
   script> import X`` and ``from tradedesk... import X`` names an existing object (anywhere in the file, function-level
   imports included); every ``cmn.X`` on a module bound by ``import common_helpers as cmn`` (or a sibling script)
   names an existing top-level object. Sibling scripts are read with ``ast``, never executed; ``tradedesk`` modules are
   imported (the package has no import-time I/O).
2. Import (only scripts with an ``if __name__ == "__main__":`` guard): all of them are imported in one child process
   with sockets disabled and an environment stripped to ``PATH``/``HOME``/``PYTHONPATH``, so third-party imports and
   module-level constants are exercised too. Scripts that do their work at module level are listed in
   ``RUNS_AT_MODULE_LEVEL`` and get the static check only.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# Library modules imported by the scripts (no main guard by design); not scripts themselves.
HELPER_MODULES = {"common_helpers", "_upbit_0c_helpers"}

# Scripts that do their work at module level (no ``__main__`` guard): importing them would run the analysis, so they
# get the static check only. Reason per entry.
RUNS_AT_MODULE_LEVEL = {
    "verify_demo_trend_veto": "one-off bootstrap over cached demo trades; reads the cache and prints at import",
    "explore_us_open_calls": "one-off exploration of saved calls; loops over JSON files and prints at import",
}

# Scripts that are run with an extra package outside the project's dependencies (``uv run --with ...``): their import
# may fail only with ModuleNotFoundError for exactly that top-level module.
EXTRA_DEPS = {
    "lesson_sheets": "PIL",  # uv run --no-project --with pillow (see the script's docstring)
}

# Scripts whose import is known to be broken and is left as is, with the reason. Keep empty unless a fix would
# change a frozen forward script (condor_forward_costs, pm_gap_paper_report, paper_carry_report).
KNOWN_BROKEN: dict[str, str] = {}


def _script_paths() -> list[Path]:
    # top level only: scripts/server/, scripts/research/ and scripts/mac/ are out of scope; _*.py are helpers
    return sorted(p for p in SCRIPTS.glob("*.py") if not p.name.startswith("_") and p.stem != "common_helpers")


SCRIPT_PATHS = _script_paths()
SIBLINGS = {p.stem for p in SCRIPTS.glob("*.py")}


@cache
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _bound_names(stmts: list[ast.stmt]) -> set[str]:
    """Names bound at module level (descending into if/try/with blocks, not into functions or classes)."""
    out: set[str] = set()

    def targets(node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                targets(elt)
        elif isinstance(node, ast.Starred):
            targets(node.value)

    for s in stmts:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(s.name)
        elif isinstance(s, ast.Assign):
            for t in s.targets:
                targets(t)
        elif isinstance(s, (ast.AnnAssign, ast.AugAssign)):
            targets(s.target)
        elif isinstance(s, ast.Import):
            out.update(a.asname or a.name.split(".")[0] for a in s.names)
        elif isinstance(s, ast.ImportFrom):
            out.update(a.asname or a.name for a in s.names)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            targets(s.target)
            out |= _bound_names(s.body) | _bound_names(s.orelse)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for item in s.items:
                if item.optional_vars is not None:
                    targets(item.optional_vars)
            out |= _bound_names(s.body)
        elif isinstance(s, ast.If):
            out |= _bound_names(s.body) | _bound_names(s.orelse)
        elif isinstance(s, ast.Try):
            out |= _bound_names(s.body) | _bound_names(s.orelse) | _bound_names(s.finalbody)
            for h in s.handlers:
                out |= _bound_names(h.body)
        elif isinstance(s, ast.Expr) and isinstance(s.value, ast.NamedExpr):
            targets(s.value.target)
    return out


@cache
def _sibling_names(stem: str) -> frozenset[str] | None:
    """Top-level names of a sibling script, or None when it star-imports (names cannot be listed statically)."""
    tree = _tree(SCRIPTS / f"{stem}.py")
    if any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names) for n in ast.walk(tree)):
        return None
    return frozenset(_bound_names(tree.body))


def _tradedesk_has(module: str, name: str) -> bool:
    mod = importlib.import_module(module)
    if hasattr(mod, name):
        return True
    try:  # ``from tradedesk.analytics import options`` names a submodule
        importlib.import_module(f"{module}.{name}")
    except ModuleNotFoundError:
        return False
    return True


def _module_exists(module: str) -> bool:
    try:
        importlib.import_module(module)
    except ModuleNotFoundError:
        return False
    return True


def _has_main_guard(tree: ast.Module) -> bool:
    for s in tree.body:
        if isinstance(s, ast.If) and isinstance(s.test, ast.Compare):
            parts = [s.test.left, *s.test.comparators]
            if any(isinstance(p, ast.Name) and p.id == "__name__" for p in parts) and any(
                isinstance(p, ast.Constant) and p.value == "__main__" for p in parts
            ):
                return True
    return False


def _broken_imports(path: Path) -> list[str]:
    """Problems with the names this script imports from common_helpers, sibling scripts and tradedesk."""
    tree = _tree(path)
    problems: list[str] = []
    aliases: dict[str, str] = {}  # local name -> sibling script stem bound by ``import x [as y]``

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("tradedesk") and not _module_exists(a.name):
                    problems.append(f"line {node.lineno}: no module {a.name}")
                elif a.name in SIBLINGS:
                    aliases[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mod = node.module
            names = [a.name for a in node.names if a.name != "*"]
            if mod in SIBLINGS:
                known = _sibling_names(mod)
                if known is not None:
                    problems += [f"line {node.lineno}: {mod} has no {n}" for n in names if n not in known]
            elif mod == "tradedesk" or mod.startswith("tradedesk."):
                if not _module_exists(mod):
                    problems.append(f"line {node.lineno}: no module {mod}")
                else:
                    problems += [f"line {node.lineno}: {mod} has no {n}" for n in names if not _tradedesk_has(mod, n)]

    # cmn.X on ``import common_helpers as cmn``; a name rebound in the module or a function (``st = ...``) is skipped
    aliases = {k: v for k, v in aliases.items() if k not in _stored_names(tree.body, into_functions=False)}

    def visit(node: ast.AST, live: dict[str, str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            local = _stored_names([node], into_functions=True)
            local |= {a.arg for a in ast.walk(node.args) if isinstance(a, ast.arg)}
            live = {k: v for k, v in live.items() if k not in local}
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in live:
            stem = live[node.value.id]
            known = _sibling_names(stem)
            if known is not None and node.attr not in known and not isinstance(node.ctx, ast.Store):
                problems.append(f"line {node.lineno}: {stem} has no {node.attr} ({node.value.id}.{node.attr})")
        for child in ast.iter_child_nodes(node):
            visit(child, live)

    if aliases:
        visit(tree, aliases)
    return sorted(set(problems))


def _stored_names(nodes: list[ast.AST], into_functions: bool) -> set[str]:
    """Names assigned under ``nodes`` other than by imports. Nested functions are entered only when
    ``into_functions`` (a function's own scope; over-approximates, which means fewer false alarms)."""
    out: set[str] = set()
    roots = {id(n) for n in nodes}
    stack = list(nodes)
    while stack:
        sub = stack.pop()
        own_root = into_functions and id(sub) in roots  # the function whose scope is being collected
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            out.add(sub.id)
        if not own_root and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(sub.name)
        if not into_functions and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(sub))
    return out


def test_scripts_are_found():
    assert len(SCRIPT_PATHS) > 100
    assert "common_helpers" in SIBLINGS
    assert set(EXTRA_DEPS) | set(KNOWN_BROKEN) <= {p.stem for p in SCRIPT_PATHS}  # no stale allowlist entries


@pytest.mark.parametrize("path", SCRIPT_PATHS, ids=lambda p: p.stem)
def test_script_parses_and_its_imports_resolve(path: Path):
    if path.stem in KNOWN_BROKEN:
        pytest.skip(KNOWN_BROKEN[path.stem])
    assert _broken_imports(path) == []


def test_checker_catches_missing_names_and_ignores_shadowing(tmp_path):
    src = (
        "import common_helpers as cmn\n"
        "from common_helpers import read_json, no_such_helper\n"
        "from tradedesk.core.http import Http, NoSuchClass\n"
        "from tradedesk.no_such_module import x\n"
        "def f(cmn):\n"
        "    return cmn.anything\n"  # a parameter shadows the module alias: not checked
        "def g():\n"
        "    return cmn.ymd_day(0), cmn.gone(1)\n"
        "def h():\n"
        "    cmn = {}\n"  # a local rebinding shadows it in h only (g is still checked)
        "    return cmn.get\n"
        "if __name__ == '__main__':\n"
        "    g()\n"
    )
    p = tmp_path / "probe.py"
    p.write_text(src)
    assert _broken_imports(p) == [
        "line 2: common_helpers has no no_such_helper",
        "line 3: tradedesk.core.http has no NoSuchClass",
        "line 4: no module tradedesk.no_such_module",
        "line 8: common_helpers has no gone (cmn.gone)",
    ]


def test_unguarded_scripts_are_listed():
    """A script without a ``__main__`` guard would run on import: it must be listed (and is not imported)."""
    unguarded = {p.stem for p in SCRIPT_PATHS if not _has_main_guard(_tree(p))}
    assert unguarded == set(RUNS_AT_MODULE_LEVEL)
    assert not set(RUNS_AT_MODULE_LEVEL) & set(KNOWN_BROKEN)


_CHILD = r"""
import importlib, json, socket, sys, traceback

def _no_net(*a, **k):
    raise OSError("network disabled in the import smoke test")

socket.socket.connect = _no_net
socket.socket.connect_ex = _no_net
socket.create_connection = _no_net
socket.getaddrinfo = _no_net

failed = {}
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except BaseException as e:  # SystemExit included: a guarded script must not exit on import
        missing = e.name if isinstance(e, ModuleNotFoundError) else None
        failed[name] = [missing, f"{type(e).__name__}: {e}"]
print(json.dumps(failed))
"""


def test_guarded_scripts_import_without_side_effects(tmp_path):
    names = [p.stem for p in SCRIPT_PATHS if p.stem not in RUNS_AT_MODULE_LEVEL and p.stem not in KNOWN_BROKEN]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),  # nothing from the real home (no ~/.env, no tokens)
        "PYTHONPATH": os.pathsep.join([str(SCRIPTS), str(ROOT / "src")]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    res = subprocess.run(
        [sys.executable, "-c", _CHILD, *names],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert res.returncode == 0, res.stderr[-4000:]
    failed = json.loads(res.stdout.strip().splitlines()[-1])
    broken = {k: msg for k, (missing, msg) in failed.items() if not (missing and missing == EXTRA_DEPS.get(k))}
    assert broken == {}
