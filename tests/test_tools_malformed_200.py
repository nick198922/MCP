"""End-to-end check of CLAUDE.md invariant 1 for HTTP 200 answers with a malformed body.

Sibling of test_tools_never_raise.py (same arguments, skip list and MCP-client loop): every HTTP transport answers
200 with an empty body, non-JSON text, `{}`, `[]` or `null`, and every ccxt call returns the same malformed value
(None for the two non-JSON modes). A tool must still return the envelope (`ok` true or false), never an error result.
"""

from __future__ import annotations

import httpx
import pytest
from mcp import Client

from conftest import NOW
from test_server import EXPECTED_TOOLS
from test_tools_never_raise import SKIP, _AllTransports, _call_every_tool
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

# mode -> (HTTP 200 body, content type, value every ccxt call returns)
MODES: dict[str, tuple[bytes, str, object]] = {
    "empty": (b"", "application/json", None),
    "text": (b"<html>maintenance</html>", "text/html", None),
    "object": (b"{}", "application/json", {}),
    "array": (b"[]", "application/json", []),
    "null": (b"null", "application/json", None),
}

# Tools that still raise on a malformed 200 body, per mode; each is listed in docs/changes/2026-11-06-malformed-200.md.
KNOWN_RAISERS: dict[str, set[str]] = {mode: set() for mode in MODES}


class _MalformedEx:
    """A ccxt exchange whose every call returns the same malformed value (`has` is empty: no capability gate)."""

    def __init__(self, ex_id: str, value: object) -> None:
        self.id = ex_id
        self.has: dict = {}
        self.markets: dict = {}
        self._value = value

    def __getattr__(self, name: str):
        async def reply(*_a, **_k):
            return type(self._value)() if self._value is not None else None  # a fresh copy per call

        return reply

    async def close(self) -> None:
        pass


def _runtime(tmp_path, mode: str) -> tuple[Runtime, list[str]]:
    body, ctype, ccxt_value = MODES[mode]
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.url.host}{req.url.path}")
        return httpx.Response(200, content=body, headers={"content-type": ctype})

    rt = Runtime(
        Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / "malformed.sqlite")),
        transports=_AllTransports(httpx.MockTransport(handler)),
        exchange_factory=lambda ex_id: _MalformedEx(ex_id, ccxt_value),
    )
    rt.now = lambda: NOW
    return rt, calls


@pytest.mark.parametrize("mode", list(MODES))
async def test_no_tool_raises_on_malformed_200(tmp_path, mode):
    rt, calls = _runtime(tmp_path, mode)
    srv, _ = build_server(rt)
    listed, called, raised, no_envelope = await _call_every_tool(srv)

    assert listed >= len(EXPECTED_TOOLS)
    assert called == listed - len(SKIP)
    assert calls, "no request reached the malformed transport"
    assert set(raised) == KNOWN_RAISERS[mode], f"tools that raised: {raised}"
    assert not no_envelope, f"tools without an envelope: {no_envelope}"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("market_ticker", {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT"}),
        ("market_orderbook", {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT"}),
    ],
)
async def test_ccxt_list_instead_of_dict_goes_to_missing(tmp_path, tool, args):
    """A ccxt call that returns a list where a dict is expected is reported in `missing`, not raised (fixed 06.11)."""
    rt, _ = _runtime(tmp_path, "array")
    srv, _ = build_server(rt)
    async with Client(srv) as c:
        res = await c.call_tool(tool, args)
    assert not res.is_error
    data = res.structured_content
    data = data.get("result", data)
    assert data["ok"] is False
    assert [(m["provider"], m["reason"]) for m in data["missing"]] == [("ccxt:binanceusdm", "data_unavailable")]
    assert "list" in data["missing"][0]["detail"]


def test_known_raisers_name_registered_tools_only():
    assert set(KNOWN_RAISERS) == set(MODES)
    assert set().union(*KNOWN_RAISERS.values()) <= EXPECTED_TOOLS
