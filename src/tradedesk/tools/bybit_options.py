"""Bybit option chain (BTC / ETH): the venue the owner trades options on; every other option tool reads Deribit.

Data: the Bybit v5 option tickers through ``Exchanges.public_get`` (``Exchanges._call``: rate limit, retries, health,
``Evidence``), the same request the collector ``option_xvenue`` and scripts/option_ideas_tg.py make. Parsing and the
chain view live in ``analytics/bybit_options.py`` (pure).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.bybit_options import SETTLE, days_to, expiry_view, index_price, parse_chain, select_expiries
from ..analytics.instruments import expiry_token
from ..analytics.option_xvenue import ms
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, find_expiry, r

BYBIT = "bybit"
BYBIT_TICKERS = "publicGetV5MarketTickers"  # GET /v5/market/tickers?category=option&baseCoin=<COIN>
TICKERS_TTL = 2.0  # seconds, as market_tickers

# Decimal places per output field (prices in USD per coin; BTC options tick 5 USDT, ETH 0.1 USDT).
DIGITS = {
    "bid": 2,
    "ask": 2,
    "mid": 2,
    "spread_pct": 2,
    "spread_iv": 4,
    "mark_iv": 4,
    "oi": 2,
    "bid_size": 2,
    "ask_size": 2,
    "bid_iv": 4,
    "ask_iv": 4,
    "mark": 2,
    "delta": 4,
}


def _round_leg(x: dict[str, Any] | None) -> dict[str, Any] | None:
    if x is None:
        return None
    return {k: r(v, DIGITS[k]) if k in DIGITS else v for k, v in x.items()}


def _round_expiry(e: dict[str, Any]) -> dict[str, Any]:
    out = dict(e, days=r(e["days"], 2), underlying=r(e["underlying"], 2), atm_iv=r(e["atm_iv"], 4))
    out["oi_calls"], out["oi_puts"] = r(e["oi_calls"], 2), r(e["oi_puts"], 2)
    out["strikes"] = [{"strike": s["strike"], "call": _round_leg(s["call"]), "put": _round_leg(s["put"])} for s in e["strikes"]]
    return out


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Bybit option chain for BTC/ETH (USDT-settled options on Bybit; every deribit_* tool reads Deribit only). Per expiry: "
            "days = calendar days to the 08:00 UTC expiry, underlying = Bybit's underlying price for that expiry (USD), ATM strike, "
            "atm_iv (mean of the ATM call and put mark IV, decimal), oi_calls / oi_puts over ALL strikes of the expiry, n_strikes listed. "
            "Per strike, call and put: best bid / ask and mid in USD per 1 coin (USDT treated as USD), spread_pct = (ask - bid) / mid in "
            "PERCENT, spread_iv = ask IV - bid IV as a DECIMAL (0.01 = 1 vol point; null when Bybit quotes no IV for a side, e.g. no "
            "bid), mark_iv (Bybit's mark IV, decimal: 0.45 = 45 %), oi = open interest in coins (1 contract = 1 BTC / 1 ETH); null "
            "fields mean the side is not quoted. Default answer is compact: the `max_expiries` nearest expiries within "
            "[min_days, max_days] and `strikes_each_side` strikes on each side of the ATM strike; `expiry` picks one expiry; "
            "include_details=true returns every expiry in the window and every strike, plus sizes at the touch (coins), bid / ask IV, "
            "mark price (USD) and Bybit's delta. Caveats: top of book only (the size at the best level, not depth); Bybit prices and "
            "IVs are on its own USDT index, which runs a few bp off Deribit's USD index, so they are not directly comparable with "
            "Deribit's; symbols in another settle currency (USDC) are skipped and named in `warnings`."
        ),
    )
    async def bybit_option_chain(
        currency: Annotated[Literal["BTC", "ETH"], Field(description="Underlying coin of the Bybit options.")] = "BTC",
        expiry: Annotated[
            str | None,
            Field(
                description="One expiry as 27NOV26 or 2026-11-27 (08:00 UTC); overrides min_days / max_days / max_expiries. "
                "Empty = the nearest expiries in the window."
            ),
        ] = None,
        min_days: Annotated[float, Field(ge=0, description="Skip expiries fewer than this many calendar days away (inclusive bound).")] = 0,
        max_days: Annotated[
            float, Field(gt=0, description="Skip expiries more than this many calendar days away (inclusive bound).")
        ] = 400,
        max_expiries: Annotated[
            int,
            Field(ge=1, le=50, description="How many of the nearest expiries in the window to show (ignored with include_details=true)."),
        ] = 3,
        strikes_each_side: Annotated[
            int,
            Field(
                ge=0,
                le=50,
                description="Strikes shown on each side of the ATM strike (0 = ATM only; ignored with include_details=true, which shows all).",
            ),
        ] = 3,
        include_details: Annotated[
            bool,
            Field(
                description="Every expiry in the window and every strike, with sizes at the touch, bid / ask IV, mark price and delta; "
                "default false = the compact view."
            ),
        ] = False,
    ) -> dict[str, Any]:
        b = Builder()
        body = await b.guard(
            f"bybit_options:{currency}",
            f"ccxt:{BYBIT}",
            rt.exchanges.public_get(BYBIT, BYBIT_TICKERS, {"category": "option", "baseCoin": currency}, ttl=TICKERS_TTL),
        )
        if body is None:
            return b.build(None, ok=False)
        result = body.get("result") if isinstance(body, dict) else None
        chain, other = parse_chain(result.get("list") if isinstance(result, dict) else None, currency)
        if other:
            b.warn(f"{currency}: Bybit option symbols with settle suffix {', '.join(other)} skipped (only {SETTLE} is shown)")
        if not chain:
            b.miss(
                f"bybit_options:{currency}", f"ccxt:{BYBIT}", "data_unavailable", f"no parsable {SETTLE}-settled {currency} option ticker"
            )
            return b.build(None, ok=False)
        now = rt.now()
        if expiry:
            exp, err = find_expiry(dict(sorted(chain.items())), expiry)  # sorted: the error lists expiries in date order
            if exp is None:
                b.miss("expiry", "tradedesk", "data_unavailable", err or "")
                return b.build(None, ok=False)
            window = picked = [exp]
        else:
            window = select_expiries(chain, now, min_days, max_days)
            picked = window if include_details else window[:max_expiries]
            if not window:
                listed = ", ".join(f"{expiry_token(e)} ({days_to(e, now):.1f} d)" for e in sorted(chain))
                b.warn(f"no {currency} expiry within {min_days:g}-{max_days:g} days; listed: {listed}")
        each = None if include_details else strikes_each_side
        expiries = [_round_expiry(expiry_view(chain[e], e, now, each, include_details)) for e in picked]
        data = {
            "currency": currency,
            "settle": SETTLE,
            "index": r(index_price(chain), 2),
            "quote_time_ms": ms(body.get("time")),
            "expiries_listed": len(chain),
            "expiries_in_window": len(window),
            "expiries": expiries,
        }
        return b.build(data)
