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
anything outside a small allow-list (``check_source``):

- imports only from ``__future__``, ``math``, ``numpy`` (``numpy.random``, ``numpy.linalg``) and
  ``tradedesk.analytics.*`` (not other specs);
- every imported name and every attribute chain rooted at one (``np.random.default_rng``) is resolved at check time
  (``import_module`` + ``getattr``); each link must be one of the modules ``numpy``, ``numpy.random``,
  ``numpy.linalg``, ``math``, ``tradedesk.analytics.*``, a function / class / value defined in them, or a plain
  number / string / container. So ``np.ma.core.operator.attrgetter`` or a stdlib name re-exported by one of our
  modules is refused;
- a module is only the root of such a chain: never assigned, passed or returned (no aliasing past the check), and an
  imported name is never rebound;
- no ``open`` / ``eval`` / ``exec`` / ``getattr`` / ``globals`` / ``__import__`` / ``type`` / ``format``, no attribute
  starting with ``_``, no ``.format`` (string-built attribute access), no string containing ``__``, no frame or code
  introspection, no numpy file I/O.

The check is static and conservative, not a sandbox: attributes of runtime values (``bars.c``) are only checked by
name, and the critic still reads every spec diff.
"""

from __future__ import annotations

import ast
import hashlib
import re
from importlib import import_module, util
from types import ModuleType

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
REQUIRED = ("NAME", "SOURCE", "TIMEFRAMES", "MANAGEMENT", "PARAMS", "signals")
ALLOWED_MODULES = ("__future__", "math", "numpy", "numpy.random", "numpy.linalg")
ALLOWED_PREFIX = "tradedesk.analytics."
# modules a spec may reach at run time (as import targets or along attribute chains)
RUNTIME_MODULES = frozenset({"math", "numpy", "numpy.random", "numpy.linalg"})
SPECS_PKG = __name__  # a spec never imports another spec (it would run before its own check)
# values a link may be without an owning module (contents of containers are not inspected)
PLAIN_TYPES = frozenset({int, float, complex, bool, str, bytes, type(None), tuple, frozenset, list, dict, set})
BANNED_NAMES = frozenset(
    {
        "open",
        "type",
        "object",
        "super",
        "format",
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
        # attribute access built from strings, and the stdlib helpers that do it
        "format",
        "format_map",
        "attrgetter",
        "methodcaller",
        "getattr_static",
        "get_annotations",
        "test",  # numpy's pytest runner
    }
)
BANNED_ATTR_PREFIXES = ("_", "gi_", "cr_", "ag_", "f_", "tb_", "co_", "func_")


def _under(mod: str, pkg: str) -> bool:
    return mod == pkg or mod.startswith(pkg + ".")


def _ours(mod: str) -> bool:
    """A ``tradedesk.analytics`` module other than the spec package and the specs."""
    return mod.startswith(ALLOWED_PREFIX) and not _under(mod, SPECS_PKG)


def _module_ok(mod: str) -> bool:
    """An import statement's target."""
    return mod in ALLOWED_MODULES or _ours(mod)


def _owner_ok(mod: str) -> bool:
    """The module a function, class or value reached by a spec is defined in: an allowed module or a private
    implementation module of one (``numpy._core.fromnumeric``, ``numpy.random._generator``)."""
    return mod in RUNTIME_MODULES or _ours(mod) or any(mod.startswith(m + "._") for m in RUNTIME_MODULES)


def _attr_ok(name: str) -> bool:
    return name not in BANNED_ATTRS and not name.startswith(BANNED_ATTR_PREFIXES)


def _owner(obj: object, depth: int = 0) -> str:
    """Name of the module that defines ``obj`` (method descriptors and bound builtins: their class or receiver)."""
    m = getattr(obj, "__module__", None)
    if isinstance(m, str):
        return m
    if depth < 3:
        for a in ("__objclass__", "__self__"):
            o = getattr(obj, a, None)
            if o is not None and o is not obj:
                return o.__name__ if isinstance(o, ModuleType) else _owner(o, depth + 1)
    return type(obj).__module__


def _link_problem(obj: object) -> str | None:
    """Why a value reached statically by a spec is refused (None = allowed)."""
    if isinstance(obj, ModuleType):
        return None if obj.__name__ in RUNTIME_MODULES or _ours(obj.__name__) else f"the module {obj.__name__}"
    if type(obj) in PLAIN_TYPES:
        return None
    owner = _owner(obj)
    return None if _owner_ok(owner) else f"a {type(obj).__name__} from {owner}"


