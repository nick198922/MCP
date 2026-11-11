"""S14: the market's volatility state (BTC 1h realized 7-day vol as a percentile of its own trailing 180 days) as a
weight for the score (IDEA-20261030-demo-setups-depth, item «S14, стан волатильності ринку як вага»; registration
«Попередня реєстрація — S14, стан волатильності»).

    uv run python scripts/verify_s14_vol_state.py --stage smoke        # BTC series checks, counts, feature, no R
    uv run python scripts/verify_s14_vol_state.py --stage run --dry    # code check with R replaced by noise
    uv run python scripts/verify_s14_vol_state.py --stage run          # MDE80 gate, then the registered test

No network: the S14 passport's candidate files (``<passport-cache>/S14/cands``, rule B, demo costs) and the BTCUSDT
1h cache of the passport (``course_s14_ema/k1h``). Every stage writes its output to ``<cache-dir>/<stage>.txt``.
R is per trade risk, not on capital.
"""

from __future__ import annotations

import argparse
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import verify_setup_passport as vsp  # passport rows, masks, chain, loaders (read-only reuse)
from common_helpers import rss_mb
from verify_s3_range_efficiency import coin_deltas, fd

from tradedesk.analytics import range_efficiency as rf
from tradedesk.analytics import vol_state as vs
from tradedesk.analytics.course_s14_ema import resample
from tradedesk.analytics.liquidation_flush import holm
from tradedesk.analytics.stop_management import cochran_q

SETUP = "S14"
SEED = 20261030
N_BOOT = 10_000
N_BOOT_RHO = 2_000
MDE_GATE = 0.15  # registered: the interesting T1 - T3 difference, R
SHIFT_DAYS = 90  # registered placebo shift
NULL_SHIFTS = [s * d for d in range(60, 361, 30) for s in (1, -1)]  # descriptive null band, 22 shifts
KNOWN = (6039, 1868)  # passport S14 verdict sample: 2021+ / OOS
HOME = Path.home() / ".cache/tradedesk-agents"
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


# ---------------------------------------------------------------- feature (no outcome)


def btc_series(a: argparse.Namespace) -> dict[str, Any]:
    """BTCUSDT 1h bars and the hourly feature series: registered p180 and the descriptive variants."""
    h1, _ = vsp.load_1h_qv(Path(a.s1_cache), Path(a.data_dir), "BTCUSDT")
    t, c = np.asarray(h1.t, dtype=np.int64), np.asarray(h1.c, dtype=float)
    rv7 = vs.realized_vol(t, c, window=168, min_count=160)
    rv30 = vs.realized_vol(t, c, window=720, min_count=684)
    d180, d365 = 180 * vs.DAY_MS, 365 * vs.DAY_MS
    S = {
        "p180": vs.trailing_percentile(t, rv7, d180, int(0.9 * 180 * 24)),
        "rv7": rv7,
        "p180_rv30": vs.trailing_percentile(t, rv30, d180, int(0.9 * 180 * 24)),
        "p365": vs.trailing_percentile(t, rv7, d365, int(0.9 * 365 * 24)),
    }
    return {"t": t, "c": c, "rv30": rv30, "S": S, "bars": h1}


def row_feature(B: dict[str, Any], name: str, t_in: np.ndarray, shift_days: int = 0) -> np.ndarray:
    return vs.value_at(B["t"], B["S"][name], t_in + shift_days * vs.DAY_MS)


