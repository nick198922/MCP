"""Polymarket neg-risk sum scanner, step 0 (IDEA-20261015-polymarket-negrisk-sum-scanner).

Manual passes over all active neg-risk events (no collector). The pre-registration is in the idea card
("Попередня реєстрація"). Network only through the runtime provider ``rt.polymarket`` (Gamma, CLOB, data-api);
per-file cache under ``--cache-dir`` (default ``~/.cache/tradedesk-agents/negrisk``). Read-only: no orders.

Usage:
    uv run python scripts/verify_negrisk_step0.py --stage register   # sha256 of the pre-registration (before any book)
    uv run python scripts/verify_negrisk_step0.py --stage probe      # schema only (keys / flags, no neg-risk prices)
    uv run python scripts/verify_negrisk_step0.py --stage pass [--limit N]   # one full pass -> passes/<UTC stamp>/
    uv run python scripts/verify_negrisk_step0.py --stage report     # aggregate all passes, apply the step-0 rule
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import resource
import statistics
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.negrisk import (
    FEE_LOWER,
    FEE_UPPER,
    annualized,
    classify_event,
    is_stale,
    last_trade_ts,
    levels,
    set_edge,
    thin_edge,
    ts_spread_ms,
    utc_segment,
)

DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/negrisk").expanduser()
ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "docs/ideas/IDEA-20261015-polymarket-negrisk-sum-scanner.md"
REG_TXT = ROOT / "docs/verification/2026-10-16-polymarket-negrisk-step0.registration.txt"
KS = (100, 500)
SIDES = ("buy", "sell")
SIMUL_MS = 5000.0
BOOKS_CHUNK = 100
# pass run before amendment 1 (simultaneity by book `timestamp`): exploratory pilot, never in the decision
PILOT_STAMPS = frozenset({"20261016T094353Z"})
# (name, fee mode, buffer per leg); "primary" and "lenient" are pre-registered, the rest are sensitivity variants
VARIANTS = [
    ("primary", FEE_UPPER, 0.005),
    ("lenient", FEE_LOWER, 0.0),
    ("upper_buf0", FEE_UPPER, 0.0),
    ("upper_buf1c", FEE_UPPER, 0.01),
    ("gross", "none", 0.0),
]


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def log(msg: str) -> None:
    print(f"{datetime.now(UTC).strftime('%H:%M:%S')} rss={rss_mb():.0f}MB {msg}", flush=True)


def _append(p: Path, obj: Any) -> None:
    with p.open("a") as fh:
        fh.write(json.dumps(obj) + "\n")


def _write(p: Path, obj: Any) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


# ----------------------------------------------------------------------------- register / probe


def stage_register(cache: Path) -> None:
    """Append sha256 of the card's pre-registration section with a UTC timestamp; keep a copy of the card."""
    text = CARD.read_text()
    sec = text.split("## Попередня реєстрація", 1)[1].split("\n## ", 1)[0]
    h = hashlib.sha256(sec.encode()).hexdigest()
    line = f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={h} bytes={len(sec.encode())}\n"
    with open(cache / "registration.sha256", "a") as fh:
        fh.write(line)
    copy = cache / f"card_at_registration_{h[:12]}.md"
    copy.write_text(text)
    with open(REG_TXT, "a") as fh:
        fh.write(line)
        fh.write(
            "Реєстрація (або поправка) картки IDEA-20261015-polymarket-negrisk-sum-scanner, крок 0; що саме і відносно яких "
            f"проходів — див. розділ «Попередня реєстрація» картки. Копія картки: ~/.cache/tradedesk-agents/negrisk/{copy.name}.\n"
        )
    print(line, end="")


