#!/usr/bin/env python3
"""S38 modules from the weekly audit of 2026-11-03 (IDEA-20261030-demo-setups-depth, «Покращення»): T (>= 3 touches
of the range low before the breakout), D (dry retest: retest-bar / breakout-bar quote volume <= 0.6) and E (sell-stop
entry at the retest bar's low - 1 bp over the next 2 bars instead of the entry at its close), each against the S38
passport base on the same trades, with the registered placebos. Registration: the card's section «Попередня реєстрація
хвилі S38 з аудиту 03.11».

    uv run python scripts/verify_s38_audit_modules.py --stage run      # per-symbol rows (local caches, 4 workers)
    uv run python scripts/verify_s38_audit_modules.py --stage report   # tables (also <cache>/report.txt)

No network: 1h bars from the S14 cache, base trades / demo selection / chain from the S38 passport candidates, the
cross-coin placebo trades of ``verify_s38_modules`` (``--placebo-dir``).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_s38_modules as v38  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import s3_modules as mo  # noqa: E402
from tradedesk.analytics import s38_audit as au  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import simulate_stop_rule  # noqa: E402

DAY_MS = 24 * HOUR_MS
BAR4 = 4 * HOUR_MS
MODULES = ("T", "D", "E")
T_MIN, D_MAX = 3, 0.6
WAIT_MS = 2 * BAR4
MDE_K = 2.128 + 0.842  # one-sided alpha 0.05 / 3 (Holm's first step) and 80 % power
N_PLACEBO_SUB = 2000
SETUP = "S38"
emit = vsp.emit


def _f(x: float) -> float | None:
    return None if not np.isfinite(x) else float(x)


# ---------------------------------------------------------------- per-symbol rows


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir = args
    det = vsp.SETUPS[SETUP][0]
    h1, qv = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    tb = resample(h1, 4)
    a_arr = atr(tb, 14, segment_pos(tb.t))
    path = h1.mirrored()
    hold = vsp.HOLD["4h"]
    rows: list[dict[str, Any]] = []
    for sig in det(tb, "4h"):
        t_in = sig.signal_ts + tb.bar_ms
        if not (vsp.START_MS <= t_in < vsp.END["4h"]):
            continue
        e, st, tg = -sig.entry, -sig.stop, -sig.target
        if not (st < e < tg):
            continue
        base = simulate_stop_rule(path, t_in, e, st, tg, hold, vsp.RULE_B, fees=vsp.F1)
        if base is None:
            continue
        ind = sig.indicators
        j = int(np.searchsorted(tb.t, sig.signal_ts))
        i_s = int(np.searchsorted(tb.t, int(ind["breakout_ts"])))
        lvl, a_j = float(ind["range_low"]), float(ind["atr14"])
        a_prev = float(a_arr[i_s - 1]) if i_s >= 1 else math.nan
        r: dict[str, Any] = {"k": v38.key_of(s, t_in), "B": base.r_net}
        # T: touches of the range low (bars < s)
        r["Tn"] = au.range_touches(tb, i_s, lvl, a_prev)
        # D: retest / breakout quote volume (bars s and j, closed by t_in)
        q_s = au.bar_quote_volume(h1.t, qv, int(tb.t[i_s]), BAR4)
        q_j = au.bar_quote_volume(h1.t, qv, int(tb.t[j]), BAR4)
        r["Dq"] = au.dry_ratio(q_j, q_s)
        r["qs"] = q_s
        # E: sell-stop at the retest low - 1 bp, 2 bars; base stop; target 2R from the fill
        trig = -float(tb.low[j]) * (1 - au.TICK_FRAC)
        for fees, suf in ((vsp.F1, ""), (vsp.F15, "_15")):
            x = au.stop_entry_trade(path, t_in, trig, st, WAIT_MS, hold, vsp.RULE_B, fees)
            r["E" + suf] = _f(x.r_net)
            if not suf:
                r["Est"] = x.status
                r["Ed"] = (x.fill_ms - t_in) // HOUR_MS if x.fill_ms >= 0 else None
        alt = au.stop_entry_trade(path, t_in, trig, -(lvl + 0.5 * a_j), WAIT_MS, hold, vsp.RULE_B, vsp.F1)
        r["Ealt"] = _f(alt.r_net)
        r["Ep"] = [_f(au.delayed_market_trade(path, t_in, d, st, hold, vsp.RULE_B, vsp.F1)) for d in range(WAIT_MS // HOUR_MS)]
        rows.append(r)
    out = Path(out_dir) / "rows"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{s}.json").write_text(json.dumps(rows, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "rss": vsp.rss_mb()}


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "rows" / f"{s}.json").exists()]
    v38.pool_run(run_symbol, jobs, a.workers, "run")


# ---------------------------------------------------------------- report


def year_table(T: dict[str, np.ndarray], rows: list[tuple[str, np.ndarray, np.ndarray]]) -> None:
    """Trades and sum R per calendar year (the last year is partial: to 2026-09-23) for each (label, R, mask)."""
    yrs = sorted({int(y) for y in T["year"][rows[0][2]]})
    emit("  рік | " + " | ".join(f"{lab}: угод / Σ R" for lab, _, _ in rows))
    for y in yrs:
        cells = []
        for _, R, m in rows:
            mm = m & (T["year"] == y) & np.isfinite(R)
            cells.append(f"{int(mm.sum())} / {np.sum(R[mm]):+.1f}")
        emit(f"  {y} | " + " | ".join(cells))


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T, S, _ = v38.samples(a)
    mod = v38.load_mod(out)
    n = T["B"].size

    def col(name: str) -> np.ndarray:
        return np.array([math.nan if (x := mod.get(k, {}).get(name)) is None else float(x) for k in T["key"]])

    V, P = S["V"], S["P"]
    miss = sum(1 for k in T["key"][V | P] if k not in mod)
    Bm = col("B")
    agree = (V | P) & np.isfinite(Bm)
    emit(
        f"# S38 audit modules T / D / E: candidates {n}; V {int(V.sum())}, P {int(P.sum())}; without a row {miss}; base R equal to the passport on {int(np.sum(np.abs(Bm[agree] - T['B'][agree]) < 1e-9))} of {int(agree.sum())}"
    )
    IS, OOS = T["t_in"] < vsp.OOS_MS, T["t_in"] >= vsp.OOS_MS
    yrs = (vsp.END["4h"] - vsp.OOS_MS) / (365.25 * DAY_MS)
    B, B15 = T["B"], T["B15"]
    rb = vsp.ci(T, V & OOS)
    emit(
        f"база OOS V {v38.fmtd(rb)} (n {int(rb['n'])}, тижнів {int(rb['k'])}); Σ R / рік {np.sum(B[V & OOS]) / yrs:+.1f}, угод / рік {np.sum(V & OOS) / yrs:.0f}"
    )

    # cross-coin placebo (verify_s38_modules), mean over draws
    pl: dict[str, list[float]] = defaultdict(list)
    for p in sorted(a.placebo_dir.glob("*.json")):
        for r in json.loads(p.read_text()):
            if r["B"] is not None and np.isfinite(r["B"]):
                pl[r["k"]].append(r["B"])
    PB = np.array([np.mean(pl[k]) if pl.get(k) else math.nan for k in T["key"]])
    X = B - PB
    emit(f"плацебо «монета тієї ж свічки»: угод V OOS з ≥ 1 плацебо {int(np.sum(V & OOS & np.isfinite(PB)))} з {int(np.sum(V & OOS))}")

    # sanity
    Tn, Dq, qs = col("Tn"), col("Dq"), col("qs")
    m = V & OOS
    emit("\n## Санітарні перевірки (OOS V)")
    emit(
        f"  T визначено {np.mean(np.isfinite(Tn[m])):.1%}; дотиків: мін {np.nanmin(Tn[m]):.0f}, медіана {np.nanmedian(Tn[m]):.0f}, 90-й перц. {np.nanpercentile(Tn[m], 90):.0f}"
    )
    emit(
        f"  D визначено {np.mean(np.isfinite(Dq[m])):.1%}; обсяг свічки пробою медіана ${np.nanmedian(qs[m]) / 1e6:.2f}M; q_vol медіана {np.nanmedian(Dq[m]):.2f}, квартилі {np.nanpercentile(Dq[m], 25):.2f} / {np.nanpercentile(Dq[m], 75):.2f}"
    )
    st = np.array([mod.get(k, {}).get("Est", "") for k in T["key"]])
    emit("  E статуси: " + ", ".join(f"{s} {np.mean(st[m] == s):.1%}" for s in ("filled", "no_fill", "cancel_stop", "no_path")))

    res: dict[str, dict[str, Any]] = {}
    years = (("2025", T["year"] == 2025), ("2026", T["year"] == 2026))

    # ------------------------------------------------ subgroups T, D
    flags = {
        "T": (np.where(np.isfinite(Tn), (Tn >= T_MIN).astype(float), math.nan), Tn, f"дотиків ≥ {T_MIN}"),
        "D": (np.where(np.isfinite(Dq), (Dq <= D_MAX).astype(float), math.nan), Dq, f"q_vol ≤ {D_MAX}"),
    }
    for name, (fv, raw, lab) in flags.items():
        defined = np.isfinite(fv)
        flag = fv == 1.0
        m = V & OOS & defined
        r = v38.sub_delta(T, B, flag, m)
        obs, plc = mo.subgroup_placebo(B[m], flag[m], N_PLACEBO_SUB, vsp.SEED)
        pp = float((np.sum(plc >= obs) + 1) / (plc.size + 1)) if plc.size else math.nan
        rc = v38.sub_delta(T, X, flag, m & np.isfinite(X))
        share = r["n"] / max(1, m.sum())
        se = (r["hi"] - r["lo"]) / 3.92
        emit(f"\n## {name} ({lab})")
        emit(
            f"OOS V (визначено {int(m.sum())}): з ознакою n {int(r['n'])} ({share:.0%}), E[R] {np.mean(B[m & flag]):+.3f} проти {np.mean(B[m]):+.3f} усіх; Δ {v38.fmtd(r)} p₁ {r['p1']:.4f}; MDE₈₀ {MDE_K * se:.3f}"
        )
        emit(
            f"  плацебо-підгрупа p {pp:.4f} (95-й перц. {np.quantile(plc, 0.95):+.3f}); проти монети тієї ж свічки {v38.fmtd(rc)} p₁ {rc['p1']:.4f}"
        )
        er = v38.paired(T, B, m & flag)
        emit(
            f"  E[R] з ознакою {v38.fmtd(er)}; без {np.mean(B[m & ~flag]):+.3f} (n {int(np.sum(m & ~flag))}); стрес ×1.5: з ознакою {np.nanmean(B15[m & flag]):+.3f}, усі {np.nanmean(B15[m]):+.3f}"
        )
        ri = v38.sub_delta(T, B, flag, V & IS & defined)
        rP = v38.sub_delta(T, B, flag, P & OOS & defined)
        emit(
            f"  IS V Δ {v38.fmtd(ri)} (частка {ri['n'] / max(1, np.sum(V & IS & defined)):.0%}); OOS P Δ {v38.fmtd(rP)} (n {int(rP['n'])})"
        )
        cells = [(c, float(np.mean(B[m & cm & flag]) - np.mean(B[m & cm])) if np.any(m & cm & flag) else math.nan) for c, cm in years]
        contrib = np.where(flag, B - np.mean(B[m]), 0.0)
        mt, top = v38.drop_top(T, contrib, m)
        cells.append(("без топ-5 монет", float(np.mean(B[mt & flag]) - np.mean(B[mt]))))
        emit("  розбивки OOS V (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
        emit(
            f"  угод з ознакою / рік OOS {np.sum(m & flag) / yrs:.0f}; Σ R з ознакою / рік {np.sum(B[m & flag]) / yrs:+.1f} (база {np.sum(B[m]) / yrs:+.1f})"
        )
        emit(f"  Спірмен (сира ознака, R) OOS V {au.spearman(raw[m], B[m]):+.3f}; IS V {au.spearman(raw[V & IS], B[V & IS]):+.3f}")
        year_table(T, [("усі V", B, V & defined), (f"{name} V", B, V & defined & flag)])
        v38.per_asset(T, np.where(V & defined & flag, B - np.mean(B[V & OOS & defined]), math.nan), V & IS, V & OOS, name)
        res[name] = {"r": r, "pp": pp, "rc": rc["m"], "cells": cells, "rP": rP, "er": er, "se": se}

    # descriptive bins
    m = V & OOS
    emit("\n## Описово (зареєстровано)")
    for lab, c in (("1", Tn == 1), ("2", Tn == 2), ("3–4", (Tn >= 3) & (Tn <= 4)), ("5+", Tn >= 5)):
        emit(f"  дотиків {lab}: n {int(np.sum(m & c))}, E[R] {np.mean(B[m & c]) if np.any(m & c) else math.nan:+.3f}")
    qs_ = np.nanpercentile(Dq[m], [25, 50, 75])
    edges = [-np.inf, *qs_, np.inf]
    for i in range(4):
        c = (Dq > edges[i]) & (Dq <= edges[i + 1])
        emit(f"  q_vol квартиль {i + 1} (≤ {edges[i + 1]:.2f}): n {int(np.sum(m & c))}, E[R] {np.mean(B[m & c]):+.3f}")

    # ------------------------------------------------ E: stop entry
    E, E15, Ealt, Ed = col("E"), col("E_15"), col("Ealt"), col("Ed")
    m = V & OOS & np.isfinite(E)
    filled = st == "filled"
    fill_share = float(np.mean(filled[m]))
    dd = Ed[m & filled]
    probs = np.bincount(dd.astype(int), minlength=WAIT_MS // HOUR_MS)[: WAIT_MS // HOUR_MS].astype(float)
    Ep = np.array([au.placebo_delay(mod[k]["Ep"], fill_share, probs, k) if k in mod else math.nan for k in T["key"]])
    d = E - B
    r = v38.paired(T, d, m)
    rp = v38.paired(T, E - Ep, m & np.isfinite(Ep))
    se = (r["hi"] - r["lo"]) / 3.92
    emit("\n## E (стоп-вхід на лоу свічки ретесту − 1 б. п., 2 свічки)")
    emit(
        f"OOS V: сигналів {int(m.sum())}, заповнено {fill_share:.1%} (години заповнення 0..7: {', '.join(f'{x:.0f}' for x in probs)}); Δ R на сигнал {v38.fmtd(r)} p₁ {r['p1']:.4f}; MDE₈₀ {MDE_K * se:.3f}"
    )
    emit(
        f"  проти плацебо «випадкова затримка тієї ж частоти» {v38.fmtd(rp)} p₁ {rp['p1']:.4f}; E[R] плацебо на сигнал {np.nanmean(Ep[m]):+.3f}"
    )
    er = v38.paired(T, E, m)
    emit(
        f"  E[R] на сигнал: база {np.mean(B[m]):+.3f}, модуль {v38.fmtd(er)}; на заповнений {np.mean(E[m & filled]):+.3f} (база тих самих {np.mean(B[m & filled]):+.3f}); незаповнені — база {np.mean(B[m & ~filled]):+.3f} (n {int(np.sum(m & ~filled))})"
    )
    emit(
        f"  стрес ×1.5: база {np.nanmean(B15[m]):+.3f}, модуль {np.nanmean(E15[m]):+.3f}; описово стоп «рівень + 0.5 ATR»: {np.nanmean(Ealt[m]):+.3f}"
    )
    emit(
        f"  Σ R / рік OOS V: база {np.sum(B[m]) / yrs:+.1f}, модуль {np.nansum(E[m]) / yrs:+.1f}; угод / рік база {np.sum(m) / yrs:.0f}, модуль {np.sum(m & filled) / yrs:.0f}"
    )
    ri = v38.paired(T, d, V & IS & np.isfinite(E))
    rP = v38.paired(T, d, P & OOS & np.isfinite(E))
    emit(f"  IS V Δ {v38.fmtd(ri)}; OOS P Δ {v38.fmtd(rP)} (n {int(rP['n'])})")
    cells = [(c, float(np.nanmean(d[m & cm]))) for c, cm in years]
    mt, top = v38.drop_top(T, d, m)
    cells.append(("без топ-5 монет", float(np.nanmean(d[mt]))))
    emit("  розбивки OOS V (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
    year_table(T, [("база V", B, V & np.isfinite(E)), ("E V (заповнені)", E, V & np.isfinite(E) & filled)])
    v38.per_asset(T, np.where(V & np.isfinite(E), d, math.nan), V & IS, V & OOS, "E")
    res["E"] = {"r": r, "pp": rp["p1"], "rc": math.inf, "cells": cells, "rP": rP, "er": er, "se": se}

    # ------------------------------------------------ verdict: Holm over 3
    p_mod = [res[mn]["r"]["p1"] if np.isfinite(res[mn]["r"]["p1"]) else 1.0 for mn in MODULES]
    adj = dict(zip(MODULES, holm(p_mod), strict=True))
    emit("\n## Вердикт (Холм на 3)")
    for mn in MODULES:
        x = res[mn]
        robust = all(np.isfinite(v) and v > 0 for _, v in x["cells"])
        dm = x["r"]["m"]
        mde = MDE_K * x["se"]
        ok = dm > 0 and adj[mn] < 0.05 and x["pp"] < 0.05 and x["rc"] > 0 and robust and x["rP"]["m"] > 0
        if ok:
            v = "✓ CONFIRMED"
        elif dm > 0 and x["r"]["p1"] < 0.05:
            v = "≈ DIRECTIONALLY RIGHT"
        elif dm <= 0 or x["r"]["hi"] < mde:
            v = "✗ NOT CONFIRMED"
        else:
            v = "◯ UNVERIFIABLE"
        rc = "—" if not np.isfinite(x["rc"]) else f"{x['rc']:+.3f}"
        emit(
            f"{mn}: Δ {v38.fmtd(x['r'])}, p₁ {x['r']['p1']:.4f}, Холм {adj[mn]:.4f}, власне плацебо p {x['pp']:.4f}, монета тієї ж свічки {rc}, стійкість {'так' if robust else 'ні'}, P Δ {x['rP']['m']:+.3f}, MDE₈₀ {mde:.3f}, E[R] модуля OOS {x['er']['m']:+.3f} → {v}"
        )
        if ok:
            emit(f"   {'торгова ідея (E[R] > 0)' if x['er']['m'] > 0 else 'зменшення збитку (E[R] ≤ 0)'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s38_audit_modules")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--placebo-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s38_modules/placebo")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with (out / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"verify_s38_audit_modules --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"run": stage_run, "report": stage_report}[a.stage](a, out)
        emit(f"done in {time.time() - t0:.0f}s; peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
