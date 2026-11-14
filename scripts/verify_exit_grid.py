#!/usr/bin/env python3
"""Exit grid for the demo setups (IDEA-20261114-exit-grid): the same entries and stops as the setup passports, the
registered exit variants B* (the demo's current target, the passport's B), A 1.5R, B 2R, C 3R, D a structural target
floored at 1R, E half at 1R / half at 3R, F a 3 x ATR(14) trail after 1R. Pre-registration: the card's section
«Попередня реєстрація» (committed before this script).

    uv run python scripts/verify_exit_grid.py --stage sanity   # joins, base reproduction, n gate; prints no R
    uv run python scripts/verify_exit_grid.py --stage run      # per-symbol variant rows (4 workers)
    uv run python scripts/verify_exit_grid.py --stage report   # tables -> <cache>/report.txt

No network: the passport caches only (1h Binance USD-M bars, membership, listing / daily turnover, the passport's
candidate rows), all filled earlier through the runtime providers. Signals come again from the passport's detectors on
the same bars and are joined to the passport rows by (symbol, timeframe, side, t_in); the demo selection uses the
passport row's fields. Returns are in R (the live risk unit), not on capital or notional.
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

import verify_course_s14_ema as s1  # noqa: E402
import verify_setup_passport as vp  # noqa: E402
from common_helpers import rss_mb, write_json  # noqa: E402

from tradedesk.analytics import exit_grid as eg  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.course_s14_ema import atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm, without_top_days  # noqa: E402
from tradedesk.analytics.stop_management import Fees, StopRule, cochran_q, max_drawdown, simulate_stop_rule  # noqa: E402

DAY_MS = vp.DAY_MS
SEED, N_BOOT = 20261030, 10_000
OOS_MS = s1.ms("2024-01-01T00:00")
IS21_MS = s1.ms("2021-01-01T00:00")
SETUPS = ("S14", "S16", "S3", "S12", "S28", "S17", "S38", "S39", "S40")
TFS = {"S17": ("1d",)}  # the demo trades S17 on 1d only (owner 31.10)
TWO_R = ("S12", "S17", "S28", "S38", "S39", "S40")
D_LEVEL = ("S14", "S3", "S12", "S28")  # S16: D = B* (not tested); S17, S38-S40: not defined
FAMILY = {
    "S14": "ABCDEF",
    "S16": "ABCEF",
    "S3": "ABCDEF",
    "S12": "ACDEF",
    "S28": "ACDEF",
    "S17": "ACEF",
    "S38": "ACEF",
    "S39": "ACEF",
    "S40": "ACEF",
}
VARS = ("Bs", "A", "B", "C", "D", "E", "F")
NOBE = ("Bs", "A", "B", "C", "D")
MIN_OOS_N, MIN_OOS_DAYS, DELTA_MIN = 100, 50, 0.05
RULE_B, RULE_A, F1, F15 = vp.RULE_B, StopRule("A"), vp.F1, vp.F15
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def static_ok(r: dict[str, Any], floor: float, tfs: tuple[str, ...], lim: bool) -> bool:
    """The demo selection that needs only the passport row (R:R after fees, stop, score, turnover floor, the fill)."""
    turn = r.get("turn")
    return (
        r["tf"] in tfs
        and (not lim or bool(r.get("filled")))
        and r["rr"] >= vp.MIN_RR
        and r["stop_pct"] <= vp.MAX_STOP
        and r["score"] >= vp.MIN_SCORE
        and turn is not None
        and float(turn) >= floor
    )


def d_level(setup: str, sig: Any, sg: float, path: Any, bar_ms: int, t_fill: int) -> float | None:
    """The registered structural level D in the trade's (long) price space, only from bars closed before the entry."""
    ind = sig.indicators
    if setup == "S14":
        return sg * float(ind["impulse_extreme"])
    if setup == "S3":
        return sg * float(ind["range_high"] if sig.side == "long" else ind["range_low"])
    if setup == "S12":  # from the open of the first of the three FVG bars to the hour before the fill hour
        return eg.window_high(path, sig.signal_ts - 2 * bar_ms, t_fill)
    if setup == "S28":  # from the open of the bearish OB candle to the hour before the fill hour
        return eg.window_high(path, int(ind["ob_ts"]), t_fill)
    return None


