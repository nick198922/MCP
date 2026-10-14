"""Dated-future basis vs realized perp funding (IDEA-20261013-dated-basis-vs-funding-forward).

Pre-registered in the idea card ("Попередня реєстрація"); primary: Deribit BTC quarterlies, entry 60 days before
expiry at 08:00 UTC, d = basis_ann - realized funding_ann, 95 % t-CI > 0 with n >= 12 and the sign held in both halves.

Data (all through the project's providers, cached per file under --cache-dir, resumable):
  * Deribit BTC/ETH-PERPETUAL hourly funding with index (``rt.deribit.funding_rate_history``, www);
  * Deribit delivery prices (``rt.deribit.delivery_prices``);
  * prints of quarterly futures and perps around 08:00 UTC (``rt.deribit_history.last_trades_by_instrument_and_time``);
  * Binance USDT-M quarterly 1h klines, index 1h klines (``rt.exchanges.public_get``) and BTCUSDT funding (ccxt).

Usage:
    uv run python scripts/verify_dated_basis.py --cache-dir ~/.cache/tradedesk-agents/dated_basis --sample
    uv run python scripts/verify_dated_basis.py --cache-dir ... --fetch-only
    uv run python scripts/verify_dated_basis.py --cache-dir ... --power-only
    uv run python scripts/verify_dated_basis.py --cache-dir ... --out report.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.dated_basis import (
    DAY_MS,
    HOUR_MS,
    FundingSeries,
    annualize,
    bar_close_at,
    basis_ann,
    binance_usdm_name,
    block_bootstrap_diff,
    bootstrap_ci,
    contract_record,
    coverage,
    deribit_name,
    mde,
    mean_ci,
    quarterly_expiries,
    ratio_from_prints,
    tercile_groups,
)
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

START_MS = int(datetime(2019, 3, 1, tzinfo=UTC).timestamp() * 1000)
LAST_EXPIRY_MS = int(datetime(2026, 9, 25, 8, tzinfo=UTC).timestamp() * 1000)
FETCH_END_MS = LAST_EXPIRY_MS + DAY_MS
HORIZONS = (60, 30, 90)
PRIMARY = 60
PRINT_WINDOW_MS = 6 * HOUR_MS  # fetched window; the rule uses the last hour, the 6h tail is the pre-registered fallback
H3_DTE = (30, 90)
LAST_YEAR_MS = int(datetime(2025, 10, 14, tzinfo=UTC).timestamp() * 1000)
REGIMES = {
    "входи 2021": (int(datetime(2021, 1, 1, tzinfo=UTC).timestamp() * 1000), int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)),
    "входи 2024-01…2025-03": (
        int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000),
        int(datetime(2025, 4, 1, tzinfo=UTC).timestamp() * 1000),
    ),
}


def ymd(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


def write_json(p: Path, obj: Any) -> None:
    tmp = p.with_suffix(p.suffix + ".part")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, p)


# ----------------------------------------------------------------------------- fetch


async def fetch_funding(rt: Any, cache: Path, cur: str) -> None:
    p = cache / f"funding_{cur}_perp.json"
    if p.exists():
        return
    recs: dict[int, list[float]] = {}
    a, t0 = START_MS, time.perf_counter()
    while a < FETCH_END_MS:
        b = min(a + 30 * DAY_MS, FETCH_END_MS)
        f = await rt.deribit.funding_rate_history(f"{cur}-PERPETUAL", a, b)
        for r in f.data or []:
            recs[int(r["timestamp"])] = [
                float(r.get("interest_1h") or 0.0),
                float(r.get("index_price") or 0.0),
                float(r.get("interest_8h") or 0.0),
            ]
        a = b
    write_json(p, sorted([k, *v] for k, v in recs.items()))
    print(f"  funding {cur}: {len(recs)} hourly records in {time.perf_counter() - t0:.0f}s", flush=True)


async def fetch_delivery(rt: Any, cache: Path, cur: str) -> None:
    p = cache / f"delivery_{cur}.json"
    if p.exists():
        return
    rows: list[dict[str, Any]] = []
    off = 0
    while True:
        f = await rt.deribit.delivery_prices(f"{cur.lower()}_usd", offset=off, count=1000)
        batch = f.data.get("data") or []
        rows += batch
        off += len(batch)
        if not batch or off >= int(f.data.get("records_total") or 0):
            break
    write_json(p, {r["date"]: float(r["delivery_price"]) for r in rows})
    print(f"  delivery {cur}: {len(rows)}", flush=True)


async def fetch_prints(rt: Any, cache: Path, inst: str, refs: list[int], sem: asyncio.Semaphore) -> int:
    """Prints of ``inst`` in [ref - 6h, ref] for each ref (freshest 100), cached in one file per instrument."""
    p = cache / "prints" / f"{inst}.json"
    got: dict[str, list[list[float]]] = json.loads(p.read_text()) if p.exists() else {}
    todo = [r for r in refs if str(r) not in got]
    if not todo:
        return 0

    async def one(ref: int) -> None:
        async with sem:
            try:
                f = await rt.deribit_history.last_trades_by_instrument_and_time(inst, ref - PRINT_WINDOW_MS, ref, count=100, sorting="desc")
                got[str(ref)] = [
                    [int(t["timestamp"]), float(t["price"]), float(t["index_price"])]
                    for t in f.data["trades"]
                    if t.get("price") and t.get("index_price")
                ]
            except ProviderError as e:
                print(f"  {inst} {ymd(ref)}: {e.reason} {e.detail}", flush=True)
                if e.reason == "data_unavailable":
                    got[str(ref)] = []

    await asyncio.gather(*(one(r) for r in todo))
    write_json(p, got)
    return len(todo)


def plan_prints(cur: str, daily: bool, first_funding_ms: int) -> dict[str, list[int]]:
    """Instrument -> reference times (08:00 UTC) whose prints are needed."""
    plan: dict[str, set[int]] = {}
    exps = quarterly_expiries(START_MS, LAST_EXPIRY_MS)
    perp = f"{cur}-PERPETUAL"
    for e in exps:
        name = deribit_name(cur, e)
        for h in HORIZONS:
            t0 = e - h * DAY_MS
            if t0 < first_funding_ms:
                continue
            plan.setdefault(name, set()).add(t0)
            plan.setdefault(perp, set()).update({t0, e})
        if daily:
            for dte in range(H3_DTE[0], H3_DTE[1] + 1):
                t = e - dte * DAY_MS
                if t >= first_funding_ms + 30 * DAY_MS:
                    plan.setdefault(name, set()).add(t)
    return {k: sorted(v) for k, v in plan.items()}


async def fetch_binance(rt: Any, cache: Path) -> None:
    p = cache / "binance_btc.json"
    blob: dict[str, Any] = json.loads(p.read_text()) if p.exists() else {"fut": {}, "index": {}}
    exps = quarterly_expiries(int(datetime(2021, 1, 1, tzinfo=UTC).timestamp() * 1000), LAST_EXPIRY_MS)
    for e in exps:
        sym = binance_usdm_name("BTC", e)
        for h in HORIZONS:
            t0 = e - h * DAY_MS
            key = f"{sym}|{t0}"
            if key in blob["fut"]:
                continue
            params = {"symbol": sym, "interval": "1h", "startTime": t0 - 3 * HOUR_MS, "endTime": t0, "limit": 5}
            try:
                blob["fut"][key] = (await rt.exchanges.public_get("binance", "fapiPublicGetKlines", params)).data
            except ProviderError as ex:
                blob["fut"][key] = []
                print(f"  binance {sym} {ymd(t0)}: {ex.reason}", flush=True)
            ip = {"pair": "BTCUSDT", "interval": "1h", "startTime": t0 - 3 * HOUR_MS, "endTime": t0, "limit": 5}
            try:
                blob["index"][str(t0)] = (await rt.exchanges.public_get("binance", "fapiPublicGetIndexPriceKlines", ip)).data
            except ProviderError as ex:
                blob["index"][str(t0)] = []
                print(f"  binance index {ymd(t0)}: {ex.reason}", flush=True)
    write_json(p, blob)
    q = cache / "binance_funding_btcusdt.json"
    if not q.exists():
        since = int(datetime(2020, 12, 1, tzinfo=UTC).timestamp() * 1000)
        f = await rt.exchanges.funding_rate_history_range("binanceusdm", "BTC/USDT:USDT", since, FETCH_END_MS, max_pages=200)
        rows = [[int(r["timestamp"]), float(r["fundingRate"])] for r in f.data["rows"] if r.get("fundingRate") is not None]
        write_json(q, {"rows": rows, "complete": f.data["complete"]})
        print(f"  binance funding: {len(rows)} rows, complete={f.data['complete']}", flush=True)


async def fetch_all(cache: Path, *, sample: bool) -> None:
    (cache / "prints").mkdir(parents=True, exist_ok=True)
    _, rt = build_server()
    sem = asyncio.Semaphore(4)
    try:
        t0 = time.perf_counter()
        if sample:
            # timing sample: one contract's daily window, before any long fetch
            e = quarterly_expiries(
                int(datetime(2022, 12, 1, tzinfo=UTC).timestamp() * 1000), int(datetime(2022, 12, 31, tzinfo=UTC).timestamp() * 1000)
            )[0]
            refs = [e - d * DAY_MS for d in range(H3_DTE[0], H3_DTE[1] + 1)]
            sdir = cache / "sample"
            (sdir / "prints").mkdir(parents=True, exist_ok=True)
            n = await fetch_prints(rt, sdir, deribit_name("BTC", e), refs, sem)
            dt = time.perf_counter() - t0
            print(f"sample: {n} print windows in {dt:.1f}s ({dt / max(n, 1):.2f}s each)", flush=True)
            return
        for cur in ("BTC", "ETH"):
            await fetch_funding(rt, cache, cur)
            await fetch_delivery(rt, cache, cur)
        for cur in ("BTC", "ETH"):
            first = json.loads((cache / f"funding_{cur}_perp.json").read_text())[0][0]
            plan = plan_prints(cur, daily=(cur == "BTC"), first_funding_ms=int(first))
            total = sum(len(v) for v in plan.values())
            print(f"{cur}: {len(plan)} instruments, {total} print windows (first funding {ymd(int(first))})", flush=True)
            done = 0
            for inst, refs in plan.items():
                n = await fetch_prints(rt, cache, inst, refs, sem)
                done += len(refs)
                if n:
                    print(f"  {inst}: +{n} windows ({done}/{total}, {time.perf_counter() - t0:.0f}s)", flush=True)
        await fetch_binance(rt, cache)
        print(f"fetch done in {time.perf_counter() - t0:.0f}s", flush=True)
    finally:
        await rt.aclose()


# ----------------------------------------------------------------------------- build records


def prints_of(cache: Path, inst: str) -> dict[int, list[dict[str, Any]]]:
    p = cache / "prints" / f"{inst}.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    return {int(k): [{"timestamp": t, "price": pr, "index_price": ix} for t, pr, ix in v] for k, v in raw.items()}


def ratio_rule(prints: list[dict[str, Any]], ref: int) -> dict[str, float] | None:
    """Pre-registered rule: last hour, else fallback to 6 hours."""
    return ratio_from_prints(prints, ref, window_ms=HOUR_MS) or ratio_from_prints(prints, ref, window_ms=PRINT_WINDOW_MS)


class Data:
    def __init__(self, cache: Path, cur: str) -> None:
        raw = json.loads((cache / f"funding_{cur}_perp.json").read_text())
        self.fs = FundingSeries([(int(r[0]), r[1], r[2]) for r in raw])
        self.raw_funding = raw
        self.delivery: dict[str, float] = json.loads((cache / f"delivery_{cur}.json").read_text())
        self.cur = cur
        self.cache = cache
        self.perp = prints_of(cache, f"{cur}-PERPETUAL")
        self._fut: dict[str, dict[int, list[dict[str, Any]]]] = {}

    def fut(self, name: str) -> dict[int, list[dict[str, Any]]]:
        if name not in self._fut:
            self._fut[name] = prints_of(self.cache, name)
        return self._fut[name]


def build_records(dat: Data, horizon: int, *, log: bool = False, cost_mult: float = 1.0) -> tuple[list[dict[str, Any]], list[str]]:
    recs, excluded = [], []
    first = dat.fs.first_ts() or 0
    for e in quarterly_expiries(START_MS, LAST_EXPIRY_MS):
        t0 = e - horizon * DAY_MS
        if t0 < first:
            continue
        name = deribit_name(dat.cur, e)
        fr = ratio_rule(dat.fut(name).get(t0, []), t0)
        if fr is None:
            excluded.append(f"{name}: нема угод у [t0 − 6 год, t0]")
            continue
        cov = coverage(dat.fs, t0, e, HOUR_MS)
        if cov < 0.95:
            excluded.append(f"{name}: покриття funding {cov:.0%}")
            continue
        pr0 = ratio_rule(dat.perp.get(t0, []), t0)
        pre = ratio_rule(dat.perp.get(e, []), e)
        rec = contract_record(
            expiry_ms=e,
            days=horizon,
            fut_ratio=fr["ratio"],
            funding=dat.fs,
            index0=fr["index"],
            perp_ratio0=pr0["ratio"] if pr0 else None,
            perp_ratio_exit=pre["ratio"] if pre else None,
            index_exit=pre["index"] if pre else None,
            delivery=dat.delivery.get(ymd(e)),
            log=log,
            cal_cost=0.00175 * cost_mult,
            cc_cost=0.00275 * cost_mult,
            perp_cc_cost=0.003 * cost_mult,
        )
        rec.update(name=name, n_prints=fr["n"], age_min=fr["age_ms"] / 60000, coverage=cov, fallback=fr["age_ms"] > HOUR_MS)
        recs.append(rec)
    return recs, excluded


def binance_records(cache: Path, horizon: int) -> tuple[list[dict[str, Any]], list[str]]:
    blob = json.loads((cache / "binance_btc.json").read_text())
    fund = json.loads((cache / "binance_funding_btcusdt.json").read_text())
    fs = FundingSeries([(int(t), r) for t, r in fund["rows"]])
    out, excl = [], []
    for e in quarterly_expiries(int(datetime(2021, 1, 1, tzinfo=UTC).timestamp() * 1000), LAST_EXPIRY_MS):
        sym = binance_usdm_name("BTC", e)
        t0 = e - horizon * DAY_MS
        fut = bar_close_at(blob["fut"].get(f"{sym}|{t0}", []), t0)
        idx = bar_close_at(blob["index"].get(str(t0), []), t0)
        if not fut or not idx:
            excl.append(f"{sym}: нема 1h-свічки на t0")
            continue
        if fs.first_ts() is None or t0 < fs.first_ts():
            excl.append(f"{sym}: нема funding")
            continue
        cov = coverage(fs, t0, e, 8 * HOUR_MS)
        if cov < 0.95:
            excl.append(f"{sym}: покриття funding {cov:.0%}")
            continue
        r = contract_record(expiry_ms=e, days=horizon, fut_ratio=fut / idx, funding=fs)
        r.update(name=sym, coverage=cov)
        out.append(r)
    return out, excl


# ----------------------------------------------------------------------------- report helpers


def pp(x: float | None, k: float = 100.0, sign: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x * k:+.2f}" if sign else f"{x * k:.2f}"


def summary_row(label: str, xs: list[float], *, boot: bool = False) -> str:
    s = mean_ci(xs)
    if not s.get("n"):
        return f"| {label} | 0 | — | — | — | — | — |"
    ci = s["ci"]
    ci_s = f"[{pp(ci[0])}; {pp(ci[1])}]" if ci else "—"
    b = bootstrap_ci(xs) if boot else None
    b_s = f"[{pp(b[0])}; {pp(b[1])}]" if b else "—"
    return f"| {label} | {s['n']} | {pp(s['mean'])} | {ci_s} | {b_s} | {pp(s['median'])} | {s['share_pos']:.0%} |"


HDR = "| Вибірка | n | середнє, п.п. | 95% t-CI | бутстреп CI | медіана | частка > 0 |\n|---|---|---|---|---|---|---|"


def halves(recs: list[dict[str, Any]], key: str) -> tuple[list[float], list[float]]:
    xs = [r[key] for r in sorted(recs, key=lambda r: r["expiry"]) if r.get(key) is not None]
    h = len(xs) // 2
    return xs[:h], xs[h:]


def h3_points(dat: Data) -> list[dict[str, Any]]:
    pts = []
    first = dat.fs.first_ts() or 0
    for e in quarterly_expiries(START_MS, LAST_EXPIRY_MS):
        fp = dat.fut(deribit_name(dat.cur, e))
        for dte in range(H3_DTE[0], H3_DTE[1] + 1):
            t = e - dte * DAY_MS
            if t < first + 30 * DAY_MS:
                continue
            fr = ratio_rule(fp.get(t, []), t)
            if fr is None or coverage(dat.fs, t, e, HOUR_MS) < 0.95:
                continue
            b = basis_ann(fr["ratio"], dte)
            fwd = annualize(dat.fs.total(t, e), dte)
            trail = annualize(dat.fs.total(t - 30 * DAY_MS, t), 30)
            pts.append({"t": t, "e": e, "d": b - fwd, "s": b - trail, "basis_ann": b, "trail": trail})
    return pts


# ----------------------------------------------------------------------------- main


def power_only(cache: Path) -> None:
    dat = Data(cache, "BTC")
    recs, excl = build_records(dat, PRIMARY)
    ds = [r["d"] for r in recs]
    s = mean_ci(ds)
    print(f"power gate (primary: Deribit BTC, {PRIMARY}d): n={s['n']}, excluded={len(excl)}")
    if s["n"] >= 3:
        print(f"  SD(d) = {s['sd'] * 100:.2f} pp annualised; MDE (alpha 0.05 two-sided, power 0.80) = {mde(s['sd'], s['n']) * 100:.2f} pp")
    for x in excl:
        print("  excluded:", x)


def report(cache: Path) -> str:
    L: list[str] = []
    dat = Data(cache, "BTC")
    eth = Data(cache, "ETH")
    first = dat.fs.first_ts() or 0
    L.append("# Датований базис проти реалізованого funding (Deribit BTC) — сирий вивід\n")
    L.append(
        f"Funding BTC-PERPETUAL: {len(dat.raw_funding)} год, з {ymd(first)}; ETH-PERPETUAL: {len(eth.raw_funding)} год, "
        f"з {ymd(eth.fs.first_ts() or 0)}. Delivery prices: BTC {len(dat.delivery)}, ETH {len(eth.delivery)}.\n"
    )
    # --- sanity
    L.append("## Перевірки даних\n")
    r1 = [r[1] for r in dat.raw_funding]
    r8 = [r[3] for r in dat.raw_funding]
    zero = sum(1 for x in r1 if abs(x) < 1e-9) / len(r1)
    L.append(
        f"- Deribit funding: середнє `interest_1h` × 24 × 365 = {statistics.fmean(r1) * 24 * 365 * 100:+.2f} % річних за весь період; "
        f"частка годин з |interest_1h| < 1e-9: {zero:.1%}; середнє `interest_8h`/8 × 24 × 365 = {statistics.fmean(r8) / 8 * 24 * 365 * 100:+.2f} %."
    )
    by_year: dict[int, list[float]] = {}
    for r in dat.raw_funding:
        by_year.setdefault(datetime.fromtimestamp(r[0] / 1000, UTC).year, []).append(r[1])
    L.append(
        "- Funding BTC-PERPETUAL по роках (річних, сума/години × 8760): "
        + ", ".join(f"{y}: {sum(v) / len(v) * 8760 * 100:+.1f} % ({len(v)} год)" for y, v in sorted(by_year.items()))
    )
    gaps = [
        (dat.raw_funding[i - 1][0], dat.raw_funding[i][0])
        for i in range(1, len(dat.raw_funding))
        if dat.raw_funding[i][0] - dat.raw_funding[i - 1][0] > 2 * HOUR_MS
    ]
    L.append(
        f"- Пропуски в погодинному funding > 2 год: {len(gaps)}"
        + (
            f"; найбільші: {', '.join(f'{ymd(a)}→{ymd(b)} ({(b - a) / HOUR_MS:.0f} год)' for a, b in sorted(gaps, key=lambda g: g[0] - g[1])[:5])}"
            if gaps
            else ""
        )
    )
    bfund = json.loads((cache / "binance_funding_btcusdt.json").read_text())
    br = [r for _, r in bfund["rows"]]
    L.append(
        f"- Binance BTCUSDT funding: {len(br)} записів, complete={bfund['complete']}, середнє × 3 × 365 = {statistics.fmean(br) * 3 * 365 * 100:+.2f} %; "
        f"частка записів рівно 0.0001: {sum(1 for x in br if abs(x - 0.0001) < 1e-10) / len(br):.0%}."
    )
    recs, excl = build_records(dat, PRIMARY)
    # delivery vs index of the last print before expiry, and TV cross-check of a known contract
    noise = [r["convergence_noise"] for r in recs if r.get("convergence_noise") is not None]
    if noise:
        L.append(
            f"- Шум конвергенції (індекс останньої угоди перпа ≤ 08:00 − delivery price, % від I0): медіана {statistics.median(noise) * 100:+.3f}, "
            f"медіана |·| {statistics.median(abs(x) for x in noise) * 100:.3f}, max |·| {max(abs(x) for x in noise) * 100:.3f}, n = {len(noise)}."
        )
    k = next((r for r in recs if r["name"] == "BTC-30DEC22"), None)
    if k:
        L.append(
            f"- Відомий контракт BTC-30DEC22: delivery price {dat.delivery.get('2022-12-30')}; останнім угодам перед експірацією (крок 0) "
            f"відповідали price 16472.0 / index 16471.84. Базис на вході (60д) {k['basis'] * 100:+.3f} % ({k['basis_ann'] * 100:+.2f} % річних)."
        )
    L.append(
        f"- Вхідні угоди (основна вибірка): медіана кількості {statistics.median(r['n_prints'] for r in recs):.0f}, "
        f"вік останньої угоди медіана {statistics.median(r['age_min'] for r in recs):.1f} хв, max {max(r['age_min'] for r in recs):.0f} хв; "
        f"резервне вікно 6 год використано {sum(r['fallback'] for r in recs)} раз."
    )
    L.append(f"- Виключено з основної вибірки: {len(excl)}" + (": " + "; ".join(excl) if excl else ""))
    # --- power
    ds = [r["d"] for r in recs]
    s = mean_ci(ds)
    m = mde(s["sd"], s["n"]) if s.get("sd") else float("inf")
    L.append("\n## Ворота потужності (зареєстровано: MDE ≤ 3 п.п. — інакше лише опис)\n")
    L.append(
        f"n = {s['n']}, SD(d) = {s['sd'] * 100:.2f} п.п., MDE = {m * 100:.2f} п.п. річних → {'пройдено' if m <= 0.03 else 'НЕ пройдено'}.\n"
    )
    # --- primary table
    L.append(f"## H1, основна: Deribit BTC, вхід за {PRIMARY} днів, d = basis_ann − funding_ann\n")
    L.append(
        "| Контракт | вхід | базис, % | базис річн. | funding річн. | d, п.п. | календар річн. (після 0.175 %) | dated-cc річн. | perp-cc річн. |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in recs:
        L.append(
            f"| {r['name']} | {ymd(r['t0'])} | {pp(r['basis'])} | {pp(r['basis_ann'])} | {pp(r['funding_ann'])} | {pp(r['d'])} | "
            f"{pp(r['calendar_ann'])} | {pp(r['dated_cc_ann'])} | {pp(r['perp_cc_ann'])} |"
        )
    L.append("\n" + HDR)
    L.append(summary_row("**d (основна)**", ds, boot=True))
    a, b = halves(recs, "d")
    L.append(summary_row("d, перша половина", a))
    L.append(summary_row("d, друга половина", b))
    top = sorted(ds, reverse=True)[3:]
    L.append(summary_row("d без 3 найбільших", top))
    for lab, (lo, hi) in REGIMES.items():
        L.append(summary_row(f"d, {lab}", [r["d"] for r in recs if lo <= r["t0"] < hi]))
    L.append(summary_row("d, останній рік (E ≥ 2025-10-14)", [r["d"] for r in recs if r["expiry"] >= LAST_YEAR_MS]))
    L.append(summary_row("базис річних", [r["basis_ann"] for r in recs]))
    L.append(summary_row("funding річних", [r["funding_ann"] for r in recs]))
    # --- books
    L.append("\n## Книги (річних, на номінал = капітал для cash-and-carry; після витрат)\n")
    L.append(HDR)
    for mult, lab in ((1.0, "основні витрати"), (2.0, "витрати ×2"), (0.0, "без витрат")):
        rr, _ = build_records(dat, PRIMARY, cost_mult=mult)
        L.append(
            summary_row(
                f"календар (шорт дат. + лонг перп), {lab}",
                [r["calendar_ann"] for r in rr if r["calendar_ann"] is not None],
                boot=mult == 1.0,
            )
        )
        L.append(summary_row(f"H2: dated cash-and-carry, {lab}", [r["dated_cc_ann"] for r in rr], boot=mult == 1.0))
        L.append(summary_row(f"perp cash-and-carry, {lab}", [r["perp_cc_ann"] for r in rr if r["perp_cc_ann"] is not None]))
        L.append(
            summary_row(
                f"H2: dated − perp carry, {lab}",
                [r["dated_cc_ann"] - r["perp_cc_ann"] for r in rr if r["perp_cc_ann"] is not None],
            )
        )
    cal_vs_d = [(r["calendar_ann"] or 0) - r["d"] for r in recs if r["calendar_ann"] is not None]
    if cal_vs_d:
        L.append(
            f"\nКалендар − d (премія перпа на вході/виході, вага funding за ціною, витрати): медіана {statistics.median(cal_vs_d) * 100:+.2f} п.п., "
            f"середнє {statistics.fmean(cal_vs_d) * 100:+.2f}."
        )
    dec = [r for r in recs if r.get("perp_premium0") is not None]
    if dec:
        an = lambda x, r: annualize(x, r["days"])  # noqa: E731
        L.append("\nРозклад «календар − d» (середнє, п.п. річних; розвідувально, після перегляду результатів):")
        L.append(f"- − премія перпа на вході: {statistics.fmean(-an(r['perp_premium0'], r) for r in dec) * 100:+.2f}")
        L.append(f"- + (перп на виході − delivery price): {statistics.fmean(an(r['exit_gap'], r) for r in dec) * 100:+.2f}")
        L.append(
            f"- − (funding зважений ціною − незважений): {statistics.fmean(-an(r['funding_weighted'] - r['funding'], r) for r in dec) * 100:+.2f}"
        )
        L.append(f"- − витрати 0.175 %: {-annualize(0.00175, PRIMARY) * 100:+.2f}")
        worst = max(dec, key=lambda r: abs(r["convergence_noise"]))
        L.append(
            f"- найбільший шум конвергенції: {worst['name']} {worst['convergence_noise'] * 100:+.2f} % I0; найбільша |exit_gap|: "
            f"{max(dec, key=lambda r: abs(r['exit_gap']))['name']} {max(abs(r['exit_gap']) for r in dec) * 100:.2f} % I0"
        )
    ly = [r["dated_cc_ann"] for r in recs if r["expiry"] >= LAST_YEAR_MS]
    if ly:
        L.append(
            f"\nH2 dated carry, останній рік (E ≥ 2025-10-14, розвідувально): середнє {statistics.fmean(ly) * 100:+.2f} п.п., n = {len(ly)}."
        )
    a, b = halves(recs, "dated_cc_ann")
    L.append(
        f"\nH2 dated carry по половинах: {pp(statistics.fmean(a))} / {pp(statistics.fmean(b))} п.п.; частка контрактів ≥ 4 % річних: {sum(1 for r in recs if r['dated_cc_ann'] >= 0.04) / len(recs):.0%}."
    )
    # --- variants
    L.append("\n## Сітка варіантів d (усе, крім першого рядка, — реплікація або розвідувально)\n")
    L.append(HDR)
    L.append(summary_row(f"Deribit BTC {PRIMARY}д (основна)", ds))
    for h in (30, 90):
        rr, ex = build_records(dat, h)
        L.append(summary_row(f"Deribit BTC {h}д" + (" (перекриття)" if h == 90 else ""), [r["d"] for r in rr]))
    rr, _ = build_records(dat, PRIMARY, log=True)
    L.append(summary_row(f"Deribit BTC {PRIMARY}д, лог-базис", [r["d"] for r in rr]))
    for h in HORIZONS:
        rr, ex = build_records(eth, h)
        L.append(summary_row(f"Deribit ETH {h}д", [r["d"] for r in rr]))
        if h == PRIMARY:
            ea, eb = halves(rr, "d")
            L.append(summary_row("  ETH 60д перша половина", ea))
            L.append(summary_row("  ETH 60д друга половина", eb))
            eth_excl = ex
    bin_by_h = {}
    for h in HORIZONS:
        rr, ex = binance_records(cache, h)
        bin_by_h[h] = (rr, ex)
        L.append(summary_row(f"Binance USDT-M BTC {h}д", [r["d"] for r in rr]))
    L.append(f"\nETH 60д виключено: {len(eth_excl)}" + (": " + "; ".join(eth_excl) if eth_excl else ""))
    rr, ex = bin_by_h[PRIMARY]
    L.append(f"Binance 60д виключено: {len(ex)}" + (": " + "; ".join(ex) if ex else ""))
    # same-contract comparison Deribit vs Binance
    db = {ymd(r["expiry"]): r for r in recs}
    pairs = [(db[ymd(r["expiry"])], r) for r in rr if ymd(r["expiry"]) in db]
    if pairs:
        L.append(
            f"\nТі самі експірації, Deribit проти Binance (60д, n = {len(pairs)}): базис річн. {statistics.fmean(x['basis_ann'] for x, _ in pairs) * 100:+.2f} "
            f"проти {statistics.fmean(y['basis_ann'] for _, y in pairs) * 100:+.2f}; funding річн. {statistics.fmean(x['funding_ann'] for x, _ in pairs) * 100:+.2f} "
            f"проти {statistics.fmean(y['funding_ann'] for _, y in pairs) * 100:+.2f}; d {statistics.fmean(x['d'] for x, _ in pairs) * 100:+.2f} "
            f"проти {statistics.fmean(y['d'] for _, y in pairs) * 100:+.2f} п.п."
        )
        L.append(
            "\n| Експірація | Deribit базис | Binance базис | Deribit funding | Binance funding | Deribit d | Binance d |\n|---|---|---|---|---|---|---|"
        )
        for x, y in pairs:
            L.append(
                f"| {ymd(x['expiry'])} | {pp(x['basis_ann'])} | {pp(y['basis_ann'])} | {pp(x['funding_ann'])} | {pp(y['funding_ann'])} | {pp(x['d'])} | {pp(y['d'])} |"
            )
    # --- H3
    pts = h3_points(dat)
    L.append("\n## H3 (розвідувально): сигнал basis − trailing 30д funding, щоденні точки, DTE ∈ [30; 90]\n")
    if pts:
        g = tercile_groups([p["s"] for p in pts])
        days = [p["t"] // DAY_MS for p in pts]
        res = block_bootstrap_diff(days, [p["d"] for p in pts], g, block_days=90)
        hi = [p["d"] for p, gg in zip(pts, g, strict=True) if gg == 1]
        lo = [p["d"] for p, gg in zip(pts, g, strict=True) if gg == 0]
        L.append(
            f"n = {len(pts)} днів, {res['n_blocks']} блоків по 90 днів. Середнє d: верхній терциль {statistics.fmean(hi) * 100:+.2f}, "
            f"нижній {statistics.fmean(lo) * 100:+.2f}; різниця {res['diff'] * 100:+.2f} п.п., 95% CI [{res['ci'][0] * 100:+.2f}; {res['ci'][1] * 100:+.2f}]."
        )
        mid = sorted(p["t"] for p in pts)[len(pts) // 2]
        for lab, sel in (("перша половина", lambda p: p["t"] < mid), ("друга половина", lambda p: p["t"] >= mid)):
            sub = [(p, gg) for p, gg in zip(pts, g, strict=True) if sel(p)]
            h_ = [p["d"] for p, gg in sub if gg == 1]
            l_ = [p["d"] for p, gg in sub if gg == 0]
            if h_ and l_:
                L.append(
                    f"- {lab}: верхній {statistics.fmean(h_) * 100:+.2f} (n={len(h_)}), нижній {statistics.fmean(l_) * 100:+.2f} (n={len(l_)}), різниця {(statistics.fmean(h_) - statistics.fmean(l_)) * 100:+.2f}"
                )
        allres = block_bootstrap_diff(days, [p["d"] for p in pts], [1] * len(pts), block_days=90)
        L.append(
            f"- Середнє d по всіх щоденних точках: {statistics.fmean(p['d'] for p in pts) * 100:+.2f} п.п. (перекриваються; довідка). {allres['n_blocks']} блоків."
        )
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--sample", action="store_true", help="time one contract's daily print windows and stop")
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--power-only", action="store_true", help="print n, SD(d) and MDE only (no means)")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.sample:
        asyncio.run(fetch_all(cache, sample=True))
        return
    if not a.no_fetch:
        asyncio.run(fetch_all(cache, sample=False))
    if a.fetch_only:
        return
    if a.power_only:
        power_only(cache)
        return
    text = report(cache)
    print(text)
    if a.out:
        Path(a.out).expanduser().write_text(text)


if __name__ == "__main__":
    main()
