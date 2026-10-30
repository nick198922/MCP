#!/usr/bin/env python3
"""S3 modules (IDEA-20261030-demo-setups-depth, item 7b): H10 partial exit, H9 split entry, H1 edge touches, H2
higher-timeframe level, H5 open-interest drop on the sweep bar, each measured against the S3 v3 base on the same
trades. Registration: the card's section «Попередня реєстрація модулів S3» (written before any run).

    uv run python scripts/verify_s3_modules.py --stage run        # per-symbol module rows (no network, 4 workers)
    uv run python scripts/verify_s3_modules.py --stage fetch_oi   # Binance archive metrics for the H5 trade days
    uv run python scripts/verify_s3_modules.py --stage report     # tables (also <cache>/report.txt)

The base trades, the demo selection, the occupancy chain and the samples come from the S3 passport's candidate files
(``verify_setup_passport.py S3 --stage run``); this script joins its module rows to them by (symbol, timeframe,
side, entry time) and checks that its own base R equals the passport's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
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

import verify_setup_passport as vsp  # noqa: E402  (data loaders, samples, chain, CI helpers of the S3 passport)

from tradedesk.analytics import s3_modules as mo  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.oi_flush import archive_obs_times  # noqa: E402
from tradedesk.analytics.setup_passport import limit_trade  # noqa: E402
from tradedesk.analytics.stop_management import chi2_sf, cochran_q, risk_unit, simulate_stop_rule  # noqa: E402

DAY_MS = 24 * HOUR_MS
REST_BARS = 12
MODULES = ("H10", "H9", "H1", "H2", "H5")
N_PLACEBO_SUB = 2000
OI_START = vsp.s1.ms("2021-12-01T00:00")
emit = vsp.emit


def key_of(sym: str, tf: str, side: str, t_in: int) -> str:
    return f"{sym}|{tf}|{side}|{int(t_in)}"


# ---------------------------------------------------------------- per-symbol module rows


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir = args
    t0 = time.time()
    out = Path(out_dir)
    det, tfs, _ = vsp.SETUPS["S3"]
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
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
            far = ind["range_high"] if sig.side == "long" else -ind["range_low"]
            edge = ind["range_low"] if sig.side == "long" else -ind["range_high"]
            ext = sg * ind["extreme"]
            path = h1 if sig.side == "long" else h1.mirrored()
            hold = vsp.HOLD[tf]
            base = simulate_stop_rule(path, t_in, e, st, tg, hold, vsp.RULE_B, fees=vsp.F1)
            if base is None:
                continue
            base15 = simulate_stop_rule(path, t_in, e, st, tg, hold, vsp.RULE_B, fees=vsp.F15)
            k = key_of(s, tf, sig.side, t_in)
            r: dict[str, Any] = {"k": k, "B": base.r_net}
            # H10: partial exit (registered), its placebo and the cost stress
            if far > tg:
                r["H10"] = mo.partial_exit(path, t_in, e, st, tg, far, hold, vsp.F1, base)[0]
                r["H10_15"] = mo.partial_exit(path, t_in, e, st, tg, far, hold, vsp.F15, base15)[0] if base15 else None
                fp = tg + mo.unit_draw(k, "h10") * (far - tg)
                r["H10p"] = mo.partial_exit(path, t_in, e, st, tg, fp, hold, vsp.F1, base)[0] if fp > tg else None
            # H9: split entry at the middle of the wick, placebo at a random depth, cost stress, H9 x H10 combination
            rest = REST_BARS * bar_ms
            lvl = mo.split_level(e, ext, 0.5)
            if st < lvl < e:
                r["H9"], r["H9_f"], _ = mo.split_entry(path, t_in, e, st, tg, lvl, rest, hold, vsp.F1, base, vsp.RULE_B)
                if base15:
                    r["H9_15"] = mo.split_entry(path, t_in, e, st, tg, lvl, rest, hold, vsp.F15, base15, vsp.RULE_B)[0]
                r["H9_ru"] = risk_unit(lvl, st, vsp.F1) / risk_unit(e, st, vsp.F1)
                if far > tg and "H10" in r:
                    t1 = min(t_in + rest, base.exit_ms)
                    o_mid, tk = limit_trade(path, t_in, t1, lvl, st, tg, hold, vsp.RULE_B, vsp.F1, True)
                    o_far, _ = limit_trade(path, t_in, t1, lvl, st, far, hold, mo.runner_rule(lvl, tg, far), vsp.F1, True)
                    leg2 = 0.0
                    if tk >= 0 and o_mid is not None and o_far is not None:
                        leg2 = (0.5 * o_mid.r_net + 0.5 * o_far.r_net) * r["H9_ru"]
                    r["H9x10"] = 0.5 * r["H10"] + 0.5 * leg2
            lp = mo.split_level(e, ext, mo.unit_draw(k, "h9"))
            if st < lp < e:
                r["H9p"], r["H9p_f"], _ = mo.split_entry(path, t_in, e, st, tg, lp, rest, hold, vsp.F1, base, vsp.RULE_B)
            # H1 / H2 on the oriented signal-timeframe bars and 1h bars, causal
            bb = oriented[sig.side]
            i = int(np.searchsorted(bb.t, sig.signal_ts))
            a_prev = float(atrs[sig.side][i - 1]) if i >= 1 else math.nan
            r["H1n"] = mo.edge_touches(bb.low, i, vsp.ss.RANGE_W, a_prev)
            lv = mo.htf_lows(path.t, path.low, sig.signal_ts)
            hit, dist = mo.htf_match(edge, lv, a_prev)
            r["H2"] = bool(hit) if np.isfinite(dist) else None
            r["H2d"] = None if not np.isfinite(dist) else round(dist, 4)
            rows.append(r)
    (out / "rows").mkdir(parents=True, exist_ok=True)
    (out / "rows" / f"{s}.json").write_text(json.dumps(rows, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "sec": time.time() - t0, "rss": vsp.rss_mb()}


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "rows" / f"{s}.json").exists()]
    emit(f"[run] {len(jobs)} of {len(syms)} symbols to do, {a.workers} workers")
    t0 = time.time()
    n = 0
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for k, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            n += r["n"]
            if k % 25 == 0 or k == len(jobs):
                el = time.time() - t0
                emit(
                    f"  [run] {k}/{len(jobs)} {el:.0f}s eta {el / k * (len(jobs) - k):.0f}s; rows {n}; worker rss {r['rss']:.0f} MB; main {vsp.rss_mb():.0f} MB"
                )
    emit(f"[run] done in {time.time() - t0:.0f}s; rows {n}")


# ---------------------------------------------------------------- samples (the passport's)


def samples(a: argparse.Namespace) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    T = vsp.load_rows(a.passport_cache / "S3")
    period = T["t_in"] >= vsp.PASSPORT_MS
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    elig = vsp.s1.eligible_syms(a.data_dir)
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig)
    in_top = vsp.in_prev_day_top(T, vsp.top_n_days(qv, vsp.TOP_N))
    S = {"P": vsp.chain(T, period & demo & turn5), "P100": vsp.chain(T, period & demo & turn5 & in_top)}
    T["key"] = np.array(
        [key_of(str(s), str(tf), str(sd), int(t)) for s, tf, sd, t in zip(T["sym"], T["tf"], T["side"], T["t_in"], strict=True)]
    )
    return T, S


def oi_days(T: dict[str, np.ndarray], m: np.ndarray) -> set[tuple[str, str]]:
    need: set[tuple[str, str]] = set()
    for s, tf, t in zip(T["sym"][m], T["tf"][m], T["t_in"][m], strict=True):
        t0 = int(t) - vsp.HOURS[str(tf)] * HOUR_MS
        if int(t) < OI_START:
            continue
        for x in (t0 - mo.OI_MAX_AGE_MS, t0, int(t) - 1):
            need.add((str(s), vsp.iso(x)))
    return need


def oi_path(a: argparse.Namespace, s: str, d: str) -> Path | None:
    for root in (a.cache_dir / "metrics", a.oi_cache / "metrics"):
        p = root / s / f"{d}.json"
        if p.exists():
            return p
    return None


def stage_fetch_oi(a: argparse.Namespace, out: Path) -> None:
    import verify_liquidation_flush as vlf  # noqa: PLC0415  (shared archive fetcher with a per-file cache)

    from tradedesk.server import build_server  # noqa: PLC0415

    T, S = samples(a)
    need = sorted(oi_days(T, S["P"] | S["P100"]))
    todo = [(s, d) for s, d in need if oi_path(a, s, d) is None]
    if a.limit:
        todo = todo[: a.limit]
    emit(f"[fetch_oi] trade days {len(need)}, to fetch {len(todo)}; rss {vsp.rss_mb():.0f} MB")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    async def go() -> None:
        _, rt = build_server()
        try:
            fx = vlf.Fetcher(rt, out, workers=a.workers * 4)
            jobs = []
            for s, d in todo:

                async def one(s: str = s, d: str = d) -> Any:
                    return (await rt.binance_vision.um_metrics_daily(s, d)).data

                jobs.append((out / "metrics" / s / f"{d}.json", one))
            await fx.many("metrics", jobs)
        finally:
            await rt.aclose()

    asyncio.run(go())
    emit(f"FETCH COMPLETE rss {vsp.rss_mb():.0f} MB")


def oi_change_of(a: argparse.Namespace, s: str, tf: str, t_in: int, memo: dict[tuple[str, str], Any]) -> float:
    t0 = t_in - vsp.HOURS[tf] * HOUR_MS
    rows: list[list[float]] = []
    for d in sorted({vsp.iso(x) for x in (t0 - mo.OI_MAX_AGE_MS, t0, t_in - 1)}):
        if (s, d) not in memo:
            p = oi_path(a, s, d)
            memo[(s, d)] = json.loads(p.read_text()) if p else None
        rows.extend(memo[(s, d)] or [])
    if not rows:
        return math.nan
    arr = np.array(sorted(r[:2] for r in rows), dtype=float)
    return mo.oi_change(archive_obs_times(arr[:, 0]), arr[:, 1], t0, t_in)


# ---------------------------------------------------------------- report


def fmtd(r: dict[str, float], key: str = "m") -> str:
    lo, hi = r.get("lo", math.nan), r.get("hi", math.nan)
    return f"{r[key]:+.3f} [{lo:+.3f}; {hi:+.3f}]"


def paired(T: dict[str, np.ndarray], d: np.ndarray, m: np.ndarray) -> dict[str, float]:
    ok = m & np.isfinite(d)
    r = sp.mean_ci(d[ok], T["week"][ok], n_boot=vsp.N_BOOT, seed=vsp.SEED)
    r["p1"] = mo.one_sided_p(r["m"], r["p"])
    return r


def sub_delta(T: dict[str, np.ndarray], flag: np.ndarray, m: np.ndarray) -> dict[str, float]:
    r = sp.diff_ci(T["B"], flag & m, m, T["week"], n_boot=vsp.N_BOOT, seed=vsp.SEED)
    r["m"] = r["d"]
    r["p1"] = mo.one_sided_p(r["d"], r["p"])
    r["n"] = float((flag & m).sum())
    return r


def drop_top(T: dict[str, np.ndarray], contrib: np.ndarray, m: np.ndarray, k: int = 5) -> tuple[np.ndarray, list[str]]:
    by: dict[str, float] = defaultdict(float)
    for s, c in zip(T["sym"][m], contrib[m], strict=True):
        if np.isfinite(c):
            by[str(s)] += float(c)
    top = [s for s, _ in sorted(by.items(), key=lambda x: -x[1])[:k]]
    return m & ~np.isin(T["sym"], top), top


def years_oos() -> float:
    return (vsp.END["4h"] - vsp.OOS_MS) / (365.25 * DAY_MS)


def per_asset(T: dict[str, np.ndarray], d: np.ndarray, IS: np.ndarray, OOS: np.ndarray, label: str) -> None:
    """5a: heterogeneity of per-coin mean paired gains (OOS) and persistence of the IS top-10 coins."""
    means, ses = [], []
    for s in np.unique(T["sym"][OOS]):
        v = d[OOS & (T["sym"] == s) & np.isfinite(d)]
        if v.size >= 5:
            means.append(v.mean())
            ses.append(v.std(ddof=1) / math.sqrt(v.size))
    q, df, i2 = cochran_q(np.array(means), np.array(ses))
    is_m = {
        str(s): float(np.nanmean(d[IS & (T["sym"] == s)]))
        for s in np.unique(T["sym"][IS])
        if np.sum(IS & (T["sym"] == s) & np.isfinite(d)) >= 5
    }
    top = [s for s, _ in sorted(is_m.items(), key=lambda x: -x[1])[:10]]
    inn = OOS & np.isin(T["sym"], top) & np.isfinite(d)
    rest = OOS & ~np.isin(T["sym"], top) & np.isfinite(d)
    emit(
        f"  5a {label}: монет {len(means)}, Cochran Q {q:.1f} (df {df}, p {chi2_sf(q, df):.3f}), I² {i2:.2f}; топ-10 IS → OOS {np.mean(d[inn]) if inn.any() else math.nan:+.3f} (n {int(inn.sum())}) проти решти {np.mean(d[rest]):+.3f} (n {int(rest.sum())})"
    )


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T, S = samples(a)
    mod: dict[str, dict[str, Any]] = {}
    for p in sorted((out / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            mod[r["k"]] = r
    n = T["B"].size
    miss = sum(1 for k in T["key"] if k not in mod)

    def col(name: str) -> np.ndarray:
        return np.array([math.nan if (x := mod.get(k, {}).get(name)) is None else float(x) for k in T["key"]])

    B_mine = col("B")
    agree = np.isfinite(B_mine) & np.isfinite(T["B"])
    emit(
        f"# S3 modules: passport candidates {n}, without a module row {miss}; base R equal to the passport on {int(np.sum(np.abs(B_mine[agree] - T['B'][agree]) < 1e-9))} of {int(agree.sum())}"
    )
    IS, OOS = (T["t_in"] >= vsp.PASSPORT_MS) & (T["t_in"] < vsp.OOS_MS), T["t_in"] >= vsp.OOS_MS
    P, P100 = S["P"], S["P100"]
    emit(
        f"основна вибірка P: IS {int((P & IS).sum())}, OOS {int((P & OOS).sum())}; список демо P100: IS {int((P100 & IS).sum())}, OOS {int((P100 & OOS).sum())}"
    )
    emit(f"база OOS: P {fmtd(vsp.ci(T, P & OOS))}; P100 {fmtd(vsp.ci(T, P100 & OOS))}")
    yrs = years_oos()
    res: dict[str, dict[str, Any]] = {}
    side_long = T["side"] == "long"

    # --- paired modules H10, H9
    for name, plc in (("H10", "H10p"), ("H9", "H9p")):
        R, Rp, R15 = col(name), col(plc), col(name + "_15")
        d, dp = R - T["B"], R - Rp
        emit(f"\n## {name}")
        r = paired(T, d, P & OOS)
        rp = paired(T, dp, P & OOS)
        emit(
            f"OOS P: n {int(r['n'])}, Δ {fmtd(r)} p₁ {r['p1']:.4f}; проти плацебо {fmtd(rp)} p₁ {rp['p1']:.4f}; плацебо − база {np.nanmean((Rp - T['B'])[P & OOS]):+.3f}"
        )
        emit(
            f"  E[R] OOS P: база {np.nanmean(T['B'][P & OOS & np.isfinite(R)]):+.3f}, модуль {np.nanmean(R[P & OOS]):+.3f}, стрес ×1.5: база {np.nanmean(T['B15'][P & OOS & np.isfinite(R15)]):+.3f}, модуль {np.nanmean(R15[P & OOS]):+.3f}"
        )
        emit(
            f"  сума R / рік OOS P: база {np.nansum(T['B'][P & OOS & np.isfinite(R)]) / yrs:+.1f}, модуль {np.nansum(R[P & OOS]) / yrs:+.1f}; угод / рік {np.sum(P & OOS & np.isfinite(R)) / yrs:.0f}"
        )
        ri = paired(T, d, P & IS)
        r100 = paired(T, d, P100 & OOS)
        emit(f"  IS P Δ {fmtd(ri)}; OOS P100 Δ {fmtd(r100)} (n {int(r100['n'])})")
        cells = []
        for y in (2025, 2026):
            cells.append((str(y), float(np.nanmean(d[P & OOS & (T["year"] == y)]))))
        cells += [("long", float(np.nanmean(d[P & OOS & side_long]))), ("short", float(np.nanmean(d[P & OOS & ~side_long])))]
        mt, top = drop_top(T, d, P & OOS)
        cells.append(("без топ-5 монет", float(np.nanmean(d[mt]))))
        cells += [("4h", float(np.nanmean(d[P & OOS & (T["tf"] == "4h")]))), ("1d", float(np.nanmean(d[P & OOS & (T["tf"] == "1d")])))]
        emit("  розбивки OOS P (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
        if name == "H9":
            f, fp = col("H9_f"), col("H9p_f")
            used = 0.5 + 0.5 * np.nan_to_num(col("H9_ru")) * np.nan_to_num(f)
            emit(
                f"  заповнення другої частини OOS P: H9 {np.nanmean(f[P & OOS]):.3f}, плацебо {np.nanmean(fp[P & OOS]):.3f}; R на одиницю задіяного ризику: база {np.nanmean(T['B'][P & OOS & np.isfinite(R)]):+.3f}, H9 {np.nansum(R[P & OOS]) / np.nansum(used[P & OOS & np.isfinite(R)]):+.3f}"
            )
        per_asset(T, d, P & IS, P & OOS, name)
        res[name] = {"r": r, "rp": rp, "cells": cells, "r100": r100, "plc_p": rp["p1"]}

    # --- subgroup modules H1, H2, H5
    h1n = col("H1n")
    flags: dict[str, tuple[np.ndarray, np.ndarray]] = {
        "H1": (h1n >= 2, np.isfinite(h1n)),
        "H2": (col("H2") == 1.0, np.isfinite(col("H2"))),
    }
    if a.with_oi:
        memo: dict[tuple[str, str], Any] = {}
        dOI = np.full(n, math.nan)
        need = (P | P100) & (T["t_in"] >= OI_START)
        for i in np.flatnonzero(need):
            dOI[i] = oi_change_of(a, str(T["sym"][i]), str(T["tf"][i]), int(T["t_in"][i]), memo)
        memo.clear()
        q = np.full(n, -1)
        for tf in ("4h", "1d"):
            mt = T["tf"] == tf
            edges = mo.quintile_edges(dOI[P & IS & mt])
            q[mt] = mo.quintile_of(dOI[mt], edges)
            emit(f"H5 межі квінтилів ΔOI (IS, {tf}): {', '.join(f'{x:+.4f}' for x in edges)}")
        cov = [f"{y} {np.mean(np.isfinite(dOI[P & (T['year'] == y)])):.2f}" for y in range(2021, 2027)]
        emit("H5 покриття ΔOI у P за роками: " + ", ".join(cov))
        flags["H5"] = (q == 0, q >= 0)
        emit(
            "H5 квінтилі OOS P (описово): "
            + "; ".join(f"Q{j + 1} n {int(np.sum(P & OOS & (q == j)))} E[R] {np.mean(T['B'][P & OOS & (q == j)]):+.3f}" for j in range(5))
        )
    for name, (flag, defined) in flags.items():
        m = P & OOS & defined
        r = sub_delta(T, flag, m)
        obs, pl = mo.subgroup_placebo(T["B"][m], flag[m], N_PLACEBO_SUB, vsp.SEED)
        pp = float((np.sum(pl >= obs) + 1) / (pl.size + 1)) if pl.size else math.nan
        emit(f"\n## {name}")
        emit(
            f"OOS P (з визначеною ознакою {int(m.sum())}): з ознакою n {int(r['n'])} ({r['n'] / max(1, m.sum()):.0%}), E[R] {np.mean(T['B'][m & flag]):+.3f} проти {np.mean(T['B'][m]):+.3f} усіх; Δ {fmtd(r)} p₁ {r['p1']:.4f}; плацебо p {pp:.4f} (95-й перцентиль плацебо {np.quantile(pl, 0.95):+.3f})"
        )
        ri = sub_delta(T, flag, P & IS & defined)
        r100 = sub_delta(T, flag, P100 & OOS & defined)
        emit(f"  IS P Δ {fmtd(ri)} (n з ознакою {int(ri['n'])}); OOS P100 Δ {fmtd(r100)} (n {int(r100['n'])})")
        cells = []
        for lab, c in (
            ("2025", T["year"] == 2025),
            ("2026", T["year"] == 2026),
            ("long", side_long),
            ("short", ~side_long),
            ("4h", T["tf"] == "4h"),
            ("1d", T["tf"] == "1d"),
        ):
            mc = m & c
            cells.append((lab, float(np.mean(T["B"][mc & flag]) - np.mean(T["B"][mc])) if np.any(mc & flag) else math.nan))
        contrib = np.where(flag, T["B"] - np.mean(T["B"][m]), 0.0)
        mt, top = drop_top(T, contrib, m)
        cells.append(("без топ-5 монет", float(np.mean(T["B"][mt & flag]) - np.mean(T["B"][mt]))))
        emit("  розбивки OOS P (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
        emit(
            f"  угод з ознакою / рік OOS {np.sum(m & flag) / yrs:.0f}; сума R з ознакою / рік {np.sum(T['B'][m & flag]) / yrs:+.1f}; MDE ≈ 2·SE {2 * (r['hi'] - r['lo']) / 3.92:.3f}"
        )
        res[name] = {"r": r, "cells": cells, "r100": r100, "plc_p": pp, "flag": flag, "defined": defined}

    # --- verdict: Holm over the 5 registered modules (an unrun module counts p = 1)
    p1 = [res[mn]["r"]["p1"] if mn in res else 1.0 for mn in MODULES]
    adj = holm([x if np.isfinite(x) else 1.0 for x in p1])
    emit("\n## Вердикт (Холм на 5)")
    passed = []
    for mn, p, pa in zip(MODULES, p1, adj, strict=True):
        if mn not in res:
            emit(f"{mn}: не прогнано (p = 1 у Холмі)")
            continue
        x = res[mn]
        robust = all(np.isfinite(v) and v > 0 for c, v in x["cells"] if c in ("2025", "2026", "long", "short", "без топ-5 монет"))
        ok = (x["r"]["m"] > 0) and pa < 0.05 and x["plc_p"] < 0.05 and robust and x["r100"]["m"] > 0
        emit(
            f"{mn}: Δ {x['r']['m']:+.3f}, p₁ {p:.4f}, Холм {pa:.4f}, плацебо p {x['plc_p']:.4f}, стійкість {'так' if robust else 'ні'}, P100 Δ {x['r100']['m']:+.3f} → {'ПРОЙШОВ' if ok else 'не пройшов'}"
        )
        if ok:
            passed.append(mn)
    emit(f"пройшли: {passed or 'жоден'}")
    if len(passed) >= 2:
        R = T["B"].copy()
        if "H9" in passed and "H10" in passed:
            R = col("H9x10")
        elif "H9" in passed:
            R = col("H9")
        elif "H10" in passed:
            R = col("H10")
        w = np.ones(n)
        for mn in ("H1", "H2", "H5"):
            if mn in passed:
                w += np.where(res[mn]["flag"] & res[mn]["defined"], 1.0, 0.0)
        m = P & OOS & np.isfinite(R)
        emit(f"комбінація {passed}: E[R] OOS P {mo.weighted_mean(R[m], w[m]):+.3f} проти бази {np.mean(T['B'][m]):+.3f}")
    else:
        emit("комбінації немає (пройшло менше двох модулів)")
    cnt = Counter(int(x) for x in h1n[P & OOS] if np.isfinite(x))
    emit(f"\nописово: розподіл дотиків H1 OOS P {dict(sorted(cnt.items()))}; частка H2 {np.nanmean(col('H2')[P & OOS]):.3f}")
    xc = col("H9x10")
    rc = paired(T, xc - T["B"], P & OOS)
    emit(f"описово (зареєстровано лише для комбінації): H9×H10 разом − база OOS P {fmtd(rc)}")
    # exploratory, added after the first report: what the H2 flag is made of and the flag-vs-rest contrast
    emit("\nрозвідувально (після результатів):")
    for mn in ("H1", "H2", "H5"):
        if mn in res:
            f, dfd = res[mn]["flag"], res[mn]["defined"]
            c = sp.diff_ci(T["B"], P & OOS & dfd & f, P & OOS & dfd & ~f, T["week"], n_boot=vsp.N_BOOT, seed=vsp.SEED)
            emit(f"  {mn}: з ознакою − без ознаки OOS P {c['d']:+.3f} [{c['lo']:+.3f}; {c['hi']:+.3f}] p₂ {c['p']:.3f}")
    h2d = col("H2d")
    for lab, mk in (
        ("край = рівень HTF (d < 1e-9)", h2d < 1e-9),
        ("рівень поруч (0 < d ≤ 0.3 ATR)", (h2d >= 1e-9) & (h2d <= 0.3)),
        ("далі за 0.3 ATR", h2d > 0.3),
    ):
        for per, pm in (("IS", P & IS), ("OOS", P & OOS)):
            mm = pm & mk
            emit(f"  H2 {lab} {per}: n {int(mm.sum())}, E[R] {np.mean(T['B'][mm]):+.3f}")
    for tf in ("4h", "1d"):
        emit(f"  H2 частка «край = рівень» серед ознаки, {tf}: {np.mean((h2d < 1e-9)[P & OOS & (T['tf'] == tf) & (h2d <= 0.3)]):.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("run", "fetch_oi", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s3_modules")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--oi-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/oi_liquidation_flush")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--with-oi", action="store_true", help="report: include H5 (needs the fetch_oi stage)")
    a = ap.parse_args()
    out = a.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"verify_s3_modules --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"run": stage_run, "fetch_oi": stage_fetch_oi, "report": stage_report}[a.stage](a, out)
        emit(f"peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