def run_symbol(args: tuple[str, str, str, str, str, str, str]) -> dict[str, Any]:
    setup, s, s1c, dd, pdir, odir, mode = args
    t0 = time.time()
    det, tfs_all, floor = vp.SETUPS[setup]
    tfs = TFS.get(setup, tfs_all)
    lim = setup in vp.LIMIT
    prow = json.loads((Path(pdir) / setup / "cands" / f"{s}.json").read_text())["rows"]
    why: Counter[str] = Counter()
    # join key: (timeframe, side, t_in) as registered, plus the stop distance (the passport's own expression on the same
    # signal, bit-identical): S17 / S28 place several limits on one bar at different levels (deviation note, 2026-11-15).
    # A key can still repeat (S17: the same level and stop from two POBs on one bar, identical trades): matched as a
    # multiset in detector order, so the sample is the passport's row for row
    sel: dict[tuple[str, str, int, float], list[dict[str, Any]]] = defaultdict(list)
    unfilled: list[dict[str, Any]] = []
    keep_keys = ("sym", "tf", "side", "t_in", "rr", "stop_pct", "score", "turn")
    for r in prow:
        if lim and r["tf"] in tfs and not r.get("filled") and static_ok({**r, "filled": True}, floor, tfs, lim):
            unfilled.append({k: r[k] for k in keep_keys})
        if not static_ok(r, floor, tfs, lim):
            continue
        key = (r["tf"], r["side"], int(r["t_in"]), float(r["stop_pct"]))
        why["dup_passport_key"] += int(key in sel)
        sel[key].append(r)
    n_sel = sum(len(v) for v in sel.values())
    why["passport_sel"] = n_sel
    rows: list[dict[str, Any]] = []
    if sel:
        h1, _ = vp.load_1h_qv(Path(s1c), Path(dd), s)
        n_found = 0
        for tf in tfs:
            tb = resample(h1, vp.HOURS[tf])
            a_arr = atr(tb, 14, segment_pos(tb.t))
            for sig in det(tb, tf):
                t_in = sig.signal_ts + tb.bar_ms
                key = (tf, sig.side, t_in, abs(sig.entry - sig.stop) / abs(sig.entry))
                if not sel.get(key):
                    continue
                r = sel[key].pop(0)
                n_found += 1
                rows_one = one_trade(setup, s, sig, r, tb, a_arr, h1, mode, why)
                if rows_one is not None:
                    rows.append(rows_one)
        why["missing_join"] = n_sel - n_found
    out = Path(odir) / setup / mode
    write_json(out / f"{s}.json", {"rows": rows, "unfilled": unfilled, "why": dict(why)})
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t0, "rss": rss_mb()}


