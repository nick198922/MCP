"""Guard: importing a `scripts/*.py` module has no side effects.

Item 221 of docs/ideas/IDEA-20261102-standing-improvements.md. A script that fetches data, writes a file, reads a key
or runs its whole study when it is merely imported breaks every test that imports it (tests import many scripts for
their pure helpers) and runs that work inside `pytest`. This test parses every `scripts/**/*.py` (subdirectories such
as `scripts/research/` included, item 230) except `_*.py` helpers with the stdlib `ast` (the scripts are not imported;
no network) and checks the statements that run at import time.
It complements tests/test_scripts_import.py (item 120), which imports the guarded scripts with sockets disabled and
so sees only what fails; this one also sees work that succeeds quietly (a cache read, a print, a computed table).

Safe at module level: imports, docstrings, `pass`, `if TYPE_CHECKING:` / `try:` blocks under the same rules, anything
under `if __name__ == "__main__":`, `def` / `class` statements whose import-time parts are safe (item 241: decorators,
in `SAFE_DECORATORS`, `@x.setter`-style or a safe callable, with safe calls in their arguments; default argument values;
class bases and keywords; the statements of a class body, its methods included, under the same rules as the module),
and expressions whose calls are all safe:
- `SAFE_CALLS` / `SAFE_METHODS`: pure constructors such as `Path(__file__).resolve()`, `re.compile`, `datetime(...)`,
  `sys.path.insert`, `argparse.ArgumentParser` and `add_argument` (but not `parse_args`);
- a class defined in the same module, or a function of the same module whose body calls only safe things;
- a name or module imported from `tradedesk.analytics` (pure by invariant 3 of CLAUDE.md);
- a safe function of a sibling script imported as a module (`s1.ms(...)`, `vsp.s1.ms(...)`) or by name; a script in
  a subdirectory sees the scripts of its own directory first, then those of `scripts/`.
So `main()` and `asyncio.run(...)` may appear only under the guard; a module-level loop or `with` is a finding too.

Exceptions are in `ALLOWED` (file -> number of findings, reason); the count must match the scan, so a new finding in an
allowed file or a stale entry fails. Not checked: annotations evaluated at `def` time (scripts without
`from __future__ import annotations`), defaults of a module-level `lambda`, a class built at import whose `__init__` /
`__post_init__` does I/O, `.replace(...)` on a `Path` (treated as the pure `str`/`datetime` method),
`tradedesk.analytics` code itself, and Python in `.sh` heredocs.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# Dotted callee names that only build a value (no network, no file or key access, no output).
SAFE_CALLS = frozenset(
    {
        *("str", "int", "float", "bool", "bytes", "tuple", "list", "dict", "set", "frozenset", "isinstance"),
        *("range", "enumerate", "zip", "sorted", "reversed", "len", "min", "max", "sum", "abs", "round"),
        *("Path", "PurePath", "pathlib.Path", "Path.home", "re.compile", "struct.Struct", "logging.getLogger"),
        *("datetime", "date", "timedelta", "timezone", "ZoneInfo", "dt.datetime", "dt.date", "dt.timedelta"),
        *("datetime.fromisoformat", "date.fromisoformat", "datetime.strptime", "NormalDist", "statistics.NormalDist"),
        *("namedtuple", "collections.namedtuple", "defaultdict", "TypeVar", "field", "dataclasses.field", "Decimal"),
        *("np.datetime64", "sys.path.insert", "sys.path.append", "os.environ.get", "os.getenv"),
        *("argparse.ArgumentParser", "threading.Lock", "threading.RLock"),
    }
)
# Method names that are pure on whatever they are called on (Path, str, dict, datetime, an ArgumentParser).
SAFE_METHODS = frozenset(
    {
        *("resolve", "with_name", "with_suffix", "expanduser", "joinpath", "timestamp", "astimezone", "isoformat"),
        *("replace", "astype", "split", "rsplit", "join", "strip", "lower", "upper", "format", "items", "keys"),
        *("values", "add_argument", "add_argument_group", "add_mutually_exclusive_group", "add_subparsers"),
        *("add_parser", "set_defaults"),
    }
)
# Decorators that only wrap or annotate the object they are applied to (no I/O, no registry with side effects).
SAFE_DECORATORS = frozenset(
    {
        *("dataclass", "dataclasses.dataclass", "property", "staticmethod", "classmethod", "abstractmethod"),
        *("abc.abstractmethod", "functools.cache", "functools.lru_cache", "functools.cached_property", "cache"),
        *("lru_cache", "cached_property", "functools.wraps", "wraps", "functools.total_ordering", "total_ordering"),
        *("contextlib.contextmanager", "contextmanager", "contextlib.asynccontextmanager", "asynccontextmanager"),
        *("typing.overload", "overload", "unique", "enum.unique"),
    }
)
# `@x.setter`-style decorators of a property defined above
_PROPERTY_METHODS = frozenset({"setter", "getter", "deleter"})
_LOOPS = (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Match)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


@dataclass
class Module:
    """What one script binds that is safe to call at import time, and how it names its siblings."""

    safe: set[str] = field(default_factory=set)  # own classes and pure functions, names from tradedesk.analytics
    analytics: set[str] = field(default_factory=set)  # names bound to tradedesk.analytics modules or objects
    siblings: dict[str, str] = field(default_factory=dict)  # alias -> sibling script (import x [as y])
    from_siblings: dict[str, tuple[str, str]] = field(default_factory=dict)  # name -> (script, name) (from x import n)
    aliases: dict[str, str] = field(default_factory=dict)  # name -> dotted name it is bound to (`ms = s14.ms`)


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{_dotted(node.func)}()"
    return "<expr>"


def _is_main_guard(node: ast.stmt) -> bool:
    t = node.test if isinstance(node, ast.If) else None
    return (
        isinstance(t, ast.Compare)
        and isinstance(t.left, ast.Name)
        and t.left.id == "__name__"
        and len(t.ops) == 1
        and isinstance(t.ops[0], ast.Eq)
        and isinstance(t.comparators[0], ast.Constant)
        and t.comparators[0].value == "__main__"
    )


def _calls(node: ast.AST, *, top: bool = True):
    """Calls that run when ``node`` runs; nested ``def`` / ``class`` / ``lambda`` bodies are skipped."""
    if not top and isinstance(node, _DEFS):
        return
    if isinstance(node, ast.Call):
        yield node
    for child in ast.iter_child_nodes(node):
        yield from _calls(child, top=False)


def _name_is_safe(name: str, mod: Module, others: dict[str, Module], depth: int = 0) -> bool:
    if name in SAFE_CALLS or name in mod.safe:
        return True
    head, _, rest = name.partition(".")
    if rest and head.removesuffix("()") in mod.analytics:  # `fm.month_starts(...)`, `Fees().scaled(...)`
        return True
    if depth < 3 and name in mod.aliases:
        return _name_is_safe(mod.aliases[name], mod, others, depth + 1)
    if depth < 3 and name in mod.from_siblings:
        script, orig = mod.from_siblings[name]
        return script in others and _name_is_safe(orig, others[script], others, depth + 1)
    if depth < 3 and rest and head in mod.siblings and mod.siblings[head] in others:
        return _name_is_safe(rest, others[mod.siblings[head]], others, depth + 1)
    return False


def _call_is_safe(call: ast.Call, mod: Module, others: dict[str, Module]) -> bool:
    if isinstance(call.func, ast.Attribute) and call.func.attr in SAFE_METHODS:
        return True
    return _name_is_safe(_dotted(call.func), mod, others)


def module_info(source: str | ast.Module, siblings: frozenset[str] = frozenset()) -> Module:
    """Names one script binds that are safe to call (``siblings``: stems of the other scripts); pure, no I/O.

    ``source``: the text, or its already parsed tree (``scan`` parses each file once, not once per folder)."""
    tree = ast.parse(source) if isinstance(source, str) else source
    mod = Module()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            mod.safe.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in siblings:
                    mod.siblings[a.asname or a.name] = a.name
                elif a.name.startswith("tradedesk.analytics") and a.asname:
                    mod.analytics.add(a.asname)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module in siblings:
                mod.from_siblings |= {a.asname or a.name: (node.module, a.name) for a in node.names}
            elif node.module == "tradedesk.analytics" or node.module.startswith("tradedesk.analytics."):
                mod.analytics |= {a.asname or a.name for a in node.names}
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, (ast.Name, ast.Attribute))
        ):
            mod.aliases[node.targets[0].id] = _dotted(node.value)
    mod.safe |= mod.analytics
    # `main` is the entry point: calling it at import is a finding whatever its body looks like
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name != "main"}
    changed = True
    while changed:  # fixed point: a function is safe when every call in its body is (own helpers included)
        changed = False
        for name, fn in funcs.items():
            if name not in mod.safe and all(_call_is_safe(c, mod, {}) for s in fn.body for c in _calls(s)):
                mod.safe.add(name)
                changed = True
    return mod


def _check(body: list[ast.stmt], mod: Module, others: dict[str, Module], out: list[tuple[int, str]]) -> None:
    def calls(node: ast.AST) -> None:
        out.extend((c.lineno, f"call {_dotted(c.func)}()") for c in _calls(node) if not _call_is_safe(c, mod, others))

    for st in body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _check_def(st, mod, others, out)
            continue
        if isinstance(st, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if _is_main_guard(st):
            continue
        if isinstance(st, ast.If):
            calls(st.test)
            _check(st.body + st.orelse, mod, others, out)
        elif isinstance(st, ast.Try):
            _check(st.body + [s for h in st.handlers for s in h.body] + st.orelse + st.finalbody, mod, others, out)
        elif isinstance(st, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Assert)):
            calls(st)
        elif isinstance(st, _LOOPS):
            out.append((st.lineno, f"{type(st).__name__.lower()} at module level"))
        else:
            out.append((st.lineno, f"{type(st).__name__} at module level"))


def _check_def(
    st: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    mod: Module,
    others: dict[str, Module],
    out: list[tuple[int, str]],
) -> None:
    """What a `def` / `class` statement runs at import: decorators, default values, bases and keywords, class body."""

    def calls(node: ast.AST) -> None:
        out.extend((c.lineno, f"call {_dotted(c.func)}()") for c in _calls(node) if not _call_is_safe(c, mod, others))

    for d in st.decorator_list:
        # `@deco` and `@deco(...)` both call `deco` on the object; the arguments of `@deco(...)` are evaluated first
        target = d.func if isinstance(d, ast.Call) else d
        name = _dotted(target)
        applied = (
            name in SAFE_DECORATORS
            or (isinstance(target, ast.Attribute) and target.attr in _PROPERTY_METHODS)
            or _name_is_safe(name, mod, others)
        )
        if not applied:
            out.append((d.lineno, f"decorator @{name}"))
        for part in [d.func, *d.args, *(k.value for k in d.keywords)] if isinstance(d, ast.Call) else [d]:
            calls(part)
    if isinstance(st, ast.ClassDef):
        for e in [*st.bases, *(k.value for k in st.keywords)]:
            calls(e)
        _check(st.body, mod, others, out)  # a class body runs at import (methods in it: decorators and defaults)
    else:
        for e in [*st.args.defaults, *(d for d in st.args.kw_defaults if d is not None)]:
            calls(e)


def import_side_effects(source: str, others: dict[str, Module] | None = None) -> list[str]:
    """`"<line>: <what>"` for every statement or call that runs on import beyond the safe set; pure, no I/O.

    ``others``: `module_info` of the sibling scripts by stem, to resolve `s1.ms(...)`-style calls.
    """
    others = others or {}
    tree = ast.parse(source)
    return _side_effects(tree, module_info(tree, frozenset(others)), others)


def _side_effects(tree: ast.Module, mod: Module, others: dict[str, Module]) -> list[str]:
    out: list[tuple[int, str]] = []
    _check(tree.body, mod, others, out)
    return [f"{line}: {what}" for line, what in sorted(out)]


def _imported_names(tree: ast.Module) -> frozenset[str]:
    """The module names module_info tests against ``siblings``: top-level `import x` and absolute `from x import`."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return frozenset(names)


