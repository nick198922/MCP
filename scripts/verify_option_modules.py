"""Option modules H1 (iron condor), H4 (size by DVOL percentile), H5 (weekend condor) on Deribit BTC.

IDEA-20261031-structural-edges-depth, item 7; pre-registration: the card's "Пункт 7. Попередня реєстрація".
No network: the put-skew-v2 cache (``--src-dir``, read only): ``snapshots_v2.json`` (daily 09:00 surfaces),
``delivery_btc_usd.json``, ``dvol_BTC.json``. Results go to stdout and ``--out``.

    uv run python scripts/verify_option_modules.py --stage coverage   # stage 0: leg coverage only, no P&L
    uv run python scripts/verify_option_modules.py --stage full --out ~/.cache/tradedesk-agents/options_modules/full.md
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import resource
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_put_skew as vps  # noqa: E402  (scripts/ is not a package)

from tradedesk.analytics.option_condor import (  # noqa: E402
    LEGS,
    block_bootstrap_mean,
    capital_paths,
    cvar,
    dvol_size,
    exec_legs,
    permutation_band,
    plan_condor,
    ruin_gates,
    scaled_minus_equal,
    settle_condor,
)
from tradedesk.analytics.pm_modules import cochran_q, holm  # noqa: E402
from tradedesk.analytics.put_skew import (  # noqa: E402
    chain,
    choose_expiry,
    dvol_known_at_entry,
    inverse_price,
    trailing_percentile,
)
from tradedesk.analytics.weekly_straddle import (  # noqa: E402
    DAY_MS,
    HOUR_MS,
    deribit_im_estimate,
    friday_expiry,
    fridays,
    mde,
    newey_west_mean,
)

FIRST = int(datetime(2021, 1, 1, tzinfo=UTC).timestamp() * 1000)
LAST = int(datetime(2026, 10, 2, tzinfo=UTC).timestamp() * 1000)
OOS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
LAST_EXPIRY_MS = vps.LAST_EXPIRY_MS
COVERAGE_MIN = 0.60
BAND = 0.015
SEED = 20261031
PCT = 100.0
REPO = Path(__file__).resolve().parent.parent
EVENTS = REPO / "docs" / "verification" / "2026-10-15-us-macro-events.csv"

# module -> (wing, expiry rule); "ctrl" enters on Tuesday (Friday + 4 days)
MODULES = {"h1": 0.15, "h5": 0.08, "ctrl": 0.08, "m30": 0.15}
MODELS = {
    "side": ("side", 0.015),
    "mark": ("mark", 0.015),
    "zero": ("mark", 0.0),
    "stress": ("mark", 0.03),
    "side_only": ("side_only", 0.015),
}


def ymd(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def pc(x: float | None, k: float = PCT) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * k:+.2f}"


def ci_s(ci: tuple[float, float] | None, k: float = PCT) -> str:
    return f"[{ci[0] * k:+.2f}; {ci[1] * k:+.2f}]" if ci else "—"


def load(src: Path) -> dict[str, Any]:
    t0 = time.perf_counter()
    days, _meta = vps.load_snapshots(src, "v2")
    delivery = {r["date"]: float(r["delivery_price"]) for r in json.loads((src / "delivery_btc_usd.json").read_text())}
    dvol = [(int(t), float(v)) for t, v in json.loads((src / "dvol_BTC.json").read_text())]
    print(
        f"loaded {len(days)} surfaces, {len(delivery)} delivery, {len(dvol)} dvol in {time.perf_counter() - t0:.0f}s, rss {rss_mb():.0f} MB",
        flush=True,
    )
    return {"days": days, "delivery": delivery, "dvol": dvol}


def entry_and_expiry(D: dict[str, Any], fri: int, module: str) -> tuple[int, int | None]:
    if module == "h1":
        return fri, friday_expiry(fri, 1)
    if module == "h5":
        return fri, fri + 3 * DAY_MS + 8 * HOUR_MS
    if module == "ctrl":
        tue = fri + 4 * DAY_MS
        return tue, tue + 2 * DAY_MS + 8 * HOUR_MS
    snap = D["days"].get(fri)
    return fri, (choose_expiry(snap, 30, 21, 56, rule="calendar_monthly") if snap else None)


def plan(D: dict[str, Any], fri: int, module: str, band: float = BAND) -> tuple[dict[str, Any] | None, str]:
    d, e = entry_and_expiry(D, fri, module)
    snap = D["days"].get(d)
    if snap is None:
        return None, "no surface"
    if e is None:
        return None, "no expiry"
    if e > LAST_EXPIRY_MS:
        return None, "expiry after cache end"
    p, why = plan_condor(snap, e, wing=MODULES[module], band=band)
    if p is not None and ymd(e) not in D["delivery"]:
        return None, "no delivery price"
    return p, why


# ----------------------------------------------------------------------------- stage 0


def coverage(D: dict[str, Any], L: list[str]) -> dict[str, float]:
    fr = fridays(FIRST, LAST)
    L.append(f"## Стадія 0: покриття (поріг {COVERAGE_MIN:.0%}, правило ±{BAND:.1%}; P&L не рахується)\n")
    L.append("| модуль | п'ятниць | покрито ±1.5 % | частка | ±0.75 % (описово) | крило за межею w/3 | ноги без принта своєї сторони |")
    L.append("|---|---|---|---|---|---|---|")
    out: dict[str, float] = {}
    reasons: dict[str, Counter[str]] = {}
    known = dvol_known_at_entry(D["dvol"])
    for m in MODULES:
        dv_in: list[float] = []
        dv_out: list[float] = []
        ok = strict = floor = 0
        miss = Counter()
        rs: Counter[str] = Counter()
        by_year: Counter[str] = Counter()
        by_year_ok: Counter[str] = Counter()
        for f in fr:
            by_year[ymd(f)[:4]] += 1
            p, why = plan(D, f, m)
            if f in known:
                (dv_out if p is None else dv_in).append(known[f])
            if p is None:
                rs[why] += 1
                continue
            ok += 1
            by_year_ok[ymd(f)[:4]] += 1
            floor += p["floor"]["lp"] or p["floor"]["lc"]
            for k in LEGS:
                side = p["sell"][k] if k in ("sp", "sc") else p["buy"][k]
                miss[k] += side is None
            if plan(D, f, m, band=0.0075)[0] is not None:
                strict += 1
        share = ok / len(fr)
        out[m] = share
        reasons[m] = rs
        ms = " / ".join(f"{k} {miss[k]}" for k in LEGS)
        L.append(f"| {m} | {len(fr)} | {ok} | {share:.1%} | {strict / len(fr):.1%} | {floor} | {ms} |")
        L.append("")
        L.append(f"  {m} за роками: " + ", ".join(f"{y} {by_year_ok[y]}/{by_year[y]}" for y in sorted(by_year)))
        L.append(f"  {m} причини пропуску: " + ", ".join(f"{k} {v}" for k, v in rs.most_common()))
        if dv_in and dv_out:
            L.append(
                f"  {m} DVOL на вході (відбір, не P&L): покриті n {len(dv_in)} медіана {statistics.median(dv_in):.3f}, "
                f"непокриті n {len(dv_out)} медіана {statistics.median(dv_out):.3f}"
            )
        L.append("")
    return out


# ----------------------------------------------------------------------------- trades


def build(D: dict[str, Any], module: str, model: str) -> dict[int, dict[str, Any]]:
    kind, hs = MODELS[model]
    out: dict[int, dict[str, Any]] = {}
    for f in fridays(FIRST, LAST):
        p, why = plan(D, f, module)
        if p is None:
            continue
        legs, fb = exec_legs(p, kind, hs)
        r = settle_condor(D["days"], p, legs, D["delivery"][ymd(p["expiry"])])
        if r is None:
            continue
        r["fallback"] = fb
        r["friday"] = f
        im = 0.0
        for k in LEGS:
            K, c = p["K"][k], k in ("sc", "lc")
            mark_btc = inverse_price(p["F"], K, p["T"], p["mark"][k], c)
            im += deribit_im_estimate(K, p["spot"], c, mark_btc) if k in ("sp", "sc") else mark_btc  # BTC per 1 BTC = share of spot
        r["sm_margin"] = im
        r["width"] = p["width"] / p["spot"]
        out[f] = r
    if module == "m30":  # non-overlapping chain of monthly entries
        keep = set(chain({d: {"expiry": r["expiry"]} for d, r in out.items()}, FIRST, LAST))
        out = {d: r for d, r in out.items() if d in keep}
    return out


def summary(tr: dict[int, dict[str, Any]], key: str = "pnl") -> dict[str, Any]:
    ds = sorted(tr)
    v = [tr[d][key] for d in ds]
    b = block_bootstrap_mean(v, seed=SEED)
    nw = newey_west_mean(v, 1)
    worst = min(v) if v else float("nan")
    top = sorted(v)[:-10] if len(v) > 10 else []
    oos = [tr[d][key] for d in ds if d >= OOS]
    ins = [tr[d][key] for d in ds if d < OOS]
    return {
        **b,
        "nw_ci": nw["ci95"],
        "mde": mde(b["sd"], b["n"]) if b["n"] > 1 else float("nan"),
        "worst": worst,
        "worst_day": ymd(ds[v.index(worst)]) if v else "—",
        "cvar5": cvar(v) if v else float("nan"),
        "no_top10": sum(top) / len(top) if top else float("nan"),
        "is": sum(ins) / len(ins) if ins else float("nan"),
        "oos": sum(oos) / len(oos) if oos else float("nan"),
        "gates": ruin_gates(worst, b["ci"][1]) if b["ci"] else None,
    }


ROW_HDR = "| вибірка | n | середнє, % спота | 95 % CI (блоки 4 тиж.) | NW | p | SD | MDE | 2021–23 | 2024–26 | без топ-10 | найгірший (дата) | CVaR 5 % |"


def row(label: str, s: dict[str, Any]) -> str:
    return (
        f"| {label} | {s['n']} | {pc(s['mean'])} | {ci_s(s['ci'])} | {ci_s(s['nw_ci'])} | {s['p']:.3f} | {pc(s['sd'])} | {pc(s['mde'])} | "
        f"{pc(s['is'])} | {pc(s['oos'])} | {pc(s['no_top10'])} | {pc(s['worst'])} ({s['worst_day']}) | {pc(s['cvar5'])} |"
    )


def events_by_week() -> list[tuple[str, str]]:
    with EVENTS.open() as fh:
        return [(r["date"], r["kind"]) for r in csv.DictReader(fh) if r.get("status", "actual") == "actual"]


# ----------------------------------------------------------------------------- full


def full(D: dict[str, Any], cov: dict[str, float], L: list[str], *, exploratory: bool = False) -> None:
    t0 = time.perf_counter()
    live = {m: exploratory or cov[m] >= COVERAGE_MIN for m in MODULES}
    if exploratory:
        L.append(
            "# [exploratory] Відхилення від реєстрації: модулі з покриттям < 60 % пораховано на покритих тижнях. "
            "Вибірка відібрана (не випадкова), p і Холм нижче — не для вердикту.\n"
        )
    T: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
    for m in MODULES:
        if not live[m]:
            continue
        for model in MODELS:
            T[(m, model)] = build(D, m, model)
        print(f"  built {m} in {time.perf_counter() - t0:.0f}s, rss {rss_mb():.0f} MB", flush=True)

    L.append("## Усі варіанти (P&L у % спота входу на 1 BTC номіналу кожної ноги, на номінал)\n")
    L.append(ROW_HDR)
    L.append("|" + "---|" * 13)
    S: dict[tuple[str, str], dict[str, Any]] = {}
    for (m, model), tr in T.items():
        S[(m, model)] = s = summary(tr)
        L.append(row(f"{m} {model}", s))
    L.append("")

    for m in ("h1", "h5", "ctrl", "m30"):
        if (m, "side") not in T:
            L.append(f"{m}: знято на стадії 0 (покриття {cov[m]:.1%} < 60 %).")
            continue
        tr = T[(m, "side")]
        v = list(tr.values())
        prem = statistics.mean(r["premium"] for r in v)
        ml = statistics.mean(r["max_loss"] for r in v)
        sm = statistics.mean(r["sm_margin"] for r in v)
        fb = sum(r["fallback"] for r in v)
        wins = sum(r["pnl"] > 0 for r in v)
        mean = S[(m, "side")]["mean"]
        per_year = 52 if m != "m30" else 12
        L.append(
            f"- **{m} (сторона продавця):** кредит {pc(prem)} %, макс. збиток {pc(ml)} %, ширина {pc(statistics.mean(r['width'] for r in v))} %, "
            f"виграшних {wins}/{len(v)}, запасних ніг (без принта) {fb} з {4 * len(v)}; ворота (д) {S[(m, 'side')]['gates']}; "
            f"H10: на ризик-капітал {mean / ml * per_year * PCT:+.1f} %/рік, на оцінку SM-маржі {mean / sm * per_year * PCT:+.1f} %/рік (SM {pc(sm)} % спота, припущення), "
            f"на номінал {mean * per_year * PCT:+.1f} %/рік; пут-сторона виплата {pc(statistics.mean(r['payoff_put'] for r in v))} %, кол {pc(statistics.mean(r['payoff_call'] for r in v))} %."
        )
    L.append("")

    # ---- H1 per year (5a substitute), events, capital paths
    pvals: dict[str, float] = {"H1": 1.0, "H4": 1.0, "H5": 1.0}
    if ("h1", "side") in T:
        tr = T[("h1", "side")]
        pvals["H1"] = S[("h1", "side")]["p"]
        L.append("### H1 по роках (5a: актив один; Q / I² між роками)\n")
        L.append("| рік | n | середнє | 95 % CI | найгірший |")
        L.append("|---|---|---|---|---|")
        means, ses = [], []
        for y in sorted({ymd(d)[:4] for d in tr}):
            vv = [tr[d]["pnl"] for d in sorted(tr) if ymd(d)[:4] == y]
            b = block_bootstrap_mean(vv, seed=SEED)
            L.append(f"| {y} | {b['n']} | {pc(b['mean'])} | {ci_s(b['ci'])} | {pc(min(vv))} |")
            if b["n"] > 2:
                means.append(b["mean"])
                ses.append(b["sd"] / math.sqrt(b["n"]))
        q = cochran_q(means, ses)
        L.append(f"\nCochran Q = {q['q']:.2f} (df {q['df']:.0f}), p = {q['p']:.3f}, I² = {q['i2']:.0%}\n")
        ev = events_by_week()
        flag = {d: [k for day, k in ev if ymd(d) <= day < ymd(tr[d]["expiry"])] for d in tr}
        a = [tr[d]["pnl"] for d in tr if flag[d]]
        b2 = [tr[d]["pnl"] for d in tr if not flag[d]]
        if a and b2:
            L.append(
                f"Тижні з FOMC/CPI/NFP (описово, досьє H6): n {len(a)}, середнє {pc(statistics.mean(a))}, найгірший {pc(min(a))}, CVaR 5 % {pc(cvar(a))}; "
                f"без подій: n {len(b2)}, середнє {pc(statistics.mean(b2))}, найгірший {pc(min(b2))}, CVaR 5 % {pc(cvar(b2))}."
            )
        worst10 = sorted(tr, key=lambda d: tr[d]["pnl"])[:10]
        L.append(
            "10 найгірших тижнів: "
            + ", ".join(
                f"{ymd(d)} {pc(tr[d]['pnl'])} (рух {pc(tr[d]['move'])}{', ' + '/'.join(flag[d]) if flag[d] else ''})" for d in worst10
            )
        )
        caps = [0.01 * tr[d]["pnl"] / tr[d]["max_loss"] for d in sorted(tr) if tr[d]["max_loss"] > 0]
        cp = capital_paths(caps, seed=SEED)
        L.append(
            f"\nH11 (ризик 1 % капіталу на тиждень = макс. збиток, бутстреп 52 тиж., блоки 4, 10 000): P(просадка > 15 %) = {cp['p_dd']:.1%}, "
            f"медіана річної дохідності {pc(cp['median'])} %, 5-й перцентиль {pc(cp['p05'])} %; середня тижнева {pc(statistics.mean(caps))} % капіталу.\n"
        )

        # ---- H4
        known = dvol_known_at_entry(D["dvol"])
        pct = trailing_percentile(sorted(known.items()), window_ms=365 * DAY_MS, min_obs=180)
        ds = [d for d in sorted(tr) if d in pct]
        size = {d: dvol_size(pct[d]) for d in ds}
        ins = [d for d in ds if d < OOS]
        oos = [d for d in ds if d >= OOS]
        s_bar = statistics.mean(size[d] for d in ins)
        d_oos = scaled_minus_equal([tr[d]["pnl"] for d in oos], [size[d] for d in oos], s_bar)
        d_is = scaled_minus_equal([tr[d]["pnl"] for d in ins], [size[d] for d in ins], s_bar)
        b4 = block_bootstrap_mean(d_oos, seed=SEED)
        pvals["H4"] = b4["p"]
        lo, hi = permutation_band([tr[d]["pnl"] for d in oos], [size[d] for d in oos], s_bar, seed=SEED)
        raw = [(size[d] - 1.0) * tr[d]["pnl"] for d in oos]
        p_ = [pct[d] for d in oos]
        y_ = [tr[d]["pnl"] for d in oos]
        mx, my = statistics.mean(p_), statistics.mean(y_)
        slope = sum((a1 - mx) * (b1 - my) for a1, b1 in zip(p_, y_, strict=True)) / sum((a1 - mx) ** 2 for a1 in p_)
        y24 = [x for d, x in zip(oos, d_oos, strict=True) if ymd(d)[:4] == "2024"]
        y256 = [x for d, x in zip(oos, d_oos, strict=True) if ymd(d)[:4] > "2024"]
        sc_oos = [size[d] / s_bar * tr[d]["pnl"] for d in oos]
        L.append("### H4: розмір за перцентилем DVOL (OOS 2024–26; IS 2021–23 описово)\n")
        L.append(
            f"- s̄_IS = {s_bar:.3f} (n IS {len(ins)}); частки розмірів OOS: "
            + ", ".join(f"{k}× {sum(1 for d in oos if size[d] == k)}" for k in (0.5, 1.0, 1.5))
        )
        L.append(
            f"- **d (масштабований − рівний, OOS):** n {b4['n']}, {pc(b4['mean'])} % {ci_s(b4['ci'])}, p {b4['p']:.3f}, SD {pc(b4['sd'])}, MDE {pc(mde(b4['sd'], b4['n']))}"
        )
        L.append(
            f"- плацебо (1 000 перестановок розмірів): [{pc(lo)}; {pc(hi)}]; IS d = {pc(statistics.mean(d_is))}; 2024 {pc(statistics.mean(y24))}, 2025–26 {pc(statistics.mean(y256))}"
        )
        L.append(f"- сирий (s − 1)·pnl OOS {pc(statistics.mean(raw))}; нахил pnl на P_t (OOS, МНК) {pc(slope)} п.п. на одиницю P")
        L.append(
            f"- OOS масштабований: середнє {pc(statistics.mean(sc_oos))}, найгірший {pc(min(sc_oos))}, CVaR 5 % {pc(cvar(sc_oos))}; "
            f"рівний: середнє {pc(statistics.mean(y_))}, найгірший {pc(min(y_))}, CVaR 5 % {pc(cvar(y_))}\n"
        )
        # robustness (exploratory, written after seeing the H4 number)
        L.append("H4 стійкість [exploratory, після перегляду результату]:\n")
        L.append("| розмір | IS n | IS середнє pnl | OOS n | OOS середнє pnl |")
        L.append("|---|---|---|---|---|")
        for k in (0.5, 1.0, 1.5):
            a1 = [tr[d]["pnl"] for d in ins if size[d] == k]
            b1 = [tr[d]["pnl"] for d in oos if size[d] == k]
            L.append(
                f"| {k}× | {len(a1)} | {pc(statistics.mean(a1)) if a1 else '—'} | {len(b1)} | {pc(statistics.mean(b1)) if b1 else '—'} |"
            )
        s_oos = statistics.mean(size[d] for d in oos)
        d_same = scaled_minus_equal(y_, [size[d] for d in oos], s_oos)
        L.append(
            f"\n- s̄ = середнє OOS ({s_oos:.3f}; той самий середній розмір на OOS, чиста коваріація): {pc(statistics.mean(d_same))} {ci_s(block_bootstrap_mean(d_same, seed=SEED)['ci'])}"
        )
        srt = sorted(range(len(d_oos)), key=lambda i: -d_oos[i])
        for cut in (5, 10):
            rest = [d_oos[i] for i in srt[cut:]]
            L.append(
                f"- без {cut} найбільших внесків у d: {pc(statistics.mean(rest))} (n {len(rest)}); ці тижні: "
                + ", ".join(ymd(oos[i]) for i in srt[:cut])
            )
        for model in ("mark", "zero", "stress"):
            tm = T.get(("h1", model), {})
            oo = [d for d in oos if d in tm]
            dm = scaled_minus_equal([tm[d]["pnl"] for d in oo], [size[d] for d in oo], s_bar)
            bm = block_bootstrap_mean(dm, seed=SEED)
            L.append(f"- модель {model}: d {pc(bm['mean'])} {ci_s(bm['ci'])} (n {bm['n']})")
        L.append("")

    # ---- H5 paired with the control
    if ("h5", "side") in T:
        pvals["H5"] = S[("h5", "side")]["p"]
        if ("ctrl", "side") in T:
            h5, ct = T[("h5", "side")], T[("ctrl", "side")]
            both = [d for d in sorted(h5) if d in ct]
            dd = [h5[d]["pnl"] - ct[d]["pnl"] for d in both]
            pc5 = [
                h5[d]["pnl"] / h5[d]["premium"] - ct[d]["pnl"] / ct[d]["premium"]
                for d in both
                if h5[d]["premium"] > 0 and ct[d]["premium"] > 0
            ]
            bd = block_bootstrap_mean(dd, seed=SEED)
            bc = block_bootstrap_mean(pc5, seed=SEED)
            L.append("### H5: вихідні − контроль (вівторок → четвер), ті самі тижні (описово)\n")
            L.append(
                f"- у % спота: n {bd['n']}, {pc(bd['mean'])} {ci_s(bd['ci'])}; на одиницю кредиту: n {bc['n']}, {bc['mean']:+.3f} [{bc['ci'][0]:+.3f}; {bc['ci'][1]:+.3f}]"
            )
            L.append(
                f"- кредит: вихідні {pc(statistics.mean(h5[d]['premium'] for d in both))} %, контроль {pc(statistics.mean(ct[d]['premium'] for d in both))} %; "
                f"P&L / кредит: вихідні {statistics.mean(h5[d]['pnl'] / h5[d]['premium'] for d in both if h5[d]['premium'] > 0):+.3f}, "
                f"контроль {statistics.mean(ct[d]['pnl'] / ct[d]['premium'] for d in both if ct[d]['premium'] > 0):+.3f}\n"
            )

    hm = holm(pvals)
    L.append("## Холм (m = 3, двосторонні, α = 0.05; знятий модуль p = 1)\n")
    for k, v in hm.items():
        L.append(f"- {k}: p {v['p']:.4f}, поріг {v['threshold']:.4f}, відхилено H0: {v['reject']}")
    L.append(f"\nчас {time.perf_counter() - t0:.0f} с, пік RSS {rss_mb():.0f} МБ")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-dir", type=Path, default=Path.home() / ".cache" / "tradedesk-agents" / "put_skew")
    ap.add_argument("--stage", choices=("coverage", "full"), default="coverage")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--exploratory", action="store_true", help="also run modules that failed the stage-0 coverage gate (labelled)")
    a = ap.parse_args()
    D = load(a.src_dir)
    L: list[str] = []
    cov = coverage(D, L)
    if a.stage == "full":
        full(D, cov, L, exploratory=a.exploratory)
    text = "\n".join(L)
    print(text, flush=True)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text + "\n")
    print(f"done, peak RSS {rss_mb():.0f} MB", flush=True)


if __name__ == "__main__":
    main()