def one_trade(
    setup: str, s: str, sig: Any, r: dict[str, Any], tb: Any, a_arr: np.ndarray, h1: Any, mode: str, why: Counter[str]
) -> dict[str, Any] | None:
    tf = sig.timeframe
    t_in = sig.signal_ts + tb.bar_ms
    i = int(np.searchsorted(tb.t, sig.signal_ts))
    sg = 1.0 if sig.side == "long" else -1.0
    e, st, tg = sg * sig.entry, sg * sig.stop, sg * sig.target
    path = h1 if sig.side == "long" else h1.mirrored()
    if setup in vp.LIMIT:
        hold = vp.own_hold(setup, sig, tf, tb.bar_ms)
        bs, tk = sp.limit_trade(path, t_in, int(sig.indicators["rest_until_ms"]), e, st, tg, hold, RULE_B, F1, True, False)
        if bs is None:
            why["refill_mismatch"] += 1
            return None
        start, hold2 = eg.limit_start(path, int(np.searchsorted(path.t, tk)), st, hold)
    else:
        tk, start, hold2 = -1, t_in, vp.HOLD[tf]
        bs = simulate_stop_rule(path, start, e, st, tg, hold2, RULE_B, fees=F1)
        if bs is None:
            why["no_path"] += 1
            return None
    r1 = e - st
    lvl = d_level(setup, sig, sg, path, tb.bar_ms, tk)
    d_tg = eg.floored_target(e, st, lvl) if lvl is not None else None
    tg2 = tg if setup in TWO_R else eg.r_target(e, st, 2.0)
    targets = {"Bs": tg, "A": eg.r_target(e, st, 1.5), "B": tg2, "C": eg.r_target(e, st, 3.0), "D": d_tg}
    row: dict[str, Any] = {k: r[k] for k in ("sym", "tf", "side", "t_in", "rr", "stop_pct", "score", "turn")}
    row.update(t_fill=tk, pB=r["B"], diff=abs(bs.r_net - float(r["B"])), Bs=bs.r_net, Bs_k=bs.exit_kind)
    row["tg2_rel"] = abs(tg - eg.r_target(e, st, 2.0)) / abs(e) if setup in TWO_R else math.nan
    a_i = float(a_arr[i])
    row["atr_ok"] = bool(math.isfinite(a_i) and a_i > 0)
    if d_tg is not None:
        row["d_floor"] = bool(not (lvl is not None and math.isfinite(lvl)) or lvl <= e + r1)
        row["d_ne"] = bool(abs(d_tg - tg) > 1e-12 * abs(e))
    if mode == "sanity":
        return row

    def sim(tgt: float, rule: StopRule, fees: Fees) -> float | None:
        o = simulate_stop_rule(path, start, e, st, tgt, hold2, rule, fees=fees)
        return None if o is None else o.r_net

    for fk, fees in (("", F1), ("15", F15)):
        for v, tgv in targets.items():
            if tgv is None:
                continue
            if v == "Bs" and fk == "":
                continue
            if fk == "":
                o = simulate_stop_rule(path, start, e, st, tgv, hold2, RULE_B, fees=fees)
                row[v], row[v + "_k"] = (o.r_net, o.exit_kind) if o else (None, None)
            else:
                row[v + fk] = sim(tgv, RULE_B, fees)
        ox = eg.simulate_split(path, start, e, st, hold2, fees)
        of = eg.simulate_atr_trail(path, start, e, st, hold2, a_i, fees)
        row["E" + fk], row["F" + fk] = (ox.r_net if ox else None), (of.r_net if of else None)
        if fk == "":
            row["E_k"], row["F_k"] = (ox.exit_kind if ox else None), (of.exit_kind if of else None)
    for v in NOBE:
        tgv = targets[v]
        if tgv is not None:
            row[v + "n"] = sim(tgv, RULE_A, F1)
    return row


# ---------------------------------------------------------------- stages


def passport_syms(pdir: Path, setup: str, limit: int | None) -> list[str]:
    syms = sorted(p.stem for p in (pdir / setup / "cands").glob("*.json"))
    return syms[:limit] if limit else syms


def compute(a: argparse.Namespace, setups: list[str], mode: str) -> None:
    for setup in setups:
        syms = passport_syms(a.passport_cache, setup, a.limit)
        jobs = [(setup, s, str(a.s1_cache), str(a.data_dir), str(a.passport_cache), str(a.cache_dir), mode) for s in syms]
        t0 = time.time()
        tot: Counter[str] = Counter()
        with Pool(a.workers, maxtasksperchild=20) as pool:
            for k, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
                tot.update(r["why"])
                if k % 100 == 0 or k == len(jobs):
                    emit(
                        f"  [{mode}] {setup} {k}/{len(jobs)} {time.time() - t0:.0f}s; worker rss {r['rss']:.0f} MB; main rss {rss_mb():.0f} MB"
                    )
        emit(f"[{mode}] {setup} done in {time.time() - t0:.0f}s; {dict(sorted(tot.items()))}")


