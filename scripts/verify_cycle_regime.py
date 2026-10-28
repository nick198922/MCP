"""Step 3 of IDEA-20261028-btc-cycle-regime-gauge: one pre-registered history test of the daily cycle states.

Pre-registration: the card's section «Попередня реєстрація». States come from `analytics.cycle_regime` unchanged.

    uv run python scripts/verify_cycle_regime.py fetch            # Binance spot / USD-M 1d klines + funding -> cache
    uv run python scripts/verify_cycle_regime.py analyse --out F  # states, effects, bootstrap, placebo, robustness

Network only through the runtime providers (build_server -> rt.binance_spot, rt.binance_futures, rt.exchanges); every
response is written to its own JSON file under --cache-dir (default ~/.cache/tradedesk-agents/cycle_regime/).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import resource
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics import cycle_regime as cr
from tradedesk.analytics import cycle_regime_eval as ev
from tradedesk.analytics.market_flow import points
from tradedesk.analytics.risk_overlay import holm

DEFAULT_CACHE = Path.home() / ".cache" / "tradedesk-agents" / "cycle_regime"
D = cr.DAY_MS
SPOT_FROM = "2017-08-01"
PERP_FROM = "2019-09-01"
SAMPLE_FROM = "2019-09-25"
SAMPLE_TO = "2026-09-30"
SPLIT = "2023-04-01"
HORIZONS = (30, 90)
BLOCK = {30: 2, 90: 4}
K_MIN = 90
MERGE_GAP = 30
CRASH = -0.15
TARGET = 0.04
SEED = 20261028
REPS = 5000
DIRECTION = {"bottom": 1, "accumulation": 1, "overheat": -1, "bottom+accumulation": 1}


def ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


def day(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).date().isoformat()


def log(msg: str) -> None:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"[{time.strftime('%H:%M:%S')}] {msg} (peak RSS {rss:.0f} MB)", flush=True)


# ---------- fetch ----------


async def fetch(cache: Path) -> None:
    from tradedesk.server import build_server

    _, rt = build_server()
    end = int(time.time() * 1000) // D * D  # open of the day in progress: bars before it are closed
    jobs = {
        "spot_1d.json": lambda: rt.binance_spot.klines_range("BTCUSDT", "1d", ms(SPOT_FROM), end, max_pages=10),
        "perp_1d.json": lambda: rt.binance_futures.klines_range("BTCUSDT", "1d", ms(PERP_FROM), end, max_pages=6),
        "funding.json": lambda: rt.exchanges.funding_rate_history_range(
            "binanceusdm", "BTC/USDT:USDT", ms(PERP_FROM), end + cr.SETTLE_SLACK_MS, max_pages=80, trust_short_page=True
        ),
    }
    try:
        for name, go in jobs.items():
            path = cache / name
            if path.exists():
                log(f"{name}: cached")
                continue
            t0 = time.perf_counter()
            got = (await go()).data
            rows = got.get("rows") or []
            if name == "funding.json":
                rows = [[int(r["timestamp"]), r.get("fundingRate")] for r in rows if r.get("timestamp") is not None]
            else:
                rows = [r[:6] for r in rows]
            path.write_text(json.dumps({"complete": got.get("complete"), "fetched_to": end, "rows": rows}))
            log(f"{name}: {len(rows)} rows, complete={got.get('complete')}, {time.perf_counter() - t0:.1f}s")
    finally:
        await rt.aclose()


# ---------- analyse ----------


def load(cache: Path) -> dict[str, Any]:
    spot_j = json.loads((cache / "spot_1d.json").read_text())
    perp_j = json.loads((cache / "perp_1d.json").read_text())
    fund_j = json.loads((cache / "funding.json").read_text())
    funding = points(({"t": r[0], "r": r[1]} for r in fund_j["rows"]), "t", "r", lo=-0.1, hi=0.1)
    return {
        "spot": cr.parse_closes(spot_j["rows"]),
        "perp": cr.parse_closes(perp_j["rows"]),
        "funding": funding,
        "complete": {"spot": spot_j["complete"], "perp": perp_j["complete"], "funding": fund_j["complete"]},
    }


def sanity(data: dict[str, Any], series: list[dict[str, Any]]) -> dict[str, Any]:
    spot, perp, funding = data["spot"], data["perp"], data["funding"]
    out: dict[str, Any] = {"complete": data["complete"]}
    for name, s in (("spot", spot), ("perp", perp)):
        ts = sorted(s)
        gaps = [(day(a), day(b)) for a, b in zip(ts, ts[1:], strict=False) if b - a != D]
        out[name] = {"first": day(ts[0]), "last": day(ts[-1]), "n": len(ts), "gaps": gaps[:10], "n_gaps": len(gaps)}
    common = [t for t in perp if t in spot]
    basis = [perp[t] / spot[t] - 1 for t in common]
    out["basis_daily"] = {
        "n": len(basis),
        "min": min(basis),
        "max": max(basis),
        "n_abs_over_2pct": sum(1 for b in basis if abs(b) > 0.02),
    }
    ft = [t for t, _ in funding]
    steps = [(b - a) / 3_600_000 for a, b in zip(ft, ft[1:], strict=False)]
    out["funding"] = {
        "first": day(ft[0]),
        "last": day(ft[-1]),
        "n": len(ft),
        "median_interval_h": statistics.median(steps),
        "n_steps_over_9h": sum(1 for s in steps if s > 9),
        "max_step_h": max(steps),
        "min_rate": min(r for _, r in funding),
        "max_rate": max(r for _, r in funding),
    }
    out["undecided_days"] = sum(1 for r in series if r["state"] is None)
    return out


def control_day(data: dict[str, Any]) -> dict[str, Any] | None:
    """Components of 2026-10-27 vs the card's snapshot (spot 80 737, SMA365 77 407, funding7 6.3 %, RSI14 44.8)."""
    t = ms("2026-10-27")
    if t not in data["spot"]:
        return None
    rows = ev.state_series(data["spot"], data["perp"], data["funding"], t, t)
    r = rows[0]
    sma = r["close"] / (1 + r["price_to_sma"]) if r["price_to_sma"] is not None else None
    return {
        "state": r["state"],
        "close": r["close"],
        "sma365": sma,
        "funding7": r["funding7"],
        "rsi14": r["rsi14"],
        "basis7": r["basis7"],
        "drawdown": r["drawdown_365d"],
    }


