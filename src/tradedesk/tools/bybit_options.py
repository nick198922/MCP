"""Bybit option chain (BTC / ETH): the venue the owner trades options on; every other option tool reads Deribit.

Data: the Bybit v5 option tickers through ``Exchanges.public_get`` (``Exchanges._call``: rate limit, retries, health,
``Evidence``), the same request the collector ``option_xvenue`` and scripts/option_ideas_tg.py make. Parsing and the
chain view live in ``analytics/bybit_options.py`` (pure).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics import option_ideas as oi
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


Structure = Literal["iron_condor", "put_debit_spread", "call_debit_spread", "long_strangle", "long_straddle"]
# Strike-selection defaults per structure: those of analytics/option_ideas.py (the weekly ideas generator), so a default
# quote and the generator's idea of the same expiry are the same legs and numbers.
STRUCTURE_DEFAULTS: dict[str, dict[str, float]] = {
    "iron_condor": {"short_delta": 0.15, "wing": 0.03},
    "put_debit_spread": {"long_delta": 0.40, "short_delta": 0.15},
    "call_debit_spread": {"long_delta": 0.40, "short_delta": 0.15},
    "long_strangle": {"long_delta": 0.25},
    "long_straddle": {},
}
STRUCTURE_WARNINGS = (
    "Live Bybit top of book at quote_time_ms, taker fills: sold legs at the bid, bought legs at the ask; the size at the "
    "touch may be small and the quote moves. All money fields are USD per 1 coin at expiry (European), not per Bybit lot "
    "(0.01 BTC / 0.1 ETH).",
    "net_premium includes Bybit's taker fee per leg (0.03 % of the underlying, capped at 7 % of the option price); "
    "Bybit's delivery fee (0.015 % of the delivery price per in-the-money leg, capped at 12.5 % of its intrinsic value) "
    "is NOT in net_premium, max_loss or breakevens.",
    "prob_profit is risk-neutral, interpolated from Bybit's put deltas: a market-implied hint, not a forecast.",
    "A price quote, not a signal: as trade rules the IV>RV condor (P1), the 200-day-trend debit spread (P3) and the "
    "IV<RV long strangle (P4) tested negative (docs/knowledge/options-playbook.md).",
)
# Bybit Regular margin does not net a spread's bought leg against the sold one (docs/knowledge/options.md, Bybit Help
# Center "Initial and Maintenance Margin Calculation (Options)"), so a debit spread's sold leg needs margin too.
CREDIT_MARGIN_WARNING = (
    "Credit structure: sold legs need margin on Bybit, and the bought wing does not reduce the sold leg's margin (Regular "
    "margin; only Portfolio Margin nets them): the capital tied up is larger than max_loss."
)
DEBIT_MARGIN_WARNING = (
    "The debit spread's sold leg still needs margin on Bybit under Regular margin (the bought leg is not netted; only "
    "Portfolio Margin nets them): the capital tied up is the debit plus that margin, larger than max_loss."
)


def _build(kind: str, quotes: list[oi.Quote], exp: int, now_ms: int, coin: str, p: dict[str, float]) -> oi.Idea | None:
    if kind == "iron_condor":
        return oi.iron_condor(quotes, exp, now_ms, coin, short_delta=p["short_delta"], wing=p["wing"])
    if kind in ("put_debit_spread", "call_debit_spread"):
        bear = kind == "put_debit_spread"
        return oi.debit_spread(quotes, exp, now_ms, coin, bearish=bear, long_delta=p["long_delta"], short_delta=p["short_delta"])
    if kind == "long_strangle":
        return oi.long_strangle(quotes, exp, now_ms, coin, delta=p["long_delta"])
    return oi.long_straddle(quotes, exp, now_ms, coin)


def _listed(quotes: list[oi.Quote], now_ms: int) -> str:
    """Expiries of the parsed quotes with their days and two-sided quote count, for a missing-expiry detail."""
    n: dict[int, int] = {}
    for q in quotes:
        n[q.expiry_ms] = n.get(q.expiry_ms, 0) + (q.bid > 0 and q.ask > 0)
    return ", ".join(
        f"{expiry_token(datetime.fromtimestamp(e / 1000, UTC))} ({(e - now_ms) / oi.DAY_MS:.1f} d, {c} two-sided)"
        for e, c in sorted(n.items())
    )


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

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Price an option structure on Bybit's live BTC/ETH option chain (USDT-settled), the way the weekly option-ideas "
            "generator prices it. kind: iron_condor (sell the ~short_delta put and call, buy wings about wing x underlying further "
            "out, same width both sides), put_debit_spread / call_debit_spread (buy the ~long_delta option, sell the ~short_delta one "
            "further out), long_strangle (buy the ~long_delta put and call), long_straddle (buy the call and put at the strike "
            "nearest the underlying). Deltas are absolute Bybit deltas (0.15 = 15-delta). Expiry: the one nearest `days` within "
            "[min_days, max_days] calendar days that has at least 8 two-sided quotes (defaults 14 in [7, 24], the generator's; for a "
            "weekly use e.g. days=7, min_days=2). Returns the legs (Bybit symbol, side, strike, bid, ask, the fill price, its taker "
            "fee, delta, mark IV as a decimal), gross_premium (before fees), fees, net_premium after fees (> 0 credit, < 0 debit), "
            "max_profit (null = unlimited), max_loss, breakevens, reward_to_risk (max_profit / max_loss) and a risk-neutral "
            "prob_profit; all in USD per 1 coin at expiry. iron_condor also returns wing_widths (put / call strike distance, "
            "USD): the grid can make them unequal, max_loss is then the wider side's and `warnings` says so. prob_profit is null "
            "for a long strangle / straddle whose breakeven lies outside the quoted put strikes (the delta interpolation cannot "
            "reach it). min_days must not exceed max_days. Caveats in `warnings`: live top-of-book prices, Bybit's delivery fee "
            "not included, sold legs need margin that the bought leg does not reduce (credit structures and debit spreads), a "
            "quote not a signal."
        ),
    )
    async def option_structure_quote(
        currency: Annotated[Literal["BTC", "ETH"], Field(description="Underlying coin of the Bybit options.")] = "BTC",
        kind: Annotated[
            Structure,
            Field(
                description="Structure: iron_condor, put_debit_spread (bearish), call_debit_spread (bullish), long_strangle or "
                "long_straddle."
            ),
        ] = "iron_condor",
        days: Annotated[
            float, Field(gt=0, le=400, description="Target calendar days to expiry; the nearest eligible expiry is used.")
        ] = 14,
        min_days: Annotated[float, Field(ge=0, le=400, description="Earliest expiry considered, calendar days (inclusive).")] = 7,
        max_days: Annotated[float, Field(gt=0, le=400, description="Latest expiry considered, calendar days (inclusive).")] = 24,
        short_delta: Annotated[
            float | None,
            Field(
                gt=0,
                lt=1,
                description="|delta| of the sold leg(s): iron_condor and the debit spreads (default 0.15). Ignored by the long "
                "strangle / straddle.",
            ),
        ] = None,
        long_delta: Annotated[
            float | None,
            Field(
                gt=0,
                lt=1,
                description="|delta| of the bought leg(s): debit spreads (default 0.40) and long_strangle (default 0.25). Ignored "
                "by iron_condor (wings are set by `wing`) and long_straddle (ATM).",
            ),
        ] = None,
        wing: Annotated[
            float | None,
            Field(
                gt=0,
                le=0.5,
                description="iron_condor only: wing width as a fraction of the underlying (0.03 = 3 %, default), the bought strike "
                "nearest that distance beyond each sold one.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        b = Builder()
        given = {"short_delta": short_delta, "long_delta": long_delta, "wing": wing}
        defaults = STRUCTURE_DEFAULTS[kind]
        ignored = [k for k, v in given.items() if v is not None and k not in defaults]
        if ignored:
            b.warn(f"{', '.join(ignored)} not used by {kind}; ignored")
        params = {k: given[k] if given[k] is not None else v for k, v in defaults.items()}
        if min_days > max_days:
            b.miss(
                "min_days",
                "tradedesk",
                "invalid_input",
                f"min_days={min_days:g} is greater than max_days={max_days:g}: the expiry window is empty",
            )
            return b.build(None, ok=False)
        body = await b.guard(
            f"bybit_options:{currency}",
            f"ccxt:{BYBIT}",
            rt.exchanges.public_get(BYBIT, BYBIT_TICKERS, {"category": "option", "baseCoin": currency}, ttl=TICKERS_TTL),
        )
        if body is None:
            return b.build(None, ok=False)
        result = body.get("result") if isinstance(body, dict) else None
        rows = result.get("list") if isinstance(result, dict) else None
        _, other = parse_chain(rows, currency)
        if other:
            b.warn(f"{currency}: Bybit option symbols with settle suffix {', '.join(other)} skipped (only {SETTLE} is priced)")
        quotes = oi.parse_bybit_tickers(rows, currency)  # USDT-settled symbols only, as the ideas generator
        if not quotes:
            b.miss(
                f"bybit_options:{currency}", f"ccxt:{BYBIT}", "data_unavailable", f"no parsable {SETTLE}-settled {currency} option ticker"
            )
            return b.build(None, ok=False)
        now_ms = int(rt.now().timestamp() * 1000)
        exp = oi.pick_expiry(quotes, now_ms, days, min_days, max_days)
        if exp is None:
            b.miss(
                "expiry",
                "tradedesk",
                "data_unavailable",
                f"no {currency} expiry within {min_days:g}-{max_days:g} days with at least 8 two-sided quotes; listed: "
                f"{_listed(quotes, now_ms)}",
            )
            return b.build(None, ok=False)
        idea = _build(kind, quotes, exp, now_ms, currency, params)
        token = expiry_token(datetime.fromtimestamp(exp / 1000, UTC))
        if idea is None:
            b.miss(
                "structure",
                "tradedesk",
                "data_unavailable",
                f"no {kind} on {token} with {params}: a leg near the target delta has no bid / ask (or no delta), or the "
                "net premium after fees is not between 0 and the strike width",
            )
            return b.build(None, ok=False)
        view = oi.structure_legs(idea, quotes)
        legs = [
            dict(
                g,
                bid=r(g["bid"], 2),
                ask=r(g["ask"], 2),
                price=r(g["price"], 2),
                fee=r(g["fee"], 2),
                delta=r(g["delta"], 4),
                mark_iv=r(g["mark_iv"], 4),
            )
            for g in view["legs"]
        ]
        for w in STRUCTURE_WARNINGS:
            b.warn(w)
        if any(g.action == "sell" for g in idea.legs):
            b.warn(CREDIT_MARGIN_WARNING if idea.net > 0 else DEBIT_MARGIN_WARNING)
        wings = (idea.extra.get("put_wing"), idea.extra.get("call_wing"))
        if kind == "iron_condor" and None not in wings and abs(wings[0] - wings[1]) > 1e-9:
            narrow = min(wings) - idea.net
            b.warn(
                f"Unequal wings (strike grid): put wing {wings[0]:g}, call wing {wings[1]:g} USD; max_loss "
                f"({idea.max_loss:.2f}) is the wider side's, the narrower side loses at most {narrow:.2f}."
            )
        if idea.prob_profit is None and "put_strikes" in idea.extra:
            lo, hi = idea.extra["put_strikes"]
            outside = ", ".join(f"{x:.2f}" for x in idea.breakevens if not lo <= x <= hi)
            b.warn(
                f"prob_profit is null: breakeven(s) {outside} outside the put strikes with a delta ({lo:g}-{hi:g}); "
                "holding the end delta flat there would overstate the probability."
            )
        rr = idea.reward_to_risk
        data = {
            "currency": currency,
            "kind": kind,
            "settle": SETTLE,
            "quote_time_ms": ms(body.get("time")),
            "expiry": token,
            "days": r((exp - now_ms) / oi.DAY_MS, 2),
            "underlying": r(idea.spot, 2),
            "atm_iv": r(oi.atm_iv(quotes, exp), 4),
            "params": params,
            "legs": legs,
            "gross_premium": r(view["gross"], 2),
            "fees": r(view["fees"], 2),
            "net_premium": r(idea.net, 2),
            "net_type": "credit" if idea.net > 0 else "debit",
            "max_profit": r(idea.max_profit, 2),
            "max_loss": r(idea.max_loss, 2),
            "breakevens": [r(x, 2) for x in idea.breakevens],
            "reward_to_risk": r(rr, 4),
            "prob_profit": r(idea.prob_profit, 4),
        }
        if kind == "iron_condor" and None not in wings:
            data["wing_widths"] = {"put": r(wings[0], 2), "call": r(wings[1], 2)}
        if "prob_max_profit" in idea.extra:
            data["prob_max_profit"] = r(idea.extra["prob_max_profit"], 4)
        return b.build(data)
