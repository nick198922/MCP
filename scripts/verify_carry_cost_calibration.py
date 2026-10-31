"""Carry cost calibration from the forward paper engines' recorded leg sides, and the carry backtests re-run with it.

Card: docs/ideas/IDEA-20261031-structural-edges-depth.md, item 2 (pre-registration "Пункт 2"). No network:

  paper     reads a read-only snapshot of the collector DB (cp_log sides / funding, cp_position, cp_funding), decomposes
            each leg side into fee, half spread, depth impact and timing (analytics/carry_costs.py), calibrates the
            per-leg-type slippage and writes calibration.json to the cache dir.
  backtest  re-runs the bench's main variants (A cash-and-carry, B perp-perp 2x with stop) on the bench cache
            (~/.cache/tradedesk-agents/paper_carry_bench/, 730 days to 2026-10-17) with the old, main, stress and x2
            cost models; APR on notional and capital, max drawdown, worst calendar week, block-bootstrap CI.

    uv run python scripts/verify_carry_cost_calibration.py paper --snapshot-from /var/lib/tradedesk-mcp/tradedesk.sqlite
    uv run python scripts/verify_carry_cost_calibration.py backtest --strategy A
    uv run python scripts/verify_carry_cost_calibration.py backtest --strategy B

Full outputs go to --cache-dir (default ~/.cache/tradedesk-agents/carry_cost_calibration/); stdout is a compact summary.
The frozen paper engines and their parameters are not touched.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import resource
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tradedesk.analytics.carry_costs import (  # noqa: E402
    OLD_MODEL,
    CostModel,
    calibrate,
    cluster_bootstrap_mean,
    cochran_q,
    decompose_side,
    risk_summary,
)

DEFAULT_CACHE = Path.home() / ".cache" / "tradedesk-agents" / "carry_cost_calibration"
BENCH_CACHE = Path.home() / ".cache" / "tradedesk-agents" / "paper_carry_bench"
SNAPSHOT = "db-snapshot.sqlite"
LEGS = {"A": ["spot_binance", "perp_binance"], "B": ["perp_binance", "perp_bybit"]}
CAPITAL = {"A": 1.5, "B": 1.0}
N_BOOT = 10_000


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg} (peak RSS {rss_mb():.0f} MB)", flush=True)


def fmt(ms: int | None) -> str:
    return "-" if ms is None else time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))


def pct(x: float | None, d: int = 3) -> str:
    return "-" if x is None else f"{x * 100:+.{d}f}%"


# ---------- paper ----------


def snapshot(src: str, dst: Path) -> None:
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    b = sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def load_sides(con: sqlite3.Connection) -> tuple[list[dict[str, Any]], int]:
    out, no_book = [], 0
    for strategy, cycle_ts, trade_id, pl in con.execute(
        "select strategy, cycle_ts, trade_id, payload from cp_log where kind='side' order by id"
    ):
        p = json.loads(pl)
        d = decompose_side(p)
        if d is None:
            no_book += 1
            continue
        x5 = decompose_side(p, notional_usd=5 * float(p["notional_usd"]))
        out.append(
            {
                **d,
                "strategy": strategy,
                "cycle_ts": int(cycle_ts),
                "trade_id": trade_id,
                "symbol": p.get("perp_symbol") or p.get("symbol"),
                "venue": p.get("venue"),
                "leg": p.get("leg"),
                "side": p.get("side"),
                "spread_bps": p.get("spread_bps"),
                "depth_05_usd": ((p.get("depth_usd") or {}).get("0.5%") or {}).get("ask" if p.get("side") == "buy" else "bid"),
                "recorded_actual_cost": p.get("actual_cost"),
                "slippage_x5": None if x5 is None else x5["slippage"],
                "depth_short_x5": None if x5 is None else x5["depth_short"],
            }
        )
    return out, no_book


def funding_check(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per trade: engine-accrued funding (rate units, signed for the position) vs the venue history over the hold."""
    last_cycle = {s: int(t) for s, t in con.execute("select strategy, max(cycle_ts) from cp_cycle where status='done' group by 1")}
    hist: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    for venue, sym, ts, rate in con.execute("select venue, symbol, ts, rate from cp_funding"):
        hist[(venue, sym)].append((int(ts), float(rate)))
    logged: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for tid, pl in con.execute("select trade_id, payload from cp_log where kind='funding'"):
        p = json.loads(pl)
        sign = 1.0 if p["usd"] * p["rate"] >= 0 else -1.0
        logged[tid][p["venue"]] += sign * float(p["rate"])
        counts[tid][p["venue"]] += 1
    deferred: dict[str, int] = defaultdict(int)
    for tid, pl in con.execute("select trade_id, payload from cp_log where kind='event'"):
        if json.loads(pl).get("type") == "funding_payment_missing" and tid:
            deferred[tid] += 1
    out = []
    for tid, strategy, sym, status, _opened, closed, state in con.execute(
        "select trade_id, strategy, symbol, status, opened_ts, closed_ts, state from cp_position"
    ):
        st = json.loads(state)
        open_ts = int(st["open_ts"])
        end = int(closed) + 300_000 if closed else last_cycle[strategy] + 300_000
        if strategy == "A":
            legs = [("binanceusdm", 1.0)]
        else:
            d = int(st["dir"])
            legs = [("binanceusdm", -d), ("bybit", float(d))]
        row = {"trade_id": tid, "strategy": strategy, "symbol": sym, "status": status, "deferred_events": deferred.get(tid, 0)}
        diff = 0.0
        for venue, sign in legs:
            h = [(t, r) for t, r in hist.get((venue, sym), []) if open_ts < t <= end]
            h_sum = sign * math.fsum(r for _, r in h)
            lg = logged[tid].get(venue, 0.0)
            row[venue] = {"history_n": len(h), "logged_n": counts[tid].get(venue, 0), "history_sum": h_sum, "logged_sum": lg}
            diff += lg - h_sum
        row["logged_minus_history"] = diff
        out.append(row)
    return out


