"""Event-day straddle premium on Deribit BTC dailies (IDEA-20261014-event-day-straddle-premium).

The pre-registration is in the idea card ("Попередня реєстрація"). Data are read from caches only:
  * BTC option prints, 24 h before 09:00 UTC per day: ``~/.cache/tradedesk-agents/put_skew/trades24``
    (built by ``scripts/verify_put_skew.py --scheme v2 --fetch-only`` through ``rt.deribit_history``);
  * Deribit delivery prices and DVOL: ``~/.cache/tradedesk-agents/put_skew/{delivery_btc_usd,dvol_BTC}.json``
    (same script, ``rt.deribit``);
  * the US macro events table ``docs/verification/2026-10-15-us-macro-events.csv`` and the seed
    ``src/tradedesk/data/events_seed.json`` (for blocking control days only).
No network access is needed; the per-day quotes are cached in ``--cache-dir`` (default
``~/.cache/tradedesk-agents/event_straddle``).

Usage:
    uv run python scripts/verify_event_straddle.py --gates-only      # coverage + sanity + power gate, no means
    uv run python scripts/verify_event_straddle.py --out report.md   # full pre-registered run
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import statistics
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from tradedesk.analytics.carry import pearson
from tradedesk.analytics.event_straddle import (
    DAY_MS,
    HOUR_MS,
    blocked_days,
    cluster_bootstrap,
    cost_frac,
    drop_top,
    entry_quote,
    group_events,
    iso_week,
    mae_short,
    pick_controls,
    power_gate,
    straddle_x,
    straddle_x_inverse,
)
from tradedesk.analytics.put_skew import dvol_known_at_entry

ROOT = Path(__file__).resolve().parents[1]
EVENTS_CSV = ROOT / "docs/verification/2026-10-15-us-macro-events.csv"
PRIMARY_STATUS = ("actual", "scheduled_2026")
START = datetime(2021, 1, 1, tzinfo=UTC)
END = datetime(2026, 10, 13, tzinfo=UTC)  # last entry day: its D+1 delivery (2026-10-14) is cached
SPLIT = datetime(2024, 1, 1, tzinfo=UTC)
QUOTES_VERSION = "v1"
PCT = 100.0


def ms(d: datetime | date) -> int:
    if not isinstance(d, datetime):
        d = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return int(d.timestamp() * 1000)


def ymd(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


START_MS, END_MS, SPLIT_MS = ms(START), ms(END), ms(SPLIT)


# ----------------------------------------------------------------------------- per-day quotes (cached)


def _process(path: str) -> tuple[int, dict[str, Any] | None, dict[str, Any]]:
    """Entry quote of day D, plus index stats of file D's window for the entry D-1 holding period and delivery check."""
    p = Path(path)
    day = ms(datetime.strptime(p.name[:10], "%Y-%m-%d").replace(tzinfo=UTC))
    trades = json.loads(gzip.decompress(p.read_bytes()))["trades"]
    q = entry_quote(trades, day)
    # holding window of the entry on D-1: (D-1 09:00, D 08:00]; delivery TWAP window: [D 07:30, D 08:00]
    h0, h1 = day - DAY_MS + 9 * HOUR_MS, day + 8 * HOUR_MS
    lo = hi = None
    twap = []
    for t in trades:
        ts, idx = t.get("timestamp"), t.get("index_price")
        if ts is None or not idx:
            continue
        ts, idx = int(ts), float(idx)
        if h0 < ts <= h1:
            if lo is None or idx < lo[1]:
                lo = (ts, idx)
            if hi is None or idx > hi[1]:
                hi = (ts, idx)
        if h1 - 30 * 60_000 <= ts <= h1:
            twap.append(idx)
    extra = {"path_prev": [lo, hi] if lo else [], "twap_0800": statistics.mean(twap) if twap else None, "n_trades": len(trades)}
    return day, q, extra


