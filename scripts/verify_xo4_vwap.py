#!/usr/bin/env python3
"""XO-4, developed weekly VWAP as a pivot (IDEA-20261113-x-traders, item 5; LIB-20261113-x-trader_xo «4б» XO-4).
Registration: the card's subsection «5.3 XO-4: … попередня реєстрація» (committed before any outcome is computed).

    uv run python scripts/verify_xo4_vwap.py --stage zero     # stage 0: counts and candle availability, no outcome metric
    uv run python scripts/verify_xo4_vwap.py --smoke          # synthetic bars through run + report (counts on screen only)
    uv run python scripts/verify_xo4_vwap.py --stage run      # per-symbol trade rows + placebo (outcomes)
    uv run python scripts/verify_xo4_vwap.py --stage report   # tables (also <cache>/report.txt)

Data: the passport's Binance USD-M 1h bars with quote volume (``verify_setup_passport.load_1h_qv``; no network), the
daily quote volume for the demo list (previous UTC day: top-100 by turnover and >= $5M) and the funding settlements of
the funding-crowding cache (``sieve.load_funding``).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import tempfile
import time
import zlib
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_setup_passport as vsp  # noqa: E402  (1h loaders, universe, daily turnover)

from tradedesk.analytics import per_asset as pa  # noqa: E402
from tradedesk.analytics import xo4_vwap as x4  # noqa: E402
from tradedesk.analytics.course_s14_ema import Bars  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.setup_passport import mean_ci  # noqa: E402
from tradedesk.analytics.stop_management import SLIPPAGE, TAKER, Fees  # noqa: E402

H, D = x4.HOUR_MS, x4.DAY_MS
SEED = 20261123
START_MS = vsp.s1.ms("2021-01-01T00:00")
OOS_MS = vsp.s1.ms("2025-03-01T00:00")
END_MS = vsp.s1.ms("2026-09-16T00:00")  # entries before; the 1h cache ends 2026-09-23, a 168 h hold must fit
TOP_N, FLOOR = 100, 5e6  # demo list: guard.universe(size=100, min_turnover_usd=5e6), previous UTC day
FEES = Fees(maker=TAKER + SLIPPAGE, taker_exit=TAKER + SLIPPAGE)  # every side a taker order: 0.15 % per round trip
STRESS_MULT = {"r15": 1.5, "r20": 2.0}
N_PERM = 200
PLACEBO_HALF = 15 * 24  # placebo starts within +-15 days of the real one (1h bars)
PLACEBO_CHUNK = 15_000  # placebo trades per vectorised call (168 h hold: ~20 MB per float matrix)
INTEREST = 0.05  # R: the smallest net edge per trade worth trading (registered)
Z_MDE = 1.960 + 0.842  # one-sided alpha 0.025 (Holm's strictest step of 2) and 80 % power
CALLS = ROOT / "docs" / "calls" / "x" / "Trader_XO.csv"
YEARS = tuple(range(2021, 2027))

# name -> period, level ("vwap" | "open"), trigger tf hours, hold hours, primary
VARIANTS: dict[str, tuple[str, str, int, int, bool]] = {
    "W4": ("week", "vwap", 4, 168, True),
    "W1": ("week", "vwap", 1, 168, False),
    "M4": ("month", "vwap", 4, 168, False),
    "WO4": ("week", "open", 4, 168, False),
    "W4h72": ("week", "vwap", 4, 72, False),
}
PRIMARY = "W4"
HYP = {-1: "H1 loss (short)", 1: "H2 back above (long)"}
emit = vsp.emit


def week(t: np.ndarray) -> np.ndarray:
    """ISO week index (Monday start) of open ms."""
    return (np.asarray(t, dtype=np.int64) // D + 3) // 7


def load_funding(data_dir: Path, s: str) -> tuple[np.ndarray, np.ndarray]:
    d = data_dir / "funding" / s
    rows: list[list[float]] = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            rows.extend(vsp.s1.rd(p) or [])
    if not rows:
        return np.zeros(0, dtype=np.int64), np.zeros(0)
    a = np.array(rows, dtype=float)
    a = a[np.argsort(a[:, 0], kind="stable")]
    return a[:, 0].astype(np.int64), a[:, 2]


# ---------------------------------------------------------------- per symbol


def levels(b: Bars, qv: np.ndarray, period: str, kind: str) -> x4.Levels:
    return x4.developing_vwap(b.t, b.h, b.low, b.c, qv, period) if kind == "vwap" else x4.period_open(b.t, b.o, period)


def symbol_signals(b: Bars, qv: np.ndarray, names: tuple[str, ...]) -> dict[str, list[x4.Signal]]:
    atr = x4.atr_sma(b.h, b.low, b.c, 14)
    cache: dict[tuple[str, str], x4.Levels] = {}
    out: dict[str, list[x4.Signal]] = {}
    for name in names:
        period, kind, tf, _, _ = VARIANTS[name]
        if (period, kind) not in cache:
            cache[(period, kind)] = levels(b, qv, period, kind)
        sig = x4.find_signals(b.t, b.h, b.low, b.c, atr, cache[(period, kind)], tf_hours=tf)
        out[name] = [s for s in sig if START_MS <= s.t_in < END_MS]
    return out


def with_path(b: Bars, sig: list[x4.Signal], n_hold: int) -> np.ndarray:
    """Flags: the hold path starts at the bar after the trigger (opening at t_in) and is hole-free (times only)."""
    valid = set(x4.valid_starts(b.t, n_hold).tolist())
    return np.array([(x.i + 1 in valid) and int(b.t[x.i + 1]) == x.t_in for x in sig], dtype=bool) if sig else np.zeros(0, dtype=bool)


def process(s: str, b: Bars, qv: np.ndarray, f_t: np.ndarray, f_r: np.ndarray, out: Path) -> dict[str, Any]:
    """Trade rows of every variant and the placebo matrices of the primary for one symbol (outcomes)."""
    sigs = symbol_signals(b, qv, tuple(VARIANTS))
    res: dict[str, Any] = {}
    rng = np.random.default_rng(SEED + zlib.crc32(s.encode()))
    for name, sig0 in sigs.items():
        _, _, tf, n_hold, primary = VARIANTS[name]
        ok = with_path(b, sig0, n_hold)
        sig = [x for x, f in zip(sig0, ok, strict=True) if f]
        drop = [x for x, f in zip(sig0, ok, strict=True) if not f]  # no full hold path (delisting, gaps): counted only
        i0 = np.array([x.i + 1 for x in sig], dtype=np.int64)
        side = np.array([x.side for x in sig], dtype=np.int64)
        e = np.array([x.entry for x in sig])
        st = np.array([x.stop for x in sig])
        lv = np.array([x.level for x in sig])
        r, ex, kind = x4.vwap_exit_outcomes(b.t, b.o, b.h, b.low, b.c, i0, side, e, st, lv, n_hold, FEES, tf)
        t_in = np.array([x.t_in for x in sig], dtype=np.int64)
        t_ex = b.t[ex] + H if ex.size else np.zeros(0, dtype=np.int64)
        fr = x4.funding_r(t_in, t_ex, side, e, x4.risk_unit(e, st, FEES), f_t, f_r)
        row: dict[str, Any] = {
            "t_in": t_in.tolist(),
            "side": side.tolist(),
            "r": np.round(r + fr, 6).tolist(),
            "r_nf": np.round(r, 6).tolist(),
            "kind": kind.tolist(),
            "hold_h": ((t_ex - t_in) // H).tolist(),
            "free": [bool(v) for v in _free_by_side(t_in, t_ex, side)],
            "opp": [bool(v) for v in x4.overlaps_opposite(t_in, t_ex, side)],
            "stop_pct": np.round(np.abs(e - st) / np.where(e > 0, e, 1), 6).tolist(),
            # per trade (review 2026-11-23, item 2a): first settlement <= entry and last >= exit - 8 h
            "fund_known": [bool(v) for v in x4.funding_covered(t_in, t_ex, f_t)],
            "drop_t_in": [x.t_in for x in drop],
            "drop_side": [x.side for x in drop],
        }
        for k, mult in STRESS_MULT.items():
            rs = x4.vwap_exit_outcomes(b.t, b.o, b.h, b.low, b.c, i0, side, e, st, lv, n_hold, FEES.scaled(mult), tf)[0]
            row[k] = np.round(rs + fr, 6).tolist()
        res[name] = row
        if primary and i0.size:
            valid = x4.boundary_starts(b.t, n_hold, tf)
            ps = _placebo_starts(valid, i0, rng)
            dst, dlv = (st - e) / e, (lv - e) / e
            pr = np.full(ps.shape, np.nan, dtype=np.float32)
            okp = ps >= 0
            j = ps[okp]
            sd = np.broadcast_to(side[:, None], ps.shape)[okp]
            pe = b.o[j]
            pst = pe * (1 + np.broadcast_to(dst[:, None], ps.shape)[okp])
            plv = pe * (1 + np.broadcast_to(dlv[:, None], ps.shape)[okp])
            vals = np.empty(j.size)
            for c0 in range(0, j.size, PLACEBO_CHUNK):  # bounded memory: chunk x hold bars per matrix
                sl = slice(c0, c0 + PLACEBO_CHUNK)
                vals[sl] = x4.vwap_exit_outcomes(b.t, b.o, b.h, b.low, b.c, j[sl], sd[sl], pe[sl], pst[sl], plv[sl], n_hold, FEES, tf)[0]
            pr[okp] = vals
            (out / "placebo").mkdir(parents=True, exist_ok=True)
            np.save(out / "placebo" / f"{s}.{name}.npy", pr)
    (out / "rows").mkdir(parents=True, exist_ok=True)
    (out / "rows" / f"{s}.json").write_text(json.dumps(res, separators=(",", ":")))
    return {"sym": s, "n": sum(len(v["r"]) for v in res.values()), "rss": vsp.rss_mb()}


def _free_by_side(t_in: np.ndarray, t_ex: np.ndarray, side: np.ndarray) -> np.ndarray:
    """One position per symbol and side at a time (greedy in entry order)."""
    keep = np.zeros(t_in.size, dtype=bool)
    for sd in (1, -1):
        idx = np.flatnonzero(side == sd)
        free = -1
        for j in idx[np.argsort(t_in[idx], kind="stable")]:
            if t_in[j] >= free:
                keep[j] = True
                free = int(t_ex[j])
    return keep


def _placebo_starts(valid: np.ndarray, i0: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    lo = np.searchsorted(valid, i0 - PLACEBO_HALF)
    hi = np.searchsorted(valid, i0 + PLACEBO_HALF, side="right")
    cnt = hi - lo
    u = rng.random((i0.size, N_PERM))
    pos = lo[:, None] + np.floor(u * np.maximum(cnt, 1)[:, None]).astype(np.int64)
    pos = np.minimum(pos, np.maximum(hi - 1, 0)[:, None])
    res = valid[np.clip(pos, 0, max(valid.size - 1, 0))] if valid.size else np.full(pos.shape, -1)
    return np.where((cnt > 0)[:, None], res, -1).astype(np.int64)


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out = args
    b, qv = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    f_t, f_r = load_funding(Path(data_dir), s)
    return process(s, b, qv, f_t, f_r, Path(out))


def zero_symbol(args: tuple[str, str, str]) -> dict[str, Any]:
    """Stage 0: signals (bars up to each trigger close only), their geometry, the hold-path flag (bar times only) and
    whether funding history covers the entry. No price after an entry is read."""
    s, s1_cache, data_dir = args
    b, qv = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    f_t, _ = load_funding(Path(data_dir), s)
    sigs = symbol_signals(b, qv, tuple(VARIANTS))
    u, cnt = np.unique(b.t // D, return_counts=True)
    res: dict[str, Any] = {
        "sym": s,
        "first": int(b.t[0]) if len(b.t) else 0,
        "last": int(b.t[-1]) if len(b.t) else 0,
        "rss": vsp.rss_mb(),
        "days20": set((u[cnt >= 20] * D).tolist()),  # UTC days with >= 20 cached 1h bars (coverage of the demo list)
    }
    for name, sig in sigs.items():
        hold = VARIANTS[name][3]
        ok = with_path(b, sig, hold)
        t_in = np.array([x.t_in for x in sig], dtype=np.int64)
        # funding history covers the longest possible hold (times only: the exit is not known at stage 0)
        fk = x4.funding_covered(t_in, t_in + hold * H, f_t)
        res[name] = [
            (
                x.t_in,
                x.side,
                round(abs(x.entry - x.stop) / x.entry, 6),
                round(abs(x.level - x.entry) / x.entry, 6),
                bool(f),
                bool(k),
            )
            for x, f, k in zip(sig, ok, fk, strict=True)
        ]
    return res


# ---------------------------------------------------------------- universe


def demo_days(qv: dict[str, list[tuple[int, float]]]) -> dict[int, set[str]]:
    """UTC day -> the demo list built from that day's turnover (top-100 and >= $5M); used for the next day."""
    by_day: dict[int, list[tuple[float, str]]] = defaultdict(list)
    for s, rows in qv.items():
        for t, v in rows:
            by_day[int(t)].append((v, s))
    return {t: {s for v, s in sorted(xs, reverse=True)[:TOP_N] if v >= FLOOR} for t, xs in by_day.items()}


