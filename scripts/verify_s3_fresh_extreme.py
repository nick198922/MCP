#!/usr/bin/env python3
"""Item 10a of IDEA-20261030-demo-setups-depth: S3 sweeps of a "fresh" range edge (the edge itself is the previous
UTC day / ISO week / calendar month extreme) against edges farther than 0.3 ATR from such levels, on data the S3
passport and the S3 modules never used: D1 = 2020 on 4h / 1d (passport candidates + ``s3_modules`` rows) and D2 =
2h / 8h / 12h aggregated from the 1h cache (2021-01 .. 2026-09). Registration: the card's section «Попередня
реєстрація 10a» (written and ``git add``-ed before any run that computes R).

    uv run python scripts/verify_s3_fresh_extreme.py --stage sanity   # 4h / 1d reproduces the passport and H2 rows
    uv run python scripts/verify_s3_fresh_extreme.py --stage run      # per-symbol 2h / 8h / 12h candidates (4 workers)
    uv run python scripts/verify_s3_fresh_extreme.py --stage report   # tables (also <cache>/report.txt)

No network: the 1h klines come from the cache of ``verify_course_s14_ema.py``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_setup_passport as vsp  # noqa: E402  (loaders, demo selection, chain, CI helpers of the S3 passport)

from tradedesk.analytics import s3_modules as mo  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import chi2_sf, cochran_q, simulate_stop_rule  # noqa: E402
from tradedesk.execution.orders import reward_risk_at  # noqa: E402  (read-only: the guard's R:R after fees)

DAY_MS = 24 * HOUR_MS
NEW_TFS = ("2h", "8h", "12h")
HOURS = {"2h": 2, "4h": 4, "8h": 8, "12h": 12, "1d": 24}
HOLD_BARS = 42  # the 4h rule (7 d = 42 bars of 4h), registered for 2h / 8h / 12h
HOLD = {tf: HOLD_BARS * h * HOUR_MS for tf, h in HOURS.items() if tf in NEW_TFS} | dict(vsp.HOLD)
END = {"2h": vsp.s1.ms("2026-09-26T00:00"), "8h": vsp.s1.ms("2026-09-16T00:00"), "12h": vsp.s1.ms("2026-09-09T00:00")} | dict(vsp.END)
D2_START = vsp.PASSPORT_MS
N_PLACEBO = 2000
emit = vsp.emit


def key_of(sym: str, tf: str, side: str, t_in: int) -> str:
    return f"{sym}|{tf}|{side}|{int(t_in)}"


# ---------------------------------------------------------------- per-symbol candidates with the edge distance


def run_symbol(args: tuple[str, str, str, str, str, tuple[str, ...]]) -> dict[str, Any]:
    s, s1_cache, data_dir, passport_dir, out_dir, tfs = args
    t0 = time.time()
    s1c, dd, out = Path(s1_cache), Path(data_dir), Path(out_dir)
    det = vsp.SETUPS["S3"][0]
    member = vsp.s1.Membership(vsp.s1.members(s1c))
    btc = json.loads((Path(passport_dir) / "btc_trend.json").read_text())
    btc_t = np.asarray(btc["t"], dtype=np.int64) + DAY_MS  # close times
    btc_tr = np.asarray(btc["trend"], dtype=np.int64)
    h1, qv = vsp.load_1h_qv(s1c, dd, s)
    daily = resample(h1, 24)
    d_tr = sp.trend_series(daily)
    rows: list[dict[str, Any]] = []
    for tf in tfs:
        tb = resample(h1, HOURS[tf])
        oriented = {"long": tb, "short": tb.mirrored()}
        atrs = {sd: atr(b, 14, segment_pos(b.t)) for sd, b in oriented.items()}
        for sig in det(tb, tf):
            t_in = sig.signal_ts + tb.bar_ms
            if not (vsp.START_MS <= t_in < END[tf]):
                continue
            top = member(s, t_in)
            turn = sp.trailing_turnover(t_in, h1.t, qv)
            grp = sp.turnover_group(top, turn)
            if grp in (sp.LT1M, sp.UNKNOWN):
                continue
            sg = 1.0 if sig.side == "long" else -1.0
            e, st, tg = sg * sig.entry, sg * sig.stop, sg * sig.target
            if not (st < e < tg):
                continue
            path = h1 if sig.side == "long" else h1.mirrored()
            b = simulate_stop_rule(path, t_in, e, st, tg, HOLD[tf], vsp.RULE_B, fees=vsp.F1)
            if b is None:
                continue
            b15 = simulate_stop_rule(path, t_in, e, st, tg, HOLD[tf], vsp.RULE_B, fees=vsp.F15)
            j = int(np.searchsorted(btc_t, t_in, side="right")) - 1
            rr = reward_risk_at(sig.side, sig.entry, sig.stop, sig.target)
            # the edge distance exactly as verify_s3_modules.run_symbol (H2)
            ind = sig.indicators
            edge = ind["range_low"] if sig.side == "long" else -ind["range_high"]
            bb = oriented[sig.side]
            i = int(np.searchsorted(bb.t, sig.signal_ts))
            a_prev = float(atrs[sig.side][i - 1]) if i >= 1 else math.nan
            _, dist = mo.htf_match(edge, mo.htf_lows(path.t, path.low, sig.signal_ts), a_prev)
            rows.append(
                {
                    "sym": s,
                    "tf": tf,
                    "side": sig.side,
                    "t_in": t_in,
                    "top30": top,
                    "turn": turn,
                    "grp": grp,
                    "rr": rr,
                    "stop_pct": abs(sig.entry - sig.stop) / abs(sig.entry),
                    "score": sp.score_points(sig.side, rr, sp.trend_at(daily, d_tr, t_in), int(btc_tr[j]) if j >= 0 else 0, s == vsp.BTC),
                    "B": b.r_net,
                    "B_k": b.exit_kind,
                    "B_x": b.exit_ms,
                    "B15": b15.r_net if b15 else None,
                    "d": None if not np.isfinite(dist) else round(dist, 4),
                }
            )
    (out / "cands").mkdir(parents=True, exist_ok=True)
    (out / "cands" / f"{s}.json").write_text(json.dumps({"rows": rows}, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "sec": time.time() - t0, "rss": vsp.rss_mb()}


def run_pool(a: argparse.Namespace, out: Path, syms: list[str], tfs: tuple[str, ...]) -> None:
    jobs = [
        (s, str(a.s1_cache), str(a.data_dir), str(a.passport_cache / "S3"), str(out), tfs)
        for s in syms
        if a.force or not (out / "cands" / f"{s}.json").exists()
    ]
    emit(f"[run] {tfs}: {len(jobs)} of {len(syms)} symbols to do, {a.workers} workers")
    t0, n = time.time(), 0
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for k, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            n += r["n"]
            if k % 25 == 0 or k == len(jobs):
                el = time.time() - t0
                emit(
                    f"  [run] {k}/{len(jobs)} {el:.0f}s eta {el / k * (len(jobs) - k):.0f}s; rows {n}; worker rss {r['rss']:.0f} MB; main {vsp.rss_mb():.0f} MB"
                )
    emit(f"[run] done in {time.time() - t0:.0f}s; rows {n}")


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    run_pool(a, out, syms, NEW_TFS)
    # data, not results: share of each edge class per timeframe (no R)
    T = vsp.load_rows(out)
    cls = classes(T["d"])
    for tf in NEW_TFS:
        m = T["tf"] == tf
        emit(
            f"  {tf}: candidates {int(m.sum())}; fresh {np.mean(cls[m] == mo.FRESH):.3f}, near {np.mean(cls[m] == mo.NEAR):.3f}, far {np.mean(cls[m] == mo.FAR):.3f}, undefined {np.mean(cls[m] == ''):.3f}"
        )


def stage_sanity(a: argparse.Namespace, out: Path) -> None:
    """4h / 1d through this script's worker for a few symbols: R and B15 must equal the passport's candidates, the edge
    distance must equal the ``s3_modules`` H2 rows; 2020 entries in the H2 sample P must be 0."""
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT"]
    tmp = out / "sanity"
    a.force = True
    run_pool(a, tmp, syms, ("4h", "1d"))
    mine = {
        key_of(r["sym"], r["tf"], r["side"], r["t_in"]): r
        for s in syms
        for r in json.loads((tmp / "cands" / f"{s}.json").read_text())["rows"]
    }
    pas: dict[str, Any] = {}
    for s in syms:
        for r in json.loads((a.passport_cache / "S3" / "cands" / f"{s}.json").read_text())["rows"]:
            pas[key_of(r["sym"], r["tf"], r["side"], r["t_in"])] = r
    h2: dict[str, Any] = {}
    for s in syms:
        for r in json.loads((a.modules_cache / "rows" / f"{s}.json").read_text()):
            h2[r["k"]] = r
    both = sorted(set(mine) & set(pas))
    eq_b = sum(abs(mine[k]["B"] - pas[k]["B"]) < 1e-9 for k in both)
    eq_b15 = sum(
        (mine[k]["B15"] is None and pas[k]["B15"] is None)
        or (mine[k]["B15"] is not None and pas[k]["B15"] is not None and abs(mine[k]["B15"] - pas[k]["B15"]) < 1e-9)
        for k in both
    )
    eq_sc = sum(mine[k]["score"] == pas[k]["score"] and abs(mine[k]["rr"] - pas[k]["rr"]) < 1e-12 for k in both)
    hb = sorted(set(mine) & set(h2))
    eq_d = sum(mine[k]["d"] == h2[k]["H2d"] for k in hb)
    emit(
        f"sanity 4h/1d on {len(syms)} symbols: mine {len(mine)}, passport {len(pas)}, common {len(both)}; B equal {eq_b}, B15 equal {eq_b15}, score+rr equal {eq_sc}"
    )
    emit(f"  edge distance vs s3_modules rows: common {len(hb)}, equal {eq_d}")
    T = vsp.load_rows(a.passport_cache / "S3")
    period = T["t_in"] >= vsp.PASSPORT_MS
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    P = vsp.chain(T, period & demo & turn5)
    emit(
        f"  H2 sample P (as verify_s3_modules.samples): {int(P.sum())} trades, entries before 2021-01-01: {int((P & (T['t_in'] < vsp.PASSPORT_MS)).sum())}"
    )
    emit(
        f"  passport candidates with entry in 2020: {int((T['t_in'] < vsp.PASSPORT_MS).sum())} (4h {int(((T['t_in'] < vsp.PASSPORT_MS) & (T['tf'] == '4h')).sum())}, 1d {int(((T['t_in'] < vsp.PASSPORT_MS) & (T['tf'] == '1d')).sum())})"
    )
    sys1 = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    emit(f"  symbols with 1h cache: {len(sys1)}")


# ---------------------------------------------------------------- statistics


def classes(d: np.ndarray) -> np.ndarray:
    return np.array([mo.edge_class(None if x is None else float(x)) or "" for x in d], dtype=object)


def years_of(T: dict[str, np.ndarray], m: np.ndarray, t0: int, t1: int) -> float:
    return (t1 - t0) / (365.25 * DAY_MS)


def contrast(T: dict[str, np.ndarray], cls: np.ndarray, m: np.ndarray, key: str = "B", cl: str = "week") -> dict[str, float]:
    r = sp.diff_ci(T[key], m & (cls == mo.FRESH), m & (cls == mo.FAR), T[cl], n_boot=vsp.N_BOOT, seed=vsp.SEED)
    r["p1"] = mo.one_sided_p(r["d"], r["p"])
    r["se"] = (r["hi"] - r["lo"]) / 3.92
    r["mde"] = 2.8 * r["se"]
    r["nf"] = float((m & (cls == mo.FRESH)).sum())
    r["nr"] = float((m & (cls == mo.FAR)).sum())
    return r


def placebo_p(T: dict[str, np.ndarray], cls: np.ndarray, m: np.ndarray, obs: float) -> float:
    """Share of label permutations (within timeframe x year, same fresh share) whose contrast >= the observed one."""
    idx = np.flatnonzero(m & ((cls == mo.FRESH) | (cls == mo.FAR)))
    v = T["B"][idx]
    lab = cls[idx] == mo.FRESH
    strata: dict[tuple[str, int], list[int]] = defaultdict(list)
    for j, (tf, y) in enumerate(zip(T["tf"][idx], T["year"][idx], strict=True)):
        strata[(str(tf), int(y))].append(j)
    groups = [np.array(g) for g in strata.values()]
    rng = np.random.default_rng(vsp.SEED)
    nf = lab.sum()
    ge = 0
    for _ in range(N_PLACEBO):
        pl = np.zeros(lab.size, dtype=bool)
        for g in groups:
            k = int(lab[g].sum())
            if k:
                pl[rng.choice(g, k, replace=False)] = True
        d = v[pl].sum() / nf - v[~pl].sum() / (lab.size - nf)
        ge += d >= obs
    return (ge + 1) / (N_PLACEBO + 1)


def fmtc(r: dict[str, float]) -> str:
    return f"{r['d']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] p₁ {r['p1']:.4f}"


def fmtm(r: dict[str, float]) -> str:
    return f"n {int(r['n'])}, E[R] {r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}]"


def fresh_profit(T: dict[str, np.ndarray], cls: np.ndarray, m: np.ndarray, yrs: float, label: str) -> dict[str, float]:
    f = m & (cls == mo.FRESH)
    r1, r15 = vsp.ci(T, f), vsp.ci(T, f, "B15")
    emit(
        f"  {label}: свіжі {fmtm(r1)}; ×1.5 {r15['m']:+.3f} [{r15['lo']:+.3f}; {r15['hi']:+.3f}]; угод / рік {r1['n'] / yrs:.0f}; сума R / рік {np.nansum(T['B'][f]) / yrs:+.1f} (×1.5 {np.nansum(T['B15'][f]) / yrs:+.1f})"
    )
    return {"m": r1["m"], "lo15": r15["lo"], "m15": r15["m"], "n": r1["n"]}


def breakdown(T: dict[str, np.ndarray], cls: np.ndarray, m: np.ndarray, label: str) -> None:
    def cell(mm: np.ndarray) -> str:
        f, r = mm & (cls == mo.FRESH), mm & (cls == mo.FAR)
        nr = mm & (cls == mo.NEAR)
        fm = np.mean(T["B"][f]) if f.any() else math.nan
        rm = np.mean(T["B"][r]) if r.any() else math.nan
        nm = np.mean(T["B"][nr]) if nr.any() else math.nan
        return f"свіжі n {int(f.sum())} {fm:+.3f}; поруч n {int(nr.sum())} {nm:+.3f}; далекі n {int(r.sum())} {rm:+.3f}; Δ {fm - rm:+.3f}"

    emit(f"  {label} розбивки (E[R] net ×1):")
    for tf in sorted(set(T["tf"][m]), key=lambda x: HOURS[str(x)]):
        emit(f"    {tf}: {cell(m & (T['tf'] == tf))}")
    for sd in ("long", "short"):
        emit(f"    {sd}: {cell(m & (T['side'] == sd))}")
    for y in sorted(set(int(x) for x in T["year"][m])):
        emit(f"    {y}: {cell(m & (T['year'] == y))}")


def drop_top5(T: dict[str, np.ndarray], cls: np.ndarray, m: np.ndarray) -> tuple[np.ndarray, list[str]]:
    by: dict[str, float] = defaultdict(float)
    f = m & (cls == mo.FRESH)
    for s, v in zip(T["sym"][f], T["B"][f], strict=True):
        by[str(s)] += float(v)
    top = [s for s, _ in sorted(by.items(), key=lambda x: -x[1])[:5]]
    return m & ~np.isin(T["sym"], top), top


def per_asset(T: dict[str, np.ndarray], cls: np.ndarray, m: np.ndarray, IS: np.ndarray, OOS: np.ndarray) -> None:
    f = m & (cls == mo.FRESH)
    means, ses = [], []
    for s in np.unique(T["sym"][f]):
        v = T["B"][f & (T["sym"] == s)]
        if v.size >= 5:
            means.append(v.mean())
            ses.append(v.std(ddof=1) / math.sqrt(v.size))
    q, df, i2 = cochran_q(np.array(means), np.array(ses))
    is_m = {
        str(s): float(np.mean(T["B"][f & IS & (T["sym"] == s)]))
        for s in np.unique(T["sym"][f & IS])
        if np.sum(f & IS & (T["sym"] == s)) >= 5
    }
    top = [s for s, _ in sorted(is_m.items(), key=lambda x: -x[1])[:10]]
    inn, rest = f & OOS & np.isin(T["sym"], top), f & OOS & ~np.isin(T["sym"], top)
    emit(
        f"  5a свіжі D2: монет (≥ 5 угод) {len(means)}, Cochran Q {q:.1f} (df {df}, p {chi2_sf(q, df):.3f}), I² {i2:.2f}; топ-10 IS → OOS {np.mean(T['B'][inn]) if inn.any() else math.nan:+.3f} (n {int(inn.sum())}) проти решти {np.mean(T['B'][rest]):+.3f} (n {int(rest.sum())})"
    )
    # exploratory (after the first report): coins with near-constant R get SE -> 0 and dominate Q; the same Q with
    # the pooled SD of fresh trades (SE = pooled SD / sqrt(n)), and the top-10 contrast with a week-cluster CI
    sd = float(np.std(T["B"][f], ddof=1))
    ns = [int(np.sum(f & (T["sym"] == s))) for s in np.unique(T["sym"][f])]
    ses_p = np.array([sd / math.sqrt(n) for n in ns if n >= 5])
    q2, df2, i22 = cochran_q(np.array(means), ses_p)
    c = sp.diff_ci(T["B"], inn, rest, T["week"], n_boot=vsp.N_BOOT, seed=vsp.SEED)
    emit(
        f"  5a (розвідувально): Q з загальним SD {q2:.1f} (df {df2}, p {chi2_sf(q2, df2):.3f}), I² {i22:.2f}; топ-10 IS − решта на OOS {c['d']:+.3f} [{c['lo']:+.3f}; {c['hi']:+.3f}]"
    )


def overlap_mask(T2: dict[str, np.ndarray], P: dict[str, np.ndarray], after_ms: int = DAY_MS) -> np.ndarray:
    """D2 rows with a passport S3 4h / 1d candidate on the same coin and side entering in [t - 24 h, t + ``after_ms``].
    The registered +-24 h window reads candidates after the D2 entry (a later sweep of the same edge usually means
    the D2 trade was stopped), so removing those rows is a look-ahead filter; ``after_ms=0`` is the causal version
    (exploratory, added after the first report)."""
    idx: dict[tuple[str, str], np.ndarray] = {}
    for key in set(zip(P["sym"], P["side"], strict=True)):
        mm = (P["sym"] == key[0]) & (P["side"] == key[1])
        idx[(str(key[0]), str(key[1]))] = np.sort(P["t_in"][mm])
    out = np.zeros(T2["t_in"].size, dtype=bool)
    for j, (s, sd, t) in enumerate(zip(T2["sym"], T2["side"], T2["t_in"], strict=True)):
        arr = idx.get((str(s), str(sd)))
        if arr is not None and arr.size:
            k = int(np.searchsorted(arr, t - DAY_MS, side="left"))
            out[j] = k < arr.size and arr[k] <= t + after_ms
    return out


def stage_report(a: argparse.Namespace, out: Path) -> None:
    # ---- D1: 2020 on 4h / 1d
    T = vsp.load_rows(a.passport_cache / "S3")
    T["key"] = np.array(
        [key_of(str(s), str(tf), str(sd), int(t)) for s, tf, sd, t in zip(T["sym"], T["tf"], T["side"], T["t_in"], strict=True)]
    )
    h2d: dict[str, Any] = {}
    for p in sorted((a.modules_cache / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            h2d[r["k"]] = r.get("H2d")
    T["d"] = np.array([h2d.get(k) for k in T["key"]], dtype=object)
    T["day"] = T["t_in"] // DAY_MS
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    y2020 = (T["t_in"] >= vsp.START_MS) & (T["t_in"] < vsp.PASSPORT_MS)
    D1 = vsp.chain(T, y2020 & demo & turn5)
    c1 = classes(T["d"])
    emit(
        f"# S3 fresh extreme (10a). D1 = 2020 4h/1d: {int(D1.sum())} trades (4h {int((D1 & (T['tf'] == '4h')).sum())}, 1d {int((D1 & (T['tf'] == '1d')).sum())}); without an H2 row {int(sum(1 for k in T['key'][D1] if k not in h2d))}"
    )
    emit(
        f"  класи D1: свіжі {int((D1 & (c1 == mo.FRESH)).sum())}, поруч {int((D1 & (c1 == mo.NEAR)).sum())}, далекі {int((D1 & (c1 == mo.FAR)).sum())}, без рівня {int((D1 & (c1 == '')).sum())}"
    )
    emit(f"  база D1 (усі угоди): {fmtm(vsp.ci(T, D1))}")
    t1 = contrast(T, c1, D1)
    t1d = contrast(T, c1, D1, cl="day")
    emit(
        f"T1 D1 Δ свіжі − далекі: {fmtc(t1)} (n {int(t1['nf'])} / {int(t1['nr'])}); MDE₈₀ {t1['mde']:.3f}; кластери за днем: [{t1d['lo']:+.3f}; {t1d['hi']:+.3f}] p₁ {t1d['p1']:.4f}"
    )
    t1x = contrast(T, c1, D1, key="B15")
    emit(f"  D1 Δ ×1.5: {fmtc(t1x)}")
    pl1 = placebo_p(T, c1, D1, t1["d"])
    emit(f"  D1 плацебо p {pl1:.4f}")
    yrs1 = years_of(T, D1, vsp.START_MS, vsp.PASSPORT_MS)
    f1 = fresh_profit(T, c1, D1, yrs1, "D1")
    breakdown(T, c1, D1, "D1")
    # ---- D2: 2h / 8h / 12h
    T2 = vsp.load_rows(out)
    T2["d"] = np.array(T2["d"], dtype=object)
    c2 = classes(T2["d"])
    demo2 = (T2["rr"] >= vsp.MIN_RR) & (T2["stop_pct"] <= vsp.MAX_STOP) & (T2["score"] >= vsp.MIN_SCORE)
    turn2 = np.nan_to_num(T2["turn"], nan=-1) >= 5e6
    D2 = np.zeros(T2["B"].size, dtype=bool)
    D2_20 = np.zeros(T2["B"].size, dtype=bool)
    for tf in NEW_TFS:
        mtf = T2["tf"] == tf
        D2 |= vsp.chain(T2, mtf & demo2 & turn2 & (T2["t_in"] >= D2_START) & (T2["t_in"] < END[tf]))
        D2_20 |= vsp.chain(T2, mtf & demo2 & turn2 & (T2["t_in"] < D2_START))
    IS2, OOS2 = T2["t_in"] < vsp.OOS_MS, T2["t_in"] >= vsp.OOS_MS
    per_tf = ", ".join(f"{tf} {int((D2 & (T2['tf'] == tf)).sum())}" for tf in NEW_TFS)
    emit(f"\nD2 = 2h/8h/12h 2021-01 … 2026-09: {int(D2.sum())} trades ({per_tf}); IS {int((D2 & IS2).sum())}, OOS {int((D2 & OOS2).sum())}")
    emit(
        f"  класи D2: свіжі {int((D2 & (c2 == mo.FRESH)).sum())}, поруч {int((D2 & (c2 == mo.NEAR)).sum())}, далекі {int((D2 & (c2 == mo.FAR)).sum())}, без рівня {int((D2 & (c2 == '')).sum())}"
    )
    emit(f"  база D2 (усі угоди): {fmtm(vsp.ci(T2, D2))}; IS {fmtm(vsp.ci(T2, D2 & IS2))}; OOS {fmtm(vsp.ci(T2, D2 & OOS2))}")
    t2 = contrast(T2, c2, D2)
    emit(f"T2 D2 Δ свіжі − далекі: {fmtc(t2)} (n {int(t2['nf'])} / {int(t2['nr'])}); MDE₈₀ {t2['mde']:.3f}")
    t2x = contrast(T2, c2, D2, key="B15")
    emit(f"  D2 Δ ×1.5: {fmtc(t2x)}")
    pl2 = placebo_p(T2, c2, D2, t2["d"])
    emit(f"  D2 плацебо p {pl2:.4f}")
    for lab, mm in (("IS", D2 & IS2), ("OOS", D2 & OOS2)):
        r = contrast(T2, c2, mm)
        emit(f"  D2 {lab} Δ: {fmtc(r)} (n {int(r['nf'])} / {int(r['nr'])})")
    for tf in NEW_TFS:
        r = contrast(T2, c2, D2 & (T2["tf"] == tf))
        emit(f"  D2 {tf} Δ: {fmtc(r)} (n {int(r['nf'])} / {int(r['nr'])})")
    mk, top = drop_top5(T2, c2, D2)
    r = contrast(T2, c2, mk)
    emit(f"  D2 без топ-5 монет свіжих ({', '.join(top)}): Δ {fmtc(r)}")
    ov = overlap_mask(T2, T)
    r = contrast(T2, c2, D2 & ~ov)
    emit(
        f"  D2 без угод, що перекриваються з кандидатом S3 4h/1d (±24 h, та сама монета й бік; перекрито {int((D2 & ov).sum())} з {int(D2.sum())}): Δ {fmtc(r)} (n {int(r['nf'])} / {int(r['nr'])})"
    )
    ovc = overlap_mask(T2, T, after_ms=0)
    r = contrast(T2, c2, D2 & ~ovc)
    emit(
        f"  розвідувально, причинне перекриття ([t − 24 h; t]; перекрито {int((D2 & ovc).sum())}): без перекриття Δ {fmtc(r)} (n {int(r['nf'])} / {int(r['nr'])}); з перекриттям Δ {fmtc(contrast(T2, c2, D2 & ovc))}"
    )
    r = contrast(T2, c2, D2_20)
    emit(f"  D2 2020 (описово, n {int(D2_20.sum())}): Δ {fmtc(r)} (n {int(r['nf'])} / {int(r['nr'])})")
    emit("\nприбуток лише свіжих країв:")
    yrs2 = years_of(T2, D2, D2_START, END["2h"])
    f2 = fresh_profit(T2, c2, D2, yrs2, "D2")
    f2i = fresh_profit(T2, c2, D2 & IS2, years_of(T2, D2, D2_START, vsp.OOS_MS), "D2 IS")
    f2o = fresh_profit(T2, c2, D2 & OOS2, years_of(T2, D2, vsp.OOS_MS, END["2h"]), "D2 OOS")
    for tf in NEW_TFS:
        fresh_profit(T2, c2, D2 & (T2["tf"] == tf), years_of(T2, D2, D2_START, END[tf]), f"D2 {tf}")
    sides = {sd: fresh_profit(T2, c2, D2 & (T2["side"] == sd), yrs2, f"D2 {sd}") for sd in ("long", "short")}
    f2t = fresh_profit(T2, c2, mk, yrs2, "D2 без топ-5")
    fresh_profit(T2, c2, D2 & ~ov, yrs2, "D2 без перекриття ±24 h (заглядає вперед)")
    fresh_profit(T2, c2, D2 & ~ovc, yrs2, "D2 без причинного перекриття (розвідувально)")
    emit(f"  база D2 без перекриття ±24 h: {fmtm(vsp.ci(T2, D2 & ~ov))}; без причинного: {fmtm(vsp.ci(T2, D2 & ~ovc))}")
    fresh_profit(T2, c2, D2_20, years_of(T2, D2, vsp.START_MS, D2_START), "D2 2020 (описово)")
    breakdown(T2, c2, D2, "D2")
    per_asset(T2, c2, D2, IS2, OOS2)
    # ---- verdict (registered rules)
    ph = holm([t1["p1"], t2["p1"]])
    emit(f"\nВердикт (Холм на 2): T1 p₁ {t1['p1']:.4f} → {ph[0]:.4f}; T2 p₁ {t2['p1']:.4f} → {ph[1]:.4f}")
    passed = [ph[0] < 0.05, ph[1] < 0.05]
    if (passed[0] and t2["d"] > 0) or (passed[1] and t1["d"] > 0):
        feat = "✓ реальна"
    elif t2["d"] <= 0 or t2["hi"] < 0.05:
        feat = "✗"
    elif t1["d"] > 0 and t2["d"] > 0 and not (t1["mde"] > 0.11 and t2["mde"] > 0.11):
        feat = "≈"
    else:
        feat = "◯"
    emit(f"ознака: {feat} (MDE₈₀ T1 {t1['mde']:.3f}, T2 {t2['mde']:.3f})")
    module = feat.startswith("✓") and f2["lo15"] > 0 and all(x["m"] > 0 for x in (f2i, f2o, f1, f2t, *sides.values()))
    emit(
        f"модуль для 7c (лише свіжі): {'пройшов' if module else 'ні'} (D2 ×1.5 {f2['m15']:+.3f}, нижня межа {f2['lo15']:+.3f}; IS {f2i['m']:+.3f}, OOS {f2o['m']:+.3f}, D1 {f1['m']:+.3f}, long {sides['long']['m']:+.3f}, short {sides['short']['m']:+.3f}, без топ-5 {f2t['m']:+.3f})"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("sanity", "run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s3_fresh_extreme")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--modules-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/s3_modules")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"verify_s3_fresh_extreme --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"sanity": stage_sanity, "run": stage_run, "report": stage_report}[a.stage](a, out)
        emit(f"peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
