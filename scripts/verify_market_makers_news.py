"""Market-maker news (IDEA-20261017-market-makers, part C, hypothesis H4): offboarding / scandals -> short the perp,
partnerships -> long the perp, entered after the exact tweet time.

Idea card: docs/ideas/IDEA-20261017-market-makers.md (section "Попередня реєстрація (частина C ...)").
Events: docs/verification/2026-10-23-news-mm.csv (Claude's curated set; times from tweet snowflake IDs).
Prices, perp resolution (``resolve_live``), funding, placebo and the trade model are the pipeline of
``scripts/verify_news_events.py`` reused as is (same per-file cache; the trade direction is passed per class) and the
stage-0d layout of ``scripts/verify_news_events_0d.py``. CSV fields are untrusted text: only stored, never
interpreted. No keys, no orders.

Stages (cache under --cache-dir, shared with the news-events stages; outputs are ``mm_*`` files):
    prereg   SHA-256 of the card section (to the end of the file or to "## Частина C: результати")
    prices   resolve the perp live at t0, fetch 1m windows, BTC, Binance / Bybit spot, funding, 1h placebo months
             (--sample N: only the first N CSV rows, for timing)
    stage0   power gate: n, clusters, SD, MDE only (no means), plus per-event symbol / price level / spot ratios
    report   all registered tables

    uv run python scripts/verify_market_makers_news.py <stage> --cache-dir ~/.cache/tradedesk-agents/news_events
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_news_events as v0  # noqa: E402  (stage-0 pipeline: fetchers, measure, collision check)

from tradedesk.analytics.news_events import (  # noqa: E402
    DAY_MS,
    MIN_MS,
    Bars,
    Event,
    MmNews,
    cluster_bootstrap,
    cluster_sd,
    cochran_q,
    dedupe,
    hack_candidates,
    holm,
    in_direction,
    mde_clusters,
    mm_candidates,
    mm_exclusion,
    mm_side,
    one_sided_p,
    paired_diff_bootstrap,
    parse_closures,
    parse_hacks,
    parse_mm_news,
    price_at,
    reaction,
)
from tradedesk.server import build_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "docs" / "ideas" / "IDEA-20261017-market-makers.md"
CSV = ROOT / "docs" / "verification" / "2026-10-23-news-mm.csv"
HACKS_CSV = ROOT / "docs" / "verification" / "2026-10-23-news-hacks.csv"
CLOSURES_CSV = ROOT / "docs" / "verification" / "2026-10-17-news-closures.csv"
SECTION = "## Попередня реєстрація (частина C"
RESULTS = "\n## Частина C: результати"
PRIMARY = "d15_h24_base"
REACT = {"+1m": MIN_MS, **v0.REACT}
GATE_MDE = 0.05
MIN_K = 20
BIG = 0.05  # descriptive size bound: is an effect >= +5 pp per trade excluded by the upper CI?
PRE_CRASH = 0.20  # robustness: |move in the 24 h before t0| >= 20 % (the news came after the move)
Y25 = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
RESOLVE_FILE = "mm_resolve.json"
MEASURED_FILE = "mm_measured.json"


def rss_mb() -> float:
    return v0.rss_mb()


def news() -> list[MmNews]:
    return parse_mm_news(CSV.read_text())


def as_event(n: MmNews, base: str) -> Event:
    return Event(base, n.ms, n.type, "curated", n.url, precise=n.time_source == "tweet_id", note=n.time_source)


def primary_keys(ns: list[MmNews], resolved: dict[str, Any]) -> set[str]:
    """Registered primary pool: ``mm_exclusion`` rules, then the 72 h same-ticker dedupe (on the priced base)."""
    evs, keys = [], {}
    for n in ns:
        if mm_exclusion(n) is not None:
            continue
        cands = mm_candidates(n)
        base = (resolved.get(n.key) or {}).get("base") or (cands[0] if cands else n.token)
        e = as_event(n, base)
        evs.append(e)
        keys[(e.token, e.ms)] = n.key
    kept, _ = dedupe(evs)
    return {keys[(e.token, e.ms)] for e in kept}


def blocked_times(cache: Path, ns: list[MmNews]) -> dict[str, list[int]]:
    """Other event times per base ticker for the placebo: rows of this CSV, the hacks and closures sets, stage-0 events."""
    by: dict[str, list[int]] = defaultdict(list)
    for n in ns:
        for t in mm_candidates(n):
            by[t].append(n.ms)
    for h in parse_hacks(HACKS_CSV.read_text()):
        for t in hack_candidates(h):
            by[t].append(h.ms)
    for c in parse_closures(CLOSURES_CSV.read_text()):
        by[c.token].append(c.ms)
    p = cache / "events.json"
    if p.exists():
        for lst in v0.rd(p).values():
            for d in lst:
                by[str(d["token"])].append(int(d["ms"]) if d.get("precise", True) else int(d["ms"]) + DAY_MS)
    return by


# ================================================================== prices


async def stage_prices(cache: Path, sample: int) -> None:
    _, rt = build_server()
    fx = v0.Fetcher()
    px = v0.Px(cache, rt, fx)
    t_start = time.time()
    try:
        await px.symbols()
        ns = news()[:sample] if sample else news()
        res_p = cache / RESOLVE_FILE
        resolved: dict[str, Any] = v0.rd(res_p) if res_p.exists() else {}
        print(f"mm news rows: {len(ns)}; um symbols {len(px.um)}, spot symbols {len(px.spot)}", flush=True)
        # reference: BTC at the MOVE offboarding post (2025-03-25 07:07 UTC), the registered consistency check
        await px.m1("binance", "BTCUSDT", int(datetime(2025, 3, 25, tzinfo=UTC).timestamp() * 1000))
        for i, n in enumerate(ns, 1):
            if n.key not in resolved:
                r, dead, base = None, [], None
                for cand in mm_candidates(n):
                    r, d = await px.resolve_live(cand, n.ms)
                    dead += d
                    if r:
                        base = cand
                        break
                resolved[n.key] = {"r": r, "dead": dead, "base": base}
                v0.wr(res_p, resolved)
            res = resolved.get(n.key) or {}
            r = res.get("r")
            if r:
                venue, sym, _ = r
                base = res["base"]
                await px.ann_spot("bybit", base, n.ms)  # Bybit spot: second collision check
                d0 = n.ms // DAY_MS * DAY_MS
                jobs = [px.m1(venue, sym, d0 + k * DAY_MS) for k in range(-1, 6)]
                jobs += [px.m1("binance", "BTCUSDT", d0 + k * DAY_MS) for k in range(0, 6)]
                if f"{base}USDT" in px.spot:
                    jobs += [px.m1("binance_spot", f"{base}USDT", d0), px.m1("binance_spot", f"{base}USDT", d0 + DAY_MS)]
                for y, m in v0.months_between(n.ms - 122 * DAY_MS, n.ms + 6 * DAY_MS):
                    jobs.append(px.funding(venue, sym, y, m))
                for y, m in v0.months_between(n.ms - 122 * DAY_MS, n.ms - 3 * DAY_MS):
                    jobs.append(px.h1(venue, sym, y, m))
                await asyncio.gather(*jobs)
            el = time.time() - t_start
            print(
                f"  {i}/{len(ns)} {n.token[:12]} {v0.ymdhm(n.ms)} perp {r} dead {res.get('dead')} "
                f"({el:.0f}s, eta {el / i * (len(ns) - i):.0f}s); files fetched {fx.fetched}; errors {dict(fx.errors)}; rss={rss_mb():.0f}MB",
                flush=True,
            )
        n_ok = sum(1 for n in ns if (resolved.get(n.key) or {}).get("r"))
        print(f"prices done: {n_ok}/{len(ns)} rows have a live Binance/Bybit perp at t0; {time.time() - t_start:.0f}s rss={rss_mb():.0f}MB")
    finally:
        await px.aclose()
        await rt.aclose()


# ================================================================== measurement (cache only)


def bars_1m(cache: Path, venue: str, sym: str, d0: int) -> Bars | None:
    rows: list = []
    for k in range(-1, 6):
        rows += v0.cached(cache, venue, sym, "1m", v0.ymd(d0 + k * DAY_MS)) or []
    return Bars.from_rows(rows) if rows else None


def measure_all(cache: Path) -> list[dict[str, Any]]:
    ns = news()
    resolved = v0.rd(cache / RESOLVE_FILE)
    blocked = blocked_times(cache, ns)
    prim = primary_keys(ns, resolved)
    recs: list[dict[str, Any]] = []
    for n in ns:
        res = resolved.get(n.key) or {}
        r = res.get("r")
        cands = mm_candidates(n)
        base = res.get("base") or (cands[0] if cands else n.token)
        e = as_event(n, base)
        side = mm_side(n)
        rec = v0.measure(cache, e, r, blocked[base], sign=side) if r else None
        if rec is None:
            rec = {"token": base, "ms": n.ms, "category": n.type, "source": "curated", "cluster": n.url, "p0": None, "no_perp": True}
        else:
            t0 = rec["t0"]
            rec["spot_ratio_ann"] = None
            pa = v0.ann_spot_price(cache, "bybit", base, t0)
            if pa and rec.get("p0"):
                rec["spot_ratio_ann"] = (rec["p0"] / r[2]) / pa
            b = bars_1m(cache, r[0], r[1], n.ms // DAY_MS * DAY_MS)
            if b is not None and rec.get("react") is not None:
                rec["react"]["+1m"] = reaction(b, t0, MIN_MS)
                rec["pre24h"] = reaction(b, t0 - DAY_MS, DAY_MS)
        exc = mm_exclusion(n)
        if exc is None and n.key not in prim:
            exc = "dedupe 72h"
        rec.update(
            label=n.token[:14],
            key=n.key,
            type=n.type,
            side=side,
            prior=n.direction_prior,
            time_source=n.time_source,
            mm=n.market_maker[:30],
            perp_csv=list(n.perp_at_t0),
            exclusion=exc,
            primary=exc is None,
            dead=res.get("dead") or [],
        )
        recs.append(rec)
    v0.wr(cache / MEASURED_FILE, recs)
    return recs


def usable(rec: dict[str, Any], key: str = PRIMARY) -> float | None:
    return v0.usable(rec, key)


def pool(recs: list[dict[str, Any]], key: str = PRIMARY, pred: Any = None) -> tuple[list[float], list[str], list[dict]]:
    vals, cl, rr = [], [], []
    for r in recs:
        if not r.get("primary") or (pred and not pred(r)):
            continue
        v = usable(r, key)
        if v is None:
            continue
        vals.append(v)
        cl.append(r["cluster"])
        rr.append(r)
    return vals, cl, rr


fmt = v0.fmt
ci_row = v0.ci_row


def gate(vals: list[float], cl: list[str]) -> tuple[float, int, float, bool]:
    sd, k = cluster_sd(vals, cl) if len(vals) > 1 else (float("nan"), 0)
    m = mde_clusters(sd, k)
    return sd, k, m, bool(m <= GATE_MDE and k >= MIN_K)


def cls(r: dict[str, Any]) -> str:
    return "neg" if r["side"] > 0 else "pos"


# ================================================================== stage0 (power gate, no means)


def stage_stage0(cache: Path) -> None:
    recs = measure_all(cache)
    L: list[str] = []
    out = v0.out
    out(L, f"mm news rows: {len(recs)}; exclusions: {dict(Counter(r['exclusion'] for r in recs if r['exclusion']))}")
    prim = [r for r in recs if r["primary"]]
    out(L, f"primary candidates: {len(prim)}; with a live Binance/Bybit perp at t0: {sum(1 for r in prim if not r.get('no_perp'))}")
    out(L, "no live perp (primary): " + ", ".join(f"{r['label']} {v0.ymd(r['ms'])} dead={r['dead']}" for r in prim if r.get("no_perp")))
    out(L, "\nperp column vs candle check (live perp at t0):")
    for r in recs:
        if bool(r["perp_csv"]) != (not r.get("no_perp")):
            out(L, f"  {r['label']:14s} {v0.ymdhm(r['ms'])} csv {r['perp_csv']} -> ours {r.get('venue')}:{r.get('sym')} dead {r['dead']}")
    out(L, "\nper-event perp and price level (manual collision review; no outcomes):")
    for r in recs:
        if r.get("no_perp"):
            continue
        sb, sa = r.get("spot_ratio"), r.get("spot_ratio_ann")
        out(
            L,
            f"  {r['label']:14s} {v0.ymdhm(r['ms'])} {r['type']:14s} primary={int(r['primary'])} {r.get('venue')}:{r.get('sym')} "
            f"p0 {r.get('p0')} spot/binance {None if sb is None else round(sb, 3)} spot/bybit {None if sa is None else round(sa, 3)} "
            f"check {v0.spot_check(r) if r.get('p0') is not None else '-'} funding missing {int(bool(r.get('funding_missing')))}",
        )
    btc = v0.cached(cache, "binance", "BTCUSDT", "1m", "2025-03-25")
    if btc:
        t = int(datetime(2025, 3, 25, 7, 7, 11, tzinfo=UTC).timestamp() * 1000)
        out(L, f"\nreference: BTCUSDT perp at the MOVE post 2025-03-25 07:07:11 UTC = {price_at(Bars.from_rows(btc), t)} (expected 84-90k)")
    vals, cl, rr = pool(recs)
    sd, k, m, ok = gate(vals, cl)
    out(
        L,
        f"\nprimary pool usable {len(vals)} in {k} clusters (neg {sum(cls(r) == 'neg' for r in rr)}, pos {sum(cls(r) == 'pos' for r in rr)})",
    )
    out(L, f"SD(cluster means) {sd:.3f}; MDE {m:.3f}")
    out(L, f"GATE ({PRIMARY}): MDE {m:.3f} vs {GATE_MDE:.2f}, k {k} vs {MIN_K} -> {'PASS' if ok else 'FAIL'}")
    (cache / "mm_stage0.txt").write_text("\n".join(L) + "\n")
    print(f"rss={rss_mb():.0f}MB")


# ================================================================== report


def mean_med(xs: list[float]) -> str:
    xs = [x for x in xs if x is not None]
    return f"{fmt(statistics.fmean(xs))}/{fmt(statistics.median(xs))} (n {len(xs)})" if xs else "—"


def stage_report(cache: Path) -> None:
    recs = measure_all(cache)
    L: list[str] = []
    out = v0.out
    out(L, f"# market-maker news (part C, H4) report {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
    out(L, f"costs: fee {v0.FEE:.4%}/side, slippage {v0.SLIP:.4%}/side (stress x3); stop {v0.STOP:.0%}; placebo {v0.N_PLACEBO}/event")
    out(L, "trade: negative class (offboard / scandal) short, positive class (partnership) long; net on notional")
    vals, cl, rr = pool(recs)
    sd, k, mde, gate_ok = gate(vals, cl)
    out(L, f"gate: MDE {fmt(mde)} on {k} clusters (min {MIN_K}) -> {'PASS' if gate_ok else 'FAIL'}")
    # ---- coverage
    out(L, "\n## coverage")
    out(
        L,
        f"rows {len(recs)}; primary {sum(r['primary'] for r in recs)}; primary with live perp {sum(1 for r in recs if r['primary'] and not r.get('no_perp'))}",
    )
    out(
        L,
        f"primary usable {len(vals)}; venues {dict(Counter(r['venue'] for r in rr))}; classes {dict(Counter(cls(r) for r in rr))}; collisions "
        f"{[r['label'] for r in recs if r['primary'] and r.get('p0') is not None and v0.spot_check(r) == 'fail']}",
    )
    out(
        L,
        f"spot check status (primary priced): {dict(Counter(v0.spot_check(r) for r in recs if r['primary'] and r.get('p0') is not None))}",
    )
    out(
        L,
        f"truncated (primary) {sum(1 for r in rr if r['trades'][PRIMARY]['truncated'])}; funding missing "
        f"{sum(1 for r in rr if r.get('funding_missing'))}, partial {sum(1 for r in rr if r.get('funding_partial'))}",
    )
    # ---- per-event table
    out(L, "\n## per event (primary pool; reactions are raw price changes from t0; net = class trade d15 h24)")
    for r in sorted(rr, key=lambda r: r["ms"]):
        rc = r["react"]
        t = r["trades"][PRIMARY]
        out(
            L,
            f"  {r['label']:6s} {v0.ymdhm(r['ms'])} {r['type']:14s} {'S' if r['side'] > 0 else 'L'} {r['venue']}:{r['sym']} | pre24h {fmt(r.get('pre24h'))} "
            f"pre60 {fmt(r.get('pre60'))} | " + " ".join(f"{h} {fmt(rc.get(h))}" for h in REACT) + f" | net {fmt(t['net'])} "
            f"(gross {fmt(t['gross'])}, fund {fmt(t['funding'], 2)}, MAE {fmt(t['mae'])})",
        )
    # ---- reactions
    out(L, "\n## reactions from t0 in the class direction (+ = the class trade gains; mean/median)")
    for lab, sub in (("all", rr), ("negative", [r for r in rr if cls(r) == "neg"]), ("positive", [r for r in rr if cls(r) == "pos"])):
        cells = [f"pre24h {mean_med([in_direction(r['pre24h'], r['side']) for r in sub if r.get('pre24h') is not None])}"]
        cells.append(f"pre60 {mean_med([in_direction(r['pre60'], r['side']) for r in sub if r.get('pre60') is not None])}")
        cells += [f"{h} {mean_med([in_direction(r['react'][h], r['side']) for r in sub if r['react'].get(h) is not None])}" for h in REACT]
        out(L, f"{lab}: " + "; ".join(cells))
    # ---- H4
    out(L, "\n## H4: class trade entered t0+15m, exit +24h, base costs + funding (net, on notional)")
    out(L, f"PRIMARY: {ci_row(vals, cl)}")
    pl = [statistics.fmean(r["placebo"]["h24"]) if r.get("placebo") and r["placebo"]["h24"] else None for r in rr]
    idx = [i for i, p in enumerate(pl) if p is not None]
    ev_v, pl_v, cl_v = [vals[i] for i in idx], [pl[i] for i in idx], [cl[i] for i in idx]
    lo_d = float("nan")
    if len(ev_v) > 1:
        out(L, f"  placebo (same perps and direction, {v0.N_PLACEBO} random hours before t0): {ci_row(pl_v, cl_v)}")  # type: ignore[arg-type]
        md, lo_d, hi_d = paired_diff_bootstrap(ev_v, pl_v, cl_v)  # type: ignore[arg-type]
        out(L, f"  event - placebo: {fmt(md)} [{fmt(lo_d)}; {fmt(hi_d)}] (n {len(ev_v)}; no placebo: {len(vals) - len(ev_v)})")
    m, lo, hi = cluster_bootstrap(vals, cl) if vals else (float("nan"),) * 3
    med = statistics.median(vals) if vals else float("nan")
    v1, c1, _ = pool(recs, pred=lambda r: r["ms"] < Y25)
    v2, c2, _ = pool(recs, pred=lambda r: r["ms"] >= Y25)
    out(L, f"  2023-2024: {ci_row(v1, c1)}")
    out(L, f"  2025-2026: {ci_row(v2, c2)}")
    halves = bool(v1 and v2 and statistics.fmean(v1) > 0 and statistics.fmean(v2) > 0)
    out(L, f"  CI clustered by token: {ci_row(vals, [r['token'] for r in rr])}")
    kill = hi <= 0 or (not (lo_d > 0) and lo <= 0 <= hi and med <= 0)
    if not gate_ok:
        verdict = "INCONCLUSIVE (power gate failed; means descriptive)"
    elif lo > 0 and lo_d > 0 and halves:
        verdict = "CONFIRMED"
    elif kill:
        verdict = "REJECTED"
    else:
        verdict = "INCONCLUSIVE"
    out(L, f"  verdict rule: {verdict}; median {fmt(med)}; both halves > 0: {halves}")
    out(L, f"  kill criterion (would apply only after the gate): {kill}")
    out(L, f"  size bound: upper CI {fmt(hi)} < +{BIG:.0%} -> effect >= {BIG:.0%} per trade excluded: {hi < BIG}")
    # ---- classes (secondary, Holm over 2)
    out(L, "\n## classes (secondary; one-sided bootstrap p that mean <= 0, Holm over 2)")
    ps = {}
    for c in ("neg", "pos"):
        v, cc, _ = pool(recs, pred=lambda r, c=c: cls(r) == c)
        ps[c] = one_sided_p(v, cc)
        out(L, f"{c} ({'short' if c == 'neg' else 'long'}): {ci_row(v, cc)}; p {ps[c]:.3f}")
    adj = holm(ps)
    out(L, f"Holm-adjusted p: {', '.join(f'{c} {p:.3f}' for c, p in adj.items())}")
    # ---- grid
    out(L, "\n## grid (net mean [CI]) delays x holds x cost/stop")
    for d in v0.DELAYS:
        for hh in v0.HOLDS:
            cells = []
            for tag in ("base", "stress", "stop"):
                v, c, _ = pool(recs, f"d{d}_h{hh}_{tag}")
                if len(v) > 1:
                    mm, l2, h2 = cluster_bootstrap(v, c)
                    cells.append(f"{tag} {fmt(mm)} [{fmt(l2)}; {fmt(h2)}] med {fmt(statistics.median(v))}")
            out(L, f"d{d} h{hh} (n {len(pool(recs, f'd{d}_h{hh}_base')[0])}): " + "; ".join(cells))
    for hh in v0.HOLDS:
        v, c, rl = pool(recs, f"d15_h{hh}_base")
        plv = [statistics.fmean(r["placebo"][f"h{hh}"]) for r in rl if r.get("placebo") and r["placebo"][f"h{hh}"]]
        out(L, f"placebo h{hh}: mean {fmt(statistics.fmean(plv)) if plv else '—'} (n {len(plv)})")
    # ---- components
    if rr:
        out(L, "\n## primary trade components")
        trs = [r["trades"][PRIMARY] for r in rr]
        out(
            L,
            f"gross {fmt(statistics.fmean(t['gross'] for t in trs))}; funding {fmt(statistics.fmean(t['funding'] for t in trs), 2)}; "
            f"cost {fmt(statistics.fmean(t['cost'] for t in trs), 2)}; MAE median {fmt(statistics.median(t['mae'] for t in trs))}, "
            f"p90 {fmt(float(np.quantile([t['mae'] for t in trs], 0.9)))}; stopped (stop variant) "
            f"{sum(1 for r in rr if (r['trades'].get('d15_h24_stop') or {}).get('stopped'))}",
        )
        hv = [
            r["trades"][PRIMARY]["net"] - (r["trades"].get("d15_h24_btc") or 0.0) for r in rr if r["trades"].get("d15_h24_btc") is not None
        ]
        hc = [r["cluster"] for r in rr if r["trades"].get("d15_h24_btc") is not None]
        out(L, f"BTC-hedged (opposite BTC leg): {ci_row(hv, hc)}")
    # ---- robustness
    out(L, "\n## robustness (primary trade)")
    slices = [
        ("2023-2024", lambda r: r["ms"] < Y25),
        ("2025-2026", lambda r: r["ms"] >= Y25),
        (f"without |pre24h| >= {PRE_CRASH:.0%}", lambda r: r.get("pre24h") is not None and abs(r["pre24h"]) < PRE_CRASH),
        ("mm_offboard only", lambda r: r["type"] == "mm_offboard"),
        ("mm_scandal only", lambda r: r["type"] == "mm_scandal"),
        ("spot-checked only", lambda r: r.get("p0") is not None and v0.spot_check(r) != "none"),
    ]
    for lab, pr in slices:
        v, c, _ = pool(recs, pred=pr)
        out(L, f"{lab}: {ci_row(v, c)}")
    order = sorted(range(len(vals)), key=lambda i: -vals[i])
    for drop in (1, 3):
        keep = sorted(order[drop:])
        out(L, f"without top {drop}: {ci_row([vals[i] for i in keep], [cl[i] for i in keep])}")
    out(L, "ranked: " + ", ".join(f"{rr[i]['label']} {v0.ymd(rr[i]['ms'])} {fmt(vals[i])}" for i in order))
    # ---- 5a
    out(L, "\n## 5a: per token")
    g: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for v, r in zip(vals, rr, strict=True):
        g[r["token"]].append((r["ms"], v))
    for t, lst in sorted(g.items(), key=lambda x: -len(x[1])):
        if len(lst) >= 2:
            v = [x for _, x in lst]
            out(L, f"  {t}: n {len(v)} mean {fmt(statistics.fmean(v))} sd {fmt(statistics.stdev(v))} events {[fmt(x) for x in v]}")
    out(L, f"  tokens with one event: {sum(1 for lst in g.values() if len(lst) == 1)}")
    multi = [(t, [x for _, x in lst]) for t, lst in g.items() if len(lst) >= 2 and statistics.stdev([x for _, x in lst]) > 0]
    if len(multi) >= 2:
        q, df, i2, pq = cochran_q([statistics.fmean(v) for _, v in multi], [statistics.stdev(v) / len(v) ** 0.5 for _, v in multi])
        out(L, f"tokens with >= 2 events: {[t for t, _ in multi]}; Cochran Q {q:.2f} df {df} I2 {i2:.2f} p {pq:.3f}")
    else:
        out(L, f"Cochran Q: not computable (tokens with >= 2 events: {[t for t, _ in multi]})")
    both = [t for t, lst in g.items() if any(ms < Y25 for ms, _ in lst) and any(ms >= Y25 for ms, _ in lst)]
    out(L, f"persistence: tokens with events in both halves: {both} -> " + ("tested" if len(both) >= 8 else "not testable (< 8)"))
    # ---- descriptive rows outside the primary pool
    out(L, "\n## descriptive (outside the primary pool or no usable perp; reactions raw from t0; net = class trade d15 h24)")
    for r in recs:
        if r["primary"] and usable(r) is not None:
            continue
        if r.get("p0") is None:
            out(L, f"  {r['label']} {v0.ymdhm(r['ms'])} ({r['exclusion']}): no live perp at t0; csv {r['perp_csv']} dead {r['dead']}")
            continue
        rc = r.get("react") or {}
        tr = (r.get("trades") or {}).get(PRIMARY)
        out(
            L,
            f"  {r['label']} {v0.ymdhm(r['t0'])} {r['type']} prior {r['prior']} ({r['exclusion']}) {r['venue']}:{r['sym']} check {v0.spot_check(r)}: "
            f"pre24h {fmt(r.get('pre24h'))} "
            + " ".join(f"{h} {fmt(rc.get(h))}" for h in REACT)
            + f" | net {fmt(tr['net'] if tr else None)}",
        )
    (cache / "mm_report.txt").write_text("\n".join(L) + "\n")
    print(f"\nrss={rss_mb():.0f}MB; written {cache / 'mm_report.txt'}", flush=True)


def stage_prereg() -> None:
    text = CARD.read_text()
    sec = text[text.index(SECTION) :]
    if RESULTS in sec:  # the results block appended after the run is outside the registered text
        sec = sec[: sec.index(RESULTS)]
    sec = sec.encode()
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec).hexdigest()} bytes={len(sec)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["prereg", "prices", "stage0", "report"])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "tradedesk-agents" / "news_events")
    ap.add_argument("--sample", type=int, default=0, help="prices: only the first N CSV rows (timing)")
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "prices":
        asyncio.run(stage_prices(a.cache_dir, a.sample))
    elif a.stage == "stage0":
        stage_stage0(a.cache_dir)
    else:
        stage_report(a.cache_dir)


if __name__ == "__main__":
    main()
