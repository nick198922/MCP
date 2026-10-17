"""Course setups, step 3 (IDEA-20261016-course-setups-crypto): S18 / H4 trend following after price reclaims all EMAs
(daily EMA 100 and 200, 4h EMA 200), exit on a daily close below the daily EMA 100; against random entries in the same
regime (P_reg), random entries held as long (P_time), all regime days (P_all) and buy-and-hold.

Rules and thresholds are fixed in the card's section "Попередня реєстрація — крок 3 (S18)". Stages:
  uv run python scripts/verify_course_step3_s18.py --stage prereg
  uv run python scripts/verify_course_step3_s18.py --stage count  --out ~/.cache/tradedesk-agents/course_step3_s18/count.md
  uv run python scripts/verify_course_step3_s18.py --stage report --out ~/.cache/tradedesk-agents/course_step3_s18/report.md
``count`` (stage 0) counts signals without any trade outcome. ``--symbols N`` runs the first N symbols only (timing).
No network: the 1h bars and the universe come from the step-1 cache (--s14-cache, filled from data.binance.vision via
rt.binance_vision), funding from the funding-crowding cache (--data-dir). Per-trade rows go to --cache-dir/trades.json.
No keys, no orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (step-1 loaders, universe, period)

from tradedesk.analytics.course_s14_ema import matched_baseline  # noqa: E402
from tradedesk.analytics.course_s18_trend import (  # noqa: E402
    DAY_MS,
    MARKET_SIDE,
    S18Ind,
    s18_indicators,
    signal_days,
    simulate,
)
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means, holm, without_top_days  # noqa: E402
from tradedesk.analytics.session_stop_hunt import cluster_bootstrap_diff  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261016-course-setups-crypto.md"
OUT: dict[str, Any] = {"fh": None}
SEED = 20261017
PER_SIGNAL = 3
WINDOW_MS = 180 * DAY_MS
START_MS, END_MS, OOS_MS = s1.START_MS, s1.END_MS, s1.OOS_MS
DATA_END_MS = s1.ms("2026-10-01T00:00")
FUND_FROM_MS = s1.ms("2021-10-01T00:00")
FUND_8H = 0.0001
STEP_P = (0.985, 0.280)  # primary p of steps 1 and 2 (Holm family of the card)
MIN_DATES, MDE_GATE, MIN_EFFECT, MIN_OOS_DATES = 100, 0.10, 0.03, 20
PRIMARY = "L60_e100"
LOOKBACKS = (30, 60, 90)
EXITS = ("e100", "e200", "h4")
VARIANTS = [f"L{L}_{x}" for L in LOOKBACKS for x in EXITS] + ["strict60_e100", "stop60_e100"]
WIDE_NOTE = " — ДОСЛІДНИЦЬКИЙ прогін (розширене членство, задане після стадії 0; не впливає на вердикт)"
EXIT_LABEL = {"e100": "денна EMA100", "e200": "денна EMA200", "h4": "4h-EMA200 (4h)"}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


day_of = s1.day_of


def parse_variant(v: str) -> tuple[int, str, bool, bool]:
    """(lookback, exit, strict, stop)."""
    if v == "strict60_e100":
        return 60, "e100", True, False
    if v == "stop60_e100":
        return 60, "e100", False, True
    a, b = v.split("_")
    return int(a[1:]), b, False, False


# ---------------------------------------------------------------- prereg


def stage_prereg() -> None:
    t = CARD.read_text()
    a = t.index("## Попередня реєстрація — крок 3")
    b = t.index("\n## ", a + 1)
    sec = t[a:b]
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec.encode()).hexdigest()} bytes={len(sec.encode())}")


# ---------------------------------------------------------------- membership


def member_intervals(uni: dict[str, list[str]]) -> dict[str, np.ndarray]:
    by: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for mo, mem in uni.items():
        y, m = int(mo[:4]), int(mo[5:])
        y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
        a, b = s1.ms(f"{mo}-01T00:00"), s1.ms(f"{y2:04d}-{m2:02d}-01T00:00")
        for s in mem:
            by[s].append((a, b))
    return {s: np.array(sorted(v), dtype=np.int64) for s, v in by.items()}


def widen(uni: dict[str, list[str]], months: int = 12) -> dict[str, list[str]]:
    """Exploratory universe: a symbol is a member in month m if it was in the registered universe in any of the
    months m-11 .. m (point-in-time: no month after m is used)."""
    keys = sorted(uni)
    out: dict[str, list[str]] = {}
    for n, mo in enumerate(keys):
        mem: set[str] = set()
        for k in keys[max(0, n - months + 1) : n + 1]:
            mem.update(uni[k])
        out[mo] = sorted(mem)
    return out


def in_intervals(t: np.ndarray, iv: np.ndarray | None) -> np.ndarray:
    if iv is None or iv.size == 0:
        return np.zeros(t.size, dtype=bool)
    k = np.searchsorted(iv[:, 0], t, side="right") - 1
    ok = k >= 0
    out = np.zeros(t.size, dtype=bool)
    out[ok] = t[ok] < iv[k[ok], 1]
    return out


# ---------------------------------------------------------------- funding


def funding_sum(data_dir: Path, s: str, a_ms: int, b_ms: int, cache: dict[str, np.ndarray]) -> float | None:
    """Sum of funding rates with time in (a, b] (a long pays a positive sum); None without data for the whole span."""
    if s not in cache:
        d = data_dir / "funding" / s
        rows: list[list[float]] = []
        if d.exists():
            for p in sorted(d.glob("*.json")):
                rows.extend(json.loads(p.read_text()) or [])
        cache[s] = np.array(rows, dtype=float).reshape(-1, 3) if rows else np.zeros((0, 3))
    f = cache[s]
    if f.shape[0] == 0 or f[0, 0] > a_ms + 9 * 3_600_000:
        return None
    m = (f[:, 0] > a_ms) & (f[:, 0] <= b_ms)
    return float(f[m, 2].sum())


# ---------------------------------------------------------------- per-symbol run


def row(sym: str, var: str, kind: str, tr: Any, sig_entry_ms: int, sig_oos: bool, dist: float) -> dict[str, Any]:
    """``dist``: entry close / daily EMA100 - 1 at the entry bar (for the distance-matched exploratory comparison)."""
    sd = day_of(sig_entry_ms)
    return {
        "dist": dist,
        "sym": sym,
        "var": var,
        "kind": kind,
        "entry_ms": tr.entry_ms,
        "exit_ms": tr.exit_ms,
        "day": day_of(tr.entry_ms),
        "sig_day": sd,
        "sig_month": sd[:7],
        "oos": sig_oos,
        "gross": tr.gross,
        "cost": tr.cost,
        "net": tr.net,
        "exit_kind": tr.kind,
        "hold": tr.hold_days,
    }


def run_symbol(
    s: str, ind: S18Ind, iv: np.ndarray | None, rng: np.random.Generator, outcomes: bool, data_dir: Path, fcache: dict[str, np.ndarray]
) -> tuple[list[dict[str, Any]], Counter[str], dict[str, list[tuple[str, float]]]]:
    d = ind.d
    close_ms = d.t + DAY_MS
    okd = in_intervals(close_ms, iv) & (close_ms >= START_MS) & (close_ms < END_MS)
    above = ind.above_all
    with np.errstate(invalid="ignore"):
        flags = {"e100": d.c < ind.e100, "e200": d.c < ind.e200, "h4": ind.h4.c < ind.h4e200}
    rows: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    daily: dict[str, list[tuple[str, float]]] = {"strat": []}

    def sim(i: int, x: str, stop: float | None = None, until: int | None = None) -> Any:
        em = int(close_ms[i])
        if x == "h4":
            return simulate(ind.h4, ind.h4seg, em, float(d.c[i]), int(ind.dseg[i]), exit_flag=flags["h4"], end_ms=DATA_END_MS)
        if until is not None:
            return simulate(d, ind.dseg, em, float(d.c[i]), int(ind.dseg[i]), until_ms=until, end_ms=DATA_END_MS)
        return simulate(d, ind.dseg, em, float(d.c[i]), int(ind.dseg[i]), exit_flag=flags[x], stop=stop, end_ms=DATA_END_MS)

    def range_stop(i: int, L: int) -> float:
        return float(d.low[i - L : i + 1].min())

    def dist(i: int) -> float:
        return float(d.c[i] / ind.e100[i] - 1)

    for var in VARIANTS:
        L, x, strict, use_stop = parse_variant(var)
        sig = [int(i) for i in signal_days(ind, L, strict)]
        busy = -1
        for i in sig:
            em = int(close_ms[i])
            if not (START_MS <= em < END_MS):
                continue
            if not okd[i]:
                why[f"{var}:not_member"] += 1
                continue
            if not outcomes:
                why[f"{var}:ok"] += 1
                rows.append({"sym": s, "var": var, "kind": "sig", "day": day_of(em), "oos": em >= OOS_MS})
                continue
            if em < busy:
                why[f"{var}:busy"] += 1
                continue
            tr = sim(i, x, range_stop(i, L) if use_stop else None)
            if tr is None:
                why[f"{var}:no_path"] += 1
                continue
            busy = tr.exit_ms
            why[f"{var}:ok"] += 1
            oos = em >= OOS_MS
            r = row(s, var, "sig", tr, em, oos, dist(i))
            if var == PRIMARY:
                fs = funding_sum(data_dir, s, tr.entry_ms, tr.exit_ms, fcache) if tr.entry_ms >= FUND_FROM_MS else None
                r["funding"] = fs
                # daily path returns for the portfolio view (costs on the entry and exit days)
                k0 = i + 1
                k1 = int(np.searchsorted(close_ms, tr.exit_ms, side="left"))
                prev = float(d.c[i])
                for k in range(k0, min(k1, len(d) - 1) + 1):
                    px = tr.exit if k == k1 else float(d.c[k])
                    ret = px / prev - 1
                    if k == k0:
                        ret -= MARKET_SIDE
                    if k == k1:
                        ret -= MARKET_SIDE * px / prev
                    daily["strat"].append((day_of(int(close_ms[k])), ret))
                    prev = float(d.c[k])
            rows.append(r)
            # placebo pools: same segment, within 180 days, not the signal day
            near = (ind.dseg == ind.dseg[i]) & (np.abs(close_ms - em) <= WINDOW_MS) & okd
            near[i] = False
            pool = np.flatnonzero(near & above)
            if pool.size == 0:
                why[f"{var}:preg_empty"] += 1
            for j in rng.choice(pool, size=min(PER_SIGNAL, pool.size), replace=False) if pool.size else []:
                j = int(j)
                if use_stop and j < L:
                    continue
                pt = sim(j, x, range_stop(j, L) if use_stop else None)
                if pt is not None:
                    rows.append(row(s, var, "preg", pt, em, oos, dist(j)))
            if var == PRIMARY:
                pool_t = np.flatnonzero(near)
                hold_ms = int(round(tr.hold_days)) * DAY_MS
                for j in rng.choice(pool_t, size=min(PER_SIGNAL, pool_t.size), replace=False) if pool_t.size else []:
                    j = int(j)
                    pt = sim(j, "e100", until=int(close_ms[j]) + max(hold_ms, DAY_MS))
                    if pt is not None:
                        rows.append(row(s, var, "ptime", pt, em, oos, dist(j)))
    if outcomes:
        for j in np.flatnonzero(okd & above):
            j = int(j)
            pt = sim(j, "e100")
            if pt is not None:
                em = int(close_ms[j])
                rows.append(row(s, PRIMARY, "pall", pt, em, em >= OOS_MS, dist(j)))
        # universe buy-and-hold daily returns (member on the bar's close, previous daily bar exactly a day before)
        ok_u = in_intervals(close_ms, iv)
        uni = []
        for k in np.flatnonzero(ok_u[1:]) + 1:
            if d.t[k] - d.t[k - 1] == DAY_MS and ind.dseg[k] == ind.dseg[k - 1]:
                uni.append((day_of(int(close_ms[k])), float(d.c[k] / d.c[k - 1] - 1)))
        daily["uni"] = uni
    return rows, why, daily


def collect(
    cache: Path, data_dir: Path, outcomes: bool, limit: int | None, wide: bool = False
) -> tuple[list[dict[str, Any]], Counter[str], dict[str, Any]]:
    uni = s1.members(cache)
    syms = sorted({s for v in uni.values() for s in v})
    if limit:
        syms = syms[:limit]
    ivs = member_intervals(widen(uni) if wide else uni)
    rng = np.random.default_rng(SEED)
    rows: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    strat: dict[str, list[float]] = defaultdict(list)
    unid: dict[str, list[float]] = defaultdict(list)
    btc: dict[str, float] = {}
    fcache: dict[str, np.ndarray] = {}
    segs = partial = 0
    t0 = time.time()
    for n, s in enumerate(syms):
        h1 = s1.load_1h(cache, data_dir, s)
        if len(h1) < 500:
            why["symbol_too_short"] += 1
            continue
        ind = s18_indicators(h1)
        segs += int(ind.dseg.max()) + 1 if len(ind.d) else 0
        partial += int(np.sum((h1.t[-1] - h1.t[0]) // DAY_MS + 1) - len(ind.d))
        r, w, dl = run_symbol(s, ind, ivs.get(s), rng, outcomes, data_dir, fcache)
        rows.extend(r)
        why.update(w)
        for dd, v in dl.get("strat", []):
            strat[dd].append(v)
        for dd, v in dl.get("uni", []):
            unid[dd].append(v)
            if s == "BTCUSDT":
                btc[dd] = v
        fcache.pop(s, None)
        del h1, ind
        if (n + 1) % 25 == 0 or n + 1 == len(syms):
            emit(f"  [run] {n + 1}/{len(syms)} symbols in {time.time() - t0:.0f}s, rows {len(rows)}, rss {rss_mb():.0f} MB")
    why["segments_total"] = segs
    why["days_missing_or_partial_total"] = partial
    return rows, why, {"strat": strat, "uni": unid, "btc": btc}


# ---------------------------------------------------------------- stats


def fmt_ci(ci: tuple[float, float] | None, pct: bool = True) -> str:
    if ci is None:
        return "—"
    return f"[{ci[0]:+.1%}; {ci[1]:+.1%}]" if pct else f"[{ci[0]:+.3f}; {ci[1]:+.3f}]"


def summ(rows: list[dict[str, Any]], key: str = "net", cl: str = "day", n_boot: int = 5000) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "days": 0}
    v = [r[key] for r in rows]
    c = [r[cl] for r in rows]
    multi = len(set(c)) > 1
    bm = cluster_bootstrap_means(v, c, n_boot=n_boot) if multi else np.array([])
    return {
        "n": len(v),
        "days": len({r["day"] for r in rows}),
        "clusters": len(set(c)),
        "mean": statistics.fmean(v),
        "median": statistics.median(v),
        "win": sum(1 for x in v if x > 0) / len(v),
        "ci": (float(np.quantile(bm, 0.025)), float(np.quantile(bm, 0.975))) if multi else None,
        "p": float((np.sum(bm <= 0) + 1) / (bm.size + 1)) if multi else None,
        "se": float(np.std(bm)) if multi else None,
        "sd": statistics.pstdev(v),
    }


def diff(sig: list[dict[str, Any]], plc: list[dict[str, Any]], cl: str = "day", n_boot: int = 5000, key: str = "net") -> dict[str, Any]:
    """Signal mean minus placebo mean; the placebo rows sit in their signal's cluster (sig_day / sig_month)."""
    if not sig or not plc:
        return {"diff": float("nan"), "ci": None, "p": None, "se": None}
    pc = "sig_day" if cl == "day" else "sig_month"
    sc = "day" if cl == "day" else "sig_month"
    out = cluster_bootstrap_diff([r[key] for r in sig], [r[sc] for r in sig], [r[key] for r in plc], [r[pc] for r in plc], n_boot=n_boot)
    if out["ci"] is not None:
        out["se"] = (out["ci"][1] - out["ci"][0]) / (2 * 1.96)
    else:
        out["se"] = None
    return out


