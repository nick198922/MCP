"""Weekly ATM straddle VRP on Deribit BTC (IDEA-20261015-weekly-straddle-vrp-term-slope).

Pre-registration: the card's "Попередня реєстрація". No network: everything comes from the put-skew-v2
cache built by ``scripts/verify_put_skew.py`` (``--src-dir``, read only):
  * ``snapshots_v2.json`` (daily 09:00 surfaces from ``trades24/*.json.gz``; rebuilt from the trade files
    if the file count changed), ``delivery_btc_usd.json``, ``funding_btc_perp.json``, ``dvol_BTC.json``.

Stages (each prints a self-contained markdown block; ``--out`` writes it to a file):
    uv run python scripts/verify_weekly_straddle.py --stage checks   # data checks + reproduce put-skew-v2 numbers
    uv run python scripts/verify_weekly_straddle.py --stage gates    # coverage + SD/MDE of H1, no means
    uv run python scripts/verify_weekly_straddle.py --stage full --out ~/.cache/tradedesk-agents/weekly_straddle/full.md
"""

from __future__ import annotations

import argparse
import logging
import math
import statistics
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_put_skew as vps  # noqa: E402  (scripts/ is not a package)

from tradedesk.analytics.carry import pearson  # noqa: E402
from tradedesk.analytics.put_skew import (  # noqa: E402
    FundingIndex,
    block_bootstrap_ci,
    chain,
    choose_expiry,
    dvol_known_at_entry,
    inverse_price,
    plan_entry,
    run_book,
    trailing_percentile,
)
from tradedesk.analytics.vrp import stats  # noqa: E402
from tradedesk.analytics.weekly_straddle import (  # noqa: E402
    DAY_MS,
    HOUR_MS,
    atm_plan,
    book_path,
    deribit_im_estimate,
    friday_expiry,
    fridays,
    mde,
    newey_west_mean,
    permutation_p,
    pre_entry_slope,
    realized_vol_path,
    threshold_spread,
)

PCT = 100.0
FIRST = int(datetime(2021, 1, 1, tzinfo=UTC).timestamp() * 1000)
LAST = int(datetime(2026, 10, 2, tzinfo=UTC).timestamp() * 1000)
LAST_EXPIRY_MS = vps.LAST_EXPIRY_MS
SPLIT_MS = vps.SPLIT_MS
THRESHOLD = 0.0025  # H1 effect size and MDE gate, fraction of spot
COVERAGE_MIN = 0.80
SLOPE_THR = 1.03
SEED = 20261015

COSTS: dict[str, dict[str, Any]] = {
    "base": {"hs": 0.015, "option_fee": 0.0003, "delivery_fee": 0.00015, "hedge_fee": 0.0005, "side": False},
    "zero": {"hs": 0.0, "option_fee": 0.0, "delivery_fee": 0.0, "hedge_fee": 0.0, "side": False},
    "stress": {"hs": 0.03, "option_fee": 0.0003, "delivery_fee": 0.00015, "hedge_fee": 0.001, "side": False},
    "trade_side": {"hs": 0.0, "option_fee": 0.0003, "delivery_fee": 0.00015, "hedge_fee": 0.0005, "side": True},
}
NAMED = (
    ("2021-05-19", "2021-05-19"),
    ("2022-06-13", "2022-06-13"),
    ("2022-11-08", "2022-11-08"),
    ("2024-08-05", "2024-08-05"),
    ("2025-10-10", "2025-10-10"),
)


def ymd(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


def ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def ci_s(ci: tuple[float, float] | None, k: float = PCT) -> str:
    return f"[{ci[0] * k:+.2f}; {ci[1] * k:+.2f}]" if ci else "—"


def pctl(v: Sequence[float], q: float) -> float:
    s = sorted(v)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))]


# ----------------------------------------------------------------------------- data


def load(src: Path) -> dict[str, Any]:
    t0 = time.perf_counter()
    days, meta = vps.load_snapshots(src, "v2")
    import json

    delivery = {r["date"]: float(r["delivery_price"]) for r in json.loads((src / "delivery_btc_usd.json").read_text())}
    funding = [(int(t), float(v)) for t, v in json.loads((src / "funding_btc_perp.json").read_text())]
    dvol = [(int(t), float(v)) for t, v in json.loads((src / "dvol_BTC.json").read_text())]
    print(
        f"loaded {len(days)} surfaces, {len(delivery)} delivery prices, {len(funding)} funding hours in {time.perf_counter() - t0:.0f}s",
        flush=True,
    )
    return {"days": days, "meta": meta, "delivery": delivery, "funding": funding, "fidx": FundingIndex(funding), "dvol": dvol}


