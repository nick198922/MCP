from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.carry import carry_snapshot, spread_snapshot
from ..analytics.derivatives import annualize_funding, basis, parse_interval_hours, summarise_funding_history
from ..analytics.funding_spread import normalize_asset
from ..analytics.instruments import expiry_token, parse_future_name
from ..analytics.orderbook import book_stats, walk_book
from ..analytics.trades import summarise as summarise_trades
from ..core.envelope import Builder
from ..providers.exchanges import PERP_EXCHANGES
from ..runtime import Runtime
from ._util import READ_ONLY, r, timeframe_seconds

BASE = Annotated[str, Field(description="Base asset, e.g. BTC, ETH, SOL")]
SPREAD_WARNING = (
    "Backtest (docs/verification/2026-10-13-funding-carry-perp-perp.md, Binance<->Bybit, 2024-10..2026-10): entering "
    "at a trailing 7d spread >= 10 % and exiting at <= 0 has an expectation of about zero or negative on the full funding "
    "history (-8.7 % APR on notional at 2x with stop, +2.9 % without stop; docs/verification/2026-10-17-paper-carry-bench.md; "
    "the earlier +76..90 % came from holes in Bybit funding history). Majors earned nothing. An older report on the holed "
    "history found one leg moving >= 33 % against itself in 46 % of trades (not recomputed on the full history; a 3x "
    "isolated leg is liquidated without a top-up). Not "
    "modelled: per-symbol position limits, venues changing caps / intervals / leverage mid-event, separate margin on "
    "each venue, liquidation fees. Paper-trade first: log entries with journal_add(tags=[...]) and check journal_stats."
)
TAKER_FEES = {"binanceusdm": 0.0005, "bybit": 0.00055, "okx": 0.0005, "hyperliquid": 0.00045}
HL_FUNDING_CAP = 0.04  # Hyperliquid caps funding at 4 % per hour
CARRY_WARNING = (
    "Funding carry is not risk-free: funding can flip negative, the perp premium moves, a pump can liquidate the "
    "short perp unless margin is topped up, and the venue is a counterparty. Backtests (docs/verification/, Binance, "
    "2024-10..2026-10, APRs on notional after fees): top-20 majors ~3-5 % (~0.3 % in the last 12 months); all crypto "
    "perps with spot, 5 slots, enter at trailing 7d >= 20 %: ~6.5 %, but 21 % of trades saw the perp rise >= 100 % "
    "while open and a handful of memecoins made most of the profit. On real capital (spot + perp margin) roughly "
    "3-5 %. Funding >= 50-100 % APR mean-reverts within days. Compare with a risk-free stablecoin or T-bill yield."
)
CCXT_VENUES = Annotated[list[str] | None, Field(description=f"ccxt perp exchange ids; default {list(PERP_EXCHANGES)}")]

Period = Literal["5m", "15m", "30m", "1h", "4h", "1d"]
_PERIOD_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
_PERIOD_BINANCE = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d"}
_PERIOD_BYBIT = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1d"}


