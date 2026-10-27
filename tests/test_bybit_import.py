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


def test_airdrop_trades_are_left_out_of_stats(tmp_path):
    import hyperliquid_import as hl

    db = str(tmp_path / "j.sqlite")
    old = dict(bi.to_trade(LONG), venue="hyperliquid", category="perp", ts=1700000000000, oid="a")
    new = dict(bi.to_trade(LOSS), venue="hyperliquid", category="perp", ts=1760200000000, oid="b")
    asyncio.run(bi.store([old, new], db))
    assert hl.mark_airdrop(db, "2025-04-18") == 1 and hl.mark_airdrop(db, "2025-04-18") == 0
    rep = bi.stats(db, venue="hyperliquid")
    assert "Закритих позицій: **1**" in rep and "ETHUSDT" in rep
    assert "Закритих позицій: **1**" in bi.stats(db, venue=None)


def test_option_trips_close_by_trade_and_at_expiry():
    F = [
        {
            "symbol": "BTC-30OCT26-86000-C-USDT",
            "side": "Buy",
            "execQty": "0.2",
            "execPrice": "1000",
            "execFee": "1",
            "execTime": "1000",
            "execType": "Trade",
        },
        {
            "symbol": "BTC-30OCT26-86000-C-USDT",
            "side": "Sell",
            "execQty": "0.2",
            "execPrice": "1500",
            "execFee": "1",
            "execTime": "2000",
            "execType": "Trade",
        },
        {
            "symbol": "ETH-2OCT26-2500-P-USDT",
            "side": "Sell",
            "execQty": "1",
            "execPrice": "40",
            "execFee": "0.5",
            "execTime": "3000",
            "execType": "Trade",
        },
    ]
    D = [{"symbol": "ETH-2OCT26-2500-P-USDT", "deliveryPrice": "2450", "fee": "0.2", "deliveryTime": "4000"}]
    t = {x["symbol"]: x for x in bi.option_trips(F, D)}
    a, b = t["BTC-30OCT26-86000-C-USDT"], t["ETH-2OCT26-2500-P-USDT"]
    assert a["side"] == "long" and round(a["pnl"], 2) == 98.0 and a["exit"] == 1500
    assert b["side"] == "short" and b["exit"] == 50 and round(b["pnl"], 2) == round(40 - 50 - 0.5 - 0.2, 2)


def test_binance_fills_to_round_trips():
    import binance_import as bn

    def row(t, sym, side, px, q, rp, fee="0.1 USDT", tid=1, ps="BOTH"):
        return {
            "Time(UTC)": t,
            "Symbol": sym,
            "Side": side,
            "Position Side": ps,
            "Price": str(px),
            "Quantity": str(q),
            "Realized Profit": str(rp),
            "Fee": fee,
            "Trade Id": str(tid),
            "Order Id": str(tid),
        }

    F = [
        row("2025-01-01 00:00:00", "ETHUSDT", "SELL", 3000, 1, 5, tid=1),  # closes a pre-history position: skipped
        row("2025-01-02 00:00:00", "ETHUSDT", "BUY", 3000, 1, 0, tid=2),
        row("2025-01-03 00:00:00", "ETHUSDT", "SELL", 3100, 1, 100, tid=3),
        row("2025-01-04 00:00:00", "SOLUSDT", "SELL", 100, 10, 0, tid=4),
        row("2025-01-05 00:00:00", "SOLUSDT", "BUY", 90, 10, 100, fee="0.01 BNB", tid=5),
    ]
    t, skipped = bn.trips_from_fills(F)
    got = sorted((x["symbol"], x["side"], x["entry"], x["exit"], round(x["pnl"], 2)) for x in t)
    assert skipped == 1 and got == [("ETHUSDT", "long", 3000, 3100, 99.8), ("SOLUSDT", "short", 100, 90, 99.9)]


def _fill(sym, side, qty, px, ts, fee="1"):
    return {"symbol": sym, "side": side, "execQty": qty, "execPrice": px, "execFee": fee, "execTime": str(ts), "execType": "Trade"}


C30 = "BTC-30OCT26-84000-C-USDT"
P13 = "BTC-13NOV26-78000-P-USDT"


