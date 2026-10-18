"""Cross-sectional factor IC panel: weekly rank IC of R7, M28 (skip 7), MAX30 and IVOL30 on USDT-M perps.

Idea card: docs/ideas/IDEA-20261018-xs-factor-ic-panel.md (sections "Тріаж критика", "Попередня реєстрація").

Data: the existing per-file cache of ``scripts/verify_funding_crowding.py`` (data.binance.vision daily klines and
funding, 678 eligible symbols incl. SETTLING / removed ones), read-only via ``--data-dir``. No network, no keys, no orders.
Outputs (panel.json, stage0.json) go to ``--cache-dir`` (default ~/.cache/tradedesk-agents/xs_factor_ic_panel).

Stages:
    panel    build the point-in-time Monday panel from the cache (universe, signals, controls, weekly legs)
    stage0   data checks and the per-signal power gate (n, SD, SE_NW, MDE, MDE after Holm) -- no means
    full     the registered test for signals that passed the gate; robustness, placebo, per-asset, basket (descriptive)

    uv run python scripts/verify_xs_factor_ic_panel.py <stage>
"""

from __future__ import annotations

import argparse
import math
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

import verify_funding_crowding as fc  # noqa: E402  (loader, metadata, sanity checks; scripts/ is not a package)

from tradedesk.analytics.funding_crowding import (  # noqa: E402
    Candidate,
    LegWeek,
    Week,
    block_bootstrap_ci,
    leg_week,
    mde_nw,
    newey_west_se,
    rank_pick,
    simulate,
    week_candidate,
)
from tradedesk.analytics.funding_spread import normalize_asset  # noqa: E402
from tradedesk.analytics.listing_drift import DAY_MS, holm  # noqa: E402
from tradedesk.analytics.per_asset import heterogeneity, permutation_q, persistence  # noqa: E402
from tradedesk.analytics.xs_factors import (  # noqa: E402
    SIGNALS,
    holm_z,
    ic_products,
    oriented_signal,
    partial_rank_ic,
    placebo_mean_ics,
    rank_ic,
    rolling_means,
    signals_at,
    two_sided_p,
    without_top,
)

FIRST = int(datetime(2022, 1, 3, tzinfo=UTC).timestamp()) // 86400  # Monday
LAST = int(datetime(2026, 9, 21, tzinfo=UTC).timestamp()) // 86400  # Monday; week ends 2026-09-27
NW_LAG = 4
GATE = 0.03
N_TESTS = 4
SEED = 7
MIN_N = 50
BTC = "BTCUSDT"
# panel row layout
COLS = ("sym", "R7", "M28", "MAX30", "IVOL30", "beta", "fund", "mqv", "r", "r_lag1", "fund_long", "trunc")
IX = {c: i for i, c in enumerate(COLS)}


def rss() -> str:
    return f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- panel


def stage_panel(data_dir: Path, out: Path) -> None:
    t0 = time.time()
    data = fc.load_all(data_dir)
    print(f"loaded {len(data)} symbols in {time.time() - t0:.0f}s; {rss()}", flush=True)
    btc = data[BTC].bars
    weeks = []
    reasons: Counter[str] = Counter()
    for day in range(FIRST, LAST + 1, 7):
        t_ms = day * DAY_MS
        cands: dict[str, Candidate] = {}
        for s, d in data.items():
            if day not in d.bars.open and (not d.bars.close or min(d.bars.close) > day or max(d.bars.close) < day - 30):
                continue
            c, why = week_candidate(s, d.bars, d.fslice(t_ms - 10 * DAY_MS, t_ms), day, starts=d.starts)
            reasons[why] += 1
            if c is not None:
                cands[s] = c
        best: dict[str, Candidate] = {}
        for s, c in cands.items():
            a = normalize_asset(s[:-4], "binanceusdm")[0]
            if a not in best or c.median_qv > best[a].median_qv:
                best[a] = c
        reasons["dup_asset"] += len(cands) - len(best)
        rows = []
        for c in best.values():
            d = data[c.symbol]
            sg = signals_at(d.bars, btc, day)
            if sg is None:
                reasons["no_signal"] += 1
                continue
            lg = leg_week(c.symbol, d.bars, d.fslice(t_ms - 2 * DAY_MS, t_ms + 9 * DAY_MS), day)
            if lg is None:
                reasons["no_leg"] += 1
                continue
            l1 = leg_week(c.symbol, d.bars, [], day + 1)
            rows.append(
                [
                    c.symbol,
                    sg.r7,
                    sg.m28,
                    sg.max30,
                    sg.ivol30,
                    sg.beta30,
                    c.signal,
                    c.median_qv,
                    lg.r,
                    l1.r if l1 else None,
                    lg.funding_long,
                    lg.truncated,
                ]
            )
        weeks.append({"day": day, "rows": rows})
        if len(weeks) % 50 == 0:
            print(f"  week {len(weeks)} {ymd(day)} N={len(rows)} ({time.time() - t0:.0f}s; {rss()})", flush=True)
    fc.wr(out / "panel.json", {"weeks": weeks, "reasons": dict(reasons), "cols": COLS})
    print(f"panel: {len(weeks)} weeks in {time.time() - t0:.0f}s; reasons {dict(reasons)}; {rss()}", flush=True)
    fc.sanity(data)