def scan(root: Path = SCRIPTS) -> tuple[int, dict[str, list[str]]]:
    """(files scanned, {path relative to ``root``: findings}) over ``root/**/*.py`` except ``_*.py``.

    ``_*.py`` helpers are only resolved. A script in a subdirectory resolves siblings in its own directory first, then
    in ``root`` (subdirectory scripts put their own directory or `scripts/` on ``sys.path``).
    """
    by_dir: dict[Path, dict[str, str]] = {}
    for p in sorted(root.rglob("*.py")):
        by_dir.setdefault(p.parent, {})[p.stem] = p.read_text(encoding="utf-8")
    top = by_dir.get(root, {})
    stems = {folder: frozenset({**top, **sources}) for folder, sources in by_dir.items()}
    # module_info of every script as each folder sees it (a top-level script is visible from every folder, unless a
    # script of that folder has its stem). Each file is parsed once here and once for its own check below; parsing
    # it again per folder was ~70 % of the scan time. Trees are not kept: all of them take ~340 MiB per worker.
    # module_info reads `siblings` only to test the names of the top-level `import x` / `from x import`, so folders
    # whose stems agree on those names share one result.
    infos: dict[Path, dict[str, Module]] = {folder: {} for folder in by_dir}
    for home, sources in by_dir.items():
        for stem, src in sources.items():
            tree = ast.parse(src)
            imported = _imported_names(tree)
            shared: dict[frozenset[str], Module] = {}
            for folder in by_dir:
                if folder == home or (home == root and stem not in by_dir[folder]):
                    key = imported & stems[folder]
                    if key not in shared:
                        shared[key] = module_info(tree, stems[folder])
                    infos[folder][stem] = shared[key]
    n, found = 0, {}
    for folder, sources in by_dir.items():
        others = infos[folder]
        for stem in (s for s in sources if not s.startswith("_")):
            n += 1
            # = import_side_effects(sources[stem], others): others[stem] is module_info(sources[stem], stems[folder])
            hits = _side_effects(ast.parse(sources[stem]), others[stem], others)
            if hits:
                found[(folder.relative_to(root) / f"{stem}.py").as_posix()] = hits
    return n, found


