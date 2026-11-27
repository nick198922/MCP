"""Reconcile the collector's Coinalyze table with the venues' own public history (card IDEA-20261017-doubletop-to-ours,
item 4c; report docs/verification/2026-11-27-coinalyze-bybit-check.md).

    uv run python scripts/verify_coinalyze_bybit.py --out ~/.cache/tradedesk-agents/coinalyze-bybit/result.json

1. Table ``coinalyze_history`` of the live database, opened READ-ONLY (sqlite URI ``mode=ro``): per symbol and metric
   rows, first/last day, missing days, duplicates, the last settled day, orders of magnitude.
2. Bybit ``.6`` (and Binance ``.A`` as a control of the method) against the venue: funding settlements (ccxt
   ``fetch_funding_rate_history`` through ``rt.exchanges``), account long/short ratio (``rt.bybit`` /
   ``rt.binance_futures``), OI (Bybit only, a sanity line: Bybit coins x Bybit daily close).
3. ``liquidations_history`` through the running MCP service (``scripts/mcp_call.py``: the service holds the key, this
   clone has none): binance, bybit and binance+bybit, sum = parts on shared closed days; and a re-read of the ``.A``
   funding bars through ``perps_funding_history`` source=coinalyze against the stored ones.
4. For each funding bar field the days it does not equal the expected settlement: what it holds instead (previous
   day's last rate, the base rate 0.0001, neither, outside the day's range), and the informative days of each check.
5. The verdict table: every mark derived by ``verdict()`` from the saved stats (``result.json`` -> ``verdicts``).

Tolerances and verdict thresholds are fixed constants (below and in analytics/coinalyze_recon.py), written on
2026-11-27 before the first comparison ran (not committed separately, so git cannot show the order). Network only
through the runtime providers and the MCP service. Each venue response is cached as JSON under ``--cache-dir``
(``--refresh`` re-fetches).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import resource
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_call  # noqa: E402  (scripts/mcp_call.py: the running MCP service)

from tradedesk.analytics.coinalyze_recon import (  # noqa: E402
    DAY,
    FUNDING_CANDIDATES,
    best_key,
    compare_pairs,
    compare_series,
    coverage,
    day_floor,
    funding_candidates_by_day,
    informative_days,
    liquidation_sum_check,
    mismatch_breakdown,
    settlements_per_day,
    verdict,
)
from tradedesk.collector.coinalyze_history import settled_end  # noqa: E402
from tradedesk.server import build_server  # noqa: E402

# ---- tolerances, fixed 2026-11-27 before the first comparison -------------------------------------------------------
FUNDING_ABS_TOL = 2e-8  # decimal per interval; Bybit/Binance publish 8 decimals (1e-8), 2 ticks of slack
LS_SHARE_ABS_TOL = 1e-4  # long share in [0, 1]; Bybit buyRatio has 4 decimals, 1 tick of slack
LS_RATIO_REL_TOL = 1e-3  # long/short ratio (secondary line)
OI_REL_TOL = 0.01  # Coinalyze USD OI vs Bybit coins x Bybit close: 1 % (closes of venues differ by bps)
LIQ_ABS_TOL, LIQ_REL_TOL = 1.0, 1e-9  # USD: the sum is plain addition of the same rows
REREAD_ABS_TOL = 1e-12  # stored bar vs the same bar asked again: identical floats expected
INTERNAL_REL_TOL = 1e-3  # stored ratio vs long_share / short_share

DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
CACHE = Path.home() / ".cache" / "tradedesk-agents" / "coinalyze-bybit"
SYMBOLS = {
    "BTCUSDT.6": ("bybit", "BTC"),
    "ETHUSDT.6": ("bybit", "ETH"),
    "BTCUSDT_PERP.A": ("binance", "BTC"),
    "ETHUSDT_PERP.A": ("binance", "ETH"),
}
CCXT_ID = {"bybit": "bybit", "binance": "binanceusdm"}


LINES: list[str] = []  # everything printed, saved next to --out as .txt
BRIEF = {"on": False}


def emit(*parts: Any, flush: bool = False) -> None:
    """Print and keep the line; with --brief only headers, best-candidate lines and checks reach the console."""
    s = " ".join(str(p) for p in parts)
    LINES.append(s)
    if not BRIEF["on"] or not s.startswith("    ") and " 1h " not in s and "next_day" not in s and "prev_day" not in s:
        print(s, flush=flush)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def say(msg: str) -> None:
    emit(f"[{time.strftime('%H:%M:%S')} rss {rss_mb():.0f} MB] {msg}", flush=True)


def day(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts is not None else "-"


# ---- 1. the table --------------------------------------------------------------------------------------------------
def read_table(db: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    try:
        for r in con.execute("SELECT * FROM coinalyze_history ORDER BY symbol, metric, ts"):
            out.setdefault((r["symbol"], r["metric"]), []).append(dict(r))
    finally:
        con.close()
    return out


def magnitude(metric: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if metric == "long_short":
        ls = [r["long_share"] for r in rows]
        sums = [r["long_share"] + r["short_share"] for r in rows]
        consistent = sum(
            1 for r in rows if r["short_share"] and abs(r["ratio"] - r["long_share"] / r["short_share"]) <= INTERNAL_REL_TOL * r["ratio"]
        )
        return {
            "long_share_median": statistics.median(ls),
            "long_share_min": min(ls),
            "long_share_max": max(ls),
            "shares_sum_minmax": [min(sums), max(sums)],
            "ratio_consistent_share": consistent / len(rows),
        }
    closes = [r["close"] for r in rows]
    return {"close_median": statistics.median(closes), "close_min": min(closes), "close_max": max(closes)}


# ---- 2. venue history (cached) -------------------------------------------------------------------------------------
async def cached(cache: Path, name: str, refresh: bool, fetch: Any) -> Any:
    p = cache / f"{name}.json"
    if p.exists() and not refresh:
        return json.loads(p.read_text())
    data = await fetch()
    p.write_text(json.dumps(data, default=str))
    return data


async def fetch_venue(rt: Any, cache: Path, refresh: bool, start: int, end: int) -> dict[str, Any]:
    v: dict[str, Any] = {}
    for venue in ("bybit", "binance"):
        for base in ("BTC", "ETH"):
            ex, sym = CCXT_ID[venue], f"{base}/USDT:USDT"

            async def funding(ex: str = ex, sym: str = sym) -> Any:
                f = await rt.exchanges.funding_rate_history_range(
                    ex, sym, start * 1000, (end + 2 * DAY) * 1000, max_pages=60, trust_short_page=ex != "bybit"
                )
                d = f.data
                return {
                    "rows": [[int(x["timestamp"]), x.get("fundingRate")] for x in d["rows"]],
                    "complete": d["complete"],
                    "dropped": d["dropped_rows"],
                }

            v[f"funding:{venue}:{base}"] = await cached(cache, f"funding_{venue}_{base}", refresh, funding)
            say(f"funding {venue} {base}: {len(v[f'funding:{venue}:{base}']['rows'])} settlements")
            native = f"{base}USDT"
            for period in ("1d", "1h"):
                if venue == "bybit":

                    async def ls(native: str = native, period: str = period) -> Any:
                        return (await rt.bybit.long_short_account_ratio(native, period, 500)).data

                else:

                    async def ls(native: str = native, period: str = period) -> Any:
                        return (await rt.binance_futures.long_short_account_ratio(native, period, 30 if period == "1d" else 500)).data

                v[f"ls:{venue}:{base}:{period}"] = await cached(cache, f"ls_{venue}_{base}_{period}", refresh, ls)
                say(f"long/short {venue} {base} {period}: {len(v[f'ls:{venue}:{base}:{period}'])} rows")
            if venue == "bybit":
                lo = (end - 400 * DAY) * 1000

                async def oi(native: str = native, lo: int = lo) -> Any:
                    return (await rt.bybit.open_interest_range(native, "1d", lo, (end + DAY) * 1000, max_pages=4)).data

                async def ohlcv(sym: str = sym, lo: int = lo) -> Any:
                    return (await rt.exchanges.ohlcv("bybit", sym, "1d", 410, since_ms=lo, until_ms=(end + DAY) * 1000)).data

                v[f"oi:{base}"] = await cached(cache, f"oi_bybit_{base}", refresh, oi)
                v[f"ohlcv:{base}"] = await cached(cache, f"ohlcv_bybit_{base}", refresh, ohlcv)
                say(f"OI bybit {base}: {len(v[f'oi:{base}']['rows'])} rows, ohlcv {len(v[f'ohlcv:{base}'])} bars")
    return v


# ---- 3. the MCP service --------------------------------------------------------------------------------------------
async def mcp(tool: str, args: dict[str, Any], url: str) -> dict[str, Any]:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # build_server() configures INFO logging
    code, text = await mcp_call.run(tool, args, False, False, url)
    if code != 0:
        return {"ok": False, "error": f"exit {code}: {text[:300]}"}
    return json.loads(text)


# ---- comparisons ---------------------------------------------------------------------------------------------------
EXPECTED = {"close": "last_in_day", "high": "max_in_day", "low": "min_in_day", "open": "first_in_day"}


def funding_checks(rows: list[dict[str, Any]], settlements: list[list[Any]]) -> dict[str, Any]:
    """Per bar field: every candidate definition, plus for the expected one the informative days and, on the days it
    does not match, what the bar holds instead (``explain``)."""
    pts = [(int(t) // 1000, float(r)) for t, r in settlements if r is not None]
    cands = funding_candidates_by_day(pts)
    out: dict[str, Any] = {}
    for field in ("close", "high", "low", "open"):
        ours = {r["ts"]: r[field] for r in rows}
        res = {}
        for c in FUNDING_CANDIDATES:
            ref = {d: x[c] for d, x in cands.items() if c in x}
            res[c] = compare_series(ours, ref, 0, FUNDING_ABS_TOL)
        res["informative"] = informative_days(cands, ours, EXPECTED[field], FUNDING_ABS_TOL)
        res["explain"] = mismatch_breakdown(ours, cands, EXPECTED[field], FUNDING_ABS_TOL)
        out[field] = res
    lo, hi = min(r["ts"] for r in rows), max(r["ts"] for r in rows)
    out["settlements_per_day"] = settlements_per_day(t for t, _ in pts if lo <= t < hi + DAY)
    return out


def ls_ref(venue: str, rows: list[dict[str, Any]]) -> tuple[dict[int, float], dict[int, float]]:
    share, ratio = {}, {}
    for x in rows:
        t = int(x["timestamp"]) // 1000
        if venue == "bybit":
            b, s = float(x["buyRatio"]), float(x["sellRatio"])
        else:
            b, s = float(x["longAccount"]), float(x["shortAccount"])
        share[t] = b
        if s:
            ratio[t] = b / s
    return share, ratio


LS_OFFSETS_1D = {"same_ts": 0, "next_day": DAY, "prev_day": -DAY}
LS_OFFSETS_1H = {"00:00": 0, "01:00": 3600, "12:00": 12 * 3600, "23:00": 23 * 3600, "next 00:00": DAY, "prev 23:00": -3600}


def ls_checks(rows: list[dict[str, Any]], venue: str, v1d: list[dict[str, Any]], v1h: list[dict[str, Any]]) -> dict[str, Any]:
    ours_share = {r["ts"]: r["long_share"] for r in rows}
    ours_ratio = {r["ts"]: r["ratio"] for r in rows}
    out: dict[str, Any] = {}
    for period, data, offsets in (("1d", v1d, LS_OFFSETS_1D), ("1h", v1h, LS_OFFSETS_1H)):
        share, ratio = ls_ref(venue, data)
        out[period] = {
            k: {
                "share": compare_series(ours_share, share, off, LS_SHARE_ABS_TOL),
                "ratio": compare_series(ours_ratio, ratio, off, 0.0, LS_RATIO_REL_TOL),
            }
            for k, off in offsets.items()
        }
    s1d, _ = ls_ref(venue, v1d)
    s1h, _ = ls_ref(venue, v1h)
    out["venue_1d_vs_1h_same_ts"] = compare_series(s1d, s1h, 0, 1e-9)
    return out


def oi_checks(rows: list[dict[str, Any]], oi: dict[str, Any], bars: list[list[Any]]) -> dict[str, Any]:
    coins = {int(x["timestamp"]) // 1000: float(x["openInterest"]) for x in oi["rows"]}
    close = {day_floor(int(b[0]) // 1000): float(b[4]) for b in bars}
    ours = {r["ts"]: r["close"] for r in rows}
    out = {}
    for k, off in (("oi_same_ts", 0), ("oi_next_day", DAY)):
        ref = {d: coins[d + off] * close[d] for d in close if d + off in coins}
        out[k] = compare_series(ours, ref, 0, 0.0, OI_REL_TOL)
    return out


def liq_rows(env: dict[str, Any]) -> list[dict[str, Any]]:
    return ((env.get("data") or {}).get("rows") or []) if env.get("ok") else []


# ---- printing ------------------------------------------------------------------------------------------------------
def line(label: str, s: dict[str, Any]) -> str:
    sh = "-" if s.get("share") is None else f"{s['share']:.3f}"
    md = "-" if s.get("median_abs_diff") is None else f"{s['median_abs_diff']:.3g}"
    sc = "-" if s.get("scale") is None else f"{s['scale']:.4g}"
    return f"  {label:<34} n={s.get('n', 0):>5} match={sh:>6} med|d|={md:>9} scale={sc:>7} {day(s.get('first'))}..{day(s.get('last'))} {verdict(s)}"


async def main_async(a: argparse.Namespace) -> dict[str, Any]:
    cache: Path = a.cache_dir  # resolved and created in main()
    now = int(time.time())
    last_settled = settled_end(now) - DAY
    res: dict[str, Any] = {"now": now, "last_settled_day": day(last_settled), "tolerances": {
        "funding_abs": FUNDING_ABS_TOL, "ls_share_abs": LS_SHARE_ABS_TOL, "ls_ratio_rel": LS_RATIO_REL_TOL, "oi_rel": OI_REL_TOL,
        "liq_abs": LIQ_ABS_TOL, "liq_rel": LIQ_REL_TOL, "reread_abs": REREAD_ABS_TOL}}  # fmt: skip

    say(f"1. table {a.db} (read-only), last settled day {day(last_settled)}")
    table = read_table(a.db)
    res["table"] = {}
    for (sym, metric), rows in sorted(table.items()):
        cov = coverage([r["ts"] for r in rows], last_settled)
        mag = magnitude(metric, rows)
        res["table"][f"{sym}:{metric}"] = {**cov, **mag}
        mags = " ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}" for k, v in mag.items())
        emit(
            f"  {sym:<15} {metric:<10} rows={cov['rows']} dup={cov['duplicates']} miss={cov['missing_days']} "
            f"misal={cov['misaligned']} {day(cov['first'])}..{day(cov['last'])} last_settled={cov['has_last_settled']} {mags}"
        )
    start = min(r["ts"] for rows in table.values() for r in rows)
    end = max(r["ts"] for rows in table.values() for r in rows)

    say("2. venue history")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _, rt = build_server()
    try:
        v = await fetch_venue(rt, cache, a.refresh, start, end)
    finally:
        await rt.aclose()

    res["funding"], res["long_short"], res["oi"] = {}, {}, {}
    for sym, (venue, base) in SYMBOLS.items():
        f = v[f"funding:{venue}:{base}"]
        fc = funding_checks(table[(sym, "funding")], f["rows"])
        res["funding"][sym] = {"venue_complete": f["complete"], "dropped": f["dropped"], **fc}
        emit(f"\nfunding {sym} vs {venue} settlements (walk complete={f['complete']}, abs tol {FUNDING_ABS_TOL:g})")
        emit(f"  settlements per day (table days): {fc['settlements_per_day']}")
        for field in EXPECTED:
            cs = fc[field]
            only = {c: cs[c] for c in FUNDING_CANDIDATES}
            k = best_key(only)
            emit(line(f"{field} best={k}", cs[k]) if k else f"  {field}: no pairs")
            exp, inf, ex = EXPECTED[field], cs["informative"], cs["explain"]
            emit(
                f"   {field} expected={exp}: informative days {inf['informative']}/{inf['n']}; mismatch {ex['mismatch']}/{ex['n']} "
                f"= prev_last {ex['eq_prev_last']}, base 1e-4 {ex['eq_base']}, neither {ex['eq_neither']}; above day max "
                f"{ex['above_day_max']}, below day min {ex['below_day_min']}; distinguishing {ex['distinguishing']} "
                f"(= prev_last {ex['distinguishing_eq_prev_last']}, = base {ex['distinguishing_eq_base']}); by year {ex['by_year']}"
            )
            for c in FUNDING_CANDIDATES:
                if c != k:
                    emit(line(f"  {field} vs {c}", cs[c]))
        ls = ls_checks(table[(sym, "long_short")], venue, v[f"ls:{venue}:{base}:1d"], v[f"ls:{venue}:{base}:1h"])
        res["long_short"][sym] = ls
        emit(f"long/short {sym} vs {venue} account ratio (share abs tol {LS_SHARE_ABS_TOL:g}, ratio rel {LS_RATIO_REL_TOL:g})")
        for period in ("1d", "1h"):
            for k, s in ls[period].items():
                emit(line(f"{period} {k} long_share", s["share"]))
                emit(line(f"{period} {k} ratio", s["ratio"]))
        emit(line("venue 1d vs venue 1h same ts", ls["venue_1d_vs_1h_same_ts"]))
        if venue == "bybit":
            oc = oi_checks(table[(sym, "oi_usd")], v[f"oi:{base}"], v[f"ohlcv:{base}"])
            res["oi"][sym] = oc
            emit(f"OI {sym} vs bybit coins x bybit close (rel tol {OI_REL_TOL:g})")
            for k, s in oc.items():
                emit(line(k, s))

    say("3. MCP service: liquidations_history and a re-read of the .A funding bars")
    res["liquidations"], res["reread"] = {}, {}
    today = day_floor(now)
    for base in ("BTC", "ETH"):
        envs = {}
        for name, ex in (("binance", ["binance"]), ("bybit", ["bybit"]), ("both", ["binance", "bybit"])):
            envs[name] = await mcp("liquidations_history", {"symbol": base, "days": a.liq_days, "exchanges": ex}, a.mcp_url)
        chk = liquidation_sum_check(
            liq_rows(envs["both"]), [liq_rows(envs["binance"]), liq_rows(envs["bybit"])], LIQ_ABS_TOL, LIQ_REL_TOL, skip_ts=[today]
        )
        meta = {
            k: {
                "ok": e.get("ok"),
                "rows": len(liq_rows(e)),
                "symbol": (e.get("data") or {}).get("symbol"),
                "exchanges": (e.get("data") or {}).get("exchanges"),
                "missing": e.get("missing"),
                "error": e.get("error"),
                "total_usd": ((e.get("data") or {}).get("totals") or {}).get("total"),
            }
            for k, e in envs.items()
        }
        res["liquidations"][base] = {"check": chk, "calls": meta}
        emit(f"\nliquidations {base}: " + ", ".join(f"{k} ok={m['ok']} rows={m['rows']} total={m['total_usd']}" for k, m in meta.items()))
        emit(line("both == binance + bybit (closed days)", chk) + f" only_total={chk['only_total']} only_parts={chk['only_parts']}")

        # the parameter is `base`: an unknown name (e.g. `symbol`) is silently ignored and the BTC default answers
        env = await mcp(
            "perps_funding_history", {"base": base, "source": "coinalyze", "days_back": a.liq_days, "interval": "daily"}, a.mcp_url
        )
        live = [r for r in ((env.get("data") or {}).get("rows") or []) if r["ts"] < today]
        stored = {r["ts"]: r for r in table[(f"{base}USDT_PERP.A", "funding")]}
        rr = {
            f: compare_pairs([(stored[r["ts"]][f], r[f]) for r in live if r["ts"] in stored], REREAD_ABS_TOL)
            for f in ("open", "high", "low", "close")
        }
        res["reread"][base] = rr
        for f, s in rr.items():
            emit(line(f"re-read .A funding {f} vs table", s))
    res["verdicts"] = verdict_table(res)
    emit("\nverdict table (marks derived from the stats by analytics.coinalyze_recon.verdict; the report copies these)")
    for r in res["verdicts"]:
        emit(
            f"  {r['series']:<38} {r['definition']:<22} n={r['n']:>5} match={r['n_match']:>5} share={r['share_str']:>8} "
            f"{r['period']} {r['mark']}"
        )
    say("done")
    return res


def verdict_table(res: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per reported line of the verdict table; the mark comes from :func:`verdict`, never typed by hand."""
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for sym in SYMBOLS:
        for field, exp in EXPECTED.items():
            rows.append((f"funding {sym} {field}", exp, res["funding"][sym][field][exp]))
        ls = res["long_short"][sym]["1d"]["same_ts"]
        rows.append((f"long_short {sym} long_share", "same_ts 1d", ls["share"]))
        rows.append((f"long_short {sym} ratio", "same_ts 1d", ls["ratio"]))
        if sym in res["oi"]:
            rows.append((f"oi_usd {sym} close", "OI at next 00:00 x close", res["oi"][sym]["oi_next_day"]))
    for base, liq in res["liquidations"].items():
        rows.append((f"liquidations {base} both = binance + bybit", "closed days", liq["check"]))
    for base, rr in res["reread"].items():
        for f, s in rr.items():
            rows.append((f"re-read .A funding {base} {f}", "service vs table", s))
    out = []
    for series, definition, s in rows:
        share = s.get("share")
        out.append(
            {
                "series": series,
                "definition": definition,
                "n": s.get("n") or 0,
                "n_match": s.get("n_match") or 0,
                "share": share,
                "share_str": "-" if share is None else f"{share:.5f}",
                "period": f"{day(s.get('first'))}..{day(s.get('last'))}",
                "mark": verdict(s),
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DB, help="SQLite database (opened read-only)")
    ap.add_argument("--cache-dir", default=str(CACHE))
    ap.add_argument("--out", default=str(CACHE / "result.json"), help="full result as JSON")
    ap.add_argument("--refresh", action="store_true", help="re-fetch the venue history instead of the cache")
    ap.add_argument("--liq-days", type=int, default=30)
    ap.add_argument("--mcp-url", default=mcp_call.URL)
    ap.add_argument("--brief", action="store_true", help="console: headers and best lines only (the .txt keeps all)")
    a = ap.parse_args()
    BRIEF["on"] = a.brief
    a.cache_dir = Path(a.cache_dir).expanduser()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    res = asyncio.run(main_async(a))
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1, default=str, ensure_ascii=False))
    emit(f"full result: {out} (printed lines: {out.with_suffix('.txt')})")
    out.with_suffix(".txt").write_text("\n".join(LINES) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
