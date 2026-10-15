"""Short perps into large scheduled token unlocks: does the token lag BTC over [T-10; T+2]?

Idea card: docs/ideas/IDEA-20261015-token-unlock-short-drift.md (section "Попередня реєстрація").

Stage 0 (before any price download): DefiLlama documented emission schedules (free static dataset
``defillama-datasets.llama.fi``; the ``api.llama.fi/emissions`` API is paid), the perp universe of Binance / Bybit /
Hyperliquid with listing dates, and the count of qualifying events of the main variant.

Network: exchanges only through the runtime providers (``rt.exchanges``, ``rt.hyperliquid._info``). DefiLlama has no
provider in the runtime, so this research script talks to it through ``core.http.Http`` (rate limit, retries) with
its own pacing; nothing in the MCP server uses it.

Usage:
    uv run python scripts/verify_token_unlocks.py stage0 --cache-dir ~/.cache/tradedesk-agents/token_unlock [--sample N]
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
    LegResult,
    beta,
    cluster_bootstrap_mean_ci,
    cluster_se,
    funding_sum,
    hedged_return,
    max_adverse_short,
    nearest_controls,
    short_vs_btc_net,
    simple_return,
)
from tradedesk.analytics.token_unlocks import (
    DAY_S,
    NONCIRCULATING,
    Section,
    UnlockEvent,
    circulating,
    parse_sections,
    placebo_days,
    select_events,
    slippage_for_volume,
    stopped_short_return,
    unlocks_by_day,
)
from tradedesk.core.http import Http
from tradedesk.server import build_server

HL, BIN, BYB = "hyperliquid", "binanceusdm", "bybit"
FIRST_T = datetime(2023, 1, 1, tzinfo=UTC)
LAST_CLOSE = datetime(2026, 10, 14, tzinfo=UTC)  # last complete UTC day used
ENTRY_OFF, EXIT_OFF = -10, 2
MIN_FRAC = 0.02
MIN_LISTED = 30  # perp listed at least this many days before T
OOS_START = datetime(2025, 1, 1, tzinfo=UTC)


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
            if any(x in str(e) for x in ("data_unavailable", "HTTP 404", "provider_tier", "Invalid symbol")):
                raise
            wait = 15 * (k + 1)
            print(f"  retry {what} in {wait}s after {type(e).__name__}: {str(e)[:120]}", flush=True)
            await asyncio.sleep(wait)
    raise RuntimeError(f"giving up on {what}")


class Fetcher:
    def __init__(self, rt: Any, cache: Path) -> None:
        self.rt, self.cache = rt, cache
        self.ds = Http("defillama_datasets", "https://defillama-datasets.llama.fi", rate=1.0, burst=2, timeout=90, retries=2)
        self.coins = Http("defillama_coins", "https://coins.llama.fi", rate=1.0, burst=2, timeout=60, retries=2)
        self.ds_pace = Pacer(50.0)
        self.hl_pace = Pacer(1000.0)

    async def aclose(self) -> None:
        await self.ds.aclose()
        await self.coins.aclose()

    async def cached(self, name: str, go: Any) -> Any:
        p = self.cache / name
        if p.exists():
            return rd(p)
        data = await go()
        wr(p, data)
        return data

    async def dsj(self, path: str) -> Any:
        await self.ds_pace.take()
        return (await retry(lambda: self.ds.get_json(path), f"datasets {path}")).data

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
            bn[m["base"]] = {
                "symbol": sym,
                "id": m.get("id"),
                "status": info.get("status"),
                "onboard": int(info.get("onboardDate") or 0),
                "delivery": int(info.get("deliveryDate") or 0),
            }
        ym = (await fx.rt.exchanges.markets(BYB)).data or {}
        by = {}
        for sym, m in ym.items():
            if m and m.get("swap") and m.get("linear") and sym.endswith("/USDT:USDT"):
                info = m.get("info") or {}
                by[m["base"]] = {"symbol": sym, "status": info.get("status"), "launch": int(info.get("launchTime") or 0)}
        return {"hl": hl, "binance": bn, "bybit": by, "binance_non_coin": sorted(tradfi), "fetched_at": int(time.time())}

    return await fx.cached("universe.json", go)


async def schedules(fx: Fetcher, sample: int) -> dict[str, Any]:
    """Index (slug -> gecko id, symbol, circulating supply) and the list of protocols with an emission schedule."""

    async def go() -> dict[str, Any]:
        idx = await fx.dsj("/emissionsIndex")
        lst = await fx.dsj("/emissionsProtocolsList")
        rows = {}
        for r in idx.get("data") or []:
            slug = r.get("protocolSlug")
            if not slug:
                continue
            sym = None
            tp = r.get("tokenPrice") or []
            if tp and isinstance(tp, list) and isinstance(tp[0], dict):
                sym = tp[0].get("symbol")
            rows[slug] = {
                "name": r.get("name"),
                "token": r.get("token"),
                "gecko_id": r.get("gecko_id"),
                "symbol": (sym or "").upper() or None,
                "circSupply": r.get("circSupply"),
                "mcap": r.get("mcap"),
                "supplyGeneratedAtSec": r.get("supplyGeneratedAtSec"),
            }
        return {"index": rows, "protocols": sorted(set(lst) | set(rows)), "fetched_at": int(time.time())}

    meta = await fx.cached("schedules_meta.json", go)
    slugs = meta["protocols"][:sample] if sample else meta["protocols"]
    t0 = time.perf_counter()
    for i, slug in enumerate(slugs):

        async def one(slug: str = slug) -> dict[str, Any]:
            try:
                return await fx.dsj(f"/emissions/{slug}")
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)[:200]}

        await fx.cached(f"emissions/{slug}.json", one)
        if (i + 1) % 25 == 0 or i + 1 == len(slugs):
            el = time.perf_counter() - t0
            print(f"  schedules {i + 1}/{len(slugs)} [{el:.0f}s, eta {el / (i + 1) * (len(slugs) - i - 1):.0f}s]", flush=True)
    return meta


def token_meta(slug: str, em: dict[str, Any], idx: dict[str, Any]) -> dict[str, Any]:
    """gecko id and symbol of a schedule: index first, then the document's own fields."""
    row = idx.get(slug) or {}
    md = em.get("metadata") or {}
    tok = row.get("token") or md.get("token") or ""
    gecko = row.get("gecko_id") or em.get("gecko_id") or (tok.split(":", 1)[1] if tok.startswith("coingecko:") else None)
    return {"slug": slug, "name": em.get("name") or row.get("name"), "gecko_id": gecko, "token": tok, "symbol": row.get("symbol")}


