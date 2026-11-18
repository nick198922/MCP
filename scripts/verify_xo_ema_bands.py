#!/usr/bin/env python3
"""Trader_XO's 12/25 EMA bands (weekly + daily) as a direction filter on the trades of S3, S16, S14 (setup passports)
and XO-1 D1 (IDEA-20261113-x-traders, item 5; registration: the card's section 5.2, written before any outcome).

    uv run python scripts/verify_xo_ema_bands.py --smoke             # synthetic bars, counts only
    uv run python scripts/verify_xo_ema_bands.py --stage coverage    # stage 0: band series + feature shares, no P&L
    uv run python scripts/verify_xo_ema_bands.py --stage full        # the registered test (<cache>/full.txt)

No network: 1h bars come from the passports' kline cache, the trades from the passport candidate files
(``verify_setup_passport.py S3|S16|S14 --stage run``) and the XO-1 rows (``verify_xo1_va_rotation.py --stage run``).
The band series of each symbol is written to ``<cache>/bands/<sym>.json`` once.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_setup_passport as vsp  # noqa: E402  (loaders, samples, chain of the passports)
import verify_xo1_va_rotation as vx  # noqa: E402  (XO-1 demo list, trader's assets, ISO weeks)

from tradedesk.analytics import ema_bands as eb  # noqa: E402
from tradedesk.analytics import s3_modules as m3  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.course_s14_ema import Bars, ema, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import chi2_sf, cochran_q  # noqa: E402

SEED = 20261118
N_BOOT = 10_000
N_PLACEBO_SUB = 2000
BASES = ("S3", "S16", "S14", "XO1")  # Holm family, m = 4 (registered)
INTEREST = 0.10  # R: the smallest lift of the "with both bands" trades over all trades worth a filter (registered)
Z_MDE = 2.241 + 0.842  # one-sided alpha 0.0125 (Holm's strictest step of 4) and 80 % power
SD_PRIOR = 1.0  # R per trade, a-priori MDE of stage 0 only
START_MS, OOS_MS = vsp.PASSPORT_MS, vsp.OOS_MS
KNOWN = (  # (date, tweet id, the author's words) for the BTC weekly band check of stage 0 (not an outcome)
    ("2024-11-25T12:00", "1860995847367909748", "Weekly 12/25 EMA = Bull, never fade this"),
    ("2026-09-19T12:00", "2101318111370592666", "expected weekly cross"),
)
emit = vsp.emit


# ---------------------------------------------------------------- band series per symbol


def band_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir = args
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    d, w = eb.daily_band(h1.t, h1.c), eb.weekly_band(h1.t, h1.c)
    daily = resample(h1, 24)
    tr = sp.trend_series(daily)  # the demo score's coin trend (close vs EMA34, EMA21 slope), for the overlap control
    res = {
        "d_end": d.end.tolist(),
        "d_dir": d.dir.tolist(),
        "w_end": w.end.tolist(),
        "w_dir": w.dir.tolist(),
        "tr_end": (daily.t + daily.bar_ms).tolist(),
        "tr": tr.tolist(),
    }
    (Path(out_dir) / "bands").mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "bands" / f"{s}.json").write_text(json.dumps(res, separators=(",", ":")))
    return {"sym": s, "rss": vsp.rss_mb()}


def stage_bands(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "bands" / f"{s}.json").exists()]
    emit(f"[bands] {len(jobs)} of {len(syms)} symbols to do, {a.workers} workers")
    t0 = time.time()
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for j, r in enumerate(pool.imap_unordered(band_symbol, jobs), 1):
            if j % 50 == 0 or j == len(jobs):
                emit(f"  [bands] {j}/{len(jobs)} {time.time() - t0:.0f}s; worker rss {r['rss']:.0f} MB; main {vsp.rss_mb():.0f} MB")


# ---------------------------------------------------------------- trades of the four bases


def years(t: np.ndarray) -> np.ndarray:
    return t.astype("datetime64[ms]").astype("datetime64[Y]").astype(np.int64) + 1970


def load_passport(a: argparse.Namespace, setup: str, qv: dict[str, Any], with_r: bool) -> dict[str, np.ndarray]:
    """The setup's trades after the demo selection and occupancy chain: ``prim`` its demo list (S3 top-100 >= $5M;
    S16 / S14 top-250 >= $1M, the demo's wide scan), ``comp`` the companion (S3 all >= $5M; S16 / S14 top-100 >= $5M)."""
    T = vsp.load_rows(a.passport_cache / setup)
    period = T["t_in"] >= vsp.PASSPORT_MS
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn = np.nan_to_num(T["turn"], nan=-1)
    top100 = vsp.in_prev_day_top(T, vsp.top_n_days(qv, vsp.TOP_N))
    if setup == "S3":
        prim = vsp.chain(T, period & demo & (turn >= 5e6) & top100)
        comp = vsp.chain(T, period & demo & (turn >= 5e6))
    else:
        top250 = vsp.in_prev_day_top(T, vsp.top_n_days(qv, vsp.WIDE_TOP_N))
        prim = vsp.chain(T, period & demo & (turn >= 1e6) & top250)
        comp = vsp.chain(T, period & demo & (turn >= 5e6) & top100)
    keep = prim | comp
    B = {
        "sym": T["sym"][keep].astype(str),
        "side": T["side"][keep].astype(str),
        "tf": T["tf"][keep].astype(str),
        "t_in": T["t_in"][keep],
        "week": T["week"][keep].astype(np.int64),
        "prim": prim[keep],
        "comp": comp[keep],
    }
    if with_r:
        B["R"], B["R15"] = T["B"][keep], T["B15"][keep]
    B["year"] = years(B["t_in"])
    return B


