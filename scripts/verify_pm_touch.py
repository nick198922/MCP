"""Polymarket "What price will Bitcoin/Ethereum hit" touch markets (IDEA-20261015-polymarket-touch-ladder-event-days).

The pre-registration is in the idea card ("Попередня реєстрація"). Network only through the runtime providers
(``rt.polymarket`` Gamma / CLOB / data-api, ``rt.binance_vision``); per-file cache under ``--cache-dir``
(default ``~/.cache/tradedesk-agents/pm_touch``).

Usage:
    uv run python scripts/verify_pm_touch.py --stage register    # sha256 of the pre-registration (before any fetch)
    uv run python scripts/verify_pm_touch.py --stage list        # Gamma listing (tag hit-price + series walk)
    uv run python scripts/verify_pm_touch.py --stage classify    # rules / direction / window checks (no prices)
    uv run python scripts/verify_pm_touch.py --stage klines      # Binance spot 1m archive -> per-day path summaries
    uv run python scripts/verify_pm_touch.py --stage semantics   # /prices-history label vs /trades on >= 10 markets
    uv run python scripts/verify_pm_touch.py --stage fetch       # quotes [listing, 07:30 ET] + last trades at each t0
    uv run python scripts/verify_pm_touch.py --stage stage0      # counts, coverage, SD, MDE gates (no means)
    uv run python scripts/verify_pm_touch.py --stage full        # only if G1-G3 pass
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import re
import resource
import statistics
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from tradedesk.analytics.pm_threshold import bootstrap_mean, day_baskets, last_trade_before, mde, quote_at, realized_vol, value_at
from tradedesk.analytics.pm_touch import (
    WEEKLY_SLUG,
    DayPath,
    buy_return,
    classify_touch_market,
    day_window,
    et_time,
    jump_trade_offsets,
    max_losing_streak,
    parse_daily_slug,
    pick_strangle,
    strangle_return,
    touch_prob,
    touched,
)
from tradedesk.core.errors import ProviderError

DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/pm_touch").expanduser()
CARD = Path(__file__).resolve().parents[1] / "docs/ideas/IDEA-20261015-polymarket-touch-ladder-event-days.md"
EVENTS_CSV = Path(__file__).resolve().parents[1] / "docs/verification/2026-10-15-us-macro-events.csv"
ASSETS = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT"}
T0S = {"0030": (0, 30), "0200": (2, 0), "0730": (7, 30)}  # ET; "0030" is primary (pre-registered)
PRIMARY = "0030"
BASKET = (0.05, 0.25)
CENTER = (0.35, 0.65)
STRANGLE = (0.07, 0.14)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def log(msg: str) -> None:
    print(f"{datetime.now(UTC).strftime('%H:%M:%S')} rss={rss_mb():.0f}MB {msg}", flush=True)


def _write(p: Path, obj: Any) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


_NO_RETRY_REASONS = frozenset({"config", "auth", "provider_tier", "data_unavailable"})


def _is_permanent(e: Exception) -> bool:
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
            log(f"retry {what} after {type(e).__name__}: {str(e)[:120]}")
            await asyncio.sleep(5 * (k + 1))
    raise RuntimeError(f"giving up on {what}")


# ----------------------------------------------------------------------------- register / probe


def stage_register(cache: Path) -> None:
    """Record sha256 of the card's pre-registration section with a UTC timestamp (append-only), before any fetch."""
    text = CARD.read_text()
    sec = text.split("## Попередня реєстрація", 1)[1].split("\n## ", 1)[0]
    h = hashlib.sha256(sec.encode()).hexdigest()
    line = f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={h} bytes={len(sec.encode())}\n"
    with open(cache / "registration.sha256", "a") as fh:
        fh.write(line)
    (cache / f"card_at_registration_{h[:12]}.md").write_text(text)
    print(line, end="")


async def stage_probe(cache: Path, slugs: list[str]) -> None:
    from tradedesk.server import build_server

    pdir = cache / "probe"
    pdir.mkdir(parents=True, exist_ok=True)
    _, rt = build_server()
    try:
        for s in slugs:
            try:
                f = await rt.polymarket.gamma.get_json("/events", params={"slug": s}, ttl=0, tool="probe", args={"slug": s})
            except Exception as e:  # noqa: BLE001
                print(s, "ERR", type(e).__name__, str(e)[:200], flush=True)
                continue
            d = f.data
            print(s, "rows", len(d) if isinstance(d, list) else type(d).__name__, flush=True)
            if isinstance(d, list) and d:
                (pdir / f"{s}.json").write_text(json.dumps(d))
                ev = d[0]
                print(
                    f"  id={ev.get('id')} start={ev.get('startDate')} end={ev.get('endDate')} "
                    f"series={[(x.get('slug'), x.get('id')) for x in ev.get('series') or []]} markets={len(ev.get('markets') or [])}",
                    flush=True,
                )
    finally:
        await rt.aclose()


# ----------------------------------------------------------------------------- list


