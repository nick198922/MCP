"""KX-2 (KillaXBT "time-based pivots"): short BTC from the 14th, long BTC from the 1st, held 14 or 3 days.

Registration: docs/ideas/IDEA-20261113-x-traders.md, section 5.5 (written and committed before any P&L).

Stages:
  fetch  — BTC daily candles through the runtime providers (Bitstamp BTC/USD for 2013-12 ... 2017-10, Binance spot
           BTC/USDT and Binance USD-M perp BTC/USDT from their listings) and Binance BTCUSDT funding, one JSON file
           per page in --cache-dir.
  zero   — counts, gaps, venue sanity, the known-number check and the ex-ante MDE gate. Prints no mean return.
  report — the registered tests (only after the registration commit).

Network only via build_server() -> rt.exchanges. No keys, no orders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import resource
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.analytics import kx2_calendar as kc  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import cochran_q  # noqa: E402

DAY = kc.DAY_MS
SEED = 20261124
N_BOOT = 10_000
N_PLACEBO = 2_000
FEE_RT = 2 * (0.00055 + 0.0002)  # taker 0.055 % + slippage 0.02 %, each side
M_TESTS = 4
# name: (day of month, side, hold days)
HYPS: dict[str, tuple[int, str, int]] = {
    "H14_14": (14, "short", 14),
    "H14_3": (14, "short", 3),
    "H1_14": (1, "long", 14),
    "H1_3": (1, "long", 3),
}
INTEREST = {14: 0.04, 3: 0.02}  # registration 5.5: below these MDE values a test is run, above them it is ◯ by power


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


# entries; the author's selection may reach back to 2025-03 ("7 of 11" on 2026-02-11, CPI pivot 2025-04-09), so the
# primary sample ends with the 2025-02 entries (registration 5.5, critic B fix round 2026-11-24, blocker 4)
PRIMARY = (ms("2014-01-01"), ms("2025-03-01"))
SPLICE = ms("2017-09-01")  # entries before: Bitstamp BTC/USD; from here: Binance BTC/USDT spot
HALF = ms("2020-01-01")
MACRO = (ms("2022-01-01"), ms("2025-03-01"))
AUTHOR = (ms("2025-03-01"), ms("2026-11-01"))
FULL = (ms("2014-01-01"), ms("2026-11-01"))
P100 = (ms("2021-01-01"), ms("2025-03-01"))
P100_IS_END = ms("2023-01-01")
FETCH_END = ms("2026-11-24")
SOURCES = {
    "bitstamp": ("bitstamp", "BTC/USD", ms("2013-12-01"), ms("2017-10-15")),
    "binance": ("binance", "BTC/USDT", ms("2017-08-17"), FETCH_END),
    "perp": ("binanceusdm", "BTC/USDT:USDT", ms("2019-09-08"), FETCH_END),
}
CACHE = Path.home() / ".cache/tradedesk-agents"
OUT: list[str] = []


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def emit(s: str) -> None:
    print(s, flush=True)
    OUT.append(s)


def day(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- fetch


async def fetch(cache: Path, limit_pages: int | None) -> None:
    from tradedesk.server import build_server

    _, rt = build_server()
    t0 = time.perf_counter()
    pages = 0
    try:
        for name, (venue, sym, start, end) in SOURCES.items():
            d = cache / name
            d.mkdir(parents=True, exist_ok=True)
            since = start
            while since < end:
                p = d / f"{since}.json"
                if p.exists():
                    rows = json.loads(p.read_text())
                else:
                    f = await rt.exchanges.ohlcv(venue, sym, "1d", 1000, since_ms=since, until_ms=end)
                    rows = [r for r in (f.data or []) if since <= r[0] < end]
                    p.write_text(json.dumps(rows))
                    pages += 1
                    emit(f"[fetch] {name} since {day(since)}: {len(rows)} rows {time.perf_counter() - t0:.0f}s rss {rss_mb():.0f} MB")
                    await asyncio.sleep(0.5)
                    if limit_pages and pages >= limit_pages:
                        return
                if not rows:
                    break
                since = int(rows[-1][0]) + DAY
        d = cache / "funding"
        d.mkdir(parents=True, exist_ok=True)
        since = ms("2019-09-01")
        while since < FETCH_END:
            p = d / f"{since}.json"
            if p.exists():
                rows = json.loads(p.read_text())
            else:
                f = await rt.exchanges.funding_rate_history("binanceusdm", "BTC/USDT:USDT", since, 1000, until_ms=FETCH_END)
                rows = sorted(
                    [int(x["timestamp"]), float(x["fundingRate"])]
                    for x in (f.data or [])
                    if x.get("timestamp") is not None and x.get("fundingRate") is not None and since <= int(x["timestamp"]) < FETCH_END
                )
                p.write_text(json.dumps(rows))
                pages += 1
                emit(f"[fetch] funding since {day(since)}: {len(rows)} rows {time.perf_counter() - t0:.0f}s rss {rss_mb():.0f} MB")
                await asyncio.sleep(0.5)
            if not rows:
                break
            since = int(rows[-1][0]) + 1
    finally:
        await rt.aclose()
    emit(f"[fetch] done, {pages} new pages, {time.perf_counter() - t0:.0f}s")


# ---------------------------------------------------------------- load


def load_series(cache: Path, name: str) -> dict[str, np.ndarray]:
    rows: dict[int, list[float]] = {}
    for p in sorted((cache / name).glob("*.json")):
        for r in json.loads(p.read_text()):
            rows[int(r[0])] = [float(x) for x in r[1:5]]
    t = np.array(sorted(rows), dtype="int64")
    a = np.array([rows[int(x)] for x in t], dtype=float).reshape(-1, 4)
    return {"t": t, "o": a[:, 0], "h": a[:, 1], "l": a[:, 2], "c": a[:, 3]}


def load_funding(cache: Path) -> tuple[np.ndarray, np.ndarray]:
    pts: dict[int, float] = {}
    for p in sorted((cache / "funding").glob("*.json")):
        for t, r in json.loads(p.read_text()):
            pts[int(t)] = float(r)
    raw = np.array(sorted(pts), dtype="int64")
    t = kc.round_to_hour(raw)
    assert np.all(np.diff(t) > 0), "funding stamps collide after rounding to the hour"
    return t, np.array([pts[int(x)] for x in raw], dtype=float)


class Btc:
    """Spliced BTC daily series: Bitstamp for entries before SPLICE, Binance spot after (or one venue if forced)."""

    def __init__(self, cache: Path, force: str | None = None) -> None:
        self.s = {n: load_series(cache, n) for n in SOURCES}
        self.f_t, self.f_r = load_funding(cache)
        self.force = force

    def venue(self, t_entry: int) -> str:
        return self.force or ("bitstamp" if t_entry < SPLICE else "binance")

    def trade(self, t_entry: int, side: str, hold: int, fee: float = FEE_RT, funding: bool = True) -> tuple[float, float]:
        """(net return, price return) of one trade; NaN if a bar is missing."""
        s = self.s[self.venue(t_entry)]
        pr = kc.window_return(s["t"], s["o"], t_entry, hold)
        f = kc.funding_for_side(self.f_t, self.f_r, t_entry, t_entry + hold * DAY, side, int(self.f_t[0])) if funding else 0.0
        return kc.net_return(pr, side, f, fee), pr

    def entries(self, dom: int, lo: int, hi: int, shift: int = 0) -> list[int]:
        out: list[int] = []
        for v, (a, b) in (("bitstamp", (lo, min(hi, SPLICE))), ("binance", (max(lo, SPLICE), hi))):
            if self.force:
                v, a, b = self.force, lo, hi
            if a < b:
                out += kc.entry_times(self.s[v]["t"], dom, a, b, shift)
            if self.force:
                break
        return sorted(set(out))

    def months(self, lo: int, hi: int) -> list[int]:
        return list(range(int(kc.month_index(np.array([lo]))[0]), int(kc.month_index(np.array([hi - 1]))[0]) + 1))

    def matrix(self, months: list[int], side: str, hold: int, shift: int = 0, fee: float = FEE_RT) -> np.ndarray:
        """Signed net returns for entries on days 1..28 (+ shift) of each month."""
        out = np.full((len(months), kc.PLACEBO_DAYS), np.nan)
        for k, m in enumerate(months):
            base = int(np.datetime64(int(m), "M").astype("datetime64[D]").astype("datetime64[ms]").astype("int64"))
            for d in range(kc.PLACEBO_DAYS):
                out[k, d] = self.trade(base + (d + shift) * DAY, side, hold, fee)[0]
        return out


def expected_months(lo: int, hi: int) -> int:
    return int(kc.month_index(np.array([hi - 1]))[0] - kc.month_index(np.array([lo]))[0] + 1)


# ---------------------------------------------------------------- P100 panel (secondary)


def p100_universe(a: argparse.Namespace) -> tuple[dict[str, tuple[np.ndarray, np.ndarray]], dict[int, set[str]], np.ndarray]:
    """Daily open bars per coin, the previous-day top-100 (>= $5M quote volume) per day, and all bar days.

    Eligible USD-M perps (TradFi excluded by ``eligible_syms``); the universe is the passport's symbols.json, which
    keeps delisted coins (FTT, SRM, LUNA, BTS, SC ...). Nothing is cached: the panel is rebuilt from code each run."""
    import verify_course_s14_ema as s1

    t0 = time.time()
    syms = s1.eligible_syms(a.data_dir)
    bars: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    by_day: dict[int, list[tuple[float, str]]] = defaultdict(list)
    for k, s in enumerate(syms, 1):
        rows: dict[int, tuple[float, float]] = {}
        for mo in s1.listing(a.data_dir, s):
            f = (a.s1_cache / "k1d" / s / f"{mo}.json") if mo < "2021-10" else (a.data_dir / "k1d" / s / f"{mo}.json")
            if f.exists():
                for r in s1.rd(f) or []:
                    rows[int(r[0])] = (float(r[1]), float(r[6] if len(r) > 6 else r[5]))
        if not rows:
            continue
        t = np.array(sorted(rows), dtype="int64")
        bars[s] = (t, np.array([rows[int(x)][0] for x in t]))
        for x in t:
            by_day[int(x)].append((rows[int(x)][1], s))
        if k % 100 == 0:
            emit(f"  [p100] loaded {k}/{len(syms)} coins {time.time() - t0:.0f}s rss {rss_mb():.0f} MB")
    top = {d: {s for v, s in sorted(xs, reverse=True)[:100] if v >= 5e6} for d, xs in by_day.items()}
    emit(f"  [p100] universe loaded {time.time() - t0:.0f}s rss {rss_mb():.0f} MB")
    return bars, top, np.array(sorted(by_day), dtype="int64")


def p100_counts(a: argparse.Namespace) -> dict[str, list[tuple[int, str]]]:
    """Stage 0 only: (entry ms, coin) pairs whose entry and exit bars both exist. No price is read, no return computed."""
    bars, top, days = p100_universe(a)
    out: dict[str, list[tuple[int, str]]] = {}
    for h, (dom, _side, hold) in HYPS.items():
        out[h] = []
        for te in kc.entry_times(days, dom, P100[0], AUTHOR[1]):
            for s in sorted(top.get(te - DAY, ())):
                t = bars[s][0]
                i, j = np.searchsorted(t, [te, te + hold * DAY])
                if i < t.size and j < t.size and t[i] == te and t[j] == te + hold * DAY:
                    out[h].append((te, s))
    return out


def p100_panel(a: argparse.Namespace) -> dict[str, list[list[Any]]]:
    """Report only: per hypothesis [entry ms, coin, net return] (fees only; no funding for alts). Built in memory."""
    bars, top, days = p100_universe(a)
    out: dict[str, list[list[Any]]] = {}
    for h, (dom, side, hold) in HYPS.items():
        rows_h: list[list[Any]] = []
        for te in kc.entry_times(days, dom, P100[0], AUTHOR[1]):
            for s in sorted(top.get(te - DAY, ())):
                t, o = bars[s]
                r = kc.window_return(t, o, te, hold)
                if math.isfinite(r):
                    rows_h.append([te, s, kc.net_return(r, side, 0.0, FEE_RT)])
        out[h] = rows_h
    return out


# ---------------------------------------------------------------- stage zero


def gate(b: Btc) -> dict[str, dict[str, float]]:
    """Ex-ante MDE per hypothesis against the NET target (gross interest minus fee and expected proxy funding per side).

    sd: all-start-day h-day price returns over the primary period (direction-free, no means). The short's funding
    credit share is the count of primary trades with a real perp funding history over n (a count, not an outcome)."""
    n = expected_months(*PRIMARY)
    sds: dict[int, tuple[float, float]] = {}
    for hold in (14, 3):
        xs = []
        for lo, hi, v in ((PRIMARY[0], SPLICE, "bitstamp"), (SPLICE, PRIMARY[1], "binance")):
            s = b.s[v]
            idx = np.flatnonzero((s["t"] >= lo) & (s["t"] < hi))
            xs += [kc.window_return(s["t"], s["o"], int(s["t"][i]), hold) for i in idx]
        x = np.array(xs)
        x = x[np.isfinite(x)]
        sds[hold] = (float(x.std(ddof=1)), 0.12 * math.sqrt(hold / 14))
    out: dict[str, dict[str, float]] = {}
    for h, (dom, side, hold) in HYPS.items():
        sd, sd_lib = sds[hold]
        mde = kc.mde80(max(sd, sd_lib), n, M_TESTS)
        share = sum(1 for x in b.entries(dom, *PRIMARY) if x >= int(b.f_t[0])) / n
        tgt = kc.net_target(INTEREST[hold], side, hold, FEE_RT, share)
        tgt0 = kc.net_target(INTEREST[hold], side, hold, FEE_RT, 0.0)
        out[h] = {
            "sd": sd,
            "sd_lib": sd_lib,
            "n": n,
            "mde": mde,
            "interest": INTEREST[hold],
            "share": share,
            "target": tgt,
            "target_no_credit": tgt0,
            "pass": float(mde <= tgt),
        }
    return out


def stage_zero(a: argparse.Namespace) -> None:
    b = Btc(a.cache_dir)
    emit("== KX-2 stage 0: counts, gaps, sanity, MDE gate (no returns) ==")
    for n, s in b.s.items():
        t = s["t"]
        gaps = int(np.sum(np.diff(t) != DAY))
        emit(
            f"{n}: {t.size} daily bars {day(int(t[0]))} … {day(int(t[-1]))}; non-1d steps {gaps}; off-midnight {int(np.sum(t % DAY != 0))}"
        )
    f_t, _ = b.f_t, b.f_r
    st = np.diff(f_t) / 3_600_000
    emit(f"funding: {f_t.size} points {day(int(f_t[0]))} … {day(int(f_t[-1]))}; step h median {np.median(st):.1f}, max {st.max():.1f}")
    bn = b.s["binance"]
    i = int(np.searchsorted(bn["t"], ms("2021-11-10")))
    emit(f"known number: Binance BTC/USDT 2021-11-10 high {bn['h'][i]:.2f} (reference 69000.00)")
    bs = b.s["bitstamp"]
    common = np.intersect1d(bs["t"], bn["t"])
    if common.size:
        ia, ib = np.searchsorted(bs["t"], common), np.searchsorted(bn["t"], common)
        dev = np.abs(bs["c"][ia] / bn["c"][ib] - 1)
        emit(
            f"Bitstamp vs Binance close, {common.size} common days {day(int(common[0]))} … {day(int(common[-1]))}: median |diff| {np.median(dev):.4%}, max {dev.max():.4%}"
        )
    pp = b.s["perp"]
    common = np.intersect1d(pp["t"], bn["t"])
    ia, ib = np.searchsorted(pp["t"], common), np.searchsorted(bn["t"], common)
    dev = np.abs(pp["o"][ia] / bn["o"][ib] - 1)
    emit(f"perp vs spot open, {common.size} days: median |diff| {np.median(dev):.4%}, p99 {np.quantile(dev, 0.99):.4%}")
    emit("")
    emit("trades per hypothesis (valid entry and exit bars; expected = calendar months):")
    samples = {
        "primary 2014-01…2025-02": PRIMARY,
        "  Bitstamp 2014-01…2017-08": (PRIMARY[0], SPLICE),
        "  Binance 2017-09…2025-02": (SPLICE, PRIMARY[1]),
        "  half 1 2014-01…2019-12": (PRIMARY[0], HALF),
        "  half 2 2020-01…2025-02": (HALF, PRIMARY[1]),
        "macro slice 2022-01…2025-02": MACRO,
        "author window 2025-03…2026-10": AUTHOR,
    }
    for h, (dom, side, hold) in HYPS.items():
        parts = []
        for lab, (lo, hi) in samples.items():
            e = b.entries(dom, lo, hi)
            ok = sum(1 for x in e if math.isfinite(b.trade(x, side, hold)[0]))
            parts.append(f"{lab.strip()} {ok}/{expected_months(lo, hi)}")
        fund = sum(1 for x in b.entries(dom, *PRIMARY) if x >= int(b.f_t[0]))
        emit(f"{h} ({side} from day {dom}, {hold} d): " + "; ".join(parts) + f"; primary trades with real funding {fund}")
    emit("")
    g = gate(b)
    emit(
        "MDE gate (one-sided, alpha 0.05 / 4 = Holm first step, power 0.8; sd = max(sample all-day sd, LIB 12 % x sqrt(h/14));"
        " net target = interest - fee - long pays 0.01 %/8h | short gets it on the real-funding share):"
    )
    for h, x in g.items():
        st_ = "testable" if x["pass"] else "◯ by power"
        emit(
            f"  {h}: n {x['n']}, sd all-day {x['sd']:.4f}, sd LIB {x['sd_lib']:.4f}, MDE80 {x['mde']:.4f}; gross {x['interest']:.4f},"
            f" real-funding share {x['share']:.3f}, net target {x['target']:.4f} (no credit {x['target_no_credit']:.4f}) → {st_}"
        )
    emit("")
    pan = p100_counts(a)
    emit(
        "P100 secondary, counts only (coin-trades with entry and exit bars; dates; median coins per date), 2021-01…2025-02 | author window:"
    )
    for h, rows in pan.items():
        for lab, (lo, hi) in (("pre", P100), ("author", AUTHOR)):
            rr = [r for r in rows if lo <= r[0] < hi]
            per = defaultdict(int)
            for r in rr:
                per[r[0]] += 1
            med = float(np.median(list(per.values()))) if per else 0.0
            emit(f"  {h} {lab}: {len(rr)} coin-trades, {len(per)} dates, median {med:.0f} coins/date, {len({r[1] for r in rr})} coins")
    emit(f"peak rss {rss_mb():.0f} MB")


# ---------------------------------------------------------------- stage report


def fmt(r: dict[str, float]) -> str:
    return f"{r['m']:+.4f} [{r['lo']:+.4f}; {r['hi']:+.4f}] n {int(r['n'])}"


def series_of(
    b: Btc, dom: int, side: str, hold: int, lo: int, hi: int, shift: int = 0, fee: float = FEE_RT, funding: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    e = b.entries(dom, lo, hi, shift)
    r = np.array([b.trade(x, side, hold, fee, funding)[0] for x in e])
    t = np.array(e, dtype="int64")
    ok = np.isfinite(r)
    return t[ok], r[ok]


def ci(x: np.ndarray, k: int = 0) -> dict[str, float]:
    return kc.mean_ci(x, N_BOOT, SEED + k)


def per_asset(rows: list[list[Any]]) -> str:
    by: dict[str, list[float]] = defaultdict(list)
    is_: dict[str, list[float]] = defaultdict(list)
    oos: dict[str, list[float]] = defaultdict(list)
    for t, s, r in rows:
        by[s].append(r)
        (is_ if t < P100_IS_END else oos)[s].append(r)
    coins = sorted(s for s, v in by.items() if len(v) >= 12)
    means = np.array([np.mean(by[s]) for s in coins])
    ses = np.array([np.std(by[s], ddof=1) / math.sqrt(len(by[s])) for s in coins])
    q, df, i2 = cochran_q(means, ses)
    lines = [f"    5a: coins with >= 12 trades {len(coins)}; Cochran Q {q:.1f} df {df} I² {i2:.2f}"]
    order = sorted(coins, key=lambda s: -float(np.mean(by[s])))
    for s in order[:5] + order[-5:]:
        lines.append(f"      {s}: n {len(by[s])}, mean {np.mean(by[s]):+.4f}, se {np.std(by[s], ddof=1) / math.sqrt(len(by[s])):.4f}")
    rank = [s for s in sorted(is_, key=lambda s: -float(np.mean(is_[s]))) if len(is_[s]) >= 6]
    top = set(rank[:10])
    a_top = [r for s in top for r in oos.get(s, [])]
    a_rest = [r for s in rank[10:] for r in oos.get(s, [])]
    both = [s for s in rank if len(oos.get(s, [])) >= 6]
    rho = float("nan")
    if len(both) >= 5:
        ri = np.argsort(np.argsort([np.mean(is_[s]) for s in both]))
        ro = np.argsort(np.argsort([np.mean(oos[s]) for s in both]))
        rho = float(np.corrcoef(ri, ro)[0, 1])
    lines.append(
        f"      persistence: top-10 IS coins OOS mean {np.mean(a_top) if a_top else float('nan'):+.4f} (n {len(a_top)}) vs rest "
        f"{np.mean(a_rest) if a_rest else float('nan'):+.4f} (n {len(a_rest)}); Spearman IS/OOS {rho:+.2f} over {len(both)} coins"
    )
    return "\n".join(lines)


def stage_report(a: argparse.Namespace) -> None:
    b = Btc(a.cache_dir)
    g = gate(b)
    emit("== KX-2 report (registration 5.5) ==")
    res: dict[str, dict[str, Any]] = {}
    for k, (h, (dom, side, hold)) in enumerate(HYPS.items()):
        if not g[h]["pass"]:
            emit(f"{h}: ◯ by power (MDE80 {g[h]['mde']:.4f} > net target {g[h]['target']:.4f}); no means computed")
            continue
        t, r = series_of(b, dom, side, hold, *PRIMARY)
        months = b.months(*PRIMARY)
        mat = b.matrix(months, side, hold)
        col = mat[:, dom - 1]
        assert np.allclose(np.sort(col[np.isfinite(col)]), np.sort(r)), "matrix column != trade series"
        plc = kc.placebo_means(mat, dom, N_PLACEBO, SEED + 100 + k)
        res[h] = {"r": ci(r, k), "t": t, "x": r, "plc": plc, "plc_p": kc.placebo_p(float(r.mean()), plc), "mat": mat}
    # Holm over all 4 registered hypotheses (m = 4), for the trade p and, separately, the placebo p; a hypothesis that
    # is ◯ by power enters with p = 1, so it never loosens the correction for the others.
    names = list(HYPS)
    for key, out_key in (("p", "holm"), ("plc_p", "plc_holm")):
        raw = [(res[h]["r"]["p"] if key == "p" else res[h]["plc_p"]) if h in res else 1.0 for h in names]
        for h, p in zip(names, holm(raw), strict=True):
            if h in res:
                res[h][out_key] = p
    pan = p100_panel(a)
    for k, (h, x) in enumerate(res.items()):
        dom, side, hold = HYPS[h]
        r, t, xs = x["r"], x["t"], x["x"]
        emit(f"\n{h} ({side} from the 00:00 UTC open of day {dom}, hold {hold} d; net of fees {FEE_RT:.4f} and funding)")
        emit(f"  primary: {fmt(r)}; p one-sided {r['p']:.4f}; Holm (m 4) {x['holm']:.4f}")
        emit(
            f"  placebo (random other day 1..28 per month, {N_PLACEBO} sets): mean {np.mean(x['plc']):+.4f}, Δ {r['m'] - np.mean(x['plc']):+.4f}, p {x['plc_p']:.4f}, Holm (m 4) {x['plc_holm']:.4f}"
        )
        h1 = xs[t < HALF]
        h2 = xs[t >= HALF]
        st15 = series_of(b, dom, side, hold, *PRIMARY, fee=1.5 * FEE_RT)[1]
        nof = series_of(b, dom, side, hold, *PRIMARY, funding=False)[1]
        no5 = np.sort(xs)[:-5]
        emit(f"  half 1 {fmt(ci(h1, 10 + k))}; half 2 {fmt(ci(h2, 20 + k))}")
        emit(
            f"  costs x1.5 {np.mean(st15):+.4f}; price minus fees, no funding {np.mean(nof):+.4f}; without top-5 months {np.mean(no5):+.4f}"
        )
        years = defaultdict(list)
        for ti, v in zip(t, xs, strict=True):
            years[day(int(ti))[:4]].append(v)
        emit("  years: " + ", ".join(f"{y} {np.mean(v):+.3f}({len(v)})" for y, v in sorted(years.items())))
        conds = {
            "CI lo > 0": r["lo"] > 0,
            "Holm < 0.05": x["holm"] < 0.05,
            "placebo Holm < 0.05": x["plc_holm"] < 0.05,
            "both halves > 0": h1.mean() > 0 and h2.mean() > 0,
            "costs x1.5 > 0": np.mean(st15) > 0,
            "no funding > 0": np.mean(nof) > 0,
            "without top-5 > 0": np.mean(no5) > 0,
        }
        p_rows = [rr for rr in pan[h] if P100[0] <= rr[0] < P100[1]]
        per_date: dict[int, list[float]] = defaultdict(list)
        for te, _, v in p_rows:
            per_date[te].append(v)
        pm = np.array([np.mean(v) for _, v in sorted(per_date.items())])
        pr = ci(pm, 40 + k)
        emit(f"  P100 secondary (per-date cross-section mean, 2021-01…2025-02, fees only): {fmt(pr)}")
        emit(per_asset(p_rows))
        allc = all(conds.values())
        if allc and pr["m"] > 0:
            v = "✓"
        elif allc or (r["m"] > 0 and (conds["Holm < 0.05"] or conds["placebo Holm < 0.05"])):
            v = "≈"
        else:
            v = "✗"
        excl = r["hi"] < g[h]["target"]
        emit(
            "  conditions: "
            + "; ".join(f"{c} {'yes' if ok else 'no'}" for c, ok in conds.items())
            + f"; P100 > 0 {'yes' if pr['m'] > 0 else 'no'}"
        )
        emit(
            f"  VERDICT {h}: {v}; net target {g[h]['target']:.4f} (gross {INTEREST[hold]:.2f}) excluded by the CI upper bound:"
            f" {'yes' if excl else 'no'}"
        )
        # sensitivity (registered list; does not change the verdict)
        emit("  sensitivity (does not change the verdict):")
        for lab, kw in (
            ("entry at the close of the day (open of D+1)", {"shift": 1}),
            ("hold 7 d", {"hold": 7}),
            ("macro slice 2022-01…2025-02", {"lo": MACRO[0], "hi": MACRO[1]}),
            ("author window 2025-03…2026-10 (author's selection)", {"lo": AUTHOR[0], "hi": AUTHOR[1]}),
            ("full 2014-01…2026-10", {"lo": FULL[0], "hi": FULL[1]}),
        ):
            sx = series_of(b, dom, side, kw.get("hold", hold), kw.get("lo", PRIMARY[0]), kw.get("hi", PRIMARY[1]), kw.get("shift", 0))[1]
            emit(f"    {lab}: {fmt(ci(sx, 60 + k))}")
        for force, lo in (("binance", SPLICE), ("perp", ms("2019-10-01"))):
            bf = Btc(a.cache_dir, force=force)
            sx = series_of(bf, dom, side, hold, lo, PRIMARY[1])[1]
            emit(f"    {force} only from {day(lo)}: {fmt(ci(sx, 70 + k))}")
        cm = np.nanmean(x["mat"], axis=0)
        rank = int(np.sum(cm > cm[dom - 1])) + 1
        emit(
            f"    day-of-month table (signed net mean by entry day 1..28): target rank {rank}/28; "
            + " ".join(f"{d + 1}:{v:+.3f}" for d, v in enumerate(cm))
        )
    emit(f"\npeak rss {rss_mb():.0f} MB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("fetch", "zero", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=CACHE / "kx2_calendar")
    ap.add_argument("--s1-cache", type=Path, default=CACHE / "course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=CACHE / "funding_crowding")
    ap.add_argument("--limit", type=int, default=None, help="fetch: stop after this many new pages (timing sample)")
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "fetch":
        asyncio.run(fetch(a.cache_dir, a.limit))
    elif a.stage == "zero":
        stage_zero(a)
    else:
        stage_report(a)
    (a.cache_dir / f"{a.stage}.txt").write_text("\n".join(OUT) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
