"""Why ``demo_trade.pnl_usd`` disagrees with the price P&L, and what it does to demo R (item 475 of
docs/ideas/IDEA-20261102-standing-improvements.md; report docs/verification/2026-11-26-demo-pnl-mismatch.md).

    uv run python scripts/verify_demo_pnl_mismatch.py --cutoff 2026-11-26T16:00 --out ~/.cache/tradedesk-agents/demo-pnl-mismatch/out.json

Read-only: the live DB is opened with ``mode=ro`` (nothing is written), the venue is read only through public endpoints
of the runtime's exchanges (``rt.exchanges``: Bybit funding history and candles), no keys. Per checked closed trade
(``analytics/demo_pnl_check``) the gap ``pnl_usd − price P&L`` is split into the funding the position paid (public funding
events × qty × the candle price at each funding time, between the fill and the close estimated from 1m candles) and a
residual (``analytics/demo_pnl_funding``). Then mean R per demo setup and the S12 stop rule are recomputed with the
same functions as ``demo_status`` (``checkpoint.demo_block``) in the three variants fixed in the card item before any
number: journal R, price R for the flagged trades, flagged trades left out. Raw venue answers are cached one JSON file
per trade under ``--cache-dir``.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import resource
import sqlite3
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics import demo_pnl_funding as dpf
from tradedesk.analytics.checkpoint import DEMO, demo_block
from tradedesk.analytics.demo_pnl_check import _checked, pnl_mismatches
from tradedesk.config import Settings
from tradedesk.runtime import Runtime

DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
CACHE = Path.home() / ".cache" / "tradedesk-agents" / "demo-pnl-mismatch"
H = 3_600_000
M = 60_000
LAST_WINDOW_MS = 65 * M  # the close lies between the previous hourly cycle and closed_ts
FUND_PAGE = 200
BAR_PAGE = 1000
PACE_S = 0.5  # pause between trades (3+ public requests each)


def _iso(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M") if ms else "-"


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def load(db: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM demo_trade ORDER BY signal_ts")]
    finally:
        con.close()


def _data(got: Any) -> Any:
    return got.data if hasattr(got, "data") else got


async def fetch_trade(rt: Runtime, t: dict[str, Any]) -> dict[str, Any]:
    """Public funding events in [opened − 1h, closed], 1h bars over the hold and 1m bars of the last cycle."""
    sym, lo, hi = t["symbol"], int(t["opened_ts"]) - H, int(t["closed_ts"])
    funding: list[list[float]] = []
    top = hi
    for _ in range(50):
        got = _data(await rt.exchanges.funding_rate_history("bybit", sym, lo, FUND_PAGE, top)) or []
        rows = [[int(x["timestamp"]), float(x["fundingRate"])] for x in got if x.get("timestamp") and x.get("fundingRate") is not None]
        funding += rows
        if len(rows) < FUND_PAGE:
            break
        top = min(r[0] for r in rows) - 1
        if top <= lo:
            break
    funding = sorted({r[0]: r for r in funding}.values())
    h1: list[list[float]] = []
    cur = lo // H * H
    for _ in range(50):
        until = min(cur + BAR_PAGE * H - 1, hi)
        got = _data(await rt.exchanges.ohlcv("bybit", sym, "1h", BAR_PAGE, cur, until)) or []
        h1 += [list(map(float, x[:6])) for x in got]
        if not got or int(got[-1][0]) + H > until:
            break
        cur = int(got[-1][0]) + H
    m1 = _data(await rt.exchanges.ohlcv("bybit", sym, "1m", 80, (hi - LAST_WINDOW_MS) // M * M, hi)) or []
    return {"funding": funding, "h1": h1, "m1": [list(map(float, x[:6])) for x in m1]}


def _cached(f: Path) -> dict[str, Any] | None:
    return json.loads(f.read_text()) if f.exists() else None


def _store(f: Path, doc: dict[str, Any]) -> None:
    f.write_text(json.dumps(doc))


async def fetch_all(trades: list[dict[str, Any]], cache: Path, offline: bool) -> dict[str, dict[str, Any]]:
    """Per trade: the cached venue answers, else a fetch written to the cache as it arrives (``cache`` must exist)."""
    out: dict[str, dict[str, Any]] = {}
    rt = None if offline else Runtime(dataclasses.replace(Settings.from_env(), db_path=""))
    t0 = time.monotonic()
    try:
        for i, t in enumerate(trades, 1):
            f = cache / f"{t['link_id']}.json"
            if (hit := _cached(f)) is not None:
                out[t["link_id"]] = hit
            elif rt is not None:
                try:
                    doc = await fetch_trade(rt, t)
                except Exception as e:  # noqa: BLE001 - one trade's failure is recorded, the run goes on
                    doc = {"error": f"{type(e).__name__}: {e}"[:300]}
                else:
                    _store(f, doc)
                out[t["link_id"]] = doc
                await asyncio.sleep(PACE_S)  # 26.11: 19 of 328 trades hit Bybit's 10006 without it
            if i % 25 == 0 or i == len(trades):
                print(f"  progress {i}/{len(trades)} {time.monotonic() - t0:.0f}s rss {_rss_mb():.0f} MB", flush=True)
    finally:
        if rt is not None:
            await rt.aclose()
    return out


def split(t: dict[str, Any], raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Funding split of one trade; the close is the first 1m bar of the last cycle holding the exit price."""
    paid = close_ms = None
    note = None
    n_drop = None
    if raw and "error" not in raw:
        opened = int(t["opened_ts"])
        exit_ts = t.get("exit_ts") if t.get("exit_reason") == "time" else None
        close_ms, end, note = dpf.close_window(float(t["exit"]), raw["m1"], raw["h1"], opened, int(t["closed_ts"]), LAST_WINDOW_MS, exit_ts)
        events, dropped = dpf.priced_events(raw["funding"], raw["h1"])
        paid = dpf.funding_paid(t["side"], float(t["qty"]), events, opened, end)
        n_ev = sum(1 for ts, _ in raw["funding"] if opened < ts < end)
        n_drop = sum(1 for ts in dropped if opened < ts < end)  # in the window but no candle price: left out of paid
    else:
        note = (raw or {}).get("error", "not fetched")
        n_ev = None
    p = dpf.decompose(t, paid)
    if p is None:
        return None
    return p | {
        "close_est": _iso(close_ms),
        "n_events": n_ev,
        "n_dropped": n_drop,
        "note": note,
        "exit_reason": t.get("exit_reason"),
        "version": t.get("version"),
    }


