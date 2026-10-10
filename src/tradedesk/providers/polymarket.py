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


class Polymarket:
    name = "polymarket"

    def __init__(self, gamma: Http, clob: Http) -> None:
        self.gamma = gamma
        self.clob = clob

    async def aclose(self) -> None:
        await self.gamma.aclose()
        await self.clob.aclose()

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
