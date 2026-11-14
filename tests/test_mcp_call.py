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
    assert mc.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "deribit_vrp:" in out and "odds_devig:" in out and "skew_percentile:" in out


def test_calls_a_tool_and_prints_the_envelope(capsys):
    assert mc.main(["odds_devig", json.dumps({"odds": [1.9, 1.9]})]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is True and env["data"]["fair_probs"] == [0.5, 0.5]


def test_unknown_tool_suggests_names(capsys):
    assert mc.main(["dvol"]) == 2
    assert "deribit_dvol" in capsys.readouterr().out


def test_bad_arguments(capsys):
    assert mc.main(["odds_devig", "[1, 2]"]) == 2
    assert mc.main(["odds_devig", "{not json"]) == 2


def test_output_is_cut(capsys):
    assert mc.main(["odds_devig", json.dumps({"odds": [1.9, 1.9]}), "--max-chars", "20"]) == 0
    assert "cut at 20" in capsys.readouterr().out
