"""scripts/bybit_import.py: closed-P&L records to journal rows, duplicate skipping and the statistics report."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bybit_import as bi  # noqa: E402

LONG = {
    "_category": "linear",
    "symbol": "BTCUSDT",
    "orderId": "o1",
    "closedSize": "0.1",
    "avgEntryPrice": "60000",
    "avgExitPrice": "62000",
    "closedPnl": "196",
    "openFee": "2",
    "closeFee": "2",
    "leverage": "5",
    "updatedTime": "1760000000000",
}
SHORT = dict(LONG, orderId="o2", avgEntryPrice="62000", avgExitPrice="61000", closedPnl="96", updatedTime="1760100000000")
LOSS = dict(
    LONG,
    orderId="o3",
    symbol="ETHUSDT",
    closedSize="1",
    avgEntryPrice="3000",
    avgExitPrice="2900",
    closedPnl="-102",
    updatedTime="1760200000000",
)


def test_side_is_inferred_from_the_pnl_sign():
    a, b, c = bi.to_trade(LONG), bi.to_trade(SHORT), bi.to_trade(LOSS)
    assert (a["side"], b["side"], c["side"]) == ("long", "short", "long")
    assert a["pnl"] == 196 and a["fees"] == 4 and a["ts"] == 1760000000000
    assert bi.to_trade(dict(LONG, closedSize="0")) is None


def test_store_skips_duplicates_and_stats_report(tmp_path):
    db = str(tmp_path / "j.sqlite")
    trades = [bi.to_trade(x) for x in (LONG, SHORT, LOSS)]
    assert asyncio.run(bi.store(trades, db)) == (3, 0)
    assert asyncio.run(bi.store(trades, db)) == (0, 3)
    rep = bi.stats(db)
    assert "Закритих позицій: **3**" in rep and "+190.00 USD" in rep and "ETHUSDT" in rep and "short: 1 угод" in rep


def test_env_file_is_parsed_without_exposing_values(tmp_path):
    p = tmp_path / "b.env"
    p.write_text("# note\nBYBIT_API_KEY=abc\nBYBIT_API_SECRET=def\n")
    assert bi.read_env(str(p)) == ("abc", "def")


def test_hyperliquid_round_trips_and_flip(tmp_path):
    import hyperliquid_import as hl

    F = [
        {
            "coin": "ETH",
            "px": "3000",
            "sz": "1",
            "side": "B",
            "time": 1000,
            "startPosition": "0",
            "dir": "Open Long",
            "closedPnl": "0",
            "fee": "1",
            "oid": 1,
            "tid": 1,
        },
        {
            "coin": "ETH",
            "px": "3100",
            "sz": "1",
            "side": "A",
            "time": 2000,
            "startPosition": "1",
            "dir": "Close Long",
            "closedPnl": "100",
            "fee": "1",
            "oid": 2,
            "tid": 2,
        },
        {
            "coin": "SOL",
            "px": "100",
            "sz": "10",
            "side": "A",
            "time": 3000,
            "startPosition": "0",
            "dir": "Open Short",
            "closedPnl": "0",
            "fee": "0.5",
            "oid": 3,
            "tid": 3,
        },
        {
            "coin": "SOL",
            "px": "110",
            "sz": "20",
            "side": "B",
            "time": 4000,
            "startPosition": "-10",
            "dir": "Short > Long",
            "closedPnl": "-100",
            "fee": "1",
            "oid": 4,
            "tid": 4,
        },
        {
            "coin": "SOL",
            "px": "120",
            "sz": "10",
            "side": "A",
            "time": 5000,
            "startPosition": "10",
            "dir": "Close Long",
            "closedPnl": "100",
            "fee": "0.5",
            "oid": 5,
            "tid": 5,
        },
        {
            "coin": "PURR/USDC",
            "px": "1",
            "sz": "5",
            "side": "B",
            "time": 6000,
            "startPosition": "0",
            "dir": "Buy",
            "closedPnl": "0",
            "fee": "0",
            "oid": 6,
            "tid": 6,
        },
    ]
    t = hl.round_trips(F)
    assert [(x["symbol"], x["side"], x["entry"], x["exit"], round(x["pnl"], 2)) for x in t] == [
        ("ETH", "long", 3000, 3100, 98.0),
        ("SOL", "short", 100, 110, -101.5),
        ("SOL", "long", 110, 120, 99.5),
    ]
    db = str(tmp_path / "j.sqlite")
    assert asyncio.run(bi.store(t, db)) == (3, 0)
    rep = bi.stats(db, venue="hyperliquid")
    assert "Hyperliquid" in rep and "Закритих позицій: **3**" in rep
