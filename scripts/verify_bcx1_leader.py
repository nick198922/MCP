#!/usr/bin/env python3
"""BCX-1: the intraday leader by residual 12 h return, continuation (long) and reversal (short after momentum loss).

IDEA-20261113-x-traders, item 5; spec «4б» BCX-1 of docs/library/LIB-20261114-x-btc_charlie.md; registration in the
card, section 5.6. Data: the 1h Binance USD-M cache of the setup passports (``--s1-cache``, delisted symbols kept, TradFi
out by ``underlyingType``), daily turnover and funding from ``--data-dir`` (no network: both caches exist).

Stages:
  --stage zero    events, trades with a full path, stop geometry, a-priori MDE, SD / ICC of placebo trades (random
                  times, no means), a level check against the author's charts. No real trade's result is computed.
  --stage run     real trades and placebo sets of both branches -> <cache-dir>/run.npz
  --stage report  verdict table -> <cache-dir>/report.txt
  --smoke         synthetic random walks through zero + run + report; prints counts only
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import verify_setup_passport as vsp  # noqa: E402  (1h loaders, eligible symbols, daily turnover)
import verify_xo4_vwap as vx4  # noqa: E402  (funding loader, fees of XO-4)

from tradedesk.analytics import bcx1_leader as bx  # noqa: E402
from tradedesk.analytics import per_asset as pa  # noqa: E402
from tradedesk.analytics import xo4_vwap as x4  # noqa: E402
from tradedesk.analytics.course_s14_ema import Bars  # noqa: E402
from tradedesk.analytics.liquidation_flush import STABLE_OR_WRAPPED, holm  # noqa: E402
from tradedesk.analytics.setup_passport import mean_ci  # noqa: E402

H, D = bx.HOUR_MS, bx.DAY_MS
SEED = 20261124
GRID0 = vsp.s1.ms("2020-12-01T00:00")
GRID1 = vsp.s1.ms("2026-09-23T00:00")  # the 1h cache ends here
START_MS = vsp.s1.ms("2021-02-01T00:00")  # trigger bars from here (30 d of beta and turnover before)
END_MS = vsp.s1.ms("2026-09-21T00:00")  # trigger bars before (reversal wait 24 h + hold 12 h must fit)
OOS_MS = vsp.s1.ms("2025-03-01T00:00")
BTC = "BTCUSDT"
TOP_K, MIN_E, LOOKBACK, BTC_FLOOR = 3, 0.05, 24, -0.02
BETA_WIN, BETA_LAG, BETA_MIN, NRET = 720, 12, 360, 12
ATR_N, STOP_ATR, HOLD = 24, 1.5, 12
MAX_WAIT, N_LOW, N_STALE = 24, 3, 3
MIN_STOP, MAX_STOP = 0.003, 0.25
UNI_TOP, UNI_FLOOR, UNI_DAYS = 100, 5e6, 30
FEES = vx4.FEES  # every side a taker order plus slippage: 0.15 % per round trip
N_PL, PL_HALF = 100, 15 * 24
INTEREST = 0.05
Z_MDE = 1.960 + 0.842  # one-sided alpha 0.025 (Holm's strictest step of 2), power 0.8
YEARS = tuple(range(2021, 2027))
BRANCH = {1: "HC continuation (long leader)", -1: "HR reversal (short after momentum loss)"}
CALLS = ROOT / "docs" / "calls" / "x" / "btc_charlie.csv"
emit = vsp.emit


def base(s: str) -> str:
    return s.removesuffix("USDT").removeprefix("1000")


def tweet_ms(tid: int) -> int:
    return (tid >> 22) + 1288834974657


def iso(t: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t / 1000))


# ---------------------------------------------------------------- universe


def universe_days(qv: dict[str, list[tuple[int, float]]], syms: list[str]) -> dict[int, set[str]]:
    """UTC day -> top-``UNI_TOP`` of ``syms`` by the mean daily quote turnover of the ``UNI_DAYS`` days before it
    (missing days count 0), mean >= ``UNI_FLOOR``. Day d uses days d - 30 .. d - 1 only."""
    days = np.arange(GRID0 - UNI_DAYS * D, GRID1 + D, D, dtype=np.int64)
    m = np.zeros((days.size, len(syms)))
    for i, s in enumerate(syms):
        for t, v in qv.get(s, []):
            k = (int(t) - int(days[0])) // D
            if 0 <= k < days.size and int(t) % D == 0:
                m[k, i] = v
    cs = np.vstack([np.zeros((1, len(syms))), np.cumsum(m, axis=0)])
    out: dict[int, set[str]] = {}
    for k in range(UNI_DAYS, days.size):
        mean = (cs[k] - cs[k - UNI_DAYS]) / UNI_DAYS
        order = np.argsort(-mean, kind="stable")[:UNI_TOP]
        out[int(days[k])] = {syms[i] for i in order if mean[i] >= UNI_FLOOR}
    return out


def demo_days(qv: dict[str, list[tuple[int, float]]]) -> dict[int, set[str]]:
    """The demo list (secondary row): top-100 by the previous UTC day's turnover over every eligible symbol, >= $5M."""
    by_day: dict[int, list[tuple[float, str]]] = defaultdict(list)
    for s, rows in qv.items():
        for t, v in rows:
            by_day[int(t)].append((v, s))
    return {t + D: {s for v, s in sorted(xs, reverse=True)[:100] if v >= 5e6} for t, xs in by_day.items()}


