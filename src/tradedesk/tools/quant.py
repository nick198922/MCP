from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics import odds as odds_mod
from ..analytics import probability
from ..analytics.options import black76_greeks, black76_price
from ..analytics.retracement import summarise_retracement
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
        return b.build(
            {k: ([r(x, 6) for x in v] if isinstance(v, list) else (r(v, 6) if isinstance(v, float) else v)) for k, v in data.items()}
        )

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
        return b.build(
            {"price_usd": r(price, 4), "price_in_underlying": r(price / forward, 6), **{k: r(v, 6) for k, v in g.items()}}, ok=True
        )

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
        return b.build(
            {
                "analytic": r(probability.prob_touch(spot, barrier, iv, T), 4),
                "monte_carlo": r(probability.mc_touch(spot, barrier, iv, T), 4),
                "one_sigma_move_pct": r(iv * T**0.5 * 100, 2),
            },
            ok=True,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Fibonacci retracement ladder. Given a reference price `p0` (decision price before "
            "a reaction) and the reaction extreme `L` (lowest low in a drop, highest high in a "
            "rally), returns the standard 23.6 / 38.2 / 50 / 61.8 / 78.6 % ladder plus "
            "direction, absolute / percent move, and an `applicable` flag that is False when the "
            "move is below `min_drop_pct` (default 2 %) — a cheap guard against pulling a "
            "ladder off noise. Pure math; it does not fetch any data."
        ),
    )
    async def fib_retracement(
        p0: Annotated[float, Field(gt=0, description="Decision / pre-reaction reference price.")],
        l_point: Annotated[float, Field(gt=0, description="Reaction extreme (lowest low on a drop, highest high on a rally).")],
        levels: Annotated[
            list[float] | None, Field(description="Custom retracement levels (defaults to 0.236/0.382/0.5/0.618/0.786).")
        ] = None,
        min_drop_pct: Annotated[float, Field(ge=0, le=100, description="Noise-filter threshold on |P0 - L| / P0, in percent.")] = 2.0,
    ) -> dict[str, Any]:
        b = Builder()
        s = summarise_retracement(p0, l_point, levels=levels, min_drop_pct=min_drop_pct)
        if not s.get("applicable"):
            # Still return the full block so the caller can inspect why.
            return b.build(
                {
                    "p0": p0,
                    "l": l_point,
                    "diff_abs": r(s.get("diff_abs", 0.0), 6),
                    "diff_pct": r(s.get("diff_pct", 0.0), 4),
                    "direction": s.get("direction"),
                    "applicable": False,
                    "reason": s.get("reason") or f"|move| < {min_drop_pct}% noise floor",
                    "ladder": [{"level": lv["level"], "price": r(lv["price"], 6)} for lv in s.get("ladder", [])],
                },
                ok=True,
            )
        ladder = [
            {
                "level": lv["level"],
                "price": r(lv["price"], 6),
                "pct_of_p0": r((lv["price"] / p0 - 1.0) * 100.0, 4) if p0 > 0 else None,
            }
            for lv in s["ladder"]
        ]
        return b.build(
            {
                "p0": p0,
                "l": l_point,
                "diff_abs": r(s["diff_abs"], 6),
                "diff_pct": r(s["diff_pct"], 4),
                "direction": s["direction"],
                "applicable": True,
                "min_drop_pct": s["min_drop_pct"],
                "ladder": ladder,
            }
        )