async def stage_probe(cache: Path) -> None:
    """Schema only: keys that mark neg-risk / fees on Gamma events and markets, and whether POST /books works."""
    from tradedesk.server import build_server

    _, rt = build_server()
    pm = rt.polymarket
    try:
        f = await pm.gamma.get_json("/events/keyset", params={"active": "true", "closed": "false", "limit": 20}, ttl=0)
        evs = f.data["events"]
        (cache / "probe").mkdir(parents=True, exist_ok=True)
        _write(cache / "probe" / "events_keyset_sample.json", f.data)
        ev = next((e for e in evs if len(e.get("markets") or []) > 2), evs[0])
        print("event keys:", sorted(ev.keys()))
        for e in evs:
            print(" ", e.get("id"), {k: e.get(k) for k in e if "risk" in k.lower()}, "n_markets", len(e.get("markets") or []))
        m = ev["markets"][0]
        print("market keys:", sorted(m.keys()))
        print("market risk keys:", {k: m.get(k) for k in m if "risk" in k.lower()})
        print("market fee keys:", {k: m.get(k) for k in m if "fee" in k.lower()})
        toks = [json.loads(x["clobTokenIds"])[0] for x in ev["markets"][:3] if x.get("clobTokenIds")]
        try:
            b = await pm.books(toks)
            print("POST /books ->", type(b.data).__name__, len(b.data), "for", len(toks), "tokens")
            print("book keys:", sorted(b.data[0].keys()))
            print("book non-price fields:", {k: v for k, v in b.data[0].items() if k not in ("bids", "asks")})
            _write(cache / "probe" / "books_sample.json", b.data)
        except Exception as e:  # noqa: BLE001
            print("POST /books ERR", type(e).__name__, str(e)[:300])
        try:
            fr = await pm.clob.get_json("/fee-rate", params={"token_id": toks[0]}, ttl=0)
            print("GET /fee-rate ->", fr.data)
        except Exception as e:  # noqa: BLE001
            print("GET /fee-rate ERR", type(e).__name__, str(e)[:300])
    finally:
        await rt.aclose()


async def stage_diag(cache: Path) -> None:
    """Measurement diagnostics on the latest pass (no edges printed): (1) why /books misses legs (Gamma flags of the
    missing markets; does single GET /book return them?), (2) what the book `timestamp` means (re-read the same books
    after a pause: unchanged books keeping an old timestamp => last-change time, not read time)."""
    from tradedesk.server import build_server

    pdir = sorted((cache / "passes").glob("*/results.jsonl"))[-1].parent
    meta = json.loads((pdir / "events_negrisk.json").read_text())
    have: set[str] = set()
    for bf in (pdir / "books").glob("*.json"):
        if not bf.name.endswith(".recheck.json"):
            have |= {str(b.get("asset_id")) for b in json.loads(bf.read_text())["books"]}
    flags_missing: Counter = Counter()
    flags_present: Counter = Counter()
    missing: list[str] = []
    for ev in meta["rows"]:
        for m in ev.get("markets") or []:
            if m.get("closed"):
                continue
            toks = json.loads(m.get("clobTokenIds") or "[]")
            if not toks:
                continue
            key = (
                f"acceptingOrders={m.get('acceptingOrders')} enableOrderBook={m.get('enableOrderBook')} active={m.get('active')} "
                f"other={m.get('negRiskOther')}"
            )
            if str(toks[0]) in have:
                flags_present[key] += 1
            else:
                flags_missing[key] += 1
                missing.append(str(toks[0]))
    print(f"pass {pdir.name}: legs present={sum(flags_present.values())} missing={sum(flags_missing.values())}")
    print("missing legs by Gamma flags:", dict(flags_missing.most_common(8)))
    print("present legs by Gamma flags:", dict(flags_present.most_common(8)))
    _, rt = build_server()
    pm = rt.polymarket
    try:
        for tok in missing[:5]:
            try:
                b = await pm.clob.get_json("/book", params={"token_id": tok}, ttl=0)
                print(f"  single GET /book {tok[:12]}…: ok, asks={len(b.data.get('asks') or [])} bids={len(b.data.get('bids') or [])}")
            except Exception as e:  # noqa: BLE001
                print(f"  single GET /book {tok[:12]}…: {type(e).__name__} {str(e)[:120]}")
        # does /books drop tokens in larger batches? re-request missing ones alone and in a batch of 5
        if missing:
            f1 = await pm.books(missing[:5])
            print(f"  POST /books with 5 missing tokens -> {len(f1.data)} books")
        # timestamp semantics: first 40 present tokens, two reads 20 s apart
        toks = sorted(have)[:40]
        a, t_a0, t_a1 = await fetch_books(pm, toks)
        await asyncio.sleep(20)
        b2, t_b0, t_b1 = await fetch_books(pm, toks)
        same_book_same_ts = same_book_new_ts = changed = 0
        ages = []
        for tok in toks:
            x, y = a.get(tok), b2.get(tok)
            if x is None or y is None:
                continue
            ages.append(t_a1 - float(x["timestamp"]) / 1000.0)
            same = x.get("asks") == y.get("asks") and x.get("bids") == y.get("bids")
            if not same:
                changed += 1
            elif x.get("timestamp") == y.get("timestamp"):
                same_book_same_ts += 1
            else:
                same_book_new_ts += 1
        print(
            f"  re-read after 20 s: unchanged book & same timestamp={same_book_same_ts}, unchanged book & new timestamp={same_book_new_ts}, "
            f"changed={changed}; request spans {t_a1 - t_a0:.2f}s / {t_b1 - t_b0:.2f}s"
        )
        if ages:
            print(f"  age of timestamp at read (s): min={min(ages):.1f} median={statistics.median(ages):.1f} max={max(ages):.0f}")
    finally:
        await rt.aclose()


