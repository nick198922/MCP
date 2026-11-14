"""Playbook rules P11 / P9 (FOMC / CPI / NFP release -> condor sold / straddle bought through it), BTC, Bybit costs.

Card IDEA-20261114-options-playbook, item 4.6 (registered before any P&L). No network: the put-skew-v2 cache
(``--src-dir``, read only: ``snapshots_v2.json`` daily 09:00 surfaces, ``delivery_btc_usd.json``, ``dvol_BTC.json``)
and the macro events CSV ``docs/verification/2026-10-15-us-macro-events.csv``.

    uv run python scripts/verify_p11_event_condor.py --stage coverage --out ~/.cache/tradedesk-agents/p11_event_condor/coverage.md
    uv run python scripts/verify_p11_event_condor.py --stage full --out ~/.cache/tradedesk-agents/p11_event_condor/full.md

``--stage coverage`` prints coverage and entry-price checks only (no outcome, no P&L).
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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import verify_option_modules as vom  # noqa: E402  (scripts/ is not a package)
import verify_p1_condor_dvol as vp1  # noqa: E402
import verify_p3_trend_spread as vp3  # noqa: E402
import verify_p5_straddle as vp5  # noqa: E402
from common_helpers import ymd_ms  # noqa: E402

from tradedesk.analytics.calendar_p10 import batch_raw_p, entry_cost, event_times_ms, exec_ivs  # noqa: E402
from tradedesk.analytics.condor_25d import block_clusters, parametric_tail, tail_gate  # noqa: E402
from tradedesk.analytics.condor_dvol import cluster_bootstrap_diff, entry_credit, plan_p1, settle  # noqa: E402
from tradedesk.analytics.dvol_filter import cvar, max_drawdown, mean_without_top  # noqa: E402
from tradedesk.analytics.event_condor import control_days, event_entry_days, expiry_after, playbook_labels  # noqa: E402
from tradedesk.analytics.option_condor import block_bootstrap_mean, exec_legs  # noqa: E402
from tradedesk.analytics.option_ideas import EVENT_KINDS  # noqa: E402
from tradedesk.analytics.straddle_p5 import plan_straddle, settle_straddle, two_sided_tail_gate, verdict  # noqa: E402
from tradedesk.analytics.trend_spread import DAY_MS, holm_adjust  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
EVENTS = REPO / "docs" / "verification" / "2026-10-15-us-macro-events.csv"
KINDS = ("fomc", "cpi", "nfp")  # macro_calendar's high_impact_within_48h kinds
OOS = vp1.OOS
LAST_EXPIRY_MS = vp1.LAST_EXPIRY_MS
FIRST = int(datetime(2021, 1, 1, tzinfo=UTC).timestamp() * 1000)
COVERAGE_MIN = 0.60
EDGE_H3 = 0.10  # registered (4.6): as P5'
EDGE_SHARE = 0.25  # registered (4.6): EDGE_R of H1 / H2 = 1/4 of the median credit / width of H1 (stage 0, no P&L)
N_BOOT = vp3.N_BOOT
SEED = vp3.SEED
MIN_COUNT = 5
PREV_RAW_P = {**vp5.PREV_RAW_P, "P5-H1": 0.284, "P5-H2": 0.299, "P5-H3": 0.004}  # p5_straddle/full.md, "Холм партії"
PREV_DEGENERATE = set(vp5.PREV_DEGENERATE)
BASE: dict[str, Any] = {
    "kinds": KINDS,
    "lag": 0,
    "max_dte": 1.5,
    "short_delta": 0.25,
    "wing": 0.03,
    "band": 0.015,
    "model": "mark",
    "hs": 0.015,
    "arb": "replace",
    "ctrl_weekdays": (0, 1, 2, 3, 4),
}

log = vp3.log
rr, ci_r = vp3.rr, vp3.ci_r
_q = vp5._q
HDR = vp1.HDR
HDR_S = vp5.vp10.HDR


def mean_r(tr: list[dict[str, Any]]) -> float:
    return statistics.fmean(t["r"] for t in tr) if tr else float("nan")


# ----------------------------------------------------------------------------- data and samples


def load(src: Path, t0: float) -> dict[str, Any]:
    D = vom.load(src)
    rows = list(csv.DictReader(EVENTS.open()))
    D["events"] = {k: event_times_ms(rows, (k,)) for k in KINDS}
    log("events: " + ", ".join(f"{k} {len(v)}" for k, v in D["events"].items()), t0)
    return D


def events_of(D: dict[str, Any], kinds: tuple[str, ...]) -> list[int]:
    return sorted({e for k in kinds for e in D["events"][k] if FIRST <= e <= LAST_EXPIRY_MS - DAY_MS})


def event_days(D: dict[str, Any], P: dict[str, Any]) -> dict[int, list[int]]:
    return event_entry_days(events_of(D, P["kinds"]), lag_days=P["lag"])


def ctrl_days(D: dict[str, Any], P: dict[str, Any]) -> list[int]:
    cal = range(FIRST, LAST_EXPIRY_MS - 2 * DAY_MS, DAY_MS)
    return control_days(cal, events_of(D, KINDS), weekdays=P["ctrl_weekdays"])


def blocks(days: list[int]) -> dict[int, int]:
    return block_clusters(days, start_ms=FIRST, weeks=4)


def _expiry(D: dict[str, Any], d: int, after: int | None, P: dict[str, Any]) -> tuple[dict[str, Any] | None, int | None, str]:
    snap = D["days"].get(d)
    if snap is None:
        return None, None, "no surface"
    e, why = expiry_after(snap, snap["t_ms"] if after is None else after, max_days=P["max_dte"])
    if e is None:
        return snap, None, why
    if e > LAST_EXPIRY_MS or D["delivery"].get(ymd_ms(e)) is None:
        return snap, None, "no delivery price"
    return snap, e, ""


def build_condor(D: dict[str, Any], d: int, after: int | None, P: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    snap, e, why = _expiry(D, d, after, P)
    if e is None:
        return None, why
    plan, why = plan_p1(snap, e, short_delta=P["short_delta"], wing=P["wing"], band=P["band"])
    if plan is None:
        return None, why
    legs, fb = exec_legs(plan, P["model"], P["hs"])
    credit = entry_credit(plan, legs)
    if credit is None:
        return None, "credit outside (0, width)"
    return {"day": d, "plan": plan, "credit": credit, "fallbacks": fb, "dte": (e - snap["t_ms"]) / DAY_MS}, ""


def build_straddle(D: dict[str, Any], d: int, after: int | None, P: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    snap, e, why = _expiry(D, d, after, P)
    if e is None:
        return None, why
    plan, why = plan_straddle(snap, e, band=P["band"], arb_rule=P["arb"])
    if plan is None:
        return None, why
    ivs, fb = exec_ivs(plan, P["model"], P["hs"])
    debit = entry_cost(plan, ivs)
    if debit is None:
        return None, "IV <= 1 %"
    return {"day": d, "plan": plan, "debit": debit, "fallbacks": fb, "dte": (e - snap["t_ms"]) / DAY_MS}, ""


def build_all(D: dict[str, Any], days: dict[int, int | None], P: dict[str, Any], kind: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    """``days``: entry day -> the release to trade through (None for control days)."""
    fn = build_condor if kind == "condor" else build_straddle
    out, why = [], Counter()
    for d in sorted(days):
        b, w = fn(D, d, days[d], P)
        if b is None:
            why[w] += 1
        else:
            out.append(b)
    return out, why


def outcome(D: dict[str, Any], b: dict[str, Any], kind: str) -> dict[str, Any]:
    plan = b["plan"]
    if kind == "condor":
        e = plan["expiry"]
        res = settle(plan, b["credit"], D["delivery"][ymd_ms(e)])
        return {"day": b["day"], "expiry": e, "spot": plan["spot"], "K": plan["K"], "width": plan["width"], "credit": b["credit"], **res}
    e = plan["expiry"]["m"]
    return settle_straddle(plan, b["debit"], D["delivery"][ymd_ms(e)])


def ev_map(D: dict[str, Any], P: dict[str, Any]) -> dict[int, int | None]:
    return {d: max(v) for d, v in event_days(D, P).items()}


def trades(D: dict[str, Any], P: dict[str, Any], days: dict[int, int | None], kind: str) -> list[dict[str, Any]]:
    built, _ = build_all(D, days, P, kind)
    return [outcome(D, b, kind) for b in built]


# ----------------------------------------------------------------------------- stage 0


def coverage(L: list[str], D: dict[str, Any], t0: float) -> dict[str, Any]:
    P = BASE
    ev = ev_map(D, P)
    ctrl = dict.fromkeys(ctrl_days(D, P))
    L.append(f"## Стадія 0: покриття (поріг {COVERAGE_MIN:.0%}; P&L не рахується)\n")
    with_exp = sum(1 for d, e in ev.items() if _expiry(D, d, e, P)[1] is not None)
    g1 = with_exp / len(ev) >= COVERAGE_MIN
    L.append(
        f"- днів подій FOMC / CPI / NFP {ymd_ms(min(ev))} … {ymd_ms(max(ev))}: **{len(ev)}**; ворота (і): поверхня з експірацією "
        f"після події (DTE ≤ {P['max_dte']} д) {with_exp} / {len(ev)} = **{with_exp / len(ev):.1%}** → {'пройдено' if g1 else 'НЕ пройдено'}"
    )
    L.append(f"- контрольних днів (пн–пт, без події в [−24 год; +48 год]): {len(ctrl)}\n")
    L.append("| вибірка / конструкція | днів | побудовано | покриття | причини пропуску |")
    L.append("|---|---|---|---|---|")
    out: dict[str, Any] = {"ev": ev, "ctrl_days": ctrl, "g1": g1}
    rows: list[tuple[str, str, dict[str, Any], dict[int, int | None] | None, str]] = [
        ("H1", "**H1: кондор 25Δ × 3 %, день події**", P, ev, "condor"),
        ("ctrl", "**H2-контроль: кондор, дні без події**", P, ctrl, "condor"),
        ("H3", "**H3: куплений ATM-стреддл, день події**", P, ev, "straddle"),
        *(
            ("", f"Δ {sd:.2f} × крило {w:.0%}", {**P, "short_delta": sd, "wing": w}, ev, "condor")
            for sd in (0.15, 0.25, 0.30)
            for w in (0.01, 0.02, 0.03)
            if (sd, w) != (0.25, 0.03)
        ),
        *(("", f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, ev, "condor") for h in (0.0, 0.03)),
        ("", "модель «сторона» (кондор)", {**P, "model": "side"}, ev, "condor"),
        ("", "модель «сторона» (стреддл)", {**P, "model": "side"}, ev, "straddle"),
        ("", "вхід за день до події (T − 1, DTE ≤ 2.5 д)", {**P, "lag": 1, "max_dte": 2.5}, None, "condor"),
        ("", "лише FOMC + CPI (без NFP)", {**P, "kinds": tuple(EVENT_KINDS)}, None, "condor"),
        *((("", f"лише {k.upper()} (розвідувально)", {**P, "kinds": (k,)}, None, "condor")) for k in KINDS),
        (
            "",
            "контроль лише вт–чт",
            {**P, "ctrl_weekdays": (1, 2, 3)},
            dict.fromkeys(ctrl_days(D, {**P, "ctrl_weekdays": (1, 2, 3)})),
            "condor",
        ),
        ("", "H3: правило (а) «пропуск дня»", {**P, "arb": "skip"}, ev, "straddle"),
    ]
    for key, name, Pv, ds, kind in rows:
        days = ds if ds is not None else ev_map(D, Pv)
        built, why = build_all(D, days, Pv, kind)
        cov = len(built) / len(days) if days else 0.0
        L.append(f"| {name} | {len(days)} | {len(built)} | **{cov:.1%}** | {dict(why.most_common())} |")
        if key:
            out[key] = built
            out[f"cov_{key}"] = cov
    log("coverage table done", t0)
    by_y = Counter(ymd_ms(d)[:4] for d in ev)
    ok_y = Counter(ymd_ms(b["day"])[:4] for b in out["H1"])
    L.append("\n- H1 по роках (днів подій / побудовано): " + ", ".join(f"{y}: {by_y[y]}/{ok_y[y]}" for y in sorted(by_y)))
    kinds = Counter(k for k in KINDS for e in events_of(D, (k,)) if e // DAY_MS * DAY_MS in ev)
    L.append(f"- подій за типом у вибірці: {dict(kinds)}; днів з двома подіями: {sum(1 for v in event_days(D, P).values() if len(v) > 1)}")
    cl = blocks(sorted(ev) + sorted(ctrl))
    h1, c = out["H1"], out["ctrl"]
    L.append(
        f"- H1: 4-тижневих блоків серед угод {len({cl[b['day']] for b in h1})}; IS / OOS {sum(b['day'] < OOS for b in h1)} / "
        f"{sum(b['day'] >= OOS for b in h1)}; контроль: блоків {len({cl[b['day']] for b in c})}, IS / OOS "
        f"{sum(b['day'] < OOS for b in c)} / {sum(b['day'] >= OOS for b in c)}\n"
    )
    return out


def sanity(L: list[str], D: dict[str, Any], cov: dict[str, Any]) -> float:
    L.append("## Перевірки до прогону (без P&L)\n")
    h1, c, h3 = cov["H1"], cov["ctrl"], cov["H3"]
    for name, built in (("H1", h1), ("контроль", c)):
        L.append(
            f"- {name}: DTE {_q([b['dte'] for b in built], '{:.2f}')}; розподіл {dict(Counter(round(b['dte'], 2) for b in built).most_common(4))}"
        )
        cw = [b["credit"] / b["plan"]["width"] for b in built]
        L.append(
            f"- {name}: кредит / ширина {_q(cw)}; кредит / спот {_q([b['credit'] / b['plan']['spot'] for b in built], '{:.4f}')}; "
            f"ширина / спот {_q([b['plan']['width'] / b['plan']['spot'] for b in built], '{:.4f}')}"
        )
        kd = (
            [(b["plan"]["K"]["sc"] - b["plan"]["spot"]) / b["plan"]["spot"] for b in built]
            if built and isinstance(built[0]["plan"]["K"], dict)
            else []
        )
        if kd:
            L.append(f"- {name}: проданий кол − спот, частка спота {_q(kd, '{:+.4f}')}")
    edge = EDGE_SHARE * statistics.median(b["credit"] / b["plan"]["width"] for b in h1)
    L.append(f"- **EDGE_R H1 / H2 = ¼ × медіана кредит / ширина H1 = {edge:.3f} R** (формула зареєстрована до стадії 0)")
    fb = sum(exec_legs(b["plan"], "side")[1] for b in h1)
    L.append(f"- модель «сторона», кондор H1: ніг без принта своєї сторони {fb} з {4 * len(h1)}")
    L.append(f"- H3: DTE {_q([b['dte'] for b in h3], '{:.2f}')}; дебет / спот {_q([b['debit'] / b['plan']['spot'] for b in h3], '{:.4f}')}")
    L.append(f"- H3: K − форвард, частка спота {_q([(b['plan']['K'] - b['plan']['F']['m']) / b['plan']['spot'] for b in h3], '{:+.4f}')}")
    L.append(f"- H3: марки входу (а) {dict(Counter(b['plan']['legs'][0]['arb'] for b in h3))}")
    fb3 = sum(exec_ivs(b["plan"], "side")[1] for b in h3)
    L.append(f"- модель «сторона», стреддл H3: ніг без принта {fb3} з {2 * len(h3)}")
    cs, _ = build_all(D, cov["ctrl_days"], BASE, "straddle")
    iv_e = [b["plan"]["iv"] for b in h3]
    iv_c = [b["plan"]["iv"] for b in cs]
    L.append(f"- ATM IV входу (денна експірація): день події {_q(iv_e)} (n = {len(iv_e)}), контроль {_q(iv_c)} (n = {len(iv_c)})")
    fr_days = vom.fridays(vom.FIRST, vom.LAST)
    ok = sum(1 for f in fr_days if vom.plan(D, f, "h1")[0] is not None)
    L.append(f"- звірка з відомим числом: покриття кондора 10Δ / ±15 % (options-modules H1) = {ok} / {len(fr_days)} (у звіті 179 / 301)\n")
    return edge


# ----------------------------------------------------------------------------- full


def analyse(L: list[str], name: str, tr: list[dict[str, Any]], cl: dict[int, int], elig: int, *, two_sided: bool) -> dict[str, Any]:
    s = vp3.summary(tr, cl)
    L += HDR_S if two_sided else HDR
    L.append(vp1.table_row(f"{name}: усі", tr, cl, elig))
    L.append(vp1.table_row("IS (до 2024)", [t for t in tr if t["day"] < OOS], cl, elig))
    L.append(vp1.table_row("OOS (2024+)", [t for t in tr if t["day"] >= OOS], cl, elig))
    for y in sorted({ymd_ms(t["day"])[:4] for t in tr}):
        L.append(vp1.table_row(f"рік {y}", [t for t in tr if ymd_ms(t["day"])[:4] == y], cl, elig))
    rs = [t["r"] for t in tr]
    bb = block_bootstrap_mean(rs, block=4, n_boot=N_BOOT, seed=SEED)
    lo, hi = min(s["ci"][0], bb["ci"][0]), max(s["ci"][1], bb["ci"][1])
    L.append(f"\n- блоковий бутстреп (блок 4 угоди): {rr(bb['mean'])} {ci_r(bb['ci'])}, p {bb['p']:.3f}")
    L.append(f"- CI для вердикту (об'єднання кластерного й блокового): [{lo:+.3f}; {hi:+.3f}]")
    first: dict[int, dict[str, Any]] = {}
    for t in tr:
        first.setdefault(cl[t["day"]], t)
    one = list(first.values())
    b1 = vp3.summary(one, {t["day"]: i for i, t in enumerate(one)})
    L.append(f"- одна угода на блок (перша): n = {len(one)}, {rr(b1['mean'])} {ci_r(b1['ci'])}")
    halves = []
    for k in (0, 1):
        sub = rs[k::2]
        bk = block_bootstrap_mean(sub, block=2, n_boot=N_BOOT, seed=SEED)
        halves.append((sum(1 for r in sub if r <= 0), len(sub)))
        L.append(f"- половина {k + 1} (кожна друга угода): n = {len(sub)}, {rr(bk.get('mean'))} {ci_r(bk.get('ci'))}")
    labels = [cl[t["day"]] for t in tr]
    if two_sided:
        g = two_sided_tail_gate(rs, labels, min_count=MIN_COUNT)
        gl, gw = g["loss"], g["win"]
        L.append(
            f"- ворота хвоста (двобічні): збиткових {gl['n_loss']} у {gl['n_loss_clusters']} з {gl['n_clusters']} блоків, "
            f"виграшних {gw['n_loss']} у {gw['n_loss_clusters']} → **{'пройдено' if g['ok'] else 'НЕ пройдено'}**"
        )
    else:
        g = tail_gate(rs, labels, min_losses=MIN_COUNT)
        L.append(
            f"- ворота хвоста: збиткових (R ≤ 0) {g['n_loss']} з {g['n']} у {g['n_loss_clusters']} з {g['n_clusters']} блоків, "
            f"P(ресемпл без збиткового) {g['p_no_loss_resample']:.3f} → **{'пройдено' if g['ok'] else 'НЕ пройдено'}**"
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


def full(L: list[str], D: dict[str, Any], cov: dict[str, Any], edge: float, cache: Path, t0: float) -> None:
    P = BASE
    ev, ctrl = cov["ev"], cov["ctrl_days"]
    cl = blocks(sorted(ev) + sorted(ctrl))
    h1 = trades(D, P, ev, "condor")
    (cache / "trades_h1.json").write_text(json.dumps(h1, default=str))
    L.append("## H1 (P11): продаж кондора 25Δ × 3 % у день події, до поставки (R на макс. збиток)\n")
    a1 = analyse(L, "H1", h1, cl, len(ev), two_sided=False)
    cov_ok = {"H1": cov["g1"] and cov["cov_H1"] >= COVERAGE_MIN}
    log("H1 done", t0)

    c = trades(D, P, ctrl, "condor")
    (cache / "trades_ctrl.json").write_text(json.dumps(c, default=str))
    L.append("## H2: подія проти контролю (той самий кондор)\n")
    ac = analyse(L, "контроль", c, cl, len(ctrl), two_sided=False)
    both = h1 + c
    labels = [("a", cl[t["day"]]) for t in h1] + [("b", cl[t["day"]]) for t in c]
    d2 = cluster_bootstrap_diff([t["r"] for t in both], labels, [True] * len(h1) + [False] * len(c), n_boot=N_BOOT, seed=SEED)
    is2 = mean_r([t for t in h1 if t["day"] < OOS]) - mean_r([t for t in c if t["day"] < OOS])
    oos2 = mean_r([t for t in h1 if t["day"] >= OOS]) - mean_r([t for t in c if t["day"] >= OOS])
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
    a2["gate"] = {"ok": a1["gate"]["ok"] and ac["gate"]["ok"], "n_loss": a1["gate"]["n_loss"] + ac["gate"]["n_loss"]}
    a2["ptail"] = ac["ptail"]
    cov_ok["H2"] = cov_ok["H1"] and cov["cov_ctrl"] >= COVERAGE_MIN
    log("H2 done", t0)

    h3 = trades(D, P, ev, "straddle")
    (cache / "trades_h3.json").write_text(json.dumps(h3, default=str))
    L.append("## H3 (P9): купівля ATM-стреддла на тій самій експірації, до поставки (R на дебет)\n")
    a3 = analyse(L, "H3", h3, cl, len(ev), two_sided=True)
    cov_ok["H3"] = cov["g1"] and cov["cov_H3"] >= COVERAGE_MIN
    log("H3 done", t0)

    P3 = {**P, "hs": 0.03}
    st = {
        "H1": mean_r(trades(D, P3, ev, "condor")),
        "H2": mean_r(trades(D, P3, ev, "condor")) - mean_r(trades(D, P3, ctrl, "condor")),
        "H3": mean_r(trades(D, P3, ev, "straddle")),
    }
    res = {"H1": a1, "H2": a2, "H3": a3}
    edges = {"H1": edge, "H2": edge, "H3": EDGE_H3}
    raw = {**PREV_RAW_P, **{f"P11-{h}": a["p"] for h, a in res.items()}}
    deg = PREV_DEGENERATE | {f"P11-{h}" for h, a in res.items() if not a["gate"]["ok"]}
    pv = batch_raw_p(raw, deg)
    adj = holm_adjust(pv)
    L.append(f"## Холм партії пункту 4 ({len(pv)} тестів; вироджені → 1.0: {sorted(deg)})\n")
    L.append("- " + ", ".join(f"{k}: сирий {raw[k]:.3f} → у Холмі {pv[k]:.3f} → {adj[k]:.3f}" for k in sorted(pv)) + "\n")
    L.append("## Вердикти (механічно за реєстрацією, `straddle_p5.verdict`)\n")
    L.append(
        "| гіпотеза | n | середнє R | CI | IS | OOS | стрес 3 в.п. | Холм p | MDE | EDGE_R | ворота хвоста | ворота покриття | стрес-хвіст | вердикт |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    vs = {}
    for h, a in res.items():
        vs[h] = verdict(
            mean=a["mean"],
            lo=a["lo"],
            hi=a["hi"],
            holm_p=adj[f"P11-{h}"],
            is_mean=a["is"],
            oos_mean=a["oos"],
            stress_cost_mean=st[h],
            edge=edges[h],
            tail_ok=a["gate"]["ok"],
            coverage_ok=cov_ok[h],
        )
        L.append(
            f"| {h} | {a['n']} | {rr(a['mean'])} | [{a['lo']:+.3f}; {a['hi']:+.3f}] | {rr(a['is'])} | {rr(a['oos'])} | {rr(st[h])} | "
            f"{adj[f'P11-{h}']:.3f} | {a['mde']:.3f} | {edges[h]:.3f} | {'так' if a['gate']['ok'] else 'ні'} ({a['gate']['n_loss']}) | "
            f"{'так' if cov_ok[h] else 'ні'} | {rr(a['ptail']['stress_mean'])} | **{vs[h]}** |"
        )
    L.append(f"\nМітки плейбуку за реєстрацією (`event_condor.playbook_labels`): {playbook_labels(vs['H1'], vs['H3'])}\n")
    log("verdict done", t0)
    sensitivity(L, D, h1, cl, t0)


def sensitivity(L: list[str], D: dict[str, Any], h1: list[dict[str, Any]], cl: dict[int, int], t0: float) -> None:
    P = BASE
    ev = ev_map(D, P)
    L.append("## Чутливість (усі варіанти зареєстровано заздалегідь; кластер = 4-тижневий блок)\n")
    L += HDR
    grid: list[tuple[str, dict[str, Any], dict[int, int | None] | None, str]] = [
        *(
            (f"Δ {sd:.2f} × крило {w:.0%}", {**P, "short_delta": sd, "wing": w}, ev, "condor")
            for sd in (0.15, 0.25, 0.30)
            for w in (0.01, 0.02, 0.03)
        ),
        *((f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, ev, "condor") for h in (0.0, 0.03)),
        ("модель «сторона»", {**P, "model": "side"}, ev, "condor"),
        ("вхід за день до події (T − 1)", {**P, "lag": 1, "max_dte": 2.5}, None, "condor"),
        ("лише FOMC + CPI (без NFP)", {**P, "kinds": tuple(EVENT_KINDS)}, None, "condor"),
        *((f"лише {k.upper()} (розвідувально)", {**P, "kinds": (k,)}, None, "condor") for k in KINDS),
        (
            "контроль лише вт–чт",
            {**P, "ctrl_weekdays": (1, 2, 3)},
            dict.fromkeys(ctrl_days(D, {**P, "ctrl_weekdays": (1, 2, 3)})),
            "condor",
        ),
    ]
    for name, Pv, ds, kind in grid:
        days = ds if ds is not None else ev_map(D, Pv)
        tr = trades(D, Pv, days, kind)
        L.append(vp1.table_row(name, tr, blocks(sorted(days)), len(days)))
        log(f"grid: {name}", t0)
    top = sorted(h1, key=lambda t: t["r"], reverse=True)[5:]
    L.append(vp1.table_row("H1 без 5 найкращих угод", sorted(top, key=lambda t: t["day"]), cl, len(ev)))
    L.append("\n### H3 (стреддл): чутливість\n")
    L += HDR_S
    for name, Pv in (
        ("половина спреду 0.0 в.п.", {**P, "hs": 0.0}),
        ("половина спреду 3.0 в.п.", {**P, "hs": 0.03}),
        ("модель «сторона»", {**P, "model": "side"}),
        ("правило (а): пропуск дня", {**P, "arb": "skip"}),
    ):
        tr = trades(D, Pv, ev, "straddle")
        L.append(vp1.table_row(name, tr, cl, len(ev)))
    for name, Pv in (("T − 1", {**P, "lag": 1, "max_dte": 2.5}), ("лише FOMC + CPI", {**P, "kinds": tuple(EVENT_KINDS)})):
        days = ev_map(D, Pv)
        tr = trades(D, Pv, days, "straddle")
        L.append(vp1.table_row(name, tr, blocks(sorted(days)), len(days)))
    L.append(
        "\nЄдиний актив (BTC): пункт 5а не застосовний; ETH не покрито кешем поверхонь. R кондора — на максимальний збиток "
        "(ширина − кредит), R стреддла — на дебет; обидва — на капітал під ризиком.\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("coverage", "full"), default="coverage")
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/p11_event_condor")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.perf_counter()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    D = load(Path(a.src_dir).expanduser(), t0)
    L = [
        f"# P11 / P9: подія FOMC / CPI / NFP → кондор через подію; стреддл через подію, BTC (stage {a.stage}, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n"
    ]
    cov = coverage(L, D, t0)
    edge = sanity(L, D, cov)
    log("coverage and sanity done", t0)
    if a.stage == "full":
        full(L, D, cov, edge, cache, t0)
    text = "\n".join(L)
    print(text)
    if a.out:
        Path(a.out).expanduser().write_text(text + "\n")
    log("done", t0)


if __name__ == "__main__":
    main()
