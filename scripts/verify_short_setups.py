#!/usr/bin/env python3
"""IDEA-20261024-short-setups-demo, step 1: detectors A (S3-range breakout + retest) and B (failed S3 sweep) on history.

No network: 1h bars and the point-in-time top-30 universe come from the course-step-1 cache (``--s1-cache``), listings,
exchange info and daily volumes from the funding-crowding cache (``--data-dir``), as ``verify_demo_stop_management.py``.
Paths on 1h bars, the live exit B (break-even at 50 % of the way) from ``analytics.stop_management``.

Stages: ``sanity`` (S3 count vs the stop-management generator, units, gaps, timing), ``count`` (signals only, no
outcomes: power and the frequency on the point-in-time top-100), ``run`` (per-symbol trade files into
``--cache-dir``/trades, worker processes), ``report`` (tables; full output also in ``--cache-dir``/report.txt).
Pre-registration: the card's «Попередня реєстрація».
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import zlib
from collections import Counter, defaultdict
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (loaders, universe, period)
import verify_demo_stop_management as dsm  # noqa: E402  (bootstrap summary helpers)

from tradedesk.analytics.course_basics import range_events  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.demo_context_filters import trend_dirs  # noqa: E402
from tradedesk.analytics.liquidation_flush import base_of, holm, monthly_universe  # noqa: E402
from tradedesk.analytics.short_setups import Signal, breakout_retest, failed_sweep, placebo_levels  # noqa: E402
from tradedesk.analytics.short_setups import regimes_at as regimes  # noqa: E402  (tested in tests/test_short_setups.py)
from tradedesk.analytics.stop_management import StopRule, chi2_sf, cochran_q, simulate_stop_rule  # noqa: E402
from tradedesk.execution.orders import reward_risk_at  # noqa: E402  (read-only pure function, the guard's R:R)

SEED = 20261024
DAY_MS = 24 * HOUR_MS
START_MS, OOS_MS = s1.START_MS, s1.OOS_MS
TFS = {"4h": (4, 7 * DAY_MS, s1.END_MS), "1d": (24, 30 * DAY_MS, s1.ms("2026-08-20T00:00"))}
EXIT_B = StopRule("B", be_frac=0.5)
EXIT_A = StopRule("A")
MAX_STOP, MIN_RR = 0.10, 1.5  # execution.guard defaults
N_PLACEBO = 5
# pre-registered base and one-at-a-time sensitivity (A-only keys are skipped for B)
VARS: dict[str, dict[str, float]] = {
    "base": {},
    "wait3": {"max_wait": 3},
    "wait12": {"max_wait": 12},
    "touch0.1": {"touch_atr": 0.1},
    "touch0.5": {"touch_atr": 0.5},
    "buf0.25": {"stop_atr": 0.25},
    "buf1.0": {"stop_atr": 1.0},
    "tgt1.5": {"target_r": 1.5},
    "tgt3": {"target_r": 3.0},
}
A_ONLY = {"touch0.1", "touch0.5", "liq"}
OUT = dsm.OUT
emit, rss_mb = dsm.emit, dsm.rss_mb


# ---------------------------------------------------------------- helpers


def month_idx(t_ms: np.ndarray) -> np.ndarray:
    d = np.asarray(t_ms, dtype="int64").astype("datetime64[ms]")
    y = d.astype("datetime64[Y]").astype(int) + 1970
    m = d.astype("datetime64[M]").astype(int) % 12 + 1
    return y * 100 + m


def mo_int(mo: str) -> int:
    return int(mo[:4]) * 100 + int(mo[5:])


def detect(bb: Bars, pos: np.ndarray, a: np.ndarray, det: str, kw: dict[str, float]) -> list[Signal]:
    if det == "A":
        return breakout_retest(bb, pos, a, **kw)
    return failed_sweep(bb, pos, a, **{k: v for k, v in kw.items() if k != "touch_atr"})


def real(side: str, x: float) -> float:
    return x if side == "long" else -x


def guard_ok(side: str, e: float, st: float, tg: float) -> tuple[bool, float, float]:
    """(passes, stop distance as a fraction of the entry, guard R:R) on real prices of a long-form signal."""
    re_, rs, rt = real(side, e), real(side, st), real(side, tg)
    stop_pct = abs(re_ - rs) / abs(re_)
    rr = reward_risk_at(side, re_, rs, rt)
    return (stop_pct <= MAX_STOP and rr >= MIN_RR), stop_pct, rr


def btc_regime(s1_cache: Path, data_dir: Path) -> tuple[np.ndarray, np.ndarray]:
    d = resample(s1.load_1h(s1_cache, data_dir, "BTCUSDT"), 24)
    return (d.t + d.bar_ms).astype(np.int64), trend_dirs(d)


# ---------------------------------------------------------------- per symbol


def run_symbol(args: tuple[str, str, str, str, bool, list[int], list[int]]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir, outcomes, bclose, btrend = args
    t0 = time.time()
    s1c, dd = Path(s1_cache), Path(data_dir)
    uni = s1.members(s1c)
    member_months = {mo_int(mo) for mo, v in uni.items() if s in v}
    bclose_a, btrend_a = np.asarray(bclose, dtype=np.int64), np.asarray(btrend, dtype=np.int64)
    h1 = s1.load_1h(s1c, dd, s)
    rng = np.random.default_rng([SEED, zlib.crc32(s.encode())])
    rows: list[dict[str, Any]] = []
    sigrec: list[tuple[Any, ...]] = []  # count stage: (det, tf, side, t_in, guard, regime, member30)
    s3check = 0
    why: Counter[str] = Counter()
    for tf, (hours, hold, end) in TFS.items():
        tb = resample(h1, hours)
        if len(tb) < 40:
            continue
        close_all = tb.t + tb.bar_ms
        reg_all = regimes(bclose_a, btrend_a, close_all)
        mem_all = np.isin(month_idx(close_all), list(member_months))
        split_all = close_all >= OOS_MS
        for side in ("long", "short"):
            bb = tb if side == "long" else tb.mirrored()
            path = h1 if side == "long" else h1.mirrored()
            pos = segment_pos(bb.t)
            a = atr(bb, 14, pos)
            if tf == "4h" and side == "long":  # the stop-management generator's S3 4h candidates (both sides counted there)
                for b2 in (tb, tb.mirrored()):
                    p2 = segment_pos(b2.t)
                    a2 = atr(b2, 14, p2)
                    for ev in range_events(b2, p2, a2, w=30, width_atr=6.0):
                        if ev.kind != "sweep" or not np.isfinite(a2[ev.r]) or a2[ev.r] <= 0:
                            continue
                        ti = b2.close_time(ev.r)
                        e, st = float(b2.c[ev.r]), ev.ld - 0.5 * float(a2[ev.r])
                        if START_MS <= ti < end and mem_all[ev.r] and st < e < ev.mid:
                            s3check += 1
            pool_ok = mem_all & (close_all >= START_MS) & (close_all < end) & np.isfinite(a) & (a > 0)
            variants = list(VARS) if tf == "4h" else ["base"]
            for det in ("A", "B"):
                base_sigs: list[Signal] = []
                for var in variants + (["liq"] if det == "A" else []):
                    if det == "B" and var in A_ONLY:
                        continue
                    if var == "liq":
                        sigs = [
                            Signal(x.kind, x.start, x.i, x.level, x.entry, x.stop, x.alt_target, x.alt_target, x.atr)
                            for x in base_sigs
                            if np.isfinite(x.alt_target)
                        ]
                    else:
                        sigs = detect(bb, pos, a, det, VARS[var])
                        if var == "base":
                            base_sigs = sigs
                    modes = ("g", "ng") if var == "base" else (("ng",) if var == "tgt1.5" else ("g",))
                    for mode in modes:
                        busy = -1
                        for sg in sigs:
                            t_in = bb.close_time(sg.i)
                            if not (START_MS <= t_in < end):
                                continue
                            ok, stop_pct, rr = guard_ok(side, sg.entry, sg.stop, sg.target)
                            if var == "base" and mode == "g":
                                sigrec.append((det, tf, side, t_in, ok, int(reg_all[sg.i]), bool(mem_all[sg.i])))
                            if not mem_all[sg.i] or (mode == "g" and not ok):
                                continue
                            key = f"{det}:{tf}:{side}:{var}:{mode}"
                            why[key + ":sig"] += 1
                            if not outcomes:
                                continue
                            if t_in < busy:
                                why[key + ":busy"] += 1
                                continue
                            o = simulate_stop_rule(path, t_in, sg.entry, sg.stop, sg.target, hold, EXIT_B)
                            if o is None:
                                why[key + ":no_path"] += 1
                                continue
                            busy = o.exit_ms
                            row: dict[str, Any] = {
                                "sym": s,
                                "det": det,
                                "tf": tf,
                                "side": side,
                                "var": var,
                                "mode": mode,
                                "entry_ms": t_in,
                                "day": t_in // DAY_MS,
                                "oos": t_in >= OOS_MS,
                                "reg": int(reg_all[sg.i]),
                                "stop_pct": stop_pct,
                                "rr": rr,
                                "r": o.r_net,
                                "c": o.cost_r,
                                "k": o.exit_kind,
                            }
                            if var == "base" and mode == "g":
                                oa = simulate_stop_rule(path, t_in, sg.entry, sg.stop, sg.target, hold, EXIT_A)
                                row["rA"] = oa.r_net if oa else math.nan
                                row["pl"], row["npl"] = (
                                    placebo(
                                        rng, bb, path, a, pool_ok, reg_all, split_all, sg, side, hold, int(reg_all[sg.i]), t_in >= OOS_MS
                                    )
                                    if tf == "4h"
                                    else (math.nan, 0)
                                )
                            rows.append(row)
                            why[key + ":trade"] += 1
    if outcomes:
        (Path(out_dir) / "trades").mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "trades" / f"{s}.json").write_text(json.dumps({"rows": rows, "why": dict(why), "s3check": s3check}))
    gaps = int(np.sum(np.diff(h1.t) > HOUR_MS)) if len(h1) else 0
    return {
        "sym": s,
        "n": len(rows),
        "why": dict(why),
        "sig": sigrec if not outcomes else [],
        "s3check": s3check,
        "sec": time.time() - t0,
        "bars": len(h1),
        "gaps": gaps,
        "rss": rss_mb(),
    }


def placebo(
    rng: np.random.Generator,
    bb: Bars,
    path: Bars,
    a: np.ndarray,
    pool_ok: np.ndarray,
    reg_all: np.ndarray,
    split_all: np.ndarray,
    sg: Signal,
    side: str,
    hold: int,
    reg: int,
    oos: bool,
) -> tuple[float, int]:
    """Mean R net (exit B) of up to N_PLACEBO random entries of the same symbol, side, BTC regime and split, with the
    trade's stop distance in ATR units, target 2R and the guard."""
    cand = np.flatnonzero(pool_ok & (reg_all == reg) & (split_all == oos))
    if cand.size == 0:
        return math.nan, 0
    k = (sg.entry - sg.stop) / sg.atr
    rs: list[float] = []
    for p in rng.permutation(cand)[: 10 * N_PLACEBO]:
        e = float(bb.c[p])
        st, tg = placebo_levels(e, float(a[p]), k, 2.0)
        if not guard_ok(side, e, st, tg)[0]:
            continue
        o = simulate_stop_rule(path, bb.close_time(int(p)), e, st, tg, hold, EXIT_B)
        if o is None:
            continue
        rs.append(o.r_net)
        if len(rs) == N_PLACEBO:
            break
    return (float(np.mean(rs)) if rs else math.nan), len(rs)


