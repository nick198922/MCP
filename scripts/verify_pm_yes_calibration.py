"""Polymarket YES calibration across categories (IDEA-20261015-polymarket-yes-calibration-all-categories).

The pre-registration is in the idea card ("Попередня реєстрація"). Network only through the runtime providers
(``rt.polymarket`` Gamma / CLOB / data-api Http clients); per-file JSON cache under ``--cache-dir``
(default ``~/.cache/tradedesk-agents/pm_yes_calibration``).

Usage:
    uv run python scripts/verify_pm_yes_calibration.py --stage list      # Gamma closed events by month (metadata)
    uv run python scripts/verify_pm_yes_calibration.py --stage classify  # exclusion counts (no prices)
    uv run python scripts/verify_pm_yes_calibration.py --stage fetch --limit 200   # timing sample + estimate
    uv run python scripts/verify_pm_yes_calibration.py --stage fetch     # quotes at T-24h..T-4h (long; sample rule)
    uv run python scripts/verify_pm_yes_calibration.py --stage trades    # last trades for q in [0.35; 0.65]
    uv run python scripts/verify_pm_yes_calibration.py --stage stage0    # coverage, freshness, SD, MDE gates (no means)
    uv run python scripts/verify_pm_yes_calibration.py --stage full      # only if the gates pass
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.pm_calibration import (
    HOUR_S,
    basket,
    bootstrap_ci,
    classify_market,
    cluster_means,
    devig_event,
    eligible_at,
    json_list,
    mde,
    no_return,
    reliability,
    resolved_outcome,
    stratified_event_sample,
)
from tradedesk.analytics.pm_threshold import last_trade_before, quote_at
from tradedesk.core.errors import ProviderError

DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/pm_yes_calibration").expanduser()
CARD = Path(__file__).resolve().parent.parent / "docs/ideas/IDEA-20261015-polymarket-yes-calibration-all-categories.md"
PERIOD = ("2025-01", "2026-10")  # endDate months of events, inclusive (last day 2026-10-14)
T_MIN = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
T_MAX = int(datetime(2026, 10, 15, tzinfo=UTC).timestamp())
H_LIST = (4, 12, 24)
H_MAIN = 4
TRADE_BAND = (0.35, 0.65)
STALE_S = 2 * HOUR_S
BUDGET_H = 6.0
POINTS_KEEP_S = 1800  # cache points in [t0 - 30 min, t0] per h

EVENT_KEYS = (
    "id slug title endDate closedTime startDate createdAt enableNegRisk negRiskAugmented negRisk category seriesSlug volume"
).split()
MARKET_KEYS = (
    "id question slug conditionId clobTokenIds outcomes outcomePrices endDate endDateIso closedTime startDate createdAt "
    "acceptingOrdersTimestamp gameStartTime umaEndDate eventStartTime volumeNum feesEnabled feeSchedule feeType negRisk "
    "negRiskOther negRiskMarketID umaResolutionStatuses umaResolutionStatus hasReviewedDates sportsMarketType marketType "
    "category enableOrderBook groupItemTitle automaticallyResolved active closed"
).split()


def slim_event(ev: dict[str, Any]) -> dict[str, Any]:
    out = {k: ev.get(k) for k in EVENT_KEYS if k in ev}
    out["tags"] = [str(t.get("slug")) for t in ev.get("tags") or [] if isinstance(t, dict)]
    out["series"] = [
        {"id": s.get("id"), "slug": s.get("slug"), "recurrence": s.get("recurrence")} for s in ev.get("series") or [] if isinstance(s, dict)
    ]
    out["markets"] = [{k: m.get(k) for k in MARKET_KEYS if k in m} for m in ev.get("markets") or [] if isinstance(m, dict)]
    return out


def months(a: str, b: str) -> list[str]:
    y, m = map(int, a.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _next_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}"


_NO_RETRY_REASONS = frozenset({"config", "auth", "provider_tier", "data_unavailable"})


def _is_permanent(e: Exception) -> bool:
    """True for errors a retry cannot fix: caller/config errors and any HTTP 4xx except 429."""
    if not isinstance(e, ProviderError):
        return False
    if e.reason in _NO_RETRY_REASONS:
        return True
    m = re.match(r"HTTP (\d{3})\b", e.detail or "")
    return bool(m) and 400 <= int(m.group(1)) < 500 and m.group(1) != "429"


async def _retry(coro_fn, what: str, tries: int = 5):  # noqa: ANN001, ANN202
    for k in range(tries):
        try:
            return await coro_fn()
        except Exception as e:  # noqa: BLE001
            if _is_permanent(e):
                raise
            print(f"  retry {what} after {type(e).__name__}: {str(e)[:120]}", flush=True)
            await asyncio.sleep(5 * (k + 1))
    raise RuntimeError(f"giving up on {what}")


def _write(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


def _needs_fetch(p: Path) -> bool:
    """Missing cache file, or a small file holding a transient error (timeouts after retries are fetched again)."""
    if not p.exists():
        return True
    if p.stat().st_size > 600:
        return False
    try:
        return "error" in json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return True


def registration_hash() -> str:
    """SHA-256 of the card's "Попередня реєстрація" section (logged before the first price request)."""
    text = CARD.read_text()
    a = text.index("## Попередня реєстрація")
    b = text.index("## Вердикт", a)
    return hashlib.sha256(text[a:b].encode()).hexdigest()


