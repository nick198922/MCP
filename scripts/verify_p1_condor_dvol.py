"""Playbook rule P1' (iron condor entered when the DVOL 365-day percentile is high) on Deribit BTC marks, Bybit costs.

IDEA-20261114-options-playbook, item 4.2; pre-registration: the card's "4.2. P1' ... попередня реєстрація".
No network: the put-skew-v2 cache (``--src-dir``, read only): ``snapshots_v2.json`` (daily 09:00 surfaces),
``delivery_btc_usd.json``, ``dvol_BTC.json`` (daily DVOL candles), loaded by ``verify_option_modules.load``.

    uv run python scripts/verify_p1_condor_dvol.py --stage coverage   # stage 0: coverage and sanity, no P&L
    uv run python scripts/verify_p1_condor_dvol.py --stage full --out ~/.cache/tradedesk-agents/p1_condor_dvol/full.md
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
import verify_p3_trend_spread as vp3  # noqa: E402
from common_helpers import ymd_ms  # noqa: E402

from tradedesk.analytics.condor_dvol import (  # noqa: E402
    cluster_bootstrap_diff,
    entry_credit,
    entry_dvol_percentiles,
    managed,
    plan_p1,
    settle,
)
from tradedesk.analytics.dvol_filter import cvar, entry_percentiles, max_drawdown, mean_without_top  # noqa: E402
from tradedesk.analytics.option_condor import block_bootstrap_mean, exec_legs  # noqa: E402
from tradedesk.analytics.put_skew import choose_expiry, iv_at_strike  # noqa: E402
from tradedesk.analytics.trend_spread import DAY_MS, episodes, holm_adjust  # noqa: E402

OOS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
LAST_EXPIRY_MS = vom.LAST_EXPIRY_MS
COVERAGE_MIN = 0.60
THRESH = 0.80
# Pre-registered "edge worth the effort" for the ✗ rule, R on max loss per ~13-day condor. Set after stage 0 showed
# credit / width median 0.085 (max profit ~0.09 R), before any P&L: 0.02 R is about a quarter of the max profit.
EDGE_R = 0.02
N_BOOT = vp3.N_BOOT
SEED = vp3.SEED
# P3 (item 4.1) raw two-sided p-values from its report: H1 0.042; H2 Holm 0.038 at rank 1 of 3 -> raw 0.038 / 3;
# H3 Holm 0.842 at rank 3 -> raw 0.842. The batch Holm of item 4 covers P3 H1-H3 and P1' H1-H3.
P3_RAW_P = {"P3-H1": 0.042, "P3-H2": 0.038 / 3, "P3-H3": 0.842}
BASE: dict[str, Any] = {
    "weekday": 5,
    "target": 14.0,
    "lo": 7.0,
    "hi": 24.0,
    "short_delta": 0.15,
    "wing": 0.03,
    "model": "mark",
    "hs": 0.015,
    "band": 0.015,
    "exit": "expiry",
    "tp": 0.80,
    "pct": "vrp",
    "thresh": THRESH,
}

log = vp3.log
rr, ci_r, pc = vp3.rr, vp3.ci_r, vp3.pc


# ----------------------------------------------------------------------------- data


def load(src: Path, t0: float) -> dict[str, Any]:
    D = vom.load(src)
    D["pct"] = {"vrp": entry_dvol_percentiles(D["dvol"]), "f180": entry_percentiles(D["dvol"], window_days=365, min_obs=180)}
    log(f"dvol candles {ymd_ms(D['dvol'][0][0])} … {ymd_ms(D['dvol'][-1][0])}; percentiles: {len(D['pct']['vrp'])} days", t0)
    return D


def entries(D: dict[str, Any], P: dict[str, Any]) -> dict[int, float]:
    """Entry day -> DVOL percentile known at entry, for every entry weekday where the percentile exists."""
    pct = D["pct"][P["pct"]]
    return {d: pct[d] for d in vp3.entry_days(D, P["weekday"]) if d in pct}


def build(D: dict[str, Any], d: int, P: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Entry plan and credit (no outcome)."""
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
    plan, why = plan_p1(snap, e, short_delta=P["short_delta"], wing=P["wing"], band=P["band"])
    if plan is None:
        return None, why
    legs, fb = exec_legs(plan, P["model"], P["hs"])
    credit = entry_credit(plan, legs)
    if credit is None:
        return None, "credit outside (0, width)"
    return {"day": d, "plan": plan, "credit": credit, "fallbacks": fb, "dte": plan["T"] * 365}, ""


