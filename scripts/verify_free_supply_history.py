"""Stage 0: is there a FREE point-in-time history of token supply good enough for the net-supply card?

Data feasibility only (no returns). Pre-registration: docs/ideas/IDEA-20261015-holders-revenue-yield-net-supply.md,
section "Стадія 0 supply-даних". Report: docs/verification/2026-10-23-free-supply-history.md.

Sources: (A) CoinMarketCap weekly historical snapshots (the JSON behind coinmarketcap.com/historical/YYYYMMDD/);
(B) Coin Metrics community API v4 and its GitHub CSV mirror. Point-in-time is checked against Wayback Machine copies.

Modes (each caches every response as a file under --cache-dir, reruns are free):
  probe    one request per endpoint, timing and shape
  cmc      quarterly sample dates + overlap days with our supply_snapshot
  cm       Coin Metrics community catalog + SplyCur for the perp tokens it lists
  wayback  archived copies of CMC historical pages -> revision test
  terms    terms-of-use pages, keyword lines
  report   C1..C5 tables
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import re
import resource
import sqlite3
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from tradedesk.analytics.supply_history import (
    coverage_fraction,
    normalize_perp_base,
    pit_verdict,
    quarter_sample_dates,
    rel_gap,
    resolve_symbol,
    summarize_gaps,
)
from tradedesk.core.http import Http

UNIVERSE = Path.home() / ".cache/tradedesk-agents/token_unlock/universe.json"
DB = "file:/var/lib/tradedesk-mcp/tradedesk.sqlite?mode=ro"
FIRST_YEAR, LAST = 2021, date(2026, 10, 18)
UA = {"User-Agent": "tradedesk-research/0.1 (personal research; low rate)"}


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%H:%M:%S} rss={rss_mb():.0f}MB] {msg}", flush=True)


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


def wr_bytes(p: Path, b: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(gzip.compress(b))
    tmp.replace(p)


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def rd_bytes(p: Path) -> bytes:
    return gzip.decompress(p.read_bytes())


class Src:
    def __init__(self) -> None:
        self.cmc_api = Http("cmc_data_api", "https://api.coinmarketcap.com", rate=0.5, burst=1, timeout=60, headers=UA)
        self.cmc_web = Http("cmc_web", "https://coinmarketcap.com", rate=0.3, burst=1, timeout=60, headers=UA)
        self.cm = Http("coinmetrics_community", "https://community-api.coinmetrics.io", rate=1.5, burst=1, timeout=60, headers=UA)
        self.cm_web = Http("coinmetrics_web", "https://coinmetrics.io", rate=0.5, burst=1, timeout=60, headers=UA)
        self.gh = Http("github_api", "https://api.github.com", rate=0.5, burst=1, timeout=60, headers=UA)
        self.wb = Http("wayback", "https://web.archive.org", rate=0.25, burst=1, timeout=90, retries=3, headers=UA)

    async def aclose(self) -> None:
        for h in (self.cmc_api, self.cmc_web, self.cm, self.cm_web, self.gh, self.wb):
            await h.aclose()


async def cached_bytes(cache: Path, name: str, http: Http, path: str, params: dict[str, Any] | None = None) -> bytes | None:
    p = cache / name
    if p.exists():
        return rd_bytes(p)
    t0 = time.monotonic()
    try:
        b = (await http.get_bytes(path, params=params)).data
    except Exception as e:  # noqa: BLE001
        log(f"FAIL {http.provider} {path} {params or ''}: {type(e).__name__} {str(e)[:160]}")
        return None
    wr_bytes(p, b)
    log(f"got {http.provider} {path} {params or ''}: {len(b) / 1e6:.2f} MB in {time.monotonic() - t0:.1f}s")
    return b


# ----------------------------------------------------------------------------- CMC


def cmc_params(d: date) -> dict[str, Any]:
    return {"date": d.isoformat(), "start": 1, "limit": 5000, "convertId": 2781}


def cmc_compact(raw: bytes) -> dict[str, list[dict[str, Any]]]:
    """symbol -> rows {id, slug, circ, total, max, price, mcap, rank, last_updated} of one historical listing."""
    js = json.loads(raw)
    out: dict[str, list[dict[str, Any]]] = {}
    for c in js.get("data") or []:
        q = (c.get("quotes") or [{}])[0].get("quote") or (c.get("quotes") or [{}])[0]
        row = {
            "id": c.get("id"),
            "slug": c.get("slug"),
            "circ": c.get("circulatingSupply"),
            "total": c.get("totalSupply"),
            "max": c.get("maxSupply"),
            "price": q.get("price"),
            "mcap": q.get("marketCap"),
            "rank": c.get("cmcRank"),
            "last_updated": c.get("lastUpdated"),
        }
        out.setdefault(str(c.get("symbol") or "").upper(), []).append(row)
    return out


async def cmc_date(src: Src, cache: Path, d: date) -> dict[str, Any] | None:
    """All coins of the CMC historical listing of day ``d`` (paginated by 5000), compacted per symbol."""
    cp = cache / f"cmc/compact2_{d.isoformat()}.json"
    if cp.exists():
        return rd(cp)
    comp: dict[str, list[dict[str, Any]]] = {}
    start = 1
    while True:
        name = f"cmc/raw_{d.isoformat()}.json.gz" if start == 1 else f"cmc/raw_{d.isoformat()}_s{start}.json.gz"
        params = cmc_params(d) | {"start": start}
        raw = await cached_bytes(cache, name, src.cmc_api, "/data-api/v3/cryptocurrency/listings/historical", params)
        if raw is None:
            return None
        try:
            page = cmc_compact(raw)
        except (ValueError, AttributeError, IndexError) as e:
            log(f"cmc {d}: unparsable ({e}); head={raw[:200]!r}")
            return None
        n = sum(len(v) for v in page.values())
        for k, v in page.items():
            comp.setdefault(k, []).extend(v)
        if n < 5000:
            break
        start += 5000
    wr(cp, comp)
    return comp


def cmc_by_id(comp: dict[str, list[dict[str, Any]]]) -> dict[int, dict[str, Any]]:
    return {int(r["id"]): r | {"symbol": s} for s, rows in comp.items() for r in rows if r.get("id") is not None}


# ----------------------------------------------------------------------------- local data


def perp_tokens(exclude_tradfi: bool = True) -> dict[str, dict[str, Any]]:
    """token symbol -> {first listing day, venues, perp bases}. Binance COIN perps + Bybit linear USDT perps
    (Bybit stock/ETF/commodity/forex perps dropped unless ``exclude_tradfi`` is False)."""
    u = rd(UNIVERSE)
    out: dict[str, dict[str, Any]] = {}

    def add(base: str, ms: int, venue: str) -> None:
        sym = normalize_perp_base(base)
        day = datetime.fromtimestamp(ms / 1000, UTC).date() if ms else date(2019, 1, 1)
        t = out.setdefault(sym, {"listed": day, "venues": set(), "bases": set()})
        t["listed"] = min(t["listed"], day)
        t["venues"].add(venue)
        t["bases"].add(base)

    meta_p = UNIVERSE.parent.parent / "free_supply_history/bybit_meta.json"
    tradfi = set()
    if exclude_tradfi and meta_p.exists():
        tradfi = {b for b, v in rd(meta_p).items() if v.get("symbolType") in ("stock", "ETF", "commodity", "forex")}
    for base, m in u["binance"].items():
        add(base, int(m.get("onboard") or 0), "binance")
    for base, m in u["bybit"].items():
        if base not in tradfi:
            add(base, int(m.get("launch") or 0), "bybit")
    return out


def our_snapshots() -> dict[str, dict[str, list[dict[str, Any]]]]:
    """day -> symbol -> rows of our CoinGecko supply_snapshot (read-only)."""
    con = sqlite3.connect(DB, uri=True)
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    q = "SELECT day, coin_id, symbol, price, market_cap, circulating_supply, total_supply, max_supply FROM supply_snapshot"
    for day, cid, sym, price, mcap, circ, total, mx in con.execute(q):
        out.setdefault(day, {}).setdefault(str(sym).upper(), []).append(
            {"coin_id": cid, "price": price, "market_cap": mcap, "circ": circ, "total": total, "max": mx}
        )
    con.close()
    return out


# ----------------------------------------------------------------------------- modes


async def probe(cache: Path) -> None:
    src = Src()
    try:
        d = date(2021, 1, 3)
        comp = await cmc_date(src, cache, d)
        if comp:
            n = sum(len(v) for v in comp.values())
            btc = comp.get("BTC", [{}])[0]
            log(f"cmc {d}: {n} coins, BTC {btc}")
        html = await cached_bytes(cache, "web/cmc_hist_20210103.html.gz", src.cmc_web, "/historical/20210103/")
        if html:
            log(f"cmc page: {len(html)} bytes, __NEXT_DATA__={b'__NEXT_DATA__' in html}, circulating={html.count(b'irculating')}")
        cat = await cached_bytes(
            cache, "cm/catalog_assets.json.gz", src.cm, "/v4/catalog-v2/asset-metrics", {"metrics": "SplyCur", "page_size": 10000}
        )
        if cat:
            js = json.loads(cat)
            log(f"cm catalog SplyCur: {len(js.get('data') or [])} assets; first {str((js.get('data') or [None])[0])[:300]}")
        ts = await cached_bytes(
            cache,
            "cm/probe_btc.json.gz",
            src.cm,
            "/v4/timeseries/asset-metrics",
            {"assets": "btc", "metrics": "SplyCur", "frequency": "1d", "start_time": "2021-01-01", "page_size": 10000},
        )
        if ts:
            js = json.loads(ts)
            rows = js.get("data") or []
            log(f"cm btc SplyCur rows={len(rows)} first={rows[:1]} last={rows[-1:]} next={bool(js.get('next_page_token'))}")
        gh = await cached_bytes(cache, "gh/mirror_root.json.gz", src.gh, "/repos/chengmarc/coinmetrics-data/contents")
        if gh:
            js = json.loads(gh)
            log(f"github mirror root: {[x.get('name') for x in js][:20] if isinstance(js, list) else str(js)[:200]}")
        cdx = await cached_bytes(
            cache,
            "wb/cdx_20210103.json.gz",
            src.wb,
            "/cdx/search/cdx",
            {"url": "coinmarketcap.com/historical/20210103/", "output": "json", "limit": 20},
        )
        if cdx:
            log(f"wayback cdx 20210103: {cdx[:600]!r}")
    finally:
        await src.aclose()


async def fetch_cmc(cache: Path) -> None:
    src = Src()
    dates = quarter_sample_dates(FIRST_YEAR, LAST)
    snap_days = sorted(our_snapshots())
    dates += [date.fromisoformat(d) for d in snap_days]
    t0 = time.monotonic()
    try:
        for i, d in enumerate(dates):
            comp = await cmc_date(src, cache, d)
            n = 0 if comp is None else sum(len(v) for v in comp.values())
            log(f"cmc {i + 1}/{len(dates)} {d}: {n} coins, {time.monotonic() - t0:.0f}s elapsed")
    finally:
        await src.aclose()


async def fetch_tickers(cache: Path) -> dict[str, dict[str, float]]:
    """Current last prices of Binance/Bybit USDT perps (resolves ticker collisions to a CMC id)."""
    p = cache / "tickers.json"
    if p.exists():
        return rd(p)
    from tradedesk.server import build_server  # noqa: PLC0415

    _, rt = build_server()
    out: dict[str, dict[str, float]] = {}
    for venue in ("binanceusdm", "bybit"):
        data = (await rt.exchanges.tickers(venue)).data or {}
        out[venue] = {s: float(t["last"]) for s, t in data.items() if s.endswith("/USDT:USDT") and t and t.get("last")}
        log(f"tickers {venue}: {len(out[venue])}")
    await rt.exchanges.aclose()
    wr(p, out)
    return out


async def fetch_bybit_meta(cache: Path) -> None:
    """Bybit linear USDT market metadata: which perps are TradFi (stocks, ETFs, metals), not crypto tokens."""
    from collections import Counter  # noqa: PLC0415

    from tradedesk.server import build_server  # noqa: PLC0415

    _, rt = build_server()
    ms = (await rt.exchanges.markets("bybit")).data or {}
    await rt.exchanges.aclose()
    out = {}
    for sym, m in ms.items():
        if m and m.get("swap") and m.get("linear") and sym.endswith("/USDT:USDT"):
            info = m.get("info") or {}
            out[m["base"]] = {k: v for k, v in info.items() if isinstance(v, str) and len(v) < 40}
    wr(cache / "bybit_meta.json", out)
    keys = Counter(k for v in out.values() for k in v)
    print("keys:", sorted(keys))
    for k in ("symbolType", "contractType", "status", "copyTrading", "isPreListing", "displayName"):
        c = Counter(v.get(k) for v in out.values())
        print(k, c.most_common(8))
    for b in ("AAPL", "AMZN", "XAU", "BTC", "PEPE"):
        print(b, {k: out.get(b, {}).get(k) for k in ("symbolType", "contractType", "baseCoin")})


def multiplier(base: str) -> float:
    norm = normalize_perp_base(base)
    pre = base.upper()[: len(base) - len(norm)]
    return 1e6 if pre == "1M" else (float(pre) if pre else 1.0)


def resolve_ids(cache: Path, tokens: dict[str, dict[str, Any]], ref_day: date) -> dict[str, dict[str, Any]]:
    """token -> {cmc_id, how, n_candidates} on the reference CMC listing (price match, else largest market cap)."""
    tick = rd(cache / "tickers.json")
    comp = rd(cache / f"cmc/compact2_{ref_day.isoformat()}.json")
    out: dict[str, dict[str, Any]] = {}
    for sym, t in tokens.items():
        cands = comp.get(sym) or []
        ref = None
        for base in sorted(t["bases"]):
            for venue in ("binanceusdm", "bybit"):
                px = tick.get(venue, {}).get(f"{base}/USDT:USDT")
                if px:
                    ref = px / multiplier(base)
                    break
            if ref:
                break
        cc = [{"price": c.get("price"), "market_cap": c.get("mcap")} for c in cands]
        idx = resolve_symbol(cc, ref) if cands else None
        how = "price" if ref else "mcap"
        if idx is None and ref and cands:
            how = "price_mismatch"
        out[sym] = {"cmc_id": None if idx is None else int(cands[idx]["id"]), "how": how, "n_cand": len(cands), "ref": ref}
    return out


# ----------------------------------------------------------------------------- Wayback (point-in-time test)


def _rows_from(obj: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 40:
        return
    if isinstance(obj, str):
        if len(obj) > 1000 and obj[:1] in "{[":
            try:
                _rows_from(json.loads(obj), out, depth + 1)
            except ValueError:
                pass
        return
    if isinstance(obj, list):
        for x in obj:
            _rows_from(x, out, depth + 1)
        return
    if not isinstance(obj, dict):
        return
    keys = obj.get("keysArr")
    if isinstance(keys, list) and isinstance(obj.get("data"), list):
        for row in obj["data"]:
            if isinstance(row, list):
                _rows_from(dict(zip(keys, row, strict=False)), out, depth + 1)
    if "circulatingSupply" in obj and ("id" in obj or "symbol" in obj):
        out.append(
            {
                "id": obj.get("id"),
                "symbol": obj.get("symbol"),
                "circ": obj.get("circulatingSupply"),
                "total": obj.get("totalSupply"),
                "max": obj.get("maxSupply"),
            }
        )
    elif "circulating_supply" in obj and ("id" in obj or "symbol" in obj):  # 2020-2021 page format
        out.append(
            {
                "id": obj.get("id"),
                "symbol": obj.get("symbol"),
                "circ": obj.get("circulating_supply"),
                "total": obj.get("total_supply"),
                "max": obj.get("max_supply"),
            }
        )
    for v in obj.values():
        if isinstance(v, (dict, list, str)):
            _rows_from(v, out, depth + 1)


def page_rows(html: bytes) -> list[dict[str, Any]]:
    m = re.search(rb'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    rows: list[dict[str, Any]] = []
    _rows_from(json.loads(m.group(1)), rows)
    seen, uniq = set(), []
    for r in rows:
        k = (r["id"], r["symbol"])
        if k not in seen and r["id"] is not None:
            seen.add(k)
            uniq.append(r)
    return uniq


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


async def wayback(cache: Path) -> None:
    src = Src()
    pairs: list[dict[str, Any]] = []
    try:
        live = rd_bytes(cache / "web/cmc_hist_20210103.html.gz")
        lr = page_rows(live)
        api = cmc_by_id(rd(cache / "cmc/compact2_2021-01-03.json"))
        g = [rel_gap(_num(r["circ"]), _num(api.get(int(r["id"]), {}).get("circ"))) for r in lr]
        log(f"sanity: live page 2021-01-03 rows={len(lr)}; vs API {summarize_gaps(g)}")
        for d in quarter_sample_dates(FIRST_YEAR, LAST):
            ds = d.strftime("%Y%m%d")
            cdx = await cached_bytes(
                cache,
                f"wb/cdx_{ds}.json.gz",
                src.wb,
                "/cdx/search/cdx",
                {"url": f"coinmarketcap.com/historical/{ds}/", "output": "json", "filter": "statuscode:200", "limit": 50},
            )
            caps = json.loads(cdx)[1:] if cdx else []
            caps = [c for c in caps if d.strftime("%Y%m%d") <= c[1][:8] <= (d + timedelta(days=14)).strftime("%Y%m%d")]
            if not caps:
                log(f"wayback {d}: no capture within 14 days")
                continue
            ts = caps[0][1]
            html = await cached_bytes(
                cache, f"wb/page_{ds}_{ts}.html.gz", src.wb, f"/web/{ts}id_/https://coinmarketcap.com/historical/{ds}/"
            )
            if not html:
                continue
            rows = page_rows(html)
            api = cmc_by_id(rd(cache / f"cmc/compact2_{d.isoformat()}.json"))
            n0 = len(pairs)
            for r in rows:
                today = api.get(int(r["id"]))
                if today is None:
                    continue
                for f in ("circ", "total", "max"):
                    pairs.append(
                        {
                            "date": d.isoformat(),
                            "capture": ts,
                            "id": int(r["id"]),
                            "symbol": r["symbol"],
                            "field": f,
                            "archived": _num(r[f]),
                            "today": _num(today.get(f)),
                        }
                    )
            log(f"wayback {d}: capture {ts}, page rows={len(rows)}, pairs+={len(pairs) - n0}")
        # more dates: earliest capture of every archived historical page, kept if taken within 14 days of the page date
        cdx = await cached_bytes(
            cache,
            "wb/cdx_prefix.json.gz",
            src.wb,
            "/cdx/search/cdx",
            {
                "url": "coinmarketcap.com/historical/",
                "matchType": "prefix",
                "output": "json",
                "filter": "statuscode:200",
                "collapse": "urlkey",
                "fl": "original,timestamp",
                "limit": 20000,
            },
        )
        extra: dict[int, list[tuple[date, str]]] = {}
        done = {p["date"] for p in pairs}
        for orig, ts in json.loads(cdx)[1:] if cdx else []:
            m = re.search(r"/historical/(\d{8})/?$", orig)
            if not m:
                continue
            try:
                d = datetime.strptime(m.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            lag = (datetime.strptime(ts[:8], "%Y%m%d").date() - d).days
            if 0 <= lag <= 14 and FIRST_YEAR <= d.year <= 2026 and d.isoformat() not in done:
                extra.setdefault(d.year, []).append((d, ts))
        log(f"wayback prefix: timely captures per year {[(y, len(v)) for y, v in sorted(extra.items())]}")
        for _y, lst in sorted(extra.items()):
            lst.sort()
            pick = [lst[int(i * len(lst) / 3)] for i in range(min(3, len(lst)))]
            for d, ts in pick:
                ds = d.strftime("%Y%m%d")
                html = await cached_bytes(
                    cache, f"wb/page_{ds}_{ts}.html.gz", src.wb, f"/web/{ts}id_/https://coinmarketcap.com/historical/{ds}/"
                )
                comp = await cmc_date(src, cache, d)
                if not html or comp is None:
                    continue
                api, rows, n0 = cmc_by_id(comp), page_rows(html), len(pairs)
                for r in rows:
                    today = api.get(int(r["id"]))
                    if today is not None:
                        for f in ("circ", "total", "max"):
                            pairs.append(
                                {
                                    "date": d.isoformat(),
                                    "capture": ts,
                                    "id": int(r["id"]),
                                    "symbol": r["symbol"],
                                    "field": f,
                                    "archived": _num(r[f]),
                                    "today": _num(today.get(f)),
                                }
                            )
                log(f"wayback extra {d}: capture {ts}, page rows={len(rows)}, pairs+={len(pairs) - n0}")
    finally:
        await src.aclose()
    wr(cache / "wayback_pairs.json", pairs)


# ----------------------------------------------------------------------------- Coin Metrics


async def coinmetrics(cache: Path, tokens: dict[str, dict[str, Any]]) -> None:
    src = Src()
    try:
        for metric in ("SplyCur", "SplyFF", "SplyCirc"):
            b = await cached_bytes(
                cache, f"cm/catalog_{metric}.json.gz", src.cm, "/v4/catalog-v2/asset-metrics", {"metrics": metric, "page_size": 10000}
            )
            n = len(json.loads(b).get("data") or []) if b else None
            log(f"cm catalog {metric}: {n} assets")
        cat = json.loads(rd_bytes(cache / "cm/catalog_SplyCur.json.gz"))["data"]
        assets = {a["asset"] for a in cat}
        hit = sorted(s for s in tokens if s.lower() in assets)
        log(f"cm: {len(assets)} assets with SplyCur, {len(hit)} match perp tokens by ticker")
        for i, s in enumerate(hit):
            await cached_bytes(
                cache,
                f"cm/ts_{s.lower()}.json.gz",
                src.cm,
                "/v4/timeseries/asset-metrics",
                {
                    "assets": s.lower(),
                    "metrics": "SplyCur,PriceUSD",
                    "frequency": "1d",
                    "start_time": "2021-01-01",
                    "page_size": 10000,
                    "ignore_unsupported_errors": "true",
                },
            )
            if i % 20 == 0:
                log(f"cm series {i + 1}/{len(hit)}")
        for p in ("/repos/coinmetrics/data", "/repos/coinmetrics/data/commits", "/users/chengmarc/repos", "/search/repositories"):
            params = {"path": "csv/eth.csv", "until": "2022-01-15T00:00:00Z", "per_page": 3} if p.endswith("commits") else None
            if p.startswith("/search"):
                params = {"q": "coinmetrics-data in:name", "per_page": 10}
            b = await cached_bytes(cache, f"gh/{p.strip('/').replace('/', '_')}.json.gz", src.gh, p, params)
            if b:
                js = json.loads(b)
                if isinstance(js, list):
                    log(
                        f"gh {p}: {[(x.get('name') or x.get('sha'), (x.get('commit') or {}).get('committer', {}).get('date'), x.get('pushed_at')) for x in js][:10]}"
                    )
                else:
                    items = js.get("items")
                    log(
                        f"gh {p}: "
                        + (
                            str([(x["full_name"], x.get("pushed_at"), (x.get("license") or {}).get("spdx_id")) for x in items][:10])
                            if items is not None
                            else str({k: js.get(k) for k in ("full_name", "pushed_at", "archived", "license", "size")})
                        )
                    )
    finally:
        await src.aclose()


async def cm_pit(cache: Path, sha: str, assets: list[str]) -> None:
    """Old CSV from the coinmetrics/data repo at commit ``sha`` vs today's community API (revision test)."""
    raw = Http("github_raw", "https://raw.githubusercontent.com", rate=0.5, burst=1, timeout=120, headers=UA)
    pairs = []
    try:
        for a in assets:
            b = await cached_bytes(cache, f"gh/csv_{sha[:8]}_{a}.csv.gz", raw, f"/coinmetrics/data/{sha}/csv/{a}.csv")
            if not b:
                continue
            lines = b.decode().splitlines()
            hdr = lines[0].split(",")
            if "SplyCur" not in hdr:
                log(f"{a}: no SplyCur column")
                continue
            i_t, i_s = hdr.index("time"), hdr.index("SplyCur")
            old = {ln.split(",")[i_t][:10]: _num(ln.split(",")[i_s]) for ln in lines[1:]}
            tp = cache / f"cm/ts_{a.lower()}.json.gz"
            ts = json.loads(rd_bytes(tp)) if tp.exists() else None
            if ts is None:
                log(f"{a}: no current series cached")
                continue
            now = {r["time"][:10]: _num(r.get("SplyCur")) for r in ts.get("data") or []}
            common = sorted(set(old) & set(now))
            for d in common:
                pairs.append({"asset": a, "date": d, "archived": old[d], "today": now[d]})
            log(f"{a}: old rows={len(old)} common={len(common)} last_common={common[-1:]}")
    finally:
        await raw.aclose()
    wr(cache / "cm_pit_pairs.json", pairs)


