"""News events (IDEA-20261017-news-events), stage 0b: project closures, deprecations, sunsets, wind-downs.

Idea card: docs/ideas/IDEA-20261017-news-events.md (section "Попередня реєстрація 0b").
Events: docs/verification/2026-10-17-news-closures.csv (Claude's curated set; times from tweet snowflake IDs).
Prices, perp resolution (``resolve_live``: Binance USD-M archive incl. delisted symbols, else Bybit linear; perp must be
live at t0), funding, placebo and the trade model are the stage-0 pipeline of ``scripts/verify_news_events.py``,
reused as is (same per-file cache). CSV fields are untrusted text: only stored, never interpreted. No keys, no orders.

Stages (cache under --cache-dir, shared with stage 0; 0b outputs are ``closures_*`` files):
    prereg   SHA-256 of the card section "## Попередня реєстрація 0b" (to the end of the file)
    prices   resolve the perp live at t0, fetch 1m windows, BTC, Binance / Bybit spot, funding, 1h placebo months,
             and the perps listed today (ccxt markets) for the "not in Claude's preliminary measurement" subset
    stage0   power gate: n, SD, MDE only (no means), plus per-event symbol / price level / spot ratios for the
             manual ticker-collision review
    report   all registered tables (after the gate)

    uv run python scripts/verify_news_events_0b.py <stage> --cache-dir ~/.cache/tradedesk-agents/news_events
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
    CLOSURE_PARTIAL,
    CLOSURE_WHOLE,
    DAY_MS,
    MIN_MS,
    OFFICIAL_TIME,
    Bars,
    Closure,
    Event,
    closure_exclusion,
    cluster_bootstrap,
    cluster_sd,
    cochran_q,
    dedupe,
    mde_clusters,
    move_share_bootstrap,
    move_share_median,
    paired_diff_bootstrap,
    parse_closures,
    reaction,
)
from tradedesk.server import build_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "docs" / "ideas" / "IDEA-20261017-news-events.md"
CSV = ROOT / "docs" / "verification" / "2026-10-17-news-closures.csv"
SECTION = "## Попередня реєстрація 0b"
PRIMARY = "d15_h24_base"
REACT = {"+1m": MIN_MS, **v0.REACT}
SPEED_H = ("+1m", "+5m", "+15m")
GATE_MDE = 0.10
THRESH = 0.10
RESOLVE_FILE = "closures_resolve.json"
LISTED_FILE = "closures_listed_now.json"
MEASURED_FILE = "closures_measured.json"


def rss_mb() -> float:
    return v0.rss_mb()


def closures() -> list[Closure]:
    return parse_closures(CSV.read_text())


def as_event(c: Closure) -> Event:
    return Event(c.token, c.ms, "closure", "curated", c.key, precise=True, note=c.type)


def primary_keys(cs: list[Closure]) -> set[str]:
    """Registered primary pool: exclusions by closure_exclusion, then the 72 h same-token dedupe."""
    pool = [c for c in cs if closure_exclusion(c) is None]
    kept, _ = dedupe([as_event(c) for c in pool])
    return {e.cluster for e in kept}


def blocked_times(cache: Path, cs: list[Closure]) -> dict[str, list[int]]:
    """Other event times per token for the placebo: the CSV and every stage-0 event (delistings, hacks, listings)."""
    by: dict[str, list[int]] = defaultdict(list)
    for c in cs:
        by[c.token].append(c.ms)
    p = cache / "events.json"
    if p.exists():
        for lst in v0.rd(p).values():
            for d in lst:
                t = int(d["ms"]) if d.get("precise", True) else int(d["ms"]) + DAY_MS
                by[str(d["token"])].append(t)
    return by


# ================================================================== prices


async def stage_prices(cache: Path) -> None:
    _, rt = build_server()
    fx = v0.Fetcher()
    px = v0.Px(cache, rt, fx)
    t_start = time.time()
    try:
        await px.symbols()
        cs = closures()
        res_p = cache / RESOLVE_FILE
        resolved: dict[str, Any] = v0.rd(res_p) if res_p.exists() else {}
        print(f"closures: {len(cs)}; um symbols {len(px.um)}, spot symbols {len(px.spot)}", flush=True)
        for i, c in enumerate(cs, 1):
            e = as_event(c)
            t0 = c.ms
            if c.key not in resolved:
                r, dead = await px.resolve_live(c.token, t0)
                resolved[c.key] = {"r": r, "dead": dead}
                v0.wr(res_p, resolved)
            r = resolved[c.key]["r"]
            if r:
                venue, sym, _ = r
                await px.ann_spot("bybit", c.token, t0)  # Bybit spot: second collision check
                d0 = e.ms // DAY_MS * DAY_MS
                jobs = [px.m1(venue, sym, d0 + k * DAY_MS) for k in range(-1, 6)]
                jobs += [px.m1("binance", "BTCUSDT", d0 + k * DAY_MS) for k in range(0, 6)]
                if f"{c.token}USDT" in px.spot:
                    jobs.append(px.m1("binance_spot", f"{c.token}USDT", d0))
                for y, m in v0.months_between(e.ms - 122 * DAY_MS, e.ms + 6 * DAY_MS):
                    jobs.append(px.funding(venue, sym, y, m))
                for y, m in v0.months_between(e.ms - 122 * DAY_MS, e.ms - 3 * DAY_MS):
                    jobs.append(px.h1(venue, sym, y, m))
                await asyncio.gather(*jobs)
            el = time.time() - t_start
            print(
                f"  {i}/{len(cs)} {c.token} {v0.ymdhm(c.ms)} perp {r} dead {resolved[c.key]['dead']} "
                f"({el:.0f}s, eta {el / i * (len(cs) - i):.0f}s); files fetched {fx.fetched}; errors {dict(fx.errors)}; rss={rss_mb():.0f}MB",
                flush=True,
            )
        # perps listed today (for the "outside Claude's preliminary measurement" subset)
        lp = cache / LISTED_FILE
        if not lp.exists():
            listed: dict[str, list[str]] = {}
            for ex_id in ("binanceusdm", "bybit"):
                mk = (await rt.exchanges.markets(ex_id)).data
                listed[ex_id] = sorted(
                    str(m.get("id"))
                    for m in mk.values()
                    if m.get("swap") and m.get("linear") and m.get("active") and m.get("quote") == "USDT"
                )
                print(f"  listed now on {ex_id}: {len(listed[ex_id])} active USDT linear perps", flush=True)
            v0.wr(lp, listed)
        n_ok = sum(1 for c in cs if (resolved.get(c.key) or {}).get("r"))
        print(
            f"prices done: {n_ok}/{len(cs)} closures have a live Binance/Bybit perp at t0; {time.time() - t_start:.0f}s rss={rss_mb():.0f}MB"
        )
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
    cs = closures()
    resolved = v0.rd(cache / RESOLVE_FILE)
    listed = v0.rd(cache / LISTED_FILE) if (cache / LISTED_FILE).exists() else {}
    now_ids = set(listed.get("binanceusdm", [])) | set(listed.get("bybit", []))
    blocked = blocked_times(cache, cs)
    prim = primary_keys(cs)
    recs: list[dict[str, Any]] = []
    for c in cs:
        e = as_event(c)
        res = resolved.get(c.key) or {}
        r = res.get("r")
        rec = v0.measure(cache, e, r, blocked[c.token]) if r else None
        if rec is None:
            rec = {"token": c.token, "ms": c.ms, "category": "closure", "source": "curated", "cluster": c.key, "p0": None, "no_perp": True}
        else:
            rec["spot_ratio_ann"] = None
            pa = v0.ann_spot_price(cache, "bybit", c.token, c.ms)  # Bybit spot (second collision check)
            if pa and rec.get("p0"):
                rec["spot_ratio_ann"] = (rec["p0"] / r[2]) / pa
            b = bars_1m(cache, r[0], r[1], c.ms // DAY_MS * DAY_MS)
            if b is not None and rec.get("react") is not None:
                rec["react"]["+1m"] = reaction(b, c.ms, MIN_MS)
        rec.update(
            type=c.type,
            direction_prior=c.direction_prior,
            time_source=c.time_source,
            exclusion=closure_exclusion(c) or ("dedupe 72h" if c.key not in prim else None),
            primary=c.key in prim,
            dead=res.get("dead") or [],
            listed_now=bool(r) and r[1] in now_ids,
        )
        # Claude's preliminary measurement: official events whose perp trades today (card, 0b)
        rec["pre_measured"] = rec["listed_now"] and c.time_source == "official_tweet"
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


def fmt(x: float | None, p: int = 1) -> str:
    return v0.fmt(x, p)


def ci_row(vals: list[float], cl: list[str]) -> str:
    return v0.ci_row(vals, cl)


# ================================================================== stage0 (power gate, no means)


def stage_stage0(cache: Path) -> None:
    recs = measure_all(cache)
    L: list[str] = []
    out = v0.out
    cs = closures()
    out(L, f"closures: {len(cs)}; exclusions: {dict(Counter(r['exclusion'] for r in recs if r['exclusion']))}")
    prim = [r for r in recs if r["primary"]]
    out(L, f"primary candidates: {len(prim)}; with a live Binance/Bybit perp at t0: {sum(1 for r in prim if not r.get('no_perp'))}")
    out(L, "no live perp (primary): " + ", ".join(f"{r['token']} {v0.ymd(r['ms'])} dead={r['dead']}" for r in prim if r.get("no_perp")))
    out(L, "\nper-event perp and price level (manual collision review; no outcomes):")
    for r in recs:
        if r.get("no_perp"):
            continue
        sb, sa = r.get("spot_ratio"), r.get("spot_ratio_ann")
        out(
            L,
            f"  {r['token']:6s} {v0.ymdhm(r['ms'])} {r['type']:22s} primary={int(r['primary'])} {r.get('venue')}:{r.get('sym')} "
            f"p0 {r.get('p0')} spot/binance {None if sb is None else round(sb, 3)} spot/bybit {None if sa is None else round(sa, 3)} "
            f"check {v0.spot_check(r) if r.get('p0') is not None else '-'} listed_now {int(r['listed_now'])} "
            f"funding missing {int(bool(r.get('funding_missing')))} partial {int(bool(r.get('funding_partial')))}",
        )
    vals, cl, _ = pool(recs)
    sd, k = cluster_sd(vals, cl) if len(vals) > 1 else (float("nan"), 0)
    m = mde_clusters(sd, k)
    out(L, f"\nprimary pool usable {len(vals)} in {k} clusters; SD(cluster means) {sd:.3f}; MDE {m:.3f}")
    out(L, f"GATE ({PRIMARY}): MDE {m:.3f} vs {GATE_MDE:.2f} -> {'PASS' if m <= GATE_MDE else 'FAIL'}")
    (cache / "closures_stage0.txt").write_text("\n".join(L) + "\n")
    print(f"rss={rss_mb():.0f}MB")


# ================================================================== report


def stage_report(cache: Path) -> None:
    recs = measure_all(cache)
    L: list[str] = []
    out = v0.out
    out(L, f"# news events 0b (closures) report {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
    out(L, f"costs: fee {v0.FEE:.4%}/side, slippage {v0.SLIP:.4%}/side (stress x3); stop {v0.STOP:.0%}; placebo {v0.N_PLACEBO}/event")
    vals, cl, rr = pool(recs)
    sd, k = cluster_sd(vals, cl)
    mde = mde_clusters(sd, k)
    out(L, f"gate: MDE {fmt(mde)} on {k} events -> {'PASS' if mde <= GATE_MDE else 'FAIL'}")
    # ---- coverage
    out(L, "\n## coverage")
    out(
        L,
        f"rows {len(recs)}; primary {sum(r['primary'] for r in recs)}; primary with live perp {sum(1 for r in recs if r['primary'] and not r.get('no_perp'))}",
    )
    out(
        L,
        f"primary usable {len(vals)}; venues {dict(Counter(r['venue'] for r in rr))}; collisions {[r['token'] for r in recs if r['primary'] and r.get('p0') is not None and v0.spot_check(r) == 'fail']}",
    )
    out(
        L,
        f"spot check status (primary priced): {dict(Counter(v0.spot_check(r) for r in recs if r['primary'] and r.get('p0') is not None))}",
    )
    out(
        L,
        f"truncated (primary) {sum(1 for r in rr if r['trades'][PRIMARY]['truncated'])}; funding missing {sum(1 for r in rr if r.get('funding_missing'))}, partial {sum(1 for r in rr if r.get('funding_partial'))}",
    )
    out(
        L,
        f"pre-measured by Claude (official + perp listed now): {sum(r['pre_measured'] for r in rr)}; not pre-measured {sum(not r['pre_measured'] for r in rr)}",
    )
    # ---- per-event table
    out(L, "\n## per event (primary pool, short d15 h24 net; reactions from t0)")
    for r in sorted(rr, key=lambda r: r["ms"]):
        rc = r["react"]
        out(
            L,
            f"  {r['token']:6s} {v0.ymdhm(r['ms'])} {r['type']:22s} {r['time_source']:22s} {r['venue']}:{r['sym']} pre {int(r['pre_measured'])} | "
            + " ".join(f"{h} {fmt(rc.get(h))}" for h in REACT)
            + f" | pre60 {fmt(r.get('pre60'))} | net {fmt(r['trades'][PRIMARY]['net'])} (gross {fmt(r['trades'][PRIMARY]['gross'])}, "
            f"fund {fmt(r['trades'][PRIMARY]['funding'], 2)}, MAE {fmt(r['trades'][PRIMARY]['mae'])})",
        )
    # ---- reactions and speed
    out(L, "\n## reactions from t0 (primary pool; mean / median)")
    cells = []
    for h in ["pre60", *REACT]:
        xs = [r["pre60"] if h == "pre60" else r["react"].get(h) for r in rr]
        xs = [x for x in xs if x is not None]
        cells.append(f"{h} {fmt(statistics.fmean(xs)) if xs else '—'}/{fmt(statistics.median(xs)) if xs else '—'}")
    out(L, f"n {len(rr)}: " + "; ".join(cells))
    out(L, "\n## speed: share of the 24 h move done by h")
    for lab, pr in (("primary", None), ("primary, official time only", lambda r: r["time_source"] in OFFICIAL_TIME)):
        sub = [r for r in rr if (pr is None or pr(r)) and r["react"].get("+24h") is not None]
        for h in SPEED_H:
            ok = [r for r in sub if r["react"].get(h) is not None]
            a, z = [r["react"][h] for r in ok], [r["react"]["+24h"] for r in ok]
            s, lo, hi = move_share_bootstrap(a, z)
            med, nm = move_share_median(a, z)
            out(
                L,
                f"{lab} {h}: pooled {s * 100:.0f}% [{lo * 100:.0f}%; {hi * 100:.0f}%] (n {len(ok)}); per-event median {med * 100:.0f}% (n {nm}, |r24| >= 5%)",
            )
    # ---- H0b
    out(L, "\n## H0b: short entered t0+15m, exit +24h, base costs + funding (net, on notional)")
    out(L, f"PRIMARY: {ci_row(vals, cl)}")
    pl = [statistics.fmean(r["placebo"]["h24"]) if r.get("placebo") and r["placebo"]["h24"] else None for r in rr]
    idx = [i for i, p in enumerate(pl) if p is not None]
    ev_v, pl_v, cl_v = [vals[i] for i in idx], [pl[i] for i in idx], [cl[i] for i in idx]
    md = lo_d = hi_d = float("nan")
    if len(ev_v) > 1:
        out(L, f"  placebo (same perps, {v0.N_PLACEBO} random hours before t0): {ci_row(pl_v, cl_v)}")
        md, lo_d, hi_d = paired_diff_bootstrap(ev_v, pl_v, cl_v)
        out(L, f"  event - placebo: {fmt(md)} [{fmt(lo_d)}; {fmt(hi_d)}] (n {len(ev_v)}; no placebo: {len(vals) - len(ev_v)})")
    m, lo, hi = cluster_bootstrap(vals, cl)
    med = statistics.median(vals)
    nv, nc, _ = pool(recs, pred=lambda r: not r["pre_measured"])
    m_new = statistics.fmean(nv) if nv else float("nan")
    out(L, f"  not pre-measured subset: {ci_row(nv, nc)}")
    tv, _, tr_ = pool(recs)
    tok_cl = [r["token"] for r in tr_]
    out(L, f"  CI clustered by token: {ci_row(tv, tok_cl)}")
    gate = mde <= GATE_MDE
    if not gate:
        verdict = "INCONCLUSIVE (power gate failed; means descriptive)"
    elif lo > 0 and lo_d > 0 and m_new > 0:
        verdict = "CONFIRMED"
    elif lo > 0 and lo_d > 0:
        verdict = "INCONCLUSIVE (not confirmed outside the preliminary measurement)"
    elif not (lo_d > 0) and lo <= 0 <= hi and med <= 0:
        verdict = "REJECTED"
    else:
        verdict = "INCONCLUSIVE"
    out(L, f"  verdict rule: {verdict}; mean >= {THRESH:.0%} (card H1 form): {m >= THRESH}; median {fmt(med)}")
    kill = not (lo_d > 0) and lo <= 0 <= hi and med <= 0
    out(L, f"  kill criterion (card: not better than placebo, CI includes 0, median <= 0): {kill}")
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
    out(L, "\n## primary trade components")
    trs = [r["trades"][PRIMARY] for r in rr]
    out(
        L,
        f"gross {fmt(statistics.fmean(t['gross'] for t in trs))}; funding {fmt(statistics.fmean(t['funding'] for t in trs), 2)}; "
        f"cost {fmt(statistics.fmean(t['cost'] for t in trs), 2)}; MAE median {fmt(statistics.median(t['mae'] for t in trs))}, "
        f"p90 {fmt(float(np.quantile([t['mae'] for t in trs], 0.9)))}; share MAE >= 25% {sum(t['mae'] >= v0.STOP for t in trs) / len(trs) * 100:.0f}%; "
        f"stopped (stop variant) {sum(1 for r in rr if (r['trades'].get('d15_h24_stop') or {}).get('stopped'))}",
    )
    hv = [r["trades"][PRIMARY]["net"] - (r["trades"].get("d15_h24_btc") or 0.0) for r in rr if r["trades"].get("d15_h24_btc") is not None]
    hc = [r["cluster"] for r in rr if r["trades"].get("d15_h24_btc") is not None]
    out(L, f"BTC-hedged (short token, long BTC): {ci_row(hv, hc)}")
    # ---- robustness
    out(L, "\n## robustness (primary trade)")
    y26 = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    slices = [
        ("official time only", lambda r: r["time_source"] in OFFICIAL_TIME),
        ("direction prior down", lambda r: r["direction_prior"] == "down"),
        ("whole project ends", lambda r: r["type"] in CLOSURE_WHOLE),
        ("partial (product / alliance / governance)", lambda r: r["type"] in CLOSURE_PARTIAL),
        ("2023-2025", lambda r: r["ms"] < y26),
        ("2026", lambda r: r["ms"] >= y26),
        ("tickers > 2 chars", lambda r: len(r["token"]) > 2),
        ("not pre-measured by Claude", lambda r: not r["pre_measured"]),
        ("pre-measured by Claude", lambda r: r["pre_measured"]),
        ("perp on Binance", lambda r: r.get("venue") == "binance"),
        ("perp on Bybit", lambda r: r.get("venue") == "bybit"),
        ("spot-checked only", lambda r: r.get("p0") is not None and v0.spot_check(r) != "none"),
    ]
    for lab, pr in slices:
        v, c, _ = pool(recs, pred=pr)
        out(L, f"{lab}: {ci_row(v, c)}")
    order = sorted(range(len(vals)), key=lambda i: -vals[i])
    for drop in (1, 3, 5):
        keep = sorted(order[drop:])
        out(L, f"without top {drop}: {ci_row([vals[i] for i in keep], [cl[i] for i in keep])}")
    out(L, "ranked: " + ", ".join(f"{rr[i]['token']} {v0.ymd(rr[i]['ms'])} {fmt(vals[i])}" for i in order))
    # ---- 5a: per token / per type
    out(L, "\n## 5a: per token and per type")
    g: dict[str, list[float]] = defaultdict(list)
    for v, r in zip(vals, rr, strict=True):
        g[r["token"]].append(v)
    for t, v in sorted(g.items(), key=lambda x: -len(x[1])):
        out(L, f"  {t}: n {len(v)} mean {fmt(statistics.fmean(v))} " + (f"sd {fmt(statistics.stdev(v))}" if len(v) > 1 else ""))
    multi = [(t, v) for t, v in g.items() if len(v) >= 2]
    if len(multi) >= 2:
        means = [statistics.fmean(v) for _, v in multi]
        ses = [statistics.stdev(v) / len(v) ** 0.5 for _, v in multi]
        q, df, i2, pq = cochran_q(means, ses)
        out(L, f"tokens with >= 2 events: {[t for t, _ in multi]}; Cochran Q {q:.2f} df {df} I2 {i2:.2f} p {pq:.3f}")
    gt: dict[str, list[float]] = defaultdict(list)
    for v, r in zip(vals, rr, strict=True):
        gt[r["type"]].append(v)
    for t, v in sorted(gt.items()):
        out(L, f"  type {t}: n {len(v)} mean {fmt(statistics.fmean(v))}")
    mt = [(t, v) for t, v in gt.items() if len(v) >= 2 and statistics.stdev(v) > 0]
    if len(mt) >= 2:
        q, df, i2, pq = cochran_q([statistics.fmean(v) for _, v in mt], [statistics.stdev(v) / len(v) ** 0.5 for _, v in mt])
        out(L, f"types with >= 2 events: Cochran Q {q:.2f} df {df} I2 {i2:.2f} p {pq:.3f}")
    # ---- descriptive rows outside the primary pool
    out(L, "\n## descriptive (outside the primary pool)")
    for r in recs:
        if r["primary"]:
            continue
        if r.get("p0") is None:
            out(L, f"  {r['token']} {v0.ymdhm(r['ms'])} ({r['exclusion']}): no live perp at t0")
            continue
        rc = r.get("react") or {}
        tr = (r.get("trades") or {}).get(PRIMARY)
        out(
            L,
            f"  {r['token']} {v0.ymdhm(r['ms'])} ({r['exclusion']}) {r['venue']}:{r['sym']} check {v0.spot_check(r)}: "
            + " ".join(f"{h} {fmt(rc.get(h))}" for h in REACT)
            + f" | short d15 h24 net {fmt(tr['net'] if tr else None)}",
        )
    (cache / "closures_report.txt").write_text("\n".join(L) + "\n")
    print(f"\nrss={rss_mb():.0f}MB; written {cache / 'closures_report.txt'}", flush=True)


def stage_prereg() -> None:
    text = CARD.read_text()
    sec = text[text.index(SECTION) :].encode()
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec).hexdigest()} bytes={len(sec)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["prereg", "prices", "stage0", "report"])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "tradedesk-agents" / "news_events")
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "prices":
        asyncio.run(stage_prices(a.cache_dir))
    elif a.stage == "stage0":
        stage_stage0(a.cache_dir)
    else:
        stage_report(a.cache_dir)


if __name__ == "__main__":
    main()
