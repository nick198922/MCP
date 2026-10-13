"""Render an agent-run transcript (coordinator + subagents) from Claude Code's JSONL logs into readable markdown.

Usage:
    uv run python scripts/agents_transcript.py                 # latest run of the agents' worktree
    uv run python scripts/agents_transcript.py <session-id>    # a specific run
    uv run python scripts/agents_transcript.py --out run.md

Shows what the coordinator said and did, each brief it gave a subagent, what that subagent did
(searches, fetches, files, commands) and the report it returned. Model reasoning is not shown.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_PROJECT = "-Users-nick-MCP-tradedesk-agents"
NAMES = {"scout": "скаут", "quant": "квант", "builder": "розробник", "critic": "критик"}


def _load(path: Path) -> list[dict[str, Any]]:
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return [e for e in out if e.get("type") in ("user", "assistant") and isinstance(e.get("message"), dict)]


def _time(e: dict[str, Any]) -> str:
    ts = e.get("timestamp")
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except (AttributeError, ValueError):
        return "--:--"


def _short(text: Any, limit: int) -> str:
    s = str(text).strip()
    return s if len(s) <= limit else s[:limit].rstrip() + " …"


def _action(block: dict[str, Any]) -> str:
    name, inp = block.get("name"), block.get("input") or {}
    if name == "Bash":
        return f"`{_short(inp.get('command', ''), 160)}`"
    if name in ("Read", "Write", "Edit"):
        return f"{name} `{inp.get('file_path', '')}`"
    if name == "WebSearch":
        return f"пошук: «{_short(inp.get('query', ''), 160)}»"
    if name == "WebFetch":
        return f"завантажив {inp.get('url', '')}"
    if name in ("Glob", "Grep"):
        return f"{name} `{_short(inp.get('pattern', ''), 100)}`"
    return f"{name}"


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _render_events(events: list[dict[str, Any]], who: str, lines: list[str], *, skip_first_user: bool) -> None:
    first_user = True
    for e in events:
        content = e["message"].get("content")
        if e["type"] == "user":
            if isinstance(content, str):
                if first_user and skip_first_user:
                    first_user = False
                    continue
                first_user = False
                continue
            for b in content or []:
                if b.get("type") == "tool_result" and b.get("is_error"):
                    lines.append(f"  - помилка інструмента: {_short(_result_text(b.get('content')), 300)}")
            continue
        for b in content or []:
            if b.get("type") == "text" and b.get("text", "").strip():
                lines.append(f"\n**{_time(e)} {who}:** {b['text'].strip()}\n")
            elif b.get("type") == "tool_use" and b.get("name") != "Agent":
                lines.append(f"  - {_time(e)} {_action(b)}")


def render(session: Path) -> str:
    main = _load(session.with_suffix(".jsonl"))
    sub_dir = session / "subagents"
    subs: dict[str, tuple[str, Path]] = {}
    for meta in sub_dir.glob("*.meta.json") if sub_dir.exists() else []:
        m = json.loads(meta.read_text())
        subs[m.get("toolUseId", "")] = (m.get("agentType", "агент"), meta.with_name(meta.name.replace(".meta.json", ".jsonl")))
    results = {
        b.get("tool_use_id"): _result_text(b.get("content"))
        for e in main
        if e["type"] == "user" and isinstance(e["message"].get("content"), list)
        for b in e["message"]["content"]
        if b.get("type") == "tool_result"
    }
    start = _time(main[0]) if main else "--:--"
    end = _time(main[-1]) if main else "--:--"
    lines = [f"# Прогін агентів {session.name[:8]} ({start}–{end})\n"]
    chunk: list[dict[str, Any]] = []

    def flush() -> None:
        _render_events(chunk, "координатор", lines, skip_first_user=True)
        chunk.clear()

    for e in main:
        content = e["message"].get("content")
        agent_calls = [b for b in content or [] if isinstance(content, list) and b.get("type") == "tool_use" and b.get("name") == "Agent"]
        chunk.append(e)
        for call in agent_calls:
            flush()
            inp = call.get("input") or {}
            kind = inp.get("subagent_type") or subs.get(call.get("id"), ("агент",))[0]
            name = NAMES.get(kind, kind)
            lines.append(f"\n## {_time(e)} Координатор → {name}: {inp.get('description', '')}\n")
            lines.append("**Доручення:**\n")
            lines.append("> " + _short(inp.get("prompt", ""), 4000).replace("\n", "\n> ") + "\n")
            if call.get("id") in subs and subs[call["id"]][1].exists():
                lines.append(f"\n### Робота: {name}\n")
                _render_events(_load(subs[call["id"]][1]), name, lines, skip_first_user=True)
            report = results.get(call.get("id"))
            lines.append(f"\n### Звіт {name} координатору\n")
            lines.append(report.strip() + "\n" if report else "_звіту немає (агент ще працює або прогін перервано)_\n")
    flush()
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", nargs="?", help="session id; default: the latest run")
    ap.add_argument("--project", default=DEFAULT_PROJECT, help="folder under ~/.claude/projects")
    ap.add_argument("--out", default="", help="write markdown here instead of stdout")
    a = ap.parse_args()
    folder = PROJECTS / a.project
    if a.session:
        session = folder / a.session
    else:
        runs = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not runs:
            raise SystemExit(f"no transcripts in {folder}")
        session = runs[-1].with_suffix("")
    text = render(session)
    if a.out:
        Path(a.out).write_text(text + "\n")
    else:
        print(text)


if __name__ == "__main__":
    main()
