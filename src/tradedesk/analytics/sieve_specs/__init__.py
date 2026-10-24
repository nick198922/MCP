"""Strategy specs for the sieve (``analytics.strategy_sieve``, ``scripts/sieve.py``).

One module per strategy, rewritten by hand from the text of its source (code from the internet is never run). A spec
module defines:

- ``NAME``: the registry key, equal to the module name (one hidden-period reveal per NAME, whatever the parameters; a
  variant of a strategy keeps its NAME and changes ``PARAMS``);
- ``SOURCE``: where the rule comes from (URL or repository document);
- ``TIMEFRAMES``: bar sizes in hours the signals are computed on (1h bars are resampled, aligned to 00:00 UTC);
- ``MANAGEMENT``: ``"strategy"`` (own stop / target / time exit, nothing moved) or ``"B"`` (break-even at half way);
- ``PARAMS``: default parameters; every override is a new registry row;
- ``signals(bars, **params) -> list[Signal]``: a pure function. A signal decided at bar i uses bars <= i only, is
  stamped with that bar's close (``bars.close_time(i)``) and enters at that bar's close price; the sieve refuses a spec
  that fails the causality check.

Specs are written by agents from untrusted text, so ``load`` parses the module before importing it and refuses
anything outside a small allow-list (``check_source``): imports only from ``__future__``, ``math``, ``numpy`` and
``tradedesk.analytics``; no ``open`` / ``eval`` / ``exec`` / ``getattr`` / ``globals`` / ``__import__``, no attribute
starting with ``_``, no frame or code introspection, no numpy file I/O. The check is static and conservative, not a
sandbox: the critic still reads every spec diff.
"""

from __future__ import annotations

import ast
import hashlib
import re
from importlib import import_module, util
from types import ModuleType

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
REQUIRED = ("NAME", "SOURCE", "TIMEFRAMES", "MANAGEMENT", "PARAMS", "signals")
ALLOWED_MODULES = ("__future__", "math", "numpy")
ALLOWED_PREFIX = "tradedesk.analytics."
BANNED_NAMES = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "globals",
        "locals",
        "vars",
        "dir",
        "breakpoint",
        "input",
        "help",
        "exit",
        "quit",
        "memoryview",
        "print",
    }
)
# attributes that reach files, processes, modules or interpreter internals (numpy and module re-exports included)
BANNED_ATTRS = frozenset(
    {
        "open",
        "io",
        "os",
        "sys",
        "subprocess",
        "shutil",
        "pathlib",
        "Path",
        "socket",
        "importlib",
        "import_module",
        "util",
        "builtins",
        "csv",
        "json",
        "pickle",
        "ctypes",
        "eval",
        "exec",
        "compile",
        "system",
        "popen",
        "load",
        "loads",
        "save",
        "savez",
        "savez_compressed",
        "savetxt",
        "loadtxt",
        "genfromtxt",
        "fromfile",
        "tofile",
        "fromregex",
        "memmap",
        "ctypeslib",
        "DataSource",
        "f2py",
        "distutils",
        "lib",
        "testing",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "get_source",
        "find_spec",
        "modules",
        "dump",
        "dumps",
    }
)
BANNED_ATTR_PREFIXES = ("_", "gi_", "cr_", "ag_", "f_", "tb_", "co_", "func_")


def _module_ok(mod: str) -> bool:
    return mod in ALLOWED_MODULES or mod.startswith(ALLOWED_PREFIX)


def _attr_ok(name: str) -> bool:
    return name not in BANNED_ATTRS and not name.startswith(BANNED_ATTR_PREFIXES)


def check_source(src: str) -> list[str]:
    """Problems of a spec module source against the allow-list (empty = accepted). Imports of names from
    ``tradedesk.analytics`` modules are resolved (our own pure modules): a name that is a module outside the package
    (a re-exported ``io``, ``os`` ...) is refused."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    bad: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for a in node.names:
                if not _module_ok(a.name):
                    bad.append(f"line {line}: import {a.name} is not allowed")
                elif "." in a.name and not a.asname:
                    bad.append(f"line {line}: import {a.name} binds the top package; use 'import {a.name} as x'")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level or not _module_ok(mod):
                bad.append(f"line {line}: from {'.' * node.level}{mod} import is not allowed")
                continue
            for a in node.names:
                if a.name == "*" or not _attr_ok(a.name):
                    bad.append(f"line {line}: from {mod} import {a.name} is not allowed")
                elif mod.startswith(ALLOWED_PREFIX):
                    obj = getattr(import_module(mod), a.name, None)
                    if isinstance(obj, ModuleType) and not obj.__name__.startswith(ALLOWED_PREFIX):
                        bad.append(f"line {line}: from {mod} import {a.name} imports the module {obj.__name__}")
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES or node.id.startswith("__"):
                bad.append(f"line {line}: name {node.id} is not allowed")
        elif isinstance(node, ast.Attribute):
            if not _attr_ok(node.attr):
                bad.append(f"line {line}: attribute .{node.attr} is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("__") and node.value.endswith("__") and len(node.value) > 4:
                bad.append(f"line {line}: string {node.value!r} is not allowed")
        elif isinstance(node, ast.Global | ast.Nonlocal):
            bad.append(f"line {line}: global / nonlocal is not allowed")
    return bad


def source_hash(src: str) -> str:
    """Content hash of a spec (12 hex): the AST without docstrings and without the ``NAME`` / ``SOURCE`` assignments,
    so a renamed or re-documented copy hashes the same."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and body:
            if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    tree.body = [
        n
        for n in tree.body
        if not (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ("NAME", "SOURCE") for t in n.targets))
    ]
    return hashlib.sha256(ast.dump(tree, annotate_fields=False).encode()).hexdigest()[:12]


def load(name: str) -> ModuleType:
    """Import a spec from this package only (no paths, no foreign code) after the static allow-list check; raises
    ValueError on a bad spec, ImportError when it does not exist. The module gets ``SIEVE_HASH`` (``source_hash``)."""
    if not _NAME.match(name):
        raise ValueError(f"spec name must match {_NAME.pattern}: {name!r}")
    full = f"{__name__}.{name}"
    found = util.find_spec(full)
    if found is None or found.loader is None or not hasattr(found.loader, "get_source"):
        raise ImportError(f"no spec module {full}")
    src = found.loader.get_source(full)
    if src is None:
        raise ImportError(f"spec {full} has no source")
    problems = check_source(src)
    if problems:
        raise ValueError(f"spec {name} refused by the allow-list: " + "; ".join(problems[:10]))
    mod = import_module(full)
    missing = [k for k in REQUIRED if not hasattr(mod, k)]
    if missing:
        raise ValueError(f"spec {name} lacks {missing}")
    if mod.NAME != name:
        raise ValueError(f"spec {name}: NAME {mod.NAME!r} must equal the module name")
    mod.SIEVE_HASH = source_hash(src)
    return mod
