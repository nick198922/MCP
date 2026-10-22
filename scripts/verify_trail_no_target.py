#!/usr/bin/env python3
"""IDEA-20261022-trail-no-target: the structural trail without a target (C2) against fixed stop and target (A) on NEW
symbols: Binance USD-M perps ranked 31-100 by trailing quote volume, point-in-time, without the step-1 top-30 members.

Entries, costs, R, path rules, swings, trail buffer and max hold are those of the first run
(``verify_demo_stop_management``: ``signals``, ``rules``, ``SW_BUF``, ``TFS``) and ``analytics.stop_management``.

Stages:
  universe  rank band 31-100 per month from the daily volume caches (no network)  -> --cache-dir/universe.json
  fetch     monthly 1h zips via rt.binance_vision (step-1 loader) for members, per-file cache --cache-dir/k1h
            (files already in the step-1 cache are read from there); --limit N for a timing sample
  sanity    reproduction of the first run on old-universe symbols, price levels, holes, delisted, funding steps
  run       per-symbol trade files into --cache-dir/trades (4 worker processes)
  report    tables; the full output goes to --cache-dir/report.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (step-1 loaders, universe, period)
import verify_demo_stop_management as dsm  # noqa: E402  (first run: entries, rules, stats helpers)

from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm, monthly_universe  # noqa: E402
from tradedesk.analytics.stop_management import (  # noqa: E402
    Fees,
    StopRule,
    chi2_sf,
    cochran_q,
    max_drawdown,
    risk_unit,
    simulate_stop_rule,
    swing_stops,
)
from tradedesk.analytics.trail_no_target import dedup_by_base, drop_best, flat_paid, funding_paid, rank_band  # noqa: E402

DAY_MS = 24 * HOUR_MS
CACHE = Path.home() / ".cache/tradedesk-agents/trail_no_target"
S1_CACHE = Path.home() / ".cache/tradedesk-agents/course_s14_ema"
DATA_DIR = Path.home() / ".cache/tradedesk-agents/funding_crowding"
OLD_TRADES = Path.home() / ".cache/tradedesk-agents/demo_stop_management/trades"
BAND = (31, 100)
FEES = Fees()
A = StopRule("A")
C2 = StopRule("C2", trail=True, use_target=False)  # = verify_demo_stop_management.rules()["C2"]
# variant -> (rule, trail buffer in ATR, hold multiplier); C2 base = buffer 0.5 (SW_BUF["C2"]), hold x1
VARS: dict[str, tuple[StopRule, float, float]] = {
    "A": (A, 0.5, 1.0),
    "C2": (C2, dsm.SW_BUF["C2"], 1.0),
    "C2b025": (C2, 0.25, 1.0),
    "C2b100": (C2, 1.0, 1.0),
    "C2h05": (C2, 0.5, 0.5),
    "C2h2": (C2, 0.5, 2.0),
}
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


# ---------------------------------------------------------------- universe and bars


def stage_universe(cache: Path, s1c: Path, dd: Path) -> None:
    syms = s1.eligible_syms(dd)
    emit(f"eligible USD-M COIN perps (incl. delisted): {len(syms)}")
    qv = s1.daily_qv(s1c, dd, syms)
    old = s1.members(s1c)
    uni: dict[str, dict[str, int]] = {}
    mism, short = [], []
    for mo in s1.months(s1.FIRST_UNI_MONTH, s1.LAST_MONTH):
        ranked = monthly_universe(qv, s1.ms(f"{mo}-01T00:00"), top_n=200)
        top = dedup_by_base(ranked)[: s1.TOP_N]
        if top != old[mo][: s1.TOP_N]:
            mism.append(mo)
        band = rank_band(ranked, *BAND, exclude=old[mo])
        if len(dedup_by_base(ranked)) < BAND[1]:
            short.append((mo, len(dedup_by_base(ranked))))
        uni[mo] = band
    s1.wr(cache / "universe.json", uni)
    info = s1.rd(dd / "exchange_info.json")["info"]
    allm = sorted({s for v in uni.values() for s in v})
    old_all = {s for v in old.values() for s in v}
    st = Counter((info.get(s) or {}).get("status", "absent") for v in uni.values() for s in v)
    emit(f"ranks 1-30 equal to the step-1 universe: {len(uni) - len(mism)}/{len(uni)} months; mismatches {mism[:10]}")
    emit(f"months with fewer than {BAND[1]} ranked bases: {short[:12]}{' ...' if len(short) > 12 else ''}")
    emit(f"months {len(uni)}; member-months {sum(len(v) for v in uni.values())}; distinct members {len(allm)}")
    emit(f"distinct members never in the old universe: {len(set(allm) - old_all)}; member-months by today's status {dict(st)}")
    for mo in ("2020-02", "2022-06", "2024-01", "2026-09"):
        v = uni[mo]
        emit(f"  {mo}: {len(v)} members, first {sorted(v, key=v.get)[:6]}")


def members(cache: Path) -> dict[str, dict[str, int]]:
    return s1.rd(cache / "universe.json")


def stage_fetch(cache: Path, s1c: Path, dd: Path, limit: int | None) -> None:
    syms = sorted({s for v in members(cache).values() for s in v})
    jobs, have = [], 0
    for s in syms:
        for mo in s1.listing(dd, s):
            if (s1c / "k1h" / s / f"{mo}.json").exists() or (cache / "k1h" / s / f"{mo}.json").exists():
                have += 1
                continue
            jobs.append((s, "1h", mo, cache / "k1h" / s / f"{mo}.json"))
    emit(f"members {len(syms)}; 1h files cached {have}; missing {len(jobs)}")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request otherwise
    t0 = time.time()
    asyncio.run(s1.fetch_jobs(jobs[:limit] if limit else jobs, "k1h"))
    emit(f"[fetch] finished in {time.time() - t0:.0f}s, peak rss {dsm.rss_mb():.0f} MB")


def load_1h(cache: Path, s1c: Path, dd: Path, s: str) -> Bars:
    """As ``verify_course_s14_ema.load_1h``, each month read from the step-1 cache or this run's cache."""
    rows: list[list[float]] = []
    for mo in s1.listing(dd, s):
        for base in (s1c, cache):
            p = base / "k1h" / s / f"{mo}.json"
            if p.exists():
                rows.extend(s1.rd(p) or [])
                break
    a = np.array(rows, dtype=float) if rows else np.zeros((0, 6))
    if a.size:
        a = a[np.argsort(a[:, 0], kind="stable")]
        a = a[np.concatenate([[True], a[1:, 0] != a[:-1, 0]])]
        a = a[(a[:, 5] > 0) & (a[:, 0] % HOUR_MS == 0)]
    return Bars.from_rows(a[:, :5] if a.size else a, HOUR_MS)


