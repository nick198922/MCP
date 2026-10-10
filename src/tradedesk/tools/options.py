from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics import probability
from ..analytics.instruments import expiry_token
from ..analytics.options import (
    OptionRow,
    atm_iv,
    atm_straddle,
    build_chain,
    build_smile,
    gex_by_strike,
    gex_flip_shift,
    gex_profile,
    group_by_expiry,
    max_pain,
    net_gex,
    oi_by_strike,
    put_call_ratios,
    rr_fly,
)
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, find_expiry, r

CUR = Annotated[Literal["BTC", "ETH"], Field(description="Deribit inverse-option currency")]
EXPIRY = Annotated[str, Field(description="Expiry as 26MAR27 or 2027-03-26 (08:00 UTC). Use deribit_vol_surface to list expiries.")]
GEX_NOTE = (
    "Heuristic: assumes dealers are long calls / short puts. Real positioning is unknown, so treat the sign and "
    "flip level as a model output, not a fact."
)


async def _chain(rt: Runtime, b: Builder, currency: str) -> list[OptionRow] | None:
    data = await b.guard("option_book", "deribit", rt.deribit.book_summary(currency, "option"))
    return None if data is None else build_chain(data, rt.now())


def _leg(row: OptionRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "iv": r(row.iv, 4),
        "delta": r(row.delta, 4),
        "gamma": r(row.gamma, 8),
        "vega": r(row.vega, 2),
        "theta": r(row.theta, 2),
        "mark_usd": r(row.mark_price_usd, 2),
        "oi": r(row.open_interest, 1),
        "volume": r(row.volume, 1),
    }


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Deribit volatility surface summary for BTC/ETH: for each expiry the forward, ATM IV, 25-delta and 10-delta risk reversal "
            "(call IV minus put IV; negative = puts richer) and butterfly, expected 1-sigma move, open interest and put/call OI ratio. "
            "IVs are decimals (0.45 = 45 %). Greeks are recomputed with Black-76 and match Deribit's. Start here for any options question."
        ),
    )
    async def deribit_vol_surface(
        currency: CUR = "BTC",
        min_days: Annotated[float, Field(ge=0)] = 0.5,
        max_days: Annotated[float, Field(gt=0)] = 400,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await _chain(rt, b, currency)
        if rows is None:
            return b.build(None, ok=False)
        now = rt.now()
        out: list[dict[str, Any]] = []
        for exp, rs in group_by_expiry(rows).items():
            days = (exp - now).total_seconds() / 86400
            if not (min_days <= days <= max_days):
                continue
            pc = put_call_ratios(rs)
            item: dict[str, Any] = {
                "expiry": expiry_token(exp),
                "days": r(days, 2),
                "forward": rs[0].forward,
                "oi_calls": r(pc["oi_calls"], 1),
                "oi_puts": r(pc["oi_puts"], 1),
                "pc_oi": r(pc["pc_oi"], 3),
            }
            sm = build_smile(rs)
            if sm is None:
                item["note"] = "fewer than 3 priced strikes"
            else:
                a = atm_iv(sm)
                r25, r10 = rr_fly(sm, 0.25), rr_fly(sm, 0.10)
                item.update(
                    atm_iv=r(a, 4),
                    rr25=r(r25["rr"], 4),
                    fly25=r(r25["fly"], 4),
                    rr10=r(r10["rr"], 4),
                    fly10=r(r10["fly"], 4),
                    expected_move_1sd_pct=r(a * math.sqrt(sm.T) * 100, 2),
                )
            out.append(item)
        spot = next((x.index for x in rows if x.index), None)
        return b.build({"currency": currency, "spot_index": spot, "expiries": out})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Option chain for one Deribit expiry, one row per strike with call and put legs (IV, delta, gamma, vega per vol point, "
            "theta per day in USD, mark price in USD, open interest, volume). Restricted to strikes within `width_pct` % of the forward."
        ),
    )
    async def deribit_option_chain(
        expiry: EXPIRY,
        currency: CUR = "BTC",
        width_pct: Annotated[float, Field(gt=0, le=200)] = 20,
        min_oi: Annotated[float, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await _chain(rt, b, currency)
        if rows is None:
            return b.build(None, ok=False)
        by = group_by_expiry(rows)
        exp, err = find_expiry(by, expiry)
        if exp is None:
            b.miss("expiry", "tradedesk", "data_unavailable", err or "")
            return b.build(None, ok=False)
        rs = by[exp]
        F = rs[0].forward
        strikes: dict[float, dict[bool, OptionRow]] = {}
        for x in rs:
            if abs(math.log(x.strike / F)) <= width_pct / 100 and x.open_interest >= min_oi:
                strikes.setdefault(x.strike, {})[x.is_call] = x
        sm = build_smile(rs)
        table = [{"strike": k, "call": _leg(v.get(True)), "put": _leg(v.get(False))} for k, v in sorted(strikes.items())]
        return b.build(
            {
                "currency": currency,
                "expiry": expiry_token(exp),
                "forward": F,
                "days": r(rs[0].T * 365, 2),
                "atm_iv": r(atm_iv(sm), 4) if sm else None,
                "strikes": table,
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Max-pain level for one Deribit expiry (settlement price minimising total option-holder payout, USD), distance from the "
            "forward, put/call ratios, and the five strikes with the largest open interest. Max pain is a heuristic, not a forecast."
        ),
    )
    async def deribit_max_pain(expiry: EXPIRY, currency: CUR = "BTC") -> dict[str, Any]:
        b = Builder()
        rows = await _chain(rt, b, currency)
        if rows is None:
            return b.build(None, ok=False)
        by = group_by_expiry(rows)
        exp, err = find_expiry(by, expiry)
        if exp is None:
            b.miss("expiry", "tradedesk", "data_unavailable", err or "")
            return b.build(None, ok=False)
        rs = by[exp]
        mp = max_pain(rs)
        if mp is None:
            b.miss("max_pain", "tradedesk", "data_unavailable", "no open interest for this expiry")
            return b.build(None, ok=False)
        F = rs[0].forward
        oi_by_strike: dict[float, float] = {}
        for x in rs:
            oi_by_strike[x.strike] = oi_by_strike.get(x.strike, 0.0) + x.open_interest
        top = sorted(oi_by_strike.items(), key=lambda kv: kv[1], reverse=True)[:5]
        curve = [c for c in mp["curve"] if abs(c["settle"] / F - 1) <= 0.25]
        pc = put_call_ratios(rs)
        return b.build(
            {
                "currency": currency,
                "expiry": expiry_token(exp),
                "forward": F,
                "max_pain": mp["max_pain"],
                "distance_from_forward_pct": r((mp["max_pain"] / F - 1) * 100, 2),
                "put_call": {k: r(v, 3) for k, v in pc.items()},
                "top_oi_strikes": [{"strike": k, "oi": r(v, 1)} for k, v in top],
                "payout_curve": [{"settle": c["settle"], "payout_usd": r(c["payout_usd"], 0)} for c in curve],
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Dealer gamma exposure (GEX) model for Deribit BTC/ETH options: net GEX in USD per 1 % move, largest positive/negative "
            "strikes, and the estimated spot level where net GEX flips sign. " + GEX_NOTE
        ),
    )
    async def deribit_gamma_exposure(
        currency: CUR = "BTC",
        expiry: Annotated[str | None, Field(description="Limit to one expiry; default all expiries")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await _chain(rt, b, currency)
        if rows is None:
            return b.build(None, ok=False)
        if expiry:
            by = group_by_expiry(rows)
            exp, err = find_expiry(by, expiry)
            if exp is None:
                b.miss("expiry", "tradedesk", "data_unavailable", err or "")
                return b.build(None, ok=False)
            rows = by[exp]
        spot = next((x.index for x in rows if x.index), None)
        total = net_gex(rows)
        by_strike = gex_by_strike(rows)
        ranked = sorted(by_strike.items(), key=lambda kv: kv[1])
        prof = gex_profile(rows)
        flip = gex_flip_shift(prof)
        data = {
            "currency": currency,
            "expiry": expiry or "all",
            "spot_index": spot,
            "net_gex_usd_per_1pct": r(total, 0),
            "regime": "positive (dampening)" if total > 0 else "negative (amplifying)",
            "flip_level": r(spot * flip, 0) if spot and flip else None,
            "top_negative_strikes": [{"strike": k, "gex_usd": r(v, 0)} for k, v in ranked[:5]],
            "top_positive_strikes": [{"strike": k, "gex_usd": r(v, 0)} for k, v in reversed(ranked[-5:])],
            "profile": [{"spot_shift_pct": r((s - 1) * 100, 0), "net_gex_usd": r(g, 0)} for s, g in prof if round((s - 1) * 100) % 5 == 0],
            "notes": GEX_NOTE,
        }
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Expected-move estimate for one or every Deribit expiry from the ATM straddle. "
            "straddle_usd is the mark price of 1 call + 1 put at the strike nearest the forward (in USD per unit of underlying); "
            "under risk-neutral lognormal with r=0 this also approximates E[|S_T - F|]. breakeven_lo/hi = F ± straddle_usd are the straddle's "
            "P/L breakevens — the magnitude of move required to profit buying the straddle. sigma_range comes from ATM IV × √T (geometric)."
        ),
    )
    async def deribit_expected_move(
        currency: CUR = "BTC",
        expiry: Annotated[str | None, Field(description="Limit to one expiry (e.g. 26MAR27); default all expiries")] = None,
        min_days: Annotated[float, Field(ge=0)] = 0.5,
        max_days: Annotated[float, Field(gt=0)] = 400,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await _chain(rt, b, currency)
        if rows is None:
            return b.build(None, ok=False)
        by = group_by_expiry(rows)
        if expiry:
            exp, err = find_expiry(by, expiry)
            if exp is None:
                b.miss("expiry", "tradedesk", "data_unavailable", err or "")
                return b.build(None, ok=False)
            by = {exp: by[exp]}
        now = rt.now()
        out: list[dict[str, Any]] = []
        for exp, rs in by.items():
            days = (exp - now).total_seconds() / 86400
            if not (min_days <= days <= max_days):
                continue
            st = atm_straddle(rs)
            if st is None:
                out.append({"expiry": expiry_token(exp), "days": r(days, 2), "note": "ATM call/put mark unavailable"})
                continue
            lo, hi = st["sigma_range"] or (None, None)
            out.append(
                {
                    "expiry": expiry_token(exp),
                    "days": r(days, 2),
                    "forward": r(st["forward"], 2),
                    "atm_strike": st["atm_strike"],
                    "atm_iv": r(st["atm_iv"], 4),
                    "call_mark_usd": r(st["call_mark_usd"], 2),
                    "put_mark_usd": r(st["put_mark_usd"], 2),
                    "straddle_usd": r(st["straddle_usd"], 2),
                    "straddle_pct": r(st["straddle_pct_of_forward"] * 100, 3),
                    "breakeven_lo": r(st["breakeven_lo"], 2),
                    "breakeven_hi": r(st["breakeven_hi"], 2),
                    "sigma_move_pct": r((st["sigma_move_pct"] or 0.0) * 100, 3) if st["sigma_move_pct"] else None,
                    "sigma_lo": r(lo, 2),
                    "sigma_hi": r(hi, 2),
                }
            )
        if not out:
            b.miss("straddle", "tradedesk", "data_unavailable", "no expiries matched the window")
            return b.build(None, ok=False)
        spot = next((x.index for x in rows if x.index), None)
        return b.build({"currency": currency, "spot_index": spot, "expiries": out})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Open-interest walls on Deribit BTC/ETH options: per-strike call/put OI (in contracts, underlying units) and USD notional at the forward, "
            "sorted largest-first. Returns top strikes overall and separately the biggest call walls (potential resistance) and put walls (potential support). "
            "Walls are a positioning snapshot, not a forecast; big OI can be dealer-hedged or vice versa."
        ),
    )
    async def deribit_oi_by_strike(
        currency: CUR = "BTC",
        expiry: Annotated[str | None, Field(description="Limit to one expiry (e.g. 26MAR27); default all expiries")] = None,
        width_pct: Annotated[float, Field(gt=0, le=200)] = 25,
        top_n: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await _chain(rt, b, currency)
        if rows is None:
            return b.build(None, ok=False)
        by = group_by_expiry(rows)
        if expiry:
            exp, err = find_expiry(by, expiry)
            if exp is None:
                b.miss("expiry", "tradedesk", "data_unavailable", err or "")
                return b.build(None, ok=False)
            scope = by[exp]
        else:
            scope = rows
        F = next((x.forward for x in scope), None)
        if F is None:
            b.miss("forward", "tradedesk", "data_unavailable", "no priced options for this scope")
            return b.build(None, ok=False)
        banded = [x for x in scope if abs(math.log(x.strike / F)) <= width_pct / 100]
        agg = oi_by_strike(banded)
        if not agg:
            b.miss("oi", "tradedesk", "data_unavailable", "no open interest in the band")
            return b.build(None, ok=False)
        total_call = sum(x["call_oi"] for x in agg)
        total_put = sum(x["put_oi"] for x in agg)
        rows_out = [
            {
                "strike": a["strike"],
                "call_oi": r(a["call_oi"], 1),
                "put_oi": r(a["put_oi"], 1),
                "total_oi": r(a["total_oi"], 1),
                "notional_usd": r(a["notional_usd"], 0),
                "pc_ratio": r(a["pc_ratio"], 3),
                "distance_pct": r((a["strike"] / F - 1) * 100, 2),
            }
            for a in agg
        ]
        top_all = sorted(rows_out, key=lambda x: x["total_oi"] or 0, reverse=True)[:top_n]
        top_calls = sorted(rows_out, key=lambda x: x["call_oi"] or 0, reverse=True)[:top_n]
        top_puts = sorted(rows_out, key=lambda x: x["put_oi"] or 0, reverse=True)[:top_n]
        spot = next((x.index for x in scope if x.index), None)
        return b.build(
            {
                "currency": currency,
                "expiry": expiry or "all",
                "forward": F,
                "spot_index": spot,
                "total_call_oi": r(total_call, 1),
                "total_put_oi": r(total_put, 1),
                "pc_oi": r((total_put / total_call) if total_call else None, 3),
                "band_strikes": len(agg),
                "top_overall": top_all,
                "top_call_walls": top_calls,
                "top_put_walls": top_puts,
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Risk-neutral probability implied by Deribit options for a price `level` at an expiry. kind='above'/'below': P(price beyond level AT "
            "expiry) from the smile slope (Breeden-Litzenberger, skew-adjusted). kind='touch': P(price touches level at ANY time before expiry), "
            "using the IV at that strike. These are market-implied (risk-neutral) odds, not real-world forecasts; options only cover quoted strikes."
        ),
    )
    async def prob_from_options(
        expiry: EXPIRY,
        level: Annotated[float, Field(gt=0, description="Price level in USD")],
        kind: Literal["above", "below", "touch"] = "above",
        currency: CUR = "BTC",
    ) -> dict[str, Any]:
        b = Builder()
        rows = await _chain(rt, b, currency)
        if rows is None:
            return b.build(None, ok=False)
        by = group_by_expiry(rows)
        exp, err = find_expiry(by, expiry)
        if exp is None:
            b.miss("expiry", "tradedesk", "data_unavailable", err or "")
            return b.build(None, ok=False)
        sm = build_smile(by[exp])
        if sm is None:
            b.miss("smile", "tradedesk", "data_unavailable", "fewer than 3 priced strikes at this expiry")
            return b.build(None, ok=False)
        if not sm.covers(level):
            b.warn(f"level {level:g} is outside the quoted strikes [{sm.strikes[0]:g}, {sm.strikes[-1]:g}]; IV is extrapolated flat")
        iv = sm.iv(level)
        if kind == "touch":
            p = probability.prob_touch(sm.forward, level, iv, sm.T)
            b.warn("touch probability uses a single constant IV (at the barrier strike); stochastic-vol effects are ignored")
        else:
            above = probability.prob_above(sm, level)
            p = above if kind == "above" else 1 - above
        move = atm_iv(sm) * math.sqrt(sm.T)
        return b.build(
            {
                "currency": currency,
                "expiry": expiry_token(exp),
                "forward": sm.forward,
                "level": level,
                "kind": kind,
                "probability": r(p, 4),
                "iv_at_level": r(iv, 4),
                "atm_iv": r(atm_iv(sm), 4),
                "one_sigma_range": [r(sm.forward * math.exp(-move), 0), r(sm.forward * math.exp(move), 0)],
                "days": r(sm.T * 365, 2),
                "measure": "risk-neutral",
            }
        )
