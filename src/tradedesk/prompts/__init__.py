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


def _render_template(filename: str, replacements: dict[str, str]) -> str:
    text = resources.files("tradedesk.prompts").joinpath(filename).read_text(encoding="utf-8")
    for k, v in replacements.items():
        text = text.replace("{{" + k + "}}", v)
    return text


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

    @server.prompt(
        name="fomc_playbook",
        title="FOMC event playbook (DoubleTop-style)",
        description=(
            "Structured FOMC trade plan for a crypto perp/option: event-study on past FOMC days, "
            "retracement ladder, multi-horizon scenario matrix, exit ladder, risks. Pulls event_study / "
            "strategy_scenario_matrix / fib_retracement / touch_probability / market_regime. "
            "Pattern mirrors the DoubleTop FOMC write-up — descriptive stats + prebuilt levels, "
            "not a forecast."
        ),
    )
    async def fomc_playbook(
        symbol: Annotated[str, Field(description="ccxt symbol, e.g. 'BTC/USDT:USDT'")] = "BTC/USDT:USDT",
        venue: Annotated[str, Field(description="ccxt venue id")] = "binanceusdm",
        timeframe: Annotated[str, Field(description="OHLCV timeframe for the study")] = "1h",
        event_date: Annotated[str, Field(description="Event date YYYY-MM-DD UTC (upcoming or past)")] = "",
        event_time_utc: Annotated[str, Field(description="Event time HH:MM UTC")] = "19:00",
        time_stop: Annotated[str, Field(description="Time-based stop expression, e.g. 'Fri 21:00 UTC'")] = "T+48h",
        regime_change_date: Annotated[str, Field(description="Date beyond which regime looks different")] = "2024-01-01",
    ) -> str:
        return _render_template(
            "fomc_playbook.md",
            {
                "SYMBOL": symbol,
                "VENUE": venue,
                "TIMEFRAME": timeframe,
                "EVENT_DATE": event_date or "<fill in>",
                "EVENT_TIME_UTC": event_time_utc,
                "TIME_STOP": time_stop,
                "REGIME_CHANGE_DATE": regime_change_date,
            },
        )

    @server.prompt(
        name="pre_trade_checklist",
        title="Trade plan / pre-trade checklist",
        description=(
            "Trade plan before opening a position, in Ukrainian: honest frame (no entry-timing edge is verified), thesis and "
            "source, regime, derivatives positioning, volatility vs stop distance, calendar (defined-risk alternatives on "
            "macro days: options or Polymarket vs touch_probability), position size from account and risk %, execution, "
            "exit plan, verdict (enter / wait for a trigger with an alert / skip), journal entry with tags for later review. "
            "Each step names the tradedesk-mcp tool to call. Places no orders."
        ),
    )
    async def pre_trade_checklist(
        symbol: Annotated[str, Field(description="Instrument you're about to trade")] = "BTC/USDT:USDT",
        side: Annotated[str, Field(description="long / short / spread name")] = "long",
        size: Annotated[str, Field(description="Position size (e.g. '0.5 BTC' or '10 contracts')")] = "<size>",
        account: Annotated[str, Field(description="Account equity, e.g. '$10000'")] = "<рахунок>",
        risk_pct: Annotated[str, Field(description="Risk per trade in percent of the account, e.g. '1'")] = "1",
    ) -> str:
        return _render_template(
            "pre_trade_checklist.md",
            {"SYMBOL": symbol, "SIDE": side, "SIZE": size, "ACCOUNT": account, "RISK_PCT": risk_pct},
        )

    @server.prompt(
        name="journal_review",
        title="Monthly trade journal review",
        description=(
            "Review of the owner's trade journal in Ukrainian: win rate, mean and total R by tag (source, setup, confidence, "
            "event), discipline (trades without stops, losses beyond 1R), three conclusions and one rule to change next "
            "month; compares with the calls tracker. Uses journal_list / journal_stats; paper trades (pp-paper) kept apart."
        ),
    )
    async def journal_review(
        days: Annotated[str, Field(description="Look-back window in days")] = "30",
    ) -> str:
        return _render_template("journal_review.md", {"DAYS": days})

    @server.prompt(
        name="morning_briefing",
        title="Morning crypto-derivatives briefing",
        description=(
            "One-page morning briefing (price/regime, sentiment/funding, vol & skew, basis/carry, "
            "flow, 24h calendar, alerts, positions, journal). Each block names the tradedesk-mcp "
            "tool to call so the client fills in live numbers. No forecasts, facts only."
        ),
    )
    async def morning_briefing() -> str:
        return _render_template("morning_briefing.md", {})
