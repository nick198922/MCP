"""`make test` needs no network (CLAUDE.md): tests never reach a real venue.

Item 238 of docs/ideas/IDEA-20261102-standing-improvements.md. A test that goes to a venue through a
direct `httpx` / `requests` / `aiohttp` / `websockets` / `ccxt` client turns red at random in the
cloud (proxy 403) or offline, and on the server sends live requests from the nightly pipeline. Two
guards:

1. Static: `find_network_risks(source)` parses a test module with the stdlib `ast` and reports
   (a) direct requests / clients with no fake transport: `httpx.get(...)`, `httpx.AsyncClient()` without
   `transport=` or with `transport=None`, `httpx.AsyncClient(**kw)` without an explicit `transport=`,
   any `requests.*` / `aiohttp.ClientSession` / `websockets.connect` call, a real ccxt exchange such
   as `ccxt.bybit()`; (b) an import of one of those libraries in a file where no fake is used: no call
   or argument that is a `MockTransport` / `Fake*` / `*Mock*` name (`FakeEx()`, `f(FakeEx)`), and no
   non-None `transport=` / `transports=` / `exchange_factory=` argument. A fake that is only
   imported, defined, used as a base class or named in a string does not count. Every
   `tests/**/*.py` (not `tests/fixtures/`) is scanned; a file with a reason is listed in `ALLOWED`,
   and the list must match the scan exactly.
2. Runtime: the autouse `no_real_network` fixture in tests/conftest.py refuses connects to
   non-loopback addresses in every test (opt-out: `@pytest.mark.allow_network(reason=...)`).

Not caught statically: a client built through a helper in `src/` (the runtime guard catches its
connect), `__import__` / `importlib` with a computed name, a `None` transport through a variable
(`transport=t` with `t = None`: only a literal `None` is seen), a fake used somewhere in the file
but not by the client that reaches the network (one used fake marks the whole file as faked).
"""

import ast
import asyncio
import socket
from pathlib import Path

import pytest

from conftest import REAL_CONNECT, RealNetworkBlocked, is_loopback

TESTS = Path(__file__).resolve().parent

LIBS = ("httpx", "requests", "aiohttp", "websockets", "ccxt")
HTTPX_REQUESTS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"})
HTTPX_CLIENTS = frozenset({"Client", "AsyncClient"})
FAKE_KEYWORDS = frozenset({"transport", "transports", "exchange_factory"})