def summarise_paper(sides: list[dict[str, Any]], as_of: int) -> dict[str, Any]:
    res: dict[str, Any] = {"as_of_ms": as_of}
    for s in ("A", "B"):
        xs = [x for x in sides if x["strategy"] == s]
        diff = cluster_bootstrap_mean([x["actual_cost"] - x["model_cost"] for x in xs], [x["symbol"] for x in xs], n_boot=N_BOOT)
        ma = statistics.fmean(x["actual_cost"] for x in xs) if xs else None
        mm = statistics.fmean(x["model_cost"] for x in xs) if xs else None
        comp = {k: statistics.fmean(x[k] for x in xs) if xs else None for k in ("fee", "half_spread", "impact", "slippage")}
        tm = [x["timing"] for x in xs if x["timing"] is not None]
        ratios = sorted(x["actual_cost"] / x["model_cost"] for x in xs)
        res[s] = {
            "n_sides": len(xs),
            "symbols": len({x["symbol"] for x in xs}),
            "trades": len({x["trade_id"] for x in xs}),
            "mean_actual": ma,
            "mean_model": mm,
            "ratio": ma / mm if ma and mm else None,
            "ratio_p90": ratios[int(0.9 * (len(ratios) - 1))] if ratios else None,
            "actual_minus_model": diff,
            "components": comp,
            "timing_leg_mean": statistics.fmean(tm) if tm else None,
            "depth_short": sum(1 for x in xs if x["depth_short"]),
            "x5_mean_slippage": statistics.fmean(x["slippage_x5"] for x in xs if x["slippage_x5"] is not None) if xs else None,
            "x5_depth_short": sum(1 for x in xs if x["depth_short_x5"]),
        }
    # timing per trade side (two legs together), cluster by symbol
    ts: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for x in sides:
        ts[(x["trade_id"], x["event"], x["cycle_ts"])].append(x)
    pairs = [
        (v[0]["symbol"], v[0]["strategy"], sum(y["timing"] for y in v))
        for v in ts.values()
        if len(v) == 2 and all(y["timing"] is not None for y in v)
    ]
    res["timing_trade_side"] = {
        s: cluster_bootstrap_mean([t for _, st, t in pairs if st == s], [sy for sy, st, _ in pairs if st == s], n_boot=N_BOOT)
        for s in ("A", "B")
    }
    res["timing_trade_side"]["all"] = cluster_bootstrap_mean([t for *_, t in pairs], [sy for sy, *_ in pairs], n_boot=N_BOOT)
    res["timing_excluded_legs"] = sum(1 for x in sides if x["timing"] is None)
    # per open/close
    res["by_event"] = {
        e: {
            lt: statistics.fmean(x["slippage"] for x in sides if x["event"] == e and x["leg_type"] == lt)
            for lt in sorted({x["leg_type"] for x in sides if x["event"] == e})
        }
        for e in ("open", "close")
    }
    # per symbol (slippage per leg side) + heterogeneity
    per_sym: dict[str, list[float]] = defaultdict(list)
    for x in sides:
        if x["leg_type"] != "spot_binance":
            per_sym[x["symbol"]].append(x["slippage"])
    pooled_sd = statistics.pstdev([v for vs in per_sym.values() for v in vs]) if per_sym else 0.0
    rows = []
    for sym, vs in sorted(per_sym.items()):
        sd = statistics.stdev(vs) if len(vs) > 2 else pooled_sd
        rows.append({"symbol": sym, "n": len(vs), "mean": statistics.fmean(vs), "se": max(sd, 1e-6) / math.sqrt(len(vs))})
    res["per_symbol_perp"] = rows
    res["heterogeneity_perp"] = cochran_q([r["mean"] for r in rows], [r["se"] for r in rows])
    # cost vs liquidity (descriptive)
    pts = [(math.log(x["depth_05_usd"]), x["slippage"]) for x in sides if x.get("depth_05_usd")]
    res["corr_log_depth_slippage"] = statistics.correlation([a for a, _ in pts], [b for _, b in pts]) if len(pts) > 2 else None
    return res