def slug_kind(slug: str) -> tuple[str, str] | None:
    """(asset, 'daily'|'weekly') for BTC/ETH touch event slugs, None otherwise."""
    p = parse_daily_slug(slug)
    if p:
        return p[0], "daily"
    m = WEEKLY_SLUG.match(slug or "")
    if m:
        return m.group(1), "weekly"
    return None


async def stage_list(cache: Path) -> None:
    from tradedesk.server import build_server

    edir = cache / "events"
    edir.mkdir(parents=True, exist_ok=True)
    _, rt = build_server()
    try:
        t = time.perf_counter()
        f = await rt.polymarket.events_all({"tag_slug": "hit-price", "closed": "true"})
        rows = f.data["rows"]
        log(
            f"tag hit-price: closed events={len(rows)} pages={f.data['pages']} complete={f.data['complete']} {time.perf_counter() - t:.0f}s"
        )
        by_id: dict[str, dict[str, Any]] = {}
        series: dict[str, str] = {}
        other = Counter()
        for ev in rows:
            for s in ev.get("series") or []:
                series[str(s.get("id"))] = str(s.get("slug"))
            if slug_kind(str(ev.get("slug"))):
                by_id[str(ev["id"])] = ev
            else:
                other[re.sub(r"-(on-)?[a-z]+-\d.*$", "-*", str(ev.get("slug")))] += 1
        log(f"  BTC/ETH touch events by slug: {len(by_id)}; other slugs (top 15): {other.most_common(15)}")
        log(f"  series seen: {sorted(series.items())}")
        only_series = 0
        incomplete = []
        for sid, sslug in sorted(series.items()):
            f2 = await rt.polymarket.events_all({"series_id": sid, "closed": "true"})
            n = 0
            for ev in f2.data["rows"]:
                if slug_kind(str(ev.get("slug"))):
                    n += 1
                    if str(ev["id"]) not in by_id:
                        only_series += 1
                        by_id[str(ev["id"])] = ev
            if not f2.data["complete"]:
                incomplete.append(sslug)
            log(f"  series {sslug} id={sid}: closed events={len(f2.data['rows'])} btc/eth touch={n} complete={f2.data['complete']}")
        log(f"  touch events found only via series walk: {only_series}; incomplete listings: {incomplete}")
        per: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for ev in by_id.values():
            per[slug_kind(ev["slug"])].append(ev)  # type: ignore[index]
        for (asset, kind), evs in sorted(per.items()):
            evs.sort(key=lambda e: str(e.get("endDate")))
            _write(edir / f"{asset}_{kind}.json", {"complete": f.data["complete"] and not incomplete, "rows": evs})
            ends = [str(e.get("endDate"))[:10] for e in evs]
            log(f"{asset} {kind}: events={len(evs)} first_end={ends[0]} last_end={ends[-1]}")
    finally:
        await rt.aclose()
    log("list done")


def load_daily(cache: Path) -> tuple[list[dict[str, Any]], Counter]:
    out: list[dict[str, Any]] = []
    why: Counter = Counter()
    for asset in ASSETS:
        p = cache / "events" / f"{asset}_daily.json"
        if not p.exists():
            continue
        for ev in json.loads(p.read_text())["rows"]:
            for m in ev.get("markets") or []:
                c = classify_touch_market(asset, ev, m)
                why[(asset, c["status"])] += 1
                if c["status"] == "ok":
                    out.append(c)
    out.sort(key=lambda r: (r["date"], r["asset"], r["direction"], r["strike"]))
    return out, why


def t0_of(m: dict[str, Any], key: str) -> int:
    hh, mm = T0S[key]
    return et_time(date.fromisoformat(m["date"]), hh, mm)


# ----------------------------------------------------------------------------- klines -> per-day path summaries


def months_between(d0: date, d1: date) -> list[tuple[int, int]]:
    out = []
    y, m = d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


