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
