"""Multi-leg strategy payoff + greeks + probability of profit. ``strategy_payoff`` takes
either the caller's ``legs`` or a ``preset`` (straddle / strangle / risk reversal /
iron condor) that generates them; cash-and-carry and funding carry stay separate
templates. All run the same summariser so the user sees payoff / greeks / PoP in one call."""

from __future__ import annotations

from collections.abc import Callable
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
    leg_problem,
    scenario_matrix,
    summarise,
)
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r, reject_nonfinite, reject_nonfinite_result

# Shared parameter schemas of strategy_payoff and the template builders (schema text only).
_PREMIUM_UNITS = "USD per contract (Deribit quotes option prices in BTC/ETH: multiply by the index price first)."
SPOT = Annotated[float, Field(gt=0, description="Underlying spot price (USD); anchors the payoff grid when `forward` is omitted.")]
DAYS = Annotated[float, Field(gt=0, description="Calendar days to expiry (T = days/365); drives the greeks, MTM and PoP.")]
IV = Annotated[
    float,
    Field(gt=0, description="Flat implied vol, decimal (0.45 = 45 %), for Black-76 greeks, MTM and the risk-neutral PoP."),
]
FORWARD = Annotated[
    float | None,
    Field(gt=0, description="Forward of the expiry for Black-76 (r = 0), USD; defaults to spot when omitted."),
]
WIDTH_PCT = Annotated[
    float,
    Field(gt=0, le=2.0, description="Payoff-grid half-width as a DECIMAL fraction of the forward (0.3 = ±30 %), despite the name."),
]
GRID = Annotated[int, Field(ge=11, le=1001, description="Number of price points on the payoff grid.")]
KIND = Annotated[
    Literal["detailed", "summary"],
    Field(description="'summary' = breakevens, max P/L, greeks, PoP; 'detailed' also returns the grid, prices and PnL curve."),
]

# Presets of strategy_payoff (the former strategy_straddle / _strangle / _risk_reversal / _iron_condor tools): the
# parameters each one requires and the ones it also accepts. Any other preset parameter is refused (invalid_input).
Preset = Literal["straddle", "strangle", "risk_reversal", "iron_condor"]
_CONDOR_STRIKES = ("long_put_strike", "short_put_strike", "short_call_strike", "long_call_strike")
_CONDOR_PREMIUMS = ("long_put_premium", "short_put_premium", "short_call_premium", "long_call_premium")
PRESETS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "straddle": (("strike", "call_premium", "put_premium"), ("side", "size")),
    "strangle": (("call_strike", "put_strike", "call_premium", "put_premium"), ("side", "size")),
    "risk_reversal": (("call_strike", "put_strike", "call_premium", "put_premium"), ("direction", "size")),
    "iron_condor": ((*_CONDOR_STRIKES, *_CONDOR_PREMIUMS), ("size",)),
}


def _opt(desc: str, ge: float | None = None) -> Any:
    """Optional positive (or, with ge=0, non-negative) float preset parameter."""
    if ge is not None:
        return Annotated[float | None, Field(ge=ge, description=desc)]
    return Annotated[float | None, Field(gt=0, description=desc)]


