"""Item 367: why carry A failed criterion (e) on MITO, frozen perp price (delisting) or ticker collision. Descriptive.

Reads the collector DB through a READ-ONLY connection (``mode=ro``: no migration, no write, no network, no keys) and
prints: (1) MITO's candidate rows of carry A (perp / spot / gap per cycle) next to the last perp bars, liquidations and
funding rows the collector stored; (2) the engine's marks and close record of the MITO position (the prices its P&L
uses); (3) every carry A / B position whose leg price in ``cp_cycle.candidates`` stayed unchanged for >= ``--run``
consecutive rows over the holding period. Nothing in ``execution/`` is touched.

    uv run python scripts/verify_carry_mito_frozen.py [--db PATH] [--run 6] [--out FILE]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import sqlite3
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
OUT = Path.home() / ".cache/tradedesk-agents/carry_mito_frozen/result.json"
LEGS = {"A": ("perp", "spot"), "B": ("price_a", "price_b")}


def _t(ms: int | None) -> str:
    return "—" if ms is None else datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%m-%d %H:%M")


def _cycles(con: sqlite3.Connection, strategy: str) -> list[tuple[int, dict[str, Any]]]:
    rows = con.execute("SELECT cycle_ts, payload FROM cp_cycle WHERE strategy=? AND status='done' ORDER BY cycle_ts", (strategy,))
    return [(int(ts), json.loads(p) if p else {}) for ts, p in rows]


def _cand(payload: dict[str, Any], sym: str) -> dict[str, Any] | None:
    return next((c for c in payload.get("candidates") or [] if isinstance(c, dict) and c.get("symbol") == sym), None)


def longest_run(values: list[float | None]) -> tuple[int, int]:
    """Longest run of equal non-None consecutive values: (length, start index); (0, -1) when empty."""
    best, start, run, s = 0, -1, 0, 0
    for i, v in enumerate(values):
        if v is not None and i > 0 and v == values[i - 1]:
            run += 1
        else:
            run, s = (1, i) if v is not None else (0, i)
        if run > best:
            best, start = run, s
    return best, start


def part1(con: sqlite3.Connection, cyc: list[tuple[int, dict[str, Any]]], sym: str) -> dict[str, Any]:
    print(f"== (1) {sym}: candidate rows of A (cycle, perp, spot, perp/spot-1) ==")
    rows = []
    for ts, p in cyc:
        c = _cand(p, sym)
        flags = [f.get("issue") for f in p.get("flags") or [] if isinstance(f, dict) and f.get("symbol") == sym]
        if c is None and not flags:
            continue
        perp, spot = (c or {}).get("perp"), (c or {}).get("spot")
        gap = perp / spot - 1 if perp and spot else None
        rows.append({"cycle_ts": ts, "perp": perp, "spot": spot, "gap": gap, "flags": flags})
        print(f"{_t(ts)}  perp {perp}  spot {spot}  gap {'—' if gap is None else f'{gap * 100:+.2f} %'}  {','.join(map(str, flags))}")
    base = sym.split("/")[0]
    print("-- ohlcv_hist (venue, symbol, tf): n, first, last bar open, last close, last volume --")
    hist = []
    for venue, s, tf, n, lo, hi in con.execute(
        "SELECT venue, symbol, timeframe, count(*), min(ts), max(ts) FROM ohlcv_hist WHERE symbol LIKE ? GROUP BY 1,2,3", (f"%{base}%",)
    ):
        last = con.execute(
            "SELECT ts, close, volume FROM ohlcv_hist WHERE venue=? AND symbol=? AND timeframe=? ORDER BY ts DESC LIMIT 1", (venue, s, tf)
        ).fetchone()
        hist.append({"venue": venue, "symbol": s, "tf": tf, "n": n, "first": lo, "last": hi, "close": last[1], "volume": last[2]})
        print(f"{venue} {s} {tf}: n {n}, {_t(lo)} … {_t(hi)}, close {last[1]}, vol {last[2]}")
    if not hist:
        print("(no ohlcv_hist rows for this base)")
    other: dict[str, Any] = {}
    # liquidations.ts is unix seconds, cp_funding.ts unix ms (store/schema.py)
    for table, scale, extra in (("liquidations", 1000, "price"), ("cp_funding", 1, "rate")):
        q = f"SELECT venue, max(ts) * {scale}, count(*), {extra} FROM {table} WHERE symbol LIKE ? GROUP BY venue"
        res = con.execute(q, (f"%{base}%",)).fetchall()
        other[table] = [{"venue": v, "last": mx, "n": n, extra: x} for v, mx, n, x in res]
        print(f"-- {table}: " + "; ".join(f"{v}: n {n}, last {_t(mx)}, {extra} at last {x}" for v, mx, n, x in res))
    return {"rows": rows, "ohlcv_hist": hist, "other": other}


async def _vision(cache: Path, base: str, days: list[str]) -> dict[str, list[list[float]]]:
    """Binance Vision daily 4h klines of the perp (USDT-M) and the spot pair, cached per file (None = not in archive)."""
    from tradedesk.core.errors import ProviderError
    from tradedesk.server import build_server

    out: dict[str, list[list[float]]] = {"um": [], "spot": []}
    rt = None
    try:
        for kind in out:
            for day in days:
                p = cache / f"{kind}_{base}USDT_4h_{day}.json"
                if not p.exists():
                    if rt is None:
                        _, rt = build_server()
                    bv = rt.binance_vision
                    go = bv.um_klines_daily if kind == "um" else bv.spot_klines_daily
                    try:
                        rows: list[list[float]] | None = [r[:6] for r in (await go(f"{base}USDT", "4h", day)).data]
                    except ProviderError as e:
                        if e.reason != "data_unavailable":
                            print(f"  {kind} {day}: {e.reason} (not cached, rerun)")
                            continue
                        rows = None
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps(rows))
                out[kind] += json.loads(p.read_text()) or []
    finally:
        if rt is not None:
            await rt.aclose()
    return out


def part1b(cache: Path, sym: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The venue's own 4h bars (Binance Vision) around the stop, next to the frozen candidate price."""
    base = sym.split("/")[0]
    days = [f"2026-10-{d:02d}" for d in range(21, 32)]
    bars = asyncio.run(_vision(cache, base, days))
    print(f"== (1b) Binance Vision daily 4h bars {base}USDT {days[0]} … {days[-1]} (open, close, volume) ==")
    spot = {int(r[0]): r for r in bars["spot"]}
    um = sorted(bars["um"], key=lambda r: int(r[0]))
    for r in um:  # 22.10 12:00 … 24.10 12:00 UTC
        if 1792713600000 - 12 * 3_600_000 <= int(r[0]) <= 1792828800000 + 4 * 3_600_000:
            s = spot.get(int(r[0]))
            print(f"{_t(r[0])}  perp O {r[1]} C {r[4]} vol {r[5]}  |  spot C {s[4] if s else '—'}")
    traded = [r for r in um if float(r[5]) > 0]
    last = traded[-1] if traded else None
    frozen = sorted({r.get("perp") for r in rows if r.get("perp") is not None and r["cycle_ts"] > 1792742400000})
    print(
        f"perp bars {len(um)}, with volume > 0 {len(traded)}; last bar with volume opens {_t(last[0]) if last else '—'} close "
        f"{last[4] if last else '—'}; bars after it {len(um) - um.index(last) - 1 if last else '—'}, their closes "
        f"{sorted({float(r[4]) for r in um[um.index(last) + 1 :]}) if last else '—'}; frozen candidate perp after 23.10 08:00 {frozen}"
    )
    gaps = [float(r[4]) / float(spot[int(r[0])][4]) - 1 for r in traded if int(r[0]) in spot]
    still = [float(spot[int(r[0])][4]) for r in um if float(r[5]) == 0 and int(r[0]) in spot]
    if gaps and still:
        print(
            f"traded bars perp/spot-1: n {len(gaps)}, median {statistics.median(gaps) * 100:+.3f} %, min {min(gaps) * 100:+.2f} %, "
            f"max {max(gaps) * 100:+.2f} %; spot in the {len(still)} frozen bars {min(still)} … {max(still)}"
        )
    return {"um": um, "spot": bars["spot"], "last_traded": last}


