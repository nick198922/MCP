"""Freeze the funding benchmark for criterion (v) of IDEA-20261017-forward-paper-carry -> docs/paper/bench.json.

Re-runs the two main backtest variants with the backtest code, unchanged:
  A  simulate_carry_portfolio: Binance crypto perps with spot, 5 slots, trailing 7d APR >= 0.20 in, <= 0 out,
     spot 0.10 % + perp 0.05 % + 0.05 % slippage per leg per side, no stop.
  B  simulate_perp_spread_portfolio: Binance <-> Bybit, 5 slots, spread >= 0.10 in, <= 0 out, price gap <= 0.02,
     fees 0.05 % / 0.055 % + 0.05 % slippage, stop at a 0.50 move against a leg + 0.01 penalty (the 2x variant).
Per trade: realized funding APR = funding / (slot notional x days held / 365); bands per bucket of entry APR are the
empirical 5th / 95th percentiles (analytics/carry_bench.py). Primary sample (registered in the card before the run):
trades held >= 7 days with no funding gap inside the holding window; all trades are reported as descriptive.

Data: fetched by the existing runners (verify_carry_alts.fetch, verify_carry_perp_perp.fetch) into a per-file JSON cache
outside the repo; the network goes through the runtime providers only (build_server -> rt.exchanges). The Bybit funding
cache those runners write has holes (ccxt sizes the window from the *current* interval, Bybit returns the newest 200
rows), so every hole > 12 h is re-fetched with an explicit window before the analysis (``fill_bybit_holes``).

Usage:
    uv run python scripts/verify_paper_carry_bench.py --probe                  # time a small sample, print estimate
    uv run python scripts/verify_paper_carry_bench.py --out docs/paper/bench.json
    uv run python scripts/verify_paper_carry_bench.py --no-fetch --out docs/paper/bench.json   # cache only
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import verify_carry_alts as alts
import verify_carry_perp_perp as pp
from verify_funding_carry import fetch_funding
from verify_lectures import fetch_history

from tradedesk.analytics.carry import simulate_carry_portfolio, simulate_perp_spread_portfolio
from tradedesk.analytics.carry_bench import EDGES_A, EDGES_B, band_check, build_bench, trade_samples
from tradedesk.server import build_server

DEFAULT_CACHE = Path.home() / ".cache" / "tradedesk-agents" / "paper_carry_bench"
PARAMS_A = dict(slots=5, lookback_days=7, entry_apr=0.20, exit_apr=0.0, spot_fee=0.001, perp_fee=0.0005, slippage=0.0005)
PARAMS_B = dict(
    slots=5, lookback_days=7, entry_apr=0.10, exit_apr=0.0, fee_a=0.0005, fee_b=0.00055, slippage=0.0005,
    max_entry_gap=0.02, stop_move=0.5, stop_penalty=0.01,
)  # fmt: skip
MIN_DAYS = 7.0
MIN_N = 10
GAP_FACTOR = 1.5  # a gap between consecutive payments above 1.5 x the symbol's median interval = missing payment(s)
GAP_FLOOR_MS = 12 * 3_600_000  # ... but never below 12 h: a 4h -> 8h interval switch is not a gap (set after the run)


class Tee:
    """stdout that also appends to a log file (the fetch helpers print progress themselves)."""

    def __init__(self, out: Any, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.out, self.f = out, path.open("a", encoding="utf-8")

    def write(self, s: str) -> int:
        self.f.write(s)
        self.f.flush()
        return self.out.write(s)

    def flush(self) -> None:
        self.f.flush()
        self.out.flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg} (peak RSS {rss_mb():.0f} MB)", flush=True)


def fmt(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))


def git_sha() -> dict[str, Any]:
    root = Path(__file__).resolve().parent.parent
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout.strip()
    return {"sha": sha, "dirty": bool(dirty)}


# ---------- probe ----------


async def probe(days: int) -> None:
    _, rt = build_server()
    try:
        sym = "BTC/USDT:USDT"
        for label, go in (
            ("binance funding", lambda: fetch_funding(rt, sym, days)),
            ("bybit funding", lambda: pp.fetch_funding_venue(rt, pp.B, sym, days)),
            ("binance perp 4h", lambda: fetch_history(rt, alts.PERP, sym, "4h", days)),
            ("binance spot 4h", lambda: fetch_history(rt, alts.SPOT, "BTC/USDT", "4h", days, page=1000)),
            ("bybit perp 4h", lambda: fetch_history(rt, pp.B, sym, "4h", days, page=1000)),
        ):
            t0 = time.perf_counter()
            rows = await go()
            log(f"probe {label}: {len(rows)} rows in {time.perf_counter() - t0:.1f}s")
    finally:
        await rt.aclose()


# ---------- sanity ----------


def funding_gap(funding: list[dict[str, Any]], lo: int, hi: int, floor_ms: int = 0) -> bool:
    """True when a payment is missing inside (lo, hi]: a step above max(GAP_FACTOR x median interval, ``floor_ms``).

    ``floor_ms = 0`` is the rule registered before the run; it also flags a plain 4h -> 8h interval switch as a gap,
    so the primary sample uses ``GAP_FLOOR_MS`` (12 h = 1.5 x the longest regular interval), chosen after the run.
    """
    ts = [int(e["ts_ms"]) for e in funding]
    if len(ts) < 3:
        return True
    lim = max(GAP_FACTOR * statistics.median(b - a for a, b in zip(ts, ts[1:], strict=False)), floor_ms)
    inside = [x for x in ts if lo < x <= hi]
    prior = [x for x in ts if x <= lo]
    seq = prior[-1:] + inside
    if not inside:
        return hi - lo > lim
    return any(b - a > lim for a, b in zip(seq, seq[1:], strict=False)) or hi - seq[-1] > lim + 3_600_000


async def fill_bybit_holes(cache: Path, syms: list[str], days: int) -> dict[str, Any]:
    """Re-fetch every hole > GAP_FLOOR_MS in the cached Bybit funding with an exact [start, end] window walk.

    The cached history comes from ``verify_carry_perp_perp.fetch_funding_venue``, which hops 30 days forward on an
    empty page (ccxt sizes Bybit's window from the current interval), leaving 400-1400 h holes. Here each hole is
    walked backwards with ``/v5/market/funding/history`` (public, startTime/endTime, newest first, 200 per page).
    Rows found are merged into the cache file; a hole the venue confirms empty is recorded in ``bybit_holes.json``.
    """
    rec_path = cache / "bybit_holes.json"
    rec: dict[str, Any] = json.loads(rec_path.read_text()) if rec_path.exists() else {}
    _, rt = build_server()
    added_total, n_holes = 0, 0

    async def page(market_id: str, start: int, end: int) -> list[dict[str, Any]] | None:
        for attempt in range(3):
            try:
                got = await rt.exchanges.public_get(
                    "bybit",
                    "publicGetV5MarketFundingHistory",
                    {"category": "linear", "symbol": market_id, "startTime": start, "endTime": end, "limit": 200},
                )
                await asyncio.sleep(0.1)
                return ((got.data or {}).get("result") or {}).get("list") or []
            except Exception as e:  # noqa: BLE001
                print(f"  retry {attempt + 1} {market_id}: {type(e).__name__}: {str(e)[:100]}", flush=True)
                await asyncio.sleep(5 * (attempt + 1))
        return None

    try:
        ids = {s: (m or {}).get("id") for s, m in ((await rt.exchanges.markets(pp.B)).data or {}).items()}
        for i, sym in enumerate(syms):
            path = pp.venue_funding_path(cache, pp.B, sym, days)
            if not path.exists() or sym in rec:
                continue
            rows = json.loads(path.read_text())
            ts = [int(e["ts_ms"]) for e in rows]
            holes = [(a, b) for a, b in zip(ts, ts[1:], strict=False) if b - a > GAP_FLOOR_MS]
            have = {int(e["ts_ms"]): float(e["rate"]) for e in rows}
            found_per_hole = []
            mid = ids.get(sym)
            for a, b in holes:
                n_holes += 1
                end, found = b - 1, 0
                for _ in range(100):
                    lst = await page(mid, a + 1, end) if mid else None
                    if lst is None:
                        found = -1  # could not query: the hole stays and is reported
                        break
                    pts = [(int(x["fundingRateTimestamp"]), float(x["fundingRate"])) for x in lst if a < int(x["fundingRateTimestamp"]) < b]
                    for t, r in pts:
                        if t not in have:
                            have[t] = r
                            found += 1
                    if len(lst) < 200 or not pts:
                        break
                    end = min(t for t, _ in pts) - 1
                found_per_hole.append([a, b, found])
                added_total += max(found, 0)
            if holes:
                path.write_text(json.dumps([{"ts_ms": t, "rate": r} for t, r in sorted(have.items())]))
            rec[sym] = found_per_hole
            if i % 50 == 0:
                rec_path.write_text(json.dumps(rec))
                log(f"  bybit holes {i}/{len(syms)}: holes {n_holes}, rows added {added_total}")
    finally:
        await rt.aclose()
        rec_path.write_text(json.dumps(rec))
    left = sum(1 for v in rec.values() for _, _, f in v if f == 0)
    failed = sum(1 for v in rec.values() for _, _, f in v if f < 0)
    log(f"bybit hole fill: holes {n_holes}, rows added {added_total}, confirmed empty {left}, query failed {failed}")
    return {
        "holes_seen": sum(len(v) for v in rec.values()),
        "rows_added": sum(max(f, 0) for v in rec.values() for _, _, f in v),
        "holes_confirmed_empty": left,
        "holes_query_failed": failed,
    }


def has_hole(funding: list[dict[str, Any]]) -> bool:
    ts = [int(e["ts_ms"]) for e in funding]
    return any(b - a > GAP_FLOOR_MS for a, b in zip(ts, ts[1:], strict=False))


def summary(res: dict[str, Any], key: str) -> dict[str, Any]:
    st, tr = res["stats"], res["trades"]
    return {
        "apr_on_notional": st[key],
        "n_trades": st["n_trades"],
        "share_negative": sum(1 for t in tr if t["pnl"] < 0) / len(tr),
        "mean_days_held": statistics.mean((t["close_ts"] - t["open_ts"]) / 86_400_000 for t in tr),
        "funding_per_year": st["funding"] / st["years"],
        "n_stopped": st.get("n_stopped"),
        "years": st["years"],
    }


def oos_coverage(samples: list[dict[str, Any]], edges: tuple[float, ...], split_ms: int) -> dict[str, Any]:
    """Bands from trades opened before the split, coverage on trades opened after: the realistic pass rate of (v)."""
    first = [s for s in samples if s["open_ts"] < split_ms]
    second = [s for s in samples if s["open_ts"] >= split_ms]
    if not first or not second:
        return {"n_first": len(first), "n_second": len(second)}
    bench = build_bench(first, edges, min_n=MIN_N)
    hits = [band_check(bench, s["entry"], s["realized_apr"])["in_band"] for s in second]
    known = [h for h in hits if h is not None]
    return {
        "split": fmt(split_ms),
        "n_first": len(first),
        "n_second": len(second),
        "coverage_second": sum(1 for h in known if h) / len(known) if known else None,
        "n_second_without_bucket": len(hits) - len(known),
        "bench_first": bench,
    }


def in_sample_coverage(samples: list[dict[str, Any]], bench: dict[str, Any]) -> float | None:
    hits = [band_check(bench, s["entry"], s["realized_apr"])["in_band"] for s in samples]
    known = [h for h in hits if h is not None]
    return sum(1 for h in known if h) / len(known) if known else None


def per_symbol(samples: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, int] = {}
    for s in samples:
        by[s["symbol"]] = by.get(s["symbol"], 0) + 1
    top = sorted(by.items(), key=lambda x: -x[1])[:5]
    return {"n_symbols": len(by), "top5": top, "top5_share": sum(n for _, n in top) / max(len(samples), 1)}


def rnd(x: Any) -> Any:
    if isinstance(x, float):
        return round(x, 6)
    if isinstance(x, dict):
        return {k: rnd(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [rnd(v) for v in x]
    return x


def strategy_block(
    name: str, res: dict[str, Any], entry_key: str, edges: tuple[float, ...], gaps: set[int], gaps_registered: set[int]
) -> dict[str, Any]:
    trades = res["trades"]
    all_s = trade_samples(trades, entry_key)
    long_s = trade_samples(trades, entry_key, min_days=MIN_DAYS)
    # trade_samples keeps order; mark gap trades by index in the original list
    idx_long = [i for i, t in enumerate(trades) if (t["close_ts"] - t["open_ts"]) / 86_400_000 >= MIN_DAYS and t["close_ts"] > t["open_ts"]]
    primary = [s for s, i in zip(long_s, idx_long, strict=True) if i not in gaps]
    bench = build_bench(primary, edges, min_n=MIN_N)
    start = min(t["open_ts"] for t in trades)
    end = max(t["close_ts"] for t in trades)
    variants = {
        "all_trades": build_bench(all_s, edges, min_n=MIN_N),
        "ge7d_with_gap_trades": build_bench(long_s, edges, min_n=MIN_N),
        "ge7d_closed_only": build_bench([s for s in primary if not s["open_at_end"]], edges, min_n=MIN_N),
        "ge7d_registered_gap_rule": build_bench(
            [s for s, i in zip(long_s, idx_long, strict=True) if i not in gaps_registered], edges, min_n=MIN_N
        ),
    }
    if name == "B":
        variants["ge7d_not_stopped"] = build_bench([s for s in primary if not s["stopped"]], edges, min_n=MIN_N)
    log(f"{name}: trades {len(trades)}, >= {MIN_DAYS:.0f}d {len(long_s)}, with funding gaps {len(gaps)}, primary sample {len(primary)}")
    for b in bench["buckets"]:
        hi = "inf" if b["entry_hi"] is None else f"{b['entry_hi']:.2f}"
        band = f"p05 {b['p05']:+.3f} p95 {b['p95']:+.3f} median {b['median']:+.3f}" if "p05" in b else "empty"
        log(f"  {name} bucket [{b['entry_lo']:.2f}, {hi}): n {b['n']} {band}")
    return {
        "primary_sample": f"trades held >= {MIN_DAYS:.0f} days, no funding gap in the holding window, open-at-end included",
        "n_trades_backtest": len(trades),
        "n_ge7d": len(long_s),
        "n_gap_trades_excluded": len(gaps),
        "n_gap_trades_registered_rule": len(gaps_registered),
        "bench": bench,
        "in_sample_coverage": in_sample_coverage(primary, bench),
        "oos": oos_coverage(primary, edges, (start + end) // 2),
        "concentration": per_symbol(primary),
        "variants_descriptive": variants,
        "samples": [
            {k: s[k] for k in ("symbol", "open_ts", "close_ts", "days", "entry", "realized_apr", "open_at_end", "stopped")} for s in primary
        ],
    }


# ---------- main ----------


def run_a(cache: Path, days: int) -> tuple[dict[str, Any], dict[str, Any]]:
    uni = {k: tuple(v) for k, v in json.loads((cache / "universe_a.json").read_text()).items()}
    mk = alts.load_markets(cache, days, uni)  # type: ignore[arg-type]
    log(f"A: universe {len(uni)}, with prices {len(mk)}")
    first = min(d["funding"][0]["ts_ms"] for d in mk.values())
    last = max(d["funding"][-1]["ts_ms"] for d in mk.values())
    res = simulate_carry_portfolio(mk, **PARAMS_A)
    gaps = {i for i, t in enumerate(res["trades"]) if funding_gap(mk[t["symbol"]]["funding"], t["open_ts"], t["close_ts"], GAP_FLOOR_MS)}
    gaps_reg = {i for i, t in enumerate(res["trades"]) if funding_gap(mk[t["symbol"]]["funding"], t["open_ts"], t["close_ts"])}
    holes = sum(1 for d in mk.values() if has_hole(d["funding"]))
    meta = {
        "universe": len(uni),
        "universe_filter": "Binance USDT-M PERPETUAL, underlyingType == COIN (TradFi excluded), active, Binance USDT spot exists",
        "with_prices": len(mk),
        "multiplier_symbols": sorted(s for s, (_, m) in uni.items() if m != 1 and s in mk),
        "symbols_with_funding_hole_gt_12h": holes,
        "funding_first": fmt(first),
        "funding_last": fmt(last),
        "summary": summary(res, "apr_on_capital"),
    }
    del mk
    gc.collect()
    return res, {"meta": meta, "gaps": gaps, "gaps_registered": gaps_reg}


def run_b(cache: Path, days: int) -> tuple[dict[str, Any], dict[str, Any]]:
    u = json.loads((cache / "universe_b.json").read_text())
    pp.MISMATCHED.clear()
    mk = pp.load(cache, days, u["hot"])
    log(f"B: universe {len(u['uni'])}, hot {len(u['hot'])}, with data {len(mk)}, collisions excluded {len(pp.MISMATCHED)}")
    first = min(max(d["a"]["funding"][0]["ts_ms"], d["b"]["funding"][0]["ts_ms"]) for d in mk.values())
    last = max(min(d["a"]["funding"][-1]["ts_ms"], d["b"]["funding"][-1]["ts_ms"]) for d in mk.values())
    res = simulate_perp_spread_portfolio(mk, **PARAMS_B)
    nostop = simulate_perp_spread_portfolio(mk, **dict(PARAMS_B, stop_move=None))
    gaps, gaps_reg = (
        {
            i
            for i, t in enumerate(res["trades"])
            if funding_gap(mk[t["symbol"]]["a"]["funding"], t["open_ts"], t["close_ts"], floor)
            or funding_gap(mk[t["symbol"]]["b"]["funding"], t["open_ts"], t["close_ts"], floor)
        }
        for floor in (GAP_FLOOR_MS, 0)
    )
    holes_a = sum(1 for d in mk.values() if has_hole(d["a"]["funding"]))
    holes_b = sum(1 for d in mk.values() if has_hole(d["b"]["funding"]))
    meta = {
        "universe": len(u["uni"]),
        "hot": len(u["hot"]),
        "with_data": len(mk),
        "collisions_excluded": [[n, r] for n, r in pp.MISMATCHED],
        "pairs_with_funding_hole_gt_12h": {"binance": holes_a, "bybit": holes_b},
        "funding_first": fmt(first),
        "funding_last": fmt(last),
        "summary": summary(res, "apr_on_notional"),
        "summary_no_stop": summary(nostop, "apr_on_notional"),
    }
    del mk
    gc.collect()
    return res, {"meta": meta, "gaps": gaps, "gaps_registered": gaps_reg}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out", default="")
    ap.add_argument("--no-fetch", action="store_true", help="analyse the cache only (universe_*.json must exist)")
    ap.add_argument("--probe", action="store_true", help="time one symbol on every endpoint and exit")
    ap.add_argument("--log", default="", help="also append all progress lines to this file (no shell redirection needed)")
    a = ap.parse_args()
    if a.log:
        sys.stdout = Tee(sys.stdout, Path(a.log).expanduser())
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.probe:
        asyncio.run(probe(a.days))
        return
    if not a.no_fetch:
        log("fetch A (verify_carry_alts.fetch)")
        uni_a = asyncio.run(alts.fetch(cache, a.days, min_apr=0.20))
        (cache / "universe_a.json").write_text(json.dumps(uni_a))
        log("fetch B (verify_carry_perp_perp.fetch)")
        uni_b, hot = asyncio.run(pp.fetch(cache, a.days, min_spread=0.10))
        (cache / "universe_b.json").write_text(json.dumps({"uni": uni_b, "hot": hot}))
    hot = json.loads((cache / "universe_b.json").read_text())["hot"]
    holes = asyncio.run(fill_bybit_holes(cache, hot, a.days))
    res_a, ex_a = run_a(cache, a.days)
    block_a = strategy_block("A", res_a, "entry_apr", EDGES_A, ex_a["gaps"], ex_a["gaps_registered"])
    del res_a
    res_b, ex_b = run_b(cache, a.days)
    block_b = strategy_block("B", res_b, "entry_spread", EDGES_B, ex_b["gaps"], ex_b["gaps_registered"])
    del res_b
    for k, ex in (("A", ex_a), ("B", ex_b)):
        s = ex["meta"]["summary"]
        log(
            f"{k} consistency: APR on notional {s['apr_on_notional']:+.3f}, trades {s['n_trades']}, negative {s['share_negative']:.0%}, "
            f"mean days {s['mean_days_held']:.1f}, stopped {s['n_stopped']}"
        )
    for k, blk in (("A", block_a), ("B", block_b)):
        o = blk["oos"]
        cov = o.get("coverage_second")
        log(
            f"{k} coverage: in-sample {blk['in_sample_coverage']}, out-of-sample (bands 1st half -> 2nd half) {cov} on n={o.get('n_second')}"
        )
    out = {
        "card": "docs/ideas/IDEA-20261017-forward-paper-carry.md",
        "criterion": "(v) realized funding APR of a paper trade (>= 7 days held, not deferred) within [p05, p95] of its entry bucket",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git": git_sha(),
        "script": "scripts/verify_paper_carry_bench.py",
        "definition": {
            "realized_funding_apr": "funding collected (fraction of slot notional) / (days held / 365)",
            "slot_notional": "A: spot entry price x qty; B: leg A (Binance) entry price x qty; $2000 per leg in the paper book",
            "price_at_payment": "close of the 4h bar containing the payment (backtest convention; the paper engine does the same)",
            "entry": {"A": "trailing 7d funding APR at entry", "B": "|trailing 7d APR Bybit - Binance| at entry"},
            "percentiles": "linear interpolation between order statistics (numpy default / R type 7)",
            "merge": f"bucket with < {MIN_N} trades merged into its lower neighbour (lowest into upper), repeated top-down",
            "pass": ">= 80 % of paper trades (>= 7 days, not deferred) inside their bucket's band; needs >= 10 such trades",
            "rates": "decimals: 0.10 = 10 %",
        },
        "params": {
            "A": PARAMS_A,
            "B": PARAMS_B,
            "days": a.days,
            "min_days": MIN_DAYS,
            "min_n": MIN_N,
            "gap_factor": GAP_FACTOR,
            "gap_floor_h": GAP_FLOOR_MS / 3_600_000,
        },
        "data": {"A": ex_a["meta"], "B": {**ex_b["meta"], "bybit_hole_fill": holes}},
        "A": block_a,
        "B": block_b,
    }
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(rnd(out), indent=1, ensure_ascii=False) + "\n")
        log(f"wrote {a.out}")
    log(f"done; years A {ex_a['meta']['summary']['years']:.2f}, B {ex_b['meta']['summary']['years']:.2f}")


if __name__ == "__main__":
    main()