_P_STRIKE = _opt("Preset 'straddle': common strike of the call and the put, USD (usually ATM).")
_P_CALL_STRIKE = _opt("Presets 'strangle' (OTM call, above put_strike) and 'risk_reversal': call strike, USD.")
_P_PUT_STRIKE = _opt("Presets 'strangle' (OTM put, below call_strike) and 'risk_reversal': put strike, USD.")
_P_CALL_PREMIUM = _opt(f"Presets 'straddle' / 'strangle' / 'risk_reversal': call premium at entry, {_PREMIUM_UNITS}", ge=0)
_P_PUT_PREMIUM = _opt(f"Presets 'straddle' / 'strangle' / 'risk_reversal': put premium at entry, {_PREMIUM_UNITS}", ge=0)
_P_SIDE = Annotated[
    Literal["long", "short"] | None,
    Field(
        description="Presets 'straddle' / 'strangle' (default 'long'): 'long' buys both options (long volatility); 'short' sells "
        "both (loss grows past either breakeven, unbounded upside)."
    ),
]
_P_DIRECTION = Annotated[
    Literal["bullish", "bearish"] | None,
    Field(
        description="Preset 'risk_reversal' (default 'bullish'): 'bullish' = long call + short put; 'bearish' = long put + "
        "short call (the short leg carries the tail risk)."
    ),
]
_P_SIZE = _opt("Any preset: contracts per leg (> 0, default 1); direction comes from side / direction, not the sign.")
_P_LONG_PUT_STRIKE = _opt("Preset 'iron_condor': lowest strike, the bought protective put wing, USD.")
_P_SHORT_PUT_STRIKE = _opt("Preset 'iron_condor': sold put strike, USD; above long_put_strike.")
_P_SHORT_CALL_STRIKE = _opt("Preset 'iron_condor': sold call strike, USD; above short_put_strike.")
_P_LONG_CALL_STRIKE = _opt("Preset 'iron_condor': highest strike, the bought protective call wing, USD.")
_P_LONG_PUT_PREMIUM = _opt(f"Preset 'iron_condor': premium paid for the long put, {_PREMIUM_UNITS}", ge=0)
_P_SHORT_PUT_PREMIUM = _opt(f"Preset 'iron_condor': premium received for the short put, {_PREMIUM_UNITS}", ge=0)
_P_SHORT_CALL_PREMIUM = _opt(f"Preset 'iron_condor': premium received for the short call, {_PREMIUM_UNITS}", ge=0)
_P_LONG_CALL_PREMIUM = _opt(f"Preset 'iron_condor': premium paid for the long call, {_PREMIUM_UNITS}", ge=0)


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


# Extreme but finite inputs can still break the maths: log(F / K) of an underflowed ratio, an int beyond the float range.
_NUMERIC_ERRORS = (ValueError, ZeroDivisionError, OverflowError)


