"""Item 383: carry B entries failing criterion (e) (|median leg-price gap| > 0.5 % over the holding), engine or market.

Reads the collector DB through a READ-ONLY connection (``mode=ro``) for the engine's records (positions, open / close
``side`` rows, ``cp_cycle.candidates``), finds the entries that fail (e) as ``checkpoint.carry_universe`` does, and checks
each against the venues' own 4h and 1m bars fetched through the runtime providers (``rt.exchanges.ohlcv``, public, no
keys; cached per file under ``--cache-dir``). Classification rule: ``tradedesk.analytics.carry_leg_gap`` (registered
in the card before the run). Nothing in ``execution/`` or the engine is touched.

    uv run python scripts/verify_carry_b_leg_gap.py [--db PATH] [--date YYYY-MM-DD] [--out FILE] [--no-fetch]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics import carry_leg_gap as g
from tradedesk.analytics.checkpoint import _asof, _carry_entries, _held_gap

DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
CACHE = Path.home() / ".cache/tradedesk-agents/carry_b_leg_gap"
VENUES = ("binanceusdm", "bybit")  # leg a, leg b (collector.carry_paper_engine.PERP_VENUE, BYBIT)
TF_MS = {"4h": g.BAR_MS, "1m": 60_000}
PAGE = 1000


def _t(ms: int | None) -> str:
    return "—" if ms is None else datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%m-%d %H:%M")


def _pc(x: float | None, nd: int = 2) -> str:
    return "—" if x is None else f"{x * 100:+.{nd}f} %"


def _vol(x: float | None) -> str:
    return "—" if x is None else f"{x:.0f}"


def _rss() -> str:
    return f"rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def load_b(db: str) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    pos = [dict(r) for r in con.execute("SELECT * FROM cp_position WHERE strategy='B' ORDER BY opened_ts, trade_id")]
    q = "SELECT cycle_ts, kind, trade_id, payload FROM cp_log WHERE strategy='B' AND kind='side' ORDER BY id"
    side = [{**dict(r), "payload": json.loads(r["payload"] or "{}")} for r in con.execute(q)]
    q = "SELECT strategy, cycle_ts, status, payload FROM cp_cycle WHERE strategy='B' ORDER BY cycle_ts"
    cyc = [{**dict(r), "payload": json.loads(r["payload"] or "{}")} for r in con.execute(q)]
    con.close()
    return {"positions": pos, "side": side, "cycles": cyc}


async def bars(rt: Any, cache: Path, venue: str, sym: str, tf: str, start: int, end: int, fetch: bool) -> dict[int, list[float | None]]:
    """Bars [open, high, low, close, volume] by open ms in [start, end), cached per (venue, sym, tf, start, end).
    An unknown volume stays None (only an explicit 0 is a zero-volume bar for R1)."""
    p = cache / f"{venue}_{sym.split('/')[0]}_{tf}_{start}_{end}.json"
    if p.exists():
        return {int(k): v for k, v in json.loads(p.read_text()).items()}
    if not fetch or rt is None:
        return {}
    out: dict[int, list[float | None]] = {}
    cur = start
    while cur < end:
        until = min(cur + PAGE * TF_MS[tf] - 1, end - 1)
        rows = (await rt.exchanges.ohlcv(venue, sym, tf, PAGE, cur, until)).data or []
        for r in rows:
            if start <= int(r[0]) < end and r[4] is not None:
                vol = r[5] if len(r) > 5 else None
                ohlc: list[float | None] = [float(x) if x is not None else 0.0 for x in r[1:5]]
                out[int(r[0])] = [*ohlc, float(vol) if vol is not None else None]
        cur = until + 1
        await asyncio.sleep(0.2)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out))
    return out


async def run(a: argparse.Namespace) -> dict[str, Any]:
    now_ms = int(datetime.strptime(a.date, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000) + 86_400_000 - 1
    d = load_b(a.db)
    pos = _asof(d["positions"], now_ms)
    side = [x for x in d["side"] if int(x["cycle_ts"]) <= now_ms]
    cyc = [c for c in d["cycles"] if int(c["cycle_ts"]) <= now_ms]
    entries = _carry_entries(pos, side, cyc)
    print(f"as of {_t(now_ms)}: B positions {len(pos)}, side rows {len(side)}, cycles {len(cyc)}, {_rss()}")
    meds = sorted(((m, e) for e in entries if (m := _held_gap("B", e["held"])) is not None), key=lambda x: -x[0])
    print(
        "widest |median gap| (top 5): "
        + ", ".join(f"{e['symbol'].split('/')[0]} {_t(e['opened_ts'])} {m * 100:.3f} % ({len(e['held'])} rows)" for m, e in meds[:5])
    )
    print(f"entries with the gap checked (>= 3 rows): {len(meds)} of {len(entries)}")
    fail = [e for m, e in meds if m > g.GAP]
    print(f"entries failing (e) gap: {len(fail)}: " + ", ".join(f"{e['symbol']} {_t(e['opened_ts'])}" for e in fail))
    closes = {}
    for x in side:
        if x["payload"].get("event") == "close":
            closes.setdefault(str(x["trade_id"]), []).append(x["payload"])
    status = {str(p["trade_id"]): p for p in pos}
    rt = None
    if not a.no_fetch:
        from tradedesk.server import build_server

        _, rt = build_server()
    cache = Path(a.cache_dir)
    out = []
    try:
        for e in fail:
            sym, tid = e["symbol"], e["trade_id"]
            q = status[tid]
            rows = [{"cycle_ts": ts, **c} for ts, c in zip(e["held_ts"], e["held"], strict=True)]
            s4 = g.cycle_bar_open(rows[0]["cycle_ts"]) - g.BAR_MS
            e4 = g.cycle_bar_open(rows[-1]["cycle_ts"]) + 2 * g.BAR_MS
            b4 = [await bars(rt, cache, v, sym, "4h", s4, e4, not a.no_fetch) for v in VENUES]
            end = int(q["closed_ts"]) if q.get("status") == "closed" and q.get("closed_ts") else rows[-1]["cycle_ts"]
            s1 = g.cycle_bar_open(rows[0]["cycle_ts"])  # from the bar behind the first row
            e1 = min(end, s1 + 14 * 86_400_000)
            m1 = [await bars(rt, cache, v, sym, "1m", s1, e1, not a.no_fetch) for v in VENUES]
            checks = g.row_checks(rows, b4[0], b4[1])
            minute = g.minute_gap({k: v[3] for k, v in m1[0].items()}, {k: v[3] for k, v in m1[1].items()}, s1, e1)
            verdict = g.classify(checks, minute)
            legs = {"open": e["legs"], "close": closes.get(tid, [])}
            snap = {}
            for ev, ls in legs.items():
                by = {x.get("venue"): x for x in ls}
                la, lb = by.get(VENUES[0]) or {}, by.get(VENUES[1]) or {}

                def ratio(k: str, la: dict[str, Any] = la, lb: dict[str, Any] = lb) -> float | None:
                    return la[k] / lb[k] - 1.0 if la.get(k) and lb.get(k) else None

                ta, tb = la.get("book_ts_ms"), lb.get("book_ts_ms")
                snap[ev] = {
                    "model_gap": ratio("model_price"), "mid_gap": ratio("mid"), "mark_gap": ratio("mark"), "index_gap": ratio("index"),
                    "book_skew_s": None if ta is None or tb is None else (int(ta) - int(tb)) / 1000,
                    "latency_s": [None if x.get("book_latency_ms") is None else x["book_latency_ms"] / 1000 for x in (la, lb)],
                    "spread_bps": [x.get("spread_bps") for x in (la, lb)], "sides": [x.get("side") for x in (la, lb)],
                    "model_vs_mid": [x["model_price"] / x["mid"] - 1.0 if x.get("model_price") and x.get("mid") else None for x in (la, lb)],
                }  # fmt: skip
            out.append(
                {"symbol": sym, "trade_id": tid, "opened": e["opened_ts"], "closed": q.get("closed_ts"), "status": q.get("status"),
                 "checks": checks, "minute": minute, "snap": snap, **verdict}
            )  # fmt: skip
            print(f"  fetched {sym} {_t(e['opened_ts'])}: 4h {len(b4[0])}/{len(b4[1])}, 1m {len(m1[0])}/{len(m1[1])}, {_rss()}")
    finally:
        if rt is not None:
            await rt.aclose()
    return {"as_of": now_ms, "n_fail": len(fail), "entries": out}


def show(res: dict[str, Any]) -> None:
    for i, e in enumerate(res["entries"], 1):
        print(f"== {i}. {e['symbol']} {e['trade_id']}: {_t(e['opened'])} -> {_t(e['closed'])} ({e['status']}) ==")
        print("  cycle        price_a(Bin)  close_a    vol_a        price_b(Byb)  close_b    vol_b        gap      gap_indep")
        for c in e["checks"]:
            print(
                f"  {_t(c['cycle_ts'])}  {c['price_a']!s:>12}  {c['close_a']!s:>9}  {_vol(c['vol_a']):>11}  "
                f"{c['price_b']!s:>12}  {c['close_b']!s:>9}  {_vol(c['vol_b']):>11}  {_pc(c['gap'])}  {_pc(c['gap_indep'])}"
            )
        m = e["minute"]
        print(
            f"  median gap: engine {_pc(e.get('median_engine'))}, independent 4h {_pc(e.get('median_indep'))}; "
            f"1m n {m['n']} median {_pc(m['median'])}, |gap|>0.5 % share {_pc(m['share_over'], 1)}, "
            f"bar-closing minutes n {m['n_closing']} median {_pc(m['median_closing'])}"
        )
        for ev, s in e["snap"].items():
            print(
                f"  {ev}: model {_pc(s['model_gap'])}, mid {_pc(s['mid_gap'])}, mark {_pc(s['mark_gap'])}, index {_pc(s['index_gap'])}; "
                f"book skew {s['book_skew_s']} s, latency {s['latency_s']} s, spread bps {s['spread_bps']}, "
                f"model/mid-1 {[_pc(x) for x in s['model_vs_mid']]}, sides {s['sides']}"
            )
        nb = e.get("no_bar") or {}
        print(f"  rows without a 4h bar: a {nb.get('a', 0)}, b {nb.get('b', 0)}" + (" (R2 unknown there)" if e.get("r2_unknown") else ""))
        print(f"  flags {e['flags']} -> {e['verdict']} {e['cls']}")
    n_unk = sum(1 for e in res["entries"] if e.get("r2_unknown"))
    print(f"entries with rows without a 4h bar (R2 unknown): {n_unk} of {len(res['entries'])}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--date", default=datetime.now(UTC).strftime("%Y-%m-%d"), help="as-of date (UTC, end of day)")
    ap.add_argument("--cache-dir", default=str(CACHE))
    ap.add_argument("--out", default=str(CACHE / "result.json"))
    ap.add_argument("--no-fetch", action="store_true", help="use cached bars only (no network)")
    a = ap.parse_args()
    res = asyncio.run(run(a))
    show(res)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print(f"written {a.out}; {_rss()}")


if __name__ == "__main__":
    main()