def _chain(node: ast.Attribute) -> tuple[ast.expr, list[str]]:
    """``a.b.c`` -> (Name a, ["b", "c"]); the root is whatever expression the chain hangs from."""
    attrs: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        attrs.append(cur.attr)
        cur = cur.value
    return cur, attrs[::-1]


_MISSING = object()


def _imports(tree: ast.Module, bad: list[str]) -> dict[str, object]:
    """Check the import statements and resolve what each binds (only allowed modules are imported here)."""
    bound: dict[str, object] = {}
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for a in node.names:
                if not _module_ok(a.name):
                    bad.append(f"line {line}: import {a.name} is not allowed")
                elif "." in a.name and not a.asname:
                    bad.append(f"line {line}: import {a.name} binds the top package; use 'import {a.name} as x'")
                else:
                    try:
                        bound[a.asname or a.name] = import_module(a.name)
                    except ImportError as e:
                        bad.append(f"line {line}: import {a.name} fails: {e}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level or not _module_ok(mod):
                bad.append(f"line {line}: from {'.' * node.level}{mod} import is not allowed")
                continue
            if mod == "__future__":
                continue
            try:
                m = import_module(mod)
            except ImportError as e:
                bad.append(f"line {line}: from {mod} import fails: {e}")
                continue
            for a in node.names:
                if a.name == "*" or not _attr_ok(a.name):
                    bad.append(f"line {line}: from {mod} import {a.name} is not allowed")
                    continue
                try:
                    obj = getattr(m, a.name, _MISSING)
                except Exception:
                    obj = _MISSING
                if obj is _MISSING:
                    bad.append(f"line {line}: from {mod} import {a.name}: no such name")
                    continue
                p = _link_problem(obj)
                if p:
                    bad.append(f"line {line}: from {mod} import {a.name} reaches {p}")
                else:
                    bound[a.asname or a.name] = obj
    return bound


def check_source(src: str) -> list[str]:
    """Problems of a spec module source against the allow-list (empty = accepted); see the module docstring. Allowed
    modules are imported to resolve names and attribute chains (numpy, math and our own pure modules only)."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    bad: list[str] = []
    bound = _imports(tree, bad)
    parent: dict[ast.AST, ast.AST] = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}

    def chain_head(node: ast.expr) -> bool:  # node is the object of a longer attribute chain
        p = parent.get(node)
        return isinstance(p, ast.Attribute) and p.value is node

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Name):
            if node.id in BANNED_NAMES or node.id.startswith("__"):
                bad.append(f"line {line}: name {node.id} is not allowed")
            elif node.id in bound:
                if not isinstance(node.ctx, ast.Load):
                    bad.append(f"line {line}: the imported name {node.id} is rebound")
                elif isinstance(bound[node.id], ModuleType) and not chain_head(node):
                    bad.append(f"line {line}: the module {node.id} is used as a value (only 'module.name' is allowed)")
        elif isinstance(node, ast.arg | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.ExceptHandler):
            name = node.arg if isinstance(node, ast.arg) else node.name
            if name in bound:
                bad.append(f"line {line}: the imported name {name} is rebound")
        elif isinstance(node, ast.Attribute):
            if not _attr_ok(node.attr):
                bad.append(f"line {line}: attribute .{node.attr} is not allowed")
            if chain_head(node):
                continue  # the outermost chain resolves this part
            root, attrs = _chain(node)
            if not (isinstance(root, ast.Name) and root.id in bound):
                continue  # a runtime value: its attributes are checked by name only
            text = ".".join([root.id, *attrs])
            if not isinstance(node.ctx, ast.Load):
                bad.append(f"line {line}: {text} assigns to an imported object")
                continue
            obj = bound[root.id]
            for k, at in enumerate(attrs):
                if not _attr_ok(at):
                    break  # reported above
                try:
                    obj = getattr(obj, at, _MISSING)
                except Exception:  # a lazy module attribute that fails to import, a raising descriptor
                    obj = _MISSING
                where = ".".join([root.id, *attrs[: k + 1]])
                if obj is _MISSING:
                    bad.append(f"line {line}: {where} cannot be resolved")
                    break
                p = _link_problem(obj)
                if p:
                    bad.append(f"line {line}: {where} reaches {p}")
                    break
            else:
                if isinstance(obj, ModuleType):
                    bad.append(f"line {line}: the module {text} is used as a value (only 'module.name' is allowed)")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "__" in node.value:
                bad.append(f"line {line}: a string containing '__' is not allowed: {node.value[:40]!r}")
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
