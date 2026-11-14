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
    assert "in-process server" in capsys.readouterr().err


def test_write_tools_exist_on_the_server():
    from tradedesk.server import build_server

    server, _ = build_server()
    names = {t.name for t in __import__("asyncio").run(server.list_tools())}
    assert mc.WRITE_TOOLS <= names  # a renamed tool must not slip out of the refusal list


def test_agents_launcher_denies_the_same_write_tools():
    import re

    sh = (Path(__file__).resolve().parent.parent / "scripts" / "nightly_agents.sh").read_text(encoding="utf-8")
    denied = set(re.findall(r'"mcp__tradedesk__([a-z_]+)"', sh))
    assert denied == set(mc.WRITE_TOOLS)