def trader_syms() -> set[str]:
    with CALLS.open() as fh:
        return {f"{(r.get('asset') or '').strip().upper()}USDT" for r in csv.DictReader(x for x in fh if not x.startswith("#"))}


# ---------------------------------------------------------------- events and trades


def grid_close(b: Bars, n: int) -> np.ndarray:
    return bx.on_grid(b.t, b.c, GRID0, n)


def find_events(bars: dict[str, Bars], btc: Bars, uni: dict[int, set[str]]) -> tuple[list[tuple[str, int]], dict[str, Any]]:
    """Events (symbol, coin bar index of the trigger bar) of the registered rule, plus counters."""
    n = (GRID1 - GRID0) // H
    syms = sorted(bars)
    cb = grid_close(btc, n)
    e = np.full((n, len(syms)), np.nan, dtype=np.float32)
    for i, s in enumerate(syms):
        cc = grid_close(bars[s], n)
        e[:, i] = bx.residual_return(cc, cb, bx.lagged_beta(cc, cb, window=BETA_WIN, lag=BETA_LAG, min_obs=BETA_MIN), NRET)
    row_t = GRID0 + np.arange(n, dtype=np.int64) * H
    day = (row_t // D) * D
    mem = np.zeros((n, len(syms)), dtype=bool)
    col = {s: i for i, s in enumerate(syms)}
    for d in np.unique(day):
        cols = [col[s] for s in uni.get(int(d), ()) if s in col]
        if cols:
            rr = np.flatnonzero(day == d)
            mem[rr[0] : rr[-1] + 1, cols] = True
    with np.errstate(invalid="ignore", divide="ignore"):
        r3 = np.full(n, np.nan)
        r3[3:] = cb[3:] / cb[:-3] - 1.0
    rows, cols = bx.leader_events(e, mem, r3, k=TOP_K, min_e=MIN_E, lookback=LOOKBACK, btc_floor=BTC_FLOOR)
    keep = (row_t[rows] >= START_MS) & (row_t[rows] < END_MS)
    out = []
    for r, c in zip(rows[keep], cols[keep], strict=True):
        s = syms[int(c)]
        j = int(np.searchsorted(bars[s].t, row_t[r]))
        out.append((s, j))
    stats = {
        "rows": n,
        "member_cells": int(mem.sum()),
        "e_finite_member": int((np.isfinite(e) & mem).sum()),
        "events_all": int(rows.size),
    }
    return out, stats


def make_trades(
    bars: dict[str, Bars], events: list[tuple[str, int]]
) -> tuple[dict[int, list[tuple[str, int, float, float]]], Counter[str]]:
    """Per branch: (symbol, trigger bar j, entry, stop); entry = open of bar j + 1, stop STOP_ATR x ATR(24) of bar j."""
    atr: dict[str, np.ndarray] = {}
    out: dict[int, list[tuple[str, int, float, float]]] = {1: [], -1: []}
    drop: Counter[str] = Counter()
    for s, j in events:
        b = bars[s]
        if s not in atr:
            atr[s] = x4.atr_sma(b.h, b.low, b.c, ATR_N)
        for side in (1, -1):
            jj = j if side == 1 else bx.momentum_loss_bar(b.t, b.h, b.low, b.c, j, max_wait=MAX_WAIT, n_low=N_LOW, n_stale=N_STALE)
            if jj < 0:
                drop[f"{side}:no trigger"] += 1
                continue
            if not bx.full_path(b.t, jj, HOLD):
                drop[f"{side}:no full path"] += 1
                continue
            en = float(b.o[jj + 1])
            st = en - side * STOP_ATR * float(atr[s][jj])
            dist = side * (en - st) / en
            if not (np.isfinite(dist) and MIN_STOP <= dist <= MAX_STOP):
                drop[f"{side}:stop out of bounds"] += 1
                continue
            out[side].append((s, jj, en, st))
    return out, drop


def simulate(b: Bars, rows: list[tuple[int, float, float]], side: int, fees: Any = FEES) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """R net of fees (no funding), exit bar, exit kind for trades (j, entry, stop) of one symbol and side."""
    if not rows:
        z = np.zeros(0)
        return z, z.astype(np.int64), z.astype(np.int64)
    j = np.array([r[0] for r in rows], dtype=np.int64)
    en = np.array([r[1] for r in rows])
    st = np.array([r[2] for r in rows])
    sd = np.full(j.size, side)
    return x4.vwap_exit_outcomes(b.t, b.o, b.h, b.low, b.c, j + 1, sd, en, st, -sd * np.inf, HOLD, fees)


def placebo_r(b: Bars, j: np.ndarray, dist: np.ndarray, side: int, n_sets: int, rng: np.random.Generator) -> np.ndarray:
    """(len(j), n_sets) R of random trigger bars of the same coin (+-15 d), same side and stop distance, no funding."""
    pb = bx.placebo_bars(b.t, j, HOLD, n_sets, PL_HALF, rng)
    out = np.full(pb.shape, np.nan)
    ok = pb >= 0
    if not ok.any():
        return out
    jj = pb[ok]
    en = b.o[jj + 1]
    dd = np.broadcast_to(dist[:, None], pb.shape)[ok]
    st = en * (1 - side * dd)
    sd = np.full(jj.size, side)
    r, _, _ = x4.vwap_exit_outcomes(b.t, b.o, b.h, b.low, b.c, jj + 1, sd, en, st, -sd * np.inf, HOLD, FEES)
    out[ok] = r
    return out


# ---------------------------------------------------------------- data


def load_all(a: argparse.Namespace) -> tuple[dict[str, Bars], Bars, dict[str, list[tuple[int, float]]], list[str]]:
    elig = vsp.s1.eligible_syms(a.data_dir)
    syms = [s for s in vsp.syms_with_1h(a.s1_cache, a.data_dir) if s != BTC and base(s) not in STABLE_OR_WRAPPED]
    if a.limit:
        syms = syms[: a.limit]
    t0 = time.time()
    bars: dict[str, Bars] = {}
    for k, s in enumerate(syms, 1):
        b, _ = vsp.load_1h_qv(a.s1_cache, a.data_dir, s)
        if len(b):
            bars[s] = b
        if k % 100 == 0 or k == len(syms):
            emit(f"  [load] {k}/{len(syms)} {time.time() - t0:.0f}s; rss {vsp.rss_mb():.0f} MB")
    btc, _ = vsp.load_1h_qv(a.s1_cache, a.data_dir, BTC)
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig)
    return bars, btc, qv, elig


