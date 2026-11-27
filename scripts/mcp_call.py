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

Only a service that cannot be reached (connect or handshake fails) leads to the in-process server; once connected,
any failure is reported as it is and nothing is called a second time. Exit codes: 0 the answer is printed;
2 bad usage, arguments not a JSON object, no such tool, or an argument name the tool does not have (checked against
its inputSchema before calling: the server would drop it and run with defaults); 3 a write tool, refused; 4 the tool answered with an
error (e.g. it rejected the arguments), its text is printed; 5 the call failed after connecting (transport or
protocol error, unreadable response) or the in-process server did not start.
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
from tradedesk.unknown_args import schema_params, shown_names, unknown_keys, valid_text

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
        "assistant_paper_open",
    }
)


class _NoAnswer(Exception):
    """The server could not be reached: the connection or the handshake failed, nothing was called."""


def _why(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:  # the SDK's task groups wrap the cause
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _text(res: Any) -> str:
    return "\n".join(t for t in (getattr(x, "text", None) for x in res.content) if t is not None)


def _render(res: Any) -> str:
    data = res.structured_content
    if data is None:
        text = _text(res)
        try:
            data = json.loads(text)
        except ValueError:
            return text  # a plain-text answer is printed as it is
    data = data["result"] if isinstance(data, dict) and set(data) == {"result"} else data
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


async def _session(c: Client, tool: str | None, args: dict[str, Any], list_only: bool) -> tuple[int, str]:
    tools = (await c.list_tools()).tools
    if list_only:
        return 0, "\n".join(
            f"{t.name}{' [writes: refused here]' if t.name in WRITE_TOOLS else ''}: {(t.description or '').splitlines()[0][:150]}"
            for t in sorted(tools, key=lambda t: t.name)
        )
    by_name = {t.name: t for t in tools}
    if tool not in by_name:
        near = sorted(n for n in by_name if tool and (tool in n or n in tool))
        return 2, f"no tool '{tool}'" + (f"; similar: {', '.join(near)}" if near else "; see --list")
    params = schema_params(by_name[tool].input_schema)
    unknown = unknown_keys(args, params)
    if unknown:  # the SDK would drop them silently and run with defaults (backlog 27.11): not called
        return 2, f"'{tool}' has no argument(s) {shown_names(unknown)}; not called. Valid parameters: {valid_text(params)}"
    res = await c.call_tool(tool, args)
    if res.is_error:
        return 4, _text(res) or f"'{tool}' returned an error without text"
    return 0, _render(res)


async def _over(target: Any, tool: str | None, args: dict[str, Any], list_only: bool) -> tuple[int, str]:
    """One session with `target`. Raises _NoAnswer only when it could not be entered (connect or handshake failed);
    a failure after that is an answer (exit 5): the SDK re-raises it from the session's exit, wrapped in a group."""
    entered = False
    result: tuple[int, str] | None = None
    try:
        async with Client(target) as c:
            entered = True
            result = await _session(c, tool, args, list_only)
    except Exception as e:  # noqa: BLE001 - reported, never raised out of the script
        if result is not None:  # the answer is complete; only closing the session failed
            print(f"[closing the MCP session failed ({_why(e)}); the answer is complete]", file=sys.stderr)
            return result
        if not entered:
            raise _NoAnswer(_why(e)) from e
        return 5, f"the call failed after connecting to the MCP server: {_why(e)}"
    assert result is not None
    return result


async def run(tool: str | None, args: dict[str, Any], list_only: bool, local: bool = False, url: str = URL) -> tuple[int, str]:
    if tool in WRITE_TOOLS:
        return 3, f"'{tool}' writes to the store: refused (agents read only, Q-20261114-1)"
    if not local:
        try:
            return await _over(url, tool, args, list_only)
        except _NoAnswer as e:
            print(f"[the MCP service at {url} did not answer ({e}); using an in-process server with this clone's store]", file=sys.stderr)
    server, rt = build_server()
    try:
        return await _over(server, tool, args, list_only)
    except _NoAnswer as e:
        return 5, f"the in-process MCP server did not start: {e}"
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
