#!/usr/bin/env python3
"""S39: signals of one 4h bar as one bet (IDEA-20261030-demo-setups-depth, item «S39, сигнали однієї свічки як одна
ставка»; registration «Попередня реєстрація — S39, сигнали однієї свічки»).

    uv run python scripts/verify_s39_bar_cluster.py --stage 0           # counts, distribution, SDs and MDE gate; no mean R
    uv run python scripts/verify_s39_bar_cluster.py --stage run         # registered tables and verdict (also <out-dir>/run.txt)
    uv run python scripts/verify_s39_bar_cluster.py --stage run --stub  # code path with synthetic R (not a result)

No network: the S39 passport cache (``setup_passport/S39/cands``) and the turnover lists of ``course_s14_ema`` /
``funding_crowding``, all filled earlier through the runtime providers by ``verify_setup_passport.py``. The verdict
sample is the passport's (top-250 by the previous day's turnover and >= $1M, demo selection, one trade per coin,
rule B, R on the trade's risk after fees, 2021-01-01 .. 2026-09-23). The cap of 2 entries per signal bar copies
``demo_trader._select_and_enter`` (read-only): candidates sorted by higher score, then higher R:R after fees
(``verify_setup_passport.demo_rank``; the 24h turnover breaks exact ties).
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_setup_passport as vsp  # noqa: E402
from common_helpers import rss_mb  # noqa: E402

from tradedesk.analytics import bar_cluster as bc  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402

SETUP = "S39"
CAP = 2  # registered: the S38 demo limit (demo_trader.BAR_CAP), proposed for S39
CAPS = (1, 2, 3)  # sensitivity grid (cap 2 primary)
GATE = 0.10  # MDE80 of the cap effect on E[R] per trade, in R; above it the verdict is ◯
N_RANDOM_ORDERS = 200  # within-bar random order instead of the demo's (score, R:R)
N_TOP_WEEKS = 10  # «without the top contributors»: weeks with the largest |sum R| of the trades the cap removes
START = vsp.PASSPORT_MS
END = vsp.END["4h"]
LINES: list[str] = []


def emit(s: str = "") -> None:
    vsp.emit(s)
    LINES.append(s)


def f3(x: float) -> str:
    return "—" if x is None or not math.isfinite(x) else f"{x:+.3f}"


def load(a: argparse.Namespace) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
    """Passport rows of S39 (4h), the demo list before the chain and the verdict sample V (uncapped chain)."""
    T = vsp.load_rows(a.passport_cache / SETUP)
    elig = vsp.s1.eligible_syms(a.data_dir)
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig)
    top = vsp.top_n_days(qv, vsp.top_n_of(SETUP))
    D = vsp.demo_list(T, SETUP, top) & (T["tf"] == "4h") & (T["t_in"] < END)
    V = vsp.chain(T, D)
    return T, D, V


def periods(T: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {"2021+": np.ones(T["t_in"].size, dtype=bool), "IS": T["t_in"] < vsp.OOS_MS, "OOS": T["t_in"] >= vsp.OOS_MS}


def capped(T: dict[str, np.ndarray], D: np.ndarray, cap: int, rank: np.ndarray | None = None) -> np.ndarray:
    """Chain of the demo list with at most ``cap`` entries per signal bar; ``rank`` None = the demo's order."""
    if rank is None:
        return vsp.chain(T, D, cap)
    idx = np.flatnonzero(D)
    keep = sp.busy_chain_capped([str(s) for s in T["sym"][idx]], T["t_in"][idx], T["B_x"][idx], rank[idx], cap)
    out = np.zeros(D.size, dtype=bool)
    out[idx[keep]] = True
    return out


def size_table(T: dict[str, np.ndarray], m: np.ndarray, label: str) -> None:
    h = bc.size_histogram(T["t_in"][m])
    bars = sum(h.values())
    tr = {k: k * v for k, v in h.items()}
    n = sum(tr.values())
    g = {1: h.get(1, 0), 2: h.get(2, 0), 3: sum(v for k, v in h.items() if k >= 3)}
    gt = {1: tr.get(1, 0), 2: tr.get(2, 0), 3: sum(v for k, v in tr.items() if k >= 3)}
    emit(
        f"| {label} | {n} | {bars} | {g[1]} / {g[2]} / {g[3]} | {gt[1]} / {gt[2]} / {gt[3]} | "
        f"{(gt[3] / n if n else math.nan):.1%} | {max(h) if h else 0} |"
    )