def _series_stats(values: list[float], step_seconds: int) -> dict[str, Any]:
    """Minimum / mean / max / latest / change over last 24h-worth of entries."""
    if not values:
        return {"count": 0, "latest": None, "mean": None, "min": None, "max": None, "change_24h": None, "z_score_latest": None}
    n = len(values)
    mean = sum(values) / n
    latest = values[-1]
    out: dict[str, Any] = {"count": n, "latest": latest, "mean": mean, "min": min(values), "max": max(values)}
    steps_24h = max(1, 86400 // step_seconds)
    if n > steps_24h:
        out["change_24h"] = latest - values[-1 - steps_24h]
    else:
        out["change_24h"] = latest - values[0]
    if n > 1:
        var = sum((v - mean) ** 2 for v in values) / (n - 1)
        sd = var**0.5
        out["z_score_latest"] = (latest - mean) / sd if sd > 0 else None
    else:
        out["z_score_latest"] = None
    return out


def _iso(ms: Any) -> str | None:
    return None if not ms else datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


def _warn_short_coverage(b: Builder, venue: str, entries: list[dict[str, Any]], since_ms: int) -> None:
    """Flag history that starts well after the requested window (venue retention, e.g. OKX ~3 months, or a later listing)."""
    first = min(e["ts_ms"] for e in entries)
    if first - since_ms > 2 * 86_400_000:
        b.warn(f"{venue} funding history starts {_iso(first)[:10]}, later than requested (venue retention or listing date)")


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


async def current_funding(rt: Runtime, b: Builder, venue: str, markets: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Current funding per USDT perp with the right interval (bulk Binance rows omit it) and the rate cap when known."""
    bulk = await b.guard(f"funding_rates:{venue}", f"ccxt:{venue}", rt.exchanges.funding_rates(venue))
    if not bulk:
        return {}
    meta: dict[str, Any] = {}
    if any(not (x or {}).get("interval") for x in bulk.values()):
        meta = await b.guard(f"funding_intervals:{venue}", f"ccxt:{venue}", rt.exchanges.funding_intervals(venue)) or {}
        if not meta:
            b.warn(f"{venue}: funding intervals unavailable; 8h shown (interval_source=assumed) where the venue omits it")
    out: dict[str, dict[str, Any]] = {}
    for sym, x in bulk.items():
        if not sym.endswith("/USDT:USDT") or (x or {}).get("fundingRate") is None:
            continue
        m = meta.get(sym) or {}
        raw_interval = x.get("interval") or m.get("interval")
        hours = parse_interval_hours(raw_interval)
        # "venue": reported by the venue; "default": the venue's interval list was read and omits the symbol (Binance lists
        # only non-default ones, so 8h); "assumed": no interval source at all (the 8h is a guess).
        source = "venue" if raw_interval else ("default" if meta else "assumed")
        info = ((markets or {}).get(sym) or {}).get("info") or {}
        cap = (m.get("info") or {}).get("adjustedFundingRateCap") or info.get("upperFundingRate")
        rate = float(x["fundingRate"])
        out[sym] = {
            "rate": rate,
            "interval_hours": hours,
            "interval_source": source,
            "apr": annualize_funding(rate, hours),
            "next_funding_ts": x.get("nextFundingTimestamp"),
            "mark": float(x["markPrice"]) if x.get("markPrice") else None,
            "cap": float(cap) if cap not in (None, "") else None,
        }
    return out


def hl_symbol(coin: str) -> str:
    """Hyperliquid coin -> the ccxt-style key CEXes use for the same instrument (kPEPE -> 1000PEPE/USDT:USDT)."""
    asset, mult = normalize_asset(coin, "hyperliquid")
    return f"{mult if mult > 1 else ''}{asset}/USDT:USDT"


async def hl_current_funding(rt: Runtime, b: Builder) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Hyperliquid current funding keyed like the CEX symbols, plus key -> native HL coin name.

    Funding is hourly: ``apr`` = hourly rate x 8760 (decimal, simple). ``mark`` is in the same unit as the CEX
    contract (kPEPE quotes 1000 PEPE, like 1000PEPE).
    """
    rows = await b.guard("asset_contexts:hyperliquid", "hyperliquid", rt.hyperliquid.asset_contexts())
    out: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    for x in rows or []:
        if x.get("funding_1h") is None:
            continue
        key = hl_symbol(x["coin"])
        if key in names:  # two HL names for one instrument: ambiguous, drop both
            out.pop(key, None)
            names[key] = ""
            continue
        names[key] = x["coin"]
        out[key] = {
            "rate": x["funding_1h"],
            "interval_hours": 1.0,
            "interval_source": "venue",
            "apr": x["funding_1h"] * 8760.0,
            "next_funding_ts": None,
            "mark": x["mark_px"],
            "cap": HL_FUNDING_CAP,
        }
    return out, {k: v for k, v in names.items() if v}


def register(server: MCPServer, rt: Runtime) -> None:
    async def _hl_name(b: Builder, coin: str) -> str:
        """Resolve the case-sensitive HL name once per tool call; warn when the universe could not confirm it."""
        name, status = await rt.hyperliquid.lookup_coin(coin)
        if status == "unavailable":
            b.warn(f"hyperliquid universe unavailable; coin name sent as '{name}' (case-sensitive names like kPEPE may fail)")
        elif status == "unknown":
            b.warn(f"'{coin.strip()}' not found in the hyperliquid universe; sent as '{name}'")
        return name

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
            "mean/stdev/min/max of the rate, 7d and 30d means, carry APR (= sum of actual payments over the covered span, annualised; robust to interval changes), z-score and percentile of the latest "
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

        async def ccxt_row(ex: str) -> dict[str, Any] | None:
            got = await b.guard(
                f"funding_history:{ex}", f"ccxt:{ex}", rt.exchanges.funding_rate_history_range(ex, symbol, since_ms, now_ms)
            )
            if not got:
                return None
            if not got["complete"]:
                b.warn(f"{ex} funding history incomplete (page limit or a later page failed); window truncated")
            entries = [
                {"ts_ms": int(x["timestamp"]), "rate": float(x["fundingRate"])}
                for x in got["rows"]
                if x.get("fundingRate") is not None and x.get("timestamp")
            ]
            if not entries:
                return None
            _warn_short_coverage(b, ex, entries, since_ms)
            stats = summarise_funding_history(entries, now_ms=now_ms)
            return {"venue": ex, **_row_from_stats(stats), **({"series": entries} if include_series else {})}

        async def hl_row() -> dict[str, Any] | None:
            hl_base = await _hl_name(b, base)
            got = await b.guard(
                "funding_history:hyperliquid", "hyperliquid", rt.hyperliquid.funding_history_range(hl_base, since_ms, now_ms)
            )
            if not got:
                return None
            if not got["complete"]:
                b.warn("hyperliquid funding history incomplete (page limit or a later page failed); window truncated")
            entries = [
                {"ts_ms": int(x["time"]), "rate": float(x["fundingRate"])}
                for x in got["rows"]
                if x.get("fundingRate") is not None and x.get("time")
            ]
            if not entries:
                return None
            _warn_short_coverage(b, "hyperliquid", entries, since_ms)
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

    async def _current_funding(b: Builder, venue: str, markets: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        return await current_funding(rt, b, venue, markets)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Scan perps on one venue for funding carry (short perp collects funding; cash-and-carry pairs it with "
            "long spot). Per symbol: current funding and its APR, trailing realized APR over `lookback_days` and 30d "
            "(sum of actual payments over real time, robust to 8h/4h/1h interval changes), share of positive payments, "
            "status vs `entry_apr` / `exit_apr` (above_entry / between / below_exit), `breakeven_days` = days the "
            "trailing APR must persist to pay `roundtrip_fee`, and whether a spot market exists on `spot_venue`. "
            "Without `bases` the venue's `candidates` perps with the highest current funding are scanned, by default "
            "only crypto perps (TradFi perps skipped) that also have a spot market. "
            "All rates and APRs are decimals (0.10 = 10 %), on position notional; capital needed is higher "
            "(spot leg + perp margin). Carry is not risk-free: see `warnings`."
        ),
    )
    async def funding_carry_scan(
        venue: Annotated[Literal["binanceusdm", "bybit", "okx"], Field(description="ccxt perp venue.")] = "binanceusdm",
        bases: Annotated[
            list[str] | None,
            Field(description="Bases to scan, e.g. ['BTC','ETH']; default: top `candidates` by current funding.", max_length=50),
        ] = None,
        candidates: Annotated[int, Field(ge=1, le=50, description="How many perps to scan when `bases` is empty.")] = 20,
        lookback_days: Annotated[float, Field(ge=1, le=30, description="Trailing window for the realized APR, days.")] = 7.0,
        entry_apr: Annotated[
            float,
            Field(
                ge=0,
                le=5,
                description="Trailing APR at or above which carry is 'above_entry'. Binance's baseline 0.01 % per 8h is ~10.95 %.",
            ),
        ] = 0.20,
        exit_apr: Annotated[float, Field(ge=-1, le=5, description="Trailing APR at or below which carry is 'below_exit'.")] = 0.0,
        roundtrip_fee: Annotated[
            float,
            Field(
                ge=0, le=0.05, description="Entry + exit fees on both legs, decimal of notional (0.003 = spot 0.10 % + perp 0.05 %, twice)."
            ),
        ] = 0.003,
        spot_venue: Annotated[str | None, Field(description="ccxt spot venue checked for a USDT spot market; null to skip.")] = "binance",
        crypto_only: Annotated[
            bool, Field(description="When picking candidates, skip TradFi perps (commodities, equities, ETFs) flagged by the venue.")
        ] = True,
        require_spot: Annotated[
            bool, Field(description="When picking candidates, keep only bases with a spot market on `spot_venue` (cash-and-carry).")
        ] = True,
        max_concurrency: Annotated[int, Field(ge=1, le=16, description="Parallel funding-history requests.")] = 4,
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(CARRY_WARNING)
        now_ms = int(rt.now().timestamp() * 1000)
        current = await _current_funding(b, venue)
        spot: set[str] | None = None
        if spot_venue:
            markets = await b.guard("spot_markets", f"ccxt:{spot_venue}", rt.exchanges.markets(spot_venue))
            if markets:
                spot = {m for m, v in markets.items() if (v or {}).get("type") == "spot" and (v or {}).get("active") is not False}
        tradfi: set[str] | None = None
        perp_markets = await b.guard("perp_markets", f"ccxt:{venue}", rt.exchanges.markets(venue))
        if perp_markets:
            tradfi = set()
            for m, v in perp_markets.items():
                info = (v or {}).get("info") or {}
                if str(info.get("contractType") or "").startswith("TRADIFI") or info.get("underlyingType") not in (None, "COIN"):
                    tradfi.add(m)

        if bases:
            symbols = [f"{x.upper()}/USDT:USDT" for x in bases]
        elif current:
            pool = list(current)
            if crypto_only:
                if tradfi is None:
                    b.warn("perp market metadata unavailable; TradFi perps could not be filtered out")
                else:
                    pool = [k for k in pool if k not in tradfi]
            if require_spot:
                if spot is None:
                    b.warn("spot markets unavailable; candidates were not restricted to bases with spot")
                else:
                    pool = [k for k in pool if f"{k.split('/')[0]}/USDT" in spot]
            symbols = sorted(pool, key=lambda k: current[k]["apr"], reverse=True)[:candidates]
            if not symbols:
                b.miss("candidates", "tradedesk", "data_unavailable", "no perps left after crypto_only / require_spot filters")
                return b.build(None, ok=False)
        else:
            return b.build(None, ok=False)

        sem = asyncio.Semaphore(max_concurrency)
        since_ms = now_ms - int(max(30.0, lookback_days) * 86_400_000)

        async def row(sym: str) -> dict[str, Any] | None:
            async with sem:
                got = await b.guard(
                    f"funding_history:{sym}", f"ccxt:{venue}", rt.exchanges.funding_rate_history_range(venue, sym, since_ms, now_ms)
                )
            if got and not got["complete"]:
                b.warn(f"{sym}: funding history incomplete (page limit or a later page failed)")
            entries = [
                {"ts_ms": int(x["timestamp"]), "rate": float(x["fundingRate"])}
                for x in (got["rows"] if got else [])
                if x.get("fundingRate") is not None and x.get("timestamp")
            ]
            cur = current.get(sym)
            if not entries and not cur:
                return None
            snap = carry_snapshot(
                entries, now_ms=now_ms, lookback_days=lookback_days, entry_apr=entry_apr, exit_apr=exit_apr, roundtrip_fee=roundtrip_fee
            )
            base = sym.split("/")[0]
            return {
                "base": base,
                "symbol": sym,
                "current_rate": r(cur["rate"], 8) if cur else None,
                "interval_hours": cur["interval_hours"] if cur else None,
                "current_apr": r(cur["apr"], 4) if cur else None,
                "next_funding": _iso(cur["next_funding_ts"]) if cur else None,
                "trailing_apr": r(snap.get("trailing_apr"), 4),
                "trailing_30d_apr": r(snap.get("trailing_30d_apr"), 4),
                "positive_share": r(snap.get("positive_share"), 3),
                "status": snap.get("status"),
                "breakeven_days": r(snap.get("breakeven_days"), 1),
                "coverage_days": r(snap.get("coverage_days"), 1),
                "spot_available": (f"{base}/USDT" in spot) if spot is not None else None,
                "tradfi": (sym in tradfi) if tradfi is not None else None,
            }

        rows = [x for x in await asyncio.gather(*[row(s) for s in symbols]) if x]
        if not rows:
            return b.build(None, ok=False)
        rows.sort(key=lambda x: x["trailing_apr"] if x["trailing_apr"] is not None else float("-inf"), reverse=True)
        trailing = sorted(x["trailing_apr"] for x in rows if x["trailing_apr"] is not None)
        above = [x["base"] for x in rows if x["status"] == "above_entry"]
        return b.build(
            {
                "venue": venue,
                "lookback_days": lookback_days,
                "thresholds": {"entry_apr": entry_apr, "exit_apr": exit_apr, "roundtrip_fee": roundtrip_fee},
                "summary": {
                    "scanned": len(rows),
                    "above_entry": above,
                    "above_entry_with_spot": [x["base"] for x in rows if x["status"] == "above_entry" and x["spot_available"]],
                    "median_trailing_apr": r(trailing[len(trailing) // 2], 4) if trailing else None,
                },
                "rows": rows,
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Cross-venue funding spread scan for perp-perp carry: short the perp on the venue with the higher trailing "
            "funding, long it on the venue with the lower one. Per base: each venue's current funding (interval-correct) "
            "and trailing realized APR over `lookback_days` and 30d; the best pair; trailing and current spread; status vs "
            "`entry_apr` / `exit_apr`; round-trip cost (taker fees binanceusdm 0.05 %, bybit 0.055 %, okx 0.05 % plus "
            "`slippage` per leg, entry and exit) and the days of trailing spread needed to pay it; the mark-price gap "
            "between venues (null when a venue's bulk funding has no mark price, e.g. OKX); the 7-day high/low range of the short venue's perp; and flags: price_mismatch (likely different "
            "tokens under one ticker), price_gap, current_spread_reversed, funding_at_cap, short_history, "
            "interval_mismatch, volatile (7d range >= 33 %). `hyperliquid` is optional (hourly funding, taker fee 0.045 %, cap 4 %/h; "
            "kPEPE-style names are matched to 1000PEPE-style CEX tickers by multiplier, other naming differences stay unmatched; "
            "the HL<->Binance 7d carry backtest was unprofitable after costs, so treat it as monitoring, not a signal). `tradable_now` = above entry with no price or reversal flag. "
            "Without `bases`, the `candidates` crypto perps listed on at least two venues with the widest current spread "
            "are scanned. Rates and APRs are decimals on notional. See `warnings` before acting."
        ),
    )
    async def funding_spread_scan(
        venues: Annotated[
            list[Literal["binanceusdm", "bybit", "okx", "hyperliquid"]] | None,
            Field(
                description="Two to four perp venues (ccxt + hyperliquid); default binanceusdm and bybit.",
                max_length=4,
            ),
        ] = None,
        bases: Annotated[
            list[str] | None,
            Field(description="Bases to scan, e.g. ['BTC','WIF']; default: widest current spreads.", max_length=50),
        ] = None,
        candidates: Annotated[int, Field(ge=1, le=50, description="How many bases to scan when `bases` is empty.")] = 20,
        lookback_days: Annotated[float, Field(ge=1, le=30, description="Trailing window for realized funding, days.")] = 7.0,
        entry_apr: Annotated[float, Field(ge=0, le=50, description="Trailing spread at or above which the pair is 'above_entry'.")] = 0.10,
        exit_apr: Annotated[float, Field(ge=-1, le=50, description="Trailing spread at or below which the pair is 'below_exit'.")] = 0.0,
        slippage: Annotated[float, Field(ge=0, le=0.01, description="Slippage per leg per side, decimal of notional.")] = 0.0005,
        max_entry_gap: Annotated[
            float, Field(ge=0, le=0.2, description="Largest mark-price gap between venues to call it tradable.")
        ] = 0.02,
        crypto_only: Annotated[bool, Field(description="Skip TradFi perps flagged by the venue when picking candidates.")] = True,
        max_concurrency: Annotated[int, Field(ge=1, le=16, description="Parallel requests.")] = 4,
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(SPREAD_WARNING)
        vs = list(dict.fromkeys(v.lower() for v in (venues or ["binanceusdm", "bybit"])))
        if len(vs) < 2:
            b.miss("venues", "tradedesk", "config", "need at least two venues")
            return b.build(None, ok=False)
        now_ms = int(rt.now().timestamp() * 1000)
        markets: dict[str, dict[str, Any]] = {}
        current: dict[str, dict[str, dict[str, Any]]] = {}
        tradfi: set[str] = set()
        hl_names: dict[str, str] = {}
        for v in vs:
            if v == "hyperliquid":
                current[v], hl_names = await hl_current_funding(rt, b)
                continue
            markets[v] = await b.guard(f"perp_markets:{v}", f"ccxt:{v}", rt.exchanges.markets(v)) or {}
            current[v] = await _current_funding(b, v, markets[v])
            for m, x in markets[v].items():
                info = (x or {}).get("info") or {}
                if str(info.get("contractType") or "").startswith("TRADIFI") or info.get("underlyingType") not in (None, "COIN"):
                    tradfi.add(m)
        if bases:
            symbols = [f"{x.upper()}/USDT:USDT" for x in bases]
        else:
            listed: dict[str, list[float]] = {}
            for v in vs:
                for sym, cur in current[v].items():
                    if not (crypto_only and sym in tradfi):
                        listed.setdefault(sym, []).append(cur["apr"])
            ranked = sorted(((max(a) - min(a), sym) for sym, a in listed.items() if len(a) >= 2), reverse=True)
            symbols = [sym for _, sym in ranked[:candidates]]
            if not symbols:
                b.miss("candidates", "tradedesk", "data_unavailable", "no perp is listed with current funding on two venues")
                return b.build(None, ok=False)
        sem = asyncio.Semaphore(max_concurrency)
        since_ms = now_ms - int(max(30.0, lookback_days) * 86_400_000)

        async def leg(v: str, sym: str) -> tuple[str, dict[str, Any]] | None:
            cur = current[v].get(sym)
            if not bases and cur is None:
                return None
            if v == "hyperliquid":
                coin = hl_names.get(sym)
                if coin is None:
                    return None
                async with sem:
                    got = await b.guard(
                        f"funding_history:{v}:{sym}", "hyperliquid", rt.hyperliquid.funding_history_range(coin, since_ms, now_ms)
                    )
                entries = [
                    {"ts_ms": int(x["time"]), "rate": float(x["fundingRate"])}
                    for x in (got["rows"] if got else [])
                    if x.get("fundingRate") is not None and x.get("time")
                ]
            else:
                async with sem:
                    got = await b.guard(
                        f"funding_history:{v}:{sym}", f"ccxt:{v}", rt.exchanges.funding_rate_history_range(v, sym, since_ms, now_ms)
                    )
                entries = [
                    {"ts_ms": int(x["timestamp"]), "rate": float(x["fundingRate"])}
                    for x in (got["rows"] if got else [])
                    if x.get("fundingRate") is not None and x.get("timestamp")
                ]
            if not entries and cur is None:
                return None
            cur = cur or {}
            return v, {
                "entries": entries,
                "rate": cur.get("rate"),
                "interval_hours": cur.get("interval_hours"),
                "mark": cur.get("mark"),
                "cap": cur.get("cap"),
            }

        async def row(sym: str) -> dict[str, Any] | None:
            legs = dict(x for x in await asyncio.gather(*[leg(v, sym) for v in vs]) if x)
            if len(legs) < 2:
                return None
            snap = spread_snapshot(
                legs,
                now_ms=now_ms,
                lookback_days=lookback_days,
                entry_apr=entry_apr,
                exit_apr=exit_apr,
                fees=TAKER_FEES,
                slippage=slippage,
                max_entry_gap=max_entry_gap,
            )
            pair = snap["pair"]
            if pair is None:
                return None
            async with sem:
                if pair["short_venue"] == "hyperliquid":
                    raw = await b.guard(
                        f"ohlcv:hyperliquid:{sym}",
                        "hyperliquid",
                        rt.hyperliquid.candles(hl_names.get(sym, ""), "4h", now_ms - 42 * 4 * 3_600_000, now_ms),
                    )
                    # HL candles are dicts (t, o, h, l, c): reshape to ccxt [ts, o, h, l, c]
                    bars = [[x["t"], x["o"], x["h"], x["l"], x["c"]] for x in raw or [] if isinstance(x, dict) and "h" in x and "l" in x]
                else:
                    bars = await b.guard(
                        f"ohlcv:{pair['short_venue']}:{sym}",
                        f"ccxt:{pair['short_venue']}",
                        rt.exchanges.ohlcv(pair["short_venue"], sym, "4h", 42),
                    )
            ok_bars = [x for x in (bars or []) if isinstance(x, (list, tuple)) and len(x) >= 5]
            range_7d = max(float(x[2]) for x in ok_bars) / min(float(x[3]) for x in ok_bars) - 1.0 if ok_bars else None
            flags = list(pair["flags"])
            if range_7d is not None and range_7d >= 1 / 3:
                flags.append("volatile")
            return {
                "base": sym.split("/")[0],
                "symbol": sym,
                "short_venue": pair["short_venue"],
                "long_venue": pair["long_venue"],
                "trailing_spread_apr": r(pair["trailing_spread_apr"], 4),
                "current_spread_apr": r(pair["current_spread_apr"], 4),
                "status": pair["status"],
                "tradable_now": pair["tradable_now"],
                "breakeven_days": r(pair["breakeven_days"], 1),
                "roundtrip_cost": r(pair["roundtrip_cost"], 5),
                "price_gap": r(pair["price_gap"], 5),
                "range_7d": r(range_7d, 4),
                "flags": flags,
                "venues": {
                    v: {
                        "trailing_apr": r(x["trailing_apr"], 4),
                        "trailing_30d_apr": r(x["trailing_30d_apr"], 4),
                        "current_apr": r(x["current_apr"], 4),
                        "current_rate": r(x["current_rate"], 8),
                        "interval_hours": x["interval_hours"],
                        "cap": x["cap"],
                        "mark": x["mark"],
                        "coverage_days": r(x["coverage_days"], 1),
                    }
                    for v, x in snap["venues"].items()
                },
            }

        rows = [x for x in await asyncio.gather(*[row(s_) for s_ in symbols]) if x]
        if not rows:
            return b.build(None, ok=False)
        rows.sort(key=lambda x: x["trailing_spread_apr"] if x["trailing_spread_apr"] is not None else float("-inf"), reverse=True)
        spreads = sorted(x["trailing_spread_apr"] for x in rows if x["trailing_spread_apr"] is not None)
        return b.build(
            {
                "venues": vs,
                "lookback_days": lookback_days,
                "thresholds": {"entry_apr": entry_apr, "exit_apr": exit_apr, "slippage": slippage, "max_entry_gap": max_entry_gap},
                "summary": {
                    "scanned": len(rows),
                    "tradable_now": [x["base"] for x in rows if x["tradable_now"]],
                    "above_entry": [x["base"] for x in rows if x["status"] == "above_entry"],
                    "median_trailing_spread_apr": r(spreads[len(spreads) // 2], 4) if spreads else None,
                },
                "rows": rows,
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Retail long/short account ratio history on Binance USDT-M and Bybit (public /futures/data and /v5/market/account-ratio). "
            "Both report **account fractions** (not notional-weighted), summing to ~1.0. Per venue returns the raw series plus latest/mean/min/max/24h-change/z-score. "
            "Cross-venue: who is most long right now, and absolute divergence between venues (large divergence = disagreement between retail cohorts). "
            "`period` is one of 5m/15m/30m/1h/4h/1d; `limit` up to 500. Set `include_series=true` to see all rows."
        ),
    )
    async def perps_long_short_ratio(
        base: BASE = "BTC",
        period: Period = "1h",
        limit: Annotated[int, Field(ge=1, le=500)] = 48,
        include_series: bool = False,
    ) -> dict[str, Any]:
        b = Builder()
        base = base.upper()
        symbol = f"{base}USDT"

        async def binance_row() -> dict[str, Any] | None:
            raw = await b.guard(
                "ls_ratio:binance", "binance_futures", rt.binance_futures.long_short_account_ratio(symbol, _PERIOD_BINANCE[period], limit)
            )
            if not raw:
                return None
            series = [
                {
                    "ts_ms": int(x["timestamp"]),
                    "long": float(x["longAccount"]),
                    "short": float(x["shortAccount"]),
                    "ratio": float(x["longShortRatio"]),
                }
                for x in raw
                if x.get("longAccount") is not None and x.get("shortAccount") is not None
            ]
            series.sort(key=lambda s: s["ts_ms"])
            if not series:
                return None
            step = _PERIOD_SECONDS[period]
            s = _series_stats([x["ratio"] for x in series], step)
            long_fracs = _series_stats([x["long"] for x in series], step)
            return {
                "venue": "binanceusdm",
                "latest": series[-1],
                "ratio_stats": s,
                "long_stats": long_fracs,
                **({"series": series} if include_series else {}),
            }

        async def bybit_row() -> dict[str, Any] | None:
            raw = await b.guard("ls_ratio:bybit", "bybit", rt.bybit.long_short_account_ratio(symbol, _PERIOD_BYBIT[period], limit))
            if not raw:
                return None
            series = [
                {
                    "ts_ms": int(x["timestamp"]),
                    "long": float(x["buyRatio"]),
                    "short": float(x["sellRatio"]),
                    "ratio": float(x["buyRatio"]) / float(x["sellRatio"]) if float(x["sellRatio"]) else None,
                }
                for x in raw
                if x.get("buyRatio") is not None and x.get("sellRatio") is not None
            ]
            series.sort(key=lambda s: s["ts_ms"])
            series = [x for x in series if x["ratio"] is not None]
            if not series:
                return None
            step = _PERIOD_SECONDS[period]
            s = _series_stats([x["ratio"] for x in series], step)
            long_fracs = _series_stats([x["long"] for x in series], step)
            return {
                "venue": "bybit",
                "latest": series[-1],
                "ratio_stats": s,
                "long_stats": long_fracs,
                **({"series": series} if include_series else {}),
            }

        rows = [x for x in await asyncio.gather(binance_row(), bybit_row()) if x]
        if not rows:
            return b.build(None, ok=False)

        most_long = max(rows, key=lambda x: x["latest"]["long"])
        most_short = min(rows, key=lambda x: x["latest"]["long"])
        divergence = None
        if len(rows) == 2:
            divergence = abs(rows[0]["latest"]["long"] - rows[1]["latest"]["long"])
        return b.build(
            {
                "base": base,
                "period": period,
                "venues": rows,
                "cross_venue": {
                    "most_long": {"venue": most_long["venue"], "long_fraction": r(most_long["latest"]["long"], 4)},
                    "most_short": {"venue": most_short["venue"], "long_fraction": r(most_short["latest"]["long"], 4)},
                    "long_fraction_divergence": r(divergence, 4),
                },
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Open-interest history on Binance USDT-M and Bybit (public /futures/data/openInterestHist and /v5/market/open-interest). "
            "Binance reports both sum open interest in the base asset and in USD; Bybit reports the base-asset total only (USD estimate omitted). "
            "Per venue: current OI (base and USD when available), mean, 24h change, z-score of latest vs the window. `period` is 5m/15m/30m/1h/4h/1d; `limit` ≤500."
        ),
    )
    async def perps_oi_history(
        base: BASE = "BTC",
        period: Period = "1h",
        limit: Annotated[int, Field(ge=1, le=500)] = 48,
        include_series: bool = False,
    ) -> dict[str, Any]:
        b = Builder()
        base = base.upper()
        symbol = f"{base}USDT"
        step = _PERIOD_SECONDS[period]

        async def binance_row() -> dict[str, Any] | None:
            raw = await b.guard(
                "oi_hist:binance", "binance_futures", rt.binance_futures.open_interest_hist(symbol, _PERIOD_BINANCE[period], limit)
            )
            if not raw:
                return None
            series = [
                {"ts_ms": int(x["timestamp"]), "oi_base": float(x["sumOpenInterest"]), "oi_usd": float(x["sumOpenInterestValue"])}
                for x in raw
                if x.get("sumOpenInterest") is not None
            ]
            series.sort(key=lambda s: s["ts_ms"])
            if not series:
                return None
            return {
                "venue": "binanceusdm",
                "latest": series[-1],
                "base_stats": _series_stats([x["oi_base"] for x in series], step),
                "usd_stats": _series_stats([x["oi_usd"] for x in series], step),
                **({"series": series} if include_series else {}),
            }

        async def bybit_row() -> dict[str, Any] | None:
            raw = await b.guard("oi_hist:bybit", "bybit", rt.bybit.open_interest(symbol, _PERIOD_BYBIT[period], limit))
            if not raw:
                return None
            series = [
                {"ts_ms": int(x["timestamp"]), "oi_base": float(x["openInterest"]), "oi_usd": None}
                for x in raw
                if x.get("openInterest") is not None
            ]
            series.sort(key=lambda s: s["ts_ms"])
            if not series:
                return None
            return {
                "venue": "bybit",
                "latest": series[-1],
                "base_stats": _series_stats([x["oi_base"] for x in series], step),
                "usd_stats": None,
                **({"series": series} if include_series else {}),
            }

        rows = [x for x in await asyncio.gather(binance_row(), bybit_row()) if x]
        if not rows:
            return b.build(None, ok=False)

        def pct_change(x: dict[str, Any]) -> float | None:
            s = x["base_stats"]
            if s.get("change_24h") is None or s.get("latest") in (None, 0):
                return None
            prev = s["latest"] - s["change_24h"]
            return (s["change_24h"] / prev) if prev else None

        with_pct = [(x, pct_change(x)) for x in rows]
        growth = [(x, p) for x, p in with_pct if p is not None]
        cross: dict[str, Any] = {"biggest_gain_24h": None, "biggest_drop_24h": None}
        if growth:
            best = max(growth, key=lambda t: t[1])
            worst = min(growth, key=lambda t: t[1])
            cross["biggest_gain_24h"] = {"venue": best[0]["venue"], "pct_change_24h": r(best[1] * 100, 3)}
            cross["biggest_drop_24h"] = {"venue": worst[0]["venue"], "pct_change_24h": r(worst[1] * 100, 3)}
        return b.build({"base": base, "period": period, "venues": rows, "cross_venue": cross})

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
            oi_base = oi.get("openInterestAmount")
            oi_usd = oi.get("openInterestValue")
            if oi_usd is None and oi_base:
                tkr = await b.guard(f"oi_mark:{ex}", f"ccxt:{ex}", rt.exchanges.ticker(ex, symbol))
                mark = (tkr or {}).get("last")
                if mark:
                    oi_usd = oi_base * mark
                    b.warn(f"{ex}: oi_usd estimated as amount * last price (openInterestValue not reported)")
            return {"venue": ex, "oi_base": oi_base, "oi_usd": oi_usd}

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
        coin = await _hl_name(b, coin)
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
                "coin": coin,
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
        coin = await _hl_name(b, coin)
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
            "coin": coin,
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
        coin = await _hl_name(b, coin)
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
        return b.build({"coin": coin, "fetched": len(raw), "parsed": len(normalised), "summary": summary})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Hyperliquid funding-rate history for one coin over a lookback window (default 30 days, interval 1h). Returns the raw series plus "
            "mean/stdev/min/max/7d-mean/30d-mean of the per-hour rate, annualised carry (sum of payments over the covered span), the latest rate's z-score and percentile "
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
        coin = await _hl_name(b, coin)
        got = await b.guard("funding_history", "hyperliquid", rt.hyperliquid.funding_history_range(coin, start_ms, now_ms))
        if not got or not got["rows"]:
            if got is not None:
                b.miss("funding_history", "hyperliquid", "data_unavailable", "empty result")
            return b.build(None, ok=False)
        if not got["complete"]:
            b.warn("funding history incomplete (page limit or a later page failed); window truncated")
        entries: list[dict[str, Any]] = []
        for e in got["rows"]:
            try:
                entries.append({"ts_ms": int(e["time"]), "rate": float(e["fundingRate"])})
            except (KeyError, TypeError, ValueError):
                continue
        if not entries:
            b.miss("funding_history", "hyperliquid", "data_unavailable", "no parsable entries")
            return b.build(None, ok=False)
        stats = summarise_funding_history(entries, interval_hours=1.0, now_ms=now_ms)
        return b.build({"coin": coin, "days_requested": days_back, "stats": stats, "entries": entries})

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

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Cross-venue basis snapshot. For each perp venue (Binance USDT-M, Bybit, OKX via ccxt, "
            "Hyperliquid, Deribit for BTC/ETH): mark, spot index, premium (bps and decimal, 10000 bps "
            "= 100 %) and annualised funding. Also returns Deribit's dated-futures curve so quarterly "
            "basis can be compared against perp funding (cash-and-carry setup when quarterly basis is "
            "well above funding). Summary names the venue with the highest / lowest perp premium and "
            "the cross-venue spread; `nearest_dated_annualized` is the simple annualised basis of the "
            "shortest dated contract."
        ),
    )
    async def perps_basis_compare(base: BASE = "BTC", exchanges: CCXT_VENUES = None) -> dict[str, Any]:
        b = Builder()
        base = base.upper()
        venues = [e.lower() for e in (exchanges or PERP_EXCHANGES)]
        symbol = f"{base}/USDT:USDT"

        def _premium(mark: Any, index: Any) -> tuple[float | None, float | None]:
            try:
                m, i = float(mark), float(index)
            except (TypeError, ValueError):
                return None, None
            if i <= 0:
                return None, None
            pct = m / i - 1.0
            return pct, pct * 10_000.0

        async def ccxt_row(ex: str) -> dict[str, Any] | None:
            fr = await b.guard(f"basis:{ex}", f"ccxt:{ex}", rt.exchanges.funding_rate(ex, symbol))
            if fr is None:
                return None
            pct, bps = _premium(fr.get("markPrice"), fr.get("indexPrice"))
            if pct is None:
                return None
            rate = fr.get("fundingRate")
            hours = parse_interval_hours(fr.get("interval"))
            return {
                "venue": ex,
                "mark": fr.get("markPrice"),
                "index": fr.get("indexPrice"),
                "premium_pct": r(pct, 6),
                "premium_bps": r(bps, 2),
                "funding_annualized": r(annualize_funding(float(rate), hours), 5) if rate is not None else None,
            }

        async def hl_row() -> dict[str, Any] | None:
            ctx = await b.guard("basis:hyperliquid", "hyperliquid", rt.hyperliquid.asset_contexts())
            row = next((c for c in ctx or [] if c["coin"] == base), None)
            if row is None:
                return None
            pct, bps = _premium(row.get("mark_px"), row.get("oracle_px"))
            if pct is None:
                return None
            return {
                "venue": "hyperliquid",
                "mark": row.get("mark_px"),
                "index": row.get("oracle_px"),
                "premium_pct": r(pct, 6),
                "premium_bps": r(bps, 2),
                "funding_annualized": r(row.get("funding_annualized"), 5) if row.get("funding_annualized") is not None else None,
            }

        async def deribit_perp_and_dated() -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
            if base not in ("BTC", "ETH"):
                return None, []
            fut = await b.guard("basis:deribit", "deribit", rt.deribit.book_summary(base, "future"))
            if fut is None:
                return None, []
            perp_row: dict[str, Any] | None = None
            dated: list[dict[str, Any]] = []
            now = rt.now()
            for f in fut:
                parsed = parse_future_name(f.get("instrument_name", ""))
                idx, mark = f.get("estimated_delivery_price"), f.get("mark_price")
                if not parsed or not idx or not mark:
                    continue
                if parsed[1] is None:
                    pct, bps = _premium(mark, idx)
                    if pct is None:
                        continue
                    rate = f.get("funding_8h")
                    perp_row = {
                        "venue": "deribit",
                        "mark": mark,
                        "index": idx,
                        "premium_pct": r(pct, 6),
                        "premium_bps": r(bps, 2),
                        "funding_annualized": r(annualize_funding(rate, 8.0), 5) if rate is not None else None,
                    }
                else:
                    bs = basis(mark, idx, parsed[1], now)
                    dated.append(
                        {
                            "instrument": f["instrument_name"],
                            "expiry": expiry_token(parsed[1]),
                            "days": bs["days_to_expiry"],
                            "mark": mark,
                            "basis_pct": r(bs["basis_pct"] * 100, 4),
                            "annualized_simple": r(bs["annualized_simple"], 4),
                            "annualized_log": r(bs["annualized_log"], 4),
                        }
                    )
            dated.sort(key=lambda x: x["days"])
            return perp_row, dated

        results = await asyncio.gather(*[ccxt_row(e) for e in venues], hl_row())
        perps = [x for x in results if x]
        dbt_perp, dated = await deribit_perp_and_dated()
        if dbt_perp is not None:
            perps.append(dbt_perp)
        if not perps:
            return b.build(None, ok=False)

        perps.sort(key=lambda x: x["premium_bps"], reverse=True)
        hi, lo = perps[0], perps[-1]
        nearest = dated[0] if dated else None
        return b.build(
            {
                "base": base,
                "perps": perps,
                "deribit_dated_futures": dated,
                "summary": {
                    "highest_premium": {"venue": hi["venue"], "premium_bps": hi["premium_bps"]},
                    "lowest_premium": {"venue": lo["venue"], "premium_bps": lo["premium_bps"]},
                    "cross_venue_spread_bps": r(hi["premium_bps"] - lo["premium_bps"], 2),
                    "nearest_dated_annualized": nearest["annualized_simple"] if nearest else None,
                    "nearest_dated_expiry": nearest["expiry"] if nearest else None,
                },
            }
        )