def part2(con: sqlite3.Connection, sym: str) -> list[dict[str, Any]]:
    print(f"== (2) {sym}: positions, marks and close of A ==")
    out = []
    for tid, status, o, c, reason, trade in con.execute(
        "SELECT trade_id, status, opened_ts, closed_ts, exit_reason, trade FROM cp_position WHERE strategy='A' AND symbol=?", (sym,)
    ):
        tr = json.loads(trade) if trade else {}
        print(f"{tid}: {status} {_t(o)} -> {_t(c)} ({reason})")
        print("  trade: " + json.dumps({k: v for k, v in tr.items() if not isinstance(v, (list, dict))}, default=str)[:900])
        logs = []
        for ts, kind, p in con.execute(
            "SELECT cycle_ts, kind, payload FROM cp_log WHERE trade_id=? AND kind IN ('mark','close','event') ORDER BY cycle_ts, id", (tid,)
        ):
            x = json.loads(p)
            logs.append({"cycle_ts": ts, "kind": kind, **x})
            keep = ("spot", "perp", "pnl_usd", "basis_usd", "funding_usd", "costs_usd", "type", "exit_signal")
            print(f"  {_t(ts)} {kind}: " + ", ".join(f"{k}={x[k]}" for k in keep if k in x))
        out.append({"trade_id": tid, "status": status, "opened_ts": o, "closed_ts": c, "reason": reason, "trade": tr, "logs": logs})
    return out