def outcome(D: dict[str, Any], b: dict[str, Any], P: dict[str, Any]) -> dict[str, Any]:
    plan, dp = b["plan"], D["delivery"][ymd_ms(b["plan"]["expiry"])]
    if P["exit"] == "expiry":
        res = settle(plan, b["credit"], dp)
    else:
        res = managed(D["days"], plan, b["credit"], dp, tp=P["tp"], breach=True, hs=P["hs"])
    return {
        "day": b["day"],
        "expiry": plan["expiry"],
        "spot": plan["spot"],
        "K": plan["K"],
        "width": plan["width"],
        "credit": b["credit"],
        **res,
    }


def build_all(D: dict[str, Any], days: list[int], P: dict[str, Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    out: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    for d in days:
        b, w = build(D, d, P)
        if b is None:
            why[w] += 1
        else:
            out.append(b)
    return out, why


def state_episodes(pcts: dict[int, float], thr: float, *, both: bool) -> dict[int, int]:
    """Clusters: runs of consecutive entries in the same DVOL state (``both``) or runs of high-state entries only."""
    lab = {d: ("high" if p >= thr else ("low" if both else "neutral")) for d, p in pcts.items()}
    return episodes(list(lab.items()))


# ----------------------------------------------------------------------------- stage 0


def coverage(L: list[str], D: dict[str, Any], t0: float) -> list[dict[str, Any]]:
    P = BASE
    pcts = entries(D, P)
    all_days = vp3.entry_days(D, P["weekday"])
    L.append(f"## Стадія 0: покриття (поріг {COVERAGE_MIN:.0%}; P&L не рахується)\n")
    L.append(
        f"- суботи {ymd_ms(all_days[0])} … {ymd_ms(all_days[-1])}: {len(all_days)}; з відомим перцентилем DVOL (365 рядків, як `deribit_vrp`): {len(pcts)} (від {ymd_ms(min(pcts))})"
    )
    L.append("| вибірка | субот | план побудовано | покриття | причини пропуску |")
    L.append("|---|---|---|---|---|")
    base: list[dict[str, Any]] = []
    hi_days: list[int] = []
    for name, days in (
        *((f"перцентиль ≥ {th:.1f}", [d for d, p in pcts.items() if p >= th]) for th in (0.7, 0.8, 0.9)),
        ("усі з перцентилем (контроль)", sorted(pcts)),
        ("усі суботи 2021+ (описово)", all_days),
    ):
        built, why = build_all(D, sorted(days), P)
        cov = len(built) / len(days) if days else 0.0
        L.append(f"| {name} | {len(days)} | {len(built)} | **{cov:.1%}** | {dict(why.most_common())} |")
        if name.endswith("0.8"):
            base = built
            hi_days = days
    log("coverage table done", t0)
    by_year: Counter[str] = Counter(ymd_ms(d)[:4] for d in hi_days)
    ok_year: Counter[str] = Counter(ymd_ms(b["day"])[:4] for b in base)
    L.append("\n- ≥ 0.8 по роках (суботи / побудовано): " + ", ".join(f"{y}: {by_year[y]}/{ok_year[y]}" for y in sorted(by_year)))
    ep_hi = state_episodes(pcts, THRESH, both=False)
    ep_all = state_episodes(pcts, THRESH, both=True)
    L.append(
        f"- кластери (епізоди): серій субот ≥ 0.8 — {len(set(ep_hi.values()))}, серед побудованих — {len({ep_hi[b['day']] for b in base})}; "
        f"серій обох станів (для H2) — {len(set(ep_all.values()))}; IS / OOS побудованих ≥ 0.8: "
        f"{sum(b['day'] < OOS for b in base)} / {sum(b['day'] >= OOS for b in base)}"
    )
    f180 = D["pct"]["f180"]
    n180 = [d for d in all_days if d in f180 and f180[d] >= THRESH]
    L.append(
        f"- варіант перцентиля `dvol_filter.entry_percentiles` (365 д, від 180 спостережень, описово): субот ≥ 0.8 — {len(n180)} (від {ymd_ms(min(f180))})\n"
    )
    return base


def sanity(L: list[str], D: dict[str, Any], base: list[dict[str, Any]]) -> None:
    L.append("## Перевірки до прогону (без P&L)\n")
    known = {(t // DAY_MS) * DAY_MS + DAY_MS: v for t, v in D["dvol"]}
    diffs = []
    for d in vp3.entry_days(D, 5):
        s = D["days"].get(d)
        if s is None or d not in known:
            continue
        e = choose_expiry(s, 30, 14, 60, rule="nearest")
        if e is None:
            continue
        x = s["expiries"][e]
        iv = iv_at_strike(x, x["F"])[0]
        if iv:
            diffs.append(known[d] - iv)
    L.append(
        f"- одиниці DVOL: значення в кеші {min(v for _, v in D['dvol']):.3f} … {max(v for _, v in D['dvol']):.3f} (десяткові, як IV); "
        f"DVOL (вчора) − ATM mark IV ≈30 д поверхні: n = {len(diffs)}, медіана {statistics.median(diffs):+.3f}, "
        f"медіана |різниці| {statistics.median(abs(x) for x in diffs):.3f}"
    )
    ds = sorted(abs(b["plan"]["delta"][k]) for b in base for k in ("sp", "sc"))
    dw = sorted(abs(b["plan"]["delta"][k]) for b in base for k in ("lp", "lc"))
    L.append(
        f"- фактичні |Δ| проданих ніг: медіана {statistics.median(ds):.3f} [p10 {ds[len(ds) // 10]:.3f}; p90 {ds[9 * len(ds) // 10]:.3f}] (ціль 0.15); "
        f"крил: медіана {statistics.median(dw):.3f}"
    )
    wing = sorted((b["plan"]["K"]["sp"] - b["plan"]["K"]["lp"]) / b["plan"]["spot"] for b in base)
    wing += sorted((b["plan"]["K"]["lc"] - b["plan"]["K"]["sc"]) / b["plan"]["spot"] for b in base)
    wing.sort()
    L.append(f"- крило / спот: медіана {statistics.median(wing):.2%}, мін {wing[0]:.2%}, макс {wing[-1]:.2%} (ціль 3 %, допуск ±1.5 %)")
    fr = [b["credit"] / b["plan"]["width"] for b in base]
    L.append(
        f"- кредит (після комісій входу) / ширина: медіана {statistics.median(fr):.3f}, мін {min(fr):.3f}, макс {max(fr):.3f} (має бути в (0; 1))"
    )
    L.append(
        f"- DTE: медіана {statistics.median(b['dte'] for b in base):.2f} д, мін {min(b['dte'] for b in base):.2f}, макс {max(b['dte'] for b in base):.2f}"
    )
    side_fb = sum(exec_legs(b["plan"], "side")[1] for b in base)
    L.append(f"- модель «сторона»: ніг без принта своєї сторони {side_fb} з {4 * len(base)} (замінено на mark ± 1.5 в.п.)")
    # consistency with a known number: options-modules H1 (10Δ, ±15 %) covered 179 of 301 Fridays
    fr_days = vom.fridays(vom.FIRST, vom.LAST)
    ok = sum(1 for f in fr_days if vom.plan(D, f, "h1")[0] is not None)
    L.append(f"- звірка з відомим числом: покриття кондора 10Δ / ±15 % (options-modules H1) = {ok} / {len(fr_days)} (у звіті 179 / 301)\n")


# ----------------------------------------------------------------------------- full


def trades_for(D: dict[str, Any], P: dict[str, Any], *, thr: float | None) -> tuple[list[dict[str, Any]], int, dict[int, float]]:
    """Trades on entries with percentile >= thr (``thr`` None = every entry with a known percentile)."""
    pcts = entries(D, P)
    days = sorted(d for d, p in pcts.items() if thr is None or p >= thr)
    built, _ = build_all(D, days, P)
    out = [{**outcome(D, b, P), "pct": pcts[b["day"]]} for b in built]
    return out, len(days), pcts


def table_row(label: str, tr: list[dict[str, Any]], ep: dict[int, int], elig: int) -> str:
    cov = len(tr) / elig if elig else 0.0
    return vp3.row(label, tr, ep)[:-1] + f" {cov:.0%} |"


HDR = [
    "| варіант | n | кластерів | середній R (на макс. збиток) | 95 % CI (кластери) | p | % спота (на номінал) | частка R > 0 | покриття |",
    "|---|---|---|---|---|---|---|---|---|",
]


def full(L: list[str], D: dict[str, Any], cache: Path, t0: float) -> None:
    P = BASE
    base, elig, pcts = trades_for(D, P, thr=THRESH)
    ep_hi = state_episodes(pcts, THRESH, both=False)
    s1 = vp3.summary(base, ep_hi)
    (cache / "trades_base.json").write_text(json.dumps(base, default=str))
    L.append("## H1: кондор P1′ при перцентилі DVOL ≥ 0.8, до експірації\n")
    L += HDR
    L.append(table_row("H1 усі", base, ep_hi, elig))
    L.append(table_row("IS (до 2024)", [t for t in base if t["day"] < OOS], ep_hi, elig))
    L.append(table_row("OOS (2024+)", [t for t in base if t["day"] >= OOS], ep_hi, elig))
    for y in sorted({ymd_ms(t["day"])[:4] for t in base}):
        L.append(table_row(f"рік {y}", [t for t in base if ymd_ms(t["day"])[:4] == y], ep_hi, elig))
    rs = [t["r"] for t in sorted(base, key=lambda t: t["day"])]
    bb = block_bootstrap_mean(rs, block=4, n_boot=N_BOOT, seed=SEED)
    L.append(f"\n- блоковий бутстреп (тижні, блок 4): {rr(bb.get('mean'))} {ci_r(bb.get('ci95'))}")
    for k in (0, 1):
        sub = rs[k::2]
        bk = block_bootstrap_mean(sub, block=2, n_boot=N_BOOT, seed=SEED)
        L.append(f"- неперекривна підвибірка {k + 1} (кожен другий вхід): n = {len(sub)}, {rr(bk.get('mean'))} {ci_r(bk.get('ci95'))}")
    L.append(
        f"- хвіст: найгірший R {min(rs):+.3f}, CVaR 5 % {cvar(rs):+.3f}, найбільша просадка суми R {max_drawdown(rs):.2f}; "
        f"без 5 найкращих {mean_without_top(rs, 5):+.3f}; MDE {s1['mde']:.3f} R; виходи {dict(Counter(t['exit'] for t in base))}\n"
    )
    log("H1 done", t0)

    allp, _, _ = trades_for(D, P, thr=None)
    ep_all = state_episodes(pcts, THRESH, both=True)
    d2 = cluster_bootstrap_diff(
        [t["r"] for t in allp], [ep_all[t["day"]] for t in allp], [t["pct"] >= THRESH for t in allp], n_boot=N_BOOT, seed=SEED
    )
    L.append("## H2: чи додає фільтр — R(≥ 0.8) − R(< 0.8), ті самі суботи, кластери = серії стану\n")
    L.append(f"- різниця {rr(d2['diff'])} {ci_r(d2['ci'])}, p {d2['p']:.3f}, кластерів {d2['k_a']} / {d2['k_b']}")
    n_lo = sum(1 for t in allp if t["pct"] < THRESH)
    e_lo = sum(1 for p in pcts.values() if p < THRESH)
    cov_lo = n_lo / e_lo if e_lo else 0.0
    gate = "пройдено" if min(cov_lo, len(base) / elig if elig else 0.0) >= COVERAGE_MIN else "НЕ пройдено → H2 лише розвідувально (◯)"
    L.append(f"- покриття груп: ≥ 0.8 {len(base)}/{elig}, < 0.8 {n_lo}/{e_lo} ({cov_lo:.1%}); ворота H2 (обидві ≥ 60 %): {gate}")
    L += HDR
    L.append(table_row("безумовний вхід (усі з перцентилем)", allp, ep_all, len(pcts)))
    L.append(table_row("< 0.8", [t for t in allp if t["pct"] < THRESH], ep_all, len(pcts)))
    log("H2 done", t0)

    man, _, _ = trades_for(D, {**P, "exit": "managed"}, thr=THRESH)
    s3 = vp3.summary(man, ep_hi)
    L.append("\n## H3: вихід за курсом (80 % кредиту або спот на проданому страйку), ≥ 0.8\n")
    L += HDR
    L.append(table_row("H3", man, ep_hi, elig))
    L.append(f"- виходи {dict(Counter(t['exit'] for t in man))}\n")

    stress, _, _ = trades_for(D, {**P, "hs": 0.03}, thr=THRESH)
    s_stress = vp3.summary(stress, ep_hi)
    pv = {**P3_RAW_P, "P1-H1": s1["p"], "P1-H2": d2["p"], "P1-H3": s3["p"]}
    adj = holm_adjust(pv)
    L.append("## Холм партії пункту 4 (P3 H1–H3 + P1′ H1–H3)\n")
    L.append("- " + ", ".join(f"{k}: сирий {pv[k]:.3f} → {adj[k]:.3f}" for k in sorted(pv)))

    is_m = vp3.summary([t for t in base if t["day"] < OOS], ep_hi)["mean"]
    oos_m = vp3.summary([t for t in base if t["day"] >= OOS], ep_hi)["mean"]
    # registered: few clusters make the cluster CI unreliable, so the verdict uses the union of the cluster-bootstrap
    # and the weekly block-bootstrap CIs (the wider bound on each side)
    lo, hi = s1["ci"]
    if bb.get("ci95"):
        lo, hi = min(lo, bb["ci95"][0]), max(hi, bb["ci95"][1])
    L.append(f"- CI для вердикту (об'єднання кластерного й блокового): [{lo:+.3f}; {hi:+.3f}]; кластерів серед угод {s1['n_clusters']}")
    if lo > 0 and adj["P1-H1"] < 0.05 and oos_m > 0 and s_stress["mean"] > 0:
        v = "✓"
    elif hi < 0 or (s1["mean"] <= 0 and hi < EDGE_R):
        v = "✗"
    elif s1["mean"] > 0 and is_m > 0 and oos_m > 0:
        v = "≈"
    else:
        v = "◯"
    L.append(
        f"\n**Вердикт H1 (механічно за реєстрацією): {v}** — середній R {rr(s1['mean'])} {ci_r(s1['ci'])}, IS {rr(is_m)}, OOS {rr(oos_m)}, "
        f"стрес 3 в.п. {rr(s_stress['mean'])}, Холм p {adj['P1-H1']:.3f}, MDE {s1['mde']:.3f} R\n"
    )
    log("verdict done", t0)

    L.append("## Чутливість (усі варіанти зареєстровано заздалегідь)\n")
    L += HDR
    grid: list[tuple[str, dict[str, Any], float | None]] = [
        *((f"поріг {th:.1f}", P, th) for th in (0.7, 0.9)),
        ("без фільтра", P, None),
        *((f"половина спреду {h * 100:.1f} в.п.", {**P, "hs": h}, THRESH) for h in (0.0, 0.03)),
        ("модель «сторона»", {**P, "model": "side"}, THRESH),
        *(
            (f"Δ {sd:.2f} × крило {w:.0%}", {**P, "short_delta": sd, "wing": w}, THRESH)
            for sd in (0.10, 0.15, 0.20)
            for w in (0.02, 0.03, 0.05)
            if (sd, w) != (0.15, 0.03)
        ),
        *((f"ціль DTE {tg:.0f} д", {**P, "target": tg}, THRESH) for tg in (7.0, 21.0)),
        ("вхід у п'ятницю", {**P, "weekday": 4}, THRESH),
        *((f"вихід за курсом, ціль {tp:.0%}", {**P, "exit": "managed", "tp": tp}, THRESH) for tp in (0.5,)),
        ("перцентиль 365 д від 180 спостережень", {**P, "pct": "f180"}, THRESH),
    ]
    for name, Pv, th in grid:
        tr, el, pv_ = trades_for(D, Pv, thr=th)
        ep = state_episodes(pv_, th if th is not None else THRESH, both=th is None)
        L.append(table_row(name, tr, ep, el))
        log(f"grid: {name}", t0)
    top = sorted(base, key=lambda t: t["r"], reverse=True)[5:]
    L.append(table_row("без 5 найкращих угод", top, ep_hi, elig))
    L.append(
        "\nЄдиний актив (BTC): пункт 5а (по активах) не застосовний; ETH не покрито кешем поверхонь. Маржа Bybit не зменшується крилом, тож R на макс. збиток — верхня межа дохідності на фактичну маржу.\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("coverage", "full"), default="coverage")
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/p1_condor_dvol")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.perf_counter()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    D = load(Path(a.src_dir).expanduser(), t0)
    L = [f"# P1′: кондор за рівнем DVOL, BTC (stage {a.stage}, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n"]
    base = coverage(L, D, t0)
    sanity(L, D, base)
    log("coverage and sanity done", t0)
    if a.stage == "full":
        full(L, D, cache, t0)
    text = "\n".join(L)
    print(text)
    if a.out:
        Path(a.out).expanduser().write_text(text + "\n")
    log("done", t0)


if __name__ == "__main__":
    main()