def universe_syms(elig: list[str]) -> list[str]:
    return [s for s in elig if s != BTC and base(s) not in STABLE_OR_WRAPPED]


# ---------------------------------------------------------------- stage 0


def zero_summary(bars: dict[str, Bars], trades: dict[int, list[tuple[str, int, float, float]]], drop: Counter[str]) -> None:
    rng = np.random.default_rng(SEED)
    for side in (1, -1):
        rows = trades[side]
        t_in = np.array([int(bars[s].t[j]) + H for s, j, _, _ in rows], dtype=np.int64)
        dist = np.array([side * (en - st) / en for _, _, en, st in rows])
        emit(
            f"\n[zero {BRANCH[side]}] trades {len(rows)}; dropped: no trigger {drop[f'{side}:no trigger']}, "
            f"no full path {drop[f'{side}:no full path']}, stop out of [{MIN_STOP}, {MAX_STOP}] {drop[f'{side}:stop out of bounds']}"
        )
        if not rows:
            continue
        dd, wk = t_in // D, vx4.week(t_in)
        yrs = Counter(time.gmtime(int(t) / 1000).tm_year for t in t_in)
        emit(
            f"  IS (< 2025-03) {int((t_in < OOS_MS).sum())}, OOS {int((t_in >= OOS_MS).sum())}; days {np.unique(dd).size}; "
            f"weeks {np.unique(wk).size}; symbols {len({r[0] for r in rows})}; per day max {Counter(dd.tolist()).most_common(1)[0][1]}"
        )
        emit("  by year: " + ", ".join(f"{y} {yrs.get(y, 0)}" for y in YEARS))
        q = np.quantile(dist, [0.1, 0.5, 0.9])
        cost = 2 * (FEES.maker) / np.maximum(dist, 1e-9)
        emit(f"  stop distance p10 / p50 / p90: {q[0]:.4f} / {q[1]:.4f} / {q[2]:.4f}; cost per round trip median {np.median(cost):.3f}R")
        # placebo trades only (random times of the same coins, same side and stop distance): SD and ICC, no means
        by: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(rows):
            by[r[0]].append(i)
        pr = np.full(len(rows), np.nan)
        for s, idx in by.items():
            j = np.array([rows[i][1] for i in idx], dtype=np.int64)
            pr[idx] = placebo_r(bars[s], j, dist[idx], side, 1, rng)[:, 0]
        ok = np.isfinite(pr)
        sd = float(np.std(pr[ok], ddof=1)) if ok.sum() > 1 else math.nan
        icc_d = bx.icc_oneway(pr[ok], dd[ok])
        icc_w = bx.icc_oneway(pr[ok], wk[ok])
        nd, nw = np.unique(dd).size, np.unique(wk).size
        m_d, m_w = len(rows) / nd, len(rows) / nw
        deff_d = 1 + (m_d - 1) * max(icc_d, 0) if np.isfinite(icc_d) else math.nan
        deff_w = 1 + (m_w - 1) * max(icc_w, 0) if np.isfinite(icc_w) else math.nan
        emit(f"  placebo trades (one per trade, random time): n {int(ok.sum())}; SD {sd:.3f}R; ICC day {icc_d:.4f}, week {icc_w:.4f}")
        for sdv in (0.8, 1.0, 1.2, sd):
            emit(
                f"  a-priori MDE80 (sd {sdv:.2f}R): iid {Z_MDE * sdv / math.sqrt(len(rows)):.4f}R; "
                f"day ICC deff {deff_d:.2f} -> {Z_MDE * sdv * math.sqrt(deff_d / len(rows)):.4f}R; "
                f"week ICC deff {deff_w:.2f} -> {Z_MDE * sdv * math.sqrt(deff_w / len(rows)):.4f}R; one per day {Z_MDE * sdv / math.sqrt(nd):.4f}R"
            )


