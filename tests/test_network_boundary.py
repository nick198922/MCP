"""Invariant 2 of CLAUDE.md: network I/O only through `core.http.Http` and `Exchanges._call`.

Item 167 of docs/ideas/IDEA-20261102-standing-improvements.md. A direct `httpx` / `urllib` /
`requests` / `websockets` call outside `Http` skips rate limits, retries, health and `Evidence`,
and its URL or key can end up in a log. This test parses every `src/tradedesk/**/*.py` with the
stdlib `ast` (no network, no imports of the scanned code) and finds imports of the network
libraries and `ccxt` anywhere in a module: top level, inside `try`, `if`, functions and methods.
Each permitted file is listed in `ALLOWED` with the modules it may import and why (so `ccxt` is
allowed only in `providers/exchanges.py`); known violations that need a decision are in `KNOWN_VIOLATIONS`
(docs/BACKLOG.md 6.45). Both lists must match the scan exactly, so a stale entry fails too.

Item 220: the same scan over `scripts/**/*.py` (research, reports, Telegram and ops scripts). A
script that fetches market data past `Http` has no limits, retries, cache, health or `Evidence`,
so a report built on it has no trace of where the numbers came from. Scripts with a reason are in
`ALLOWED_SCRIPTS`; the rest are listed, not rewritten, in `KNOWN_SCRIPT_VIOLATIONS`.

Not caught: `__import__("httpx")` / `importlib.import_module(...)` with a computed name,
network use through a third-party library that is not in `NETWORK_MODULES`, Python embedded in
`scripts/**/*.sh` heredocs, and `tests/`.
"""

import ast
import re
from pathlib import Path

from test_naive_datetime import _may_spell_ascii_names

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "tradedesk"
SCRIPTS = ROOT / "scripts"

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

_TG = "Telegram Bot API (owner's group), not market data; stdlib-only so it runs as plain python3 from units"
# path relative to scripts/ -> (modules it may import, reason). Scan of 07.11: 233 files, 17 with network imports
# (14 after item 235 moved three scripts onto Http).
ALLOWED_SCRIPTS: dict[str, tuple[frozenset[str], str]] = {
    "tg_send.py": (frozenset({"urllib.request"}), _TG + "; token from agents.env, never logged"),
    "tg_bridge.py": (frozenset({"urllib.request"}), _TG + "; long-poll loop of the owner's chat topic"),
    "telegram_topics.py": (frozenset({"urllib.request"}), _TG + "; one-off topic setup run by the owner"),
    "gh_pr.py": (
        frozenset({"urllib.request"}),
        "GitHub REST API to open an agent PR (fine-grained token, no push or merge); not market data",
    ),
    "yt_transcript.py": (
        frozenset({"urllib.request"}),
        "YouTube oEmbed title/channel for a library item (run with --no-project, outside the venv); not market data",
    ),
    "channel_watch.py": (
        frozenset({"urllib.request"}),
        "public t.me/s/<channel> web preview and TradingView snapshot images for the library; not market data",
    ),
    "agents_status.py": (
        frozenset({"urllib.request"}),
        "Anthropic plan-usage endpoint (api/oauth/usage) for the status message; not market data",
    ),
    "dashboard.py": (
        frozenset({"urllib.request"}),
        "Anthropic plan-usage endpoint (api/oauth/usage) for the page; market data comes from the collector snapshot",
    ),
    "server/bybit_demo_key.py": (
        frozenset({"urllib.request"}),
        "owner-run key setup over ssh with plain python3: signed GET /v5/user/query-api on api-demo.bybit.com only",
    ),
    "bybit_import.py": (
        frozenset({"ccxt"}),
        "owner's private read-only Bybit history: needs a signed ccxt.bybit, and Exchanges holds no keys by design "
        "(the MCP server never reads a key); `check` refuses a key that is not read-only",
    ),
    "binance_import.py": (
        frozenset({"ccxt", "httpx"}),
        "owner's private read-only Binance history: signed ccxt.binance (Exchanges holds no keys), httpx only to "
        "download the prepared file from the URL Binance returns; `check` refuses a key that can trade",
    ),
    "bybit_options_positions.py": (
        frozenset({"ccxt"}),
        "owner's open Bybit option positions through bybit_import's signed read-only client (Exchanges holds no "
        "keys); the public option tickers go through the same client",
    ),
    "fx_history.py": (
        frozenset({"httpx"}),
        "bulk Dukascopy .bi5 day-file downloader for forex research (no provider covers it): own pacing, retries "
        "and a resumable on-disk cache that Http's per-request TTL cache does not give",
    ),
    "thegraph_sample.py": (
        frozenset({"httpx"}),
        "live fixture recorder: requests go through rt.thegraph (Http); httpx only for the wrapping Recorder transport",
    ),
}

