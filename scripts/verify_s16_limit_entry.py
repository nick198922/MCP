#!/usr/bin/env python3
"""S16 entry by a limit on the pullback to the broken range edge (IDEA-20261030-demo-setups-depth, audit 14.11 item
«S16, вхід лімітом на відкаті до зламаної межі»). Registration: the card's section «Попередня реєстрація — S16, вхід
лімітом на відкаті» (written and committed before any full run).

    uv run python scripts/verify_s16_limit_entry.py --stage run      # per-symbol rows (no network, 4 workers)
    uv run python scripts/verify_s16_limit_entry.py --stage gate     # MDE80 of the per-signal delta; no means
    uv run python scripts/verify_s16_limit_entry.py --stage report   # only if the gate passed

Base trades, samples and the occupancy chain come from the S16 passport's candidate files
(``verify_setup_passport.py S16 --stage run``); rows join them by (symbol, timeframe, side, entry time) and the script
checks that its own base R equals the passport's.
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

import verify_s3_modules as v3  # noqa: E402  (paired statistics, 5a, drop-top)
import verify_setup_passport as vsp  # noqa: E402  (data loaders, samples, chain, CI helpers of the passport)

from tradedesk.analytics import s3_modules as m3  # noqa: E402
from tradedesk.analytics import s16_limit_entry as le  # noqa: E402
from tradedesk.analytics.course_basics import range_events  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import simulate_stop_rule  # noqa: E402
from tradedesk.execution.orders import reward_risk_at  # noqa: E402  (read-only: the guard's R:R after fees)

ALPHA = 0.05 / 2  # one-sided, strictest Holm step of the 2 registered tests
GATE_MDE = 0.10  # R per signal (registered)
INTEREST = 0.10  # registered effect of interest, R per signal
N_DRAWS = 20  # placebo draws per signal
emit = vsp.emit
key_of = v3.key_of
fmtd = v3.fmtd


# ---------------------------------------------------------------- per-symbol rows


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir = args
    t0 = time.time()
    out = Path(out_dir)
    det, tfs, _ = vsp.SETUPS["S16"]
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    rows: list[dict[str, Any]] = []
    for tf in tfs:
        tb = resample(h1, vsp.HOURS[tf])
        bar_ms = tb.bar_ms
        evs: dict[str, dict[int, Any]] = {}
        for sd, bb in (("long", tb), ("short", tb.mirrored())):
            pos = segment_pos(bb.t)
            a_arr = atr(bb, 14, pos)
            evs[sd] = {
                ev.r: ev for ev in range_events(bb, pos, a_arr, w=vsp.ss.RANGE_W, width_atr=vsp.ss.RANGE_WIDTH_ATR) if ev.kind == "dev"
            }
        oriented = {"long": tb, "short": tb.mirrored()}
        for sig in det(tb, tf):
            t_in = sig.signal_ts + bar_ms
            if not (vsp.START_MS <= t_in < vsp.END[tf]):
                continue
            sg = 1.0 if sig.side == "long" else -1.0
            e, st, tg = sg * sig.entry, sg * sig.stop, sg * sig.target
            if not (st < e < tg):
                continue
            i = int(np.searchsorted(oriented[sig.side].t, sig.signal_ts))
            lr = float(evs[sig.side][i].lr)
            path = h1 if sig.side == "long" else h1.mirrored()
            hold = vsp.HOLD[tf]
            base = simulate_stop_rule(path, t_in, e, st, tg, hold, vsp.RULE_B, fees=vsp.F1)
            if base is None:
                continue
            L, tk = le.retest_limit(path, t_in, bar_ms, lr, st, tg, hold, vsp.RULE_B, vsp.F1)
            L15, _ = le.retest_limit(path, t_in, bar_ms, lr, st, tg, hold, vsp.RULE_B, vsp.F15)
            n_h = le.WINDOW_BARS * bar_ms // HOUR_MS
            pd = [le.delayed_entry(path, t_in, d, st, tg, hold, vsp.RULE_B, vsp.F1) for d in range(n_h)]
            lr_px = sg * lr
            rows.append(
                {
                    "k": key_of(s, tf, sig.side, t_in),
                    "B": base.r_net,
                    "lr_eq_e": bool(lr >= e),
                    "geo": round((e - lr) / (e - st), 4),  # limit distance below the base entry, in base R
                    "fill": tk >= 0,
                    "fh": int((tk - t_in) // HOUR_MS) if tk >= 0 else -1,
                    "L": L,
                    "L15": L15,
                    "Pd": [None if x is None else round(x, 6) for x in pd],
                    "rr_l": reward_risk_at(sig.side, lr_px, sig.stop, sig.target),
                    "sp_l": abs(lr_px - sig.stop) / abs(lr_px),
                }
            )
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


# ---------------------------------------------------------------- samples (the passport's) and columns


def load(a: argparse.Namespace, out: Path) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    T = vsp.load_rows(a.passport_cache / "S16")
    period = T["t_in"] >= vsp.PASSPORT_MS
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn1 = np.nan_to_num(T["turn"], nan=-1) >= 1e6
    elig = vsp.s1.eligible_syms(a.data_dir)
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig)
    in_top = vsp.in_prev_day_top(T, vsp.top_n_days(qv, vsp.WIDE_TOP_N))
    T["key"] = np.array(
        [key_of(str(s), str(tf), str(sd), int(t)) for s, tf, sd, t in zip(T["sym"], T["tf"], T["side"], T["t_in"], strict=True)]
    )
    mod: dict[str, dict[str, Any]] = {}
    for p in sorted((out / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            mod[r["k"]] = r
    T["_has"] = np.array([k in mod for k in T["key"]])

    def col(name: str) -> np.ndarray:
        return np.array([math.nan if (x := mod.get(k, {}).get(name)) is None else float(x) for k in T["key"]])

    for c in ("B", "L", "L15", "fh", "geo", "rr_l", "sp_l"):
        T["m_" + c] = col(c)
    T["fill"] = np.array([bool(mod.get(k, {}).get("fill", False)) for k in T["key"]])
    T["lr_eq_e"] = np.array([bool(mod.get(k, {}).get("lr_eq_e", False)) for k in T["key"]])
    T["_pd"] = np.array([mod.get(k, {}).get("Pd") for k in T["key"]], dtype=object)
    # per-signal R of the limit: unfilled = 0; filled without a path = NaN (left out)
    # (signals without a module row are NaN, never 0)
    T["LS"] = np.where(T["_has"], np.where(T["fill"], T["m_L"], 0.0), math.nan)
    T["LS15"] = np.where(T["_has"], np.where(T["fill"], T["m_L15"], 0.0), math.nan)
    T["D"] = T["LS"] - T["B"]
    score_l = np.array(
        [
            le.rescored(int(sc), float(r0), float(r1)) if np.isfinite(r1) else -9
            for sc, r0, r1 in zip(T["score"], T["rr"], T["m_rr_l"], strict=True)
        ]
    )
    demo_l = np.array(
        [
            le.demo_eligible(float(r1), float(s1_), int(sc), vsp.MIN_RR, vsp.MAX_STOP, vsp.MIN_SCORE) if np.isfinite(r1) else False
            for r1, s1_, sc in zip(T["m_rr_l"], T["m_sp_l"], score_l, strict=True)
        ]
    )
    IS, OOS = (T["t_in"] >= vsp.PASSPORT_MS) & (T["t_in"] < vsp.OOS_MS), T["t_in"] >= vsp.OOS_MS
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    in_top100 = vsp.in_prev_day_top(T, vsp.top_n_days(qv, vsp.TOP_N))
    P = vsp.chain(T, period & demo & turn1)
    M = {
        "period": period,
        "demo": demo,
        "pool": period & turn1 & in_top,
        "V": vsp.chain(T, period & demo & turn1 & in_top),
        "P": P,
        # Second registration (2026-11-19, «S16 ліміт — ширша V»): the verdict sample W is the side sample P of the
        # first registration (>= $1M previous-day turnover, no top-250 rank), with its own occupancy chain.
        "W": P,
        "top250": in_top,
        "demo_list": turn5 & in_top100,  # the demo's own universe: top-100 by turnover and >= $5M
        "demo_l": demo_l,
        "IS": IS,
        "OOS": OOS,
    }
    return T, M


def gate_numbers(T: dict[str, np.ndarray], M: dict[str, np.ndarray], key: str = "V") -> dict[str, Any]:
    """n, clusters, fill share, SE and MDE80 of the per-signal delta on the OOS part of sample ``key``.
    Reads no mean of any R."""
    m = M[key] & M["OOS"]
    ok = m & np.isfinite(T["D"])
    se = le.cluster_se(T["D"][ok], T["week"][ok])
    mde = le.mde80(se, ALPHA)
    return {
        "n": int(ok.sum()),
        "n_sel": int(m.sum()),
        "weeks": int(np.unique(T["week"][ok]).size),
        "fill": float(np.mean(T["fill"][ok])) if ok.any() else math.nan,
        "se": se,
        "mde80": mde,
        "pass": bool(np.isfinite(mde) and mde <= GATE_MDE),
        "sample": key,
    }


def suffix(key: str) -> str:
    """File suffix of a verdict sample: V keeps the first registration's files (gate.json), W writes gate_W.json."""
    return "" if key == "V" else f"_{key}"


