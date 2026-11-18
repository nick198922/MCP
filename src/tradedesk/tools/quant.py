from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics import odds as odds_mod
from ..analytics import probability
from ..analytics.options import black76_greeks, black76_price
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r, reject_nonfinite, reject_nonfinite_result


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
        method: Annotated[
            Literal["proportional", "power"],
            Field(
                description="De-vig method: 'proportional' divides each implied probability by the overround; "
                "'power' solves sum(p_i^k) = 1 and takes more margin off long shots."
            ),
        ] = "proportional",
    ) -> dict[str, Any]:
        b = Builder()
        if (bad := reject_nonfinite(b, odds=odds)) is not None:
            return bad
        try:
            data = odds_mod.devig(odds, method)
        except ValueError as e:
            b.miss("odds", "tradedesk", "config", str(e))
            return b.build(None, ok=False)
        except (ZeroDivisionError, OverflowError) as e:  # power de-vig can underflow a huge long shot's p**k to 0
            b.miss("odds", "tradedesk", "invalid_input", f"odds are outside the de-vig's numeric range: {type(e).__name__}: {e}")
            return b.build(None, ok=False)
        out = {k: ([r(x, 6) for x in v] if isinstance(v, list) else (r(v, 6) if isinstance(v, float) else v)) for k, v in data.items()}
        return reject_nonfinite_result(b, out) or b.build(out)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Black-76 option calculator (r = 0, like Deribit): price in USD and greeks for any forward, strike, days to expiry and IV. "
            "iv is a decimal (0.60 = 60 %). vega is per 1 vol point, theta per day."
        ),
    )
    async def option_calc(
        forward: Annotated[
            float, Field(gt=0, description="Forward (futures) price of the underlying for this expiry, USD; not spot (r = 0).")
        ],
        strike: Annotated[float, Field(gt=0, description="Strike price, USD.")],
        days: Annotated[float, Field(gt=0, description="Calendar days to expiry; may be fractional. T = days / 365.")],
        iv: Annotated[float, Field(gt=0, lt=5, description="Annualised implied volatility as a decimal (0.60 = 60 %), below 5.")],
        kind: Annotated[Literal["call", "put"], Field(description="Option type.")] = "call",
    ) -> dict[str, Any]:
        b = Builder()
        if (bad := reject_nonfinite(b, forward=forward, strike=strike, days=days, iv=iv)) is not None:
            return bad
        T, is_call = days / 365.0, kind == "call"
        try:
            g = black76_greeks(forward, strike, T, iv, is_call)
            price = black76_price(forward, strike, T, iv, is_call)
            data = {"price_usd": r(price, 4), "price_in_underlying": r(price / forward, 6), **{k: r(v, 6) for k, v in g.items()}}
        except (ValueError, ZeroDivisionError, OverflowError) as e:  # F / K under- or overflows for extreme finite inputs
            b.miss("option", "tradedesk", "invalid_input", f"inputs are outside the model's numeric range: {type(e).__name__}: {e}")
            return b.build(None, ok=False)
        return reject_nonfinite_result(b, data) or b.build(data, ok=True)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Probability that price touches `barrier` at any time within `days`, for a given IV (continuous monitoring, driftless, constant vol: a risk-neutral model value with r=0, not a forecast). "
            "Returns the closed-form value and a Monte Carlo cross-check. For options-implied odds on BTC/ETH use prob_from_options instead. "
            "Units: `iv` is an annualised decimal (0.60 = 60 %); T = days / 365; analytic and monte_carlo are probabilities as "
            "fractions 0-1 (0.25 = 25 %); one_sigma_move_pct = iv × sqrt(T) × 100, in percent."
        ),
    )
    async def touch_probability(
        spot: Annotated[float, Field(gt=0, description="Current price of the underlying, USD (treated as the forward, r = 0).")],
        barrier: Annotated[float, Field(gt=0, description="Price level to touch, USD; above or below spot (direction is inferred).")],
        days: Annotated[float, Field(gt=0, description="Calendar days in the window; may be fractional. T = days / 365.")],
        iv: Annotated[float, Field(gt=0, lt=5, description="Annualised vol as decimal")],
    ) -> dict[str, Any]:
        b = Builder()
        if (bad := reject_nonfinite(b, spot=spot, barrier=barrier, days=days, iv=iv)) is not None:
            return bad
        T = days / 365.0
        try:
            data = {
                "analytic": r(probability.prob_touch(spot, barrier, iv, T), 4),
                "monte_carlo": r(probability.mc_touch(spot, barrier, iv, T), 4),
                "one_sigma_move_pct": r(iv * T**0.5 * 100, 2),
            }
        # An absurd `days` (e.g. 1e12) overflows exp() in the MC barrier shift; a tiny `iv` (1e-300) divides by sigma^2 = 0.
        except (OverflowError, ValueError, ZeroDivisionError) as e:
            b.miss("touch", "tradedesk", "invalid_input", f"days={days} with iv={iv} is outside the model's numeric range: {e}")
            return b.build(None, ok=False)
        return reject_nonfinite_result(b, data) or b.build(data, ok=True)
