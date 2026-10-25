"""Polymarket long-dated tail NO (IDEA-20261025-pm-longdated-tail-no): Step 0 gates (no outcomes read).

Network only through the runtime providers (``rt.polymarket`` CLOB / data-api Http clients); per-file JSON cache under
``--cache-dir`` (default ``~/.cache/tradedesk-agents/pm_longdated_tail_no``). The market universe is streamed from the
calibration study's ``universe.json`` (``--src-cache``), classified there before any price.

Usage:
    uv run python scripts/verify_pm_longdated_tail_no.py --stage universe            # long non-sport markets (no prices)
    uv run python scripts/verify_pm_longdated_tail_no.py --stage fetch --limit 200   # timing sample + estimate
    uv run python scripts/verify_pm_longdated_tail_no.py --stage fetch               # /prices-history T-91d..T-7d
    uv run python scripts/verify_pm_longdated_tail_no.py --stage trades              # last prints at T-30 (freshness, q)
    uv run python scripts/verify_pm_longdated_tail_no.py --stage probe_empty         # G2 diagnostic: prints on empty history
    uv run python scripts/verify_pm_longdated_tail_no.py --stage stage0 --out FILE   # gates G1-G3, outcomes never opened
Outcomes are written to a separate ``outcomes.json`` that the Step 0 stages do not open.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import resource
import statistics
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.pm_calibration import HOUR_S, mde
from tradedesk.analytics.pm_longdated import (
    DAY_S,
    EXCLUDED_CATEGORIES,
    design_effect,
    eligible_long,
    fresh_trade_q,
    in_tail,
    long_lived,
    mde_from_se,
    month_key,
    null_power,
    t0_sched,
    tail_bucket,
)
from tradedesk.analytics.pm_threshold import quote_at

DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/pm_longdated_tail_no").expanduser()
SRC_CACHE = Path("~/.cache/tradedesk-agents/pm_yes_calibration").expanduser()
T_MIN = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
T_MAX = int(datetime(2026, 10, 15, tzinfo=UTC).timestamp())
H_DAYS = (90, 30, 7)
H_MAIN = 30
MIN_LIFE_D = 60
LISTED_LEAD_D = 7
FRESH_D = 7.0
PRESCREEN = (0.01, 0.40)  # /prices-history band that triggers a /trades request (wider than the 0.03-0.30 tail)
Q_MAX_AGE = 6 * HOUR_S  # prices-history point age at t0 (fidelity 60 min)
KEEP_S = DAY_S  # cache points in [t0 - 1 d, t0] per horizon
G1_MIN, G2_MIN, G3_MAX = 3000, 0.70, 0.02


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _write(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


def _needs_fetch(p: Path) -> bool:
    if not p.exists():
        return True
    if p.stat().st_size > 600:
        return False
    try:
        return "error" in json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return True


async def _retry(coro_fn, what: str, tries: int = 5):  # noqa: ANN001, ANN202
    from tradedesk.core.errors import ProviderError

    for k in range(tries):
        try:
            return await coro_fn()
        except ProviderError as e:
            if (
                e.reason in ("config", "auth", "data_unavailable")
                or (e.detail or "").startswith("HTTP 4")
                and "429" not in (e.detail or "")
            ):
                raise
            print(f"  retry {what}: {str(e)[:120]}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  retry {what} after {type(e).__name__}: {str(e)[:120]}", flush=True)
        await asyncio.sleep(5 * (k + 1))
    raise RuntimeError(f"giving up on {what}")


# ----------------------------------------------------------------------------- stage: universe (no prices)


def _stream_markets(path: Path, chunk: int = 32 << 20):  # noqa: ANN202
    """Yield the objects of the top-level "markets" array of a ~2 GB JSON file, reading it in chunks."""
    dec = json.JSONDecoder()
    with path.open() as f:
        buf = ""
        while '"markets": [' not in buf:
            more = f.read(chunk)
            if not more:
                return
            buf += more
        i = buf.index('"markets": [') + len('"markets": [')
        buf = buf[i:]
        i = 0
        while True:
            while i < len(buf) and buf[i] in " ,\n\r\t":
                i += 1
            if i >= len(buf) - 1:
                more = f.read(chunk)
                if not more:
                    return
                buf = buf[i:] + more
                i = 0
                continue
            if buf[i] == "]":
                return
            try:
                obj, j = dec.raw_decode(buf, i)
            except json.JSONDecodeError:
                more = f.read(chunk)
                if not more:
                    raise
                buf = buf[i:] + more
                i = 0
                continue
            yield obj
            i = j
            if i > chunk:
                buf = buf[i:]
                i = 0


def stage_universe(cache: Path, src: Path) -> None:
    t = time.perf_counter()
    why: Counter = Counter()
    keep: list[dict[str, Any]] = []
    outcomes: dict[str, int] = {}
    named_ev: set[str] = set()
    n = 0
    for m in _stream_markets(src / "universe.json"):
        n += 1
        if n % 500_000 == 0:
            print(f"  streamed {n} markets {time.perf_counter() - t:.0f}s rss {rss_mb():.0f} MB", flush=True)
        if not m.get("yes_no"):
            why["not_yes_no"] += 1
            if m.get("category") not in EXCLUDED_CATEGORIES and long_lived(m, MIN_LIFE_D):
                why["not_yes_no_but_long_nonsport"] += 1  # named binaries: descriptive count only
                named_ev.add(m["event_id"])
            continue
        if m.get("category") in EXCLUDED_CATEGORIES:
            why["sports"] += 1
            continue
        if not (T_MIN <= int(m["T"]) < T_MAX):
            why["out_of_period"] += 1
            continue
        if not long_lived(m, MIN_LIFE_D):
            why["life_lt_60d"] += 1
            continue
        el = {str(h): eligible_long(m, t0_sched(m["T"], h), listed_lead_days=LISTED_LEAD_D) for h in H_DAYS}
        if not any(el.values()):
            why["not_open_at_any_t0"] += 1
            continue
        why["kept"] += 1
        outcomes[m["market_id"]] = int(m["outcome"])
        r = {k: v for k, v in m.items() if k != "outcome"}
        r["eligible"] = el
        keep.append(r)
    _write(cache / "long_markets.json", {"why": dict(why), "n_streamed": n, "named_events": len(named_ev), "markets": keep})
    _write(cache / "outcomes.json", outcomes)  # never opened by Step 0 stages
    ev = {m["event_id"] for m in keep}
    print(f"universe: streamed {n}, kept {len(keep)} markets / {len(ev)} events; why {dict(why)}", flush=True)
    print(f"  named (non Yes/No) long non-sport binaries: events {len(named_ev)} (not in the universe)", flush=True)
    ev30 = {m["event_id"] for m in keep if m["eligible"][str(H_MAIN)]}
    print(
        f"  open at T_sched - {H_MAIN} d: markets {sum(m['eligible'][str(H_MAIN)] for m in keep)}, events {len(ev30)}; "
        f"slug-dated events {len({m['event_id'] for m in keep if m['eligible'][str(H_MAIN)] and m.get('slug_dated')})}",
        flush=True,
    )
    print(
        f"  events by category (T-{H_MAIN}): {dict(Counter(m['category'] for m in {m['event_id']: m for m in keep if m['eligible'][str(H_MAIN)]}.values()).most_common())}",
        flush=True,
    )
    print(
        f"  events by year of T_sched: {dict(sorted(Counter(m['year'] for m in {m['event_id']: m for m in keep}.values()).items()))}",
        flush=True,
    )
    print(f"  {time.perf_counter() - t:.0f}s peak rss {rss_mb():.0f} MB", flush=True)


def load_long(cache: Path) -> list[dict[str, Any]]:
    return json.loads((cache / "long_markets.json").read_text())["markets"]


def qpath(cache: Path, mid: str, h: int) -> Path:
    return cache / "quotes" / mid[-2:] / f"{mid}_h{h}.json"


def tpath(cache: Path, mid: str, h: int) -> Path:
    return cache / "trades" / mid[-2:] / f"{mid}_h{h}.json"


# ----------------------------------------------------------------------------- stage: fetch (/prices-history)


async def stage_fetch(cache: Path, limit: int | None, rate: float, hs: tuple[int, ...]) -> None:
    from tradedesk.core.ratelimit import TokenBucket
    from tradedesk.server import build_server

    ms = load_long(cache)
    pool = ms
    if limit:  # a seeded sample of EVENTS (all their markets), chosen from ids only
        evs = sorted({m["event_id"] for m in ms})
        sel = set(random.Random(7).sample(evs, min(limit, len(evs))))
        pool = [m for m in ms if m["event_id"] in sel]
    # the API rejects long [startTs, endTs] windows ("interval is too long"): one 1-day window per horizon
    todo = [(m, h) for m in pool for h in hs if m["eligible"][str(h)] and _needs_fetch(qpath(cache, m["market_id"], h))]
    print(f"quotes: pool={len(pool)} markets, to fetch={len(todo)} (market, h) for h={hs} rate={rate}/s", flush=True)
    _, rt = build_server()
    rt.polymarket.clob._limiter = TokenBucket(rate, int(2 * rate))
    sem = asyncio.Semaphore(max(4, int(2 * rate)))
    t_start = time.perf_counter()
    done = errs = 0

    async def one(m: dict[str, Any], h: int) -> None:
        nonlocal done, errs
        t0 = t0_sched(m["T"], h)
        params = {"market": m["yes_token"], "startTs": t0 - KEEP_S, "endTs": t0 + 60, "fidelity": 60}
        try:
            async with sem:
                f = await _retry(
                    lambda: rt.polymarket.clob.get_json("/prices-history", params=params, ttl=0, tool="prices-history", args=params), "hist"
                )
            hist = (f.data or {}).get("history") or [] if isinstance(f.data, dict) else []
            pts = sorted((int(p["t"]), float(p["p"])) for p in hist if isinstance(p, dict) and "t" in p and "p" in p)
            steps = [b[0] - a[0] for a, b in zip(pts, pts[1:], strict=False)]
            keep = [{"t": tt, "p": p} for tt, p in pts if t0 - KEEP_S <= tt <= t0]
            obj = {"n": len(pts), "step": statistics.median(steps) if steps else None, "pts": keep}
        except Exception as e:  # noqa: BLE001
            errs += 1
            obj = {"error": f"{type(e).__name__}: {str(e)[:200]}", "pts": []}
        _write(qpath(cache, m["market_id"], h), obj)
        done += 1
        if done == min(100, len(todo)):
            el = time.perf_counter() - t_start
            n_all = sum(m["eligible"][str(h)] for m in ms for h in hs)
            print(
                f"  sample: {done} in {el:.1f}s -> {done / el:.1f}/s; all {n_all} (market, h): {n_all / (done / el) / 60:.0f} min",
                flush=True,
            )
        if done % 2000 == 0:
            el = time.perf_counter() - t_start
            print(f"  quotes {done}/{len(todo)} {el:.0f}s ({done / el:.1f}/s, errors {errs}) rss {rss_mb():.0f} MB", flush=True)

    try:
        await asyncio.gather(*(one(m, h) for m, h in todo))
    finally:
        await rt.aclose()
    el = time.perf_counter() - t_start
    print(f"quotes done {done} in {el:.0f}s ({done / max(el, 1e-9):.1f}/s) errors={errs} rss {rss_mb():.0f} MB", flush=True)
    print("fetch done", flush=True)


def hist_q(cache: Path, m: dict[str, Any], h: int) -> tuple[dict[str, Any] | None, tuple[float, int] | None]:
    p = qpath(cache, m["market_id"], h)
    if not p.exists():
        return None, None
    d = json.loads(p.read_text())
    return d, quote_at(d.get("pts") or [], t0_sched(m["T"], h), max_age=Q_MAX_AGE)


# ----------------------------------------------------------------------------- stage: trades (q from a print)


async def stage_trades(cache: Path, rate: float, hs: tuple[int, ...]) -> None:
    from tradedesk.core.ratelimit import TokenBucket
    from tradedesk.server import build_server

    ms = load_long(cache)
    todo = []
    for m in ms:
        for h in hs:
            if not m["eligible"][str(h)]:
                continue
            _, qa = hist_q(cache, m, h)
            if qa and PRESCREEN[0] <= qa[0] <= PRESCREEN[1] and _needs_fetch(tpath(cache, m["market_id"], h)):
                todo.append((m, h))
    print(f"trades: {len(todo)} (market, h) to fetch rate={rate}/s", flush=True)
    _, rt = build_server()
    rt.polymarket.data._limiter = TokenBucket(rate, int(2 * rate))
    sem = asyncio.Semaphore(max(4, int(2 * rate)))
    t_start = time.perf_counter()
    done = errs = 0

    async def one(m: dict[str, Any], h: int) -> None:
        nonlocal done, errs
        t0 = t0_sched(m["T"], h)
        try:
            async with sem:
                f = await _retry(lambda: rt.polymarket.trades(m["condition_id"], limit=5, end=t0), "trades")
            obj: dict[str, Any] = {"trades": f.data}
        except Exception as e:  # noqa: BLE001
            errs += 1
            obj = {"error": f"{type(e).__name__}: {str(e)[:200]}", "trades": []}
        _write(tpath(cache, m["market_id"], h), obj)
        done += 1
        if done == min(100, len(todo)):
            el = time.perf_counter() - t_start
            print(f"  sample: {done} in {el:.1f}s -> estimate {el / done * len(todo) / 60:.0f} min", flush=True)
        if done % 2000 == 0:
            print(f"  trades {done}/{len(todo)} {time.perf_counter() - t_start:.0f}s errors {errs} rss {rss_mb():.0f} MB", flush=True)

    try:
        await asyncio.gather(*(one(m, h) for m, h in todo))
    finally:
        await rt.aclose()
    print(f"trades done {done} in {time.perf_counter() - t_start:.0f}s errors={errs}", flush=True)


def trade_q(cache: Path, m: dict[str, Any], h: int) -> tuple[bool, dict[str, Any] | None]:
    """(fetched, fresh last print at t0 or None)."""
    p = tpath(cache, m["market_id"], h)
    if not p.exists():
        return False, None
    d = json.loads(p.read_text())
    if d.get("error"):
        return False, None
    return True, fresh_trade_q(d.get("trades") or [], t0_sched(m["T"], h), m["yes_token"], max_age_days=FRESH_D)


async def stage_probe_empty(cache: Path, rate: float, n_pick: int) -> None:
    """Diagnostic for G2: do markets with an EMPTY /prices-history window at T-30 have fresh prints? (no outcomes)"""
    from tradedesk.core.ratelimit import TokenBucket
    from tradedesk.server import build_server

    ms = load_long(cache)
    empty, by_year, by_neg = [], Counter(), Counter()
    n_all = Counter()
    for m in ms:
        if not m["eligible"][str(H_MAIN)]:
            continue
        d, _ = hist_q(cache, m, H_MAIN)
        if d is None or d.get("error"):
            continue
        n_all[m["year"]] += 1
        n_all[f"neg={bool(m.get('neg_risk'))}"] += 1
        if not d.get("n"):
            empty.append(m)
            by_year[m["year"]] += 1
            by_neg[f"neg={bool(m.get('neg_risk'))}"] += 1
    print(f"probe_empty: empty /prices-history windows at T-{H_MAIN}: {len(empty)}", flush=True)
    for k in sorted(n_all, key=str):
        e = by_year.get(k, 0) + by_neg.get(k, 0)
        print(f"  {k}: empty {e}/{n_all[k]} = {e / max(1, n_all[k]):.3f}", flush=True)
    pick = random.Random(7).sample(empty, min(n_pick, len(empty)))
    _, rt = build_server()
    rt.polymarket.data._limiter = TokenBucket(rate, int(2 * rate))
    res: list[dict[str, Any] | None] = []
    try:
        for m in pick:
            p = cache / "trades_probe" / f"{m['market_id']}_h{H_MAIN}.json"
            if _needs_fetch(p):
                f = await _retry(lambda m=m: rt.polymarket.trades(m["condition_id"], limit=5, end=t0_sched(m["T"], H_MAIN)), "trades")
                _write(p, {"trades": f.data})
            tr = json.loads(p.read_text())["trades"]
            res.append(fresh_trade_q(tr, t0_sched(m["T"], H_MAIN), m["yes_token"], max_age_days=FRESH_D))
    finally:
        await rt.aclose()
    fresh = [r for r in res if r]
    tail = [r for r in fresh if in_tail(r["yes_px"])]
    print(
        f"  probe {len(pick)} empty-history markets: fresh print <= {FRESH_D:.0f} d {len(fresh)} ({len(fresh) / max(1, len(pick)):.3f}), "
        f"of them q in tail {len(tail)}",
        flush=True,
    )


# ----------------------------------------------------------------------------- stage: stage0 (gates, no outcomes)


def build_rows(cache: Path, ms: list[dict[str, Any]], h: int) -> list[dict[str, Any]]:
    rows = []
    for m in ms:
        if not m["eligible"][str(h)]:
            continue
        d, qa = hist_q(cache, m, h)
        fetched, lt = trade_q(cache, m, h)
        rows.append(
            {
                "m": m,
                "hist_fetched": d is not None and not d.get("error"),
                "hist_q": qa[0] if qa else None,
                "trades_fetched": fetched,
                "trade": lt,
                "q": lt["yes_px"] if lt else None,
            }
        )
    return rows


def gate_events(rows: list[dict[str, Any]], slug_dated_only: bool = False) -> list[dict[str, Any]]:
    """Events with at least one fresh tail market; ``qs`` = every open option with a q (print, else history quote)."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if slug_dated_only and not r["m"].get("slug_dated"):
            continue
        by[r["m"]["event_id"]].append(r)
    out = []
    for eid, rs in sorted(by.items()):
        tail = [r["m"]["market_id"] for r in rs if in_tail(r["q"])]
        if not tail:
            continue
        qs = {}
        for r in rs:
            q = r["q"] if r["q"] is not None else r["hist_q"]
            if q is not None:
                qs[r["m"]["market_id"]] = float(q)
        m0 = rs[0]["m"]
        out.append(
            {
                "event_id": eid,
                "month": month_key(int(m0["T"])),
                "neg_risk": bool(m0.get("neg_risk")),
                "category": m0.get("category"),
                "qs": qs,
                "tail": tail,
            }
        )
    return out


