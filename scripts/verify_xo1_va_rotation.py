#!/usr/bin/env python3
"""XO-1, value-area edge rotation (IDEA-20261113-x-traders, item 5; LIB-20261113-x-trader_xo «4б» XO-1). Registration:
the card's subsection «5.1 XO-1: попередня реєстрація» (committed before any outcome was computed).

    uv run python scripts/verify_xo1_va_rotation.py --stage coverage   # stage 0: counters only, no outcome metric
    uv run python scripts/verify_xo1_va_rotation.py --smoke            # synthetic bars through run + report (no market)
    uv run python scripts/verify_xo1_va_rotation.py --stage run        # per-symbol trade rows + placebo (outcomes)
    uv run python scripts/verify_xo1_va_rotation.py --stage report     # tables (also <cache>/report.txt)

Data: the passport's Binance USD-M 1h bars with quote volume (``verify_setup_passport.load_1h_qv``; no network) and
the daily quote volume for the demo list (previous UTC day: top-100 by turnover and >= $5M).
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
from tradedesk.analytics import xo1_va_rotation as xo  # noqa: E402
from tradedesk.analytics.course_s14_ema import Bars  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.setup_passport import mean_ci  # noqa: E402
from tradedesk.analytics.stop_management import SLIPPAGE, TAKER, Fees  # noqa: E402

H, D = xo.HOUR_MS, xo.DAY_MS
SEED = 20261118
START_MS = vsp.s1.ms("2021-01-01T00:00")
OOS_MS = vsp.s1.ms("2025-03-01T00:00")
END_MS = vsp.s1.ms("2026-09-22T00:00")  # entries before; the 1h cache ends 2026-09-23 (passport END)
TOP_N, FLOOR = 100, 5e6  # demo list: guard.universe(size=100, min_turnover_usd=5e6), previous UTC day
FEES = Fees(maker=TAKER + SLIPPAGE, taker_exit=TAKER + SLIPPAGE)  # every side a taker order: 0.15 % per round trip
STRESS_MULT = {"r15": 1.5, "r20": 2.0}
N_PERM = 200
PLACEBO_HALF = 15 * 24  # placebo entries within +-15 days of the real one (1h bars)
PLACEBO_CHUNK = 40_000  # placebo trades per vectorised call (72 h hold: ~23 MB per float matrix)
INTEREST = 0.05  # R: the smallest net edge per trade worth trading (registered)
Z_MDE = 1.960 + 0.842  # one-sided alpha 0.025 (Holm's strictest step of 2) and 80 % power
CALLS = ROOT / "docs" / "calls" / "x" / "Trader_XO.csv"

# name -> window days, volume profile, k_probe, buf_atr, target ("poc" | "edge"), hold bars, primary
VARIANTS: dict[str, tuple[int, bool, int, float, str, int, bool]] = {
    "D1": (1, False, 3, 0.5, "poc", 24, True),
    "C7": (7, False, 3, 0.5, "poc", 72, True),
    "D1v": (1, True, 3, 0.5, "poc", 24, False),
    "D1b25": (1, False, 3, 0.25, "poc", 24, False),
    "D1b100": (1, False, 3, 1.0, "poc", 24, False),
    "D1k1": (1, False, 1, 0.5, "poc", 24, False),
    "D1k6": (1, False, 6, 0.5, "poc", 24, False),
    "D1e": (1, False, 3, 0.5, "edge", 24, False),
}
PRIMARY = tuple(k for k, v in VARIANTS.items() if v[6])
emit = vsp.emit


def week(t: np.ndarray) -> np.ndarray:
    """ISO week index (Monday start) of open ms."""
    return (np.asarray(t, dtype=np.int64) // D + 3) // 7


# ---------------------------------------------------------------- per symbol


def symbol_signals(b: Bars, qv: np.ndarray, names: tuple[str, ...]) -> tuple[dict[str, list[xo.Signal]], dict[int, bool]]:
    atr = xo.atr_sma(b.h, b.low, b.c, 14)
    lv: dict[tuple[int, bool], xo.Levels] = {}
    out: dict[str, list[xo.Signal]] = {}
    for name in names:
        w, vol, k, buf, _, _, _ = VARIANTS[name]
        if (w, vol) not in lv:
            lv[(w, vol)] = xo.day_levels(b.t, b.low, b.h, w, vol=qv if vol else None)
        sig = xo.find_signals(b.t, b.h, b.low, b.c, atr, lv[(w, vol)], k_probe=k, buf_atr=buf)
        out[name] = [s for s in sig if START_MS <= s.t_in < END_MS]
    daily = lv.get((1, False)) or xo.day_levels(b.t, b.low, b.h, 1)
    return out, xo.balance_flags(daily)


def process(s: str, b: Bars, qv: np.ndarray, out: Path) -> dict[str, Any]:
    """Trade rows of every variant and the placebo matrices of the primaries for one symbol (outcomes)."""
    sigs, bal = symbol_signals(b, qv, tuple(VARIANTS))
    res: dict[str, Any] = {}
    rng = np.random.default_rng(SEED + zlib.crc32(s.encode()))
    for name, sig in sigs.items():
        _, _, _, _, tgt, n_hold, primary = VARIANTS[name]
        valid = xo.valid_starts(b.t, n_hold)
        i0 = np.array([x.i + 1 for x in sig], dtype=np.int64)
        ok = np.isin(i0, valid)
        sig = [x for x, f in zip(sig, ok, strict=True) if f]
        i0 = i0[ok]
        side = np.array([x.side for x in sig], dtype=np.int64)
        e = np.array([x.entry for x in sig])
        st = np.array([x.stop for x in sig])
        tg = np.array([x.target if tgt == "poc" else x.target2 for x in sig])
        r, ex, kind = xo.barrier_outcomes(b.o, b.h, b.low, b.c, i0, side, e, st, tg, n_hold, FEES)
        t_in = np.array([x.t_in for x in sig], dtype=np.int64)
        t_ex = b.t[ex] + H if ex.size else np.zeros(0, dtype=np.int64)
        row: dict[str, Any] = {
            "t_in": t_in.tolist(),
            "side": side.tolist(),
            "r": np.round(r, 6).tolist(),
            "kind": kind.tolist(),
            "free": xo.non_overlapping(t_in, t_ex).tolist(),
            "bal": [bal.get(x.day) for x in sig],
            "stop_pct": np.round(np.abs(e - st) / e, 6).tolist(),
            "rr": np.round(np.abs(tg - e) / np.abs(e - st), 4).tolist(),
        }
        for k, mult in STRESS_MULT.items():
            f = FEES.scaled(mult)
            row[k] = np.round(xo.barrier_outcomes(b.o, b.h, b.low, b.c, i0, side, e, st, tg, n_hold, f)[0], 6).tolist()
        res[name] = row
        if primary and i0.size:
            ps = xo.placebo_starts(valid, i0, PLACEBO_HALF, N_PERM, rng)
            dst, dtg = np.abs(e - st) / e, np.abs(tg - e) / e
            pr = np.full(ps.shape, np.nan, dtype=np.float32)
            okp = ps >= 0
            j = ps[okp]
            sd = np.broadcast_to(side[:, None], ps.shape)[okp]
            pe = b.o[j]
            pst = pe * (1 - sd * np.broadcast_to(dst[:, None], ps.shape)[okp])
            ptg = pe * (1 + sd * np.broadcast_to(dtg[:, None], ps.shape)[okp])
            vals = np.empty(j.size)
            for c0 in range(0, j.size, PLACEBO_CHUNK):  # bounded memory: chunk x hold bars per matrix
                sl = slice(c0, c0 + PLACEBO_CHUNK)
                vals[sl] = xo.barrier_outcomes(b.o, b.h, b.low, b.c, j[sl], sd[sl], pe[sl], pst[sl], ptg[sl], n_hold, FEES)[0]
            pr[okp] = vals
            (out / "placebo").mkdir(parents=True, exist_ok=True)
            np.save(out / "placebo" / f"{s}.{name}.npy", pr)
    (out / "rows").mkdir(parents=True, exist_ok=True)
    (out / "rows" / f"{s}.json").write_text(json.dumps(res, separators=(",", ":")))
    return {"sym": s, "n": sum(len(v["r"]) for v in res.values()), "rss": vsp.rss_mb()}


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out = args
    b, qv = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    return process(s, b, qv, Path(out))


def coverage_symbol(args: tuple[str, str, str]) -> dict[str, Any]:
    """Stage 0: signals of the primaries only (no path after the entry is read), their geometry and dates."""
    s, s1_cache, data_dir = args
    b, qv = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    sigs, bal = symbol_signals(b, qv, PRIMARY)
    res: dict[str, Any] = {"sym": s, "first": int(b.t[0]) if len(b.t) else 0, "last": int(b.t[-1]) if len(b.t) else 0, "rss": vsp.rss_mb()}
    for name, sig in sigs.items():
        valid = set(xo.valid_starts(b.t, VARIANTS[name][5]).tolist())
        res[name] = [
            (
                x.t_in,
                x.side,
                round(abs(x.entry - x.stop) / x.entry, 6),
                round(abs(x.target - x.entry) / abs(x.entry - x.stop), 4),
                x.i + 1 in valid,
                bal.get(x.day),
            )
            for x in sig
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
    prev = (t_in // D) * D - D
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


def stage_coverage(a: argparse.Namespace) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    all_syms = vsp.s1.rd(a.data_dir / "symbols.json")
    emit(f"[coverage] universe: {len(all_syms)} Binance USD-M symbols, eligible with 1h bars {len(syms)} (TradFi / non-USDT out)")
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, syms)
    days = demo_days(qv)
    tsy = trader_syms(set(syms))
    emit(f"[coverage] trader's assets with bars: {len(tsy)}: {' '.join(x.removesuffix('USDT') for x in tsy)}")
    t0 = time.time()
    agg: dict[str, list[tuple[str, tuple[Any, ...]]]] = {n: [] for n in PRIMARY}
    last: dict[str, int] = {}
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for j, r in enumerate(pool.imap_unordered(coverage_symbol, [(s, str(a.s1_cache), str(a.data_dir)) for s in syms]), 1):
            last[r["sym"]] = r["last"]
            for n in PRIMARY:
                agg[n].extend((r["sym"], x) for x in r[n])
            if j % 50 == 0 or j == len(syms):
                emit(f"  [coverage] {j}/{len(syms)} {time.time() - t0:.0f}s; worker rss {r['rss']:.0f} MB; main {vsp.rss_mb():.0f} MB")
    gone = sum(1 for v in last.values() if v < vsp.s1.ms("2026-09-01T00:00"))
    emit(f"[coverage] symbols whose bars end before 2026-09 (delisted, kept: no survivorship filter): {gone}")
    for n in PRIMARY:
        rows = agg[n]
        t_in = np.array([x[1][0] for x in rows], dtype=np.int64)
        dm = np.array([x[0] in days.get(int(t // D * D - D), ()) for x, t in zip(rows, t_in, strict=True)], dtype=bool)
        vh = np.array([x[1][4] for x in rows], dtype=bool)
        m = dm & vh
        side = np.array([x[1][1] for x in rows])
        sp_ = np.array([x[1][2] for x in rows])
        rr = np.array([x[1][3] for x in rows])
        bal = np.array([x[1][5] is True for x in rows])
        nw = np.unique(week(t_in[m])).size
        nsw = len({(x[0], int(w)) for x, w, f in zip(rows, week(t_in), m, strict=True) if f})
        emit(
            f"\n[coverage {n}] signals 2021-01 .. 2026-09-21: all eligible {len(rows)}; demo list {int(dm.sum())}; with a full hold path {int(m.sum())}"
        )
        emit(
            f"  demo list: longs {int((side[m] == 1).sum())}, shorts {int((side[m] == -1).sum())}; symbols {len({x[0] for x, f in zip(rows, m, strict=True) if f})}; weeks {nw}; symbol-weeks {nsw}"
        )
        emit(
            f"  IS (< 2025-03) {int((m & (t_in < OOS_MS)).sum())}, OOS {int((m & (t_in >= OOS_MS)).sum())}; balance days {int((m & bal).sum())}"
        )
        yrs = Counter(time.gmtime(int(t) / 1000).tm_year for t in t_in[m])
        emit("  by year: " + ", ".join(f"{y} {c}" for y, c in sorted(yrs.items())))
        if m.any():
            q = np.quantile(sp_[m], [0.1, 0.5, 0.9])
            qr = np.quantile(rr[m], [0.1, 0.5, 0.9])
            emit(
                f"  stop distance p10 / p50 / p90: {q[0]:.4f} / {q[1]:.4f} / {q[2]:.4f}; target / stop distance: {qr[0]:.2f} / {qr[1]:.2f} / {qr[2]:.2f}"
            )
            cost = 2 * (TAKER + SLIPPAGE) / np.maximum(sp_[m], 1e-9)
            emit(f"  cost per round trip in R (0.15 % / stop distance): median {np.median(cost):.3f}R")
            for sd in (0.8, 1.0, 1.2):
                emit(
                    f"  a-priori MDE80 (sd {sd}R, one-sided 0.025): iid n={int(m.sum())} {Z_MDE * sd / math.sqrt(m.sum()):.4f}R; "
                    f"one obs per symbol-week n={nsw} {Z_MDE * sd / math.sqrt(nsw):.4f}R; one per week n={nw} {Z_MDE * sd / math.sqrt(max(nw, 1)):.4f}R"
                )
        tm = np.array([x[0] in tsy for x in rows], dtype=bool) & vh
        emit(f"  trader's assets (no demo-list filter): {int(tm.sum())} signals")
    known_level_check(a)


def known_level_check(a: argparse.Namespace) -> None:
    """Levels only: BTC previous-day profile of 2025-04-14 vs the author's «prev VAH 86.2» (tweet 1912176095975145617)."""
    b, qv = vsp.load_1h_qv(a.s1_cache, a.data_dir, "BTCUSDT")
    d = vsp.s1.ms("2025-04-15T00:00")
    for vol in (False, True):
        lv = xo.day_levels(b.t, b.low, b.h, 1, vol=qv if vol else None)
        k = int(np.searchsorted(lv.day, d))
        emit(
            f"[known] BTCUSDT 2025-04-14 {'volume' if vol else 'TPO'} profile: VAL {lv.val[k]:.0f} POC {lv.poc[k]:.0f} VAH {lv.vah[k]:.0f}"
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


def load(out: Path, name: str, days: dict[int, set[str]]) -> dict[str, np.ndarray]:
    cols: dict[str, list[Any]] = defaultdict(list)
    for p in sorted((out / "rows").glob("*.json")):
        s = p.stem
        r = json.loads(p.read_text()).get(name)
        if not r or not r["r"]:
            continue
        t = np.array(r["t_in"], dtype=np.int64)
        cols["sym"].extend([s] * t.size)
        cols["demo"].extend(in_demo(s, t, days).tolist())
        for k in ("t_in", "side", "r", "r15", "r20", "kind", "free", "stop_pct", "rr"):
            cols[k].extend(r[k])
        cols["bal"].extend([x is True for x in r["bal"]])
        cols["bal_known"].extend([x is not None for x in r["bal"]])
    T = {k: np.array(v) for k, v in cols.items()}
    if T:
        T["wk"] = week(T["t_in"])
    return T


def fmt(r: dict[str, float]) -> str:
    return f"n {int(r['n'])} wk {int(r['k'])} E[R] {r['m']:+.4f} [{r['lo']:+.4f}; {r['hi']:+.4f}] p2 {r['p']:.4f}"


def line(T: dict[str, np.ndarray], m: np.ndarray, label: str, key: str = "r") -> dict[str, float]:
    r = (
        mean_ci(T[key][m], T["wk"][m], seed=SEED)
        if m.any()
        else {"n": 0, "k": 0, "m": math.nan, "lo": math.nan, "hi": math.nan, "p": math.nan, "se": math.nan}
    )
    emit(f"  {label:<44} {fmt(r)}")
    return r


def placebo(out: Path, name: str, T: dict[str, np.ndarray], m: np.ndarray) -> np.ndarray:
    """Per permutation: the mean placebo R over the trades of mask ``m`` (rows in the order of ``load``)."""
    tot, cnt = np.zeros(N_PERM), np.zeros(N_PERM)
    pos = 0
    for s in dict.fromkeys(T["sym"].tolist()):
        n = int(np.sum(T["sym"] == s))
        mm = m[pos : pos + n]
        pos += n
        if not mm.any():
            continue
        pr = np.load(out / "placebo" / f"{s}.{name}.npy")[mm].astype(float)
        tot += np.nansum(pr, axis=0)
        cnt += np.sum(np.isfinite(pr), axis=0)
    return tot / np.maximum(cnt, 1)


def one_sided(r: dict[str, float]) -> float:
    if not np.isfinite(r["p"]):
        return math.nan
    return r["p"] / 2 if r["m"] > 0 else 1 - r["p"] / 2


def stage_report(out: Path, days: dict[int, set[str]], tsy: list[str]) -> None:
    prim: dict[str, dict[str, Any]] = {}
    for name in VARIANTS:
        T = load(out, name, days)
        if not T:
            emit(f"\n[{name}] no trades")
            continue
        w, vol, k, buf, tgt, hold, primary = VARIANTS[name]
        emit(
            f"\n[{name}] window {w}d {'volume' if vol else 'TPO'}, k_probe {k}, buffer {buf} ATR, target {tgt}, hold {hold}h, {'PRIMARY' if primary else 'variant (exploratory)'}"
        )
        dm = T["demo"].astype(bool)
        is_, oos = T["t_in"] < OOS_MS, T["t_in"] >= OOS_MS
        full = line(T, dm, "demo list 2021-01 .. 2026-09")
        line(T, dm & is_, "  IS < 2025-03")
        line(T, dm & oos, "  OOS >= 2025-03")
        if not primary:
            line(T, dm, "  costs x1.5", "r15")
            continue
        r_is, r_oos = mean_ci(T["r"][dm & is_], T["wk"][dm & is_], seed=SEED), mean_ci(T["r"][dm & oos], T["wk"][dm & oos], seed=SEED)
        for y in range(2021, 2027):
            yy = np.array([time.gmtime(int(t) / 1000).tm_year == y for t in T["t_in"]])
            line(T, dm & yy, f"  year {y}")
        for sd, lab in ((1, "longs"), (-1, "shorts")):
            line(T, dm & (T["side"] == sd), f"  {lab}")
        r15 = line(T, dm, "  costs x1.5", "r15")
        r20 = line(T, dm, "  costs x2", "r20")
        line(T, dm & T["free"].astype(bool), "  one position per symbol (non-overlapping)")
        kinds = Counter(T["kind"][dm].tolist())
        emit(f"  exits: target {kinds.get(2, 0)}, stop {kinds.get(1, 0)}, time {kinds.get(0, 0)}; median R:R {np.median(T['rr'][dm]):.2f}")
        bk = T["bal_known"].astype(bool)
        line(T, dm & bk & T["bal"].astype(bool), "  exploratory: balance day (VA overlap)")
        line(T, dm & bk & ~T["bal"].astype(bool), "  exploratory: not balance")
        line(T, np.isin(T["sym"], tsy), "  trader's assets (no demo filter)")
        line(T, np.ones(dm.size, dtype=bool), "  all eligible coins (no demo filter)")
        # drop the top contributors
        tot_a = defaultdict(float)
        for s, v in zip(T["sym"][dm], T["r"][dm], strict=True):
            tot_a[str(s)] += float(v)
        top_a = [s for s, _ in sorted(tot_a.items(), key=lambda kv: -kv[1])[:5]]
        line(T, dm & ~np.isin(T["sym"], top_a), "  without the top-5 symbols")
        tot_w = defaultdict(float)
        for w_, v in zip(T["wk"][dm], T["r"][dm], strict=True):
            tot_w[int(w_)] += float(v)
        top_w = [w_ for w_, _ in sorted(tot_w.items(), key=lambda kv: -kv[1])[:5]]
        line(T, dm & ~np.isin(T["wk"], top_w), "  without the top-5 weeks")
        # placebo
        pm = placebo(out, name, T, dm)
        p_pl = (1 + int(np.sum(pm >= full["m"]))) / (N_PERM + 1)
        emit(
            f"  placebo ({N_PERM} sets, same symbol +-15 d, same side / stop / target distances): mean {pm.mean():+.4f}R, p95 {np.quantile(pm, 0.95):+.4f}; real - placebo {full['m'] - pm.mean():+.4f}R; p {p_pl:.4f}"
        )
        mde = Z_MDE * full["se"]
        emit(f"  MDE80 (cluster SE {full['se']:.4f}): {mde:.4f}R vs interest {INTEREST}R")
        # 5a per asset
        v, sy, wk = T["r"][dm], T["sym"][dm], T["wk"][dm]
        q = pa.cluster_robust_q(v, sy, wk, min_n=30)
        emit(f"  5a heterogeneity (assets with n >= 30): k {q['k']}, Q {q['q']:.1f}, df {q['df']}, p {q['p']}, I2 {q['i2']:.3f}")
        st = sorted(pa.asset_stats(v, list(sy), min_n=30), key=lambda x: -x.n)[:12]
        emit("  5a largest assets: " + "; ".join(f"{x.asset.removesuffix('USDT')} n {x.n} {x.mean:+.3f}" for x in st))
        per = pa.persistence(v, sy, wk, is_[dm], top_n=10, seed=SEED)
        emit(
            f"  5a persistence top-10 IS -> OOS: top {per['top_oos_mean']:+.4f} (n {per['n_top_oos']}) vs rest {per['rest_oos_mean']:+.4f}; "
            f"diff {per['diff']:+.4f} CI {per['diff_ci']} p {per['diff_p']}; rho {per['rho']} (n {per['rho_n']})"
        )
        prim[name] = {"full": full, "is": r_is, "oos": r_oos, "r15": r15, "r20": r20, "p_pl": p_pl, "mde": mde}
    if not prim:
        return
    names = list(prim)
    adj = holm([one_sided(prim[n]["full"]) for n in names])
    emit("\n[verdict per registered rule]")
    for n, pa_ in zip(names, adj, strict=True):
        x = prim[n]
        f = x["full"]
        ok = f["lo"] > 0 and pa_ < 0.05 and x["p_pl"] < 0.05 and x["is"]["m"] > 0 and x["oos"]["m"] > 0 and x["r15"]["m"] > 0
        if ok:
            v = "confirmed (all conditions)"
        elif f["m"] > 0 and one_sided(f) < 0.05:
            v = "directional (≈): positive, not every condition"
        elif x["mde"] > INTEREST:
            v = "inconclusive (◯): MDE80 above the interest"
        else:
            v = "rejected (✗)"
        emit(
            f"  {n}: E[R] {f['m']:+.4f} lo {f['lo']:+.4f}; p1 {one_sided(f):.4f}, Holm {pa_:.4f}; placebo p {x['p_pl']:.4f}; IS {x['is']['m']:+.4f} OOS {x['oos']['m']:+.4f}; x1.5 {x['r15']['m']:+.4f}; MDE {x['mde']:.4f} -> {v}"
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
        for k, s in enumerate(("AAAUSDT", "BBBUSDT", "CCCUSDT")):
            r = process(s, *synthetic(k, 24 * 400), out)
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
        print(
            f"[smoke] synthetic: 3 symbols, rows {n_rows}, placebo files {len(list((out / 'placebo').glob('*.npy')))}, report lines {len(lines)}: ok"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("coverage", "run", "report"))
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/xo1_va_rotation")
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
        if a.stage == "coverage":
            stage_coverage(a)
        elif a.stage == "run":
            stage_run(a, out)
        elif a.stage == "report":
            syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
            stage_report(out, demo_days(vsp.s1.daily_qv(a.s1_cache, a.data_dir, syms)), trader_syms(set(syms)))
        else:
            ap.error("--stage or --smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
