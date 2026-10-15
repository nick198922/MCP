"""Funding crowding long/short: weekly market-neutral basket of USDT-M perps ranked by trailing funding (no spot).

Idea card: docs/ideas/IDEA-20261015-funding-crowding-longshort.md (sections "Тріаж критика", "Попередня реєстрація").

Data: the data.binance.vision archive (``rt.binance_vision``, via ``core.http.Http``) for daily klines and funding
settlements; it keeps SETTLING and fully removed symbols, so the long leg sees delisted coins. Metadata (TradFi filter,
status) from today's exchangeInfo through ccxt (``rt.exchanges``). No keys, no orders.

Stages (each caches per file under --cache-dir; a rerun resumes):
    meta     archive symbol list + exchangeInfo + months per symbol (``--meta-from`` copies them from another cache)
    probe    reachability of archive samples (``--save-fixtures`` writes tests/fixtures samples of a removed symbol)
    fetch    monthly 1d klines and fundingRate for every eligible symbol, 2021-10 .. 2026-09 (``--sample N`` for timing)
    stage0   data checks, coverage, power gate: n, SD and MDE of the basket price component and of the weekly rank IC
             -- no means (the point-in-time weekly panel is rebuilt from the cache by every analysis stage)
    full     the registered test; refuses to run unless stage0 passed at least one gate
    extra    exploratory descriptions added after the full run (legs, tails, survivorship comparison)

On 2026-10-15: gate (a) basket failed (MDE 1.49 pp > 1.0), gate (b) IC passed (0.023 <= 0.03); IC -0.004 (p 0.64);
net -2.0 %/yr on gross notional -> ``inconclusive`` (docs/verification/2026-10-15-funding-crowding-longshort.md).

    uv run python scripts/verify_funding_crowding.py <stage> --cache-dir ~/.cache/tradedesk-agents/funding_crowding
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import logging
import math
import random
import shutil
import statistics
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.funding_crowding import (
    Bars,
    Candidate,
    LegWeek,
    Pick,
    Week,
    WeekResult,
    beta_scales,
    block_bootstrap_ci,
    fm_slope,
    leg_week,
    liquidated,
    liquidation_capped,
    mde_nw,
    newey_west_se,
    ols_beta,
    random_pick,
    rank_pick,
    run_start_map,
    side_width,
    simulate,
    sma_regime,
    spearman,
    trailing_beta,
    week_candidate,
)
from tradedesk.analytics.funding_spread import normalize_asset
from tradedesk.analytics.listing_drift import DAY_MS, holm, symbol_eligible
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FIRST_RANK = int(datetime(2022, 1, 2, tzinfo=UTC).timestamp()) // 86400  # Sunday
LAST_RANK = int(datetime(2026, 9, 20, tzinfo=UTC).timestamp()) // 86400
FIRST_MONTH, LAST_MONTH = "2021-10", "2026-09"
SUB_START = int(datetime(2023, 9, 3, tzinfo=UTC).timestamp()) // 86400  # the card's original start (exploratory)
SPLIT = int(datetime(2024, 5, 19, tzinfo=UTC).timestamp()) // 86400  # second half starts here (registered)
BTC = "BTCUSDT"
GATE_BASKET = 0.010  # MDE of the basket price component, per week
GATE_IC = 0.03  # MDE of the weekly rank IC
NW_LAG = 4
SEED = 7


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


def rank_days() -> list[int]:
    return list(range(FIRST_RANK, LAST_RANK + 1, 7))


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
                    if e.reason == "data_unavailable":
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


def eligible(cache: Path) -> list[str]:
    syms, info = rd(cache / "symbols.json"), rd(cache / "exchange_info.json")["info"]
    return [s for s in syms if symbol_eligible(s, (info.get(s) or {}).get("contractType"), (info.get(s) or {}).get("underlyingType"))]


def months_of(cache: Path, sym: str) -> list[str]:
    p = cache / "listing" / f"{sym}.json"
    ms = rd(p) if p.exists() else None
    return [m for m in ms or [] if FIRST_MONTH <= m <= LAST_MONTH]


async def stage_meta(cache: Path, meta_from: Path | None) -> None:
    if meta_from:
        for name in ("symbols.json", "exchange_info.json"):
            if not (cache / name).exists() and (meta_from / name).exists():
                shutil.copy2(meta_from / name, cache / name)
        if (meta_from / "listing").exists() and not (cache / "listing").exists():
            shutil.copytree(meta_from / "listing", cache / "listing")
        print(f"metadata copied from {meta_from}", flush=True)
    _, rt = build_server()
    try:
        sp, ip = cache / "symbols.json", cache / "exchange_info.json"
        if not sp.exists():
            wr(sp, (await rt.binance_vision.um_symbols()).data)
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
        elig = eligible(cache)
        print(f"archive USDT-M symbols {len(rd(sp))}; eligible (USDT, COIN, no stables/wrapped/TradFi/index): {len(elig)}", flush=True)
        fx = Fetcher(cache, workers=6)
        jobs = []
        for s in elig:

            async def go(s: str = s) -> Any:
                f = await rt.binance_vision.list_dir(f"data/futures/um/monthly/klines/{s}/1d/")
                return sorted({k.rsplit("-", 2)[-2] + "-" + k.rsplit("-", 1)[-1][:2] for k in f.data["keys"] if k.endswith(".zip")})

            jobs.append((cache / "listing" / f"{s}.json", go))
        await fx.many("listing 1d", jobs)
        in_period = [s for s in elig if months_of(cache, s)]
        print(f"eligible symbols with 1d archive months in {FIRST_MONTH}..{LAST_MONTH}: {len(in_period)}", flush=True)
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- fetch


def kpath(cache: Path, sym: str, mo: str) -> Path:
    return cache / "k1d" / sym / f"{mo}.json"


def fpath(cache: Path, sym: str, mo: str) -> Path:
    return cache / "funding" / sym / f"{mo}.json"


async def stage_fetch(cache: Path, sample: int) -> None:
    syms = [s for s in eligible(cache) if months_of(cache, s)]
    if BTC not in syms:
        syms.append(BTC)
    if sample:
        syms = sorted(syms)[:sample]
    _, rt = build_server()
    fx = Fetcher(cache)
    try:
        jobs = []
        for s in syms:
            for mo in months_of(cache, s):
                y, m = map(int, mo.split("-"))

                async def gk(s: str = s, y: int = y, m: int = m) -> Any:
                    return (await rt.binance_vision.um_klines_monthly(s, "1d", y, m)).data

                async def gf(s: str = s, y: int = y, m: int = m) -> Any:
                    return (await rt.binance_vision.um_funding_monthly(s, y, m)).data

                jobs.append((kpath(cache, s, mo), gk))
                jobs.append((fpath(cache, s, mo), gf))
        print(f"symbols this run: {len(syms)}", flush=True)
        await fx.many("fetch", jobs)
    finally:
        await rt.aclose()


REMOVED_SAMPLE = ("RNDRUSDT", 2024, 6)  # renamed to RENDER and removed from exchangeInfo; still in the archive


async def stage_probe(save: bool) -> None:
    _, rt = build_server()
    v = rt.binance_vision
    s, y, m = REMOVED_SAMPLE
    try:
        for kind, path, name in (
            ("1d", f"/data/futures/um/monthly/klines/{s}/1d/{s}-1d-{y}-{m:02d}.zip", f"binance_vision_{s}-1d-{y}-{m:02d}_2026-10-15.zip"),
            (
                "funding",
                f"/data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{y}-{m:02d}.zip",
                f"binance_vision_{s}-fundingRate-{y}-{m:02d}_2026-10-15.zip",
            ),
        ):
            try:
                f = await v.http.get_bytes(path)
                print(f"OK  {kind} {path}: {len(f.data)} bytes", flush=True)
                if save:
                    (FIX / name).write_bytes(f.data)
            except ProviderError as e:
                print(f"ERR {path}: {e.reason}: {str(e.detail)[:160]}", flush=True)
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- panel


class SymData:
    def __init__(self, bars: Bars, funding: list[list[float]]) -> None:
        self.bars = bars
        self.funding = sorted(funding, key=lambda r: r[0])
        self.times = [int(r[0]) for r in self.funding]
        self.starts = run_start_map(bars)

    def fslice(self, lo_ms: int, hi_ms: int) -> list[list[float]]:
        return self.funding[bisect.bisect_left(self.times, lo_ms) : bisect.bisect_left(self.times, hi_ms)]


def load_sym(cache: Path, sym: str) -> SymData | None:
    rows: list[list[float]] = []
    fund: list[list[float]] = []
    nk = nf = 0
    for mo in months_of(cache, sym):
        kp, fp = kpath(cache, sym, mo), fpath(cache, sym, mo)
        if kp.exists():
            k = rd(kp)
            if k:
                rows.extend(k)
                nk += 1
        if fp.exists():
            f = rd(fp)
            if f:
                fund.extend(f)
                nf += 1
    if not rows:
        return None
    return SymData(Bars.from_rows(rows), fund)


def load_all(cache: Path) -> dict[str, SymData]:
    out = {}
    for s in sorted(set(eligible(cache)) | {BTC}):
        if not months_of(cache, s):
            continue
        d = load_sym(cache, s)
        if d is not None:
            out[s] = d
    return out


def build_weeks(data: dict[str, SymData], *, window_days: float = 7.0, hold: int = 7, step: int = 7) -> tuple[list[Week], dict[str, Any]]:
    """Point-in-time weekly universe (one symbol per asset) and every member's leg; diagnostics alongside."""
    weeks: list[Week] = []
    reasons: dict[int, Counter[str]] = {}
    hold_gaps = trunc = 0
    for day in range(FIRST_RANK, LAST_RANK + 1, step):
        t_ms = day * DAY_MS
        cands: dict[str, Candidate] = {}
        rc = reasons.setdefault(int(ymd(day)[:4]), Counter())
        for s, d in data.items():
            if day not in d.bars.open and (not d.bars.close or min(d.bars.close) > day or max(d.bars.close) < day - 30):
                continue  # not alive around t at all
            recs = d.fslice(t_ms - int((window_days + 3) * DAY_MS), t_ms)
            c, why = week_candidate(s, d.bars, recs, day, window_days=window_days, starts=d.starts)
            rc[why] += 1
            if c is not None:
                cands[s] = c
        # one symbol per asset: keep the one with the larger median turnover
        best: dict[str, Candidate] = {}
        for s, c in cands.items():
            a = normalize_asset(s[:-4], "binanceusdm")[0]
            if a not in best or c.median_qv > best[a].median_qv:
                best[a] = c
        uni = {c.symbol: c for c in best.values()}
        rc["dup_asset"] += len(cands) - len(uni)
        legs: dict[str, LegWeek] = {}
        for s in uni:
            d = data[s]
            lg = leg_week(s, d.bars, d.fslice(t_ms - 2 * DAY_MS, t_ms + (hold + 2) * DAY_MS), day, hold=hold)
            if lg is not None:
                legs[s] = lg
                hold_gaps += lg.hold_gaps > 0 and not lg.truncated
                trunc += lg.truncated
        weeks.append(Week(day, uni, legs))
    return weeks, {"reasons": reasons, "hold_gaps": hold_gaps, "truncated": trunc}


