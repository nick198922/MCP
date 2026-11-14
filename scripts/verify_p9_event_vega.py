"""P9 at 3-5 days: ATM straddle bought at 09:00 UTC on a FOMC / CPI / NFP day on an expiry >= 3 d after the release,
sold on the next 09:00 surface (card IDEA-20261114-options-playbook, item 4.7).

H1: that trade after Bybit costs (R on the debit). H2: H1 minus the daily straddle through the release held to delivery
(item 4.6 H3), same day. H3: H1 minus the same trade on control weekdays without a release in [-24 h, +48 h].

Data: the local surface cache (`put_skew/snapshots_v2.json`, 09:00 UTC daily surfaces of Deribit BTC marks), Deribit
delivery prices and `docs/verification/2026-10-15-us-macro-events.csv`; no network. `--stage coverage` prints coverage
and pre-run checks only (no P&L); `--stage full` the registered test.

    uv run python scripts/verify_p9_event_vega.py --stage coverage --out ~/.cache/tradedesk-agents/p9_event_vega/coverage.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_p1_condor_dvol as vp1  # noqa: E402  (scripts/ is not a package)
import verify_p3_trend_spread as vp3  # noqa: E402
import verify_p11_event_condor as vp11  # noqa: E402
from common_helpers import ymd_ms  # noqa: E402

from tradedesk.analytics.calendar_p10 import batch_raw_p, entry_cost, exec_ivs  # noqa: E402
from tradedesk.analytics.condor_dvol import cluster_bootstrap_diff  # noqa: E402
from tradedesk.analytics.event_vega import (  # noqa: E402
    control_anchor,
    exit_snapshot,
    expiry_after_gap,
    hold_outcome,
    iv_change,
    p9_labels,
    paired,
    stratified_diff,
)
from tradedesk.analytics.option_ideas import EVENT_KINDS  # noqa: E402
from tradedesk.analytics.put_skew import iv_at_strike  # noqa: E402
from tradedesk.analytics.straddle_p5 import plan_straddle, verdict  # noqa: E402
from tradedesk.analytics.trend_spread import DAY_MS, holm_adjust  # noqa: E402

KINDS = vp11.KINDS
OOS = vp11.OOS
COVERAGE_MIN = 0.60
EDGE = {"H1": 0.05, "H2": 0.10, "H3": 0.05}  # registered in card 4.7 before any P&L
N_BOOT = vp3.N_BOOT
SEED = vp3.SEED
# item-4 batch raw p: vp11's 15 + P11 H1-H3 from p11_event_condor/full.md ("Холм партії": 0.762 / 0.048 / 0.002)
PREV_RAW_P = {**vp11.PREV_RAW_P, "P11-H1": 0.762, "P11-H2": 0.048, "P11-H3": 0.002}
PREV_DEGENERATE = set(vp11.PREV_DEGENERATE)  # P11 H1-H3 passed their tail gates
BASE: dict[str, Any] = {
    "kinds": KINDS,
    "lag": 0,
    "gap": 3.0,
    "max_dte": 10.0,
    "hold": 1,
    "band": 0.015,
    "model": "mark",
    "hs": 0.015,
    "arb": "replace",
    "ctrl_weekdays": (0, 1, 2, 3, 4),
}

log = vp3.log
rr, ci_r = vp3.rr, vp3.ci_r
_q = vp11._q
mean_r = vp11.mean_r


# ----------------------------------------------------------------------------- samples


def ev_map(D: dict[str, Any], P: dict[str, Any]) -> dict[int, int]:
    """Entry day -> the latest release it trades through (several releases on a day = one trade)."""
    return {d: max(v) for d, v in vp11.event_entry_days(vp11.events_of(D, P["kinds"]), lag_days=P["lag"]).items()}


def ctrl_map(D: dict[str, Any], P: dict[str, Any]) -> dict[int, int]:
    return {d: control_anchor(d) for d in vp11.ctrl_days(D, P)}


def build(D: dict[str, Any], d: int, anchor: int, P: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Entry plan and debit; the exit surface must hold the expiry with a mark at K (checked here, priced in `outcome`)."""
    snap = D["days"].get(d)
    if snap is None:
        return None, "no surface"
    e, why = expiry_after_gap(snap, anchor, min_gap_days=P["gap"], max_dte_days=P["max_dte"])
    if e is None:
        return None, why
    plan, why = plan_straddle(snap, e, band=P["band"], arb_rule=P["arb"])
    if plan is None:
        return None, why
    ivs, fb = exec_ivs(plan, P["model"], P["hs"])
    debit = entry_cost(plan, ivs)
    if debit is None:
        return None, "IV <= 1 %"
    ex = exit_snapshot(D["days"], d, P["hold"])
    if ex is None:
        return None, "no exit surface"
    x = ex["expiries"].get(e)
    if x is None or ex["t_ms"] >= e or iv_at_strike(x, plan["K"])[0] is None:
        return None, "no exit mark"
    return {"day": d, "plan": plan, "debit": debit, "fallbacks": fb, "dte": (e - snap["t_ms"]) / DAY_MS}, ""


