#!/usr/bin/env python3
"""Verify IDEA-20261023-alt-basket-leverage-benchmark: what a long equal-weight alt basket under 1-3x leverage
does over rolling 8-month windows (2021-11 ... 2026-09), Binance USDT-M daily bars and funding.

No network: reads the per-file cache built by ``scripts/verify_funding_crowding.py`` (``k1d/``, ``funding/``,
``listing/``, ``symbols.json``, ``exchange_info.json``) and writes its own dense panel and results to ``--out-dir``.

Stages:
    panel    build the dense days x symbols panel (npz) + sanity checks (known price, ticker collisions, funding)
    stage0   number of independent windows, SD of the window return, MDE -- no means, no shares
    full     pre-registered grid, H1-H3 with block-bootstrap CIs, robustness, vault comparison
    diag     funding received by the alt longs per asset (sanity)
    nonoverlap  H1/H2 on non-overlapping windows with exact CIs (added after the first run)
    bh       exploratory (after the review): buy-and-hold distribution, percentile table for both mechanisms,
             H2 hit dates, per-asset P&L over non-liquidated windows, starts before the vault's start

Usage:
    uv run python scripts/verify_alt_basket_leverage.py <stage> \
        --cache-dir ~/.cache/tradedesk-agents/funding_crowding --out-dir ~/.cache/tradedesk-agents/alt_basket_leverage
"""

from __future__ import annotations

import argparse
import json
import resource
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.funding_spread import normalize_asset
from tradedesk.analytics.leverage_basket import (
    Panel,
    WindowResult,
    block_bootstrap_by_month,
    clopper_pearson,
    median,
    percentile_of,
    select_basket,
    share,
    simulate_window,
)
from tradedesk.analytics.listing_drift import DAY_MS, symbol_eligible

FIRST_MONTH, LAST_MONTH = "2021-10", "2026-09"
DAY0 = int(datetime(2021, 10, 1, tzinfo=UTC).timestamp()) // 86400
LAST_DAY = int(datetime(2026, 9, 30, tzinfo=UTC).timestamp()) // 86400
FIRST_START = int(datetime(2021, 11, 1, tzinfo=UTC).timestamp()) // 86400  # Monday
SPLIT = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp()) // 86400
VAULT_START = int(datetime(2026, 2, 17, tzinfo=UTC).timestamp()) // 86400
WINDOW, REBAL, K, LOOKBACK = 243, 30, 12, 30
FEE, SLIP, MM = 0.0005, 0.0005, 0.025
BTC = "BTCUSDT"
LIB_BASKET = ("ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX", "DOT", "LTC", "UNI", "AAVE", "NEAR")  # vault_profile.py
BLOCK, N_BOOT, SEED = 8, 5000, 7


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


def month_id(day: int) -> int:
    d = datetime.fromtimestamp(day * 86400, UTC)
    return d.year * 12 + d.month - 1


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


# ---------------------------------------------------------------- panel