def main_pick(wk: Week) -> Pick:
    return rank_pick(wk, cap=10)


# ---------------------------------------------------------------- stage 0


def ic_series(weeks: list[Week]) -> tuple[list[float], list[float], list[int]]:
    ics, fms, ns = [], [], []
    for wk in weeks:
        ss = [s for s in wk.universe if s in wk.legs]
        if side_width(len(ss)) == 0:
            continue
        x = [wk.universe[s].signal for s in ss]
        y = [wk.legs[s].r for s in ss]
        ics.append(spearman(x, y))
        fms.append(fm_slope(x, y))
        ns.append(len(ss))
    return ics, fms, ns


def status_coverage(cache: Path, weeks: list[Week], data: dict[str, SymData]) -> None:
    info = rd(cache / "exchange_info.json")["info"]
    by_year: dict[str, Counter[str]] = {}
    sel_year: dict[str, Counter[str]] = {}
    for wk in weeks:
        y = ymd(wk.day)[:4]
        pk = main_pick(wk)
        for s in wk.universe:
            st = (info.get(s) or {}).get("status") or "REMOVED"
            by_year.setdefault(y, Counter())[st] += 1
        for s in pk.longs:
            sel_year.setdefault(y, Counter())["long_" + ((info.get(s) or {}).get("status") or "REMOVED")] += 1
    print("universe symbol-weeks by today's status (TRADING = still listed; SETTLING / REMOVED = delisted, archive only):")
    for y, c in sorted(by_year.items()):
        tot = sum(c.values())
        print(f"  {y}: {dict(c)}  delisted share {(tot - c['TRADING']) / tot:.1%}; long leg: {dict(sel_year.get(y, {}))}")