def build_all(D: dict[str, Any], days: dict[int, int], P: dict[str, Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    out, why = [], Counter()
    for d in sorted(days):
        b, w = build(D, d, days[d], P)
        if b is None:
            why[w] += 1
        else:
            out.append(b)
    return out, why


def outcome(D: dict[str, Any], b: dict[str, Any], P: dict[str, Any]) -> dict[str, Any]:
    res, why = hold_outcome(b["plan"], b["debit"], D["days"], hold_days=P["hold"], hs=P["hs"], arb_rule=P["arb"])
    if res is None:
        raise RuntimeError(f"exit checked in build but failed: {why} ({ymd_ms(b['day'])})")
    return {**res, "dte": b["dte"]}


def trades(D: dict[str, Any], P: dict[str, Any], days: dict[int, int]) -> list[dict[str, Any]]:
    built, _ = build_all(D, days, P)
    return [outcome(D, b, P) for b in built]


def daily(D: dict[str, Any], ev: dict[int, int], hs: float = 0.015) -> list[dict[str, Any]]:
    """Item 4.6 H3: the daily ATM straddle through the release, held to delivery (same construction and code)."""
    return vp11.trades(D, {**vp11.BASE, "hs": hs}, ev, "straddle")


# ----------------------------------------------------------------------------- stage 0


def coverage(L: list[str], D: dict[str, Any], t0: float) -> dict[str, Any]:
    P = BASE
    ev, ctrl = ev_map(D, P), ctrl_map(D, P)
    L.append(f"## Стадія 0: покриття (поріг {COVERAGE_MIN:.0%}; P&L не рахується)\n")
    L.append(
        f"- днів подій FOMC / CPI / NFP {ymd_ms(min(ev))} … {ymd_ms(max(ev))}: **{len(ev)}**; контрольних днів (пн–пт, без події в [−24 год; +48 год]): {len(ctrl)}\n"
    )
    L.append("| вибірка / конструкція | днів | побудовано | покриття | причини пропуску |")
    L.append("|---|---|---|---|---|")
    out: dict[str, Any] = {"ev": ev, "ctrl_days": ctrl}  # "H1" / "ctrl": the built plans
    rows: list[tuple[str, str, dict[str, Any], dict[int, int] | None]] = [
        ("H1", "**H1: стреддл ≥ 3 д після події, вихід наступного ранку**", P, ev),
        ("ctrl", "**H3-контроль: те саме, дні без події**", P, ctrl),
        *(("", f"відстань від події ≥ {g:.0f} д", {**P, "gap": g}, ev) for g in (2.0, 5.0)),
        ("", "утримання 2 доби (вихід 09:00 T + 2)", {**P, "hold": 2}, ev),
        ("", "вихід до події (вхід T − 1 09:00, вихід T 09:00)", {**P, "lag": 1}, None),
        *(("", f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, ev) for h in (0.0, 0.03)),
        ("", "модель «сторона»", {**P, "model": "side"}, ev),
        ("", "правило (а) «пропуск дня»", {**P, "arb": "skip"}, ev),
        ("", "лише FOMC + CPI (без NFP)", {**P, "kinds": tuple(EVENT_KINDS)}, None),
        *((("", f"лише {k.upper()} (розвідувально)", {**P, "kinds": (k,)}, None)) for k in KINDS),
        ("", "контроль лише вт–чт", {**P, "ctrl_weekdays": (1, 2, 3)}, ctrl_map(D, {**P, "ctrl_weekdays": (1, 2, 3)})),
    ]
    for key, name, Pv, ds in rows:
        days = ds if ds is not None else ev_map(D, Pv)
        built, why = build_all(D, days, Pv)
        cov = len(built) / len(days) if days else 0.0
        L.append(f"| {name} | {len(days)} | {len(built)} | **{cov:.1%}** | {dict(why.most_common())} |")
        if key:
            out[key] = built
            out[f"cov_{key}"] = cov
    log("coverage table done", t0)
    dl, why = vp11.build_all(D, ev, vp11.BASE, "straddle")
    both = {b["day"] for b in dl} & {b["day"] for b in out["H1"]}
    out["cov_H2"] = len(both) / len(ev)
    L.append(
        f"| H2: пара з денним стреддлом (4.6 H3) у той самий день | {len(ev)} | {len(both)} | **{out['cov_H2']:.1%}** | денний побудовано {len(dl)} |"
    )
    by_y = Counter(ymd_ms(d)[:4] for d in ev)
    ok_y = Counter(ymd_ms(b["day"])[:4] for b in out["H1"])
    L.append("\n- H1 по роках (днів подій / побудовано): " + ", ".join(f"{y}: {by_y[y]}/{ok_y[y]}" for y in sorted(by_y)))
    cl = vp11.blocks(sorted(ev) + sorted(ctrl))
    h1, c = out["H1"], out["ctrl"]
    L.append(
        f"- H1: 4-тижневих блоків серед угод {len({cl[b['day']] for b in h1})}; IS / OOS {sum(b['day'] < OOS for b in h1)} / "
        f"{sum(b['day'] >= OOS for b in h1)}; контроль: блоків {len({cl[b['day']] for b in c})}, IS / OOS "
        f"{sum(b['day'] < OOS for b in c)} / {sum(b['day'] >= OOS for b in c)}\n"
    )
    out["daily_n"] = len(dl)
    out["gates"] = {
        "H1": out["cov_H1"] >= COVERAGE_MIN,
        "H2": out["cov_H1"] >= COVERAGE_MIN and out["cov_H2"] >= COVERAGE_MIN,
        "H3": out["cov_H1"] >= COVERAGE_MIN and out["cov_ctrl"] >= COVERAGE_MIN,
    }
    L.append(f"- ворота покриття: {out['gates']}\n")
    return out


def sanity(L: list[str], cov: dict[str, Any]) -> None:
    L.append("## Перевірки до прогону (без P&L)\n")
    for name, built in (("H1", cov["H1"]), ("контроль", cov["ctrl"])):
        L.append(
            f"- {name}: DTE {_q([b['dte'] for b in built], '{:.2f}')}; розподіл {dict(Counter(round(b['dte'], 2) for b in built).most_common(5))}"
        )
        L.append(
            f"- {name}: дебет / спот {_q([b['debit'] / b['plan']['spot'] for b in built], '{:.4f}')}; ATM IV входу {_q([b['plan']['iv'] for b in built])}; "
            f"K − форвард, частка спота {_q([(b['plan']['K'] - b['plan']['F']['m']) / b['plan']['spot'] for b in built], '{:+.4f}')}"
        )
        L.append(f"- {name}: марки входу (а) {dict(Counter(b['plan']['legs'][0]['arb'] for b in built))}")
        L.append(f"- {name}: модель «сторона» — ніг без принта {sum(exec_ivs(b['plan'], 'side')[1] for b in built)} з {2 * len(built)}")
    exp_debit = [0.8 * b["plan"]["iv"] * (b["dte"] / 365) ** 0.5 for b in cov["H1"]]
    L.append(f"- очікування дебету H1 ≈ 0.8 σ √T (без витрат): {_q(exp_debit, '{:.4f}')}")
    L.append(f"- звірка з відомим числом: денний стреддл 4.6 H3 побудовано {cov['daily_n']} (у звіті P11: 172)")
    L.append(f"- EDGE_R (зареєстровано до стадії 0): {EDGE}\n")


# ----------------------------------------------------------------------------- full


def diff_test(L: list[str], a: list[dict[str, Any]], b: list[dict[str, Any]], cl: dict[int, int], name: str) -> dict[str, Any]:
    both = a + b
    labels = [("a", cl[t["day"]]) for t in a] + [("b", cl[t["day"]]) for t in b]
    d = cluster_bootstrap_diff([t["r"] for t in both], labels, [True] * len(a) + [False] * len(b), n_boot=N_BOOT, seed=SEED)
    is_ = mean_r([t for t in a if t["day"] < OOS]) - mean_r([t for t in b if t["day"] < OOS])
    oos = mean_r([t for t in a if t["day"] >= OOS]) - mean_r([t for t in b if t["day"] >= OOS])
    L.append(f"- {name}: {rr(d['diff'])} {ci_r(d['ci'])}, p {d['p']:.3f}; IS {rr(is_)}, OOS {rr(oos)}\n")
    return {"mean": d["diff"], "lo": d["ci"][0], "hi": d["ci"][1], "p": d["p"], "is": is_, "oos": oos, "mde": 2.8 * d["se"], "n": len(both)}


def full(L: list[str], D: dict[str, Any], cov: dict[str, Any], cache: Path, t0: float) -> None:
    P = BASE
    ev, ctrl = cov["ev"], cov["ctrl_days"]
    cl = vp11.blocks(sorted(ev) + sorted(ctrl))
    h1 = trades(D, P, ev)
    (cache / "trades_h1.json").write_text(json.dumps(h1, default=str))
    L.append("## H1: стреддл ≥ 3 д після події, вхід 09:00 дня події, вихід 09:00 наступного дня (R на дебет)\n")
    a1 = vp11.analyse(L, "H1", h1, cl, len(ev), two_sided=True)
    log("H1 done", t0)

    dl = daily(D, ev)
    d2 = paired(h1, dl)
    (cache / "trades_h2_pairs.json").write_text(json.dumps(d2, default=str))
    L.append("## H2: H1 − денний стреддл до поставки (4.6 H3), той самий день (парна різниця R)\n")
    a2 = vp11.analyse(L, "H2", d2, cl, len(ev), two_sided=True)
    L.append(f"- складові пари: H1 {rr(mean_r([{'r': t['a']} for t in d2]))}, денний {rr(mean_r([{'r': t['b']} for t in d2]))}\n")
    log("H2 done", t0)

    c = trades(D, P, ctrl)
    (cache / "trades_ctrl.json").write_text(json.dumps(c, default=str))
    L.append("## H3: подія − контроль (той самий стреддл, дні без події)\n")
    ac = vp11.analyse(L, "контроль", c, cl, len(ctrl), two_sided=True)
    a3 = diff_test(L, h1, c, cl, "різниця R подія − контроль")
    a3["gate"] = {"ok": a1["gate"]["ok"] and ac["gate"]["ok"], "n_loss": a1["gate"]["n_loss"] + ac["gate"]["n_loss"]}
    a3["ptail"] = ac["ptail"]
    # added at stage 0 (before any P&L): event and control days differ in the DTE mix (weekday -> next Friday)
    for nm, fn in (("день тижня", lambda t: datetime.fromtimestamp(t["day"] / 1000, UTC).weekday()), ("DTE", lambda t: round(t["dte"]))):
        s = stratified_diff(h1, c, fn)
        cells = "; ".join(f"{k}: {v[0]} / {v[2]}, {v[1] - v[3]:+.3f}" for k, v in sorted(s["strata"].items()))
        L.append(
            f"- H3 зі стратифікацією за «{nm}» (чутливість стадії 0): {rr(s['diff'])}, відкинуто {s['dropped']}; страти (n подія / n контроль, різниця): {cells}"
        )
    L.append("")
    log("H3 done", t0)

    crush_e = [x for b in build_all(D, ev, P)[0] if (x := iv_change(b["plan"], exit_snapshot(D["days"], b["day"], 1))) is not None]
    crush_c = [x for b in build_all(D, ctrl, P)[0] if (x := iv_change(b["plan"], exit_snapshot(D["days"], b["day"], 1))) is not None]
    L.append(f"- описово: зміна mark IV на страйку вхід → вихід: подія {_q(crush_e, '{:+.4f}')}, контроль {_q(crush_c, '{:+.4f}')}\n")

    P3 = {**P, "hs": 0.03}
    h1s = trades(D, P3, ev)
    st = {
        "H1": mean_r(h1s),
        "H2": mean_r(paired(h1s, daily(D, ev, hs=0.03))),
        "H3": mean_r(h1s) - mean_r(trades(D, P3, ctrl)),
    }
    res = {"H1": a1, "H2": a2, "H3": a3}
    raw = {**PREV_RAW_P, **{f"P9x-{h}": a["p"] for h, a in res.items()}}
    deg = PREV_DEGENERATE | {f"P9x-{h}" for h, a in res.items() if not a["gate"]["ok"]}
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
            holm_p=adj[f"P9x-{h}"],
            is_mean=a["is"],
            oos_mean=a["oos"],
            stress_cost_mean=st[h],
            edge=EDGE[h],
            tail_ok=a["gate"]["ok"],
            coverage_ok=cov["gates"][h],
        )
        L.append(
            f"| {h} | {a['n']} | {rr(a['mean'])} | [{a['lo']:+.3f}; {a['hi']:+.3f}] | {rr(a['is'])} | {rr(a['oos'])} | {rr(st[h])} | "
            f"{adj[f'P9x-{h}']:.3f} | {a['mde']:.3f} | {EDGE[h]:.3f} | {'так' if a['gate']['ok'] else 'ні'} ({a['gate']['n_loss']}) | "
            f"{'так' if cov['gates'][h] else 'ні'} | {rr(a['ptail']['stress_mean'])} | **{vs[h]}** |"
        )
    L.append(f"\nМітки плейбуку за реєстрацією (`event_vega.p9_labels`): {p9_labels(vs['H1'], vs['H2'], vs['H3'])}\n")
    log("verdict done", t0)
    sensitivity(L, D, h1, cl, t0)


def sensitivity(L: list[str], D: dict[str, Any], h1: list[dict[str, Any]], cl: dict[int, int], t0: float) -> None:
    P = BASE
    ev = ev_map(D, P)
    L.append("## Чутливість H1 (усі варіанти зареєстровано заздалегідь; кластер = 4-тижневий блок)\n")
    L += vp11.HDR_S
    grid: list[tuple[str, dict[str, Any], dict[int, int] | None]] = [
        *((f"відстань від події ≥ {g:.0f} д", {**P, "gap": g}, ev) for g in (2.0, 5.0)),
        ("утримання 2 доби", {**P, "hold": 2}, ev),
        ("вихід до події (вхід T − 1, вихід T 09:00)", {**P, "lag": 1}, None),
        *((f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, ev) for h in (0.0, 0.03)),
        ("модель «сторона»", {**P, "model": "side"}, ev),
        ("правило (а) «пропуск дня»", {**P, "arb": "skip"}, ev),
        ("лише FOMC + CPI (без NFP)", {**P, "kinds": tuple(EVENT_KINDS)}, None),
        *((f"лише {k.upper()} (розвідувально)", {**P, "kinds": (k,)}, None) for k in KINDS),
        ("контроль лише вт–чт", {**P, "ctrl_weekdays": (1, 2, 3)}, ctrl_map(D, {**P, "ctrl_weekdays": (1, 2, 3)})),
    ]
    for name, Pv, ds in grid:
        days = ds if ds is not None else ev_map(D, Pv)
        tr = trades(D, Pv, days)
        L.append(vp1.table_row(name, tr, vp11.blocks(sorted(days)), len(days)))
        log(f"grid: {name}", t0)
    top = sorted(h1, key=lambda t: t["r"], reverse=True)[5:]
    L.append(vp1.table_row("H1 без 5 найкращих угод", sorted(top, key=lambda t: t["day"]), cl, len(ev)))
    for k in (2.0, 5.0):
        L.append(vp1.table_row(f"H2 з відстанню ≥ {k:.0f} д", paired(trades(D, {**P, "gap": k}, ev), daily(D, ev)), cl, len(ev)))
    L.append(
        "\nЄдиний актив (BTC): пункт 5а не застосовний; ETH не покрито кешем поверхонь. R — на дебет стреддла (капітал під "
        "ризиком); «% спота» — на номінал 1 BTC на ногу.\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("coverage", "full"), default="coverage")
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/p9_event_vega")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.perf_counter()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    D = vp11.load(Path(a.src_dir).expanduser(), t0)
    L = [
        f"# P9 на 3–5 д: куплена вега через подію FOMC / CPI / NFP, вихід наступного ранку, BTC (stage {a.stage}, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n"
    ]
    cov = coverage(L, D, t0)
    sanity(L, cov)
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
