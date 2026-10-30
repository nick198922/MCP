#!/usr/bin/env python3
"""S39 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b): H2 (S39 on 1h and 2h), H8 (failures of the S14 / S16 /
S23 longs), H3 (structural stop for signals whose rule stop is over 10 %), H4 (exit on a 4h close back above the range
low) and H7 (the swept range low is not a previous day / week / month low), each against the S39 passport base, with
the registered placebos (own: permuted stop distance / exit level, random subgroup; cross-coin: a random coin of the
same bar, same side and geometry). Registration: the card's section «Попередня реєстрація модулів S39».

    uv run python scripts/verify_s39_modules.py --stage sanity     # timing of 3 coins, detector check (no R printed)
    uv run python scripts/verify_s39_modules.py --stage run        # per-symbol candidate rows (local caches, 4 workers)
    uv run python scripts/verify_s39_modules.py --stage placebo    # own and cross-coin placebo trades
    uv run python scripts/verify_s39_modules.py --stage report     # tables (also <cache>/report.txt)

No network: 1h bars from the S14 cache, BTC trend / regimes from the S39 passport cache.
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

from tradedesk.analytics import s3_modules as mo  # noqa: E402
from tradedesk.analytics import s38_modules as sm  # noqa: E402
from tradedesk.analytics import s39_modules as m9  # noqa: E402
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
MODULES = ("H2", "H8", "H3", "H4", "H7")
ADD = ("H2_1h", "H2_2h", "H8", "H3")
N_PLACEBO_SUB = 2000
K_COINS = 3
SETUP = "S39"
FAMILY = ("S14", "S16", "S23")
INTEREST = {"H2": 0.10, "H8": 0.10, "H3": 0.10, "H4": 0.05, "H7": 0.15}
MDE_K = 3.17  # z(0.99) + z(0.80): the strictest Holm step over 5, 80 % power
emit = vsp.emit


def short(path_m: Bars, t_in: int, e: float, st: float, tg: float, fees: Any) -> Outcome | None:
    """Rule-B short (real prices) on the mirrored 1h path."""
    return simulate_stop_rule(path_m, t_in, -e, -st, -tg, HOLD, vsp.RULE_B, fees=fees)


def thesis(path_m: Bars, t_in: int, e: float, st: float, tg: float, level: float, fees: Any) -> Outcome | None:
    return m9.thesis_exit(path_m, t_in, -e, -st, -tg, HOLD, vsp.RULE_B, -level, BAR4, fees)


def parents(tb: Bars, kind: str) -> list[m9.Parent]:
    if kind in ("S3", "S16"):
        sigs = [s for s in ss.range_all(tb, "4h") if s.setup == kind]
    elif kind == "S14":
        sigs = ss.s14_all(tb, "4h")
    else:
        sigs = ss.s23_all(tb, "4h")
    return [m9.Parent(int(np.searchsorted(tb.t, s.signal_ts)), s.stop, s.target, kind) for s in sigs if s.side == "long"]


# ---------------------------------------------------------------- per-symbol rows


def run_symbol(args: tuple[str, str, str, str, str, bool]) -> dict[str, Any]:
    s, s1_cache, data_dir, pp_dir, out_dir, with_s23 = args
    t0 = time.time()
    s1c, dd, pp = Path(s1_cache), Path(data_dir), Path(pp_dir)
    member = vsp.s1.Membership(vsp.s1.members(s1c))
    btc = json.loads((pp / "btc_trend.json").read_text())
    btc_t = np.asarray(btc["t"], dtype=np.int64) + DAY_MS
    btc_tr = np.asarray(btc["trend"], dtype=np.int64)
    h1, qv = vsp.load_1h_qv(s1c, dd, s)
    path = h1.mirrored()
    daily = resample(h1, 24)
    d_tr = sp.trend_series(daily)
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    timing: dict[str, float] = {}

    def row(kind: str, tf: str, t_in: int, e: float, st: float, tg: float, tag: str = "") -> dict[str, Any] | None:
        if not (vsp.START_MS <= t_in < END) or not (tg < e < st):
            return None
        top = member(s, t_in)
        turn = sp.trailing_turnover(t_in, h1.t, qv)
        grp = sp.turnover_group(top, turn)
        if grp in (sp.LT1M, sp.UNKNOWN):
            why[f"{kind}:{grp}"] += 1
            return None
        b = short(path, t_in, e, st, tg, vsp.F1)
        if b is None:
            why[f"{kind}:no_path"] += 1
            return None
        b15 = short(path, t_in, e, st, tg, vsp.F15)
        j = int(np.searchsorted(btc_t, t_in, side="right")) - 1
        rr = reward_risk_at("short", e, st, tg)
        why[f"{kind}:cand"] += 1
        return {
            "k": f"{s}|{tf}|{kind}|{t_in}",
            "sym": s,
            "tf": tf,
            "kind": kind,
            "tag": tag,
            "t_in": t_in,
            "top30": top,
            "turn": turn,
            "grp": grp,
            "rr": rr,
            "stop_pct": (st - e) / e,
            "score": sp.score_points("short", rr, sp.trend_at(daily, d_tr, t_in), int(btc_tr[j]) if j >= 0 else 0, s == vsp.BTC),
            "B": b.r_net,
            "B_x": b.exit_ms,
            "B15": None if b15 is None else b15.r_net,
            "cost": b.cost_r,
            "pct": m9.price_return(b.r_net, -e, -st),
            "e": e,
            "st": st,
            "tg": tg,
        }

    def add_h4(r: dict[str, Any], rl: float, a_j: float) -> None:
        e, st, tg, t_in = r["e"], r["st"], r["tg"], r["t_in"]
        lv = m9.thesis_level(rl, a_j)
        r["H4f"] = (lv - e) / (st - e)
        r["H4on"] = bool(lv < st)
        h4 = thesis(path, t_in, e, st, tg, lv, vsp.F1) if lv < st else None
        h415 = thesis(path, t_in, e, st, tg, lv, vsp.F15) if lv < st else None
        r["H4"] = r["B"] if h4 is None else h4.r_net
        r["H4_15"] = r["B15"] if h415 is None else h415.r_net
        r["H4k"] = "" if h4 is None else h4.exit_kind
        r["H4pct"] = m9.price_return(r["H4"], -e, -st)

    for tf, hours in (("4h", 4), ("2h", 2), ("1h", 1)):
        tt = time.time()
        tb = h1 if hours == 1 else resample(h1, hours)
        if len(tb) < ss.RANGE_W + 20:
            continue
        pos = segment_pos(tb.t)
        a_arr = atr(tb, 14, pos)
        used: set[int] = set()
        f3 = m9.failed_long(tb, pos, a_arr, parents(tb, "S3"), used=used)
        if tf != "4h":
            for f in f3:
                r = row(f"H2_{tf}", tf, int(tb.t[f.j]) + tb.bar_ms, f.entry, f.stop, f.target)
                if r:
                    rows.append(r)
            timing[tf] = time.time() - tt
            continue
        # base: the passport's detector; the generic detector must give the same signals
        base = ss.s39_all(tb, "4h")
        gen = {(int(tb.t[f.j]), round(f.entry, 12), round(f.stop, 12)) for f in f3}
        ref = {(int(x.signal_ts), round(x.entry, 12), round(x.stop, 12)) for x in base}
        why["detector_mismatch"] += len(gen ^ ref)
        why["detector_signals"] += len(ref)
        for sig in base:
            ind = sig.indicators
            t_in = sig.signal_ts + tb.bar_ms
            rl, a_j = float(ind["range_low"]), float(ind["atr14"])
            r = row("base", "4h", t_in, sig.entry, sig.stop, sig.target)
            if r is not None:
                add_h4(r, rl, a_j)
                i_r = int(np.searchsorted(tb.t, int(ind["sweep_ts"])))
                a_prev = float(a_arr[i_r - 1]) if i_r >= 1 else math.nan
                r["H7"] = m9.non_htf_edge(h1.t, h1.low, int(ind["sweep_ts"]), rl, a_prev)
                rows.append(r)
            if (sig.stop - sig.entry) / sig.entry > vsp.MAX_STOP:
                st = m9.structural_stop(sig.entry, sig.stop, rl, a_j)
                r3 = row("H3", "4h", t_in, sig.entry, st, sig.entry - ss.SHORT_TARGET_R * (st - sig.entry))
                if r3 is not None:
                    r3["base_stop_pct"] = (sig.stop - sig.entry) / sig.entry
                    add_h4(r3, rl, a_j)
                    rows.append(r3)
        timing["4h"] = time.time() - tt
        # H8: the failures of the S14 / S16 / S23 longs; bars the base already took are not repeated
        fam = []
        for kind in FAMILY:
            if kind == "S23" and not with_s23:
                continue
            tk = time.time()
            fam.extend(parents(tb, kind))
            timing[kind] = time.time() - tk
        for f in m9.failed_long(tb, pos, a_arr, fam, used=used):
            r = row("H8", "4h", int(tb.t[f.j]) + tb.bar_ms, f.entry, f.stop, f.target, f.tag)
            if r:
                rows.append(r)
    out = Path(out_dir) / "rows"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{s}.json").write_text(json.dumps({"rows": rows, "why": dict(why)}, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t0, "timing": timing, "rss": vsp.rss_mb()}


def stage_sanity(a: argparse.Namespace, out: Path) -> None:
    t0 = time.time()
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        r = run_symbol((s, str(a.s1_cache), str(a.data_dir), str(a.passport_cache / SETUP), str(out / "sanity"), True))
        cnt = Counter(x["kind"] for x in json.loads((out / "sanity" / "rows" / f"{s}.json").read_text())["rows"])
        emit(
            f"timing {s}: {r['sec']:.1f}s ({', '.join(f'{k} {v:.1f}s' for k, v in r['timing'].items())}); rows {dict(cnt)}; detector signals {r['why'].get('detector_signals', 0)}, mismatches {r['why'].get('detector_mismatch', 0)}; rss {r['rss']:.0f} MB"
        )
    n = len(vsp.syms_with_1h(a.s1_cache, a.data_dir))
    emit(
        f"3 symbols in {time.time() - t0:.0f}s -> about {(time.time() - t0) / 3 * n / a.workers / 60:.0f} min for {n} on {a.workers} workers"
    )


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [
        (s, str(a.s1_cache), str(a.data_dir), str(a.passport_cache / SETUP), str(out), not a.no_s23)
        for s in syms
        if a.force or not (out / "rows" / f"{s}.json").exists()
    ]
    v38.pool_run(run_symbol, jobs, a.workers, "run")


# ---------------------------------------------------------------- samples


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
        if k in ("k", "sym", "tf", "kind", "tag", "grp", "H4k"):
            T[k] = np.array(["" if x is None else str(x) for x in col])
        elif k in ("t_in", "B_x"):
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
    period = T["t_in"] >= vsp.PASSPORT_MS
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn = np.nan_to_num(T["turn"], nan=-1) >= vsp.SETUPS[SETUP][2]
    elig = vsp.s1.eligible_syms(a.data_dir)
    top = vsp.top_n_days(vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig), vsp.top_n_of(SETUP))
    v_ok = period & demo & turn & vsp.in_prev_day_top(T, top)
    p_ok = period & demo & turn
    base = T["kind"] == "base"
    S = {"V": vsp.chain(T, base & v_ok), "P": vsp.chain(T, base & p_ok)}
    for mod in ADD:
        mk = T["kind"] == mod
        S[f"V_{mod}"] = vsp.chain(T, (base | mk) & v_ok)
        S[f"P_{mod}"] = vsp.chain(T, (base | mk) & p_ok)
        S[f"add_{mod}"] = S[f"V_{mod}"] & mk
        S[f"addP_{mod}"] = S[f"P_{mod}"] & mk
    return S, top


# ---------------------------------------------------------------- placebo (own + cross-coin)


def placebo_symbol(args: tuple[str, str, str, str, list[list[Any]]]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir, jobs = args
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    path = h1.mirrored()
    rows = []
    for job in jobs:
        mode, key, kk, t_in = job[0], job[1], job[2], int(job[3])
        if mode == "x":  # cross-coin: same stop distance in %, entry at this coin's 1h close before t_in
            sf, f = float(job[4]), float(job[5])
            i = int(np.searchsorted(h1.t, t_in - HOUR_MS))
            if i >= len(h1) or int(h1.t[i]) != t_in - HOUR_MS:
                continue
            e, st, tg = sm.placebo_levels(float(h1.c[i]), sf)
            b = short(path, t_in, e, st, tg, vsp.F1)
            if b is None:
                continue
            h4 = b.r_net
            if np.isfinite(f) and f < 1.0:
                o = thesis(path, t_in, e, st, tg, e + f * (st - e), vsp.F1)
                h4 = math.nan if o is None else o.r_net
            rows.append({"m": mode, "k": key, "i": kk, "B": b.r_net, "H4": h4})
        elif mode == "own3":  # H3: the same entry, the stop distance of another added trade
            e, sf = float(job[4]), float(job[5])
            st = e * (1 + sf)
            b = short(path, t_in, e, st, e - 2.0 * (st - e), vsp.F1)
            if b is not None:
                rows.append({"m": mode, "k": key, "i": 0, "B": b.r_net, "pct": m9.price_return(b.r_net, -e, -st)})
        else:  # own4: the thesis exit at the level fraction of another trade
            e, st, tg, lv = (float(x) for x in job[4:8])
            o = thesis(path, t_in, e, st, tg, lv, vsp.F1) if lv < st else short(path, t_in, e, st, tg, vsp.F1)
            if o is not None:
                rows.append({"m": mode, "k": key, "i": 0, "B": o.r_net})
    out = Path(out_dir) / "placebo"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{s}.json").write_text(json.dumps(rows, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "rss": vsp.rss_mb()}


def stage_placebo(a: argparse.Namespace, out: Path) -> None:
    T = load(out)
    S, top = samples(a, T)
    have = set(vsp.syms_with_1h(a.s1_cache, a.data_dir))
    by: dict[str, list[list[Any]]] = defaultdict(list)
    cross = S["V"] | S["P"]
    for mod in ADD:
        cross |= S[f"add_{mod}"] | S[f"addP_{mod}"]
    for i in np.flatnonzero(cross):
        key, sym, t = str(T["k"][i]), str(T["sym"][i]), int(T["t_in"][i])
        prev = (t // DAY_MS) * DAY_MS - DAY_MS
        pool = sorted((top.get(prev, set()) & have) - {sym})
        f = float(T["H4f"][i]) if T["kind"][i] in ("base", "H3") else math.nan
        for kk, c in enumerate(sm.draw_coins(key, pool, K_COINS)):
            by[c].append(["x", key, kk, t, float(T["stop_pct"][i]), f])
    # own H4: the level fraction of another base trade (V or P)
    idx = np.flatnonzero((S["V"] | S["P"]) & (T["kind"] == "base"))
    fp = m9.permuted(T["H4f"][idx], vsp.SEED)
    for i, f in zip(idx, fp, strict=True):
        e, st = float(T["e"][i]), float(T["st"][i])
        by[str(T["sym"][i])].append(["own4", str(T["k"][i]), 0, int(T["t_in"][i]), e, st, float(T["tg"][i]), e + f * (st - e)])
    # own H3: the stop distance of another added H3 trade
    idx = np.flatnonzero(S["add_H3"] | S["addP_H3"])
    sp3 = m9.permuted(T["stop_pct"][idx], vsp.SEED)
    for i, d in zip(idx, sp3, strict=True):
        by[str(T["sym"][i])].append(["own3", str(T["k"][i]), 0, int(T["t_in"][i]), float(T["e"][i]), float(d)])
    emit(f"[placebo] jobs {sum(len(v) for v in by.values())}, coins {len(by)}")
    jobs = [(c, str(a.s1_cache), str(a.data_dir), str(out), v) for c, v in sorted(by.items())]
    if a.limit:
        jobs = jobs[: a.limit]
    v38.pool_run(placebo_symbol, jobs, a.workers, "placebo")


# ---------------------------------------------------------------- report


def label(name: str, r: dict[str, float], passed: bool) -> str:
    if passed:
        return "ПРОЙШОВ"
    mde = MDE_K * (r["hi"] - r["lo"]) / 3.92
    if r["hi"] < INTEREST[name]:
        return f"✗ (верхня межа {r['hi']:+.3f} < {INTEREST[name]:.2f}: корисний ефект виключено; MDE₈₀ {mde:.3f})"
    if mde > INTEREST[name]:
        return f"◯ невимірно (MDE₈₀ {mde:.3f} > {INTEREST[name]:.2f})"
    return f"✗ (MDE₈₀ {mde:.3f})"


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T = load(out)
    S, _ = samples(a, T)
    why = json.loads(str(T["_why"][0]))
    n = T["B"].size
    IS, OOS = T["t_in"] < vsp.OOS_MS, T["t_in"] >= vsp.OOS_MS
    yrs = (END - vsp.OOS_MS) / (365.25 * DAY_MS)
    yrs_all = (END - vsp.PASSPORT_MS) / (365.25 * DAY_MS)
    B, V, P = T["B"], S["V"], S["P"]
    paired = v38.paired
    fmtd = v38.fmtd
    emit(
        f"# S39 modules: rows {n} {dict(Counter(T['kind']))}; detector: s39_all signals {why.get('detector_signals')}, generic mismatches {why.get('detector_mismatch')}"
    )
    # base vs the passport, to the trade
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
        f"база OOS V {fmtd(rb)} (n {int(rb['n'])}, тижнів {int(rb['k'])}); 2021+ {fmtd(paired(T, B, V))}; сума R / рік OOS {np.sum(B[V & OOS]) / yrs:+.1f}, угод / рік {np.sum(V & OOS) / yrs:.0f}"
    )

    # placebo columns
    pl: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    own: dict[str, dict[str, float]] = defaultdict(dict)
    for p in sorted((out / "placebo").glob("*.json")):
        for r in json.loads(p.read_text()):
            if r["m"] == "x":
                for f in ("B", "H4"):
                    if r[f] is not None and np.isfinite(r[f]):
                        pl[r["k"]][f].append(r[f])
            else:
                own[r["k"]][r["m"]] = r["B"]
                if "pct" in r:
                    own[r["k"]]["own3pct"] = r["pct"]

    def pcol(f: str) -> np.ndarray:
        return np.array([np.mean(pl[k][f]) if k in pl and pl[k][f] else math.nan for k in T["k"]])

    def ocol(f: str) -> np.ndarray:
        return np.array([own.get(k, {}).get(f, math.nan) for k in T["k"]])

    PB, PH4 = pcol("B"), pcol("H4")
    X = B - PB
    rx = paired(T, X, V & OOS)
    emit(
        f"база S39 − плацебо «монета тієї ж свічки» OOS V {fmtd(rx)} p₂ {rx['p']:.3f}; плацебо E[R] {np.nanmean(PB[V & OOS]):+.3f}; IS {fmtd(paired(T, X, V & IS))}"
    )
    years = (("2025", T["year"] == 2025), ("2026", T["year"] == 2026))
    res: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------ modules that add trades: H2 (1h, 2h), H8, H3
    for mod in ADD:
        add, addP, Vm = S[f"add_{mod}"], S[f"addP_{mod}"], S[f"V_{mod}"]
        mo_ = add & OOS
        emit(f"\n## {mod}")
        cand = (T["kind"] == mod) & (T["t_in"] >= vsp.PASSPORT_MS)
        emit(
            f"кандидатів 2021+ {int(cand.sum())}; відбір демо пройшли (з ≥ $1M) {int(np.sum(cand & (T['rr'] >= vsp.MIN_RR) & (T['stop_pct'] <= vsp.MAX_STOP) & (T['score'] >= vsp.MIN_SCORE)))}; додано у V_mod: IS {int((add & IS).sum())}, OOS {int(mo_.sum())}; витіснено угод бази OOS {int(np.sum(V & OOS & ~Vm))}"
        )
        r = paired(T, B, mo_)
        rc = paired(T, X, mo_ & np.isfinite(X))
        emit(
            f"E[R] доданих OOS {fmtd(r)} p₁ {r['p1']:.4f} (тижнів {int(r['k'])}); ×1.5 {np.nanmean(T['B15'][mo_]):+.3f}; витрати {np.nanmean(T['cost'][mo_]):.3f}R; % ціни на угоду {np.nanmean(T['pct'][mo_]):+.4%}; медіана стопу {np.nanmedian(T['stop_pct'][mo_]):.2%}"
        )
        emit(
            f"  проти монети тієї ж свічки (X) {fmtd(rc)} p₁ {rc['p1']:.4f}; плацебо E[R] {np.nanmean(PB[mo_]):+.3f} (n з плацебо {int(np.sum(mo_ & np.isfinite(X)))})"
        )
        rown = None
        if mod == "H3":
            O3, O3p = ocol("own3"), ocol("own3pct")
            rown = paired(T, B - O3, mo_ & np.isfinite(O3))
            rpp = paired(T, T["pct"] - O3p, mo_ & np.isfinite(O3p))
            emit(
                f"  проти власного плацебо (перестановка відстані стопу) R {fmtd(rown)} p₁ {rown['p1']:.4f}; у % ціни {rpp['m']:+.4%} [{rpp['lo']:+.4%}; {rpp['hi']:+.4%}]"
            )
            emit(
                f"  медіана базового стопу доданих {np.nanmedian(T['base_stop_pct'][mo_]):.1%}, нового {np.nanmedian(T['stop_pct'][mo_]):.1%}"
            )
        Vm_o = Vm & OOS
        emit(
            f"  сума R / рік OOS: V_mod {np.sum(B[Vm_o]) / yrs:+.1f} проти V {np.sum(B[V & OOS]) / yrs:+.1f}; угод / рік {np.sum(Vm_o) / yrs:.0f} проти {np.sum(V & OOS) / yrs:.0f}; сума % ціни / рік {np.nansum(T['pct'][Vm_o]) / yrs:+.2f} проти {np.nansum(T['pct'][V & OOS]) / yrs:+.2f}"
        )
        ri = paired(T, B, add & IS)
        rP = paired(T, B, addP & OOS)
        emit(f"  IS доданих {fmtd(ri)} (n {int(ri['n'])}); OOS P {fmtd(rP)} (n {int(rP['n'])}); 2021+ {fmtd(paired(T, B, add))}")
        cells = [(lab, float(np.mean(B[mo_ & c])) if np.any(mo_ & c) else math.nan) for lab, c in years]
        mt, topc = v38.drop_top(T, B, mo_)
        cells.append(("без топ-5 монет", float(np.mean(B[mt])) if mt.any() else math.nan))
        emit("  розбивки OOS: " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(topc)})")
        for g in ("top30", "ge5m", "1to5m"):
            mm = mo_ & (T["grp"] == g)
            emit(f"  група {g}: n {int(mm.sum())}, E[R] {np.mean(B[mm]) if mm.any() else math.nan:+.3f}")
        if mod == "H8":
            for tag in FAMILY:
                mm = add & (T["tag"] == tag)
                emit(
                    f"  батько {tag}: OOS n {int(np.sum(mm & OOS))}, E[R] {np.mean(B[mm & OOS]) if np.any(mm & OOS) else math.nan:+.3f}; 2021+ n {int(mm.sum())}, E[R] {np.mean(B[mm]) if mm.any() else math.nan:+.3f}"
                )
        v38.per_asset(T, np.where(add, B, math.nan), add & IS, add & OOS, mod)
        res[mod] = {"r": r, "rc": rc, "rown": rown, "cells": cells, "rP": rP, "er15": float(np.nanmean(T["B15"][mo_])), "er": r["m"]}

    # ------------------------------------------------ H4: thesis exit on all V trades
    emit("\n## H4")
    H4, H415 = T["H4"], T["H4_15"]
    on = T["H4on"] == 1.0
    d = H4 - B
    m4 = V & OOS
    r = paired(T, d, m4)
    O4 = ocol("own4")
    O4 = np.where(on, O4, B)
    rown = paired(T, H4 - O4, m4 & np.isfinite(O4))
    dp = PH4 - PB
    rc = paired(T, d - dp, m4 & np.isfinite(dp))
    trig = T["H4k"] == "thesis"
    emit(
        f"OOS V: n {int(r['n'])}, модуль діє (рівень нижче стопу) {np.mean(on[m4]):.0%}, вихід за закриттям {np.mean(trig[m4]):.0%}; Δ {fmtd(r)} p₁ {r['p1']:.4f}"
    )
    emit(
        f"  проти власного плацебо (перестановка рівня) {fmtd(rown)} p₁ {rown['p1']:.4f}; проти монети тієї ж свічки (Δ − Δплацебо) {fmtd(rc)} p₁ {rc['p1']:.4f}"
    )
    er = paired(T, H4, m4)
    emit(
        f"  E[R] OOS V: база {np.mean(B[m4]):+.3f}, модуль {fmtd(er)}; ×1.5: база {np.nanmean(T['B15'][m4]):+.3f}, модуль {np.nanmean(H415[m4]):+.3f}"
    )
    emit(
        f"  % ціни на угоду: база {np.nanmean(T['pct'][m4]):+.4%}, модуль {np.nanmean(T['H4pct'][m4]):+.4%}; сума R / рік {np.sum(B[m4]) / yrs:+.1f} → {np.sum(H4[m4]) / yrs:+.1f}"
    )
    emit(
        f"  Δ на угодах, де спрацював вихід: {np.mean(d[m4 & trig]) if np.any(m4 & trig) else math.nan:+.3f} (n {int(np.sum(m4 & trig))}); середня частка рівня f {np.nanmean(T['H4f'][m4]):.2f}"
    )
    ri, rP = paired(T, d, V & IS), paired(T, d, P & OOS)
    emit(f"  IS V Δ {fmtd(ri)}; OOS P Δ {fmtd(rP)}; 2021+ Δ {fmtd(paired(T, d, V))}")
    cells = [(lab, float(np.mean(d[m4 & c]))) for lab, c in years]
    mt, topc = v38.drop_top(T, d, m4)
    cells.append(("без топ-5 монет", float(np.mean(d[mt]))))
    emit("  розбивки OOS V (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(topc)})")
    v38.per_asset(T, np.where(V, d, math.nan), V & IS, V & OOS, "H4")
    res["H4"] = {"r": r, "rc": rc, "rown": rown, "cells": cells, "rP": rP, "er15": float(np.nanmean(H415[m4])), "er": er["m"]}
    # H3 and H4 together (both move the stop): H4 on the H3 sample
    vm3 = S["V_H3"] & OOS
    emit(
        f"  разом з H3 (H4 на V_mod H3, OOS): E[R] {np.mean(B[vm3]):+.3f} → {np.mean(H4[vm3]):+.3f}; сума R / рік {np.sum(B[vm3]) / yrs:+.1f} → {np.sum(H4[vm3]) / yrs:+.1f}"
    )

    # ------------------------------------------------ H7: subgroup on V 2021+ (main), OOS alongside
    emit("\n## H7")
    fv = T["H7"]
    defined = np.isfinite(fv)
    flag = fv == 1.0
    mm = V & defined
    share = float(np.mean(flag[mm])) if mm.any() else math.nan
    emit(
        f"частка ознаки (край не HTF-рівень): V 2021+ {share:.1%} (визначена {int(mm.sum())} з {int(V.sum())}); IS {np.mean(flag[mm & IS]):.1%}; OOS {np.mean(flag[mm & OOS]):.1%}"
    )
    r = v38.sub_delta(T, B, flag, mm)
    obs, plc = mo.subgroup_placebo(B[mm], flag[mm], N_PLACEBO_SUB, vsp.SEED)
    pp = float((np.sum(plc >= obs) + 1) / (plc.size + 1)) if plc.size else math.nan
    rc = v38.sub_delta(T, X, flag, mm & np.isfinite(X))
    emit(
        f"V 2021+: з ознакою n {int(r['n'])}, E[R] {np.mean(B[mm & flag]):+.3f} проти {np.mean(B[mm]):+.3f} усіх, без ознаки {np.mean(B[mm & ~flag]):+.3f}; Δ {fmtd(r)} p₁ {r['p1']:.4f}"
    )
    emit(f"  плацебо-підгрупа p {pp:.4f}; монета тієї ж свічки (E[X|ознака] − E[X]) {fmtd(rc)} p₁ {rc['p1']:.4f}")
    ri, ro = v38.sub_delta(T, B, flag, mm & IS), v38.sub_delta(T, B, flag, mm & OOS)
    rP = v38.sub_delta(T, B, flag, P & defined)
    emit(f"  IS Δ {fmtd(ri)}; OOS Δ {fmtd(ro)} (E[R] з ознакою {np.mean(B[mm & OOS & flag]):+.3f}); P 2021+ Δ {fmtd(rP)}")
    cells = [(lab, float(np.mean(B[mm & c & flag]) - np.mean(B[mm & c]))) for lab, c in years]
    cells += [("IS", ri["m"]), ("OOS", ro["m"])]
    contrib = np.where(flag, B - np.mean(B[mm]), 0.0)
    mt, topc = v38.drop_top(T, contrib, mm)
    cells.append(("без топ-5 монет", float(np.mean(B[mt & flag]) - np.mean(B[mt]))))
    emit("  розбивки (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(topc)})")
    er15 = float(np.nanmean(T["B15"][mm & OOS & flag]))
    emit(
        f"  E[R] ×1.5 з ознакою OOS {er15:+.3f}, 2021+ {np.nanmean(T['B15'][mm & flag]):+.3f}; угод з ознакою / рік {np.sum(mm & flag) / yrs_all:.0f}"
    )
    for lab, c in (("з H7", flag), ("без H7", ~flag)):
        q = V & OOS & defined & c
        emit(f"  Δ H4 у угодах {lab} (OOS): {np.mean(d[q]) if q.any() else math.nan:+.3f} (n {int(q.sum())})")
    v38.per_asset(T, np.where(mm & flag, B - np.mean(B[mm]), math.nan), V & IS, V & OOS, "H7")
    res["H7"] = {
        "r": r,
        "rc": rc,
        "plc_p": pp,
        "cells": cells,
        "rP": rP,
        "er15": er15,
        "er": float(np.mean(B[mm & OOS & flag])),
        "share": share,
    }

    # ------------------------------------------------ verdict: Holm over 5 (H2 = 2 x min of 1h / 2h)
    p1h, p2h = res["H2_1h"]["r"]["p1"], res["H2_2h"]["r"]["p1"]
    h2v = "H2_1h" if p1h <= p2h else "H2_2h"
    pm = {
        "H2": min(1.0, 2 * min(p1h, p2h)),
        "H8": res["H8"]["r"]["p1"],
        "H3": res["H3"]["r"]["p1"],
        "H4": res["H4"]["r"]["p1"],
        "H7": res["H7"]["r"]["p1"],
    }
    adj = dict(zip(MODULES, holm([pm[x] if np.isfinite(pm[x]) else 1.0 for x in MODULES]), strict=True))
    emit(f"\n## Вердикт (Холм на 5; H2: Холм на 2 всередині — p_1h {p1h:.4f}, p_2h {p2h:.4f}, кандидат {h2v})")
    passed = []
    for mn in MODULES:
        x = res[h2v if mn == "H2" else mn]
        if mn in ("H2", "H8"):
            own_p = x["rc"]["p1"]
        elif mn in ("H3", "H4"):
            own_p = x["rown"]["p1"]
        else:
            own_p = x["plc_p"]
        robust = all(np.isfinite(v) and v > 0 for _, v in x["cells"])
        ok = x["r"]["m"] > 0 and adj[mn] < 0.05 and own_p < 0.05 and x["rc"]["m"] > 0 and robust and x["rP"]["m"] > 0 and x["er15"] > 0
        if mn == "H7" and not (0.2 <= x["share"] <= 0.8):
            ok = False
        lab = label(mn, x["r"], ok)
        if mn == "H7" and not (0.2 <= x["share"] <= 0.8):
            lab = "◯ невимірно (частка поза 20–80 %)"
        emit(
            f"{mn}: {x['r']['m']:+.3f} [{x['r']['lo']:+.3f}; {x['r']['hi']:+.3f}], p₁ {pm[mn]:.4f}, Холм {adj[mn]:.4f}, власне плацебо p {own_p:.4f}, монета тієї ж свічки {x['rc']['m']:+.3f}, стійкість {'так' if robust else 'ні'}, P {x['rP']['m']:+.3f}, E[R] {x['er']:+.3f}, ×1.5 {x['er15']:+.3f} → {lab}"
        )
        if ok:
            passed.append(mn)
    emit(f"пройшли: {passed or 'жоден'}")
    if len(passed) >= 2:
        emit("комбінація: пройшло ≥ 2 — порахувати окремо (не реалізовано автоматично)")
    else:
        emit("комбінації немає (пройшло менше двох модулів)")
    cnt = Counter(int(x) for x in T["year"][V & OOS])
    emit(
        f"угод V OOS за роками {dict(sorted(cnt.items()))}; кандидати за причинами {dict(sorted((k, v) for k, v in why.items() if not k.startswith('detector')))}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("sanity", "run", "placebo", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s39_modules")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-s23", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"verify_s39_modules --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"sanity": stage_sanity, "run": stage_run, "placebo": stage_placebo, "report": stage_report}[a.stage](a, out)
        emit(f"peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
