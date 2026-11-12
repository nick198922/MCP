"""Wikimedia public APIs (no key): daily article pageviews and en.wikipedia page metadata (research scripts, not tools).

Two hosts:
- ``wikimedia.org/api/rest_v1`` Pageviews (per-article daily views since 2015-07-01). ``agent`` is ``user`` (humans as
  classified by Wikimedia *today*: the bot/spider classification has been revised over time, so history is "as known
  now"), ``all-agents``, ``spider`` or ``automated``. Views are recorded under the title that was requested: a renamed
  article has its pre-move views under the old title, and a redirect has its own series. A day with zero views is
  omitted from the answer; an article without any views in the range answers HTTP 404 (``data_unavailable``).
- ``en.wikipedia.org/w/api.php`` action API: resolves a title through redirects to a page id, with the short
  description, the disambiguation flag and the namespace-0 redirects that point to it now.

Wikimedia asks automated clients for a descriptive User-Agent (generic library agents are throttled or refused).
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

PAGEVIEWS_URL = "https://wikimedia.org/api/rest_v1"
ENWIKI_URL = "https://en.wikipedia.org"
# Wikimedia's robot policy (https://w.wiki/4wJS) refuses agents without a contact (site or e-mail). The contact is the
# public project repository (owner's decision, Q-20261111-1); TRADEDESK_WIKIMEDIA_CONTACT only overrides it.
DEFAULT_CONTACT = "https://github.com/nick198922/MCP"
CONTACT_ENV = "TRADEDESK_WIKIMEDIA_CONTACT"
AGENTS = ("user", "all-agents", "spider", "automated")


def user_agent(contact: str | None = None) -> str:
    """Descriptive User-Agent with a contact, as Wikimedia's policy requires."""
    c = (contact if contact is not None else os.environ.get(CONTACT_ENV, "")).strip() or DEFAULT_CONTACT
    return f"tradedesk-mcp/0.1 ({c}; read-only research client; pageviews)"


USER_AGENT = user_agent(DEFAULT_CONTACT)


def article_key(title: str) -> str:
    """Title as the Pageviews path wants it: spaces to underscores, every reserved character (incl. '/') escaped."""
    return quote(title.replace(" ", "_"), safe="")


def pageviews_rows(body: Any) -> list[tuple[str, int]]:
    """``(YYYYMMDD, views)`` pairs from a per-article daily answer, sorted by day."""
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise ProviderError("wikimedia", "backend_error", "missing items array in pageviews answer")
    out = []
    for it in body["items"]:
        ts, v = str(it.get("timestamp", "")), it.get("views")
        if len(ts) < 8 or not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ProviderError("wikimedia", "backend_error", f"bad pageviews item {it!r}"[:200])
        out.append((ts[:8], v))
    return sorted(out)


def page_summary(body: Any) -> dict[str, Any]:
    """One resolved page from an action=query answer (formatversion=2): id, canonical title, short description,
    disambiguation flag, redirect chain followed, current namespace-0 redirects. ``pageid`` is None if missing."""
    if not isinstance(body, dict) or not isinstance(body.get("query"), dict):
        raise ProviderError("wikipedia", "backend_error", "missing query object in action API answer")
    q = body["query"]
    pages = q.get("pages") or []
    if len(pages) != 1:
        raise ProviderError("wikipedia", "backend_error", f"expected one page, got {len(pages)}")
    p = pages[0]
    props = p.get("pageprops") or {}
    return {
        "pageid": None if p.get("missing") or p.get("invalid") else p.get("pageid"),
        "title": p.get("title"),
        "ns": p.get("ns"),
        "shortdesc": props.get("wikibase-shortdesc"),
        "disambiguation": "disambiguation" in props,
        "redirected_from": [r.get("from") for r in q.get("redirects") or []],
        "normalized_from": [r.get("from") for r in q.get("normalized") or []],
        "redirects": sorted(r.get("title") for r in p.get("redirects") or [] if r.get("ns", 0) == 0),
        "lastrevid": p.get("lastrevid"),
        "touched": p.get("touched"),
    }


class Wikimedia:
    name = "wikimedia"

    def __init__(self, pageviews: Http, enwiki: Http) -> None:
        self.http = pageviews
        self.enwiki = enwiki

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.enwiki.aclose()

    async def pageviews_daily(self, title: str, start: str, end: str, agent: str = "user") -> Fetched:
        """Daily en.wikipedia views of one title, all access methods. ``start``/``end`` are ``YYYYMMDD`` (inclusive).
        Returns ``[(YYYYMMDD, views), ...]``; days with zero views are absent."""
        if agent not in AGENTS:
            raise ProviderError(self.name, "config", f"agent must be one of {AGENTS}")
        path = f"/metrics/pageviews/per-article/en.wikipedia/all-access/{agent}/{article_key(title)}/daily/{start}00/{end}00"
        f = await self.http.get_json(path, tool="wiki_pageviews", args={"title": title, "agent": agent, "start": start, "end": end})
        return Fetched(pageviews_rows(f.data), f.evidence)

    async def page(self, title: str) -> Fetched:
        """Resolve one title (following redirects) to its page id, short description and current redirects."""
        params: dict[str, Any] = {
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "redirects": 1,
            "prop": "info|pageprops|redirects",
            "ppprop": "wikibase-shortdesc|disambiguation",
            "rdlimit": "max",
            "rdnamespace": 0,
            "titles": title,
        }
        merged: dict[str, Any] | None = None
        evidence = None
        for _ in range(20):  # continuation of the redirect list (rdcontinue); 500 per page
            f = await self.enwiki.get_json("/w/api.php", params=params, tool="wiki_page", args={"title": title})
            evidence = evidence or f.evidence
            s = page_summary(f.data)
            if merged is None:
                merged = s
            else:
                merged["redirects"] = sorted(set(merged["redirects"]) | set(s["redirects"]))
            cont = (f.data or {}).get("continue")
            if not cont:
                break
            params = {**params, **cont}
        assert merged is not None and evidence is not None
        return Fetched(merged, evidence)
