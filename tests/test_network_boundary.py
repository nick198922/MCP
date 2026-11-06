"""Invariant 2 of CLAUDE.md: network I/O only through `core.http.Http` and `Exchanges._call`.

Item 167 of docs/ideas/IDEA-20261102-standing-improvements.md. A direct `httpx` / `urllib` /
`requests` / `websockets` call outside `Http` skips rate limits, retries, health and `Evidence`,
and its URL or key can end up in a log. This test parses every `src/tradedesk/**/*.py` with the
stdlib `ast` (no network, no imports of the scanned code) and finds imports of the network
libraries and `ccxt` anywhere in a module: top level, inside `try`, `if`, functions and methods.
Each permitted file is listed in `ALLOWED` with the modules it may import and why (so `ccxt` is
allowed only in `providers/exchanges.py`); known violations that need a decision are in `KNOWN_VIOLATIONS`
(docs/BACKLOG.md 6.45). Both lists must match the scan exactly, so a stale entry fails too.

Not caught: `__import__("httpx")` / `importlib.import_module(...)` with a computed name, and
network use through a third-party library that is not in `NETWORK_MODULES`.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "tradedesk"

# Importing any of these (or a submodule) is network I/O outside `Http`. `urllib.parse` is not.
NETWORK_MODULES = ("httpx", "requests", "urllib.request", "aiohttp", "websockets", "socket")
CCXT = "ccxt"  # allowed only where ALLOWED says so (providers/exchanges.py)

# path relative to src/tradedesk -> (modules it may import, reason)
ALLOWED: dict[str, tuple[frozenset[str], str]] = {
    "core/http.py": (frozenset({"httpx"}), "the Http wrapper itself: limits, retries, cache, health, Evidence"),
    "runtime.py": (
        frozenset({"httpx"}),
        "type only: `transports: dict[str, httpx.AsyncBaseTransport]` in Runtime.__init__ (test MockTransports "
        "handed to Http); no request is made here",
    ),
    "providers/exchanges.py": (frozenset({"ccxt"}), "Exchanges._call is the ccxt boundary of invariant 2"),
    "providers/liquidations.py": (
        frozenset({"websockets"}),
        "public liquidation WebSocket streams (Binance/Bybit/OKX), lazy import in _ws_consume; Http is HTTP-only "
        "and has no WS client. Used by the collector (collector/scheduler.py), not by MCP tools; bounded by "
        "max_seconds, no keys in the URLs",
    ),
}

# Violations found by the first scan (06.11), not fixed here: docs/BACKLOG.md 6.45.
KNOWN_VIOLATIONS: dict[str, tuple[frozenset[str], str]] = {
    "execution/bybit_demo.py": (
        frozenset({"ccxt"}),
        "Bybit demo order client builds its own signed ccxt.bybit (demo host check, key redaction) and "
        "imports ccxt for error classes; outside Exchanges._call by design of execution/, decision pending",
    ),
}


def _matches(name: str, banned: str) -> bool:
    return name == banned or name.startswith(banned + ".")


def network_imports(source: str) -> list[tuple[int, str]]:
    """(line, banned module) for every network or ccxt import in ``source``; pure, no I/O.

    Relative imports (``from ..core.http import Http``) are never network imports.
    """
    banned = (*NETWORK_MODULES, CCXT)
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # `from urllib import request` imports urllib.request
            names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
        else:
            continue
        hits = {b for n in names for b in banned if _matches(n, b)}
        found.extend((node.lineno, b) for b in sorted(hits))
    return sorted(found)


def scan(root: Path = SRC) -> tuple[int, dict[str, set[str]]]:
    """(files scanned, {path relative to root: banned modules imported})."""
    files = sorted(root.rglob("*.py"))
    out: dict[str, set[str]] = {}
    for f in files:
        hits = network_imports(f.read_text(encoding="utf-8"))
        if hits:
            out[f.relative_to(root).as_posix()] = {m for _, m in hits}
    return len(files), out


def violations(found: dict[str, set[str]], allowed: dict[str, tuple[frozenset[str], str]]) -> dict[str, set[str]]:
    """Imports in ``found`` that ``allowed`` does not permit for that file; pure."""
    out = {p: m - allowed.get(p, (frozenset(), ""))[0] for p, m in found.items()}
    return {p: m for p, m in out.items() if m}


# --- checker on fixtures -------------------------------------------------------------------------


def test_allowed_file_passes_forbidden_file_fails():
    assert network_imports("import httpx\n") == [(1, "httpx")]
    found = {"core/http.py": {"httpx"}, "tools/market.py": {"httpx"}}
    assert violations(found, ALLOWED) == {"tools/market.py": {"httpx"}}


def test_forbidden_imports_are_found():
    src = (
        "import os, socket\n"
        "import requests.adapters\n"
        "from aiohttp import ClientSession\n"
        "from urllib import request\n"
        "import urllib.request as ur\n"
        "import ccxt.async_support as ccxt_async\n"
    )
    assert network_imports(src) == [
        (1, "socket"),
        (2, "requests"),
        (3, "aiohttp"),
        (4, "urllib.request"),
        (5, "urllib.request"),
        (6, "ccxt"),
    ]


def test_import_inside_try_is_found():
    src = "try:\n    import websockets\nexcept ImportError:\n    websockets = None\n"
    assert network_imports(src) == [(2, "websockets")]


def test_import_inside_function_is_found():
    src = "class A:\n    async def f(self):\n        if True:\n            from httpx import AsyncClient\n"
    assert network_imports(src) == [(4, "httpx")]


def test_harmless_imports_are_not_found():
    src = (
        "from urllib.parse import urlparse\n"
        "import urllib.parse\n"
        "from ..core.http import Http\n"
        "from .httpx import x\n"  # relative module that happens to share the name
        "import httpx_mock_helpers\n"
        "import socketserver\n"
    )
    assert network_imports(src) == []


def test_ccxt_only_in_exchanges():
    found = {"providers/exchanges.py": {"ccxt"}, "providers/deribit.py": {"ccxt"}}
    assert violations(found, ALLOWED) == {"providers/deribit.py": {"ccxt"}}
    # permission is per module: the exchanges module may not open other network clients
    assert violations({"providers/exchanges.py": {"ccxt", "httpx"}}, ALLOWED) == {"providers/exchanges.py": {"httpx"}}


def test_scan_finds_planted_import(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "ok.py").write_text("from ..core.http import Http\n", encoding="utf-8")
    (tmp_path / "tools" / "bad.py").write_text("def f():\n    import httpx\n", encoding="utf-8")
    n, found = scan(tmp_path)
    assert n == 2
    assert found == {"tools/bad.py": {"httpx"}}
    assert violations(found, ALLOWED) == {"tools/bad.py": {"httpx"}}


# --- the repository ------------------------------------------------------------------------------


def test_src_network_imports_match_allowed_list():
    n, found = scan()
    assert n > 300, f"scanned only {n} files: is SRC right?"
    listed = {**ALLOWED, **KNOWN_VIOLATIONS}
    new = violations(found, listed)
    assert not new, f"network imports outside core.http.Http / Exchanges._call (invariant 2): {new}"
    stale = {p: set(m) - found.get(p, set()) for p, (m, _) in listed.items() if set(m) - found.get(p, set())}
    assert not stale, f"ALLOWED / KNOWN_VIOLATIONS entries no longer imported, remove them: {stale}"


def test_lists_have_reasons_and_existing_files():
    assert not set(ALLOWED) & set(KNOWN_VIOLATIONS)
    for path, (mods, reason) in {**ALLOWED, **KNOWN_VIOLATIONS}.items():
        assert (SRC / path).is_file(), path
        assert mods and len(reason) > 20, path
