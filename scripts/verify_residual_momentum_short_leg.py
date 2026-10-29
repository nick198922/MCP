"""Verify IDEA-20261029-residual-momentum-short-leg: does BTC-beta-cleaned 4-week momentum (RM28) rank next-week
beta-neutral returns of Binance USDT-M perps, and does a short of its bottom quintile (BTC-beta hedged, 4-week
staggered hold) earn money after costs and funding? Pre-registration: the card's section "Попередня реєстрація".

No network, no keys, no orders. Inputs (read-only): ``--panel`` (scripts/verify_xs_factor_ic_panel.py panel.json:
point-in-time universe incl. delisted symbols, M28 / R7 / MAX30 / IVOL30, trailing funding, median turnover) and
``--data-dir`` (scripts/verify_funding_crowding.py cache: daily klines and funding). Output: ``--cache-dir``.

Stages:
    build    weekly table: universe x (RM28, RM56, betas, controls, 4 forward weekly legs with funding) + BTC legs
    stage0   data checks, gates G1-G3 and the correlations of RM28 with M28 / IVOL30 / MAX30 -- never a mean return
    full     the registered single run: H1, H3, H2 (staggered short book), placebo, robustness, per-asset check

    uv run python scripts/verify_residual_momentum_short_leg.py build | stage0 | full
"""

from __future__ import annotations

import argparse
import math
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_funding_crowding as fc  # noqa: E402  (loader of the kline / funding cache)

from tradedesk.analytics.funding_crowding import block_bootstrap_ci, leg_week, newey_west_se, unit_cost  # noqa: E402
from tradedesk.analytics.listing_drift import DAY_MS  # noqa: E402
from tradedesk.analytics.oi_turnover import quintiles  # noqa: E402
from tradedesk.analytics.per_asset import asset_stats, heterogeneity, permutation_q, persistence  # noqa: E402
from tradedesk.analytics.residual_momentum import (  # noqa: E402
    hedge_beta,
    inverse_vol_weights,
    placebo_shift_means,
    residual_momentum,
    staggered_book,
    weighted_partial_rank_ic,
)
from tradedesk.analytics.xs_factors import (  # noqa: E402
    ic_products,
    partial_rank_ic,
    placebo_mean_ics,
    rank_ic,
    two_sided_p,
    window_return,
    without_top,
)

CACHE = Path("~/.cache/tradedesk-agents/residual_momentum_short_leg").expanduser()
PANEL = Path("~/.cache/tradedesk-agents/xs_factor_ic_panel/panel.json").expanduser()
DATA = Path("~/.cache/tradedesk-agents/funding_crowding").expanduser()
PCOLS = ("sym", "R7", "M28", "MAX30", "IVOL30", "beta", "fund", "mqv", "r", "r_lag1", "fund_long", "trunc")
PX = {c: i for i, c in enumerate(PCOLS)}
HOLD = 4
COLS = ("sym", "RM28", "RM56", "beta_s", "rsd", "beta_h", "M28", "R7", "MAX30", "IVOL30", "fund", "mqv", "raw28", "r_panel") + tuple(
    f"{x}{k}" for k in range(HOLD) for x in ("r", "f")
)
IX = {c: i for i, c in enumerate(COLS)}
BTC = "BTCUSDT"
MIN_N = 50
NW_LAG = 4
SEED = 7
OOS_DAY = int(datetime(2025, 3, 3, tzinfo=UTC).timestamp()) // 86400
RT_COST = 0.0015  # 15 bp per round trip (card)
SHIFTS = tuple(range(5, 21))  # placebo shifts, weeks
CTRL = ("fund", "mqv", "IVOL30", "MAX30")


def rss() -> str:
    return f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- build


