"""Item 8g of IDEA-20261031-structural-edges-depth: entry filters of carry A against perp-leg delisting.

Stages (in order; each reads the cache of the previous one):
  fetch    daily (1d) perp and spot klines of every symbol (bench of item 2 + the delisted candidates of item 8b) from
           Binance Vision through rt.binance_vision, one JSON file per month under --cache-dir/vision_1d/ (404 -> null)
  run      (i), (ii), (iii) of item 8b with and without the filters F1 (perp age >= 180 d) and F2 (median 30-day spot
           quote volume >= $10M), cost models 'main' and 'stress', both settlement proxies -> results.json
  placebo  50 random symbol-blocking filters per F (main model, both settlements) -> placebo.json

Network only in `fetch` and only through rt.binance_vision; no keys, no orders. Returns on capital
(A: capital = 1.5 x notional). Pre-registration: the card, section "Пункт 8g". Data of item 8b are reused from
~/.cache/tradedesk-agents/carry_a_delisting/ (read only).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gc
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_carry_a_delisting as dl  # noqa: E402
import verify_carry_alts as alts  # noqa: E402

from tradedesk.analytics.carry_costs import CostModel, risk_summary, weekly_changes  # noqa: E402
from tradedesk.analytics.carry_delisting import simulate_carry_delisting, vanish_events  # noqa: E402
from tradedesk.analytics.carry_entry_filter import (  # noqa: E402
    block_symbols,
    first_traded_open,
    make_entry_filter,
    median_volume,
    random_blocked,
)
from tradedesk.analytics.carry_modules import block_bootstrap, diff_curve, holm  # noqa: E402
from tradedesk.core.errors import ProviderError  # noqa: E402

HOME = Path.home() / ".cache" / "tradedesk-agents"
DEFAULT_CACHE = HOME / "carry_a_entry_filter"
DL_CACHE = dl.DEFAULT_CACHE
CAPITAL = dl.CAPITAL
N_BOOT = 10_000
PERP_FROM = (2023, 9)
SPOT_FROM = (2024, 9)
LAST = (2026, 9)
AGE, VOL = 180.0, 10_000_000.0
FILTERS: dict[str, dict[str, float]] = {
    "F1": {"min_age_days": AGE},
    "F2": {"min_volume": VOL},
    "F1&F2": {"min_age_days": AGE, "min_volume": VOL},
    "F1x0.5": {"min_age_days": AGE * 0.5},
    "F1x2": {"min_age_days": AGE * 2},
    "F2x0.5": {"min_volume": VOL * 0.5},
    "F2x2": {"min_volume": VOL * 2},
}
STRESS_FILTERS = ("F1", "F2")
SETTLES = ("perp_last", "spot")
N_PLACEBO = 50
PLACEBO_SEED = 20261101

log = dl.log
fmt = dl.fmt


def months(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    y, m, out = a[0], a[1], []
    while (y, m) <= b:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def symbol_map() -> dict[str, tuple[str, str]]:
    """Market name (as in the simulations) -> (Binance perp id, Binance spot id)."""
    _, _, uni = dl.bench_window()
    out: dict[str, tuple[str, str]] = {}
    for name, (spot_sym, _mult) in uni.items():
        out[name] = (name.split(":")[0].replace("/", ""), str(spot_sym).replace("/", ""))
    c = json.loads((DL_CACHE / "candidates.json").read_text())
    for r in c["hot"]:
        out[f"{r['symbol'][:-4]}/USDT:USDT"] = (r["symbol"], r["spot"])
    return out


def kpath(cache: Path, kind: str, sym: str, y: int, m: int) -> Path:
    return cache / "vision_1d" / kind / sym / f"{y:04d}-{m:02d}.json"


def load_daily(cache: Path, kind: str, sym: str) -> list[list[float]]:
    d = cache / "vision_1d" / kind / sym
    rows: list[list[float]] = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        rows += json.loads(p.read_text()) or []
    rows.sort(key=lambda r: r[0])
    return rows


# ---------------------------------------------------------------- stage: fetch


async def stage_fetch(cache: Path, limit: int | None) -> None:
    from tradedesk.server import build_server

    jobs: list[tuple[str, str, int, int]] = []
    for perp_id, spot_id in sorted(set(symbol_map().values())):
        jobs += [("um", perp_id, y, m) for y, m in months(PERP_FROM, LAST)]
        jobs += [("spot", spot_id, y, m) for y, m in months(SPOT_FROM, LAST)]
    jobs = [j for j in jobs if not kpath(cache, *j).exists()]
    if limit:
        jobs = jobs[:limit]
    log(f"fetch: {len(jobs)} month files to get")
    _, rt = build_server()
    sem = asyncio.Semaphore(8)
    errors: dict[str, int] = {}
    done = 0
    t0 = time.time()

    async def one(kind: str, sym: str, y: int, m: int) -> None:
        nonlocal done
        async with sem:
            bv = rt.binance_vision
            go = bv.um_klines_monthly if kind == "um" else bv.spot_klines_monthly
            try:
                rows: list[list[float]] | None = (await go(sym, "1d", y, m)).data
            except ProviderError as e:
                if e.reason != "data_unavailable":
                    errors[e.reason] = errors.get(e.reason, 0) + 1
                    return  # transient: a rerun retries
                rows = None
            p = kpath(cache, kind, sym, y, m)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(rows))
            done += 1
            if done % 500 == 0:
                log(f"  {done}/{len(jobs)} files, {time.time() - t0:.0f} s, errors {errors}")

    try:
        await asyncio.gather(*(one(*j) for j in jobs))
    finally:
        await rt.aclose()
    log(f"fetch done: {done}/{len(jobs)} in {time.time() - t0:.0f} s, errors {errors}")


# ---------------------------------------------------------------- stage: run


def load_all(cache: Path) -> dict[str, Any]:
    start, end, uni = dl.bench_window()
    cal = json.loads(dl.CALIB.read_text())
    models = {k: CostModel(cal["models"][k]["fees"], cal["models"][k]["slippage"], cal["models"][k]["timing"]) for k in ("main", "stress")}
    bench = alts.load_markets(dl.BENCH, dl.DAYS, uni)  # type: ignore[arg-type]
    end = max(int(e["ts_ms"]) for d in bench.values() for e in d["funding"])
    bar_lo = min(b["ts"] for d in bench.values() for b in d["perp"][:1])
    bar_hi = max(b["ts"] for d in bench.values() for b in d["perp"][-1:])
    cand, not_measured_8b, _meta = dl.load_candidates(DL_CACHE, bar_lo, bar_hi)
    full = {**bench, **cand}
    pe = {s: e["perp_end"] for s, e in vanish_events(full, end, n_cycles=dl.N_MAIN).items()}
    smap = symbol_map()
    first: dict[str, int | None] = {}
    spot_daily: dict[str, list[list[float]]] = {}
    nm: list[dict[str, str]] = []
    coll: list[tuple[str, str, float]] = []
    for name in full:
        perp_id, spot_id = smap[name]
        pr = load_daily(cache, "um", perp_id)
        sr = load_daily(cache, "spot", spot_id)
        first[name] = first_traded_open(pr)
        if first[name] is None:
            nm.append({"symbol": name, "why": "no daily perp bar (F1)"})
        if not sr:
            nm.append({"symbol": name, "why": "no daily spot bar (F2)"})
        else:
            spot_daily[name] = sr
        # collisions: daily spot close vs the 4h spot close at the day end (4h close x mult in the sims)
        s4 = {b["ts"]: b["close"] for b in full[name]["spot"]}
        dev = []
        for r in sr:
            c4 = s4.get(int(r[0]) + 20 * 3_600_000)
            if c4 and r[4] > 0:
                dev.append(c4 / r[4])
        if dev:
            ratio = statistics.median(dev)
            coll.append((name, "spot", ratio))
    return {
        "start": start, "end": end, "models": models, "bench": bench, "cand": cand, "full": full, "pe": pe,
        "first": first, "spot_daily": spot_daily, "not_measured": nm, "not_measured_8b": not_measured_8b, "coll": coll,
    }  # fmt: skip


def stats(res: dict[str, Any], years: float, pe: dict[str, int]) -> dict[str, Any]:
    rk = risk_summary(res["equity"], years, CAPITAL, n_boot=N_BOOT)
    tr = res["trades"]
    return {
        **rk,
        "n_trades": len(tr),
        "n_perp_ended": sum(1 for t in tr if t.get("perp_ended")),
        "n_rule_closes": len(res.get("rule_closes", [])),
        "symbols": sorted({t["symbol"] for t in tr}),
    }


def diff(eq_a: list[tuple[int, float]], eq_b: list[tuple[int, float]], years: float) -> dict[str, Any]:
    d = diff_curve(eq_a, eq_b)
    rk = risk_summary(d, years, CAPITAL, n_boot=N_BOOT)
    bb = block_bootstrap([v for _, v in weekly_changes(d)], n_boot=N_BOOT)
    out = {"apr_on_capital": rk["apr_on_capital"], "ci": rk["apr_on_capital_ci"], "p": bb["p"]}
    for k, lo, hi in (("is", 0, dl.SPLIT_MS), ("oos", dl.SPLIT_MS, None)):
        from tradedesk.analytics.carry_modules import window_weekly

        b = block_bootstrap([v for _, v in window_weekly(d, lo, hi)], n_boot=N_BOOT)
        out[k] = None if b["apr"] is None else b["apr"] / CAPITAL
    return out


def trade_keys(res: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    return {(t["symbol"], t["open_ts"]): t for t in res["trades"]}


def stage_run(cache: Path) -> None:
    D = load_all(cache)
    full, bench, pe, end = D["full"], D["bench"], D["pe"], D["end"]
    log(
        f"bench {len(bench)}, candidates {len(D['cand'])}, perp ended {len(pe)}; not measured (8g) {len(D['not_measured'])}: "
        + ", ".join(f"{x['symbol']} ({x['why']})" for x in D["not_measured"])
    )
    bad = [(n, r) for n, _, r in D["coll"] if abs(r - 1) > 0.05 and abs(r / 1000 - 1) > 0.05 and abs(r / 1e6 - 1) > 0.05]
    log(f"collision check (daily spot close x mult vs 4h spot close): {len(D['coll'])} symbols, off by > 5 %: {bad[:10]}")
    filt = {k: make_entry_filter(first_open=D["first"], spot_daily=D["spot_daily"], **v) for k, v in FILTERS.items()}  # type: ignore[arg-type]
    out: dict[str, Any] = {"not_measured": D["not_measured"], "models": {}, "pass_share": {}}
    # share of entry opportunities that each filter blocks (bench vs candidates), sampled daily at 00:05
    t_s = list(range(D["start"] + 8 * 86_400_000, end, 86_400_000))
    for k, f in filt.items():
        nb = [sum(f(s, t) for t in t_s) / len(t_s) for s in bench]
        nc = [sum(f(s, t) for t in t_s if t <= pe.get(s, end)) / max(1, sum(1 for t in t_s if t <= pe.get(s, end))) for s in D["cand"]]
        out["pass_share"][k] = {"bench": statistics.mean(nb), "candidates": statistics.mean(nc)}
    log(
        "pass share (mean over symbols of days allowed): "
        + "; ".join(f"{k} bench {v['bench']:.2f} cand {v['candidates']:.2f}" for k, v in out["pass_share"].items())
    )
    for mname, model in D["models"].items():
        kw = dict(slots=5, lookback_days=7, entry_apr=0.20, exit_apr=0.0, spot_fee=model.fees["spot_binance"], perp_fee=model.fees["perp_binance"], slippage=model.sim_slippage(dl.LEGS), end_ms=end)  # fmt: skip
        fl = list(FILTERS) if mname == "main" else list(STRESS_FILTERS)
        runs: dict[str, dict[str, Any]] = {}
        t0 = time.time()
        runs["i"] = simulate_carry_delisting(bench, **kw)
        for st in SETTLES:
            runs[f"ii|{st}"] = simulate_carry_delisting(full, perp_end=pe, settle=st, **kw)
            runs[f"iii|{st}"] = simulate_carry_delisting(full, perp_end=pe, settle=st, close_on_vanish=True, n_cycles=dl.N_MAIN, **kw)
        log(f"{mname}: base runs in {time.time() - t0:.0f} s")
        years = runs["i"]["stats"]["years"]
        for fk in fl:
            f = filt[fk]
            runs[f"i+{fk}"] = simulate_carry_delisting(bench, entry_allowed=f, **kw)
            for st in SETTLES:
                runs[f"ii+{fk}|{st}"] = simulate_carry_delisting(full, perp_end=pe, settle=st, entry_allowed=f, **kw)
                runs[f"iii+{fk}|{st}"] = simulate_carry_delisting(
                    full, perp_end=pe, settle=st, close_on_vanish=True, n_cycles=dl.N_MAIN, entry_allowed=f, **kw
                )
            log(f"{mname}: {fk} done, {time.time() - t0:.0f} s")
        mo: dict[str, Any] = {"variants": {k: stats(r, years, pe) for k, r in runs.items()}, "diffs": {}, "changed": {}}
        for fk in fl:
            mo["diffs"][f"i+{fk}-i"] = diff(runs[f"i+{fk}"]["equity"], runs["i"]["equity"], years)
            for st in SETTLES:
                for v in ("ii", "iii"):
                    mo["diffs"][f"{v}+{fk}-{v}|{st}"] = diff(runs[f"{v}+{fk}|{st}"]["equity"], runs[f"{v}|{st}"]["equity"], years)
                    mo["diffs"][f"{v}+{fk}-(i+{fk})|{st}"] = diff(runs[f"{v}+{fk}|{st}"]["equity"], runs[f"i+{fk}"]["equity"], years)
                # 5a: trades removed / added by the filter in (iii)
                a, b = trade_keys(runs[f"iii|{st}"]), trade_keys(runs[f"iii+{fk}|{st}"])
                mo["changed"][f"{fk}|{st}"] = {
                    "removed": [{"symbol": k[0], "open": k[1], "pnl": a[k]["pnl"], "event": bool(a[k].get("perp_ended"))} for k in a if k not in b],
                    "added": [{"symbol": k[0], "open": k[1], "pnl": b[k]["pnl"], "event": bool(b[k].get("perp_ended"))} for k in b if k not in a],
                }  # fmt: skip
        if mname == "main":
            # without the event with the largest contribution to the gain (re-run with that symbol dropped)
            mo["no_top"] = {}
            for fk in STRESS_FILTERS:
                for st in SETTLES:
                    rem = [x for x in mo["changed"][f"{fk}|{st}"]["removed"] if x["event"]]
                    if not rem:
                        continue
                    top = min(rem, key=lambda x: x["pnl"])["symbol"]
                    sub = {s: d for s, d in full.items() if s != top}
                    pe2 = {s: v for s, v in pe.items() if s != top}
                    r0 = simulate_carry_delisting(sub, perp_end=pe2, settle=st, close_on_vanish=True, n_cycles=dl.N_MAIN, **kw)
                    r1 = simulate_carry_delisting(
                        sub, perp_end=pe2, settle=st, close_on_vanish=True, n_cycles=dl.N_MAIN, entry_allowed=filt[fk], **kw
                    )
                    mo["no_top"][f"{fk}|{st}"] = {"dropped": top, **diff(r1["equity"], r0["equity"], years)}
            log(f"main: no-top re-runs done, {time.time() - t0:.0f} s")
        out["models"][mname] = mo
        del runs
        gc.collect()
    # Holm m = 2 (F1, F2) on the one-sided p of (iii)+F - (iii), main model, per settlement
    out["holm"] = {st: holm({fk: out["models"]["main"]["diffs"][f"iii+{fk}-iii|{st}"]["p"] for fk in STRESS_FILTERS}) for st in SETTLES}
    (cache / "results.json").write_text(json.dumps(out, indent=1, default=str))
    report(out)


def report(out: dict[str, Any]) -> None:
    def pc(x: float | None) -> str:
        return "—" if x is None else f"{x * 100:+.2f}"

    for mname, mo in out["models"].items():
        print(f"\n== {mname}: variants (APR on capital %, CI, max DD, worst week, trades, perp-ended, rule closes)")
        for k, v in mo["variants"].items():
            ci = v["apr_on_capital_ci"]
            print(
                f"  {k:<18} {pc(v['apr_on_capital'])} [{pc(ci[0])}; {pc(ci[1])}] notional {pc(v['apr_on_notional'])}"
                f" DD {pc(v['max_dd_on_capital'])} wk {pc(v['worst_week_on_capital'])} ({fmt(v['worst_week_start_ms'])[:10]})"
                f" n {v['n_trades']} ev {v['n_perp_ended']} rule {v['n_rule_closes']}"
            )
        print(f"== {mname}: differences (pp/yr on capital, CI, one-sided p, IS, OOS)")
        for k, d in mo["diffs"].items():
            print(
                f"  {k:<26} {pc(d['apr_on_capital'])} [{pc(d['ci'][0])}; {pc(d['ci'][1])}] p {d['p']:.3f} IS {pc(d['is'])} OOS {pc(d['oos'])}"
            )
        for k, c in mo["changed"].items():
            ev = [x for x in c["removed"] if x["event"]]
            print(
                f"  changed {k:<16} removed {len(c['removed'])} (events {len(ev)}: {', '.join(x['symbol'].split('/')[0] for x in ev)}),"
                f" added {len(c['added'])} (events {sum(1 for x in c['added'] if x['event'])})"
            )
        for k, d in mo.get("no_top", {}).items():
            print(f"  without {d['dropped']} [{k}]: {pc(d['apr_on_capital'])} [{pc(d['ci'][0])}; {pc(d['ci'][1])}]")
    print("\nHolm m=2 (one-sided, alpha 0.025):", json.dumps(out["holm"]))


# ---------------------------------------------------------------- stage: placebo


def stage_placebo(cache: Path) -> None:
    res = json.loads((cache / "results.json").read_text())
    D = load_all(cache)
    full, pe, end = D["full"], D["pe"], D["end"]
    model = D["models"]["main"]
    kw = dict(slots=5, lookback_days=7, entry_apr=0.20, exit_apr=0.0, spot_fee=model.fees["spot_binance"], perp_fee=model.fees["perp_binance"], slippage=model.sim_slippage(dl.LEGS), end_ms=end)  # fmt: skip
    years = simulate_carry_delisting(D["bench"], **kw)["stats"]["years"]
    out: dict[str, Any] = {}
    t0 = time.time()
    for st in SETTLES:
        base = simulate_carry_delisting(full, perp_end=pe, settle=st, close_on_vanish=True, n_cycles=dl.N_MAIN, **kw)
        for fk in STRESS_FILTERS:
            v = res["models"]["main"]["variants"]
            k = len(set(v[f"iii|{st}"]["symbols"]) - set(v[f"iii+{fk}|{st}"]["symbols"]))
            deltas = []
            for j in range(N_PLACEBO):
                blk = random_blocked(full, k, PLACEBO_SEED + j)
                r = simulate_carry_delisting(
                    full, perp_end=pe, settle=st, close_on_vanish=True, n_cycles=dl.N_MAIN, entry_allowed=block_symbols(blk), **kw
                )
                d = diff_curve(r["equity"], base["equity"])
                deltas.append(d[-1][1] / years / CAPITAL if d else 0.0)
            real = res["models"]["main"]["diffs"][f"iii+{fk}-iii|{st}"]["apr_on_capital"]
            pct = sum(1 for x in deltas if x < real) / len(deltas)
            out[f"{fk}|{st}"] = {"k_blocked": k, "real": real, "placebo": sorted(deltas), "percentile": pct}
            log(
                f"placebo {fk}|{st}: k {k}, real {real * 100:+.2f}, placebo median {statistics.median(deltas) * 100:+.2f},"
                f" p05 {sorted(deltas)[2] * 100:+.2f} p95 {sorted(deltas)[-3] * 100:+.2f}, real percentile {pct:.2f} ({time.time() - t0:.0f} s)"
            )
    (cache / "placebo.json").write_text(json.dumps(out, indent=1))


def stage_report(cache: Path) -> None:
    report(json.loads((cache / "results.json").read_text()))


def stage_volumes(cache: Path) -> None:
    """Descriptive: F2 input at the entry of every (iii) event trade and the median over bench entries."""
    res = json.loads((cache / "results.json").read_text())
    D = load_all(cache)
    for st in SETTLES:
        rows = res["models"]["main"]["changed"][f"F2|{st}"]
        print(st, "F2-removed:", [(x["symbol"].split("/")[0], round(x["pnl"], 3), x["event"]) for x in rows["removed"]][:40])
    for name in sorted({e for e in D["pe"] if e in D["cand"]}):
        sd = D["spot_daily"].get(name) or []
        opens, qv = [int(r[0]) for r in sd], [r[6] for r in sd]
        t = D["pe"][name] - 30 * 86_400_000
        mv = median_volume(opens, qv, t) if sd else None
        fo = D["first"].get(name)
        age = None if fo is None else (D["pe"][name] - fo) / 86_400_000
        print(
            f"  {name.split('/')[0]:<10} perp age at end {age if age is None else round(age)} d, median spot vol 30 d before (end - 30 d) {mv if mv is None else round(mv / 1e6, 2)} M"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["fetch", "run", "placebo", "report", "volumes"])
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--limit", type=int, default=None, help="fetch: only the first N missing files (timing sample)")
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "fetch":
        asyncio.run(stage_fetch(cache, a.limit))
    elif a.stage == "run":
        stage_run(cache)
    elif a.stage == "placebo":
        stage_placebo(cache)
    elif a.stage == "volumes":
        stage_volumes(cache)
    else:
        stage_report(cache)
    log(f"done; peak RSS {dl.rss_mb():.0f} MB; {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC")


if __name__ == "__main__":
    main()