def passport_masks(a: argparse.Namespace) -> dict[str, Any]:
    """The S14 passport rows and the registered verdict sample (top-250 >= $1M + demo selection, chain), no outcome."""
    T = vsp.load_rows(Path(a.passport_cache) / SETUP)
    floor = vsp.SETUPS[SETUP][2]
    period = T["t_in"] >= vsp.PASSPORT_MS
    sel = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn = np.nan_to_num(T["turn"], nan=-1) >= floor
    qv = vsp.s1.daily_qv(Path(a.s1_cache), Path(a.data_dir), vsp.s1.eligible_syms(Path(a.data_dir)))
    in_top = vsp.in_prev_day_top(T, vsp.top_n_days(qv, vsp.WIDE_TOP_N))
    V = vsp.chain(T, period & sel & turn & in_top, vsp.BAR_CAP.get(SETUP))
    oos = T["t_in"] >= vsp.OOS_MS
    month = np.array([(lambda d: d.year * 12 + d.month - 1)(datetime.fromtimestamp(t / 1000, UTC)) for t in T["t_in"]])
    return {"T": T, "V": V, "OOS": oos, "IS": ~oos, "month": month}


def tertiles(x: np.ndarray, is_m: np.ndarray) -> tuple[np.ndarray, tuple[float, float]]:
    """Registered: pooled IS cuts (both timeframes), applied everywhere; codes 1..3, 0 = undefined."""
    lo, hi = rf.tertile_cuts(x[is_m & np.isfinite(x)])
    return rf.tertile_codes(x, lo, hi), (lo, hi)


def ym(m: int) -> str:
    return f"{m // 12}-{m % 12 + 1:02d}"


# ---------------------------------------------------------------- stages


