from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.derivatives import annualize_funding, basis, parse_interval_hours
from ..analytics.instruments import expiry_token, parse_future_name
from ..core.envelope import Builder
from ..providers.exchanges import PERP_EXCHANGES
from ..runtime import Runtime
from ._util import READ_ONLY, r

BASE = Annotated[str, Field(description="Base asset, e.g. BTC, ETH, SOL")]
CCXT_VENUES = Annotated[list[str] | None, Field(description=f"ccxt perp exchange ids; default {list(PERP_EXCHANGES)}")]


def _iso(ms: Any) -> str | None:
    return None if not ms else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Compare perpetual funding across venues (Binance USDT-M, Bybit, OKX via ccxt; Hyperliquid; Deribit for BTC/ETH). "
            "Each row has the rate per funding interval, the interval length, and the ANNUALISED rate (simple, fraction: 0.10 = 10 %), "
            "so venues with 1h/4h/8h intervals are comparable. Also reports the widest spread = cross-venue funding carry."
        ),
    )
    async def perps_funding_compare(
        base: BASE = "BTC",
        exchanges: CCXT_VENUES = None,
        include_hyperliquid: bool = True,
        include_deribit: bool = True,
    ) -> dict[str, Any]:
        b = Builder()
        base = base.upper()
        venues = [e.lower() for e in (exchanges or PERP_EXCHANGES)]
        symbol = f"{base}/USDT:USDT"

        async def ccxt_row(ex: str) -> dict[str, Any] | None:
            fr = await b.guard(f"funding:{ex}", f"ccxt:{ex}", rt.exchanges.funding_rate(ex, symbol))
            if fr is None or fr.get("fundingRate") is None:
                return None
            hours = parse_interval_hours(fr.get("interval"))
            if not fr.get("interval"):
                b.warn(f"{ex}: funding interval not reported, assumed 8h")
            rate = float(fr["fundingRate"])
            return {
                "venue": ex, "rate_per_interval": rate, "interval_hours": hours,
                "annualized": r(annualize_funding(rate, hours), 5),
                "next_funding_utc": _iso(fr.get("nextFundingTimestamp") or fr.get("fundingTimestamp")),
                "mark": fr.get("markPrice"), "index": fr.get("indexPrice"),
            }

        async def hl_row() -> dict[str, Any] | None:
            ctx = await b.guard("funding:hyperliquid", "hyperliquid", rt.hyperliquid.asset_contexts())
            row = next((c for c in ctx or [] if c["coin"] == base), None)
            if row is None or row["funding_1h"] is None:
                if ctx is not None:
                    b.miss("funding:hyperliquid", "hyperliquid", "data_unavailable", f"{base} not listed")
                return None
            return {"venue": "hyperliquid", "rate_per_interval": row["funding_1h"], "interval_hours": 1.0,
                    "annualized": r(row["funding_annualized"], 5), "mark": row["mark_px"], "index": row["oracle_px"]}

        async def deribit_row() -> dict[str, Any] | None:
            if base not in ("BTC", "ETH"):
                return None
            fut = await b.guard("funding:deribit", "deribit", rt.deribit.book_summary(base, "future"))
            perp = next((x for x in fut or [] if x.get("instrument_name", "").endswith("PERPETUAL")), None)
            if not perp or perp.get("funding_8h") is None:
                return None
            rate = float(perp["funding_8h"])
            return {"venue": "deribit", "rate_per_interval": rate, "interval_hours": 8.0,
                    "annualized": r(annualize_funding(rate, 8.0), 5), "mark": perp.get("mark_price"),
                    "index": perp.get("estimated_delivery_price")}

        jobs = [ccxt_row(e) for e in venues]
        if include_hyperliquid:
            jobs.append(hl_row())
        if include_deribit:
            jobs.append(deribit_row())
        rows = [x for x in await asyncio.gather(*jobs) if x]
        if not rows:
            return b.build(None, ok=False)
        rows.sort(key=lambda x: x["annualized"], reverse=True)
        hi, lo = rows[0], rows[-1]
        data = {
            "base": base, "venues": rows,
            "widest_spread": {"long_venue": lo["venue"], "short_venue": hi["venue"],
                              "annualized_spread": r(hi["annualized"] - lo["annualized"], 5),
                              "note": "short the high-funding venue, long the low one, delta-neutral; ignores fees, basis and margin cost"},
        }
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Open interest for a base asset across venues in USD where available "
            "(ccxt exchanges, Hyperliquid, Deribit for BTC/ETH). Use to see where leverage sits."
        ),
    )
    async def perps_open_interest(base: BASE = "BTC", exchanges: CCXT_VENUES = None) -> dict[str, Any]:
        b = Builder()
        base = base.upper()
        symbol = f"{base}/USDT:USDT"

        async def ccxt_oi(ex: str) -> dict[str, Any] | None:
            oi = await b.guard(f"oi:{ex}", f"ccxt:{ex}", rt.exchanges.open_interest(ex, symbol))
            if oi is None:
                return None
            return {"venue": ex, "oi_base": oi.get("openInterestAmount"), "oi_usd": oi.get("openInterestValue")}

        async def hl() -> dict[str, Any] | None:
            ctx = await b.guard("oi:hyperliquid", "hyperliquid", rt.hyperliquid.asset_contexts())
            row = next((c for c in ctx or [] if c["coin"] == base), None)
            return None if not row else {"venue": "hyperliquid", "oi_base": row["open_interest"], "oi_usd": row["open_interest_usd"]}

        async def dbt() -> dict[str, Any] | None:
            if base not in ("BTC", "ETH"):
                return None
            fut = await b.guard("oi:deribit", "deribit", rt.deribit.book_summary(base, "future"))
            perp = next((x for x in fut or [] if x.get("instrument_name", "").endswith("PERPETUAL")), None)
            return None if not perp else {"venue": "deribit", "oi_base": None, "oi_usd": perp.get("open_interest")}

        rows = [x for x in await asyncio.gather(*[ccxt_oi(e.lower()) for e in (exchanges or PERP_EXCHANGES)], hl(), dbt()) if x]
        if not rows:
            return b.build(None, ok=False)
        total = sum(x["oi_usd"] for x in rows if x["oi_usd"])
        for x in rows:
            x["share"] = r(x["oi_usd"] / total, 4) if x["oi_usd"] and total else None
            if x["oi_usd"] is None:
                b.warn(f"{x['venue']}: USD open interest not reported by the exchange")
        rows.sort(key=lambda x: x["oi_usd"] or 0, reverse=True)
        return b.build({"base": base, "total_oi_usd": total, "venues": rows})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Hyperliquid perp universe ranked by a metric: open interest (USD), annualised funding, 24h volume or 24h change. "
            "Hyperliquid funding is hourly; `funding_annualized` is simple (fraction). Good for screening extreme funding / crowded coins."
        ),
    )
    async def hyperliquid_overview(
        sort_by: Literal["open_interest_usd", "funding_annualized", "day_volume_usd", "change_24h_pct"] = "open_interest_usd",
        top_n: Annotated[int, Field(ge=1, le=100)] = 20,
        ascending: bool = False,
        min_oi_usd: Annotated[float, Field(ge=0, description="Filter out illiquid coins")] = 1_000_000,
    ) -> dict[str, Any]:
        b = Builder()
        ctx = await b.guard("hyperliquid_universe", "hyperliquid", rt.hyperliquid.asset_contexts())
        if ctx is None:
            return b.build(None, ok=False)
        rows = [c for c in ctx if (c["open_interest_usd"] or 0) >= min_oi_usd and c.get(sort_by) is not None]
        rows.sort(key=lambda c: c[sort_by], reverse=not ascending)
        out = [{k: (r(v, 6) if isinstance(v, float) else v) for k, v in c.items()} for c in rows[:top_n]]
        return b.build({"sort_by": sort_by, "count_after_filter": len(rows), "coins": out})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Deribit futures term structure for BTC or ETH: the perpetual plus every dated future with basis vs the spot index "
            "(absolute, %, annualised simple and log). Positive annualised basis = contango (cash-and-carry opportunity)."
        ),
    )
    async def deribit_futures_curve(currency: Literal["BTC", "ETH"] = "BTC") -> dict[str, Any]:
        b = Builder()
        fut = await b.guard("futures", "deribit", rt.deribit.book_summary(currency, "future"))
        if fut is None:
            return b.build(None, ok=False)
        now = rt.now()
        curve: list[dict[str, Any]] = []
        perp: dict[str, Any] | None = None
        for f in fut:
            parsed = parse_future_name(f.get("instrument_name", ""))
            idx, mark = f.get("estimated_delivery_price"), f.get("mark_price")
            if not parsed or not idx or not mark:
                continue
            if parsed[1] is None:
                perp = {"instrument": f["instrument_name"], "mark": mark, "index": idx,
                        "basis_pct": r((mark / idx - 1) * 100, 4), "funding_8h": f.get("funding_8h"),
                        "funding_annualized": r(annualize_funding(f["funding_8h"], 8.0), 5) if f.get("funding_8h") is not None else None,
                        "open_interest_usd": f.get("open_interest")}
                continue
            bs = basis(mark, idx, parsed[1], now)
            curve.append({"instrument": f["instrument_name"], "expiry": expiry_token(parsed[1]), "mark": mark,
                          "days": bs["days_to_expiry"], "basis_abs": r(bs["basis_abs"], 2), "basis_pct": r(bs["basis_pct"] * 100, 4),
                          "annualized_simple": r(bs["annualized_simple"], 4), "annualized_log": r(bs["annualized_log"], 4),
                          "open_interest_usd": f.get("open_interest"), "volume_usd": f.get("volume_usd")})
        curve.sort(key=lambda x: x["days"])
        index = next((f.get("estimated_delivery_price") for f in fut if f.get("estimated_delivery_price")), None)
        return b.build({"currency": currency, "spot_index": index, "perpetual": perp, "futures": curve})
