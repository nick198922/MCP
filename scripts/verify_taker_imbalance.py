"""Verify IDEA-20261028-taker-imbalance-8h-continuation (docs/ideas/...).

Stages (``python scripts/verify_taker_imbalance.py <stage>``):
    universe     fixed universe from cached klines (volume and coverage only, no returns) -> universe.json
    fetch        archive ``metrics`` daily files of the universe -> compact per-file JSON cache (taker ratio only)
    semantics    BTCUSDT metrics taker ratio vs 1m kline taker volumes, 2024-03-02..05 and 2026-09-20 (no returns)
    stage0       coverage by quarter and coin, power gates G1-G3, leak detector (no forward returns)
    full         registered run (H1-H3, placebo, baskets, controls, permutation, IS/OOS, by year) -> --out

Inputs reused from earlier work (read-only): 5m klines ``liquidation_flush/k5m`` (``[open_ms, o, h, l, c, base_vol,
quote_vol]``, 2021-11..2026-09), daily klines and funding of ``funding_crowding``. Output under ``--cache-dir``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import resource
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_liquidation_flush as vlf  # noqa: E402  (per-file JSON Fetcher)

from tradedesk.analytics import taker_imbalance as ta  # noqa: E402
from tradedesk.analytics.oi_flush import SEMANTICS_SWITCH_MS, archive_obs_times  # noqa: E402
from tradedesk.analytics.per_asset import heterogeneity, permutation_q, persistence  # noqa: E402
from tradedesk.analytics.xs_factors import ic_products, rank_ic, without_top  # noqa: E402
from tradedesk.server import build_server  # noqa: E402

AG = Path.home() / ".cache/tradedesk-agents"
K5M = AG / "liquidation_flush/k5m"
K1D = AG / "funding_crowding/k1d"
FUND = AG / "funding_crowding/funding"
FIRST_DAY = "2021-12-01"  # metrics archive starts 2021-12 for most alts (SOLUSDT 2021-12-01)
LAST_DAY = "2026-09-30"  # last month of the cached 5m klines
N_UNIVERSE = 28
STABLE = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT", "EURUSDT", "BTCDOMUSDT", "DEFIUSDT"}


def rss() -> str:
    return f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def days(a: str, b: str) -> list[str]:
    d0, d1 = datetime.fromisoformat(a), datetime.fromisoformat(b)
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days + 1)]


def months(a: str, b: str) -> list[str]:
    out, y, m = [], int(a[:4]), int(a[5:7])
    while f"{y:04d}-{m:02d}" <= b[:7]:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---------------------------------------------------------------- universe (no returns)


def stage_universe(cache: Path) -> None:
    need = set(months("2021-11", LAST_DAY))
    vol: dict[str, float] = {}
    for d in sorted(K1D.iterdir()):
        s = d.name
        if not s.endswith("USDT") or s in STABLE or s == "BTCUSDT":
            continue
        have = {p.stem for p in d.glob("*.json")}
        fund = {p.stem for p in (FUND / s).glob("*.json")} if (FUND / s).is_dir() else set()
        if not need <= have or not (need - {"2021-11"}) <= fund:
            continue
        tot = 0.0
        for m in months("2022-01", "2023-12"):
            p = K1D / s / f"{m}.json"
            rows = rd(p) if p.exists() else None
            tot += sum(r[6] for r in rows or [])
        vol[s] = tot
    ranked = sorted(vol, key=lambda s: -vol[s])
    uni = ranked[:N_UNIVERSE]
    out = {
        "rule": f"USDT-M perps with cached daily klines 2021-11..{LAST_DAY[:7]} and funding 2021-12.., no stablecoins/indices, "
        f"no BTC (hedge); top {N_UNIVERSE} by quote volume 2022-01..2023-12 (daily klines)",
        "universe": uni,
        "quote_vol_2022_23_busd": {s: round(vol[s] / 1e9, 1) for s in uni},
        "n_candidates": len(vol),
        "next": ranked[N_UNIVERSE : N_UNIVERSE + 5],
    }
    (cache / "universe.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


# ---------------------------------------------------------------- fetch


def compact(rows: list[list[float]]) -> dict[str, Any]:
    """Keep only create_time and sum_taker_long_short_vol_ratio (index 6); NaN -> None."""
    out = [[int(r[0]), (None if r[6] != r[6] else r[6])] for r in rows]
    return {"n_rows": len(rows), "rows": out}


async def stage_fetch(cache: Path, syms: list[str], first: str, last: str, sample: int | None) -> None:
    ds = days(first, last)
    if sample:
        ds = ds[:: max(1, len(ds) // sample)]
    _, rt = build_server()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        fx = vlf.Fetcher(rt, cache, workers=24)
        kjobs = []
        for s in [*syms, "BTCUSDT"]:
            for m in months("2021-11", last):
                p = cache / "k5m" / s / f"{m}.json"
                old = K5M / s / f"{m}.json"
                if not p.exists() and old.exists():
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(old.read_bytes())

                async def gk(s: str = s, m: str = m) -> Any:
                    return (await rt.binance_vision.um_klines_monthly(s, "5m", int(m[:4]), int(m[5:]))).data

                kjobs.append((p, gk))
        await fx.many("k5m", kjobs)
        print(f"klines done; {rss()}", flush=True)
        jobs = []
        for d in ds:  # date-major so a partial run covers every coin up to some date
            for s in syms:

                async def go(s: str = s, d: str = d) -> Any:
                    return compact((await rt.binance_vision.um_metrics_ratios_daily(s, d)).data)

                jobs.append((cache / "metrics" / s / f"{d}.json", go))
        t0 = time.time()
        await fx.many("metrics", jobs)
        print(f"FETCH COMPLETE {len(jobs)} files in {time.time() - t0:.0f}s; {rss()}", flush=True)
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- semantics (no returns)


async def stage_semantics(cache: Path) -> None:
    """Match metrics taker ratio rows against buy/sell taker volume of 1m klines over candidate 5-min windows."""
    _, rt = build_server()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    res: dict[str, Any] = {}
    try:
        k1m: dict[int, list[float]] = {}
        for y, m in ((2024, 2), (2024, 3), (2026, 9)):
            rows = (await rt.binance_vision.um_klines_taker_monthly("BTCUSDT", "1m", y, m)).data
            for r in rows:
                k1m[int(r[0])] = [r[5], r[7]]  # base volume, taker-buy base volume
        for d in ("2024-03-02", "2024-03-03", "2024-03-04", "2024-03-05", "2026-09-20"):
            rows = (await rt.binance_vision.um_metrics_ratios_daily("BTCUSDT", d)).data
            out = {}
            for off in (-10, -5, 0, 5):  # window [ct + off, ct + off + 5 min)
                errs, n = [], 0
                for r in rows:
                    ct, ratio = int(r[0]), r[6]
                    if ratio != ratio:
                        continue
                    ks = [ct + (off + j) * 60_000 for j in range(5)]
                    if not all(k in k1m for k in ks):
                        continue
                    v = sum(k1m[k][0] for k in ks)
                    b = sum(k1m[k][1] for k in ks)
                    if v - b <= 0:
                        continue
                    n += 1
                    errs.append(abs(ratio / (b / (v - b)) - 1.0))
                e = np.asarray(errs)
                out[f"win[ct{off:+d}m,ct{off + 5:+d}m)"] = {
                    "n": n,
                    "median_rel_err": float(np.median(e)) if n else None,
                    "share_within_0.1pct": float(np.mean(e < 1e-3)) if n else None,
                }
            # also: inverse definition sell/buy for the best window
            res[d] = out
            print(d, json.dumps(out), flush=True)
    finally:
        await rt.aclose()
    (cache / "semantics.json").write_text(json.dumps(res, indent=1))


# ---------------------------------------------------------------- panel


T0 = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
T1 = int(datetime(2026, 9, 30, 16, tzinfo=UTC).timestamp() * 1000)
WIN_MS = 8 * ta.H_MS
GRID0 = int(datetime(2021, 11, 1, tzinfo=UTC).timestamp() * 1000)
MIN_N = 15  # coins per window for an IC
BLOCK = 12  # 4 days of 8h windows
SPLIT = int(datetime(2024, 6, 1, tzinfo=UTC).timestamp() * 1000)  # registered after stage0 (condition 1)


def load_metrics(cache: Path, s: str) -> tuple[np.ndarray, np.ndarray, Counter[str], Counter[str]]:
    """(obs_ms, ratio) sorted, plus per-quarter counts of finite rows and of days with a file."""
    ct, rr, fin, nday = [], [], Counter(), Counter()
    for p in sorted((cache / "metrics" / s).glob("*.json")):
        v = rd(p)
        q = f"{p.stem[:4]}Q{(int(p.stem[5:7]) - 1) // 3 + 1}"
        if not v:
            continue
        nday[q] += 1
        for c, r in v["rows"]:
            ct.append(c)
            rr.append(np.nan if r is None else r)
            fin[q] += r is not None
    c = np.asarray(ct, dtype=np.int64)
    o = np.argsort(c, kind="stable")
    c, r = c[o], np.asarray(rr, dtype=float)[o]
    keep = np.concatenate([[True], np.diff(c) > 0]) if c.size else np.zeros(0, bool)
    return archive_obs_times(c[keep]), r[keep], fin, nday


def load_k5m(cache: Path, s: str) -> dict[str, np.ndarray]:
    """Dense 5m grid from GRID0: open, high, low, close, quote volume (NaN where a bar is missing)."""
    n = int((T1 + 2 * WIN_MS - GRID0) // ta.M5_MS) + 1
    g = {k: np.full(n, np.nan) for k in ("o", "h", "l", "c", "qv")}
    for p in sorted((cache / "k5m" / s).glob("*.json")):
        rows = rd(p)
        if not rows:
            continue
        a = np.asarray(rows, dtype=float)
        i = ((a[:, 0] - GRID0) // ta.M5_MS).astype(np.int64)
        ok = (i >= 0) & (i < n)
        for k, col in (("o", 1), ("h", 2), ("l", 3), ("c", 4), ("qv", 6)):
            g[k][i[ok]] = a[ok, col]
    return g


def load_funding(s: str) -> tuple[np.ndarray, np.ndarray]:
    rows: list[list[float]] = []
    for p in sorted((FUND / s).glob("*.json")) if (FUND / s).is_dir() else []:
        rows += rd(p) or []
    a = np.asarray(sorted(rows), dtype=float).reshape(-1, 3)
    return a[:, 0].astype(np.int64), a[:, 2]


def gi(t_ms: int | np.ndarray) -> Any:
    return (np.asarray(t_ms) - GRID0) // ta.M5_MS


def coin_panel(cache: Path, s: str, ts: np.ndarray, btc_hr: np.ndarray | None) -> dict[str, Any]:
    obs, ratio, fin, nday = load_metrics(cache, s)
    imb = ta.imbalance(ratio)
    g = load_k5m(cache, s)
    # quote volume of the 5m kline covering each metrics row's interval [obs - 5m, obs)
    j = gi(obs - ta.M5_MS)
    w = np.full(obs.size, np.nan)
    ok = (j >= 0) & (j < g["qv"].size)
    w[ok] = g["qv"][j[ok]]
    minute = (obs - ta.M5_MS) // 60_000 % 60
    qh = np.where(minute % 15 == 0, imb, np.nan)  # exploratory: bars starting on a quarter hour only
    raw = np.array([ta.window_mean(obs, imb, int(t)) for t in ts])
    raw_w = np.array([ta.window_mean(obs, imb, int(t), weights=w) for t in ts])
    raw_qh = np.array([ta.window_mean(obs, qh, int(t), min_bars=12) for t in ts])
    raw_lead = np.array([ta.window_mean(obs, imb, int(t) + ta.M5_MS) for t in ts])  # leak detector: +5 min
    o, h, lo, c = g["o"], g["h"], g["l"], g["c"]
    it = gi(ts)
    e0, e1 = it + 1, it + 1 + WIN_MS // ta.M5_MS  # entry bar t+5m, exit bar t+8h+5m
    out: dict[str, Any] = {
        "raw": raw,
        "S": ta.standardize_prior(raw),
        "Sw": ta.standardize_prior(raw_w),
        "Sqh": ta.standardize_prior(raw_qh),
        "S_lead": ta.standardize_prior(raw_lead),
        "r4h": o[it] / o[it - 48] - 1.0,
        "bar_after": c[it] / o[it] - 1.0,  # bar [t, t+5m): leak detector only
        "bar_before": c[it - 1] / o[it - 1] - 1.0,
        "r8": o[e1] / o[e0] - 1.0,
        "r4": o[e0 + 48] / o[e0] - 1.0,
        "r12": o[e0 + 144] / o[e0] - 1.0,
        "bars": np.stack([o[e0], h[e0], lo[e0], c[e0], o[e1], h[e1], lo[e1], c[e1]], axis=1),
        "fin": fin,
        "nday": nday,
    }
    hr = o[::12]  # hourly open prices from GRID0
    hret = hr[1:] / hr[:-1] - 1.0
    out["hret"] = hret
    if btc_hr is not None:
        at = ((ts - GRID0) // ta.H_MS).astype(int)
        out["beta"] = ta.rolling_beta(hret, btc_hr, at, window=1440, min_obs=1000)
    fc_, fr = load_funding(s)
    out["fund"] = np.array([ta.funding_sum(fc_, fr, int(t) + ta.M5_MS, int(t) + WIN_MS + ta.M5_MS) for t in ts])
    return out


def panel_path(cache: Path, uni: list[str]) -> Path:
    """Panel cache file keyed by coin count and a hash of the coin set (order-free)."""
    tag = hashlib.sha256(",".join(sorted(uni)).encode()).hexdigest()[:10]
    return cache / f"panel_{len(uni)}_{tag}.npz"


def build_panel(cache: Path, uni: list[str]) -> dict[str, Any]:
    p = panel_path(cache, uni)
    ts = np.arange(T0, T1 + 1, WIN_MS, dtype=np.int64)
    # Legacy panel_<n>.npz files (keyed by count only) are deliberately not read: they may be partial.
    if p.exists():
        z = np.load(p, allow_pickle=True)
        meta = json.loads(str(z["meta_json"]))
        if sorted(meta.get("uni", [])) == sorted(uni):
            return {k: z[k] for k in z.files} | {"meta": meta}
        print(f"panel cache {p.name}: coin set differs, rebuilding", flush=True)
    t0 = time.time()
    g = load_k5m(cache, "BTCUSDT")
    bo = g["o"]
    btc_hr = bo[::12][1:] / bo[::12][:-1] - 1.0
    it = gi(ts)
    btc = {
        "r8": bo[it + 1 + 96] / bo[it + 1] - 1.0,
        "r4": bo[it + 1 + 48] / bo[it + 1] - 1.0,
        "r12": bo[it + 1 + 144] / bo[it + 1] - 1.0,
    }
    bfc, bfr = load_funding("BTCUSDT")
    btc_f = np.array([ta.funding_sum(bfc, bfr, int(t) + ta.M5_MS, int(t) + WIN_MS + ta.M5_MS) for t in ts])
    keys = ("raw", "S", "Sw", "Sqh", "S_lead", "r4h", "bar_after", "bar_before", "r8", "r4", "r12", "beta", "fund")
    mats: dict[str, Any] = {k: np.full((ts.size, len(uni)), np.nan) for k in keys}
    mats["bars"] = np.full((ts.size, len(uni), 8), np.nan)
    cov: dict[str, Any] = {}
    for j, s in enumerate(uni):
        cp = coin_panel(cache, s, ts, btc_hr)
        for k in keys:
            mats[k][:, j] = cp[k]
        mats["bars"][:, j, :] = cp["bars"]
        cov[s] = {q: [cp["fin"][q], cp["nday"][q]] for q in sorted(cp["nday"])}
        print(f"  panel {j + 1}/{len(uni)} {s} {time.time() - t0:.0f}s {rss()}", flush=True)
    meta = {"uni": uni, "cov": cov}
    np.savez(p, ts=ts, btc_r8=btc["r8"], btc_r4=btc["r4"], btc_r12=btc["r12"], btc_f=btc_f, meta_json=json.dumps(meta), **mats)
    return build_panel(cache, uni)


def mean_ic(sig: np.ndarray, ret: np.ndarray, rows: np.ndarray | None = None) -> np.ndarray:
    """Per-window Spearman IC over coins with both values finite (NaN with fewer than MIN_N coins)."""
    out = np.full(sig.shape[0], np.nan)
    for k in range(sig.shape[0]) if rows is None else rows:
        ok = np.isfinite(sig[k]) & np.isfinite(ret[k])
        if ok.sum() >= MIN_N:
            out[k] = rank_ic(sig[k, ok], ret[k, ok])
    return out


def ic_summary(ic: np.ndarray, ts: np.ndarray) -> dict[str, Any]:
    def one(m: np.ndarray) -> dict[str, Any]:
        x = ic[m & np.isfinite(ic)]
        if x.size < 30:
            return {"n": int(x.size)}
        lo, hi = ta.block_bootstrap_mean_ci(x, block=BLOCK)
        return {"n": int(x.size), "mean": float(x.mean()), "ci": [lo, hi], "sd": float(x.std(ddof=1))}

    allm = np.ones(ts.size, bool)
    return {"all": one(allm), "IS": one(ts < SPLIT), "OOS": one(ts >= SPLIT)}


# ---------------------------------------------------------------- stage 0 (no forward returns)


def stage0(cache: Path, uni: list[str], out: Path) -> None:
    P = build_panel(cache, uni)
    ts, S = P["ts"], P["S"]
    cov = P["meta"]["cov"]
    quarters = sorted({q for s in cov for q in cov[s]})
    print("coverage of sum_taker_long_short_vol_ratio: share of 288 rows/day finite; coins with >= 90 %")
    qtab = {}
    for q in quarters:
        shares = []
        for s in uni:
            fin, nd = cov[s].get(q, [0, 0])
            y, qq = int(q[:4]), int(q[-1])
            ndays = (datetime(y + (qq == 4), 1 if qq == 4 else 3 * qq + 1, 1) - datetime(y, 3 * qq - 2, 1)).days
            if q == "2026Q3":
                ndays = 92
            shares.append(fin / (288 * ndays))
        a = np.asarray(shares)
        qtab[q] = {"median": float(np.median(a)), "min": float(a.min()), "n_ge90": int((a >= 0.9).sum())}
        print(f"  {q}: median {np.median(a):.3f} min {a.min():.3f} coins>=90% {int((a >= 0.9).sum())}/{len(uni)}")
    nwin = np.isfinite(S).sum(axis=1)
    valid = nwin >= MIN_N
    g1 = float(nwin[valid].mean()) if valid.any() else 0.0
    day = ts // 86_400_000
    vdays = len({int(d) for d, v in zip(day, nwin >= 25, strict=True) if v})
    g3 = float(np.sqrt(np.mean(1.0 / (nwin[valid] - 1)))) if valid.any() else float("nan")
    leak = {}
    for name, sig in (("S rule (obs <= t)", S), ("S shifted +5 min (obs <= t+5m)", P["S_lead"])):
        for bar in ("bar_before", "bar_after"):
            ic = mean_ic(sig, P[bar])
            leak[f"{name} vs {bar}"] = float(np.nanmean(ic))
    by_regime = {}
    for name, m in (("old (<2024-03-04)", ts < ta_switch()), ("new (>=2024-03-04)", ts >= ta_switch())):
        a = mean_ic(S[m], P["bar_after"][m])
        b = mean_ic(P["S_lead"][m], P["bar_after"][m])
        by_regime[name] = {"rule_vs_bar_after": float(np.nanmean(a)), "lead_vs_bar_after": float(np.nanmean(b))}
    res = {
        "windows": int(ts.size),
        "windows_valid": int(valid.sum()),
        "G1_mean_coins_per_window": g1,
        "G1_pass": g1 >= 25,
        "G2_days_with_ge25_coins": vdays,
        "G2_pass": vdays >= 1000,
        "G3_sd_ic_per_window_analytic": g3,
        "G3_pass": g3 <= 0.22,
        "first_valid_window": str(datetime.fromtimestamp(int(ts[valid][0]) / 1000, UTC)) if valid.any() else None,
        "coins_per_window_by_year": {
            str(y): float(
                nwin[
                    (day >= (datetime(y, 1, 1, tzinfo=UTC).timestamp() // 86400))
                    & (day < (datetime(y + 1, 1, 1, tzinfo=UTC).timestamp() // 86400))
                ].mean()
            )
            for y in range(2022, 2027)
        },
        "leak_detector_mean_ic": leak,
        "leak_by_regime": by_regime,
        "coverage_by_quarter": qtab,
    }
    out.write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k != "coverage_by_quarter"}, indent=1))
    print(rss())


def ta_switch() -> int:
    return int(SEMANTICS_SWITCH_MS)


# ---------------------------------------------------------------- full run


def basket(P: dict[str, Any], sig: np.ndarray, c: ta.Costs, k: int = 3, rkey: str = "r8") -> dict[str, np.ndarray]:
    """Per-window gross / E1 net / E2 net of long top-k / short bottom-k, BTC-hedged, on $1 per side notional."""
    ts = P["ts"]
    n = ts.size
    out = {x: np.full(n, np.nan) for x in ("gross", "e1", "e2", "fund", "hedge_cost", "beta_net")}
    out["legs"] = np.empty(n, dtype=object)
    R = P[rkey] - P["beta"] * P[f"btc_{rkey}"][:, None]
    for t in range(n):
        ok = np.isfinite(sig[t]) & np.isfinite(R[t]) & np.isfinite(P["beta"][t]) & np.all(np.isfinite(P["bars"][t]), axis=1)
        if ok.sum() < MIN_N:
            continue
        idx = np.flatnonzero(ok)
        order = idx[np.argsort(sig[t, idx], kind="stable")]
        L, Sh = order[-k:], order[:k]
        gross = R[t, L].mean() - R[t, Sh].mean()
        bnet = P["beta"][t, L].mean() - P["beta"][t, Sh].mean()
        f = P["fund"][t]
        fund = -np.nan_to_num(f[L]).mean() + np.nan_to_num(f[Sh]).mean() + bnet * np.nan_to_num(P["btc_f"][t])
        hc = abs(bnet) * c.taker_round_trip()
        br = P[f"btc_{rkey}"][t]
        e2l = np.mean([ta.leg_return_e2(+1, P["bars"][t, i, :4], P["bars"][t, i, 4:], c) - P["beta"][t, i] * br for i in L])
        e2s = np.mean([ta.leg_return_e2(-1, P["bars"][t, i, :4], P["bars"][t, i, 4:], c) + P["beta"][t, i] * br for i in Sh])
        out["gross"][t] = gross
        out["e1"][t] = gross - 2 * c.round_trip_e1() - hc + fund
        out["e2"][t] = e2l + e2s - hc + fund
        out["fund"][t], out["hedge_cost"][t], out["beta_net"][t] = fund, hc, bnet
        out["legs"][t] = (tuple(L), tuple(Sh))
    return out


def stats(x: np.ndarray, m: np.ndarray | None = None) -> dict[str, Any]:
    v = x[np.isfinite(x) & (True if m is None else m)]
    if v.size < 30:
        return {"n": int(v.size)}
    lo, hi = ta.block_bootstrap_mean_ci(v, block=BLOCK, n_boot=3000)
    return {"n": int(v.size), "bp": float(v.mean() * 1e4), "ci_bp": [lo * 1e4, hi * 1e4]}


def full(cache: Path, uni: list[str], out: Path, n_perm: int) -> None:
    P = build_panel(cache, uni)
    ts = P["ts"]
    R8 = P["r8"] - P["beta"] * P["btc_r8"][:, None]
    res: dict[str, Any] = {"split": "2024-06-01", "n_windows": int(ts.size)}
    t0 = time.time()
    ic = mean_ic(P["S"], R8)
    res["H1"] = ic_summary(ic, ts)
    print("H1", json.dumps(res["H1"]), f"{time.time() - t0:.0f}s", flush=True)
    d_lead = mean_ic(P["S_lead"], R8) - ic  # condition 2: +5 min shift of the series, per regime
    lead = {}
    for name, m in (("old", ts < ta_switch()), ("new", ts >= ta_switch())):
        x = d_lead[m & np.isfinite(d_lead)]
        lead[name] = {"n": int(x.size), "mean": float(x.mean()), "ci": list(ta.block_bootstrap_mean_ci(x, block=BLOCK))}
    res["lead_shift_minus_rule_IC"] = lead
    print("lead shift", json.dumps(lead), flush=True)
    s_res = np.full_like(P["S"], np.nan)
    sw_res = np.full_like(P["S"], np.nan)
    for t in range(ts.size):
        for src, dst in ((P["S"], s_res), (P["Sw"], sw_res)):
            ok = np.isfinite(src[t]) & np.isfinite(P["r4h"][t])
            if ok.sum() >= MIN_N:
                dst[t, ok] = ta.xs_residual(src[t, ok], P["r4h"][t, ok])
    res["H3"] = ic_summary(mean_ic(s_res, R8), ts)
    res["H3w_volume_weighted"] = ic_summary(mean_ic(sw_res, R8), ts)
    res["IC_volume_weighted_raw"] = ic_summary(mean_ic(P["Sw"], R8), ts)
    res["IC_own_r4h_momentum"] = ic_summary(mean_ic(P["r4h"], R8), ts)
    print("H3", json.dumps(res["H3"]), flush=True)
    # placebo
    pl = {}
    for dd in (2, 3, 4, 5):
        sh = np.full_like(P["S"], np.nan)
        sh[3 * dd :] = P["S"][: -3 * dd]
        pl[f"S shifted {dd} days"] = ic_summary(mean_ic(sh, R8), ts)["all"]
    rng = np.random.default_rng(20261028)
    perm_means = []
    for _ in range(200):
        Sp = P["S"].copy()
        for t in range(ts.size):
            ok = np.flatnonzero(np.isfinite(Sp[t]))
            Sp[t, ok] = Sp[t, rng.permutation(ok)]
        perm_means.append(float(np.nanmean(mean_ic(Sp, R8))))
    pm = np.asarray(perm_means)
    pl["shuffle within window (200)"] = {
        "mean": float(pm.mean()),
        "p95": float(np.quantile(pm, 0.95)),
        "p_ge_obs": float((1 + (pm >= np.nanmean(ic)).sum()) / 201),
    }
    res["placebo"] = pl
    print("placebo", json.dumps(pl), f"{time.time() - t0:.0f}s", flush=True)
    # by year
    yr = np.array([datetime.fromtimestamp(int(t) / 1000, UTC).year for t in ts])
    res["H1_by_year"] = {
        str(y): {"n": int(np.isfinite(ic[yr == y]).sum()), "mean": float(np.nanmean(ic[yr == y]))} for y in sorted(set(yr))
    }
    # baskets
    c = ta.Costs()
    B = basket(P, P["S"], c)
    M = basket(P, P["r4h"], c)
    res["H2"] = {
        x: {"all": stats(B[x]), "IS": stats(B[x], ts < SPLIT), "OOS": stats(B[x], ts >= SPLIT)}
        for x in ("gross", "e1", "e2", "fund", "hedge_cost")
    }
    res["momentum_basket"] = {
        x: {"all": stats(M[x]), "IS": stats(M[x], ts < SPLIT), "OOS": stats(M[x], ts >= SPLIT)} for x in ("gross", "e1", "e2")
    }
    res["H2_minus_momentum_gross"] = stats(B["gross"] - M["gross"])
    res["H2_by_year"] = {str(y): {x: stats(B[x], yr == y).get("bp") for x in ("gross", "e1", "e2")} for y in sorted(set(yr))}
    g = B["gross"][np.isfinite(B["gross"])]
    res["H2_gross_without_top10_windows"] = float(np.mean(without_top(list(g), 10, +1)) * 1e4)
    ll = [x for x in B["legs"] if x is not None]
    turn = [1 - len(set(a[0]) & set(b[0])) / 3 for a, b in zip(ll[:-1], ll[1:], strict=True)]
    res["H2_long_leg_turnover_mean"] = float(np.mean(turn))
    stress = {}
    for name, cc in (
        ("costs x1.5", ta.Costs(0.0003, 0.00075, 0.00015, 0.3)),
        ("costs x2", ta.Costs(0.0004, 0.001, 0.0002, 0.3)),
        ("slip 3bp", ta.Costs(slip=0.0003)),
        ("p_miss 50%", ta.Costs(p_miss=0.5)),
    ):
        b = basket(P, P["S"], cc)
        stress[name] = {"e1": stats(b["e1"]).get("bp"), "e2": stats(b["e2"]).get("bp")}
    res["cost_stress"] = stress
    print(
        "H2",
        json.dumps(res["H2"]["gross"]),
        json.dumps(res["H2"]["e1"]["all"]),
        json.dumps(res["H2"]["e2"]["all"]),
        f"{time.time() - t0:.0f}s",
        flush=True,
    )
    # random 3/3 baskets
    Rg = R8
    obs_gross = float(np.nanmean(B["gross"]))
    vt = [t for t in range(ts.size) if np.isfinite(B["gross"][t])]
    okm = [
        np.flatnonzero(np.isfinite(P["S"][t]) & np.isfinite(Rg[t]) & np.isfinite(P["beta"][t]) & np.all(np.isfinite(P["bars"][t]), axis=1))
        for t in vt
    ]
    pr = np.empty(n_perm)
    for b_ in range(n_perm):
        acc = 0.0
        for t, ok in zip(vt, okm, strict=True):
            pick = rng.choice(ok, 6, replace=False)
            acc += Rg[t, pick[:3]].mean() - Rg[t, pick[3:]].mean()
        pr[b_] = acc / len(vt)
    res["random_basket_gross_bp"] = {
        "mean": float(pr.mean() * 1e4),
        "p95": float(np.quantile(pr, 0.95) * 1e4),
        "p_ge_obs": float((1 + (pr >= obs_gross).sum()) / (1 + n_perm)),
    }
    print("random baskets", json.dumps(res["random_basket_gross_bp"]), f"{time.time() - t0:.0f}s", flush=True)
    # per-asset (5a): IC contributions z(rank S) * z(rank R)
    vals, assets, clus, ism = [], [], [], []
    for t in range(ts.size):
        ok = np.isfinite(P["S"][t]) & np.isfinite(R8[t])
        if ok.sum() < MIN_N:
            continue
        pr_ = ic_products(P["S"][t, ok], R8[t, ok])
        for j, v in zip(np.flatnonzero(ok), pr_, strict=True):
            vals.append(float(v))
            assets.append(uni[j])
            clus.append(t)
            ism.append(bool(ts[t] < SPLIT))
    va, aa = np.asarray(vals), np.asarray(assets)
    tab = {}
    for s in uni:
        x = va[aa == s]
        se = x.std(ddof=1) / np.sqrt(x.size) * np.sqrt(3.0)  # x sqrt(3): rough allowance for autocorrelation
        tab[s] = {"n": int(x.size), "mean": float(x.mean()), "se": float(se)}
    q, df, i2 = ta.cochran_q([tab[s]["mean"] for s in uni], [tab[s]["se"] for s in uni])
    res["per_asset"] = {
        "table": tab,
        "cochran": {"q": q, "df": df, "i2": i2},
        "perm_q": permutation_q(vals, assets, clus, n_perm=300),
        "persistence_top7": persistence(vals, assets, clus, ism, top_n=7, n_boot=2000),
        "heterogeneity": heterogeneity(vals, assets),
    }
    # exploratory (not in the verdict)
    R4 = P["r4"] - P["beta"] * P["btc_r4"][:, None]
    R12 = P["r12"] - P["beta"] * P["btc_r12"][:, None]
    res["exploratory"] = {
        "IC_h4": ic_summary(mean_ic(P["S"], R4), ts)["all"],
        "IC_h12": ic_summary(mean_ic(P["S"], R12), ts)["all"],
        "IC_quarter_hour_bars_only": ic_summary(mean_ic(P["Sqh"], R8), ts)["all"],
        "basket_k2_gross_e1": [stats(basket(P, P["S"], c, k=2)[x]).get("bp") for x in ("gross", "e1")],
        "basket_k5_gross_e1": [stats(basket(P, P["S"], c, k=5)[x]).get("bp") for x in ("gross", "e1")],
    }
    res["peak_rss"] = rss()
    out.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: res[k] for k in ("H1", "H3", "H3w_volume_weighted", "IC_own_r4h_momentum")}, indent=1))
    print("H2", json.dumps(res["H2"], indent=0))
    print("momentum", json.dumps(res["momentum_basket"], indent=0))
    for k in (
        "H2_minus_momentum_gross",
        "H2_by_year",
        "H2_gross_without_top10_windows",
        "H2_long_leg_turnover_mean",
        "cost_stress",
        "H1_by_year",
        "exploratory",
    ):
        print(k, json.dumps(res[k]))
    pa = res["per_asset"]
    print("per-asset cochran", json.dumps(pa["cochran"]), "perm_q", json.dumps(pa["perm_q"]))
    print("persistence", json.dumps(pa["persistence_top7"], default=str)[:800])
    print(rss())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["universe", "fetch", "semantics", "stage0", "full"])
    ap.add_argument("--cache-dir", type=Path, default=AG / "taker_imbalance")
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--perm", type=int, default=1000)
    ap.add_argument("--coins", type=int, default=None, help="first N coins only (code-path checks)")
    ap.add_argument("--quiet", action="store_true", help="full: hide results (code-path check before the real run)")
    a = ap.parse_args()
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "universe":
        stage_universe(cache)
        return
    uni = rd(cache / "universe.json")["universe"][: a.coins]
    if a.coins:
        globals()["MIN_N"] = max(3, a.coins - 1)
    if a.stage == "fetch":
        asyncio.run(stage_fetch(cache, uni, FIRST_DAY, LAST_DAY, a.sample))
    elif a.stage == "semantics":
        asyncio.run(stage_semantics(cache))
    elif a.stage == "stage0":
        stage0(cache, uni, a.out or cache / "stage0.json")
    else:
        if a.quiet:  # code-path check only: results are not shown
            sys.stdout = open(os.devnull, "w")  # noqa: SIM115
        full(cache, uni, a.out or cache / "full.json", a.perm)
        print("FULL DONE", file=sys.stderr)


if __name__ == "__main__":
    main()
