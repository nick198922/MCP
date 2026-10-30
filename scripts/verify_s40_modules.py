#!/usr/bin/env python3
"""S40 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b): H1 (R net of beta x BTC over the hold, and a BTC short
hedge), H3 (failures of the S14 / S16 / S23 shorts -> the same long), H4 (failure window 12 bars instead of 6, added
events j - r = 7..12) and H5 (the same S40 on 2020, independent weeks), each against the S40 passport base, with the
registered placebos (a random long of the same 4h bar on another coin; for H3 also the same coin at another time).
Registration: the card's section «Попередня реєстрація модулів S40».

    uv run python scripts/verify_s40_modules.py --stage sanity     # timing of 3 coins, detector check (no R printed)
    uv run python scripts/verify_s40_modules.py --stage run        # per-symbol candidate rows (local caches, 4 workers)
    uv run python scripts/verify_s40_modules.py --stage placebo    # placebo trades
    uv run python scripts/verify_s40_modules.py --stage report     # tables (also <cache>/report.txt)

No network: 1h bars from the S14 cache, BTC trend from the S40 passport cache, BTC funding from the cycle_regime cache.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_s38_modules as v38  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import s38_modules as sm  # noqa: E402
from tradedesk.analytics import s39_modules as m9  # noqa: E402
from tradedesk.analytics import s40_modules as m0  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics import setup_signals as ss  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import Outcome, simulate_stop_rule  # noqa: E402
from tradedesk.execution.orders import reward_risk_at  # noqa: E402  (read-only: the guard's R:R after fees)

DAY_MS = 24 * HOUR_MS
BAR4 = 4 * HOUR_MS
HOLD = 7 * DAY_MS
END = vsp.END["4h"]
Y2020_MID = vsp.s1.ms("2020-07-01T00:00")
MODULES = ("H1", "H3", "H4", "H5")
ADD = ("H3", "H4")
K_COINS = 3
SETUP = "S40"
FAMILY = ("S14", "S16", "S23")
INTEREST = 0.10
MDE_K = 3.08  # z(1 - 0.0125) + z(0.80): the strictest Holm step over 4, 80 % power
emit = vsp.emit
_G: dict[str, Any] = {}


def long_(path: Bars, t_in: int, e: float, st: float, tg: float, fees: Any) -> Outcome | None:
    return simulate_stop_rule(path, t_in, e, st, tg, HOLD, vsp.RULE_B, fees=fees)


def parents(tb: Bars, kind: str) -> list[m0.Parent]:
    if kind in ("S3", "S16"):
        sigs = [s for s in ss.range_all(tb, "4h") if s.setup == kind]
    elif kind == "S14":
        sigs = ss.s14_all(tb, "4h")
    else:
        sigs = ss.s23_all(tb, "4h")
    return [m0.Parent(int(np.searchsorted(tb.t, s.signal_ts)), s.stop, s.target, kind) for s in sigs if s.side == "short"]


def btc_ctx(s1_cache: str, data_dir: str, regime_cache: str) -> dict[str, Any]:
    """BTC 1h closes / returns and BTCUSDT funding, once per worker process."""
    if "btc" not in _G:
        h, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), vsp.BTC)
        rows = json.loads((Path(regime_cache) / "funding.json").read_text())["rows"]
        f = np.array([[float(r[0]), float(r[1])] for r in rows if r[1] is not None]) if rows else np.zeros((0, 2))
        f = f[np.argsort(f[:, 0], kind="stable")] if f.size else f
        _G["btc"] = {
            "t": h.t,
            "c": h.c,
            "r": m0.hourly_returns(h.t, h.c),
            "ft": f[:, 0].astype(np.int64) if f.size else np.zeros(0, dtype=np.int64),
            "fr": f[:, 1] if f.size else np.zeros(0),
        }
    return _G["btc"]


def h1_fields(
    btc: dict[str, Any],
    t: np.ndarray,
    rc: np.ndarray,
    rb: np.ndarray,
    t_in: int,
    x_ms: int,
    e: float,
    st: float,
    r: float,
    r15: float | None,
) -> dict[str, Any]:
    beta = m0.rolling_beta(t, rc, rb, t_in)
    bret = m0.window_return(btc["t"], btc["c"], t_in, x_ms)
    fund = m0.funding_sum(btc["ft"], btc["fr"], t_in, x_ms)
    out: dict[str, Any] = {"beta": beta, "btc": bret, "fund": fund, "res": None, "hedge": None, "hedge15": None}
    if np.isfinite(beta) and np.isfinite(bret):
        res = m0.residual_r(r, beta, bret, e, st, vsp.F1)
        out["res"] = res
        out["hedge"] = res - m0.hedge_cost_r(beta, e, st, fund, fees=vsp.F1)
        if r15 is not None:
            out["hedge15"] = m0.residual_r(r15, beta, bret, e, st, vsp.F15) - m0.hedge_cost_r(beta, e, st, fund, mult=1.5, fees=vsp.F15)
    return out


# ---------------------------------------------------------------- per-symbol rows


def run_symbol(args: tuple[str, str, str, str, str, str, bool]) -> dict[str, Any]:
    s, s1_cache, data_dir, pp_dir, regime_cache, out_dir, with_s23 = args
    t0 = time.time()
    s1c, dd, pp = Path(s1_cache), Path(data_dir), Path(pp_dir)
    member = vsp.s1.Membership(vsp.s1.members(s1c))
    bt = json.loads((pp / "btc_trend.json").read_text())
    btc_t = np.asarray(bt["t"], dtype=np.int64) + DAY_MS
    btc_tr = np.asarray(bt["trend"], dtype=np.int64)
    btc = btc_ctx(s1_cache, data_dir, regime_cache)
    h1, qv = vsp.load_1h_qv(s1c, dd, s)
    rc = m0.hourly_returns(h1.t, h1.c)
    rb = m0.aligned(btc["t"], btc["r"], h1.t)
    daily = resample(h1, 24)
    d_tr = sp.trend_series(daily)
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    timing: dict[str, float] = {}

    def row(kind: str, t_in: int, e: float, st: float, tg: float, tag: str, lag: int) -> dict[str, Any] | None:
        if not (vsp.START_MS <= t_in < END) or not (st < e < tg):
            return None
        top = member(s, t_in)
        turn = sp.trailing_turnover(t_in, h1.t, qv)
        grp = sp.turnover_group(top, turn)
        if grp in (sp.LT1M, sp.UNKNOWN):
            why[f"{kind}:{grp}"] += 1
            return None
        b = long_(h1, t_in, e, st, tg, vsp.F1)
        if b is None:
            why[f"{kind}:no_path"] += 1
            return None
        b15 = long_(h1, t_in, e, st, tg, vsp.F15)
        j = int(np.searchsorted(btc_t, t_in, side="right")) - 1
        rr = reward_risk_at("long", e, st, tg)
        why[f"{kind}:cand"] += 1
        r = {
            "k": f"{s}|{kind}|{t_in}",
            "sym": s,
            "kind": kind,
            "tag": tag,
            "lag": lag,
            "t_in": t_in,
            "top30": top,
            "turn": turn,
            "grp": grp,
            "rr": rr,
            "stop_pct": (e - st) / e,
            "score": sp.score_points("long", rr, sp.trend_at(daily, d_tr, t_in), int(btc_tr[j]) if j >= 0 else 0, s == vsp.BTC),
            "B": b.r_net,
            "B_x": b.exit_ms,
            "B_k": b.exit_kind,
            "B15": None if b15 is None else b15.r_net,
            "cost": b.cost_r,
            "pct": m9.price_return(b.r_net, e, st),
        }
        r.update(h1_fields(btc, h1.t, rc, rb, t_in, b.exit_ms, e, st, b.r_net, r["B15"]))
        return r

    tb = resample(h1, 4)
    if len(tb) >= ss.RANGE_W + 20:
        tt = time.time()
        pos = segment_pos(tb.t)
        a_arr = atr(tb, 14, pos)
        p3 = parents(tb, "S3")
        f3 = m0.failed_short(tb, pos, a_arr, p3)
        base = ss.s40_all(tb, "4h")
        gen = {(int(tb.t[f.j]), round(f.entry, 12), round(f.stop, 12)) for f in f3}
        ref = {(int(x.signal_ts), round(x.entry, 12), round(x.stop, 12)) for x in base}
        why["detector_mismatch"] += len(gen ^ ref)
        why["detector_signals"] += len(ref)
        base_j = {f.j for f in f3}
        for sig in base:
            lag = int(round((sig.signal_ts - float(sig.indicators["sweep_ts"])) / BAR4))
            r = row("base", sig.signal_ts + BAR4, sig.entry, sig.stop, sig.target, "S3", lag)
            if r:
                rows.append(r)
        timing["4h"] = time.time() - tt
        # H4: the same S3 parents, window 12; bars the base took are not repeated
        tt = time.time()
        for f in m0.failed_short(tb, pos, a_arr, p3, max_wait=12, used=set(base_j)):
            why["H4_lag_le6"] += int(f.j - f.r <= 6)
            r = row("H4", int(tb.t[f.j]) + BAR4, f.entry, f.stop, f.target, "S3", f.j - f.r)
            if r:
                rows.append(r)
        timing["H4"] = time.time() - tt
        # H3: the failures of the S14 / S16 / S23 shorts
        fam = []
        for kind in FAMILY:
            if kind == "S23" and not with_s23:
                continue
            tk = time.time()
            fam.extend(parents(tb, kind))
            timing[kind] = time.time() - tk
        for f in m0.failed_short(tb, pos, a_arr, fam, used=set(base_j)):
            r = row("H3", int(tb.t[f.j]) + BAR4, f.entry, f.stop, f.target, f.tag, f.j - f.r)
            if r:
                rows.append(r)
    out = Path(out_dir) / "rows"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{s}.json").write_text(json.dumps({"rows": rows, "why": dict(why)}, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t0, "timing": timing, "rss": vsp.rss_mb()}


def job(a: argparse.Namespace, s: str, out: Path) -> tuple[str, str, str, str, str, str, bool]:
    return (s, str(a.s1_cache), str(a.data_dir), str(a.passport_cache / SETUP), str(a.regime_cache), str(out), not a.no_s23)


def stage_sanity(a: argparse.Namespace, out: Path) -> None:
    t0 = time.time()
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        r = run_symbol(job(a, s, out / "sanity"))
        rr = json.loads((out / "sanity" / "rows" / f"{s}.json").read_text())["rows"]
        cnt = Counter(x["kind"] for x in rr)
        nb = sum(1 for x in rr if x["beta"] is not None and math.isfinite(x["beta"]))
        emit(
            f"timing {s}: {r['sec']:.1f}s ({', '.join(f'{k} {v:.1f}s' for k, v in r['timing'].items())}); rows {dict(cnt)}; beta defined {nb}/{len(rr)}; detector signals {r['why'].get('detector_signals', 0)}, mismatches {r['why'].get('detector_mismatch', 0)}, H4 lag<=6 {r['why'].get('H4_lag_le6', 0)}; rss {r['rss']:.0f} MB"
        )
    b = btc_ctx(str(a.s1_cache), str(a.data_dir), str(a.regime_cache))
    if b["ft"].size:
        emit(
            f"funding: {b['ft'].size} settlements {vsp.iso(int(b['ft'][0]))} .. {vsp.iso(int(b['ft'][-1]))}, median {np.median(b['fr']):+.5f}"
        )
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    n20 = sum(1 for s in syms if any((a.s1_cache / "k1h" / s / f"2020-{m:02d}.json").exists() for m in range(2, 13)))
    emit(f"coins with any 1h data in 2020-02..12: {n20}")
    emit(
        f"3 symbols in {time.time() - t0:.0f}s -> about {(time.time() - t0) / 3 * len(syms) / a.workers / 60:.0f} min for {len(syms)} on {a.workers} workers"
    )


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [job(a, s, out) for s in syms if a.force or not (out / "rows" / f"{s}.json").exists()]
    v38.pool_run(run_symbol, jobs, a.workers, "run")


# ---------------------------------------------------------------- samples

STR = ("k", "sym", "kind", "tag", "grp", "B_k")


def load(out: Path) -> dict[str, np.ndarray]:
    rows: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    for p in sorted((out / "rows").glob("*.json")):
        d = json.loads(p.read_text())
        rows.extend(d["rows"])
        why.update(d["why"])
    keys = sorted({k for r in rows for k in r})
    T: dict[str, np.ndarray] = {}
    for k in keys:
        col = [r.get(k) for r in rows]
        if k in STR:
            T[k] = np.array(["" if x is None else str(x) for x in col])
        elif k in ("t_in", "B_x", "lag"):
            T[k] = np.array(col, dtype=np.int64)
        else:
            T[k] = np.array([math.nan if x is None else float(x) for x in col])
    T["score"] = T["score"].astype(np.int64)
    T["top30"] = T["top30"] == 1.0
    T["week"] = T["t_in"] // sp.WEEK_MS
    T["year"] = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in T["t_in"]])
    T["order"] = (T["kind"] != "base").astype(np.int64)
    T["_why"] = np.array([json.dumps(dict(why))])
    return T


def samples(a: argparse.Namespace, T: dict[str, np.ndarray]) -> tuple[dict[str, np.ndarray], dict[int, set[str]]]:
    p21 = T["t_in"] >= vsp.PASSPORT_MS
    p20 = (T["t_in"] >= vsp.START_MS) & ~p21
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn = np.nan_to_num(T["turn"], nan=-1) >= vsp.SETUPS[SETUP][2]
    elig = vsp.s1.eligible_syms(a.data_dir)
    top = vsp.top_n_days(vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig), vsp.top_n_of(SETUP))
    intop = vsp.in_prev_day_top(T, top)
    base = T["kind"] == "base"
    v_ok, p_ok = p21 & demo & turn & intop, p21 & demo & turn
    S = {
        "V": vsp.chain(T, base & v_ok),
        "P": vsp.chain(T, base & p_ok),
        "V20": vsp.chain(T, base & p20 & demo & turn & intop),
        "P20": vsp.chain(T, base & p20 & demo & turn),
    }
    for mod in ADD:
        mk = T["kind"] == mod
        S[f"V_{mod}"] = vsp.chain(T, (base | mk) & v_ok)
        S[f"P_{mod}"] = vsp.chain(T, (base | mk) & p_ok)
        S[f"add_{mod}"] = S[f"V_{mod}"] & mk
        S[f"addP_{mod}"] = S[f"P_{mod}"] & mk
    return S, top


# ---------------------------------------------------------------- placebo


def placebo_symbol(args: tuple[str, str, str, str, str, list[list[Any]]]) -> dict[str, Any]:
    s, s1_cache, data_dir, regime_cache, out_dir, jobs = args
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    btc = btc_ctx(s1_cache, data_dir, regime_cache)
    rc = m0.hourly_returns(h1.t, h1.c)
    rb = m0.aligned(btc["t"], btc["r"], h1.t)
    rows = []
    for jb in jobs:
        mode, key, kk, t_in, sf = jb[0], jb[1], int(jb[2]), int(jb[3]), float(jb[4])
        if not (t_in < END):
            continue
        e = m0.close_at(h1.t, h1.c, t_in)
        if not (np.isfinite(e) and e > 0):
            continue
        st = e * (1 - sf)
        tg = e + 2.0 * (e - st)
        b = long_(h1, t_in, e, st, tg, vsp.F1)
        if b is None:
            continue
        r: dict[str, Any] = {"m": mode, "k": key, "i": kk, "B": b.r_net}
        if mode == "x":
            r["res"] = h1_fields(btc, h1.t, rc, rb, t_in, b.exit_ms, e, st, b.r_net, None)["res"]
        rows.append(r)
    out = Path(out_dir) / "placebo"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{s}.json").write_text(json.dumps(rows, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "rss": vsp.rss_mb()}


def stage_placebo(a: argparse.Namespace, out: Path) -> None:
    T = load(out)
    S, top = samples(a, T)
    have = set(vsp.syms_with_1h(a.s1_cache, a.data_dir))
    by: dict[str, list[list[Any]]] = defaultdict(list)
    cross = S["V"] | S["P"] | S["V20"] | S["P20"]
    for mod in ADD:
        cross |= S[f"add_{mod}"] | S[f"addP_{mod}"]
    for i in np.flatnonzero(cross):
        key, sym, t = str(T["k"][i]), str(T["sym"][i]), int(T["t_in"][i])
        prev = (t // DAY_MS) * DAY_MS - DAY_MS
        pool = sorted((top.get(prev, set()) & have) - {sym})
        for kk, c in enumerate(sm.draw_coins(key, pool, K_COINS)):
            by[c].append(["x", key, kk, t, float(T["stop_pct"][i])])
    for i in np.flatnonzero(S["add_H3"] | S["addP_H3"]):
        key = str(T["k"][i])
        for kk, tt in enumerate(m0.same_coin_times(key, int(T["t_in"][i]), K_COINS)):
            by[str(T["sym"][i])].append(["same", key, kk, tt, float(T["stop_pct"][i])])
    emit(f"[placebo] jobs {sum(len(v) for v in by.values())}, coins {len(by)}")
    jobs = [(c, str(a.s1_cache), str(a.data_dir), str(a.regime_cache), str(out), v) for c, v in sorted(by.items())]
    if a.limit:
        jobs = jobs[: a.limit]
    v38.pool_run(placebo_symbol, jobs, a.workers, "placebo")


# ---------------------------------------------------------------- report


def label(r: dict[str, float], passed: bool) -> str:
    if passed:
        return "ПРОЙШОВ"
    mde = MDE_K * (r["hi"] - r["lo"]) / 3.92
    if r["hi"] < INTEREST:
        return f"✗ (верхня межа {r['hi']:+.3f} < {INTEREST:.2f}: корисний ефект виключено; MDE₈₀ {mde:.3f})"
    if mde > INTEREST:
        return f"◯ невимірно (MDE₈₀ {mde:.3f} > {INTEREST:.2f})"
    return f"✗ (MDE₈₀ {mde:.3f})"


def mean(v: np.ndarray, m: np.ndarray) -> float:
    x = v[m & np.isfinite(v)]
    return float(np.mean(x)) if x.size else math.nan


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T = load(out)
    S, _ = samples(a, T)
    why = json.loads(str(T["_why"][0]))
    IS, OOS = T["t_in"] < vsp.OOS_MS, T["t_in"] >= vsp.OOS_MS
    yrs = (END - vsp.OOS_MS) / (365.25 * DAY_MS)
    B, V, P = T["B"], S["V"], S["P"]
    paired, fmtd = v38.paired, v38.fmtd
    emit(
        f"# S40 modules: rows {T['B'].size} {dict(Counter(T['kind']))}; detector: s40_all signals {why.get('detector_signals')}, generic mismatches {why.get('detector_mismatch')}; H4 added with j-r<=6: {why.get('H4_lag_le6', 0)}"
    )
    PT = vsp.load_rows(a.passport_cache / SETUP)
    pv = vsp.chain(
        PT, vsp.demo_list(PT, SETUP, vsp.top_n_days(vsp.s1.daily_qv(a.s1_cache, a.data_dir, vsp.s1.eligible_syms(a.data_dir)), 250))
    )
    pk = {(str(s), int(t)): float(b) for s, t, b in zip(PT["sym"][pv], PT["t_in"][pv], PT["B"][pv], strict=True)}
    mk = {(str(s), int(t)): float(b) for s, t, b in zip(T["sym"][V], T["t_in"][V], B[V], strict=True)}
    same = sum(1 for k, b in mk.items() if k in pk and abs(pk[k] - b) < 1e-9)
    emit(
        f"база V: n {int(V.sum())} (паспорт {len(pk)}), однакових угод з тим самим R {same}; IS {int((V & IS).sum())}, OOS {int((V & OOS).sum())}"
    )
    rb = paired(T, B, V & OOS)
    emit(
        f"база OOS V {fmtd(rb)} (n {int(rb['n'])}, тижнів {int(rb['k'])}); 2021+ {fmtd(paired(T, B, V))}; сума R / рік OOS {np.sum(B[V & OOS]) / yrs:+.1f}"
    )

    pl: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for p in sorted((out / "placebo").glob("*.json")):
        for r in json.loads(p.read_text()):
            pl[r["k"]][r["m"]].append(r["B"])
            if r["m"] == "x" and r.get("res") is not None:
                pl[r["k"]]["xres"].append(r["res"])

    def pcol(f: str) -> np.ndarray:
        return np.array([np.mean(pl[k][f]) if k in pl and pl[k][f] else math.nan for k in T["k"]])

    PB, PRES, PS = pcol("x"), pcol("xres"), pcol("same")
    X = B - PB
    rx = paired(T, X, V & OOS)
    emit(
        f"база S40 − плацебо «випадковий лонг тієї ж свічки» OOS {fmtd(rx)} p₂ {rx['p']:.3f}; плацебо E[R] {mean(PB, V & OOS):+.3f}; IS {fmtd(paired(T, X, V & IS))}"
    )
    years = (("2025", T["year"] == 2025), ("2026", T["year"] == 2026))
    res: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------ H1
    emit("\n## H1")
    R1, D = T["res"], T["res"] - B
    m1 = V & OOS & np.isfinite(R1)
    emit(
        f"β визначена: V OOS {int(m1.sum())} з {int((V & OOS).sum())}; 2021+ {int((V & np.isfinite(R1)).sum())} з {int(V.sum())}; медіана β {np.nanmedian(T['beta'][m1]):.2f}"
    )
    r = paired(T, D, m1)
    rr_ = paired(T, R1, m1)
    rraw = paired(T, B, m1)
    xr = paired(T, R1 - PRES, m1 & np.isfinite(PRES))
    emit(f"Δ = R_res − R OOS {fmtd(r)} p₁ {r['p1']:.4f} (тижнів {int(r['k'])})")
    emit(
        f"  E[R_res] OOS {fmtd(rr_)} (ширина {rr_['hi'] - rr_['lo']:.3f}) проти сирого R тих самих угод {fmtd(rraw)} (ширина {rraw['hi'] - rraw['lo']:.3f}); SD R_res {np.std(R1[m1]):.2f}, R {np.std(B[m1]):.2f}"
    )
    emit(f"  проти плацебо (X_res = R_res − R̄_res випадкового лонга) {fmtd(xr)} p₁ {xr['p1']:.4f}; плацебо E[R_res] {mean(PRES, m1):+.3f}")
    hd = paired(T, T["hedge"], m1)
    emit(
        f"  хедж: E[R_hedge] OOS {fmtd(hd)}; ×1.5 {mean(T['hedge15'], m1):+.3f}; витрати хеджу {mean(R1 - T['hedge'], m1):+.3f}R; E[r_BTC за утримання] {mean(T['btc'], m1):+.4%}"
    )
    ri, rP = paired(T, D, V & IS & np.isfinite(R1)), paired(T, D, P & OOS & np.isfinite(R1))
    emit(
        f"  IS Δ {fmtd(ri)}, E[R_res] IS {fmtd(paired(T, R1, V & IS))}; 2021+ Δ {fmtd(paired(T, D, V))}, E[R_res] 2021+ {fmtd(paired(T, R1, V))}; P OOS Δ {fmtd(rP)}"
    )
    cells = [(lab, mean(D, m1 & c)) for lab, c in years]
    mt, topc = v38.drop_top(T, np.where(np.isfinite(D), D, 0.0), m1)
    cells.append(("без топ-5 монет", mean(D, mt)))
    emit("  розбивки OOS (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(topc)})")
    emit(
        "  E[R_res] за роками V: "
        + "; ".join(
            f"{y} {mean(R1, V & (T['year'] == y)):+.3f} (R {mean(B, V & (T['year'] == y) & np.isfinite(R1)):+.3f})"
            for y in range(2021, 2027)
        )
    )
    for mod in ADD:
        q = S[f"V_{mod}"] & OOS & np.isfinite(R1)
        emit(f"  на V_mod {mod} OOS (описово): E[R] {mean(B, q):+.3f} → E[R_res] {mean(R1, q):+.3f}")
    v38.per_asset(T, np.where(V, D, math.nan), V & IS, V & OOS, "H1")
    res["H1"] = {
        "r": r,
        "own": xr["p1"],
        "cond2": rr_["m"] > 0 and xr["m"] > 0,
        "cells": cells,
        "P": rP["m"],
        "er15": mean(T["hedge15"], m1),
        "er": rr_["m"],
    }

    # ------------------------------------------------ H3, H4: added events
    for mod in ADD:
        add, addP, Vm = S[f"add_{mod}"], S[f"addP_{mod}"], S[f"V_{mod}"]
        mo_ = add & OOS
        emit(f"\n## {mod}")
        cand = (T["kind"] == mod) & (T["t_in"] >= vsp.PASSPORT_MS)
        emit(
            f"кандидатів 2021+ {int(cand.sum())}; додано у V_mod: IS {int((add & IS).sum())}, OOS {int(mo_.sum())}; витіснено угод бази OOS {int(np.sum(V & OOS & ~Vm))}"
        )
        r = paired(T, X, mo_)
        re_ = paired(T, B, mo_)
        emit(
            f"X = R − R̄_плацебо доданих OOS {fmtd(r)} p₁ {r['p1']:.4f} (тижнів {int(r['k'])}, n з плацебо {int(r['n'])}); плацебо E[R] {mean(PB, mo_):+.3f}"
        )
        emit(
            f"  E[R] доданих OOS {fmtd(re_)} p₁ {re_['p1']:.4f} (n {int(re_['n'])}); ×1.5 {mean(T['B15'], mo_):+.3f}; витрати {mean(T['cost'], mo_):.3f}R; % ціни {mean(T['pct'], mo_):+.4%}; медіана стопу {np.nanmedian(T['stop_pct'][mo_]):.2%}"
        )
        emit(
            f"  сума R / рік OOS: V_mod {np.sum(B[Vm & OOS]) / yrs:+.1f} проти V {np.sum(B[V & OOS]) / yrs:+.1f}; угод / рік {np.sum(Vm & OOS) / yrs:.0f} проти {np.sum(V & OOS) / yrs:.0f}; сума % ціни / рік {np.nansum(T['pct'][Vm & OOS]) / yrs:+.2f} проти {np.nansum(T['pct'][V & OOS]) / yrs:+.2f}"
        )
        ri = paired(T, X, add & IS)
        rP = paired(T, X, addP & OOS)
        emit(
            f"  IS: X {fmtd(ri)}, E[R] {fmtd(paired(T, B, add & IS))} (n {int((add & IS).sum())}); 2021+ X {fmtd(paired(T, X, add))}, E[R] {fmtd(paired(T, B, add))}; P OOS X {fmtd(rP)}"
        )
        cells = [(lab, mean(X, mo_ & c)) for lab, c in years]
        mt, topc = v38.drop_top(T, np.where(np.isfinite(X), X, 0.0), mo_)
        cells.append(("без топ-5 монет", mean(X, mt)))
        cells.append(("IS", ri["m"]))
        extra = True
        if mod == "H3":
            xs = paired(T, B - PS, mo_)
            emit(f"  проти тієї ж монети в інший час (X') {fmtd(xs)} p₁ {xs['p1']:.4f}; плацебо E[R] {mean(PS, mo_):+.3f}")
            extra = xs["m"] > 0
            for tag in FAMILY:
                q = add & (T["tag"] == tag)
                emit(
                    f"  батько {tag}: OOS n {int(np.sum(q & OOS))}, E[R] {mean(B, q & OOS):+.3f}, X {mean(X, q & OOS):+.3f}; IS n {int(np.sum(q & IS))}, E[R] {mean(B, q & IS):+.3f}, X {mean(X, q & IS):+.3f}"
                )
            q = mo_ & (T["tag"] != "S23")
            rn = paired(T, X, q)
            emit(f"  без S23 OOS: X {fmtd(rn)} (n {int(rn['n'])}), E[R] {mean(B, q):+.3f}")
            cells.append(("без S23", rn["m"]))
        else:
            vm = Vm & (T["t_in"] >= vsp.PASSPORT_MS)
            emit(
                "  E[R] за j − r на V_mod 2021+ (база ≤ 6, додані 7…12): "
                + "; ".join(f"{k}: {mean(B, vm & (T['lag'] == k)):+.3f} (n {int(np.sum(vm & (T['lag'] == k)))})" for k in range(1, 13))
            )
            ok = vm & (T["lag"] >= 1)
            sl = np.polyfit(T["lag"][ok].astype(float), B[ok], 1)[0] if ok.sum() > 2 else math.nan
            emit(
                f"  нахил E[R] за j − r (МНК, описово) {sl:+.4f}R на свічку; база ≤ 6 OOS {mean(B, Vm & OOS & (T['kind'] == 'base')):+.3f}, додані {mean(B, mo_):+.3f}, разом {mean(B, Vm & OOS):+.3f}"
            )
        emit("  розбивки OOS (X): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(topc)})")
        for g in ("top30", "ge5m", "1to5m"):
            mm = mo_ & (T["grp"] == g)
            emit(f"  група {g}: n {int(mm.sum())}, E[R] {mean(B, mm):+.3f}, X {mean(X, mm):+.3f}")
        v38.per_asset(T, np.where(add, X, math.nan), add & IS, add & OOS, mod)
        res[mod] = {
            "r": r,
            "own": r["p1"],
            "cond2": re_["m"] > 0 and extra,
            "cells": cells,
            "P": rP["m"],
            "er15": mean(T["B15"], mo_),
            "er": re_["m"],
        }

    # ------------------------------------------------ H5: 2020
    emit("\n## H5 (2020)")
    V20, P20 = S["V20"], S["P20"]
    r = paired(T, X, V20)
    re_ = paired(T, B, V20)
    emit(
        f"V 2020: n {int(V20.sum())}, монет {len(set(T['sym'][V20]))}, тижнів {len(set(T['week'][V20]))}; кандидатів S40 2020 (≥ $1M) {int(np.sum((T['kind'] == 'base') & (T['year'] == 2020)))}"
    )
    emit(f"X = R − R̄_плацебо {fmtd(r)} p₁ {r['p1']:.4f} (тижнів {int(r['k'])}); плацебо E[R] {mean(PB, V20):+.3f}")
    emit(
        f"  E[R] 2020 {fmtd(re_)} p₁ {re_['p1']:.4f}; ×1.5 {mean(T['B15'], V20):+.3f}; сума R {np.sum(B[V20]):+.1f}; виходи {dict(Counter(T['B_k'][V20]))}"
    )
    h1_, h2_ = V20 & (T["t_in"] < Y2020_MID), V20 & (T["t_in"] >= Y2020_MID)
    cells = [("2020 H1", mean(X, h1_)), ("2020 H2", mean(X, h2_))]
    mt, topc = v38.drop_top(T, np.where(np.isfinite(X), X, 0.0), V20)
    cells.append(("без топ-5 монет", mean(X, mt)))
    rP = paired(T, X, P20)
    emit(
        f"  півріччя: E[R] {mean(B, h1_):+.3f} (n {int(h1_.sum())}) / {mean(B, h2_):+.3f} (n {int(h2_.sum())}); P 2020 X {fmtd(rP)}, E[R] {mean(B, P20):+.3f} (n {int(P20.sum())})"
    )
    emit(f"  E[R_res] 2020 (H1-міра) {fmtd(paired(T, T['res'], V20))}")
    emit("  розбивки (X): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(topc)})")
    res["H5"] = {"r": r, "own": r["p1"], "cond2": re_["m"] > 0, "cells": cells, "P": rP["m"], "er15": mean(T["B15"], V20), "er": re_["m"]}

    # ------------------------------------------------ verdict: Holm over 4
    pm = {mn: res[mn]["r"]["p1"] for mn in MODULES}
    adj = dict(zip(MODULES, holm([pm[x] if np.isfinite(pm[x]) else 1.0 for x in MODULES]), strict=True))
    emit("\n## Вердикт (Холм на 4)")
    passed = []
    for mn in MODULES:
        x = res[mn]
        robust = all(np.isfinite(v) and v > 0 for _, v in x["cells"])
        own_ok = x["own"] < 0.05 if mn == "H1" else True
        ok = x["r"]["m"] > 0 and adj[mn] < 0.05 and own_ok and x["cond2"] and robust and x["P"] > 0 and x["er15"] > 0
        emit(
            f"{mn}: {x['r']['m']:+.3f} [{x['r']['lo']:+.3f}; {x['r']['hi']:+.3f}], p₁ {pm[mn]:.4f}, Холм {adj[mn]:.4f}, умова 2 {'так' if x['cond2'] and own_ok else 'ні'}, стійкість {'так' if robust else 'ні'}, P {x['P']:+.3f}, E[R] {x['er']:+.3f}, ×1.5 {x['er15']:+.3f} → {label(x['r'], ok)}"
        )
        if ok:
            passed.append(mn)
    emit(f"пройшли: {passed or 'жоден'}")
    emit(
        "комбінація: пройшло ≥ 2 з H1 / H3 / H4 — порахувати окремо"
        if len([p for p in passed if p != "H5"]) >= 2
        else "комбінації немає (з H1 / H3 / H4 пройшло менше двох)"
    )
    cnt = Counter(int(x) for x in T["year"][V & OOS])
    emit(
        f"угод V OOS за роками {dict(sorted(cnt.items()))}; кандидати за причинами {dict(sorted((k, v) for k, v in why.items() if not k.startswith('detector')))}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("sanity", "run", "placebo", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s40_modules")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--regime-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/cycle_regime")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-s23", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"verify_s40_modules --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"sanity": stage_sanity, "run": stage_run, "placebo": stage_placebo, "report": stage_report}[a.stage](a, out)
        emit(f"peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
