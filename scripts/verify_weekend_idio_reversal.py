"""Weekend idiosyncratic reversal of USDT-M alt perps: rank IC of the beta-neutral weekend move vs the Monday move.

Idea card: docs/ideas/IDEA-20261029-weekend-idio-reversal-alt-perps.md (sections "Тріаж критика", "Попередня реєстрація").

Data: the per-file cache of ``scripts/verify_funding_crowding.py`` (data.binance.vision daily klines and funding, 678
eligible symbols incl. SETTLING / removed ones; read-only via ``--data-dir``) for the point-in-time universe, beta,
turnover and funding; 1h klines from data.binance.vision (``rt.binance_vision`` through ``core.http.Http``) for the
universe members, cached per symbol-month under ``--cache-dir``. No keys, no orders.

Stages (each caches under --cache-dir; a rerun resumes):
    universe  weekly point-in-time universe at every Saturday from the daily cache (+ beta, turnover, funding)
    fetch     1h monthly klines for every (member, month) the panel needs (``--sample N`` for timing)
    stage0    the 1h panel, data checks and the power gate (SD, SE_NW, MDE of the weekly IC) -- no means
    full      the registered test H1 -> H2 -> H3 with placebos; refuses to run unless stage0 passed the gate

    uv run python scripts/verify_weekend_idio_reversal.py <stage>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
import resource
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_funding_crowding as fc  # noqa: E402  (loader, Fetcher, json helpers; scripts/ is not a package)

from tradedesk.analytics.funding_crowding import block_bootstrap_ci, newey_west_se, week_candidate  # noqa: E402
from tradedesk.analytics.funding_spread import normalize_asset  # noqa: E402
from tradedesk.analytics.listing_drift import DAY_MS  # noqa: E402
from tradedesk.analytics.per_asset import heterogeneity, permutation_q, persistence  # noqa: E402
from tradedesk.analytics.weekend_reversal import (  # noqa: E402
    HOUR_MS,
    HourBars,
    calendar_window,
    capped_weight,
    cross_ic,
    dominated,
    funding_sum,
    maker_short_filled,
    quintile_size,
    span_idio,
    tercile_labels,
    top_k,
    turnover_ratio,
)
from tradedesk.analytics.xs_factors import (  # noqa: E402
    beta_ivol,
    holm_z,
    ic_products,
    placebo_mean_ics,
    rank_ic,
    two_sided_p,
    window_return,
    without_top,
)
from tradedesk.server import build_server  # noqa: E402

FIRST_SAT = int(datetime(2021, 11, 6, tzinfo=UTC).timestamp()) // 86400
LAST_SAT = int(datetime(2026, 9, 19, tzinfo=UTC).timestamp()) // 86400  # weekday placebo ends Fri 2026-09-25
OOS_MON = int(datetime(2025, 3, 3, tzinfo=UTC).timestamp()) // 86400  # Mondays >= this are out of sample
BTC = "BTCUSDT"
NW_LAG = 2
N_TESTS = 3
GATE = 0.025  # MDE_Holm of the weekly rank IC (critic condition 2)
MIN_N = 50  # weeks with fewer members are skipped
MIN_COVER = 120  # median 1h-covered members per weekend (critic condition 1)
SEED = 7
COST_ALT = 0.0002 + 0.00055 + 2 * 0.0005  # maker entry + taker exit + slippage per side
COST_ALT_TAKER = 0.00055 + 0.00055 + 2 * 0.0005
COST_HEDGE = 0.0007  # BTC hedge round trip per unit of hedge notional
CAP = 0.05
DEFAULT_DATA = Path.home() / ".cache/tradedesk-agents/funding_crowding"
DEFAULT_OUT = Path.home() / ".cache/tradedesk-agents/weekend_idio_reversal"

# panel row layout: W/T registered; W0/T0 no gap (card's original, bounce-contaminated); T72 72 h hold; TT Tue->Wed
COLS = ("sym", "W", "T", "Wd", "Td", "W0", "T0", "T72", "TT", "beta", "mqv", "tr", "r7", "fill", "f_we", "f_wd", "f_72", "beta_d")
IX = {c: i for i, c in enumerate(COLS)}


def rss() -> str:
    return f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


def saturdays() -> list[int]:
    return list(range(FIRST_SAT, LAST_SAT + 1, 7))


def month_of(day: int) -> str:
    return ymd(day)[:7]


# ---------------------------------------------------------------- universe (daily cache only)


def stage_universe(data_dir: Path, out: Path) -> None:
    t0 = time.time()
    data = fc.load_all(data_dir)
    print(f"loaded {len(data)} symbols in {time.time() - t0:.0f}s; {rss()}", flush=True)
    btc = data[BTC].bars
    btc_f = data[BTC]
    weeks = []
    reasons: Counter[str] = Counter()
    for sat in saturdays():
        t_ms = sat * DAY_MS
        mon_ms = (sat + 2) * DAY_MS + HOUR_MS
        thu_ms = (sat + 5) * DAY_MS + HOUR_MS
        cands = {}
        for s, d in data.items():
            if sat not in d.bars.open and (not d.bars.close or min(d.bars.close) > sat or max(d.bars.close) < sat - 30):
                continue
            c, why = week_candidate(s, d.bars, d.fslice(t_ms - 10 * DAY_MS, t_ms), sat, starts=d.starts)
            reasons[why] += 1
            if c is not None:
                cands[s] = c
        best = {}
        for s, c in cands.items():
            a = normalize_asset(s[:-4], "binanceusdm")[0]
            if a not in best or c.median_qv > best[a].median_qv:
                best[a] = c
        reasons["dup_asset"] += len(cands) - len(best)
        rows = []
        for c in best.values():
            if c.symbol == BTC:
                reasons["btc_itself"] += 1
                continue
            d = data[c.symbol]
            bi = beta_ivol(d.bars, btc, sat)
            bd = beta_ivol(d.bars, btc, sat + 3)
            tr = turnover_ratio(d.bars, sat)
            if bi is None or bd is None or tr is None:
                reasons["no_beta_or_turnover"] += 1
                continue
            r7 = window_return(d.bars, sat + 2, 8, 1)
            fl = d.fslice(mon_ms - DAY_MS, thu_ms + 2 * DAY_MS)
            rows.append(
                {
                    "sym": c.symbol,
                    "beta": bi[0],
                    "beta_d": bd[0],
                    "mqv": c.median_qv,
                    "tr": tr,
                    "r7": r7,
                    "f_we": funding_sum(fl, mon_ms, mon_ms + DAY_MS),
                    "f_72": funding_sum(fl, mon_ms, mon_ms + 3 * DAY_MS),
                    "f_wd": funding_sum(fl, thu_ms, thu_ms + DAY_MS),
                }
            )
        bf = btc_f.fslice(mon_ms - DAY_MS, thu_ms + 2 * DAY_MS)
        weeks.append(
            {
                "sat": sat,
                "rows": rows,
                "btc_f_we": funding_sum(bf, mon_ms, mon_ms + DAY_MS),
                "btc_f_72": funding_sum(bf, mon_ms, mon_ms + 3 * DAY_MS),
                "btc_f_wd": funding_sum(bf, thu_ms, thu_ms + DAY_MS),
            }
        )
        if len(weeks) % 50 == 0:
            print(f"  week {len(weeks)} {ymd(sat)} N={len(rows)} ({time.time() - t0:.0f}s; {rss()})", flush=True)
    fc.wr(out / "universe.json", {"weeks": weeks, "reasons": dict(reasons)})
    ns = [len(w["rows"]) for w in weeks]
    need = needed_months(weeks)
    print(
        f"universe: {len(weeks)} Saturdays {ymd(FIRST_SAT)}..{ymd(LAST_SAT)}; N min {min(ns)} median {statistics.median(ns)} "
        f"max {max(ns)}; symbols {len(need)}; symbol-months to fetch {sum(len(v) for v in need.values())}; reasons {dict(reasons)}; {rss()}",
        flush=True,
    )


def needed_months(weeks: list[dict[str, Any]]) -> dict[str, list[str]]:
    need: dict[str, set[str]] = {}
    for w in weeks:
        mos = {month_of(w["sat"] + k) for k in range(0, 7)}
        for r in w["rows"]:
            need.setdefault(r["sym"], set()).update(mos)
    allm = {month_of(d) for d in range(FIRST_SAT - 1, LAST_SAT + 8)}
    need[BTC] = allm
    return {s: sorted(v) for s, v in sorted(need.items())}


# ---------------------------------------------------------------- fetch (1h archive)


def hpath(out: Path, sym: str, mo: str) -> Path:
    return out / "k1h" / sym / f"{mo}.json"


async def stage_fetch(out: Path, sample: int) -> None:
    need = needed_months(fc.rd(out / "universe.json")["weeks"])
    syms = sorted(need)
    if sample:
        syms = syms[:sample]
    _, rt = build_server()
    fx = fc.Fetcher(out, workers=12)
    try:
        jobs = []
        for s in syms:
            for mo in need[s]:
                y, m = map(int, mo.split("-"))

                async def g(s: str = s, y: int = y, m: int = m) -> Any:
                    rows = (await rt.binance_vision.um_klines_monthly(s, "1h", y, m)).data
                    return [[int(r[0]), r[1], r[2], r[3], r[4], r[5], r[6]] for r in rows]

                jobs.append((hpath(out, s, mo), g))
        print(f"symbols this run: {len(syms)}; {rss()}", flush=True)
        await fx.many("fetch 1h", jobs)
    finally:
        await rt.aclose()
    print(f"fetch done; {rss()}", flush=True)


def load_hours(out: Path, sym: str, months: list[str]) -> HourBars:
    rows: list[list[float]] = []
    for mo in months:
        p = hpath(out, sym, mo)
        if p.exists():
            rows.extend(fc.rd(p) or [])
    return HourBars.from_rows(rows)


# ---------------------------------------------------------------- panel + stage 0


def stage_panel(data_dir: Path, out: Path) -> dict[str, Any]:
    uni = fc.rd(out / "universe.json")
    need = needed_months(uni["weeks"])
    btc = load_hours(out, BTC, need[BTC])
    by_sym: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for i, w in enumerate(uni["weeks"]):
        for r in w["rows"]:
            by_sym.setdefault(r["sym"], []).append((i, r))
    rows_by_week: list[list[list[Any]]] = [[] for _ in uni["weeks"]]
    miss: Counter[str] = Counter()
    lvl: list[float] = []
    t0 = time.time()
    for k, (s, items) in enumerate(sorted(by_sym.items())):
        hb = load_hours(out, s, need[s])
        if not hb.bars:
            miss["no_1h_file"] += len(items)
            continue
        dd = fc.load_sym(data_dir, s)
        for i, r in items:
            sat = uni["weeks"][i]["sat"]
            we, wd = calendar_window(sat), calendar_window(sat + 3)
            w = span_idio(hb, btc, we.sig_start, we.sig_end, r["beta"])
            t = span_idio(hb, btc, we.tgt_start, we.tgt_end, r["beta"])
            if w is None or t is None:
                miss["no_1h_price"] += 1
                continue
            wdv = span_idio(hb, btc, wd.sig_start, wd.sig_end, r["beta_d"])
            tdv = span_idio(hb, btc, wd.tgt_start, wd.tgt_end, r["beta_d"])
            w0 = span_idio(hb, btc, sat * 24, (sat + 2) * 24, r["beta"])
            t0v = span_idio(hb, btc, (sat + 2) * 24, (sat + 3) * 24, r["beta"])
            t72 = span_idio(hb, btc, we.tgt_start, we.tgt_start + 72, r["beta"])
            tt = span_idio(hb, btc, (sat + 3) * 24, (sat + 4) * 24, r["beta"])
            bar = hb.bar(we.tgt_start)
            fill = maker_short_filled(bar) if bar is not None else None
            rows_by_week[i].append(
                [
                    s,
                    w,
                    t,
                    wdv,
                    tdv,
                    w0,
                    t0v,
                    t72,
                    tt,
                    r["beta"],
                    r["mqv"],
                    r["tr"],
                    r["r7"],
                    fill,
                    r["f_we"],
                    r["f_wd"],
                    r["f_72"],
                    r["beta_d"],
                ]
            )
            # consistency: 1h open at Saturday 00:00 vs the daily close of Friday (same archive, two intervals)
            if dd is not None and (sat - 1) in dd.bars.close:
                p = hb.price_at(sat * 24)
                if p is not None:
                    lvl.append(abs(p / dd.bars.close[sat - 1] - 1.0))
        if (k + 1) % 100 == 0:
            print(f"  panel {k + 1}/{len(by_sym)} symbols ({time.time() - t0:.0f}s; {rss()})", flush=True)
    weeks = []
    for i, w in enumerate(uni["weeks"]):
        sat = w["sat"]
        we = calendar_window(sat)
        b0, b1 = btc.price_at(we.sig_start), btc.price_at(we.sig_end)
        weeks.append(
            {
                "sat": sat,
                "n_uni": len(w["rows"]),
                "rows": rows_by_week[i],
                "btc_we": (b1 / b0 - 1.0) if b0 and b1 else None,
                "btc_f_we": w["btc_f_we"],
                "btc_f_72": w["btc_f_72"],
                "btc_f_wd": w["btc_f_wd"],
            }
        )
    lvl.sort()
    chk = {
        "n": len(lvl),
        "median": lvl[len(lvl) // 2] if lvl else None,
        "p99": lvl[99 * len(lvl) // 100] if lvl else None,
        "gt_1pct": sum(v > 0.01 for v in lvl),
    }
    fc.wr(out / "panel.json", {"weeks": weeks, "miss": dict(miss), "cols": COLS, "level_check": chk})
    print(f"panel built: missing {dict(miss)}; 1h-vs-1d level check {chk}; {rss()}", flush=True)
    return {"weeks": weeks, "miss": dict(miss), "level_check": chk}


def load_weeks(out: Path) -> list[dict[str, Any]]:
    p = out / "panel.json"
    if not p.exists():
        raise SystemExit("run stage0 first")
    return [w for w in fc.rd(p)["weeks"] if len(w["rows"]) >= MIN_N]


def col(rows: list[list[Any]], name: str) -> list[Any]:
    return [r[IX[name]] for r in rows]


def ic_of(rows: list[list[Any]], s: str, t: str) -> float | None:
    rr = [r for r in rows if r[IX[s]] is not None and r[IX[t]] is not None]
    if len(rr) < MIN_N:
        return None
    return rank_ic(col(rr, s), col(rr, t))


def stage0(data_dir: Path, out: Path) -> None:
    raw = stage_panel(data_dir, out)
    allw = raw["weeks"]
    nu = [w["n_uni"] for w in allw]
    nc = [len(w["rows"]) for w in allw]
    print(
        f"\nSaturdays {len(allw)} ({ymd(FIRST_SAT)}..{ymd(LAST_SAT)}); daily universe N median {statistics.median(nu)} min {min(nu)} max {max(nu)}"
    )
    print(
        f"1h-covered N per weekend: median {statistics.median(nc)} min {min(nc)} max {max(nc)}; weekends with N >= 40: "
        f"{sum(n >= 40 for n in nc) / len(nc):.1%}, >= {MIN_N}: {sum(n >= MIN_N for n in nc) / len(nc):.1%}, >= {MIN_COVER}: "
        f"{sum(n >= MIN_COVER for n in nc) / len(nc):.1%}; coverage share {sum(nc) / sum(nu):.2%}"
    )
    weeks = [w for w in allw if len(w["rows"]) >= MIN_N]
    rows = [r for w in weeks for r in w["rows"]]
    print(f"weekends used (N >= {MIN_N}): {len(weeks)}; symbol-weekends {len(rows)}; distinct symbols {len({r[0] for r in rows})}")
    print(f"weekday-placebo rows missing a price: {sum(r[IX['Wd']] is None or r[IX['Td']] is None for r in rows)}")
    print(f"maker fill flag known {sum(r[IX['fill']] is not None for r in rows)}; |T| > 50 %: {sum(abs(r[IX['T']]) > 0.5 for r in rows)}")
    for c in ("W", "T", "beta", "tr"):
        v = sorted(col(rows, c))
        print(f"  {c:<5} p1 {v[len(v) // 100]:+.4f} p50 {v[len(v) // 2]:+.4f} p99 {v[99 * len(v) // 100]:+.4f}")
    xsd = [float(np.std(col(w["rows"], "W"))) for w in weeks]
    print(f"cross-sectional SD of W per weekend: median {statistics.median(xsd):.4f}")
    rho = [rank_ic(col(w["rows"], "W"), col(w["rows"], "r7")) for w in weeks if all(v is not None for v in col(w["rows"], "r7"))]
    print(f"rho(W, R7) cross-sectional: mean {statistics.fmean(rho):+.3f} (n weeks {len(rho)})")
    pairs = [(statistics.median(col(w["rows"], "W")), w["btc_we"]) for w in weeks if w["btc_we"] is not None]
    a, b = np.array(pairs).T
    print(
        f"corr over weekends (median W, BTC weekend return) {np.corrcoef(a, b)[0, 1]:+.3f}; with sign(BTC) {np.corrcoef(a, np.sign(b))[0, 1]:+.3f}; "
        f"BTC up weekends {int(np.sum(b > 0))}/{b.size}"
    )
    print(
        f"weekends where one coin dominates W (> 10 MAD from the median): {sum(dominated(col(w['rows'], 'W')) for w in weeks) / len(weeks):.1%}"
    )
    # power gate: SD of the weekly IC series, no means
    z = holm_z(N_TESTS)
    res: dict[str, Any] = {"level_check": raw["level_check"], "median_cover": statistics.median(nc)}
    for lab, s, t in (("H1 weekend IC(W,T)", "W", "T"), ("weekday placebo IC(Wd,Td)", "Wd", "Td")):
        ics = [x for x in (ic_of(w["rows"], s, t) for w in weeks) if x is not None]
        se = newey_west_se(ics, NW_LAG)
        res[s] = {"n": len(ics), "sd": statistics.stdev(ics), "se": se, "mde": 2.8 * se, "mde_holm": z * se}
        print(
            f"  {lab:<28} n={len(ics)} SD={statistics.stdev(ics):.4f} SE_NW{NW_LAG}={se:.4f} MDE={2.8 * se:.4f} MDE_Holm{N_TESTS}={z * se:.4f}"
        )
    # H2 tercile difference: SE of the difference of two tercile means (iid over weekends)
    ics = [ic_of(w["rows"], "W", "T") for w in weeks]
    thin = [statistics.median(col(w["rows"], "tr")) for w in weeks]
    lab = tercile_labels(thin)
    g0 = [x for x, g in zip(ics, lab, strict=True) if g == 0 and x is not None]
    g2 = [x for x, g in zip(ics, lab, strict=True) if g == 2 and x is not None]
    se_d = math.sqrt(statistics.variance(g0) / len(g0) + statistics.variance(g2) / len(g2))
    print(f"  H2 tercile difference: n {len(g0)}/{len(g2)} SE={se_d:.4f} MDE={2.8 * se_d:.4f} (registered threshold 0.02; descriptive)")
    gate = res["W"]["mde_holm"] <= GATE and res["median_cover"] >= MIN_COVER
    res["gate"] = gate
    print(
        f"GATE: MDE_Holm{N_TESTS}(IC) {res['W']['mde_holm']:.4f} <= {GATE} and median 1h cover {res['median_cover']} >= {MIN_COVER}: "
        f"{'PASS' if gate else 'FAIL -> inconclusive, no returns'}"
    )
    fc.wr(out / "stage0.json", res)
    print(f"done; {rss()}")


# ---------------------------------------------------------------- full test


def stat(xs: list[float]) -> tuple[float, float, float, float]:
    m = statistics.fmean(xs)
    se = newey_west_se(xs, NW_LAG)
    return m, se, m / se if se > 0 else float("nan"), two_sided_p(m / se) if se > 0 else 1.0


def fmt(label: str, xs: list[float], unit: float = 1.0, nd: int = 4) -> str:
    m, se, t, p = stat(xs)
    u = unit
    return f"    {label:<52} n={len(xs):>3} mean {m * u:+.{nd}f} CI95 [{(m - 1.96 * se) * u:+.{nd}f}; {(m + 1.96 * se) * u:+.{nd}f}] t {t:+.2f} p {p:.4f}"


def leg(
    rows: list[list[Any]], sig: str, tgt: str, *, largest: bool, f_alt: str, f_btc: float, cost: float, k: int | None = None
) -> dict[str, float] | None:
    """Short (largest=True) the top quintile of ``sig`` or long (False) the bottom one; beta-neutral; per unit of capital."""
    rr = [r for r in rows if r[IX[sig]] is not None and r[IX[tgt]] is not None]
    if len(rr) < MIN_N:
        return None
    k = k or quintile_size(len(rr))
    by = {r[0]: r for r in rr}
    pick = top_k({r[0]: r[IX[sig]] for r in rr}, k, largest=largest)
    w = capped_weight(k, cap=CAP)
    side = -1.0 if largest else 1.0
    gross = side * w * sum(by[s][IX[tgt]] for s in pick)
    bsum = w * sum(by[s][IX["beta"]] for s in pick)
    # short alt receives +rate; the BTC hedge is long beta (pays +rate); mirrored for the long leg
    fund = -side * w * sum(by[s][IX[f_alt]] for s in pick) + side * bsum * f_btc
    costs = cost * w * k + COST_HEDGE * abs(bsum)
    return {"gross": gross, "fund": fund, "cost": costs, "net": gross + fund - costs, "k": k, "w": w}


def stage_full(data_dir: Path, out: Path, n_perm: int) -> None:
    s0 = fc.rd(out / "stage0.json")
    if not s0.get("gate"):
        raise SystemExit("stage0 gate failed: the card is inconclusive without returns")
    info = fc.rd(data_dir / "exchange_info.json")["info"]
    weeks = load_weeks(out)
    mons = [w["sat"] + 2 for w in weeks]
    ic = [ic_of(w["rows"], "W", "T") for w in weeks]
    assert all(x is not None for x in ic)
    ics: list[float] = [float(x) for x in ic]  # type: ignore[arg-type]
    is_m = [m < OOS_MON for m in mons]
    print(f"weekends {len(weeks)} ({ymd(mons[0])}..{ymd(mons[-1])}); IS {sum(is_m)} OOS {len(is_m) - sum(is_m)}; {rss()}")
    print("\nH1 (registered): mean weekly rank IC(W, T); NW lag 2")
    print(fmt("all weekends", ics))
    iso = [x for x, k in zip(ics, is_m, strict=True) if k]
    oos = [x for x, k in zip(ics, is_m, strict=True) if not k]
    print(fmt("IS (Mondays < 2025-03-03)", iso))
    print(fmt("OOS (Mondays >= 2025-03-03)", oos))
    nt = without_top(ics, 10, -1.0)
    print(fmt("without the 10 most negative weekends", nt))
    bb = block_bootstrap_ci(ics, block=4, n_boot=5000, seed=SEED)
    print(f"    block bootstrap (4) CI95 [{bb[0]:+.4f}; {bb[1]:+.4f}]")
    m, se, _, _ = stat(ics)
    # placebos
    print("\nplacebos")
    sig = [{r[0]: r[IX["W"]] for r in w["rows"]} for w in weeks]
    tgt = [{r[0]: r[IX["T"]] for r in w["rows"]} for w in weeks]
    shift_max = 0.0
    for k in (-5, -4, -3, -2, -1, 1, 2, 3, 4, 5):
        xs = [x for x in (cross_ic(sig[i], tgt[i + k]) for i in range(len(weeks)) if 0 <= i + k < len(weeks)) if x is not None]
        mk, sek, _, _ = stat(xs)
        shift_max = max(shift_max, abs(mk))
        print(f"    target shifted {k:+d} weeks: n={len(xs)} IC {mk:+.4f} (SE {sek:.4f})")
    rng = np.random.default_rng(SEED)
    pm = []
    t0 = time.time()
    for _ in range(n_perm):
        perm = rng.permutation(len(weeks))
        xs = [x for x in (cross_ic(sig[i], tgt[perm[i]]) for i in range(len(weeks)) if perm[i] != i) if x is not None]
        pm.append(statistics.fmean(xs))
    pa = np.array(pm)
    print(
        f"    week permutation ({n_perm}): mean {pa.mean():+.4f} SD {pa.std():.4f}; p(|IC*| >= |obs|) "
        f"{(1 + int(np.sum(np.abs(pa) >= abs(m)))) / (1 + n_perm):.4f} ({time.time() - t0:.0f}s)"
    )
    wp = placebo_mean_ics([(col(w["rows"], "W"), col(w["rows"], "T")) for w in weeks], n_perm, seed=SEED)
    print(f"    within-week W shuffle ({n_perm}): SD {wp.std():.4f}; p {(1 + int(np.sum(np.abs(wp) >= abs(m)))) / (1 + n_perm):.4f}")
    h1 = (
        m <= -0.03
        and m + 1.96 * se < 0
        and statistics.fmean(iso) < 0
        and statistics.fmean(oos) < 0
        and statistics.fmean(nt) < 0
        and shift_max < 0.02
    )
    flag = m - 1.96 * se < -0.05
    print(
        f"H1 verdict: {'CONFIRMED' if h1 else 'NOT CONFIRMED'} (mean <= -0.03: {m <= -0.03}; CI upper < 0: {m + 1.96 * se < 0}; "
        f"IS<0 {statistics.fmean(iso) < 0}; OOS<0 {statistics.fmean(oos) < 0}; w/o top10 <0 {statistics.fmean(nt) < 0}; "
        f"max |shift placebo| < 0.02: {shift_max < 0.02}); CI lower < -0.05 artefact flag: {flag}"
    )
    # H2
    print("\nH2 (registered, descriptive by power)")
    thin = [statistics.median(col(w["rows"], "tr")) for w in weeks]
    lab = tercile_labels(thin)
    g = {t: [x for x, la in zip(ics, lab, strict=True) if la == t] for t in (0, 1, 2)}
    for t in (0, 1, 2):
        th = [x for x, la in zip(thin, lab, strict=True) if la == t]
        print(fmt(f"tercile {t} of weekend turnover ratio (median {statistics.median(th):.2f})", g[t]))
    d_t = statistics.fmean(g[0]) - statistics.fmean(g[2])
    se_t = math.sqrt(statistics.variance(g[0]) / len(g[0]) + statistics.variance(g[2]) / len(g[2]))
    print(f"    low - high tercile: {d_t:+.4f} CI95 [{d_t - 1.96 * se_t:+.4f}; {d_t + 1.96 * se_t:+.4f}] (threshold <= -0.02)")
    wd = [ic_of(w["rows"], "Wd", "Td") for w in weeks]
    pair = [(a, b) for a, b in zip(ics, wd, strict=True) if b is not None]
    print(fmt("weekday placebo calendar IC(Wd, Td) (Tue-Wed -> Thu)", [b for _, b in pair]))
    diff = [b - a for a, b in pair]
    print(fmt("weekday IC - weekend IC (paired; threshold >= +0.02)", diff))
    dm = statistics.fmean(diff)
    h2a, h2b = d_t <= -0.02, dm >= 0.02
    print(
        f"H2 verdict: tercile part {'met' if h2a else 'not met'}; calendar part {'met' if h2b else 'not met'} -> {'CONFIRMED' if h2a and h2b else 'NOT CONFIRMED'}"
    )
    # symbol-level split (descriptive, not registered as a hypothesis)
    lo_ic, hi_ic = [], []
    for w in weeks:
        rr = w["rows"]
        la = tercile_labels(col(rr, "tr"))
        for t, acc in ((0, lo_ic), (2, hi_ic)):
            sub = [r for r, x in zip(rr, la, strict=True) if x == t]
            acc.append(rank_ic(col(sub, "W"), col(sub, "T")))
    print(fmt("[descr] IC inside the thinnest third of coins (by own ratio)", lo_ic))
    print(fmt("[descr] IC inside the thickest third of coins", hi_ic))
    # variants (all reported; exploratory)
    print("\nvariants of the calendar (exploratory, not registered)")
    for lab_v, s, t in (
        ("no gap: Sat 00 -> Mon 00, Mon 00 -> Tue 00 (bounce-contaminated)", "W0", "T0"),
        ("registered W, 72 h hold Mon 01 -> Thu 01", "W", "T72"),
        ("registered W, Tue 00 -> Wed 00 (critic's no-bounce control)", "W", "TT"),
    ):
        xs = [x for x in (ic_of(w["rows"], s, t) for w in weeks) if x is not None]
        print(fmt(lab_v, xs))
    # BTC regime
    up = [x for x, w in zip(ics, weeks, strict=True) if (w["btc_we"] or 0) > 0]
    dn = [x for x, w in zip(ics, weeks, strict=True) if (w["btc_we"] or 0) <= 0]
    print(fmt("BTC up over the weekend (descriptive)", up))
    print(fmt("BTC down / flat over the weekend (descriptive)", dn))
    alive = [
        x
        for x in (ic_of([r for r in w["rows"] if (info.get(r[0]) or {}).get("status") == "TRADING"], "W", "T") for w in weeks)
        if x is not None
    ]
    print(fmt("TRADING-only universe (survivorship, descriptive)", alive))
    # H3 and baselines
    print(
        f"\nH3 (registered thresholds; {'gated: H1 and H2 confirmed' if h1 and h2a and h2b else 'descriptive: H1/H2 not both confirmed'}); per unit of leg capital, %"
    )
    res = {}
    for lab_l, kw in (
        (
            "short top quintile W (registered)",
            {"sig": "W", "tgt": "T", "largest": True, "f_alt": "f_we", "fb": "btc_f_we", "cost": COST_ALT},
        ),
        ("  same, all-taker entry", {"sig": "W", "tgt": "T", "largest": True, "f_alt": "f_we", "fb": "btc_f_we", "cost": COST_ALT_TAKER}),
        ("  same, slippage x2", {"sig": "W", "tgt": "T", "largest": True, "f_alt": "f_we", "fb": "btc_f_we", "cost": COST_ALT + 0.001}),
        (
            "long bottom quintile W (mirror)",
            {"sig": "W", "tgt": "T", "largest": False, "f_alt": "f_we", "fb": "btc_f_we", "cost": COST_ALT},
        ),
        (
            "short top quintile W, 72 h (secondary)",
            {"sig": "W", "tgt": "T72", "largest": True, "f_alt": "f_72", "fb": "btc_f_72", "cost": COST_ALT},
        ),
        ("base: short top quintile R7", {"sig": "r7", "tgt": "T", "largest": True, "f_alt": "f_we", "fb": "btc_f_we", "cost": COST_ALT}),
        (
            "base: weekday short top quintile Wd",
            {"sig": "Wd", "tgt": "Td", "largest": True, "f_alt": "f_wd", "fb": "btc_f_wd", "cost": COST_ALT},
        ),
    ):
        legs = []
        for w in weeks:
            rr = w["rows"] if kw["sig"] != "Wd" else [r[: IX["beta"]] + [r[IX["beta_d"]]] + r[IX["beta"] + 1 :] for r in w["rows"]]
            x = leg(rr, kw["sig"], kw["tgt"], largest=kw["largest"], f_alt=kw["f_alt"], f_btc=w[kw["fb"]], cost=kw["cost"])
            if x is not None:
                legs.append(x)
        res[lab_l] = legs
        gr, nt_ = [x["gross"] for x in legs], [x["net"] for x in legs]
        print(fmt(f"{lab_l}: gross", gr, 100, 3))
        print(fmt(f"{lab_l}: net", nt_, 100, 3))
        if lab_l.startswith("short top quintile W (reg"):
            print(
                f"      funding mean {statistics.fmean(x['fund'] for x in legs) * 100:+.3f}, costs mean {statistics.fmean(x['cost'] for x in legs) * 100:.3f}"
            )
    rnd = []
    rr_rng = random.Random(SEED)
    for w in weeks:
        rr = [r for r in w["rows"]]
        k = quintile_size(len(rr))
        acc = []
        for _ in range(50):
            pick = rr_rng.sample(rr, k)
            acc.append(-capped_weight(k, cap=CAP) * sum(r[IX["T"]] for r in pick))
        rnd.append(statistics.fmean(acc))
    print(fmt("base: short random quintile, gross", rnd, 100, 3))
    reg = res["short top quintile W (registered)"]
    gr, ne = [x["gross"] for x in reg], [x["net"] for x in reg]
    half = len(ne) // 2
    nt10 = without_top(ne, 10, 1.0)
    h3 = (
        statistics.fmean(gr) >= 0.003
        and statistics.fmean(ne) >= 0.001
        and statistics.fmean(ne[:half]) > 0
        and statistics.fmean(ne[half:]) > 0
        and statistics.fmean(nt10) > 0
    )
    print(
        f"    halves net: {statistics.fmean(ne[:half]) * 100:+.3f} / {statistics.fmean(ne[half:]) * 100:+.3f}; without top-10 weekends net "
        f"{statistics.fmean(nt10) * 100:+.3f}; per year (52) net {statistics.fmean(ne) * 52 * 100:+.2f} % on leg capital"
    )
    print(f"H3 thresholds met: {h3} (gross short >= +0.30 pp, net >= +0.10 pp, halves > 0, w/o top10 > 0)")
    # maker fill and selection
    fl, nf = [], []
    for w in weeks:
        rr = w["rows"]
        k = quintile_size(len(rr))
        for s in top_k({r[0]: r[IX["W"]] for r in rr}, k):
            r = next(x for x in rr if x[0] == s)
            (fl if r[IX["fill"]] else nf).append(-r[IX["T"]])
    print(
        f"    maker entry at Mon 01:00 open, top quintile: filled {len(fl) / (len(fl) + len(nf)):.1%}; short gross filled "
        f"{statistics.fmean(fl) * 100:+.3f} % vs not filled {statistics.fmean(nf) * 100:+.3f} % (adverse selection if filled < not filled)"
    )
    caps = [
        statistics.median(
            [r[IX["mqv"]] for r in w["rows"] if r[0] in top_k({x[0]: x[IX["W"]] for x in w["rows"]}, quintile_size(len(w["rows"])))]
        )
        for w in weeks
    ]
    print(f"    capacity: median 30d turnover of top-quintile names {statistics.median(caps) / 1e6:.1f} M USD/day (1 % of it per name)")
    # per asset (5a): IC contributions per asset
    vals, assets, clus = [], [], []
    for w in weeks:
        pr = ic_products(col(w["rows"], "W"), col(w["rows"], "T"))
        vals.extend((-pr).tolist())  # oriented: positive = reversal
        assets.extend(col(w["rows"], "sym"))
        clus.extend([w["sat"]] * len(w["rows"]))
    het = heterogeneity(vals, assets, min_n=20)
    pq = permutation_q(vals, assets, clus, min_n=20, n_perm=500, seed=SEED)
    dmask = {w["sat"]: (w["sat"] + 2) < OOS_MON for w in weeks}
    ism = [dmask[c] for c in clus]
    print(
        f"\nper asset (oriented -IC contributions): k={het['k']} (>= 20 weekends) Q={het['q']:.1f} df={het['df']} p={het['p']:.4f} "
        f"I2={het['i2']:.2f}; within-week label permutation p={pq['p']:.4f} (Q null mean {pq['null_mean']:.1f})"
    )
    for lab_p, kw2 in (("top 10", {"top_n": 10}), ("top quarter", {"top_frac": 0.25})):
        r = persistence(vals, assets, clus, ism, min_n_is=20, n_boot=2000, seed=SEED, **kw2)
        print(
            f"    persistence {lab_p}: IS top {r['top_is_mean']:+.4f} rest {r['rest_is_mean']:+.4f} -> OOS top {r['top_oos_mean']:+.4f} "
            f"(n {r['n_top_oos']}) rest {r['rest_oos_mean']:+.4f}; diff {r['diff']:+.4f} CI [{r['diff_ci'][0]:+.4f}; {r['diff_ci'][1]:+.4f}] "
            f"p {r['diff_p']:.3f}; rho IS-OOS {r['rho']:+.3f} (k {r['rho_n']})"
        )
    yrs: dict[str, list[float]] = {}
    for x, mo in zip(ics, mons, strict=True):
        yrs.setdefault(ymd(mo)[:4], []).append(x)
    print("\nby year: " + "; ".join(f"{y} {statistics.fmean(v):+.4f} (n {len(v)})" for y, v in sorted(yrs.items())))
    fc.wr(out / "full.json", {"ic": ics, "mons": mons, "h1": h1, "h2": [h2a, h2b], "h3": h3})
    print(f"done; {rss()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=("universe", "fetch", "stage0", "full"))
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--n-perm", type=int, default=1000)
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "universe":
        stage_universe(a.data_dir, a.cache_dir)
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(a.cache_dir, a.sample))
    elif a.stage == "stage0":
        stage0(a.data_dir, a.cache_dir)
    else:
        stage_full(a.data_dir, a.cache_dir, a.n_perm)


if __name__ == "__main__":
    main()
