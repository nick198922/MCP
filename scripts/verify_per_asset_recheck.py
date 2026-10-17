"""IDEA-20261017-per-asset-recheck: are the closed setups tied to particular assets? Per-asset table, heterogeneity
(Cochran's Q / I^2 and a within-date permutation of asset labels), persistence of the in-sample top quartile out of
sample, and descriptive subgroups (majors / alts, liquidity and volatility quartiles).

Rules are fixed in the card's section "Попередня реєстрація". No network and no re-simulation: only the per-trade rows
saved by the registered runs of the source steps (under --root, default ~/.cache/tradedesk-agents):
  course_s14_ema/trades.json, course_step4_fvg_ob/rows.npz, course_step5_s17/rows.npz,
  deviation_weekly_trend/rows_top50/*.npz, session_stop_hunt/trades.json (+ universe.json / universe_top50.json).
Stages:
  uv run python scripts/verify_per_asset_recheck.py --stage prereg
  uv run python scripts/verify_per_asset_recheck.py --stage sanity --out ~/.cache/tradedesk-agents/per_asset_recheck/sanity.md
  uv run python scripts/verify_per_asset_recheck.py --stage report --out ~/.cache/tradedesk-agents/per_asset_recheck/report.md
  uv run python scripts/verify_per_asset_recheck.py --stage explore --out ~/.cache/tradedesk-agents/per_asset_recheck/explore.md
  uv run python scripts/verify_per_asset_recheck.py --stage bounds --out ~/.cache/tradedesk-agents/per_asset_recheck/bounds.md
``explore`` is exploratory (chosen after the registered results) and is not part of the verdict rule. ``bounds`` was
added after the critic's review: it tests nothing new, it states what the registered tests can exclude (95 % CI for
tau, implied true mean of the best quarter of assets, MDE of the persistence test). No keys, no orders.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import resource
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_step4_fvg_ob as s4  # noqa: E402  (variant ids)
import verify_course_step5_s17 as s5  # noqa: E402  (variant ids)
import verify_deviation_weekly_trend as dv  # noqa: E402  (group / variant codes)

from tradedesk.analytics.liquidation_flush import cluster_bootstrap_ci, holm  # noqa: E402
from tradedesk.analytics.per_asset import (  # noqa: E402
    asset_stats,
    cluster_robust_q,
    heterogeneity,
    mde,
    permutation_q,
    persistence,
    quantile_bins,
    tau_ci_qprofile,
    top_fraction_uplift,
)

CARD = ROOT / "docs" / "ideas" / "IDEA-20261017-per-asset-recheck.md"
DAY_MS = 86_400_000
OOS_DAY = int(np.datetime64("2025-03-01", "D").astype(np.int64))
MAJORS = ("BTCUSDT", "ETHUSDT")
MIN_N, MIN_N_IS, MIN_N_OOS_RHO = 30, 20, 10
MIN_ASSETS_Q, MIN_RANKED = 5, 8
N_PERM, N_BOOT, SEED = 2000, 10_000, 20261017
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


@dataclass
class Rows:
    key: str
    label: str
    primary: bool
    val: np.ndarray  # R net per trade (on the trade's risk, not on capital)
    asset: np.ndarray  # str labels (object)
    day: np.ndarray  # UTC day number
    vol: np.ndarray  # volatility proxy known at entry (fraction of price)
    rank: np.ndarray  # monthly universe volume rank (1 = most liquid), nan if not applicable

    @property
    def is_(self) -> np.ndarray:
        return self.day < OOS_DAY


def day_num(s: str) -> int:
    return int(np.datetime64(s, "D").astype(np.int64))


def month_of_day(d: int) -> str:
    return datetime.fromtimestamp(d * 86400, tz=UTC).strftime("%Y-%m")


def ranks_for(uni: dict[str, list[str]], assets: np.ndarray, days: np.ndarray) -> np.ndarray:
    pos = {mo: {s: i + 1 for i, s in enumerate(v)} for mo, v in uni.items()}
    out = np.full(assets.size, np.nan)
    cache: dict[int, str] = {}
    for i, (a, d) in enumerate(zip(assets, days, strict=True)):
        mo = cache.setdefault(int(d), month_of_day(int(d)))
        r = pos.get(mo, {}).get(a)
        if r is not None:
            out[i] = r
    return out


# ---------------------------------------------------------------- loaders (cached rows of the registered runs)


def load_s14(root: Path) -> list[Rows]:
    t0 = time.time()
    raw = json.loads((root / "course_s14_ema" / "trades.json").read_text())
    emit(f"  [load] S14 trades.json: {len(raw)} rows in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    want = {("4h", "long", "touch"): ("S14", "S14: 4h лонг, дотик EMA (основний)", True)}
    want[("1h", "long", "touch")] = ("S14-1h", "S14: 1h лонг, дотик (другорядний)", False)
    want[("4h", "short", "touch")] = ("S14-sh", "S14: 4h шорт, дотик (другорядний)", False)
    acc: dict[tuple[str, str, str], list[tuple[float, str, str, float]]] = {k: [] for k in want}
    for r in raw:
        k = (r["tf"], r["side"], r["variant"])
        if k in acc:
            acc[k].append((float(r["r_net"]), r["sym"], r["day"], float(r["stop_pct"])))
    del raw
    gc.collect()
    uni = json.loads((root / "course_s14_ema" / "universe.json").read_text())
    out = []
    for k, (key, label, prim) in want.items():
        v, a, d, sp = zip(*acc[k], strict=True)
        asset = np.array(a, dtype=object)
        day = np.array([day_num(x) for x in d], dtype=np.int64)
        out.append(Rows(key, label, prim, np.array(v), asset, day, np.array(sp), ranks_for(uni, asset, day)))
    return out


def load_npz_step(root: Path, sub: str, picks: list[tuple[str, str, bool, int]]) -> list[Rows]:
    with np.load(root / sub / "rows.npz") as z:
        R = {c: z[c] for c in ("sym", "var", "kind", "day", "filled", "r_gross", "cost_r", "risk", "fill")}
    uni = json.loads((root / "course_s14_ema" / "universe.json").read_text())
    syms = sorted({s for v in uni.values() for s in v})
    out = []
    for key, label, prim, vid in picks:
        m = (R["var"] == vid) & (R["kind"] == 0) & (R["filled"] == 1)
        net = R["r_gross"][m].astype(np.float64) - R["cost_r"][m].astype(np.float64)
        asset = np.array([syms[int(i)] for i in R["sym"][m]], dtype=object)
        day = R["day"][m].astype(np.int64)
        vol = R["risk"][m].astype(np.float64) / np.abs(R["fill"][m].astype(np.float64))
        out.append(Rows(key, label, prim, net, asset, day, vol, ranks_for(uni, asset, day)))
    del R
    return out


def load_step4(root: Path) -> list[Rows]:
    return load_npz_step(
        root,
        "course_step4_fvg_ob",
        [
            ("FVG-A", "Крок 4 A: FVG 0.5, 4h (основний)", True, s4.VID[s4.PRIMARY["A"]]),
            ("OB-B", "Крок 4 B: валідні OB, 4h (основний)", True, s4.VID[s4.PRIMARY["B"]]),
            ("BRK-C", "Крок 4 C: валідні брейкери, 4h (основний)", True, s4.VID[s4.PRIMARY["C"]]),
        ],
    )


def load_step5(root: Path) -> list[Rows]:
    return load_npz_step(
        root,
        "course_step5_s17",
        [
            ("POB-1D", "Крок 5 POB 1D (другорядний, необов'язковий)", False, s5.VID[s5.PRIMARY["1D"]]),
            ("POB-12h", "Крок 5 POB 12h (другорядний, необов'язковий)", False, s5.VID[s5.PRIMARY["12h"]]),
        ],
    )


def load_dev(root: Path) -> list[Rows]:
    d = root / "deviation_weekly_trend"
    uni = json.loads((d / "universe_top50.json").read_text())
    groups = [("DEV-T", "Девіація: група T, лонг (основний)", True, dv.GROUPS["T"])]
    groups += [("DEV-C", "Девіація: група C, лонг (другорядний)", False, dv.GROUPS["C"])]
    groups += [("DEV-deep", "Девіація: група deep, лонг (другорядний)", False, dv.GROUPS["deep"])]
    acc: dict[str, list[tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]]] = {g[0]: [] for g in groups}
    for f in sorted((d / "rows_top50").glob("*.npz")):
        with np.load(f) as z:
            R = {k: z[k] for k in z.files}
        if R["sym"].size == 0:
            continue
        net = R["r_gross"] - R["cost_r"]
        base = (R["v"] == dv.VCODE["main"]) & (R["side"] == 1) & (R["skipped"] == 0) & (R["has_path"] == 1) & (R["filled"] == 1)
        base &= np.isfinite(net)
        for key, _, _, g in groups:
            m = base & (R["grp"] == g)
            if m.any():
                acc[key].append((net[m], R["day"][m].astype(np.int64), R["stop_pct"][m] / R["stop_atr4"][m], [f.stem] * int(m.sum())))
    out = []
    for key, label, prim, _ in groups:
        parts = acc[key]
        asset = np.array([s for p in parts for s in p[3]], dtype=object)
        day = np.concatenate([p[1] for p in parts])
        out.append(
            Rows(
                key,
                label,
                prim,
                np.concatenate([p[0] for p in parts]),
                asset,
                day,
                np.concatenate([p[2] for p in parts]),
                ranks_for(uni, asset, day),
            )
        )
    return out


def load_ssh(root: Path) -> list[Rows]:
    raw = json.loads((root / "session_stop_hunt" / "trades.json").read_text())
    rows = [r for r in raw if r["anchor"] == "london" and r["entry"] == "E1" and r["target"] == "T1"]
    del raw
    asset = np.array([r["sym"] for r in rows], dtype=object)
    day = np.array([day_num(r["day"]) for r in rows], dtype=np.int64)
    val = np.array([float(r["r_net"]) for r in rows])
    vol = np.array([float(r["stop_pct"]) for r in rows])
    return [Rows("SSH", "Session Stop Hunt: Лондон E1×T1, 6 активів (описово)", False, val, asset, day, vol, np.full(val.size, np.nan))]


def load_all(root: Path) -> list[Rows]:
    out: list[Rows] = []
    for fn in (load_s14, load_step4, load_ssh, load_dev, load_step5):
        t0 = time.time()
        out += fn(root)
        gc.collect()
        emit(f"  [load] {fn.__name__} in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    return out


# ---------------------------------------------------------------- formatting and stats


def ci_s(ci: tuple[float, float] | None) -> str:
    return "—" if ci is None else f"[{ci[0]:+.3f}; {ci[1]:+.3f}]"


def p_s(p: float | None) -> str:
    return "—" if p is None or (isinstance(p, float) and math.isnan(p)) else f"{p:.4f}"


def pooled(v: np.ndarray, d: np.ndarray, n_boot: int = N_BOOT) -> tuple[int, int, float, tuple[float, float] | None]:
    if v.size == 0:
        return 0, 0, float("nan"), None
    return int(v.size), int(np.unique(d).size), float(v.mean()), cluster_bootstrap_ci(v.tolist(), d.tolist(), n_boot=n_boot)


def pooled_line(lab: str, v: np.ndarray, d: np.ndarray, n_boot: int = 5000) -> str:
    n, nd, m, ci = pooled(v, d, n_boot)
    return f"| {lab} | {n} | {nd} | {m:+.3f} | {ci_s(ci)} |" if n else f"| {lab} | 0 | 0 | — | — |"


# ---------------------------------------------------------------- stages


def stage_prereg() -> None:
    t = CARD.read_text()
    sec = t[t.index("## Попередня реєстрація") :]
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec.encode()).hexdigest()} bytes={len(sec.encode())}")


SOURCE = {  # registered pooled numbers of the sources (n, E[R] net); SSH over the whole period, others IS
    "S14": (4664, -0.086, "IS"),
    "FVG-A": (44243, 0.029, "IS"),
    "OB-B": (4137, -0.052, "IS"),
    "BRK-C": (10782, -0.024, "IS"),
    "DEV-T": (183, 0.111, "IS"),
    "SSH": (1915, -0.214, "all"),
}


def sanity(rows: list[Rows]) -> bool:
    emit("## Санітарна перевірка: зведені цифри проти джерел")
    emit("| рядок | вибірка | n (наш / джерело) | E[R] net (наш / джерело) | збіг |\n|---|---|---|---|---|")
    ok_all = True
    for r in rows:
        if r.key not in SOURCE:
            continue
        n_src, m_src, per = SOURCE[r.key]
        m = r.is_ if per == "IS" else np.ones(r.val.size, dtype=bool)
        n, mean = int(m.sum()), float(r.val[m].mean())
        ok = n == n_src and abs(mean - m_src) < 0.0015
        ok_all &= ok
        emit(f"| {r.key} | {per} | {n} / {n_src} | {mean:+.4f} / {m_src:+.3f} | {'так' if ok else '**НІ**'} |")
    emit(
        "\n| рядок | угод | дат | активів | IS / OOS угод | частка з рангом ліквідності | частка з мірою волатильності |\n|---|---|---|---|---|---|---|"
    )
    for r in rows:
        emit(
            f"| {r.key} | {r.val.size} | {np.unique(r.day).size} | {np.unique(r.asset).size} | {int(r.is_.sum())} / {int((~r.is_).sum())} | "
            f"{np.isfinite(r.rank).mean():.1%} | {np.isfinite(r.vol).mean():.1%} |"
        )
    emit(f"\n- пікова RSS {rss_mb():.0f} MB")
    return ok_all


def analyse_row(r: Rows) -> dict[str, Any]:
    t0 = time.time()
    emit(f"\n## {r.key} — {r.label}")
    a, v, d, ism = r.asset, r.val, r.day, r.is_
    emit("| вибірка | угод | дат | E[R] net | 95 % CI (кластери за датою) |\n|---|---|---|---|---|")
    emit(pooled_line("IS", v[ism], d[ism], N_BOOT))
    emit(pooled_line("OOS", v[~ism], d[~ism], N_BOOT))
    emit(pooled_line("весь період", v, d, N_BOOT))

    # (a) per-asset table and heterogeneity, whole period
    al = list(a)
    h = heterogeneity(v, al, min_n=MIN_N)
    testable_a = h["k"] >= MIN_ASSETS_Q
    pq = (
        permutation_q(v, al, list(d), min_n=MIN_N, n_perm=N_PERM, seed=SEED)
        if h["k"] >= 2
        else {"p": None, "q": float("nan"), "null_mean": float("nan"), "null_p95": float("nan")}
    )
    emit(f"\n### (а) Неоднорідність (весь період, активи з n ≥ {MIN_N})")
    emit(
        f"- активів {h['k']} (угод {h['n']} з {v.size}); Q_pooled {h['q']:.1f}, df {h['df']}, p(χ²) {p_s(h['p'])}, I² {h['i2']:.1%}; "
        f"Q з власними дисперсіями {h['q_sep']:.1f}, p {p_s(h['p_sep'])}; τ (DL) {h['tau']:.3f}R"
    )
    emit(
        f"- перестановка міток усередині дати ({N_PERM}): p {p_s(pq['p'])}; Q* середнє {pq['null_mean']:.1f}, 95-й перцентиль {pq['null_p95']:.1f}"
    )
    if not testable_a:
        emit(f"- ◯ непридатний до тесту (а): менше {MIN_ASSETS_Q} активів з n ≥ {MIN_N}")
    keys, counts = np.unique(a.astype(str), return_counts=True)
    elig = [k for k, c in zip(keys, counts, strict=True) if c >= MIN_N]
    a_str = a.astype(str)
    tab = []
    for k in elig:
        m = a_str == k
        x = v[m]
        mi, mo = m & ism, m & ~ism
        se = x.std(ddof=1) / math.sqrt(x.size)
        tab.append(
            (
                k,
                x.size,
                x.mean(),
                (x.mean() - 1.96 * se, x.mean() + 1.96 * se),
                int(mi.sum()),
                v[mi].mean() if mi.any() else float("nan"),
                int(mo.sum()),
                v[mo].mean() if mo.any() else float("nan"),
            )
        )
    tab.sort(key=lambda t: -t[2])
    emit("\n| актив | n | E[R] net | 95 % CI | n IS | E[R] IS | n OOS | E[R] OOS |\n|---|---|---|---|---|---|---|---|")
    for t in tab:
        emit(f"| {t[0]} | {t[1]} | {t[2]:+.3f} | {ci_s(t[3])} | {t[4]} | {t[5]:+.3f} | {t[6]} | {t[7]:+.3f} |")
    n_pos = sum(1 for t in tab if t[3][0] > 0)
    n_neg = sum(1 for t in tab if t[3][1] < 0)
    emit(
        f"- активів з CI > 0: {n_pos}; з CI < 0: {n_neg}; з {len(tab)} (очікувано випадково при нульовому ефекті ≈ {0.025 * len(tab):.1f} з кожного боку)"
    )

    # (b) persistence
    pr = persistence(v, al, list(d), list(ism), min_n_is=MIN_N_IS, top_frac=0.25, min_n_oos_rho=MIN_N_OOS_RHO, n_boot=N_BOOT, seed=SEED)
    p10 = persistence(v, al, list(d), list(ism), min_n_is=MIN_N_IS, top_n=10, min_n_oos_rho=MIN_N_OOS_RHO, n_boot=5000, seed=SEED)
    testable_b = pr["k"] >= MIN_RANKED and pr["n_top_oos"] > 0 and pr["n_rest_oos"] > 0
    emit(f"\n### (б) Стійкість (ранг за IS, n_IS ≥ {MIN_N_IS}; OOS з 2025-03-01)")
    emit(f"- ранжовано активів {pr['k']}; верхня чверть {len(pr['top'])}: {', '.join(pr['top'])}")
    emit(f"- IS: верхня чверть {pr['top_is_mean']:+.3f}, решта ранжованих {pr['rest_is_mean']:+.3f}")
    emit(
        f"- OOS: верхня чверть {pr['top_oos_mean']:+.3f} {ci_s(pr['top_oos_ci'])} (угод {pr['n_top_oos']}, активів з OOS-угодами {pr['n_top_assets_oos']}), "
        f"решта ранжованих {pr['rest_oos_mean']:+.3f} (угод {pr['n_rest_oos']})"
    )
    emit(f"- **різниця OOS (верхня чверть − решта): {pr['diff']:+.3f} {ci_s(pr['diff_ci'])}, p(>0) {p_s(pr['diff_p'])}**")
    emit(f"- верхня чверть − усі інші OOS-угоди: {pr['diff_vs_all']:+.3f} {ci_s(pr['diff_vs_all_ci'])}")
    emit(
        f"- верхні 10 − решта ранжованих (OOS): {p10['diff']:+.3f} {ci_s(p10['diff_ci'])}, p {p_s(p10['diff_p'])} (верхні 10 OOS {p10['top_oos_mean']:+.3f}, угод {p10['n_top_oos']})"
    )
    emit(f"- Spearman ρ IS проти OOS середніх (n_IS ≥ {MIN_N_IS}, n_OOS ≥ {MIN_N_OOS_RHO}): {pr['rho']:+.3f} на {pr['rho_n']} активах")
    if not testable_b:
        emit(f"- ◯ непридатний до тесту (б): ранжованих < {MIN_RANKED} або немає OOS-угод в одній із груп")

    # (c) descriptive subgroups
    emit("\n### (в) Підгрупи (описово, без вердикту)")
    emit("| підгрупа | IS: угод / дат / E[R] net / CI | OOS: угод / дат / E[R] net / CI |\n|---|---|---|")

    def sub(lab: str, m: np.ndarray) -> None:
        cells = []
        for per in (ism, ~ism):
            mm = m & per
            n, nd, mean, ci = pooled(v[mm], d[mm], 3000)
            cells.append(f"{n} / {nd} / {mean:+.3f} / {ci_s(ci)}" if n else "0")
        emit(f"| {lab} | {cells[0]} | {cells[1]} |")

    maj = np.isin(a, MAJORS)
    sub("мейджори (BTC, ETH)", maj)
    sub("альти", ~maj)
    subs: dict[str, Any] = {}
    for name, x in (("ліквідність (ранг у всесвіті; Q1 = найліквідніші)", r.rank), ("волатильність (Q1 = найнижча)", r.vol)):
        fin = np.isfinite(x) & ism
        if fin.sum() < 40:
            continue
        cuts = np.quantile(x[fin], [0.25, 0.5, 0.75])
        b = quantile_bins(x, cuts)
        emit(f"| _{name}; межі IS: {', '.join(f'{c:.4g}' for c in cuts)}_ | | |")
        for q in range(4):
            sub(f"  Q{q + 1}", b == q)
        subs[name] = cuts
    emit(f"\n- рядок пораховано за {time.time() - t0:.0f} с; пікова RSS {rss_mb():.0f} MB")
    return {
        "key": r.key,
        "label": r.label,
        "primary": r.primary,
        "h": h,
        "pq": pq,
        "pr": pr,
        "p10": p10,
        "testable_a": testable_a,
        "testable_b": testable_b,
        "n": v.size,
    }


def classify(p_het: float | None, p_per: float | None) -> str:
    het = p_het is not None and p_het < 0.05
    per = p_per is not None and p_per < 0.05
    if het and per:
        return "прив'язаний до активів"
    if het:
        return "неоднорідність без стійкості"
    if per:
        return "стійкість без неоднорідності"
    return "шум"


def stage_report(root: Path) -> None:
    emit("# Перевірка по активах (IDEA-20261017-per-asset-recheck): результати")
    emit(
        f"R net на угоду (на ризик угоди, не на капітал), витрати — як у джерелах. IS < 2025-03-01 ≤ OOS. n ≥ {MIN_N} для (а), n_IS ≥ {MIN_N_IS} для (б); "
        f"перестановок {N_PERM}, бутстреп {N_BOOT}, seed {SEED}.\n"
    )
    rows = load_all(root)
    if not sanity(rows):
        emit("\n**Санітарна перевірка не пройдена — аналіз зупинено.**")
        return
    res = [analyse_row(r) for r in rows]
    prim = [x for x in res if x["primary"]]
    fam_a = [x for x in prim if x["testable_a"] and x["pq"]["p"] is not None]
    fam_b = [x for x in prim if x["testable_b"] and x["pr"]["diff_p"] is not None]
    adj_a = dict(zip([x["key"] for x in fam_a], holm([x["pq"]["p"] for x in fam_a]), strict=True)) if fam_a else {}
    adj_b = dict(zip([x["key"] for x in fam_b], holm([x["pr"]["diff_p"] for x in fam_b]), strict=True)) if fam_b else {}
    emit("\n## Зведення (основні рядки — Holm окремо для двох тестів; решта — сирі p, без вердикту)")
    emit(
        "| рядок | угод | активів (n ≥ 30) | I² | τ, R | p перестановки (Holm) | ранжовано | OOS: верхня чверть / решта | різниця [CI] | p стійкості (Holm) | ρ IS–OOS | висновок |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for x in res:
        h, pq, pr = x["h"], x["pq"], x["pr"]
        pa = adj_a.get(x["key"])
        pb = adj_b.get(x["key"])
        if x["primary"] and x["key"] in adj_a and x["key"] in adj_b:
            concl = classify(pa, pb)
        elif x["primary"]:
            concl = "◯ непридатний (замало активів)"
        else:
            concl = f"описово: {classify(pq['p'], pr['diff_p'])} (сирі p)"
        emit(
            f"| {x['key']} | {x['n']} | {h['k']} | {h['i2']:.1%} | {h['tau']:.3f} | {p_s(pq['p'])}{f' ({pa:.4f})' if pa is not None else ''} | {pr['k']} | "
            f"{pr['top_oos_mean']:+.3f} / {pr['rest_oos_mean']:+.3f} | {pr['diff']:+.3f} {ci_s(pr['diff_ci'])} | {p_s(pr['diff_p'])}{f' ({pb:.4f})' if pb is not None else ''} | "
            f"{pr['rho']:+.2f} ({pr['rho_n']}) | {concl} |"
        )
    emit(f"\n- пікова RSS {rss_mb():.0f} MB")


def stage_explore(root: Path) -> None:
    """Exploratory, chosen after the registered results (not part of the verdict rule): heterogeneity that survives
    (1) cluster-robust per-asset SEs with ISO-week clusters inside each asset, (2) dropping BTC and ETH, (3) dropping
    the five assets that contribute most to Q. Run on every row, not only the significant ones."""
    emit("# Перевірка по активах: дослідницькі перевірки неоднорідності (після результатів, поза правилом вердикту)")
    rows = load_all(root)
    emit(
        "\n| рядок | Q_pooled / df (зареєстроване) | Q з кластерами «тиждень у активі» / df, p, I², design effect | без BTC, ETH: Q / df, p перестановки | без 5 найбільших внесків у Q: Q / df, p(χ²); хто |\n"
        "|---|---|---|---|---|"
    )
    for r in rows:
        al = list(r.asset)
        h = heterogeneity(r.val, al, min_n=MIN_N)
        if h["k"] < 2:
            emit(f"| {r.key} | — | — | — | — |")
            continue
        weeks = list((r.day + 3) // 7)
        cr = cluster_robust_q(r.val, al, weeks, min_n=MIN_N)
        nm = ~np.isin(r.asset, MAJORS)
        hn = heterogeneity(r.val[nm], list(r.asset[nm]), min_n=MIN_N)
        pn = (
            permutation_q(r.val[nm], list(r.asset[nm]), list(r.day[nm]), min_n=MIN_N, n_perm=1000, seed=SEED)
            if hn["k"] >= 2
            else {"p": None}
        )
        # contributions n_i (m_i - m)^2 / s_w^2 to the pooled Q
        keys, counts = np.unique(r.asset.astype(str), return_counts=True)
        elig = [k for k, c in zip(keys, counts, strict=True) if c >= MIN_N]
        a_str = r.asset.astype(str)
        sel = np.isin(a_str, elig)
        m_all = r.val[sel].mean()
        contrib = sorted(((int((a_str == k).sum()) * (r.val[a_str == k].mean() - m_all) ** 2, k) for k in elig), reverse=True)
        drop = [k for _, k in contrib[:5]]
        keep = ~np.isin(a_str, drop)
        hd = heterogeneity(r.val[keep], list(a_str[keep]), min_n=MIN_N)
        emit(
            f"| {r.key} | {h['q']:.1f} / {h['df']} | {cr['q']:.1f} / {cr['df']}, p {p_s(cr['p'])}, I² {cr['i2']:.1%}, deff {cr['deff']:.2f} | "
            f"{hn['q']:.1f} / {hn['df']}, p {p_s(pn['p'])} | {hd['q']:.1f} / {hd['df']}, p {p_s(hd['p'])}; {', '.join(drop)} |"
        )
    emit(f"\n- пікова RSS {rss_mb():.0f} MB")


def stage_bounds(root: Path) -> None:
    """What the registered tests can exclude (added after the critic's review; no new hypothesis, no verdict rule):
    (1) Q-profile 95 % CI for tau over assets with n >= 30 (whole period, per-asset iid variances: overlapping trades
    of one asset inflate tau, so tau_hi is conservative); (2) implied true E[R] of the best quarter of assets,
    mu_RE(tau_hi) + 1.271 tau_hi, and the same with the upper 95 % bound of the pooled date-cluster mean in place of
    mu_RE; (3) MDE of the persistence difference at 80 % power, SE = CI width / 3.92, one-sided alpha 0.05 (the
    registered level before Holm) and 0.0125 (Holm's first step for 4 rows); (4) own OOS CI of the top-10 group."""
    up = top_fraction_uplift(0.25)
    emit("# Перевірка по активах: межі того, що тести здатні виключити (додано після рев'ю критика)")
    emit(
        f"τ — SD справжніх середніх активів (n ≥ {MIN_N}, весь період), Q-profile 95 % CI на власних дисперсіях активів "
        f"(перекриття угод однієї монети завищує τ, тож верхня межа консервативна). Верхня чверть ≈ μ + {up:.3f}·τ_верх "
        "(нормальний розподіл справжніх середніх). MDE різниці стійкості — потужність 80 %, SE = ширина CI / 3.92."
    )
    rows = load_all(root)
    emit(
        "\n| рядок | активів | τ DL (зареєстр.) | τ 95 % CI | μ_RE при τ_верх | верхня чверть: μ_RE + 1.27·τ_верх | "
        "те саме з верхньою межею CI зведеного μ | OOS верхня чверть (факт) [CI] | MDE різниці, α 0.05 / 0.0125 | "
        "верхні 10 OOS [CI] |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    for r in rows:
        al = list(r.asset)
        st = asset_stats(r.val, al, min_n=MIN_N)
        if len(st) < 2:
            emit(f"| {r.key} | {len(st)} | — | — | — | — | — | — | — | — |")
            continue
        h = heterogeneity(r.val, al, min_n=MIN_N)
        tc = tau_ci_qprofile([s.mean for s in st], [s.sd**2 / s.n for s in st])
        sel = np.isin(r.asset, [s.asset for s in st])
        _, _, _, pci = pooled(r.val[sel], r.day[sel], N_BOOT)
        top_q = tc["mu_hi_tau"] + up * tc["tau_hi"]
        top_q_hi = (pci[1] if pci else float("nan")) + up * tc["tau_hi"]
        common = {"min_n_is": MIN_N_IS, "min_n_oos_rho": MIN_N_OOS_RHO, "seed": SEED}
        pr = persistence(r.val, al, list(r.day), list(r.is_), top_frac=0.25, n_boot=N_BOOT, **common)
        p10 = persistence(r.val, al, list(r.day), list(r.is_), top_n=10, n_boot=5000, **common)
        ci = pr["diff_ci"]
        if ci is not None and pr["n_top_oos"] > 0 and pr["n_rest_oos"] > 0:
            se = (ci[1] - ci[0]) / (2 * 1.959963984540054)
            mde_s = f"{mde(se):.3f} / {mde(se, alpha=0.0125):.3f}"
        else:
            mde_s = "—"
        emit(
            f"| {r.key} | {tc['k']} | {h['tau']:.3f} | [{tc['tau_lo']:.3f}; {tc['tau_hi']:.3f}] | {tc['mu_hi_tau']:+.3f} | {top_q:+.3f} | "
            f"{top_q_hi:+.3f} | {pr['top_oos_mean']:+.3f} {ci_s(pr['top_oos_ci'])} | {mde_s} | "
            f"{p10['top_oos_mean']:+.3f} {ci_s(p10['top_oos_ci'])} ({p10['n_top_oos']} угод) |"
        )
    emit(f"\n- пікова RSS {rss_mb():.0f} MB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("prereg", "sanity", "report", "explore", "bounds"), required=True)
    ap.add_argument("--root", type=Path, default=Path.home() / ".cache/tradedesk-agents")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = args.out.open("w")
    try:
        if args.stage == "prereg":
            stage_prereg()
        elif args.stage == "sanity":
            sanity(load_all(args.root))
        elif args.stage == "explore":
            stage_explore(args.root)
        elif args.stage == "bounds":
            stage_bounds(args.root)
        else:
            stage_report(args.root)
    finally:
        if OUT["fh"] is not None:
            OUT["fh"].close()


if __name__ == "__main__":
    main()