def venues_by_asset(uni: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    by_asset: dict[str, dict[str, list[str]]] = {}
    for v, names in ((HL, uni["hl"]), (BIN, uni["binance"]), (BYB, uni["bybit"])):
        for n in names:
            by_asset.setdefault(normalize_asset(n, v)[0], {}).setdefault(v, []).append(n)
    return by_asset


async def hl_life(fx: Fetcher, coin: str) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        start = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
        end = int(LAST_CLOSE.timestamp() * 1000) + 86_400_000
        rows = await fx.hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d", "startTime": start, "endTime": end}}, 20)
        ts = [int(r["t"]) // 1000 // DAY_S for r in rows or []]
        return {"first": min(ts) if ts else None, "last": max(ts) if ts else None, "n": len(ts)}

    return await fx.cached(f"hl_life/{coin}.json", go)


async def probe_removed(fx: Fetcher, sym: str) -> dict[str, Any]:
    """First and last daily bar of ``SYMUSDT`` perps on Binance / Bybit via raw public kline endpoints (not limited to
    symbols in today's metadata). Empty dict when neither venue has history."""

    async def go() -> dict[str, Any]:
        res: dict[str, Any] = {}
        start = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp() * 1000)
        try:
            f = await fx.rt.exchanges.public_get(
                BIN, "fapiPublicGetKlines", {"symbol": f"{sym}USDT", "interval": "1d", "startTime": start, "limit": 1500}
            )
            rows = f.data or []
            if rows:
                last = await fx.rt.exchanges.public_get(BIN, "fapiPublicGetKlines", {"symbol": f"{sym}USDT", "interval": "1d", "limit": 2})
                res[BIN] = {"first": int(rows[0][0]) // 1000 // DAY_S, "last": int((last.data or rows)[-1][0]) // 1000 // DAY_S}
        except Exception as e:  # noqa: BLE001
            res[f"{BIN}_error"] = str(e)[:120]
        try:
            f = await fx.rt.exchanges.public_get(
                BYB,
                "publicGetV5MarketKline",
                {"category": "linear", "symbol": f"{sym}USDT", "interval": "D", "start": start, "limit": 1000},
            )
            rows = ((f.data or {}).get("result") or {}).get("list") or []
            if rows:
                ds = [int(r[0]) // 1000 // DAY_S for r in rows]
                res[BYB] = {"first": min(ds), "last": max(ds)}
        except Exception as e:  # noqa: BLE001
            res[f"{BYB}_error"] = str(e)[:120]
        await asyncio.sleep(0.3)
        return res

    r = await fx.cached(f"removed_probe/{sym}.json", go)
    return {k: v for k, v in r.items() if not k.endswith("_error")}


def listing_window(perps: dict[str, list[str]], uni: dict[str, Any], lives: dict[str, Any]) -> tuple[int | None, int | None]:
    """Earliest listing day across venues and the last day a perp existed (today if still listed)."""
    firsts, lasts = [], []
    today = day_of(LAST_CLOSE)
    for n in perps.get(BIN, []):
        b = uni["binance"][n]
        if b["onboard"]:
            firsts.append(b["onboard"] // 1000 // DAY_S)
        lasts.append(today if b.get("status") == "TRADING" else (b.get("delivery") or 0) // 1000 // DAY_S or None)
    for n in perps.get(BYB, []):
        y = uni["bybit"][n]
        if y["launch"]:
            firsts.append(y["launch"] // 1000 // DAY_S)
        lasts.append(today)
    for n in perps.get(HL, []):
        life = lives.get(n) or {}
        if life.get("first") is not None:
            firsts.append(life["first"])
            lasts.append(life["last"] if uni["hl"][n]["delisted"] else today)
    return (min(firsts) if firsts else None), max((x for x in lasts if x is not None), default=None)


def load_sections(cache: Path, slug: str) -> list[Section]:
    p = cache / f"emissions/{slug}.json"
    if not p.exists():
        return []
    em = rd(p)
    return [] if "error" in em else parse_sections(em)


def schedule_events(
    secs: list[Section], *, min_frac: float, tradable: Any = None, use_cliff: bool = False, exclude: tuple[str, ...] = (NONCIRCULATING,)
) -> list[UnlockEvent]:
    first_t, last_t = day_of(FIRST_T), day_of(LAST_CLOSE) - EXIT_OFF
    ub = unlocks_by_day(secs, exclude=exclude, first_day=first_t - 1, last_day=last_t + 1)
    return select_events(
        ub,
        lambda d: circulating(secs, d, exclude=exclude),
        min_frac=min_frac,
        first_day=first_t,
        last_day=last_t,
        entry_offset=ENTRY_OFF,
        exit_offset=EXIT_OFF,
        tradable=tradable,
        use_cliff=use_cliff,
    )


async def stage0(cache: Path, sample: int) -> None:
    _, rt = build_server()
    fx = Fetcher(rt, cache)
    t0 = time.perf_counter()
    try:
        meta = await schedules(fx, sample)
        uni = await perp_universe(fx)
        by_asset = venues_by_asset(uni)
        idx = meta["index"]
        slugs = meta["protocols"][:sample] if sample else meta["protocols"]
        st = {"schedules": len(slugs), "error": 0, "no_sections": 0, "no_symbol": 0, "no_perp": 0, "with_perp": 0}
        tokens: dict[str, Any] = {}
        for slug in slugs:
            em = rd(cache / f"emissions/{slug}.json")
            if "error" in em:
                st["error"] += 1
                continue
            secs = parse_sections(em)
            if not secs:
                st["no_sections"] += 1
                continue
            tm = token_meta(slug, em, idx)
            if not tm["symbol"]:
                st["no_symbol"] += 1
                continue
            perps = by_asset.get(tm["symbol"])
            if not perps:
                st["no_perp"] += 1
                tokens[slug] = {**tm, "perps": {}}
                continue
            st["with_perp"] += 1
            tokens[slug] = {**tm, "perps": perps}
        hl_need = sorted({n for t in tokens.values() for n in t["perps"].get(HL, [])})
        lives = {}
        for n in hl_need:
            lives[n] = await hl_life(fx, n)
        print(f"schedules {st}; HL coins checked for first candle {len(hl_need)}; elapsed {time.perf_counter() - t0:.0f}s", flush=True)
        print(
            f"perps: HL {len(uni['hl'])} ({sum(v['delisted'] for v in uni['hl'].values())} delisted), Binance crypto "
            f"{len(uni['binance'])} (status: {count_by(v.get('status') for v in uni['binance'].values())}; non-COIN skipped "
            f"{len(uni['binance_non_coin'])}), Bybit {len(uni['bybit'])}",
            flush=True,
        )
        sym_count: dict[str, list[str]] = {}
        for slug, t in tokens.items():
            sym_count.setdefault(t["symbol"], []).append(slug)
        collisions = {s: v for s, v in sym_count.items() if len(v) > 1}
        print(f"symbol collisions among schedules: {collisions}", flush=True)
        out = []
        for slug, t in sorted(tokens.items()):
            secs = load_sections(cache, slug)
            first, last = listing_window(t["perps"], uni, lives) if t["perps"] else (None, None)
            t["first_listed"], t["last_listed"] = first, last

            def tradable(entry: int, tt: int, first: int | None = first, last: int | None = last) -> bool:
                return first is not None and last is not None and tt - first >= MIN_LISTED and last >= tt + EXIT_OFF

            for label, kw in [
                ("main", {"min_frac": MIN_FRAC, "tradable": tradable}),
                ("ge5", {"min_frac": 0.05, "tradable": tradable}),
                ("cliff2", {"min_frac": MIN_FRAC, "tradable": tradable, "use_cliff": True}),
                ("any_perp_status", {"min_frac": MIN_FRAC}),
            ]:
                for e in schedule_events(secs, **kw):
                    out.append({"variant": label, "slug": slug, "symbol": t["symbol"], "T": e.day, "frac": e.frac,
                                "cliff_frac": e.cliff_frac, "insider_frac": e.insider_frac, "has_perp": bool(t["perps"])})  # fmt: skip
        # survivorship: tokens with >= 2 % events but no perp in today's metadata -> try the raw kline endpoints of
        # Binance / Bybit for SYMBOLUSDT (perps removed from exchangeInfo / instruments-info still serve history)
        need_probe = sorted({e["slug"] for e in out if e["variant"] == "any_perp_status" and not e["has_perp"]})
        removed = {}
        for slug in need_probe:
            removed[slug] = await probe_removed(fx, tokens[slug]["symbol"])
        found = {s: r for s, r in removed.items() if r}
        print(f"tokens with >= 2 % events and no listed perp: {len(need_probe)}; removed perps found by raw klines: "
              + ", ".join(f"{tokens[s]['symbol']} {v}:{ymd(x['first'])}..{ymd(x['last'])}" for s, r in found.items() for v, x in r.items()))  # fmt: skip
        wr(cache / "removed_perps.json", removed)
        for slug, r in found.items():
            t = tokens[slug]
            first = min(x["first"] for x in r.values())
            last = max(x["last"] for x in r.values())

            def tradable2(entry: int, tt: int, first: int = first, last: int = last) -> bool:
                return tt - first >= MIN_LISTED and last >= tt + EXIT_OFF

            for e in schedule_events(load_sections(cache, slug), min_frac=MIN_FRAC, tradable=tradable2):
                out.append({"variant": "main_removed", "slug": slug, "symbol": t["symbol"], "T": e.day, "frac": e.frac,
                            "cliff_frac": e.cliff_frac, "insider_frac": e.insider_frac, "has_perp": True})  # fmt: skip
        n_rem = sum(e["variant"] == "main_removed" for e in out)
        print(f"main events on removed perps (added to the main count): {n_rem}")
        for e in out:
            if e["variant"] == "main_removed":
                e["variant"] = "main"
        wr(cache / "tokens.json", tokens)
        wr(cache / "events_stage0.json", out)
        main = [e for e in out if e["variant"] == "main"]
        print("\n## Stage 0 counts (T in 2023-01-01 .. 2026-10-12, schedule-implied circulating at T-10)\n")
        for v in ("main", "ge5", "cliff2"):
            ev = [e for e in out if e["variant"] == v]
            yrs = count_by(ymd(e["T"])[:4] for e in ev)
            print(f"{v:<8} events {len(ev):>4} on {len({e['slug'] for e in ev}):>3} tokens; by year {yrs}")
        allv = [e for e in out if e["variant"] == "any_perp_status"]
        print(
            f"all >= 2 % events regardless of perps: {len(allv)} on {len({e['slug'] for e in allv})} tokens; "
            f"of them on tokens with any matched perp today {sum(e['has_perp'] for e in allv)}"
        )
        fr = sorted(e["frac"] for e in main)
        if fr:
            print(
                f"main frac: median {statistics.median(fr):.3f}, p90 {fr[int(0.9 * len(fr))]:.3f}, max {fr[-1]:.2f}; "
                f"cliff share of amount (median) {statistics.median(e['cliff_frac'] / e['frac'] for e in main):.2f}"
            )
        print(
            "\nmain events per token: "
            + ", ".join(f"{k} {v}" for k, v in sorted(count_by(e["symbol"] for e in main).items(), key=lambda kv: -kv[1]))
        )
        print(f"\nstage0 done in {time.perf_counter() - t0:.0f}s", flush=True)
    finally:
        await fx.aclose()
        await rt.aclose()


REALIZED_WORDS = ("track", "transfer", "outflow", "on-chain", "onchain", "supply api", "realtime", "real-time", "real time")
REVISED_WORDS = ("extend", "change", "pause", "revis", "original", "updated", "reduc", "burn", "rebrand", "swap", "migrat", "delay")


def schedule_kind(em: dict[str, Any]) -> dict[str, Any]:
    """Flags that a schedule (or part of it) is reconstructed from realised on-chain flows rather than vesting terms
    (``realized_notes``, ``component_data``), or that the schedule was revised after publication (``revised_notes``)."""
    md = em.get("metadata") or {}
    notes = " ".join(str(n) for n in md.get("notes") or []).lower()
    hits = [w for w in REALIZED_WORDS if w in notes]
    return {
        "realized_notes": hits,
        "revised_notes": [w for w in REVISED_WORDS if w in notes],
        "component_data": "componentData" in em,
        "version": md.get("version") or em.get("version"),
        "sources": len(md.get("sources") or []),
        "notes": notes[:300],
    }


def audit(cache: Path) -> None:
    """Per token with main events: schedule kind flags, schedule-implied circulating today vs DefiLlama index."""
    tokens = rd(cache / "tokens.json")
    meta = rd(cache / "schedules_meta.json")
    ev = [e for e in rd(cache / "events_stage0.json") if e["variant"] == "main"]
    by_tok = count_by(e["slug"] for e in ev)
    today = day_of(LAST_CLOSE)
    rows = []
    for slug, n in sorted(by_tok.items(), key=lambda kv: -kv[1]):
        em = rd(cache / f"emissions/{slug}.json")
        k = schedule_kind(em)
        secs = parse_sections(em)
        circ = circulating(secs, today)
        idx = meta["index"].get(slug) or {}
        cs = idx.get("circSupply")
        ratio = circ / cs if cs else None
        flag = "REALIZED" if (k["realized_notes"] or k["component_data"]) else ""
        rows.append({"slug": slug, "n": n, **k, "circ_sched": circ, "circ_index": cs, "ratio": ratio})
        print(f"{tokens[slug]['symbol']:<8} {slug:<28} n={n:>3} {flag:<8} v={k['version']} src={k['sources']} comp={k['component_data']} "
              f"circ_sched/index={'—' if ratio is None else f'{ratio:.2f}'} notes={k['notes'][:110]!r}")  # fmt: skip
    wr(cache / "audit.json", rows)
    nr = [r for r in rows if r["realized_notes"] or r["component_data"]]
    print(
        f"\ntokens flagged realised: {len(nr)} with {sum(r['n'] for r in nr)} events; others {len(rows) - len(nr)} with {sum(r['n'] for r in rows) - sum(r['n'] for r in nr)}"
    )
    rv = [r for r in rows if r["revised_notes"] and not (r["realized_notes"] or r["component_data"])]
    print(f"of the others, flagged revised: {len(rv)} with {sum(r['n'] for r in rv)} events: "
          + ", ".join(f"{tokens[r['slug']]['symbol']}({','.join(r['revised_notes'])})" for r in rv))  # fmt: skip
    clean = {r["slug"] for r in rows if not (r["realized_notes"] or r["component_data"] or r["revised_notes"])}
    ce = [e for e in ev if e["slug"] in clean]
    print(f"clean schedules (main universe): {len(clean)} tokens, {len(ce)} events; by year {count_by(ymd(e['T'])[:4] for e in ce)}; "
          f">= 5 %: {sum(e['frac'] >= 0.05 for e in ce)}")  # fmt: skip
    wr(cache / "clean_slugs.json", sorted(clean))


SNAPSHOTS = [datetime(y, m, 1, tzinfo=UTC) for y in range(2022, 2027) for m in (1, 4, 7, 10) if datetime(y, m, 1, tzinfo=UTC) <= LAST_CLOSE]


async def adapter_snapshots(cache: Path) -> None:
    """Which emission adapter files existed in DefiLlama/emissions-adapters at quarterly snapshots (GitHub API,
    unauthenticated, 60 requests / hour: one commit lookup and one tree per snapshot). A lower bound for when a
    schedule was public on DefiLlama; it does not show later edits of an existing file."""
    gh = Http(
        "github", "https://api.github.com", rate=0.5, burst=1, timeout=60, retries=1, headers={"Accept": "application/vnd.github+json"}
    )
    out_p = cache / "adapter_snapshots.json"
    snaps: dict[str, Any] = rd(out_p) if out_p.exists() else {}
    try:
        for dt in SNAPSHOTS:
            key = f"{dt:%Y-%m-%d}"
            if key in snaps:
                continue
            try:
                c = (await gh.get_json("/repos/DefiLlama/emissions-adapters/commits", params={"until": dt.isoformat(), "per_page": 1})).data
                if not c:
                    snaps[key] = {"sha": None, "files": []}
                    continue
                sha = c[0]["sha"]
                tree = (await gh.get_json(f"/repos/DefiLlama/emissions-adapters/git/trees/{sha}", params={"recursive": "1"})).data
            except Exception as e:  # noqa: BLE001
                print(f"  github stopped at {key}: {str(e)[:160]}", flush=True)
                break
            files = sorted(x["path"] for x in tree.get("tree") or [] if x.get("type") == "blob" and x["path"].startswith("protocols/"))
            snaps[key] = {"sha": sha, "truncated": bool(tree.get("truncated")), "files": files}
            print(f"  {key} {sha[:8]} protocol files {len(files)} truncated={tree.get('truncated')}", flush=True)
            wr(out_p, snaps)
    finally:
        await gh.aclose()
        wr(out_p, snaps)


def adapter_first_seen(cache: Path) -> dict[str, str | None]:
    """slug -> first quarterly snapshot whose tree has ``protocols/<slug>.ts`` (or a ``protocols/<slug>/`` folder)."""
    snaps = rd(cache / "adapter_snapshots.json")
    out: dict[str, str | None] = {}
    for slug in rd(cache / "tokens.json"):
        first = None
        for key in sorted(snaps):
            fs = snaps[key]["files"]
            if any(f == f"protocols/{slug}.ts" or f.startswith(f"protocols/{slug}/") for f in fs):
                first = key
                break
        out[slug] = first
    return out


# ----------------------------------------------------------------------------- prices and funding

BARS_START = datetime(2021, 6, 1, tzinfo=UTC)
CHECK_DATES = [
    datetime(y, m, 1, tzinfo=UTC) for y, m in [(2023, 4), (2023, 10), (2024, 4), (2024, 10), (2025, 4), (2025, 10), (2026, 4), (2026, 10)]
]
BYR = "bybitraw"  # Bybit perps no longer in instruments-info, read through raw public endpoints
HOLDERS_CACHE = Path.home() / ".cache/tradedesk-agents/holders_revenue"


def bars_name(venue: str, name: str) -> str:
    return f"bars/{venue}_{name.replace('/', '-')}.json"


def legs_of(t: dict[str, Any], removed: dict[str, Any]) -> list[tuple[str, str]]:
    out = [(v, n) for v in (BIN, BYB, HL) for n in t["perps"].get(v, [])]
    r = removed.get(t["slug"]) or {}
    if BYB in r and not t["perps"].get(BYB):
        out.append((BYR, t["symbol"]))
    return out


async def cex_daily(fx: Fetcher, venue: str, sym: str) -> list[list[float]]:
    """Daily OHLCV ``[day, o, h, l, c, v_base]`` from BARS_START to LAST_CLOSE, walked page by page."""
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


async def bybit_raw_daily(fx: Fetcher, sym: str) -> list[list[float]]:
    """Daily bars of a Bybit linear perp that is no longer listed (``/v5/market/kline``, newest first, 1000 per page)."""
    rows: dict[int, list[float]] = {}
    end = (day_of(LAST_CLOSE) + 1) * DAY_S * 1000 - 1
    start = int(BARS_START.timestamp() * 1000)
    for _ in range(10):
        f = await retry(
            lambda e=end: fx.rt.exchanges.public_get(BYB, "publicGetV5MarketKline", {"category": "linear", "symbol": f"{sym}USDT", "interval": "D", "start": start, "end": e, "limit": 1000}),
            f"bybit raw {sym}",
        )  # fmt: skip
        lst = ((f.data or {}).get("result") or {}).get("list") or []
        if not lst:
            break
        for r in lst:
            d = int(r[0]) // 1000 // DAY_S
            rows[d] = [d, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
        first = min(int(r[0]) for r in lst)
        if len(lst) < 1000 or first <= start:
            break
        end = first - 1
        await asyncio.sleep(0.2)
    return [rows[d] for d in sorted(rows) if d <= day_of(LAST_CLOSE)]


async def hl_daily(fx: Fetcher, coin: str) -> list[list[float]]:
    start = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
    end = (day_of(LAST_CLOSE) + 1) * DAY_S * 1000
    rows = await fx.hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d", "startTime": start, "endTime": end}}, 20)
    out = {}
    for r in rows or []:
        d = int(r["t"]) // 1000 // DAY_S
        # volume stored in coins like the CEX rows (USD volume = v * close everywhere)
        out[d] = [d, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])]
    return [out[d] for d in sorted(out) if d <= day_of(LAST_CLOSE)]


def venue_symbol(uni: dict[str, Any], venue: str, name: str) -> str:
    return name if venue in (HL, BYR) else uni["binance" if venue == BIN else "bybit"][name]["symbol"]


async def fetch_prices(cache: Path, sample: int) -> None:
    _, rt = build_server()
    fx = Fetcher(rt, cache)
    t0 = time.perf_counter()
    try:
        uni = rd(cache / "universe.json")
        tokens = rd(cache / "tokens.json")
        removed = rd(cache / "removed_perps.json")
        jobs: list[tuple[str, str]] = [(BIN, "BTC")]
        for t in tokens.values():
            jobs.extend(legs_of(t, removed))
        jobs = sorted(set(jobs), key=lambda j: (j != (BIN, "BTC"), j))
        if sample:
            jobs = jobs[:sample]
        reused = 0
        print(f"candle jobs: {len(jobs)}", flush=True)
        for i, (v, n) in enumerate(jobs):
            p = cache / bars_name(v, n)
            hp = HOLDERS_CACHE / bars_name(v, n)
            if not p.exists() and v in (BIN, BYB) and hp.exists():
                # same fetcher, same LAST_CLOSE, earlier BARS_START (2020-06): reuse the holders-revenue download
                wr(p, rd(hp))
                reused += 1
            elif v == HL:
                await fx.cached(bars_name(v, n), lambda n=n: hl_daily(fx, n))
            elif v == BYR:
                await fx.cached(bars_name(v, n), lambda n=n: bybit_raw_daily(fx, n))
            else:

                async def go(v: str = v, n: str = n) -> list[list[float]]:
                    try:
                        return await cex_daily(fx, v, venue_symbol(uni, v, n))
                    except Exception as e:  # noqa: BLE001
                        print(f"  no bars for {v}:{n}: {str(e)[:120]}", flush=True)
                        return []

                await fx.cached(bars_name(v, n), go)
            if (i + 1) % 20 == 0 or i + 1 == len(jobs):
                el = time.perf_counter() - t0
                print(
                    f"  bars {i + 1}/{len(jobs)} (reused {reused}) [{el:.0f}s, eta {el / (i + 1) * (len(jobs) - i - 1):.0f}s]", flush=True
                )
        keys = sorted({f"coingecko:{t['gecko_id']}" for t in tokens.values() if t.get("gecko_id")})
        for dt in CHECK_DATES:

            async def go(dt: datetime = dt) -> dict[str, Any]:
                out: dict[str, Any] = {}
                for j in range(0, len(keys), 30):
                    chunk = ",".join(keys[j : j + 30])
                    await fx.ds_pace.take()
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

            got = await fx.cached(f"coins/{dt:%Y%m%d}.json", go)
            print(f"  coins {dt:%Y-%m-%d}: {len(got)} of {len(keys)}", flush=True)

        async def cg() -> dict[str, Any]:
            ids = sorted({t["gecko_id"] for t in tokens.values() if t.get("gecko_id")})
            out: dict[str, Any] = {}
            for j in range(0, len(ids), 200):
                r = await retry(
                    lambda c=",".join(ids[j : j + 200]): rt.coingecko.http.get_json("/coins/markets", params={"vs_currency": "usd", "ids": c, "per_page": 250, "page": 1}),
                    "coingecko markets",
                )  # fmt: skip
                for x in r.data or []:
                    out[x["id"]] = {
                        "circulating_supply": x.get("circulating_supply"),
                        "price": x.get("current_price"),
                        "market_cap": x.get("market_cap"),
                    }
                await asyncio.sleep(3)
            return {"fetched_at": int(time.time()), "coins": out}

        cgd = await fx.cached("coingecko_markets.json", cg)
        print(f"coingecko current circulating supply for {len(cgd['coins'])} ids", flush=True)
        print(f"prices fetched in {time.perf_counter() - t0:.0f}s", flush=True)
    finally:
        await fx.aclose()
        await rt.aclose()


async def fetch_funding(cache: Path, needed: dict[tuple[str, str], tuple[int, int]]) -> None:
    """Funding history per leg over [first needed entry - 1; last needed exit + 1] (days)."""
    _, rt = build_server()
    fx = Fetcher(rt, cache)
    uni = rd(cache / "universe.json")
    pace = Pacer(300.0)
    t0 = time.perf_counter()
    items = sorted(needed.items())
    try:
        for i, ((v, n), (d0, d1)) in enumerate(items):
            start, end = (d0 - 1) * DAY_S * 1000, (d1 + 2) * DAY_S * 1000
            name = f"funding/{v}_{n}.json"
            p, hp = cache / name, HOLDERS_CACHE / name
            if not p.exists() and hp.exists() and v in (BIN, BYB):
                h = rd(hp)
                rows = h["rows"]
                if h.get("complete") and rows and rows[0][0] * 1000 <= start + 3 * DAY_S * 1000:
                    wr(p, {"rows": rows, "complete": True, "from": start, "reused": True})
            if p.exists() and rd(p).get("from", 0) <= start:
                continue

            async def go(v: str = v, n: str = n, start: int = start, end: int = end) -> dict[str, Any]:
                out: dict[int, float] = {}
                complete = False
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
                elif v == BYR:
                    e = end
                    for _ in range(200):
                        f = await retry(
                            lambda e=e: fx.rt.exchanges.public_get(BYB, "publicGetV5MarketFundingHistory", {"category": "linear", "symbol": f"{n}USDT", "startTime": start, "endTime": e, "limit": 200}),
                            f"bybit raw funding {n}",
                        )  # fmt: skip
                        lst = ((f.data or {}).get("result") or {}).get("list") or []
                        for x in lst:
                            out[int(x["fundingRateTimestamp"]) // 1000] = float(x["fundingRate"])
                        if len(lst) < 200:
                            complete = True
                            break
                        e = min(int(x["fundingRateTimestamp"]) for x in lst) - 1
                        await asyncio.sleep(0.2)
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
                        if len(batch) < 1000 or max(int(x["timestamp"]) for x in batch) >= end:
                            complete = True
                            break
                        s = max(int(x["timestamp"]) for x in batch) + 1
                return {"rows": sorted(out.items()), "complete": complete, "from": start}

            if p.exists():
                p.unlink()
            await fx.cached(name, go)
            if (i + 1) % 10 == 0 or i + 1 == len(items):
                el = time.perf_counter() - t0
                print(f"  funding {i + 1}/{len(items)} [{el:.0f}s, eta {el / (i + 1) * (len(items) - i - 1):.0f}s]", flush=True)
    finally:
        await fx.aclose()
        await rt.aclose()


# ----------------------------------------------------------------------------- analysis

TAKER = {BIN: 0.0005, BYB: 0.00055, BYR: 0.00055, HL: 0.00045}
BTC_SIDE = 0.0005 + 0.0002
VENUE_ORDER = (BIN, BYB, BYR, HL)
BAND = (0.8, 1.25)
LIQ = {"3x": 1 / 3 - 0.025, "5x": 1 / 5 - 0.025}
STOP = 0.30
BLOCK_FRAC = 0.005  # schedule increments >= 0.5 % of circulating block placebo / control windows
K_CTRL = 3


class Bars:
    def __init__(self, rows: list[list[float]]) -> None:
        # zero-volume days did not trade (Binance serves flat candles for SETTLING perps)
        rows = [r for r in rows if r[5] > 0 and r[4] > 0]
        self.close = {int(r[0]): r[4] for r in rows}
        self.high = {int(r[0]): r[2] for r in rows if r[2] > 0}
        self.usd_vol = {int(r[0]): r[5] * r[4] for r in rows}
        self.first = min(self.close) if self.close else None
        self.last = max(self.close) if self.close else None

    def last_close_on_or_before(self, day: int, floor: int) -> int | None:
        for d in range(day, floor, -1):
            if d in self.close:
                return d
        return None

    def median_usd_vol(self, end: int, days: int = 30) -> float | None:
        xs = [self.usd_vol[d] for d in range(end - days + 1, end + 1) if d in self.usd_vol]
        return statistics.median(xs) if len(xs) >= 10 else None


@dataclass
class Leg:
    venue: str
    name: str
    mult: int
    bars: Bars


class Data:
    def __init__(self, cache: Path) -> None:
        self.cache = cache
        self.tokens = rd(cache / "tokens.json")
        self.removed = rd(cache / "removed_perps.json")
        self.btc = Bars(rd(cache / bars_name(BIN, "BTC")))
        self.coins = {f.stem: rd(f) for f in sorted((cache / "coins").glob("*.json"))}
        cg = cache / "coingecko_markets.json"
        self.cg = rd(cg)["coins"] if cg.exists() else {}
        self.kind: dict[str, dict[str, Any]] = {}
        self.secs: dict[str, list[Section]] = {}
        self.legs: dict[str, list[Leg]] = {}
        self.checks: list[str] = []
        self.funding: dict[tuple[str, str], list[tuple[int, float]] | None] = {}
        for slug, t in sorted(self.tokens.items()):
            p = cache / f"emissions/{slug}.json"
            if not p.exists():
                continue
            em = rd(p)
            if "error" in em:
                continue
            self.kind[slug] = schedule_kind(em)
            self.secs[slug] = parse_sections(em)
            ok = []
            for v, n in legs_of(t, self.removed):
                bp = cache / bars_name(v, n)
                if not bp.exists():
                    continue
                bars = Bars(rd(bp))
                mult = normalize_asset(n, HL if v == HL else BIN)[1]
                ratios = []
                for stem, cs in self.coins.items():
                    d = day_of(datetime.strptime(stem, "%Y%m%d").replace(tzinfo=UTC))
                    cp = (cs.get(f"coingecko:{t['gecko_id']}") or {}).get("price") if t.get("gecko_id") else None
                    pc = bars.close.get(d - 1)  # coins price at 00:00 UTC = close of the previous day
                    if cp and pc:
                        ratios.append(pc / mult / cp)
                med = statistics.median(ratios) if ratios else None
                good = med is not None and BAND[0] <= med <= BAND[1]
                self.checks.append(
                    f"{t['symbol']:<8} {slug:<26} {v}:{n} n={len(ratios)} median={med if med is None else round(med, 3)} -> {'ok' if good else 'DROP'}"
                )
                if good:
                    ok.append(Leg(v, n, mult, bars))
            if ok:
                self.legs[slug] = sorted(ok, key=lambda leg: VENUE_ORDER.index(leg.venue))

    def clean(self, slug: str) -> bool:
        k = self.kind.get(slug) or {}
        return bool(k) and not (k["realized_notes"] or k["component_data"] or k["revised_notes"])

    def leg_at(self, slug: str, entry: int, t: int) -> Leg | None:
        """First venue whose perp has a traded close on the entry day and a first candle <= T - MIN_LISTED."""
        for leg in self.legs.get(slug, []):
            b = leg.bars
            if b.first is not None and t - b.first >= MIN_LISTED and entry in b.close:
                return leg
        return None

    def fund(self, v: str, n: str) -> list[tuple[int, float]] | None:
        if (v, n) not in self.funding:
            p = self.cache / f"funding/{v}_{n}.json"
            self.funding[(v, n)] = [(int(a), float(b)) for a, b in rd(p)["rows"]] if p.exists() else None
        return self.funding[(v, n)]

    def circ(self, slug: str, day: int, *, all_sections: bool = False) -> float:
        return circulating(self.secs[slug], day, exclude=() if all_sections else (NONCIRCULATING,))

    def blocked_days(self, slug: str) -> list[int]:
        """Days with a scheduled increment >= BLOCK_FRAC of circulating (any unlock that should not sit in a control window)."""
        secs = self.secs[slug]
        ub = unlocks_by_day(secs, first_day=day_of(FIRST_T) - 60, last_day=day_of(LAST_CLOSE) + 20)
        out = []
        for d, u in ub.items():
            c = circulating(secs, d - 1)
            if c > 0 and u.amount / c >= BLOCK_FRAC:
                out.append(d)
        return out


@dataclass(frozen=True)
class Variant:
    label: str
    entry_off: int = ENTRY_OFF
    exit_off: int = EXIT_OFF
    min_frac: float = MIN_FRAC
    use_cliff: bool = False
    universe: str = "clean"  # clean | all | cg
    all_sections: bool = False
    insider_min: float = 0.0  # insiders + privateSale share of the unlock amount


MAIN = Variant("main")


@dataclass
class Trade:
    slug: str
    t: int
    entry: int
    exit: int
    leg: Leg
    beta: float
    ar: float
    r_tok: float
    r_btc: float
    f_tok: float
    f_btc: float
    side: float
    net: float
    gross: float
    net_nh: float
    mae: float | None
    net_stop: float | None
    truncated: bool
    funding_missing: bool
    frac: float = float("nan")
    vol: float | None = None


def trade(data: Data, slug: str, t: int, entry: int, exit_: int, *, cost_mult: float = 1.0, need: dict | None = None) -> Trade | None:
    leg = data.leg_at(slug, entry, t)
    if leg is None:
        return None
    b = leg.bars
    be = beta(b.close, data.btc.close, min(t - 20, entry), days=60)  # 60 daily returns ending T-20
    if be is None:
        return None
    ex = b.last_close_on_or_before(exit_, entry)
    if ex is None or ex == entry or ex not in data.btc.close or entry not in data.btc.close:
        return None
    ar = hedged_return(b.close, data.btc.close, entry, ex, be)
    r_tok = simple_return(b.close, entry, ex)
    r_btc = simple_return(data.btc.close, entry, ex)
    if ar is None or r_tok is None or r_btc is None:
        return None
    if need is not None:
        for key in ((leg.venue, leg.name), (BIN, "BTC")):
            lo, hi = need.get(key, (entry, ex))
            need[key] = (min(lo, entry), max(hi, ex))
    fr = data.fund(leg.venue, leg.name)
    f_tok = funding_sum(fr, (entry + 1) * DAY_S, (ex + 1) * DAY_S) if fr is not None else 0.0
    fb = data.fund(BIN, "BTC")
    f_btc = funding_sum(fb, (entry + 1) * DAY_S, (ex + 1) * DAY_S) if fb else 0.0
    vol = b.median_usd_vol(entry)
    side = (TAKER[leg.venue] + slippage_for_volume(vol)) * cost_mult
    gross, net = short_vs_btc_net(LegResult(be, ar, f_tok, side), btc_funding=f_btc, btc_side_cost=BTC_SIDE * cost_mult)
    net_nh = -r_tok - 2 * side + f_tok
    mae = max_adverse_short(b.high, b.close[entry], entry, ex)
    st = stopped_short_return(b.high, b.close, entry, ex, STOP)
    net_stop = None
    if st is not None:
        r_s, d_s = st
        rb_s = simple_return(data.btc.close, entry, d_s) if d_s in data.btc.close else r_btc
        f_tok_s = funding_sum(fr, (entry + 1) * DAY_S, (d_s + 1) * DAY_S) if fr is not None else 0.0
        f_btc_s = funding_sum(fb, (entry + 1) * DAY_S, (d_s + 1) * DAY_S) if fb else 0.0
        net_stop = -(r_s - be * (rb_s or 0.0)) - 2 * side - abs(be) * 2 * BTC_SIDE * cost_mult + f_tok_s - be * f_btc_s
    return Trade(
        slug,
        t,
        entry,
        ex,
        leg,
        be,
        ar,
        r_tok,
        r_btc,
        f_tok,
        f_btc,
        side,
        net,
        gross,
        net_nh,
        mae,
        net_stop,
        ex < exit_,
        fr is None,
        vol=vol,
    )


def events_for(data: Data, var: Variant) -> dict[str, list[UnlockEvent]]:
    first_t, last_t = day_of(FIRST_T), day_of(LAST_CLOSE) - max(var.exit_off, 0)
    out = {}
    for slug, secs in data.secs.items():
        if slug not in data.legs:
            continue
        if var.universe == "clean" and not data.clean(slug):
            continue
        if var.universe == "cg" and not (data.clean(slug) and cg_consistent(data, slug)):
            continue
        ex = () if var.all_sections else (NONCIRCULATING,)
        ub = unlocks_by_day(secs, exclude=ex, first_day=first_t - 1, last_day=last_t + 1)
        evs = select_events(
            ub,
            lambda d, secs=secs, ex=ex: circulating(secs, d, exclude=ex),
            min_frac=var.min_frac,
            first_day=first_t,
            last_day=last_t,
            entry_offset=var.entry_off,
            exit_offset=var.exit_off,
            tradable=lambda e, t, slug=slug: data.leg_at(slug, e, t) is not None,
            use_cliff=var.use_cliff,
        )
        if var.insider_min > 0:
            evs = [e for e in evs if e.insider_frac >= var.insider_min * e.frac]
        if evs:
            out[slug] = evs
    return out


def cg_consistent(data: Data, slug: str) -> bool:
    t = data.tokens[slug]
    cs = (data.cg.get(t.get("gecko_id") or "") or {}).get("circulating_supply")
    sc = data.circ(slug, day_of(LAST_CLOSE))
    return bool(cs) and sc > 0 and 1 / 1.5 <= sc / cs <= 1.5


def run(data: Data, var: Variant, *, cost_mult: float = 1.0, need: dict | None = None) -> list[Trade]:
    rows = []
    for slug, evs in events_for(data, var).items():
        for e in evs:
            tr = trade(data, slug, e.day, e.entry, e.exit, cost_mult=cost_mult, need=need)
            if tr is not None:
                tr.frac = e.frac
                rows.append(tr)
    return sorted(rows, key=lambda r: (r.t, r.slug))


def placebo(data: Data, main_events: dict[str, list[UnlockEvent]], *, need: dict | None = None) -> list[Trade]:
    rows = []
    last_t = day_of(LAST_CLOSE) - EXIT_OFF
    for slug in main_events:
        first = min((leg.bars.first for leg in data.legs[slug] if leg.bars.first is not None), default=None)
        if first is None:
            continue
        cands = range(max(first + 40, day_of(FIRST_T)), last_t + 1)
        for d in placebo_days(cands, data.blocked_days(slug), entry_offset=ENTRY_OFF, exit_offset=EXIT_OFF, step=13):
            tr = trade(data, slug, d, d + ENTRY_OFF, d + EXIT_OFF, need=need)
            if tr is not None:
                rows.append(tr)
    return rows


def cap_controls(data: Data, rows: list[Trade], *, need: dict | None = None) -> list[tuple[Trade, list[Trade]]]:
    """Per event: K_CTRL clean tokens without a >= 0.5 % scheduled increment in [T-10; T+2], nearest log market cap."""
    blocked = {s: data.blocked_days(s) for s in data.secs if s in data.legs and data.clean(s)}
    out = []
    for r in rows:
        cap_e = mcap(data, r.slug, r.entry, r.leg)
        if cap_e is None:
            continue
        pool = {}
        for s, bl in blocked.items():
            if s == r.slug or data.tokens[s]["symbol"] == data.tokens[r.slug]["symbol"]:
                continue
            if any(r.entry <= b <= r.exit for b in bl):
                continue
            leg = data.leg_at(s, r.entry, r.t)
            if leg is None:
                continue
            c = mcap(data, s, r.entry, leg)
            if c is not None:
                pool[s] = math.log(c)
        ctrl = nearest_controls(math.log(cap_e), pool, K_CTRL)
        trs = [x for s in ctrl if (x := trade(data, s, r.t, r.entry, r.exit, need=need)) is not None]
        if len(trs) == K_CTRL:
            out.append((r, trs))
    return out


def mcap(data: Data, slug: str, day: int, leg: Leg) -> float | None:
    p = leg.bars.close.get(day)
    c = data.circ(slug, day)
    return p / leg.mult * c if p and c > 0 else None


def summarize(xs_rows: list[tuple[float, Trade]]) -> dict[str, Any]:
    xs = [x for x, _ in xs_rows]
    rr = [r for _, r in xs_rows]
    if len(xs) < 3:
        return {"n": len(xs)}
    se = cluster_se(xs, [[r.slug for r in rr], [r.t // 30 for r in rr]])
    m = statistics.fmean(xs)
    return {
        "n": len(xs),
        "tokens": len({r.slug for r in rr}),
        "mean": m,
        "median": statistics.median(xs),
        "se": se,
        "t": m / se if se > 0 else float("nan"),
        "ci_token": cluster_bootstrap_mean_ci(xs, [r.slug for r in rr]),
        "hit": sum(x > 0 for x in xs) / len(xs),
    }


def S(rows: list[Trade], attr: str = "net") -> dict[str, Any]:
    return summarize([(getattr(r, attr), r) for r in rows if getattr(r, attr) is not None])


def fmt(s: dict[str, Any]) -> str:
    if "mean" not in s:
        return f"n={s['n']} (too few)"
    ci = s["ci_token"]
    cis = f"[{ci[0] * 100:+.2f}, {ci[1] * 100:+.2f}]" if ci else "—"
    return f"n={s['n']:>4} tok={s['tokens']:>3} mean={s['mean'] * 100:+6.2f}pp median={s['median'] * 100:+6.2f}pp t2way={s['t']:+5.2f} CI_tok={cis} hit={s['hit']:.0%}"


EXPLORATORY = [
    Variant("entry T-5", entry_off=-5),
    Variant("entry T-1", entry_off=-1),
    Variant("exit T+0", exit_off=0),
    Variant("exit T+5", exit_off=5),
    Variant(">= 5 %", min_frac=0.05),
    Variant("pre-event [T-10; T-1]", exit_off=-1),
    Variant("post-event [T-1; T+5] (entry at the close before T)", entry_off=-1, exit_off=5),
    Variant("cliff part >= 2 %", use_cliff=True),
    Variant("insiders + investors >= 50 % of the unlock", insider_min=0.5),
    Variant("all 115 tokens (flagged schedules too)", universe="all"),
    Variant("denominator incl. noncirculating", all_sections=True),
    Variant("schedule circ today within x1.5 of CoinGecko", universe="cg"),
]


def collect_needed(cache: Path) -> dict[tuple[str, str], tuple[int, int]]:
    data = Data(cache)
    need: dict[tuple[str, str], tuple[int, int]] = {}
    rows = run(data, MAIN, need=need)
    placebo(data, events_for(data, MAIN), need=need)
    cap_controls(data, rows, need=need)
    for v in EXPLORATORY:
        run(data, v, need=need)
    return need


def sanity(cache: Path) -> None:
    data = Data(cache)
    drops = [c for c in data.checks if c.endswith("DROP")]
    print(
        f"legs checked {len(data.checks)}, kept {len(data.checks) - len(drops)}, dropped {len(drops)}; tokens with a kept leg {len(data.legs)}"
    )
    for c in data.checks:
        print("  " + c)
    d = day_of(datetime(2024, 12, 31, tzinfo=UTC))
    print(f"BTC Binance close 2024-12-31: {data.btc.close.get(d)} (known ~93,500); first {ymd(data.btc.first)} last {ymd(data.btc.last)}")
    # known unlocks: ARB 2024-03-16 cliff 1.11 bn (team + investors); APT ~11.3 m on the 12th of each month
    for slug, day, what in [
        ("arbitrum-foundation", datetime(2024, 3, 16, tzinfo=UTC), "ARB 1.11 bn"),
        ("aptos", datetime(2024, 6, 12, tzinfo=UTC), "APT ~11.3 m"),
    ]:
        if slug in data.secs:
            ub = unlocks_by_day(data.secs[slug], first_day=day_of(day) - 1, last_day=day_of(day) + 1)
            u = ub.get(day_of(day))
            c = data.circ(slug, day_of(day) - 10)
            print(
                f"known unlock {what}: schedule amount {u.amount if u else 0:,.0f} (cliff {u.cliff if u else 0:,.0f}), circ(T-10) {c:,.0f}"
            )
    gaps = []
    for slug, legs in data.legs.items():
        for leg in legs:
            b = leg.bars
            missing = [x for x in range(b.first, b.last + 1) if x not in b.close]
            if missing:
                gaps.append(f"{data.tokens[slug]['symbol']} {leg.venue}:{leg.name} {len(missing)} missing days of {b.last - b.first + 1}")
    print(f"legs with gaps: {len(gaps)}")
    for g in gaps[:40]:
        print("  " + g)
    ratios = []
    for slug in data.secs:
        t = data.tokens[slug]
        cs = (data.cg.get(t.get("gecko_id") or "") or {}).get("circulating_supply")
        sc = data.circ(slug, day_of(LAST_CLOSE))
        if cs and sc > 0 and slug in data.legs and data.clean(slug):
            ratios.append((sc / cs, t["symbol"]))
    ratios.sort()
    print(f"schedule circulating today / CoinGecko circulating (clean tokens with a leg, n={len(ratios)}): "
          + ", ".join(f"{s} {x:.2f}" for x, s in ratios))  # fmt: skip
    evs = events_for(data, MAIN)
    print(f"main events after the price check: {sum(len(v) for v in evs.values())} on {len(evs)} tokens")


def report(cache: Path) -> None:
    data = Data(cache)
    drops = [c for c in data.checks if c.endswith("DROP")]
    print(f"## Ticker / price check\n\nlegs checked {len(data.checks)}, kept {len(data.checks) - len(drops)}, dropped {len(drops)}")
    for c in drops:
        print("  " + c)
    evs = events_for(data, MAIN)
    rows = run(data, MAIN)
    n_ev = sum(len(v) for v in evs.values())
    print(
        f"\n## Main variant (short token + beta BTC, >= 2 %, entry close T-10, exit close T+2)\n\n"
        f"events after the price check: {n_ev} on {len(evs)} tokens; evaluated (beta, closes): {len(rows)}; "
        f"truncated exits {sum(r.truncated for r in rows)}; funding missing {sum(r.funding_missing for r in rows)}; "
        f"by venue {count_by(r.leg.venue for r in rows)}"
    )
    s_main = S(rows)
    print("H1 net : " + fmt(s_main))
    print("   gross: " + fmt(S(rows, "gross")))
    print(
        f"   mean round-trip cost {statistics.fmean(2 * r.side + abs(r.beta) * 2 * BTC_SIDE for r in rows) * 100:.2f}pp; "
        f"token funding received by the short: mean {statistics.fmean(r.f_tok for r in rows) * 100:+.3f}pp, "
        f"share of trades where the short paid {sum(r.f_tok < 0 for r in rows) / len(rows):.0%}; mean beta {statistics.fmean(r.beta for r in rows):.2f}"
    )
    prow = placebo(data, evs)
    s_pl = S(prow)
    print("\nplacebo (same tokens, windows clear of any >= 0.5 % unlock): " + fmt(s_pl))
    ids = {k: i for i, k in enumerate(sorted({r.slug for r in rows + prow}))}
    diff = cluster_bootstrap_diff([r.net for r in rows], [ids[r.slug] for r in rows], [r.net for r in prow], [ids[r.slug] for r in prow])
    if diff["diff"] is not None:
        ci = diff["ci"]
        print(f"event - placebo: {diff['diff'] * 100:+.2f}pp, token-cluster bootstrap 95% CI [{ci[0] * 100:+.2f}, {ci[1] * 100:+.2f}]")
    cc = cap_controls(data, rows)
    if cc:
        ex = [(r.net - statistics.fmean(c.net for c in cs), r) for r, cs in cc]
        print(f"cap-matched control (diagnostic): event net - mean of {K_CTRL} controls' short net: " + fmt(summarize(ex)))
        print("   controls alone (short net): " + fmt(summarize([(statistics.fmean(c.net for c in cs), r) for r, cs in cc])))
    oos = [r for r in rows if r.t >= day_of(OOS_START)]
    ins = [r for r in rows if r.t < day_of(OOS_START)]
    print("\nOOS 2025-01-01..: " + fmt(S(oos)))
    print("2023-2024       : " + fmt(S(ins)))
    for y in sorted({ymd(r.t)[:4] for r in rows}):
        print(f"  {y}: " + fmt(S([r for r in rows if ymd(r.t)[:4] == y])))
    top = sorted(rows, key=lambda r: -r.net)
    print("without top-5 by net: " + fmt(S(top[5:])))
    print("without bottom-5 by net: " + fmt(S(top[:-5])))
    print("costs x2: " + fmt(S(run(data, MAIN, cost_mult=2.0))))
    tok_means = {}
    for r in rows:
        tok_means.setdefault(r.slug, []).append(r.net)
    tm = [statistics.fmean(v) for v in tok_means.values()]
    print(
        f"token-level means (each token weight 1): n={len(tm)} mean {statistics.fmean(tm) * 100:+.2f}pp median {statistics.median(tm) * 100:+.2f}pp share > 0 {sum(x > 0 for x in tm) / len(tm):.0%}"
    )
    print(
        "per token (n, mean net pp): "
        + "; ".join(
            f"{data.tokens[k]['symbol']} {len(v)} {statistics.fmean(v) * 100:+.1f}"
            for k, v in sorted(tok_means.items(), key=lambda kv: -len(kv[1]))
        )
    )
    # tails, stop, capacity, capital
    maes = sorted(r.mae for r in rows if r.mae is not None)
    print(
        f"\nshort-leg MAE (daily highs): median {statistics.median(maes) * 100:.1f}%, p90 {maes[int(0.9 * len(maes))] * 100:.1f}%, max {maes[-1] * 100:.1f}%; "
        + ", ".join(
            f"share liquidated at {k} (MAE >= {lv * 100:.1f}%): {sum(m >= lv for m in maes) / len(maes):.1%}" for k, lv in LIQ.items()
        )
    )
    print(f"with a +{STOP:.0%} stop: " + fmt(S(rows, "net_stop")) + f"; stopped {sum(r.mae is not None and r.mae >= STOP for r in rows)}")
    days: dict[int, int] = {}
    for r in rows:
        for d in range(r.entry, r.exit):
            days[d] = days.get(d, 0) + 1
    vols = sorted(r.vol for r in rows if r.vol)
    print(
        f"max simultaneous positions {max(days.values())}; median when any open {statistics.median(days.values()):.0f}; "
        f"perp median daily volume at entry: median ${statistics.median(vols) / 1e6:.1f}m, p10 ${vols[int(0.1 * len(vols))] / 1e6:.1f}m"
    )
    if "mean" in s_main:
        bn = statistics.fmean(abs(r.beta) for r in rows)
        print(
            f"on capital at 3x on both legs (capital = (1 + |beta|)/3 of token notional, |beta|~{bn:.2f}): mean net {s_main['mean'] * 3 / (1 + bn) * 100:+.2f}% per 12-day trade"
        )
    print("\n## Verdict inputs\n")
    pl_ci = diff["ci"] if diff["diff"] is not None else None
    s_oos = S(oos)
    print(
        f"n={s_main.get('n')} mean={s_main.get('mean', float('nan')) * 100:+.2f}pp t={s_main.get('t', float('nan')):+.2f} "
        f"CI_tok={s_main.get('ci_token')} diff_placebo_CI={pl_ci} OOS_mean={s_oos.get('mean', float('nan')) * 100:+.2f}pp"
    )
    print(f"\n## Exploratory variants ({len(EXPLORATORY)} + no-hedge = {len(EXPLORATORY) + 1} attempts; do not change the verdict)\n")
    print(f"{'no hedge (short token outright)':<52} " + fmt(S(rows, "net_nh")))
    for v in EXPLORATORY:
        print(f"{v.label:<52} " + fmt(S(run(data, v))))
    print("\n## Top-20 events by |net| (manual review)\n")
    for r in sorted(rows, key=lambda r: -abs(r.net))[:20]:
        print(
            f"  {ymd(r.t)} {data.tokens[r.slug]['symbol']:<8} {r.leg.venue}:{r.leg.name:<10} frac {r.frac:6.3f} beta {r.beta:+.2f} "
            f"r_tok {r.r_tok * 100:+6.1f} r_btc {r.r_btc * 100:+6.1f} f_tok {r.f_tok * 100:+.2f} net {r.net * 100:+6.1f}pp mae {(r.mae or 0) * 100:5.1f}"
        )
    print("\n## All main events\n")
    for r in rows:
        print(
            f"  {ymd(r.t)} {data.tokens[r.slug]['symbol']:<8} {r.leg.venue[:3]} frac {r.frac:6.3f} beta {r.beta:+.2f} AR {r.ar * 100:+6.1f} "
            f"f {r.f_tok * 100:+.2f} net {r.net * 100:+6.1f}{' T' if r.truncated else ''}{' F?' if r.funding_missing else ''}"
        )
    wr(cache / "rows_main.json", [{"slug": r.slug, "T": r.t, "net": r.net, "gross": r.gross, "frac": r.frac} for r in rows])


def peek(cache: Path, slug: str, start: str, end: str) -> None:
    """Scheduled increments per day and section of one token between two dates (manual review of events)."""
    secs = load_sections(cache, slug)
    d0 = day_of(datetime.fromisoformat(start).replace(tzinfo=UTC))
    d1 = day_of(datetime.fromisoformat(end).replace(tzinfo=UTC))
    print(
        f"{slug}: sections "
        + "; ".join(f"{s.label} [{s.category}] {ymd(s.points[0][0])}..{ymd(s.points[-1][0])} n={len(s.points)}" for s in secs)
    )
    for d in range(d0, d1 + 1):
        parts = [(s.label, s.category, s.at(d) - s.at(d - 1)) for s in secs if s.at(d) - s.at(d - 1) != 0]
        if parts:
            c = circulating(secs, d - 1)
            print(f"  {ymd(d)} circ(d-1) {c:,.0f}: " + "; ".join(f"{lab} [{cat}] {x:+,.0f}" for lab, cat, x in parts))


def count_by(xs: Any) -> dict[Any, int]:
    out: dict[Any, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: str(kv[0])))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["stage0", "audit", "adapters", "fetch", "funding", "sanity", "report", "peek"])
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--slug", default="")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/token_unlock")
    ap.add_argument("--log", type=Path, default=None, help="write stdout (progress lines) to this file instead")
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.log:
        sys.stdout = open(a.log, "w", buffering=1)  # noqa: SIM115
    if a.cmd == "stage0":
        asyncio.run(stage0(a.cache_dir, a.sample))
    elif a.cmd == "audit":
        audit(a.cache_dir)
    elif a.cmd == "fetch":
        asyncio.run(fetch_prices(a.cache_dir, a.sample))
    elif a.cmd == "funding":
        need = collect_needed(a.cache_dir)
        print(f"funding legs needed: {len(need)}", flush=True)
        items = dict(sorted(need.items())[: a.sample]) if a.sample else need
        asyncio.run(fetch_funding(a.cache_dir, items))
        print("funding done", flush=True)
    elif a.cmd == "peek":
        peek(a.cache_dir, a.slug, a.start, a.end)
    elif a.cmd == "sanity":
        sanity(a.cache_dir)
    elif a.cmd == "report":
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report(a.cache_dir)
        sys.stdout.write(buf.getvalue())
        if a.out:
            a.out.write_text(buf.getvalue())
    elif a.cmd == "adapters":
        asyncio.run(adapter_snapshots(a.cache_dir))
        fs = adapter_first_seen(a.cache_dir)
        print(f"slugs with an adapter file found: {sum(v is not None for v in fs.values())} of {len(fs)}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
