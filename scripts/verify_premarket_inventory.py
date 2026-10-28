"""Step 0 for IDEA-20261028-premarket-perp-conversion-fade: inventory of pre-market perp contracts (no returns).

Only event counts, data coverage, pre-market volume and how precisely the conversion hour can be reconstructed are
computed. No R_k, no gap g, no funding outcome is read or printed (the card forbids it before pre-registration).

Stages:
  ann      Binance CMS announcement lists (titles + release dates only; untrusted text, used for symbol + date)
  venues   current instrument snapshots: Binance USD-M exchangeInfo, Bybit linear instruments, OKX SWAP instruments
  fetch    per symbol (cache sym2/): USD-M 1d quote volume, first 1d/1m index-kline time, funding settlement
           timestamps (no rates), first spot candle time; no price levels are stored
  inventory  event table, gates G1-G3, power note

An earlier, unversioned method-check fetch for METUSDT/CCUSDT/MONUSDT stored price levels and funding rates (cache
sym/, since moved to quarantine_sym_price_levels_DO_NOT_USE/). That code and its `probe` stage were removed: they are
not part of the reproducible pipeline, feed no gate, and the quarantined files must not be read by any later step.

Network only through the runtime providers (rt.exchanges.public_get -> Exchanges._call) and core.http.Http (Binance
CMS). Per-file JSON cache under --cache-dir (outside the repo).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import resource
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.premarket_inventory import (
    conversion_time,
    days_after,
    hour_confirmed,
    hour_precise,
    mde,
    median_or_none,
    premarket_volumes,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.server import build_server

HOUR = 3_600_000
DAY = 24 * HOUR
ANN_FROM = int(datetime(2024, 9, 1, tzinfo=UTC).timestamp() * 1000)
NOW_MS = int(datetime(2026, 10, 28, tzinfo=UTC).timestamp() * 1000)
CATALOGS = {48: "new_listing", 49: "latest_news", 161: "delisting"}
LAUNCH_RE = re.compile(r"Launch USD.{1,3}-Margined ([A-Z0-9]+USDT) Perpetual Contract Pre-Market", re.I)
CONVERT_RE = re.compile(r"Convert USD.{1,3}-Margined ([A-Z0-9]+USDT) Perpetual Contract Pre-Market", re.I)
SPOT_PM_RE = re.compile(r"Binance Will End the .*\(([A-Z0-9]+)\) Pre-Market", re.I)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(p)


def ts(ms: int | None) -> str:
    return "-" if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- ann


async def stage_ann(cache: Path) -> None:
    bn = Http("binance_cms", "https://www.binance.com", rate=0.5, burst=1, timeout=30, retries=2)
    try:
        for cat, label in CATALOGS.items():
            page = 1
            while page <= 60:
                p = cache / "ann" / f"binance_{cat}_{page:03d}.json"
                if not p.exists():
                    f = await bn.get_json(
                        "/bapi/composite/v1/public/cms/article/list/query",
                        params={"type": 1, "catalogId": cat, "pageNo": page, "pageSize": 50},
                    )
                    arts = f.data["data"]["catalogs"][0]["articles"]
                    wr(p, [{"ms": int(a["releaseDate"]), "title": a["title"], "code": a["code"]} for a in arts])
                arts = rd(p)
                if not arts or len(arts) < 50 or min(a["ms"] for a in arts) < ANN_FROM:
                    break
                page += 1
            print(f"binance cms {label}: {page} pages, rss {rss_mb():.0f} MB", flush=True)
    finally:
        await bn.aclose()
    ev = ann_events(cache)
    print(f"launch titles: {len(ev['launch'])}; convert titles: {len(ev['convert'])}; spot pre-market end: {len(ev['spot_pm'])}")


def ann_events(cache: Path) -> dict[str, dict[str, int]]:
    """Earliest release time per symbol for launch / convert titles (symbol + date only from untrusted text)."""
    out: dict[str, dict[str, int]] = {"launch": {}, "convert": {}, "spot_pm": {}}
    for p in sorted((cache / "ann").glob("binance_*.json")):
        for a in rd(p):
            for key, rx in (("launch", LAUNCH_RE), ("convert", CONVERT_RE), ("spot_pm", SPOT_PM_RE)):
                m = rx.search(a["title"])
                if m:
                    s = m.group(1).upper()
                    out[key][s] = min(out[key].get(s, a["ms"]), a["ms"])
    return out


# ---------------------------------------------------------------- venues


async def stage_venues(cache: Path) -> None:
    _, rt = build_server()
    ex = rt.exchanges
    try:
        p = cache / "venues" / "binance_exchange_info.json"
        if not p.exists():
            d = (await ex.public_get("binance", "fapiPublicGetExchangeInfo", {})).data
            keep = ("symbol", "pair", "contractType", "underlyingType", "underlyingSubType", "onboardDate", "deliveryDate", "status")
            wr(p, [{k: s.get(k) for k in keep} for s in d["symbols"]])
        p = cache / "venues" / "bybit_linear.json"
        if not p.exists():
            rows: list[dict[str, Any]] = []
            cursor = ""
            for _ in range(20):
                q = {"category": "linear", "limit": 1000, **({"cursor": cursor} if cursor else {})}
                r = (await ex.public_get("bybit", "publicGetV5MarketInstrumentsInfo", q)).data["result"]
                for i in r["list"]:
                    rows.append({k: i.get(k) for k in ("symbol", "contractType", "status", "launchTime", "isPreListing", "preListingInfo")})
                cursor = r.get("nextPageCursor") or ""
                if not cursor:
                    break
            wr(p, rows)
        p = cache / "venues" / "okx_swap.json"
        if not p.exists():
            d = (await ex.public_get("okx", "publicGetPublicInstruments", {"instType": "SWAP"})).data["data"]
            wr(p, [{k: i.get(k) for k in ("instId", "state", "listTime", "ruleType", "instFamily")} for i in d])
        p = cache / "venues" / "okx_futures.json"
        if not p.exists():
            d = (await ex.public_get("okx", "publicGetPublicInstruments", {"instType": "FUTURES"})).data["data"]
            wr(p, [{k: i.get(k) for k in ("instId", "state", "listTime", "ruleType", "instFamily", "alias")} for i in d])
    finally:
        await rt.aclose()
    bi = rd(cache / "venues" / "binance_exchange_info.json")
    sub: dict[str, int] = {}
    for s in bi:
        for t in s.get("underlyingSubType") or []:
            sub[t] = sub.get(t, 0) + 1
    print(f"binance USD-M symbols {len(bi)}; underlyingSubType tags: {dict(sorted(sub.items(), key=lambda kv: -kv[1]))}")
    by = rd(cache / "venues" / "bybit_linear.json")
    pre = [r for r in by if r.get("isPreListing")]
    print(f"bybit linear {len(by)}; isPreListing now: {len(pre)} {[r['symbol'] for r in pre]}")
    for name in ("okx_swap", "okx_futures"):
        ok = rd(cache / "venues" / f"{name}.json")
        rules: dict[str, int] = {}
        for r in ok:
            rules[str(r.get("ruleType"))] = rules.get(str(r.get("ruleType")), 0) + 1
        pm = [r["instId"] for r in ok if r.get("ruleType") == "pre_market"]
        print(f"{name} {len(ok)}; ruleType counts {rules}; pre_market: {pm}")


# ---------------------------------------------------------------- fetch


async def klines(
    rt: Any, ex_id: str, endpoint: str, params: dict[str, Any], start: int, end: int, step: int, limit: int = 1500
) -> list[list[Any]]:
    """Forward pagination by startTime (Binance kline endpoints: USD-M limit 1500, spot 1000)."""
    out: list[list[Any]] = []
    t = start
    while t < end:
        rows = (await rt.exchanges.public_get(ex_id, endpoint, {**params, "startTime": t, "endTime": end, "limit": limit})).data
        if not rows:
            break
        out.extend(rows)
        nxt = int(rows[-1][0]) + step
        if nxt <= t or len(rows) < limit:
            break
        t = nxt
    return out


async def funding_times(rt: Any, sym: str, start: int, end: int) -> list[int]:
    """Funding settlement timestamps only (the rates are an outcome of the card and are not stored)."""
    out: list[int] = []
    t = start
    for _ in range(10):
        q = {"symbol": sym, "startTime": t, "endTime": end, "limit": 1000}
        rows = (await rt.exchanges.public_get("binance", "fapiPublicGetFundingRate", q)).data
        if not rows:
            break
        out.extend(int(r["fundingTime"]) for r in rows)
        if len(rows) < 1000:
            break
        t = out[-1] + 1
    return out


def symbols(cache: Path) -> list[str]:
    ev = ann_events(cache)
    return sorted(set(ev["launch"]) | set(ev["convert"]))


async def stage_fetch(cache: Path, only: list[str] | None) -> None:
    """Per symbol (prices are stored only as levels / volumes for the inventory; nothing outcome-like is printed)."""
    _, rt = build_server()
    ev = ann_events(cache)
    syms = only or symbols(cache)
    t_start = time.time()
    try:
        for n, sym in enumerate(syms, 1):
            p = cache / "sym2" / f"{sym}.json"
            if p.exists():
                continue
            a = min(x for x in (ev["launch"].get(sym), ev["convert"].get(sym)) if x is not None)
            start = a - 2 * DAY
            end = NOW_MS
            rec: dict[str, Any] = {"symbol": sym, "ann_launch": ev["launch"].get(sym), "ann_convert": ev["convert"].get(sym)}
            try:
                d1 = await klines(rt, "binance", "fapiPublicGetKlines", {"symbol": sym, "interval": "1d"}, start, end, DAY)
                rec["perp_1d"] = [[int(r[0]), float(r[7])] for r in d1]  # open time, quote volume (USDT)
                i1 = await klines(rt, "binance", "fapiPublicGetIndexPriceKlines", {"pair": sym, "interval": "1d"}, start, end, DAY)
                rec["index_1d_first"] = int(i1[0][0]) if i1 else None
                rec["index_1d_n"] = len(i1)
                if i1:  # minute of the first index print: one day of 1m index klines from the first index day
                    d0 = int(i1[0][0])
                    im = await klines(
                        rt, "binance", "fapiPublicGetIndexPriceKlines", {"pair": sym, "interval": "1m"}, d0 - DAY, d0 + DAY, 60_000
                    )
                    rec["index_1m_first"] = int(im[0][0]) if im else None
                ft = await funding_times(rt, sym, start, end)
                rec["funding_first"], rec["funding_n"] = (ft[0] if ft else None), len(ft)
                rec["funding_gaps_h"] = sorted({round((b - x) / HOUR, 2) for x, b in zip(ft, ft[1:], strict=False)})
            except ProviderError as e:
                rec["perp_error"] = f"{e.reason}: {e}"[:200]
            try:
                sd = await klines(rt, "binance", "publicGetKlines", {"symbol": sym, "interval": "1d"}, start, end, DAY, limit=1000)
                if sd:
                    sh = await klines(
                        rt, "binance", "publicGetKlines", {"symbol": sym, "interval": "1h"}, int(sd[0][0]), int(sd[0][0]) + DAY, HOUR, 1000
                    )
                    rec["spot_first"] = int(sh[0][0]) if sh else int(sd[0][0])
                else:
                    rec["spot_first"] = None
            except ProviderError as e:
                rec["spot_first"] = None
                rec["spot_error"] = f"{e.reason}"[:60]
            wr(p, rec)
            el = time.time() - t_start
            print(
                f"  [{n}/{len(syms)}] {sym} 1d={len(rec.get('perp_1d', []))} idx1d={rec.get('index_1d_n')} "
                f"fund={rec.get('funding_n')} spot={ts(rec.get('spot_first'))} {rec.get('perp_error', '')[:60]} "
                f"{el:.0f}s rss {rss_mb():.0f} MB",
                flush=True,
            )
    finally:
        await rt.aclose()
    print("fetch done", flush=True)


# ---------------------------------------------------------------- extra

UTC_TS_RE = re.compile(r"(20\d\d-\d\d-\d\d) (\d\d:\d\d)(?::\d\d)? ?\(UTC\)")


async def stage_extra(cache: Path) -> None:
    """First 1h perp candle per symbol; announced conversion time (timestamps only, from the untrusted body);
    Bybit / OKX announcement titles mentioning pre-market (visibility of other venues' history)."""
    _, rt = build_server()
    try:
        for p in sorted((cache / "sym2").glob("*.json")):
            rec = rd(p)
            if "perp_first_1h" in rec or not rec.get("perp_1d"):
                continue
            d0 = rec["perp_1d"][0][0]
            h = await klines(rt, "binance", "fapiPublicGetKlines", {"symbol": rec["symbol"], "interval": "1h"}, d0, d0 + DAY, HOUR)
            rec["perp_first_1h"] = int(h[0][0]) if h else None
            wr(p, rec)
    finally:
        await rt.aclose()
    bn = Http("binance_cms", "https://www.binance.com", rate=0.5, burst=1, timeout=30, retries=2)
    by = Http("bybit_ann", "https://api.bybit.com", rate=1, burst=1, timeout=30, retries=2)
    okx = Http("okx_ann", "https://www.okx.com", rate=0.5, burst=1, timeout=30, retries=2)
    try:
        codes: dict[str, str] = {}
        for f in sorted((cache / "ann").glob("binance_*.json")):
            for a in rd(f):
                m = CONVERT_RE.search(a["title"])
                if m:
                    codes[m.group(1).upper()] = a["code"]
        p = cache / "ann_convert_times.json"
        got: dict[str, list[str]] = rd(p) if p.exists() else {}
        for sym, code in sorted(codes.items()):
            if sym in got:
                continue
            f = await bn.get_json("/bapi/composite/v1/public/cms/article/detail/query", params={"articleCode": code})
            body = json.dumps((f.data.get("data") or {}).get("body") or "")
            got[sym] = sorted({f"{d} {t}" for d, t in UTC_TS_RE.findall(body)})
            wr(p, got)
        print(f"announced conversion timestamps: {got}")
        # events without a convert title: timestamps near "convert"/"pre-market" in the spot-listing and launch bodies
        p2 = cache / "ann_list_conv_times.json"
        got2: dict[str, list[str]] = rd(p2) if p2.exists() else {}
        for sym in symbols(cache):
            if sym in codes or sym in got2:
                continue
            base, found = sym[:-4], set()
            arts = [a for f in sorted((cache / "ann").glob("binance_48_*.json")) for a in rd(f)]
            pick = [a for a in arts if f"({base})" in a["title"] and a["title"].startswith("Binance Will List")]
            pick += [a for a in arts if LAUNCH_RE.search(a["title"]) and LAUNCH_RE.search(a["title"]).group(1).upper() == sym]
            for a in pick:
                f = await bn.get_json("/bapi/composite/v1/public/cms/article/detail/query", params={"articleCode": a["code"]})
                body = json.dumps((f.data.get("data") or {}).get("body") or "")
                for m in UTC_TS_RE.finditer(body):
                    ctx = body[max(0, m.start() - 400) : m.start()].lower()
                    if "convert" in ctx:
                        found.add(f"{m.group(1)} {m.group(2)}")
            got2[sym] = sorted(found)
            wr(p2, got2)
        print(f"timestamps after 'convert' in listing / launch bodies: {got2}")
        for venue, http, path, mk in (
            ("bybit", by, "/v5/announcements/index", lambda pg: {"locale": "en-US", "type": "new_crypto", "limit": 20, "page": pg}),
            ("okx", okx, "/api/v5/support/announcements", lambda pg: {"annType": "announcements-new-listings", "page": pg}),
        ):
            for pg in range(1, 80):
                q = cache / "ann_other" / f"{venue}_{pg:03d}.json"
                if not q.exists():
                    f = await http.get_json(path, params=mk(pg))
                    if venue == "bybit":
                        rows = [
                            {"ms": int(a.get("publishTime") or a.get("dateTimestamp")), "title": a["title"]}
                            for a in f.data["result"]["list"]
                        ]
                    else:
                        det = f.data["data"][0]["details"] if f.data.get("data") else []
                        rows = [{"ms": int(a["pTime"]), "title": a["title"]} for a in det]
                    wr(q, rows)
                rows = rd(q)
                if not rows or min(r["ms"] for r in rows) < ANN_FROM:
                    break
            print(f"{venue} new-listing pages: {pg}", flush=True)
    finally:
        for h in (bn, by, okx):
            await h.aclose()
    for venue in ("bybit", "okx"):
        hits = []
        for q in sorted((cache / "ann_other").glob(f"{venue}_*.json")):
            hits += [(r["ms"], r["title"]) for r in rd(q) if re.search(r"pre-?market", r["title"], re.I) and r["ms"] >= ANN_FROM]
        hits = sorted(set(hits))
        wr(cache / f"{venue}_premarket_titles.json", hits)
        print(
            f"{venue}: {len(hits)} titles mention pre-market since {ts(ANN_FROM)}; first/last: "
            f"{ts(hits[0][0]) if hits else '-'} / {ts(hits[-1][0]) if hits else '-'}"
        )


# ---------------------------------------------------------------- inventory

SD_R7 = 0.25  # card's SD assumption for R_7d (decimal)
G2_MIN_USD = 1_000_000.0
MIN_DAYS_AFTER = 7.0


def flags_by_base(cache: Path) -> dict[str, dict[str, int]]:
    """Earliest Binance HODLer-airdrop / spot-listing title per base asset ("(BASE)" in the title; date only)."""
    out: dict[str, dict[str, int]] = {"hodler": {}, "spot_list": {}}
    for p in sorted((cache / "ann").glob("binance_48_*.json")):
        for a in rd(p):
            t = a["title"]
            kind = "hodler" if "HODLer Airdrop" in t else "spot_list" if re.match(r"Binance Will List ", t) else None
            if kind:
                for b in re.findall(r"\(([A-Z0-9]+)\)", t):
                    out[kind][b] = min(out[kind].get(b, a["ms"]), a["ms"])
    return out


def stage_inventory(cache: Path) -> None:
    conv_ann: dict[str, list[str]] = rd(cache / "ann_convert_times.json") if (cache / "ann_convert_times.json").exists() else {}
    fl = flags_by_base(cache)
    rows: list[dict[str, Any]] = []
    for p in sorted((cache / "sym2").glob("*.json")):
        r = rd(p)
        sym, base = r["symbol"], r["symbol"][:-4]
        daily = [(int(t), float(v)) for t, v in r.get("perp_1d", [])]
        ann_ts = [int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp() * 1000) for s in conv_ann.get(sym, [])]
        est = r.get("index_1m_first") or r.get("index_1d_first")
        ann_ms = min(ann_ts, key=lambda x: abs(x - est)) if (ann_ts and est) else (ann_ts[0] if ann_ts else None)
        c = conversion_time(r.get("index_1m_first"), r.get("index_1d_first"), ann_ms)
        first = r.get("perp_first_1h") or (daily[0][0] if daily else None)
        last = daily[-1][0] if daily else None
        pm_vol = premarket_volumes(daily, (daily[0][0] + 1) if daily else 0, c.t_ms) if (daily and c.t_ms) else []
        med = median_or_none(pm_vol)
        da = days_after(c.t_ms, last, NOW_MS) if c.t_ms else 0.0
        pm_days = (c.t_ms - first) / DAY if (c.t_ms and first) else None
        delisted = last is not None and last < NOW_MS - 2 * DAY
        h = fl["hodler"].get(base)
        g1 = bool(pm_days and pm_days >= 1 and da >= MIN_DAYS_AFTER)
        g2 = bool(med is not None and med >= G2_MIN_USD)
        g3 = hour_precise(c)
        g3s = hour_confirmed(c, r.get("spot_first"))
        rows.append({
            "g3_strict": g3s,
            "symbol": sym, "first": first, "ann_launch": r.get("ann_launch"), "conv": c.t_ms, "conv_src": c.source,
            "ann_conv_ts": conv_ann.get(sym), "ann_gap_h": c.ann_gap_h, "spot_first": r.get("spot_first"),
            "pm_days": pm_days, "pm_full_days": len(pm_vol), "pm_med_vol": med, "days_after": da, "delisted": delisted,
            "hodler": h, "spot_list_ann": fl["spot_list"].get(base), "funding_gaps_h": r.get("funding_gaps_h"),
            "g1": g1, "g2": g2, "g3": g3,
        })  # fmt: skip
    wr(cache / "inventory.json", rows)
    print(
        f"{'symbol':<12}{'first 1h':<17}{'conversion':<17}{'src':<9}{'annΔh':>6}{'spot 1st':<17}"
        f"{'pmD':>5}{'medVol$M':>9}{'aftD':>6}{'HODL':>5}{'dl':>3}  G1 G2 G3"
    )
    for x in sorted(rows, key=lambda x: x["first"] or 0):
        gap = "-" if x["ann_gap_h"] is None else f"{x['ann_gap_h']:.1f}"
        mv = "-" if x["pm_med_vol"] is None else f"{x['pm_med_vol'] / 1e6:.1f}"
        pmd = "-" if x["pm_days"] is None else f"{x['pm_days']:.1f}"
        print(
            f"{x['symbol']:<12}{ts(x['first']):<17}{ts(x['conv']):<17}{x['conv_src']:<9}{gap:>6} {ts(x['spot_first']):<16}"
            f"{pmd:>5}{mv:>9}{x['days_after']:>6.0f}{'y' if x['hodler'] else '':>5}{'y' if x['delisted'] else '':>3}"
            f"  {'+' if x['g1'] else '-':>2} {'+' if x['g2'] else '-':>2} {'+' if x['g3'] else '-':>2}"
        )
    n = len(rows)
    g1 = [x for x in rows if x["g1"]]
    meds = [x["pm_med_vol"] for x in g1 if x["pm_med_vol"] is not None]
    usable = [x for x in g1 if x["g2"] and x["g3"]]
    with_ann = [x for x in rows if x["ann_gap_h"] is not None]
    print(f"\nevents (Binance perp pre-market launches): {n}; converted (index exists): {sum(1 for x in rows if x['conv'])}")
    print(
        f"G1 (pre-market candles and >= {MIN_DAYS_AFTER:.0f} d after conversion): {len(g1)} (gate n >= 30: {'PASS' if len(g1) >= 30 else 'FAIL'})"
    )
    mm = median_or_none(meds)
    print(
        f"G2 median over G1 events of per-event median daily pre-market volume: "
        f"{'-' if mm is None else f'${mm / 1e6:.2f}M'} ({'PASS' if mm and mm >= G2_MIN_USD else 'FAIL'}); "
        f"events >= $1M: {sum(1 for x in g1 if x['g2'])}/{len(g1)}; no full pre-market day: {sum(1 for x in g1 if x['pm_med_vol'] is None)}"
    )
    g3n = sum(1 for x in g1 if x["g3"])
    print(
        f"G3 hour-precise conversion (1m index start, |Δ announced| <= 1 h when announced): {g3n}/{len(g1)} "
        f"({'PASS' if g1 and g3n / len(g1) >= 0.9 else 'FAIL'}); announced-time cross-checks: {len(with_ann)}, "
        f"|Δ| h = {sorted(round(x['ann_gap_h'], 2) for x in with_ann)}"
    )
    g3s = sum(1 for x in g1 if x["g3_strict"])
    print(
        f"G3 strict (exploratory, defined after the first inventory run): 1m index start confirmed within 1 h by the "
        f"announced time or, without one, by the first Binance spot candle: {g3s}/{len(g1)}; not confirmed: "
        f"{[x['symbol'] for x in g1 if not x['g3_strict']]}"
    )
    print(f"usable n (G1 & G2 & G3 per event): {len(usable)}; with strict G3: {sum(1 for x in usable if x['g3_strict'])}")
    bt = rd(cache / "bybit_premarket_titles.json") if (cache / "bybit_premarket_titles.json").exists() else []
    conv_by = {s for _, t in bt if re.search(r"conver", t, re.I) for s in re.findall(r"\b([A-Z0-9]{2,}USDT)\b", t)}
    list_by = {s for _, t in bt if re.search(r"list", t, re.I) for s in re.findall(r"\b([A-Z0-9]{2,}USDT)\b", t)}
    bn = {x["symbol"] for x in rows}
    print(
        f"bybit (titles only, no candles fetched): pre-market listing symbols {len(list_by)}, conversion symbols "
        f"{len(conv_by)}; conversions also on Binance list {len(conv_by & bn)}; Bybit-only conversions {len(conv_by - bn)}: "
        f"{' '.join(sorted(conv_by - bn))}"
    )
    for k in sorted({len(g1), len(usable), 30, 60}):
        print(f"  power: n={k:<3} MDE = 2.8*{SD_R7 * 100:.0f}/sqrt(n) = {mde(SD_R7, k) * 100:.1f} pp")
    print(
        f"HODLer overlap among G1 events: {sum(1 for x in g1 if x['hodler'])}/{len(g1)}; funding intervals seen (h): "
        f"{sorted({g for x in rows for g in (x['funding_gaps_h'] or [])})[:12]}; peak RSS {rss_mb():.0f} MB"
    )


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["ann", "venues", "fetch", "extra", "inventory"])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/premarket_inventory")
    ap.add_argument("--symbols", default="", help="comma list (fetch)")
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    only = [s for s in a.symbols.split(",") if s]
    if a.stage == "ann":
        asyncio.run(stage_ann(a.cache_dir))
    elif a.stage == "venues":
        asyncio.run(stage_venues(a.cache_dir))
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(a.cache_dir, only or None))
    elif a.stage == "extra":
        asyncio.run(stage_extra(a.cache_dir))
    else:
        stage_inventory(a.cache_dir)


if __name__ == "__main__":
    main()
