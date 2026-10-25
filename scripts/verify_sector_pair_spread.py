"""Verify IDEA-20261025-sector-pair-weekly-spread-reversion: z60 spread reversion in pairs of close substitutes.

No network: reads the per-file cache of ``scripts/verify_funding_crowding.py`` (Binance USDT-M daily klines and
funding from data.binance.vision, 2021-10..2026-09, delisted symbols included). Pre-registration: the idea card.

Stages:
    stage0  pair counts, events, measured SD, cluster SE and MDE of H1 net (no means printed); the gate
    power   after a failed gate: tails of H1 net (data integrity) and exploratory power of nearby designs (no means)
    full    H1-H3, placebo, IS/OOS, without top-10, per-pair check, sensitivity grid

    uv run python scripts/verify_sector_pair_spread.py stage0 --out ~/.cache/tradedesk-agents/sector_pair_spread/stage0.txt
"""

from __future__ import annotations

import argparse
import math
import resource
import sys
import time
import warnings
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_funding_crowding as fc  # noqa: E402  (cache layout and metadata helpers; scripts/ is not a package)

from tradedesk.analytics.pair_spread import (  # noqa: E402
    Params,
    Trade,
    close_trade,
    cluster_bootstrap_ci,
    cluster_se,
    control_cross_sector,
    control_low_corr,
    form_pairs,
    hedge_weights,
    ols_slope,
    rolling_z,
    simulate_pair,
)
from tradedesk.analytics.per_asset import heterogeneity, mde, permutation_q, persistence  # noqa: E402

DAY_MS = 86_400_000
D0 = int(datetime(2021, 10, 1, tzinfo=UTC).timestamp()) // 86400
D_END = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp()) // 86400  # exclusive
COST, COST_STRESS = 0.0062, 0.0124
FORM_DAYS, MIN_RET, QV_DAYS, MIN_QV = 180, 170, 30, 10e6
IS_END = int(datetime(2025, 3, 1, tzinfo=UTC).timestamp()) // 86400
SEED = 20261025
OUT: Any = None


def say(s: str = "") -> None:
    print(s, flush=True)
    if OUT is not None:
        OUT.write(s + "\n")
        OUT.flush()


def rss() -> str:
    return f"rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


def week(day: int) -> str:
    y, w, _ = datetime.fromtimestamp(day * 86400, UTC).isocalendar()
    return f"{y}-W{w:02d}"


# ---------------------------------------------------------------- data


def build_grid(data_dir: Path, out_dir: Path) -> dict[str, Any]:
    npz = out_dir / "grid.npz"
    if npz.exists():
        g = np.load(npz, allow_pickle=False)
        return {k: g[k] for k in g.files}
    syms = sorted(s for s in set(fc.eligible(data_dir)) | {fc.BTC} if fc.months_of(data_dir, s))
    T, N = D_END - D0, len(syms)
    o, c, qv, fu = (np.full((T, N), np.nan) for _ in range(4))
    fu[:] = 0.0
    intervals: Counter[float] = Counter()
    t0 = time.time()
    for j, s in enumerate(syms):
        for mo in fc.months_of(data_dir, s):
            kp, fp = fc.kpath(data_dir, s, mo), fc.fpath(data_dir, s, mo)
            for r in (fc.rd(kp) or []) if kp.exists() else []:
                t = int(r[0]) // DAY_MS - D0
                if 0 <= t < T and r[5] > 0 and r[1] > 0 and r[4] > 0:
                    o[t, j], c[t, j], qv[t, j] = r[1], r[4], r[6]
            for r in (fc.rd(fp) or []) if fp.exists() else []:
                t = int(r[0]) // DAY_MS - D0
                if 0 <= t < T:
                    fu[t, j] += r[2]
                    intervals[r[1]] += 1
        if j % 100 == 0:
            say(f"  loaded {j}/{N} symbols in {time.time() - t0:.0f}s, {rss()}")
    out_dir.mkdir(parents=True, exist_ok=True)
    iv = np.array(sorted(intervals.items()), dtype=float).reshape(-1, 2)
    np.savez(npz, o=o, c=c, qv=qv, fu=fu, syms=np.array(syms), intervals=iv)
    return {"o": o, "c": c, "qv": qv, "fu": fu, "syms": np.array(syms), "intervals": iv}