def stage_show(cache: Path, ids: list[str]) -> None:
    """Print rules text (untrusted, for manual classification only), leg titles, fee schedules and per-leg top of book
    for the given event ids from the latest pass that has them."""
    for pdir in sorted((cache / "passes").glob("*"), reverse=True):
        meta_p = pdir / "events_negrisk.json"
        if not meta_p.exists():
            continue
        meta = json.loads(meta_p.read_text())
        found = [e for e in meta["rows"] if str(e.get("id")) in ids]
        if not found:
            continue
        for ev in found:
            print(f"\n### {ev['id']} {ev.get('slug')} (pass {pdir.name}) end={ev.get('endDate')} augmented={ev.get('negRiskAugmented')}")
            print("rules:", " ".join(str(ev.get("description") or "").split())[:1200])
            bf = pdir / "books" / f"{ev['id']}.recheck.json"
            bk = {b["asset_id"]: b for b in json.loads(bf.read_text())["books"]} if bf.exists() else {}
            for m in ev.get("markets") or []:
                tok = json.loads(m.get("clobTokenIds") or "[]")
                b = bk.get(tok[0]) if tok else None
                a = min(levels(b.get("asks")))[0] if b and levels(b.get("asks")) else None
                bb = max(levels(b.get("bids")))[0] if b and levels(b.get("bids")) else None
                print(
                    f"  - {m.get('groupItemTitle')!r} closed={m.get('closed')} active={m.get('active')} fees={m.get('feesEnabled')} "
                    f"{m.get('feeType')} {m.get('feeSchedule')} ask={a} bid={bb}"
                )
        ids = [i for i in ids if i not in {str(e.get("id")) for e in found}]
        if not ids:
            return


def stage_fixture(cache: Path, event_id: str) -> None:
    """Copy one event's cached recheck books (real POST /books response) into tests/fixtures with source and date."""
    for pdir in sorted((cache / "passes").glob("*"), reverse=True):
        bf = pdir / "books" / f"{event_id}.recheck.json"
        if bf.exists():
            d = json.loads(bf.read_text())
            out = {
                "_source": f"clob.polymarket.com POST /books, all YES legs of neg-risk event {event_id}, "
                f"pass {pdir.name}, read at unix {d['t_req'][0]:.0f}; fetched by scripts/verify_negrisk_step0.py",
                "books": d["books"],
            }
            dst = ROOT / "tests/fixtures/polymarket_books_negrisk_2026-10-16.json"
            dst.write_text(json.dumps(out, indent=1))
            print(f"wrote {dst} ({len(d['books'])} books)")
            return
    print("not found")


# ----------------------------------------------------------------------------- one pass


async def fetch_books(pm: Any, tokens: list[str]) -> tuple[dict[str, dict[str, Any]], float, float]:
    """All books for `tokens` via POST /books in chunks; returns (books by asset_id, t_start, t_end) in unix s."""
    t0 = time.time()
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(tokens), BOOKS_CHUNK):
        f = await pm.books(tokens[i : i + BOOKS_CHUNK])
        for b in f.data:
            if isinstance(b, dict) and b.get("asset_id") is not None:
                out[str(b["asset_id"])] = b
    return out, t0, time.time()


