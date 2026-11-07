"""Import-boundary guard for CLAUDE.md invariants 2 and 3 (AST only, no network, nothing imported from src/).

(a) `analytics/` stays pure: no I/O libraries, no imports of providers/tools/store/execution, no calls to `open(`,
    `time.time(`, `datetime.now(`, `datetime.utcnow(` (time is passed in as a parameter).
(b) Network goes through `core.http.Http` and `Exchanges._call`: in `src/tradedesk/` only `core/http.py` and
    `runtime.py` import `httpx`, only `providers/exchanges.py` imports `ccxt`.
(c) `tools/` (the read-only MCP surface) never import `tradedesk.execution`.
(d) `tools/` and `providers/` never read the wall clock (`time.time(`, `datetime.now(`, `datetime.utcnow(`, aliases
    included): time comes from `rt.now`, so tests with a swapped clock do not depend on the real date.

Existing violations live in ALLOWED with the reason; a new one fails the test, and an ALLOWED entry that no longer
matches anything fails too, so the list only shrinks.
"""

import ast
import functools
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PKG = SRC / "tradedesk"

ANALYTICS_FORBIDDEN_IMPORTS = (
    "httpx",
    "requests",
    "urllib.request",
    "socket",
    "sqlite3",
    "ccxt",
    "websockets",
    "aiohttp",
    "subprocess",
    "tradedesk.providers",
    "tradedesk.tools",
    "tradedesk.store",
    "tradedesk.execution",
)
NETWORK_IMPORT_OWNERS = {
    "httpx": {"tradedesk/core/http.py", "tradedesk/runtime.py"},
    "ccxt": {"tradedesk/providers/exchanges.py"},
}
TOOLS_FORBIDDEN_IMPORTS = ("tradedesk.execution",)
# Wall-clock reads here bypass the swappable `rt.now` clock (CLAUDE.md invariant 3, docs/BACKLOG.md 6.26). `core/`,
# collectors and execution/ keep their own clocks and are deliberately not scanned.
CLOCK_GUARDED = ("tradedesk/tools/", "tradedesk/providers/")

# (rule, file relative to src/, offending module or call) -> why it is accepted for now. See docs/BACKLOG.md 6.20.
ALLOWED: dict[tuple[str, str, str], str] = {
    # execution/ is the ExecutionGuard package (Bybit demo only, docs/SECURITY.md): it builds its own ccxt client
    # with keys, outside the read-only Exchanges provider. Not refactored here: execution/ is out of the agents' scope.
    ("network", "tradedesk/execution/bybit_demo.py", "ccxt"): "ExecutionGuard demo client, owner-only package",
    ("network", "tradedesk/execution/bybit_demo.py", "ccxt.async_support"): "ExecutionGuard demo client, owner-only package",
}


def _matches(module: str, prefixes: tuple[str, ...] | set[str]) -> str | None:
    for p in prefixes:
        if module == p or module.startswith(p + "."):
            return p
    return None


def _package(rel: str) -> list[str]:
    """Dotted package of a module path relative to src/ (`tradedesk/a/b.py` -> ['tradedesk', 'a'])."""
    parts = rel.removesuffix(".py").split("/")
    return parts[:-1]  # for `pkg/__init__.py` this is `pkg`, for `pkg/mod.py` it is `pkg` too


def imported_modules(nodes: list[ast.AST], rel: str) -> list[tuple[int, str]]:
    """(line, dotted module) for every import anywhere in the file (`nodes` = all of its `ast.walk`), incl.
    function-level ones; relative imports are resolved against `rel`, and `from X import y` also yields `X.y` (catches
    `from tradedesk import execution`)."""
    out = []
    pkg = _package(rel)
    for node in nodes:
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out.append((node.lineno, mod))
            out += [(node.lineno, f"{mod}.{a.name}") for a in node.names if a.name != "*"]
    return out


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else None
    return None


def _import_aliases(nodes: list[ast.AST]) -> dict[str, str]:
    """Local name -> imported dotted name (`import time as t` -> t: time, `from time import time` -> time: time.time,
    `from datetime import datetime as D` -> D: datetime.datetime), so aliased clock calls resolve to the real callee."""
    out = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            out.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names if a.name != "*"})
    return out


