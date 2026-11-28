"""Reconcile Coinalyze daily (and hourly) liquidations with our own WebSocket liquidation rows (card
IDEA-20261017-doubletop-to-ours, item 4c; report docs/verification/2026-11-28-coinalyze-liq-check.md).

    uv run python scripts/verify_coinalyze_liq.py --out ~/.cache/tradedesk-agents/coinalyze-liq/result.json

A data reconciliation, not a trading test: no returns, no P&L.

Sources
* Ours: the live database, opened READ-ONLY (sqlite URI ``mode=ro``): table ``liquidations`` (venue ``binanceusdm`` /
  ``bybit``, symbols BTCUSDT / ETHUSDT / SOLUSDT; side = taker side, ``sell`` = long liquidated) and the connection
  journal ``liquidation_stream_hour`` (from 2026-11-12 19:00 UTC).
* Coinalyze: ``liquidations_history`` of the RUNNING MCP service (it holds the key; this clone has none). The script
  talks to the service only: if it does not answer, the script stops with an error (no in-process fallback, which
  would have no key). Each answer is saved under ``--cache-dir/snapshots/`` with its fetch time.
* Venue daily closes (USD check only): ``rt.exchanges.ohlcv`` (ccxt ``binanceusdm`` / ``bybit``), cached.

Comparison rules, written 2026-11-28 BEFORE any Coinalyze number was compared with our rows (the constants live in
analytics/liq_recon.py and below):
1. Days: full UTC days from 2026-11-13 (first full journaled day) to the last settled day (the collector's
   ``settled_end``: a day counts from 02:00 UTC of the next day). A day qualifies for a venue when its 24 journal hours
   exist, >= 90 % of the day was connected and no hour was below 60 % (36 min). Hours (secondary test): connected
   >= 3540 s and ending before the last settled day's end.
2. Quantities first in coins (``in_usd=false``). Our coins per bucket are compared with coverage x Coinalyze
   (coverage = connected share of the bucket). Tolerance per bucket: max(eps, 10 % of the expectation) for days,
   max(eps, 5 %) for hours; eps = 0.005 BTC, 0.05 ETH, 0.5 SOL. Marks by ``liq_recon.stream_verdict``: ✓ pooled
   coverage-adjusted ratio in [0.95, 1.05] and >= 80 % of informative days (>= 90 % of hours) agree; Bybit (complete
   stream): ≈ ratio in [0.80, 1.25] and correlation >= 0.90, else ✗; Binance (our rows are a LOWER BOUND, one push
   per symbol per second): ≈ ours above the band on <= 10 % of buckets and correlation >= 0.80, else ✗; ◯ below
   10 informative days / 30 hours. BTC and ETH are the test; SOL is descriptive.
3. Side mapping: per bucket where Coinalyze long and short differ by more than eps, is the direct (sell -> long,
   buy -> short) or the crossed assignment closer to Coinalyze? A swapped side shows as "swapped closer" > "direct".
4. USD separately: Coinalyze USD / coins per day (the conversion price) against the venue's daily close (rel 0.3 %)
   and against the Binance USD-M close; Coinalyze hourly coins summed to days against its daily coins (rel 0.1 %
   + eps); our own notional (qty x fill / bankruptcy price) against Coinalyze USD: descriptive only.
5. Revision of closed days: every run saves its answers; a later run compares each bucket already closed at the
   earliest snapshot older than 30 min with that snapshot, exact to 1e-9 relative; "young" = buckets closed less than
   2 h before that snapshot (not yet stored by the collector). (Edited before any revision was compared: the first
   draft took only buckets settled at the old fetch, which leaves out the young bars.) The table ``coinalyze_history`` metric
   ``liq_usd`` is reported (rows, range, fetched_ts) but cannot be re-read through the tool beyond 365 days.

Added 2026-11-28 AFTER the first run (exploratory, no mark depends on them): the correlation of our long share with
Coinalyze's (``long_share_corr``: the side test stays informative for the Binance lower bound, where both
assignments tie when ours is below both Coinalyze sides), and our Bybit rows throttled to the last row of each second
(``throttle_per_second``) against Coinalyze Bybit, i.e. what a Binance-like one-push-per-second stream would keep.

Peak RSS is printed in the progress lines; the full result goes to ``--out`` (JSON) and its printed lines next to it
(``.txt``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import resource
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_call  # noqa: E402  (scripts/mcp_call.py: the running MCP service)

from tradedesk.analytics.coinalyze_recon import compare_series  # noqa: E402
from tradedesk.analytics.liq_recon import (  # noqa: E402
    DAILY_BAND,
    DAILY_MIN_N,
    DAILY_SHARE_FULL,
    DAY,
    EPS_COINS,
    HOUR,
    HOURLY_BAND,
    HOURLY_MIN_N,
    HOURLY_SHARE_FULL,
    SIDES,
    bucket_events,
    compare_side,
    hour_coverage,
    implied_prices,
    long_share_corr,
    qualifying_days,
    qualifying_hours,
    side_test,
    stream_verdict,
    sum_hours_by_day,
    throttle_per_second,
)
from tradedesk.collector.coinalyze_history import settled_end  # noqa: E402

DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
CACHE = Path.home() / ".cache" / "tradedesk-agents" / "coinalyze-liq"
FIRST_DAY = 1_794_528_000  # 2026-11-13 00:00 UTC, first full journaled day (journal starts 2026-11-12 19:00 UTC)
VENUES = {"binance": ("binanceusdm", True), "bybit": ("bybit", False)}  # journal name -> (liquidations.venue, lower bound)
BASES = ("BTC", "ETH", "SOL")
TESTED = ("BTC", "ETH")
PRICE_REL_TOL = 0.003
HOURSUM_REL_TOL = 1e-3
REVISION_REL_TOL = 1e-9
REVISION_MIN_AGE_S = 1800  # an earlier snapshot must be at least this old to count as a re-read

LINES: list[str] = []


def emit(s: str) -> None:
    LINES.append(s)
    print(s, flush=True)


def say(msg: str) -> None:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    emit(f"[{time.strftime('%H:%M:%S')} rss {rss:.0f} MB] {msg}")


def day(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts is not None else "-"


def fmt(x: Any, p: str = ".3f") -> str:
    return "-" if x is None else format(x, p)


# ---- ours ----------------------------------------------------------------------------------------------------------
def read_ours(db: str, start: int, end: int) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out: dict[str, Any] = {"hours": {}, "events": {}, "dups": {}, "table": []}
    try:
        for ex in VENUES:
            rows = con.execute("SELECT hour_ts, connected_s FROM liquidation_stream_hour WHERE exchange = ?", (ex,)).fetchall()
            out["hours"][ex] = hour_coverage(rows)
        for ex, (venue, _) in VENUES.items():
            for base in BASES:
                sym = f"{base}USDT"
                q = "SELECT ts, side, qty, notional_usd FROM liquidations WHERE venue = ? AND symbol = ? AND ts >= ? AND ts < ? ORDER BY id"
                out["events"][(ex, base)] = con.execute(q, (venue, sym, start, end)).fetchall()
                out["dups"][(ex, base)] = con.execute(
                    "SELECT COUNT(*) - COUNT(DISTINCT ts || '|' || side || '|' || qty || '|' || price) FROM liquidations "
                    "WHERE venue = ? AND symbol = ? AND ts >= ? AND ts < ?",
                    (venue, sym, start, end),
                ).fetchone()[0]
        out["table"] = con.execute(
            "SELECT symbol, COUNT(*), MIN(ts), MAX(ts), MIN(fetched_ts), MAX(fetched_ts) FROM coinalyze_history "
            "WHERE metric = 'liq_usd' GROUP BY symbol"
        ).fetchall()
    finally:
        con.close()
    return out


# ---- the MCP service -----------------------------------------------------------------------------------------------
async def service(tool: str, args: dict[str, Any], url: str) -> dict[str, Any]:
    """One call to the running service. No in-process fallback: that server has no Coinalyze key."""
    try:
        code, text = await mcp_call._over(url, tool, args, False)  # noqa: SLF001 - same client code as mcp_call.py
    except mcp_call._NoAnswer as e:  # noqa: SLF001
        raise SystemExit(f"the MCP service at {url} did not answer ({e}): it holds the Coinalyze key; retry in a few minutes") from e
    if code != 0:
        raise SystemExit(f"{tool} {args}: exit {code}: {text[:300]}")
    env = json.loads(text)
    if not env.get("ok"):
        raise SystemExit(f"{tool} {args}: ok=false, missing={env.get('missing')}")
    return env


def snapshot_key(base: str, ex: str, interval: str, usd: bool) -> str:
    return f"{base}_{ex}_{interval}_{'usd' if usd else 'coins'}"


async def fetch_coinalyze(a: argparse.Namespace, snap_dir: Path, now: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    for base in BASES:
        for ex in VENUES:
            for interval, days, usd in (("daily", a.days, False), ("daily", a.days, True), ("hourly", a.hourly_days, False)):
                args = {"symbol": base, "days": days, "exchanges": [ex], "interval": interval, "in_usd": usd}
                env = await service("liquidations_history", args, a.mcp_url)
                key = snapshot_key(base, ex, interval, usd)
                (snap_dir / f"{key}_{stamp}.json").write_text(json.dumps({"fetched": now, "args": args, "envelope": env}))
                rows = env["data"]["rows"]
                if (env["data"].get("unit") or "") != ("USD" if usd else base):
                    raise SystemExit(f"{key}: unexpected unit {env['data'].get('unit')}")
                out[key] = {int(r["ts"]): {"long": float(r["long"]), "short": float(r["short"])} for r in rows}
            say(f"coinalyze {base} {ex}: daily {len(out[snapshot_key(base, ex, 'daily', False)])} rows, hourly "
                f"{len(out[snapshot_key(base, ex, 'hourly', False)])} rows")  # fmt: skip
    return out


def revisions(snap_dir: Path, current: dict[str, Any], now: int) -> dict[str, Any]:
    """Each key: compare the current rows with the earliest snapshot older than :data:`REVISION_MIN_AGE_S`, on the
    buckets already closed at that fetch; ``young`` = those closed less than 2 h (the collector's settle delay) before
    it, i.e. the bars the collector would NOT have stored yet. (Changed 2026-11-28 before any revision was compared:
    the first draft only took buckets settled at the old fetch, which drops exactly the young bars the question is
    about.)"""
    res: dict[str, Any] = {}
    for key, cur in current.items():
        files = sorted(snap_dir.glob(f"{key}_*.json"))
        older = [p for p in files if json.loads(p.read_text())["fetched"] < now - REVISION_MIN_AGE_S]
        if not older:
            res[key] = {"n": 0, "note": f"no snapshot older than {REVISION_MIN_AGE_S} s"}
            continue
        old = json.loads(older[0].read_text())
        step = HOUR if "_hourly_" in key else DAY
        f0 = int(old["fetched"])
        prev = {int(r["ts"]): r for r in old["envelope"]["data"]["rows"] if int(r["ts"]) + step <= f0}
        out: dict[str, Any] = {"old_fetched": f0, "age_h": (now - f0) / 3600}
        for subset, keep in (("all", lambda t: True), ("young", lambda t: f0 - (t + step) < 7200)):  # noqa: B023
            p = {t: r for t, r in prev.items() if keep(t)}
            out[subset] = {
                side: compare_series(
                    {t: v[side] for t, v in cur.items()}, {t: float(r[side]) for t, r in p.items()}, 0, 0.0, REVISION_REL_TOL
                )
                for side in SIDES
            }
        res[key] = out
    return res


# ---- venue closes --------------------------------------------------------------------------------------------------
async def closes(cache: Path, start: int, end: int, refresh: bool) -> dict[str, dict[int, float]]:
    from tradedesk.server import build_server  # noqa: PLC0415

    logging.getLogger("httpx").setLevel(logging.WARNING)
    _, rt = build_server()
    out: dict[str, dict[int, float]] = {}
    try:
        for base in BASES:
            for ccxt_id in ("binanceusdm", "bybit"):
                p = cache / f"ohlcv_{ccxt_id}_{base}_{day(start)}_{day(end)}.json"
                if p.exists() and not refresh:
                    bars = json.loads(p.read_text())
                else:
                    f = await rt.exchanges.ohlcv(ccxt_id, f"{base}/USDT:USDT", "1d", 60, since_ms=start * 1000, until_ms=end * 1000)
                    bars = f.data
                    p.write_text(json.dumps(bars))
                out[f"{ccxt_id}:{base}"] = {int(b[0]) // 1000 // DAY * DAY: float(b[4]) for b in bars}
    finally:
        await rt.aclose()
    return out


def binance_raw_check(path: Path, cz: dict[str, Any]) -> dict[str, Any]:
    """Exploratory (added 2026-11-28 after the first run): a raw capture of the Binance stream keeps both ``l`` (last
    fill qty, what our parser stores) and ``z`` (accumulated filled qty). On the whole UTC hours inside the capture,
    compare both with Coinalyze ``.A`` hourly coins (coverage 1; the capture has the same one-push-per-second limit)."""
    raw = json.loads(path.read_text())
    if not raw:
        return {"rows": 0}
    ts = [int(r["E"]) // 1000 for r in raw]
    lo, hi = (min(ts) // HOUR + 1) * HOUR, max(ts) // HOUR * HOUR
    hours = list(range(lo, hi, HOUR))
    out: dict[str, Any] = {"rows": len(raw), "hours": [time.strftime("%m-%d %H:00", time.gmtime(h)) for h in hours], "by_base": {}}
    side = {"SELL": "sell", "BUY": "buy"}
    for base in BASES:
        mine = [r for r in raw if r["s"] == f"{base}USDT" and r["S"] in side]
        ref = cz[snapshot_key(base, "binance", "hourly", False)]
        res: dict[str, Any] = {"rows": len(mine)}
        for field in ("z", "l"):
            ev = [(int(r["T"]) // 1000, side[r["S"]], float(r[field]), 0.0) for r in mine]
            b = bucket_events(ev, HOUR)
            for sd in SIDES:
                ours = sum(b.get(h, {}).get(sd, 0.0) for h in hours)
                theirs = sum(ref[h][sd] for h in hours if h in ref)
                res[f"{field}:{sd}"] = {"ours": ours, "cz": theirs, "ratio": ours / theirs if theirs else None}
        out["by_base"][base] = res
        emit(
            f"  binance raw {base}: rows {len(mine)} in hours {out['hours']}; "
            + ", ".join(f"{k} {v['ours']:.3f}/{v['cz']:.3f}={fmt(v['ratio'])}" for k, v in res.items() if k != "rows")
        )
    return out


# ---- main ----------------------------------------------------------------------------------------------------------
def stat_line(label: str, s: dict[str, Any], mark: str) -> str:
    return (
        f"  {label:<26} n={s['n']:>3} inf={s['informative']:>3} agree={fmt(s['share'], '.2f'):>5} above={fmt(s['above_share'], '.2f'):>5} "
        f"ours={s['sum_ours']:>11.2f} cz={s['sum_ref']:>11.2f} ratio={fmt(s['ratio']):>6} ratio_cov={fmt(s['ratio_cov']):>6} "
        f"med={fmt(s['median_ratio']):>6} corr={fmt(s['corr']):>6} {day(s['first'])}..{day(s['last'])} {mark}"
    )


async def main_async(a: argparse.Namespace) -> dict[str, Any]:
    now = int(time.time())
    end = settled_end(now)  # exclusive end of the last settled day
    res: dict[str, Any] = {"now": now, "first_day": day(FIRST_DAY), "last_settled_day": day(end - DAY), "compare": {}, "verdicts": []}
    say(f"window {day(FIRST_DAY)}..{day(end - DAY)} (settled end {end}); db {a.db} read-only")
    ours = read_ours(a.db, FIRST_DAY - DAY, end)
    for ex in VENUES:
        qd = qualifying_days(ours["hours"][ex], FIRST_DAY, end - DAY)
        qh = qualifying_hours(ours["hours"][ex], end)
        res.setdefault("qualifying", {})[ex] = {"days": {day(d): round(c, 4) for d, c in qd.items()}, "hours": len(qh)}
        emit(f"  {ex}: qualifying days {len(qd)} ({', '.join(day(d)[5:] for d in qd)}); fully connected hours {len(qh)}")
    for (ex, base), ev in ours["events"].items():
        emit(f"  ours {ex} {base}: {len(ev)} rows, exact duplicate rows {ours['dups'][(ex, base)]}")
    res["table_liq_usd"] = [
        {"symbol": s, "rows": n, "first": day(lo), "last": day(hi), "fetched_min": fmin, "fetched_max": fmax}
        for s, n, lo, hi, fmin, fmax in ours["table"]
    ]
    emit(f"  coinalyze_history liq_usd: {res['table_liq_usd']}")

    snap_dir = a.cache_dir / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    say("coinalyze through the MCP service")
    cz = await fetch_coinalyze(a, snap_dir, now)
    say("venue daily closes")
    px = await closes(a.cache_dir, FIRST_DAY - 2 * DAY, end + DAY, a.refresh)

    say("comparisons (coins)")
    for ex, (_, lower) in VENUES.items():
        qd = qualifying_days(ours["hours"][ex], FIRST_DAY, end - DAY)
        qh = qualifying_hours(ours["hours"][ex], end)
        for base in BASES:
            eps = EPS_COINS[base]
            ev = ours["events"][(ex, base)]
            od, oh = bucket_events(ev, DAY, until=end), bucket_events(ev, HOUR, until=end)
            czd, czh = cz[snapshot_key(base, ex, "daily", False)], cz[snapshot_key(base, ex, "hourly", False)]
            block: dict[str, Any] = {}
            for side in SIDES:
                for label, o, r, cov, band, min_n, sfull in (
                    ("daily", od, czd, qd, DAILY_BAND, DAILY_MIN_N, DAILY_SHARE_FULL),
                    ("hourly", oh, czh, qh, HOURLY_BAND, HOURLY_MIN_N, HOURLY_SHARE_FULL),
                ):
                    s = compare_side({k: v[side] for k, v in o.items()}, {k: v[side] for k, v in r.items()}, cov, eps, band)
                    mark = stream_verdict(s, lower_bound=lower, min_n=min_n, share_full=sfull)
                    block[f"{label}:{side}"] = {**s, "mark": mark}
                    emit(stat_line(f"{ex} {base} {label} {side}", s, mark))
                    res["verdicts"].append(
                        {
                            "venue": ex,
                            "base": base,
                            "bucket": label,
                            "side": side,
                            "tested": base in TESTED,
                            "mark": mark,
                            "n": s["informative"],
                            "ratio_cov": s["ratio_cov"],
                            "share": s["share"],
                            "corr": s["corr"],
                            "period": f"{day(s['first'])}..{day(s['last'])}",
                        }  # fmt: skip
                    )
            block["side_daily"] = side_test(od, czd, qd, eps)
            block["side_hourly"] = side_test(oh, czh, qh, eps)
            emit(f"  {ex} {base} side test: daily {block['side_daily']}, hourly {block['side_hourly']}")
            # exploratory (added after the first run): long-share correlation; Bybit throttled to 1 row per second
            block["long_share_corr"] = {"daily": long_share_corr(od, czd, qd, eps), "hourly": long_share_corr(oh, czh, qh, eps)}
            emit(
                f"  {ex} {base} [exploratory] long-share corr ours vs cz: "
                + ", ".join(f"{k} n={v['n']} r={fmt(v['corr'])}" for k, v in block["long_share_corr"].items())
            )
            if ex == "bybit":
                td = bucket_events(throttle_per_second(ev), DAY, until=end)
                block["throttled"] = {}
                for side in SIDES:
                    s = compare_side({k: v[side] for k, v in td.items()}, {k: v[side] for k, v in czd.items()}, qd, eps, DAILY_BAND)
                    block["throttled"][side] = s
                kept = len(throttle_per_second(ev)) / len(ev) if ev else None
                emit(
                    f"  bybit {base} [exploratory] throttled to 1 row/s: rows kept {fmt(kept)}; coins vs cov x cz daily "
                    + ", ".join(
                        f"{sd} ratio_cov={fmt(s['ratio_cov'])} med={fmt(s['median_ratio'])}" for sd, s in block["throttled"].items()
                    )
                )
            # Coinalyze internal: hourly coins summed to days vs daily coins
            hs = {sd: sum_hours_by_day({k: v[sd] for k, v in czh.items()}, czd) for sd in SIDES}
            block["hours_sum_vs_daily"] = {
                sd: compare_series(hs[sd], {k: v[sd] for k, v in czd.items()}, 0, eps, HOURSUM_REL_TOL) for sd in SIDES
            }
            emit(
                f"  {ex} {base} coinalyze hourly sum = daily: "
                + ", ".join(f"{sd} {v['n_match']}/{v['n']} scale={fmt(v['scale'], '.4f')}" for sd, v in block["hours_sum_vs_daily"].items())
            )
            # USD: conversion price and our notional
            czu = cz[snapshot_key(base, ex, "daily", True)]
            tot_usd = {k: v["long"] + v["short"] for k, v in czu.items()}
            tot_c = {k: v["long"] + v["short"] for k, v in czd.items()}
            imp = implied_prices(tot_usd, tot_c, eps)
            settled_imp = {k: v for k, v in imp.items() if k + DAY <= end}
            block["usd_price"] = {
                src: compare_series(settled_imp, px[f"{src}:{base}"], 0, 0.0, PRICE_REL_TOL) for src in ("binanceusdm", "bybit")
            }
            usd_long = sum(od.get(d, {}).get("long_usd", 0.0) for d in qd)
            usd_short = sum(od.get(d, {}).get("short_usd", 0.0) for d in qd)
            cz_long = sum(czu[d]["long"] * qd[d] for d in qd if d in czu)
            cz_short = sum(czu[d]["short"] * qd[d] for d in qd if d in czu)
            block["usd_ours_vs_cz"] = {
                "long": usd_long / cz_long if cz_long else None,
                "short": usd_short / cz_short if cz_short else None,
            }
            emit(
                f"  {ex} {base} USD: conversion price = close "
                + ", ".join(f"{src} {v['n_match']}/{v['n']} scale={fmt(v['scale'], '.4f')}" for src, v in block["usd_price"].items())
                + f"; our notional / (cov x cz USD) long={fmt(block['usd_ours_vs_cz']['long'])} short={fmt(block['usd_ours_vs_cz']['short'])}"
            )
            res["compare"][f"{ex}:{base}"] = block

    if a.binance_raw:
        say("[exploratory] raw Binance capture: accumulated qty z vs last-fill qty l against Coinalyze hourly")
        res["binance_raw"] = binance_raw_check(a.binance_raw, cz)

    say("revisions against earlier snapshots")
    res["revisions"] = revisions(snap_dir, cz, now)
    for key, r in res["revisions"].items():
        if r.get("n") == 0:
            continue
        emit(
            f"  {key}: snapshot {time.strftime('%m-%d %H:%M', time.gmtime(r['old_fetched']))} UTC, age {r['age_h']:.1f} h; "
            + "; ".join(
                f"{sub} " + ", ".join(f"{sd} {r[sub][sd]['n_match']}/{r[sub][sd]['n']}" for sd in SIDES) for sub in ("all", "young")
            )
        )
    if all(r.get("n") == 0 for r in res["revisions"].values()):
        emit(f"  no snapshot older than {REVISION_MIN_AGE_S} s yet: run again later (best: a day after, past 02:00 UTC)")
    say("done")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DB, help="SQLite database (opened read-only)")
    ap.add_argument("--cache-dir", default=str(CACHE))
    ap.add_argument("--out", default=str(CACHE / "result.json"), help="full result as JSON")
    ap.add_argument("--days", type=int, default=20, help="daily look-back of liquidations_history")
    ap.add_argument("--hourly-days", type=int, default=18, help="hourly look-back (the tool caps it at 30)")
    ap.add_argument("--refresh", action="store_true", help="re-fetch the venue closes")
    ap.add_argument("--binance-raw", default="", help="raw !forceOrder@arr capture (JSON list of o-fields + E), exploratory")
    ap.add_argument("--mcp-url", default=mcp_call.URL)
    a = ap.parse_args()
    a.cache_dir = Path(a.cache_dir).expanduser()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    a.binance_raw = Path(a.binance_raw).expanduser() if a.binance_raw else None
    logging.getLogger("httpx").setLevel(logging.WARNING)
    res = asyncio.run(main_async(a))
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1, default=str, ensure_ascii=False))
    emit(f"full result: {out} (printed lines: {out.with_suffix('.txt')})")
    out.with_suffix(".txt").write_text("\n".join(LINES) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