def evaluate(ev: dict[str, Any], books: dict[str, dict[str, Any]], t_req: tuple[float, float]) -> dict[str, Any]:
    """Edges of one snapshot for all sides, depths and variants.

    Amendment 1: `simultaneous` = all legs read by batch POST /books within SIMUL_MS of wall time (`t_req` span);
    the spread of the books' `timestamp` (last-change time) is kept as `spread_ms` for reference only."""
    legs = ev["legs"]
    got = [books.get(leg["token"]) for leg in legs]
    res: dict[str, Any] = {"n_legs": len(legs), "n_books": sum(b is not None for b in got)}
    if any(b is None for b in got):
        res["coverage"] = False
        return res
    res["coverage"] = True
    bl = [b for b in got if b is not None]
    res["spread_ms"] = ts_spread_ms(bl)
    res["read_ms"] = (t_req[1] - t_req[0]) * 1000.0
    res["simultaneous"] = res["read_ms"] <= SIMUL_MS
    res["max_age_s"] = t_req[1] - min(float(b.get("timestamp") or 0) for b in bl) / 1000.0
    res["book_ts_ms"] = max(float(b.get("timestamp") or 0) for b in bl)
    res["book_negrisk"] = sum(b.get("neg_risk") is True for b in bl)
    asks = [levels(b.get("asks")) for b in bl]
    bids = [levels(b.get("bids")) for b in bl]
    res["top_ask_sum"] = sum(min(a)[0] for a in asks) if all(asks) else None
    res["top_bid_sum"] = sum(max(b)[0] for b in bids) if all(bids) else None
    edges: dict[str, Any] = {}
    for var, mode, buf in VARIANTS:
        fees = [(0.0, 1.0) if mode == "none" else (leg["rate"], leg["exp"]) for leg in legs]
        fm = FEE_UPPER if mode == "none" else mode
        for k in KS:
            for side in SIDES:
                r = set_edge(bl, fees, k, side, fee_mode=fm, buffer_per_leg=buf)
                key = f"{var}|{k}|{side}"
                if not r["feasible"]:
                    edges[key] = None
                    continue
                d = ev.get("days_to_end")
                edges[key] = {
                    "s": round(r["s"], 6),
                    "gross": round(r["gross"], 6),
                    "fee": round(r["fee"], 6),
                    "net": round(r["net"], 6),
                    "capital": round(r["capital"], 6),
                    "ann": annualized(r["net"], r["capital"], d) if d is not None else None,
                    "thin": thin_edge(r),
                    "min_leg": round(min(r["avgs"]), 6),
                }
    res["edges"] = edges
    return res


def positive_sides(res: dict[str, Any], var: str, k: int = 100) -> list[str]:
    if not res.get("coverage") or not res.get("simultaneous"):
        return []
    out = []
    for side in SIDES:
        e = (res.get("edges") or {}).get(f"{var}|{k}|{side}")
        if e is not None and e["net"] > 0:
            out.append(side)
    return out


