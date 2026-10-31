"""Item 8b of IDEA-20261031-structural-edges-depth: carry A with perp-leg delisting (survivorship) and an exit rule.

Stages (run in order; every stage reads the cache of the previous one):
  candidates  delisted / stopped Binance USDT-M perps (funding_crowding cache, archive funding with intervals) that
              have a Binance USDT spot pair and were ever "hot" (trailing 7d APR >= 0.20) -> candidates.json
  fetch       4h perp and spot klines of the hot candidates from Binance Vision (rt.binance_vision), one JSON file
              per month under --cache-dir/vision/ (404 -> null)
  run         (i) bench as is, (ii) full universe under the frozen A rules with delisting accounting, (iii) the same
              plus "close on N missing cycles"; cost models 'main' and 'stress' of item 2 -> results.json

Network only in the first two stages and only through rt.binance_vision; no keys, no orders. Returns on capital
(A: capital = 1.5 x notional). Pre-registration: the card, section "Пункт 8b".
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gc
import json
import resource
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_carry_alts as alts  # noqa: E402

from tradedesk.analytics.carry import simulate_carry_portfolio  # noqa: E402
from tradedesk.analytics.carry_costs import CostModel, risk_summary  # noqa: E402
from tradedesk.analytics.carry_delisting import (  # noqa: E402
    interval_change_before,
    simulate_carry_delisting,
    vanish_events,
)
from tradedesk.analytics.carry_modules import block_bootstrap, diff_curve, window_weekly  # noqa: E402
from tradedesk.core.errors import ProviderError  # noqa: E402

HOME = Path.home() / ".cache" / "tradedesk-agents"
DEFAULT_CACHE = HOME / "carry_a_delisting"
BENCH = HOME / "paper_carry_bench"
CROWD = HOME / "funding_crowding"
CALIB = HOME / "carry_cost_calibration" / "calibration.json"
DAYS = 730
H4 = 4 * 3_600_000
H8 = 8 * 3_600_000
CAPITAL = 1.5
LEGS = ["spot_binance", "perp_binance"]
N_MAIN = 2
N_SENS = (1, 3)
SPLIT_MS = int(dt.datetime(2025, 3, 1, tzinfo=dt.UTC).timestamp() * 1000)
OCT_TAIL_MS = int(dt.datetime(2026, 9, 30, 16, tzinfo=dt.UTC).timestamp() * 1000)
N_BOOT = 10_000


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')} rss {rss_mb():.0f} MB] {msg}", flush=True)


def fmt(ms: int | None) -> str:
    return "—" if ms is None else dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")


def pct(x: float | None, d: int = 1) -> str:
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def month_range(lo_ms: int, hi_ms: int) -> list[tuple[int, int]]:
    a = dt.datetime.fromtimestamp(lo_ms / 1000, dt.UTC)
    b = dt.datetime.fromtimestamp(hi_ms / 1000, dt.UTC)
    y, m, out = a.year, a.month, []
    while (y, m) <= (b.year, b.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def bench_window() -> tuple[int, int, dict[str, tuple[str, int]]]:
    uni = {k: tuple(v) for k, v in json.loads((BENCH / "universe_a.json").read_text()).items()}
    f = json.loads(alts.funding_path(BENCH, "BTC/USDT:USDT", DAYS).read_text())
    return int(f[0]["ts_ms"]), int(f[-1]["ts_ms"]), uni  # type: ignore[return-value]


def archive_funding(sym: str) -> list[list[float]]:
    d = CROWD / "funding" / sym
    rows: list[list[float]] = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        x = json.loads(p.read_text())
        if x:
            rows += x
    rows.sort(key=lambda r: r[0])
    return rows


# ---------------------------------------------------------------- stage: candidates


async def stage_candidates(cache: Path) -> None:
    start, end, uni = bench_window()
    spot_path = cache / "spot_symbols.json"
    if not spot_path.exists():
        from tradedesk.server import build_server

        _, rt = build_server()
        try:
            spot_path.write_text(json.dumps((await rt.binance_vision.spot_symbols()).data))
        finally:
            await rt.aclose()
    spots = set(json.loads(spot_path.read_text()))
    info = json.loads((CROWD / "exchange_info.json").read_text())["info"]
    syms = json.loads((CROWD / "symbols.json").read_text())
    bench_bases = {k.split("/")[0] for k in uni}
    out: dict[str, Any] = {
        "window": [start, end],
        "hot": [],
        "not_hot": [],
        "no_spot": [],
        "out_of_window": 0,
        "not_measured": [],
        "excluded": [],
    }
    for s in syms:
        if not s.endswith("USDT"):
            continue
        meta = info.get(s) or {}
        if meta.get("status") == "TRADING":
            continue
        base = s[:-4]
        if meta.get("underlyingType") not in (None, "COIN") or str(meta.get("contractType", "PERPETUAL")) != "PERPETUAL":
            out["excluded"].append({"symbol": s, "why": f"{meta.get('underlyingType')}/{meta.get('contractType')}"})
            continue
        if base in bench_bases:
            out["excluded"].append({"symbol": s, "why": "in bench universe (live on 2026-10-17)"})
            continue
        mult, spot_base = 1, base
        if (g := alts.MULT.match(base)) is not None:
            mult, spot_base = int(g.group(1)), g.group(2)
        if spot_base in alts.STABLES:
            continue
        rows = archive_funding(s)
        if not rows:
            out["not_measured"].append({"symbol": s, "why": "no funding in cache"})
            continue
        last = int(rows[-1][0])
        if last < start or last > end:
            out["out_of_window"] += 1
            continue
        if f"{spot_base}USDT" not in spots:
            out["no_spot"].append(s)
            continue
        if last >= OCT_TAIL_MS:
            out["not_measured"].append({"symbol": s, "why": "funding runs to the end of 2026-09 (October archive not out)"})
            continue
        fw = [{"ts_ms": int(r[0]), "rate": float(r[2])} for r in rows if start <= r[0] <= end]
        rec = {"symbol": s, "spot": f"{spot_base}USDT", "mult": mult, "first_ms": int(fw[0]["ts_ms"]) if fw else None, "last_ms": last}
        if fw and alts.max_trailing_apr(fw) >= 0.20:
            out["hot"].append(rec)
        else:
            out["not_hot"].append(s)
    (cache / "candidates.json").write_text(json.dumps(out, indent=1))
    log(
        f"candidates: hot {len(out['hot'])}, not hot {len(out['not_hot'])}, no spot pair {len(out['no_spot'])}, "
        f"out of window {out['out_of_window']}, not measured {len(out['not_measured'])}, excluded {len(out['excluded'])}"
    )
    for r in out["not_measured"]:
        print(f"  not measured: {r['symbol']}: {r['why']}")


# ---------------------------------------------------------------- stage: fetch


def kpath(cache: Path, kind: str, sym: str, y: int, m: int) -> Path:
    # vision_v: rows [ts, open, high, low, close, base volume]; the first cache (vision/) had no volume, which hid the
    # archive's flat zero-volume bars after a delisting (found after the first run; see the report).
    return cache / "vision_v" / kind / sym / f"{y:04d}-{m:02d}.json"


def fetch_jobs(cache: Path) -> list[tuple[str, str, int, int]]:
    c = json.loads((cache / "candidates.json").read_text())
    start, end = c["window"]
    jobs = []
    for r in c["hot"]:
        lo = max(start, r["first_ms"] - 8 * 86_400_000)
        for y, m in month_range(lo, min(r["last_ms"] + 2 * 86_400_000, end)):
            jobs.append(("um", r["symbol"], y, m))
        for y, m in month_range(lo, min(r["last_ms"] + 40 * 86_400_000, end)):
            jobs.append(("spot", r["spot"], y, m))
    return [j for j in jobs if (j[2], j[3]) <= (2026, 9)]


async def stage_fetch(cache: Path, limit: int | None) -> None:
    from tradedesk.server import build_server

    jobs = [j for j in fetch_jobs(cache) if not kpath(cache, j[0], j[1], j[2], j[3]).exists()]
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
                rows: list[list[float]] | None = (await go(sym, "4h", y, m)).data
            except ProviderError as e:
                if e.reason != "data_unavailable":
                    errors[e.reason] = errors.get(e.reason, 0) + 1
                    return  # transient: a rerun retries
                rows = None
            p = kpath(cache, kind, sym, y, m)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(None if rows is None else [r[:6] for r in rows]))
            done += 1
            if done % 100 == 0:
                log(f"  {done}/{len(jobs)} files, {time.time() - t0:.0f} s, errors {errors}")

    try:
        await asyncio.gather(*(one(*j) for j in jobs))
    finally:
        await rt.aclose()
    log(f"fetch done: {done}/{len(jobs)} in {time.time() - t0:.0f} s, errors {errors}")


# ---------------------------------------------------------------- stage: run


def load_bars(cache: Path, kind: str, sym: str, lo: int, hi: int, mult: int = 1) -> list[dict[str, Any]]:
    """4h bars in ``[lo, hi]`` with base volume > 0 (zero-volume bars are the archive's filler after a delisting)."""
    out: list[dict[str, Any]] = []
    d = cache / "vision_v" / kind / sym
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        for r in json.loads(p.read_text()) or []:
            if lo <= r[0] <= hi and r[5] > 0:
                out.append({"ts": int(r[0]), "open": r[1] * mult, "high": r[2] * mult, "low": r[3] * mult, "close": r[4] * mult})
    out.sort(key=lambda b: b["ts"])
    return out


def load_candidates(
    cache: Path, bar_lo: int, bar_hi: int, *, tol_ms: int = H8, exclude: frozenset[str] = frozenset()
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    c = json.loads((cache / "candidates.json").read_text())
    start, end = c["window"]
    mk: dict[str, dict[str, Any]] = {}
    not_measured = list(c["not_measured"])
    meta: dict[str, Any] = {}
    for r in c["hot"]:
        s = r["symbol"]
        if s in exclude:  # exploratory re-runs without one event (critic B, blocker 3)
            continue
        rows = archive_funding(s)
        funding = [{"ts_ms": int(x[0]), "rate": float(x[2])} for x in rows if start <= x[0] <= end]
        perp = load_bars(cache, "um", s, bar_lo, bar_hi)
        spot = load_bars(cache, "spot", r["spot"], bar_lo, bar_hi, r["mult"])
        if not perp or not spot:
            not_measured.append({"symbol": s, "why": f"no 4h bars in archive (perp {len(perp)}, spot {len(spot)})"})
            continue
        sp = {b["ts"]: b["close"] for b in spot}
        dev = [abs(b["close"] / sp[b["ts"]] - 1) for b in perp if sp.get(b["ts"])]
        med = statistics.median(dev) if dev else None
        if med is None or med > 0.05:
            not_measured.append({"symbol": s, "why": f"price levels do not match (median |perp/spot-1| {med})"})
            continue
        lo, hi = perp[0]["ts"], perp[-1]["ts"]
        gaps = (hi - lo) // H4 + 1 - len(perp)
        # Correction found after the first run: after a delisting the archive keeps writing a constant default rate
        # (0.0001 / 8h, 0.00005 / 4h) until a bulk cleanup on 2025-06-19. No funding is paid without a contract, so
        # rows later than the last traded perp bar close + tol (8 h, chosen after the run; 0 h as sensitivity) are
        # dropped as filler.
        last_traded = hi + H4
        n_filler = sum(1 for x in funding if x["ts_ms"] > last_traded + tol_ms)
        funding = [x for x in funding if x["ts_ms"] <= last_traded + tol_ms]
        name = f"{s[:-4]}/USDT:USDT"
        mk[name] = {"funding": funding, "perp": perp, "spot": spot}
        meta[name] = {
            "symbol": s, "rows": [x for x in rows if x[0] <= last_traded + tol_ms], "median_dev": med, "perp_gaps": gaps,
            "spot_last": spot[-1]["ts"] + H4, "funding_filler_rows": n_filler,
        }  # fmt: skip
    return mk, not_measured, meta


def stats_row(res: dict[str, Any], years: float) -> dict[str, Any]:
    rk = risk_summary(res["equity"], years, CAPITAL, n_boot=N_BOOT)
    return {**rk, "n_trades": len(res["trades"])}


def diff_row(eq_a: list[tuple[int, float]], eq_b: list[tuple[int, float]], years: float) -> dict[str, Any]:
    d = diff_curve(eq_a, eq_b)
    rk = risk_summary(d, years, CAPITAL, n_boot=N_BOOT)
    out = {"apr_on_capital": rk["apr_on_capital"], "ci": rk["apr_on_capital_ci"], "n_points": len(d)}
    for k, lo, hi in (("is", 0, SPLIT_MS), ("oos", SPLIT_MS, None)):
        b = block_bootstrap([v for _, v in window_weekly(d, lo, hi)], n_boot=N_BOOT)
        out[k] = {"weeks": b["weeks"], "apr_on_capital": None if b["apr"] is None else b["apr"] / CAPITAL}
    return out


def stage_run(cache: Path, *, tol_h: float = 8.0, exclude: frozenset[str] = frozenset(), out_name: str = "results.json") -> None:
    start, end, uni = bench_window()
    cal = json.loads(CALIB.read_text())
    models = {k: CostModel(cal["models"][k]["fees"], cal["models"][k]["slippage"], cal["models"][k]["timing"]) for k in ("main", "stress")}
    bench = alts.load_markets(BENCH, DAYS, uni)  # type: ignore[arg-type]
    end = max(int(e["ts_ms"]) for d in bench.values() for e in d["funding"])  # the frozen simulator's own end
    bar_lo = min(b["ts"] for d in bench.values() for b in d["perp"][:1])
    bar_hi = max(b["ts"] for d in bench.values() for b in d["perp"][-1:])
    cand, not_measured, meta = load_candidates(cache, bar_lo, bar_hi, tol_ms=int(tol_h * 3_600_000), exclude=exclude)
    full = {**bench, **cand}
    log(
        f"bench {len(bench)} symbols, candidates with data {len(cand)}, not measured {len(not_measured)}; bars {fmt(bar_lo)} … {fmt(bar_hi)}"
        f"; funding tolerance {tol_h:g} h; excluded {sorted(exclude) or '-'}"
    )
    ev = vanish_events(full, end, n_cycles=N_MAIN)
    pe = {s: e["perp_end"] for s, e in ev.items()}
    log(
        f"perp ended before the window end: {len(ev)} ({sum(1 for s in ev if s in bench)} in bench), spot still trading after: {sum(1 for e in ev.values() if e['spot_after'])}"
    )
    out: dict[str, Any] = {"window": [start, end], "not_measured": not_measured, "candidates": {}, "models": {}}
    for name, m in meta.items():
        e = ev.get(name)
        ch = interval_change_before(m["rows"], e["perp_end"]) if e else None
        out["candidates"][name] = {
            "symbol": m["symbol"], "median_dev": m["median_dev"], "perp_gaps": m["perp_gaps"],
            "perp_end": e["perp_end"] if e else None, "spot_after": e["spot_after"] if e else None, "interval_change_30d": ch,
        }  # fmt: skip
    for mname, model in models.items():
        kw = dict(slots=5, lookback_days=7, entry_apr=0.20, exit_apr=0.0, spot_fee=model.fees["spot_binance"], perp_fee=model.fees["perp_binance"], slippage=model.sim_slippage(LEGS))  # fmt: skip
        r1 = simulate_carry_portfolio(bench, **kw)
        years = r1["stats"]["years"]
        chk = simulate_carry_delisting(bench, end_ms=end, **kw)
        same = len(chk["trades"]) == len(r1["trades"]) and abs(chk["stats"]["total_return"] - r1["stats"]["total_return"]) < 1e-9
        log(f"{mname}: (i) {len(r1['trades'])} trades, reproduction by the new walk: {same}")
        del chk
        runs = {"i": r1, "ii": simulate_carry_delisting(full, perp_end=pe, end_ms=end, **kw)}
        for n in (N_MAIN, *N_SENS):
            runs[f"iii_n{n}"] = simulate_carry_delisting(full, perp_end=pe, close_on_vanish=True, n_cycles=n, end_ms=end, **kw)
        # exploratory (added after the first run): perp leg settled at the spot (index proxy) instead of its last close
        runs["ii_spotsettle"] = simulate_carry_delisting(full, perp_end=pe, settle="spot", end_ms=end, **kw)
        runs["iii_n2_spotsettle"] = simulate_carry_delisting(
            full, perp_end=pe, close_on_vanish=True, n_cycles=N_MAIN, settle="spot", end_ms=end, **kw
        )
        mo: dict[str, Any] = {"variants": {}, "diffs": {}, "events": {}}
        for k, r in runs.items():
            row = stats_row(r, years)
            row["n_perp_ended"] = sum(1 for t in r["trades"] if t.get("perp_ended"))
            row["n_rule_closes"] = len(r.get("rule_closes", []))
            row["n_candidate_trades"] = sum(1 for t in r["trades"] if t["symbol"] in cand)
            if "rule_closes" in r:
                # false trigger: the perp has data again later (it did not end for good)
                row["n_false_triggers"] = sum(1 for x in r["rule_closes"] if x["symbol"] not in pe or x["t"] < pe[x["symbol"]])
            mo["variants"][k] = row
        mo["diffs"]["ii_minus_i"] = diff_row(runs["ii"]["equity"], r1["equity"], years)
        for n in (N_MAIN, *N_SENS):
            mo["diffs"][f"iii_n{n}_minus_ii"] = diff_row(runs[f"iii_n{n}"]["equity"], runs["ii"]["equity"], years)
        mo["diffs"]["ii_spotsettle_minus_i"] = diff_row(runs["ii_spotsettle"]["equity"], r1["equity"], years)
        mo["diffs"]["iii_n2_spotsettle_minus_ii_spotsettle"] = diff_row(
            runs["iii_n2_spotsettle"]["equity"], runs["ii_spotsettle"]["equity"], years
        )
        # candidate trades of (ii) and per-symbol contribution (APR on capital; slot weight 1/5)
        ct = [t for t in runs["ii"]["trades"] if t["symbol"] in cand]
        mo["candidate_trades_ii"] = ct
        by: dict[str, float] = {}
        for t in ct:
            by[t["symbol"]] = by.get(t["symbol"], 0.0) + t["pnl"] / 5 / years / CAPITAL
            sl = meta[t["symbol"]]["spot_last"]
            t["spot_ended"] = t["open_ts"] <= sl < t["close_ts"] and not (t["symbol"] in pe and pe[t["symbol"]] <= sl)
        mo["candidate_contrib_ii"] = dict(sorted(by.items(), key=lambda kv: kv[1]))
        se = [t for t in ct if t["spot_ended"]]
        mo["spot_ended_first_ii"] = {"n": len(se), "apr_on_capital": sum(t["pnl"] for t in se) / 5 / years / CAPITAL}
        # (ii) - (i) without the largest single event contribution (by slot-weighted P&L of perp-ended trades)
        ended = [t for t in runs["ii"]["trades"] if t.get("perp_ended")]
        for t in ended:
            m_i = next((x for x in runs[f"iii_n{N_MAIN}"]["trades"] if x["symbol"] == t["symbol"] and x["open_ts"] == t["open_ts"]), None)
            nm = out["candidates"].get(t["symbol"], {})
            mo["events"][f"{t['symbol']}@{t['open_ts']}"] = {
                "symbol": t["symbol"], "open": t["open_ts"], "close_ii": t["close_ts"], "perp_end": pe.get(t["symbol"]),
                "pnl_ii": t["pnl"], "basis_ii": t["basis"], "funding_ii": t["funding"],
                "close_iii": m_i["close_ts"] if m_i else None, "pnl_iii": m_i["pnl"] if m_i else None,
                "reason_iii": m_i.get("reason") if m_i else None, "interval_change": nm.get("interval_change_30d"),
                "naked_cycles_ii": (t["close_ts"] - pe[t["symbol"]]) // H8 if t["symbol"] in pe else None,
            }  # fmt: skip
        mo["n_trades_ii"] = len(runs["ii"]["trades"])
        mo["share_trades_with_event_ii"] = len(ended) / max(len(runs["ii"]["trades"]), 1)
        out["models"][mname] = mo
        del runs, r1
        gc.collect()
    out["options"] = {"funding_tol_h": tol_h, "exclude": sorted(exclude)}
    (cache / out_name).write_text(json.dumps(out, indent=1, default=str))
    report(out)


def report(out: dict[str, Any]) -> None:
    print(f"\nwindow {fmt(out['window'][0])} … {fmt(out['window'][1])}; returns on capital = notional / {CAPITAL}")
    for mname, mo in out["models"].items():
        print(f"\n== cost model: {mname} ==")
        print(
            "| variant | trades | cand. trades | perp ended in trade | rule closes (false) | APR notional | APR capital | 95% CI | max DD cap | worst week cap (start) |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|")
        for k, r in mo["variants"].items():
            lo, hi = r["apr_on_capital_ci"]
            fl = f"{r['n_rule_closes']} ({r.get('n_false_triggers', 0)})" if k.startswith("iii") else "—"
            print(
                f"| {k} | {r['n_trades']} | {r['n_candidate_trades']} | {r['n_perp_ended']} | {fl} | {pct(r['apr_on_notional'], 2)} | {pct(r['apr_on_capital'], 2)} | "
                f"[{pct(lo, 2)}; {pct(hi, 2)}] | {pct(-r['max_dd_on_capital'], 2)} | {pct(r['worst_week_on_capital'], 2)} ({fmt(r['worst_week_start_ms'])[:10]}) |"
            )
        print("| difference | APR capital | 95% CI | IS (<2025-03-01) | OOS |")
        print("|---|---|---|---|---|")
        for k, d in mo["diffs"].items():
            print(
                f"| {k} | {pct(d['apr_on_capital'], 2)} | [{pct(d['ci'][0], 2)}; {pct(d['ci'][1], 2)}] | {pct(d['is']['apr_on_capital'], 2)} ({d['is']['weeks']} wk) | {pct(d['oos']['apr_on_capital'], 2)} ({d['oos']['weeks']} wk) |"
            )
        print(f"share of (ii) trades with the event: {mo['share_trades_with_event_ii']:.3f} of {mo['n_trades_ii']}")
        print(
            "| symbol | open | perp end | close (ii) | naked 8h cycles | P&L (ii) | basis (ii) | close (iii n2) | P&L (iii) | saving | interval change 30d |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        for e in mo["events"].values():
            ch = e["interval_change"]
            chs = (
                f"{ch['from_h']:.0f}h→{ch['to_h']:.0f}h {fmt(ch['at_ms'])[:10]}"
                if ch and ch["changed"]
                else ("no" if ch else "n/a (bench)")
            )
            sav = None if e["pnl_iii"] is None else e["pnl_iii"] - e["pnl_ii"]
            print(
                f"| {e['symbol']} | {fmt(e['open'])} | {fmt(e['perp_end'])} | {fmt(e['close_ii'])} | {e['naked_cycles_ii']} | {pct(e['pnl_ii'], 2)} | {pct(e['basis_ii'], 2)} | "
                f"{fmt(e['close_iii'])} | {pct(e['pnl_iii'], 2)} | {pct(sav, 2)} | {chs} |"
            )
        ct = mo.get("candidate_trades_ii", [])
        print(f"candidate (delisted) trades in (ii): {len(ct)}; P&L per trade of slot notional, worst first")
        print(
            "| symbol | open | close | days | entry APR | funding | basis | pnl | max runup | max premium rise | perp ended | spot ended first |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for t in sorted(ct, key=lambda t: t["pnl"]):
            print(
                f"| {t['symbol']} | {fmt(t['open_ts'])} | {fmt(t['close_ts'])} | {(t['close_ts'] - t['open_ts']) / 86_400_000:.1f} | {t['entry_apr']:.2f} | "
                f"{pct(t['funding'], 2)} | {pct(t['basis'], 2)} | {pct(t['pnl'], 2)} | {t['max_runup']:.2f} | {t['max_premium_rise']:+.3f} | {t.get('perp_ended')} | {t.get('spot_ended')} |"
            )
        s1 = mo.get("spot_ended_first_ii", {})
        print(
            f"trades where the spot ended first (frozen engine holds a perp-only 'position'): {s1.get('n')}, contribution {pct(s1.get('apr_on_capital'), 2)} APR on capital"
        )
        print(
            "contribution to (ii) by delisted symbol, APR on capital: "
            + ", ".join(f"{k.split('/')[0]} {pct(v, 2)}" for k, v in mo.get("candidate_contrib_ii", {}).items())
        )
    nm = out["not_measured"]
    print(f"\nnot measured ({len(nm)}): " + "; ".join(f"{x['symbol']} ({x['why']})" for x in nm))
    c = out["candidates"]
    n_ch = sum(1 for v in c.values() if v["interval_change_30d"] and v["interval_change_30d"]["changed"])
    n_end = sum(1 for v in c.values() if v["perp_end"])
    print(f"candidates with data {len(c)}, perp ended in window {n_end}, interval change within 30 d before the end {n_ch}")
    print(
        f"max median |perp/spot-1| {max((v['median_dev'] for v in c.values()), default=None)}, perp bar gaps total {sum(v['perp_gaps'] for v in c.values())}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["candidates", "fetch", "run", "report"])
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--limit", type=int, default=None, help="fetch: only the first N missing files (timing sample)")
    ap.add_argument("--funding-tol-h", type=float, default=8.0, help="run: funding kept up to last traded bar close + H")
    ap.add_argument("--exclude", default="", help="run: comma-separated candidate symbols to drop (e.g. A2ZUSDT)")
    ap.add_argument("--out", default="results.json", help="run/report: results file name in the cache dir")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "candidates":
        asyncio.run(stage_candidates(cache))
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(cache, a.limit))
    elif a.stage == "run":
        ex = frozenset(s.strip() for s in a.exclude.split(",") if s.strip())
        stage_run(cache, tol_h=a.funding_tol_h, exclude=ex, out_name=a.out)
    else:
        report(json.loads((cache / a.out).read_text()))
    log("done")


if __name__ == "__main__":
    main()
