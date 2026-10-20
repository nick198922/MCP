"""Funding settlement window on Binance USDT-M: does a 4-minute trade through the settlement, on the side that receives
an extreme funding, earn the funding net of the price move and costs?

Idea card: docs/ideas/IDEA-20261020-funding-settlement-window.md ("Тріаж критика", "Попередня реєстрація").

Data: the data.binance.vision archive (``rt.binance_vision``; keeps delisted symbols): 1m perp klines and 1m
``premiumIndexKlines`` on candidate symbol-days (new cache), plus the ``fundingRate`` archive, 1d klines and today's
exchangeInfo already cached by ``verify_funding_crowding.py`` (``--fc-cache``). No keys, no orders.

Stages (each caches per file; a rerun resumes):
    count    universe and candidate symbol-days from the funding archive (no prices); consistency with the triage table
    fetch    1m perp klines + 1m premium index for candidate days and the day before (``--sample N`` symbols for timing)
    build    events: predicted funding at entry, trades, placebo and H2 windows -> events.jsonl (prints no outcomes)
    stage0   signal vs settled funding, sanity checks, power gate on the PLACEBO only (no event outcomes)
    full     the registered test; refuses to run unless stage0 passed the gate

    uv run python scripts/verify_funding_settlement.py <stage> --cache-dir ~/.cache/tradedesk-agents/funding_settlement \
        --log ~/.cache/tradedesk-agents/funding_settlement/<stage>.log
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import logging
import math
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.funding_settlement import (
    DAY_MS,
    FEE_TAKER,
    MIN_MS,
    band,
    cap_from_history,
    final_prediction,
    interval_start,
    predicted_funding,
    receiver_side,
    round_trip_cost,
    scale_mode,
    settlement_trade,
    trailing_median,
    window_return,
)
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_ci, cluster_bootstrap_p
from tradedesk.analytics.listing_drift import cr1_se, symbol_eligible
from tradedesk.analytics.per_asset import asset_stats, cluster_robust_q, permutation_q, persistence
from tradedesk.analytics.premium_spike import top_cluster_drop
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

START = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
END = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1000)
PRESCREEN = 0.001  # |settled funding| on the day (candidate days only; events are defined by the predicted rate)
MAIN = 0.003
MIN_TURNOVER = 1e6
MIN_HISTORY_DAYS = 7
GATE_MDE = 0.0010
IS_END = int(datetime(2025, 7, 1, tzinfo=UTC).timestamp() * 1000)
H2026_1 = (int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000), int(datetime(2026, 7, 1, tzinfo=UTC).timestamp() * 1000))
SLICES = [
    ("2024", datetime(2024, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)),
    ("2025-H1", datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 7, 1, tzinfo=UTC)),
    ("2025-H2", datetime(2025, 7, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
    ("2026-H1", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)),
    ("2026-Q3", datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
]


class Tee:
    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, s: str) -> int:
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self) -> None:
        for st in self.streams:
            st.flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(p)


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


def ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def pct(x: float | None, nd: int = 3) -> str:
    return "nan" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{nd}f}%"


# ---------------------------------------------------------------- funding-crowding cache (funding, 1d, exchangeInfo)


def load_funding(fc: Path, sym: str) -> list[tuple[int, float, float]]:
    """[(settlement minute ms, interval hours, rate)] sorted; calc_time floored to the minute."""
    d = fc / "funding" / sym
    out: dict[int, tuple[int, float, float]] = {}
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        for t, h, r in rd(p) or []:
            m = int(t) // MIN_MS * MIN_MS
            out[m] = (m, float(h), float(r))
    return [out[k] for k in sorted(out)]


def load_daily(fc: Path, sym: str) -> tuple[dict[int, float], dict[int, float], int | None]:
    """(quote volume by day, close by day, first day with base volume > 0)."""
    d = fc / "k1d" / sym
    qv: dict[int, float] = {}
    close: dict[int, float] = {}
    first = None
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        for r in rd(p) or []:
            day = int(r[0]) // DAY_MS
            qv[day] = float(r[6])
            close[day] = float(r[4])
            if r[5] > 0 and (first is None or day < first):
                first = day
    return qv, close, first


def universe(fc: Path) -> tuple[list[str], Counter[str]]:
    info = rd(fc / "exchange_info.json")["info"]
    syms = sorted(p.name for p in (fc / "funding").iterdir())
    ok, why = [], Counter()
    for s in syms:
        i = info.get(s) or {}
        if symbol_eligible(s, i.get("contractType"), i.get("underlyingType")):
            ok.append(s)
        else:
            why["tradfi/index/stable/other" if s.endswith("USDT") else "not USDT"] += 1
    return ok, why


def candidate_days(fc: Path, syms: list[str]) -> dict[str, list[int]]:
    out = {}
    for s in syms:
        days = {t // DAY_MS for t, _h, r in load_funding(fc, s) if START <= t < END and abs(r) >= PRESCREEN}
        if days:
            out[s] = sorted(days)
    return out


def stage_count(fc: Path, cache: Path) -> None:
    syms, why = universe(fc)
    print(f"funding archive symbols {len(list((fc / 'funding').iterdir()))}; eligible {len(syms)}; excluded {dict(why)}")
    # consistency with the critic's triage table (all 678 symbols, settled |funding|, 2024-01 .. 2026-09)
    allsyms = sorted(p.name for p in (fc / "funding").iterdir())
    for th in (0.001, 0.002, 0.003):
        n = pos = 0
        moments: set[int] = set()
        days: set[int] = set()
        ss: set[str] = set()
        for s in allsyms:
            for t, _h, r in load_funding(fc, s):
                if START <= t < END and abs(r) >= th:
                    n += 1
                    pos += r > 0
                    moments.add(t)
                    days.add(t // DAY_MS)
                    ss.add(s)
        print(
            f"  settled |F| >= {th * 100:.2f}%: events {n}, > 0 {pos} ({pos / max(n, 1):.0%}), moments {len(moments)}, days {len(days)}, symbols {len(ss)}"
        )
    cand = candidate_days(fc, syms)
    n_d = sum(len(v) for v in cand.values())
    need = {(s, d) for s, v in cand.items() for d in v} | {(s, d - 1) for s, v in cand.items() for d in v}
    print(
        f"candidate symbol-days (eligible, settled |F| >= {PRESCREEN * 100:.2f}%): {n_d} on {len(cand)} symbols; with previous days {len(need)}"
    )
    print(f"files to fetch (perp 1m + premium 1m): {2 * len(need)}")
    wr(cache / "candidates.json", cand)
    # 1d coverage in the funding-crowding cache
    miss = 0
    for s, v in cand.items():
        qv, _c, _f = load_daily(fc, s)
        miss += sum(1 for d in v if d - 1 not in qv)
    print(f"candidate days without a 1d bar on the day before (turnover gaps): {miss}")


# ---------------------------------------------------------------- fetch


class Fetcher:
    """Per-file JSON cache; ``null`` marks a file the archive does not have (404)."""

    def __init__(self, workers: int = 16) -> None:
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
                            print(f"  error {path.name}: {e.reason}: {str(e.detail)[:200]}", flush=True)
                        return
                    await asyncio.sleep(5 * (k + 1))
        await asyncio.to_thread(wr, path, data)
        self.done += 1

    async def many(self, label: str, jobs: list[tuple[Path, Callable[[], Awaitable[Any]]]]) -> None:
        todo = [(p, g) for p, g in jobs if not p.exists()]
        print(f"[{label}] {len(jobs)} files, {len(todo)} to fetch", flush=True)
        t0 = time.time()
        self.done = 0
        step = max(50, len(todo) // 40)
        it = iter(todo)

        async def worker() -> None:
            for p, g in it:
                await self.one(p, g)
                if self.done and self.done % step == 0:
                    el = time.time() - t0
                    print(
                        f"  [{label}] {self.done}/{len(todo)} in {el:.0f}s, eta {el / self.done * (len(todo) - self.done):.0f}s, rss {rss_mb():.0f} MB",
                        flush=True,
                    )

        await asyncio.gather(*[worker() for _ in range(16)])
        print(f"[{label}] done {self.done} in {time.time() - t0:.0f}s; transient errors {dict(self.errors)}", flush=True)


def pk_path(cache: Path, sym: str, day: int) -> Path:
    return cache / "pk1m" / sym / f"{ymd(day)}.json"


def pi_path(cache: Path, sym: str, day: int) -> Path:
    return cache / "pi1m" / sym / f"{ymd(day)}.json"


async def stage_fetch(cache: Path, sample: int) -> None:
    cand = rd(cache / "candidates.json")
    syms = sorted(cand)
    if sample:
        syms = syms[:: max(1, len(syms) // sample)][:sample]
    _, rt = build_server()
    v = rt.binance_vision
    try:
        jobs: list[tuple[Path, Callable[[], Awaitable[Any]]]] = []
        for s in syms:
            need = sorted(set(cand[s]) | {d - 1 for d in cand[s]})
            for d in need:

                async def gk(s: str = s, d: int = d) -> Any:
                    rows = (await v.um_klines_daily(s, "1m", ymd(d))).data
                    return [[int(r[0]), r[1], r[2], r[3], r[4], r[6]] for r in rows]

                async def gp(s: str = s, d: int = d) -> Any:
                    rows = (await v.um_premium_index_daily(s, "1m", ymd(d))).data
                    return [[int(r[0]), r[4]] for r in rows]

                jobs.append((pk_path(cache, s, d), gk))
                jobs.append((pi_path(cache, s, d), gp))
        await Fetcher().many(f"1m perp + premium, {len(syms)} symbols", jobs)
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- events


def load_days(cache: Path, sym: str, days: list[int]) -> tuple[dict[int, tuple[float, ...]], dict[int, float], set[int], set[int]]:
    bars: dict[int, tuple[float, ...]] = {}
    prem: dict[int, float] = {}
    have_k: set[int] = set()
    have_p: set[int] = set()
    for d in days:
        pk = pk_path(cache, sym, d)
        if pk.exists():
            rows = rd(pk)
            if rows:
                have_k.add(d)
                for t, o, h, lo, c, q in rows:
                    bars[int(t)] = (o, h, lo, c, q)
        pp = pi_path(cache, sym, d)
        if pp.exists():
            rows = rd(pp)
            if rows:
                have_p.add(d)
                for t, c in rows:
                    prem[int(t)] = float(c)
    return bars, prem, have_k, have_p


def stage_build(fc: Path, cache: Path) -> None:
    cand = rd(cache / "candidates.json")
    out = (cache / "events.jsonl").open("w")
    why: Counter[str] = Counter()
    n_ev = 0
    t0 = time.time()
    for i, s in enumerate(sorted(cand)):
        cdays = set(cand[s])
        fund = load_funding(fc, s)
        qv, close1d, first_day = load_daily(fc, s)
        hist = [(t, r) for t, _h, r in fund]
        hist_t = [t for t, _r in hist]
        prev_of = {fund[k][0]: (fund[k - 1][0] if k else None) for k in range(len(fund))}
        need = sorted(cdays | {d - 1 for d in cdays})
        # load in monthly chunks to bound memory
        chunks: dict[int, list[int]] = defaultdict(list)
        for d in need:
            chunks[d // 31].append(d)
        settle = [(t, h, r) for t, h, r in fund if START <= t < END and t // DAY_MS in cdays]
        for key in sorted(chunks):
            span = set(chunks[key])
            days_here = [d for d in sorted(span) if d in cdays]
            if not days_here:
                continue
            load = sorted(set(days_here) | {d - 1 for d in days_here})
            bars, prem, have_k, have_p = load_days(cache, s, load)
            for t, h, r in settle:
                d = t // DAY_MS
                if d not in span or d not in cdays:
                    continue
                if d not in have_k or d not in have_p:
                    # a missing day before only shortens premium coverage (checked by predicted_funding)
                    why["no 1m data on the day"] += 1
                    continue
                start = interval_start(t, h, prev_of.get(t))
                entry = t - 3 * MIN_MS
                lo_i, hi_i = bisect.bisect_left(hist_t, t - 30 * DAY_MS), bisect.bisect_left(hist_t, t)
                cap = cap_from_history(hist[lo_i:hi_i], t)
                raw = predicted_funding(prem, start, entry, h)
                if raw is None:
                    why["premium coverage < 50%"] += 1
                    continue
                raw_unw = predicted_funding(prem, start, entry, h, weighted=False)
                side = receiver_side(raw)
                if side == 0:
                    side = 1
                tr = settlement_trade(bars, t, side, r)
                if tr is None:
                    why["missing entry/exit bar"] += 1
                    continue
                trw = settlement_trade(bars, t, side, r, worse=True)
                to7, n7 = trailing_median(qv, d)
                plc = [window_return(bars, side, t + o - 3 * MIN_MS, t + o + MIN_MS) for o in (-30 * MIN_MS, 30 * MIN_MS)]
                plc_ok = [x for x in plc if x is not None]
                pre = window_return(bars, side, t - 16 * MIN_MS, t - MIN_MS)
                post = window_return(bars, side, t - MIN_MS, t + 14 * MIN_MS)
                eb = bars.get(entry)
                ev = {
                    "s": s,
                    "t": t,
                    "h": h,
                    "F": r,
                    "raw": raw,
                    "raw_unw": raw_unw,
                    "cap": cap,
                    "side": side,
                    "to7": to7,
                    "n7": n7,
                    "age": (d - first_day) if first_day is not None else None,
                    "pr": tr.price_ret,
                    "fund": tr.funding,
                    "mae": tr.mae,
                    "pr_w": trw.price_ret if trw else None,
                    "plc": statistics.fmean(plc_ok) if plc_ok else None,
                    "pre": pre,
                    "post": post,
                    "eq": eb[4] if eb else None,
                    "p_in": tr.p_in,
                    "c1d_prev": close1d.get(d - 1),
                }
                out.write(json.dumps(ev, separators=(",", ":")) + "\n")
                n_ev += 1
            del bars, prem
        if (i + 1) % 25 == 0:
            print(f"  build {i + 1}/{len(cand)} symbols, events {n_ev}, {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB", flush=True)
    out.close()
    print(f"events written {n_ev}; skipped {dict(why)}; peak rss {rss_mb():.0f} MB")


def events(cache: Path) -> Iterator[dict[str, Any]]:
    with (cache / "events.jsonl").open() as f:
        for line in f:
            yield json.loads(line)


def eligible(e: dict[str, Any]) -> str | None:
    """None if the event passes the registered filters, else the reason."""
    if e["age"] is None or e["age"] < MIN_HISTORY_DAYS:
        return "history < 7 days"
    if e["to7"] is None or e["to7"] < MIN_TURNOVER:
        return "7d median turnover < $1M"
    return None


def cost1(e: dict[str, Any]) -> float:
    return round_trip_cost(e["to7"])


def net(e: dict[str, Any], mult: float = 2.0, *, worse: bool = False, fees_only: bool = False) -> float:
    pr = e["pr_w"] if worse else e["pr"]
    c = 2 * FEE_TAKER if fees_only else cost1(e)
    return pr + e["fund"] - mult * c


def day(e: dict[str, Any]) -> int:
    return e["t"] // DAY_MS


def main_cell(e: dict[str, Any]) -> bool:
    return e["pred"] <= -MAIN


def short_cell(e: dict[str, Any]) -> bool:
    return e["pred"] >= MAIN


def assign_signals(allrows: list[dict[str, Any]]) -> Counter[str]:
    """Final predicted funding per settlement, in time order. The scaling mode (raw vs raw x hours/8 for non-8h symbols)
    comes from settlements published before T: the symbol's last 12 non-8h settlements within 7 days (|F| >= 0.05 %,
    not at the cap), else the last 200 such settlements of all symbols, else ``unscaled`` (the registered formula)."""
    allrows.sort(key=lambda e: (e["t"], e["s"]))
    per: dict[str, deque[tuple[int, float, float, float]]] = defaultdict(lambda: deque(maxlen=12))
    glob: deque[tuple[int, float, float, float]] = deque(maxlen=200)
    src: Counter[str] = Counter()
    i = 0
    while i < len(allrows):
        j = i
        t = allrows[i]["t"]
        while j < len(allrows) and allrows[j]["t"] == t:
            j += 1
        batch = allrows[i:j]
        for e in batch:
            hist = [(h, r, f) for tt, h, r, f in per[e["s"]] if t - tt <= 7 * DAY_MS]
            mode = scale_mode(hist)
            if mode is not None:
                src["symbol " + mode] += 1
            else:
                mode = scale_mode([(h, r, f) for _tt, h, r, f in glob])
                src[("global " + mode) if mode else "default unscaled"] += 1
                mode = mode or "unscaled"
            e["mode"] = mode
            e["pred"] = final_prediction(e["raw"], e["h"], mode, e["cap"])
            e["pred_nocap"] = final_prediction(e["raw"], e["h"], mode, None)
            e["pred_unw"] = final_prediction(e["raw_unw"], e["h"], mode, e["cap"]) if e["raw_unw"] is not None else e["pred"]
        for e in batch:  # published at T: usable from the next settlement on
            at_cap = e["cap"] is not None and abs(abs(e["F"]) - e["cap"]) <= 1e-12
            if e["h"] != 8 and abs(e["F"]) >= 0.0005 and not at_cap:
                rec = (t, e["h"], e["raw"], e["F"])
                per[e["s"]].append(rec)
                glob.append(rec)
        i = j
    return src


def load_eligible(cache: Path) -> tuple[list[dict[str, Any]], Counter[str]]:
    allrows = list(events(cache))
    src = assign_signals(allrows)
    print(f"scaling mode source: {dict(src)}")
    rows, why = [], Counter()
    for e in allrows:
        r = eligible(e)
        if r:
            if abs(e["pred"]) >= 0.001:
                why[r] += 1
            continue
        rows.append(e)
    return rows, why


# ---------------------------------------------------------------- stage 0 (no event outcomes)


def stage0(fc: Path, cache: Path) -> None:
    rows, why = load_eligible(cache)
    print(f"eligible settlements in events.jsonl {len(rows)}; excluded with |pred| >= 0.10%: {dict(why)}; rss {rss_mb():.0f} MB")
    # signal vs settled funding
    big = [e for e in rows if abs(e["pred"]) >= 0.001 or abs(e["F"]) >= 0.001]
    p = np.array([e["pred"] for e in big])
    f = np.array([e["F"] for e in big])
    print(f"\nSIGNAL vs SETTLED (|pred| or |F| >= 0.10%, n={len(big)}):")
    print(f"  corr {np.corrcoef(p, f)[0, 1]:.3f}; same sign {np.mean(np.sign(p) == np.sign(f)):.3f}")
    ok = np.abs(f) > 1e-6
    print(
        f"  median pred/F {np.median(p[ok] / f[ok]):.3f}; median |pred-F| {np.median(np.abs(p - f)) * 100:.4f}pp; p90 {np.quantile(np.abs(p - f), 0.9) * 100:.4f}pp"
    )
    for lab, key in (("cap-clipped", "pred"), ("no cap", "pred_nocap"), ("unweighted", "pred_unw")):
        q = np.array([e[key] for e in big])
        print(f"  {lab:<12} median |pred-F| {np.median(np.abs(q - f)) * 100:.4f}pp, corr {np.corrcoef(q, f)[0, 1]:.3f}")
    for key in sorted({(e["h"], e["mode"]) for e in big}):
        sub = [e for e in big if (e["h"], e["mode"]) == key and abs(e["F"]) > 1e-6]
        if sub:
            r_ = [e["pred"] / e["F"] for e in sub]
            print(f"  interval {key[0]:g}h mode {key[1]:<8} n={len(sub):>6} median pred/F {statistics.median(r_):.3f}")
    pm, fm = p <= -MAIN, f <= -MAIN
    print(f"  2x2 pred<=-0.30% x F<=-0.30%: both {int(np.sum(pm & fm))}, pred only {int(np.sum(pm & ~fm))}, F only {int(np.sum(~pm & fm))}")
    mc = [e for e in rows if main_cell(e)]
    sc = [e for e in rows if short_cell(e)]
    miss = sum(1 for e in mc if abs(e["pred"] - e["F"]) > 0.002)
    print(f"  main-cell events with |pred - F| > 0.20pp: {miss} of {len(mc)} (bound on main-cell events missed by the prescreen)")
    print(
        f"  F distribution in main cell: median {np.median([e['F'] for e in mc]) * 100:.3f}%, share F > -0.10%: {np.mean([e['F'] > -0.001 for e in mc]):.3f}"
    )
    # counts
    print("\nCOUNTS (eligible, by predicted band):")
    bands = Counter(band(e["pred"]) for e in rows)
    for b_, n in sorted(bands.items(), key=lambda x: str(x[0])):
        if b_:
            print(f"  {b_:<16} {n}")
    for lab, cell in (("MAIN pred<=-0.30% (long)", mc), ("SECONDARY pred>=+0.30% (short)", sc)):
        if not cell:
            print(f"{lab}: no events")
            continue
        days_ = {day(e) for e in cell}
        print(f"\n{lab}: n={len(cell)} days={len(days_)} symbols={len({e['s'] for e in cell})}")
        print(
            f"  by year {dict(sorted(Counter(ymd(day(e))[:4] for e in cell).items()))}; by interval h {dict(sorted(Counter(e['h'] for e in cell).items()))}"
        )
        print(f"  top symbols {Counter(e['s'] for e in cell).most_common(10)}")
        print(
            f"  slippage ladder (7d turnover) {dict(Counter('>=50M' if e['to7'] >= 50e6 else ('>=5M' if e['to7'] >= 5e6 else '<5M') for e in cell))}"
        )
        print(
            f"  entry-minute quote volume: median ${statistics.median([e['eq'] or 0 for e in cell]):,.0f}, p10 ${np.quantile([e['eq'] or 0 for e in cell], 0.1):,.0f}"
        )
        pl = [e for e in cell if e["plc"] is not None]
        x = [e["plc"] for e in pl]
        cl = [day(e) for e in pl]
        dm = defaultdict(list)
        for v_, c_ in zip(x, cl, strict=True):
            dm[c_].append(v_)
        sd = statistics.stdev(x)
        sd_day = statistics.stdev([statistics.fmean(v_) for v_ in dm.values()])
        se = cr1_se(x, cl)
        m = 2.8 * se
        print(
            f"  PLACEBO power (price only, T+-30 min windows; no event outcomes): n={len(x)} SD/trade {sd * 100:.3f}pp, SD of day mean {sd_day * 100:.3f}pp, CR1 SE {se * 100:.4f}pp"
        )
        print(f"  MDE (2.8 x CR1 SE by day) = {m * 100:.4f}pp; gate <= {GATE_MDE * 100:.2f}pp -> {'PASS' if m <= GATE_MDE else 'FAIL'}")
        if cell is mc:
            wr(cache / "stage0.json", {"n": len(mc), "days": len(days_), "sd": sd, "sd_day": sd_day, "mde": m, "pass": m <= GATE_MDE})
    sanity(fc, rows)


def sanity(fc: Path, rows: list[dict[str, Any]]) -> None:
    print("\nSANITY:")
    _qv, close, _f = load_daily(fc, "BTCUSDT")
    d = int(datetime(2024, 12, 31, tzinfo=UTC).timestamp()) // 86400
    print(f"  BTCUSDT 1d close 2024-12-31 = {close.get(d)} (token-unlock / premium-spike reports: 93,548.9)")
    r = [e["p_in"] / e["c1d_prev"] for e in rows if e.get("c1d_prev") and abs(e["pred"]) >= 0.003]
    if r:
        r_ = np.array(r)
        print(
            f"  1m entry price / previous-day 1d close (|pred| >= 0.30%): median {np.median(r_):.3f}, share outside [0.5, 2]: {np.mean((r_ < 0.5) | (r_ > 2)):.4f}"
        )
    hs = Counter(e["h"] for e in rows if abs(e["pred"]) >= 0.003)
    print(f"  funding intervals (h) of |pred| >= 0.30% events: {dict(sorted(hs.items()))}")
    caps = [e["cap"] for e in rows if e["cap"] is not None and abs(e["pred"]) >= 0.003]
    print(f"  events with an inferred cap: {len(caps)}; cap values (top) {Counter(round(c, 5) for c in caps).most_common(6)}")
    mins = Counter((e["t"] // MIN_MS) % 60 for e in rows)
    print(f"  settlement minute-of-hour (should be 0): {dict(mins.most_common(4))}")


# ---------------------------------------------------------------- full test


def ci_line(label: str, xs: list[float], cl: list[Any], n_boot: int = 10_000) -> tuple[float, tuple[float, float] | None]:
    m = statistics.fmean(xs) if xs else float("nan")
    ci = cluster_bootstrap_ci(xs, cl, n_boot=n_boot) if len(set(cl)) > 1 else None
    p = cluster_bootstrap_p(xs, cl, n_boot=n_boot) if len(set(cl)) > 1 else None
    cis = f"[{pct(ci[0])}, {pct(ci[1])}]" if ci else "n/a"
    print(f"  {label:<52} n={len(xs):>6} days={len(set(cl)):>4} mean {pct(m)} CI95 {cis} p(>0) {p if p is None else f'{p:.4f}'}")
    return m, ci


def full(fc: Path, cache: Path) -> None:
    s0 = rd(cache / "stage0.json") if (cache / "stage0.json").exists() else None
    if not s0 or not s0["pass"]:
        print("stage-0 gate not passed: full test not run (registered rule)")
        return
    rows, _ = load_eligible(cache)
    mc = [e for e in rows if main_cell(e)]
    sc = [e for e in rows if short_cell(e)]
    print("MAIN CELL: predicted funding <= -0.30% per interval, long perp, entry close [T-3,T-2), exit close [T+1,T+2); on notional")
    x2 = [net(e, 2.0) for e in mc]
    cl = [day(e) for e in mc]
    m2, ci2 = ci_line("(a) net, costs x2 (REGISTERED)", x2, cl)
    ci_line("net, costs x1", [net(e, 1.0) for e in mc], cl)
    ci_line("net, taker fees only (no slippage)", [net(e, 1.0, fees_only=True) for e in mc], cl)
    ci_line("gross (price + funding)", [e["pr"] + e["fund"] for e in mc], cl)
    ci_line("price component only", [e["pr"] for e in mc], cl)
    ci_line("funding component only", [e["fund"] for e in mc], cl)
    ci_line("mean cost x1 (fees + slippage)", [cost1(e) for e in mc], cl)
    ci_line("worse price (1-min shift), costs x2", [net(e, 2.0, worse=True) for e in mc], cl)
    pl = [e for e in mc if e["plc"] is not None]
    db, dci = ci_line("(b) gross - placebo (paired)", [e["pr"] + e["fund"] - e["plc"] for e in pl], [day(e) for e in pl])
    ci_line("price - placebo (paired; settlement price effect)", [e["pr"] - e["plc"] for e in pl], [day(e) for e in pl])
    ci_line("placebo price (T+-30 min)", [e["plc"] for e in pl], [day(e) for e in pl])
    h1 = [e for e in mc if H2026_1[0] <= e["t"] < H2026_1[1]]
    mh, _ = ci_line("(c) 2026-H1 net x2", [net(e, 2.0) for e in h1], [day(e) for e in h1])
    xd = top_cluster_drop(x2, cl, 10)
    xs_ = top_cluster_drop(x2, [e["s"] for e in mc], 10)
    md = statistics.fmean(xd)
    ms = statistics.fmean(xs_)
    print(f"  (d) without top-10 days: n={len(xd)} mean {pct(md)}; without top-10 symbols: n={len(xs_)} mean {pct(ms)}")
    # risk
    arr = np.array(x2)
    mae = np.array([e["mae"] for e in mc])
    print(
        f"  risk: net x2 p1 {pct(float(np.quantile(arr, 0.01)))}, p5 {pct(float(np.quantile(arr, 0.05)))}, min {pct(float(arr.min()))}, "
        f"max {pct(float(arr.max()))}; MAE p1 {pct(float(np.quantile(mae, 0.01)))}, MAE <= -10%: {int(np.sum(mae <= -0.10))}, <= -17.5%: {int(np.sum(mae <= -0.175))}"
    )
    print(f"  win rate net x2 {np.mean(arr > 0):.3f}; median net x2 {pct(float(np.median(arr)))}")
    ok_a = ci2 is not None and ci2[0] > 0
    ok_b = dci is not None and dci[0] > 0 and db > 0
    ok_c = mh > 0
    ok_d = md >= 0.0002 and ms >= 0.0002
    rej = (ci2 is not None and ci2[1] < 0.0003) or db <= 0
    print(
        f"\nREGISTERED CRITERIA: (a) {ok_a} (b) {ok_b} (c) {ok_c} (d) {ok_d} -> confirm {ok_a and ok_b and ok_c and ok_d}; reject rule {rej}"
    )
    verdict = "confirmed" if (ok_a and ok_b and ok_c and ok_d) else ("rejected" if rej else "inconclusive")
    print(f"VERDICT (main cell): {verdict}")

    print("\nSECONDARY CELL: predicted >= +0.30% (short)")
    if sc:
        ci_line("net, costs x2", [net(e, 2.0) for e in sc], [day(e) for e in sc])
        ci_line("net, costs x1", [net(e, 1.0) for e in sc], [day(e) for e in sc])
        ci_line("gross", [e["pr"] + e["fund"] for e in sc], [day(e) for e in sc])
        ci_line("price component", [e["pr"] for e in sc], [day(e) for e in sc])

    print("\nDOSE-RESPONSE by predicted band (descriptive; [0.10,0.20) is incomplete at its lower edge)")
    by_b: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in rows:
        b_ = band(e["pred"])
        if b_:
            by_b[b_].append(e)
    for b_ in sorted(by_b):
        ev = by_b[b_]
        print(
            f"  {b_:<16} n={len(ev):>6} days={len({day(e) for e in ev}):>4} funding {pct(statistics.fmean(e['fund'] for e in ev))} "
            f"price {pct(statistics.fmean(e['pr'] for e in ev))} gross {pct(statistics.fmean(e['pr'] + e['fund'] for e in ev))} "
            f"cost1 {pct(statistics.fmean(cost1(e) for e in ev))} net x1 {pct(statistics.fmean(net(e, 1.0) for e in ev))} net x2 {pct(statistics.fmean(net(e, 2.0) for e in ev))}"
        )

    print("\nTIME SLICES (main cell, net x2)")
    for lab, a, b in SLICES:
        sub = [e for e in mc if a.timestamp() * 1000 <= e["t"] < b.timestamp() * 1000]
        if len({day(e) for e in sub}) > 1:
            ci_line(lab, [net(e, 2.0) for e in sub], [day(e) for e in sub], n_boot=2000)
    print("\nBY INTERVAL (main cell, net x2)")
    for h in sorted({e["h"] for e in mc}):
        sub = [e for e in mc if e["h"] == h]
        if len({day(e) for e in sub}) > 1:
            ci_line(f"{h:g}h", [net(e, 2.0) for e in sub], [day(e) for e in sub], n_boot=2000)
    print("\nBY SLIPPAGE TIER (main cell)")
    for lab, lo, hi in (("<5M", 0, 5e6), (">=5M <50M", 5e6, 50e6), (">=50M", 50e6, math.inf)):
        sub = [e for e in mc if lo <= e["to7"] < hi]
        if len({day(e) for e in sub}) > 1:
            ci_line(f"{lab} net x2", [net(e, 2.0) for e in sub], [day(e) for e in sub], n_boot=2000)
            ci_line(f"{lab} gross", [e["pr"] + e["fund"] for e in sub], [day(e) for e in sub], n_boot=2000)
    nc = [e for e in mc if e["pred_nocap"] is not None]
    print("\nNO-CAP SIGNAL (pred_nocap <= -0.30%)")
    sub = [e for e in rows if e["pred_nocap"] is not None and e["pred_nocap"] <= -MAIN]
    ci_line("net x2", [net(e, 2.0) for e in sub], [day(e) for e in sub], n_boot=2000)
    _ = nc

    per_asset(mc)
    h2(rows)


def per_asset(mc: list[dict[str, Any]]) -> None:
    print("\nPER-ASSET (main cell, net x2)")
    x = [net(e, 2.0) for e in mc]
    a = [e["s"] for e in mc]
    cl = [day(e) for e in mc]
    st = sorted(asset_stats(x, a, min_n=20), key=lambda s: -s.n)
    print(f"  symbols with >= 20 trades: {len(st)} of {len(set(a))}")
    for s in st[:25]:
        ci = f"[{pct(s.ci[0])}, {pct(s.ci[1])}]" if s.ci else ""
        print(f"    {s.asset:<16} n={s.n:>5} mean {pct(s.mean)} CI(iid) {ci}")
    q = cluster_robust_q(x, a, cl, min_n=20)
    print(f"  Cochran Q (cluster-robust by day): k={q['k']} Q={q['q']:.1f} df={q['df']} p={q['p']} I2={q['i2']:.3f} deff={q['deff']:.2f}")
    pq = permutation_q(x, a, cl, min_n=20, n_perm=1000)
    print(
        f"  permutation of symbol labels within days: Q={pq['q']:.1f} p={pq['p']} null mean {pq['null_mean']:.1f} p95 {pq['null_p95']:.1f}"
    )
    ism = [e["t"] < IS_END for e in mc]
    if sum(ism) and sum(not v for v in ism):
        ps = persistence(x, a, cl, ism, min_n_is=5, top_frac=0.25, n_boot=2000)
        print(
            f"  persistence IS<2025-07-01: ranked {ps['k']}, top {len(ps['top'])}: IS top {pct(ps['top_is_mean'])} rest {pct(ps['rest_is_mean'])}; "
            f"OOS top {pct(ps['top_oos_mean'])} (n={ps['n_top_oos']}) rest {pct(ps['rest_oos_mean'])} (n={ps['n_rest_oos']}); "
            f"diff {pct(ps['diff'])} CI {ps['diff_ci']} p {ps['diff_p']}; Spearman rho {ps['rho']:.3f} (n={ps['rho_n']})"
        )


def h2(rows: list[dict[str, Any]]) -> None:
    print("\nH2 (descriptive): receiver-side return T-15->T0 (pre) and T0->T+15 (post)")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in rows:
        if e["pre"] is None or e["post"] is None:
            continue
        a = abs(e["pred"])
        if a < 0.0001:
            groups["control |pred|<0.01%"].append(e)
        elif a >= 0.001:
            groups[("neg " if e["pred"] < 0 else "pos ") + (">=0.30%" if a >= MAIN else "[0.10,0.30)")].append(e)
    for g in sorted(groups):
        ev = groups[g]
        cl = [day(e) for e in ev]
        if len(set(cl)) < 2:
            continue
        ci_line(f"{g} pre", [e["pre"] for e in ev], cl, n_boot=2000)
        ci_line(f"{g} post", [e["post"] for e in ev], cl, n_boot=2000)
        ci_line(f"{g} pre - post", [e["pre"] - e["post"] for e in ev], cl, n_boot=2000)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["count", "fetch", "build", "stage0", "full"])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_settlement")
    ap.add_argument("--fc-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--log", type=Path, default=None)
    a = ap.parse_args()
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    fc = a.fc_cache.expanduser()
    if a.log is not None:
        sys.stdout = Tee(sys.stdout, a.log.expanduser().open("w", buffering=1))
    t0 = time.time()
    if a.stage == "count":
        stage_count(fc, cache)
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(cache, a.sample))
    elif a.stage == "build":
        stage_build(fc, cache)
    elif a.stage == "stage0":
        stage0(fc, cache)
    elif a.stage == "full":
        full(fc, cache)
    print(f"stage {a.stage} finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")


if __name__ == "__main__":
    main()
