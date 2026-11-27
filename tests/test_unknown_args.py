"""Unknown argument names (backlog 27.11): the SDK drops them silently; DeskServer.call_tool warns (read tools) or
refuses (store-writing tools). Every test goes through an MCP Client on the fake transports unless it says otherwise."""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import DeskServer, build_server
from tradedesk.unknown_args import MAX_NAME_CHARS, MAX_SHOWN_NAMES, STORE_WRITE_TOOLS, schema_params, shown_name, shown_names

NOTE = "unknown argument name(s) ignored"


def _env(res) -> dict:
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


async def _call(server, name, args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    assert not res.is_error, res.content
    env = _env(res)
    assert json.loads(res.content[0].text) == env  # the text block says the same as the structured content
    return env


def _same_answer(a: dict, b: dict) -> None:
    assert a["ok"] == b["ok"] and a["data"] == b["data"] and a["missing"] == b["missing"]
    assert [(e["provider"], e["tool"], e["args"]) for e in a["evidence"]] == [(e["provider"], e["tool"], e["args"]) for e in b["evidence"]]


async def test_sdk_still_drops_unknown_keys_silently(rt):
    """The hole itself, on a plain MCPServer: the unknown key is gone and the default (BTC) answered. If the SDK starts
    rejecting extra keys this fails, and DeskServer's warning is no longer needed in this form."""
    from mcp.server.mcpserver import MCPServer

    from tradedesk.tools import register_all

    plain = MCPServer("plain")
    register_all(plain, rt)
    base = await _call(plain, "perps_funding_history", {})
    typo = await _call(plain, "perps_funding_history", {"symbol": "ETH"})
    _same_answer(typo, base)
    assert typo["warnings"] == base["warnings"] and not any(NOTE in w for w in typo["warnings"])


async def test_read_tool_unknown_key_warns_and_answers_as_before(server):
    base = await _call(server, "perps_funding_history", {})
    typo = await _call(server, "perps_funding_history", {"symbol": "ETH"})
    _same_answer(typo, base)
    assert typo["warnings"][1:] == base["warnings"]
    w = typo["warnings"][0]
    assert w.startswith(NOTE) and "'symbol'" in w and "defaults may have applied" in w
    assert "base, days_back, exchanges" in w  # the valid names, sorted


async def test_valid_call_gets_no_new_warning(server):
    env = await _call(server, "perps_funding_history", {"base": "BTC", "days_back": 30})
    assert env["ok"] and not any("unknown argument" in w for w in env["warnings"])


async def test_provider_failure_still_carries_the_warning(server, state):
    state.deribit_status = 503
    env = await _call(server, "deribit_vol_surface", {"underlying": "ETH"})
    assert env["ok"] is False and env["missing"][0]["provider"] == "deribit"
    assert env["warnings"][0].startswith(NOTE) and "'underlying'" in env["warnings"][0]


async def test_direct_in_process_call_is_covered_too(server):
    res = await server.call_tool("perps_funding_history", {"symbol": "ETH"})
    assert _env(res)["warnings"][0].startswith(NOTE)


async def test_rejected_value_is_still_the_sdk_error(server):
    """A wrong value next to an unknown key: the SDK's validation error, as before (no envelope to warn in)."""
    async with Client(server) as c:
        res = await c.call_tool("perps_oi_history", {"period": "bogus", "symbol": "ETH"})
    assert res.is_error and "Input should be" in res.content[0].text


async def test_check_uses_the_sdk_hooks_without_falling_back(server, caplog):
    """DeskServer reads the SDK's private tool registry; if it moves, the check logs and lets calls through unchecked,
    which the warning tests above would catch; this pins the hook itself."""
    assert isinstance(server, DeskServer)
    tool = server._tool_manager.get_tool("perps_funding_history")
    assert "base" in schema_params(tool.parameters) and callable(tool.fn_metadata.convert_result)
    with caplog.at_level(logging.WARNING, logger="tradedesk.server"):
        await _call(server, "perps_funding_history", {"symbol": "ETH"})
    assert not caplog.records


async def _store_rt(state, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / "args.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    await rt.store.init()
    return rt


async def test_write_tool_unknown_key_is_refused_and_writes_nothing(state, tmp_path):
    rt = await _store_rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        args = {"symbol": "BTC/USDT:USDT", "side": "long", "size": 1.0, "entry": 80000.0, "stop_price": 78000.0}
        env = await _call(srv, "journal_add", args)
        assert env["ok"] is False and env["data"] is None
        assert env["missing"][0]["reason"] == "invalid_input" and env["missing"][0]["field"] == "arguments"
        assert "'stop_price'" in env["warnings"][0] and "nothing was written" in env["warnings"][0]
        assert "entry, opened_ts, side, size, stop" in env["warnings"][0]
        assert (await _call(srv, "journal_list", {}))["data"]["count"] == 0
        # the same call with the right name writes, without a new warning
        args["stop"] = args.pop("stop_price")
        ok = await _call(srv, "journal_add", args)
        assert ok["ok"] and ok["data"]["id"] == 1 and not any("unknown argument" in w for w in ok["warnings"])
        listed = await _call(srv, "journal_list", {})
        assert listed["data"]["count"] == 1 and listed["data"]["trades"][0]["stop"] == 78000.0
    finally:
        await rt.aclose()


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mcp_call.py"


async def test_write_tools_exist_and_match_the_script_list(server):
    spec = importlib.util.spec_from_file_location("mcp_call", _SCRIPT)
    mc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc)
    assert STORE_WRITE_TOOLS == mc.WRITE_TOOLS
    assert STORE_WRITE_TOOLS <= {t.name for t in await server.list_tools()}


def test_echoed_names_are_bounded_and_cleaned():
    assert shown_name("a\nb\x00c‮d\te") == "'abcde'"  # newline, NUL, bidi override, tab stripped
    assert shown_name("x" * 5000) == "'" + "x" * MAX_NAME_CHARS + "…'"
    assert shown_name(5) == "<int key>"
    text = shown_names([f"k{i}" for i in range(50)])
    assert text.count("'") == 2 * MAX_SHOWN_NAMES and text.endswith("and 40 more")
    assert schema_params({}) == [] and schema_params(None) == []


async def test_hostile_names_reach_the_warning_cleaned(server):
    args = {f"evil\n{i}‮" + "y" * 300: 1 for i in range(30)}
    env = await _call(server, "odds_devig", {"odds": [1.9, 1.9], **args})
    assert env["ok"] and env["data"]["fair_probs"] == [0.5, 0.5]
    w = env["warnings"][0]
    assert "\n" not in w and "‮" not in w and "and 20 more" in w and len(w) < 1200