# path relative to tests/ -> reason. Scan of 07.11 (docs/changes/2026-11-07-no-real-network.md): 2 flagged, both
# handler-only files whose MockTransports are built in another test module; no direct client or request anywhere.
# Tightened scan of 07.11 (docs/changes/2026-11-07-no-real-network-tighten.md): +1, a fake used only as a base class;
# no transport=None, no **kwargs client.
ALLOWED: dict[str, str] = {
    "test_demo_option_leg.py": (
        "ccxt only for error classes (ccxt.InvalidOrder / NetworkError / RequestTimeout) set on the venue; the venue is "
        "OptionVenue(FakeVenue), a subclass of test_demo_trader.FakeVenue, so the fake is a base class, not a call"
    ),
    "test_dashboard_flows.py": (
        "httpx only for a 503 handler (httpx.Response) and ccxt only for ccxt.NetworkError in DownEx; the transports "
        "and FlowEx come from test_market_flow._flow_rt (httpx.MockTransport per handler)"
    ),
    "test_dashboard_stablecoins.py": (
        "httpx only for a 503 handler (httpx.Response); the server and its httpx.MockTransport come from test_stablecoin_supply.make_server"
    ),
}


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> dotted module path for imports of LIBS (`import ccxt.async_support as ccxt` -> ccxt.async_support)."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in LIBS:
                    out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0 and node.module.split(".")[0] in LIBS:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in aliases:
        return None
    return ".".join([aliases[node.id], *reversed(parts)])


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _ident(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None


def _is_fake(node: ast.AST) -> bool:
    ident = _ident(node)
    return bool(ident) and (ident == "MockTransport" or ident.startswith("Fake") or "Mock" in ident)


def _uses_fake(call: ast.Call) -> bool:
    """This call builds a fake (`FakeEx()`, `httpx.MockTransport(h)`), passes one (`f(FakeEx)`, `x=MagicMock`) or
    passes a non-None `transport=` / `transports=` / `exchange_factory=`."""
    if _is_fake(call.func):
        return True
    if any(_is_fake(a.value if isinstance(a, ast.Starred) else a) for a in call.args):
        return True
    return any(_is_fake(k.value) or (k.arg in FAKE_KEYWORDS and not _is_none(k.value)) for k in call.keywords)


def _direct_use(call: ast.Call, name: str) -> str | None:
    """Why this call to `name` (a resolved dotted path) reaches the network, or None."""
    lib, _, rest = name.partition(".")
    last = name.rsplit(".", 1)[-1]
    keywords = {k.arg: k.value for k in call.keywords}
    if lib == "httpx":
        if rest in HTTPX_REQUESTS:
            return f"{name}() is a real request"
        if rest in HTTPX_CLIENTS:
            if "transport" in keywords:
                return f"{name}() with transport=None" if _is_none(keywords["transport"]) else None
            if None in keywords:
                return f"{name}(**kwargs) without an explicit transport="
            return f"{name}() without transport="
    elif lib == "requests" and rest and rest.split(".")[0] not in ("exceptions", "adapters", "structures"):
        return f"{name}() goes past any transport mock"
    elif lib == "aiohttp" and rest in ("ClientSession", "request"):
        return f"{name}() is a real client"
    elif lib == "websockets" and last == "connect":
        return f"{name}() opens a real WebSocket"
    elif lib == "ccxt" and last[:1].islower() and last not in ("exchanges",):
        return f"{name}() builds a real ccxt exchange"
    return None


def find_network_risks(source: str) -> list[str]:
    """Pure: reasons why the test module in `source` may reach the real network (empty list: none found)."""
    tree = ast.parse(source)
    aliases = _aliases(tree)
    if not aliases:
        return []
    found: list[str] = []
    faked = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func, aliases)
            why = _direct_use(node, name) if name else None
            if why:
                found.append(f"line {node.lineno}: {why}")
            faked = faked or _uses_fake(node)
    if not faked:
        libs = sorted({v.split(".")[0] for v in aliases.values()})
        found.append(f"imports {', '.join(libs)} with no MockTransport / Fake* / transport= in the file")
    return found


def check(found: dict[str, list[str]], allowed: dict[str, str]) -> tuple[dict[str, list[str]], list[str]]:
    """Pure: (files with risks that are not allowed, allowed files with nothing left to allow)."""
    return {p: r for p, r in found.items() if p not in allowed}, sorted(set(allowed) - set(found))


def scan() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in sorted(TESTS.rglob("*.py")):
        rel = path.relative_to(TESTS).as_posix()
        if rel.startswith("fixtures/"):
            continue
        risks = find_network_risks(path.read_text(encoding="utf-8"))
        if risks:
            out[rel] = risks
    return out


# --- static scanner -----------------------------------------------------------------------------------------------------

CLEAN = """
import httpx

def handler(req):
    return httpx.Response(200, json={})

async def test_ok():
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await c.get("https://api.example.com")
"""

DIRECT = """
import httpx
from httpx import AsyncClient as C

async def test_live():
    async with httpx.AsyncClient() as c:
        await c.get("https://api.bybit.com")
    C(timeout=5)
    httpx.get("https://api.binance.com")
"""

CCXT_REAL = """
import ccxt.async_support as ccxt

def test_errors_only():
    assert issubclass(ccxt.NetworkError, Exception)

async def test_real_exchange():
    ex = ccxt.bybit()
    await ex.fetch_ticker("BTC/USDT")
"""

FAKED_CCXT = """
import ccxt

class FakeEx:
    async def fetch_ticker(self, symbol):
        raise ccxt.NetworkError("down")

def test_down():
    ex = FakeEx()
"""


def test_clean_module_has_no_risks():
    assert find_network_risks(CLEAN) == []
    assert find_network_risks("import json\n\ndef test_x():\n    assert json.dumps(1) == '1'\n") == []
    assert find_network_risks(FAKED_CCXT) == []