def unexplained(found: dict[str, list[str]], allowed: dict[str, tuple[int, str]]) -> list[str]:
    """Problems: findings in a file that is not allowed, or a count that differs from the allowed count; pure."""
    problems = [f"{path}: {hits}" for path, hits in found.items() if path not in allowed]
    problems += [
        f"{path}: {len(found.get(path, []))} findings, ALLOWED says {n}"
        for path, (n, _) in allowed.items()
        if len(found.get(path, [])) != n
    ]
    return problems


# path relative to scripts/ -> (number of findings, why running that at import is acceptable). Scan of 07.11: 224 files,
# 7 with findings; verify_demo_trend_veto and explore_us_open_calls were put under the guard, verify_pm_h11_recheck
# and verify_pm_h7_qlike now import their sibling with a plain `import` (item 226), these 3 remain. Subdirectories
# (item 230, 07.11): 8 files in research/ and server/, no findings. Decorators, defaults and class bodies (item 241,
# 07.11): 232 files, one new finding, `threading.Lock()` in a class body of dashboard.py, pure: added to SAFE_CALLS.
ALLOWED: dict[str, tuple[int, str]] = {
    "lesson_sheets.py": (
        1,
        "sys.exit with the `uv run --with pillow` command when PIL is missing (except ImportError); the message is "
        "asserted by tests/test_deps_check.py",
    ),
    "verify_carry_modules.py": (1, "T_START = time.time(): reads the clock for elapsed seconds in its log lines; no I/O"),
    "copy_vaults.py": (1, "BYBIT = Http(...): constructing the client does no I/O (httpx client is created lazily)"),
    "research/vault_profile.py": (1, "HL_INFO = Http(...): constructing the client does no I/O (httpx client is created lazily)"),
    "verify_course_step5_s17.py": (1, "a for loop that builds the VARIANTS table of Var NamedTuples; pure, no I/O"),
}


