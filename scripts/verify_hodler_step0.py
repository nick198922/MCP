"""Step 0 for IDEA-20261028-hodler-airdrop-listing-fade-short: event inventory, no event returns.

Stages (per-file JSON cache under --cache-dir, a rerun resumes):
  ann        Binance CMS article lists (catalogs 48 / 93 / 128) back to 2024-11 -> ann/
  detail     article body of every HODLer intro announcement -> detail/SYM.json; prints which schedule phrase
             it carries. The design marker is the listing line ("Binance will then list SYM at ..." / "before
             trading starts"): an announcement without it is an airdrop on an already listed token. The phrase
             "within N hours after the HODLer Airdrops is announced" is boilerplate in every announcement and is
             printed only to show that. Makes the 2026-03 programme-change claim reproducible.
  fetch      per HODLer token: first Binance spot 1h bars (API, fallback data.binance.vision) -> spot/;
             missing Binance USD-M 1h months (listing_drift cache read-only first) -> k1h/
  inventory  usable events by the registered rules, months, overlap with the pre-market inventory, and the
             SD of 72h BTC-hedged residuals OUTSIDE the event window (windows starting at t_e + 72h·k, k >= 1);
             prints dispersion only (no means of any return), MDE80 and the gate. Full rows -> inventory.json

Never computes or prints a return statistic of the event window [t_e; t_e + 72h]. Announcement titles are untrusted
text: only "(SYM)" and the release time are used. Network only through core.http.Http / the runtime providers.

    uv run python scripts/verify_hodler_step0.py ann|detail|fetch|inventory [--only SYM ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import resource
import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.server import build_server

HOUR = 3_600_000
DAY = 24 * HOUR
HOME_CACHE = Path.home() / ".cache" / "tradedesk-agents"
LD = HOME_CACHE / "listing_drift"
INV = HOME_CACHE / "premarket_inventory"
BTC_DIR = HOME_CACHE / "course_s14_ema" / "k1h" / "BTCUSDT"
ANN_FROM = int(datetime(2024, 11, 1, tzinfo=UTC).timestamp() * 1000)
EV_FROM = int(datetime(2024, 12, 1, tzinfo=UTC).timestamp() * 1000)
EV_TO = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1000)
CATALOGS = (48, 93, 128)
INTRO_RE = re.compile(r"^\s*Introducing (.+?) on Binance HODLer Airdrops", re.I)
ENTRY_LAG = 6 * HOUR
HOLD = 72 * HOUR
POST_K = 19  # post-window 72h windows k = 1..19 (~60 days)
LIQ_MIN_USD = 1_250_000.0  # perp quote volume over [t_e - 6h; t_e)
ICC = 0.05
Z = 2.8
SPLIT = int(datetime(2025, 10, 1, tzinfo=UTC).timestamp() * 1000)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(p)


def ts(ms: float | None) -> str:
    return "-" if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def ym(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m")


# ---------------------------------------------------------------- ann


async def stage_ann(cache: Path) -> None:
    bn = Http("binance_cms", "https://www.binance.com", rate=0.5, burst=1, timeout=30, retries=2)
    try:
        for cat in CATALOGS:
            page = 1
            while page <= 40:
                p = cache / "ann" / f"binance_{cat}_{page:03d}.json"
                if not p.exists():
                    try:
                        f = await bn.get_json(
                            "/bapi/composite/v1/public/cms/article/list/query",
                            params={"type": 1, "catalogId": cat, "pageNo": page, "pageSize": 50},
                        )
                        cats = (f.data.get("data") or {}).get("catalogs") or []
                        arts = cats[0]["articles"] if cats else []
                    except ProviderError as err:
                        print(f"  catalog {cat} page {page}: {err.reason}", flush=True)
                        break
                    wr(p, [{"ms": int(a["releaseDate"]), "title": a["title"], "code": a["code"]} for a in arts])
                arts = rd(p)
                if not arts or len(arts) < 50 or min(a["ms"] for a in arts) < ANN_FROM:
                    break
                page += 1
            n_h = sum("HODLer" in a["title"] for q in (cache / "ann").glob(f"binance_{cat}_*.json") for a in rd(q))
            print(f"catalog {cat}: {page} pages, HODLer titles {n_h}, rss {rss_mb():.0f} MB", flush=True)
    finally:
        await bn.aclose()


def hodler_events(cache: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """One row per token from 'Introducing ... (SYM) ... on Binance HODLer Airdrops' titles; other HODLer titles listed."""
    seen: dict[str, dict[str, Any]] = {}
    other: list[str] = []
    for p in sorted((cache / "ann").glob("binance_*.json")):
        cat = int(p.name.split("_")[1])
        for a in rd(p):
            t = a["title"]
            if "HODLer" not in t:
                continue
            m = INTRO_RE.search(t)
            if not m:
                other.append(f"{ts(a['ms'])} c{cat} {t[:90]}")
                continue
            for sym in re.findall(r"\(([A-Z0-9]+)\)", m.group(1)):
                if sym not in seen or a["ms"] < seen[sym]["ann_ms"]:
                    seen[sym] = {"sym": sym, "ann_ms": a["ms"], "cat": cat, "title": t[:120], "code": a["code"]}
    return sorted(seen.values(), key=lambda e: e["ann_ms"]), sorted(set(other))


# ---------------------------------------------------------------- detail

# Schedule phrases only (announcement text is untrusted; nothing else is read from it).
OLD_RE = re.compile(r"will then list|before trading (?:starts|begins|opens)", re.I)
NEW_RE = re.compile(r"within \d+ hours? after the HODLer Airdrops? (?:is|are|has been|have been) announced", re.I)


def flat_text(node: Any) -> str:
    """Flatten the CMS article body (a JSON node tree, possibly as a string) into plain text."""
    if isinstance(node, str):
        try:
            node = json.loads(node)
        except ValueError:
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", node))
    out: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if isinstance(x.get("text"), str):
                out.append(x["text"])
            for v in x.values():
                if isinstance(v, dict | list):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(node)
    return re.sub(r"\s+", " ", " ".join(out))


async def stage_detail(cache: Path) -> None:
    """Article bodies of every HODLer intro announcement -> detail/SYM.json; prints the schedule phrases found."""
    events, _ = hodler_events(cache)
    bn = Http("binance_cms", "https://www.binance.com", rate=0.3, burst=1, timeout=30, retries=2)
    rows: list[tuple[str, str, str, str]] = []
    try:
        for e in events:
            p = cache / "detail" / f"{e['sym']}.json"
            if not p.exists():
                try:
                    f = await bn.get_json("/bapi/composite/v1/public/cms/article/detail/query", params={"articleCode": e["code"]})
                except ProviderError as err:
                    print(f"  {e['sym']} {e['code']}: {err.reason}", flush=True)
                    continue
                wr(p, f.data)
            text = flat_text((rd(p).get("data") or {}).get("body") or "")
            old = "old" if OLD_RE.search(text) else "-"
            new = "new" if NEW_RE.search(text) else "-"
            rows.append((ts(e["ann_ms"]), e["sym"], old, new))
    finally:
        await bn.aclose()
    print("announcement schedule phrases (old = listing line 'will then list' / 'before trading starts';")
    print("  new = 'within N hours after the HODLer Airdrops is announced' -- boilerplate, not a design marker):")
    for r in rows:
        print(f"  {r[0]}  {r[1]:<10} {r[2]:<4} {r[3]}")
    olds = [r[0] for r in rows if r[2] == "old"]
    news = [r[0] for r in rows if r[3] == "new"]
    print(f"bodies {len(rows)}/{len(events)}; old phrase {len(olds)} (last {olds[-1] if olds else '-'});", end=" ")
    print(f"new phrase {len(news)} (first {news[0] if news else '-'}); neither {sum(r[2] == r[3] == '-' for r in rows)}")
    print(f"rss {rss_mb():.0f} MB")


# ---------------------------------------------------------------- fetch


def norm_rows(rows: list[list[Any]]) -> list[list[float]]:
    out = []
    for r in rows:
        t = float(r[0])
        if t > 1e14:  # microsecond archives
            t /= 1000
        out.append([t, *(float(x) for x in r[1:7])])
    return out


async def spot_first(rt: Any, sym: str, ann_ms: int) -> dict[str, Any]:
    """First traded spot 1h bars of SYMUSDT around the announcement (bars up to first + 7h only)."""
    pair = f"{sym}USDT"
    start = ann_ms - 30 * DAY
    src = "api"
    try:
        raw = (
            await rt.exchanges.public_get(
                "binance", "publicGetKlines", {"symbol": pair, "interval": "1h", "startTime": start, "limit": 1000}
            )
        ).data
        rows = norm_rows(raw or [])
    except ProviderError as err:
        rows, src = [], f"api:{err.reason}"
    if not rows:
        rows = []
        for mo_ms in (ann_ms - 30 * DAY, ann_ms, ann_ms + 20 * DAY):
            y, m = map(int, ym(mo_ms).split("-"))
            try:
                part = (await rt.binance_vision.spot_klines_monthly(pair, "1h", y, m)).data
            except ProviderError:
                continue
            rows.extend(norm_rows(part or []))
        rows = sorted({r[0]: r for r in rows}.values())
        src += "+vision"
    rows = [r for r in rows if r[5] > 0]
    if not rows:
        return {"pair": pair, "first": None, "src": src, "bars": []}
    first = rows[0][0]
    return {"pair": pair, "first": first, "src": src, "bars": [r for r in rows if r[0] <= first + 7 * HOUR]}


def perp_symbol(sym: str) -> list[tuple[str, int]]:
    return [(f"{sym}USDT", 1), (f"1000{sym}USDT", 1000)]


async def perp_months(rt: Any, cache: Path, psym: str, months: list[str]) -> dict[str, str]:
    """Ensure 1h months exist (listing_drift cache first, then own k1h/); returns month -> status."""
    st = {}
    for mo in months:
        src, own = LD / "k1h" / psym / f"{mo}.json", cache / "k1h" / psym / f"{mo}.json"
        if src.exists():
            st[mo] = "ld"
            continue
        if own.exists():
            st[mo] = "own" if rd(own) else "none"
            continue
        y, m = map(int, mo.split("-"))
        try:
            part = (await rt.binance_vision.um_klines_monthly(psym, "1h", y, m)).data
        except ProviderError as err:
            part = None
            st[mo] = f"err:{err.reason}"
        wr(own, norm_rows(part) if part else [])
        st.setdefault(mo, "own" if part else "none")
    return st


def months_between(a: float, b: float) -> list[str]:
    out, t = [], a
    while ym(t) <= ym(b):
        if ym(t) not in out:
            out.append(ym(t))
        t += 20 * DAY
    if ym(b) not in out:
        out.append(ym(b))
    return [m for m in out if m <= "2026-09"]


async def stage_fetch(cache: Path, only: list[str] | None) -> None:
    evs, _ = hodler_events(cache)
    evs = [e for e in evs if not only or e["sym"] in only]
    _, rt = build_server()
    t0 = time.time()
    try:
        for i, e in enumerate(evs, 1):
            sym = e["sym"]
            p = cache / "spot" / f"{sym}.json"
            if not p.exists():
                wr(p, await spot_first(rt, sym, e["ann_ms"]))
            sp = rd(p)
            if sp["first"] is not None:
                te = sp["first"] + ENTRY_LAG
                for psym, _ in perp_symbol(sym):
                    if psym == f"1000{sym}USDT" and not (LD / "k1h" / psym).exists():
                        continue
                    await perp_months(rt, cache, psym, months_between(te - DAY, te + HOLD * (POST_K + 1) + DAY))
            if i % 10 == 0 or i == len(evs):
                print(f"  [{i}/{len(evs)}] {sym} spot_first={ts(sp['first'])} {time.time() - t0:.0f}s rss {rss_mb():.0f} MB", flush=True)
    finally:
        await rt.aclose()
    print("fetch done", flush=True)


# ---------------------------------------------------------------- inventory


def load_bars(cache: Path, psym: str) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {}
    for d in (LD / "k1h" / psym, cache / "k1h" / psym):
        for f in sorted(d.glob("*.json")) if d.exists() else []:
            for r in rd(f) or []:
                out[int(r[0])] = [float(x) for x in r[:7]]
    return out


def load_btc() -> dict[int, float]:
    out: dict[int, float] = {}
    for f in sorted(BTC_DIR.glob("*.json")):
        if f.name >= "2024-11":
            for r in rd(f):
                out[int(r[0])] = float(r[1])
    return out


def sd(xs: list[float]) -> float:
    return statistics.stdev(xs) if len(xs) > 1 else math.nan


def stage_inventory(cache: Path) -> None:
    evs, other = hodler_events(cache)
    ei = {s["symbol"]: s for s in rd(INV / "venues" / "binance_exchange_info.json")}
    bybit = {r["symbol"]: int(r["launchTime"]) for r in rd(INV / "venues" / "bybit_linear.json") if r.get("launchTime")}
    pm = {r["symbol"][:-4]: r for r in rd(INV / "inventory.json")}
    btc = load_btc()
    rows: list[dict[str, Any]] = []
    resid: dict[int, list[float]] = {}
    for e in evs:
        sym = e["sym"]
        sp = rd(cache / "spot" / f"{sym}.json") if (cache / "spot" / f"{sym}.json").exists() else {"first": None}
        r: dict[str, Any] = {"sym": sym, "ann": ts(e["ann_ms"]), "spot_first": ts(sp.get("first")), "reason": None}
        rows.append(r)
        first = sp.get("first")
        if first is None:
            r["reason"] = "no spot bars"
            continue
        r["month"] = ym(first)
        r["pm_overlap"] = sym in pm
        if sym in pm and pm[sym].get("spot_first"):
            r["pm_spot_first"] = pm[sym]["spot_first"]
        if not (EV_FROM <= first < EV_TO):
            r["reason"] = "outside period"
            continue
        if first < e["ann_ms"] - DAY:
            r["reason"] = "spot traded before announcement"
            continue
        te = int(first + ENTRY_LAG)
        r["t_e"] = ts(te)
        by = bybit.get(f"{sym}USDT")
        r["bybit_by_te"] = by is not None and by <= te
        best = None
        for psym, mult in perp_symbol(sym):
            bars = load_bars(cache, psym)
            if not bars:
                continue
            pf = min(t for t, b in bars.items() if b[5] > 0) if any(b[5] > 0 for b in bars.values()) else None
            best = (psym, mult, bars, pf)
            break
        if best is None:
            r["reason"] = "no Binance perp 1h bars"
            continue
        psym, mult, bars, pf = best
        r["perp"], r["perp_first"] = psym, ts(pf)
        r["perp_lag_h"] = None if pf is None else round((pf - first) / HOUR, 1)
        r["underlying"] = (ei.get(psym) or {}).get("underlyingType")
        ob = (ei.get(psym) or {}).get("onboardDate")
        r["onboard"] = ts(ob) if ob else "-"
        if r["underlying"] not in (None, "COIN"):
            r["reason"] = f"underlyingType {r['underlying']}"
            continue
        if pf is None or pf > te or te not in bars or bars[te][5] <= 0:
            r["reason"] = "perp not trading at t_e"
            r["perp_within_24h"] = pf is not None and pf <= first + DAY
            continue
        if te + HOLD not in bars:
            r["reason"] = "no exit bar"
            continue
        # price-level check on the last closed bar before entry (no return involved)
        spot_bars = {int(b[0]): b for b in sp["bars"]}
        sb, pb = spot_bars.get(te - HOUR), bars.get(te - HOUR)
        ratio = (pb[4] / mult) / sb[4] if sb and pb and sb[4] > 0 else None
        r["px_ratio"] = None if ratio is None else round(ratio, 3)
        if ratio is not None and not 0.5 <= ratio <= 2.0:
            r["reason"] = "price-level mismatch"
            continue
        liq = sum(bars[t][6] for t in range(te - 6 * HOUR, te, HOUR) if t in bars)
        r["liq6h_musd"] = round(liq / 1e6, 2)
        r["vol_first24h_musd"] = round(sum(bars[t][6] for t in range(int(pf), int(pf) + DAY, HOUR) if t in bars) / 1e6, 1)
        if liq < LIQ_MIN_USD:
            r["reason"] = "liquidity < 1.25M USD in 6h before entry"
            continue
        r["reason"] = "usable"
        # dispersion outside the event window only: k >= 1
        for k in range(1, POST_K + 1):
            s, x = te + HOLD * k, te + HOLD * (k + 1)
            if s in bars and x in bars and s in btc and x in btc and bars[s][1] > 0:
                resid.setdefault(k, []).append((bars[x][1] / bars[s][1] - 1) - (btc[x] / btc[s] - 1))
    wr(cache / "inventory.json", rows)

    reasons = Counter(r["reason"] for r in rows)
    print(f"HODLer intro titles -> tokens: {len(evs)}; other HODLer titles (not events): {len(other)}")
    for o in other:
        print(f"   other: {o}")
    print(f"reasons: {dict(reasons)}")
    use = [r for r in rows if r["reason"] == "usable"]
    inper = [r for r in rows if r.get("month") and r["reason"] not in ("outside period", "no spot bars")]
    print(f"\nin period 2024-12..2026-09: {len(inper)}; usable (Binance perp at t_e, liquid): {len(use)}")
    print(
        f"not usable but Bybit perp listed by t_e (launchTime, current instruments only): "
        f"{sum(1 for r in inper if r['reason'] != 'usable' and r.get('bybit_by_te'))}"
    )
    print(f"perp not at t_e but within spot+24h: {sum(1 for r in inper if r.get('perp_within_24h'))}")
    print("\n sym        spot_first        perp            lag_h  liq6h  vol24h  ratio  pm  reason")
    for r in rows:
        if r.get("month"):
            print(
                f" {r['sym']:<10} {r['spot_first']:<17} {r.get('perp', '-'):<15} {str(r.get('perp_lag_h', '-')):>6} "
                f"{str(r.get('liq6h_musd', '-')):>6} {str(r.get('vol_first24h_musd', '-')):>7} {str(r.get('px_ratio', '-')):>6} "
                f"{'y' if r.get('pm_overlap') else '':>3}  {r['reason']}"
            )
    by_m = Counter(r["month"] for r in use)
    print("\nusable by month:", " ".join(f"{m}:{c}" for m, c in sorted(by_m.items())))
    print(
        f"halves: 2024-12..2025-09 {sum(1 for r in use if r['month'] < '2025-10')}, 2025-10..2026-09 {sum(1 for r in use if r['month'] >= '2025-10')}"
    )
    print(
        f"overlap with pre-market inventory (S46, premarket-perp-conversion-fade): {sorted(r['sym'] for r in use if r.get('pm_overlap'))}"
    )
    chk = [(r["sym"], r["spot_first"], r["pm_spot_first"]) for r in rows if r.get("pm_spot_first")]
    print(f"spot_first cross-check vs premarket inventory: {chk}")
    n = len(use)
    msz = list(by_m.values())
    mbar = sum(m * m for m in msz) / sum(msz) if msz else 0
    print(f"\nmonth clusters {len(msz)}; m_bar = sum m^2 / sum m = {mbar:.2f}")
    print("\nSD of 72h BTC-hedged residual OUTSIDE the event window (dispersion only; means not printed):")
    groups = {"k=1 (78-150h after t_spot)": [1], "k=1-3": [1, 2, 3], "k=4-9": list(range(4, 10)), "k=10-19": list(range(10, 20))}
    sds = {}
    for name, ks in groups.items():
        xs = [x for k in ks for x in resid.get(k, [])]
        med = statistics.median(xs) if xs else math.nan
        mad = 1.4826 * statistics.median(abs(x - med) for x in xs) if xs else math.nan
        sds[name] = sd(xs)
        print(f"  {name:<28} n_windows={len(xs):>4}  SD={100 * sds[name]:6.1f} pp  robust(1.4826*MAD)={100 * mad:6.1f} pp")
    ext = 0.429 * math.sqrt(72 / 336)
    print(f"  external: new-perp-listing-drift SD 42.9 pp / 14 d scaled to 72 h = {100 * ext:.1f} pp")
    print(f"\nMDE80 = {Z} * SD / sqrt(n / deff), n = {n}")
    for name, s in [
        ("k=1", sds["k=1 (78-150h after t_spot)"]),
        ("k=1-3", sds["k=1-3"]),
        ("external", ext),
        ("card 20pp", 0.20),
        ("card gate 14pp", 0.14),
    ]:
        cells = []
        for icc in (0.02, 0.05, 0.10):
            deff = 1 + (mbar - 1) * icc
            cells.append(f"ICC {icc:.2f} deff {deff:.2f}: {100 * Z * s / math.sqrt(n / deff):5.1f}")
        cells.append(f"card deff 1.3: {100 * Z * s / math.sqrt(n / 1.3):5.1f}")
        print(f"  SD {name:<14} {100 * s:5.1f} pp | " + " | ".join(cells))
    deff = 1 + (mbar - 1) * ICC
    s_prim = sds["k=1 (78-150h after t_spot)"]
    mde = Z * s_prim / math.sqrt(n / deff) if n else math.inf
    print(
        f"\nGATE (registered: n >= 50 and MDE80 <= 6 pp; SD = k=1, ICC 0.05): n={n} -> {'pass' if n >= 50 else 'FAIL'}; "
        f"MDE={100 * mde:.1f} pp -> {'pass' if mde <= 0.06 else 'FAIL'}"
    )
    need = math.ceil(deff * (Z * s_prim / 0.06) ** 2)
    print(f"n needed for MDE 6 pp at this SD/deff: {need}; at SD 14 pp: {math.ceil(deff * (Z * 0.14 / 0.06) ** 2)}")
    print(f"rss {rss_mb():.0f} MB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["ann", "detail", "fetch", "inventory"])
    ap.add_argument("--cache-dir", type=Path, default=HOME_CACHE / "hodler_step0")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "ann":
        asyncio.run(stage_ann(a.cache_dir))
    elif a.stage == "detail":
        asyncio.run(stage_detail(a.cache_dir))
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(a.cache_dir, a.only))
    else:
        stage_inventory(a.cache_dir)


if __name__ == "__main__":
    main()