HDR = "| вибірка | n | дат | частка net>0 | E[gross] | E[net] | медіана net | 95 % CI net (дати) | p (E>0) |\n|---|---|---|---|---|---|---|---|---|"


def line(label: str, rows: list[dict[str, Any]], n_boot: int = 5000, key: str = "net") -> str:
    if not rows:
        return f"| {label} | 0 | — | — | — | — | — | — | — |"
    st = summ(rows, key, "day", n_boot)
    g = statistics.fmean(r["gross"] for r in rows)
    p = "—" if st["p"] is None else f"{st['p']:.4f}"
    return f"| {label} | {st['n']} | {st['days']} | {st['win']:.1%} | {g:+.1%} | {st['mean']:+.1%} | {st['median']:+.1%} | {fmt_ci(st['ci'])} | {p} |"


def sel(rows: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    return [r for r in rows if all((r[k] in v) if isinstance(v, (set, tuple, list)) else r[k] == v for k, v in kw.items())]


def q(xs: list[float], p: float) -> float:
    return float(np.quantile(np.asarray(xs), p)) if xs else float("nan")


def port_stats(days: list[str], ret: dict[str, float]) -> str:
    eq, peak, mdd = 1.0, 1.0, 0.0
    inv = 0
    for dd in days:
        r = ret.get(dd)
        if r is not None:
            inv += 1
            eq *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    yrs = len(days) / 365.0
    cagr = eq ** (1 / yrs) - 1 if yrs > 0 and eq > 0 else float("nan")
    return f"разом {eq - 1:+.0%}, CAGR {cagr:+.1%}, макс. просадка {mdd:.0%}, днів у ринку {inv / max(1, len(days)):.0%}"


# ---------------------------------------------------------------- stages


def sanity(cache: Path, data_dir: Path) -> None:
    emit("## Санітарні перевірки")
    h1 = s1.load_1h(cache, data_dir, "BTCUSDT")
    ind = s18_indicators(h1)
    k = int(np.searchsorted(ind.d.t, s1.ms("2024-12-31T00:00")))
    emit(f"- BTCUSDT: денне закриття 2024-12-31 = {ind.d.c[k]:g} (еталон з попередніх перевірок: 93548.9)")
    emit(f"- BTCUSDT: денних барів {len(ind.d)}, сегментів {int(ind.dseg.max()) + 1}, 4h-барів {len(ind.h4)}")
    sig = signal_days(ind, 60)
    lines = []
    for i in sig:
        lines.append(
            f"{day_of(int(ind.d.t[i]))} close {ind.d.c[i]:.0f} EMA100 {ind.e100[i]:.0f} EMA200 {ind.e200[i]:.0f} 4hEMA200 {ind.e4[i]:.0f}"
        )
    emit(f"- BTCUSDT: сигнали L = 60 (доба сигналу, рівні): {lines or '—'}")
    emit(f"- пікова RSS {rss_mb():.0f} MB")


def stage_count(cache: Path, data_dir: Path, limit: int | None, wide: bool) -> None:
    emit(f"# Крок 3 S18, стадія 0: частота сигналів (без результатів угод){WIDE_NOTE if wide else ''}")
    sanity(cache, data_dir)
    rows, why, _ = collect(cache, data_dir, False, limit, wide)
    emit("\n| варіант | сигналів IS | дат IS | сигналів OOS | дат OOS | символів |\n|---|---|---|---|---|---|")
    for v in VARIANTS:
        r = sel(rows, var=v)
        i_, o_ = [x for x in r if not x["oos"]], [x for x in r if x["oos"]]
        emit(
            f"| {v} | {len(i_)} | {len({x['day'] for x in i_})} | {len(o_)} | {len({x['day'] for x in o_})} | {len({x['sym'] for x in r})} |"
        )
    emit(f"\n- відсів: {dict(sorted(why.items()))}")
    m = sel(rows, var=PRIMARY)
    d_is = len({x["day"] for x in m if not x["oos"]})
    emit(f"- основне правило, різних дат IS (без «одна угода за раз»): {d_is}; поріг реєстрації {MIN_DATES}")


def stage_report(cache: Path, data_dir: Path, limit: int | None, out_dir: Path, wide: bool) -> None:
    emit(f"# Крок 3 S18 / H4: результати{WIDE_NOTE if wide else ''}")
    emit(
        f"Всесвіт і дані — кроку 1{' (розширене членство: у всесвіті будь-якого з 12 місяців до місяця входу включно)' if wide else ''}; входи {day_of(START_MS)} … {day_of(END_MS)}; IS < {day_of(OOS_MS)} ≤ OOS. Дохідність — на номінал без плеча, net після 0.07 % на сторону.\n"
    )
    sanity(cache, data_dir)
    rows, why, port = collect(cache, data_dir, True, limit, wide)
    s1.wr(out_dir / ("trades_wide.json" if wide else "trades.json"), rows)
    emit(f"\n- відсів і лічильники: {dict(sorted(why.items()))}")

    def split(r: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return [x for x in r if not x["oos"]], [x for x in r if x["oos"]]

    main = sel(rows, var=PRIMARY, kind="sig")
    m_is, m_oos = split(main)
    preg_is, preg_oos = split(sel(rows, var=PRIMARY, kind="preg"))
    pt_is, pt_oos = split(sel(rows, var=PRIMARY, kind="ptime"))
    pa_is, pa_oos = split(sel(rows, var=PRIMARY, kind="pall"))

    emit("\n## Основне правило: L = 60, вихід — денне закриття під EMA100")
    emit(HDR)
    emit(line("сигнали IS (основна метрика)", m_is, 10000))
    emit(line("сигнали OOS", m_oos, 10000))
    emit(line("сигнали, весь період", main, 10000))
    emit(line("P_reg IS", preg_is))
    emit(line("P_reg OOS", preg_oos))
    emit(line("P_time IS", pt_is))
    emit(line("P_time OOS", pt_oos))
    emit(line("P_all IS (усі доби режиму)", pa_is))
    emit(line("P_all OOS", pa_oos))

    s_is = summ(m_is, n_boot=10000)
    s_is_m = summ(m_is, cl="sig_month", n_boot=10000)
    s_oos = summ(m_oos, n_boot=10000)
    dr = diff(m_is, preg_is, "day", 10000)
    dr_m = diff(m_is, preg_is, "month", 10000)
    dr_oos = diff(m_oos, preg_oos, "day", 10000)
    emit("\n| порівняння | різниця E[net] | 95 % CI (дати) | p (>0) | 95 % CI (місяці) |\n|---|---|---|---|---|")
    for lab, a, b in (
        ("сигнал − P_reg, IS (основне p)", m_is, preg_is),
        ("сигнал − P_reg, OOS", m_oos, preg_oos),
        ("сигнал − P_time, IS", m_is, pt_is),
        ("сигнал − P_time, OOS", m_oos, pt_oos),
    ):
        x, xm = diff(a, b, "day", 10000), diff(a, b, "month", 10000)
        emit(f"| {lab} | {x['diff']:+.1%} | {fmt_ci(x['ci'])} | {x['p'] if x['p'] is None else round(x['p'], 4)} | {fmt_ci(xm['ci'])} |")
    emit(f"\n- E[net] сигналів IS з кластерами за місяцем: CI {fmt_ci(s_is_m['ci'])} ({s_is_m.get('clusters', 0)} місяців)")

    # exploratory (added after the first results): P_reg / P_all matched on the distance to the daily EMA100
    emit("\n### Дослідницьке (додано після перших результатів): плацебо з тією самою відстанню до EMA100 на вході")
    emit(
        f"- медіана відстані close/EMA100 − 1 на вході: сигнали IS {q([r['dist'] for r in m_is], 0.5):+.1%}, P_reg IS {q([r['dist'] for r in preg_is], 0.5):+.1%}, P_all IS {q([r['dist'] for r in pa_is], 0.5):+.1%}"
    )
    for lab, sig_rows, base, bins in (
        ("IS, база P_reg (5 квантильних кошиків)", m_is, preg_is, 5),
        ("OOS, база P_reg (5 кошиків)", m_oos, preg_oos, 5),
        ("IS, база P_all (10 кошиків)", m_is, pa_is, 10),
        ("OOS, база P_all (10 кошиків)", m_oos, pa_oos, 10),
    ):
        if not sig_rows or not base:
            emit(f"- {lab}: —")
            continue
        mb = matched_baseline(
            [r["dist"] for r in sig_rows],
            [r["net"] for r in sig_rows],
            np.array([r["dist"] for r in base]),
            np.array([r["net"] for r in base]),
            bins=bins,
        )
        ex = mb["expected"]
        vals = [r["net"] - e for r, e in zip(sig_rows, ex, strict=True) if np.isfinite(e)]
        dd = [r["day"] for r, e in zip(sig_rows, ex, strict=True) if np.isfinite(e)]
        bm = cluster_bootstrap_means(vals, dd, n_boot=5000) if len(set(dd)) > 1 else np.array([])
        ci = (float(np.quantile(bm, 0.025)), float(np.quantile(bm, 0.975))) if bm.size else None
        emit(
            f"- {lab}: сигнали {mb['event_rate']:+.1%}, база з тією ж відстанню {mb['base_rate']:+.1%}; Δ {statistics.fmean(vals):+.1%} CI {fmt_ci(ci)} (n {len(vals)})"
        )

    # gates and verdict
    n_dates = s_is.get("days", 0)
    mde = 2.8 * (dr.get("se") or float("inf"))
    p_raw = dr.get("p")
    p_adj = holm([*STEP_P, p_raw])[2] if p_raw is not None else None
    emit("\n## Ворота й вердикт (за реєстрацією)")
    emit(f"- різних дат входу IS: {n_dates} (поріг {MIN_DATES}); OOS: {s_oos.get('days', 0)} (поріг для confirmed {MIN_OOS_DATES})")
    emit(f"- MDE = 2.8·SE(Δ_reg) = {mde:.3f} (ворота {MDE_GATE}); SE з CI за датами")
    emit(
        f"- основне p (Δ_reg > 0, IS): сире {p_raw}; Holm (m = 3: 0.985, 0.280, це) {p_adj}; ×11 для довідки {min(1.0, 11 * p_raw) if p_raw is not None else None}"
    )
    if n_dates < MIN_DATES or mde > MDE_GATE:
        verdict = "inconclusive (замало дат або потужності)"
    elif s_is["ci"] is None or s_is["ci"][0] <= 0 or dr["diff"] < MIN_EFFECT:
        verdict = "rejected"
    elif (
        s_is_m["ci"] is not None
        and s_is_m["ci"][0] > 0
        and dr["ci"] is not None
        and dr["ci"][0] > 0
        and dr_m["ci"] is not None
        and dr_m["ci"][0] > 0
        and p_adj is not None
        and p_adj < 0.05
        and s_oos.get("days", 0) >= MIN_OOS_DATES
        and s_oos["mean"] > 0
        and dr_oos["diff"] > 0
    ):
        verdict = "confirmed"
    else:
        verdict = "inconclusive (позитив в IS не пройшов CI за місяцями, Holm чи OOS)"
    emit(f"- **вердикт за правилами реєстрації: {verdict}**")

    # robustness
    emit("\n## Стійкість основного правила")
    emit(HDR)
    emit(line("BTC+ETH, весь період", [r for r in main if r["sym"] in s1.ALWAYS]))
    emit(line("альти, весь період", [r for r in main if r["sym"] not in s1.ALWAYS]))
    for y in range(2020, 2027):
        emit(line(f"вхід у {y}", [r for r in main if r["day"].startswith(str(y))]))
    emit(line("IS без open_end", [r for r in m_is if r["exit_kind"] != "open_end"]))
    emit(line("OOS без open_end", [r for r in m_oos if r["exit_kind"] != "open_end"]))
    for k in (5, 10, 20):
        v, dd, _ = without_top_days([r["net"] for r in m_is], [r["day"] for r in m_is], k)
        fake = [{"net": a, "gross": a, "day": b} for a, b in zip(v, dd, strict=True)]
        st = summ(fake)
        emit(
            f"| IS без {k} найкращих дат | {st['n']} | {st['days']} | {st['win']:.1%} | — | {st['mean']:+.1%} | {st['median']:+.1%} | {fmt_ci(st['ci'])} | {st['p']:.4f} |"
        )
    for mult in (2.0, 3.0):
        v2 = [{"net": r["gross"] - mult * r["cost"], "gross": r["gross"], "day": r["day"]} for r in m_is]
        emit(line(f"IS, витрати ×{mult:g}", v2))
    v3 = [{"net": r["net"] - FUND_8H * r["hold"] * 3, "gross": r["gross"], "day": r["day"]} for r in m_is]
    emit(line("IS, фандинг 0.01 %/8 год проти лонга", v3))
    fr = [r for r in main if r.get("funding") is not None]
    emit(line("з фактичним фандингом (вхід ≥ 2021-10), без нього", fr))
    emit(line("з фактичним фандингом, net − фандинг", [{**r, "net": r["net"] - r["funding"]} for r in fr]))
    if fr:
        emit(
            f"\n- фактичний фандинг на угоду: середнє {statistics.fmean(r['funding'] for r in fr):+.2%}, медіана {q([r['funding'] for r in fr], 0.5):+.2%} (n {len(fr)})"
        )
    lg = [{**r, "net": math.log1p(r["net"]) if r["net"] > -1 else -10.0} for r in m_is]
    lp = [{**r, "net": math.log1p(r["net"]) if r["net"] > -1 else -10.0} for r in preg_is]
    emit(
        f"- лог-дохідність IS: сигнали {statistics.fmean(r['net'] for r in lg):+.3f}, P_reg {statistics.fmean(r['net'] for r in lp) if lp else float('nan'):+.3f}; Δ {fmt_ci(diff(lg, lp)['ci'], False)}"
    )
    if main:
        nets = [r["net"] for r in main]
        emit(
            f"- розподіл net (весь період): p5 {q(nets, 0.05):+.1%}, p25 {q(nets, 0.25):+.1%}, медіана {q(nets, 0.5):+.1%}, p75 {q(nets, 0.75):+.1%}, p95 {q(nets, 0.95):+.1%}, макс {max(nets):+.0%}"
        )
        hd = [r["hold"] for r in main]
        emit(
            f"- утримання, діб: p10 {q(hd, 0.1):.0f}, медіана {q(hd, 0.5):.0f}, p90 {q(hd, 0.9):.0f}; виходи: {dict(Counter(r['exit_kind'] for r in main))}"
        )
        top = sorted(main, key=lambda r: -r["net"])[:5]
        emit(f"- 5 найкращих угод: {[(r['sym'], r['day'], round(r['net'], 2)) for r in top]}")
        emit(
            f"- символів із сигналами: {len({r['sym'] for r in main})}; угод на символ (макс): {max(Counter(r['sym'] for r in main).values())}"
        )

    # buy-and-hold, portfolio view
    emit("\n## Портфель проти buy-and-hold (на капітал, описово; не входить у вердикт)")
    strat = {k: statistics.fmean(v) for k, v in port["strat"].items()}
    unid = {k: statistics.fmean(v) for k, v in port["uni"].items()}
    btc = port["btc"]
    alld = sorted(set(unid) | set(strat))
    for lab, lo, hi in (("IS", START_MS, OOS_MS), ("OOS", OOS_MS, DATA_END_MS)):
        days = [x for x in alld if day_of(lo) <= x < day_of(hi)]
        emit(f"- {lab} ({days[0] if days else '—'} … {days[-1] if days else '—'}, {len(days)} діб):")
        emit(f"  - S18 (рівні ваги серед відкритих угод, інакше кеш): {port_stats(days, strat)}")
        emit(f"  - рівноважний портфель усіх членів всесвіту: {port_stats(days, unid)}")
        emit(f"  - BTC buy-and-hold: {port_stats(days, btc)}")

    # secondary variants
    emit("\n## Сітка й варіанти (усі; сирі p і Holm усередині кроку — лише довідка)")
    emit(
        "| варіант | вихід | n IS | дат IS | E[net] IS | CI IS | P_reg IS | Δ_reg IS [CI] | p Δ | n OOS | E[net] OOS | Δ_reg OOS |\n|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    sec: list[tuple[str, float]] = []
    for v in VARIANTS:
        L, x, strict, stop = parse_variant(v)
        si, so = split(sel(rows, var=v, kind="sig"))
        pi, po = split(sel(rows, var=v, kind="preg"))
        a, d1, d2 = summ(si), diff(si, pi), diff(so, po)
        pm = statistics.fmean(r["net"] for r in pi) if pi else float("nan")
        om = statistics.fmean(r["net"] for r in so) if so else float("nan")
        lab = f"{v}{' (основне)' if v == PRIMARY else ''}"
        emit(
            f"| {lab} | {EXIT_LABEL[x]}{' + стоп' if stop else ''}{' строге' if strict else ''} | {a.get('n', 0)} | {a.get('days', 0)} | {a.get('mean', float('nan')):+.1%} | {fmt_ci(a.get('ci'))} | {pm:+.1%} | {d1['diff']:+.1%} {fmt_ci(d1['ci'])} | {d1['p'] if d1['p'] is None else round(d1['p'], 4)} | {len(so)} | {om:+.1%} | {d2['diff']:+.1%} |"
        )
        if v != PRIMARY and d1["p"] is not None:
            sec.append((v, d1["p"]))
    if sec:
        adj = holm([p for _, p in sec])
        emit("\n| варіант | сире p (Δ_reg > 0) | Holm усередині кроку |\n|---|---|---|")
        for (v, pr), pa in zip(sec, adj, strict=True):
            emit(f"| {v} | {pr:.4f} | {pa:.4f} |")
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_step3_s18")
    ap.add_argument("--s14-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--stage", choices=("prereg", "count", "report"), required=True)
    ap.add_argument("--symbols", type=int, help="run only the first N symbols (timing sample)")
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    ap.add_argument("--wide", action="store_true", help="exploratory: membership in any of the last 12 universe months")
    a = ap.parse_args()
    out_dir = a.cache_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("w")
    t0 = time.time()
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "count":
        stage_count(a.s14_cache.expanduser(), a.data_dir.expanduser(), a.symbols, a.wide)
    else:
        stage_report(a.s14_cache.expanduser(), a.data_dir.expanduser(), a.symbols, out_dir, a.wide)
    emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