# --- checker on fixtures -------------------------------------------------------------------------


_OK = '''\
"""Docstring."""
import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tradedesk.analytics import flow_model as fm  # noqa: E402
from tradedesk.analytics.stop_management import StopRule  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = Path.home() / "x" / "y.json"
parser = argparse.ArgumentParser(description="x")
parser.add_argument("--n", type=int, default=3)
NAMES = ",".join(sorted({"b", "a"}))
LATER = lambda: fetch()  # noqa: E731


def ms(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp() * 1000)


def day_ms(s: str) -> int:
    return ms(*(int(x) for x in s.split("-")))


@dataclass(frozen=True)
class Params:
    n: int


START, END = ms(2021, 1, 1), day_ms("2026-01-01")
DEFAULT, RULE, MONTHS = Params(3), StopRule("A"), fm.month_starts(START, END)


def main() -> None:
    args = parser.parse_args()
    print(args)


if __name__ == "__main__":
    main()
'''


def test_clean_script_has_no_findings():
    assert import_side_effects(_OK) == []
    # `map` is not in SAFE_CALLS, so a helper using it is not safe and calling it at import is reported
    line = _OK.splitlines().index('START, END = ms(2021, 1, 1), day_ms("2026-01-01")') + 1
    assert import_side_effects(_OK.replace("(int(x) for x in s.split", "map(int, s.split")) == [f"{line}: call day_ms()"]


def test_main_at_module_level_is_found():
    # `main` is the entry point: never treated as a safe helper, even with a body that looks pure
    src = "import asyncio\n\n\ndef main():\n    pass\n\n\nmain()\nasyncio.run(main())\n"
    assert import_side_effects(src) == ["8: call main()", "9: call asyncio.run()", "9: call main()"]