def load_panel(out: Path) -> list[dict[str, Any]]:
    p = out / "panel.json"
    if not p.exists():
        raise SystemExit("run the panel stage first")
    return [w for w in fc.rd(p)["weeks"] if len(w["rows"]) >= MIN_N]


def col(rows: list[list[Any]], name: str) -> list[Any]:
    return [r[IX[name]] for r in rows]


def ic_week(rows: list[list[Any]], s: str, *, ret: str = "r", ctrl: tuple[str, ...] = ()) -> float:
    rr = [r for r in rows if r[IX[ret]] is not None]
    x, y = col(rr, s), col(rr, ret)
    if not ctrl:
        return rank_ic(x, y)
    cs = [[math.log(v) if c == "mqv" else v for v in col(rr, c)] for c in ctrl]
    return partial_rank_ic(x, y, cs)


NEUTRAL = ("fund", "mqv")
NEUTRAL_B = ("fund", "mqv", "beta")


def series(weeks: list[dict[str, Any]], s: str, **kw: Any) -> list[float]:
    return [ic_week(w["rows"], s, **kw) for w in weeks]


# ---------------------------------------------------------------- stage 0


def stage0(data_dir: Path, out: Path) -> None:
    weeks = load_panel(out)
    raw = fc.rd(out / "panel.json")
    info = fc.rd(data_dir / "exchange_info.json")["info"]
    ns = [len(w["rows"]) for w in raw["weeks"]]
    print(f"rank Mondays {len(raw['weeks'])} ({ymd(FIRST)}..{ymd(LAST)}); weeks with N >= {MIN_N}: {len(weeks)}")
    print(f"universe N per week: min {min(ns)}, median {statistics.median(ns)}, max {max(ns)}; exclusion reasons {raw['reasons']}")
    by_year: dict[str, Counter[str]] = {}
    trunc = 0
    big = 0
    for w in weeks:
        y = ymd(w["day"])[:4]
        for r in w["rows"]:
            by_year.setdefault(y, Counter())[(info.get(r[0]) or {}).get("status") or "REMOVED"] += 1
            trunc += bool(r[IX["trunc"]])
            big += abs(r[IX["r"]]) > 1.0
    for y, c in sorted(by_year.items()):
        tot = sum(c.values())
        print(f"  {y}: {dict(c)} delisted share {(tot - c['TRADING']) / tot:.1%}")
    print(f"legs truncated by delisting inside the week (kept, return to last close): {trunc}; symbol-weeks with |r| > 100 %: {big}")
    # signal distributions (levels, not IC)
    allrows = [r for w in weeks for r in w["rows"]]
    for s in (*SIGNALS, "beta"):
        v = sorted(col(allrows, s))
        print(f"  {s:<6} p1 {v[len(v) // 100]:+.4f} p50 {v[len(v) // 2]:+.4f} p99 {v[99 * len(v) // 100]:+.4f} (n {len(v)})")
    btc_iv = [r[IX["IVOL30"]] for w in weeks for r in w["rows"] if r[0] == BTC]
    print(f"  BTCUSDT IVOL30 max {max(btc_iv):.2e} (expected ~0); in universe {len(btc_iv)} weeks")
    print(f"  r_lag1 missing: {sum(r[IX['r_lag1']] is None for r in allrows)}")
    print()
    z = holm_z(N_TESTS)
    print(f"STAGE 0 (no means): MDE = 2.8 x SE_NW, MDE_Holm = {z:.3f} x SE_NW (Holm {N_TESTS}, NW lag {NW_LAG}); gate MDE_Holm <= {GATE}")
    res: dict[str, Any] = {}
    for s in SIGNALS:
        ics = series(weeks, s)
        nic = series(weeks, s, ctrl=NEUTRAL)
        se, sen = newey_west_se(ics, NW_LAG), newey_west_se(nic, NW_LAG)
        gate = z * se <= GATE
        res[s] = {"n": len(ics), "sd": statistics.stdev(ics), "se": se, "mde": mde_nw(ics, NW_LAG), "mde_holm": z * se, "gate": gate}
        print(
            f"  {s:<7} n={len(ics)} SD={statistics.stdev(ics):.4f} SE_NW={se:.4f} MDE={2.8 * se:.4f} MDE_Holm={z * se:.4f} "
            f"gate={'PASS' if gate else 'FAIL -> inconclusive'} | neutralised SD={statistics.stdev(nic):.4f} SE_NW={sen:.4f}"
        )
    fc.wr(out / "stage0.json", res)
    print(f"done; {rss()}")