def _rows(db):
    import sqlite3

    con = sqlite3.connect(db)
    out = {r[0]: r for r in con.execute("SELECT symbol, id, status, size, entry, pnl, tags, note, thesis FROM journal ORDER BY id")}
    con.close()
    return out


def test_open_option_becomes_an_open_row_and_closes_in_place(tmp_path):
    db = str(tmp_path / "j.sqlite")
    buys = [_fill(C30, "Buy", "0.25", "1069", 1791477003361), _fill(C30, "Buy", "0.25", "1000", 1791477003999)]
    trips = bi.option_trips(buys, [], include_open=True)
    assert len(trips) == 1 and trips[0]["open"] and trips[0]["size"] == 0.5 and trips[0]["fees"] == 2
    assert bi.option_trips(buys, []) == []  # the default stays «closed trips only»
    assert asyncio.run(bi.store_options(trips, db)) == (1, 0, 0)
    assert asyncio.run(bi.store_options(trips, db)) == (0, 0, 1)
    r = _rows(db)[C30]
    assert r[2] == "open" and f"bybit:option:{C30}:1791477003361" in r[7] and '"import"' in r[6]
    sold = buys + [_fill(C30, "Sell", "0.5", "1500", 1791500000000)]
    assert asyncio.run(bi.store_options(bi.option_trips(sold, [], include_open=True), db)) == (0, 1, 0)
    after = _rows(db)[C30]
    assert after[1] == r[1] and after[2] == "closed" and round(after[5], 2) == round(0.5 * 1500 - (0.25 * 1069 + 0.25 * 1000) - 3, 2)


def test_manual_rows_are_reused_not_duplicated(tmp_path):
    from tradedesk.store.db import Store

    db = str(tmp_path / "j.sqlite")

    async def manual():
        st = Store(db)
        await st.init()
        await st.journal_insert(
            opened_ts=1793006243, symbol=P13, side="long", size=0.1, entry=1590, thesis="моя теза", tags=["bybit", "option", "manual"]
        )
        await st.aclose()

    asyncio.run(manual())
    buy = [_fill(P13, "Buy", "0.1", "1590", 1793006243022, fee="1.6")]
    assert asyncio.run(bi.store_options(bi.option_trips(buy, [], include_open=True), db)) == (0, 0, 1)
    r = _rows(db)[P13]
    assert r[2] == "open" and r[8] == "моя теза" and '"manual"' in r[6] and f"bybit:option:{P13}:1793006243022" in r[7]
    D = [{"symbol": P13, "deliveryPrice": "76000", "fee": "0.3", "deliveryTime": "1794556800000"}]
    assert asyncio.run(bi.store_options(bi.option_trips(buy, D, include_open=True), db)) == (0, 1, 0)
    r = _rows(db)[P13]
    assert r[2] == "closed" and r[8] == "моя теза" and round(r[5], 2) == round(0.1 * 2000 - 159 - 1.6 - 0.3, 2)
    assert len(_rows(db)) == 1


def test_a_trade_the_owner_closed_by_hand_keeps_his_numbers(tmp_path):
    from tradedesk.store.db import Store

    db = str(tmp_path / "j.sqlite")

    async def manual():
        st = Store(db)
        await st.init()
        tid = await st.journal_insert(opened_ts=1791477003, symbol=C30, side="long", size=0.25, entry=1069, tags=["bybit", "option"])
        await st.journal_update_close(
            tid, closed_ts=1791500100, status="closed", exit_=1500, outcome="win", pnl=100.0, r_multiple=None, note="закрив сам"
        )
        await st.aclose()

    asyncio.run(manual())
    fills = [_fill(C30, "Buy", "0.25", "1069", 1791477003361), _fill(C30, "Sell", "0.25", "1500", 1791500000000)]
    assert asyncio.run(bi.store_options(bi.option_trips(fills, []), db)) == (0, 0, 1)
    r = _rows(db)[C30]
    assert r[5] == 100.0 and r[7].startswith(f"bybit:option:{C30}:") and r[7].endswith("закрив сам") and len(_rows(db)) == 1
