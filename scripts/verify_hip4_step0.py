"""HIP-4 step 0 probe (card IDEA-20261029-hip4-btc-daily-binary-vs-deribit): availability inventory only.

No hypothesis numbers (no Deribit gaps, Brier or returns). Public ``POST /info`` through ``rt.hyperliquid`` (core Http),
no keys. Phases, in order (each reads the cache the previous ones wrote under ``--cache-dir``):
  meta       outcomeMeta + primary docs (.md pages of the gitbook)
  probe      current BTC daily: l2Book, recentTrades, candleSnapshot 1m/1h/1d, spotMeta(+AssetCtxs), guessed types
  users      userFillsByTime of the accounts in the current contract's recentTrades (past outcome coins)
  blocks     candleSnapshot 1h for the past outcomes seen in fills (settled ones answer HTTP 500)
  errors     the error per settled id (c1h_<id>.err.json) + control requests for a non-existent coin
  fees       observed fee / notional on outcome fills of those accounts, by direction (min / p50 / p90 / max)
  inventory  compact summary; full JSON to --out
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import time
from pathlib import Path
from typing import Any

CACHE = Path.home() / ".cache" / "tradedesk-agents" / "hip4-step0"
DOCS = {
    "hip4": "/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-4-outcome-markets",
    "asset_ids": "/hyperliquid-docs/for-developers/api/asset-ids",
    "fees": "/hyperliquid-docs/trading/fees",
    "contract_specs": "/hyperliquid-docs/trading/contract-specifications",
    "info_endpoint": "/hyperliquid-docs/for-developers/api/info-endpoint",
}


def phase_peek(cache: Path) -> None:
    m = json.loads((cache / "outcomeMeta.json").read_text())
    print("feeScale", m.get("feeScale"), "deployers", json.dumps(m.get("deployers"))[:600])
    outs = m["outcomes"]
    ids = [o["outcome"] for o in outs]
    print("n outcomes", len(outs), "id range", min(ids), max(ids))
    seen: dict[str, int] = {}
    for o in outs:
        key = o["description"].split("|")[0][:40]
        seen[key] = seen.get(key, 0) + 1
    print("description prefixes:", json.dumps(seen)[:800])
    for o in outs[:2] + outs[-2:]:
        print(json.dumps(o)[:700])
    for q in m["questions"][:2]:
        print(json.dumps(q)[:700])
    for o in outs:
        if "venue" not in o or not o["name"].startswith("template"):
            print("NOVENUE/NONTEMPLATE", json.dumps(o)[:400])


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def log(msg: str, t0: float) -> None:
    print(f"[{time.time() - t0:6.1f}s rss={rss_mb():.0f}MB] {msg}", flush=True)


def shape(x: Any, depth: int = 0) -> str:
    if depth > 3:
        return "..."
    if isinstance(x, dict):
        return "{" + ", ".join(f"{k}: {shape(v, depth + 1)}" for k, v in list(x.items())[:12]) + "}"
    if isinstance(x, list):
        return f"[{len(x)} x {shape(x[0], depth + 1) if x else '-'}]"
    return type(x).__name__


async def phase_meta(cache: Path, t0: float) -> None:
    from tradedesk.core.http import Http
    from tradedesk.server import build_server

    _, rt = build_server()
    try:
        f = await rt.hyperliquid._info({"type": "outcomeMeta"}, ttl=0)
        (cache / "outcomeMeta.json").write_text(json.dumps(f.data))
        log(f"outcomeMeta shape: {shape(f.data)}", t0)
        docs = Http("gitbook", "https://hyperliquid.gitbook.io", rate=1, burst=1, timeout=20)
        try:
            for name, path in DOCS.items():
                for suffix in (".md", ""):
                    try:
                        b = await docs.get_bytes(path + suffix)
                    except Exception as e:  # noqa: BLE001 - inventory: record and go on
                        log(f"docs {name}{suffix}: {e!r}"[:200], t0)
                        continue
                    (cache / f"doc_{name}{suffix or '.html'}").write_bytes(b.data)
                    log(f"docs {name}{suffix}: {len(b.data)} bytes", t0)
        finally:
            await docs.aclose()
    finally:
        await rt.aclose()


async def _try(rt: Any, payload: dict[str, Any], cache: Path, name: str, t0: float) -> Any:
    """One info request; the raw answer (or the error) goes to ``<cache>/<name>.json``."""
    try:
        f = await rt.hyperliquid._info(payload, ttl=0)
        data: Any = f.data
        err = None
    except Exception as e:  # noqa: BLE001 - inventory: record the failure and go on
        data, err = None, repr(e)[:300]
    (cache / f"{name}.json").write_text(json.dumps({"payload": payload, "error": err, "data": data}))
    n = len(data) if isinstance(data, list | dict) else None
    log(f"{name}: {'ERR ' + err if err else f'ok len={n}'}", t0)
    return data


async def phase_probe(cache: Path, t0: float) -> None:
    from tradedesk.analytics.hip4 import book_summary, recurring_series
    from tradedesk.server import build_server

    meta = json.loads((cache / "outcomeMeta.json").read_text())
    rec = recurring_series(meta)
    for r in rec:
        print("recurring", json.dumps(r))
    btc = [r for r in rec if r["underlying"] == "BTC" and r["period"] == "1d" and r["cls"] == "priceBinary"]
    _, rt = build_server()
    now = int(time.time() * 1000)
    try:
        for r in btc:
            for coin in (r["coin_yes"], r["coin_no"]):
                tag = coin.lstrip("#")
                b = await _try(rt, {"type": "l2Book", "coin": coin}, cache, f"l2Book_{tag}", t0)
                if isinstance(b, dict):
                    print("book", coin, json.dumps(book_summary(b)))
                tr = await _try(rt, {"type": "recentTrades", "coin": coin}, cache, f"recentTrades_{tag}", t0)
                if isinstance(tr, list) and tr:
                    ts = [x.get("time", 0) for x in tr]
                    ntl = sum(float(x["px"]) * float(x["sz"]) for x in tr)
                    print(f"trades {coin}: n={len(tr)} span_h={(max(ts) - min(ts)) / 3.6e6:.2f} ntl={ntl:.0f}")
                for iv, back_ms in (("1m", 86_400_000), ("1h", 7 * 86_400_000), ("1d", now - 1_777_593_600_000)):
                    req = {"coin": coin, "interval": iv, "startTime": now - back_ms, "endTime": now}
                    c = await _try(rt, {"type": "candleSnapshot", "req": req}, cache, f"candles_{tag}_{iv}", t0)
                    if isinstance(c, list) and c:
                        print(f"candles {coin} {iv}: n={len(c)} first={c[0]} last_t={c[-1].get('t')}")
        sm = await _try(rt, {"type": "spotMeta"}, cache, "spotMeta", t0)
        if isinstance(sm, dict):
            plus = [t for t in sm.get("tokens", []) if str(t.get("name", "")).startswith("+")]
            hashu = [u for u in sm.get("universe", []) if str(u.get("name", "")).startswith("#")]
            print(f"spotMeta tokens '+': {len(plus)}; universe '#': {len(hashu)}")
        smc = await _try(rt, {"type": "spotMetaAndAssetCtxs"}, cache, "spotMetaAndAssetCtxs", t0)
        if isinstance(smc, list) and len(smc) > 1:
            hc = [c for c in smc[1] if str(c.get("coin", "")).startswith("#")]
            print(f"spot asset ctxs with '#': {len(hc)} sample={hc[:2]}")
        for guess in ("outcomeMetaAndAssetCtxs", "settledOutcomes", "outcomeSettlements"):
            await _try(rt, {"type": guess}, cache, f"guess_{guess}", t0)
    finally:
        await rt.aclose()


PACE_S = 2.0  # HL info weight budget is per IP and shared with the collector: <= ~30 requests a minute here
START_MS = 1_777_680_000_000  # 2026-05-02 00:00 UTC (HIP-4 mainnet)


async def phase_users(cache: Path, t0: float, max_users: int) -> None:
    """Discover past outcome coins from the public fills of accounts that trade the current BTC daily contract."""
    from tradedesk.server import build_server

    ctx = json.loads((cache / "spotMetaAndAssetCtxs.json").read_text())["data"]
    for c in ctx[1]:
        if c.get("coin") in ("#479480", "#479481"):
            print("ctx", json.dumps(c))
    meta = json.loads((cache / "outcomeMeta.json").read_text())
    for q in meta["questions"]:
        if 47953 in q.get("namedOutcomes", []):
            print("recurring question", json.dumps(q)[:400])
    users: dict[str, int] = {}
    for tag in ("479480", "479481"):
        for tr in json.loads((cache / f"recentTrades_{tag}.json").read_text())["data"]:
            for u in tr.get("users", []):
                users[u] = users.get(u, 0) + 1
    picked = [u for u, _ in sorted(users.items(), key=lambda kv: -kv[1])][:max_users]
    print(f"distinct users in recent trades: {len(users)}; probing {len(picked)}")
    _, rt = build_server()
    try:
        for i, u in enumerate(picked):
            p = cache / f"fills_{u}.json"
            if p.exists():
                continue
            rows: list[dict[str, Any]] = []
            start = START_MS
            for _page in range(5):
                await asyncio.sleep(PACE_S)
                payload = {"type": "userFillsByTime", "user": u, "startTime": start, "aggregateByTime": True}
                try:
                    f = await rt.hyperliquid._info(payload, ttl=0)
                except Exception as e:  # noqa: BLE001
                    log(f"user {i} fills error {e!r}"[:200], t0)
                    break
                page = f.data if isinstance(f.data, list) else []
                rows += [
                    {"coin": x["coin"], "time": x["time"], "px": x["px"], "sz": x["sz"]}
                    for x in page
                    if str(x.get("coin", "")).startswith("#")
                ]
                if len(page) < 2000:
                    break
                start = max(x["time"] for x in page) + 1
            p.write_text(json.dumps(rows))
            log(f"user {i}: outcome fills={len(rows)}", t0)
    finally:
        await rt.aclose()


def daily_windows(cache: Path) -> dict[int, dict[str, Any]]:
    """Per outcome id seen in any cached user fills: first/last fill time and count."""
    from tradedesk.analytics.hip4 import decode_coin

    seen: dict[int, dict[str, Any]] = {}
    for p in sorted(cache.glob("fills_*.json")):
        for x in json.loads(p.read_text()):
            d = decode_coin(x["coin"])
            if d is None:
                continue
            s = seen.setdefault(d[0], {"first": x["time"], "last": x["time"], "n": 0})
            s["first"], s["last"], s["n"] = min(s["first"], x["time"]), max(s["last"], x["time"]), s["n"] + 1
    return seen


DAY_MS = 86_400_000
SIX_MS = 6 * 3_600_000


def expiry_of(t_ms: int) -> int:
    """The first 06:00 UTC strictly after ``t_ms`` (a daily contract trading at t settles then)."""
    base = (t_ms - SIX_MS) // DAY_MS * DAY_MS + SIX_MS
    return base + DAY_MS if base <= t_ms else base


async def _candles_1h(rt: Any, cache: Path, oid: int, t_from: int, t_to: int) -> list[dict[str, Any]] | None:
    p = cache / f"c1h_{oid}.json"
    if p.exists():
        return json.loads(p.read_text())
    await asyncio.sleep(PACE_S)
    req = {"coin": f"#{10 * oid}", "interval": "1h", "startTime": t_from, "endTime": t_to}
    err_p = cache / f"c1h_{oid}.err.json"
    try:
        f = await rt.hyperliquid._info({"type": "candleSnapshot", "req": req}, ttl=0)
    except Exception as e:  # noqa: BLE001 - record the reason per id; a failed id is retried on the next run
        err_p.write_text(json.dumps({"req": req, "error": repr(e)[:300], "at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}))
        return None
    rows = [{"t": c["t"], "c": c["c"], "v": c["v"], "n": c["n"]} for c in (f.data or [])]
    p.write_text(json.dumps(rows))
    err_p.unlink(missing_ok=True)
    return rows


NONEXISTENT_ID = 999_999_999  # coin #9999999990: far above any listed outcome; the control for "server does not know it"


def settled_tried(cache: Path) -> list[int]:
    """Outcome ids seen in fills, below the current BTC daily and no longer listed in outcomeMeta (settled)."""
    meta = json.loads((cache / "outcomeMeta.json").read_text())
    listed = {o["outcome"] for o in meta["outcomes"]}
    return sorted(o for o in daily_windows(cache) if o < 47948 and o not in listed)


def error_tally(cache: Path) -> dict[str, Any]:
    """Per-id candleSnapshot errors recorded by ``_candles_1h`` vs the control requests for a non-existent coin."""
    tried = settled_tried(cache)
    msgs: dict[str, int] = {}
    stamps: list[str] = []
    no_record = 0
    for oid in tried:
        p = cache / f"c1h_{oid}.err.json"
        if (cache / f"c1h_{oid}.json").exists():
            continue
        if not p.exists():
            no_record += 1
            continue
        r = json.loads(p.read_text())
        msgs[r["error"]] = msgs.get(r["error"], 0) + 1
        stamps.append(r.get("at_utc", "?"))
    ctrl = {}
    for name in ("candles_1h", "l2Book"):
        q = cache / f"control_nonexistent_{name}.json"
        ctrl[name] = json.loads(q.read_text())["error"] if q.exists() else None
    return {
        "tried": len(tried),
        "error_recorded": sum(msgs.values()),
        "no_record": no_record,
        "messages": msgs,
        "control": ctrl,
        "same_as_control": bool(msgs) and set(msgs) == {ctrl["candles_1h"]},
        "first_last_utc": [min(stamps), max(stamps)] if stamps else None,
    }


async def phase_errors(cache: Path, t0: float, limit: int | None) -> None:
    """Record the candleSnapshot 1h error for every settled id seen in fills (same window as ``blocks``), plus the
    control requests for a non-existent coin; ids already answered or already recorded are not re-requested."""
    from tradedesk.server import build_server

    seen = daily_windows(cache)
    todo = [o for o in settled_tried(cache) if not (cache / f"c1h_{o}.json").exists() and not (cache / f"c1h_{o}.err.json").exists()]
    todo = todo[:limit] if limit else todo
    log(f"settled ids to request: {len(todo)}", t0)
    _, rt = build_server()
    try:
        for name, payload in (
            (
                "candles_1h",
                {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": f"#{10 * NONEXISTENT_ID}",
                        "interval": "1h",
                        "startTime": int(time.time() * 1000) - 3 * DAY_MS,
                        "endTime": int(time.time() * 1000),
                    },
                },
            ),
            ("l2Book", {"type": "l2Book", "coin": f"#{10 * NONEXISTENT_ID}"}),
        ):
            if not (cache / f"control_nonexistent_{name}.json").exists():
                await asyncio.sleep(PACE_S)
                await _try(rt, payload, cache, f"control_nonexistent_{name}", t0)
        for i, oid in enumerate(todo):
            e = expiry_of(seen[oid]["first"])
            await _candles_1h(rt, cache, oid, e - 2 * DAY_MS, e + DAY_MS)
            if i % 25 == 0:
                log(f"errors {i}/{len(todo)}", t0)
    finally:
        await rt.aclose()
    t = error_tally(cache)
    print(
        f"  settled candleSnapshot 1h: tried {t['tried']}, error recorded {t['error_recorded']}, no record {t['no_record']}, "
        f"messages {t['messages']}, at {t['first_last_utc']}"
    )
    print(f"  control #{10 * NONEXISTENT_ID}: {t['control']}; settled errors identical to control: {t['same_as_control']}")


async def phase_blocks(cache: Path, t0: float, low_probe: int) -> None:
    """1h candles of outcomes seen in fills whose fills fit one 06:00-to-06:00 day, then walk down to each day's
    first contiguous id that also opened at that day's 06:00 (the recurring block; BTC binary first, by inference)."""
    from tradedesk.server import build_server

    seen = daily_windows(cache)
    now = int(time.time() * 1000)
    cands = {
        oid: s
        for oid, s in seen.items()
        # settlement shows up as a fill stamped at (or a few ms after) 06:00, so allow the last fill up to 60 s late
        if oid < 47948 and s["last"] <= expiry_of(s["first"]) + 60_000 and expiry_of(s["first"]) < now
    }
    log(f"candidate one-day outcomes: {len(cands)} of {len(seen)}", t0)
    _, rt = build_server()
    starts: dict[int, int | None] = {}
    try:
        for oid in range(low_probe):  # are the earliest ids (May) still answered?
            rows = await _candles_1h(rt, cache, oid, START_MS - DAY_MS, now)
            starts[oid] = rows[0]["t"] if rows else None
        log(f"low ids 0..{low_probe - 1}: with candles {[o for o in range(low_probe) if starts.get(o)]}", t0)
        for i, (oid, s) in enumerate(sorted(cands.items())):
            e = expiry_of(s["first"])
            rows = await _candles_1h(rt, cache, oid, e - 2 * DAY_MS, e + DAY_MS)
            starts[oid] = rows[0]["t"] if rows else None
            if i % 25 == 0:
                log(f"candles {i}/{len(cands)}", t0)
        days: dict[int, list[int]] = {}
        for oid in cands:
            st = starts.get(oid)
            if st is not None and st % DAY_MS == SIX_MS:
                days.setdefault(st + DAY_MS, []).append(oid)
        for e, ids in sorted(days.items()):
            lo = min(ids)
            for _ in range(10):  # walk down while the previous id opened at the same 06:00
                rows = await _candles_1h(rt, cache, lo - 1, e - 2 * DAY_MS, e + DAY_MS)
                if not rows or rows[0]["t"] != e - DAY_MS:
                    break
                lo -= 1
            days[e] = sorted(set(ids) | {lo})
        (cache / "blocks.json").write_text(json.dumps({str(k): v for k, v in sorted(days.items())}))
        log(f"days with a 06:00-opened outcome: {len(days)}", t0)
    finally:
        await rt.aclose()


FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "hip4_outcomes_2026-11-26.json"


def phase_fixture(cache: Path) -> None:
    """Trimmed raw sample for offline tests (recurring outcomes, a few deployer outcomes, one book, two ctxs)."""
    meta = json.loads((cache / "outcomeMeta.json").read_text())
    rec = [o for o in meta["outcomes"] if "class:" in o["description"]]
    tpl = [o for o in meta["outcomes"] if o["outcome"] in (46302, 41462, 48518, 45941)]
    qs = [q for q in meta["questions"] if q["question"] == 708]
    book = json.loads((cache / "l2Book_479480.json").read_text())["data"]
    ctx = json.loads((cache / "spotMetaAndAssetCtxs.json").read_text())["data"][1]
    sample = {
        "_source": "POST https://api.hyperliquid.xyz/info, public, no key; fetched 2026-11-26 ~17:47 UTC by "
        "scripts/verify_hip4_step0.py; outcomes trimmed to the recurring series and 4 deployer outcomes",
        "outcomeMeta": {"outcomes": rec + tpl, "questions": qs, "feeScale": meta.get("feeScale")},
        "l2Book_479480": book,
        "spotAssetCtxs": [c for c in ctx if c.get("coin") in ("#479480", "#479481")],
    }
    FIXTURE.write_text(json.dumps(sample, indent=1) + "\n")
    print("wrote", FIXTURE, len(rec), "recurring,", len(tpl), "template")