def load_xo1(a: argparse.Namespace, days: dict[int, set[str]], with_r: bool) -> dict[str, np.ndarray]:
    """XO-1 D1 trades (all have a full 1h path): ``prim`` the demo list (top-100 >= $5M), ``comp`` all coins."""
    cols: dict[str, list[Any]] = defaultdict(list)
    for p in sorted((a.xo1_cache / "rows").glob("*.json")):
        r = json.loads(p.read_text()).get("D1")
        if not r or not r["t_in"]:
            continue
        t = np.array(r["t_in"], dtype=np.int64)
        cols["sym"].extend([p.stem] * t.size)
        cols["t_in"].extend(r["t_in"])
        cols["side"].extend("long" if x > 0 else "short" for x in r["side"])
        cols["prim"].extend(vx.in_demo(p.stem, t, days).tolist())
        if with_r:
            cols["R"].extend(r["r"])
            cols["R15"].extend(r["r15"])
    B = {k: np.array(v) for k, v in cols.items()}
    B["t_in"] = B["t_in"].astype(np.int64)
    B["prim"] = B["prim"].astype(bool)
    B["comp"] = np.ones(B["t_in"].size, dtype=bool)
    B["tf"] = np.full(B["t_in"].size, "1h")
    B["week"] = vx.week(B["t_in"])
    B["year"] = years(B["t_in"])
    if with_r:
        B["R"], B["R15"] = B["R"].astype(float), B["R15"].astype(float)
    return B


def add_features(B: dict[str, np.ndarray], bands_dir: Path) -> None:
    """Weekly / daily band state, alignment with the trade and the score's coin trend at each entry (causal)."""
    n = B["t_in"].size
    w, d, tr = np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)
    for s in np.unique(B["sym"]):
        idx = np.flatnonzero(B["sym"] == s)
        p = bands_dir / f"{s}.json"
        if not p.exists():
            continue
        z = json.loads(p.read_text())
        t = B["t_in"][idx]
        w[idx] = eb.states_at(eb.Band(np.array(z["w_end"], dtype=np.int64), np.array(z["w_dir"], dtype=np.int64), eb.WEEK_MS), t)
        d[idx] = eb.states_at(eb.Band(np.array(z["d_end"], dtype=np.int64), np.array(z["d_dir"], dtype=np.int64), eb.DAY_MS), t)
        te = np.array(z["tr_end"], dtype=np.int64)
        j = np.searchsorted(te, t, side="right") - 1
        tv = np.array(z["tr"], dtype=np.int64)
        tr[idx] = np.where(j >= 0, tv[np.maximum(j, 0)], 0)
    want = np.where(B["side"] == "long", 1, -1)
    B["w"], B["d"] = w, d
    B["al"] = eb.alignments(B["side"], w, d)
    B["defined"] = B["al"] != "undef"
    B["with"] = B["al"] == "with"
    B["w_with"] = (w == want) & B["defined"]
    B["d_with"] = (d == want) & B["defined"]
    B["tr_with"] = tr == want
    B["is"], B["oos"] = B["t_in"] < OOS_MS, B["t_in"] >= OOS_MS
    B["strat"] = eb.strata_codes(B["side"], B["year"])  # side x calendar year (registered strata of the main lift)