def eligibility(T: dict[str, np.ndarray], M: dict[str, np.ndarray]) -> None:
    """Sample pace (counts only): base-rejected signals of the demo pool that the limit price makes eligible."""
    rej = M["pool"] & ~M["demo"] & T["_has"]
    new = rej & M["demo_l"]
    lost = M["pool"] & M["demo"] & T["_has"] & ~M["demo_l"]
    why_rr = T["rr"] < vsp.MIN_RR
    why_st = T["stop_pct"] > vsp.MAX_STOP
    for lab, per in (("IS", M["IS"]), ("OOS", M["OOS"])):
        emit(
            f"придатність {lab}: пул {int((M['pool'] & per & T['_has']).sum())}, відхилено базою {int((rej & per).sum())} "
            f"(R:R < 1.5 {int((rej & per & why_rr).sum())}, стоп > 10 % {int((rej & per & why_st).sum())}); стають придатними з ліміткою {int((new & per).sum())}, "
            f"з них заповнено б {int((new & per & T['fill']).sum())}; придатні базою, але не ліміткою {int((lost & per).sum())}"
        )
    yrs = v3.years_oos()
    emit(
        f"темп OOS: сигналів V / рік {int((M['V'] & M['OOS']).sum()) / yrs:.0f}; нових придатних / рік {int((new & M['OOS']).sum()) / yrs:.0f}, заповнених з них / рік {int((new & M['OOS'] & T['fill']).sum()) / yrs:.0f}"
    )