def expiry_for(snap: dict[str, Any], d: int, tenor: str) -> int | None:
    if tenor == "7d":
        return friday_expiry(d, 1)
    if tenor == "14d":
        return friday_expiry(d, 2)
    return choose_expiry(snap, 30, 21, 56, rule="calendar_monthly")


def plan_week(D: dict[str, Any], d: int, tenor: str) -> tuple[dict[str, Any] | None, str]:
    """(plan, reason): reason is '' when covered."""
    snap = D["days"].get(d)
    if snap is None:
        return None, "немає поверхні дня"
    e = expiry_for(snap, d, tenor)
    if e is None or e not in snap["expiries"]:
        return None, "цільової експірації немає в угодах за 24 год"
    if e > LAST_EXPIRY_MS:
        return None, "експірація після кінця кешу"
    p = atm_plan(snap, e)
    if p is None:
        return None, "немає страйків зі свіжою маркою"
    if not p["covered"]:
        return p, "ATM-страйк не в call-Δ [0.40; 0.60]"
    if ymd(e) not in D["delivery"]:
        return p, "немає delivery price"
    return p, ""


def make_trade(
    D: dict[str, Any], p: dict[str, Any], cost: dict[str, Any], *, fund: bool = True, hedge: bool = True
) -> dict[str, Any] | None:
    if cost["side"]:
        if p["sell_iv"] is None:
            return None
        iv = p["sell_iv"]
    else:
        iv = p["mark_iv"] - cost["hs"]
    if iv <= 0.01:
        return None
    K, e = p["K"], p["expiry"]
    legs = [(K, True, -1.0, iv), (K, False, -1.0, iv)]
    r = book_path(
        D["days"],
        p["day"],
        e,
        legs,
        D["delivery"][ymd(e)],
        funding=D["fidx"] if fund else (),
        option_fee=cost["option_fee"],
        delivery_fee=cost["delivery_fee"],
        hedge_fee=cost["hedge_fee"],
        hedge=hedge,
    )
    if r is None:
        return None
    mark_btc = [inverse_price(p["F"], K, p["T"], p["mark_iv"], c) for c in (True, False)]
    r["im"] = sum(deribit_im_estimate(K, p["spot"], c, m) for c, m in zip((True, False), mark_btc, strict=True))
    r["iv"] = p["mark_iv"]
    r["K"] = K
    r["dte"] = p["T"] * 365
    return r


def build(D: dict[str, Any], tenor: str, cost_key: str, *, fund: bool = True, hedge: bool = True) -> dict[int, dict[str, Any]]:
    out = {}
    for d in fridays(FIRST, LAST):
        p, why = plan_week(D, d, tenor)
        if why or p is None:
            continue
        r = make_trade(D, p, COSTS[cost_key], fund=fund, hedge=hedge)
        if r is not None:
            out[d] = r
    return out


# ----------------------------------------------------------------------------- stages


