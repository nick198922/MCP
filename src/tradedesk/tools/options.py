from __future__ import annotations

import math
from datetime import UTC, datetime
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
    wing_quality,
)
from ..analytics.vrp import align_daily, vrp_summary
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, find_expiry, r

CUR = Annotated[Literal["BTC", "ETH"], Field(description="Deribit inverse-option currency")]
EXPIRY = Annotated[str, Field(description="Expiry as 26MAR27 or 2027-03-26 (08:00 UTC). Use deribit_vol_surface to list expiries.")]
VRP_WARNING = (
    "Backtest 2021-03..2026-10 (docs/verification/2026-10-13-vrp.md): DVOL exceeded the next 30 days' realized vol "
    "on ~72 % (BTC) / ~65 % (ETH) of days; a daily delta-hedged short 30d ATM straddle earned roughly +1.3..2 % of "
    "spot per month on BTC (95 % CI above zero), weaker and not significant on ETH, with single-month losses up to "
    "~-15 % (BTC) / ~-35 % (ETH) of spot and a premium that has shrunk since 2021. The DVOL-minus-trailing-RV spread "
    "did NOT predict the next month's premium; DVOL's level did (exploratory, consistent in 2021-23 and 2024-26): "
    "starts with DVOL in the top fifth of its trailing-year range earned the most (BTC ~+2.7..3.9 % of spot per "
    "month), the bottom fifth about zero. DVOL is a 30d index that includes the wings and usually sits above ATM "
    "IV, so the straddle here is priced at DVOL minus `entry_cost_vol`. Daily closes only: intraday gamma, margin, "
    "early hedging slippage and Deribit's 08:00 UTC settlement are not modelled."
)


def _in_bucket(p: float | None, lo: float, hi: float) -> bool:
    return p is not None and (lo <= p < hi or (hi >= 1.0 and p >= 1.0))


def _day(d: float) -> str:
    return datetime.fromtimestamp(int(d) * 86400, UTC).date().isoformat()