def stage_smoke(a: argparse.Namespace, out: Path) -> None:
    t0 = time.monotonic()
    B = btc_series(a)
    t, c = B["t"], B["c"]
    holes = np.diff(t) != vs.HOUR_MS
    emit(
        f"BTCUSDT 1h: bars {t.size}, {datetime.fromtimestamp(t[0] / 1000, UTC):%Y-%m-%d} .. {datetime.fromtimestamp(t[-1] / 1000, UTC):%Y-%m-%d %H:%M}; "
        f"holes {int(holes.sum())} (missing hours {int(((np.diff(t)[holes] // vs.HOUR_MS) - 1).sum())}); series in {time.monotonic() - t0:.1f} s; peak RSS {rss_mb():.0f} MB"
    )
    for d in ("2021-01-01", "2022-11-09", "2025-03-01"):  # price-level check (known: ~29k, ~16-18k, ~84-86k)
        i = int(np.searchsorted(t, vsp.s1.ms(f"{d}T00:00")))
        emit(f"  close at {d} 00:00 UTC: {c[i]:.0f}")
    for y in range(2020, 2027):
        m = (t >= vsp.s1.ms(f"{y}-01-01T00:00")) & (t < vsp.s1.ms(f"{y + 1}-01-01T00:00"))
        rv = B["S"]["rv7"][m]
        emit(
            f"  {y}: RV7 median {np.nanmedian(rv) if np.isfinite(rv).any() else math.nan:.3f}, p180 defined {int(np.isfinite(B['S']['p180'][m]).sum())}/{int(m.sum())}"
        )
    # consistency: RV30 from 1h vs from daily closes of the same bars (sampled daily)
    d1 = resample(B["bars"], 24)
    rvd = vs.realized_vol(d1.t, d1.c, window=30, min_count=29, bar_ms=vs.DAY_MS, periods_per_year=365.0)
    rvh = vs.value_at(t, B["rv30"], np.asarray(d1.t, dtype=np.int64) + vs.DAY_MS)
    ok = np.isfinite(rvd) & np.isfinite(rvh)
    emit(
        f"  RV30 1h vs 1d closes (daily, n {int(ok.sum())}): Spearman {rf.spearman(rvh[ok], rvd[ok]):+.3f}, median ratio 1h/1d {np.median(rvh[ok] / rvd[ok]):.3f}"
    )

    M = passport_masks(a)
    T, V, IS, OOS, month = M["T"], M["V"], M["IS"], M["OOS"], M["month"]
    emit(
        f"\npassport S14 rows {T['t_in'].size}; verdict sample (known {KNOWN[0]} / OOS {KNOWN[1]}): 2021+ {int(V.sum())}, IS {int((V & IS).sum())}, OOS {int((V & OOS).sum())}"
    )
    emit(f"months: IS {np.unique(month[V & IS]).size}, OOS {np.unique(month[V & OOS]).size}; peak RSS {rss_mb():.0f} MB")
    x = row_feature(B, "p180", T["t_in"])
    emit(
        f"p180 undefined in V: {int((V & ~np.isfinite(x)).sum())} (IS {int((V & IS & ~np.isfinite(x)).sum())}, OOS {int((V & OOS & ~np.isfinite(x)).sum())})"
    )
    k, cuts = tertiles(x, V & IS)
    emit(f"IS cuts (pooled 4h + 1d): {cuts[0]:.4f} / {cuts[1]:.4f}")
    for nm, m in (("IS", V & IS), ("OOS", V & OOS)):
        emit(f"  {nm}: T1 {int((m & (k == 1)).sum())}, T2 {int((m & (k == 2)).sum())}, T3 {int((m & (k == 3)).sum())}")
        for tf in ("4h", "1d"):
            mm = m & (T["tf"] == tf)
            emit(f"    {tf}: T1 {int((mm & (k == 1)).sum())}, T2 {int((mm & (k == 2)).sum())}, T3 {int((mm & (k == 3)).sum())}")
        for sd in ("long", "short"):
            mm = m & (T["side"] == sd)
            emit(f"    {sd}: T1 {int((mm & (k == 1)).sum())}, T2 {int((mm & (k == 2)).sum())}, T3 {int((mm & (k == 3)).sum())}")
    emit("OOS months: trades in T1 / T2 / T3")
    for mo in np.unique(month[V & OOS]):
        mm = V & OOS & (month == mo)
        emit(f"  {ym(int(mo))}: {int((mm & (k == 1)).sum())} / {int((mm & (k == 2)).sum())} / {int((mm & (k == 3)).sum())}")
    for nm, m in (("IS", V & IS), ("OOS", V & OOS)):
        ok = m & (k > 0) & (k != 2)
        emit(
            f"clusters (months) with T1 or T3, {nm}: {np.unique(month[ok]).size}; with both: {len(set(month[m & (k == 1)]) & set(month[m & (k == 3)]))}"
        )
    emit("Spearman of p180 with non-outcome columns, V 2021+:")
    for col in ("stop_pct", "rr", "score"):
        mm = V & np.isfinite(x)
        emit(f"  p180 ~ {col}: {rf.spearman(x[mm], np.asarray(T[col][mm], dtype=float)):+.3f}")
    for nm in ("rv7", "p180_rv30", "p365"):
        y = row_feature(B, nm, T["t_in"])
        mm = V & np.isfinite(x) & np.isfinite(y)
        emit(f"  p180 ~ {nm}: {rf.spearman(x[mm], y[mm]):+.3f}; {nm} undefined in V {int((V & ~np.isfinite(y)).sum())}")
    for sh in (SHIFT_DAYS, -SHIFT_DAYS):
        y = row_feature(B, "p180", T["t_in"], sh)
        mm = V & np.isfinite(x) & np.isfinite(y)
        emit(
            f"  placebo shift {sh:+d} d: undefined in V {int((V & ~np.isfinite(y)).sum())} (OOS {int((V & OOS & ~np.isfinite(y)).sum())}); Spearman with p180 {rf.spearman(x[mm], y[mm]):+.3f}"
        )
    emit(f"smoke: no outcome (R) computed; {time.monotonic() - t0:.1f} s; peak RSS {rss_mb():.0f} MB")


def delta(y: np.ndarray, k: np.ndarray, m: np.ndarray, cl: np.ndarray, n_boot: int = N_BOOT) -> dict[str, float]:
    ok = m & np.isfinite(y) & (k > 0)
    return rf.boot_diff(y[ok], (k == 1)[ok], (k == 3)[ok], cl[ok], n_boot=n_boot, seed=SEED)