def stage_panel(cache: Path, out: Path) -> None:
    syms_all, info = rd(cache / "symbols.json"), rd(cache / "exchange_info.json")["info"]
    elig = [s for s in syms_all if symbol_eligible(s, (info.get(s) or {}).get("contractType"), (info.get(s) or {}).get("underlyingType"))]
    syms: list[str] = []
    for s in sorted(set(elig) | {BTC}):
        lp = cache / "listing" / f"{s}.json"
        ms = rd(lp) if lp.exists() else None
        if any(FIRST_MONTH <= m <= LAST_MONTH for m in ms or []):
            syms.append(s)
    n_days = LAST_DAY - DAY0 + 1
    shape = (n_days, len(syms))
    o, h, lo, c, qv, fu = (np.full(shape, np.nan) for _ in range(6))
    intervals: Counter[float] = Counter()
    t0 = time.time()
    for j, s in enumerate(syms):
        kd, fd = cache / "k1d" / s, cache / "funding" / s
        for kp in sorted(kd.glob("*.json")) if kd.exists() else []:
            for r in rd(kp) or []:
                d = int(r[0]) // DAY_MS - DAY0
                if not 0 <= d < n_days or len(r) < 6 or r[5] <= 0 or min(r[1], r[3], r[4]) <= 0:
                    continue
                o[d, j], h[d, j], lo[d, j], c[d, j] = r[1], r[2], r[3], r[4]
                qv[d, j] = r[6] if len(r) > 6 else r[5] * r[4]
        for fp in sorted(fd.glob("*.json")) if fd.exists() else []:
            for r in rd(fp) or []:
                d = int(r[0]) // DAY_MS - DAY0
                if 0 <= d < n_days:
                    fu[d, j] = (0.0 if np.isnan(fu[d, j]) else fu[d, j]) + float(r[2])
                    intervals[float(r[1])] += 1
        if (j + 1) % 100 == 0:
            print(f"  loaded {j + 1}/{len(syms)} in {time.time() - t0:.0f}s, peak RSS {rss_mb():.0f} MB", flush=True)
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / "panel.npz", open=o, high=h, low=lo, close=c, qv=qv, fund=fu)
    (out / "symbols.json").write_text(json.dumps(syms))
    print(f"panel: {len(syms)} symbols ({len(elig)} eligible in the archive list), {n_days} days {ymd(DAY0)} .. {ymd(LAST_DAY)}")
    print(f"funding settlements by interval (hours): {dict(sorted(intervals.items()))}")
    bj = syms.index(BTC)
    print(
        f"check BTCUSDT close 2024-12-31 = {c[int(datetime(2024, 12, 31, tzinfo=UTC).timestamp()) // 86400 - DAY0, bj]} (other reports: 93,548.9)"
    )
    bars = np.isfinite(c)
    held_no_fund = int((bars & np.isnan(fu)).sum())
    print(f"bar-days {int(bars.sum())}, bar-days without funding {held_no_fund} ({held_no_fund / max(1, bars.sum()):.2%})")
    groups: dict[str, list[str]] = {}
    for s in syms:
        groups.setdefault(normalize_asset(s[:-4], "binance")[0], []).append(s)
    for a, ss in sorted(groups.items()):
        if len(ss) > 1:
            lv = []
            for s in ss:
                j = syms.index(s)
                med = float(np.nanmedian(c[:, j])) / normalize_asset(s[:-4], "binance")[1]
                lv.append(f"{s} {med:.4g}/coin")
            print(f"  collision {a}: " + ", ".join(lv))
    print(f"peak RSS {rss_mb():.0f} MB")


def load(out: Path) -> tuple[Panel, list[str]]:
    z = np.load(out / "panel.npz")
    return Panel(z["open"], z["high"], z["low"], z["close"], z["qv"], z["fund"]), rd(out / "symbols.json")


# ---------------------------------------------------------------- runs


def starts(p: Panel) -> list[int]:
    n = p.close.shape[0]
    return list(range(FIRST_START - DAY0, n - WINDOW + 1, 7))


class Universe:
    def __init__(self, p: Panel, syms: list[str], *, include: set[str] | None = None, exclude: set[str] = frozenset(), k: int = K) -> None:
        assets = [normalize_asset(s[:-4], "binance")[0] for s in syms]
        ids = {a: i for i, a in enumerate(sorted(set(assets)))}
        self.group = np.array([ids[a] for a in assets])
        if include is not None:
            self.cand = np.array([s in include for s in syms])
        else:
            self.cand = np.array([s != BTC and assets[i] not in exclude for i, s in enumerate(syms)])
        self.p, self.k, self.memo = p, k, {}

    def __call__(self, d: int) -> list[int]:
        if d not in self.memo:
            self.memo[d] = select_basket(self.p.close, self.p.qv, d, k=self.k, lookback=LOOKBACK, candidates=self.cand, group=self.group)
        return self.memo[d]


def run_variant(p: Panel, uni: Universe, *, lev: float, mode: str, slip: float = SLIP, fee: float = FEE) -> list[WindowResult]:
    return [
        simulate_window(p, s, WINDOW, uni, leverage=lev, rebalance_every=REBAL, fee=fee, slip=slip, mm=MM, mode=mode) for s in starts(p)
    ]