def all_syms(s1_cache: Path) -> list[str]:
    return sorted({s for v in s1.members(s1_cache).values() for s in v})


def jobs_for(syms: list[str], a: argparse.Namespace, outcomes: bool, out: Path) -> list[tuple[Any, ...]]:
    bc, bt = btc_regime(a.s1_cache, a.data_dir)
    return [(s, str(a.s1_cache), str(a.data_dir), str(out), outcomes, bc.tolist(), bt.tolist()) for s in syms]


# ---------------------------------------------------------------- stages


def stage_sanity(a: argparse.Namespace) -> None:
    syms = all_syms(a.s1_cache)
    info = s1.rd(a.data_dir / "exchange_info.json")["info"]
    under = Counter((info.get(s) or {}).get("underlyingType") for s in syms)
    emit(f"members {len(syms)}; underlyingType {dict(under)}")
    bc, bt = btc_regime(a.s1_cache, a.data_dir)
    emit(f"BTC daily regime days: {dict(Counter(bt.tolist()))}; first close {datetime.fromtimestamp(bc[0] / 1000, UTC):%Y-%m-%d}")
    t0 = time.time()
    for s in ("BTCUSDT", "SOLUSDT", "DOGEUSDT"):
        r = run_symbol(jobs_for([s], a, True, a.cache_dir / "sanity")[0])
        h1 = s1.load_1h(a.s1_cache, a.data_dir, s)
        ref = json.loads((a.ref_cache / "sanity" / "trades" / f"{s}.json").read_text())["why"].get("S3:4h:cand")
        base = {k: v for k, v in r["why"].items() if ":base:g:" in k}
        emit(
            f"{s}: bars {r['bars']} ({datetime.fromtimestamp(h1.t[0] / 1000, UTC):%Y-%m-%d}..{datetime.fromtimestamp(h1.t[-1] / 1000, UTC):%Y-%m-%d}), "
            f"close {h1.c[-1]:.6g}, holes>1h {r['gaps']}, {r['sec']:.1f}s, rss {r['rss']:.0f} MB; "
            f"S3 4h cand {r['s3check']} vs stop-management {ref}; base guard counts {base}"
        )
    el = time.time() - t0
    emit(
        f"timing: {el:.0f}s for 3 symbols -> estimate {el / 3 * len(syms) / a.workers / 60:.0f} min for {len(syms)} symbols on {a.workers} workers"
    )


