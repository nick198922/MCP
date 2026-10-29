"""Binance perp launch of an existing token (IDEA-20261029-binance-perp-launch-existing-token), step 0: inventory and
power gate WITHOUT event returns.

Idea card: docs/ideas/IDEA-20261029-binance-perp-launch-existing-token.md (section "Тріаж критика": the gate is
computed at step 0 without event returns; the SD comes from placebo windows of the same tokens on random dates).

Sources (free, no keys): Binance CMS catalog 48 (titles) and article bodies (launch time L), Binance USD-M
exchangeInfo (``underlyingType``), Binance spot first daily bar, Bybit v5 instruments-info (current contracts =
what ccxt lists) and v5 kline (also answers for delisted symbols), Hyperliquid ``meta`` (with delisted) and daily
candles. Announcement text is untrusted: only matched against fixed patterns.

Stages (each caches per file under --cache-dir; a rerun resumes):
    ann       catalog-48 pages (newest first) down to the start of the period
    bodies    article bodies of the "Futures Will Launch USDⓈ-M ... Perpetual" titles -> flattened text
    parse     launches, L per symbol, 20-example parser check file, counts by year, lead "announcement -> L"
    venues    Bybit / Hyperliquid first bar per token, Binance spot first bar, exchangeInfo -> venues.json
    placebo   Bybit 1h months around the eligible events; placebo hedged-short outcomes on random dates of the same
              month (never within 4 days of an announcement); collision check (price level ratio Bybit / Binance)
    stage0    power gate: n by year, shares, SD of placebo month means, MDE (no event returns are read)

    uv run python scripts/verify_perp_launch_existing.py <stage> --cache-dir ~/.cache/tradedesk-agents/perp_launch_existing --out <file>

Forward check (new announcements after step 0; --end = tomorrow's UTC date, the same for every stage):
    ann --refresh-ann 3 --end D, then bodies / parse / venues / stage0 with --end D; new eligible rows are those
    with an announcement >= 2026-10-29 in venues.json (stage0 prints the counts by year).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import resource
import statistics
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.news_events import STABLES, cluster_sd, mde_clusters, perp_base, perp_symbols
from tradedesk.analytics.perp_launch import (
    DAY_MS,
    HOUR_MS,
    beta,
    body_text,
    classify_launch_title,
    collision_ratio,
    eligible,
    hedged_short,
    launch_from_title,
    launch_times,
    live_at,
    placebo_hours,
    venue_symbols,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.server import build_server

START = int(datetime(2023, 1, 1, tzinfo=UTC).timestamp() * 1000)
STEP0_END = int(datetime(2026, 10, 29, tzinfo=UTC).timestamp() * 1000)
END = STEP0_END  # --end overrides (forward check)
MIN_AGE = 14 * DAY_MS
COST = 0.0025  # card: taker 0.055 % x 2 + slippage 0.07 % x 2 per round trip
N_PLACEBO = 20  # placebo draws per event (each draw = one random date in the event's month)
GATE_N = 120
GATE_MDE = 0.03


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def ymd(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def ymdhm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(p)


class Out:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, s: str = "") -> None:
        print(s, flush=True)
        self.lines.append(s)


# ================================================================== announcements


async def stage_ann(cache: Path, out: Out, refresh: int = 0) -> None:
    """Catalog pages, newest first, cached per page. ``refresh`` > 0 (forward check): also download the newest
    ``refresh`` pages into separate dated files (``binance_48_fwd<date>_<page>.json``), so the step-0 pages stay as
    they were and nothing between them is lost when new articles shift the paging; ``load_titles`` merges all files
    by article code. ``refresh`` x 50 must exceed the number of catalog-48 articles since the previous check."""
    bn = Http("binance_cms", "https://www.binance.com", rate=0.5, burst=1, timeout=30, retries=2)
    try:
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        for page in range(1, refresh + 1):
            p = cache / "ann" / f"binance_48_fwd{stamp}_{page:03d}.json"
            if not p.exists():
                f = await bn.get_json(
                    "/bapi/composite/v1/public/cms/article/list/query",
                    params={"type": 1, "catalogId": 48, "pageNo": page, "pageSize": 50},
                )
                wr(p, f.data["data"]["catalogs"][0]["articles"])
            arts = rd(p)
            newest = max((int(a["releaseDate"]) for a in arts), default=0)
            out(f"  refresh page {page}: {len(arts)} articles, newest {ymd(newest) if newest else '-'}")
        page = 1
        while True:
            p = cache / "ann" / f"binance_48_{page:03d}.json"
            if not p.exists():
                f = await bn.get_json(
                    "/bapi/composite/v1/public/cms/article/list/query",
                    params={"type": 1, "catalogId": 48, "pageNo": page, "pageSize": 50},
                )
                wr(p, f.data["data"]["catalogs"][0]["articles"])
            arts = rd(p)
            oldest = min((int(a["releaseDate"]) for a in arts), default=0)
            out(f"  page {page}: {len(arts)} articles, oldest {ymd(oldest) if oldest else '-'} rss={rss_mb():.0f}MB")
            if not arts or len(arts) < 50 or oldest < START - 30 * DAY_MS:
                break
            page += 1
    finally:
        await bn.aclose()


def load_titles(cache: Path) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for p in sorted((cache / "ann").glob("binance_48_*.json")):
        for a in rd(p):
            seen[a["code"]] = {"code": a["code"], "ms": int(a["releaseDate"]), "title": a["title"]}
    return sorted(seen.values(), key=lambda r: r["ms"])


def launch_titles(cache: Path) -> list[dict[str, Any]]:
    rows = []
    for r in load_titles(cache):
        if START <= r["ms"] < END:
            kind, syms = classify_launch_title(r["title"])
            if kind != "none":
                rows.append({**r, "kind": kind, "syms": syms})
    return rows


async def stage_bodies(cache: Path, out: Out) -> None:
    bn = Http("binance_cms", "https://www.binance.com", rate=0.3, burst=1, timeout=30, retries=2)
    try:
        need = [r for r in launch_titles(cache) if r["kind"] in ("launch", "multiple")]
        have = sum((cache / "bodies" / f"{r['code']}.json").exists() for r in need)
        out(f"launch titles: {len(need)}; bodies cached: {have}")
        t0 = time.time()
        for i, r in enumerate(need):
            p = cache / "bodies" / f"{r['code']}.json"
            if p.exists():
                continue
            try:
                f = await bn.get_json("/bapi/composite/v1/public/cms/article/detail/query", params={"articleCode": r["code"]})
            except ProviderError as e:
                out(f"  body failed {r['code']}: {e.reason}")
                if e.reason == "rate_limited":
                    await asyncio.sleep(60)
                continue
            body = (f.data.get("data") or {}).get("body") or ""
            wr(p, {"text": body_text(body)[:12000]})
            if i % 25 == 0:
                out(f"  bodies {i + 1}/{len(need)} {time.time() - t0:.0f}s rss={rss_mb():.0f}MB")
        out(f"bodies done in {time.time() - t0:.0f}s")
    finally:
        await bn.aclose()


# ================================================================== parse


def build_launches(cache: Path) -> tuple[list[dict[str, Any]], Counter[str]]:
    kinds: Counter[str] = Counter()
    res = []
    for r in launch_titles(cache):
        kinds[r["kind"]] += 1
        if r["kind"] not in ("launch", "multiple"):
            continue
        p = cache / "bodies" / f"{r['code']}.json"
        text = rd(p)["text"] if p.exists() else ""
        if r["kind"] == "multiple":
            r = {**r, "syms": [s for s in perp_symbols(text) if s.endswith("USDT")]}
        lt = launch_times(text, r["syms"], r["ms"]) if text else dict.fromkeys(r["syms"])
        tradfi_body = bool(re.search(r"Pre-IPO|Equity Perpetual|USDT-Priced|stock token", text, re.I))
        for s in r["syms"]:
            tok, mult = launch_from_title(s)
            res.append(
                {
                    "symbol": s,
                    "token": tok,
                    "mult": mult,
                    "ann": r["ms"],
                    "L": lt.get(s),
                    "code": r["code"],
                    "n_syms": len(r["syms"]),
                    "title": r["title"],
                    "tradfi_body": tradfi_body,
                }
            )
    return res, kinds


def stage_parse(cache: Path, out: Out) -> None:
    rows, kinds = build_launches(cache)
    out(f"catalog-48 'Binance Futures Will Launch' titles {ymd(START)}..{ymd(END)}: {dict(kinds)}")
    for r in launch_titles(cache):
        if r["kind"] in ("other_futures", "multiple", "usdc"):
            out(f"    [{r['kind']}] {ymd(r['ms'])} {r['title'][:120]}")
    out(f"USDT perp launches (symbol rows): {len(rows)}; announcements: {len({r['code'] for r in rows})}")
    no_body = sum(1 for r in rows if not (cache / "bodies" / f"{r['code']}.json").exists())
    out(f"  without body: {no_body}; L parsed: {sum(r['L'] is not None for r in rows)}")
    stab = [r for r in rows if r["token"] in STABLES]
    out(f"  stablecoin / wrapped tokens: {[r['symbol'] for r in stab]}")
    by_year = Counter(ymd(r["ann"])[:4] for r in rows)
    out(f"  by year: {dict(sorted(by_year.items()))}")
    leads = sorted((r["L"] - r["ann"]) / HOUR_MS for r in rows if r["L"] is not None)
    if leads:
        q = statistics.quantiles(leads, n=20)
        out(
            f"  lead announcement -> L, hours: min {leads[0]:.2f}, p5 {q[0]:.2f}, p25 {q[4]:.2f}, median {statistics.median(leads):.2f}, "
            f"p75 {q[14]:.2f}, p95 {q[18]:.2f}, max {leads[-1]:.2f}"
        )
        bins = Counter(
            "<0" if x < 0 else "0-1h" if x < 1 else "1-2h" if x < 2 else "2-4h" if x < 4 else "4-24h" if x < 24 else ">=24h" for x in leads
        )
        out(f"  lead bins: {dict(bins)}")
    # 20-example parser check (seeded sample; half from multi-symbol announcements when available)
    rng = random.Random(20261029)
    multi = [r for r in rows if r["n_syms"] > 1]
    single = [r for r in rows if r["n_syms"] == 1]
    sample = rng.sample(multi, min(8, len(multi))) + rng.sample(single, min(20 - min(8, len(multi)), len(single)))
    chk = []
    for r in sorted(sample, key=lambda r: r["ann"]):
        text = rd(cache / "bodies" / f"{r['code']}.json")["text"] if (cache / "bodies" / f"{r['code']}.json").exists() else ""
        i = text.find(r["symbol"])
        chk.append(
            {
                "symbol": r["symbol"],
                "ann": ymdhm(r["ann"]),
                "L": ymdhm(r["L"]) if r["L"] else None,
                "n_syms": r["n_syms"],
                "snippet": text[max(0, i - 80) : i + 260] if i >= 0 else text[:300],
            }
        )
    wr(cache / "parser_check.json", chk)
    for c in chk:
        out(f"    check {c['symbol']:<16} ann {c['ann']}  L {c['L']}  syms in ann {c['n_syms']}")
    out(f"  Pre-IPO / equity wording in body (excluded later as TradFi): {[r['symbol'] for r in rows if r['tradfi_body']]}")
    out(f"  parser check sample: {len(chk)} rows -> {cache / 'parser_check.json'}")
    wr(cache / "launches.json", rows)


# ================================================================== venues


class Venues:
    def __init__(self, cache: Path, rt: Any) -> None:
        self.cache, self.rt = cache, rt
        self.bybit = Http("bybit_mkt", "https://api.bybit.com", rate=8, burst=8, timeout=30, retries=2)
        self.bspot = Http("binance_api", "https://api.binance.com", rate=5, burst=5, timeout=30, retries=2)
        self.sem = asyncio.Semaphore(8)

    async def aclose(self) -> None:
        await self.bybit.aclose()
        await self.bspot.aclose()

    async def cached(self, p: Path, go: Any) -> Any:
        if await asyncio.to_thread(p.exists):
            return rd(p)
        async with self.sem:
            try:
                data = await go()
            except ProviderError as e:
                if e.reason == "data_unavailable" or any(x in str(e.detail) for x in ("Invalid symbol", "Not supported", "-1121")):
                    data = None
                else:
                    print(f"  error {p.name}: {e.reason} {str(e.detail)[:120]}", flush=True)
                    return "ERR"
            wr(p, data)
            return data

    async def bybit_kline(self, sym: str, interval: str, a: int, z: int, limit: int = 1000) -> list | None:
        f = await self.bybit.get_json(
            "/v5/market/kline", params={"category": "linear", "symbol": sym, "interval": interval, "start": a, "end": z, "limit": limit}
        )
        if f.data.get("retCode") not in (0, None):
            raise ProviderError("bybit_mkt", "data_unavailable", str(f.data.get("retMsg")))
        lst = (f.data.get("result") or {}).get("list") or []
        return sorted([int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in lst) or None

    async def bybit_first(self, sym: str, ann: int) -> tuple[int, int, int] | None:
        """(first daily bar open, last daily bar open, daily bars with volume > 0 in the 14 days before the
        announcement) of ``sym`` on Bybit within 1000 days before the announcement (None: no bar)."""
        p = self.cache / "bybit_first" / f"{sym}_{ann // DAY_MS}.json"
        rows = await self.cached(p, lambda: self.bybit_kline(sym, "D", ann - 1000 * DAY_MS, ann))
        if rows == "ERR" or not rows:
            return None
        n14 = sum(1 for x in rows if ann - MIN_AGE <= x[0] < ann and x[5] > 0)
        return int(rows[0][0]), int(rows[-1][0]), n14

    async def instruments(self) -> dict[str, int]:
        p = self.cache / "bybit_instruments.json"
        if not p.exists():
            rows, cursor = [], ""
            for _ in range(20):
                f = await self.bybit.get_json(
                    "/v5/market/instruments-info", params={"category": "linear", "limit": 1000, **({"cursor": cursor} if cursor else {})}
                )
                res = f.data.get("result") or {}
                rows += [
                    {"symbol": x["symbol"], "launch": int(x.get("launchTime") or 0), "status": x.get("status")}
                    for x in res.get("list") or []
                ]
                cursor = res.get("nextPageCursor") or ""
                if not cursor:
                    break
            wr(p, rows)
        return {x["symbol"]: x["launch"] for x in rd(p)}

    async def hl_meta(self) -> dict[str, bool]:
        p = self.cache / "hl_meta.json"
        if not p.exists():
            f = await self.rt.hyperliquid._info({"type": "meta"}, ttl=0)
            wr(p, [{"name": u["name"], "delisted": bool(u.get("isDelisted"))} for u in f.data["universe"]])
        return {x["name"]: x["delisted"] for x in rd(p)}

    async def hl_first(self, coin: str) -> int | None:
        p = self.cache / "hl_first" / f"{coin}.json"

        async def go() -> list | None:
            f = await self.rt.hyperliquid._info(
                {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d", "startTime": 0, "endTime": END}}, ttl=0
            )
            return [int(c["t"]) for c in f.data[:1]] or None

        rows = await self.cached(p, go)
        return int(rows[0]) if rows and rows != "ERR" else None

    async def spot_first(self, token: str) -> int | None:
        p = self.cache / "spot_first" / f"{token}USDT.json"

        async def go() -> list | None:
            f = await self.bspot.get_json("/api/v3/klines", params={"symbol": f"{token}USDT", "interval": "1d", "startTime": 0, "limit": 1})
            return [int(r[0]) for r in f.data] or None

        rows = await self.cached(p, go)
        return int(rows[0]) if rows and rows != "ERR" else None


async def stage_venues(cache: Path, out: Out, sample: int) -> None:
    _, rt = build_server()
    v = Venues(cache, rt)
    try:
        rows = rd(cache / "launches.json")
        if sample:
            rows = rows[:: max(1, len(rows) // sample)][:sample]
        inst = await v.instruments()
        hl = await v.hl_meta()
        p = cache / "um_exchange_info.json"
        # forward: a symbol announced after step 0 that today's cached exchangeInfo lacks -> fetch it again (only then,
        # so a step-0 rerun keeps the snapshot it used)
        fresh = p.exists() and any(r["ann"] >= STEP0_END and r["symbol"] not in rd(p) for r in rows)
        if not p.exists() or fresh:
            ei = (await rt.binance_futures.exchange_info()).data
            wr(
                p,
                {
                    s["symbol"]: {"u": s.get("underlyingType"), "onboard": s.get("onboardDate"), "ct": s.get("contractType")}
                    for s in ei["symbols"]
                },
            )
        um = rd(p)
        out(f"bybit current linear instruments {len(inst)}; HL universe {len(hl)} ({sum(hl.values())} delisted); UM exchangeInfo {len(um)}")
        t0 = time.time()
        res: list[dict[str, Any]] = []

        async def one(r: dict[str, Any]) -> None:
            tok, ann = r["token"], r["ann"]
            cands = venue_symbols(tok)
            by_first, by_sym, by_last, by_n14 = None, None, None, 0
            found = []
            for s in cands["bybit"]:
                f = await v.bybit_first(s, ann)
                if f is not None:
                    found.append((not live_at(f[1], f[2], ann), f[0], s, f))
            if found:  # a contract live at the announcement first, then the oldest
                _, _, by_sym, f = min(found)
                by_first, by_last, by_n14 = f
            hl_first, hl_sym = None, None
            for c in cands["hyperliquid"]:
                if c in hl:
                    f = await v.hl_first(c)
                    if f is not None and (hl_first is None or f < hl_first):
                        hl_first, hl_sym = f, c
            sp = await v.spot_first(tok)
            info = um.get(r["symbol"]) or {}
            res.append(
                {
                    **{k: r[k] for k in ("symbol", "token", "mult", "ann", "L", "code", "n_syms", "tradfi_body")},
                    "bybit_sym": by_sym,
                    "bybit_first": by_first,
                    "bybit_last": by_last,
                    "bybit_n14": by_n14,
                    "bybit_current": by_sym in inst if by_sym else False,
                    "bybit_launch_api": inst.get(by_sym) if by_sym else None,
                    "hl_sym": hl_sym,
                    "hl_first": hl_first,
                    "hl_delisted": hl.get(hl_sym) if hl_sym else None,
                    "spot_first": sp,
                    "underlying": info.get("u"),
                    "um_onboard": info.get("onboard"),
                }
            )

        for i in range(0, len(rows), 20):
            await asyncio.gather(*[one(r) for r in rows[i : i + 20]])
            el = time.time() - t0
            done = min(i + 20, len(rows))
            out(f"  venues {done}/{len(rows)} {el:.0f}s (eta {el / done * (len(rows) - done):.0f}s) rss={rss_mb():.0f}MB")
        res.sort(key=lambda x: (x["ann"], x["symbol"]))
        wr(cache / ("venues.json" if not sample else "venues_sample.json"), res)
    finally:
        await v.aclose()
        await rt.aclose()


def classify_rows(cache: Path) -> list[dict[str, Any]]:
    rows = rd(cache / "venues.json")
    for r in rows:
        ann = r["ann"]
        onb = r.get("um_onboard")
        same_contract = onb is not None and abs(int(onb) - (r["L"] or ann)) <= 30 * DAY_MS  # exchangeInfo is today's
        r["tradfi"] = (same_contract and (r.get("underlying") or "COIN") != "COIN") or bool(r.get("tradfi_body"))
        r["stable"] = r["token"] in STABLES
        r["by_ok_raw"] = eligible(r["bybit_first"], ann, min_age_ms=MIN_AGE)
        r["by_ok"] = r["by_ok_raw"] and live_at(r.get("bybit_last"), r.get("bybit_n14", 0), ann)
        r["hl_ok"] = eligible(r["hl_first"], ann, min_age_ms=MIN_AGE)
        r["spot_before"] = r["spot_first"] is not None and r["spot_first"] < ann
        r["in_period"] = START <= ann < END
        r["usable"] = r["in_period"] and not r["tradfi"] and not r["stable"] and r["L"] is not None and (r["by_ok"] or r["hl_ok"])
    return rows


# ================================================================== placebo (no event windows)


def month_bounds(ms: int, shift: int = 0) -> tuple[int, int]:
    d = datetime.fromtimestamp(ms / 1000, UTC)
    y, m = d.year, d.month + shift
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    a = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    return a, int(datetime(y2, m2, 1, tzinfo=UTC).timestamp() * 1000)


async def bybit_month_1h(v: Venues, sym: str, a: int, z: int) -> list | None:
    p = v.cache / "px" / "bybit" / sym / "1h" / f"{ymd(a)[:7]}.json"

    async def go() -> list | None:
        rows: list = []
        s = a
        while s < z:
            e = min(z, s + 1000 * HOUR_MS) - 1
            rows += await v.bybit_kline(sym, "60", s, e) or []
            s = e + 1
        return sorted(rows) or None

    r = await v.cached(p, go)
    return None if r == "ERR" else r


async def stage_placebo(cache: Path, out: Out) -> None:
    _, rt = build_server()
    v = Venues(cache, rt)
    try:
        rows = [r for r in classify_rows(cache) if r["usable"] and r["by_ok"]]
        anns_by_tok: dict[str, list[int]] = defaultdict(list)
        for r in rd(cache / "venues.json"):
            anns_by_tok[r["token"]].append(r["ann"])
        out(f"eligible events with a Bybit perp >= 14 d: {len(rows)}")
        t0 = time.time()
        res = []
        for i, r in enumerate(rows):
            ma, mz = month_bounds(r["ann"])
            months = [month_bounds(r["ann"], k) for k in (-3, -2, -1, 0)]
            alt: list = []
            btc: list = []
            for a, z in months:
                alt += await bybit_month_1h(v, r["bybit_sym"], a, z) or []
                btc += await bybit_month_1h(v, "BTCUSDT", a, z) or []
            # next month's first days: a placebo hour late in the month needs +24 h; L can be up to ~3 days after a
            # month-end announcement (collision check). Bars after the placebo windows are never read by them.
            na, _ = month_bounds(r["ann"], 1)
            if na <= END:
                alt += [x for x in (await bybit_month_1h(v, r["bybit_sym"], *month_bounds(r["ann"], 1)) or []) if x[0] < na + 4 * DAY_MS]
                btc += [x for x in (await bybit_month_1h(v, "BTCUSDT", *month_bounds(r["ann"], 1)) or []) if x[0] < na + 4 * DAY_MS]
            alt.sort()
            btc.sort()
            lo = max(ma, (r["bybit_first"] or 0) + MIN_AGE)
            hrs = placebo_hours(f"{r['symbol']}@{r['ann']}", lo, min(mz, END - DAY_MS), N_PLACEBO, blocked=anns_by_tok[r["token"]])
            vals = []
            for t in hrs:
                b = beta(alt, btc, t)
                y = hedged_short(alt, btc, t, b, cost=COST) if b is not None else None
                vals.append(y)
            # collision check (fixed 2026-10-29 after the critic's review): the first Binance USD-M trade (open of
            # the first 1h bar at/after the hour of L) vs the close of the last Bybit 1h bar before that hour, both
            # per token. The first version compared with Bybit 1 h before the ANNOUNCEMENT, which mixes the
            # "announcement -> L" pump with a different asset (MOODENG). Only the ratio is kept.
            ratio = None
            lday = r["L"] // DAY_MS * DAY_MS
            pq = cache / "px" / "binance_um_1h" / r["symbol"] / f"{ymd(lday)}.json"

            async def go_um(sym: str = r["symbol"], d: int = lday) -> list | None:
                return [[int(x[0]), float(x[1])] for x in (await rt.binance_vision.um_klines_daily(sym, "1h", ymd(d))).data] or None

            um1 = await v.cached(pq, go_um)
            if um1 and um1 != "ERR":
                ratio = collision_ratio(alt, um1, r["L"], bybit_mult=perp_base(r["bybit_sym"])[1], binance_mult=r["mult"])
            res.append({"symbol": r["symbol"], "ann": r["ann"], "month": ymd(r["ann"])[:7], "hours": hrs, "vals": vals, "ratio": ratio})
            if i % 20 == 0:
                out(f"  placebo {i + 1}/{len(rows)} {time.time() - t0:.0f}s rss={rss_mb():.0f}MB")
        wr(cache / "placebo.json", res)
        out(f"placebo done {time.time() - t0:.0f}s")
    finally:
        await v.aclose()
        await rt.aclose()


# ================================================================== stage 0 summary


def stage0(cache: Path, out: Out) -> None:
    rows = classify_rows(cache)
    out(f"symbol rows {len(rows)} (announcements {len({r['code'] for r in rows})})")
    tf = [r["symbol"] for r in rows if r["tradfi"]]
    out(f"  TradFi / non-COIN underlying (excluded): {len(tf)} {tf}")
    flip = [r["symbol"] for r in rows if (r.get("underlying") or "COIN") != "COIN" and not r["tradfi"]]
    out(f"  non-COIN in today's exchangeInfo but a different (older) contract, kept: {flip}")
    raw_only = [r["symbol"] for r in rows if r["by_ok_raw"] and not r["by_ok"] and not r["tradfi"] and not r["stable"]]
    out(f"  Bybit first bar >= 14 d but not live at the announcement (dropped by the sanity amendment): {len(raw_only)} {raw_only}")
    out(f"  stable/wrapped (excluded): {[r['symbol'] for r in rows if r['stable']]}")
    out(f"  L not parsed (excluded): {[r['symbol'] for r in rows if r['L'] is None]}")
    base = [r for r in rows if r["in_period"] and not r["tradfi"] and not r["stable"] and r["L"] is not None]
    out(f"  base universe (crypto, L parsed): {len(base)}")

    def yr(r: dict[str, Any]) -> str:
        return ymd(r["ann"])[:4]

    years = sorted({yr(r) for r in base})
    hdr = "| рік | база | Bybit ≥14д | лише HL ≥14д | Bybit або HL ≥14д | Bybit <14д | Bybit-перп є, але не в поточних (делістнуто) | без перпа Bybit/HL | спот Binance до анонсу |"
    out(hdr)
    out("|" + "---|" * 9)
    tot: Counter[str] = Counter()
    for y in [*years, "усього"]:
        sel = base if y == "усього" else [r for r in base if yr(r) == y]
        c = Counter()
        for r in sel:
            c["base"] += 1
            c["by"] += r["by_ok"]
            c["hl_only"] += r["hl_ok"] and not r["by_ok"]
            c["any"] += r["by_ok"] or r["hl_ok"]
            c["by_young"] += r["bybit_first"] is not None and not r["by_ok"]
            c["by_gone"] += r["by_ok"] and not r["bybit_current"]
            c["none"] += r["bybit_first"] is None and r["hl_first"] is None
            c["spot"] += r["spot_before"]
        if y == "усього":
            tot = c
        out(
            f"| {y} | {c['base']} | {c['by']} | {c['hl_only']} | {c['any']} | {c['by_young']} | {c['by_gone']} | {c['none']} | {c['spot']} |"
        )
    elig = [r for r in base if r["by_ok"] or r["hl_ok"]]
    out(f"eligible (Bybit or HL >= 14 d): {len(elig)} of {len(base)} ({len(elig) / max(1, len(base)):.0%}); Bybit: {tot['by']}")
    out(
        f"  of eligible-by-Bybit, contract no longer listed (absent from instruments-info = ccxt markets): {tot['by_gone']} "
        f"({tot['by_gone'] / max(1, tot['by']):.0%}); these would be lost with a ccxt-only universe"
    )
    out(f"  eligible with Binance spot before the announcement: {sum(r['spot_before'] for r in elig)} of {len(elig)}")
    leads = sorted((r["L"] - r["ann"]) / HOUR_MS for r in elig)
    if leads:
        q = statistics.quantiles(leads, n=4)
        out(
            f"  lead announcement -> L (eligible), h: min {leads[0]:.2f} p25 {q[0]:.2f} median {q[1]:.2f} p75 {q[2]:.2f} max {leads[-1]:.2f}"
        )
    # age check: Bybit API launchTime vs first daily bar (current contracts)
    diffs = [abs(r["bybit_launch_api"] - r["bybit_first"]) / DAY_MS for r in elig if r["bybit_launch_api"] and r["bybit_first"]]
    if diffs:
        out(
            f"  consistency: |Bybit launchTime - first daily bar| days: median {statistics.median(diffs):.2f}, max {max(diffs):.1f}, n {len(diffs)}"
        )
    ages = sorted((r["ann"] - r["bybit_first"]) / DAY_MS for r in elig if r["by_ok"])
    if ages:
        q = statistics.quantiles(ages, n=4)
        out(f"  Bybit perp age at announcement, days: p25 {q[0]:.0f} median {q[1]:.0f} p75 {q[2]:.0f}")
    # power gate from placebo windows
    pp = cache / "placebo.json"
    if not pp.exists():
        out("placebo.json missing: run the placebo stage")
        return
    pl = rd(pp)
    rat = [e["ratio"] for e in pl if e.get("ratio")]
    col = sorted(e["symbol"] for e in pl if e.get("ratio") and not 0.5 <= e["ratio"] <= 2.0)
    if rat:
        q = statistics.quantiles(rat, n=20)
        out(
            f"collision check (Binance UM first trade at L / Bybit close of the last 1h bar before L, per token): n {len(rat)} "
            f"of {len(pl)}; p5 {q[0]:.3f} median {statistics.median(rat):.3f} p95 {q[18]:.3f}; outside [0.5, 2]: {col}"
        )
    pl = [e for e in pl if e["symbol"] not in col]
    tot["by"] -= len(col)
    mdes, sds, ks, sd_ev = [], [], [], []
    for j in range(N_PLACEBO):
        vals, cl = [], []
        for e in pl:
            if j < len(e["vals"]) and e["vals"][j] is not None:
                vals.append(e["vals"][j])
                cl.append(e["month"])
        if len(vals) > 2:
            sd_c, k = cluster_sd(vals, cl)
            sds.append(sd_c)
            ks.append(k)
            mdes.append(mde_clusters(sd_c, k))
            sd_ev.append(statistics.stdev(vals))
    flat = [x for e in pl for x in e["vals"] if x is not None]
    n_ev = sum(1 for e in pl if any(x is not None for x in e["vals"]))
    out(f"placebo: events with outcomes {n_ev}/{len(pl)}, outcomes {len(flat)}")
    if mdes:
        out(
            f"  per-event SD of the placebo hedged 24 h short (incl. cost): median over draws {statistics.median(sd_ev):.4f}; pooled {statistics.stdev(flat):.4f}"
        )
        out(f"  SD of month means: median {statistics.median(sds):.4f}; months k median {statistics.median(ks):.0f}")
        out(f"  MDE (2.8 x SD_month_means / sqrt(k)): median {statistics.median(mdes):.4f}, range {min(mdes):.4f}..{max(mdes):.4f}")
        mde_ev = 2.8 * statistics.stdev(flat) / max(1, n_ev) ** 0.5
        out(f"  MDE without clustering (2.8 x SD / sqrt(n)): {mde_ev:.4f}")
        tails = sum(1 for x in flat if x < -0.25) / len(flat)
        out(f"  placebo tail: share of outcomes < -25 % (alt +25 % vs short): {tails:.3%}")
        n_by = tot["by"]
        g_n = n_by >= GATE_N
        g_m = statistics.median(mdes) <= GATE_MDE
        out(
            f"GATE: n (Bybit >= 14 d, usable) = {n_by} >= {GATE_N}: {'PASS' if g_n else 'FAIL'}; MDE {statistics.median(mdes):.4f} <= {GATE_MDE}: {'PASS' if g_m else 'FAIL'}"
        )
        out(f"      n (Bybit or HL) = {len(elig)}")
        n_raw = sum(1 for r in base if r["by_ok_raw"]) - sum(1 for r in base if r["by_ok_raw"] and r["symbol"] in col)
        out(f"      sensitivity: n without the 'live at the announcement' amendment (first bar >= 14 d only) = {n_raw}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["ann", "bodies", "parse", "venues", "placebo", "stage0"])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "tradedesk-agents" / "perp_launch_existing")
    ap.add_argument("--out", type=Path, default=None, help="also write the printed lines to this file")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument(
        "--end",
        default="2026-10-29",
        help="announcements before this UTC date (YYYY-MM-DD); the default reproduces step 0, a later date is the forward check",
    )
    ap.add_argument(
        "--refresh-ann",
        type=int,
        default=0,
        metavar="N",
        help="ann stage: re-download the newest N catalog pages even if cached (forward: new announcements)",
    )
    a = ap.parse_args()
    global END
    END = int(datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    out = Out()
    t = time.time()
    if a.stage == "ann":
        asyncio.run(stage_ann(cache, out, a.refresh_ann))
    elif a.stage == "bodies":
        asyncio.run(stage_bodies(cache, out))
    elif a.stage == "parse":
        stage_parse(cache, out)
    elif a.stage == "venues":
        asyncio.run(stage_venues(cache, out, a.sample))
    elif a.stage == "placebo":
        asyncio.run(stage_placebo(cache, out))
    else:
        stage0(cache, out)
    out(f"[{a.stage}] done in {time.time() - t:.0f}s, peak rss {rss_mb():.0f}MB")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text("\n".join(out.lines) + "\n")


if __name__ == "__main__":
    main()
