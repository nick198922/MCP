"""New USDT-M perp listings on Binance: does a short drift against the market over the first 2-4 weeks?

Idea card: docs/ideas/IDEA-20261015-new-perp-listing-drift.md (sections "Тріаж критика", "Попередня реєстрація").

Data: the data.binance.vision archive (``rt.binance_vision``) keeps delisted symbols, so the Binance universe is
point-in-time. Metadata through ccxt (``rt.exchanges``). Market age of a token: Binance spot archive, Bybit raw
klines, OKX instrument list times, Hyperliquid daily candles. No keys, no orders.

Stages (each caches per file under --cache-dir; a rerun resumes):
    probe    reachability of the archive files (``--save-fixtures`` refreshes tests/fixtures samples)
    meta     archive symbol list + today's exchangeInfo (TradFi filter, onboardDate)
    fetch0   1h bars of the first month, 1d bars of the first three months, BTC daily, October daily files
    age      market-age evidence on other venues (Binance spot, Bybit spot/linear, OKX, Hyperliquid)
    stage0   n, SD, ICC by month, MDE and the gate -- no means; data checks; SD-only planning table

The registered full test (funding, placebo, basket, tails) runs only if the gate passes. On 2026-10-15 the gate failed
(MDE 7.25 pp > 5 pp), the verdict is ``inconclusive`` and those stages were deliberately not written: no means of the
outcome were computed. The analytics for them (funding, gaps, placebo day, basket) are in
``tradedesk.analytics.listing_drift`` with tests, ready for a rerun when enough events accumulate.

    uv run python scripts/verify_listing_drift.py <stage> --cache-dir ~/.cache/tradedesk-agents/listing_drift
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.funding_spread import normalize_asset
from tradedesk.analytics.listing_drift import (
    DAY_MS,
    DailyBars,
    bench_return,
    cr1_se,
    design_effect,
    entry_day,
    first_trade_ms,
    icc_anova,
    market_age_days,
    mde,
    month_key,
    short_leg,
    symbol_eligible,
)
from tradedesk.core.errors import ProviderError
from tradedesk.providers.binance_vision import parse_klines_zip
from tradedesk.server import build_server

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
START = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
END = int(datetime(2026, 9, 16, tzinfo=UTC).timestamp() * 1000)  # t0 < END
LAST_DAY = int(datetime(2026, 10, 14, tzinfo=UTC).timestamp() * 1000) // DAY_MS  # last complete UTC day
ARCHIVE_LAST_MONTH = (2026, 9)  # monthly archive files exist up to this month; October from daily files
BTC = "BTCUSDT"
COST = 0.0025
H_MAIN = 14
GATE_MDE = 0.05
GATE_N = 150
GATE_MONTHS = 24


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


def ym_of(ms: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(ms / 1000, UTC)
    return d.year, d.month


def add_month(y: int, m: int, k: int = 1) -> tuple[int, int]:
    i = y * 12 + m - 1 + k
    return i // 12, i % 12 + 1


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(p)


class Fetcher:
    """Per-file JSON cache; ``null`` marks a file the source does not have (404)."""

    def __init__(self, cache: Path, workers: int = 12) -> None:
        self.cache = cache
        self.sem = asyncio.Semaphore(workers)
        self.done = 0
        self.errors: Counter[str] = Counter()

    async def one(self, path: Path, go: Callable[[], Awaitable[Any]]) -> None:
        if await asyncio.to_thread(path.exists):
            return
        async with self.sem:
            data = None
            for k in range(4):
                try:
                    data = await go()
                    break
                except ProviderError as e:
                    if e.reason == "data_unavailable" or any(
                        x in str(e.detail) for x in ("Symbol Is Invalid", "Invalid symbol", "doesn't exist")
                    ):
                        data = None
                        break
                    if k == 3:
                        self.errors[e.reason] += 1
                        if self.errors[e.reason] <= 3:
                            print(f"  error {path.relative_to(self.cache)}: {e.reason}: {str(e.detail)[:200]}", flush=True)
                        return  # transient: leave uncached so a rerun retries
                    await asyncio.sleep(5 * (k + 1))
        wr(path, data)
        self.done += 1

    async def many(self, label: str, jobs: list[tuple[Path, Callable[[], Awaitable[Any]]]]) -> None:
        todo = [(p, g) for p, g in jobs if not p.exists()]
        print(f"[{label}] {len(jobs)} files, {len(todo)} to fetch", flush=True)
        t0 = time.time()
        self.done = 0
        step = max(25, len(todo) // 20)

        async def run(p: Path, g: Callable[[], Awaitable[Any]]) -> None:
            await self.one(p, g)
            if self.done and self.done % step == 0:
                el = time.time() - t0
                print(f"  [{label}] {self.done}/{len(todo)} in {el:.0f}s, eta {el / self.done * (len(todo) - self.done):.0f}s", flush=True)

        await asyncio.gather(*[run(p, g) for p, g in todo])
        print(f"[{label}] done in {time.time() - t0:.0f}s; transient errors: {dict(self.errors)}", flush=True)


# ---------------------------------------------------------------- metadata


async def stage_meta(cache: Path) -> None:
    _, rt = build_server()
    try:
        sp, ip = cache / "symbols.json", cache / "exchange_info.json"
        if not sp.exists():
            f = await rt.binance_vision.um_symbols()
            wr(sp, f.data)
        if not ip.exists():
            mk = (await rt.exchanges.markets("binanceusdm")).data or {}
            info = {}
            for m in mk.values():
                i = (m or {}).get("info") or {}
                if i.get("symbol"):
                    info[i["symbol"]] = {
                        "underlyingType": i.get("underlyingType"),
                        "contractType": i.get("contractType"),
                        "status": i.get("status"),
                        "onboardDate": int(i.get("onboardDate") or 0),
                        "deliveryDate": int(i.get("deliveryDate") or 0),
                    }
            wr(ip, {"fetched_at": int(time.time()), "info": info})
        syms, info = rd(sp), rd(ip)["info"]
        print(f"archive USDT-M symbols {len(syms)}; exchangeInfo symbols {len(info)}", flush=True)
        elig = eligible(syms, info)
        print(f"eligible (USDT, COIN, no stables/wrapped/TradFi/index): {len(elig)}", flush=True)
        fx = Fetcher(cache, workers=6)
        jobs = []
        for s in elig:

            async def go(s: str = s) -> Any:
                f = await rt.binance_vision.list_dir(f"data/futures/um/monthly/klines/{s}/1d/")
                return sorted({k.rsplit("-", 2)[-2] + "-" + k.rsplit("-", 1)[-1][:2] for k in f.data["keys"] if k.endswith(".zip")})

            jobs.append((cache / "listing" / f"{s}.json", go))
        await fx.many("listing 1d", jobs)
    finally:
        await rt.aclose()


def eligible(syms: list[str], info: dict[str, Any]) -> list[str]:
    out = []
    for s in syms:
        i = info.get(s) or {}
        if symbol_eligible(s, i.get("contractType"), i.get("underlyingType")):
            out.append(s)
    return out


def candidates(cache: Path) -> dict[str, str]:
    """Eligible symbols whose first archive month lies in [2022-01; 2026-09] -> first month."""
    syms, info = rd(cache / "symbols.json"), rd(cache / "exchange_info.json")["info"]
    out = {}
    for s in eligible(syms, info):
        p = cache / "listing" / f"{s}.json"
        months = rd(p) if p.exists() else None
        if months and "2022-01" <= months[0] <= "2026-09":
            out[s] = months[0]
    return out


# ---------------------------------------------------------------- archive bars


def k_path(cache: Path, iv: str, sym: str, y: int, m: int) -> Path:
    return cache / f"k{iv}" / sym / f"{y:04d}-{m:02d}.json"


def kd_path(cache: Path, sym: str, day: int) -> Path:
    return cache / "k1d_daily" / sym / f"{ymd(day)}.json"


def month_job(rt: Any, cache: Path, iv: str, sym: str, y: int, m: int) -> tuple[Path, Callable[[], Awaitable[Any]]]:
    async def go() -> Any:
        return (await rt.binance_vision.um_klines_monthly(sym, iv, y, m)).data

    return k_path(cache, iv, sym, y, m), go


def day_job(rt: Any, cache: Path, sym: str, day: int) -> tuple[Path, Callable[[], Awaitable[Any]]]:
    async def go() -> Any:
        return (await rt.binance_vision.um_klines_daily(sym, "1d", ymd(day))).data

    return kd_path(cache, sym, day), go


def october_days() -> list[int]:
    first = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1000) // DAY_MS
    return list(range(first, LAST_DAY + 1))


def daily_jobs(rt: Any, cache: Path, sym: str, y: int, m: int, n_months: int) -> list[tuple[Path, Callable[[], Awaitable[Any]]]]:
    jobs = []
    for k in range(n_months):
        yy, mm = add_month(y, m, k)
        if (yy, mm) <= ARCHIVE_LAST_MONTH:
            jobs.append(month_job(rt, cache, "1d", sym, yy, mm))
        elif (yy, mm) == (2026, 10):
            jobs.extend(day_job(rt, cache, sym, d) for d in october_days())
    return jobs


async def stage_fetch0(cache: Path, sample: int) -> None:
    _, rt = build_server()
    try:
        cand = candidates(cache)
        syms = sorted(cand)[:sample] if sample else sorted(cand)
        print(f"candidates (first archive month 2022-01..2026-09): {len(cand)}; this run: {len(syms)}", flush=True)
        fx = Fetcher(cache)
        jobs = []
        for s in syms:
            y, m = map(int, cand[s].split("-"))
            jobs.append(month_job(rt, cache, "1h", s, y, m))
            jobs.extend(daily_jobs(rt, cache, s, y, m, 3))
        y, m = 2021, 12
        while (y, m) <= ARCHIVE_LAST_MONTH:
            jobs.append(month_job(rt, cache, "1d", BTC, y, m))
            y, m = add_month(y, m)
        jobs.extend(day_job(rt, cache, BTC, d) for d in october_days())
        await fx.many("fetch0", jobs)
        # second 1h month when the first month has no traded bar (listed at the very end of a month)
        more = []
        for s in syms:
            y, m = map(int, cand[s].split("-"))
            rows = rd(k_path(cache, "1h", s, y, m)) if k_path(cache, "1h", s, y, m).exists() else None
            if not rows or first_trade_ms(rows) is None:
                yy, mm = add_month(y, m)
                if (yy, mm) <= ARCHIVE_LAST_MONTH:
                    more.append(month_job(rt, cache, "1h", s, yy, mm))
        if more:
            await fx.many("fetch0 1h second month", more)
    finally:
        await rt.aclose()


def load_daily(cache: Path, sym: str) -> DailyBars:
    rows: list[list[float]] = []
    for p in sorted((cache / "k1d" / sym).glob("*.json")) if (cache / "k1d" / sym).exists() else []:
        rows.extend(rd(p) or [])
    for p in sorted((cache / "k1d_daily" / sym).glob("*.json")) if (cache / "k1d_daily" / sym).exists() else []:
        rows.extend(rd(p) or [])
    return DailyBars.from_rows(rows)


def first_trade(cache: Path, sym: str, first_month: str) -> int | None:
    y, m = map(int, first_month.split("-"))
    for k in range(2):
        yy, mm = add_month(y, m, k)
        p = k_path(cache, "1h", sym, yy, mm)
        if p.exists():
            t = first_trade_ms(rd(p) or [])
            if t is not None:
                return t
    return None


# ---------------------------------------------------------------- events


def build_events(cache: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """One event per normalised asset (earliest symbol), t0 in [START; END). Returns (events, notes)."""
    cand = candidates(cache)
    info = rd(cache / "exchange_info.json")["info"]
    notes: list[str] = []
    raw = []
    # earliest symbol per asset across the whole archive (incl. symbols listed before 2022)
    syms, _ = rd(cache / "symbols.json"), None
    all_first: dict[str, tuple[str, str]] = {}
    for s in eligible(syms, info):
        p = cache / "listing" / f"{s}.json"
        months = rd(p) if p.exists() else None
        if not months:
            continue
        a = normalize_asset(s[:-4], "binanceusdm")[0]
        if a not in all_first or months[0] < all_first[a][1]:
            all_first[a] = (s, months[0])
    for s, fm in sorted(cand.items()):
        a = normalize_asset(s[:-4], "binanceusdm")[0]
        if all_first[a][0] != s:
            notes.append(f"duplicate asset {a}: {s} (first {fm}) after {all_first[a][0]} (first {all_first[a][1]}) -> not an event")
            continue
        t0 = first_trade(cache, s, fm)
        if t0 is None:
            notes.append(f"no traded 1h bar for {s} in {fm} (+1 month)")
            continue
        if not (START <= t0 < END):
            continue
        i = info.get(s) or {}
        raw.append(
            {
                "symbol": s,
                "asset": a,
                "mult": normalize_asset(s[:-4], "binanceusdm")[1],
                "t0": t0,
                "status": i.get("status") or "REMOVED",
                "onboard": i.get("onboardDate") or 0,
            }
        )
    return raw, notes


def stage0_rows(cache: Path, events: list[dict[str, Any]], horizon: int = H_MAIN, delay: int = 0) -> list[dict[str, Any]]:
    btc = load_daily(cache, BTC)
    out = []
    for e in events:
        bars = load_daily(cache, e["symbol"])
        E = entry_day(e["t0"], delay_days=delay)
        if E + horizon - 1 > LAST_DAY:
            continue
        leg = short_leg(bars, E, horizon)
        if leg is None:
            continue
        rb = bench_return(btc, E, leg.exit)
        if rb is None:
            continue
        out.append({**e, "entry": E, "leg": leg, "r_btc": rb, "metric0": leg.pnl + rb - COST})
    return out


def stage0(cache: Path) -> None:
    events, notes = build_events(cache)
    print(f"events (one per asset, first 1h trade in [2022-01-01; 2026-09-16)): {len(events)}")
    for n in notes:
        print("  note:", n)
    print("by year:", dict(sorted(Counter(datetime.fromtimestamp(e["t0"] / 1000, UTC).year for e in events).items())))
    print("status today:", dict(Counter(e["status"] for e in events)))
    removed = [e["symbol"] for e in events if e["status"] == "REMOVED"]
    print(f"absent from today's exchangeInfo (fully removed, from the archive only): {len(removed)}: {' '.join(removed)}")
    mism = [e for e in events if e["onboard"] and abs(e["onboard"] - e["t0"]) > 2 * DAY_MS]
    print(f"onboardDate vs first 1h trade differ by > 2 days: {len(mism)}")
    for e in mism:
        print(f"  {e['symbol']:<16} onboard {ymd(e['onboard'] // DAY_MS)} first trade {ymd(e['t0'] // DAY_MS)}")
    gaps = [e["t0"] - e["onboard"] for e in events if e["onboard"]]
    if gaps:
        print(
            f"first trade - onboardDate (h): median {statistics.median(gaps) / 3.6e6:.2f}, min {min(gaps) / 3.6e6:.1f}, max {max(gaps) / 3.6e6:.1f}"
        )
    hrs = Counter(datetime.fromtimestamp(e["t0"] / 1000, UTC).hour for e in events)
    print("first-trade hour (UTC):", dict(sorted(hrs.items())))
    lag = Counter(round((entry_day(e["t0"]) * DAY_MS - e["t0"]) / 3.6e6) for e in events)
    print(f"hours from first trade to entry open: min {min(lag)}, max {max(lag)}")
    age_summary(cache, events)

    rows = stage0_rows(cache, events)
    xs = [r["metric0"] for r in rows]
    cl = [month_key(r["t0"]) for r in rows]
    n, k = len(xs), len(set(cl))
    sd = statistics.stdev(xs)
    icc = icc_anova(xs, cl)
    deff = design_effect(cl, icc)
    m_icc = mde(sd, n, deff)
    m_cr = 2.8 * cr1_se(xs, cl)
    trunc = sum(r["leg"].truncated for r in rows)
    print()
    print(f"STAGE 0 metric: short price P&L (1x liquidation) + r_BTC - {COST:.2%} cost, H={H_MAIN}, entry delay 0 (no funding)")
    print(
        f"evaluable events n={n} in {k} months (by year: {dict(sorted(Counter(ymd(r['entry'])[:4] for r in rows).items()))}); truncated exits {trunc}"
    )
    print(f"SD={sd * 100:.2f}pp  ICC(month)={icc:+.4f}  deff={deff:.3f}  n_eff={n / deff:.1f}")
    print(
        f"MDE_icc={m_icc * 100:.2f}pp  MDE_cr={m_cr * 100:.2f}pp  -> gate MDE=max={max(m_icc, m_cr) * 100:.2f}pp (threshold {GATE_MDE * 100:.0f}pp)"
    )
    ok = n >= GATE_N and k >= GATE_MONTHS and max(m_icc, m_cr) <= GATE_MDE
    print(
        f"GATE: n>={GATE_N}: {n >= GATE_N}; months>={GATE_MONTHS}: {k >= GATE_MONTHS}; MDE<={GATE_MDE * 100:.0f}pp: {max(m_icc, m_cr) <= GATE_MDE} -> {'PASS' if ok else 'FAIL'}"
    )
    for half, sel in (("2022-2023", lambda r: ymd(r["entry"]) < "2024"), ("2024-2026", lambda r: ymd(r["entry"]) >= "2024")):
        sub = [r for r in rows if sel(r)]
        if len(sub) > 2:
            print(f"  {half}: n={len(sub)} SD={statistics.stdev([r['metric0'] for r in sub]) * 100:.2f}pp")
    wr(cache / "stage0.json", {"n": n, "months": k, "sd": sd, "icc": icc, "deff": deff, "mde_icc": m_icc, "mde_cr": m_cr, "pass": ok})
    for need in (5.0, 6.0):
        print(
            f"events needed for MDE <= {need:.0f}pp at this SD/clustering: ICC route {n * (m_icc * 100 / need) ** 2:.0f}, "
            f"CR1 route {n * (m_cr * 100 / need) ** 2:.0f} (MDE ~ 1/sqrt(n))"
        )
    data_checks(cache, events, rows)
    planning(cache, events)


def power_line(label: str, xs: list[float], cl: list[int]) -> str:
    sd = statistics.stdev(xs)
    icc = icc_anova(xs, cl)
    deff = design_effect(cl, icc)
    m1, m2 = mde(sd, len(xs), deff), 2.8 * cr1_se(xs, cl)
    return f"  {label:<44} n={len(xs):>4} months={len(set(cl)):>3} SD={sd * 100:6.2f}pp ICC={icc:+.3f} MDE_icc={m1 * 100:5.2f}pp MDE_cr={m2 * 100:5.2f}pp"


def planning(cache: Path, events: list[dict[str, Any]]) -> None:
    """Power of other designs (exploratory, after the gate; SD and MDE only, no means). Not part of the verdict."""
    print()
    print("PLANNING ONLY (exploratory, not registered, no means): power of other designs on the same events")
    btc = load_daily(cache, BTC)
    for h in (7, 14, 30):
        for lev, loss in ((1.0, 1.0), (3.0, 1 / 3)):
            xs, cl = [], []
            for e in events:
                E = entry_day(e["t0"])
                if E + h - 1 > LAST_DAY:
                    continue
                leg = short_leg(load_daily(cache, e["symbol"]), E, h, liq_move=(1 / 3) if lev == 3.0 else liq_threshold_1x(), liq_loss=loss)
                if leg is None or (rb := bench_return(btc, E, leg.exit)) is None:
                    continue
                xs.append(leg.pnl + rb - COST)
                cl.append(month_key(e["t0"]))
            print(power_line(f"H={h:<2} liquidation {'1x (-100%)' if lev == 1 else '3x at +33% (-1/3)'}", xs, cl))
    rows = stage0_rows(cache, events)
    if all("age" in e for e in events):
        new = [r for r in rows if r["age"]["new"]]
        print(power_line("H=14 1x, new tokens only (market <= 30 d)", [r["metric0"] for r in new], [month_key(r["t0"]) for r in new]))
    print(f"  H=14 1x, share of evaluable events per year: {dict(sorted(Counter(ymd(r['entry'])[:4] for r in rows).items()))}")


def liq_threshold_1x() -> float:
    return 1.0 - 0.025


def data_checks(cache: Path, events: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    """Integrity checks that do not look at average outcomes."""
    print()
    btc = load_daily(cache, BTC)
    d = int(datetime(2024, 12, 31, tzinfo=UTC).timestamp()) // 86400
    print(f"check BTCUSDT perp close 2024-12-31 = {btc.close.get(d)} (known ~93,500; token-unlock report 93,548.9)")
    arb = next((e for e in events if e["symbol"] == "ARBUSDT"), None)
    if arb:
        print(
            f"check ARBUSDT first 1h trade {datetime.fromtimestamp(arb['t0'] / 1000, UTC):%Y-%m-%d %H:%M} UTC, onboardDate "
            f"{datetime.fromtimestamp(arb['onboard'] / 1000, UTC):%Y-%m-%d %H:%M} UTC (ARB airdrop and first trading 2023-03-23)"
        )
    # 1h vs 1d: the daily close of the first full day equals the last 1h close of that day
    bad, cmp = [], 0
    for e in events:
        bars = load_daily(cache, e["symbol"])
        d1 = e["t0"] // DAY_MS + 1
        y, m = ym_of(d1 * DAY_MS)
        p = k_path(cache, "1h", e["symbol"], y, m)
        if d1 not in bars.close or not p.exists():
            continue
        hrs = [r for r in rd(p) or [] if int(r[0]) // DAY_MS == d1]
        if len(hrs) == 24:
            cmp += 1
            if abs(hrs[-1][4] / bars.close[d1] - 1) > 1e-9:
                bad.append(e["symbol"])
    print(f"check 1d close == last 1h close on the first full day: {cmp - len(bad)}/{cmp} equal; differ: {bad[:10]}")
    # gaps and extreme day-over-day moves inside the H=14 window
    gaps, jumps = [], []
    for r in rows:
        bars = load_daily(cache, r["symbol"])
        E, X = r["entry"], r["entry"] + H_MAIN - 1
        miss = [x for x in range(E, X + 1) if x not in bars.close and x <= (bars.last or 0)]
        if miss:
            gaps.append(f"{r['symbol']}:{len(miss)}")
        for x in range(E, X + 1):
            if x in bars.close and x - 1 in bars.close:
                q = bars.close[x] / bars.close[x - 1]
                if q >= 3 or q <= 1 / 3:
                    jumps.append(f"{r['symbol']} {ymd(x)} x{q:.2f}")
    print(f"check missing daily bars inside the H=14 window: {len(gaps)} events {gaps[:20]}")
    print(f"check day-over-day close moves >= x3 or <= x1/3 inside the window (squeezes or data errors; verified by hand): {jumps}")


# ---------------------------------------------------------------- market age (other venues)


def age_path(cache: Path, kind: str, key: str) -> Path:
    return cache / "age" / kind / f"{key}.json"


async def stage_age(cache: Path, sample: int) -> None:
    events, _ = build_events(cache)
    if sample:
        events = events[:sample]
    _, rt = build_server()
    fx = Fetcher(cache, workers=4)
    try:
        # OKX and Hyperliquid metadata: one request each
        async def okx(kind: str) -> Any:
            f = await rt.exchanges.public_get("okx", "publicGetPublicInstruments", {"instType": kind})
            return [
                {"instId": x.get("instId"), "listTime": int(x.get("listTime") or 0), "ctVal": x.get("ctVal")}
                for x in (f.data or {}).get("data") or []
            ]

        async def hl_meta() -> Any:
            f = await rt.hyperliquid._info({"type": "meta"}, ttl=0)
            return [{"name": u["name"], "delisted": bool(u.get("isDelisted"))} for u in (f.data or {}).get("universe") or []]

        await fx.many(
            "age meta",
            [
                (age_path(cache, "meta", "okx_SPOT"), lambda: okx("SPOT")),
                (age_path(cache, "meta", "okx_SWAP"), lambda: okx("SWAP")),
                (age_path(cache, "meta", "hl"), hl_meta),
            ],
        )
        hl_names = {normalize_asset(u["name"], "hyperliquid")[0]: u["name"] for u in rd(age_path(cache, "meta", "hl")) or []}
        jobs = []
        for e in events:
            a, t0 = e["asset"], e["t0"]
            E = entry_day(t0)
            for q in ("USDT", "BUSD", "BTC"):
                sym = f"{a}{q}"

                async def go(sym: str = sym) -> Any:
                    f = await rt.binance_vision.list_dir(f"data/spot/monthly/klines/{sym}/1d/")
                    return sorted({k.rsplit("-", 2)[-2] + "-" + k.rsplit("-", 1)[-1][:2] for k in f.data["keys"] if k.endswith(".zip")})

                jobs.append((age_path(cache, "binance_spot_listing", sym), go))
            for cat, sym in (("spot", f"{a}USDT"), ("linear", e["symbol"]), ("linear", f"{a}USDT")):

                async def gb(cat: str = cat, sym: str = sym, t0: int = t0, E: int = E) -> Any:
                    f = await rt.exchanges.public_get(
                        "bybit",
                        "publicGetV5MarketKline",
                        {
                            "category": cat,
                            "symbol": sym,
                            "interval": "D",
                            "start": t0 - 1000 * DAY_MS,
                            "end": (E + 1) * DAY_MS,
                            "limit": 1000,
                        },
                    )
                    lst = ((f.data or {}).get("result") or {}).get("list") or []
                    rows = sorted([int(r[0]), float(r[4]), float(r[5])] for r in lst)
                    rows = [r for r in rows if r[2] > 0]
                    if not rows:
                        return None
                    close_e = next((r[1] for r in rows if r[0] // DAY_MS == E), None)
                    return {"first": rows[0][0], "n": len(rows), "close_E": close_e}

                jobs.append((age_path(cache, f"bybit_{cat}", f"{sym}_{E}"), gb))
            if a in hl_names:
                coin = hl_names[a]

                async def gh(coin: str = coin, E: int = E) -> Any:
                    f = await rt.hyperliquid._info(
                        {
                            "type": "candleSnapshot",
                            "req": {"coin": coin, "interval": "1d", "startTime": START - 400 * DAY_MS, "endTime": (E + 1) * DAY_MS},
                        },
                        ttl=0,
                    )
                    rows = sorted([int(r["t"]), float(r["c"]), float(r["v"])] for r in f.data or [])
                    rows = [r for r in rows if r[2] > 0]
                    if not rows:
                        return None
                    close_e = next((r[1] for r in rows if r[0] // DAY_MS == E), None)
                    return {"first": rows[0][0], "n": len(rows), "close_E": close_e, "coin": coin}

                jobs.append((age_path(cache, "hl", f"{coin}_{E}"), gh))
        await fx.many("age", jobs)
        # exact first day of the Binance spot listing when it is close to the perp listing; spot price on E for the check
        more = []
        for e in events:
            E = entry_day(e["t0"])
            for q in ("USDT", "BUSD", "BTC"):
                sym = f"{e['asset']}{q}"
                months = rd(age_path(cache, "binance_spot_listing", sym)) if age_path(cache, "binance_spot_listing", sym).exists() else None
                if not months:
                    continue
                for mo in {months[0], datetime.fromtimestamp(E * 86400, UTC).strftime("%Y-%m")}:
                    if mo not in months:
                        continue
                    y, m = map(int, mo.split("-"))

                    async def gs(sym: str = sym, y: int = y, m: int = m) -> Any:
                        f = await rt.binance_vision.http.get_bytes(f"/data/spot/monthly/klines/{sym}/1d/{sym}-1d-{y:04d}-{m:02d}.zip")
                        return parse_klines_zip(f.data)

                    more.append((cache / "age" / "binance_spot_k1d" / sym / f"{mo}.json", gs))
        await fx.many("age spot months", more)
    finally:
        await rt.aclose()


def price_ok(other: float | None, perp_open: float | None, mult: int, other_mult: int = 1) -> bool | None:
    """Same token? per-coin prices within [0.5; 2] on the entry day (None when not comparable)."""
    if not other or not perp_open:
        return None
    r = (other / other_mult) / (perp_open / mult)
    return 0.5 <= r <= 2.0


def age_evidence(cache: Path, e: dict[str, Any], perp: DailyBars) -> dict[str, Any]:
    """Earliest trading evidence per venue (ms) after the price-level check; also flags other perps before t0."""
    a, t0, E, mult = e["asset"], e["t0"], entry_day(e["t0"]), e["mult"]
    p_e = perp.open.get(E)
    ev: dict[str, int | None] = {}
    rejected: list[str] = []
    other_perp_first: list[int] = []
    # Binance spot
    for q in ("USDT", "BUSD", "BTC"):
        sym = f"{a}{q}"
        p = age_path(cache, "binance_spot_listing", sym)
        months = rd(p) if p.exists() else None
        if not months:
            continue
        d = cache / "age" / "binance_spot_k1d" / sym
        first_rows = rd(d / f"{months[0]}.json") if (d / f"{months[0]}.json").exists() else None
        first = first_trade_ms(first_rows or []) if first_rows else None
        if first is None:
            y, m = map(int, months[0].split("-"))
            first = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
        if q == "USDT":
            mo = datetime.fromtimestamp(E * 86400, UTC).strftime("%Y-%m")
            rows = rd(d / f"{mo}.json") if (d / f"{mo}.json").exists() else None
            spot_e = next((r[1] for r in rows or [] if int(r[0]) // DAY_MS == E), None)
            if price_ok(spot_e, p_e, mult) is False:
                rejected.append(f"binance spot {sym}")
                continue
        ev[f"binance_spot_{q}"] = first
    # Bybit
    for cat, sym in (("spot", f"{a}USDT"), ("linear", e["symbol"]), ("linear", f"{a}USDT")):
        p = age_path(cache, f"bybit_{cat}", f"{sym}_{E}")
        r = rd(p) if p.exists() else None
        if not r:
            continue
        om = normalize_asset(sym[:-4], "bybit")[1]
        if price_ok(r.get("close_E"), p_e, mult, om) is False:
            rejected.append(f"bybit {cat} {sym}")
            continue
        ev[f"bybit_{cat}_{sym}"] = r["first"]
        if cat == "linear":
            other_perp_first.append(r["first"])
    # OKX (today's instruments only; no price check)
    for kind, suffix in (("SPOT", ""), ("SWAP", "-SWAP")):
        p = age_path(cache, "meta", f"okx_{kind}")
        for x in rd(p) or [] if p.exists() else []:
            if x["instId"] in (f"{a}-USDT{suffix}", f"{e['symbol'][:-4]}-USDT{suffix}") and x["listTime"]:
                ev[f"okx_{kind}"] = min(ev.get(f"okx_{kind}") or x["listTime"], x["listTime"])
                if kind == "SWAP":
                    other_perp_first.append(x["listTime"])
    # Hyperliquid
    for p in (cache / "age" / "hl").glob(f"*_{E}.json") if (cache / "age" / "hl").exists() else []:
        r = rd(p)
        if not r or normalize_asset(r["coin"], "hyperliquid")[0] != a:
            continue
        om = normalize_asset(r["coin"], "hyperliquid")[1]
        if price_ok(r.get("close_E"), p_e, mult, om) is False:
            rejected.append(f"hl {r['coin']}")
            continue
        ev["hl"] = r["first"]
        other_perp_first.append(r["first"])
    age = market_age_days(t0, ev.values())
    return {
        "evidence": ev,
        "age_days": age,
        "new": age is None or age <= 30,
        "other_perp_before": any(f < t0 - DAY_MS for f in other_perp_first),
        "rejected": rejected,
    }


def age_summary(cache: Path, events: list[dict[str, Any]]) -> None:
    if not (cache / "age").exists():
        print("market age: not fetched yet (stage 'age')")
        return
    groups: Counter[str] = Counter()
    by_year: dict[str, Counter[str]] = {}
    other = 0
    rej: Counter[str] = Counter()
    for e in events:
        a = age_evidence(cache, e, load_daily(cache, e["symbol"]))
        e["age"] = a
        g = "<=30d" if a["new"] else ("31-365d" if a["age_days"] <= 365 else ">365d")
        groups[g] += 1
        by_year.setdefault(ymd(e["t0"] // DAY_MS)[:4], Counter())[g] += 1
        other += a["other_perp_before"]
        for r in a["rejected"]:
            rej[r.split()[0]] += 1
    print(f"market age before the Binance perp: {dict(groups)}; by year: { {y: dict(c) for y, c in sorted(by_year.items())} }")
    print(f"already traded on another perp (Bybit/OKX/HL) > 1 day before t0: {other} of {len(events)}")
    print(f"venue legs rejected by the price-level check (different token, same ticker): {dict(rej)}")


PROBES = {
    "binance_vision_ARBUSDT-1h-2023-03_2026-10-15.zip": "/data/futures/um/monthly/klines/ARBUSDT/1h/ARBUSDT-1h-2023-03.zip",
    "binance_vision_ARBUSDT-fundingRate-2023-03_2026-10-15.zip": "/data/futures/um/monthly/fundingRate/ARBUSDT/ARBUSDT-fundingRate-2023-03.zip",
    "binance_vision_FTTUSDT-fundingRate-2022-11_2026-10-15.zip": "/data/futures/um/monthly/fundingRate/FTTUSDT/FTTUSDT-fundingRate-2022-11.zip",
}


async def probe(save: bool) -> None:
    """Reachability of the archive files this study needs; ``save`` refreshes the test fixtures."""
    _, rt = build_server()
    v = rt.binance_vision
    try:
        for name, path in PROBES.items():
            try:
                f = await v.http.get_bytes(path)
                print(f"OK  {path}: {len(f.data)} bytes", flush=True)
                if save:
                    (FIX / name).write_bytes(f.data)
            except ProviderError as e:
                print(f"ERR {path}: {e.reason}: {str(e.detail)[:160]}", flush=True)
        prefix = "data/spot/monthly/klines/ARBUSDT/1d/"
        f = await v.listing_http.get_bytes("/data.binance.vision", params={"delimiter": "/", "prefix": prefix})
        print(f"OK  s3 listing {prefix}: {len(f.data)} bytes", flush=True)
        if save:
            (FIX / "binance_vision_spot_listing_ARBUSDT_1d_2026-10-15.xml").write_bytes(f.data)
    finally:
        await rt.aclose()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["probe", "meta", "fetch0", "age", "stage0"])
    ap.add_argument("--save-fixtures", action="store_true", help="probe: overwrite the test fixtures with fresh samples")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/listing_drift")
    ap.add_argument("--sample", type=int, default=0)
    a = ap.parse_args()
    a.cache_dir = a.cache_dir.expanduser()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "probe":
        asyncio.run(probe(a.save_fixtures))
    elif a.stage == "meta":
        asyncio.run(stage_meta(a.cache_dir))
    elif a.stage == "fetch0":
        asyncio.run(stage_fetch0(a.cache_dir, a.sample))
    elif a.stage == "age":
        asyncio.run(stage_age(a.cache_dir, a.sample))
    elif a.stage == "stage0":
        stage0(a.cache_dir)


if __name__ == "__main__":
    main()
