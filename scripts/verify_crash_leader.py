#!/usr/bin/env python3
"""Verify IDEA-20261023-crash-relative-strength: after an alt-market crash, do the coins that fell least beat the
market over the next 30 / 90 / 180 days? Binance USDT-M daily bars and funding, 2021-10 ... 2026-09.

No network: reads the dense panel built by ``scripts/verify_alt_basket_leverage.py panel``.

Stages:
    stage0   crash events for every threshold and universe size (dates, depth, universe size) -- no returns
    full     pre-registered H1 (S1 at 90 days, top-50, 30 %), robustness grid, betas, HYPE in Feb 2026

Usage:
    uv run python scripts/verify_crash_leader.py <stage> --panel-dir ~/.cache/tradedesk-agents/alt_basket_leverage
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.crash_leader import CrashEvent, beta, compound_index, detect_events, hold_return, terciles
from tradedesk.analytics.funding_spread import normalize_asset
from tradedesk.analytics.leverage_basket import select_basket

DAY0 = int(datetime(2021, 10, 1, tzinfo=UTC).timestamp()) // 86400
BTC = "BTCUSDT"
LOOKBACK_VOL, LOOKBACK_DD, REARM, MIN_GAP = 30, 90, 0.15, 30
COST = 0.002
MAIN = {"k": 50, "threshold": 0.30, "horizon": 90}
THRESHOLDS, SIZES, HORIZONS = (0.25, 0.30, 0.40), (30, 50, 100), (30, 90, 180)
T_CRIT = 1.9


def ymd(day: int) -> str:
    return datetime.fromtimestamp((DAY0 + day) * 86400, UTC).strftime("%Y-%m-%d")


class Data:
    def __init__(self, panel_dir: Path) -> None:
        z = np.load(panel_dir / "panel.npz")
        self.close, self.qv, self.fund = z["close"], z["qv"], z["fund"]
        self.syms: list[str] = json.loads((panel_dir / "symbols.json").read_text())
        assets = [normalize_asset(s[:-4], "binance")[0] for s in self.syms]
        ids = {a: i for i, a in enumerate(sorted(set(assets)))}
        self.assets = assets
        self.group = np.array([ids[a] for a in assets])
        self.cand = np.array([s != BTC for s in self.syms])
        self.ret = self.close[1:] / self.close[:-1] - 1
        self.ret = np.vstack([np.full((1, self.close.shape[1]), np.nan), self.ret])
        self.memo: dict[tuple[int, int], list[int]] = {}

    def universe(self, d: int, k: int) -> list[int]:
        key = (d, k)
        if key not in self.memo:
            self.memo[key] = select_basket(self.close, self.qv, d, k=k, lookback=LOOKBACK_VOL, candidates=self.cand, group=self.group)
        return self.memo[key]

    def index(self, k: int) -> tuple[np.ndarray, np.ndarray]:
        """(daily EW return of the day's universe, compounded index); days before the first universe are flat."""
        n = self.close.shape[0]
        r = np.full(n, np.nan)
        for d in range(LOOKBACK_VOL, n):
            u = self.universe(d, k)
            v = self.ret[d, u] if u else np.array([])
            v = v[np.isfinite(v)]
            if len(v):
                r[d] = float(v.mean())
        return r, compound_index(r)

    def events(self, k: int, threshold: float) -> list[CrashEvent]:
        _, idx = self.index(k)
        return detect_events(idx, threshold=threshold, lookback=LOOKBACK_DD, rearm=REARM, min_gap=MIN_GAP, first_day=LOOKBACK_VOL + 1)


def event_row(data: Data, ev: CrashEvent, k: int, horizon: int, idx_ret: np.ndarray) -> dict[str, Any] | None:
    t, p = ev.day, ev.peak
    if t + horizon >= data.close.shape[0]:
        return None
    u = data.universe(t, k)
    crash = {
        j: float(data.close[t, j] / data.close[p, j] - 1) for j in u if np.isfinite(data.close[p, j]) and np.isfinite(data.close[t, j])
    }
    strong, weak = terciles(crash)
    fwd = {j: hold_return(data.close, data.fund, j, t, horizon) - COST for j in crash}
    fwd = {j: v for j, v in fwd.items() if math.isfinite(v)}

    def mean(js: list[int]) -> float:
        v = [fwd[j] for j in js if j in fwd]
        return float(np.mean(v)) if v else float("nan")

    def med(js: list[int]) -> float:
        v = [fwd[j] for j in js if j in fwd]
        return float(np.median(v)) if v else float("nan")

    def grp_beta(js: list[int], lo: int, hi: int) -> float:
        if not js:
            return float("nan")
        block = data.ret[lo:hi][:, js]
        y = np.array([np.nan if not np.isfinite(row).any() else float(np.nanmean(row)) for row in block])
        return beta(y, idx_ret[lo:hi])

    allj = list(fwd)
    rank = sorted(crash, key=lambda j: (-crash[j], j))
    return {
        "day": t,
        "peak": p,
        "dd": ev.drawdown,
        "n": len(crash),
        "strong": mean(strong),
        "weak": mean(weak),
        "all": mean(allj),
        "S1": mean(strong) - mean(allj),
        "S2": mean(strong) - mean(weak),
        "S1_med": med(strong) - med(allj),
        # ex-ante beta adjustment: the strong third's beta to the index over the 90 days before the event
        "S1_beta": mean(strong) - grp_beta(strong, max(0, t - 90), t + 1) * mean(allj),
        "crash_strong": float(np.mean([crash[j] for j in strong])),
        "crash_weak": float(np.mean([crash[j] for j in weak])),
        "beta_pre": (grp_beta(strong, max(0, t - 90), t + 1), grp_beta(weak, max(0, t - 90), t + 1)),
        "beta_post": (grp_beta(strong, t + 1, t + 1 + horizon), grp_beta(weak, t + 1, t + 1 + horizon)),
        "top": [(data.assets[j], crash[j], fwd.get(j, float("nan"))) for j in rank[:5]],
        "rank_of": {data.assets[j]: (i + 1, len(rank), crash[j], fwd.get(j, float("nan"))) for i, j in enumerate(rank)},
    }


def summarize(rows: list[dict[str, Any]], key: str = "S1") -> dict[str, float]:
    s = np.array([r[key] for r in rows if math.isfinite(r[key])])
    if len(s) == 0:
        return {"n": 0, "mean": float("nan"), "t": float("nan"), "pos": float("nan"), "median": float("nan")}
    sd = float(s.std(ddof=1)) if len(s) > 1 else float("nan")
    return {
        "n": len(s),
        "mean": float(s.mean()),
        "median": float(np.median(s)),
        "sd": sd,
        "t": float(s.mean() / (sd / math.sqrt(len(s)))) if len(s) > 1 and sd > 0 else float("nan"),
        "pos": float((s > 0).mean()),
        "mde": 2.8 * sd / math.sqrt(len(s)) if len(s) > 1 else float("nan"),
    }


def verdict(s: dict[str, float]) -> str:
    if s["n"] == 0:
        return "◯"
    if s["mean"] <= 0 or s["pos"] < 0.5:
        return "✗"
    if s["t"] >= T_CRIT and s["pos"] >= 0.7:
        return "✓"
    if s["pos"] >= 0.6:
        return "≈"
    return "◯"


def stage0(data: Data) -> None:
    for k in SIZES:
        for th in THRESHOLDS:
            evs = data.events(k, th)
            print(f"top-{k} threshold {th:.0%}: {len(evs)} events")
            for ev in evs:
                print(
                    f"    {ymd(ev.day)} (peak {ymd(ev.peak)}, {ev.day - ev.peak} d) dd {ev.drawdown:+.1%}, universe {len(data.universe(ev.day, k))}"
                )


def stage_full(data: Data) -> None:
    k, th, h = MAIN["k"], MAIN["threshold"], MAIN["horizon"]
    idx_ret, _ = data.index(k)
    rows = [r for ev in data.events(k, th) if (r := event_row(data, ev, k, h, idx_ret))]
    print(f"== main: top-{k}, threshold {th:.0%}, horizon {h} d, cost {COST:.1%} per round trip, funding included ==")
    for r in rows:
        print(
            f"  {ymd(r['day'])} dd {r['dd']:+.0%} n {r['n']:3d} | crash strong {r['crash_strong']:+.0%} weak {r['crash_weak']:+.0%} | "
            f"fwd strong {r['strong']:+7.1%} weak {r['weak']:+7.1%} all {r['all']:+7.1%} | S1 {r['S1']:+7.1%} S2 {r['S2']:+7.1%} S1med {r['S1_med']:+7.1%} S1beta {r['S1_beta']:+7.1%} | "
            f"beta pre s/w {r['beta_pre'][0]:.2f}/{r['beta_pre'][1]:.2f} post {r['beta_post'][0]:.2f}/{r['beta_post'][1]:.2f}"
        )
        print("      strongest in the crash: " + ", ".join(f"{a} {c:+.0%}→{f:+.0%}" for a, c, f in r["top"]))
    for key in ("S1", "S2", "S1_med", "S1_beta"):
        s = summarize(rows, key)
        print(
            f"  {key:6s} n {s['n']} mean {s['mean']:+.1%} median {s['median']:+.1%} sd {s.get('sd', float('nan')):.1%} t {s['t']:+.2f} positive {s['pos']:.0%} MDE {s.get('mde', float('nan')):.1%}"
        )
    main = summarize(rows, "S1")
    print(f"  H1 verdict (S1, pre-registered): {verdict(main)}")

    print("\n== robustness grid (descriptive): S1 mean / t / positive share, n events ==")
    for kk in SIZES:
        ir, _ = data.index(kk)
        for tt in THRESHOLDS:
            evs = data.events(kk, tt)
            cells = []
            for hh in HORIZONS:
                rr = [r for ev in evs if (r := event_row(data, ev, kk, hh, ir))]
                s1, s2, sb = summarize(rr, "S1"), summarize(rr, "S2"), summarize(rr, "S1_beta")
                cells.append(
                    f"H{hh}: S1 {s1['mean']:+6.1%} t {s1['t']:+5.2f} {s1['pos']:.0%} | S2 {s2['mean']:+6.1%} | S1β {sb['mean']:+6.1%} t {sb['t']:+5.2f} (n {s1['n']})"
                )
            print(f"  top-{kk:3d} {tt:.0%}: " + " ;  ".join(cells))

    print("\n== kill criterion: S1 at 90 d, top-50, by threshold ==")
    neg = 0
    for tt in THRESHOLDS:
        rr = [r for ev in data.events(k, tt) if (r := event_row(data, ev, k, h, idx_ret))]
        s = summarize(rr, "S1")
        neg += s["mean"] <= 0
        print(f"  {tt:.0%}: mean {s['mean']:+.1%} (n {s['n']})")
    print(f"  kill (main ≤ 0 and ≥ 2 of 3 thresholds ≤ 0): {main['mean'] <= 0 and neg >= 2}")

    print("\n== Feb 2026 crash (not an event under the re-arm rule; descriptive, ranking at the first close <= -30 %) ==")
    _, idx = data.index(k)
    feb = [
        e
        for e in detect_events(idx, threshold=th, lookback=LOOKBACK_DD, rearm=1.0, min_gap=1, first_day=LOOKBACK_VOL + 1)
        if "2026-01-15" <= ymd(e.day) <= "2026-02-28"
    ]
    extra = [r for ev in feb[:1] if (r := event_row(data, ev, k, h, idx_ret))]
    for r in extra:
        print(
            f"  {ymd(r['day'])} (peak {ymd(r['peak'])}) dd {r['dd']:+.0%}: fwd strong {r['strong']:+.1%} weak {r['weak']:+.1%} all {r['all']:+.1%} | "
            f"S1 {r['S1']:+.1%} S2 {r['S2']:+.1%} S1beta {r['S1_beta']:+.1%}"
        )
        print("      strongest in the crash: " + ", ".join(f"{a} {c:+.0%}→{f:+.0%}" for a, c, f in r["top"]))
        for a in ("HYPE", "NEAR", "AAVE", "ZRO", "ENA", "LINK", "UNI", "ONDO"):
            if a in r["rank_of"]:
                i, n, c, f = r["rank_of"][a]
                print(f"      {a}: crash rank {i}/{n} ({c:+.0%}), fwd {h} d {f:+.0%}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("stage", choices=("stage0", "full"))
    ap.add_argument("--panel-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/alt_basket_leverage")
    args = ap.parse_args()
    data = Data(args.panel_dir)
    {"stage0": stage0, "full": stage_full}[args.stage](data)


if __name__ == "__main__":
    main()
