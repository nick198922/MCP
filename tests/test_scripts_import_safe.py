"""Guard: importing a `scripts/*.py` module has no side effects.

Item 221 of docs/ideas/IDEA-20261102-standing-improvements.md. A script that fetches data, writes a file, reads a key
or runs its whole study when it is merely imported breaks every test that imports it (tests import many scripts for
their pure helpers) and runs that work inside `pytest`. This test parses every `scripts/*.py` except `_*.py` helpers
with the stdlib `ast` (the scripts are not imported; no network) and checks the statements that run at import time.
It complements tests/test_scripts_import.py (item 120), which imports the guarded scripts with sockets disabled and
so sees only what fails; this one also sees work that succeeds quietly (a cache read, a print, a computed table).

Safe at module level: imports, `def` / `class`, docstrings, `pass`, `if TYPE_CHECKING:` / `try:` blocks under the
same rules, anything under `if __name__ == "__main__":`, and expressions whose calls are all safe:
- `SAFE_CALLS` / `SAFE_METHODS`: pure constructors such as `Path(__file__).resolve()`, `re.compile`, `datetime(...)`,
  `sys.path.insert`, `argparse.ArgumentParser` and `add_argument` (but not `parse_args`);
- a class defined in the same module, or a function of the same module whose body calls only safe things;
- a name or module imported from `tradedesk.analytics` (pure by invariant 3 of CLAUDE.md);
- a safe function of a sibling script imported as a module (`s1.ms(...)`, `vsp.s1.ms(...)`) or by name.
So `main()` and `asyncio.run(...)` may appear only under the guard; a module-level loop or `with` is a finding too.

Exceptions are in `ALLOWED` (file -> number of findings, reason); the count must match the scan, so a new finding in an
allowed file or a stale entry fails. Not checked: decorators, default argument values, class bodies, a class built at
import whose `__init__` / `__post_init__` does I/O, `.replace(...)` on a `Path` (treated as the pure `str`/`datetime`
method), `tradedesk.analytics` code itself, and code in `scripts/**/` subdirectories and `.sh` heredocs.
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
        *("argparse.ArgumentParser",),
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


def module_info(source: str, siblings: frozenset[str] = frozenset()) -> Module:
    """Names one script binds that are safe to call (``siblings``: stems of the other scripts); pure, no I/O."""
    tree = ast.parse(source)
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
        if isinstance(st, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Pass)):
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


def import_side_effects(source: str, others: dict[str, Module] | None = None) -> list[str]:
    """`"<line>: <what>"` for every statement or call that runs on import beyond the safe set; pure, no I/O.

    ``others``: `module_info` of the sibling scripts by stem, to resolve `s1.ms(...)`-style calls.
    """
    others = others or {}
    mod = module_info(source, frozenset(others))
    out: list[tuple[int, str]] = []
    _check(ast.parse(source).body, mod, others, out)
    return [f"{line}: {what}" for line, what in sorted(out)]


def scan(root: Path = SCRIPTS) -> tuple[int, dict[str, list[str]]]:
    """(files scanned, {file name: findings}) over ``root/*.py`` except ``_*.py`` (helpers are only resolved)."""
    sources = {p.stem: p.read_text(encoding="utf-8") for p in sorted(root.glob("*.py"))}
    stems = frozenset(sources)
    others = {stem: module_info(src, stems) for stem, src in sources.items()}
    found = {}
    scanned = [s for s in sources if not s.startswith("_")]
    for stem in scanned:
        hits = import_side_effects(sources[stem], others)
        if hits:
            found[f"{stem}.py"] = hits
    return len(scanned), found


def unexplained(found: dict[str, list[str]], allowed: dict[str, tuple[int, str]]) -> list[str]:
    """Problems: findings in a file that is not allowed, or a count that differs from the allowed count; pure."""
    problems = [f"{path}: {hits}" for path, hits in found.items() if path not in allowed]
    problems += [
        f"{path}: {len(found.get(path, []))} findings, ALLOWED says {n}"
        for path, (n, _) in allowed.items()
        if len(found.get(path, [])) != n
    ]
    return problems


# file name in scripts/ -> (number of findings, why running that at import is acceptable). Scan of 07.11: 224 files,
# 7 with findings; verify_demo_trend_veto and explore_us_open_calls were put under the guard, these 5 remain.
_SIBLING = "loads a sibling script with importlib (exec_module) to reuse its helpers; that script is scanned here too"
ALLOWED: dict[str, tuple[int, str]] = {
    "lesson_sheets.py": (
        1,
        "sys.exit with the `uv run --with pillow` command when PIL is missing (except ImportError); the message is "
        "asserted by tests/test_deps_check.py",
    ),
    "verify_carry_modules.py": (1, "T_START = time.time(): reads the clock for elapsed seconds in its log lines; no I/O"),
    "verify_course_step5_s17.py": (1, "a for loop that builds the VARIANTS table of Var NamedTuples; pure, no I/O"),
    "verify_pm_h11_recheck.py": (1, _SIBLING + " (verify_pm_threshold)"),
    "verify_pm_h7_qlike.py": (1, _SIBLING + " (verify_pm_h7_iv); tests/test_verify_pm_h7_qlike.py imports it"),
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


# --- the repository ------------------------------------------------------------------------------


def test_scripts_import_without_side_effects():
    n, found = scan()
    assert n > 200, f"scanned only {n} files: is SCRIPTS right?"
    problems = unexplained(found, ALLOWED)
    assert problems == [], "move module-level work under `if __name__ == '__main__':` or add a reason to ALLOWED"


def test_allowed_entries_have_reasons_and_files():
    for name, (count, reason) in ALLOWED.items():
        assert (SCRIPTS / name).is_file(), name
        assert count > 0 and len(reason) > 20, name
