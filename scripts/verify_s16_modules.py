#!/usr/bin/env python3
"""S16 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b): H6 half out at the range middle, H7 time exit without
progress to the middle, H8 target at the edge of the range's value area, H1 closes outside the range, H3 strength of
the bar closing back in, each measured against the S16 base of the passport on the same trades. Registration: the
card's section «Попередня реєстрація модулів S16» (written and staged before any run).

    uv run python scripts/verify_s16_modules.py --stage run      # per-symbol module rows (no network, 4 workers)
    uv run python scripts/verify_s16_modules.py --stage report   # tables (also <cache>/report.txt)

The base trades, the demo selection, the occupancy chain and the samples come from the S16 passport's candidate files
(``verify_setup_passport.py S16 --stage run``); this script joins its module rows to them by (symbol, timeframe, side,
entry time) and checks that its own base R equals the passport's.
"""

from __future__ import annotations

import argparse
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

import verify_s3_modules as v3  # noqa: E402  (paired / subgroup statistics, 5a, drop-top of the S3 modules)
import verify_setup_passport as vsp  # noqa: E402  (data loaders, samples, chain, CI helpers of the passport)

from tradedesk.analytics import s3_modules as m3  # noqa: E402
from tradedesk.analytics import s16_modules as mo  # noqa: E402
from tradedesk.analytics.course_basics import range_events  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import simulate_stop_rule  # noqa: E402

MODULES = ("H6", "H7", "H8", "H1", "H3")
N_BARS = {"4h": 12, "1d": 6}  # H7 window in signal bars (registered)
BIN_ATR = 0.25  # H8 profile bin
INTEREST = {"H6": 0.026, "H7": 0.026, "H8": 0.026, "H1": 0.25, "H3": 0.25}  # registered effect of interest
Z_MDE = 2.326 + 0.842  # one-sided alpha 0.01 (strictest Holm step of 5) and 80 % power
emit = vsp.emit
key_of = v3.key_of
fmtd = v3.fmtd


