"""Call one tool of the tradedesk MCP server from the shell and print its envelope as JSON (owner 14.11: the agents
must use our MCP, not only know about it).

    uv run python scripts/mcp_call.py --list                                   # tool names and first description lines
    uv run python scripts/mcp_call.py deribit_vrp                              # default arguments
    uv run python scripts/mcp_call.py skew_percentile '{"currency": "ETH", "delta_target": "25"}'
    uv run python scripts/mcp_call.py deribit_expected_move '{"max_days": 40}' --max-chars 4000

The server is built in this process (``build_server()``, the same code and providers as the running service, with
the store on the server's database when TRADEDESK_DB_PATH points there), so the numbers are exactly what the MCP tools
return in the chat. Read-only like the server. For a research result the numbers still come from a script under
``scripts/verify_*.py`` (reproducible); this is for live checks: what the market looks like now, whether a condition
of a rule holds, whether a new tool answers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from mcp import Client

from tradedesk.server import build_server


def _data(res: Any) -> Any:
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


async def run(tool: str | None, args: dict[str, Any], list_only: bool) -> tuple[int, str]:
    server, rt = build_server()
    try:
        async with Client(server) as c:
            if list_only:
                tools = (await c.list_tools()).tools
                return 0, "\n".join(f"{t.name}: {(t.description or '').splitlines()[0][:150]}" for t in sorted(tools, key=lambda t: t.name))
            names = {t.name for t in (await c.list_tools()).tools}
            if tool not in names:
                near = sorted(n for n in names if tool and (tool in n or n in tool))
                return 2, f"no tool '{tool}'" + (f"; similar: {', '.join(near)}" if near else "; see --list")
            res = await c.call_tool(tool, args)
            return 0, json.dumps(_data(res), ensure_ascii=False, indent=1, default=str)
    finally:
        await rt.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tool", nargs="?", help="tool name, e.g. deribit_vrp")
    ap.add_argument("args", nargs="?", default="{}", help="arguments as a JSON object (default {})")
    ap.add_argument("--list", action="store_true", help="list the tools")
    ap.add_argument("--max-chars", type=int, default=20_000, help="cut the printed JSON (default 20000)")
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
    code, out = asyncio.run(run(a.tool, args, a.list))
    print(out if len(out) <= a.max_chars else out[: a.max_chars] + f"\n… cut at {a.max_chars} of {len(out)} chars (--max-chars)")
    return code


if __name__ == "__main__":
    sys.exit(main())