def build_models(cal: dict[str, Any], timing: dict[str, Any]) -> dict[str, CostModel]:
    fees = dict(OLD_MODEL.fees)
    t_ok = timing["lo"] is not None and (timing["lo"] > 0 or timing["hi"] < 0)
    t_main = timing["mean"] if t_ok else 0.0
    t_stress = max(timing["mean"], 0.0) if t_ok else 0.0
    main = CostModel(fees, {k: cal[k]["main"] for k in cal}, timing=t_main)
    stress = CostModel(fees, {k: cal[k]["stress"] for k in cal}, timing=t_stress)
    x2 = CostModel(fees, {k: 2 * cal[k]["main"] for k in cal}, timing=2 * t_main)
    return {"old": OLD_MODEL, "main": main, "stress": stress, "x2": x2}


def run_paper(a: argparse.Namespace, cache: Path) -> None:
    db = cache / SNAPSHOT
    if a.snapshot_from:
        snapshot(a.snapshot_from, db)
        log(f"snapshot {a.snapshot_from} -> {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    sides, no_book = load_sides(con)
    as_of = max(x["cycle_ts"] for x in sides)
    log(f"sides with a book {len(sides)}, without {no_book}, last side {fmt(as_of)}")
    mism = [x for x in sides if x["recorded_actual_cost"] is not None and abs(x["recorded_actual_cost"] - x["actual_cost"]) > 1e-9]
    log(f"decomposition check: recomputed actual cost differs from the recorded one on {len(mism)} sides")
    cal = calibrate(sides, as_of_ms=as_of, n_boot=N_BOOT)
    summ = summarise_paper(sides, as_of)
    fund = funding_check(con)
    con.close()
    models = build_models(cal, summ["timing_trade_side"]["all"])
    out = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "snapshot": str(db),
        "calibration": cal,
        "summary": summ,
        "funding_check": fund,
        "models": {k: {"fees": m.fees, "slippage": m.slippage, "timing": m.timing} for k, m in models.items()},
        "sides": sides,
    }
    (cache / "calibration.json").write_text(json.dumps(out, indent=1, default=str))
    print("\n== per leg type: slippage = half spread + impact per side (registered calibration) ==")
    print("| leg type | n | symbols | mean | 95% CI | max | enough | main | stress |")
    for lt, c in cal.items():
        print(
            f"| {lt} | {c['n']} | {c['clusters']} | {pct(c['mean'])} | [{pct(c['lo'])}; {pct(c['hi'])}] | {pct(c['max'])} | {c['enough']} | {pct(c['main'])} | {pct(c['stress'])} |"
        )
    print("\n== per strategy: actual vs model per side ==")
    print(
        "| strat | sides | trades | symbols | fee | half spread | impact | actual | model | ratio | p90 | actual-model [CI] | timing/leg | short depth | x5 slip | x5 short |"
    )
    for s in ("A", "B"):
        r = summ[s]
        c, d = r["components"], r["actual_minus_model"]
        print(
            f"| {s} | {r['n_sides']} | {r['trades']} | {r['symbols']} | {pct(c['fee'])} | {pct(c['half_spread'])} | {pct(c['impact'])} | {pct(r['mean_actual'])} | "
            f"{pct(r['mean_model'])} | {r['ratio']:.2f} | {r['ratio_p90']:.2f} | {pct(d['mean'])} [{pct(d['lo'])}; {pct(d['hi'])}] | {pct(r['timing_leg_mean'])} | "
            f"{r['depth_short']} | {pct(r['x5_mean_slippage'])} | {r['x5_depth_short']} |"
        )
    print("\n== timing per trade side (both legs, + = adverse) ==")
    for k, t in summ["timing_trade_side"].items():
        print(f"{k}: n {t['n']} sym {t['clusters']} mean {pct(t['mean'])} CI [{pct(t['lo'])}; {pct(t['hi'])}]")
    print(f"legs excluded from timing (|timing| > 10 %): {summ['timing_excluded_legs']}")
    print(f"slippage by event: {json.dumps({e: {k: round(v * 100, 4) for k, v in d.items()} for e, d in summ['by_event'].items()})} (in %)")
    h = summ["heterogeneity_perp"]
    print(
        f"perp slippage heterogeneity across symbols: k {h['k']}, Q {h['q']:.1f}, df {h['df']}, I2 {h['i2']:.2f}; corr(log depth 0.5 %, slippage) {summ['corr_log_depth_slippage']:+.2f}"
    )
    print("\n== per trade: costs USD on a $2000 leg (actual vs model, sum over recorded sides) ==")
    print("| trade | sides | fee | half spread | impact | actual | model | timing |")
    by_t: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in sides:
        by_t[x["trade_id"]].append(x)
    for tid, xs in sorted(by_t.items()):
        f = lambda k, xs=xs: sum(y[k] or 0.0 for y in xs) * 2000  # noqa: E731
        print(
            f"| {tid.rsplit(':', 1)[0]} {fmt(int(tid.rsplit(':', 1)[1]))[5:10]} | {len(xs)} | {f('fee'):.2f} | {f('half_spread'):.2f} | {f('impact'):.2f} | {f('actual_cost'):.2f} | {f('model_cost'):.2f} | {f('timing'):+.2f} |"
        )
    print("\n== funding: engine-accrued minus venue history over the hold (rate units, x $2000 = USD) ==")
    big = [r for r in fund if abs(r["logged_minus_history"]) > 1e-9]
    print(
        f"trades {len(fund)}, with a difference {len(big)}, sum |diff| x 2000 = {sum(abs(r['logged_minus_history']) for r in fund) * 2000:.2f} USD"
    )
    for r in big:
        legs = {k: v for k, v in r.items() if isinstance(v, dict)}
        print(
            f"  {r['trade_id']} {r['status']} deferred {r['deferred_events']} diff {r['logged_minus_history'] * 2000:+.2f} USD {json.dumps(legs)[:300]}"
        )
    print("\n== cost models (per side, fraction of leg notional) ==")
    for k, m in models.items():
        print(
            f"{k}: slippage {json.dumps({x: round(v * 100, 4) for x, v in m.slippage.items()})} %, timing {m.timing * 100:.4f} %, A side {m.side_cost(LEGS['A']) * 100:.3f} %, B side {m.side_cost(LEGS['B']) * 100:.3f} %"
        )


