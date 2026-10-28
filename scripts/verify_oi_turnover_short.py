"""Verify IDEA-20261028-oi-turnover-leverage-short-basket: does L = OI(USD) / median 7-day turnover rank next-week
beta-neutral returns of Binance USDT-M perps (high L -> lower return)? Pre-registration: the card's section
"Попередня реєстрація". No keys, no orders.

Stages (network only through ``rt.binance_vision`` on ``core.http.Http``; per-file JSON cache under ``--cache-dir``):
    fetch    archive ``metrics`` file of every (panel symbol, Sunday before a rank Monday) -> OI-value snapshot cache
    build    point-in-time weekly table: universe (xs-factor-ic-panel, age >= 90 d), L, controls, weekly legs
    stage0   data checks and the power gate (counts, SD / SE_NW / MDE of the weekly IC; never the mean)
    full     registered tests H1 / H2 / H3, placebo, spread with costs, robustness, per-asset check

Inputs: ``--panel`` (scripts/verify_xs_factor_ic_panel.py panel.json: universe, beta30, trailing funding) and
``--data-dir`` (scripts/verify_funding_crowding.py cache: daily klines and funding, delisted symbols included).

    uv run python scripts/verify_oi_turnover_short.py fetch --sample 300
    uv run python scripts/verify_oi_turnover_short.py fetch --log ~/.cache/tradedesk-agents/oi_turnover_short/fetch.log
    uv run python scripts/verify_oi_turnover_short.py build | stage0 | full
"""

from __future__ import annotations

import argparse
import asyncio
import logging
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_funding_crowding as fc  # noqa: E402  (loader of the kline / funding cache)
import verify_liquidation_flush as vlf  # noqa: E402  (per-file JSON Fetcher)

from tradedesk.analytics.funding_crowding import block_bootstrap_ci, leg_week, newey_west_se, unit_cost  # noqa: E402
from tradedesk.analytics.oi_turnover import (  # noqa: E402
    DAY_MS,
    beta_residual,
    leverage_ratio,
    median_qv,
    neutralized_signal,
    oi_value_snapshot,
    quintiles,
    realized_vol,
    split_diff,
    turnover,
)
from tradedesk.analytics.per_asset import asset_stats, heterogeneity, permutation_q, persistence  # noqa: E402
from tradedesk.analytics.xs_factors import ic_products, partial_rank_ic, placebo_mean_ics, rank_ic, two_sided_p, without_top  # noqa: E402
from tradedesk.server import build_server  # noqa: E402

CACHE = Path("~/.cache/tradedesk-agents/oi_turnover_short").expanduser()
PANEL = Path("~/.cache/tradedesk-agents/xs_factor_ic_panel/panel.json").expanduser()
DATA = Path("~/.cache/tradedesk-agents/funding_crowding").expanduser()
PCOLS = ("sym", "R7", "M28", "MAX30", "IVOL30", "beta", "fund", "mqv", "r", "r_lag1", "fund_long", "trunc")
PX = {c: i for i, c in enumerate(PCOLS)}
# build table row layout
COLS = ("sym", "L", "med7", "mqv", "vol30", "beta", "fund", "r", "fund_long", "trunc", "age", "up", "px_ratio", "r_lag1")
IX = {c: i for i, c in enumerate(COLS)}
BTC = "BTCUSDT"
MIN_AGE = 90
MIN_N = 50
NW_LAG = 4
SEED = 7
GATE = 0.025
OOS_DAY = int(datetime(2025, 7, 7, tzinfo=UTC).timestamp()) // 86400  # first OOS Monday (H3)
BASE_FUND = 0.0001 * 3 * 365  # 0.01 % per 8 h, annualised (Binance interest component)


def rss() -> str:
    return f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


def load_panel(p: Path) -> list[dict[str, Any]]:
    return fc.rd(p)["weeks"]


# ---------------------------------------------------------------- fetch