async def stage_pass(cache: Path, limit: int | None, not_before: str | None = None) -> None:
    from tradedesk.server import build_server

    if not_before:
        hh, mm = (int(x) for x in not_before.split(":"))
        now_dt = datetime.now(UTC)
        start = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        wait = (start - now_dt).total_seconds()
        if wait > 0:
            log(f"waiting {wait / 60:.0f} min until {not_before} UTC")
            await asyncio.sleep(wait)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    pdir = cache / ("samples" if limit else "passes") / stamp  # timing samples never count as passes
    (pdir / "books").mkdir(parents=True, exist_ok=True)
    _, rt = build_server()
    pm = rt.polymarket
    try:
        t = time.perf_counter()
        now = time.time()
        why: Counter = Counter()
        kept: list[dict[str, Any]] = []
        raw_kept: list[dict[str, Any]] = []
        n_rows = n_pages = 0
        complete = False
        async for page, last in pm.iter_events({"active": "true", "closed": "false"}):
            n_pages += 1
            for ev in page:
                if not isinstance(ev, dict):
                    continue
                n_rows += 1
                c = classify_event(ev, now)
                why[c["status"]] += 1
                if c["status"] == "ok":
                    c["title"] = ev.get("title")  # untrusted text: stored for the report, never acted on
                    c["end"] = ev.get("endDate")
                    c["gamma_best"] = [
                        (m.get("bestAsk"), m.get("bestBid")) for m in ev.get("markets") or [] if isinstance(m, dict) and not m.get("closed")
                    ]
                    kept.append(c)
                    raw_kept.append(ev)
            complete = last
            if n_pages % 50 == 0:
                log(f"  listing page {n_pages}, events {n_rows}, kept {len(kept)}")
        log(f"listing: active events={n_rows} pages={n_pages} complete={complete} {time.perf_counter() - t:.0f}s")
        _write(pdir / "events_negrisk.json", {"listed_at": now, "complete": complete, "status": dict(why), "rows": raw_kept})
        del raw_kept
        log(f"status: {dict(why)}")
        log(f"kept: {len(kept)} groups={dict(Counter(c['group'] for c in kept))} cats={dict(Counter(c['category'] for c in kept))}")
        log(f"fuzzy flags: {dict(Counter(','.join(c['flags']) for c in kept if c['flags']))}")
        log(f"full by category: {dict(Counter(c['category'] for c in kept if c['group'] == 'full'))}")
        if limit:
            kept = kept[:limit]
        res_path = pdir / "results.jsonl"
        res_path.write_text("")
        n_pos = n_conf = 0
        t1 = time.perf_counter()
        for i, ev in enumerate(kept, 1):
            tokens = [leg["token"] for leg in ev["legs"]]
            try:
                books, ta, tb = await fetch_books(pm, tokens)
            except Exception as e:  # noqa: BLE001
                _append(res_path, {"id": ev["id"], "error": f"{type(e).__name__}: {str(e)[:200]}"})
                continue
            _write(pdir / "books" / f"{ev['id']}.json", {"t_req": [ta, tb], "books": list(books.values())})
            res = evaluate(ev, books, (ta, tb))
            row = {k: ev.get(k) for k in ("id", "slug", "title", "category", "group", "flags", "days_to_end", "volume", "end")}
            row |= {"t_req": [ta, tb], "first": res, "gamma_best": ev["gamma_best"]}
            pos = positive_sides(res, "lenient")
            if pos:
                n_pos += 1
                books2, ta2, tb2 = await fetch_books(pm, tokens)
                _write(pdir / "books" / f"{ev['id']}.recheck.json", {"t_req": [ta2, tb2], "books": list(books2.values())})
                res2 = evaluate(ev, books2, (ta2, tb2))
                conf = [s for s in positive_sides(res2, "lenient") if s in pos]
                row |= {"recheck": res2, "recheck_t": [ta2, tb2], "confirmed_sides": conf}
                if conf:
                    n_conf += 1
                    fresh: list[Any] = []
                    for leg in ev["legs"]:
                        try:
                            tr = await pm.trades(str(leg["condition_id"]), limit=5, end=int(ta2))
                            lt = last_trade_ts(tr.data, ta2)
                        except Exception as e:  # noqa: BLE001
                            log(f"  trades error {ev['id']}: {type(e).__name__}")
                            lt = None
                        fresh.append(lt)
                    row["leg_last_trade"] = fresh
                    row["stale_legs"] = sum(is_stale(x, ta2) for x in fresh)
            _append(res_path, row)
            if i % 25 == 0 or i == len(kept):
                el = time.perf_counter() - t1
                log(
                    f"  {i}/{len(kept)} events, lenient-positive={n_pos}, confirmed={n_conf}, {el:.0f}s (eta {el / i * (len(kept) - i):.0f}s)"
                )
    finally:
        await rt.aclose()
    log(f"pass {stamp} done")


# ----------------------------------------------------------------------------- report


def _q(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))]