def variants(trades: list[dict[str, Any]], flagged: set[str], now_ms: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for v in dpf.VARIANTS:
        block = demo_block(dpf.variant_rows(trades, flagged, v), now_ms)
        rows = {r["key"]: r for r in block["rows"]}
        setups = {}
        for spec in DEMO:
            r = rows[f"demo:{spec.setup}"]
            setups[spec.setup] = {"n": r["n"], "mean_r": r["metric"], "verdict": r["verdict"]}
        s12 = block["s12_stop_rule"]
        out[v] = {
            "setups": setups,
            "s12": {
                "verdict": s12["verdict"],
                "checks": [{k: c[k] for k in ("name", "n", "mean", "ci", "verdict")} for c in s12["checks"]],
            },
        }
    return out


def funding_by_setup(trades: list[dict[str, Any]], parts: dict[str, dict[str, Any]], now_ms: int) -> dict[str, Any]:
    """Descriptive, not a variant: mean funding R on the demo_status selection (``r_multiple`` replaced by funding R)."""
    rows = []
    for t in trades:
        p = parts.get(t["link_id"])
        row = dict(t)
        row["r_multiple"] = p["funding_r"] if p else None
        rows.append(row)
    block = demo_block(rows, now_ms)
    by = {r["key"]: r for r in block["rows"]}
    return {s.setup: {"n": by[f"demo:{s.setup}"]["n"], "mean_funding_r": by[f"demo:{s.setup}"]["metric"]} for s in DEMO}


def _f(x: float | None, nd: int = 3) -> str:
    return "—" if x is None else f"{x:+.{nd}f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--cache-dir", default=str(CACHE))
    ap.add_argument("--cutoff", help="UTC ISO time; trades closed after it are left out (default: now)")
    ap.add_argument("--limit", type=int, help="smoke: fetch only the first N checked trades")
    ap.add_argument("--offline", action="store_true", help="use the cache only, no venue requests")
    ap.add_argument("--out", help="full JSON output")
    a = ap.parse_args(argv)
    now_ms = int((datetime.fromisoformat(a.cutoff).replace(tzinfo=UTC) if a.cutoff else datetime.now(UTC)).timestamp() * 1000)
    trades = [t for t in load(a.db) if not (t.get("status") == "closed" and (t.get("closed_ts") or 0) > now_ms)]
    checked = [t for t in trades if _checked(t) is not None and t.get("opened_ts") and t.get("closed_ts")]
    flagged_rows = pnl_mismatches(trades)
    flagged = {m["id"] for m in flagged_rows}
    print(f"cutoff {_iso(now_ms)} UTC; demo_trade rows {len(trades)}, checked closed {len(checked)}, flagged {len(flagged)}")
    todo = checked[: a.limit] if a.limit else checked
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    raw = asyncio.run(fetch_all(todo, cache, a.offline))
    parts = {t["link_id"]: p for t in todo if (p := split(t, raw.get(t["link_id"]))) is not None}
    if a.limit:  # smoke: fetch and split only, no outcome numbers
        ok = sum(1 for p in parts.values() if p["paid"] is not None)
        print(f"smoke: {len(parts)} trades split, {ok} with a funding number; rss {_rss_mb():.0f} MB")
        return 0

    print("\n1) flagged trades (USD; R of risk_usd)")
    print(
        f"{'trade':34} {'reason':9} {'pnl':>8} {'price':>8} {'gap':>8} {'fund.paid':>9} {'resid':>7} {'tol':>5} expl  close_est        ev"
    )
    for m in flagged_rows:
        p = parts[m["id"]]
        print(
            f"{m['id'][3:]:34} {p['exit_reason'] or '':9} {p['recorded']:8.2f} {p['price']:8.2f} {p['gap']:8.2f} "
            f"{_f(p['paid'], 2):>9} {_f(p['residual'], 2):>7} {p['tol']:5.2f} {str(p['explained']):5} {p['close_est']}  {p['n_events']}"
        )
    unfl = [p for k, p in parts.items() if k not in flagged]
    with_paid = [p for p in unfl if p["paid"] is not None]
    unexpl = sorted([p for p in with_paid if not p["explained"]], key=lambda p: -abs(p["residual"]))
    no_num = [p for p in unfl if p["paid"] is None]
    print(
        f"\n2) unflagged checked trades: {len(unfl)}; with a funding number {len(with_paid)}; residual beyond tolerance {len(unexpl)}; no number {len(no_num)}"
    )
    if with_paid:
        gaps = [abs(p["gap_r"]) for p in with_paid if p["gap_r"] is not None]
        res = [abs(p["residual_r"]) for p in with_paid if p["residual_r"] is not None]
        print(
            f"   |gap| R median {statistics.median(gaps):.4f} max {max(gaps):.4f}; |residual| R median {statistics.median(res):.4f} max {max(res):.4f}"
        )
        big = [p for p in with_paid if abs(p["gap_r"] or 0) >= 0.05]
        print(f"   unflagged with |gap| >= 0.05R: {len(big)}; of them explained by funding: {sum(1 for p in big if p['explained'])}")
        for cut in (0.01, 0.02, 0.05):
            print(
                f"   |residual| > {cut}R: {sum(1 for p in with_paid if abs(p['residual_r'] or 0) > cut)}; |gap| > {cut}R: {sum(1 for p in with_paid if abs(p['gap_r'] or 0) > cut)}"
            )
        notes: dict[str, int] = {}
        for p in with_paid:
            notes[p["note"] or "close in the last cycle"] = notes.get(p["note"] or "close in the last cycle", 0) + 1
        print(f"   close estimate: {notes}")
    drop = [p for p in parts.values() if p["n_dropped"]]
    print(
        f"   funding events in a holding window with no candle price (left out of paid): "
        f"{sum(p['n_dropped'] for p in drop)} in {len(drop)} trades"
    )
    for p in unexpl[:12]:
        print(
            f"   unexplained {p['id'][3:]:34} {p['exit_reason'] or '':9} gap {p['gap']:+8.2f} paid {_f(p['paid'], 2)} resid {p['residual']:+7.2f} tol {p['tol']:.2f} ({p['residual_r']:+.3f}R) {p['note'] or ''}"
        )
    for p in no_num[:5]:
        print(f"   no funding number {p['id'][3:]} {p['note']}")

    var = variants(trades, flagged, now_ms)
    print("\n3) mean R per setup (demo_status selection), three variants fixed in advance")
    print(f"{'setup':6} " + " ".join(f"{v:>28}" for v in dpf.VARIANTS))
    for spec in DEMO:
        cells = [var[v]["setups"][spec.setup] for v in dpf.VARIANTS]
        print(f"{spec.setup:6} " + " ".join(f"{c['n']:>4} {_f(c['mean_r'], 4):>8} {c['verdict']:>14}" for c in cells))
    print("   S12 stop rule:")
    for v in dpf.VARIANTS:
        s = var[v]["s12"]
        checks = "; ".join(
            f"{c['name']}: n {c['n']} mean {_f(c['mean'], 4)} ci [{_f(c['ci'][0])}, {_f(c['ci'][1])}] -> {c['verdict']}"
            for c in s["checks"]
        )
        print(f"   {v:14} {s['verdict']} | {checks}")

    fb = funding_by_setup(trades, parts, now_ms)
    print("\n4) descriptive, not a variant: mean funding R per setup on the same selection (+ = received)")
    for s, x in fb.items():
        print(f"   {s:5} n {x['n']:>3} funding R {_f(x['mean_funding_r'], 4)}")
    print(f"\nrss {_rss_mb():.0f} MB")
    if a.out:
        out = Path(a.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {"cutoff_ms": now_ms, "flagged": flagged_rows, "parts": parts, "variants": var, "funding_by_setup": fb},
                indent=1,
                default=str,
            )
        )
        print(f"full output: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