# ---------------------------------------------------------------- per-symbol module rows


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir = args
    t0 = time.time()
    out = Path(out_dir)
    det, tfs, _ = vsp.SETUPS["S16"]
    h1, qv = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    rows: list[dict[str, Any]] = []
    for tf in tfs:
        tb = resample(h1, vsp.HOURS[tf])
        bar_ms = tb.bar_ms
        oriented = {"long": tb, "short": tb.mirrored()}
        evs: dict[str, dict[int, Any]] = {}
        atrs: dict[str, np.ndarray] = {}
        for sd, bb in oriented.items():
            pos = segment_pos(bb.t)
            atrs[sd] = atr(bb, 14, pos)
            evs[sd] = {
                ev.r: ev for ev in range_events(bb, pos, atrs[sd], w=vsp.ss.RANGE_W, width_atr=vsp.ss.RANGE_WIDTH_ATR) if ev.kind == "dev"
            }
        for sig in det(tb, tf):
            t_in = sig.signal_ts + bar_ms
            if not (vsp.START_MS <= t_in < vsp.END[tf]):
                continue
            sg = 1.0 if sig.side == "long" else -1.0
            e, st, tg = sg * sig.entry, sg * sig.stop, sg * sig.target
            if not (st < e < tg):
                continue
            bb = oriented[sig.side]
            i = int(np.searchsorted(bb.t, sig.signal_ts))
            ev = evs[sig.side][i]
            a = float(atrs[sig.side][i])
            mid = float(ev.mid)
            path = h1 if sig.side == "long" else h1.mirrored()
            hold = vsp.HOLD[tf]
            base = simulate_stop_rule(path, t_in, e, st, tg, hold, vsp.RULE_B, fees=vsp.F1)
            if base is None:
                continue
            base15 = simulate_stop_rule(path, t_in, e, st, tg, hold, vsp.RULE_B, fees=vsp.F15)
            k = key_of(s, tf, sig.side, t_in)
            r: dict[str, Any] = {"k": k, "B": base.r_net}
            # H6: half out at the middle, the rest to the opposite edge; placebo at a random price; cost stress
            r["H6"] = mo.half_exit(path, t_in, e, st, mid, tg, hold, vsp.F1)[0]
            r["H6_15"] = mo.half_exit(path, t_in, e, st, mid, tg, hold, vsp.F15)[0]
            fp = e + m3.unit_draw(k, "h6") * (tg - e)
            r["H6p"] = mo.half_exit(path, t_in, e, st, fp, tg, hold, vsp.F1)[0] if e < fp < tg else None
            # H7: time exit after N bars without a touch of the middle; placebo exit at a random moment
            n_ms = N_BARS[tf] * bar_ms
            r["H7"], r["H7a"], r["H7f"] = mo.time_exit(path, t_in, e, st, tg, mid, n_ms, vsp.RULE_B, vsp.F1, base)
            if base15 is not None:
                r["H7_15"] = mo.time_exit(path, t_in, e, st, tg, mid, n_ms, vsp.RULE_B, vsp.F15, base15)[0]
            if r["H7a"]:
                hp = mo.placebo_hours(m3.unit_draw(k, "h7t"), n_ms)
                op = simulate_stop_rule(path, t_in, e, st, tg, hp * HOUR_MS, vsp.RULE_B, fees=vsp.F1)
                r["H7p"] = op.r_net if op is not None else None
            # H8: target at the value-area edge of the range (1h quote volume, bars of the range window, causal)
            vah = mo.range_vah(path, qv, int(bb.t[ev.s - vsp.ss.RANGE_W]), int(bb.t[ev.s]), BIN_ATR * a)
            r["H8a"] = bool(np.isfinite(vah) and e < vah < tg)
            r["H8"], r["H8_15"] = base.r_net, base15.r_net if base15 else None
            if r["H8a"]:
                o8 = simulate_stop_rule(path, t_in, e, st, vah, hold, vsp.RULE_B, fees=vsp.F1)
                o8s = simulate_stop_rule(path, t_in, e, st, vah, hold, vsp.RULE_B, fees=vsp.F15)
                r["H8"] = o8.r_net if o8 else None
                r["H8_15"] = o8s.r_net if o8s else None
                r["H8x"] = round((vah - e) / (tg - e), 4)
                tp = e + m3.unit_draw(k, "h8") * (tg - e)
                op = simulate_stop_rule(path, t_in, e, st, tp, hold, vsp.RULE_B, fees=vsp.F1) if e < tp < tg else None
                r["H8p"] = op.r_net if op else None
            # H1 / H3 and the depth control basket, on the oriented signal-timeframe bars (bar r closed at t_in)
            r["k_out"] = int(ev.r - ev.s)
            r["depth"] = round(mo.depth_atr(float(ev.lr), float(ev.ld), a), 4)
            c1, c2, c3 = mo.strong_return(float(bb.o[i]), float(bb.h[i]), float(bb.low[i]), float(bb.c[i]), float(ev.lr), a)
            r["H3c"] = [bool(c1), bool(c2), bool(c3)]
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
        for j, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            n += r["n"]
            if j % 25 == 0 or j == len(jobs):
                el = time.time() - t0
                emit(
                    f"  [run] {j}/{len(jobs)} {el:.0f}s eta {el / j * (len(jobs) - j):.0f}s; rows {n}; worker rss {r['rss']:.0f} MB; main {vsp.rss_mb():.0f} MB"
                )
    emit(f"[run] done in {time.time() - t0:.0f}s; rows {n}")


# ---------------------------------------------------------------- samples (the passport's)


def samples(a: argparse.Namespace) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    T = vsp.load_rows(a.passport_cache / "S16")
    period = T["t_in"] >= vsp.PASSPORT_MS
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn1 = np.nan_to_num(T["turn"], nan=-1) >= 1e6
    elig = vsp.s1.eligible_syms(a.data_dir)
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig)
    in_top = vsp.in_prev_day_top(T, vsp.top_n_days(qv, vsp.WIDE_TOP_N))
    S = {"V": vsp.chain(T, period & demo & turn1 & in_top), "P": vsp.chain(T, period & demo & turn1)}
    T["key"] = np.array(
        [key_of(str(s), str(tf), str(sd), int(t)) for s, tf, sd, t in zip(T["sym"], T["tf"], T["side"], T["t_in"], strict=True)]
    )
    return T, S


# ---------------------------------------------------------------- report


def label(name: str, ok: bool, r: dict[str, float]) -> str:
    if ok:
        return "✓ пройшов"
    se = (r["hi"] - r["lo"]) / 3.92
    mde = Z_MDE * se
    eff = INTEREST[name]
    if r["hi"] < eff:
        return f"✗ не підтверджено, корисний приріст (≥ {eff:g}R) виключено (MDE₈₀ {mde:.3f})"
    if mde > eff:
        return f"◯ невимірно (MDE₈₀ {mde:.3f} > {eff:g}R)"
    return f"✗ не підтверджено (MDE₈₀ {mde:.3f})"