def _answer(
    b: Builder,
    legs: list[dict[str, Any]],
    *,
    spot: float,
    days: float,
    iv: float,
    forward: float | None,
    width_pct: float,
    grid: int,
    kind: str,
    extra: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Template answer: `_packed` (plus the `extra` fields), or ok=false (`invalid_input`) when an input or the result
    is NaN / inf."""
    if (bad := reject_nonfinite(b, legs=legs, spot=spot, days=days, iv=iv, forward=forward)) is not None:
        return bad
    try:
        data = _packed(legs, spot=spot, days=days, iv=iv, forward=forward, width_pct=width_pct, grid=grid, kind=kind)
        if extra is not None:
            data.update(extra())
    except _NUMERIC_ERRORS as e:
        b.miss("strategy", "tradedesk", "invalid_input", f"inputs are outside the model's numeric range: {type(e).__name__}: {e}")
        return b.build(None, ok=False)
    return reject_nonfinite_result(b, data) or b.build(data)


def _preset_legs(b: Builder, preset: str, p: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Legs of a `preset` from its parameters `p` (required ones present: checked by the caller), exactly as the former
    template tools built them; None with `missing: strikes` when the strike order is wrong (strangle, iron condor)."""
    size = p.get("size", 1.0)
    if preset == "straddle":
        return build_straddle(
            strike=p["strike"], call_premium=p["call_premium"], put_premium=p["put_premium"], side=p.get("side", "long"), size=size
        )
    if preset == "strangle":
        if p["put_strike"] >= p["call_strike"]:
            b.miss("strikes", "tradedesk", "data_unavailable", "put_strike must be below call_strike")
            return None
        return build_strangle(
            call_strike=p["call_strike"],
            put_strike=p["put_strike"],
            call_premium=p["call_premium"],
            put_premium=p["put_premium"],
            side=p.get("side", "long"),
            size=size,
        )
    if preset == "risk_reversal":
        return build_risk_reversal(
            call_strike=p["call_strike"],
            put_strike=p["put_strike"],
            call_premium=p["call_premium"],
            put_premium=p["put_premium"],
            direction=p.get("direction", "bullish"),
            size=size,
        )
    try:
        return build_iron_condor(**{k: p[k] for k in (*_CONDOR_STRIKES, *_CONDOR_PREMIUMS)}, size=size)
    except ValueError as e:
        b.miss("strikes", "tradedesk", "data_unavailable", str(e))
        return None


def _preset_problem(preset: str | None, legs: list[dict[str, Any]] | None, given: dict[str, Any]) -> tuple[str, str] | None:
    """(field, message) when `legs`, `preset` and the preset parameters do not fit together, else None."""
    if preset is None:
        if given:
            return "preset", f"{', '.join(sorted(given))} apply only with a preset; set preset or drop them"
        if legs is None:
            return "legs", f"give legs or a preset ({' | '.join(PRESETS)})"
        return None
    if legs is not None:
        return "legs", f"give legs or preset='{preset}', not both"
    required, optional = PRESETS[preset]
    if absent := [k for k in required if k not in given]:
        return "preset", f"preset '{preset}' needs {', '.join(absent)}"
    if foreign := sorted(set(given) - set(required) - set(optional)):
        return "preset", f"{', '.join(foreign)} do not apply to preset '{preset}' (it takes {', '.join((*required, *optional))})"
    return None


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Strategy payoff from your own `legs` OR from a `preset` structure (exactly one of the two). Each leg is a "
            "dict with: kind ('call' | 'put' | 'linear'), side ('long' | 'short'), size (contracts, > 0), strike (for "
            "options), entry_premium (option premium in USD), entry (for 'linear' legs). Presets (they replace the former "
            "strategy_straddle / strategy_strangle / strategy_risk_reversal / strategy_iron_condor tools; the generated "
            "legs come back in data.legs): 'straddle' needs strike, call_premium, put_premium (optional side, size): "
            "call + put at one strike, long = long volatility, max loss = both premiums, breakevens at strike ± premium. "
            "'strangle' needs call_strike, put_strike (put_strike < call_strike), call_premium, put_premium (optional "
            "side, size): OTM call + OTM put, wider breakevens for a lower premium. 'risk_reversal' needs call_strike, "
            "put_strike, call_premium, put_premium (optional direction, size): bullish = long call + short put, bearish = "
            "long put + short call; crypto puts often trade richer (downside skew), so a bullish RR can be a credit with "
            "unbounded loss past the short strike. 'iron_condor' needs long_put_strike < short_put_strike < "
            "short_call_strike < long_call_strike and the four premiums (optional size): net credit, max profit between "
            "the short strikes, max loss = wing width − credit. A missing or foreign preset parameter, or legs together "
            "with a preset, answers ok=false (invalid_input); a wrong strike order answers ok=false (missing: strikes). "
            "Strikes and premiums in USD (Deribit quotes options in BTC/ETH: multiply by the index first); iv is a "
            "decimal (0.45 = 45 %). Returns the payoff curve on a grid around the forward, breakeven prices, max profit "
            "/ loss on that grid, Black-76 net greeks (r = 0, T = days/365) at the current forward, mark-to-market "
            "value, and the RISK-NEUTRAL probability of profit under one flat vol — a hint, not a forecast."
        ),
    )
    async def strategy_payoff(
        spot: SPOT,
        days: DAYS,
        iv: IV,
        legs: Annotated[
            list[dict[str, Any]] | None,
            Field(
                min_length=1,
                description="List of leg dicts: {kind, side, size, strike?, entry_premium?, entry?}. A leg with another kind or "
                "side, size <= 0, an option strike <= 0 or a negative premium is rejected (invalid_input). Omit with a preset.",
            ),
        ] = None,
        preset: Annotated[
            Preset | None,
            Field(
                description="Build the legs from a ready structure instead of `legs`; its parameters are listed in the tool description."
            ),
        ] = None,
        forward: FORWARD = None,
        width_pct: WIDTH_PCT = 0.3,
        grid: GRID = 201,
        kind: Annotated[
            Literal["detailed", "summary"] | None,
            Field(
                description="'summary' = breakevens, max P/L, greeks, PoP; 'detailed' also returns the grid, prices and PnL "
                "curve. Default: 'detailed' with legs, 'summary' with a preset."
            ),
        ] = None,
        strike: _P_STRIKE = None,
        call_strike: _P_CALL_STRIKE = None,
        put_strike: _P_PUT_STRIKE = None,
        call_premium: _P_CALL_PREMIUM = None,
        put_premium: _P_PUT_PREMIUM = None,
        side: _P_SIDE = None,
        direction: _P_DIRECTION = None,
        size: _P_SIZE = None,
        long_put_strike: _P_LONG_PUT_STRIKE = None,
        short_put_strike: _P_SHORT_PUT_STRIKE = None,
        short_call_strike: _P_SHORT_CALL_STRIKE = None,
        long_call_strike: _P_LONG_CALL_STRIKE = None,
        long_put_premium: _P_LONG_PUT_PREMIUM = None,
        short_put_premium: _P_SHORT_PUT_PREMIUM = None,
        short_call_premium: _P_SHORT_CALL_PREMIUM = None,
        long_call_premium: _P_LONG_CALL_PREMIUM = None,
    ) -> dict[str, Any]:
        b = Builder()
        params = {
            "strike": strike,
            "call_strike": call_strike,
            "put_strike": put_strike,
            "call_premium": call_premium,
            "put_premium": put_premium,
            "side": side,
            "direction": direction,
            "size": size,
            "long_put_strike": long_put_strike,
            "short_put_strike": short_put_strike,
            "short_call_strike": short_call_strike,
            "long_call_strike": long_call_strike,
            "long_put_premium": long_put_premium,
            "short_put_premium": short_put_premium,
            "short_call_premium": short_call_premium,
            "long_call_premium": long_call_premium,
        }
        given = {k: v for k, v in params.items() if v is not None}
        if (problem := _preset_problem(preset, legs, given)) is not None:
            b.miss(problem[0], "tradedesk", "invalid_input", problem[1])
            return b.build(None, ok=False)
        if preset is not None:
            if (built := _preset_legs(b, preset, given)) is None:
                return b.build(None, ok=False)
            return _answer(b, built, spot=spot, days=days, iv=iv, forward=forward, width_pct=width_pct, grid=grid, kind=kind or "summary")
        # No preset: `_preset_problem` has made sure `legs` are given.
        if (bad := reject_nonfinite(b, legs=legs, spot=spot, days=days, iv=iv, forward=forward)) is not None:
            return bad
        if (problem := leg_problem(legs)) is not None:
            b.miss("legs", "tradedesk", "invalid_input", problem)
            return b.build(None, ok=False)
        F = float(forward) if forward is not None else float(spot)
        try:
            res = summarise(list(legs), forward=F, days=float(days), iv=float(iv), width_pct=float(width_pct), grid=int(grid))
        except (KeyError, TypeError, *_NUMERIC_ERRORS) as e:
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
        if kind in (None, "detailed"):
            data["grid"] = res["grid"]
            data["prices"] = [r(x, 2) for x in res["prices"]]
            data["pnl"] = [r(x, 2) for x in res["pnl"]]
        return reject_nonfinite_result(b, data) or b.build(data)

    # -------- template builders --------

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
        spot_price: Annotated[float, Field(gt=0, description="Spot price you buy at, USD; also the payoff-grid anchor.")],
        futures_price: Annotated[
            float,
            Field(gt=0, description="Dated-future price you sell at, USD; the basis is futures_price − spot_price (not annualised)."),
        ],
        days: Annotated[
            float,
            Field(
                gt=0, description="Calendar days to the future's expiry; used only for the PoP sanity check (fees, borrow not modelled)."
            ),
        ],
        iv: Annotated[
            float, Field(gt=0, description="Flat implied vol, decimal (0.45 = 45 %); used only for the forward-grid width + PoP sanity.")
        ],
        size: Annotated[float, Field(gt=0, description="Units of the underlying on each leg (long spot, short future), > 0.")] = 1.0,
        width_pct: WIDTH_PCT = 0.3,
        grid: GRID = 201,
        kind: KIND = "summary",
    ) -> dict[str, Any]:
        b = Builder()
        legs = build_cash_and_carry(spot_price=spot_price, futures_price=futures_price, size=size)

        def basis() -> dict[str, Any]:  # the basis, explicitly for context
            return {"basis_abs": r(futures_price - spot_price, 4), "basis_pct": r((futures_price / spot_price - 1.0) * 100, 4)}

        return _answer(
            b, legs, spot=spot_price, days=days, iv=iv, forward=spot_price, width_pct=width_pct, grid=grid, kind=kind, extra=basis
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Funding carry: long perp on the cheap venue + short perp on the expensive venue. "
            "Payoff shown here is only the mark spread convergence — the real driver is the "
            "funding differential, which this tool does NOT compound (pair with funding_zscore "
            "and perps_basis_compare to size that leg; both report annualised funding as a decimal, 0.10 = 10 %)."
        ),
    )
    async def strategy_funding_carry(
        long_mark: Annotated[float, Field(gt=0, description="Mark on the venue you're long.")],
        short_mark: Annotated[float, Field(gt=0, description="Mark on the venue you're short.")],
        days: Annotated[
            float,
            Field(gt=0, description="Holding horizon in calendar days; funding over it is NOT accrued here (PoP sanity only)."),
        ],
        iv: Annotated[
            float, Field(gt=0, description="Flat implied vol, decimal (0.45 = 45 %); used only for the forward-grid width + PoP sanity.")
        ],
        size: Annotated[float, Field(gt=0, description="Units of the underlying on each perp leg (long and short), > 0.")] = 1.0,
        width_pct: WIDTH_PCT = 0.3,
        grid: GRID = 201,
        kind: KIND = "summary",
    ) -> dict[str, Any]:
        b = Builder()
        legs = build_funding_carry(long_mark=long_mark, short_mark=short_mark, size=size)

        def spread() -> dict[str, Any]:
            return {"spread_abs": r(short_mark - long_mark, 4), "spread_bps": r((short_mark / long_mark - 1.0) * 10_000, 2)}

        mid = 0.5 * (long_mark + short_mark)  # overflows to inf for marks near the float maximum: `_answer` rejects it
        return _answer(b, legs, spot=mid, days=days, iv=iv, forward=mid, width_pct=width_pct, grid=grid, kind=kind, extra=spread)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Mark-to-market SCENARIO MATRIX across (horizon, price). For each horizon "
            "{days, iv} and each S in the grid, revalues the whole strategy via Black-76 "
            "(linear legs = (S - entry) * sign * size). Returns a per-horizon block with "
            "the price list, the PnL row, and net δ/γ/vega/θ at the initial forward and "
            "that horizon. Default price grid is percent-moves around `spot`; override via "
            "`price_grid_pct` (e.g. [-10,-5,-2,0,2,5,10]). Horizons default to a near / mid "
            "/ expiry schedule derived from the longest horizon."
        ),
    )
    async def strategy_scenario_matrix(
        legs: Annotated[
            list[dict[str, Any]],
            Field(
                min_length=1,
                description="Leg dicts: {kind, side, size, strike?, entry_premium?, entry?}; checked as in strategy_payoff.",
            ),
        ],
        spot: Annotated[float, Field(gt=0, description="Underlying spot used as the price-grid anchor.")],
        horizons: Annotated[
            list[dict[str, float]] | None,
            Field(
                description="List of {days, iv}: days from now (>= 0; 0 = the expiry row, negative is rejected), iv decimal "
                "(>= 0; 0 = intrinsic value). Omit to use [day/3, day/2, day] from `days`; an empty list is rejected."
            ),
        ] = None,
        days: Annotated[float, Field(gt=0, description="Longest horizon in days when `horizons` is omitted.")] = 30.0,
        iv: Annotated[float, Field(gt=0, description="Default IV for the derived horizon schedule, decimal (0.50 = 50 %).")] = 0.5,
        forward: Annotated[float | None, Field(gt=0, description="Forward used for greeks; defaults to spot.")] = None,
        price_grid_pct: Annotated[
            list[float] | None,
            Field(description="Percent-moves around spot (e.g. [-10,-5,-2,0,2,5,10]). None → default grid."),
        ] = None,
    ) -> dict[str, Any]:
        b = Builder()
        named = {"legs": legs, "spot": spot, "horizons": horizons, "days": days, "iv": iv, "forward": forward}
        if (bad := reject_nonfinite(b, **named, price_grid_pct=price_grid_pct)) is not None:
            return bad
        if (problem := leg_problem(legs)) is not None:
            b.miss("legs", "tradedesk", "invalid_input", problem)
            return b.build(None, ok=False)
        if horizons == []:  # an empty matrix is not an answer; omitting `horizons` gives the default schedule
            b.miss("input", "tradedesk", "invalid_input", "horizons is empty: omit it for the default schedule or give {days, iv}")
            return b.build(None, ok=False)
        # A negative horizon is in the past: the pricer would clamp it to 1e-9 days and show expiry values under it.
        # A negative iv would be priced as zero vol (intrinsic value) without a word.
        neg = [(i, k) for i, h in enumerate(horizons or []) for k in ("days", "iv") if isinstance(h.get(k), int | float) and h[k] < 0]
        if neg:
            i, k = neg[0]
            b.miss("input", "tradedesk", "invalid_input", f"horizons.{i}.{k} is negative ({horizons[i][k]}): must be >= 0")
            return b.build(None, ok=False)
        F = float(forward) if forward is not None else float(spot)
        if horizons is None:
            horizons = [
                {"days": max(days / 3.0, 0.01), "iv": iv},
                {"days": max(days / 2.0, 0.02), "iv": iv},
                {"days": float(days), "iv": iv},
            ]
        pcts = price_grid_pct or [-10.0, -5.0, -2.0, 0.0, 2.0, 5.0, 10.0]
        prices = [float(spot) * (1.0 + p / 100.0) for p in pcts]
        try:
            matrix = scenario_matrix(list(legs), initial_forward=F, horizons=list(horizons), prices=prices)
        except (KeyError, TypeError, *_NUMERIC_ERRORS) as e:
            b.miss("legs", "tradedesk", "data_unavailable", f"bad input: {type(e).__name__}: {e}")
            return b.build(None, ok=False)

        rows = [
            {
                "days": row["days"],
                "iv": row["iv"],
                "prices": [r(p, 2) for p in row["prices"]],
                "pct_moves": pcts,
                "pnl_usd": [r(p, 2) for p in row["pnl"]],
                "greeks": {k: r(v, 6) for k, v in row["greeks"].items()},
            }
            for row in matrix
        ]
        data = {
            "spot": spot,
            "forward": F,
            "n_horizons": len(rows),
            "price_grid_pct": pcts,
            "matrix": rows,
            "measure": "mark-to-market (USD, Black-76, flat IV per horizon)",
        }
        return reject_nonfinite_result(b, data) or b.build(data)