def coverage(weeks: list[Week], diag: dict[str, Any]) -> None:
    print("exclusion reasons per year (symbol-weeks alive around t):")
    for y, c in sorted(diag["reasons"].items()):
        liquid = c["ok"] + c["funding_gap"]
        print(f"  {y}: {dict(c)}; funding-gap share among liquid & aged: {c['funding_gap'] / liquid if liquid else 0:.2%}")
    no_leg = sum(len(wk.universe) - len(wk.legs) for wk in weeks)
    print(
        f"universe members without a leg (no entry open): {no_leg}; legs truncated by delisting: {diag['truncated']}; legs with funding holes while held (not truncated): {diag['hold_gaps']}"
    )
    ns = [len(wk.legs) for wk in weeks]
    print(
        f"weeks {len(weeks)}; universe N per week: min {min(ns)}, median {statistics.median(ns)}, max {max(ns)}; weeks with N < 50: {sum(n < 50 for n in ns)}"
    )
    by_year: dict[str, list[int]] = {}
    for wk in weeks:
        by_year.setdefault(ymd(wk.day)[:4], []).append(len(wk.legs))
    print("  median N by year:", {y: statistics.median(v) for y, v in sorted(by_year.items())})


def sanity(data: dict[str, SymData]) -> None:
    btc = data[BTC].bars
    d = int(datetime(2024, 12, 31, tzinfo=UTC).timestamp()) // 86400
    print(f"check BTCUSDT perp close 2024-12-31 = {btc.close.get(d)} (listing-drift / token-unlock reports: 93,548.9)")
    f = data.get("FTTUSDT")
    if f:
        lo = int(datetime(2022, 11, 1, tzinfo=UTC).timestamp() * 1000)
        hi = int(datetime(2022, 12, 1, tzinfo=UTC).timestamp() * 1000)
        nov = f.fslice(lo, hi)
        print(f"check FTTUSDT funding 2022-11: {len(nov)} settlements, min rate {min(r[2] for r in nov):.4f} (fixture: 126, -0.0250)")
    # ticker collisions / bad prints: day-over-day close moves >= x3 or <= 1/3 among universe-eligible symbols
    jumps = []
    for s, dd in data.items():
        cl = dd.bars.close
        for x in cl:
            if x - 1 in cl and FIRST_RANK - 60 <= x <= LAST_RANK + 7:
                q = cl[x] / cl[x - 1]
                if q >= 3 or q <= 1 / 3:
                    jumps.append(f"{s} {ymd(x)} x{q:.2f}")
    print(f"check day-over-day close moves >= x3 or <= x1/3 (squeezes, collapses or bad prints): {len(jumps)}: {jumps[:40]}")
    # funding intervals seen in the archive
    iv: Counter[float] = Counter()
    for dd in data.values():
        for r in dd.funding:
            iv[r[1]] += 1
    print(f"funding interval hours in the archive (settlement counts): {dict(sorted(iv.items()))}")