def top100(a: argparse.Namespace, months: list[str]) -> dict[int, set[str]]:
    elig = s1.eligible_syms(a.data_dir)
    qv = s1.daily_qv(a.s1_cache, a.data_dir, elig)
    out: dict[int, set[str]] = {}
    for mo in months:
        ranked = monthly_universe(qv, s1.ms(f"{mo}-01T00:00"), top_n=160)
        seen, mem = set(), []
        for s in ranked:
            if base_of(s) in seen:
                continue
            seen.add(base_of(s))
            mem.append(s)
            if len(mem) == 100:
                break
        out[mo_int(mo)] = set(mem)
    return out


def stage_count(a: argparse.Namespace) -> None:
    syms = all_syms(a.s1_cache)
    jobs = jobs_for(syms, a, False, a.cache_dir)
    recs: list[tuple[Any, ...]] = []
    t0 = time.time()
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            recs += [(r["sym"], *x) for x in r["sig"]]
            if k % 50 == 0 or k == len(jobs):
                emit(f"  {k}/{len(jobs)} symbols, {time.time() - t0:.0f}s, worker rss {r['rss']:.0f} MB")
    emit("\n## Потужність (сигнали з guard, топ-30, до правила однієї позиції; без R)")
    emit("| детектор | tf | бік | частина | n сигналів | дат | MDE80 (n) | MDE80 (дати) |")
    emit("|---|---|---|---|---|---|---|---|")
    for det in ("A", "B"):
        for tf in TFS:
            for side in ("short", "long"):
                for part, lo, hi in (("IS", START_MS, OOS_MS), ("OOS", OOS_MS, 1 << 62)):
                    xs = [x for x in recs if x[1] == det and x[2] == tf and x[3] == side and x[5] and x[7] and lo <= x[4] < hi]
                    n, d = len(xs), len({x[4] // DAY_MS for x in xs})
                    mde_n = 2.8 * 1.1 / math.sqrt(n) if n else math.nan
                    mde_d = 2.8 * 1.1 / math.sqrt(d) if d else math.nan
                    emit(f"| {det} | {tf} | {side} | {part} | {n} | {d} | {mde_n:.3f} | {mde_d:.3f} |")
    # frequency on the point-in-time top-100 (4h, guard, base)
    months = [m for m in s1.months("2025-03", "2026-09")]
    uni = top100(a, months)
    have = set(syms)
    cov: dict[int, int] = {}
    for mo in months:
        mi = mo_int(mo)
        cov[mi] = sum(1 for s in uni[mi] if s in have and (a.s1_cache / "k1h" / s / f"{mo}.json").exists())
    emit("\n## Покриття топ-100 1h-кешем (символів із 100)")
    emit(", ".join(f"{mo} {cov[mo_int(mo)]}" for mo in months))
    bc, bt = btc_regime(a.s1_cache, a.data_dir)
    emit("\n## Частота: сигнали 4h з guard на тиждень, point-in-time топ-100 (масштаб на покриття), до правила однієї позиції")
    emit(
        "| детектор | бік | період | усі тижні: сирі / масштаб. | тижні BTC −1: масштаб. | тижні BTC +1: масштаб. | днів −1 / +1 / усього |"
    )
    emit("|---|---|---|---|---|---|---|")
    for det in ("A", "B"):
        for side in ("short", "long"):
            for lab, lo, hi in (("OOS 2025-03…2026-09", OOS_MS, s1.END_MS), ("ост. 12 міс.", s1.END_MS - 365 * DAY_MS, s1.END_MS)):
                days = np.arange(lo // DAY_MS, hi // DAY_MS)
                dreg = regimes(bc, bt, days * DAY_MS + DAY_MS - 1)  # regime known during the day (last close before its end)
                raw, sc = 0, Counter()
                for x in recs:
                    sym, d_, tf, sd, t_in, ok, reg, _ = x
                    if d_ != det or tf != "4h" or sd != side or not ok or not (lo <= t_in < hi):
                        continue
                    mi = int(month_idx(np.array([t_in]))[0])
                    if sym not in uni.get(mi, set()) or cov.get(mi, 0) == 0:
                        continue
                    raw += 1
                    w = 100 / cov[mi]
                    sc["all"] += w
                    sc[reg] += w
                nd, nm, npl = days.size, int((dreg == -1).sum()), int((dreg == 1).sum())
                emit(
                    f"| {det} | {side} | {lab} | {raw / (nd / 7):.1f} / {sc['all'] / (nd / 7):.1f} | "
                    f"{(sc[-1] / (nm / 7)) if nm else math.nan:.1f} | {(sc[1] / (npl / 7)) if npl else math.nan:.1f} | {nm} / {npl} / {nd} |"
                )


def stage_run(a: argparse.Namespace) -> None:
    syms = all_syms(a.s1_cache)[: a.limit or None]
    jobs = jobs_for(syms, a, True, a.cache_dir)
    t0 = time.time()
    tot: Counter[str] = Counter()
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            tot.update(r["why"])
            if k % 20 == 0 or k == len(jobs):
                emit(f"  {k}/{len(jobs)} symbols, {time.time() - t0:.0f}s, last {r['sym']} n={r['n']}, worker rss {r['rss']:.0f} MB")
    emit(f"run done in {time.time() - t0:.0f}s; base guard: " + ", ".join(f"{k} {v}" for k, v in sorted(tot.items()) if ":base:g:" in k))


# ---------------------------------------------------------------- report


def load(cache: Path) -> tuple[dict[str, np.ndarray], int]:
    cols: dict[str, list[Any]] = defaultdict(list)
    s3 = 0
    keys = ("sym", "det", "tf", "side", "var", "mode", "entry_ms", "day", "oos", "reg", "stop_pct", "rr", "r", "c", "k", "rA", "pl", "npl")
    for p in sorted((cache / "trades").glob("*.json")):
        d = json.loads(p.read_text())
        s3 += d["s3check"]
        for r in d["rows"]:
            for k in keys:
                cols[k].append(r.get(k, math.nan))
    return {k: np.asarray(v) for k, v in cols.items()}, s3


def line(lab: str, v: np.ndarray, day: np.ndarray, extra: str = "") -> dict[str, Any]:
    x = dsm.summ(v, day)
    wr = float(np.mean(v > 0)) if v.size else math.nan
    emit(f"| {lab} | {x['n']} | {x['d']} | {dsm.fs(x) if x['n'] >= 2 else 'n/a'} | {x['p']:.4f} | {wr:.1%} |{extra}")
    return x


HDR = "| зріз | n | дат | E[R] net [95 % CI] | p (двобічне) | частка R>0 |\n|---|---|---|---|---|---|"


def stage_report(cache: Path) -> None:
    T, s3 = load(cache)
    emit(f"trades {T['sym'].size}; symbols {np.unique(T['sym']).size}; S3 4h candidates (both sides, top-30): {s3}; rss {rss_mb():.0f} MB")
    IS, OOS = ~T["oos"].astype(bool), T["oos"].astype(bool)

    def m(**kw: Any) -> np.ndarray:
        out = np.ones(T["sym"].size, dtype=bool)
        for k, v in kw.items():
            out &= T[k] == v
        return out

    base4 = m(tf="4h", var="base", mode="g")
    emit("\n## Основне: 4h, база, guard, вихід B (сім'я Holm {A-шорт, B-шорт}, IS)")
    emit(HDR)
    prim: dict[str, dict[str, Any]] = {}
    for det in ("A", "B"):
        for side in ("short", "long"):
            for part, pm in (("IS", IS), ("OOS", OOS), ("усе", IS | OOS)):
                mm = base4 & m(det=det, side=side) & pm
                prim[f"{det}:{side}:{part}"] = line(f"{det} {side} {part}", T["r"][mm], T["day"][mm])
    ps = [prim["A:short:IS"]["p"], prim["B:short:IS"]["p"]]
    adj = holm(ps)
    emit(f"Holm p (IS, шорти): A {adj[0]:.4f}, B {adj[1]:.4f}")

    emit("\n## Плацебо (той самий символ, бік, режим BTC, частина; стоп у тих самих ATR; 2R; вихід B)")
    emit("| зріз | n (з плацебо) | дат | E[R] угод | E[R] плацебо | Δ угода − плацебо [95 % CI] |\n|---|---|---|---|---|---|")
    plc: dict[str, dict[str, Any]] = {}
    for det in ("A", "B"):
        for side in ("short", "long"):
            for part, pm in (("IS", IS), ("OOS", OOS)):
                mm = base4 & m(det=det, side=side) & pm & np.isfinite(T["pl"].astype(float))
                d = T["r"][mm] - T["pl"][mm].astype(float)
                x = dsm.summ(d, T["day"][mm])
                plc[f"{det}:{side}:{part}"] = x
                emit(
                    f"| {det} {side} {part} | {x['n']} | {x['d']} | {T['r'][mm].mean():+.3f} | {T['pl'][mm].astype(float).mean():+.3f} | {dsm.fs(x) if x['n'] >= 2 else 'n/a'} |"
                )

    emit("\n## За режимом денного тренду BTC (4h, база, guard)")
    emit(HDR)
    for det in ("A", "B"):
        for side in ("short", "long"):
            for reg in (-1, 0, 1):
                for part, pm in (("IS", IS), ("OOS", OOS)):
                    mm = base4 & m(det=det, side=side, reg=reg) & pm
                    if mm.sum() >= 2:
                        line(f"{det} {side} BTC {reg:+d} {part}", T["r"][mm], T["day"][mm])

    emit("\n## Чутливість (4h, guard, вихід B; по одному параметру; описово)")
    emit("| варіант | детектор | бік | IS n | IS E[R] [CI] | OOS n | OOS E[R] [CI] |\n|---|---|---|---|---|---|---|")
    for var in [*VARS, "liq"]:
        for det in ("A", "B"):
            if det == "B" and var in A_ONLY:
                continue
            for side in ("short", "long"):
                # a 1.5R target never passes the guard (R:R after fees < 1.5): shown without the guard
                mode = "ng" if var == "tgt1.5" else "g"
                mm = m(tf="4h", var=var, mode=mode, det=det, side=side)
                xi, xo = dsm.summ(T["r"][mm & IS], T["day"][mm & IS]), dsm.summ(T["r"][mm & OOS], T["day"][mm & OOS])
                lab = f"{var} (без guard)" if mode == "ng" else var
                emit(f"| {lab} | {det} | {side} | {xi['n']} | {dsm.fs(xi)} | {xo['n']} | {dsm.fs(xo)} |")

    emit("\n## Стійкість (4h, база)")
    emit("| рядок | детектор | бік | IS [CI] | OOS [CI] |\n|---|---|---|---|---|")
    for det in ("A", "B"):
        for side in ("short", "long"):
            b0 = base4 & m(det=det, side=side)
            rows: list[tuple[str, np.ndarray, np.ndarray]] = [
                ("вихід A (стоп не рухається)", T["rA"].astype(float), b0),
                ("витрати ×2", T["r"] - T["c"], b0),
                ("без guard", T["r"], m(tf="4h", var="base", mode="ng", det=det, side=side)),
                ("1d, guard", T["r"], m(tf="1d", var="base", mode="g", det=det, side=side)),
            ]
            for lab, v, mm in rows:
                xi, xo = dsm.summ(v[mm & IS], T["day"][mm & IS]), dsm.summ(v[mm & OOS], T["day"][mm & OOS])
                emit(f"| {lab} | {det} | {side} | {dsm.fs(xi)} n={xi['n']} | {dsm.fs(xo)} n={xo['n']} |")
            for part, pm in (("IS", IS), ("OOS", OOS)):
                mm = b0 & pm
                v, dd = T["r"][mm], T["day"][mm]
                tot: dict[int, float] = defaultdict(float)
                for x, y in zip(v.tolist(), dd.tolist(), strict=True):
                    tot[y] += x
                top = [k for k, _ in sorted(tot.items(), key=lambda kv: -kv[1])[:10]]
                keep = ~np.isin(dd, top)
                x = dsm.summ(v[keep], dd[keep])
                emit(f"| без 10 найкращих дат ({part}) | {det} | {side} | {dsm.fs(x)} n={x['n']} | |")

    emit("\n## За роками (4h, база, guard, E[R] net)")
    years = (T["entry_ms"].astype("int64")).astype("datetime64[ms]").astype("datetime64[Y]").astype(int) + 1970
    emit("| рік | " + " | ".join(f"{d} {s}" for d in ("A", "B") for s in ("short", "long")) + " |\n|---|---|---|---|---|")
    for y in sorted(set(years[base4].tolist())):
        cells = []
        for det in ("A", "B"):
            for side in ("short", "long"):
                mm = base4 & m(det=det, side=side) & (years == y)
                cells.append(f"{T['r'][mm].mean():+.3f} (n={int(mm.sum())})" if mm.any() else "—")
        emit(f"| {y} | " + " | ".join(cells) + " |")

    emit("\n## Розподіл виходів і стопів (4h, база, guard)")
    for det in ("A", "B"):
        for side in ("short", "long"):
            mm = base4 & m(det=det, side=side)
            k = Counter(T["k"][mm].tolist())
            emit(
                f"{det} {side}: n={int(mm.sum())}, стоп медіана {np.median(T['stop_pct'][mm]):.2%}, p90 {np.quantile(T['stop_pct'][mm], 0.9):.2%}; "
                f"виходи {dict(k.most_common())}"
            )
            per_bar = np.array(list(Counter(T["entry_ms"][mm].tolist()).values()))
            per_day = np.array(list(Counter(T["day"][mm].tolist()).values()))
            emit(
                f"  входів на 4h-бар: медіана {np.median(per_bar):.0f}, p95 {np.quantile(per_bar, 0.95):.0f}, макс {per_bar.max()}; "
                f"на день: медіана {np.median(per_day):.0f}, p95 {np.quantile(per_day, 0.95):.0f}, макс {per_day.max()}"
            )

    for det in ("A", "B"):
        per_asset(T, base4 & m(det=det, side="short"), IS, OOS, f"{det} short")

    emit("\n## Автоматичні вердикти за попередньою реєстрацією (шорти, 4h, база)")
    for k, det in enumerate(("A", "B")):
        i, o, pl = prim[f"{det}:short:IS"], prim[f"{det}:short:OOS"], plc[f"{det}:short:IS"]
        conf = i["m"] >= 0.05 and i["lo"] > 0 and adj[k] < 0.05 and o["m"] > 0 and pl["m"] >= 0.05 and pl["lo"] > 0
        rej = i["hi"] < 0.05 or (o["m"] * i["m"] < 0 and not i["lo"] > 0)
        verdict = "confirmed" if conf else ("rejected" if rej else "inconclusive")
        emit(
            f"{det}: IS {dsm.fs(i)} n={i['n']}; OOS {dsm.fs(o)} n={o['n']}; Holm p {adj[k]:.4f}; Δ плацебо IS {dsm.fs(pl)} -> {verdict}; "
            f"умова кроку 3 (IS E[R] ≥ −0.05R): {i['m'] >= -0.05}; лонги IS {prim[f'{det}:long:IS']['m']:+.3f} ({prim[f'{det}:long:IS']['m'] >= -0.05})"
        )


def per_asset(T: dict[str, np.ndarray], base: np.ndarray, IS: np.ndarray, OOS: np.ndarray, title: str) -> None:
    emit(f"\n## За активами (5a): {title}, 4h, база")
    syms = sorted(set(T["sym"][base].tolist()))
    means, ses, rows = [], [], []
    for s in syms:
        mm = base & (T["sym"] == s)
        v = T["r"][mm]
        if v.size >= 10:  # with n = 2 .. 9 the SE is unreliable (two equal -1R stops give SE 0) and Q explodes
            means.append(float(v.mean()))
            ses.append(float(v.std(ddof=1) / math.sqrt(v.size)))
        rows.append(
            (s, int(mm.sum()), float(v.mean()), float(T["r"][mm & IS].mean()) if (mm & IS).any() else math.nan, int((mm & IS).sum()))
        )
    ok = [(mu, se) for mu, se in zip(means, ses, strict=True) if se > 0]
    q, df, i2 = cochran_q(np.array([x[0] for x in ok]), np.array([x[1] for x in ok]))
    emit(f"Cochran Q={q:.1f}, df={df}, p={chi2_sf(q, df):.3f}, I²={i2:.1%} (symbols with n >= 10: {df + 1})")
    emit("| символ | n | E[R] |\n|---|---|---|")
    for s, nn, mu, _, _ in sorted(rows, key=lambda r: -r[1])[:12]:
        emit(f"| {s} | {nn} | {mu:+.3f} |")
    cand = sorted([r for r in rows if r[4] >= 10], key=lambda r: -r[3])
    top = {r[0] for r in cand[: max(1, len(cand) // 4)]}
    mt = np.isin(T["sym"], list(top))
    a = dsm.summ(T["r"][base & OOS & mt], T["day"][base & OOS & mt])
    b = dsm.summ(T["r"][base & OOS & ~mt], T["day"][base & OOS & ~mt])
    emit(
        f"Стійкість: верхня чверть за IS ({len(top)} із {len(cand)} символів з ≥10 IS-угод): OOS {dsm.fs(a)} n={a['n']}; решта OOS {dsm.fs(b)} n={b['n']}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("sanity", "count", "run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/short_setups")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--ref-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/demo_stop_management")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    with (a.cache_dir / f"{a.stage}.txt").open("w") as fh:
        OUT["fh"] = fh
        emit(f"# {a.stage} {datetime.now(UTC).isoformat(timespec='seconds')}")
        if a.stage == "sanity":
            stage_sanity(a)
        elif a.stage == "count":
            stage_count(a)
        elif a.stage == "run":
            stage_run(a)
        else:
            stage_report(a.cache_dir)
        emit(f"# done, rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
