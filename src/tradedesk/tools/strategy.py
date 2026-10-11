"""Multi-leg strategy payoff + greeks + probability of profit, plus a small
set of ready-made templates (straddle / strangle / risk-reversal / iron
condor / cash-and-carry) that generate the ``legs`` and run the same
summariser so the user sees payoff / greeks / PoP in one call."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.strategy import (
    build_cash_and_carry,
    build_funding_carry,
    build_iron_condor,
    build_risk_reversal,
    build_straddle,
    build_strangle,
    summarise,
)
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r


def _packed(
    legs: list[dict[str, Any]], *, spot: float, days: float, iv: float, forward: float | None, width_pct: float, grid: int, kind: str
) -> dict[str, Any]:
    F = float(forward) if forward is not None else float(spot)
    res = summarise(list(legs), forward=F, days=float(days), iv=float(iv), width_pct=float(width_pct), grid=int(grid))
    out: dict[str, Any] = {
        "legs": legs,
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
        out["grid"] = res["grid"]
        out["prices"] = [r(x, 2) for x in res["prices"]]
        out["pnl"] = [r(x, 2) for x in res["pnl"]]
    return out


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

    # -------- template builders --------

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Long or short straddle: call + put at the same strike. Long = long volatility, "
            "max loss = call_premium + put_premium at the strike, breakevens at strike ± premium. "
            "Returns the generated legs plus the full payoff/greeks/PoP block."
        ),
    )
    async def strategy_straddle(
        strike: Annotated[float, Field(gt=0)],
        call_premium: Annotated[float, Field(ge=0)],
        put_premium: Annotated[float, Field(ge=0)],
        spot: Annotated[float, Field(gt=0)],
        days: Annotated[float, Field(gt=0)],
        iv: Annotated[float, Field(gt=0)],
        side: Literal["long", "short"] = "long",
        size: Annotated[float, Field(gt=0)] = 1.0,
        forward: Annotated[float | None, Field(gt=0)] = None,
        width_pct: Annotated[float, Field(gt=0, le=2.0)] = 0.3,
        grid: Annotated[int, Field(ge=11, le=1001)] = 201,
        kind: Literal["detailed", "summary"] = "summary",
    ) -> dict[str, Any]:
        b = Builder()
        legs = build_straddle(strike=strike, call_premium=call_premium, put_premium=put_premium, side=side, size=size)
        return b.build(_packed(legs, spot=spot, days=days, iv=iv, forward=forward, width_pct=width_pct, grid=grid, kind=kind))

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Long or short strangle: OTM call + OTM put. Wider breakevens than a straddle for a "
            "lower premium — a cheaper way to buy or sell range. put_strike < call_strike."
        ),
    )
    async def strategy_strangle(
        call_strike: Annotated[float, Field(gt=0)],
        put_strike: Annotated[float, Field(gt=0)],
        call_premium: Annotated[float, Field(ge=0)],
        put_premium: Annotated[float, Field(ge=0)],
        spot: Annotated[float, Field(gt=0)],
        days: Annotated[float, Field(gt=0)],
        iv: Annotated[float, Field(gt=0)],
        side: Literal["long", "short"] = "long",
        size: Annotated[float, Field(gt=0)] = 1.0,
        forward: Annotated[float | None, Field(gt=0)] = None,
        width_pct: Annotated[float, Field(gt=0, le=2.0)] = 0.3,
        grid: Annotated[int, Field(ge=11, le=1001)] = 201,
        kind: Literal["detailed", "summary"] = "summary",
    ) -> dict[str, Any]:
        b = Builder()
        if put_strike >= call_strike:
            b.miss("strikes", "tradedesk", "data_unavailable", "put_strike must be below call_strike")
            return b.build(None, ok=False)
        legs = build_strangle(
            call_strike=call_strike,
            put_strike=put_strike,
            call_premium=call_premium,
            put_premium=put_premium,
            side=side,
            size=size,
        )
        return b.build(_packed(legs, spot=spot, days=days, iv=iv, forward=forward, width_pct=width_pct, grid=grid, kind=kind))

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Risk reversal: bullish = long call + short put; bearish = long put + short call. "
            "Net premium is call_premium − put_premium (bullish); crypto puts often trade richer "
            "than calls (downside skew), making a bullish RR a credit position with unlimited "
            "upside but unlimited (bar strike) downside."
        ),
    )
    async def strategy_risk_reversal(
        call_strike: Annotated[float, Field(gt=0)],
        put_strike: Annotated[float, Field(gt=0)],
        call_premium: Annotated[float, Field(ge=0)],
        put_premium: Annotated[float, Field(ge=0)],
        spot: Annotated[float, Field(gt=0)],
        days: Annotated[float, Field(gt=0)],
        iv: Annotated[float, Field(gt=0)],
        direction: Literal["bullish", "bearish"] = "bullish",
        size: Annotated[float, Field(gt=0)] = 1.0,
        forward: Annotated[float | None, Field(gt=0)] = None,
        width_pct: Annotated[float, Field(gt=0, le=2.0)] = 0.3,
        grid: Annotated[int, Field(ge=11, le=1001)] = 201,
        kind: Literal["detailed", "summary"] = "summary",
    ) -> dict[str, Any]:
        b = Builder()
        legs = build_risk_reversal(
            call_strike=call_strike,
            put_strike=put_strike,
            call_premium=call_premium,
            put_premium=put_premium,
            direction=direction,
            size=size,
        )
        return b.build(_packed(legs, spot=spot, days=days, iv=iv, forward=forward, width_pct=width_pct, grid=grid, kind=kind))

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Iron condor: short strangle protected by further-OTM wings. Max profit (net credit) "
            "when S stays between the short strikes; max loss is the wing width minus the credit. "
            "Strikes must satisfy long_put < short_put < short_call < long_call."
        ),
    )
    async def strategy_iron_condor(
        long_put_strike: Annotated[float, Field(gt=0)],
        short_put_strike: Annotated[float, Field(gt=0)],
        short_call_strike: Annotated[float, Field(gt=0)],
        long_call_strike: Annotated[float, Field(gt=0)],
        long_put_premium: Annotated[float, Field(ge=0)],
        short_put_premium: Annotated[float, Field(ge=0)],
        short_call_premium: Annotated[float, Field(ge=0)],
        long_call_premium: Annotated[float, Field(ge=0)],
        spot: Annotated[float, Field(gt=0)],
        days: Annotated[float, Field(gt=0)],
        iv: Annotated[float, Field(gt=0)],
        size: Annotated[float, Field(gt=0)] = 1.0,
        forward: Annotated[float | None, Field(gt=0)] = None,
        width_pct: Annotated[float, Field(gt=0, le=2.0)] = 0.3,
        grid: Annotated[int, Field(ge=11, le=1001)] = 201,
        kind: Literal["detailed", "summary"] = "summary",
    ) -> dict[str, Any]:
        b = Builder()
        try:
            legs = build_iron_condor(
                long_put_strike=long_put_strike,
                short_put_strike=short_put_strike,
                short_call_strike=short_call_strike,
                long_call_strike=long_call_strike,
                long_put_premium=long_put_premium,
                short_put_premium=short_put_premium,
                short_call_premium=short_call_premium,
                long_call_premium=long_call_premium,
                size=size,
            )
        except ValueError as e:
            b.miss("strikes", "tradedesk", "data_unavailable", str(e))
            return b.build(None, ok=False)
        return b.build(_packed(legs, spot=spot, days=days, iv=iv, forward=forward, width_pct=width_pct, grid=grid, kind=kind))

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Cash-and-carry: long spot + short dated future. Earns the basis as the future "
            "converges to spot at expiry. Returns two linear legs plus the terminal-date payoff; "
            "PoP is 1.0 ONLY on the grid (the strategy's profit comes from basis convergence, "
            "which is NOT modelled by the simple log-normal) — treat PoP as a sanity check."
        ),
    )
    async def strategy_cash_and_carry(
        spot_price: Annotated[float, Field(gt=0)],
        futures_price: Annotated[float, Field(gt=0)],
        days: Annotated[float, Field(gt=0)],
        iv: Annotated[float, Field(gt=0, description="Used only for the forward-grid width + PoP sanity.")],
        size: Annotated[float, Field(gt=0)] = 1.0,
        width_pct: Annotated[float, Field(gt=0, le=2.0)] = 0.3,
        grid: Annotated[int, Field(ge=11, le=1001)] = 201,
        kind: Literal["detailed", "summary"] = "summary",
    ) -> dict[str, Any]:
        b = Builder()
        legs = build_cash_and_carry(spot_price=spot_price, futures_price=futures_price, size=size)
        data = _packed(legs, spot=spot_price, days=days, iv=iv, forward=spot_price, width_pct=width_pct, grid=grid, kind=kind)
        # Record the basis explicitly for context.
        data["basis_abs"] = r(futures_price - spot_price, 4)
        data["basis_pct"] = r((futures_price / spot_price - 1.0) * 100, 4)
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Funding carry: long perp on the cheap venue + short perp on the expensive venue. "
            "Payoff shown here is only the mark spread convergence — the real driver is the "
            "funding differential, which this tool does NOT compound (pair with funding_zscore "
            "and perps_basis_compare to size that leg)."
        ),
    )
    async def strategy_funding_carry(
        long_mark: Annotated[float, Field(gt=0, description="Mark on the venue you're long.")],
        short_mark: Annotated[float, Field(gt=0, description="Mark on the venue you're short.")],
        days: Annotated[float, Field(gt=0)],
        iv: Annotated[float, Field(gt=0, description="Used only for the forward-grid width + PoP sanity.")],
        size: Annotated[float, Field(gt=0)] = 1.0,
        width_pct: Annotated[float, Field(gt=0, le=2.0)] = 0.3,
        grid: Annotated[int, Field(ge=11, le=1001)] = 201,
        kind: Literal["detailed", "summary"] = "summary",
    ) -> dict[str, Any]:
        b = Builder()
        legs = build_funding_carry(long_mark=long_mark, short_mark=short_mark, size=size)
        data = _packed(
            legs,
            spot=0.5 * (long_mark + short_mark),
            days=days,
            iv=iv,
            forward=0.5 * (long_mark + short_mark),
            width_pct=width_pct,
            grid=grid,
            kind=kind,
        )
        data["spread_abs"] = r(short_mark - long_mark, 4)
        data["spread_bps"] = r((short_mark / long_mark - 1.0) * 10_000, 2)
        return b.build(data)