def load_funding(dd: Path, s: str) -> tuple[np.ndarray, np.ndarray, list[float]]:
    d = dd / "funding" / s
    rows: list[list[float]] = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            rows.extend(s1.rd(p) or [])
    if not rows:
        return np.zeros(0, dtype=np.int64), np.zeros(0), []
    a = np.array(rows, dtype=float)
    a = a[np.argsort(a[:, 0], kind="stable")]
    return a[:, 0].astype(np.int64), a[:, 2], sorted(set(a[:, 1].tolist()))


# ---------------------------------------------------------------- per-symbol run


def run_symbol(args: tuple[str, str, str, str, str, str]) -> dict[str, Any]:
    """mode 'new': membership from this run's universe; 'old': the step-1 universe (reproduction of the first run)."""
    s, mode, cache_s, s1c_s, dd_s, out_s = args
    t0 = time.time()
    cache, s1c, dd = Path(cache_s), Path(s1c_s), Path(dd_s)
    uni: dict[str, Any] = members(cache) if mode == "new" else s1.members(s1c)
    member = s1.Membership(uni)
    h1 = load_1h(cache, s1c, dd, s)
    ft, fr, _ = load_funding(dd, s)
    cands = []
    swings = {}
    for tf, hours, hold, end in dsm.TFS:
        tb = resample(h1, hours)
        for side in ("long", "short"):
            bb = tb if side == "long" else tb.mirrored()
            a = atr(bb, 14, segment_pos(bb.t))
            swings[(tf, side)] = {k: swing_stops(bb, a, k=2, buf_atr=k) for k in dsm.BUFS}
            for setup, i, e, st, tg in dsm.signals(bb):
                t_in = bb.close_time(i)
                if not (dsm.START_MS <= t_in < end) or not member(s, t_in) or not (st < e < tg):
                    continue
                cands.append((t_in, dsm.SETUP_ORDER[setup], dsm.TF_ORDER[tf], dsm.SIDE_ORDER[side], setup, tf, side, e, st, tg, hold))
    cands.sort(key=lambda x: x[:4])
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    busy = -1
    for t_in, _, _, _, setup, tf, side, e, st, tg, hold in cands:
        why[f"{setup}:{tf}:cand"] += 1
        if t_in < busy:
            why[f"{setup}:{tf}:busy"] += 1
            continue
        path = h1 if side == "long" else h1.mirrored()
        base = simulate_stop_rule(path, t_in, e, st, tg, hold, A)
        if base is None:
            why[f"{setup}:{tf}:no_path"] += 1
            continue
        busy = base.exit_ms
        ru = risk_unit(e, st, FEES)
        sign = 1 if side == "long" else -1
        mo = s1.month_of(t_in)
        row: dict[str, Any] = {
            "sym": s,
            "setup": setup,
            "tf": tf,
            "side": side,
            "entry_ms": t_in,
            "day": t_in // DAY_MS,
            "oos": t_in >= dsm.OOS_MS,
            "rank": int(uni[mo][s]) if mode == "new" else 0,
            "stop_pct": (e - st) / abs(e),
            "target_r": (tg - e) / (e - st),
            "te": (FEES.taker_exit - FEES.maker) * abs(e) / ru,  # entry as a taker instead of a maker, in R
        }
        for v, (rule, buf, hk) in VARS.items():
            o = base if v == "A" else simulate_stop_rule(path, t_in, e, st, tg, int(hold * hk), rule, swings=swings[(tf, side)][buf])
            row[f"{v}_r"] = o.r_net
            row[f"{v}_c"] = o.cost_r
            row[f"{v}_k"] = o.exit_kind
            row[f"{v}_x"] = o.exit_ms
            if v in ("A", "C2"):
                paid, src = funding_paid(ft, fr, t_in, o.exit_ms, sign)
                row[f"{v}_fa"] = paid * abs(e) / ru
                row[f"{v}_ff"] = flat_paid(t_in, o.exit_ms) * abs(e) / ru
                row[f"{v}_fs"] = src
        rows.append(row)
        why[f"{setup}:{tf}:trade"] += 1
    (Path(out_s) / "trades").mkdir(parents=True, exist_ok=True)
    (Path(out_s) / "trades" / f"{s}.json").write_text(json.dumps({"rows": rows, "check": [], "why": dict(why)}))
    gaps = int(np.sum(np.diff(h1.t) > HOUR_MS)) if len(h1) else 0
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t0, "bars": len(h1), "gaps": gaps, "rss": dsm.rss_mb()}