def load_quotes(trades_dir: Path, cache: Path) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    files = sorted(
        str(p) for p in trades_dir.glob("*.json.gz") if START_MS - DAY_MS <= ms(date.fromisoformat(p.name[:10])) <= END_MS + DAY_MS
    )
    cp = cache / f"quotes_{QUOTES_VERSION}.json"
    if cp.exists():
        blob = json.loads(cp.read_text())
        if blob.get("n_files") == len(files):
            return {int(k): v for k, v in blob["quotes"].items() if v}, {int(k): v for k, v in blob["extra"].items()}
    print(f"quotes: processing {len(files)} day files", flush=True)
    t0 = time.perf_counter()
    quotes: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    workers = max(1, (os.cpu_count() or 2) - 2)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, (day, q, x) in enumerate(ex.map(_process, files, chunksize=8)):
            quotes[str(day)] = q
            extra[str(day)] = x
            if i % 250 == 0 or i == len(files) - 1:
                print(f"  {i + 1}/{len(files)} in {time.perf_counter() - t0:.0f}s", flush=True)
    cp.write_text(json.dumps({"n_files": len(files), "quotes": quotes, "extra": extra}))
    return {int(k): v for k, v in quotes.items() if v}, {int(k): v for k, v in extra.items()}


# ----------------------------------------------------------------------------- events


def load_events(path: Path) -> tuple[dict[int, str], set[int], list[dict[str, str]]]:
    rows = list(csv.DictReader(path.open()))
    prim = [(ms(date.fromisoformat(r["date"])), r["kind"]) for r in rows if r["status"] in PRIMARY_STATUS]
    prim = [(d, k) for d, k in prim if START_MS <= d <= END_MS]
    block_dates = [ms(date.fromisoformat(r["date"])) for r in rows if r["status"] != "cancelled"]
    with resources.files("tradedesk.data").joinpath("events_seed.json").open("r", encoding="utf-8") as f:
        seed = json.load(f)["events"]
    block_dates += [int(e["ts"]) * 1000 for e in seed]
    return group_events(prim), blocked_days(block_dates), rows


# ----------------------------------------------------------------------------- formatting


def f2(x: float | None, k: float = PCT, sign: bool = True) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x * k:+.2f}" if sign else f"{x * k:.2f}"


def ci_s(ci: tuple[float, float] | None) -> str:
    return f"[{ci[0] * PCT:+.2f}; {ci[1] * PCT:+.2f}]" if ci else "—"


def row(label: str, rows: list[tuple[str, float]]) -> str:
    if not rows:
        return f"| {label} | 0 | — | — | — | — | — |"
    b = cluster_bootstrap(rows)
    v = [x for _, x in rows]
    return (
        f"| {label} | {b['n']} | {f2(b['mean'])} | {ci_s(b['ci95'])} | {f2(statistics.median(v))} | "
        f"{sum(1 for x in v if x > 0) / len(v):.0%} | {f2(min(v))} / {f2(max(v))} |"
    )


HDR = "| Вибірка | n | середнє, % спота | 95% CI (кластери-тижні) | медіана | частка > 0 | мін / макс |\n|---|---|---|---|---|---|---|"


def pct(v: list[float], q: float) -> float:
    s = sorted(v)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))]