def test_client_without_transport_and_direct_request_are_found():
    risks = find_network_risks(DIRECT)
    assert "line 6: httpx.AsyncClient() without transport=" in risks
    assert "line 8: httpx.AsyncClient() without transport=" in risks  # through `from httpx import AsyncClient as C`
    assert "line 9: httpx.get() is a real request" in risks
    assert risks[-1] == "imports httpx with no MockTransport / Fake* / transport= in the file"


def test_real_ccxt_exchange_is_found_but_error_classes_are_not():
    risks = find_network_risks(CCXT_REAL)
    assert risks == [
        "line 8: ccxt.async_support.bybit() builds a real ccxt exchange",
        "imports ccxt with no MockTransport / Fake* / transport= in the file",
    ]


def test_other_libraries_are_found():
    src = "import requests\nimport websockets\nimport aiohttp\nrequests.get('u')\nwebsockets.connect('wss://x')\naiohttp.ClientSession()\n"
    risks = find_network_risks(src)
    assert risks[:3] == [
        "line 4: requests.get() goes past any transport mock",
        "line 5: websockets.connect() opens a real WebSocket",
        "line 6: aiohttp.ClientSession() is a real client",
    ]


FAKED_HEADER = "import httpx\n\ndef handler(req):\n    return httpx.Response(200)\n\nT = httpx.MockTransport(handler)\n"


def test_transport_none_is_no_transport():
    # norm: a real fake transport, also passed through `transports=` to a helper
    assert find_network_risks(FAKED_HEADER + "httpx.AsyncClient(transport=T)\n") == []
    assert find_network_risks("import httpx\nfrom h import make\nmake(transports={'x': T})\n") == []
    # bypass: transport=None is a real client; transports=None / exchange_factory=None do not make the file faked
    assert find_network_risks(FAKED_HEADER + "httpx.AsyncClient(transport=None)\n") == ["line 7: httpx.AsyncClient() with transport=None"]
    assert find_network_risks("import ccxt\nfrom h import make\nmake(transports=None, exchange_factory=None)\n") == [
        "imports ccxt with no MockTransport / Fake* / transport= in the file"
    ]
    # allowed: a listed file with this finding is not a failure
    found = {"test_x.py": find_network_risks(FAKED_HEADER + "httpx.Client(transport=None)\n")}
    assert found == {"test_x.py": ["line 7: httpx.Client() with transport=None"]}
    assert check(found, {"test_x.py": "reason"}) == ({}, [])


def test_kwargs_client_needs_an_explicit_transport():
    # norm: **kwargs next to an explicit transport
    assert find_network_risks(FAKED_HEADER + "kw = {}\nhttpx.AsyncClient(transport=T, **kw)\n") == []
    # bypass: **kwargs alone may carry no transport
    assert find_network_risks(FAKED_HEADER + "kw = {}\nhttpx.AsyncClient(**kw)\n") == [
        "line 8: httpx.AsyncClient(**kwargs) without an explicit transport="
    ]
    assert find_network_risks(FAKED_HEADER + "httpx.Client(timeout=5, **{'transport': None})\n") == [
        "line 7: httpx.Client(**kwargs) without an explicit transport="
    ]
    # allowed
    found = {"test_kw.py": find_network_risks(FAKED_HEADER + "httpx.AsyncClient(**{})\n")}
    assert check(found, {"test_kw.py": "kwargs always hold a MockTransport"}) == ({}, [])


UNUSED_FAKES = '''
import ccxt
from unittest.mock import MagicMock
from helpers import FakeVenue

class FakeEx:
    """FakeEx is defined but never built."""

class LiveVenue(FakeVenue):
    pass

def test_live():
    ex = ccxt.bybit({"enableRateLimit": True}) if False else LiveVenue
    assert "MockTransport" not in str(ex)
'''


