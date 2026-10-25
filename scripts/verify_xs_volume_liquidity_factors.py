"""Volume and liquidity factors of USDT-M perps: weekly rank IC of AMI30 (registered), VS and DV30 (descriptive).

Idea card: docs/ideas/IDEA-20261025-xs-volume-liquidity-factors.md (sections "Тріаж критика", "Попередня реєстрація").

Data (read-only, no network, no keys, no orders):
    --data-dir   daily klines of scripts/verify_funding_crowding.py (~/.cache/tradedesk-agents/funding_crowding)
    --panel-dir  the Monday panel of scripts/verify_xs_factor_ic_panel.py (panel.json: universe, controls, weekly legs)
Outputs go to --cache-dir (default ~/.cache/tradedesk-agents/xs_volume_liquidity_factors); --out also writes the
printed output to a text file.

Stages:
    features  compute AMI30 / VS / DV30 (+ AMI14 / AMI60) for every panel row from the daily bars (features.json)
    stage0    data checks and the power gate of the registered H1 series (no means)
    full      registered test of AMI30 (H1, H2, IS/OOS, robustness, placebo, per asset); VS, DV30 descriptive

    uv run python scripts/verify_xs_volume_liquidity_factors.py <stage> [--out FILE]
"""

from __future__ import annotations

import argparse
import math
import resource
import statistics
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_funding_crowding as fc  # noqa: E402  (loader and JSON helpers; scripts/ is not a package)

from tradedesk.analytics.funding_crowding import (  # noqa: E402
    Candidate,
    LegWeek,
    Pick,
    Week,
    block_bootstrap_ci,
    mde_nw,
    newey_west_se,
    pick_baskets,
    simulate,
)
from tradedesk.analytics.per_asset import heterogeneity, permutation_q, persistence  # noqa: E402
from tradedesk.analytics.xs_factors import partial_rank_ic, rank_ic, rolling_means, two_sided_p, without_top  # noqa: E402
from tradedesk.analytics.xs_volume import (  # noqa: E402
    features_at,
    partial_ic_products,
    partial_residuals,
    placebo_corr_mean,
    quantile_spread,
    shift_placebo_mean_ics,
)

NW_LAG = 4
GATE = 0.03
MIN_IC = 0.02
MIN_N = 50
SEED = 7
SPLIT = int(datetime(2025, 3, 3, tzinfo=UTC).timestamp()) // 86400  # first OOS Monday
PANEL_COLS = ("sym", "R7", "M28", "MAX30", "IVOL30", "beta", "fund", "mqv", "r", "r_lag1", "fund_long", "trunc")
FEAT = ("AMI30", "VS", "DV30", "AMI14", "AMI60")
COLS = PANEL_COLS + FEAT
IX = {c: i for i, c in enumerate(COLS)}
H1_CTRL = ("beta",)
H2_CTRL = ("beta", "IVOL30", "MAX30")


class Tee:
    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, s: str) -> int:
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self) -> None:
        for st in self.streams:
            st.flush()


def rss() -> str:
    return f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- features


