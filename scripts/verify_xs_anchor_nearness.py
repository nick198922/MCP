"""Anchoring factor of USDT-M perps: weekly partial rank IC of NEAR365 (nearness to the trailing 365-day high).

Idea card: docs/ideas/IDEA-20261025-xs-anchor-nearness-high-short.md (sections "Тріаж критика", "Попередня реєстрація").

Data (read-only, no network, no keys, no orders):
    --data-dir   daily klines + listing months of scripts/verify_funding_crowding.py (~/.cache/tradedesk-agents/funding_crowding)
    --panel-dir  the Monday panel of scripts/verify_xs_factor_ic_panel.py (panel.json: universe, controls, weekly legs)
Outputs go to --cache-dir (default ~/.cache/tradedesk-agents/xs_anchor_nearness); --out also writes the printed output
to a text file.

Stages:
    features  NEAR365 / NEAR180 / NEAR90 / ATH / M90 / AGE / BETA90 for every panel row + the BTC weekly leg (features.json)
    stage0    data checks and the power gate of the registered H1 series (no means)
    full      registered test (H1, low-minus-middle spread, H2, IS/OOS, robustness, placebo, per asset, baseline)

    uv run python scripts/verify_xs_anchor_nearness.py <stage> [--out FILE]
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

from tradedesk.analytics.funding_crowding import leg_week, mde_nw, newey_west_se  # noqa: E402
from tradedesk.analytics.per_asset import heterogeneity, permutation_q, persistence  # noqa: E402
from tradedesk.analytics.xs_anchor import FEATURES, bottom_set, features_at, hedged_returns, low_minus_middle  # noqa: E402
from tradedesk.analytics.xs_factors import partial_rank_ic, rank_ic, rank_z, residualize, rolling_means, two_sided_p, without_top  # noqa: E402
from tradedesk.analytics.xs_volume import partial_ic_products, partial_residuals, placebo_corr_mean, shift_placebo_mean_ics  # noqa: E402

NW_LAG = 4
GATE = 0.03
MIN_IC = 0.02
MIN_N = 50
MIN_OOS = 50
SEED = 7
BTC = "BTCUSDT"
SPLIT = int(datetime(2025, 3, 3, tzinfo=UTC).timestamp()) // 86400  # first OOS Monday
PANEL_COLS = ("sym", "R7", "M28", "MAX30", "IVOL30", "beta", "fund", "mqv", "r", "r_lag1", "fund_long", "trunc")
COLS = (*PANEL_COLS, *FEATURES)
IX = {c: i for i, c in enumerate(COLS)}
S = "NEAR365"
H1_CTRL = ("beta",)
H2_CTRL = ("beta", "IVOL30", "MAX30", "M28", "mqv", "AGE", "M90")
H2_CARD = ("beta", "IVOL30", "MAX30", "M28", "mqv")  # the card's original H2 set (before the critic's AGE, M90)


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


def month_day(mo: str) -> int:
    y, m = map(int, mo.split("-"))
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) // 86400


# ---------------------------------------------------------------- features


def first_listing_day(data_dir: Path, sym: str, bars_first: int | None) -> int | None:
    """First day of the first 1d archive month (the archive lists all months, also before the cache's 2021-10 start)."""
    p = data_dir / "listing" / f"{sym}.json"
    ms = fc.rd(p) if p.exists() else None
    cands = [x for x in ([month_day(min(ms))] if ms else []) + ([bars_first] if bars_first is not None else [])]
    return min(cands) if cands else None


def stage_features(data_dir: Path, panel_dir: Path, out: Path) -> None:
    t0 = time.time()
    panel = fc.rd(panel_dir / "panel.json")
    if tuple(panel["cols"]) != PANEL_COLS:
        raise SystemExit(f"unexpected panel layout {panel['cols']}")
    data = fc.load_all(data_dir)
    print(f"loaded {len(data)} symbols in {time.time() - t0:.0f}s; {rss()}", flush=True)
    btc = data[BTC].bars
    first = {s: first_listing_day(data_dir, s, min(d.bars.close) if d.bars.close else None) for s, d in data.items()}
    weeks = []
    miss = dict.fromkeys(FEATURES, 0)
    n = 0
    for i, w in enumerate(panel["weeks"]):
        f: dict[str, list[float | None]] = {}
        for r in w["rows"]:
            fa = features_at(data[r[0]].bars, btc, w["day"], first.get(r[0]))
            f[r[0]] = [fa[k] for k in FEATURES]
            n += 1
            for k in FEATURES:
                miss[k] += fa[k] is None
        lg = leg_week(BTC, btc, data[BTC].fslice((w["day"] - 2) * 86_400_000, (w["day"] + 9) * 86_400_000), w["day"])
        weeks.append({"day": w["day"], "f": f, "r_btc": lg.r if lg else None, "f_btc": lg.funding_long if lg else None})
        if (i + 1) % 50 == 0:
            print(f"  week {i + 1} {ymd(w['day'])} ({time.time() - t0:.0f}s; {rss()})", flush=True)
    fc.wr(out / "features.json", {"weeks": weeks, "cols": FEATURES})
    print(f"features: {len(weeks)} weeks, {n} symbol-weeks, missing {miss}; {time.time() - t0:.0f}s; {rss()}", flush=True)
    fc.sanity(data)


def load(panel_dir: Path, out: Path) -> list[dict[str, Any]]:
    panel = fc.rd(panel_dir / "panel.json")["weeks"]
    feats = {w["day"]: w for w in fc.rd(out / "features.json")["weeks"]}
    weeks = []
    for w in panel:
        fw = feats[w["day"]]
        rows = [r + fw["f"][r[0]] for r in w["rows"]]
        rows = [r for r in rows if r[IX[S]] is not None]
        if len(rows) < MIN_N or fw["r_btc"] is None:
            continue
        weeks.append({"day": w["day"], "rows": rows, "r_btc": fw["r_btc"], "f_btc": fw["f_btc"]})
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


def fmt(label: str, xs: list[float], scale: float = 1.0, unit: str = "IC") -> str:
    m, se, t, p = stat(xs)
    m, se = m * scale, se * scale
    return f"    {label:<56} n={len(xs):>3} {unit} {m:+.4f} CI95 [{m - 1.96 * se:+.4f}; {m + 1.96 * se:+.4f}] t {t:+.2f} p {p:.4f}"


def sub(weeks: list[dict[str, Any]], keep: Callable[[list[Any]], bool]) -> list[dict[str, Any]]:
    return [{**w, "rows": [r for r in w["rows"] if keep(r)]} for w in weeks]


def top_by_volume(weeks: list[dict[str, Any]], k: int = 100) -> list[dict[str, Any]]:
    return [{**w, "rows": sorted(w["rows"], key=lambda r: -r[IX["mqv"]])[:k]} for w in weeks]


def hedged(w: dict[str, Any], rows: list[list[Any]]) -> list[float]:
    """Weekly return hedged with BETA90 x the BTC leg (BETA90 missing -> beta30)."""
    betas = [r[IX["BETA90"]] if r[IX["BETA90"]] is not None else r[IX["beta"]] for r in rows]
    return hedged_returns(col(rows, "r"), betas, w["r_btc"])


def spread_series(weeks: list[dict[str, Any]], s: str = S, *, sign: float = 1.0, resid_on: tuple[str, ...] = ()) -> list[float]:
    """Q1 - Q3 of the hedged return by ``sign * s`` (or by its residual on ``resid_on`` controls), per week."""
    out = []
    for w in weeks:
        rr = valid(w["rows"], s, "r", resid_on)
        x = np.asarray(col(rr, s), dtype=float) * sign
        if resid_on:
            x = residualize(rank_z(x.tolist()), [rank_z(tr(c, col(rr, c))) for c in resid_on])
        out.append(low_minus_middle(x.tolist(), hedged(w, rr)))
    return out


# ---------------------------------------------------------------- stage 0


def stage0(panel_dir: Path, out: Path) -> None:
    weeks = load(panel_dir, out)
    raw = fc.rd(panel_dir / "panel.json")["weeks"]
    rows = [r for w in weeks for r in w["rows"]]
    n_is = sum(w["day"] < SPLIT for w in weeks)
    n_oos = len(weeks) - n_is
    print(
        f"panel Mondays {len(raw)}; weeks with >= {MIN_N} rows with NEAR365: {len(weeks)} ({ymd(weeks[0]['day'])}..{ymd(weeks[-1]['day'])})"
    )
    print(f"symbol-weeks with NEAR365: {len(rows)} of {sum(len(w['rows']) for w in raw)} panel rows (all weeks)")
    ns = [len(w["rows"]) for w in weeks]
    print(f"N per week: min {min(ns)} median {statistics.median(ns)} max {max(ns)}")
    print(f"IS weeks (< {ymd(SPLIT)}): {n_is}; OOS: {n_oos} ({'binding' if n_oos >= MIN_OOS else 'descriptive only'}, threshold {MIN_OOS})")
    for k in FEATURES:
        v = sorted(x for x in col(rows, k) if x is not None)
        print(
            f"  {k:<7} defined {len(v)}/{len(rows)} p1 {v[len(v) // 100]:+.4g} p50 {v[len(v) // 2]:+.4g} p99 {v[99 * len(v) // 100]:+.4g}"
        )
    nmin = min(len(valid(w["rows"], S, "r", H2_CTRL)) for w in weeks)
    print(f"  min symbols per week with NEAR365 and all H2 controls: {nmin}")
    print("  mean weekly Spearman of NEAR365 with:", end="")
    for c in ("M28", "M90", "ATH", "NEAR180", "IVOL30", "MAX30", "beta", "mqv", "AGE"):
        cs = [rank_ic(col(rr, S), col(rr, c)) for w in weeks if len(rr := valid(w["rows"], c, "r", (S,))) > 10]
        print(f" {c} {statistics.fmean(cs):+.3f}", end="")
    print()
    btc = [(w["day"], r[IX[S]]) for w in weeks for r in w["rows"] if r[0] == BTC]
    print(
        f"  BTCUSDT in the H1 universe {len(btc)} weeks; NEAR365 on 2024-12-30: {[round(x, 4) for d, x in btc if ymd(d) == '2024-12-30']}"
    )
    print(f"  r_btc weekly: min {min(w['r_btc'] for w in weeks):+.3f} max {max(w['r_btc'] for w in weeks):+.3f}")
    print()
    print(f"STAGE 0 (no means): MDE = 2.8 x SE_NW (NW lag {NW_LAG}); gate MDE <= {GATE} for the registered H1 series only")
    res: dict[str, Any] = {}
    for lab, ctrl in (("H1 beta30", H1_CTRL), ("H2 full controls", H2_CTRL)):
        ics = series(weeks, S, ctrl=ctrl)
        se = newey_west_se(ics, NW_LAG)
        gate = 2.8 * se <= GATE
        res[lab] = {"n": len(ics), "sd": statistics.stdev(ics), "se": se, "mde": mde_nw(ics, NW_LAG), "gate": gate}
        tag = ("PASS" if gate else "FAIL -> inconclusive") if lab.startswith("H1") else "(descriptive)"
        print(f"  {lab:<18} n={len(ics)} SD={statistics.stdev(ics):.4f} SE_NW={se:.4f} MDE={2.8 * se:.4f} {tag}")
        for part, keep in (("IS", lambda d: d < SPLIT), ("OOS", lambda d: d >= SPLIT)):
            xs = [x for x, w in zip(ics, weeks, strict=True) if keep(w["day"])]
            print(f"      {part}: n={len(xs)} SE_NW={newey_west_se(xs, NW_LAG):.4f} MDE={2.8 * newey_west_se(xs, NW_LAG):.4f}")
    d = spread_series(weeks)
    se = newey_west_se(d, NW_LAG)
    print(
        f"  spread Q1-Q3 hedged (cond. 1): n={len(d)} SD={statistics.stdev(d) * 100:.3f} pp SE_NW={se * 100:.3f} pp MDE={2.8 * se * 100:.3f} pp/wk"
    )
    res["spread"] = {"n": len(d), "se": se}
    fc.wr(out / "stage0.json", res)
    print(f"done; {rss()}")


# ---------------------------------------------------------------- full test


def per_asset(weeks: list[dict[str, Any]], ctrl: tuple[str, ...], sg: float) -> None:
    vals: list[float] = []
    assets: list[str] = []
    clus: list[int] = []
    for w in weeks:
        rr = valid(w["rows"], S, "r", ctrl)
        pr = partial_ic_products(col(rr, S), col(rr, "r"), [tr(c, col(rr, c)) for c in ctrl])
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


def baseline(weeks: list[dict[str, Any]]) -> None:
    print()
    print("=== BASELINE (card item 5; descriptive): Q1 - Q3 hedged spread of the short side of other signals, same rows ===")
    print(fmt("NEAR365 lowest quintile", spread_series(weeks), 100, "pp/wk"))
    print(fmt("M28 lowest quintile", spread_series(weeks, "M28"), 100, "pp/wk"))
    print(fmt("M90 lowest quintile", spread_series(weeks, "M90"), 100, "pp/wk"))
    print(fmt("IVOL30 highest quintile", spread_series(weeks, "IVOL30", sign=-1.0), 100, "pp/wk"))
    print(fmt("NEAR365 residual on H2 controls, lowest quintile", spread_series(weeks, resid_on=H2_CTRL), 100, "pp/wk"))
    jm, ji, ex = [], [], []
    for w in weeks:
        rr = valid(w["rows"], S, "r", ("M90",))
        bn = bottom_set(col(rr, S))
        bm = bottom_set(col(rr, "M28"))
        bi = bottom_set([-x for x in col(rr, "IVOL30")])
        jm.append(len(bn & bm) / len(bn | bm))
        ji.append(len(bn & bi) / len(bn | bi))
        h = hedged(w, rr)
        only = [h[i] for i in bn - bm - bi]
        mid = np.argsort(rank_z(col(rr, S)), kind="stable").tolist()
        k = len(rr) // 5
        m0 = (len(rr) - k) // 2
        if only:
            ex.append(statistics.fmean(only) - statistics.fmean(h[i] for i in mid[m0 : m0 + k]))
    print(
        f"    overlap (Jaccard) of the NEAR365 bottom quintile with M28 bottom {statistics.fmean(jm):.2f}, with IVOL30 top {statistics.fmean(ji):.2f}"
    )
    print(fmt("NEAR365 bottom quintile minus names in either baseline, vs Q3", ex, 100, "pp/wk"))


def stage_full(data_dir: Path, panel_dir: Path, out: Path, n_perm: int, n_shift: int) -> None:
    s0 = fc.rd(out / "stage0.json")
    weeks = load(panel_dir, out)
    info = fc.rd(data_dir / "exchange_info.json")["info"]
    days = [w["day"] for w in weeks]
    is_mask = [d < SPLIT for d in days]
    n_oos = len(weeks) - sum(is_mask)
    print(f"weeks {len(weeks)}; IS {sum(is_mask)} ({ymd(days[0])}..{ymd(SPLIT - 7)}); OOS {n_oos} ({ymd(SPLIT)}..{ymd(days[-1])})")
    if not s0["H1 beta30"]["gate"]:
        print(f"stage-0 gate failed (MDE {s0['H1 beta30']['mde']:.4f}) -> inconclusive; means not computed")
        return
    print()
    print("=== CONSISTENCY with 2026-10-18-xs-factor-ic-panel (rows restricted to NEAR365 defined, so not identical) ===")
    print(fmt("MAX30 raw IC (full panel report: -0.0911)", series(weeks, "MAX30")))
    print(fmt("M28 raw IC (full panel report: see report)", series(weeks, "M28")))

    h1 = series(weeks, S, ctrl=H1_CTRL)
    h2 = series(weeks, S, ctrl=H2_CTRL)
    sp = spread_series(weeks)
    m1, se1, _, _ = stat(h1)
    m2, se2, _, _ = stat(h2)
    ms, ses, _, _ = stat(sp)
    split = lambda xs, k: [x for x, m in zip(xs, is_mask, strict=True) if m == k]  # noqa: E731
    print()
    print("=== MAIN (registered): NEAR365, mean weekly partial rank IC, NW lag 4, one test, no Holm; expected sign + ===")
    print(fmt("H1: partial on beta30", h1))
    print(fmt("H1 IS", split(h1, True)))
    print(fmt("H1 OOS (from 2025-03-03)", split(h1, False)))
    print(fmt("cond. 1: Q1 - Q3 hedged (BETA90 x BTC), gross", sp, 100, "pp/wk"))
    print(fmt("cond. 1 IS", split(sp, True), 100, "pp/wk"))
    print(fmt("cond. 1 OOS", split(sp, False), 100, "pp/wk"))
    print(fmt("H2: partial on beta30+IVOL30+MAX30+M28+ln mqv+AGE+M90", h2))
    print(fmt("H2 IS", split(h2, True)))
    print(fmt("H2 OOS", split(h2, False)))
    print(fmt("H2 card set (no AGE, M90; descriptive)", series(weeks, S, ctrl=H2_CARD)))
    for c in ("AGE", "M90", "M28", "IVOL30"):
        print(fmt(f"H1 + {c} only (descriptive decomposition)", series(weeks, S, ctrl=("beta", c))))

    print()
    print("=== ROBUSTNESS (registered list; sign +) ===")
    print(fmt("raw IC (no controls)", series(weeks, S)))
    for y in sorted({ymd(d)[:4] for d in days}):
        print(fmt(f"H1 year {y}", [x for x, d in zip(h1, days, strict=True) if ymd(d)[:4] == y]))
    for lab, xs in (("H1", h1), ("H2", h2)):
        roll = rolling_means(xs, 52)
        print(f"    {lab} rolling 52w: min {min(roll):+.4f} max {max(roll):+.4f} share > 0 {sum(r > 0 for r in roll) / len(roll):.0%}")
    print(fmt("H1 without the 10 best weeks (sign +)", without_top(h1, 10, 1.0)))
    print(fmt("H2 without the 10 best weeks (sign +)", without_top(h2, 10, 1.0)))
    nobig = sub(weeks, lambda r: abs(r[IX["r"]]) <= 1.0)
    print(fmt("H1 without symbol-weeks with |r| > 100 %", series(nobig, S, ctrl=H1_CTRL)))
    print(fmt("H2 without symbol-weeks with |r| > 100 %", series(nobig, S, ctrl=H2_CTRL)))
    print(fmt("cond. 1 without symbol-weeks with |r| > 100 %", spread_series(nobig), 100, "pp/wk"))
    print(fmt("H1 entry lag 1 day", series(weeks, S, ret="r_lag1", ctrl=H1_CTRL)))
    alive = sub(weeks, lambda r: (info.get(r[0]) or {}).get("status") == "TRADING")
    print(fmt("H1 TRADING-only (survivorship, descriptive)", series(alive, S, ctrl=H1_CTRL)))
    t100 = top_by_volume(weeks)
    print(fmt("H1 top-100 by turnover", series(t100, S, ctrl=H1_CTRL)))
    print(fmt("H2 top-100 by turnover", series(t100, S, ctrl=H2_CTRL)))
    print(fmt("cond. 1 top-100 by turnover", spread_series(t100), 100, "pp/wk"))

    t0 = time.time()
    for lab, ctrl, mo in (("H1", H1_CTRL, m1), ("H2", H2_CTRL, m2)):
        pairs = []
        for w in weeks:
            rr = valid(w["rows"], S, "r", ctrl)
            pairs.append(partial_residuals(col(rr, S), col(rr, "r"), [tr(c, col(rr, c)) for c in ctrl]))
        perm = placebo_corr_mean(pairs, n_perm, seed=SEED)
        p = (1 + int(np.sum(np.abs(perm) >= abs(mo)))) / (1 + n_perm)
        print(f"    placebo {lab} ({n_perm} within-week shuffles of residual ranks): SD {perm.std():.4f}, |obs| {abs(mo):.4f}, p {p:.4f}")
    sig = [{r[0]: r[IX[S]] for r in w["rows"]} for w in weeks]
    ret = [{r[0]: r[IX["r"]] for r in w["rows"]} for w in weeks]
    rng = np.random.default_rng(SEED)
    shifts = [int(k) for k in rng.integers(8, len(weeks) - 8, size=n_shift)]
    sh = shift_placebo_mean_ics(sig, ret, shifts)
    raw = statistics.fmean(series(weeks, S))
    p_sh = (1 + sum(abs(x) >= abs(raw) for x in sh)) / (1 + len(sh))
    print(
        f"    placebo shift ({n_shift} circular shifts k in [8; n-8], raw IC): mean {statistics.fmean(sh):+.4f} SD {statistics.pstdev(sh):.4f} "
        f"range [{min(sh):+.4f}; {max(sh):+.4f}], |obs raw| {abs(raw):.4f}, p {p_sh:.4f} ({time.time() - t0:.0f}s)"
    )
    per_asset(weeks, H1_CTRL, 1.0 if m1 > 0 else -1.0)

    print()
    print("=== DESCRIPTIVE shape (cond. 5: no selection, no verdict) ===")
    for g in ("NEAR180", "NEAR90", "ATH"):
        print(fmt(f"{g}: H1-type partial on beta30", series(weeks, g, ctrl=H1_CTRL)))
        print(fmt(f"{g}: H2-type partial on full controls", series(weeks, g, ctrl=H2_CTRL)))
    baseline(weeks)

    oos_binding = n_oos >= MIN_OOS
    ci1, ci2, cis = m1 - 1.96 * se1 > 0, m2 - 1.96 * se2 > 0, ms + 1.96 * ses < 0
    is_ok = statistics.fmean(split(h1, True)) > 0
    oos_ok = statistics.fmean(split(h1, False)) > 0
    stable = is_ok and (oos_ok or not oos_binding)
    h2_ok = m2 >= MIN_IC and ci2
    if m1 < 0 and m1 + 1.96 * se1 < 0:
        verdict = "NOT CONFIRMED (opposite sign, CI excl. 0)"
    elif ci1 and m1 >= GATE and stable and h2_ok and cis:
        verdict = "CONFIRMED"
    elif not ci1 or m1 < MIN_IC or not stable or m2 < MIN_IC:
        verdict = "NOT CONFIRMED"
    else:
        verdict = "DIRECTIONALLY RIGHT"
    print()
    print(
        f"=== REGISTERED VERDICT NEAR365: H1 {m1:+.4f} CI>0 {ci1}, >= {GATE} {m1 >= GATE}, >= {MIN_IC} {m1 >= MIN_IC}; IS>0 {is_ok} OOS>0 {oos_ok} "
        f"(binding {oos_binding}); H2 {m2:+.4f} >= {MIN_IC} & CI>0 {h2_ok}; spread {ms * 100:+.3f} pp CI<0 {cis} -> {verdict}"
    )
    if verdict == "CONFIRMED":
        print("    H3 eligible: run the short-leg simulation (not implemented in this runner until H1/H2 pass)")
    else:
        print("    H3 basket not built (registered: only after a passed H1 and H2)")
    print(f"done; {rss()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["features", "stage0", "full"])
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--panel-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/xs_factor_ic_panel")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/xs_anchor_nearness")
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
        else:
            stage_full(a.data_dir, a.panel_dir, a.cache_dir, a.perm, a.shifts)
    finally:
        if fh is not None:
            sys.stdout = sys.__stdout__
            fh.close()


if __name__ == "__main__":
    main()
