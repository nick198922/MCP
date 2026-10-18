from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.pendle import ADDR_RE, build_yield_curve, pt_price_to_apy, scan_fixed_yields, summarise_all
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY

_CAVEATS = (
    "impliedApy is the market's own fixed rate for a PT bought now and held to expiry (annualised, compounded, "
    "365-day year); it is only realised if held to expiry and the underlying does not depeg or default. "
    "Thin liquidity makes the quoted rate unreachable at size. Market names are untrusted third-party text."
)


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Scan Pendle fixed-yield markets (Pendle public API, all chains): for each active market the implied "
            "(fixed) APY of buying the PT, the underlying APY, their difference, days to expiry, PT price and "
            "discount derived from impliedApy (not a traded quote; price is in accounting-asset units, i.e. what "
            "1 PT redeems for at expiry), liquidity / TVL / trading volume in USD (trading_volume_usd is the API's "
            "tradingVolume field, period not verified as 24h). All rates and discounts are "
            "decimals (0.05 = 5 %). Sorted by `sort_by` (default implied_apy, highest first). Use it to find "
            "fixed-rate carry alternatives or to compare with perp funding. " + _CAVEATS
        ),
    )
    async def pendle_fixed_yields(
        min_liquidity_usd: Annotated[float, Field(ge=0, description="Drop markets with pool liquidity below this (USD).")] = 100_000.0,
        min_apy: Annotated[float | None, Field(description="Minimum implied APY as a decimal (0.08 = 8 %).")] = None,
        min_days: Annotated[float, Field(ge=0, description="Minimum days to expiry.")] = 1.0,
        max_days: Annotated[float, Field(gt=0, le=3650, description="Maximum days to expiry.")] = 1095.0,
        stable_only: Annotated[bool, Field(description="Only markets tagged 'stables' by Pendle.")] = False,
        chain_ids: Annotated[list[int] | None, Field(description="Optional EVM chain ids, e.g. [1, 42161, 8453].")] = None,
        sort_by: Annotated[
            Literal["implied_apy", "liquidity_usd", "days_to_expiry", "implied_minus_underlying"],
            Field(description="Sort key; days_to_expiry sorts soonest first, the others highest first."),
        ] = "implied_apy",
        limit: Annotated[int, Field(ge=1, le=200, description="Max markets returned.")] = 30,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await b.guard("markets", "pendle", rt.pendle.markets())
        if rows is None:
            return b.build(None, ok=False)
        data = scan_fixed_yields(
            rows,
            rt.now(),
            min_days=min_days,
            max_days=max_days,
            min_liquidity_usd=min_liquidity_usd,
            min_apy=min_apy,
            stable_only=stable_only,
            chain_ids=chain_ids,
            sort_by=sort_by,
            limit=limit,
        )
        if data["markets_total"] == 0:
            b.miss("markets", "pendle", "data_unavailable", "no usable markets in the response")
            return b.build(None, ok=False)
        if data["markets_skipped"]:
            b.warn(f"{data['markets_skipped']} malformed market entries were skipped.")
        if data["markets_matched"] == 0:
            b.warn("No market matches the filters; loosen min_liquidity_usd / min_apy / max_days.")
        b.warn(_CAVEATS)
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Term structure of Pendle fixed (implied) APY for one asset: all active markets whose name matches "
            "`asset` (e.g. 'sUSDe'; case-insensitive, exact match preferred, else substring), ordered by expiry, "
            "with days to expiry, implied APY and underlying APY (decimals, 0.05 = 5 %), implied-minus-underlying, "
            "change in implied APY vs the previous expiry, liquidity / TVL in USD, chain, and a `thin` flag for "
            "points below `thin_liquidity_usd` (null when liquidity is unknown). Observed points only: NO "
            "interpolation, so the curve is not arbitrage-free and gaps between expiries are real gaps. Different "
            "chains with the same expiry are separate points; the most liquid one is the `representative` of that "
            "expiry, and the change vs previous expiry and `shape_first_to_last` use representatives only "
            "(shape is null when several distinct names matched). Same-name markets from different issuers may be unrelated assets. "
            + _CAVEATS
        ),
    )
    async def pendle_yield_curve(
        asset: Annotated[str, Field(min_length=1, max_length=60, description="Market/asset name, e.g. 'sUSDe', 'USDe', 'stETH'.")],
        min_liquidity_usd: Annotated[float, Field(ge=0, description="Drop markets with pool liquidity below this (USD).")] = 0.0,
        thin_liquidity_usd: Annotated[
            float, Field(ge=0, description="Points with liquidity below this are flagged thin (USD).")
        ] = 1_000_000.0,
        min_days: Annotated[float, Field(ge=0, description="Minimum days to expiry.")] = 1.0,
        max_days: Annotated[float, Field(gt=0, le=3650, description="Maximum days to expiry.")] = 1095.0,
        chain_ids: Annotated[list[int] | None, Field(description="Optional EVM chain ids, e.g. [1, 42161, 8453].")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        if min_days > max_days:
            b.miss("curve", "pendle", "invalid_argument", f"min_days ({min_days}) is greater than max_days ({max_days})")
            return b.build(None, ok=False)
        rows = await b.guard("markets", "pendle", rt.pendle.markets())
        if rows is None:
            return b.build(None, ok=False)
        data = build_yield_curve(
            rows,
            rt.now(),
            asset,
            min_days=min_days,
            max_days=max_days,
            min_liquidity_usd=min_liquidity_usd,
            thin_liquidity_usd=thin_liquidity_usd,
            chain_ids=chain_ids,
        )
        if data["markets_total"] == 0:
            b.miss("markets", "pendle", "data_unavailable", "no usable markets in the response")
            return b.build(None, ok=False)
        if data["markets_skipped"]:
            b.warn(f"{data['markets_skipped']} malformed market entries were skipped.")
        if data["points_count"] == 0:
            b.miss("curve", "pendle", "data_unavailable", f"no active market matches asset {asset!r} and the filters")
            return b.build(None, ok=False)
        if data["distinct_expiries"] < 2:
            b.warn("Fewer than two distinct expiries: this is a single point, not a curve.")
        if data["thin_points"]:
            b.warn(
                f"{data['thin_points']} point(s) have liquidity below {thin_liquidity_usd:,.0f} USD; their APY may not be reachable at size."
            )
        if data["unknown_liquidity_points"]:
            b.warn(f"{data['unknown_liquidity_points']} point(s) have unknown liquidity (thin=null); not flagged thin.")
        if len(data["matched_names"]) > 1:
            b.warn(f"Substring match mixed several names {data['matched_names']}; pass the exact name for one asset.")
        b.warn("Implied APY is market-implied, not guaranteed. Points are observed, not interpolated; the curve is not arbitrage-free.")
        b.warn(_CAVEATS)
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "One Pendle market by its market address: implied (fixed) APY, underlying APY, days to expiry, implied PT "
            "price and discount (derived from impliedApy, not a traded quote), liquidity / TVL / trading volume in "
            "USD (trading_volume_usd = API tradingVolume, period unverified; decimals for rates). Optionally pass "
            "`pt_price` (PT price in units of the accounting asset, i.e. what 1 PT redeems for at expiry, e.g. 0.96; "
            "NOT in units of a yield-bearing underlying like sUSDe) to get the fixed APY it locks in to expiry: "
            "(1/price)^(365/days) - 1, compounded. The same address can exist on several chains; pass `chain_id` "
            "to disambiguate. " + _CAVEATS
        ),
    )
    async def pendle_market(
        market: Annotated[str, Field(description="Pendle market (pool) address, 0x + 40 hex chars; see pendle_fixed_yields.")],
        pt_price: Annotated[
            float | None,
            Field(gt=0, le=2, description="Optional PT price in accounting-asset units (1 PT redeems for 1) to convert into a fixed APY."),
        ] = None,
        chain_id: Annotated[
            int | None, Field(description="Optional EVM chain id to disambiguate the same address on several chains.")
        ] = None,
    ) -> dict[str, Any]:
        b = Builder()
        if not ADDR_RE.match(market or ""):
            b.miss("market", "pendle", "invalid_argument", "market must be a 0x-prefixed 40-hex-char address")
            return b.build(None, ok=False)
        rows = await b.guard("markets", "pendle", rt.pendle.markets())
        if rows is None:
            return b.build(None, ok=False)
        now = rt.now()
        summaries, _skipped = summarise_all(rows, now)
        found = [r for r in summaries if r["market"] == market.lower() and (chain_id is None or r["chain_id"] == chain_id)]
        if not found:
            b.miss("market", "pendle", "data_unavailable", "market not found among active Pendle markets (may have expired)")
            return b.build(None, ok=False)
        data = found[0]
        if len(found) > 1:
            chains = sorted({r["chain_id"] for r in found if r["chain_id"] is not None})
            b.warn(f"Address matches {len(found)} markets (chains {chains}); returned the first. Pass chain_id.")
        if pt_price is not None:
            d = data["days_to_expiry"]
            data["fixed_apy_at_pt_price"] = pt_price_to_apy(pt_price, d) if d is not None else None
            data["pt_price_input"] = pt_price
            if pt_price >= 1:
                b.warn("pt_price >= 1 means a negative fixed yield (PT above its redemption value).")
        b.warn(_CAVEATS)
        return b.build(data)
