"""scripts/mcp_call.py: the agents' shell entry to the MCP tools (no network: only pure tools are called)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mcp_call.py"
_spec = importlib.util.spec_from_file_location("mcp_call", _SCRIPT)
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)


def test_list_has_the_tools(capsys):
    assert mc.main(["--list", "--local"]) == 0
    out = capsys.readouterr().out
    assert "deribit_vrp:" in out and "odds_devig:" in out and "skew_percentile:" in out


def test_calls_a_tool_and_prints_the_envelope(capsys):
    assert mc.main(["odds_devig", json.dumps({"odds": [1.9, 1.9]}), "--local"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is True and env["data"]["fair_probs"] == [0.5, 0.5]


def test_unknown_tool_suggests_names(capsys):
    assert mc.main(["dvol", "--local"]) == 2
    assert "deribit_dvol" in capsys.readouterr().out


def test_bad_arguments(capsys):
    assert mc.main(["odds_devig", "[1, 2]"]) == 2
    assert mc.main(["odds_devig", "{not json"]) == 2


def test_output_is_cut(capsys):
    assert mc.main(["odds_devig", json.dumps({"odds": [1.9, 1.9]}), "--max-chars", "20", "--local"]) == 0
    assert "cut at 20" in capsys.readouterr().out


def test_write_tools_are_refused_before_any_connection(capsys):
    assert mc.main(["journal_add", "{}"]) == 3
    assert "refused" in capsys.readouterr().out
    assert {"plan_add", "rules_disable", "assistant_paper_open"} <= mc.WRITE_TOOLS


def test_unreachable_service_falls_back_to_local(capsys):
    code, out = __import__("asyncio").run(mc.run("odds_devig", {"odds": [2.0, 2.0]}, False, url="http://127.0.0.1:9/mcp"))
    assert code == 0 and json.loads(out)["ok"] is True
    err = capsys.readouterr().err
    assert "in-process server" in err and "ConnectError" in err


def _service_without_fallback(monkeypatch):
    """A connected 'service' (an in-process server passed as the URL); building a local server fails the test."""
    from tradedesk.server import build_server

    server, _ = build_server()

    def no_fallback():
        raise AssertionError("fell back to the in-process server after the service answered")

    monkeypatch.setattr(mc, "build_server", no_fallback)
    return server


def test_tool_rejecting_arguments_prints_the_error_without_fallback(monkeypatch, capsys):
    service = _service_without_fallback(monkeypatch)
    code, out = __import__("asyncio").run(mc.run("perps_oi_history", {"period": "bogus"}, False, url=service))
    assert code == 4 and "period" in out and "Input should be" in out
    assert "in-process server" not in capsys.readouterr().err


def test_tool_rejecting_arguments_local(capsys):
    assert mc.main(["perps_oi_history", json.dumps({"period": "bogus"}), "--local"]) == 4
    assert "Input should be" in capsys.readouterr().out


def test_failure_after_connecting_is_reported_without_fallback(monkeypatch, capsys):
    service = _service_without_fallback(monkeypatch)

    async def broken(self, name, arguments=None, *a, **k):
        raise RuntimeError("stream closed mid-call")

    monkeypatch.setattr(mc.Client, "call_tool", broken)
    code, out = __import__("asyncio").run(mc.run("odds_devig", {"odds": [2.0, 2.0]}, False, url=service))
    assert code == 5 and "RuntimeError: stream closed mid-call" in out
    assert "in-process server" not in capsys.readouterr().err


def test_plain_text_answer_is_printed(monkeypatch, capsys):
    import mcp_types

    async def plain(self, name, arguments=None, *a, **k):
        return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="not json")])

    monkeypatch.setattr(mc.Client, "call_tool", plain)
    assert mc.main(["odds_devig", "{}", "--local"]) == 0
    assert capsys.readouterr().out.strip() == "not json"


def test_write_tools_exist_on_the_server():
    from tradedesk.server import build_server

    server, _ = build_server()
    names = {t.name for t in __import__("asyncio").run(server.list_tools())}
    assert mc.WRITE_TOOLS <= names  # a renamed tool must not slip out of the refusal list


# Write tools removed from the MCP server on 2026-11-18 (docs/changes/2026-11-18-mcp-remove-six.md). The agents may not
# edit scripts/nightly_agents.sh, so its deny list may still name them until the owner drops them there (denying a tool
# that no longer exists is harmless). Only these names may be extra; once the owner removes them this still passes.
REMOVED_WRITE_TOOLS = frozenset({"events_seed_load", "rules_presets_load", "carry_paper_open", "carry_paper_close"})


def test_agents_launcher_denies_the_same_write_tools():
    import re

    sh = (Path(__file__).resolve().parent.parent / "scripts" / "nightly_agents.sh").read_text(encoding="utf-8")
    denied = set(re.findall(r'"mcp__tradedesk__([a-z_]+)"', sh))
    assert denied - REMOVED_WRITE_TOOLS == set(mc.WRITE_TOOLS)
    assert not REMOVED_WRITE_TOOLS & mc.WRITE_TOOLS


def test_removed_write_tools_are_not_on_the_server():
    from tradedesk.server import build_server

    server, _ = build_server()
    names = {t.name for t in __import__("asyncio").run(server.list_tools())}
    assert not REMOVED_WRITE_TOOLS & names