def in_demo(sym: str, t_in: np.ndarray, days: dict[int, set[str]]) -> np.ndarray:
    """Entry t_in is a close; the decision day is the UTC day of the trigger bar (t_in - 1 ms), the list is built on the day before."""
    prev = ((t_in - 1) // D) * D - D
    return np.array([sym in days.get(int(d), ()) for d in prev], dtype=bool)


def trader_syms(have: set[str]) -> list[str]:
    out: set[str] = set()
    with CALLS.open() as fh:
        for row in csv.DictReader(line for line in fh if not line.startswith("#")):
            a = (row.get("asset") or "").strip().upper()
            if a and f"{a}USDT" in have:
                out.add(f"{a}USDT")
    return sorted(out)


# ---------------------------------------------------------------- stage 0


def stage_zero(a: argparse.Namespace) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    all_syms = vsp.s1.rd(a.data_dir / "symbols.json")
    elig = vsp.s1.eligible_syms(a.data_dir)
    emit(f"[zero] universe: {len(all_syms)} Binance USD-M symbols, eligible (TradFi / non-USDT out) {len(elig)}, with 1h bars {len(syms)}")
    days = demo_days(vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig))  # the real top-100: ranked over every eligible symbol
    tsy = trader_syms(set(syms))
    emit(f"[zero] trader's assets with bars: {len(tsy)}")
    t0 = time.time()
    agg: dict[str, list[tuple[str, tuple[Any, ...]]]] = {n: [] for n in VARIANTS}
    last: dict[str, int] = {}
    days20: dict[str, set[int]] = {}
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for j, r in enumerate(pool.imap_unordered(zero_symbol, [(s, str(a.s1_cache), str(a.data_dir)) for s in syms]), 1):
            last[r["sym"]] = r["last"]
            days20[r["sym"]] = r["days20"]
            for n in VARIANTS:
                agg[n].extend((r["sym"], x) for x in r[n])
            if j % 50 == 0 or j == len(syms):
                emit(f"  [zero] {j}/{len(syms)} {time.time() - t0:.0f}s; worker rss {r['rss']:.0f} MB; main {vsp.rss_mb():.0f} MB")
    gone = sum(1 for v in last.values() if v < vsp.s1.ms("2026-09-01T00:00"))
    emit(f"[zero] symbols whose bars end before 2026-09 (delisted, kept: no survivorship filter): {gone}")
    demo_coverage(days, days20)
    for n, (period, kind, tf, hold, primary) in VARIANTS.items():
        rows = agg[n]
        t_in = np.array([x[1][0] for x in rows], dtype=np.int64)
        dm = in_demo_rows(rows, t_in, days)
        vh = np.array([x[1][4] for x in rows], dtype=bool)
        m = dm & vh
        side = np.array([x[1][1] for x in rows])
        emit(
            f"\n[zero {n}] {period} {kind}, trigger {tf}h close, hold <= {hold}h, {'PRIMARY' if primary else 'variant (exploratory)'}: "
            f"signals all eligible {len(rows)}; demo list {int(dm.sum())}; with a full hold path {int(m.sum())} "
            f"(shorts {int((m & (side == -1)).sum())}, longs {int((m & (side == 1)).sum())}); "
            f"dropped for no full hold path: shorts {int((dm & ~vh & (side == -1)).sum())}, longs {int((dm & ~vh & (side == 1)).sum())}"
        )
        if not primary:
            continue
        sp_ = np.array([x[1][2] for x in rows])
        dl = np.array([x[1][3] for x in rows])
        fk = np.array([x[1][5] for x in rows], dtype=bool)
        syms_m = {x[0] for x, f in zip(rows, m, strict=True) if f}
        for sd in (-1, 1):
            ms_ = m & (side == sd)
            nw = np.unique(week(t_in[ms_])).size
            nsw = len({(x[0], int(w)) for x, w, f in zip(rows, week(t_in), ms_, strict=True) if f})
            yrs = Counter(time.gmtime(int(t) / 1000).tm_year for t in t_in[ms_])
            emit(
                f"  {HYP[sd]}: n {int(ms_.sum())}; IS (< 2025-03) {int((ms_ & (t_in < OOS_MS)).sum())}, OOS {int((ms_ & (t_in >= OOS_MS)).sum())}; "
                f"weeks {nw}; symbol-weeks {nsw}; funding history covers entry .. entry + {hold}h {int((ms_ & fk).sum())}"
            )
            emit("    by year: " + ", ".join(f"{y} {yrs.get(y, 0)}" for y in YEARS))
            if ms_.any():
                for sdv in (0.8, 1.0, 1.2):
                    emit(
                        f"    a-priori MDE80 (sd {sdv}R): iid {Z_MDE * sdv / math.sqrt(ms_.sum()):.4f}R; "
                        f"one per symbol-week {Z_MDE * sdv / math.sqrt(nsw):.4f}R; one per week {Z_MDE * sdv / math.sqrt(max(nw, 1)):.4f}R"
                    )
        emit(
            f"  symbols {len(syms_m)}; signals per symbol-week (both sides) {m.sum() / max(1, len({(x[0], int(w)) for x, w, f in zip(rows, week(t_in), m, strict=True) if f})):.2f}"
        )
        if m.any():
            q = np.quantile(sp_[m], [0.1, 0.5, 0.9])
            ql = np.quantile(dl[m], [0.1, 0.5, 0.9])
            emit(
                f"  stop distance p10 / p50 / p90: {q[0]:.4f} / {q[1]:.4f} / {q[2]:.4f}; entry-to-level distance: {ql[0]:.4f} / {ql[1]:.4f} / {ql[2]:.4f}"
            )
            cost = 2 * (TAKER + SLIPPAGE) / np.maximum(sp_[m], 1e-9)
            emit(f"  cost per round trip in R (0.15 % / stop distance): median {np.median(cost):.3f}R")
        tm = np.array([x[0] in tsy for x in rows], dtype=bool) & vh
        emit(f"  trader's assets (no demo-list filter): {int(tm.sum())} signals")
    known_level_check(a)