def stage_panel(cache: Path) -> tuple[list[Week], dict[str, Any], dict[str, SymData]]:
    t0 = time.time()
    data = load_all(cache)
    print(f"loaded {len(data)} symbols in {time.time() - t0:.0f}s; with funding: {sum(bool(d.funding) for d in data.values())}", flush=True)
    weeks, diag = build_weeks(data)
    print(f"panel built in {time.time() - t0:.0f}s", flush=True)
    return weeks, diag, data


def stage0(cache: Path) -> None:
    weeks, diag, data = stage_panel(cache)
    sanity(data)
    coverage(weeks, diag)
    status_coverage(cache, weeks, data)
    res = [r for r in simulate(weeks, main_pick) if r.n_side > 0]
    price = [r.price for r in res]
    fund = [r.funding for r in res]
    net = [r.net for r in res]
    ics, fms, ns = ic_series(weeks)
    print()
    print("STAGE 0 (no means): main cell width min(10, N/10), signal window 7 d, weekly, equal notional, gross-notional returns")
    print(f"basket weeks n={len(res)} (of {len(weeks)} rank days); side width: {dict(Counter(r.n_side for r in res))}")

    def line(label: str, xs: list[float], unit: float = 100.0, suf: str = "pp/week") -> str:
        return f"  {label:<34} SD={statistics.stdev(xs) * unit:7.3f} SE_NW={newey_west_se(xs, NW_LAG) * unit:6.3f} MDE={mde_nw(xs, NW_LAG) * unit:6.3f} {suf}"

    print(line("basket price component", price))
    print(line("basket funding component (info)", fund))
    print(line("basket net (info)", net))
    print(line("weekly rank IC (all universe)", ics, 1.0, "IC"))
    print(line("Fama-MacBeth slope (info)", fms, 100.0, "pp/week per 1 SD rank"))
    m_b, m_ic = mde_nw(price, NW_LAG), mde_nw(ics, NW_LAG)
    gate_b, gate_ic = m_b <= GATE_BASKET, m_ic <= GATE_IC
    print(f"IC weeks n={len(ics)}; universe N per IC week median {statistics.median(ns)}")
    print(f"GATE (a) basket price MDE {m_b * 100:.3f} pp <= {GATE_BASKET * 100:.1f} pp: {gate_b}")
    print(f"GATE (b) rank-IC MDE {m_ic:.4f} <= {GATE_IC:.2f}: {gate_ic}")
    verdict = (
        "PASS (both)" if gate_b and gate_ic else ("PASS (a) only" if gate_b else ("PASS (b) only" if gate_ic else "FAIL -> inconclusive"))
    )
    print(f"GATE: {verdict}")
    wr(
        cache / "stage0.json",
        {"mde_basket": m_b, "mde_ic": m_ic, "gate_basket": gate_b, "gate_ic": gate_ic, "n_weeks": len(res), "n_ic": len(ics)},
    )


# ---------------------------------------------------------------- full test


def pct(x: float) -> str:
    return f"{x * 100:+.3f}%"


def ann(x: float) -> str:
    return f"{x * 52 * 100:+.1f}%/yr"


