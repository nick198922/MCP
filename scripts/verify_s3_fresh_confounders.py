#!/usr/bin/env python3
"""Item 10c of IDEA-20261030-demo-setups-depth, step 1: is the "fresh" S3 range edge of 10a a feature of its own or a
proxy for the stop distance, R:R, the age of the edge and the width of the range? Descriptive, on the trades 10a
already computed (registration: the card's section «Попередня реєстрація 10c», ``git add``-ed before any run).

    uv run python scripts/verify_s3_fresh_confounders.py --stage features   # edge age + range width per signal (no R)
    uv run python scripts/verify_s3_fresh_confounders.py --stage report     # tables (also <cache>/report.txt)

No network: 1h klines from the cache of ``verify_course_s14_ema.py``; trades and R from the 10a / passport caches.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_s3_fresh_extreme as fx  # noqa: E402  (10a: samples, classes, keys)
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import fresh_confounders as fc  # noqa: E402
from tradedesk.analytics import s3_modules as mo  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, resample  # noqa: E402
from tradedesk.analytics.setup_signals import RANGE_W  # noqa: E402
from tradedesk.analytics.spot_perp_flow import contrast as lin_contrast  # noqa: E402
from tradedesk.analytics.spot_perp_flow import ols_cluster_cov  # noqa: E402
from tradedesk.analytics.stop_management import chi2_sf, cochran_q  # noqa: E402

TFS = ("2h", "8h", "12h", "4h", "1d")
CONF = ("stop_pct", "rr", "age", "width")
emit = vsp.emit


# ---------------------------------------------------------------- features (no R)


def feat_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir = args
    t0 = time.time()
    det = vsp.SETUPS["S3"][0]
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    feats: dict[str, list[float]] = {}
    bad = 0
    for tf in TFS:
        tb = resample(h1, fx.HOURS[tf])
        oriented = {"long": tb, "short": tb.mirrored()}
        for sig in det(tb, tf):
            t_in = sig.signal_ts + tb.bar_ms
            if not (vsp.START_MS <= t_in < fx.END[tf]):
                continue
            bb = oriented[sig.side]
            i = int(np.searchsorted(bb.t, sig.signal_ts))
            ind = sig.indicators
            edge = ind["range_low"] if sig.side == "long" else -ind["range_high"]
            if i < RANGE_W or abs(float(bb.low[i - RANGE_W : i].min()) - edge) > 1e-9 * max(1.0, abs(edge)):
                bad += 1
                continue
            age = fc.edge_age(bb.low, i, RANGE_W)
            width = (ind["range_high"] - ind["range_low"]) / ind["atr14"]
            feats[fx.key_of(s, tf, sig.side, t_in)] = [age, round(width, 6)]
    (Path(out_dir) / "feat").mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "feat" / f"{s}.json").write_text(json.dumps({"feats": feats, "bad": bad}, separators=(",", ":")))
    return {"sym": s, "n": len(feats), "bad": bad, "sec": time.time() - t0, "rss": vsp.rss_mb()}


def stage_features(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "feat" / f"{s}.json").exists()]
    emit(f"[features] {len(jobs)} of {len(syms)} symbols to do, {a.workers} workers")
    t0, n, bad = time.time(), 0, 0
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for k, r in enumerate(pool.imap_unordered(feat_symbol, jobs), 1):
            n, bad = n + r["n"], bad + r["bad"]
            if k % 25 == 0 or k == len(jobs):
                el = time.time() - t0
                emit(
                    f"  [features] {k}/{len(jobs)} {el:.0f}s eta {el / k * (len(jobs) - k):.0f}s; signals {n}; edge mismatches {bad}; worker rss {r['rss']:.0f} MB"
                )
    emit(f"[features] done in {time.time() - t0:.0f}s; signals {n}; edge mismatches {bad}")


# ---------------------------------------------------------------- report


def load_feats(out: Path) -> dict[str, list[float]]:
    f: dict[str, list[float]] = {}
    for p in sorted((out / "feat").glob("*.json")):
        f.update(json.loads(p.read_text())["feats"])
    return f


def attach(T: dict[str, np.ndarray], feats: dict[str, list[float]]) -> None:
    keys = [fx.key_of(str(s), str(tf), str(sd), int(t)) for s, tf, sd, t in zip(T["sym"], T["tf"], T["side"], T["t_in"], strict=True)]
    T["age"] = np.array([feats[k][0] if k in feats else math.nan for k in keys], dtype=float)
    T["width"] = np.array([feats[k][1] if k in feats else math.nan for k in keys], dtype=float)


def adjusted(T: dict[str, np.ndarray], cls: np.ndarray, m: np.ndarray, key: str = "B") -> dict[str, float]:
    """Registered Δ_adj: OLS R ~ fresh + tf x year + side + quintiles (within tf) of the four confounders, CR1 by week."""
    sel = m & ((cls == mo.FRESH) | (cls == mo.FAR)) & np.isfinite(T[key])
    for c in CONF:
        sel &= np.isfinite(T[c])
    idx = np.flatnonzero(sel)
    tf = T["tf"][idx].astype(str)
    fresh = cls[idx] == mo.FRESH
    tfy = np.array([f"{a}|{b}" for a, b in zip(tf, T["year"][idx], strict=True)])
    nested = [(tf, fc.quantile_codes(T[c][idx], tf, 5)) for c in CONF]
    X = fc.design(fresh, [tfy, T["side"][idx].astype(str)], nested)
    fit = ols_cluster_cov(T[key][idx], X, T["week"][idx])
    e = np.zeros(X.shape[1])
    e[1] = 1.0
    r = lin_contrast(fit, e)
    return {
        "d": r["est"],
        "se": r["se"],
        "lo": r["est"] - 1.96 * r["se"],
        "hi": r["est"] + 1.96 * r["se"],
        "p": r["p"],
        "n": float(idx.size),
        "nf": float(fresh.sum()),
    }


def fmt_adj(r: dict[str, float]) -> str:
    return (
        f"{r['d']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] p {r['p']:.4f}, MDE₈₀ {2.8 * r['se']:.3f} (n {int(r['n'])}, свіжих {int(r['nf'])})"
    )


def strat_one(T: dict[str, np.ndarray], cls: np.ndarray, m: np.ndarray, conf: str | None) -> dict[str, float]:
    sel = m & ((cls == mo.FRESH) | (cls == mo.FAR)) & np.isfinite(T["B"])
    if conf is not None:
        sel &= np.isfinite(T[conf])
    idx = np.flatnonzero(sel)
    tf = T["tf"][idx].astype(str)
    strata = tf if conf is None else np.array([f"{a}|{b}" for a, b in zip(tf, fc.quantile_codes(T[conf][idx], tf, 5), strict=True)])
    return fc.stratified_diff_ci(
        T["B"][idx], cls[idx] == mo.FRESH, cls[idx] == mo.FAR, strata, T["week"][idx], n_boot=vsp.N_BOOT, seed=vsp.SEED
    )


def balance(T: dict[str, np.ndarray], cls: np.ndarray, m: np.ndarray, tfs: tuple[str, ...]) -> None:
    for tf in tfs:
        mt = m & (T["tf"] == tf)
        f, r = mt & (cls == mo.FRESH), mt & (cls == mo.FAR)
        parts = [f"{c} {np.nanmedian(T[c][f]):.4g} / {np.nanmedian(T[c][r]):.4g}" for c in CONF]
        emit(f"    {tf} (свіжі {int(f.sum())} / далекі {int(r.sum())}), медіани свіжі / далекі: " + "; ".join(parts))


def block(T: dict[str, np.ndarray], cls: np.ndarray, m: np.ndarray, label: str, tfs: tuple[str, ...], full: bool) -> dict[str, float]:
    emit(f"\n## {label}")
    miss = m & ((cls == mo.FRESH) | (cls == mo.FAR)) & ~np.isfinite(T["age"])
    emit(f"  угод свіжі + далекі без ознак віку / ширини: {int(miss.sum())}")
    emit("  баланс домішок (stop_pct частка ціни; rr після комісій; age свічок ТФ; width у ATR14):")
    balance(T, cls, m, tfs)
    raw = strat_one(T, cls, m, None)
    emit(f"  сира Δ (страти = ТФ): {raw['d']:+.3f} [{raw['lo']:+.3f}; {raw['hi']:+.3f}]")
    for c in CONF:
        r = strat_one(T, cls, m, c)
        emit(f"  Δ, стратифікована за {c} (квінтиль × ТФ): {r['d']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}]")
    adj = adjusted(T, cls, m)
    emit(f"  Δ_adj (OLS, усі чотири домішки + ТФ × рік + бік): {fmt_adj(adj)}")
    if full:
        emit(f"  Δ_adj ×1.5: {fmt_adj(adjusted(T, cls, m, 'B15'))}")
        for tf in tfs:
            emit(f"  Δ_adj {tf}: {fmt_adj(adjusted(T, cls, m & (T['tf'] == tf)))}")
        emit(f"  Δ_adj IS: {fmt_adj(adjusted(T, cls, m & (T['t_in'] < vsp.OOS_MS)))}")
        emit(f"  Δ_adj OOS: {fmt_adj(adjusted(T, cls, m & (T['t_in'] >= vsp.OOS_MS)))}")
        m5, top = fx.drop_top5(T, cls, m)
        emit(f"  Δ_adj без топ-5 монет ({', '.join(top)}): {fmt_adj(adjusted(T, cls, m5))}")
        for sd in ("long", "short"):
            emit(f"  Δ_adj {sd}: {fmt_adj(adjusted(T, cls, m & (T['side'] == sd)))}")
    return adj


def per_asset_resid(T: dict[str, np.ndarray], cls: np.ndarray, m: np.ndarray) -> None:
    """5a: residuals of the regression without the fresh term; per-coin fresh-minus-far of residuals."""
    sel = m & ((cls == mo.FRESH) | (cls == mo.FAR)) & np.isfinite(T["B"])
    for c in CONF:
        sel &= np.isfinite(T[c])
    idx = np.flatnonzero(sel)
    tf = T["tf"][idx].astype(str)
    tfy = np.array([f"{a}|{b}" for a, b in zip(tf, T["year"][idx], strict=True)])
    X = fc.design(
        np.zeros(idx.size, dtype=bool), [tfy, T["side"][idx].astype(str)], [(tf, fc.quantile_codes(T[c][idx], tf, 5)) for c in CONF]
    )
    X = np.delete(X, 1, axis=1)
    beta, *_ = np.linalg.lstsq(X, T["B"][idx], rcond=None)
    res = T["B"][idx] - X @ beta
    fresh = cls[idx] == mo.FRESH
    sym = T["sym"][idx].astype(str)
    is_ = T["t_in"][idx] < vsp.OOS_MS
    sd = float(res.std(ddof=1))
    means, ses, d_is = [], [], {}
    for s in np.unique(sym):
        ms = sym == s
        nf, nr = int((ms & fresh).sum()), int((ms & ~fresh).sum())
        if nf >= 5 and nr >= 5:
            means.append(res[ms & fresh].mean() - res[ms & ~fresh].mean())
            ses.append(sd * math.sqrt(1 / nf + 1 / nr))
        mi = ms & is_
        if (mi & fresh).sum() >= 5 and (mi & ~fresh).sum() >= 5:
            d_is[s] = res[mi & fresh].mean() - res[mi & ~fresh].mean()
    q, df, i2 = cochran_q(np.array(means), np.array(ses))
    top = [s for s, _ in sorted(d_is.items(), key=lambda x: -x[1])[:10]]
    oos = ~is_

    def dd(mm: np.ndarray) -> tuple[float, int]:
        return (float(res[mm & fresh].mean() - res[mm & ~fresh].mean()) if (mm & fresh).any() and (mm & ~fresh).any() else math.nan), int(
            mm.sum()
        )

    a_top, n_top = dd(oos & np.isin(sym, top))
    a_rest, n_rest = dd(oos & ~np.isin(sym, top))
    emit(
        f"  5a (залишки без «свіжий»): монет (≥ 5 / ≥ 5) {len(means)}, Cochran Q {q:.1f} (df {df}, p {chi2_sf(q, df):.3f}), I² {i2:.2f}; "
        f"топ-10 за Δ на IS → OOS Δ {a_top:+.3f} (n {n_top}) проти решти {a_rest:+.3f} (n {n_rest})"
    )


def stage_report(a: argparse.Namespace, out: Path) -> None:
    feats = load_feats(out)
    emit(f"# S3 fresh edge vs confounders (10c, step 1, descriptive). Features for {len(feats)} signals")
    # ---- D2 exactly as 10a
    T2 = vsp.load_rows(a.fresh_cache)
    T2["d"] = np.array(T2["d"], dtype=object)
    c2 = fx.classes(T2["d"])
    demo2 = (T2["rr"] >= vsp.MIN_RR) & (T2["stop_pct"] <= vsp.MAX_STOP) & (T2["score"] >= vsp.MIN_SCORE)
    turn2 = np.nan_to_num(T2["turn"], nan=-1) >= 5e6
    D2 = np.zeros(T2["B"].size, dtype=bool)
    for tf in fx.NEW_TFS:
        D2 |= vsp.chain(T2, (T2["tf"] == tf) & demo2 & turn2 & (T2["t_in"] >= fx.D2_START) & (T2["t_in"] < fx.END[tf]))
    attach(T2, feats)
    emit(
        f"D2: {int(D2.sum())} trades (10a: 54 970); свіжі {int((D2 & (c2 == mo.FRESH)).sum())} (10a 15 720), далекі {int((D2 & (c2 == mo.FAR)).sum())} (10a 31 586)"
    )
    adj = block(T2, c2, D2, "D2 (2h / 8h / 12h, 2021-01 … 2026-09) — головна вибірка кроку 1", fx.NEW_TFS, True)
    per_asset_resid(T2, c2, D2)
    # ---- 4h / 1d from the passport cache (P = 2021+, the finding's own data; D1 = 2020)
    T = vsp.load_rows(a.passport_cache / "S3")
    keys = [fx.key_of(str(s), str(tf), str(sd), int(t)) for s, tf, sd, t in zip(T["sym"], T["tf"], T["side"], T["t_in"], strict=True)]
    h2d: dict[str, Any] = {}
    for p in sorted((a.modules_cache / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            h2d[r["k"]] = r.get("H2d")
    T["d"] = np.array([h2d.get(k) for k in keys], dtype=object)
    c1 = fx.classes(T["d"])
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    P = vsp.chain(T, (T["t_in"] >= vsp.PASSPORT_MS) & demo & turn5)
    D1 = vsp.chain(T, (T["t_in"] >= vsp.START_MS) & (T["t_in"] < vsp.PASSPORT_MS) & demo & turn5)
    attach(T, feats)
    emit(f"\n4h / 1d: P {int(P.sum())} угод, D1 {int(D1.sum())} угод")
    block(T, c1, P, "4h / 1d 2021+ (вибірка P розбивки H2 — дані самої знахідки)", ("4h", "1d"), False)
    block(T, c1, D1, "D1: 4h / 1d 2020", ("4h", "1d"), False)
    # ---- registered decision for step 1 (D2)
    if adj["lo"] <= 0 <= adj["hi"] and adj["d"] <= 0.036:
        dec = "проксі (Δ ≈ 0): крок 2 не потрібен"
    elif adj["lo"] > 0:
        dec = "окрема ознака (нижня межа CI > 0): крок 2 — спершу MDE₈₀"
    else:
        dec = "не розрізнено (CI містить 0, точкова > +0.036): крок 2 — спершу MDE₈₀"
    emit(f"\nРІШЕННЯ КРОКУ 1 (D2, за реєстрацією): Δ_adj {adj['d']:+.3f} [{adj['lo']:+.3f}; {adj['hi']:+.3f}] → {dec}")


# ---------------------------------------------------------------- step 2: 2020 on 1h / 2h, power before any mean

CUT_2020 = vsp.s1.ms("2021-01-10T00:00")  # 1h data kept up to here: entries < 2021-01-01 + 42 h holding + margin
_orig_load = vsp.load_1h_qv


def _load_2020(cache: Path, data_dir: Path, s: str) -> tuple[Any, np.ndarray]:
    b, qv = _orig_load(cache, data_dir, s)
    j = int(np.searchsorted(b.t, CUT_2020))
    return b.slice(0, j), qv[:j]


def stage_h1(a: argparse.Namespace, out: Path) -> None:
    """10a's ``run_symbol`` on 1h for entries 2020-02-01 .. 2020-12-31 (1h data cut at 2021-01-10; all causal)."""
    fx.HOURS["1h"] = 1
    fx.HOLD["1h"] = fx.HOLD_BARS * HOUR_MS
    fx.END["1h"] = vsp.PASSPORT_MS
    vsp.load_1h_qv = _load_2020  # inherited by the forked workers
    syms = [s for s in vsp.syms_with_1h(a.s1_cache, a.data_dir) if any((a.s1_cache / "k1h" / s).glob("2020-*.json"))]
    if a.limit:
        syms = syms[: a.limit]
    emit(f"[h1] symbols with 1h data in 2020: {len(syms)}; hold {fx.HOLD['1h'] / HOUR_MS:.0f} h")
    fx.run_pool(a, out / "h1_2020", syms, ("1h",))