def stage0(cache: Path, out: str, n_sim: int) -> None:
    P: list[str] = []

    def p(s: str = "") -> None:
        print(s, flush=True)
        P.append(s)

    t = time.perf_counter()
    doc = json.loads((cache / "long_markets.json").read_text())
    ms = doc["markets"]
    p("stage0 (scripts/verify_pm_longdated_tail_no.py): gates G1-G3, outcomes not opened")
    p(f"universe filter (markets): {doc['why']} of {doc['n_streamed']} classified-ok markets")
    ev_all = {m["event_id"] for m in ms}
    p(f"long non-sport Yes/No markets open at some t0: {len(ms)} in {len(ev_all)} events")
    p(
        f"slug_dated share: {sum(1 for m in ms if m.get('slug_dated')) / max(1, len(ms)):.3f}; neg_risk share: "
        f"{sum(1 for m in ms if m.get('neg_risk')) / max(1, len(ms)):.3f}"
    )
    ev30 = {m["event_id"] for m in ms if m["eligible"][str(H_MAIN)]}
    p(f"G1 upper bound before any price: events open at T_sched - {H_MAIN} d = {len(ev30)} (gate {G1_MIN})")
    fetched_ev: dict[str, bool] = {}
    for m in ms:
        if m["eligible"][str(H_MAIN)]:
            fetched_ev[m["event_id"]] = fetched_ev.get(m["event_id"], True) and qpath(cache, m["market_id"], H_MAIN).exists()
    done_ev = {e for e, ok in fetched_ev.items() if ok}
    scale = len(ev30) / max(1, len(done_ev & ev30))
    if len(done_ev) < len(fetched_ev):
        p(
            f"SAMPLE: {len(done_ev)} of {len(fetched_ev)} events fully fetched; counts below are for the sample, "
            f"x{scale:.2f} = projection to all events open at T-{H_MAIN}"
        )
        ms = [m for m in ms if m["event_id"] in done_ev]
    p("")
    gate: dict[str, Any] = {}
    for h in (H_MAIN, 90, 7):
        rows = build_rows(cache, ms, h)
        if not rows:
            continue
        n_el = len(rows)
        n_hf = sum(r["hist_fetched"] for r in rows)
        n_hq = sum(r["hist_q"] is not None for r in rows)
        pre = [r for r in rows if r["hist_q"] is not None and PRESCREEN[0] <= r["hist_q"] <= PRESCREEN[1]]
        n_tf = sum(r["trades_fetched"] for r in pre)
        n_fresh = sum(r["trade"] is not None for r in pre)
        n_tail = sum(in_tail(r["q"]) for r in rows)
        p(f"## h = T_sched - {h} d")
        p(
            f"eligible markets {n_el} (events {len({r['m']['event_id'] for r in rows})}); history fetched {n_hf}, "
            f"history quote at t0 {n_hq} -> coverage {n_hq / max(1, n_el):.3f}"
        )
        if n_tf == 0:
            p("  trades not fetched for this horizon (only h = 30 is needed for the gates)")
            p("")
            continue
        p(
            f"prescreen {PRESCREEN} {len(pre)} markets; trades fetched {n_tf}; fresh print <= {FRESH_D:.0f} d: {n_fresh} "
            f"({n_fresh / max(1, n_tf):.3f}); fresh tail q in [0.03, 0.30]: {n_tail} markets"
        )
        ages = sorted(r["trade"]["age"] / DAY_S for r in pre if r["trade"])
        if ages:
            p(f"  fresh print age d: median {statistics.median(ages):.2f}, p90 {ages[int(0.9 * (len(ages) - 1))]:.2f}")
        bk = Counter(tail_bucket(r["q"]) for r in rows if in_tail(r["q"]))
        p(f"  tail markets by bucket: {dict(sorted(bk.items()))}")
        evs = gate_events(rows)
        evs_sd = gate_events(rows, slug_dated_only=True)
        per_ev = [len(e["tail"]) for e in evs]
        p(
            f"events with >= 1 fresh tail market: {len(evs)} (slug-dated only {len(evs_sd)}); neg-risk events "
            f"{sum(e['neg_risk'] for e in evs)}; tail markets per event: median {statistics.median(per_ev) if per_ev else 0}, "
            f"max {max(per_ev) if per_ev else 0}"
        )
        p(f"  by category: {dict(Counter(e['category'] for e in evs).most_common())}")
        mc = Counter(e["month"] for e in evs)
        p(f"  by month of T_sched: {dict(sorted(mc.items()))}")
        if h != H_MAIN:
            p("")
            continue
        pw = null_power(evs, n_sim=n_sim)
        sizes = list(mc.values())
        p(f"null power ({n_sim} simulations, y ~ Bernoulli(q), one winner per neg-risk event; no outcomes read):")
        if pw["sd_event"] is None:
            p("  too few events")
            gate = {"G1": len(evs), "G2": n_hq / max(1, n_el), "G3": None}
            p("")
            continue
        p(
            f"  n events {pw['n_events']}, month clusters {pw['n_clusters']}; SD of event gap {pw['sd_event']:.4f}; "
            f"SE sim {pw['se_sim']:.4f}, SE cluster bootstrap {pw['se_cluster']:.4f}"
        )
        p(f"  n_eff (sim) {pw['n_eff_sim']:.0f}, n_eff (month-cluster bootstrap) {pw['n_eff_cluster']:.0f}")
        mde_main = mde_from_se(max(pw["se_sim"], pw["se_cluster"]))
        p(f"  MDE (2.8 x max SE) {mde_main * 100:.2f} pp; naive 2.8 x SD/sqrt(n) {mde(pw['sd_event'], pw['n_events']) * 100:.2f} pp")
        for rho in (0.005, 0.02):
            de = design_effect(sizes, rho)
            p(
                f"  sensitivity ICC(month) rho={rho}: design effect {de:.2f}, n_eff {pw['n_events'] / de:.0f}, "
                f"MDE {mde_main * de**0.5 * 100:.2f} pp"
            )
        gate = {"G1": len(evs), "G1_slug": len(evs_sd), "G2": n_hq / max(1, n_el), "G3": mde_main, "n_eff": pw["n_eff_cluster"]}
        if scale > 1.0001:
            proj_n = len(evs) * scale
            gate["G1_proj"] = proj_n
            gate["G3_proj"] = mde_main / scale**0.5
            p(f"  projection to all events: G1 ~ {proj_n:.0f} events, MDE ~ {gate['G3_proj'] * 100:.2f} pp (SE / sqrt(scale))")
        p("")
    p("## Gates (pre-registered in the card: G1 >= 3000 events after the freshness filter, G2 >= 0.70, G3 MDE <= 2 pp)")
    if len(ev30) < G1_MIN:
        p(f"G1 FAIL structurally: only {len(ev30)} events are open at T-{H_MAIN} before any price or freshness filter")
    if gate and "G1_proj" in gate:
        gate = {**gate, "G1": gate["G1_proj"], "G3": gate["G3_proj"]}
        p("(gate lines use the projections from the event sample)")
    if gate:
        g1 = gate["G1"] >= G1_MIN
        g2 = gate["G2"] >= G2_MIN
        g3 = gate["G3"] is not None and gate["G3"] <= G3_MAX
        p(f"G1 events {gate['G1']:.0f} (>= {G1_MIN}): {'PASS' if g1 else 'FAIL'}")
        p(f"G2 coverage {gate['G2']:.3f} (>= {G2_MIN}): {'PASS' if g2 else 'FAIL'}")
        g3s = f"{gate['G3'] * 100:.2f} pp" if gate["G3"] is not None else "n/a"
        p(f"G3 MDE {g3s} (<= {G3_MAX * 100:.0f} pp): {'PASS' if g3 else 'FAIL'}")
        p(f"overall: {'PASS -> Step 1' if g1 and g2 and g3 else 'FAIL -> inconclusive, no means computed'}")
    p(f"[{time.perf_counter() - t:.0f}s, peak rss {rss_mb():.0f} MB]")
    if out:
        Path(out).expanduser().parent.mkdir(parents=True, exist_ok=True)
        Path(out).expanduser().write_text("\n".join(P) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["universe", "fetch", "trades", "probe_empty", "stage0"])
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--src-cache", default=str(SRC_CACHE))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--rate", type=float, default=5.0)
    ap.add_argument("--h", type=int, nargs="*", default=[H_MAIN])
    ap.add_argument("--n-sim", type=int, default=400)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    c = Path(a.cache_dir).expanduser()
    c.mkdir(parents=True, exist_ok=True)
    if a.stage == "universe":
        stage_universe(c, Path(a.src_cache).expanduser())
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(c, a.limit, a.rate, tuple(a.h)))
    elif a.stage == "trades":
        asyncio.run(stage_trades(c, a.rate, tuple(a.h)))
    elif a.stage == "probe_empty":
        asyncio.run(stage_probe_empty(c, a.rate, a.limit or 200))
    elif a.stage == "stage0":
        stage0(c, a.out, a.n_sim)


if __name__ == "__main__":
    main()
