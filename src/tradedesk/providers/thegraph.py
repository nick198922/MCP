"""The Graph decentralized-network gateway (free API key): GraphQL queries to subgraphs by subgraph id.

`POST https://gateway.thegraph.com/api/subgraphs/id/<subgraph_id>` with body `{"query": ..., "variables": {...}}` and the
key in the `Authorization: Bearer <GRAPH_API_KEY>` header (never in the URL, so it cannot reach evidence, logs or error
texts; any echo of it in an error is scrubbed by `Http`). The answer is `{"data": {...}}` or, for a bad query, an
indexer failure or an auth problem, `{"errors": [{"message": ...}], "data": null | partial}` **with HTTP 200**; any
`errors` entry is treated as a failure (partial data is not returned); it reaches health once, as an error (the `Http`
body check), not as a success plus an error.

Shape per the Graph docs (GraphQL over HTTP); the provider was written without network access, the tests use hand-built
synthetic answers until `scripts/thegraph_sample.py` has saved a live sample. The gateway sits behind Cloudflare: the default `Python-urllib` agent gets 403 "error 1010" (seen
2026-10-25), so requests carry an explicit `User-Agent`.

Budget: the free tier is 100k queries a month. Besides the token bucket (`RATE_PER_SECOND`), this class counts the
queries it sends per UTC day and refuses more than `DAILY_QUERY_BUDGET` (~90k per 30 days) with
ProviderError("rate_limited"), recorded in health. The counter is per process (a restart resets it) and counts one query per call, while
`Http` may retry a 429/5xx once more. Only successful answers are cached (an HTTP 200 body may carry `errors`, so the
generic `Http` cache is off here, as for GoPlus).

Paging: the Graph caps `first` at 1000 rows and makes deep `skip` slow, so `query_pages` walks by `id_gt` cursor.
Point-in-time reads go through the standard `block: {number: N}` argument in the caller's query.

Without a key, or with a key that cannot go into a header (non-ASCII, control characters), every call raises
ProviderError("config") before any request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from ..core.cache import TTLCache
from ..core.envelope import Fetched, utcnow
from ..core.errors import ProviderError
from ..core.health import HealthRegistry
from ..core.http import Http

BASE_URL = "https://gateway.thegraph.com/api"
USER_AGENT = "tradedesk-mcp/0.1"
# Polymarket main subgraph (https://thegraph.com/docs/en/subgraphs/guides/polymarket/). The P&L subgraph id is not
# pinned here: it comes from settings (TRADEDESK_GRAPH_PNL_SUBGRAPH_ID) once read off Graph Explorer.
POLYMARKET_SUBGRAPH_ID = "Bx1W4S7kDVxs9gC3s2G6DS8kdNBJNVhMviCtin2DiBp"
RATE_PER_SECOND = 0.5
BURST = 2
DAILY_QUERY_BUDGET = 3_000  # x 30 days = 90k, under the free 100k a month
TTL_SECONDS = 600.0
PAGE_CAP = 1000  # the Graph refuses first > 1000
META_QUERY = "{ _meta { block { number hash timestamp } deployment hasIndexingErrors } }"

_SUBGRAPH_ID = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,64}$")  # base58
_AUTH_WORDS = ("auth error", "api key", "apikey", "unauthorized", "authorization")
_LIMIT_WORDS = ("rate limit", "too many", "quota", "query limit")


def key_problem(api_key: str | None) -> str | None:
    """Why a key cannot go into an HTTP header (None: it can, or there is no key at all, see `has_key`).
    httpx would raise UnicodeEncodeError, outside its own error types, on a non-ASCII header value."""
    if api_key and not (api_key.isascii() and api_key.isprintable()):
        return "GRAPH_API_KEY has non-ASCII or control characters (check ~/.config/tradedesk/onchain.env); it is not sent"
    return None


def headers(api_key: str | None) -> dict[str, str]:
    h = {"User-Agent": USER_AGENT}
    if api_key and key_problem(api_key) is None:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _compact(query: str, limit: int = 160) -> str:
    return " ".join(query.split())[:limit]


def _body_check(body: Any) -> tuple[str, str] | None:
    """`Http` body check: an HTTP 200 answer with `errors`, or without a `data` object, is a failure."""
    if not isinstance(body, dict):
        return "backend_error", "unexpected response shape (not an object)"
    errors = body.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        msg = str(first.get("message", "")) if isinstance(first, dict) else str(first)
        low = msg.lower()
        reason = (
            "auth" if any(w in low for w in _AUTH_WORDS) else "rate_limited" if any(w in low for w in _LIMIT_WORDS) else "backend_error"
        )
        # third-party text: Http scrubs its secrets, then redacts and cuts it, before health and ProviderError see it
        return reason, f"GraphQL error: {msg}"
    if not isinstance(body.get("data"), dict):
        return "backend_error", "GraphQL answer without data"
    return None


class TheGraph:
    name = "thegraph"

    def __init__(
        self,
        http: Http,
        *,
        has_key: bool,
        key_problem: str | None = None,
        pnl_subgraph_id: str | None = None,
        health: HealthRegistry | None = None,
        ttl: float = TTL_SECONDS,
        daily_budget: int = DAILY_QUERY_BUDGET,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.http = http
        self.has_key = has_key
        self._key_problem = key_problem  # see `key_problem()`: such a key is never put into a header
        self.pnl_subgraph_id = pnl_subgraph_id or None
        self._health = health
        self._ttl = ttl
        self._budget = daily_budget
        self._clock = clock
        self._day: date | None = None
        self._sent = 0
        self._cache = TTLCache(max_items=512)

    async def aclose(self) -> None:
        await self.http.aclose()

    def _error(self, reason: str, detail: str) -> ProviderError:
        if self._health:
            self._health.error(self.name, reason, detail)
        return ProviderError(self.name, reason, detail)

    def queries_today(self) -> int:
        """Queries sent in the current UTC day by this process (cache hits not counted)."""
        return self._sent if self._day == self._clock().date() else 0

    def _spend(self) -> None:
        today = self._clock().date()
        if self._day != today:
            self._day, self._sent = today, 0
        if self._sent >= self._budget:
            raise self._error("rate_limited", f"daily query budget of {self._budget} reached (free tier 100k a month); resets at 00:00 UTC")
        self._sent += 1

    async def _fetch(self, subgraph_id: str, query: str, variables: dict[str, Any]) -> Fetched:
        self._spend()
        f = await self.http.post_json(
            f"/subgraphs/id/{subgraph_id}",
            json={"query": query, "variables": variables},
            ttl=0,  # never cache in Http: an HTTP 200 body may carry `errors` (see module docstring)
            tool="graphql",
            args={"subgraph_id": subgraph_id, "query": _compact(query), "variables": variables},
            check=_body_check,  # `errors` in a 200 body: one health error, not a success plus an error
        )
        return Fetched(f.data["data"], f.evidence)

    async def query(self, subgraph_id: str, query: str, variables: dict[str, Any] | None = None) -> Fetched:
        """Run one GraphQL query against a subgraph. Returns Fetched(data dict of the answer, evidence).

        Raises ProviderError: `config` (no key, malformed subgraph id), `auth`, `rate_limited` (HTTP 429, a GraphQL
        rate/quota error, or the local daily budget), `backend_error` (any other GraphQL `errors`, bad shape),
        `network`. Successful answers are cached for `ttl` seconds by (subgraph, query, variables).
        """
        if not self.has_key:
            raise ProviderError(self.name, "config", "GRAPH_API_KEY is not set (free key at thegraph.com/studio/apikeys)")
        if self._key_problem:
            raise ProviderError(self.name, "config", self._key_problem)
        if not isinstance(subgraph_id, str) or not _SUBGRAPH_ID.match(subgraph_id):
            raise ProviderError(self.name, "config", "malformed subgraph id (expected a base58 id from Graph Explorer)")
        vars_ = dict(variables or {})
        if self._ttl <= 0:
            return await self._fetch(subgraph_id, query, vars_)
        key = (subgraph_id, query, json.dumps(vars_, sort_keys=True, default=str))
        fetched, from_cache = await self._cache.get_or_fetch(key, self._ttl, lambda: self._fetch(subgraph_id, query, vars_))
        if from_cache:
            return Fetched(fetched.data, fetched.evidence.model_copy(update={"cached": True}))
        return fetched

    async def query_pages(
        self,
        subgraph_id: str,
        query: str,
        entity: str,
        variables: dict[str, Any] | None = None,
        *,
        page_size: int = PAGE_CAP,
        max_pages: int = 50,
    ) -> Fetched:
        """Walk an entity list by `id_gt` cursor. Returns Fetched({"rows", "pages", "complete"}, evidence of the last page).

        The query must declare `$first: Int!` and `$lastId: String!` (or `ID!`) and use them as
        `<entity>(first: $first, orderBy: id, orderDirection: asc, where: {id_gt: $lastId, ...})`; `entity` is the
        field read from `data`. `complete` is False when `max_pages` stopped the walk.
        """
        if "$first" not in query or "$lastId" not in query:
            raise ProviderError(self.name, "config", "query_pages needs $first and $lastId in the query")
        if not 1 <= page_size <= PAGE_CAP:
            raise ProviderError(self.name, "config", f"page_size must be 1..{PAGE_CAP}")
        rows: list[Any] = []
        last_id = ""
        last: Fetched | None = None
        pages = 0
        complete = False
        for _ in range(max_pages):
            f = await self.query(subgraph_id, query, {**(variables or {}), "first": page_size, "lastId": last_id})
            pages += 1
            last = f
            # The two refusals below follow a valid answer (already counted in health as a success): they are about the
            # caller's query shape, so they do not go to health a second time.
            page = f.data.get(entity)
            if not isinstance(page, list):
                raise ProviderError(self.name, "backend_error", f"field {entity!r} is not a list in the answer")
            rows.extend(page)
            if len(page) < page_size:
                complete = True
                break
            tail = page[-1].get("id") if isinstance(page[-1], dict) else None
            if not isinstance(tail, str) or tail == last_id:
                raise ProviderError(self.name, "backend_error", "page without a new last id: order the query by id asc and select id")
            last_id = tail
        if last is None:
            raise ProviderError(self.name, "config", "max_pages must be >= 1")
        ev = last.evidence.model_copy(update={"args": {**(last.evidence.args or {}), "pages": pages, "complete": complete}})
        return Fetched({"rows": rows, "pages": pages, "complete": complete}, ev)

    async def meta(self, subgraph_id: str) -> Fetched:
        """`_meta` of a subgraph: indexed block {number, hash, timestamp}, deployment id, hasIndexingErrors."""
        return await self.query(subgraph_id, META_QUERY)

    async def polymarket(self, query: str, variables: dict[str, Any] | None = None) -> Fetched:
        """Query the Polymarket main subgraph (`POLYMARKET_SUBGRAPH_ID`)."""
        return await self.query(POLYMARKET_SUBGRAPH_ID, query, variables)

    async def polymarket_pnl(self, query: str, variables: dict[str, Any] | None = None) -> Fetched:
        """Query the Polymarket P&L subgraph; its id comes from settings (TRADEDESK_GRAPH_PNL_SUBGRAPH_ID)."""
        if not self.pnl_subgraph_id:
            raise ProviderError(self.name, "config", "Polymarket P&L subgraph id is not set (TRADEDESK_GRAPH_PNL_SUBGRAPH_ID)")
        return await self.query(self.pnl_subgraph_id, query, variables)
