#!/usr/bin/env python3
"""S38 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b): H10 (half at 1R + the rest to the measured move or under
a structural trail), H1 (displacement on the breakout bar), H4 (breakout to a new 180-bar low), H5 (half taken above the
nearest liquidity pool on the way) and H6 (clean retest), each against the S38 passport base on the same trades, with
the registered placebos (own: random subgroup / price / target; cross-coin: a random coin of the same 4h bar, same
side and geometry). Registration: the card's section «Попередня реєстрація модулів S38».

    uv run python scripts/verify_s38_modules.py --stage run        # per-symbol module rows (local caches, 4 workers)
    uv run python scripts/verify_s38_modules.py --stage placebo    # cross-coin placebo trades (local caches)
    uv run python scripts/verify_s38_modules.py --stage report     # tables (also <cache>/report.txt)

No network: 1h bars from the S14 cache, the base trades / demo selection / chain from the S38 passport candidates.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import s3_modules as mo  # noqa: E402
from tradedesk.analytics import s38_modules as sm  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import chi2_sf, cochran_q, simulate_stop_rule  # noqa: E402

DAY_MS = 24 * HOUR_MS
BAR4 = 4 * HOUR_MS
MODULES = ("H10", "H1", "H4", "H5", "H6")
N_PLACEBO_SUB = 2000
K_COINS = 3
SETUP = "S38"
emit = vsp.emit


def key_of(sym: str, t_in: int) -> str:
    return f"{sym}|4h|short|{int(t_in)}"


def _r(o: Any) -> float | None:
    return None if o is None else float(o.r_net)


def _h(x: float) -> float | None:
    return None if not np.isfinite(x) else float(x)


# ---------------------------------------------------------------- per-symbol module rows


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir = args
    t0 = time.time()
    det = vsp.SETUPS[SETUP][0]
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    tb = resample(h1, 4)
    pos = segment_pos(tb.t)
    a_arr = atr(tb, 14, pos)
    path = h1.mirrored()
    fr = sm.fractal_lows(tb)
    tr_t, tr_lv = sm.trail_levels(tb.mirrored())
    hold = vsp.HOLD["4h"]
    rows: list[dict[str, Any]] = []
    for sig in det(tb, "4h"):
        t_in = sig.signal_ts + tb.bar_ms
        if not (vsp.START_MS <= t_in < vsp.END["4h"]):
            continue
        e, st, tg = -sig.entry, -sig.stop, -sig.target
        if not (st < e < tg):
            continue
        base = simulate_stop_rule(path, t_in, e, st, tg, hold, vsp.RULE_B, fees=vsp.F1)
        if base is None:
            continue
        base15 = simulate_stop_rule(path, t_in, e, st, tg, hold, vsp.RULE_B, fees=vsp.F15)
        ind = sig.indicators
        j = int(np.searchsorted(tb.t, sig.signal_ts))
        i_s = int(np.searchsorted(tb.t, int(ind["breakout_ts"])))
        lvl, a_j = float(ind["range_low"]), float(ind["atr14"])
        a_prev = float(a_arr[i_s - 1]) if i_s >= 1 else math.nan
        k = key_of(s, t_in)
        r: dict[str, Any] = {"k": k, "B": base.r_net, "stop_frac": (sig.stop - sig.entry) / sig.entry}
        # features (real prices; H1 / H4 read bars <= s, H6 bars s and j, H5 bars closed by t_in)
        r["H1"] = sm.displacement(tb, i_s, lvl, a_prev)
        r["H4"] = sm.new_low(tb, pos, i_s, 180)
        r["H4_90"] = sm.new_low(tb, pos, i_s, 90)
        q = sm.retest_quality(tb, i_s, j, lvl, a_j)
        if q is not None:
            r.update({f"H6_{x}": v for x, v in q.items()})
        r["H6"] = None if q is None else q["clean"]
        # H10: half at 1R, the rest to the measured move (MM) or under the structural trail (TR); placebos; stress
        one_r = e + (e - st)
        far = -sm.measured_move(lvl, float(ind["range_high"]), sig.target)
        r["mm_R"] = (far - e) / (e - st)
        for fees, suf in ((vsp.F1, ""), (vsp.F15, "_15")):
            one = sm.fixed_half(path, t_in, e, st, one_r, hold, fees)
            r["H10MM" + suf] = _h(sm.halves(one, sm.mm_runner(path, t_in, e, st, far, hold, fees)))
            r["H10TR" + suf] = _h(sm.halves(one, sm.trail_runner(path, t_in, e, st, hold, tr_t, tr_lv, fees)))
            if suf:
                continue
            fp = tg + mo.unit_draw(k, "h10mm") * (far - tg)
            r["H10MMp"] = _h(sm.halves(one, sm.mm_runner(path, t_in, e, st, fp, hold, fees)))
            d = max(far, e + 4.0 * (e - st))
            ft = tg + mo.unit_draw(k, "h10tr") * (d - tg)
            r["H10TRp"] = _h(sm.halves(one, sm.mm_runner(path, t_in, e, st, ft, hold, fees)))
        # H5: half above the nearest pool on the way to 2R (real prices), placebo at a random price in (target, entry)
        pool = sm.pool_on_path(sm.pool_levels(h1, fr, t_in), sig.entry, sig.target)
        p_real = sm.take_price(pool, a_j, sig.entry)
        r["H5on"] = bool(np.isfinite(p_real))
        r["H5_R"] = None if not np.isfinite(p_real) else (sig.entry - p_real) / (sig.stop - sig.entry)
        if np.isfinite(p_real):
            half = sm.fixed_half(path, t_in, e, st, -p_real, hold, vsp.F1)
            half15 = sm.fixed_half(path, t_in, e, st, -p_real, hold, vsp.F15)
            r["H5"] = None if half is None else 0.5 * half.r_net + 0.5 * base.r_net
            r["H5_15"] = None if (half15 is None or base15 is None) else 0.5 * half15.r_net + 0.5 * base15.r_net
            pp = e + mo.unit_draw(k, "h5") * (tg - e)
            hp = sm.fixed_half(path, t_in, e, st, pp, hold, vsp.F1)
            r["H5p"] = None if hp is None else 0.5 * hp.r_net + 0.5 * base.r_net
        rows.append(r)
    out = Path(out_dir) / "rows"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{s}.json").write_text(json.dumps(rows, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "sec": time.time() - t0, "rss": vsp.rss_mb()}


def pool_run(fn: Any, jobs: list[Any], workers: int, label: str) -> None:
    emit(f"[{label}] {len(jobs)} jobs, {workers} workers")
    t0, n = time.time(), 0
    with Pool(workers, maxtasksperchild=20) as pool:
        for k, r in enumerate(pool.imap_unordered(fn, jobs), 1):
            n += r["n"]
            if k % 25 == 0 or k == len(jobs):
                el = time.time() - t0
                emit(
                    f"  [{label}] {k}/{len(jobs)} {el:.0f}s eta {el / k * (len(jobs) - k):.0f}s; rows {n}; worker rss {r['rss']:.0f} MB; main {vsp.rss_mb():.0f} MB"
                )
    emit(f"[{label}] done in {time.time() - t0:.0f}s; rows {n}")


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "rows" / f"{s}.json").exists()]
    pool_run(run_symbol, jobs, a.workers, "run")


# ---------------------------------------------------------------- samples (the passport's)


def samples(a: argparse.Namespace) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[int, set[str]]]:
    T = vsp.load_rows(a.passport_cache / SETUP)
    period = T["t_in"] >= vsp.PASSPORT_MS
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn = np.nan_to_num(T["turn"], nan=-1) >= vsp.SETUPS[SETUP][2]
    elig = vsp.s1.eligible_syms(a.data_dir)
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig)
    top = vsp.top_n_days(qv, vsp.top_n_of(SETUP))
    in_top = vsp.in_prev_day_top(T, top)
    cap = vsp.BAR_CAP[SETUP]
    S = {"V": vsp.chain(T, period & demo & turn & in_top, cap), "P": vsp.chain(T, period & demo & turn, cap)}
    T["key"] = np.array([key_of(str(s), int(t)) for s, t in zip(T["sym"], T["t_in"], strict=True)])
    return T, S, top


# ---------------------------------------------------------------- cross-coin placebo


def placebo_symbol(args: tuple[str, str, str, str, list[list[Any]]]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir, jobs = args
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    tb = resample(h1, 4)
    a_arr = atr(tb, 14, segment_pos(tb.t))
    path = h1.mirrored()
    fr = sm.fractal_lows(tb)
    tr_t, tr_lv = sm.trail_levels(tb.mirrored())
    hold = vsp.HOLD["4h"]
    rows = []
    for key, kk, t_in, stop_frac, mm_r in jobs:
        i = int(np.searchsorted(h1.t, t_in - HOUR_MS))
        if i >= len(h1) or int(h1.t[i]) != t_in - HOUR_MS:
            continue
        er, sr, tr = sm.placebo_levels(float(h1.c[i]), float(stop_frac))
        e, st, tg = -er, -sr, -tr
        base = simulate_stop_rule(path, t_in, e, st, tg, hold, vsp.RULE_B, fees=vsp.F1)
        if base is None:
            continue
        one = sm.fixed_half(path, t_in, e, st, e + (e - st), hold, vsp.F1)
        far = e + max(2.0, float(mm_r)) * (e - st)
        q = int(np.searchsorted(tb.t, t_in - BAR4))
        a_j = float(a_arr[q]) if q < len(tb) and int(tb.t[q]) == t_in - BAR4 else math.nan
        pr = sm.take_price(sm.pool_on_path(sm.pool_levels(h1, fr, t_in), er, tr), a_j, er)
        h5 = base.r_net
        if np.isfinite(pr):
            half = sm.fixed_half(path, t_in, e, st, -pr, hold, vsp.F1)
            h5 = math.nan if half is None else 0.5 * half.r_net + 0.5 * base.r_net
        rows.append(
            {
                "k": key,
                "i": kk,
                "B": base.r_net,
                "MM": _h(sm.halves(one, sm.mm_runner(path, t_in, e, st, far, hold, vsp.F1))),
                "TR": _h(sm.halves(one, sm.trail_runner(path, t_in, e, st, hold, tr_t, tr_lv, vsp.F1))),
                "H5": _h(h5),
            }
        )
    out = Path(out_dir) / "placebo"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{s}.json").write_text(json.dumps(rows, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "rss": vsp.rss_mb()}


def stage_placebo(a: argparse.Namespace, out: Path) -> None:
    T, S, top = samples(a)
    mod = load_mod(out)
    have = set(vsp.syms_with_1h(a.s1_cache, a.data_dir))
    by: dict[str, list[list[Any]]] = defaultdict(list)
    for i in np.flatnonzero(S["V"] | S["P"]):
        key, sym, t = str(T["key"][i]), str(T["sym"][i]), int(T["t_in"][i])
        r = mod.get(key)
        if r is None:
            continue
        prev = (t // DAY_MS) * DAY_MS - DAY_MS
        pool = sorted((top.get(prev, set()) & have) - {sym})
        for kk, c in enumerate(sm.draw_coins(key, pool, K_COINS)):
            by[c].append([key, kk, t, r["stop_frac"], r["mm_R"]])
    emit(f"[placebo] trades {int((S['V'] | S['P']).sum())}, draws {sum(len(v) for v in by.values())}, coins {len(by)}")
    jobs = [(c, str(a.s1_cache), str(a.data_dir), str(out), v) for c, v in sorted(by.items())]
    if a.limit:
        jobs = jobs[: a.limit]
    pool_run(placebo_symbol, jobs, a.workers, "placebo")


# ---------------------------------------------------------------- report


def load_mod(out: Path) -> dict[str, dict[str, Any]]:
    mod: dict[str, dict[str, Any]] = {}
    for p in sorted((out / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            mod[r["k"]] = r
    return mod


def fmtd(r: dict[str, float], key: str = "m") -> str:
    return f"{r[key]:+.3f} [{r.get('lo', math.nan):+.3f}; {r.get('hi', math.nan):+.3f}]"


def paired(T: dict[str, np.ndarray], d: np.ndarray, m: np.ndarray) -> dict[str, float]:
    ok = m & np.isfinite(d)
    r = sp.mean_ci(d[ok], T["week"][ok], n_boot=vsp.N_BOOT, seed=vsp.SEED)
    r["p1"] = mo.one_sided_p(r["m"], r["p"])
    return r


def sub_delta(T: dict[str, np.ndarray], v: np.ndarray, flag: np.ndarray, m: np.ndarray) -> dict[str, float]:
    ok = m & np.isfinite(v)
    r = sp.diff_ci(v, flag & ok, ok, T["week"], n_boot=vsp.N_BOOT, seed=vsp.SEED)
    r["m"] = r["d"]
    r["p1"] = mo.one_sided_p(r["d"], r["p"])
    r["n"] = float((flag & ok).sum())
    return r


def drop_top(T: dict[str, np.ndarray], contrib: np.ndarray, m: np.ndarray, k: int = 5) -> tuple[np.ndarray, list[str]]:
    by: dict[str, float] = defaultdict(float)
    for s, c in zip(T["sym"][m], contrib[m], strict=True):
        if np.isfinite(c):
            by[str(s)] += float(c)
    top = [s for s, _ in sorted(by.items(), key=lambda x: -x[1])[:k]]
    return m & ~np.isin(T["sym"], top), top


def per_asset(T: dict[str, np.ndarray], d: np.ndarray, IS: np.ndarray, OOS: np.ndarray, label: str) -> None:
    """5a: heterogeneity of per-coin means of ``d`` (OOS) and persistence of the IS top-10 coins."""
    means, ses = [], []
    for s in np.unique(T["sym"][OOS]):
        v = d[OOS & (T["sym"] == s) & np.isfinite(d)]
        if v.size >= 5 and v.std(ddof=1) > 0:
            means.append(v.mean())
            ses.append(v.std(ddof=1) / math.sqrt(v.size))
    q, df, i2 = cochran_q(np.array(means), np.array(ses)) if len(means) >= 2 else (math.nan, 0, math.nan)
    is_m = {}
    for s in np.unique(T["sym"][IS]):
        v = d[IS & (T["sym"] == s) & np.isfinite(d)]
        if v.size >= 5:
            is_m[str(s)] = float(v.mean())
    top = [s for s, _ in sorted(is_m.items(), key=lambda x: -x[1])[:10]]
    inn = OOS & np.isin(T["sym"], top) & np.isfinite(d)
    rest = OOS & ~np.isin(T["sym"], top) & np.isfinite(d)
    pq = chi2_sf(q, df) if df > 0 else math.nan
    emit(
        f"  5a {label}: монет з ≥ 5 угод OOS {len(means)}, Cochran Q {q:.1f} (df {df}, p {pq:.3f}), I² {i2:.2f}; топ-10 IS → OOS {np.mean(d[inn]) if inn.any() else math.nan:+.3f} (n {int(inn.sum())}) проти решти {np.mean(d[rest]) if rest.any() else math.nan:+.3f} (n {int(rest.sum())})"
    )


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T, S, _ = samples(a)
    mod = load_mod(out)
    n = T["B"].size

    def col(name: str) -> np.ndarray:
        return np.array([math.nan if (x := mod.get(k, {}).get(name)) is None else float(x) for k in T["key"]])

    V, P = S["V"], S["P"]
    miss = sum(1 for k in T["key"][V | P] if k not in mod)
    B_mine = col("B")
    agree = (V | P) & np.isfinite(B_mine)
    emit(
        f"# S38 modules: passport candidates {n}; V {int(V.sum())}, P {int(P.sum())}; without a module row {miss}; base R equal to the passport on {int(np.sum(np.abs(B_mine[agree] - T['B'][agree]) < 1e-9))} of {int(agree.sum())}"
    )
    IS, OOS = T["t_in"] < vsp.OOS_MS, T["t_in"] >= vsp.OOS_MS
    yrs = (vsp.END["4h"] - vsp.OOS_MS) / (365.25 * DAY_MS)
    B = T["B"]
    rb = vsp.ci(T, V & OOS)
    emit(f"вибірка вердикту V: IS {int((V & IS).sum())}, OOS {int((V & OOS).sum())}; супутня P: OOS {int((P & OOS).sum())}")
    emit(
        f"база OOS V {fmtd(rb)} (n {int(rb['n'])}, тижнів {int(rb['k'])}); сума R / рік {np.sum(B[V & OOS]) / yrs:+.1f}, угод / рік {np.sum(V & OOS) / yrs:.0f}"
    )

    # cross-coin placebo, mean over the available draws per trade
    pl: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for p in sorted((out / "placebo").glob("*.json")):
        for r in json.loads(p.read_text()):
            for f in ("B", "MM", "TR", "H5"):
                if r[f] is not None and np.isfinite(r[f]):
                    pl[r["k"]][f].append(r[f])

    def pcol(f: str) -> np.ndarray:
        return np.array([np.mean(pl[k][f]) if k in pl and pl[k][f] else math.nan for k in T["key"]])

    PB = pcol("B")
    X = B - PB
    ndraw = np.array([len(pl[k]["B"]) if k in pl else 0 for k in T["key"]])
    emit(
        f"плацебо «випадкова монета тієї ж свічки»: угод V OOS з ≥ 1 плацебо {int(np.sum(V & OOS & (ndraw > 0)))}, середньо монет {np.mean(ndraw[V & OOS]):.2f}"
    )
    rx = paired(T, X, V & OOS)
    emit(
        f"  база S38 − плацебо OOS V {fmtd(rx)} p₂ {rx['p']:.3f}; плацебо E[R] {np.nanmean(PB[V & OOS]):+.3f}; IS: {fmtd(paired(T, X, V & IS))}"
    )

    res: dict[str, dict[str, Any]] = {}
    years = (("2025", T["year"] == 2025), ("2026", T["year"] == 2026))

    # ------------------------------------------------ exit modules: H10 (MM, TR), H5
    exits = {
        "H10MM": (col("H10MM"), col("H10MMp"), col("H10MM_15"), pcol("MM"), np.ones(n, dtype=bool)),
        "H10TR": (col("H10TR"), col("H10TRp"), col("H10TR_15"), pcol("TR"), np.ones(n, dtype=bool)),
        "H5": (col("H5"), col("H5p"), col("H5_15"), pcol("H5"), col("H5on") == 1.0),
    }
    for name, (R, Rp, R15, PR, on) in exits.items():
        d = R - B
        m = V & OOS & on & np.isfinite(d)
        emit(f"\n## {name}")
        r = paired(T, d, m)
        rp = paired(T, R - Rp, m)
        rc = paired(T, d - (PR - PB), m)
        emit(
            f"OOS V: n {int(r['n'])} (частка, де модуль діє, {np.sum(m) / max(1, np.sum(V & OOS)):.0%}), Δ {fmtd(r)} p₁ {r['p1']:.4f}; MDE ≈ 2·SE {2 * (r['hi'] - r['lo']) / 3.92:.3f}"
        )
        emit(
            f"  проти власного плацебо {fmtd(rp)} p₁ {rp['p1']:.4f}; проти монети тієї ж свічки (Δ − Δплацебо) {fmtd(rc)} p₁ {rc['p1']:.4f}"
        )
        er = paired(T, R, m)
        emit(
            f"  E[R] OOS V (ці угоди): база {np.mean(B[m]):+.3f}, модуль {fmtd(er)}; стрес ×1.5: база {np.nanmean(T['B15'][m]):+.3f}, модуль {np.nanmean(R15[m]):+.3f}"
        )
        if name == "H5":
            allv = V & OOS
            Rall = np.where(on, R, B)
            emit(
                f"  на всі угоди V OOS: E[R] {np.nanmean(Rall[allv]):+.3f} проти бази {np.mean(B[allv]):+.3f} (Δ × частка {np.nanmean((Rall - B)[allv]):+.4f}); середня відстань пулу {np.nanmean(col('H5_R')[m]):.2f}R"
            )
        emit(f"  сума R / рік OOS V: база {np.sum(B[m]) / yrs:+.1f}, модуль {np.nansum(R[m]) / yrs:+.1f}; угод / рік {np.sum(m) / yrs:.0f}")
        ri = paired(T, d, V & IS & on)
        rP = paired(T, d, P & OOS & on)
        emit(f"  IS V Δ {fmtd(ri)}; OOS P Δ {fmtd(rP)} (n {int(rP['n'])})")
        cells = [(lab, float(np.nanmean(d[m & c]))) for lab, c in years]
        mt, top = drop_top(T, d, m)
        cells.append(("без топ-5 монет", float(np.nanmean(d[mt]))))
        emit("  розбивки OOS V (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
        per_asset(T, np.where(V & on, d, math.nan), V & IS, V & OOS, name)
        res[name] = {"r": r, "rp": rp, "rc": rc, "cells": cells, "rP": rP, "er": er, "R": R, "share": np.sum(m) / max(1, np.sum(V & OOS))}

    # ------------------------------------------------ subgroup modules: H1, H4, H6
    flags = {"H1": col("H1"), "H4": col("H4"), "H6": col("H6")}
    for name, fv in flags.items():
        defined = np.isfinite(fv)
        flag = fv == 1.0
        m = V & OOS & defined
        r = sub_delta(T, B, flag, m)
        obs, plc = mo.subgroup_placebo(B[m], flag[m], N_PLACEBO_SUB, vsp.SEED)
        pp = float((np.sum(plc >= obs) + 1) / (plc.size + 1)) if plc.size else math.nan
        rc = sub_delta(T, X, flag, m & np.isfinite(X))
        share = r["n"] / max(1, m.sum())
        emit(f"\n## {name}")
        emit(
            f"OOS V (ознака визначена {int(m.sum())}): з ознакою n {int(r['n'])} ({share:.0%}), E[R] {np.mean(B[m & flag]):+.3f} проти {np.mean(B[m]):+.3f} усіх; Δ {fmtd(r)} p₁ {r['p1']:.4f}; MDE ≈ 2·SE {2 * (r['hi'] - r['lo']) / 3.92:.3f}"
        )
        emit(
            f"  плацебо-підгрупа p {pp:.4f} (95-й перцентиль {np.quantile(plc, 0.95):+.3f}); проти монети тієї ж свічки (E[X|ознака] − E[X]) {fmtd(rc)} p₁ {rc['p1']:.4f}"
        )
        er = paired(T, B, m & flag)
        emit(f"  E[R] угод з ознакою OOS V {fmtd(er)}; без ознаки {np.mean(B[m & ~flag]):+.3f} (n {int(np.sum(m & ~flag))})")
        ri = sub_delta(T, B, flag, V & IS & defined)
        rP = sub_delta(T, B, flag, P & OOS & defined)
        emit(
            f"  IS V Δ {fmtd(ri)} (n з ознакою {int(ri['n'])}, частка {ri['n'] / max(1, np.sum(V & IS & defined)):.0%}); OOS P Δ {fmtd(rP)} (n {int(rP['n'])})"
        )
        cells = [(lab, float(np.mean(B[m & c & flag]) - np.mean(B[m & c])) if np.any(m & c & flag) else math.nan) for lab, c in years]
        contrib = np.where(flag, B - np.mean(B[m]), 0.0)
        mt, top = drop_top(T, contrib, m)
        cells.append(("без топ-5 монет", float(np.mean(B[mt & flag]) - np.mean(B[mt]))))
        emit("  розбивки OOS V (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
        emit(f"  угод з ознакою / рік OOS {np.sum(m & flag) / yrs:.0f}; сума R з ознакою / рік {np.sum(B[m & flag]) / yrs:+.1f}")
        per_asset(T, np.where(V & defined & flag, B - np.mean(B[V & OOS & defined]), math.nan), V & IS, V & OOS, name)
        res[name] = {"r": r, "rc": rc, "cells": cells, "rP": rP, "plc_p": pp, "flag": flag & defined, "er": er, "share": share}

    # ------------------------------------------------ verdict: Holm over 5 (H10 = 2 x min of its two variants)
    pm, pt = res["H10MM"]["r"]["p1"], res["H10TR"]["r"]["p1"]
    h10v = "H10MM" if pm <= pt else "H10TR"
    p_mod = {"H10": min(1.0, 2 * min(pm, pt)), **{mn: res[mn]["r"]["p1"] for mn in ("H1", "H4", "H5", "H6")}}
    adj = dict(zip(MODULES, holm([p_mod[mn] if np.isfinite(p_mod[mn]) else 1.0 for mn in MODULES]), strict=True))
    emit(f"\n## Вердикт (Холм на 5; H10: Холм на 2 всередині — p_MM {pm:.4f}, p_TR {pt:.4f}, кандидат {h10v})")
    passed = []
    for mn in MODULES:
        x = res[h10v if mn == "H10" else mn]
        plc_p = x["rp"]["p1"] if "rp" in x else x["plc_p"]
        robust = all(np.isfinite(v) and v > 0 for _, v in x["cells"])
        ok = x["r"]["m"] > 0 and adj[mn] < 0.05 and plc_p < 0.05 and x["rc"]["m"] > 0 and robust and x["rP"]["m"] > 0
        mde = 2 * (x["r"]["hi"] - x["r"]["lo"]) / 3.92
        label = "ПРОЙШОВ" if ok else ("не пройшов, невимірно (|Δ| < MDE)" if abs(x["r"]["m"]) < mde else "не пройшов")
        emit(
            f"{mn}: Δ {x['r']['m']:+.3f}, p₁ {p_mod[mn]:.4f}, Холм {adj[mn]:.4f}, власне плацебо p {plc_p:.4f}, монета тієї ж свічки {x['rc']['m']:+.3f}, стійкість {'так' if robust else 'ні'}, P Δ {x['rP']['m']:+.3f}, MDE {mde:.3f}, частка {x['share']:.0%}, E[R] модуля OOS {x['er']['m']:+.3f} → {label}"
        )
        if ok:
            passed.append(mn)
            emit(f"   {'торгова ідея (E[R] > 0)' if x['er']['m'] > 0 else 'зменшення збитку (E[R] ≤ 0)'}")
    emit(f"пройшли: {passed or 'жоден'}")
    if len(passed) >= 2:
        R = B.copy()
        if "H10" in passed:
            R = res[h10v]["R"].copy()
        elif "H5" in passed:
            R = np.where(col("H5on") == 1.0, res["H5"]["R"], B)
        w = np.ones(n)
        for mn in ("H1", "H4", "H6"):
            if mn in passed:
                w += res[mn]["flag"].astype(float)
        mm = V & OOS & np.isfinite(R)
        emit(f"комбінація {passed}: E[R] OOS V {mo.weighted_mean(R[mm], w[mm]):+.3f} проти бази {np.mean(B[mm]):+.3f}")
    else:
        emit("комбінації немає (пройшло менше двох модулів)")

    # ------------------------------------------------ descriptive (registered)
    emit("\n## Описово (зареєстровано)")
    m = V & OOS
    f1, f6 = res["H1"]["flag"], res["H6"]["flag"]
    d16 = np.isfinite(col("H1")) & np.isfinite(col("H6"))
    for lab, c in (("H1 і H6", f1 & f6), ("лише H1", f1 & ~f6), ("лише H6", ~f1 & f6), ("жодної", ~f1 & ~f6)):
        mm = m & d16 & c
        emit(f"  2×2 {lab}: n {int(mm.sum())}, E[R] {np.mean(B[mm]) if mm.any() else math.nan:+.3f}")
    for lab, c in (("H4 (180)", col("H4") == 1.0), ("H4 лише 90 свічок", col("H4_90") == 1.0)):
        for sc_lab, sc in (("бал 2", T["score"] == 2), ("бал 3+", T["score"] >= 3)):
            mm = m & sc
            emit(
                f"  {lab}, {sc_lab}: з ознакою n {int(np.sum(mm & c))} E[R] {np.mean(B[mm & c]) if np.any(mm & c) else math.nan:+.3f}; без n {int(np.sum(mm & ~c))} E[R] {np.mean(B[mm & ~c]) if np.any(mm & ~c) else math.nan:+.3f}"
            )
    for part in ("fast", "low_third", "wick", "shallow"):
        c = col(f"H6_{part}") == 1.0
        emit(f"  H6 {part}: частка {np.mean(c[m]):.2f}, E[R] з {np.mean(B[m & c]):+.3f} / без {np.mean(B[m & ~c]):+.3f}")
    emit(
        f"  H10: частка угод з виміряним рухом далі за 2R {np.mean(col('mm_R')[m] > 2.0 + 1e-9):.2f}; медіана MM {np.nanmedian(col('mm_R')[m]):.2f}R"
    )
    cnt = Counter(int(x) for x in T["year"][m])
    emit(f"  угод V OOS за роками {dict(sorted(cnt.items()))}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("run", "placebo", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s38_modules")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
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
        emit(f"verify_s38_modules --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"run": stage_run, "placebo": stage_placebo, "report": stage_report}[a.stage](a, out)
        emit(f"peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