GEX_NOTE = (
    "Heuristic: assumes dealers are long calls / short puts. Real positioning is unknown, so treat the sign and "
    "flip level as a model output, not a fact."
)
# docs/verification/2026-10-25-deribit-expiry-pin-mirror-control.md
PIN_WARNING = (
    "Pinning of settlement to these levels is not confirmed on our data (Deribit BTC daily expiries 2021-2026, "
    "n = 1 393): a systematic shift of settlement toward the top-volume strike of ≥ ~0.2 % of spot is ruled out at "
    "95 %; weaker shifts and local sticking to a strike were not tested. Treat these levels as a description of "
    "positioning, not as a settlement target."
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
            "IVs are decimals (0.45 = 45 %). Greeks are recomputed with Black-76 and match Deribit's. Liquidity controls: `min_oi` and "
            "`max_spread_pct` filter strikes BEFORE the smile is built; `thin_oi_threshold` only decides whether the 25Δ/10Δ wing is flagged "
            "as `thin_wing` (OI below threshold or missing two-sided quote). Start here for any options question."
        ),
    )
    async def deribit_vol_surface(
        currency: CUR = "BTC",
        min_days: Annotated[float, Field(ge=0)] = 0.5,
        max_days: Annotated[float, Field(gt=0)] = 400,
        min_oi: Annotated[float, Field(ge=0, description="Drop strikes whose OTM-side OI is below this before building the smile.")] = 0.0,
        max_spread_pct: Annotated[
            float | None, Field(ge=0, description="Drop strikes with bid/ask spread above this percent of mid (native units).")
        ] = None,
        thin_oi_threshold: Annotated[float, Field(ge=0, description="Wing OI below this flags the expiry's 25Δ/10Δ wing as thin.")] = 1.0,
        liquidity_weighted: Annotated[
            bool,
            Field(
                description="Replace the raw mark-IV interpolation with a quadratic least-squares smile fit in log-moneyness, "
                "weighted by OI and bid/ask spread (bounded weights). Smooths noisy thin wings; default false = exact mark IVs."
            ),
        ] = False,
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
            sm = build_smile(rs, min_oi=min_oi, max_spread_pct=max_spread_pct, liquidity_weighted=liquidity_weighted)
            if sm is None:
                item["note"] = "fewer than 3 priced strikes after filter"
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
                    strikes_used=len(sm.strikes),
                )
                for label, delta in (("25d", 0.25), ("10d", 0.10)):
                    wq = wing_quality(rs, sm, delta)
                    if wq is None:
                        item[f"thin_wing_{label}"] = True
                        continue
                    c, p = wq["call"], wq["put"]
                    thin = any(
                        x is None
                        or x["oi"] < thin_oi_threshold
                        or (max_spread_pct is not None and not x["quoted_both_sides"])
                        or (max_spread_pct is not None and x["spread_pct"] is not None and x["spread_pct"] > max_spread_pct)
                        for x in (c, p)
                    )
                    item[f"thin_wing_{label}"] = bool(thin)
                    if thin:
                        b.warn(f"thin {label} wing on {expiry_token(exp)}: call OI {c['oi'] if c else '?'}, put OI {p['oi'] if p else '?'}")
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
            "forward, put/call ratios, and the five strikes with the largest open interest. Max pain is a heuristic, not a forecast. "
            + PIN_WARNING
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
        b.warn(PIN_WARNING)
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
            "strikes, and the estimated spot level where net GEX flips sign. "
            + GEX_NOTE
            + " Key levels (flip level, top strikes): "
            + PIN_WARNING
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
        if data["flip_level"] is not None or ranked:
            b.warn(PIN_WARNING)
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

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Deribit DVOL index (implied-volatility index) time series via /public/get_volatility_index_data. "
            "DVOL is reported in annualised PERCENT (e.g. 45.3 means 45.3 %). Returns the OHLC series plus "
            "latest/mean/min/max/stdev and point-change vs 24h/7d ago. Resolutions supported: 60s, 3600s, "
            "43200s, 86400s; defaults to 3600s. IV rank/percentile needs stored history (Phase 2)."
        ),
    )
    async def deribit_dvol(
        currency: CUR = "BTC",
        hours_back: Annotated[int, Field(ge=1, le=720, description="How far back to pull (max 30 days).")] = 168,
        resolution: Annotated[Literal[60, 3600, 43200, 86400], Field(description="Candle size in seconds.")] = 3600,
    ) -> dict[str, Any]:
        b = Builder()
        end_ms = int(rt.now().timestamp() * 1000)
        start_ms = end_ms - hours_back * 3_600_000
        data = await b.guard("dvol", "deribit", rt.deribit.volatility_index_data(currency, start_ms, end_ms, resolution))
        rows = (data or {}).get("data") or []
        if not rows:
            if data is not None:
                b.miss("dvol", "deribit", "data_unavailable", "empty series")
            return b.build(None, ok=False)
        series = [{"ts": int(x[0]), "open": float(x[1]), "high": float(x[2]), "low": float(x[3]), "close": float(x[4])} for x in rows]
        series.sort(key=lambda s: s["ts"])
        closes = [s["close"] for s in series]
        n = len(closes)
        mean = sum(closes) / n
        var = sum((v - mean) ** 2 for v in closes) / (n - 1) if n > 1 else 0.0
        stdev = math.sqrt(var)
        latest = series[-1]
        latest_ms = latest["ts"]

        def delta_hours(h: int) -> float | None:
            cutoff = latest_ms - h * 3_600_000
            past = [s["close"] for s in series if s["ts"] <= cutoff]
            return latest["close"] - past[-1] if past else None

        return b.build(
            {
                "currency": currency,
                "resolution_seconds": resolution,
                "count": n,
                "latest": {"ts": latest_ms, "value_pct": r(latest["close"], 3)},
                "mean_pct": r(mean, 3),
                "stdev_pct": r(stdev, 3),
                "min_pct": r(min(closes), 3),
                "max_pct": r(max(closes), 3),
                "change_24h_pts": r(delta_hours(24), 3),
                "change_7d_pts": r(delta_hours(168), 3),
                "series": [{"ts": s["ts"], "close": r(s["close"], 3)} for s in series],
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Volatility risk premium for BTC/ETH: Deribit DVOL (30d implied vol index) against realized volatility "
            "of daily spot closes. Returns the current DVOL, realized vol over 7d and 30d, their spread and DVOL's "
            "percentile over the history (an IV rank that needs no stored data); the history's realized premium "
            "(DVOL minus the following 30 days' realized vol); and the record of selling a 30d ATM straddle at "
            "DVOL - `entry_cost_vol` with daily delta hedging (P&L as a fraction of spot per trade), overall and by "
            "DVOL's percentile within the trailing 365 days (`by_dvol_level`, current bucket flagged). Vols are "
            "decimals (0.45 = 45 %). Historical, not a forecast: see `warnings`."
        ),
    )
    async def deribit_vrp(
        currency: CUR = "BTC",
        history_days: Annotated[int, Field(ge=120, le=1000, description="Days of daily DVOL and spot history to use.")] = 1000,
        entry_cost_vol: Annotated[
            float,
            Field(
                ge=0,
                le=0.2,
                description="Vol knocked off DVOL when pricing the sold straddle (DVOL-to-ATM gap + half-spread); 0.02 = 2 vol points.",
            ),
        ] = 0.02,
        price_venue: Annotated[str, Field(description="ccxt spot venue for daily {currency}/USDT closes.")] = "binance",
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(VRP_WARNING)
        end_ms = int(rt.now().timestamp() * 1000)
        start_ms = end_ms - history_days * 86_400_000
        iv: dict[int, float] = {}
        end = end_ms
        for _ in range(5):
            page = await b.guard("dvol", "deribit", rt.deribit.volatility_index_data(currency, start_ms, end, 86400))
            if not page:
                break
            for x in page.get("data") or []:
                iv[int(x[0])] = float(x[4]) / 100.0
            cont = page.get("continuation")
            if not cont or int(cont) <= start_ms or int(cont) >= end:
                break
            end = int(cont)
        bars = await b.guard(
            f"ohlcv:{price_venue}", f"ccxt:{price_venue}", rt.exchanges.ohlcv(price_venue, f"{currency}/USDT", "1d", history_days)
        )
        prices = [(int(x[0]), float(x[4])) for x in (bars or []) if isinstance(x, (list, tuple)) and len(x) >= 5]
        if not iv or not prices:
            if not iv and not any(m.field == "dvol" for m in b.missing):
                b.miss("dvol", "deribit", "data_unavailable", "empty DVOL series")
            if not prices and bars is not None:
                b.miss(f"ohlcv:{price_venue}", f"ccxt:{price_venue}", "data_unavailable", "no daily closes")
            return b.build(None, ok=False)
        rows = align_daily(sorted(iv.items()), prices)
        if not rows:
            b.miss("history", "tradedesk", "data_unavailable", "DVOL and spot closes share no days")
            return b.build(None, ok=False)
        s = vrp_summary(rows, entry_cost_vol=entry_cost_vol)
        cur = s["current"]
        data: dict[str, Any] = {
            "currency": currency,
            "price_venue": price_venue,
            "history": {"days": s["history_days"], "from": _day(rows[0]["day"]), "to": _day(rows[-1]["day"])},
            "current": {
                "date": _day(cur["day"]),
                "dvol": r(cur["iv"], 4),
                "rv_7d": r(cur["rv_7d"], 4),
                "rv_30d": r(cur["rv_30d"], 4),
                "spread_vs_rv30": r(cur["spread_vs_rv30"], 4),
                "dvol_percentile": r(cur["iv_percentile"], 3),
                "dvol_min": r(cur["iv_min"], 4),
                "dvol_max": r(cur["iv_max"], 4),
                "dvol_mean": r(cur["iv_mean"], 4),
                "dvol_percentile_365d": r(cur.get("iv_percentile_365d"), 3),
            },
        }
        if s.get("by_iv_level"):
            p365 = cur.get("iv_percentile_365d")
            data["by_dvol_level"] = [
                {
                    "dvol_percentile_365d": f"{x['percentile_from']:.0%}-{x['percentile_to']:.0%}",
                    "n_starts": x["n_starts"],
                    "mean_straddle_pnl": r(x["mean_pnl"], 4),
                    "share_positive": r(x["share_positive"], 3),
                    "current": _in_bucket(p365, x["percentile_from"], x["percentile_to"]),
                }
                for x in s["by_iv_level"]
            ]
        if "premium" in s:
            pm = s["premium"]
            no = pm["non_overlapping"]
            data["premium"] = {
                "mean_vrp": r(pm["mean_vrp_vol"], 4),
                "share_dvol_above_realized": r(pm["share_iv_above_realized"], 3),
                "mean_dvol": r(pm["mean_iv"], 4),
                "mean_realized_forward": r(pm["mean_rv_forward"], 4),
                "non_overlapping": {
                    "n": no["n"],
                    "mean": r(no.get("mean"), 4),
                    "ci95": [r(x, 4) for x in no["ci95"]] if no.get("ci95") else None,
                },
                "spread_correlation_with_next_premium": r(pm["spread_correlation"], 3),
            }
        else:
            b.warn("history too short for a forward realized-vol window (needs >= 61 consecutive days)")
        if "hedged_short_straddle" in s:
            h = s["hedged_short_straddle"]
            al, no, w = h["all_starts"], h["non_overlapping"], h["worst"]
            data["hedged_short_straddle"] = {
                "entry_cost_vol": entry_cost_vol,
                "mean_premium": r(h["mean_premium"], 4),
                "mean_pnl_all_starts": r(al["mean"], 4),
                "share_positive": r(al["share_positive"], 3),
                "last_365d_mean_pnl": r(h["last_365d_mean"], 4),
                "non_overlapping": {
                    "n": no["n"],
                    "mean": r(no.get("mean"), 4),
                    "ci95": [r(x, 4) for x in no["ci95"]] if no.get("ci95") else None,
                    "sharpe_annual": r(no.get("sharpe"), 2),
                    "max_drawdown": r(no.get("max_drawdown"), 4),
                },
                "worst": {"start": _day(w["day"]), "pnl": r(w["pnl"], 4), "spot_move": r(w["move"], 4), "dvol": r(w["iv"], 4)},
            }
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "ATM implied-vol term structure for a currency (BTC/ETH): one row per Deribit expiry with the "
            "forward, days-to-expiry and ATM IV (decimal, 0.45 = 45 %). ATM IV is the smile value AT the "
            "forward, linear-interpolated in log-moneyness. Rising curve = contango, falling = backwardation."
        ),
    )
    async def deribit_atm_term(
        currency: CUR = "BTC",
        min_days: Annotated[float, Field(ge=0)] = 0.5,
        max_days: Annotated[float, Field(gt=0)] = 400,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await _chain(rt, b, currency)
        if rows is None:
            return b.build(None, ok=False)
        by = group_by_expiry(rows)
        out: list[dict[str, Any]] = []
        for exp, rs in by.items():
            days = (exp - rt.now()).total_seconds() / 86400
            if not (min_days <= days <= max_days):
                continue
            sm = build_smile(rs)
            if sm is None:
                continue
            out.append(
                {
                    "expiry": expiry_token(exp),
                    "days": r(days, 2),
                    "forward": r(sm.forward, 2),
                    "atm_iv": r(atm_iv(sm), 4),
                    "strikes": len(sm.strikes),
                }
            )
        if not out:
            b.miss("atm_term", "deribit", "data_unavailable", "no priced expiries in [min_days, max_days]")
            return b.build(None, ok=False)
        shape = None
        if len(out) >= 2:
            shape = (
                "contango"
                if out[-1]["atm_iv"] > out[0]["atm_iv"]
                else ("backwardation" if out[-1]["atm_iv"] < out[0]["atm_iv"] else "flat")
            )
        return b.build({"currency": currency, "shape": shape, "expiries": out})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Skew term structure for Deribit options (BTC/ETH): per expiry the risk reversal (RR = IV_call - "
            "IV_put at the chosen call-equivalent delta; negative = puts richer, i.e. downside skew) and the "
            "butterfly (fly = (IV_call + IV_put)/2 - ATM_IV; positive = wings richer than ATM). IVs are "
            "decimals. target_delta picks the wings (0.25 -> 25-delta call vs 25-delta put). Also reports the "
            "most-extreme expiry by |RR| and whether the skew is `flattening` / `steepening` / `stable` from "
            "front to back. IV rank/percentile for RR/fly needs stored history (Phase 2)."
        ),
    )
    async def deribit_skew_term(
        currency: CUR = "BTC",
        target_delta: Annotated[float, Field(gt=0, lt=0.5, description="Call-equivalent target delta for the wings.")] = 0.25,
        min_days: Annotated[float, Field(ge=0)] = 0.5,
        max_days: Annotated[float, Field(gt=0)] = 400,
    ) -> dict[str, Any]:
        b = Builder()
        rows = await _chain(rt, b, currency)
        if rows is None:
            return b.build(None, ok=False)
        by = group_by_expiry(rows)
        out: list[dict[str, Any]] = []
        for exp, rs in by.items():
            days = (exp - rt.now()).total_seconds() / 86400
            if not (min_days <= days <= max_days):
                continue
            sm = build_smile(rs)
            if sm is None:
                continue
            rf = rr_fly(sm, target_delta)
            if rf["rr"] is None:
                continue
            out.append(
                {
                    "expiry": expiry_token(exp),
                    "days": r(days, 2),
                    "atm_iv": r(rf["atm"], 4),
                    "iv_call": r(rf["iv_call"], 4),
                    "iv_put": r(rf["iv_put"], 4),
                    "rr": r(rf["rr"], 4),
                    "fly": r(rf["fly"], 4),
                }
            )
        if not out:
            b.miss("skew_term", "deribit", "data_unavailable", "no expiries with a usable smile at the target delta")
            return b.build(None, ok=False)
        most_extreme = max(out, key=lambda x: abs(x["rr"] or 0))
        rr_shape: str | None = None
        if len(out) >= 2 and out[0]["rr"] is not None and out[-1]["rr"] is not None:
            front, back = abs(out[0]["rr"]), abs(out[-1]["rr"])
            rr_shape = "flattening" if back < front * 0.5 else ("steepening" if back > front * 1.5 else "stable")
        return b.build(
            {
                "currency": currency,
                "target_delta": target_delta,
                "most_extreme": {"expiry": most_extreme["expiry"], "rr": most_extreme["rr"]},
                "rr_shape": rr_shape,
                "expiries": out,
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Recent largest trades for Deribit BTC/ETH options or futures via "
            "/public/get_last_trades_by_currency. Each row includes direction (taker side), amount, "
            "native price, IV (for options), notional USD = amount * price * index_price for inverse "
            "options (close enough for sizing), and whether it is a block print (block_trade_id set). "
            "Returns top_n largest trades by notional USD plus a summary (counts, block share, "
            "buy/sell notional, taker imbalance). Flow is INFORMATIVE: block prints may be hedged, "
            "big taker buys do not imply directional conviction."
        ),
    )
    async def deribit_largest_trades(
        currency: CUR = "BTC",
        kind: Literal["option", "future"] = "option",
        count: Annotated[int, Field(ge=1, le=1000, description="How many recent trades to pull from Deribit.")] = 200,
        only_blocks: Annotated[bool, Field(description="If true, keep only trades with a block_trade_id.")] = False,
        min_notional_usd: Annotated[float, Field(ge=0, description="Drop trades smaller than this USD notional.")] = 0.0,
        top_n: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        b = Builder()
        trades = await b.guard("last_trades", "deribit", rt.deribit.last_trades_by_currency(currency, kind, count, False))
        if not trades:
            if trades is not None:
                b.miss("last_trades", "deribit", "data_unavailable", "no trades returned")
            return b.build(None, ok=False)
        rows: list[dict[str, Any]] = []
        for t in trades:
            try:
                amount = float(t.get("amount") or 0)
                price = float(t.get("price") or 0)
                index = float(t.get("index_price") or 0)
            except (TypeError, ValueError):
                continue
            if amount <= 0 or price <= 0:
                continue
            direction = str(t.get("direction") or "").lower()
            if direction not in ("buy", "sell"):
                continue
            # Deribit option prices are quoted in native (BTC/ETH): USD notional ~= amount * price * index.
            # For dated futures price is USD; use amount * price directly.
            notional_usd = amount * price * index if kind == "option" else amount * price
            block_id = t.get("block_trade_id")
            is_block = bool(block_id)
            if only_blocks and not is_block:
                continue
            if notional_usd < min_notional_usd:
                continue
            rows.append(
                {
                    "ts": int(t["timestamp"]) if t.get("timestamp") is not None else None,
                    "instrument": t.get("instrument_name"),
                    "direction": direction,
                    "amount": amount,
                    "price": price,
                    "iv": t.get("iv"),
                    "mark_price": t.get("mark_price"),
                    "index_price": index or None,
                    "notional_usd": r(notional_usd, 0),
                    "is_block": is_block,
                    "block_trade_id": block_id or None,
                }
            )
        if not rows:
            b.miss("last_trades", "deribit", "data_unavailable", "no trades matched the filters")
            return b.build(None, ok=False)

        buy_usd = sum(r_["notional_usd"] for r_ in rows if r_["direction"] == "buy")
        sell_usd = sum(r_["notional_usd"] for r_ in rows if r_["direction"] == "sell")
        total_usd = buy_usd + sell_usd
        imbalance = (buy_usd - sell_usd) / total_usd if total_usd else None
        block_count = sum(1 for r_ in rows if r_["is_block"])
        top = sorted(rows, key=lambda x: x["notional_usd"] or 0, reverse=True)[:top_n]
        return b.build(
            {
                "currency": currency,
                "kind": kind,
                "summary": {
                    "fetched": len(trades),
                    "kept": len(rows),
                    "block_count": block_count,
                    "block_share": r(block_count / len(rows), 4) if rows else None,
                    "buy_notional_usd": r(buy_usd, 0),
                    "sell_notional_usd": r(sell_usd, 0),
                    "taker_imbalance": r(imbalance, 4) if imbalance is not None else None,
                },
                "top_trades": top,
            }
        )