# ---------- backtest ----------


def run_backtest(a: argparse.Namespace, cache: Path) -> None:
    import verify_carry_alts as alts
    import verify_carry_perp_perp as pp

    from tradedesk.analytics.carry import simulate_carry_portfolio, simulate_perp_spread_portfolio

    cal = json.loads((cache / "calibration.json").read_text())
    models = {k: CostModel(v["fees"], v["slippage"], v["timing"]) for k, v in cal["models"].items()}
    # Sensitivity added after the first run (labelled in the report): the timing term's CI excluded 0 by a hair.
    models["main_no_timing"] = CostModel(models["main"].fees, models["main"].slippage, 0.0)
    s = a.strategy
    days = 730
    if s == "A":
        uni = {k: tuple(v) for k, v in json.loads((BENCH_CACHE / "universe_a.json").read_text()).items()}
        mk = alts.load_markets(BENCH_CACHE, days, uni)  # type: ignore[arg-type]
        log(f"A: universe {len(uni)}, with prices {len(mk)}")

        def sim(m: CostModel) -> dict[str, Any]:
            return simulate_carry_portfolio(
                mk,
                slots=5,
                lookback_days=7,
                entry_apr=0.20,
                exit_apr=0.0,
                spot_fee=m.fees["spot_binance"],
                perp_fee=m.fees["perp_binance"],
                slippage=m.sim_slippage(LEGS["A"]),
            )
    else:
        u = json.loads((BENCH_CACHE / "universe_b.json").read_text())
        pp.MISMATCHED.clear()
        mk = pp.load(BENCH_CACHE, days, u["hot"])
        log(f"B: hot {len(u['hot'])}, with data {len(mk)}, collisions excluded {len(pp.MISMATCHED)}")

        def sim(m: CostModel) -> dict[str, Any]:
            return simulate_perp_spread_portfolio(
                mk, slots=5, lookback_days=7, entry_apr=0.10, exit_apr=0.0, fee_a=m.fees["perp_binance"], fee_b=m.fees["perp_bybit"],
                slippage=m.sim_slippage(LEGS["B"]), max_entry_gap=0.02, stop_move=0.5, stop_penalty=0.01,
            )  # fmt: skip

    c = CAPITAL[s]
    out: dict[str, Any] = {"strategy": s, "capital_factor": c, "variants": {}}
    print(f"\n== {s}: backtest re-run (bench cache, rules unchanged), returns on capital = notional / {c} ==")
    print(
        "| costs | side cost | trades | APR notional | APR capital | 95% CI (4-wk blocks) | max DD cap | worst week cap (start) | costs/yr | 1st half cap | 2nd half cap | w/o top-5 cap |"
    )
    for name, m in models.items():
        res = sim(m)
        st, eq = res["stats"], res["equity"]
        rk = risk_summary(eq, st["years"], c, n_boot=N_BOOT)
        mid_t = eq[0][0] + (eq[-1][0] - eq[0][0]) // 2
        v_mid = next(v for t, v in eq if t >= mid_t)
        half_y = st["years"] / 2
        w = 1 / 5
        by_sym: dict[str, float] = defaultdict(float)
        for tr in res["trades"]:
            by_sym[tr["symbol"]] += w * tr["pnl"]
        top5 = sorted(by_sym, key=lambda k: by_sym[k], reverse=True)[:5]
        wo = (st["total_return"] - sum(by_sym[k] for k in top5)) / st["years"] / c
        row = {
            **rk,
            "n_trades": st["n_trades"],
            "side_cost": m.side_cost(LEGS[s]),
            "costs_per_year_on_capital": st["costs"] / st["years"] / c,
            "first_half_apr_on_capital": v_mid / half_y / c,
            "second_half_apr_on_capital": (st["total_return"] - v_mid) / half_y / c,
            "without_top5_apr_on_capital": wo,
            "top5": top5,
            "top5_share_of_pnl": sum(by_sym[k] for k in top5) / st["total_return"] if st["total_return"] else None,
            "share_negative": sum(1 for tr in res["trades"] if tr["pnl"] < 0) / max(len(res["trades"]), 1),
            "n_stopped": st.get("n_stopped"),
            "years": st["years"],
        }
        out["variants"][name] = row
        lo, hi = rk["apr_on_capital_ci"]
        print(
            f"| {name} | {pct(row['side_cost'])} | {row['n_trades']} | {pct(rk['apr_on_notional'], 1)} | {pct(rk['apr_on_capital'], 1)} | [{pct(lo, 1)}; {pct(hi, 1)}] | "
            f"{pct(-rk['max_dd_on_capital'], 1)} | {pct(rk['worst_week_on_capital'], 1)} ({fmt(rk['worst_week_start_ms'])[:10]}) | {pct(-row['costs_per_year_on_capital'], 1)} | "
            f"{pct(row['first_half_apr_on_capital'], 1)} | {pct(row['second_half_apr_on_capital'], 1)} | {pct(wo, 1)} |"
        )
        del res
        gc.collect()
    print(f"top-5 symbols (main): {out['variants']['main']['top5']}, share of P&L {out['variants']['main']['top5_share_of_pnl']}")
    (cache / f"backtest_{s}.json").write_text(json.dumps(out, indent=1, default=str))
    log(f"{s} done")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("part", choices=["paper", "backtest"])
    ap.add_argument("--strategy", choices=["A", "B"], default="A")
    ap.add_argument("--snapshot-from", default="", help="collector DB to snapshot read-only first (else reuse the cached snapshot)")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.part == "paper":
        run_paper(a, cache)
    else:
        run_backtest(a, cache)


if __name__ == "__main__":
    main()
