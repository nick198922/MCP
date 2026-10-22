from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..runtime import Runtime


def register_all(server: MCPServer, rt: Runtime) -> None:
    from .. import prompts
    from . import (
        assistant_paper,
        backtest,
        calendar,
        carry_paper,
        coinalyze,
        coingecko,
        events,
        forecast_log,
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
        pm_gap,
        polymarket,
        positions,
        quant,
        rules,
        sentiment,
        strategy,
        system,
        trade_plan,
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
        pm_gap,
        coingecko,
        coinalyze,
        history,
        journal,
        carry_paper,
        paper_carry,
        assistant_paper,
        forecast_log,
        trade_plan,
        positions,
        liquidations,
        onchain,
        pendle,
        rules,
        strategy,
        backtest,
        events,
        prompts,
    ):
        module.register(server, rt)