def test_network_call_at_module_level_is_found():
    src = "import requests\nDATA = requests.get('https://example.com').json()\n"
    assert import_side_effects(src) == ["2: call requests.get()", "2: call requests.get().json()"]
    # a helper that fetches is not safe, so calling it at import is reported
    src = "import requests\n\n\ndef load():\n    return requests.get('https://example.com').json()\n\n\nDATA = load()\n"
    assert import_side_effects(src) == ["8: call load()"]


def test_parse_args_file_io_and_loops_are_found():
    src = (
        "import argparse, json\nfrom pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "args = p.parse_args()\n"
        "rows = json.loads(Path('x.json').read_text())\n"
        "for r in rows:\n    print(r)\n"
        "with open('y') as f:\n    pass\n"
        "if args.n:\n    Path('z').write_text('1')\n"
    )
    assert import_side_effects(src) == [
        "4: call p.parse_args()",
        "5: call Path().read_text()",
        "5: call json.loads()",
        "6: for at module level",
        "8: with at module level",
        "11: call Path().write_text()",
    ]


def test_guard_present_and_variants():
    # the guard hides its body; a guard with another comparison does not
    assert import_side_effects("if __name__ == '__main__':\n    main()\n") == []
    assert import_side_effects("if __name__ != '__main__':\n    main()\n") == ["2: call main()"]
    assert import_side_effects("if __name__ == 'x':\n    main()\n") == ["2: call main()"]
    # a class from another (non-analytics) module is not assumed pure
    assert import_side_effects("from tradedesk.core.http import Http\nH = Http()\n") == ["2: call Http()"]


def test_decorators_are_checked():
    clean = (
        "import contextlib, functools\nfrom dataclasses import dataclass\n\n\n"
        "def deco(f):\n    return f\n\n\n"
        "@dataclass(frozen=True)\nclass A:\n    x: int = 1\n\n"
        "    @property\n    def y(self):\n        return 1\n\n"
        "    @y.setter\n    def y(self, v):\n        pass\n\n"
        "    @staticmethod\n    def s():\n        pass\n\n\n"
        "@functools.lru_cache(maxsize=int('8'))\n@contextlib.contextmanager\n@deco\ndef f():\n    yield\n"
    )
    assert import_side_effects(clean) == []
    bad = (
        "import app, functools, requests\nREG = []\n\n\n"
        "def reg(f):\n    REG.append(f)\n    return f\n\n\n"  # registers: not a pure decorator
        "@app.route('/')\ndef a():\n    pass\n\n\n"
        "@functools.lru_cache(maxsize=requests.get('u').json())\ndef b():\n    pass\n\n\n"
        "@reg\nclass C:\n    pass\n\n\n"
        "@app.make().route\ndef d():\n    pass\n"
    )
    assert import_side_effects(bad) == [
        "10: decorator @app.route",
        "15: call requests.get()",
        "15: call requests.get().json()",
        "20: decorator @reg",
        "25: call app.make()",
        "25: decorator @app.make().route",
    ]


def test_class_bodies_bases_and_keywords_are_checked():
    clean = (
        "import threading\nfrom dataclasses import dataclass, field\nfrom typing import NamedTuple\n\n\n"
        "class Var(NamedTuple):\n    '''Doc.'''\n    name: str\n    n: int = 3\n\n\n"
        "@dataclass\nclass S:\n    rows: list = field(default_factory=list)\n    lock = threading.Lock()\n"
        "    if True:\n        LIMIT = int('5')\n\n"
        "    class Inner:\n        pass\n"
    )
    assert import_side_effects(clean) == []
    bad = (
        "import json\n\n\n"
        "class Cfg(make_base()):\n"
        "    data = json.load(open('x'))\n"
        "    for i in range(3):\n        pass\n\n"
        "    class Inner(metaclass=meta()):\n        print('x')\n\n"
        "    def method(self):\n        return json.load(open('y'))\n"  # a method body runs only when called
    )
    assert import_side_effects(bad) == [
        "4: call make_base()",
        "5: call json.load()",
        "5: call open()",
        "6: for at module level",
        "9: call meta()",
        "10: call print()",
    ]


