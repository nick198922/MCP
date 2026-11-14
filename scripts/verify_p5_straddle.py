"""Playbook rule P5' (IV Rank of DVOL <= 10 % -> buy a 60-90-day ATM straddle; H1 of the DataHawks radar LIB) on
Deribit BTC marks with Bybit costs.

IDEA-20261114-options-playbook, item 4.5; pre-registration: the card's "4.5. P5′ ... попередня реєстрація".
No network: the put-skew-v2 cache (``--src-dir``, read only: ``snapshots_v2.json`` daily 09:00 surfaces,
``delivery_btc_usd.json``, ``dvol_BTC.json``, loaded by ``verify_p1_condor_dvol.load``). Condition, construction, P&L,
hedge, gates and verdict: `tradedesk.analytics.straddle_p5`.

    uv run python scripts/verify_p5_straddle.py --stage coverage --out ~/.cache/tradedesk-agents/p5_straddle/coverage.md
    uv run python scripts/verify_p5_straddle.py --stage full --out ~/.cache/tradedesk-agents/p5_straddle/full.md
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
import verify_p10_calendar as vp10  # noqa: E402
from common_helpers import ymd_ms  # noqa: E402

from tradedesk.analytics.calendar_p10 import batch_raw_p, entry_cost, exec_ivs  # noqa: E402
from tradedesk.analytics.condor_25d import block_clusters, parametric_tail  # noqa: E402
from tradedesk.analytics.condor_dvol import cluster_bootstrap_diff  # noqa: E402
from tradedesk.analytics.dvol_filter import cvar, max_drawdown, mean_without_top  # noqa: E402
from tradedesk.analytics.option_condor import block_bootstrap_mean  # noqa: E402
from tradedesk.analytics.put_skew import choose_expiry  # noqa: E402
from tradedesk.analytics.s40_modules import HEDGE_FEE_SIDE  # noqa: E402
from tradedesk.analytics.straddle_p5 import (  # noqa: E402
    dvol_change,
    hedge_pnl,
    iv_rank_at,
    overlap_clusters,
    plan_straddle,
    sell_early,
    settle_straddle,
    two_sided_tail_gate,
    verdict,
)
from tradedesk.analytics.trend_spread import DAY_MS, holm_adjust  # noqa: E402

OOS = vp1.OOS
LAST_EXPIRY_MS = vp1.LAST_EXPIRY_MS
FIRST_SAT = int(datetime(2021, 1, 2, tzinfo=UTC).timestamp() * 1000)
RANK_FROM = int(datetime(2022, 2, 19, tzinfo=UTC).timestamp() * 1000)  # first Saturday whose window can hold 330 closes
COVERAGE_MIN = 0.60
EDGE_R = 0.10  # registered in the card (4.5) before stage 0: 10 % of the premium on a ~75-day bought straddle
THRESH = 0.10
N_BOOT = vp3.N_BOOT
SEED = vp3.SEED
MIN_COUNT = 5
PREV_RAW_P = {**vp10.PREV_RAW_P, "P10-H1": 0.588, "P10-H2": 0.323, "P10-H3": 0.0}  # p10_calendar/full.md, "Холм партії"
PREV_DEGENERATE = vp10.PREV_DEGENERATE | {"P10-H1", "P10-H2", "P10-H3"}  # P10: tail gate failed (card 4.4 result)
BASE: dict[str, Any] = {
    "weekday": 5,
    "field": "rank",
    "thr": THRESH,
    "target": 75.0,
    "lo": 60.0,
    "hi": 90.0,
    "band": 0.015,
    "arb": "replace",
    "hs": 0.015,
    "model": "mark",
    "exit": "expiry",
    "hedge": 0,  # 0 = no hedge, else rebalance every N days
}
H3P: dict[str, Any] = {**BASE, "hedge": 1}

log = vp3.log
rr, ci_r = vp3.rr, vp3.ci_r
_q = vp10._q
mean_r = vp10.mean_r


# ----------------------------------------------------------------------------- data and samples


def load(src: Path, t0: float) -> dict[str, Any]:
    D = vp1.load(src, t0)
    D["dvol_map"] = dict(D["dvol"])
    D["rank"] = {}
    return D


def rank(D: dict[str, Any], d: int) -> tuple[dict[str, Any] | None, str | None]:
    if d not in D["rank"]:
        D["rank"][d] = iv_rank_at(D["dvol"], d + 9 * 3_600_000)
    return D["rank"][d]


def entry_days(D: dict[str, Any], weekday: int) -> list[int]:
    """Entry days 2021+ whose 90-day horizon ends inside the cache (every window expiry has a delivery price)."""
    return [d for d in vp3.entry_days(D, weekday) if d + 9 * 3_600_000 + 90 * DAY_MS <= LAST_EXPIRY_MS]


def split_days(D: dict[str, Any], P: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"all": entry_days(D, P["weekday"]), "measured": [], "met": [], "not_met": []}
    why: Counter[str] = Counter()
    for d in out["all"]:
        v, w = rank(D, d)
        if v is None:
            why[(w or "").split(" for ")[0].split(":")[0]] += 1
            continue
        out["measured"].append(d)
        out["met" if v[P["field"]] <= P["thr"] else "not_met"].append(d)
    out["why"] = why
    return out


def episodes(days: list[int]) -> list[list[int]]:
    """Series of consecutive weekly entries."""
    out: list[list[int]] = []
    for d in sorted(days):
        if out and d - out[-1][-1] == 7 * DAY_MS:
            out[-1].append(d)
        else:
            out.append([d])
    return out


def exit_snap(D: dict[str, Any], d: int, expiry: int, after: int = 30, slack: int = 2) -> dict[str, Any] | None:
    for k in range(slack + 1):
        s = D["days"].get(d + (after + k) * DAY_MS)
        if s is not None and expiry in s["expiries"] and s["t_ms"] < expiry:
            return s
    return None


def build(D: dict[str, Any], d: int, P: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Entry plan and debit (no outcome)."""
    snap = D["days"].get(d)
    if snap is None:
        return None, "no surface"
    e = choose_expiry(snap, P["target"], P["lo"], P["hi"], rule="nearest")
    if e is None:
        return None, "no expiry in window"
    if e > LAST_EXPIRY_MS:
        return None, "expiry after cache end"
    if D["delivery"].get(ymd_ms(e)) is None:
        return None, "no delivery price"
    plan, why = plan_straddle(snap, e, band=P["band"], arb_rule=P["arb"])
    if plan is None:
        return None, why
    ivs, fb = exec_ivs(plan, P["model"], P["hs"])
    debit = entry_cost(plan, ivs)
    if debit is None:
        return None, "IV <= 1 %"
    b = {"day": d, "plan": plan, "debit": debit, "fallbacks": fb}
    if P["exit"] == "early30":
        xs = exit_snap(D, d, e)
        if xs is None:
            return None, "no exit surface"
        b["exit_day"] = xs["day"]
    return b, ""


