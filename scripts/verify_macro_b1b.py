"""Dollar (UUP), oil (USO), gold (GLD) and crypto: same-day links by regime and "gold up strongly today -> BTC next day"
(IDEA-20261017-macro, B1b).

The pre-registration is the card section "Попередня реєстрація (B1b: долар, нафта, золото)" in
``docs/ideas/IDEA-20261017-macro.md``. Network only through ``core.http.Http`` (public Nasdaq API, GLD and USO); COMP, UUP,
the Fed table and the BTC/ETH hourly klines are read from the B1 cache (``--b1-cache``, read only).

    uv run python scripts/verify_macro_b1b.py --stage prereg   # hash of the registration -> registration.txt
    uv run python scripts/verify_macro_b1b.py --stage fetch    # Nasdaq GLD, USO -> per-file cache
    uv run python scripts/verify_macro_b1b.py --stage sanity   # levels, gaps, splits, event counts (no results)
    uv run python scripts/verify_macro_b1b.py --stage power    # n events, SD and MDE only (no means)
    uv run python scripts/verify_macro_b1b.py --stage report --out raw.md
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import resource
import statistics
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.macro_crossasset import (
    UUP_EXDIV_LOG,
    corr_stat,
    event_diff,
    forward_log_return,
    g_verdict,
    holm,
    is_uup_exdiv,
    ols_coefs,
    r_verdict,
    shift_null,
    split_index,
    trailing_rank_flag,
    trailing_z,
)
from tradedesk.analytics.macro_regimes import (
    HOUR_MS,
    aligned_changes,
    beta,
    fed_phase,
    moving_block_bootstrap,
    ny_close_ms,
    parse_fed_changes,
    parse_nasdaq_historical,
    pearson,
    rolling_corr,
)
from tradedesk.analytics.macro_release import cochran_q

ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "docs/ideas/IDEA-20261017-macro.md"
SECTION = "## Попередня реєстрація (B1b: долар, нафта, золото)"
FIRST_DAY = date(2017, 12, 1)
LAST_DAY = date(2026, 10, 16)
SPLIT = date(2022, 1, 1)
CRYPTO = ("BTC", "ETH")
ETFS = ("GLD", "USO", "UUP")
SERIES = ("GLD", "USO", "UUP", "COMP")
FETCH = ("GLD", "USO")
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}
REPS = 2000
SEED = 20261018
BLOCK = 20
COST = 0.0005  # per side
Z_MAIN = 1.5
GATES = {"G1": 0.010, "G2": 0.015, "G3": 0.10}
H24 = 24 * HOUR_MS
L: list[str] = []


def emit(s: str = "") -> None:
    L.append(s)
    print(s, flush=True)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _write(p: Path, data: str) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(data)
    tmp.replace(p)


# ----------------------------------------------------------------------------- prereg / fetch


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


async def fetch(cache: Path) -> None:
    from tradedesk.core.http import Http

    nas = Http("nasdaq", "https://api.nasdaq.com", rate=0.3, burst=1, timeout=60, retries=2, headers=UA)
    t0 = time.perf_counter()
    try:
        for sym in FETCH:
            p = cache / f"nasdaq_{sym}.json"
            if p.exists():
                print(f"nasdaq {sym}: cached", flush=True)
                continue
            params = {"assetclass": "etf", "fromdate": FIRST_DAY.isoformat(), "todate": LAST_DAY.isoformat(), "limit": "9999"}
            f = await nas.get_json(f"/api/quote/{sym}/historical", params=params)
            _write(p, json.dumps(f.data))
            n = len(parse_nasdaq_historical(f.data))
            print(f"nasdaq {sym}: {n} rows {time.perf_counter() - t0:.1f}s rss={rss_mb():.0f}MB", flush=True)
    finally:
        await nas.aclose()
    print("fetch done", flush=True)


# ----------------------------------------------------------------------------- load


class Data:
    def __init__(self, cache: Path, b1: Path) -> None:
        src = {"COMP": b1, "UUP": b1, "GLD": cache, "USO": cache}
        self.nas = {s: parse_nasdaq_historical(json.loads((src[s] / f"nasdaq_{s}.json").read_text())) for s in SERIES}
        self.days = sorted(d for d in self.nas["COMP"] if FIRST_DAY <= d <= LAST_DAY)
        self.fed = parse_fed_changes((b1 / "fed_openmarket.htm").read_bytes().decode("utf-8-sig"))
        self.closes: dict[str, dict[int, float]] = {}
        for a in CRYPTO:
            m: dict[int, float] = {}
            for p in sorted((b1 / "klines").glob(f"{a}USDT-1h-*.json")):
                for r in json.loads(p.read_text()):
                    m[int(r[0])] = float(r[4])
            self.closes[a] = m
        d = self.days
        self.end = d[1:]  # signal day of pair i
        self.n = len(self.end)
        self.r: dict[str, list[float | None]] = {}
        self.split_dropped: dict[str, list[str]] = {}
        for s in SERIES:
            ch = aligned_changes(d, {k: v[1] for k, v in self.nas[s].items()})
            drop = []
            for i, x in enumerate(ch):
                if x is not None and abs(x) > math.log(4.0):  # unadjusted split (registered: USO 1:8, 2020-04-29)
                    drop.append(f"{self.end[i]} ({x:+.3f})")
                    ch[i] = None
                elif s == "UUP" and x is not None and is_uup_exdiv(self.end[i], x):
                    # Departure from the registration (data defect, not a parameter choice; added after the first run
                    # on the critic's review): the unadjusted UUP series drops on the December distribution day.
                    drop.append(f"{self.end[i]} ({x:+.4f}, ex-div)")
                    ch[i] = None
            self.split_dropped[s] = drop
            self.r[s] = ch
        self.t_close = [ny_close_ms(t) for t in self.end]
        for a in CRYPTO:
            px = {t: self.closes[a].get(ny_close_ms(t) - HOUR_MS) for t in d}
            self.r[a] = aligned_changes(d, px)  # same-day crypto between NY closes
            self.r[a + "_y24"] = [forward_log_return(self.closes[a], t, H24) for t in self.t_close]
            self.r[a + "_y72"] = [forward_log_return(self.closes[a], t, 3 * H24) for t in self.t_close]
            self.r[a + "_y24e1"] = [forward_log_return(self.closes[a], t + HOUR_MS, H24) for t in self.t_close]
            self.r[a + "_ynext"] = [*self.r[a][1:], None]  # to the NY close of the next COMP trading day (B1 pair)
        self.z: dict[tuple[str, int], list[float | None]] = {}
        for s in SERIES:
            for w in (20, 60, 120):
                self.z[(s, w)] = trailing_z(self.r[s], window=w, min_valid=max(15, int(w * 2 / 3)))
        self.top10 = trailing_rank_flag(self.r["GLD"], window=250, q=0.9, min_valid=200)


# ----------------------------------------------------------------------------- helpers


def f(x: float | None, k: float = 1.0, d: int = 2) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * k:+.{d}f}"


def ci(c: tuple[float, float] | None, k: float = 1.0, d: int = 2) -> str:
    return f"[{c[0] * k:+.{d}f}; {c[1] * k:+.{d}f}]" if c else "—"


def boot(n: int, stat: Callable[[np.ndarray], float]) -> dict[str, Any]:
    return moving_block_bootstrap(n, stat, block=BLOCK, reps=REPS, seed=SEED)


def valid(*cols: Sequence[Any], pred: Callable[[int], bool] = lambda i: True) -> np.ndarray:
    n = len(cols[0])
    return np.array([i for i in range(n) if pred(i) and all(c[i] is not None for c in cols)], dtype=int)


def col(c: Sequence[Any], idx: np.ndarray, dtype: Any = float) -> np.ndarray:
    return np.array([c[int(i)] for i in idx], dtype=dtype)


def diff_test(flag: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    """D = mean(y | flag) - mean(y | not flag) with block-bootstrap CI, circular-shift p and MDE."""
    b = boot(len(y), lambda i: event_diff(flag[i], y[i]))
    sn = shift_null(flag, y, lambda s, v: event_diff(s.astype(bool), v))
    k = split_index(len(y))
    return {
        "n": len(y),
        "ne": int(flag.sum()),
        "stat": b["stat"],
        "ci95": b["ci95"],
        "se": b["se"],
        "mde": 2.8 * b["se"] if b["se"] else float("nan"),
        "p": sn["p"],
        "is": event_diff(flag[:k], y[:k]),
        "oos": event_diff(flag[k:], y[k:]),
        "ne_oos": int(flag[k:].sum()),
        "mean_e": float(y[flag].mean()) if flag.any() else float("nan"),
        "mean_o": float(y[~flag].mean()) if (~flag).any() else float("nan"),
    }


def flags_from(z: Sequence[float | None], idx: np.ndarray, thr: float) -> np.ndarray:
    return np.array([z[int(i)] >= thr if thr > 0 else z[int(i)] <= thr for i in idx], dtype=bool)  # type: ignore[operator]


# ----------------------------------------------------------------------------- sanity / power (no results)


def stage_sanity(D: Data) -> None:
    emit("## Санітарні перевірки")
    emit(f"- торгових днів COMP: {len(D.days)} ({D.days[0]} … {D.days[-1]}), пар: {D.n}; peak RSS {rss_mb():.0f} MB")
    emit(f"- дублі дат COMP: {len(D.days) - len(set(D.days))}")
    for s in ETFS:
        miss = [str(d) for d in D.days if d not in D.nas[s]]
        extra = sum(1 for d in D.nas[s] if FIRST_DAY <= d <= LAST_DAY and d not in set(D.days))
        emit(f"- {s}: рядків {len(D.nas[s])}, днів COMP без {s}: {len(miss)} {miss[:6]}, днів {s} поза календарем COMP: {extra}")
        big = sorted(((abs(x), D.end[i], x) for i, x in enumerate(D.r[s]) if x is not None), reverse=True)[:5]
        emit("  - найбільші |лог-зміни|: " + ", ".join(f"{d} {x:+.3f}" for _, d, x in big))
        emit(
            f"  - виключено (спліт |r| > ln 4; для UUP також ex-div: друга половина грудня, лог-зміна < {UUP_EXDIV_LOG}): {D.split_dropped[s] or 'нема'}"
        )
        emit(f"  - |лог-зміна| > 0.25: {[f'{D.end[i]} {x:+.3f}' for i, x in enumerate(D.r[s]) if x is not None and abs(x) > 0.25]}")
    emit(f"- GLD close 2020-08-06 = {D.nas['GLD'].get(date(2020, 8, 6))} (очікуємо ≈ 190–195)")
    for d in (date(2020, 4, 27), date(2020, 4, 28), date(2020, 4, 29), date(2020, 4, 30)):
        emit(f"- USO {d}: {D.nas['USO'].get(d)}")
    emit(f"- GLD / USO / UUP на {LAST_DAY}: {[D.nas[s].get(LAST_DAY) for s in ETFS]}")
    for a in CRYPTO:
        emit(
            f"- {a}: годин у кеші {len(D.closes[a])}; сигнальних днів без Y24: {sum(1 for v in D.r[a + '_y24'] if v is None)}, без Y72: {sum(1 for v in D.r[a + '_y72'] if v is None)}"
        )
        miss = [str(D.end[i]) for i, v in enumerate(D.r[a + "_y24"]) if v is None]
        emit(f"  - дні без Y24: {miss[:10]}")
    emit(f"- BTC 2021-11-10 о 16:00 ET: {D.closes['BTC'].get(ny_close_ms(date(2021, 11, 10)) - HOUR_MS)} (B1: той самий файл кешу)")
    z = D.z[("GLD", 60)]
    rows = valid(z, D.r["BTC_y24"])
    ev = [D.end[int(i)] for i in rows if z[int(i)] >= Z_MAIN]  # type: ignore[operator]
    emit(
        f"- сигнальних днів (z_GLD і Y24 BTC є): {len(rows)}, {D.end[int(rows[0])]} … {D.end[int(rows[-1])]}; подій z_GLD ≥ 1.5: {len(ev)}"
    )
    cnt = Counter(d.year for d in ev)
    emit("- подій за роками: " + ", ".join(f"{y}: {cnt[y]}" for y in sorted(cnt)))
    k = split_index(len(rows))
    emit(f"- межа OOS (25 %): з {D.end[int(rows[k])]} (рядок {k} з {len(rows)})")
    zu = D.z[("UUP", 60)]
    ru = valid(zu, D.r["BTC_y24"])
    emit(
        f"- подій z_UUP ≥ 1.5 / ≤ −1.5 (після виключення ex-div): {int(flags_from(zu, ru, Z_MAIN).sum())} / {int(flags_from(zu, ru, -Z_MAIN).sum())}"
    )


def stage_power(D: Data) -> None:
    emit("## Стадія 0: потужність (лише n і SD)")
    for a in CRYPTO:
        for key, gate in (("_y24", GATES["G1"]), ("_y72", GATES["G2"])):
            z = D.z[("GLD", 60)]
            idx = valid(z, D.r[a + key])
            y = col(D.r[a + key], idx)
            fl = flags_from(z, idx, Z_MAIN)
            se = math.sqrt(y[fl].var(ddof=1) / fl.sum() + y[~fl].var(ddof=1) / (~fl).sum())
            emit(
                f"- {a}{key}: n {len(y)}, подій {int(fl.sum())}, SD подій {y[fl].std(ddof=1):.4f}, SD інших {y[~fl].std(ddof=1):.4f}, MDE iid {2.8 * se:.4f} (ворота {gate})"
            )


# ----------------------------------------------------------------------------- descriptive part


def descriptive(D: Data) -> None:
    emit("## Описово: одночасний зв'язок (зміни між закриттями NY того самого дня)")
    years = [str(y) for y in range(2018, 2027)]
    for a in CRYPTO:
        emit(f"\n### {a}: кореляція з CI (блоки {BLOCK} днів)")
        emit("| ряд | уся | n | 2018–2021 | 2022–2026 | " + " | ".join(years) + " |")
        emit("|---|---|---|---|---|" + "---|" * len(years))
        for s in SERIES:
            idx = valid(D.r[a], D.r[s], pred=lambda i: D.end[i] >= date(2018, 1, 2))
            x, y = col(D.r[s], idx), col(D.r[a], idx)
            b = boot(len(x), lambda i, x=x, y=y: pearson(x[i], y[i]))
            cells = []
            for pred in (lambda d: d < SPLIT, lambda d: d >= SPLIT):
                j = np.array([k for k, i in enumerate(idx) if pred(D.end[int(i)])], dtype=int)
                bb = boot(len(j), lambda i, j=j, x=x, y=y: pearson(x[j][i], y[j][i]))
                cells.append(f"{bb['stat']:+.2f} {ci(bb['ci95'])}")
            for yr in years:
                j = np.array([k for k, i in enumerate(idx) if str(D.end[int(i)].year) == yr], dtype=int)
                bb = boot(len(j), lambda i, j=j, x=x, y=y: pearson(x[j][i], y[j][i]))
                cells.append(f"{bb['stat']:+.2f} {ci(bb['ci95'])}")
            emit(f"| {s} | {b['stat']:+.2f} {ci(b['ci95'])} | {len(idx)} | " + " | ".join(cells) + " |")

    emit("\n### Ковзна 60-денна кореляція (лише минулі 60 пар) за роками: медіана / мін / макс")
    emit("| актив | ряд | " + " | ".join(years) + " |\n|---|---|" + "---|" * len(years))
    for a in CRYPTO:
        for s in ETFS:
            rc = rolling_corr(D.r[s], D.r[a], window=60)
            cells = []
            for yr in years:
                v = [c for i, c in enumerate(rc) if c is not None and str(D.end[i].year) == yr]
                cells.append(f"{statistics.median(v):+.2f} / {min(v):+.2f} / {max(v):+.2f}" if v else "—")
            emit(f"| {a} | {s} | " + " | ".join(cells) + " |")

    emit("\n### За фазою ФРС (вікно 120 днів, лише минулі зміни)")
    ph = [fed_phase(D.fed, D.days[i]) for i in range(D.n)]  # phase at the start of the pair window
    emit("| актив | ряд | підвищення: n, кор. [CI] | пауза | зниження |\n|---|---|---|---|---|")
    for a in CRYPTO:
        for s in ETFS:
            cells = []
            for p in ("hike", "pause", "cut"):
                idx = valid(D.r[a], D.r[s], pred=lambda i, p=p: ph[i] == p and D.end[i] >= date(2018, 1, 2))
                x, y = col(D.r[s], idx), col(D.r[a], idx)
                b = boot(len(x), lambda i, x=x, y=y: pearson(x[i], y[i]))
                cells.append(f"{len(idx)}: {f(b['stat'])} {ci(b['ci95'])}")
            emit(f"| {a} | {s} | " + " | ".join(cells) + " |")

    emit("\n### Стрес-дні Nasdaq (лог-зміна COMP ≤ ln 0.98) проти інших: бета крипти до ряду (описово)")
    emit("| актив | ряд | n стрес | β стрес | β інші | β стрес − інші [CI] | кор. стрес / інші |\n|---|---|---|---|---|---|---|")
    lt = math.log(0.98)
    for a in CRYPTO:
        for s in ETFS:
            idx = valid(D.r[a], D.r[s], D.r["COMP"], pred=lambda i: D.end[i] >= date(2018, 1, 2))
            x, y, c = col(D.r[s], idx), col(D.r[a], idx), col(D.r["COMP"], idx)
            st = c <= lt

            def bd(i: np.ndarray, x=x, y=y, st=st) -> float:
                m = st[i]
                if m.sum() < 5:
                    return float("nan")
                return beta(y[i][m], x[i][m]) - beta(y[i][~m], x[i][~m])

            b = boot(len(x), bd)
            emit(
                f"| {a} | {s} | {int(st.sum())} | {beta(y[st], x[st]):+.2f} | {beta(y[~st], x[~st]):+.2f} | {f(b['stat'])} {ci(b['ci95'])} | "
                f"{pearson(x[st], y[st]):+.2f} / {pearson(x[~st], y[~st]):+.2f} |"
            )
    emit("\n### Середня дохідність крипти того ж дня в дні сильного руху ряду (|z| ≥ 1.5, z за 60 минулими парами), %")
    emit("| актив | ряд | z ≥ 1.5: n, середнє [CI] | z ≤ −1.5: n, середнє [CI] | інші дні: середнє |\n|---|---|---|---|---|")
    for a in CRYPTO:
        for s in ETFS:
            z = D.z[(s, 60)]
            idx = valid(z, D.r[a])
            y = col(D.r[a], idx)
            cells = []
            for thr in (Z_MAIN, -Z_MAIN):
                fl = flags_from(z, idx, thr)
                b = boot(len(y), lambda i, fl=fl, y=y: float(y[i][fl[i]].mean()) if fl[i].sum() >= 3 else float("nan"))
                cells.append(f"{int(fl.sum())}: {f(b['stat'], 100)} {ci(b['ci95'], 100)}")
            other = ~(flags_from(z, idx, Z_MAIN) | flags_from(z, idx, -Z_MAIN))
            emit(f"| {a} | {s} | " + " | ".join(cells) + f" | {y[other].mean() * 100:+.2f} |")

    emit("\n### Відтворення заміру Claude (відтворення, не тест; без вердикту, не для вибору порогів)")
    idx = valid(D.r["GLD"], D.r["BTC_ynext"], pred=lambda i: D.end[i] >= date(2018, 1, 2))
    g, y = col(D.r["GLD"], idx), col(D.r["BTC_ynext"], idx)
    thr = float(np.quantile(g, 0.9))
    fl = g >= thr
    b = boot(len(y), lambda i: float(y[i][fl[i]].mean()) if fl[i].sum() >= 3 else float("nan"))
    emit(
        f"- повновибірковий топ-10 % r_GLD (поріг {thr * 100:+.2f} %, із зазиранням уперед) → BTC до закриття наступного торгового дня: "
        f"n {int(fl.sum())}, середнє {b['stat'] * 100:+.2f} % {ci(b['ci95'], 100)}, інші дні {y[~fl].mean() * 100:+.2f} %, "
        f"різниця {event_diff(fl, y) * 100:+.2f} %"
    )


def claude_diagnostic(D: Data) -> None:
    """Exploratory (added after the first run): which window / clock could give Claude's +0.8 % with n = 110."""
    emit("\n### Діагностика розбіжності із заміром Claude (дослідницьке, додано після першого прогону; не тест)")
    emit("| вибірка | годинник BTC | поріг топ-10 % | n | BTC після, % [CI] | інші дні, % |\n|---|---|---|---|---|---|")
    day_ms = 24 * HOUR_MS
    utc_next = []
    for t in D.end:
        t0 = int(datetime(t.year, t.month, t.day, tzinfo=UTC).timestamp() * 1000) + day_ms  # 00:00 UTC of t+1
        utc_next.append(forward_log_return(D.closes["BTC"], t0, day_ms))
    for lab, pred in (("2018–2026", lambda i: D.end[i] >= date(2018, 1, 2)), ("2022–2026", lambda i: D.end[i] >= SPLIT)):
        for clk, y_all in (("закриття NY → наступне закриття NY", D.r["BTC_ynext"]), ("денна свічка UTC дня t+1", utc_next)):
            idx = valid(D.r["GLD"], y_all, pred=pred)
            g, y = col(D.r["GLD"], idx), col(y_all, idx)
            thr = float(np.quantile(g, 0.9))
            fl = g >= thr
            b = boot(len(y), lambda i, fl=fl, y=y: float(y[i][fl[i]].mean()) if fl[i].sum() >= 3 else float("nan"))
            emit(
                f"| {lab} | {clk} | {thr * 100:+.2f} % | {int(fl.sum())} | {b['stat'] * 100:+.2f} {ci(b['ci95'], 100)} | {y[~fl].mean() * 100:+.2f} |"
            )


