"""scripts/verify_carry_a_cost_breakdown.py on a synthetic read-only collector DB: books A and B, smoke and full run."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_carry_a_cost_breakdown as v  # noqa: E402

from tradedesk.analytics.carry_rules import leg_execution  # noqa: E402

H8 = 8 * 3_600_000
T0 = 1_762_000_000_000 // H8 * H8
SYM = "X/USDT:USDT"


def _book(hs: float) -> dict:
    return {"bids": [[100 * (1 - hs), 1e6]], "asks": [[100 * (1 + hs), 1e6]]}


def _side(strategy: str, venue: str, leg: str, event: str, side: str, fee: float, k: int) -> tuple:
    ex = leg_execution(_book(0.001), side, 2000.0, fee=fee, model_slippage=0.0005, cycle_ts=T0 + k * H8)
    tid = f"{strategy}:{SYM}:0"
    p = {"trade_id": tid, "event": event, "leg": leg, "venue": venue, "symbol": SYM, "perp_symbol": SYM, "model_price": 100.0, **ex}
    return (strategy, T0 + k * H8, "side", tid, json.dumps(p))


def make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cp_log (id INTEGER PRIMARY KEY, strategy TEXT, cycle_ts INTEGER, kind TEXT, trade_id TEXT, payload TEXT)")
    con.execute("CREATE TABLE cp_cycle (strategy TEXT, cycle_ts INTEGER, status TEXT, payload TEXT)")
    con.execute("CREATE TABLE cp_position (strategy TEXT, symbol TEXT)")
    con.execute("CREATE TABLE cp_funding (venue TEXT, symbol TEXT, ts INTEGER, rate REAL)")
    rows = [
        _side("A", "binance", "spot", "open", "buy", 0.001, 0),
        _side("A", "binanceusdm", "perp", "open", "sell", 0.0005, 0),
        _side("B", "binanceusdm", "short", "open", "sell", 0.0005, 0),
        _side("B", "bybit", "long", "open", "buy", 0.00055, 0),
        _side("B", "binanceusdm", "short", "close", "buy", 0.0005, 3),
        _side("B", "bybit", "long", "close", "sell", 0.00055, 3),
    ]
    con.executemany("INSERT INTO cp_log (strategy, cycle_ts, kind, trade_id, payload) VALUES (?, ?, ?, ?, ?)", rows)
    ev = json.dumps({"type": "funding_interval_change", "symbol": SYM, "rate": 0.01})
    con.execute("INSERT INTO cp_log (strategy, cycle_ts, kind, trade_id, payload) VALUES ('B', ?, 'event', NULL, ?)", (T0 + H8, ev))
    flag = {"symbol": SYM, "issue": "funding_payment_missing", "detail": "payment_gap", "gap_cycles": 1}
    prov = {"issue": "provider_missing", "field": "bybit.funding", "reason": "timeout"}
    for s in ("A", "B"):
        for k in range(4):
            flags = [flag] if (s, k) == ("B", 2) else [prov] if (s, k) == ("B", 1) else []
            p = {"on_time": True, "complete": not flags, "flags": flags}
            con.execute("INSERT INTO cp_cycle VALUES (?, ?, 'done', ?)", (s, T0 + k * H8, json.dumps(p)))
    con.executemany("INSERT INTO cp_position VALUES (?, ?)", [("A", SYM), ("B", SYM)])
    pays = [("binanceusdm", SYM, T0 + k * H8, 0.0001) for k in range(-9, 4)]
    pays += [("bybit", SYM, T0 + k * H8, 0.0001) for k in range(-9, 4) if k != 1]  # a 16 h hole before cycle 2 on bybit
    con.executemany("INSERT INTO cp_funding VALUES (?, ?, ?, ?)", pays)
    con.commit()
    con.close()


def test_smoke_b_prints_counts_only(tmp_path, capsys):
    db = tmp_path / "c.sqlite"
    make_db(db)
    assert v.main(["--db", str(db), "--strategy", "B", "--as-of-ms", str(T0 + 3 * H8), "--smoke", "--cache-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "side rows 4, with book 4, coins 1" in out
    assert "binanceusdm/short/close 1" in out and "bybit/long/open 1" in out and "trades with sides 1" in out
    assert "stored payments of flagged symbols by venue (<= as_of): binanceusdm 13, bybit 12" in out
    assert "ratio" not in out and "share" not in out
    assert not list(tmp_path.glob("breakdown_*.json"))


def test_full_b_sections_and_venue_attribution(tmp_path, capsys):
    db = tmp_path / "c.sqlite"
    make_db(db)
    assert v.main(["--db", str(db), "--strategy", "B", "--as-of-ms", str(T0 + 3 * H8), "--n-perm", "50", "--cache-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "== by venue ==" in out and "== by venue x event ==" in out and "== by leg x event ==" not in out
    assert "depth part of (b)" in out and "labels permuted within venue x event" in out
    assert "provider_missing [bybit.funding] 1" in out
    assert "suspect ['bybit']" in out
    saved = json.loads((tmp_path / f"breakdown_{T0 + 3 * H8}.json").read_text())
    assert saved["strategy"] == "B" and saved["depth_short"]["with_book"] == 4


def test_full_a_keeps_its_sections(tmp_path, capsys):
    db = tmp_path / "c.sqlite"
    make_db(db)
    assert v.main(["--db", str(db), "--as-of-ms", str(T0 + 3 * H8), "--n-perm", "50", "--cache-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "== by leg x event ==" in out and "== by venue ==" not in out and "depth part of (b)" not in out
    assert "labels permuted within leg x event" in out and "scheduled_payment_missing flags: was the payment stored later?" in out
    assert "strategy" not in json.loads((tmp_path / f"breakdown_{T0 + 3 * H8}.json").read_text())