# ---------------------------------------------------------------- full test


def stat(xs: list[float]) -> tuple[float, float, float, float]:
    m = statistics.fmean(xs)
    se = newey_west_se(xs, NW_LAG)
    return m, se, m / se if se > 0 else float("nan"), two_sided_p(m / se) if se > 0 else 1.0


def fmt(label: str, xs: list[float]) -> str:
    m, se, t, p = stat(xs)
    return f"    {label:<44} n={len(xs):>3} IC {m:+.4f} CI95 [{m - 1.96 * se:+.4f}; {m + 1.96 * se:+.4f}] t {t:+.2f} p {p:.4f}"


def stage_full(data_dir: Path, out: Path, n_perm: int) -> None:
    s0 = fc.rd(out / "stage0.json") if (out / "stage0.json").exists() else None
    if not s0:
        raise SystemExit("run stage0 first")
    weeks = load_panel(out)
    info = fc.rd(data_dir / "exchange_info.json")["info"]
    days = [w["day"] for w in weeks]
    n_oos = len(weeks) - (len(weeks) * 3) // 4
    is_mask = [i < len(weeks) - n_oos for i in range(len(weeks))]
    print(
        f"weeks {len(weeks)}; IS {sum(is_mask)} ({ymd(days[0])}..{ymd(days[sum(is_mask) - 1])}); OOS {n_oos} ({ymd(days[sum(is_mask)])}..{ymd(days[-1])})"
    )
    passed = [s for s in SIGNALS if s0[s]["gate"]]
    for s in SIGNALS:
        if s not in passed:
            print(f"  {s}: gate failed in stage 0 (MDE_Holm {s0[s]['mde_holm']:.4f}) -> inconclusive, mean not computed")

    main: dict[str, list[float]] = {s: series(weeks, s) for s in passed}
    neut: dict[str, list[float]] = {s: series(weeks, s, ctrl=NEUTRAL) for s in passed}
    neut_b: dict[str, list[float]] = {s: series(weeks, s, ctrl=NEUTRAL_B) for s in passed}
    ps = [stat(main[s])[3] if s in passed else 1.0 for s in SIGNALS]
    ph = dict(zip(SIGNALS, holm(ps), strict=True))
    print()
    print("=== MAIN (registered): mean weekly rank IC, NW lag 4; Holm over 4 signals ===")
    for s in passed:
        m, se, t, p = stat(main[s])
        print(f"  {s}")
        print(fmt("raw IC", main[s]) + f" Holm {ph[s]:.4f}")
        print(fmt("neutralised (funding 7d, log turnover 30d)", neut[s]))
        print(fmt("neutralised + beta30 (descriptive)", neut_b[s]))
    # consistency check against funding-crowding (Sunday panel, IC -0.0038)
    fic = [rank_ic(col(w["rows"], "fund"), col(w["rows"], "r")) for w in weeks]
    print(fmt("check: trailing funding IC (fc Sunday: -0.0038)", fic))

    verdict: dict[str, str] = {}
    for s in passed:
        xs = main[s]
        m = statistics.fmean(xs)
        sg = 1.0 if m > 0 else -1.0
        print()
        print(f"=== ROBUSTNESS {s} (registered; sign of the full-sample IC {sg:+.0f}) ===")
        iso = [x for x, k in zip(xs, is_mask, strict=True) if k]
        oos = [x for x, k in zip(xs, is_mask, strict=True) if not k]
        print(fmt("IS", iso))
        print(fmt("OOS (last 25 %)", oos))
        print(fmt("neutralised IS", [x for x, k in zip(neut[s], is_mask, strict=True) if k]))
        print(fmt("neutralised OOS", [x for x, k in zip(neut[s], is_mask, strict=True) if not k]))
        for y in sorted({ymd(d)[:4] for d in days}):
            print(fmt(f"year {y}", [x for x, d in zip(xs, days, strict=True) if ymd(d)[:4] == y]))
        roll = rolling_means(xs, 52)
        print(
            f"    rolling 52w: min {min(roll):+.4f} max {max(roll):+.4f} share with the full-sample sign {sum(r * sg > 0 for r in roll) / len(roll):.0%}"
        )
        nt = without_top(xs, 10, sg)
        print(fmt("without the 10 best weeks (in the effect's direction)", nt))
        myx = {r[0] for w in weeks for r in w["rows"] if abs(r[IX["r"]]) > 1.0}
        xa = [ic_week([r for r in w["rows"] if r[0] not in myx], s) for w in weeks]
        xb = [ic_week([r for r in w["rows"] if abs(r[IX["r"]]) <= 1.0], s) for w in weeks]
        print(fmt(f"(a) without {len(myx)} symbols with any |weekly r| > 100 %", xa))
        print(fmt("(b) without symbol-weeks with |r| > 100 %", xb))
        print(fmt("entry lag 1 day (open t+1 -> close t+7)", series(weeks, s, ret="r_lag1")))
        alive = [ic_week([r for r in w["rows"] if (info.get(r[0]) or {}).get("status") == "TRADING"], s) for w in weeks]
        print(fmt("TRADING-only universe (survivorship, descriptive)", alive))
        t0 = time.time()
        perm = placebo_mean_ics([(col(w["rows"], s), col(w["rows"], "r")) for w in weeks], n_perm, seed=SEED)
        p_perm = (1 + int(np.sum(np.abs(perm) >= abs(m)))) / (1 + n_perm)
        print(
            f"    placebo ({n_perm} within-week shuffles): SD of mean IC {perm.std():.4f}, |obs| {abs(m):.4f}, p {p_perm:.4f} ({time.time() - t0:.0f}s)"
        )
        # per asset (5a)
        vals, assets, clus = [], [], []
        for w in weeks:
            pr = ic_products(col(w["rows"], s), col(w["rows"], "r"))
            vals.extend(pr.tolist())
            assets.extend(col(w["rows"], "sym"))
            clus.extend([w["day"]] * len(w["rows"]))
        het = heterogeneity(vals, assets, min_n=20)
        pq = permutation_q(vals, assets, clus, min_n=20, n_perm=500, seed=SEED)
        dmask = {d: k for d, k in zip(days, is_mask, strict=True)}
        ism = [dmask[c] for c in clus]
        per = {}
        for lab, kw in (("top 10", {"top_n": 10}), ("top quarter", {"top_frac": 0.25})):
            per[lab] = persistence(vals, assets, clus, ism, min_n_is=20, n_boot=2000, seed=SEED, **kw)
        print(
            f"    per asset: k={het['k']} (>= 20 weeks) Q={het['q']:.1f} df={het['df']} p={het['p']:.4f} I2={het['i2']:.2f}; "
            f"within-week label permutation p={pq['p']:.4f} (Q null mean {pq['null_mean']:.1f})"
        )
        for lab, r in per.items():
            print(
                f"    persistence {lab}: IS top {r['top_is_mean']:+.4f} rest {r['rest_is_mean']:+.4f} -> OOS top {r['top_oos_mean']:+.4f} "
                f"(n {r['n_top_oos']}) rest {r['rest_oos_mean']:+.4f}; diff {r['diff']:+.4f} CI [{r['diff_ci'][0]:+.4f}; {r['diff_ci'][1]:+.4f}] "
                f"p {r['diff_p']:.3f}; rho IS-OOS {r['rho']:+.3f} (k {r['rho_n']})"
            )
        top = sorted(((a, n, mu) for a, n, mu in per["top 10"]["is_ranked"]), key=lambda x: -x[2] * sg)[:5]
        print(f"    largest IS contributors (direction of the effect): {[(a, n, round(mu, 3)) for a, n, mu in top]}")
        # verdict per the registered rule
        neut_m, neut_se, _, _ = stat(neut[s])
        same_n = neut_m * sg > 0 and abs(neut_m) > 1.96 * neut_se
        stable = statistics.fmean(iso) * sg > 0 and statistics.fmean(oos) * sg > 0 and statistics.fmean(nt) * sg > 0
        if ph[s] < 0.05 and stable and abs(m) >= GATE and same_n:
            verdict[s] = "CONFIRMED"
        elif ph[s] < 0.05 and stable:
            verdict[s] = "DIRECTIONALLY RIGHT"
        else:
            verdict[s] = "NOT CONFIRMED"
        print(
            f"    VERDICT INPUTS: Holm p {ph[s]:.4f}; |IC| {abs(m):.4f} (>= {GATE}); IS/OOS/no-top10 same sign {stable}; "
            f"neutralised same sign & CI excl. 0 {same_n} -> {verdict[s]}"
        )

        # descriptive basket 20/20
        bw = []
        for w in weeks:
            uni = {r[0]: Candidate(r[0], r[IX[s]], r[IX["mqv"]], 0) for r in w["rows"]}
            legs = {r[0]: LegWeek(r[0], r[IX["r"]], r[IX["fund_long"]], 0.0, 0.0, 0, bool(r[IX["trunc"]]), 0, 0) for r in w["rows"]}
            bw.append(Week(w["day"], uni, legs))

        def pick(wk: Week, sg: float = sg) -> Any:
            return rank_pick(wk, cap=20, signal=oriented_signal({k: c.signal for k, c in wk.universe.items()}, sg))

        res = [x for x in simulate(bw, pick) if x.n_side > 0]
        net = [x.net for x in res]
        lo, hi = block_bootstrap_ci(net, n_boot=5000, seed=SEED)
        print(
            f"    BASKET 20/20 (descriptive, direction {sg:+.0f} chosen in-sample): n={len(net)} net {statistics.fmean(net) * 100:+.3f}%/wk "
            f"({statistics.fmean(net) * 5200:+.1f}%/yr on gross notional) CI95 [{lo * 100:+.3f}; {hi * 100:+.3f}] MDE {mde_nw(net, NW_LAG) * 100:.2f} pp | "
            f"price {statistics.fmean(x.price for x in res) * 100:+.3f} funding {statistics.fmean(x.funding for x in res) * 100:+.3f} "
            f"cost {statistics.fmean(x.cost for x in res) * 100:.3f} turnover {statistics.fmean(x.turnover for x in res):.2f}; worst week {min(net) * 100:+.1f}%"
        )
        print(f"    ({rss()})", flush=True)

    print()
    print("=== REGISTERED VERDICT ===")
    for s in SIGNALS:
        print(f"  {s:<7} {verdict.get(s, 'UNVERIFIABLE (gate)')}")
    card = "confirmed" if "CONFIRMED" in verdict.values() else ("inconclusive" if len(passed) < len(SIGNALS) else "rejected")
    print(f"  card status: {card}")


