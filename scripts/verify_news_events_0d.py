"""News events (IDEA-20261017-news-events), stage 0d: hacks with the exact time of the first tweet.

Idea card: docs/ideas/IDEA-20261017-news-events.md (section "Попередня реєстрація 0d").
Events: docs/verification/2026-10-23-news-hacks.csv (Claude's curated set; times from tweet snowflake IDs).
Prices, perp resolution (``resolve_live``: Binance USD-M archive incl. delisted symbols, else Bybit linear; perp must be
live at t0), funding, placebo and the trade model are the stage-0 / 0b pipeline of ``scripts/verify_news_events.py``,
reused as is (same per-file cache). CSV fields are untrusted text: only stored, never interpreted. No keys, no orders.

Stages (cache under --cache-dir, shared with stages 0 / 0b; 0d outputs are ``hacks_*`` files):
    prereg   SHA-256 of the card section "## Попередня реєстрація 0d" (to the end of the file)
    prices   resolve the perp live at t0 (candidates: CSV perp bases, then the token), fetch 1m windows, BTC,
             Binance / Bybit spot, funding, 1h placebo months  (--sample N: only the first N rows, for timing)
    stage0   power gate: n, clusters, SD, MDE only (no means), plus per-event symbol / price level / spot ratios
    report   all registered tables (after the gate)

    uv run python scripts/verify_news_events_0d.py <stage> --cache-dir ~/.cache/tradedesk-agents/news_events
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
    HACK_TIME_MONITORED,
    MIN_MS,
    Bars,
    Event,
    Hack,
    cluster_bootstrap,
    cluster_sd,
    cochran_q,
    dedupe,
    hack_candidates,
    hack_exclusion,
    mde_clusters,
    move_share_bootstrap,
    move_share_median,
    paired_diff_bootstrap,
    parse_closures,
    parse_hacks,
    price_at,
    reaction,
    residual_short,
)
from tradedesk.server import build_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "docs" / "ideas" / "IDEA-20261017-news-events.md"
CSV = ROOT / "docs" / "verification" / "2026-10-23-news-hacks.csv"
CLOSURES_CSV = ROOT / "docs" / "verification" / "2026-10-17-news-closures.csv"
SECTION = "## Попередня реєстрація 0d"
RESULTS = "\n## 0d: результати"
PRIMARY = "d15_h24_base"
REACT = {"+1m": MIN_MS, **v0.REACT}
SPEED_H = ("+1m", "+5m", "+15m")
GATE_MDE = 0.05
MIN_K = 20
THRESH = 0.10
BIG_USD = 20e6
Y25 = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
RESOLVE_FILE = "hacks_resolve.json"
MEASURED_FILE = "hacks_measured.json"


def rss_mb() -> float:
    return v0.rss_mb()


def hacks() -> list[Hack]:
    return parse_hacks(CSV.read_text())


def as_event(h: Hack, base: str) -> Event:
    return Event(base, h.ms, "hack", "curated", h.key, precise=True, note=h.time_source)


def primary_keys(hs: list[Hack], resolved: dict[str, Any]) -> set[str]:
    """Registered primary pool: precise time, then the 72 h same-token dedupe (on the priced base ticker)."""
    evs = []
    for h in hs:
        if hack_exclusion(h) is not None:
            continue
        cands = hack_candidates(h)
        base = (resolved.get(h.key) or {}).get("base") or (cands[0] if cands else h.token)
        evs.append(as_event(h, base))
    kept, _ = dedupe(evs)
    return {e.cluster for e in kept}


def blocked_times(cache: Path, hs: list[Hack]) -> dict[str, list[int]]:
    """Other event times per base ticker for the placebo: hack rows, closure rows, every stage-0 event."""
    by: dict[str, list[int]] = defaultdict(list)
    for h in hs:
        for t in hack_candidates(h):
            by[t].append(h.ms)
    for c in parse_closures(CLOSURES_CSV.read_text()):
        by[c.token].append(c.ms)
    p = cache / "events.json"
    if p.exists():
        for lst in v0.rd(p).values():
            for d in lst:
                t = int(d["ms"]) if d.get("precise", True) else int(d["ms"]) + DAY_MS
                by[str(d["token"])].append(t)
    return by


# ================================================================== prices


async def stage_prices(cache: Path, sample: int) -> None:
    _, rt = build_server()
    fx = v0.Fetcher()
    px = v0.Px(cache, rt, fx)
    t_start = time.time()
    try:
        await px.symbols()
        hs = hacks()[:sample] if sample else hacks()
        res_p = cache / RESOLVE_FILE
        resolved: dict[str, Any] = v0.rd(res_p) if res_p.exists() else {}
        print(f"hacks: {len(hs)}; um symbols {len(px.um)}, spot symbols {len(px.spot)}", flush=True)
        # reference: BTC around the EUL alert (2023-03-13), the registered consistency check
        await px.m1("binance", "BTCUSDT", int(datetime(2023, 3, 13, tzinfo=UTC).timestamp() * 1000))
        for i, h in enumerate(hs, 1):
            if hack_exclusion(h) is None and h.key not in resolved:
                r, dead, base = None, [], None
                for cand in hack_candidates(h):
                    r, d = await px.resolve_live(cand, h.ms)
                    dead += d
                    if r:
                        base = cand
                        break
                resolved[h.key] = {"r": r, "dead": dead, "base": base}
                v0.wr(res_p, resolved)
            res = resolved.get(h.key) or {}
            r = res.get("r")
            if r:
                venue, sym, _ = r
                base = res["base"]
                await px.ann_spot("bybit", base, h.ms)  # Bybit spot: second collision check
                d0 = h.ms // DAY_MS * DAY_MS
                jobs = [px.m1(venue, sym, d0 + k * DAY_MS) for k in range(-1, 6)]
                jobs += [px.m1("binance", "BTCUSDT", d0 + k * DAY_MS) for k in range(0, 6)]
                if f"{base}USDT" in px.spot:
                    jobs.append(px.m1("binance_spot", f"{base}USDT", d0))
                for y, m in v0.months_between(h.ms - 122 * DAY_MS, h.ms + 6 * DAY_MS):
                    jobs.append(px.funding(venue, sym, y, m))
                for y, m in v0.months_between(h.ms - 122 * DAY_MS, h.ms - 3 * DAY_MS):
                    jobs.append(px.h1(venue, sym, y, m))
                await asyncio.gather(*jobs)
            el = time.time() - t_start
            print(
                f"  {i}/{len(hs)} {h.token} {v0.ymdhm(h.ms)} perp {r} dead {res.get('dead')} "
                f"({el:.0f}s, eta {el / i * (len(hs) - i):.0f}s); files fetched {fx.fetched}; errors {dict(fx.errors)}; rss={rss_mb():.0f}MB",
                flush=True,
            )
        n_ok = sum(1 for h in hs if (resolved.get(h.key) or {}).get("r"))
        print(
            f"prices done: {n_ok}/{len(hs)} hacks have a live Binance/Bybit perp at t0; {time.time() - t_start:.0f}s rss={rss_mb():.0f}MB"
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
    hs = hacks()
    resolved = v0.rd(cache / RESOLVE_FILE)
    blocked = blocked_times(cache, hs)
    prim = primary_keys(hs, resolved)
    recs: list[dict[str, Any]] = []
    for h in hs:
        res = resolved.get(h.key) or {}
        r = res.get("r")
        base = res.get("base") or h.token
        e = as_event(h, base)
        rec = v0.measure(cache, e, r, blocked[base]) if r else None
        if rec is None:
            rec = {"token": base, "ms": h.ms, "category": "hack", "source": "curated", "cluster": h.key, "p0": None, "no_perp": True}
        else:
            rec["spot_ratio_ann"] = None
            pa = v0.ann_spot_price(cache, "bybit", base, h.ms)
            if pa and rec.get("p0"):
                rec["spot_ratio_ann"] = (rec["p0"] / r[2]) / pa
            b = bars_1m(cache, r[0], r[1], h.ms // DAY_MS * DAY_MS)
            if b is not None and rec.get("react") is not None:
                rec["react"]["+1m"] = reaction(b, h.ms, MIN_MS)
        rec.update(
            label=h.token,
            time_source=h.time_source,
            amount=h.amount_usd,
            perp_csv=list(h.perp_at_t0),
            exclusion=hack_exclusion(h) or ("dedupe 72h" if h.key not in prim else None),
            primary=h.key in prim,
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


def fmt(x: float | None, p: int = 1) -> str:
    return v0.fmt(x, p)


def ci_row(vals: list[float], cl: list[str]) -> str:
    return v0.ci_row(vals, cl)


def gate(vals: list[float], cl: list[str]) -> tuple[float, int, float, bool]:
    sd, k = cluster_sd(vals, cl) if len(vals) > 1 else (float("nan"), 0)
    m = mde_clusters(sd, k)
    return sd, k, m, bool(m <= GATE_MDE and k >= MIN_K)


# ================================================================== stage0 (power gate, no means)


def stage_stage0(cache: Path) -> None:
    recs = measure_all(cache)
    L: list[str] = []
    out = v0.out
    out(L, f"hacks: {len(recs)}; exclusions: {dict(Counter(r['exclusion'] for r in recs if r['exclusion']))}")
    prim = [r for r in recs if r["primary"]]
    out(L, f"primary candidates: {len(prim)}; with a live Binance/Bybit perp at t0: {sum(1 for r in prim if not r.get('no_perp'))}")
    out(L, "no live perp (primary): " + ", ".join(f"{r['label']} {v0.ymd(r['ms'])} dead={r['dead']}" for r in prim if r.get("no_perp")))
    out(L, "\nperp column vs candle check (live perp at t0):")
    for r in recs:
        has_csv, has = bool(r["perp_csv"]), not r.get("no_perp")
        if has_csv != has:
            out(L, f"  {r['label']:10s} {v0.ymdhm(r['ms'])} csv {r['perp_csv']} -> ours {r.get('venue')}:{r.get('sym')} dead {r['dead']}")
    out(L, "\nper-event perp and price level (manual collision review; no outcomes):")
    for r in recs:
        if r.get("no_perp"):
            continue
        sb, sa = r.get("spot_ratio"), r.get("spot_ratio_ann")
        out(
            L,
            f"  {r['label']:10s} {v0.ymdhm(r['ms'])} {r['time_source']:20s} primary={int(r['primary'])} {r.get('venue')}:{r.get('sym')} "
            f"p0 {r.get('p0')} spot/binance {None if sb is None else round(sb, 3)} spot/bybit {None if sa is None else round(sa, 3)} "
            f"check {v0.spot_check(r) if r.get('p0') is not None else '-'} "
            f"funding missing {int(bool(r.get('funding_missing')))} partial {int(bool(r.get('funding_partial')))}",
        )
    btc = v0.cached(cache, "binance", "BTCUSDT", "1m", "2023-03-13")
    if btc:
        t = int(datetime(2023, 3, 13, 8, 59, 10, tzinfo=UTC).timestamp() * 1000)
        out(
            L, f"\nreference: BTCUSDT perp at the EUL alert 2023-03-13 08:59:10 UTC = {price_at(Bars.from_rows(btc), t)} (expected ~21-23k)"
        )
    vals, cl, _ = pool(recs)
    sd, k, m, ok = gate(vals, cl)
    out(L, f"\nprimary pool usable {len(vals)} in {k} clusters; SD(cluster means) {sd:.3f}; MDE {m:.3f}")
    out(L, f"GATE ({PRIMARY}): MDE {m:.3f} vs {GATE_MDE:.2f}, k {k} vs {MIN_K} -> {'PASS' if ok else 'FAIL'}")
    (cache / "hacks_stage0.txt").write_text("\n".join(L) + "\n")
    print(f"rss={rss_mb():.0f}MB")


# ================================================================== report


# Exploratory slices added in the fix round after the critic's review (2026-10-23), i.e. after the results were seen.
# Not part of the registration; the registered verdict is unchanged. Keys: (CSV token, t0 UTC date).
LATE_T0 = {  # t0 is not the first public signal according to the CSV itself
    ("BAL", "2023-08-28"): "disclosed 22 Aug, exploit 27 Aug; t0 = PeckShield summary (second_utc 143.8 h earlier)",
    ("SUSHI", "2023-04-09"): "whitehat tweeted 47 min earlier",
    ("RDNT", "2024-01-02"): "researcher tweeted 78 min earlier",
    ("KNC", "2023-11-22"): "'earlier alerts may exist'",
    ("ONE", "2026-08-12"): "txEcosystem tweet earlier (second_utc 8.3 h earlier)",
    ("KITE", "2026-08-06"): "DefiLlama date one day before t0",
    ("FET", "2026-09-20"): "DefiLlama date one day before t0",
    ("GALA", "2024-05-20"): "'first signal likely earlier'",
    ("RDNT", "2024-10-16"): "'hack likely hours before' the protocol post",
}
DOUBTFUL_LINK = {  # hack -> token link is weak
    ("TRX", "2023-11-10"): "Poloniex exchange hack, not TRON",
    ("ZRX", "2026-01-25"): "'0x token link is a guess' (Matcha frontend)",
    ("KITE", "2026-08-06"): "'contained with no loss'",
    ("SHIB/BONE", "2025-09-12"): "Shibarium bridge; BONE perp unsure, priced 1000SHIB",
}


def _tag(r: dict[str, Any]) -> tuple[str, str]:
    return (r["label"], v0.ymd(r["ms"]))


def _slice_block(L: list[str], recs: list[dict[str, Any]], lab: str, pred: Any) -> None:
    out = v0.out
    v, c, sub = pool(recs, pred=pred)
    out(L, f"[{lab}] H0d d15 h24: {ci_row(v, c)}")
    cells = []
    for h in ("pre60", "+1m", "+5m", "+15m", "+24h", "+72h"):
        xs = [r["pre60"] if h == "pre60" else r["react"].get(h) for r in sub]
        xs = [x for x in xs if x is not None]
        cells.append(f"{h} {fmt(statistics.fmean(xs))}/{fmt(statistics.median(xs))}" if xs else f"{h} —")
    out(L, f"[{lab}] reactions mean/median (n {len(sub)}): " + "; ".join(cells))
    ok = [r for r in sub if r["react"].get("+24h") is not None]
    for h in SPEED_H:
        okh = [r for r in ok if r["react"].get(h) is not None]
        a, z = [r["react"][h] for r in okh], [r["react"]["+24h"] for r in okh]
        s, lo, hi = move_share_bootstrap(a, z)
        med, nm = move_share_median(a, z)
        out(L, f"[{lab}] speed {h}: pooled {s * 100:.0f}% [{lo * 100:.0f}%; {hi * 100:.0f}%]; per-event median {med * 100:.0f}% (n {nm})")


def exploratory(L: list[str], cache: Path, recs: list[dict[str, Any]], rr: list[dict[str, Any]]) -> None:
    out = v0.out
    out(L, "\n## exploratory (added after results in the review fix round; NOT registered)")
    out(L, "late t0 (CSV says t0 is not the first public signal):")
    for r in rr:
        if _tag(r) in LATE_T0:
            out(L, f"  {r['label']} {v0.ymd(r['ms'])}: {LATE_T0[_tag(r)]}")
    out(L, f"  in pool: {sum(1 for r in rr if _tag(r) in LATE_T0)} of {len(LATE_T0)} listed")
    hs = {h.key: h for h in hacks()}
    early2 = [r for r in rr if 0 < hs[r["cluster"]].second_ms < r["ms"]]
    out(
        L,
        "  pool rows with second_utc < t0: "
        + ", ".join(f"{r['label']} {v0.ymd(r['ms'])} ({(r['ms'] - hs[r['cluster']].second_ms) / 3.6e6:.1f} h)" for r in early2),
    )
    out(L, "doubtful hack->token link:")
    for r in rr:
        if _tag(r) in DOUBTFUL_LINK:
            out(L, f"  {r['label']} {v0.ymd(r['ms'])}: {DOUBTFUL_LINK[_tag(r)]}")
    _slice_block(L, recs, "all 43", None)
    _slice_block(L, recs, "without late t0", lambda r: _tag(r) not in LATE_T0)
    _slice_block(L, recs, "without doubtful link", lambda r: _tag(r) not in DOUBTFUL_LINK)
    _slice_block(L, recs, "without late t0 and doubtful link", lambda r: _tag(r) not in LATE_T0 and _tag(r) not in DOUBTFUL_LINK)
    # pre60 concentration
    pre = sorted(((r["pre60"], r["label"]) for r in rr if r.get("pre60") is not None), key=lambda x: x[0])
    xs = [p for p, _ in pre]
    out(
        L,
        f"pre60 distribution (n {len(xs)}): <= -5%: {sum(1 for x in xs if x <= -0.05)}; <= -1%: {sum(1 for x in xs if x <= -0.01)}; > 0: {sum(1 for x in xs if x > 0)}",
    )
    out(L, "pre60 most negative: " + ", ".join(f"{lab} {fmt(p)}" for p, lab in pre[:6]))
    if len(xs) > 4:
        out(L, f"pre60 without the 4 most negative: mean {fmt(statistics.fmean(xs[4:]))} median {fmt(statistics.median(xs[4:]))}")
    # registered '1 - S(15 min)' item
    ok = [r for r in rr if r["react"].get("+15m") is not None and r["react"].get("+24h") is not None]
    s, lo, hi = move_share_bootstrap([r["react"]["+15m"] for r in ok], [r["react"]["+24h"] for r in ok])
    med, nm = move_share_median([r["react"]["+15m"] for r in ok], [r["react"]["+24h"] for r in ok])
    out(
        L,
        f"registered item 1 - S(15m): pooled {(1 - s) * 100:.0f}% [{(1 - hi) * 100:.0f}%; {(1 - lo) * 100:.0f}%]; per-event median {(1 - med) * 100:.0f}% (n {nm})",
    )
    # overlap with stage 0 (DefiLlama-dated hacks)
    p = cache / "events.json"
    if p.exists():
        s0 = [(str(d["token"]), int(d["ms"])) for lst in v0.rd(p).values() for d in lst if d.get("category") == "hack"]
        ov = [r for r in rr if any(t == r["token"] and abs(ms - r["ms"]) <= 2 * DAY_MS for t, ms in s0)]
        out(L, f"overlap with stage-0 hack events (same token, |dt| <= 2 d): {len(ov)} of {len(rr)}")
    # funding rule evidence
    big = [(r["label"], r["trades"][PRIMARY]["funding"]) for r in rr if r["trades"][PRIMARY]["funding"] <= -0.02]
    out(L, f"events where the short paid >= 2% funding in 24 h: {len(big)}: " + ", ".join(f"{a} {fmt(b)}" for a, b in big))


def stage_report(cache: Path) -> None:
    recs = measure_all(cache)
    L: list[str] = []
    out = v0.out
    out(L, f"# news events 0d (hacks, exact time) report {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
    out(L, f"costs: fee {v0.FEE:.4%}/side, slippage {v0.SLIP:.4%}/side (stress x3); stop {v0.STOP:.0%}; placebo {v0.N_PLACEBO}/event")
    vals, cl, rr = pool(recs)
    sd, k, mde, gate_ok = gate(vals, cl)
    out(L, f"gate: MDE {fmt(mde)} on {k} events (min {MIN_K}) -> {'PASS' if gate_ok else 'FAIL'}")
    # ---- coverage
    out(L, "\n## coverage")
    out(
        L,
        f"rows {len(recs)}; primary {sum(r['primary'] for r in recs)}; primary with live perp {sum(1 for r in recs if r['primary'] and not r.get('no_perp'))}",
    )
    out(
        L,
        f"primary usable {len(vals)}; venues {dict(Counter(r['venue'] for r in rr))}; collisions "
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
    out(L, "\n## per event (primary pool, short d15 h24 net; reactions from t0)")
    for r in sorted(rr, key=lambda r: r["ms"]):
        rc = r["react"]
        t = r["trades"][PRIMARY]
        out(
            L,
            f"  {r['label']:10s} {v0.ymdhm(r['ms'])} {r['time_source']:20s} ${r['amount'] / 1e6:.0f}M {r['venue']}:{r['sym']} | "
            + " ".join(f"{h} {fmt(rc.get(h))}" for h in REACT)
            + f" | pre60 {fmt(r.get('pre60'))} | net {fmt(t['net'])} (gross {fmt(t['gross'])}, fund {fmt(t['funding'], 2)}, MAE {fmt(t['mae'])})",
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
    for lab, pr in (("primary", None), ("primary, monitored sources only", lambda r: r["time_source"] in HACK_TIME_MONITORED)):
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
    out(L, "\n## left for an automaton: gross short from t0+h to t0+24h minus 0.41% (no funding; descriptive)")
    for h in ("+1m", "+5m", "+15m"):
        ok = [r for r in rr if r["react"].get(h) is not None and r["react"].get("+24h") is not None]
        xs = [residual_short(r["react"][h], r["react"]["+24h"]) - 2 * v0.COST for r in ok]
        out(L, f"from {h}: {ci_row(xs, [r['cluster'] for r in ok])}")
    # ---- H0d
    out(L, "\n## H0d: short entered t0+15m, exit +24h, base costs + funding (net, on notional)")
    out(L, f"PRIMARY: {ci_row(vals, cl)}")
    pl = [statistics.fmean(r["placebo"]["h24"]) if r.get("placebo") and r["placebo"]["h24"] else None for r in rr]
    idx = [i for i, p in enumerate(pl) if p is not None]
    ev_v, pl_v, cl_v = [vals[i] for i in idx], [pl[i] for i in idx], [cl[i] for i in idx]
    md = lo_d = hi_d = float("nan")
    if len(ev_v) > 1:
        out(L, f"  placebo (same perps, {v0.N_PLACEBO} random hours before t0): {ci_row(pl_v, cl_v)}")  # type: ignore[arg-type]
        md, lo_d, hi_d = paired_diff_bootstrap(ev_v, pl_v, cl_v)  # type: ignore[arg-type]
        out(L, f"  event - placebo: {fmt(md)} [{fmt(lo_d)}; {fmt(hi_d)}] (n {len(ev_v)}; no placebo: {len(vals) - len(ev_v)})")
    m, lo, hi = cluster_bootstrap(vals, cl)
    med = statistics.median(vals)
    v1, c1, _ = pool(recs, pred=lambda r: r["ms"] < Y25)
    v2, c2, _ = pool(recs, pred=lambda r: r["ms"] >= Y25)
    out(L, f"  2023-2024: {ci_row(v1, c1)}")
    out(L, f"  2025-2026: {ci_row(v2, c2)}")
    halves = bool(v1 and v2 and statistics.fmean(v1) > 0 and statistics.fmean(v2) > 0)
    tv, _, tr_ = pool(recs)
    out(L, f"  CI clustered by token: {ci_row(tv, [r['token'] for r in tr_])}")
    kill = hi <= 0 or (not (lo_d > 0) and lo <= 0 <= hi and med <= 0)
    if not gate_ok:
        verdict = "INCONCLUSIVE (power gate failed; means descriptive)"
    elif lo > 0 and lo_d > 0 and halves:
        verdict = "CONFIRMED"
    elif lo > 0 and lo_d > 0:
        verdict = "INCONCLUSIVE (not positive in both time halves)"
    elif kill:
        verdict = "REJECTED"
    else:
        verdict = "INCONCLUSIVE"
    out(L, f"  verdict rule: {verdict}; mean >= {THRESH:.0%} (card H1 form): {m >= THRESH}; median {fmt(med)}; both halves > 0: {halves}")
    out(L, f"  kill criterion (upper CI <= 0, or not better than placebo with CI including 0 and median <= 0): {kill}")
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
    slices = [
        ("monitored sources (no community_tweet)", lambda r: r["time_source"] in HACK_TIME_MONITORED),
        ("community_tweet only", lambda r: r["time_source"] == "community_tweet"),
        (f"amount >= ${BIG_USD / 1e6:.0f}M", lambda r: r["amount"] >= BIG_USD),
        (f"amount < ${BIG_USD / 1e6:.0f}M", lambda r: r["amount"] < BIG_USD),
        ("2023-2024", lambda r: r["ms"] < Y25),
        ("2025-2026", lambda r: r["ms"] >= Y25),
        ("perp on Binance", lambda r: r.get("venue") == "binance"),
        ("perp on Bybit", lambda r: r.get("venue") == "bybit"),
        ("tickers > 2 chars", lambda r: len(r["token"]) > 2),
        ("spot-checked only", lambda r: r.get("p0") is not None and v0.spot_check(r) != "none"),
    ]
    for lab, pr in slices:
        v, c, _ = pool(recs, pred=pr)
        out(L, f"{lab}: {ci_row(v, c)}")
    order = sorted(range(len(vals)), key=lambda i: -vals[i])
    for drop in (1, 3, 5):
        keep = sorted(order[drop:])
        out(L, f"without top {drop}: {ci_row([vals[i] for i in keep], [cl[i] for i in keep])}")
    out(L, "ranked: " + ", ".join(f"{rr[i]['label']} {v0.ymd(rr[i]['ms'])} {fmt(vals[i])}" for i in order))
    exploratory(L, cache, recs, rr)
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
    both = [t for t, lst in g.items() if any(ms < Y25 for ms, _ in lst) and any(ms >= Y25 for ms, _ in lst)]
    out(
        L,
        f"persistence: tokens with events in both 2023-2024 and 2025-2026: {both} -> "
        + ("tested" if len(both) >= 8 else "not testable (< 8)"),
    )
    # ---- descriptive rows outside the primary pool
    out(L, "\n## descriptive (outside the primary pool or no usable perp)")
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
            f"  {r['label']} {v0.ymdhm(r['ms'])} ({r['exclusion']}) {r['venue']}:{r['sym']} check {v0.spot_check(r)}: "
            + " ".join(f"{h} {fmt(rc.get(h))}" for h in REACT)
            + f" | short d15 h24 net {fmt(tr['net'] if tr else None)}",
        )
    (cache / "hacks_report.txt").write_text("\n".join(L) + "\n")
    print(f"\nrss={rss_mb():.0f}MB; written {cache / 'hacks_report.txt'}", flush=True)


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
