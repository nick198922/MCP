"""Polymarket public APIs (no key for reads).

Two hosts:
* Gamma `https://gamma-api.polymarket.com` — market metadata: question, outcomes,
  outcomePrices (JSON string), clobTokenIds (JSON string), volume, liquidity, endDate.
* CLOB  `https://clob.polymarket.com` — per-outcome order book and historical prices;
  each outcome is addressed by its `clobTokenId`.

Yes-outcome price ≈ implied probability. For multi-outcome markets the sum across
outcomes may exceed 1; `analytics.polymarket.devig_multi` normalises back to 1.
"""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
DATA_URL = "https://data-api.polymarket.com"
GAMMA_PAGE_CAP = 100  # Gamma /events silently caps `limit` at 100 rows per page and rejects deep offsets (2026-10-15)
TRADES_OFFSET_CAP = 10_000  # data-api /trades rejects offset > 10 000 with HTTP 400


class Polymarket:
    name = "polymarket"

    def __init__(self, gamma: Http, clob: Http, data: Http | None = None) -> None:
        self.gamma = gamma
        self.clob = clob
        self.data = data

    async def aclose(self) -> None:
        await self.gamma.aclose()
        await self.clob.aclose()
        if self.data is not None:
            await self.data.aclose()

    # ------------------------------------------------------------------ research helpers (scripts, not tools)

    async def events_all(self, filters: dict[str, Any], *, max_pages: int = 1000) -> Fetched:
        """Walk Gamma `/events/keyset` (cursor pagination; plain `/events` refuses deep offsets with HTTP 422).

        `filters` are passed as-is (e.g. ``{"tag_slug": "multi-strikes", "closed": "true"}`` or ``{"series_id": "45"}``).
        Returns ``{"rows": [...], "pages": n, "complete": bool}``; `complete` is True only when the API returned no
        `next_cursor`, so a caller can show the listing was not truncated. Rows are de-duplicated by event id.
        """
        rows: dict[str, Any] = {}
        pages = 0
        complete = False
        last = None
        cursor: str | None = None
        for page in range(1, max_pages + 1):
            pages = page
            params: dict[str, Any] = {**filters, "limit": GAMMA_PAGE_CAP}
            if cursor:
                params["after_cursor"] = cursor
            f = await self.gamma.get_json("/events/keyset", params=params, ttl=300, tool="events-keyset", args=params)
            if not isinstance(f.data, dict) or not isinstance(f.data.get("events"), list):
                raise ProviderError(self.name, "backend_error", "unexpected shape at /events/keyset")
            last = f
            for ev in f.data["events"]:
                if isinstance(ev, dict) and ev.get("id") is not None:
                    rows[str(ev["id"])] = ev
            cursor = f.data.get("next_cursor") or None
            if not cursor or not f.data["events"]:
                complete = True
                break
        if last is None:
            raise ProviderError(self.name, "backend_error", "no pages fetched")
        ev = last.evidence.model_copy(update={"args": {**(last.evidence.args or {}), "pages": pages, "complete": complete}})
        return Fetched({"rows": list(rows.values()), "pages": pages, "complete": complete}, ev)

    async def price_history_range(self, token_id: str, start_ts: int, end_ts: int, fidelity: int) -> Fetched:
        """CLOB `/prices-history` over an explicit [start_ts, end_ts] window (unix seconds), `fidelity` in minutes.

        For closed markets `interval=max` returns an empty list, but an explicit window with fidelity=1 returns
        ~1-minute points (observed 2026-10-15). Points are `{"t": unix_s, "p": price}`. `p` is a quote snapshot,
        not a trade price (it rarely equals a nearby trade and exists on markets with no trades); that it is the book
        midpoint is an unproven hypothesis (only ~17 % of points sit on half-cent steps), and what moment the label
        `t` refers to is unverified, so callers should cut off points well before the decision time
        (see docs/verification/2026-10-15-polymarket-threshold-vs-vol-model.md).
        """
        params: dict[str, Any] = {"market": token_id, "startTs": int(start_ts), "endTs": int(end_ts), "fidelity": int(fidelity)}
        f = await self.clob.get_json("/prices-history", params=params, ttl=300, tool="prices-history", args=params)
        if not isinstance(f.data, dict):
            raise ProviderError(self.name, "backend_error", "unexpected shape at /prices-history")
        rows = f.data.get("history") or []
        if not isinstance(rows, list):
            raise ProviderError(self.name, "backend_error", "history is not a list")
        return Fetched(rows, f.evidence)

    async def trades(
        self,
        condition_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        start: int | None = None,
        end: int | None = None,
    ) -> Fetched:
        """data-api `/trades` for one market (both outcome tokens), newest first.

        `start`/`end` are unix seconds (inclusive bounds honoured by the API); `offset` must stay <= 10 000.
        Rows carry `asset` (token id), `outcome`, `side`, `price`, `size`, `timestamp`.
        """
        if self.data is None:
            raise ProviderError(self.name, "config", "data-api client not configured")
        if offset > TRADES_OFFSET_CAP:
            raise ProviderError(self.name, "config", f"offset {offset} exceeds {TRADES_OFFSET_CAP}")
        params: dict[str, Any] = {"market": condition_id, "limit": limit, "offset": offset, "takerOnly": "false"}
        if start is not None:
            params["start"] = int(start)
        if end is not None:
            params["end"] = int(end)
        f = await self.data.get_json("/trades", params=params, ttl=300, tool="trades", args=params)
        if not isinstance(f.data, list):
            raise ProviderError(self.name, "backend_error", "unexpected shape at /trades")
        return f

    async def search(self, query: str | None, limit: int, active: bool, closed: bool) -> Fetched:
        """With `query`: Gamma `/public-search?q=` (returns events, each with a markets list).
        Without `query`: Gamma `/markets?order=volume` (top markets by volume).

        `/markets` honours `active`/`closed` as filters; `/public-search` does not, so we
        apply the same filters locally after flattening events → markets.
        """
        if query:
            params: dict[str, Any] = {"q": query, "limit": limit}
            f = await self.gamma.get_json("/public-search", params=params, ttl=30, tool="public-search", args=params)
            if not isinstance(f.data, dict):
                raise ProviderError(self.name, "backend_error", "unexpected shape at /public-search")
            events = f.data.get("events") or []
            rows: list[dict[str, Any]] = []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                for m in ev.get("markets") or []:
                    if not isinstance(m, dict):
                        continue
                    if active and not m.get("active"):
                        continue
                    if not closed and m.get("closed"):
                        continue
                    rows.append(m)
            return Fetched(rows[:limit], f.evidence)

        params = {
            "limit": limit,
            "active": "true" if active else "false",
            "closed": "true" if closed else "false",
            "order": "volume",
            "ascending": "false",
        }
        f = await self.gamma.get_json("/markets", params=params, ttl=30, tool="markets", args=params)
        if not isinstance(f.data, list):
            raise ProviderError(self.name, "backend_error", "unexpected shape at /markets")
        return f

    async def market(self, market_id: str) -> Fetched:
        """`market_id` may be the numeric id or the slug."""
        f = await self.gamma.get_json(f"/markets/{market_id}", params={}, ttl=15, tool="market", args={"id": market_id})
        if not isinstance(f.data, dict):
            raise ProviderError(self.name, "backend_error", "unexpected shape at /markets/{id}")
        return f

    async def book(self, token_id: str) -> Fetched:
        f = await self.clob.get_json("/book", params={"token_id": token_id}, ttl=2, tool="book", args={"token_id": token_id})
        if not isinstance(f.data, dict):
            raise ProviderError(self.name, "backend_error", "unexpected shape at /book")
        return f

    async def price_history(self, token_id: str, interval: str, fidelity: int | None) -> Fetched:
        params: dict[str, Any] = {"market": token_id, "interval": interval}
        if fidelity is not None:
            params["fidelity"] = fidelity
        f = await self.clob.get_json("/prices-history", params=params, ttl=30, tool="prices-history", args=params)
        if not isinstance(f.data, dict):
            raise ProviderError(self.name, "backend_error", "unexpected shape at /prices-history")
        rows = f.data.get("history") or []
        if not isinstance(rows, list):
            raise ProviderError(self.name, "backend_error", "history is not a list")
        return Fetched(rows, f.evidence)
