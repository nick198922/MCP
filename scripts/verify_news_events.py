"""News events (IDEA-20261017-news-events), stage 0: do perps keep drifting after official negative news?

Idea card: docs/ideas/IDEA-20261017-news-events.md (section "Попередня реєстрація").

Event sources (free, no keys): Binance CMS announcement catalogs (Delisting 161, New listings 48), Bybit v5
announcements (delistings), OKX support announcements (delistings; from this server only the EEA feed answers),
DefiLlama hacks + protocols. Exact times are the publication timestamps of the announcements; hacks only have a UTC day.
Prices: Binance USD-M 1m/1h from data.binance.vision (``rt.binance_vision``, keeps delisted symbols), else Bybit linear
1m/1h from the public v5 API (it also returns delisted symbols). Funding: Binance archive (fapi for the current month),
Bybit v5 funding history. Announcement titles and bodies are untrusted text: only matched against fixed patterns.
No keys, no orders.

Stages (each caches per file under --cache-dir; a rerun resumes):
    ann      announcement archives and DefiLlama hacks/protocols
    bodies   article bodies for announcements whose tickers are not in the title
    events   event list (no prices): counts by category/source/year -> events.json
    prices   perp live at t0 (1m with volume, the day before and the day of t0), 1m windows, 1h placebo months,
             funding, BTC, Binance spot and the announcing exchange's spot (Bybit / OKX) for the collision check
             (--sample N to time it); resolution in resolve_live.json (the first run's resolve.json is kept)
    stage0   power gate: n, clusters, SD of cluster means of the primary outcome (no means)
    report   all registered tables (only after the gate)
    prereg   SHA-256 of the card section "## Попередня реєстрація"

    uv run python scripts/verify_news_events.py <stage> --cache-dir ~/.cache/tradedesk-agents/news_events
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import resource
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.news_events import (
    DAY_MS,
    HOUR_MS,
    MIN_MS,
    MULTIPLIERS,
    Bars,
    Event,
    classify_delisting,
    classify_listing,
    cluster_bootstrap,
    cluster_sd,
    cochran_q,
    dedupe,
    hack_events,
    max_move,
    mde_clusters,
    paired_diff_bootstrap,
    perp_base,
    perp_live,
    perp_symbols,
    placebo_times,
    price_at,
    reaction,
    short_trade,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.server import build_server

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "docs" / "ideas" / "IDEA-20261017-news-events.md"
BINANCE_CATALOGS = {161: "delisting", 48: "new_listing"}

# ---- registered parameters (card, "Попередня реєстрація")
START = int(datetime(2023, 1, 1, tzinfo=UTC).timestamp() * 1000)
END = int(datetime(2026, 10, 13, tzinfo=UTC).timestamp() * 1000)  # t0 < END: the 72.5 h window ends before the data
REACT = {"+5m": 5 * MIN_MS, "+15m": 15 * MIN_MS, "+60m": HOUR_MS, "+4h": 4 * HOUR_MS, "+24h": 24 * HOUR_MS, "+72h": 72 * HOUR_MS}
DELAYS = (5, 15, 30)  # minutes
HOLDS = (24, 72)  # hours after entry
FEE = 0.00055  # taker per side
SLIP = 0.0015  # spread + slippage per side (base)
COST = FEE + SLIP
COST_STRESS = FEE + 3 * SLIP
STOP = 0.25
N_PLACEBO = 20
HACK_MIN_USD = 1e6
THRESH = 0.10
GATE_MDE = 0.10
# Amendment after the first report (critic's review 2026-10-17): "perp at t0" now means "live at t0" (perp_live:
# price range over the prior 24 h > 0 and volume over the prior 60 min > 0), because the Binance archive keeps flat
# zero-volume bars after a perp is settled. The first run's resolution (resolve.json) is kept for the comparison.
RESOLVE_FILE = "resolve_live.json"
OUT_SUFFIX = "_live"  # measured / stage0 / report of the amended run; the first run's files stay as they were
CURATED = [  # owner's examples (library entry): descriptive only, excluded from H1
    Event(
        "BLUR",
        int(datetime(2026, 10, 12, 19, 59, 48, tzinfo=UTC).timestamp() * 1000),
        "shutdown",
        "curated",
        "c:BLUR",
        note="blur.io frontend deprecation",
    ),
    Event(
        "BLAST",
        int(datetime(2026, 10, 2, 14, 45, 23, tzinfo=UTC).timestamp() * 1000),
        "shutdown",
        "curated",
        "c:BLAST",
        note="Blast L2 shutting down",
    ),
]


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def ymd(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def ymdhm(ms: int | float) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(p)


def out(lines: list[str], s: str = "") -> None:
    print(s, flush=True)
    lines.append(s)


# ================================================================== announcements


async def stage_ann(cache: Path) -> None:
    bn = Http("binance_cms", "https://www.binance.com", rate=0.5, burst=1, timeout=30, retries=2)
    by = Http("bybit_ann", "https://api.bybit.com", rate=1, burst=1, timeout=30, retries=2)
    okx = Http("okx_ann", "https://www.okx.com", rate=0.5, burst=1, timeout=30, retries=2)
    ll = Http("defillama", "https://api.llama.fi", rate=0.5, burst=1, timeout=90, retries=2)
    try:
        for cat, label in BINANCE_CATALOGS.items():
            page = 1
            while True:
                p = cache / "ann" / f"binance_{cat}_{page:03d}.json"
                if not p.exists():
                    f = await bn.get_json(
                        "/bapi/composite/v1/public/cms/article/list/query",
                        params={"type": 1, "catalogId": cat, "pageNo": page, "pageSize": 50},
                    )
                    wr(p, f.data["data"]["catalogs"][0]["articles"])
                arts = rd(p)
                if page % 10 == 1:
                    print(f"  binance {label} page {page}: {len(arts)} rss={rss_mb():.0f}MB", flush=True)
                if not arts or len(arts) < 50:
                    break
                page += 1
        page = 1
        while True:
            p = cache / "ann" / f"bybit_delistings_{page:03d}.json"
            if not p.exists():
                f = await by.get_json(
                    "/v5/announcements/index", params={"locale": "en-US", "type": "delistings", "limit": 20, "page": page}
                )
                wr(p, f.data["result"]["list"])
            if not rd(p):
                break
            page += 1
        print(f"  bybit delistings pages: {page - 1}", flush=True)
        page = 1
        while True:
            p = cache / "ann" / f"okx_delistings_{page:03d}.json"
            if not p.exists():
                f = await okx.get_json("/api/v5/support/announcements", params={"annType": "announcements-delistings", "page": page})
                wr(p, f.data["data"][0]["details"] if f.data["data"] else [])
            if not rd(p):
                break
            page += 1
        print(f"  okx delistings pages: {page - 1}", flush=True)
        for name, path in (("hacks", "/hacks"), ("protocols", "/protocols")):
            p = cache / "ann" / f"llama_{name}.json"
            if not p.exists():
                f = await ll.get_json(path)
                data = f.data
                if name == "protocols":  # keep only the fields we use
                    data = [{k: x.get(k) for k in ("id", "name", "symbol", "gecko_id", "slug", "parentProtocol")} for x in data]
                wr(p, data)
            print(f"  llama {name}: {len(rd(p))}", flush=True)
    finally:
        for h in (bn, by, okx, ll):
            await h.aclose()


def load_ann(cache: Path) -> list[dict[str, Any]]:
    """Flat list ``{source, catalog, ms, title, id, url}`` from the cached pages."""
    rows: list[dict[str, Any]] = []
    for p in sorted((cache / "ann").glob("binance_*_*.json")):
        cat = int(p.stem.split("_")[1])
        for a in rd(p):
            rows.append(
                {"source": "binance", "catalog": BINANCE_CATALOGS[cat], "ms": int(a["releaseDate"]), "title": a["title"], "id": a["code"]}
            )
    for p in sorted((cache / "ann").glob("bybit_delistings_*.json")):
        for a in rd(p):
            ms = int(a.get("publishTime") or a.get("dateTimestamp"))
            rows.append({"source": "bybit", "catalog": "delisting", "ms": ms, "title": a["title"], "id": a["url"], "url": a["url"]})
    for p in sorted((cache / "ann").glob("okx_delistings_*.json")):
        for a in rd(p):
            rows.append({"source": "okx", "catalog": "delisting", "ms": int(a["pTime"]), "title": a["title"], "id": a["url"]})
    return rows


def body_path(cache: Path, row: dict[str, Any]) -> Path:
    return cache / "bodies" / f"{row['source']}_{hashlib.sha1(row['id'].encode()).hexdigest()[:16]}.json"


async def stage_bodies(cache: Path) -> None:
    bn = Http("binance_cms", "https://www.binance.com", rate=0.5, burst=1, timeout=30, retries=2)
    by = Http("bybit_web", "https://announcements.bybit.com", rate=0.5, burst=1, timeout=30, retries=2)
    try:
        need = [r for r in load_ann(cache) if r["catalog"] == "delisting" and START <= r["ms"] < END]
        need = [r for r in need if classify_delisting(r["source"], r["title"])[0] == "needs_body"]
        print(f"bodies to fetch: {len(need)}", flush=True)
        for r in need:
            p = body_path(cache, r)
            if p.exists():
                continue
            try:
                if r["source"] == "binance":
                    f = await bn.get_json("/bapi/composite/v1/public/cms/article/detail/query", params={"articleCode": r["id"]})
                    text = json.dumps((f.data.get("data") or {}).get("body") or "")
                else:
                    path = r["url"].split("announcements.bybit.com", 1)[-1]
                    f = await by.get_bytes(path)
                    text = f.data.decode("utf-8", "replace")
            except ProviderError as e:
                print(f"  body failed {r['source']} {r['title'][:60]}: {e.reason}", flush=True)
                continue
            # keep only what we parse: perp symbols and spot pairs X/USDT (body text is untrusted)
            wr(p, {"perps": perp_symbols(text), "pairs": list(dict.fromkeys(re.findall(r"\b([A-Z0-9]{2,15})/USDT\b", text)))})
            print(
                f"  {r['source']} {ymdhm(r['ms'])} {r['title'][:60]} -> {len(rd(p)['perps'])} perps, {len(rd(p)['pairs'])} pairs",
                flush=True,
            )
    finally:
        await bn.aclose()
        await by.aclose()


# ================================================================== events


def build_events(cache: Path) -> dict[str, list[Event]]:
    rows = [r for r in load_ann(cache) if START <= r["ms"] < END]
    ev: dict[str, list[Event]] = defaultdict(list)
    for r in rows:
        if r["catalog"] == "new_listing":
            for t in classify_listing(r["title"]):
                ev["listing"].append(Event(t, r["ms"], "listing", r["source"], f"{r['source']}:{r['id']}"))
            continue
        kind, ticks = classify_delisting(r["source"], r["title"])
        if kind == "needs_body":
            p = body_path(cache, r)
            if not p.exists():
                continue
            b = rd(p)
            if "Perpetual" in r["title"] or "Futures" in r["title"]:
                kind, ticks = "perp_delist", b["perps"]
            else:
                kind, ticks = "spot_delist", b["pairs"]
        if kind == "spot_delist":
            for t in ticks:
                ev["spot_delist"].append(Event(t, r["ms"], "spot_delist", r["source"], f"{r['source']}:{r['id']}"))
        elif kind == "perp_delist":
            for s in ticks:
                ev["perp_delist"].append(Event(perp_base(s)[0], r["ms"], "perp_delist", r["source"], f"{r['source']}:{r['id']}", note=s))
    ev["hack"] = hack_events(
        rd(cache / "ann" / "llama_hacks.json"),
        rd(cache / "ann" / "llama_protocols.json"),
        min_amount=HACK_MIN_USD,
        start_ms=START,
        end_ms=END,
    )
    ev["shutdown"] = list(CURATED)
    return ev


def stage_events(cache: Path) -> None:
    ev = build_events(cache)
    store: dict[str, Any] = {}
    for cat, lst in ev.items():
        kept, dropped = dedupe(lst)
        store[cat] = [asdict(e) for e in kept]
        by_src = Counter(e.source for e in kept)
        by_year = Counter(ymd(e.ms)[:4] for e in kept)
        print(
            f"{cat}: {len(lst)} raw, {len(kept)} after 72h dedupe ({len(dropped)} dropped); clusters {len({e.cluster for e in kept})}; "
            f"by source {dict(by_src)}; by year {dict(sorted(by_year.items()))}",
            flush=True,
        )
    wr(cache / "events.json", store)


def load_events(cache: Path) -> dict[str, list[Event]]:
    return {k: [Event(**d) for d in v] for k, v in rd(cache / "events.json").items()}


# ================================================================== prices


class Fetcher:
    """Per-file JSON cache; ``null`` marks a file the source does not have (404 / unknown symbol)."""

    def __init__(self, workers: int = 10) -> None:
        self.sem = asyncio.Semaphore(workers)
        self.errors: Counter[str] = Counter()
        self.fetched = 0

    async def get(self, path: Path, go: Callable[[], Awaitable[Any]]) -> Any:
        if await asyncio.to_thread(path.exists):
            return rd(path)
        async with self.sem:
            data = None
            for k in range(4):
                try:
                    data = await go()
                    break
                except ProviderError as e:
                    if e.reason == "data_unavailable" or any(
                        x in str(e.detail) for x in ("Invalid symbol", "Symbol Is Invalid", "Not supported symbols")
                    ):
                        data = None
                        break
                    if k == 3:
                        self.errors[e.reason] += 1
                        if self.errors[e.reason] <= 5:
                            print(f"  error {path.name}: {e.reason}: {str(e.detail)[:160]}", flush=True)
                        return None  # transient: not cached, a rerun retries
                    await asyncio.sleep(3 * (k + 1))
            wr(path, data)
            self.fetched += 1
            return data


class Px:
    """Fetch helpers (per-file cache) for one venue at a time."""

    def __init__(self, cache: Path, rt: Any, fx: Fetcher) -> None:
        self.cache, self.rt, self.fx = cache, rt, fx
        self.bybit = Http("bybit_mkt", "https://api.bybit.com", rate=8, burst=8, timeout=30, retries=2)
        self.fapi = Http("binance_fapi", "https://fapi.binance.com", rate=4, burst=4, timeout=30, retries=2)
        self.okx = Http("okx_mkt", "https://www.okx.com", rate=4, burst=4, timeout=30, retries=2)
        self.um: set[str] = set()
        self.spot: set[str] = set()

    async def aclose(self) -> None:
        await self.bybit.aclose()
        await self.fapi.aclose()
        await self.okx.aclose()

    async def symbols(self) -> None:
        p, q = self.cache / "um_symbols.json", self.cache / "spot_symbols.json"
        if not p.exists():
            wr(p, (await self.rt.binance_vision.um_symbols()).data)
        if not q.exists():
            wr(q, (await self.rt.binance_vision.spot_symbols()).data)
        self.um, self.spot = set(rd(p)), set(rd(q))

    # ---- 1m days
    async def m1(self, venue: str, sym: str, day_ms: int) -> list | None:
        p = self.cache / "px" / venue / sym / "1m" / f"{ymd(day_ms)}.json"
        if venue == "binance":
            return await self.fx.get(p, lambda: self._vision_day(sym, day_ms, spot=False))
        if venue == "binance_spot":
            return await self.fx.get(p, lambda: self._vision_day(sym, day_ms, spot=True))
        return await self.fx.get(p, lambda: self._bybit_kline(sym, "1", day_ms, day_ms + DAY_MS))

    async def m1v(self, venue: str, sym: str, day_ms: int) -> list | None:
        """1m day with the volume column (``[open_ms, o, h, l, c, volume]``), for the "perp live at t0" rule.
        Kept apart from the 5-column ``1m`` files of the first run, which stay as they were."""
        p = self.cache / "px" / venue / sym / "1mv" / f"{ymd(day_ms)}.json"
        if venue == "binance":
            return await self.fx.get(p, lambda: self._vision_day(sym, day_ms, spot=False, cols=6))
        return await self.fx.get(p, lambda: self._bybit_kline(sym, "1", day_ms, day_ms + DAY_MS, vol=True))

    async def _vision_day(self, sym: str, day_ms: int, *, spot: bool, cols: int = 5) -> list:
        f = await (self.rt.binance_vision.spot_klines_daily if spot else self.rt.binance_vision.um_klines_daily)(sym, "1m", ymd(day_ms))
        return [r[:cols] for r in f.data]

    async def _bybit_kline(self, sym: str, interval: str, a: int, z: int, *, vol: bool = False, category: str = "linear") -> list | None:
        step = MIN_MS if interval == "1" else HOUR_MS
        rows: list = []
        s = a
        while s < z:
            e = min(z, s + 1000 * step) - 1
            f = await self.bybit.get_json(
                "/v5/market/kline", params={"category": category, "symbol": sym, "interval": interval, "start": s, "end": e, "limit": 1000}
            )
            if f.data.get("retCode") not in (0, None):
                raise ProviderError("bybit_mkt", "data_unavailable", str(f.data.get("retMsg")))
            lst = (f.data.get("result") or {}).get("list") or []
            rows += [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])] + ([float(r[5])] if vol else []) for r in lst]
            s = e + 1
        return sorted(rows) if rows else None

    # ---- spot on the announcing exchange (ticker-collision check; amendment after the first report)
    async def ann_spot(self, source: str, token: str, t0: int) -> list | None:
        """1m spot bars with volume over [t0 - 6 h, t0) on the exchange that made the announcement (Bybit or OKX)."""
        a = t0 - 6 * HOUR_MS
        if source == "bybit":
            p = self.cache / "px" / "bybit_spot" / f"{token}USDT" / f"{t0 // MIN_MS}.json"
            return await self.fx.get(p, lambda: self._bybit_kline(f"{token}USDT", "1", a, t0, vol=True, category="spot"))
        if source == "okx":
            p = self.cache / "px" / "okx_spot" / f"{token}-USDT" / f"{t0 // MIN_MS}.json"
            return await self.fx.get(p, lambda: self._okx_candles(f"{token}-USDT", a, t0))
        return None

    async def _okx_candles(self, inst: str, a: int, z: int) -> list | None:
        rows: list = []
        after = z
        for _ in range(5):
            f = await self.okx.get_json(
                "/api/v5/market/history-candles", params={"instId": inst, "bar": "1m", "after": after, "limit": 100}
            )
            if str(f.data.get("code")) != "0":
                raise ProviderError("okx_mkt", "data_unavailable", str(f.data.get("msg")))
            lst = f.data.get("data") or []
            rows += [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in lst]
            if len(lst) < 100 or min(int(r[0]) for r in lst) <= a:
                break
            after = min(int(r[0]) for r in lst)
        rows = [r for r in rows if a <= r[0] < z]
        return sorted(rows) if rows else None

    # ---- 1h months (placebo)
    async def h1(self, venue: str, sym: str, y: int, m: int) -> list | None:
        p = self.cache / "px" / venue / sym / "1h" / f"{y:04d}-{m:02d}.json"
        a = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
        z = int(datetime(y + m // 12, m % 12 + 1, 1, tzinfo=UTC).timestamp() * 1000)
        if venue == "binance":

            async def go() -> list | None:
                if (y, m) >= (2026, 10):
                    rows: list = []
                    for d in range(a, min(z, END + 5 * DAY_MS), DAY_MS):
                        try:
                            rows += [r[:5] for r in (await self.rt.binance_vision.um_klines_daily(sym, "1h", ymd(d))).data]
                        except ProviderError:
                            pass
                    return rows or None
                return [r[:5] for r in (await self.rt.binance_vision.um_klines_monthly(sym, "1h", y, m)).data]

            return await self.fx.get(p, go)
        return await self.fx.get(p, lambda: self._bybit_kline(sym, "60", a, z))

    # ---- funding months
    async def funding(self, venue: str, sym: str, y: int, m: int) -> list | None:
        p = self.cache / "fund" / venue / sym / f"{y:04d}-{m:02d}.json"
        a = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
        z = int(datetime(y + m // 12, m % 12 + 1, 1, tzinfo=UTC).timestamp() * 1000)
        if venue == "binance":

            async def go() -> list | None:
                if (y, m) < (2026, 10):
                    return [[int(r[0]), r[2]] for r in (await self.rt.binance_vision.um_funding_monthly(sym, y, m)).data]
                f = await self.fapi.get_json(
                    "/fapi/v1/fundingRate", params={"symbol": sym, "startTime": a, "endTime": z - 1, "limit": 1000}
                )
                return [[int(r["fundingTime"]), float(r["fundingRate"])] for r in f.data] or None

            return await self.fx.get(p, go)

        async def go_b() -> list | None:
            rows: list = []
            e = z - 1
            for _ in range(10):
                f = await self.bybit.get_json(
                    "/v5/market/funding/history", params={"category": "linear", "symbol": sym, "startTime": a, "endTime": e, "limit": 200}
                )
                lst = (f.data.get("result") or {}).get("list") or []
                rows += [[int(r["fundingRateTimestamp"]), float(r["fundingRate"])] for r in lst]
                if len(lst) < 200:
                    break
                e = min(int(r["fundingRateTimestamp"]) for r in lst) - 1
            return sorted(rows) or None

        return await self.fx.get(p, go_b)

    # ---- which perp prices this event (amended rule: the perp must be live at t0, see perp_live)
    async def resolve_live(self, token: str, t0: int) -> tuple[list | None, list[str]]:
        """([venue, symbol, multiplier] of the first perp live at t0 -- Binance USD-M, else Bybit linear -- or None,
        candidates that had bars at t0 but were not live: settled / delisted). Uses only bars closed by t0."""
        d = t0 // DAY_MS * DAY_MS
        dead: list[str] = []
        for venue in ("binance", "bybit"):
            for mult in MULTIPLIERS:
                sym = f"{mult if mult > 1 else ''}{token}USDT"
                if venue == "binance" and sym not in self.um:
                    continue
                if venue == "bybit":
                    p = self.cache / "px" / "bybit_probe" / f"{sym}_{t0 // HOUR_MS}.json"
                    probe = await self.fx.get(p, lambda sym=sym: self._bybit_kline(sym, "1", t0 - 40 * MIN_MS, t0))
                    if not probe:
                        continue
                rows = (await self.m1v(venue, sym, d - DAY_MS) or []) + (await self.m1v(venue, sym, d) or [])
                if not rows:
                    continue
                b = Bars.from_rows(rows)
                if price_at(b, t0) is None:
                    continue
                if perp_live(b, t0):
                    return [venue, sym, mult], dead
                dead.append(f"{venue}:{sym}")
        return None, dead

    async def resolve(self, token: str, t0: int) -> tuple[str, str, int] | None:
        """First-run rule (registered wording, kept for the comparison): a 1m bar not older than 30 min at t0."""
        """(venue, symbol, multiplier) of the first perp that traded at t0: Binance USD-M, else Bybit linear."""
        day = t0 // DAY_MS * DAY_MS
        for mult in MULTIPLIERS:
            sym = f"{mult if mult > 1 else ''}{token}USDT"
            if sym in self.um:
                rows = await self.m1("binance", sym, day)
                if rows and price_at(Bars.from_rows(rows), t0) is not None:
                    return "binance", sym, mult
                if rows and t0 - day < 31 * MIN_MS:  # t0 right after midnight: the previous day holds the bar
                    prev = await self.m1("binance", sym, day - DAY_MS)
                    if prev and price_at(Bars.from_rows(prev + rows), t0) is not None:
                        return "binance", sym, mult
        for mult in MULTIPLIERS:
            sym = f"{mult if mult > 1 else ''}{token}USDT"
            p = self.cache / "px" / "bybit_probe" / f"{sym}_{t0 // HOUR_MS}.json"
            probe = await self.fx.get(p, lambda sym=sym: self._bybit_kline(sym, "1", t0 - 40 * MIN_MS, t0))
            if probe:
                return "bybit", sym, mult
        return None


def months_between(a: int, z: int) -> list[tuple[int, int]]:
    da, dz = datetime.fromtimestamp(a / 1000, UTC), datetime.fromtimestamp(z / 1000, UTC)
    y, m = da.year, da.month
    res = []
    while (y, m) <= (dz.year, dz.month):
        res.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return res


def all_events(cache: Path) -> list[Event]:
    ev = load_events(cache)
    return [e for cat in ("spot_delist", "perp_delist", "hack", "shutdown", "listing") for e in ev.get(cat, [])]


async def stage_prices(cache: Path, sample: int) -> None:
    _, rt = build_server()
    fx = Fetcher()
    px = Px(cache, rt, fx)
    try:
        await px.symbols()
        evs = all_events(cache)
        if sample:
            evs = evs[:: max(1, len(evs) // sample)][:sample]
        res_p = cache / RESOLVE_FILE  # amended rule (perp live at t0); the first run's resolve.json stays untouched
        resolved: dict[str, Any] = rd(res_p) if res_p.exists() else {}
        t_start = time.time()
        print(f"events: {len(evs)}; um symbols {len(px.um)}, spot symbols {len(px.spot)}", flush=True)

        async def one(e: Event) -> None:
            key = f"{e.token}@{e.ms}"
            t0 = e.ms if e.precise else e.ms + DAY_MS
            if key not in resolved:
                r, dead = await px.resolve_live(e.token, t0)
                resolved[key] = {"r": r, "dead": dead}
            r = resolved[key]["r"]
            if not r:
                return
            venue, sym, _ = r
            if e.source in ("bybit", "okx"):
                await px.ann_spot(e.source, e.token, t0)
            d0 = e.ms // DAY_MS * DAY_MS
            jobs = [px.m1(venue, sym, d0 + k * DAY_MS) for k in range(-1, 6)]
            jobs += [px.m1("binance", "BTCUSDT", d0 + k * DAY_MS) for k in range(0, 6)]
            spot = f"{e.token}USDT"
            if spot in px.spot:
                jobs.append(px.m1("binance_spot", spot, d0 if e.precise else d0 + DAY_MS))
            for y, m in months_between(e.ms - 122 * DAY_MS, e.ms + 6 * DAY_MS):
                jobs.append(px.funding(venue, sym, y, m))
            for y, m in months_between(e.ms - 122 * DAY_MS, e.ms - 3 * DAY_MS):
                jobs.append(px.h1(venue, sym, y, m))
            await asyncio.gather(*jobs)

        done = 0
        batch = 25
        for i in range(0, len(evs), batch):
            await asyncio.gather(*[one(e) for e in evs[i : i + batch]])
            done += len(evs[i : i + batch])
            wr(res_p, resolved)
            el = time.time() - t_start
            print(
                f"  prices {done}/{len(evs)} in {el:.0f}s (eta {el / done * (len(evs) - done):.0f}s); files fetched {fx.fetched}; "
                f"errors {dict(fx.errors)}; rss={rss_mb():.0f}MB",
                flush=True,
            )
        n_ok = sum(1 for e in evs if (resolved.get(f"{e.token}@{e.ms}") or {}).get("r"))
        print(f"prices done: {n_ok}/{len(evs)} events have a perp; {time.time() - t_start:.0f}s", flush=True)
    finally:
        await px.aclose()
        await rt.aclose()


# ================================================================== measurement (from the cache only)


def cached(cache: Path, venue: str, sym: str, iv: str, name: str) -> list | None:
    p = cache / "px" / venue / sym / iv / f"{name}.json"
    return rd(p) if p.exists() else None


def fund_rows(cache: Path, venue: str, sym: str, a: int, z: int) -> tuple[list[tuple[int, float]] | None, bool]:
    """(funding rows over the months of [a, z] or None if none at all, partial: some month file is missing)."""
    rows: list[tuple[int, float]] = []
    missing = False
    for y, m in months_between(a, z):
        p = cache / "fund" / venue / sym / f"{y:04d}-{m:02d}.json"
        d = rd(p) if p.exists() else None
        if d is None:
            missing = True
            continue
        rows += [(int(r[0]), float(r[1])) for r in d]
    return (None if missing and not rows else rows), (missing and bool(rows))


def ann_spot_price(cache: Path, source: str, token: str, t0: int) -> float | None:
    """Last traded (volume > 0) 1m close on the announcing exchange's spot within 6 h before t0, from the cache."""
    if source == "bybit":
        p = cache / "px" / "bybit_spot" / f"{token}USDT" / f"{t0 // MIN_MS}.json"
    elif source == "okx":
        p = cache / "px" / "okx_spot" / f"{token}-USDT" / f"{t0 // MIN_MS}.json"
    else:
        return None
    rows = rd(p) if p.exists() else None
    rows = [r for r in rows or [] if len(r) > 5 and r[5] > 0]
    return price_at(Bars.from_rows(rows), t0, max_stale_ms=6 * HOUR_MS) if rows else None