def test_fake_counts_only_when_called_or_passed():
    # norm: a fake built by a call, or passed as an argument
    assert find_network_risks("import ccxt\nclass FakeEx:\n    pass\nex = FakeEx()\n") == []
    assert (
        find_network_risks("import ccxt\nfrom h import FakeEx\ndef test_x(monkeypatch):\n    monkeypatch.setattr(ccxt, 'bybit', FakeEx)\n")
        == []
    )
    assert find_network_risks("import httpx\nfrom unittest import mock\nc = mock.MagicMock()\n") == []
    assert find_network_risks("import httpx\nfrom h import run\nrun(rt=FakeRt)\n") == []
    # bypass: imported, defined, a base class or a string only
    risks = find_network_risks(UNUSED_FAKES)
    assert risks == [
        "line 13: ccxt.bybit() builds a real ccxt exchange",
        "imports ccxt with no MockTransport / Fake* / transport= in the file",
    ]
    no_call = "import ccxt\nfrom h import FakeVenue\nclass V(FakeVenue):\n    pass\nE = ccxt.NetworkError\n"
    assert find_network_risks(no_call) == ["imports ccxt with no MockTransport / Fake* / transport= in the file"]
    # allowed: the subclass case is how test_demo_option_leg.py is listed
    assert check({"test_demo_option_leg.py": find_network_risks(no_call)}, ALLOWED)[0] == {}


def test_allowed_file_is_not_a_failure_and_a_stale_entry_is():
    found = {"test_a.py": ["line 1: httpx.get() is a real request"], "test_b.py": ["x"]}
    assert check(found, {"test_a.py": "reason"}) == ({"test_b.py": ["x"]}, [])
    assert check({}, {"test_gone.py": "reason"}) == ({}, ["test_gone.py"])


def test_tests_reach_no_real_network():
    unexpected, stale = check(scan(), ALLOWED)
    assert not unexpected, f"tests that may reach the real network (fix the transport or add to ALLOWED with a reason): {unexpected}"
    assert not stale, f"ALLOWED entries with nothing left to allow: {stale}"


def test_allowed_entries_have_reasons():
    assert all(reason.strip() for reason in ALLOWED.values())


# --- runtime guard (tests/conftest.py) -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "local"),
    [
        (("127.0.0.1", 8000), True),
        (("127.8.9.10", 1), True),
        (("::1", 80, 0, 0), True),
        (("::ffff:127.0.0.1", 80, 0, 0), True),
        (("localhost", 80), True),
        ((b"127.0.0.1", 80), True),
        ("/tmp/socket", True),
        (("192.0.2.1", 443), False),
        (("2001:db8::1", 443, 0, 0), False),
        (("api.bybit.com", 443), False),
        (("0.0.0.0", 80), False),
    ],
)
def test_is_loopback(address, local):
    assert is_loopback(address) is local


def test_guard_refuses_a_non_loopback_socket(no_real_network):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(RealNetworkBlocked):
        s.connect(("192.0.2.1", 443))  # TEST-NET-1 (RFC 5737): never routed
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(RealNetworkBlocked):
        s.connect_ex(("192.0.2.1", 443))
    assert no_real_network == ["('192.0.2.1', 443)", "('192.0.2.1', 443)"]
    no_real_network.clear()  # the refusals were the point of this test: do not fail it at teardown


async def test_guard_refuses_asyncio_connections(no_real_network):
    with pytest.raises(RealNetworkBlocked):
        await asyncio.open_connection("192.0.2.1", 443)
    loop = asyncio.get_running_loop()
    with pytest.raises(RealNetworkBlocked):
        await loop.create_connection(asyncio.Protocol, "192.0.2.1", 443)
    assert len(no_real_network) == 2
    no_real_network.clear()


async def test_guard_lets_loopback_through(no_real_network):
    async def echo(reader, writer):
        writer.write(await reader.read(4))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping")
        await writer.drain()
        assert await reader.read(4) == b"ping"
        writer.close()
        await writer.wait_closed()
    assert no_real_network == []


@pytest.mark.allow_network(reason="checks that the marker lifts the guard; makes no connection")
def test_allow_network_marker_lifts_the_guard():
    assert socket.socket.connect is REAL_CONNECT


def test_guard_is_on_without_the_marker():
    assert socket.socket.connect is not REAL_CONNECT
