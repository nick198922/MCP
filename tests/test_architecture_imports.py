"""Import-boundary guard for CLAUDE.md invariants 2 and 3 (AST only, no network, nothing imported from src/).

(a) `analytics/` stays pure: no I/O libraries, no imports of providers/tools/store/execution, no calls to `open(`,
    `time.time(`, `datetime.now(`, `datetime.utcnow(` (time is passed in as a parameter).
(b) Network goes through `core.http.Http` and `Exchanges._call`: in `src/tradedesk/` only `core/http.py` and
    `runtime.py` import `httpx`, only `providers/exchanges.py` imports `ccxt`.
(c) `tools/` (the read-only MCP surface) never import `tradedesk.execution`.

Existing violations live in ALLOWED with the reason; a new one fails the test, and an ALLOWED entry that no longer
matches anything fails too, so the list only shrinks.
"""

import ast
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


def imported_modules(tree: ast.AST, rel: str) -> list[tuple[int, str]]:
    """(line, dotted module) for every import anywhere in the file, incl. function-level ones; relative imports are
    resolved against `rel`, and `from X import y` also yields `X.y` (catches `from tradedesk import execution`)."""
    out = []
    pkg = _package(rel)
    for node in ast.walk(tree):
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


def impure_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, callee) for `open(`, `time.time(`, `datetime.now(`, `datetime.utcnow(` (any `*.datetime.now` alias too)."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name is None:
            continue
        if name in ("open", "time.time") or name.split(".")[-2:] in (["datetime", "now"], ["datetime", "utcnow"]):
            out.append((node.lineno, name))
    return out


def violations(rel: str, source: str) -> list[tuple[str, str, str, int]]:
    """(rule, rel, module or call, line) for one source file; `rel` is its path relative to src/."""
    tree = ast.parse(source, filename=rel)
    imports = imported_modules(tree, rel)
    out = []
    if rel.startswith("tradedesk/analytics/"):
        for line, mod in imports:
            if _matches(mod, ANALYTICS_FORBIDDEN_IMPORTS):
                out.append(("analytics-import", rel, mod, line))
        out += [("analytics-call", rel, name, line) for line, name in impure_calls(tree)]
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


def all_violations() -> list[tuple[str, str, str, int]]:
    out = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        out += violations(rel, path.read_text(encoding="utf-8"))
    return out


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