def load(a: argparse.Namespace, setup: str, mode: str, part: str = "rows") -> dict[str, np.ndarray]:
    rows: list[dict[str, Any]] = []
    for p in sorted((a.cache_dir / setup / mode).glob("*.json")):
        rows.extend(json.loads(p.read_text()).get(part, []))
    if not rows:
        return {}
    keys = sorted({k for r in rows for k in r})
    T: dict[str, np.ndarray] = {}
    for k in keys:
        col = [r.get(k) for r in rows]
        if k in ("sym", "tf", "side") or k.endswith("_k"):
            T[k] = np.array(["" if x is None else str(x) for x in col], dtype=object)
        elif k in ("d_floor", "d_ne", "atr_ok"):
            T[k] = np.array([bool(x) for x in col])
        else:
            T[k] = np.array([math.nan if x is None else float(x) for x in col])
    T["t_in"] = T["t_in"].astype(np.int64)
    T["day"] = T["t_in"] // DAY_MS
    T["week"] = T["t_in"] // sp.WEEK_MS
    T["year"] = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in T["t_in"]])
    return T


def select(T: dict[str, np.ndarray], setup: str, tops: dict[int, dict[int, set[str]]]) -> np.ndarray:
    """The demo list of the last closed day (top-N) on top of the static selection; S38 at most 2 per signal bar."""
    if not T:
        return np.zeros(0, dtype=bool)
    m = vp.in_prev_day_top(T, tops[vp.top_n_of(setup)])
    cap = vp.BAR_CAP.get(setup)
    if cap:
        idx = np.flatnonzero(m)
        sub = {k: T[k][idx] for k in ("turn", "rr", "score")}
        keep = eg.cap_per_bar(T["t_in"][idx], vp.demo_rank(sub), cap)
        m = np.zeros(m.size, dtype=bool)
        m[idx[keep]] = True
    return m


def top_lists(a: argparse.Namespace) -> tuple[dict[int, dict[int, set[str]]], dict[str, list[tuple[int, float]]]]:
    qv = s1.daily_qv(a.s1_cache, a.data_dir, s1.eligible_syms(a.data_dir))
    return {n: vp.top_n_days(qv, n) for n in (vp.TOP_N, vp.WIDE_TOP_N)}, qv


def gate(T: dict[str, np.ndarray], m: np.ndarray) -> tuple[int, int, bool]:
    o = m & (T["t_in"] >= OOS_MS)
    n, days = int(o.sum()), int(np.unique(T["day"][o]).size)
    return n, days, n >= MIN_OOS_N and days >= MIN_OOS_DAYS