def stage0(a: argparse.Namespace, T: dict[str, np.ndarray], D: np.ndarray, V: np.ndarray) -> None:
    P = periods(T)
    W = T["week"]
    emit(f"демо-список (до ланцюга) {int(D.sum())}; вибірка V (ланцюг без ліміту) {int(V.sum())}")
    emit(
        f"звірка з паспортом (2026-10-30: 2021+ 2840, OOS 885, IS 1955): 2021+ {int(V.sum())}, "
        f"OOS {int((V & P['OOS']).sum())}, IS {int((V & P['IS']).sum())}"
    )
    bad = int((V & ~np.isfinite(T["B"])).sum())
    emit(f"угод V з нечисловим R: {bad}; ТФ {sorted(set(T['tf'][V].tolist()))}")
    emit("\n## Розподіл: угод V на 4h-свічку (групи 1 / 2 / ≥ 3)")
    emit("| вибірка | угод | свічок | свічок 1 / 2 / ≥3 | угод 1 / 2 / ≥3 | частка угод у ≥3 | макс. на свічку |")
    emit("|---|---|---|---|---|---|---|")
    for k, pm in P.items():
        size_table(T, V & pm, f"V {k}")
    for k, pm in P.items():
        size_table(T, D & pm, f"демо-список до ланцюга {k} (описово)")
    vi = np.flatnonzero(V)
    gr = np.zeros(T["t_in"].size, dtype=np.int64)
    gr[vi] = bc.size_group(bc.bar_sizes(T["t_in"][vi]))
    h = bc.size_histogram(T["t_in"][V])
    emit("свічок за кількістю угод V 2021+: " + ", ".join(f"{k}: {v}" for k, v in h.items()))
    emit("\n## Кластери (тижні, як у паспорті: t_in // 7 діб)")
    for k, pm in P.items():
        m = V & pm
        wk, w3 = np.unique(W[m]), np.unique(W[m & (gr == 3)])
        per_wk = np.sort(np.unique(W[m], return_counts=True)[1])[::-1]
        emit(
            f"{k}: тижнів з угодами {wk.size}; тижнів зі свічкою ≥ 3 угод {w3.size}; угод на тиждень з угодами {m.sum() / max(wk.size, 1):.1f}; "
            f"частка угод у 10 найбільших тижнях {per_wk[:10].sum() / max(m.sum(), 1):.1%} (найбільший {per_wk[0] if per_wk.size else 0})"
        )
    emit("\n## Ліміт на свічку (відбір як на демо: бал, потім R:R) — лише кількості")
    emit("| ліміт | вибірка | угод з лімітом | прибрано | додано (монета не зайнята) | прибрано / тиждень періоду |")
    emit("|---|---|---|---|---|---|")
    for cap in CAPS:
        C = capped(T, D, cap)
        rem, add = bc.chain_change(V, C)
        for k, pm in P.items():
            lo, hi = (START, vsp.OOS_MS) if k == "IS" else (vsp.OOS_MS, END) if k == "OOS" else (START, END)
            wb = bc.week_buckets(T["t_in"][rem & pm], lo, hi, sp.WEEK_MS)
            emit(f"| {cap} | {k} | {int((C & pm).sum())} | {int((rem & pm).sum())} | {int((add & pm).sum())} | {wb.mean():.2f} |")
    emit(
        f"свічок, де порядок демо (бал, R:R) має рівність на межі ліміту {CAP}: "
        f"{bc.cap_boundary_ties(T['t_in'][vi], T['score'][vi], T['rr'][vi], CAP)} (там вирішує оборот 24 год)"
    )
    sc, sn = np.unique(T["score"][V], return_counts=True)
    emit("бали V: " + ", ".join(f"{int(s)}: {int(n)}" for s, n in zip(sc, sn, strict=True)) + " (один бал: порядок демо вирішує R:R)")
    emit("\n## Розкид R (SD, без середніх) і ворота MDE₈₀")
    emit("| вибірка | SD R усі | SD R групи 1 / 2 / ≥3 | SE Δ ліміту 2 (тижні) | MDE₈₀ Δ | SE (≥3 − 1) | MDE₈₀ (≥3 − 1) |")
    emit("|---|---|---|---|---|---|---|")
    C2 = capped(T, D, CAP)
    gate: dict[str, float] = {}
    for k, pm in P.items():
        m = V & pm
        sds = [float(np.std(T["B"][m & (gr == g)], ddof=1)) if (m & (gr == g)).sum() > 1 else math.nan for g in (1, 2, 3)]
        se_d = bc.boot_se_diff(T["B"], C2 & pm, m, W, n_boot=a.n_boot, seed=vsp.SEED)
        se_g = bc.boot_se_diff(T["B"], m & (gr == 3), m & (gr == 1), W, n_boot=a.n_boot, seed=vsp.SEED)
        gate[k] = bc.mde80(se_d)
        emit(
            f"| {k} | {np.std(T['B'][m], ddof=1):.3f} | {sds[0]:.3f} / {sds[1]:.3f} / {sds[2]:.3f} | {se_d:.4f} | "
            f"{gate[k]:.3f} | {se_g:.3f} | {bc.mde80(se_g):.3f} |"
        )
    ok = math.isfinite(gate["2021+"]) and gate["2021+"] <= GATE
    emit(
        f"\nворота (2021+, Δ ліміту 2): MDE₈₀ {gate['2021+']:.3f}R {'≤' if ok else '>'} {GATE:.2f}R → {'пройдено' if ok else 'НЕ пройдено (вердикт ◯)'}"
    )