def stage_power(a: argparse.Namespace, out: Path) -> None:
    """Registered: n, weeks, SE of Δ fresh − far with R centred within class (no means are printed), MDE₈₀ = 2.8·SE."""
    T1 = vsp.load_rows(out / "h1_2020")
    T2 = vsp.load_rows(a.fresh_cache)
    m_all = []
    for T, tf in ((T1, "1h"), (T2, "2h")):
        T["d"] = np.array(T["d"], dtype=object)
        demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
        turn = np.nan_to_num(T["turn"], nan=-1) >= 5e6
        m = vsp.chain(T, (T["tf"] == tf) & demo & turn & (T["t_in"] >= vsp.START_MS) & (T["t_in"] < vsp.PASSPORT_MS))
        cls = fx.classes(T["d"])
        f, r = m & (cls == mo.FRESH), m & (cls == mo.FAR)
        m_all.append((T["B"][f | r], (cls == mo.FRESH)[f | r], T["week"][f | r], T["sym"][f | r], tf))
        emit(f"  {tf} 2020: угод {int(m.sum())}, свіжі {int(f.sum())}, далекі {int(r.sum())}")
    v = np.concatenate([x[0] for x in m_all])
    fr = np.concatenate([x[1] for x in m_all])
    wk = np.concatenate([x[2] for x in m_all])
    sym = np.concatenate([x[3] for x in m_all])
    vc = v.copy()
    vc[fr] -= v[fr].mean()
    vc[~fr] -= v[~fr].mean()
    r = sp.diff_ci(vc, fr, ~fr, wk, n_boot=vsp.N_BOOT, seed=vsp.SEED)
    se = (r["hi"] - r["lo"]) / 3.92
    emit(
        f"POWER 2020 1h + 2h: свіжі {int(fr.sum())}, далекі {int((~fr).sum())}, тижнів {np.unique(wk).size}, монет {np.unique(sym).size}; "
        f"SE Δ {se:.3f}; MDE₈₀ {2.8 * se:.3f} → {'лише форвард (MDE₈₀ > 0.10R)' if 2.8 * se > 0.10 else 'прогін T'}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("features", "report", "h1", "power"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s3_fresh_confounders")
    ap.add_argument("--fresh-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/s3_fresh_extreme")
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
        emit(f"verify_s3_fresh_confounders --stage {a.stage} at {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC")
        {"features": stage_features, "report": stage_report, "h1": stage_h1, "power": stage_power}[a.stage](a, out)
        vsp.OUT["fh"] = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