def load_all(a: argparse.Namespace, with_r: bool) -> dict[str, dict[str, np.ndarray]]:
    t0 = time.time()
    elig = vsp.s1.eligible_syms(a.data_dir)
    qv_e = vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig)
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    days = vx.demo_days(vsp.s1.daily_qv(a.s1_cache, a.data_dir, syms))
    emit(f"[load] turnover lists {time.time() - t0:.0f}s; rss {vsp.rss_mb():.0f} MB")
    out: dict[str, dict[str, np.ndarray]] = {}
    for name in BASES:
        t1 = time.time()
        B = load_xo1(a, days, with_r) if name == "XO1" else load_passport(a, name, qv_e, with_r)
        B = {k: v[B["t_in"] >= START_MS] for k, v in B.items()}
        add_features(B, a.cache_dir / "bands")
        out[name] = B
        emit(f"[load] {name}: {B['t_in'].size} trades kept in {time.time() - t1:.0f}s; rss {vsp.rss_mb():.0f} MB")
    return out


# ---------------------------------------------------------------- stage 0 (no outcomes)


def share(m: np.ndarray, base: np.ndarray) -> str:
    k = int(base.sum())
    return f"{int(m.sum())} ({100 * m.sum() / k:.1f} %)" if k else "0"


def prior_mde(B: dict[str, np.ndarray], M: np.ndarray, weight: str = eb.WEIGHT) -> tuple[float, float]:
    """MDE80 of the stratified lift (side x year) with sd 1R: independent trades, and the week as the unit (all
    trades of a week perfectly correlated: design effect n / k). No outcomes used."""
    se = eb.stratified_prior_se(B["with"], M, B["strat"], SD_PRIOR, weight=weight)
    k = np.unique(B["week"][M]).size
    return Z_MDE * se, Z_MDE * se * math.sqrt(int(M.sum()) / max(k, 1))


def known_check(a: argparse.Namespace) -> None:
    h1, _ = vsp.load_1h_qv(a.s1_cache, a.data_dir, vsp.BTC)
    start, cl = eb.period_closes(h1.t, h1.c, eb.HOUR_MS, eb.WEEK_MS, eb.WEEK_OFFSET_MS)
    pos = segment_pos(start, max_gap_ms=2 * eb.WEEK_MS)
    e12, e25 = ema(cl, eb.FAST, pos), ema(cl, eb.SLOW, pos)
    end = start + eb.WEEK_MS
    for iso, tid, words in KNOWN:
        t = vsp.s1.ms(iso)
        j = int(np.searchsorted(end, t, side="right")) - 1
        emit(
            f"  BTCUSDT weekly at {iso[:10]} (`{tid}`, «{words}»): week closed {vsp.iso(int(end[j]))}, close {cl[j]:.0f}, "
            f"EMA12 {e12[j]:.0f}, EMA25 {e25[j]:.0f}, gap {(e12[j] / e25[j] - 1) * 100:+.2f} %"
        )