def group_row(T: dict[str, np.ndarray], m: np.ndarray, label: str) -> str:
    r = vsp.ci(T, m)
    sd = float(np.std(T["B"][m], ddof=1)) if m.sum() > 1 else math.nan
    return f"| {label} | {int(r['n'])} | {int(r['k'])} | {f3(r['m'])} | [{f3(r['lo'])}; {f3(r['hi'])}] | {sd:.3f} |"


def delta(T: dict[str, np.ndarray], key: str, a_: np.ndarray, b_: np.ndarray, n_boot: int) -> dict[str, float]:
    return sp.diff_ci(T[key], a_, b_, T["week"], n_boot=n_boot, seed=vsp.SEED)


def stage_run(a: argparse.Namespace, T: dict[str, np.ndarray], D: np.ndarray, V: np.ndarray) -> None:
    P = periods(T)
    W = T["week"]
    vi = np.flatnonzero(V)
    gr = np.zeros(T["t_in"].size, dtype=np.int64)
    gr[vi] = bc.size_group(bc.bar_sizes(T["t_in"][vi]))
    emit("\n## 1. Середній R за кількістю угод V на свічці (CI кластерами за тижнем)")
    emit("| група | n | тижнів | E[R] net | 95 % CI | SD |")
    emit("|---|---|---|---|---|---|")
    for k, pm in P.items():
        for g, lab in ((1, "1"), (2, "2"), (3, "≥ 3")):
            emit(group_row(T, V & pm & (gr == g), f"{k}, {lab}"))
        emit(group_row(T, V & pm, f"{k}, усі"))
    emit("\nконтрасти груп (різниця середніх угод, кластери — тижні; описово, ворота не мають):")
    for k, pm in P.items():
        for g in (3, 2):
            d = delta(T, "B", V & pm & (gr == g), V & pm & (gr == 1), a.n_boot)
            emit(f"- {k}: ({g if g < 3 else '≥3'}) − (1) {f3(d['d'])} [{f3(d['lo'])}; {f3(d['hi'])}] p {d['p']:.3f}")
    emit("\n## 2. «Одна ставка»: ефект дизайну середнього R V (SE кластерами / SE i.i.d.)²")
    for k, pm in P.items():
        m = V & pm
        v, n, sd = T["B"][m], int(m.sum()), float(np.std(T["B"][m], ddof=1))
        se_bar = bc.boot_se_mean(v, T["t_in"][m], n_boot=a.n_boot, seed=vsp.SEED)
        se_wk = bc.boot_se_mean(v, W[m], n_boot=a.n_boot, seed=vsp.SEED)
        db, dw = bc.design_effect(se_bar, sd, n), bc.design_effect(se_wk, sd, n)
        emit(f"- {k}: n {n}; свічка: deff {db:.2f} (ефективне n {n / db:.0f}); тиждень: deff {dw:.2f} (ефективне n {n / dw:.0f})")
    emit("\n## 3. Ефект ліміту на свічку на E[R] угоди: Δ = E[R] з лімітом − E[R] без (парний тижневий бутстреп)")
    emit("| ліміт | порядок | вибірка | n з лімітом | Δ | 95 % CI | p | MDE₈₀ |")
    emit("|---|---|---|---|---|---|---|---|")
    res: dict[tuple[int, str], dict[str, float]] = {}
    for cap in CAPS:
        C = capped(T, D, cap)
        for k, pm in P.items():
            d = delta(T, "B", C & pm, V & pm, a.n_boot)
            mde = bc.mde80(bc.boot_se_diff(T["B"], C & pm, V & pm, W, n_boot=a.n_boot, seed=vsp.SEED))
            res[(cap, k)] = d
            emit(
                f"| {cap} | демо | {k} | {int((C & pm).sum())} | {f3(d['d'])} | [{f3(d['lo'])}; {f3(d['hi'])}] | {d['p']:.3f} | {mde:.3f} |"
            )
    C2 = capped(T, D, CAP)
    rem, add = bc.chain_change(V, C2)
    emit("\nприбрані лімітом 2 проти залишених у тих самих свічках (≥ 3 угод), описово:")
    kept3 = C2 & (gr == 3)
    for k, pm in P.items():
        d = delta(T, "B", rem & pm, kept3 & pm, a.n_boot)
        emit(
            f"- {k}: прибрані {int((rem & pm).sum())}, залишені {int((kept3 & pm).sum())}: різниця {f3(d['d'])} [{f3(d['lo'])}; {f3(d['hi'])}]"
        )
    rng = np.random.default_rng(vsp.SEED)
    rd = {k: [] for k in P}
    for _ in range(N_RANDOM_ORDERS):
        C = capped(T, D, CAP, rng.random(T["t_in"].size))
        for k, pm in P.items():
            rd[k].append(float(T["B"][C & pm].mean() - T["B"][V & pm].mean()))
    emit(f"\nліміт 2, випадковий порядок у свічці ({N_RANDOM_ORDERS} разів), Δ 5 / 50 / 95 %:")
    for k in P:
        q = np.quantile(rd[k], (0.05, 0.5, 0.95))
        emit(f"- {k}: {f3(q[0])} / {f3(q[1])} / {f3(q[2])}  (демо-порядок {f3(res[(CAP, k)]['d'])})")
    emit("\n## 4. Стійкість Δ ліміту 2 (2021+)")
    T["B_stress"] = T["B"] - np.nan_to_num(T["cost"], nan=0.0)
    d = delta(T, "B_stress", C2, V, a.n_boot)
    emit(f"- подвійні комісії (R − cost): Δ {f3(d['d'])} [{f3(d['lo'])}; {f3(d['hi'])}]")
    wk_sum: dict[int, float] = {}
    for w, r in zip(W[rem], T["B"][rem], strict=True):
        wk_sum[int(w)] = wk_sum.get(int(w), 0.0) + float(r)
    top = [w for w, _ in sorted(wk_sum.items(), key=lambda x: -abs(x[1]))[:N_TOP_WEEKS]]
    keep = ~np.isin(W, top)
    d = delta(T, "B", C2 & keep, V & keep, a.n_boot)
    emit(f"- без {N_TOP_WEEKS} тижнів з найбільшим |Σ R| прибраних угод: Δ {f3(d['d'])} [{f3(d['lo'])}; {f3(d['hi'])}]")
    emit("\n## 5. Скільки місць вивільняє ліміт 2")
    for k, pm in P.items():
        lo, hi = (START, vsp.OOS_MS) if k == "IS" else (vsp.OOS_MS, END) if k == "OOS" else (START, END)
        wr = bc.week_buckets(T["t_in"][rem & pm], lo, hi, sp.WEEK_MS)
        wa = bc.week_buckets(T["t_in"][add & pm], lo, hi, sp.WEEK_MS)
        net = wr - wa
        se = float(np.std(net, ddof=1) / math.sqrt(net.size))
        conc = (bc.held_ms(T["t_in"][rem & pm], T["B_x"][rem & pm]) - bc.held_ms(T["t_in"][add & pm], T["B_x"][add & pm])) / (hi - lo)
        emit(
            f"- {k}: входів прибрано {wr.mean():.2f} / тиждень, додано {wa.mean():.2f}, нетто {net.mean():.2f} "
            f"[{net.mean() - 1.96 * se:.2f}; {net.mean() + 1.96 * se:.2f}]; тижнів, де ліміт спрацював {int((wr > 0).sum())} з {wr.size}; "
            f"середньо вивільнених одночасних позицій {conc:.2f}; макс. прибрано за тиждень {int(wr.max())}"
        )
    emit("\n## 6. За монетами (5a), угоди V у свічках з ≥ 3 угодами")
    vsp.per_asset(T, V & (gr == 3), P["IS"], P["OOS"])
    emit("\n## Вердикт (правила реєстрації; Δ ліміту 2, 2021+)")
    se = bc.boot_se_diff(T["B"], C2, V, W, n_boot=a.n_boot, seed=vsp.SEED)
    mde = bc.mde80(se)
    d, di, do = res[(CAP, "2021+")], res[(CAP, "IS")]["d"], res[(CAP, "OOS")]["d"]
    if not (math.isfinite(mde) and mde <= GATE):
        v = f"◯ — MDE₈₀ {mde:.3f}R > {GATE}R"
    elif d["lo"] > 0 and di > 0 and do > 0:
        v = "✓ — ліміт 2 підвищує E[R] угоди (CI > 0, IS і OOS > 0)"
    elif d["hi"] < 0 and di < 0 and do < 0:
        v = "✗ — ліміт 2 знижує E[R] угоди (CI < 0, IS і OOS < 0)"
    elif d["lo"] > -GATE and d["hi"] < GATE:
        v = f"≈ нейтральний — |Δ| ≥ {GATE}R виключено; рішення про ліміт — за місцями (розділ 5)"
    else:
        v = "◯ — CI містить 0 і ширший за ±0.10R або IS / OOS різного знака"
    emit(f"Δ {f3(d['d'])} [{f3(d['lo'])}; {f3(d['hi'])}], IS {f3(di)}, OOS {f3(do)}, MDE₈₀ {mde:.3f} → {v}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("0", "run"), required=True)
    ap.add_argument("--stub", action="store_true", help="run: replace R by seeded synthetic noise (code path check)")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--out-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s39-bar-cluster")
    ap.add_argument("--n-boot", type=int, default=vsp.N_BOOT)
    a = ap.parse_args()
    t0 = time.time()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    name = "stage0.txt" if a.stage == "0" else ("run_stub.txt" if a.stub else "run.txt")
    with (a.out_dir / name).open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"# verify_s39_bar_cluster --stage {a.stage}{' --stub' if a.stub else ''} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        files = sorted((a.passport_cache / SETUP / "cands").glob("*.json"))
        emit(f"файлів кандидатів S39: {len(files)}")
        if not files:
            emit("кешу паспорта S39 немає — стоп")
            return 1
        T, D, V = load(a)
        emit(f"рядків {T['t_in'].size}; {time.time() - t0:.1f} с; rss {rss_mb():.0f} MB")
        if a.stub:
            rng = np.random.default_rng(1)
            T["B"] = rng.normal(0.0, 1.5, T["t_in"].size)
            emit("STUB: R замінено синтетичним шумом N(0, 1.5) — це НЕ результат")
        if a.stage == "0":
            stage0(a, T, D, V)
        else:
            stage_run(a, T, D, V)
        emit(f"\nготово: {time.time() - t0:.1f} с; peak rss {rss_mb():.0f} MB; вивід {a.out_dir / name}")
        vsp.OUT["fh"] = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