def stage0(out: Path) -> None:
    p, syms = load(out)
    st = starts(p)
    span = (st[-1] + WINDOW) - st[0]
    n_eff = span / WINDOW
    uni = Universe(p, syms)
    sizes = Counter(len(uni(s)) for s in st)
    res = {m: run_variant(p, uni, lev=3.0, mode=m) for m in ("intraday", "close")}
    sd = {m: float(np.std([r.ret for r in v], ddof=1)) for m, v in res.items()}
    mde_share = 2.8 * (0.35 * 0.65 / n_eff) ** 0.5
    print(f"windows: {len(st)} starts {ymd(st[0] + DAY0)} .. {ymd(st[-1] + DAY0)} (weekly), length {WINDOW} d; span {span} d")
    print(f"n_eff = {span} / {WINDOW} = {n_eff:.2f} independent windows; basket sizes at start: {dict(sizes)}")
    for m in res:
        print(
            f"SD of the 8-month return, L=3, {m}: {sd[m]:.2f} (= {sd[m] * 100:.0f} pp); MDE of the mean (2.8 SD / sqrt n_eff) = {2.8 * sd[m] / n_eff**0.5 * 100:.0f} pp"
        )
    print(f"MDE of a share at p = 0.35: 2.8 * sqrt(p(1-p)/n_eff) = {mde_share * 100:.0f} pp")
    (out / "stage0.json").write_text(
        json.dumps({"n_windows": len(st), "span_days": span, "n_eff": n_eff, "sd_l3": sd, "mde_share_p35": mde_share})
    )
    print(f"peak RSS {rss_mb():.0f} MB")


def summary(res: list[WindowResult], months: list[int], *, block: int = BLOCK) -> dict[str, Any]:
    ret = np.array([r.ret for r in res])
    dd = np.array([r.max_dd for r in res])
    liq = np.array([r.liquidated for r in res], dtype=float)
    h1 = ((dd >= 0.70) | (liq > 0)).astype(float)
    h2 = (ret >= 1.0).astype(float)

    def ci(x: np.ndarray, f: Any) -> tuple[float, float]:
        return block_bootstrap_by_month(x, months, f, block=block, n_boot=N_BOOT, seed=SEED)

    return {
        "n": len(res),
        "liq": share(liq),
        "dd70": share((dd >= 0.70).astype(float)),
        "h1": share(h1),
        "h1_ci": ci(h1, share),
        "h2": share(h2),
        "h2_ci": ci(h2, share),
        "med": median(ret),
        "med_ci": ci(ret, median),
        "mean": float(ret.mean()),
        "p10": float(np.quantile(ret, 0.1)),
        "p90": float(np.quantile(ret, 0.9)),
        "med_dd": median(dd),
        "delist": sum(r.n_delist for r in res),
        "fees": float(np.mean([r.fees for r in res])),
        "funding": float(np.mean([r.funding for r in res])),
        "holes": sum(r.funding_holes for r in res),
    }


def fmt_ci(x: float, c: tuple[float, float]) -> str:
    return f"{x * 100:5.1f} [{c[0] * 100:5.1f}, {c[1] * 100:5.1f}]"


def row(label: str, s: dict[str, Any]) -> str:
    return (
        f"{label:<34} n={s['n']:3d} liq {s['liq'] * 100:5.1f} DD70 {s['dd70'] * 100:5.1f} | H1 {fmt_ci(s['h1'], s['h1_ci'])} | "
        f"H2 {fmt_ci(s['h2'], s['h2_ci'])} | med {fmt_ci(s['med'], s['med_ci'])} | mean {s['mean'] * 100:6.1f} "
        f"p10 {s['p10'] * 100:6.1f} p90 {s['p90'] * 100:6.1f} medDD {s['med_dd'] * 100:4.0f} | delist {s['delist']} "
        f"fees {s['fees'] * 100:.2f} fund {s['funding'] * 100:.2f}"
    )


def verdict_h1(s: dict[str, Any]) -> str:
    return "✓" if s["h1_ci"][0] >= 0.35 else ("≈" if s["h1"] >= 0.35 else "✗")


def verdict_h2(s: dict[str, Any]) -> str:
    return "✓" if s["h2_ci"][1] <= 0.25 else ("≈" if s["h2"] <= 0.25 else "✗")


def verdict_h3(s: dict[str, Any]) -> str:
    return "✓" if s["med_ci"][1] <= 0 else ("≈" if s["med"] <= 0 else "✗")


