"""scripts/verify_coinalyze_liq.py: Binance rows split by parser (qty_src), on a database with or without the column."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import verify_coinalyze_liq as script  # noqa: E402

COLS = "ts INTEGER, venue TEXT, symbol TEXT, side TEXT, qty REAL, price REAL, notional_usd REAL"


def _db(path: Path, with_col: bool) -> Path:
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE liquidations (id INTEGER PRIMARY KEY AUTOINCREMENT, {COLS}{', qty_src TEXT' if with_col else ''})")
    rows = [
        (100, "binanceusdm", "BTCUSDT", None),
        (200, "binanceusdm", "BTCUSDT", None),
        (300, "binanceusdm", "BTCUSDT", "z"),
        (400, "binanceusdm", "BTCUSDT", "q"),
        (500, "binanceusdm", "ETHUSDT", None),
        (300, "bybit", "BTCUSDT", None),  # other venue: not counted
        (900, "binanceusdm", "BTCUSDT", "z"),  # outside [start, end)
    ]
    for ts, venue, sym, src in rows:
        vals = (ts, venue, sym, "sell", 1.0, 1.0, 1.0)
        if with_col:
            con.execute(
                "INSERT INTO liquidations (ts, venue, symbol, side, qty, price, notional_usd, qty_src) VALUES (?,?,?,?,?,?,?,?)",
                (*vals, src),
            )
        else:
            con.execute("INSERT INTO liquidations (ts, venue, symbol, side, qty, price, notional_usd) VALUES (?,?,?,?,?,?,?)", vals)
    con.commit()
    con.close()
    return path


@pytest.mark.parametrize("with_col", [True, False])
def test_binance_parser_split(tmp_path, with_col):
    db = _db(tmp_path / "t.sqlite", with_col)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)  # the script only ever reads
    try:
        out = script.binance_parser_split(con, 0, 800)
    finally:
        con.close()
    assert out["has_qty_src"] is with_col
    btc, eth, sol = (out["by_base"][b] for b in ("BTC", "ETH", "SOL"))
    if with_col:
        assert btc == {"old": 2, "new": 2, "new_first_ts": 300, "new_last_ts": 400}
    else:
        assert btc == {"old": 4, "new": 0, "new_first_ts": None, "new_last_ts": None}
    assert eth["old"] == 1 and eth["new"] == 0
    assert sol == {"old": 0, "new": 0, "new_first_ts": None, "new_last_ts": None}
