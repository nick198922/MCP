#!/usr/bin/env python3
"""Item 8/7b for S12 v4 (IDEA-20261030-demo-setups-depth, registration «Попередня реєстрація модулів S12»): the v4
base (entry at the close of the touch bar) on the demo list with a placebo of random entries of the same geometry,
and the modules H1 (CLV of the touch bar), H2 (early 1h entry), H3 (gap width / ATR), H5 (edge_touch), H8 (dry),
Holm over 5.

    uv run python scripts/verify_s12_modules.py --stage sanity   # 3 symbols: timing, detector equality
    uv run python scripts/verify_s12_modules.py --stage run      # per-symbol rows (local caches, no network)
    uv run python scripts/verify_s12_modules.py --stage report

1h bars from the S14 cache (``course_s14_ema``); BTC daily trend from ``setup_passport/S12/btc_trend.json``.
Outputs (rows, <stage>.txt) under ``~/.cache/tradedesk-agents/s12_modules/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_s28_modules as v28  # noqa: E402
import verify_s38_modules as v38  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import s12_modules as m12  # noqa: E402
from tradedesk.analytics import s28_modules as m28  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics import setup_signals as ss  # noqa: E402
from tradedesk.analytics.course_s14_ema import resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import risk_unit  # noqa: E402
from tradedesk.execution.orders import reward_risk_at  # noqa: E402  (read-only: the guard's R:R after fees)

HOLD = vsp.HOLD["4h"]
END = vsp.END["4h"]
BAR = 4 * 3_600_000
K_PLC = 3
MODULES = ("H1", "H2", "H3", "H5", "H8")
SIGN = {"H1": 1.0, "H3": 1.0, "H5": -1.0, "H8": -1.0}  # registered direction of rho(feature, R)
FEAT = {"H1": "clv", "H3": "gap_atr", "H5": "edge", "H8": "dry"}
INTEREST = {"H1": 0.05, "H2": 0.05, "H3": 0.05, "H5": 0.05, "H8": 0.05}
MDE_K = 3.17  # z(0.99) + z(0.80): the strictest Holm step over 5
NB_MAIN, NB_DESC = vsp.N_BOOT, 2000
KNOWN = (14665, 0.000, -0.011, 0.036)  # 2026-10-26-prelimit-entries, S12 close: n, E[R], IS, OOS
emit = vsp.emit
YEAR_MS = 365.25 * vsp.DAY_MS


def draw(key: str, k: int, size: int) -> int:
    h = hashlib.sha256(f"s12plc|{key}|{k}".encode()).digest()
    return int.from_bytes(h[:8], "big") % size


# ---------------------------------------------------------------- per-symbol rows


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir = args
    t0w = time.time()
    s1c, dd = Path(s1_cache), Path(data_dir)
    member = vsp.s1.Membership(vsp.s1.members(s1c))
    btc = json.loads((Path(out_dir) / "btc_trend.json").read_text())
    btc_t = np.asarray(btc["t"], dtype=np.int64) + vsp.DAY_MS
    btc_tr = np.asarray(btc["trend"], dtype=np.int64)
    h1, qv = vsp.load_1h_qv(s1c, dd, s)
    tb = resample(h1, 4)
    daily = resample(h1, 24)
    d_tr = sp.trend_series(daily)
    qv4 = m12.bar_volume(h1.t, qv, tb.t, BAR)
    why: Counter[str] = Counter()

    def ctx(t_in: int, side: str, e: float, st: float, tg: float) -> dict[str, Any] | None:
        top = member(s, t_in)
        turn = sp.trailing_turnover(t_in, h1.t, qv)
        grp = sp.turnover_group(top, turn)
        if grp in (sp.LT1M, sp.UNKNOWN):
            return None
        j = int(np.searchsorted(btc_t, t_in, side="right")) - 1
        rr = reward_risk_at(side, e, st, tg)
        sc = sp.score_points(side, rr, sp.trend_at(daily, d_tr, t_in), int(btc_tr[j]) if j >= 0 else 0, s == vsp.BTC)
        return {"top30": top, "turn": turn, "rr": rr, "stop_pct": abs(e - st) / abs(e), "score": sc}

    def guard_ok(x: dict[str, Any] | None) -> bool:
        return x is not None and x["rr"] >= vsp.MIN_RR and x["stop_pct"] <= vsp.MAX_STOP and x["score"] >= vsp.MIN_SCORE

    def sim(path: Any, t_in: int, e: float, st: float, tg: float) -> tuple[Any, Any]:
        return vsp._outcome(path, t_in, e, st, tg, HOLD, vsp.RULE_B, vsp.F1), vsp._outcome(path, t_in, e, st, tg, HOLD, vsp.RULE_B, vsp.F15)

    det = {(sg.side, int(sg.signal_ts)): sg.entry for sg in ss.s12_all(tb, "4h")}
    rows: list[dict[str, Any]] = []
    h2only: list[dict[str, Any]] = []
    plc: list[dict[str, Any]] = []
    mine: set[tuple[str, int]] = set()
    for side in ("long", "short"):
        sg = 1.0 if side == "long" else -1.0
        bb = tb if side == "long" else tb.mirrored()
        path = h1 if side == "long" else h1.mirrored()
        pos, zones, _, _ = ss._s12_zones(bb, 2, len(bb) - 1)
        close_t = bb.t + BAR
        pool = np.flatnonzero((segment_pos(bb.t) >= ss.S12_WARMUP) & (close_t >= vsp.START_MS) & (close_t < END))
        v4: dict[int, tuple[Any, float, float, float]] = {}
        h2: dict[int, tuple[Any, float, float, float, int, float]] = {}
        for z in zones:
            i, mid = z.form, z.levels["mid"]
            j = ss._first_test(bb, pos, i, mid)
            if j < 0:
                continue
            st = mid - ss.S12_STOP_ATR * z.atr
            tg = mid + ss.S12_TARGET_R * (mid - st)
            if bb.low[j] > st and bb.c[j] < tg and (j not in v4 or i > v4[j][0].form):
                v4[j] = (z, mid, st, tg)
            ee = m12.early_entry(path, int(bb.t[j]), BAR, mid, st, tg)
            if ee is not None:
                cur = h2.get(j)
                if cur is None or ee[0] < cur[4] or (ee[0] == cur[4] and i > cur[0].form):
                    h2[j] = (z, mid, st, tg, ee[0], ee[1])

        def h2_fields(
            j: int, key: str, ev: tuple[Any, float, float, float, int, float], path: Any, bb: Any, side: str, sg: float
        ) -> dict[str, Any]:
            _, mid, st, tg, mh, e2 = ev
            t2 = int(path.t[mh]) + 3_600_000
            x2 = ctx(t2, side, sg * e2, sg * st, sg * tg)
            o2, o2f = sim(path, t2, e2, st, tg)
            k0 = m12.touch_hours(path, int(bb.t[j]), BAR)
            hh = m12.first_touch_hour(path, k0, 4, mid)
            u = hh + draw(key, 99, k0 + 3 - hh)
            ep = m12.entry_at_hour(path, hh, u, st, tg)
            rp = rp15 = 0.0
            if ep is not None:
                tp = int(path.t[u]) + 3_600_000
                if guard_ok(ctx(tp, side, sg * ep, sg * st, sg * tg)):
                    op, opf = sim(path, tp, ep, st, tg)
                    if op is not None and opf is not None:
                        rp, rp15 = op.r_net, opf.r_net
            ok = guard_ok(x2) and o2 is not None and o2f is not None
            return {
                "h2": True,
                "t2": t2,
                "h2ok": bool(ok),
                "h2turn": x2["turn"] if x2 else None,
                "R2": o2.r_net if ok else 0.0,
                "R2_15": o2f.r_net if ok else 0.0,
                "R2x": o2.exit_ms if ok else t2,
                "R2p": rp,
                "R2p15": rp15,
                "m_off": int(mh - k0),
            }

        for j, (z, _mid, st, tg) in sorted(v4.items()):
            t_in = int(close_t[j])
            if not (vsp.START_MS <= t_in < END):
                continue
            if det.get((side, int(bb.t[j]))) is None or abs(det[(side, int(bb.t[j]))] - sg * float(bb.c[j])) > 1e-9:
                why["detector_mismatch"] += 1
            mine.add((side, int(bb.t[j])))
            e = float(bb.c[j])
            x = ctx(t_in, side, sg * e, sg * st, sg * tg)
            if x is None:
                why["under_1m_or_unknown"] += 1
                continue
            o, of = sim(path, t_in, e, st, tg)
            if o is None or of is None:
                why["no_path"] += 1
                continue
            i = z.form
            near, full = z.levels["near"], z.levels["full"]
            key = f"{s}:{side}:{int(bb.t[j])}"
            r = {
                "k": key,
                "sym": s,
                "side": side,
                "t_in": t_in,
                **x,
                "B": o.r_net,
                "B15": of.r_net,
                "B_x": o.exit_ms,
                "kind": o.exit_kind,
                "rp": risk_unit(e, st) / abs(e),
                "clv": m12.clv(float(bb.h[j]), float(bb.low[j]), e),
                "shadow": (min(float(bb.o[j]), e) - float(bb.low[j])) / z.atr,
                "gap_atr": m12.gap_atr(near, full, z.atr),
                "edge": m12.edge_touch(bb.low, i, j, near),
                "age": j - i,
                "dry": m12.dry(qv4, i, j),  # volume is the same in the mirrored frame
                "h2": False,
            }
            if j in h2:
                r.update(h2_fields(j, key, h2[j], path, bb, side, sg))
            rows.append(r)
            why[f"{side}:v4"] += 1
            for k in range(K_PLC):  # random entries of the same geometry (percent of the entry), same coin and side
                if pool.size == 0:
                    break
                rr_ = int(pool[draw(key, k, pool.size)])
                c_r = float(bb.c[rr_])
                e_r, st_r, tg_r = c_r, c_r * st / e, c_r * tg / e
                tr = int(close_t[rr_])
                xr = ctx(tr, side, sg * e_r, sg * st_r, sg * tg_r)
                if not guard_ok(xr) or xr is None or xr["turn"] < 5e6 or not (st_r < e_r < tg_r):
                    continue
                op, opf = sim(path, tr, e_r, st_r, tg_r)
                if op is None or opf is None:
                    continue
                plc.append({"k": key, "sym": s, "t_in": tr, "B": op.r_net, "B15": opf.r_net})
        for j in sorted(set(h2) - set(v4)):  # H2 events without a v4 trade (registered (b))
            t_in = int(close_t[j])
            if not (vsp.START_MS <= t_in < END):
                continue
            key = f"{s}:{side}:{int(bb.t[j])}"
            f = h2_fields(j, key, h2[j], path, bb, side, sg)
            if f["h2ok"]:
                h2only.append({"k": key, "sym": s, "side": side, "t_in": f["t2"], "top_t": f["t2"], **f})
                why[f"{side}:h2only"] += 1
    why["detector_extra"] = sum(1 for k in det if k not in mine and vsp.START_MS <= k[1] + BAR < END)
    out = Path(out_dir) / "rows"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{s}.json").write_text(json.dumps({"rows": rows, "h2only": h2only, "plc": plc, "why": dict(why)}, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t0w, "rss": vsp.rss_mb()}


def stage_sanity(a: argparse.Namespace, out: Path) -> None:
    t0 = time.time()
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        r = run_symbol((s, str(a.s1_cache), str(a.data_dir), str(out / "sanity")))
        emit(f"timing {s}: {r['sec']:.1f}s; {r['why']}; rss {r['rss']:.0f} MB")
    n = len(vsp.syms_with_1h(a.s1_cache, a.data_dir))
    emit(
        f"3 symbols in {time.time() - t0:.0f}s -> about {(time.time() - t0) / 3 * n / a.workers / 60:.1f} min for {n} on {a.workers} workers"
    )


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "rows" / f"{s}.json").exists()]
    v38.pool_run(run_symbol, jobs, a.workers, "run")


# ---------------------------------------------------------------- tables


def table(rows: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    if not rows:
        return {}
    keys = sorted({k for r in rows for k in r})
    T = {k: np.array([r.get(k) for r in rows], dtype=object) for k in keys}
    for k in keys:
        if k in ("k", "sym", "side", "kind"):
            T[k] = T[k].astype(str)
        elif k in ("t_in", "B_x", "t2", "R2x", "top_t", "m_off", "edge", "age", "score"):
            T[k] = np.array([-1 if v is None else int(v) for v in T[k]], dtype=np.int64)
        elif k in ("h2", "h2ok", "top30"):
            T[k] = np.array([bool(v) for v in T[k]])
        else:
            T[k] = np.array([math.nan if v is None else float(v) for v in T[k]])
    T["week"] = T["t_in"] // sp.WEEK_MS
    T["year"] = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in T["t_in"]])
    if "side" in T:
        T["order"] = (T["side"] == "short").astype(np.int64)
    return T


def load(out: Path) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, np.ndarray], Counter[str]]:
    rows: list[dict[str, Any]] = []
    h2o: list[dict[str, Any]] = []
    plc: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    for p in sorted((out / "rows").glob("*.json")):
        d = json.loads(p.read_text())
        rows.extend(d["rows"])
        h2o.extend(d["h2only"])
        plc.extend(d["plc"])
        why.update(d["why"])
    return table(rows), table(h2o), table(plc), why


def mci(v: np.ndarray, wk: np.ndarray, nb: int = NB_DESC) -> dict[str, float]:
    return sp.mean_ci(v, wk, n_boot=nb, seed=vsp.SEED)


def f(r: dict[str, float], key: str = "m") -> str:
    return f"{r[key]:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}]"


def span(lo: int, hi: int) -> float:
    return (hi - lo) / YEAR_MS


def label(r: dict[str, float], interest: float, key: str = "m") -> str:
    if r["hi"] < interest:
        return "✗ (useful effect excluded)"
    if MDE_K * r["se"] > interest:
        return f"◯ (MDE80 {MDE_K * r['se']:.3f} > {interest})"
    return "✗"


# ---------------------------------------------------------------- report


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T, H2o, Q, why = load(out)
    emit(f"rows: v4 candidates {T['B'].size}, H2-only events {H2o.get('R2', np.zeros(0)).size}, placebo rows {Q['B'].size}")
    emit(f"detector check: mismatches {why['detector_mismatch']}, s12_all signals not reproduced {why['detector_extra']} (both must be 0)")
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, vsp.s1.eligible_syms(a.data_dir))
    top_days = vsp.top_n_days(qv, vsp.TOP_N)
    guard = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    top100 = vsp.in_prev_day_top(T, top_days)
    post = T["t_in"] >= vsp.PASSPORT_MS
    V = vsp.chain(T, top100 & turn5 & guard & post)
    P = vsp.chain(T, turn5 & guard & post)
    oos = T["t_in"] >= vsp.OOS_MS
    IS = V & ~oos
    VO = V & oos
    wk = T["week"]

    emit("\n## known number (2026-10-26-prelimit-entries, close: top-30, guard, chain per coin + side, from 2020-02, day clusters)")
    g30 = T["top30"] & (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP)
    idx = np.flatnonzero(g30)
    idx = idx[np.lexsort((T["order"][idx], T["t_in"][idx]))]
    busy: dict[tuple[str, str], int] = {}
    kk = np.zeros(T["B"].size, bool)
    for k in idx:
        key = (str(T["sym"][k]), str(T["side"][k]))
        if int(T["t_in"][k]) < busy.get(key, -1):
            continue
        kk[k] = True
        busy[key] = int(T["B_x"][k])
    r = sp.mean_ci(T["B"][kk], T["t_in"][kk] // vsp.DAY_MS, n_boot=NB_DESC, seed=vsp.SEED)
    n0, m0, is0, oos0 = KNOWN
    ok_n, ok_m = abs(r["n"] / n0 - 1) <= 0.05, abs(r["m"] - m0) <= 0.03
    emit(
        f"there n {n0}, {m0:+.3f} (IS {is0:+.3f}, OOS {oos0:+.3f}); here n {int(r['n'])} ({r['n'] / n0 - 1:+.1%}), {f(r)}, "
        f"IS {np.mean(T['B'][kk & ~oos]):+.3f}, OOS {np.mean(T['B'][kk & oos]):+.3f} -> {'reproduced' if ok_n and ok_m else 'NOT reproduced'} (n ± 5 %, E[R] ± 0.03R)"
    )
    if not (ok_n and ok_m):
        emit("stop: the base does not reproduce the known number (registration)")
        return

    emit("\n## base v4 (R per trade risk, not capital; taker entry at the close of the touch bar)")
    qtop = vsp.in_prev_day_top(Q, top_days)
    kpos = {k: n for n, k in enumerate(T["k"])}
    qsig = np.array([kpos[k] for k in Q["k"]], dtype=np.int64)
    Qw = T["week"][qsig]
    base: dict[str, dict[str, float]] = {}
    for lab, msk, nb in (
        ("V 2021+", V, NB_MAIN),
        ("V IS", IS, NB_DESC),
        ("V OOS", VO, NB_MAIN),
        ("V 2025", V & (T["year"] == 2025), NB_DESC),
        ("V 2026", V & (T["year"] == 2026), NB_DESC),
        ("V longs 2021+", V & (T["side"] == "long"), NB_DESC),
        ("V shorts 2021+", V & (T["side"] == "short"), NB_DESC),
        ("V longs OOS", VO & (T["side"] == "long"), NB_DESC),
        ("V shorts OOS", VO & (T["side"] == "short"), NB_DESC),
        ("P 2021+", P, NB_DESC),
        ("P OOS", P & oos, NB_DESC),
    ):
        b = mci(T["B"][msk], wk[msk], nb)
        b15 = float(np.mean(T["B15"][msk])) if msk.any() else math.nan
        lo_ = vsp.OOS_MS if "OOS" in lab else vsp.PASSPORT_MS
        hi_ = vsp.OOS_MS if "IS" in lab else END
        if lab[-4:].isdigit():
            y = int(lab[-4:])
            lo_, hi_ = max(vsp.PASSPORT_MS, vsp.s1.ms(f"{y}-01-01T00:00")), min(END, vsp.s1.ms(f"{y + 1}-01-01T00:00"))
        yrs = span(lo_, hi_)
        pm = np.isin(qsig, np.flatnonzero(msk)) & qtop
        pr = float(np.mean(Q["B"][pm])) if pm.any() else math.nan
        emit(
            f"{lab:16s} n {int(b['n']):6d} | E[R] {f(b)} p {b['p']:.3f} | x1.5 {b15:+.3f} | {b['n'] / yrs:5.0f}/yr, ΣR/yr {b['m'] * b['n'] / yrs:+.1f} "
            f"| % price {np.mean(T['B'][msk] * T['rp'][msk]):+.4%} | placebo n {int(pm.sum())} E[R] {pr:+.3f}"
        )
        base[lab] = {**b, "x15": b15}
    pm = np.isin(qsig, np.flatnonzero(V)) & qtop
    vals = np.concatenate([T["B"][V], Q["B"][pm]])
    ina = np.concatenate([np.ones(int(V.sum()), bool), np.zeros(int(pm.sum()), bool)])
    cl = np.concatenate([wk[V], Qw[pm]])
    d = sp.diff_ci(vals, ina, ~ina, cl, n_boot=NB_MAIN, seed=vsp.SEED)
    p1b = m28.one_sided(d["d"], d["p"])
    emit(f"base vs placebo (V 2021+): {d['d']:+.3f} [{d['lo']:+.3f}; {d['hi']:+.3f}] p1 {p1b:.3f}")
    pmo = np.isin(qsig, np.flatnonzero(VO)) & qtop
    vals = np.concatenate([T["B"][VO], Q["B"][pmo]])
    ina = np.concatenate([np.ones(int(VO.sum()), bool), np.zeros(int(pmo.sum()), bool)])
    do = sp.diff_ci(vals, ina, ~ina, np.concatenate([wk[VO], Qw[pmo]]), n_boot=NB_DESC, seed=vsp.SEED)
    emit(f"base vs placebo (V OOS): {do['d']:+.3f} [{do['lo']:+.3f}; {do['hi']:+.3f}]")
    bv, bo = base["V 2021+"], base["V OOS"]
    if bv["lo"] > 0 and bo["lo"] > 0 and bo["x15"] > 0 and p1b < 0.05:
        vb = "✓"
    elif bv["hi"] < 0:
        vb = "✗ (reject)"
    else:
        vb = "inconclusive"
    emit(f"base verdict (registered rule): {vb}")
    emit("exit kinds V 2021+: " + str(dict(Counter(T["kind"][V].tolist()).most_common())))
    for lab, msk in (("OOS", VO), ("2021+", V)):
        ii = np.flatnonzero(msk)
        ii = ii[np.argsort(T["t_in"][ii], kind="stable")]
        bnd = sp.r_band(T["B"][ii], 30, reps=NB_MAIN, seed=vsp.SEED)
        emit(
            f"R band n=30 {lab}: iid {bnd['iid_p5']:+.3f} / {bnd['iid_p50']:+.3f} / {bnd['iid_p95']:+.3f}; "
            f"rolling {bnd['roll_p5']:+.3f} / {bnd['roll_p50']:+.3f} / {bnd['roll_p95']:+.3f}"
        )
    emit("5a base (E[R] per coin, V)")
    v28.per_asset_mean("base", T["B"][V], T["sym"][V], wk[V], ~oos[V], oos[V])

    res: dict[str, dict[str, Any]] = {}
    emit("\n## H1 / H3 / H5 / H8 (Spearman of the feature with R on V; registered sign; rho_adj = sign * rho)")
    medians: dict[str, float] = {}
    for h in ("H1", "H3", "H5", "H8"):
        x, sg = T[FEAT[h]], SIGN[h]
        ok = np.isfinite(x)
        Vm = V & ok
        main = m28.spearman_ci(sg * x[Vm], T["B"][Vm], wk[Vm], n_boot=NB_MAIN, seed=vsp.SEED)
        p1 = m28.one_sided(main["m"], main["p"])
        cells = {}
        for lab, msk in (("IS", IS), ("OOS", VO), ("2025", V & (T["year"] == 2025)), ("2026", V & (T["year"] == 2026)), ("P", P)):
            mm = msk & ok
            cells[lab] = m28.spearman(sg * x[mm], T["B"][mm])
        cells["no top5"] = v28.drop_top5_rho(x[Vm], T["B"][Vm], T["sym"][Vm], sg)
        pq = np.isin(qsig, np.flatnonzero(Vm)) & qtop
        dpl = m28.spearman_diff_ci(sg * x[Vm], T["B"][Vm], wk[Vm], sg * x[qsig[pq]], Q["B"][pq], Qw[pq], n_boot=NB_MAIN, seed=vsp.SEED)
        rho_pl = m28.spearman(sg * x[qsig[pq]], Q["B"][pq])
        med = float(np.median(x[IS & ok]))
        medians[h] = med
        fav = (x >= med) if sg > 0 else (x <= med)
        lo_half = (x < med) if sg > 0 else (x > med)
        e15 = float(np.mean(T["B15"][VO & ok & fav]))
        emit(
            f"{h} {FEAT[h]:8s} n {int(main['n'])} share>0 {np.mean(x[Vm] > 0):.1%} median IS {med:.3f} | rho_adj {f(main)} p1 {p1:.4f} SE {main['se']:.4f} "
            f"MDE80 {MDE_K * main['se']:.3f} | IS {cells['IS']:+.3f} OOS {cells['OOS']:+.3f} 2025 {cells['2025']:+.3f} 2026 {cells['2026']:+.3f} "
            f"no-top5 {cells['no top5']:+.3f} P {cells['P']:+.3f}"
        )
        emit(
            f"   placebo rho_adj {rho_pl:+.3f}; sig - placebo {dpl['d']:+.3f} [{dpl['lo']:+.3f}; {dpl['hi']:+.3f}] p1 {m28.one_sided(dpl['d'], dpl['p']):.4f} "
            f"| favourable half E[R] 2021+ {np.mean(T['B'][V & ok & fav]):+.3f} vs other {np.mean(T['B'][V & ok & lo_half]):+.3f}; "
            f"OOS {np.mean(T['B'][VO & ok & fav]):+.3f} vs {np.mean(T['B'][VO & ok & lo_half]):+.3f}; OOS x1.5 favourable {e15:+.3f}"
        )
        res[h] = {"main": main, "p1": p1, "pl_p1": m28.one_sided(dpl["d"], dpl["p"]), "cells": cells, "e15": e15}
    x3, sp3 = T["gap_atr"], T["stop_pct"]
    ok3 = V & np.isfinite(x3)
    emit(f"H3 partial rho (control stop %) V 2021+ {m12.partial_spearman(x3[ok3], T['B'][ok3], sp3[ok3]):+.3f} (descriptive)")
    for nm, xx in (("age (two-sided, descriptive)", T["age"].astype(float)), ("shadow/ATR (descriptive)", T["shadow"])):
        okx = V & np.isfinite(xx)
        emit(f"{nm}: rho {m28.spearman(xx[okx], T['B'][okx]):+.3f}, OOS {m28.spearman(xx[okx & oos], T['B'][okx & oos]):+.3f}")

    emit("\n## H2 early 1h entry (events: (a) v4 trade in V with an early entry, (b) no v4 trade, H2 passes list + guard, coin free)")
    A = V & T["h2"]
    a_top = A.copy()  # same UTC day as the v4 close: the same previous-day top list
    a_ok = T["h2ok"] & (np.nan_to_num(T["h2turn"], nan=-1) >= 5e6)
    R2a = np.where(a_ok, T["R2"], 0.0)
    R2a15 = np.where(a_ok, T["R2_15"], 0.0)
    if H2o:
        H2o["t_in"] = H2o["t2"]
        btop = vsp.in_prev_day_top(H2o, top_days) & (np.nan_to_num(H2o["h2turn"], nan=-1) >= 5e6) & (H2o["t2"] >= vsp.PASSPORT_MS)
        occ: dict[str, list[tuple[int, int]]] = {}
        for k in np.flatnonzero(V):
            occ.setdefault(str(T["sym"][k]), []).append((int(T["t_in"][k]), int(T["B_x"][k])))
        free = np.array(
            [not any(lo_ <= int(t) < hi_ for lo_, hi_ in occ.get(str(s_), [])) for s_, t in zip(H2o["sym"], H2o["t2"], strict=True)]
        )
        Bm = btop & free
    else:
        Bm = np.zeros(0, bool)
    nb_ = int(Bm.sum())
    dl = np.concatenate([R2a[a_top] - T["B"][a_top], H2o["R2"][Bm] if nb_ else np.zeros(0)])
    dl15 = np.concatenate([R2a15[a_top] - T["B15"][a_top], H2o["R2_15"][Bm] if nb_ else np.zeros(0)])
    dpl = np.concatenate([T["R2p"][a_top] - T["B"][a_top], H2o["R2p"][Bm] if nb_ else np.zeros(0)])
    ev_t = np.concatenate([T["t_in"][a_top], H2o["t2"][Bm] if nb_ else np.zeros(0, np.int64)])
    ev_sym = np.concatenate([T["sym"][a_top], H2o["sym"][Bm] if nb_ else np.zeros(0, str)])
    ev_w = ev_t // sp.WEEK_MS
    ev_y = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in ev_t])
    ev_o = ev_t >= vsp.OOS_MS
    emit(
        f"V trades {int(V.sum())}: with an early entry {int(A.sum())} ({A.sum() / max(1, V.sum()):.1%}; H2 fails list / guard {int((A & ~a_ok).sum())}), "
        f"fallback = v4 (Δ ≡ 0) {int((V & ~T['h2']).sum())}; (b) events {nb_}; entry hour offsets in the bar {dict(Counter(T['m_off'][A].tolist()))}"
    )
    main = mci(dl, ev_w, NB_MAIN)
    p1h2 = m28.one_sided(main["m"], main["p"])
    emit(f"Δ (a)+(b) 2021+: n {int(main['n'])} {f(main)} p1 {p1h2:.4f} SE {main['se']:.4f} MDE80 {MDE_K * main['se']:.3f}")
    cells2 = {}
    for lab, msk in (("IS", ~ev_o), ("OOS", ev_o), ("2025", ev_y == 2025), ("2026", ev_y == 2026)):
        rr_ = mci(dl[msk], ev_w[msk])
        cells2[lab] = rr_["m"]
        emit(f"  Δ {lab:5s} n {int(rr_['n']):6d} {f(rr_)} | x1.5 {np.mean(dl15[msk]):+.3f}")
    cells2["no top5"] = v28.drop_top5_mean(dl, ev_sym)
    ra = mci(R2a[a_top] - T["B"][a_top], wk[a_top])
    emit(f"  (a) only: n {int(ra['n'])} {f(ra)}; v4 E[R] {np.mean(T['B'][a_top]):+.3f} -> H2 {np.mean(R2a[a_top]):+.3f}")
    if nb_:
        rb = mci(H2o["R2"][Bm], H2o["week"][Bm])
        emit(f"  (b) only (added trades): n {int(rb['n'])} E[R] {f(rb)}; OOS {np.mean(H2o['R2'][Bm & (H2o['t2'] >= vsp.OOS_MS)]):+.3f}")
    plc = mci(dl - dpl, ev_w, NB_MAIN)
    p_pl2 = m28.one_sided(plc["m"], plc["p"])
    emit(f"  placebo (random 1h close h .. last-1): Δ_placebo {np.mean(dpl):+.3f}; Δ - Δ_placebo {f(plc)} p1 {p_pl2:.4f}")
    # P row: same events on P (no rank)
    AP = P & T["h2"]
    emit(f"  P (a) Δ 2021+ {np.mean(np.where(a_ok, T['R2'], 0.0)[AP] - T['B'][AP]):+.3f} (n {int(AP.sum())})")
    cells2["P"] = float(np.mean(np.where(a_ok, T["R2"], 0.0)[AP] - T["B"][AP]))
    so = VO & ~T["h2"]
    strat15 = np.concatenate(
        [T["B15"][so], T["R2_15"][VO & A & a_ok], H2o["R2_15"][Bm & (H2o["t2"] >= vsp.OOS_MS)] if nb_ else np.zeros(0)]
    )
    strat = np.concatenate([T["B"][so], T["R2"][VO & A & a_ok], H2o["R2"][Bm & (H2o["t2"] >= vsp.OOS_MS)] if nb_ else np.zeros(0)])
    e15_2 = float(np.mean(strat15))
    emit(
        f"  strategy with H2, OOS: n {strat.size}, E[R] {np.mean(strat):+.3f}, x1.5 {e15_2:+.3f} (base OOS {bo['m']:+.3f}, x1.5 {bo['x15']:+.3f})"
    )
    emit("  5a Δ per coin")
    v28.per_asset_mean("H2 Δ", dl, ev_sym, ev_w, ~ev_o, ev_o)
    res["H2"] = {"main": main, "p1": p1h2, "pl_p1": p_pl2, "cells": cells2, "e15": e15_2}

    emit("\n## overlap (Spearman between features, V 2021+) and H3 x H5")
    names = ["clv", "gap_atr", "edge", "dry"]
    for i_, a_ in enumerate(names):
        for b_ in names[i_ + 1 :]:
            okk = V & np.isfinite(T[a_]) & np.isfinite(T[b_])
            emit(f"  rho({a_}, {b_}) {m28.spearman(T[a_][okk], T[b_][okk]):+.3f}")
    for g_hi in (False, True):
        for e_lo in (True, False):
            mm = V & ((T["gap_atr"] >= medians["H3"]) == g_hi) & ((T["edge"] <= medians["H5"]) == e_lo)
            emit(
                f"  gap_atr {'>=' if g_hi else '<'} med, edge {'<=' if e_lo else '>'} med: n {int(mm.sum())}, E[R] 2021+ {np.mean(T['B'][mm]):+.3f}, "
                f"OOS {np.mean(T['B'][mm & oos]):+.3f}"
            )
    emit("\n## 5a for the rho modules")
    for h in ("H1", "H3", "H5", "H8"):
        x = T[FEAT[h]]
        okv = V & np.isfinite(x)
        v28.per_asset_rho(h, x[okv], T["B"][okv], T["sym"][okv], SIGN[h], ~oos[okv], oos[okv])

    emit("\n## Holm over 5 and the registered pass rule")
    adj = holm([res[h]["p1"] for h in MODULES])
    for h, padj in zip(MODULES, adj, strict=True):
        rr_ = res[h]
        pts = all(v > 0 for v in rr_["cells"].values() if math.isfinite(v))
        passed = padj < 0.05 and rr_["pl_p1"] < 0.05 and pts and rr_["e15"] > 0
        mm = rr_["main"]
        lab = "PASS" if passed else label(mm, INTEREST[h])
        emit(
            f"{h}: est {mm['m']:+.3f} [{mm['lo']:+.3f}; {mm['hi']:+.3f}] p1 {rr_['p1']:.4f} Holm {padj:.3f} | placebo p1 {rr_['pl_p1']:.3f} | "
            f"points in direction {pts} ({', '.join(f'{k} {v:+.3f}' for k, v in rr_['cells'].items())}) | OOS x1.5 {rr_['e15']:+.3f} -> {lab}"
        )


def stage_h2fix(a: argparse.Namespace, out: Path) -> None:
    """H2 on a causal event set (written AFTER the registered results; a correction of a registration defect, not a
    new variant). The registered sample kept an early-entry event only if its v4 trade was in V, and V's guard reads
    the R:R from the 4h close: the H2 trade was kept mostly when the bar later closed low (price fell after the early
    entry). Here an event counts when its early entry passes the demo list and guard at t2 and the coin is not inside
    a V trade, whatever the v4 trade did later: Δ = R_H2 - R_v4 * [v4 in V]. Events where H2 fails the guard and the
    v4 trade is in V stay (Δ = -R_v4); events where neither trades are Δ ≡ 0 and are only counted. That first line is
    only partly causal (V membership of the 'fails, v4 in V' group reads the 4h close); the second line (added after
    the critic B review, also post-hoc) is the main causal H2 number: the set fixed at t2 by ``s12_modules.h2_in_set``,
    the v4 entry kept with Δ = 0 when the early entry fails at t2."""
    T, H2o, _, _ = load(out)
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, vsp.s1.eligible_syms(a.data_dir))
    top_days = vsp.top_n_days(qv, vsp.TOP_N)
    guard = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    V = vsp.chain(T, vsp.in_prev_day_top(T, top_days) & turn5 & guard & (T["t_in"] >= vsp.PASSPORT_MS))
    occ: dict[str, list[tuple[int, int, int]]] = {}
    for k in np.flatnonzero(V):
        occ.setdefault(str(T["sym"][k]), []).append((int(T["t_in"][k]), int(T["B_x"][k]), int(k)))

    def free(sym: str, t: int, own: int) -> bool:
        return not any(lo <= t < hi and k != own for lo, hi, k in occ.get(sym, []))

    T2 = {"t_in": np.where(T["t2"] > 0, T["t2"], T["t_in"]), "sym": T["sym"]}
    top2 = vsp.in_prev_day_top(T2, top_days)
    h2ok = T["h2"] & T["h2ok"] & (np.nan_to_num(T["h2turn"], nan=-1) >= 5e6) & top2
    cand = T["h2"] & (T["t2"] >= vsp.PASSPORT_MS)
    fr = np.array([bool(c) and free(str(s), int(t), int(k)) for k, (c, s, t) in enumerate(zip(cand, T["sym"], T["t2"], strict=True))])
    ev = cand & fr & (h2ok | V)
    emit(
        f"events from v4 candidates: {int(ev.sum())} (v4 in V {int((ev & V).sum())}, v4 not in V {int((ev & ~V).sum())}); Δ ≡ 0 not counted {int((cand & fr & ~h2ok & ~V).sum())}"
    )
    if H2o:
        H2o["t_in"] = H2o["t2"]
        bt = vsp.in_prev_day_top(H2o, top_days) & (np.nan_to_num(H2o["h2turn"], nan=-1) >= 5e6) & (H2o["t2"] >= vsp.PASSPORT_MS)
        bf = np.array([free(str(s), int(t), -1) for s, t in zip(H2o["sym"], H2o["t2"], strict=True)])
        Bm = bt & bf
    else:
        Bm = np.zeros(0, bool)

    def block(name: str, evm: np.ndarray) -> None:
        r2 = np.where(h2ok, T["R2"], 0.0)[evm]
        r2_15 = np.where(h2ok, T["R2_15"], 0.0)[evm]
        rp = np.where(h2ok, T["R2p"], 0.0)[evm]
        b = np.where(V, T["B"], 0.0)[evm]
        b15 = np.where(V, T["B15"], 0.0)[evm]
        dl = np.concatenate([r2 - b, H2o["R2"][Bm]]) if H2o else r2 - b
        dl15 = np.concatenate([r2_15 - b15, H2o["R2_15"][Bm]]) if H2o else r2_15 - b15
        dpl = np.concatenate([rp - b, H2o["R2p"][Bm]]) if H2o else rp - b
        tt = np.concatenate([T["t2"][evm], H2o["t2"][Bm]]) if H2o else T["t2"][evm]
        sym = np.concatenate([T["sym"][evm], H2o["sym"][Bm]]) if H2o else T["sym"][evm]
        w, o = tt // sp.WEEK_MS, tt >= vsp.OOS_MS
        yr = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in tt])
        r = mci(dl, w, NB_MAIN)
        emit(
            f"Δ {name} 2021+: n {int(r['n'])} {f(r)} p1 {m28.one_sided(r['m'], r['p']):.4f} SE {r['se']:.4f} MDE80 {MDE_K * r['se']:.3f} | x1.5 {np.mean(dl15):+.3f}"
        )
        for lab, msk in (("IS", ~o), ("OOS", o), ("2025", yr == 2025), ("2026", yr == 2026)):
            rr_ = mci(dl[msk], w[msk])
            emit(f"  Δ {lab:5s} n {int(rr_['n']):6d} {f(rr_)} | x1.5 {np.mean(dl15[msk]):+.3f}")
        emit(f"  without top-5 coins {v28.drop_top5_mean(dl, sym):+.3f}")
        pl = mci(dl - dpl, w, NB_MAIN)
        emit(f"  placebo: Δ_placebo {np.mean(dpl):+.3f}; Δ - Δ_placebo {f(pl)} p1 {m28.one_sided(pl['m'], pl['p']):.4f}")

    # (1) strategy comparison without a fallback entry: H2 vs v4, the v4 trade skipped when the early entry fails the
    # guard; its 'fails, v4 in V' group (Δ = -R_v4) is selected by the 4h close, so this set is only partly causal.
    block("causal", ev)
    # (2) MAIN causal H2 number (added after the critic B review, post-hoc like (1)): the set is fixed at t2 by
    # s12_modules.h2_in_set (early entry passes list + guard at t2, coin free at t2); otherwise the v4 entry stays, Δ = 0.
    ev_t2 = cand & np.array([m12.h2_in_set(bool(p), bool(q)) for p, q in zip(h2ok, fr, strict=True)])
    chk = np.array(
        [m12.h2_delta(True, bool(vv), float(x), float(y)) for vv, x, y in zip(V[ev_t2], T["R2"][ev_t2], T["B"][ev_t2], strict=True)]
    )
    assert np.allclose(chk, (T["R2"] - np.where(V, T["B"], 0.0))[ev_t2])
    emit(
        f"\nset fixed at t2 (fully causal): events from v4 candidates {int(ev_t2.sum())} (v4 in V {int((ev_t2 & V).sum())}, "
        f"v4 not in V {int((ev_t2 & ~V).sum())}), H2-only {int(Bm.sum())}; v4 in V with H2 failing at t2 -> fallback v4, "
        f"Δ = 0: {int((cand & fr & ~h2ok & V).sum())}"
    )
    block("t2-set", ev_t2)
    a_ok = T["h2"] & T["h2ok"] & (np.nan_to_num(T["h2turn"], nan=-1) >= 5e6)
    other_day = V & T["h2"] & (T["t2"] // vsp.DAY_MS != T["t_in"] // vsp.DAY_MS)
    emit(
        f"  registered (a) used the list on the v4 close day: V events whose t2 is on another UTC day than t_in {int(other_day.sum())}, "
        f"failing the t2 list {int((other_day & ~top2).sum())}, of them in the registered (a) {int((other_day & a_ok & ~top2).sum())}"
    )
    vh = V & T["h2"] & (T["t2"] >= vsp.PASSPORT_MS)
    emit(
        f"  V trades with an early entry: {int(vh.sum())}, early entry fails list / guard at t2 {int((vh & ~h2ok).sum())} ({(vh & ~h2ok).sum() / max(1, vh.sum()):.1%})"
    )
    nv = ev & ~V
    emit(
        f"  v4 not in V (the events the registered sample dropped): n {int(nv.sum())}, H2 E[R] {np.mean(T['R2'][nv]):+.3f}; "
        f"v4 in V with H2 passing: n {int((ev & V & h2ok).sum())}, Δ {np.mean((T['R2'] - T['B'])[ev & V & h2ok]):+.3f}"
    )
    so = V & (T["t_in"] >= vsp.OOS_MS) & ~(ev & h2ok)  # v4 trades the module does not replace
    strat15 = np.concatenate(
        [
            T["B15"][so],
            T["R2_15"][ev & h2ok & (T["t2"] >= vsp.OOS_MS)],
            H2o["R2_15"][Bm & (H2o["t2"] >= vsp.OOS_MS)] if H2o else np.zeros(0),
        ]
    )
    emit(f"  strategy with H2 (causal), OOS: n {strat15.size}, E[R] x1.5 {np.mean(strat15):+.3f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("sanity", "run", "report", "h2fix"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s12_modules")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir / "S12"
    out.mkdir(parents=True, exist_ok=True)
    bt = (a.passport_cache / "S12" / "btc_trend.json").read_text()
    for d in (out, out / "sanity"):
        d.mkdir(parents=True, exist_ok=True)
        (d / "btc_trend.json").write_text(bt)
    with (out / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"verify_s12_modules --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"sanity": stage_sanity, "run": stage_run, "report": stage_report, "h2fix": stage_h2fix}[a.stage](a, out)
        emit(f"peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
