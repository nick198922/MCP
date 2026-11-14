"""Call one tool of the tradedesk MCP server from the shell and print its envelope as JSON (owner 14.11: the agents
must use our MCP, not only know about it).

    uv run python scripts/mcp_call.py --list                                   # tool names and first description lines
    uv run python scripts/mcp_call.py deribit_vrp                              # default arguments
    uv run python scripts/mcp_call.py skew_percentile '{"currency": "ETH", "delta_target": "25"}'
    uv run python scripts/mcp_call.py deribit_expected_move '{"max_days": 40}' --max-chars 4000

It talks to the running MCP service (TRADEDESK_MCP_URL, default http://127.0.0.1:8000/mcp: the server's live database,
so iv_rank, skew_percentile and other tools that read the collector's history work); when the service does not answer,
or with --local, the server is built in this process (``build_server()``, the clone's own store) and a note says so.
Tools that write to the store (WRITE_TOOLS: journal, plans, rules, events, paper books) are refused: the agents only
read (owner 14.11, Q-20261114-1 «Г1»). For a research result the numbers still come from a script under
``scripts/verify_*.py`` (reproducible); this is for live checks: what the market looks like now, whether a condition
of a rule holds, whether a new tool answers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

from mcp import Client

from tradedesk.server import build_server

URL = os.environ.get("TRADEDESK_MCP_URL", "http://127.0.0.1:8000/mcp")
# Tools that write to the local store (no orders anywhere): never through this script (owner 14.11, Q-20261114-1 «Г1»).
WRITE_TOOLS = frozenset(
    {
        "journal_add",
        "journal_close",
        "positions_add",
        "positions_close",
        "rules_add",
        "rules_disable",
        "plan_add",
        "plan_link",
        "events_add",
        "events_seed_load",
        "rules_presets_load",
        "carry_paper_open",
        "carry_paper_close",
        "assistant_paper_open",
    }
)


def _data(res: Any) -> Any:
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


async def _session(c: Client, tool: str | None, args: dict[str, Any], list_only: bool) -> tuple[int, str]:
    tools = (await c.list_tools()).tools
    if list_only:
        return 0, "\n".join(
            f"{t.name}{' [writes: refused here]' if t.name in WRITE_TOOLS else ''}: {(t.description or '').splitlines()[0][:150]}"
            for t in sorted(tools, key=lambda t: t.name)
        )
    names = {t.name for t in tools}
    if tool not in names:
        near = sorted(n for n in names if tool and (tool in n or n in tool))
        return 2, f"no tool '{tool}'" + (f"; similar: {', '.join(near)}" if near else "; see --list")
    res = await c.call_tool(tool, args)
    return 0, json.dumps(_data(res), ensure_ascii=False, indent=1, default=str)


async def run(tool: str | None, args: dict[str, Any], list_only: bool, local: bool = False, url: str = URL) -> tuple[int, str]:
    if tool in WRITE_TOOLS:
        return 3, f"'{tool}' writes to the store: refused (agents read only, Q-20261114-1)"
    if not local:
        try:
            async with Client(url) as c:
                return await _session(c, tool, args, list_only)
        except Exception as e:  # noqa: BLE001 - fall back to an in-process server
            print(
                f"[the MCP service at {url} did not answer ({type(e).__name__}); using an in-process server with this clone's store]",
                file=sys.stderr,
            )
    server, rt = build_server()
    try:
        async with Client(server) as c:
            return await _session(c, tool, args, list_only)
    finally:
        await rt.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tool", nargs="?", help="tool name, e.g. deribit_vrp")
    ap.add_argument("args", nargs="?", default="{}", help="arguments as a JSON object (default {})")
    ap.add_argument("--list", action="store_true", help="list the tools")
    ap.add_argument("--max-chars", type=int, default=20_000, help="cut the printed JSON (default 20000)")
    ap.add_argument("--local", action="store_true", help="build the server in this process instead of calling the service")
    a = ap.parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep stdout/stderr to the tool's answer
    if not a.list and not a.tool:
        ap.error("a tool name or --list")
    try:
        args = json.loads(a.args)
        if not isinstance(args, dict):
            raise ValueError("not an object")
    except ValueError as e:
        print(f"arguments must be a JSON object: {e}", file=sys.stderr)
        return 2
    code, out = asyncio.run(run(a.tool, args, a.list, a.local))
    print(out if len(out) <= a.max_chars else out[: a.max_chars] + f"\n… cut at {a.max_chars} of {len(out)} chars (--max-chars)")
    return code


if __name__ == "__main__":
    sys.exit(main())