def month_starts() -> list[int]:
    out = []
    for y in range(2022, 2027):
        for m in range(1, 13):
            d = int(datetime(y, m, 1, tzinfo=UTC).timestamp()) // 86400 - D0
            if int(datetime(2022, 4, 1, tzinfo=UTC).timestamp()) // 86400 - D0 <= d <= D_END - D0 - 30:
                out.append(d)
    return out


def log_returns(c: np.ndarray) -> np.ndarray:
    r = np.full_like(c, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        r[1:] = np.log(c[1:]) - np.log(c[:-1])
    return r


def admitted(c: np.ndarray, qv: np.ndarray, R: np.ndarray, F: int, btc: int) -> np.ndarray:
    """Columns admitted at formation day F, from rows before F only: bar on F-1, >= 170 of 180 returns, median
    quote volume over the last 30 days >= 10 M USD; BTC excluded (market factor)."""
    ok_ret = np.isfinite(R[F - FORM_DAYS : F]).sum(axis=0) >= MIN_RET
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-nan columns (symbols not listed yet)
        mq = np.nanmedian(qv[F - QV_DAYS : F], axis=0)
    cols = np.where(np.isfinite(c[F - 1]) & ok_ret & (np.nan_to_num(mq) >= MIN_QV))[0]
    return cols[cols != btc]


def formations(g: dict[str, Any]) -> list[dict[str, Any]]:
    """Point-in-time pair sets per month: H1 and the two H3 controls (column indices into the grid)."""
    c, qv, syms = g["c"], g["qv"], [str(s) for s in g["syms"]]
    R = log_returns(c)
    btc = syms.index(fc.BTC)
    rng = np.random.default_rng(SEED)
    months = month_starts()
    res = []
    for k, F in enumerate(months):
        nxt = months[k + 1] if k + 1 < len(months) else D_END - D0
        win = slice(F - FORM_DAYS, F)
        cols = admitted(c, qv, R, F, btc)
        f = form_pairs(R[win][:, cols], R[win, btc])
        h1 = [(int(cols[p.a]), int(cols[p.b]), p.h, p.rho, p.rho_e) for p in f.pairs]

        def ctrl(
            pairs: list[tuple[int, int]], f: Any = f, cols: np.ndarray = cols, win: slice = win
        ) -> list[tuple[int, int, float, float, float]]:
            out = []
            for i, j in pairs:
                h = ols_slope(R[win, cols[i]], R[win, cols[j]])
                h = min(max(h if math.isfinite(h) else 1.0, 0.5), 2.0)
                out.append((int(cols[i]), int(cols[j]), h, float(f.rho[i, j]), float(f.rho_e[i, j])))
            return out

        dup = [
            (syms[cols[i]], syms[cols[j]], float(f.rho[i, j]))
            for i in range(len(cols))
            for j in range(i + 1, len(cols))
            if f.rho[i, j] >= 0.98
        ]
        res.append(
            {
                "F": F,
                "next": nxt,
                "n_elig": len(cols),
                "n_clusters": len(set(f.labels)),
                "n_multi": sum(1 for v in Counter(f.labels).values() if v >= 2),
                "h1": h1,
                "h3a": ctrl(control_low_corr(f.rho, len(h1), rng)),
                "h3b": ctrl(control_cross_sector(f.rho, f.rho_e, f.labels, len(h1))),
                "dup": dup,
            }
        )
    return res


def run_set(g: dict[str, Any], forms: list[dict[str, Any]], key: str, p: Params, *, unit_h: bool = False) -> list[dict[str, Any]]:
    o, c, fu, syms = g["o"], g["c"], g["fu"], [str(s) for s in g["syms"]]
    zc: dict[tuple[int, int], np.ndarray] = {}
    free: dict[tuple[int, int], int] = {}
    out = []
    for fm in forms:
        for a, b, h, rho, rho_e in fm[key]:
            if (a, b) not in zc:
                with np.errstate(invalid="ignore", divide="ignore"):
                    zc[(a, b)] = rolling_z(np.log(c[:, a]) - np.log(c[:, b]))
            wa, wb = hedge_weights(1.0 if unit_h else h)
            trs, free[(a, b)] = simulate_pair(
                o[:, a], c[:, a], o[:, b], c[:, b], fu[:, a], fu[:, b], zc[(a, b)],
                w_a=wa, w_b=wb, sig_lo=fm["F"], sig_hi=fm["next"], free_from=free.get((a, b), 0), p=p,
            )  # fmt: skip
            for t in trs:
                out.append(
                    {
                        "pair": f"{syms[a]}/{syms[b]}",
                        "a": a,
                        "b": b,
                        "wa": wa,
                        "wb": wb,
                        "F": fm["F"],
                        "next": fm["next"],
                        "t": t,
                        "rho": rho,
                        "rho_e": rho_e,
                    }
                )
    return out


def nets(trs: list[dict[str, Any]], cost: float = COST, funding: bool = True) -> np.ndarray:
    return np.array([r["t"].price + (r["t"].funding if funding else 0.0) - cost for r in trs])


def weeks_of(trs: list[dict[str, Any]]) -> list[str]:
    return [week(r["t"].entry + D0) for r in trs]


# ---------------------------------------------------------------- stages


def sanity(g: dict[str, Any], forms: list[dict[str, Any]]) -> None:
    syms = [str(s) for s in g["syms"]]
    say(f"grid: {len(syms)} symbols x {g['c'].shape[0]} days ({ymd(D0)}..{ymd(D_END - 1)}), {rss()}")
    say("funding intervals (hours: settlements): " + ", ".join(f"{int(h)}h: {int(n)}" for h, n in g["intervals"]))
    j = syms.index("FTTUSDT") if "FTTUSDT" in syms else None
    if j is not None:
        lo = int(datetime(2022, 11, 1, tzinfo=UTC).timestamp()) // 86400 - D0
        say(
            f"check FTTUSDT funding 2022-11 sum {g['fu'][lo : lo + 30, j].sum():+.4f}, min daily {g['fu'][lo : lo + 30, j].min():+.4f} (fixture: 126 settlements, min rate -0.0250)"
        )
    b = syms.index(fc.BTC)
    d = int(datetime(2024, 12, 31, tzinfo=UTC).timestamp()) // 86400 - D0
    say(f"check BTCUSDT close 2024-12-31 {g['c'][d, b]:.0f} (known ~93,500)")
    dup = Counter((x, y) for fm in forms for x, y, _ in fm["dup"])
    say(
        f"pairs with rho >= 0.98 (excluded, rename/duplicate guard): {len(dup)} distinct; top: "
        + ", ".join(f"{x}/{y}x{n}" for (x, y), n in dup.most_common(8))
    )


def describe_forms(forms: list[dict[str, Any]], g: dict[str, Any]) -> None:
    syms = [str(s) for s in g["syms"]]
    say("month       elig clusters multi  H1 H3a H3b")
    for fm in forms[:: max(1, len(forms) // 18)]:
        say(
            f"{ymd(fm['F'] + D0)}  {fm['n_elig']:4d} {fm['n_clusters']:8d} {fm['n_multi']:5d} {len(fm['h1']):3d} {len(fm['h3a']):3d} {len(fm['h3b']):3d}"
        )
    tot = Counter(f"{syms[a]}/{syms[b]}" for fm in forms for a, b, *_ in fm["h1"])
    say(
        f"H1 pair-months {sum(tot.values())}, distinct pairs {len(tot)}; most frequent: "
        + ", ".join(f"{k} x{n}" for k, n in tot.most_common(15))
    )
    rh = [x[3] for fm in forms for x in fm["h1"]]
    re = [x[4] for fm in forms for x in fm["h1"]]
    hs = [x[2] for fm in forms for x in fm["h1"]]
    if rh:
        say(
            f"H1 rho median {np.median(rh):.3f}, rho_e median {np.median(re):.3f}, h median {np.median(hs):.2f} (clipped share {np.mean([x in (0.5, 2.0) for x in hs]):.0%})"
        )


def stage0(g: dict[str, Any], forms: list[dict[str, Any]]) -> dict[str, float]:
    trs = run_set(g, forms, "h1", Params())
    v = nets(trs)
    wk = weeks_of(trs)
    reasons = Counter(r["t"].reason for r in trs)
    holds = [r["t"].exit - r["t"].entry for r in trs]
    se = cluster_se(v, wk)
    m = mde(se, one_sided=False)
    say(f"H1 trades {len(trs)}, distinct entry weeks (n_eff) {len(set(wk))}, pairs {len(set(r['pair'] for r in trs))}")
    say(f"exits {dict(reasons)}, hold median {np.median(holds):.0f} d, truncated {sum(r['t'].truncated for r in trs)}")
    say(f"net SD {v.std(ddof=1):.4f}, iid SE {v.std(ddof=1) / math.sqrt(len(v)):.4f}, cluster(week) SE {se:.4f}, MDE(2.80 x SE) {m:.4f}")
    say(f"IS trades {sum(r['t'].entry + D0 < IS_END for r in trs)}, OOS trades {sum(r['t'].entry + D0 >= IS_END for r in trs)}")
    say(f"GATE: MDE {m * 100:.2f} pp vs 1.00 pp -> {'PASS' if m <= 0.01 else 'FAIL (inconclusive, no means)'}")
    return {"mde": m, "n": len(trs)}


def power_only(label: str, trs: list[dict[str, Any]]) -> None:
    """n, n_eff, SD, cluster SE and MDE of net returns; deliberately no means (the gate failed)."""
    v, wk = nets(trs), weeks_of(trs)
    if len(v) < 3:
        say(f"{label:44s} n={len(v)}")
        return
    se = cluster_se(v, wk)
    say(f"{label:44s} n={len(v):5d} wk={len(set(wk)):3d} SD {v.std(ddof=1):.4f} SE_cl {se:.4f} MDE {mde(se, one_sided=False) * 100:.2f} pp")


def tails(trs: list[dict[str, Any]]) -> None:
    """Data-integrity view of the tails: |net| quantiles and the largest |net| trades (no means)."""
    v = nets(trs)
    q = np.quantile(np.abs(v), [0.5, 0.9, 0.99])
    say(f"|net| quantiles p50 {q[0]:.3f} p90 {q[1]:.3f} p99 {q[2]:.3f}, max {np.abs(v).max():.3f}")
    for i in np.argsort(-np.abs(v))[:6]:
        t = trs[i]["t"]
        say(
            f"  {trs[i]['pair']:26s} {ymd(t.entry + D0)}..{ymd(t.exit + D0)} side {t.side:+d} {t.reason:6s} price {t.price:+.3f} funding {t.funding:+.4f}"
        )


def loose_forms(g: dict[str, Any], forms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exploratory: all disjoint pairs with 0.80 <= rho < 0.98 by descending rho_e (no one-per-cluster rule)."""
    c, qv, syms = g["c"], g["qv"], [str(s) for s in g["syms"]]
    R = log_returns(c)
    btc = syms.index(fc.BTC)
    out = []
    for fm in forms:
        F = fm["F"]
        win = slice(F - FORM_DAYS, F)
        cols = admitted(c, qv, R, F, btc)
        f = form_pairs(R[win][:, cols], R[win, btc])
        n = len(cols)
        cand = sorted(
            (
                (float(f.rho_e[i, j]), i, j)
                for i in range(n)
                for j in range(i + 1, n)
                if 0.80 <= f.rho[i, j] < 0.98 and math.isfinite(f.rho_e[i, j])
            ),
            reverse=True,
        )
        used: set[int] = set()
        sel = []
        for re, i, j in cand:
            if i in used or j in used:
                continue
            used.update((i, j))
            h = ols_slope(R[win, cols[i]], R[win, cols[j]])
            sel.append((int(cols[i]), int(cols[j]), min(max(h if math.isfinite(h) else 1.0, 0.5), 2.0), float(f.rho[i, j]), re))
        out.append({**fm, "loose": sel})
    return out


def stage_power(g: dict[str, Any], forms: list[dict[str, Any]]) -> None:
    say("== tails of H1 net (data integrity) ==")
    tails(run_set(g, forms, "h1", Params()))
    say("== exploratory power (no means): would any nearby design pass the 1.0 pp gate? ==")
    lf = loose_forms(g, forms)
    say(f"loose formation: pair-months {sum(len(f['loose']) for f in lf)}")
    for z_in in (1.5, 2.0):
        for hold in (5, 10):
            p = replace(Params(), z_in=z_in, max_hold=hold)
            power_only(f"H1 z_in {z_in} hold {hold}", run_set(g, forms, "h1", p))
            power_only(f"loose z_in {z_in} hold {hold}", run_set(g, lf, "loose", p))
    power_only("loose z_in 2 hold 10 no stops", run_set(g, lf, "loose", replace(Params(), stops=False)))


def stat_line(label: str, v: np.ndarray, wk: list[str]) -> str:
    if len(v) < 2:
        return f"{label:34s} n={len(v)}"
    lo, hi = cluster_bootstrap_ci(v, wk)
    se = cluster_se(v, wk)
    return f"{label:34s} n={len(v):4d} wk={len(set(wk)):3d} mean {v.mean() * 100:+6.2f}%  CI [{lo * 100:+6.2f}, {hi * 100:+6.2f}]  t_cl {v.mean() / se:+5.2f}  med {np.median(v) * 100:+5.2f}%  win {np.mean(v > 0):.0%}"


def placebo(g: dict[str, Any], trs: list[dict[str, Any]], draws: int = 20) -> tuple[np.ndarray, list[str]]:
    o, c, fu = g["o"], g["c"], g["fu"]
    T = c.shape[0]
    rng = np.random.default_rng(SEED + 1)
    v, wk = [], []
    for r in trs:
        t: Trade = r["t"]
        a, b = r["a"], r["b"]
        hold = t.exit - t.entry
        got = []
        for _ in range(draws * 5):
            if len(got) >= draws:
                break
            e = int(rng.integers(r["F"], r["next"])) + 1
            if e >= T or not (math.isfinite(o[e, a]) and math.isfinite(o[e, b])):
                continue
            x = min(e + hold, T)
            pt = close_trade(
                o[:, a],
                c[:, a],
                o[:, b],
                c[:, b],
                fu[:, a],
                fu[:, b],
                w_a=r["wa"],
                w_b=r["wb"],
                signal=e - 1,
                entry=e,
                exit=x,
                side=t.side,
                reason="placebo",
            )
            got.append(pt.net(COST))
        if got:
            v.append(float(np.mean(got)))
            wk.append(week(t.entry + D0))
    return np.array(v), wk


def per_pair(trs: list[dict[str, Any]], v: np.ndarray, wk: list[str]) -> None:
    pairs = [r["pair"] for r in trs]
    cnt = Counter(pairs)
    say("per-pair (>= 5 trades): pair, n, mean net, 95% CI (normal)")
    for k, n in sorted(cnt.items(), key=lambda kv: -kv[1]):
        if n < 5:
            continue
        x = v[[p == k for p in pairs]]
        s = x.std(ddof=1) / math.sqrt(n)
        say(f"  {k:28s} {n:3d} {x.mean() * 100:+6.2f}%  [{(x.mean() - 1.96 * s) * 100:+6.2f}, {(x.mean() + 1.96 * s) * 100:+6.2f}]")
    het = heterogeneity(v, pairs, min_n=5)
    say(
        f"Cochran Q (pairs with >= 5 trades): k={het['k']} n={het['n']} Q={het.get('q', float('nan')):.1f} df={het['df']} p={het.get('p')} I2={het.get('i2', float('nan')):.2f}"
    )
    pq = permutation_q(v, pairs, wk, min_n=5, n_perm=2000)
    say(
        f"permutation of pair labels within weeks: Q={pq['q']:.1f}, null mean {pq['null_mean']:.1f}, null p95 {pq['null_p95']:.1f}, p={pq['p']}"
    )
    ism = [r["t"].entry + D0 < IS_END for r in trs]
    ps = persistence(v, pairs, wk, ism, min_n_is=3, top_frac=0.25, min_n_oos_rho=3)
    say(
        f"persistence: k={ps['k']} ranked IS pairs, top {len(ps['top'])}; IS top {ps['top_is_mean'] * 100:+.2f}% rest {ps['rest_is_mean'] * 100:+.2f}%; "
        f"OOS top {ps['top_oos_mean'] * 100:+.2f}% (n={ps['n_top_oos']}) rest {ps['rest_oos_mean'] * 100:+.2f}% (n={ps['n_rest_oos']}); "
        f"diff {ps['diff'] * 100:+.2f}% CI {tuple(round(x * 100, 2) for x in ps['diff_ci']) if ps['diff_ci'] else None} p={ps['diff_p']}; rho IS/OOS {ps['rho']:.2f} (n={ps['rho_n']})"
    )


def full(g: dict[str, Any], forms: list[dict[str, Any]]) -> None:
    trs = run_set(g, forms, "h1", Params())
    v, wk = nets(trs), weeks_of(trs)
    say("== H1 (pre-registered) ==")
    say(stat_line("H1 net (cost 0.62%)", v, wk))
    say(stat_line("H1 gross price only", nets(trs, 0.0, False), wk))
    say(stat_line("H1 funding only", np.array([r["t"].funding for r in trs]), wk))
    say(stat_line("H1 net, cost x2 (1.24%)", nets(trs, COST_STRESS), wk))
    for reason in ("target", "stop", "time", "end"):
        sel = [i for i, r in enumerate(trs) if r["t"].reason == reason]
        if sel:
            say(stat_line(f"  exit={reason}", v[sel], [wk[i] for i in sel]))
    say("== H2 ==")
    ism = np.array([r["t"].entry + D0 < IS_END for r in trs])
    say(stat_line("IS (< 2025-03-01)", v[ism], [w for w, m in zip(wk, ism, strict=True) if m]))
    say(stat_line("OOS (>= 2025-03-01)", v[~ism], [w for w, m in zip(wk, ism, strict=True) if not m]))
    order = np.argsort(-v)
    keep = np.ones(len(v), bool)
    keep[order[:10]] = False
    say(stat_line("without top-10 trades", v[keep], [w for w, k in zip(wk, keep, strict=True) if k]))
    say("top-10 trades: " + ", ".join(f"{trs[i]['pair']} {ymd(trs[i]['t'].entry + D0)} {v[i] * 100:+.1f}%" for i in order[:10]))
    say("bottom-5 trades: " + ", ".join(f"{trs[i]['pair']} {ymd(trs[i]['t'].entry + D0)} {v[i] * 100:+.1f}%" for i in order[-5:]))
    for y in range(2022, 2027):
        sel = [i for i, r in enumerate(trs) if ymd(r["t"].entry + D0).startswith(str(y))]
        if sel:
            say(stat_line(f"  year {y}", v[sel], [wk[i] for i in sel]))
    say("== H3 controls and placebo ==")
    for key, lab in (("h3a", "H3a low corr (rho<0.5)"), ("h3b", "H3b cross-sector (rho>=.8, rho_e<.1)")):
        t3 = run_set(g, forms, key, Params())
        say(stat_line(lab, nets(t3), weeks_of(t3)))
    pv, pwk = placebo(g, trs)
    say(stat_line("placebo (same pair/side/hold, random day)", pv, pwk))
    if len(pv) == len(v):
        say(stat_line("H1 - placebo (paired)", v - pv, wk))
    say("== capital ==")
    days = np.zeros(g["c"].shape[0])
    for r in trs:
        days[r["t"].entry : r["t"].exit] += 1
    act = days[month_starts()[0] :]
    yrs = len(act) / 365.0
    say(
        f"open pairs: mean {act.mean():.2f}, max {act.max():.0f}; trades/yr {len(v) / yrs:.0f}; sum net per yr {v.sum() / yrs * 100:+.1f}% of one pair notional; "
        f"on gross capital (2 x notional x max concurrent, unlevered) {v.sum() / yrs / (2 * act.max()) * 100:+.2f}%/yr"
    )
    say("== per-pair check (5a) ==")
    per_pair(trs, v, wk)
    say("== sensitivity grid (exploratory except z_in=2, hold=10, beta) ==")
    for z_in in (1.5, 2.0, 2.5):
        for hold in (5, 10, 20):
            t2 = run_set(g, forms, "h1", replace(Params(), z_in=z_in, max_hold=hold))
            say(stat_line(f"z_in {z_in} hold {hold}", nets(t2), weeks_of(t2)))
    t2 = run_set(g, forms, "h1", Params(), unit_h=True)
    say(stat_line("h = 1 (equal notionals)", nets(t2), weeks_of(t2)))
    t2 = run_set(g, forms, "h1", replace(Params(), stops=False))
    say(stat_line("no stops", nets(t2), weeks_of(t2)))
    say(stat_line("no funding", nets(trs, COST, False), wk))
    say(f"done, {rss()}")


def main() -> None:
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["stage0", "power", "full"])
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/sector_pair_spread")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        OUT = a.out.open("w")
    t0 = time.time()
    g = build_grid(a.data_dir, a.cache_dir)
    forms = formations(g)
    say(f"formations {len(forms)} months in {time.time() - t0:.0f}s, {rss()}")
    sanity(g, forms)
    describe_forms(forms, g)
    gate = stage0(g, forms)
    if a.stage == "power":
        stage_power(g, forms)
    if a.stage == "full":
        if gate["mde"] > 0.01:
            say("gate failed: full stage not run (pre-registered: inconclusive without means)")
        else:
            full(g, forms)
    say(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