def build_all(D: dict[str, Any], days: list[int], P: dict[str, Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    out, why = [], Counter()
    for d in days:
        b, w = build(D, d, P)
        if b is None:
            why[w] += 1
        else:
            out.append(b)
    return out, why


def path_of(D: dict[str, Any], plan: dict[str, Any]) -> tuple[list[tuple[int, float]], int]:
    """(t_ms, spot) of every surface strictly between entry and expiry; and the number of days without a surface."""
    pts, miss = [], 0
    k = plan["day"] + DAY_MS
    while k < plan["expiry"]["m"]:
        s = D["days"].get(k)
        if s is not None and plan["t_ms"] < s["t_ms"] < plan["expiry"]["m"]:
            pts.append((s["t_ms"], s["spot"]))
        elif s is None:
            miss += 1
        k += DAY_MS
    return pts, miss


def outcome(D: dict[str, Any], b: dict[str, Any], P: dict[str, Any]) -> dict[str, Any] | None:
    plan = b["plan"]
    e = plan["expiry"]["m"]
    if P["exit"] == "early30":
        res = sell_early(plan, b["debit"], D["days"][b["exit_day"]], hs=P["hs"], arb_rule=P["arb"])
        if res is None:
            return None
    else:
        res = settle_straddle(plan, b["debit"], D["delivery"][ymd_ms(e)])
    if P["hedge"]:
        pts, miss = path_of(D, plan)
        h = hedge_pnl(plan, pts, D["delivery"][ymd_ms(e)], fee_side=HEDGE_FEE_SIDE, every_days=P["hedge"])
        pnl = res["pnl"] + h["pnl"]
        res = {**res, "pnl": pnl, "r": pnl / b["debit"], "pnl_spot": pnl / plan["spot"], "hedge": h, "missing_days": miss}
    return {**res, "expiry": e, "spot": plan["spot"], "iv": plan["iv"]}


def trades(D: dict[str, Any], P: dict[str, Any], days: list[int]) -> list[dict[str, Any]]:
    built, _ = build_all(D, days, P)
    return sorted((t for t in (outcome(D, b, P) for b in built) if t is not None), key=lambda t: t["day"])


def clusters_of(tr: list[dict[str, Any]]) -> dict[int, int]:
    """Entry day -> overlap cluster (holding periods chain), for trades or built plans."""
    rows = sorted((t["day"], t["expiry"] if "expiry" in t else t["plan"]["expiry"]["m"]) for t in tr)
    return dict(zip([d for d, _ in rows], overlap_clusters(rows), strict=True))


def blocks13(D: dict[str, Any], weekday: int = 5) -> dict[int, int]:
    return block_clusters(entry_days(D, weekday), start_ms=FIRST_SAT, weeks=13)


# ----------------------------------------------------------------------------- stage 0


def coverage(L: list[str], D: dict[str, Any], t0: float) -> dict[str, Any]:
    P = BASE
    sp = split_days(D, P)
    sats, meas, met, nmet = sp["all"], sp["measured"], sp["met"], sp["not_met"]
    L.append(f"## Стадія 0: покриття (поріг {COVERAGE_MIN:.0%}; P&L не рахується)\n")
    L.append(f"- суботи {ymd_ms(sats[0])} … {ymd_ms(sats[-1])}: {len(sats)}; rank виміряно {len(meas)} (від {ymd_ms(meas[0])})")
    from_rank = [d for d in sats if d >= RANK_FROM]
    g1 = sum(1 for d in from_rank if d in set(meas)) / len(from_rank)
    L.append(
        f"- ворота (і): rank виміряно на {sum(1 for d in from_rank if d in set(meas))} / {len(from_rank)} субот від 2022-02-19 = **{g1:.1%}**; не виміряно (усі суботи): {dict(sp['why'])}"
    )
    eps = episodes(met)
    L.append(
        f"- rank ≤ {THRESH:.2f}: **{len(met)}** субот з {len(meas)} ({len(met) / len(meas):.1%}) у {len(eps)} епізодах (серіях сусідніх субот): "
        + ", ".join(f"{ymd_ms(e[0])}×{len(e)}" for e in eps)
    )
    L.append("")
    L.append("| вибірка / конструкція | днів | побудовано | покриття | причини пропуску |")
    L.append("|---|---|---|---|---|")
    rows: list[tuple[str, dict[str, Any], list[int]]] = [
        ("**H1: стреддл 60–90 д, rank ≤ 0.10**", P, met),
        ("**H2-контроль: той самий стреддл, rank > 0.10**", P, nmet),
        ("**H3: H1 з дельта-хеджем щодня**", H3P, met),
        *((f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, met) for h in (0.0, 0.03)),
        ("модель «сторона»", {**P, "model": "side"}, met),
        *((f"ціль DTE {x:.0f} д", {**P, "target": x}, met) for x in (60.0, 90.0)),
        ("правило (а): пропуск дня без заміни", {**P, "arb": "skip"}, met),
        ("вихід через 30 д (продаж на поверхні)", {**P, "exit": "early30"}, met),
        ("усі суботи з rank (без умови)", P, meas),
    ]
    for thr in (0.05, 0.20, 0.33):
        Pv = {**P, "thr": thr}
        rows.append((f"поріг rank {thr:.2f}", Pv, split_days(D, Pv)["met"]))
    Pp = {**P, "field": "percentile"}
    rows.append(("IV-перцентиль ≤ 0.10", Pp, split_days(D, Pp)["met"]))
    Pf = {**P, "weekday": 4}
    rows.append(("вхід у п'ятницю", Pf, split_days(D, Pf)["met"]))
    built_h1: list[dict[str, Any]] = []
    built_c: list[dict[str, Any]] = []
    for name, Pv, ds in rows:
        built, why = build_all(D, ds, Pv)
        cov = len(built) / len(ds) if ds else 0.0
        L.append(f"| {name} | {len(ds)} | {len(built)} | **{cov:.1%}** | {dict(why.most_common())} |")
        if name.startswith("**H1"):
            built_h1 = built
        elif name.startswith("**H2"):
            built_c = built
    log("coverage table done", t0)
    yrs = sorted({ymd_ms(d)[:4] for d in sats})
    L.append(
        "\n- по роках (суботи / rank виміряно / rank ≤ 0.10 / H1 побудовано): "
        + ", ".join(
            f"{y}: {sum(ymd_ms(d)[:4] == y for d in sats)}/{sum(ymd_ms(d)[:4] == y for d in meas)}/"
            f"{sum(ymd_ms(d)[:4] == y for d in met)}/{sum(ymd_ms(b['day'])[:4] == y for b in built_h1)}"
            for y in yrs
        )
    )
    cl = clusters_of(built_h1) if built_h1 else {}
    L.append(
        f"- H1: IS / OOS {sum(b['day'] < OOS for b in built_h1)} / {sum(b['day'] >= OOS for b in built_h1)}; "
        f"кластерів перекриття серед угод **{len(set(cl.values()))}**; H2-контроль: 13-тижневих блоків {len({blocks13(D)[b['day']] for b in built_c})}"
    )
    g2 = len(built_h1) / len(met) if met else 0.0
    gc = len(built_c) / len(nmet) if nmet else 0.0
    L.append(
        f"- ворота (іі): H1 побудовано {len(built_h1)} / {len(met)} = **{g2:.1%}**; контроль {len(built_c)} / {len(nmet)} = **{gc:.1%}**\n"
    )
    return {"split": sp, "h1": built_h1, "ctrl": built_c, "g1": g1 >= COVERAGE_MIN}


def sanity(L: list[str], D: dict[str, Any], cov: dict[str, Any]) -> None:
    L.append("## Перевірки до прогону (без P&L)\n")
    sp, h1, ctrl = cov["split"], cov["h1"], cov["ctrl"]
    rk = [rank(D, d)[0] for d in sp["measured"]]
    L.append(
        f"- rank: {_q([v['rank'] for v in rk])}, мін {min(v['rank'] for v in rk):.3f}, макс {max(v['rank'] for v in rk):.3f}; DVOL (десяткові) {_q([v['dvol'] for v in rk])}"
    )
    for name, built in (("H1", h1), ("контроль", ctrl)):
        if not built:
            continue
        dte = [(b["plan"]["expiry"]["m"] - b["plan"]["t_ms"]) / DAY_MS for b in built]
        L.append(f"- {name}: DTE {_q(dte, '{:.2f}')}; розподіл {dict(Counter(round(x) for x in dte).most_common(8))}")
        kf = [(b["plan"]["K"] - b["plan"]["F"]["m"]) / b["plan"]["spot"] for b in built]
        L.append(
            f"- {name}: K − форвард, частка спота {_q(kf, '{:+.4f}')}; дебет / спот {_q([b['debit'] / b['plan']['spot'] for b in built], '{:.4f}')}"
        )
        dv = [D["dvol_map"].get(b["day"] - DAY_MS) for b in built]
        dd = [b["plan"]["iv"] - v for b, v in zip(built, dv, strict=True) if v is not None]
        L.append(f"- {name}: mark IV стреддла − DVOL (свічка попереднього дня) {_q(dd, '{:+.3f}')}, n = {len(dd)}")
        L.append(f"- {name}: марки входу (а) {dict(Counter(b['plan']['legs'][0]['arb'] for b in built))}")
    if h1:
        fb = sum(exec_ivs(b["plan"], "side")[1] for b in h1)
        L.append(f"- модель «сторона»: ніг без принта своєї сторони {fb} з {2 * len(h1)} (замінено на mark + 1.5 в.п.)")
        miss = [path_of(D, b["plan"])[1] for b in h1]
        L.append(f"- днів без поверхні між входом і експірацією (хедж H3): усього {sum(miss)}, на угоду {_q(miss, '{:.0f}')}")
    fr_days = vom.fridays(vom.FIRST, vom.LAST)
    ok = sum(1 for f in fr_days if vom.plan(D, f, "h1")[0] is not None)
    L.append(f"- звірка з відомим числом: покриття кондора 10Δ / ±15 % (options-modules H1) = {ok} / {len(fr_days)} (у звіті 179 / 301)\n")


# ----------------------------------------------------------------------------- full


def analyse(L: list[str], name: str, tr: list[dict[str, Any]], ep: dict[int, int], elig: int) -> dict[str, Any]:
    s = vp3.summary(tr, ep)
    L += vp10.HDR
    L.append(vp1.table_row(f"{name}: усі", tr, ep, elig))
    L.append(vp1.table_row("IS (до 2024)", [t for t in tr if t["day"] < OOS], ep, elig))
    L.append(vp1.table_row("OOS (2024+)", [t for t in tr if t["day"] >= OOS], ep, elig))
    for y in sorted({ymd_ms(t["day"])[:4] for t in tr}):
        L.append(vp1.table_row(f"рік {y}", [t for t in tr if ymd_ms(t["day"])[:4] == y], ep, elig))
    rs = [t["r"] for t in tr]
    bb = block_bootstrap_mean(rs, block=4, n_boot=N_BOOT, seed=SEED)
    lo, hi = min(s["ci"][0], bb["ci"][0]), max(s["ci"][1], bb["ci"][1])
    L.append(f"\n- блоковий бутстреп (блок 4 угоди): {rr(bb['mean'])} {ci_r(bb['ci'])}, p {bb['p']:.3f}")
    L.append(f"- CI для вердикту (об'єднання кластерного й блокового): [{lo:+.3f}; {hi:+.3f}]")
    first = {}
    for t in tr:
        first.setdefault(ep[t["day"]], t)
    one = list(first.values())
    L.append(
        f"- одна угода на кластер (перший вхід): n = {len(one)}, середній R {rr(mean_r(one))}; R: "
        + ", ".join(f"{ymd_ms(t['day'])} {t['r']:+.2f}" for t in one)
    )
    halves = []
    for k in (0, 1):
        sub = rs[k::2]
        bk = block_bootstrap_mean(sub, block=2, n_boot=N_BOOT, seed=SEED)
        halves.append((sum(1 for r in sub if r <= 0), len(sub)))
        L.append(f"- половина {k + 1} (кожна друга угода): n = {len(sub)}, {rr(bk.get('mean'))} {ci_r(bk.get('ci'))}")
    g = two_sided_tail_gate(rs, [ep[t["day"]] for t in tr], min_count=MIN_COUNT)
    gl, gw = g["loss"], g["win"]
    L.append(
        f"- ворота хвоста: збиткових (R ≤ 0) {gl['n_loss']} з {gl['n']} у {gl['n_loss_clusters']} з {gl['n_clusters']} кластерів, "
        f"P(ресемпл без збиткового) {gl['p_no_loss_resample']:.3f}; виграшних (R ≥ 0) {gw['n_loss']} у {gw['n_loss_clusters']} кластерах, "
        f"P(ресемпл без виграшного) {gw['p_no_loss_resample']:.3f} → **{'пройдено' if g['ok'] else 'НЕ пройдено'}**"
    )
    pt = max((parametric_tail(rs, k_eff=k, n_eff=n) for k, n in halves), key=lambda x: x["q_up"])
    L.append(
        f"- параметричний хвіст: частка збиткових {pt['q_obs']:.1%}, верхня межа {pt['q_up']:.1%}; виграш {rr(pt['win_mean'])}, "
        f"збиток {rr(pt['loss_mean'])} → стрес-середнє {rr(pt['stress_mean'])}"
    )
    L.append(
        f"- хвіст: найгірший R {min(rs):+.3f}, найкращий {max(rs):+.3f}, CVaR 5 % {cvar(rs):+.3f}, просадка суми R {max_drawdown(rs):.2f}; "
        f"без 5 найкращих {mean_without_top(rs, 5):+.3f}; MDE {s['mde']:.3f} R\n"
    )
    ins = [t for t in tr if t["day"] < OOS]
    oos = [t for t in tr if t["day"] >= OOS]
    return {**s, "lo": lo, "hi": hi, "gate": g, "ptail": pt, "is": mean_r(ins), "oos": mean_r(oos)}


def full(L: list[str], D: dict[str, Any], cov: dict[str, Any], cache: Path, t0: float) -> None:
    P = BASE
    sp = cov["split"]
    met, nmet = sp["met"], sp["not_met"]
    h1 = trades(D, P, met)
    (cache / "trades_h1.json").write_text(json.dumps(h1, default=str))
    cl1 = clusters_of(h1)
    L.append("## H1: купівля ATM-стреддла 60–90 д при IV rank ≤ 0.10, до експірації, без хеджу\n")
    a1 = analyse(L, "H1", h1, cl1, len(met))
    cov_ok = {"H1": cov["g1"] and len(h1) / max(len(met), 1) >= COVERAGE_MIN}
    log("H1 done", t0)

    ctrl = trades(D, P, nmet)
    (cache / "trades_ctrl.json").write_text(json.dumps(ctrl, default=str))
    b13 = blocks13(D)
    L.append("## H2: rank ≤ 0.10 проти rank > 0.10 (той самий стреддл)\n")
    a_c = analyse(L, "контроль (rank > 0.10, кластер = 13-тижневий блок)", ctrl, b13, len(nmet))
    both = h1 + ctrl
    labels = [("a", cl1[t["day"]]) for t in h1] + [("b", b13[t["day"]]) for t in ctrl]
    in_a = [True] * len(h1) + [False] * len(ctrl)
    d2 = cluster_bootstrap_diff([t["r"] for t in both], labels, in_a, n_boot=N_BOOT, seed=SEED)
    is2 = mean_r([t for t in h1 if t["day"] < OOS]) - mean_r([t for t in ctrl if t["day"] < OOS])
    oos2 = mean_r([t for t in h1 if t["day"] >= OOS]) - mean_r([t for t in ctrl if t["day"] >= OOS])
    L.append(f"- різниця R: {rr(d2['diff'])} {ci_r(d2['ci'])}, p {d2['p']:.3f}; IS {rr(is2)}, OOS {rr(oos2)}\n")
    a2 = {
        "mean": d2["diff"],
        "lo": d2["ci"][0],
        "hi": d2["ci"][1],
        "p": d2["p"],
        "is": is2,
        "oos": oos2,
        "mde": 2.8 * d2["se"],
        "n": len(both),
    }
    a2["gate"] = {"ok": a1["gate"]["ok"] and a_c["gate"]["ok"], "n_loss": a1["gate"]["n_loss"] + a_c["gate"]["n_loss"]}
    a2["ptail"] = a_c["ptail"]
    cov_ok["H2"] = cov_ok["H1"] and len(ctrl) / max(len(nmet), 1) >= COVERAGE_MIN
    log("H2 done", t0)

    h3 = trades(D, H3P, met)
    (cache / "trades_h3.json").write_text(json.dumps(h3, default=str))
    L.append("## H3: той самий стреддл з щоденним дельта-хеджем (BTC, IV входу)\n")
    a3 = analyse(L, "H3", h3, clusters_of(h3), len(met))
    hg = [t["hedge"] for t in h3]
    if hg:
        L.append(
            f"- хедж: валовий P&L / дебет {_q([t['hedge']['gross'] / t['debit'] for t in h3], '{:+.3f}')}, витрати / дебет "
            f"{_q([h['fees'] / t['debit'] for h, t in zip(hg, h3, strict=True)], '{:.3f}')}, ребалансів {_q([h['rebalances'] for h in hg], '{:.0f}')}\n"
        )
    cov_ok["H3"] = cov["g1"] and len(h3) / max(len(met), 1) >= COVERAGE_MIN
    log("H3 done", t0)

    st = {
        "H1": mean_r(trades(D, {**P, "hs": 0.03}, met)),
        "H2": mean_r(trades(D, {**P, "hs": 0.03}, met)) - mean_r(trades(D, {**P, "hs": 0.03}, nmet)),
        "H3": mean_r(trades(D, {**H3P, "hs": 0.03}, met)),
    }
    res = {"H1": a1, "H2": a2, "H3": a3}
    raw = {**PREV_RAW_P, **{f"P5-{h}": a["p"] for h, a in res.items()}}
    deg = PREV_DEGENERATE | {f"P5-{h}" for h, a in res.items() if not a["gate"]["ok"]}
    pv = batch_raw_p(raw, deg)
    adj = holm_adjust(pv)
    L.append(f"## Холм партії пункту 4 ({len(pv)} тестів; вироджені → 1.0: {sorted(deg)})\n")
    L.append("- " + ", ".join(f"{k}: сирий {raw[k]:.3f} → у Холмі {pv[k]:.3f} → {adj[k]:.3f}" for k in sorted(pv)) + "\n")
    L.append("## Вердикти (механічно за реєстрацією, `straddle_p5.verdict`)\n")
    L.append(
        "| гіпотеза | n | середнє R | CI | IS | OOS | стрес 3 в.п. | Холм p | MDE | ворота хвоста | ворота покриття | стрес-хвіст | вердикт |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for h, a in res.items():
        v = verdict(
            mean=a["mean"],
            lo=a["lo"],
            hi=a["hi"],
            holm_p=adj[f"P5-{h}"],
            is_mean=a["is"],
            oos_mean=a["oos"],
            stress_cost_mean=st[h],
            edge=EDGE_R,
            tail_ok=a["gate"]["ok"],
            coverage_ok=cov_ok[h],
        )
        L.append(
            f"| {h} | {a['n']} | {rr(a['mean'])} | [{a['lo']:+.3f}; {a['hi']:+.3f}] | {rr(a['is'])} | {rr(a['oos'])} | {rr(st[h])} | "
            f"{adj[f'P5-{h}']:.3f} | {a['mde']:.3f} | {'так' if a['gate']['ok'] else 'ні'} ({a['gate']['n_loss']}) | "
            f"{'так' if cov_ok[h] else 'ні'} | {rr(a['ptail']['stress_mean'])} | **{v}** |"
        )
    L.append(f"\nEDGE_R = {EDGE_R:.2f} R (зареєстровано до стадії 0).\n")
    log("verdict done", t0)
    sensitivity(L, D, h1, met, t0)
    dvol_moves(L, D, sp)


def sensitivity(L: list[str], D: dict[str, Any], h1: list[dict[str, Any]], met: list[int], t0: float) -> None:
    P = BASE
    L.append("## Чутливість H1 (усі варіанти зареєстровано заздалегідь; кластер = компонента перекриття варіанта)\n")
    L += vp10.HDR
    grid: list[tuple[str, dict[str, Any], list[int] | None]] = [
        *((f"поріг rank {thr:.2f}", {**P, "thr": thr}, None) for thr in (0.05, 0.20, 0.33)),
        ("IV-перцентиль ≤ 0.10", {**P, "field": "percentile"}, None),
        *((f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, met) for h in (0.0, 0.03)),
        ("модель «сторона»", {**P, "model": "side"}, met),
        *((f"ціль DTE {x:.0f} д", {**P, "target": x}, met) for x in (60.0, 90.0)),
        ("правило (а): пропуск дня без заміни", {**P, "arb": "skip"}, met),
        ("вихід через 30 д (продаж на поверхні)", {**P, "exit": "early30"}, met),
        ("вхід у п'ятницю", {**P, "weekday": 4}, None),
        ("H3 з ребалансом щотижня", {**H3P, "hedge": 7}, met),
    ]
    for name, Pv, ds in grid:
        days = ds if ds is not None else split_days(D, Pv)["met"]
        tr = trades(D, Pv, days)
        L.append(vp1.table_row(name, tr, clusters_of(tr) if tr else {}, len(days)))
        log(f"grid: {name}", t0)
    meas = split_days(D, P)["measured"]
    allr = trades(D, P, meas)
    L.append(vp1.table_row("стреддл на всіх суботах з rank (без умови; кластер = 13-тижневий блок)", allr, blocks13(D), len(meas)))
    cl1 = clusters_of(h1)
    first: dict[int, dict[str, Any]] = {}
    for t in h1:
        first.setdefault(cl1[t["day"]], t)
    L.append(vp1.table_row("одна угода на кластер (перший вхід)", list(first.values()), cl1, len(met)))
    top = sorted(h1, key=lambda t: t["r"], reverse=True)[5:]
    L.append(vp1.table_row("H1 без 5 найкращих угод", sorted(top, key=lambda t: t["day"]), cl1, len(met)))
    L.append(
        "\nЄдиний актив (BTC): пункт 5а не застосовний; ETH не покрито кешем поверхонь. R — на дебет (премія + комісії = "
        "максимальний збиток купленого стреддла).\n"
    )


def dvol_moves(L: list[str], D: dict[str, Any], sp: dict[str, Any]) -> None:
    """Descriptive (second part of H1 LIB): DVOL change 14 / 30 days after a Saturday with rank <= 0.10 vs the rest."""
    L.append("## Описово: зміна DVOL після суботи з rank ≤ 0.10 (без вердикту, поза Холмом)\n")
    b13 = blocks13(D)
    for h in (14, 30):
        a = [(dvol_change(D["dvol_map"], d + 9 * 3_600_000, h), d) for d in sp["met"]]
        b = [(dvol_change(D["dvol_map"], d + 9 * 3_600_000, h), d) for d in sp["not_met"]]
        a = [(v, d) for v, d in a if v is not None]
        b = [(v, d) for v, d in b if v is not None]
        vals = [v for v, _ in a] + [v for v, _ in b]
        lab = [("a", b13[d]) for _, d in a] + [("b", b13[d]) for _, d in b]
        dd = cluster_bootstrap_diff(vals, lab, [True] * len(a) + [False] * len(b), n_boot=N_BOOT, seed=SEED)
        L.append(
            f"- +{h} д: rank ≤ 0.10 {statistics.fmean(v for v, _ in a):+.4f} (n = {len(a)}), решта {statistics.fmean(v for v, _ in b):+.4f} "
            f"(n = {len(b)}); різниця {rr(dd['diff'])} {ci_r(dd['ci'])} (кластер = 13-тижневий блок у кожній групі)"
        )
    L.append("")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("coverage", "full"), default="coverage")
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/p5_straddle")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.perf_counter()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    D = load(Path(a.src_dir).expanduser(), t0)
    L = [f"# P5′: IV rank DVOL ≤ 10 % → купівля ATM-стреддла 60–90 д, BTC (stage {a.stage}, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n"]
    cov = coverage(L, D, t0)
    sanity(L, D, cov)
    log("coverage and sanity done", t0)
    if a.stage == "full":
        full(L, D, cov, cache, t0)
    text = "\n".join(L)
    print(text)
    if a.out:
        Path(a.out).expanduser().write_text(text + "\n")
    log("done", t0)


if __name__ == "__main__":
    main()