def stage_extra(data_dir: Path, out: Path) -> None:
    """Exploratory, added after the full run (labelled in the report; no verdict depends on it): is the MAX30 rank IC a
    mean effect or a median effect of volatility (volatility drag / right skew of volatile coins)?"""
    weeks = load_panel(out)
    data = fc.load_all(data_dir)
    print(f"loaded {len(data)} symbols; {rss()}", flush=True)
    from tradedesk.analytics.funding_crowding import fm_slope
    from tradedesk.analytics.xs_factors import daily_returns

    sd30: dict[tuple[str, int], float] = {}
    for w in weeks:
        for r in w["rows"]:
            dr = list(daily_returns(data[r[0]].bars, w["day"]).values())
            sd30[(r[0], w["day"])] = statistics.stdev(dr)
    del data
    rng = np.random.default_rng(SEED)
    for s in ("MAX30", "M28"):
        print()
        print(f"=== EXTRA {s} (exploratory, after the full run) ===")
        fms = [fm_slope(col(w["rows"], s), col(w["rows"], "r")) for w in weeks]
        m, se, t, _ = stat(fms)
        print(f"  Fama-MacBeth slope on rank z: {m * 100:+.3f} pp/week per 1 SD rank, SE_NW {se * 100:.3f}, t {t:+.2f}")
        q_mean: list[list[float]] = [[] for _ in range(5)]
        q_med: list[list[float]] = [[] for _ in range(5)]
        q_fund: list[list[float]] = [[] for _ in range(5)]
        for w in weeks:
            rows = sorted(w["rows"], key=lambda r: r[IX[s]])
            n = len(rows)
            for k in range(5):
                part = rows[k * n // 5 : (k + 1) * n // 5]
                q_mean[k].append(statistics.fmean(r[IX["r"]] for r in part))
                q_med[k].append(statistics.median(r[IX["r"]] for r in part))
                q_fund[k].append(statistics.fmean(r[IX["fund_long"]] for r in part))
        for k in range(5):
            lo, hi = block_bootstrap_ci(q_mean[k], n_boot=2000, seed=SEED)
            print(
                f"  quintile {k + 1} (low -> high {s}): mean weekly r {statistics.fmean(q_mean[k]) * 100:+.3f}% CI [{lo * 100:+.3f}; {hi * 100:+.3f}] "
                f"median-of-quintile r {statistics.fmean(q_med[k]) * 100:+.3f}% funding to a long {statistics.fmean(q_fund[k]) * 100:+.3f}%"
            )
        d = [a - b for a, b in zip(q_mean[0], q_mean[4], strict=True)]
        lo, hi = block_bootstrap_ci(d, n_boot=5000, seed=SEED)
        print(
            f"  Q1 - Q5 mean price spread {statistics.fmean(d) * 100:+.3f}%/wk CI [{lo * 100:+.3f}; {hi * 100:+.3f}] (before costs and funding)"
        )
        df_ = [a - b for a, b in zip(q_fund[0], q_fund[4], strict=True)]
        print(f"  Q1 - Q5 funding (long Q1, short Q5) {statistics.fmean(df_) * 100:+.3f}%/wk")
        # null: equal arithmetic means, lognormal weekly returns with each row's own trailing 30d daily SD
        sims = []
        for _ in range(20):
            ics = []
            for w in weeks:
                sig = np.array([sd30[(r[0], w["day"])] for r in w["rows"]]) * math.sqrt(7)
                rs = np.exp(sig * rng.standard_normal(sig.size) - sig**2 / 2) - 1
                ics.append(rank_ic(col(w["rows"], s), rs.tolist()))
            sims.append(statistics.fmean(ics))
        print(
            f"  null (equal means, lognormal with own trailing vol, 20 draws): mean IC {statistics.fmean(sims):+.4f} "
            f"(range {min(sims):+.4f}..{max(sims):+.4f}) vs observed {statistics.fmean(series(weeks, s)):+.4f}"
        )
        vic = [rank_ic([sd30[(r[0], w["day"])] for r in w["rows"]], col(w["rows"], "r")) for w in weeks]
        print(fmt("IC of trailing 30d total vol (SD of daily r)", vic))
        resid = [partial_rank_ic(col(w["rows"], s), col(w["rows"], "r"), [[sd30[(r[0], w["day"])] for r in w["rows"]]]) for w in weeks]
        print(fmt(f"{s} IC after neutralising trailing 30d total vol", resid))
    print(f"done; {rss()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["panel", "stage0", "full", "extra"])
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/xs_factor_ic_panel")
    ap.add_argument("--perm", type=int, default=1000)
    a = ap.parse_args()
    data, out = a.data_dir.expanduser(), a.cache_dir.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    if a.stage == "panel":
        stage_panel(data, out)
    elif a.stage == "stage0":
        stage0(data, out)
    elif a.stage == "full":
        stage_full(data, out, a.perm)
    else:
        stage_extra(data, out)


if __name__ == "__main__":
    main()
