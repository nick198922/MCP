"""Weekend vol discount on BTC: weekend (Fri 16 -> Sun 08 UTC) vs mid-week (Tue 16 -> Thu 08) windows.

IDEA-20261014-weekend-vol-discount; the pre-registration is in the idea card ("Попередня реєстрація").
Data:
  * BTCUSDT spot hourly klines from the Binance public archive (``rt.binance_vision``; monthly files, daily files
    for the current month), cached per file under ``--cache-dir`` (default ``~/.cache/tradedesk-agents/weekend_vol``);
  * Deribit BTC option prints ``~/.cache/tradedesk-agents/put_skew/trades24`` and delivery prices
    ``put_skew/delivery_btc_usd.json`` (built by ``scripts/verify_put_skew.py``), read only;
  * the US macro dates table ``docs/verification/2026-10-15-us-macro-events.csv`` (UNVERIFIED, Q-20261015-2) and
    ``src/tradedesk/data/events_seed.json``: used only to exclude control weeks (and NFP Fridays in a sensitivity).

Usage:
    uv run python scripts/verify_weekend_vol.py --stage fetch     # Binance archive klines -> cache
    uv run python scripts/verify_weekend_vol.py --stage stage0    # H0 gate (OHLCV only)
    uv run python scripts/verify_weekend_vol.py --stage gates     # option coverage + H2 power gate, no means
    uv run python scripts/verify_weekend_vol.py --stage full --out raw.md
Stages after stage0 refuse to run if the H0 gate fails (the card is closed without looking at options).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import math
import os
import statistics
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from tradedesk.analytics.event_straddle import cost_frac, entry_quote, straddle_x, straddle_x_inverse
from tradedesk.analytics.weekend_vol import (
    DAY_MS,
    HOUR_MS,
    WEEK_MS,
    block_bootstrap,
    event_weeks,
    h0_gate,
    hedged_short_straddle,
    mean,
    median_weekly_diff,
    pooled_ratio,
    prices_from_bars,
    ratio_diff,
    week_windows,
    weekend_share,
    window_extremes,
    window_rv,
)
from tradedesk.providers.binance_vision import parse_klines_zip

ROOT = Path(__file__).resolve().parents[1]
EVENTS_CSV = ROOT / "docs/verification/2026-10-15-us-macro-events.csv"
SYMBOL = "BTCUSDT"
FIRST_TUE = date(2021, 1, 5)
LAST_TUE = date(2026, 10, 6)
FIRST_TUE_2020 = date(2020, 1, 7)
SPLIT = date(2024, 1, 1)
QUOTES_VERSION = "v1"
PCT = 100.0
YEARS = [str(y) for y in range(2021, 2027)]


def ms(d: date | datetime) -> int:
    if not isinstance(d, datetime):
        d = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return int(d.timestamp() * 1000)


def ymd(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


def year(t: int) -> str:
    return ymd(t)[:4]


def tuesdays(first: date, last: date) -> list[int]:
    return list(range(ms(first), ms(last) + 1, WEEK_MS))


# ----------------------------------------------------------------------------- fetch (network via runtime only)


async def fetch(cache: Path) -> None:
    from tradedesk.server import build_server

    kdir = cache / "klines"
    kdir.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[Path, str]] = []
    for y in range(2020, 2027):
        for m in range(1, 13):
            if (y, m) > (2026, 9):
                break
            name = f"{SYMBOL}-1h-{y:04d}-{m:02d}"
            jobs.append((kdir / f"{name}.json", f"/data/spot/monthly/klines/{SYMBOL}/1h/{name}.zip"))
    for d in range(1, 13):
        name = f"{SYMBOL}-1h-2026-10-{d:02d}"
        jobs.append((kdir / f"{name}.json", f"/data/spot/daily/klines/{SYMBOL}/1h/{name}.zip"))
    todo = [j for j in jobs if not j[0].exists()]
    print(f"klines: {len(jobs)} files, {len(todo)} to fetch", flush=True)
    _, rt = build_server()
    t0 = time.perf_counter()
    try:
        for i, (p, url) in enumerate(todo):
            for k in range(5):
                try:
                    f = await rt.binance_vision.http.get_bytes(url, tool="spot_klines")
                    rows = parse_klines_zip(f.data)
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"  retry {url} after {type(e).__name__}: {str(e)[:120]}", flush=True)
                    await asyncio.sleep(10 * (k + 1))
            else:
                raise RuntimeError(f"giving up on {url}")
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows))
            tmp.replace(p)
            if i == 1:
                el = time.perf_counter() - t0
                print(f"  sample: 2 files in {el:.1f}s -> estimate {el / 2 * len(todo):.0f}s for all", flush=True)
            if i % 10 == 0 or i == len(todo) - 1:
                print(f"  {i + 1}/{len(todo)} {p.name} rows={len(rows)} {time.perf_counter() - t0:.0f}s", flush=True)
            await asyncio.sleep(0.2)
    finally:
        await rt.aclose()
    print("fetch done", flush=True)


def load_bars(cache: Path) -> list[list[float]]:
    rows: dict[int, list[float]] = {}
    for p in sorted((cache / "klines").glob("*.json")):
        for r in json.loads(p.read_text()):
            rows[int(r[0])] = r
    return [rows[k] for k in sorted(rows)]


# ----------------------------------------------------------------------------- events (UNVERIFIED dates)


def load_events() -> tuple[set[int], set[int]]:
    """(Tuesdays of weeks with any event on Tue/Wed/Thu, Tuesdays of weeks whose Friday is an NFP date)."""
    rows = list(csv.DictReader(EVENTS_CSV.open()))
    all_d = [ms(date.fromisoformat(r["date"])) for r in rows if r["status"] != "cancelled"]
    nfp = [ms(date.fromisoformat(r["date"])) for r in rows if r["status"] != "cancelled" and r["kind"] == "nfp"]
    with resources.files("tradedesk.data").joinpath("events_seed.json").open("r", encoding="utf-8") as f:
        seed = json.load(f)["events"]
    all_d += [int(e["ts"]) * 1000 for e in seed]
    nfp += [int(e["ts"]) * 1000 for e in seed if e.get("kind") == "nfp"]
    return event_weeks(all_d, (0, 1, 2)), event_weeks(nfp, (3,))


# ----------------------------------------------------------------------------- formatting


def f2(x: float | None, k: float = PCT) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * k:+.2f}"


def ci_s(ci: tuple[float, float] | None, k: float = PCT, d: int = 2) -> str:
    return f"[{ci[0] * k:+.{d}f}; {ci[1] * k:+.{d}f}]" if ci else "—"


def pctl(v: Sequence[float], q: float) -> float:
    s = sorted(v)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))]


def mean_row(label: str, rows: list[tuple[int, float]]) -> str:
    if not rows:
        return f"| {label} | 0 | — | — | — | — | — |"
    v = [x for _, x in rows]
    b = block_bootstrap(v, mean)
    return (
        f"| {label} | {len(v)} | {f2(b['stat'])} | {ci_s(b['ci95'])} | {f2(statistics.median(v))} | "
        f"{sum(1 for x in v if x > 0) / len(v):.0%} | {f2(min(v))} / {f2(max(v))} |"
    )


HDR = "| Вибірка | n тижнів | середнє, % спота | 95% CI (блоки-тижні) | медіана | частка > 0 | мін / макс |\n|---|---|---|---|---|---|---|"


def finish(L: list[str], out: str) -> None:
    report = "\n".join(L)
    print(report, flush=True)
    if out:
        Path(out).expanduser().write_text(report + "\n")


# ----------------------------------------------------------------------------- stage 0


def rv_rows(prices: dict[int, float], weeks: list[int]) -> tuple[dict[int, tuple[float, float]], int]:
    out, gaps = {}, 0
    for w in weeks:
        win = week_windows(w)
        a, b = window_rv(prices, *win["wkd"]), window_rv(prices, *win["ctrl"])
        if a is None or b is None:
            gaps += 1
            continue
        out[w] = (a, b)
    return out, gaps


def ratio_line(label: str, rows: list[tuple[float, float]]) -> tuple[str, dict[str, Any]]:
    b = block_bootstrap(rows, pooled_ratio)
    return f"| {label} | {len(rows)} | {b['stat']:.3f} | {ci_s(b['ci95'], 1, 3)} |", b


def stage0(prices: dict[int, float], bars: list[list[float]], ev_weeks: set[int], nfp_weeks: set[int], L: list[str]) -> dict[str, Any]:
    weeks = tuesdays(FIRST_TUE, LAST_TUE)
    rv, gaps = rv_rows(prices, weeks)
    prim = [w for w in weeks if w in rv and w not in ev_weeks]
    L.append("## Стадія 0: дані й перевірки\n")
    first, last = int(bars[0][0]), int(bars[-1][0])
    exp_n = (last - first) // HOUR_MS + 1
    L.append(
        f"- Свічки BTCUSDT 1h (Binance spot, архів): {len(bars)} шт., {ymd(first)} … {ymd(last)}; очікувано {exp_n}, пропущено {exp_n - len(bars)}."
    )
    opens = {int(b[0]) for b in bars}
    miss = Counter(year(t) for t in range(first, last + 1, HOUR_MS) if t not in opens)
    L.append("- Пропущені години по роках: " + (", ".join(f"{k} {v}" for k, v in sorted(miss.items())) or "немає") + ".")
    L.append(
        f"- Тижнів у [{FIRST_TUE}; {LAST_TUE}]: {len(weeks)}; з пропусками у вікнах (виключено з обох вікон): {gaps}; "
        f"контрольних тижнів з подією Tue/Wed/Thu (CSV, неперевірені дати + seed): {sum(1 for w in weeks if w in ev_weeks)}; "
        f"основна вибірка: **{len(prim)}** тижнів; NFP-п'ятниць в основній вибірці: {sum(1 for w in prim if w in nfp_weeks)}."
    )
    hr = [math.log(prices[t] / prices[t - HOUR_MS]) for t in sorted(prices) if t - HOUR_MS in prices and ms(FIRST_TUE) <= t]
    L.append(
        f"- Узгодженість: річна вола з годинних доходностей 2021–2026 = {statistics.pstdev(hr) * math.sqrt(24 * 365) * PCT:.1f} % "
        "(DVOL за той самий період у середньому ≈ 60 %, реалізована BTC зазвичай нижча; порядок величини збігається)."
    )
    L.append("")

    L.append("## H0 (ворота): R0 = Σ RV²(Fri 16 → Sun 08) / Σ RV²(Tue 16 → Thu 08)\n")
    L.append("Пороги (зареєстровано): R0 ≤ 0.79, верхня межа 95 % CI < 1.00, R0 за рік ≤ 0.79 у ≥ 5 з 6 років 2021–2026.\n")
    L.append("| Вибірка | n тижнів | R0 | 95% CI |\n|---|---|---|---|")
    line, h0 = ratio_line("**основна (без контрольних тижнів з подіями; дати неперевірені)**", [rv[w] for w in prim])
    L.append(line)
    yearly = {}
    for y in YEARS:
        rr = [rv[w] for w in prim if year(w) == y]
        line, b = ratio_line(f"  {y}", rr)
        L.append(line)
        yearly[y] = b["stat"]
    sens = {}
    for lab, rows in (
        ("чутливість: без виключень", [rv[w] for w in weeks if w in rv]),
        ("чутливість: без NFP-п'ятниць", [rv[w] for w in prim if w not in nfp_weeks]),
        ("чутливість: 2021–2023", [rv[w] for w in prim if w < ms(SPLIT)]),
        ("чутливість: 2024–2026", [rv[w] for w in prim if w >= ms(SPLIT)]),
    ):
        line, sens[lab] = ratio_line(lab, rows)
        L.append(line)
    w20 = tuesdays(FIRST_TUE_2020, date(2020, 12, 29))
    rv20, _ = rv_rows(prices, w20)
    line, _ = ratio_line("чутливість: 2020 (без виключень, таблиці подій для 2020 немає)", list(rv20.values()))
    L.append(line)
    gate = h0_gate(h0["stat"], h0["ci95"][1], yearly)
    L.append(
        f"\nH0: R0 = {h0['stat']:.3f} (≤ 0.79: {'так' if gate['pooled_ok'] else 'ні'}), CI {ci_s(h0['ci95'], 1, 3)} "
        f"(верх < 1: {'так' if gate['ci_ok'] else 'ні'}), років з R0 ≤ 0.79: {gate['years_passed']}/6 "
        f"(≥ 5: {'так' if gate['years_ok'] else 'ні'}) → **{'ПРОЙДЕНО' if gate['passed'] else 'НЕ ПРОЙДЕНО'}**."
    )
    nx = sens["чутливість: без виключень"]
    gate_nx = h0_gate(nx["stat"], nx["ci95"][1], {y: pooled_ratio([rv[w] for w in weeks if w in rv and year(w) == y]) for y in YEARS})
    L.append(
        f"Те саме без виключень контрольних тижнів: {'пройдено' if gate_nx['passed'] else 'не пройдено'} (років {gate_nx['years_passed']}/6)."
    )

    # descriptive: Sat 08 -> Mon 08 share of the week's variance
    L.append("\n### Описово: частка дисперсії Sat 08 → Mon 08 у тижні Mon 08 → Mon 08 (пропорційно 2/7 = 28.6 %)\n")
    L.append("| Вибірка | n тижнів | частка, % | 95% CI |\n|---|---|---|---|")
    sh = {}
    for w in tuesdays(FIRST_TUE_2020, LAST_TUE):
        mon = w - DAY_MS
        if mon + WEEK_MS + 8 * HOUR_MS > max(prices):
            continue
        s = weekend_share(prices, mon)
        if s:
            sh[w] = s
    for lab, ws in [("2021–2026", [w for w in sh if w >= ms(FIRST_TUE)])] + [
        (y, [w for w in sh if year(w) == y]) for y in ["2020", *YEARS]
    ]:
        b = block_bootstrap([sh[w] for w in ws], pooled_ratio)
        L.append(f"| {lab} | {len(ws)} | {b['stat'] * PCT:.1f} | {ci_s(b['ci95'], PCT, 1)} |")
    # per-hour variance profile of the two windows, descriptive
    L.append("\n### Описово: дисперсія на годину вікна, відносно середньої години тижня (2021–2026)\n")
    allv = [math.log(prices[t] / prices[t - HOUR_MS]) ** 2 for t in prices if t - HOUR_MS in prices and t >= ms(FIRST_TUE)]
    base = mean(allv)
    for lab, key in (("вихідні Fri 16 → Sun 08", "wkd"), ("контроль Tue 16 → Thu 08", "ctrl")):
        tot = sum(rv[w][0 if key == "wkd" else 1] for w in weeks if w in rv)
        n = sum(1 for w in weeks if w in rv) * 40
        L.append(f"- {lab}: {tot / n / base:.3f}")
    return {"gate": gate, "rv": rv, "prim": prim, "h0": h0, "gate_noexcl": gate_nx}


# ----------------------------------------------------------------------------- stage 1: option quotes (cached)


def _quote(job: tuple[str, int]) -> tuple[int, dict[str, Any] | None]:
    path, day = job
    p = Path(path)
    if not p.exists():
        return day, None
    trades = json.loads(gzip.decompress(p.read_bytes()))["trades"]
    return day, entry_quote(trades, day, entry_hour=16, expiry_days=2)


def load_quotes(trades_dir: Path, cache: Path, weeks: list[int]) -> dict[int, dict[str, Any]]:
    days = sorted({w for w in weeks} | {w + 3 * DAY_MS for w in weeks})
    cp = cache / f"quotes_{QUOTES_VERSION}.json"
    if cp.exists():
        blob = json.loads(cp.read_text())
        if blob.get("days") == days:
            return {int(k): v for k, v in blob["quotes"].items() if v}
    # entry at D 16:00 reads the cache file of D + 1 (window [D 09:00; D+1 09:00])
    jobs = [(str(trades_dir / f"{ymd(d + DAY_MS)}.json.gz"), d) for d in days]
    print(f"quotes: {len(jobs)} entry days", flush=True)
    t0 = time.perf_counter()
    out: dict[str, Any] = {}
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 2)) as ex:
        for i, (d, q) in enumerate(ex.map(_quote, jobs, chunksize=8)):
            out[str(d)] = q
            if i % 100 == 0 or i == len(jobs) - 1:
                print(f"  {i + 1}/{len(jobs)} in {time.perf_counter() - t0:.0f}s", flush=True)
    cp.write_text(json.dumps({"days": days, "quotes": out}))
    return {int(k): v for k, v in out.items() if v}


def stage1(
    s0: dict[str, Any],
    prices: dict[int, float],
    bars: list[list[float]],
    quotes: dict[int, dict[str, Any]],
    delivery: dict[int, float],
    ev_weeks: set[int],
    nfp_weeks: set[int],
    gates_only: bool,
    L: list[str],
) -> None:
    rv: dict[int, tuple[float, float]] = s0["rv"]
    weeks_all = [w for w in tuesdays(FIRST_TUE, LAST_TUE) if w in rv]

    def ok(d: int, exp_day: int) -> bool:
        q = quotes.get(d)
        return bool(q and q["covered"] and exp_day in delivery)

    def wk_ok(w: int) -> bool:
        return ok(w + 3 * DAY_MS, w + 5 * DAY_MS)

    def ct_ok(w: int) -> bool:
        return ok(w, w + 2 * DAY_MS)

    prim_all = s0["prim"]
    L.append("\n## Стадія 1: ворота покриття (вхід 16:00 лише з угод ≤ 16:00, експірація через 2 доби 08:00)\n")
    L.append("| Вибірка | тижнів | вихідні покрито | контроль покрито | обидва |\n|---|---|---|---|---|")
    for lab, ws in [("основна", prim_all)] + [(y, [w for w in prim_all if year(w) == y]) for y in YEARS]:
        a, b, c = sum(map(wk_ok, ws)), sum(map(ct_ok, ws)), sum(1 for w in ws if wk_ok(w) and ct_ok(w))
        L.append(f"| {lab} | {len(ws)} | {a} ({a / len(ws):.0%}) | {b} ({b / len(ws):.0%}) | {c} |")
    cov_w, cov_c = sum(map(wk_ok, prim_all)) / len(prim_all), sum(map(ct_ok, prim_all)) / len(prim_all)
    reasons = Counter()
    for w in prim_all:
        for d, e in ((w + 3 * DAY_MS, w + 5 * DAY_MS), (w, w + 2 * DAY_MS)):
            q = quotes.get(d)
            if q is None:
                reasons["немає файлу/угод"] += 1
            elif not q["covered"]:
                reasons[q["reason"]] += 1
            elif e not in delivery:
                reasons["немає delivery"] += 1
    L.append("\nПричини непокриття (входи основної вибірки): " + (", ".join(f"{k} {v}" for k, v in reasons.most_common()) or "немає") + ".")
    gate_cov = cov_w >= 0.8 and cov_c >= 0.8
    L.append(f"Покриття вихідних {cov_w:.1%}, контролю {cov_c:.1%} → ворота покриття **{'пройдено' if gate_cov else 'НЕ пройдено'}**.\n")

    # sanity (no P&L)
    cov_q = [quotes[d] for w in weeks_all for d in (w, w + 3 * DAY_MS) if quotes.get(d) and quotes[d]["covered"]]
    L.append("### Перевірки узгодженості (без P&L)\n")
    dd = []
    for w in weeks_all:
        for e in (w + 2 * DAY_MS, w + 5 * DAY_MS):
            if e in delivery and (e + 8 * HOUR_MS) in prices:
                dd.append(abs(prices[e + 8 * HOUR_MS] / delivery[e] - 1))
    L.append(
        f"- Binance close о 08:00 проти delivery price Deribit (TWAP індексу 07:30–08:00), Thu і Sun: медіана |різниці| {statistics.median(dd) * PCT:.3f} %, p95 {pctl(dd, 0.95) * PCT:.3f} %, n={len(dd)}."
    )
    s_b = [abs(q["spot"] / prices[q["t0"]] - 1) for q in cov_q if q["t0"] in prices]
    L.append(
        f"- S0 (index Deribit) проти Binance close о 16:00: медіана |різниці| {statistics.median(s_b) * PCT:.3f} %, p95 {pctl(s_b, 0.95) * PCT:.3f} %."
    )
    an = [q["premium"] / q["spot"] / (math.sqrt(2 / math.pi) * (q["iv_call"] + q["iv_put"]) / 2 * math.sqrt(q["T"])) for q in cov_q]
    L.append(f"- Премія / (√(2/π)·σ·√T·S0): медіана {statistics.median(an):.3f} (≈ 1 для ATM).")
    L.append(f"- T входу, год: {statistics.median(q['T'] * 365 * 24 for q in cov_q):.2f} (має бути 40).")
    L.append(
        f"- F/S (r): медіана {statistics.median(q['ratio'] for q in cov_q):.5f}; |K/F0 − 1| медіана {statistics.median(abs(q['K'] / q['F'] - 1) for q in cov_q) * PCT:.2f} %."
    )
    L.append(
        f"- Вік найстарішої ноги, год: медіана {statistics.median(q['max_age_h'] for q in cov_q):.2f}, p90 {pctl([q['max_age_h'] for q in cov_q], 0.9):.2f}."
    )
    if not gate_cov:
        L.append("\n**Ворота покриття не пройдено → `inconclusive`, далі не рахується.**")
        return

    # pairs
    def pairs_of(ws: list[int]) -> list[int]:
        return [w for w in ws if wk_ok(w) and ct_ok(w)]

    P = pairs_of(prim_all)
    Pnx = pairs_of(weeks_all)
    Pnfp = [w for w in P if w not in nfp_weeks]

    def X(d: int, e: int, key: str = "premium") -> float:
        return straddle_x(quotes[d], delivery[e], premium_key=key)

    xw = {w: X(w + 3 * DAY_MS, w + 5 * DAY_MS) for w in Pnx}
    xc = {w: X(w, w + 2 * DAY_MS) for w in Pnx}
    dlt = [xw[w] - xc[w] for w in P]
    sd = statistics.stdev(dlt)
    mde = 2.8 * sd / math.sqrt(len(P))
    gate_pw = mde <= 0.004 and len(P) >= 200
    se_b = block_bootstrap(dlt, mean)["se"]
    L.append("\n## Ворота потужності H2 (друкуються до середніх)\n")
    L.append(f"- Пар (тиждень з покритими обома входами й обома вікнами RV): n = {len(P)} (без виключень {len(Pnx)}).")
    L.append(
        f"- SD(Δ), Δ = X_вих − X_контр до витрат: {sd * PCT:.2f} % спота; SD(X_вих) {statistics.stdev(xw[w] for w in P) * PCT:.2f} %, SD(X_контр) {statistics.stdev(xc[w] for w in P) * PCT:.2f} %."
    )
    L.append(
        f"- MDE = 2.8·SD/√n = **{mde * PCT:.2f} %** (поріг 0.40 %, n ≥ 200); інформативно 2.8·SE бутстрепа = {2.8 * se_b * PCT:.2f} %."
    )
    L.append(
        f"- Ворота потужності: **{'пройдено' if gate_pw else 'НЕ пройдено → H2 і H3 лише описово; вердикт за H1 («ціноутворення»)'}**."
    )
    L.append(f"- H1: n = {len(P)} пар (поріг для ✓/✗ — 200).\n")
    if gates_only:
        L.append("`--stage gates`: середні не рахувались.")
        return

    # ---- H1
    def iv2t(q: dict[str, Any], trade: bool = False) -> float:
        iv = (q["iv_call_trade"] + q["iv_put_trade"]) / 2 if trade else (q["iv_call"] + q["iv_put"]) / 2
        return iv * iv * q["T"]

    def h1_rows(ws: list[int], trade: bool = False) -> list[tuple[float, float, float, float]]:
        return [(iv2t(quotes[w + 3 * DAY_MS], trade), iv2t(quotes[w], trade), rv[w][0], rv[w][1]) for w in ws]

    L.append("## H1: D = r_IV − r_RV (пулові відношення вихідні / контроль)\n")
    L.append(
        "Поріг (зареєстровано): D ≥ +0.10 і нижня межа 95 % CI > 0, n ≥ 200. D > 0: ринок знижує IV вихідних менше, ніж падає реалізована дисперсія.\n"
    )
    L.append("| Вибірка | n | r_IV [CI] | r_RV [CI] | D | 95% CI D |\n|---|---|---|---|---|---|")

    def h1_line(lab: str, rows: list[tuple[float, float, float, float]]) -> dict[str, Any]:
        if len(rows) < 2:
            L.append(f"| {lab} | {len(rows)} | — | — | — | — |")
            return {"stat": float("nan"), "ci95": None}
        bi = block_bootstrap([(a, b) for a, b, _, _ in rows], pooled_ratio)
        br = block_bootstrap([(c, d) for _, _, c, d in rows], pooled_ratio)
        bd = block_bootstrap(rows, ratio_diff)
        L.append(
            f"| {lab} | {len(rows)} | {bi['stat']:.3f} {ci_s(bi['ci95'], 1, 3)} | {br['stat']:.3f} {ci_s(br['ci95'], 1, 3)} | "
            f"**{bd['stat']:+.3f}** | {ci_s(bd['ci95'], 1, 3)} |"
        )
        return bd

    h1 = h1_line("**основна (дати неперевірені)**", h1_rows(P))
    h1_nx = h1_line("без виключень", h1_rows(Pnx))
    h1_line("без NFP-п'ятниць", h1_rows(Pnfp))
    h1_line("IV угод замість mark IV", h1_rows(P, trade=True))
    h1_line("2021–2023", h1_rows([w for w in P if w < ms(SPLIT)]))
    h1_line("2024–2026", h1_rows([w for w in P if w >= ms(SPLIT)]))
    h1_line("без 2022", h1_rows([w for w in P if year(w) != "2022"]))
    for y in YEARS:
        h1_line(f"  {y}", h1_rows([w for w in P if year(w) == y]))
    md = block_bootstrap(h1_rows(P), median_weekly_diff)
    L.append(
        f"\nЧутливість (оригінальний оцінювач картки): медіана тижневих різниць відношень = {md['stat']:+.3f}, CI {ci_s(md['ci95'], 1, 3)}."
    )

    def h1_verdict(b: dict[str, Any], n: int) -> str:
        if n < 200:
            return "◯"
        return "✓" if b["stat"] >= 0.10 and b["ci95"] and b["ci95"][0] > 0 else "✗"

    v1, v1nx = h1_verdict(h1, len(P)), h1_verdict(h1_nx, len(Pnx))
    L.append(
        f"H1: D = {h1['stat']:+.3f}, CI {ci_s(h1['ci95'], 1, 3)}, n = {len(P)} → **{v1}**; "
        f"без виключень (n = {len(Pnx)}) D = {h1_nx['stat']:+.3f}, CI {ci_s(h1_nx['ci95'], 1, 3)} → {v1nx}."
    )
    lv = [
        ((quotes[w + 3 * DAY_MS]["iv_call"] + quotes[w + 3 * DAY_MS]["iv_put"]) / 2, (quotes[w]["iv_call"] + quotes[w]["iv_put"]) / 2)
        for w in P
    ]
    ann = 365 * 24 / 40
    L.append(
        f"Рівні (описово): середня ATM IV вихідних {mean([a for a, _ in lv]) * PCT:.1f} %, контролю {mean([b for _, b in lv]) * PCT:.1f} %; "
        f"реалізована вола (√(ΣRV²/n·{ann:.0f})) вихідних {math.sqrt(mean([rv[w][0] for w in P]) * ann) * PCT:.1f} %, контролю {math.sqrt(mean([rv[w][1] for w in P]) * ann) * PCT:.1f} %."
    )
    # exploratory, added after seeing the results: is D an artifact of hourly sampling? Terminal squared log return
    # ln(S_T / S0)^2 (Deribit delivery vs entry index) instead of the hourly RV; and premium vs realized |S_T - K|.
    L.append("\n**Розвідувально (додано після перегляду результатів):** D з термінальною дисперсією ln(S_T/S0)² замість годинної RV.\n")
    L.append("| Вибірка | n | r_IV [CI] | r_RV [CI] | D | 95% CI D |\n|---|---|---|---|---|---|")

    def term_rows(ws: list[int]) -> list[tuple[float, float, float, float]]:
        out = []
        for w in ws:
            qw, qc = quotes[w + 3 * DAY_MS], quotes[w]
            out.append(
                (
                    iv2t(qw),
                    iv2t(qc),
                    math.log(delivery[w + 5 * DAY_MS] / qw["spot"]) ** 2,
                    math.log(delivery[w + 2 * DAY_MS] / qc["spot"]) ** 2,
                )
            )
        return out

    h1_line("термінальна, основна", term_rows(P))
    h1_line("термінальна, без виключень", term_rows(Pnx))
    for lab, ws in (("основна", P), ("без виключень", Pnx)):
        pw = mean([quotes[w + 3 * DAY_MS]["premium"] / quotes[w + 3 * DAY_MS]["spot"] for w in ws])
        pc = mean([quotes[w]["premium"] / quotes[w]["spot"] for w in ws])
        mw_ = mean([abs(delivery[w + 5 * DAY_MS] - quotes[w + 3 * DAY_MS]["K"]) / quotes[w + 3 * DAY_MS]["spot"] for w in ws])
        mc_ = mean([abs(delivery[w + 2 * DAY_MS] - quotes[w]["K"]) / quotes[w]["spot"] for w in ws])
        L.append(
            f"- {lab}: премія вихідних {pw * PCT:.2f} %, |S_T − K| {mw_ * PCT:.2f} % (відношення {mw_ / pw:.2f}); "
            f"контроль {pc * PCT:.2f} %, {mc_ * PCT:.2f} % (відношення {mc_ / pc:.2f})."
        )

    # ---- H2
    hs_year: dict[str, float] = {}
    for y in YEARS:
        v = [quotes[d]["hs_sell"] for w in Pnx for d in (w, w + 3 * DAY_MS) if year(d) == y and quotes[d].get("hs_sell") is not None]
        if v:
            hs_year[y] = statistics.median(v)

    def net(d: int, e: int, model: str, key: str = "premium") -> float:
        q = quotes[d]
        return straddle_x(q, delivery[e], premium_key=key) - cost_frac(
            q, delivery[e], model=model, direction=1, fallback_hs=hs_year.get(year(d))
        )

    def nw(w: int, model: str = "base") -> float:
        return net(w + 3 * DAY_MS, w + 5 * DAY_MS, model)

    def nc(w: int, model: str = "base") -> float:
        return net(w, w + 2 * DAY_MS, model)

    L.append(
        f"\n## H2 (торгова): короткий ATM-стреддл, чистий X, % спота на номінал 1 BTC {'' if gate_pw else '(ОПИСОВО: ворота потужності не пройдено)'}\n"
    )
    L.append(HDR)
    res = {}
    for model in ("base", "stress", "empirical", "zero"):
        L.append(mean_row(f"вихідні, витрати {model}", [(w, nw(w, model)) for w in P]))
        L.append(mean_row(f"контроль, витрати {model}", [(w, nc(w, model)) for w in P]))
        rr = [(w, nw(w, model) - nc(w, model)) for w in P]
        res[model] = block_bootstrap([x for _, x in rr], mean)
        L.append(mean_row(f"**Δ = вихідні − контроль, {model}**", rr))
    costs = {
        m: mean(
            [
                cost_frac(quotes[d], delivery[e], model=m, direction=1, fallback_hs=hs_year.get(year(d)))
                for w in P
                for d, e in ((w, w + 2 * DAY_MS), (w + 3 * DAY_MS, w + 5 * DAY_MS))
            ]
        )
        for m in ("base", "stress", "empirical")
    }
    L.append(
        f"\nСередні витрати за стреддл, % спота: base {costs['base'] * PCT:.3f}, stress {costs['stress'] * PCT:.3f}, empirical {costs['empirical'] * PCT:.3f}."
    )
    mw = mean([nw(w) for w in P])
    b = res["base"]
    h2_ok = gate_pw and mw >= 0.002 and b["stat"] >= 0.002 and b["ci95"][0] > 0
    L.append(
        f"H2 (base): вихідні {f2(mw)} % (≥ +0.20: {'так' if mw >= 0.002 else 'ні'}), Δ {f2(b['stat'])} п.п., CI {ci_s(b['ci95'])} "
        f"→ {'✓' if h2_ok else ('✗' if gate_pw else 'описово (◯)')}."
    )

    # ---- H3 + robustness
    L.append("\n## H3 і стійкість Δ (base, усі варіанти)\n")
    L.append(HDR)
    allr = [(w, nw(w) - nc(w)) for w in P]
    sign = 1 if b["stat"] > 0 else -1
    early = [(w, x) for w, x in allr if w < ms(SPLIT)]
    late = [(w, x) for w, x in allr if w >= ms(SPLIT)]
    L.append(mean_row("2021–2023", early))
    L.append(mean_row("2024–2026", late))
    drop = set(sorted(allr, key=lambda r: sign * r[1], reverse=True)[:3])
    no_top = [r for r in allr if r not in drop]
    L.append(mean_row(f"без топ-3 внесків у напрямку знака ({'+' if sign > 0 else '−'})", no_top))
    L.append(mean_row("без 2022", [r for r in allr if year(r[0]) != "2022"]))
    L.append(mean_row("без NFP-п'ятниць", [r for r in allr if r[0] not in nfp_weeks]))
    L.append(mean_row("без виключень контрольних тижнів", [(w, nw(w) - nc(w)) for w in Pnx]))
    L.append(
        mean_row(
            "IV угод (base)",
            [
                (w, net(w + 3 * DAY_MS, w + 5 * DAY_MS, "base", "premium_trade") - net(w, w + 2 * DAY_MS, "base", "premium_trade"))
                for w in P
            ],
        )
    )
    xi = [
        (w, straddle_x_inverse(quotes[w + 3 * DAY_MS], delivery[w + 5 * DAY_MS]) - straddle_x_inverse(quotes[w], delivery[w + 2 * DAY_MS]))
        for w in P
    ]
    L.append(mean_row("інверсний облік, до витрат (частка 1 BTC)", xi))
    for y in YEARS:
        L.append(mean_row(f"  {y}", [r for r in allr if year(r[0]) == y]))
    h3_same = mean([x for _, x in early]) * mean([x for _, x in late]) > 0
    h3_top = mean([x for _, x in no_top]) * b["stat"] > 0
    L.append(f"\nH3: знак однаковий у половинах: {'так' if h3_same else 'ні'}; без топ-3 знак зберігається: {'так' if h3_top else 'ні'}.")

    # ---- tail, MAE, drawdown
    L.append("\n## Хвіст (короткий стреддл вихідних, до витрат)\n")
    L.append("Найгірші 5 вихідних: " + "; ".join(f"{ymd(w + 3 * DAY_MS)} {f2(xw[w])} %" for w in sorted(P, key=lambda w: xw[w])[:5]) + ".")
    L.append("Найгірші 5 контролів: " + "; ".join(f"{ymd(w)} {f2(xc[w])} %" for w in sorted(P, key=lambda w: xc[w])[:5]) + ".")

    def mae(d: int) -> float | None:
        q = quotes[d]
        ex = window_extremes(bars, q["t0"], q["expiry"])
        return None if ex is None else (max(abs(ex[0] - q["K"]), abs(ex[1] - q["K"])) - q["premium"]) / q["spot"]

    mw_ = [m for w in P if (m := mae(w + 3 * DAY_MS)) is not None]
    mc_ = [m for w in P if (m := mae(w)) is not None]
    L.append(
        f"MAE (оцінка знизу за внутрішньою вартістю, годинні high/low Binance, > 0 = збиток, % спота): вихідні медіана {statistics.median(mw_) * PCT:+.2f}, "
        f"p95 {pctl(mw_, 0.95) * PCT:+.2f}, макс {max(mw_) * PCT:+.2f}; контроль медіана {statistics.median(mc_) * PCT:+.2f}, p95 {pctl(mc_, 0.95) * PCT:+.2f}, макс {max(mc_) * PCT:+.2f}."
    )
    oct25 = ms(date(2025, 10, 7))
    if oct25 in quotes and (oct25 + 3 * DAY_MS) in quotes and oct25 + 5 * DAY_MS in delivery:
        q = quotes[oct25 + 3 * DAY_MS]
        L.append(
            f"Вихідні 2025-10-10 16:00 → 2025-10-12 08:00: S0 {q['spot']:.0f}, K {q['K']:.0f}, премія {q['premium'] / q['spot'] * PCT:.2f} %, "
            f"S_T {delivery[oct25 + 5 * DAY_MS]:.0f}, X {f2(straddle_x(q, delivery[oct25 + 5 * DAY_MS]))} %, MAE {f2(mae(oct25 + 3 * DAY_MS))} %, "
            f"в основній вибірці: {'так' if oct25 in P else 'ні'}."
        )
    cum, peak, dd_ = 0.0, 0.0, 0.0
    for w in P:
        cum += nw(w)
        peak = max(peak, cum)
        dd_ = max(dd_, peak - cum)
    L.append(
        f"Кумулятивний чистий X вихідних (base, сума по тижнях, на номінал): {cum * PCT:+.1f} %, максимальна просадка {dd_ * PCT:.1f} % спота."
    )

    # ---- exploratory hedge
    L.append("\n## Розвідувально: дельта-хедж спотом кожні 4 год (комісія 0.05 %, без funding; до витрат опціонів)\n")
    L.append(HDR)
    hw = {w: hedged_short_straddle(quotes[w + 3 * DAY_MS], prices, delivery[w + 5 * DAY_MS]) for w in P}
    hc = {w: hedged_short_straddle(quotes[w], prices, delivery[w + 2 * DAY_MS]) for w in P}
    hp = [w for w in P if hw[w] is not None and hc[w] is not None]
    L.append(mean_row("вихідні, хедж", [(w, hw[w]) for w in hp]))
    L.append(mean_row("контроль, хедж", [(w, hc[w]) for w in hp]))
    L.append(mean_row("Δ хеджованих", [(w, hw[w] - hc[w]) for w in hp]))
    L.append(
        f"SD тижневого результату: вихідні без хеджу {statistics.stdev(xw[w] for w in hp) * PCT:.2f} %, з хеджем {statistics.stdev(hw[w] for w in hp) * PCT:.2f} %."
    )

    # ---- capacity
    L.append(
        f"\nЄмність (описово): медіанний обсяг угод на ATM-страйку за 2 год до входу — вихідні {statistics.median(quotes[w + 3 * DAY_MS]['volume_K'] for w in P):.1f} BTC, "
        f"контроль {statistics.median(quotes[w]['volume_K'] for w in P):.1f} BTC; уся експірація — {statistics.median(quotes[w + 3 * DAY_MS]['volume'] for w in P):.0f} / {statistics.median(quotes[w]['volume'] for w in P):.0f} BTC."
    )

    # ---- verdict (mechanical, as registered)
    L.append("\n## Механічний вердикт за реєстрацією\n")
    L.append(
        f"- Ворота H0: {'пройдено' if s0['gate']['passed'] else 'не пройдено'}; без виключень: {'пройдено' if s0['gate_noexcl']['passed'] else 'не пройдено'}."
    )
    L.append(
        f"- Ворота покриття: {'пройдено' if gate_cov else 'не пройдено'}. Ворота потужності H2: {'пройдено' if gate_pw else 'не пройдено'}."
    )
    L.append(
        f"- H1: {v1} (без виключень {v1nx}). H2: {'✓' if h2_ok else ('✗' if gate_pw else '◯')}. H3: половини {'так' if h3_same else 'ні'}, без топ-3 {'так' if h3_top else 'ні'}."
    )
    bnx = block_bootstrap([nw(w) - nc(w) for w in Pnx], mean)
    h2_nx = gate_pw and mean([nw(w) for w in Pnx]) >= 0.002 and bnx["stat"] >= 0.002 and bnx["ci95"][0] > 0
    flip = v1 != v1nx or s0["gate"]["passed"] != s0["gate_noexcl"]["passed"] or (gate_pw and h2_ok != h2_nx)
    if not gate_pw:
        status = {"✓": "confirmed (ціноутворення)", "✗": "rejected", "◯": "inconclusive"}[v1]
    else:
        status = "confirmed" if (v1 == "✓" and h2_ok and h3_same and h3_top) else "rejected"
    if flip:
        status = "inconclusive (варіант без виключень за неперевіреними датами змінює ✓/✗/◯)"
    L.append(f"- Статус за реєстрацією: **{status}**.")
    if v1 == "◯":
        L.append(f"- H1 ◯, бо n = {len(P)} < 200 (виключення контрольних тижнів з подіями Tue/Wed/Thu лишають ≤ {len(prim_all)} тижнів).")


# ----------------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("fetch", "stage0", "gates", "full"), required=True)
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/weekend_vol")
    ap.add_argument("--trades-dir", default="~/.cache/tradedesk-agents/put_skew/trades24")
    ap.add_argument("--aux-dir", default="~/.cache/tradedesk-agents/put_skew", help="delivery_btc_usd.json")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "fetch":
        asyncio.run(fetch(cache))
        return
    bars = load_bars(cache)
    prices = prices_from_bars(bars)
    ev_weeks, nfp_weeks = load_events()
    L: list[str] = ["# Weekend vol discount: сирий вивід `scripts/verify_weekend_vol.py`\n"]
    s0 = stage0(prices, bars, ev_weeks, nfp_weeks, L)
    if a.stage == "stage0":
        return finish(L, a.out)
    if not s0["gate"]["passed"]:
        L.append("\n**H0 не пройдено: за реєстрацією картку закрито (`rejected`), опціони не рахуються.**")
        return finish(L, a.out)
    aux = Path(a.aux_dir).expanduser()
    delivery = {
        ms(date.fromisoformat(r["date"])): float(r["delivery_price"]) for r in json.loads((aux / "delivery_btc_usd.json").read_text())
    }
    quotes = load_quotes(Path(a.trades_dir).expanduser(), cache, tuesdays(FIRST_TUE, LAST_TUE))
    stage1(s0, prices, bars, quotes, delivery, ev_weeks, nfp_weeks, a.stage == "gates", L)
    finish(L, a.out)


if __name__ == "__main__":
    main()
