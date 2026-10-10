"""MCP server entry point.

tradedesk-mcp                        # stdio (Claude Desktop / Claude Code, local)
TRADEDESK_TRANSPORT=streamable-http TRADEDESK_PORT=8000 tradedesk-mcp    # always-on server
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .runtime import Runtime
from .tools import register_all

INSTRUCTIONS = """\
Personal trading desk for crypto derivatives, options and quant tooling. Read-only market data.

Conventions: IV and rates are DECIMALS (0.45 = 45 %); funding is annualised as a simple fraction;
prices in USD unless a field says otherwise. Every response is an envelope {ok, data, evidence, missing, warnings, as_of}:
`evidence` shows the source and fetch time of each number, `missing` lists anything a provider could not supply and why.
Probabilities derived from options are risk-neutral, not forecasts. If results look empty, call system_health(probe=true).
Typical flow: deribit_vol_surface -> deribit_option_chain / prob_from_options; perps_funding_compare -> perps_open_interest;
deribit_futures_curve for basis.
"""


def build_server(rt: Runtime | None = None) -> tuple[MCPServer, Runtime]:
    rt = rt or Runtime()

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await rt.aclose()

    server = MCPServer("tradedesk", instructions=INSTRUCTIONS, version=__version__, lifespan=lifespan)
    register_all(server, rt)
    return server, rt


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server, rt = build_server()
    s = rt.settings
    kwargs: dict[str, Any] = {}
    if s.transport == "streamable-http":
        kwargs = {"host": s.host, "port": s.port}
    server.run(s.transport, **kwargs)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