# ---------------------------------------------------------------- sanity


def stage_sanity(cache: Path, s1c: Path, dd: Path) -> None:
    out = cache / "sanity"
    emit("## reproduction of the first run (old universe, same code path) vs demo_stop_management/trades")
    for s in ("BTCUSDT", "SOLUSDT", "DOGEUSDT", "LINKUSDT"):
        r = run_symbol((s, "old", str(cache), str(s1c), str(dd), str(out)))
        new = {(x["entry_ms"], x["setup"], x["tf"], x["side"]): x for x in json.loads((out / "trades" / f"{s}.json").read_text())["rows"]}
        old = {(x["entry_ms"], x["setup"], x["tf"], x["side"]): x for x in json.loads((OLD_TRADES / f"{s}.json").read_text())["rows"]}
        common = new.keys() & old.keys()
        dmax = {v: max((abs(new[k][f"{v}_r"] - old[k][f"{v}_r"]) for k in common), default=math.nan) for v in ("A", "C2")}
        kinds = sum(new[k]["C2_k"] != old[k]["C2_k"] for k in common)
        emit(
            f"{s}: trades new {len(new)} / old {len(old)}, common {len(common)}; max |dR| A {dmax['A']:.2e}, C2 {dmax['C2']:.2e}; "
            f"C2 exit-kind mismatches {kinds}; {r['sec']:.1f}s"
        )
    uni = members(cache)
    syms = sorted({s for v in uni.values() for s in v})
    info = s1.rd(dd / "exchange_info.json")["info"]
    emit(f"\nmembers {len(syms)}; not TRADING today: {sum(1 for s in syms if (info.get(s) or {}).get('status') != 'TRADING')}")
    emit(f"underlyingType of members today: {dict(Counter((info.get(s) or {}).get('underlyingType', 'absent') for s in syms))}")
    t0 = time.time()
    samp = [s for s in ("ARBUSDT", "TRBUSDT", "1000BONKUSDT", "ZILUSDT", "LUNAUSDT") if s in syms][:4] or syms[:4]
    for s in samp:
        h1 = load_1h(cache, s1c, dd, s)
        ft, fr, steps = load_funding(dd, s)
        r = run_symbol((s, "new", str(cache), str(s1c), str(dd), str(out)))
        missing = sum(
            1 for mo in s1.listing(dd, s) if not ((s1c / "k1h" / s / f"{mo}.json").exists() or (cache / "k1h" / s / f"{mo}.json").exists())
        )
        span = (
            f"{datetime.fromtimestamp(h1.t[0] / 1000, UTC):%Y-%m-%d}..{datetime.fromtimestamp(h1.t[-1] / 1000, UTC):%Y-%m-%d}"
            if len(h1)
            else "-"
        )
        emit(
            f"{s}: bars {len(h1)} ({span}), last close {h1.c[-1] if len(h1) else math.nan:.6g}, holes>1h {r['gaps']}, missing 1h files {missing}, "
            f"funding points {ft.size} intervals(h) {steps}, member-months {sum(1 for v in uni.values() if s in v)}, trades {r['n']}, {r['sec']:.1f}s"
        )
    el = time.time() - t0
    emit(f"timing: {el:.0f}s for {len(samp)} symbols -> about {el / max(1, len(samp)) * len(syms) / 4 / 60:.0f} min on 4 workers")


