#!/usr/bin/env python3
"""Verify IDEA-20261023-post-capitulation-entry: is buying the equal-weight alt basket right after a >= 30 % crash
better than buying it on any day? Binance USDT-M daily bars and funding, 2021-10 ... 2026-09; no network.

Reuses the panel, universe, index and crash events of ``scripts/verify_crash_leader.py``.

Usage:
    uv run python scripts/verify_post_capitulation.py --panel-dir ~/.cache/tradedesk-agents/alt_basket_leverage
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_crash_leader import COST, LOOKBACK_VOL, THRESHOLDS, Data, ymd  # noqa: E402

from tradedesk.analytics.crash_leader import CrashEvent, basket_path  # noqa: E402

K, MAIN_TH, MAIN_H = 50, 0.30, 90
HORIZONS = (30, 90, 180)
TARGET_SHARE, TARGET_MAX = 0.70, 365
N_DRAWS, SEED = 20_000, 11


def trade(data: Data, day: int, horizon: int) -> np.ndarray:
    return basket_path(data.close, data.fund, data.universe(day, K), day, horizon)


def baseline(data: Data, horizon: int) -> tuple[list[int], np.ndarray]:
    n = data.close.shape[0]
    days = list(range(LOOKBACK_VOL + 1, n - horizon))
    return days, np.array([trade(data, d, horizon)[-1] - 1 - COST for d in days])


def percentile_of_mean(events: np.ndarray, base: np.ndarray, rng: np.random.Generator) -> float:
    draws = rng.choice(base, size=(N_DRAWS, len(events)), replace=True).mean(axis=1)
    m = events.mean()
    return float(((draws < m).sum() + 0.5 * (draws == m).sum()) / N_DRAWS * 100)


def verdict(ev_mean: float, base_mean: float, pct: float) -> str:
    if ev_mean <= base_mean:
        return "✗"
    return "✓" if pct >= 95 and ev_mean > 0 else "≈"


def funding_flush(data: Data, day: int) -> float:
    """Mean over the day's universe of the summed daily funding on days day-2 … day (negative = shorts pay)."""
    u = data.universe(day, K)
    v = np.nansum(data.fund[day - 2 : day + 1][:, u], axis=0)
    return float(np.mean(v)) if len(u) else float("nan")


def report_events(data: Data, evs: list[CrashEvent], horizon: int, base: np.ndarray, rng: np.random.Generator, label: str) -> str:
    n = data.close.shape[0]
    rets = np.array([trade(data, e.day, horizon)[-1] - 1 - COST for e in evs if e.day + horizon < n])
    if len(rets) == 0:
        return f"  {label}: no events"
    pct = percentile_of_mean(rets, base, rng)
    v = verdict(rets.mean(), base.mean(), pct)
    return (
        f"  {label}: n {len(rets)} mean {rets.mean():+.1%} median {np.median(rets):+.1%} positive {np.mean(rets > 0):.0%} | "
        f"base mean {base.mean():+.1%} median {np.median(base):+.1%} positive {np.mean(base > 0):.0%} | percentile {pct:.0f} → {v}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--panel-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/alt_basket_leverage")
    args = ap.parse_args()
    data = Data(args.panel_dir)
    rng = np.random.default_rng(SEED)
    n = data.close.shape[0]
    bases = {h: baseline(data, h) for h in HORIZONS}
    print(f"baseline entries: {len(bases[90][0])} days {ymd(bases[90][0][0])} … {ymd(bases[90][0][-1])} (90 d horizon)")

    evs = data.events(K, MAIN_TH)
    print(f"\n== main: top-{K}, threshold {MAIN_TH:.0%}, {MAIN_H} d, 1x, cost {COST:.1%} + funding ==")
    for e in evs:
        if e.day + MAIN_H < n:
            path = trade(data, e.day, MAIN_H)
            print(
                f"  {ymd(e.day)} dd {e.drawdown:+.0%}: 90 d {path[-1] - 1 - COST:+6.1%}, worst {path.min() - 1:+6.1%}, "
                f"funding flush {funding_flush(data, e.day):+.3%}"
            )
    print(report_events(data, evs, MAIN_H, bases[MAIN_H][1], rng, "H1 (pre-registered)"))

    print("\n== grid (descriptive) ==")
    for th in THRESHOLDS:
        e_th = data.events(K, th)
        for h in HORIZONS:
            print(report_events(data, e_th, h, bases[h][1], rng, f"threshold {th:.0%}, {h:3d} d"))
    flush = [e for e in evs if funding_flush(data, e.day) < 0]
    print(report_events(data, flush, MAIN_H, bases[MAIN_H][1], rng, "funding-flush filter, 30 %, 90 d"))

    print(f"\n== author's exit: {TARGET_SHARE:.0%} of the way back to the pre-crash peak, else {TARGET_MAX} d (1x) ==")
    _, idx = data.index(K)
    for th in THRESHOLDS:
        out = []
        for e in data.events(K, th):
            target = TARGET_SHARE * (idx[e.peak] / idx[e.day] - 1)
            path = trade(data, e.day, TARGET_MAX)
            hit = np.flatnonzero(path - 1 >= target)
            day_out = int(hit[0]) if len(hit) else len(path) - 1
            full = len(hit) > 0 or e.day + TARGET_MAX < n
            out.append((e, target, len(hit) > 0, day_out, path[day_out] - 1 - COST, path[: day_out + 1].min() - 1, full))
        done = [o for o in out if o[6]]
        print(
            f"  threshold {th:.0%}: {len(done)} complete trades, target hit {sum(o[2] for o in done)}, "
            f"mean {np.mean([o[4] for o in done]):+.1%}, median {np.median([o[4] for o in done]):+.1%}"
        )
        for e, target, hit, d_out, r, worst, full in out:
            print(
                f"    {ymd(e.day)} target {target:+.0%}: {'HIT' if hit else 'no '} after {d_out:3d} d, return {r:+6.1%}, worst {worst:+6.1%}"
                + ("" if full else "  (data end, open)")
            )

    print("\n== leverage without rebalancing, 90 d: share of trades whose basket close touched 1 - 1/L ==")
    for lev in (2.0, 3.0):
        ev_paths = [trade(data, e.day, MAIN_H) for e in evs if e.day + MAIN_H < n]
        liq_ev = np.mean([p.min() <= 1 - 1 / lev for p in ev_paths])
        days = bases[MAIN_H][0][::7]
        liq_base = np.mean([trade(data, d, MAIN_H).min() <= 1 - 1 / lev for d in days])
        print(f"  {lev:.0f}x: after crashes {liq_ev:.0%} (n {len(ev_paths)}), any week {liq_base:.0%} (n {len(days)})")


if __name__ == "__main__":
    main()
