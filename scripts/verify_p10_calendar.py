"""Playbook rule P10 (front-month IV inversion before FOMC / CPI -> calendar / diagonal) on Deribit BTC marks, Bybit costs.

IDEA-20261114-options-playbook, item 4.4; pre-registration: the card's "4.4. P10 ... попередня реєстрація".
No network: the put-skew-v2 cache (``--src-dir``, read only: ``snapshots_v2.json`` daily 09:00 surfaces,
``delivery_btc_usd.json``, ``dvol_BTC.json``, loaded by ``verify_p1_condor_dvol.load``) and the macro events CSV
``docs/verification/2026-10-15-us-macro-events.csv``. Condition, construction, arbitrage rule, P&L and verdict:
`tradedesk.analytics.calendar_p10`.

    uv run python scripts/verify_p10_calendar.py --stage coverage --out ~/.cache/tradedesk-agents/p10_calendar/coverage.md
    uv run python scripts/verify_p10_calendar.py --stage full --out ~/.cache/tradedesk-agents/p10_calendar/full.md
"""

from __future__ import annotations

import argparse
import csv
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

from tradedesk.analytics.calendar_p10 import (  # noqa: E402
    batch_raw_p,
    clean_iv,
    entry_cost,
    event_times_ms,
    exec_ivs,
    inversion_at,
    plan_calendar,
    plan_diagonal,
    settle_at_front,
    verdict,
)
from tradedesk.analytics.condor_25d import block_clusters, parametric_tail, tail_gate  # noqa: E402
from tradedesk.analytics.condor_dvol import cluster_bootstrap_diff  # noqa: E402
from tradedesk.analytics.dvol_filter import cvar, max_drawdown, mean_without_top  # noqa: E402
from tradedesk.analytics.option_condor import block_bootstrap_mean  # noqa: E402
from tradedesk.analytics.trend_spread import DAY_MS, holm_adjust  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
EVENTS = REPO / "docs" / "verification" / "2026-10-15-us-macro-events.csv"
OOS = vp1.OOS
LAST_EXPIRY_MS = vp1.LAST_EXPIRY_MS
COVERAGE_MIN = 0.60
EDGE_R = 0.10  # registered in the card before stage 0 (P3 had 0.15 R on a ~13-day debit spread; ~6-day trade -> 0.10)
N_BOOT = vp3.N_BOOT
SEED = vp3.SEED
MIN_LOSSES = 5
# Raw two-sided p of the earlier rules of the item-4 batch, from the saved full outputs (three decimals):
# p3_trend_spread/full.md (H1-H3 tables), p1_condor_dvol/full.md, p12_condor_25d/full.md ("Холм партії").
PREV_RAW_P = {
    "P3-H1": 0.042,
    "P3-H2": 0.013,
    "P3-H3": 0.842,
    "P1-H1": 0.0,
    "P1-H2": 0.0,
    "P1-H3": 0.0,
    "P12-H1": 0.001,
    "P12-H2": 0.011,
    "P12-H3": 0.0,
}
# Card 4.4 (б): a hypothesis whose sample failed the tail gate counts as p = 1.0 (P1' H1-H3: no loss; P12-H2: 2 losses).
PREV_DEGENERATE = {"P1-H1", "P1-H2", "P1-H3", "P12-H2"}
BASE: dict[str, Any] = {
    "weekday": 5,
    "threshold": 0.015,
    "hs": 0.015,
    "model": "mark",
    "band": 0.015,
    "month_interp": False,
    "arb": "replace",
    "exit_slack": 0,
    "kind": "calendar",
    "delta": 0.25,
    "qty": 0.5,
    "events": "all",
}
H3P: dict[str, Any] = {**BASE, "kind": "diagonal"}

log = vp3.log
rr, ci_r, pc = vp3.rr, vp3.ci_r, vp3.pc


# ----------------------------------------------------------------------------- data and samples


def load(src: Path, t0: float) -> dict[str, Any]:
    D = vp1.load(src, t0)
    with EVENTS.open() as fh:
        rows = list(csv.DictReader(fh))
    D["events"] = {"all": event_times_ms(rows), "fomc": event_times_ms(rows, ("fomc",)), "cpi": event_times_ms(rows, ("cpi",))}
    D["dvol_map"] = dict(D["dvol"])
    log(f"events: FOMC {len(D['events']['fomc'])}, CPI {len(D['events']['cpi'])}", t0)
    return D