def pilot_summary(cache: Path) -> None:
    """Pilot pass (before amendment 1): exploratory only. Snapshots simultaneous by the amended rule (read span) were
    not re-queried there, so nothing in it is 'confirmed'."""
    for p in sorted((cache / "passes").glob("*/results.jsonl")):
        if p.parent.name not in PILOT_STAMPS:
            continue
        rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        rows = [r for r in rows if "error" not in r and r["first"].get("coverage")]
        for r in rows:
            ta, tb = r["t_req"]
            r["first"]["simultaneous"] = (tb - ta) * 1000.0 <= SIMUL_MS
        print(f"\n== PILOT {p.parent.name} (exploratory, not in the decision): covered events={len(rows)}")
        for grp in ("full", "fuzzy"):
            g = [r for r in rows if r["group"] == grp]
            for var in ("lenient", "primary", "gross"):
                pos = [r for r in g if positive_sides(r["first"], var)]
                print(f"   {grp}: unconfirmed first-snapshot net>0 [{var} K=100] events={len(pos)}/{len(g)}")


def stage_report(cache: Path) -> None:
    pilot_summary(cache)
    passes = sorted(p for p in (cache / "passes").glob("*/results.jsonl") if p.parent.name not in PILOT_STAMPS)
    print(f"passes: {len(passes)}")
    segs: set[int] = set()
    stamps: list[float] = []
    total_snap_full = 0
    ev_any: dict[str, set[str]] = defaultdict(set)  # event -> labels across passes
    confirmed_rows: list[dict[str, Any]] = []
    for p in passes:
        rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        meta = json.loads((p.parent / "events_negrisk.json").read_text())
        ts0 = meta["listed_at"]
        stamps.append(ts0)
        segs.add(utc_segment(ts0))
        err = sum("error" in r for r in rows)
        rows = [r for r in rows if "error" not in r]
        cov = [r for r in rows if r["first"].get("coverage")]
        sim = [r for r in cov if r["first"].get("simultaneous")]
        legs_total = sum(r["first"]["n_legs"] for r in rows)
        legs_book = sum(r["first"]["n_books"] for r in rows)
        print(f"\n== pass {p.parent.name}  (UTC segment {utc_segment(ts0)}), listing complete={meta['complete']}")
        print(f"   gamma status: {meta['status']}")
        print(f"   events kept={len(rows) + err} errors={err} full coverage={len(cov)} simultaneous(<=5s)={len(sim)}")
        print(f"   leg coverage {legs_book}/{legs_total} = {legs_book / max(1, legs_total):.3f}")
        for grp in ("full", "fuzzy"):
            g_all = [r for r in rows if r["group"] == grp]
            g_cov = [r for r in g_all if r["first"].get("coverage")]
            lt = sum(r["first"]["n_legs"] for r in g_all)
            lb = sum(r["first"]["n_books"] for r in g_all)
            print(f"   group {grp}: events={len(g_all)} full coverage={len(g_cov)} legs with book {lb}/{lt} = {lb / max(1, lt):.3f}")
        spreads = [r["first"]["spread_ms"] for r in cov if r["first"].get("spread_ms") is not None]
        if spreads:
            print(
                f"   book timestamp spread ms (last-change, reference): median={statistics.median(spreads):.0f} p90={_q(spreads, 0.9):.0f}"
            )
        reads = [r["first"]["read_ms"] for r in cov if r["first"].get("read_ms") is not None]
        if reads:
            print(f"   batch read wall time ms: median={statistics.median(reads):.0f} max={max(reads):.0f}")
        nrk = sum(r["first"].get("book_negrisk", 0) for r in cov)
        print(f"   books with neg_risk=true: {nrk}/{sum(r['first']['n_legs'] for r in cov)}")
        for grp in ("full", "fuzzy"):
            g = [r for r in sim if r["group"] == grp]
            if grp == "full":
                total_snap_full += len(g)
            cats = Counter(r["category"] for r in g)
            print(f"   group {grp}: simultaneous snapshots={len(g)} by category={dict(cats)}")
            for k in (100, 500):
                for side in SIDES:
                    vals = [r["first"]["edges"].get(f"gross|{k}|{side}") for r in g]
                    feas = [v["s"] for v in vals if v]
                    if feas:
                        print(
                            f"     S_{side}({k}) feasible={len(feas)}/{len(g)} p05={_q(feas, 0.05):.4f} median={statistics.median(feas):.4f} "
                            f"p95={_q(feas, 0.95):.4f}"
                        )
        # sanity: book top sums vs Gamma bestAsk/bestBid sums
        diffs = []
        for r in sim:
            ga = [_fl(a) for a, _ in r["gamma_best"]]
            if r["first"].get("top_ask_sum") is not None and ga and all(x is not None for x in ga):
                diffs.append(r["first"]["top_ask_sum"] - sum(ga))  # type: ignore[arg-type]
        if diffs:
            print(
                f"   sanity top-ask sum (books) - sum bestAsk (Gamma): median={statistics.median(diffs):+.4f} "
                f"p05={_q(diffs, 0.05):+.4f} p95={_q(diffs, 0.95):+.4f} n={len(diffs)}"
            )
        for var, _, _ in VARIANTS:
            for k in (100, 500):
                pos = [r for r in sim if positive_sides(r["first"], var, k)]
                print(
                    f"   first-snapshot net>0 [{var} K={k}]: full={sum(r['group'] == 'full' for r in pos)} "
                    f"fuzzy={sum(r['group'] == 'fuzzy' for r in pos)}"
                )
        for r in sim:
            label = r["group"]
            if positive_sides(r["first"], "lenient"):
                ev_any[r["id"]].add(f"{label}:first_lenient")
            if r.get("confirmed_sides"):
                ev_any[r["id"]].add(f"{label}:confirmed_lenient")
                rc = r["recheck"]
                prim = [s for s in r["confirmed_sides"] if (rc["edges"].get(f"primary|100|{s}") or {}).get("net", -1) > 0]
                if prim:
                    ev_any[r["id"]].add(f"{label}:confirmed_primary")
                stale = r.get("stale_legs", 0) > 0
                confirmed_rows.append(r | {"_pass": p.parent.name, "_primary": prim, "_stale": stale})
    print("\n== across passes")
    print(
        f"UTC segments covered: {sorted(segs)}; pass gaps h: {[round((b - a) / 3600, 2) for a, b in zip(stamps, stamps[1:], strict=False)]}"
    )
    print(f"simultaneous full-group event-snapshots: {total_snap_full}")
    n_ev = {"full": set(), "fuzzy": set()}
    for p in passes:
        for x in p.read_text().splitlines():
            r = json.loads(x)
            if "error" not in r:
                n_ev[r["group"]].add(r["id"])
    print(f"distinct events: full={len(n_ev['full'])} fuzzy={len(n_ev['fuzzy'])}")
    lab = Counter(lbl for s in ev_any.values() for lbl in s)
    print(f"events by label (distinct events): {dict(lab)}")
    print(f"\nconfirmed deviations (rows): {len(confirmed_rows)}")
    for r in confirmed_rows:
        rc = r["recheck"]
        for s in r["confirmed_sides"]:
            e = {v: (rc["edges"].get(f"{v}|100|{s}") or {}).get("net") for v, _, _ in VARIANTS}
            e5 = {v: (rc["edges"].get(f"{v}|500|{s}") or {}).get("net") for v, _, _ in VARIANTS}
            g = rc["edges"].get(f"gross|100|{s}") or {}
            print(
                f"  {r['_pass']} id={r['id']} [{r['category']}/{r['group']} {','.join(r['flags'] or [])}] side={s} legs={rc['n_legs']} "
                f"S={g.get('s')} net100={e} net500={e5} thin={g.get('thin')} min_leg={g.get('min_leg')} "
                f"stale_legs={r.get('stale_legs')} days={_r(r.get('days_to_end'))} ann_primary={_r((rc['edges'].get(f'primary|100|{s}') or {}).get('ann'))} "
                f"slug={r['slug']}"
            )
    # exploratory (added after pass A, not registered): lower-bound fee + 0.005/leg buffer, from the cached recheck books
    print("\nexploratory variant 'lower fee + 0.005/leg' on confirmed rows (recomputed from cached recheck books):")
    expl_ids: set[str] = set()
    for r in confirmed_rows:
        pdir = cache / "passes" / r["_pass"]
        evs = {str(e["id"]): e for e in json.loads((pdir / "events_negrisk.json").read_text())["rows"]}
        ev = evs.get(r["id"])
        bf = pdir / "books" / f"{r['id']}.recheck.json"
        if ev is None or not bf.exists():
            continue
        c = classify_event(ev, r["recheck_t"][0])
        bk = {b["asset_id"]: b for b in json.loads(bf.read_text())["books"]}
        bl = [bk[leg["token"]] for leg in c["legs"]]
        fees = [(leg["rate"], leg["exp"]) for leg in c["legs"]]
        for s in r["confirmed_sides"]:
            for k in (100, 500):
                e = set_edge(bl, fees, k, s, fee_mode=FEE_LOWER, buffer_per_leg=0.005)
                if not e["feasible"]:
                    print(f"  {r['_pass']} id={r['id']} {s} K={k}: infeasible")
                    continue
                ann = annualized(e["net"], e["capital"], r["days_to_end"]) if r.get("days_to_end") else None
                print(
                    f"  {r['_pass']} id={r['id']} [{r['group']}] {s} K={k}: net={e['net']:+.4f} per set, on capital {e['net'] / e['capital']:+.4f}, "
                    f"ann={_r(ann)} stale_legs={r.get('stale_legs')}"
                )
                if k == 100 and e["net"] > 0 and r["group"] == "full" and not r["_stale"]:
                    expl_ids.add(r["id"])
    print(f"  full-group fresh events with net>0 at K=100 under this variant: {len(expl_ids)} {sorted(expl_ids)}")
    full_fresh_primary = {r["id"] for r in confirmed_rows if r["group"] == "full" and r["_primary"] and not r["_stale"]}
    full_fresh_lenient = {r["id"] for r in confirmed_rows if r["group"] == "full" and not r["_stale"]}
    full_fresh_primary_nosport = {
        r["id"] for r in confirmed_rows if r["group"] == "full" and r["_primary"] and not r["_stale"] and r["category"] != "sports"
    }
    print(
        f"\nfull-group, fresh, confirmed: lenient events={len(full_fresh_lenient)} primary events={len(full_fresh_primary)} "
        f"(without sports: {len(full_fresh_primary_nosport)})"
    )
    passes_ok = (
        len(passes) >= 3
        and total_snap_full >= 300
        and len(segs) >= 2
        and all((b - a) >= 2 * 3600 for a, b in zip(stamps, stamps[1:], strict=False))
    )
    if full_fresh_primary:
        verdict = "PROCEED to collector (>=1 confirmed fresh full-group deviation with primary net > 0 at K=100)"
    elif not passes_ok:
        verdict = "CONTINUE: pass requirements not met yet (>=3 passes, >=2h apart, >=2 UTC segments, >=300 snapshots)"
    elif full_fresh_lenient:
        verdict = "INCONCLUSIVE: confirmed only under the lenient fee bound; check the fee formula first"
    else:
        verdict = "REJECTED: no confirmed fresh full-group deviation even under the lenient net at K=100"
    print(f"passes_ok={passes_ok}\nSTEP-0 DECISION: {verdict}")


def _fl(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _r(x: Any) -> Any:
    return round(x, 3) if isinstance(x, float) else x


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["register", "probe", "diag", "pass", "report", "show", "fixture"])
    ap.add_argument("--ids", default="", help="show: comma-separated event ids")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--limit", type=int, default=None, help="pass: only the first N kept events (timing sample)")
    ap.add_argument("--not-before", default=None, help="pass: start no earlier than HH:MM UTC today")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "register":
        stage_register(cache)
    elif a.stage == "probe":
        asyncio.run(stage_probe(cache))
    elif a.stage == "fixture":
        stage_fixture(cache, a.ids)
    elif a.stage == "show":
        stage_show(cache, [x for x in a.ids.split(",") if x])
    elif a.stage == "diag":
        asyncio.run(stage_diag(cache))
    elif a.stage == "pass":
        asyncio.run(stage_pass(cache, a.limit, a.not_before))
    else:
        stage_report(cache)
    log("done")


if __name__ == "__main__":
    main()