def stage_coverage(a: argparse.Namespace) -> None:
    out = a.cache_dir
    stage_bands(a, out)  # only the symbols without a band file (all with --force)
    emit(f"[coverage] band files {len(list((out / 'bands').glob('*.json')))}")
    known_check(a)
    data = load_all(a, with_r=False)
    for name, B in data.items():
        emit(f"\n== {name} ==")
        for lab, m0 in (("prim", B["prim"]), ("comp", B["comp"])):
            emit(
                f"  {lab}: n {int(m0.sum())}, coins {np.unique(B['sym'][m0]).size}, weeks {np.unique(B['week'][m0]).size}; "
                f"band defined {share(m0 & B['defined'], m0)}"
            )
            M = m0 & B["defined"]
            for per, pm in (("2021+", M), ("IS", M & B["is"]), ("OOS", M & B["oos"])):
                cnt = {k: share(pm & (B["al"] == k), pm) for k in ("with", "mixed", "against")}
                emit(f"    {per}: n {int(pm.sum())}; with {cnt['with']} · mixed {cnt['mixed']} · against {cnt['against']}")
            for sd in ("long", "short"):
                sm = M & (B["side"] == sd)
                emit(f"    {sd}: n {int(sm.sum())}; with {share(sm & B['with'], sm)}")
            for tf in np.unique(B["tf"][M]):
                tm = M & (B["tf"] == tf)
                emit(f"    {tf}: n {int(tm.sum())}; with {share(tm & B['with'], tm)}")
            if lab == "prim":
                yrs = ", ".join(
                    f"{y} {int((M & (B['year'] == y)).sum())}/{int((M & B['with'] & (B['year'] == y)).sum())}"
                    for y in np.unique(B["year"][M])
                )
                emit(f"    years (all / with): {yrs}")
                emit(
                    f"    overlap with the score's coin trend: trend agrees {share(M & B['tr_with'], M)}; "
                    f"with-bands among trend-agree {share(M & B['tr_with'] & B['with'], M & B['tr_with'])}; "
                    f"with-bands among trend-not {share(M & ~B['tr_with'] & B['with'], M & ~B['tr_with'])}"
                )
                emit(f"    weekly agrees (daily any) {share(M & B['w_with'], M)}; daily agrees (weekly any) {share(M & B['d_with'], M)}")
                st = eb.stratified_delta(np.zeros(B["t_in"].size), B["with"], M, B["strat"])  # zeros: strata counts only
                emit(f"    strata side x year used {int(st['used'])} of {int(st['total'])} (n {int(st['n'])}, with {int(st['n_a'])})")
                for wt in ("flag", "base"):
                    mi, mw = prior_mde(B, M, wt)
                    emit(
                        f"    a-priori MDE80 of the stratified lift, weights n_{'a,s' if wt == 'flag' else 's'}"
                        f"{' (registered)' if wt == eb.WEIGHT else ''} (sd 1R, 2021+): {mi:.3f}R independent trades, "
                        f"{mw:.3f}R week as unit (interest {INTEREST}R)"
                    )
                pi = Z_MDE * SD_PRIOR * math.sqrt(1 / max(int((M & B["with"]).sum()), 1) - 1 / int(M.sum()))
                pw = pi * math.sqrt(int(M.sum()) / max(np.unique(B["week"][M]).size, 1))
                emit(f"    a-priori MDE80 of the pooled lift (descriptive): {pi:.3f}R independent, {pw:.3f}R week as unit")
        emit(f"  rss {vsp.rss_mb():.0f} MB")


# ---------------------------------------------------------------- full run


def fmtd(r: dict[str, float]) -> str:
    return f"{r['m']:+.4f} [{r['lo']:+.4f}; {r['hi']:+.4f}]"


def delta(B: dict[str, np.ndarray], flag: np.ndarray, M: np.ndarray, key: str = "R") -> dict[str, float]:
    """Registered statistic: the lift stratified by side x year, CI / p by the weekly cluster bootstrap."""
    r = eb.stratified_delta_ci(B[key], flag, M, B["strat"], B["week"], n_boot=N_BOOT, seed=SEED)
    r["m"] = r["d"]
    r["p1"] = m3.one_sided_p(r["d"], r["p"]) if np.isfinite(r["d"]) else math.nan
    return r


def pooled(B: dict[str, np.ndarray], flag: np.ndarray, M: np.ndarray, key: str = "R") -> dict[str, float]:
    """Descriptive only: the pooled lift mean(R | flag) - mean(R | M) over sides and years."""
    r = sp.diff_ci(B[key], flag & M, M, B["week"], n_boot=N_BOOT, seed=SEED)
    r["m"] = r["d"]
    return r