FEE_LINES = ("Fees are currently zero", "only charges fees when closing or settling", "does not support rebates")


def btc_daily_by_day(seen: dict[int, dict[str, Any]]) -> dict[int, int]:
    """expiry ms -> inferred BTC daily binary id: lowest id whose fills all lie in (D-1 06:00, D 06:00 + 60 s] and
    that has a fill within 60 s after D 06:00 (the settlement record). Inference: today's protocol block 47948-47955
    starts with the BTC binary, and the blocks seen in fills are spaced about one day apart."""
    out: dict[int, int] = {}
    for oid, s in seen.items():
        e = expiry_of(s["first"])
        if s["first"] > e - DAY_MS and e <= s["last"] <= e + 60_000:
            out[e] = min(out.get(e, oid), oid)
    return dict(sorted(out.items()))


async def _settled_probe(cache: Path, oid: int, t0: float) -> dict[str, Any]:
    from tradedesk.server import build_server

    _, rt = build_server()
    res: dict[str, Any] = {}
    now = int(time.time() * 1000)
    try:
        for name, payload in (
            ("l2Book", {"type": "l2Book", "coin": f"#{10 * oid}"}),
            ("recentTrades", {"type": "recentTrades", "coin": f"#{10 * oid}"}),
            (
                "candles_1m",
                {
                    "type": "candleSnapshot",
                    "req": {"coin": f"#{10 * oid}", "interval": "1m", "startTime": now - 3 * DAY_MS, "endTime": now},
                },
            ),
            (
                "candles_token",
                {
                    "type": "candleSnapshot",
                    "req": {"coin": f"+{10 * oid}", "interval": "1h", "startTime": now - 3 * DAY_MS, "endTime": now},
                },
            ),
        ):
            cached = cache / f"settled_{oid}_{name}.json"
            if cached.exists():  # keep the first run's answer (the report quotes it); delete the file to re-probe
                d = json.loads(cached.read_text())["data"]
            else:
                await asyncio.sleep(PACE_S)
                d = await _try(rt, payload, cache, f"settled_{oid}_{name}", t0)
            raw = json.loads(cached.read_text())
            res[name] = raw["error"] or f"ok, {len(d) if isinstance(d, list | dict) else d!r}"[:120]
            if name == "l2Book" and isinstance(d, dict):
                lv = d.get("levels") or [[], []]
                res[name] += f" (levels {len(lv[0])}/{len(lv[1])})"
    finally:
        await rt.aclose()
    return res


