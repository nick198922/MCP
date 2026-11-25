#!/usr/bin/env python3
"""Delta absorption on the S3 signal bar (Trader_XO / KillaXBT «CVD / delta», «absorption»), a module of S3
(IDEA-20261113-x-traders, item 5; registration: the card's section 5.7).

    uv run python scripts/verify_delta_absorption.py --stage fetch    # 1h taker klines of the months the trades need
    uv run python scripts/verify_delta_absorption.py --stage rows     # per-symbol feature rows of the S3 trades (no R)
    uv run python scripts/verify_delta_absorption.py --stage zero     # stage 0: coverage, sanity, shares, MDE; no outcome
    uv run python scripts/verify_delta_absorption.py --stage report   # the registered test (after the registration commit)
    uv run python scripts/verify_delta_absorption.py --stage report --dry [--dry-plant 0.3]
                                                                      # code check on seeded synthetic R -> report_dry_*.txt

The S3 trades, their R, the demo selection, the occupancy chain and the samples P / P100 are the S3 passport's
(``verify_setup_passport.py S3 --stage run``), joined by (symbol, timeframe, side, entry time) as in
``verify_s3_modules.py``. The signal bar of a trade is ``[t_in - bar, t_in)``; its taker delta comes from Binance
USD-M monthly 1h klines with the aggressor columns (``rt.binance_vision.um_klines_taker_monthly``, per-file JSON cache).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_kx4_levels as vkx  # noqa: E402  (dci, per-asset 5a, a-priori MDE scaling of the S3 module wave)
import verify_s3_modules as vsm  # noqa: E402  (samples P / P100, join key, ΔOI of H5)
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import delta_absorption as da  # noqa: E402
from tradedesk.analytics import s3_modules as mo  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402

HOUR_MS = da.HOUR_MS
N_PLACEBO = 200
SEED = 20261125
INTEREST = 0.05
GATE_MDE = 0.25  # registration 5.7: above 5 x the interest the means are not computed
# registration 5.7 (amended before the run): S3-wide guard p1 < 0.05 / 20; hypotheses ever tested on the S3 trades:
# wave 30.10 (5), 10a (2), 11.11 (2), 5.2 (1), 5.4 (2), demo-context-filters H1-H4 (4), demo-trend-veto H1-H3 (3), 5.7 (1)
S3_GUARD_K = 20
MODULE_HINT = 0.10  # amendment: at Δs ≈ +0.05R the flagged trades stay at or below 0; a module needs Δs ≳ 0.10R
ADJ_COVARS = ("body", "lrr", "lstop")  # condition (13): oriented body, log R:R, log stop (% of price), slopes per TF
DRY_SEED = 777
CACHE = Path.home() / ".cache/tradedesk-agents"
emit = vsp.emit


def month_of(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m")


def months_between(t0: int, t1: int) -> list[str]:
    """Calendar months touched by [t0, t1)."""
    out, d = [], datetime.fromtimestamp(t0 / 1000, UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp((t1 - 1) / 1000, UTC)
    while d <= end:
        out.append(d.strftime("%Y-%m"))
        d = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
    return out


def trade_months(tf: str, t_in: int) -> list[str]:
    bar = vsp.HOURS[tf] * HOUR_MS
    return months_between(t_in - bar - da.PLACEBO_MAX_DAYS * da.DAY_MS, t_in)


def trades(a: argparse.Namespace) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], np.ndarray]:
    T, S = vsm.samples(a)
    need = (S["P"] | S["P100"]) & (T["t_in"] >= vsp.PASSPORT_MS)
    return T, S, need


# ---------------------------------------------------------------- fetch


def stage_fetch(a: argparse.Namespace, out: Path) -> None:
    import verify_liquidation_flush as vlf  # noqa: PLC0415  (per-file JSON cache in front of the archive provider)

    from tradedesk.server import build_server  # noqa: PLC0415

    T, _, need = trades(a)
    files: set[tuple[str, str]] = set()
    for s, tf, t in zip(T["sym"][need], T["tf"][need], T["t_in"][need], strict=True):
        for m in trade_months(str(tf), int(t)):
            files.add((str(s), m))
    todo = sorted(f for f in files if not (out / "um1h" / f[0] / f"{f[1]}.json").exists())
    if a.limit:
        todo = todo[: a.limit]
    emit(f"[fetch] trades {int(need.sum())}, symbol-months {len(files)}, to fetch {len(todo)}; rss {vsp.rss_mb():.0f} MB")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    async def go() -> None:
        _, rt = build_server()
        try:
            fx = vlf.Fetcher(rt, out, workers=a.workers * 4)
            jobs = []
            for s, m in todo:

                async def one(s: str = s, m: str = m) -> Any:
                    return (await rt.binance_vision.um_klines_taker_monthly(s, "1h", int(m[:4]), int(m[5:]))).data

                jobs.append((out / "um1h" / s / f"{m}.json", one))
            await fx.many("um1h-taker", jobs)
        finally:
            await rt.aclose()

    t0 = time.time()
    asyncio.run(go())
    emit(f"FETCH COMPLETE in {time.time() - t0:.0f}s; rss {vsp.rss_mb():.0f} MB")


# ---------------------------------------------------------------- per-symbol feature rows (no R)


def load_taker(out: Path, s: str, months: list[str]) -> np.ndarray:
    rows: list[list[float]] = []
    for m in months:
        p = out / "um1h" / s / f"{m}.json"
        if p.exists():
            rows.extend(json.loads(p.read_text()) or [])
    if not rows:
        return np.zeros((0, 9))
    r = np.array(rows, dtype=float)
    r = r[np.argsort(r[:, 0], kind="stable")]
    return r[np.concatenate([[True], r[1:, 0] != r[:-1, 0]])]


def run_symbol(args: tuple[str, list[tuple[str, str, str, int]], str, str, str]) -> dict[str, Any]:
    s, tr, out_dir, s1_cache, data_dir = args
    t0 = time.time()
    out = Path(out_dir)
    months = sorted({m for _, tf, _, t in tr for m in trade_months(tf, t)})
    R = load_taker(out, s, months)
    t = R[:, da.T_].astype(np.int64)
    base, tbb = R[:, da.V_], R[:, da.TB_]
    vw_ok = da.taker_vwap_inside(R) if R.size else np.zeros(0, dtype=bool)
    h1, qv = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    pt = h1.t.astype(np.int64)
    rows = []
    for key, tf, side, t_in in tr:
        bar = vsp.HOURS[tf] * HOUR_MS
        a0 = t_in - bar
        w = slice(int(np.searchsorted(t, a0)), int(np.searchsorted(t, t_in)))
        pw = slice(int(np.searchsorted(pt, a0)), int(np.searchsorted(pt, t_in)))
        d = da.bar_delta(t, base, tbb, a0, t_in)
        low_or = R[:, da.L_] if side == "long" else -R[:, da.H_]
        nh = t[w].size
        q_t, q_p = float(R[w, da.QV_].sum()) if nh else math.nan, float(qv[pw].sum()) if pt[pw].size else math.nan
        cand = da.candidate_deltas(t, base, tbb, a0, bar, side)
        rows.append(
            {
                "k": key,
                "d": None if not math.isfinite(d) else d,
                "nh": nh,
                "nhp": int(pt[pw].size),
                "qrel": None if not (q_p > 0 and math.isfinite(q_t)) else abs(q_t - q_p) / q_p,
                "cl_ok": bool(nh and pt[pw].size and abs(R[w.stop - 1, da.C_] - h1.c[pw.stop - 1]) <= 1e-9 * abs(h1.c[pw.stop - 1])),
                "vw_ok": bool(nh and vw_ok[w].all()),
                "tb_ok": bool(nh and np.all(tbb[w] <= base[w] * (1 + 1e-9)) and np.all(tbb[w] >= 0)),
                "xh": None if not math.isfinite(x := da.extreme_hour_delta(t, low_or, base, tbb, a0, t_in, side)) else x,
                "body": None
                if not nh
                else (None if not math.isfinite(b := da.oriented_body(R[w.start, da.O_], R[w.stop - 1, da.C_], side)) else b),
                "cand": [None if not math.isfinite(x) else round(float(x), 6) for x in cand],
            }
        )
    (out / "rows").mkdir(parents=True, exist_ok=True)
    (out / "rows" / f"{s}.json").write_text(json.dumps(rows, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "sec": time.time() - t0, "rss": vsp.rss_mb()}


def stage_rows(a: argparse.Namespace, out: Path) -> None:
    T, _, need = trades(a)
    by: dict[str, list[tuple[str, str, str, int]]] = defaultdict(list)
    for k, s, tf, sd, t in zip(T["key"][need], T["sym"][need], T["tf"][need], T["side"][need], T["t_in"][need], strict=True):
        by[str(s)].append((str(k), str(tf), str(sd), int(t)))
    syms = sorted(by)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [(s, by[s], str(out), str(a.s1_cache), str(a.data_dir)) for s in syms if a.force or not (out / "rows" / f"{s}.json").exists()]
    emit(f"[rows] {len(jobs)} of {len(syms)} symbols to do, {a.workers} workers")
    t0, n = time.time(), 0
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for k, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            n += r["n"]
            if k % 25 == 0 or k == len(jobs):
                el = time.time() - t0
                emit(f"  [rows] {k}/{len(jobs)} {el:.0f}s eta {el / k * (len(jobs) - k):.0f}s; rows {n}; worker rss {r['rss']:.0f} MB")
    emit(f"[rows] done in {time.time() - t0:.0f}s; rows {n}; rss {vsp.rss_mb():.0f} MB")


# ---------------------------------------------------------------- joined table


def load(a: argparse.Namespace, out: Path) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Passport rows with the feature columns (nan where no feature row), the registered flag and the H2 flag."""
    T, S = vsm.samples(a)
    mod: dict[str, dict[str, Any]] = {}
    for p in sorted((out / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            mod[r["k"]] = r
    n = T["t_in"].size
    T["has"] = np.array([k in mod for k in T["key"]])
    for c in ("d", "xh", "body", "qrel", "nh", "nhp"):
        T[c] = np.array([math.nan if (x := mod.get(k, {}).get(c)) is None else float(x) for k in T["key"]])
    for c in ("cl_ok", "vw_ok", "tb_ok"):
        T[c] = np.array([bool(mod.get(k, {}).get(c, False)) for k in T["key"]])
    T["cand"] = [
        np.array([math.nan if x is None else x for x in mod[k]["cand"]], dtype=float) if k in mod else np.zeros(0) for k in T["key"]
    ]
    sg = np.where(T["side"] == "long", 1.0, -1.0)
    T["od"] = sg * T["d"]
    T["raw"] = np.isfinite(T["od"]) & (T["od"] < 0)  # first-written definition, exploratory since stage 0 (see 5.7)
    T["exc"] = np.array([da.excess_delta(float(T["od"][i]), T["cand"][i]) for i in range(n)])
    T["def"] = np.isfinite(T["exc"]) & np.isfinite(T["body"])
    T["A"] = T["def"] & (T["exc"] < da.THRESHOLD)
    h2: dict[str, Any] = {}
    for p in sorted((a.s3m_cache / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            h2[r["k"]] = r.get("H2")
    T["H2"] = np.array([h2.get(k) is True for k in T["key"]])
    T["H2def"] = np.array([h2.get(k) is not None for k in T["key"]])
    T["month"] = T["t_in"].astype("datetime64[ms]").astype("datetime64[M]").astype(np.int64)
    period = T["t_in"] >= vsp.PASSPORT_MS
    IS = period & (T["t_in"] < vsp.OOS_MS)
    # registered strata: timeframe x quintile of the oriented bar return, edges from P100 IS per timeframe (features only)
    T["body_q"] = np.full(n, -1, dtype=np.int64)
    for j, tf in enumerate(("4h", "1d")):
        mt = T["tf"] == tf
        q = mo.quintile_of(T["body"][mt], mo.quintile_edges(T["body"][S["P100"] & IS & mt & T["def"]]))
        T["body_q"][mt] = np.where(q >= 0, q + 5 * j, -1)
    with np.errstate(invalid="ignore", divide="ignore"):  # rr / stop_pct are entry features (known at the signal)
        T["lrr"] = np.log(np.asarray(T["rr"], dtype=float))
        T["lstop"] = np.log(np.asarray(T["stop_pct"], dtype=float))
    T["tf_i"] = np.where(T["tf"] == "1d", 1, 0)
    return T, S


def dry_outcomes(T: dict[str, np.ndarray], plant: float) -> None:
    """--dry: overwrite every outcome column with seeded synthetic R right after loading, before any statistic (noise
    like an S3 trade: -1 or +2 with p 2/3 and 1/3, plus N(0, 0.2), minus 0.05; ``plant`` R added to flagged trades),
    so nothing a dry run prints relates to real trade outcomes."""
    rng = np.random.default_rng(DRY_SEED)
    n = T["t_in"].size
    noise = rng.choice([-1.0, 2.0], n, p=[2 / 3, 1 / 3]) + rng.normal(0, 0.2, n) - 0.05
    for k in ("Bx", "L", "L15"):
        T[k] = noise.copy()
    for k in ("T", "T_x", "D", "K", "K_c"):
        if k in T:
            T[k] = np.full(n, math.nan)
    T["B"] = noise + plant * T["A"]
    T["B15"] = T["B"] - 0.02


def balance(T: dict[str, np.ndarray], M: np.ndarray) -> None:
    """Features only: flagged vs unflagged medians of the oriented body, R:R and stop (% of price) per registered
    stratum, and the pooled gap of each covariate of condition (13) as ``stratified_delta`` of the feature over its
    SD on M (the direction in which an R that depends on the feature would shift Δs)."""
    A, bq = T["A"], T["body_q"]
    emit("  balance (features only; registered strata 4h q0..q4, 1d q5..q9): stratum · n · A · body A/notA · R:R A/notA · stop % A/notA")
    for g in range(10):
        m = M & (bq == g)
        fa, fu = m & A, m & ~A
        if not (fa.any() and fu.any()):
            emit(f"    {g} · {int(m.sum())} · {int(fa.sum())} · one side empty")
            continue
        emit(
            f"    {g} · {int(m.sum())} · {int(fa.sum())} · {np.median(T['body'][fa]):+.4f}/{np.median(T['body'][fu]):+.4f} · "
            f"{np.median(T['rr'][fa]):.2f}/{np.median(T['rr'][fu]):.2f} · {100 * np.median(T['stop_pct'][fa]):.2f}/{100 * np.median(T['stop_pct'][fu]):.2f}"
        )
    gaps = []
    for c in ADJ_COVARS:
        x = T[c][M]
        gaps.append(f"{c} {da.stratified_delta(x / np.std(x), A[M], bq[M]):+.3f}")
    emit("  covariate gap Δs(feature) / SD (0 = balanced): " + ", ".join(gaps))


def delta_oi(a: argparse.Namespace, T: dict[str, np.ndarray], m: np.ndarray) -> np.ndarray:
    """ΔOI on the signal bar as in H5 of the S3 wave (``vsm.oi_change_of``), nan before the OI archive."""
    ns = SimpleNamespace(cache_dir=a.s3m_cache, oi_cache=a.oi_cache)
    out = np.full(T["t_in"].size, math.nan)
    idx = np.flatnonzero(m & (T["t_in"] >= vsm.OI_START))
    idx = idx[np.argsort(T["sym"][idx], kind="stable")]
    memo: dict[tuple[str, str], Any] = {}
    last = None
    for i in idx:
        s = str(T["sym"][i])
        if s != last:
            memo.clear()
            last = s
        out[i] = vsm.oi_change_of(ns, s, str(T["tf"][i]), int(T["t_in"][i]), memo)
    return out


def trader_syms() -> set[str]:
    import csv  # noqa: PLC0415

    out: set[str] = set()
    for name in ("Trader_XO", "KillaXBT"):
        lines = [ln for ln in (ROOT / f"docs/calls/x/{name}.csv").open() if not ln.startswith("#")]
        out |= {f"{r['asset'].strip().upper()}USDT" for r in csv.DictReader(lines) if (r.get("asset") or "").strip()}
    return out


def qtl(x: np.ndarray, qs: tuple[float, ...] = (0.01, 0.1, 0.5, 0.9, 0.99)) -> str:
    x = x[np.isfinite(x)]
    return " / ".join(f"{v:+.3f}" for v in np.quantile(x, qs)) if x.size else "n/a"


# ---------------------------------------------------------------- stage 0 (no outcome metric)


def stage_zero(a: argparse.Namespace, out: Path) -> None:
    T, S = load(a, out)
    period = T["t_in"] >= vsp.PASSPORT_MS
    IS, OOS = period & (T["t_in"] < vsp.OOS_MS), T["t_in"] >= vsp.OOS_MS
    emit(
        f"# Delta absorption, stage 0 (counts, coverage, feature distribution only; no outcome metric). Passport S3 candidates {T['t_in'].size}"
    )
    for name in ("P100", "P"):
        M = S[name] & period
        H = M & T["has"]
        D = M & T["def"]
        emit(
            f"\n## {name} 2021-01 … 2026-09: n {int(M.sum())} (IS {int((M & IS).sum())}, OOS {int((M & OOS).sum())}); feature row {int(H.sum())}, delta defined {int(D.sum())} ({D.sum() / max(M.sum(), 1):.4f})"
        )
        hrs = np.array([vsp.HOURS[str(x)] for x in T["tf"]])
        emit(
            f"  undefined: no taker row for any hour {int((H & (T['nh'] == 0)).sum())}, incomplete hours {int((H & (T['nh'] > 0) & (T['nh'] < hrs)).sum())}, "
            f"other (bad values / V = 0 / baseline < 50 % of the 6–28 d bars) {int((H & (T['nh'] == hrs) & ~T['def']).sum())}"
        )
        emit(
            "  coverage by year (defined / n): "
            + " · ".join(f"{y} {int((D & (T['year'] == y)).sum())}/{int((M & (T['year'] == y)).sum())}" for y in range(2021, 2027))
        )
        emit(
            f"  sanity on defined: close of the last hour = passport close {int((D & T['cl_ok']).sum())}/{int(D.sum())}; passport hours = taker hours "
            f"{int((D & (T['nhp'] == T['nh'])).sum())}; taker buy ≤ volume {int((D & T['tb_ok']).sum())}; taker-buy VWAP in [low, high] {int((D & T['vw_ok']).sum())}"
        )
        emit(f"  |Σ quote volume taker archive − passport| / passport: p50 / p99 / max {qtl(T['qrel'][D], (0.5, 0.99, 1.0))}")
        for tf in ("4h", "1d"):
            mt = D & (T["tf"] == tf)
            emit(
                f"  oriented delta {tf} (n {int(mt.sum())}) p1 / p10 / p50 / p90 / p99: {qtl(T['od'][mt])}; taker-buy share B/V p50 {np.nanmedian((T['d'][mt] + 1) / 2):.4f}"
            )
        Hd = H & np.isfinite(T["od"])
        emit(
            f"  first-written raw flag od < 0 (exploratory since stage 0): {T['raw'][Hd].mean():.3f} of {int(Hd.sum())}; long {T['raw'][Hd & (T['side'] == 'long')].mean():.3f}, "
            f"short {T['raw'][Hd & (T['side'] == 'short')].mean():.3f}; by year "
            + " · ".join(f"{y} {T['raw'][Hd & (T['year'] == y)].mean():.3f}" for y in range(2021, 2027))
        )
        f = T["A"][D]
        emit(
            f"  ABSORPTION (excess od < 0): {int(f.sum())} of {int(D.sum())} ({f.mean():.3f}); IS {int((T['A'] & D & IS).sum())}/{int((D & IS).sum())}, "
            f"OOS {int((T['A'] & D & OOS).sum())}/{int((D & OOS).sum())}; weeks with a flag {np.unique(T['week'][D & T['A']]).size}, months {np.unique(T['month'][D & T['A']]).size}"
        )
        vf = da.stratified_var_factor(T["A"][D], T["body_q"][D])
        m0 = vkx.mde_scaled(float(f.mean()), int(D.sum()))
        fo = T["A"][D & OOS]
        emit(
            f"    a-priori MDE80 ≈ {m0:.3f}R plain, ×√{vf:.2f} stratified = {m0 * math.sqrt(vf):.3f}R "
            f"(OOS only {vkx.mde_scaled(float(fo.mean()), int(fo.size)) * math.sqrt(da.stratified_var_factor(fo, T['body_q'][D & OOS])):.3f}R); "
            f"gate {GATE_MDE}R, interest {INTEREST}R; excess od p10 / p50 / p90 {qtl(T['exc'][D], (0.1, 0.5, 0.9))}"
        )
        emit(
            "    share by year: "
            + " · ".join(f"{y} {T['A'][D & (T['year'] == y)].mean():.3f}" for y in range(2021, 2027) if (D & (T["year"] == y)).any())
            + f"; long {T['A'][D & (T['side'] == 'long')].mean():.3f} (n {int((D & (T['side'] == 'long')).sum())}), short {T['A'][D & (T['side'] == 'short')].mean():.3f} "
            f"(n {int((D & (T['side'] == 'short')).sum())}); 4h {T['A'][D & (T['tf'] == '4h')].mean():.3f}, 1d {T['A'][D & (T['tf'] == '1d')].mean():.3f}"
        )
        bq = T["body_q"]
        emit(
            "    body strata (4h q0..q4, 1d q5..q9; q0 = weakest oriented close): flag share "
            + " · ".join(f"{g} {T['A'][D & (bq == g)].mean():.2f}/{int((D & (bq == g)).sum())}" for g in range(10))
            + f"; Spearman(excess od, body) {spearman(T['exc'][D], T['body'][D]):+.2f}, Spearman(od, body) {spearman(T['od'][D], T['body'][D]):+.2f}"
        )
        idx = np.flatnonzero(D)
        pl = da.placebo_pick([T["cand"][i] for i in idx], list(T["key"][D]), 20, SEED)
        base = np.array([np.median(T["cand"][i][np.isfinite(T["cand"][i])]) for i in idx])
        pf = ((pl - base[:, None]) < da.THRESHOLD).mean(axis=0)
        emit(
            f"    placebo (same coin, bar 6–28 d earlier, excess vs the same baseline; 20 of {N_PLACEBO} sets): flag share mean {pf.mean():.3f} "
            f"(min {pf.min():.3f}, max {pf.max():.3f}); Spearman(excess od, placebo excess set 0) {spearman(T['exc'][D], pl[:, 0] - base):+.2f}"
        )
        both = D & T["H2def"]
        emit(
            f"    overlap with H2: H2 share {T['H2'][both].mean():.3f}; A & H2 {int((T['A'] & T['H2'] & both).sum())}, A & not H2 {int((T['A'] & ~T['H2'] & both).sum())}"
        )
        # exploratory variants (shares only)
        q20 = {tf: np.quantile(T["exc"][S["P100"] & IS & T["def"] & (T["tf"] == tf)], 0.2) for tf in ("4h", "1d")}
        v_q = D & np.array([T["exc"][i] <= q20[str(T["tf"][i])] for i in range(T["exc"].size)])
        emit(
            f"    variants (exploratory): IS-quintile excess ≤ ({q20['4h']:+.4f} 4h, {q20['1d']:+.4f} 1d) {v_q[D].mean():.3f}; "
            f"raw od < 0 {np.mean(T['raw'][D]):.3f}; sweep-hour od < 0 {np.mean(T['xh'][D] < 0):.3f} (defined {int(np.isfinite(T['xh'][D]).sum())})"
        )
        if name == "P100":
            doi = delta_oi(a, T, D)
            ok = D & np.isfinite(doi)
            h5 = np.zeros(T["od"].size, dtype=bool)
            for tf in ("4h", "1d"):
                mt = T["tf"] == tf
                edges = mo.quintile_edges(doi[ok & IS & mt]) if (ok & IS & mt).any() else None
                if edges is not None:
                    h5[mt] = mo.quintile_of(doi[mt], edges) == 0
            emit(
                f"    overlap with H5 (ΔOI on the signal bar, defined {int(ok.sum())}): Spearman(excess od, ΔOI) {spearman(T['exc'][ok], doi[ok]):+.2f}; edges from P100 IS; "
                f"H5 (bottom quintile) share among flagged {h5[ok & T['A']].mean():.3f} vs unflagged {h5[ok & ~T['A']].mean():.3f}"
            )
            balance(T, D)
            cov_ok = D & np.all(np.isfinite(np.column_stack([T[c] for c in ADJ_COVARS])), axis=1)
            emit(
                f"  condition (13) covariates finite: {int(cov_ok.sum())} of {int(D.sum())} (dropped {1 - cov_ok.sum() / max(D.sum(), 1):.4f})"
            )
        tr = D & np.isin(T["sym"], list(trader_syms()))
        emit(f"  traders' coins (Trader_XO + KillaXBT CSV assets): defined {int(tr.sum())}, flagged {int((tr & T['A']).sum())}")
    btc = S["P"] & period & T["def"] & (T["sym"] == "BTCUSDT")
    emit(f"\nBTCUSDT signal bars (P): n {int(btc.sum())}, taker-buy share B/V median {np.nanmedian((T['d'][btc] + 1) / 2):.4f}")
    emit(f"\npeak rss {vsp.rss_mb():.0f} MB")


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3:
        return math.nan
    rx = np.argsort(np.argsort(x[ok]))
    ry = np.argsort(np.argsort(y[ok]))
    return float(np.corrcoef(rx, ry)[0, 1])


# ---------------------------------------------------------------- report (the registered test)


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T, S = load(a, out)
    if a.dry:
        dry_outcomes(T, a.dry_plant)
        emit(
            f"# DRY RUN: every R is seeded synthetic noise (seed {DRY_SEED}) + {a.dry_plant:+.2f}R on flagged trades; nothing below is a real outcome"
        )
    period = T["t_in"] >= vsp.PASSPORT_MS
    IS, OOS = period & (T["t_in"] < vsp.OOS_MS), T["t_in"] >= vsp.OOS_MS
    M = S["P100"] & period & T["def"]
    fl = T["A"]
    f = float(np.mean(fl[M]))
    mde0 = vkx.mde_scaled(f, int(M.sum())) * math.sqrt(da.stratified_var_factor(fl[M], T["body_q"][M]))
    emit(f"# Delta absorption report. P100 2021-01 … 2026-09 with delta: n {int(M.sum())}, flagged {int((fl & M).sum())} ({f:.3f})")
    emit(f"MDE gate: a-priori stratified MDE80 {mde0:.3f}R vs {GATE_MDE}R")
    balance(T, M)
    if not (np.isfinite(mde0) and mde0 <= GATE_MDE):
        emit("Verdict: ◯ (MDE gate; no means, CI or placebo differences computed)")
        return
    rw, rm = strat(T, "B", fl, M), strat(T, "B", fl, M, "month")
    emit(
        f"PRIMARY Δs = E[R − E[R | stratum] | absorption]: {vkx.fd(rw)} p₁ {rw['p1']:.4f} (week clusters); month [{rm['lo']:+.3f}; {rm['hi']:+.3f}]"
    )
    rplain = vkx.dci(T, "B", fl, M)
    emit(f"  plain Δ = E[R | absorption] − E[R]: {vkx.fd(rplain)}")
    ea = float(np.mean(T["B"][M]))
    ef = sp.mean_ci(T["B"][M & fl], T["week"][M & fl], n_boot=vsp.N_BOOT, seed=SEED)
    ef15 = sp.mean_ci(T["B15"][M & fl], T["week"][M & fl], n_boot=vsp.N_BOOT, seed=SEED)
    eu = sp.mean_ci(T["B"][M & ~fl], T["week"][M & ~fl], n_boot=vsp.N_BOOT, seed=SEED)
    emit(f"  E[R] flagged {vkx.fd(ef, 'm')}, unflagged {vkx.fd(eu, 'm')}, all {ea:+.3f}; flagged ×1.5 {vkx.fd(ef15, 'm')}")
    idx = np.flatnonzero(M)
    pl = da.placebo_pick([T["cand"][i] for i in idx], list(T["key"][M]), N_PLACEBO, SEED)
    base = np.array([np.median(T["cand"][i][np.isfinite(T["cand"][i])]) for i in idx])
    bq = T["body_q"][M]
    fm = fl[M]
    dpl = np.array([da.stratified_delta(T["B"][M], da.matched_flags(pl[:, j] - base, bq, fm), bq) for j in range(N_PLACEBO)])
    pp = vkx.kx.placebo_p(rw["d"], dpl)
    emit(
        f"  placebo (same coin, bar 6–28 d earlier, same baseline and strata, {N_PLACEBO} sets): Δs median {np.nanmedian(dpl):+.3f}, "
        f"p95 {np.nanquantile(dpl, 0.95):+.3f}; p {pp:.4f}; observed − placebo median {rw['d'] - np.nanmedian(dpl):+.3f}"
    )
    ri, ro, r15 = strat(T, "B", fl, M & IS), strat(T, "B", fl, M & OOS), strat(T, "B15", fl, M)
    rP = strat(T, "B", fl, S["P"] & period & T["def"])

    def sd_(m: np.ndarray, key: str = "B") -> float:
        return da.stratified_delta(T[key][m], fl[m], T["body_q"][m])

    years = {y: sd_(M & (T["year"] == y)) for y in range(2021, 2027)}
    sides = {s: sd_(M & (T["side"] == s)) for s in ("long", "short")}
    res = da.stratified_resid(T["B"], np.where(M, T["body_q"], -1))
    mt, top = vsm.drop_top(T, np.where(fl & M, res, 0.0), M)
    d_top = sd_(mt)
    emit(
        f"  IS {vkx.fd(ri)} (n {int(ri['n'])}); OOS {vkx.fd(ro)} (n {int(ro['n'])}); ×1.5 {r15['d']:+.3f}; P {vkx.fd(rP)} (n {int(rP['n'])})"
    )
    emit(
        "  years: "
        + " · ".join(f"{y} {v:+.3f}" for y, v in years.items())
        + f"; long {sides['long']:+.3f}, short {sides['short']:+.3f}; without top-5 coins {d_top:+.3f} ({', '.join(top)})"
    )
    for tf in ("4h", "1d"):
        m2 = M & (T["tf"] == tf)
        emit(f"  {tf}: {vkx.fd(strat(T, 'B', fl, m2))} (n {int(m2.sum())})")
    # max(x, nan) returns x in Python: an undefined SE of either clustering must leave the MDE undefined (card: "◯")
    mde = 2.8 * max(rw["se"], rm["se"]) if np.isfinite(rw["se"]) and np.isfinite(rm["se"]) else math.nan
    emit(f"  MDE80 = 2.8 × max(SE week {rw['se']:.3f}, month {rm['se']:.3f}) = {mde:.3f}R; two-sided p {rw['p']:.4f}")
    # condition (13): Δs of R net of the linear part of body, log R:R and log stop within the registered strata
    X = np.column_stack([T[c] for c in ADJ_COVARS])
    T["B_adj"], betas = da.covariate_adjusted(np.where(M, T["B"], math.nan), X, np.where(M, T["body_q"], -1), T["tf_i"])
    Mc = M & np.isfinite(T["B_adj"])
    radj = strat(T, "B_adj", fl, Mc)
    emit(
        f"  (13) covariate-adjusted Δs (body, log R:R, log stop; slopes per TF, fixed in the bootstrap): {vkx.fd(radj)} (n {int(Mc.sum())} of {int(M.sum())}); "
        + "; ".join(f"{('4h', '1d')[g]} β " + " / ".join(f"{b:+.3f}" for b in be) for g, be in betas.items())
    )
    emit(f"  5a (Δ unstratified: flagged − all of the coin, not the primary Δs): {vkx.per_asset(T, fl, M, IS, OOS)}")
    per_coin_table(T, fl, M)
    p1 = rw["p1"] if np.isfinite(rw["p1"]) else 1.0
    cond = {
        "CI week > 0": rw["lo"] > 0,
        "CI month > 0": rm["lo"] > 0,
        "Holm (m = 1) p1 < 0.05": p1 < 0.05,
        f"S3 guard p1 < 0.05/{S3_GUARD_K}": p1 < 0.05 / S3_GUARD_K,
        "placebo p < 0.05": pp < 0.05,
        "IS > 0 and OOS > 0": ri["d"] > 0 and ro["d"] > 0,
        ">= 4 of 6 years > 0": sum(1 for v in years.values() if np.isfinite(v) and v > 0) >= 4,
        "×1.5 > 0": r15["d"] > 0,
        "long > 0 and short > 0": all(np.isfinite(v) and v > 0 for v in sides.values()),
        "without top-5 coins > 0": d_top > 0,
        "P > 0": rP["d"] > 0,
        "plain (unstratified) Δ > 0": rplain["d"] > 0,
        "(13) covariate-adjusted Δs CI week > 0": radj["lo"] > 0,
    }
    v = da.verdict(mde0, all(cond.values()), rw["d"], p1, mde, rw["hi"], GATE_MDE, INTEREST)
    emit(f"\n## Verdict: Δs {rw['d']:+.3f}, week CI hi {rw['hi']:+.3f}, p₁ {p1:.4f}, MDE80 {mde:.3f} → {v}")
    emit("  " + "; ".join(f"{k}: {'yes' if ok else 'no'}" for k, ok in cond.items()))
    if np.isfinite(rw["p"]) and rw["p"] < 0.05 and rw["d"] < 0:
        emit("  Δs significantly < 0 (two-sided p < 0.05): a new hypothesis for a new card, not a verdict of this one")
    emit(
        f"  module for an S3 version (✓ and lower CI of E[R] flagged ×1.5 > 0; expected only at Δs ≳ {MODULE_HINT}R): "
        f"{'yes' if v == '✓' and ef15['lo'] > 0 else 'no'}"
    )
    emit("\n## Variants (exploratory, outside Holm; P100 2021+)")
    q20 = {tf: np.quantile(T["exc"][S["P100"] & IS & T["def"] & (T["tf"] == tf)], 0.2) for tf in ("4h", "1d")}
    v_q = np.array([bool(T["def"][i] and T["exc"][i] <= q20[str(T["tf"][i])]) for i in range(T["exc"].size)])
    variants = {
        "raw od < 0 (first-written definition)": T["raw"],
        "IS-quintile excess (strongest 20 %)": v_q,
        "sweep-hour od < 0": T["xh"] < 0,
        "A within H2": T["A"],
    }
    for lab, vf in variants.items():
        m2 = M & (T["H2"] if lab == "A within H2" else True)
        r = strat(T, "B", vf, m2)
        emit(
            f"{lab}: n {int(m2.sum())}, flagged {int(r['n'])}; Δs {vkx.fd(r)}; plain {vkx.dci(T, 'B', vf, m2)['d']:+.3f}; "
            f"IS {strat(T, 'B', vf, m2 & IS)['d']:+.3f}, OOS {strat(T, 'B', vf, m2 & OOS)['d']:+.3f}"
        )
    m2 = S["P"] & period & T["def"] & np.isin(T["sym"], list(trader_syms()))
    emit(f"traders' coins (sample P): n {int(m2.sum())}; Δs {vkx.fd(strat(T, 'B', fl, m2))}")
    btc = m2 & np.isin(T["sym"], ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    emit(f"Trader_XO majors BTC / ETH / SOL (sample P): n {int(btc.sum())}; Δs {vkx.fd(strat(T, 'B', fl, btc))}")
    emit(f"\npeak rss {vsp.rss_mb():.0f} MB")


def strat(T: dict[str, np.ndarray], key: str, flag: np.ndarray, m: np.ndarray, cl: str = "week") -> dict[str, float]:
    """The registered statistic: body-stratified Δ with a cluster bootstrap (week, or month for the second CI)."""
    r = da.stratified_delta_ci(T[key][m], flag[m], T["body_q"][m], T[cl][m], n_boot=vsp.N_BOOT, seed=SEED)
    r["p1"] = mo.one_sided_p(r["d"], r["p"])
    r["n"] = float((flag & m).sum())
    return r


def per_coin_table(T: dict[str, np.ndarray], fl: np.ndarray, M: np.ndarray, min_n: int = 30) -> None:
    """5a (a): per-coin n, E[R] net of flagged / unflagged and Δ with a week-cluster CI (coins with >= min_n trades)."""
    emit(f"  per coin (n >= {min_n}; Δ unstratified, not the primary Δs): coin · n · flagged · E[R] flagged · E[R] unflagged · Δ [95 % CI]")
    for s in sorted(np.unique(T["sym"][M]), key=lambda x: -int((M & (T["sym"] == x)).sum())):
        ms_ = M & (T["sym"] == s)
        if ms_.sum() < min_n or not (ms_ & fl).any() or not (ms_ & ~fl).any():
            continue
        r = vkx.dci(T, "B", fl, ms_)
        emit(
            f"    {s} · {int(ms_.sum())} · {int((ms_ & fl).sum())} · {T['B'][ms_ & fl].mean():+.3f} · {T['B'][ms_ & ~fl].mean():+.3f} · {vkx.fd(r)}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("fetch", "rows", "zero", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=CACHE / "delta_absorption")
    ap.add_argument("--passport-cache", type=Path, default=CACHE / "setup_passport")
    ap.add_argument("--s3m-cache", type=Path, default=CACHE / "s3_modules")
    ap.add_argument("--oi-cache", type=Path, default=CACHE / "oi_liquidation_flush")
    ap.add_argument("--s1-cache", type=Path, default=CACHE / "course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=CACHE / "funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry", action="store_true", help="report on seeded synthetic R (never the real outcomes); own output file")
    ap.add_argument("--dry-plant", type=float, default=0.0, help="--dry: R added to flagged trades (0 = pure noise)")
    a = ap.parse_args()
    if a.dry and a.stage != "report":
        ap.error("--dry applies to --stage report only")
    out = a.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    name = f"report_dry_{a.dry_plant:+.2f}" if a.dry else a.stage
    with (out / f"{name}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"verify_delta_absorption --stage {a.stage}{' --dry' if a.dry else ''} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"fetch": stage_fetch, "rows": stage_rows, "zero": stage_zero, "report": stage_report}[a.stage](a, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