def entry_days(D: dict[str, Any], weekday: int) -> list[int]:
    return vp3.entry_days(D, weekday)


def event_days(D: dict[str, Any], P: dict[str, Any]) -> list[int]:
    """Entry days with an FOMC / CPI event in [09:00, 09:00 + 8 d) by the calendar alone (surface or not)."""
    ev = D["events"][P["events"]]
    out = []
    for d in entry_days(D, P["weekday"]):
        t = d + 9 * 3_600_000
        if any(0 <= e - t < 8 * DAY_MS for e in ev):
            out.append(d)
    return out


def condition(D: dict[str, Any], d: int, P: dict[str, Any]) -> dict[str, Any] | None:
    snap = D["days"].get(d)
    return None if snap is None else inversion_at(snap, D["events"][P["events"]], threshold=P["threshold"])


def split_days(D: dict[str, Any], P: dict[str, Any]) -> dict[str, list[int]]:
    """Event days -> measured / met / not met / not measured (with the reason)."""
    out: dict[str, list[int]] = {"event": event_days(D, P), "measured": [], "met": [], "not_met": []}
    why: Counter[str] = Counter()
    for d in out["event"]:
        inv = condition(D, d, P)
        if inv is None or inv["met"] is None or inv["state"] != "measured":
            why["no surface" if inv is None else inv["state"]] += 1
            continue
        out["measured"].append(d)
        out["met" if inv["met"] else "not_met"].append(d)
    out["why"] = why  # type: ignore[assignment]
    return out