# ----------------------------------------------------------------------------- registered tests


def g_family(D: Data, a: str) -> dict[str, dict[str, Any]]:
    z = D.z[("GLD", 60)]
    out: dict[str, dict[str, Any]] = {}
    idx = valid(z, D.r[a + "_y24"])
    out["G1"] = diff_test(flags_from(z, idx, Z_MAIN), col(D.r[a + "_y24"], idx))
    out["G1"]["idx"] = idx
    idx2 = valid(z, D.r[a + "_y72"])
    out["G2"] = diff_test(flags_from(z, idx2, Z_MAIN), col(D.r[a + "_y72"], idx2))
    x, y = col(D.r["GLD"], idx), col(D.r[a + "_y24"], idx)
    b = boot(len(y), lambda i: pearson(x[i], y[i]))
    sn = shift_null(x, y, corr_stat)
    k = split_index(len(y))
    out["G3"] = {
        "n": len(y),
        "ne": len(y),
        "stat": b["stat"],
        "ci95": b["ci95"],
        "se": b["se"],
        "mde": 2.8 * b["se"],
        "p": sn["p"],
        "is": pearson(x[:k], y[:k]),
        "oos": pearson(x[k:], y[k:]),
        "ne_oos": len(y) - k,
    }
    ph = holm([out[g]["p"] for g in ("G1", "G2", "G3")])
    for g, p in zip(("G1", "G2", "G3"), ph, strict=True):
        out[g]["p_holm"] = p
        out[g]["verdict"] = g_verdict(out[g]["stat"], out[g]["ci95"], p, out[g]["is"], out[g]["oos"], out[g]["mde"], GATES[g])
    return out