def coverage_block(D: dict[str, Any], L: list[str]) -> float:
    fr = fridays(FIRST, LAST)
    L.append("## Покриття входів (поріг ≥ 80 % п'ятниць; зафіксовано до P&L)\n")
    L.append("| Тенор | п'ятниць | покрито | частка | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |\n|---|---|---|---|---|---|---|---|---|---|")
    share7 = 0.0
    reasons7: dict[str, int] = {}
    near7: list[int] = []
    ages7: list[float] = []
    for tenor in ("7d", "14d", "monthly"):
        ok = []
        for d in fr:
            p, why = plan_week(D, d, tenor)
            if not why:
                ok.append(d)
            if tenor == "7d":
                reasons7[why or "покрито"] = reasons7.get(why or "покрито", 0) + 1
                if p is not None and not why:
                    near7.append(p["prints_near"])
                    if p["mark_age_h"] is not None:
                        ages7.append(p["mark_age_h"])
        sok = set(ok)
        by_y = []
        for y in range(2021, 2027):
            ys = [d for d in fr if ymd(d)[:4] == str(y)]
            by_y.append(f"{sum(1 for d in ys if d in sok) / len(ys):.0%}")
        share = len(ok) / len(fr)
        if tenor == "7d":
            share7 = share
        L.append(f"| {tenor} | {len(fr)} | {len(ok)} | {share:.1%} | " + " | ".join(by_y) + " |")
    L.append(f"\nГоловна комірка (7d): покриття {share7:.1%} → поріг {'виконано' if share7 >= COVERAGE_MIN else 'НЕ виконано'}.\n")
    L.append("Причини (7d): " + "; ".join(f"{k}: {v}" for k, v in sorted(reasons7.items(), key=lambda kv: -kv[1])) + ".")
    if near7:
        L.append(
            f"Інформативно: угод за 24 год у ATM ± 2 страйки — медіана {statistics.median(near7):.0f}; тижнів з ≥ 5: "
            f"{sum(1 for x in near7 if x >= 5) / len(fr):.0%} від усіх п'ятниць. Вік mark ATM, год: медіана {statistics.median(ages7):.2f}, "
            f"p90 {pctl(ages7, 0.9):.2f}, max {max(ages7):.2f}."
        )
    return share7


def checks(D: dict[str, Any], L: list[str]) -> None:
    days = D["days"]
    L.append("## Перевірки даних (без P&L тижневих угод)\n")
    meta = D["meta"]
    L.append(
        f"- Поверхонь {len(days)}, угод {meta['trades']}, порожніх днів {len(meta['empty_days'])}; delivery prices {len(D['delivery'])}; "
        f"funding-годин {len(D['funding'])}; DVOL-днів {len(D['dvol'])}. Мережа не використовувалась."
    )
    fr = fridays(FIRST, LAST)
    dd = [abs(days[d]["spot"] / D["delivery"][ymd(d)] - 1) for d in fr if d in days and ymd(d) in D["delivery"]]
    L.append(
        f"- Spot входу (index, 15 хв до 09:00) проти delivery price 08:00 того ж дня: медіана |відхилення| {statistics.median(dd) * PCT:.2f} %, p95 {pctl(dd, 0.95) * PCT:.2f} %, n = {len(dd)}."
    )
    dv = dvol_known_at_entry(D["dvol"])
    pairs = []
    for d in fr:
        p, why = plan_week(D, d, "7d")
        if not why and p is not None and d in dv:
            pairs.append((p["mark_iv"], dv[d]))
    if pairs:
        L.append(
            f"- ATM mark IV 7д (09:00) проти DVOL(d−1): середнє {statistics.mean(x for x, _ in pairs) * PCT:.1f} % проти "
            f"{statistics.mean(y for _, y in pairs) * PCT:.1f} %, кореляція {pearson(pairs):.3f}, n = {len(pairs)}."
        )
    fu = [v for _, v in D["funding"]]
    L.append(
        f"- Funding Deribit BTC-PERPETUAL (`interest_1h`, десяткова частка за годину): середнє {statistics.mean(fu) * 24 * 365 * PCT:.1f} % річних, "
        f"p1/p99 за годину {pctl(fu, 0.01) * PCT:.4f} / {pctl(fu, 0.99) * PCT:.4f} %."
    )
    gaps = [d for d in range(FIRST, LAST + 7 * DAY_MS, DAY_MS) if d not in days]
    L.append(
        f"- Днів без поверхні у 2021-01-01…2026-10-08: {len(gaps)}" + (f" ({', '.join(ymd(g) for g in gaps[:10])})" if gaps else "") + "."
    )
    # reproduce put-skew-v2 numbers with its own code path (known numbers: 30d straddle phase 0 +0.06 %, n = 66; 10d +0.39 %, n = 144)
    for tenor, (tgt, band, rule), known in (
        ("30d", (30, (21, 56), "calendar_monthly"), "+0.06 % [−0.83; +0.95], n = 66"),
        ("10d", (10, (7, 14), "nearest"), "+0.39 % [−0.14; +0.93], n = 144"),
    ):
        res = {}
        for d, snap in days.items():
            if d < vps.ENTRY_START_MS:
                continue
            p = plan_entry(snap, target_days=tgt, dte_band=band, target_delta=0.25, delta_band=(0.20, 0.30), expiry_rule=rule)
            if p is None or not p["covered"] or p["expiry"] > LAST_EXPIRY_MS:
                continue
            dp = D["delivery"].get(ymd(p["expiry"]))
            if dp is None:
                continue
            books = vps.make_books(snap, p, vps.COSTS["base"])
            if books["straddle"] is None:
                continue
            r = run_book(
                days, d, p["expiry"], books["straddle"], dp, funding=D["fidx"], option_fee=0.0003, delivery_fee=0.00015, hedge_fee=0.0005
            )
            if r is not None:
                res[d] = r
        ph0 = [res[d]["pnl"] for d in chain(res, vps.ENTRY_START_MS, max(res))]
        s = stats(ph0)
        L.append(
            f"- Відтворення put-skew-v2, стреддл {tenor}, base, funding, фаза 0: {s['mean'] * PCT:+.2f} % {ci_s(s['ci95'])}, n = {s['n']} "
            f"(у звіті put-skew-v2: {known})."
        )