def clock_calls(nodes: list[ast.AST]) -> list[tuple[int, str]]:
    """(line, callee as written) for wall-clock reads: `time.time(`, `datetime.now(`, `datetime.utcnow(`, aliases
    included (any `*.datetime.now`, `import time as t; t.time()`, `from time import time; time()`). Monotonic and
    perf counters are not clock reads. `nodes` = all of the file's `ast.walk`."""
    aliases = _import_aliases(nodes)
    out = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name is None:
            continue
        head, _, rest = name.partition(".")
        real = ".".join(filter(None, (aliases.get(head, head), rest)))
        if real == "time.time" or real.split(".")[-2:] in (["datetime", "now"], ["datetime", "utcnow"]):
            out.append((node.lineno, name))
    return out


def impure_calls(nodes: list[ast.AST]) -> list[tuple[int, str]]:
    """(line, callee) for `open(` plus every `clock_calls` hit; `nodes` = all of the file's `ast.walk`."""
    opens = [(n.lineno, "open") for n in nodes if isinstance(n, ast.Call) and _dotted(n.func) == "open"]
    return opens + clock_calls(nodes)


def violations(rel: str, source: str) -> list[tuple[str, str, str, int]]:
    """(rule, rel, module or call, line) for one source file; `rel` is its path relative to src/."""
    nodes = list(ast.walk(ast.parse(source, filename=rel)))  # walked once, read by both checks
    imports = imported_modules(nodes, rel)
    out = []
    if rel.startswith("tradedesk/analytics/"):
        for line, mod in imports:
            if _matches(mod, ANALYTICS_FORBIDDEN_IMPORTS):
                out.append(("analytics-import", rel, mod, line))
        out += [("analytics-call", rel, name, line) for line, name in impure_calls(nodes)]
    if rel.startswith(CLOCK_GUARDED):
        out += [("clock-call", rel, name, line) for line, name in clock_calls(nodes)]
    for lib, owners in NETWORK_IMPORT_OWNERS.items():
        if rel in owners or rel.startswith("tradedesk/analytics/"):  # analytics: already reported as analytics-import
            continue
        for line, mod in imports:
            if _matches(mod, (lib,)):
                out.append(("network", rel, mod, line))
    if rel.startswith("tradedesk/tools/"):
        for line, mod in imports:
            if _matches(mod, TOOLS_FORBIDDEN_IMPORTS):
                out.append(("tools-execution", rel, mod, line))
    # `from X import y` yields both X and X.y: drop X.y when X itself is reported on the same line.
    rows = set(out)
    return sorted(
        (rule, r, mod, line)
        for rule, r, mod, line in rows
        if not any(r2 == r and l2 == line and ru2 == rule and mod.startswith(m2 + ".") for ru2, r2, m2, l2 in rows)
    )


@functools.cache
def all_violations() -> tuple[tuple[str, str, str, int], ...]:
    """One scan of src/ per process (~4 s), shared by the two tests below; src/ does not change during a run."""
    out = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        out += violations(rel, path.read_text(encoding="utf-8"))
    return tuple(out)


def test_import_boundaries_hold():
    found = all_violations()
    new = [v for v in found if v[:3] not in ALLOWED]
    assert not new, "import-boundary violations (CLAUDE.md invariants 2-3):\n" + "\n".join(
        f"  {rule}: {rel}:{line} -> {mod}" for rule, rel, mod, line in new
    )


def test_allowed_entries_are_still_real():
    found = {v[:3] for v in all_violations()}
    stale = [k for k in ALLOWED if k not in found]
    assert not stale, f"ALLOWED entries that no longer match anything (remove them and BACKLOG 6.20): {stale}"


def test_scan_covers_the_package():
    rels = {p.relative_to(SRC).as_posix() for p in PKG.rglob("*.py")}
    assert {"tradedesk/core/http.py", "tradedesk/runtime.py", "tradedesk/providers/exchanges.py"} <= rels
    assert sum(r.startswith("tradedesk/analytics/") for r in rels) > 10
    assert sum(r.startswith("tradedesk/tools/") for r in rels) > 10


