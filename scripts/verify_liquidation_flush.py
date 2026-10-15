"""Liquidation-flush overshoot on Binance USDT-M perps (IDEA-20261013-liquidation-flush-overshoot).

Pre-registered in the idea card ("Попередня реєстрація") before the first full run:
  universe  = monthly point-in-time top-50 by trailing-30d quote volume (COIN only, no stables/wrapped/index),
              from the data.binance.vision archive, which keeps delisted symbols;
  event     = 5m bar with |close/prev_close - 1| >= 4 % and robust volume z > 5 vs the 7 days strictly before;
              4 h cooldown per symbol; fade the move (long after a drop, short after a squeeze);
  primary   = entry at the close of the first 1m bar after the event bar closes (>= 60 s), exit 2 h later,
              no stop, 0.30 % round-trip costs, both directions pooled; day-clustered bootstrap CI;
  period    = event bars opened in [2024-01-01, 2026-10-01) UTC; halves split at 2025-05-17.

Stages (each caches per file under --cache-dir, so a rerun resumes):
  uv run python scripts/verify_liquidation_flush.py --cache-dir ~/.cache/tradedesk-agents/liquidation_flush --stage fetch
  uv run python scripts/verify_liquidation_flush.py --cache-dir ~/.cache/tradedesk-agents/liquidation_flush --stage report --out r.md
Network only through the runtime providers (rt.binance_vision, rt.exchanges). No keys, no orders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.liquidation_flush import (
    DAY_MS,
    HOUR_MS,
    MIN_MS,
    control_times,
    detect_flush_events,
    held_extreme,
    holm,
    month_start,
    month_starts,
    monthly_universe,
    quantile,
    simulate_trade,
    summarise,
    symbol_eligible,
    window_return,
    without_top_days,
)
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

START = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
END = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1000)
SPLIT = int(datetime(2025, 5, 17, tzinfo=UTC).timestamp() * 1000)
CRASH_DAY = "2025-10-10"
COST = 0.003
HORIZONS = {"30m": 30 * MIN_MS, "1h": HOUR_MS, "2h": 2 * HOUR_MS, "4h": 4 * HOUR_MS}
N_CONTROLS = 3
BTC = "BTCUSDT"
# Derived-file generation. v1 (never used for results) read exchangeInfo for contractType=PERPETUAL only, so TradFi
# perps (contractType TRADIFI_PERPETUAL) looked like delisted coins and entered the 2026 universe.
DERIVED = "v2"
INFO_FILE = f"exchange_info_{DERIVED}.json"


# ---------------------------------------------------------------- helpers


def ym(ms: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return d.year, d.month


def day_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def day_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def prev_month(ms: int) -> int:
    return month_start(ms - DAY_MS)


def read_json(p: Path) -> Any:
    return json.loads(p.read_text())


class Fetcher:
    """Per-file JSON cache in front of the archive provider; ``null`` marks a file the archive does not have."""

    def __init__(self, rt: Any, cache: Path, workers: int = 16) -> None:
        self.rt = rt
        self.cache = cache
        self.sem = asyncio.Semaphore(workers)
        self.done = 0
        self.errors: Counter[str] = Counter()

    async def one(self, path: Path, go: Callable[[], Awaitable[Any]]) -> None:
        if await asyncio.to_thread(path.exists):
            return
        async with self.sem:
            try:
                data = await go()
            except ProviderError as e:
                if e.reason != "data_unavailable":
                    self.errors[e.reason] += 1
                    return  # transient: leave uncached so a rerun retries
                data = None
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        await asyncio.to_thread(tmp.write_text, json.dumps(data, separators=(",", ":")))
        await asyncio.to_thread(tmp.replace, path)
        self.done += 1

    async def many(self, label: str, jobs: list[tuple[Path, Callable[[], Awaitable[Any]]]]) -> None:
        todo = [(p, g) for p, g in jobs if not p.exists()]
        print(f"[{label}] {len(jobs)} files, {len(todo)} to fetch", flush=True)
        t0 = time.time()
        self.done = 0
        step = max(50, len(todo) // 20)

        async def run(p: Path, g: Callable[[], Awaitable[Any]]) -> None:
            await self.one(p, g)
            if self.done and self.done % step == 0:
                el = time.time() - t0
                print(f"  [{label}] {self.done}/{len(todo)} in {el:.0f}s, eta {el / self.done * (len(todo) - self.done):.0f}s", flush=True)

        await asyncio.gather(*[run(p, g) for p, g in todo])
        print(f"[{label}] done in {time.time() - t0:.0f}s; transient errors: {dict(self.errors)}", flush=True)


def kline_rows(rows: list[list[float]]) -> list[list[float]]:
    """Keep [ts, open, high, low, close, quote_volume, base_volume]."""
    return [[r[0], r[1], r[2], r[3], r[4], r[6], r[5]] for r in rows]


# ---------------------------------------------------------------- fetch stages


async def stage_meta(rt: Any, cache: Path) -> tuple[list[str], dict[str, dict[str, Any]]]:
    sp, ip = cache / "symbols.json", cache / INFO_FILE
    if not sp.exists():
        f = await rt.binance_vision.um_symbols()
        sp.write_text(json.dumps(f.data))
    if not ip.exists():
        mk = (await rt.exchanges.markets("binanceusdm")).data or {}
        info = {}
        for m in mk.values():
            i = (m or {}).get("info") or {}
            # keep every contract type: TradFi perps (stocks, metals, oil) use contractType TRADIFI_PERPETUAL and
            # must be recognised to be excluded, not mistaken for delisted coins
            if i.get("symbol"):
                info[i["symbol"]] = {
                    "underlyingType": i.get("underlyingType"),
                    "contractType": i.get("contractType"),
                    "status": i.get("status"),
                    "active": m.get("active"),
                }
        ip.write_text(json.dumps(info))
    return read_json(sp), read_json(ip)


def effective_type(info: dict[str, dict[str, Any]], sym: str) -> str | None:
    """underlyingType for plain perps; the contract type for anything else (e.g. TRADIFI_PERPETUAL); None if unknown."""
    i = info.get(sym)
    if not i:
        return None
    if i.get("contractType") not in (None, "PERPETUAL"):
        return str(i.get("contractType"))
    return i.get("underlyingType")


def candidate_symbols(symbols: list[str], info: dict[str, dict[str, Any]]) -> list[str]:
    return [s for s in symbols if symbol_eligible(s, effective_type(info, s))]


def months_needed_1d() -> list[int]:
    return month_starts(prev_month(START), END - DAY_MS)  # Dec 2023 .. Sep 2026 (Sep only matters for nothing; harmless)


async def stage_daily(fx: Fetcher, cache: Path, syms: list[str]) -> None:
    rt = fx.rt
    jobs = []
    for s in syms:
        p = cache / "listing" / f"{s}_1d.json"

        async def go(s: str = s) -> Any:
            f = await rt.binance_vision.list_dir(f"data/futures/um/monthly/klines/{s}/1d/")
            return sorted({k.rsplit("-", 2)[-2] + "-" + k.rsplit("-", 1)[-1][:2] for k in f.data["keys"] if k.endswith(".zip")})

        jobs.append((p, go))
    await fx.many("listing 1d", jobs)
    want = {f"{y:04d}-{m:02d}" for y, m in (ym(x) for x in months_needed_1d())}
    jobs = []
    for s in syms:
        lp = cache / "listing" / f"{s}_1d.json"
        if not lp.exists():
            continue
        for mo in read_json(lp) or []:
            if mo in want:
                y, m = int(mo[:4]), int(mo[5:])

                async def go(s: str = s, y: int = y, m: int = m) -> Any:
                    return kline_rows((await rt.binance_vision.um_klines_monthly(s, "1d", y, m)).data)

                jobs.append((cache / "k1d" / s / f"{mo}.json", go))
    await fx.many("klines 1d", jobs)


def load_daily(cache: Path, syms: list[str]) -> dict[str, list[tuple[int, float]]]:
    out: dict[str, list[tuple[int, float]]] = {}
    for s in syms:
        d = cache / "k1d" / s
        if not d.exists():
            continue
        rows: list[tuple[int, float]] = []
        for p in sorted(d.glob("*.json")):
            for r in read_json(p) or []:
                rows.append((int(r[0]), float(r[5])))
        if rows:
            out[s] = sorted(rows)
    return out


def build_universe(cache: Path, syms: list[str]) -> dict[int, list[str]]:
    up = cache / f"universe_{DERIVED}.json"
    if up.exists():
        return {int(k): v for k, v in read_json(up).items()}
    daily = load_daily(cache, syms)
    uni = {m: monthly_universe(daily, m) for m in month_starts(START, END)}
    up.write_text(json.dumps({str(k): v for k, v in uni.items()}))
    return uni


def needed_5m(uni: dict[int, list[str]]) -> set[tuple[str, int]]:
    need: set[tuple[str, int]] = set()
    for m, syms in uni.items():
        for s in syms:
            need.add((s, m))
            need.add((s, prev_month(m)))  # 7-day volume baseline at the start of the month
    for m in month_starts(prev_month(START), END):
        need.add((BTC, m))
    return need


async def stage_5m(fx: Fetcher, cache: Path, uni: dict[int, list[str]]) -> None:
    rt = fx.rt
    jobs = []
    for s, m in sorted(needed_5m(uni)):
        y, mo = ym(m)

        async def go(s: str = s, y: int = y, mo: int = mo) -> Any:
            return kline_rows((await rt.binance_vision.um_klines_monthly(s, "5m", y, mo)).data)

        jobs.append((cache / "k5m" / s / f"{y:04d}-{mo:02d}.json", go))
    await fx.many("klines 5m", jobs)


def load_5m(cache: Path, sym: str, months: list[int]) -> np.ndarray:
    rows: list[list[float]] = []
    for m in sorted(set(months)):
        y, mo = ym(m)
        p = cache / "k5m" / sym / f"{y:04d}-{mo:02d}.json"
        if p.exists():
            rows.extend(read_json(p) or [])
    if not rows:
        return np.zeros((0, 7))
    a = np.array(rows, dtype=float)
    a = a[np.argsort(a[:, 0], kind="stable")]
    keep = np.concatenate([[True], a[1:, 0] != a[:-1, 0]])
    return a[keep]


# ---------------------------------------------------------------- events


def detect_all(
    cache: Path, uni: dict[int, list[str]], *, ret_threshold: float = 0.04, z_threshold: float = 5.0, keep_opens: bool = False
) -> tuple[list[dict[str, Any]], dict[str, tuple[np.ndarray, np.ndarray]], dict[str, Any]]:
    by_sym: dict[str, list[int]] = defaultdict(list)
    for m, syms in uni.items():
        for s in syms:
            by_sym[s].append(m)
    events: list[dict[str, Any]] = []
    opens: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    diag: dict[str, Any] = {"gaps_over_1d": 0, "gap_symbols": set(), "bars": 0, "qv_ratio": []}
    syms_all = sorted(set(by_sym) | {BTC})
    for s in syms_all:
        months = by_sym.get(s, [])
        load_months = [x for m in months for x in (m, prev_month(m))]
        if s == BTC:
            load_months = month_starts(prev_month(START), END)
        a = load_5m(cache, s, load_months)
        if a.shape[0] < 2:
            continue
        diag["bars"] += a.shape[0]
        dt = np.diff(a[:, 0])
        big = int(np.sum(dt > DAY_MS))
        # gaps between non-consecutive months of membership are expected; count only gaps inside loaded spans
        if big:
            diag["gaps_over_1d"] += big
            diag["gap_symbols"].add(s)
        nz = a[:, 6] > 0
        if nz.any():
            diag["qv_ratio"].append(float(np.median(a[nz, 5] / (a[nz, 6] * a[nz, 4]))))
        if keep_opens:
            opens[s] = (a[:, 0].astype(np.int64), a[:, 1].copy())
        member = set(months)
        evs = detect_flush_events(
            a[:, 0].astype(np.int64),
            a[:, 4],
            a[:, 5],
            low=a[:, 3],
            high=a[:, 2],
            ret_threshold=ret_threshold,
            z_threshold=z_threshold,
            eligible=lambda t, member=member: month_start(t) in member,
            start_ms=START,
            end_ms=END,
        )
        for e in evs:
            e["symbol"] = s
            e["day"] = day_str(e["ts"])
            idx = int(np.searchsorted(a[:, 0], e["ts"]))
            e["bar_qv"] = float(a[idx, 5])
        events.extend(evs)
    events.sort(key=lambda e: (e["ts"], e["symbol"]))
    return events, opens, diag


def detect_cached(cache: Path, uni: dict[int, list[str]], ret_threshold: float = 0.04, z_threshold: float = 5.0) -> list[dict[str, Any]]:
    """Events for one threshold pair, cached as JSON (the 5m pass takes minutes)."""
    p = cache / f"events_{DERIVED}_r{ret_threshold:g}_z{z_threshold:g}.json"
    if p.exists():
        return read_json(p)
    evs, _, _ = detect_all(cache, uni, ret_threshold=ret_threshold, z_threshold=z_threshold)
    p.write_text(json.dumps(evs))
    return evs


def control_plan(events: list[dict[str, Any]]) -> dict[tuple[str, int], list[int]]:
    ev_by_sym: dict[str, list[int]] = defaultdict(list)
    for e in events:
        ev_by_sym[e["symbol"]].append(e["signal_ts"])
    out = {}
    for e in events:
        times = ev_by_sym[e["symbol"]]
        blocked = lambda t, times=times: any(abs(t - x) < 4 * HOUR_MS for x in times)  # noqa: E731
        out[(e["symbol"], e["ts"])] = control_times(e["signal_ts"], e["symbol"], k=N_CONTROLS, blocked=blocked)
    return out


def days_for(start_ms: int, end_ms: int) -> list[str]:
    out, d = [], day_ms(day_str(start_ms))
    while d <= end_ms:
        out.append(day_str(d))
        d += DAY_MS
    return out


def needed_1m(events: list[dict[str, Any]], controls: dict[tuple[str, int], list[int]]) -> set[tuple[str, str]]:
    need: set[tuple[str, str]] = set()
    for e in events:
        T = e["signal_ts"]
        for d in days_for(T - MIN_MS, T + 30 * MIN_MS + 4 * HOUR_MS):
            need.add((e["symbol"], d))
        for c in controls[(e["symbol"], e["ts"])]:
            for d in days_for(c - MIN_MS, c + 2 * HOUR_MS + MIN_MS):
                need.add((e["symbol"], d))
    return need


async def stage_1m(fx: Fetcher, cache: Path, need: set[tuple[str, str]]) -> None:
    rt = fx.rt
    jobs = []
    for s, d in sorted(need):

        async def go(s: str = s, d: str = d) -> Any:
            return [r[:5] for r in kline_rows((await rt.binance_vision.um_klines_daily(s, "1m", d)).data)]

        jobs.append((cache / "k1m" / s / f"{d}.json", go))
    await fx.many("klines 1m", jobs)


def load_1m(
    cache: Path, sym: str, days: list[str], memo: dict[tuple[str, str], dict[int, tuple[float, ...]]]
) -> dict[int, tuple[float, ...]]:
    out: dict[int, tuple[float, ...]] = {}
    for d in days:
        k = (sym, d)
        if k not in memo:
            p = cache / "k1m" / sym / f"{d}.json"
            rows = read_json(p) if p.exists() else None
            memo[k] = {int(r[0]): (r[1], r[2], r[3], r[4]) for r in rows or []}
        out.update(memo[k])
    return out


# ---------------------------------------------------------------- simulation


def simulate_all(
    cache: Path,
    events: list[dict[str, Any]],
    controls: dict[tuple[str, int], list[int]],
    opens: dict[str, tuple[np.ndarray, np.ndarray]],
    uni: dict[int, list[str]],
) -> list[dict[str, Any]]:
    open_maps: dict[str, dict[int, float]] = {}

    def omap(s: str) -> dict[int, float]:
        if s not in open_maps:
            ts, op = opens.get(s, (np.array([], dtype=np.int64), np.array([])))
            open_maps[s] = dict(zip(ts.tolist(), op.tolist(), strict=True))
        return open_maps[s]

    memo: dict[tuple[str, str], dict[int, tuple[float, ...]]] = {}
    rows = []
    for n, e in enumerate(events):
        s, T, d = e["symbol"], e["signal_ts"], e["direction"]
        m1 = load_1m(cache, s, days_for(T - 6 * MIN_MS, T + 30 * MIN_MS + 4 * HOUR_MS), memo)
        r: dict[str, Any] = {k: e[k] for k in ("symbol", "ts", "signal_ts", "direction", "ret", "z", "day", "bar_qv")}
        r["half"] = 1 if e["ts"] < SPLIT else 2
        # consistency: the 1m bars inside the event bar must reproduce its 5m close
        last = m1.get(T - MIN_MS)
        r["m1_close_match"] = None if last is None else abs(last[3] / e["close"] - 1) < 1e-9
        r["entry_qv_1m"] = None
        variants: dict[str, dict[str, Any] | None] = {}
        for hn, h in HORIZONS.items():
            variants[f"close1m|{hn}"] = simulate_trade(m1, T + MIN_MS, d, h, cost=0.0)
            variants[f"worst1m|{hn}"] = simulate_trade(m1, T + MIN_MS, d, h, cost=0.0, worst_entry=True)
            variants[f"next5mopen|{hn}"] = simulate_trade(m1, T, d, h, cost=0.0)
            variants[f"delay15|{hn}"] = simulate_trade(m1, T + 15 * MIN_MS, d, h, cost=0.0)
            variants[f"delay30|{hn}"] = simulate_trade(m1, T + 30 * MIN_MS, d, h, cost=0.0)
            level = e.get("low") if d > 0 else e.get("high")
            held = held_extreme(m1, T, 5, d, level) if level is not None else None
            variants[f"held5m|{hn}"] = simulate_trade(m1, T + 5 * MIN_MS, d, h, cost=0.0) if held else None
            r[f"held5m_ok|{hn}"] = held
            variants[f"close1m_stop3|{hn}"] = simulate_trade(m1, T + MIN_MS, d, h, cost=0.0, stop=0.03)
        r["v"] = {
            k: (None if v is None else {"g": v["gross"], "mae": v["mae"], "mfe": v["mfe"], "st": v["stopped"]}) for k, v in variants.items()
        }
        # matched random controls (primary mechanics only)
        cg = []
        for c in controls[(s, e["ts"])]:
            cm = load_1m(cache, s, days_for(c - MIN_MS, c + 2 * HOUR_MS + MIN_MS), memo)
            res = simulate_trade(cm, c + MIN_MS, d, 2 * HOUR_MS, cost=0.0)
            if res is not None:
                cg.append(res["gross"])
        r["control_g"] = cg
        # market: equal-weight basket of the other universe members, and BTC, over [T, T+h] from 5m opens
        members = [x for x in uni.get(month_start(e["ts"]), []) if x != s]
        for hn, h in HORIZONS.items():
            br = [w for x in members if (w := window_return(omap(x), T, T + h)) is not None]
            r[f"basket|{hn}"] = statistics.fmean(br) if br else None
            r[f"basket_n|{hn}"] = len(br)
            r[f"btc|{hn}"] = window_return(omap(BTC), T, T + h) if s != BTC else None
        rows.append(r)
        if len(memo) > 4000:
            memo.clear()
        if (n + 1) % 500 == 0:
            print(f"  simulated {n + 1}/{len(events)}", flush=True)
    return rows


# ---------------------------------------------------------------- report


def pct(x: float | None, nd: int = 2) -> str:
    return "—" if x is None else f"{x * 100:+.{nd}f}%"


def ci_txt(ci: tuple[float, float] | None) -> str:
    return "—" if not ci else f"[{ci[0] * 100:+.2f}; {ci[1] * 100:+.2f}]"


HDR = "| Вибірка | n | днів | середнє | медіана | 95% CI (кластери-дні) | p (одност.) | частка > 0 |\n|---|---|---|---|---|---|---|---|"


def line(label: str, vals: list[float], days: list[str]) -> tuple[str, dict[str, Any]]:
    st = summarise(vals, days)
    if not st["n"]:
        return f"| {label} | 0 | 0 | — | — | — | — | — |", st
    p = "—" if st["p"] is None else f"{st['p']:.4f}"
    return (
        f"| {label} | {st['n']} | {st['days']} | {pct(st['mean'])} | {pct(st['median'])} | {ci_txt(st['ci'])} | {p} | {st['win']:.0%} |",
        st,
    )


def net(r: dict[str, Any], key: str, cost: float = COST) -> float | None:
    v = r["v"].get(key)
    return None if v is None else v["g"] - cost


def pick(
    rows: list[dict[str, Any]], key: str, cost: float = COST, pred: Callable[[dict[str, Any]], bool] | None = None
) -> tuple[list[float], list[str]]:
    vals, days = [], []
    for r in rows:
        if pred is not None and not pred(r):
            continue
        x = net(r, key, cost)
        if x is not None:
            vals.append(x)
            days.append(r["day"])
    return vals, days


def report(
    rows: list[dict[str, Any]],
    meta: dict[str, Any],
    grid_rows: list[str],
    cache: Path,
) -> str:
    L: list[str] = []
    P = "close1m|2h"
    prim = [r for r in rows if r["v"].get(P) is not None]
    L.append("# Відскок після ліквідаційних флешів: Binance USDT-M, 5m-події, вхід на 1m\n")
    for k, v in meta.items():
        L.append(f"- {k}: {v}")
    L.append("")

    L.append("## Основна комірка (зафіксована наперед): вхід close 1m-бару після закриття бару події, 2 год, без стопу, витрати 0.30 %\n")
    L.append("Дохід на номінал угоди, рівна вага подій.\n")
    L.append(HDR)
    v_all, d_all = pick(prim, P)
    s_line, st_all = line("**усі події (обидва напрямки)**", v_all, d_all)
    L.append(s_line)
    vl, dl = pick(prim, P, pred=lambda r: r["direction"] > 0)
    vs, ds = pick(prim, P, pred=lambda r: r["direction"] < 0)
    sl_line, st_l = line("лонг після падіння", vl, dl)
    ss_line, st_s = line("шорт після сквізу", vs, ds)
    L.append(sl_line)
    L.append(ss_line)
    v1, d1 = pick(prim, P, pred=lambda r: r["half"] == 1)
    v2, d2 = pick(prim, P, pred=lambda r: r["half"] == 2)
    h1_line, st_h1 = line(f"перша половина (< {day_str(SPLIT)})", v1, d1)
    h2_line, st_h2 = line(f"друга половина (≥ {day_str(SPLIT)})", v2, d2)
    L.append(h1_line)
    L.append(h2_line)
    vc, dc = pick(prim, P, pred=lambda r: r["day"] != CRASH_DAY)
    c_line, st_nc = line(f"без {CRASH_DAY}", vc, dc)
    L.append(c_line)
    vt, dt, dropped = without_top_days(v_all, d_all, 3)
    t_line, st_nt = line(f"без топ-3 днів ({', '.join(dropped)})", vt, dt)
    L.append(t_line)
    gross_line, _ = line("брутто (без витрат), довідково", *pick(prim, P, cost=0.0))
    L.append(gross_line)
    pl, ps = st_l.get("p"), st_s.get("p")
    if pl is not None and ps is not None:
        hl = holm([pl, ps])
        L.append(f"\nHolm для двох вторинних тверджень (лонг, шорт): скориговані p = {hl[0]:.4f} / {hl[1]:.4f}.")

    L.append("\n## Контролі\n")
    L.append(HDR)
    diff_v, diff_d, ctrl_v, ctrl_d = [], [], [], []
    for r in prim:
        if r["control_g"]:
            c = statistics.fmean(r["control_g"]) - COST
            ctrl_v.append(c)
            ctrl_d.append(r["day"])
            diff_v.append(net(r, P) - c)
            diff_d.append(r["day"])
    cl, _ = line(f"випадкові входи-контроль ({N_CONTROLS} на подію, та сама хвилина доби, ±1…14 діб, нетто)", ctrl_v, ctrl_d)
    L.append(cl)
    dl_, st_diff = line("**подія − контроль**", diff_v, diff_d)
    L.append(dl_)
    st_basket: dict[str, Any] = {}
    for lab, key in (("**понад рівноважний кошик всесвіту**", "basket|2h"), ("понад BTC (без подій BTC)", "btc|2h")):
        ev, ed = [], []
        for r in prim:
            if r.get(key) is not None:
                ev.append(net(r, P) - r["direction"] * r[key])
                ed.append(r["day"])
        ln, st_x = line(lab, ev, ed)
        L.append(ln)
        if key.startswith("basket"):
            st_basket = st_x
    L.append(
        "\nКошик і BTC — з 5m-барів: open бару T → open бару T + 2 год (зсув на 1 хв раніше за угоду); це діагностика "
        "ринкової бети, а не торгований хедж (витрати хеджа не враховано)."
    )

    # pre-registered decision
    L.append("\n## Перевірка заздалегідь зафіксованого критерію\n")
    checks = [
        (
            "n ≥ 150 і ≥ 25 днів",
            st_all.get("n", 0) >= 150 and st_all.get("days", 0) >= 25,
            f"n={st_all.get('n')}, днів={st_all.get('days')}",
        ),
        ("нижня межа CI > 0", bool(st_all.get("ci")) and st_all["ci"][0] > 0, ci_txt(st_all.get("ci"))),
        ("середнє ≥ +0.5 %", st_all.get("mean", -1) >= 0.005, pct(st_all.get("mean"))),
        ("медіана ≥ +0.3 %", st_all.get("median", -1) >= 0.003, pct(st_all.get("median"))),
        ("без топ-3 днів > 0", st_nt.get("mean", -1) > 0, pct(st_nt.get("mean"))),
        (f"без {CRASH_DAY} > 0", st_nc.get("mean", -1) > 0, pct(st_nc.get("mean"))),
        ("CI (подія − контроль) > 0", bool(st_diff.get("ci")) and st_diff["ci"][0] > 0, ci_txt(st_diff.get("ci"))),
        ("CI понад кошик > 0", bool(st_basket.get("ci")) and st_basket["ci"][0] > 0, ci_txt(st_basket.get("ci"))),
        (
            "обидві половини > 0",
            st_h1.get("mean", -1) > 0 and st_h2.get("mean", -1) > 0,
            f"{pct(st_h1.get('mean'))} / {pct(st_h2.get('mean'))}",
        ),
    ]
    L.append("| Умова | Виконано | Значення |\n|---|---|---|")
    for name, ok, val in checks:
        L.append(f"| {name} | {'так' if ok else 'ні'} | {val} |")
    kill_a = not (st_all.get("mean", -1) > 0 and st_all.get("ci") and st_all["ci"][0] > 0)
    kill_b = not st_nt.get("mean", -1) > 0
    kill_c = not (st_diff.get("ci") and st_diff["ci"][0] > 0)
    all_ok = all(ok for _, ok, _ in checks)
    verdict = "rejected" if (kill_a or kill_b or kill_c) else ("confirmed" if all_ok else "inconclusive")
    L.append(
        f"\nКритерії вбити: (а) {'так' if kill_a else 'ні'}, (б) {'так' if kill_b else 'ні'}, (в) {'так' if kill_c else 'ні'}. **Механічний вердикт: {verdict}.**"
    )

    L.append("\n## Сітка (розвідувальна, усі варіанти): нетто 0.30 %, обидва напрямки\n")
    L.append("| Вхід \\ горизонт | " + " | ".join(HORIZONS) + " |\n|---|" + "---|" * len(HORIZONS))
    names = {
        "close1m": "close 1m після бару (основний вхід)",
        "worst1m": "найгірша ціна 1m-бару входу (стрес)",
        "next5mopen": "open наступного 5m-бару (чутливість, оптимістично)",
        "delay15": "затримка 15 хв (людина за алертом)",
        "delay30": "затримка 30 хв (людина за алертом)",
        "held5m": "лише якщо наступні 5 хв не оновили екстремум, вхід на +5 хв",
        "close1m_stop3": "основний вхід, стоп −3 %",
    }
    for k, lab in names.items():
        cells = []
        for hn in HORIZONS:
            vals, days = pick(rows, f"{k}|{hn}")
            st = summarise(vals, days, n_boot=2000)
            cells.append("—" if not st["n"] else f"{pct(st['mean'])} {ci_txt(st['ci'])} n={st['n']}")
        L.append(f"| {lab} | " + " | ".join(cells) + " |")
    for direction, lab in ((1, "лонг"), (-1, "шорт")):
        L.append(f"\n{lab}:\n")
        L.append("| Вхід \\ горизонт | " + " | ".join(HORIZONS) + " |\n|---|" + "---|" * len(HORIZONS))
        for k, kl in names.items():
            cells = []
            for hn in HORIZONS:
                vals, days = pick(rows, f"{k}|{hn}", pred=lambda r, direction=direction: r["direction"] == direction)
                st = summarise(vals, days, n_boot=2000)
                cells.append("—" if not st["n"] else f"{pct(st['mean'])} {ci_txt(st['ci'])}")
            L.append(f"| {kl} | " + " | ".join(cells) + " |")

    L.append("\n## Витрати (основна комірка)\n")
    L.append(HDR)
    for c in (0.0, 0.002, 0.003, 0.004, 0.007):
        L.append(line(f"витрати {c * 100:.1f}% за коло", *pick(prim, P, cost=c))[0])

    L.append("\n## Чутливість порогів події (розвідувальна, основна комірка)\n")
    L.append(HDR)
    L.extend(grid_rows)

    L.append("\n## Хвіст і ризик (основна комірка)\n")
    mae = [r["v"][P]["mae"] for r in prim]
    if mae:
        L.append(
            f"- MAE (найгірший рух проти позиції за 2 год, від ціни входу): медіана {pct(quantile(mae, 0.5))}, p10 {pct(quantile(mae, 0.1))}, "
            f"p1 {pct(quantile(mae, 0.01))}, мінімум {pct(min(mae))}."
        )
        L.append(
            f"- Частка угод з MAE ≤ −3 %: {sum(1 for x in mae if x <= -0.03) / len(mae):.0%}; ≤ −10 %: {sum(1 for x in mae if x <= -0.10) / len(mae):.1%}."
        )
        w = min(prim, key=lambda r: net(r, P))
        b = max(prim, key=lambda r: net(r, P))
        L.append(
            f"- Найгірша угода: {w['symbol']} {datetime.fromtimestamp(w['ts'] / 1000, tz=UTC):%Y-%m-%d %H:%M} ({'лонг' if w['direction'] > 0 else 'шорт'}) {pct(net(w, P))}."
        )
        L.append(
            f"- Найкраща угода: {b['symbol']} {datetime.fromtimestamp(b['ts'] / 1000, tz=UTC):%Y-%m-%d %H:%M} ({'лонг' if b['direction'] > 0 else 'шорт'}) {pct(net(b, P))}."
        )
        byday: dict[str, list[float]] = defaultdict(list)
        for r in prim:
            byday[r["day"]].append(net(r, P))
        top = sorted(byday.items(), key=lambda x: -sum(x[1]))
        L.append("- Найбільші дні за сумою: " + ", ".join(f"{d} ({len(v)} под., {pct(sum(v))})" for d, v in top[:5]) + ".")
        L.append("- Найгірші дні: " + ", ".join(f"{d} ({len(v)} под., {pct(sum(v))})" for d, v in top[-5:]) + ".")
        bysym: dict[str, float] = defaultdict(float)
        for r in prim:
            bysym[r["symbol"]] += net(r, P)
        ts_ = sorted(bysym.items(), key=lambda x: -x[1])
        L.append(f"- Різних символів: {len(bysym)}; найбільший внесок: " + ", ".join(f"{s} {pct(v)}" for s, v in ts_[:5]) + ".")
        multi = Counter(r["day"] for r in prim)
        L.append(
            f"- Подій на день: медіана {statistics.median(multi.values()):.0f}, максимум {max(multi.values())} ({multi.most_common(1)[0][0]})."
        )
        per_month = Counter(day_str(r["ts"])[:7] for r in prim)
        L.append("- Подій за місяць: " + ", ".join(f"{k} {v}" for k, v in sorted(per_month.items())) + ".")
        qv = [r["bar_qv"] for r in prim]
        L.append(f"- Quote-об'єм бару події: медіана {statistics.median(qv) / 1e6:.1f} млн USDT, p10 {quantile(qv, 0.1) / 1e6:.2f} млн.")
    report_text = "\n".join(L)
    (cache / "verdict.txt").write_text(verdict + "\n")
    return report_text


def threshold_grid(cache: Path, uni: dict[int, list[str]]) -> list[str]:
    out = []
    for ret, z in ((0.03, 5.0), (0.06, 5.0), (0.04, 3.0), (0.04, 8.0)):
        evs = detect_cached(cache, uni, ret, z)
        memo: dict[tuple[str, str], dict[int, tuple[float, ...]]] = {}
        vals, days = [], []
        for e in evs:
            T = e["signal_ts"]
            m1 = load_1m(cache, e["symbol"], days_for(T - MIN_MS, T + 2 * HOUR_MS + MIN_MS), memo)
            if not m1:
                continue
            r = simulate_trade(m1, T + MIN_MS, e["direction"], 2 * HOUR_MS, cost=COST)
            if r is not None:
                vals.append(r["net"])
                days.append(e["day"])
            if len(memo) > 4000:
                memo.clear()
        ln, _ = line(f"|r| ≥ {ret:.0%}, z > {z:g} (подій {len(evs)}, з 1m-даними {len(vals)})", vals, days)
        out.append(ln)
    return out


# ---------------------------------------------------------------- main


async def fetch_all(cache: Path, sample: int | None, grid: bool) -> None:
    _, rt = build_server()
    logging.getLogger("httpx").setLevel(logging.WARNING)  # per-request INFO lines would flood the progress log
    try:
        fx = Fetcher(rt, cache)
        symbols, info = await stage_meta(rt, cache)
        cands = candidate_symbols(symbols, info)
        print(f"archive USDT-M symbols: {len(symbols)}; eligible names: {len(cands)}", flush=True)
        if sample:
            cands = cands[:sample]
        await stage_daily(fx, cache, cands)
        if sample:
            return
        uni = build_universe(cache, cands)
        print(f"universe months: {len(uni)}; distinct members: {len({s for v in uni.values() for s in v})}", flush=True)
        await stage_5m(fx, cache, uni)
        events = detect_cached(cache, uni)
        controls = control_plan(events)
        print(f"events: {len(events)} on {len({e['day'] for e in events})} days", flush=True)
        need = needed_1m(events, controls)
        if grid:
            for ret, z in ((0.03, 5.0), (0.06, 5.0), (0.04, 3.0), (0.04, 8.0)):
                evs = detect_cached(cache, uni, ret, z)
                print(f"grid |r|>={ret:g} z>{z:g}: {len(evs)} events", flush=True)
                for e in evs:
                    for d in days_for(e["signal_ts"] - MIN_MS, e["signal_ts"] + 2 * HOUR_MS + MIN_MS):
                        need.add((e["symbol"], d))
        await stage_1m(fx, cache, need)
        print("FETCH COMPLETE", flush=True)
    finally:
        await rt.aclose()


async def consistency_check(cache: Path) -> str:
    """Archive vs live API (ccxt) for one known bar: BTCUSDT 5m 2025-10-10 21:20 UTC (the cascade low)."""
    ts = int(datetime(2025, 10, 10, 21, 20, tzinfo=UTC).timestamp() * 1000)
    a = load_5m(cache, BTC, [month_start(ts)])
    row = a[np.searchsorted(a[:, 0], ts)] if a.shape[0] else None
    _, rt = build_server()
    try:
        f = await rt.exchanges.ohlcv("binanceusdm", "BTC/USDT:USDT", "5m", 1, since_ms=ts)
        live = (f.data or [None])[0]
    except ProviderError as e:
        live = None
        print(f"live check failed: {e}", flush=True)
    finally:
        await rt.aclose()
    if row is None or live is None:
        return "не виконано"
    return (
        f"архів O/H/L/C {row[1]:.1f}/{row[2]:.1f}/{row[3]:.1f}/{row[4]:.1f}, base vol {row[6]:.1f}; "
        f"live API {live[1]:.1f}/{live[2]:.1f}/{live[3]:.1f}/{live[4]:.1f}, base vol {live[5]:.1f}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--stage", choices=("fetch", "report"), default="fetch")
    ap.add_argument("--sample", type=int, default=0, help="Fetch stage: only the first N symbols' daily data (timing).")
    ap.add_argument("--no-grid", action="store_true", help="Skip the event-threshold sensitivity rows.")
    ap.add_argument("--out", default="")
    ap.add_argument("--log", default="", help="Append progress lines to this file (line-buffered) instead of stdout.")
    a = ap.parse_args()
    if a.log:
        sys.stdout = open(a.log, "a", buffering=1)  # noqa: SIM115 - lives for the whole run
        sys.stderr = sys.stdout
        logging.getLogger("httpx").setLevel(logging.WARNING)
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "fetch":
        asyncio.run(fetch_all(cache, a.sample or None, not a.no_grid))
        return
    symbols, info = read_json(cache / "symbols.json"), read_json(cache / INFO_FILE)
    cands = candidate_symbols(symbols, info)
    uni = build_universe(cache, cands)
    members = sorted({s for v in uni.values() for s in v})
    events, opens, diag = detect_all(cache, uni, keep_opens=True)
    controls = control_plan(events)
    print(f"events {len(events)}; simulating", flush=True)
    rows = simulate_all(cache, events, controls, opens, uni)
    (cache / "trades.json").write_text(json.dumps(rows))
    not_listed = [s for s in members if (info.get(s) or {}).get("status") != "TRADING"]
    excluded_type = sorted(s for s in symbols if s.endswith("USDT") and "_" not in s and effective_type(info, s) not in (None, "COIN"))
    daily = load_daily(cache, cands)
    traded_in_period = [s for s, rows_ in daily.items() if any(START <= d < END for d, _ in rows_)]
    delisted_in_period = [s for s in traded_in_period if (info.get(s) or {}).get("status") != "TRADING"]
    match = [r["m1_close_match"] for r in rows if r["m1_close_match"] is not None]
    no_1m = sum(1 for r in rows if r["v"]["close1m|2h"] is None)
    meta = {
        "Період подій": f"{day_str(START)} … {day_str(END - DAY_MS)} (UTC), розбиття на половини {day_str(SPLIT)}",
        "Символів USDT-M в архіві (усі роки)": len(symbols),
        "Придатних назв (COIN, без стейблів/обгорнутих/індексів)": len(cands),
        "Торгувались у періоді": len(traded_in_period),
        "З них зараз не торгуються (делістнуті/зупинені)": f"{len(delisted_in_period)} ({len(delisted_in_period) / max(1, len(traded_in_period)):.0%})",
        "Різних членів місячного топ-50": len(members),
        "Із них зараз не торгуються": f"{len(not_listed)}: {', '.join(not_listed)}",
        "Відсіяно як не-COIN (TradFi/індекси) за exchangeInfo": f"{len(excluded_type)}: {', '.join(excluded_type[:40])}{' …' if len(excluded_type) > 40 else ''}",
        "5m-барів завантажено": diag["bars"],
        "Розривів > 1 доби в 5m-рядах (символів)": f"{diag['gaps_over_1d']} ({len(diag['gap_symbols'])}); зазвичай це місяці поза всесвітом, подія вимагає ≥1500 барів історії за 7 діб",
        "Медіана quote_volume / (base_volume × close) по символах": f"{statistics.median(diag['qv_ratio']):.4f} (одиниці: USDT)",
        "Подій (основне визначення)": f"{len(events)} у {len({e['day'] for e in events})} днях; лонг {sum(1 for e in events if e['direction'] > 0)}, шорт {sum(1 for e in events if e['direction'] < 0)}",
        "Подій без 1m-даних для основної комірки (відкинуто)": no_1m,
        "1m-закриття останньої хвилини = 5m-закриття бару події": f"{sum(match)}/{len(match)}",
        "Звірка архіву з live API (BTCUSDT 5m 2025-10-10 21:20)": asyncio.run(consistency_check(cache)),
    }
    grid_rows = [] if a.no_grid else threshold_grid(cache, uni)
    text = report(rows, meta, grid_rows, cache)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")


if __name__ == "__main__":
    sys.exit(main())