def point(B: dict[str, np.ndarray], flag: np.ndarray, M: np.ndarray, key: str = "R") -> float:
    """Point stratified lift (side x year) on the subset M; NaN if no stratum has >= 2 flagged and >= 2 other."""
    return eb.stratified_delta(B[key], flag, M, B["strat"])["d"]


def mean_ci(B: dict[str, np.ndarray], m: np.ndarray, key: str = "R") -> dict[str, float]:
    ok = m & np.isfinite(B[key])
    return sp.mean_ci(B[key][ok], B["week"][ok], n_boot=N_BOOT, seed=SEED)


def placebo_p(B: dict[str, np.ndarray], flag: np.ndarray, M: np.ndarray, obs: float) -> tuple[float, float]:
    """Stratified placebo: random subgroups with the flagged count of every side x year stratum, same statistic."""
    ds = eb.stratified_placebo(B["R"], flag, M, B["strat"], n_rep=N_PLACEBO_SUB, seed=SEED)
    ds = ds[np.isfinite(ds)]
    return (1 + np.sum(ds >= obs)) / (1 + ds.size), float(np.quantile(ds, 0.95))


def drop_top(B: dict[str, np.ndarray], flag: np.ndarray, M: np.ndarray, k: int = 5) -> tuple[np.ndarray, list[str]]:
    """The k coins with the largest contribution to the lift: sum over their flagged trades of R minus the mean R of
    the trade's side x year stratum."""
    ok = M & np.isfinite(B["R"])
    S = int(B["strat"].max()) + 1
    sm = np.bincount(B["strat"][ok], B["R"][ok], S) / np.maximum(np.bincount(B["strat"][ok], minlength=S), 1)
    contrib = np.where(flag & ok, B["R"] - sm[B["strat"]], 0.0)
    by: dict[str, float] = defaultdict(float)
    for s, c in zip(B["sym"][M], contrib[M], strict=True):
        by[str(s)] += float(c)
    top = [s for s, _ in sorted(by.items(), key=lambda x: -x[1])[:k]]
    return M & ~np.isin(B["sym"], top), top


def per_asset(B: dict[str, np.ndarray], flag: np.ndarray, M: np.ndarray) -> str:
    """5a: per-coin contrast E[R | with] - E[R | not with] (2021+), Cochran Q / I2; IS top-10 coins -> OOS."""

    def contrast(m: np.ndarray) -> tuple[float, float]:
        a, b = B["R"][m & flag], B["R"][m & ~flag]
        if a.size < 5 or b.size < 5:
            return math.nan, math.nan
        return float(a.mean() - b.mean()), math.sqrt(a.var(ddof=1) / a.size + b.var(ddof=1) / b.size)

    rows = [contrast(M & (B["sym"] == s)) for s in np.unique(B["sym"][M])]
    mu = np.array([r[0] for r in rows])
    se = np.array([r[1] for r in rows])
    q, df, i2 = cochran_q(mu, se)
    is_c = {str(s): contrast(M & B["is"] & (B["sym"] == s))[0] for s in np.unique(B["sym"][M & B["is"]])}
    is_c = {s: v for s, v in is_c.items() if np.isfinite(v)}
    top = [s for s, _ in sorted(is_c.items(), key=lambda x: -x[1])[:10]]
    o = M & B["oos"]
    ti, tr = contrast(o & np.isin(B["sym"], top)), contrast(o & ~np.isin(B["sym"], top))
    return (
        f"coins {int(np.isfinite(mu).sum())}, Cochran Q {q:.1f} (df {df}, p {chi2_sf(q, df):.3g}), I² {i2:.2f}; "
        f"IS top-10 {top} -> OOS contrast {ti[0]:+.3f} (SE {ti[1]:.3f}) vs rest {tr[0]:+.3f} (SE {tr[1]:.3f})"
    )


