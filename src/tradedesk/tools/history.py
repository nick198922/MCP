"""Historical queries over the local SQLite store (Phase 2).

Every tool in this module needs the store to be initialised AND populated by the
collector; otherwise it returns `ok=false` with `missing: store`.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.ranges import classify_ranges
from ..analytics.regime import classify_regime
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r


def _summary(values: list[float], latest: float) -> dict[str, Any]:
    """Shared stats block for history-rank style tools."""
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    stdev = math.sqrt(var)
    z = (latest - mean) / stdev if stdev > 0 else None
    below = sum(1 for v in values if v < latest)
    ties = sum(1 for v in values if v == latest)
    pct = (below + 0.5 * ties) / n
    lo, hi = min(values), max(values)
    rank = (latest - lo) / (hi - lo) if hi > lo else None
    return {
        "count": n,
        "latest": r(latest, 6),
        "mean": r(mean, 6),
        "stdev": r(stdev, 6),
        "min": r(lo, 6),
        "max": r(hi, 6),
        "z_score": r(z, 4) if z is not None else None,
        "percentile": r(pct, 4),
        "rank": r(rank, 4) if rank is not None else None,
    }


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Historical IV rank and percentile for a Deribit expiry, computed over the local SQLite "
            "history collected by the ATM-term job. IV rank = (latest - min) / (max - min), 0..1. "
            "Percentile = fraction of past points strictly below latest, plus half the ties. Both are "
            "informative only — the lookback window matters more than tiny differences. Needs the "
            "collector enabled and some stored history (otherwise ok=false)."
        ),
    )
    async def iv_rank(
        currency: Literal["BTC", "ETH"] = "BTC",
        expiry: Annotated[str, Field(description="ISO-8601 UTC expiry as stored by the collector, e.g. '2026-11-27T08:00:00+00:00'.")] = "",
        lookback_days: Annotated[int, Field(ge=1, le=365)] = 30,
        min_points: Annotated[int, Field(ge=2, le=10000, description="Minimum number of stored points required.")] = 10,
    ) -> dict[str, Any]:
        b = Builder()
        if rt.store is None:
            b.miss("store", "tradedesk", "config", "db_path is empty; set TRADEDESK_DB_PATH to enable history")
            return b.build(None, ok=False)
        try:
            await rt.store.init()
        except Exception as e:  # noqa: BLE001
            b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
            return b.build(None, ok=False)

        now = rt.now()
        until_ts = int(now.timestamp())
        since_ts = until_ts - lookback_days * 86_400
        series = await rt.store.atm_term_series(currency, expiry, since_ts, until_ts)
        if len(series) < min_points:
            b.miss(
                "atm_term",
                "store",
                "data_unavailable",
                f"only {len(series)} points stored for {currency} {expiry} in last {lookback_days}d; need >= {min_points}",
            )
            return b.build(None, ok=False)

        ivs = [row["atm_iv"] for row in series if row.get("atm_iv") is not None]
        if len(ivs) < min_points:
            b.miss("atm_term", "store", "data_unavailable", "stored rows missing atm_iv")
            return b.build(None, ok=False)

        latest = ivs[-1]
        lo, hi = min(ivs), max(ivs)
        rank = (latest - lo) / (hi - lo) if hi > lo else None
        below = sum(1 for v in ivs if v < latest)
        ties = sum(1 for v in ivs if v == latest)
        pct = (below + 0.5 * ties) / len(ivs)
        latest_ts = series[-1]["ts"]
        latest_dt = datetime.fromtimestamp(latest_ts, tz=now.tzinfo).isoformat()
        return b.build(
            {
                "currency": currency,
                "expiry": expiry,
                "lookback_days": lookback_days,
                "count": len(ivs),
                "latest": {"ts": latest_ts, "iso": latest_dt, "atm_iv": r(latest, 4)},
                "min_iv": r(lo, 4),
                "max_iv": r(hi, 4),
                "iv_rank": r(rank, 4) if rank is not None else None,
                "percentile": r(pct, 4),
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Historical z-score and percentile of annualised funding for one venue × base over the "
            "local SQLite history collected by the funding job. Reads `funding_hist.annualized` "
            "(fraction, 0.10 = 10 %/year). Returns latest, mean, stdev, min, max, z_score and "
            "percentile over the lookback window. Needs the funding collector enabled and some "
            "stored history (otherwise ok=false)."
        ),
    )
    async def funding_zscore(
        venue: Annotated[str, Field(description="Venue id as stored by the collector (e.g. 'binanceusdm', 'bybit', 'hyperliquid').")],
        base: Literal["BTC", "ETH"] = "BTC",
        lookback_days: Annotated[int, Field(ge=1, le=365)] = 30,
        min_points: Annotated[int, Field(ge=2, le=10000)] = 10,
    ) -> dict[str, Any]:
        b = Builder()
        if rt.store is None:
            b.miss("store", "tradedesk", "config", "db_path is empty; set TRADEDESK_DB_PATH to enable history")
            return b.build(None, ok=False)
        try:
            await rt.store.init()
        except Exception as e:  # noqa: BLE001
            b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
            return b.build(None, ok=False)
        now = rt.now()
        until_ts = int(now.timestamp())
        since_ts = until_ts - lookback_days * 86_400
        series = await rt.store.funding_series(venue, base, since_ts, until_ts)
        if len(series) < min_points:
            b.miss(
                "funding_hist",
                "store",
                "data_unavailable",
                f"only {len(series)} points stored for {venue} {base} in last {lookback_days}d; need >= {min_points}",
            )
            return b.build(None, ok=False)
        rates = [row["annualized"] for row in series if row.get("annualized") is not None]
        if len(rates) < min_points:
            b.miss("funding_hist", "store", "data_unavailable", "stored rows missing annualized")
            return b.build(None, ok=False)
        stats = _summary(rates, latest=rates[-1])
        latest_ts = series[-1]["ts"]
        latest_dt = datetime.fromtimestamp(latest_ts, tz=now.tzinfo).isoformat()
        return b.build(
            {
                "venue": venue,
                "base": base,
                "lookback_days": lookback_days,
                "latest_ts": latest_ts,
                "latest_iso": latest_dt,
                "annualized": stats,
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Historical z-score and percentile of a smile wing (risk reversal) for one Deribit "
            "expiry over the local SQLite history. `delta_target='25'` reads `rr25`, '10' reads "
            "`rr10` (both stored by the atm_term collector job). RR is IV_call - IV_put at that "
            "call-equivalent delta — negative = puts richer (downside skew). Returns latest, mean, "
            "stdev, min, max, z_score and percentile. Needs the atm_term collector enabled and "
            "some stored history (otherwise ok=false)."
        ),
    )
    async def skew_percentile(
        currency: Literal["BTC", "ETH"] = "BTC",
        expiry: Annotated[str, Field(description="ISO-8601 UTC expiry as stored by the collector.")] = "",
        delta_target: Literal["25", "10"] = "25",
        lookback_days: Annotated[int, Field(ge=1, le=365)] = 30,
        min_points: Annotated[int, Field(ge=2, le=10000)] = 10,
    ) -> dict[str, Any]:
        b = Builder()
        if rt.store is None:
            b.miss("store", "tradedesk", "config", "db_path is empty; set TRADEDESK_DB_PATH to enable history")
            return b.build(None, ok=False)
        try:
            await rt.store.init()
        except Exception as e:  # noqa: BLE001
            b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
            return b.build(None, ok=False)
        now = rt.now()
        until_ts = int(now.timestamp())
        since_ts = until_ts - lookback_days * 86_400
        series = await rt.store.atm_term_series(currency, expiry, since_ts, until_ts)
        if len(series) < min_points:
            b.miss(
                "atm_term",
                "store",
                "data_unavailable",
                f"only {len(series)} points stored for {currency} {expiry} in last {lookback_days}d; need >= {min_points}",
            )
            return b.build(None, ok=False)
        key = "rr25" if delta_target == "25" else "rr10"
        rrs = [row[key] for row in series if row.get(key) is not None]
        if len(rrs) < min_points:
            b.miss("atm_term", "store", "data_unavailable", f"stored rows missing {key}")
            return b.build(None, ok=False)
        stats = _summary(rrs, latest=rrs[-1])
        latest_ts = series[-1]["ts"]
        latest_dt = datetime.fromtimestamp(latest_ts, tz=now.tzinfo).isoformat()
        return b.build(
            {
                "currency": currency,
                "expiry": expiry,
                "delta_target": delta_target,
                "lookback_days": lookback_days,
                "latest_ts": latest_ts,
                "latest_iso": latest_dt,
                "rr": stats,
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Historical OHLCV from the local SQLite store (table ``ohlcv_hist``). Needs the "
            "ohlcv_hist collector enabled (TRADEDESK_COLLECT_OHLCV_HIST_ENABLED=1 with a market "
            "list in TRADEDESK_COLLECT_OHLCV_HIST_MARKETS). Returns stored bars for the "
            "(venue, symbol, timeframe), optionally bounded by since/until (unix MILLISECONDS, "
            "ccxt convention). Reports the inclusive date range of what's actually stored."
        ),
    )
    async def ohlcv_history(
        venue: Annotated[str, Field(description="ccxt venue id, e.g. 'binanceusdm'.")],
        symbol: Annotated[str, Field(description="ccxt symbol, e.g. 'BTC/USDT:USDT'.")],
        timeframe: Annotated[str, Field(description="OHLCV timeframe as stored, e.g. '1h'.")] = "1h",
        since_ts_ms: Annotated[int | None, Field(description="Unix ms lower bound (inclusive).")] = None,
        until_ts_ms: Annotated[int | None, Field(description="Unix ms upper bound (inclusive).")] = None,
        limit: Annotated[int | None, Field(ge=1, le=50_000)] = None,
    ) -> dict[str, Any]:
        b = Builder()
        if rt.store is None:
            b.miss("store", "tradedesk", "config", "db_path is empty; set TRADEDESK_DB_PATH to enable history")
            return b.build(None, ok=False)
        try:
            await rt.store.init()
        except Exception as e:  # noqa: BLE001
            b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
            return b.build(None, ok=False)
        rows = await rt.store.ohlcv_range(
            venue=venue,
            symbol=symbol,
            timeframe=timeframe,
            since_ts=since_ts_ms or 0,
            until_ts=until_ts_ms,
            limit=limit,
        )
        if not rows:
            b.miss(
                "ohlcv_hist",
                "store",
                "data_unavailable",
                "no bars stored for this (venue, symbol, timeframe) in the requested window",
            )
            return b.build({"venue": venue, "symbol": symbol, "timeframe": timeframe, "count": 0, "bars": []}, ok=False)
        return b.build(
            {
                "venue": venue,
                "symbol": symbol,
                "timeframe": timeframe,
                "count": len(rows),
                "first_ts_ms": rows[0]["ts"],
                "last_ts_ms": rows[-1]["ts"],
                "bars": rows,
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Detect horizontal ranges on OHLCV plus the two short-lived events the DoubleTop "
            "'Рейнджі' lecture names: SWEEPS (candles whose wick pokes past a prior swing "
            "level and closes back on the original side — a liquidity grab) and DEVIATIONS "
            "(closes beyond the range boundary that are pulled back within `max_deviation_bars`). "
            "Pulls bars from ohlcv_hist (default) or live ccxt (`source='live'`). "
            "Interpretation hints: in a bull-range price works the LOWER boundary (sweeps / "
            "deviations down), in a bear-range the UPPER boundary — combine with market_regime "
            "to pick the side. Returns ranges with {top, bottom, mid, width_pct}, swing high/low "
            "pivots, sweeps and per-range deviations."
        ),
    )
    async def range_detect(
        venue: Annotated[str, Field(description="ccxt venue id, e.g. 'binanceusdm'.")],
        symbol: Annotated[str, Field(description="ccxt symbol, e.g. 'BTC/USDT:USDT'.")],
        timeframe: Annotated[str, Field(description="OHLCV timeframe, e.g. '1h'.")] = "1h",
        lookback_bars: Annotated[int, Field(ge=20, le=10_000)] = 500,
        min_bars: Annotated[int, Field(ge=5, le=1000, description="Minimum bars in a range.")] = 20,
        max_width_pct: Annotated[float, Field(gt=0, le=50, description="Max (high - low) / mid, in percent.")] = 10.0,
        swing_lookback: Annotated[int, Field(ge=1, le=50, description="Pivot look-around for swing points.")] = 3,
        sweep_wick_ratio: Annotated[float, Field(ge=0.0, le=1.0, description="Min piercing wick as fraction of candle range.")] = 0.5,
        max_deviation_bars: Annotated[int, Field(ge=1, le=20, description="How quickly a deviation must snap back.")] = 3,
        source: Annotated[Literal["store", "live"], Field(description="Where bars come from.")] = "store",
    ) -> dict[str, Any]:
        b = Builder()
        bars: list[dict[str, Any]] = []
        if source == "live":
            fetched = await b.guard(f"ohlcv:{venue}", f"ccxt:{venue}", rt.exchanges.ohlcv(venue, symbol, timeframe, lookback_bars))
            raw = fetched or []
            bars = [
                {
                    "ts": int(x[0]),
                    "open": float(x[1]),
                    "high": float(x[2]),
                    "low": float(x[3]),
                    "close": float(x[4]),
                    "volume": float(x[5]) if len(x) >= 6 else 0.0,
                }
                for x in raw
                if isinstance(x, (list, tuple)) and len(x) >= 5
            ]
        else:
            if rt.store is None:
                b.miss("store", "tradedesk", "config", "db_path is empty; set TRADEDESK_DB_PATH to use source='store'")
                return b.build(None, ok=False)
            try:
                await rt.store.init()
            except Exception as e:  # noqa: BLE001
                b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
                return b.build(None, ok=False)
            rows = await rt.store.ohlcv_range(venue=venue, symbol=symbol, timeframe=timeframe, since_ts=0, limit=lookback_bars * 2)
            bars = rows[-lookback_bars:] if rows else []
        if len(bars) < min_bars:
            b.miss("bars", "tradedesk", "data_unavailable", f"only {len(bars)} bars; need >= {min_bars}")
            return b.build(None, ok=False)
        res = classify_ranges(
            bars,
            min_bars=min_bars,
            max_width_pct=max_width_pct,
            swing_lookback=swing_lookback,
            sweep_wick_ratio=sweep_wick_ratio,
            max_deviation_bars=max_deviation_bars,
        )
        return b.build(
            {
                "venue": venue,
                "symbol": symbol,
                "timeframe": timeframe,
                "source": source,
                "lookback_bars": lookback_bars,
                "n_bars_evaluated": res["n_bars"],
                "n_ranges": len(res["ranges"]),
                "n_sweeps": len(res["sweeps"]),
                "latest_range": (
                    {
                        "top": r(res["ranges"][-1]["top"], 6),
                        "bottom": r(res["ranges"][-1]["bottom"], 6),
                        "mid": r(res["ranges"][-1]["mid"], 6),
                        "width_pct": r(res["ranges"][-1]["width_pct"], 4),
                        "n_bars": res["ranges"][-1]["n_bars"],
                        "start_ts": res["ranges"][-1]["start_ts"],
                        "end_ts": res["ranges"][-1]["end_ts"],
                        "classification": (res["classifications"] or [None])[-1],
                    }
                    if res["ranges"]
                    else None
                ),
                "ranges": [
                    {
                        "top": r(x["top"], 6),
                        "bottom": r(x["bottom"], 6),
                        "mid": r(x["mid"], 6),
                        "width_pct": r(x["width_pct"], 4),
                        "n_bars": x["n_bars"],
                        "start_ts": x["start_ts"],
                        "end_ts": x["end_ts"],
                        "classification": res["classifications"][i] if i < len(res["classifications"]) else None,
                    }
                    for i, x in enumerate(res["ranges"])
                ],
                "swing_highs": [{"ts": s["ts"], "price": r(s["price"], 6)} for s in res["swings"]["highs"]],
                "swing_lows": [{"ts": s["ts"], "price": r(s["price"], 6)} for s in res["swings"]["lows"]],
                "sweeps": [
                    {
                        "ts": s["ts"],
                        "side": s["side"],
                        "swept_level": r(s["swept_level"], 6),
                        "reversal_pct": r(s["reversal_pct"], 4),
                    }
                    for s in res["sweeps"]
                ],
                "deviations_per_range": res["deviations_per_range"],
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Classify the market regime (up / down / chop) over the trailing `lookback_bars` bars. "
            "Pulls OHLCV from the local `ohlcv_hist` by default, or set `source='live'` to fetch "
            "fresh bars via ccxt (handy before the collector has backfilled anything). Reports "
            "slope (log and %/bar), stdev of bar log-returns and a Sharpe-shaped `trend_strength` "
            "that drives the regime label at `trend_threshold` (default 0.5). Classifier is "
            "deliberately simple; feed `trend_threshold` higher to only flag very clean trends."
        ),
    )
    async def market_regime(
        venue: Annotated[str, Field(description="ccxt venue id, e.g. 'binanceusdm'.")],
        symbol: Annotated[str, Field(description="ccxt symbol, e.g. 'BTC/USDT:USDT'.")],
        timeframe: Annotated[str, Field(description="OHLCV timeframe, e.g. '1h'.")] = "1h",
        lookback_bars: Annotated[int, Field(ge=3, le=10_000)] = 100,
        trend_threshold: Annotated[float, Field(ge=0, le=10, description="trend_strength cutoff for up/down vs chop.")] = 0.5,
        source: Annotated[Literal["store", "live"], Field(description="Where bars come from.")] = "store",
    ) -> dict[str, Any]:
        b = Builder()
        bars: list[dict[str, Any]] = []
        if source == "live":
            fetched = await b.guard(f"ohlcv:{venue}", f"ccxt:{venue}", rt.exchanges.ohlcv(venue, symbol, timeframe, lookback_bars))
            raw = fetched or []
            bars = [
                {"ts": int(x[0]), "open": float(x[1]), "high": float(x[2]), "low": float(x[3]), "close": float(x[4])}
                for x in raw
                if isinstance(x, (list, tuple)) and len(x) >= 5
            ]
        else:
            if rt.store is None:
                b.miss("store", "tradedesk", "config", "db_path is empty; set TRADEDESK_DB_PATH to use source='store'")
                return b.build(None, ok=False)
            try:
                await rt.store.init()
            except Exception as e:  # noqa: BLE001
                b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
                return b.build(None, ok=False)
            # Pull a little more than we need so OLS has enough rows after dropping bad entries.
            rows = await rt.store.ohlcv_range(
                venue=venue,
                symbol=symbol,
                timeframe=timeframe,
                since_ts=0,
                limit=lookback_bars * 2,
            )
            bars = rows[-lookback_bars:] if rows else []

        if len(bars) < 3:
            b.miss(
                "ohlcv_hist" if source == "store" else "ohlcv",
                "store" if source == "store" else f"ccxt:{venue}",
                "data_unavailable",
                f"only {len(bars)} bars available; need at least 3",
            )
            return b.build(None, ok=False)

        res = classify_regime(bars, lookback=lookback_bars, trend_threshold=trend_threshold)
        return b.build(
            {
                "venue": venue,
                "symbol": symbol,
                "timeframe": timeframe,
                "lookback_bars": lookback_bars,
                "source": source,
                "regime": res["regime"],
                "count": res["count"],
                "slope_pct_per_bar": r(res.get("slope_pct_per_bar"), 6),
                "slope_per_bar_log": r(res.get("slope_per_bar_log"), 8),
                "stdev_bar_log": r(res.get("stdev_bar_log"), 6),
                "trend_strength": r(res.get("trend_strength"), 3),
                "trend_threshold": trend_threshold,
                "window_return_pct": r(res.get("window_return_pct"), 4),
                "first_ts": res.get("first_ts"),
                "last_ts": res.get("last_ts"),
                "first_close": r(res.get("first_close"), 6),
                "last_close": r(res.get("last_close"), 6),
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Recent alert history (from the local SQLite store). Returns up to `limit` most-recent "
            "alerts with id, rule_id, timestamp, channel, delivered flag, delivery_error (if any) "
            "and the full payload. Needs the alerts job enabled (TRADEDESK_ALERTS_ENABLED=1) and "
            "some fired alerts; empty history is a valid ok=true response."
        ),
    )
    async def alerts_recent(
        limit: Annotated[int, Field(ge=1, le=500, description="Max rows to return.")] = 50,
    ) -> dict[str, Any]:
        b = Builder()
        if rt.store is None:
            b.miss("store", "tradedesk", "config", "db_path is empty; set TRADEDESK_DB_PATH to enable history")
            return b.build(None, ok=False)
        try:
            await rt.store.init()
        except Exception as e:  # noqa: BLE001
            b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
            return b.build(None, ok=False)
        rows = await rt.store.recent_alerts(limit=limit)
        return b.build({"count": len(rows), "alerts": rows})
