"""market_flow: descriptive market-flow reader (IDEA-20261028-market-flow-reader, step 1).

All I/O goes through the providers (Binance spot / USD-M klines and /futures/data, Bybit v5, ccxt funding history,
Coinalyze liquidations, Coinbase candles); the numbers come from `analytics.market_flow` (pure).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics import market_flow as mf
from ..analytics.liquidations_agg import exchange_symbol
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY

H = mf.HOUR_MS
CCXT_ID = {"binance": "binanceusdm", "bybit": "bybit"}
FUNDING_LOOKBACK_MS = 2 * 86_400_000  # settlements before the window, so its first bars have a last settled rate

REJECTED_NOTE = (
    "Descriptive context, not a signal. Individually these flows did not predict direction in our backtests: rejected "
    "spot-vs-perp-flow-divergence, taker-imbalance-8h-continuation, oi-quadrant-squeeze-vs-newlong, "
    "liquidation-flush-overshoot, gex-taker-flow-range, us-demand-flows (Coinbase premium); inconclusive "
    "funding-crowding-longshort, oi-confirmed-liquidation-flush, perp-spot-premium-spike-convergence. The combined "
    "test of all metrics together (card IDEA-20261028-market-flow-reader, step 3) is not done yet."
)
CVD_NOTE = (
    "CVD = aggressor buys minus sells (2 x taker-buy quote volume - quote volume) of Binance spot and USD-M klines "
    "only: one venue's flow, not market-wide; USDT read as USD. Bybit klines have no taker split, so Bybit adds no CVD."
)
OI_NOTE = (
    "OI = snapshots in base coins as of each bar close, summed over the selected venues (a bar where a venue has no "
    "snapshot gets no total); USD = coins x the bar's Binance USD-M close. Changes are in coins, so price moves do not "
    "inflate them."
)
FUNDING_NOTE = (
    "Funding = last settled rate per venue as of the bar close, simple-annualised (rate x 8760 / interval hours; "
    "0.10 = 10 %/yr), plain mean over venues; before fees and basis."
)
LIQ_NOTE = (
    "Liquidations: Coinalyze hourly rows (Binance '.A', Bybit '.6'), USD = coins x the interval close (Coinalyze's "
    "conversion), exchange-reported and understated; the latest hour may not be published yet."
)
LS_NOTE = "Long share = fraction of accounts long (not notional), mean over venues."
PREMIUM_NOTE = (
    "Coinbase premium = Coinbase BASE-USD close / Binance BASE-USDT close - 1 (decimal); raw includes the USDT/USD "
    "basis and shifts in USDT stress; premium_adj = raw minus the median of the previous 30 days of hourly premiums "
    "(needs at least 7 days)."
)
QUADRANT_NOTE = (
    "Quadrant = price x OI regime with the aggressor side of perps and spot; flat below 0.1 % price, 0.2 % OI, CVD "
    "below 1 % of traded volume. Summaries use closed hours up to the last one (for 4h series the trailing hours of "
    "an unfinished 4h bar count in the summaries, not in the series)."
)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


def _rows_of(b: Builder, field: str, got: Any) -> list[Any] | None:
    """Rows of a ``{"rows", "complete"}`` range result, with a warning when it is incomplete."""
    if not isinstance(got, dict):
        return None
    if not got.get("complete", True):
        b.warn(f"{field}: history incomplete (paging stopped early); the earliest part of the window may be missing.")
    return got.get("rows") or []


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Market flow reader for one coin: who is pushing the price (descriptive context, NOT a signal). Closed UTC "
            "hours over the last `days` (1-14), as 1h or 4h bars. Series and summaries: spot CVD and perp CVD in USD "
            "(Binance spot and USD-M klines: aggressor buys minus sells = 2 x taker-buy quote volume - quote volume; "
            "USDT read as USD; one venue's flow, not market-wide; Bybit klines have no taker split), open interest in "
            "base coins summed over `venues` and in USD (coins x Binance USD-M close) with its change, funding (last "
            "settled rate per venue, simple-annualised decimal, 0.10 = 10 %/yr, mean over venues), long and short "
            "liquidations in USD (Coinalyze, needs COINALYZE_API_KEY; coins x interval close, exchange-reported, "
            "understated), long share of accounts (fraction, not notional), and the Coinbase premium (Coinbase BASE-USD "
            "/ Binance BASE-USDT - 1, decimal; raw includes the USDT/USD basis; premium_adj = minus the median of the "
            "previous 30 days of hourly premiums). Summaries for the last 4 h, 24 h and the window; a quadrant price x "
            "OI x perp-CVD for the last 4 h and 24 h (flat below 0.1 % price, 0.2 % OI, CVD 1 % of volume) and a short "
            "descriptive Ukrainian sentence. Individually these flows did NOT predict direction in our backtests: "
            "rejected spot-vs-perp-flow-divergence, taker-imbalance-8h-continuation, oi-quadrant-squeeze-vs-newlong, "
            "liquidation-flush-overshoot, gex-taker-flow-range, us-demand-flows (Coinbase premium); inconclusive "
            "funding-crowding-longshort, oi-confirmed-liquidation-flush, perp-spot-premium-spike-convergence; the "
            "combined test is not done. A source that fails goes to `missing`; the rest is returned."
        ),
    )
    async def market_flow(
        base: Annotated[
            str, Field(description="Base asset ticker, e.g. 'BTC', 'ETH' (USDT pairs on Binance / Bybit, BASE-USD on Coinbase).")
        ] = "BTC",
        timeframe: Annotated[
            Literal["1h", "4h"], Field(description="Bar size of the series (UTC-aligned; 4h bars only when complete).")
        ] = "1h",
        days: Annotated[int, Field(ge=1, le=14, description="Window in days of closed bars (1-14).")] = 7,
        venues: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Derivatives venues summed for OI, funding, liquidations and long share: 'binance', 'bybit' "
                    "(default both). CVD and price always come from Binance, the premium from Coinbase vs Binance spot."
                )
            ),
        ] = None,
        include_series: Annotated[bool, Field(description="Return the per-bar series (false = summaries only).")] = True,
    ) -> dict[str, Any]:
        b = Builder()
        sym_base = (base or "").strip().upper()
        if not mf.BASE_RE.match(sym_base):
            b.miss("base", "tradedesk", "invalid_argument", "base must be a plain ticker like 'BTC'")
            return b.build(None, ok=False)
        names, bad = mf.parse_venues(venues)
        if bad or not names:
            head = f"unknown venue(s) {bad[:3]}" if bad else "no venue given"
            b.miss("venues", "tradedesk", "invalid_argument", f"{head}; allowed: {list(mf.VENUES)}")
            return b.build(None, ok=False)

        tf_h = mf.TIMEFRAME_HOURS[timeframe]
        end = int(rt.now().timestamp() * 1000) // H * H  # open of the hour in progress: bars before it are closed
        series_end = end // (tf_h * H) * (tf_h * H)
        start = series_end - days * 24 * H
        grid_start = start - H  # one leading bar: the reference close of the first window
        hist_start = start - mf.PREMIUM_MEDIAN_HOURS * H
        n_hours = (end - grid_start) // H
        limit = min(500, n_hours + 3)
        symbol = f"{sym_base}USDT"

        async def none() -> None:
            return None

        def per_venue(make: Any) -> list[Any]:
            return [make(v) if v in names else none() for v in mf.VENUES]

        spot_t = b.guard("spot_klines", "binance_spot", rt.binance_spot.klines_range(symbol, "1h", hist_start, end))
        perp_t = b.guard("perp_klines", "binance_futures", rt.binance_futures.klines_range(symbol, "1h", grid_start, end))
        cb_t = b.guard("premium:coinbase", "coinbase", rt.coinbase.candles_range(f"{sym_base}-USD", 3600, hist_start, end))
        oi_t = per_venue(
            lambda v: (
                b.guard("open_interest:binance", "binance_futures", rt.binance_futures.open_interest_hist(symbol, "1h", limit))
                if v == "binance"
                else b.guard("open_interest:bybit", "bybit", rt.bybit.open_interest_range(symbol, "1h", grid_start - H, end))
            )
        )
        ls_t = per_venue(
            lambda v: (
                b.guard("long_short:binance", "binance_futures", rt.binance_futures.long_short_account_ratio(symbol, "1h", limit))
                if v == "binance"
                else b.guard("long_short:bybit", "bybit", rt.bybit.long_short_account_ratio(symbol, "1h", limit))
            )
        )
        fund_t = per_venue(
            lambda v: b.guard(
                f"funding:{v}",
                f"ccxt:{CCXT_ID[v]}",
                rt.exchanges.funding_rate_history_range(
                    CCXT_ID[v], f"{sym_base}/USDT:USDT", grid_start - FUNDING_LOOKBACK_MS, end, trust_short_page=v == "binance"
                ),
            )
        )
        liq_t = per_venue(
            lambda v: b.guard(
                f"liquidations:{v}",
                "coinalyze",
                rt.coinalyze.liquidation_history(exchange_symbol(sym_base, "usdt", v) or "", "1hour", grid_start // 1000, end // 1000 - 1),
            )
        )
        got = await asyncio.gather(spot_t, perp_t, cb_t, *oi_t, *ls_t, *fund_t, *liq_t)
        spot_raw, perp_raw, cb_raw = got[0], got[1], got[2]
        k = len(mf.VENUES)
        oi_raw = dict(zip(mf.VENUES, got[3 : 3 + k], strict=True))
        ls_raw = dict(zip(mf.VENUES, got[3 + k : 3 + 2 * k], strict=True))
        fund_raw = dict(zip(mf.VENUES, got[3 + 2 * k : 3 + 3 * k], strict=True))
        liq_raw = dict(zip(mf.VENUES, got[3 + 3 * k : 3 + 4 * k], strict=True))

        spot = mf.parse_binance_klines(_rows_of(b, "spot_klines", spot_raw) or [])
        perp = mf.parse_binance_klines(_rows_of(b, "perp_klines", perp_raw) or [])
        coinbase = mf.parse_coinbase_candles(_rows_of(b, "premium:coinbase", cb_raw) or [])

        oi: dict[str, list[tuple[int, float]]] = {}
        if oi_raw["binance"] is not None:
            oi["binance"] = mf.points(oi_raw["binance"], "timestamp", "sumOpenInterest", lo=0)
        if oi_raw["bybit"] is not None:
            oi["bybit"] = mf.points(_rows_of(b, "open_interest:bybit", oi_raw["bybit"]) or [], "timestamp", "openInterest", lo=0)
        ls: dict[str, list[tuple[int, float]]] = {}
        if ls_raw["binance"] is not None:
            ls["binance"] = mf.points(ls_raw["binance"], "timestamp", "longAccount", lo=0, hi=1)
        if ls_raw["bybit"] is not None:
            ls["bybit"] = mf.points(ls_raw["bybit"], "timestamp", "buyRatio", lo=0, hi=1)
        funding: dict[str, tuple[list[tuple[int, float]], float]] = {}
        for v, raw in fund_raw.items():
            if raw is None:
                continue
            pts = mf.points(_rows_of(b, f"funding:{v}", raw) or [], "timestamp", "fundingRate", lo=-0.1, hi=0.1)
            hrs = mf.funding_interval_hours(pts)
            if hrs is None:
                b.miss(f"funding:{v}", f"ccxt:{CCXT_ID[v]}", "data_unavailable", "too few settlements to tell the funding interval")
                continue
            funding[v] = (pts, hrs)
        liqs: dict[str, dict[int, tuple[float, float]]] = {}
        for v, raw in liq_raw.items():
            if raw is None:
                continue
            bars = mf.liquidation_bars(raw)
            if bars:
                liqs[v] = bars
            else:
                b.miss(f"liquidations:{v}", "coinalyze", "data_unavailable", "no hourly liquidation rows in the window")
        for name, d in (("open_interest", oi), ("long_short", ls)):
            for v in list(d):
                if not d[v]:
                    b.miss(f"{name}:{v}", "binance_futures" if v == "binance" else "bybit", "data_unavailable", "no rows in the response")
                    del d[v]

        ext = list(range(hist_start, end, H))
        raw_p, adj_p = mf.premium_series(coinbase, spot, ext)
        premium = dict(zip(ext, raw_p, strict=True))
        premium_adj = dict(zip(ext, adj_p, strict=True))

        grid = list(range(grid_start, end, H))
        rows = mf.hourly_rows(
            grid, spot=spot, perp=perp, oi=oi, funding=funding, liquidations=liqs, long_share=ls, premium=premium, premium_adj=premium_adj
        )
        has_data = any(
            r[f] is not None for r in rows for f in ("price", "oi_coins", "funding_annualized", "long_share", "liq_long", "premium")
        )
        if not has_data:
            return b.build(None, ok=False)

        series = mf.resample([r for r in rows if start <= r["ts"] < series_end], tf_h)
        mf.add_cumulative(series)
        summary = {"4h": mf.window_summary(rows, 4), "24h": mf.window_summary(rows, 24), "window": mf.window_summary(rows, len(rows) - 1)}
        quad = {"4h": mf.quadrant(summary["4h"]), "24h": mf.quadrant(summary["24h"])}
        sentence_24 = mf.describe_ua(summary["24h"], quad["24h"])
        sentence_4 = mf.describe_ua(summary["4h"], quad["4h"])

        b.warn(REJECTED_NOTE)
        b.warn(CVD_NOTE)
        if oi:
            b.warn(OI_NOTE + ("" if set(oi) == set(names) else f" OI covers {', '.join(sorted(oi))} only (see missing)."))
        if funding:
            b.warn(FUNDING_NOTE)
        if liqs:
            b.warn(LIQ_NOTE + ("" if set(liqs) == set(names) else f" Liquidations cover {', '.join(sorted(liqs))} only."))
        if ls:
            b.warn(LS_NOTE)
        if coinbase:
            b.warn(PREMIUM_NOTE)
            if all(r["premium_adj"] is None for r in rows):
                b.warn("premium_adj is empty: fewer than 7 days of hourly Coinbase/Binance closes before the window.")
        b.warn(QUADRANT_NOTE)

        out_series = [
            {
                "t": _iso(r["ts"]),
                "ts": r["ts"],
                **{
                    f: r[f]
                    for f in (
                        "price",
                        "spot_delta",
                        "spot_cvd",
                        "perp_delta",
                        "perp_cvd",
                        "oi_coins",
                        "oi_usd",
                        "funding_annualized",
                        "liq_long",
                        "liq_short",
                        "long_share",
                        "premium",
                        "premium_adj",
                    )
                },
            }
            for r in series
        ]
        data: dict[str, Any] = {
            "base": sym_base,
            "timeframe": timeframe,
            "days": days,
            "venues": names,
            "window": {"start": _iso(start), "series_end": _iso(series_end), "last_closed_hour_end": _iso(end)},
            "units": {
                "cvd": "USD (USDT)",
                "oi": "base coins; oi_usd in USD",
                "funding": "decimal per year, simple",
                "liquidations": "USD",
                "long_share": "fraction of accounts",
                "premium": "decimal",
                "changes": "decimal (0.01 = 1 %)",
            },
            "summary": summary,
            "quadrant": quad,
            "sentence_ua": sentence_24 or sentence_4,
            "sentence_4h_ua": sentence_4,
            "venues_latest": mf.venue_latest(end, oi=oi, funding=funding, long_share=ls, liquidations=liqs, since_ms=start),
        }
        if include_series:
            data["series"] = out_series
        return b.build(mf.round_tree(data))