def level_check(bars: dict[str, Bars], btc: Bars) -> None:
    """The author's charts: who led at the last closed hour before the tweet (e12 rank among cached coins, no outcome)."""
    n = (GRID1 - GRID0) // H
    cb = grid_close(btc, n)
    for tid, names in ((2082896353638621480, ("UNIUSDT", "KAITOUSDT")), (2001273280854909412, ())):
        tt = tweet_ms(tid)
        r = (tt - GRID0) // H - 1  # the bar that closed before the tweet
        vals = []
        for s, b in bars.items():
            cc = grid_close(b, n)
            beta = bx.lagged_beta(cc[: r + 1], cb[: r + 1], window=BETA_WIN, lag=BETA_LAG, min_obs=BETA_MIN)
            e = bx.residual_return(cc[: r + 1], cb[: r + 1], beta, NRET)[-1]
            if np.isfinite(e):
                vals.append((float(e), s))
        vals.sort(reverse=True)
        rank = {s: i + 1 for i, (_, s) in enumerate(vals)}
        emit(
            f"[known] tweet {tid} at {iso(tt)} UTC: top-5 e12 at the {iso(GRID0 + r * H + H)} close: "
            + ", ".join(f"{s.removesuffix('USDT')} {v:+.3f}" for v, s in vals[:5])
            + "".join(f"; {s} rank {rank.get(s, '-')} of {len(vals)}" for s in names)
        )