def stage_build(panel: Path, data_dir: Path, cache: Path) -> None:
    t0 = time.time()
    pw = [w for w in fc.rd(panel)["weeks"] if len(w["rows"]) >= MIN_N]
    data = fc.load_all(data_dir)
    print(f"panel weeks {len(pw)}; loaded {len(data)} symbols in {time.time() - t0:.0f}s; {rss()}", flush=True)
    btc = data[BTC]
    why: Counter[str] = Counter()
    weeks = []
    for i, w in enumerate(pw):
        day = w["day"]
        bl = []
        for k in range(HOLD):
            dk = day + 7 * k
            lg = leg_week(BTC, btc.bars, btc.fslice(dk * DAY_MS - 2 * DAY_MS, (dk + 9) * DAY_MS), dk)
            bl.append([lg.r, lg.funding_long] if lg else None)
        btc7 = window_return(btc.bars, day, 8, 1)
        rows = []
        for pr in w["rows"]:
            s = pr[PX["sym"]]
            if s == BTC:
                why["btc_is_hedge"] += 1
                continue
            d = data[s]
            rm = residual_momentum(d.bars, btc.bars, day)
            if rm is None:
                why["no_rm28"] += 1
                continue
            rm56 = residual_momentum(d.bars, btc.bars, day, form=56)
            bh = hedge_beta(d.bars, btc.bars, day)
            if bh is None:
                why["no_hedge_beta"] += 1
                continue
            legs: list[Any] = []
            for k in range(HOLD):
                dk = day + 7 * k
                lg = leg_week(s, d.bars, d.fslice(dk * DAY_MS - 2 * DAY_MS, (dk + 9) * DAY_MS), dk)
                legs += [lg.r, lg.funding_long] if lg else [None, None]
            if legs[0] is None:
                why["no_leg0"] += 1
                continue
            rows.append(
                [
                    s,
                    rm.rm,
                    rm56.rm if rm56 else None,
                    rm.beta,
                    rm.resid_sd,
                    bh,
                    pr[PX["M28"]],
                    pr[PX["R7"]],
                    pr[PX["MAX30"]],
                    pr[PX["IVOL30"]],
                    pr[PX["fund"]],
                    pr[PX["mqv"]],
                    window_return(d.bars, day, 36, 8),
                    pr[PX["r"]],
                    *legs,
                ]
            )
            why["ok"] += 1
        weeks.append({"day": day, "btc": bl, "btc7": btc7, "rows": rows})
        if (i + 1) % 50 == 0:
            print(f"  week {i + 1} {ymd(day)} N={len(rows)} ({time.time() - t0:.0f}s; {rss()})", flush=True)
    fc.wr(cache / "table.json", {"weeks": weeks, "why": dict(why), "cols": COLS})
    print(f"BUILD DONE {len(weeks)} weeks, {why['ok']} symbol-weeks; {dict(why)}; {time.time() - t0:.0f}s; {rss()}", flush=True)
    fc.sanity(data)


# ---------------------------------------------------------------- shared


def load_table(cache: Path) -> list[dict[str, Any]]:
    t = fc.rd(cache / "table.json")
    return [w for w in t["weeks"] if len(w["rows"]) >= MIN_N and w["btc"][0] is not None]


def col(rows: list[list[Any]], c: str) -> list[Any]:
    return [r[IX[c]] for r in rows]


def rres(w: dict[str, Any], rows: list[list[Any]] | None = None, k: int = 0) -> list[float]:
    """Beta-neutral return of week k (hedge beta fixed at formation); 0 for a leg that no longer exists (cash)."""
    rb = w["btc"][k][0] if w["btc"][k] else 0.0
    out = []
    for r in w["rows"] if rows is None else rows:
        x = r[IX[f"r{k}"]]
        out.append(0.0 if x is None else x - r[IX["beta_h"]] * rb)
    return out


def ctrls(rows: list[list[Any]]) -> list[list[float]]:
    return [[math.log(v) if c == "mqv" else v for v in col(rows, c)] for c in CTRL]


def ic_neu(w: dict[str, Any], sig: str = "RM28", rows: list[list[Any]] | None = None) -> float:
    rows = [r for r in (w["rows"] if rows is None else rows) if r[IX[sig]] is not None]
    return partial_rank_ic(col(rows, sig), rres(w, rows), ctrls(rows))


def ic_neu_w(w: dict[str, Any]) -> float:
    rows = w["rows"]
    return weighted_partial_rank_ic(col(rows, "RM28"), rres(w), ctrls(rows), inverse_vol_weights(col(rows, "rsd")).tolist())