def sma_regime(p: Panel, bj: int, d: int) -> int | None:
    cl = p.close[d - 200 : d, bj] if d >= 200 else np.array([])
    if len(cl) < 200 or not np.isfinite(cl).all():
        return None
    return 1 if cl[-1] > cl.mean() else -1


def stage_full(out: Path) -> None:
    p, syms = load(out)
    st = starts(p)
    months = [month_id(s + DAY0) for s in st]
    t0 = time.time()
    uni = Universe(p, syms)
    btc = Universe(p, syms, include={BTC}, k=1)
    print(f"stage0: {rd(out / 'stage0.json')}")
    # sanity: who is in the basket over time
    members: Counter[str] = Counter()
    for d in sorted({s + REBAL * j for s in st for j in range(-(-WINDOW // REBAL))}):
        members.update(syms[i] for i in uni(d))
    print(
        f"distinct basket members over all rebalance days: {len(members)}; most frequent: "
        + ", ".join(f"{s[:-4]} {n}" for s, n in members.most_common(30))
    )
    print(f"basket at first start {ymd(st[0] + DAY0)}: {[syms[i][:-4] for i in uni(st[0])]}")
    print(f"basket at last start {ymd(st[-1] + DAY0)}: {[syms[i][:-4] for i in uni(st[-1])]}")

    results: dict[str, list[WindowResult]] = {}
    summ: dict[str, dict[str, Any]] = {}
    print("\n== pre-registered grid (returns on account capital, %, 95 % CI block bootstrap by month, block 8) ==")
    for slip in (SLIP, 0.0002):
        for mode in ("intraday", "close"):
            for lev in (1.0, 2.0, 3.0):
                key = f"alt L{lev:.0f} {mode} slip{slip * 1e4:.0f}bp"
                results[key] = run_variant(p, uni, lev=lev, mode=mode, slip=slip)
                summ[key] = summary(results[key], months)
                print(row(key, summ[key]), flush=True)
    for mode in ("intraday", "close"):
        key = f"BTC L3 {mode} slip5bp"
        results[key] = run_variant(p, btc, lev=3.0, mode=mode)
        summ[key] = summary(results[key], months)
        print(row(key, summ[key]), flush=True)
    print(f"[{time.time() - t0:.0f}s, peak RSS {rss_mb():.0f} MB]")

    main, low = "alt L3 intraday slip5bp", "alt L3 close slip5bp"
    print("\n== verdicts (registered: main = intraday; close = lower bound) ==")
    for key in (main, low):
        s = summ[key]
        print(
            f"{key}: H1 {verdict_h1(s)}  H2 {verdict_h2(s)}  H3 {verdict_h3(s)}; kill (H1 < 15 %): {s['h1'] < 0.15}; weaken (H1<15 % & H2>40 %): {s['h1'] < 0.15 and s['h2'] > 0.40}"
        )

    print("\n== exploratory: CI block length (months) for the main variant ==")
    for b in (4, 12):
        s = summary(results[main], months, block=b)
        print(f"block {b:2d}: H1 {fmt_ci(s['h1'], s['h1_ci'])} H2 {fmt_ci(s['h2'], s['h2_ci'])} med {fmt_ci(s['med'], s['med_ci'])}")

    print("\n== time split and BTC regime at start (descriptive) ==")
    bj = syms.index(BTC)
    for key in (main, low):
        res = results[key]
        for lab, sel in (
            ("starts < 2024-01-01", [i for i, s in enumerate(st) if s + DAY0 < SPLIT]),
            ("starts >= 2024-01-01", [i for i, s in enumerate(st) if s + DAY0 >= SPLIT]),
            ("BTC > SMA200 at start", [i for i, s in enumerate(st) if sma_regime(p, bj, s) == 1]),
            ("BTC < SMA200 at start", [i for i, s in enumerate(st) if sma_regime(p, bj, s) == -1]),
        ):
            if sel:
                s = summary([res[i] for i in sel], [months[i] for i in sel])
                print(row(f"{key[4:]} {lab}", s))

    print("\n== liquidation timing (main): starts liquidated, by start year ==")
    for key in (main, low):
        by: Counter[str] = Counter()
        tot: Counter[str] = Counter()
        for s, r in zip(st, results[key], strict=True):
            y = ymd(s + DAY0)[:4]
            tot[y] += 1
            by[y] += r.liquidated
        days = [r.liq_day for r in results[key] if r.liquidated]
        print(
            f"{key}: "
            + ", ".join(f"{y} {by[y]}/{tot[y]}" for y in sorted(tot))
            + f"; days to liquidation median {np.median(days) if days else float('nan'):.0f}"
        )

    print("\n== per-asset contribution (main, sum over windows / n windows, % of capital) ==")
    contrib: Counter[str] = Counter()
    appear: Counter[str] = Counter()
    for r in results[main]:
        for i, v in r.contrib.items():
            a = normalize_asset(syms[i][:-4], "binance")[0]
            contrib[a] += v / len(st)
            appear[a] += 1
    ranked = sorted(contrib.items(), key=lambda kv: -kv[1])
    print("top: " + ", ".join(f"{a} {v * 100:+.1f} (in {appear[a]})" for a, v in ranked[:8]))
    print("bottom: " + ", ".join(f"{a} {v * 100:+.1f} (in {appear[a]})" for a, v in ranked[-8:]))
    print(
        f"total {sum(contrib.values()) * 100:+.1f} vs mean return {summ[main]['mean'] * 100:+.1f} (diff = liquidation losses not in legs' P&L)"
    )
    top3 = {a for a, _ in ranked[:3]}
    print(f"\n== exploratory: without the top-3 contributors {sorted(top3)}, and fee 0.10 % ==")
    ex = Universe(p, syms, exclude=top3)
    for mode in ("intraday", "close"):
        print(row(f"ex-top3 L3 {mode}", summary(run_variant(p, ex, lev=3.0, mode=mode), months)))
        print(row(f"fee10bp L3 {mode}", summary(run_variant(p, uni, lev=3.0, mode=mode, fee=0.001), months)))

    print("\n== vault comparison (descriptive, condition 4): 2026-02-17 .. 2026-09-30 ==")
    vs = VAULT_START - DAY0
    vlen = p.close.shape[0] - vs
    lib = Universe(p, syms, include={a + "USDT" for a in LIB_BASKET}, k=len(LIB_BASKET))
    main_rets = [r.ret for r in results[main]]
    low_rets = [r.ret for r in results[low]]
    print(f"rule basket at {ymd(VAULT_START)}: {[syms[i][:-4] for i in uni(vs)]}; LIB basket legs found: {len(lib(vs))}")
    for lab, u in (("rule basket", uni), ("LIB basket", lib)):
        for reb, rl in ((vlen, "buy&hold"), (REBAL, "rebal 30d")):
            for mode in ("intraday", "close"):
                r = simulate_window(p, vs, vlen, u, leverage=3.0, rebalance_every=reb, fee=FEE, slip=SLIP, mm=MM, mode=mode)
                print(f"  {lab:<11} 3x {rl:<9} {mode:<8}: {r.ret * 100:+7.1f} %, maxDD {r.max_dd * 100:4.0f} %, liquidated {r.liquidated}")
    for x in (1.17, 1.45):
        print(
            f"vault +{x * 100:.0f} %: percentile in 8-month windows, L=3 intraday {percentile_of(x, main_rets) * 100:.0f}, close {percentile_of(x, low_rets) * 100:.0f}"
        )

    dump = {
        k: [
            {"start": ymd(s + DAY0), "ret": r.ret, "dd": r.max_dd, "liq": r.liquidated, "liq_day": r.liq_day, "delist": r.n_delist}
            for s, r in zip(st, v, strict=True)
        ]
        for k, v in results.items()
    }
    (out / "windows.json").write_text(json.dumps(dump))
    (out / "summary.json").write_text(json.dumps(summ, default=list))
    print(f"\nwrote {out / 'windows.json'}; done in {time.time() - t0:.0f}s, peak RSS {rss_mb():.0f} MB")


def stage_diag(out: Path) -> None:
    """Sanity of the two numbers that surprised: funding received by the alt longs and the MYX contribution."""
    p, syms = load(out)
    st = starts(p)
    uni = Universe(p, syms)
    nofund = Panel(p.open, p.high, p.low, p.close, p.qv, np.zeros_like(p.fund))
    by: Counter[str] = Counter()
    for s in st:
        a = simulate_window(p, s, WINDOW, uni, leverage=1.0, rebalance_every=REBAL, fee=FEE, slip=SLIP, mm=MM, mode="close")
        b = simulate_window(nofund, s, WINDOW, uni, leverage=1.0, rebalance_every=REBAL, fee=FEE, slip=SLIP, mm=MM, mode="close")
        for i in set(a.contrib) | set(b.contrib):
            by[syms[i]] += (a.contrib.get(i, 0.0) - b.contrib.get(i, 0.0)) / len(st)
    ranked = sorted(by.items(), key=lambda kv: kv[1])
    print("L1 close: funding effect per asset (approx., % of capital per window; + = received)")
    print("  most received: " + ", ".join(f"{s[:-4]} {v * 100:+.2f}" for s, v in ranked[::-1][:10]))
    print("  most paid: " + ", ".join(f"{s[:-4]} {v * 100:+.2f}" for s, v in ranked[:6]))
    print(f"  total {sum(by.values()) * 100:+.2f}")
    for s in [s for s, _ in ranked[::-1][:3]] + ["MYXUSDT", "ETHUSDT"]:
        j = syms.index(s)
        f, c = p.fund[:, j], p.close[:, j]
        ok = np.isfinite(f) & np.isfinite(c)
        days = np.flatnonzero(ok)
        neg = np.sort(f[ok])[:3]
        mo: Counter[str] = Counter()
        for d in days:
            mo[ymd(int(d) + DAY0)[:7]] += float(f[d])
        worst = sorted(mo.items(), key=lambda kv: kv[1])[:3]
        print(
            f"{s}: days {len(days)} {ymd(int(days[0]) + DAY0)}..{ymd(int(days[-1]) + DAY0)}; mean daily funding {np.mean(f[ok]) * 100:+.4f} %; "
            f"3 lowest days {[round(x * 100, 2) for x in neg]} %; most negative months {[(m, round(v * 100, 1)) for m, v in worst]} %; "
            f"close min {np.nanmin(c):.4g} max {np.nanmax(c):.4g}"
        )
    j = syms.index("MYXUSDT")
    c = p.close[:, j]
    print(
        "MYX monthly last close: "
        + ", ".join(f"{ymd(int(d) + DAY0)[:7]} {c[d]:.3g}" for d in np.flatnonzero(np.isfinite(c)) if ymd(int(d) + DAY0)[8:] in ("28",))
    )
    mf = [np.nanmean(p.fund[d - 30 : d, :][:, uni(d)].sum(axis=0)) for d in st]
    print(
        f"basket mean 30d funding sum before each start: median {np.median(mf) * 100:+.2f} %, by year "
        + ", ".join(
            f"{y} {np.median([m for s, m in zip(st, mf, strict=True) if ymd(s + DAY0)[:4] == y]) * 100:+.2f}"
            for y in ("2021", "2022", "2023", "2024", "2025", "2026")
        )
    )


def stage_nonoverlap(out: Path) -> None:
    """Added after the first full run (CI method, not a threshold change): the block bootstrap degenerates to
    [100, 100] when every window is a hit, so also count hits on non-overlapping windows (every 35th weekly start,
    >= 243 days apart) for each of the 35 offsets, with exact Clopper-Pearson CIs."""
    w = rd(out / "windows.json")
    step = -(-WINDOW // 7)  # 35 weekly starts >= one window apart
    for key in (
        "alt L3 intraday slip5bp",
        "alt L3 close slip5bp",
        "alt L2 intraday slip5bp",
        "alt L2 close slip5bp",
        "alt L1 close slip5bp",
        "BTC L3 intraday slip5bp",
        "BTC L3 close slip5bp",
    ):
        rows = w[key]
        h1s, h2s, meds, n_s = [], [], [], []
        for off in range(step):
            sub = rows[off::step]
            n_s.append(len(sub))
            h1s.append(sum(1 for r in sub if r["liq"] or r["dd"] >= 0.70))
            h2s.append(sum(1 for r in sub if r["ret"] >= 1.0))
            meds.append(float(np.median([r["ret"] for r in sub])))
        n = min(n_s)
        k1, k2 = min(h1s), max(h2s)
        print(
            f"{key:<26} n per offset {min(n_s)}-{max(n_s)}; H1 hits min {min(h1s)} max {max(h1s)} (CP 95 % of the worst offset {k1}/{n}: "
            f"[{clopper_pearson(k1, n)[0] * 100:.0f}, {clopper_pearson(k1, n)[1] * 100:.0f}]); H2 hits min {min(h2s)} max {k2} "
            f"(CP of the worst {k2}/{n}: [{clopper_pearson(k2, n)[0] * 100:.0f}, {clopper_pearson(k2, n)[1] * 100:.0f}]); "
            f"median ret over offsets: min {min(meds) * 100:.0f} med {np.median(meds) * 100:.0f} max {max(meds) * 100:.0f} %; "
            f"offsets with median > 0: {sum(m > 0 for m in meds)}/{step}"
        )


def nonoverlap_hits(rows: list[dict[str, Any]]) -> tuple[int, int, int, int, int]:
    """(n, min H1 hits, max H1 hits, min H2 hits, max H2 hits) over the 35 offsets of non-overlapping windows."""
    step = -(-WINDOW // 7)
    subs = [rows[off::step] for off in range(step)]
    h1 = [sum(1 for r in s if r["liq"] or r["dd"] >= 0.70) for s in subs]
    h2 = [sum(1 for r in s if r["ret"] >= 1.0) for s in subs]
    return min(len(s) for s in subs), min(h1), max(h1), min(h2), max(h2)


def stage_bh(out: Path) -> None:
    """Exploratory, added after the critic's review (2026-10-23): the vault replicates a buy-and-hold basket, so
    also give the distribution of the same windows without rebalancing (``rebalance_every = WINDOW``), the
    percentile table for both mechanisms, the dates of the H2 hits, the per-asset P&L over windows that were *not*
    liquidated (``contrib`` stops at the liquidation day and does not carry the forfeited equity) and the starts
    just before the vault's start. Not a registered variant; thresholds unchanged."""
    p, syms = load(out)
    st = starts(p)
    months = [month_id(s + DAY0) for s in st]
    t0 = time.time()
    uni = Universe(p, syms)
    w = rd(out / "windows.json")
    dump: dict[str, list[dict[str, Any]]] = {}
    res: dict[str, list[WindowResult]] = {}
    print("== exploratory: buy-and-hold (no rebalance inside the 243-day window), rule basket ==")
    for mode in ("intraday", "close"):
        for lev in (1.0, 2.0, 3.0):
            key = f"alt L{lev:.0f} {mode} B&H"
            rr = [
                simulate_window(p, s, WINDOW, uni, leverage=lev, rebalance_every=WINDOW, fee=FEE, slip=SLIP, mm=MM, mode=mode) for s in st
            ]
            res[key] = rr
            dump[key] = [
                {"start": ymd(s + DAY0), "ret": r.ret, "dd": r.max_dd, "liq": r.liquidated, "liq_day": r.liq_day}
                for s, r in zip(st, rr, strict=True)
            ]
            n, a1, b1, a2, b2 = nonoverlap_hits(dump[key])
            lo1 = clopper_pearson(a1, n)[0]
            hi2 = clopper_pearson(b2, n)[1]
            print(
                row(key, summary(rr, months))
                + f" | non-overlap n={n}: H1 {a1}-{b1} (CP low of worst {lo1 * 100:.0f}), H2 {a2}-{b2} (CP high of worst {hi2 * 100:.0f})",
                flush=True,
            )
    print(f"[{time.time() - t0:.0f}s, peak RSS {rss_mb():.0f} MB]")

    print("\n== percentile of the vault's +117 % / +145 % among the 222 windows (mid-rank) ==")
    print(f"{'variant':<22} {'rebal 30d':>14} {'buy&hold':>14}")
    for mode in ("intraday", "close"):
        for lev in (1, 2, 3):
            reb = [r["ret"] for r in w[f"alt L{lev} {mode} slip5bp"]]
            bh = [r["ret"] for r in dump[f"alt L{lev} {mode} B&H"]]
            print(
                f"alt L{lev} {mode:<14} "
                + f"{percentile_of(1.17, reb) * 100:6.1f}/{percentile_of(1.45, reb) * 100:5.1f}  "
                + f"{percentile_of(1.17, bh) * 100:6.1f}/{percentile_of(1.45, bh) * 100:5.1f}"
            )

    print("\n== starts of the H2 hits (ret >= +100 %), L=3 ==")
    for key, rows in (
        ("rebal intraday", w["alt L3 intraday slip5bp"]),
        ("rebal close", w["alt L3 close slip5bp"]),
        ("B&H intraday", dump["alt L3 intraday B&H"]),
        ("B&H close", dump["alt L3 close B&H"]),
    ):
        hits = [r["start"] for r in rows if r["ret"] >= 1.0]
        yrs = Counter(h[:4] for h in hits)
        print(f"{key:<15} {len(hits):2d} hits, {hits[0] if hits else '-'} .. {hits[-1] if hits else '-'}; by year {dict(yrs)}")

    print("\n== per-asset P&L over windows NOT liquidated, rebal 30d, L=3 (% of capital, mean over those windows) ==")
    for mode in ("intraday", "close"):
        rr = run_variant(p, uni, lev=3.0, mode=mode)
        alive = [r for r in rr if not r.liquidated]
        cb: Counter[str] = Counter()
        for r in alive:
            for i, v in r.contrib.items():
                cb[normalize_asset(syms[i][:-4], "binance")[0]] += v / len(alive)
        ranked = sorted(cb.items(), key=lambda kv: -kv[1])
        tot = sum(cb.values())
        mean_alive = float(np.mean([r.ret for r in alive])) if alive else float("nan")
        print(f"{mode}: {len(alive)} windows not liquidated; sum of legs {tot * 100:+.1f} vs mean return {mean_alive * 100:+.1f}")
        print("  top: " + ", ".join(f"{a} {v * 100:+.1f}" for a, v in ranked[:6]))
        print("  bottom: " + ", ".join(f"{a} {v * 100:+.1f}" for a, v in ranked[-6:]))
        j = syms.index("MYXUSDT")
        with_myx = [(s, r) for s, r in zip(st, rr, strict=True) if j in r.contrib]
        pos = [ymd(s + DAY0) + f" {r.ret * 100:+.0f}%" for s, r in with_myx if r.ret > 0]
        print(
            f"  MYX (first bar {ymd(int(np.flatnonzero(np.isfinite(p.close[:, j]))[0]) + DAY0)}): in {len(with_myx)} windows, "
            f"liquidated {sum(r.liquidated for _, r in with_myx)}, positive {pos}"
        )
    print(f"[{time.time() - t0:.0f}s, peak RSS {rss_mb():.0f} MB]")

    print("\n== starts just before the vault's start (2026-02-17), L=3 intraday rebal 30d ==")
    for r in w["alt L3 intraday slip5bp"]:
        if r["start"] >= "2025-12-15":
            d = int(datetime.fromisoformat(r["start"]).replace(tzinfo=UTC).timestamp()) // 86400
            when = ymd(d + r["liq_day"]) if r["liq"] else "-"
            print(f"  start {r['start']}: ret {r['ret'] * 100:+.0f} %, liquidated {r['liq']} on {when}")
    print(f"vault window length: {LAST_DAY - VAULT_START + 1} days (registered window {WINDOW})")
    (out / "windows_bh.json").write_text(json.dumps(dump))
    print(f"wrote {out / 'windows_bh.json'}; done in {time.time() - t0:.0f}s, peak RSS {rss_mb():.0f} MB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["panel", "stage0", "full", "diag", "nonoverlap", "bh"])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--out-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/alt_basket_leverage")
    a = ap.parse_args()
    if a.stage == "panel":
        stage_panel(a.cache_dir.expanduser(), a.out_dir.expanduser())
    elif a.stage == "stage0":
        stage0(a.out_dir.expanduser())
    elif a.stage == "diag":
        stage_diag(a.out_dir.expanduser())
    elif a.stage == "nonoverlap":
        stage_nonoverlap(a.out_dir.expanduser())
    elif a.stage == "bh":
        stage_bh(a.out_dir.expanduser())
    else:
        stage_full(a.out_dir.expanduser())


if __name__ == "__main__":
    main()
