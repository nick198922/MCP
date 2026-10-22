"""Shared market-data readers for the carry paper code: 4h bars paged through ``rt.exchanges`` and ccxt funding rows.

Used by ``tools/carry_paper.py`` (manual perp-perp pairs) and ``collector/carry_paper_feed.py`` (the paper engine's
live feed), so neither imports private helpers from the other. Read-only public data; no orders.
"""

from __future__ import annotations

from typing import Any

from ..core.envelope import Builder
from ..runtime import Runtime

BAR_MS = 4 * 3_600_000
BAR_PAGE = 100  # OKX caps candles per request at 100; paginate rather than rely on a bigger page


def parse_bar(x: Any) -> dict[str, Any] | None:
    """ccxt row [ts, o, h, l, c, v] -> bar dict; None for malformed rows (None / non-numeric / non-positive prices)."""
    try:
        bar = {"ts": int(x[0]), "high": float(x[2]), "low": float(x[3]), "close": float(x[4])}
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return bar if min(bar["high"], bar["low"], bar["close"]) > 0 else None


async def bars_4h(rt: Runtime, b: Builder, venue: str, sym: str, since_ms: int, now_ms: int) -> list[dict[str, Any]] | None:
    """4h bars from ``since_ms`` to now, paged forward with an explicit upper bound per page (Bybit's request carries
    only ``start`` otherwise and may answer with the latest bars). None when the first page fails."""
    out: dict[int, dict[str, Any]] = {}
    cursor = since_ms
    for page in range(60):
        until = min(cursor + BAR_PAGE * BAR_MS - 1, now_ms)
        rows = await b.guard(f"ohlcv:{venue}:{sym}", f"ccxt:{venue}", rt.exchanges.ohlcv(venue, sym, "4h", BAR_PAGE, cursor, until))
        if rows is None:
            if page == 0:
                return None
            b.warn(f"{venue} {sym}: 4h bars incomplete (a later page failed); stop detection may be incomplete")
            break
        for x in rows if isinstance(rows, list) else []:
            bar = parse_bar(x)
            if bar is not None:
                out[bar["ts"]] = bar
        if until >= now_ms:
            break
        cursor = until + 1
    return [out[k] for k in sorted(out)]


def funding_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ccxt funding-history rows -> ``[{ts_ms, rate}]`` (rate as a decimal per interval); rows without a rate are dropped."""
    return [
        {"ts_ms": int(x["timestamp"]), "rate": float(x["fundingRate"])}
        for x in rows
        if x.get("fundingRate") is not None and x.get("timestamp")
    ]