def phase_inventory(cache: Path, out: Path | None, t0: float) -> None:
    from tradedesk.analytics.hip4 import book_summary, priced_in_window, recurring_series, series_inventory

    meta = json.loads((cache / "outcomeMeta.json").read_text())
    rep: dict[str, Any] = {"fetched_utc_note": "outcomeMeta/books/ctx cached 2026-11-26 ~17:47 UTC"}
    inv = series_inventory(meta)
    rep["n_outcomes"], rep["n_questions"], rep["feeScale"] = len(meta["outcomes"]), len(meta["questions"]), meta.get("feeScale")
    by_venue: dict[str, int] = {}
    for k, v in inv.items():
        by_venue[k.split(":")[0]] = by_venue.get(k.split(":")[0], 0) + v
    rep["by_venue"] = by_venue
    rep["recurring"] = recurring_series(meta)
    rep["recurring_questions"] = [q for q in meta["questions"] if "class:" in q["description"]]
    print(f"outcomes {rep['n_outcomes']} questions {rep['n_questions']} feeScale {rep['feeScale']} by venue {by_venue}")
    for r in rep["recurring"]:
        print(f"  recurring #{r['outcome']} {r['cls']} {r['underlying']} {r['period']} target {r['target']}")
    for q in rep["recurring_questions"]:
        print(f"  recurring question {q['question']}: {q['description']}")
    ctx = {
        c["coin"]: c
        for c in json.loads((cache / "spotMetaAndAssetCtxs.json").read_text())["data"][1]
        if str(c.get("coin", "")).startswith("#")
    }
    rep["books"] = {}
    for coin in ("#479480", "#479481"):
        s = book_summary(json.loads((cache / f"l2Book_{coin[1:]}.json").read_text())["data"])
        rep["books"][coin] = {**s, "ctx": ctx.get(coin)}
        c = ctx.get(coin, {})
        print(
            f"  book {coin}: bid {s['best_bid']} ask {s['best_ask']} spread {s['spread']:.4f} "
            f"depth bid/ask ${s['depth_bid_usd']:.0f}/${s['depth_ask_usd']:.0f} "
            f"(+-5pp ${s['bid_band_usd']:.0f}/${s['ask_band_usd']:.0f}) | dayNtlVlm {c.get('dayNtlVlm')} "
            f"dayBaseVlm {c.get('dayBaseVlm')} circulatingSupply {c.get('circulatingSupply')}"
        )
    # day volume (Yes-side premium) over every listed outcome, split by recurring BTC daily vs the rest
    rec_ids = {r["outcome"] for r in rep["recurring"]}
    tot = sum(float(c.get("dayNtlVlm") or 0) for k, c in ctx.items() if k.endswith("0"))
    rec = {r["underlying"]: float(ctx.get(r["coin_yes"], {}).get("dayNtlVlm") or 0) for r in rep["recurring"]}
    rep["day_ntl_yes_side"] = {"all_listed": tot, "recurring_by_underlying": rec, "n_recurring": len(rec_ids)}
    print(f"  day volume, Yes side, all listed outcomes ${tot:.0f}; recurring binaries {rec}")
    # history: settled candles
    seen = daily_windows(cache)
    listed = {o["outcome"] for o in meta["outcomes"]}
    tried = [o for o in seen if o < 47948 and o not in listed]  # settled (no longer listed)
    have = [o for o in tried if (cache / f"c1h_{o}.json").exists()]
    rep["settled_candles"] = {"tried": len(tried), "answered": len(have), "answered_ids": sorted(have)}
    print(f"  candleSnapshot 1h for settled/old outcomes seen in fills: tried {len(tried)}, answered {len(have)} {sorted(have)}")
    rep["settled_errors"] = error_tally(cache)
    t = rep["settled_errors"]
    print(
        f"  per-id errors: recorded {t['error_recorded']} of {t['tried']} (no record {t['no_record']}), {t['messages']}, "
        f"at {t['first_last_utc']}; control non-existent coin {t['control']}; identical: {t['same_as_control']}"
    )
    # Yes/No mirror: ask Yes <-> bid No at 1 - p, and bid Yes <-> ask No (two snapshots, taken a moment apart)
    by, bn = (json.loads((cache / f"l2Book_{c}.json").read_text())["data"] for c in ("479480", "479481"))
    ya, yb, na, nb = by["levels"][1][0], by["levels"][0][0], bn["levels"][1][0], bn["levels"][0][0]
    rep["mirror"] = {
        "ask_yes_vs_bid_no": [ya["px"], ya["sz"], nb["px"], nb["sz"]],
        "bid_yes_vs_ask_no": [yb["px"], yb["sz"], na["px"], na["sz"]],
        "snapshot_gap_s": (bn["time"] - by["time"]) / 1000,
    }

    def tot(side: list[dict[str, Any]]) -> float:  # contracts over all returned levels
        return round(sum(float(x["sz"]) for x in side), 2)

    rep["mirror"]["total_contracts"] = {
        "ask_yes": tot(by["levels"][1]),
        "bid_no": tot(bn["levels"][0]),
        "bid_yes": tot(by["levels"][0]),
        "ask_no": tot(bn["levels"][1]),
    }
    print(
        f"  mirror (top level): ask Yes {ya['px']} x {ya['sz']} vs bid No {nb['px']} x {nb['sz']} (1-p {1 - float(nb['px']):.5f}); "
        f"bid Yes {yb['px']} x {yb['sz']} vs ask No {na['px']} x {na['sz']} (1-p {1 - float(na['px']):.5f}); "
        f"20-level totals {rep['mirror']['total_contracts']}; No book {rep['mirror']['snapshot_gap_s']:.1f} s after Yes"
    )
    days = btc_daily_by_day(seen)
    users = sorted(cache.glob("fills_*.json"))
    acct = []
    for p in users:  # no addresses printed: rows (outcome fills only) and the date span per account
        ts = [x["time"] for x in json.loads(p.read_text())]
        acct.append((len(ts), time.strftime("%Y-%m-%d", time.gmtime(min(ts) / 1000)) if ts else "-"))
    rep["fills_per_account"] = sorted(acct, reverse=True)
    print(f"  outcome fills per account (rows, earliest; API keeps the last 10 000 fills): {rep['fills_per_account']}")
    rec_days = {e: oid for e, oid in days.items() if priced_in_window([t for t in _fill_times(cache, oid)], e)}
    # settled expiries: 2026-05-03 06:00 (first contract) .. the last 06:00 before the snapshot (outcomeMeta.json mtime)
    as_of = int((cache / "outcomeMeta.json").stat().st_mtime * 1000)
    rep["as_of_utc"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(as_of / 1000))
    n_total = (expiry_of(as_of) - DAY_MS - (START_MS + DAY_MS + SIX_MS)) // DAY_MS + 1
    rep["fills"] = {
        "users": len(users),
        "btc_daily_days_seen": len(days),
        "recoverable_days": len(rec_days),
        "settled_days_since_2026_05_03": int(n_total),
        "first_day": time.strftime("%Y-%m-%d", time.gmtime(min(days) / 1000)) if days else None,
        "ids": {time.strftime("%Y-%m-%d", time.gmtime(e / 1000)): o for e, o in days.items()},
    }
    print(
        f"  userFillsByTime ({len(users)} accounts): inferred BTC daily contracts seen {len(days)} "
        f"(from {rep['fills']['first_day']}), with a fill 1-6 h before 06:00: {len(rec_days)} of {n_total} days"
    )
    probe_id = days[max(days)] if days else None
    if probe_id is not None:
        rep["settled_probe"] = {"outcome": probe_id, **asyncio.run(_settled_probe(cache, probe_id, t0))}
        print(f"  settled probe #{probe_id}: {rep['settled_probe']}")
    fees = []
    for name in ("hip4", "fees"):
        txt = (cache / f"doc_{name}.md").read_text()
        fees += [ln.strip()[:240] for ln in txt.splitlines() if any(k in ln for k in FEE_LINES)]
    rep["fee_doc_lines"] = fees
    for ln in fees:
        print("  doc:", ln)
    if out:
        out.write_text(json.dumps(rep, indent=1, default=str))
        print("wrote", out)