# ----------------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/event_straddle")
    ap.add_argument("--trades-dir", default="~/.cache/tradedesk-agents/put_skew/trades24")
    ap.add_argument("--aux-dir", default="~/.cache/tradedesk-agents/put_skew", help="delivery_btc_usd.json, dvol_BTC.json")
    ap.add_argument("--gates-only", action="store_true", help="coverage, sanity checks and the power gate; no means")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    aux = Path(a.aux_dir).expanduser()
    quotes, extra = load_quotes(Path(a.trades_dir).expanduser(), cache)
    delivery = {
        ms(date.fromisoformat(r["date"])): float(r["delivery_price"]) for r in json.loads((aux / "delivery_btc_usd.json").read_text())
    }
    dvol = [(int(t), float(v)) for t, v in json.loads((aux / "dvol_BTC.json").read_text())]
    events, blocked, ev_rows = load_events(EVENTS_CSV)

    L: list[str] = ["# Event-day straddle: сирий вивід `scripts/verify_event_straddle.py`\n"]
    st = Counter(r["status"] for r in ev_rows)
    L.append(
        f"Таблиця подій: {len(ev_rows)} рядків ({', '.join(f'{k} {v}' for k, v in sorted(st.items()))}); "
        f"в основній вибірці {len(events)} днів подій у [{ymd(START_MS)}; {ymd(END_MS)}] "
        f"({', '.join(f'{k} {v}' for k, v in sorted(Counter(events.values()).items()))}). "
        f"Заблоковано для контролів (±1 доба від будь-якої дати CSV, крім cancelled, і seed): {len(blocked)} днів.\n"
    )

    def entry_ok(d: int) -> bool:
        q = quotes.get(d)
        return bool(q and q["covered"] and d + DAY_MS in delivery)

    all_days = list(range(START_MS, END_MS + 1, DAY_MS))
    valid = {d for d in all_days if entry_ok(d)}

    # ---- coverage gate
    L.append("## Ворота покриття (≥ 80 %, окремо події й контрольні дні)\n")
    ev_days = sorted(events)
    ctrl_pool = [d for d in all_days if d not in blocked]
    years = list(range(2021, 2027))
    L.append("| Група | усього | покрито | частка | " + " | ".join(str(y) for y in years) + " |\n|---|---|---|---|" + "---|" * len(years))

    def cov_line(label: str, ds: list[int]) -> float:
        ok = [d for d in ds if d in valid]
        by = []
        for y in years:
            ys = [d for d in ds if ymd(d)[:4] == str(y)]
            by.append(f"{sum(1 for d in ys if d in valid)}/{len(ys)}" if ys else "—")
        share = len(ok) / len(ds) if ds else 0.0
        L.append(f"| {label} | {len(ds)} | {len(ok)} | {share:.1%} | " + " | ".join(by) + " |")
        return share

    cov_ev = cov_line("події (усі)", ev_days)
    for k in ("fomc", "cpi", "nfp", "multi"):
        cov_line(f"  {k}", [d for d in ev_days if events[d] == k])
    cov_ctrl = cov_line("дні без подій ±1 (пул контролів)", ctrl_pool)
    reasons = Counter()
    for d in all_days:
        q = quotes.get(d)
        if q is None:
            reasons["немає файлу/угод"] += 1
        elif not q["covered"]:
            reasons[q["reason"]] += 1
        elif d + DAY_MS not in delivery:
            reasons["немає delivery price D+1"] += 1
    L.append("\nПричини непокриття (усі дні): " + (", ".join(f"{k} {v}" for k, v in reasons.most_common()) or "немає") + ".")
    gate_cov = cov_ev >= 0.8 and cov_ctrl >= 0.8
    L.append(f"\nПокриття подій {cov_ev:.1%}, контролів {cov_ctrl:.1%} → ворота {'пройдено' if gate_cov else 'НЕ пройдено'}.\n")

    # ---- sanity (no P&L)
    L.append("## Перевірки узгодженості (без P&L)\n")
    cv = [quotes[d] for d in sorted(valid)]
    r_ = [q["ratio"] for q in cv]
    L.append(f"- F/S з угод (r): медіана {statistics.median(r_):.5f}, p1 {pct(r_, 0.01):.5f}, p99 {pct(r_, 0.99):.5f}.")
    dist = [abs(q["K"] / q["F"] - 1) for q in cv]
    L.append(
        f"- |K/F0 − 1|: медіана {statistics.median(dist) * PCT:.2f} %, p95 {pct(dist, 0.95) * PCT:.2f} %, макс {max(dist) * PCT:.2f} %."
    )
    s_vs_d = [abs(quotes[d]["spot"] / delivery[d] - 1) for d in valid if d in delivery]
    L.append(
        f"- S0 (09:00) проти delivery price того ж дня (08:00): середнє |різниці| {statistics.mean(s_vs_d) * PCT:.2f} %, p95 {pct(s_vs_d, 0.95) * PCT:.2f} %."
    )
    tw = [abs(extra[d]["twap_0800"] / delivery[d] - 1) for d in extra if d in delivery and extra[d].get("twap_0800")]
    L.append(
        f"- Середній index угод за 07:30–08:00 проти delivery price (відомий еталон: delivery = TWAP індексу 07:30–08:00): медіана |різниці| {statistics.median(tw) * PCT:.3f} %, p95 {pct(tw, 0.95) * PCT:.3f} %, n={len(tw)}."
    )
    an = [q["premium"] / q["spot"] / (math.sqrt(2 / math.pi) * (q["iv_call"] + q["iv_put"]) / 2 * math.sqrt(q["T"])) for q in cv]
    L.append(
        f"- Премія / (√(2/π)·σ·√T·S0), σ = середнє IV ніг, тобто до наближення ATM-стреддла: медіана {statistics.median(an):.3f}, p5 {pct(an, 0.05):.3f}, p95 {pct(an, 0.95):.3f} (≈ 1 для ATM)."
    )
    prem = [q["premium"] / q["spot"] for q in cv]
    L.append(
        f"- Премія стреддла, % спота: медіана {statistics.median(prem) * PCT:.2f}, p5 {pct(prem, 0.05) * PCT:.2f}, p95 {pct(prem, 0.95) * PCT:.2f}."
    )
    dv = dvol_known_at_entry(dvol)
    pairs = [((q["iv_call"] + q["iv_put"]) / 2, dv[d]) for d, q in ((d, quotes[d]) for d in valid) if d in dv]
    L.append(
        f"- ATM mark IV добової (09:00) проти DVOL на закритті попереднього дня: середнє {statistics.mean(x for x, _ in pairs) * PCT:.1f} % "
        f"проти {statistics.mean(y for _, y in pairs) * PCT:.1f} %, кореляція {pearson(pairs):.3f}, n={len(pairs)}."
    )
    ag = [q["max_age_h"] for q in cv]
    L.append(f"- Вік найстарішої з двох ніг (остання угода), год: медіана {statistics.median(ag):.2f}, p90 {pct(ag, 0.9):.2f}.")
    civ = [(q["iv_call"] - q["iv_put"]) for q in cv]
    L.append(
        f"- IV call − IV put на одному страйку (паритет): медіана {statistics.median(civ) * PCT:+.2f} вп, p5/p95 {pct(civ, 0.05) * PCT:+.2f}/{pct(civ, 0.95) * PCT:+.2f}."
    )

    if not gate_cov:
        L.append("\n**Ворота покриття не пройдено: за реєстрацією ΔX і P&L не рахуються; картка → `inconclusive`.**")
        return finish(L, a.out)

    # ---- X, controls, ΔX
    X = {d: straddle_x(quotes[d], delivery[d + DAY_MS]) for d in valid}
    excl = Counter()
    rec: list[dict[str, Any]] = []
    for d in ev_days:
        if d not in valid:
            excl["вхід не покрито"] += 1
            continue
        cs = pick_controls(d, valid, blocked)
        if len(cs) < 2:
            excl["< 2 контролів"] += 1
            continue
        xc = statistics.median(X[c] for c in cs)
        rec.append({"day": d, "kind": events[d], "x": X[d], "xc": xc, "dx": X[d] - xc, "ctrls": cs, "wk": iso_week(d)})
    dx = [r["dx"] for r in rec]
    gate = power_gate(dx)
    bse = cluster_bootstrap([(r["wk"], r["dx"]) for r in rec])["se"]
    L.append("\n## Ворота потужності (друкуються до середніх)\n")
    L.append(f"- Подій із ΔX: n = {gate['n']}; виключено: " + (", ".join(f"{k} {v}" for k, v in excl.items()) or "0") + ".")
    L.append("- Контролів на подію: " + ", ".join(f"{k}: {v}" for k, v in sorted(Counter(len(r["ctrls"]) for r in rec).items())) + ".")
    L.append(f"- SD(ΔX) = {gate['sd'] * PCT:.2f} % спота; MDE = 2.8·SD/√n = **{gate['mde'] * PCT:.2f} %** (поріг 0.40 %, n ≥ 120).")
    L.append(f"- Інформативно: кластерний MDE = 2.8·SE бутстрепа по тижнях = {2.8 * bse * PCT:.2f} %.")
    L.append(
        f"- SD(X) подій {statistics.stdev(r['x'] for r in rec) * PCT:.2f} %, SD(X) контролю (медіана) {statistics.stdev(r['xc'] for r in rec) * PCT:.2f} %."
    )
    L.append(f"- Ворота потужності: **{'пройдено' if gate['passed'] else 'НЕ пройдено → лише описова таблиця, без ✓/✗'}**.\n")
    if a.gates_only:
        L.append("`--gates-only`: середні не рахувались.")
        return finish(L, a.out)
    verdicts = gate["passed"]
    mark = (lambda ok: "✓" if ok else "✗") if verdicts else (lambda ok: "(описово)")

    # ---- H1
    L.append("## H1: ΔX = X_подія − X_контроль (до витрат; % спота на номінал 1 BTC)\n")
    L.append(HDR)
    allr = [(r["wk"], r["dx"]) for r in rec]
    h1 = cluster_bootstrap(allr)
    L.append(row("**ΔX, усі події (основна)**", allr))
    L.append(row("X подій (короткий стреддл, до витрат)", [(r["wk"], r["x"]) for r in rec]))
    L.append(row("X контролю (медіана до 4 днів)", [(r["wk"], r["xc"]) for r in rec]))
    ctrl_all = sorted({c for r in rec for c in r["ctrls"]})
    L.append(row("X усіх використаних контрольних днів", [(iso_week(c), X[c]) for c in ctrl_all]))
    L.append(row("X усіх покритих днів без подій ±1", [(iso_week(d), X[d]) for d in sorted(valid) if d not in blocked]))
    sign = 1 if h1["mean"] > 0 else -1
    h1_ok = abs(h1["mean"]) >= 0.003 and h1["ci95"] is not None and (h1["ci95"][0] > 0 or h1["ci95"][1] < 0)
    L.append(
        f"\nH1: середнє {f2(h1['mean'])} %, CI {ci_s(h1['ci95'])}, кластерів {h1['clusters']}; "
        f"|ΔX| ≥ 0.30 % і CI без 0: {'так' if h1_ok else 'ні'} → {mark(h1_ok)}; знак: {'подія переоцінена' if sign > 0 else 'подія недооцінена'} (точкова оцінка)."
    )

    # ---- H3, H4, by year
    L.append("\n## H3 (розвідувально) по типах, H4 по половинах, по роках\n")
    L.append(HDR)
    for k in ("fomc", "cpi", "nfp", "multi"):
        L.append(row(f"{k}", [(r["wk"], r["dx"]) for r in rec if r["kind"] == k]))
    halves = {}
    for lab, lo, hi in (("2021–2023", 0, SPLIT_MS), ("2024–2026", SPLIT_MS, 10**15)):
        rr = [(r["wk"], r["dx"]) for r in rec if lo <= r["day"] < hi]
        halves[lab] = cluster_bootstrap(rr)
        L.append(row(lab, rr))
    for y in years:
        L.append(row(str(y), [(r["wk"], r["dx"]) for r in rec if ymd(r["day"])[:4] == str(y)]))
    types = {k: statistics.mean(r["dx"] for r in rec if r["kind"] == k) for k in ("fomc", "cpi", "nfp") if any(r["kind"] == k for r in rec)}
    h3_same = len({math.copysign(1, v) for v in types.values()}) == 1
    h4_same = halves["2021–2023"]["mean"] * halves["2024–2026"]["mean"] > 0
    L.append(
        f"\nH3: знаки FOMC/CPI/NFP {', '.join(f'{k} {f2(v)}' for k, v in types.items())} → однакові: {'так' if h3_same else 'ні'} (розвідувально)."
    )
    L.append(
        f"H4: 2021–2023 {f2(halves['2021–2023']['mean'])}, 2024–2026 {f2(halves['2024–2026']['mean'])} → знак однаковий: {'так' if h4_same else 'ні'} → {mark(h4_same)}."
    )

    # ---- robustness
    L.append("\n## Стійкість ΔX (усі варіанти; жоден не обрано після результатів)\n")
    L.append(HDR)
    top_dir = drop_top(allr, 3, direction=sign)
    L.append(row(f"без топ-3 внесків у напрямку середнього ({'+' if sign > 0 else '−'})", top_dir))
    L.append(row("без топ-3 за |ΔX|", drop_top(allr, 3)))
    L.append(row("без 2022", [(r["wk"], r["dx"]) for r in rec if ymd(r["day"])[:4] != "2022"]))
    L.append(row("без 2026 (дати 2026 — розклад)", [(r["wk"], r["dx"]) for r in rec if ymd(r["day"])[:4] != "2026"]))
    L.append(row("лише події з 4 контролями", [(r["wk"], r["dx"]) for r in rec if len(r["ctrls"]) == 4]))
    xt = {d: straddle_x(quotes[d], delivery[d + DAY_MS], premium_key="premium_trade") for d in valid}
    L.append(row("IV угод замість mark IV", [(r["wk"], xt[r["day"]] - statistics.median(xt[c] for c in r["ctrls"])) for r in rec]))
    xi = {d: straddle_x_inverse(quotes[d], delivery[d + DAY_MS]) for d in valid}
    L.append(row("інверсний облік (частка 1 BTC)", [(r["wk"], xi[r["day"]] - statistics.median(xi[c] for c in r["ctrls"])) for r in rec]))
    L.append(
        row(
            "контроль — середнє замість медіани (розвідувально)",
            [(r["wk"], r["x"] - statistics.mean(X[c] for c in r["ctrls"])) for r in rec],
        )
    )
    no_top = cluster_bootstrap(top_dir)
    kill_d = no_top["mean"] * h1["mean"] <= 0

    # ---- H2
    L.append(f"\n## H2 (наслідок H1): чистий P&L події в напрямку знака H1 ({'короткий' if sign > 0 else 'довгий'} стреддл)\n")
    by_year_hs: dict[tuple[str, int], float] = {}
    for side, key in ((1, "hs_sell"), (-1, "hs_buy")):
        for y in years:
            v = [quotes[d][key] for d in valid if ymd(d)[:4] == str(y) and quotes[d].get(key) is not None]
            if v:
                by_year_hs[(str(y), side)] = statistics.median(v)

    def net(d: int, model: str, direction: int, premium_key: str = "premium") -> float:
        q = quotes[d]
        st_ = delivery[d + DAY_MS]
        x = straddle_x(q, st_, premium_key=premium_key)
        return direction * x - cost_frac(q, st_, model=model, direction=direction, fallback_hs=by_year_hs.get((ymd(d)[:4], direction)))

    L.append(HDR)
    h2 = {}
    for model in ("base", "stress", "empirical", "zero"):
        rr = [(r["wk"], net(r["day"], model, sign)) for r in rec]
        h2[model] = cluster_bootstrap(rr)
        L.append(row(f"події, витрати {model}", rr))
    for model in ("base",):
        L.append(row(f"контрольні дні (усі використані), витрати {model}", [(iso_week(c), net(c, model, sign)) for c in ctrl_all]))
    emp_cov = sum(1 for r in rec if quotes[r["day"]].get("hs_sell" if sign > 0 else "hs_buy") is not None) / len(rec)
    costs = {
        m: statistics.mean(
            cost_frac(
                quotes[r["day"]],
                delivery[r["day"] + DAY_MS],
                model=m,
                direction=sign,
                fallback_hs=by_year_hs.get((ymd(r["day"])[:4], sign)),
            )
            for r in rec
        )
        for m in ("base", "stress", "empirical")
    }
    L.append(
        f"\nСередні витрати за коло, % спота: base {costs['base'] * PCT:.3f}, stress {costs['stress'] * PCT:.3f}, empirical {costs['empirical'] * PCT:.3f} "
        f"(частка подій з власним півспредом боку {emp_cov:.0%}, решта — медіана року)."
    )
    b = h2["base"]
    h2_ok = b["mean"] >= 0.0015 and b["ci95"] is not None and b["ci95"][0] > 0
    L.append(
        f"H2 (base): {f2(b['mean'])} %, CI {ci_s(b['ci95'])} → ≥ +0.15 % і CI > 0: {'так' if h2_ok else 'ні'} → {mark(h2_ok)} (наслідок H1)."
    )
    early = [r["dx"] for r in rec if r["day"] < SPLIT_MS]
    s_early = 1 if statistics.mean(early) > 0 else -1
    oos = [(r["wk"], net(r["day"], "base", s_early)) for r in rec if r["day"] >= SPLIT_MS]
    L.append("\nПозавибірково: знак з 2021–2023, P&L подій 2024–2026 (base):\n")
    L.append(HDR)
    L.append(row(f"знак 2021–2023 = {'+' if s_early > 0 else '−'} ({'короткий' if s_early > 0 else 'довгий'}), події 2024–2026", oos))
    oos_b = cluster_bootstrap(oos)

    # ---- tail, MAE
    L.append("\n## Хвіст і MAE (короткий стреддл, до витрат)\n")
    worst = sorted(rec, key=lambda r: r["x"])[:5]
    L.append(
        "Найгірші 5 подій для продавця (X): "
        + "; ".join(f"{ymd(r['day'])} {r['kind']} {f2(r['x'])} % (ΔX {f2(r['dx'])})" for r in worst)
        + "."
    )
    best = sorted(rec, key=lambda r: -r["dx"])[:5]
    L.append("Найбільші 5 ΔX: " + "; ".join(f"{ymd(r['day'])} {r['kind']} {f2(r['dx'])}" for r in best) + ".")
    lowest = sorted(rec, key=lambda r: r["dx"])[:5]
    L.append("Найменші 5 ΔX: " + "; ".join(f"{ymd(r['day'])} {r['kind']} {f2(r['dx'])}" for r in lowest) + ".")

    def mae_of(d: int) -> float | None:
        x = extra.get(d + DAY_MS)
        if not x or not x["path_prev"]:
            return None
        return mae_short([tuple(p) for p in x["path_prev"]], quotes[d])

    me = [m for r in rec if (m := mae_of(r["day"])) is not None]
    mc = [m for c in ctrl_all if (m := mae_of(c)) is not None]
    if me and mc:
        L.append(
            f"\nMAE (оцінка знизу за внутрішньою вартістю, % спота; > 0 = збиток): події медіана {statistics.median(me) * PCT:+.2f}, p95 {pct(me, 0.95) * PCT:+.2f}, макс {max(me) * PCT:+.2f} (n={len(me)}); "
            f"контрольні дні медіана {statistics.median(mc) * PCT:+.2f}, p95 {pct(mc, 0.95) * PCT:+.2f}, макс {max(mc) * PCT:+.2f} (n={len(mc)})."
        )

    # ---- descriptive diagnostics
    L.append("\n## Описово (діагностика, не вердикт)\n")
    L.append(
        "| Група | n | премія (implied move), % | |ln(S_T/S0)|, % | відношення | ATM IV E, % | IV E / IV E+1д | обсяг експірації E у вікні, BTC (медіана) | обсяг на K, BTC (медіана) |\n|---|---|---|---|---|---|---|---|---|"
    )

    def diag(label: str, ds: list[int]) -> None:
        ds = [d for d in ds if d in valid]
        if not ds:
            return
        im = [quotes[d]["premium"] / quotes[d]["spot"] for d in ds]
        rm = [abs(math.log(delivery[d + DAY_MS] / quotes[d]["spot"])) for d in ds]
        iv = [(quotes[d]["iv_call"] + quotes[d]["iv_put"]) / 2 for d in ds]
        tr = [(quotes[d]["iv_call"] + quotes[d]["iv_put"]) / 2 / quotes[d]["iv_next"] for d in ds if quotes[d].get("iv_next")]
        L.append(
            f"| {label} | {len(ds)} | {statistics.mean(im) * PCT:.2f} | {statistics.mean(rm) * PCT:.2f} | {statistics.mean(rm) / statistics.mean(im):.2f} | "
            f"{statistics.mean(iv) * PCT:.1f} | {statistics.median(tr):.3f} (n={len(tr)}) | {statistics.median(quotes[d]['volume'] for d in ds):.1f} | "
            f"{statistics.median(quotes[d]['volume_K'] for d in ds):.1f} |"
        )

    diag("події", [r["day"] for r in rec])
    for k in ("fomc", "cpi", "nfp", "multi"):
        diag(f"  {k}", [r["day"] for r in rec if r["kind"] == k])
    diag("використані контрольні дні", ctrl_all)
    diag("усі дні без подій ±1", [d for d in valid if d not in blocked])
    L.append("\nВідношення = середній |ln(S_T/S0)| / середня премія; для чесної ціни ATM-стреддла ≈ 1 (премія ≈ E|S_T − K|).")

    # ---- mechanical kill criteria
    L.append("\n## Механічна перевірка критеріїв (за реєстрацією)\n")
    if not verdicts:
        L.append("Ворота потужності не пройдено: ✓/✗ не ставляться; нижче — лише описові відповіді на критерії.\n")
    L.append(f"- (б) CI ΔX включає 0 або |ΔX| < 0.30 %: {'так' if not h1_ok else 'ні'}.")
    L.append(
        f"- (в) знак різний у половинах (H4): {'так' if not h4_same else 'ні'}; різний для CPI і FOMC: {'так' if 'cpi' in types and 'fomc' in types and types['cpi'] * types['fomc'] < 0 else 'ні'}."
    )
    L.append(
        f"- (г) чистий дохід ≤ 0 після 0.15 % або CI включає 0: {'так' if not (b['mean'] > 0 and b['ci95'] and b['ci95'][0] > 0) else 'ні'}."
    )
    L.append(
        f"- (д) без топ-3 подій (у напрямку середнього) знак змінюється: {'так' if kill_d else 'ні'} ({f2(no_top['mean'])} проти {f2(h1['mean'])})."
    )
    L.append(f"- Позавибірковий H2 (знак 2021–2023 → 2024–2026): {f2(oos_b['mean'])} %, CI {ci_s(oos_b['ci95'])}.")

    # ---- per-event list
    L.append("\n## Усі події (D, тип, X, X_контроль, ΔX, % спота; контролі)\n")
    L.append("| D | тип | X | X_контр | ΔX | контрольні дні |\n|---|---|---|---|---|---|")
    for r in rec:
        L.append(
            f"| {ymd(r['day'])} | {r['kind']} | {f2(r['x'])} | {f2(r['xc'])} | {f2(r['dx'])} | {', '.join(ymd(c)[5:] for c in r['ctrls'])} |"
        )
    return finish(L, a.out)


def finish(L: list[str], out: str) -> None:
    report = "\n".join(L)
    print(report, flush=True)
    if out:
        Path(out).expanduser().write_text(report + "\n")


if __name__ == "__main__":
    main()
