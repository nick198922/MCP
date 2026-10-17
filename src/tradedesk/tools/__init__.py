from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..runtime import Runtime


def register_all(server: MCPServer, rt: Runtime) -> None:
    from .. import prompts
    from . import (
        backtest,
        calendar,
        carry_paper,
        coinalyze,
        coingecko,
        events,
        history,
        journal,
        liquidations,
        market,
        markup,
        onchain,
        options,
        paper_carry,
        perps,
        polymarket,
        positions,
        quant,
        rules,
        sentiment,
        strategy,
        system,
    )

    for module in (
        system,
        market,
        markup,
        perps,
        options,
        quant,
        sentiment,
        calendar,
        polymarket,
        coingecko,
        coinalyze,
        history,
        journal,
        carry_paper,
        paper_carry,
        positions,
        liquidations,
        onchain,
        rules,
        strategy,
        backtest,
        events,
        prompts,
    ):
        module.register(server, rt)