def stage_run(cache: Path, s1c: Path, dd: Path, limit: int | None, workers: int) -> None:
    syms = sorted({s for v in members(cache).values() for s in v})[: limit or None]
    jobs = [(s, "new", str(cache), str(s1c), str(dd), str(cache)) for s in syms]
    t0 = time.time()
    tot: Counter[str] = Counter()
    with Pool(workers) as pool:
        for k, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            tot.update(r["why"])
            if k % 25 == 0 or k == len(jobs):
                emit(f"  {k}/{len(jobs)} symbols, {time.time() - t0:.0f}s, last {r['sym']} n={r['n']}, worker rss {r['rss']:.0f} MB")
    emit(f"run done: {dict(sorted(tot.items()))}")


# ---------------------------------------------------------------- report


def summ(v: np.ndarray, day: np.ndarray) -> dict[str, Any]:
    return dsm.summ(v, day)


fs = dsm.fs


def slice_rows(T: dict[str, np.ndarray], groups: list[tuple[str, np.ndarray]], title: str) -> None:
    emit(f"\n### {title}")
    emit("| зріз | n | дат | E[R] A | E[R] C2 | Δ C2 − A [95 % CI] |\n|---|---|---|---|---|---|")
    for lab, m in groups:
        if m.sum() < 2:
            continue
        day = T["day"][m]
        d = summ(T["C2_r"][m] - T["A_r"][m], day)
        emit(f"| {lab} | {d['n']} | {d['d']} | {fs(summ(T['A_r'][m], day))} | {fs(summ(T['C2_r'][m], day))} | {fs(d)} |")


