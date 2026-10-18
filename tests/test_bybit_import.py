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
