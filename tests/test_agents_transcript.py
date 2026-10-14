"""Refused tool calls in scripts/agents_transcript.py, for the doctor's daily check (no network)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agents_transcript as tr  # noqa: E402

DENIED = "Permission to use Bash has been denied because Claude Code is running in don't ask mode."


def turn(role, *blocks):
    return {"type": role, "message": {"content": list(blocks)}}


def use(i, cmd):
    return {"type": "tool_use", "id": i, "name": "Bash", "input": {"command": cmd}}


def result(i, text, error=True):
    return {"type": "tool_result", "tool_use_id": i, "is_error": error, "content": text}


EVENTS = [
    turn("assistant", use("a", "ps aux")),
    turn("user", result("a", DENIED)),
    turn("assistant", use("b", "make test")),
    turn("user", result("b", "1 failed", error=True)),  # failed, not refused
    turn("assistant", use("c", "ls ~/.cache/x")),
    turn("user", result("c", [{"type": "text", "text": DENIED}])),
]


def test_denied_calls_are_the_refused_ones_only():
    assert tr.denied_calls(EVENTS) == ["`ps aux`", "`ls ~/.cache/x`"]


def test_denied_report_counts_across_runs_and_subagents(tmp_path):
    (tmp_path / "s1" / "subagents").mkdir(parents=True)
    for path in (tmp_path / "s1.jsonl", tmp_path / "s1" / "subagents" / "agent-1.jsonl"):
        path.write_text("\n".join(json.dumps(e) for e in EVENTS[:2]) + "\n")
    assert tr.denied_report(tmp_path, 24) == "- 2× `ps aux`"
    assert tr.denied_report(tmp_path / "missing", 24) == "none"