def stage_report(cache: Path, s1c: Path) -> None:
    T, _ = dsm.load(cache)
    n = T["sym"].size
    day = T["day"]
    old_all = {s for v in s1.members(s1c).values() for s in v}
    emit(f"trades {n}; symbols {np.unique(T['sym']).size}; dates {np.unique(day).size}; rss {dsm.rss_mb():.0f} MB")
    for key in ("setup", "tf", "side"):
        emit(f"{key}: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(T[key].tolist()).items())))
    IS, OOS = ~T["oos"].astype(bool), T["oos"].astype(bool)
    emit(
        f"IS {int(IS.sum())} / OOS {int(OOS.sum())}; mean stop {np.mean(T['stop_pct']):.2%}, median target {np.median(T['target_r']):.2f}R"
    )

    emit("\n## Основні тести (усі нові дані), Holm {Δ C2 − A, E[R] C2 проти −0.02R}")
    d = T["C2_r"] - T["A_r"]
    x1 = summ(d, day)
    x2 = summ(T["C2_r"], day)
    p2 = summ(T["C2_r"] + 0.02, day)["p"]
    adj = holm([x1["p"], p2])
    emit("| тест | n | дат | значення [95 % CI] | p (двобічне) | Holm p | MDE 80 % |\n|---|---|---|---|---|---|---|")
    emit(f"| Δ C2 − A (H0: 0) | {x1['n']} | {x1['d']} | {fs(x1)} | {x1['p']:.4f} | {adj[0]:.4f} | {2.8 * x1['se']:.3f} |")
    emit(f"| E[R] C2 (H0: −0.02) | {x2['n']} | {x2['d']} | {fs(x2)} | {p2:.4f} | {adj[1]:.4f} | {2.8 * x2['se']:.3f} |")
    emit(f"E[R] A: {fs(summ(T['A_r'], day))}")
    conf = x1["m"] >= 0.03 and x1["lo"] > 0 and x2["lo"] > -0.02 and max(adj) < 0.05
    rej = x1["hi"] < 0.03 or x1["m"] <= 0
    verdict = "confirmed" if conf else ("rejected" if rej else "inconclusive")
    emit(
        f"ПРАВИЛО ВЕРДИКТУ: {verdict} (Δ={x1['m']:+.3f}, CI [{x1['lo']:+.3f}; {x1['hi']:+.3f}], E[R] C2 CI lo={x2['lo']:+.3f}, Holm p {adj[0]:.4f} / {adj[1]:.4f})"
    )

    emit("\n## Варіанти: опис")
    emit("| варіант | E[R] net [95 % CI] | виграшів | медіана холду, год | макс. просадка, R |\n|---|---|---|---|---|")
    for v in ("A", "C2"):
        r = T[f"{v}_r"]
        hold = (T[f"{v}_x"] - T["entry_ms"]) / HOUR_MS
        emit(
            f"| {v} | {fs(summ(r, day))} | {np.mean(r > 0):.1%} | {np.median(hold):.0f} | {max_drawdown(r[np.argsort(T[f'{v}_x'], kind='stable')]):.1f} |"
        )
    emit("\n### Виходи")
    for v in ("A", "C2"):
        c = Counter(T[f"{v}_k"].tolist())
        emit(f"{v}: " + ", ".join(f"{k} {c[k] / n:.1%}" for k in sorted(c)))

    emit("\n## Хвіст: без найкращих угод (за R C2)")
    emit("| вибірка | n | E[R] C2 | Δ C2 − A |\n|---|---|---|---|")
    for lab, sh in (("усі", 0.0), ("без 1 %", 0.01), ("без 5 %", 0.05)):
        k = drop_best(T["C2_r"], sh)
        emit(f"| {lab} | {int(k.sum())} | {fs(summ(T['C2_r'][k], day[k]))} | {fs(summ(d[k], day[k]))} |")
    top1 = ~drop_best(T["C2_r"], 0.01)
    emit(
        f"сума R C2: усі {T['C2_r'].sum():+.1f}, найкращий 1 % ({int(top1.sum())} угод) {T['C2_r'][top1].sum():+.1f}; сума Δ {d.sum():+.1f}, з них у найкращому 1 % {d[top1].sum():+.1f}"
    )

    years = (T["entry_ms"] // DAY_MS * DAY_MS).astype("datetime64[ms]").astype("datetime64[Y]").astype(int) + 1970
    groups = [("IS", IS), ("OOS", OOS)]
    groups += [(f"setup {x}", T["setup"] == x) for x in ("S14", "S16", "S3")]
    groups += [(f"tf {x}", T["tf"] == x) for x in ("4h", "1d")]
    groups += [(x, T["side"] == x) for x in ("long", "short")]
    groups += [("ранг 31–50", T["rank"] <= 50), ("ранг 51–100", T["rank"] > 50)]
    slice_rows(T, groups, "Розрізи")
    slice_rows(T, [(str(y), years == y) for y in sorted(set(years.tolist()))], "За роками")
    slice_rows(
        T, [(f"{st} {tf}", (T["setup"] == st) & (T["tf"] == tf)) for st in ("S14", "S16", "S3") for tf in ("4h", "1d")], "Сетап × таймфрейм"
    )

    emit("\n## Стійкість (усі нові дані)")
    emit("| рядок | Δ проти A [95 % CI] | E[R] варіанта [95 % CI] |\n|---|---|---|")
    c2x2 = T["C2_r"] - T["C2_c"]
    emit(f"| витрати ×2 | {fs(summ(c2x2 - (T['A_r'] - T['A_c']), day))} | {fs(summ(c2x2, day))} |")
    for v, lab in (("C2b025", "буфер 0.25 ATR"), ("C2b100", "буфер 1.0 ATR"), ("C2h05", "холд ×0.5"), ("C2h2", "холд ×2")):
        emit(f"| {lab} | {fs(summ(T[f'{v}_r'] - T['A_r'], day))} | {fs(summ(T[f'{v}_r'], day))} |")
    for suf, lab in (("fa", "фандинг: фактичний (плоский до історії)"), ("ff", "фандинг: лише плоский 0.01 %/8 год")):
        c2f = T["C2_r"] - T[f"C2_{suf}"]
        emit(f"| {lab} | {fs(summ(c2f - (T['A_r'] - T[f'A_{suf}']), day))} | {fs(summ(c2f, day))} |")
    emit(f"| вхід тейкером (Δ не змінюється) | {fs(x1)} | {fs(summ(T['C2_r'] - T['te'], day))} |")
    emit(f"| вхід тейкером і витрати ×2 | {fs(summ(c2x2 - (T['A_r'] - T['A_c']), day))} | {fs(summ(c2x2 - 2 * T['te'], day))} |")
    nev = ~np.isin(T["sym"], list(old_all))
    emit(
        f"| лише символи, яких не було в старому всесвіті (n={int(nev.sum())}) | {fs(summ(d[nev], day[nev]))} | {fs(summ(T['C2_r'][nev], day[nev]))} |"
    )
    tot: dict[int, float] = {}
    for a_, b_ in zip(d.tolist(), day.tolist(), strict=True):
        tot[b_] = tot.get(b_, 0.0) + a_
    top = [k for k, _ in sorted(tot.items(), key=lambda kv: -kv[1])[:10]]
    keep = ~np.isin(day, top)
    emit(f"| без 10 найкращих дат (за Δ) | {fs(summ(d[keep], day[keep]))} | {fs(summ(T['C2_r'][keep], day[keep]))} |")
    src = Counter(T["C2_fs"].tolist())
    emit(
        f"джерело фандингу C2: {dict(src)}; середній фандинг, R: C2 факт {T['C2_fa'].mean():+.4f}, плоский {T['C2_ff'].mean():+.4f}; A факт {T['A_fa'].mean():+.4f}, плоский {T['A_ff'].mean():+.4f}"
    )
    per_asset(T, d, IS, OOS)


def per_asset(T: dict[str, np.ndarray], d: np.ndarray, IS: np.ndarray, OOS: np.ndarray) -> None:
    emit("\n## За активами (5a): Δ C2 − A")
    rows, means, ses = [], [], []
    for s in sorted(set(T["sym"].tolist())):
        m = T["sym"] == s
        v = d[m]
        se = float(v.std(ddof=1) / math.sqrt(v.size)) if v.size >= 2 else math.nan
        means.append(float(v.mean()))
        ses.append(se)
        rows.append((s, int(m.sum()), float(v.mean()), se, float(d[m & IS].mean()) if (m & IS).any() else math.nan, int((m & IS).sum())))
    q, df, i2 = cochran_q(np.array(means), np.array(ses))
    emit(f"Cochran Q={q:.1f}, df={df}, p={chi2_sf(q, df):.3f}, I²={i2:.1%} (symbols with n>=2: {df + 1})")
    emit("| символ | n | Δ C2 − A | SE |\n|---|---|---|---|")
    for s, nn, mu, se, _, _ in sorted(rows, key=lambda r: -r[1])[:20]:
        emit(f"| {s} | {nn} | {mu:+.3f} | {se:.3f} |")
    emit("(20 символів з найбільшою кількістю угод; повні дані — у trades/)")
    ok = sorted([r for r in rows if r[5] >= 20], key=lambda r: -r[4])
    top = {r[0] for r in ok[: max(1, len(ok) // 4)]}
    mt = np.isin(T["sym"], list(top))
    a = summ(d[OOS & mt], T["day"][OOS & mt])
    b = summ(d[OOS & ~mt], T["day"][OOS & ~mt])
    emit(
        f"Стійкість: верхня чверть за IS Δ ({len(top)} із {len(ok)} символів з >=20 IS-угод): OOS {fs(a)} n={a['n']}; решта OOS {fs(b)} n={b['n']}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("universe", "fetch", "sanity", "run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=CACHE)
    ap.add_argument("--s1-cache", type=Path, default=S1_CACHE)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    with (a.cache_dir / f"{a.stage}.txt").open("w") as fh:
        OUT["fh"] = fh
        s1.OUT["fh"] = fh  # the step-1 fetcher's progress lines go to the same file
        emit(f"# {a.stage} {datetime.now(UTC).isoformat(timespec='seconds')}")
        if a.stage == "universe":
            stage_universe(a.cache_dir, a.s1_cache, a.data_dir)
        elif a.stage == "fetch":
            stage_fetch(a.cache_dir, a.s1_cache, a.data_dir, a.limit)
        elif a.stage == "sanity":
            stage_sanity(a.cache_dir, a.s1_cache, a.data_dir)
        elif a.stage == "run":
            stage_run(a.cache_dir, a.s1_cache, a.data_dir, a.limit, a.workers)
        else:
            stage_report(a.cache_dir, a.s1_cache)
        emit(f"# done, rss {dsm.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