def summarize(label: str, res: list[WeekResult], *, boot: bool = True) -> dict[str, float]:
    res = [r for r in res if r.n_side > 0]
    net = [r.net for r in res]
    m = statistics.fmean(net)
    se = newey_west_se(net, NW_LAG)
    lo, hi = block_bootstrap_ci(net, n_boot=10_000 if boot else 2000, seed=SEED)
    out = {
        "n": len(net),
        "net": m,
        "lo": lo,
        "hi": hi,
        "t": m / se if se > 0 else float("nan"),
        "price": statistics.fmean(r.price for r in res),
        "funding": statistics.fmean(r.funding for r in res),
        "cost": statistics.fmean(r.cost for r in res),
        "turn": statistics.fmean(r.turnover for r in res),
    }
    print(
        f"  {label:<40} n={out['n']:>3} net {pct(m)}/wk ({ann(m)}) CI95 [{pct(lo)}; {pct(hi)}] t_NW={out['t']:+.2f} | "
        f"price {pct(out['price'])} funding {pct(out['funding'])} cost {pct(out['cost'])} turnover {out['turn']:.2f}"
    )
    return out


def one_sided_p_from_t(t: float) -> float:
    return 0.5 * math.erfc(t / math.sqrt(2.0))


def stage_full(cache: Path, n_perm: int) -> None:
    s0 = rd(cache / "stage0.json") if (cache / "stage0.json").exists() else None
    if not s0 or not (s0["gate_basket"] or s0["gate_ic"]):
        print("stage0 has not passed (or not run): the registered rule stops here, no means are computed.")
        return
    weeks, diag, data = stage_panel(cache)
    btc = data[BTC].bars
    main = simulate(weeks, main_pick)
    res = [r for r in main if r.n_side > 0]
    print()
    print("=== MAIN CELL (registered): width min(10, N/10), window 7 d, weekly, equal notional, costs on actual turnover ===")
    S = summarize("net = price + funding - cost", main)
    net = [r.net for r in res]
    price = [r.price for r in res]
    fund = [r.funding for r in res]
    p_lo, p_hi = block_bootstrap_ci(price, seed=SEED)
    f_lo, f_hi = block_bootstrap_ci(fund, seed=SEED)
    print(f"  price component   {pct(statistics.fmean(price))}/wk ({ann(statistics.fmean(price))}) CI95 [{pct(p_lo)}; {pct(p_hi)}]")
    print(f"  funding component {pct(statistics.fmean(fund))}/wk ({ann(statistics.fmean(fund))}) CI95 [{pct(f_lo)}; {pct(f_hi)}]")
    ni = [p + 0.5 * f for p, f in zip(price, fund, strict=True)]
    n_lo, n_hi = block_bootstrap_ci(ni, seed=SEED)
    print(f"  H2 non-inferiority: price + 0.5*funding {pct(statistics.fmean(ni))}/wk CI95 [{pct(n_lo)}; {pct(n_hi)}]")
    print(
        f"  legs on own notional: long price {pct(statistics.fmean(r.long_price for r in res))} funding {pct(statistics.fmean(r.long_funding for r in res))}; "
        f"short price {pct(statistics.fmean(r.short_price for r in res))} funding {pct(statistics.fmean(r.short_funding for r in res))} (per week)"
    )
    print(f"  return on capital at 2x / 3x per leg: {ann(S['net'] * 2)} / {ann(S['net'] * 3)} (= k x gross-notional return)")

    # price test on the whole universe
    ics, fms, _ = ic_series(weeks)
    m_ic, se_ic = statistics.fmean(ics), newey_west_se(ics, NW_LAG)
    m_fm, se_fm = statistics.fmean(fms), newey_west_se(fms, NW_LAG)
    p_ic = 2 * one_sided_p_from_t(abs(m_ic / se_ic))
    print()
    print("=== PRICE ON THE WHOLE UNIVERSE (registered) ===")
    print(f"  mean weekly rank IC {m_ic:+.4f} SE_NW {se_ic:.4f} t {m_ic / se_ic:+.2f} p(two-sided) {p_ic:.4f} n={len(ics)}")
    print(f"  Fama-MacBeth slope {m_fm * 100:+.3f} pp/week per 1 SD rank, SE_NW {se_fm * 100:.3f}, t {m_fm / se_fm:+.2f}")
    for lab, sel in (("first half", lambda d: d < SPLIT), ("second half", lambda d: d >= SPLIT)):
        sub_w = [wk for wk in weeks if sel(wk.day)]
        si, _, _ = ic_series(sub_w)
        print(f"    IC {lab}: {statistics.fmean(si):+.4f} t {statistics.fmean(si) / newey_west_se(si, NW_LAG):+.2f} n={len(si)}")

    # permutations
    print()
    print(f"=== PERMUTATIONS ({n_perm}, seed {SEED}): random baskets of the same shape from the same universe ===")
    rng = random.Random(SEED)
    obs = S["net"]
    perm = []
    t0 = time.time()
    for _ in range(n_perm):
        pr = [r.net for r in simulate(weeks, lambda wk: random_pick(wk, rng)) if r.n_side > 0]
        perm.append(statistics.fmean(pr))
    p_perm = (1 + sum(x >= obs for x in perm)) / (1 + n_perm)
    perm.sort()
    print(
        f"  observed {pct(obs)}; permutation mean {pct(statistics.fmean(perm))}, 95th pct {pct(perm[int(0.95 * n_perm)])}; p(one-sided) {p_perm:.4f} ({time.time() - t0:.0f}s)"
    )

    # H3 robustness
    print()
    print("=== H3 ROBUSTNESS (registered) ===")
    halves = {}
    for lab, sel in (
        ("first half 2022-01-02..2024-05-12", lambda d: d < SPLIT),
        ("second half 2024-05-19..2026-09-20", lambda d: d >= SPLIT),
    ):
        sub = [r for r in res if sel(r.day)]
        halves[lab] = statistics.fmean(r.net for r in sub)
        print(
            f"  {lab}: n={len(sub)} net {pct(halves[lab])}/wk ({ann(halves[lab])}) price {pct(statistics.fmean(r.price for r in sub))} funding {pct(statistics.fmean(r.funding for r in sub))}"
        )
    srt = sorted(net)
    no_top = statistics.fmean(srt[:-10])
    print(f"  without the 10 best weeks: {pct(no_top)}/wk ({ann(no_top)})")
    by_year: dict[str, list[WeekResult]] = {}
    for r in res:
        by_year.setdefault(ymd(r.day)[:4], []).append(r)
    for y, rr in sorted(by_year.items()):
        print(
            f"  {y}: n={len(rr)} net {pct(statistics.fmean(r.net for r in rr))} price {pct(statistics.fmean(r.price for r in rr))} "
            f"funding {pct(statistics.fmean(r.funding for r in rr))} cost {pct(statistics.fmean(r.cost for r in rr))}"
        )
    crash = int(datetime(2025, 10, 5, tzinfo=UTC).timestamp()) // 86400
    wo = [r.net for r in res if r.day != crash]
    print(
        f"  without the week of 2025-10-05 (contains 2025-10-10/11): {pct(statistics.fmean(wo))}/wk; that week {pct(next((r.net for r in res if r.day == crash), float('nan')))}"
    )
    # beta and regime
    btc_w = {wk.day: (btc.close[wk.day + 6] / btc.open[wk.day] - 1.0) for wk in weeks if wk.day in btc.open and wk.day + 6 in btc.close}
    pairs = [(r.net, btc_w[r.day]) for r in res if r.day in btc_w]
    print(
        f"  basket beta to BTC weekly return: net {ols_beta([a for a, _ in pairs], [b for _, b in pairs]):+.3f}; "
        f"price {ols_beta([r.price for r in res if r.day in btc_w], [btc_w[r.day] for r in res if r.day in btc_w]):+.3f}"
    )
    for lab, g in (("BTC above 200d SMA", 1), ("BTC below 200d SMA", -1)):
        sub = [r for r in res if sma_regime(btc, r.day) == g]
        if sub:
            print(
                f"  {lab}: n={len(sub)} net {pct(statistics.fmean(r.net for r in sub))} price {pct(statistics.fmean(r.price for r in sub))} funding {pct(statistics.fmean(r.funding for r in sub))}"
            )

    # tails
    print()
    print("=== TAILS ===")
    worst = min(res, key=lambda r: r.net)
    print(f"  worst week {ymd(worst.day)}: net {pct(worst.net)} price {pct(worst.price)}; best week {pct(max(net))}")
    liq = Counter()
    legs_n = 0
    big = 0
    mae_s, mae_l = [], []
    for wk in weeks:
        pk = main_pick(wk)
        if not pk.longs:
            continue
        moved = False
        for s in pk.longs:
            lg = wk.legs[s]
            legs_n += 1
            mae_l.append(lg.down)
            for k in (2, 3):
                liq[f"long {k}x"] += liquidated(lg, +1, k)
            moved |= abs(lg.r) >= 0.5
        for s in pk.shorts:
            lg = wk.legs[s]
            legs_n += 1
            mae_s.append(lg.up)
            for k in (2, 3):
                liq[f"short {k}x"] += liquidated(lg, -1, k)
            moved |= abs(lg.r) >= 0.5
        big += moved
    half = legs_n // 2
    print(
        "  leg liquidations within a week (adverse move >= 1/k - 2.5 %): "
        + ", ".join(f"{k} {v} ({v / half:.2%})" for k, v in sorted(liq.items()))
    )
    print(
        f"  leg MAE: short max-up p95 {sorted(mae_s)[int(0.95 * len(mae_s))]:.1%} max {max(mae_s):.1%}; long max-down p95 {sorted(mae_l)[int(0.95 * len(mae_l))]:.1%} max {max(mae_l):.1%}"
    )
    print(f"  weeks with a basket coin moving >= +-50 %: {big}")

    # costs
    print()
    print("=== COST STRESS (main cell) ===")
    for lab, kw in (
        ("fee 0.02 % (maker), slippage x1", {"fee": 0.0002}),
        ("slippage x2", {"slip_mult": 2.0}),
        ("slippage x3", {"slip_mult": 3.0}),
    ):
        summarize(lab, simulate(weeks, main_pick, **kw), boot=False)

    # realistic constraint: isolated legs liquidated (added after seeing the MYX 2025 tail in the data checks,
    # before any mean was computed; exploratory, not part of the verdict)
    print()
    print("=== CONSTRAINT: isolated legs liquidated at k (exploratory; added after the tail check, before means) ===")
    for k in (1.0, 2.0, 3.0):
        capped = [liquidation_capped(wk, main_pick(wk), k) for wk in weeks]
        o = summarize(f"legs isolated at {k:.0f}x (notional)", simulate(capped, main_pick), boot=False)
        print(f"    return on capital at {k:.0f}x: {ann(o['net'] * k)} (= k x notional)")

    # exploratory grid
    print()
    print("=== EXPLORATORY (registered list, Holm across; does not change the verdict) ===")
    expl: list[tuple[str, dict[str, float]]] = []
    for cap in (5, 20):
        expl.append(
            (f"width cap {cap}", summarize(f"width cap {cap}", simulate(weeks, lambda wk, c=cap: rank_pick(wk, cap=c)), boot=False))
        )
    for w in (3.0, 14.0):
        wk_w, _ = build_weeks(data, window_days=w)
        expl.append((f"signal window {w:.0f} d", summarize(f"signal window {w:.0f} d", simulate(wk_w, main_pick), boot=False)))
    wk2, _ = build_weeks(data, hold=14, step=14)
    r2 = simulate(wk2, main_pick)
    expl.append(("biweekly (per 2 weeks)", summarize("biweekly (returns per 2 weeks)", r2, boot=False)))

    def beta_pick(wk: Week) -> Pick:
        pk = main_pick(wk)
        if not pk.longs:
            return pk
        bl = [b for s in pk.longs if (b := trailing_beta(data[s].bars, btc, wk.day)) is not None]
        bs = [b for s in pk.shorts if (b := trailing_beta(data[s].bars, btc, wk.day)) is not None]
        ls, ss = beta_scales(statistics.fmean(bl) if bl else None, statistics.fmean(bs) if bs else None)
        return Pick(pk.longs, pk.shorts, ls, ss)

    bres = simulate(weeks, beta_pick)
    expl.append(("beta-neutral (trailing 60d)", summarize("beta-neutral (trailing 60d beta)", bres, boot=False)))
    bp = [(r.net, btc_w[r.day]) for r in bres if r.n_side > 0 and r.day in btc_w]
    print(f"    beta-neutral basket beta to BTC: {ols_beta([a for a, _ in bp], [b for _, b in bp]):+.3f}")
    sub = [wk for wk in weeks if wk.day >= SUB_START]
    expl.append(("sub-period from 2023-09-03", summarize("sub-period from 2023-09-03 (card)", simulate(sub, main_pick), boot=False)))
    ps = [one_sided_p_from_t(o["t"]) for _, o in expl]
    for (lab, _), p, ph in zip(expl, ps, holm(ps), strict=True):
        print(f"    {lab:<32} one-sided p {p:.4f} Holm {ph:.4f}")

    # verdict
    print()
    print("=== REGISTERED VERDICT INPUTS ===")
    yr = 52
    fa = statistics.fmean(fund) * yr
    print(f"  funding component {fa:+.1%}/yr (kill (a) if < 10 %)")
    print(
        f"  net {S['net'] * yr:+.1%}/yr, CI95 [{S['lo'] * yr:+.1%}; {S['hi'] * yr:+.1%}] (H1: >= +20 % and lower > 0; kill (b): upper < +10 %)"
    )
    print(f"  permutation p {p_perm:.4f} (kill (c) if > 0.05)")
    print(f"  halves {', '.join(f'{v * yr:+.1%}/yr' for v in halves.values())}; without top-10 {no_top * yr:+.1%}/yr (kill (d))")
    print(f"  worst week {worst.net:+.2%} (kill (e) if < -15 % and net < +20 %/yr)")
    print(f"  IC {m_ic:+.4f} p {p_ic:.4f}; gate (a) basket passed: {s0['gate_basket']}, gate (b) IC passed: {s0['gate_ic']}")