def stage_gate(a: argparse.Namespace, out: Path) -> None:
    T, M = load(a, out)
    m = (M["V"] | M["P"]) & T["_has"]
    agree = m & np.isfinite(T["m_B"])
    emit(
        f"# S16 limit entry gate: passport candidates {T['B'].size}; V ∪ P {int((M['V'] | M['P']).sum())}, without a row {int(((M['V'] | M['P']) & ~T['_has']).sum())}; base R equal to the passport on {int(np.sum(np.abs(T['m_B'][agree] - T['B'][agree]) < 1e-9))} of {int(agree.sum())}"
    )
    key = a.sample
    so = M[key] & M["OOS"]
    mo = so & T["_has"]
    emit(
        f"геометрія OOS {key}: лімітка = входу бази {np.mean(T['lr_eq_e'][mo]):.1%}; відстань входу бази до межі, частки R бази: медіана {np.nanmedian(T['m_geo'][mo]):.2f}, квартилі {np.nanquantile(T['m_geo'][mo], 0.25):.2f} / {np.nanquantile(T['m_geo'][mo], 0.75):.2f}"
    )
    if key != "V":
        # composition of the wider sample (counts only): how much of it the demo could trade at all
        emit(
            f"склад OOS {key}: відібрано {int(so.sum())}, монет {np.unique(T['sym'][so]).size}; у топ-250 {int((so & M['top250']).sum())}, "
            f"поза топ-250 {int((so & ~M['top250']).sum())}; у списку демо (топ-100 і ≥ $5M) {int((so & M['demo_list']).sum())}; "
            f"для порівняння OOS V відібрано {int((M['V'] & M['OOS']).sum())}"
        )
    g = gate_numbers(T, M, key)
    emit(
        f"ВОРОТА (OOS {key}): n {g['n']} (відібрано {g['n_sel']}), тижнів {g['weeks']}, заповнено {g['fill']:.1%}; SE(Δ) кластерна {g['se']:.4f}R; MDE₈₀ = {le.mde80(1.0, ALPHA):.3f}·SE = {g['mde80']:.3f}R → {'пройдено' if g['pass'] else 'НЕ пройдено'} (поріг {GATE_MDE:g}R)"
    )
    eligibility(T, M)
    (out / f"gate{suffix(key)}.json").write_text(json.dumps(g))