# ----------------------------------------------------------------------------- terms


TERMS = [
    ("cmc_web", "https://coinmarketcap.com", "/terms/"),
    ("cmc_web", "https://coinmarketcap.com", "/robots.txt"),
    ("cmc_api", "https://api.coinmarketcap.com", "/robots.txt"),
    ("cm_web", "https://coinmetrics.io", "/terms-of-use/"),
    ("cm_web", "https://coinmetrics.io", "/community-data-terms-of-use/"),
    ("cm_docs", "https://docs.coinmetrics.io", "/api/v4/"),
    ("cm_docs", "https://docs.coinmetrics.io", "/access-our-data/api"),
    ("gh_raw", "https://raw.githubusercontent.com", "/coinmetrics/data/master/README.md"),
    ("archive", "https://archive.org", "/about/terms.php"),
]
KEYWORDS = re.compile(
    r"scrap|robot|crawl|spider|automat|non-?commercial|creative commons|cc by|personal use|data mining|disallow|attribution", re.I
)


async def terms(cache: Path) -> None:
    for i, (prov, base, path) in enumerate(TERMS):
        h = Http(prov, base, rate=0.5, burst=1, timeout=60, headers=UA)
        b = await cached_bytes(cache, f"terms/{i}_{prov}.gz", h, path)
        await h.aclose()
        if not b:
            continue
        text = re.sub(rb"<script.*?</script>|<style.*?</style>", b" ", b, flags=re.S)
        text = re.sub(rb"<[^>]+>", b" ", text).decode("utf-8", "replace")
        text = re.sub(r"\s+", " ", text)
        sents = [s.strip() for s in re.split(r"(?<=[.;])\s+|(?=Disallow:|User-agent:)", text) if KEYWORDS.search(s)]
        print(f"--- {base}{path}: {len(text)} chars, {len(sents)} keyword sentences")
        for s in sents[:14]:
            print("   ", s[:260])