def stage_extra(cache: Path) -> None:
    """Exploratory descriptions added after the full run (labelled as such in the report; no verdict depends on them)."""
    weeks, _, _ = stage_panel(cache)
    info = rd(cache / "exchange_info.json")["info"]
    res = [r for r in simulate(weeks, main_pick) if r.n_side > 0]
    print()
    print("=== EXTRA (exploratory, after the full run) ===")
    for lab, xs in (
        ("long leg alone, price + funding, own notional, before costs", [r.long_price + r.long_funding for r in res]),
        ("short leg alone, price + funding, own notional, before costs", [r.short_price + r.short_funding for r in res]),
        ("long leg funding only", [r.long_funding for r in res]),
        ("short leg funding only", [r.short_funding for r in res]),
    ):
        lo, hi = block_bootstrap_ci(xs, n_boot=5000, seed=SEED)
        srt = sorted(xs)
        print(
            f"  {lab:<62} mean {pct(statistics.fmean(xs))}/wk CI95 [{pct(lo)}; {pct(hi)}] median {pct(statistics.median(xs))} "
            f"without top-5 weeks {pct(statistics.fmean(srt[:-5]))}"
        )
    fund = [r.funding for r in res]
    print(
        f"  basket funding component: mean {ann(statistics.fmean(fund))}, median {ann(statistics.median(fund))}, "
        f"without top-10 weeks {ann(statistics.fmean(sorted(fund)[:-10]))}"
    )
    myx = {wk.day for wk in weeks if "MYXUSDT" in (main_pick(wk).longs + main_pick(wk).shorts)}
    sub = [r for r in res if r.day not in myx]
    print(
        f"  weeks with MYXUSDT in the basket: {len(myx)}; without them: net {ann(statistics.fmean(r.net for r in sub))} "
        f"price {ann(statistics.fmean(r.price for r in sub))} funding {ann(statistics.fmean(r.funding for r in sub))}"
    )
    top = sorted(res, key=lambda r: abs(r.net), reverse=True)[:8]
    for r in top:
        wk = next(w for w in weeks if w.day == r.day)
        pk = main_pick(wk)
        big = sorted(pk.longs + pk.shorts, key=lambda s: abs(wk.legs[s].r + wk.legs[s].funding_long), reverse=True)[:2]
        print(f"    |net| top: {ymd(r.day)} net {pct(r.net)} price {pct(r.price)} funding {pct(r.funding)}; biggest legs {big}")
    # survivorship: what a today's-listings-only universe (ccxt view) would show
    alive = [
        Week(wk.day, {s: c for s, c in wk.universe.items() if (info.get(s) or {}).get("status") == "TRADING"}, wk.legs) for wk in weeks
    ]
    print("  survivorship comparison (universe restricted to symbols TRADING on 2026-10-15):")
    summarize("TRADING-only universe", simulate(alive, main_pick), boot=False)
    ra = [r for r in simulate(alive, main_pick) if r.n_side > 0]
    print(
        f"    legs: long price {pct(statistics.fmean(r.long_price for r in ra))} funding {pct(statistics.fmean(r.long_funding for r in ra))}; "
        f"short price {pct(statistics.fmean(r.short_price for r in ra))} funding {pct(statistics.fmean(r.short_funding for r in ra))}"
    )
    ics, _, _ = ic_series(alive)
    print(f"    rank IC on TRADING-only universe {statistics.fmean(ics):+.4f} t {statistics.fmean(ics) / newey_west_se(ics, NW_LAG):+.2f}")
    # how persistent is the signal: realised next-week funding of the ranked sides vs the trailing signal
    sig_s, sig_l, real_s, real_l = [], [], [], []
    for wk in weeks:
        pk = main_pick(wk)
        if not pk.longs:
            continue
        sig_s.append(statistics.fmean(wk.universe[s].signal for s in pk.shorts))
        sig_l.append(statistics.fmean(wk.universe[s].signal for s in pk.longs))
        real_s.append(statistics.fmean(-wk.legs[s].funding_long for s in pk.shorts) * 52)
        real_l.append(statistics.fmean(-wk.legs[s].funding_long for s in pk.longs) * 52)
    print(
        f"  signal persistence (annualised funding paid by a long): short side trailing median {statistics.median(sig_s):+.1%} -> "
        f"next week median {statistics.median(real_s):+.1%}; long side trailing median {statistics.median(sig_l):+.1%} -> next week median {statistics.median(real_l):+.1%}"
    )


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["meta", "probe", "fetch", "stage0", "full", "extra"])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--meta-from", type=Path, default=None, help="meta: copy symbols/exchangeInfo/listing from this cache")
    ap.add_argument("--save-fixtures", action="store_true")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--perm", type=int, default=1000)
    a = ap.parse_args()
    a.cache_dir = a.cache_dir.expanduser()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "meta":
        asyncio.run(stage_meta(a.cache_dir, a.meta_from.expanduser() if a.meta_from else None))
    elif a.stage == "probe":
        asyncio.run(stage_probe(a.save_fixtures))
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(a.cache_dir, a.sample))
    elif a.stage == "stage0":
        stage0(a.cache_dir)
    elif a.stage == "full":
        stage_full(a.cache_dir, a.perm)
    elif a.stage == "extra":
        stage_extra(a.cache_dir)


if __name__ == "__main__":
    main()
