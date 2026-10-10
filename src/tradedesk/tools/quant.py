from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics import odds as odds_mod
from ..analytics import probability
from ..analytics.options import black76_greeks, black76_price
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Bookmaker maths. Given decimal odds for ALL mutually exclusive outcomes of one market, returns the margin (overround), "
            "raw implied probabilities and fair (de-vigged) probabilities/odds. method 'proportional' scales evenly; 'power' shrinks long "
            "shots more (usually closer to reality for outright markets). For a two-outcome prediction market pass odds = 1/price for each side."
        ),
    )
    async def odds_devig(
        odds: Annotated[list[float], Field(min_length=2, description="Decimal odds, each > 1")],
        method: Literal["proportional", "power"] = "proportional",
    ) -> dict[str, Any]:
        b = Builder()
        try:
            data = odds_mod.devig(odds, method)
        except ValueError as e:
            b.miss("odds", "tradedesk", "config", str(e))
            return b.build(None, ok=False)
        return b.build({k: ([r(x, 6) for x in v] if isinstance(v, list) else (r(v, 6) if isinstance(v, float) else v)) for k, v in data.items()})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Black-76 option calculator (r = 0, like Deribit): price in USD and greeks for any forward, strike, days to expiry and IV. "
            "iv is a decimal (0.60 = 60 %). vega is per 1 vol point, theta per day."
        ),
    )
    async def option_calc(
        forward: Annotated[float, Field(gt=0)],
        strike: Annotated[float, Field(gt=0)],
        days: Annotated[float, Field(gt=0)],
        iv: Annotated[float, Field(gt=0, lt=5)],
        kind: Literal["call", "put"] = "call",
    ) -> dict[str, Any]:
        b = Builder()
        T, is_call = days / 365.0, kind == "call"
        g = black76_greeks(forward, strike, T, iv, is_call)
        price = black76_price(forward, strike, T, iv, is_call)
        return b.build({"price_usd": r(price, 4), "price_in_underlying": r(price / forward, 6), **{k: r(v, 6) for k, v in g.items()}}, ok=True)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Probability that price touches `barrier` at any time within `days`, for a given IV (continuous monitoring, driftless, constant vol). "
            "Returns the closed-form value and a Monte Carlo cross-check. For options-implied odds on BTC/ETH use prob_from_options instead."
        ),
    )
    async def touch_probability(
        spot: Annotated[float, Field(gt=0)],
        barrier: Annotated[float, Field(gt=0)],
        days: Annotated[float, Field(gt=0)],
        iv: Annotated[float, Field(gt=0, lt=5, description="Annualised vol as decimal")],
    ) -> dict[str, Any]:
        b = Builder()
        T = days / 365.0
        return b.build({
            "analytic": r(probability.prob_touch(spot, barrier, iv, T), 4),
            "monte_carlo": r(probability.mc_touch(spot, barrier, iv, T), 4),
            "one_sigma_move_pct": r(iv * T**0.5 * 100, 2),
        }, ok=True)