def stage_sanity(a: argparse.Namespace, setups: list[str]) -> None:
    compute(a, setups, "sanity")
    tops, _ = top_lists(a)
    d20 = sorted(t for t in tops[vp.TOP_N] if s1.ms("2020-01-01T00:00") <= t < s1.ms("2021-01-01T00:00"))
    n20 = [len(tops[vp.TOP_N][t]) for t in d20]
    emit(f"coverage 2020: {len(d20)} UTC days with a top-100 list, {min(n20, default=0)}..{max(n20, default=0)} coins/day")
    emit(
        "| сетап | паспорт (стат. відбір) | з'єднано | не знайдено | дубл. | max abs(B*−B паспорта) | > 1e-9 | 2R-ціль abs відн. | після топ-N/ліміту: паспорт / наші | 2020 | IS | OOS n / днів | поріг | D=підлога | D≠B* | ATR ok |"
    )
    emit("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    fail = False
    for setup in setups:
        whys: Counter[str] = Counter()
        for p in (a.cache_dir / setup / "sanity").glob("*.json"):
            whys.update(json.loads(p.read_text())["why"])
        T = load(a, setup, "sanity")
        if not T:
            emit(f"| {setup} | {whys['passport_sel']} | 0 | — |")
            fail = True
            continue
        m = select(T, setup, tops)
        # the passport's sample on the same selection: its static rows (joined or not) with the same top-N / cap
        P = passport_static(a, setup)
        pm = select(P, setup, tops)
        mx = float(np.nanmax(T["diff"]))
        bad = int(np.sum(T["diff"] > 1e-9))
        tg2 = f"{np.nanmax(T['tg2_rel']):.1e}" if setup in TWO_R else "—"
        n, days, ok = gate(T, m)
        y20 = int(np.sum(m & (T["year"] == 2020)))
        n_is = int(np.sum(m & (T["t_in"] < OOS_MS)))
        dfl = f"{np.mean(T['d_floor'][m]):.1%}" if "d_floor" in T and m.any() else "—"
        dne = f"{np.mean(T['d_ne'][m]):.1%}" if "d_ne" in T and m.any() else "—"
        emit(
            f"| {setup} | {whys['passport_sel']} | {len(T['t_in'])} | {whys['missing_join']} | {whys['dup_passport_key']}/{whys['dup_signal']} | {mx:.1e} | {bad} | {tg2} | {int(pm.sum())} / {int(m.sum())} | {y20} | {n_is} | {n} / {days} | {'так' if ok else 'ні (◯)'} | {dfl} | {dne} | {int(T['atr_ok'][m].sum())}/{int(m.sum())} |"
        )
        if bad or whys["missing_join"] or whys["refill_mismatch"] or int(pm.sum()) != int(m.sum()):
            fail = True
    emit("SANITY FAIL: stop, no report" if fail else "SANITY OK")


def passport_static(a: argparse.Namespace, setup: str) -> dict[str, np.ndarray]:
    _, tfs_all, floor = vp.SETUPS[setup]
    tfs, lim = TFS.get(setup, tfs_all), setup in vp.LIMIT
    rows: list[dict[str, Any]] = []
    for s in passport_syms(a.passport_cache, setup, a.limit):
        for r in json.loads((a.passport_cache / setup / "cands" / f"{s}.json").read_text())["rows"]:
            if static_ok(r, floor, tfs, lim):
                rows.append({k: r[k] for k in ("sym", "t_in", "rr", "score", "turn")})
    T = {k: np.array([r[k] for r in rows], dtype=object) for k in ("sym", "t_in", "rr", "score", "turn")}
    T["t_in"] = T["t_in"].astype(np.int64)
    for k in ("rr", "score", "turn"):
        T[k] = T[k].astype(float)
    return T


# ---------------------------------------------------------------- report


def bci(v: np.ndarray, cl: np.ndarray) -> dict[str, float]:
    return sp.mean_ci(v, cl, n_boot=N_BOOT, seed=SEED)


def pair(T: dict[str, np.ndarray], m: np.ndarray, v: str, sfx: str = "") -> tuple[np.ndarray, np.ndarray]:
    x, b = T.get(v + sfx), T.get("Bs" + sfx)
    if x is None or b is None:
        return np.zeros(0), np.zeros(0, dtype=bool)
    ok = m & np.isfinite(x) & np.isfinite(b)
    return x[ok] - b[ok], ok


def f3(x: float) -> str:
    return "—" if not math.isfinite(x) else f"{x:+.3f}"


def fp(x: float) -> str:
    return "—" if not math.isfinite(x) else f"{x:.3f}"


def stage_report(a: argparse.Namespace, setups: list[str]) -> None:
    tops, _ = top_lists(a)
    OUT["tops"] = tops
    data: dict[str, tuple[dict[str, np.ndarray], np.ndarray]] = {}
    cells: list[dict[str, Any]] = []
    for setup in setups:
        T = load(a, setup, "rows")
        m = select(T, setup, tops)
        if int(np.sum(T["diff"][m] > 1e-9)):
            emit(f"{setup}: base reproduction broken, no report")
            return
        data[setup] = (T, m)
        n, days, ok = gate(T, m)
        IS, OOS = m & (T["t_in"] < OOS_MS), m & (T["t_in"] >= OOS_MS)
        for v in FAMILY[setup]:
            d_o, k_o = pair(T, OOS, v)
            d_i, _ = pair(T, IS, v)
            cd = bci(d_o, T["day"][k_o])
            cw = bci(d_o, T["week"][k_o])
            cells.append(
                {
                    "setup": setup,
                    "v": v,
                    "gated": ok,
                    "n": cd["n"],
                    "d": cd["m"],
                    "lo": cd["lo"],
                    "hi": cd["hi"],
                    "p": cd["p"],
                    "pw": cw["p"],
                    "se": cd["se"],
                    "d_is": float(d_i.mean()) if d_i.size else math.nan,
                    "n_is": d_i.size,
                }
            )
    fam = [c for c in cells if c["gated"]]
    hd = holm([c["p"] for c in fam])
    hw = holm([c["pw"] for c in fam])
    for c, x, y in zip(fam, hd, hw, strict=True):
        c["holm"], c["holm_w"] = x, y
    emit(f"Холм: сімейство {len(fam)} клітинок (максимум 43); поріг n: OOS ≥ {MIN_OOS_N} угод і ≥ {MIN_OOS_DAYS} днів")
    emit("")
    emit("## Вердикти (OOS 2024+, Δ = варіант − B*, кластери за днем; тиждень — умова ≈)")
    emit(
        "| сетап | варіант | n OOS | Δ OOS [95 % CI] | p (день) | Холм p | p (тиждень) | Холм p (тиждень) | MDE₈₀ | n IS | Δ IS | вердикт |"
    )
    emit("|---|---|---|---|---|---|---|---|---|---|---|---|")
    best: dict[str, dict[str, Any]] = {}
    for c in cells:
        if not c["gated"]:
            verdict = "◯"
        else:
            pass_d = c["d"] >= DELTA_MIN and c["holm"] < 0.05 and c["d"] > 0 and c["d_is"] > 0
            verdict = ("✓" if c["holm_w"] < 0.05 else "≈") if pass_d else "✗"
        c["verdict"] = verdict
        emit(
            f"| {c['setup']} | {c['v']} | {int(c['n'])} | {f3(c['d'])} [{f3(c['lo'])}; {f3(c['hi'])}] | {fp(c['p'])} | {fp(c.get('holm', math.nan))} | {fp(c['pw'])} | {fp(c.get('holm_w', math.nan))} | {fp(eg.mde80(c['se']))} | {c['n_is']} | {f3(c['d_is'])} | {verdict} |"
        )
        if math.isfinite(c["d"]) and (c["setup"] not in best or c["d"] > best[c["setup"]]["d"]):
            best[c["setup"]] = c
    for setup in setups:
        T, m = data[setup]
        setup_tables(setup, T, m, a, [c for c in cells if c["setup"] == setup], best.get(setup))


def setup_tables(
    setup: str, T: dict[str, np.ndarray], m: np.ndarray, a: argparse.Namespace, cells: list[dict[str, Any]], best: dict[str, Any] | None
) -> None:
    IS, OOS = m & (T["t_in"] < OOS_MS), m & (T["t_in"] >= OOS_MS)
    n, days, ok = gate(T, m)
    emit("")
    emit(
        f"## {setup}: вибірка {int(m.sum())} (IS {int(IS.sum())}, OOS {n} / {days} днів; поріг {'пройдено' if ok else 'НЕ пройдено — описово'})"
    )
    if setup in vp.LIMIT:
        U = load(a, setup, "rows", "unfilled")
        nu = int(select(U, setup, OUT["tops"]).sum()) if U else 0
        emit(
            f"лімітки: заповнено {int(m.sum())} з {int(m.sum()) + nu} розміщень ({int(m.sum()) / max(1, int(m.sum()) + nu):.1%}); R на розміщення = E[R] × частка (описово)"
        )
    emit(
        "| варіант | IS E[R] | IS част. R>0 | IS макс. просадка R | OOS E[R] [95 % CI] | OOS част. R>0 | OOS макс. просадка R | OOS E[R] ×1.5 витрат |"
    )
    emit("|---|---|---|---|---|---|---|---|")
    for v in VARS:
        x = T.get(v)
        if x is None or not np.isfinite(x[m]).any():
            continue
        line = [v if v != "Bs" else "B*"]
        for M in (IS, OOS):
            k = M & np.isfinite(x)
            order = np.argsort(T["t_in"][k], kind="stable")
            r = x[k][order]
            if M is OOS:
                c = bci(r, T["day"][k][order])
                line.append(f"{f3(c['m'])} [{f3(c['lo'])}; {f3(c['hi'])}]")
            else:
                line.append(f3(float(r.mean())) if r.size else "—")
            line += [f"{np.mean(r > 0):.1%}" if r.size else "—", f"{max_drawdown(r):.1f}"]
        x15 = T.get(v + "15")
        line.append(f3(float(np.nanmean(x15[OOS]))) if x15 is not None and OOS.any() else "—")
        emit("| " + " | ".join(line) + " |")
    emit("")
    emit("Стійкість Δ (описово): ×1.5 витрат (OOS), IS 2021–2023, OOS без 5 днів з найбільшим внеском, по ТФ (OOS)")
    emit(
        "| варіант | Δ OOS ×1.5 [CI] | Δ IS 2021–23 [CI] | Δ OOS без топ-5 днів | "
        + " | ".join(f"Δ OOS {tf}" for tf in TFS.get(setup, vp.SETUPS[setup][1]))
        + " |"
    )
    emit("|---|---|---|---|" + "---|" * len(TFS.get(setup, vp.SETUPS[setup][1])))
    i21 = IS & (T["t_in"] >= IS21_MS)
    for v in FAMILY[setup]:
        d15, k15 = pair(T, OOS, v, "15")
        c15 = bci(d15, T["day"][k15])
        d21, k21 = pair(T, i21, v)
        c21 = bci(d21, T["day"][k21])
        d_o, k_o = pair(T, OOS, v)
        vals, _, _ = without_top_days(d_o.tolist(), T["day"][k_o].tolist(), 5)
        tfc = []
        for tf in TFS.get(setup, vp.SETUPS[setup][1]):
            dt, _ = pair(T, OOS & (T["tf"] == tf), v)
            tfc.append(f"{f3(float(dt.mean()))} (n {dt.size})" if dt.size else "—")
        emit(
            f"| {v} | {f3(c15['m'])} [{f3(c15['lo'])}; {f3(c15['hi'])}] | {f3(c21['m'])} [{f3(c21['lo'])}; {f3(c21['hi'])}] | {f3(float(np.mean(vals))) if vals else '—'} | "
            + " | ".join(tfc)
            + " |"
        )
    years = sorted({int(y) for y in T["year"][m]})
    emit("")
    emit("Δ по роках (середнє, n у дужках для B*)")
    emit("| варіант | " + " | ".join(f"{y} (n {int(np.sum(m & (T['year'] == y)))})" for y in years) + " |")
    emit("|---|" + "---|" * len(years))
    for v in FAMILY[setup]:
        cols = []
        for y in years:
            dy, _ = pair(T, m & (T["year"] == y), v)
            cols.append(f3(float(dy.mean())) if dy.size else "—")
        emit(f"| {v} | " + " | ".join(cols) + " |")
    emit("")
    emit("Без беззбитку (StopRule A), OOS: E[R] і Δ проти B* з беззбитком")
    cols = []
    for v in NOBE:
        x = T.get(v + "n")
        if x is None or not np.isfinite(x[OOS]).any():
            continue
        k = OOS & np.isfinite(x) & np.isfinite(T["Bs"])
        cols.append(f"{'B*' if v == 'Bs' else v}: {f3(float(x[k].mean()))} (Δ {f3(float((x[k] - T['Bs'][k]).mean()))})")
    emit("; ".join(cols))
    emit("")
    emit("Виходи за типом (уся вибірка, частки)")
    for v in VARS:
        kk = T.get(v + "_k")
        if kk is None:
            continue
        kinds = [str(x) for x in kk[m] if str(x)]
        if not kinds:
            continue
        cnt = Counter(kinds)
        emit(f"- {'B*' if v == 'Bs' else v}: " + ", ".join(f"{k} {c / len(kinds):.0%}" for k, c in cnt.most_common(6)))
    if "d_floor" in T and m.any():
        emit(f"D: підлога 1R у {np.mean(T['d_floor'][m]):.1%} угод, D ≠ B* у {np.mean(T['d_ne'][m]):.1%}")
    per_asset(setup, T, IS, OOS, cells, best)


def per_asset(
    setup: str, T: dict[str, np.ndarray], IS: np.ndarray, OOS: np.ndarray, cells: list[dict[str, Any]], best: dict[str, Any] | None
) -> None:
    emit("")
    emit("Перевірка по монетах (5a): Cochran Q / I² на OOS Δ по монетах з n ≥ 10")
    parts = []
    for c in cells:
        d, k = pair(T, OOS, c["v"])
        by: dict[str, list[float]] = defaultdict(list)
        for s, x in zip(T["sym"][k], d, strict=True):
            by[str(s)].append(float(x))
        g = [np.asarray(x) for x in by.values() if len(x) >= 10]
        if len(g) < 2:
            parts.append(f"{c['v']}: монет {len(g)}")
            continue
        q, df, i2 = cochran_q(np.array([x.mean() for x in g]), np.array([x.std(ddof=1) / math.sqrt(x.size) for x in g]))
        parts.append(
            f"{c['v']}: монет {len(g)}, Q {q:.1f} / df {df}, I² {i2:.0%}" if math.isfinite(q) else f"{c['v']}: монет {len(g)}, Q —"
        )
    emit("; ".join(parts))
    show = [c for c in cells if c.get("verdict") in ("✓", "≈")]
    if best is not None and best not in show:
        show.append(best)
    for c in show:
        v = c["v"]
        d_i, k_i = pair(T, IS, v)
        d_o, k_o = pair(T, OOS, v)
        isd: dict[str, list[float]] = defaultdict(list)
        for s, x in zip(T["sym"][k_i], d_i, strict=True):
            isd[str(s)].append(float(x))
        rank = sorted(((float(np.mean(x)), s) for s, x in isd.items() if len(x) >= 10), reverse=True)
        top = {s for _, s in rank[:10]}
        osym = np.array([str(s) for s in T["sym"][k_o]])
        in_top = np.array([s in top for s in osym])
        dc = sp.diff_ci(d_o, in_top, ~in_top, T["day"][k_o], n_boot=N_BOOT, seed=SEED)
        lab = "пройшла правило" if c.get("verdict") in ("✓", "≈") else "найбільша OOS Δ сетапу (описово)"
        emit(
            f"- {setup} {v} ({lab}): топ-10 монет за IS Δ (IS n ≥ 10, {len(rank)} монет) в OOS: n {int(in_top.sum())}, Δ {f3(float(d_o[in_top].mean())) if in_top.any() else '—'}; решта n {int((~in_top).sum())}, Δ {f3(float(d_o[~in_top].mean())) if (~in_top).any() else '—'}; різниця {f3(dc['d'])} [{f3(dc['lo'])}; {f3(dc['hi'])}] p {fp(dc['p'])}"
        )
        oby: dict[str, list[float]] = defaultdict(list)
        for s, x in zip(osym, d_o, strict=True):
            oby[s].append(float(x))
        big = sorted(oby.items(), key=lambda kv: -len(kv[1]))[:8]
        emit("  OOS монети з найбільшим n: " + "; ".join(f"{s} n {len(x)} Δ {f3(float(np.mean(x)))}" for s, x in big))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("sanity", "run", "report"), required=True)
    ap.add_argument("--setup", choices=SETUPS, action="append", default=None, help="repeatable; default all nine")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/exit_grid")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None, help="first N symbols per setup (smoke)")
    ap.add_argument("--out", type=Path, default=None, help="text output (default <cache>/<stage>.txt)")
    a = ap.parse_args()
    setups = a.setup or list(SETUPS)
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    out = a.out or a.cache_dir / f"{a.stage}.txt"
    with out.open("w") as fh:
        OUT["fh"] = fh
        emit(f"verify_exit_grid --stage {a.stage} {' '.join(setups)} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        if a.stage == "sanity":
            stage_sanity(a, setups)
        elif a.stage == "run":
            compute(a, setups, "rows")
        else:
            stage_report(a, setups)
        emit(f"peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
