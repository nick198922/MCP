"""Verify IDEA-20261024-toptrader-vs-crowd-divergence: does top-trader vs crowd long/short divergence rank next-week
returns of Binance USDT-M perps? Pre-registration: the card's section "Попередня реєстрація".

Stages (network only through ``rt.binance_vision`` / ``rt.binance_futures`` on ``core.http.Http``):
    plan         Sunday snapshots needed by the xs-factor-ic-panel universe (no network)
    fetch        archive ``metrics`` file of each needed (symbol, Sunday) -> per-file JSON snapshot cache
    consistency  archive count_long_short_ratio vs the live globalLongShortAccountRatio API (last ~40 h)
    stage0       power gate (counts, SD / SE / MDE of the weekly IC; never the mean)
    full         step 1: primary IC7 test and every registered check

Inputs: the panel of scripts/verify_xs_factor_ic_panel.py (``--panel``) and the kline cache of
scripts/verify_funding_crowding.py (``--data-dir``, for 3-day returns). Outputs under ``--cache-dir``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
import verify_funding_crowding as fc  # noqa: E402
import verify_liquidation_flush as vlf  # noqa: E402  (per-file JSON Fetcher)

from tradedesk.analytics.funding_crowding import block_bootstrap_ci, newey_west_se  # noqa: E402
from tradedesk.analytics.listing_drift import holm  # noqa: E402
from tradedesk.analytics.per_asset import heterogeneity, permutation_q, persistence  # noqa: E402
from tradedesk.analytics.toptrader_divergence import (  # noqa: E402
    DAY_MS,
    day_snapshot,
    divergence,
    gate,
    hit_rate,
    placebo_within_asset,
    trailing_z,
    weekly_ic,
)
from tradedesk.analytics.xs_factors import ic_products, partial_rank_ic, two_sided_p, without_top  # noqa: E402
from tradedesk.server import build_server  # noqa: E402

COLS = ("sym", "R7", "M28", "MAX30", "IVOL30", "beta", "fund", "mqv", "r", "r_lag1", "fund_long", "trunc")
IX = {c: i for i, c in enumerate(COLS)}
WINDOW, MIN_OBS = 13, 10
MIN_N = 30
NW_LAG = 4
SEED = 7
OOS_DAY = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp()) // 86400
CONTROLS = ("beta", "R7", "fund", "IVOL30", "MAX30", "mqv")
WARM = 26  # Sundays of history kept before a panel week (covers the 26-week sensitivity window)


def rss() -> str:
    return f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


def load_panel(p: Path) -> list[dict[str, Any]]:
    return fc.rd(p)["weeks"]


def needed(weeks: list[dict[str, Any]], warm: int) -> set[tuple[str, int]]:
    out = set()
    for w in weeks:
        for r in w["rows"]:
            for k in range(warm):
                out.add((r[0], w["day"] - 1 - 7 * k))
    return out


# ---------------------------------------------------------------- fetch


async def stage_fetch(panel: Path, cache: Path, sample: int | None, warm: int) -> None:
    need = sorted(needed(load_panel(panel), warm), key=lambda x: (x[1], x[0]))
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
                snap = day_snapshot(rows, d)
                return {"n_rows": len(rows), "snap": snap}

            jobs.append((cache / "snap" / s / f"{ymd(d)}.json", go))
        t0 = time.time()
        await fx.many("metrics", jobs)
        print(f"FETCH COMPLETE {len(jobs)} files in {time.time() - t0:.0f}s; {rss()}", flush=True)
    finally:
        await rt.aclose()


async def stage_consistency(cache: Path) -> None:
    _, rt = build_server()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        api = (await rt.binance_futures.long_short_account_ratio("BTCUSDT", "5m", 500)).data
        amap = {int(x["timestamp"]): float(x["longShortRatio"]) for x in api}
        days = sorted({ts // DAY_MS for ts in amap})
        print(f"API rows {len(amap)}: {datetime.fromtimestamp(min(amap) / 1000, UTC)} .. {datetime.fromtimestamp(max(amap) / 1000, UTC)}")
        for d in days[:-1]:
            try:
                rows = (await rt.binance_vision.um_metrics_ratios_daily("BTCUSDT", ymd(d))).data
            except Exception as e:  # noqa: BLE001  (archive file of the last day may not exist yet)
                print(f"  {ymd(d)}: archive unavailable ({e})")
                continue
            for lag in (0, 5):
                diffs = [abs(r[5] - amap[int(r[0]) + lag * 60_000]) for r in rows if int(r[0]) + lag * 60_000 in amap]
                exact = sum(x < 1e-6 for x in diffs)
                print(
                    f"  {ymd(d)} archive create_time + {lag} min vs API timestamp: matched {len(diffs)}, equal {exact}, max |diff| {max(diffs) if diffs else float('nan'):.6f}"
                )
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- signal panel


def snaps(cache: Path, sym: str) -> dict[int, dict[str, float]]:
    out = {}
    d = cache / "snap" / sym
    if not d.exists():
        return out
    for p in d.glob("*.json"):
        v = fc.rd(p)
        if v and v.get("snap"):
            out[int(datetime.fromisoformat(p.stem).replace(tzinfo=UTC).timestamp()) // 86400] = v["snap"]
    return out


def signal_table(weeks: list[dict[str, Any]], cache: Path) -> dict[str, dict[str, dict[int, float]]]:
    """variant -> sym -> Monday -> signal."""
    syms = sorted({r[0] for w in weeks for r in w["rows"]})
    first, last = weeks[0]["day"] - 1 - 7 * (WARM - 1), weeks[-1]["day"] - 1
    grid = list(range(first, last + 1, 7))
    out: dict[str, dict[str, dict[int, float]]] = defaultdict(dict)
    for s in syms:
        sn = snaps(cache, s)
        top = [sn[d]["top_pos"] if d in sn else None for d in grid]
        tacc = [sn[d]["top_acc"] if d in sn else None for d in grid]
        crowd = [sn[d]["crowd"] if d in sn else None for d in grid]
        var = {
            "D": divergence(top, crowd, window=WINDOW, min_obs=MIN_OBS),
            "D_w8": divergence(top, crowd, window=8, min_obs=6),
            "D_w26": divergence(top, crowd, window=26, min_obs=20),
            "D_acc": divergence(tacc, crowd, window=WINDOW, min_obs=MIN_OBS),
            "raw": [math.log(a) - math.log(b) if a and b else None for a, b in zip(top, crowd, strict=True)],
        }
        zt = trailing_z([math.log(v) if v else None for v in top], window=WINDOW, min_obs=MIN_OBS)
        zc = trailing_z([math.log(v) if v else None for v in crowd], window=WINDOW, min_obs=MIN_OBS)
        var["z_top"] = zt
        var["-z_crowd"] = [-v if v is not None else None for v in zc]
        for name, vals in var.items():
            out[name][s] = {d + 1: v for d, v in zip(grid, vals, strict=True) if v is not None}
    return out


def r3_table(weeks: list[dict[str, Any]], data_dir: Path) -> dict[tuple[str, int], float]:
    data = fc.load_all(data_dir)
    out = {}
    for w in weeks:
        t = w["day"]
        for r in w["rows"]:
            d = data.get(r[0])
            if d is None or t not in d.bars.open:
                continue
            c = d.bars.close_on_or_before(t + 2, t)
            if c is not None and d.bars.open[t] > 0:
                out[(r[0], t)] = d.bars.close[c] / d.bars.open[t] - 1.0
    del data
    return out


def cross_sections(
    weeks: list[dict[str, Any]], sig: dict[str, dict[int, float]], r3: dict[tuple[str, int], float] | None = None
) -> list[dict[str, Any]]:
    """Per week: rows with the signal; weeks with fewer than MIN_N names dropped."""
    out = []
    for w in weeks:
        t = w["day"]
        rows = [(r, sig[r[0]][t]) for r in w["rows"] if r[0] in sig and t in sig[r[0]] and r[IX["r"]] is not None]
        if len(rows) < MIN_N:
            continue
        out.append({"day": t, "rows": rows, "r3": [r3.get((r[0], t)) if r3 is not None else None for r, _ in rows]})
    return out


def ic_series(xs: list[dict[str, Any]], horizon: str = "r7", ctrl: tuple[str, ...] = ()) -> list[float]:
    out = []
    for w in xs:
        if horizon == "r7":
            pairs = [(d, r[IX["r"]], r) for r, d in w["rows"]]
        else:
            pairs = [(d, y, r) for (r, d), y in zip(w["rows"], w["r3"], strict=True) if y is not None]
        if len(pairs) < MIN_N:
            out.append(float("nan"))
            continue
        x, y = [p[0] for p in pairs], [p[1] for p in pairs]
        if ctrl:
            cs = [[math.log(p[2][IX[c]]) if c == "mqv" else p[2][IX[c]] for p in pairs] for c in ctrl]
            out.append(partial_rank_ic(x, y, cs))
        else:
            out.append(weekly_ic(x, y))
    return out


def clean(xs: list[float]) -> list[float]:
    return [x for x in xs if math.isfinite(x)]


# ---------------------------------------------------------------- stage 0


def stage0(panel: Path, cache: Path, out_dir: Path) -> dict[str, Any]:
    weeks = load_panel(panel)
    sig = signal_table(weeks, cache)
    nfiles = sum(1 for _ in (cache / "snap").rglob("*.json"))
    have = Counter()
    nrow = []
    for p in (cache / "snap").rglob("*.json"):
        v = fc.rd(p)
        have["404" if v is None else ("ok" if v.get("snap") else "no_snapshot")] += 1
        if v:
            nrow.append(v["n_rows"])
    print(
        f"snapshot files {nfiles}: {dict(have)}; rows per file median {statistics.median(nrow) if nrow else 0}, share full (>=288) {sum(n >= 288 for n in nrow) / max(len(nrow), 1):.1%}"
    )
    xs = cross_sections(weeks, sig["D"])
    per_coin = Counter(r[0] for w in xs for r, _ in w["rows"])
    long_coins = sorted(s for s, n in per_coin.items() if n >= 104)
    ns = [len(w["rows"]) for w in xs]
    panel_rows = sum(len(w["rows"]) for w in weeks)
    print(
        f"panel weeks {len(weeks)}, symbol-weeks {panel_rows}; with D: weeks (N >= {MIN_N}) {len(xs)} ({ymd(xs[0]['day'])}..{ymd(xs[-1]['day'])}), coin-weeks {sum(ns)} ({sum(ns) / panel_rows:.1%} of panel)"
    )
    print(
        f"N per week: min {min(ns)}, median {statistics.median(ns)}, max {max(ns)}; coins total {len(per_coin)}, with >= 104 weeks of D: {len(long_coins)}"
    )
    print(f"weeks IS (< 2024) {sum(w['day'] < OOS_DAY for w in xs)}, OOS {sum(w['day'] >= OOS_DAY for w in xs)}")
    for y in sorted({ymd(w["day"])[:4] for w in xs}):
        sub = [len(w["rows"]) for w in xs if ymd(w["day"])[:4] == y]
        print(f"  {y}: weeks {len(sub)}, median N {statistics.median(sub)}")
    # signal levels (no returns)
    allv = sorted(v for s in sig["D"].values() for v in s.values())
    print(f"D p1 {allv[len(allv) // 100]:+.2f} p50 {allv[len(allv) // 2]:+.2f} p99 {allv[99 * len(allv) // 100]:+.2f} (n {len(allv)})")
    ics = clean(ic_series(xs))
    se_nw = newey_west_se(ics, NW_LAG)
    g = gate(len(long_coins), ics, se_nw)
    rng = np.random.default_rng(SEED)  # null SD: random signal against the same returns
    null = [weekly_ic(list(rng.normal(size=len(w["rows"]))), [r[IX["r"]] for r, _ in w["rows"]]) for w in xs]
    print(
        f"STAGE 0 (no means): IC7 weeks {g['n_weeks']}, SD {g['sd']:.4f}, SE_iid {g['sd'] / math.sqrt(g['n_weeks']):.4f}, SE_NW {se_nw:.4f}, "
        f"MDE = 2.8 x max = {g['mde']:.4f}; null SD (random signal) {statistics.stdev(null):.4f}; "
        f"gate (>= 12 coins with >= 2 y and MDE <= 0.025): {'PASS' if g['pass'] else 'FAIL -> inconclusive'}"
    )
    res = {**g, "se_nw": se_nw, "long_coins": len(long_coins), "coins": len(per_coin), "coin_weeks": sum(ns), "weeks": len(xs)}
    fc.wr(out_dir / "stage0.json", res)
    print(rss())
    return res


# ---------------------------------------------------------------- full


def summary(label: str, xs: list[float]) -> str:
    xs = clean(xs)
    m = statistics.fmean(xs)
    se = newey_west_se(xs, NW_LAG)
    t = m / se if se > 0 else float("nan")
    return f"  {label:<42} n={len(xs):>3} IC {m:+.4f} SE_NW {se:.4f} t {t:+.2f} p {two_sided_p(t):.4f}"


def stage_full(panel: Path, cache: Path, data_dir: Path, out_dir: Path, n_perm: int) -> None:
    s0 = fc.rd(out_dir / "stage0.json")
    if not s0["pass"]:
        raise SystemExit("stage 0 gate failed: no step 1")
    weeks = load_panel(panel)
    sig = signal_table(weeks, cache)
    r3 = r3_table(weeks, data_dir)
    print(f"r3 legs {len(r3)}; {rss()}", flush=True)
    xs = cross_sections(weeks, sig["D"], r3)
    days = [w["day"] for w in xs]
    ic7, ic3 = ic_series(xs), ic_series(xs, "r3")
    lines = ["PRIMARY (pre-registered)"]
    res: dict[str, Any] = {}
    for name, v in (("IC7", ic7), ("IC3", ic3)):
        v = clean(v)
        m, se = statistics.fmean(v), newey_west_se(v, NW_LAG)
        lo, hi = block_bootstrap_ci(v, block=4, n_boot=10_000, seed=SEED)
        res[name] = {"n": len(v), "mean": m, "se": se, "p": two_sided_p(m / se), "ci": (lo, hi)}
    hp = holm([res["IC7"]["p"], res["IC3"]["p"]])
    for (name, r), h in zip(res.items(), hp, strict=True):
        r["p_holm"] = h
        lines.append(
            f"  {name}: n={r['n']} mean {r['mean']:+.4f} SE_NW {r['se']:.4f} p {r['p']:.4f} p_Holm {h:.4f} boot95 [{r['ci'][0]:+.4f}; {r['ci'][1]:+.4f}]"
        )
    isv = clean([x for x, d in zip(ic7, days, strict=True) if d < OOS_DAY])
    oov = clean([x for x, d in zip(ic7, days, strict=True) if d >= OOS_DAY])
    lines += [summary("IC7 IS 2022-2023", isv), summary("IC7 OOS 2024+", oov)]
    for y in sorted({ymd(d)[:4] for d in days}):
        lines.append(summary(f"IC7 {y}", [x for x, d in zip(ic7, days, strict=True) if ymd(d)[:4] == y]))
    lines.append(summary("IC3 OOS 2024+", [x for x, d in zip(ic3, days, strict=True) if d >= OOS_DAY]))
    part = ic_series(xs, ctrl=CONTROLS)
    lines.append(summary("IC7 partial (beta,R7,fund,IVOL30,MAX30,lnQV)", part))
    for c in CONTROLS:
        lines.append(summary(f"IC7 partial ({c} only)", ic_series(xs, ctrl=(c,))))
    c7 = clean(ic7)
    sign = 1.0 if statistics.fmean(c7) >= 0 else -1.0
    lines.append(summary("IC7 without 5 most favourable weeks", without_top(c7, 5, sign)))
    lines.append(summary("IC7 without 10 most favourable weeks", without_top(c7, 10, sign)))
    # placebo
    pw = [{r[0]: (d, r[IX["r"]]) for r, d in w["rows"]} for w in xs]
    t0 = time.time()
    null = placebo_within_asset(pw, n_perm, seed=SEED)
    obs = statistics.fmean(c7)
    lines.append(
        f"  placebo (dates of D shuffled within coin, {n_perm} perms, {time.time() - t0:.0f}s): null mean {np.mean(null):+.4f} "
        f"p2.5 {np.quantile(null, 0.025):+.4f} p97.5 {np.quantile(null, 0.975):+.4f}; share null >= observed {np.mean(null >= obs):.3f}"
    )
    # sensitivity
    lines.append("SENSITIVITY (all variants; not used for the verdict)")
    for v in ("D_w8", "D_w26", "D_acc", "raw", "z_top", "-z_crowd"):
        xv = cross_sections(weeks, sig[v])
        lines.append(summary(f"{v} IC7", ic_series(xv)))
        lines.append(summary(f"{v} IC7 OOS", [x for x, w in zip(ic_series(xv), xv, strict=True) if w["day"] >= OOS_DAY]))
    # BTC / ETH descriptive hit rates
    lines.append("BTC/ETH (descriptive, excluded from Holm)")
    for s in ("BTCUSDT", "ETHUSDT"):
        pr = [(sig["D"][s][t], r3[(s, t)]) for t in sorted(sig["D"].get(s, {})) if (s, t) in r3]
        hr, n = hit_rate([a for a, _ in pr], [b for _, b in pr])
        pr7 = [(d, r[IX["r"]]) for w in xs for r, d in w["rows"] if r[0] == s]
        hr7, n7 = hit_rate([a for a, _ in pr7], [b for _, b in pr7])
        lines.append(
            f"  {s}: sign(D) == sign(r3) {hr:.3f} (n {n}, SE {math.sqrt(0.25 / max(n, 1)):.3f}); sign(D) == sign(r7) {hr7:.3f} (n {n7})"
        )
    # per-asset (5a)
    vals, assets, clus, ism = [], [], [], []
    for w in xs:
        d = [x for _, x in w["rows"]]
        y = [r[IX["r"]] for r, _ in w["rows"]]
        for (r, _), c in zip(w["rows"], ic_products(d, y), strict=True):
            vals.append(float(c))
            assets.append(r[0])
            clus.append(w["day"])
            ism.append(w["day"] < OOS_DAY)
    het = heterogeneity(vals, assets, min_n=52)
    pq = permutation_q(vals, assets, clus, min_n=52, n_perm=1000, seed=SEED)
    per = persistence(vals, assets, clus, ism, min_n_is=26, top_n=10, n_boot=2000, seed=SEED)
    lines.append("PER-ASSET (contribution z(rank D) * z(rank r7); asset mean = its share of the IC)")
    lines.append(
        f"  Cochran Q k={het['k']} Q {het['q']:.1f} df {het['df']} p {het['p']} I2 {het['i2']:.3f}; permutation within weeks: p {pq['p']}, null p95 {pq['null_p95']:.1f}"
    )
    lines.append(
        f"  persistence top-10 IS -> OOS: top OOS {per['top_oos_mean']:+.4f} vs rest {per['rest_oos_mean']:+.4f}, diff {per['diff']:+.4f} "
        f"CI {per['diff_ci']}, p {per['diff_p']}; rho IS/OOS {per['rho']:+.3f} (n {per['rho_n']})"
    )
    st = defaultdict(list)
    for a, v in zip(assets, vals, strict=True):
        st[a].append(v)
    big = sorted(
        ((a, len(v), statistics.fmean(v), statistics.stdev(v) / math.sqrt(len(v))) for a, v in st.items() if len(v) >= 150),
        key=lambda x: -x[1],
    )
    full_lines = [f"  {a:<14} n {n:>3} mean {m:+.4f} CI [{m - 1.96 * se:+.4f}; {m + 1.96 * se:+.4f}]" for a, n, m, se in big]
    lines.append(f"  assets with >= 150 weeks: {len(big)} (table in full.txt)")
    txt = "\n".join(lines)
    print(txt)
    (out_dir / "full.txt").write_text(
        txt + "\nPER-ASSET TABLE\n" + "\n".join(full_lines) + "\nTOP10 IS\n" + json.dumps(per["is_ranked"][:10]) + "\n"
    )
    fc.wr(out_dir / "full.json", {k: {kk: vv for kk, vv in v.items()} for k, v in res.items()})
    print(rss())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=("plan", "fetch", "consistency", "stage0", "full"))
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/toptrader_divergence")
    ap.add_argument("--panel", type=Path, default=Path.home() / ".cache/tradedesk-agents/xs_factor_ic_panel/panel.json")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--perm", type=int, default=1000)
    a = ap.parse_args()
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "plan":
        weeks = load_panel(a.panel)
        n13, n26 = needed(weeks, 13), needed(weeks, WARM)
        print(
            f"weeks {len(weeks)}, symbols {len({r[0] for w in weeks for r in w['rows']})}; files needed: 13-week window {len(n13)}, {WARM}-week {len(n26)}"
        )
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(a.panel, cache, a.sample, WARM))
    elif a.stage == "consistency":
        asyncio.run(stage_consistency(cache))
    elif a.stage == "stage0":
        stage0(a.panel, cache, cache)
    else:
        stage_full(a.panel, cache, a.data_dir, cache, a.perm)


if __name__ == "__main__":
    main()
