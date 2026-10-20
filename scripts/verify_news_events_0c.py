"""News events (IDEA-20261017-news-events), stage 0c: Korean exchanges (Upbit; Bithumb if a public feed exists).

ARCHIVED (2026-10-19): the Upbit provider, tool and analytics were removed from the project (owner decision); the
helpers this script needs are vendored in ``scripts/_upbit_0c_helpers.py``. Kept as history of the 0c report only.

Idea card: docs/ideas/IDEA-20261017-news-events.md (section "Попередня реєстрація 0c").
Events: Upbit announcements API (category ``trade``, all pages): KRW-market listings, trading warnings
("거래 유의 종목 지정"), delistings ("거래지원 종료"). Titles are untrusted text: only matched against fixed patterns.
Prices: Binance USD-M from data.binance.vision (1m klines, 1h for the placebo, funding, aggTrades -> 1 s bars), via the
stage-0 pipeline of ``scripts/verify_news_events.py`` (same per-file cache layout). No keys, no orders.

Stages (cache under --cache-dir; a rerun resumes):
    prereg   SHA-256 of the card section "## Попередня реєстрація 0c" (to the end of the file)
    ann      Upbit announcement pages; Bithumb probe
    bodies   bodies of batch notices ("(8종)": tickers only in the body), one request per notice
    titles   counts and titles per class (no prices)
    prices   perp live at t0 (Binance USD-M only), 1m windows, 1h placebo months, funding, BTC, Binance spot,
             Upbit KRW price before t0 and Upbit KRW-X / KRW-BTC at t0 + 24 h / + 72 h (collision check)
             [--sample N to time it]
    seconds  aggTrades -> 1 s bars in [t0 - 5 min, t0 + 61 min]; for listings also side-aware bars (aggressive buys /
             sells from ``is_buyer_maker``) under sec2/
    stage0   power gate: n, clusters, SD, MDE only (no means) + per-event price levels for the collision review
    report   all registered tables (+ the post-review rows: taker-side execution for H1, collision at t0 + 24 h)

Outputs carry the run tag (--tag, default v2): events_<tag>.json, measured_<tag>.json, stage0_<tag>.txt,
report_<tag>.txt. Run 1 (before the critic's review 2026-10-18) wrote the untagged files, which stay as they were.

    uv run python scripts/verify_news_events_0c.py <stage> --cache-dir ~/.cache/tradedesk-agents/news_events_0c
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import logging
import statistics
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_news_events as v0  # noqa: E402  (stage-0 pipeline: fetchers, cache helpers, formatting)
from _upbit_0c_helpers import (  # noqa: E402  (vendored: the Upbit analytics were removed from the package)
    btc_cross_ratio,
    classify_upbit,
    upbit_batch_size,
    upbit_body_is_reminder,
    upbit_body_tickers,
    upbit_time_ms,
)

from tradedesk.analytics.news_events import (  # noqa: E402
    DAY_MS,
    HOUR_MS,
    MIN_MS,
    MULTIPLIERS,
    Bars,
    Event,
    cluster_bootstrap,
    cluster_sd,
    cochran_q,
    dedupe,
    flat_share,
    holm,
    mde_clusters,
    move_share_bootstrap,
    one_sided_p,
    paired_diff_bootstrap,
    perp_live,
    placebo_times,
    price_at,
    reaction,
    second_bars,
    short_trade,
    taker_buy_fill,
    taker_sell_exit,
    vwap_fill,
)
from tradedesk.core.errors import ProviderError  # noqa: E402
from tradedesk.core.http import Http  # noqa: E402
from tradedesk.server import build_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "docs" / "ideas" / "IDEA-20261017-news-events.md"
SECTION = "## Попередня реєстрація 0c"
STAGE0_CACHE = Path.home() / ".cache" / "tradedesk-agents" / "news_events"  # stage-0 events for placebo blocking

# ---- registered parameters (card, "Попередня реєстрація 0c")
START = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp() * 1000)
END = int(datetime(2026, 10, 13, tzinfo=UTC).timestamp() * 1000)
PRE_CUT = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)  # Claude's preliminary measurement: 2026-01..10
CATS = ("listing", "warning", "delist")
REACT = {"+1m": MIN_MS, "+5m": 5 * MIN_MS, "+15m": 15 * MIN_MS, "+60m": HOUR_MS, "+4h": 4 * HOUR_MS, "+24h": DAY_MS, "+72h": 3 * DAY_MS}
SEC_REACT = {"+5s": 5000, "+10s": 10000, "+30s": 30000, "+60s": 60000, "+5m": 5 * MIN_MS, "+15m": 15 * MIN_MS, "+60m": HOUR_MS}
SEC_D = (5, 10, 30, 60)  # seconds after t0 (H1 entries)
SEC_H = (5, 15, 60)  # minutes after t0 (H1 exits)
SEC_WIN = (-5 * MIN_MS, 61 * MIN_MS)
FEE, SLIP = v0.FEE, v0.SLIP
COST, COST_STRESS = FEE + SLIP, FEE + 3 * SLIP
STOP = 0.25
N_PLACEBO = 20
GATE_MDE = 0.05
MIN_K, MIN_K_OOS = 20, 15
ALPHA = 0.05
# hypothesis -> (category, sign: +1 short / -1 long, primary key, grid delays (min), grid holds (h))
HYP = {
    "H1": ("listing", -1.0, "s10_m15_base", None, None),
    "H2": ("listing", 1.0, "d60_h24_base", (30, 60, 120), (24, 72)),
    "H3": ("warning", 1.0, "d15_h24_base", (5, 15, 30), (24, 72)),
    "H4": ("delist", 1.0, "d15_h24_base", (5, 15, 30), (24, 72)),
}
UPBIT_API = "https://api-manager.upbit.com"
KST_MS = 9 * HOUR_MS
TAG = "v2"  # set from --tag in main()
COLL_LAGS = (DAY_MS, 3 * DAY_MS)  # post-review collision check: Upbit KRW-X / KRW-BTC vs perp / BTC perp at t0 + lag


def tagged(cache: Path, stem: str, ext: str) -> Path:
    return cache / f"{stem}_{TAG}.{ext}"


def rss_mb() -> float:
    return v0.rss_mb()


rd, wr, out, fmt, ymd, ymdhm = v0.rd, v0.wr, v0.out, v0.fmt, v0.ymd, v0.ymdhm


# ================================================================== announcements


async def stage_ann(cache: Path) -> None:
    up = Http("upbit_ann", UPBIT_API, rate=1, burst=1, timeout=30, retries=2, headers={"Accept": "application/json"})
    t_start = time.time()
    try:
        page = 1
        while True:
            p = cache / "ann" / f"upbit_trade_{page:04d}.json"
            if not p.exists():
                f = None
                for k in range(6):
                    try:
                        await asyncio.sleep(6)  # Cloudflare 1015 at ~1 req/s
                        f = await up.get_json(
                            "/api/v1/announcements", params={"os": "web", "page": page, "per_page": 20, "category": "trade"}
                        )
                        break
                    except ProviderError as e:
                        if e.reason != "rate_limited" or k == 5:
                            raise
                        print(f"  page {page}: rate limited, sleeping {60 * (k + 1)}s", flush=True)
                        await asyncio.sleep(60 * (k + 1))
                assert f is not None
                d = f.data.get("data") if isinstance(f.data, dict) else None
                notices = (d or {}).get("notices") if isinstance(d, dict) else None
                if notices is None:
                    print(f"  unexpected payload on page {page}: keys {list(f.data)[:10] if isinstance(f.data, dict) else type(f.data)}")
                    wr(cache / "ann" / f"upbit_raw_{page:04d}.json", f.data)
                    break
                wr(p, {"notices": notices, "total_pages": d.get("total_pages"), "total_count": d.get("total_count")})
            pg = rd(p)
            if page % 10 == 1:
                print(
                    f"  upbit page {page}/{pg.get('total_pages')}: {len(pg['notices'])} notices "
                    f"({time.time() - t_start:.0f}s) rss={rss_mb():.0f}MB",
                    flush=True,
                )
            if not pg["notices"] or (pg.get("total_pages") and page >= int(pg["total_pages"])):
                break
            page += 1
        print(f"upbit pages: {page}; {time.time() - t_start:.0f}s", flush=True)
    finally:
        await up.aclose()
    await probe_bithumb(cache)


BITHUMB_CANDIDATES = [
    ("https://api.bithumb.com", "/v1/notices", {"page": 1, "count": 20}),
    ("https://api.bithumb.com", "/v1/notice", {"page": 1}),
    ("https://feed.bithumb.com", "/api/notice", {"page": 1}),
    ("https://feed.bithumb.com", "/notice", {"category": 9, "page": 1}),
    ("https://www.bithumb.com", "/customer_support/info_notice", {}),
    ("https://cafe.bithumb.com", "/view/boards/43", {}),
]


async def probe_bithumb(cache: Path) -> None:
    """Try public Bithumb notice endpoints; record status, content type and the first bytes (no parsing)."""
    res = []
    for base, path, params in BITHUMB_CANDIDATES:
        h = Http("bithumb_probe", base, rate=1, burst=1, timeout=20, retries=0, headers={"Accept": "application/json, text/html"})
        try:
            f = await h.get_bytes(path, params=params)
            body = f.data[:400].decode("utf-8", "replace")
            res.append({"url": base + path, "ok": True, "bytes": len(f.data), "head": body})
            (cache / "ann").mkdir(parents=True, exist_ok=True)
            (cache / "ann" / f"bithumb_probe_{hashlib.sha1((base + path).encode()).hexdigest()[:8]}.bin").write_bytes(f.data[:2_000_000])
        except ProviderError as e:
            res.append({"url": base + path, "ok": False, "reason": e.reason, "detail": str(e.detail)[:200]})
        finally:
            await h.aclose()
    wr(cache / "ann" / "bithumb_probe.json", res)
    for r in res:
        print(f"  bithumb probe {r['url']}: {'OK ' + str(r['bytes']) + ' bytes' if r['ok'] else r['reason'] + ' ' + r['detail'][:120]}")


def load_upbit(cache: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for p in sorted((cache / "ann").glob("upbit_trade_*.json")):
        for n in rd(p)["notices"]:
            if n.get("id") in seen:
                continue
            seen.add(n.get("id"))
            rows.append(n)
    return rows


async def stage_bodies(cache: Path) -> None:
    """Fetch the body of every batch notice in the period (``*_batch``: the tickers are only in the body)."""
    up = Http("upbit_ann", UPBIT_API, rate=1, burst=1, timeout=30, retries=2, headers={"Accept": "application/json"})
    try:
        for n in load_upbit(cache):
            ms = upbit_time_ms(n)
            kind, _ = classify_upbit(str(n.get("title") or ""))
            if not kind.endswith("_batch") or ms is None or not (START <= ms < END):
                continue
            p = cache / "ann" / f"body_{n['id']}.json"
            if not p.exists():
                for k in range(6):
                    try:
                        await asyncio.sleep(6)  # Cloudflare 1015 at ~1 req/s
                        f = await up.get_json(f"/api/v1/announcements/{n['id']}")
                        wr(p, f.data)
                        break
                    except ProviderError as e:
                        if e.reason != "rate_limited" or k == 5:
                            print(f"  body {n['id']}: {e.reason} {str(e.detail)[:120]}", flush=True)
                            break
                        await asyncio.sleep(60 * (k + 1))
            if p.exists():
                ticks = body_tickers(cache, n["id"])
                print(
                    f"  {ymdhm(ms)} id {n['id']} {kind}: N {upbit_batch_size(str(n['title']))}, parsed {len(ticks or [])}: {ticks}",
                    flush=True,
                )
    finally:
        await up.aclose()


def body_text(cache: Path, nid: Any) -> str | None:
    p = cache / "ann" / f"body_{nid}.json"
    if not p.exists():
        return None
    d = rd(p)
    data = d.get("data") if isinstance(d, dict) else None
    return str(data.get("body") or "") if isinstance(data, dict) else ""


def body_tickers(cache: Path, nid: Any) -> list[str] | None:
    """Tickers in the cached body of notice ``nid`` (None if not fetched; [] if the body repeats an earlier notice)."""
    text = body_text(cache, nid)
    if text is None:
        return None
    return [] if upbit_body_is_reminder(text) else upbit_body_tickers(text)


def build_events(cache: Path) -> tuple[dict[str, list[Event]], list[dict[str, Any]]]:
    """Events per category (72 h same-token dedupe) and the classified announcement rows. Batch notices use the
    tickers of their body when the count equals the N in the title (else they are skipped and printed)."""
    ev: dict[str, list[Event]] = defaultdict(list)
    rows = []
    for n in load_upbit(cache):
        ms = upbit_time_ms(n)
        if ms is None:
            continue
        kind, ticks = classify_upbit(str(n.get("title") or ""))
        if kind.endswith("_batch") and START <= ms < END:
            bt, nn = body_tickers(cache, n.get("id")), upbit_batch_size(str(n.get("title")))
            if bt is not None and len(bt) == nn:
                kind, ticks = kind.removesuffix("_batch"), bt
            else:
                print(f"  batch {n.get('id')} {ymd(ms)} skipped: N {nn}, body tickers {None if bt is None else len(bt)}")
        rows.append({"id": n.get("id"), "ms": ms, "title": n.get("title"), "kind": kind, "ticks": ticks})
        if kind in CATS and START <= ms < END:
            for t in ticks:
                ev[kind].append(Event(t, ms, kind, "upbit", f"upbit:{n.get('id')}"))
    out_ev = {}
    for k in CATS:
        kept, _ = dedupe(ev[k])
        out_ev[k] = kept
    return out_ev, rows


def stage_titles(cache: Path) -> None:
    ev, rows = build_events(cache)
    print(f"upbit announcements: {len(rows)}; span {ymd(min(r['ms'] for r in rows))} .. {ymd(max(r['ms'] for r in rows))}")
    print("by kind:", dict(Counter(r["kind"] for r in rows)))
    for k in ("listing", "warning", "delist", "warning_batch", "delist_batch"):
        sel = [r for r in rows if r["kind"] == k]
        print(f"\n## {k}: {len(sel)} announcements; examples:")
        for r in sel[:: max(1, len(sel) // 25)][:25]:
            print(f"  {ymdhm(r['ms'])} {r['ticks']} {str(r['title'])[:110]}")
    print("\n## ignored titles (sample of distinct prefixes)")
    pre = Counter(str(r["title"])[:28] for r in rows if r["kind"] == "ignore")
    for t, c in pre.most_common(60):
        print(f"  {c:4d} {t}")
    for k in CATS:
        evs = ev[k]
        print(
            f"{k}: {len(evs)} events after dedupe in [{ymd(START)}; {ymd(END)}); clusters {len({e.cluster for e in evs})}; by year {dict(sorted(Counter(ymd(e.ms)[:4] for e in evs).items()))}"
        )
    wr(tagged(cache, "events", "json"), {k: [e.__dict__ for e in v] for k, v in ev.items()})
    # difference to run 1 (untagged events.json), for the report
    if (cache / "events.json").exists():
        old = {(d["cluster"], d["token"], c) for c, v in rd(cache / "events.json").items() for d in v}
        new = {(e.cluster, e.token, e.category) for k in CATS for e in ev[k]}
        for c in CATS:
            add = sorted((x for x in new - old if x[2] == c), key=lambda x: int(x[0].split(":")[1]))
            gone = sorted((x for x in old - new if x[2] == c), key=lambda x: int(x[0].split(":")[1]))
            print(f"vs run 1, {c}: +{len(add)} {[x[1] for x in add]}; -{len(gone)} {[x[1] for x in gone]}")


def load_events(cache: Path) -> dict[str, list[Event]]:
    return {k: [Event(**d) for d in v] for k, v in rd(tagged(cache, "events", "json")).items()}


# ================================================================== prices (Binance USD-M only)


def all_events(cache: Path) -> list[Event]:
    ev = load_events(cache)
    return [e for k in CATS for e in ev.get(k, [])]


async def resolve_binance(px: v0.Px, token: str, t0: int) -> tuple[list | None, list[str]]:
    """[venue, symbol, multiplier] of the first Binance USD-M perp live at t0 (registered rule ``perp_live``), and the
    candidates that had bars at t0 but were not live. Only bars closed by t0 are used."""
    d = t0 // DAY_MS * DAY_MS
    dead: list[str] = []
    for mult in MULTIPLIERS:
        sym = f"{mult if mult > 1 else ''}{token}USDT"
        if sym not in px.um:
            continue
        rows = (await px.m1v("binance", sym, d - DAY_MS) or []) + (await px.m1v("binance", sym, d) or [])
        if not rows:
            continue
        b = Bars.from_rows(rows)
        if price_at(b, t0) is None:
            continue
        if perp_live(b, t0):
            return ["binance", sym, mult], dead
        dead.append(f"binance:{sym}")
    return None, dead


async def upbit_krw(px: v0.Px, up: Http, market: str, t0: int) -> list | None:
    """Last 1m Upbit candle of ``market`` (e.g. KRW-XRP) that opened at or before t0 - 1 min (closed by t0):
    [open_ms, close]; None if the market has no candle in the 6 h before t0 (not listed then, or delisted and no
    longer served). Used before t0 (registered check) and at t0 + 24 h / 72 h (post-review identity check)."""
    p = px.cache / "px" / "upbit" / market / f"{t0 // MIN_MS}.json"

    async def go() -> list | None:
        to = datetime.fromtimestamp((t0 - MIN_MS) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        f = await up.get_json("/v1/candles/minutes/1", params={"market": market, "to": to, "count": 1})
        if not isinstance(f.data, list) or not f.data:
            return None
        c = f.data[0]
        ms = int(datetime.fromisoformat(str(c["candle_date_time_utc"])).replace(tzinfo=UTC).timestamp() * 1000)
        return [ms, float(c["trade_price"])] if t0 - ms <= 6 * HOUR_MS else None

    return await px.fx.get(p, go)


async def stage_prices(cache: Path, sample: int) -> None:
    for name in ("um_symbols.json", "spot_symbols.json"):  # reuse the stage-0 listing (archive symbols incl. delisted)
        if not (cache / name).exists() and (STAGE0_CACHE / name).exists():
            wr(cache / name, rd(STAGE0_CACHE / name))
    _, rt = build_server()
    fx = v0.Fetcher(workers=8)
    px = v0.Px(cache, rt, fx)
    up = Http("upbit_quote", "https://api.upbit.com", rate=5, burst=5, timeout=30, retries=2)
    t_start = time.time()
    try:
        await px.symbols()
        evs = all_events(cache)
        if sample:
            evs = evs[:: max(1, len(evs) // sample)][:sample]
        res_p = cache / "resolve.json"
        resolved: dict[str, Any] = rd(res_p) if res_p.exists() else {}
        print(f"events: {len(evs)}; um symbols {len(px.um)}, spot symbols {len(px.spot)}", flush=True)

        async def one(e: Event) -> None:
            key = f"{e.token}@{e.ms}"
            if key not in resolved:
                r, dead = await resolve_binance(px, e.token, e.ms)
                resolved[key] = {"r": r, "dead": dead}
            r = resolved[key]["r"]
            if not r:
                return
            _, sym, _ = r
            d0 = e.ms // DAY_MS * DAY_MS
            jobs = [px.m1("binance", sym, d0 + k * DAY_MS) for k in range(-1, 6)]
            jobs += [px.m1("binance", "BTCUSDT", d0 + k * DAY_MS) for k in range(0, 6)]
            if f"{e.token}USDT" in px.spot:
                jobs.append(px.m1("binance_spot", f"{e.token}USDT", d0))
            for y, m in v0.months_between(e.ms - 122 * DAY_MS, e.ms + 6 * DAY_MS):
                jobs.append(px.funding("binance", sym, y, m))
            for y, m in v0.months_between(e.ms - 122 * DAY_MS, e.ms - 3 * DAY_MS):
                jobs.append(px.h1("binance", sym, y, m))
            if e.category != "listing":  # the KRW market exists before t0 only for warnings and delistings
                jobs.append(upbit_krw(px, up, f"KRW-{e.token}", e.ms))
                jobs.append(upbit_krw(px, up, "KRW-USDT", e.ms))
            await asyncio.gather(*jobs)
            # post-review identity check (token identity is static, so a price after t0 is not lookahead for the
            # trade): Upbit KRW-X and KRW-BTC at t0 + 24 h, then t0 + 72 h if the first is missing
            for lag in COLL_LAGS:
                kx = await upbit_krw(px, up, f"KRW-{e.token}", e.ms + lag)
                if kx:
                    await upbit_krw(px, up, "KRW-BTC", e.ms + lag)
                    break

        batch = 10
        for i in range(0, len(evs), batch):
            await asyncio.gather(*[one(e) for e in evs[i : i + batch]])
            wr(res_p, resolved)
            done = min(len(evs), i + batch)
            el = time.time() - t_start
            print(
                f"  prices {done}/{len(evs)} in {el:.0f}s (eta {el / done * (len(evs) - done):.0f}s); files fetched {fx.fetched}; "
                f"errors {dict(fx.errors)}; rss={rss_mb():.0f}MB",
                flush=True,
            )
        n_ok = sum(1 for e in evs if (resolved.get(f"{e.token}@{e.ms}") or {}).get("r"))
        print(
            f"prices done: {n_ok}/{len(evs)} events have a live Binance USD-M perp at t0; {time.time() - t_start:.0f}s rss={rss_mb():.0f}MB"
        )
    finally:
        await px.aclose()
        await up.aclose()
        await rt.aclose()


# ================================================================== seconds (aggTrades -> 1 s bars)


def parse_agg_window(blob: bytes, a_ms: int, z_ms: int) -> list[list[float]]:
    """Trades ``[time_ms, price, qty, is_buyer_maker]`` with a_ms <= time < z_ms from a USD-M ``aggTrades`` daily zip,
    streaming the CSV (``agg_trade_id, price, quantity, first_trade_id, last_trade_id, transact_time,
    is_buyer_maker``; header optional; microsecond times converted). is_buyer_maker = 1: the aggressor sold."""
    zf = zipfile.ZipFile(io.BytesIO(blob))
    name = next(n for n in zf.namelist() if n.endswith(".csv"))
    out_rows: list[list[float]] = []
    with zf.open(name) as fh:
        for raw in io.TextIOWrapper(fh, encoding="utf-8"):
            parts = raw.split(",")
            if len(parts) < 6 or not parts[0][:1].isdigit():
                continue
            ts = int(parts[5])
            if ts > 10**14:
                ts //= 1000
            if a_ms <= ts < z_ms:
                side = parts[6].strip().lower() in ("true", "1") if len(parts) > 6 else None
                if side is None:
                    raise ValueError("aggTrades row without is_buyer_maker")
                out_rows.append([ts, float(parts[1]), float(parts[2]), 1.0 if side else 0.0])
    return out_rows


async def stage_seconds(cache: Path, sample: int) -> None:
    _, rt = build_server()
    vis = rt.binance_vision.http
    resolved = rd(cache / "resolve.json")
    evs = [e for e in all_events(cache) if (resolved.get(f"{e.token}@{e.ms}") or {}).get("r")]
    evs = [
        e
        for e in evs
        if not (cache / "sec" / resolved[f"{e.token}@{e.ms}"]["r"][1] / f"{e.ms}.json").exists()
        or (e.category == "listing" and not (cache / "sec2" / resolved[f"{e.token}@{e.ms}"]["r"][1] / f"{e.ms}.json").exists())
    ]
    print(f"events needing aggTrades: {len(evs)}", flush=True)
    evs.sort(key=lambda e: ({"listing": 0, "delist": 1, "warning": 2}[e.category], -e.ms))
    if sample:
        evs = evs[:: max(1, len(evs) // sample)][:sample]
    t_start = time.time()
    fetched = mb = 0.0
    errors: Counter[str] = Counter()
    sem = asyncio.Semaphore(2)

    async def one(e: Event) -> None:
        nonlocal fetched, mb
        sym = resolved[f"{e.token}@{e.ms}"]["r"][1]
        p = cache / "sec" / sym / f"{e.ms}.json"
        p2 = cache / "sec2" / sym / f"{e.ms}.json"  # side-aware bars (listings: H1 taker execution)
        need2 = e.category == "listing" and not p2.exists()
        if p.exists() and not need2:
            return
        a, z = e.ms + SEC_WIN[0], e.ms + SEC_WIN[1]
        days = sorted({a // DAY_MS * DAY_MS, (z - 1) // DAY_MS * DAY_MS})
        trades: list[list[float]] = []
        async with sem:
            for d in days:
                name = f"{sym}-aggTrades-{ymd(d)}.zip"
                try:
                    f = await vis.get_bytes(f"/data/futures/um/daily/aggTrades/{sym}/{name}", tool="um_agg_trades_daily")
                except ProviderError as ex:
                    errors[ex.reason] += 1
                    if errors[ex.reason] <= 5:
                        print(f"  aggTrades {name}: {ex.reason} {str(ex.detail)[:120]}", flush=True)
                    if ex.reason == "data_unavailable":
                        wr(p, None)
                        if e.category == "listing":
                            wr(p2, None)
                    return
                fetched += 1
                mb += len(f.data) / 1e6
                trades += await asyncio.to_thread(parse_agg_window, f.data, a, z)
                del f
        bars = second_bars(trades, a, z)
        del trades
        if e.category == "listing":
            wr(p2, bars)
        if not p.exists():  # run-1 files stay untouched; new events get the same 7-column bars
            wr(p, [r[:7] for r in bars])

    batch = 4
    for i in range(0, len(evs), batch):
        await asyncio.gather(*[one(e) for e in evs[i : i + batch]])
        done = min(len(evs), i + batch)
        el = time.time() - t_start
        print(
            f"  seconds {done}/{len(evs)} in {el:.0f}s (eta {el / done * (len(evs) - done):.0f}s); zips {fetched:.0f} ({mb:.0f} MB); "
            f"errors {dict(errors)}; rss={rss_mb():.0f}MB",
            flush=True,
        )
    await rt.aclose()
    print(f"seconds done: {len(evs)} events; {time.time() - t_start:.0f}s; rss={rss_mb():.0f}MB", flush=True)


# ================================================================== measurement (cache only)


def blocked_times(cache: Path) -> dict[str, list[int]]:
    by: dict[str, list[int]] = defaultdict(list)
    for e in all_events(cache):
        by[e.token].append(e.ms)
    p = STAGE0_CACHE / "events.json"
    if p.exists():
        for lst in rd(p).values():
            for d in lst:
                by[str(d["token"])].append(int(d["ms"]) if d.get("precise", True) else int(d["ms"]) + DAY_MS)
    return by


def bars_1m(cache: Path, sym: str, d0: int, days: range) -> Bars | None:
    rows: list = []
    for k in days:
        rows += v0.cached(cache, "binance", sym, "1m", ymd(d0 + k * DAY_MS)) or []
    return Bars.from_rows(rows) if rows else None


def grid_keys(cat: str) -> list[tuple[int, int]]:
    for _h, (c, _s, _k, ds, hs) in HYP.items():
        if c == cat and ds:
            return [(d, h) for d in ds for h in hs]
    return []


def measure(cache: Path, e: Event, r: list, blocked: list[int]) -> dict[str, Any]:
    venue, sym, mult = r
    t0, d0 = e.ms, e.ms // DAY_MS * DAY_MS
    rec: dict[str, Any] = {"token": e.token, "ms": t0, "category": e.category, "cluster": e.cluster, "sym": sym, "p0": None}
    b = bars_1m(cache, sym, d0, range(-1, 6))
    if b is None:
        return rec
    p0 = price_at(b, t0)
    rec["p0"] = p0
    if p0 is None:
        return rec
    sp = v0.cached(cache, "binance_spot", f"{e.token}USDT", "1m", ymd(d0))
    ps = price_at(Bars.from_rows(sp), t0, max_stale_ms=6 * HOUR_MS) if sp else None
    rec["spot_ratio"] = (p0 / mult) / ps if ps else None
    uk = cache / "px" / "upbit" / f"KRW-{e.token}" / f"{t0 // MIN_MS}.json"
    uu = cache / "px" / "upbit" / "KRW-USDT" / f"{t0 // MIN_MS}.json"
    k1, k2 = (rd(uk) if uk.exists() else None), (rd(uu) if uu.exists() else None)
    rec["upbit_ratio"] = (p0 / mult) / (k1[1] / k2[1]) if k1 and k2 and k1[1] > 0 and k2[1] > 0 else None
    # post-review identity check at t0 + 24 h (or + 72 h): token price in BTC, perp vs Upbit KRW market
    rec["x24_ratio"] = rec["x24_lag_h"] = None
    btc_b = bars_1m(cache, "BTCUSDT", d0, range(0, 6))
    for lag in COLL_LAGS:
        kx_p = cache / "px" / "upbit" / f"KRW-{e.token}" / f"{(t0 + lag) // MIN_MS}.json"
        kb_p = cache / "px" / "upbit" / "KRW-BTC" / f"{(t0 + lag) // MIN_MS}.json"
        kx, kb = (rd(kx_p) if kx_p.exists() else None), (rd(kb_p) if kb_p.exists() else None)
        if not (kx and kb and btc_b is not None):
            continue
        pp, pb = price_at(b, t0 + lag), price_at(btc_b, t0 + lag)
        x = btc_cross_ratio(pp / mult if pp else None, pb, kx[1], kb[1])  # type: ignore[arg-type]
        if x is not None:
            rec["x24_ratio"], rec["x24_lag_h"] = x, lag // HOUR_MS
            break
    rec["react"] = {k: reaction(b, t0, dt) for k, dt in REACT.items()}
    rec["pre60"] = reaction(b, t0 - HOUR_MS, HOUR_MS)
    rec["flat60"] = flat_share(b, t0)
    fund, partial = v0.fund_rows(cache, venue, sym, t0 - DAY_MS, t0 + 5 * DAY_MS)
    rec["funding_missing"], rec["funding_partial"] = fund is None, partial
    fund = [(int(a), float(x)) for a, x in (fund or [])]
    btc = btc_b
    trades: dict[str, Any] = {}  # H2-H4 are shorts (H2: the fade after a listing); H1 (long) below
    for d, hh in grid_keys(e.category):
        ent, ex = t0 + d * MIN_MS, t0 + d * MIN_MS + hh * HOUR_MS
        for tag, cs, stop in (("base", COST, None), ("stress", COST_STRESS, None), ("stop", COST, STOP)):
            tr = short_trade(b, ent, ex, cost_side=cs, funding=fund, stop=stop, sign=1.0)
            trades[f"d{d}_h{hh}_{tag}"] = tr.__dict__ if tr else None
        if btc is not None:
            tb = short_trade(btc, ent, ex, cost_side=0.0, sign=1.0)
            trades[f"d{d}_h{hh}_btc"] = tb.gross if tb else None
    if e.category == "listing":  # H1 fallback on 1m bars: long t0 + 1 min -> t0 + 15 min
        tr = short_trade(b, t0 + MIN_MS, t0 + 15 * MIN_MS, cost_side=COST, funding=fund, sign=-1.0)
        trades["m1_m15_base"] = tr.__dict__ if tr else None
    rec["trades"] = trades
    # placebo H2-H4: random hours on 1h bars, same direction (short), holds 24 / 72 h
    pl: dict[str, list[float]] = {"h24": [], "h72": [], "m15": []}
    h_rows: list = []
    for y, m in v0.months_between(t0 - 122 * DAY_MS, t0 - 3 * DAY_MS):
        h_rows += v0.cached(cache, venue, sym, "1h", f"{y:04d}-{m:02d}") or []
    if h_rows:
        hb = Bars.from_rows(h_rows, step=HOUR_MS)
        pf = [(int(a), float(x)) for a, x in (v0.fund_rows(cache, venue, sym, t0 - 122 * DAY_MS, t0)[0] or [])]
        for ts in placebo_times(f"{e.token}@{t0}", t0, N_PLACEBO, blocked=[x for x in blocked if x != t0]):
            for hh in (24, 72):
                tr = short_trade(hb, ts, ts + hh * HOUR_MS, cost_side=COST, funding=pf, sign=1.0, max_stale_ms=2 * HOUR_MS)
                if tr and not tr.truncated:
                    pl[f"h{hh}"].append(tr.net)
    if e.category == "listing":  # placebo H1: random minutes in [t0 - 23 h, t0 - 2 h], long 15 min on 1m bars
        for ts in placebo_times(f"H1:{e.token}@{t0}", t0, N_PLACEBO, grid_ms=MIN_MS, lo_ms=23 * HOUR_MS, hi_ms=2 * HOUR_MS):
            tr = short_trade(b, ts, ts + 15 * MIN_MS, cost_side=COST, funding=fund, sign=-1.0)
            if tr and not tr.truncated:
                pl["m15"].append(tr.net)
    rec["placebo"] = pl
    # seconds
    sp_ = cache / "sec" / sym / f"{t0}.json"
    srows = rd(sp_) if sp_.exists() else None
    rec["sec_status"] = "missing" if not sp_.exists() else ("no_data" if not srows else "ok")
    if srows:
        sb = Bars.from_rows(srows, step=1000)
        s0 = price_at(sb, t0, max_stale_ms=5 * MIN_MS)
        rec["sec_p0"] = s0
        rec["sec_react"] = {k: reaction(sb, t0, dt, max_stale_ms=5 * MIN_MS) for k, dt in SEC_REACT.items()}
        st: dict[str, Any] = {}
        for ds in SEC_D:
            pe = vwap_fill(srows, t0 + ds * 1000, t0 + ds * 1000 + 5000)
            st[f"fill{ds}"] = pe
            for hm in SEC_H:
                for tag, cs in (("base", COST), ("stress", COST_STRESS)):
                    tr = (
                        short_trade(
                            sb,
                            t0 + ds * 1000,
                            t0 + hm * MIN_MS,
                            cost_side=cs,
                            funding=fund,
                            sign=-1.0,
                            entry_price=pe,
                            max_stale_ms=5 * MIN_MS,
                        )
                        if pe
                        else None
                    )
                    st[f"s{ds}_m{hm}_{tag}"] = tr.__dict__ if tr else None
        # post-review execution model (listings): entry from aggressive buys only, exit at the last aggressive sell
        p2 = cache / "sec2" / sym / f"{t0}.json"
        rows2 = rd(p2) if e.category == "listing" and p2.exists() else None
        rec["sec2_status"] = None if e.category != "listing" else ("missing" if not p2.exists() else ("no_data" if not rows2 else "ok"))
        if rows2:
            for ds in SEC_D:
                for mode, first in (("tv", False), ("tf", True)):  # VWAP of taker buys in 5 s / first taker buy
                    pe = taker_buy_fill(rows2, t0 + ds * 1000, t0 + ds * 1000 + 5000, first=first)
                    st[f"fill{ds}_{mode}"] = pe
                    for hm in SEC_H:
                        px_ex = taker_sell_exit(rows2, t0 + hm * MIN_MS)
                        for tag, cs in (("base", COST), ("fee", FEE), ("stress", COST_STRESS)):
                            tr = (
                                short_trade(
                                    sb,
                                    t0 + ds * 1000,
                                    t0 + hm * MIN_MS,
                                    cost_side=cs,
                                    funding=fund,
                                    sign=-1.0,
                                    entry_price=pe,
                                    exit_price=px_ex,
                                    max_stale_ms=5 * MIN_MS,
                                )
                                if pe and px_ex
                                else None
                            )
                            st[f"s{ds}_m{hm}_{mode}_{tag}"] = tr.__dict__ if tr else None
        rec["sec_trades"] = st
    return rec


def measure_all(cache: Path) -> list[dict[str, Any]]:
    resolved = rd(cache / "resolve.json")
    blocked = blocked_times(cache)
    recs = []
    for e in all_events(cache):
        res = resolved.get(f"{e.token}@{e.ms}")
        if res is None:
            continue  # not fetched (sample run)
        r = res.get("r")
        rec = (
            measure(cache, e, r, blocked[e.token])
            if r
            else {"token": e.token, "ms": e.ms, "category": e.category, "cluster": e.cluster, "p0": None}
        )
        rec["no_perp"] = not r
        rec["dead"] = res.get("dead") or []
        recs.append(rec)
    wr(tagged(cache, "measured", "json"), recs)
    return recs


def collision(rec: dict[str, Any]) -> str:
    """``fail`` if a check is outside [0.8; 1.25]; ``checked`` if at least one check exists; ``none`` otherwise.
    Checks: Binance spot at t0, Upbit KRW before t0 (registered) and, after the critic's review, the token's price in
    BTC on Upbit vs the perp at t0 + 24 h / 72 h (``x24_ratio``; identity is static, so this is not lookahead)."""
    rs = [x for x in (rec.get("spot_ratio"), rec.get("upbit_ratio"), rec.get("x24_ratio")) if x is not None]
    if any(not (0.8 <= x <= 1.25) for x in rs):
        return "fail"
    return "checked" if rs else "none"


def value(rec: dict[str, Any], key: str) -> float | None:
    if rec.get("p0") is None or collision(rec) == "fail":
        return None
    if key.startswith("s") and key[1].isdigit():
        tr = (rec.get("sec_trades") or {}).get(key)
    else:
        tr = (rec.get("trades") or {}).get(key)
    return None if tr is None else float(tr["net"])


def pool(recs: list[dict[str, Any]], cat: str, key: str, pred: Any = None) -> tuple[list[float], list[str], list[dict]]:
    vals, cl, rr = [], [], []
    for r in recs:
        if r["category"] != cat or (pred and not pred(r)):
            continue
        v = value(r, key)
        if v is None:
            continue
        vals.append(v)
        cl.append(r["cluster"])
        rr.append(r)
    return vals, cl, rr


def primary_key(h: str, recs: list[dict[str, Any]]) -> str:
    """H1 uses the 1 s entry when aggTrades exist for the listing pool; otherwise the registered 1m fallback."""
    if h != "H1":
        return HYP[h][2]
    n_sec = sum(1 for r in recs if r["category"] == "listing" and r.get("sec_status") == "ok")
    return HYP[h][2] if n_sec else "m1_m15_base"


def placebo_of(rec: dict[str, Any], key: str) -> float | None:
    pl = rec.get("placebo") or {}
    lst = pl.get("m15") if key.startswith(("s", "m1_")) else pl.get("h72" if "_h72_" in key else "h24")
    return statistics.fmean(lst) if lst else None


def ci_row(vals: list[float], cl: list[str]) -> str:
    return v0.ci_row(vals, cl)


# ================================================================== stage0 (gate: n, SD, MDE only)


def stage_stage0(cache: Path) -> None:
    recs = measure_all(cache)
    L: list[str] = []
    out(L, f"# news events 0c stage 0 {datetime.now(UTC):%Y-%m-%d %H:%M} UTC (no means)")
    for cat in CATS:
        rr = [r for r in recs if r["category"] == cat]
        live = [r for r in rr if not r["no_perp"]]
        priced = [r for r in live if r.get("p0") is not None]
        out(
            L,
            f"{cat}: events {len(rr)}; live Binance USD-M perp at t0 {len(live)}; dead/settled candidates {sum(1 for r in rr if r['no_perp'] and r['dead'])}; "
            f"priced {len(priced)}; collision {dict(Counter(collision(r) for r in priced))}; flat60>50% {sum(1 for r in priced if (r.get('flat60') or 0) > 0.5)}; "
            f"seconds {dict(Counter(r.get('sec_status') for r in priced))}; funding missing {sum(1 for r in priced if r.get('funding_missing'))}",
        )
    for h, (cat, _s, _k, _d, _hs) in HYP.items():
        key = primary_key(h, recs)
        vals, cl, _ = pool(recs, cat, key)
        sd, k = cluster_sd(vals, cl) if len(vals) > 1 else (float("nan"), 0)
        m = mde_clusters(sd, k)
        oos = pool(recs, cat, key, pred=lambda r: r["ms"] < PRE_CUT)
        k_oos = len(set(oos[1]))
        out(
            L,
            f"{h} {cat} {key}: n {len(vals)} in {k} clusters (t0 < 2026: {k_oos}); SD(cluster means) {sd:.3f}; MDE {m:.3f} -> "
            f"gate {'PASS' if m <= GATE_MDE else 'FAIL'}; min-n {'OK' if k >= MIN_K and k_oos >= MIN_K_OOS else 'NOT MET'}",
        )
    out(L, "\nper-event perp and price level (collision review; no outcomes):")
    for r in sorted(recs, key=lambda r: (r["category"], r["ms"])):
        if r.get("p0") is None:
            continue
        sr, ur, xr = r.get("spot_ratio"), r.get("upbit_ratio"), r.get("x24_ratio")
        out(
            L,
            f"  {r['category']:7s} {r['token']:8s} {ymdhm(r['ms'])} {r['sym']:16s} p0 {r['p0']:.6g} spot/binance {None if sr is None else round(sr, 3)} "
            f"upbit {None if ur is None else round(ur, 3)} upbit-in-BTC +{r.get('x24_lag_h')}h {None if xr is None else round(xr, 3)} "
            f"-> {collision(r)}; flat60 {r.get('flat60', float('nan')):.2f}",
        )
    tagged(cache, "stage0", "txt").write_text("\n".join(L) + "\n")
    print(f"rss={rss_mb():.0f}MB")


# ================================================================== report


def verdict_for(h: str, recs: list[dict[str, Any]], p_holm: float) -> dict[str, Any]:
    cat = HYP[h][0]
    key = primary_key(h, recs)
    vals, cl, rr = pool(recs, cat, key)
    k = len(set(cl))
    sd, _ = cluster_sd(vals, cl) if len(vals) > 1 else (float("nan"), 0)
    mde = mde_clusters(sd, k)
    m, lo, hi = cluster_bootstrap(vals, cl) if vals else (float("nan"),) * 3
    med = statistics.median(vals) if vals else float("nan")
    pv = [(v, placebo_of(r, key), c) for v, r, c in zip(vals, rr, cl, strict=True)]
    pv = [x for x in pv if x[1] is not None]
    md = lo_d = hi_d = float("nan")
    if len(pv) > 1:
        md, lo_d, hi_d = paired_diff_bootstrap([x[0] for x in pv], [x[1] for x in pv], [x[2] for x in pv])
    ov, oc, _ = pool(recs, cat, key, pred=lambda r: r["ms"] < PRE_CUT)
    k_oos = len(set(oc))
    m_oos = statistics.fmean(ov) if ov else float("nan")
    p_oos = one_sided_p(ov, oc) if len(ov) > 1 else float("nan")
    gate = mde <= GATE_MDE
    minn = k >= MIN_K and k_oos >= MIN_K_OOS
    if gate and minn and p_holm < ALPHA and lo_d > 0 and m_oos > 0 and p_oos < ALPHA:
        v = "confirmed"
    elif k >= MIN_K and not (lo_d > 0) and lo <= 0 <= hi and med <= 0:
        v = "rejected"
    else:
        v = "inconclusive"
    return dict(
        key=key,
        n=len(vals),
        k=k,
        mean=m,
        lo=lo,
        hi=hi,
        med=med,
        mde=mde,
        gate=gate,
        minn=minn,
        diff=md,
        lo_d=lo_d,
        hi_d=hi_d,
        k_oos=k_oos,
        m_oos=m_oos,
        p_oos=p_oos,
        verdict=v,
        n_placebo=len(pv),
    )


def stage_report(cache: Path) -> None:
    recs = measure_all(cache)
    L: list[str] = []
    out(L, f"# news events 0c (Upbit) report {datetime.now(UTC):%Y-%m-%d %H:%M} UTC; t0 in [{ymd(START)}; {ymd(END)})")
    out(
        L,
        f"costs: fee {FEE:.4%}/side, slippage {SLIP:.4%}/side (stress x3); stop {STOP:.0%}; placebo {N_PLACEBO}/event; gate MDE <= {GATE_MDE:.0%}",
    )
    # ---- coverage
    out(L, "\n## coverage")
    for cat in CATS:
        rr = [r for r in recs if r["category"] == cat]
        pr = [r for r in rr if r.get("p0") is not None]
        out(
            L,
            f"{cat}: events {len(rr)}; no live Binance perp {sum(r['no_perp'] for r in rr)}; priced {len(pr)}; collisions "
            f"{[r['token'] for r in pr if collision(r) == 'fail']}; unchecked {sum(collision(r) == 'none' for r in pr)}; seconds ok "
            f"{sum(r.get('sec_status') == 'ok' for r in pr)}; truncated d15/d60 primary "
            f"{sum(1 for r in pr if any((t or {}).get('truncated') for t in (r.get('trades') or {}).values() if isinstance(t, dict)))}",
        )
    # ---- reactions
    out(L, "\n## reactions from t0, 1m bars (mean / median)")
    for cat in CATS:
        rr = [r for r in recs if r["category"] == cat and r.get("p0") is not None and collision(r) != "fail"]
        cells = []
        for h in ["pre60", *REACT]:
            xs = [r["pre60"] if h == "pre60" else r["react"].get(h) for r in rr]
            xs = [x for x in xs if x is not None]
            cells.append(f"{h} {fmt(statistics.fmean(xs)) if xs else '—'}/{fmt(statistics.median(xs)) if xs else '—'}")
        out(L, f"{cat} (n {len(rr)}): " + "; ".join(cells))
    out(L, "\n## reactions from t0, 1 s bars from aggTrades (mean / median; n)")
    for cat in CATS:
        rr = [r for r in recs if r["category"] == cat and r.get("sec_status") == "ok" and collision(r) != "fail" and r.get("sec_p0")]
        cells = []
        for h in SEC_REACT:
            xs = [r["sec_react"].get(h) for r in rr]
            xs = [x for x in xs if x is not None]
            cells.append(f"{h} {fmt(statistics.fmean(xs)) if xs else '—'}/{fmt(statistics.median(xs)) if xs else '—'}")
        out(L, f"{cat} (n {len(rr)}): " + "; ".join(cells))
        if not rr:
            continue
        for hk in ("+15m", "+60m"):
            parts = []
            for dk in ("+5s", "+10s", "+30s", "+60s"):
                ok = [r for r in rr if r["sec_react"].get(dk) is not None and r["sec_react"].get(hk) is not None]
                if len(ok) > 1:
                    s, lo, hi = move_share_bootstrap([r["sec_react"][dk] for r in ok], [r["sec_react"][hk] for r in ok])
                    parts.append(f"{dk} {s * 100:.0f}% [{lo * 100:.0f}; {hi * 100:.0f}]")
            out(L, f"  share of the {hk} move done by: " + "; ".join(parts) + f" (n {len(rr)})")
        for ds in SEC_D:
            cells = []
            for hm in SEC_H:
                xs = []
                for r in rr:
                    pe = (r.get("sec_trades") or {}).get(f"fill{ds}")
                    p1 = r["sec_react"].get(f"+{hm}m")
                    if pe and p1 is not None:
                        xs.append(r["sec_p0"] * (1 + p1) / pe - 1)
                cells.append(
                    f"to +{hm}m {fmt(statistics.fmean(xs)) if xs else '—'}/{fmt(statistics.median(xs)) if xs else '—'} (n {len(xs)})"
                )
            out(L, f"  price move left after a fill at t0+{ds}s (VWAP 5 s; mean/median, gross): " + "; ".join(cells))
    # ---- speed (minutes; share of the 24 h move)
    out(L, "\n## speed: share of the 24 h move done by +1 / +5 / +15 min (1m bars)")
    for cat in CATS:
        rr = [
            r
            for r in recs
            if r["category"] == cat and r.get("p0") is not None and collision(r) != "fail" and r["react"].get("+24h") is not None
        ]
        parts = []
        for h in ("+1m", "+5m", "+15m", "+60m"):
            ok = [r for r in rr if r["react"].get(h) is not None]
            if len(ok) > 1:
                s, lo, hi = move_share_bootstrap([r["react"][h] for r in ok], [r["react"]["+24h"] for r in ok])
                parts.append(f"{h} {s * 100:.0f}% [{lo * 100:.0f}; {hi * 100:.0f}]")
        out(L, f"{cat} (n {len(rr)}): " + "; ".join(parts))
    # ---- hypotheses
    out(L, "\n## H1-H4 (net on notional, base costs + funding); Holm over the four primary one-sided p")
    raw_p = {}
    for h, (cat, _s, _k, _d, _hs) in HYP.items():
        vals, cl, _ = pool(recs, cat, primary_key(h, recs))
        raw_p[h] = one_sided_p(vals, cl) if len(vals) > 1 else float("nan")
    adj = holm(raw_p)
    verdicts = {}
    for h in HYP:
        res = verdict_for(h, recs, adj[h])
        verdicts[h] = res
        cat = HYP[h][0]
        vals, cl, _ = pool(recs, cat, res["key"])
        out(L, f"{h} {cat} {res['key']}: {ci_row(vals, cl)}")
        out(
            L,
            f"  p one-sided {raw_p[h]:.4f}, Holm {adj[h]:.4f}; MDE {fmt(res['mde'])} gate {'PASS' if res['gate'] else 'FAIL'}; "
            f"event - placebo {fmt(res['diff'])} [{fmt(res['lo_d'])}; {fmt(res['hi_d'])}] (n {res['n_placebo']}); "
            f"t0 < 2026: k {res['k_oos']} mean {fmt(res['m_oos'])} p {res['p_oos']:.4f}; min-n {'OK' if res['minn'] else 'NOT MET'} -> {res['verdict'].upper()}",
        )
        ov, oc, _ = pool(recs, cat, res["key"], pred=lambda r: r["ms"] < PRE_CUT)
        out(L, f"  t0 < 2026 (outside Claude's preliminary measurement): {ci_row(ov, oc)}")
        nv, nc, _ = pool(recs, cat, res["key"], pred=lambda r: r["ms"] >= PRE_CUT)
        out(L, f"  t0 >= 2026 (Claude's period): {ci_row(nv, nc)}")
    vs = [verdicts[h]["verdict"] for h in HYP]
    overall = "confirmed" if "confirmed" in vs else ("rejected" if all(v == "rejected" for v in vs) else "inconclusive")
    per_h = ", ".join(f"{h} {verdicts[h]['verdict']}" for h in HYP)
    out(L, f"STAGE 0c VERDICT: {overall.upper()} ({per_h})")
    # ---- grids
    out(L, "\n## grids (net mean [CI], median; base / stress / stop 25%)")
    for cat in CATS:
        for d, hh in grid_keys(cat):
            cells = []
            for tag in ("base", "stress", "stop"):
                v, c, _ = pool(recs, cat, f"d{d}_h{hh}_{tag}")
                if len(v) > 1:
                    mm, l2, h2 = cluster_bootstrap(v, c)
                    cells.append(f"{tag} {fmt(mm)} [{fmt(l2)}; {fmt(h2)}] med {fmt(statistics.median(v))}")
            out(L, f"{cat} short d{d} h{hh} (n {len(pool(recs, cat, f'd{d}_h{hh}_base')[0])}): " + "; ".join(cells))
    for ds in SEC_D:
        for hm in SEC_H:
            cells = []
            for tag in ("base", "stress"):
                v, c, _ = pool(recs, "listing", f"s{ds}_m{hm}_{tag}")
                if len(v) > 1:
                    mm, l2, h2 = cluster_bootstrap(v, c)
                    cells.append(f"{tag} {fmt(mm)} [{fmt(l2)}; {fmt(h2)}] med {fmt(statistics.median(v))}")
            out(L, f"listing LONG fill t0+{ds}s exit +{hm}m (n {len(pool(recs, 'listing', f's{ds}_m{hm}_base')[0])}): " + "; ".join(cells))
    v, c, _ = pool(recs, "listing", "m1_m15_base")
    out(L, f"listing LONG 1m fallback t0+1m -> +15m: {ci_row(v, c)}")
    for cat in CATS:
        for hh in (24, 72):
            plv = [
                statistics.fmean(r["placebo"][f"h{hh}"])
                for r in recs
                if r["category"] == cat and r.get("placebo") and r["placebo"][f"h{hh}"]
            ]
            out(L, f"placebo {cat} short h{hh}: mean {fmt(statistics.fmean(plv)) if plv else '—'} (n {len(plv)})")
    plv = [statistics.fmean(r["placebo"]["m15"]) for r in recs if r["category"] == "listing" and r.get("placebo") and r["placebo"]["m15"]]
    out(L, f"placebo listing long 15 min: mean {fmt(statistics.fmean(plv)) if plv else '—'} (n {len(plv)})")
    # ---- robustness per hypothesis
    out(L, "\n## robustness (primary trade of each hypothesis)")
    y = lambda a: int(datetime(a, 1, 1, tzinfo=UTC).timestamp() * 1000)  # noqa: E731
    slices = [
        ("2020-2023", lambda r: r["ms"] < y(2024)),
        ("2024-2025", lambda r: y(2024) <= r["ms"] < y(2026)),
        ("2026", lambda r: r["ms"] >= y(2026)),
        ("tickers > 2 chars", lambda r: len(r["token"]) > 2),
        ("no flat bars (flat60 <= 50%)", lambda r: (r.get("flat60") or 0) <= 0.5),
        ("collision-checked only", lambda r: collision(r) == "checked"),
        ("funding complete", lambda r: not r.get("funding_missing") and not r.get("funding_partial")),
    ]
    for h in HYP:
        cat, key = HYP[h][0], verdicts[h]["key"]
        out(L, f"-- {h} {cat} {key}")
        for lab, pr in slices:
            v, c, _ = pool(recs, cat, key, pred=pr)
            out(L, f"  {lab}: {ci_row(v, c)}")
        vals, cl, rr = pool(recs, cat, key)
        if not vals:
            continue
        order = sorted(range(len(vals)), key=lambda i: -vals[i])
        for drop in (1, 3, 5):
            keep = sorted(order[drop:])
            out(L, f"  without top {drop}: {ci_row([vals[i] for i in keep], [cl[i] for i in keep])}")
        out(L, "  top 5: " + ", ".join(f"{rr[i]['token']} {ymd(rr[i]['ms'])} {fmt(vals[i])}" for i in order[:5]))
        out(L, "  bottom 5: " + ", ".join(f"{rr[i]['token']} {ymd(rr[i]['ms'])} {fmt(vals[i])}" for i in order[-5:]))
        if key.startswith("d"):
            trs = [r["trades"][key] for r in rr]
            out(
                L,
                f"  components: gross {fmt(statistics.fmean(t['gross'] for t in trs))}; funding {fmt(statistics.fmean(t['funding'] for t in trs), 2)}; "
                f"cost {fmt(statistics.fmean(t['cost'] for t in trs), 2)}; MAE median {fmt(statistics.median(t['mae'] for t in trs))}, "
                f"p90 {fmt(float(np.quantile([t['mae'] for t in trs], 0.9)))}; share MAE >= 25% {sum(t['mae'] >= STOP for t in trs) / len(trs) * 100:.0f}%; "
                f"truncated {sum(t['truncated'] for t in trs)}",
            )
            bk = key.replace("_base", "_btc")
            hv = [r["trades"][key]["net"] - (r["trades"].get(bk) or 0.0) for r in rr if r["trades"].get(bk) is not None]
            hc = [r["cluster"] for r in rr if r["trades"].get(bk) is not None]
            out(L, f"  BTC-hedged: {ci_row(hv, hc)}")
        pre = [r["pre60"] for r in rr if r.get("pre60") is not None]
        out(
            L,
            f"  move in the 60 min before t0: mean {fmt(statistics.fmean(pre)) if pre else '—'} median {fmt(statistics.median(pre)) if pre else '—'}",
        )
        # 5a
        g: dict[str, list[float]] = defaultdict(list)
        for v_, r in zip(vals, rr, strict=True):
            g[r["token"]].append(v_)
        multi = [(t, v_) for t, v_ in g.items() if len(v_) >= 2 and statistics.stdev(v_) > 0]
        out(
            L,
            f"  5a: tokens {len(g)}, with >= 2 events {len(multi)}: "
            + ", ".join(f"{t} n{len(v_)} {fmt(statistics.fmean(v_))}" for t, v_ in sorted(multi, key=lambda x: -len(x[1]))[:15]),
        )
        if len(multi) >= 2:
            q, df, i2, pq = cochran_q([statistics.fmean(v_) for _, v_ in multi], [statistics.stdev(v_) / len(v_) ** 0.5 for _, v_ in multi])
            out(L, f"  Cochran Q {q:.2f} df {df} I2 {i2:.2f} p {pq:.3f}")
        early = defaultdict(list)
        late = defaultdict(list)
        for v_, r in zip(vals, rr, strict=True):
            (early if r["ms"] < y(2025) else late)[r["token"]].append(v_)
        both = [t for t in early if t in late]
        if len(both) >= 8:
            ranked = sorted(both, key=lambda t: -statistics.fmean(early[t]))
            top = ranked[: max(1, len(ranked) // 4)]
            rest = ranked[len(top) :]
            out(
                L,
                f"  persistence: tokens in both periods {len(both)}; top quartile (pre-2025) late mean {fmt(statistics.fmean(x for t in top for x in late[t]))} "
                f"vs rest {fmt(statistics.fmean(x for t in rest for x in late[t]))}",
            )
        else:
            out(L, f"  persistence: tokens with events both before and after 2025-01-01: {len(both)} (< 8, not testable)")
    exploratory_h1(cache, recs, L)
    post_review(recs, L)
    tagged(cache, "report", "txt").write_text("\n".join(L) + "\n")
    print(f"\nrss={rss_mb():.0f}MB; written {tagged(cache, 'report', 'txt')}", flush=True)


def exploratory_h1(cache: Path, recs: list[dict[str, Any]], L: list[str]) -> None:
    """EXPLORATORY (chosen after seeing the H1 result; no effect on the registered verdict): timing of t0, fill
    window, capacity in the fill window, higher slippage."""
    out(L, "\n## EXPLORATORY (after seeing results): H1 timing, fill window, capacity, slippage")
    rr = [r for r in recs if r["category"] == "listing" and r.get("sec_status") == "ok" and value(r, "s10_m15_base") is not None]
    pre30, pre5, empty, qv, n_tr, first_move = [], [], 0, [], [], []
    for r in rr:
        rows = rd(cache / "sec" / r["sym"] / f"{r['ms']}.json")
        t0 = r["ms"]
        b = Bars.from_rows(rows, step=1000)
        p0 = price_at(b, t0, max_stale_ms=5 * MIN_MS)
        pa, pb = price_at(b, t0 - 30_000, max_stale_ms=5 * MIN_MS), price_at(b, t0 - 5000, max_stale_ms=5 * MIN_MS)
        if p0 and pa:
            pre30.append(p0 / pa - 1)
        if p0 and pb:
            pre5.append(p0 / pb - 1)
        win = [x for x in rows if t0 + 10_000 <= x[0] < t0 + 15_000]
        empty += not win
        qv.append(sum(float(x[6]) for x in win))
        n_tr.append(len(win))
        if p0:  # first second after t0 with a price 2 % above p0
            hit = next((x[0] - t0 for x in rows if x[0] >= t0 and x[2] >= p0 * 1.02), None)
            if hit is not None:
                first_move.append(hit / 1000)
    q = np.quantile
    out(
        L,
        f"listing n {len(rr)}: price change t0-30s -> t0 mean {fmt(statistics.fmean(pre30))} median {fmt(statistics.median(pre30))}; t0-5s -> t0 mean {fmt(statistics.fmean(pre5))} median {fmt(statistics.median(pre5))}",
    )
    out(
        L,
        f"  first second with a high >= p0 + 2%: n {len(first_move)} of {len(rr)}; quantiles 10/25/50/75/90% {[round(float(x), 1) for x in q(first_move, [0.1, 0.25, 0.5, 0.75, 0.9])]} s after t0",
    )
    out(
        L,
        f"  fill window [t0+10s; t0+15s): empty in {empty} events (fallback to the next trade); quote volume USDT quantiles 10/50/90% {[round(float(x)) for x in q(qv, [0.1, 0.5, 0.9])]}",
    )
    vals_b, cl, rb = pool(recs, "listing", "s10_m15_base")
    for slip in (0.005, 0.01):
        extra = 2 * (slip - SLIP)
        out(L, f"  H1 with slippage {slip:.1%}/side: {ci_row([v - extra for v in vals_b], cl)}")
    for lab, a, z in (("2024", 2024, 2025), ("2025", 2025, 2026), ("2026", 2026, 2027)):
        lo_ms, hi_ms = (int(datetime(x, 1, 1, tzinfo=UTC).timestamp() * 1000) for x in (a, z))
        v = [x for x, r in zip(vals_b, rb, strict=True) if lo_ms <= r["ms"] < hi_ms]
        c = [x for x, r in zip(cl, rb, strict=True) if lo_ms <= r["ms"] < hi_ms]
        out(L, f"  H1 {lab}: {ci_row(v, c)}")
    trim = sorted(vals_b)[int(0.05 * len(vals_b)) : len(vals_b) - int(0.05 * len(vals_b))]
    out(L, f"  H1 5%-trimmed mean {fmt(statistics.fmean(trim))} (n {len(trim)})")


def post_review(recs: list[dict[str, Any]], L: list[str]) -> None:
    """POST-HOC rows asked for by the critic's review 2026-10-18 (after the results of run 1): H1 with taker-side
    execution, the collision check at t0 + 24 h for the top contributors, funding of H2 in detail."""
    out(L, "\n## POST-HOC (critic's review 2026-10-18): H1 with taker-side execution (aggressive buys in, aggressive sells out)")
    y = lambda a: int(datetime(a, 1, 1, tzinfo=UTC).timestamp() * 1000)  # noqa: E731
    n_ok = sum(1 for r in recs if r["category"] == "listing" and r.get("sec2_status") == "ok")
    out(L, f"listings with side-aware 1 s bars: {n_ok}")
    for mode, lab in (("tv", "VWAP of aggressive buys in [t0+d; t0+d+5s)"), ("tf", "first aggressive buy at/after t0+d")):
        out(L, f"-- entry: {lab}; exit: last aggressive sell before the exit time")
        for ds in SEC_D:
            for hm in SEC_H:
                cells = []
                for tag in ("fee", "base", "stress"):
                    v, c, _ = pool(recs, "listing", f"s{ds}_m{hm}_{mode}_{tag}")
                    if len(v) > 1:
                        mm, l2, h2 = cluster_bootstrap(v, c)
                        cells.append(f"{tag} {fmt(mm)} [{fmt(l2)}; {fmt(h2)}] med {fmt(statistics.median(v))}")
                n_ = len(pool(recs, "listing", f"s{ds}_m{hm}_{mode}_base")[0])
                out(L, f"  LONG t0+{ds}s -> +{hm}m (n {n_}): " + "; ".join(cells))
    for key in ("s10_m15_tv_base", "s10_m15_tf_base"):
        vals, cl, rr = pool(recs, "listing", key)
        if len(vals) < 2:
            continue
        out(L, f"-- {key}: {ci_row(vals, cl)}; one-sided p {one_sided_p(vals, cl):.4f}")
        pv = [(v, placebo_of(r, "s10_m15_base"), c) for v, r, c in zip(vals, rr, cl, strict=True)]
        pv = [x for x in pv if x[1] is not None]
        if len(pv) > 1:
            md, lo_d, hi_d = paired_diff_bootstrap([x[0] for x in pv], [x[1] for x in pv], [x[2] for x in pv])
            out(L, f"  event - placebo {fmt(md)} [{fmt(lo_d)}; {fmt(hi_d)}] (placebo enters at a 1m close: controls costs and drift only)")
        for lab, pr in (
            ("t0 < 2026", lambda r: r["ms"] < PRE_CUT),
            ("2026", lambda r: r["ms"] >= PRE_CUT),
            ("2020-2023", lambda r: r["ms"] < y(2024)),
            ("2024-2025", lambda r: y(2024) <= r["ms"] < y(2026)),
            ("collision-checked only", lambda r: collision(r) == "checked"),
        ):
            v, c, _ = pool(recs, "listing", key, pred=pr)
            extra = f"; one-sided p {one_sided_p(v, c):.4f}" if len(v) > 1 else ""
            out(L, f"  {lab}: {ci_row(v, c)}{extra}")
        order = sorted(range(len(vals)), key=lambda i: -vals[i])
        for drop in (1, 3, 5):
            keep = sorted(order[drop:])
            out(L, f"  without top {drop}: {ci_row([vals[i] for i in keep], [cl[i] for i in keep])}")
        for slip in (0.005, 0.01):
            extra = 2 * (slip - SLIP)
            out(L, f"  slippage {slip:.1%}/side: {ci_row([v - extra for v in vals], cl)}")
    # top 10 contributors of the registered H1 row and of the taker row, with every identity check by name
    for key in ("s10_m15_base", "s10_m15_tv_base"):
        vals, cl, rr = pool(recs, "listing", key)
        order = sorted(range(len(vals)), key=lambda i: -vals[i])[:10]
        out(L, f"-- top 10 of {key} with identity checks (spot/binance at t0; upbit-in-BTC at t0 + lag):")
        for i in order:
            r = rr[i]
            sr, xr = r.get("spot_ratio"), r.get("x24_ratio")
            out(
                L,
                f"  {r['token']:8s} {ymdhm(r['ms'])} {r['sym']:14s} {fmt(vals[i])}; spot {None if sr is None else round(sr, 3)}; "
                f"upbit-in-BTC +{r.get('x24_lag_h')}h {None if xr is None else round(xr, 3)} -> {collision(r)}",
            )
    # collision coverage after the post-review check
    for cat in CATS:
        pr = [r for r in recs if r["category"] == cat and r.get("p0") is not None]
        fails = [(r["token"], ymd(r["ms"]), round(x, 3)) for r in pr if (x := r.get("x24_ratio")) is not None and not (0.8 <= x <= 1.25)]
        out(
            L,
            f"{cat}: priced {len(pr)}; identity checks: {dict(Counter(collision(r) for r in pr))}; upbit-in-BTC checked "
            f"{sum(r.get('x24_ratio') is not None for r in pr)}; failures {fails}",
        )
    # H2 funding in detail
    vals, cl, rr = pool(recs, "listing", "d60_h24_base")
    if rr:
        fu = sorted(((r["trades"]["d60_h24_base"]["funding"], r["token"], ymd(r["ms"])) for r in rr), key=lambda x: x[0])
        xs = [f[0] for f in fu]
        out(
            L,
            f"\n## POST-HOC: H2 funding received by the short over 24 h (n {len(xs)}): mean {fmt(statistics.fmean(xs), 2)}, median "
            f"{fmt(statistics.median(xs), 2)}, p10 {fmt(float(np.quantile(xs, 0.1)), 2)}, p90 {fmt(float(np.quantile(xs, 0.9)), 2)}; "
            f"share < -1% {sum(x < -0.01 for x in xs) / len(xs) * 100:.0f}%",
        )
        out(L, "  most paid by the short: " + ", ".join(f"{t} {d} {fmt(x, 2)}" for x, t, d in fu[:5]))
        rest = xs[5:]
        out(L, f"  mean without these 5: {fmt(statistics.fmean(rest), 2)}" if rest else "")


def stage_prereg() -> None:
    text = CARD.read_text()
    sec = text[text.index(SECTION) :].encode()
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec).hexdigest()} bytes={len(sec)}")
    for n in (11228, 15181):  # registered prefixes (hash 1, hash 2); the post-review section is appended after them
        print(f"  first {n} bytes: sha256={hashlib.sha256(sec[:n]).hexdigest()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["prereg", "ann", "bodies", "titles", "prices", "seconds", "stage0", "report"])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "tradedesk-agents" / "news_events_0c")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--max-events", type=int, default=0)
    ap.add_argument("--tag", default="v2", help="run tag of the output files (run 1 wrote untagged files)")
    a = ap.parse_args()
    global TAG
    TAG = a.tag
    logging.getLogger("httpx").setLevel(logging.WARNING)
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "ann":
        asyncio.run(stage_ann(a.cache_dir))
    elif a.stage == "bodies":
        asyncio.run(stage_bodies(a.cache_dir))
    elif a.stage == "titles":
        stage_titles(a.cache_dir)
    elif a.stage == "prices":
        asyncio.run(stage_prices(a.cache_dir, a.sample))
    elif a.stage == "seconds":
        asyncio.run(stage_seconds(a.cache_dir, a.sample))
    elif a.stage == "stage0":
        stage_stage0(a.cache_dir)
    else:
        stage_report(a.cache_dir)


if __name__ == "__main__":
    main()