# Scripts that read public market data past Http without such a reason (BACKLOG 6.45). Empty since item 235 (07.11):
# hyperliquid_import.py, copy_vaults.py and research/vault_profile.py now go through Http (tests/test_scripts_http.py).
KNOWN_SCRIPT_VIOLATIONS: dict[str, tuple[frozenset[str], str]] = {}


def _matches(name: str, banned: str) -> bool:
    return name == banned or name.startswith(banned + ".")


# An import of a banned module spells the first part of its name; source without any of them is not parsed (~70 % of
# the files, most of the scan time). Python NFKC-normalises identifiers (a full-width `httpx` imports httpx), so source
# with a character that NFKC turns into ASCII letters, digits or `_` is always parsed (as in test_naive_datetime).
_MAY_IMPORT = re.compile("|".join(sorted({m.split(".")[0] for m in (*NETWORK_MODULES, CCXT)})))


def network_imports(source: str) -> list[tuple[int, str]]:
    """(line, banned module) for every network or ccxt import in ``source``; pure, no I/O.

    Relative imports (``from ..core.http import Http``) are never network imports.
    """
    if not _MAY_IMPORT.search(source) and not _may_spell_ascii_names(source):
        return []
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


def test_text_prefilter_keeps_every_import():
    # skipped without parsing: no banned name in the text (Cyrillic and emoji cannot spell one)
    assert not _MAY_IMPORT.search("import os  # Київ 🙂\n") and network_imports("import os  # Київ 🙂\n") == []
    # a full-width name is NFKC-normalised by Python into the real module, so such source is still parsed
    fullwidth = "import ｈｔｔｐｘ\nfrom ｕｒｌｌｉｂ import request\n"
    assert not _MAY_IMPORT.search(fullwidth) and network_imports(fullwidth) == [(1, "httpx"), (2, "urllib.request")]


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


def test_scripts_fixture_violation_allowed_and_http_import(tmp_path):
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "bad.py").write_text("import httpx\n", encoding="utf-8")
    (tmp_path / "tg_send.py").write_text("import urllib.request\nimport urllib.parse\n", encoding="utf-8")
    (tmp_path / "uses_http.py").write_text("from tradedesk.core.http import Http\nimport tradedesk.core.http\n", encoding="utf-8")
    n, found = scan(tmp_path)
    assert n == 3
    assert found == {"research/bad.py": {"httpx"}, "tg_send.py": {"urllib.request"}}
    assert violations(found, ALLOWED_SCRIPTS) == {"research/bad.py": {"httpx"}}
    # an allowed script may import only the modules named for it
    assert violations({"tg_send.py": {"urllib.request", "ccxt"}}, ALLOWED_SCRIPTS) == {"tg_send.py": {"ccxt"}}


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


def test_scripts_network_imports_match_allowed_list():
    n, found = scan(SCRIPTS)
    assert n > 200, f"scanned only {n} files: is SCRIPTS right?"
    listed = {**ALLOWED_SCRIPTS, **KNOWN_SCRIPT_VIOLATIONS}
    new = violations(found, listed)
    assert not new, f"scripts with network imports past core.http.Http (invariant 2): {new}; add a reason or use Http"
    stale = {p: set(m) - found.get(p, set()) for p, (m, _) in listed.items() if set(m) - found.get(p, set())}
    assert not stale, f"ALLOWED_SCRIPTS / KNOWN_SCRIPT_VIOLATIONS entries no longer imported, remove them: {stale}"


def test_script_lists_have_reasons_and_existing_files():
    assert not set(ALLOWED_SCRIPTS) & set(KNOWN_SCRIPT_VIOLATIONS)
    for path, (mods, reason) in {**ALLOWED_SCRIPTS, **KNOWN_SCRIPT_VIOLATIONS}.items():
        assert (SCRIPTS / path).is_file(), path
        assert mods and len(reason) > 20, path
