from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..runtime import Runtime


def register_all(server: MCPServer, rt: Runtime) -> None:
    from .. import prompts
    from . import market, options, perps, quant, system

    for module in (system, market, perps, options, quant, prompts):
        module.register(server, rt)