def effect(
    y: np.ndarray, mask: np.ndarray, months: np.ndarray, h: int, direction: int, *, vs_rest: bool = False, block: int | None = None
) -> dict[str, Any]:
    bs = ev.month_block_bootstrap(y, mask, months, block=block or BLOCK[h], reps=REPS, seed=SEED, vs_rest=vs_rest)
    pl = ev.shift_placebo(y, mask, k_min=K_MIN, direction=direction, vs_rest=vs_rest)
    return {
        "n_days": int(mask.sum()),
        "mean_in": float(y[mask].mean()) if mask.any() else None,
        "obs": pl["obs"],
        "lo": bs["lo"],
        "hi": bs["hi"],
        "share_empty": bs["share_empty"],
        "p": pl["p"],
        "n_shifts": pl["n_shifts"],
        "placebo_mean": pl["placebo_mean"],
    }


def masks(rows: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    st = [r["state"] for r in rows]
    out = {s: np.asarray([x == s for x in st]) for s in ("bottom", "accumulation", "overheat", "growth", "decline")}
    out["bottom+accumulation"] = out["bottom"] | out["accumulation"]
    return out


def episode_table(
    rows_all: list[dict[str, Any]], fwd: dict[int, dict[int, float]], state_mask: list[bool], gap: int
) -> list[dict[str, Any]]:
    days = [r["t"] for r in rows_all]
    eps = ev.episodes(days, state_mask, gap)
    out = []
    for a, b, n in eps:
        out.append({"from": day(a), "to": day(b), "n_days": n, **{f"r{h}_first": fwd[h].get(a) for h in HORIZONS}})
    return out


def analyse(cache: Path, out_path: Path | None) -> None:
    data = load(cache)
    log(f"loaded: spot {len(data['spot'])}, perp {len(data['perp'])}, funding {len(data['funding'])}; complete {data['complete']}")
    t0 = time.perf_counter()
    lo_t, hi_t = ms(SAMPLE_FROM), ms(SAMPLE_TO)
    series = ev.state_series(data["spot"], data["perp"], data["funding"], lo_t, hi_t)
    log(f"states for {len(series)} days in {time.perf_counter() - t0:.1f}s")
    report: dict[str, Any] = {"version": cr.VERSION, "sample": [SAMPLE_FROM, SAMPLE_TO]}
    report["sanity"] = sanity(data, series)
    report["control_2026_10_27"] = control_day(data)
    decided = [r for r in series if r["state"] is not None]
    report["state_days"] = {s: sum(1 for r in decided if r["state"] == s) for s in cr.STATES}

    fwd = {h: ev.forward_returns(data["spot"], h, hi_t) for h in HORIZONS}
    # episodes over all decided days of the sample (horizon-free)
    mk_all = masks(decided)
    report["episodes"] = {
        f"gap{g}": {
            s: episode_table(decided, fwd, list(mk_all[s]), g) for s in ("bottom", "accumulation", "overheat", "bottom+accumulation")
        }
        for g in (7, MERGE_GAP, 60)
    }

    results: dict[str, Any] = {}
    fam_keys, fam_p = [], []
    for h in HORIZONS:
        rows = [r for r in decided if r["t"] in fwd[h]]
        y = np.asarray([fwd[h][r["t"]] for r in rows])
        months = ev.month_index([r["t"] for r in rows])
        above = np.asarray([r["price_to_sma"] >= 0 for r in rows])
        mk = masks(rows)
        resid = ev.trend_residual(y, above)
        crash = (y <= CRASH).astype(float)
        hr: dict[str, Any] = {"n_days": len(rows), "uncond_mean": float(y.mean()), "uncond_sd": float(y.std(ddof=1))}
        y_nov = y[::h]
        hr["sd_nonoverlap"] = float(np.std(y_nov, ddof=1))
        hr["n_nonoverlap"] = int(y_nov.size)
        hr["sma_rule"] = effect(y, above, months, h, 1, vs_rest=True)
        hr["mean_above"] = float(y[above].mean())
        hr["mean_below"] = float(y[~above].mean())
        for s in ("bottom", "accumulation", "overheat", "bottom+accumulation", "growth", "decline"):
            d = DIRECTION.get(s, 1)
            e = effect(y, mk[s], months, h, d)
            e["trend_excess"] = effect(resid, mk[s], months, h, d)
            if s == "overheat":
                e["crash"] = effect(crash, mk[s], months, h, 1)
                e["crash"]["p_in"] = float(crash[mk[s]].mean()) if mk[s].any() else None
                e["crash"]["p_all"] = float(crash.mean())
            # halves
            first = np.asarray([r["t"] < ms(SPLIT) for r in rows])
            e["halves"] = {
                "first": ev.masked_diff(y[first], mk[s][first]),
                "second": ev.masked_diff(y[~first], mk[s][~first]),
                "n_first": int((mk[s] & first).sum()),
                "n_second": int((mk[s] & ~first).sum()),
            }
            e["block_grid"] = {b: ev.month_block_bootstrap(y, mk[s], months, block=b, reps=REPS, seed=SEED) for b in (1, 2, 4, 6)}
            # without the episode contributing most in the hypothesis direction
            eps = ev.episodes([r["t"] for r in rows], list(mk[s]), MERGE_GAP)
            if len(eps) >= 2:
                ts = np.asarray([r["t"] for r in rows])
                contrib = [d * float((y[(ts >= a) & (ts <= b) & mk[s]] - y.mean()).sum()) for a, b, _ in eps]
                j = int(np.argmax(contrib))
                a, b, _ = eps[j]
                keep = ~((ts >= a) & (ts <= b) & mk[s])
                e["without_top_episode"] = {"dropped": [day(a), day(b)], "obs": ev.masked_diff(y, mk[s] & keep)}
            e["n_episodes"] = len(eps)
            ep_first = [fwd[h][a] for a, _, _ in eps if a in fwd[h]]
            e["episode_level"] = ev.t_ci([x - float(y.mean()) for x in ep_first])
            e["mde_episodes"] = ev.mde(hr["sd_nonoverlap"], len(eps))
            hr[s] = e
            if s in ("bottom", "accumulation", "overheat"):
                fam_keys.append((h, s))
                fam_p.append(e["p"] if e["p"] is not None else 1.0)
        results[str(h)] = hr
        log(f"h={h}: done")
    adj = holm(fam_p)
    for (h, s), pa in zip(fam_keys, adj, strict=True):
        results[str(h)][s]["p_holm"] = pa
    report["results"] = results
    print_summary(report)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=1, default=str))
        log(f"full output -> {out_path}")


