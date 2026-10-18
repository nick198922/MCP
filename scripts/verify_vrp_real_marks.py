"""VRP on real Deribit marks (IDEA-20261017-vrp-real-marks, step 1).

Pre-registration: the idea card, section "Попередня реєстрація". Short ATM straddle, listed expiry
nearest to 30 days (DTE in [21; 56]), sold at mark - 1 vol point at 09:00 UTC, delta-hedged daily with
the perp, perp funding charged, held to the 08:00 UTC delivery. Secondary: the same trade only when
DVOL is in the top quintile of its trailing year (Holm over the two hypotheses).

No network: every input is already cached by `scripts/verify_put_skew.py` (Deribit trade prints ->
daily surfaces ``snapshots_v2.json``, delivery prices, BTC-PERPETUAL funding, DVOL) and, for the anchor
reproduction of the 2026-10-13 VRP report, Binance BTCUSDT 1h klines cached by the macro-regimes run.

Usage:
    uv run python scripts/verify_vrp_real_marks.py --cache-dir ~/.cache/tradedesk-agents/vrp_real_marks --stage0
    uv run python scripts/verify_vrp_real_marks.py --cache-dir ~/.cache/tradedesk-agents/vrp_real_marks --out report.md
Options ``--put-skew-cache`` and ``--klines-dir`` default to the sibling caches.
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from verify_put_skew import STRESS, _ms, load_snapshots

from tradedesk.analytics.carry import pearson
from tradedesk.analytics.put_skew import (
    FundingIndex,
    block_bootstrap_ci,
    chain,
    dvol_known_at_entry,
    is_monthly_expiry,
    plan_entry,
    trailing_percentile,
)
from tradedesk.analytics.vrp import align_daily, short_straddle_hedged, stats
from tradedesk.analytics.vrp_marks import (
    bootstrap_summary,
    classify_primary,
    classify_secondary,
    hedge_path,
    hedged_book,
    holm,
    mde,
    plan_straddle,
    select_signal_days,
    straddle_legs,
)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
ENTRY_START_MS = int(datetime(2021, 4, 1, tzinfo=UTC).timestamp() * 1000)
LAST_EXPIRY_MS = int(datetime(2026, 10, 13, 8, tzinfo=UTC).timestamp() * 1000)
SPLIT_MS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)

# ---- pre-registered constants (idea card, "Попередня реєстрація") ----
PRIMARY_RULE = ("nearest", (21, 56))
ALT_RULES = {"calendar_monthly [21; 56]": ("calendar_monthly", (21, 56)), "nearest [21; 45]": ("nearest", (21, 45))}
SPREAD_BASE = 0.01  # sell at mark - 1 vol point
FEES = {"option_fee": 0.0003, "delivery_fee": 0.00015, "hedge_fee": 0.0005}
STRESS_COST = {"spread": 0.015, "hedge_fee": 0.001}
PCT_WINDOW_MS = 365 * DAY_MS
PCT_MIN_OBS = 180
PCT_THRESHOLD = 0.80
BLOCK = 3  # bootstrap block, in consecutive non-overlapping trades (~ a quarter)
N_BOOT = 10_000
SEED = 20261018
SD_PRIOR_PRIMARY = 0.037  # put-skew v2 straddle phase 0: half-width 0.89 % at n = 66
SD_PRIOR_SECONDARY = 0.045  # critic's triage: SD in the top quintile >= 4-5 %
MIN_EFFECT = 0.005
RUIN = -0.20
MDE_CAP_SECONDARY = 0.02


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def ymd(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


def log(msg: str, t0: float) -> None:
    print(f"[{time.perf_counter() - t0:6.0f}s, rss {rss_mb():.0f} MB] {msg}", flush=True)


# ----------------------------------------------------------------------------- data


def load_inputs(ps_cache: Path) -> dict[str, Any]:
    delivery = {r["date"]: float(r["delivery_price"]) for r in json.loads((ps_cache / "delivery_btc_usd.json").read_text())}
    funding = [(int(t), float(v)) for t, v in json.loads((ps_cache / "funding_btc_perp.json").read_text())]
    dvol = [(int(t), float(v)) for t, v in json.loads((ps_cache / "dvol_BTC.json").read_text())]
    dv_known = dvol_known_at_entry(dvol)
    pct = trailing_percentile(sorted(dv_known.items()), window_ms=PCT_WINDOW_MS, min_obs=PCT_MIN_OBS)
    return {"delivery": delivery, "funding": funding, "fidx": FundingIndex(funding), "dvol": dvol, "dv_known": dv_known, "pct": pct}


def make_plans(days: dict[int, dict[str, Any]], rule: tuple[str, tuple[int, int]], delivery: dict[str, float]) -> dict[int, dict[str, Any]]:
    """Covered entry plans (ATM call delta in [0.40; 0.60]) whose expiry is inside the data and has a delivery price."""
    out = {}
    for d, snap in days.items():
        if d < ENTRY_START_MS:
            continue
        p = plan_straddle(snap, dte_band=rule[1], expiry_rule=rule[0])
        if p is None or not p["covered"] or p["expiry"] > LAST_EXPIRY_MS or ymd(p["expiry"]) not in delivery:
            continue
        out[d] = p
    return out


def entry_days(rule: tuple[str, tuple[int, int]]) -> list[int]:
    hi = rule[1][1]
    return list(range(ENTRY_START_MS, LAST_EXPIRY_MS - hi * DAY_MS, DAY_MS))


def phase_chain(trades: dict[int, dict[str, Any]], k: int = 0) -> list[dict[str, Any]]:
    if not trades:
        return []
    return [trades[d] for d in chain(trades, ENTRY_START_MS + k * DAY_MS, max(trades))]


# ----------------------------------------------------------------------------- books


def run_variant(
    days: dict[int, dict[str, Any]],
    plans: dict[int, dict[str, Any]],
    inp: dict[str, Any],
    *,
    entry_vol: str = "mark",
    hedge_vol: str = "marks",
    spread: float = SPREAD_BASE,
    side: bool = False,
    funding: bool = True,
    hedge: bool = True,
    hedge_fee: float = FEES["hedge_fee"],
    zero_fees: bool = False,
) -> dict[int, dict[str, Any]]:
    out = {}
    dv = inp["dv_known"]
    for d, p in plans.items():
        if entry_vol == "dvol":
            if d not in dv:
                continue
            iv = dv[d] - spread
        elif side:
            if not p.get("sell_iv"):
                continue
            iv = p["sell_iv"]
        else:
            iv = p["mark_iv"] - spread
        path = hedge_path(days, d, p["expiry"], [p["k"], p["k"]], vol_by_day=dv if hedge_vol == "dvol" else None)
        r = hedged_book(
            p,
            straddle_legs(p["k"], iv),
            path,
            p["expiry"],
            inp["delivery"][ymd(p["expiry"])],
            funding=inp["fidx"] if funding else (),
            option_fee=0.0 if zero_fees else FEES["option_fee"],
            delivery_fee=0.0 if zero_fees else FEES["delivery_fee"],
            hedge_fee=0.0 if zero_fees else hedge_fee,
            hedge=hedge,
        )
        r["dte"] = p["dte"]
        r["mark_iv"] = p["mark_iv"]
        r["dvol"] = dv.get(d)
        r["vega_pct"] = abs(r["vega"]) / p["spot"]  # fraction of spot per 1 vol point (black76 vega is per vol point)
        r["monthly"] = is_monthly_expiry(p["expiry"])
        out[d] = r
    return out


# ----------------------------------------------------------------------------- reporting

HDR = "| Вибірка | n | середнє | 95% CI (блоковий бутстреп) | p | частка > 0 | медіана | найгірше | найгірший MTM |\n|---|---|---|---|---|---|---|---|---|"


def row(label: str, tr: list[dict[str, Any]], key: str = "pnl", block: int = BLOCK) -> str:
    vals = [t[key] for t in tr]
    if len(vals) < 2:
        return f"| {label} | {len(vals)} | — | — | — | — | — | — | — |"
    b = bootstrap_summary(vals, block=block, n_boot=N_BOOT, seed=SEED)
    mtm = min((t.get("mtm_min", t[key]) for t in tr), default=float("nan"))
    return (
        f"| {label} | {b['n']} | {b['mean'] * 100:+.2f} | [{b['ci'][0] * 100:+.2f}; {b['ci'][1] * 100:+.2f}] | {b['p']:.3f} | "
        f"{sum(1 for v in vals if v > 0) / len(vals):.0%} | {statistics.median(vals) * 100:+.2f} | {min(vals) * 100:+.2f} | {mtm * 100:+.2f} |"
    )


def phases_line(trades: dict[int, dict[str, Any]], key: str = "pnl") -> str:
    pm, lows = [], 0
    for k in range(30):
        ch = phase_chain(trades, k)
        if len(ch) < 2:
            continue
        v = [t[key] for t in ch]
        pm.append(statistics.mean(v))
        b = bootstrap_summary(v, block=BLOCK, n_boot=2000, seed=SEED + k)
        lows += b["ci"][0] > 0
    if not pm:
        return "30 фаз: —"
    return (
        f"30 фаз старту: середнє від {min(pm) * 100:+.2f} до {max(pm) * 100:+.2f} (медіана {statistics.median(pm) * 100:+.2f}); "
        f"нижня межа CI > 0 у {lows}/{len(pm)} фаз."
    )


def all_days_ci(trades: dict[int, dict[str, Any]], key: str = "pnl") -> str:
    rows = [trades[d][key] for d in sorted(trades)]
    ci = block_bootstrap_ci(rows, lambda xs: sum(xs) / len(xs), block=60, n_boot=2000, seed=SEED)
    m = statistics.mean(rows)
    return f"усі дні старту (перекриваються): n = {len(rows)}, середнє {m * 100:+.2f}" + (
        f", блоковий бутстреп (60 днів) 95% CI [{ci[0] * 100:+.2f}; {ci[1] * 100:+.2f}]" if ci else ""
    )


def stage0(L: list[str], days: dict[int, dict[str, Any]], inp: dict[str, Any], t0: float) -> dict[str, Any]:
    """Data density and power, no P&L."""
    L.append("## Стадія 0: щільність марок ATM ≈ 30 д і потужність (без P&L)\n")
    L.append(
        "| Правило експірації | днів входу | покрито | частка | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 | медіана DTE | вік mark ATM, год (медіана / p90) | угод фази 0 |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    out: dict[str, Any] = {}
    for label, rule in [("**основне: nearest [21; 56]**", PRIMARY_RULE)] + list(ALT_RULES.items()):
        plans = make_plans(days, rule, inp["delivery"])
        base = entry_days(rule)
        ok = [d for d in base if d in plans]
        by_y = []
        for y in range(2021, 2027):
            ys = [d for d in base if ymd(d)[:4] == str(y)]
            by_y.append(f"{sum(1 for d in ys if d in plans) / len(ys):.0%}" if ys else "—")
        dte = [plans[d]["dte"] for d in ok]
        ages = sorted(plans[d]["mark_age_h"] for d in ok if plans[d]["mark_age_h"] is not None)
        n0 = len(chain(plans, ENTRY_START_MS, max(plans))) if plans else 0
        L.append(
            f"| {label} | {len(base)} | {len(ok)} | {len(ok) / len(base):.1%} | " + " | ".join(by_y) + f" | {statistics.median(dte):.1f} | "
            f"{statistics.median(ages):.2f} / {ages[int(0.9 * (len(ages) - 1))]:.2f} | {n0} |"
        )
        out[rule] = plans
    plans = out[PRIMARY_RULE]
    n0 = len(chain(plans, ENTRY_START_MS, max(plans)))
    sig = select_signal_days(plans, inp["pct"], PCT_THRESHOLD)
    n_sig = len(chain(sig, ENTRY_START_MS, max(plans))) if sig else 0
    pct_days = [d for d in plans if d in inp["pct"]]
    L.append(
        f"\nПерцентиль DVOL (вікно 365 д, ≥ {PCT_MIN_OBS} спостережень, DVOL закриття d−1): є для {len(pct_days)} з {len(plans)} покритих днів; "
        f"днів із P ≥ {PCT_THRESHOLD:.2f}: {len(sig)} ({len(sig) / max(1, len(pct_days)):.0%}); неперекривних угод умовної гіпотези: {n_sig}."
    )
    m1, m2 = mde(SD_PRIOR_PRIMARY, n0), mde(SD_PRIOR_SECONDARY, n_sig)
    m1h = mde(SD_PRIOR_PRIMARY, n0, alpha=0.025)
    L.append(
        f"\nMDE (α = 0.05 двобічно, потужність 80 %, SD з апріорних оцінок): основна — SD {SD_PRIOR_PRIMARY * 100:.1f} %, n = {n0} → "
        f"**{m1 * 100:.2f} %** спота за угоду (за Holm-порогом 0.025: {m1h * 100:.2f} %); "
        f"умовна — SD {SD_PRIOR_SECONDARY * 100:.1f} %, n = {n_sig} → **{m2 * 100:.2f} %** "
        f"({'> 2 п.п.: за реєстрацією результат умовної гіпотези описовий, ◯' if m2 > MDE_CAP_SECONDARY else '≤ 2 п.п.'})."
    )
    log(f"stage 0 done: primary chain {n0}, secondary chain {n_sig}", t0)
    return {"plans": out, "n0": n0, "n_sig": n_sig, "mde1": m1, "mde2": m2}


def binance_rows(kdir: Path, dvol: list[tuple[int, float]]) -> list[dict[str, float]]:
    """Daily rows (DVOL candle d, Binance close of day d = close of the 23:00 hourly candle) for the VRP model."""
    closes: dict[int, float] = {}
    for p in sorted(kdir.glob("BTCUSDT-1h-*.json")):
        for b in json.loads(p.read_text()):
            ts = int(b[0])
            if ts % DAY_MS == 23 * HOUR_MS:
                closes[ts - 23 * HOUR_MS] = float(b[4])
    end = int(datetime(2026, 10, 12, tzinfo=UTC).timestamp() * 1000)
    return align_daily([(t, v) for t, v in dvol if t <= end], sorted(closes.items()))


def vrp_phase0(rows: list[dict[str, float]], **kw: Any) -> list[dict[str, float]]:
    """Phase-0 non-overlapping trades exactly as `scripts/verify_vrp.py` (start +0, step 30 rows)."""
    out, i = [], 0
    while i + 30 < len(rows):
        t = short_straddle_hedged(rows, i, **kw)
        if t is not None:
            out.append(t)
        i += 30
    return out


def full(L: list[str], days: dict[int, dict[str, Any]], inp: dict[str, Any], s0: dict[str, Any], a: argparse.Namespace, t0: float) -> None:
    plans = s0["plans"][PRIMARY_RULE]
    dv = inp["dv_known"]

    # ---------------- consistency checks
    L.append("\n## Перевірки узгодженості\n")
    pairs = [(p["mark_iv"], dv[d]) for d, p in plans.items() if d in dv]
    L.append(
        f"- ATM mark IV (основне правило, 09:00) проти DVOL (закриття d−1): {statistics.mean(x for x, _ in pairs) * 100:.1f}% проти "
        f"{statistics.mean(y for _, y in pairs) * 100:.1f}%, різниця {statistics.mean(y - x for x, y in pairs) * 100:+.2f} вп, "
        f"кореляція {pearson(pairs):.3f}, n = {len(pairs)}."
    )
    dd = [abs(days[d]["spot"] / inp["delivery"][ymd(d)] - 1) for d in plans if ymd(d) in inp["delivery"]]
    L.append(
        f"- Спот входу (index 09:00) проти delivery 08:00 того ж дня: середнє |відхилення| {statistics.mean(dd) * 100:.2f}%, n = {len(dd)}."
    )
    # known number 1: the put-skew v2 straddle comparison (+0.06 % [-0.83; +0.95], n = 66)
    ps_plans = {}
    for d, snap in days.items():
        if d < ENTRY_START_MS:
            continue
        p = plan_entry(snap, target_days=30, dte_band=(21, 56), expiry_rule="calendar_monthly")
        if p and p["covered"] and p["expiry"] <= LAST_EXPIRY_MS and ymd(p["expiry"]) in inp["delivery"]:
            x = snap["expiries"][p["expiry"]]
            ps_plans[d] = dict(p, k=p["ka"], ratio=x["ratio"], mark_iv=x["strikes"][p["ka"]]["mark_iv"])
    ps = run_variant(days, ps_plans, inp)
    ps0 = [t["pnl"] for t in phase_chain(ps)]
    st = stats(ps0)
    L.append(
        f"- Відтворення відомого числа put-skew v2 (стреддл на днях покриття put-skew, calendar_monthly, mark − 1 вп, funding): фаза 0 "
        f"{st['mean'] * 100:+.2f}% [{st['ci95'][0] * 100:+.2f}; {st['ci95'][1] * 100:+.2f}] (t-CI), n = {st['n']}; опубліковано +0.06 [−0.83; +0.95], n = 66."
    )
    # known number 2: the 2026-10-13 VRP report (DVOL model, Binance closes): +1.96 [+0.79; +3.13], n = 67
    rows = binance_rows(Path(a.klines_dir).expanduser(), inp["dvol"])
    base_kw = {"entry_cost_vol": 0.01, "option_fee": 0.0003, "hedge_fee": 0.0005}
    v0 = vrp_phase0(rows, **base_kw)
    sv = stats([t["pnl"] for t in v0])
    L.append(
        f"- Відтворення VRP-звіту 2026-10-13 (DVOL-модель, закриття Binance з годинних свічок): фаза 0 {sv['mean'] * 100:+.2f}% "
        f"[{sv['ci95'][0] * 100:+.2f}; {sv['ci95'][1] * 100:+.2f}] (t-CI), n = {sv['n']}; опубліковано +1.96 [+0.79; +3.13], n = 67. "
        f"Днів у рядах: {len(rows)} ({ymd(int(rows[0]['day']) * DAY_MS)} … {ymd(int(rows[-1]['day']) * DAY_MS)})."
    )
    log("consistency checks done", t0)

    # ---------------- primary
    prim = run_variant(days, plans, inp)
    log(f"primary books: {len(prim)}", t0)
    (Path(a.cache_dir).expanduser() / "trades_primary.json").write_text(json.dumps({str(d): r for d, r in sorted(prim.items())}))
    p0 = phase_chain(prim)
    v1 = [t["pnl"] for t in p0]
    b1 = bootstrap_summary(v1, block=BLOCK, n_boot=N_BOOT, seed=SEED)
    sig = select_signal_days(prim, inp["pct"], PCT_THRESHOLD)
    q0 = phase_chain(sig)
    v2 = [t["pnl"] for t in q0]
    b2 = bootstrap_summary(v2, block=BLOCK, n_boot=N_BOOT, seed=SEED)
    adj = holm([b1["p"], b2["p"]])
    worst_all = min(t["pnl"] for t in prim.values())
    worst_sig = min(t["pnl"] for t in sig.values())
    verdict1 = classify_primary(b1["mean"], b1["ci"], adj[0], worst_all, min_effect=MIN_EFFECT, ruin=RUIN)
    verdict2 = classify_secondary(b2["mean"], b2["ci"], adj[1], worst_sig, s0["mde2"], mde_cap=MDE_CAP_SECONDARY, ruin=RUIN)

    L.append("\n## Основна гіпотеза H1: короткий ATM-стреддл ≈ 30 д (nearest [21; 56]), mark − 1 вп, щоденний хедж перпом, funding\n")
    L.append(
        f"Результат — % спота входу за угоду, на номінал (1 BTC кожного опціону), USD-лінійно, після витрат. Середній DTE {statistics.mean(t['dte'] for t in p0):.1f} д.\n"
    )
    L.append(HDR)
    L.append(row("**неперекривні, фаза 0 (основна)**", p0))
    L.append(row("2021–2023, фаза 0", [t for t in p0 if t["day"] < SPLIT_MS]))
    L.append(row("2024–2026, фаза 0", [t for t in p0 if t["day"] >= SPLIT_MS]))
    srt = sorted(p0, key=lambda t: t["pnl"], reverse=True)
    L.append(row("фаза 0 без топ-3 угод", srt[3:]))
    L.append(row("фаза 0 без найгіршої угоди", srt[:-1]))
    n30 = [dict(t, pnl30=t["pnl"] * 30 / t["dte"]) for t in p0]
    L.append(row("фаза 0, нормовано на 30 д (× 30/DTE), розвідувально", n30, key="pnl30"))
    st1 = stats(v1)
    L.append(f"\nt-CI фази 0 (довідка): [{st1['ci95'][0] * 100:+.2f}; {st1['ci95'][1] * 100:+.2f}], SD {st1['sd'] * 100:.2f} %.")
    L.append(phases_line(prim))
    L.append(all_days_ci(prim))
    L.append("\nПо роках (усі дні старту, перекриваються):\n")
    L.append(HDR)
    for y in sorted({ymd(d)[:4] for d in prim}):
        L.append(row(y, [t for d, t in sorted(prim.items()) if ymd(d)[:4] == y], block=30))
    comp = {k: statistics.mean(t[k] for t in p0) for k in ("premium", "payoff", "hedge_pnl", "funding_pnl", "costs")}
    L.append(
        "\nСкладові фази 0, % спота: "
        + ", ".join(f"{k} {v * 100:+.2f}" for k, v in comp.items())
        + f"; vega стреддла {statistics.mean(t['vega_pct'] for t in p0) * 100:.3f} % спота на вол-пункт."
    )
    L.append(
        f"Угод фази 0 на місячних експіраціях (остання п'ятниця): {sum(t['monthly'] for t in p0)}/{len(p0)}; "
        f"усіх днів старту: {sum(t['monthly'] for t in prim.values()) / len(prim):.0%}."
    )
    stale = sum(t["stale_leg_days"] for t in prim.values()) / max(1, 2 * sum(t["hedge_days"] for t in prim.values()))
    L.append(f"Частка нога-днів хеджу без марки експірації (вола перенесена з попереднього дня): {stale:.2%}.")

    # ---------------- ruin
    L.append("\n### Ризик руїни (найгірший місяць у % спота)\n")
    allv = sorted(t["pnl"] for t in prim.values())
    w0 = min(p0, key=lambda t: t["pnl"])
    wa = min(prim.values(), key=lambda t: t["pnl"])
    wm = min(prim.values(), key=lambda t: t["mtm_min"])
    L.append(
        f"- Найгірша угода фази 0: {ymd(w0['day'])} → {ymd(w0['expiry'])}: {w0['pnl'] * 100:+.2f}% (рух {w0['move'] * 100:+.1f}%, IV {w0['mark_iv'] * 100:.0f}%)."
    )
    L.append(
        f"- Найгірша угода серед усіх днів старту: {ymd(wa['day'])} → {ymd(wa['expiry'])}: {wa['pnl'] * 100:+.2f}% (поріг руїни {RUIN * 100:.0f}%)."
    )
    L.append(f"- Найгірший внутрішній MTM (усі дні старту): {ymd(wm['day'])}: {wm['mtm_min'] * 100:+.2f}% спота.")
    L.append(
        f"- Хвіст усіх днів старту: 1-й перцентиль {allv[int(0.01 * len(allv))] * 100:+.2f}%, 5-й {allv[int(0.05 * len(allv))] * 100:+.2f}%; "
        f"найгірша угода фази 0 = {abs(w0['pnl'] / b1['mean']) if b1['mean'] else float('nan'):.1f}× середнього за модулем."
    )
    L.append("\nСтрес-вікна (усі угоди, що тримались через дату):\n")
    L.append("| Вікно | n угод | середнє | найгірше | угода фази 0 |\n|---|---|---|---|---|")
    for lab, x, y in STRESS:
        lo, hi = _ms(x), _ms(y) + DAY_MS
        sel = [t for t in prim.values() if t["day"] < hi and t["expiry"] > lo]
        ph = [t for t in p0 if t["day"] < hi and t["expiry"] > lo]
        if sel:
            L.append(
                f"| {lab} | {len(sel)} | {statistics.mean(t['pnl'] for t in sel) * 100:+.2f} | {min(t['pnl'] for t in sel) * 100:+.2f} | "
                + (", ".join(f"{ymd(t['day'])}: {t['pnl'] * 100:+.2f}" for t in ph) or "—")
                + " |"
            )
    worst5 = sorted(prim.values(), key=lambda t: t["pnl"])[:5]
    L.append(
        "\nНайгірші старти: "
        + "; ".join(f"{ymd(t['day'])}: {t['pnl'] * 100:+.1f}% (рух {t['move'] * 100:+.0f}%, IV {t['mark_iv'] * 100:.0f}%)" for t in worst5)
        + "."
    )

    # ---------------- secondary
    L.append(f"\n## Вторинна гіпотеза H2: та сама угода лише при P_t ≥ {PCT_THRESHOLD:.2f} (перцентиль DVOL за 365 д)\n")
    L.append(HDR)
    L.append(row("**верхній квінтиль, неперекривні (основна для H2)**", q0))
    rest = {d: t for d, t in prim.items() if d in inp["pct"] and inp["pct"][d] < PCT_THRESHOLD}
    L.append(row("решта (P < 0.80), неперекривні", phase_chain(rest)))
    L.append(row("верхній квінтиль, 2021–2023", [t for t in q0 if t["day"] < SPLIT_MS]))
    L.append(row("верхній квінтиль, 2024–2026", [t for t in q0 if t["day"] >= SPLIT_MS]))
    L.append(
        "\n"
        + all_days_ci(sig).replace("усі дні старту", "верхній квінтиль, усі дні старту")
        + " (дні сигналу зібрані в кілька епізодів, блоки йдуть по списку днів, а не по календарю: CI завужений, лише довідка)."
    )
    ws = min(sig.values(), key=lambda t: t["pnl"])
    wsm = min(sig.values(), key=lambda t: t["mtm_min"])
    L.append(
        f"Ризик руїни у квінтилі: найгірша угода (усі дні) {ymd(ws['day'])}: {ws['pnl'] * 100:+.2f}%; найгірша фази 0 "
        f"{min(v2) * 100:+.2f}%; найгірший MTM {ymd(wsm['day'])}: {wsm['mtm_min'] * 100:+.2f}%."
    )
    L.append("\nСтійкість (розвідувально):\n")
    L.append(HDR)
    for thr in (0.70, 0.90):
        L.append(row(f"поріг {thr:.2f}, неперекривні", phase_chain(select_signal_days(prim, inp["pct"], thr))))
    pct365 = trailing_percentile(sorted(dv.items()), window_ms=PCT_WINDOW_MS, min_obs=365)
    L.append(row("поріг 0.80, повне вікно (≥ 365 спостережень)", phase_chain(select_signal_days(prim, pct365, PCT_THRESHOLD))))
    reg = [(inp["pct"][t["day"]], t["pnl"]) for t in p0 if t["day"] in inp["pct"]]
    if len(reg) > 3:
        r_ = pearson(reg)
        tstat = r_ * math.sqrt(len(reg) - 2) / math.sqrt(max(1e-12, 1 - r_ * r_))
        p_r = 2 * (1 - statistics.NormalDist().cdf(abs(tstat)))
        L.append(
            f"\nРегресія P&L фази 0 на перцентиль (усі угоди фази 0 з перцентилем, n = {len(reg)}): кореляція {r_:+.3f}, "
            f"t = {tstat:.2f}, p ≈ {p_r:.3f} (нормальне наближення); MDE кореляції ≈ {2.8 / math.sqrt(len(reg)):.2f}."
        )
    allp = [(inp["pct"][d], t["pnl"]) for d, t in prim.items() if d in inp["pct"]]
    top = [v for p, v in allp if p >= PCT_THRESHOLD]
    oth = [v for p, v in allp if p < PCT_THRESHOLD]
    L.append(
        f"Картковий H2 (описово, усі дні старту на марках): верхній квінтиль {statistics.mean(top) * 100:+.2f}% (n = {len(top)}) мінус решта "
        f"{statistics.mean(oth) * 100:+.2f}% (n = {len(oth)}) = {(statistics.mean(top) - statistics.mean(oth)) * 100:+.2f} п.п."
    )

    # ---------------- decomposition
    L.append("\n## Розклад розриву «DVOL-модель → марки» (ті самі угоди, парно)\n")
    ladder = [
        (
            "L1: вхід DVOL − 1 вп, хедж на DVOL, без funding",
            run_variant(days, plans, inp, entry_vol="dvol", hedge_vol="dvol", funding=False),
        ),
        ("L2: вхід mark − 1 вп, хедж на DVOL, без funding", run_variant(days, plans, inp, hedge_vol="dvol", funding=False)),
        ("L3: вхід mark − 1 вп, хедж на марках, без funding", run_variant(days, plans, inp, funding=False)),
        ("L4 = основна: + funding хеджу", prim),
    ]
    log("ladder books done", t0)
    # R0: the 2026-10-13 model (Binance close of the entry day, DVOL candle d, exactly 30 d) on the same entry days
    idx = {int(r["day"]): i for i, r in enumerate(rows)}
    r0 = {}
    for t in p0:
        i = idx.get(t["day"] // DAY_MS)
        if i is not None and (x := short_straddle_hedged(rows, i, **base_kw)) is not None:
            r0[t["day"]] = x
    common = [t["day"] for t in p0 if t["day"] in r0 and all(t["day"] in lv for _, lv in ladder)]
    L.append(f"Угоди фази 0 основної схеми, для яких є всі щаблі: n = {len(common)}.\n")
    L.append("| Щабель | середнє фази 0 | 95% CI | усі дні старту |\n|---|---|---|---|")
    seq = [("R0: модель VRP-звіту (закриття Binance дня входу, DVOL, рівно 30 д)", {d: r0[d] for d in common}, None)] + [
        (lab, {d: lv[d] for d in common}, lv) for lab, lv in ladder
    ]
    for lab, sub, full_lv in seq:
        b = bootstrap_summary([sub[d]["pnl"] for d in common], block=BLOCK, n_boot=N_BOOT, seed=SEED)
        alld = f"{statistics.mean(t['pnl'] for t in full_lv.values()) * 100:+.2f}" if full_lv else "—"
        L.append(f"| {lab} | {b['mean'] * 100:+.2f} | [{b['ci'][0] * 100:+.2f}; {b['ci'][1] * 100:+.2f}] | {alld} |")
    L.append("\n| Крок | що змінюється | парна різниця фази 0 | 95% CI |\n|---|---|---|---|")
    names = [
        "дата/час входу (09:00 проти 00:00), лістингова експірація замість рівно 30 д, index Deribit і delivery замість Binance",
        "рівень IV входу: ATM mark замість DVOL",
        "вола хеджу: марки замість DVOL",
        "funding перпа-хеджу",
    ]
    for i, nm in enumerate(names):
        a_, b_ = seq[i][1], seq[i + 1][1]
        diffs = [a_[d]["pnl"] - b_[d]["pnl"] for d in common]
        b = bootstrap_summary(diffs, block=BLOCK, n_boot=N_BOOT, seed=SEED)
        L.append(
            f"| {seq[i][0][:2]} → {seq[i + 1][0][:2]} | {nm} | {b['mean'] * 100:+.2f} | [{b['ci'][0] * 100:+.2f}; {b['ci'][1] * 100:+.2f}] |"
        )
    gap = [(t["dvol"] - t["mark_iv"]) for t in p0 if t["dvol"] is not None]
    vg = statistics.mean(t["vega_pct"] for t in p0)
    L.append(
        f"\nДовідка: DVOL − ATM mark на входах фази 0 у середньому {statistics.mean(gap) * 100:+.2f} вп; vega стреддла "
        f"{vg * 100:.3f} % спота на вол-пункт → очікуваний вплив рівня ≈ "
        f"{statistics.mean(gap) * 100 * vg * 100:.2f} % спота (лінійно, без зміни хеджу)."
    )

    # ---------------- robustness grid
    L.append("\n## Сітка варіантів (усе, крім основного рядка, розвідувальне)\n")
    L.append(
        "| Варіант | n фаза 0 | середнє фаза 0 | 95% CI | p | 2024–26 фаза 0 | медіана 30 фаз | усі дні | найгірша (усі дні) |\n|---|---|---|---|---|---|---|---|---|"
    )
    grid: list[tuple[str, dict[int, dict[str, Any]]]] = [
        ("**основна** (nearest [21; 56], mark − 1 вп, funding)", prim),
        ("без витрат (mark, без комісій)", run_variant(days, plans, inp, spread=0.0, zero_fees=True)),
        ("ціни угод зі сторони продавця (sell_iv)", run_variant(days, plans, inp, side=True)),
        (
            "стрес: mark − 1.5 вп, хедж 0.10 %",
            run_variant(days, plans, inp, spread=STRESS_COST["spread"], hedge_fee=STRESS_COST["hedge_fee"]),
        ),
        ("без funding", ladder[2][1]),
        ("без хеджу", run_variant(days, plans, inp, hedge=False)),
    ]
    for lab, rule in ALT_RULES.items():
        grid.append((f"експірація {lab}", run_variant(days, s0["plans"][rule], inp)))
    log("grid books done", t0)
    if a.age3:
        d3, _ = load_snapshots(Path(a.put_skew_cache).expanduser(), "v2_age3")
        pl3 = make_plans(d3, PRIMARY_RULE, inp["delivery"])
        grid.append(("поверхня v2_age3 (марки ≤ 3 год)", run_variant(d3, pl3, inp)))
        del d3
        log("age3 books done", t0)
    for lab, tr in grid:
        ch = phase_chain(tr)
        v = [t["pnl"] for t in ch]
        if len(v) < 2:
            continue
        b = bootstrap_summary(v, block=BLOCK, n_boot=N_BOOT, seed=SEED)
        late = [t["pnl"] for t in ch if t["day"] >= SPLIT_MS]
        pm = [statistics.mean(t["pnl"] for t in c) for k in range(30) if (c := phase_chain(tr, k))]
        L.append(
            f"| {lab} | {b['n']} | {b['mean'] * 100:+.2f} | [{b['ci'][0] * 100:+.2f}; {b['ci'][1] * 100:+.2f}] | {b['p']:.3f} | "
            f"{statistics.mean(late) * 100:+.2f} | {statistics.median(pm) * 100:+.2f} | {statistics.mean(t['pnl'] for t in tr.values()) * 100:+.2f} | "
            f"{min(t['pnl'] for t in tr.values()) * 100:+.2f} |"
        )

    # ---------------- verdict (mechanical)
    L.append("\n## Механічна перевірка критеріїв реєстрації\n")
    L.append(
        f"- H1: середнє {b1['mean'] * 100:+.2f}%, CI [{b1['ci'][0] * 100:+.2f}; {b1['ci'][1] * 100:+.2f}], p = {b1['p']:.3f}, Holm p = {adj[0]:.3f}, найгірша угода (усі дні) {worst_all * 100:+.2f}% → **{verdict1}**."
    )
    L.append(
        f"- H2: середнє {b2['mean'] * 100:+.2f}%, CI [{b2['ci'][0] * 100:+.2f}; {b2['ci'][1] * 100:+.2f}], p = {b2['p']:.3f}, Holm p = {adj[1]:.3f}, "
        f"MDE {s0['mde2'] * 100:.2f}%, найгірша угода у квінтилі (усі дні) {worst_sig * 100:+.2f}% → **{verdict2}**."
    )
    log("report done", t0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True, help="outputs of this study (per-trade JSON)")
    ap.add_argument("--put-skew-cache", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--klines-dir", default="~/.cache/tradedesk-agents/macro_regimes/klines")
    ap.add_argument("--stage0", action="store_true", help="data density and power only, no P&L")
    ap.add_argument("--no-age3", dest="age3", action="store_false", help="skip the v2_age3 surface sensitivity")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.perf_counter()
    Path(a.cache_dir).expanduser().mkdir(parents=True, exist_ok=True)
    ps = Path(a.put_skew_cache).expanduser()
    days, meta = load_snapshots(ps, "v2")
    log(f"surfaces: {len(days)} days", t0)
    inp = load_inputs(ps)
    log(f"inputs: delivery {len(inp['delivery'])}, funding hours {len(inp['funding'])}, DVOL days {len(inp['dvol'])}", t0)
    L = ["# VRP на реальних марках Deribit BTC: сирий вивід `scripts/verify_vrp_real_marks.py`\n"]
    L.append(
        f"Дані: {len(days)} днів поверхонь (кеш put-skew v2, {meta['trades']} угод), delivery {len(inp['delivery'])}, funding-годин "
        f"{len(inp['funding'])}, DVOL-днів {len(inp['dvol'])}. Вхід 09:00 UTC, з {ymd(ENTRY_START_MS)}, експірації до {ymd(LAST_EXPIRY_MS)}.\n"
    )
    s0 = stage0(L, days, inp, t0)
    if not a.stage0:
        full(L, days, inp, s0, a, t0)
    report = "\n".join(L)
    print(report)
    if a.out:
        Path(a.out).expanduser().write_text(report + "\n")
    print(f"\npeak RSS {rss_mb():.0f} MB, {time.perf_counter() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
