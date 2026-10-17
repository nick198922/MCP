"""Regimes of BTC/ETH correlation with Nasdaq, S&P 500 (SPY), the dollar (UUP), 2Y/10Y yields (IDEA-20261017-macro, B1).

The pre-registration is the card section "Попередня реєстрація (B1: режими кореляції)" in
``docs/ideas/IDEA-20261017-macro.md``. Network only through ``core.http.Http`` (Nasdaq, Treasury, Fed) and the runtime's
``rt.binance_vision`` (crypto klines); every response is written to the per-file cache under ``--cache-dir`` as it
arrives (default ``~/.cache/tradedesk-agents/macro_regimes``).

    uv run python scripts/verify_macro_regimes.py --stage prereg   # hash of the registration -> registration.txt
    uv run python scripts/verify_macro_regimes.py --stage fetch    # Nasdaq COMP/SPY/UUP, Treasury CSVs, Fed table, klines
    uv run python scripts/verify_macro_regimes.py --stage sanity   # levels, counts, gaps, open-field check (no results)
    uv run python scripts/verify_macro_regimes.py --stage report --out raw.md
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import resource
import shutil
import statistics
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.macro_regimes import (
    HOUR_MS,
    aligned_changes,
    beta,
    complete_rows,
    episodes,
    fed_phase,
    h3_verdict,
    moving_block_bootstrap,
    ny_close_ms,
    ny_time_ms,
    ols_r2,
    parse_fed_changes,
    parse_nasdaq_historical,
    parse_treasury_csv,
    pearson,
    rolling,
    rolling_corr,
    sign_rule,
)

ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "docs/ideas/IDEA-20261017-macro.md"
SECTION = "## Попередня реєстрація (B1: режими кореляції)"
FIRST_DAY = date(2017, 12, 29)  # first pair ends 2018-01-02
LAST_DAY = date(2026, 10, 16)
SPLIT = date(2022, 1, 1)
HALF2 = date(2024, 1, 1)
CRYPTO = ("BTC", "ETH")
MACRO = ("COMP", "SPY", "UUP", "D2Y", "D10Y")
NASDAQ = {"COMP": "index", "SPY": "etf", "UUP": "etf"}
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}
CLAUDE_YEARLY = {
    "2018": 0.07,
    "2019": -0.09,
    "2020": 0.41,
    "2021": 0.24,
    "2022": 0.61,
    "2023": 0.23,
    "2024": 0.36,
    "2025": 0.49,
    "2026": 0.49,
}
REPS = 2000
SEED = 20261018
COST = 0.0005
L: list[str] = []


def emit(s: str = "") -> None:
    L.append(s)
    print(s, flush=True)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


# ----------------------------------------------------------------------------- prereg


def stage_prereg(cache: Path) -> None:
    raw = CARD.read_bytes()
    t = raw.decode()
    a = t.index(SECTION)
    b = t.index("\n## ", a + 1)
    sec = t[a:b].encode()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()  # = git hash-object
    line = f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec).hexdigest()} bytes={len(sec)} card_blob={blob}"
    emit(line)
    with (cache / "registration.txt").open("a") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------------- fetch


def _write(p: Path, data: bytes | str) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data)
    else:
        tmp.write_bytes(data)
    tmp.replace(p)


async def fetch(cache: Path, other: Path) -> None:
    from tradedesk.core.http import Http
    from tradedesk.providers.binance_vision import parse_klines_zip
    from tradedesk.server import build_server

    nas = Http("nasdaq", "https://api.nasdaq.com", rate=0.3, burst=1, timeout=60, retries=2, headers=UA)
    tre = Http("treasury", "https://home.treasury.gov", rate=0.3, burst=1, timeout=60, retries=2, headers=UA)
    fed = Http("fed", "https://www.federalreserve.gov", rate=0.3, burst=1, timeout=60, retries=2, headers=UA)
    try:
        for sym, ac in NASDAQ.items():
            p = cache / f"nasdaq_{sym}.json"
            if p.exists():
                continue
            params = {"assetclass": ac, "fromdate": "2017-12-01", "todate": LAST_DAY.isoformat(), "limit": "9999"}
            f = await nas.get_json(f"/api/quote/{sym}/historical", params=params)
            _write(p, json.dumps(f.data))
            n = len(parse_nasdaq_historical(f.data))
            print(f"nasdaq {sym}: {n} rows  rss={rss_mb():.0f}MB", flush=True)
        for yr in range(2017, LAST_DAY.year + 1):
            p = cache / f"treasury_{yr}.csv"
            if p.exists() and yr != LAST_DAY.year:
                continue
            path = f"/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{yr}/all"
            params = {"type": "daily_treasury_yield_curve", "field_tdr_date_value": str(yr), "page": "", "_format": "csv"}
            f = await tre.get_bytes(path, params=params)
            _write(p, f.data)
            print(f"treasury {yr}: {len(parse_treasury_csv(f.data.decode('utf-8-sig')))} rows", flush=True)
        p = cache / "fed_openmarket.htm"
        if not p.exists():
            f = await fed.get_bytes("/monetarypolicy/openmarket.htm")
            _write(p, f.data)
        print(f"fed changes: {len(parse_fed_changes(p.read_bytes().decode('utf-8-sig')))}", flush=True)
    finally:
        await nas.aclose()
        await tre.aclose()
        await fed.aclose()

    kdir = cache / "klines"
    jobs: list[tuple[Path, str]] = []
    for asset in CRYPTO:
        sym = f"{asset}USDT"
        for y in range(2017, 2027):
            for m in range(1, 13):
                if (y, m) < (2017, 12) or (y, m) > (2026, 9):
                    continue
                name = f"{sym}-1h-{y:04d}-{m:02d}"
                jobs.append((kdir / f"{name}.json", f"/data/spot/monthly/klines/{sym}/1h/{name}.zip"))
        for d in range(1, LAST_DAY.day + 1):
            name = f"{sym}-1h-2026-10-{d:02d}"
            jobs.append((kdir / f"{name}.json", f"/data/spot/daily/klines/{sym}/1h/{name}.zip"))
    for p, _ in jobs:
        if not p.exists() and (other / p.name).exists():
            shutil.copyfile(other / p.name, p)
    todo = [j for j in jobs if not j[0].exists()]
    print(f"klines: {len(jobs)} files, {len(todo)} to fetch", flush=True)
    if not todo:
        return
    _, rt = build_server()
    t0 = time.perf_counter()
    try:
        for i, (p, url) in enumerate(todo):
            for k in range(4):
                try:
                    f = await rt.binance_vision.http.get_bytes(url, tool="spot_klines")
                    rows = parse_klines_zip(f.data)
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"  retry {url} after {type(e).__name__}: {str(e)[:120]}", flush=True)
                    await asyncio.sleep(5 * (k + 1))
            else:
                raise RuntimeError(f"giving up on {url}")
            _write(p, json.dumps(rows))
            if i == 1:
                el = time.perf_counter() - t0
                print(f"  sample: 2 files in {el:.1f}s -> estimate {el / 2 * len(todo):.0f}s for all", flush=True)
            if i % 10 == 0 or i == len(todo) - 1:
                print(f"  {i + 1}/{len(todo)} {p.name} rows={len(rows)} {time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB", flush=True)
            await asyncio.sleep(0.2)
    finally:
        await rt.aclose()
    print("fetch done", flush=True)


# ----------------------------------------------------------------------------- load


class Data:
    def __init__(self, cache: Path) -> None:
        self.nas = {s: parse_nasdaq_historical(json.loads((cache / f"nasdaq_{s}.json").read_text())) for s in NASDAQ}
        self.days = sorted(d for d in self.nas["COMP"] if FIRST_DAY <= d <= LAST_DAY)
        ylds: dict[date, tuple[float | None, ...]] = {}
        for p in sorted(cache.glob("treasury_*.csv")):
            ylds.update(parse_treasury_csv(p.read_bytes().decode("utf-8-sig")))
        self.y2 = {d: v[0] for d, v in ylds.items() if v[0] is not None}
        self.y10 = {d: v[1] for d, v in ylds.items() if v[1] is not None}
        self.fed = parse_fed_changes((cache / "fed_openmarket.htm").read_bytes().decode("utf-8-sig"))
        # crypto: only the hours we need (NY close of every day, 09:00 ET of every day), one file at a time
        need_close = {ny_close_ms(d): d for d in self.days}
        need_open = {ny_time_ms(d, 9): d for d in self.days}
        need_utc0 = {int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000) + 86_400_000: d for d in self.days}
        self.close: dict[str, dict[date, float]] = {a: {} for a in CRYPTO}
        self.pre_open: dict[str, dict[date, float]] = {a: {} for a in CRYPTO}
        self.utc_close: dict[str, dict[date, float]] = {a: {} for a in CRYPTO}
        self.hours: dict[str, int] = {}
        for a in CRYPTO:
            nh = 0
            for p in sorted((cache / "klines").glob(f"{a}USDT-1h-*.json")):
                for r in json.loads(p.read_text()):
                    nh += 1
                    t_end = int(r[0]) + HOUR_MS
                    if t_end in need_close:
                        self.close[a][need_close[t_end]] = float(r[4])
                    if t_end in need_open:
                        self.pre_open[a][need_open[t_end]] = float(r[4])
                    if t_end in need_utc0:
                        self.utc_close[a][need_utc0[t_end]] = float(r[4])
            self.hours[a] = nh
        d = self.days
        self.gap = [(b - a).days for a, b in zip(d[:-1], d[1:], strict=True)]
        self.end = d[1:]
        self.r: dict[str, list[float | None]] = {
            "COMP": aligned_changes(d, {k: v[1] for k, v in self.nas["COMP"].items()}),
            "SPY": aligned_changes(d, {k: v[1] for k, v in self.nas["SPY"].items()}),
            "UUP": aligned_changes(d, {k: v[1] for k, v in self.nas["UUP"].items()}),
            "D2Y": aligned_changes(d, self.y2, kind="diff"),
            "D10Y": aligned_changes(d, self.y10, kind="diff"),
        }
        for a in CRYPTO:
            self.r[a] = aligned_changes(d, self.close[a])
        self.n = len(self.end)


# ----------------------------------------------------------------------------- formatting / stats helpers


def f(x: float | None, k: float = 1.0, d: int = 2) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * k:+.{d}f}"


def ci(c: tuple[float, float] | None, k: float = 1.0, d: int = 2) -> str:
    return f"[{c[0] * k:+.{d}f}; {c[1] * k:+.{d}f}]" if c else "—"


def boot(n: int, stat: Callable[[np.ndarray], float]) -> dict[str, Any]:
    return moving_block_bootstrap(n, stat, block=20, reps=REPS, seed=SEED)


def arr(col: Sequence[float | None], idx: np.ndarray) -> np.ndarray:
    return np.array([col[i] for i in idx], float)


def corr_boot(x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    return boot(len(x), lambda i: pearson(x[i], y[i]))


def beta_boot(y: np.ndarray, x: np.ndarray) -> dict[str, Any]:
    return boot(len(x), lambda i: beta(y[i], x[i]))


def rows_where(D: Data, cols: Sequence[str], pred: Callable[[int], bool] = lambda i: True) -> np.ndarray:
    keep = complete_rows(*[D.r[c] for c in cols])
    return np.array([i for i in keep if pred(int(i))], dtype=int)


def year_of(D: Data, i: int) -> str:
    return str(D.end[i].year)


YEARS = [str(y) for y in range(2018, 2027)]


# ----------------------------------------------------------------------------- sanity (no results)


def stage_sanity(D: Data) -> None:
    emit("## Санітарні перевірки")
    emit(f"- торгових днів COMP у вікні: {len(D.days)} ({D.days[0]} … {D.days[-1]}), пар: {D.n}; peak RSS {rss_mb():.0f} MB")
    cnt = Counter(d.year for d in D.days)
    emit("- днів за роками: " + ", ".join(f"{y}: {cnt[y]}" for y in sorted(cnt)))
    emit(f"- дублі дат: {len(D.days) - len(set(D.days))}; розриви між торговими днями (діб): {dict(sorted(Counter(D.gap).items()))}")
    c = D.nas["COMP"].get(date(2021, 11, 19))
    emit(f"- COMP 2021-11-19 close = {c[1] if c else None} (очікуємо ≈ 16 057)")
    emit(f"- 10Y 2020-08-04 = {D.y10.get(date(2020, 8, 4))} (очікуємо ≈ 0.0052); 2Y 2026-10-16 = {D.y2.get(date(2026, 10, 16))}")
    emit(f"- SPY 2018-01-02 / 2026-10-16: {D.nas['SPY'].get(date(2018, 1, 2))} / {D.nas['SPY'].get(LAST_DAY)}")
    emit(f"- UUP 2018-01-02 / 2026-10-16: {D.nas['UUP'].get(date(2018, 1, 2))} / {D.nas['UUP'].get(LAST_DAY)}")
    for s in ("SPY", "UUP"):
        miss = sum(1 for d in D.days if d not in D.nas[s])
        emit(f"- {s}: днів COMP без {s}: {miss}")
    emit(f"- днів COMP без 2Y: {sum(1 for d in D.days if d not in D.y2)}, без 10Y: {sum(1 for d in D.days if d not in D.y10)}")
    for a in CRYPTO:
        miss = [d for d in D.days if d not in D.close[a]]
        diffs = [abs(D.close[a][d] / D.utc_close[a][d] - 1) for d in D.days if d in D.close[a] and d in D.utc_close[a]]
        emit(
            f"- {a}: годин у кеші {D.hours[a]}; днів без ціни о закритті NY: {len(miss)} {[str(x) for x in miss[:8]]}; "
            f"|ціна 16:00 ET / close 00:00 UTC − 1|: медіана {statistics.median(diffs):.2%}, макс {max(diffs):.1%}"
        )
    emit(
        f"- BTC 2021-11-10 о закритті NY: {D.close['BTC'].get(date(2021, 11, 10))} (того дня ATH ≈ 69 000, закриття 16:00 ET ≈ 65–66 тис.)"
    )
    # COMP open field check (H4b gate)
    bad = 0
    tot = 0
    for a, b in zip(D.days[:-1], D.days[1:], strict=True):
        o = D.nas["COMP"][b][0]
        tot += 1
        if o is None or abs(o / D.nas["COMP"][a][1] - 1) < 1e-9:
            bad += 1
    emit(f"- open COMP: днів, де open відсутній або = попередньому close: {bad}/{tot} ({bad / tot:.1%}); поріг для H4b 5 %")
    emit(f"- змін ставки ФРС у таблиці від 2017: {[(str(d), round(c * 1e4)) for d, c in D.fed if d.year >= 2017]}")
    ph = [fed_phase(D.fed, D.days[i]) for i in range(D.n)]
    eps = [(lab, D.end[s], D.end[e], e - s + 1) for lab, s, e in episodes(ph)]
    emit("- епізоди фаз (вікно 120 днів): " + "; ".join(f"{lab} {s}…{e} ({n})" for lab, s, e, n in eps))


# ----------------------------------------------------------------------------- report


def h1(D: Data) -> None:
    emit("## H1. Кореляція і R² за роками (денні зміни, закриття NY)")
    for a in CRYPTO:
        emit(f"\n### {a}: кореляція з CI (блоки 20 днів) — уся вибірка і за роками")
        emit("| ряд | уся вибірка | n | " + " | ".join(YEARS) + " |")
        emit("|---|---|---|" + "---|" * len(YEARS))
        for m in MACRO:
            idx = rows_where(D, [a, m])
            x, y = arr(D.r[m], idx), arr(D.r[a], idx)
            b = corr_boot(x, y)
            cells = []
            for yr in YEARS:
                j = np.array([k for k, i in enumerate(idx) if year_of(D, int(i)) == yr], dtype=int)
                if len(j) < 20:
                    cells.append("—")
                    continue
                bb = corr_boot(x[j], y[j])
                cells.append(f"{bb['stat']:+.2f} {ci(bb['ci95'])}")
            emit(f"| {m} | {b['stat']:+.2f} {ci(b['ci95'])} | {len(idx)} | " + " | ".join(cells) + " |")
    emit("\n### Узгодженість із заміром Claude (BTC ↔ COMP, кореляція за роками)")
    idx = rows_where(D, ["BTC", "COMP"])
    emit("| рік | наша | Claude | різниця |\n|---|---|---|---|")
    for yr in YEARS:
        j = [int(i) for i in idx if year_of(D, int(i)) == yr]
        c = pearson(arr(D.r["COMP"], np.array(j)), arr(D.r["BTC"], np.array(j)))
        emit(f"| {yr} | {c:+.2f} | {CLAUDE_YEARLY[yr]:+.2f} | {c - CLAUDE_YEARLY[yr]:+.2f} |")

    emit("\n### Ковзна 60-денна кореляція з COMP за роками (описово; кінець вікна в році)")
    for a in CRYPTO:
        rc = rolling_corr(D.r["COMP"], D.r[a], window=60)
        emit(f"\n{a}:\n\n| рік | n вікон | медіана | мін | макс | частка > 0.3 | частка < 0 |\n|---|---|---|---|---|---|---|")
        for yr in YEARS:
            v = [c for i, c in enumerate(rc) if c is not None and year_of(D, i) == yr]
            if not v:
                continue
            emit(
                f"| {yr} | {len(v)} | {statistics.median(v):+.2f} | {min(v):+.2f} | {max(v):+.2f} | "
                f"{sum(c > 0.3 for c in v) / len(v):.0%} | {sum(c < 0 for c in v) / len(v):.0%} |"
            )
        for m in ("SPY", "UUP", "D2Y", "D10Y"):
            rm = [c for c in rolling_corr(D.r[m], D.r[a], window=60) if c is not None]
            emit(f"- {a} ↔ {m}: ковзна 60д медіана {statistics.median(rm):+.2f}, мін {min(rm):+.2f}, макс {max(rm):+.2f}")

    emit("\n### Тест тези «90 % руху BTC дає макро»: R² OLS на 5 рядів (COMP, SPY, UUP, Δ2Y, Δ10Y)")
    for a in CRYPTO:
        cols = [a, *MACRO]
        idx = rows_where(D, cols)
        Y = arr(D.r[a], idx)
        Xs = [arr(D.r[m], idx) for m in MACRO]
        emit(f"\n{a}:\n\n| період | n | R² | 95 % CI |\n|---|---|---|---|")
        b = boot(len(Y), lambda i, Y=Y, Xs=Xs: ols_r2(Y[i], [x[i] for x in Xs]))
        emit(f"| 2018–2026 | {len(Y)} | {b['stat']:.3f} | {ci(b['ci95'], d=3)} |")
        his = []
        for yr in YEARS:
            j = np.array([k for k, i in enumerate(idx) if year_of(D, int(i)) == yr], dtype=int)
            Yj, Xj = Y[j], [x[j] for x in Xs]
            bb = boot(len(Yj), lambda i, Yj=Yj, Xj=Xj: ols_r2(Yj[i], [x[i] for x in Xj]))
            his.append(bb["ci95"][1] if bb["ci95"] else float("nan"))
            emit(f"| {yr} | {len(Yj)} | {bb['stat']:.3f} | {ci(bb['ci95'], d=3)} |")
        roll = rolling([D.r[a], *[D.r[m] for m in MACRO]], lambda c: ols_r2(c[0], c[1:]), window=60)
        rv = [(v, D.end[i]) for i, v in enumerate(roll) if v is not None]
        mx = max(rv)
        emit(f"- максимум ковзного 60-денного R²: {mx[0]:.3f} (вікно до {mx[1]}); медіана {statistics.median(v for v, _ in rv):.3f}")
        verdict = "✗" if all(h < 0.9 for h in his) else "≈"
        emit(f"- **вердикт тези 90 % для {a}: {verdict}** (верхня межа CI R² < 0.9 у кожному році: {all(h < 0.9 for h in his)})")

    emit("\n### Стійкість H1: лише пари вт–пт (1 календарна доба), 2018–2021 проти 2022–2026")
    emit("| актив | ряд | усі пари | вт–пт | 2018–2021 | 2022–2026 |\n|---|---|---|---|---|---|")
    for a in CRYPTO:
        for m in MACRO:
            out = []
            for pred in (lambda i: True, lambda i: D.gap[i] == 1, lambda i: D.end[i] < SPLIT, lambda i: D.end[i] >= SPLIT):
                idx = rows_where(D, [a, m], pred)
                b = corr_boot(arr(D.r[m], idx), arr(D.r[a], idx))
                out.append(f"{b['stat']:+.2f} {ci(b['ci95'])} (n {len(idx)})")
            emit(f"| {a} | {m} | " + " | ".join(out) + " |")

    emit("\n### Перевірка 5a (за активами): ETH − BTC, кореляція з COMP")
    idx = rows_where(D, ["BTC", "ETH", "COMP"])
    x, yb, ye = arr(D.r["COMP"], idx), arr(D.r["BTC"], idx), arr(D.r["ETH"], idx)
    b = boot(len(x), lambda i: pearson(x[i], ye[i]) - pearson(x[i], yb[i]))
    emit(f"- різниця кореляцій ETH − BTC: {b['stat']:+.3f} {ci(b['ci95'], d=3)} (n {len(idx)})")
    for pred, lab in ((lambda i: D.end[i] < SPLIT, "2018–2021"), (lambda i: D.end[i] >= SPLIT, "2022–2026")):
        j = np.array([k for k, i in enumerate(idx) if pred(int(i))], dtype=int)
        b = boot(len(j), lambda i, j=j: pearson(x[j][i], ye[j][i]) - pearson(x[j][i], yb[j][i]))
        emit(f"- {lab}: {b['stat']:+.3f} {ci(b['ci95'], d=3)} (n {len(j)})")
    bb = boot(len(x), lambda i: beta(ye[i], x[i]) - beta(yb[i], x[i]))
    emit(f"- різниця бет ETH − BTC до COMP: {bb['stat']:+.3f} {ci(bb['ci95'], d=3)}")


def stress_block(D: Data, a: str, thr: float, pred: Callable[[int], bool] = lambda i: True, drop: int = 0) -> dict[str, Any]:
    idx = rows_where(D, [a, "COMP"], pred)
    dropped: list[str] = []
    if drop:
        big = set(sorted(idx, key=lambda i: -abs(D.r[a][int(i)]))[:drop])  # type: ignore[arg-type]
        dropped = [f"{D.end[int(i)]} ({D.r[a][int(i)] * 100:+.1f} %)" for i in sorted(big)]  # type: ignore[operator]
        idx = np.array([i for i in idx if i not in big], dtype=int)
    x, y = arr(D.r["COMP"], idx), arr(D.r[a], idx)
    lt = math.log(1 - thr)
    s = x <= lt

    def mean_s(i: np.ndarray) -> float:
        m = s[i]
        return float(y[i][m].mean()) if m.sum() >= 3 else float("nan")

    def bdiff(i: np.ndarray) -> float:
        m = s[i]
        if m.sum() < 5:
            return float("nan")
        return beta(y[i][m], x[i][m]) - beta(y[i][~m], x[i][~m])

    return {
        "n": int(s.sum()),
        "N": len(idx),
        "mean": boot(len(x), mean_s),
        "bdiff": boot(len(x), bdiff),
        "b_s": beta(y[s], x[s]),
        "b_o": beta(y[~s], x[~s]),
        "c_s": pearson(x[s], y[s]),
        "c_o": pearson(x[~s], y[~s]),
        "up": (int((x >= -lt).sum()), beta(y[x >= -lt], x[x >= -lt]) if (x >= -lt).sum() >= 5 else float("nan")),
        "x_mean": float(x[s].mean()) if s.any() else float("nan"),
        "dropped": dropped,
    }


def h2(D: Data) -> None:
    emit("\n## H2. Стресові дні (лог-дохідність COMP ≤ ln(1 − поріг))")
    hdr = (
        "| актив | варіант | n стрес / усіх | середнє COMP у стрес | середнє актива у стрес, % | 95 % CI | β стрес | β інші | "
        "β стрес − інші | 95 % CI | кор. стрес / інші (описово) | β у дні COMP ≥ +поріг (n) |\n|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    emit(hdr)
    variants: list[tuple[str, float, Callable[[int], bool], int]] = [
        ("**2 % (головний)**", 0.02, lambda i: True, 0),
        ("1.5 %", 0.015, lambda i: True, 0),
        ("3 %", 0.03, lambda i: True, 0),
        ("2 %, 2018–2021", 0.02, lambda i: D.end[i] < SPLIT, 0),
        ("2 %, 2022–2026", 0.02, lambda i: D.end[i] >= SPLIT, 0),
        ("2 %, лише вт–пт", 0.02, lambda i: D.gap[i] == 1, 0),
        ("2 %, без 5 найбільших |r|", 0.02, lambda i: True, 5),
    ]
    res = {}
    for a in CRYPTO:
        for lab, thr, pred, drop in variants:
            r = stress_block(D, a, thr, pred, drop)
            res[(a, lab)] = r
            emit(
                f"| {a} | {lab} | {r['n']} / {r['N']} | {r['x_mean'] * 100:+.2f} % | {f(r['mean']['stat'], 100)} | {ci(r['mean']['ci95'], 100)} | "
                f"{r['b_s']:.2f} | {r['b_o']:.2f} | {f(r['bdiff']['stat'])} | {ci(r['bdiff']['ci95'])} | {r['c_s']:+.2f} / {r['c_o']:+.2f} | "
                f"{r['up'][1]:.2f} ({r['up'][0]}) |"
            )
    for a in CRYPTO:
        emit(f"- {a}, «без 5 найбільших |r|»: відкинуто {', '.join(res[(a, '2 %, без 5 найбільших |r|')]['dropped'])}")
    for a in CRYPTO:
        r = res[(a, "**2 % (головний)**")]
        m_hi = r["mean"]["ci95"][1] if r["mean"]["ci95"] else float("nan")
        lo, hi = r["bdiff"]["ci95"] if r["bdiff"]["ci95"] else (float("nan"), float("nan"))
        va = "✓" if m_hi < 0 else ("≈" if r["mean"]["stat"] < 0 else "✗")
        vb = "✓" if lo > 0 else ("✗" if hi < 0 else ("≈" if r["bdiff"]["stat"] > 0 else "◯"))
        emit(f"- **{a}: H2a {va}, H2b {vb}**")


def h3(D: Data) -> None:
    emit("\n## H3. Фаза ФРС (за змінами з датою чинності ≤ початку вікна)")
    for w in (120, 90, 180):
        ph = [fed_phase(D.fed, D.days[i], window_days=w) for i in range(D.n)]
        emit(f"\n### вікно {w} днів{' (головне)' if w == 120 else ' (стійкість)'}")
        emit("| актив | фаза | n | β до COMP | 95 % CI | кор. | 95 % CI |\n|---|---|---|---|---|---|---|")
        for a in CRYPTO:
            idx = rows_where(D, [a, "COMP"])
            x, y = arr(D.r["COMP"], idx), arr(D.r[a], idx)
            lab = np.array([ph[int(i)] for i in idx])
            for p in ("hike", "pause", "cut"):
                m = lab == p
                bb = beta_boot(y[m], x[m])
                cb = corr_boot(x[m], y[m])
                emit(f"| {a} | {p} | {int(m.sum())} | {bb['stat']:.2f} | {ci(bb['ci95'])} | {cb['stat']:+.2f} | {ci(cb['ci95'])} |")

            def dif(i: np.ndarray, x=x, y=y, lab=lab) -> float:
                h, q = lab[i] == "hike", lab[i] == "pause"
                if h.sum() < 10 or q.sum() < 10:
                    return float("nan")
                return beta(y[i][h], x[i][h]) - beta(y[i][q], x[i][q])

            d = boot(len(x), dif)
            emit(f"| {a} | β підв − β пауза | — | {f(d['stat'])} | {ci(d['ci95'])} | | |")
            if w == 120:
                bp = beta(y[lab == "pause"], x[lab == "pause"])
                emit(
                    f"\nЕпізоди ({a}, вікно 120):\n\n| фаза | від | до | n | β | кор. | β − β пауза (уся) |\n|---|---|---|---|---|---|---|"
                )
                okeps = []
                for p, s, e in episodes(list(lab)):
                    j = np.arange(s, e + 1)
                    be, ce = beta(y[j], x[j]), pearson(x[j], y[j])
                    emit(f"| {p} | {D.end[int(idx[s])]} | {D.end[int(idx[e])]} | {len(j)} | {f(be)} | {f(ce)} | {f(be - bp)} |")
                    if p == "hike" and len(j) >= 60:
                        okeps.append(be - bp)
                lo = d["ci95"][0] if d["ci95"] else float("nan")
                hi = d["ci95"][1] if d["ci95"] else float("nan")
                # registered rule, analytics.macro_regimes.h3_verdict (fixed after review 2026-10-18: the first run
                # graded ≈ on the point estimate alone and invented "✗ (opposite sign)")
                v = h3_verdict(d["stat"], lo, hi, okeps)
                emit(
                    f"\n- **{a}: H3 {v}** (епізодів підвищень ≥ 60 пар: {len(okeps)}, β епізоду − β пауза: "
                    f"{', '.join(f(e) for e in okeps) or '—'}; знак тримається в кожному: {all(e > 0 for e in okeps) if okeps else None})"
                )


def h4(D: Data) -> None:
    emit("\n## H4. Lead-lag (денні дані)")
    emit(
        "| актив | варіант | n | кор(COMP[t−1], крипта[t]) | 95 % CI | кор(крипта[t−1], COMP[t]) | 95 % CI |\n|---|---|---|---|---|---|---|"
    )
    for a in CRYPTO:
        for lab, pred in (
            ("усі", lambda i: True),
            ("2018–2021", lambda i: D.end[i] < SPLIT),
            ("2022–2026", lambda i: D.end[i] >= SPLIT),
            ("день t — вт–пт", lambda i: D.gap[i] == 1),
        ):
            rows = [i for i in range(1, D.n) if pred(i) and all(D.r[c][k] is not None for c in (a, "COMP") for k in (i - 1, i))]
            xl = np.array([D.r["COMP"][i - 1] for i in rows], float)
            yc = np.array([D.r[a][i] for i in rows], float)
            cl = np.array([D.r[a][i - 1] for i in rows], float)
            xc = np.array([D.r["COMP"][i] for i in rows], float)
            b1 = corr_boot(xl, yc)
            b2 = corr_boot(cl, xc)
            emit(f"| {a} | {lab} | {len(rows)} | {b1['stat']:+.3f} | {ci(b1['ci95'], d=3)} | {b2['stat']:+.3f} | {ci(b2['ci95'], d=3)} |")

    emit("\n### H4b. Вихідні: крипта до 09:00 ET першого дня після вихідних проти гепу і сесії COMP")
    rows = []
    for i in range(D.n):
        if D.gap[i] < 3:
            continue
        a_d, b_d = D.days[i], D.days[i + 1]
        o = D.nas["COMP"][b_d][0]
        pc, pb = D.close["BTC"].get(a_d), D.pre_open["BTC"].get(b_d)
        pe, pe2 = D.close["ETH"].get(a_d), D.pre_open["ETH"].get(b_d)
        if o is None or None in (pc, pb, pe, pe2):
            continue
        rows.append(
            (
                math.log(pb / pc),
                math.log(pe2 / pe),
                math.log(o / D.nas["COMP"][a_d][1]),
                math.log(D.nas["COMP"][b_d][1] / o),
            )
        )
    R = np.array(rows, float)
    emit(
        "| актив | n | кор(крипта вихідні, геп COMP) | 95 % CI | кор(крипта вихідні, сесія COMP open→close) | 95 % CI |\n|---|---|---|---|---|---|"
    )
    for k, a in enumerate(CRYPTO):
        b1 = corr_boot(R[:, k], R[:, 2])
        b2 = corr_boot(R[:, k], R[:, 3])
        emit(f"| {a} | {len(R)} | {b1['stat']:+.3f} | {ci(b1['ci95'], d=3)} | {b2['stat']:+.3f} | {ci(b2['ci95'], d=3)} |")

    emit("\n### H4c. Правило: позиція = знак r_COMP[t] на наступне вікно крипти, витрати 0.05 % за одиницю зміни (на нотіонал)")
    emit("| актив | варіант | n днів | середнє net, % на день | 95 % CI | річних (×252), % | частка > 0 |\n|---|---|---|---|---|---|---|")
    dropped_notes: list[str] = []
    for a in CRYPTO:
        for lab, pred, cost, drop in (
            ("**головний**", lambda i: True, COST, 0),
            ("витрати ×2", lambda i: True, 2 * COST, 0),
            ("без витрат", lambda i: True, 0.0, 0),
            ("2018–2023", lambda i: D.end[i] < HALF2, COST, 0),
            ("2024–2026 (друга половина)", lambda i: D.end[i] >= HALF2, COST, 0),
            ("без 5 найбільших |r|", lambda i: True, COST, 5),
        ):
            rows_i = [i for i in range(D.n - 1) if D.r["COMP"][i] is not None and D.r[a][i + 1] is not None and pred(i + 1)]
            if drop:
                big = set(sorted(rows_i, key=lambda i: -abs(D.r[a][i + 1]))[:drop])  # type: ignore[arg-type]
                rows_i = [i for i in rows_i if i not in big]
                days = ", ".join(f"{D.end[i + 1]} ({D.r[a][i + 1] * 100:+.1f} %)" for i in sorted(big))  # type: ignore[operator]
                dropped_notes.append(f"- {a}, «без 5 найбільших |r|»: відкинуто {days}")
            sig = [D.r["COMP"][i] for i in rows_i]
            nxt = [D.r[a][i + 1] for i in rows_i]
            pnl = np.array(sign_rule(sig, nxt, cost=cost), float)  # type: ignore[arg-type]
            b = boot(len(pnl), lambda i, p=pnl: float(p[i].mean()))
            emit(
                f"| {a} | {lab} | {len(pnl)} | {f(b['stat'], 100, 3)} | {ci(b['ci95'], 100, 3)} | {f(b['stat'] * 252, 100, 1)} | "
                f"{float((pnl > 0).mean()):.0%} |"
            )
    for line in dropped_notes:
        emit(line)


def stage_report(D: Data, out: str) -> None:
    emit(f"# Сирий вивід verify_macro_regimes.py ({datetime.now(UTC).isoformat(timespec='seconds')})")
    emit(f"Пари {D.end[0]} … {D.end[-1]}, n = {D.n}; бутстреп рухомими блоками 20 днів, {REPS} повторів, seed {SEED}.")
    t0 = time.perf_counter()
    h1(D)
    print(f"[h1 {time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB]", flush=True)
    h2(D)
    h3(D)
    h4(D)
    emit(f"\n_час {time.perf_counter() - t0:.0f} с, peak RSS {rss_mb():.0f} MB_")
    if out:
        Path(out).expanduser().write_text("\n".join(L) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("prereg", "fetch", "sanity", "report"), required=True)
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/macro_regimes")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "prereg":
        stage_prereg(cache)
    elif a.stage == "fetch":
        (cache / "klines").mkdir(exist_ok=True)
        # BTC hourly files already downloaded by verify_weekend_vol.py (same format), read only
        asyncio.run(fetch(cache, Path("~/.cache/tradedesk-agents/weekend_vol/klines").expanduser()))
    elif a.stage == "sanity":
        stage_sanity(Data(cache))
        if a.out:
            Path(a.out).expanduser().write_text("\n".join(L) + "\n")
    else:
        stage_report(Data(cache), a.out)


if __name__ == "__main__":
    main()