def in_demo_rows(rows: list[tuple[str, tuple[Any, ...]]], t_in: np.ndarray, days: dict[int, set[str]]) -> np.ndarray:
    prev = ((t_in - 1) // D) * D - D
    return np.array([x[0] in days.get(int(d), ()) for x, d in zip(rows, prev, strict=True)], dtype=bool)


def demo_coverage(days: dict[int, set[str]], days20: dict[str, set[int]]) -> None:
    """Share of the real demo list (top-100 by the previous UTC day's turnover over every eligible symbol, >= $5M) whose
    trading day has >= 20 cached 1h bars, by year; the rest cannot trade in this test (review 2026-11-23, item 6)."""
    cnt: Counter[str] = Counter()
    for d, ss in days.items():
        td = d + D  # the list built on day d is the demo list of the next day
        if not (START_MS <= td < END_MS):
            continue
        y = str(time.gmtime(td / 1000).tm_year)
        for s in ss:
            for k in (y, "all"):
                cnt[f"{k}:n"] += 1
                cnt[f"{k}:ok"] += int(td in days20.get(s, ()))
                cnt[f"{k}:nocache"] += int(s not in days20)
    keys = [str(y) for y in YEARS] + ["all"]
    emit(
        "[zero] demo-list coin-days covered by the 1h cache (>= 20 bars on the trading day): "
        + "; ".join(f"{k} {cnt[f'{k}:ok'] / max(1, cnt[f'{k}:n']):.1%} of {cnt[f'{k}:n']}" for k in keys)
    )
    emit(
        "[zero] of which the coin has no 1h cache at all: "
        + "; ".join(f"{k} {cnt[f'{k}:nocache'] / max(1, cnt[f'{k}:n']):.1%}" for k in keys)
    )


def known_level_check(a: argparse.Namespace) -> None:
    """Levels only, against the author's words: BTC below the weekly VWAP and Monday's high on 2026-05-07 14:35 UTC
    (tweet 2052396842323710453); below the monthly VWAP on 2026-06-17 19:43 UTC (tweet 2067332441488994557)."""
    b, qv = vsp.load_1h_qv(a.s1_cache, a.data_dir, "BTCUSDT")
    for when, period in (("2026-05-07T13:00", "week"), ("2026-06-17T18:00", "month")):
        lv = x4.developing_vwap(b.t, b.h, b.low, b.c, qv, period)
        k = int(np.searchsorted(b.t, vsp.s1.ms(when)))
        mon = (b.t >= lv.start[k]) & (b.t < lv.start[k] + D)
        emit(
            f"[known] BTCUSDT bar {when} close {b.c[k]:.0f}; developing {period} VWAP {lv.level[k]:.0f} (since {vsp.iso(int(lv.start[k]))}); "
            f"period's first-day high {b.h[mon].max():.0f}"
        )


# ---------------------------------------------------------------- run / report


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "rows" / f"{s}.json").exists()]
    emit(f"[run] {len(jobs)} of {len(syms)} symbols to do, {a.workers} workers")
    t0, n = time.time(), 0
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for j, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            n += r["n"]
            if j % 25 == 0 or j == len(jobs):
                el = time.time() - t0
                emit(
                    f"  [run] {j}/{len(jobs)} {el:.0f}s eta {el / j * (len(jobs) - j):.0f}s; rows {n}; worker rss {r['rss']:.0f} MB; main {vsp.rss_mb():.0f} MB"
                )
    emit(f"[run] done in {time.time() - t0:.0f}s; rows {n}")


