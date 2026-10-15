"""Perp-spot premium spikes on Binance: does a hedged short of an expensive USDT-M perp (long spot) earn the convergence?

Idea card: docs/ideas/IDEA-20261015-perp-spot-premium-spike-convergence.md ("Тріаж критика", "Попередня реєстрація").

Data: the data.binance.vision archive (``rt.binance_vision``): USDT-M ``premiumIndexKlines`` (detection), USDT-M and
spot 1m klines (P&L legs), 1d klines (turnover filter), ``fundingRate``. The archive keeps delisted symbols. Metadata
(TradFi filter) through ccxt (``rt.exchanges``). No keys, no orders.

Stages (each caches per file under --cache-dir; a rerun resumes):
    probe    archive semantics on a few samples (``--save-fixtures`` refreshes tests/fixtures samples)
    meta     archive symbol lists (USDT-M, spot), today's exchangeInfo (TradFi filter), premiumIndexKlines months
    pre1h    1h premiumIndexKlines (high/low) for the lossless prescreen
    cand     candidate symbol-days per threshold (counts only)
    fetch    1m premium on candidate days; 1d bars + funding for signal months; 1m windows of main/new signals
    stage0   events, n, SD, ICC by day, MDE and the gate -- no means; data checks
    diag     data diagnostics after the gate (clustering by minute, premium index vs traded basis), SD-only rows
    full     the registered full test; refuses to run unless stage0 passed the gate

On 2026-10-15 the gate failed (MDE 1.31 pp > 0.25 pp), the verdict is ``inconclusive`` and the full test was not
written: no means of the outcome were computed. The analytics it needs (H1-H3 metrics, liquidation, funding, pseudo
events via ``build_events(pseudo=True)``) are in ``tradedesk.analytics.premium_spike`` and this script.

    uv run python scripts/verify_premium_spike.py <stage> --cache-dir ~/.cache/tradedesk-agents/premium_spike \
        --log ~/.cache/tradedesk-agents/premium_spike/<stage>.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import statistics
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.listing_drift import cr1_se, design_effect, icc_anova, mde, symbol_eligible
from tradedesk.analytics.premium_spike import (
    DAY_MS,
    HOUR_MS,
    MIN_MS,
    Bars,
    Trade,
    price_ratio,
    round_trip_cost,
    run_signals,
    select_events,
    simulate,
    slippage,
    spot_candidates,
    trailing_turnover,
)
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FIRST_MONTH = (2023, 1)
LAST_MONTH = (2026, 9)
START = int(datetime(2023, 1, 1, tzinfo=UTC).timestamp() * 1000)
END = int(datetime(2026, 9, 29, tzinfo=UTC).timestamp() * 1000)
GATE_N = 300
GATE_SYMDAYS = 120
GATE_MDE = 0.0025
SPLIT = int(datetime(2024, 11, 15, tzinfo=UTC).timestamp() * 1000)
OCT_2025 = {int(datetime(2025, 10, d, tzinfo=UTC).timestamp() * 1000) // DAY_MS for d in (10, 11)}


class Tee:
    """stdout that also writes to a log file, so a run cut off by a time limit leaves its progress behind."""

    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, s: str) -> int:
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self) -> None:
        for st in self.streams:
            st.flush()


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(p)


async def probe(save: bool) -> None:
    """Archive semantics: premium index units vs perp/spot closes, daily-file availability, file sizes."""
    _, rt = build_server()
    v = rt.binance_vision
    try:
        paths = {
            "pi1h_m": "/data/futures/um/monthly/premiumIndexKlines/ARBUSDT/1h/ARBUSDT-1h-2024-03.zip",
            "pi1m_m": "/data/futures/um/monthly/premiumIndexKlines/ARBUSDT/1m/ARBUSDT-1m-2024-03.zip",
            "pi1m_d": "/data/futures/um/daily/premiumIndexKlines/ARBUSDT/1m/ARBUSDT-1m-2024-03-12.zip",
            "pi1m_d_old": "/data/futures/um/daily/premiumIndexKlines/ARBUSDT/1m/ARBUSDT-1m-2023-04-12.zip",
            "pi1m_d_new": "/data/futures/um/daily/premiumIndexKlines/ARBUSDT/1m/ARBUSDT-1m-2026-09-12.zip",
            "pk1m_d": "/data/futures/um/daily/klines/ARBUSDT/1m/ARBUSDT-1m-2024-03-12.zip",
            "sk1m_d": "/data/spot/daily/klines/ARBUSDT/1m/ARBUSDT-1m-2024-03-12.zip",
            "sk1m_d_new": "/data/spot/daily/klines/ARBUSDT/1m/ARBUSDT-1m-2026-09-12.zip",
            "sk1d_m": "/data/spot/monthly/klines/ARBUSDT/1d/ARBUSDT-1d-2024-03.zip",
            "pepe_p1d": "/data/futures/um/monthly/klines/1000PEPEUSDT/1d/1000PEPEUSDT-1d-2024-03.zip",
            "pepe_s1d": "/data/spot/monthly/klines/PEPEUSDT/1d/PEPEUSDT-1d-2024-03.zip",
        }
        from tradedesk.providers.binance_vision import parse_klines_zip

        got: dict[str, list[list[float]]] = {}
        for k, path in paths.items():
            t = time.time()
            try:
                f = await v.http.get_bytes(path)
                rows = parse_klines_zip(f.data)
                got[k] = rows
                print(
                    f"OK  {k:<11} {len(f.data):>9} bytes {len(rows):>6} rows {time.time() - t:.2f}s first {rows[0][:5] if rows else None}",
                    flush=True,
                )
                if save and k in ("pi1m_d", "pk1m_d", "sk1m_d", "pi1h_m", "sk1d_m"):
                    (FIX / f"binance_vision_{path.rsplit('/', 1)[-1][:-4]}_{k}_2026-10-15.zip").write_bytes(f.data)
            except ProviderError as e:
                print(f"ERR {k:<11} {e.reason}: {str(e.detail)[:120]}", flush=True)
        if {"pi1m_d", "pk1m_d", "sk1m_d"} <= set(got):
            pi = {int(r[0]): r for r in got["pi1m_d"]}
            pk = {int(r[0]): r for r in got["pk1m_d"]}
            sk = {int(r[0]): r for r in got["sk1m_d"]}
            common = sorted(set(pi) & set(pk) & set(sk))
            raw = [pk[t][4] / sk[t][4] - 1 for t in common]
            prem = [pi[t][4] for t in common]
            diff = [a - b for a, b in zip(raw, prem, strict=True)]
            print(
                f"1m ARB 2024-03-12: n={len(common)} premium close median {statistics.median(prem):+.5f} "
                f"perp/spot-1 median {statistics.median(raw):+.5f} diff median {statistics.median(diff):+.5f} "
                f"|diff| p90 {sorted(abs(d) for d in diff)[int(0.9 * len(diff))]:.5f}"
            )
        if "pi1h_m" in got and "pi1m_m" in got:
            hi1h = {int(r[0]): r[2] for r in got["pi1h_m"]}
            by_h: dict[int, float] = {}
            for r in got["pi1m_m"]:
                h = int(r[0]) // 3_600_000 * 3_600_000
                by_h[h] = max(by_h.get(h, -1.0), r[2])
            bad = [h for h in by_h if h in hi1h and by_h[h] > hi1h[h] + 1e-9]
            print(
                f"1h high >= max 1m high: {len(by_h) - len(bad)}/{len(by_h)} hours; violations {len(bad)} "
                f"(max excess {max((by_h[h] - hi1h[h] for h in bad), default=0):.6f})"
            )
        if "pepe_p1d" in got and "pepe_s1d" in got:
            s = {int(r[0]): r[4] for r in got["pepe_s1d"]}
            q = [r[4] / s[int(r[0])] for r in got["pepe_p1d"] if int(r[0]) in s]
            print(f"1000PEPE perp close / PEPE spot close median {statistics.median(q):.2f} (expect 1000)")
    finally:
        await rt.aclose()


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
        await asyncio.to_thread(wr, path, data)
        self.done += 1

    async def many(self, label: str, jobs: list[tuple[Path, Callable[[], Awaitable[Any]]]]) -> None:
        todo = [(p, g) for p, g in jobs if not p.exists()]
        print(f"[{label}] {len(jobs)} files, {len(todo)} to fetch", flush=True)
        t0 = time.time()
        self.done = 0
        self.errors = Counter()
        step = max(25, len(todo) // 20)

        async def run(p: Path, g: Callable[[], Awaitable[Any]]) -> None:
            await self.one(p, g)
            if self.done and self.done % step == 0:
                el = time.time() - t0
                print(f"  [{label}] {self.done}/{len(todo)} in {el:.0f}s, eta {el / self.done * (len(todo) - self.done):.0f}s", flush=True)

        await asyncio.gather(*[run(p, g) for p, g in todo])
        print(f"[{label}] done in {time.time() - t0:.0f}s; transient errors: {dict(self.errors)}", flush=True)


def months_between(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    out = []
    y, m = a
    while (y, m) <= b:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def ym_str(ym: tuple[int, int]) -> str:
    return f"{ym[0]:04d}-{ym[1]:02d}"


def months_of_keys(keys: list[str]) -> list[str]:
    """``.../SYM-1h-2024-03.zip`` -> ``2024-03`` (monthly archive keys)."""
    return sorted({k.rsplit("-", 2)[-2] + "-" + k.rsplit("-", 1)[-1][:2] for k in keys if k.endswith(".zip")})


# ---------------------------------------------------------------- universe


async def stage_meta(cache: Path) -> None:
    _, rt = build_server()
    try:
        v = rt.binance_vision
        fx = Fetcher(cache, workers=6)

        async def ex_info() -> Any:
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
                    }
            return {"fetched_at": int(time.time()), "info": info}

        async def um() -> Any:
            return (await v.um_symbols()).data

        async def spot() -> Any:
            return (await v.spot_symbols()).data

        await fx.many(
            "meta", [(cache / "um_symbols.json", um), (cache / "spot_symbols.json", spot), (cache / "exchange_info.json", ex_info)]
        )
        u = universe(cache)
        print(f"archive USDT-M symbols {len(rd(cache / 'um_symbols.json'))}; spot symbols {len(rd(cache / 'spot_symbols.json'))}")
        print(f"eligible perps with a spot candidate: {len(u['pairs'])}; excluded: {dict(Counter(r for r in u['excluded'].values()))}")
        jobs = []
        for s in sorted(u["pairs"]):

            async def go(s: str = s) -> Any:
                return months_of_keys((await v.list_dir(f"data/futures/um/monthly/premiumIndexKlines/{s}/1h/")).data["keys"])

            jobs.append((cache / "listing_pi" / f"{s}.json", go))
        await fx.many("listing premiumIndexKlines 1h", jobs)
    finally:
        await rt.aclose()


def universe(cache: Path) -> dict[str, Any]:
    """Eligible perps -> spot candidates (same symbol first, then the normalised asset with its multiplier)."""
    syms = rd(cache / "um_symbols.json")
    spot = set(rd(cache / "spot_symbols.json"))
    info = rd(cache / "exchange_info.json")["info"]
    pairs: dict[str, list[list[Any]]] = {}
    excluded: dict[str, str] = {}
    for s in syms:
        i = info.get(s) or {}
        if not symbol_eligible(s, i.get("contractType"), i.get("underlyingType")):
            if s.endswith("USDT") and "_" not in s:
                ct, ut = i.get("contractType"), i.get("underlyingType")
                excluded[s] = "tradfi" if ct == "TRADIFI_PERPETUAL" else ("index" if ut == "INDEX" else "stable/wrapped/other")
            continue
        cands = spot_candidates(s, spot)
        if not cands:
            excluded[s] = "no Binance spot USDT pair in the archive"
            continue
        pairs[s] = [list(c) for c in cands]
    return {"pairs": pairs, "excluded": excluded}


# ---------------------------------------------------------------- 1h prescreen


async def stage_pre1h(cache: Path) -> None:
    _, rt = build_server()
    try:
        u = universe(cache)
        fx = Fetcher(cache, workers=16)
        jobs = []
        for s in sorted(u["pairs"]):
            p = cache / "listing_pi" / f"{s}.json"
            months = rd(p) if p.exists() else None
            for mo in months or []:
                if not (ym_str(FIRST_MONTH) <= mo <= ym_str(LAST_MONTH)):
                    continue
                y, m = map(int, mo.split("-"))

                async def go(s: str = s, y: int = y, m: int = m) -> Any:
                    rows = (await rt.binance_vision.um_premium_index_monthly(s, "1h", y, m)).data
                    return [[int(r[0]), r[2], r[3]] for r in rows]  # open, high, low

                jobs.append((cache / "pi1h" / s / f"{mo}.json", go))
        await fx.many("premiumIndex 1h", jobs)
    finally:
        await rt.aclose()


def candidate_days(cache: Path, side: int, theta: float) -> dict[str, list[int]]:
    """UTC days with at least one hour whose premium extreme (side +1: high, -1: -low) reaches ``theta``.

    Lossless prescreen: a 1m close at or beyond ``theta`` lies in an hour whose 1h extreme does too (probe: 744/744)."""
    u = universe(cache)
    out: dict[str, list[int]] = {}
    for s in sorted(u["pairs"]):
        d = cache / "pi1h" / s
        if not d.exists():
            continue
        days: set[int] = set()
        for p in d.glob("*.json"):
            for t, hi, lo in rd(p) or []:
                if START <= t < END and (hi if side > 0 else -lo) >= theta:
                    days.add(int(t) // DAY_MS)
        if days:
            out[s] = sorted(days)
    return out


def stage_cand(cache: Path) -> None:
    """Counts of candidate days only (no events, no outcomes)."""
    for side, theta in ((1, 0.007), (1, 0.010), (1, 0.015), (-1, 0.010)):
        c = candidate_days(cache, side, theta)
        n = sum(len(v) for v in c.values())
        by_year = Counter(ymd(d)[:4] for v in c.values() for d in v)
        print(f"side {side:+d} theta {theta:.3f}: {n} candidate symbol-days on {len(c)} symbols; by year {dict(sorted(by_year.items()))}")


# ---------------------------------------------------------------- 1m data


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


def ym_of_day(day: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(day * 86400, UTC)
    return d.year, d.month


def pi_path(cache: Path, sym: str, day: int) -> Path:
    return cache / "pi1m" / sym / f"{ymd(day)}.json"


def k1m_path(cache: Path, kind: str, sym: str, day: int) -> Path:
    return cache / f"{kind}1m" / sym / f"{ymd(day)}.json"


def k1d_path(cache: Path, kind: str, sym: str, ym: tuple[int, int]) -> Path:
    return cache / f"{kind}1d" / sym / f"{ym_str(ym)}.json"


def fr_path(cache: Path, sym: str, ym: tuple[int, int]) -> Path:
    return cache / "funding" / sym / f"{ym_str(ym)}.json"


def pi_job(rt: Any, cache: Path, sym: str, day: int) -> tuple[Path, Callable[[], Awaitable[Any]]]:
    async def go() -> Any:
        rows = (await rt.binance_vision.um_premium_index_daily(sym, "1m", ymd(day))).data
        return [[int(r[0]), r[4]] for r in rows]  # open, close

    return pi_path(cache, sym, day), go


def k1m_job(rt: Any, cache: Path, kind: str, sym: str, day: int) -> tuple[Path, Callable[[], Awaitable[Any]]]:
    async def go() -> Any:
        v = rt.binance_vision
        f = v.um_klines_daily if kind == "p" else v.spot_klines_daily
        rows = (await f(sym, "1m", ymd(day))).data
        return [[int(r[0]), r[1], r[2], r[3], r[4], r[5]] for r in rows]

    return k1m_path(cache, kind, sym, day), go


def k1d_job(rt: Any, cache: Path, kind: str, sym: str, ym: tuple[int, int]) -> tuple[Path, Callable[[], Awaitable[Any]]]:
    async def go() -> Any:
        v = rt.binance_vision
        f = v.um_klines_monthly if kind == "p" else v.spot_klines_monthly
        rows = (await f(sym, "1d", ym[0], ym[1])).data
        return [[int(r[0]), r[4], r[6]] for r in rows]  # open, close, quote volume

    return k1d_path(cache, kind, sym, ym), go


def fr_job(rt: Any, cache: Path, sym: str, ym: tuple[int, int]) -> tuple[Path, Callable[[], Awaitable[Any]]]:
    async def go() -> Any:
        return (await rt.binance_vision.um_funding_monthly(sym, ym[0], ym[1])).data

    return fr_path(cache, sym, ym), go


def load_premium(cache: Path, sym: str) -> dict[int, float]:
    d = cache / "pi1m" / sym
    out: dict[int, float] = {}
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        for t, c in rd(p) or []:
            out[int(t)] = float(c)
    return out


def load_1m(cache: Path, kind: str, sym: str) -> Bars:
    d = cache / f"{kind}1m" / sym
    rows: list[list[float]] = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        rows.extend(rd(p) or [])
    return Bars.from_rows(rows)


def load_1d(cache: Path, kind: str, sym: str) -> tuple[dict[int, float], dict[int, float]]:
    """(close by day, quote volume by day)."""
    d = cache / f"{kind}1d" / sym
    close, qv = {}, {}
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        for t, c, q in rd(p) or []:
            close[int(t) // DAY_MS] = float(c)
            qv[int(t) // DAY_MS] = float(q)
    return close, qv


def load_funding(cache: Path, sym: str) -> list[list[float]]:
    d = cache / "funding" / sym
    out: list[list[float]] = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        out.extend(rd(p) or [])
    return sorted(out, key=lambda r: r[0])


def window_days(sig: int, hold_ms: int) -> list[int]:
    """UTC days the trade of a signal may read: signal - 3 min .. signal + 6 min + hold + 61 min."""
    a = (sig - 3 * MIN_MS) // DAY_MS
    b = (sig + 6 * MIN_MS + hold_ms + 61 * MIN_MS) // DAY_MS
    return list(range(a, b + 1))


async def stage_fetch(cache: Path, side: int, theta: float, hold_h: int, sample: int) -> None:
    """Pass A: 1m premium on candidate days. Pass B: 1m premium, perp, spot on every signal's window; 1d bars for the
    turnover/price filters; funding months."""
    _, rt = build_server()
    try:
        u = universe(cache)
        cand = candidate_days(cache, side, theta)
        syms = sorted(cand)[:sample] if sample else sorted(cand)
        fx = Fetcher(cache, workers=16)
        jobs = [pi_job(rt, cache, s, d) for s in syms for d in cand[s]]
        await fx.many(f"A premium 1m side {side:+d} theta {theta}", jobs)
        hold = hold_h * HOUR_MS
        sigs_by: dict[str, list[int]] = {}
        jobs_d: dict[Path, Callable[[], Awaitable[Any]]] = {}
        for s in syms:
            prem = load_premium(cache, s)
            sigs = []
            for d in cand[s]:
                sigs += run_signals(prem, max(d * DAY_MS, START), min((d + 1) * DAY_MS, END), side=side, lo=theta)
            sigs_by[s] = sigs
            months: set[tuple[int, int]] = set()
            for g in sigs:
                months.update(ym_of_day(d) for d in range(g // DAY_MS - 31, g // DAY_MS + 3))
            for ym in sorted(months):
                if ym > LAST_MONTH:
                    continue
                for p, go in (k1d_job(rt, cache, "p", s, ym), fr_job(rt, cache, s, ym)):
                    jobs_d[p] = go
                for sp, _m in u["pairs"][s]:
                    p, go = k1d_job(rt, cache, "s", sp, ym)
                    jobs_d[p] = go
        n_sig = sum(len(v) for v in sigs_by.values())
        print(
            f"signals (before cooldown/filters) side {side:+d} theta {theta}: {n_sig} on {sum(bool(v) for v in sigs_by.values())} symbols",
            flush=True,
        )
        await fx.many("1d bars + funding for signal months", list(jobs_d.items()))
        # 1m windows only for signals whose filter group is main or new (classification reads days < signal day)
        jobs_b: dict[Path, Callable[[], Awaitable[Any]]] = {}
        groups: Counter[str] = Counter()
        for s in syms:
            if not sigs_by[s]:
                continue
            d1 = {f"p:{s}": load_1d(cache, "p", s)} | {f"s:{sp}": load_1d(cache, "s", sp) for sp, _m in u["pairs"][s]}
            for g in sigs_by[s]:
                grp, sp, _mult, _tp, _ts = classify(cache, u, s, g // DAY_MS, d1)
                groups[grp] += 1
                if grp not in ("main", "new") or sp is None:
                    continue
                for d in window_days(g, hold):
                    for p, go in (pi_job(rt, cache, s, d), k1m_job(rt, cache, "p", s, d), k1m_job(rt, cache, "s", sp, d)):
                        jobs_b[p] = go
        print(f"signal groups (before cooldown): {dict(groups)}", flush=True)
        await fx.many(f"B 1m windows hold {hold_h}h", list(jobs_b.items()))
        # October 2026 funding is not in the monthly archive; windows ending there use the last September settlements only
        for p, go in (k1d_job(rt, cache, "p", "BTCUSDT", (2024, 12)), k1d_job(rt, cache, "s", "BTCUSDT", (2024, 12))):
            await fx.one(p, go)
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- events


@dataclass
class Variant:
    name: str
    side: int = 1
    theta: float = 0.010
    hi: float = math.inf
    hold_h: int = 24
    delay: int = 1
    same_bar: bool = False


MAIN = Variant("main: theta 1.0%, exit 0.2% / 24h, entry next open")


def classify(cache: Path, u: dict[str, Any], sym: str, day: int, d1: dict[str, Any]) -> tuple[str, str | None, int, float, float]:
    """(group, spot symbol, mult, perp turnover, spot turnover) for an event on ``day``; reads days < ``day`` only.

    group: main | new | day0 | fail_price | fail_turnover"""
    pc, pq = d1[f"p:{sym}"]
    chosen = None
    for sp, mult in u["pairs"][sym]:
        sc, _sq = d1[f"s:{sp}"]
        r = price_ratio(pc, sc, mult, day)
        if r is not None and 0.95 <= r <= 1.05:
            chosen = (sp, mult)
            break
    tp, np_ = trailing_turnover(pq, day)
    if chosen is None:
        spot_prior = any(trailing_turnover(d1[f"s:{sp}"][1], day)[1] > 0 for sp, _m in u["pairs"][sym])
        return ("day0" if np_ == 0 or not spot_prior else "fail_price"), None, 1, tp or 0.0, 0.0
    sp, mult = chosen
    ts, ns = trailing_turnover(d1[f"s:{sp}"][1], day)
    if min(np_, ns) == 0:
        return "day0", sp, mult, tp or 0.0, ts or 0.0
    ok = (tp or 0) >= 20e6 and (ts or 0) >= 5e6
    if np_ >= 25 and ns >= 25:
        return ("main" if ok else "fail_turnover"), sp, mult, tp or 0.0, ts or 0.0
    return ("new" if ok else "fail_turnover_new"), sp, mult, tp or 0.0, ts or 0.0


def build_events(cache: Path, v: Variant, *, pseudo: bool = False, avoid: dict[str, list[int]] | None = None) -> list[dict[str, Any]]:
    """Events of one variant across the universe; every event carries its group and (if tradable) the trade."""
    u = universe(cache)
    cand = candidate_days(cache, v.side, 0.007 if pseudo else v.theta)
    out = []
    for s in sorted(cand):
        prem = load_premium(cache, s)
        if not prem:
            continue
        if pseudo:
            loaded = sorted({t // DAY_MS for t in prem})
            sigs = []
            for d in loaded:
                sigs += run_signals(prem, max(d * DAY_MS, START), min((d + 1) * DAY_MS, END), side=v.side, lo=0.003, hi=0.006)
            bad = (avoid or {}).get(s, [])
            sigs = [g for g in sigs if all(abs(g - b) >= DAY_MS for b in bad)]
        else:
            sigs = []
            for d in cand[s]:
                sigs += run_signals(prem, max(d * DAY_MS, START), min((d + 1) * DAY_MS, END), side=v.side, lo=v.theta, hi=v.hi)
        if not sigs:
            continue
        perp = load_1m(cache, "p", s)
        d1 = {f"p:{s}": load_1d(cache, "p", s)}
        spots = {}
        for sp, _m in u["pairs"][s]:
            d1[f"s:{sp}"] = load_1d(cache, "s", sp)
            spots[sp] = load_1m(cache, "s", sp)
        fund = load_funding(cache, s)
        meta: dict[int, tuple[str, str | None, int, float, float]] = {}

        def sim(
            g: int,
            s: str = s,
            perp: Bars = perp,
            d1: dict[str, Any] = d1,
            spots: dict[str, Bars] = spots,
            fund: list[list[float]] = fund,
            prem: dict[int, float] = prem,
            meta: dict = meta,
        ) -> tuple[Trade | None, str]:
            grp = classify(cache, u, s, g // DAY_MS, d1)
            meta[g] = grp
            # filtered-out events are not trades: no simulation, no exit; they only start the 6h cooldown
            if grp[1] is None or grp[0] not in ("main", "new") or (pseudo and grp[0] != "main"):
                return None, grp[0]
            return simulate(
                prem, perp, spots[grp[1]], fund, g, side=v.side, delay_bars=v.delay, same_bar=v.same_bar, max_hold_ms=v.hold_h * HOUR_MS
            )

        for g, tr, why in select_events(sigs, sim):
            grp, sp, mult, tp, ts = meta[g]
            ev = {
                "symbol": s,
                "signal": g,
                "day": g // DAY_MS,
                "group": grp,
                "spot": sp,
                "mult": mult,
                "tp": tp,
                "ts": ts,
                "trade": tr,
                "why": why,
            }
            if tr is not None:
                ev["cost1"] = round_trip_cost(slippage(ts), slippage(tp))
                ev["entry_gap"] = tr.p_in / (tr.s_in * mult) - 1 - tr.prem_signal
            out.append(ev)
    return out


def usable(evs: list[dict[str, Any]], group: str = "main") -> list[dict[str, Any]]:
    return [e for e in evs if e["group"] == group and e["trade"] is not None and e["trade"].funding_ok]


def metric(e: dict[str, Any], *, lev: float = 2.0, cost_mult: float = 2.0, slip_mult: float = 1.0) -> float:
    tr: Trade = e["trade"]
    c = round_trip_cost(slippage(e["ts"]), slippage(e["tp"]), mult=cost_mult, slip_mult=slip_mult)
    return tr.net(lev, c)


def describe_events(evs: list[dict[str, Any]], label: str) -> None:
    print(f"{label}: events {len(evs)}; by group {dict(Counter(e['group'] for e in evs))}")
    print(f"  untradable/excluded reasons: {dict(Counter(e['why'] for e in evs if e['trade'] is None))}")
    main_t = [e for e in evs if e["group"] == "main" and e["trade"] is not None]
    print(f"  main tradable {len(main_t)}; funding holes (excluded from H1) {sum(not e['trade'].funding_ok for e in main_t)}")


def power(xs: list[float], cl: list[Any]) -> tuple[float, float, float, float, float]:
    sd = statistics.stdev(xs)
    icc = icc_anova(xs, cl)
    deff = design_effect(cl, icc)
    return sd, icc, deff, mde(sd, len(xs), deff), 2.8 * cr1_se(xs, cl)


def stage0(cache: Path) -> None:
    evs = build_events(cache, MAIN)
    describe_events(evs, MAIN.name)
    rows = usable(evs)
    by_year = Counter(ymd(e["day"])[:4] for e in rows)
    print(f"  main sample by year {dict(sorted(by_year.items()))}; symbols {len({e['symbol'] for e in rows})}")
    print(f"  top symbols {Counter(e['symbol'] for e in rows).most_common(12)}")
    print(
        f"  new-listing group (exploratory) {sum(e['group'] == 'new' for e in evs)}, listing-day events (count only) {sum(e['group'] == 'day0' for e in evs)}"
    )
    if len(rows) < 3:
        print("too few events for stage 0")
        return
    xs = [metric(e) for e in rows]
    cl = [e["day"] for e in rows]
    sd_ = {(e["symbol"], e["day"]) for e in rows}
    sd, icc, deff, m1, m2 = power(xs, cl)
    n = len(xs)
    print()
    print("STAGE 0 metric: H1 net on notional (2x liquidation, funding, costs x2) -- SD and MDE only, no means")
    print(f"n={n} symbol-days={len(sd_)} days={len(set(cl))}")
    print(f"SD={sd * 100:.3f}pp ICC(day)={icc:+.4f} deff={deff:.3f} n_eff={n / deff:.1f}")
    print(f"MDE_icc={m1 * 100:.3f}pp MDE_cr={m2 * 100:.3f}pp -> gate MDE={max(m1, m2) * 100:.3f}pp (threshold {GATE_MDE * 100:.2f}pp)")
    ok = n >= GATE_N and len(sd_) >= GATE_SYMDAYS and max(m1, m2) <= GATE_MDE
    print(
        f"GATE: n>={GATE_N}: {n >= GATE_N}; symbol-days>={GATE_SYMDAYS}: {len(sd_) >= GATE_SYMDAYS}; MDE<={GATE_MDE * 100:.2f}pp: {max(m1, m2) <= GATE_MDE} -> {'PASS' if ok else 'FAIL'}"
    )
    hold = sorted(e["trade"].hold_ms / HOUR_MS for e in rows)
    conv = sum(e["trade"].converged for e in rows)
    print(
        f"time to convergence (entry -> exit, h; timeouts = 24): median {statistics.median(hold):.2f}, p25 {hold[len(hold) // 4]:.2f}, "
        f"p75 {hold[3 * len(hold) // 4]:.2f}; converged {conv}/{n}"
    )
    prem = sorted(e["trade"].prem_signal for e in rows)
    print(f"premium at signal: median {statistics.median(prem) * 100:.2f}%, p90 {prem[int(0.9 * n)] * 100:.2f}%, max {prem[-1] * 100:.2f}%")
    wr(cache / "stage0.json", {"n": n, "symbol_days": len(sd_), "sd": sd, "icc": icc, "mde_icc": m1, "mde_cr": m2, "pass": ok})
    for need in (0.25,):
        print(
            f"events needed for MDE <= {need:.2f}pp at this SD/clustering: ICC {n * (m1 * 100 / need) ** 2:.0f}, CR1 {n * (m2 * 100 / need) ** 2:.0f}"
        )
    data_checks(cache, evs, rows)


def data_checks(cache: Path, evs: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    print()
    pc, _ = load_1d(cache, "p", "BTCUSDT")
    sc, _ = load_1d(cache, "s", "BTCUSDT")
    d = int(datetime(2024, 12, 31, tzinfo=UTC).timestamp()) // 86400
    print(f"check BTCUSDT perp close 2024-12-31 = {pc.get(d)} (token-unlock report 93,548.9); spot close {sc.get(d)}")
    gaps = sorted(abs(e["entry_gap"]) for e in rows)
    if gaps:
        print(
            f"check |P_in/(S_in*mult) - 1 - premium(signal)|: median {statistics.median(gaps) * 100:.3f}pp, p90 {gaps[int(0.9 * len(gaps))] * 100:.3f}pp, "
            f"> 3pp: {sum(g > 0.03 for g in gaps)}"
        )
    mults = Counter((e["symbol"], e["spot"], e["mult"]) for e in rows if e["mult"] != 1 or e["spot"] != e["symbol"])
    print(f"check mapped pairs with a multiplier or another ticker: {sorted(mults)}")
    fails = Counter(e["symbol"] for e in evs if e["group"] == "fail_price")
    print(f"price-level failures (different token / rename): {fails.most_common(15)}")


def diag(cache: Path) -> None:
    """Data diagnostics after the gate (no means of the outcome): event clustering by day, premium index vs the
    traded perp/spot basis around signals, SD-only power of the registered sensitivity rows."""
    evs = build_events(cache, MAIN)
    rows = usable(evs)
    days = Counter(e["day"] for e in rows)
    print(f"days with most main events: {[(ymd(d), k) for d, k in days.most_common(12)]}")
    print(f"events on the top-10 days: {sum(k for _, k in days.most_common(10))} of {len(rows)}")
    by_h = Counter(datetime.fromtimestamp(e["signal"] / 1000, UTC).strftime("%Y-%m-%d %H:%M") for e in rows)
    print(f"signal minutes shared by >= 5 symbols: {[(t, k) for t, k in by_h.most_common() if k >= 5][:15]}")
    # premium index vs the traded basis (perp close / (spot close * mult) - 1) on the signal bar and the two before
    u = universe(cache)
    gap_sig = []
    for e in rows:
        perp, spot = load_1m(cache, "p", e["symbol"]), load_1m(cache, "s", e["spot"])
        prem = load_premium(cache, e["symbol"])
        g = e["signal"]
        pb, sb = perp.rows.get(g), spot.rows.get(g)
        if pb and sb:
            gap_sig.append((pb[3] / (sb[3] * e["mult"]) - 1, prem[g], e))
    raw = sorted(x[0] for x in gap_sig)
    print(
        f"traded basis on the signal bar (perp close / spot close - 1): median {statistics.median(raw) * 100:.3f}%, "
        f"share >= 1.0%: {sum(x >= 0.01 for x in raw) / len(raw):.3f}, share >= 0.5%: {sum(x >= 0.005 for x in raw) / len(raw):.3f} (n={len(raw)})"
    )
    for yr in ("2023", "2024", "2025", "2026"):
        sub = [x for x in gap_sig if ymd(x[2]["day"])[:4] == yr]
        if sub:
            print(
                f"  {yr}: n={len(sub)} median traded basis {statistics.median(x[0] for x in sub) * 100:.3f}%, premium index {statistics.median(x[1] for x in sub) * 100:.3f}%"
            )
    both = 0
    for _, _, e in gap_sig:
        perp, spot = load_1m(cache, "p", e["symbol"]), load_1m(cache, "s", e["spot"])
        ok = True
        for t in (e["signal"] - MIN_MS, e["signal"]):
            pb, sb = perp.rows.get(t), spot.rows.get(t)
            ok = ok and bool(pb and sb) and pb[3] / (sb[3] * e["mult"]) - 1 >= 0.01
        both += ok
    shared = {t for t, k in by_h.items() if k >= 5}
    print(
        f"planning count: main events whose traded basis is >= 1.0% on both signal bars: {both} of {len(gap_sig)}; "
        f"events on minutes shared by >= 5 symbols: {sum(datetime.fromtimestamp(e['signal'] / 1000, UTC).strftime('%Y-%m-%d %H:%M') in shared for e in rows)}"
    )
    print("examples (premium index | traded basis), minutes signal-2 .. signal+3:")
    for _, _, e in sorted(gap_sig, key=lambda x: x[2]["signal"])[:: max(1, len(gap_sig) // 8)][:8]:
        perp, spot = load_1m(cache, "p", e["symbol"]), load_1m(cache, "s", e["spot"])
        prem = load_premium(cache, e["symbol"])
        cells = []
        for k in range(-2, 4):
            t = e["signal"] + k * MIN_MS
            pb, sb = perp.rows.get(t), spot.rows.get(t)
            rb = (pb[3] / (sb[3] * e["mult"]) - 1) * 100 if pb and sb else float("nan")
            cells.append(f"{prem.get(t, float('nan')) * 100:+.2f}|{rb:+.2f}")
        print(f"  {e['symbol']:<14} {datetime.fromtimestamp(e['signal'] / 1000, UTC):%Y-%m-%d %H:%M}  " + "  ".join(cells))
    _ = u
    print()
    print("SD-only power of registered sensitivity rows (no means):")
    for label, sel in (
        ("all main (registered)", lambda e: True),
        ("without |entry gap| > 3pp (registered sensitivity row)", lambda e: abs(e["entry_gap"]) <= 0.03),
        ("2024-11-15 and later (second half)", lambda e: e["signal"] >= SPLIT),
        ("before 2024-11-15 (first half)", lambda e: e["signal"] < SPLIT),
        ("without 2025-10-10/11", lambda e: e["day"] not in OCT_2025),
    ):
        sub = [e for e in rows if sel(e)]
        if len(sub) > 2:
            xs, cl = [metric(e) for e in sub], [e["day"] for e in sub]
            sd, icc, deff, m1, m2 = power(xs, cl)
            print(
                f"  {label:<56} n={len(sub):>4} days={len(set(cl)):>3} SD={sd * 100:6.3f}pp ICC={icc:+.3f} MDE_icc={m1 * 100:6.3f}pp MDE_cr={m2 * 100:6.3f}pp"
            )


def full(cache: Path) -> None:
    """Registered full test; runs only after the stage-0 gate passed."""
    s0 = rd(cache / "stage0.json") if (cache / "stage0.json").exists() else None
    if not s0 or not s0["pass"]:
        print("stage-0 gate not passed: full test not run (registered rule)")
        return
    raise SystemExit("gate passed: implement the registered full test (card section 'Попередня реєстрація') before reading means")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["probe", "meta", "pre1h", "cand", "fetch", "stage0", "diag", "full"])
    ap.add_argument("--save-fixtures", action="store_true")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/premium_spike")
    ap.add_argument("--side", type=int, default=1)
    ap.add_argument("--theta", type=float, default=0.010)
    ap.add_argument("--hold-h", type=int, default=24)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--log", type=Path, default=None, help="also write output to this file (line-buffered, overwritten)")
    a = ap.parse_args()
    a.cache_dir = a.cache_dir.expanduser()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.log is not None:
        sys.stdout = Tee(sys.stdout, a.log.expanduser().open("w", buffering=1))
    if a.stage == "probe":
        asyncio.run(probe(a.save_fixtures))
    elif a.stage == "meta":
        asyncio.run(stage_meta(a.cache_dir))
    elif a.stage == "pre1h":
        asyncio.run(stage_pre1h(a.cache_dir))
    elif a.stage == "cand":
        stage_cand(a.cache_dir)
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(a.cache_dir, a.side, a.theta, a.hold_h, a.sample))
    elif a.stage == "stage0":
        stage0(a.cache_dir)
    elif a.stage == "diag":
        diag(a.cache_dir)
    elif a.stage == "full":
        full(a.cache_dir)


if __name__ == "__main__":
    main()
