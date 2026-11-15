#!/usr/bin/env python3
"""P6 / P3 mechanism on history: vertical debit spread vs the naked long option (IDEA-20261114-options-playbook, 4.9).

The playbook claims a debit spread "pays less VRP" than the bare option. BTC, Saturdays 09:00 UTC 2021-01-02 …
2026-10-03, surfaces of `put_skew` (Deribit marks from trades), Bybit costs (mark +/- half-spread, taker and delivery
fees). Each Saturday and side: bull call spread |delta| 0.40 / 0.15 vs the long 0.40 call alone (and the put mirror),
same strike, same execution IV, same expiry (nearest to 14 d in 7-24 d), held to expiry.

--stage coverage: coverage and sanity only (no P&L). --stage full: H1-H3, Holm of the item-4 batch, sensitivity.
Pre-registration: the card, section 4.9. Run: uv run python scripts/verify_p6_spread_vs_naked.py --stage coverage
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
import verify_p1_condor_ivrv as vpi  # noqa: E402  (scripts/ is not a package)
import verify_p3_trend_spread as vp3  # noqa: E402
from common_helpers import ymd_ms  # noqa: E402

from tradedesk.analytics.calendar_p10 import batch_raw_p  # noqa: E402
from tradedesk.analytics.condor_25d import block_clusters, parametric_tail  # noqa: E402
from tradedesk.analytics.condor_dvol import cluster_bootstrap_diff  # noqa: E402
from tradedesk.analytics.dvol_filter import cvar, max_drawdown, mean_without_top  # noqa: E402
from tradedesk.analytics.option_condor import block_bootstrap_mean  # noqa: E402
from tradedesk.analytics.spread_vs_naked import p6_labels, pairs_for  # noqa: E402
from tradedesk.analytics.straddle_p5 import two_sided_tail_gate, verdict  # noqa: E402
from tradedesk.analytics.trend_spread import DAY_MS, cluster_bootstrap, holm_adjust  # noqa: E402

OOS = vp3.OOS
COVERAGE_MIN = 0.60
N_BOOT = vp3.N_BOOT
SEED = vp3.SEED
# Registered before stage 0 (card 4.9): "difference worth the effort" for the ✗ branch.
EDGE_R = 0.10  # H1, H3: R per capital (each construction on its own debit), per ~13-day trade
EDGE_SPOT = 0.0005  # H2: 0.05 % of spot per 1 BTC per trade
PREV_RAW_P = {**vpi.PREV_RAW_P, "P1ivrv-H1": 0.000, "P1ivrv-H2": 0.313, "P1ivrv-H3": 0.000}  # p1_condor_ivrv/full.md
PREV_DEGENERATE = set(vpi.PREV_DEGENERATE)
KEY = "P6vs"
BASE: dict[str, Any] = {
    "weekday": 5,
    "target": 14.0,
    "lo": 7.0,
    "hi": 24.0,
    "long_delta": 0.40,
    "short_delta": 0.15,
    "band": 0.015,
    "model": "mark",
    "hs": 0.015,
    "arb": "replace",
    "exit": "expiry",
    "tp": 0.70,
    "last_expiry": vp3.LAST_EXPIRY_MS,
}

log = vp3.log
rr = vp3.rr

HDR = [
    "| варіант | n | кластерів | середнє | 95 % CI (4-тижневі блоки) | p | частка > 0 | R спреду | R голого | покриття |",
    "|---|---|---|---|---|---|---|---|---|---|",
]


# ----------------------------------------------------------------------------- data


def load(src: Path, closes_dir: Path, kdir: Path, t0: float) -> dict[str, Any]:
    D = vp3.load(src, closes_dir, kdir, t0)
    by_exp: dict[int, float] = {}
    for snap in D["days"].values():
        for e in snap["expiries"]:
            p = D["delivery"].get(ymd_ms(e))
            if p is not None:
                by_exp[e] = p
    D["dlv"] = by_exp
    log(f"delivery prices mapped to {len(by_exp)} expiries", t0)
    return D


def saturdays(D: dict[str, Any], weekday: int = 5) -> list[int]:
    return vp3.entry_days(D, weekday)


def both_sides(days: list[int]) -> list[tuple[int, bool]]:
    return [(d, b) for d in days for b in (False, True)]


def trend_side(D: dict[str, Any], weekday: int = 5, counter: bool = False) -> list[tuple[int, bool]]:
    lab = vp3.labels_for(D, weekday)
    return [(d, (x == "bear") != counter) for d, x in sorted(lab.items()) if x != "neutral"]


def pairs(
    D: dict[str, Any], entries: list[tuple[int, bool]], P: dict[str, Any], *, outcome: bool = True
) -> tuple[list[dict[str, Any]], Counter[str]]:
    return pairs_for(D["days"], D["dlv"], entries, P, outcome=outcome)


def blocks(days: list[int]) -> dict[int, int]:
    return block_clusters(days, start_ms=days[0], weeks=4)


# ----------------------------------------------------------------------------- stats


def fmt(x: float | None, key: str) -> str:
    if x is None or x != x:
        return "—"
    return f"{x * 100:+.3f} %" if key == "pnl_spot" else f"{x:+.3f}"


def row(label: str, tr: list[dict[str, Any]], ep: dict[int, int], elig: int, key: str = "r") -> str:
    cov = len(tr) / elig if elig else 0.0
    if not tr:
        return f"| {label} | 0 | 0 | — | — | — | — | — | — | {cov:.0%} |"
    s = cluster_bootstrap([t[key] for t in tr], [ep[t["day"]] for t in tr], n_boot=N_BOOT, seed=SEED)
    win = sum(1 for t in tr if t[key] > 0) / len(tr)
    ms = statistics.fmean(t["r_spread"] for t in tr)
    mn = statistics.fmean(t["r_naked"] for t in tr)
    ci = f"[{fmt(s['ci'][0], key)}; {fmt(s['ci'][1], key)}]"
    return f"| {label} | {s['n']} | {s['n_clusters']} | {fmt(s['mean'], key)} | {ci} | {s['p']:.3f} | {win:.0%} | {ms:+.3f} | {mn:+.3f} | {cov:.0%} |"


def mean_of(tr: list[dict[str, Any]], key: str) -> float:
    return statistics.fmean(t[key] for t in tr) if tr else float("nan")


def analyse(L: list[str], name: str, tr: list[dict[str, Any]], ep: dict[int, int], elig: int, key: str) -> dict[str, Any]:
    tr = sorted(tr, key=lambda t: (t["day"], t["bearish"]))
    vals = [t[key] for t in tr]
    cl = [ep[t["day"]] for t in tr]
    s = cluster_bootstrap(vals, cl, n_boot=N_BOOT, seed=SEED)
    L += HDR
    L.append(row(f"{name}: усі", tr, ep, elig, key))
    L.append(row("IS (до 2024)", [t for t in tr if t["day"] < OOS], ep, elig, key))
    L.append(row("OOS (2024+)", [t for t in tr if t["day"] >= OOS], ep, elig, key))
    L.append(row("кол-сторона (bull call spread − кол)", [t for t in tr if not t["bearish"]], ep, elig, key))
    L.append(row("пут-сторона (bear put spread − пут)", [t for t in tr if t["bearish"]], ep, elig, key))
    for y in sorted({ymd_ms(t["day"])[:4] for t in tr}):
        L.append(row(f"рік {y}", [t for t in tr if ymd_ms(t["day"])[:4] == y], ep, elig, key))
    bb = block_bootstrap_mean(vals, block=4, n_boot=N_BOOT, seed=SEED)
    lo, hi = min(s["ci"][0], bb["ci"][0]), max(s["ci"][1], bb["ci"][1])
    L.append(
        f"\n- блоковий бутстреп (угоди в порядку входу, блок 4): {fmt(bb['mean'], key)} [{fmt(bb['ci'][0], key)}; {fmt(bb['ci'][1], key)}]"
    )
    L.append(f"- CI для вердикту (об'єднання кластерного й блокового): [{fmt(lo, key)}; {fmt(hi, key)}]")
    halves = []
    for k in (0, 1):
        days_k = sorted({t["day"] for t in tr})[k::2]
        sub = [t[key] for t in tr if t["day"] in set(days_k)]
        bk = block_bootstrap_mean(sub, block=4, n_boot=N_BOOT, seed=SEED)
        halves.append((sum(1 for v in sub if v <= 0), len(sub)))
        L.append(f"- половина {k + 1} (кожна друга субота, обидві сторони): n = {len(sub)}, {fmt(bk.get('mean'), key)}")
    g = two_sided_tail_gate(vals, cl)
    L.append(
        f"- ворота хвоста (двобічні): ≤ 0 — {g['n_loss']}, ≥ 0 — {g['n_win']}; P(ресемпл без від'ємного / додатного кластера) "
        f"{g['loss']['p_no_loss_resample']:.3f} / {g['win']['p_no_loss_resample']:.3f} → **{'пройдено' if g['ok'] else 'НЕ пройдено'}**"
    )
    pt = max((parametric_tail(vals, k_eff=k, n_eff=n, loss_ref=None) for k, n in halves), key=lambda x: x["q_up"])
    L.append(
        f"- параметричний хвіст: частка ≤ 0 {pt['q_obs']:.1%}, 95 % верхня межа {pt['q_up']:.1%} → стрес-середнє {fmt(pt['stress_mean'], key)}"
    )
    L.append(
        f"- хвіст: найгірше {fmt(min(vals), key)}, CVaR 5 % {fmt(cvar(vals), key)}, просадка суми {max_drawdown(vals):.3f}; "
        f"без 5 найкращих {fmt(mean_without_top(vals, 5), key)}; MDE {fmt(2.8 * s['se'], key)}\n"
    )
    side = cluster_bootstrap_diff(
        vals, [(c, t["bearish"]) for c, t in zip(cl, tr, strict=True)], [not t["bearish"] for t in tr], n_boot=N_BOOT, seed=SEED
    )
    calls_is = mean_of([t for t in tr if not t["bearish"] and t["day"] < OOS], key)
    calls_oos = mean_of([t for t in tr if not t["bearish"] and t["day"] >= OOS], key)
    puts_is = mean_of([t for t in tr if t["bearish"] and t["day"] < OOS], key)
    puts_oos = mean_of([t for t in tr if t["bearish"] and t["day"] >= OOS], key)
    if side["ci"]:
        L.append(
            f"- сторони (5а: гетерогенність): кол − пут {fmt(side['diff'], key)} [{fmt(side['ci'][0], key)}; {fmt(side['ci'][1], key)}], p {side['p']:.3f} "
            f"(групи ресемплюються окремо; сторони однієї суботи корельовані — CI наближений); стійкість IS → OOS: "
            f"кол {fmt(calls_is, key)} → {fmt(calls_oos, key)}, пут {fmt(puts_is, key)} → {fmt(puts_oos, key)}\n"
        )
    return {
        **s,
        "lo": lo,
        "hi": hi,
        "gate": g,
        "ptail": pt,
        "is": mean_of([t for t in tr if t["day"] < OOS], key),
        "oos": mean_of([t for t in tr if t["day"] >= OOS], key),
        "mde": 2.8 * s["se"],
    }


# ----------------------------------------------------------------------------- stage 0


def q(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


def coverage(L: list[str], D: dict[str, Any], t0: float) -> dict[str, Any]:
    P = BASE
    sats = saturdays(D)
    ent = both_sides(sats)
    built, why = pairs(D, ent, P, outcome=False)
    cov = len(built) / len(ent)
    L.append(f"## Стадія 0: покриття (поріг {COVERAGE_MIN:.0%}; P&L не рахується)\n")
    L.append(f"- суботи {ymd_ms(sats[0])} … {ymd_ms(sats[-1])}: {len(sats)}; пар субота × сторона {len(ent)}")
    L.append(f"- **H1 / H2: пару побудовано {len(built)} / {len(ent)} = {cov:.1%}**; пропуски: {dict(why.most_common())}")
    for b, nm in ((False, "кол"), (True, "пут")):
        n_b = sum(1 for t in built if t["bearish"] == b)
        L.append(f"  - {nm}-сторона: {n_b} / {len(sats)} = {n_b / len(sats):.1%}")
    yrs = Counter(ymd_ms(d)[:4] for d, _ in ent)
    yb = Counter(ymd_ms(t["day"])[:4] for t in built)
    L.append("  - по роках (пар / побудовано): " + ", ".join(f"{y} {yrs[y]}/{yb[y]}" for y in sorted(yrs)))
    ep = blocks(sats)
    L.append(
        f"  - IS / OOS {sum(1 for t in built if t['day'] < OOS)} / {sum(1 for t in built if t['day'] >= OOS)}; "
        f"4-тижневих блоків серед пар {len({ep[t['day']] for t in built})} з {len(set(ep.values()))}"
    )
    tre = trend_side(D)
    h3, why3 = pairs(D, tre, P, outcome=False)
    L.append(f"- **H3 (сторона тренду P3): {len(h3)} / {len(tre)} = {len(h3) / len(tre):.1%}**; пропуски: {dict(why3.most_common())}")
    p3, _ = pairs(D, tre, {**P, "arb": "none"}, outcome=False)
    L.append(
        f"- звірка з відомим числом: конструкція P3 (без правила (а)) на трендових суботах — {len(p3)} / {len(tre)} (звіт P3: 197 / 238)"
    )
    L.append("\n### Покриття варіантів чутливості\n")
    for name, Pv, e in variants(D, P):
        b, _ = pairs(D, e, Pv, outcome=False)
        L.append(f"- {name}: {len(b)} / {len(e)} = {len(b) / len(e):.1%}" if e else f"- {name}: 0")
    log("coverage done", t0)
    return {"built": built, "cov": cov}


def variants(D: dict[str, Any], P: dict[str, Any]) -> list[tuple[str, dict[str, Any], list[tuple[int, bool]]]]:
    sats = both_sides(saturdays(D))
    return [
        *((f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, sats) for h in (0.0, 0.03)),
        ("модель «сторона»", {**P, "model": "side"}, sats),
        *((f"|Δ| купленої {ld:.2f} × проданої 0.15", {**P, "long_delta": ld}, sats) for ld in (0.35, 0.45)),
        *((f"|Δ| купленої 0.40 × проданої {sd:.2f}", {**P, "short_delta": sd}, sats) for sd in (0.10, 0.20)),
        ("ціль DTE 7 д (вікно 5–24 д)", {**P, "target": 7.0, "lo": 5.0}, sats),
        ("ціль DTE 21 д", {**P, "target": 21.0}, sats),
        ("правило (а) «пропуск дня»", {**P, "arb": "skip"}, sats),
        ("без правила (а) (сирі марки, як P3)", {**P, "arb": "none"}, sats),
        ("спред з виходом P6 (70 % макс. прибутку / спот за проданою), голий до експірації", {**P, "exit": "managed"}, sats),
        ("сторона проти тренду P3", P, trend_side(D, counter=True)),
        ("вхід у п'ятницю", {**P, "weekday": 4}, both_sides(saturdays(D, 4))),
    ]


def sanity(L: list[str], D: dict[str, Any], built: list[dict[str, Any]]) -> None:
    L.append("\n## Перевірки до прогону\n")
    dl = [abs(t["delta_long"]) for t in built]
    ds = [abs(t["delta_short"]) for t in built]
    L.append(
        f"- фактичні |Δ|: куплена медіана {statistics.median(dl):.3f} [p10 {q(dl, 0.1):.3f}; p90 {q(dl, 0.9):.3f}], продана {statistics.median(ds):.3f} [p10 {q(ds, 0.1):.3f}; p90 {q(ds, 0.9):.3f}]"
    )
    fr = [t["debit_spread"] / t["width"] for t in built]
    L.append(f"- дебет спреду / ширина: медіана {statistics.median(fr):.3f}, мін {min(fr):.3f}, макс {max(fr):.3f} (має бути в (0; 1))")
    ns = [t["debit_naked"] / t["spot"] for t in built]
    L.append(
        f"- дебет голого / спот: медіана {statistics.median(ns):.2%} [p10 {q(ns, 0.1):.2%}; p90 {q(ns, 0.9):.2%}] (очікування ≈ 0.3 σ √T ≈ 2.5–3 % при IV 50 %)"
    )
    rt = [t["debit_spread"] / t["debit_naked"] for t in built]
    L.append(f"- дебет спреду / дебет голого: медіана {statistics.median(rt):.3f} [p10 {q(rt, 0.1):.3f}; p90 {q(rt, 0.9):.3f}]")
    L.append(
        f"- DTE медіана {statistics.median(t['dte'] for t in built):.2f} д; мін {min(t['dte'] for t in built):.2f}, макс {max(t['dte'] for t in built):.2f}"
    )
    arb = Counter(t["arb_long"] for t in built) + Counter(t["arb_short"] for t in built)
    L.append(f"- правило (а) на 2 ногах пар: {dict(arb)}")
    side, _ = pairs(D, both_sides(saturdays(D)), {**BASE, "model": "side"}, outcome=False)
    L.append(f"- модель «сторона»: ніг без принта {sum(t['fallbacks'] for t in side)} з {2 * len(side)}")
    ivs = [
        x["strikes"][k]["mark_iv"] for s in list(D["days"].values())[:50] for x in s["expiries"].values() for k in list(x["strikes"])[:3]
    ]
    L.append(f"- одиниці: mark IV (перші 50 днів) медіана {statistics.median(ivs):.3f} (десяткові)")
    close_at = dict(zip(D["stamps"], D["closes"], strict=True))
    diffs = [abs(D["days"][d]["spot"] / close_at[d - DAY_MS] - 1) for d in saturdays(D) if d in D["days"] and d - DAY_MS in close_at]
    L.append(
        f"- рівень цін: спот поверхні проти закриття Binance п'ятниці: медіана {statistics.median(diffs):.2%}, максимум {max(diffs):.2%}\n"
    )


# ----------------------------------------------------------------------------- full


def full(L: list[str], D: dict[str, Any], cov: float, cache: Path, t0: float) -> None:
    P = BASE
    sats = saturdays(D)
    ep = blocks(sats)
    ent = both_sides(sats)
    h1, _ = pairs(D, ent, P)
    (cache / "pairs_h1.json").write_text(json.dumps(h1))
    L.append("## H1: R(спред) − R(голий опціон), обидві сторони щосуботи, до експірації (R — на власний дебет)\n")
    a1 = analyse(L, "H1", h1, ep, len(ent), "r")
    log("H1 done", t0)
    L.append("## H2: P&L(спред) − P&L(голий) у % спота на 1 BTC (= продана нога 15Δ з її витратами)\n")
    a2 = analyse(L, "H2", h1, ep, len(ent), "pnl_spot")
    log("H2 done", t0)
    tre = trend_side(D)
    h3, _ = pairs(D, tre, P)
    (cache / "pairs_h3.json").write_text(json.dumps(h3))
    L.append("## H3: як H1, лише сторона тренду P3 (трендові суботи)\n")
    a3 = analyse(L, "H3", h3, ep, len(tre), "r")
    cov3 = len(h3) / len(tre)
    log("H3 done", t0)

    s1, _ = pairs(D, ent, {**P, "hs": 0.03})
    s3, _ = pairs(D, tre, {**P, "hs": 0.03})
    st = {"H1": mean_of(s1, "r"), "H2": mean_of(s1, "pnl_spot"), "H3": mean_of(s3, "r")}
    res = {"H1": a1, "H2": a2, "H3": a3}
    edge = {"H1": EDGE_R, "H2": EDGE_SPOT, "H3": EDGE_R}
    cov_ok = {"H1": cov >= COVERAGE_MIN, "H2": cov >= COVERAGE_MIN, "H3": cov3 >= COVERAGE_MIN}
    raw = {**PREV_RAW_P, **{f"{KEY}-{h}": a["p"] for h, a in res.items()}}
    deg = PREV_DEGENERATE | {f"{KEY}-{h}" for h, a in res.items() if not a["gate"]["ok"]}
    pv = batch_raw_p(raw, deg)
    adj = holm_adjust(pv)
    L.append(f"## Холм партії пункту 4 ({len(pv)} тестів; вироджені → 1.0: {sorted(deg)})\n")
    L.append("- " + ", ".join(f"{k}: {pv[k]:.3f} → {adj[k]:.3f}" for k in sorted(pv)) + "\n")
    L.append("## Вердикти (механічно за реєстрацією, `straddle_p5.verdict`)\n")
    L.append("| гіпотеза | n | середнє | CI | IS | OOS | стрес 3 в.п. | Холм p | MDE | EDGE | ворота хвоста | покриття | вердикт |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    vs = {}
    for h, a in res.items():
        k = "pnl_spot" if h == "H2" else "r"
        vs[h] = verdict(
            mean=a["mean"],
            lo=a["lo"],
            hi=a["hi"],
            holm_p=adj[f"{KEY}-{h}"],
            is_mean=a["is"],
            oos_mean=a["oos"],
            stress_cost_mean=st[h],
            edge=edge[h],
            tail_ok=a["gate"]["ok"],
            coverage_ok=cov_ok[h],
        )
        L.append(
            f"| {h} | {a['n']} | {fmt(a['mean'], k)} | [{fmt(a['lo'], k)}; {fmt(a['hi'], k)}] | {fmt(a['is'], k)} | {fmt(a['oos'], k)} | "
            f"{fmt(st[h], k)} | {adj[f'{KEY}-{h}']:.3f} | {fmt(a['mde'], k)} | {fmt(edge[h], k)} | {'так' if a['gate']['ok'] else 'ні'} | "
            f"{'так' if cov_ok[h] else 'ні'} | **{vs[h]}** |"
        )
    L.append(f"\nМітки плейбуку (`spread_vs_naked.p6_labels`): {p6_labels(vs['H1'], vs['H2'], vs['H3'])}\n")
    log("verdicts done", t0)

    L.append("## Чутливість H1 (усі варіанти зареєстровано заздалегідь; R спреду − R голого)\n")
    L += HDR
    for name, Pv, e in variants(D, P):
        tr, _ = pairs(D, e, Pv)
        epv = blocks(sorted({d for d, _ in e})) if e else {}
        L.append(row(name, tr, epv, len(e)))
        if name.startswith("половина спреду") or name == "модель «сторона»":
            L.append(row(f"{name}, H2 у % спота", tr, epv, len(e), "pnl_spot"))
        log(f"grid: {name}", t0)
    top = sorted(h1, key=lambda t: t["r"], reverse=True)[5:]
    L.append(row("H1 без 5 найкращих пар", top, ep, len(ent)))
    L.append(
        "\nЄдиний актив (BTC): пункт 5а по активах не застосовний; замість нього — сторони (кол / пут) вище. R — на капітал (дебет).\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("coverage", "full"), default="coverage")
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--closes-dir", default="~/.cache/tradedesk-agents/assistant_recs")
    ap.add_argument("--klines-dir", default="~/.cache/tradedesk-agents/macro_regimes/klines")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/p6_spread_vs_naked")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.perf_counter()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    D = load(Path(a.src_dir).expanduser(), Path(a.closes_dir).expanduser(), Path(a.klines_dir).expanduser(), t0)
    L = [f"# P6 / P3: дебетовий спред проти голого опціону, BTC (stage {a.stage}, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n"]
    c = coverage(L, D, t0)
    sanity(L, D, c["built"])
    if a.stage == "full":
        full(L, D, c["cov"], cache, t0)
    text = "\n".join(L)
    print(text)
    if a.out:
        Path(a.out).expanduser().write_text(text + "\n")
    log("done", t0)


if __name__ == "__main__":
    main()