def month(t: np.ndarray) -> np.ndarray:
    """Calendar-month index (UTC) of open ms: the second, coarser cluster (review 2026-11-23, item 4)."""
    return np.asarray(t, dtype=np.int64).astype("datetime64[ms]").astype("datetime64[M]").astype(np.int64)


def load(out: Path, name: str, days: dict[int, set[str]]) -> dict[str, np.ndarray]:
    """Row-aligned trade columns of variant ``name``; ``drop_side`` / ``drop_demo`` (not row-aligned) are the signals
    dropped for lacking a full hold path."""
    cols: dict[str, list[Any]] = defaultdict(list)
    for p in sorted((out / "rows").glob("*.json")):
        s = p.stem
        r = json.loads(p.read_text()).get(name)
        if not r:
            continue
        dt = np.array(r.get("drop_t_in", []), dtype=np.int64)
        cols["drop_side"].extend(r.get("drop_side", []))
        cols["drop_demo"].extend(in_demo(s, dt, days).tolist())
        if not r["r"]:
            continue
        t = np.array(r["t_in"], dtype=np.int64)
        cols["sym"].extend([s] * t.size)
        cols["demo"].extend(in_demo(s, t, days).tolist())
        for k in ("t_in", "side", "r", "r_nf", "r15", "r20", "kind", "hold_h", "free", "opp", "fund_known", "stop_pct"):
            cols[k].extend(r[k])
    T = {k: np.array(v) for k, v in cols.items()}
    if "t_in" in T:
        T["wk"] = week(T["t_in"])
        T["mo"] = month(T["t_in"])
        T["year"] = np.array([time.gmtime(int(t) / 1000).tm_year for t in T["t_in"]])
    else:
        T = {}
    return T