def measure(cache: Path, e: Event, r: list | None, blocked: list[int] | None = None) -> dict[str, Any] | None:
    """All registered numbers of one event, from the cached files only."""
    if not r:
        return None
    venue, sym, mult = r
    d0 = e.ms // DAY_MS * DAY_MS
    rows: list = []
    for k in range(-1, 6):
        rows += cached(cache, venue, sym, "1m", ymd(d0 + k * DAY_MS)) or []
    if not rows:
        return None
    b = Bars.from_rows(rows)
    t0 = e.ms if e.precise else d0 + DAY_MS  # hacks: only the day is known; t0 = end of that UTC day
    rec: dict[str, Any] = {
        "token": e.token,
        "ms": e.ms,
        "t0": t0,
        "category": e.category,
        "source": e.source,
        "cluster": e.cluster,
        "venue": venue,
        "sym": sym,
        "precise": e.precise,
        "note": e.note,
    }
    p0 = price_at(b, t0)
    rec["p0"] = p0
    if p0 is None:
        return rec
    # collision check against Binance spot (same day)
    sp = cached(cache, "binance_spot", f"{e.token}USDT", "1m", ymd(t0 // DAY_MS * DAY_MS))
    if sp:
        ps = price_at(Bars.from_rows(sp), t0, max_stale_ms=6 * HOUR_MS)
        rec["spot_ratio"] = (p0 / mult) / ps if ps else None
    pa = ann_spot_price(cache, e.source, e.token, t0)  # amendment: spot on the announcing exchange (Bybit / OKX)
    rec["spot_ratio_ann"] = (p0 / mult) / pa if pa else None
    rec["react"] = {k: reaction(b, t0, dt) for k, dt in REACT.items()}
    if not e.precise:  # hack day move: from the day's start to t0
        p_day = price_at(b, d0)
        rec["react"]["day"] = (p0 / p_day - 1.0) if p_day else None
    rec["pre60"] = reaction(b, t0 - HOUR_MS, HOUR_MS)
    mm = max_move(b, t0, t0 + 72 * HOUR_MS, p0)
    rec["max72"] = mm
    fund, partial = fund_rows(cache, venue, sym, t0 - DAY_MS, t0 + 5 * DAY_MS)
    rec["funding_missing"] = fund is None
    rec["funding_partial"] = partial
    fund = fund or []
    btc_rows: list = []
    for k in range(0, 6):
        btc_rows += cached(cache, "binance", "BTCUSDT", "1m", ymd(d0 + k * DAY_MS)) or []
    btc = Bars.from_rows(btc_rows) if btc_rows else None
    sign = -1.0 if e.category == "listing" else 1.0  # H2: long; negative categories: short
    trades: dict[str, Any] = {}
    for d in DELAYS:
        for hh in HOLDS:
            ent, ex = t0 + d * MIN_MS, t0 + d * MIN_MS + hh * HOUR_MS
            variants = [("base", COST, None, "level"), ("stress", COST_STRESS, None, "level"), ("stop", COST, STOP, "level")]
            # exploratory (after the first report): worst-bar stop fill, other stop levels
            variants += [("stopx", COST, STOP, "extreme"), ("stop15", COST, 0.15, "level"), ("stop35", COST, 0.35, "level")]
            variants += [("stop15x", COST, 0.15, "extreme"), ("stop35x", COST, 0.35, "extreme")]
            for tag, cs, stop, fill in variants:
                tr = short_trade(b, ent, ex, cost_side=cs, funding=fund, stop=stop, sign=sign, stop_fill=fill)
                trades[f"d{d}_h{hh}_{tag}"] = asdict(tr) if tr else None
            if btc is not None:
                tb = short_trade(btc, ent, ex, cost_side=0.0, sign=sign)
                trades[f"d{d}_h{hh}_btc"] = tb.gross if tb else None
    rec["trades"] = trades
    # placebo: same perp, random hours in [t0 - 120 d, t0 - 7 d], 1h bars, same costs and funding series
    h_rows: list = []
    for y, m in months_between(t0 - 122 * DAY_MS, t0 - 3 * DAY_MS):
        h_rows += cached(cache, venue, sym, "1h", f"{y:04d}-{m:02d}") or []
    pl: dict[str, list[float]] = {f"h{hh}": [] for hh in HOLDS}
    if h_rows:
        hb = Bars.from_rows(h_rows, step=HOUR_MS)
        pf = fund_rows(cache, venue, sym, t0 - 122 * DAY_MS, t0)[0] or []
        for ts in placebo_times(f"{e.token}@{e.ms}", t0, N_PLACEBO, blocked=[b for b in (blocked or []) if b != e.ms]):
            for hh in HOLDS:
                tr = short_trade(hb, ts, ts + hh * HOUR_MS, cost_side=COST, funding=pf, sign=sign, max_stale_ms=2 * HOUR_MS)
                if tr and not tr.truncated:
                    pl[f"h{hh}"].append(tr.net)
    rec["placebo"] = pl
    return rec


def measure_all(cache: Path) -> list[dict[str, Any]]:
    p = cache / f"measured{OUT_SUFFIX}.json"
    resolved = rd(cache / RESOLVE_FILE)
    old = rd(cache / "resolve.json") if (cache / "resolve.json").exists() else {}
    recs = []
    evs = all_events(cache)
    by_tok: dict[str, list[int]] = defaultdict(list)
    for e in evs:
        by_tok[e.token].append(e.ms)
    for e in evs:
        key = f"{e.token}@{e.ms}"
        res = resolved.get(key) or {}
        rec = measure(cache, e, res.get("r"), by_tok[e.token])
        rec = rec or {
            "token": e.token,
            "ms": e.ms,
            "category": e.category,
            "source": e.source,
            "cluster": e.cluster,
            "p0": None,
            "no_perp": rec is None,
        }
        rec["dead"] = res.get("dead") or []
        rec["old_r"] = old.get(key)
        recs.append(rec)
    wr(p, recs)
    return recs


def _bad(sr: float | None) -> bool:
    return sr is not None and not (0.8 <= sr <= 1.25)


def spot_check(rec: dict[str, Any]) -> str:
    """Ticker-collision status: ``fail`` if the announcing exchange's spot (Bybit / OKX; amendment) or Binance spot
    (registered) disagrees with the perp; ``ann`` checked on the announcing exchange (Binance spot for Binance
    announcements); ``binance`` checked only against Binance spot; ``none`` unchecked."""
    sa, sb = rec.get("spot_ratio_ann"), rec.get("spot_ratio")
    if _bad(sa) or _bad(sb):
        return "fail"
    if sa is not None or (rec.get("source") == "binance" and sb is not None):
        return "ann"
    return "binance" if sb is not None else "none"


def usable(rec: dict[str, Any], key: str, *, checked: tuple[str, ...] = ("ann", "binance", "none")) -> float | None:
    """Net result of one trade variant; None when the event has no live perp price, fails the collision check (or its
    check status is not in ``checked``) or the trade is missing."""
    if rec.get("p0") is None:
        return None
    if spot_check(rec) not in checked:
        return None
    tr = (rec.get("trades") or {}).get(key)
    return None if tr is None else tr["net"]


PRIMARY = "d15_h24_base"


def pool(
    recs: list[dict[str, Any]],
    cats: tuple[str, ...],
    key: str = PRIMARY,
    sources: tuple[str, ...] | None = None,
    checked: tuple[str, ...] = ("ann", "binance", "none"),
) -> tuple[list[float], list[str], list[dict]]:
    vals, cl, rr = [], [], []
    for r in recs:
        if r["category"] not in cats or (sources and r["source"] not in sources):
            continue
        v = usable(r, key, checked=checked)
        if v is None:
            continue
        vals.append(v)
        cl.append(r["cluster"])
        rr.append(r)
    return vals, cl, rr


def stage_stage0(cache: Path) -> None:
    recs = measure_all(cache)
    lines: list[str] = []
    for cat in ("spot_delist", "perp_delist", "hack", "listing"):
        n_all = sum(1 for r in recs if r["category"] == cat)
        n_perp = sum(1 for r in recs if r["category"] == cat and not r.get("no_perp"))
        n_px = sum(1 for r in recs if r["category"] == cat and r.get("p0") is not None)
        n_dead = sum(1 for r in recs if r["category"] == cat and r.get("old_r") and r.get("no_perp"))
        chk = Counter(spot_check(r) for r in recs if r["category"] == cat and r.get("p0") is not None)
        vals, cl, _ = pool(recs, (cat,))
        sd, k = cluster_sd(vals, cl) if len(vals) > 1 else (float("nan"), 0)
        sd_ev = statistics.stdev(vals) if len(vals) > 1 else float("nan")
        out(
            lines,
            f"{cat}: events {n_all}; with a live Binance/Bybit perp at t0 {n_perp} (first-run rule had a perp but none live now: "
            f"{n_dead}); priced {n_px}; spot check {dict(chk)}; "
            f"usable {len(vals)} in {k} clusters; SD(event) {sd_ev:.3f}; SD(cluster means) {sd:.3f}; MDE {mde_clusters(sd, k):.3f}",
        )
    vals, cl, _ = pool(recs, ("spot_delist",))
    sd, k = cluster_sd(vals, cl)
    m = mde_clusters(sd, k)
    out(lines, f"GATE (primary pool spot_delist, {PRIMARY}): MDE {m:.3f} vs {GATE_MDE:.2f} -> {'PASS' if m <= GATE_MDE else 'FAIL'}")
    (cache / f"stage0{OUT_SUFFIX}.txt").write_text("\n".join(lines) + "\n")


def stage_sanity(cache: Path) -> None:
    """Data checks before the report: the owner's examples against the library numbers, collisions, coverage."""
    recs = rd(cache / f"measured{OUT_SUFFIX}.json")
    for r in recs:
        if r["category"] == "shutdown":
            rc = {k: (round(v * 100, 1) if v is not None else None) for k, v in (r.get("react") or {}).items()}
            print(
                f"curated {r['token']} {ymdhm(r['ms'])}: venue {r.get('venue')} {r.get('sym')} p0 {r.get('p0')} react% {rc} spot_ratio {r.get('spot_ratio')}"
            )
    for r in recs:
        if r.get("p0") is not None and spot_check(r) == "fail":
            sa, sb = r.get("spot_ratio_ann"), r.get("spot_ratio")
            print(
                f"collision {r['category']} {r['token']} {r['sym']} via {r['source']} {ymd(r['ms'])}: perp/spot "
                f"binance {sb if sb is None else round(sb, 3)} announcer {sa if sa is None else round(sa, 3)}"
            )
    # perp-liveness amendment: what changed against the first run's resolution
    changed = Counter()
    for r in recs:
        if r["category"] == "shutdown":
            continue
        new = [r.get("venue"), r.get("sym")] if not r.get("no_perp") else None
        old = r.get("old_r")[:2] if r.get("old_r") else None
        if old and not new:
            changed[(r["category"], "dropped (not live)")] += 1
        elif old and new and old != new:
            changed[(r["category"], "moved to another live perp")] += 1
        elif new and not old:
            changed[(r["category"], "new")] += 1
    print("liveness vs first run:", dict(changed))
    ex = [r for r in recs if r["category"] == "spot_delist" and r.get("old_r") and r.get("no_perp")]
    print("spot_delist dropped as not live (first 20):", [f"{r['token']} {ymd(r['ms'])} {r['source']} {r['dead']}" for r in ex[:20]])
    ok = [r for r in recs if r.get("p0") is not None]
    print("priced by venue:", dict(Counter((r["category"], r.get("venue")) for r in ok)))
    print("funding missing:", dict(Counter(r["category"] for r in ok if r.get("funding_missing"))))
    print("funding partial:", dict(Counter(r["category"] for r in ok if r.get("funding_partial"))))
    print("spot check status:", dict(Counter((r["category"], spot_check(r)) for r in ok)))
    both = [r for r in ok if r.get("spot_ratio") is not None and r.get("spot_ratio_ann") is not None]
    print(
        f"both Binance and announcer spot: {len(both)}; disagree on collision: {sum(_bad(r['spot_ratio']) != _bad(r['spot_ratio_ann']) for r in both)}"
    )
    print("no spot check:", dict(Counter(r["category"] for r in ok if spot_check(r) == "none")))
    ratios = [r["spot_ratio"] for r in ok if r.get("spot_ratio") is not None]
    print(f"perp/spot ratio quantiles (5/50/95%): {np.quantile(ratios, [0.05, 0.5, 0.95]).round(4).tolist()}")
    gaps = [r for r in ok if (r.get("trades") or {}).get(PRIMARY) is None]
    print(f"priced but no primary trade (gap after t0): {len(gaps)} {[r['token'] for r in gaps][:15]}")


def fmt(x: float | None, p: int = 1) -> str:
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:+.{p}f}%"


def ci_row(vals: list[float], cl: list[str]) -> str:
    if len(vals) < 2:
        return f"n {len(vals)}"
    m, lo, hi = cluster_bootstrap(vals, cl)
    med = statistics.median(vals)
    win = sum(v > 0 for v in vals) / len(vals)
    return f"n {len(vals)} / {len(set(cl))} cl; mean {fmt(m)} [{fmt(lo)}; {fmt(hi)}]; median {fmt(med)}; win {win * 100:.0f}%"


def stage_report(cache: Path) -> None:
    recs = measure_all(cache)
    L: list[str] = []
    out(L, f"# news events report {datetime.now(UTC):%Y-%m-%d %H:%M} UTC; period {ymd(START)} .. {ymd(END)} (t0 < END)")
    out(L, f"costs: fee {FEE:.4%}/side, slippage {SLIP:.4%}/side (stress x3); stop variant {STOP:.0%}; placebo {N_PLACEBO}/event")
    # ---- coverage
    out(L, "\n## coverage")
    for cat in ("spot_delist", "perp_delist", "hack", "shutdown", "listing"):
        rr = [r for r in recs if r["category"] == cat]
        venues = Counter((r.get("venue") or "none") for r in rr if r.get("p0") is not None)
        coll = [r["token"] for r in rr if r.get("p0") is not None and spot_check(r) == "fail"]
        trunc = sum(1 for r in rr if (r.get("trades") or {}).get(PRIMARY) and r["trades"][PRIMARY]["truncated"])
        fm = sum(1 for r in rr if r.get("funding_missing"))
        fp = sum(1 for r in rr if r.get("funding_partial"))
        dead = sum(1 for r in rr if r.get("old_r") and r.get("no_perp"))
        out(
            L,
            f"{cat}: {len(rr)} events; no live perp {sum(1 for r in rr if r.get('no_perp'))} (of them settled/dead at t0 but counted "
            f"in the first run: {dead}); priced {sum(venues.values())} {dict(venues)}; "
            f"collisions {len(coll)} {coll[:12]}; truncated (primary) {trunc}; funding missing {fm}, partial {fp}",
        )
    # ---- reactions
    out(L, "\n## reactions from t0 (mean / median), price change of the token")
    for cat in ("spot_delist", "perp_delist", "hack", "listing"):
        rr = [r for r in recs if r["category"] == cat and usable(r, PRIMARY) is not None]
        cells = []
        for k in ["pre60", *REACT] + (["day"] if cat == "hack" else []):
            xs = [r["pre60"] if k == "pre60" else r["react"].get(k) for r in rr]
            xs = [x for x in xs if x is not None]
            cells.append(f"{k} {fmt(statistics.fmean(xs)) if xs else '—'}/{fmt(statistics.median(xs)) if xs else '—'}")
        out(L, f"{cat} (n {len(rr)}): " + "; ".join(cells))
    for e in [r for r in recs if r["category"] == "shutdown"]:
        rc = e.get("react") or {}
        out(
            L,
            f"curated {e['token']} {ymdhm(e['ms'])} venue {e.get('venue')} {e.get('sym')}: "
            + "; ".join(f"{k} {fmt(v)}" for k, v in rc.items())
            + f"; short d15 h24 net {fmt(usable(e, PRIMARY))}",
        )
    # ---- H1
    out(L, "\n## H1: short entered t0+15m, exit +24h, base costs + funding (net, on notional)")
    vals, cl, rr = pool(recs, ("spot_delist",))
    out(L, f"PRIMARY spot_delist: {ci_row(vals, cl)}")
    pl = [statistics.fmean(r["placebo"]["h24"]) if r.get("placebo") and r["placebo"]["h24"] else None for r in rr]
    idx = [i for i, p in enumerate(pl) if p is not None]
    ev_v, pl_v, cl_v = [vals[i] for i in idx], [pl[i] for i in idx], [cl[i] for i in idx]
    if len(ev_v) > 1:
        out(L, f"  placebo (same perps, {N_PLACEBO} random hours before t0): {ci_row(pl_v, cl_v)}")
        md, lo, hi = paired_diff_bootstrap(ev_v, pl_v, cl_v)
        out(L, f"  event - placebo: {fmt(md)} [{fmt(lo)}; {fmt(hi)}] (n {len(ev_v)})")
    sd, k = cluster_sd(vals, cl)
    out(L, f"  MDE {fmt(mde_clusters(sd, k))} (clusters {k})")
    m, lo, _ = cluster_bootstrap(vals, cl)
    med = statistics.median(vals) if vals else float("nan")
    verdict = "CONFIRMED" if (m >= THRESH and lo > 0) else ("EDGE<THRESH" if lo > 0 else "NOT CONFIRMED")
    out(L, f"  H1 criterion (mean >= {THRESH:.0%} and CI low > 0): {verdict}; median {fmt(med)}")
    # ---- grid
    out(L, "\n## grid (net mean [CI]) per category; delays x holds x cost/stop")
    for cat in ("spot_delist", "perp_delist", "hack"):
        for d in DELAYS:
            for hh in HOLDS:
                cells = []
                for tag in ("base", "stress", "stop"):
                    v, c, _ = pool(recs, (cat,), f"d{d}_h{hh}_{tag}")
                    if len(v) > 1:
                        mm, l2, h2 = cluster_bootstrap(v, c)
                        cells.append(f"{tag} {fmt(mm)} [{fmt(l2)}; {fmt(h2)}]")
                out(L, f"{cat} d{d} h{hh} (n {len(pool(recs, (cat,), f'd{d}_h{hh}_base')[0])}): " + "; ".join(cells))
    # ---- components of the primary trade
    out(L, "\n## primary trade components (spot_delist)")
    trs = [r["trades"][PRIMARY] for r in rr]
    out(
        L,
        f"gross {fmt(statistics.fmean(t['gross'] for t in trs))}; funding {fmt(statistics.fmean(t['funding'] for t in trs), 2)}; "
        f"cost {fmt(statistics.fmean(t['cost'] for t in trs), 2)}; MAE median {fmt(statistics.median(t['mae'] for t in trs))}, "
        f"p90 {fmt(float(np.quantile([t['mae'] for t in trs], 0.9)))}; share MAE >= 25% {sum(t['mae'] >= STOP for t in trs) / len(trs) * 100:.0f}%",
    )
    hv = [r["trades"][PRIMARY]["net"] - (r["trades"].get("d15_h24_btc") or 0.0) for r in rr if r["trades"].get("d15_h24_btc") is not None]
    hc = [r["cluster"] for r in rr if r["trades"].get("d15_h24_btc") is not None]
    out(L, f"BTC-hedged (short token, long BTC): {ci_row(hv, hc)}")
    # ---- robustness
    out(L, "\n## robustness (spot_delist, primary trade)")
    for src in ("binance", "bybit", "okx"):
        v, c, _ = pool(recs, ("spot_delist",), sources=(src,))
        out(L, f"source {src}: {ci_row(v, c)}")
    # amendment (critic's review): ticker-collision sensitivity
    v, c, _ = pool(recs, ("spot_delist",), checked=("ann", "binance"))
    out(L, f"only spot-checked events (announcer or Binance spot): {ci_row(v, c)}")
    v, c, _ = pool(recs, ("spot_delist",), checked=("ann",))
    out(L, f"only events checked on the announcing exchange's spot: {ci_row(v, c)}")
    unc = [r for r in rr if spot_check(r) == "none"]
    out(L, "unchecked events: " + ", ".join(f"{r['token']} {r['source']} {ymd(r['ms'])} {fmt(r['trades'][PRIMARY]['net'])}" for r in unc))
    v, c, _ = pool([r for r in recs if not r.get("funding_partial")], ("spot_delist",))
    out(L, f"without partial-funding events: {ci_row(v, c)}")
    for lab, a, z in (
        ("2023-2024", START, int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)),
        ("2025-2026", int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000), END),
    ):
        v = [x for x, r in zip(vals, rr, strict=True) if a <= r["ms"] < z]
        c = [x for x, r in zip(cl, rr, strict=True) if a <= r["ms"] < z]
        out(L, f"period {lab}: {ci_row(v, c)}")
    order = sorted(range(len(vals)), key=lambda i: -vals[i])
    for drop in (3, 5, 10):
        keep = sorted(order[drop:])
        out(L, f"without top {drop} events: {ci_row([vals[i] for i in keep], [cl[i] for i in keep])}")
    cm: dict[str, list[float]] = defaultdict(list)
    for v, c in zip(vals, cl, strict=True):
        cm[c].append(v)
    top_cl = sorted(cm, key=lambda c: -sum(cm[c]))[:3]
    keep = [i for i in range(len(vals)) if cl[i] not in top_cl]
    out(L, f"without top 3 announcements: {ci_row([vals[i] for i in keep], [cl[i] for i in keep])}")
    out(L, "top 10 events: " + ", ".join(f"{rr[i]['token']} {ymd(rr[i]['ms'])} {fmt(vals[i])}" for i in order[:10]))
    out(L, "bottom 5 events: " + ", ".join(f"{rr[i]['token']} {ymd(rr[i]['ms'])} {fmt(vals[i])}" for i in order[-5:]))
    # ---- heterogeneity across announcements and persistence (5a)
    out(L, "\n## heterogeneity (5a)")
    groups = [(c, v) for c, v in cm.items() if len(v) >= 2]
    means = [statistics.fmean(v) for _, v in groups]
    ses = [statistics.stdev(v) / len(v) ** 0.5 for _, v in groups]
    q, df, i2, pq = cochran_q(means, ses)
    out(L, f"announcements with >= 2 tokens: {len(groups)}; Cochran Q {q:.1f} df {df} I2 {i2:.2f} p {pq:.3f}")
    tok = Counter(r["token"] for r in recs if r["category"] in ("spot_delist", "perp_delist", "hack") and r.get("p0") is not None)
    out(L, f"tokens with >= 2 negative events: {sum(1 for v in tok.values() if v >= 2)} of {len(tok)} (per-asset persistence not testable)")
    for src in ("binance", "bybit", "okx"):
        sv = [vals[i] for i in range(len(vals)) if rr[i]["source"] == src]
        out(L, f"  by source {src}: n {len(sv)} mean {fmt(statistics.fmean(sv)) if sv else '—'}")
    # ---- H2
    # ---- exploratory: chosen after the first report (labelled; no effect on the verdict)
    out(L, "\n## EXPLORATORY (after seeing results): stop fill and level, Binance-only subgroup")
    for cat in ("spot_delist", "perp_delist"):
        for src in (None, ("binance",)):
            for d, hh in ((15, 24), (15, 72)):
                cells = []
                for tag in ("base", "stop", "stopx", "stop15", "stop15x", "stop35", "stop35x"):
                    v, c, _ = pool(recs, (cat,), f"d{d}_h{hh}_{tag}", sources=src)
                    if len(v) > 1:
                        mm, l2, h2 = cluster_bootstrap(v, c)
                        cells.append(f"{tag} {fmt(mm)} [{fmt(l2)}; {fmt(h2)}]")
                n = len(pool(recs, (cat,), f"d{d}_h{hh}_base", sources=src)[0])
                out(L, f"{cat} {'binance' if src else 'all'} d{d} h{hh} (n {n}): " + "; ".join(cells))
    v, c, rb = pool(recs, ("spot_delist",), sources=("binance",))
    for lab, a, z in (
        ("2023-2024", START, int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)),
        ("2025-2026", int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000), END),
    ):
        vv = [x for x, r in zip(v, rb, strict=True) if a <= r["ms"] < z]
        cc = [x for x, r in zip(c, rb, strict=True) if a <= r["ms"] < z]
        out(L, f"spot_delist binance base {lab}: {ci_row(vv, cc)}")
        vs, cs_, rs = pool(recs, ("spot_delist",), "d15_h24_stopx", sources=("binance",))
        vv = [x for x, r in zip(vs, rs, strict=True) if a <= r["ms"] < z]
        cc = [x for x, r in zip(cs_, rs, strict=True) if a <= r["ms"] < z]
        out(L, f"spot_delist binance stopx {lab}: {ci_row(vv, cc)}")
    for src in (None, ("binance",)):
        vs, cs_, _ = pool(recs, ("spot_delist",), "d15_h24_stopx", sources=src)
        order_s = sorted(range(len(vs)), key=lambda i: -vs[i])
        for drop in (5, 10):
            keep = sorted(order_s[drop:])
            out(
                L,
                f"spot_delist {'binance' if src else 'all'} stopx d15 h24 without top {drop}: {ci_row([vs[i] for i in keep], [cs_[i] for i in keep])}",
            )
    rx = [r for r in rr if r["trades"]["d15_h24_stop"] and r["trades"]["d15_h24_stop"]["stopped"]]
    out(
        L,
        f"primary stopped at 25%: {len(rx)} of {len(rr)}; worst-bar fill adds {fmt(statistics.fmean(r['trades']['d15_h24_stop']['net'] - r['trades']['d15_h24_stopx']['net'] for r in rx)) if rx else '—'} loss per stopped trade",
    )
    out(L, "\n## H2 (descriptive): Binance spot listing of a token that already had a perp; LONG entered t0+15m")
    for hh in HOLDS:
        v, c, rl = pool(recs, ("listing",), f"d15_h{hh}_base")
        out(L, f"listing long h{hh}: {ci_row(v, c)}")
        plv = [statistics.fmean(r["placebo"][f"h{hh}"]) for r in rl if r.get("placebo") and r["placebo"][f"h{hh}"]]
        if plv:
            out(L, f"  placebo long h{hh}: mean {fmt(statistics.fmean(plv))}")
    (cache / f"report{OUT_SUFFIX}.txt").write_text("\n".join(L) + "\n")
    print(f"\nrss={rss_mb():.0f}MB; written {cache / f'report{OUT_SUFFIX}.txt'}", flush=True)


# ================================================================== prereg


def stage_prereg() -> None:
    text = CARD.read_text()
    i = text.index("## Попередня реєстрація")
    sec = text[i:].encode()
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec).hexdigest()} bytes={len(sec)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["ann", "bodies", "events", "prices", "stage0", "sanity", "report", "prereg"])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "tradedesk-agents" / "news_events")
    ap.add_argument("--sample", type=int, default=0)
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "ann":
        asyncio.run(stage_ann(a.cache_dir))
    elif a.stage == "bodies":
        asyncio.run(stage_bodies(a.cache_dir))
    elif a.stage == "events":
        stage_events(a.cache_dir)
    elif a.stage == "prices":
        asyncio.run(stage_prices(a.cache_dir, a.sample))
    elif a.stage == "stage0":
        stage_stage0(a.cache_dir)
    elif a.stage == "sanity":
        stage_sanity(a.cache_dir)
    elif a.stage == "report":
        stage_report(a.cache_dir)
    else:
        stage_prereg()


if __name__ == "__main__":
    main()
