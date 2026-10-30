#!/usr/bin/env python3
"""S14 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b): H10 half out at the base target with the rest held until
a close under EMA21 of the signal timeframe, H7 impulse high breaking a 90-bar channel, H1 the swept fractal being the
lowest low of the whole pullback, H2 the sweep zone holding a higher-timeframe level, each measured against the S14 base
of the passport on the same trades. Registration: the card's section «Попередня реєстрація модулів S14» (written and
staged before any run).

    uv run python scripts/verify_s14_modules.py --stage run      # per-symbol module rows (no network, 4 workers)
    uv run python scripts/verify_s14_modules.py --stage report   # tables (also <cache>/report.txt)

The base trades, the demo selection, the occupancy chain and the samples come from the S14 passport's candidate files
(``verify_setup_passport.py S14 --stage run``); this script joins its module rows to them by (symbol, timeframe, side,
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
from tradedesk.analytics import s14_modules as mo  # noqa: E402
from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, ema, find_signals, indicators, resample  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.setup_signals import S14_PARAMS  # noqa: E402
from tradedesk.analytics.stop_management import simulate_stop_rule  # noqa: E402

MODULES = ("H10", "H7", "H1", "H2")
CHANNEL = {"4h": 90, "1d": 60}  # H7 window in signal bars (registered)
INTEREST = {"H10": 0.05, "H7": 0.15, "H1": 0.15, "H2": 0.15}  # registered effect of interest
Z_MDE = 2.241 + 0.842  # one-sided alpha 0.0125 (strictest Holm step of 4) and 80 % power
emit = vsp.emit
key_of = v3.key_of
fmtd = v3.fmtd


# ---------------------------------------------------------------- per-symbol module rows


def _levels(e100: np.ndarray, e200: np.ndarray, j: int, path: Any, ref_ms: int) -> tuple[list[float], list[float]]:
    """(EMA100 / EMA200 of signal bar j, PDL / PWL of the periods closed before ``ref_ms``)."""
    emas = [float(e100[j]), float(e200[j])] if j >= 0 else [math.nan, math.nan]
    lv = m3.htf_lows(path.t, path.low, ref_ms)
    return emas, [lv["day"], lv["week"]]


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir = args
    t0 = time.time()
    out = Path(out_dir)
    det, tfs, _ = vsp.SETUPS["S14"]
    h1, _qv = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    paths = {"long": h1, "short": h1.mirrored()}
    rows: list[dict[str, Any]] = []
    bad = 0
    for tf in tfs:
        tb = resample(h1, vsp.HOURS[tf])
        bar_ms = tb.bar_ms
        ctx: dict[str, Any] = {}
        for sd, bb in (("long", tb), ("short", tb.mirrored())):
            ind = indicators(bb, S14_PARAMS)
            sigs = {x.idx: x for x in find_signals(bb, ind, S14_PARAMS, variant="sweep")}
            ctx[sd] = (bb, ind, sigs, ema(bb.c, 100, ind.pos), ema(bb.c, 200, ind.pos), bb.t + bar_ms)
        for sig in det(tb, tf):
            t_in = sig.signal_ts + bar_ms
            if not (vsp.START_MS <= t_in < vsp.END[tf]):
                continue
            sg = 1.0 if sig.side == "long" else -1.0
            e, st, tg = sg * sig.entry, sg * sig.stop, sg * sig.target
            if not (st < e < tg):
                continue
            bb, ind, sigs, e100, e200, close_ms = ctx[sig.side]
            i = int(np.searchsorted(bb.t, sig.signal_ts))
            x = sigs.get(i)
            if x is None or abs(x.entry - e) > 1e-9 * abs(e):
                bad += 1
                continue
            k = int(x.hi_idx)
            f = mo.swept_fractal(bb, ind, i, k)
            path = paths[sig.side]
            hold = vsp.HOLD[tf]
            base = simulate_stop_rule(path, t_in, e, st, tg, hold, vsp.RULE_B, fees=vsp.F1)
            if base is None:
                continue
            base15 = simulate_stop_rule(path, t_in, e, st, tg, hold, vsp.RULE_B, fees=vsp.F15)
            key = key_of(s, tf, sig.side, t_in)
            r: dict[str, Any] = {"k": key, "B": base.r_net, "tgt": base.exit_kind == "target", "f": f >= 0}
            # H10: half at the base target, the rest from the next 1h bar with a break-even stop until a close of the
            # signal timeframe under its EMA21 (causal: closes at or before each decision)
            r["H10"], r["H10_15"] = base.r_net, base15.r_net if base15 else None
            if r["tgt"]:
                rr, ex_ms, kind = mo.runner_after_target(path, t_in, e, st, hold, vsp.F1, base, close_ms, bb.c, ind.e_mid)
                r["H10"] = mo.half_and_runner(base.r_net, rr)
                r["H10d"] = int((ex_ms - base.exit_ms) // HOUR_MS)
                r["H10k"] = kind
                if base15 is not None and base15.exit_kind == "target":
                    r15 = mo.runner_after_target(path, t_in, e, st, hold, vsp.F15, base15, close_ms, bb.c, ind.e_mid)[0]
                    r["H10_15"] = mo.half_and_runner(base15.r_net, r15)
                r["H10c"] = [round(float(v), 5) for v in mo.runner_curve(path, t_in, e, st, hold, vsp.F1, base)]
            # H7: the impulse high breaks the channel of the bars before it
            r["H7"] = mo.channel_breakout(bb.h, ind.pos, k, CHANNEL[tf])
            if f < 0:
                rows.append(r)
                continue
            # H1: the swept fractal is the lowest low of bars k .. i - 1
            r["H1"] = mo.whole_pullback(bb.low, k, f, i)
            # H2: a higher-timeframe level inside the sweep zone (EMAs of bar i - 1, periods closed before bar i opens)
            a = float(ind.atr[i])
            zlo, zhi = mo.sweep_zone(float(bb.low[i]), float(bb.low[f]), a)
            emas, htf = _levels(e100, e200, i - 1, path, int(bb.t[i]))
            r["H2e"] = _rnd(mo.zone_distance(emas, zlo, zhi, a))
            r["H2h"] = _rnd(mo.zone_distance(htf, zlo, zhi, a))
            r["H2d"] = _rnd(mo.zone_distance(emas + htf, zlo, zhi, a))
            # H2 placebo: the same four levels as of t0 - L days (stale levels of the same kind)
            lag = 5 + int(m3.unit_draw(key, "h2lag") * 26)
            t_l = int(bb.t[i]) - lag * DAY_MS
            j = int(np.searchsorted(close_ms, t_l, side="right")) - 1
            pe, ph = _levels(e100, e200, j, path, t_l)
            r["H2pd"] = _rnd(mo.zone_distance(pe + ph, zlo, zhi, a))
            rows.append(r)
    (out / "rows").mkdir(parents=True, exist_ok=True)
    (out / "rows" / f"{s}.json").write_text(json.dumps(rows, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "bad": bad, "sec": time.time() - t0, "rss": vsp.rss_mb()}


def _rnd(x: float) -> float | None:
    return None if not np.isfinite(x) else round(float(x), 4)


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "rows" / f"{s}.json").exists()]
    emit(f"[run] {len(jobs)} of {len(syms)} symbols to do, {a.workers} workers")
    t0 = time.time()
    n = bad = 0
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for j, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            n += r["n"]
            bad += r["bad"]
            if j % 25 == 0 or j == len(jobs):
                el = time.time() - t0
                emit(
                    f"  [run] {j}/{len(jobs)} {el:.0f}s eta {el / j * (len(jobs) - j):.0f}s; rows {n}; unmatched signals {bad}; worker rss {r['rss']:.0f} MB; main {vsp.rss_mb():.0f} MB"
                )
    emit(f"[run] done in {time.time() - t0:.0f}s; rows {n}; unmatched signals {bad}")


# ---------------------------------------------------------------- samples (the passport's)


def samples(a: argparse.Namespace) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    T = vsp.load_rows(a.passport_cache / "S14")
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


def label(name: str, ok: bool, r: dict[str, float], share_ok: bool = True) -> str:
    if ok:
        return "✓ пройшов"
    se = (r["hi"] - r["lo"]) / 3.92
    mde = Z_MDE * se
    eff = INTEREST[name]
    if not share_ok:
        return f"◯ невимірно (частка ознаки поза зареєстрованими межами; MDE₈₀ {mde:.3f})"
    if r["hi"] < eff:
        return f"✗ не підтверджено, корисний приріст (≥ {eff:g}R) виключено (MDE₈₀ {mde:.3f})"
    if mde > eff:
        return f"◯ невимірно (MDE₈₀ {mde:.3f} > {eff:g}R)"
    return f"✗ не підтверджено (MDE₈₀ {mde:.3f})"


def cells_of(T: dict[str, np.ndarray], m: np.ndarray, side_m: np.ndarray, val: Any) -> list[tuple[str, float]]:
    """Robustness cells: OOS years on ``m``'s OOS part, sides and timeframes on ``side_m``."""
    oos = T["t_in"] >= vsp.OOS_MS
    out = [("2025", val(m & oos & (T["year"] == 2025))), ("2026", val(m & oos & (T["year"] == 2026)))]
    for lab, c in (("long", T["side"] == "long"), ("short", T["side"] == "short"), ("4h", T["tf"] == "4h"), ("1d", T["tf"] == "1d")):
        out.append((lab, val(side_m & c)))
    return out


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T, S = samples(a)
    mod: dict[str, dict[str, Any]] = {}
    for p in sorted((out / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            mod[r["k"]] = r
    n = T["B"].size
    V, P = S["V"], S["P"]
    keys = T["key"]
    miss = sum(1 for k in keys[V | P] if k not in mod)

    def col(name: str) -> np.ndarray:
        return np.array([math.nan if (x := mod.get(k, {}).get(name)) is None else float(x) for k in keys])

    B_mine = col("B")
    agree = (V | P) & np.isfinite(B_mine)
    emit(
        f"# S14 modules: passport candidates {n}; V ∪ P {int((V | P).sum())}, without a module row {miss}; base R equal to the passport on {int(np.sum(np.abs(B_mine[agree] - T['B'][agree]) < 1e-9))} of {int(agree.sum())}"
    )
    IS, OOS = (T["t_in"] >= vsp.PASSPORT_MS) & (T["t_in"] < vsp.OOS_MS), T["t_in"] >= vsp.OOS_MS
    emit(
        f"V: IS {int((V & IS).sum())}, OOS {int((V & OOS).sum())} (2025 {int((V & OOS & (T['year'] == 2025)).sum())}, 2026 {int((V & OOS & (T['year'] == 2026)).sum())}); P: 2021+ {int(P.sum())}, OOS {int((P & OOS).sum())}"
    )
    emit(
        f"база: V OOS {fmtd(vsp.ci(T, V & OOS))} (тижнів {np.unique(T['week'][V & OOS]).size}); V 2021+ {fmtd(vsp.ci(T, V))}; P OOS {fmtd(vsp.ci(T, P & OOS))}; ×1.5 V OOS {np.nanmean(T['B15'][V & OOS]):+.3f}, 2021+ {np.nanmean(T['B15'][V]):+.3f}"
    )
    yrs = v3.years_oos()
    yrs21 = (vsp.END["4h"] - vsp.PASSPORT_MS) / (365.25 * DAY_MS)
    res: dict[str, dict[str, Any]] = {}

    # --- H10 (paired, OOS)
    tgt = col("tgt") == 1.0
    R10, R10s = col("H10"), col("H10_15")
    d10 = R10 - T["B"]
    dur = col("H10d")
    Rp = T["B"].copy()
    for tf in ("4h", "1d"):
        pool = dur[V & OOS & tgt & (T["tf"] == tf) & np.isfinite(dur)]
        if pool.size == 0:
            continue
        for idx in np.flatnonzero(tgt & (T["tf"] == tf)):
            c = mod.get(keys[idx], {}).get("H10c")
            if not c:
                continue
            h = mo.quantile_hours(pool, m3.unit_draw(str(keys[idx]), "h10t"))
            Rp[idx] = mo.half_and_runner(float(T["B"][idx]), float(c[min(h, len(c) - 1)]))
    m = V & OOS
    emit("\n## H10 (половина на цілі, решта до закриття під EMA21)")
    r = v3.paired(T, d10, m)
    rp = v3.paired(T, R10 - Rp, m)
    ra = v3.paired(T, d10, m & tgt)
    kinds: dict[str, int] = {}
    for k in keys[m & tgt]:
        kk = mod.get(k, {}).get("H10k", "?")
        kinds[kk] = kinds.get(kk, 0) + 1
    emit(
        f"OOS V: n {int(r['n'])}, Δ {fmtd(r)} p₁ {r['p1']:.4f}; проти плацебо {fmtd(rp)} p₁ {rp['p1']:.4f}; плацебо − база {np.nanmean((Rp - T['B'])[m]):+.3f}"
    )
    emit(
        f"  дійшли до цілі {np.mean(tgt[m]):.1%}; Δ на них {fmtd(ra)} (n {int(ra['n'])}); виходи решти {kinds}; тривалість решти, год: медіана {np.nanmedian(dur[m & tgt]):.0f}"
    )
    mk = m & np.isfinite(R10)
    emit(
        f"  E[R] OOS V: база {np.mean(T['B'][mk]):+.3f}, модуль {np.mean(R10[mk]):+.3f}; ×1.5: база {np.nanmean(T['B15'][mk]):+.3f}, модуль {np.nanmean(R10s[mk]):+.3f}"
    )
    emit(
        f"  сума R / рік OOS V: база {np.sum(T['B'][mk]) / yrs:+.1f}, модуль {np.sum(R10[mk]) / yrs:+.1f}; угод / рік {mk.sum() / yrs:.0f}"
    )
    ri, rP, r21 = v3.paired(T, d10, V & IS), v3.paired(T, d10, P & OOS), v3.paired(T, d10, V)
    emit(f"  IS V Δ {fmtd(ri)}; 2021+ V Δ {fmtd(r21)}; OOS P Δ {fmtd(rP)} (n {int(rP['n'])})")
    cells = cells_of(T, m, m, lambda mm: float(np.nanmean(d10[mm])) if mm.any() else math.nan)
    mt, top = v3.drop_top(T, d10, m)
    cells.append(("без топ-5 монет", float(np.nanmean(d10[mt]))))
    emit("  розбивки OOS V (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
    v3.per_asset(T, d10, V & IS, m, "H10")
    res["H10"] = {"r": r, "cells": cells, "rP": rP, "plc_p": rp["p1"], "E15": float(np.nanmean(R10s[mk])), "share_ok": True, "extra": True}

    # --- subgroup modules H7, H1, H2 (primary: V 2021+)
    h2d, h2pd = col("H2d"), col("H2pd")
    flags = {
        "H7": (col("H7") == 1.0, np.isfinite(col("H7"))),
        "H1": (col("H1") == 1.0, np.isfinite(col("H1"))),
        "H2": (h2d <= 0.0, np.isfinite(h2d)),
    }
    for name, (flag, defined) in flags.items():
        m21 = V & defined
        mO = V & OOS & defined
        sh_is, sh_oos, sh21 = np.mean(flag[V & IS & defined]), np.mean(flag[mO]), np.mean(flag[m21])
        share_ok = True
        if name == "H1":
            share_ok = bool(0.30 <= sh_is <= 0.70 and 0.30 <= sh_oos <= 0.70)
        if name == "H2":
            share_ok = bool(sh21 >= 0.20)
        emit(f"\n## {name}")
        emit(
            f"частка ознаки: IS {sh_is:.1%}, OOS {sh_oos:.1%}, 2021+ {sh21:.1%} (визначено 2021+ {int(m21.sum())} з {int(V.sum())}); умова частки {'так' if share_ok else 'НІ'}"
        )
        r = v3.sub_delta(T, flag, m21)
        obs, pl = m3.subgroup_placebo(T["B"][m21], flag[m21], v3.N_PLACEBO_SUB, vsp.SEED)
        pp = float((np.sum(pl >= obs) + 1) / (pl.size + 1)) if pl.size else math.nan
        emit(
            f"2021+ V: з ознакою n {int(r['n'])}, E[R] {np.mean(T['B'][m21 & flag]):+.3f} проти {np.mean(T['B'][m21]):+.3f} усіх; Δ {fmtd(r)} p₁ {r['p1']:.4f}; плацебо p {pp:.4f}"
        )
        ri, ro, rP = v3.sub_delta(T, flag, V & IS & defined), v3.sub_delta(T, flag, mO), v3.sub_delta(T, flag, P & defined)
        emit(
            f"  IS V Δ {fmtd(ri)}; OOS V Δ {fmtd(ro)} (n з ознакою {int(ro['n'])}, E[R] {np.mean(T['B'][mO & flag]):+.3f}); 2021+ P Δ {fmtd(rP)}"
        )
        extra = ri["m"] > 0 and ro["m"] > 0
        if name == "H2":
            q = float(np.quantile(h2pd[m21 & np.isfinite(h2pd)], sh21)) if np.any(m21 & np.isfinite(h2pd)) else math.nan
            fp = h2pd <= q
            mp = m21 & np.isfinite(h2pd)
            rpl = v3.sub_delta(T, fp, mp)
            emit(
                f"  плацебо «застарілий рівень» (t₀ − 5…30 діб, поріг {q:.3f} ATR, частка {np.mean(fp[mp]):.1%}): Δ {fmtd(rpl)}; частка ознаки з EMA {np.mean((col('H2e') <= 0)[m21]):.1%}, з PDL/PWL {np.mean((col('H2h') <= 0)[m21]):.1%}"
            )
            extra = extra and rpl["m"] < r["m"]
        cells = cells_of(
            T,
            V & defined,
            m21,
            lambda mm, flag=flag: float(np.mean(T["B"][mm & flag]) - np.mean(T["B"][mm])) if np.any(mm & flag) else math.nan,
        )
        contrib = np.where(flag, T["B"] - np.mean(T["B"][m21]), 0.0)
        mt, top = v3.drop_top(T, contrib, m21)
        cells.append(("без топ-5 монет", float(np.mean(T["B"][mt & flag]) - np.mean(T["B"][mt]))))
        emit("  розбивки (Δ; роки — OOS, інше — 2021+): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
        e15o, e15 = float(np.nanmean(T["B15"][mO & flag])), float(np.nanmean(T["B15"][m21 & flag]))
        emit(
            f"  угод з ознакою / рік: 2021+ {np.sum(m21 & flag) / yrs21:.0f}, OOS {np.sum(mO & flag) / yrs:.0f}; сума R з ознакою / рік: 2021+ {np.sum(T['B'][m21 & flag]) / yrs21:+.1f}, OOS {np.sum(T['B'][mO & flag]) / yrs:+.1f}; E[R] з ознакою ×1.5: OOS {e15o:+.3f}, 2021+ {e15:+.3f}"
        )
        dflag = np.where(flag, T["B"], math.nan)
        v3.per_asset(T, dflag - np.nanmean(T["B"][m21]), V & IS & flag, V & OOS & flag, name + " (R угод з ознакою)")
        res[name] = {"r": r, "cells": cells, "rP": rP, "plc_p": pp, "E15": min(e15o, e15), "share_ok": share_ok, "extra": extra}

    # --- verdict: Holm over the 4 registered modules
    p1 = [res[mn]["r"]["p1"] for mn in MODULES]
    adj = holm([x if np.isfinite(x) else 1.0 for x in p1])
    emit("\n## Вердикт (Холм на 4; H10 — OOS, H7 / H1 / H2 — 2021+)")
    passed = []
    for mn, p, pa in zip(MODULES, p1, adj, strict=True):
        x = res[mn]
        robust = all(np.isfinite(v) and v > 0 for c, v in x["cells"] if c in ("2025", "2026", "long", "short", "без топ-5 монет"))
        ok = (x["r"]["m"] > 0) and pa < 0.05 and x["plc_p"] < 0.05 and robust and x["rP"]["m"] > 0 and x["extra"] and x["share_ok"]
        idea = "торгова ідея" if x["E15"] > 0 else "менший збиток"
        # H10 has no registered IS / stale-level requirement: its "extra" is a pass-through True.
        extra_txt = "н/з" if mn == "H10" else ("так" if x["extra"] else "ні")
        emit(
            f"{mn}: Δ {fmtd(x['r'])}, p₁ {p:.4f}, Холм {pa:.4f}, плацебо p {x['plc_p']:.4f}, стійкість {'так' if robust else 'ні'}, IS/OOS/плацебо-рівень {extra_txt}, P Δ {x['rP']['m']:+.3f}, E[R]×1.5 {x['E15']:+.3f} → {label(mn, ok, x['r'], x['share_ok'])}{' (' + idea + ')' if ok else ''}"
        )
        if ok:
            passed.append(mn)
    emit(f"пройшли: {passed or 'жоден'}")
    emit("комбінація: потрібна окрема стадія (пройшло ≥ 2)" if len(passed) >= 2 else "комбінації немає (пройшло менше двох модулів)")

    # --- registered descriptive rows: overlap
    emit("\n## Описово (зареєстровано): перекриття")
    f7, f1, f2 = (flags[x][0] for x in ("H7", "H1", "H2"))
    alld = flags["H7"][1] & flags["H1"][1] & flags["H2"][1]
    for per, pm in (("2021+", V & alld), ("OOS", V & OOS & alld)):
        parts = []
        for a7 in (True, False):
            for a1 in (True, False):
                for a2 in (True, False):
                    c = pm & (f7 == a7) & (f1 == a1) & (f2 == a2)
                    lab = "".join(nm if on else "·" for nm, on in (("7", a7), ("1", a1), ("2", a2)))
                    parts.append(f"{lab} n {int(c.sum())} {np.mean(T['B'][c]) if c.any() else math.nan:+.3f}")
        emit(f"H7×H1×H2 {per}: " + "; ".join(parts))
    emit(
        "Δ H10 OOS за ознаками: "
        + "; ".join(
            f"{nm} так {np.nanmean(d10[V & OOS & fl & flags[nm][1]]):+.3f} / ні {np.nanmean(d10[V & OOS & ~fl & flags[nm][1]]):+.3f}"
            for nm, fl in (("H7", f7), ("H1", f1), ("H2", f2))
        )
    )
    emit(
        f"сигналів без знайденого фрактала (H1 / H2 не визначені): V {np.mean(col('f')[V] != 1.0):.1%}; tf 1d OOS n {int(np.sum(V & OOS & (T['tf'] == '1d')))}"
    )

    # --- exploratory, added after the first report (not registered, no Holm): H2's flag held 91 % of the trades
    # (PDL / PWL in the sweep zone), so the weight metric could hardly move; what the flag-vs-rest contrast and the
    # EMA-only part show
    emit("\n## Розвідувально (після результатів, не зареєстровано)")
    fe = col("H2e") <= 0.0
    for per, pm in (("2021+", V), ("OOS", V & OOS)):
        c2 = sp_diff(T, f2, ~f2, pm)
        ce = sp_diff(T, fe, ~fe, pm)
        re = v3.sub_delta(T, fe, pm)
        emit(
            f"H2 {per}: з ознакою − без {fmtd(c2)} (без n {int(np.sum(pm & ~f2))}); лише EMA100/200 у зоні: частка {np.mean(fe[pm]):.1%}, Δ ваги {fmtd(re)}, з − без {fmtd(ce)}"
        )
    for per, pm in (("2021+", V), ("OOS", V & OOS)):
        c1 = sp_diff(T, f1, ~f1, pm)
        c7 = sp_diff(T, f7, ~f7, pm)
        emit(f"з ознакою − без, {per}: H1 {fmtd(c1)}; H7 {fmtd(c7)}")


def sp_diff(T: dict[str, np.ndarray], a: np.ndarray, b: np.ndarray, m: np.ndarray) -> dict[str, float]:
    """E[R | a] - E[R | b] on ``m``, week-block bootstrap (the passport's ``diff_ci``)."""
    r = v3.sp.diff_ci(T["B"], a & m, b & m, T["week"], n_boot=vsp.N_BOOT, seed=vsp.SEED)
    r["m"] = r["d"]
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s14_modules")
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
        emit(f"verify_s14_modules --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"run": stage_run, "report": stage_report}[a.stage](a, out)
        emit(f"peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