def summ(xs: list[float], boot: bool = False) -> dict[str, float]:
    m = statistics.fmean(xs)
    se = newey_west_se(xs, NW_LAG)
    lo, hi = block_bootstrap_ci(xs, seed=SEED) if boot else (m - 1.96 * se, m + 1.96 * se)
    return {
        "n": len(xs),
        "mean": m,
        "se": se,
        "t": m / se if se > 0 else float("nan"),
        "p": two_sided_p(m / se) if se > 0 else 1.0,
        "lo": lo,
        "hi": hi,
    }


def fmt(label: str, s: dict[str, float], scale: float = 1.0, unit: str = "") -> str:
    return (
        f"  {label:<58} n={s['n']:>4}  mean {s['mean'] * scale:+.4f}{unit}  CI [{s['lo'] * scale:+.4f}; {s['hi'] * scale:+.4f}]"
        f"  t_NW {s['t']:+.2f}  p {s['p']:.3f}"
    )


def cohort_path(w: dict[str, Any], names: list[str], *, cost_fn: Callable[[list[list[Any]], float], float]) -> dict[str, list[float]]:
    """Per holding week k of a short cohort (equal weight, BTC long of size mean beta_h): beta-neutral price return of
    the names (the H2 'gross' quantity, negative = good for the short), funding P&L of short + hedge, cost (round trip
    charged in k = 0), and the net P&L of the short book."""
    by = {r[0]: r for r in w["rows"]}
    rows = [by[s] for s in names]
    price, fund, cost, net = [], [], [], []
    for k in range(HOLD):
        bk = w["btc"][k]
        rb, fb = (bk[0], bk[1]) if bk else (0.0, 0.0)
        rr, fl, be = [], [], []
        for r in rows:
            x = r[IX[f"r{k}"]]
            if x is None:  # delisted / no bar: position is cash, hedge closed
                rr.append(0.0)
                fl.append(0.0)
                be.append(0.0)
            else:
                rr.append(x - r[IX["beta_h"]] * rb)
                fl.append(r[IX[f"f{k}"]])
                be.append(r[IX["beta_h"]])
        n = len(rows)
        p = sum(rr) / n
        f = -sum(fl) / n + sum(be) / n * fb
        c = cost_fn(rows, sum(r[IX["beta_h"]] for r in rows) / n) if k == 0 else 0.0
        price.append(p)
        fund.append(f)
        cost.append(c)
        net.append(-p + f - c)
    return {"price": price, "fund": fund, "cost": cost, "net": net}


def card_cost(rows: list[list[Any]], beta_mean: float) -> float:
    return RT_COST * (1.0 + abs(beta_mean))


def tier_cost(mult: float) -> Callable[[list[list[Any]], float], float]:
    def f(rows: list[list[Any]], beta_mean: float) -> float:
        alt = statistics.fmean(2 * unit_cost(r[IX["mqv"]], slip_mult=mult) for r in rows)
        return alt + abs(beta_mean) * 2 * unit_cost(1e9, slip_mult=mult)

    return f


def book(
    weeks: list[dict[str, Any]], pick: Callable[[dict[str, Any]], list[str]], cost_fn: Callable[[list[list[Any]], float], float] = card_cost
) -> dict[str, Any]:
    """Staggered 4-week short book on consecutive Mondays; weeks whose 4 cohorts are not all present are skipped."""
    paths = {w["day"]: cohort_path(w, pick(w), cost_fn=cost_fn) for w in weeks}
    out: dict[str, Any] = defaultdict(list)
    for w in weeks:
        d = w["day"]
        if not all(d - 7 * k in paths for k in range(HOLD)):
            continue
        out["day"].append(d)
        for key in ("price", "fund", "cost", "net"):
            # cohorts formed at d-21, d-14, d-7, d (in order); the book's week d is the last output of staggered_book
            ps = [paths[d - 7 * (HOLD - 1 - j)][key] for j in range(HOLD)]
            out[key].append(staggered_book(ps, HOLD)[-1])
    return out


def bottom(sig: str) -> Callable[[dict[str, Any]], list[str]]:
    def f(w: dict[str, Any]) -> list[str]:
        rows = [r for r in w["rows"] if r[IX[sig]] is not None]
        return quintiles({r[0]: r[IX[sig]] for r in rows})[0]

    return f