def rule_r(D: Data, a: str, *, cost: float = COST, slip: float = 0.0, entry_key: str = "_y24") -> dict[str, Any]:
    z = D.z[("GLD", 60)]
    idx = valid(z, D.r[a + entry_key])
    y = col(D.r[a + entry_key], idx)
    fl = flags_from(z, idx, Z_MAIN)
    net = np.expm1(y) - 2 * (cost + slip)
    b = boot(len(y), lambda i: float(net[i][fl[i]].mean()) if fl[i].sum() >= 3 else float("nan"))
    k = split_index(len(y))
    return {
        "n": int(fl.sum()),
        "stat": b["stat"],
        "ci95": b["ci95"],
        "mde": 2.8 * b["se"] if b["se"] else float("nan"),
        "oos": float(net[k:][fl[k:]].mean()) if fl[k:].any() else float("nan"),
        "is": float(net[:k][fl[:k]].mean()) if fl[:k].any() else float("nan"),
        "any_day": float(net.mean()),
        "hit": float((net[fl] > 0).mean()) if fl.any() else float("nan"),
    }


def main_tests(D: Data) -> dict[str, Any]:
    emit("\n## Головна сім'я: золото сьогодні → BTC далі (z_GLD за 60 минулими парами, поріг 1.5; Холм m = 3)")
    emit(
        "| актив | тест | n днів | подій (OOS) | середнє подій / інших, % | D або кор. | 95 % CI | MDE | p (зсуви) | p Холма | IS | OOS | вердикт |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    res = {}
    for a in CRYPTO:
        g = g_family(D, a)
        res[a] = g
        for name in ("G1", "G2", "G3"):
            r = g[name]
            k = 1.0 if name == "G3" else 100.0
            d = 3 if name == "G3" else 2
            me = f"{r['mean_e'] * 100:+.2f} / {r['mean_o'] * 100:+.2f}" if "mean_e" in r else "—"
            emit(
                f"| {a} | {name} | {r['n']} | {r['ne']} ({r['ne_oos']}) | {me} | {f(r['stat'], k, d)} | {ci(r['ci95'], k, d)} | "
                f"{r['mde'] * k:.{d}f} | {r['p']:.3f} | {r['p_holm']:.3f} | {f(r['is'], k, d)} | {f(r['oos'], k, d)} | {r['verdict']}{'' if a == 'BTC' else ' (вторинний актив)'} |"
            )
        idx = g["G1"]["idx"]
        k = split_index(len(idx))
        emit(f"- {a}: межа OOS — {D.end[int(idx[k])]}; D і p у %, кор. G3 — без множника")
    emit("\n### Правило R: лонг на 24 год після z_GLD ≥ 1.5, net на нотіонал (= на капітал без плеча), %")
    emit(
        "| актив | варіант | угод | середнє net за угоду | 95 % CI | MDE | IS | OOS | частка > 0 | «будь-який день» net | вердикт |\n|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for a in CRYPTO:
        for lab, kw in (
            ("**головний** (0.05 % на бік)", {}),
            ("витрати ×2", {"cost": 2 * COST}),
            ("проковзування +0.02 % на бік", {"slip": 0.0002}),
            ("вхід через 1 год", {"entry_key": "_y24e1"}),
        ):
            r = rule_r(D, a, **kw)
            v = r_verdict(res[a]["G1"]["verdict"], r["ci95"], r["oos"], r["mde"], GATES["G1"]) if not kw else ""
            emit(
                f"| {a} | {lab} | {r['n']} | {r['stat'] * 100:+.3f} | {ci(r['ci95'], 100, 3)} | {r['mde'] * 100:.2f} | {r['is'] * 100:+.3f} | "
                f"{r['oos'] * 100:+.3f} | {r['hit']:.0%} | {r['any_day'] * 100:+.3f} | {v} |"
            )
            if not kw:
                res[a]["R"] = {**r, "verdict": v}
    return res


def robustness(D: Data) -> None:
    emit("\n## Стійкість G1 (D_24 = середнє подій − інших, %; друкується все)")
    emit("| актив | варіант | n днів | подій | D, % | 95 % CI | p (зсуви) | MDE |\n|---|---|---|---|---|---|---|---|")
    for a in CRYPTO:
        y24 = D.r[a + "_y24"]
        variants: list[tuple[str, Sequence[Any], Sequence[Any], float, Callable[[int], bool]]] = [
            ("головний (z60 ≥ 1.5)", D.z[("GLD", 60)], y24, Z_MAIN, lambda i: True),
            ("z ≥ 1.0", D.z[("GLD", 60)], y24, 1.0, lambda i: True),
            ("z ≥ 2.0", D.z[("GLD", 60)], y24, 2.0, lambda i: True),
            ("вікно σ 20", D.z[("GLD", 20)], y24, Z_MAIN, lambda i: True),
            ("вікно σ 120", D.z[("GLD", 120)], y24, Z_MAIN, lambda i: True),
            ("вхід через 1 год", D.z[("GLD", 60)], D.r[a + "_y24e1"], Z_MAIN, lambda i: True),
            ("до закриття наступного торгового дня", D.z[("GLD", 60)], D.r[a + "_ynext"], Z_MAIN, lambda i: True),
            ("2018–2021", D.z[("GLD", 60)], y24, Z_MAIN, lambda i: D.end[i] < SPLIT),
            ("2022–2026", D.z[("GLD", 60)], y24, Z_MAIN, lambda i: D.end[i] >= SPLIT),
            ("лише вт–чт", D.z[("GLD", 60)], y24, Z_MAIN, lambda i: D.end[i].weekday() in (1, 2, 3)),
            ("дзеркально z ≤ −1.5 (описово)", D.z[("GLD", 60)], y24, -Z_MAIN, lambda i: True),
        ]
        for lab, z, y, thr, pred in variants:
            idx = valid(z, y, pred=pred)
            r = diff_test(flags_from(z, idx, thr), col(y, idx))
            emit(
                f"| {a} | {lab} | {r['n']} | {r['ne']} | {f(r['stat'], 100)} | {ci(r['ci95'], 100)} | {r['p']:.3f} | {r['mde'] * 100:.2f} |"
            )
        # causal top decile of the past 250 pairs
        idx = valid(D.top10, y24)
        fl = col(D.top10, idx, bool)
        r = diff_test(fl, col(y24, idx))
        emit(
            f"| {a} | причинний топ-10 % (250 пар) | {r['n']} | {r['ne']} | {f(r['stat'], 100)} | {ci(r['ci95'], 100)} | {r['p']:.3f} | {r['mde'] * 100:.2f} |"
        )
        # without the 3 largest event responses
        z = D.z[("GLD", 60)]
        idx = valid(z, y24)
        fl = flags_from(z, idx, Z_MAIN)
        y = col(y24, idx)
        top = np.argsort(-np.where(fl, y, -np.inf))[:3]
        keep = np.ones(len(y), bool)
        keep[top] = False
        r = diff_test(fl[keep], y[keep])
        dropped = ", ".join(f"{D.end[int(idx[t])]} ({y[t] * 100:+.1f} %)" for t in sorted(top))
        emit(
            f"| {a} | без 3 найбільших Y подій | {r['n']} | {r['ne']} | {f(r['stat'], 100)} | {ci(r['ci95'], 100)} | {r['p']:.3f} | {r['mde'] * 100:.2f} |"
        )
        emit(f"| {a} | (відкинуто: {dropped}) | | | | | | |")
        # regression control: Y24 ~ flag + r_crypto(t) + r_COMP(t)
        idx = valid(z, y24, D.r[a], D.r["COMP"])
        fl = flags_from(z, idx, Z_MAIN).astype(float)
        y, rc, rm = col(y24, idx), col(D.r[a], idx), col(D.r["COMP"], idx)
        b = boot(len(y), lambda i, y=y, fl=fl, rc=rc, rm=rm: ols_coefs(y[i], [fl[i], rc[i], rm[i]])[1])
        emit(
            f"| {a} | регресія: коеф. індикатора при r_{a}(t), r_COMP(t) | {len(y)} | {int(fl.sum())} | {f(b['stat'], 100)} | {ci(b['ci95'], 100)} | — | {2.8 * b['se'] * 100:.2f} |"
        )


def check_5a(D: Data, res: dict[str, Any]) -> None:
    emit("\n## Перевірка 5a: два активи")
    z = D.z[("GLD", 60)]
    idx = valid(z, D.r["BTC_y24"], D.r["ETH_y24"])
    fl = flags_from(z, idx, Z_MAIN)
    yb, ye = col(D.r["BTC_y24"], idx), col(D.r["ETH_y24"], idx)
    b = boot(len(yb), lambda i: event_diff(fl[i], ye[i]) - event_diff(fl[i], yb[i]))
    emit(f"- D_ETH − D_BTC на спільних днях: {f(b['stat'], 100)} % {ci(b['ci95'], 100)} (n {len(yb)}, подій {int(fl.sum())})")
    q = cochran_q([res[a]["G1"]["stat"] for a in CRYPTO], [res[a]["G1"]["se"] for a in CRYPTO])
    emit(
        f"- Q Кохрена (2 активи, формально): Q {q['Q']:.2f}, df {q['df']}, p {q['p']:.3f}, I² {q['I2']:.0%}; стійкість «топ-активів» на 2 активах не має сенсу"
    )


def exploratory(D: Data) -> None:
    emit("\n## Дослідницька сім'я (без вердикту; Холм окремо, m = 4): долар і нафта сьогодні → BTC за 24 год")
    emit("| сигнал | n днів | подій | середнє подій / інших, % | D, % | 95 % CI | p (зсуви) | p Холма |\n|---|---|---|---|---|---|---|---|")
    rows = []
    for s in ("USO", "UUP"):
        for thr in (Z_MAIN, -Z_MAIN):
            z = D.z[(s, 60)]
            idx = valid(z, D.r["BTC_y24"])
            rows.append((f"z_{s} {'≥' if thr > 0 else '≤'} {thr:+.1f}", diff_test(flags_from(z, idx, thr), col(D.r["BTC_y24"], idx))))
    ph = holm([r["p"] for _, r in rows])
    for (lab, r), p in zip(rows, ph, strict=True):
        emit(
            f"| {lab} | {r['n']} | {r['ne']} | {r['mean_e'] * 100:+.2f} / {r['mean_o'] * 100:+.2f} | {f(r['stat'], 100)} | {ci(r['ci95'], 100)} | "
            f"{r['p']:.3f} | {p:.3f} |"
        )


def stage_report(D: Data, out: str) -> None:
    emit(f"# Сирий вивід verify_macro_b1b.py ({datetime.now(UTC).isoformat(timespec='seconds')})")
    emit(f"Пари {D.end[0]} … {D.end[-1]}, n = {D.n}; бутстреп рухомими блоками {BLOCK} днів, {REPS} повторів, seed {SEED}.")
    emit(f"Виключено дні UUP (ex-div; відхилення від реєстрації, дефект даних, додано після рев'ю): {D.split_dropped['UUP']}")
    t0 = time.perf_counter()
    res = main_tests(D)
    print(f"[main {time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB]", flush=True)
    robustness(D)
    check_5a(D, res)
    exploratory(D)
    print(f"[robust {time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB]", flush=True)
    descriptive(D)
    claude_diagnostic(D)
    emit(f"\n_час {time.perf_counter() - t0:.0f} с, peak RSS {rss_mb():.0f} MB_")
    if out:
        Path(out).expanduser().write_text("\n".join(L) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("prereg", "fetch", "sanity", "power", "report"), required=True)
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/macro_b1b")
    ap.add_argument("--b1-cache", default="~/.cache/tradedesk-agents/macro_regimes")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    b1 = Path(a.b1_cache).expanduser()
    if a.stage == "prereg":
        stage_prereg(cache)
    elif a.stage == "fetch":
        asyncio.run(fetch(cache))
    elif a.stage in ("sanity", "power"):
        D = Data(cache, b1)
        (stage_sanity if a.stage == "sanity" else stage_power)(D)
        if a.out:
            Path(a.out).expanduser().write_text("\n".join(L) + "\n")
    else:
        stage_report(Data(cache, b1), a.out)


if __name__ == "__main__":
    main()