def stage_zero(a: argparse.Namespace) -> None:
    bars, btc, qv, elig = load_all(a)
    emit(
        f"[zero] eligible {len(elig)}; coins with 1h bars (no BTC / stables) {len(bars)}; delisted before 2026-09 "
        f"{sum(1 for b in bars.values() if b.t[-1] < vsp.s1.ms('2026-09-01T00:00'))} (kept)"
    )
    uni = universe_days(qv, universe_syms(elig))
    sizes = [len(uni[d]) for d in uni if START_MS <= d < END_MS]
    cov = [len(uni[d] & set(bars)) / max(1, len(uni[d])) for d in uni if START_MS <= d < END_MS]
    emit(
        f"[zero] universe size per day min / median {min(sizes)} / {int(np.median(sizes))}; covered by the 1h cache median {np.median(cov):.1%}, min {min(cov):.1%}"
    )
    t0 = time.time()
    events, st = find_events(bars, btc, uni)
    emit(f"[zero] events {len(events)} ({st}); {time.time() - t0:.0f}s; rss {vsp.rss_mb():.0f} MB")
    trades, drop = make_trades(bars, events)
    zero_summary(bars, trades, drop)
    level_check(bars, btc)


# ---------------------------------------------------------------- run


def stage_run(a: argparse.Namespace, out: Path, data: tuple[Any, ...] | None = None) -> None:
    if data is None:
        bars, btc, qv, elig = load_all(a)
        uni = universe_syms(elig)
        events, _ = find_events(bars, btc, universe_days(qv, uni))
        demo = demo_days(qv)
    else:
        bars, btc, uni_days, demo = data
        events, _ = find_events(bars, btc, uni_days)
    trades, _ = make_trades(bars, events)
    tsy = trader_syms()
    rng = np.random.default_rng(SEED + 1)
    cols: dict[str, list[Any]] = defaultdict(list)
    pls: list[np.ndarray] = []
    for side in (1, -1):
        by: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(trades[side]):
            by[r[0]].append(i)
        for s, idx in sorted(by.items()):
            b = bars[s]
            rows = [(trades[side][i][1], trades[side][i][2], trades[side][i][3]) for i in idx]
            j = np.array([x[0] for x in rows], dtype=np.int64)
            en = np.array([x[1] for x in rows])
            stp = np.array([x[2] for x in rows])
            r, k_ex, kind = simulate(b, rows, side)
            r15, _, _ = simulate(b, rows, side, FEES.scaled(1.5))
            r20, _, _ = simulate(b, rows, side, FEES.scaled(2.0))
            t_in = b.t[j] + H
            t_ex = b.t[k_ex] + H
            f_t, f_r = vx4.load_funding(a.data_dir, s) if data is None else (np.zeros(0, dtype=np.int64), np.zeros(0))
            ru = x4.risk_unit(en, stp, FEES)
            sd = np.full(j.size, side)
            fund = x4.funding_r(t_in, t_ex, sd, en, ru, f_t, f_r)
            dist = side * (en - stp) / en
            pls.append(placebo_r(b, j, dist, side, N_PL, rng).astype(np.float32))
            prev = ((t_in - 1) // D) * D
            for key, val in (
                ("sym", [s] * j.size),
                ("side", sd),
                ("t_in", t_in),
                ("t_ex", t_ex),
                ("r_nf", r),
                ("r", r + fund),
                ("r15", r15 + fund),
                ("r20", r20 + fund),
                ("fund", fund),
                ("cover", x4.funding_covered(t_in, t_ex, f_t)),
                ("kind", kind),
                ("dist", dist),
                ("demo", [s in demo.get(int(d), ()) for d in prev]),
                ("trader", [s in tsy] * j.size),
            ):
                cols[key].extend(np.asarray(val).tolist())
        emit(f"  [run] {BRANCH[side]}: {len(trades[side])} trades; rss {vsp.rss_mb():.0f} MB")
    arr = {k: np.array(v) for k, v in cols.items()}
    np.savez_compressed(out / "run.npz", placebo=np.vstack(pls) if pls else np.zeros((0, N_PL)), **arr)
    emit(f"[run] done: {len(arr.get('r', []))} trades -> {out / 'run.npz'}")


# ---------------------------------------------------------------- report


def fmt(r: dict[str, float]) -> str:
    return f"n {int(r['n'])} (k {int(r['k'])}) mean {r['m']:+.4f}R CI [{r['lo']:+.4f}; {r['hi']:+.4f}] p2 {r['p']:.4f}"


def ci(v: np.ndarray, cl: np.ndarray) -> dict[str, float]:
    if v.size == 0:
        return {"n": 0, "k": 0, "m": math.nan, "lo": math.nan, "hi": math.nan, "p": math.nan, "se": math.nan}
    return mean_ci(v, cl, seed=SEED)


def one_sided(r: dict[str, float]) -> float:
    if not np.isfinite(r["p"]):
        return math.nan
    return r["p"] / 2 if r["m"] > 0 else 1 - r["p"] / 2


def stage_report(out: Path) -> None:
    z = np.load(out / "run.npz", allow_pickle=False)
    T = {k: z[k] for k in z.files}
    day, wk = T["t_in"] // D, vx4.week(T["t_in"])
    yr = np.array([time.gmtime(int(t) / 1000).tm_year for t in T["t_in"]])
    res: dict[int, dict[str, Any]] = {}
    for side in (1, -1):
        m = T["side"] == side
        emit(f"\n[{BRANCH[side]}] conditions:")
        rd, rw = ci(T["r"][m], day[m]), ci(T["r"][m], wk[m])
        emit(f"  primary, cluster day:  {fmt(rd)}")
        emit(f"  primary, cluster week: {fmt(rw)}")
        pl = T["placebo"][m].astype(float)
        dif = x4.paired_placebo_diff(T["r_nf"][m], pl)
        okd = np.isfinite(dif)
        rp = ci(dif[okd], day[m][okd])
        emit(f"  paired real - mean placebo (no funding), cluster day: {fmt(rp)}; trades without a placebo {int((~okd).sum())}")
        emit(f"  placebo mean R over sets (descriptive): {np.nanmean(pl):+.4f}")
        ri, ro = (
            ci(T["r"][m & (T["t_in"] < OOS_MS)], day[m & (T["t_in"] < OOS_MS)]),
            ci(T["r"][m & (T["t_in"] >= OOS_MS)], day[m & (T["t_in"] >= OOS_MS)]),
        )
        emit(f"  IS:  {fmt(ri)}")
        emit(f"  OOS: {fmt(ro)}")
        ys = {y: float(T["r"][m & (yr == y)].mean()) if (m & (yr == y)).any() else math.nan for y in YEARS}
        emit("  by year: " + ", ".join(f"{y} {v:+.4f} (n {int((m & (yr == y)).sum())})" for y, v in ys.items()))
        r15 = float(T["r15"][m].mean()) if m.any() else math.nan
        cov = m & T["cover"].astype(bool)
        rc = float(T["r"][cov].mean()) if cov.any() else math.nan
        emit(
            f"  costs x1.5 mean {r15:+.4f}; x2 mean {float(T['r20'][m].mean()) if m.any() else math.nan:+.4f}; full funding coverage mean {rc:+.4f} (n {int(cov.sum())})"
        )
        mde = Z_MDE * max(rd["se"], rw["se"])
        res[side] = {"d": rd, "w": rw, "p": rp, "is": ri, "oos": ro, "ys": ys, "r15": r15, "rc": rc, "mde": mde, "p1": one_sided(rd)}
    ph = holm([res[s]["p1"] if np.isfinite(res[s]["p1"]) else 1.0 for s in (1, -1)])
    for side, padj in zip((1, -1), ph, strict=True):
        x = res[side]
        cond = {
            "CI day lo > 0": x["d"]["lo"] > 0,
            "CI week lo > 0": x["w"]["lo"] > 0,
            "Holm p1 < 0.05": padj < 0.05,
            "placebo diff lo > 0": x["p"]["lo"] > 0,
            "IS > 0 and OOS > 0": x["is"]["m"] > 0 and x["oos"]["m"] > 0,
            ">= 4 of 6 years > 0": sum(1 for v in x["ys"].values() if v > 0) >= 4,
            "costs x1.5 > 0": x["r15"] > 0,
            "full funding coverage > 0": x["rc"] > 0,
        }
        if all(cond.values()):
            v = "CONFIRMED"
        elif x["d"]["m"] > 0 and x["p1"] < 0.05:
            v = "DIRECTIONAL"
        elif x["mde"] > INTEREST and not x["d"]["hi"] < 0:
            v = "UNDERPOWERED"
        else:
            v = "NOT CONFIRMED"
        emit(f"\n[verdict {BRANCH[side]}] {v}; Holm p {padj:.4f}; MDE80 {x['mde']:.4f}R (interest {INTEREST}R)")
        for k, ok in cond.items():
            emit(f"    {k}: {'yes' if ok else 'no'}")
    robustness(T, day)


def robustness(T: dict[str, np.ndarray], day: np.ndarray) -> None:
    emit("\n[robustness] (no effect on the verdict)")
    for side in (1, -1):
        m = T["side"] == side
        if not m.any():
            continue
        emit(f"  {BRANCH[side]}:")
        emit(f"    no funding:            {fmt(ci(T['r_nf'][m], day[m]))}")
        emit(f"    demo list:             {fmt(ci(T['r'][m & T['demo']], day[m & T['demo']]))}")
        emit(f"    trader's assets:       {fmt(ci(T['r'][m & T['trader']], day[m & T['trader']]))}")
        sy = T["sym"][m]
        tot = Counter()
        for s_, v in zip(sy.tolist(), T["r"][m].tolist(), strict=True):
            tot[s_] += v
        top = {s_ for s_, _ in tot.most_common(5)}
        ks = m & ~np.isin(T["sym"], list(top))
        emit(f"    without top-5 coins:   {fmt(ci(T['r'][ks], day[ks]))} (removed {sorted(top)})")
        dt = Counter()
        for d_, v in zip(day[m].tolist(), T["r"][m].tolist(), strict=True):
            dt[d_] += v
        kd = m & ~np.isin(day, [d_ for d_, _ in dt.most_common(5)])
        emit(f"    without top-5 days:    {fmt(ci(T['r'][kd], day[kd]))}")
        kinds = Counter(T["kind"][m].tolist())
        emit(
            f"    exits: stop {kinds.get(x4.KIND_STOP, 0)}, time {kinds.get(x4.KIND_TIME, 0)}; median stop distance {np.median(T['dist'][m]):.4f}"
        )
        is_ = T["t_in"][m] < OOS_MS
        q = pa.cluster_robust_q(T["r"][m], sy, day[m], min_n=30)
        emit(f"    5a heterogeneity (n >= 30): k {q['k']}, Q {q['q']:.1f}, df {q['df']}, p {q['p']}, I2 {q['i2']:.3f}")
        st = sorted(pa.asset_stats(T["r"][m], list(sy), min_n=30), key=lambda x: -x.n)[:10]
        emit("    5a largest: " + "; ".join(f"{x.asset.removesuffix('USDT')} n {x.n} {x.mean:+.3f}" for x in st))
        per = pa.persistence(T["r"][m], sy, day[m], is_, top_n=10, seed=SEED)
        emit(
            f"    5a persistence top-10 IS -> OOS: top {per['top_oos_mean']:+.4f} (n {per['n_top_oos']}) vs rest {per['rest_oos_mean']:+.4f}; "
            f"diff {per['diff']:+.4f} CI {per['diff_ci']} p {per['diff_p']}; rho {per['rho']} (n {per['rho_n']})"
        )
    both = np.zeros(T["t_in"].size, dtype=bool)
    for s_ in np.unique(T["sym"]):
        ii = np.flatnonzero(T["sym"] == s_)
        both[ii] = x4.overlaps_opposite(T["t_in"][ii], T["t_ex"][ii], T["side"][ii])
    emit(f"  trades overlapping an opposite trade of the same coin: {int(both.sum())} of {both.size}")


# ---------------------------------------------------------------- smoke


def synthetic(n_coins: int = 8, n: int = 24 * 220) -> tuple[dict[str, Bars], Bars, dict[int, set[str]]]:
    rng = np.random.default_rng(SEED)
    t = GRID0 + np.arange(n, dtype=np.int64) * H

    def walk(scale: float, beta: float, rb: np.ndarray) -> Bars:
        r = beta * rb + rng.standard_t(3, n) * scale
        c = 10 * np.exp(np.cumsum(r))
        o = np.concatenate([[c[0]], c[:-1]])
        hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, scale / 2, n)))
        lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, scale / 2, n)))
        return Bars(t, o, hi, lo, c, H)

    rb = rng.normal(0, 0.006, n)
    btc = walk(0.0, 1.0, rb)
    bars = {f"C{i}USDT": walk(0.012, 1.0 + 0.1 * i, rb) for i in range(n_coins)}
    uni = {int(d): set(bars) for d in range(GRID0 - D, GRID0 + n * H + D, D)}
    return bars, btc, uni