# ---------------------------------------------------------------- stage 0 (no means)


def stage0(cache: Path) -> None:
    t = fc.rd(cache / "table.json")
    print(f"build: {t['why']}")
    weeks = load_table(cache)
    days = [w["day"] for w in weeks]
    ns = [len(w["rows"]) for w in weeks]
    print(
        f"weeks with N >= {MIN_N}: {len(weeks)} of {len(t['weeks'])} ({ymd(days[0])} .. {ymd(days[-1])}); IS {sum(d < OOS_DAY for d in days)}, OOS {sum(d >= OOS_DAY for d in days)}"
    )
    print(
        f"N per week: min {min(ns)}, median {statistics.median(ns)}, max {max(ns)}; symbol-weeks {sum(ns)}; symbols {len({r[0] for w in weeks for r in w['rows']})}"
    )
    allr = [r for w in weeks for r in w["rows"]]
    dif = [abs(r[IX["r0"]] - r[IX["r_panel"]]) for r in allr]
    print(f"consistency: |r0 (rebuilt) - r (xs panel)| max {max(dif):.2e}")
    for c in ("RM28", "RM56", "beta_s", "beta_h", "rsd"):
        v = sorted(x for x in col(allr, c) if x is not None)
        print(f"  {c:<6} p1 {v[len(v) // 100]:+.4f} p50 {v[len(v) // 2]:+.4f} p99 {v[99 * len(v) // 100]:+.4f} (n {len(v)})")
    eth = [r[IX["beta_s"]] for r in allr if r[0] == "ETHUSDT"]
    print(f"  ETHUSDT beta_s median {statistics.median(eth):.3f} over {len(eth)} weeks (expected ~1)")
    miss = sum(sum(r[IX[f"r{k}"]] is None for r in allr) for k in range(1, HOLD))
    print(f"  forward legs k=1..3 missing (delisted / no bar -> cash): {miss}; |r0| > 100 %: {sum(abs(r[IX['r0']]) > 1 for r in allr)}")
    print()
    print("G2: mean weekly cross-sectional Spearman (min / max over weeks)")
    for a, b in (
        ("RM28", "M28"),
        ("RM28", "raw28"),
        ("RM28", "IVOL30"),
        ("RM28", "MAX30"),
        ("RM28", "R7"),
        ("RM28", "beta_s"),
        ("RM56", "M28"),
        ("RM28", "RM56"),
    ):
        cs = []
        for w in weeks:
            rows = [r for r in w["rows"] if r[IX[a]] is not None and r[IX[b]] is not None]
            cs.append(rank_ic(col(rows, a), col(rows, b)))
        print(f"  rho({a}, {b}) = {statistics.fmean(cs):+.3f} ({min(cs):+.3f} / {max(cs):+.3f})")
    g2 = statistics.fmean(rank_ic(col(w["rows"], "RM28"), col(w["rows"], "M28")) for w in weeks)
    print(f"  G2 |rho(RM28, M28)| = {abs(g2):.3f} < 0.85 -> {'PASS' if abs(g2) < 0.85 else 'FAIL -> rejected without returns'}")
    print()
    ics = [ic_neu(w) for w in weeks]
    raw = [rank_ic(col(w["rows"], "RM28"), rres(w)) for w in weeks]
    se, ser = newey_west_se(ics, NW_LAG), newey_west_se(raw, NW_LAG)
    oos = [x for x, d in zip(ics, days, strict=True) if d >= OOS_DAY]
    print(
        f"G1: IC_neu weekly SD {statistics.stdev(ics):.4f} SE_NW {se:.4f} MDE {2.8 * se:.4f} (<= 0.025 -> {'PASS' if 2.8 * se <= 0.025 else 'FAIL'})"
    )
    print(
        f"    raw IC SD {statistics.stdev(raw):.4f} SE_NW {ser:.4f} MDE {2.8 * ser:.4f}; OOS ({len(oos)} wk) MDE {2.8 * newey_west_se(oos, NW_LAG):.4f}"
    )
    sp = []
    for w in weeks:
        rows = w["rows"]
        order = sorted(range(len(rows)), key=lambda i: (rows[i][IX["RM28"]], rows[i][0]))
        rr = rres(w)
        lo_i, hi_i = order[:20], order[-20:]
        wl = inverse_vol_weights([rows[i][IX["rsd"]] for i in lo_i])
        wh = inverse_vol_weights([rows[i][IX["rsd"]] for i in hi_i])
        sp.append(float(np.dot(wh, [rr[i] for i in hi_i]) - np.dot(wl, [rr[i] for i in lo_i])))
    s = newey_west_se(sp, NW_LAG)
    print(
        f"G3: 20/20 spread (1/resid-vol weights) SD {statistics.stdev(sp) * 100:.3f} pp SE_NW {s * 100:.3f} pp MDE {2.8 * s * 100:.3f} pp"
    )
    bk = book(weeks, bottom("RM28"))
    q1 = [statistics.fmean(rres(w, [r for r in w["rows"] if r[0] in set(bottom("RM28")(w))])) for w in weeks]
    for lab, xs in (
        ("staggered short book net (H2 primary)", bk["net"]),
        ("staggered book price (H2 gross)", bk["price"]),
        ("bottom quintile 1-week price", q1),
    ):
        s = newey_west_se(xs, NW_LAG)
        print(f"    {lab}: n {len(xs)} SD {statistics.stdev(xs) * 100:.3f} pp SE_NW {s * 100:.3f} pp MDE {2.8 * s * 100:.3f} pp")
    s = newey_west_se(bk["net"], NW_LAG)
    print(
        f"    G3 leg MDE {2.8 * s * 100:.3f} pp vs 1.0 pp -> {'trading verdict can be confirmed' if 2.8 * s <= 0.01 else 'H2 at most inconclusive (triage condition 2)'}"
    )
    print(rss())