def cells_of(T: dict[str, np.ndarray], m: np.ndarray, val: Any) -> list[tuple[str, float]]:
    out = []
    for lab, c in (
        ("2025", T["year"] == 2025),
        ("2026", T["year"] == 2026),
        ("long", T["side"] == "long"),
        ("short", T["side"] == "short"),
        ("4h", T["tf"] == "4h"),
        ("1d", T["tf"] == "1d"),
    ):
        out.append((lab, val(m & c)))
    return out


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T, S = samples(a)
    mod: dict[str, dict[str, Any]] = {}
    for p in sorted((out / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            mod[r["k"]] = r
    n = T["B"].size
    V, P = S["V"], S["P"]
    miss = sum(1 for k in T["key"][V | P] if k not in mod)

    def col(name: str) -> np.ndarray:
        return np.array([math.nan if (x := mod.get(k, {}).get(name)) is None else float(x) for k in T["key"]])

    B_mine = col("B")
    agree = (V | P) & np.isfinite(B_mine)
    emit(
        f"# S16 modules: passport candidates {n}; V ∪ P {int((V | P).sum())}, without a module row {miss}; base R equal to the passport on {int(np.sum(np.abs(B_mine[agree] - T['B'][agree]) < 1e-9))} of {int(agree.sum())}"
    )
    IS, OOS = (T["t_in"] >= vsp.PASSPORT_MS) & (T["t_in"] < vsp.OOS_MS), T["t_in"] >= vsp.OOS_MS
    emit(
        f"V: IS {int((V & IS).sum())}, OOS {int((V & OOS).sum())} (2025 {int((V & OOS & (T['year'] == 2025)).sum())}, 2026 {int((V & OOS & (T['year'] == 2026)).sum())}); P: OOS {int((P & OOS).sum())}"
    )
    emit(
        f"база OOS: V {fmtd(vsp.ci(T, V & OOS))} (тижнів {np.unique(T['week'][V & OOS]).size}); P {fmtd(vsp.ci(T, P & OOS))}; стрес ×1.5 V {np.nanmean(T['B15'][V & OOS]):+.3f}"
    )
    yrs = v3.years_oos()
    res: dict[str, dict[str, Any]] = {}
    keys = T["key"]
    u_sel = np.array([m3.unit_draw(str(k), "h7sel") for k in keys])

    # --- paired exit modules H6, H7, H8
    acts = {"H6": np.ones(n, dtype=bool), "H7": col("H7f") == 1.0, "H8": col("H8a") == 1.0}
    alive7 = col("H7a") == 1.0
    f7 = float(np.mean(acts["H7"][V & OOS & alive7])) if np.any(V & OOS & alive7) else 0.0
    plc = {
        "H6": col("H6p"),
        "H7": np.where(alive7 & (u_sel < f7), col("H7p"), T["B"]),
        "H8": np.where(acts["H8"], col("H8p"), T["B"]),
    }
    emit(f"H7: відкритих через N свічок (V OOS) {np.mean(alive7[V & OOS]):.3f}; серед них спрацювань {f7:.3f} (частка плацебо)")
    D: dict[str, np.ndarray] = {}
    for name in ("H6", "H7", "H8"):
        R, R15, Rp = col(name), col(name + "_15"), plc[name]
        d = R - T["B"]
        D[name] = d
        emit(f"\n## {name}")
        r = v3.paired(T, d, V & OOS)
        rp = v3.paired(T, R - Rp, V & OOS)
        act = acts[name]
        ra = v3.paired(T, d, V & OOS & act) if name != "H6" else r
        emit(
            f"OOS V: n {int(r['n'])}, Δ {fmtd(r)} p₁ {r['p1']:.4f}; проти плацебо {fmtd(rp)} p₁ {rp['p1']:.4f}; плацебо − база {np.nanmean((Rp - T['B'])[V & OOS]):+.3f}"
        )
        emit(f"  діє на {np.mean(act[V & OOS]):.0%} угод; Δ на них {fmtd(ra)} (n {int(ra['n'])})")
        mk = V & OOS & np.isfinite(R)
        emit(
            f"  E[R] OOS V: база {np.mean(T['B'][mk]):+.3f}, модуль {np.mean(R[mk]):+.3f}; ×1.5: база {np.nanmean(T['B15'][mk]):+.3f}, модуль {np.nanmean(R15[mk]):+.3f}"
        )
        emit(
            f"  сума R / рік OOS V: база {np.sum(T['B'][mk]) / yrs:+.1f}, модуль {np.sum(R[mk]) / yrs:+.1f}; угод / рік {mk.sum() / yrs:.0f}"
        )
        ri, rP, r21 = v3.paired(T, d, V & IS), v3.paired(T, d, P & OOS), v3.paired(T, d, V)
        emit(f"  IS V Δ {fmtd(ri)}; 2021+ V Δ {fmtd(r21)}; OOS P Δ {fmtd(rP)} (n {int(rP['n'])})")
        cells = cells_of(T, V & OOS, lambda mm, d=d: float(np.nanmean(d[mm])) if mm.any() else math.nan)
        mt, top = v3.drop_top(T, d, V & OOS)
        cells.append(("без топ-5 монет", float(np.nanmean(d[mt]))))
        emit("  розбивки OOS V (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
        v3.per_asset(T, d, V & IS, V & OOS, name)
        res[name] = {"r": r, "rp": rp, "cells": cells, "rP": rP, "plc_p": rp["p1"], "E15": float(np.nanmean(R15[mk]))}
    x8 = col("H8x")[V & OOS & acts["H8"]]
    emit(
        f"\nH8: ціль VAH у частках шляху до протилежної межі (V OOS, де діє): медіана {np.nanmedian(x8):.2f}, квартилі {np.nanquantile(x8, 0.25):.2f} / {np.nanquantile(x8, 0.75):.2f}"
    )
    m = V & OOS
    dd = np.vstack([D[nm][m] for nm in ("H6", "H7", "H8")])
    cc = np.corrcoef(dd[:, np.all(np.isfinite(dd), axis=0)])
    emit(
        f"перекриття виходів (V OOS): кореляції Δ H6·H7 {cc[0, 1]:+.2f}, H6·H8 {cc[0, 2]:+.2f}, H7·H8 {cc[1, 2]:+.2f}; діють: H6 100 %, H7 {np.mean(acts['H7'][m]):.0%}, H8 {np.mean(acts['H8'][m]):.0%}, H7 і H8 разом {np.mean((acts['H7'] & acts['H8'])[m]):.0%}"
    )

    # --- subgroup modules H1, H3
    kout = col("k_out")
    med = float(np.median(kout[V & IS & np.isfinite(kout)]))
    emit(f"\nH1: медіана k на V IS = {med:g} → ознака «швидка девіація» k ≤ {int(med)}")
    h3c = np.array([mod.get(k, {}).get("H3c", [False, False, False]) for k in keys], dtype=bool)
    has = np.array([k in mod for k in keys])
    flags = {"H1": (kout <= med, np.isfinite(kout)), "H3": (h3c.all(axis=1) & has, has)}
    for name, (flag, defined) in flags.items():
        m = V & OOS & defined
        r = v3.sub_delta(T, flag, m)
        obs, pl = m3.subgroup_placebo(T["B"][m], flag[m], v3.N_PLACEBO_SUB, vsp.SEED)
        pp = float((np.sum(pl >= obs) + 1) / (pl.size + 1)) if pl.size else math.nan
        emit(f"\n## {name}")
        emit(
            f"OOS V (визначено {int(m.sum())}): з ознакою n {int(r['n'])} ({r['n'] / max(1, m.sum()):.0%}), E[R] {np.mean(T['B'][m & flag]):+.3f} проти {np.mean(T['B'][m]):+.3f} усіх; Δ {fmtd(r)} p₁ {r['p1']:.4f}; плацебо p {pp:.4f}"
        )
        ri, rP, r21 = v3.sub_delta(T, flag, V & IS & defined), v3.sub_delta(T, flag, P & OOS & defined), v3.sub_delta(T, flag, V & defined)
        emit(f"  IS V Δ {fmtd(ri)}; 2021+ V Δ {fmtd(r21)} (n з ознакою {int(r21['n'])}); OOS P Δ {fmtd(rP)}")
        cells = cells_of(
            T, m, lambda mm, flag=flag: float(np.mean(T["B"][mm & flag]) - np.mean(T["B"][mm])) if np.any(mm & flag) else math.nan
        )
        contrib = np.where(flag, T["B"] - np.mean(T["B"][m]), 0.0)
        mt, top = v3.drop_top(T, contrib, m)
        cells.append(("без топ-5 монет", float(np.mean(T["B"][mt & flag]) - np.mean(T["B"][mt]))))
        emit("  розбивки OOS V (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
        e15 = float(np.nanmean(T["B15"][m & flag]))
        emit(
            f"  угод з ознакою / рік OOS {np.sum(m & flag) / yrs:.0f}; сума R з ознакою / рік {np.sum(T['B'][m & flag]) / yrs:+.1f}; E[R] з ознакою ×1.5 {e15:+.3f}"
        )
        dflag = np.where(flag, T["B"], math.nan)
        v3.per_asset(T, dflag - np.nanmean(T["B"][m]), V & IS & flag, V & OOS & flag, name + " (R угод з ознакою)")
        res[name] = {"r": r, "cells": cells, "rP": rP, "plc_p": pp, "E15": e15, "flag": flag, "defined": defined}

    # --- verdict: Holm over the 5 registered modules
    p1 = [res[mn]["r"]["p1"] for mn in MODULES]
    adj = holm([x if np.isfinite(x) else 1.0 for x in p1])
    emit("\n## Вердикт (Холм на 5)")
    passed = []
    for mn, p, pa in zip(MODULES, p1, adj, strict=True):
        x = res[mn]
        robust = all(np.isfinite(v) and v > 0 for c, v in x["cells"] if c in ("2025", "2026", "long", "short", "без топ-5 монет"))
        ok = (x["r"]["m"] > 0) and pa < 0.05 and x["plc_p"] < 0.05 and robust and x["rP"]["m"] > 0
        idea = "торгова ідея" if x["E15"] > 0 else "менший збиток"
        emit(
            f"{mn}: Δ {fmtd(x['r'])}, p₁ {p:.4f}, Холм {pa:.4f}, плацебо p {x['plc_p']:.4f}, стійкість {'так' if robust else 'ні'}, P Δ {x['rP']['m']:+.3f}, E[R]×1.5 {x['E15']:+.3f} → {label(mn, ok, x['r'])}{' (' + idea + ')' if ok else ''}"
        )
        if ok:
            passed.append(mn)
    emit(f"пройшли: {passed or 'жоден'}")
    emit("комбінація: потрібна окрема стадія (пройшло ≥ 2)" if len(passed) >= 2 else "комбінації немає (пройшло менше двох модулів)")

    # --- registered descriptive rows
    emit("\n## Описово (зареєстровано)")
    f1, f3 = flags["H1"][0], flags["H3"][0]
    for per, pm in (("OOS", V & OOS), ("2021+", V)):
        emit(
            f"H1 × H3 {per}: "
            + "; ".join(
                f"{lab} n {int(np.sum(pm & c))} E[R] {np.mean(T['B'][pm & c]) if np.any(pm & c) else math.nan:+.3f}"
                for lab, c in (("обидві", f1 & f3), ("лише H1", f1 & ~f3), ("лише H3", ~f1 & f3), ("жодної", ~f1 & ~f3))
            )
        )
        emit(
            f"k {per}: "
            + "; ".join(
                f"{k} n {int(np.sum(pm & (kout == k)))} {np.mean(T['B'][pm & (kout == k)]) if np.any(pm & (kout == k)) else math.nan:+.3f}"
                for k in range(3, 11)
            )
        )
        emit(
            f"H3 умови {per}: "
            + "; ".join(
                f"{lab} {np.mean(h3c[pm, j]):.0%} E[R] {np.mean(T['B'][pm & h3c[:, j]]):+.3f}"
                for j, lab in enumerate(("тіло ≥ 0.5 ATR", "верхня третина", "≥ край + 0.3 ATR"))
            )
        )
    dep = col("depth")
    ed = np.nanquantile(dep[V & IS], [1 / 3, 2 / 3])
    tq = np.searchsorted(ed, dep)
    emit(
        f"глибина (контроль H2), межі терцилів IS {ed[0]:.2f} / {ed[1]:.2f} ATR: "
        + "; ".join(f"T{j + 1} OOS n {int(np.sum(V & OOS & (tq == j)))} {np.mean(T['B'][V & OOS & (tq == j)]):+.3f}" for j in range(3))
    )
    emit(
        f"частки ознак OOS V: H1 {np.mean(f1[V & OOS]):.0%}, H3 {np.mean(f3[V & OOS]):.0%}; tf 1d OOS n {int(np.sum(V & OOS & (T['tf'] == '1d')))}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s16_modules")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
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
        emit(f"verify_s16_modules --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"run": stage_run, "report": stage_report}[a.stage](a, out)
        emit(f"peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