# ----------------------------------------------------------------------------- stage: list (metadata, no prices)


async def stage_list(cache: Path) -> None:
    from tradedesk.server import build_server

    edir = cache / "events"
    edir.mkdir(parents=True, exist_ok=True)
    # Months not yet listed are split into weekly windows (deep keyset cursors on 100k-event months got HTTP 500).
    todo: list[tuple[str, str, str, str]] = []
    for ym in months(*PERIOD):
        if (edir / f"{ym}.json").exists():
            continue
        end = "2026-10-15T00:00:00Z" if ym == PERIOD[1] else f"{_next_month(ym)}-01T00:00:00Z"
        cuts = [f"{ym}-01T00:00:00Z", f"{ym}-08T00:00:00Z", f"{ym}-15T00:00:00Z", f"{ym}-22T00:00:00Z", end]
        for k in range(4):
            if cuts[k] < cuts[k + 1] and not (edir / f"{ym}-p{k}.json").exists():
                todo.append((ym, f"{ym}-p{k}", cuts[k], cuts[k + 1]))
    print(f"list: {len(todo)} windows to fetch", flush=True)
    _, rt = build_server()
    sem = asyncio.Semaphore(3)

    async def one(ym: str, name: str, lo: str, hi: str) -> None:
        t = time.perf_counter()
        rows: dict[str, Any] = {}
        cursor: str | None = None
        pages = 0
        complete = False
        async with sem:
            for _ in range(5000):
                params: dict[str, Any] = {"closed": "true", "limit": 100, "end_date_min": lo, "end_date_max": hi}
                if cursor:
                    params["after_cursor"] = cursor
                try:
                    f = await _retry(
                        lambda params=params: rt.polymarket.gamma.get_json(
                            "/events/keyset", params=params, ttl=0, tool="events-keyset", args=params
                        ),
                        f"keyset {name}",
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"  {name}: FAILED after {pages} pages: {str(e)[:120]} (window not written)", flush=True)
                    return
                pages += 1
                evs = f.data.get("events") or []
                for ev in evs:
                    if isinstance(ev, dict) and ev.get("id") is not None:
                        rows[str(ev["id"])] = slim_event(ev)
                cursor = f.data.get("next_cursor") or None
                if not cursor or not evs:
                    complete = True
                    break
        _write(edir / f"{name}.json", {"month": ym, "window": [lo, hi], "complete": complete, "pages": pages, "rows": list(rows.values())})
        n_m = sum(len(e["markets"]) for e in rows.values())
        print(f"  {name}: events={len(rows)} markets={n_m} pages={pages} complete={complete} {time.perf_counter() - t:.0f}s", flush=True)

    try:
        await asyncio.gather(*(one(*w) for w in todo))
    finally:
        await rt.aclose()
    print("list done", flush=True)


# ----------------------------------------------------------------------------- universe


def build_universe(cache: Path) -> dict[str, Any]:
    """Stream the event files once (they reach 0.6 GB each), classify every market before prices, cache the result.

    Returns {"meta": [...per file], "why": {status: n}, "markets": [ok markets], "events": {id: {"neg", "n"}},
    "yes_share_all": [yes, n] over every resolved Yes/No market (automated included), "n_events": int}.
    """
    files = sorted((cache / "events").glob("20*.json"))
    up = cache / "universe.json"
    sig = [[p.name, p.stat().st_size] for p in files]
    if up.exists():
        u = json.loads(up.read_text())
        if u.get("sig") == sig:
            return u
    meta: list[dict[str, Any]] = []
    why: Counter = Counter()
    out: list[dict[str, Any]] = []
    ev_meta: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    y_all = n_all = 0
    for p in files:
        d = json.loads(p.read_text())
        meta.append({"file": p.stem, "complete": d["complete"], "pages": d["pages"], "events": len(d["rows"])})
        for ev in d["rows"]:
            eid = str(ev["id"])
            if eid in seen:
                continue
            seen.add(eid)
            any_ok = False
            for m in ev.get("markets") or []:
                outs = json_list(m.get("outcomes"))
                if outs and [str(x).lower() for x in outs] == ["yes", "no"]:
                    y = resolved_outcome(m.get("outcomePrices"))
                    if y is not None:
                        n_all += 1
                        y_all += y
                c = classify_market(ev, m)
                if c["status"] == "ok" and not (T_MIN <= c["T"] < T_MAX):
                    c = {**c, "status": "out_of_period"}
                why[c["status"]] += 1
                if c["status"] == "ok":
                    out.append(c)
                    any_ok = True
            if any_ok:
                ev_meta[eid] = {"neg": bool(ev.get("enableNegRisk")), "n": len(ev.get("markets") or [])}
        del d
    out.sort(key=lambda r: (r["T"], r["event_id"], r["market_id"]))
    u = {
        "sig": sig,
        "meta": meta,
        "why": dict(why),
        "markets": out,
        "events": ev_meta,
        "yes_share_all": [y_all, n_all],
        "n_events": len(seen),
    }
    _write(up, u)
    return u