def gates(D: dict[str, Any], L: list[str]) -> dict[str, Any]:
    share = coverage_block(D, L)
    prim = build(D, "7d", "base")
    vals = [t["pnl"] for t in prim.values()]
    n = len(vals)
    sd = statistics.stdev(vals) if n > 1 else float("nan")
    m = mde(sd, n)
    passed = share >= COVERAGE_MIN and m <= THRESHOLD
    L.append("\n## Ворота потужності H1 (друкуються до середніх)\n")
    L.append(f"- Тижнів з угодою (base, funding): n = {n}; SD P&L = {sd * PCT:.2f} % спота; SE = {sd / math.sqrt(n) * PCT:.3f} %.")
    L.append(f"- MDE = 2.8·SD/√n = **{m * PCT:.2f} %** (поріг {THRESHOLD * PCT:.2f} %).")
    L.append("- Для порівняння: критик очікував SD 2.5–3 %, MDE ≈ 0.4–0.5 %; картка — SD 1.2–1.6 %.")
    L.append(f"- Ворота: **{'пройдено' if passed else 'НЕ пройдено → статус inconclusive; середні лише описово'}**.")
    return {"share": share, "n": n, "sd": sd, "mde": m, "passed": passed, "prim": prim}


def summary_row(label: str, tr: dict[int, dict[str, Any]], key: str = "pnl", lag: int = 1) -> str:
    v = [tr[d][key] for d in sorted(tr)]
    if len(v) < 2:
        return f"| {label} | {len(v)} | — | — | — | — | — | — |"
    nw = newey_west_mean(v, lag)
    early = [tr[d][key] for d in tr if d < SPLIT_MS]
    late = [tr[d][key] for d in tr if d >= SPLIT_MS]
    top = sorted(v, reverse=True)[10:]
    return (
        f"| {label} | {len(v)} | {nw['mean'] * PCT:+.2f} | {ci_s(nw['ci95'])} | {statistics.stdev(v) * PCT:.2f} | "
        f"{(statistics.mean(early) * PCT if early else float('nan')):+.2f} | {(statistics.mean(late) * PCT if late else float('nan')):+.2f} | "
        f"{statistics.mean(top) * PCT:+.2f} |"
    )


GRID_HDR = (
    "| Варіант | n | середнє, % спота | 95 % CI (NW) | SD | 2021–2023 | 2024–2026 (точкова) | без 10 найкращих |\n"
    "|---|---|---|---|---|---|---|---|"
)


