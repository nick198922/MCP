"""CoinGecko trending list: one snapshot per UTC hour of the free ``/search/trending`` coins.

Docs: docs/ideas/IDEA-20261028-coingecko-trending-attention-fade-forward.md, step 0 (forward-only collection; nothing
here trades). One public request through the existing provider (`rt.coingecko`, `core.http.Http`: rate limit, retries,
health). Rows are built by `analytics/coingecko_trending.snapshot_rows` into `coingecko_trending`, one per listed coin
with its 1-based position. Names and symbols are untrusted display text: stored cleaned, never acted upon.

The key is the UTC hour (``ts`` = its start); ``fetched_ts`` is the time the answer was in. The job ticks more often
than hourly (default 10 min) and does nothing once the hour is stored, so an MCP restart or a failed attempt does not
leave a hole. A failed request, a changed shape or a list without one usable coin writes nothing: the reason is logged
and the job returns 0; it never raises on a provider failure. Off by default: TRADEDESK_COLLECT_COINGECKO_TRENDING_ENABLED=1.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..analytics.coingecko_trending import hour_ts, snapshot_rows
from ..core.envelope import Builder
from .bad_rows import note_bad_rows

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

PROVIDER = "coingecko"
# (item part, field) -> stored column: the numbers `snapshot_rows` stores as NULL when malformed
FIELDS = {
    ("item", "coin_id"): "coin_num",
    ("item", "market_cap_rank"): "market_cap_rank",
    ("item", "score"): "score",
    ("data", "price"): "price_usd",
    ("data", "market_cap"): "market_cap_usd",
    ("data", "total_volume"): "volume_usd",
}


def _nulled(payload: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    """Numeric fields CoinGecko sent (not None / "") that were stored as NULL: NaN, inf, 1e308, a 400-digit integer,
    text that is not a number."""
    n = 0
    for r in rows:
        item = payload["coins"][r["rank"] - 1]["item"]
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        for (part, field), col in FIELDS.items():
            v = (item if part == "item" else data).get(field)
            n += v is not None and v != "" and r[col] is None
    return n


async def collect_coingecko_trending(rt: "Runtime") -> int:
    """Collector job. Returns the rows written (0: the hour is already stored, or the request failed and was logged)."""
    if rt.store is None:
        log.warning("coingecko_trending: no SQLite store (set TRADEDESK_DB_PATH), nothing recorded")
        return 0
    await rt.store.init()
    ts = hour_ts(rt.now())
    if await rt.store.coingecko_trending_has_hour(ts):
        return 0
    b = Builder()
    payload = await b.guard("coingecko_trending", PROVIDER, rt.coingecko.search_trending())
    rows = snapshot_rows(payload) if payload is not None else []
    if not rows:
        if payload is not None:
            b.miss("coingecko_trending", PROVIDER, "data_unavailable", "no usable coin in /search/trending")
        why = "; ".join(f"{m.field}: {m.reason} {m.detail}".strip()[:200] for m in b.missing[:2])
        log.warning("coingecko_trending: hour %d not recorded (retried on the next tick): %s", ts, why)
        return 0
    bad = _nulled(payload, rows)
    if bad:
        note_bad_rows(bad)
        log.warning("coingecko_trending: hour %d, %d malformed numeric field(s) stored as NULL", ts, bad)
    written = await rt.store.insert_coingecko_trending(ts, int(rt.now().timestamp()), rows)
    log.info(
        "coingecko_trending: hour %d, %d coins of %d listed (%s)",
        ts, len(rows), len(payload["coins"]), ",".join(r["coin_id"] for r in rows)[:200],
    )  # fmt: skip
    return written
