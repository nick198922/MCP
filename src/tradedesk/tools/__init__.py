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
        macro,
        market,
        markup,
        onchain,
        options,
        paper_carry,
        pendle,
        perps,
        polymarket,
        positions,
        quant,
        rules,
        sentiment,
        strategy,
        system,
        upbit,
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
        macro,
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
        pendle,
        rules,
        strategy,
        backtest,
        events,
        upbit,
        prompts,
    ):
        module.register(server, rt)
