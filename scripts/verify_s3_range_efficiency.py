"""S3: Kaufman efficiency of the range before the sweep as a weight for the score (IDEA-20261030-demo-setups-depth,
item «S3, „ефективність“ рейнджу як вага»; registration «Попередня реєстрація — S3, ефективність рейнджу»).

    uv run python scripts/verify_s3_range_efficiency.py --stage smoke      # counts, timings, feature checks, no R
    uv run python scripts/verify_s3_range_efficiency.py --stage features   # ER / VR of every S3 candidate, no R
    uv run python scripts/verify_s3_range_efficiency.py --stage run        # MDE80 gate, then the registered test

No network: the S3 passport's candidate files (``<passport-cache>/S3/cands``, rule B, demo costs) and the 1h candle
cache of the passport (``course_s14_ema/k1h``, resampled to 4h / 1d as the detector saw them). The features are
written one JSON per symbol to ``<cache-dir>/features/``; every stage writes its output to ``<cache-dir>/<stage>.txt``.
R is per trade risk, not on capital.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np
import verify_setup_passport as vsp  # passport rows, masks, chain, loaders (read-only reuse)
from common_helpers import rss_mb

from tradedesk.analytics import range_efficiency as rf
from tradedesk.analytics.course_s14_ema import resample, segment_pos
from tradedesk.analytics.liquidation_flush import holm
from tradedesk.analytics.stop_management import cochran_q

SETUP = "S3"
SEED = 20261030
N_BOOT = 10_000
N_BOOT_RHO = 2_000
N_PERM = 2_000
MDE_GATE = 0.10  # registered: the interesting T1 - T3 difference, R
HOME = Path.home() / ".cache/tradedesk-agents"
FEATS = ("er30", "er20", "er60", "vr4")
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


# ---------------------------------------------------------------- features (no outcome)


def symbol_features(args: tuple[str, str, str, str, str]) -> dict[str, Any]:
    """ER30 / ER20 / ER60 / VR(4) of every S3 candidate row of one symbol, in the row order of its cands file."""
    s, cands_dir, s1_cache, data_dir, out_dir = args
    t0 = time.time()
    rows = json.loads((Path(cands_dir) / f"{s}.json").read_text()).get("rows", [])
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    bars = {}
    out: list[dict[str, Any]] = []
    miss = 0
    for r in rows:
        tf = r["tf"]
        if tf not in bars:
            tb = resample(h1, vsp.HOURS[tf])
            bars[tf] = (tb, segment_pos(tb.t))
        tb, pos = bars[tf]
        sig = int(r["t_in"]) - tb.bar_ms
        i = int(np.searchsorted(tb.t, sig))
        if i >= len(tb) or int(tb.t[i]) != sig:
            miss += 1
            out.append({"t_in": r["t_in"], "tf": tf, "side": r["side"], **dict.fromkeys(FEATS)})
            continue
        f = {
            "er30": rf.efficiency_ratio(tb.c, pos, i, 30),
            "er20": rf.efficiency_ratio(tb.c, pos, i, 20),
            "er60": rf.efficiency_ratio(tb.c, pos, i, 60),
            "vr4": rf.variance_ratio(tb.c, pos, i, 120, 4),
        }
        out.append({"t_in": r["t_in"], "tf": tf, "side": r["side"], **{k: (None if not math.isfinite(v) else v) for k, v in f.items()}})
    if out_dir:
        (Path(out_dir) / "features").mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "features" / f"{s}.json").write_text(json.dumps({"rows": out}))
    return {"sym": s, "n": len(rows), "miss": miss, "sec": time.time() - t0, "rss": rss_mb(), "rows": out if not out_dir else None}


def load_features(cache: Path, cands: Path) -> dict[str, np.ndarray]:
    """Feature arrays aligned with ``vsp.load_rows`` (same sorted file order, same row order; keys checked)."""
    vals: dict[str, list[float]] = {k: [] for k in FEATS}
    keys: list[tuple[int, str, str]] = []
    for p in sorted(cands.glob("*.json")):
        fp = cache / "features" / p.name
        rows = json.loads(fp.read_text())["rows"] if fp.exists() else None
        src = json.loads(p.read_text()).get("rows", [])
        if rows is None or len(rows) != len(src):
            raise SystemExit(f"features missing or misaligned for {p.stem}: run --stage features")
        for r in rows:
            keys.append((int(r["t_in"]), r["tf"], r["side"]))
            for k in FEATS:
                vals[k].append(math.nan if r[k] is None else float(r[k]))
    out = {k: np.asarray(v, dtype=float) for k, v in vals.items()}
    out["_keys"] = np.array([f"{a}|{b}|{c}" for a, b, c in keys], dtype=object)
    return out


def passport_masks(a: argparse.Namespace) -> dict[str, Any]:
    """The S3 passport rows, the registered main sample and the top-100 slice (no outcome)."""
    T = vsp.load_rows(Path(a.passport_cache) / SETUP)
    floor = vsp.SETUPS[SETUP][2]
    period = T["t_in"] >= vsp.PASSPORT_MS
    sel = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= floor
    main = vsp.chain(T, period & sel & turn5)
    oos = T["t_in"] >= vsp.OOS_MS
    month = np.array([(lambda d: d.year * 12 + d.month - 1)(datetime.fromtimestamp(t / 1000, UTC)) for t in T["t_in"]])
    return {"T": T, "main": main, "OOS": oos, "IS": ~oos, "month": month, "sel": sel, "period": period, "turn5": turn5}


def top100(a: argparse.Namespace, M: dict[str, Any]) -> np.ndarray:
    T = M["T"]
    qv = vsp.s1.daily_qv(Path(a.s1_cache), Path(a.data_dir), vsp.s1.eligible_syms(Path(a.data_dir)))
    in_top = vsp.in_prev_day_top(T, vsp.top_n_days(qv, vsp.TOP_N))
    return vsp.chain(T, M["period"] & M["sel"] & M["turn5"] & in_top)


def describe_feature(x: np.ndarray, T: dict[str, np.ndarray], m: np.ndarray, name: str) -> None:
    for tf in ("4h", "1d"):
        mm = m & (T["tf"] == tf)
        v = x[mm]
        ok = np.isfinite(v)
        q = np.quantile(v[ok], [0.05, 0.25, 0.5, 0.75, 0.95]) if ok.any() else [math.nan] * 5
        emit(f"  {name} {tf}: n {int(mm.sum())}, undefined {int((~ok).sum())}; p5/25/50/75/95 " + " / ".join(f"{z:.3f}" for z in q))


# ---------------------------------------------------------------- stages


def stage_smoke(a: argparse.Namespace, out: Path) -> None:
    cands = Path(a.passport_cache) / SETUP / "cands"
    t0 = time.monotonic()
    M = passport_masks(a)
    T, main = M["T"], M["main"]
    emit(f"passport S3 rows {T['t_in'].size}; loaded in {time.monotonic() - t0:.1f} s; peak RSS {rss_mb():.0f} MB")
    emit(
        f"main sample (known number 21 235 / OOS 7 006): 2021+ {int(main.sum())}, IS {int((main & M['IS']).sum())}, OOS {int((main & M['OOS']).sum())}"
    )
    emit(f"OOS months {np.unique(M['month'][main & M['OOS']]).size}, IS months {np.unique(M['month'][main & M['IS']]).size}")
    emit(f"symbols with S3 cands {len(list(cands.glob('*.json')))}")
    for s in a.symbols.split(","):
        r = symbol_features((s, str(cands), a.s1_cache, a.data_dir, ""))
        rows = r["rows"]
        er = np.array([math.nan if x["er30"] is None else x["er30"] for x in rows])
        vr = np.array([math.nan if x["vr4"] is None else x["vr4"] for x in rows])
        emit(
            f"{s}: rows {r['n']}, signal bar not found {r['miss']}, ER30 undefined {int(np.isnan(er).sum())}, VR undefined "
            f"{int(np.isnan(vr).sum())}; ER30 median {np.nanmedian(er):.3f} range [{np.nanmin(er):.3f}, {np.nanmax(er):.3f}]; "
            f"VR median {np.nanmedian(vr):.3f}; {r['sec']:.1f} s; peak RSS {r['rss']:.0f} MB"
        )
    emit("smoke: no outcome (R) computed")


def stage_features(a: argparse.Namespace, out: Path) -> None:
    cands = Path(a.passport_cache) / SETUP / "cands"
    syms = sorted(p.stem for p in cands.glob("*.json"))
    jobs = [(s, str(cands), a.s1_cache, a.data_dir, str(out)) for s in syms if a.force or not (out / "features" / f"{s}.json").exists()]
    emit(f"features: {len(jobs)} of {len(syms)} symbols to compute")
    t0 = time.monotonic()
    miss = 0
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(symbol_features, jobs), 1):
            miss += r["miss"]
            if k % 25 == 0 or k == len(jobs):
                emit(
                    f"  {k}/{len(jobs)} {r['sym']} {time.monotonic() - t0:.0f} s; worker peak RSS {r['rss']:.0f} MB; parent {rss_mb():.0f} MB"
                )
    emit(f"signal bar not found: {miss}")
    M = passport_masks(a)
    T, main = M["T"], M["main"]
    F = load_features(out, cands)
    keys = np.array([f"{a_}|{b}|{c}" for a_, b, c in zip(T["t_in"], T["tf"], T["side"], strict=True)], dtype=object)
    emit(f"alignment with passport rows: {'ok' if np.array_equal(keys, F['_keys']) else 'MISMATCH'} ({keys.size} rows)")
    emit("feature distribution, main sample 2021+ (no outcome):")
    for k in FEATS:
        describe_feature(F[k], T, main, k)
    emit("IS tertile cuts of ER30 (registered, applied to OOS):")
    for tf in ("4h", "1d"):
        lo, hi = rf.tertile_cuts(F["er30"][main & M["IS"] & (T["tf"] == tf)])
        emit(f"  {tf}: {lo:.4f} / {hi:.4f}")
    emit("Spearman of ER30 with non-outcome columns, main 2021+:")
    for c in ("stop_pct", "rr", "score"):
        emit(f"  ER30 ~ {c}: {rf.spearman(F['er30'][main], np.asarray(T[c][main], dtype=float)):+.3f}")
    emit(
        f"  ER30 ~ VR4: {rf.spearman(F['er30'][main], F['vr4'][main]):+.3f}; ER30 ~ ER20 {rf.spearman(F['er30'][main], F['er20'][main]):+.3f}; ER30 ~ ER60 {rf.spearman(F['er30'][main], F['er60'][main]):+.3f}"
    )
    emit(f"features: no outcome (R) computed; peak RSS {rss_mb():.0f} MB")


def cuts_by_tf(x: np.ndarray, T: dict[str, np.ndarray], is_m: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict[str, tuple[float, float]]]:
    lo = np.full(x.size, math.nan)
    hi = np.full(x.size, math.nan)
    cuts = {}
    for tf in ("4h", "1d"):
        c = rf.tertile_cuts(x[is_m & (T["tf"] == tf)])
        cuts[tf] = c
        lo[T["tf"] == tf], hi[T["tf"] == tf] = c
    return lo, hi, cuts


def fd(r: dict[str, float]) -> str:
    return f"{r['est']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] (n {r.get('n_a', r.get('n', ''))}/{r.get('n_b', '')}, p1 {r.get('p', math.nan):.3f})"


def delta(y: np.ndarray, k: np.ndarray, m: np.ndarray, cl: np.ndarray, n_boot: int = N_BOOT) -> dict[str, float]:
    ok = m & np.isfinite(y)
    return rf.boot_diff(y[ok], (k == 1)[ok], (k == 3)[ok], cl[ok], n_boot=n_boot, seed=SEED)


def stage_run(a: argparse.Namespace, out: Path) -> None:
    cands = Path(a.passport_cache) / SETUP / "cands"
    M = passport_masks(a)
    T, main, IS, OOS, month = M["T"], M["main"], M["IS"], M["OOS"], M["month"]
    F = load_features(out, cands)
    keys = np.array([f"{a_}|{b}|{c}" for a_, b, c in zip(T["t_in"], T["tf"], T["side"], strict=True)], dtype=object)
    if not np.array_equal(keys, F["_keys"]):
        raise SystemExit("features misaligned with passport rows")
    if a.dry:  # code check without the outcome: R columns replaced by seeded noise, the gate is not applied
        rng = np.random.default_rng(1)
        for col in ("B", "B15", "Bx"):
            T[col] = rng.normal(0.0, 1.0, T[col].size)
        emit("DRY RUN: B / B15 / Bx are random noise, nothing below is a result")
    B = T["B"]
    emit(f"main sample: 2021+ {int(main.sum())}, IS {int((main & IS).sum())}, OOS {int((main & OOS).sum())} (known 21 235 / 7 006)")
    x = F["er30"]
    lo, hi, cuts = cuts_by_tf(x, T, main & IS)
    k = rf.tertile_codes(x, lo, hi)
    emit(f"ER30 IS cuts: {cuts}; ER30 undefined in main {int((main & ~np.isfinite(x)).sum())}")
    emit(
        f"OOS tertile sizes T1 {int((main & OOS & (k == 1)).sum())}, T2 {int((main & OOS & (k == 2)).sum())}, T3 {int((main & OOS & (k == 3)).sum())}"
    )

    # ---- gate: MDE80 before any mean
    emit("\n== Gate MDE80 (T1 - T3, cluster-robust SE) ==")
    gate = {}
    for nm, m, cl in (("OOS, months (gate)", main & OOS, month), ("IS, months", main & IS, month), ("OOS, weeks", main & OOS, T["week"])):
        ok = m & np.isfinite(B)
        se = rf.cluster_se_diff(B[ok], (k == 1)[ok], (k == 3)[ok], cl[ok])
        gate[nm] = rf.mde80(se)
        emit(f"  {nm}: clusters {np.unique(cl[ok & (k > 0) & (k != 2)]).size}, SE {se:.4f}, MDE80 {rf.mde80(se):.4f} R")
    if gate["OOS, months (gate)"] > MDE_GATE and not a.dry:
        emit(f"\nGATE FAILED: MDE80 {gate['OOS, months (gate)']:.4f} > {MDE_GATE} R -> verdict ◯ (unmeasurable); no means computed")
        return
    emit(f"gate passed: MDE80 {gate['OOS, months (gate)']:.4f} <= {MDE_GATE} R")

    # ---- H1 and the registered secondary rows
    emit("\n== H1: Δ = E[B|T1] - E[B|T3] (bootstrap over months, 10 000) ==")
    h1 = delta(B, k, main & OOS, month)
    h1_is = delta(B, k, main & IS, month)
    emit(f"  OOS (primary): {fd(h1)}")
    emit(f"  IS:            {fd(h1_is)}")
    rho = {}
    for nm, m in (("IS", main & IS), ("OOS", main & OOS)):
        mm = m & np.isfinite(x) & np.isfinite(B)
        p = rf.within_percentile(x[mm], T["tf"][mm])
        rho[nm] = rf.boot_spearman(p, B[mm], month[mm], n_boot=N_BOOT_RHO, seed=SEED)
        emit(f"  Spearman ρ(ER pct within tf, B) {nm}: {rho[nm]['est']:+.4f} [{rho[nm]['lo']:+.4f}; {rho[nm]['hi']:+.4f}] n {rho[nm]['n']}")
    emit("  E[B] by tertile (bootstrap over months):")
    for nm, m in (("IS", main & IS), ("OOS", main & OOS)):
        cells = []
        for t in (1, 2, 3):
            mm = m & (k == t) & np.isfinite(B)
            r = rf.boot_mean(B[mm], month[mm], n_boot=N_BOOT, seed=SEED)
            cells.append(f"T{t} {r['est']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] n {r['n']}")
        emit(f"    {nm}: " + " | ".join(cells))
    cell = np.array([f"{mo}|{tf}" for mo, tf in zip(month, T["tf"], strict=True)], dtype=object)
    pl = {}
    for nm, m in (("OOS", main & OOS), ("IS", main & IS)):
        mm = m & np.isfinite(x) & np.isfinite(B)
        perm = rf.placebo_diff(x[mm], B[mm], cell[mm], lo[mm], hi[mm], n_perm=N_PERM, seed=SEED)
        obs = h1["est"] if nm == "OOS" else h1_is["est"]
        pl[nm] = float((1 + np.sum(perm >= obs)) / (1 + perm.size))
        emit(
            f"  placebo {nm} (ER permuted within month × tf, {N_PERM}): mean Δ_perm {np.mean(perm):+.4f}, p5/p95 {np.quantile(perm, 0.05):+.4f}/{np.quantile(perm, 0.95):+.4f}, p_pl {pl[nm]:.4f}"
        )

    # ---- decision (registered order)
    if h1["est"] > 0 and h1["lo"] > 0 and h1_is["est"] > 0 and pl["OOS"] < 0.05:
        verdict = "✓ weight useful"
    elif h1["hi"] < 0:
        verdict = "✗ opposite sign"
    elif h1["hi"] < MDE_GATE:
        verdict = "✗ useful effect excluded (CI upper < 0.10R)"
    elif h1["est"] > 0 and h1_is["est"] > 0 and rho["IS"]["est"] < 0 and rho["OOS"]["est"] < 0:
        verdict = "≈ direction right, not significant"
    else:
        verdict = "◯ inconclusive"
    emit(f"\nVERDICT (registered rule): {verdict}")

    # ---- robustness / descriptive
    emit("\n== Descriptive: other features and windows (same pipeline, IS cuts per tf) ==")
    pvals = {"ER30": h1["p"]}
    for f in ("vr4", "er20", "er60"):
        lo_f, hi_f, _ = cuts_by_tf(F[f], T, main & IS)
        kf = rf.tertile_codes(F[f], lo_f, hi_f)
        r_o, r_i = delta(B, kf, main & OOS, month), delta(B, kf, main & IS, month)
        line = f"  {f}: OOS {fd(r_o)} | IS {fd(r_i)}"
        if f == "vr4":
            mm = main & OOS & np.isfinite(F[f]) & np.isfinite(B)
            perm = rf.placebo_diff(F[f][mm], B[mm], cell[mm], lo_f[mm], hi_f[mm], n_perm=N_PERM, seed=SEED)
            line += f" | placebo p {float((1 + np.sum(perm >= r_o['est'])) / (1 + perm.size)):.4f}"
            pvals["VR4"] = r_o["p"]
        emit(line)
    hp = holm(list(pvals.values()))
    emit("  Holm on 2 (descriptive): " + ", ".join(f"{n} p1 {p:.3f} -> {h:.3f}" for (n, p), h in zip(pvals.items(), hp, strict=True)))

    emit("\n== Descriptive: costs and stop rule (ER30) ==")
    for col in ("B15", "Bx"):
        emit(f"  {col}: OOS {fd(delta(T[col], k, main & OOS, month))} | IS {fd(delta(T[col], k, main & IS, month))}")
    emit(
        "  E[B15] of T1, OOS: "
        + (lambda r: f"{r['est']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] n {r['n']}")(
            rf.boot_mean(
                T["B15"][main & OOS & (k == 1) & np.isfinite(T["B15"])], month[main & OOS & (k == 1) & np.isfinite(T["B15"])], N_BOOT, SEED
            )
        )
    )

    emit("\n== Descriptive: splits (Δ ER30, bootstrap over months) ==")
    for nm, m in (("4h", T["tf"] == "4h"), ("1d", T["tf"] == "1d"), ("long", T["side"] == "long"), ("short", T["side"] == "short")):
        emit(f"  {nm}: OOS {fd(delta(B, k, main & OOS & m, month))} | IS {fd(delta(B, k, main & IS & m, month))}")
    for y in range(2021, 2027):
        emit(f"  {y}: {fd(delta(B, k, main & (T['year'] == y), month))}")

    emit("\n== Descriptive: without the 5 coins with the largest contribution to Δ (OOS) ==")
    mm = main & OOS & np.isfinite(B)
    n1, n3 = int((mm & (k == 1)).sum()), int((mm & (k == 3)).sum())
    contrib: dict[str, float] = {}
    for s, yv, kk in zip(T["sym"][mm], B[mm], k[mm], strict=True):
        if kk == 1:
            contrib[s] = contrib.get(s, 0.0) + yv / n1
        elif kk == 3:
            contrib[s] = contrib.get(s, 0.0) - yv / n3
    top5 = sorted(contrib, key=lambda s: -contrib[s])[:5]
    emit(f"  top-5 contributors {[(str(s), round(float(contrib[s]), 4)) for s in top5]}")
    emit(f"  without them: {fd(delta(B, k, main & OOS & ~np.isin(T['sym'], top5), month))}")

    for col in ("stop_pct", "rr"):
        emit(f"\n== Descriptive: inside {col} tertiles (IS cuts per tf) ==")
        lo_s, hi_s, _ = cuts_by_tf(T[col].astype(float), T, main & IS)
        ks = rf.tertile_codes(T[col].astype(float), lo_s, hi_s)
        ests, ws = [], []
        for t in (1, 2, 3):
            r = delta(B, k, main & OOS & (ks == t), month)
            emit(f"  {col} tertile {t}: OOS {fd(r)}")
            ests.append(r["est"])
            ws.append(r["n_a"] + r["n_b"])
        emit(f"  weighted mean of within-stratum Δ (OOS): {np.average(ests, weights=ws):+.4f}")

    emit("\n== Descriptive: top-100 slice (P100) ==")
    p100 = top100(a, M)
    emit(
        f"  n 2021+ {int(p100.sum())}, OOS {int((p100 & OOS).sum())}; OOS {fd(delta(B, k, p100 & OOS, month))} | IS {fd(delta(B, k, p100 & IS, month))}"
    )

    emit("\n== 5a: per coin ==")
    per_asset(T, B, k, main, IS, OOS, month)
    emit(f"\npeak RSS {rss_mb():.0f} MB")


def coin_deltas(
    T: dict[str, np.ndarray], B: np.ndarray, k: np.ndarray, m: np.ndarray, min_n: int = 5
) -> dict[str, tuple[float, float, int]]:
    """Per coin: (Δ T1 - T3, its i.i.d. SE, n) for coins with >= min_n trades in both T1 and T3."""
    out = {}
    syms = T["sym"]
    for s in np.unique(syms[m]):
        mm = m & (syms == s) & np.isfinite(B)
        a, b = B[mm & (k == 1)], B[mm & (k == 3)]
        if a.size >= min_n and b.size >= min_n:
            out[str(s)] = (float(a.mean() - b.mean()), math.sqrt(a.var(ddof=1) / a.size + b.var(ddof=1) / b.size), int(a.size + b.size))
    return out


def per_asset(
    T: dict[str, np.ndarray], B: np.ndarray, k: np.ndarray, main: np.ndarray, IS: np.ndarray, OOS: np.ndarray, month: np.ndarray
) -> None:
    allc = coin_deltas(T, B, k, main)
    d = np.array([v[0] for v in allc.values()])
    se = np.array([v[1] for v in allc.values()])
    q, df, i2 = cochran_q(d, se)
    emit(f"  coins with >= 5 trades in T1 and T3 (2021+): {len(allc)}; Cochran Q {q:.1f} df {df}, I² {i2:.2f}")
    top = sorted(allc.items(), key=lambda kv: -kv[1][0])
    emit("  best 5 / worst 5 by Δ (2021+): " + ", ".join(f"{s} {v[0]:+.2f} (n {v[2]})" for s, v in top[:5] + top[-5:]))
    isc = coin_deltas(T, B, k, main & IS)
    lead = [s for s, _ in sorted(isc.items(), key=lambda kv: -kv[1][0])[:10]]
    inl = np.isin(T["sym"], lead)
    emit(f"  IS leaders (coins with >= 5 in T1 and T3 on IS: {len(isc)}): {lead}")
    emit(f"  OOS Δ leaders: {fd(delta(B, k, main & OOS & inl, month))}")
    emit(f"  OOS Δ rest:    {fd(delta(B, k, main & OOS & ~inl, month))}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("smoke", "features", "run"), required=True)
    ap.add_argument("--cache-dir", default=str(HOME / "s3-range-efficiency"))
    ap.add_argument("--passport-cache", default=str(HOME / "setup_passport"))
    ap.add_argument("--s1-cache", default=str(HOME / "course_s14_ema"))
    ap.add_argument("--data-dir", default=str(HOME / "funding_crowding"))
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT", help="smoke: symbols to time")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry", action="store_true", help="run: replace R with seeded noise (code check, no result)")
    a = ap.parse_args()
    out = Path(a.cache_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.stage}{'-dry' if a.dry else ''}.txt").open("w") as fh:
        OUT["fh"] = fh
        emit(f"# verify_s3_range_efficiency --stage {a.stage} at {datetime.now(UTC).isoformat(timespec='seconds')}")
        {"smoke": stage_smoke, "features": stage_features, "run": stage_run}[a.stage](a, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