async def stage_fetch(panel: Path, cache: Path, sample: int | None) -> None:
    need = sorted({(r[0], w["day"] - 1) for w in load_panel(panel) for r in w["rows"] if r[0] != BTC}, key=lambda x: (x[1], x[0]))
    need += [(BTC, w["day"] - 1) for w in load_panel(panel)]
    if sample:
        step = max(1, len(need) // sample)
        need = need[::step][:sample]
    _, rt = build_server()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        fx = vlf.Fetcher(rt, cache, workers=16)
        jobs = []
        for s, d in need:

            async def go(s: str = s, d: int = d) -> Any:
                rows = (await rt.binance_vision.um_metrics_ratios_daily(s, ymd(d))).data
                return {"n_rows": len(rows), "snap": oi_value_snapshot(rows, d)}

            jobs.append((cache / "snap" / s / f"{ymd(d)}.json", go))
        t0 = time.time()
        await fx.many("metrics", jobs)
        print(f"FETCH COMPLETE {len(jobs)} files in {time.time() - t0:.0f}s; {rss()}", flush=True)
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- build


def snap(cache: Path, sym: str, day: int) -> dict[str, Any] | None:
    p = cache / "snap" / sym / f"{ymd(day)}.json"
    return fc.rd(p) if p.exists() else {"missing": True}


def stage_build(panel: Path, data_dir: Path, cache: Path) -> None:
    t0 = time.time()
    weeks = load_panel(panel)
    by_sym: dict[str, list[tuple[int, list[Any]]]] = defaultdict(list)
    for w in weeks:
        for r in w["rows"]:
            by_sym[r[0]].append((w["day"], r))
    btc = fc.load_sym(data_dir, BTC)
    assert btc is not None
    r_btc = {}
    for w in weeks:
        lg = leg_week(BTC, btc.bars, btc.fslice(w["day"] * DAY_MS - 2 * DAY_MS, w["day"] * DAY_MS + 9 * DAY_MS), w["day"])
        l1 = leg_week(BTC, btc.bars, [], w["day"] + 1)
        r_btc[w["day"]] = (lg.r if lg else None, l1.r if l1 else None, lg.funding_long if lg else None)
    out: dict[int, list[list[Any]]] = defaultdict(list)
    why: Counter[str] = Counter()
    for i, (s, items) in enumerate(sorted(by_sym.items())):
        if s == BTC:
            why["btc_hedge_leg"] += len(items)
            continue
        d = fc.load_sym(data_dir, s)
        if d is None:
            why["no_bars"] += len(items)
            continue
        for day, pr in items:
            st = d.starts.get(day)
            age = day - st if st is not None else -1
            if age < MIN_AGE:
                why["young_lt_90"] += 1
                continue
            sn = snap(cache, s, day - 1)
            if sn is None or sn.get("missing"):
                why["snap_404" if sn is None else "snap_not_fetched"] += 1
                continue
            if not sn.get("snap"):
                why["snap_no_row"] += 1
                continue
            m7 = median_qv(d.bars, day)
            L = leverage_ratio(sn["snap"]["oi_value"], m7)
            v30 = realized_vol(d.bars, day)
            if L is None or v30 is None or v30 <= 0:
                why["no_L_or_vol"] += 1
                continue
            lg = leg_week(s, d.bars, d.fslice(day * DAY_MS - 2 * DAY_MS, day * DAY_MS + 9 * DAY_MS), day)
            if lg is None:
                why["no_leg"] += 1
                continue
            c1 = d.bars.close.get(day - 1)
            oi = sn["snap"]["oi"]
            pxr = (sn["snap"]["oi_value"] / oi) / c1 if c1 and oi > 0 else None
            out[day].append(
                [
                    s,
                    L,
                    m7,
                    pr[PX["mqv"]],
                    v30,
                    pr[PX["beta"]],
                    pr[PX["fund"]],
                    lg.r,
                    lg.funding_long,
                    lg.truncated,
                    age,
                    lg.up,
                    pxr,
                    pr[PX["r_lag1"]],
                ]
            )
            why["ok"] += 1
        if i % 100 == 0:
            print(f"  {i}/{len(by_sym)} symbols ({time.time() - t0:.0f}s; {rss()})", flush=True)
    res = {
        "weeks": [{"day": w["day"], "r_btc": r_btc[w["day"]], "rows": out.get(w["day"], [])} for w in weeks],
        "why": dict(why),
        "cols": COLS,
    }
    fc.wr(cache / "table.json", res)
    print(f"BUILD DONE {sum(len(v) for v in out.values())} symbol-weeks; {dict(why)}; {time.time() - t0:.0f}s; {rss()}", flush=True)


# ---------------------------------------------------------------- shared


def load_table(cache: Path) -> list[dict[str, Any]]:
    t = fc.rd(cache / "table.json")
    return [w for w in t["weeks"] if len(w["rows"]) >= MIN_N and w["r_btc"][0] is not None]


def col(rows: list[list[Any]], c: str) -> list[Any]:
    return [r[IX[c]] for r in rows]


def rres(w: dict[str, Any], lag: bool = False) -> list[float]:
    rb = w["r_btc"][1] if lag else w["r_btc"][0]
    k = "r_lag1" if lag else "r"
    return [beta_residual(r[IX[k]], r[IX["beta"]], rb) for r in w["rows"]]


def signal(w: dict[str, Any], kind: str) -> np.ndarray:
    rows = w["rows"]
    logl = np.log(np.array(col(rows, "L"), dtype=float))
    if kind == "raw":
        return logl
    ctrl = [np.log(np.array(col(rows, "mqv"), dtype=float)), np.log(np.array(col(rows, "vol30"), dtype=float))]
    if kind == "neu_beta":
        ctrl.append(np.array(col(rows, "beta"), dtype=float))
    return neutralized_signal(logl, ctrl)


def summ(xs: list[float], boot: bool = True) -> dict[str, float]:
    n = len(xs)
    m = statistics.fmean(xs)
    se = newey_west_se(xs, NW_LAG)
    lo, hi = block_bootstrap_ci(xs, seed=SEED) if boot else (m - 1.96 * se, m + 1.96 * se)
    return {
        "n": n,
        "mean": m,
        "se": se,
        "t": m / se if se > 0 else float("nan"),
        "p": two_sided_p(m / se) if se > 0 else float("nan"),
        "lo": lo,
        "hi": hi,
    }


def fmt(label: str, s: dict[str, float], scale: float = 1.0, unit: str = "") -> str:
    return (
        f"  {label:<52} n={s['n']:>4}  mean {s['mean'] * scale:+.4f}{unit}  CI [{s['lo'] * scale:+.4f}; {s['hi'] * scale:+.4f}]"
        f"  t_NW {s['t']:+.2f}  p {s['p']:.3f}"
    )


# ---------------------------------------------------------------- stage 0


def stage0(cache: Path) -> None:
    t = fc.rd(cache / "table.json")
    print(f"build exclusions / inclusions (symbol-weeks): {t['why']}")
    weeks = load_table(cache)
    allw = t["weeks"]
    print(f"weeks in panel {len(allw)}; weeks with N >= {MIN_N}: {len(weeks)} ({ymd(weeks[0]['day'])} .. {ymd(weeks[-1]['day'])})")
    ns = [len(w["rows"]) for w in weeks]
    print(
        f"N per week: min {min(ns)}, median {statistics.median(ns)}, max {max(ns)}; symbol-weeks {sum(ns)}; symbols {len({r[0] for w in weeks for r in w['rows']})}"
    )
    allrows = [r for w in weeks for r in w["rows"]]
    L = [r[IX["L"]] for r in allrows]
    q = np.percentile(L, [1, 10, 25, 50, 75, 90, 99])
    print("L (days of turnover) percentiles 1/10/25/50/75/90/99: " + " ".join(f"{x:.3f}" for x in q))
    pxr = [r[IX["px_ratio"]] for r in allrows if r[IX["px_ratio"]] is not None]
    pq = np.percentile(pxr, [0.5, 5, 50, 95, 99.5])
    bad = sum(abs(x - 1) > 0.05 for x in pxr)
    print(
        f"units: (oi_value / oi) / close(T-1) percentiles 0.5/5/50/95/99.5: {' '.join(f'{x:.4f}' for x in pq)}; |dev| > 5 %: {bad} of {len(pxr)}"
    )
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        p = cache / "snap" / s
        if p.exists():
            fs = sorted(p.glob("*.json"))
            for f in (fs[0], fs[len(fs) // 2], fs[-1]):
                v = fc.rd(f)
                if v and v.get("snap"):
                    print(
                        f"  {s} {f.stem}: rows {v['n_rows']}, OI value {v['snap']['oi_value'] / 1e9:.2f} bn USD, obs {datetime.fromtimestamp(v['snap']['obs_ms'] / 1000, UTC):%H:%M}"
                    )
    eth = [(w["day"], r) for w in weeks for r in w["rows"] if r[0] == "ETHUSDT"]
    if eth:
        print(f"  ETHUSDT L median {statistics.median(r[IX['L']] for _, r in eth):.3f} over {len(eth)} weeks")
    print(
        f"  truncated legs (delisted inside the week): {sum(bool(r[IX['trunc']]) for r in allrows)}; |r| > 100 %: {sum(abs(r[IX['r']]) > 1 for r in allrows)}"
    )
    # is L just 'small / illiquid / volatile'? cross-sectional Spearman of log L with controls, R^2 of the neutralising OLS
    c_mqv, c_vol, c_m7, r2 = [], [], [], []
    for w in weeks:
        rows = w["rows"]
        logl = np.log(col(rows, "L"))
        c_mqv.append(rank_ic(logl, np.log(col(rows, "mqv"))))
        c_vol.append(rank_ic(logl, np.log(col(rows, "vol30"))))
        c_m7.append(rank_ic(logl, np.log(col(rows, "med7"))))
        res = signal(w, "neu")
        r2.append(1 - float(np.var(res)) / float(np.var(logl)))
    print(
        f"control check: mean weekly Spearman(log L, log mqv30) {statistics.fmean(c_mqv):+.3f}, (log L, log vol30) {statistics.fmean(c_vol):+.3f}, (log L, log med7) {statistics.fmean(c_m7):+.3f}; R^2 of log L on [log mqv30, log vol30]: median {statistics.median(r2):.3f}"
    )
    # H2 split feasibility (signal-side only)
    pos0 = pos_b = 0
    for w in weeks:
        _, top = quintiles(dict(zip(col(w["rows"], "sym"), signal(w, "neu").tolist(), strict=True)))
        f = statistics.median([r[IX["fund"]] for r in w["rows"] if r[0] in set(top)])
        pos0 += f > 0
        pos_b += f > BASE_FUND * 1.0001
    print(f"H2 split: weeks with median trailing funding of the top-L quintile > 0: {pos0} of {len(weeks)}; > base 10.95 %/yr: {pos_b}")
    gate = {}
    for kind in ("neu", "raw"):
        ics = [rank_ic(signal(w, kind), rres(w)) for w in weeks]
        sd = statistics.stdev(ics)
        se = newey_west_se(ics, NW_LAG)
        gate[kind] = {"n": len(ics), "sd": sd, "se_iid": sd / math.sqrt(len(ics)), "se_nw": se, "mde": 2.8 * se}
        print(
            f"power [{kind}]: weeks {len(ics)}, SD weekly IC {sd:.4f}, SE iid {sd / math.sqrt(len(ics)):.4f}, SE_NW {se:.4f}, MDE (2.8 SE_NW) {2.8 * se:.4f} -> gate {'PASS' if 2.8 * se <= GATE else 'FAIL (stop: inconclusive)'}"
        )
    oos = [w for w in weeks if w["day"] >= OOS_DAY]
    ics = [rank_ic(signal(w, "neu"), rres(w)) for w in oos]
    if len(ics) > 2:
        print(f"H3 OOS weeks {len(ics)}: SD {statistics.stdev(ics):.4f}, MDE {2.8 * newey_west_se(ics, NW_LAG):.4f}")
    fc.wr(cache / "stage0.json", gate)
    print(rss())


# ---------------------------------------------------------------- full


def spread_series(weeks: list[dict[str, Any]], kind: str, *, cost_mode: str = "card", slip_mult: float = 1.0) -> dict[str, list[float]]:
    """Weekly long-bottom / short-top quintile spread on beta-neutral returns (each side on its own notional, so the
    spread is P&L per 1 USD long + 1 USD short), funding and costs; also the short-only hedged and unhedged top leg."""
    out: dict[str, list[float]] = defaultdict(list)
    prev_b: list[str] = []
    prev_t: list[str] = []
    prev_h = 0.0
    for w in weeks:
        rows = w["rows"]
        sig = dict(zip(col(rows, "sym"), signal(w, kind).tolist(), strict=True))
        bot, top = quintiles(sig)
        by = {r[0]: r for r in rows}
        rr = dict(zip(col(rows, "sym"), rres(w), strict=True))
        rb, _, fb = w["r_btc"]

        def m(names: list[str], f: Any, by: dict[str, list[Any]] = by) -> float:
            return statistics.fmean(f(by[s]) for s in names)

        def ucost(names: list[str], prev: list[str], by: dict[str, list[Any]] = by) -> float:
            if cost_mode == "card":  # 0.1 % per round trip = 0.05 % per unit traded
                return turnover(prev, names) * 0.0005
            ws = {s: 1 / len(names) for s in names}
            wp = {s: 1 / len(prev) for s in prev} if prev else {}
            return sum(
                abs(ws.get(s, 0) - wp.get(s, 0)) * unit_cost(by[s][IX["mqv"]] if s in by else None, slip_mult=slip_mult)
                for s in set(ws) | set(wp)
            )

        g = statistics.fmean(rr[s] for s in bot) - statistics.fmean(rr[s] for s in top)
        fund = m(bot, lambda r: r[IX["fund_long"]]) - m(top, lambda r: r[IX["fund_long"]])
        hedge = m(bot, lambda r: r[IX["beta"]]) - m(top, lambda r: r[IX["beta"]])  # BTC short of this size
        fund -= hedge * fb
        hc = 0.0005 if cost_mode == "card" else unit_cost(1e9, slip_mult=slip_mult)
        cost = ucost(bot, prev_b) + ucost(top, prev_t) + abs(hedge - prev_h) * hc
        out["gross"].append(g)
        out["fund"].append(fund)
        out["cost"].append(cost)
        out["net"].append(g + fund - cost)
        bt = m(top, lambda r: r[IX["beta"]])
        st_cost = (
            ucost(top, prev_t)
            + abs(bt - (statistics.fmean(by[s][IX["beta"]] for s in prev_t if s in by) if prev_t and any(s in by for s in prev_t) else 0.0))
            * hc
        )
        short_h = -statistics.fmean(rr[s] for s in top) - m(top, lambda r: r[IX["fund_long"]]) + bt * fb - st_cost
        short_u = -m(top, lambda r: r[IX["r"]]) - m(top, lambda r: r[IX["fund_long"]]) - ucost(top, prev_t)
        out["short_hedged_net"].append(short_h)
        out["short_unhedged_net"].append(short_u)
        out["turn_top"].append(turnover(prev_t, top))
        out["liq_top_3x"].append(sum(by[s][IX["up"]] >= 1 / 3 - 0.025 for s in top) / len(top))
        out["n_side"].append(len(top))
        out["btc"].append(rb)
        prev_b, prev_t, prev_h = bot, top, hedge
    return out


def stage_full(cache: Path, n_perm: int) -> None:
    weeks = load_table(cache)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    days = [w["day"] for w in weeks]
    say(f"weeks {len(weeks)} ({ymd(days[0])} .. {ymd(days[-1])}); symbol-weeks {sum(len(w['rows']) for w in weeks)}")
    ic = {k: [rank_ic(signal(w, k), rres(w)) for w in weeks] for k in ("neu", "raw", "neu_beta")}
    say("\nH1 primary: weekly Spearman IC of neutralised L vs next-week beta-neutral return (block bootstrap CI, block 4)")
    s1 = summ(ic["neu"])
    say(fmt("IC neutralised L (PRIMARY)", s1))
    say(fmt("IC raw log L (secondary)", summ(ic["raw"])))
    say(fmt("IC neutralised incl. beta30 (descriptive)", summ(ic["neu_beta"])))
    say(f"  share of weeks with IC < 0 (neutralised): {sum(x < 0 for x in ic['neu']) / len(ic['neu']):.1%}")
    perm = placebo_mean_ics([(signal(w, "neu").tolist(), rres(w)) for w in weeks], n_perm, seed=SEED)
    p_lo = (1 + int(np.sum(perm <= s1["mean"]))) / (1 + n_perm)
    say(
        f"  placebo (L shuffled within week, {n_perm}x): perm mean IC 2.5/50/97.5 % {np.percentile(perm, 2.5):+.4f} / {np.percentile(perm, 50):+.4f} / {np.percentile(perm, 97.5):+.4f}; one-sided p(perm <= obs) {p_lo:.3f}"
    )

    say("\nH3 OOS (2025-07-07 .. ) and IS split")
    is_m = [d < OOS_DAY for d in days]
    for lab, mask in (("IS 2022-01 .. 2025-06", is_m), ("OOS 2025-07 .. 2026-09 (H3)", [not x for x in is_m])):
        for k in ("neu", "raw"):
            say(fmt(f"{lab} [{k}]", summ([x for x, f in zip(ic[k], mask, strict=True) if f])))
    say("  by calendar year [neu]:")
    for y in sorted({ymd(d)[:4] for d in days}):
        xs = [x for x, d in zip(ic["neu"], days, strict=True) if ymd(d)[:4] == y]
        say(f"    {y}: n {len(xs):>3}, mean IC {statistics.fmean(xs):+.4f}, SE {statistics.stdev(xs) / math.sqrt(len(xs)):.4f}")

    say("\nH2: IC in weeks where the median trailing funding of the top-L quintile is positive vs the rest")
    flags0, flagsb = [], []
    for w in weeks:
        _, top = quintiles(dict(zip(col(w["rows"], "sym"), signal(w, "neu").tolist(), strict=True)))
        f = statistics.median([r[IX["fund"]] for r in w["rows"] if r[0] in set(top)])
        flags0.append(f > 0)
        flagsb.append(f > BASE_FUND * 1.0001)
    for lab, fl in (("funding > 0", flags0), ("funding > base 10.95 %/yr", flagsb)):
        d, se, na, nb = split_diff(ic["neu"], fl)
        say(
            f"  {lab}: diff IC(flag) - IC(rest) {d:+.4f} (Welch SE {se:.4f}, 95 % CI [{d - 1.96 * se:+.4f}; {d + 1.96 * se:+.4f}]), weeks {na} / {nb}"
        )
    # symbol-level variant: IC inside positive-premium names only (descriptive)
    ic_hi = []
    for w in weeks:
        rows = [r for r in w["rows"] if r[IX["fund"]] > BASE_FUND * 1.0001]
        if len(rows) >= 20:
            sub = {"rows": rows, "r_btc": w["r_btc"]}
            ic_hi.append(rank_ic(signal(sub, "neu"), rres(sub)))
    if len(ic_hi) > 10:
        say(fmt("descriptive: IC within names with funding > base (N>=20)", summ(ic_hi)))

    say("\nRobustness of the primary IC")
    say(fmt("without the 10 most negative weeks", summ(without_top(ic["neu"], 10, -1))))
    say(fmt("without the 10 most positive weeks", summ(without_top(ic["neu"], 10, +1))))
    xb = []
    for w in weeks:
        keep = [i for i, r in enumerate(w["rows"]) if abs(r[IX["r"]]) <= 1.0]
        sub = {"rows": [w["rows"][i] for i in keep], "r_btc": w["r_btc"]}
        xb.append(rank_ic(signal(sub, "neu"), rres(sub)))
    say(fmt("excluding symbol-weeks with |r| > 100 %", summ(xb)))
    lag = []
    for w in weeks:
        if w["r_btc"][1] is None:
            continue
        rows = [r for r in w["rows"] if r[IX["r_lag1"]] is not None]
        sub = {"rows": rows, "r_btc": w["r_btc"]}
        lag.append(rank_ic(signal(sub, "neu"), rres(sub, lag=True)))
    say(fmt("entry one day later (open Tue -> close Mon)", summ(lag)))
    raw_ret = [rank_ic(signal(w, "neu"), col(w["rows"], "r")) for w in weeks]
    say(fmt("unhedged return (no beta neutralisation)", summ(raw_ret)))
    part = [
        partial_rank_ic(np.log(col(w["rows"], "L")), rres(w), [np.log(col(w["rows"], "mqv")), np.log(col(w["rows"], "vol30"))])
        for w in weeks
    ]
    say(fmt("rank-partial IC (controls on both sides)", summ(part)))
    fund_ic = [rank_ic(col(w["rows"], "fund"), rres(w)) for w in weeks]
    say(fmt("reference: IC of trailing funding alone", summ(fund_ic)))

    say("\nQuintile spread (confirmatory; underpowered per triage). Per 1 USD long + 1 USD short, per week, % points")
    for kind in ("neu", "raw"):
        for mode, sm in (("card", 1.0), ("tier", 1.0), ("tier", 2.0)):
            sp = spread_series(weeks, kind, cost_mode=mode, slip_mult=sm)
            lab = f"[{kind}] costs {mode}{' x2 slip' if sm > 1 else ''}"
            if mode == "card" or kind == "neu":
                say(fmt(f"{lab}: gross", summ(sp["gross"]), 100, " pp"))
                say(
                    f"      funding mean {statistics.fmean(sp['fund']) * 100:+.4f} pp, cost mean {statistics.fmean(sp['cost']) * 100:.4f} pp, top-leg turnover {statistics.fmean(sp['turn_top']):.2f}/wk, names per side median {statistics.median(sp['n_side'])}"
                )
                say(fmt(f"{lab}: net", summ(sp["net"]), 100, " pp"))
                if kind == "neu":
                    say(fmt(f"{lab}: short top quintile, BTC-beta hedged, net", summ(sp["short_hedged_net"]), 100, " pp"))
                    say(fmt(f"{lab}: short top quintile, unhedged, net", summ(sp["short_unhedged_net"]), 100, " pp"))
                    if mode == "card":
                        say(
                            f"      top-quintile names with a weekly up-move >= 30.8 % (isolated 3x short liquidated): {statistics.fmean(sp['liq_top_3x']):.2%} per week"
                        )
        if kind == "neu":
            sp = spread_series(weeks, "neu")
            for lab, mask in (("IS", is_m), ("OOS", [not x for x in is_m])):
                say(fmt(f"[neu] card costs net, {lab}", summ([x for x, f in zip(sp["net"], mask, strict=True) if f]), 100, " pp"))

    say("\nPer-asset check (5a): per-symbol contributions z(rank L_neu) * z(rank r), sign flipped (positive = supports H1)")
    vals, assets, clus = [], [], []
    for w in weeks:
        pr = ic_products(signal(w, "neu").tolist(), rres(w))
        for r, v in zip(w["rows"], pr, strict=True):
            vals.append(-float(v))
            assets.append(r[0])
            clus.append(w["day"])
    st = sorted(asset_stats(vals, assets, min_n=30), key=lambda s: s.mean)
    say(
        f"  symbols with >= 30 weeks: {len(st)}; per-asset mean contribution: median {statistics.median(s.mean for s in st):+.4f}; share > 0 {sum(s.mean > 0 for s in st) / len(st):.1%}"
    )
    for s in st[:5] + st[-5:]:
        say(f"    {s.asset:<14} n {s.n:>4} mean {s.mean:+.4f}")
    h = heterogeneity(vals, assets, min_n=30)
    say(f"  Cochran Q {h['q']:.1f} (df {h.get('df', len(st) - 1)}), p {h['p']:.3f}, I^2 {h['i2']:.3f}")
    pq = permutation_q(vals, assets, clus, min_n=30, n_perm=1000, seed=SEED)
    say(f"  permutation Q (labels shuffled within weeks, 1000x): p {pq['p']:.3f}")
    ps = persistence(vals, assets, clus, [d < OOS_DAY for d in clus], min_n_is=30, top_n=10, n_boot=2000, seed=SEED)
    say(
        f"  persistence: top-10 IS symbols ({', '.join(ps['top'])}) OOS mean {ps['top_oos_mean']:+.4f} vs rest {ps['rest_oos_mean']:+.4f}; "
        f"diff {ps['diff']:+.4f} CI [{ps['diff_ci'][0]:+.4f}; {ps['diff_ci'][1]:+.4f}]; rho IS/OOS {ps['rho']:+.3f} (n {ps['rho_n']})"
    )
    (cache / "full.txt").write_text("\n".join(lines))
    fc.wr(cache / "full.json", {"ic": ic, "days": days, "perm": perm.tolist()})
    say(f"\n{rss()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=("fetch", "build", "stage0", "full"))
    ap.add_argument("--cache-dir", default=str(CACHE))
    ap.add_argument("--panel", default=str(PANEL))
    ap.add_argument("--data-dir", default=str(DATA))
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--perm", type=int, default=1000)
    ap.add_argument("--log", default=None)
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.log:
        f = open(Path(a.log).expanduser(), "a", buffering=1)  # noqa: SIM115
        sys.stdout = f
    if a.stage == "fetch":
        asyncio.run(stage_fetch(Path(a.panel).expanduser(), cache, a.sample))
    elif a.stage == "build":
        stage_build(Path(a.panel).expanduser(), Path(a.data_dir).expanduser(), cache)
    elif a.stage == "stage0":
        stage0(cache)
    else:
        stage_full(cache, a.perm)


if __name__ == "__main__":
    main()