async def stage_klines(cache: Path) -> None:
    """Download Binance spot 1m archive files (monthly; daily for the current month) and stream them into DayPath
    summaries per ET date (+ hourly closes for EWMA vol). One file in memory at a time."""
    from tradedesk.providers.binance_vision import parse_klines_zip
    from tradedesk.server import build_server

    markets, _ = load_daily(cache)
    dates = sorted({m["date"] for m in markets})
    d0 = date.fromisoformat(dates[0]) - timedelta(days=95)  # 90-day EWMA lookback
    d1 = date.fromisoformat(dates[-1]) + timedelta(days=1)
    zdir = cache / "klines_zip"
    zdir.mkdir(exist_ok=True)
    today = datetime.now(UTC).date()
    jobs: list[tuple[str, Path, str]] = []
    for sym in ASSETS.values():
        for y, mo in months_between(d0, d1):
            if (y, mo) < (today.year, today.month):
                name = f"{sym}-1m-{y:04d}-{mo:02d}"
                jobs.append((sym, zdir / f"{name}.zip", f"/data/spot/monthly/klines/{sym}/1m/{name}.zip"))
            else:
                d = date(y, mo, 1)
                while d <= min(d1, today - timedelta(days=1)):
                    name = f"{sym}-1m-{d.isoformat()}"
                    jobs.append((sym, zdir / f"{name}.zip", f"/data/spot/daily/klines/{sym}/1m/{name}.zip"))
                    d += timedelta(days=1)
    todo = [j for j in jobs if not j[1].exists()]
    log(f"klines: {len(jobs)} files, {len(todo)} to fetch")
    _, rt = build_server()
    try:
        for i, (_, p, url) in enumerate(todo, 1):
            t = time.perf_counter()
            try:
                f = await _retry(lambda url=url: rt.binance_vision.http.get_bytes(url, tool="spot_klines"), url)
            except ProviderError as e:
                log(f"  missing {url}: {e.reason}")
                continue
            p.write_bytes(f.data)
            log(f"  {i}/{len(todo)} {p.name} {len(f.data) / 1e6:.1f}MB {time.perf_counter() - t:.1f}s")
    finally:
        await rt.aclose()
    # stream into summaries
    for sym in ASSETS.values():
        paths: dict[str, DayPath] = {}
        hourly: list[tuple[int, float]] = []
        dd = d0
        while dd <= d1:
            s, e = day_window(dd)
            paths[dd.isoformat()] = DayPath(s, e, [et_time(dd, *T0S[k]) for k in T0S])
            dd += timedelta(days=1)
        files = sorted(zdir.glob(f"{sym}-1m-*.zip"))
        for p in files:
            rows = parse_klines_zip(p.read_bytes())
            for r in rows:
                o = int(r[0]) // 1000
                dpk = datetime.fromtimestamp(o, UTC)
                # ET date of the candle: try the UTC date and the day before (ET is UTC-4/-5)
                for cand in (dpk.date(), dpk.date() - timedelta(days=1)):
                    dp = paths.get(cand.isoformat())
                    if dp is not None and dp.start <= o < dp.end:
                        dp.add(o, r[2], r[3], r[4])
                        break
                if (o + 60) % 3600 == 0:
                    hourly.append((o + 60, r[4]))
            del rows
        hourly.sort()
        out = {k: v.as_dict() for k, v in paths.items()}
        _write(cache / f"daypath_{sym}.json", {"days": out, "hourly": hourly})
        n_inc = sum(1 for v in paths.values() if not v.complete())
        log(f"{sym}: {len(files)} files, days={len(paths)} incomplete_days={n_inc} hourly={len(hourly)}")
    log("klines done")


