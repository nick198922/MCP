"""Scripts that open the store through scripts/common_helpers.py (item 34 of the standing-improvements card): each
reads a path with a space and '?' by its real name, read-only, and creates nothing next to it."""

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bybit_import as bi  # noqa: E402
import cycle_regime_eval  # noqa: E402
import dashboard  # noqa: E402
import verify_demo_execution as vde  # noqa: E402
import verify_free_supply_history as vfs  # noqa: E402

# unquoted, 'file:<this>?mode=ro' would open "a b" with mode=rwc instead (and could create it)
ODD = "a b?mode=rwc#x.sqlite"


def _make(path: Path, *sql: str) -> Path:
    con = sqlite3.connect(path)
    for s in sql:
        con.execute(s)
    con.commit()
    con.close()
    return path


def _only(path: Path) -> None:
    assert sorted(x.name for x in path.parent.iterdir()) == [path.name]


def test_dashboard_table_freshness_odd_path(tmp_path):
    p = _make(tmp_path / ODD, "CREATE TABLE funding_hist (ts INTEGER)", "INSERT INTO funding_hist VALUES (1000)")
    rows = {r["table"]: r for r in dashboard.table_freshness(p, 1600.0)}
    assert rows["funding_hist"]["age_s"] == 600 and rows["funding_hist"]["rows"] == 1
    _only(p)


def test_bybit_stats_and_journal_symbols_odd_path(tmp_path, monkeypatch):
    p = tmp_path / ODD
    trade = {"_category": "linear", "symbol": "XYZUSDT", "orderId": "o1", "closedSize": "1", "avgEntryPrice": "10",
             "avgExitPrice": "11", "closedPnl": "1", "openFee": "0", "closeFee": "0", "leverage": "2",
             "updatedTime": "1760000000000"}  # fmt: skip
    loss = dict(trade, orderId="o2", avgExitPrice="9", closedPnl="-1", updatedTime="1760100000000")
    assert asyncio.run(bi.store([bi.to_trade(trade), bi.to_trade(loss)], str(p))) == (2, 0)
    assert "Закритих позицій: **2**" in bi.stats(str(p))

    seen = {}

    def fake_fetch(env, syms, start, end):
        seen["syms"] = syms
        return [], []

    async def fake_store(trades, db):
        return 0, 0

    monkeypatch.setattr(bi, "check", lambda env: 0)
    monkeypatch.setattr(bi, "fetch_classic", fake_fetch)
    monkeypatch.setattr(bi, "store", fake_store)
    assert bi.main(["--db", str(p), "import-classic", "--symbols", "journal"]) == 0
    assert "XYZUSDT" in seen["syms"] and "BTCUSDT" in seen["syms"]
    # the Store keeps the journal in WAL mode, so a reader may leave its -wal/-shm; no file under a cut-short name
    assert {x.name for x in tmp_path.iterdir()} <= {p.name, p.name + "-wal", p.name + "-shm"}


def test_verify_free_supply_history_snapshots_odd_path(tmp_path, monkeypatch):
    p = _make(
        tmp_path / ODD,
        "CREATE TABLE supply_snapshot (day TEXT, coin_id TEXT, symbol TEXT, price REAL, market_cap REAL, "
        "circulating_supply REAL, total_supply REAL, max_supply REAL)",
        "INSERT INTO supply_snapshot VALUES ('2026-10-01', 'bitcoin', 'btc', 1, 2, 3, 4, 5)",
    )
    monkeypatch.setattr(vfs, "DB", str(p))
    got = vfs.our_snapshots()
    assert got["2026-10-01"]["BTC"][0]["coin_id"] == "bitcoin" and got["2026-10-01"]["BTC"][0]["max"] == 5
    _only(p)


def test_verify_demo_execution_loads_odd_path_read_only(tmp_path):
    p = _make(
        tmp_path / ODD,
        "CREATE TABLE demo_trade (id INTEGER PRIMARY KEY, signal_ts INTEGER, symbol TEXT)",
        "INSERT INTO demo_trade (signal_ts, symbol) VALUES (5, 'BTCUSDT')",
        "CREATE TABLE exec_audit (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, link_id TEXT, symbol TEXT, setup TEXT, payload TEXT)",
        "INSERT INTO exec_audit (ts, kind, link_id, symbol, setup, payload) VALUES (1, 'signal', 'L', 'BTCUSDT', 'S', '{\"a\": 1}')",
        "INSERT INTO exec_audit (ts, kind, link_id, symbol, setup, payload) VALUES (2, 'filled', 'L', 'BTCUSDT', 'S', '{}')",
    )
    trades, audit = vde.load(str(p))
    assert [t["symbol"] for t in trades] == ["BTCUSDT"] and [x["kind"] for x in audit] == ["filled"]
    assert vde.load_signals(str(p)) == [{"ts": 1, "link_id": "L", "setup": "S", "payload": {"a": 1}}]
    _only(p)


def test_cycle_regime_eval_load_uses_has_table(tmp_path):
    empty = _make(tmp_path / "e b?.sqlite", "CREATE VIEW cycle_regime_log AS SELECT 1 AS x")  # a view is not the table
    assert cycle_regime_eval.load(str(empty), "BTC") == []
    (tmp_path / "d").mkdir()
    p = _make(
        tmp_path / "d" / ODD,
        "CREATE TABLE cycle_regime_log (date TEXT, base TEXT, state TEXT, close REAL, version INTEGER)",
        "INSERT INTO cycle_regime_log VALUES ('2026-10-02', 'BTC', 'growth', 2.0, 1)",
        "INSERT INTO cycle_regime_log VALUES ('2026-10-01', 'BTC', 'top', 1.0, 1)",
        "INSERT INTO cycle_regime_log VALUES ('2026-10-01', 'ETH', 'top', 1.0, 1)",
    )
    assert [r["date"] for r in cycle_regime_eval.load(str(p), "BTC")] == ["2026-10-01", "2026-10-02"]
    _only(p)