def full(D: dict[str, Any], G: dict[str, Any], L: list[str]) -> None:
    prim = G["prim"]
    keys = sorted(prim)
    vals = [prim[d]["pnl"] for d in keys]
    L.append("\n## H1: головна комірка (7d, base, funding, хедж раз на добу)\n")
    if not G["passed"]:
        L.append("**Ворота не пройдено: усі числа нижче описові, висновків про прибуток з них не робити.**\n")
    nw = newey_west_mean(vals, 1)
    bb = block_bootstrap_ci([(v,) for v in vals], lambda rows: statistics.mean(r[0] for r in rows), block=4, n_boot=10_000, seed=SEED)
    s = stats(vals, periods_per_year=52)
    L.append(
        f"- n = {s['n']}; середнє {s['mean'] * PCT:+.3f} % спота; NW(1) 95 % CI {ci_s(nw['ci95'])}; блоковий бутстреп (4 тижні) {ci_s(bb)}; "
        f"медіана {s['median'] * PCT:+.2f}; частка > 0 {s['share_positive']:.0%}; найгірше {s['min'] * PCT:+.2f}; найкраще {s['max'] * PCT:+.2f}; "
        f"макс. просадка суми {s['max_drawdown'] * PCT:.1f} % спота; Sharpe (річний, на номінал) {s.get('sharpe', float('nan')):.2f}."
    )
    late = [prim[d]["pnl"] for d in keys if d >= SPLIT_MS]
    early = [prim[d]["pnl"] for d in keys if d < SPLIT_MS]
    top = sorted(vals, reverse=True)[10:]
    L.append(
        f"- 2021–2023: {statistics.mean(early) * PCT:+.2f} % (n = {len(early)}); 2024–2026 (лише знак і точкова): {statistics.mean(late) * PCT:+.2f} % (n = {len(late)})."
    )
    L.append(
        f"- Без 10 найкращих тижнів: {statistics.mean(top) * PCT:+.2f} %; без 10 найгірших: {statistics.mean(sorted(vals)[10:]) * PCT:+.2f} %."
    )
    L.append(
        "- По роках: "
        + "; ".join(
            f"{y} {statistics.mean(v) * PCT:+.2f} (n={len(v)})"
            for y in range(2021, 2027)
            if (v := [prim[d]["pnl"] for d in keys if ymd(d)[:4] == str(y)])
        )
        + "."
    )
    comp = {k: statistics.mean(prim[d][k] for d in keys) for k in ("premium", "payoff", "hedge_pnl", "funding_pnl", "costs", "entry")}
    L.append(
        f"- Складові (середні, % спота): премія {comp['premium'] * PCT:+.2f}, виплата {comp['payoff'] * PCT:+.2f}, хедж {comp['hedge_pnl'] * PCT:+.2f}, "
        f"funding {comp['funding_pnl'] * PCT:+.3f}, витрати −{comp['costs'] * PCT:.3f} (з них вхід: спред + комісії опціонів + перший хедж {comp['entry'] * PCT:+.3f})."
    )
    stale = sum(prim[d]["stale_leg_days"] for d in keys) / max(1, sum(2 * prim[d]["hedge_days"] for d in keys))
    hd = [prim[d]["hedge_days"] for d in keys]
    L.append(
        f"- Точок хеджу на угоду: медіана {statistics.median(hd):.0f}, мінімум {min(hd)}; частка нога-точок без марки експірації (стара IV) {stale:.1%}. DTE входу: медіана {statistics.median(prim[d]['dte'] for d in keys):.2f}."
    )
    ivrv = [(prim[d]["iv"], realized_vol_path(prim[d]["spots"])) for d in keys]
    ivrv = [(a, b) for a, b in ivrv if b is not None]
    L.append(
        f"- Нульова модель: ATM mark IV входу {statistics.mean(a for a, _ in ivrv) * PCT:.1f} % проти реалізованої воли тижня (index 09:00 + delivery) "
        f"{statistics.mean(b for _, b in ivrv) * PCT:.1f} %; середнє IV − RV {statistics.mean(a - b for a, b in ivrv) * PCT:+.1f} вп, частка тижнів IV > RV "
        f"{sum(1 for a, b in ivrv if a > b) / len(ivrv):.0%}; IV² − RV² {statistics.mean(a * a - b * b for a, b in ivrv) * PCT:+.2f} (×100)."
    )

    # --- grid
    L.append("\n## Сітка чутливості (усе розвідувальне, крім першого рядка; місячний — перекривні угоди, CI з NW лаг 8)\n")
    L.append(GRID_HDR)
    L.append(summary_row("**7d, base, funding, хедж (головна)**", prim))
    variants: list[tuple[str, str, str, bool, bool]] = [
        ("7d, zero (нульова модель)", "7d", "zero", True, True),
        ("7d, stress (−3 вп, хедж 0.10 %)", "7d", "stress", True, True),
        ("7d, trade_side (IV угод taker-sell)", "7d", "trade_side", True, True),
        ("7d, base, без funding", "7d", "base", False, True),
        ("7d, base, без хеджу", "7d", "base", True, False),
        ("14d, base", "14d", "base", True, True),
        ("14d, zero", "14d", "zero", True, True),
        ("14d, stress", "14d", "stress", True, True),
        ("місячний, base", "monthly", "base", True, True),
        ("місячний, zero", "monthly", "zero", True, True),
        ("місячний, stress", "monthly", "stress", True, True),
    ]
    built: dict[str, dict[int, dict[str, Any]]] = {}
    for lab, tenor, ck, fund, hedge in variants:
        tr = build(D, tenor, ck, fund=fund, hedge=hedge)
        built[lab] = tr
        L.append(summary_row(lab, tr, lag=8 if tenor == "monthly" else 1))
    L.append("\n14d: вхід щоп'ятниці, угоди перекриваються на 1 тиждень (CI NW лаг 1 занижений; лише довідково).")

    # --- H2
    L.append("\n## H2 (розвідувально): нахил IV(цільова)/IV(30д) зі знімка четверга 09:00, поріг 1.03, контроль DVOL(d−1)\n")
    dv = dvol_known_at_entry(D["dvol"])
    dv_pct = trailing_percentile(sorted(dv.items()), window_ms=365 * DAY_MS, min_obs=180)
    rows = []
    for d in keys:
        if d not in dv_pct:
            continue
        sl = pre_entry_slope(D["days"], d, prim[d]["expiry"])
        if sl is not None:
            rows.append({"day": d, "pnl": prim[d]["pnl"], "slope": sl, "dvol_pct": dv_pct[d]})
    if rows:
        sls = [r["slope"] for r in rows]
        L.append(
            f"Тижнів з нахилом і DVOL: {len(rows)}; нахил медіана {statistics.median(sls):.3f}, p10/p90 {pctl(sls, 0.1):.3f}/{pctl(sls, 0.9):.3f}; частка > 1.03: {sum(1 for x in sls if x > SLOPE_THR) / len(sls):.0%}.\n"
        )
        L.append("| DVOL-половина | s > 1.03: середнє (n) | s ≤ 1.03: середнє (n) | різниця |\n|---|---|---|---|")
        for lab, cond in (("< 0.5", lambda c: c < 0.5), ("≥ 0.5", lambda c: c >= 0.5)):
            part = [r for r in rows if cond(r["dvol_pct"])]
            hi = [r["pnl"] for r in part if r["slope"] > SLOPE_THR]
            lo = [r["pnl"] for r in part if r["slope"] <= SLOPE_THR]
            if hi and lo:
                L.append(
                    f"| {lab} | {statistics.mean(hi) * PCT:+.2f} ({len(hi)}) | {statistics.mean(lo) * PCT:+.2f} ({len(lo)}) | {(statistics.mean(hi) - statistics.mean(lo)) * PCT:+.2f} |"
                )
            else:
                L.append(f"| {lab} | — ({len(hi)}) | — ({len(lo)}) | — |")
        est = threshold_spread(rows, thr=SLOPE_THR)
        if est is None:
            L.append("\nH2: клітинка < 10 тижнів → ◯.")
        else:
            ci = block_bootstrap_ci(rows, lambda rs: threshold_spread(rs, thr=SLOPE_THR, min_n=3), block=4, n_boot=5000, seed=SEED)
            p = permutation_p(rows, thr=SLOPE_THR, n_perm=5000, seed=SEED)
            L.append(
                f"\nH2 статистика: {est * PCT:+.2f} п.п., бутстреп 95 % CI {ci_s(ci)}, перестановочне p = {p:.3f} (ціль ≥ +0.40 п.п.)."
            )
        corr = pearson([(r["slope"], r["pnl"]) for r in rows])
        L.append(f"Довідково: кореляція нахилу з P&L {corr:+.3f}.")

    # --- H3
    L.append("\n## H3 (розвідувально): P&L на одиницю премії, тиждень − місяць, парно за п'ятницями\n")
    mon = built["місячний, base"]
    pairs = [
        (d, prim[d]["pnl"] / prim[d]["premium"], mon[d]["pnl"] / mon[d]["premium"])
        for d in keys
        if d in mon and prim[d]["premium"] > 0 and mon[d]["premium"] > 0
    ]
    if pairs:
        diff = newey_west_mean([a - b for _, a, b in pairs], 8)
        L.append(
            f"n = {len(pairs)} п'ятниць; тиждень {statistics.mean(a for _, a, _ in pairs):+.3f}, місяць {statistics.mean(b for _, _, b in pairs):+.3f} (частки премії); "
            f"різниця {diff['mean']:+.3f}, NW(8) 95 % CI {ci_s(diff['ci95'], 1.0)}. Середня премія: тиждень {statistics.mean(prim[d]['premium'] for d, _, _ in pairs) * PCT:.2f} %, "
            f"місяць {statistics.mean(mon[d]['premium'] for d, _, _ in pairs) * PCT:.2f} % спота."
        )

    # --- weekend decomposition
    L.append("\n## Внесок вікна вихідних (описово; добові точки переоцінки за mark IV)\n")
    parts: dict[str, list[float]] = {
        "вхід (спред, комісії, перший хедж)": [],
        "пт 09 → нд 09 (вихідні)": [],
        "нд 09 → чт 09": [],
        "чт 09 → пт 08 (розрахунок)": [],
    }
    for d in keys:
        t = prim[d]
        t_entry = d + 9 * HOUR_MS
        w = sum(x[2] for x in t["intervals"] if x[1] <= t_entry + 2 * DAY_MS)
        last = t["intervals"][-1][2] if t["intervals"] and t["intervals"][-1][1] == t["expiry"] else 0.0
        mid = sum(x[2] for x in t["intervals"]) - w - last
        parts["вхід (спред, комісії, перший хедж)"].append(t["entry"])
        parts["пт 09 → нд 09 (вихідні)"].append(w)
        parts["нд 09 → чт 09"].append(mid)
        parts["чт 09 → пт 08 (розрахунок)"].append(last)
    L.append("| Частина | середнє, % спота | 95 % CI (NW 1) | частка > 0 |\n|---|---|---|---|")
    for k, v in parts.items():
        nwk = newey_west_mean(v, 1)
        L.append(f"| {k} | {nwk['mean'] * PCT:+.3f} | {ci_s(nwk['ci95'])} | {sum(1 for x in v if x > 0) / len(v):.0%} |")
    ex_w = [t - w for t, w in zip(vals, parts["пт 09 → нд 09 (вихідні)"], strict=True)]
    L.append(
        f"\nH1 без внеску вихідних (тиждень мінус пт 09 → нд 09): {statistics.mean(ex_w) * PCT:+.2f} % {ci_s(newey_west_mean(ex_w, 1)['ci95'])}."
    )

    # --- tails and capital
    L.append("\n## Хвіст і капітал\n")
    L.append("10 найгірших тижнів (головна комірка):\n")
    L.append(
        "| Вхід | рух S_T/S0 | P&L USD-лін., % спота | P&L у BTC (÷S_T), % 1 BTC | мін. MTM на точках 09:00 | на оцінену IM |\n|---|---|---|---|---|---|"
    )
    for d in sorted(keys, key=lambda x: prim[x]["pnl"])[:10]:
        t = prim[d]
        L.append(
            f"| {ymd(d)} | {t['move'] * PCT:+.1f} % | {t['pnl'] * PCT:+.2f} | {t['pnl_btc_at_expiry'] * PCT:+.2f} | {t['min_mtm'] * PCT:+.2f} | {t['pnl'] / t['im'] * PCT:+.1f} % |"
        )
    L.append("\nНазвані стрес-дати (усі тижні, що перетинають добу дати; п'ятниця дає два тижні):\n")
    L.append("| Дата | тиждень входу | рух | P&L USD-лін. | у BTC | мін. MTM | на IM |\n|---|---|---|---|---|---|---|")
    for a, _ in NAMED:
        t_ms = ms(a)
        hit = [d for d in keys if d + 9 * HOUR_MS < t_ms + DAY_MS and t_ms < prim[d]["expiry"]]
        if not hit:
            L.append(f"| {a} | — (тиждень не покрито) | | | | | |")
            continue
        for h in hit:
            t = prim[h]
            L.append(
                f"| {a} | {ymd(h)} | {t['move'] * PCT:+.1f} % | {t['pnl'] * PCT:+.2f} | {t['pnl_btc_at_expiry'] * PCT:+.2f} | {t['min_mtm'] * PCT:+.2f} | {t['pnl'] / t['im'] * PCT:+.1f} % |"
            )
    btc = [prim[d]["pnl_btc_at_expiry"] for d in keys]
    im = [prim[d]["im"] for d in keys]
    rom = [prim[d]["pnl"] / prim[d]["im"] for d in keys]
    mtm = [prim[d]["min_mtm"] for d in keys]
    L.append(
        f"\n- USD-лінійно: найгірше {min(vals) * PCT:+.2f} %, p1 {pctl(vals, 0.01) * PCT:+.2f} %; у BTC (÷S_T): середнє {statistics.mean(btc) * PCT:+.2f} %, найгірше {min(btc) * PCT:+.2f} %, p1 {pctl(btc, 0.01) * PCT:+.2f} %."
    )
    L.append(
        f"- Оцінена IM (оцінка критика, не специфікація Deribit): медіана {statistics.median(im) * PCT:.1f} % спота на 1 BTC стреддла. Дохід на IM за тиждень: "
        f"середнє {statistics.mean(rom) * PCT:+.2f} %, найгірше {min(rom) * PCT:+.1f} %. Найгірша MTM на добових точках: {min(mtm) * PCT:+.2f} % спота "
        f"({min(m / i for m, i in zip(mtm, im, strict=True)) * PCT:+.0f} % IM); тижнів з MTM < −15 % спота (≈ запас IM − MM): {sum(1 for m in mtm if m < -0.15)}; < −10 %: {sum(1 for m in mtm if m < -0.10)}."
    )
    L.append("- Добові точки 09:00 занижують внутрішньоденний мінімум; маржа хедж-перпа і портфельна маржа не моделювались.")

    # --- mechanical verdict
    L.append("\n## Механічна перевірка критеріїв реєстрації\n")
    ci = nw["ci95"]
    c_gate = G["passed"]
    c_hit = s["mean"] >= THRESHOLD and ci[0] > 0
    c_rej = ci[1] < THRESHOLD
    k_a = statistics.mean(late) <= 0
    k_b = statistics.mean(top) <= 0
    k_d = s["min"] < -0.08 and s["mean"] < 0.005
    L.append(
        f"- Ворота (покриття ≥ 80 % і MDE ≤ 0.25 %): {'так' if c_gate else 'ні'} (покриття {G['share']:.1%}, MDE {G['mde'] * PCT:.2f} %)."
    )
    L.append(
        f"- Успіх (середнє ≥ 0.25 % і нижня межа NW CI > 0): {'так' if c_hit else 'ні'}; бутстреп нижня межа > 0: {'так' if bb and bb[0] > 0 else 'ні'}."
    )
    L.append(f"- ✗ за CI (верхня межа NW CI < 0.25 %): {'так' if c_rej else 'ні'}.")
    L.append(
        f"- «Вбити» (а) 2024–2026 точкова ≤ 0: {'так' if k_a else 'ні'}; (б) без 10 найкращих ≤ 0: {'так' if k_b else 'ні'}; (д) найгірший < −8 % при середньому < 0.5 %: {'так' if k_d else 'ні'}."
    )
    if not c_gate:
        status = "inconclusive (ворота не пройдено; маркери вище — для критика)"
    elif c_rej or k_a or k_b or k_d:
        status = "rejected"
    elif c_hit:
        status = "історично ✓ → потрібен форвард (статус лишається testing)"
    else:
        status = "inconclusive"
    L.append(f"- Статус за реєстрацією: **{status}**.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/weekly_straddle")
    ap.add_argument("--stage", choices=("checks", "gates", "full"), required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    Path(a.cache_dir).expanduser().mkdir(parents=True, exist_ok=True)
    D = load(Path(a.src_dir).expanduser())
    L = [f"# Тижневий ATM-стреддл Deribit BTC: сирий вивід `scripts/verify_weekly_straddle.py --stage {a.stage}`\n"]
    if a.stage == "checks":
        checks(D, L)
    else:
        G = gates(D, L)
        if a.stage == "full":
            full(D, G, L)
    report = "\n".join(L)
    print(report)
    if a.out:
        Path(a.out).expanduser().write_text(report + "\n")


if __name__ == "__main__":
    main()