def diff_means(y: np.ndarray, k: np.ndarray, m: np.ndarray) -> tuple[float, int]:
    ok = m & np.isfinite(y)
    a, b = y[ok & (k == 1)], y[ok & (k == 3)]
    return (float(a.mean() - b.mean()) if a.size and b.size else math.nan), int(a.size + b.size)


def stage_run(a: argparse.Namespace, out: Path) -> None:
    B = btc_series(a)
    M = passport_masks(a)
    T, V, IS, OOS, month = M["T"], M["V"], M["IS"], M["OOS"], M["month"]
    if a.dry:  # code check without the outcome: R columns replaced by seeded noise, the gate is not applied
        rng = np.random.default_rng(1)
        for col in ("B", "B15", "Bx"):
            T[col] = rng.normal(0.0, 1.0, T[col].size)
        emit("DRY RUN: B / B15 / Bx are random noise, nothing below is a result")
    R = T["B"]
    emit(f"verdict sample: 2021+ {int(V.sum())}, IS {int((V & IS).sum())}, OOS {int((V & OOS).sum())} (known {KNOWN[0]} / {KNOWN[1]})")
    x = row_feature(B, "p180", T["t_in"])
    k, cuts = tertiles(x, V & IS)
    emit(f"p180 IS cuts {cuts[0]:.4f} / {cuts[1]:.4f}; undefined in V {int((V & ~np.isfinite(x)).sum())}")
    emit(
        f"OOS tertile sizes T1 {int((V & OOS & (k == 1)).sum())}, T2 {int((V & OOS & (k == 2)).sum())}, T3 {int((V & OOS & (k == 3)).sum())}"
    )

    # ---- gate: MDE80 before any mean
    emit("\n== Gate MDE80 (T1 - T3, cluster-robust SE) ==")
    gate = {}
    for nm, m, cl in (("OOS, months (gate)", V & OOS, month), ("IS, months", V & IS, month), ("OOS, weeks", V & OOS, T["week"])):
        ok = m & np.isfinite(R) & (k > 0)
        se = rf.cluster_se_diff(R[ok], (k == 1)[ok], (k == 3)[ok], cl[ok])
        gate[nm] = rf.mde80(se)
        emit(f"  {nm}: clusters {np.unique(cl[ok & (k != 2)]).size}, SE {se:.4f}, MDE80 {rf.mde80(se):.4f} R")
    if not gate["OOS, months (gate)"] <= MDE_GATE and not a.dry:
        emit(f"\nGATE FAILED: MDE80 {gate['OOS, months (gate)']:.4f} > {MDE_GATE} R -> verdict ◯ (unmeasurable); no means computed")
        return
    emit(f"gate passed: MDE80 {gate['OOS, months (gate)']:.4f} <= {MDE_GATE} R" + (" (dry: not applied)" if a.dry else ""))

    # ---- H1 and the registered secondary rows
    emit("\n== H1: Δ = E[B|T1] - E[B|T3] (bootstrap over months, 10 000) ==")
    h1, h1_is = delta(R, k, V & OOS, month), delta(R, k, V & IS, month)
    emit(f"  OOS (primary): {fd(h1)}")
    emit(f"  IS:            {fd(h1_is)}")
    rho = {}
    for nm, m in (("IS", V & IS), ("OOS", V & OOS)):
        mm = m & np.isfinite(x) & np.isfinite(R)
        rho[nm] = rf.boot_spearman(x[mm], R[mm], month[mm], n_boot=N_BOOT_RHO, seed=SEED)
        emit(f"  Spearman ρ(p180, B) {nm}: {rho[nm]['est']:+.4f} [{rho[nm]['lo']:+.4f}; {rho[nm]['hi']:+.4f}] n {rho[nm]['n']}")
    emit("  E[B] by tertile (bootstrap over months):")
    for nm, m in (("IS", V & IS), ("OOS", V & OOS)):
        cells = []
        for tt in (1, 2, 3):
            mm = m & (k == tt) & np.isfinite(R)
            r = rf.boot_mean(R[mm], month[mm], n_boot=N_BOOT, seed=SEED)
            cells.append(f"T{tt} {r['est']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] n {r['n']}")
        emit(f"    {nm}: " + " | ".join(cells))

    # ---- placebo: the p180 series shifted by +-90 days (cuts recomputed on IS), and the descriptive null band
    emit("\n== Placebo: p180 shifted ==")
    pl = {}
    for sh in (SHIFT_DAYS, -SHIFT_DAYS):
        xs = row_feature(B, "p180", T["t_in"], sh)
        ks, cs = tertiles(xs, V & IS)
        pl[sh] = delta(R, ks, V & OOS, month)
        emit(f"  shift {sh:+d} d (cuts {cs[0]:.3f}/{cs[1]:.3f}): OOS {fd(pl[sh])} | IS {fd(delta(R, ks, V & IS, month))}")
    placebo_ok = all(h1["est"] > pl[sh]["est"] for sh in pl)
    emit(f"  registered placebo passed (Δ OOS real > both shifts): {placebo_ok}")
    null = []
    for sh in NULL_SHIFTS:
        xs = row_feature(B, "p180", T["t_in"], sh)
        ks, _ = tertiles(xs, V & IS)
        d, n = diff_means(R, ks, V & OOS)
        null.append(d)
        emit(f"    null shift {sh:+d} d: Δ OOS {d:+.4f} (n {n})")
    nv = np.array([d for d in null if math.isfinite(d)])
    emit(
        f"  null band (descriptive): {nv.size} shifts, share with Δ >= real {float(np.mean(nv >= h1['est'])):.3f}, p5/p95 {np.quantile(nv, 0.05):+.4f}/{np.quantile(nv, 0.95):+.4f}"
    )

    # ---- decision (registered order)
    if h1["est"] > 0 and h1["lo"] > 0 and h1_is["est"] > 0 and placebo_ok:
        verdict = "✓ weight useful"
    elif h1["hi"] < 0:
        verdict = "✗ opposite sign"
    elif h1["hi"] < MDE_GATE:
        verdict = "✗ useful effect excluded (CI upper < 0.15R)"
    elif h1["est"] > 0 and h1_is["est"] > 0 and rho["IS"]["est"] < 0 and rho["OOS"]["est"] < 0:
        verdict = "≈ direction right, not significant"
    else:
        verdict = "◯ inconclusive"
    emit(f"\nVERDICT (registered rule): {verdict}")

    # ---- robustness / descriptive
    emit("\n== Descriptive: other features (same pipeline, pooled IS cuts) ==")
    pvals = {"p180": h1["p"]}
    for f in ("rv7", "p180_rv30", "p365"):
        xf = row_feature(B, f, T["t_in"])
        kf, cf = tertiles(xf, V & IS)
        r_o = delta(R, kf, V & OOS, month)
        pvals[f] = r_o["p"]
        emit(f"  {f} (cuts {cf[0]:.3f}/{cf[1]:.3f}): OOS {fd(r_o)} | IS {fd(delta(R, kf, V & IS, month))}")
    hp = holm(list(pvals.values()))
    emit("  Holm on 4 (descriptive): " + ", ".join(f"{n} p1 {p:.3f} -> {h:.3f}" for (n, p), h in zip(pvals.items(), hp, strict=True)))

    emit("\n== Descriptive: costs and stop rule ==")
    for col in ("B15", "Bx"):
        emit(f"  {col}: OOS {fd(delta(T[col], k, V & OOS, month))} | IS {fd(delta(T[col], k, V & IS, month))}")

    emit("\n== Descriptive: splits (Δ p180, bootstrap over months) ==")
    for nm, m in (("4h", T["tf"] == "4h"), ("1d", T["tf"] == "1d"), ("long", T["side"] == "long"), ("short", T["side"] == "short")):
        emit(f"  {nm}: OOS {fd(delta(R, k, V & OOS & m, month))} | IS {fd(delta(R, k, V & IS & m, month))}")
    for y in range(2021, 2027):
        mm = V & (T["year"] == y)
        if (mm & (k == 1)).sum() >= 2 and (mm & (k == 3)).sum() >= 2:
            emit(f"  {y}: {fd(delta(R, k, mm, month))}")
        else:
            emit(f"  {y}: T1 n {int((mm & (k == 1)).sum())}, T3 n {int((mm & (k == 3)).sum())} (too few)")

    emit("\n== Descriptive: without the 5 coins with the largest contribution to Δ (OOS) ==")
    mm = V & OOS & np.isfinite(R)
    n1, n3 = int((mm & (k == 1)).sum()), int((mm & (k == 3)).sum())
    contrib: dict[str, float] = {}
    for s, yv, kk in zip(T["sym"][mm], R[mm], k[mm], strict=True):
        if kk == 1:
            contrib[s] = contrib.get(s, 0.0) + yv / n1
        elif kk == 3:
            contrib[s] = contrib.get(s, 0.0) - yv / n3
    top5 = sorted(contrib, key=lambda s: -contrib[s])[:5]
    emit(f"  top-5 contributors {[(str(s), round(float(contrib[s]), 4)) for s in top5]}")
    emit(f"  without them: {fd(delta(R, k, V & OOS & ~np.isin(T['sym'], top5), month))}")

    emit("\n== Descriptive: inside stop_pct tertiles (pooled IS cuts) ==")
    ks, _ = tertiles(T["stop_pct"].astype(float), V & IS)
    ests, ws = [], []
    for tt in (1, 2, 3):
        r = delta(R, k, V & OOS & (ks == tt), month)
        emit(f"  stop_pct tertile {tt}: OOS {fd(r)}")
        ests.append(r["est"])
        ws.append(r["n_a"] + r["n_b"])
    emit(f"  weighted mean of within-stratum Δ (OOS): {np.average(ests, weights=ws):+.4f}")

    emit("\n== 5a: per coin ==")
    allc = coin_deltas(T, R, k, V)
    d = np.array([v[0] for v in allc.values()])
    se = np.array([v[1] for v in allc.values()])
    q, df, i2 = cochran_q(d, se)
    emit(f"  coins with >= 5 trades in T1 and T3 (2021+): {len(allc)}; Cochran Q {q:.1f} df {df}, I² {i2:.2f}")
    top = sorted(allc.items(), key=lambda kv: -kv[1][0])
    emit("  best 5 / worst 5 by Δ (2021+): " + ", ".join(f"{s} {v[0]:+.2f} (n {v[2]})" for s, v in top[:5] + top[-5:]))
    isc = coin_deltas(T, R, k, V & IS)
    lead = [s for s, _ in sorted(isc.items(), key=lambda kv: -kv[1][0])[:10]]
    inl = np.isin(T["sym"], lead)
    emit(f"  IS leaders (coins with >= 5 in T1 and T3 on IS: {len(isc)}): {lead}")
    emit(f"  OOS Δ leaders: {fd(delta(R, k, V & OOS & inl, month))}")
    emit(f"  OOS Δ rest:    {fd(delta(R, k, V & OOS & ~inl, month))}")
    emit(f"\npeak RSS {rss_mb():.0f} MB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    ap.add_argument("--cache-dir", default=str(HOME / "s14-vol-state"))
    ap.add_argument("--passport-cache", default=str(HOME / "setup_passport"))
    ap.add_argument("--s1-cache", default=str(HOME / "course_s14_ema"))
    ap.add_argument("--data-dir", default=str(HOME / "funding_crowding"))
    ap.add_argument("--dry", action="store_true", help="run: replace R with seeded noise (code check, no result)")
    a = ap.parse_args()
    out = Path(a.cache_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.stage}{'-dry' if a.dry else ''}.txt").open("w") as fh:
        OUT["fh"] = fh
        emit(f"# verify_s14_vol_state --stage {a.stage} at {datetime.now(UTC).isoformat(timespec='seconds')}")
        {"smoke": stage_smoke, "run": stage_run}[a.stage](a, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