# ---------------------------------------------------------------- report (only after a passed gate)


def placebo_col(T: dict[str, np.ndarray], M: dict[str, np.ndarray], key: str = "V") -> np.ndarray:
    """Placebo R per signal: fill frequency and delay pool from the limit on the OOS part of sample ``key``, per
    timeframe."""
    out = np.full(T["B"].size, math.nan)
    base = M[key] & M["OOS"] & T["_has"]
    for tf in ("4h", "1d"):
        mt = base & (T["tf"] == tf)
        f = float(np.mean(T["fill"][mt])) if mt.any() else 0.0
        pool = T["m_fh"][mt & T["fill"]].astype(int).tolist()
        emit(f"плацебо {tf}: частка заповнення {f:.3f}, годин заповнення в пулі {len(pool)}")
        for i in np.flatnonzero((T["tf"] == tf) & T["_has"]):
            k = str(T["key"][i])
            us = [m3.unit_draw(k, f"s16lim_sel{j}") for j in range(N_DRAWS)]
            ud = [m3.unit_draw(k, f"s16lim_d{j}") for j in range(N_DRAWS)]
            out[i] = le.placebo_signal_r(T["_pd"][i] or [], us, ud, f, pool)
    return out


def stage_report(a: argparse.Namespace, out: Path) -> None:
    key = a.sample
    gp = out / f"gate{suffix(key)}.json"
    if not gp.exists() or not json.loads(gp.read_text()).get("pass"):
        emit(f"ворота ({key}) не пройдено або не рахувалися: середні не рахуються (реєстрація)")
        return
    T, M = load(a, out)
    S, P, IS, OOS = M[key], M["P"], M["IS"], M["OOS"]
    D = T["D"]
    PL = placebo_col(T, M, key)
    m = S & OOS
    r = v3.paired(T, D, m)
    rp = v3.paired(T, T["LS"] - PL, m)
    adj = holm([r["p1"] if np.isfinite(r["p1"]) else 1.0, rp["p1"] if np.isfinite(rp["p1"]) else 1.0])
    emit(
        f"OOS {key}: n {int(r['n'])}, Δ ліміт − база {fmtd(r)} p₁ {r['p1']:.4f} (Холм {adj[0]:.4f}); ліміт − плацебо {fmtd(rp)} p₁ {rp['p1']:.4f} (Холм {adj[1]:.4f})"
    )
    ok = m & np.isfinite(D)
    emit(
        f"  E[R] на сигнал OOS {key}: база {np.mean(T['B'][ok]):+.3f}, ліміт {np.mean(T['LS'][ok]):+.3f}, плацебо {np.nanmean(PL[ok]):+.3f}; ×1.5: база {np.nanmean(T['B15'][ok]):+.3f}, ліміт {np.nanmean(T['LS15'][ok]):+.3f}"
    )
    fl = ok & T["fill"]
    emit(
        f"  розклад: заповнено {np.mean(T['fill'][ok]):.1%}; E[R бази | заповнено] {np.mean(T['B'][fl]):+.3f}, E[R бази | не заповнено] {np.mean(T['B'][ok & ~T['fill']]):+.3f}; E[R ліміту | заповнено] {np.mean(T['LS'][fl]):+.3f}"
    )
    yrs = v3.years_oos()
    emit(
        f"  угод / рік: база {ok.sum() / yrs:.0f}, ліміт {fl.sum() / yrs:.0f}; сума R / рік: база {np.sum(T['B'][ok]) / yrs:+.1f}, ліміт {np.sum(T['LS'][ok]) / yrs:+.1f}"
    )
    ri, r21 = v3.paired(T, D, S & IS), v3.paired(T, D, S)
    if key == "V":
        # first registration, condition (4): delta > 0 on OOS P
        rP = v3.paired(T, D, P & OOS)
        emit(f"  IS V Δ {fmtd(ri)}; 2021+ V Δ {fmtd(r21)}; OOS P Δ {fmtd(rP)} (n {int(rP['n'])})")
        cond4 = bool(rP["m"] > 0)
    else:
        # second registration, condition (4): delta > 0 on both disjoint liquidity parts of OOS W
        rin, rout = v3.paired(T, D, m & M["top250"]), v3.paired(T, D, m & ~M["top250"])
        rdl, rv = v3.paired(T, D, m & M["demo_list"]), v3.paired(T, D, M["V"] & OOS)
        emit(f"  IS {key} Δ {fmtd(ri)}; 2021+ {key} Δ {fmtd(r21)}")
        emit(f"  умова (4): OOS {key} у топ-250 Δ {fmtd(rin)} (n {int(rin['n'])}); поза топ-250 Δ {fmtd(rout)} (n {int(rout['n'])})")
        emit(
            f"  описово: OOS {key} у списку демо (топ-100, ≥ $5M) Δ {fmtd(rdl)} (n {int(rdl['n'])}); OOS V першої реєстрації Δ {fmtd(rv)} (n {int(rv['n'])})"
        )
        cond4 = bool(rin["m"] > 0 and rout["m"] > 0)
    cells = []
    for lab, c in (
        ("2025", T["year"] == 2025),
        ("2026", T["year"] == 2026),
        ("long", T["side"] == "long"),
        ("short", T["side"] == "short"),
        ("4h", T["tf"] == "4h"),
        ("1d", T["tf"] == "1d"),
    ):
        mm = ok & c
        cells.append((lab, float(np.mean(D[mm])) if mm.any() else math.nan))
    mt, top = v3.drop_top(T, D, ok)
    cells.append(("без топ-5 монет", float(np.nanmean(D[mt]))))
    emit(f"  розбивки OOS {key} (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
    v3.per_asset(T, D, S & IS, S & OOS, f"S16 ліміт {key}")
    robust = all(np.isfinite(v) and v > 0 for c, v in cells if c in ("2025", "2026", "long", "short", "без топ-5 монет"))
    passed = r["m"] > 0 and adj[0] < 0.05 and adj[1] < 0.05 and robust and cond4
    if passed:
        verdict = "✓ пройшов" + (" (торгова ідея)" if np.mean(T["LS"][ok]) > 0 else " (менший збиток)")
    elif r["m"] > 0 and r["p1"] < 0.05:
        verdict = "≈ Δ > 0 і сирий p₁ < 0.05, але не пройшов решту умов"
    elif r["m"] <= 0 or r["hi"] < INTEREST:
        verdict = f"✗ не підтверджено (Δ ≤ 0 або верхня межа CI < {INTEREST:g}R)"
    else:
        verdict = "◯ незначуще, CI сумісний і з 0, і з 0.10R"
    emit(
        f"\nВЕРДИКТ ({key}): {verdict}; стійкість {'так' if robust else 'ні'}; умова (4) {'так' if cond4 else 'ні'}; SE бутстрепу {r.get('se', math.nan):.4f}, MDE₈₀ {le.mde80(r.get('se', math.nan), ALPHA):.3f}"
    )
    new = M["pool"] & ~M["demo"] & T["_has"] & M["demo_l"] & OOS
    emit(
        f"описово, нові придатні OOS (без ланцюга): n {int(new.sum())}, E[R ліміту на сигнал] {np.nanmean(T['LS'][new]) if new.any() else math.nan:+.3f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("run", "gate", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s16_limit_entry")
    ap.add_argument("--out", type=Path, default=None, help="text output of the stage (default <cache-dir>/<stage>.txt)")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument(
        "--sample",
        choices=("V", "W"),
        default="V",
        help="verdict sample: V = first registration (top-250, >= $1M); W = second registration (>= $1M, no rank)",
    )
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    sfx = "" if a.stage == "run" else suffix(a.sample)
    with (a.out or out / f"{a.stage}{sfx}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"verify_s16_limit_entry --stage {a.stage} --sample {a.sample} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"run": stage_run, "gate": stage_gate, "report": stage_report}[a.stage](a, out)
        emit(f"peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