def verdict_label(ok: bool, r: dict[str, float]) -> str:
    se = (r["hi"] - r["lo"]) / 3.92
    mde = Z_MDE * se
    if ok:
        return f"✓ пройшов (MDE₈₀ {mde:.3f})"
    if r["hi"] < INTEREST:
        return f"✗ не підтверджено, корисний приріст (≥ {INTEREST:g}R) виключено (MDE₈₀ {mde:.3f})"
    if mde > INTEREST:
        return f"◯ невимірно (MDE₈₀ {mde:.3f} > {INTEREST:g}R)"
    return f"✗ не підтверджено (MDE₈₀ {mde:.3f})"


def stage_full(a: argparse.Namespace) -> None:
    data = load_all(a, with_r=True)
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    tsy = vx.trader_syms(set(syms))
    res = {name: analyse(name, B, tsy) for name, B in data.items()}
    verdicts(res)


def verdicts(res: dict[str, dict[str, Any]]) -> None:
    adj = holm([res[n]["main"]["p1"] for n in res])
    emit(f"\n== verdict (Holm m = {len(res)}) ==")
    for name, p_h in zip(res, adj, strict=True):
        r = res[name]
        ok = r["main"]["m"] > 0 and p_h < 0.05 and r["p_pl"] < 0.05 and all(np.isfinite(v) and v > 0 for v in r["cells"].values())
        idea = "торгова ідея" if r["e15"]["m"] > 0 else "менший збиток"
        emit(
            f"  {name}: Δ {fmtd(r['main'])}, Holm p {p_h:.4f}, placebo p {r['p_pl']:.4f} -> {verdict_label(ok, r['main'])}"
            + (f" ({idea})" if ok else "")
        )


def analyse(name: str, B: dict[str, np.ndarray], tsy: list[str]) -> dict[str, Any]:
    emit(f"\n== {name} ==")
    M = B["prim"] & B["defined"] & np.isfinite(B["R"])
    A = B["with"]
    main = delta(B, A, M)
    p_pl, q95 = placebo_p(B, A, M, main["m"])
    cells = {
        "IS": point(B, A, M & B["is"]),
        "OOS": point(B, A, M & B["oos"]),
        "2025": point(B, A, M & B["oos"] & (B["year"] == 2025)),
        "2026": point(B, A, M & B["oos"] & (B["year"] == 2026)),
        "long": point(B, A, M & (B["side"] == "long")),
        "short": point(B, A, M & (B["side"] == "short")),
    }
    Mx, top = drop_top(B, A, M)
    cells["no top-5"] = point(B, A, Mx)
    C = B["comp"] & B["defined"] & np.isfinite(B["R"])
    cells["comp"] = point(B, A, C)
    e15 = mean_ci(B, A & M & B["oos"], "R15")
    res = {"main": main, "p_pl": p_pl, "cells": cells, "e15": e15}
    emit(
        f"  main 2021+ (Δ stratified side x year): {fmtd(main)}, strata {int(main['used'])}/{int(main['total'])}, "
        f"n with {int(main['n_a'])} of {int(main['n'])} (base {int(M.sum())}), p1 {main['p1']:.4f}"
    )
    emit(f"  placebo (stratified side x year, {N_PLACEBO_SUB} subgroups): p {p_pl:.4f}, 95th pct {q95:+.4f}")
    emit("  cells (point stratified Δ): " + " · ".join(f"{k} {v:+.4f}" for k, v in cells.items()) + f"; top-5 dropped {top}")
    emit(f"  descriptive: pooled Δ (no strata) {fmtd(pooled(B, A, M))}")
    emit(
        "  descriptive: Δ by year (stratified by side): "
        + " · ".join(
            f"{y} {point(B, A, M & (B['year'] == y)):+.4f} (n {int((M & (B['year'] == y)).sum())})" for y in np.unique(B["year"][M])
        )
    )
    for lab, m in (("all", M), ("with", A & M), ("mixed", M & (B["al"] == "mixed")), ("against", M & (B["al"] == "against"))):
        emit(f"  E[R] {lab}: 2021+ {fmtd(mean_ci(B, m))} n {int(m.sum())}; OOS {fmtd(mean_ci(B, m & B['oos']))}")
    emit(f"  E[R] with, OOS, costs x1.5: {fmtd(e15)}")
    emit("  exploratory (outside Holm):")
    for lab, f in (("weekly agrees (daily any)", B["w_with"]), ("daily agrees (weekly any)", B["d_with"])):
        emit(f"    {lab}: Δ {fmtd(delta(B, f, M))}; OOS {point(B, f, M & B['oos']):+.4f}")
    tw = M & B["tr_with"]
    emit(
        f"    within score-trend-agree trades: Δ {fmtd(delta(B, A, tw))} (n {int(tw.sum())}); within trend-not: {point(B, A, M & ~B['tr_with']):+.4f}"
    )
    ts = M & np.isin(B["sym"], tsy)
    emit(f"    trader's assets: Δ {point(B, A, ts):+.4f} (n {int(ts.sum())}, with {int((ts & A).sum())})")
    if name != "XO1":
        for tf in ("4h", "1d"):
            emit(f"    {tf}: Δ {point(B, A, M & (B['tf'] == tf)):+.4f} (n {int((M & (B['tf'] == tf)).sum())})")
    emit(f"  5a: {per_asset(B, A, M)}")
    emit(f"  rss {vsp.rss_mb():.0f} MB")
    return res