def part3(con: sqlite3.Connection, cycles: dict[str, list[tuple[int, dict[str, Any]]]], min_run: int) -> list[dict[str, Any]]:
    print(f"== (3) positions with a leg price unchanged >= {min_run} consecutive candidate rows over the holding period ==")
    hits, n_pos = [], 0
    for strategy, legs in LEGS.items():
        cyc = cycles[strategy]
        for tid, sym, o, c in con.execute(
            "SELECT trade_id, symbol, opened_ts, closed_ts FROM cp_position WHERE strategy=? ORDER BY opened_ts", (strategy,)
        ):
            n_pos += 1
            held = [(ts, _cand(p, sym)) for ts, p in cyc if ts >= int(o) - 8 * 3_600_000 and (c is None or ts <= int(c))]
            held = [(ts, r) for ts, r in held if r is not None]
            for leg in legs:
                vals = [r.get(leg) for _, r in held]
                run, i = longest_run(vals)
                if run >= min_run:
                    hits.append(
                        {
                            "strategy": strategy,
                            "trade_id": tid,
                            "leg": leg,
                            "run": run,
                            "from": held[i][0],
                            "to": held[i + run - 1][0],
                            "value": vals[i],
                        }
                    )
                    print(
                        f"{strategy} {sym} {leg}: {run} rows at {vals[i]} from {_t(held[i][0])} to {_t(held[i + run - 1][0])} ({len(held)} rows held)"
                    )
    print(f"positions checked {n_pos}, hits {len(hits)}")
    return hits


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--symbol", default="MITO/USDT:USDT")
    ap.add_argument("--run", type=int, default=6)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--no-fetch", action="store_true", help="skip the Binance Vision bars (the only network part)")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=10)
    try:
        cycles = {s: _cycles(con, s) for s in LEGS}
        print(f"done cycles: A {len(cycles['A'])}, B {len(cycles['B'])}")
        res = {"p1": part1(con, cycles["A"], a.symbol), "p2": part2(con, a.symbol), "p3": part3(con, cycles, a.run)}
    finally:
        con.close()
    out = Path(a.out)
    if not a.no_fetch:
        res["p1b"] = part1b(out.parent / "vision", a.symbol, res["p1"]["rows"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, default=str))
    print(f"written {out}; peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")


if __name__ == "__main__":
    main()
