"""Holders-revenue collapse -> token underperformance over 60 days?

Idea card: docs/ideas/IDEA-20261015-holders-revenue-collapse-lag.md (section "Попередня реєстрація").

Stage 0 (before any price download): DefiLlama holders-revenue series for protocols whose token has a USDT perp on
Binance / Bybit / Hyperliquid, perp listing dates, the event count of the main variant and a two-snapshot stability
check. The power gate (>= 40 events) decides whether prices are fetched at all.

Network: exchanges only through the runtime providers (``rt.exchanges``, ``rt.hyperliquid._info``). DefiLlama has no
provider in the runtime, so this research script talks to it through ``core.http.Http`` (rate limit, retries) with
its own pacing; nothing in the MCP server uses it.

Usage:
    uv run python scripts/verify_holders_revenue.py stage0 --cache-dir ~/.cache/tradedesk-agents/holders_revenue
    uv run python scripts/verify_holders_revenue.py stage0 --cache-dir ... --snapshot b   # second download, compare
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import math
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.funding_spread import cluster_bootstrap_diff, normalize_asset
from tradedesk.analytics.holders_revenue import (
    DAY_S,
    DropRule,
    LegResult,
    RevenueEvent,
    beta,
    cluster_bootstrap_mean_ci,
    cluster_se,
    combine,
    drop_events,
    excluded_from_control_pool,
    funding_sum,
    hedged_return,
    max_adverse_short,
    nearest_controls,
    pair_net,
    rank_controls,
    short_vs_btc_net,
    simple_return,
    to_daily,
)
from tradedesk.core.http import Http
from tradedesk.server import build_server

HL, BIN, BYB = "hyperliquid", "binanceusdm", "bybit"
FIRST_SIGNAL = datetime(2021, 1, 1, tzinfo=UTC)
LAST_CLOSE = datetime(2026, 10, 14, tzinfo=UTC)  # last complete UTC day used
HORIZON = 60
MIN_LISTED = 30  # perp listed at least this many days before the entry day
MAIN = DropRule()  # 30/30, >= 50 %, prior >= 100k USD, lag 1


def day_of(dt: datetime) -> int:
    return int(dt.timestamp()) // DAY_S


def ymd(day: int | None) -> str:
    return "—" if day is None else datetime.fromtimestamp(day * DAY_S, UTC).strftime("%Y-%m-%d")


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


class Pacer:
    def __init__(self, per_min: float) -> None:
        self.per_min, self.next_ok, self.lock = per_min, 0.0, asyncio.Lock()

    async def take(self, weight: float = 1.0) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.next_ok > now:
                await asyncio.sleep(self.next_ok - now)
            self.next_ok = max(now, self.next_ok) + weight * 60.0 / self.per_min


async def retry(go: Any, what: str, tries: int = 5) -> Any:
    for k in range(tries):
        try:
            return await go()
        except Exception as e:  # noqa: BLE001
            if "data_unavailable" in str(e) or "HTTP 404" in str(e):
                raise
            wait = 15 * (k + 1)
            print(f"  retry {what} in {wait}s after {type(e).__name__}: {str(e)[:120]}", flush=True)
            await asyncio.sleep(wait)
    raise RuntimeError(f"giving up on {what}")


class Fetcher:
    def __init__(self, rt: Any, cache: Path) -> None:
        self.rt, self.cache = rt, cache
        self.llama = Http("defillama", "https://api.llama.fi", rate=1.0, burst=2, timeout=60, retries=2)
        self.coins = Http("defillama_coins", "https://coins.llama.fi", rate=1.0, burst=2, timeout=60, retries=2)
        self.llama_pace = Pacer(40.0)
        self.hl_pace = Pacer(1000.0)

    async def aclose(self) -> None:
        await self.llama.aclose()
        await self.coins.aclose()

    async def cached(self, name: str, go: Any) -> Any:
        p = self.cache / name
        if p.exists():
            return rd(p)
        data = await go()
        wr(p, data)
        return data

    async def ll(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self.llama_pace.take()
        return (await retry(lambda: self.llama.get_json(path, params=params or {}), f"llama {path}")).data

    async def hl(self, payload: dict[str, Any], weight: float) -> Any:
        await self.hl_pace.take(weight)
        return (await retry(lambda: self.rt.hyperliquid._info(payload, ttl=0), f"hl {payload.get('type')}")).data


# ----------------------------------------------------------------------------- stage 0


async def perp_universe(fx: Fetcher) -> dict[str, Any]:
    """Binance USDT-M crypto perps (what ccxt still lists, incl. settling), Bybit linear USDT perps, HL incl. delisted."""

    async def go() -> dict[str, Any]:
        meta = await fx.hl({"type": "metaAndAssetCtxs"}, 20)
        hl = {}
        for u, ctx in zip(meta[0]["universe"], meta[1], strict=False):
            hl[u["name"]] = {"delisted": bool(u.get("isDelisted")), "mark": float(ctx.get("markPx") or 0) or None}
        bm = (await fx.rt.exchanges.markets(BIN)).data or {}
        bn, tradfi = {}, []
        for sym, m in bm.items():
            info = (m or {}).get("info") or {}
            if not sym.endswith("/USDT:USDT") or info.get("contractType") != "PERPETUAL":
                continue
            if info.get("underlyingType") != "COIN":
                tradfi.append(f"{m.get('base')}:{info.get('underlyingType')}")
                continue
            bn[m["base"]] = {"symbol": sym, "status": info.get("status"), "onboard": int(info.get("onboardDate") or 0)}
        ym = (await fx.rt.exchanges.markets(BYB)).data or {}
        by = {}
        for sym, m in ym.items():
            if m and m.get("swap") and m.get("linear") and sym.endswith("/USDT:USDT"):
                info = m.get("info") or {}
                by[m["base"]] = {"symbol": sym, "status": info.get("status"), "launch": int(info.get("launchTime") or 0)}
        return {"hl": hl, "binance": bn, "bybit": by, "binance_non_coin": sorted(tradfi), "fetched_at": int(time.time())}

    return await fx.cached("universe.json", go)


async def llama_meta(fx: Fetcher, snap: str) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        ov = await fx.ll(
            "/overview/fees",
            {"dataType": "dailyHoldersRevenue", "excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"},
        )
        protos = await fx.ll("/protocols")
        lite = await fx.ll("/lite/protocols2")
        keep = ("id", "name", "slug", "symbol", "gecko_id", "address", "parentProtocol", "category", "listedAt")
        pmap = {str(p.get("id")): {k: p.get(k) for k in keep} for p in protos}
        parents = {p["id"]: {k: p.get(k) for k in ("id", "name", "symbol", "gecko_id", "address")} for p in lite["parentProtocols"]}
        rows = []
        for p in ov["protocols"]:
            rows.append(
                {
                    k: p.get(k)
                    for k in ("defillamaId", "name", "slug", "category", "parentProtocol", "total30d", "totalAllTime", "methodologyURL")
                }
            )
        return {"overview": rows, "protocols": pmap, "parents": parents, "fetched_at": int(time.time())}

    return await fx.cached(f"llama_meta_{snap}.json", go)


def token_table(meta: dict[str, Any], uni: dict[str, Any]) -> dict[str, Any]:
    """Group holders-revenue protocols by token (gecko id, else symbol) and match them to perp tickers by symbol."""
    venues: dict[str, dict[str, tuple[str, int]]] = {
        HL: {n: normalize_asset(n, HL) for n in uni["hl"]},
        BIN: {n: normalize_asset(n, BIN) for n in uni["binance"]},
        BYB: {n: normalize_asset(n, BYB) for n in uni["bybit"]},
    }
    by_asset: dict[str, dict[str, list[str]]] = {}
    for v, names in venues.items():
        for n, (asset, _) in names.items():
            by_asset.setdefault(asset, {}).setdefault(v, []).append(n)
    tokens: dict[str, Any] = {}
    skipped = {"chain": 0, "no_symbol": 0, "no_perp": 0}
    for row in meta["overview"]:
        pid = str(row["defillamaId"])
        if pid.startswith("chain#") or row.get("category") == "Chain":
            skipped["chain"] += 1
            continue
        p = meta["protocols"].get(pid) or {}
        par = meta["parents"].get(p.get("parentProtocol") or row.get("parentProtocol") or "") or {}
        sym = (p.get("symbol") or "").strip().upper()
        if not sym or sym == "-":
            skipped["no_symbol"] += 1
            continue
        gecko = p.get("gecko_id") or (par.get("gecko_id") if (par.get("symbol") or "").upper() == sym else None)
        address = p.get("address") or (par.get("address") if (par.get("symbol") or "").upper() == sym else None)
        perps = by_asset.get(sym)
        if not perps:
            skipped["no_perp"] += 1
            continue
        key = f"{sym}|{gecko or address or '?'}"
        t = tokens.setdefault(key, {"symbol": sym, "gecko_id": gecko, "address": address, "perps": perps, "protocols": []})
        t["protocols"].append(
            {
                "id": pid,
                "slug": row["slug"],
                "name": row["name"],
                "category": row.get("category"),
                "listedAt": p.get("listedAt"),
                "methodologyURL": row.get("methodologyURL"),
            }
        )
    collisions = {}
    for key, t in tokens.items():
        same = [k for k in tokens if k != key and tokens[k]["symbol"] == t["symbol"]]
        if same:
            collisions[t["symbol"]] = sorted([key, *same])
    return {"tokens": tokens, "skipped": skipped, "symbol_collisions": collisions}


async def revenue_series(fx: Fetcher, slug: str, snap: str) -> dict[str, Any] | None:
    async def go() -> dict[str, Any]:
        try:
            s = await fx.ll(f"/summary/fees/{slug}", {"dataType": "dailyHoldersRevenue"})
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200], "chart": []}
        return {
            "chart": s.get("totalDataChart") or [],
            "methodology": (s.get("methodology") or {}).get("HoldersRevenue"),
            "fetched_at": int(time.time()),
        }

    return await fx.cached(f"rev_{snap}/{slug.replace('/', '_')}.json", go)


async def hl_life(fx: Fetcher, coin: str) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        start = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
        end = int(LAST_CLOSE.timestamp() * 1000) + 86_400_000
        rows = await fx.hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d", "startTime": start, "endTime": end}}, 20)
        ts = [int(r["t"]) // 1000 // DAY_S for r in rows or []]
        last_close = float(rows[-1]["c"]) if rows else None
        return {"first": min(ts) if ts else None, "last": max(ts) if ts else None, "n": len(ts), "last_close": last_close}

    return await fx.cached(f"hl_life/{coin}.json", go)


def listing_window(t: dict[str, Any], uni: dict[str, Any], hl_lives: dict[str, Any]) -> tuple[int | None, int | None, list[str]]:
    """Earliest listing day across venues and the last day a perp existed (None = still listed); plus notes."""
    firsts, lasts, notes = [], [], []
    today = day_of(LAST_CLOSE)
    for n in t["perps"].get(BIN, []):
        b = uni["binance"][n]
        if b["onboard"]:
            firsts.append(b["onboard"] // 1000 // DAY_S)
        lasts.append(today if b.get("status") == "TRADING" else None)
        if b.get("status") != "TRADING":
            notes.append(f"binance {n} status {b.get('status')}")
    for n in t["perps"].get(BYB, []):
        y = uni["bybit"][n]
        if y["launch"]:
            firsts.append(y["launch"] // 1000 // DAY_S)
        lasts.append(today)
    for n in t["perps"].get(HL, []):
        life = hl_lives.get(n) or {}
        if life.get("first") is not None:
            firsts.append(life["first"])
            lasts.append(life["last"] if uni["hl"][n]["delisted"] else today)
            if uni["hl"][n]["delisted"]:
                notes.append(f"hl {n} delisted, last candle {ymd(life['last'])}")
    first = min(firsts) if firsts else None
    last = max((x for x in lasts if x is not None), default=None)
    return first, last, notes


async def stage0(cache: Path, snap: str) -> None:
    _, rt = build_server()
    fx = Fetcher(rt, cache)
    t0 = time.perf_counter()
    try:
        uni = await perp_universe(fx)
        meta = await llama_meta(fx, snap)
        tab = token_table(meta, uni)
        toks = tab["tokens"]
        n_prot = sum(len(t["protocols"]) for t in toks.values())
        print(
            f"defillama holders-revenue protocols {len(meta['overview'])}; skipped {tab['skipped']}; "
            f"tokens with a perp {len(toks)} ({n_prot} protocols); symbol collisions {tab['symbol_collisions']}",
            flush=True,
        )
        print(
            f"perps: HL {len(uni['hl'])} ({sum(v['delisted'] for v in uni['hl'].values())} delisted), "
            f"Binance crypto {len(uni['binance'])} (non-COIN skipped {len(uni['binance_non_coin'])}), Bybit {len(uni['bybit'])}",
            flush=True,
        )
        slugs = sorted({p["slug"] for t in toks.values() for p in t["protocols"]})
        t1 = time.perf_counter()
        for i, s in enumerate(slugs):
            await revenue_series(fx, s, snap)
            if (i + 1) % 20 == 0 or i + 1 == len(slugs):
                el = time.perf_counter() - t1
                print(f"  revenue {i + 1}/{len(slugs)} [{el:.0f}s, eta {el / (i + 1) * (len(slugs) - i - 1):.0f}s]", flush=True)
        coins = sorted({n for t in toks.values() for n in t["perps"].get(HL, [])})
        lives = {}
        for c in coins:
            lives[c] = await hl_life(fx, c)
        wr(cache / f"token_table_{snap}.json", tab)
        print(f"fetched in {time.perf_counter() - t0:.0f}s", flush=True)
    finally:
        await fx.aclose()
        await rt.aclose()
    report_stage0(cache, snap, uni, tab, lives)


def token_series(cache: Path, snap: str, t: dict[str, Any]) -> tuple[dict[int, float], list[str]]:
    parts, errs = [], []
    for p in t["protocols"]:
        f = cache / f"rev_{snap}/{p['slug'].replace('/', '_')}.json"
        if not f.exists():
            errs.append(f"{p['slug']}: not fetched")
            continue
        d = rd(f)
        if d.get("error"):
            errs.append(f"{p['slug']}: {d['error'][:80]}")
        parts.append(to_daily(d.get("chart") or []))
    return combine(parts), errs


def count_events(
    cache: Path, snap: str, uni: dict[str, Any], tab: dict[str, Any], lives: dict[str, Any], rule: DropRule
) -> list[dict[str, Any]]:
    first_sig, last_sig = day_of(FIRST_SIGNAL), day_of(LAST_CLOSE) - HORIZON - rule.lag
    out = []
    for key, t in sorted(tab["tokens"].items()):
        series, _ = token_series(cache, snap, t)
        if not series:
            continue
        first, last, _ = listing_window(t, uni, lives)
        if first is None:
            continue

        def tradable(entry: int, first: int = first, last: int | None = last) -> bool:
            return entry - first >= MIN_LISTED and (last is None or entry <= last)

        for ev in drop_events(series, rule, first_day=first_sig, last_day=last_sig, tradable=tradable):
            out.append(
                {
                    "token": key,
                    "symbol": t["symbol"],
                    "signal": ev.signal_day,
                    "entry": ev.entry_day,
                    "prior": ev.prior,
                    "current": ev.current,
                    "ratio": ev.ratio,
                }
            )
    return out


def report_stage0(cache: Path, snap: str, uni: dict[str, Any], tab: dict[str, Any], lives: dict[str, Any]) -> None:
    print("\n## Token table (DefiLlama token <-> perps <-> first listing)\n")
    print("| token | protocols | perps | first perp day | last | revenue rows | first rev day | notes |")
    print("|---|---|---|---|---|---|---|---|")
    for key, t in sorted(tab["tokens"].items()):
        series, errs = token_series(cache, snap, t)
        first, last, notes = listing_window(t, uni, lives)
        perps = "; ".join(f"{v}:{','.join(ns)}" for v, ns in sorted(t["perps"].items()))
        nz = [d for d, v in series.items() if v > 0]
        print(
            f"| {key} | {', '.join(p['slug'] for p in t['protocols'])} | {perps} | {ymd(first)} | {ymd(last) if last != day_of(LAST_CLOSE) else 'live'} "
            f"| {len(series)} | {ymd(min(nz)) if nz else '—'} | {'; '.join(notes + errs)} |"
        )
    evs = count_events(cache, snap, uni, tab, lives, MAIN)
    print(f"\n## Main-variant events (30/30, drop >= 50 %, prior >= 100k USD, lag 1, perp listed >= {MIN_LISTED}d before entry)\n")
    print(f"events: {len(evs)}; tokens: {len({e['token'] for e in evs})}")
    by_year: dict[str, int] = {}
    for e in evs:
        by_year[ymd(e["entry"])[:4]] = by_year.get(ymd(e["entry"])[:4], 0) + 1
    print(f"by entry year: {dict(sorted(by_year.items()))}")
    for e in sorted(evs, key=lambda e: e["entry"]):
        print(f"  {ymd(e['signal'])} {e['token']:<28} prior {e['prior']:>12,.0f} current {e['current']:>12,.0f} ratio {e['ratio']:.2f}")
    no_perp_rule = DropRule()
    all_evs = count_events_no_perp(cache, snap, tab, no_perp_rule)
    print(f"\n(for reference) the same rule without the perp-existence filter: {all_evs} events")
    wr(cache / f"events_main_{snap}.json", evs)


def count_events_no_perp(cache: Path, snap: str, tab: dict[str, Any], rule: DropRule) -> int:
    first_sig, last_sig = day_of(FIRST_SIGNAL), day_of(LAST_CLOSE) - HORIZON - rule.lag
    return sum(
        len(drop_events(token_series(cache, snap, t)[0], rule, first_day=first_sig, last_day=last_sig)) for t in tab["tokens"].values()
    )


def compare_snapshots(cache: Path, a: str, b: str) -> None:
    """Revisions between two downloads: rows changed by > 1 % (or > 100 USD) per protocol, and event-list changes."""
    da, db = cache / f"rev_{a}", cache / f"rev_{b}"
    changed, total, prots = 0, 0, []
    for f in sorted(db.glob("*.json")):
        g = da / f.name
        if not g.exists():
            continue
        sa, sb = to_daily(rd(g).get("chart") or []), to_daily(rd(f).get("chart") or [])
        common = [d for d in sa.keys() & sb.keys() if d < day_of(LAST_CLOSE) - 2]
        diff = [d for d in common if abs(sa[d] - sb[d]) > max(100.0, 0.01 * abs(sa[d]))]
        total += len(common)
        changed += len(diff)
        if diff:
            prots.append(f"{f.stem}: {len(diff)} rows ({ymd(min(diff))}..{ymd(max(diff))})")
    print(f"snapshot {a} vs {b}: {changed} of {total} common rows (older than 2 days) changed")
    for p in prots[:40]:
        print("  " + p)
    ea, eb = cache / f"events_main_{a}.json", cache / f"events_main_{b}.json"
    if ea.exists() and eb.exists():
        ka = {(e["token"], e["signal"]) for e in rd(ea)}
        kb = {(e["token"], e["signal"]) for e in rd(eb)}
        print(f"main events: {len(ka)} vs {len(kb)}; only in {a}: {sorted(ka - kb)}; only in {b}: {sorted(kb - ka)}")


# ----------------------------------------------------------------------------- prices

BARS_START = datetime(2020, 6, 1, tzinfo=UTC)
CHECK_DATES = [
    datetime(y, m, 1, tzinfo=UTC)
    for y, m in [
        (2021, 7),
        (2022, 7),
        (2023, 7),
        (2024, 1),
        (2024, 7),
        (2025, 1),
        (2025, 7),
        (2025, 9),
        (2026, 1),
        (2026, 4),
        (2026, 7),
        (2026, 10),
    ]
]


# Manual ticker table: perps listed under another name than the DefiLlama symbol (renames, venue-specific tickers).
# Every leg still has to pass the price check against DefiLlama coins.
ALIASES: dict[str, dict[str, list[str]]] = {
    "RAY": {BIN: ["RAYSOL"], BYB: ["RAYDIUM"]},  # Raydium: Binance RAYSOL since 2024-12, Bybit RAYDIUM since 2024-06
}


def perps_of(t: dict[str, Any]) -> dict[str, list[str]]:
    out = {v: list(ns) for v, ns in t["perps"].items()}
    for v, ns in ALIASES.get(t["symbol"], {}).items():
        out[v] = [*out.get(v, []), *(n for n in ns if n not in out.get(v, []))]
    return out


def bars_name(venue: str, name: str) -> str:
    return f"bars/{venue}_{name.replace('/', '-')}.json"


async def cex_daily(fx: Fetcher, venue: str, sym: str) -> list[list[float]]:
    """Daily OHLCV ``[day, o, h, l, c, v]`` from BARS_START to LAST_CLOSE (complete days only), walked page by page."""
    start = int(BARS_START.timestamp() * 1000)
    end = (day_of(LAST_CLOSE) + 1) * DAY_S * 1000
    page = 1500 if venue == BIN else 1000
    rows: dict[int, list[float]] = {}
    since = start
    while since < end:
        until = min(end, since + page * DAY_S * 1000) - 1
        f = await retry(lambda s=since, u=until: fx.rt.exchanges.ohlcv(venue, sym, "1d", page, since_ms=s, until_ms=u), f"{venue} {sym}")
        batch = [b for b in (f.data or []) if isinstance(b, (list, tuple)) and len(b) >= 6 and b[4]]
        for b in batch:
            rows[int(b[0]) // 1000 // DAY_S] = [int(b[0]) // 1000 // DAY_S, *(float(x or 0) for x in b[1:6])]
        last = max((int(b[0]) for b in batch), default=None)
        since = last + DAY_S * 1000 if last is not None and last + DAY_S * 1000 > since else until + 1
        await asyncio.sleep(0.12)
    return [rows[d] for d in sorted(rows) if d <= day_of(LAST_CLOSE)]


async def hl_daily(fx: Fetcher, coin: str) -> list[list[float]]:
    start = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
    end = (day_of(LAST_CLOSE) + 1) * DAY_S * 1000
    rows = await fx.hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d", "startTime": start, "endTime": end}}, 20)
    out = {}
    for r in rows or []:
        d = int(r["t"]) // 1000 // DAY_S
        out[d] = [d, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"]) * float(r["c"])]
    return [out[d] for d in sorted(out) if d <= day_of(LAST_CLOSE)]


def venue_symbol(uni: dict[str, Any], venue: str, name: str) -> str:
    return name if venue == HL else uni["binance" if venue == BIN else "bybit"][name]["symbol"]


def coin_key(t: dict[str, Any]) -> str | None:
    if t.get("gecko_id"):
        return f"coingecko:{t['gecko_id']}"
    a = (t.get("address") or "").strip()
    key = a if ":" in a else (f"ethereum:{a}" if a.startswith("0x") else None)
    return None if key is None or "/" in key or " " in key else key


async def fetch_prices(cache: Path, sample: int) -> None:
    _, rt = build_server()
    fx = Fetcher(rt, cache)
    t0 = time.perf_counter()
    try:
        uni = rd(cache / "universe.json")
        tab = rd(cache / "token_table_a.json")
        jobs: list[tuple[str, str]] = [(BIN, "BTC")]
        for t in tab["tokens"].values():
            for v, names in perps_of(t).items():
                jobs.extend((v, n) for n in names)
        jobs = sorted(set(jobs), key=lambda j: (j != (BIN, "BTC"), j))
        if sample:
            jobs = jobs[:sample]
        print(f"candle jobs: {len(jobs)}", flush=True)
        for i, (v, n) in enumerate(jobs):
            if v == HL:
                await fx.cached(bars_name(v, n), lambda n=n: hl_daily(fx, n))
            else:
                await fx.cached(bars_name(v, n), lambda v=v, n=n: cex_daily(fx, v, venue_symbol(uni, v, n)))
            if (i + 1) % 20 == 0 or i + 1 == len(jobs):
                el = time.perf_counter() - t0
                print(f"  bars {i + 1}/{len(jobs)} [{el:.0f}s, eta {el / (i + 1) * (len(jobs) - i - 1):.0f}s]", flush=True)
        keys = sorted({k for t in tab["tokens"].values() if (k := coin_key(t))})
        for dt in CHECK_DATES:

            async def go(dt: datetime = dt) -> dict[str, Any]:
                out: dict[str, Any] = {}
                for j in range(0, len(keys), 40):
                    chunk = ",".join(keys[j : j + 40])
                    await fx.llama_pace.take()
                    try:
                        r = await retry(
                            lambda c=chunk: fx.coins.get_json(
                                f"/prices/historical/{int(dt.timestamp())}/{c}", params={"searchWidth": "12h"}
                            ),
                            "coins",
                        )
                        out.update((r.data or {}).get("coins") or {})
                    except Exception as e:  # noqa: BLE001
                        print(f"  coins chunk failed: {str(e)[:120]}", flush=True)
                return out

            await fx.cached(f"coins_v2/{dt:%Y%m%d}.json", go)
        print(f"prices fetched in {time.perf_counter() - t0:.0f}s", flush=True)
    finally:
        await fx.aclose()
        await rt.aclose()


async def fetch_funding(cache: Path, needed: list[tuple[str, str]]) -> None:
    """Full funding history for the (venue, name) legs the analysis used."""
    _, rt = build_server()
    fx = Fetcher(rt, cache)
    uni = rd(cache / "universe.json")
    pace = Pacer(300.0)
    t0 = time.perf_counter()
    end = (day_of(LAST_CLOSE) + 1) * DAY_S * 1000
    try:
        for i, (v, n) in enumerate(needed):

            async def go(v: str = v, n: str = n) -> dict[str, Any]:
                out: dict[int, float] = {}
                complete = False
                bars = rd(cache / bars_name(v, n))
                # start at the first daily bar of the leg: walking empty pre-listing windows costs hundreds of requests
                start = max(int(BARS_START.timestamp() * 1000), (int(bars[0][0]) - 1) * DAY_S * 1000) if bars else end
                if v == HL:
                    s = start
                    for _ in range(400):
                        rows = await fx.hl({"type": "fundingHistory", "coin": n, "startTime": s, "endTime": end}, 45)
                        batch = [r for r in rows or [] if r.get("time") is not None]
                        for r in batch:
                            out[int(r["time"]) // 1000] = float(r["fundingRate"])
                        if len(batch) < 500:
                            complete = True
                            break
                        s = max(int(r["time"]) for r in batch) + 1
                elif v == BYB:
                    f = await retry(
                        lambda: fx.rt.exchanges.funding_rate_history_range(v, venue_symbol(uni, v, n), start, end, max_pages=800),
                        f"{v} {n} funding",
                    )
                    for x in f.data["rows"]:
                        if x.get("fundingRate") is not None:
                            out[int(x["timestamp"]) // 1000] = float(x["fundingRate"])
                    complete = bool(f.data["complete"])
                else:
                    s = start
                    for _ in range(400):
                        await pace.take()
                        f = await retry(
                            lambda s=s: fx.rt.exchanges.funding_rate_history(v, venue_symbol(uni, v, n), s, 1000), f"{v} {n} funding"
                        )
                        batch = [x for x in (f.data or []) if x.get("timestamp") is not None and x.get("fundingRate") is not None]
                        for x in batch:
                            out[int(x["timestamp"]) // 1000] = float(x["fundingRate"])
                        if len(batch) < 1000:
                            complete = True
                            break
                        s = max(int(x["timestamp"]) for x in batch) + 1
                return {"rows": sorted(out.items()), "complete": complete}

            await fx.cached(f"funding/{v}_{n}.json", go)
            if (i + 1) % 10 == 0 or i + 1 == len(needed):
                el = time.perf_counter() - t0
                print(f"  funding {i + 1}/{len(needed)} [{el:.0f}s, eta {el / (i + 1) * (len(needed) - i - 1):.0f}s]", flush=True)
    finally:
        await fx.aclose()
        await rt.aclose()


# ----------------------------------------------------------------------------- analysis

SIDE_COST = {BIN: 0.0005 + 0.001, BYB: 0.00055 + 0.001, HL: 0.00045 + 0.001}
BTC_SIDE = 0.0005 + 0.0002
VENUE_ORDER = (BIN, BYB, HL)
K = 3
BAND = (0.8, 1.25)
LIQ = {"3x": 1 / 3 - 0.025, "5x": 1 / 5 - 0.025}


class Bars:
    def __init__(self, rows: list[list[float]], *, drop_zero_volume: bool = True) -> None:
        # Binance keeps serving flat zero-volume candles for SETTLING (delisted) perps: those days did not trade
        rows = [r for r in rows if r[5] > 0] if drop_zero_volume else rows
        self.close = {int(r[0]): r[4] for r in rows if r[4] > 0}
        self.high = {int(r[0]): r[2] for r in rows if r[2] > 0}
        self.vol = {int(r[0]): r[5] for r in rows}
        self.first = min(self.close) if self.close else None
        self.last = max(self.close) if self.close else None

    def last_close_on_or_before(self, day: int, floor: int) -> int | None:
        for d in range(day, floor, -1):
            if d in self.close:
                return d
        return None


class Data:
    def __init__(self, cache: Path, snap: str = "a") -> None:
        self.cache = cache
        self.uni = rd(cache / "universe.json")
        self.tab = rd(cache / f"token_table_{snap}.json")
        self.btc = Bars(rd(cache / bars_name(BIN, "BTC")))
        self.coins = {f.stem: rd(f) for f in sorted((cache / "coins_v2").glob("*.json"))}
        self.legs: dict[str, list[tuple[str, str, int, Bars]]] = {}
        self.checks: list[str] = []
        self.revenue: dict[str, dict[int, float]] = {}
        self.funding: dict[tuple[str, str], list[tuple[int, float]] | None] = {}
        for key, t in sorted(self.tab["tokens"].items()):
            ck = coin_key(t)
            ok = []
            for v in VENUE_ORDER:
                for n in perps_of(t).get(v, []):
                    p = cache / bars_name(v, n)
                    if not p.exists():
                        continue
                    bars = Bars(rd(p))
                    mult = normalize_asset(n, v)[1]
                    ratios = []
                    for stem, cs in self.coins.items():
                        d = day_of(datetime.strptime(stem, "%Y%m%d").replace(tzinfo=UTC))
                        cp = (cs.get(ck) or {}).get("price") if ck else None
                        # the coins price is at 00:00 UTC of the date = close of the previous day
                        pc = bars.close.get(d - 1)
                        if cp and pc:
                            ratios.append(pc / mult / cp)
                    med = statistics.median(ratios) if ratios else None
                    good = med is not None and BAND[0] <= med <= BAND[1]
                    self.checks.append(
                        f"{key} {v}:{n} n={len(ratios)} median={med if med is None else round(med, 3)} -> {'ok' if good else 'DROP'}"
                    )
                    if good:
                        ok.append((v, n, mult, bars))
            if ok:
                self.legs[key] = ok
            self.revenue[key] = token_series(cache, snap, t)[0]

    def leg_at(self, key: str, entry: int) -> tuple[str, str, int, Bars] | None:
        """First venue (Binance, Bybit, HL) whose perp is >= MIN_LISTED days old with a close on the entry day."""
        for leg in self.legs.get(key, []):
            b = leg[3]
            if b.first is not None and entry - b.first >= MIN_LISTED and entry in b.close:
                return leg
        return None

    def fund(self, v: str, n: str) -> list[tuple[int, float]] | None:
        if (v, n) not in self.funding:
            p = self.cache / f"funding/{v}_{n}.json"
            self.funding[(v, n)] = [(int(a), float(b)) for a, b in rd(p)["rows"]] if p.exists() else None
        return self.funding[(v, n)]


@dataclass
class Obs:
    token: str
    entry: int
    exit: int
    venue: str
    name: str
    leg: LegResult
    ret30: float | None
    volchg: float | None
    truncated: bool
    funding_missing: bool
    mae: float | None


def observe(data: Data, key: str, entry: int, horizon: int, *, need_funding: set[tuple[str, str]] | None = None) -> Obs | None:
    leg = data.leg_at(key, entry)
    if leg is None:
        return None
    v, n, _, bars = leg
    b = beta(bars.close, data.btc.close, entry)
    if b is None:
        return None
    exit_ = bars.last_close_on_or_before(entry + horizon, entry)
    if exit_ is None or exit_ == entry or exit_ not in data.btc.close:
        return None
    ar = hedged_return(bars.close, data.btc.close, entry, exit_, b)
    if ar is None:
        return None
    if need_funding is not None:
        need_funding.add((v, n))
    fr = data.fund(v, n)
    f = funding_sum(fr, (entry + 1) * DAY_S, (exit_ + 1) * DAY_S) if fr is not None else 0.0
    r30 = simple_return(bars.close, entry - 30, entry)
    v_cur = sum(bars.vol.get(d, 0.0) for d in range(entry - 29, entry + 1))
    v_pri = sum(bars.vol.get(d, 0.0) for d in range(entry - 59, entry - 29))
    volchg = math.log(v_cur / v_pri) if v_cur > 0 and v_pri > 0 and entry - 59 >= (bars.first or entry) else None
    mae = max_adverse_short(bars.high, bars.close[entry], entry, exit_)
    return Obs(key, entry, exit_, v, n, LegResult(b, ar, f, SIDE_COST[v]), r30, volchg, exit_ < entry + horizon, fr is None, mae)


def btc_fund(data: Data, entry: int, exit_: int) -> float:
    fr = data.fund(BIN, "BTC")
    return funding_sum(fr, (entry + 1) * DAY_S, (exit_ + 1) * DAY_S) if fr else 0.0


def events_for(data: Data, rule: DropRule) -> dict[str, list[RevenueEvent]]:
    first_sig, last_sig = day_of(FIRST_SIGNAL), day_of(LAST_CLOSE) - HORIZON - rule.lag
    out = {}
    for key in data.legs:
        evs = drop_events(
            data.revenue[key], rule, first_day=first_sig, last_day=last_sig, tradable=lambda e, key=key: data.leg_at(key, e) is not None
        )
        if evs:
            out[key] = evs
    return out


@dataclass
class Row:
    token: str
    entry: int
    signal: int
    ratio: float
    prior: float
    gross: float
    net: float
    gross_nc: float
    net_nc: float
    net2: float | None
    controls: list[str]
    obs: Obs
    cost: float
    net_nh: float  # short the token outright, no hedge
    dbeta: float  # beta_event - mean beta of the controls


def evaluate(
    data: Data,
    events: dict[str, list[RevenueEvent]],
    *,
    horizon: int = HORIZON,
    cost_mult: float = 1.0,
    need: set[tuple[str, str]] | None = None,
    dates: dict[str, list[int]] | None = None,
) -> list[Row]:
    """Rows for every event (or for explicit ``dates`` per token: placebo) with the momentum control."""
    past_events = {k: [e.entry_day for e in v] for k, v in events.items()}
    rows: list[Row] = []
    items: list[tuple[str, int, RevenueEvent | None]] = []
    if dates is None:
        items = [(k, e.entry_day, e) for k, evs in events.items() for e in evs]
    else:
        items = [(k, d, None) for k, ds in dates.items() for d in ds]
    cand_cache: dict[int, dict[str, Obs]] = {}
    for key, entry, ev in sorted(items, key=lambda x: (x[1], x[0])):
        o = observe(data, key, entry, horizon, need_funding=need)
        if o is None or o.ret30 is None:
            continue
        if entry not in cand_cache:
            pool = {}
            for c in data.legs:
                if excluded_from_control_pool(past_events.get(c, []), entry, lookback=60):
                    continue
                oc = observe(data, c, entry, horizon, need_funding=need)
                if oc is not None and oc.ret30 is not None:
                    pool[c] = oc
            cand_cache[entry] = pool
        pool = {
            c: oc
            for c, oc in cand_cache[entry].items()
            if c != key and data.tab["tokens"][c]["symbol"] != data.tab["tokens"][key]["symbol"]
        }
        if len(pool) < K:
            continue
        ctrl = nearest_controls(o.ret30, {c: oc.ret30 for c, oc in pool.items() if oc.ret30 is not None}, K)
        bf = btc_fund(data, entry, o.exit)
        scale = lambda lr: LegResult(lr.beta, lr.ar, lr.funding, lr.side_cost * cost_mult)  # noqa: E731
        gross, net = pair_net(scale(o.leg), [scale(pool[c].leg) for c in ctrl], btc_funding=bf, btc_side_cost=BTC_SIDE * cost_mult)
        g_nc, n_nc = short_vs_btc_net(scale(o.leg), btc_funding=bf, btc_side_cost=BTC_SIDE * cost_mult)
        net2 = None
        if o.volchg is not None:
            p2 = {c: (oc.ret30, oc.volchg) for c, oc in pool.items() if oc.volchg is not None and oc.ret30 is not None}
            if len(p2) >= K:
                c2 = rank_controls((o.ret30, o.volchg), p2, K)
                net2 = pair_net(scale(o.leg), [scale(pool[c].leg) for c in c2], btc_funding=bf, btc_side_cost=BTC_SIDE * cost_mult)[1]
        dbeta = o.leg.beta - statistics.fmean(pool[c].leg.beta for c in ctrl)
        cost = gross - net + o.leg.funding - statistics.fmean(pool[c].leg.funding for c in ctrl) - dbeta * bf
        r_btc = simple_return(data.btc.close, entry, o.exit) or 0.0
        net_nh = -(o.leg.ar + o.leg.beta * r_btc) - 2 * o.leg.side_cost * cost_mult + o.leg.funding
        rows.append(
            Row(
                key, entry, ev.signal_day if ev else entry - 1, ev.ratio if ev else float("nan"), ev.prior if ev else float("nan"),
                gross, net, g_nc, n_nc, net2, ctrl, o, cost, net_nh, dbeta,
            )
        )  # fmt: skip
    return rows


def placebo_dates(data: Data, events: dict[str, list[RevenueEvent]]) -> dict[str, list[int]]:
    last = day_of(LAST_CLOSE) - HORIZON
    out = {}
    for key, evs in events.items():
        start = next((leg[3].first for leg in data.legs[key] if leg[3].first is not None), None)
        if start is None:
            continue
        ds = []
        d = start + MIN_LISTED + 30
        while d <= last:
            if all(abs(d - e.entry_day) >= 60 for e in evs) and d >= day_of(FIRST_SIGNAL):
                ds.append(d)
            d += 60
        out[key] = ds
    return out


def summarize(rows: list[Row], attr: str = "net") -> dict[str, Any]:
    xs = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
    rr = [r for r in rows if getattr(r, attr) is not None]
    if len(xs) < 3:
        return {"n": len(xs)}
    se = cluster_se(xs, [[r.token for r in rr], [r.entry // 60 for r in rr]])
    m = statistics.fmean(xs)
    ci = cluster_bootstrap_mean_ci(xs, [r.token for r in rr])
    return {
        "n": len(xs),
        "tokens": len({r.token for r in rr}),
        "mean": m,
        "median": statistics.median(xs),
        "se": se,
        "t": m / se if se > 0 else float("nan"),
        "ci_token": ci,
        "hit": sum(x > 0 for x in xs) / len(xs),
    }


def fmt(s: dict[str, Any]) -> str:
    if "mean" not in s:
        return f"n={s['n']} (too few)"
    ci = s["ci_token"]
    cis = f"[{ci[0] * 100:+.2f}, {ci[1] * 100:+.2f}]" if ci else "—"
    return f"n={s['n']:>3} tok={s['tokens']:>2} mean={s['mean'] * 100:+6.2f}pp median={s['median'] * 100:+6.2f}pp t2way={s['t']:+5.2f} CI_tok={cis} hit={s['hit']:.0%}"


def collect_needed(cache: Path) -> list[tuple[str, str]]:
    data = Data(cache)
    need: set[tuple[str, str]] = {(BIN, "BTC")}
    evs = events_for(data, MAIN)
    evaluate(data, evs, need=need)
    evaluate(data, evs, need=need, dates=placebo_dates(data, evs))
    for rule in (DropRule(lag=3),):
        evaluate(data, events_for(data, rule), need=need)
    return sorted(need)


def report(cache: Path) -> None:
    data = Data(cache)
    print("## Ticker / price check (perp close / DefiLlama coins price, median over check dates)\n")
    drops = [c for c in data.checks if c.endswith("DROP")]
    print(
        f"legs checked {len(data.checks)}, kept {len(data.checks) - len(drops)}, dropped {len(drops)}; tokens with a kept leg {len(data.legs)}"
    )
    for c in drops:
        print("  " + c)
    evs = events_for(data, MAIN)
    rows = evaluate(data, evs)
    n_ev = sum(len(v) for v in evs.values())
    miss_f = sum(r.obs.funding_missing for r in rows)
    print(
        f"\n## Main variant\n\nevents after the price check: {n_ev} on {len(evs)} tokens; evaluated (beta, 30d return, >= {K} controls): {len(rows)}; "
        f"truncated exits {sum(r.obs.truncated for r in rows)}; funding missing {miss_f}"
    )
    print("H1  net, momentum control : " + fmt(summarize(rows, "net")))
    print("    gross, momentum control: " + fmt(summarize(rows, "gross")))
    print("    net, control-2 (ret30 + volume change ranks): " + fmt(summarize(rows, "net2")))
    print("    net, no control (short vs beta BTC): " + fmt(summarize(rows, "net_nc")))
    print("    gross, no control: " + fmt(summarize(rows, "gross_nc")))
    print(
        f"    mean cost per trade (both legs, round trip): {statistics.fmean(r.cost for r in rows) * 100:.2f}pp; "
        f"mean funding received by the short token {statistics.fmean(r.obs.leg.funding for r in rows) * 100:+.2f}pp"
    )
    # placebo
    pd = placebo_dates(data, evs)
    prow = evaluate(data, evs, dates=pd)
    print("\nplacebo (same tokens, non-overlapping dates): " + fmt(summarize(prow, "net")))
    print("placebo, no control (diagnostic, not pre-registered): " + fmt(summarize(prow, "net_nc")))
    ids = {k: i for i, k in enumerate(sorted({r.token for r in rows + prow}))}
    diff = cluster_bootstrap_diff([r.net for r in rows], [ids[r.token] for r in rows], [r.net for r in prow], [ids[r.token] for r in prow])
    if diff["diff"] is not None:
        ci = diff["ci"]
        print(f"event - placebo: {diff['diff'] * 100:+.2f}pp, token-cluster bootstrap 95% CI [{ci[0] * 100:+.2f}, {ci[1] * 100:+.2f}]")
    # halves, years
    med = statistics.median(r.entry for r in rows)
    h1 = [r for r in rows if r.entry <= med]
    h2 = [r for r in rows if r.entry > med]
    print(f"\nfirst half (entry <= {ymd(int(med))}): " + fmt(summarize(h1)))
    print("second half: " + fmt(summarize(h2)))
    for y in sorted({ymd(r.entry)[:4] for r in rows}):
        print(f"  {y}: " + fmt(summarize([r for r in rows if ymd(r.entry)[:4] == y])))
    top = sorted(rows, key=lambda r: -r.net)
    print("without top-5 by net: " + fmt(summarize(top[5:])))
    print("without bottom-5 by net: " + fmt(summarize(top[:-5])))
    print("costs x2: " + fmt(summarize(evaluate(data, evs, cost_mult=2.0))))
    print("lag 3 days: " + fmt(summarize(evaluate(data, events_for(data, DropRule(lag=3))))))
    print("\nper token (n, mean net):")
    by_tok: dict[str, list[float]] = {}
    for r in rows:
        by_tok.setdefault(r.token, []).append(r.net)
    print(
        "  "
        + "; ".join(
            f"{k.split('|')[0]} {len(v)} {statistics.fmean(v) * 100:+.1f}" for k, v in sorted(by_tok.items(), key=lambda kv: -len(kv[1]))
        )
    )
    # tails and capacity
    maes = [r.obs.mae for r in rows if r.obs.mae is not None]
    print(
        f"\nshort-leg MAE (daily highs): median {statistics.median(maes) * 100:.1f}%, p90 {sorted(maes)[int(0.9 * len(maes))] * 100:.1f}%, max {max(maes) * 100:.1f}%; "
        + ", ".join(f"share >= {lv * 100:.1f}% ({k}): {sum(m >= lv for m in maes) / len(maes):.0%}" for k, lv in LIQ.items())
    )
    days = {}
    for r in rows:
        for d in range(r.entry, r.obs.exit):
            days[d] = days.get(d, 0) + 1
    print(f"max simultaneous positions {max(days.values())}; median when any open {statistics.median(days.values()):.0f}")
    s = summarize(rows)
    if "mean" in s:
        bn = statistics.fmean(abs(r.dbeta) for r in rows)
        print(
            f"on capital at 3x (capital = (2 + |dbeta|)/3 of token notional, |dbeta|~{bn:.2f}): mean net {s['mean'] * 3 / (2 + bn) * 100:+.2f}% per 60 days"
        )
    # exploratory variants
    print("\n## Exploratory variants (do not change the verdict)\n")
    for label, rule in [
        ("90/90", DropRule(window=90, regular_days=180, min_nonzero=24, min_prior=300_000)),
        ("prior >= 30k", DropRule(min_prior=30_000)),
        ("prior >= 300k", DropRule(min_prior=300_000)),
        ("drop >= 40 %", DropRule(drop=0.4)),
        ("drop >= 60 %", DropRule(drop=0.6)),
    ]:
        ev2 = events_for(data, rule)
        print(f"{label:<14} " + fmt(summarize(evaluate(data, ev2))))
    print("no hedge (short token outright, net): " + fmt(summarize(rows, "net_nh")))
    # top-20 by absolute contribution
    print("\n## Top-20 events by |net| (manual review)\n")
    for r in sorted(rows, key=lambda r: -abs(r.net))[:20]:
        print(
            f"  {ymd(r.signal)} {r.token:<30} {r.obs.venue}:{r.obs.name:<10} ratio {r.ratio:.2f} prior {r.prior:>12,.0f} "
            f"net {r.net * 100:+6.1f}pp AR_e {r.obs.leg.ar * 100:+6.1f} ret30 {r.obs.ret30 * 100:+6.1f} controls {','.join(c.split('|')[0] for c in r.controls)}"
        )
    print("\n## All events\n")
    for r in rows:
        print(
            f"  {ymd(r.signal)} {r.token.split('|')[0]:<9} {r.obs.venue[:3]} ratio {r.ratio:.2f} beta {r.obs.leg.beta:+.2f} AR {r.obs.leg.ar * 100:+6.1f} "
            f"ret30 {r.obs.ret30 * 100:+6.1f} gross {r.gross * 100:+6.1f} net {r.net * 100:+6.1f} nc {r.net_nc * 100:+6.1f}{' T' if r.obs.truncated else ''}"
        )
    wr(
        cache / "rows_main.json",
        [{"token": r.token, "signal": r.signal, "entry": r.entry, "net": r.net, "gross": r.gross, "net_nc": r.net_nc} for r in rows],
    )


def sanity(cache: Path) -> None:
    """Checks before the full run: ticker/price agreement, gaps, a known BTC close, event counts after the check."""
    data = Data(cache)
    drops = [c for c in data.checks if c.endswith("DROP")]
    print(
        f"legs checked {len(data.checks)}, kept {len(data.checks) - len(drops)}, dropped {len(drops)}; tokens with a kept leg {len(data.legs)}"
    )
    for c in data.checks:
        print("  " + c)
    d = day_of(datetime(2024, 12, 31, tzinfo=UTC))
    print(f"BTC Binance close 2024-12-31: {data.btc.close.get(d)} (known ~93,500); first {ymd(data.btc.first)} last {ymd(data.btc.last)}")
    gaps = []
    for key, legs in data.legs.items():
        for v, n, _, b in legs:
            missing = [x for x in range(b.first, b.last + 1) if x not in b.close]
            if missing:
                gaps.append(f"{key.split('|')[0]} {v}:{n} {len(missing)} missing days of {b.last - b.first + 1}")
    print(f"legs with gaps: {len(gaps)}")
    for g in gaps[:30]:
        print("  " + g)
    evs = events_for(data, MAIN)
    print(
        f"main events after the price check (tradable leg, >= {MIN_LISTED}d listed): {sum(len(v) for v in evs.values())} on {len(evs)} tokens"
    )


async def adapter_ages(cache: Path) -> None:
    """Date of the oldest commit touching each event token's DefiLlama adapter path (GitHub API, unauthenticated,
    60 requests / hour). A lower bound for when the series could have existed in real time: renames are not
    followed and a ``HoldersRevenue`` field may have been added after the file was created."""
    tab = rd(cache / "token_table_a.json")
    evs = rd(cache / "events_main_a.json")
    keys = sorted({e["token"] for e in evs})
    gh = Http(
        "github", "https://api.github.com", rate=0.5, burst=1, timeout=30, retries=1, headers={"Accept": "application/vnd.github+json"}
    )
    out_p = cache / "adapter_ages.json"
    ages: dict[str, Any] = rd(out_p) if out_p.exists() else {}
    try:
        for key in keys:
            for p in tab["tokens"][key]["protocols"]:
                url = p.get("methodologyURL") or ""
                if p["slug"] in ages or "dimension-adapters/blob/" not in url:
                    continue
                path = url.split("dimension-adapters/blob/", 1)[1].split("/", 1)[1]
                oldest = None
                try:
                    for page in range(1, 20):
                        r = (
                            await gh.get_json(
                                "/repos/DefiLlama/dimension-adapters/commits", params={"path": path, "per_page": 100, "page": page}
                            )
                        ).data
                        if r:
                            oldest = r[-1]["commit"]["committer"]["date"][:10]
                        if len(r) < 100:
                            break
                except Exception as e:  # noqa: BLE001
                    print(f"  github stopped at {p['slug']}: {str(e)[:120]}", flush=True)
                    wr(out_p, ages)
                    return
                ages[p["slug"]] = {"path": path, "oldest_commit": oldest}
                print(f"  {key.split('|')[0]:<8} {p['slug']:<32} {path:<45} {oldest}", flush=True)
        wr(out_p, ages)
    finally:
        await gh.aclose()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["stage0", "compare", "fetch", "funding", "report", "adapters", "sanity"])
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--reverse", action="store_true", help="walk the funding legs from the end (second worker)")
    ap.add_argument("--out", type=Path, default=None, help="report: also write the output to this file")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/holders_revenue")
    ap.add_argument("--snapshot", default="a")
    ap.add_argument("--other", default="a")
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.cmd == "stage0":
        asyncio.run(stage0(a.cache_dir, a.snapshot))
    elif a.cmd == "compare":
        compare_snapshots(a.cache_dir, a.other, a.snapshot)
    elif a.cmd == "fetch":
        asyncio.run(fetch_prices(a.cache_dir, a.sample))
    elif a.cmd == "funding":
        need = collect_needed(a.cache_dir)
        print(f"funding legs needed: {len(need)}", flush=True)
        need = need[: a.sample] if a.sample else need
        asyncio.run(fetch_funding(a.cache_dir, need[::-1] if a.reverse else need))
    elif a.cmd == "sanity":
        sanity(a.cache_dir)
    elif a.cmd == "adapters":
        asyncio.run(adapter_ages(a.cache_dir))
    else:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report(a.cache_dir)
        sys.stdout.write(buf.getvalue())
        if a.out:
            a.out.write_text(buf.getvalue())
    sys.stdout.flush()


if __name__ == "__main__":
    main()