def stage_features(data_dir: Path, panel_dir: Path, out: Path) -> None:
    t0 = time.time()
    panel = fc.rd(panel_dir / "panel.json")
    if tuple(panel["cols"]) != PANEL_COLS:
        raise SystemExit(f"unexpected panel layout {panel['cols']}")
    data = fc.load_all(data_dir)
    print(f"loaded {len(data)} symbols in {time.time() - t0:.0f}s; {rss()}", flush=True)
    weeks = []
    miss = dict.fromkeys(FEAT, 0)
    n = 0
    for i, w in enumerate(panel["weeks"]):
        f: dict[str, list[float | None]] = {}
        for r in w["rows"]:
            fa = features_at(data[r[0]].bars, w["day"])
            f[r[0]] = [fa[k] for k in FEAT]
            n += 1
            for k in FEAT:
                miss[k] += fa[k] is None
        weeks.append({"day": w["day"], "f": f})
        if (i + 1) % 50 == 0:
            print(f"  week {i + 1} {ymd(w['day'])} ({time.time() - t0:.0f}s; {rss()})", flush=True)
    # units check: the panel's 30-day median turnover vs exp(DV30) (mean) for BTC
    btc = [(w["day"], r[IX["mqv"]]) for w in panel["weeks"] for r in w["rows"] if r[0] == "BTCUSDT"]
    d0, mq = btc[len(btc) // 2]
    dv = next(x["f"]["BTCUSDT"][2] for x in weeks if x["day"] == d0)
    print(f"units: BTCUSDT {ymd(d0)} median qv 30d {mq / 1e9:.2f} bn USD, mean qv 30d exp(DV30) {math.exp(dv) / 1e9:.2f} bn USD")
    raw_short = 0
    for s in list(data)[:: max(1, len(data) // 25)]:
        mos = fc.months_of(data_dir, s)
        if mos:
            k = fc.rd(fc.kpath(data_dir, s, mos[len(mos) // 2])) or []
            raw_short += sum(len(row) < 7 for row in k)
    print(f"kline rows without the quote-volume field in a 25-symbol sample: {raw_short}")
    fc.wr(out / "features.json", {"weeks": weeks, "cols": FEAT})
    print(f"features: {len(weeks)} weeks, {n} symbol-weeks, missing {miss}; {time.time() - t0:.0f}s; {rss()}", flush=True)


def load(panel_dir: Path, out: Path) -> list[dict[str, Any]]:
    panel = fc.rd(panel_dir / "panel.json")["weeks"]
    feats = {w["day"]: w["f"] for w in fc.rd(out / "features.json")["weeks"]}
    weeks = []
    for w in panel:
        if len(w["rows"]) < MIN_N:
            continue
        f = feats[w["day"]]
        weeks.append({"day": w["day"], "rows": [r + f[r[0]] for r in w["rows"]]})
    return weeks


# ---------------------------------------------------------------- statistics


def col(rows: list[list[Any]], name: str) -> list[Any]:
    return [r[IX[name]] for r in rows]


def tr(c: str, v: list[float]) -> list[float]:
    return [math.log(x) for x in v] if c == "mqv" else v


def valid(rows: list[list[Any]], s: str, ret: str, ctrl: tuple[str, ...]) -> list[list[Any]]:
    return [r for r in rows if r[IX[s]] is not None and r[IX[ret]] is not None and all(r[IX[c]] is not None for c in ctrl)]


def ic_week(rows: list[list[Any]], s: str, *, ret: str = "r", ctrl: tuple[str, ...] = ()) -> float:
    rr = valid(rows, s, ret, ctrl)
    x, y = col(rr, s), col(rr, ret)
    if not ctrl:
        return rank_ic(x, y)
    return partial_rank_ic(x, y, [tr(c, col(rr, c)) for c in ctrl])


def series(weeks: list[dict[str, Any]], s: str, **kw: Any) -> list[float]:
    return [ic_week(w["rows"], s, **kw) for w in weeks]


def stat(xs: list[float]) -> tuple[float, float, float, float]:
    m = statistics.fmean(xs)
    se = newey_west_se(xs, NW_LAG)
    return m, se, m / se if se > 0 else float("nan"), two_sided_p(m / se) if se > 0 else 1.0


def fmt(label: str, xs: list[float]) -> str:
    m, se, t, p = stat(xs)
    return f"    {label:<50} n={len(xs):>3} IC {m:+.4f} CI95 [{m - 1.96 * se:+.4f}; {m + 1.96 * se:+.4f}] t {t:+.2f} p {p:.4f}"


def sub(weeks: list[dict[str, Any]], keep: Callable[[list[Any]], bool]) -> list[dict[str, Any]]:
    return [{"day": w["day"], "rows": [r for r in w["rows"] if keep(r)]} for w in weeks]


def top_by_volume(weeks: list[dict[str, Any]], k: int = 100) -> list[dict[str, Any]]:
    return [{"day": w["day"], "rows": sorted(w["rows"], key=lambda r: -r[IX["mqv"]])[:k]} for w in weeks]


# ---------------------------------------------------------------- stage 0


def stage0(panel_dir: Path, out: Path) -> None:
    weeks = load(panel_dir, out)
    rows = [r for w in weeks for r in w["rows"]]
    print(f"weeks with N >= {MIN_N}: {len(weeks)} ({ymd(weeks[0]['day'])}..{ymd(weeks[-1]['day'])}); symbol-weeks {len(rows)}")
    print(f"IS weeks (< {ymd(SPLIT)}): {sum(w['day'] < SPLIT for w in weeks)}; OOS: {sum(w['day'] >= SPLIT for w in weeks)}")
    for s in FEAT:
        v = sorted(x for x in col(rows, s) if x is not None)
        print(
            f"  {s:<6} defined {len(v)}/{len(rows)} p1 {v[len(v) // 100]:+.4g} p50 {v[len(v) // 2]:+.4g} p99 {v[99 * len(v) // 100]:+.4g}"
        )
    nmin = min(len(valid(w["rows"], "AMI30", "r", H2_CTRL)) for w in weeks)
    print(f"  min symbols per week with AMI30 and all H2 controls: {nmin}")
    print("  mean weekly Spearman of AMI30 with:", end="")
    for c in ("IVOL30", "MAX30", "mqv", "DV30", "beta", "VS"):
        cs = [
            rank_ic(col(rr, "AMI30"), col(rr, c))
            for w in weeks
            if len(rr := [r for r in w["rows"] if r[IX["AMI30"]] is not None and r[IX[c]] is not None]) > 10
        ]
        print(f" {c} {statistics.fmean(cs):+.3f}", end="")
    print()
    print()
    print(f"STAGE 0 (no means): MDE = 2.8 x SE_NW (NW lag {NW_LAG}); gate MDE <= {GATE} for the registered H1 series only")
    res: dict[str, Any] = {}
    for s in ("AMI30", "VS", "DV30"):
        for lab, ctrl in (("H1 beta", H1_CTRL), ("H2 beta+IVOL30+MAX30", H2_CTRL)):
            ics = series(weeks, s, ctrl=ctrl)
            se = newey_west_se(ics, NW_LAG)
            gate = 2.8 * se <= GATE
            res[f"{s}|{lab}"] = {"n": len(ics), "sd": statistics.stdev(ics), "se": se, "mde": mde_nw(ics, NW_LAG), "gate": gate}
            tag = ("PASS" if gate else "FAIL -> inconclusive") if (s == "AMI30" and lab.startswith("H1")) else "(descriptive)"
            print(f"  {s:<6} {lab:<22} n={len(ics)} SD={statistics.stdev(ics):.4f} SE_NW={se:.4f} MDE={2.8 * se:.4f} {tag}")
    fc.wr(out / "stage0.json", res)
    print(f"done; {rss()}")


# ---------------------------------------------------------------- full test


def per_asset(weeks: list[dict[str, Any]], s: str, ctrl: tuple[str, ...], sg: float) -> None:
    vals: list[float] = []
    assets: list[str] = []
    clus: list[int] = []
    for w in weeks:
        rr = valid(w["rows"], s, "r", ctrl)
        pr = partial_ic_products(col(rr, s), col(rr, "r"), [tr(c, col(rr, c)) for c in ctrl])
        vals.extend(pr.tolist())
        assets.extend(col(rr, "sym"))
        clus.extend([w["day"]] * len(rr))
    het = heterogeneity(vals, assets, min_n=20)
    pq = permutation_q(vals, assets, clus, min_n=20, n_perm=500, seed=SEED)
    ism = [c < SPLIT for c in clus]
    print(
        f"    per asset (contributions to the H1 partial IC): k={het['k']} (>= 20 weeks) Q={het['q']:.1f} df={het['df']} "
        f"p={het['p']:.4f} I2={het['i2']:.2f}; within-week label permutation p={pq['p']:.4f} (Q null mean {pq['null_mean']:.1f})"
    )
    per = {}
    for lab, kw in (("top 10", {"top_n": 10}), ("top quarter", {"top_frac": 0.25})):
        per[lab] = r = persistence(vals, assets, clus, ism, min_n_is=20, n_boot=2000, seed=SEED, **kw)
        print(
            f"    persistence {lab} (IS rank by mean contribution): IS top {r['top_is_mean']:+.4f} rest {r['rest_is_mean']:+.4f} -> "
            f"OOS top {r['top_oos_mean']:+.4f} (n {r['n_top_oos']}) rest {r['rest_oos_mean']:+.4f}; diff {r['diff']:+.4f} "
            f"CI [{r['diff_ci'][0]:+.4f}; {r['diff_ci'][1]:+.4f}] p {r['diff_p']:.3f}; rho IS-OOS {r['rho']:+.3f} (k {r['rho_n']})"
        )
    top = sorted(per["top 10"]["is_ranked"], key=lambda x: -x[2] * sg)[:5]
    print(f"    largest IS contributors (direction of the effect): {[(a, n, round(mu, 3)) for a, n, mu in top]}")


def basket(weeks: list[dict[str, Any]], s: str, sg: float) -> None:
    """H3: 20/20 on the top 100 by turnover; long the side the H1 sign favours. Runs only after a passed H1/H2."""
    bw = []
    for w in top_by_volume(weeks):
        rr = [r for r in w["rows"] if r[IX[s]] is not None]
        uni = {r[0]: Candidate(r[0], -sg * r[IX[s]], r[IX["mqv"]], 0) for r in rr}
        legs = {r[0]: LegWeek(r[0], r[IX["r"]], r[IX["fund_long"]], 0.0, 0.0, 0, bool(r[IX["trunc"]]), 0, 0) for r in rr}
        bw.append(Week(w["day"], uni, legs))

    def pick(wk: Week) -> Pick:
        lo, hi = pick_baskets({k: c.signal for k, c in wk.universe.items()}, 20)
        return Pick(lo, hi)

    for lab, kw in (("base (fee 0.05 %, tiered slippage)", {}), ("stress (fee 0.055 %, slippage x2)", {"fee": 0.00055, "slip_mult": 2.0})):
        res = [x for x in simulate(bw, pick, **kw) if x.n_side > 0]
        net = [x.net for x in res]
        lo, hi = block_bootstrap_ci(net, n_boot=5000, seed=SEED)
        print(
            f"    BASKET 20/20 top-100 {lab}: n={len(net)} net {statistics.fmean(net) * 100:+.3f}%/wk ({statistics.fmean(net) * 5200:+.1f}%/yr "
            f"on gross notional) CI95 [{lo * 100:+.3f}; {hi * 100:+.3f}] | price {statistics.fmean(x.price for x in res) * 100:+.3f} "
            f"funding {statistics.fmean(x.funding for x in res) * 100:+.3f} cost {statistics.fmean(x.cost for x in res) * 100:.3f}; worst week {min(net) * 100:+.1f}%"
        )


def stage_full(data_dir: Path, panel_dir: Path, out: Path, n_perm: int, n_shift: int) -> None:
    s0 = fc.rd(out / "stage0.json")
    weeks = load(panel_dir, out)
    info = fc.rd(data_dir / "exchange_info.json")["info"]
    days = [w["day"] for w in weeks]
    is_mask = [d < SPLIT for d in days]
    print(
        f"weeks {len(weeks)}; IS {sum(is_mask)} ({ymd(days[0])}..{ymd(SPLIT - 7)}); OOS {len(weeks) - sum(is_mask)} ({ymd(SPLIT)}..{ymd(days[-1])})"
    )
    print()
    print("=== CONSISTENCY with 2026-10-18-xs-factor-ic-panel (same rows) ===")
    print(fmt("MAX30 raw IC (report: -0.0911)", series(weeks, "MAX30")))
    print(fmt("trailing funding raw IC (report: -0.0009)", series(weeks, "fund")))

    print()
    print("=== DESCRIPTIVE: VS and DV30 (no verdict) ===")
    for s in ("VS", "DV30"):
        print(f"  {s}")
        print(fmt("raw IC", series(weeks, s)))
        print(fmt("H1-type: partial on beta30", series(weeks, s, ctrl=H1_CTRL)))
        print(fmt("H2-type: partial on beta30 + IVOL30 + MAX30", series(weeks, s, ctrl=H2_CTRL)))
        print(fmt("top-100 by turnover, partial on beta30", series(top_by_volume(weeks), s, ctrl=H1_CTRL)))

    if not s0["AMI30|H1 beta"]["gate"]:
        print()
        print(f"AMI30: stage-0 gate failed (MDE {s0['AMI30|H1 beta']['mde']:.4f}) -> inconclusive; mean not computed")
        return

    s = "AMI30"
    h1 = series(weeks, s, ctrl=H1_CTRL)
    h2 = series(weeks, s, ctrl=H2_CTRL)
    m1, se1, _, _ = stat(h1)
    m2, se2, _, _ = stat(h2)
    sg = 1.0 if m1 > 0 else -1.0
    print()
    print("=== MAIN (registered): AMI30, mean weekly partial rank IC, NW lag 4, no Holm ===")
    print(fmt("H1: partial on beta30", h1))
    print(fmt("H2: partial on beta30 + IVOL30 + MAX30", h2))
    iso1 = [x for x, k in zip(h1, is_mask, strict=True) if k]
    oos1 = [x for x, k in zip(h1, is_mask, strict=True) if not k]
    iso2 = [x for x, k in zip(h2, is_mask, strict=True) if k]
    oos2 = [x for x, k in zip(h2, is_mask, strict=True) if not k]
    print(fmt("H1 IS", iso1))
    print(fmt("H1 OOS (from 2025-03-03)", oos1))
    print(fmt("H2 IS", iso2))
    print(fmt("H2 OOS", oos2))

    print()
    print(f"=== ROBUSTNESS AMI30 (registered; sign of the full-sample H1 {sg:+.0f}) ===")
    print(fmt("raw IC (no controls)", series(weeks, s)))
    print(fmt("partial on funding 7d + log turnover (old panel's neutral)", series(weeks, s, ctrl=("fund", "mqv"))))
    print(fmt("partial on beta + IVOL30 + MAX30 + log turnover", series(weeks, s, ctrl=(*H2_CTRL, "mqv"))))
    for y in sorted({ymd(d)[:4] for d in days}):
        print(fmt(f"H1 year {y}", [x for x, d in zip(h1, days, strict=True) if ymd(d)[:4] == y]))
        print(fmt(f"H2 year {y}", [x for x, d in zip(h2, days, strict=True) if ymd(d)[:4] == y]))
    for lab, xs in (("H1", h1), ("H2", h2)):
        roll = rolling_means(xs, 52)
        print(
            f"    {lab} rolling 52w: min {min(roll):+.4f} max {max(roll):+.4f} share with the H1 sign {sum(r * sg > 0 for r in roll) / len(roll):.0%}"
        )
    print(fmt("H1 without the 10 best weeks (effect's direction)", without_top(h1, 10, sg)))
    print(fmt("H2 without the 10 best weeks (H1 direction)", without_top(h2, 10, sg)))
    nobig = sub(weeks, lambda r: abs(r[IX["r"]]) <= 1.0)
    print(fmt("H1 without symbol-weeks with |r| > 100 %", series(nobig, s, ctrl=H1_CTRL)))
    print(fmt("H2 without symbol-weeks with |r| > 100 %", series(nobig, s, ctrl=H2_CTRL)))
    print(fmt("H1 entry lag 1 day", series(weeks, s, ret="r_lag1", ctrl=H1_CTRL)))
    print(fmt("H2 entry lag 1 day", series(weeks, s, ret="r_lag1", ctrl=H2_CTRL)))
    alive = sub(weeks, lambda r: (info.get(r[0]) or {}).get("status") == "TRADING")
    print(fmt("H1 TRADING-only (survivorship, descriptive)", series(alive, s, ctrl=H1_CTRL)))
    t100 = top_by_volume(weeks)
    print(fmt("H1 top-100 by turnover", series(t100, s, ctrl=H1_CTRL)))
    print(fmt("H2 top-100 by turnover", series(t100, s, ctrl=H2_CTRL)))
    for g in ("AMI14", "AMI60"):
        print(fmt(f"grid {g}: H1", series(weeks, g, ctrl=H1_CTRL)))
        print(fmt(f"grid {g}: H2", series(weeks, g, ctrl=H2_CTRL)))
    qs = []
    for w in weeks:
        rr = valid(w["rows"], s, "r", ())
        qs.append(quantile_spread(col(rr, s), col(rr, "r")))
    m, se, t, _ = stat(qs)
    print(f"    quintile spread Q5-Q1 of raw AMI30 (gross, equal weight, price only): {m * 100:+.3f}%/wk SE_NW {se * 100:.3f} t {t:+.2f}")

    t0 = time.time()
    for lab, ctrl, mo in (("H1", H1_CTRL, m1), ("H2", H2_CTRL, m2)):
        pairs = []
        for w in weeks:
            rr = valid(w["rows"], s, "r", ctrl)
            pairs.append(partial_residuals(col(rr, s), col(rr, "r"), [tr(c, col(rr, c)) for c in ctrl]))
        perm = placebo_corr_mean(pairs, n_perm, seed=SEED)
        p = (1 + int(np.sum(np.abs(perm) >= abs(mo)))) / (1 + n_perm)
        print(f"    placebo {lab} ({n_perm} within-week shuffles of residual ranks): SD {perm.std():.4f}, |obs| {abs(mo):.4f}, p {p:.4f}")
    sig = [{r[0]: r[IX[s]] for r in w["rows"] if r[IX[s]] is not None} for w in weeks]
    ret = [{r[0]: r[IX["r"]] for r in w["rows"]} for w in weeks]
    rng = np.random.default_rng(SEED)
    shifts = [int(k) for k in rng.integers(8, len(weeks) - 8, size=n_shift)]
    sh = shift_placebo_mean_ics(sig, ret, shifts)
    raw = statistics.fmean(series(weeks, s))
    p_sh = (1 + sum(abs(x) >= abs(raw) for x in sh)) / (1 + len(sh))
    print(
        f"    placebo shift ({n_shift} circular shifts k in [8; n-8], raw IC): mean {statistics.fmean(sh):+.4f} SD {statistics.pstdev(sh):.4f} "
        f"range [{min(sh):+.4f}; {max(sh):+.4f}], |obs raw| {abs(raw):.4f}, p {p_sh:.4f} ({time.time() - t0:.0f}s)"
    )
    per_asset(weeks, s, H1_CTRL, sg)

    same = lambda xs: statistics.fmean(xs) * sg > 0  # noqa: E731
    ci1 = abs(m1) > 1.96 * se1
    ci2 = abs(m2) > 1.96 * se2
    stable = same(iso1) and same(oos1)
    h2_ok = m2 * sg > 0 and abs(m2) >= MIN_IC and ci2
    if ci1 and abs(m1) >= GATE and stable and h2_ok:
        verdict = "CONFIRMED"
    elif not ci1 or abs(m1) < MIN_IC or not stable or not h2_ok:
        verdict = "NOT CONFIRMED"
    else:
        verdict = "DIRECTIONALLY RIGHT"
    print()
    print(
        f"=== REGISTERED VERDICT AMI30: H1 IC {m1:+.4f} CI excl. 0 {ci1}, |IC| >= {GATE} {abs(m1) >= GATE}, >= {MIN_IC} {abs(m1) >= MIN_IC}; "
        f"IS/OOS same sign {stable}; H2 IC {m2:+.4f} same sign & >= {MIN_IC} & CI excl. 0 {h2_ok} -> {verdict}"
    )
    if verdict == "CONFIRMED":
        basket(weeks, s, sg)
    else:
        print("    H3 basket not built (registered: only after a passed H1 and H2)")
    print(f"done; {rss()}")


def stage_extra(panel_dir: Path, out: Path) -> None:
    """Exploratory, added after the full run (labelled in the report; no verdict depends on it): (1) is the positive
    raw quintile mean spread of AMI30 a tail effect (rank IC ~ 0)? (2) why is the shift placebo far from zero?"""
    weeks = load(panel_dir, out)
    s = "AMI30"
    print("=== EXTRA (exploratory): quintile spread Q5-Q1 of raw AMI30, gross, equal weight, price only, %/wk ===")
    variants: dict[str, Callable[[list[float]], list[float]]] = {
        "mean": lambda r: r,
        "mean, |r| capped at 100 %": lambda r: [max(min(x, 1.0), -1.0) for x in r],
        "mean of log(1 + r)": lambda r: [math.log(max(1.0 + x, 1e-6)) for x in r],
    }
    for lab, f in variants.items():
        for part, keep in (("all", lambda d: True), ("IS", lambda d: d < SPLIT), ("OOS", lambda d: d >= SPLIT)):
            qs = []
            for w in weeks:
                if not keep(w["day"]):
                    continue
                rr = valid(w["rows"], s, "r", ())
                qs.append(quantile_spread(col(rr, s), f(col(rr, "r"))))
            m, se, t, _ = stat(qs)
            print(f"    {lab:<28} {part:<4} n={len(qs):>3} {m * 100:+.3f} SE_NW {se * 100:.3f} t {t:+.2f}")
    med = []
    for w in weeks:
        rr = sorted(valid(w["rows"], s, "r", ()), key=lambda r: r[IX[s]])
        k = len(rr) // 5
        med.append(statistics.median(col(rr[-k:], "r")) - statistics.median(col(rr[:k], "r")))
    m, se, t, _ = stat(med)
    print(f"    median(Q5) - median(Q1)           n={len(med):>3} {m * 100:+.3f} SE_NW {se * 100:.3f} t {t:+.2f}")
    print()
    print("=== EXTRA (exploratory): non-circular shift of the AMI30 signal against weekly returns (raw rank IC) ===")
    sig = [{r[0]: r[IX[s]] for r in w["rows"]} for w in weeks]
    ret = [{r[0]: r[IX["r"]] for r in w["rows"]} for w in weeks]
    for k in (1, 4, 8, 26, 52, 104):
        fut = shift_placebo_mean_ics(sig[k:], ret[:-k], [0])[0]
        past = shift_placebo_mean_ics(sig[:-k], ret[k:], [0])[0]
        print(f"    k={k:>3}: signal from week w+k (future) {fut:+.4f}; signal from week w-k (past, a longer-lag predictor) {past:+.4f}")
    print(f"done; {rss()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["features", "stage0", "full", "extra"])
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--panel-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/xs_factor_ic_panel")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/xs_volume_liquidity_factors")
    ap.add_argument("--out", type=Path, default=None, help="also write the printed output to this text file")
    ap.add_argument("--perm", type=int, default=1000)
    ap.add_argument("--shifts", type=int, default=200)
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    fh = None
    if a.out is not None:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        fh = a.out.open("w", encoding="utf-8")
        sys.stdout = Tee(sys.__stdout__, fh)  # type: ignore[assignment]
    try:
        if a.stage == "features":
            stage_features(a.data_dir, a.panel_dir, a.cache_dir)
        elif a.stage == "stage0":
            stage0(a.panel_dir, a.cache_dir)
        elif a.stage == "full":
            stage_full(a.data_dir, a.panel_dir, a.cache_dir, a.perm, a.shifts)
        else:
            stage_extra(a.panel_dir, a.cache_dir)
    finally:
        if fh is not None:
            sys.stdout = sys.__stdout__
            fh.close()


if __name__ == "__main__":
    main()