# ----------------------------------------------------------------------------- report


def report(cache: Path, exclude_tradfi: bool = True) -> None:
    tokens = perp_tokens(exclude_tradfi)
    print(f"universe: {'crypto only (Bybit TradFi perps dropped)' if exclude_tradfi else 'as registered (incl. Bybit TradFi perps)'}")
    sample = quarter_sample_dates(FIRST_YEAR, LAST)
    ref_day = date(2026, 10, 22)
    ids = resolve_ids(cache, tokens, ref_day)
    res: dict[str, Any] = {"n_tokens": len(tokens)}
    by_date = {d: cmc_by_id(rd(cache / f"cmc/compact2_{d.isoformat()}.json")) for d in sample}
    sym_date = {d: rd(cache / f"cmc/compact2_{d.isoformat()}.json") for d in sample}
    rows = []
    for sym, t in tokens.items():
        win = [d for d in sample if d >= t["listed"]]
        cid = ids[sym]["cmc_id"]
        have_id = [d for d in win if cid is not None and _num(by_date[d].get(cid, {}).get("circ"))]
        have_sym = [d for d in win if any(_num(r.get("circ")) for r in sym_date[d].get(sym, []))]
        rows.append(
            {
                "sym": sym,
                "listed": t["listed"].isoformat(),
                "venues": sorted(t["venues"]),
                "n_win": len(win),
                "cmc_id": cid,
                "how": ids[sym]["how"],
                "n_cand": ids[sym]["n_cand"],
                "cov_id": coverage_fraction(win, have_id),
                "cov_sym": coverage_fraction(win, have_sym),
                "in_listing_any": cid is not None and any(cid in by_date[d] for d in win),
            }
        )
    wr(cache / "coverage_rows.json", rows)
    elig = [r for r in rows if r["n_win"] > 0]
    print(f"\n== C1 CMC coverage: perp tokens {len(rows)}, with >=1 sample date in window {len(elig)} (sample dates {len(sample)})")
    for label, key in (("by CMC id", "cov_id"), ("by symbol", "cov_sym")):
        ok = sum(1 for r in elig if (r[key] or 0) >= 0.8)
        print(f"  {label}: covered >=80% of dates: {ok}/{len(elig)} = {ok / len(elig):.1%}")
    print(
        f"  resolution: price={sum(r['how'] == 'price' for r in rows)} mcap={sum(r['how'] == 'mcap' for r in rows)} "
        f"price_mismatch={sum(r['how'] == 'price_mismatch' for r in rows)} no_cmc_id={sum(r['cmc_id'] is None for r in rows)} "
        f"ticker_collisions(n_cand>1)={sum(r['n_cand'] > 1 for r in rows)}"
    )
    for y in range(2021, 2027):
        sub = [r for r in elig if r["listed"][:4] == str(y) or (y == 2021 and r["listed"] < "2021")]
        if sub:
            ok = sum(1 for r in sub if (r["cov_id"] or 0) >= 0.8)
            print(f"  listed {'<=' if y == 2021 else ''}{y}: {ok}/{len(sub)} = {ok / len(sub):.1%}")
    sub = [r for r in elig if r["n_win"] >= 2]
    ok = sum(1 for r in sub if (r["cov_id"] or 0) >= 0.8)
    print(f"  exploratory, tokens with >=2 sample dates in window: {ok}/{len(sub)} = {ok / len(sub):.1%}")
    one = [r for r in elig if r["n_win"] == 1]
    print(
        f"  tokens with a single sample date (listed 2026-07-06..10-04): {len(one)}, covered {sum(1 for r in one if (r['cov_id'] or 0) >= 0.8)}"
    )
    # per-date share of active perp tokens with positive circ on that date
    print("  per sample date: active perp tokens with circ>0 (by id) / active")
    line = []
    for d in sample:
        act = [r for r in rows if r["listed"] <= d.isoformat() and r["cmc_id"] is not None]
        okd = sum(1 for r in act if _num(by_date[d].get(r["cmc_id"], {}).get("circ")))
        nall = sum(1 for r in rows if r["listed"] <= d.isoformat())
        line.append(f"{d:%y-%m}:{okd}/{nall}")
    print("   ", " ".join(line))
    misses = sorted((r for r in elig if (r["cov_id"] or 0) < 0.8), key=lambda r: r["sym"])
    print(f"  not covered ({len(misses)}), first 40: {[(r['sym'], r['how'], round(r['cov_id'] or 0, 2)) for r in misses[:40]]}")

    # ---- C2: CMC vs our CoinGecko snapshots
    ours = our_snapshots()
    print("\n== C2 CMC vs our supply_snapshot (CoinGecko) on common days")
    gaps: dict[str, list[float | None]] = {"circ": [], "total": [], "max": []}
    big = []
    for day, osyms in sorted(ours.items()):
        p = cache / f"cmc/compact2_{day}.json"
        if not p.exists():
            continue
        api = cmc_by_id(rd(p))
        if not api:
            continue
        for sym, r in ids.items():
            c = api.get(r["cmc_id"]) if r["cmc_id"] is not None else None
            if not c or sym not in osyms:
                continue
            k = resolve_symbol([{"price": o["price"], "market_cap": o["market_cap"]} for o in osyms[sym]], _num(c.get("price")))
            if k is None:
                continue
            o = osyms[sym][k]
            for f in gaps:
                gaps[f].append(rel_gap(_num(c.get(f)), _num(o.get(f))))
            gc = rel_gap(_num(c.get("circ")), _num(o.get("circ")))
            if gc is not None and gc > 0.10 and day == "2026-10-22":
                big.append((sym, round(gc, 3)))
    for f, g in gaps.items():
        print(f"  {f}: {summarize_gaps(g)}")
    print(f"  circ gap >10% on 2026-10-22 ({len(big)}): {sorted(big, key=lambda x: -x[1])[:25]}")
    res["c2"] = {f: summarize_gaps(g).__dict__ for f, g in gaps.items()}

    # ---- C4: Wayback
    wp = cache / "wayback_pairs.json"
    if wp.exists():
        pairs = rd(wp)
        print(f"\n== C4 point-in-time (Wayback capture vs today's API): {len({p['date'] for p in pairs})} dates")
        for f in ("circ", "total", "max"):
            s = summarize_gaps(rel_gap(p["today"], p["archived"]) for p in pairs if p["field"] == f)
            print(f"  {f}: {s} -> {pit_verdict(s)}")
        perp_ids = {r["cmc_id"] for r in rows if r["cmc_id"] is not None}
        s = summarize_gaps(rel_gap(p["today"], p["archived"]) for p in pairs if p["field"] == "circ" and p["id"] in perp_ids)
        print(f"  circ, perp tokens only: {s} -> {pit_verdict(s)}")
        for y in range(2021, 2027):
            s = summarize_gaps(rel_gap(p["today"], p["archived"]) for p in pairs if p["field"] == "circ" and p["date"][:4] == str(y))
            print(f"  circ {y}: n={s.n} median={s.median} share>5%={s.share_gt_5pct}")
        worst = sorted(
            (p for p in pairs if p["field"] == "circ" and (rel_gap(p["today"], p["archived"]) or 0) > 0.05),
            key=lambda p: -(rel_gap(p["today"], p["archived"]) or 0),
        )[:12]
        print("  worst circ revisions:", [(p["date"], p["symbol"], p["archived"], p["today"]) for p in worst])
    cp = cache / "cm_pit_pairs.json"
    if cp.exists():
        pairs = rd(cp)
        s = summarize_gaps(rel_gap(p["today"], p["archived"]) for p in pairs)
        print(f"\n== C4 Coin Metrics SplyCur (old GitHub CSV vs today's API): {s} -> {pit_verdict(s)}")
        for a in sorted({p["asset"] for p in pairs}):
            sa = summarize_gaps(rel_gap(p["today"], p["archived"]) for p in pairs if p["asset"] == a)
            print(f"  {a}: n={sa.n} median={sa.median} share>5%={sa.share_gt_5pct}")

    # ---- Coin Metrics coverage
    cat = cache / "cm/catalog_SplyCur.json.gz"
    if cat.exists():
        print("\n== C1 Coin Metrics community SplyCur coverage")
        cov, n_el, mism, gaps_cm = 0, 0, [], []
        for r in elig:
            p = cache / f"cm/ts_{r['sym'].lower()}.json.gz"
            n_el += 1
            if not p.exists():
                continue
            data = json.loads(rd_bytes(p)).get("data") or []
            series = {x["time"][:10]: x for x in data}
            ref = by_date[sample[-1]].get(r["cmc_id"], {}) if r["cmc_id"] else {}
            last = series.get(sample[-1].isoformat()) or {}
            pg = rel_gap(_num(last.get("PriceUSD")), _num(ref.get("price")))
            if pg is not None and pg > 0.5:
                mism.append(r["sym"])
                continue
            win = [d for d in sample if d >= date.fromisoformat(r["listed"])]
            have = [d for d in win if _num((series.get(d.isoformat()) or {}).get("SplyCur"))]
            if (coverage_fraction(win, have) or 0) >= 0.8:
                cov += 1
            gaps_cm.append(rel_gap(_num(last.get("SplyCur")), _num(ref.get("circ"))))
        print(f"  covered >=80%: {cov}/{n_el} = {cov / n_el:.1%}; ticker collisions by price (excluded): {mism}")
        print(f"  SplyCur vs CMC circulating on {sample[-1]}: {summarize_gaps(gaps_cm)}")
    wr(cache / "report.json", res)


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("mode", choices=["probe", "cmc", "tickers", "bybit_meta", "cm", "cm_pit", "wayback", "terms", "report"])
    a.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/free_supply_history")
    a.add_argument("--with-tradfi", action="store_true", help="report: keep Bybit stock/ETF/commodity/forex perps")
    a.add_argument("--sha", default="", help="cm_pit: coinmetrics/data commit")
    a.add_argument("--assets", default="btc,eth,link,uni,aave,doge,ltc,xrp", help="cm_pit: assets")
    args = a.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "probe":
        asyncio.run(probe(args.cache_dir))
    elif args.mode == "cmc":
        asyncio.run(fetch_cmc(args.cache_dir))
    elif args.mode == "tickers":
        asyncio.run(fetch_tickers(args.cache_dir))
    elif args.mode == "bybit_meta":
        asyncio.run(fetch_bybit_meta(args.cache_dir))
    elif args.mode == "cm":
        asyncio.run(coinmetrics(args.cache_dir, perp_tokens()))
    elif args.mode == "cm_pit":
        if not args.sha:
            sys.exit("--sha is required")
        asyncio.run(cm_pit(args.cache_dir, args.sha, args.assets.split(",")))
    elif args.mode == "wayback":
        asyncio.run(wayback(args.cache_dir))
    elif args.mode == "terms":
        asyncio.run(terms(args.cache_dir))
    else:
        report(args.cache_dir, exclude_tradfi=not args.with_tradfi)


if __name__ == "__main__":
    main()
