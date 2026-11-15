"""Playbook rule P1 (ATM IV / RV30 >= 1.0 -> iron condor 15-delta x wing 3 %) on Deribit BTC marks, Bybit costs.

IDEA-20261114-options-playbook, item 4.8; pre-registration: the card's "4.8. P1 ... попередня реєстрація".
No network: the put-skew-v2 cache (``--src-dir``, read only: ``snapshots_v2.json`` daily 09:00 surfaces,
``delivery_btc_usd.json``, ``dvol_BTC.json``) and Binance BTC/USDT 1d closes (``--closes-dir``). The condor and its
P&L are those of P1' (`condor_dvol`, via ``verify_p1_condor_dvol``); the condition is `condor_ivrv`; the tail gate is
`condor_25d`; the verdict `straddle_p5.verdict` (a failed coverage gate gives ◯).

    uv run python scripts/verify_p1_condor_ivrv.py --stage coverage   # stage 0: coverage and sanity, no P&L
    uv run python scripts/verify_p1_condor_ivrv.py --stage full --out ~/.cache/tradedesk-agents/p1_condor_ivrv/full.md
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
import verify_p9_event_vega as vp9  # noqa: E402
from common_helpers import ymd_ms  # noqa: E402
from verify_assistant_recs import load_closes  # noqa: E402

from tradedesk.analytics.calendar_p10 import batch_raw_p  # noqa: E402
from tradedesk.analytics.condor_25d import block_clusters, parametric_tail, tail_gate  # noqa: E402
from tradedesk.analytics.condor_dvol import cluster_bootstrap_diff  # noqa: E402
from tradedesk.analytics.condor_ivrv import atm_iv_forward, dvol_known_at, iv_rv_ratio, rv_known_at  # noqa: E402
from tradedesk.analytics.dvol_filter import cvar, max_drawdown, mean_without_top  # noqa: E402
from tradedesk.analytics.option_condor import block_bootstrap_mean, exec_legs  # noqa: E402
from tradedesk.analytics.put_skew import choose_expiry  # noqa: E402
from tradedesk.analytics.straddle_p5 import verdict  # noqa: E402
from tradedesk.analytics.trend_spread import HOUR_MS, holm_adjust  # noqa: E402

OOS = vp1.OOS
COVERAGE_MIN = 0.60
THRESH = 1.0
# Registered fallback (card 4.8): if the band-0.015 construction covers < 60 % of the condition Saturdays, the
# smallest band of BANDS reaching 60 % becomes the construction of H1-H3 (fixed at stage 0, before any P&L).
BANDS = (0.015, 0.02, 0.03)
BAND: float | None = 0.03  # fixed in the card from stage 0 (0.015 -> 43.6 %, 0.02 -> 57.1 %, 0.03 -> 68.7 %)
EDGE_R: float | None = 0.02  # 0.25 x median credit / width (0.064) of the registered construction, stage 0
N_BOOT = vp3.N_BOOT
SEED = vp3.SEED
MIN_LOSSES = 5
H3_TP = 0.80
PREV_RAW_P = {**vp9.PREV_RAW_P, "P9x-H1": 0.000, "P9x-H2": 0.250, "P9x-H3": 0.277}
PREV_DEGENERATE = set(vp9.PREV_DEGENERATE)
KEY = "P1ivrv"
BASE: dict[str, Any] = {**vp1.BASE, "short_delta": 0.15, "wing": 0.03, "exit": "expiry", "tp": H3_TP, "band": 0.015}

log = vp3.log
rr, ci_r, pc = vp3.rr, vp3.ci_r, vp3.pc
HDR = vp1.HDR


# ----------------------------------------------------------------------------- data and condition


def load(src: Path, closes_dir: Path, kdir: Path, t0: float) -> dict[str, Any]:
    D = vp1.load(src, t0)
    D["stamps"], D["closes"], _info = load_closes(closes_dir, kdir, t0)
    log(f"closes {ymd_ms(D['stamps'][0])} … {ymd_ms(D['stamps'][-1])}: {len(D['closes'])}", t0)
    return D


def saturdays(D: dict[str, Any], P: dict[str, Any]) -> list[int]:
    return vp3.entry_days(D, P["weekday"])


def measure(D: dict[str, Any], d: int, P: dict[str, Any]) -> dict[str, Any]:
    """Condition inputs known at the entry (09:00 of day d): ATM IV of the chosen expiry, RV30, DVOL."""
    t = d + 9 * HOUR_MS
    rv = rv_known_at(D["stamps"], D["closes"], t)
    dv = dvol_known_at(D["dvol"], t)
    snap = D["days"].get(d)
    e = choose_expiry(snap, P["target"], P["lo"], P["hi"], rule="nearest") if snap else None
    iv = atm_iv_forward(snap["expiries"][e]) if e is not None else None
    return {"day": d, "iv": iv, "rv": rv, "ratio": iv_rv_ratio(iv, rv), "dvol": dv, "spread": None if dv is None or rv is None else dv - rv}


def cond_days(M: dict[int, dict[str, Any]], *, key: str = "ratio", thr: float = THRESH, below: bool = False) -> list[int]:
    out = []
    for d, m in M.items():
        v = m[key]
        if v is None:
            continue
        if (v < thr) if below else (v >= thr):
            out.append(d)
    return sorted(out)


def trades(D: dict[str, Any], P: dict[str, Any], days: list[int]) -> list[dict[str, Any]]:
    built, _ = vp1.build_all(D, days, P)
    return [vp1.outcome(D, b, P) for b in built]


def blocks(days: list[int]) -> dict[int, int]:
    return block_clusters(days, start_ms=days[0], weeks=4)


# ----------------------------------------------------------------------------- stage 0


def coverage(L: list[str], D: dict[str, Any], t0: float) -> tuple[dict[int, dict[str, Any]], dict[float, list[dict[str, Any]]]]:
    P = BASE
    days = saturdays(D, P)
    M = {d: measure(D, d, P) for d in days}
    met, unmet = cond_days(M), cond_days(M, below=True)
    unmeasured = [d for d in days if M[d]["ratio"] is None]
    L.append(f"## Стадія 0: покриття (поріг {COVERAGE_MIN:.0%}; P&L не рахується)\n")
    L.append(
        f"- суботи {ymd_ms(days[0])} … {ymd_ms(days[-1])}: {len(days)}; умова виміряна {len(days) - len(unmeasured)} "
        f"(ATM IV / RV30 ≥ {THRESH:.1f}: **{len(met)}**, < {THRESH:.1f}: {len(unmet)}; не виміряна {len(unmeasured)})"
    )
    L.append("| вибірка / конструкція | субот | план побудовано | покриття | причини пропуску |")
    L.append("|---|---|---|---|---|")
    base_by_band: dict[float, list[dict[str, Any]]] = {}
    rows: list[tuple[str, dict[str, Any], list[int]]] = [
        *((f"**умова P1 (ratio ≥ 1.0), допуск {b:.3f}**", {**P, "band": b}, met) for b in BANDS),
        *((f"ratio < 1.0, допуск {b:.3f}", {**P, "band": b}, unmet) for b in BANDS),
        *((f"усі суботи, допуск {b:.3f}", {**P, "band": b}, days) for b in BANDS),
        ("ratio ≥ 0.9", P, cond_days(M, thr=0.9)),
        ("ratio ≥ 1.1", P, cond_days(M, thr=1.1)),
        ("ratio ≥ 1.2", P, cond_days(M, thr=1.2)),
        ("MCP-форма: DVOL − RV30 ≥ 0", P, cond_days(M, key="spread", thr=0.0)),
    ]
    for name, Pv, ds in rows:
        built, why = vp1.build_all(D, ds, Pv)
        cov = len(built) / len(ds) if ds else 0.0
        L.append(f"| {name} | {len(ds)} | {len(built)} | **{cov:.1%}** | {dict(why.most_common())} |")
        if ds is met:
            base_by_band[Pv["band"]] = built
    log("coverage table done", t0)
    by_year: Counter[str] = Counter(ymd_ms(d)[:4] for d in met)
    for b in BANDS:
        ok_year: Counter[str] = Counter(ymd_ms(x["day"])[:4] for x in base_by_band[b])
        L.append(
            f"- умова P1 по роках, допуск {b:.3f} (суботи / побудовано): "
            + ", ".join(f"{y}: {by_year[y]}/{ok_year[y]}" for y in sorted(by_year))
        )
    cl = blocks(days)
    for b in BANDS:
        bb = base_by_band[b]
        L.append(
            f"- допуск {b:.3f}: блоків-кластерів серед побудованих {len({cl[x['day']] for x in bb})} (усього {len(set(cl.values()))}); "
            f"IS / OOS {sum(x['day'] < OOS for x in bb)} / {sum(x['day'] >= OOS for x in bb)}"
        )
    chosen = next((b for b in BANDS if len(base_by_band[b]) / len(met) >= COVERAGE_MIN), None)
    L.append(f"\n- **за реєстрацією обрано допуск: {chosen if chosen is not None else 'жоден (покриття < 60 % при всіх) → ◯'}**\n")
    return M, base_by_band


def sanity(L: list[str], D: dict[str, Any], M: dict[int, dict[str, Any]], base: list[dict[str, Any]], band: float) -> float:
    L.append(f"## Перевірки до прогону (без P&L; конструкція з допуском {band:.3f})\n")
    rat = sorted(m["ratio"] for m in M.values() if m["ratio"] is not None)
    L.append(
        f"- ATM IV / RV30: медіана {statistics.median(rat):.2f} [p10 {rat[len(rat) // 10]:.2f}; p90 {rat[9 * len(rat) // 10]:.2f}], "
        f"частка ≥ 1.0 {sum(r >= 1 for r in rat) / len(rat):.0%}"
    )
    ivs = [m["iv"] for m in M.values() if m["iv"] is not None]
    rvs = [m["rv"] for m in M.values() if m["rv"] is not None]
    gap = [m["dvol"] - m["iv"] for m in M.values() if m["dvol"] is not None and m["iv"] is not None]
    L.append(
        f"- одиниці: ATM IV медіана {statistics.median(ivs):.3f} (мін {min(ivs):.3f}, макс {max(ivs):.3f}); RV30 медіана {statistics.median(rvs):.3f} "
        f"(мін {min(rvs):.3f}, макс {max(rvs):.3f}); DVOL − ATM IV ≈ 13 д: медіана {statistics.median(gap):+.3f}"
    )
    agree = [((m["ratio"] >= 1) == (m["spread"] >= 0)) for m in M.values() if m["ratio"] is not None and m["spread"] is not None]
    L.append(
        f"- збіг умови генератора (ATM IV / RV30 ≥ 1) і MCP-форми (DVOL − RV30 ≥ 0): {sum(agree) / len(agree):.0%} з {len(agree)} субот"
    )
    ds = sorted(abs(b["plan"]["delta"][k]) for b in base for k in ("sp", "sc"))
    L.append(
        f"- фактичні |Δ| проданих: медіана {statistics.median(ds):.3f} [p10 {ds[len(ds) // 10]:.3f}; p90 {ds[9 * len(ds) // 10]:.3f}] (ціль 0.15)"
    )
    wing = sorted(
        x
        for b in base
        for x in (
            (b["plan"]["K"]["sp"] - b["plan"]["K"]["lp"]) / b["plan"]["spot"],
            (b["plan"]["K"]["lc"] - b["plan"]["K"]["sc"]) / b["plan"]["spot"],
        )
    )
    L.append(
        f"- крило / спот: медіана {statistics.median(wing):.2%}, p10 {wing[len(wing) // 10]:.2%}, мін {wing[0]:.2%}, макс {wing[-1]:.2%} (ціль 3 %)"
    )
    fr = sorted(b["credit"] / b["plan"]["width"] for b in base)
    med = statistics.median(fr)
    L.append(
        f"- кредит (після комісій входу) / ширина: медіана {med:.3f} [p10 {fr[len(fr) // 10]:.3f}; p90 {fr[9 * len(fr) // 10]:.3f}], "
        f"мін {fr[0]:.3f}, макс {fr[-1]:.3f} → EDGE_R = 0.25 × медіана = {round(0.25 * med, 2):.2f} R"
    )
    L.append(
        f"- DTE: медіана {statistics.median(b['dte'] for b in base):.2f} д, мін {min(b['dte'] for b in base):.2f}, макс {max(b['dte'] for b in base):.2f}"
    )
    side_fb = sum(exec_legs(b["plan"], "side")[1] for b in base)
    L.append(f"- модель «сторона»: ніг без принта {side_fb} з {4 * len(base)}")
    fr_days = vom.fridays(vom.FIRST, vom.LAST)
    ok = sum(1 for f in fr_days if vom.plan(D, f, "h1")[0] is not None)
    L.append(f"- звірка з відомим числом: покриття кондора 10Δ / ±15 % (options-modules H1) = {ok} / {len(fr_days)} (у звіті 179 / 301)\n")
    return round(0.25 * med, 2)


# ----------------------------------------------------------------------------- full


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
    L.append(f"\n- блоковий бутстреп (угоди, блок 4): {rr(bb['mean'])} {ci_r(bb['ci'])}, p {bb['p']:.3f}")
    L.append(f"- CI для вердикту (об'єднання кластерного й блокового): [{lo:+.3f}; {hi:+.3f}]")
    halves = []
    for k in (0, 1):
        sub = rs[k::2]
        bk = block_bootstrap_mean(sub, block=2, n_boot=N_BOOT, seed=SEED)
        halves.append((sum(1 for r in sub if r <= 0), len(sub)))
        L.append(f"- неперекривна підвибірка {k + 1}: n = {len(sub)}, {rr(bk.get('mean'))} {ci_r(bk.get('ci'))}")
    g = tail_gate(rs, [ep[t["day"]] for t in tr], min_losses=MIN_LOSSES)
    L.append(
        f"- ворота хвоста: збиткових {g['n_loss']} з {g['n']} у {g['n_loss_clusters']} з {g['n_clusters']} кластерів; "
        f"P(ресемпл без збиткового кластера) {g['p_no_loss_resample']:.3f} → **{'пройдено' if g['ok'] else 'НЕ пройдено'}**"
    )
    pt = max((parametric_tail(rs, k_eff=k, n_eff=n, loss_ref=loss_ref) for k, n in halves), key=lambda x: x["q_up"])
    L.append(
        f"- параметричний хвіст: частка збиткових {pt['q_obs']:.1%}, 95 % верхня межа {pt['q_up']:.1%}; виграш {rr(pt['win_mean'])}, "
        f"збиток {rr(pt['loss_mean'])} → стрес-середнє {rr(pt['stress_mean'])}"
    )
    L.append(
        f"- хвіст: найгірший R {min(rs):+.3f}, CVaR 5 % {cvar(rs):+.3f}, просадка суми R {max_drawdown(rs):.2f}; без 5 найкращих "
        f"{mean_without_top(rs, 5):+.3f}; MDE {s['mde']:.3f} R; виходи {dict(Counter(t['exit'] for t in tr))}\n"
    )
    sub_is = [t for t in tr if t["day"] < OOS]
    sub_oos = [t for t in tr if t["day"] >= OOS]
    return {
        **s,
        "lo": lo,
        "hi": hi,
        "gate": g,
        "ptail": pt,
        "is": vp3.summary(sub_is, ep)["mean"] if sub_is else float("nan"),
        "oos": vp3.summary(sub_oos, ep)["mean"] if sub_oos else float("nan"),
    }


def full(L: list[str], D: dict[str, Any], M: dict[int, dict[str, Any]], cache: Path, t0: float, band: float, edge: float) -> None:
    P = {**BASE, "band": band}
    days = saturdays(D, P)
    cl = blocks(days)
    met, unmet = cond_days(M), cond_days(M, below=True)
    h1 = trades(D, P, met)
    (cache / "trades_h1.json").write_text(json.dumps(h1, default=str))
    cov1 = len(h1) / len(met)
    L.append(f"## H1: кондор 15Δ × крило 3 % (допуск {band:.3f}) на суботах з ATM IV / RV30 ≥ 1.0, до експірації\n")
    a1 = analyse(L, "H1", h1, cl, len(met), None)
    loss_ref = [t["r"] for t in h1 if t["r"] <= 0]
    log("H1 done", t0)

    lo_tr = trades(D, P, unmet)
    (cache / "trades_ctrl.json").write_text(json.dumps(lo_tr, default=str))
    cov_lo = len(lo_tr) / len(unmet) if unmet else 0.0
    L.append("## Контроль: той самий кондор на суботах з ratio < 1.0\n")
    a_lo = analyse(L, "ratio < 1", lo_tr, cl, len(unmet), loss_ref)
    both = [(t["r"], (cl[t["day"]], True), True) for t in h1] + [(t["r"], (cl[t["day"]], False), False) for t in lo_tr]
    d2 = cluster_bootstrap_diff([x[0] for x in both], [x[1] for x in both], [x[2] for x in both], n_boot=N_BOOT, seed=SEED)
    L.append(
        f"## H2: R(ratio ≥ 1) − R(ratio < 1) = {rr(d2['diff'])} {ci_r(d2['ci'])}, p {d2['p']:.3f} "
        f"(кластери: 4-тижневий блок × група; {d2['k_a']} / {d2['k_b']}); покриття груп {cov1:.0%} / {cov_lo:.0%}\n"
    )
    h1_is = {t["day"] for t in h1 if t["day"] < OOS}
    lo_is = {t["day"] for t in lo_tr if t["day"] < OOS}
    m = statistics.fmean
    d2_is = m([t["r"] for t in h1 if t["day"] in h1_is]) - m([t["r"] for t in lo_tr if t["day"] in lo_is])
    d2_oos = m([t["r"] for t in h1 if t["day"] >= OOS]) - m([t["r"] for t in lo_tr if t["day"] >= OOS])
    L.append(f"- H2 IS {d2_is:+.3f}, OOS {d2_oos:+.3f}\n")
    log("H2 done", t0)

    P3 = {**P, "exit": "managed", "tp": H3_TP}
    h3 = trades(D, P3, met)
    L.append(f"## H3: вихід за курсом ({H3_TP:.0%} кредиту або спот на проданому страйку), умова P1\n")
    a3 = analyse(L, "H3", h3, cl, len(met), loss_ref)
    log("H3 done", t0)

    st = {
        "H1": vp3.summary(trades(D, {**P, "hs": 0.03}, met), cl)["mean"],
        "H3": vp3.summary(trades(D, {**P3, "hs": 0.03}, met), cl)["mean"],
    }
    lo_st = trades(D, {**P, "hs": 0.03}, unmet)
    st["H2"] = st["H1"] - statistics.fmean(t["r"] for t in lo_st) if lo_st else float("nan")
    a2 = {
        "n": len(h1) + len(lo_tr),
        "mean": d2["diff"],
        "lo": d2["ci"][0],
        "hi": d2["ci"][1],
        "p": d2["p"],
        "is": d2_is,
        "oos": d2_oos,
        "mde": 2.8 * d2["se"],
        "gate": {"ok": a1["gate"]["ok"] and a_lo["gate"]["ok"], "n_loss": a1["gate"]["n_loss"] + a_lo["gate"]["n_loss"]},
        "ptail": {"stress_mean": float("nan")},
    }
    res = {"H1": a1, "H2": a2, "H3": a3}
    cov_ok = {"H1": cov1 >= COVERAGE_MIN, "H2": cov1 >= COVERAGE_MIN and cov_lo >= COVERAGE_MIN, "H3": cov1 >= COVERAGE_MIN}
    raw = {**PREV_RAW_P, **{f"{KEY}-{h}": a["p"] for h, a in res.items()}}
    deg = PREV_DEGENERATE | {f"{KEY}-{h}" for h, a in res.items() if not a["gate"]["ok"]}
    pv = batch_raw_p(raw, deg)
    adj = holm_adjust(pv)
    L.append(f"## Холм партії пункту 4 ({len(pv)} тестів; вироджені → 1.0: {sorted(deg)})\n")
    L.append("- " + ", ".join(f"{k}: {pv[k]:.3f} → {adj[k]:.3f}" for k in sorted(pv)) + "\n")
    L.append("## Вердикти (механічно за реєстрацією, `straddle_p5.verdict`)\n")
    L.append(
        "| гіпотеза | n | середнє R | CI | IS | OOS | стрес 3 в.п. | Холм p | MDE | ворота хвоста | покриття | стрес-хвіст | вердикт |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for h, a in res.items():
        v = verdict(
            mean=a["mean"],
            lo=a["lo"],
            hi=a["hi"],
            holm_p=adj[f"{KEY}-{h}"],
            is_mean=a["is"],
            oos_mean=a["oos"],
            stress_cost_mean=st[h],
            edge=edge,
            tail_ok=a["gate"]["ok"],
            coverage_ok=cov_ok[h],
        )
        L.append(
            f"| {h} | {a['n']} | {rr(a['mean'])} | [{a['lo']:+.3f}; {a['hi']:+.3f}] | {rr(a['is'])} | {rr(a['oos'])} | {rr(st[h])} | "
            f"{adj[f'{KEY}-{h}']:.3f} | {a['mde']:.3f} | {'так' if a['gate']['ok'] else 'ні'} ({a['gate']['n_loss']}) | "
            f"{'так' if cov_ok[h] else 'ні'} | {rr(a['ptail']['stress_mean'])} | **{v}** |"
        )
    L.append(f"\nEDGE_R = {edge:.2f} R (стадія 0); допуск страйків {band:.3f} (стадія 0).\n")
    log("verdict done", t0)

    L.append("## Чутливість H1 (усі варіанти зареєстровано заздалегідь)\n")
    L += HDR
    grid: list[tuple[str, dict[str, Any], list[int]]] = [
        *((f"поріг ratio ≥ {th:.1f}", P, cond_days(M, thr=th)) for th in (0.9, 1.1, 1.2)),
        ("MCP-форма: DVOL − RV30 ≥ 0", P, cond_days(M, key="spread", thr=0.0)),
        ("без умови (усі суботи)", P, days),
        *((f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, met) for h in (0.0, 0.03)),
        ("модель «сторона»", {**P, "model": "side"}, met),
        *((f"Δ {sd:.2f} × крило 3 %", {**P, "short_delta": sd}, met) for sd in (0.10, 0.20)),
        *((f"Δ 0.15 × крило {w:.0%}", {**P, "wing": w}, met) for w in (0.02, 0.05)),
        *((f"допуск страйків {b:.3f}", {**P, "band": b}, met) for b in BANDS if b != band),
        ("вихід за курсом, ціль 50 %", {**P, "exit": "managed", "tp": 0.5}, met),
    ]
    for name, Pv, ds in grid:
        L.append(vp1.table_row(name, trades(D, Pv, ds), cl, len(ds)))
        log(f"grid: {name}", t0)
    fri = vp3.entry_days(D, 4)
    Mf = {d: measure(D, d, {**P, "weekday": 4}) for d in fri}
    L.append(vp1.table_row("вхід у п'ятницю", trades(D, {**P, "weekday": 4}, cond_days(Mf)), blocks(fri), len(cond_days(Mf))))
    top = sorted(h1, key=lambda t: t["r"], reverse=True)[5:]
    L.append(vp1.table_row("H1 без 5 найкращих угод", top, cl, len(met)))
    r = {t["day"]: t["r"] for t in h1}
    rebuilt = []
    for d in met:
        if d in r:
            continue
        for b in (0.03, 0.05):
            if b <= band:
                continue
            x, _ = vp1.build(D, d, {**P, "band": b})
            if x is not None:
                r[d] = vp1.outcome(D, x, {**P, "band": b})["r"]
                rebuilt.append(r[d])
                break
    L.append(
        f"\n- «намір торгувати» (H1 + пропущені суботи з допуском до 0.05): n = {len(r)} з {len(met)}, середній R "
        f"{rr(statistics.fmean(r.values()))}; лише добудовані n = {len(rebuilt)}, середній {rr(statistics.fmean(rebuilt) if rebuilt else None)}"
    )
    L.append(
        "\nЄдиний актив (BTC): пункт 5а не застосовний. R — на макс. збиток (капітал під ризиком); маржа Bybit крилом не зменшується.\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("coverage", "full"), default="coverage")
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--closes-dir", default="~/.cache/tradedesk-agents/assistant_recs")
    ap.add_argument("--klines-dir", default="~/.cache/tradedesk-agents/macro_regimes/klines")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/p1_condor_ivrv")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.perf_counter()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    D = load(Path(a.src_dir).expanduser(), Path(a.closes_dir).expanduser(), Path(a.klines_dir).expanduser(), t0)
    L = [f"# P1: ATM IV / RV30 ≥ 1 → кондор 15Δ × 3 %, BTC (stage {a.stage}, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n"]
    M, by_band = coverage(L, D, t0)
    met = cond_days(M)
    chosen = next((b for b in BANDS if len(by_band[b]) / len(met) >= COVERAGE_MIN), BANDS[0])
    edge0 = sanity(L, D, M, by_band[chosen], chosen)
    log("coverage and sanity done", t0)
    if a.stage == "full":
        if BAND is None or EDGE_R is None:
            raise SystemExit("BAND / EDGE_R are not registered yet: run --stage coverage and fix them in the card first")
        if abs(EDGE_R - edge0) > 1e-9 or BAND != chosen:
            L.append(f"> Увага: у коді BAND {BAND} / EDGE_R {EDGE_R:.2f} ≠ стадія 0 {chosen} / {edge0:.2f}.\n")
        full(L, D, M, cache, t0, BAND, EDGE_R)
    text = "\n".join(L)
    print(text)
    if a.out:
        Path(a.out).expanduser().write_text(text + "\n")
    log("done", t0)


if __name__ == "__main__":
    main()