def fmt(r: dict[str, float]) -> str:
    return f"n {int(r['n'])} cl {int(r['k'])} E[R] {r['m']:+.4f} [{r['lo']:+.4f}; {r['hi']:+.4f}] p2 {r['p']:.4f}"


NAN_CI = {"n": 0, "k": 0, "m": math.nan, "lo": math.nan, "hi": math.nan, "p": math.nan, "se": math.nan}


def line(T: dict[str, np.ndarray], m: np.ndarray, label: str, key: str = "r", cl: str = "wk") -> dict[str, float]:
    """Mean and 95 % CI by a cluster bootstrap over ``cl`` (``wk`` ISO week of entry, ``mo`` calendar month)."""
    r = mean_ci(T[key][m], T[cl][m], seed=SEED) if m.any() else dict(NAN_CI)
    emit(f"  {label:<52} {fmt(r)}")
    return r


def placebo(out: Path, name: str, T: dict[str, np.ndarray], m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Placebo of the trades of mask ``m`` (rows in the order of ``load``): the per-trade paired difference real R minus
    the mean of its placebo results, both without funding (row-aligned, nan outside ``m`` or without a placebo), and
    per set the mean placebo R over those trades (descriptive only)."""
    d = np.full(m.size, np.nan)
    tot, cnt = np.zeros(N_PERM), np.zeros(N_PERM)
    pos = 0
    for s in dict.fromkeys(T["sym"].tolist()):
        n = int(np.sum(T["sym"] == s))
        sl = slice(pos, pos + n)
        mm = m[sl]
        pos += n
        if not mm.any():
            continue
        pr = np.load(out / "placebo" / f"{s}.{name}.npy")[mm].astype(float)
        d[sl][mm] = x4.paired_placebo_diff(T["r_nf"][sl][mm], pr)
        tot += np.nansum(pr, axis=0)
        cnt += np.sum(np.isfinite(pr), axis=0)
    return d, tot / np.maximum(cnt, 1)


def one_sided(r: dict[str, float]) -> float:
    if not np.isfinite(r["p"]):
        return math.nan
    return r["p"] / 2 if r["m"] > 0 else 1 - r["p"] / 2


def per_asset(T: dict[str, np.ndarray], m: np.ndarray, is_: np.ndarray) -> None:
    v, sy, wk = T["r"][m], T["sym"][m], T["wk"][m]
    q = pa.cluster_robust_q(v, sy, wk, min_n=30)
    emit(f"    5a heterogeneity (assets with n >= 30): k {q['k']}, Q {q['q']:.1f}, df {q['df']}, p {q['p']}, I2 {q['i2']:.3f}")
    st = sorted(pa.asset_stats(v, list(sy), min_n=30), key=lambda x: -x.n)[:12]
    emit("    5a largest assets: " + "; ".join(f"{x.asset.removesuffix('USDT')} n {x.n} {x.mean:+.3f}" for x in st))
    per = pa.persistence(v, sy, wk, is_[m], top_n=10, seed=SEED)
    emit(
        f"    5a persistence top-10 IS -> OOS: top {per['top_oos_mean']:+.4f} (n {per['n_top_oos']}) vs rest {per['rest_oos_mean']:+.4f}; "
        f"diff {per['diff']:+.4f} CI {per['diff_ci']} p {per['diff_p']}; rho {per['rho']} (n {per['rho_n']})"
    )


def stage_report(out: Path, days: dict[int, set[str]], tsy: list[str]) -> None:
    prim: dict[int, dict[str, Any]] = {}
    for name, (period, kind, tf, hold, primary) in VARIANTS.items():
        T = load(out, name, days)
        if not T:
            emit(f"\n[{name}] no trades")
            continue
        emit(f"\n[{name}] {period} {kind}, trigger {tf}h close, hold <= {hold}h, {'PRIMARY' if primary else 'variant (exploratory)'}")
        dm = T["demo"].astype(bool)
        is_, oos = T["t_in"] < OOS_MS, T["t_in"] >= OOS_MS
        for sd in (-1, 1):
            ms_ = dm & (T["side"] == sd)
            emit(f" {HYP[sd]}")
            full = line(T, ms_, "demo list 2021-01 .. 2026-09 (with funding)")
            r_is = line(T, ms_ & is_, "  IS < 2025-03")
            r_oos = line(T, ms_ & oos, "  OOS >= 2025-03")
            r15 = line(T, ms_, "  costs x1.5", "r15")
            if not primary:
                continue
            full_mo = line(T, ms_, "  same, CI clustered by calendar month", cl="mo")
            yrs = {y: line(T, ms_ & (T["year"] == y), f"  year {y}") for y in YEARS}
            line(T, ms_, "  costs x2", "r20")
            line(T, ms_, "  without funding", "r_nf")
            fk = T["fund_known"].astype(bool)
            emit(
                f"  funding history covers the hold (first settlement <= entry, last >= exit - 8h): {int((ms_ & fk).sum())} of {int(ms_.sum())} "
                f"({(ms_ & fk).sum() / max(1, ms_.sum()):.1%}); known from 2021-10-01, earlier entries get funding 0"
            )
            r_fk = line(T, ms_ & fk, "  trades with full funding coverage")
            line(T, ms_ & ~fk, "  trades without full funding coverage (0 / partial)")
            r_free = line(T, ms_ & T["free"].astype(bool), "  one position per symbol and side (free)")
            emit(
                f"  share overlapping an open opposite-side trade of the same coin: {(ms_ & T['opp'].astype(bool)).sum() / max(1, ms_.sum()):.1%}"
            )
            emit(
                f"  dropped (demo list, no full {hold}h path; not traded): {int(np.sum(T['drop_demo'].astype(bool) & (T['drop_side'] == sd)))}"
                if "drop_side" in T
                else "  dropped: n/a"
            )
            kinds = Counter(T["kind"][ms_].tolist())
            emit(
                f"  exits: stop {kinds.get(x4.KIND_STOP, 0)}, close through level {kinds.get(x4.KIND_INVALID, 0)}, time {kinds.get(x4.KIND_TIME, 0)}; "
                f"median hold {np.median(T['hold_h'][ms_]) if ms_.any() else math.nan:.0f}h"
            )
            line(T, (T["side"] == sd) & np.isin(T["sym"], tsy), "  trader's assets (no demo filter)")
            line(T, T["side"] == sd, "  all eligible coins (no demo filter)")
            tot_a: dict[str, float] = defaultdict(float)
            for s, v in zip(T["sym"][ms_], T["r"][ms_], strict=True):
                tot_a[str(s)] += float(v)
            top_a = [s for s, _ in sorted(tot_a.items(), key=lambda kv: -kv[1])[:5]]
            line(T, ms_ & ~np.isin(T["sym"], top_a), "  without the top-5 symbols")
            tot_w: dict[int, float] = defaultdict(float)
            for w_, v in zip(T["wk"][ms_], T["r"][ms_], strict=True):
                tot_w[int(w_)] += float(v)
            top_w = [w_ for w_, _ in sorted(tot_w.items(), key=lambda kv: -kv[1])[:5]]
            line(T, ms_ & ~np.isin(T["wk"], top_w), "  without the top-5 weeks")
            nf = line(T, ms_, "  (placebo reference: real without funding)", "r_nf")
            d, pm = placebo(out, name, T, ms_)
            T["d_pl"] = d
            okd = ms_ & np.isfinite(d)
            emit(f"  placebo ({N_PERM} sets, same symbol +-15 d, 4h-close starts, same side / stop / level distances, no funding):")
            emit(f"    trades without any placebo start (left out of the difference): {int((ms_ & ~np.isfinite(d)).sum())}")
            d_pl = line(T, okd, "  paired real - mean placebo, cluster week (✓)", "d_pl")
            line(T, okd, "  paired real - mean placebo, cluster month", "d_pl", cl="mo")
            p_pl = (1 + int(np.sum(pm >= nf["m"]))) / (N_PERM + 1)
            emit(
                f"    descriptive: placebo set means {pm.mean():+.4f}R, p95 {np.quantile(pm, 0.95):+.4f}; "
                f"real - placebo {nf['m'] - pm.mean():+.4f}R; set-rank p {p_pl:.4f} (not a condition: ignores clustering)"
            )
            se = max(full["se"], full_mo["se"]) if np.isfinite(full_mo["se"]) else full["se"]
            mde = Z_MDE * se
            emit(f"  MDE80 (larger of week / month cluster SE {full['se']:.4f} / {full_mo['se']:.4f}): {mde:.4f}R vs interest {INTEREST}R")
            per_asset(T, ms_, is_)
            prim[sd] = {
                "full": full,
                "full_mo": full_mo,
                "is": r_is,
                "oos": r_oos,
                "r15": r15,
                "d_pl": d_pl,
                "fk": r_fk,
                "free": r_free,
                "mde": mde,
                "yrs_pos": sum(1 for y in YEARS if yrs[y]["m"] > 0),
            }
        if primary:
            emit(" both sides pooled (descriptive)")
            line(T, dm, "demo list, both sides")
    if not prim:
        return
    sides = list(prim)
    adj = holm([one_sided(prim[sd]["full"]) for sd in sides])
    emit("\n[verdict per registered rule]")
    for sd, pa_ in zip(sides, adj, strict=True):
        x = prim[sd]
        f = x["full"]
        conds = {
            "lo_week": f["lo"] > 0,
            "lo_month": x["full_mo"]["lo"] > 0,
            "holm": pa_ < 0.05,
            "placebo_diff_lo": x["d_pl"]["lo"] > 0,
            "is": x["is"]["m"] > 0,
            "oos": x["oos"]["m"] > 0,
            "years>=4": x["yrs_pos"] >= 4,
            "x1.5": x["r15"]["m"] > 0,
            "funding_covered": x["fk"]["m"] > 0,
            "free": x["free"]["m"] > 0,
        }
        ok = all(conds.values())
        emit(f"  {HYP[sd]} conditions: " + ", ".join(f"{k} {'yes' if v else 'NO'}" for k, v in conds.items()))
        if ok:
            v = "confirmed (all conditions)"
        elif f["m"] > 0 and one_sided(f) < 0.05:
            v = "directional (≈): positive, not every condition"
        elif x["mde"] > INTEREST:
            v = "inconclusive (◯): MDE80 above the interest"
        else:
            v = "rejected (✗)"
        emit(
            f"  {HYP[sd]}: E[R] {f['m']:+.4f} lo week {f['lo']:+.4f} / month {x['full_mo']['lo']:+.4f}; p1 {one_sided(f):.4f}, "
            f"Holm {pa_:.4f}; placebo diff {x['d_pl']['m']:+.4f} lo {x['d_pl']['lo']:+.4f}; IS {x['is']['m']:+.4f} "
            f"OOS {x['oos']['m']:+.4f}; years > 0 {x['yrs_pos']}/6; x1.5 {x['r15']['m']:+.4f}; funding-covered {x['fk']['m']:+.4f}; "
            f"free {x['free']['m']:+.4f}; MDE {x['mde']:.4f} -> {v}"
        )


# ---------------------------------------------------------------- smoke


def synthetic(seed: int, n: int) -> tuple[Bars, np.ndarray]:
    rng = np.random.default_rng(seed)
    t = START_MS - 10 * D + np.arange(n, dtype=np.int64) * H
    c = 50 * np.exp(np.cumsum(rng.normal(0, 0.008, n)))
    o = np.concatenate([[50.0], c[:-1]])
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.004, n)))
    return Bars(t, o, h, low, c, H), rng.uniform(1e5, 1e6, n)


def smoke() -> None:
    """Synthetic random walks through run + report; prints counts only (report text goes to a temp file)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        n_rows = 0
        f_t = START_MS - 10 * D + np.arange(0, 24 * 400, 8, dtype=np.int64) * H
        for k, s in enumerate(("AAAUSDT", "BBBUSDT", "CCCUSDT")):
            r = process(s, *synthetic(k, 24 * 400), f_t, np.full(f_t.size, 0.0001), out)
            n_rows += r["n"]
        days = {START_MS - 10 * D + d * D: {"AAAUSDT", "BBBUSDT", "CCCUSDT"} for d in range(420)}
        rep = out / "report.txt"
        with rep.open("w") as fh:
            vsp.OUT["fh"] = fh
            sys.stdout = open("/dev/null", "w")  # noqa: SIM115  (smoke: no outcome numbers on screen)
            try:
                stage_report(out, days, ["BBBUSDT"])
            finally:
                sys.stdout.close()
                sys.stdout = sys.__stdout__
                vsp.OUT["fh"] = None
        lines = rep.read_text().splitlines()
        assert any(x.startswith("[verdict") for x in lines), "report incomplete"
        for key in (
            "conditions:",
            "paired real - mean placebo, cluster week",
            "cluster by calendar month",
            "full funding coverage",
            "dropped (demo",
        ):
            assert any(key in x or key.replace("cluster by", "clustered by") in x for x in lines), f"report lacks {key!r}"
        print(
            f"[smoke] synthetic: 3 symbols, rows {n_rows}, placebo files {len(list((out / 'placebo').glob('*.npy')))}, report lines {len(lines)}: ok"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("zero", "run", "report"))
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/xo4_vwap")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.smoke:
        smoke()
        return 0
    out = a.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        if a.stage == "zero":
            stage_zero(a)
        elif a.stage == "run":
            stage_run(a, out)
        elif a.stage == "report":
            syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
            elig = vsp.s1.eligible_syms(a.data_dir)  # the real top-100 over every eligible symbol (review 2026-11-23, item 6)
            stage_report(out, demo_days(vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig)), trader_syms(set(syms)))
        else:
            ap.error("--stage or --smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