# ---------------------------------------------------------------- smoke


def smoke() -> None:
    rng = np.random.default_rng(1)
    n = 24 * 900
    t = vsp.s1.ms("2023-01-02T00:00") + eb.HOUR_MS * np.arange(n, dtype=np.int64)
    c = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    b = Bars(t, c, c, c, c, eb.HOUR_MS)
    w, d = eb.weekly_band(b.t, b.c), eb.daily_band(b.t, b.c)
    q = np.sort(rng.integers(t[0], t[-1], 2000))
    sides = np.where(rng.random(q.size) < 0.5, "long", "short")
    al = eb.alignments(sides, eb.states_at(w, q), eb.states_at(d, q))
    emit(
        f"[smoke] weeks {w.end.size}, days {d.end.size}; trades {q.size}: "
        + ", ".join(f"{k} {int((al == k).sum())}" for k in ("with", "mixed", "against", "undef"))
    )
    # the statistics of the full run on synthetic trades (random R, not market outcomes): the code path only
    syms = np.array([f"C{i}USDT" for i in rng.integers(0, 12, q.size)])
    r = rng.normal(0, 1, q.size)
    want = np.where(sides == "long", 1, -1)
    ws, ds = eb.states_at(w, q), eb.states_at(d, q)
    B: dict[str, np.ndarray] = {
        "sym": syms,
        "side": sides,
        "tf": np.full(q.size, "4h"),
        "t_in": q,
        "week": q // eb.WEEK_MS,
        "year": years(q),
        "prim": np.ones(q.size, dtype=bool),
        "comp": rng.random(q.size) < 0.7,
        "R": r,
        "R15": r - 0.02,
        "w": ws,
        "d": ds,
        "al": al,
        "defined": al != "undef",
        "with": al == "with",
        "w_with": (ws == want) & (al != "undef"),
        "d_with": (ds == want) & (al != "undef"),
        "tr_with": rng.random(q.size) < 0.5,
        "is": q < OOS_MS,
        "oos": q >= OOS_MS,
    }
    B["strat"] = eb.strata_codes(B["side"], B["year"])
    verdicts({"SYN": analyse("SYN", B, ["C1USDT", "C2USDT"])})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("coverage", "full"))
    ap.add_argument("--smoke", action="store_true")
    home = Path.home() / ".cache/tradedesk-agents"
    ap.add_argument("--cache-dir", type=Path, default=home / "xo_ema_bands")
    ap.add_argument("--passport-cache", type=Path, default=home / "setup_passport")
    ap.add_argument("--xo1-cache", type=Path, default=home / "xo1_va_rotation")
    ap.add_argument("--s1-cache", type=Path, default=home / "course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=home / "funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.smoke:
        smoke()
        return 0
    if not a.stage:
        ap.error("--stage or --smoke")
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    with (a.cache_dir / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        t0 = time.time()
        if a.stage == "coverage":
            stage_coverage(a)
        else:
            stage_full(a)
        emit(f"[done] {time.time() - t0:.0f}s; peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