def smoke() -> None:
    global START_MS, END_MS, GRID1, OOS_MS  # noqa: PLW0603  (smoke: the synthetic window)
    bars, btc, uni = synthetic()
    START_MS, GRID1 = GRID0 + 35 * D, GRID0 + 220 * D
    END_MS, OOS_MS = GRID1 - 2 * D, GRID0 + 150 * D
    events, st = find_events(bars, btc, uni)
    trades, drop = make_trades(bars, events)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        with (out / "smoke.txt").open("w") as fh:
            vsp.OUT["fh"] = fh
            sys.stdout = open("/dev/null", "w")  # noqa: SIM115  (smoke: no outcome numbers on screen)
            try:
                zero_summary(bars, trades, drop)
                stage_run(argparse.Namespace(data_dir=out), out, (bars, btc, uni, {}))
                stage_report(out)
            finally:
                sys.stdout.close()
                sys.stdout = sys.__stdout__
                vsp.OUT["fh"] = None
        lines = (out / "smoke.txt").read_text().splitlines()
        assert sum(x.startswith("[verdict") for x in lines) == 2, "report incomplete"
        for key in ("paired real - mean placebo", "cluster week", "5a persistence", "a-priori MDE80", "ICC day"):
            assert any(key in x for x in lines), f"output lacks {key!r}"
    print(
        f"[smoke] synthetic: {len(bars)} coins, events {len(events)} of {st['events_all']}, "
        f"trades HC {len(trades[1])} HR {len(trades[-1])}, output lines {len(lines)}: ok"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("zero", "run", "report"))
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/bcx1_leader")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--limit", type=int, default=None)
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
            stage_report(out)
        else:
            ap.error("--stage or --smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