def pp(x: float | None, nd: int = 1) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{nd}f}"


def print_summary(rep: dict[str, Any]) -> None:
    s = rep["sanity"]
    print("== sanity ==")
    print(
        f"complete {s['complete']}; spot {s['spot']['first']}..{s['spot']['last']} n={s['spot']['n']} gaps={s['spot']['n_gaps']}; "
        f"perp {s['perp']['first']}..{s['perp']['last']} n={s['perp']['n']} gaps={s['perp']['n_gaps']}"
    )
    b = s["basis_daily"]
    print(f"daily basis perp/spot-1: min {pp(b['min'], 3)} % max {pp(b['max'], 3)} %, |b|>2 %: {b['n_abs_over_2pct']} of {b['n']}")
    f = s["funding"]
    print(
        f"funding {f['first']}..{f['last']} n={f['n']} median interval {f['median_interval_h']:.1f} h, steps>9h {f['n_steps_over_9h']} "
        f"(max {f['max_step_h']:.1f} h), rate {f['min_rate']:.5f}..{f['max_rate']:.5f}; undecided days {s['undecided_days']}"
    )
    print(f"control 2026-10-27: {rep['control_2026_10_27']}")
    print(f"state days: {rep['state_days']}")
    print("== episodes (gap 30 d) ==")
    for st, eps in rep["episodes"][f"gap{MERGE_GAP}"].items():
        print(
            f"{st}: {len(eps)} episodes: "
            + "; ".join(f"{e['from']}..{e['to']} ({e['n_days']}d, R30 {pp(e['r30_first'])}, R90 {pp(e['r90_first'])})" for e in eps)
        )
    for g in (7, 60):
        print(f"episodes gap {g}: " + ", ".join(f"{st} {len(e)}" for st, e in rep["episodes"][f"gap{g}"].items()))
    print("== effects (pp; delta = mean in state - unconditional; CI month-block bootstrap; p shift placebo) ==")
    for h, hr in rep["results"].items():
        print(
            f"-- h={h}: n={hr['n_days']} uncond {pp(hr['uncond_mean'])} sd {pp(hr['uncond_sd'])} sd_nonoverlap {pp(hr['sd_nonoverlap'])} (n {hr['n_nonoverlap']})"
        )
        r = hr["sma_rule"]
        print(
            f"   SMA rule above-below: {pp(r['obs'])} [{pp(r['lo'])}, {pp(r['hi'])}] p={r['p']:.3f} (above {pp(hr['mean_above'])}, below {pp(hr['mean_below'])})"
        )
        for st in ("bottom", "accumulation", "overheat", "bottom+accumulation", "growth", "decline"):
            e = hr[st]
            te = e["trend_excess"]
            ph = f" holm={e['p_holm']:.3f}" if "p_holm" in e else ""
            p = "—" if e["p"] is None else f"{e['p']:.3f}"
            tp = "—" if te["p"] is None else f"{te['p']:.3f}"
            el = e["episode_level"]
            print(
                f"   {st:20s} days {e['n_days']:4d} ep {e['n_episodes']:2d} mean {pp(e['mean_in'])} delta {pp(e['obs'])} "
                f"[{pp(e['lo'])}, {pp(e['hi'])}] empty {e['share_empty']:.2f} p={p}{ph} | over-trend {pp(te['obs'])} "
                f"[{pp(te['lo'])}, {pp(te['hi'])}] p={tp} | ep-level {pp(el['mean'])} [{pp(el['lo'])}, {pp(el['hi'])}] "
                f"MDE {pp(e['mde_episodes'])}"
            )
            hv = e["halves"]
            wt = e.get("without_top_episode")
            grid = ", ".join(f"b{k}: [{pp(v['lo'])}, {pp(v['hi'])}]" for k, v in e["block_grid"].items())
            print(
                f"      halves {pp(hv['first'])} (n {hv['n_first']}) / {pp(hv['second'])} (n {hv['n_second']}); "
                f"w/o top ep {wt['dropped'] if wt else '—'} {pp(wt['obs']) if wt else '—'}; {grid}"
            )
            if "crash" in e:
                c = e["crash"]
                cp = "—" if c["p"] is None else f"{c['p']:.3f}"
                print(
                    f"      P(R{h}<=-15%): in {pp(c['p_in'])} vs all {pp(c['p_all'])}; diff {pp(c['obs'])} [{pp(c['lo'])}, {pp(c['hi'])}] p={cp}"
                )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["fetch", "analyse"])
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    if a.cmd == "fetch":
        a.cache_dir.mkdir(parents=True, exist_ok=True)
        asyncio.run(fetch(a.cache_dir))
    else:
        analyse(a.cache_dir, a.out)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