def _fill_times(cache: Path, oid: int) -> list[int]:
    coin = f"#{10 * oid}"
    alt = f"#{10 * oid + 1}"
    ts: list[int] = []
    for p in sorted(cache.glob("fills_*.json")):
        ts += [x["time"] for x in json.loads(p.read_text()) if x["coin"] in (coin, alt)]
    return ts


def probed_users(cache: Path, max_users: int) -> list[str]:
    return [p.stem.removeprefix("fills_") for p in sorted(cache.glob("fills_*.json"))][:max_users]


async def phase_fees(cache: Path, t0: float, users: list[str]) -> None:
    """Observed fee on recent outcome fills (``fee`` / (px*sz)) of the accounts already probed; by fill direction."""
    from tradedesk.server import build_server

    stats: dict[str, list[float]] = {}
    zero_px: dict[str, list[int]] = {}  # dir -> [zero fee at px 0, zero fee at px > 0, nonzero fee at px 0]
    last = 0
    _, rt = build_server()
    try:
        for u in users:
            p = cache / f"userFills_{u}.json"  # outcome rows only; outside git (the name holds the address)
            if not p.exists():
                await asyncio.sleep(PACE_S)
                f = await rt.hyperliquid._info({"type": "userFills", "user": u}, ttl=0)
                rows = [
                    {k: x.get(k) for k in ("coin", "time", "px", "sz", "dir", "fee", "feeToken", "builderFee")}
                    for x in f.data or []
                    if str(x.get("coin", "")).startswith("#")
                ]
                p.write_text(json.dumps(rows))
            for x in json.loads(p.read_text()):
                px, sz, fee = float(x["px"]), float(x["sz"]), float(x.get("fee") or 0)
                key = f"{x.get('dir', '?')}|{x.get('feeToken', '?')}"
                stats.setdefault(key, []).append(fee / (px * sz) if px * sz > 0 else 0.0)
                z = zero_px.setdefault(key, [0, 0, 0])
                z[0] += fee == 0 and px == 0
                z[1] += fee == 0 and px > 0
                z[2] += fee != 0 and px == 0
                last = max(last, x["time"])
        log(f"fills of {len(users)} accounts; last outcome fill {time.strftime('%m-%d %H:%M', time.gmtime(last / 1000))} UTC", t0)
    finally:
        await rt.aclose()

    def q(v: list[float], a: float) -> float:
        s = sorted(v)
        return s[min(len(s) - 1, int(a * len(s)))]

    out: dict[str, Any] = {}
    for k, v in sorted(stats.items(), key=lambda kv: -len(kv[1])):
        nz = [r for r in v if r != 0]
        z = zero_px[k]
        out[k] = {"n": len(v), "nonzero": len(nz), "zero_fee_px0": z[0], "zero_fee_px_pos": z[1], "fee_at_px0": z[2]}
        line = f"  {k:28s} n={len(v):5d} nonzero={len(nz):5d} zero@px0={z[0]:4d} zero@px>0={z[1]:5d} fee@px0={z[2]}"
        if nz:
            out[k].update({"min": min(nz), "p50": q(nz, 0.5), "p90": q(nz, 0.9), "max": max(nz)})
            line += f" fee/notional min {min(nz):.5f} p50 {q(nz, 0.5):.5f} p90 {q(nz, 0.9):.5f} max {max(nz):.5f}"
        print(line)
    (cache / "fee_stats.json").write_text(json.dumps(out, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="meta")
    ap.add_argument("--cache-dir", type=Path, default=CACHE)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--max-users", type=int, default=6)
    ap.add_argument("--low-probe", type=int, default=12)
    ap.add_argument("--limit", type=int, default=None, help="errors phase: request at most this many ids (timing)")
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if a.phase == "meta":
        asyncio.run(phase_meta(a.cache_dir, t0))
    elif a.phase == "peek":
        phase_peek(a.cache_dir)
    elif a.phase == "probe":
        asyncio.run(phase_probe(a.cache_dir, t0))
    elif a.phase == "fees":
        asyncio.run(phase_fees(a.cache_dir, t0, probed_users(a.cache_dir, a.max_users)))
    elif a.phase == "errors":
        asyncio.run(phase_errors(a.cache_dir, t0, a.limit))
    elif a.phase == "inventory":
        phase_inventory(a.cache_dir, a.out, t0)
    elif a.phase == "listfills":
        fmt = "%m-%d %H:%M"
        for oid, s in sorted(daily_windows(a.cache_dir).items()):
            if oid >= a.low_probe:
                c = a.cache_dir / f"c1h_{oid}.json"
                cn = len(json.loads(c.read_text())) if c.exists() else "-"
                print(
                    oid,
                    time.strftime(fmt, time.gmtime(s["first"] / 1000)),
                    time.strftime(fmt, time.gmtime(s["last"] / 1000)),
                    s["n"],
                    "c1h",
                    cn,
                )
    elif a.phase == "fixture":
        phase_fixture(a.cache_dir)
    elif a.phase == "blocks":
        asyncio.run(phase_blocks(a.cache_dir, t0, a.low_probe))
    elif a.phase == "users":
        asyncio.run(phase_users(a.cache_dir, t0, a.max_users))
        seen = daily_windows(a.cache_dir)
        ids = sorted(seen)
        print(f"distinct outcome ids in fills: {len(ids)}; range {ids[:1]}..{ids[-1:]}")
        old = [i for i in ids if i < 47948]
        print(
            f"ids below the current BTC daily: {len(old)}; earliest fill day:",
            time.strftime("%Y-%m-%d", time.gmtime(min(s["first"] for s in seen.values()) / 1000)) if seen else "-",
        )


if __name__ == "__main__":
    main()
