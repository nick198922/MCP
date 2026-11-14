"""Playbook rule P12 (iron condor with 25-delta short legs) on Deribit BTC marks, Bybit costs.

IDEA-20261114-options-playbook, item 4.3; pre-registration: the card's "4.3. P12 ... попередня реєстрація".
No network: the put-skew-v2 cache (``--src-dir``, read only): ``snapshots_v2.json`` (daily 09:00 surfaces),
``delivery_btc_usd.json``, ``dvol_BTC.json``, loaded by ``verify_p1_condor_dvol.load``. The construction and P&L are
those of P1' (`condor_dvol`) with |delta| 0.25 short legs; the tail gate and the verdict rule are `condor_25d`.

    uv run python scripts/verify_p12_condor_25d.py --stage coverage   # stage 0: coverage and sanity, no P&L
    uv run python scripts/verify_p12_condor_25d.py --stage full --out ~/.cache/tradedesk-agents/p12_condor_25d/full.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_option_modules as vom  # noqa: E402  (scripts/ is not a package)
import verify_p1_condor_dvol as vp1  # noqa: E402
import verify_p3_trend_spread as vp3  # noqa: E402
from common_helpers import ymd_ms  # noqa: E402

from tradedesk.analytics.condor_25d import block_clusters, parametric_tail, tail_gate, verdict  # noqa: E402
from tradedesk.analytics.dvol_filter import cvar, max_drawdown, mean_without_top  # noqa: E402
from tradedesk.analytics.option_condor import block_bootstrap_mean, exec_legs  # noqa: E402
from tradedesk.analytics.trend_spread import cluster_bootstrap, holm_adjust  # noqa: E402

OOS = vp1.OOS
COVERAGE_MIN = 0.60
THRESH = 0.80  # P2 condition (the playbook's candidate condition for P12)
# Pre-registered "edge worth the effort" for the ✗ rule: a quarter of the median max profit (credit / width) of the
# base sample, fixed from stage 0 before any P&L (see the card): credit / width median 0.190 -> 0.05 R.
EDGE_R: float | None = 0.05
N_BOOT = vp3.N_BOOT
SEED = vp3.SEED
MIN_LOSSES = 5
# Raw two-sided p of the earlier rules of the item-4 batch: P3 from its report (H2: Holm 0.038 at rank 1 of 3 -> raw
# 0.038 / 3); P1' from its report (all three bootstrap p = 0.000, degenerate: no losing trade in the sample).
PREV_RAW_P = {"P3-H1": 0.042, "P3-H2": 0.038 / 3, "P3-H3": 0.842, "P1-H1": 0.0, "P1-H2": 0.0, "P1-H3": 0.0}
BASE: dict[str, Any] = {**vp1.BASE, "short_delta": 0.25, "wing": 0.03, "exit": "expiry", "tp": 0.80}
H3_TP = 0.80
# From a Saturday 09:00 the next Friday expiry is 6.96 d away, outside the 7-24 d window, so "target 7 d" with the
# base window repeats the base (stage 0); the 7-day variant therefore opens the window to 5 d (set before any P&L).
DTE_VARIANTS: list[tuple[str, dict[str, Any]]] = [
    ("ціль DTE 7 д (вікно 5–24 д)", {**BASE, "target": 7.0, "lo": 5.0}),
    ("ціль DTE 21 д", {**BASE, "target": 21.0}),
]

log = vp3.log
rr, ci_r, pc = vp3.rr, vp3.ci_r, vp3.pc


# ----------------------------------------------------------------------------- samples


def saturdays(D: dict[str, Any], P: dict[str, Any]) -> list[int]:
    return vp3.entry_days(D, P["weekday"])


def blocks(D: dict[str, Any], P: dict[str, Any]) -> dict[int, int]:
    """Clusters of the unconditional series: 4-week calendar blocks from the first entry day."""
    days = saturdays(D, P)
    return block_clusters(days, start_ms=days[0], weeks=4)


def trades(D: dict[str, Any], P: dict[str, Any], days: list[int]) -> list[dict[str, Any]]:
    built, _ = vp1.build_all(D, days, P)
    return [vp1.outcome(D, b, P) for b in built]


def hi_days(D: dict[str, Any], P: dict[str, Any], thr: float) -> tuple[list[int], dict[int, float]]:
    pcts = vp1.entries(D, P)
    return sorted(d for d, p in pcts.items() if p >= thr), pcts


# ----------------------------------------------------------------------------- stage 0


def coverage(L: list[str], D: dict[str, Any], t0: float) -> list[dict[str, Any]]:
    P = BASE
    days = saturdays(D, P)
    h2, pcts = hi_days(D, P, THRESH)
    L.append(f"## Стадія 0: покриття (поріг {COVERAGE_MIN:.0%}; P&L не рахується)\n")
    L.append(f"- суботи {ymd_ms(days[0])} … {ymd_ms(days[-1])}: {len(days)}; з перцентилем DVOL: {len(pcts)} (від {ymd_ms(min(pcts))})")
    L.append("| вибірка / конструкція | субот | план побудовано | покриття | причини пропуску |")
    L.append("|---|---|---|---|---|")
    base: list[dict[str, Any]] = []
    rows: list[tuple[str, dict[str, Any], list[int]]] = [
        ("**H1: усі суботи, Δ 0.25 × крило 3 %**", P, days),
        ("**H2: перцентиль ≥ 0.8 (умова P2)**", P, h2),
        ("перцентиль ≥ 0.7", P, [d for d, p in pcts.items() if p >= 0.7]),
        ("перцентиль ≥ 0.9", P, [d for d, p in pcts.items() if p >= 0.9]),
        ("перцентиль < 0.8", P, [d for d, p in pcts.items() if p < THRESH]),
        *(
            (f"Δ {sd:.2f} × крило {w:.0%}", {**P, "short_delta": sd, "wing": w}, days)
            for sd in (0.20, 0.25, 0.30)
            for w in (0.02, 0.03, 0.05)
            if (sd, w) != (0.25, 0.03)
        ),
        ("Δ 0.15 × крило 3 % (P1′, для парної різниці)", {**P, "short_delta": 0.15}, days),
        *((f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, days) for h in (0.0, 0.03)),
        ("модель «сторона»", {**P, "model": "side"}, days),
        *((name, Pv, days) for name, Pv in DTE_VARIANTS),
        ("вхід у п'ятницю", {**P, "weekday": 4}, saturdays(D, {**P, "weekday": 4})),
    ]
    for name, Pv, ds in rows:
        built, why = vp1.build_all(D, sorted(ds), Pv)
        cov = len(built) / len(ds) if ds else 0.0
        L.append(f"| {name} | {len(ds)} | {len(built)} | **{cov:.1%}** | {dict(why.most_common())} |")
        if Pv is P and ds is days:
            base = built
    log("coverage table done", t0)
    by_year: Counter[str] = Counter(ymd_ms(d)[:4] for d in days)
    ok_year: Counter[str] = Counter(ymd_ms(b["day"])[:4] for b in base)
    L.append("\n- H1 по роках (суботи / побудовано): " + ", ".join(f"{y}: {by_year[y]}/{ok_year[y]}" for y in sorted(by_year)))
    cl = blocks(D, P)
    L.append(
        f"- H1: 4-тижневих блоків-кластерів {len(set(cl.values()))}, серед побудованих {len({cl[b['day']] for b in base})}; "
        f"IS / OOS побудованих {sum(b['day'] < OOS for b in base)} / {sum(b['day'] >= OOS for b in base)}"
    )
    built2, _ = vp1.build_all(D, h2, P)
    ep2 = vp1.state_episodes(pcts, THRESH, both=False)
    L.append(
        f"- H2: побудовано {len(built2)} / {len(h2)}, серій ≥ 0.8 серед побудованих {len({ep2[b['day']] for b in built2})}; "
        f"IS / OOS {sum(b['day'] < OOS for b in built2)} / {sum(b['day'] >= OOS for b in built2)}\n"
    )
    return base


def sanity(L: list[str], D: dict[str, Any], base: list[dict[str, Any]]) -> float:
    L.append("## Перевірки до прогону (без P&L)\n")
    ds = sorted(abs(b["plan"]["delta"][k]) for b in base for k in ("sp", "sc"))
    dw = sorted(abs(b["plan"]["delta"][k]) for b in base for k in ("lp", "lc"))
    L.append(
        f"- фактичні |Δ| проданих ніг: медіана {statistics.median(ds):.3f} [p10 {ds[len(ds) // 10]:.3f}; p90 {ds[9 * len(ds) // 10]:.3f}] (ціль 0.25); "
        f"крил: медіана {statistics.median(dw):.3f}"
    )
    otm = sorted(
        x
        for b in base
        for x in (
            (b["plan"]["spot"] - b["plan"]["K"]["sp"]) / b["plan"]["spot"],
            (b["plan"]["K"]["sc"] - b["plan"]["spot"]) / b["plan"]["spot"],
        )
    )
    L.append(
        f"- відстань проданих страйків від спота: медіана {statistics.median(otm):.2%} [p10 {otm[len(otm) // 10]:.2%}; p90 {otm[9 * len(otm) // 10]:.2%}]"
    )
    wing = sorted(
        x
        for b in base
        for x in (
            (b["plan"]["K"]["sp"] - b["plan"]["K"]["lp"]) / b["plan"]["spot"],
            (b["plan"]["K"]["lc"] - b["plan"]["K"]["sc"]) / b["plan"]["spot"],
        )
    )
    L.append(f"- крило / спот: медіана {statistics.median(wing):.2%}, мін {wing[0]:.2%}, макс {wing[-1]:.2%} (ціль 3 %, допуск ±1.5 %)")
    fr = sorted(b["credit"] / b["plan"]["width"] for b in base)
    med = statistics.median(fr)
    L.append(
        f"- кредит (після комісій входу) / ширина: медіана {med:.3f} [p10 {fr[len(fr) // 10]:.3f}; p90 {fr[9 * len(fr) // 10]:.3f}], "
        f"мін {fr[0]:.3f}, макс {fr[-1]:.3f} (має бути в (0; 1)) → EDGE_R = 0.25 × медіана = {round(0.25 * med, 2):.2f} R"
    )
    L.append(
        f"- DTE: медіана {statistics.median(b['dte'] for b in base):.2f} д, мін {min(b['dte'] for b in base):.2f}, макс {max(b['dte'] for b in base):.2f}"
    )
    side_fb = sum(exec_legs(b["plan"], "side")[1] for b in base)
    L.append(f"- модель «сторона»: ніг без принта своєї сторони {side_fb} з {4 * len(base)} (замінено на mark ± 1.5 в.п.)")
    fr_days = vom.fridays(vom.FIRST, vom.LAST)
    ok = sum(1 for f in fr_days if vom.plan(D, f, "h1")[0] is not None)
    L.append(f"- звірка з відомим числом: покриття кондора 10Δ / ±15 % (options-modules H1) = {ok} / {len(fr_days)} (у звіті 179 / 301)\n")
    return round(0.25 * med, 2)


# ----------------------------------------------------------------------------- full

HDR = vp1.HDR


def analyse(
    L: list[str], name: str, tr: list[dict[str, Any]], ep: dict[int, int], elig: int, loss_ref: list[float] | None
) -> dict[str, Any]:
    """Table, block bootstrap, non-overlapping halves, tail gate and parametric tail of one hypothesis."""
    tr = sorted(tr, key=lambda t: t["day"])
    s = vp3.summary(tr, ep)
    L += HDR
    L.append(vp1.table_row(f"{name}: усі", tr, ep, elig))
    L.append(vp1.table_row("IS (до 2024)", [t for t in tr if t["day"] < OOS], ep, elig))
    L.append(vp1.table_row("OOS (2024+)", [t for t in tr if t["day"] >= OOS], ep, elig))
    for y in sorted({ymd_ms(t["day"])[:4] for t in tr}):
        L.append(vp1.table_row(f"рік {y}", [t for t in tr if ymd_ms(t["day"])[:4] == y], ep, elig))
    rs = [t["r"] for t in tr]
    bb = block_bootstrap_mean(rs, block=4, n_boot=N_BOOT, seed=SEED)
    lo, hi = min(s["ci"][0], bb["ci"][0]), max(s["ci"][1], bb["ci"][1])
    L.append(f"\n- блоковий бутстреп (тижні, блок 4): {rr(bb['mean'])} {ci_r(bb['ci'])}, p {bb['p']:.3f}")
    L.append(f"- CI для вердикту (об'єднання кластерного й блокового): [{lo:+.3f}; {hi:+.3f}]")
    halves = []
    for k in (0, 1):
        sub = rs[k::2]
        bk = block_bootstrap_mean(sub, block=2, n_boot=N_BOOT, seed=SEED)
        halves.append((sum(1 for r in sub if r <= 0), len(sub)))
        L.append(f"- неперекривна підвибірка {k + 1} (кожен другий вхід): n = {len(sub)}, {rr(bk.get('mean'))} {ci_r(bk.get('ci'))}")
    g = tail_gate(rs, [ep[t["day"]] for t in tr], min_losses=MIN_LOSSES)
    L.append(
        f"- ворота хвоста: збиткових угод (R ≤ 0) {g['n_loss']} з {g['n']} у {g['n_loss_clusters']} з {g['n_clusters']} кластерів; "
        f"P(ресемпл без збиткового кластера) {g['p_no_loss_resample']:.3f} → **{'пройдено' if g['ok'] else 'НЕ пройдено (✓ / ≈ неможливі)'}**"
    )
    # registered: the more pessimistic of the two non-overlapping halves
    tails = [parametric_tail(rs, k_eff=k, n_eff=n, loss_ref=loss_ref) for k, n in halves]
    pt = max(tails, key=lambda x: x["q_up"])
    L.append(
        f"- параметричний хвіст: частка збиткових {pt['q_obs']:.1%}, 95 % верхня межа (Клоппер — Пірсон, неперекривні) {pt['q_up']:.1%}; "
        f"середній виграш {rr(pt['win_mean'])}, середній збиток {rr(pt['loss_mean'])} ({'вибірка' if pt['loss_from'] == 'sample' else 'еталон H1'}) "
        f"→ стрес-середнє {rr(pt['stress_mean'])}"
    )
    L.append(
        f"- хвіст: найгірший R {min(rs):+.3f}, CVaR 5 % {cvar(rs):+.3f}, найбільша просадка суми R {max_drawdown(rs):.2f}; "
        f"без 5 найкращих {mean_without_top(rs, 5):+.3f}; MDE {s['mde']:.3f} R; виходи {dict(Counter(t['exit'] for t in tr))}\n"
    )
    return {
        **s,
        "lo": lo,
        "hi": hi,
        "block": bb,
        "gate": g,
        "ptail": pt,
        "is": vp3.summary([t for t in tr if t["day"] < OOS], ep)["mean"],
        "oos": vp3.summary([t for t in tr if t["day"] >= OOS], ep)["mean"],
    }


def full(L: list[str], D: dict[str, Any], cache: Path, t0: float, edge: float) -> None:
    P = BASE
    days = saturdays(D, P)
    cl = blocks(D, P)
    h1 = trades(D, P, days)
    (cache / "trades_h1.json").write_text(json.dumps(h1, default=str))
    L.append("## H1: кондор 25Δ × крило 3 %, щосуботи без умови, до експірації\n")
    a1 = analyse(L, "H1", h1, cl, len(days), None)
    loss_ref = [t["r"] for t in h1 if t["r"] <= 0]
    log("H1 done", t0)

    h2d, pcts = hi_days(D, P, THRESH)
    ep2 = vp1.state_episodes(pcts, THRESH, both=False)
    h2 = trades(D, P, h2d)
    (cache / "trades_h2.json").write_text(json.dumps(h2, default=str))
    L.append("## H2: той самий кондор за умови P2 (перцентиль DVOL ≥ 0.8), кластери = серії ≥ 0.8\n")
    a2 = analyse(L, "H2", h2, ep2, len(h2d), loss_ref)
    log("H2 done", t0)

    h3 = trades(D, {**P, "exit": "managed", "tp": H3_TP}, days)
    L.append(f"## H3: вихід за курсом ({H3_TP:.0%} кредиту або спот на проданому страйку), щосуботи\n")
    a3 = analyse(L, "H3", h3, cl, len(days), loss_ref)
    log("H3 done", t0)

    st = {
        "H1": vp3.summary(trades(D, {**P, "hs": 0.03}, days), cl)["mean"],
        "H2": vp3.summary(trades(D, {**P, "hs": 0.03}, h2d), ep2)["mean"],
        "H3": vp3.summary(trades(D, {**P, "hs": 0.03, "exit": "managed", "tp": H3_TP}, days), cl)["mean"],
    }
    pv = {**PREV_RAW_P, "P12-H1": a1["p"], "P12-H2": a2["p"], "P12-H3": a3["p"]}
    adj = holm_adjust(pv)
    L.append("## Холм партії пункту 4 (P3 H1–H3 + P1′ H1–H3 + P12 H1–H3, 9 тестів)\n")
    L.append("- " + ", ".join(f"{k}: сирий {pv[k]:.3f} → {adj[k]:.3f}" for k in sorted(pv)) + "\n")
    L.append("## Вердикти (механічно за реєстрацією)\n")
    L.append(
        "| гіпотеза | n | середній R | CI об'єднання | IS | OOS | стрес 3 в.п. | Холм p | MDE | ворота хвоста | стрес-хвіст | вердикт |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for h, a in (("H1", a1), ("H2", a2), ("H3", a3)):
        v = verdict(
            mean=a["mean"],
            lo=a["lo"],
            hi=a["hi"],
            holm_p=adj[f"P12-{h}"],
            is_mean=a["is"],
            oos_mean=a["oos"],
            stress_cost_mean=st[h],
            edge=edge,
            tail_ok=a["gate"]["ok"],
        )
        L.append(
            f"| {h} | {a['n']} | {rr(a['mean'])} | [{a['lo']:+.3f}; {a['hi']:+.3f}] | {rr(a['is'])} | {rr(a['oos'])} | {rr(st[h])} | "
            f"{adj[f'P12-{h}']:.3f} | {a['mde']:.3f} | {'так' if a['gate']['ok'] else 'ні'} ({a['gate']['n_loss']}) | {rr(a['ptail']['stress_mean'])} | **{v}** |"
        )
    L.append(f"\nEDGE_R (поріг «перевага, варта зусиль» для ✗) = {edge:.2f} R, зафіксовано на стадії 0.\n")
    log("verdict done", t0)

    L.append("## Чутливість H1 (усі варіанти зареєстровано заздалегідь)\n")
    L += HDR
    grid: list[tuple[str, dict[str, Any]]] = [
        *((f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}) for h in (0.0, 0.03)),
        ("модель «сторона»", {**P, "model": "side"}),
        *(
            (f"Δ {sd:.2f} × крило {w:.0%}", {**P, "short_delta": sd, "wing": w})
            for sd in (0.20, 0.25, 0.30)
            for w in (0.02, 0.03, 0.05)
            if (sd, w) != (0.25, 0.03)
        ),
        ("Δ 0.15 × крило 3 % (P1′ без фільтра)", {**P, "short_delta": 0.15}),
        *DTE_VARIANTS,
        ("вхід у п'ятницю", {**P, "weekday": 4}),
        ("вихід за курсом, ціль 50 %", {**P, "exit": "managed", "tp": 0.5}),
    ]
    for name, Pv in grid:
        ds = saturdays(D, Pv)
        L.append(vp1.table_row(name, trades(D, Pv, ds), blocks(D, Pv), len(ds)))
        log(f"grid: {name}", t0)
    for th in (0.7, 0.9):
        ds, _ = hi_days(D, P, th)
        L.append(vp1.table_row(f"умова P2, поріг {th:.1f}", trades(D, P, ds), vp1.state_episodes(pcts, th, both=False), len(ds)))
    lo_d = sorted(d for d, p in pcts.items() if p < THRESH)
    L.append(vp1.table_row("перцентиль < 0.8", trades(D, P, lo_d), cl, len(lo_d)))
    top = sorted(h1, key=lambda t: t["r"], reverse=True)[5:]
    L.append(vp1.table_row("H1 без 5 найкращих угод", top, cl, len(days)))

    # paired 25Δ - 15Δ on the same Saturdays (registered sensitivity: is the premium richer at 25Δ?)
    p15 = {t["day"]: t for t in trades(D, {**P, "short_delta": 0.15}, days)}
    pairs = [(t["day"], t["r"] - p15[t["day"]]["r"], t["pnl_spot"] - p15[t["day"]]["pnl_spot"]) for t in h1 if t["day"] in p15]
    if pairs:
        b = cluster_bootstrap([x for _, x, _ in pairs], [cl[d] for d, _, _ in pairs], n_boot=N_BOOT, seed=SEED)
        bs = cluster_bootstrap([x for _, _, x in pairs], [cl[d] for d, _, _ in pairs], n_boot=N_BOOT, seed=SEED)
        L.append(
            f"\n- парна різниця 25Δ − 15Δ (ті самі суботи, n = {b['n']}): R {rr(b['mean'])} {ci_r(b['ci'])}, p {b['p']:.3f}; "
            f"% спота {pc(bs['mean'])} [{pc(bs['ci'][0])}; {pc(bs['ci'][1])}]"
        )
    intent_to_treat(L, D, h1, days)
    L.append(
        "\nЄдиний актив (BTC): пункт 5а (по активах) не застосовний; ETH не покрито кешем поверхонь. Маржа Bybit не "
        "зменшується крилом, тож R на макс. збиток — верхня межа дохідності на фактичну маржу.\n"
    )


def intent_to_treat(L: list[str], D: dict[str, Any], h1: list[dict[str, Any]], days: list[int]) -> None:
    """Registered selection check: the Saturdays the coverage filter skipped, rebuilt with a looser strike tolerance
    (0.03, then 0.05 of spot); the wings then deviate from the registered construction, so this is a check, not H1."""
    r = {t["day"]: t["r"] for t in h1}
    rebuilt: list[float] = []
    for d in days:
        if d in r:
            continue
        for band in (0.03, 0.05):
            Pb = {**BASE, "band": band}
            b, _ = vp1.build(D, d, Pb)
            if b is not None:
                r[d] = vp1.outcome(D, b, Pb)["r"]
                rebuilt.append(r[d])
                break
    rs = list(r.values())
    L.append(
        f"\n- «намір торгувати» (H1 + пропущені суботи з допуском страйків до 0.05 спота): n = {len(rs)} з {len(days)}, "
        f"середній R {rr(sum(rs) / len(rs))}; лише добудовані: n = {len(rebuilt)}, "
        f"середній R {rr(sum(rebuilt) / len(rebuilt) if rebuilt else None)}, у плюсі {sum(1 for x in rebuilt if x > 0)}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("coverage", "full"), default="coverage")
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/p12_condor_25d")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.perf_counter()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    D = vp1.load(Path(a.src_dir).expanduser(), t0)
    L = [f"# P12: кондор з 25Δ шортами, BTC (stage {a.stage}, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n"]
    base = coverage(L, D, t0)
    edge0 = sanity(L, D, base)
    log("coverage and sanity done", t0)
    if a.stage == "full":
        if EDGE_R is None:
            raise SystemExit("EDGE_R is not registered yet: run --stage coverage and fix it in the card first")
        if abs(EDGE_R - edge0) > 1e-9:
            L.append(f"> Увага: EDGE_R у коді {EDGE_R:.2f} ≠ значення стадії 0 {edge0:.2f}.\n")
        full(L, D, cache, t0, EDGE_R)
    text = "\n".join(L)
    print(text)
    if a.out:
        Path(a.out).expanduser().write_text(text + "\n")
    log("done", t0)


if __name__ == "__main__":
    main()
