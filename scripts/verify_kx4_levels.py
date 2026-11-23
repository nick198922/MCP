#!/usr/bin/env python3
"""KX-4 (KillaXBT): yearly open and XO-4 weekly VWAP (K1) and a confluence of >= 2 levels (K2) swept and reclaimed by
the S3 sweep bar, as a module of S3 H2 (IDEA-20261113-x-traders, item 5; registration: the card's section 5.4).

    uv run python scripts/verify_kx4_levels.py --stage levels   # per-symbol level rows of the S3 signals (no R)
    uv run python scripts/verify_kx4_levels.py --stage zero     # stage 0: counts and shares only, no outcome metric
    uv run python scripts/verify_kx4_levels.py --stage report   # the registered test (after the registration commit)

The S3 trades, their R, the demo selection, the occupancy chain and the samples P / P100 are the S3 passport's
(``verify_setup_passport.py S3 --stage run``, joined by (symbol, timeframe, side, entry time) as in
``verify_s3_modules.py``). This script only re-runs the S3 detector to read each signal's sweep extreme, its close and
the levels at the open of the signal bar. No network: 1h candles from the passport caches.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_s3_modules as vsm  # noqa: E402  (samples P / P100 and the join key of the S3 module wave)
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import kx4_levels as kx  # noqa: E402
from tradedesk.analytics import s3_modules as mo  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.course_s14_ema import atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import chi2_sf, cochran_q  # noqa: E402

DAY_MS = 24 * kx.HOUR_MS
N_PLACEBO = 200
SEED = 20261123
INTEREST = 0.05
HYPS = ("K1", "K2")
emit = vsp.emit
CACHE = Path.home() / ".cache/tradedesk-agents"


# ---------------------------------------------------------------- per-symbol level rows (no R)


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir = args
    t0 = time.time()
    det, tfs, _ = vsp.SETUPS["S3"]
    h1, qv = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    rows: list[dict[str, Any]] = []
    for tf in tfs:
        tb = resample(h1, vsp.HOURS[tf])
        bar_ms = tb.bar_ms
        oriented = {"long": tb, "short": tb.mirrored()}
        atrs = {sd: atr(b, 14, segment_pos(b.t)) for sd, b in oriented.items()}
        for sig in det(tb, tf):
            t_in = sig.signal_ts + bar_ms
            if not (vsp.START_MS <= t_in < vsp.END[tf]):
                continue
            sg = 1.0 if sig.side == "long" else -1.0
            e, st, tg = sg * sig.entry, sg * sig.stop, sg * sig.target
            if not (st < e < tg):
                continue
            ind = sig.indicators
            bb = oriented[sig.side]
            i = int(np.searchsorted(bb.t, sig.signal_ts))
            if i >= bb.t.size or int(bb.t[i]) != sig.signal_ts:
                continue
            lv = kx.oriented_levels(h1.t, h1.o, h1.h, h1.low, h1.c, qv, sig.signal_ts, sig.side)
            rows.append(
                {
                    "k": vsm.key_of(s, tf, sig.side, t_in),
                    "ext": sg * ind["extreme"],
                    "cl": float(bb.c[i]),
                    "lo": float(bb.low[i]),
                    "e": e,
                    "edge": ind["range_low"] if sig.side == "long" else -ind["range_high"],
                    "atr": float(atrs[sig.side][i - 1]) if i >= 1 else None,
                    "lv": [None if not math.isfinite(lv[n]) else lv[n] for n in kx.LEVELS],
                }
            )
    (Path(out_dir) / "rows").mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "rows" / f"{s}.json").write_text(json.dumps(rows, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "sec": time.time() - t0, "rss": vsp.rss_mb()}


def stage_levels(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "rows" / f"{s}.json").exists()]
    emit(f"[levels] {len(jobs)} of {len(syms)} symbols to do, {a.workers} workers")
    t0 = time.time()
    n = 0
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for k, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            n += r["n"]
            if k % 25 == 0 or k == len(jobs):
                el = time.time() - t0
                emit(f"  [levels] {k}/{len(jobs)} {el:.0f}s eta {el / k * (len(jobs) - k):.0f}s; rows {n}; worker rss {r['rss']:.0f} MB")
    emit(f"[levels] done in {time.time() - t0:.0f}s; rows {n}")


# ---------------------------------------------------------------- joined table


def load(a: argparse.Namespace, out: Path) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Passport rows with this script's level columns (nan where no level row) and the H2 flag of the S3 wave."""
    T, S = vsm.samples(a)
    mod: dict[str, dict[str, Any]] = {}
    for p in sorted((out / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            mod[r["k"]] = r
    n = T["t_in"].size
    T["has"] = np.array([k in mod for k in T["key"]])
    for c in ("ext", "cl", "lo", "e", "edge", "atr"):
        T[c] = np.array([math.nan if (x := mod.get(k, {}).get(c)) is None else float(x) for k in T["key"]])
    lv = np.full((n, len(kx.LEVELS)), math.nan)
    for i, k in enumerate(T["key"]):
        r = mod.get(k)
        if r:
            lv[i] = [math.nan if x is None else float(x) for x in r["lv"]]
    T["lv"] = lv
    h2: dict[str, Any] = {}
    for p in sorted((a.s3m_cache / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            h2[r["k"]] = r.get("H2")
    T["H2"] = np.array([h2.get(k) is True for k in T["key"]])
    T["H2def"] = np.array([h2.get(k) is not None for k in T["key"]])
    T["month"] = T["t_in"].astype("datetime64[ms]").astype("datetime64[M]").astype(np.int64)
    k1, k2 = kx.flag_matrix(T["ext"], T["cl"], lv)
    T["K1"], T["K2"] = k1, k2
    fin = np.isfinite(lv)
    T["K1def"] = T["has"] & (fin[:, kx.LEVELS.index("YO")] | fin[:, kx.LEVELS.index("VW")])
    T["K2def"] = T["has"] & (fin.sum(axis=1) >= 2)
    return T, S


def trader_syms() -> set[str]:
    lines = [ln for ln in (ROOT / "docs/calls/x/KillaXBT.csv").open() if not ln.startswith("#")]
    return {f"{r['asset'].strip().upper()}USDT" for r in csv.DictReader(lines) if r["asset"].strip()}


def mde_scaled(f: float, n: int) -> float:
    """A-priori MDE80 of Δ (flag vs all): the S3 wave's H2 week-cluster SE 0.037R (share 0.48, n 7 006 OOS P) scaled
    by sqrt(((1 - f) / (f n)) / ((1 - 0.48) / (0.48 * 7006))); 2.8 x SE."""
    if not (0 < f < 1 and n > 0):
        return math.nan
    return 2.8 * 0.037 * math.sqrt(((1 - f) / (f * n)) / ((1 - 0.48) / (0.48 * 7006)))


def level_geometry(T: dict[str, np.ndarray], M: np.ndarray) -> str:
    """Where the levels sit relative to the S3 edge (oriented, in ATR of the signal timeframe): p10 / p50 / p90 of
    (level - edge) / ATR; positive = on the entry side of the edge (inside the range for VW)."""
    a = T["atr"][M]
    out = []
    for j, n in enumerate(kx.LEVELS):
        x = (T["lv"][M, j] - T["edge"][M]) / a
        x = x[np.isfinite(x)]
        if x.size:
            q = np.quantile(x, [0.1, 0.5, 0.9])
            out.append(f"{n} {q[0]:+.1f} / {q[1]:+.1f} / {q[2]:+.1f}")
    return "(level − edge) / ATR p10 / p50 / p90: " + ", ".join(out)


GATE_MDE = 0.25  # registration 5.4: above 5 x the interest (0.05R) the means are not computed


# ---------------------------------------------------------------- stage 0 (no outcome metric)


def stage_zero(a: argparse.Namespace, out: Path) -> None:
    T, S = load(a, out)
    period = T["t_in"] >= vsp.PASSPORT_MS
    IS, OOS = period & (T["t_in"] < vsp.OOS_MS), T["t_in"] >= vsp.OOS_MS
    emit(f"# KX-4 stage 0 (counts only). Passport S3 candidates {T['t_in'].size}, with a level row {int(T['has'].sum())}")
    m = T["has"]
    emit(
        f"sanity: close of the signal bar = entry on {int(np.sum(np.abs(T['cl'][m] - T['e'][m]) < 1e-9 * np.abs(T['e'][m])))} of {int(m.sum())}; "
        f"extreme = low of the signal bar on {int(np.sum(np.abs(T['ext'][m] - T['lo'][m]) < 1e-9 * np.abs(T['lo'][m])))}; extreme < edge < close on "
        f"{int(np.sum((T['ext'][m] < T['edge'][m]) & (T['edge'][m] < T['cl'][m])))}"
    )
    syms_k = trader_syms()
    for name in ("P100", "P"):
        M = S[name] & period & T["has"]
        emit(
            f"\n## {name} 2021-01 … 2026-09: n {int(M.sum())} (IS {int((M & IS).sum())}, OOS {int((M & OOS).sum())}); without a level row {int((S[name] & period & ~T['has']).sum())}"
        )
        fin = np.isfinite(T["lv"][M])
        emit("  defined levels: " + ", ".join(f"{n} {fin[:, j].mean():.2f}" for j, n in enumerate(kx.LEVELS)))
        sw = np.isfinite(T["lv"]) & (T["lv"] > T["ext"][:, None]) & (T["lv"] < T["cl"][:, None])
        emit("  swept and reclaimed (share of n): " + ", ".join(f"{n} {sw[M, j].mean():.3f}" for j, n in enumerate(kx.LEVELS)))
        for h in HYPS:
            D = M & T[h + "def"]
            f = T[h][D]
            emit(
                f"  {h}: defined {int(D.sum())}, flagged {int(f.sum())} ({f.mean():.3f}); IS {int((T[h] & D & IS).sum())} / OOS {int((T[h] & D & OOS).sum())}; "
                f"weeks with a flag {np.unique(T['week'][D & T[h]]).size}, months {np.unique(T['month'][D & T[h]]).size}; "
                f"a-priori MDE80 ≈ {mde_scaled(float(f.mean()), int(D.sum())):.3f}R (OOS-only {mde_scaled(float(f.mean()), int((D & OOS).sum())):.3f}R)"
            )
            emit(
                "    by year: "
                + " · ".join(
                    f"{y} {int((T[h] & D & (T['year'] == y)).sum())}/{int((D & (T['year'] == y)).sum())}" for y in range(2021, 2027)
                )
                + "; long "
                + f"{int((T[h] & D & (T['side'] == 'long')).sum())}, short {int((T[h] & D & (T['side'] == 'short')).sum())}; 4h {int((T[h] & D & (T['tf'] == '4h')).sum())}, 1d {int((T[h] & D & (T['tf'] == '1d')).sum())}"
            )
            both = D & T["H2def"]
            emit(
                f"    overlap with H2 (edge ≤ 0.3 ATR of PDL/PWL/PML): H2 share {T['H2'][both].mean():.3f}; {h} & H2 {int((T[h] & T['H2'] & both).sum())}, {h} & not H2 {int((T[h] & ~T['H2'] & both).sum())}"
            )
            keys = list(T["key"][D])
            u = kx.placebo_u(keys, 20, SEED)
            p1, p2 = kx.placebo_flags(T["ext"][D], T["cl"][D], T["lv"][D], u)
            pf = (p1 if h == "K1" else p2).mean(axis=0)
            emit(f"    placebo flag share (20 of {N_PLACEBO} sets): mean {pf.mean():.3f}, min {pf.min():.3f}, max {pf.max():.3f}")
        emit("  swept and reclaimed (count): " + ", ".join(f"{n} {int(sw[M, j].sum())}" for j, n in enumerate(kx.LEVELS)))
        n1, n2 = kx.near_matrix(T["edge"], T["atr"], T["lv"])
        for h, fl in (("K1 rule H2 (edge ≤ 0.3 ATR of YO / VW)", n1), ("K2 rule H2 (edge ≤ 0.3 ATR of a confluent level)", n2)):
            D = M & T[("K1" if h.startswith("K1") else "K2") + "def"]
            f = fl[D]
            emit(
                f"  {h}: flagged {int(f.sum())} ({f.mean():.3f}); OOS {int((fl & D & OOS).sum())}; weeks {np.unique(T['week'][D & fl]).size}; "
                f"a-priori MDE80 ≈ {mde_scaled(float(f.mean()), int(D.sum())):.3f}R"
            )
        nearj = np.isfinite(T["lv"]) & (np.abs(T["lv"] - T["edge"][:, None]) <= 0.3 * T["atr"][:, None])
        emit("  edge ≤ 0.3 ATR of the level (count): " + ", ".join(f"{n} {int(nearj[M, j].sum())}" for j, n in enumerate(kx.LEVELS)))
        emit("  " + level_geometry(T, M))
        tr = M & np.isin(T["sym"], list(syms_k))
        emit(f"  trader's coins ({len(syms_k)} assets): n {int(tr.sum())}, K1 {int((tr & T['K1']).sum())}, K2 {int((tr & T['K2']).sum())}")
    emit(f"\npeak rss {vsp.rss_mb():.0f} MB")


# ---------------------------------------------------------------- report (the registered test)


def dci(T: dict[str, np.ndarray], key: str, flag: np.ndarray, m: np.ndarray, cl: str = "week") -> dict[str, float]:
    r = sp.diff_ci(T[key], flag & m, m, T[cl], n_boot=vsp.N_BOOT, seed=SEED)
    r["p1"] = mo.one_sided_p(r["d"], r["p"])
    r["se"] = (r["hi"] - r["lo"]) / 3.92 if np.isfinite(r["hi"]) else math.nan
    r["n"] = float((flag & m).sum())
    return r


def fd(r: dict[str, float], k: str = "d") -> str:
    return f"{r[k]:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}]"


def per_asset(T: dict[str, np.ndarray], flag: np.ndarray, M: np.ndarray, IS: np.ndarray, OOS: np.ndarray) -> str:
    """5a: per-coin Δ (flagged minus all of the coin), Cochran Q / I² (coins with >= 10 flagged and >= 10 unflagged),
    persistence of the IS top-10 coins in OOS and Spearman ρ of per-coin Δ IS vs OOS (coins with >= 5 flagged in each)."""
    B = T["B"]

    def coin_d(mask: np.ndarray, min_f: int) -> dict[str, tuple[float, float]]:
        out = {}
        for s in np.unique(T["sym"][mask]):
            ms_ = mask & (T["sym"] == s)
            f, u = B[ms_ & flag], B[ms_ & ~flag]
            if f.size >= min_f and u.size >= min_f:
                se = math.sqrt(f.var(ddof=1) / f.size + u.var(ddof=1) / u.size) * (u.size / ms_.sum())
                out[str(s)] = (float(f.mean() - B[ms_].mean()), se)
        return out

    allc = coin_d(M, 10)
    lines = []
    if len(allc) >= 3:
        q, df, i2 = cochran_q(np.array([v[0] for v in allc.values()]), np.array([v[1] for v in allc.values()]))
        lines.append(f"монет {len(allc)}, Cochran Q {q:.1f} (df {df}, p {chi2_sf(q, df):.3f}), I² {i2:.2f}")
    di, do = coin_d(M & IS, 5), coin_d(M & OOS, 5)
    top = [s for s, _ in sorted(di.items(), key=lambda x: -x[1][0])[:10]]
    inn, rest = M & OOS & np.isin(T["sym"], top), M & OOS & ~np.isin(T["sym"], top)
    lines.append(
        f"топ-10 IS → OOS Δ {kx.subgroup_delta(B[inn], flag[inn]):+.3f} (n з ознакою {int((inn & flag).sum())}) проти решти {kx.subgroup_delta(B[rest], flag[rest]):+.3f} (n {int((rest & flag).sum())})"
    )
    both = sorted(set(di) & set(do))
    if len(both) >= 5:
        x = np.argsort(np.argsort([di[s][0] for s in both]))
        y = np.argsort(np.argsort([do[s][0] for s in both]))
        lines.append(f"Spearman ρ IS↔OOS {np.corrcoef(x, y)[0, 1]:+.2f} (монет {len(both)})")
    return "; ".join(lines)


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T, S = load(a, out)
    period = T["t_in"] >= vsp.PASSPORT_MS
    IS, OOS = period & (T["t_in"] < vsp.OOS_MS), T["t_in"] >= vsp.OOS_MS
    emit(f"# KX-4 report. S3 passport candidates {T['t_in'].size}, with a level row {int(T['has'].sum())}")
    lv = T["lv"]
    # MDE gate (registered): a hypothesis whose a-priori MDE80 on the primary sample exceeds GATE_MDE gets ◯ and no means
    gated = {}
    emit("\n## MDE-ворота (до будь-якого середнього)")
    for h in HYPS:
        M = S["P100"] & period & T[h + "def"]
        f = float(np.mean(T[h][M]))
        mde0 = mde_scaled(f, int(M.sum()))
        gated[h] = not (np.isfinite(mde0) and mde0 <= GATE_MDE)
        emit(
            f"{h}: n {int(M.sum())}, з ознакою {int((T[h] & M).sum())} ({f:.3f}; IS {int((T[h] & M & IS).sum())} / OOS {int((T[h] & M & OOS).sum())}), "
            f"тижнів з ознакою {np.unique(T['week'][M & T[h]]).size}; апріорна MDE₈₀ {mde0:.3f}R → {'◯ середні не рахуються' if gated[h] else 'прогін'}"
        )
    emit("  " + level_geometry(T, S["P100"] & period & T["has"]))
    if all(gated.values()):
        emit("\n## Вердикт: K1 ◯, K2 ◯ (MDE-ворота; середніх, CI і плацебо-різниць не рахували)")
        emit(f"\npeak rss {vsp.rss_mb():.0f} MB")
        return
    res: dict[str, dict[str, Any]] = {}
    for h in HYPS:
        if gated[h]:
            continue
        M = S["P100"] & period & T[h + "def"]
        fl = T[h]
        emit(f"\n## {h} (первинна: P100, 2021-01 … 2026-09, n {int(M.sum())}, з ознакою {int((fl & M).sum())} = {np.mean(fl[M]):.3f})")
        rw, rm = dci(T, "B", fl, M), dci(T, "B", fl, M, "month")
        emit(f"Δ = E[R | ознака] − E[R]: {fd(rw)} p₁ {rw['p1']:.4f} (тиждень); місяць [{rm['lo']:+.3f}; {rm['hi']:+.3f}]")
        ef, ea = sp.mean_ci(T["B"][M & fl], T["week"][M & fl], n_boot=vsp.N_BOOT, seed=SEED), float(np.mean(T["B"][M]))
        ef15 = sp.mean_ci(T["B15"][M & fl], T["week"][M & fl], n_boot=vsp.N_BOOT, seed=SEED)
        emit(
            f"  E[R] з ознакою {ef['m']:+.3f} [{ef['lo']:+.3f}; {ef['hi']:+.3f}], усіх {ea:+.3f}; ×1.5 з ознакою {ef15['m']:+.3f} [{ef15['lo']:+.3f}; {ef15['hi']:+.3f}]"
        )
        keys = list(T["key"][M])
        u = kx.placebo_u(keys, N_PLACEBO, SEED)
        p1, p2 = kx.placebo_flags(T["ext"][M], T["cl"][M], lv[M], u)
        pf = p1 if h == "K1" else p2
        dpl = np.array([kx.subgroup_delta(T["B"][M], pf[:, j]) for j in range(N_PLACEBO)])
        pp = kx.placebo_p(rw["d"], dpl)
        emit(
            f"  плацебо (рівні зсунуто на ±0.5–3 %, {N_PLACEBO} наборів): Δ медіана {np.nanmedian(dpl):+.3f}, 95-й перцентиль {np.nanquantile(dpl, 0.95):+.3f}; p {pp:.4f}; частка ознаки {pf.mean():.3f}"
        )
        ri, ro = dci(T, "B", fl, M & IS), dci(T, "B", fl, M & OOS)
        r15 = dci(T, "B15", fl, M)
        rP = dci(T, "B", fl, S["P"] & period & T[h + "def"])
        years = {y: kx.subgroup_delta(T["B"][M & (T["year"] == y)], fl[M & (T["year"] == y)]) for y in range(2021, 2027)}
        sides = {sd: kx.subgroup_delta(T["B"][M & (T["side"] == sd)], fl[M & (T["side"] == sd)]) for sd in ("long", "short")}
        contrib = np.where(fl, T["B"] - ea, 0.0)
        mt, top = vsm.drop_top(T, contrib, M)
        d_top = kx.subgroup_delta(T["B"][mt], fl[mt])
        emit(f"  IS {fd(ri)} (n {int(ri['n'])}); OOS {fd(ro)} (n {int(ro['n'])}); ×1.5 {r15['d']:+.3f}; P {fd(rP)} (n {int(rP['n'])})")
        emit(
            "  роки: "
            + " · ".join(f"{y} {v:+.3f}" for y, v in years.items())
            + f"; long {sides['long']:+.3f}, short {sides['short']:+.3f}; без топ-5 монет {d_top:+.3f} ({', '.join(top)})"
        )
        emit(
            f"  4h {kx.subgroup_delta(T['B'][M & (T['tf'] == '4h')], fl[M & (T['tf'] == '4h')]):+.3f}, 1d {kx.subgroup_delta(T['B'][M & (T['tf'] == '1d')], fl[M & (T['tf'] == '1d')]):+.3f}"
        )
        mde = 2.8 * max(rw["se"], rm["se"])
        emit(f"  MDE₈₀ = 2.8 × max(SE тиждень {rw['se']:.3f}, місяць {rm['se']:.3f}) = {mde:.3f}R")
        emit(f"  5a: {per_asset(T, fl, M, IS, OOS)}")
        res[h] = {
            "rw": rw,
            "rm": rm,
            "pp": pp,
            "ri": ri,
            "ro": ro,
            "r15": r15,
            "rP": rP,
            "years": years,
            "sides": sides,
            "d_top": d_top,
            "mde": mde,
            "ef15": ef15,
        }

    adj = holm([res[h]["rw"]["p1"] if h in res and np.isfinite(res[h]["rw"]["p1"]) else 1.0 for h in HYPS])
    emit("\n## Вердикт (Холм m = 2; гіпотеза за воротами — p = 1)")
    for h, pa in zip(HYPS, adj, strict=True):
        if h not in res:
            emit(f"{h}: ◯ (MDE-ворота)")
            continue
        x = res[h]
        cond = {
            "CI тиждень > 0": x["rw"]["lo"] > 0,
            "CI місяць > 0": x["rm"]["lo"] > 0,
            "Холм < 0.05": pa < 0.05,
            "плацебо p < 0.05": x["pp"] < 0.05,
            "IS > 0 і OOS > 0": x["ri"]["d"] > 0 and x["ro"]["d"] > 0,
            "≥ 4 з 6 років > 0": sum(1 for v in x["years"].values() if np.isfinite(v) and v > 0) >= 4,
            "×1.5 > 0": x["r15"]["d"] > 0,
            "long > 0 і short > 0": all(np.isfinite(v) and v > 0 for v in x["sides"].values()),
            "без топ-5 монет > 0": x["d_top"] > 0,
            "P > 0": x["rP"]["d"] > 0,
        }
        if all(cond.values()):
            v = "✓"
        elif x["rw"]["d"] > 0 and x["rw"]["p1"] < 0.05:
            v = "≈"
        elif x["mde"] > INTEREST:
            v = "◯"
        else:
            v = "✗"
        emit(f"{h}: Δ {x['rw']['d']:+.3f}, p₁ {x['rw']['p1']:.4f}, Холм {pa:.4f}, MDE₈₀ {x['mde']:.3f} → {v}")
        emit("  " + "; ".join(f"{k}: {'так' if ok else 'ні'}" for k, ok in cond.items()))
        emit(f"  модуль для версії S3 (✓ і нижня межа E[R] з ознакою ×1.5 > 0): {'так' if v == '✓' and x['ef15']['lo'] > 0 else 'ні'}")

    emit("\n## Варіанти (дослідницькі, поза Холмом; P100 2021+)")
    sw = np.isfinite(lv) & (lv > T["ext"][:, None]) & (lv < T["cl"][:, None])
    base = S["P100"] & period & T["has"]
    j = {n: kx.LEVELS.index(n) for n in kx.LEVELS}
    dist = np.array(
        [kx.edge_distance_atr(T["edge"][i], dict(zip(kx.LEVELS, lv[i], strict=True)), kx.NEW, T["atr"][i]) for i in range(lv.shape[0])]
    )
    _, k2w = kx.flag_matrix(T["ext"], T["cl"], lv, tol=0.01)
    variants = {
        "YO лише": (sw[:, j["YO"]], np.isfinite(lv[:, j["YO"]])),
        "VW лише": (sw[:, j["VW"]], np.isfinite(lv[:, j["VW"]])),
        "PW лише (старий рівень H2 / 10a)": (sw[:, j["PW"]], np.isfinite(lv[:, j["PW"]])),
        "K1 за правилом H2 (край ≤ 0.3 ATR від YO / VW)": (dist <= 0.3, np.isfinite(dist)),
        "K2 з допуском 1 %": (k2w, T["K2def"]),
        "H2 ∪ K1": (T["H2"] | T["K1"], T["H2def"] & T["K1def"]),
        "K1 без H2": (T["K1"] & ~T["H2"], T["H2def"] & T["K1def"] & ~T["H2"]),
    }
    for lab, (fl, de) in variants.items():
        M = base & de
        r = dci(T, "B", fl, M)
        ri, ro = dci(T, "B", fl, M & IS), dci(T, "B", fl, M & OOS)
        emit(
            f"{lab}: n {int(M.sum())}, з ознакою {int(r['n'])}; Δ {fd(r)}; IS {ri['d']:+.3f}, OOS {ro['d']:+.3f}; ×1.5 {dci(T, 'B15', fl, M)['d']:+.3f}"
        )
    rh = dci(T, "B", T["H2"], base & T["H2def"])
    emit(f"довідково H2 на тій самій вибірці: Δ {fd(rh)}")
    syms_k = trader_syms()
    for h in HYPS:
        M = S["P"] & period & T[h + "def"] & np.isin(T["sym"], list(syms_k))
        r = dci(T, "B", T[h], M)
        emit(f"активи трейдера ({h}, вибірка P): n {int(M.sum())}, з ознакою {int(r['n'])}; Δ {fd(r)}")
    emit(f"\npeak rss {vsp.rss_mb():.0f} MB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("levels", "zero", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=CACHE / "kx4_levels")
    ap.add_argument("--passport-cache", type=Path, default=CACHE / "setup_passport")
    ap.add_argument("--s3m-cache", type=Path, default=CACHE / "s3_modules")
    ap.add_argument("--s1-cache", type=Path, default=CACHE / "course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=CACHE / "funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"verify_kx4_levels --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"levels": stage_levels, "zero": stage_zero, "report": stage_report}[a.stage](a, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
