"""Multi-leg strategy payoff + greeks + probability of profit."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.strategy import summarise
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Multi-leg strategy payoff. Each leg is a dict with: kind ('call' | 'put' | "
            "'linear'), side ('long' | 'short'), size (contracts, > 0), strike (for options), "
            "entry_premium (option premium in USD), entry (for 'linear' legs). Returns the "
            "payoff curve on a grid around the forward, breakeven prices, max profit / loss on "
            "that grid, Black-76 net greeks at the current forward, mark-to-market value, and "
            "the RISK-NEUTRAL probability of profit under one flat vol — a hint, not a forecast."
        ),
    )
    async def strategy_payoff(
        legs: Annotated[
            list[dict[str, Any]],
            Field(
                min_length=1,
                description="List of leg dicts: {kind, side, size, strike?, entry_premium?, entry?}.",
            ),
        ],
        spot: Annotated[float, Field(gt=0, description="Underlying spot used to anchor the payoff grid.")],
        days: Annotated[float, Field(gt=0, description="Calendar days until expiry (used for greeks + PoP).")],
        iv: Annotated[float, Field(gt=0, description="Flat implied vol (decimal, 0.45 = 45 %) for greeks + PoP.")],
        forward: Annotated[float | None, Field(gt=0, description="Forward price; defaults to spot when omitted.")] = None,
        width_pct: Annotated[float, Field(gt=0, le=2.0, description="Grid half-width around the forward.")] = 0.3,
        grid: Annotated[int, Field(ge=11, le=1001, description="Grid resolution.")] = 201,
        kind: Annotated[Literal["detailed", "summary"], Field(description="'summary' omits the full curve.")] = "detailed",
    ) -> dict[str, Any]:
        b = Builder()
        F = float(forward) if forward is not None else float(spot)
        try:
            res = summarise(list(legs), forward=F, days=float(days), iv=float(iv), width_pct=float(width_pct), grid=int(grid))
        except (KeyError, TypeError, ValueError) as e:
            b.miss("legs", "tradedesk", "data_unavailable", f"bad leg definition: {type(e).__name__}: {e}")
            return b.build(None, ok=False)

        data: dict[str, Any] = {
            "forward": F,
            "spot": spot,
            "days": days,
            "iv": iv,
            "breakevens": [r(x, 2) for x in res["breakevens"]],
            "max_profit_usd": r(res["max_profit"], 2),
            "max_loss_usd": r(res["max_loss"], 2),
            "mtm_usd": r(res["mtm_usd"], 2),
            "pop": r(res["pop"], 4) if res["pop"] is not None else None,
            "greeks": {k: r(v, 6) for k, v in res["greeks"].items()},
            "measure": "risk-neutral",
        }
        if kind == "detailed":
            data["grid"] = res["grid"]
            data["prices"] = [r(x, 2) for x in res["prices"]]
            data["pnl"] = [r(x, 2) for x in res["pnl"]]
        return b.build(data)
