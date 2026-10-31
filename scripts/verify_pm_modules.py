"""Polymarket modules H11, H7 (with H2), H8 (IDEA-20261031-structural-edges-depth, item 5).

The pre-registration is in the idea card ("Пункт 5. Попередня реєстрація"). Offline: reads only caches
(``polymarket_threshold/`` quotes, outcomes, Binance 1h, DVOL; ``weekend_vol/klines`` BTC 1h since 2020) and a
read-only copy of the collector DB (``pm_modules/tradedesk_copy.sqlite``, made with ``sqlite3 backup`` from
``/var/lib/tradedesk-mcp/tradedesk.sqlite``; ``--refresh-db`` makes a new copy). No network.

Usage:
    uv run python scripts/verify_pm_modules.py [--module h11|h7|h8|all] [--n-boot 10000]
Prints compact tables; the full result goes to ``<out-dir>/result.json``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import resource
import sqlite3
import statistics
import sys
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics import pm_modules as pm
from tradedesk.analytics.pm_threshold import HOUR_S, YEAR_S, value_at

PMT = Path("~/.cache/tradedesk-agents/polymarket_threshold").expanduser()
WKV = Path("~/.cache/tradedesk-agents/weekend_vol/klines").expanduser()
OUT = Path("~/.cache/tradedesk-agents/pm_modules").expanduser()
DB_SRC = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
IS_START = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp())
IS_END = int(datetime(2025, 3, 1, tzinfo=UTC).timestamp())  # IS < 2025-03-01 <= OOS
SEED = 20261031
TAU12 = 12 * HOUR_S / YEAR_S


def _load_threshold_script():  # noqa: ANN202
    p = Path(__file__).with_name("verify_pm_threshold.py")
    spec = importlib.util.spec_from_file_location("verify_pm_threshold", p)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["verify_pm_threshold"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def week_of(ts: int) -> str:
    y, w, _ = datetime.fromtimestamp(ts, UTC).isocalendar()
    return f"{y}-W{w:02d}"


def f(x: float | None, k: float = 1.0, nd: int = 3) -> str:
    return "—" if x is None else f"{x * k:+.{nd}f}"


def ci(r: dict[str, Any], k: float = 1.0, nd: int = 3) -> str:
    if r.get("mean") is None:
        return "—"
    return f"{r['mean'] * k:+.{nd}f} [{r['lo'] * k:+.{nd}f}; {r['hi'] * k:+.{nd}f}] p={r['p']:.3f}"


def boot_units(units: list[tuple[str, float]], n_boot: int) -> dict[str, Any]:
    """(block, value) units -> block bootstrap of the mean, plus n_eff and MDE from the block SE."""
    blocks: dict[str, list[float]] = defaultdict(list)
    for b, v in units:
        blocks[b].append(v)
    r = pm.block_bootstrap_ratio([(sum(v), len(v)) for v in blocks.values()], n_boot=n_boot, seed=SEED)
    vals = [v for _, v in units]
    if len(vals) > 1 and r.get("se"):
        r["n_eff"] = statistics.pvariance(vals) / r["se"] ** 2
        r["mde"] = 2.8 * r["se"]
    return r


def load_btc_long() -> list[tuple[int, float]]:
    rows: dict[int, float] = {}
    for p in sorted(WKV.glob("BTCUSDT-1h-*.json")):
        for r in json.loads(p.read_text()):
            rows[int(r[0]) // 1000] = float(r[4])
    return [(k + HOUR_S, rows[k]) for k in sorted(rows)]


def bar_returns(closes: list[tuple[int, float]], lo: int, hi: int) -> list[tuple[int, float]]:
    """(bar_open, log return) of consecutive hourly closes with lo <= bar_open < hi."""
    out = []
    for (ta, a), (tb, b) in zip(closes, closes[1:], strict=False):
        if tb - ta == HOUR_S and lo <= tb - HOUR_S < hi and a > 0 and b > 0:
            out.append((tb - HOUR_S, math.log(b / a)))
    return out


def window(closes: list[tuple[int, float]], keys: list[int], t0: int, days: int = 91) -> list[tuple[int, float]]:
    return closes[bisect_left(keys, t0 - days * 86400) : bisect_right(keys, t0)]


# ----------------------------------------------------------------------------- shared event table


def build_events(vt: Any) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[str, list[tuple[int, float]]]]:
    rows = vt.build_rows(PMT)
    spot = vt.load_spot(PMT)
    dvol = vt.load_dvol(PMT)
    ev: dict[tuple[str, int], dict[str, Any]] = {}
    for r in rows:
        if r["q"] is None or r["S0"] is None:
            continue
        e = ev.setdefault((r["asset"], r["t0"]), {"asset": r["asset"], "t0": r["t0"], "T": r["T"], "S0": r["S0"], "mk": []})
        e["mk"].append((r["strike"], r["q"], r["outcome"]))
    for (a, t0), e in ev.items():
        e["dvol"] = value_at(dvol[a], t0) if a in dvol else None
        e["rv"] = pm.realized_var(spot[a], t0, e["T"])
    return ev, spot


# ----------------------------------------------------------------------------- H7


def run_h7(ev: dict, spot: dict, n_boot: int, res: dict[str, Any]) -> None:
    t = time.perf_counter()
    btc = load_btc_long()
    prof = pm.intraday_profile(bar_returns(btc, IS_START, IS_END))
    res["h7_profile_is"] = {f"{h:02d}{'w' if w else 'd'}": round(v, 4) for (h, w), v in sorted(prof.items())}
    n_is = len(bar_returns(btc, IS_START, IS_END))
    wd_sum = sum(prof[(h, False)] for h in range(4, 16))
    we_sum = sum(prof[(h, True)] for h in range(4, 16))
    print(
        f"H7 IS profile (BTC 1h 2020-01..2025-02, n bars={n_is}): weekday 04-16 UTC sum={wd_sum:.2f}, weekend 04-16 sum={we_sum:.2f} (flat=12)",
        flush=True,
    )
    keys = {a: [k for k, _ in s] for a, s in spot.items()}
    # per-asset expanding profile (past only, refreshed monthly from 2025-06-01 to the month start of t0)
    month_prof: dict[tuple[str, str], dict] = {}

    def asset_prof(a: str, t0: int) -> dict:
        d = datetime.fromtimestamp(t0, UTC)
        key = (a, f"{d.year}-{d.month:02d}")
        if key not in month_prof:
            end = int(datetime(d.year, d.month, 1, tzinfo=UTC).timestamp())
            month_prof[key] = pm.intraday_profile(bar_returns(spot[a], 0, end))
        return month_prof[key]

    units: dict[str, list] = defaultdict(list)  # variant -> [(week, asset, t0, dBrier, dLogloss, weekend, absdp)]
    for i, ((a, t0), e) in enumerate(sorted(ev.items(), key=lambda kv: kv[0][1])):
        w = window(spot[a], keys[a], t0)
        v_raw = pm.ewma_hourly_var(w, t0)
        if v_raw is None:
            continue
        v_base = v_raw * 12
        variants = {"main": (prof, 0), "asset_prof": (asset_prof(a, t0), 0)}
        for k in range(1, 24):
            variants[f"shift{k:02d}"] = (prof, k)
        wk = week_of(t0)
        weekend = pm.is_weekend(t0 + 6 * HOUR_S)  # middle of the window
        for name, (pr, sh) in variants.items():
            v_d = pm.ewma_hourly_var(w, t0, profile=pr, shift_h=sh)
            if v_d is None:
                continue
            v_prof = v_d * pm.window_weight(pr, t0, e["T"], sh)
            for K, _q, y in e["mk"]:
                pb = pm.prob_from_var(e["S0"], K, v_base)
                pp = pm.prob_from_var(e["S0"], K, v_prof)
                units[name].append(
                    (wk, a, t0, pm.brier(pb, y) - pm.brier(pp, y), pm.log_loss(pb, y) - pm.log_loss(pp, y), weekend, abs(pp - pb))
                )
        if i % 300 == 0:
            print(f"  h7 events {i}/{len(ev)} {time.perf_counter() - t:.0f}s rss={rss_mb():.0f}MB", flush=True)
    main = units["main"]
    out: dict[str, Any] = {}
    out["brier"] = boot_units([(u[0], u[3]) for u in main], n_boot)
    out["logloss"] = boot_units([(u[0], u[4]) for u in main], n_boot)
    out["asset_prof"] = boot_units([(u[0], u[3]) for u in units["asset_prof"]], n_boot)
    out["n_markets"], out["n_events"] = len(main), len({(u[1], u[2]) for u in main})
    out["share_dp_gt_1pp"] = sum(u[6] > 0.01 for u in main) / len(main)
    out["share_dp_gt_275pp"] = sum(u[6] > 0.0275 for u in main) / len(main)
    out["weekend"] = boot_units([(u[0], u[3]) for u in main if u[5]], n_boot)
    out["weekday"] = boot_units([(u[0], u[3]) for u in main if not u[5]], n_boot)
    out["y2025"] = boot_units([(u[0], u[3]) for u in main if datetime.fromtimestamp(u[2], UTC).year == 2025], n_boot)
    out["y2026"] = boot_units([(u[0], u[3]) for u in main if datetime.fromtimestamp(u[2], UTC).year == 2026], n_boot)
    plac = sorted(sum(u[3] for u in units[f"shift{k:02d}"]) / len(units[f"shift{k:02d}"]) for k in range(1, 24))
    out["placebo"] = plac
    out["placebo_p95"] = _pct(plac, 0.95)
    per_asset = _per_asset(main, 3, n_boot)
    out["per_asset"] = per_asset
    res["h7"] = out
    print(f"H7 done: {out['n_markets']} markets, {out['n_events']} events, {time.perf_counter() - t:.0f}s rss={rss_mb():.0f}MB", flush=True)


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _per_asset(units: list, idx: int, n_boot: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in sorted({u[1] for u in units}):
        sub = [u for u in units if u[1] == a]
        r = boot_units([(u[0], u[idx]) for u in sub], n_boot)
        h1 = [u[idx] for u in sub if datetime.fromtimestamp(u[2], UTC).year == 2025]
        h2 = [u[idx] for u in sub if datetime.fromtimestamp(u[2], UTC).year == 2026]
        r["first_half"] = sum(h1) / len(h1) if h1 else None
        r["second_half"] = sum(h2) / len(h2) if h2 else None
        r["sum"] = sum(u[idx] for u in sub)
        out[a] = r
    ok = [a for a in out if out[a].get("se")]
    if len(ok) >= 2:
        out["_cochran"] = pm.cochran_q([out[a]["mean"] for a in ok], [out[a]["se"] for a in ok])
    return out


# ----------------------------------------------------------------------------- H11


def run_h11(ev: dict, spot: dict, n_boot: int, res: dict[str, Any]) -> None:
    keys = {a: [k for k, _ in s] for a, s in spot.items()}
    out: dict[str, Any] = {}
    for label, lo, hi, mn in (("main", 0.03, 0.97, 2), ("min3", 0.03, 0.97, 3), ("q05_95", 0.05, 0.95, 2)):
        units = []  # (week, asset, t0, d_qlike, rv, v_pm, v_ewma, v_dvol)
        for (a, t0), e in sorted(ev.items(), key=lambda kv: kv[0][1]):
            if e["rv"] is None or e["rv"] <= 0:
                continue
            fit = pm.ladder_sigma([m[0] for m in e["mk"]], [m[1] for m in e["mk"]], e["S0"], lo=lo, hi=hi, min_n=mn)
            if fit is None:
                continue
            v_raw = pm.ewma_hourly_var(window(spot[a], keys[a], t0), t0)
            if v_raw is None:
                continue
            v_pm, v_ew = fit["s"] ** 2, v_raw * 12
            v_dv = e["dvol"] ** 2 * TAU12 if e["dvol"] else None
            units.append((week_of(t0), a, t0, pm.qlike(e["rv"], v_ew) - pm.qlike(e["rv"], v_pm), e["rv"], v_pm, v_ew, v_dv))
        r: dict[str, Any] = {"n_events": len(units), "n_weeks": len({u[0] for u in units})}
        r["dm"] = boot_units([(u[0], u[3]) for u in units], n_boot)
        # exploratory (fix round after critic B): how much of each variant is degenerate fits
        r["share_gt3"] = sum(u[5] > 9 * u[6] for u in units) / len(units) if units else None
        r["dm_le3"] = boot_units([(u[0], u[3]) for u in units if u[5] <= 9 * u[6]], n_boot)
        if label == "main":
            wk: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
            for u in units:
                x = wk[u[0]]
                x[0] += u[4]
                x[1] += u[5]
                x[2] += u[6]
                if u[7] is not None:
                    x[3] += u[4]
                    x[4] += u[7]
            r["bias_pm"] = pm.block_bootstrap_logratio([(x[0], x[1]) for x in wk.values()], n_boot=n_boot, seed=SEED)
            r["bias_ewma"] = pm.block_bootstrap_logratio([(x[0], x[2]) for x in wk.values()], n_boot=n_boot, seed=SEED)
            r["bias_dvol"] = pm.block_bootstrap_logratio([(x[3], x[4]) for x in wk.values() if x[4] > 0], n_boot=n_boot, seed=SEED)
            r["median_sigma_ratio_pm_ewma"] = statistics.median(math.sqrt(u[5] / u[6]) for u in units)
            # exploratory (added after the first run): the pooled-sum bias is dominated by a few flat fits
            r["explore"] = {
                "median_ln_rv_over_v_pm": statistics.median(math.log(u[4] / u[5]) for u in units),
                "median_ln_rv_over_v_ewma": statistics.median(math.log(u[4] / u[6]) for u in units),
                "share_sigma_pm_gt_3x_ewma": sum(u[5] > 9 * u[6] for u in units) / len(units),
                "top5_share_of_sum_v_pm": sum(sorted((u[5] for u in units), reverse=True)[:5]) / sum(u[5] for u in units),
                "per_asset_median_ln_rv_over_v_pm": {
                    a: statistics.median(math.log(u[4] / u[5]) for u in units if u[1] == a) for a in sorted({u[1] for u in units})
                },
                "per_asset_median_ln_rv_over_v_ewma": {
                    a: statistics.median(math.log(u[4] / u[6]) for u in units if u[1] == a) for a in sorted({u[1] for u in units})
                },
            }
            # exploratory (fix round after critic B): d without degenerate ladder fits (beta ~ 0 -> huge v_PM).
            # Filters use forecasts only (no outcome); the one-sided ratio cut removes PM's worst cases by construction,
            # so it is biased towards PM; the symmetric and absolute cuts and the median / trimmed mean are controls.
            filt = {
                "ratio_le_3": lambda u: u[5] <= 9 * u[6],
                "ratio_sym_3": lambda u: u[6] / 9 <= u[5] <= 9 * u[6],
                "sigma_ann_le_2": lambda u: u[5] <= 4.0 * TAU12,  # annualised sigma_PM = sqrt(v_PM / tau) <= 200 %
            }
            ex2: dict[str, Any] = {}
            for name, keep in filt.items():
                sub = [u for u in units if keep(u)]
                e2: dict[str, Any] = {"n": len(sub), "pool": boot_units([(u[0], u[3]) for u in sub], n_boot)}
                e2["y2025"] = boot_units([(u[0], u[3]) for u in sub if datetime.fromtimestamp(u[2], UTC).year == 2025], n_boot)
                e2["y2026"] = boot_units([(u[0], u[3]) for u in sub if datetime.fromtimestamp(u[2], UTC).year == 2026], n_boot)
                e2["per_asset"] = _per_asset(sub, 3, n_boot)
                ex2[name] = e2
            ds = sorted(u[3] for u in units)
            lo_d, hi_d = _pct(ds, 0.01), _pct(ds, 0.99)
            ex2["median_d"] = statistics.median(ds)
            ex2["median_d_per_asset"] = {a: statistics.median(u[3] for u in units if u[1] == a) for a in sorted({u[1] for u in units})}
            ex2["trim_1_99"] = boot_units([(u[0], min(max(u[3], lo_d), hi_d)) for u in units], n_boot)
            ex2["trim_1_99"]["winsor_bounds"] = (lo_d, hi_d)
            r["explore_nodegen"] = ex2
            r["mz"] = _ols([math.log(u[4]) for u in units], [[1.0, math.log(u[5]), math.log(u[6])] for u in units])
            r["y2025"] = boot_units([(u[0], u[3]) for u in units if datetime.fromtimestamp(u[2], UTC).year == 2025], n_boot)
            r["y2026"] = boot_units([(u[0], u[3]) for u in units if datetime.fromtimestamp(u[2], UTC).year == 2026], n_boot)
            r["per_asset"] = _per_asset(units, 3, n_boot)
            # per-asset bias
            for a in sorted({u[1] for u in units}):
                wa: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
                for u in units:
                    if u[1] == a:
                        wa[u[0]][0] += u[4]
                        wa[u[0]][1] += u[5]
                r["per_asset"][a]["bias_pm"] = pm.block_bootstrap_logratio([tuple(x) for x in wa.values()], n_boot=n_boot, seed=SEED)
        out[label] = r
        print(f"H11 {label}: {r['n_events']} events, {r['n_weeks']} weeks", flush=True)
    res["h11"] = out


def _ols(y: list[float], X: list[list[float]]) -> list[float]:
    k = len(X[0])
    a = [[sum(x[i] * x[j] for x in X) for j in range(k)] for i in range(k)]
    b = [sum(x[i] * yy for x, yy in zip(X, y, strict=True)) for i in range(k)]
    for c in range(k):  # Gauss-Jordan
        piv = a[c][c]
        a[c] = [v / piv for v in a[c]]
        b[c] /= piv
        for r in range(k):
            if r != c:
                fct = a[r][c]
                a[r] = [vr - fct * vc for vr, vc in zip(a[r], a[c], strict=True)]
                b[r] -= fct * b[c]
    return b


# ----------------------------------------------------------------------------- H8


def copy_db(dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    a = sqlite3.connect(f"file:{DB_SRC}?mode=ro", uri=True)
    b = sqlite3.connect(dst)
    with b:
        a.backup(b)
    a.close()
    b.close()


def fee(p: float, rate: float, exp: float) -> float:
    return rate * (p * (1 - p)) ** exp


def best_edge(p: float, r: dict) -> float | None:
    e = []
    if r["yes_ask"] is not None:
        e.append(p - r["yes_ask"] - fee(r["yes_ask"], r["fee_rate"] or 0.0, r["fee_exp"] or 1.0))
    if r["no_ask"] is not None:
        e.append((1 - p) - r["no_ask"] - fee(r["no_ask"], r["fee_rate"] or 0.0, r["fee_exp"] or 1.0))
    return max(e) if e else None


def run_h8(db: Path, n_boot: int, res: dict[str, Any]) -> None:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    pairs: dict[tuple[int, str], dict[str, Any]] = {}
    for r in con.execute(
        "SELECT ts, asset, binance_last, spot, model_forward FROM pm_gap_snapshot WHERE binance_last IS NOT NULL AND spot IS NOT NULL"
    ):
        p = pairs.setdefault((r["ts"], r["asset"]), {"bl": set(), "spot": set(), "fwd": []})
        p["bl"].add(r["binance_last"])
        p["spot"].add(r["spot"])
        if r["model_forward"]:
            p["fwd"].append(r["model_forward"])
    multi = sum(len(p["bl"]) > 1 or len(p["spot"]) > 1 for p in pairs.values())
    g: dict[tuple[int, str], float] = {k: statistics.median(p["bl"]) / statistics.median(p["spot"]) - 1 for k, p in pairs.items()}
    out: dict[str, Any] = {"n_pairs": len(g), "pairs_with_multiple_values": multi}
    days = {datetime.fromtimestamp(ts, UTC).date().isoformat() for ts, _ in g}
    out["n_days"], out["first"], out["last"] = len(days), min(days), max(days)
    vals = list(g.values())
    out["sd_g"] = statistics.pstdev(vals)
    out["median_g"] = statistics.median(vals)
    out["p05_p95_g"] = (_pct(vals, 0.05), _pct(vals, 0.95))
    out["mean"] = boot_units([(datetime.fromtimestamp(ts, UTC).date().isoformat(), v) for (ts, _), v in g.items()], n_boot)
    # lag-1 autocorrelation of g within asset (hourly dependence)
    ac = {}
    for a in ("BTC", "ETH"):
        s = [v for (ts, aa), v in sorted(g.items()) if aa == a]
        if len(s) > 3:
            m = statistics.fmean(s)
            ac[a] = sum((x - m) * (y - m) for x, y in zip(s, s[1:], strict=False)) / sum((x - m) ** 2 for x in s)
        sub = [(datetime.fromtimestamp(ts, UTC).date().isoformat(), v) for (ts, aa), v in g.items() if aa == a]
        out[f"asset_{a}"] = boot_units(sub, n_boot) | {"median": statistics.median(v for _, v in sub)} if sub else None
    out["lag1_autocorr"] = ac
    fw = [statistics.median(p["fwd"]) / statistics.median(p["spot"]) - 1 for p in pairs.values() if p["fwd"]]
    out["median_forward_basis"] = statistics.median(fw) if fw else None
    # recomputation of model_prob
    dps, cross_in, cross_out, n_rows, pass0, pass1 = [], 0, 0, 0, 0, 0
    by_kind: dict[str, list[float]] = defaultdict(list)
    out_by: dict[str, int] = defaultdict(int)
    for r in con.execute(
        "SELECT ts, asset, kind, direction, strike, hours_to_end, model_prob, model_iv, model_forward, yes_ask, no_ask, fee_rate, fee_exp "
        "FROM pm_gap_snapshot WHERE model_note='ok' AND model_prob IS NOT NULL AND model_iv > 0 AND model_forward > 0 AND hours_to_end > 0"
    ):
        gg = g.get((r["ts"], r["asset"]))
        if gg is None:
            continue
        n_rows += 1
        d = pm.shift_delta(
            r["kind"],
            r["model_forward"],
            r["strike"],
            r["model_iv"],
            pm.tau_of_hours(r["hours_to_end"]),
            gg,
            r["direction"] if r["kind"] == "touch" else None,
        )
        dps.append(d)
        by_kind[r["kind"]].append(d)
        p0 = r["model_prob"]
        p1 = min(1.0, max(0.0, p0 + d))
        e0, e1 = best_edge(p0, dict(r)), best_edge(p1, dict(r))
        if e0 is not None and e1 is not None:
            # exploratory (added after the first run): base count and which side / kind loses the 5 p.p. edge
            pass0 += e0 >= 0.05
            pass1 += e1 >= 0.05
            if e1 < 0.05 <= e0:
                side = (
                    "yes"
                    if r["yes_ask"] is not None and p0 - r["yes_ask"] >= (1 - p0) - (r["no_ask"] if r["no_ask"] is not None else 1)
                    else "no"
                )
                out_by[f"{r['kind']}:{r['direction']}:{side}"] += 1
            cross_in += e0 < 0.05 <= e1
            cross_out += e1 < 0.05 <= e0
    con.close()
    if dps:
        ad = [abs(x) for x in dps]
        out["recompute"] = {
            "n_rows": n_rows,
            "mean_dp": statistics.fmean(dps),
            "median_abs_dp": statistics.median(ad),
            "p95_abs_dp": _pct(ad, 0.95),
            "share_abs_gt_1pp": sum(x > 0.01 for x in ad) / len(ad),
            "share_abs_gt_05pp": sum(x > 0.005 for x in ad) / len(ad),
            "edge_cross_in": cross_in,
            "edge_cross_out": cross_out,
            "edge_pass_before_after": (pass0, pass1),
            "edge_out_by_kind_side": dict(out_by),
            "by_kind": {
                k: {"n": len(v), "mean": statistics.fmean(v), "median_abs": statistics.median(abs(x) for x in v)}
                for k, v in by_kind.items()
            },
        }
    res["h8"] = out


# ----------------------------------------------------------------------------- report


def print_report(res: dict[str, Any]) -> None:
    pv = {}
    if "h11" in res:
        m = res["h11"]["main"]
        pv["H11"] = m["dm"]["p"]
        print("\n## H11 (QLIKE EWMA - PM; >0 = PM better), weeks block bootstrap")
        for lbl in ("main", "min3", "q05_95"):
            r = res["h11"][lbl]
            print(
                f"  {lbl}: n={r['n_events']} weeks={r['n_weeks']} dm={ci(r['dm'])} n_eff={r['dm'].get('n_eff', 0):.0f} mde={r['dm'].get('mde', 0):.3f}"
            )
        print(f"  bias ln(sum RV / sum v): PM {ci(m['bias_pm'])}; EWMA {ci(m['bias_ewma'])}; DVOL(BTC/ETH) {ci(m['bias_dvol'])}")
        print(
            f"  median sigma_PM / sigma_EWMA = {m['median_sigma_ratio_pm_ewma']:.3f}; MZ ln RV = {m['mz'][0]:+.2f} + {m['mz'][1]:.2f} ln v_PM + {m['mz'][2]:.2f} ln v_EWMA"
        )
        print(f"  2025: {ci(m['y2025'])}  2026: {ci(m['y2026'])}")
        x = m["explore"]
        print(
            f"  [exploratory] median ln(RV/v): PM {x['median_ln_rv_over_v_pm']:+.3f}, EWMA {x['median_ln_rv_over_v_ewma']:+.3f}; "
            f"sigma_PM > 3x EWMA: {x['share_sigma_pm_gt_3x_ewma']:.3f}; top-5 events' share of sum v_PM: {x['top5_share_of_sum_v_pm']:.3f}"
        )
        print(
            "  [exploratory] per asset median ln(RV/v_PM) / ln(RV/v_EWMA): "
            + ", ".join(
                f"{a} {x['per_asset_median_ln_rv_over_v_pm'][a]:+.3f}/{x['per_asset_median_ln_rv_over_v_ewma'][a]:+.3f}"
                for a in x["per_asset_median_ln_rv_over_v_pm"]
            )
        )
        for a, r in m["per_asset"].items():
            if a.startswith("_"):
                print(f"  cochran Q={r['q']:.2f} df={r['df']:.0f} p={r['p']:.3f} I2={r['i2']:.2f}")
            else:
                print(f"  {a:9s} n={r['n']:4d} dm={ci(r)} 1st={f(r['first_half'])} 2nd={f(r['second_half'])} bias_pm={ci(r['bias_pm'])}")
        x2 = m.get("explore_nodegen")
        if x2:
            for lbl in ("main", "min3", "q05_95"):
                r = res["h11"][lbl]
                if r.get("dm_le3"):
                    print(
                        f"  [exploratory] {lbl}: share sigma_PM > 3x EWMA={r['share_gt3']:.3f}; dm without them={ci(r['dm_le3'])} (n={r['dm_le3'].get('n', 0)})"
                    )
            print("  [exploratory] d without degenerate fits (ratio = sigma_PM / sigma_EWMA; sigma_ann = annualised sigma_PM):")
            for name in ("ratio_le_3", "ratio_sym_3", "sigma_ann_le_2"):
                e2 = x2[name]
                print(f"    {name}: n={e2['n']} dm={ci(e2['pool'])} 2025={ci(e2['y2025'])} 2026={ci(e2['y2026'])}")
                for a, r in e2["per_asset"].items():
                    if a.startswith("_"):
                        print(f"      cochran Q={r['q']:.2f} p={r['p']:.3f} I2={r['i2']:.2f}")
                    else:
                        print(f"      {a:9s} n={r['n']:4d} dm={ci(r)} 2025={f(r['first_half'])} 2026={f(r['second_half'])}")
            print(
                f"    median d={x2['median_d']:+.3f} ("
                + ", ".join(f"{a} {v:+.3f}" for a, v in x2["median_d_per_asset"].items())
                + f"); winsorised 1/99 % mean d={ci(x2['trim_1_99'])} bounds={x2['trim_1_99']['winsor_bounds'][0]:+.2f}..{x2['trim_1_99']['winsor_bounds'][1]:+.2f}"
            )
    if "h7" in res:
        h = res["h7"]
        pv["H7"] = h["brier"]["p"]
        print("\n## H7 (dBrier = base - profile, x1e4; >0 = profile better)")
        print(
            f"  markets={h['n_markets']} events={h['n_events']} brier={ci(h['brier'], 1e4)} n_eff={h['brier'].get('n_eff', 0):.0f} mde={h['brier'].get('mde', 0) * 1e4:.3f}"
        )
        print(f"  logloss x1e4={ci(h['logloss'], 1e4)}  asset_prof brier={ci(h['asset_prof'], 1e4)}")
        print(f"  weekend={ci(h['weekend'], 1e4)}  weekday={ci(h['weekday'], 1e4)}")
        print(f"  2025={ci(h['y2025'], 1e4)}  2026={ci(h['y2026'], 1e4)}")
        print(
            f"  placebo (23 shifts) x1e4: min={h['placebo'][0] * 1e4:+.3f} p95={h['placebo_p95'] * 1e4:+.3f} max={h['placebo'][-1] * 1e4:+.3f}"
        )
        print(f"  share |p_prof - p_base| > 1pp: {h['share_dp_gt_1pp']:.3f}; > 2.75pp: {h['share_dp_gt_275pp']:.3f}")
        for a, r in h["per_asset"].items():
            if a.startswith("_"):
                print(f"  cochran Q={r['q']:.2f} df={r['df']:.0f} p={r['p']:.3f} I2={r['i2']:.2f}")
            else:
                print(
                    f"  {a:9s} n={r['n']:5d} {ci(r, 1e4)} 1st={f(r['first_half'], 1e4)} 2nd={f(r['second_half'], 1e4)} sum={r['sum']:+.3f}"
                )
    if "h8" in res:
        h = res["h8"]
        pv["H8"] = h["mean"]["p"]
        print("\n## H8 (g = binance_last / spot - 1, %; day blocks)")
        print(
            f"  pairs={h['n_pairs']} (multi-valued {h['pairs_with_multiple_values']}) days={h['n_days']} {h['first']}..{h['last']} lag1 autocorr={ {k: round(v, 2) for k, v in h['lag1_autocorr'].items()} }"
        )
        print(
            f"  mean g={ci(h['mean'], 100)} n_eff={h['mean'].get('n_eff', 0):.0f}; median={h['median_g'] * 100:+.4f}; sd={h['sd_g'] * 100:.4f}; p05..p95={h['p05_p95_g'][0] * 100:+.4f}..{h['p05_p95_g'][1] * 100:+.4f}"
        )
        for a in ("BTC", "ETH"):
            r = h.get(f"asset_{a}")
            if r:
                print(f"  {a}: n={r['n']} mean={ci(r, 100, 4)} median={r['median'] * 100:+.4f}")
        if h.get("median_forward_basis") is not None:
            print(f"  median model_forward / spot - 1 = {h['median_forward_basis'] * 100:+.4f} %")
        rc = h.get("recompute")
        if rc:
            print(
                f"  recompute: rows={rc['n_rows']} mean dp={rc['mean_dp'] * 100:+.3f}pp median|dp|={rc['median_abs_dp'] * 100:.3f}pp p95|dp|={rc['p95_abs_dp'] * 100:.3f}pp "
                f">1pp={rc['share_abs_gt_1pp']:.3f} >0.5pp={rc['share_abs_gt_05pp']:.3f} edge>=5pp in/out={rc['edge_cross_in']}/{rc['edge_cross_out']}"
            )
            print(
                f"  [exploratory] rows with edge>=5pp before/after: {rc['edge_pass_before_after']}; lost by kind:direction:side {rc['edge_out_by_kind_side']}"
            )
            for k, v in rc["by_kind"].items():
                print(f"    {k}: n={v['n']} mean dp={v['mean'] * 100:+.3f}pp median|dp|={v['median_abs'] * 100:.3f}pp")
    if len(pv) == 3:
        print("\n## Holm (m=3, two-sided, alpha 0.05)")
        for k, v in pm.holm(pv).items():
            print(f"  {k}: p={v['p']:.4f} threshold={v['threshold']:.4f} reject={v['reject']}")
        res["holm"] = pm.holm(pv)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", default="all", choices=("h11", "h7", "h8", "all"))
    ap.add_argument("--n-boot", type=int, default=10_000)
    ap.add_argument("--out-dir", default=str(OUT))
    ap.add_argument("--refresh-db", action="store_true")
    a = ap.parse_args()
    out = Path(a.out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    res: dict[str, Any] = {"generated": datetime.now(UTC).isoformat(timespec="seconds"), "n_boot": a.n_boot, "seed": SEED}
    t = time.perf_counter()
    if a.module in ("h11", "h7", "all"):
        vt = _load_threshold_script()
        ev, spot = build_events(vt)
        print(
            f"events: {len(ev)} (asset, t0) with q and S0; markets={sum(len(e['mk']) for e in ev.values())}; {time.perf_counter() - t:.0f}s rss={rss_mb():.0f}MB",
            flush=True,
        )
        if a.module in ("h11", "all"):
            run_h11(ev, spot, a.n_boot, res)
        if a.module in ("h7", "all"):
            run_h7(ev, spot, a.n_boot, res)
    if a.module in ("h8", "all"):
        db = out / "tradedesk_copy.sqlite"
        if a.refresh_db or not db.exists():
            copy_db(db)
        run_h8(db, a.n_boot, res)
    (out / f"result_{a.module}.json").write_text(json.dumps(res, indent=1, default=str))
    print_report(res)
    print(f"\ndone {time.perf_counter() - t:.0f}s peak rss={rss_mb():.0f}MB -> {out}/result_{a.module}.json", flush=True)


if __name__ == "__main__":
    main()
