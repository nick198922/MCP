"""MCP prompts shipped with tradedesk — selectable from clients like Claude Desktop / Code."""

from __future__ import annotations

from importlib import resources
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..runtime import Runtime

PROJECT_PLACEHOLDER = "[НАЗВАНИЕ ПРОЕКТА / ТИКЕР]"
TRADEDESK_HINT = (
    "\n\n---\n\n"
    "**Инструменты tradedesk-mcp, которые могут помочь при анализе графика и позиционирования:**\n"
    "- `market_ohlcv`, `hyperliquid_candles` — цена + реализованная вола на разных ТФ.\n"
    "- `market_orderbook`, `hyperliquid_book` — глубина стакана и slippage под нужный размер.\n"
    "- `market_trades`, `hyperliquid_trades` — aggressor flow (buy/sell, VWAP, CVD).\n"
    "- `perps_funding_compare`, `perps_funding_history` — текущий и исторический funding "
    "(carry 7d/30d, z-score, percentile) по Binance / Bybit / OKX / Hyperliquid.\n"
    "- `perps_open_interest` — распределение OI между площадками.\n"
    "- `hyperliquid_overview` — скрининг всей вселенной HL по OI / funding / объёму.\n"
    "- Для BTC и ETH опционы: `deribit_vol_surface`, `deribit_option_chain`, `deribit_expected_move`, "
    "`deribit_oi_by_strike`, `deribit_gamma_exposure`, `prob_from_options`.\n"
)


def _render_token_deep_analysis(project: str) -> str:
    text = resources.files("tradedesk.prompts").joinpath("token_deep_analysis.md").read_text(encoding="utf-8")
    rendered = text.replace(PROJECT_PLACEHOLDER, project)
    return rendered + TRADEDESK_HINT


def register(server: MCPServer, rt: Runtime) -> None:
    @server.prompt(
        name="token_deep_analysis",
        title="Deep token / project investment analysis",
        description=(
            "10-section investment research template for a crypto project / token: product, funding, partnerships, tokenomics, "
            "unlocks, revenue, value accrual, roadmap, narrative fit, chart. Finishes with bull / base / bear cases, numeric scores "
            "and a short/mid/long-term outlook. Russian text. Use for serious due diligence; it will reach for tradedesk-mcp tools "
            "where live market data helps (chart, funding, OI, options)."
        ),
    )
    async def token_deep_analysis(
        project: Annotated[str, Field(description="Project name or ticker, e.g. 'Hyperliquid (HYPE)' or 'SOL'")],
    ) -> str:
        return _render_token_deep_analysis(project)