def test_checker_flags_synthetic_violations():
    analytics_src = """
import httpx
import urllib.request
from urllib import request
from ..providers import deribit
from tradedesk import store
import time
from datetime import datetime
import datetime as dt

def f(path):
    import sqlite3
    with open(path) as fh:
        fh.read()
    return time.time(), datetime.now(), dt.datetime.utcnow()
"""
    got = {(rule, mod) for rule, _, mod, _ in violations("tradedesk/analytics/bad.py", analytics_src)}
    assert got == {
        ("analytics-import", "httpx"),
        ("analytics-import", "urllib.request"),
        ("analytics-import", "tradedesk.providers"),
        ("analytics-import", "tradedesk.store"),
        ("analytics-import", "sqlite3"),
        ("analytics-call", "open"),
        ("analytics-call", "time.time"),
        ("analytics-call", "datetime.now"),
        ("analytics-call", "dt.datetime.utcnow"),
    }

    tool_src = "import httpx\nfrom ..execution import guard\nfrom tradedesk.execution.bybit_demo import X\n"
    got = {(rule, mod) for rule, _, mod, _ in violations("tradedesk/tools/bad.py", tool_src)}
    assert got == {("network", "httpx"), ("tools-execution", "tradedesk.execution"), ("tools-execution", "tradedesk.execution.bybit_demo")}

    provider_src = "def g():\n    import ccxt.async_support as c\n    import httpx\n"
    got = {(rule, mod) for rule, _, mod, _ in violations("tradedesk/providers/other.py", provider_src)}
    assert got == {("network", "ccxt.async_support"), ("network", "httpx")}


def test_checker_accepts_clean_and_owner_sources():
    clean = "import math\nfrom datetime import UTC, datetime\n\ndef f(now):\n    return datetime.fromtimestamp(now, UTC)\n"
    assert violations("tradedesk/analytics/ok.py", clean) == []
    assert violations("tradedesk/core/http.py", "import httpx\n") == []
    assert violations("tradedesk/runtime.py", "import httpx\n") == []
    assert violations("tradedesk/providers/exchanges.py", "import ccxt\nimport ccxt.async_support\n") == []
    # Docstrings and comments that mention open(...) are text, not calls.
    assert violations("tradedesk/analytics/doc.py", '"""open(t1) / open(t0) - 1"""\n# time.time()\n') == []


CLOCK_BAD = """
import time
import time as t
from time import time as wall
from datetime import UTC, datetime
from datetime import datetime as D
import datetime as dt

def f():
    return time.time(), datetime.now(UTC), dt.datetime.utcnow(), t.time(), wall(), D.now()
"""
CLOCK_BAD_CALLS = {"time.time", "datetime.now", "dt.datetime.utcnow", "t.time", "wall", "D.now"}


def test_clock_guard_flags_tools_and_providers():
    for rel in ("tradedesk/tools/bad.py", "tradedesk/providers/bad.py"):
        got = {(rule, call) for rule, _, call, _ in violations(rel, CLOCK_BAD)}
        assert got == {("clock-call", c) for c in CLOCK_BAD_CALLS}, rel
    # analytics/ keeps its own rule name for the same calls; core/ and execution/ are not scanned for clocks.
    assert {c for r, _, c, _ in violations("tradedesk/analytics/bad.py", CLOCK_BAD) if r == "analytics-call"} == CLOCK_BAD_CALLS
    assert violations("tradedesk/core/bad.py", CLOCK_BAD) == []
    assert violations("tradedesk/execution/bad.py", CLOCK_BAD) == []


def test_clock_guard_accepts_rt_now_and_monotonic():
    clean = """
import time
from datetime import UTC, datetime

def f(rt, ms):
    t0 = time.monotonic()
    now = rt.now()
    return now.timestamp(), datetime.fromtimestamp(ms / 1000, UTC), time.perf_counter() - t0, open
"""
    assert violations("tradedesk/tools/ok.py", clean) == []
    assert violations("tradedesk/providers/ok.py", clean) == []
    # open( is an analytics-only rule: tools/providers may read files (e.g. fixtures, store paths).
    assert violations("tradedesk/tools/files.py", "def f(p):\n    return open(p)\n") == []
