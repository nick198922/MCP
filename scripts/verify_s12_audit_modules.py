#!/usr/bin/env python3
"""S12 v4 modules from the weekly audit of 2026-11-03 (IDEA-20261030-demo-setups-depth, «Покращення»): L (the touch
bar sweeps the nearest unswept 1h Williams fractal low in the lower half of the gap and closes above it) and A (>= 3
of the touch bar's four 1h sub-bars closed above the gap's middle), each as a subgroup of the v4 base trades, with the
registered placebos, Holm over 2. Registration: the card's section «Попередня реєстрація хвилі S12 v4 з аудиту 03.11».

    uv run python scripts/verify_s12_audit_modules.py --stage run      # per-symbol feature rows (local caches)
    uv run python scripts/verify_s12_audit_modules.py --stage report   # tables (also <cache>/report.txt)

No network: 1h bars from the S14 cache; base trades, the demo selection and the random-entry placebo trades from the
S12 modules cache of 2026-11-01 (``verify_s12_modules.py --stage run``, ``--base-dir``).
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

import verify_s12_modules as v12  # noqa: E402
import verify_s38_modules as v38  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import s3_modules as mo  # noqa: E402
from tradedesk.analytics import s12_audit as au  # noqa: E402
from tradedesk.analytics import s12_modules as m12  # noqa: E402
from tradedesk.analytics import setup_signals as ss  # noqa: E402
from tradedesk.analytics.course_s14_ema import resample  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.s38_audit import spearman  # noqa: E402

BAR = 4 * 3_600_000
MODULES = ("L", "A")
A_MIN = 3
L4_BACK = 30  # sanity only: 4h fractals from 30 bars before the gap
MDE_K = 1.960 + 0.842  # one-sided alpha 0.05 / 2 (Holm's first step) and 80 % power
INTEREST = 0.05
N_PLACEBO_SUB = 2000
EXPECT = (35505, 8829)  # V 2021+ and V OOS of the 2026-11-01 report
emit = vsp.emit


# ---------------------------------------------------------------- per-symbol rows


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir = args
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    tb = resample(h1, 4)
    rows: list[dict[str, Any]] = []
    for side in ("long", "short"):
        bb = tb if side == "long" else tb.mirrored()
        path = h1 if side == "long" else h1.mirrored()
        pos, zones, _, _ = ss._s12_zones(bb, 2, len(bb) - 1)
        close_t = bb.t + BAR
        v4: dict[int, tuple[Any, float]] = {}
        for z in zones:  # the same v4 selection as verify_s12_modules.run_symbol
            i, mid = z.form, z.levels["mid"]
            j = ss._first_test(bb, pos, i, mid)
            if j < 0:
                continue
            st = mid - ss.S12_STOP_ATR * z.atr
            tg = mid + ss.S12_TARGET_R * (mid - st)
            if bb.low[j] > st and bb.c[j] < tg and (j not in v4 or i > v4[j][0].form):
                v4[j] = (z, mid)
        for j, (z, mid) in sorted(v4.items()):
            t_in = int(close_t[j])
            if not (vsp.START_MS <= t_in < vsp.END["4h"]):
                continue
            i, full = z.form, float(z.levels["full"])
            lo = int(np.searchsorted(path.t, int(bb.t[i - 2])))
            end = int(np.searchsorted(path.t, int(bb.t[j])))
            lev = au.sweep_level(path.t, path.low, lo, end, full, mid)
            lev4 = au.sweep_level(bb.t, bb.low, max(i - 2 - L4_BACK, 1), j, full, mid, step_ms=BAR)
            k0 = m12.touch_hours(path, int(bb.t[j]), BAR)
            acc = au.acceptance_hours(path.c[k0 : k0 + 4], mid) if k0 >= 0 else None
            rows.append(
                {
                    "k": f"{s}:{side}:{int(bb.t[j])}",
                    "L": bool(au.sweep_flag(lev, float(bb.low[j]), float(bb.c[j]))),
                    "Lh": bool(math.isfinite(lev)),
                    "Ld": (float(bb.c[j]) - lev) / z.atr if math.isfinite(lev) else None,
                    "L4": bool(math.isfinite(lev4)),
                    "A": acc,
                }
            )
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
    yrs = sorted({int(y) for y in T["year"][rows[0][2]]})
    emit("  рік | " + " | ".join(f"{lab}: угод / Σ R" for lab, _, _ in rows))
    for y in yrs:
        cells = []
        for _, R, m in rows:
            mm = m & (T["year"] == y) & np.isfinite(R)
            cells.append(f"{int(mm.sum())} / {np.sum(R[mm]):+.1f}")
        emit(f"  {y} | " + " | ".join(cells))


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T, _, Q, why = v12.load(a.base_dir)
    mod: dict[str, dict[str, Any]] = {}
    for p in sorted((out / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            mod[r["k"]] = r
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, vsp.s1.eligible_syms(a.data_dir))
    top_days = vsp.top_n_days(qv, vsp.TOP_N)
    guard = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    post = T["t_in"] >= vsp.PASSPORT_MS
    V = vsp.chain(T, vsp.in_prev_day_top(T, top_days) & turn5 & guard & post)
    P = vsp.chain(T, turn5 & guard & post)
    OOS = T["t_in"] >= vsp.OOS_MS
    IS = ~OOS
    B, B15 = T["B"], T["B15"]
    yrs = (vsp.END["4h"] - vsp.OOS_MS) / (365.25 * vsp.DAY_MS)
    miss = int(sum(1 for k in T["k"] if k not in mod))
    extra = len(set(mod) - set(T["k"].tolist()))
    dropped = why["under_1m_or_unknown"] + why["no_path"]  # zones the base cache skipped before writing a row
    emit(
        f"# S12 v4 audit modules L / A: v4 candidates {B.size}; V 2021+ {int(V.sum())}, V OOS {int((V & OOS).sum())} (2026-11-01: {EXPECT[0]} / {EXPECT[1]}); "
        f"P {int(P.sum())}; candidates without a feature row {miss}, rows without a candidate {extra} (base skipped: turnover < $1M / unknown or no path {dropped}); "
        f"detector mismatches in the base cache {why['detector_mismatch']}"
    )
    if (int(V.sum()), int((V & OOS).sum())) != EXPECT or miss or extra != dropped:
        emit("stop: the base does not reproduce the 2026-11-01 sample (registration)")
        return
    rb = v12.mci(B[V & OOS], T["week"][V & OOS], vsp.N_BOOT)
    emit(f"база OOS V {v38.fmtd(rb)} (n {int(rb['n'])}); Σ R / рік {np.sum(B[V & OOS]) / yrs:+.1f}, угод / рік {np.sum(V & OOS) / yrs:.0f}")

    def col(name: str) -> np.ndarray:
        return np.array([math.nan if (x := mod.get(k, {}).get(name)) is None else float(x) for k in T["k"]])

    Lf, Lh, Ld, L4, Ac = col("L"), col("Lh"), col("Ld"), col("L4"), col("A")

    # random-entry placebo of the same geometry (2026-11-01 cache), only the ones in the top-100 of their day
    qtop = vsp.in_prev_day_top(Q, top_days)
    pl: dict[str, list[float]] = defaultdict(list)
    for k, b, ok in zip(Q["k"], Q["B"], qtop, strict=True):
        if ok and np.isfinite(b):
            pl[str(k)].append(float(b))
    PB = np.array([np.mean(pl[k]) if pl.get(k) else math.nan for k in T["k"]])
    X = B - PB

    m = V & OOS
    emit("\n## Санітарні перевірки (OOS V)")
    emit(
        f"  L: рівень (1h-фрактал у [низ; середина], незібраний) є в {np.mean(Lh[m] == 1):.1%}; прапор L {np.mean(Lf[m] == 1):.1%}; "
        f"4h-варіант (30 свічок до розриву): рівень є в {np.mean(L4[m] == 1):.2%} (очікується ≈ 0 за побудовою)"
    )
    emit(f"  L: (закриття j − рівень) / ATR у прапорі: медіана {np.nanmedian(Ld[m & (Lf == 1)]):.2f}")
    emit(
        f"  A визначено {np.mean(np.isfinite(Ac[m])):.1%}; розподіл годин над серединою 0..4: {', '.join(str(int(np.sum(Ac[m] == h))) for h in range(5))}"
    )
    emit(f"  плацебо геометрії: угод V OOS з ≥ 1 плацебо {int(np.sum(m & np.isfinite(PB)))} з {int(m.sum())}")

    flags = {
        "L": (Lf, Lf, "свип 1h-фрактального лоу в нижній половині розриву, закриття вище"),
        "A": (np.where(np.isfinite(Ac), (Ac >= A_MIN).astype(float), math.nan), Ac, f"≥ {A_MIN} з 4 годин закрились вище середини"),
    }
    years = (("2025", T["year"] == 2025), ("2026", T["year"] == 2026))
    res: dict[str, dict[str, Any]] = {}
    for name, (fv, raw, lab) in flags.items():
        defined = np.isfinite(fv)
        flag = fv == 1.0
        m = V & OOS & defined
        r = v38.sub_delta(T, B, flag, m)
        obs, plc = mo.subgroup_placebo(B[m], flag[m], N_PLACEBO_SUB, vsp.SEED)
        pp = float((np.sum(plc >= obs) + 1) / (plc.size + 1)) if plc.size else math.nan
        rc = v38.sub_delta(T, X, flag, m & np.isfinite(X))
        se = (r["hi"] - r["lo"]) / 3.92
        emit(f"\n## {name} ({lab})")
        emit(
            f"OOS V (визначено {int(m.sum())}): з ознакою n {int(r['n'])} ({r['n'] / max(1, m.sum()):.1%}), E[R] {np.mean(B[m & flag]):+.3f} проти {np.mean(B[m]):+.3f} усіх; "
            f"Δ {v38.fmtd(r)} p₁ {r['p1']:.4f}; MDE₈₀ {MDE_K * se:.3f}"
        )
        emit(
            f"  плацебо-підгрупа p {pp:.4f} (95-й перц. {np.quantile(plc, 0.95):+.3f}); плацебо геометрії: Δ на X {v38.fmtd(rc)} p₁ {rc['p1']:.4f}"
        )
        er = v38.paired(T, B, m & flag)
        emit(
            f"  E[R] з ознакою {v38.fmtd(er)}; без {np.mean(B[m & ~flag]):+.3f} (n {int(np.sum(m & ~flag))}); стрес ×1.5: з ознакою {np.nanmean(B15[m & flag]):+.3f}, усі {np.nanmean(B15[m]):+.3f}"
        )
        ri = v38.sub_delta(T, B, flag, V & IS & defined)
        r21 = v38.sub_delta(T, B, flag, V & defined)
        rP = v38.sub_delta(T, B, flag, P & OOS & defined)
        emit(
            f"  IS V Δ {v38.fmtd(ri)} (частка {ri['n'] / max(1, np.sum(V & IS & defined)):.1%}); V 2021+ Δ {v38.fmtd(r21)} MDE₈₀ {MDE_K * (r21['hi'] - r21['lo']) / 3.92:.3f}; "
            f"OOS P Δ {v38.fmtd(rP)} (n {int(rP['n'])})"
        )
        cells = [(c, float(np.mean(B[m & cm & flag]) - np.mean(B[m & cm])) if np.any(m & cm & flag) else math.nan) for c, cm in years]
        contrib = np.where(flag, B - np.mean(B[m]), 0.0)
        mt, top = v38.drop_top(T, contrib, m)
        cells.append(("без топ-5 монет", float(np.mean(B[mt & flag]) - np.mean(B[mt]))))
        emit("  розбивки OOS V (Δ): " + "; ".join(f"{c} {v:+.3f}" for c, v in cells) + f" (топ-5: {', '.join(top)})")
        for sd in ("long", "short"):
            ms = m & (T["side"] == sd)
            emit(f"  {sd}: Δ {float(np.mean(B[ms & flag]) - np.mean(B[ms])):+.3f} (n з ознакою {int(np.sum(ms & flag))} з {int(ms.sum())})")
        emit(
            f"  угод з ознакою / рік OOS {np.sum(m & flag) / yrs:.0f}; Σ R з ознакою / рік {np.sum(B[m & flag]) / yrs:+.1f} (база {np.sum(B[m]) / yrs:+.1f})"
        )
        emit(
            f"  Спірмен (сира ознака, R) OOS V {spearman(raw[m], B[m]):+.3f}; IS V {spearman(raw[V & IS & defined], B[V & IS & defined]):+.3f}"
        )
        year_table(T, [("усі V", B, V & defined), (f"{name} V", B, V & defined & flag)])
        v38.per_asset(T, np.where(V & defined & flag, B - np.mean(B[V & OOS & defined]), math.nan), V & IS, V & OOS, name)
        res[name] = {
            "r": r,
            "pp": pp,
            "rc": rc["m"],
            "cells": cells,
            "rP": rP,
            "ri": ri,
            "r21": r21,
            "er": er,
            "se": se,
            "x15": float(np.nanmean(B15[m & flag])),
        }

    # descriptive
    m = V & OOS
    emit("\n## Описово (зареєстровано)")
    for h in range(5):
        c = m & (Ac == h)
        emit(
            f"  A годин {h}: n {int(c.sum())}, E[R] {np.mean(B[c]) if c.any() else math.nan:+.3f}, ×1.5 {np.mean(B15[c]) if c.any() else math.nan:+.3f}"
        )
    for lab, c in (("рівень є, свип", Lf == 1), ("рівень є, без свипу", (Lh == 1) & (Lf == 0)), ("рівня немає", Lh == 0)):
        cc = m & c
        emit(f"  L {lab}: n {int(cc.sum())}, E[R] {np.mean(B[cc]) if cc.any() else math.nan:+.3f}")
    emit("  2 × 2 L × A (OOS V, n / E[R]):")
    for la, cl in (("L=1", Lf == 1), ("L=0", Lf == 0)):
        cells = []
        for aa, ca in (("A≥3", Ac >= A_MIN), ("A<3", Ac < A_MIN)):
            cc = m & cl & ca
            cells.append(f"{aa} {int(cc.sum())} / {np.mean(B[cc]) if cc.any() else math.nan:+.3f}")
        emit(f"    {la}: " + "; ".join(cells))
    kinds = Counter(T["kind"][m].tolist())
    emit("  виходи бази OOS V: " + ", ".join(f"{k} {v / m.sum():.1%}" for k, v in kinds.most_common()))

    # verdict: Holm over 2
    p_mod = [res[mn]["r"]["p1"] if np.isfinite(res[mn]["r"]["p1"]) else 1.0 for mn in MODULES]
    adj = dict(zip(MODULES, holm(p_mod), strict=True))
    emit("\n## Вердикт (Холм на 2)")
    for mn in MODULES:
        x = res[mn]
        robust = all(np.isfinite(v) and v > 0 for _, v in x["cells"])
        dm, mde = x["r"]["m"], MDE_K * x["se"]
        ok = dm > 0 and adj[mn] < 0.05 and x["pp"] < 0.05 and x["rc"] > 0 and robust and x["rP"]["m"] > 0 and x["ri"]["m"] > 0
        if ok:
            v = "✓ CONFIRMED"
        elif dm > 0 and x["r"]["p1"] < 0.05:
            v = "≈ DIRECTIONALLY RIGHT"
        elif x["r"]["hi"] < INTEREST:
            v = "✗ NOT CONFIRMED (корисний ефект виключено на OOS)"
        elif mde > INTEREST:
            v = (
                "✗ NOT CONFIRMED (на OOS невимірно; корисний ефект виключено на 2021+)"
                if x["r21"]["hi"] < INTEREST
                else f"◯ UNVERIFIABLE (MDE₈₀ {mde:.3f} > {INTEREST})"
            )
        else:
            v = "✗ NOT CONFIRMED"
        emit(
            f"{mn}: Δ {v38.fmtd(x['r'])}, p₁ {x['r']['p1']:.4f}, Холм {adj[mn]:.4f}, плацебо-підгрупа p {x['pp']:.4f}, геометрія {x['rc']:+.3f}, стійкість {'так' if robust else 'ні'}, "
            f"P Δ {x['rP']['m']:+.3f}, IS Δ {x['ri']['m']:+.3f}, 2021+ Δ {v38.fmtd(x['r21'])}, MDE₈₀ {mde:.3f}, E[R] з ознакою OOS {x['er']['m']:+.3f} (×1.5 {x['x15']:+.3f}) → {v}"
        )
        if ok:
            emit(f"   {'торгова ідея (E[R] ×1.5 > 0)' if x['x15'] > 0 else 'зменшення збитку (E[R] ×1.5 ≤ 0)'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s12_audit_modules")
    ap.add_argument("--base-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s12_modules/S12")
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
        emit(f"verify_s12_audit_modules --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"run": stage_run, "report": stage_report}[a.stage](a, out)
        emit(f"done in {time.time() - t0:.0f}s; peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
