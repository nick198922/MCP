"""cycle_regime: daily cycle regime of a coin (IDEA-20261028-btc-cycle-regime-gauge, step 1).

I/O through the providers only (Binance spot and USD-M daily klines, USD-M premium-index klines, ccxt funding
history); the state and its components come from `analytics.cycle_regime` (pure).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics import cycle_regime as cr
from ..analytics.market_flow import BASE_RE, points, round_tree
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY

D = cr.DAY_MS

CONTEXT_NOTE = (
    "Context, not a signal: the states and thresholds were fixed in card IDEA-20261028-btc-cycle-regime-gauge before "
    "any data was looked at and are NOT backtested yet (step 3 pending). BTC has had only about four independent "
    "cycles since perps began (2019), so a historical test has little power (expected verdict: inconclusive); the "
    "related trend and funding filters were inconclusive in our earlier tests (risk-overlay-vol-target-trend, "
    "funding-crowding-longshort, perp-spot-premium-spike-convergence). Nothing here is a probability, risk-neutral or "
    "otherwise."
)
UNITS_NOTE = (
    "Daily UTC closes of Binance only. price = spot close; basis7 = mean of 7 daily (USD-M perp close / spot close - "
    "1); funding7 = mean Binance funding of the last 7 days, simple-annualised decimal (0.10 = 10 %/yr; base rate "
    "0.01 %/8 h = 0.1095); premium7 = mean of the daily premium-index closes (reported, no rule uses it); RSI14 = "
    "Wilder, points. All other components are decimals (0.01 = 1 %)."
)
TREND_NOTE = (
    "growth / decline: the card names the price side of SMA365 and SMA365's 30-day slope without saying what a day "
    "with mixed signs is; the state follows the price side and trend_confirmed tells whether the slope agrees."
)
CLOSEST_NOTE = (
    "closest / transitions: greedy estimate of which conditions must change, ranked by the number of components, then "
    "by the size of the moves in rough typical units (a presentation choice, not part of the rules)."
)


def _rows_of(b: Builder, field: str, got: Any) -> list[Any]:
    """Rows of a ``{"rows", "complete"}`` range result, with a warning when it is incomplete."""
    if not isinstance(got, dict):
        return []
    if not got.get("complete", True):
        b.warn(f"{field}: history incomplete (paging stopped early); the earliest part of the window may be missing.")
    return got.get("rows") or []


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Daily cycle regime of a coin (BTC by default): context for decisions, NOT a trading signal. Labels the last "
            "closed UTC day with one of five states fixed before any data was examined (card "
            "IDEA-20261028-btc-cycle-regime-gauge): bottom = drawdown from the 365-day high >= 35 %, funding7 <= 0, "
            "basis7 < 0, RSI14 <= 35 (all); accumulation = |price / SMA365 - 1| <= 10 %, basis7 < 0, funding7 < 8 %/yr, "
            "price not at its 30-day low (all); overheat = price >= 1.3 x SMA365, funding7 >= 20 %/yr, basis7 > "
            "+0.05 %, RSI14 >= 70 (any 3 of 4); otherwise growth / decline by the side of the price against SMA365 "
            "(trend_confirmed = SMA365's 30-day slope agrees). Precedence: bottom, accumulation, overheat. Returns every "
            "component (spot close, SMA365, price_to_sma, sma_slope_30d, drawdown_365d, dist_30d_low, RSI14, basis7, "
            "funding7, premium7), every rule with its conditions, the nearest other state and what must change to get "
            "there, a Ukrainian one-liner and the states of the last `history_days` days. Units: decimals (0.01 = "
            "1 %); funding7 simple-annualised (Binance base 0.01 %/8 h = 0.1095); basis7 = mean of 7 daily perp/spot "
            "closes - 1; RSI in points. Data: Binance spot and USD-M daily klines, premium-index klines and funding "
            "history (needs about 400 days of spot history). Caveats: not backtested yet; few independent cycles, so "
            "any history test has little power; one venue; nothing here is a probability. A source that fails goes "
            "to `missing`; the rest is returned."
        ),
    )
    async def cycle_regime(
        base: Annotated[str, Field(description="Base asset ticker, e.g. 'BTC' (Binance BASEUSDT spot and USD-M perp).")] = "BTC",
        history_days: Annotated[
            int, Field(ge=0, le=365, description="Closed days of state history to return, oldest first (0 = none).")
        ] = 90,
    ) -> dict[str, Any]:
        b = Builder()
        sym_base = (base or "").strip().upper()
        if not BASE_RE.match(sym_base):
            b.miss("base", "tradedesk", "invalid_argument", "base must be a plain ticker like 'BTC'")
            return b.build(None, ok=False)
        now = rt.now()
        end = int(now.timestamp() * 1000) // D * D  # open of the day in progress: bars before it are closed
        span = max(history_days, 1)
        spot_start = end - (cr.MIN_SPOT_DAYS + span + cr.RSI_WARMUP_DAYS) * D
        short_start = end - (span + cr.AVG_DAYS + 1) * D
        symbol = f"{sym_base}USDT"
        got = await asyncio.gather(
            b.guard("spot_klines", "binance_spot", rt.binance_spot.klines_range(symbol, "1d", spot_start, end)),
            b.guard("perp_klines", "binance_futures", rt.binance_futures.klines_range(symbol, "1d", short_start, end)),
            b.guard("premium_index", "binance_futures", rt.binance_futures.premium_index_klines_range(symbol, "1d", short_start, end)),
            b.guard(
                "funding",
                "ccxt:binanceusdm",
                rt.exchanges.funding_rate_history_range(
                    "binanceusdm", f"{sym_base}/USDT:USDT", short_start, end + cr.SETTLE_SLACK_MS, trust_short_page=True
                ),
            ),
        )
        spot = cr.parse_closes(_rows_of(b, "spot_klines", got[0]))
        perp = cr.parse_closes(_rows_of(b, "perp_klines", got[1]))
        premium = cr.parse_closes(_rows_of(b, "premium_index", got[2]), positive=False)
        funding = points(_rows_of(b, "funding", got[3]), "timestamp", "fundingRate", lo=-0.1, hi=0.1)
        if not spot:
            if got[0] is not None:
                b.miss("spot_klines", "binance_spot", "data_unavailable", f"no daily {symbol} spot klines")
            return b.build(None, ok=False)

        data = cr.cycle_regime(spot, perp, funding, premium, now, history_days=history_days)
        comps = data["components"]
        if comps is None:
            b.warn(f"No spot close for {data['date']} (the last closed day): no state.")
        else:
            if comps["sma365"] is None:
                b.warn(f"Only {len(spot)} daily spot closes: SMA365, the 365-day drawdown and the state need at least 365.")
            elif comps["sma_slope_30d"] is None:
                b.warn("Fewer than 395 daily spot closes: no 30-day slope of SMA365 (trend_confirmed is null).")
            # fetched fine but too thin for the 7-day mean (a failed fetch is already in `missing`)
            for raw, field, provider, key, have in (
                (got[1], "perp_klines", "binance_futures", "basis7", "basis_days"),
                (got[2], "premium_index", "binance_futures", "premium7", "premium_days"),
                (got[3], "funding", "ccxt:binanceusdm", "funding7", "funding_settlements"),
            ):
                if raw is not None and comps[key] is None:
                    b.miss(field, provider, "data_unavailable", f"{key}: too little data in the last 7 days ({comps[have]})")
        if data["state"] is None and data["undecided_by"]:
            b.warn(f"State undecided: no data for {', '.join(data['undecided_by'])}.")
        b.warn(CONTEXT_NOTE)
        b.warn(UNITS_NOTE)
        b.warn(TREND_NOTE)
        b.warn(CLOSEST_NOTE)
        return b.build(round_tree({"base": sym_base, "history_days": history_days, **data}))
