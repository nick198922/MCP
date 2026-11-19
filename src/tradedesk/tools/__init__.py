from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..runtime import Runtime


def register_all(server: MCPServer, rt: Runtime) -> None:
    from .. import prompts
    from . import (
        assistant_paper,
        bitcoin,
        calendar,
        coinalyze,
        coingecko,
        cycle,
        events,
        evm,
        flow,
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
        flow,
        cycle,
        history,
        journal,
        paper_carry,
        assistant_paper,
        forecast_log,
        trade_plan,
        positions,
        liquidations,
        onchain,
        evm,
        bitcoin,
        pendle,
        rules,
        strategy,
        events,
        prompts,
    ):
        module.register(server, rt)
