from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..runtime import Runtime


def register_all(server: MCPServer, rt: Runtime) -> None:
    from .. import prompts
    from . import calendar, coingecko, history, journal, market, options, perps, polymarket, quant, sentiment, system

    for module in (system, market, perps, options, quant, sentiment, calendar, polymarket, coingecko, history, journal, prompts):
        module.register(server, rt)
