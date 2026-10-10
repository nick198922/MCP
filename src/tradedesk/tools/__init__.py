from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..runtime import Runtime


def register_all(server: MCPServer, rt: Runtime) -> None:
    from .. import prompts
    from . import calendar, coingecko, market, options, perps, polymarket, quant, sentiment, system

    for module in (system, market, perps, options, quant, sentiment, calendar, polymarket, coingecko, prompts):
        module.register(server, rt)