def load_markets(cache: Path) -> tuple[list[dict[str, Any]], Counter, dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Classified markets in the period (checks before prices). Returns (ok markets, status counts, event meta, file meta)."""
    u = build_universe(cache)
    return u["markets"], Counter(u["why"]), u["events"], u["meta"]


def t0_of(m: dict[str, Any], h: int) -> int:
    return int(m["T"]) - h * HOUR_S


def needs_quotes(m: dict[str, Any]) -> bool:
    return any(eligible_at(m, t0_of(m, h)) for h in H_LIST)


def stratum(m: dict[str, Any]) -> str:
    return f"{m['category']}|{datetime.fromtimestamp(m['T'], UTC):%Y-%m}"


def qpath(cache: Path, mid: str) -> Path:
    return cache / "quotes" / mid[-2:] / f"{mid}.json"


def tpath(cache: Path, mid: str, h: int) -> Path:
    return cache / "trades" / mid[-2:] / f"{mid}_{h}.json"


def sample_ids(cache: Path) -> set[str] | None:
    p = cache / "sample.json"
    return set(json.loads(p.read_text())["events"]) if p.exists() else None


def stage_classify(cache: Path) -> None:
    ms, why, events, meta = load_markets(cache)
    print("months", len(meta), "all complete", all(x["complete"] for x in meta))
    print("events", len(events), "markets", sum(why.values()), dict(why.most_common()))
    print("ok markets", len(ms), "yes/no", sum(1 for m in ms if m["yes_no"]))
    print("by category (yes/no):", Counter(m["category"] for m in ms if m["yes_no"]).most_common())
    print("by year (yes/no):", sorted(Counter(m["year"] for m in ms if m["yes_no"]).items()))
    print("anchor (yes/no):", Counter(m["anchor"] for m in ms if m["yes_no"]))
    print("fees:", Counter((m["fees_enabled"], m["fee_rate"], m["fee_exp"]) for m in ms).most_common(6))
    yn = [m for m in ms if m["yes_no"] and needs_quotes(m)]
    print("yes/no markets eligible at some h:", len(yn), "events:", len({m["event_id"] for m in yn}))
    oth = [m for m in ms if not m["yes_no"] and needs_quotes(m)]
    print("other binaries eligible at some h:", len(oth), "events:", len({m["event_id"] for m in oth}))


# ----------------------------------------------------------------------------- stage: fetch quotes


async def stage_fetch(cache: Path, limit: int | None, rate: float, include_named: bool) -> None:
    from tradedesk.core.ratelimit import TokenBucket
    from tradedesk.server import build_server

    h = registration_hash()
    print(f"registration sha256={h}", flush=True)
    (cache / "registration.sha256").write_text(h + "\n")
    ms, _, _, _ = load_markets(cache)
    pool = [m for m in ms if (m["yes_no"] or include_named) and needs_quotes(m)]
    if limit:
        rng = random.Random(7)
        pool = rng.sample(pool, min(limit, len(pool)))
    else:
        sel = sample_ids(cache)
        if sel is not None:
            pool = [m for m in pool if m["event_id"] in sel]
    todo = [m for m in pool if _needs_fetch(qpath(cache, m["market_id"]))]
    print(f"quotes: pool={len(pool)} to fetch={len(todo)} rate={rate}/s", flush=True)
    _, rt = build_server()
    rt.polymarket.clob._limiter = TokenBucket(rate, int(2 * rate))  # pacing for /prices-history (default 5/s)
    sem = asyncio.Semaphore(max(4, int(2 * rate)))
    t_start = time.perf_counter()
    done = errs = 0

    async def one(m: dict[str, Any]) -> None:
        nonlocal done, errs
        T = int(m["T"])
        lo, hi = T - 24 * HOUR_S - POINTS_KEEP_S, T - H_MAIN * HOUR_S
        params = {"market": m["yes_token"], "startTs": lo, "endTs": hi, "fidelity": 1}
        try:
            async with sem:
                f = await _retry(
                    lambda: rt.polymarket.clob.get_json("/prices-history", params=params, ttl=0, tool="prices-history", args=params), "hist"
                )
            hist = (f.data or {}).get("history") or [] if isinstance(f.data, dict) else []
            pts = sorted((int(p["t"]), float(p["p"])) for p in hist if isinstance(p, dict) and "t" in p and "p" in p)
            steps = [b[0] - a[0] for a, b in zip(pts, pts[1:], strict=False)]
            keep = {}
            for hh in H_LIST:
                t0 = T - hh * HOUR_S
                keep[str(hh)] = [{"t": t, "p": p} for t, p in pts if t0 - POINTS_KEEP_S <= t <= t0]
            obj = {"n": len(pts), "step": statistics.median(steps) if steps else None, "first": pts[0][0] if pts else None, "pts": keep}
        except Exception as e:  # noqa: BLE001
            errs += 1
            obj = {"error": f"{type(e).__name__}: {str(e)[:200]}", "pts": {}}
        _write(qpath(cache, m["market_id"]), obj)
        done += 1
        if done == min(100, len(todo)):
            el = time.perf_counter() - t_start
            print(f"  sample: {done} in {el:.1f}s -> {done / el:.1f}/s", flush=True)
        if done % 5000 == 0:
            el = time.perf_counter() - t_start
            print(f"  quotes {done}/{len(todo)} {el:.0f}s ({done / el:.1f}/s, errors {errs})", flush=True)

    try:
        await asyncio.gather(*(one(m) for m in todo))
    finally:
        await rt.aclose()
    el = time.perf_counter() - t_start
    print(f"quotes done {done} in {el:.0f}s ({done / max(el, 1e-9):.1f}/s) errors={errs}", flush=True)
    if limit:
        all_yn = [m for m in ms if m["yes_no"] and needs_quotes(m)]
        rate_obs = done / max(el, 1e-9)
        est_h = len(all_yn) / rate_obs / 3600
        print(f"estimate for all {len(all_yn)} yes/no markets: {est_h:.1f} h at {rate_obs:.1f}/s (budget {BUDGET_H} h)", flush=True)
        if not (cache / "sample.json").exists():
            frac = min(1.0, BUDGET_H / est_h)
            strata = {m["event_id"]: stratum(m) for m in all_yn}
            sel = stratified_event_sample(strata, frac)
            n_m = sum(1 for m in all_yn if m["event_id"] in sel)
            _write(cache / "sample.json", {"frac": frac, "rate": rate_obs, "est_h": est_h, "events": sorted(sel)})
            print(f"sample rule: frac={frac:.3f} events={len(sel)}/{len(strata)} markets={n_m}", flush=True)
    print("fetch done", flush=True)


def load_quotes(cache: Path, markets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """market_id -> {"fetched", "error", "step", "q": {h: (q, t) | None}} (q via quote_at, no lookahead)."""
    out: dict[str, dict[str, Any]] = {}
    for m in markets:
        p = qpath(cache, m["market_id"])
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        qs = {}
        for h in H_LIST:
            qs[h] = quote_at(d.get("pts", {}).get(str(h), []), t0_of(m, h))
        out[m["market_id"]] = {"error": d.get("error"), "step": d.get("step"), "n": d.get("n"), "q": qs}
    return out


# ----------------------------------------------------------------------------- stage: trades (freshness + H2 entry)


async def stage_trades(cache: Path, rate: float, include_named: bool) -> None:
    from tradedesk.core.ratelimit import TokenBucket
    from tradedesk.server import build_server

    ms, _, _, _ = load_markets(cache)
    sel = sample_ids(cache)
    pool = [m for m in ms if (m["yes_no"] or include_named) and (sel is None or m["event_id"] in sel)]
    qs = load_quotes(cache, pool)
    todo = []
    for m in pool:
        qd = qs.get(m["market_id"])
        if not qd:
            continue
        for h in H_LIST:
            qa = qd["q"].get(h)
            if (
                qa
                and TRADE_BAND[0] <= qa[0] <= TRADE_BAND[1]
                and eligible_at(m, t0_of(m, h))
                and _needs_fetch(tpath(cache, m["market_id"], h))
            ):
                todo.append((m, h))
    print(f"trades: {len(todo)} (market, h) to fetch rate={rate}/s", flush=True)
    _, rt = build_server()
    rt.polymarket.data._limiter = TokenBucket(rate, int(2 * rate))
    sem = asyncio.Semaphore(max(4, int(2 * rate)))
    t_start = time.perf_counter()
    done = errs = 0

    async def one(m: dict[str, Any], h: int) -> None:
        nonlocal done, errs
        t0 = t0_of(m, h)
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
            print(f"  trades {done}/{len(todo)} {time.perf_counter() - t_start:.0f}s errors {errs}", flush=True)

    try:
        await asyncio.gather(*(one(m, h) for m, h in todo))
    finally:
        await rt.aclose()
    print(f"trades done {done} in {time.perf_counter() - t_start:.0f}s errors={errs}", flush=True)


# ----------------------------------------------------------------------------- rows


def build_rows(cache: Path, markets: list[dict[str, Any]], qs: dict[str, dict[str, Any]], h: int) -> list[dict[str, Any]]:
    """One row per market eligible at t0 = T - h (state at t0 only): q, last trade, freshness inputs."""
    rows = []
    for m in markets:
        t0 = t0_of(m, h)
        if not eligible_at(m, t0):
            continue
        r = dict(m)
        r["h"] = h
        r["t0"] = t0
        qd = qs.get(m["market_id"])
        r["fetched"] = qd is not None
        qa = qd["q"].get(h) if qd else None
        r["q"] = qa[0] if qa else None
        tp = tpath(cache, m["market_id"], h)
        if tp.exists():
            lt = last_trade_before(json.loads(tp.read_text()).get("trades") or [], t0, m["yes_token"])
            r["trades_fetched"] = True
        else:
            lt = None
            r["trades_fetched"] = False
        r["lt"] = lt
        rows.append(r)
    return rows


def fresh(r: dict[str, Any], stale_s: int = STALE_S) -> bool:
    return r["lt"] is not None and r["lt"]["age"] <= stale_s


def date_key(r: dict[str, Any]) -> str:
    return datetime.fromtimestamp(r["T"], UTC).strftime("%Y-%m-%d")


def center(rows: list[dict[str, Any]], stale_s: int | None = STALE_S, years: set[int] | None = None) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if r["q"] is None or not basket(r["q"]):
            continue
        if stale_s is not None and not fresh(r, stale_s):
            continue
        if years is not None and r["year"] not in years:
            continue
        out.append(r)
    return out


def coverage(rows: list[dict[str, Any]]) -> tuple[int, int, float]:
    n = len(rows)
    k = sum(1 for r in rows if r["q"] is not None)
    return n, k, k / n if n else 0.0


def ok_years(rows: list[dict[str, Any]]) -> set[int]:
    by = defaultdict(list)
    for r in rows:
        by[r["year"]].append(r)
    return {y for y, rs in by.items() if coverage(rs)[2] >= 0.70}


# ----------------------------------------------------------------------------- stage 0 (no means)


def stage0(cache: Path, out: str) -> dict[str, Any]:
    u = build_universe(cache)
    ms, why, meta = u["markets"], Counter(u["why"]), u["meta"]
    sel = sample_ids(cache)
    samp = json.loads((cache / "sample.json").read_text()) if (cache / "sample.json").exists() else {}
    yn = [m for m in ms if m["yes_no"] and (sel is None or m["event_id"] in sel)]
    qs = load_quotes(cache, yn)
    L: list[str] = []
    P = L.append
    P("## Стадія 0 (без середніх)\n")
    P(
        f"Перелік: {len(meta)} файлів-вікон endDate {meta[0]['file']}…{meta[-1]['file']}, усі курсори до кінця: {all(x['complete'] for x in meta)}; "
        f"подій {u['n_events']}, ринків {sum(why.values())}, сторінок {sum(x['pages'] for x in meta)}.\n"
    )
    P("| Класифікація (до цін) | ринків |\n|---|---|")
    for k, v in why.most_common():
        P(f"| {k} | {v} |")
    # consistency check against the accuracy page: YES share of all resolved Yes/No markets (incl. automated)
    y_all, n_all = u["yes_share_all"]
    all_ok = [m for m in ms if m["yes_no"]]
    P(
        f"\n**Звірка з polymarket.com/accuracy (21.9 % YES):** усі розв'язані Yes/No ринки переліку (зокрема автоматичні): "
        f"{y_all}/{n_all} = {y_all / max(n_all, 1):.1%}; наш універс Yes/No після виключень: "
        f"{sum(m['outcome'] for m in all_ok)}/{len(all_ok)} = {sum(m['outcome'] for m in all_ok) / max(len(all_ok), 1):.1%}.\n"
    )
    P(
        f"Вибірка подій (правило бюджету): частка {samp.get('frac', 1.0):.3f}, оцінка повного fetch {samp.get('est_h', 0):.1f} год "
        f"при {samp.get('rate', 0):.1f} зап./с; Yes/No ринків у вибірці {len(yn)}, подій {len({m['event_id'] for m in yn})}.\n"
    )
    steps = [q["step"] for q in qs.values() if q.get("step")]
    errs = Counter((q["error"] or "")[:60] for q in qs.values() if q.get("error"))
    P(
        f"Котирування: файлів {len(qs)}, помилок {sum(errs.values())} {dict(errs.most_common(3))}; медіанний крок точок {statistics.median(steps) if steps else None} с "
        f"(частка ринків із кроком ≤ 60 с: {sum(1 for s in steps if s <= 60) / max(len(steps), 1):.1%}).\n"
    )
    res: dict[str, Any] = {}
    for h in H_LIST:
        rows = build_rows(cache, yn, qs, h)
        P(f"### h = {h} год\n")
        P("| Рік | придатних на t0 | є q | покриття |\n|---|---|---|---|")
        for y in sorted({r["year"] for r in rows}):
            n, k, c = coverage([r for r in rows if r["year"] == y])
            P(f"| {y} | {n} | {k} | {c:.1%} |")
        n, k, c = coverage(rows)
        P(f"| усі | {n} | {k} | {c:.1%} |")
        yrs = ok_years(rows)
        allc = center(rows, stale_s=None)
        P(
            f"\nЦентр q ∈ [0.40; 0.60]: ринків {len(allc)}, подій {len({r['event_id'] for r in allc})}; з угодами {sum(1 for r in allc if r['trades_fetched'])}."
        )
        P("| Давність останньої угоди | ринків | частка | подій |\n|---|---|---|---|")
        for s in (1800, STALE_S, 6 * HOUR_S):
            cs = center(rows, stale_s=s)
            P(f"| ≤ {s // 60} хв | {len(cs)} | {len(cs) / max(len(allc), 1):.0%} | {len({r['event_id'] for r in cs})} |")
        main = center(rows, years=yrs)
        vals = cluster_means((r["event_id"], r["q"] - r["outcome"]) for r in main)
        sd_e = statistics.stdev(vals) if len(vals) > 1 else float("nan")
        m_ = mde(sd_e, len(vals))
        P(
            f"\nГоловна вибірка (давність ≤ 2 год, роки з покриттям ≥ 70 %: {sorted(yrs)}): ринків {len(main)}, подій {len(vals)}; "
            f"SD значень подій {sd_e:.3f}; MDE = 2.8·SD/√n = {m_ * 100:.2f} п.п.\n"
        )
        res[h] = {"events": len(vals), "cov": c, "mde": m_}
    g = res[H_MAIN]
    gates = {
        "G1 подій у центрі ≥ 2 000 (h = 4)": (g["events"] >= 2000, str(g["events"])),
        "G2 покриття q ≥ 70 % (h = 4)": (g["cov"] >= 0.70, f"{g['cov']:.1%}"),
        "G3 MDE ≤ 3 п.п. (h = 4)": (g["mde"] <= 0.03, f"{g['mde'] * 100:.2f} п.п."),
    }
    P("### Ворота\n")
    P("| Ворота | Значення | Пройдено |\n|---|---|---|")
    for k, (ok, v) in gates.items():
        P(f"| {k} | {v} | {'так' if ok else 'ні'} |")
    report = "\n".join(L)
    print(report, flush=True)
    if out:
        Path(out).expanduser().write_text(report + "\n")
    return {k: ok for k, (ok, _) in gates.items()}


# ----------------------------------------------------------------------------- full (only after the gates)


def _fmt(label: str, pairs: list[tuple[str, float]], k: float = 100.0, unit: str = "п.п.") -> str:
    vals = cluster_means(pairs)
    b = bootstrap_ci(vals)
    n_m = len(pairs)
    if not vals:
        return f"| {label} | 0 | 0 | — | — |"
    lo = f"{b['lo'] * k:+.2f}" if b["lo"] is not None else "—"
    hi = f"{b['hi'] * k:+.2f}" if b["hi"] is not None else "—"
    return f"| {label} | {n_m} | {len(vals)} | {b['mean'] * k:+.2f} | [{lo}; {hi}] |"


def _gap(rs: list[dict[str, Any]], key: str = "event_id") -> list[tuple[str, float]]:
    return [(r[key] if key != "date" else date_key(r), r["q"] - r["outcome"]) for r in rs]


def _h2(rs: list[dict[str, Any]], half_spread: float = 0.01, fee_all: float | None = None) -> list[tuple[str, float]]:
    out = []
    for r in rs:
        rate, exp = (fee_all, 1.0) if fee_all is not None else (r["fee_rate"], r["fee_exp"])
        x = no_return(
            r["q"], r["outcome"], half_spread=half_spread, last_no_px=r["lt"]["no_px"] if r["lt"] else None, fee_rate=rate, fee_exp=exp
        )
        out.append((r["event_id"], x["ret"]))
    return out


def full(cache: Path, out: str) -> None:
    g = stage0(cache, "")
    if not all(g.values()):
        print("\nG1-G3 not passed: no means are computed (pre-registered). Verdict: inconclusive.", flush=True)
        return
    ms, _, events, _ = load_markets(cache)
    sel = sample_ids(cache)
    yn = [m for m in ms if m["yes_no"] and (sel is None or m["event_id"] in sel)]
    qs = load_quotes(cache, yn)
    rows_h = {h: build_rows(cache, yn, qs, h) for h in H_LIST}
    rows = rows_h[H_MAIN]
    yrs = ok_years(rows)
    main = center(rows, years=yrs)
    L: list[str] = ["## Повний прогін (h = 4 год, якщо не сказано інше)\n"]
    P = L.append
    hdr = "| Варіант | ринків | кластерів | середнє | 95 % CI |\n|---|---|---|---|---|"
    P("### H1: розрив q − частота YES у центрі [0.40; 0.60], п.п. (кластер — подія)\n")
    P(hdr)
    P(_fmt("**H1 головний: Yes/No, давність ≤ 2 год**", _gap(main)))
    P(_fmt("той самий, кластер — календарна дата T", _gap(main, "date")))
    P(_fmt("той самий, без кластерів (ринок = одиниця)", [(r["market_id"], r["q"] - r["outcome"]) for r in main]))
    # halves
    ds = sorted(date_key(r) for r in main)
    mid = ds[len(ds) // 2]
    P(_fmt(f"перша половина (T < {mid})", _gap([r for r in main if date_key(r) < mid])))
    P(_fmt(f"друга половина (T ≥ {mid})", _gap([r for r in main if date_key(r) >= mid])))
    cats = Counter(r["category"] for r in main)
    top_cat = cats.most_common(1)[0][0]
    for c, _ in cats.most_common():
        P(_fmt(f"категорія {c}", _gap([r for r in main if r["category"] == c])))
    P(_fmt(f"без найбільшої категорії ({top_cat})", _gap([r for r in main if r["category"] != top_cat])))
    big = Counter(r["event_id"] for r in main)
    top10 = {e for e, _ in big.most_common(10)}
    P(_fmt("без 10 подій з найбільшою кількістю ринків у кошику", _gap([r for r in main if r["event_id"] not in top10])))
    for y in sorted(yrs):
        P(_fmt(f"рік {y}", _gap([r for r in main if r["year"] == y])))
    for lo, hi, lab in ((0, 1e4, "< $10k"), (1e4, 1e5, "$10–100k"), (1e5, 1e18, "> $100k")):
        P(_fmt(f"обсяг {lab} (опис)", _gap([r for r in main if lo <= r["volume"] < hi])))
    P(_fmt("neg-risk події", _gap([r for r in main if r["neg_risk"]])))
    P(_fmt("не neg-risk", _gap([r for r in main if not r["neg_risk"]])))
    P(_fmt("T = gameStartTime", _gap([r for r in main if r["anchor"] == "game_start"])))
    P(_fmt("T = endDate", _gap([r for r in main if r["anchor"] == "end_date"])))
    P(_fmt("лише ринки з датою в slug", _gap([r for r in main if r["slug_dated"]])))
    for s in (1800, 6 * HOUR_S):
        P(_fmt(f"давність ≤ {s // 60} хв", _gap(center(rows, stale_s=s, years=yrs))))
    P(_fmt("без фільтра давності", _gap(center(rows, stale_s=None, years=yrs))))
    for h in H_LIST:
        if h == H_MAIN:
            continue
        rh = rows_h[h]
        P(_fmt(f"h = {h} год", _gap(center(rh, years=ok_years(rh)))))
    rng = random.Random(7)
    null = []
    for r in sorted(main, key=lambda x: x["market_id"]):
        s = 1 if rng.random() < 0.5 else -1
        null.append((r["event_id"], s * (r["q"] - r["outcome"])))
    P(_fmt("нуль: випадкова сторона (seed 7)", null))
    # closed in (t0, T) vs at/after T
    early = [r for r in main if r["closed"] < r["T"]]
    late = [r for r in main if r["closed"] >= r["T"]]
    P(
        f"\nЧастка YES у головній вибірці: закрились у (t0; T) — {sum(r['outcome'] for r in early)}/{len(early)}"
        f" ({sum(r['outcome'] for r in early) / max(len(early), 1):.1%}); закрились ≥ T — {sum(r['outcome'] for r in late)}/{len(late)}"
        f" ({sum(r['outcome'] for r in late) / max(len(late), 1):.1%}). Середнє q: {statistics.mean(r['q'] for r in main):.3f}."
    )

    # H1-devig on neg-risk events (no freshness filter; paired with raw on the same markets)
    P("\n### H1-devig (neg-risk події з ≥ 3 ринками, котирування всіх відкритих варіантів на t0; без фільтра давності), п.п.\n")
    P(hdr)
    by_ev: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in ms:
        by_ev[m["event_id"]].append(m)
    q_all = load_quotes(cache, [m for m in ms if m["event_id"] in {r["event_id"] for r in rows if r["neg_risk"]}])
    raw_p, dv_p, n_ev_skip = [], [], 0
    for eid in sorted({r["event_id"] for r in rows if r["neg_risk"]}):
        ev = events.get(eid) or {}
        if not ev.get("neg"):
            continue
        ems = by_ev.get(eid, [])
        if ev.get("n", 0) < 3 or len(ems) != ev.get("n") or len({m["T"] for m in ems}) != 1:
            n_ev_skip += 1
            continue
        t0 = t0_of(ems[0], H_MAIN)
        open_ = [m for m in ems if m["start"] <= t0 and m["closed"] > t0]
        qq = {}
        for m in open_:
            qa = (q_all.get(m["market_id"]) or {}).get("q", {}).get(H_MAIN)
            if qa is None:
                break
            qq[m["market_id"]] = qa[0]
        if len(qq) != len(open_) or len(qq) < 2:
            n_ev_skip += 1
            continue
        dv = devig_event(qq)
        if dv is None:
            continue
        for m in open_:
            if m["year"] not in yrs or not eligible_at(m, t0):
                continue
            if basket(qq[m["market_id"]]):
                raw_p.append((eid, qq[m["market_id"]] - m["outcome"]))
            if basket(dv[m["market_id"]]):
                dv_p.append((eid, dv[m["market_id"]] - m["outcome"]))
    P(_fmt("сирі q (ті самі події)", raw_p))
    P(_fmt("нормалізовані q' = q / Σq", dv_p))
    P(f"\nПодій пропущено (не всі варіанти класифіковані/котировані або різні T): {n_ev_skip}.")
    for c in ("politics", "sports", "culture"):
        P(_fmt(f"категорія {c} (H1 головний)", _gap([r for r in main if r["category"] == c])))

    # H2
    P("\n### H2: купівля NO у центрі, дохід на капітал, % (кластер — подія)\n")
    P(hdr)
    P(_fmt("**H2 головний: півспреду 0.01, комісія за ринком**", _h2(main), unit="%"))
    for hs in (0.005, 0.02, 0.03):
        P(_fmt(f"півспреду {hs}", _h2(main, half_spread=hs)))
    P(_fmt("комісія 0.0175 на всі ринки", _h2(main, fee_all=0.0175)))
    P(_fmt(f"перша половина (T < {mid})", _h2([r for r in main if date_key(r) < mid])))
    P(_fmt(f"друга половина (T ≥ {mid})", _h2([r for r in main if date_key(r) >= mid])))
    P(_fmt(f"без найбільшої категорії ({top_cat})", _h2([r for r in main if r["category"] != top_cat])))
    lock = [(r["closed"] - r["t0"]) / 3600 for r in main]
    P(
        f"\nБлокування капіталу (closedTime − t0): медіана {statistics.median(lock):.1f} год, p75 {statistics.quantiles(lock, n=4)[2]:.1f} год, "
        f"p90 {statistics.quantiles(lock, n=10)[8]:.1f} год. Частка входів, де остання NO-угода гірша за 1 − q + 0.01: "
        f"{sum(1 for r in main if r['lt'] and r['lt']['no_px'] > 1 - r['q'] + 0.01) / max(len(main), 1):.0%}."
    )
    # reliability curve (descriptive)
    P("\n### Крива калібрування (Yes/No, h = 4, усі ринки з q, без фільтра давності; описово)\n")
    P("| q | ринків | середнє q | частота YES | q − частота, п.п. |\n|---|---|---|---|---|")
    pairs = [(r["q"], r["outcome"]) for r in rows if r["q"] is not None and r["year"] in yrs]
    for b in reliability(pairs, [i / 20 for i in range(21)]):
        if b["n"]:
            P(f"| [{b['lo']:.2f}; {b['hi']:.2f}) | {b['n']} | {b['mean_q']:.3f} | {b['freq']:.3f} | {b['gap'] * 100:+.2f} |")
    # other binaries, descriptive
    named = [m for m in ms if not m["yes_no"] and (sel is None or m["event_id"] in sel)]
    qn = load_quotes(cache, named)
    if qn:
        rn = center(build_rows(cache, named, qn, H_MAIN), stale_s=None, years=yrs)
        P("\n### Інші бінарні (перший варіант як «YES», без фільтра давності; описово)\n")
        P(hdr)
        P(_fmt("перший варіант, центр", _gap(rn)))
    report = "\n".join(L)
    print(report, flush=True)
    if out:
        Path(out).expanduser().write_text(report + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0, help="fetch: random timing sample of N markets (seed 7)")
    ap.add_argument("--rate", type=float, default=10.0, help="requests/s for CLOB (fetch) or data-api (trades)")
    ap.add_argument("--named", action="store_true", help="also fetch non Yes/No binaries (descriptive)")
    ap.add_argument("--log", default="", help="write progress lines to this file instead of the terminal")
    a = ap.parse_args()
    c = Path(a.cache_dir).expanduser()
    c.mkdir(parents=True, exist_ok=True)
    if a.log:
        import logging
        import sys

        fh = open(Path(a.log).expanduser(), "a", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stderr = fh
        logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.stage == "list":
        asyncio.run(stage_list(c))
    elif a.stage == "classify":
        stage_classify(c)
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(c, a.limit or None, a.rate, a.named))
    elif a.stage == "trades":
        asyncio.run(stage_trades(c, a.rate, a.named))
    elif a.stage == "stage0":
        stage0(c, a.out)
    elif a.stage == "full":
        full(c, a.out)