def test_default_argument_values_are_checked():
    clean = (
        "import os\nfrom pathlib import Path\n\n\n"
        "def f(x=3, *, out=Path('a') / 'b', key=os.environ.get('A'), z=None):\n"
        "    def inner(y=fetch()):\n        return y\n"  # a nested default runs only when `f` runs
        "    return inner\n"
    )
    assert import_side_effects(clean) == []
    bad = (
        "import time\n\n\n"
        "def f(x=time.time(), *, w=load()):\n    pass\n\n\n"
        "async def g(a=fetch()):\n    pass\n\n\n"
        "class K:\n    def m(self, now=time.time()):\n        pass\n"
    )
    assert import_side_effects(bad) == [
        "4: call load()",
        "4: call time.time()",
        "8: call fetch()",
        "13: call time.time()",
    ]


def test_sibling_script_helpers_are_resolved():
    s1_src = "import json\n\n\ndef ms(y):\n    return y\n\n\ndef load():\n    return json.load(open('x'))\n"
    others = {"s1": module_info(s1_src), "vsp": module_info("import s1\n", frozenset({"s1"}))}
    assert others["s1"].safe == {"ms"} and others["vsp"].siblings == {"s1": "s1"}
    src = (
        "import s1\nimport vsp\nfrom s1 import ms as m2, load\n"
        "A = s1.ms(1)\nB = vsp.s1.ms(2)\nC = m2(3)\nD = s1.load()\nE = load()\n"
        "ms, get = s1.ms, s1.load\nms = s1.ms\nget = s1.load\nF = ms(4)\nG = get()\n"
    )
    assert import_side_effects(src, others) == ["7: call s1.load()", "8: call load()", "13: call get()"]


def test_allowed_counts_and_scan(tmp_path):
    (tmp_path / "good.py").write_text(_OK, encoding="utf-8")
    (tmp_path / "bad.py").write_text("def main():\n    pass\n\nmain()\n", encoding="utf-8")
    (tmp_path / "_helper.py").write_text("main()\n", encoding="utf-8")  # helpers are not scanned
    n, found = scan(tmp_path)
    assert n == 2
    assert found == {"bad.py": ["4: call main()"]}
    assert unexplained(found, {}) == ["bad.py: ['4: call main()']"]
    allowed = {"bad.py": (1, "a reason long enough")}
    assert unexplained(found, allowed) == []
    assert unexplained({"bad.py": ["4: call main()", "5: call main()"]}, allowed) == ["bad.py: 2 findings, ALLOWED says 1"]
    assert unexplained({}, allowed) == ["bad.py: 0 findings, ALLOWED says 1"]


def test_scan_covers_subdirectories(tmp_path):
    top = "def ms(y):\n    return y\n\n\ndef load():\n    return open('x').read()\n"
    (tmp_path / "top.py").write_text(top, encoding="utf-8")
    (tmp_path / "near.py").write_text("def day(s):\n    return open(s).read()\n", encoding="utf-8")
    sub = tmp_path / "research"
    (sub / "deeper").mkdir(parents=True)
    (sub / "near.py").write_text("def day(s):\n    return s\n", encoding="utf-8")  # shadows the unsafe top-level one
    uses = "import top\nfrom near import day\nA = top.ms(1)\nB = day('x')\nC = top.load()\n"
    (sub / "uses.py").write_text(uses, encoding="utf-8")
    bad = "import asyncio\n\n\nasync def main():\n    pass\n\n\nasyncio.run(main())\n"
    (sub / "bad.py").write_text(bad, encoding="utf-8")
    (sub / "_helper.py").write_text("main()\n", encoding="utf-8")  # helpers are not scanned in subdirectories either
    (sub / "deeper" / "x.py").write_text("print('hi')\n", encoding="utf-8")
    n, found = scan(tmp_path)
    assert n == 6
    assert found == {
        "research/bad.py": ["8: call asyncio.run()", "8: call main()"],
        "research/uses.py": ["5: call top.load()"],
        "research/deeper/x.py": ["1: call print()"],
    }


# --- the repository ------------------------------------------------------------------------------


def test_scripts_import_without_side_effects():
    n, found = scan()
    assert n > 200, f"scanned only {n} files: is SCRIPTS right?"
    n_top = sum(1 for p in SCRIPTS.glob("*.py") if not p.name.startswith("_"))
    assert n > n_top, "scripts/**/ subdirectories (research/, server/) are not scanned"
    problems = unexplained(found, ALLOWED)
    assert problems == [], "move module-level work under `if __name__ == '__main__':` or add a reason to ALLOWED"


def test_allowed_entries_have_reasons_and_files():
    for name, (count, reason) in ALLOWED.items():
        assert (SCRIPTS / name).is_file(), name
        assert count > 0 and len(reason) > 20, name