def exit_day(D: dict[str, Any], plan: dict[str, Any], slack: int) -> int | None:
    d0 = (plan["expiry"]["f"] // DAY_MS) * DAY_MS
    for k in range(slack + 1):
        s = D["days"].get(d0 + k * DAY_MS)
        if s is not None and plan["expiry"]["m"] in s["expiries"] and s["t_ms"] >= plan["expiry"]["f"]:
            return d0 + k * DAY_MS
    return None


def build(D: dict[str, Any], d: int, P: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Entry plan, debit and exit surface (no outcome)."""
    snap = D["days"].get(d)
    if snap is None:
        return None, "no surface"
    inv = condition(D, d, P)
    if inv is None or inv["state"] != "measured":
        return None, "condition not measured"
    if inv["front"] > LAST_EXPIRY_MS:
        return None, "expiry after cache end"
    if D["delivery"].get(ymd_ms(inv["front"])) is None:
        return None, "no delivery price"
    if P["kind"] == "calendar":
        plan, why = plan_calendar(snap, inv, band=P["band"], month_interp=P["month_interp"], arb_rule=P["arb"])
    else:
        plan, why = plan_diagonal(snap, inv, delta=P["delta"], qty=P["qty"], band=P["band"], arb_rule=P["arb"])
    if plan is None:
        return None, why
    ivs, fb = exec_ivs(plan, P["model"], P["hs"])
    debit = entry_cost(plan, ivs)
    if debit is None:
        return None, "debit <= 0 or IV <= 1 %"
    xd = exit_day(D, plan, P["exit_slack"])
    if xd is None:
        return None, "no exit surface"
    return {"day": d, "plan": plan, "debit": debit, "fallbacks": fb, "exit_day": xd}, ""


def build_all(D: dict[str, Any], days: list[int], P: dict[str, Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    out, why = [], Counter()
    for d in days:
        b, w = build(D, d, P)
        if b is None:
            why[w] += 1
        else:
            out.append(b)
    return out, why


def outcome(D: dict[str, Any], b: dict[str, Any], P: dict[str, Any]) -> dict[str, Any] | None:
    plan = b["plan"]
    res, _ = settle_at_front(
        plan, b["debit"], D["delivery"][ymd_ms(plan["expiry"]["f"])], D["days"][b["exit_day"]], hs=P["hs"], arb_rule=P["arb"]
    )
    if res is None:
        return None
    return {**res, "expiry": plan["expiry"]["f"], "spot": plan["spot"], "diff": plan["diff"], "event_ms": plan["event_ms"]}


def trades(D: dict[str, Any], P: dict[str, Any], days: list[int]) -> list[dict[str, Any]]:
    built, _ = build_all(D, days, P)
    return [t for t in (outcome(D, b, P) for b in built) if t is not None]


def blocks(D: dict[str, Any], P: dict[str, Any]) -> dict[int, int]:
    days = entry_days(D, P["weekday"])
    return block_clusters(days, start_ms=days[0], weeks=4)


# ----------------------------------------------------------------------------- stage 0


def _q(xs: list[float], f: str = "{:.3f}") -> str:
    xs = sorted(xs)
    if not xs:
        return "—"
    return f"медіана {f.format(statistics.median(xs))} [p10 {f.format(xs[len(xs) // 10])}; p90 {f.format(xs[9 * len(xs) // 10])}]"


def coverage(L: list[str], D: dict[str, Any], t0: float) -> dict[str, Any]:
    P = BASE
    sp = split_days(D, P)
    sats = entry_days(D, P["weekday"])
    ev, meas, met, nmet = sp["event"], sp["measured"], sp["met"], sp["not_met"]
    L.append(f"## Стадія 0: покриття (поріг {COVERAGE_MIN:.0%}; P&L не рахується)\n")
    L.append(f"- суботи {ymd_ms(sats[0])} … {ymd_ms(sats[-1])}: {len(sats)}; з подією FOMC / CPI у [09:00; +8 д): **{len(ev)}**")
    g1 = len(meas) / len(ev) if ev else 0.0
    L.append(
        f"- ворота (і): умова виміряна на {len(meas)} / {len(ev)} = **{g1:.1%}**; не виміряна: {dict(sp['why'].most_common())}"  # type: ignore[union-attr]
    )
    L.append(
        f"- умова виконана (інверсія ≥ +1.5 п.): **{len(met)}** з {len(meas)} виміряних ({len(met) / max(len(meas), 1):.1%}); не виконана {len(nmet)}\n"
    )
    L.append("| вибірка / конструкція | днів | побудовано | покриття | причини пропуску |")
    L.append("|---|---|---|---|---|")
    rows: list[tuple[str, dict[str, Any], list[int]]] = [
        ("**H1: календар ATM, умова виконана**", P, met),
        ("**H2-контроль: календар, подія без інверсії**", P, nmet),
        ("**H3: діагональ 25Δ, ½ фронту, умова виконана**", H3P, met),
        *((f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, met) for h in (0.0, 0.03)),
        ("модель «сторона»", {**P, "model": "side"}, met),
        ("місячна нога на інтерпольованій марці", {**P, "month_interp": True}, met),
        ("правило (а): пропуск дня без заміни", {**P, "arb": "skip"}, met),
        ("поверхня виходу з запасом ≤ 2 д", {**P, "exit_slack": 2}, met),
        ("діагональ, об'єм фронту 0.75", {**H3P, "qty": 0.75}, met),
        ("діагональ, |Δ| 0.40", {**H3P, "delta": 0.40}, met),
        ("календар на всіх виміряних суботах з подією", P, meas),
    ]
    for th, name in ((0.010, "+1.0 п."), (0.020, "+2.0 п."), (1e-9, "будь-яка > 0")):
        Pv = {**P, "threshold": th}
        rows.append((f"поріг інверсії {name}", Pv, split_days(D, Pv)["met"]))
    Pf = {**P, "weekday": 4}
    rows.append(("вхід у п'ятницю", Pf, split_days(D, Pf)["met"]))
    for k in ("fomc", "cpi"):
        Pk = {**P, "events": k}
        rows.append((f"лише {k.upper()} (розвідувально)", Pk, split_days(D, Pk)["met"]))
    built_h1: list[dict[str, Any]] = []
    built_h3: list[dict[str, Any]] = []
    built_c: list[dict[str, Any]] = []
    for name, Pv, ds in rows:
        built, why = build_all(D, ds, Pv)
        cov = len(built) / len(ds) if ds else 0.0
        L.append(f"| {name} | {len(ds)} | {len(built)} | **{cov:.1%}** | {dict(why.most_common())} |")
        if name.startswith("**H1"):
            built_h1 = built
        elif name.startswith("**H3"):
            built_h3 = built
        elif name.startswith("**H2"):
            built_c = built
    log("coverage table done", t0)
    yrs = sorted({ymd_ms(d)[:4] for d in sats})
    L.append(
        "\n- по роках (з подією / виміряно / умова / H1 побудовано): "
        + ", ".join(
            f"{y}: {sum(ymd_ms(d)[:4] == y for d in ev)}/{sum(ymd_ms(d)[:4] == y for d in meas)}/"
            f"{sum(ymd_ms(d)[:4] == y for d in met)}/{sum(ymd_ms(b['day'])[:4] == y for b in built_h1)}"
            for y in yrs
        )
    )
    cl = blocks(D, P)
    L.append(
        f"- H1: IS / OOS {sum(b['day'] < OOS for b in built_h1)} / {sum(b['day'] >= OOS for b in built_h1)}; "
        f"4-тижневих блоків серед угод {len({cl[b['day']] for b in built_h1})}; H2: групи {len(built_h1)} / {len(built_c)}; H3: {len(built_h3)}"
    )
    g2 = len(built_h1) / len(met) if met else 0.0
    L.append(f"- ворота (іі): H1 побудовано {len(built_h1)} / {len(met)} = **{g2:.1%}**\n")
    arb_stats(L, D, built_h1, built_h3, built_c, rows)
    return {"split": sp, "h1": built_h1, "h3": built_h3, "ctrl": built_c}


def arb_stats(L: list[str], D: dict[str, Any], h1: list, h3: list, ctrl: list, rows: list) -> None:
    L.append("## Статичний арбітраж марок (вимога (а); без P&L)\n")
    for name, built in (("H1", h1), ("H2-контроль", ctrl), ("H3", h3)):
        ent: Counter[str] = Counter()
        ext: Counter[str] = Counter()
        for b in built:
            seen = set()
            for leg in b["plan"]["legs"]:
                key = (leg["exp"], leg["K"])
                if key not in seen:
                    seen.add(key)
                    ent[leg["arb"]] += 1
            mx = D["days"][b["exit_day"]]["expiries"][b["plan"]["expiry"]["m"]]
            for K in {leg["K"] for leg in b["plan"]["legs"] if leg["exp"] == "m"}:
                ext[clean_iv(mx, K, at_exit=True)[1]] += 1
        L.append(f"- {name}: марки входу {dict(ent)}; марки виходу (місяць) {dict(ext)} (raw = порушення без заміни, лишено й позначено)")
    skipped = {name: why.get("arbitrage in marks", 0) for name, Pv, ds in rows[:3] for why in [build_all(D, ds, Pv)[1]]}
    L.append(f"- днів пропущено через арбітраж у марках входу (правило «заміна»): {skipped}\n")


def sanity(L: list[str], D: dict[str, Any], cov: dict[str, Any]) -> None:
    L.append("## Перевірки до прогону (без P&L)\n")
    sp, h1 = cov["split"], cov["h1"]
    ev_y = Counter(ymd_ms(e)[:4] for e in D["events"]["all"] if vp3.FIRST <= e <= LAST_EXPIRY_MS)
    L.append("- подій FOMC + CPI по роках (у межах кешу): " + ", ".join(f"{y}: {n}" for y, n in sorted(ev_y.items())))
    invs = [condition(D, d, BASE) for d in sp["measured"]]
    diffs = [i["diff"] * 100 for i in invs]
    L.append(
        f"- інверсія ATM фронт − місяць (п.), виміряні суботи з подією: {_q(diffs, '{:+.2f}')}, частка ≥ +1.5: {sum(x >= 1.5 for x in diffs) / max(len(diffs), 1):.1%}"
    )
    fd = [(i["front"] - i["t_ms"]) / DAY_MS for i in invs]
    md = [(i["month"] - i["t_ms"]) / DAY_MS for i in invs]
    L.append(
        f"- DTE фронту: {dict(sorted(Counter(round(x, 2) for x in fd).items()))}; DTE місяця: {dict(sorted(Counter(round(x, 2) for x in md).items()))}"
    )
    L.append(f"- ATM фронту: {_q([i['front_atm'] for i in invs])}; ATM місяця: {_q([i['month_atm'] for i in invs])} (десяткові)")
    dv = [D["dvol_map"].get(i["t_ms"] // DAY_MS * DAY_MS - DAY_MS) for i in invs]
    dd = [v - i["month_atm"] for v, i in zip(dv, invs, strict=True) if v is not None]
    L.append(f"- DVOL (свічка попереднього дня) − ATM місяця: {_q(dd, '{:+.3f}')}, n = {len(dd)} (у 4.2: DVOL − ATM ≈ 30 д медіана +0.041)")
    if h1:
        kf = [(b["plan"]["K"] - b["plan"]["F"]["f"]) / b["plan"]["spot"] for b in h1]
        L.append(f"- K − форвард фронту, частка спота: {_q(kf, '{:+.4f}')} (допуск ±0.015)")
        L.append(f"- дебет (з комісіями входу) / спот: {_q([b['debit'] / b['plan']['spot'] for b in h1], '{:.4f}')}")
        fb = sum(exec_ivs(b["plan"], "side")[1] for b in h1)
        L.append(f"- модель «сторона»: ніг без принта своєї сторони {fb} з {4 * len(h1)} (замінено на mark ± 1.5 в.п.)")
    if cov["h3"]:
        h3 = cov["h3"]
        L.append(
            "- H3: місячні страйки від спота "
            + _q([(b["plan"]["K"]["mc"] - b["plan"]["spot"]) / b["plan"]["spot"] for b in h3], "{:+.3f}")
            + " (кол), фронт − місяць (кол) "
            + _q([(b["plan"]["K"]["fc"] - b["plan"]["K"]["mc"]) / b["plan"]["spot"] for b in h3], "{:+.4f}")
            + f"; дебет / спот {_q([b['debit'] / b['plan']['spot'] for b in h3], '{:.4f}')}"
        )
    fr_days = vom.fridays(vom.FIRST, vom.LAST)
    ok = sum(1 for f in fr_days if vom.plan(D, f, "h1")[0] is not None)
    L.append(f"- звірка з відомим числом: покриття кондора 10Δ / ±15 % (options-modules H1) = {ok} / {len(fr_days)} (у звіті 179 / 301)\n")


# ----------------------------------------------------------------------------- full


HDR = [
    "| варіант | n | кластерів | середній R (на дебет) | 95 % CI (кластери) | p | % спота (на номінал) | частка R > 0 | покриття |",
    "|---|---|---|---|---|---|---|---|---|",
]


def analyse(
    L: list[str], name: str, tr: list[dict[str, Any]], ep: dict[int, int], elig: int, loss_ref: list[float] | None
) -> dict[str, Any]:
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
    L.append(f"\n- блоковий бутстреп (блок 4 угоди): {rr(bb['mean'])} {ci_r(bb['ci'])}, p {bb['p']:.3f}")
    L.append(f"- CI для вердикту (об'єднання кластерного й блокового): [{lo:+.3f}; {hi:+.3f}]")
    halves = []
    for k in (0, 1):
        sub = rs[k::2]
        bk = block_bootstrap_mean(sub, block=2, n_boot=N_BOOT, seed=SEED)
        halves.append((sum(1 for r in sub if r <= 0), len(sub)))
        L.append(f"- неперекривна половина {k + 1} (кожна друга угода): n = {len(sub)}, {rr(bk.get('mean'))} {ci_r(bk.get('ci'))}")
    g = tail_gate(rs, [ep[t["day"]] for t in tr], min_losses=MIN_LOSSES)
    L.append(
        f"- ворота хвоста: збиткових (R ≤ 0) {g['n_loss']} з {g['n']} у {g['n_loss_clusters']} з {g['n_clusters']} кластерів; "
        f"P(ресемпл без збиткового кластера) {g['p_no_loss_resample']:.3f} → **{'пройдено' if g['ok'] else 'НЕ пройдено'}**"
    )
    pt = max((parametric_tail(rs, k_eff=k, n_eff=n, loss_ref=loss_ref) for k, n in halves), key=lambda x: x["q_up"])
    L.append(
        f"- параметричний хвіст: частка збиткових {pt['q_obs']:.1%}, верхня межа {pt['q_up']:.1%}; виграш {rr(pt['win_mean'])}, "
        f"збиток {rr(pt['loss_mean'])} → стрес-середнє {rr(pt['stress_mean'])}"
    )
    L.append(
        f"- хвіст: найгірший R {min(rs):+.3f}, CVaR 5 % {cvar(rs):+.3f}, просадка суми R {max_drawdown(rs):.2f}; без 5 найкращих "
        f"{mean_without_top(rs, 5):+.3f}; MDE {s['mde']:.3f} R; позначених марок виходу (raw) {sum(t['exit_arb'].count('raw') for t in tr)}\n"
    )
    oos = [t for t in tr if t["day"] >= OOS]
    ins = [t for t in tr if t["day"] < OOS]
    return {
        **s,
        "lo": lo,
        "hi": hi,
        "gate": g,
        "ptail": pt,
        "is": statistics.fmean(t["r"] for t in ins) if ins else float("nan"),
        "oos": statistics.fmean(t["r"] for t in oos) if oos else float("nan"),
    }


def mean_r(tr: list[dict[str, Any]]) -> float:
    return statistics.fmean(t["r"] for t in tr) if tr else float("nan")


def full(L: list[str], D: dict[str, Any], cov: dict[str, Any], cache: Path, t0: float) -> None:
    P = BASE
    sp = cov["split"]
    met, nmet = sp["met"], sp["not_met"]
    cl = blocks(D, P)
    g1 = len(sp["measured"]) / len(sp["event"]) >= COVERAGE_MIN
    h1 = trades(D, P, met)
    (cache / "trades_h1.json").write_text(json.dumps(h1, default=str))
    L.append("## H1: календарний ATM-стреддл за умови P10, до експірації фронту\n")
    a1 = analyse(L, "H1", h1, cl, len(met), None)
    cov_ok = {"H1": g1 and len(h1) / max(len(met), 1) >= COVERAGE_MIN}
    log("H1 done", t0)

    ctrl = trades(D, P, nmet)
    (cache / "trades_ctrl.json").write_text(json.dumps(ctrl, default=str))
    L.append("## H2: інверсія ≥ +1.5 п. проти «подія без інверсії» (той самий календар)\n")
    a_c = analyse(L, "контроль", ctrl, cl, len(nmet), None)
    both = h1 + ctrl
    in_a = [True] * len(h1) + [False] * len(ctrl)
    d2 = cluster_bootstrap_diff([t["r"] for t in both], [t["day"] for t in both], in_a, n_boot=N_BOOT, seed=SEED)
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
    cov_ok["H2"] = g1 and cov_ok["H1"] and len(ctrl) / max(len(nmet), 1) >= COVERAGE_MIN
    log("H2 done", t0)

    h3 = trades(D, H3P, met)
    (cache / "trades_h3.json").write_text(json.dumps(h3, default=str))
    L.append("## H3: діагональ (місячний стренгл 25Δ, ½ фронтового стренгла не ближче), за умови P10\n")
    a3 = analyse(L, "H3", h3, cl, len(met), None)
    cov_ok["H3"] = g1 and len(h3) / max(len(met), 1) >= COVERAGE_MIN
    log("H3 done", t0)

    c1 = trades(D, {**P, "hs": 0.03}, met)
    cc = trades(D, {**P, "hs": 0.03}, nmet)
    st = {"H1": mean_r(c1), "H2": mean_r(c1) - mean_r(cc), "H3": mean_r(trades(D, {**H3P, "hs": 0.03}, met))}
    res = {"H1": a1, "H2": a2, "H3": a3}
    raw = {**PREV_RAW_P, **{f"P10-{h}": a["p"] for h, a in res.items()}}
    deg = PREV_DEGENERATE | {f"P10-{h}" for h, a in res.items() if not a["gate"]["ok"]}
    pv = batch_raw_p(raw, deg)
    adj = holm_adjust(pv)
    L.append(f"## Холм партії пункту 4 ({len(pv)} тестів; вироджені → 1.0: {sorted(deg)})\n")
    L.append("- " + ", ".join(f"{k}: сирий {raw[k]:.3f} → у Холмі {pv[k]:.3f} → {adj[k]:.3f}" for k in sorted(pv)) + "\n")
    L.append("## Вердикти (механічно за реєстрацією, `calendar_p10.verdict`)\n")
    L.append(
        "| гіпотеза | n | середнє R | CI | IS | OOS | стрес 3 в.п. | Холм p | MDE | ворота хвоста | ворота покриття | стрес-хвіст | вердикт |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for h, a in res.items():
        v = verdict(
            mean=a["mean"],
            lo=a["lo"],
            hi=a["hi"],
            holm_p=adj[f"P10-{h}"],
            is_mean=a["is"],
            oos_mean=a["oos"],
            stress_cost_mean=st[h],
            edge=EDGE_R,
            tail_ok=a["gate"]["ok"],
            coverage_ok=cov_ok[h],
        )
        L.append(
            f"| {h} | {a['n']} | {rr(a['mean'])} | [{a['lo']:+.3f}; {a['hi']:+.3f}] | {rr(a['is'])} | {rr(a['oos'])} | {rr(st[h])} | "
            f"{adj[f'P10-{h}']:.3f} | {a['mde']:.3f} | {'так' if a['gate']['ok'] else 'ні'} ({a['gate']['n_loss']}) | "
            f"{'так' if cov_ok[h] else 'ні'} | {rr(a['ptail']['stress_mean'])} | **{v}** |"
        )
    L.append(f"\nEDGE_R = {EDGE_R:.2f} R (зареєстровано до стадії 0).\n")
    log("verdict done", t0)
    sensitivity(L, D, h1, met, cl, t0)


def sensitivity(L: list[str], D: dict[str, Any], h1: list[dict[str, Any]], met: list[int], cl: dict[int, int], t0: float) -> None:
    P = BASE
    L.append("## Чутливість H1 (усі варіанти зареєстровано заздалегідь)\n")
    L += HDR
    grid: list[tuple[str, dict[str, Any], list[int] | None]] = [
        *((f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, met) for h in (0.0, 0.03)),
        ("модель «сторона»", {**P, "model": "side"}, met),
        ("місячна нога на інтерпольованій марці", {**P, "month_interp": True}, met),
        ("правило (а): пропуск дня без заміни", {**P, "arb": "skip"}, met),
        ("поверхня виходу з запасом ≤ 2 д", {**P, "exit_slack": 2}, met),
        ("діагональ, об'єм 0.75", {**H3P, "qty": 0.75}, met),
        ("діагональ, |Δ| 0.40", {**H3P, "delta": 0.40}, met),
        *((f"поріг інверсії {n}", {**P, "threshold": th}, None) for th, n in ((0.010, "+1.0 п."), (0.020, "+2.0 п."), (1e-9, "> 0"))),
        ("вхід у п'ятницю", {**P, "weekday": 4}, None),
        ("лише FOMC (розвідувально)", {**P, "events": "fomc"}, None),
        ("лише CPI (розвідувально)", {**P, "events": "cpi"}, None),
    ]
    for name, Pv, ds in grid:
        days = ds if ds is not None else split_days(D, Pv)["met"]
        L.append(vp1.table_row(name, trades(D, Pv, days), blocks(D, Pv), len(days)))
        log(f"grid: {name}", t0)
    meas = split_days(D, P)["measured"]
    L.append(vp1.table_row("календар на всіх виміряних суботах з подією (без умови)", trades(D, P, meas), cl, len(meas)))
    top = sorted(h1, key=lambda t: t["r"], reverse=True)[5:]
    L.append(vp1.table_row("H1 без 5 найкращих угод", top, cl, len(met)))
    L.append(
        "\nЄдиний актив (BTC): пункт 5а не застосовний; ETH не покрито кешем поверхонь. R — на дебет (капітал під ризиком "
        "календаря); Bybit маржує проданий стреддл окремо, тож дохідність на фактичну маржу нижча.\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("coverage", "full"), default="coverage")
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/p10_calendar")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.perf_counter()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    D = load(Path(a.src_dir).expanduser(), t0)
    L = [f"# P10: інверсія фронту → календар / діагональ, BTC (stage {a.stage}, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n"]
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
