"""Weekly ATM straddle sold only when the DVOL percentile >= 0.4 (IDEA-20261031-structural-edges-depth, item 8e).

Pre-registration: the card's "Пункт 8e. Попередня реєстрація". No network: the trades are rebuilt by
``scripts/verify_weekly_straddle.py`` (``load`` / ``build``, unchanged) from the put-skew-v2 cache
(``--src-dir``, read only). Output: a markdown block on stdout and in ``--out``.

    uv run python scripts/verify_dvol_filtered_straddle.py --stage checks
    uv run python scripts/verify_dvol_filtered_straddle.py --stage full --out ~/.cache/tradedesk-agents/dvol_filtered_straddle/full.md
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_weekly_straddle as vws  # noqa: E402  (scripts/ is not a package)

from tradedesk.analytics.dvol_filter import (  # noqa: E402
    block_bootstrap_mean,
    cvar,
    entry_percentiles,
    filter_rows,
    max_drawdown,
    mean_without_top,
    placebo_gains,
    placebo_p,
    quantile,
    ruin_gate,
)
from tradedesk.analytics.pm_modules import cochran_q, holm  # noqa: E402
from tradedesk.analytics.weekly_straddle import HOUR_MS, mde, newey_west_mean  # noqa: E402

PCT = 100.0
DAY_MS = 86_400_000
THRESHOLD = 0.4
SPLIT = int(datetime(2025, 3, 1, tzinfo=UTC).timestamp() * 1000)
SPLIT_2024 = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
Y2026 = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
SEED = 20261031
N_BOOT = 10_000
N_PLACEBO = 10_000
DESCRIPTIVE_THRESHOLDS = (0.2, 0.3, 0.5, 0.6, 0.8)
VARIANTS: tuple[tuple[str, str, bool, bool], ...] = (
    ("base (основна)", "base", True, True),
    ("zero", "zero", True, True),
    ("stress (−3 вп, хедж 0.10 %)", "stress", True, True),
    ("trade_side", "trade_side", True, True),
    ("base, без funding", "base", False, True),
    ("base, без хеджу", "base", True, False),
)
NAMED = ("2022-06-13", "2022-11-08", "2024-08-05", "2025-10-10")


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def ymd(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


def ci_s(ci: tuple[float, float] | None) -> str:
    return f"[{ci[0] * PCT:+.2f}; {ci[1] * PCT:+.2f}]" if ci else "—"


def boot(v: list[float]) -> dict[str, Any]:
    return block_bootstrap_mean(v, block=4, n_boot=N_BOOT, seed=SEED)


def split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return [r for r in rows if r["day"] < SPLIT], [r for r in rows if r["day"] >= SPLIT]


def checks(D: dict[str, Any], pct: dict[int, float], base: dict[int, dict[str, Any]], L: list[str]) -> None:
    L.append("## Перевірки (без P&L за групами фільтра)\n")
    vals = [base[d]["pnl"] for d in sorted(base)]
    nw = newey_west_mean(vals, 1)
    L.append(
        f"- Відтворення базової комірки (7d, base): n = {len(vals)}, середнє {nw['mean'] * PCT:+.3f} % {ci_s(nw['ci95'])}, "
        f"найгірший {min(vals) * PCT:+.2f} % ({ymd(min(base, key=lambda d: base[d]['pnl']))}); у звіті 2026-10-15: n = 300, +0.10 % [−0.20; +0.40], −10.65 % (2021-05-07)."
    )
    have = [d for d in base if d in pct]
    is_, oos = [d for d in have if d < SPLIT], [d for d in have if d >= SPLIT]
    L.append(
        f"- DVOL-свічок {len(D['dvol'])} ({ymd(min(t for t, _ in D['dvol']))} … {ymd(max(t for t, _ in D['dvol']))}); тижнів угод з P_t: {len(have)} "
        f"(перший {ymd(min(have))}); IS {len(is_)}, OOS {len(oos)}."
    )
    L.append(
        f"- Частка тижнів з P_t ≥ {THRESHOLD}: IS {sum(pct[d] >= THRESHOLD for d in is_) / len(is_):.0%} ({sum(pct[d] >= THRESHOLD for d in is_)}), "
        f"OOS {sum(pct[d] >= THRESHOLD for d in oos) / len(oos):.0%} ({sum(pct[d] >= THRESHOLD for d in oos)})."
    )
    # no-lookahead spot check: the DVOL value used on the first OOS Friday is the candle stamped the day before
    d0 = min(oos)
    stamp = {(int(t) // DAY_MS) * DAY_MS: v for t, v in D["dvol"]}
    L.append(
        f"- Приклад входу {ymd(d0)} 09:00: використано свічку {ymd(d0 - DAY_MS)} (закриття о 00:00 {ymd(d0)}), DVOL {stamp.get(d0 - DAY_MS, float('nan')):.2f}, P_t = {pct[d0]:.3f}."
    )
    gaps = sorted(stamp)
    big = [(ymd(a), ymd(b)) for a, b in zip(gaps, gaps[1:], strict=False) if b - a > DAY_MS]
    L.append(f"- Пропуски денних свічок DVOL: {len(big)} {big[:5]}.")


def variant_block(name: str, tr: dict[int, dict[str, Any]], pct: dict[int, float]) -> dict[str, Any]:
    pnl = {d: t["pnl"] for d, t in tr.items()}
    rows = filter_rows(pnl, pct, THRESHOLD)
    is_, oos = split(rows)
    g_oos = [r["gain"] for r in oos]
    p1 = boot(g_oos)
    sel_oos = [r["pnl"] for r in oos if r["selected"]]
    p2 = boot(sel_oos)
    return {
        "name": name,
        "rows": rows,
        "is": is_,
        "oos": oos,
        "p1": p1,
        "p1_nw": newey_west_mean(g_oos, 1),
        "p2": p2,
        "is_gain": statistics.mean(r["gain"] for r in is_),
        "is_sel": statistics.mean(r["pnl"] for r in is_ if r["selected"]),
        "base_oos": statistics.mean(r["pnl"] for r in oos),
        "base_is": statistics.mean(r["pnl"] for r in is_),
    }


def full(D: dict[str, Any], pct: dict[int, float], base: dict[int, dict[str, Any]], L: list[str]) -> None:
    t0 = time.perf_counter()
    blocks = [variant_block(VARIANTS[0][0], base, pct)]
    for name, ck, fund, hedge in VARIANTS[1:]:
        tr = vws.build(D, "7d", ck, fund=fund, hedge=hedge)
        blocks.append(variant_block(name, tr, pct))
        print(f"  variant {name}: {len(tr)} trades, {time.perf_counter() - t0:.0f}s, peak RSS {rss_mb():.0f} MB", flush=True)
    B = blocks[0]
    rows, is_, oos = B["rows"], B["is"], B["oos"]
    g_oos = [r["gain"] for r in oos]
    k = sum(r["selected"] for r in oos)

    lo = [r["pnl"] for r in rows if r["pct"] < 0.5]
    hi = [r["pnl"] for r in rows if r["pct"] >= 0.5]
    L.append(
        f"\n- Узгодженість з H2 базового звіту (половини P, усі тижні): P < 0.5 {statistics.mean(lo) * PCT:+.2f} % (n = {len(lo)}), "
        f"P ≥ 0.5 {statistics.mean(hi) * PCT:+.2f} % (n = {len(hi)}); у звіті ≈ −0.24 (186) і ≈ +0.76 (76)."
    )

    # --- primary
    L.append("\n## P1 / P2 (основна модель base; OOS = входи ≥ 2025-03-01)\n")
    sd_g = statistics.stdev(g_oos)
    L.append(
        f"- OOS: n = {len(oos)} тижнів з P_t, з них продано {k} ({k / len(oos):.0%}); база (щотижня) {B['base_oos'] * PCT:+.3f} % на тиждень."
    )
    L.append(
        f"- **P1 приріст** {B['p1']['mean'] * PCT:+.3f} % спота на тиждень, бутстреп {ci_s(B['p1']['ci95'])}, p = {B['p1']['p']:.3f}; NW(1) {ci_s(B['p1_nw']['ci95'])}; "
        f"SD(d) {sd_g * PCT:.2f} %, **MDE {mde(sd_g, len(g_oos)) * PCT:.2f} %**; ×52 = {B['p1']['mean'] * 52 * PCT:+.1f} % номіналу за рік."
    )
    sel = [r["pnl"] for r in oos if r["selected"]]
    L.append(
        f"- **P2 фільтрована на тиждень угоди** {B['p2']['mean'] * PCT:+.3f} % (n = {len(sel)}), бутстреп {ci_s(B['p2']['ci95'])}, p = {B['p2']['p'] if B['p2']['p'] is not None else float('nan'):.3f}; "
        f"MDE {mde(statistics.stdev(sel), len(sel)) * PCT:.2f} %. Непродані тижні OOS: {statistics.mean([r['pnl'] for r in oos if not r['selected']]) * PCT:+.3f} % (n = {len(oos) - k})."
    )
    pv = {"P1": B["p1"]["p"] if B["p1"]["p"] is not None else 1.0, "P2": B["p2"]["p"] if B["p2"]["p"] is not None else 1.0}
    H = holm(pv)
    L.append(
        "- Холм (m = 2, α = 0.05): "
        + "; ".join(f"{h} p = {v['p']:.3f}, поріг {v['threshold']:.3f}, {'відкинуто' if v['reject'] else 'ні'}" for h, v in H.items())
        + "."
    )

    # --- placebo
    dr = placebo_gains([r["pnl"] for r in oos], k, n_draws=N_PLACEBO, seed=SEED)
    q975 = quantile(dr, 0.975)
    L.append(
        f"- Плацебо (OOS, {N_PLACEBO} випадкових виборів k = {k} тижнів): середнє {statistics.mean(dr) * PCT:+.3f}, 2.5/97.5 перцентилі "
        f"{quantile(dr, 0.025) * PCT:+.3f} / {q975 * PCT:+.3f}; спостережене {B['p1']['mean'] * PCT:+.3f} → p = {placebo_p(B['p1']['mean'], dr):.4f}, "
        f"{'поза 97.5-м' if B['p1']['mean'] > q975 else 'всередині'}."
    )
    k_is = sum(r["selected"] for r in is_)
    dr_is = placebo_gains([r["pnl"] for r in is_], k_is, n_draws=N_PLACEBO, seed=SEED)
    L.append(
        f"- IS (описово): n = {len(is_)}, продано {k_is}; база {B['base_is'] * PCT:+.3f}; приріст {B['is_gain'] * PCT:+.3f} {ci_s(boot([r['gain'] for r in is_])['ci95'])}; "
        f"фільтрована на тиждень угоди {B['is_sel'] * PCT:+.3f}; плацебо p = {placebo_p(B['is_gain'], dr_is):.4f}."
    )
    o25, o26 = [r["gain"] for r in oos if r["day"] < Y2026], [r["gain"] for r in oos if r["day"] >= Y2026]
    no_top = mean_without_top(g_oos, 5)
    L.append(
        f"- Частини OOS: 2025-03…12 {statistics.mean(o25) * PCT:+.3f} (n = {len(o25)}), 2026 {statistics.mean(o26) * PCT:+.3f} (n = {len(o26)}); "
        f"без 5 тижнів з найбільшим приростом {no_top * PCT:+.3f}; stress P1 {blocks[2]['p1']['mean'] * PCT:+.3f}."
    )
    top5 = sorted(oos, key=lambda r: r["gain"], reverse=True)[:5]
    L.append(
        "- 5 найбільших внесків у приріст OOS: "
        + ", ".join(f"{ymd(r['day'])} {r['gain'] * PCT:+.2f} (P {r['pct']:.2f})" for r in top5)
        + "."
    )

    # --- ruin gate
    L.append("\n## Ворота руїни (д) і хвіст (base; усі тижні з P_t)\n")
    L.append(
        "| Книга | період | тижнів угод | середнє на угоду | NW 95 % CI | найгірший | CVaR 5 % | мін. MTM | (д) строга | (д) механічна |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    gate_f = None
    for lab, part in (("усі", rows), ("OOS", oos)):
        for book, cond in (("фільтр P ≥ 0.4", lambda r: r["selected"]), ("база щотижня", lambda r: True)):
            pr = [r for r in part if cond(r)]
            v = [r["pnl"] for r in pr]
            nw = newey_west_mean(v, 1)
            g = ruin_gate(min(v), nw["mean"], nw["ci95"][1] if nw["ci95"] else None)
            worst_d = min(pr, key=lambda r: r["pnl"])["day"]
            mtm = min(base[r["day"]]["min_mtm"] for r in pr)
            if lab == "усі" and book.startswith("фільтр"):
                gate_f = g
            L.append(
                f"| {book} | {lab} | {len(v)} | {nw['mean'] * PCT:+.3f} | {ci_s(nw['ci95'])} | {min(v) * PCT:+.2f} ({ymd(worst_d)}) | {cvar(v) * PCT:+.2f} | {mtm * PCT:+.2f} | "
                f"{'спрацював' if g['strict'] else 'ні'} | {'спрацював' if g['mechanical'] else 'ні'} |"
            )
    cal_f = [r["filtered"] for r in rows]
    cal_b = [r["pnl"] for r in rows]
    L.append(
        f"\nМакс. просадка суми (календарні тижні з P_t, % спота на 1 BTC): фільтр {max_drawdown(cal_f) * PCT:.1f}, база {max_drawdown(cal_b) * PCT:.1f}. "
        f"Дохід на оцінену IM (фільтр, тиждень угоди): середнє {statistics.mean(base[r['day']]['pnl'] / base[r['day']]['im'] for r in rows if r['selected']) * PCT:+.2f} %, "
        f"медіана IM {statistics.median(base[r['day']]['im'] for r in rows if r['selected']) * PCT:.1f} % спота."
    )
    L.append(
        "\n5 найгірших тижнів фільтрованої (усі): "
        + ", ".join(
            f"{ymd(r['day'])} {r['pnl'] * PCT:+.2f} (P {r['pct']:.2f})"
            for r in sorted((r for r in rows if r["selected"]), key=lambda r: r["pnl"])[:5]
        )
        + "."
    )
    L.append(
        "Найгірші тижні бази, відсіяні фільтром: "
        + ", ".join(
            f"{ymd(r['day'])} {r['pnl'] * PCT:+.2f} (P {r['pct']:.2f})"
            for r in sorted((r for r in rows if not r["selected"]), key=lambda r: r["pnl"])[:5]
        )
        + "."
    )

    # --- grid
    L.append("\n## Сітка базового звіту (7d), з фільтром і без (усе, крім base, описово)\n")
    L.append(
        "| Варіант | база OOS | P1 приріст OOS | бутстреп CI | p | P2 фільтр OOS / угоду | CI | приріст IS | фільтр IS / угоду |\n|---|---|---|---|---|---|---|---|---|"
    )
    for b in blocks:
        L.append(
            f"| {b['name']} | {b['base_oos'] * PCT:+.3f} | {b['p1']['mean'] * PCT:+.3f} | {ci_s(b['p1']['ci95'])} | {b['p1']['p']:.3f} | {b['p2']['mean'] * PCT:+.3f} | {ci_s(b['p2']['ci95'])} | "
            f"{b['is_gain'] * PCT:+.3f} | {b['is_sel'] * PCT:+.3f} |"
        )

    # --- thresholds (descriptive)
    L.append("\n## Інші пороги (лише описово, base)\n")
    L.append("| Поріг | продано OOS | приріст OOS | CI | приріст IS | фільтр OOS / угоду |\n|---|---|---|---|---|---|")
    pnl = {d: t["pnl"] for d, t in base.items()}
    for thr in sorted((*DESCRIPTIVE_THRESHOLDS, THRESHOLD)):
        rr = filter_rows(pnl, pct, thr)
        a, b = split(rr)
        go = [r["gain"] for r in b]
        so = [r["pnl"] for r in b if r["selected"]]
        L.append(
            f"| {thr:.1f}{' (зареєстр.)' if thr == THRESHOLD else ''} | {len(so)}/{len(b)} | {statistics.mean(go) * PCT:+.3f} | {ci_s(boot(go)['ci95'])} | "
            f"{statistics.mean(r['gain'] for r in a) * PCT:+.3f} | {(statistics.mean(so) * PCT) if so else float('nan'):+.3f} |"
        )
    rr24 = [r for r in rows if r["day"] >= SPLIT_2024]
    L.append(
        f"\nМежа 2024-01-01 (описово, як H4 пункту 7): приріст 2024+ {statistics.mean(r['gain'] for r in rr24) * PCT:+.3f} {ci_s(boot([r['gain'] for r in rr24])['ci95'])} (n = {len(rr24)})."
    )

    # --- years and heterogeneity (5a)
    L.append("\n## Роки (5a; base, приріст d_t на всіх тижнях з P_t)\n")
    L.append("| Рік | n | продано | база | приріст | NW 95 % CI | фільтр / угоду |\n|---|---|---|---|---|---|---|")
    means, ses = [], []
    for y in range(2021, 2027):
        yr = [r for r in rows if ymd(r["day"])[:4] == str(y)]
        if not yr:
            continue
        g = [r["gain"] for r in yr]
        nw = newey_west_mean(g, 1)
        s = [r["pnl"] for r in yr if r["selected"]]
        if nw["se"] and nw["se"] > 0 and len(yr) >= 10:
            means.append(nw["mean"])
            ses.append(nw["se"])
        L.append(
            f"| {y} | {len(yr)} | {len(s)} | {statistics.mean(r['pnl'] for r in yr) * PCT:+.2f} | {nw['mean'] * PCT:+.3f} | {ci_s(nw['ci95'])} | "
            f"{(statistics.mean(s) * PCT) if s else float('nan'):+.2f} |"
        )
    cq = cochran_q(means, ses)
    L.append(
        f"\nCochran Q між роками = {cq['q']:.2f}, df {cq['df']:.0f}, p = {cq['p']:.3f}, I² = {cq['i2']:.0%}; зважений приріст {cq['pooled'] * PCT:+.3f}."
    )
    L.append("\nНазвані стрес-тижні (тижні, що перетинають добу дати):\n")
    for a in NAMED:
        t_ms = int(datetime.strptime(a, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
        hit = [r for r in rows if r["day"] + 9 * HOUR_MS < t_ms + DAY_MS and t_ms < base[r["day"]]["expiry"]]
        L.append(
            f"- {a}: "
            + (
                ", ".join(
                    f"вхід {ymd(r['day'])} P {r['pct']:.2f}, P&L {r['pnl'] * PCT:+.2f}, {'продано' if r['selected'] else 'пропущено'}"
                    for r in hit
                )
                or "—"
            )
        )

    # --- mechanical verdict
    L.append("\n## Механічна перевірка критеріїв реєстрації\n")
    p1 = B["p1"]
    c = {
        "Холм P1": H["P1"]["reject"],
        "нижня межа CI P1 > 0": bool(p1["ci95"] and p1["ci95"][0] > 0),
        "плацебо поза 97.5-м": B["p1"]["mean"] > q975,
        "(д) фільтрованої не спрацював": not gate_f["strict"],
        "IS приріст > 0": B["is_gain"] > 0,
        "2025-03…12 > 0": statistics.mean(o25) > 0,
        "2026 > 0": statistics.mean(o26) > 0,
        "без топ-5 > 0": no_top > 0,
        "stress P1 > 0": blocks[2]["p1"]["mean"] > 0,
    }
    for kk, v in c.items():
        L.append(f"- {kk}: {'так' if v else 'ні'}")
    if len(oos) < 40 or k < 10:
        verdict = "◯"
    elif p1["mean"] <= 0 or gate_f["strict"]:
        verdict = "✗"
    elif all(c.values()):
        verdict = "✓ (історичний; потрібен форвард)"
    else:
        verdict = "≈"
    L.append(f"- **Вердикт за реєстрацією: {verdict}**.")
    print(f"full done in {time.perf_counter() - t0:.0f}s, peak RSS {rss_mb():.0f} MB", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/dvol_filtered_straddle")
    ap.add_argument("--stage", choices=("checks", "full"), required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    D = vws.load(Path(a.src_dir).expanduser())
    print(f"loaded, peak RSS {rss_mb():.0f} MB", flush=True)
    pct = entry_percentiles(D["dvol"], window_days=365, min_obs=180)
    base = vws.build(D, "7d", "base")
    print(f"base built: {len(base)} trades, peak RSS {rss_mb():.0f} MB", flush=True)
    (cache / "weeks_base.json").write_text(
        json.dumps([{"day": ymd(d), "pnl": t["pnl"], "pct": pct.get(d)} for d, t in sorted(base.items())])
    )
    L = [f"# 8e: тижневий ATM-стреддл при P(DVOL) ≥ 0.4 — сирий вивід `--stage {a.stage}`\n"]
    checks(D, pct, base, L)
    if a.stage == "full":
        full(D, pct, base, L)
    report = "\n".join(L)
    print(report)
    if a.out:
        Path(a.out).expanduser().write_text(report + "\n")


if __name__ == "__main__":
    main()