# ---------------------------------------------------------------- full (single registered run)


def stage_full(cache: Path, n_perm: int) -> None:
    weeks = load_table(cache)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    days = [w["day"] for w in weeks]
    is_m = [d < OOS_DAY for d in days]
    say(
        f"weeks {len(weeks)} ({ymd(days[0])} .. {ymd(days[-1])}); IS {sum(is_m)} / OOS {len(weeks) - sum(is_m)}; symbol-weeks {sum(len(w['rows']) for w in weeks)}"
    )

    # ---- H1
    ics = [ic_neu(w) for w in weeks]
    raw = [rank_ic(col(w["rows"], "RM28"), rres(w)) for w in weeks]
    s1 = summ(ics)
    say("\nH1 (auxiliary rank metric): weekly rank IC of RM28 vs next-week beta-neutral return, NW lag 4")
    say(fmt("IC_neu (controls: funding, log turnover, IVOL30, MAX30) PRIMARY", s1))
    say(fmt("IC raw RM28", summ(raw)))
    say(fmt("reference: IC_neu of raw M28 (closed, known ~0)", summ([ic_neu(w, "M28") for w in weeks])))
    say(fmt("descriptive: IC_neu of RM56 (not in the verdict)", summ([ic_neu(w, "RM56") for w in weeks])))
    sis = summ([x for x, f in zip(ics, is_m, strict=True) if f])
    soos = summ([x for x, f in zip(ics, is_m, strict=True) if not f])
    say(fmt("IC_neu IS (.. 2025-02-24)", sis))
    say(fmt("IC_neu OOS (2025-03-03 ..)", soos))
    t0 = time.time()
    pos = {d: i for i, d in enumerate(days)}
    mat = np.full((len(weeks), len(SHIFTS)), np.nan)
    for i, w in enumerate(weeks):
        cur = {r[0]: r for r in w["rows"]}
        for j, k in enumerate(SHIFTS):
            p = pos.get(w["day"] - 7 * k)
            if p is None:
                continue
            old = {r[0]: r[IX["RM28"]] for r in weeks[p]["rows"]}
            rows = [cur[s] for s in cur if s in old]
            if len(rows) >= 30:
                mat[i, j] = partial_rank_ic([old[r[0]] for r in rows], rres(w, rows), ctrls(rows))
    perm = placebo_shift_means(mat, n_perm, seed=SEED)
    say(
        f"  placebo (RM28 from a random week 5-20 weeks earlier, {n_perm}x): mean IC_neu {perm.mean():+.4f}, 2.5/97.5 % {np.percentile(perm, 2.5):+.4f} / {np.percentile(perm, 97.5):+.4f}; "
        f"|mean| >= 0.02 -> {'KILL' if abs(perm.mean()) >= 0.02 else 'ok'} ({time.time() - t0:.0f}s)"
    )
    sh = placebo_mean_ics([(col(w["rows"], "RM28"), rres(w)) for w in weeks], n_perm, seed=SEED)
    mraw = statistics.fmean(raw)
    say(
        f"  shuffle placebo (raw IC, within-week, {n_perm}x): SD {sh.std():.4f}; p(|perm| >= |obs|) {(1 + int(np.sum(np.abs(sh) >= abs(mraw)))) / (1 + n_perm):.4f}"
    )
    say("  by year (IC_neu):")
    for y in sorted({ymd(d)[:4] for d in days}):
        xs = [x for x, d in zip(ics, days, strict=True) if ymd(d)[:4] == y]
        say(f"    {y}: n {len(xs):>3} mean {statistics.fmean(xs):+.4f} SE {statistics.stdev(xs) / math.sqrt(len(xs)):.4f}")
    up = [w["btc7"] is not None and w["btc7"] > 0 for w in weeks]
    say(fmt("BTC trailing 7d up: IC_neu", summ([x for x, f in zip(ics, up, strict=True) if f])))
    say(fmt("BTC trailing 7d down: IC_neu", summ([x for x, f in zip(ics, up, strict=True) if not f])))
    say(fmt("IC_neu without the 10 best weeks", summ(without_top(ics, 10, +1))))
    say(
        fmt(
            "IC_neu excluding symbol-weeks with |r0| > 100 %",
            summ([ic_neu(w, rows=[r for r in w["rows"] if abs(r[IX["r0"]]) <= 1]) for w in weeks]),
        )
    )

    # ---- H3
    say("\nH3: H1 in the top-100 by turnover and with 1/resid-vol weights")
    top100 = [ic_neu(w, rows=sorted(w["rows"], key=lambda r: -r[IX["mqv"]])[:100]) for w in weeks]
    s3a, s3b = summ(top100), summ([ic_neu_w(w) for w in weeks])
    say(fmt("IC_neu top-100 by median turnover", s3a))
    say(fmt("IC_neu weighted 1/resid-vol", s3b))

    # ---- H2
    say(
        "\nH2 (primary trading metric): short bottom RM28 quintile, BTC-beta hedged, 4-week staggered book; per 1 USD short notional, pp/week"
    )
    bk = book(weeks, bottom("RM28"))
    bdays = bk["day"]
    g, n_ = summ(bk["price"]), summ(bk["net"], boot=True)
    say(fmt("gross: beta-neutral price return of the shorted names", g, 100, " pp"))
    say(
        f"      funding to short+hedge {statistics.fmean(bk['fund']) * 100:+.4f} pp, cost {statistics.fmean(bk['cost']) * 100:.4f} pp "
        f"(block bootstrap CI for net below)"
    )
    say(fmt("NET short book (bootstrap CI, block 4)", n_, 100, " pp"))
    half = len(bdays) // 2
    h1n, h2n = summ(bk["net"][:half]), summ(bk["net"][half:])
    say(fmt(f"net first half (.. {ymd(bdays[half - 1])})", h1n, 100, " pp"))
    say(fmt(f"net second half ({ymd(bdays[half])} ..)", h2n, 100, " pp"))
    nt = summ(without_top(bk["net"], 10, +1))
    say(fmt("net without the 10 best weeks", nt, 100, " pp"))
    bis = [d < OOS_DAY for d in bdays]
    say(fmt("net IS", summ([x for x, f in zip(bk["net"], bis, strict=True) if f]), 100, " pp"))
    say(fmt("net OOS", summ([x for x, f in zip(bk["net"], bis, strict=True) if not f]), 100, " pp"))
    for mult in (1.0, 2.0):
        tb = book(weeks, bottom("RM28"), tier_cost(mult))
        say(fmt(f"stress: tier costs (fee 5 bp + slip by turnover) x{mult:.0f} slip, net", summ(tb["net"]), 100, " pp"))
    say(f"  worst book week {min(bk['net']) * 100:+.2f} pp; best {max(bk['net']) * 100:+.2f} pp")
    say("  by year (book net / gross):")
    for y in sorted({ymd(d)[:4] for d in bdays}):
        xs = [x for x, d in zip(bk["net"], bdays, strict=True) if ymd(d)[:4] == y]
        gs = [x for x, d in zip(bk["price"], bdays, strict=True) if ymd(d)[:4] == y]
        say(f"    {y}: n {len(xs):>3} net {statistics.fmean(xs) * 100:+.3f} pp gross {statistics.fmean(gs) * 100:+.3f} pp")
    wk = {w["day"]: w for w in weeks}
    bup = [wk[d]["btc7"] is not None and wk[d]["btc7"] > 0 for d in bdays]
    say(fmt("BTC trailing 7d up: book net", summ([x for x, f in zip(bk["net"], bup, strict=True) if f]), 100, " pp"))
    say(fmt("BTC trailing 7d down: book net", summ([x for x, f in zip(bk["net"], bup, strict=True) if not f]), 100, " pp"))
    q1 = [statistics.fmean(rres(w, [r for r in w["rows"] if r[0] in set(bottom("RM28")(w))])) for w in weeks]
    say(fmt("descriptive: bottom quintile, 1-week hold, gross price", summ(q1), 100, " pp"))
    say(fmt("descriptive: RM56 bottom quintile book net (not in the verdict)", summ(book(weeks, bottom("RM56"))["net"]), 100, " pp"))
    say("  baselines (card item 4):")
    for sig in ("M28", "R7"):
        b2 = book(weeks, bottom(sig))
        say(fmt(f"bottom {sig} quintile book: gross", summ(b2["price"]), 100, " pp"))
        say(fmt(f"bottom {sig} quintile book: net", summ(b2["net"]), 100, " pp"))
    rng = np.random.default_rng(SEED)
    rnd = []
    for _ in range(100):
        picks = {w["day"]: [w["rows"][i][0] for i in rng.choice(len(w["rows"]), len(w["rows"]) // 5, replace=False)] for w in weeks}
        rb_ = book(weeks, lambda w, p=picks: p[w["day"]])
        rnd.append((statistics.fmean(rb_["price"]), statistics.fmean(rb_["net"])))
    rp, rn = sorted(x[0] for x in rnd), sorted(x[1] for x in rnd)
    say(
        f"  random quintile book (100 draws): gross mean {statistics.fmean(rp) * 100:+.4f} pp [{rp[2] * 100:+.4f}; {rp[97] * 100:+.4f}], net mean {statistics.fmean(rn) * 100:+.4f} pp [{rn[2] * 100:+.4f}; {rn[97] * 100:+.4f}]"
    )

    # ---- per asset (5a)
    say("\nPer-asset check (5a)")
    for lab, gen in (
        ("IC contributions z(rank RM28) * z(rank beta-neutral r)", lambda w: ic_products(col(w["rows"], "RM28"), rres(w)).tolist()),
        ("short P&L of bottom-quintile names, 1 week, price only", None),
    ):
        vals, assets, clus = [], [], []
        for w in weeks:
            if gen is not None:
                v = gen(w)
                vals += v
                assets += col(w["rows"], "sym")
                clus += [w["day"]] * len(v)
            else:
                b = set(bottom("RM28")(w))
                rows = [r for r in w["rows"] if r[0] in b]
                vals += [-x for x in rres(w, rows)]
                assets += [r[0] for r in rows]
                clus += [w["day"]] * len(rows)
        mn = 30 if gen is not None else 10
        st = sorted(asset_stats(vals, assets, min_n=mn), key=lambda s: s.mean)
        h = heterogeneity(vals, assets, min_n=mn)
        pq = permutation_q(vals, assets, clus, min_n=mn, n_perm=500, seed=SEED)
        ps = persistence(vals, assets, clus, [c < OOS_DAY for c in clus], min_n_is=mn, top_n=10, n_boot=2000, seed=SEED)
        say(
            f"  [{lab}] assets with >= {mn}: {len(st)}; Q {h['q']:.1f} df {h['df']} p {h['p']:.3f} I2 {h['i2']:.2f}; within-week label permutation p {pq['p']:.3f}"
        )
        say(
            f"    lowest: {[(s.asset, s.n, round(s.mean, 4)) for s in st[:3]]}; highest: {[(s.asset, s.n, round(s.mean, 4)) for s in st[-3:]]}"
        )
        say(
            f"    persistence top-10 IS -> OOS: top {ps['top_oos_mean']:+.4f} (n {ps['n_top_oos']}) vs rest {ps['rest_oos_mean']:+.4f}; diff {ps['diff']:+.4f} "
            f"CI [{ps['diff_ci'][0]:+.4f}; {ps['diff_ci'][1]:+.4f}]; rho IS/OOS {ps['rho']:+.3f} (n {ps['rho_n']})"
        )

    # ---- verdict (registered rules)
    say("\n=== REGISTERED VERDICT ===")
    same = sis["mean"] > 0 and soos["mean"] > 0
    ci0 = s1["lo"] > 0
    kill_pl = abs(perm.mean()) >= 0.02
    if s1["mean"] >= 0.03 and ci0 and same and not kill_pl:
        h1 = "CONFIRMED"
    elif s1["mean"] >= 0.02 and ci0 and same and not kill_pl:
        h1 = "DIRECTIONALLY RIGHT"
    else:
        h1 = "NOT CONFIRMED"
    say(
        f"  H1 inputs: IC_neu {s1['mean']:+.4f} CI lo {s1['lo']:+.4f}; IS {sis['mean']:+.4f} OOS {soos['mean']:+.4f}; shift placebo {perm.mean():+.4f} -> {h1}"
    )
    h3 = "CONFIRMED" if all(x["mean"] > 0 and x["mean"] >= 0.5 * s1["mean"] for x in (s3a, s3b)) and s1["mean"] > 0 else "NOT CONFIRMED"
    say(f"  H3 inputs: top-100 {s3a['mean']:+.4f}, 1/vol {s3b['mean']:+.4f} (need > 0 and >= half of {s1['mean']:+.4f}) -> {h3}")
    mde_leg = 2.8 * newey_west_se(bk["net"], NW_LAG)
    kill2 = g["mean"] > -0.0015 or n_["mean"] < 0 or nt["mean"] <= 0
    ok2 = g["mean"] <= -0.004 and n_["mean"] >= 0.0025 and h1n["mean"] > 0 and h2n["mean"] > 0 and nt["mean"] > 0
    h2 = (
        "NOT CONFIRMED"
        if kill2
        else ("CONFIRMED" if ok2 and mde_leg <= 0.01 else ("UNVERIFIABLE (power)" if ok2 else "DIRECTIONALLY RIGHT"))
    )
    say(
        f"  H2 inputs: gross {g['mean'] * 100:+.3f} pp (<= -0.40 / kill > -0.15); net {n_['mean'] * 100:+.3f} pp (>= +0.25 / kill < 0); halves {h1n['mean'] * 100:+.3f} / {h2n['mean'] * 100:+.3f}; "
        f"no-top10 {nt['mean'] * 100:+.3f}; MDE leg {mde_leg * 100:.2f} pp -> {h2}"
    )
    card = (
        "rejected"
        if "NOT" in h1 or kill2
        else ("confirmed" if h1 == "CONFIRMED" and h2 == "CONFIRMED" and h3 == "CONFIRMED" else "inconclusive")
    )
    say(f"  card status: {card}")
    (cache / "full.txt").write_text("\n".join(lines))
    fc.wr(cache / "full.json", {"ic": ics, "days": days, "book_days": bdays, "book_net": bk["net"], "book_price": bk["price"]})
    say(rss())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["build", "stage0", "full"])
    ap.add_argument("--panel", type=Path, default=PANEL)
    ap.add_argument("--data-dir", type=Path, default=DATA)
    ap.add_argument("--cache-dir", type=Path, default=CACHE)
    ap.add_argument("--perm", type=int, default=1000)
    a = ap.parse_args()
    out = a.cache_dir.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    if a.stage == "build":
        stage_build(a.panel.expanduser(), a.data_dir.expanduser(), out)
    elif a.stage == "stage0":
        stage0(out)
    else:
        stage_full(out, a.perm)


if __name__ == "__main__":
    main()
