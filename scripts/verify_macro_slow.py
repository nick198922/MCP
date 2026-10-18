"""Slow macro and BTC/ETH on monthly horizons: M2, policy rate, 2Y/10Y, real 10Y, broad dollar (IDEA-20261017-macro, B3).

The pre-registration is the card section "Попередня реєстрація (B3: повільне макро)" in
``docs/ideas/IDEA-20261017-macro.md``. Network only through ``core.http.Http`` (Fed Data Download Program, Treasury)
and the runtime's ``rt.exchanges`` (Bitstamp daily candles); every response is written to the per-file cache under
``--cache-dir`` as it arrives (default ``~/.cache/tradedesk-agents/macro_slow``). B1's cache
(``~/.cache/tradedesk-agents/macro_regimes``: Treasury 2017+, Fed rate table, UUP, COMP, Binance hourly) is read only.

    uv run python scripts/verify_macro_slow.py --stage prereg   # hash of the registration -> registration.txt
    uv run python scripts/verify_macro_slow.py --stage fetch    # H.6, H.10, Treasury nominal 2014-16 and real 2014+, Bitstamp
    uv run python scripts/verify_macro_slow.py --stage sanity   # levels, counts, gaps (no results)
    uv run python scripts/verify_macro_slow.py --stage report --out raw.md
    uv run python scripts/verify_macro_slow.py --stage explore --out explore.md   # added after the registered run
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
import zipfile
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.macro_regimes import (
    HOUR_MS,
    episodes,
    fed_phase,
    ny_close_ms,
    ols_r2,
    parse_fed_changes,
    parse_nasdaq_historical,
    parse_treasury_csv,
    pearson,
)
from tradedesk.analytics.macro_slow import (
    add_months,
    block_bootstrap,
    candle_month_close,
    changes,
    circular_shift_null,
    exposure_alpha,
    lagged_pairs,
    mde,
    month_end_values,
    month_key,
    month_range,
    parse_frb_series,
    phase_verdict,
    predictive_verdict,
    rule_alpha_stat,
    spearman,
    timing_positions,
    timing_returns,
)

ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "docs/ideas/IDEA-20261017-macro.md"
SECTION = "## Попередня реєстрація (B3: повільне макро)"
B1 = Path("~/.cache/tradedesk-agents/macro_regimes").expanduser()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36", "Accept": "*/*"}
DAY_MS = 86_400_000
BTC_FIRST, ETH_FIRST, LAST = date(2015, 1, 1), date(2018, 1, 1), date(2026, 9, 1)
HALF2 = date(2021, 1, 1)
POST = date(2024, 7, 1)
COVID = (date(2020, 3, 1), date(2020, 12, 1))
REPS, SEED, BLOCK = 2000, 20261018, 6
COST = 0.001
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


async def fetch(cache: Path) -> None:
    from tradedesk.core.http import Http
    from tradedesk.server import build_server

    fed = Http("fed", "https://www.federalreserve.gov", rate=0.3, burst=1, timeout=90, retries=2, headers=UA)
    tre = Http("treasury", "https://home.treasury.gov", rate=0.3, burst=1, timeout=60, retries=2, headers=UA)
    t0 = time.perf_counter()
    try:
        for rel in ("H6", "H10"):
            p = cache / f"{rel.lower()}.zip"
            probe = cache / "probe" / p.name
            if not p.exists() and probe.exists():
                shutil.copyfile(probe, p)  # downloaded through Http by the reachability probe the same day
            if not p.exists():
                f = await fed.get_bytes("/datadownload/Output.aspx", params={"rel": rel, "filetype": "zip"})
                _write(p, f.data)
            print(f"fed {rel}: {p.stat().st_size} bytes  rss={rss_mb():.0f}MB", flush=True)
        path = "/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{yr}/all"
        jobs = [("daily_treasury_yield_curve", "treasury", yr) for yr in (2014, 2015, 2016)]
        jobs += [("daily_treasury_real_yield_curve", "real", yr) for yr in range(2014, 2027)]
        for typ, pre, yr in jobs:
            p = cache / f"{pre}_{yr}.csv"
            if p.exists():
                continue
            params = {"type": typ, "field_tdr_date_value": str(yr), "page": "", "_format": "csv"}
            f = await tre.get_bytes(path.format(yr=yr), params=params)
            _write(p, f.data)
            print(f"treasury {pre} {yr}: {len(f.data)} bytes {time.perf_counter() - t0:.0f}s", flush=True)
    finally:
        await fed.aclose()
        await tre.aclose()

    bdir = cache / "bitstamp"
    bdir.mkdir(exist_ok=True)
    _, rt = build_server()
    end_ms = int(datetime(2026, 10, 2, tzinfo=UTC).timestamp() * 1000)
    try:
        for sym, start in (("BTC/USD", date(2014, 1, 1)), ("ETH/USD", date(2017, 8, 1))):
            since = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp() * 1000)
            while since < end_ms:
                p = bdir / f"{sym.replace('/', '_')}_{since}.json"
                if p.exists():
                    rows = json.loads(p.read_text())
                else:
                    f = await rt.exchanges.ohlcv("bitstamp", sym, "1d", 1000, since_ms=since)
                    rows = [r for r in f.data if r[0] >= since]
                    _write(p, json.dumps(rows))
                    print(
                        f"bitstamp {sym} since {datetime.fromtimestamp(since / 1000, UTC).date()}: {len(rows)} rows "
                        f"{time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB",
                        flush=True,
                    )
                    await asyncio.sleep(1.0)
                if not rows:
                    break
                since = int(rows[-1][0]) + DAY_MS
    finally:
        await rt.aclose()
    print("fetch done", flush=True)


# ----------------------------------------------------------------------------- load


def _frb(cache: Path, rel: str, series: str) -> dict[date, float]:
    with zipfile.ZipFile(cache / f"{rel.lower()}.zip") as z:
        name = next(n for n in z.namelist() if n.endswith("_data.xml"))
        return parse_frb_series(z.read(name).decode("utf-8"), series)


class Data:
    def __init__(self, cache: Path) -> None:
        # crypto (Bitstamp daily), one page file at a time
        self.daily: dict[str, dict[date, float]] = {}
        self.month: dict[str, dict[date, float]] = {}
        for a in ("BTC", "ETH"):
            rows: list[list[float]] = []
            for p in sorted((cache / "bitstamp").glob(f"{a}_USD_*.json"), key=lambda q: int(q.stem.rsplit("_", 1)[1])):
                rows.extend(json.loads(p.read_text()))
            self.daily[a] = {datetime.fromtimestamp(int(r[0]) / 1000, tz=UTC).date(): float(r[4]) for r in rows}
            self.month[a] = candle_month_close(rows)
        self.r = {a: changes(self.month[a]) for a in ("BTC", "ETH")}
        # yields
        nom: dict[date, tuple[float | None, ...]] = {}
        for p in [*(cache / f"treasury_{y}.csv" for y in (2014, 2015, 2016)), *sorted(B1.glob("treasury_*.csv"))]:
            nom.update(parse_treasury_csv(p.read_bytes().decode("utf-8-sig")))
        real: dict[date, tuple[float | None, ...]] = {}
        for p in sorted(cache.glob("real_*.csv")):
            real.update(parse_treasury_csv(p.read_bytes().decode("utf-8-sig"), columns=("10 YR",)))
        self.y2d = {d: v[0] for d, v in nom.items() if v[0] is not None}
        self.y10d = {d: v[1] for d, v in nom.items() if v[1] is not None}
        self.realdd = {d: v[0] for d, v in real.items() if v[0] is not None}
        self.dollar_d = _frb(cache, "H10", "JRXWTFB_N.B")
        self.m2 = {month_key(d): v for d, v in _frb(cache, "H6", "M2.M").items()}
        uup = parse_nasdaq_historical(json.loads((B1 / "nasdaq_UUP.json").read_text()))
        self.uup_d = {d: c for d, (_, c) in uup.items()}
        self.fed = parse_fed_changes((B1 / "fed_openmarket.htm").read_bytes().decode("utf-8-sig"))
        lv = {
            "y2": month_end_values(self.y2d),
            "y10": month_end_values(self.y10d),
            "real10": month_end_values(self.realdd),
            "dollar": month_end_values(self.dollar_d),
            "uup": month_end_values(self.uup_d),
        }
        self.levels = lv
        self.x = {
            "D2Y": changes(lv["y2"], kind="diff"),
            "D10Y": changes(lv["y10"], kind="diff"),
            "DREAL10": changes(lv["real10"], kind="diff"),
            "DDOLLAR": changes(lv["dollar"]),
            "DUUP": changes(lv["uup"]),
            "G1M2": changes(self.m2),
            "G3M2": changes(self.m2, span=3),
        }
        self.months = {"BTC": month_range(BTC_FIRST, LAST), "ETH": month_range(ETH_FIRST, LAST)}
        # exact alignment 2018+: crypto at 16:00 ET of the last COMP trading day of each month (Binance hourly, B1 cache)
        comp = parse_nasdaq_historical(json.loads((B1 / "nasdaq_COMP.json").read_text()))
        last_day: dict[date, date] = {}
        for d in comp:
            k = month_key(d)
            if k not in last_day or d > last_day[k]:
                last_day[k] = d
        need = {ny_close_ms(d): k for k, d in last_day.items()}
        self.exact: dict[str, dict[date, float]] = {"BTC": {}, "ETH": {}}
        for a in ("BTC", "ETH"):
            for p in sorted((B1 / "klines").glob(f"{a}USDT-1h-*.json")):
                for row in json.loads(p.read_text()):
                    t_end = int(row[0]) + HOUR_MS
                    if t_end in need:
                        self.exact[a][need[t_end]] = float(row[4])
        self.r_exact = {a: changes(self.exact[a]) for a in ("BTC", "ETH")}
        self.binance_utc0: dict[date, float] = {}
        for p in sorted((B1 / "klines").glob("BTCUSDT-1h-*.json")):
            for row in json.loads(p.read_text()):
                t = datetime.fromtimestamp((int(row[0]) + HOUR_MS) / 1000, tz=UTC)
                if t.hour == 0:
                    self.binance_utc0[(t - timedelta(days=1)).date()] = float(row[4])

    def phase(self, m: date, window: int = 120) -> str:
        return fed_phase(self.fed, m - timedelta(days=1), window_days=window)


# ----------------------------------------------------------------------------- helpers


def f(x: float | None, k: float = 1.0, d: int = 2) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * k:+.{d}f}"


def ci(c: tuple[float, float] | None, k: float = 1.0, d: int = 2) -> str:
    return f"[{c[0] * k:+.{d}f}; {c[1] * k:+.{d}f}]" if c else "—"


def boot(n: int, stat: Callable[[np.ndarray], float], block: int = BLOCK) -> dict[str, Any]:
    return block_bootstrap(n, stat, block=block, reps=REPS, seed=SEED)


def corr_of(pairs: Sequence[tuple[date, float, float]], block: int = BLOCK, rank: bool = False) -> dict[str, Any]:
    x = np.array([p[1] for p in pairs], float)
    y = np.array([p[2] for p in pairs], float)
    fn = spearman if rank else pearson
    return boot(len(x), lambda i: fn(x[i], y[i]), block=block)


def sub(pairs: Sequence[tuple[date, float, float]], pred: Callable[[date], bool]) -> list[tuple[date, float, float]]:
    return [p for p in pairs if pred(p[0])]


def point(pairs: Sequence[tuple[date, float, float]]) -> float:
    if len(pairs) < 5:
        return float("nan")
    return pearson([p[1] for p in pairs], [p[2] for p in pairs])


def drop_top(pairs: Sequence[tuple[date, float, float]], k: int = 3) -> tuple[list[tuple[date, float, float]], list[str]]:
    big = sorted(pairs, key=lambda p: -abs(p[2]))[:k]
    keep = [p for p in pairs if p not in big]
    return keep, [f"{p[0]:%Y-%m} ({p[2] * 100:+.0f} %)" for p in sorted(big)]


# ----------------------------------------------------------------------------- sanity (no results)


def stage_sanity(D: Data) -> None:
    emit("## Санітарні перевірки")
    emit(f"- peak RSS {rss_mb():.0f} MB")
    for a in ("BTC", "ETH"):
        ds = sorted(D.daily[a])
        gaps = [(x, y) for x, y in zip(ds[:-1], ds[1:], strict=True) if (y - x).days > 1]
        emit(
            f"- Bitstamp {a}: днів {len(ds)} ({ds[0]} … {ds[-1]}), розривів > 1 доби: {len(gaps)} {[(str(x), str(y)) for x, y in gaps[:6]]}"
        )
        ms = D.months[a]
        miss = [m for m in ms if m not in D.r[a]]
        emit(f"- {a}: місяців у вікні {len(ms)}, без дохідності: {len(miss)} {[str(m) for m in miss]}")
    emit(
        f"- BTC close місяця 2017-12: {D.month['BTC'].get(date(2017, 12, 1))} (очікуємо ≈ 13–14 тис.); 2021-11: {D.month['BTC'].get(date(2021, 11, 1))} (≈ 57 тис.)"
    )
    diffs = [abs(D.daily["BTC"][d] / D.binance_utc0[d] - 1) for d in D.binance_utc0 if d in D.daily["BTC"]]
    emit(
        f"- |Bitstamp BTC/USD close / Binance BTCUSDT 00:00 UTC − 1| (n {len(diffs)}): медіана {statistics.median(diffs):.3%}, 99-й перц. {np.quantile(diffs, 0.99):.2%}, макс {max(diffs):.1%}"
    )
    m2 = D.m2
    g12 = math.log(m2[date(2021, 2, 1)] / m2[date(2020, 2, 1)])
    emit(
        f"- M2 (млрд USD): 2020-02 {m2.get(date(2020, 2, 1))}, 2021-02 {m2.get(date(2021, 2, 1))} (очікуємо ≈ 15 400 і ≈ 19 400); річний приріст 2021-02 {math.expm1(g12):.1%} (пік ≈ 26–27 %); останній місяць {max(m2)}"
    )
    emit(
        f"- 10Y real кінець 2020-08: {D.levels['real10'].get(date(2020, 8, 1))} (≈ −0.010); 2Y кінець 2026-09: {D.levels['y2'].get(date(2026, 9, 1))}"
    )
    dl = {k: v for k, v in D.levels["dollar"].items() if date(2022, 6, 1) <= k <= date(2023, 1, 1)}
    emit(
        f"- broad dollar (H.10) кінці місяців 2022-06…2023-01: {[(f'{k:%Y-%m}', round(v, 1)) for k, v in sorted(dl.items())]} (пік ≈ 125–129)"
    )
    for nm, d in (("2Y", D.y2d), ("10Y", D.y10d), ("real10", D.realdd), ("dollar", D.dollar_d), ("UUP", D.uup_d)):
        ds = sorted(d)
        emit(f"- {nm}: {len(ds)} днів, {ds[0]} … {ds[-1]}")
    for k, v in D.x.items():
        ks = [m for m in month_range(date(2014, 10, 1), LAST) if m in v]
        emit(f"- зміна {k}: місяців з даними 2014-10…2026-09: {len(ks)}; перший {min(v) if v else None}")
    ph = [D.phase(m) for m in D.months["BTC"]]
    emit(
        "- фази ФРС за місяцями (на останній день попереднього місяця, вікно 120 днів): "
        + "; ".join(f"{lab} {D.months['BTC'][s]:%Y-%m}…{D.months['BTC'][e]:%Y-%m} ({e - s + 1})" for lab, s, e in episodes(ph))
    )
    emit(
        f"- exact-вирівнювання 2018+: місяців з BTC о 16:00 ET останнього торгового дня: {len(D.exact['BTC'])}, ETH: {len(D.exact['ETH'])}"
    )


# ----------------------------------------------------------------------------- report

PRED = (
    ("P1", "M2: g1(m−2) → r(m)", "G1M2", 2, +1),
    ("P2", "Δ2Y(m−1) → r(m)", "D2Y", 1, -1),
    ("P3", "Δreal10Y(m−1) → r(m)", "DREAL10", 1, -1),
    ("P4", "Δln долар H.10 (m−1) → r(m)", "DDOLLAR", 1, -1),
)


def section_c(D: Data) -> None:
    emit("## C. Одночасні місячні зміни (описово)")
    cols = ("D2Y", "D10Y", "DREAL10", "DDOLLAR", "G1M2", "DUUP")
    emit("| актив | ряд | n | кор. | 95 % CI | 2015–2020 | 2021–2026 |\n|---|---|---|---|---|---|---|")
    for a in ("BTC", "ETH"):
        for c in cols:
            pr = lagged_pairs(D.x[c], D.r[a], D.months[a], 0)
            b = corr_of(pr)
            emit(
                f"| {a} | {c} | {len(pr)} | {f(b['stat'])} | {ci(b['ci95'])} | {f(point(sub(pr, lambda m: m < HALF2)))} | {f(point(sub(pr, lambda m: m >= HALF2)))} |"
            )
    for a in ("BTC", "ETH"):
        five = ("D2Y", "D10Y", "DREAL10", "DDOLLAR", "G1M2")
        ms = [m for m in D.months[a] if m in D.r[a] and all(m in D.x[c] for c in five)]
        Y = np.array([D.r[a][m] for m in ms])
        X = [np.array([D.x[c][m] for m in ms]) for c in five]
        b = boot(len(Y), lambda i, Y=Y, X=X: ols_r2(Y[i], [x[i] for x in X]))
        emit(f"- R² OLS {a} на 5 рядів (місячні зміни): {b['stat']:.3f} {ci(b['ci95'], d=3)} (n {len(Y)}; бутстреп-CI R² зміщений угору)")
        for lo_, hi_ in ((date(2015, 1, 1), date(2020, 12, 1)), (HALF2, LAST)):
            j = [k for k, m in enumerate(ms) if lo_ <= m <= hi_]
            if len(j) > 10:
                emit(f"  - {lo_:%Y}–{hi_:%Y}: R² {ols_r2(Y[j], [x[j] for x in X]):.3f} (n {len(j)})")


def pred_block(D: Data, a: str) -> dict[str, Any]:
    out = {}
    emit(f"\n### {a}")
    emit(
        "| тест | n | кор. | 95 % CI | 99 % CI | SE | MDE | 2015–2020 | 2021–2026 (OOS) | 2024-07+ | вердикт |\n|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for code, lab, col, lag, sign in PRED:
        pr = lagged_pairs(D.x[col], D.r[a], D.months[a], lag)
        b = corr_of(pr)
        h1, h2 = point(sub(pr, lambda m: m < HALF2)), point(sub(pr, lambda m: m >= HALF2))
        md = mde(b["se"])
        v = predictive_verdict(b["stat"], b["ci95"], h2, sign, md)
        if v == "✓" and b["ci99"] and not (min(b["ci99"][0] * sign, b["ci99"][1] * sign) > 0):
            v += " (не стійке до множинності)"
        out[code] = (b, v, pr)
        emit(
            f"| {code} {lab} | {len(pr)} | {f(b['stat'])} | {ci(b['ci95'])} | {ci(b['ci99'])} | {b['se']:.3f} | {md:.2f} | {f(h1)} | {f(h2)} | "
            f"{f(point(sub(pr, lambda m: m >= POST)))} (n {len(sub(pr, lambda m: m >= POST))}) | **{v}** |"
        )
    return out


def section_p5(D: Data, a: str = "BTC") -> None:
    emit(f"\n## P5. Фаза ФРС на початок місяця і середня місячна лог-дохідність {a}")
    for w in (120, 90, 180):
        ms = [m for m in D.months[a] if m in D.r[a]]
        lab = np.array([D.phase(m, w) for m in ms])
        r = np.array([D.r[a][m] for m in ms])
        emit(f"\n### вікно {w} днів{' (головне)' if w == 120 else ' (стійкість)'}")
        emit("| фаза | місяців | середнє r, % | медіана, % |\n|---|---|---|---|")
        for p in ("hike", "pause", "cut"):
            m = lab == p
            emit(
                f"| {p} | {int(m.sum())} | {f(float(r[m].mean()) if m.any() else None, 100, 1)} | {f(float(np.median(r[m])) if m.any() else None, 100, 1)} |"
            )

        def dif(i: np.ndarray, lab=lab, r=r) -> float:
            c, h = lab[i] == "cut", lab[i] == "hike"
            if c.sum() < 3 or h.sum() < 3:
                return float("nan")
            return float(r[i][c].mean() - r[i][h].mean())

        b = boot(len(r), dif)
        halves = []
        for pred in (lambda m: m < HALF2, lambda m: m >= HALF2):
            j = np.array([k for k, m in enumerate(ms) if pred(m)], dtype=int)
            halves.append(dif(j))
        eps = episodes(list(lab))
        n_c = sum(1 for p, s, e in eps if p == "cut" and e - s + 1 >= 3)
        n_h = sum(1 for p, s, e in eps if p == "hike" and e - s + 1 >= 3)
        md = mde(b["se"])
        v = phase_verdict(b["stat"], b["ci95"], n_c, n_h, halves, md)
        emit(
            f"- cut − hike: {f(b['stat'], 100, 1)} % [{f(b['ci95'][0] if b['ci95'] else None, 100, 1)}; {f(b['ci95'][1] if b['ci95'] else None, 100, 1)}], "
            f"SE {b['se'] * 100:.1f} %, MDE {md * 100:.1f} % на місяць; половини {f(halves[0], 100, 1)} / {f(halves[1], 100, 1)}; "
            f"епізодів ≥ 3 міс.: cut {n_c}, hike {n_h} — **P5 {v}**"
        )
        if w == 120:
            emit("\n| епізод | від | до | місяців | середнє r, % |\n|---|---|---|---|---|")
            for p, s, e in eps:
                if p != "pause":
                    emit(f"| {p} | {ms[s]:%Y-%m} | {ms[e]:%Y-%m} | {e - s + 1} | {f(float(r[s : e + 1].mean()), 100, 1)} |")


def rule_block(
    D: Data, a: str, *, cost: float = COST, window: int = 60, min_hist: int = 36, months: Sequence[date] | None = None, label: str = ""
) -> dict[str, Any]:
    ms_all = months if months is not None else D.months[a]
    pos_map = timing_positions(D.x["G3M2"], ms_all, lag=2, window=window, min_hist=min_hist)
    ms = [m for m in ms_all if pos_map[m] is not None and m in D.r[a]]
    pos = np.array([pos_map[m] for m in ms], float)
    lr = np.array([D.r[a][m] for m in ms], float)
    net = np.array(timing_returns(list(pos.astype(int)), list(lr), cost=cost))
    b = boot(len(ms), rule_alpha_stat(pos, lr, cost))
    halves = []
    for pred in (lambda m: m < HALF2, lambda m: m >= HALF2):
        j = np.array([k for k, m in enumerate(ms) if pred(m)], dtype=int)
        if len(j) < 6:
            halves.append(float("nan"))
            continue
        nj = np.array(timing_returns(list(pos[j].astype(int)), list(lr[j]), cost=cost))
        halves.append(exposure_alpha(nj, pos[j], lr[j]))
    md = mde(b["se"])
    if b["ci95"] is None:
        v = "◯"
    elif b["ci95"][0] > 0 and all(h > 0 for h in halves):
        v = "✓"
    elif md > 0.03:
        v = "◯"
    else:
        v = "✗"
    emit(
        f"| {a} | {label} | {len(ms)} | {pos.mean():.0%} | {f(float(net.mean()), 100)} | {f(float(np.expm1(lr).mean()), 100)} | "
        f"{f(b['stat'], 100)} | {ci(b['ci95'], 100)} | {md * 100:.2f} | {f(halves[0], 100)} / {f(halves[1], 100)} | "
        f"{int(np.abs(np.diff(np.r_[0, pos])).sum())} | **{v}** |"
    )
    return {"b": b, "v": v, "ms": ms, "pos": pos, "lr": lr}


def section_r1(D: Data) -> None:
    emit("\n## R1. Правило M2: BTC спот, якщо g3 M2 (m−2) > медіани 60 попередніх відомих місяців, інакше кеш")
    emit(
        "| актив | варіант | місяців | у ринку | net правила, %/міс | купив і тримай, %/міс | альфа до експозиції, %/міс | 95 % CI | MDE, % | половини | угод (вх./вих.) | вердикт |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    rule_block(D, "BTC", label="**головний**")
    rule_block(D, "BTC", cost=3 * COST, label="витрати ×3")
    rule_block(D, "BTC", window=36, min_hist=36, label="медіана 36 міс.")
    rule_block(D, "BTC", months=[m for m in D.months["BTC"] if not COVID[0] <= m <= COVID[1]], label="без 2020-03…12")
    rule_block(D, "ETH", label="ETH")
    emit("- дохідності на капітал (спот, без плеча), прості; кеш без відсотків. Вердикт для стійкості й ETH — описово.")


def section_robust(D: Data) -> None:
    emit("\n## Стійкість P1–P4 (BTC, якщо не сказано інше; друкується вся)")
    emit("| тест | варіант | n | кор. | 95 % CI |\n|---|---|---|---|---|")
    notes = []
    exact_months = month_range(date(2018, 1, 1), LAST)
    for code, _lab, col, lag, _sign in PRED:
        base = lagged_pairs(D.x[col], D.r["BTC"], D.months["BTC"], lag)
        variants: list[tuple[str, list[tuple[date, float, float]], int, bool]] = [
            ("без 2020-03…2020-12", sub(base, lambda m: not COVID[0] <= m <= COVID[1]), BLOCK, False),
        ]
        kept, dropped = drop_top(base)
        variants.append(("без 3 найбільших |r_BTC|", kept, BLOCK, False))
        notes.append(f"- {code}, без 3 найбільших: {', '.join(dropped)}")
        for lg in (1, 2, 3):
            if lg != lag:
                tag = " (не торгується, описово)" if code == "P1" and lg == 1 else ""
                variants.append((f"лаг {lg}{tag}", lagged_pairs(D.x[col], D.r["BTC"], D.months["BTC"], lg), BLOCK, False))
        if code == "P1":
            variants.append(("g3 M2 (m−2)", lagged_pairs(D.x["G3M2"], D.r["BTC"], D.months["BTC"], 2), BLOCK, False))
        if code == "P2":
            variants.append(("Δ10Y замість Δ2Y", lagged_pairs(D.x["D10Y"], D.r["BTC"], D.months["BTC"], 1), BLOCK, False))
        if code == "P4":
            variants.append(("UUP замість H.10 (2018+)", lagged_pairs(D.x["DUUP"], D.r["BTC"], exact_months, 1), BLOCK, False))
        variants.append(
            ("2018+, точне вирівнювання (Binance 16:00 ET)", lagged_pairs(D.x[col], D.r_exact["BTC"], exact_months, lag), BLOCK, False)
        )
        variants.append(("2018+, Bitstamp (той самий період)", lagged_pairs(D.x[col], D.r["BTC"], exact_months, lag), BLOCK, False))
        variants.append(("Спірмен", base, BLOCK, True))
        variants.append(("блоки 3 міс.", base, 3, False))
        variants.append(("блоки 12 міс.", base, 12, False))
        variants.append(("ETH 2018+", lagged_pairs(D.x[col], D.r["ETH"], D.months["ETH"], lag), BLOCK, False))
        for lab, pr, blk, rk in variants:
            b = corr_of(pr, block=blk, rank=rk)
            emit(f"| {code} | {lab} | {len(pr)} | {f(b['stat'])} | {ci(b['ci95'])} |")
    for n in notes:
        emit(n)


def section_5a(D: Data) -> None:
    emit("\n## Перевірка 5a: ETH − BTC на спільних місяцях (2018-01 … 2026-09)")
    emit("| тест | n | кор. BTC | кор. ETH | ETH − BTC | 95 % CI |\n|---|---|---|---|---|---|")
    for code, _lab, col, lag, _s in PRED:
        ms = [m for m in D.months["ETH"] if m in D.r["ETH"] and m in D.r["BTC"] and add_months(m, -lag) in D.x[col]]
        x = np.array([D.x[col][add_months(m, -lag)] for m in ms])
        yb = np.array([D.r["BTC"][m] for m in ms])
        ye = np.array([D.r["ETH"][m] for m in ms])
        b = boot(len(ms), lambda i, x=x, yb=yb, ye=ye: pearson(x[i], ye[i]) - pearson(x[i], yb[i]))
        emit(f"| {code} | {len(ms)} | {pearson(x, yb):+.2f} | {pearson(x, ye):+.2f} | {f(b['stat'])} | {ci(b['ci95'])} |")
    emit("- Активів два: тест неоднорідності й стійкість «топ-активів» сенсу не мають.")


def section_levels(D: Data) -> None:
    emit("\n## Ілюстрація: рівні проти змін (без вердикту)")
    ms = [m for m in D.months["BTC"] if m in D.month["BTC"] and add_months(m, -2) in D.m2]
    lb = np.log([D.month["BTC"][m] for m in ms])
    lm = np.log([D.m2[add_months(m, -2)] for m in ms])
    pr = lagged_pairs(D.x["G1M2"], D.r["BTC"], D.months["BTC"], 2)
    emit(
        f"- кор. рівнів ln BTC(m) і ln M2(m−2), 2015–2026: {pearson(lm, lb):+.2f} (n {len(ms)}) — трендові ряди; кор. змін (P1): {point(pr):+.2f}"
    )


def stage_report(D: Data, out: str) -> None:
    emit(f"# Сирий вивід verify_macro_slow.py ({datetime.now(UTC).isoformat(timespec='seconds')})")
    emit(
        f"Місяці BTC {BTC_FIRST:%Y-%m} … {LAST:%Y-%m}, ETH {ETH_FIRST:%Y-%m} …; бутстреп рухомими блоками {BLOCK} міс., {REPS} повторів, seed {SEED}."
    )
    t0 = time.perf_counter()
    section_c(D)
    emit("\n## P1–P4. Передбачувальні кореляції (сигнал відомий до початку місяця)")
    res = {a: pred_block(D, a) for a in ("BTC", "ETH")}
    emit("- вердикт ETH — вторинний (описово).")
    print(f"[p {time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB]", flush=True)
    section_p5(D)
    section_r1(D)
    section_robust(D)
    section_5a(D)
    section_levels(D)
    _ = res
    emit(f"\n_час {time.perf_counter() - t0:.0f} с, peak RSS {rss_mb():.0f} MB_")
    if out:
        Path(out).expanduser().write_text("\n".join(L) + "\n")


def _xy(D: Data, col: str, a: str, lag: int, sig: dict[date, float] | None = None) -> tuple[list[float], list[float]]:
    pr = lagged_pairs(sig if sig is not None else D.x[col], D.r[a], D.months[a], lag)
    return [p[1] for p in pr], [p[2] for p in pr]


def stage_explore(D: Data, out: str) -> None:
    """Exploratory checks written after the registered report (labelled exploratory in the verification report)."""
    emit(f"# Дослідницькі перевірки після зареєстрованого прогону ({datetime.now(UTC).isoformat(timespec='seconds')})")
    emit("Додано після перегляду результатів: бутстреп-SE кореляцій (0.055–0.077) вийшли меншими за 1/√n ≈ 0.085.")
    emit("\n## Нуль кругових зсувів сигналу (≥ 6 міс.; зберігає автокореляцію рядів)")
    emit("| актив | тест | лаг | n | кор. | p (зсуви) | SD нуля | MDE = 2.8·SD |\n|---|---|---|---|---|---|---|---|")
    for a in ("BTC", "ETH"):
        for code, _l, col, lag, _s in PRED:
            for lg in sorted({lag, 2, 3}):
                r = circular_shift_null(*_xy(D, col, a, lg))
                tag = " (зареєстр.)" if lg == lag else ""
                emit(
                    f"| {a} | {code} {col} | {lg}{tag} | {r['n']} | {r['stat']:+.2f} | {r['p']:.3f} | {r['sd']:.3f} | {2.8 * r['sd']:.2f} |"
                )
    emit("\n## Одночасні (лаг 0), той самий нуль")
    for a in ("BTC", "ETH"):
        for col in ("D2Y", "D10Y", "DREAL10", "DDOLLAR", "G1M2", "DUUP"):
            r = circular_shift_null(*_xy(D, col, a, 0))
            emit(f"- {a} {col}: кор. {r['stat']:+.2f}, p {r['p']:.3f}, SD нуля {r['sd']:.3f}, n {r['n']}")
    emit("\n## Лаги 1–4 для ставок і долара, BTC: половини, без 2022")
    emit("| ряд | лаг | усі | 2015–2020 | 2021–2026 | без 2022 | лише 2022 (n) |\n|---|---|---|---|---|---|---|")
    for col in ("D2Y", "D10Y", "DREAL10", "DDOLLAR"):
        for lg in (1, 2, 3, 4):
            pr = lagged_pairs(D.x[col], D.r["BTC"], D.months["BTC"], lg)
            only22 = sub(pr, lambda m: m.year == 2022)
            emit(
                f"| {col} | {lg} | {f(point(pr))} | {f(point(sub(pr, lambda m: m < HALF2)))} | {f(point(sub(pr, lambda m: m >= HALF2)))} | "
                f"{f(point(sub(pr, lambda m: m.year != 2022)))} | {f(point(only22))} ({len(only22)}) |"
            )
    emit("\n## Накопичена зміна за 3 місяці m−3 … m−1 → r_BTC(m)")
    for col in ("D2Y", "DREAL10", "DDOLLAR"):
        cum: dict[date, float] = {}
        for m in D.months["BTC"]:
            v = [D.x[col].get(add_months(m, -k)) for k in (1, 2, 3)]
            if all(x is not None for x in v):
                cum[add_months(m, -1)] = float(sum(v))  # type: ignore[arg-type]
        r = circular_shift_null(*_xy(D, col, "BTC", 1, cum))
        emit(f"- {col}: кор. {r['stat']:+.2f}, p (зсуви) {r['p']:.3f}, n {r['n']}")
    if out:
        Path(out).expanduser().write_text("\n".join(L) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("prereg", "fetch", "sanity", "report", "explore"), required=True)
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/macro_slow")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "prereg":
        stage_prereg(cache)
    elif a.stage == "fetch":
        asyncio.run(fetch(cache))
    elif a.stage == "sanity":
        stage_sanity(Data(cache))
        if a.out:
            Path(a.out).expanduser().write_text("\n".join(L) + "\n")
    elif a.stage == "explore":
        stage_explore(Data(cache), a.out)
    else:
        stage_report(Data(cache), a.out)


if __name__ == "__main__":
    main()
