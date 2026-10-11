"""Backtest MCP tools.

Phase 3.5 MVP: one momentum-breakout backtest over ccxt OHLCV. Lets you
sanity-check an idea on a single instrument before touching the full strategy
machinery. The tool is explicit about the limitations in its description.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.backtest import run_backtest, signals_momentum
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r, timeframe_seconds


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Walk-forward momentum backtest over ccxt OHLCV. A `+1` position opens at the next "
            "bar's open when `close/close[-lookback] - 1 >= threshold_pct/100`; `-1` on the "
            "mirror condition; `0` otherwise. Fees + slippage are charged on every position "
            "change (in bps of notional, so a long→short flip pays twice). Returns the equity "
            "curve, per-bar returns, individual trades and summary stats (Sharpe, Sortino, max "
            "drawdown, win rate). NOT a strategy proposal — a sanity check. No funding, no "
            "borrow, one instrument, no multi-asset netting."
        ),
    )
    async def strategy_backtest_momentum(
        exchange: Annotated[str, Field(description="ccxt exchange id, e.g. 'binanceusdm'.")],
        symbol: Annotated[str, Field(description="ccxt unified symbol, e.g. 'BTC/USDT:USDT'.")],
        timeframe: Annotated[str, Field(description="OHLCV timeframe, e.g. '1h', '4h', '1d'.")] = "1h",
        limit: Annotated[int, Field(ge=50, le=5000, description="How many bars to pull.")] = 500,
        lookback: Annotated[int, Field(ge=2, le=500, description="Lookback bars for the breakout.")] = 24,
        threshold_pct: Annotated[float, Field(gt=0, le=20, description="Breakout trigger (percent).")] = 2.0,
        fee_bps: Annotated[float, Field(ge=0, le=100)] = 5.0,
        slippage_bps: Annotated[float, Field(ge=0, le=100)] = 2.0,
        include_curve: bool = False,
    ) -> dict[str, Any]:
        b = Builder()
        bars = await b.guard(f"ohlcv:{exchange}", f"ccxt:{exchange}", rt.exchanges.ohlcv(exchange, symbol, timeframe, limit))
        if not bars or len(bars) < max(lookback + 5, 50):
            b.miss("ohlcv", f"ccxt:{exchange}", "data_unavailable", f"only {0 if not bars else len(bars)} bars available")
            return b.build(None, ok=False)
        positions = signals_momentum(bars, lookback=lookback, threshold_pct=threshold_pct)
        tf_sec = timeframe_seconds(timeframe) or 3600
        bars_per_year = (365 * 24 * 3600) / tf_sec
        res = run_backtest(
            bars,
            positions,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            bars_per_year=bars_per_year,
        )
        stats = res["stats"]
        if stats.get("count", 0) < 100:
            b.warn(f"only {stats['count']} bars after warm-up; results are noisy, treat as a sanity check")
        trades = [
            {
                "side": t["side"],
                "entry_ts": t["entry_ts"],
                "exit_ts": t["exit_ts"],
                "entry_price": r(t["entry_price"], 6),
                "exit_price": r(t["exit_price"], 6),
                "return_pct": r(t["return_pct"] * 100, 3),
            }
            for t in res["trades"]
        ]
        data: dict[str, Any] = {
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "bars_used": len(bars),
            "params": {
                "lookback": lookback,
                "threshold_pct": threshold_pct,
                "fee_bps": fee_bps,
                "slippage_bps": slippage_bps,
            },
            "stats": {
                "num_trades": stats["num_trades"],
                "win_rate": r(stats["win_rate"], 4) if stats["win_rate"] is not None else None,
                "avg_trade_return": r(stats["avg_trade_return"], 6) if stats["avg_trade_return"] is not None else None,
                "total_return": r(stats["total_return"], 6),
                "annualized_return": r(stats["annualized_return"], 6) if stats["annualized_return"] is not None else None,
                "sharpe": r(stats["sharpe"], 3) if stats["sharpe"] is not None else None,
                "sortino": r(stats["sortino"], 3) if stats["sortino"] is not None else None,
                "max_drawdown": r(stats["max_drawdown"], 4),
                "best_bar": r(stats["best_bar"], 6),
                "worst_bar": r(stats["worst_bar"], 6),
            },
            "equity_final": r(res["equity_final"], 6),
            "trades": trades,
        }
        if include_curve:
            data["equity_curve"] = [r(v, 6) for v in res["equity_curve"]]
        return b.build(data)
