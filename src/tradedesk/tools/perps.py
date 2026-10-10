from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.derivatives import annualize_funding, basis, parse_interval_hours, summarise_funding_history
from ..analytics.instruments import expiry_token, parse_future_name
from ..analytics.orderbook import book_stats, walk_book
from ..analytics.trades import summarise as summarise_trades
from ..core.envelope import Builder
from ..providers.exchanges import PERP_EXCHANGES
from ..runtime import Runtime
from ._util import READ_ONLY, r, timeframe_seconds

BASE = Annotated[str, Field(description="Base asset, e.g. BTC, ETH, SOL")]
CCXT_VENUES = Annotated[list[str] | None, Field(description=f"ccxt perp exchange ids; default {list(PERP_EXCHANGES)}")]


def _iso(ms: Any) -> str | None:
    return None if not ms else datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


def _row_from_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Project summarise_funding_history output into the per-venue row shape used by perps_funding_history."""
    return {
        "count": stats.get("count"),
        "interval_hours": stats.get("interval_hours"),
        "latest_rate": stats.get("latest_rate"),
        "latest_annualized": r(stats.get("latest_annualized"), 6),
        "mean_rate": r(stats.get("mean_rate"), 8),
        "stdev_rate": r(stats.get("stdev_rate"), 8),
        "min_rate": r(stats.get("min_rate"), 8),
        "max_rate": r(stats.get("max_rate"), 8),
        "mean_7d": r(stats.get("mean_7d"), 8),
        "mean_30d": r(stats.get("mean_30d"), 8),
        "carry_annualized": r(stats.get("carry_annualized"), 6),
        "z_score_latest": r(stats.get("z_score_latest"), 4),
        "percentile_latest": r(stats.get("percentile_latest"), 4),
        "polarity_flips": stats.get("polarity_flips"),
        "first_ts_ms": stats.get("first_ts_ms"),
        "last_ts_ms": stats.get("last_ts_ms"),
    }


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
                "venue": ex,
                "rate_per_interval": rate,
                "interval_hours": hours,
                "annualized": r(annualize_funding(rate, hours), 5),
                "next_funding_utc": _iso(fr.get("nextFundingTimestamp") or fr.get("fundingTimestamp")),
                "mark": fr.get("markPrice"),
                "index": fr.get("indexPrice"),
            }

        async def hl_row() -> dict[str, Any] | None:
            ctx = await b.guard("funding:hyperliquid", "hyperliquid", rt.hyperliquid.asset_contexts())
            row = next((c for c in ctx or [] if c["coin"] == base), None)
            if row is None or row["funding_1h"] is None:
                if ctx is not None:
                    b.miss("funding:hyperliquid", "hyperliquid", "data_unavailable", f"{base} not listed")
                return None
            return {
                "venue": "hyperliquid",
                "rate_per_interval": row["funding_1h"],
                "interval_hours": 1.0,
                "annualized": r(row["funding_annualized"], 5),
                "mark": row["mark_px"],
                "index": row["oracle_px"],
            }

        async def deribit_row() -> dict[str, Any] | None:
            if base not in ("BTC", "ETH"):
                return None
            fut = await b.guard("funding:deribit", "deribit", rt.deribit.book_summary(base, "future"))
            perp = next((x for x in fut or [] if x.get("instrument_name", "").endswith("PERPETUAL")), None)
            if not perp or perp.get("funding_8h") is None:
                return None
            rate = float(perp["funding_8h"])
            return {
                "venue": "deribit",
                "rate_per_interval": rate,
                "interval_hours": 8.0,
                "annualized": r(annualize_funding(rate, 8.0), 5),
                "mark": perp.get("mark_price"),
                "index": perp.get("estimated_delivery_price"),
            }

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
            "base": base,
            "venues": rows,
            "widest_spread": {
                "long_venue": lo["venue"],
                "short_venue": hi["venue"],
                "annualized_spread": r(hi["annualized"] - lo["annualized"], 5),
                "note": "short the high-funding venue, long the low one, delta-neutral; ignores fees, basis and margin cost",
            },
        }
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Historical perp funding across venues (Binance USDT-M, Bybit, OKX via ccxt; Hyperliquid). "
            "For each venue returns: count, interval (auto-detected from the median timestamp spacing), latest rate per interval and annualised, "
            "mean/stdev/min/max of the rate, 7d and 30d means, carry APR (= mean rate × 8760 / interval_hours), z-score and percentile of the latest "
            "value vs the whole window, polarity flips. Plus a cross-venue summary (highest/lowest carry, biggest absolute z). "
            "Set `include_series=true` to also receive each venue's raw [ts_ms, rate] rows."
        ),
    )
    async def perps_funding_history(
        base: BASE = "BTC",
        exchanges: CCXT_VENUES = None,
        include_hyperliquid: bool = True,
        days_back: Annotated[float, Field(gt=0, le=180)] = 30.0,
        include_series: bool = False,
    ) -> dict[str, Any]:
        b = Builder()
        base = base.upper()
        symbol = f"{base}/USDT:USDT"
        venues = [e.lower() for e in (exchanges or PERP_EXCHANGES)]
        now_ms = int(rt.now().timestamp() * 1000)
        since_ms = now_ms - int(days_back * 86_400_000)
        limit = 500

        async def ccxt_row(ex: str) -> dict[str, Any] | None:
            raw = await b.guard(f"funding_history:{ex}", f"ccxt:{ex}", rt.exchanges.funding_rate_history(ex, symbol, since_ms, limit))
            if not raw:
                return None
            entries = [
                {"ts_ms": int(x["timestamp"]), "rate": float(x["fundingRate"])}
                for x in raw
                if x.get("fundingRate") is not None and x.get("timestamp")
            ]
            if not entries:
                return None
            stats = summarise_funding_history(entries, now_ms=now_ms)
            return {"venue": ex, **_row_from_stats(stats), **({"series": entries} if include_series else {})}

        async def hl_row() -> dict[str, Any] | None:
            raw = await b.guard("funding_history:hyperliquid", "hyperliquid", rt.hyperliquid.funding_history(base, since_ms, now_ms))
            if not raw:
                return None
            entries = [
                {"ts_ms": int(x["time"]), "rate": float(x["fundingRate"])}
                for x in raw
                if x.get("fundingRate") is not None and x.get("time")
            ]
            if not entries:
                return None
            if len(raw) >= 500 and days_back > 500 / 24:
                b.warn(f"hyperliquid funding history capped at {len(raw)} rows (API limit); window truncated — pagination TBD")
            stats = summarise_funding_history(entries, interval_hours=1.0, now_ms=now_ms)
            return {"venue": "hyperliquid", **_row_from_stats(stats), **({"series": entries} if include_series else {})}

        jobs: list[Any] = [ccxt_row(e) for e in venues]
        if include_hyperliquid:
            jobs.append(hl_row())
        rows = [x for x in await asyncio.gather(*jobs) if x]
        if not rows:
            return b.build(None, ok=False)

        def pick(key: str, reverse: bool) -> dict[str, Any] | None:
            candidates = [x for x in rows if x.get(key) is not None]
            if not candidates:
                return None
            v = max(candidates, key=lambda x: x[key]) if reverse else min(candidates, key=lambda x: x[key])
            return {"venue": v["venue"], key: r(v[key], 6)}

        rows.sort(key=lambda x: x.get("carry_annualized") or 0, reverse=True)
        cross = {
            "highest_carry": pick("carry_annualized", reverse=True),
            "lowest_carry": pick("carry_annualized", reverse=False),
            "biggest_abs_z": None,
        }
        abs_z = [x for x in rows if x.get("z_score_latest") is not None]
        if abs_z:
            v = max(abs_z, key=lambda x: abs(x["z_score_latest"]))
            cross["biggest_abs_z"] = {"venue": v["venue"], "z_score_latest": r(v["z_score_latest"], 3)}

        return b.build({"base": base, "days_requested": days_back, "venues": rows, "cross_venue": cross})

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
            "Hyperliquid OHLCV candles for one coin plus period return and annualised close-to-close realised volatility. "
            "`interval` is HL-shaped: 1m/3m/5m/15m/30m/1h/2h/4h/8h/12h/1d/3d/1w/1M. `hours_back` sets the lookback window."
        ),
    )
    async def hyperliquid_candles(
        coin: str,
        interval: str = "1h",
        hours_back: Annotated[float, Field(gt=0, le=24 * 365)] = 24.0,
    ) -> dict[str, Any]:
        b = Builder()
        try:
            tf_sec = timeframe_seconds(interval)
        except ValueError as e:
            b.miss("interval", "tradedesk", "config", str(e))
            return b.build(None, ok=False)
        end_ms = int(rt.now().timestamp() * 1000)
        start_ms = end_ms - int(hours_back * 3_600_000)
        raw = await b.guard("candles", "hyperliquid", rt.hyperliquid.candles(coin, interval, start_ms, end_ms))
        if not raw:
            if raw is not None:
                b.miss("candles", "hyperliquid", "data_unavailable", "empty result")
            return b.build(None, ok=False)
        rows: list[list[float]] = []
        for c in raw:
            try:
                rows.append([int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"])])
            except (KeyError, TypeError, ValueError):
                continue
        if not rows:
            b.miss("candles", "hyperliquid", "data_unavailable", "no parsable candles")
            return b.build(None, ok=False)
        rows.sort(key=lambda x: x[0])
        closes = [row[4] for row in rows if row[4]]
        summary: dict[str, Any] = {"candles": len(rows), "first_close": closes[0], "last_close": closes[-1]}
        summary["return_pct"] = r((closes[-1] / closes[0] - 1) * 100, 3)
        if len(closes) >= 10:
            lr = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
            mean = sum(lr) / len(lr)
            var = sum((x - mean) ** 2 for x in lr) / (len(lr) - 1)
            summary["realized_vol_annualized"] = r(math.sqrt(var) * math.sqrt(365 * 86400 / tf_sec), 4)
        return b.build(
            {
                "coin": coin.upper(),
                "interval": interval,
                "columns": ["ts_ms", "open", "high", "low", "close", "volume"],
                "rows": rows,
                "summary": summary,
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Hyperliquid L2 order book snapshot (20 levels per side): mid, spread (bps), bid/ask depth imbalance within 0.1/0.5/1 % of mid. "
            "If `notional_usd` is given, also estimates average fill price and slippage for a market buy and sell of that size."
        ),
    )
    async def hyperliquid_book(
        coin: str,
        notional_usd: Annotated[float | None, Field(gt=0, description="Order size in USD")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        raw = await b.guard("book", "hyperliquid", rt.hyperliquid.l2_book(coin))
        if not raw:
            if raw is not None:
                b.miss("book", "hyperliquid", "data_unavailable", "empty result")
            return b.build(None, ok=False)
        try:
            bids_raw, asks_raw = raw["levels"][0], raw["levels"][1]
        except (KeyError, IndexError, TypeError):
            b.miss("book", "hyperliquid", "backend_error", "unexpected book shape")
            return b.build(None, ok=False)

        def levels(xs: list[dict[str, Any]]) -> list[list[float]]:
            out: list[list[float]] = []
            for lv in xs:
                try:
                    out.append([float(lv["px"]), float(lv["sz"]), float(lv.get("n") or 0)])
                except (KeyError, TypeError, ValueError):
                    continue
            return out

        bids, asks = levels(bids_raw), levels(asks_raw)
        if not bids or not asks:
            b.miss("book", "hyperliquid", "data_unavailable", "no quotable levels")
            return b.build(None, ok=False)
        stats = book_stats(bids, asks)
        data: dict[str, Any] = {
            "coin": coin.upper(),
            "stats": stats,
            "bids_top": bids[:10],
            "asks_top": asks[:10],
            "levels_loaded": [len(bids), len(asks)],
        }
        if notional_usd and stats["mid"]:
            data["impact"] = {
                "buy": walk_book(asks, notional_usd, stats["mid"], "buy"),
                "sell": walk_book(bids, notional_usd, stats["mid"], "sell"),
            }
            if data["impact"]["buy"]["unfilled_usd"] > 0 or data["impact"]["sell"]["unfilled_usd"] > 0:
                b.warn("only 20 levels available per side on Hyperliquid; large orders may not size here")
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Hyperliquid recent public trades with aggressor-signed flow: buy/sell counts and notional, imbalance in [-1, 1] (positive = net taker buys), "
            "VWAP for all / buys / sells, cumulative delta end value, and the largest `top_n` prints. HL `side` field: 'B' = taker buy, 'A' = taker sell. "
            "The endpoint returns up to ~500 recent fills; HL may return fewer when activity is low."
        ),
    )
    async def hyperliquid_trades(coin: str, top_n: Annotated[int, Field(ge=1, le=50)] = 5) -> dict[str, Any]:
        b = Builder()
        raw = await b.guard("trades", "hyperliquid", rt.hyperliquid.recent_trades(coin))
        if not raw:
            if raw is not None:
                b.miss("trades", "hyperliquid", "data_unavailable", "empty result")
            return b.build(None, ok=False)
        normalised: list[dict[str, Any]] = []
        for t in raw:
            side_code = str(t.get("side") or "").upper()
            if side_code not in ("B", "A"):
                continue
            try:
                price = float(t["px"])
                amount = float(t["sz"])
            except (KeyError, TypeError, ValueError):
                continue
            normalised.append(
                {
                    "timestamp": int(t.get("time") or 0),
                    "side": "buy" if side_code == "B" else "sell",
                    "price": price,
                    "amount": amount,
                    "cost": price * amount,
                }
            )
        if not normalised:
            b.miss("trades", "hyperliquid", "data_unavailable", "no usable trades")
            return b.build(None, ok=False)
        summary = summarise_trades(normalised, top_n=top_n)
        return b.build({"coin": coin.upper(), "fetched": len(raw), "parsed": len(normalised), "summary": summary})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Hyperliquid funding-rate history for one coin over a lookback window (default 30 days, interval 1h). Returns the raw series plus "
            "mean/stdev/min/max/7d-mean/30d-mean of the per-hour rate, annualised carry (mean rate × 8760), the latest rate's z-score and percentile "
            "relative to the window, and polarity flips (sign changes). Rates are decimals (0.0001 = 0.01 % per hour)."
        ),
    )
    async def hyperliquid_funding_history(
        coin: str,
        days_back: Annotated[float, Field(gt=0, le=180)] = 30.0,
    ) -> dict[str, Any]:
        b = Builder()
        now_ms = int(rt.now().timestamp() * 1000)
        start_ms = now_ms - int(days_back * 86_400_000)
        raw = await b.guard("funding_history", "hyperliquid", rt.hyperliquid.funding_history(coin, start_ms, now_ms))
        if not raw:
            if raw is not None:
                b.miss("funding_history", "hyperliquid", "data_unavailable", "empty result")
            return b.build(None, ok=False)
        entries: list[dict[str, Any]] = []
        for e in raw:
            try:
                entries.append({"ts_ms": int(e["time"]), "rate": float(e["fundingRate"])})
            except (KeyError, TypeError, ValueError):
                continue
        if not entries:
            b.miss("funding_history", "hyperliquid", "data_unavailable", "no parsable entries")
            return b.build(None, ok=False)
        stats = summarise_funding_history(entries, interval_hours=1.0, now_ms=now_ms)
        return b.build({"coin": coin.upper(), "days_requested": days_back, "stats": stats, "entries": entries})

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
                perp = {
                    "instrument": f["instrument_name"],
                    "mark": mark,
                    "index": idx,
                    "basis_pct": r((mark / idx - 1) * 100, 4),
                    "funding_8h": f.get("funding_8h"),
                    "funding_annualized": r(annualize_funding(f["funding_8h"], 8.0), 5) if f.get("funding_8h") is not None else None,
                    "open_interest_usd": f.get("open_interest"),
                }
                continue
            bs = basis(mark, idx, parsed[1], now)
            curve.append(
                {
                    "instrument": f["instrument_name"],
                    "expiry": expiry_token(parsed[1]),
                    "mark": mark,
                    "days": bs["days_to_expiry"],
                    "basis_abs": r(bs["basis_abs"], 2),
                    "basis_pct": r(bs["basis_pct"] * 100, 4),
                    "annualized_simple": r(bs["annualized_simple"], 4),
                    "annualized_log": r(bs["annualized_log"], 4),
                    "open_interest_usd": f.get("open_interest"),
                    "volume_usd": f.get("volume_usd"),
                }
            )
        curve.sort(key=lambda x: x["days"])
        index = next((f.get("estimated_delivery_price") for f in fut if f.get("estimated_delivery_price")), None)
        return b.build({"currency": currency, "spot_index": index, "perpetual": perp, "futures": curve})