def load_paths(cache: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for asset, sym in ASSETS.items():
        p = cache / f"daypath_{sym}.json"
        if p.exists():
            out[asset] = json.loads(p.read_text())
    return out


# ----------------------------------------------------------------------------- semantics (condition 6)


async def stage_semantics(cache: Path, n_markets: int = 12) -> None:
    from tradedesk.server import build_server

    markets, _ = load_daily(cache)
    rng = random.Random(7)
    pool = markets[:]
    rng.shuffle(pool)
    sdir = cache / "semantics"
    sdir.mkdir(exist_ok=True)
    _, rt = build_server()
    used = 0
    offsets_all: list[int] = []
    try:
        for m in pool:
            if used >= n_markets:
                break
            t0 = t0_of(m, PRIMARY)
            lo, hi = int(m["listed"] or m["start"]), t0 + 6 * 3600
            p = sdir / f"{m['market_id']}.json"
            if p.exists():
                d = json.loads(p.read_text())
            else:
                trades: list[dict[str, Any]] = []
                off = 0
                while off <= 9500:
                    f = await _retry(
                        lambda m=m, off=off, lo=lo, hi=hi: rt.polymarket.trades(m["condition_id"], limit=500, offset=off, start=lo, end=hi),
                        "trades",
                    )
                    trades += f.data
                    if len(f.data) < 500:
                        break
                    off += 500
                h = []
                if len(trades) >= 20:
                    h = (await _retry(lambda m=m, lo=lo, hi=hi: rt.polymarket.price_history_range(m["yes_token"], lo, hi, 1), "hist")).data
                d = {"hist": h, "trades": trades}
                _write(p, d)
            if len(d["trades"]) < 20:
                continue
            used += 1
            offs = jump_trade_offsets(d["hist"], d["trades"], m["yes_token"])
            offsets_all += offs
            pos = sum(1 for x in offs if x > 0)
            log(
                f"{m['asset']:8s} {m['date']} {m['direction']:4s} K={m['strike']:g} pts={len(d['hist'])} trades={len(d['trades'])} "
                f"jumps_matched={len(offs)} offset>0={pos} offsets={sorted(offs)[:12]}"
            )
    finally:
        await rt.aclose()
    n = len(offsets_all)
    if n:
        srt = sorted(offsets_all)
        le60 = sum(1 for x in srt if x <= 60) / n
        le0 = sum(1 for x in srt if x <= 0) / n
        log(
            f"POOLED markets={used} jumps={n} median_offset={statistics.median(srt):.0f}s share<=0s={le0:.0%} share<=60s={le60:.0%} "
            f"p05={srt[int(0.05 * n)]} p95={srt[min(n - 1, int(0.95 * n))]}"
        )
    log("semantics done")


# ----------------------------------------------------------------------------- fetch


async def stage_fetch(cache: Path, limit: int | None, lag: int) -> None:
    from tradedesk.server import build_server

    markets, _ = load_daily(cache)
    qdir = cache / "quotes"
    tdir = cache / "trades"
    qdir.mkdir(exist_ok=True)
    tdir.mkdir(exist_ok=True)
    last_t0 = max(T0S)  # "0730"
    todo = [
        m
        for m in markets
        if m["listed"] is not None and m["listed"] <= t0_of(m, PRIMARY) - 600 and not (qdir / f"{m['market_id']}.json").exists()
    ]
    if limit:
        todo = todo[:limit]
    log(f"quotes: {len(markets)} markets, {len(todo)} to fetch (window [listing, 07:30 ET])")
    _, rt = build_server()
    sem = asyncio.Semaphore(6)
    t_start = time.perf_counter()
    done = 0

    async def one_q(m: dict[str, Any]) -> None:
        nonlocal done
        async with sem:
            h = await _retry(lambda: rt.polymarket.price_history_range(m["yes_token"], int(m["listed"]), t0_of(m, last_t0), 1), "hist")
        _write(qdir / f"{m['market_id']}.json", {"hist": h.data})
        done += 1
        if done == 40:
            el = time.perf_counter() - t_start
            log(f"  sample: 40 in {el:.1f}s -> estimate {el / 40 * len(todo) / 60:.0f} min for quotes")
        if done % 1000 == 0:
            log(f"  quotes {done}/{len(todo)} {time.perf_counter() - t_start:.0f}s")

    try:
        await asyncio.gather(*(one_q(m) for m in todo))
        log(f"quotes done {done} in {time.perf_counter() - t_start:.0f}s")
        # last trade <= t0 for every t0 where q is in a basket (selection by q only)
        jobs = []
        for m in markets:
            qp = qdir / f"{m['market_id']}.json"
            if not qp.exists():
                continue
            hist = None
            for k in T0S:
                tp = tdir / f"{m['market_id']}_{k}.json"
                if tp.exists():
                    continue
                if hist is None:
                    hist = json.loads(qp.read_text())["hist"]
                qa = quote_at(hist, t0_of(m, k), lag=lag)
                if qa and (BASKET[0] <= qa[0] <= BASKET[1] or CENTER[0] <= qa[0] <= CENTER[1]):
                    jobs.append((m, k))
        if limit:
            jobs = jobs[:limit]
        log(f"trades: {len(jobs)} (market, t0) pairs to fetch")
        done = 0
        t_start = time.perf_counter()

        async def one_t(m: dict[str, Any], k: str) -> None:
            nonlocal done
            async with sem:
                f = await _retry(lambda: rt.polymarket.trades(m["condition_id"], limit=5, end=t0_of(m, k)), "trades")
            _write(tdir / f"{m['market_id']}_{k}.json", {"trades": f.data})
            done += 1
            if done == 40:
                el = time.perf_counter() - t_start
                log(f"  sample: 40 in {el:.1f}s -> estimate {el / 40 * len(jobs) / 60:.0f} min for trades")
            if done % 1000 == 0:
                log(f"  trades {done}/{len(jobs)} {time.perf_counter() - t_start:.0f}s")

        await asyncio.gather(*(one_t(m, k) for m, k in jobs))
        log(f"trades done {done} in {time.perf_counter() - t_start:.0f}s")
    finally:
        await rt.aclose()
    log("fetch done")


# ----------------------------------------------------------------------------- rows


def build_rows(cache: Path, key: str = PRIMARY, lag: int = 60, fresh_required: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One row per classified daily market at decision time ``key``; diagnostics in the second value."""
    markets, _ = load_daily(cache)
    paths = load_paths(cache)
    qdir, tdir = cache / "quotes", cache / "trades"
    diag: Counter = Counter()
    mismatch: list[dict[str, Any]] = []
    rows = []
    for m in markets:
        t0 = t0_of(m, key)
        dp = paths.get(m["asset"], {}).get("days", {}).get(m["date"])
        r = dict(m)
        r["t0"] = t0
        if dp is None or dp["n"] != (dp["end"] - dp["start"]) // 60:
            diag["no_full_binance_day"] += 1
            r["binance_outcome"] = None
        else:
            bo = touched(m["direction"], m["strike"], dp["high"], dp["low"])
            r["binance_outcome"] = int(bool(bo))
            if r["binance_outcome"] != m["outcome"]:
                ext = dp["high"] if m["direction"] == "up" else dp["low"]
                mismatch.append(
                    {
                        "market_id": m["market_id"],
                        "date": m["date"],
                        "asset": m["asset"],
                        "dir": m["direction"],
                        "K": m["strike"],
                        "ext": ext,
                        "gamma": m["outcome"],
                    }
                )
        pre = (dp or {}).get("pre", {}).get(str(t0)) if dp else None
        r["S0"] = pre["S0"] if pre else None
        r["touched_pre"] = touched(m["direction"], m["strike"], pre["high"], pre["low"]) if pre else None
        r["listed_ok"] = m["listed"] is not None and m["listed"] <= t0 - 600
        qp = qdir / f"{m['market_id']}.json"
        qa = quote_at(json.loads(qp.read_text())["hist"], t0, lag=lag) if qp.exists() else None
        r["q"] = qa[0] if qa else None
        tp = tdir / f"{m['market_id']}_{key}.json"
        lt = last_trade_before(json.loads(tp.read_text())["trades"], t0, m["yes_token"]) if tp.exists() else None
        r["last_trade"] = lt
        r["fresh"] = lt is not None and m["listed"] is not None and lt["t"] >= m["listed"]
        r["ambiguous"] = r["binance_outcome"] is not None and r["binance_outcome"] != m["outcome"]
        r["usable"] = (
            r["q"] is not None
            and r["binance_outcome"] is not None
            and not r["ambiguous"]
            and r["touched_pre"] is False
            and (r["fresh"] or not fresh_required)
        )
        rows.append(r)
    return rows, {"diag": diag, "mismatch": mismatch}


def in_basket(q: float | None, b: tuple[float, float] = BASKET) -> bool:
    return q is not None and b[0] <= q <= b[1]


def h1_values(rows: list[dict[str, Any]], pred=None) -> list[tuple[str, float]]:  # noqa: ANN001
    return [(r["date"], r["outcome"] - r["q"]) for r in rows if r["usable"] and in_basket(r["q"]) and (pred is None or pred(r))]


def side_returns(rows: list[dict[str, Any]], side: str, half_spread: float = 0.01, fee_all: float | None = None) -> list[tuple[str, float]]:
    out = []
    for r in rows:
        if not (r["usable"] and in_basket(r["q"])):
            continue
        lt = r["last_trade"]
        rate, exp = (fee_all, 1.0) if fee_all is not None else (r["fee_rate"], r["fee_exp"])
        if side == "yes":
            x = buy_return(
                r["q"], r["outcome"], half_spread=half_spread, last_print=lt["yes_px"] if lt else None, fee_rate=rate, fee_exp=exp
            )
        else:
            x = buy_return(
                1 - r["q"], 1 - r["outcome"], half_spread=half_spread, last_print=lt["no_px"] if lt else None, fee_rate=rate, fee_exp=exp
            )
        out.append((r["date"], x["ret"]))
    return out


def strangle_values(rows: list[dict[str, Any]], half_spread: float = 0.01, fee_all: float | None = None) -> list[tuple[str, float]]:
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["usable"]:
            lt = r["last_trade"]
            by[(r["date"], r["asset"])].append({**r, "last_yes_px": lt["yes_px"] if lt else None})
    out = []
    for (d, _), cs in sorted(by.items()):
        legs = pick_strangle(cs, lo=STRANGLE[0], hi=STRANGLE[1])
        if legs:
            out.append((d, strangle_return(legs, half_spread=half_spread, fee_all=fee_all)))
    return out


# ----------------------------------------------------------------------------- stage 0 (no means)


def macro_dates() -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for line in EVENTS_CSV.read_text().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 4:
            out[parts[0]].append((parts[1], parts[3]))
    return out


def stage0(cache: Path, out: str, lag: int) -> dict[str, bool]:
    rows, info = build_rows(cache, lag=lag)
    _, why = load_daily(cache)
    L: list[str] = []
    P = L.append
    P("## Стадія 0 (без середніх)\n")
    P("### Перелік і виключення до цін (денні)\n")
    sts = ["ok", "strike", "tokens", "rules", "direction", "window", "unresolved", "slug"]
    P("| Актив | " + " | ".join(sts) + " | дат | перша | остання | пропущених дат |")
    P("|---|" + "---|" * (len(sts) + 4))
    for asset in ASSETS:
        ds = sorted({r["date"] for r in rows if r["asset"] == asset})
        gaps = (date.fromisoformat(ds[-1]) - date.fromisoformat(ds[0])).days + 1 - len(ds) if ds else 0
        P(
            f"| {asset} | "
            + " | ".join(str(why.get((asset, s), 0)) for s in sts)
            + f" | {len(ds)} | {ds[0] if ds else '—'} | {ds[-1] if ds else '—'} | {gaps} |"
        )
    by_y = Counter((r["asset"], r["date"][:4]) for r in rows)
    P("\nРинків за роком: " + ", ".join(f"{a} {y}: {n}" for (a, y), n in sorted(by_y.items())))
    mm = info["mismatch"]
    chk = [r for r in rows if r["binance_outcome"] is not None]
    P(
        f"\n**Звірка результату з Binance 1m** (High/Low за [00:00; 24:00) ET): {len(chk) - len(mm)}/{len(chk)} збігів; "
        f"неоднозначних (виключено) {len(mm)}; без повного дня Binance {info['diag'].get('no_full_binance_day', 0)}."
    )
    for x in mm[:15]:
        P(f"- {x}")
    listed = [r for r in rows if r["listed_ok"]]
    withq = [r for r in listed if r["q"] is not None]
    cov = len(withq) / max(len(listed), 1)
    P(
        f"\n**Покриття q на t0 = 00:30 ET** (відсічка t0 − {lag} с, вік ≤ 15 хв): {len(withq)}/{len(listed)} = {cov:.1%} серед ринків, що приймали ордери до t0 − 10 хв (усього ринків {len(rows)})."
    )
    pre = sum(1 for r in withq if r["touched_pre"])
    P(f"Торкнуті до t0 (виключено): {pre}.")
    P("\n| Кошик | з q, не торкнуті | з угодою ≤ t0 | свіжі (угода після лістингу) | usable |\n|---|---|---|---|---|")
    for nm, b in (("OTM [0.05; 0.25]", BASKET), ("центр [0.35; 0.65]", CENTER), ("стренгл [0.07; 0.14]", STRANGLE)):
        rs = [r for r in withq if in_basket(r["q"], b) and r["touched_pre"] is False]
        P(
            f"| {nm} | {len(rs)} | {sum(1 for r in rs if r['last_trade'])} | {sum(1 for r in rs if r['fresh'])} | {sum(1 for r in rs if r['usable'])} |"
        )
    h1 = h1_values(rows)
    dates = day_baskets(h1)
    vals = [v for _, v, _ in dates]
    sd_d = statistics.stdev(vals) if len(vals) > 1 else float("nan")
    sd_c = statistics.stdev([v for _, v in h1]) if len(h1) > 1 else float("nan")
    m = mde(sd_d, len(dates)) if dates else math.inf
    P("\n### Потужність (SD фактичних значень; середні не рахувались)\n")
    P(f"- H1: контрактів {len(h1)}, дат {len(dates)}; SD контракту {sd_c:.3f}; SD дат {sd_d:.3f}; MDE = 2.8·SD/√n = {m * 100:.2f} п.п.")
    sv = day_baskets(strangle_values(rows))
    if len(sv) > 1:
        sds = statistics.stdev([v for _, v, _ in sv])
        P(f"- H3 (описово): днів зі стренглом {len(sv)}; SD дня {sds:.2f} капіталу; MDE {mde(sds, len(sv)) * 100:.0f} % капіталу.")
    md = macro_dates()
    ds_all = sorted({d for d, _ in h1})
    ev = Counter()
    for d in ds_all:
        for kind, status in md.get(d, []):
            ev[(kind, status)] += 1
    P(f"- H2: дат H1, що збігаються з рядками CSV подій (тип, статус): {dict(sorted(ev.items()))}. Середні H2 не рахуються (реєстрація).")
    gates = {
        "G1 дат ≥ 300": (len(dates) >= 300, str(len(dates))),
        "G2 покриття q ≥ 70 %": (cov >= 0.70, f"{cov:.1%}"),
        "G3 MDE H1 ≤ 5 п.п.": (m <= 0.05, f"{m * 100:.2f} п.п."),
    }
    P("\n### Ворота\n\n| Ворота | Значення | Пройдено |\n|---|---|---|")
    for k, (ok, v) in gates.items():
        P(f"| {k} | {v} | {'так' if ok else 'ні'} |")
    report = "\n".join(L)
    print(report, flush=True)
    if out:
        Path(out).expanduser().write_text(report + "\n")
    return {k: ok for k, (ok, _) in gates.items()}


def stage_power(cache: Path, lag: int) -> None:
    """Exploratory power table (counts, SD, MDE; no means) for every t0 and with/without the freshness filter."""
    print("| t0 ET | фільтр свіжості | контрактів | дат | SD дат | MDE H1, п.п. | дат для MDE 5 п.п. | дат для MDE 3 п.п. |")
    print("|---|---|---|---|---|---|---|---|")
    for k in T0S:
        for fr in (True, False):
            rows, _ = build_rows(cache, key=k, lag=lag, fresh_required=fr)
            d = day_baskets(h1_values(rows))
            vals = [v for _, v, _ in d]
            if len(vals) < 2:
                continue
            s = statistics.stdev(vals)
            print(
                f"| {k[:2]}:{k[2:]} | {'так' if fr else 'ні'} | {sum(n for _, _, n in d)} | {len(d)} | {s:.3f} | {mde(s, len(d)) * 100:.2f} | "
                f"{math.ceil((2.8 * s / 0.05) ** 2)} | {math.ceil((2.8 * s / 0.03) ** 2)} |",
                flush=True,
            )


# ----------------------------------------------------------------------------- full


def _row(label: str, vals: list[tuple[str, float]], k: float = 100.0) -> str:
    d = day_baskets(vals)
    if not d:
        return f"| {label} | 0 | 0 | — | — |"
    b = bootstrap_mean([v for _, v, _ in d])
    ci = f"[{b['lo'] * k:+.2f}; {b['hi'] * k:+.2f}]" if b["lo"] is not None else "—"
    return f"| {label} | {sum(n for _, _, n in d)} | {len(d)} | {b['mean'] * k:+.2f} | {ci} |"


def full(cache: Path, out: str, lag: int) -> None:
    g = stage0(cache, "", lag)
    if not all(g.values()):
        print("\nG1-G3 not passed: no means are computed (pre-registered). Verdict: inconclusive.", flush=True)
        return
    rows, _ = build_rows(cache, lag=lag)
    paths = load_paths(cache)
    L = ["\n## Повний прогін\n", "| Варіант | контрактів | дат | середнє r, п.п. | 95 % CI (bootstrap по датах) |", "|---|---|---|---|---|"]
    P = L.append
    h1 = h1_values(rows)
    P(_row("**H1 головний: денні BTC+ETH, q ∈ [0.05; 0.25], t0 00:30 ET, свіжі**", h1))
    for d_ in ("up", "down"):
        P(_row(f"напрямок {d_}", h1_values(rows, lambda r, d_=d_: r["direction"] == d_)))
    for a in ASSETS:
        P(_row(f"лише {a}", h1_values(rows, lambda r, a=a: r["asset"] == a)))
    ds = sorted({d for d, _ in h1})
    mid = ds[len(ds) // 2]
    P(_row(f"перша половина (< {mid})", [x for x in h1 if x[0] < mid]))
    P(_row(f"друга половина (≥ {mid})", [x for x in h1 if x[0] >= mid]))
    mv: dict[str, float] = defaultdict(float)
    for pp in paths.values():
        for d, dp in pp["days"].items():
            if dp["high"] and dp["low"]:
                mv[d] = max(mv[d], math.log(dp["high"] / dp["low"]))
    top3 = set(sorted((d for d in ds), key=lambda d: -mv.get(d, 0))[:3])
    P(_row(f"без 3 найрухливіших дат ({', '.join(sorted(top3))})", [x for x in h1 if x[0] not in top3]))
    for lo_, hi_ in ((0.05, 0.10), (0.10, 0.15), (0.15, 0.25)):
        P(
            _row(
                f"q ∈ [{lo_:.2f}; {hi_:.2f}) (розвідувально)",
                [(r["date"], r["outcome"] - r["q"]) for r in rows if r["usable"] and lo_ <= r["q"] < hi_ + (1e-9 if hi_ == 0.25 else 0)],
            )
        )
    rows_nf, _ = build_rows(cache, lag=lag, fresh_required=False)
    P(_row("без фільтра свіжості", h1_values(rows_nf)))
    for k in ("0200", "0730"):
        rk, _ = build_rows(cache, key=k, lag=lag)
        P(_row(f"t0 = {k[:2]}:{k[2:]} ET", h1_values(rk)))
    P(
        _row(
            "центр q ∈ [0.35; 0.65] (база)", [(r["date"], r["outcome"] - r["q"]) for r in rows if r["usable"] and in_basket(r["q"], CENTER)]
        )
    )

    P("\n**Економіка боку (дохід на капітал, %, кластер за датою):**\n")
    P("| Варіант | контрактів | дат | середнє, % | 95 % CI |\n|---|---|---|---|---|")
    for side in ("yes", "no"):
        P(_row(f"купівля {side.upper()}, півспреду 0.01, комісія за ринком", side_returns(rows, side)))
        P(_row(f"купівля {side.upper()}, півспреду 0.005", side_returns(rows, side, half_spread=0.005)))
        P(_row(f"купівля {side.upper()}, півспреду 0.02", side_returns(rows, side, half_spread=0.02)))
        P(_row(f"купівля {side.upper()}, комісія 0.07 на всі", side_returns(rows, side, fee_all=0.07)))

    P("\n**H3 стренгл (описово; дохід на витрачений капітал, %):**\n")
    P("| Варіант | актив-днів | дат | середнє, % | 95 % CI |\n|---|---|---|---|---|")
    P(_row("**півспреду 0.01, комісія за ринком**", strangle_values(rows)))
    P(_row("півспреду 0.005", strangle_values(rows, half_spread=0.005)))
    P(_row("півспреду 0.02", strangle_values(rows, half_spread=0.02)))
    P(_row("комісія 0.07 на всі", strangle_values(rows, fee_all=0.07)))
    sv = day_baskets(strangle_values(rows))
    if sv:
        vals = [v for _, v, _ in sv]
        worst = min(sv, key=lambda x: x[1])
        streak = max_losing_streak(vals)
        P(
            f"\nДнів {len(vals)}; збиткових {sum(1 for v in vals if v < 0)} ({sum(1 for v in vals if v < 0) / len(vals):.0%}); "
            f"найгірший день {worst[0]} {worst[1] * 100:+.0f} %; найдовша серія збиткових днів {streak} "
            f"(при ставці 0.7 % депозиту ≈ −{streak * 0.7:.1f} % депозиту за серію, якщо кожен день −100 %)."
        )

    P("\n**q − p_model (описово, EWMA 30 дн годинних прибутковостей Binance до t0), п.п.:**\n")
    P("| q-кошик | контрактів | дат | середнє q − p_model, п.п. | 95 % CI |\n|---|---|---|---|---|")
    hourly = {a: [(int(t), float(c)) for t, c in pp["hourly"]] for a, pp in paths.items()}
    vol_cache: dict[tuple[str, int], float | None] = {}
    for lo_, hi_ in ((0.05, 0.25), (0.35, 0.65)):
        vals2 = []
        for r in rows:
            if not (r["usable"] and lo_ <= r["q"] <= hi_ and r["S0"]):
                continue
            kk = (r["asset"], r["t0"])
            if kk not in vol_cache:
                vol_cache[kk] = realized_vol(hourly[r["asset"]], r["t0"])
            sig = vol_cache[kk]
            if sig:
                vals2.append((r["date"], r["q"] - touch_prob(r["S0"], r["strike"], sig, (r["end"] - r["t0"]) / (365 * 86400))))
        P(_row(f"[{lo_:.2f}; {hi_:.2f}]", vals2))
    _ = value_at  # imported for parity with pm_threshold helpers
    report = "\n".join(L)
    print(report, flush=True)
    if out:
        Path(out).expanduser().write_text(report + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--slugs", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--lag", type=int, default=60, help="quote cutoff t0 - lag seconds (pre-registered 60; 120 if semantics says so)")
    ap.add_argument("--log", default="")
    a = ap.parse_args()
    c = Path(a.cache_dir).expanduser()
    c.mkdir(parents=True, exist_ok=True)
    if a.log:
        import logging
        import sys

        fh = open(Path(a.log).expanduser(), "a", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stderr = fh
        logging.getLogger("httpx").setLevel(logging.WARNING)
    else:
        import logging

        logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.stage == "register":
        stage_register(c)
    elif a.stage == "probe":
        asyncio.run(stage_probe(c, [s for s in a.slugs.split(",") if s]))
    elif a.stage == "list":
        asyncio.run(stage_list(c))
    elif a.stage == "classify":
        ms, why = load_daily(c)
        for k, v in sorted(why.items()):
            print(k, v)
        seen: Counter = Counter()
        for asset in ASSETS:
            for ev in json.loads((c / "events" / f"{asset}_daily.json").read_text())["rows"]:
                for mk in ev.get("markets") or []:
                    st = classify_touch_market(asset, ev, mk)["status"]
                    if st != "ok" and seen[st] < 3:
                        seen[st] += 1
                        print(
                            "  example", st, ev.get("slug"), "|", mk.get("question"), "|", mk.get("endDate"), "|", mk.get("groupItemTitle")
                        )
        dates_by = Counter((m["asset"], m["date"][:7]) for m in ms)
        print("markets by month:", sorted(dates_by.items()))
        print("ok markets", len(ms), "dates", len({m["date"] for m in ms}))
        print("fees", Counter((m["asset"], m["fees_enabled"], m["fee_rate"], m["fee_exp"]) for m in ms))
        print(
            "listing lag vs window start (s):", Counter(min((m["listed"] or 0) - m["start"], 10**6) // 60 * 60 for m in ms).most_common(8)
        )
    elif a.stage == "fixture":
        # trimmed real sample for tests: the post's day, levels 86k (up) and 81k (down); wallet fields removed
        src = json.loads((c / "probe" / "what-price-will-bitcoin-hit-on-october-14-2026.json").read_text())[0]
        keep = (
            "id",
            "question",
            "conditionId",
            "slug",
            "endDate",
            "startDate",
            "description",
            "outcomes",
            "outcomePrices",
            "clobTokenIds",
            "groupItemTitle",
            "acceptingOrdersTimestamp",
            "feesEnabled",
            "feeSchedule",
            "orderPriceMinTickSize",
        )
        mk = [{k: m.get(k) for k in keep} for m in src["markets"] if m.get("groupItemTitle") in ("↑ 86,000", "↓ 81,000")]
        ev = {k: src.get(k) for k in ("id", "slug", "title", "startDate", "endDate")} | {"markets": mk}
        dst = Path(__file__).resolve().parents[1] / "tests/fixtures/polymarket_touch_event_2026-10-16.json"
        dst.write_text(json.dumps(ev, indent=1, ensure_ascii=False) + "\n")
        print("wrote", dst, len(mk))
    elif a.stage == "klines":
        asyncio.run(stage_klines(c))
    elif a.stage == "semantics":
        asyncio.run(stage_semantics(c))
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(c, a.limit or None, a.lag))
    elif a.stage == "stage0":
        stage0(c, a.out, a.lag)
    elif a.stage == "post":
        # consistency check with the post: BTC ladder of 2026-10-14 (CPI), q at each t0 vs "YES at 10c" for 86k / 81k
        for k in T0S:
            rs, _ = build_rows(c, key=k, lag=a.lag, fresh_required=False)
            for r in rs:
                if r["asset"] == "bitcoin" and r["date"] == "2026-10-14" and r["strike"] in (86_000, 81_000):
                    lt = r["last_trade"]
                    print(
                        f"t0={k} {r['direction']:4s} K={r['strike']:g} S0={r['S0']} q={r['q']} last_yes_print={lt['yes_px'] if lt else None} "
                        f"touched_pre={r['touched_pre']} outcome={r['outcome']} binance={r['binance_outcome']}"
                    )
    elif a.stage == "power":
        stage_power(c, a.lag)
    elif a.stage == "full":
        full(c, a.out, a.lag)
