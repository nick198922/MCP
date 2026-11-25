"""Descriptive calibration of the order-book wall collector (analytics/book_walls_calibration.py) on synthetic rows,
and the report script against a small read-only SQLite file with schema 41."""

from __future__ import annotations

import importlib.util
import math
import sqlite3
from pathlib import Path

import pytest

from tradedesk.analytics import book_walls_calibration as cal
from tradedesk.store.schema import MIGRATIONS

T0 = 1_795_519_260  # unix s, a minute boundary (2026-11-24 11:21 UTC)


def _ev(reason="withdrawn", *, venue="bybit", symbol="BTCUSDT", side="bid", price=100.0, open_s=0.0, life_s=10.0,
        size_max=30.0, threshold=25.0, dist_open=0.003, dist_min=0.002, dist_close=0.003, traded=0.0, gaps=0):  # fmt: skip
    o = int((T0 + open_s) * 1000)
    return {
        "venue": venue, "symbol": symbol, "side": side, "price": price, "open_ms": o, "close_ms": o + int(life_s * 1000),
        "size_open": size_max, "size_max": size_max, "size_end": 0.0, "threshold": threshold, "dist_open": dist_open,
        "dist_min": dist_min, "dist_close": dist_close, "mid_close": 1.0, "traded": traded, "reason": reason, "gaps": gaps,
    }  # fmt: skip


def test_quantile_and_describe():
    assert cal.quantile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
    assert cal.quantile([5.0], 0.9) == 5.0 and math.isnan(cal.quantile([], 0.5))
    d = cal.describe([3.0, 1.0, 2.0, float("nan")])
    assert d["n"] == 3 and d["p50"] == 2.0 and d["max"] == 3.0 and d["p10"] == pytest.approx(1.2)
    assert cal.describe([])["n"] == 0


def test_event_table_counts_reasons_life_ratio_and_born_near():
    evs = [
        _ev("withdrawn", life_s=10, size_max=50.0),
        _ev("pulled_near", life_s=2, dist_open=0.0005, dist_close=0.0002),
        _ev("pulled_near", life_s=4, dist_open=0.003, dist_close=0.0005),
        _ev("consumed", life_s=20, traded=20.0, size_max=30.0, threshold=40.0, gaps=1),
        _ev(venue="coinbase", symbol="BTC-USD"),
    ]
    t = cal.event_table(evs, 12.0, {("bybit", "BTCUSDT"): 25.0, ("coinbase", "BTC-USD"): 25.0}, 0.001)
    b = t[("bybit", "BTCUSDT")]
    assert b["n"] == 4 and b["per_day"] == 8.0  # 12 h of data -> x2
    assert b["reasons"] == {"withdrawn": 1, "pulled_near": 2, "consumed": 1} and b["reasons_per_day"]["pulled_near"] == 4.0
    assert b["life_s"]["max"] == 20.0 and b["life_s_by_reason"]["pulled_near"]["p50"] == 3.0
    assert b["at_min_share"] == 0.75  # the consumed wall passed a bar above the minimum
    assert b["ratio"]["max"] == 2.0 and b["traded_any"] == 0.25 and b["gaps_share"] == 0.25
    assert b["near_born_near"] == 1  # one pulled_near wall already opened within 10 bp
    assert t[("coinbase", "BTC-USD")]["n"] == 1


def test_flicker_counts_the_same_level_reopening():
    evs = [
        _ev(open_s=0, life_s=10),
        _ev(open_s=30, life_s=10),  # reopens 20 s after the close: flicker
        _ev(open_s=200, life_s=10),  # 160 s later: a new wall
        _ev(price=101.0, open_s=0, life_s=5),
        _ev(side="ask", price=100.0, open_s=12, life_s=5),  # other side, same price: another level
    ]
    f = cal.flicker(evs)[("bybit", "BTCUSDT")]
    assert f["walls"] == 5 and f["levels"] == 3 and f["reopen"] == 1 and f["reopen_share"] == pytest.approx(0.2)
    assert f["per_level"]["max"] == 3.0


def _row(ts, wb=0, wa=0, short=0, gaps=0, bid_view=0.003, ask_view=0.03, venue="bybit", symbol="BTCUSDT"):
    return {"venue": venue, "symbol": symbol, "ts": ts, "walls_bid": wb, "walls_ask": wa, "short_walls": short,
            "gaps": gaps, "bid_view": bid_view, "ask_view": ask_view}  # fmt: skip


def test_depth_table_and_downtime():
    rows = [_row(T0, 1, 1, short=3, gaps=1), _row(T0 + 60, 0, 0, short=1), _row(T0 + 300, 2, 0)]
    d = cal.depth_table(rows, 6.0, 0.02, 0.8)[("bybit", "BTCUSDT")]
    assert d["minutes"] == 3 and d["open_mean"] == pytest.approx(4 / 3) and d["any_share"] == pytest.approx(2 / 3)
    assert d["short"] == 4 and d["short_per_day"] == 16.0 and d["gaps"] == 1
    assert d["narrow_bid"] == 1.0 and d["narrow_ask"] == 0.0  # 0.8 x 3 % = 2.4 % reaches the 2 % zone
    assert cal.downtime([T0, T0 + 60, T0 + 300, T0 + 360]) == [(T0 + 60, T0 + 300)]


def test_stream_table_splits_hours():
    h = T0 // 3600 * 3600
    rows = [
        {"stream": "coinbase:book", "hour_ts": h, "connected_s": 3600.0, "frames": 10, "failures": 4, "reconnects": 3},
        {"stream": "coinbase:book", "hour_ts": h + 3600, "connected_s": 1800.0, "frames": 5, "failures": 2, "reconnects": 2},
        {"stream": "coinbase:book", "hour_ts": h + 7200, "connected_s": 3600.0, "frames": 5, "failures": 0, "reconnects": 0},
    ]
    t = cal.stream_table(rows, h + 3600 + 600)["coinbase:book"]
    assert t["before"]["failures"] == 4 and t["mixed"]["failures"] == 2 and t["after"]["failures"] == 0
    assert t["all"]["hours"] == 3 and t["all"]["failures_per_hour"] == 2.0
    assert t["mixed"]["connected_share"] == 0.5
    assert set(cal.stream_table(rows, None)["coinbase:book"]) == {"all"}


def test_new_bar_and_candidate_estimates():
    # bar above the minimum: K x median was binding, so the new bar is exact
    assert cal.new_bar({"threshold": 40.0}, 25.0, 25.0, 50.0, 100.0) == (80.0, 80.0)
    # bar at the minimum: the median term is only bounded (<= 25 / 50)
    assert cal.new_bar({"threshold": 25.0}, 25.0, 25.0, 50.0, 100.0) == (25.0, 50.0)
    assert cal.new_bar({"threshold": 25.0}, 25.0, 150.0, 50.0, 50.0) == (150.0, 150.0)
    evs = [
        _ev(size_max=30.0, open_s=0, life_s=600),
        _ev("pulled_near", size_max=160.0, open_s=0, life_s=120),
        _ev(size_max=40.0, open_s=700, life_s=10),
    ]
    samples = [T0 + 60 * i for i in range(12)]  # 0 .. 660 s
    c = cal.candidate(evs, samples, 24.0, min_old=25.0, min_new=150.0, k_old=50.0, k_new=50.0)
    assert c["sure_per_day"] == 1 and c["maybe_per_day"] == 1 and c["reasons_per_day"] == {"pulled_near": 1.0}
    assert c["open_mean"] == pytest.approx(2 / 12)  # open at 0 s and 60 s only
    k = cal.candidate(evs, samples, 24.0, min_old=25.0, min_new=25.0, k_old=50.0, k_new=100.0)
    assert k["sure_per_day"] == 1 and k["maybe_per_day"] == 3  # only the 160 is surely above 50
    assert cal.open_at(evs, samples) == pytest.approx(12 / 12)  # the 600-s wall covers 0..540, the 160 one 0..60
    with pytest.raises(ValueError):
        cal.candidate(evs, samples, 24.0, min_old=25.0, min_new=10.0, k_old=50.0, k_new=50.0)


def test_table_bytes_scales_to_a_day():
    t = cal.table_bytes({"book_wall_event": 1_000_000}, {"book_wall_event": 500}, 12.0)
    assert t["book_wall_event"] == {"bytes": 1e6, "bytes_per_day": 2e6, "rows": 500.0, "rows_per_day": 1000.0}


def _script():
    path = Path(__file__).parent.parent / "scripts" / "book_walls_calibration.py"
    spec = importlib.util.spec_from_file_location("book_walls_calibration_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_report_script_reads_a_db_read_only(tmp_path, monkeypatch):
    db = tmp_path / "walls.sqlite"
    con = sqlite3.connect(db)
    for stmt in MIGRATIONS[41]:
        con.execute(stmt)
    for e in [_ev(), _ev("pulled_near", price=99.0, open_s=100, size_max=160.0, dist_open=0.0005, dist_close=0.0005)]:
        con.execute(f"INSERT INTO book_wall_event ({', '.join(e)}) VALUES ({', '.join('?' * len(e))})", tuple(e.values()))  # noqa: S608
    for i in range(10):
        con.execute(
            "INSERT INTO book_depth_minute (venue, symbol, ts, mid, spread, bid_view, ask_view, walls_bid, walls_ask, gaps,"
            " short_walls) VALUES ('bybit', 'BTCUSDT', ?, 1.0, 0.0001, 0.004, 0.005, ?, 0, 0, 1)",
            (T0 + 60 * i, 1 if i < 2 else 0),
        )
    con.execute("INSERT INTO book_stream_hour VALUES ('bybit:book', ?, 3000.0, 100, 0, 2, 1)", (T0 // 3600 * 3600 + 3600,))
    con.commit()
    con.close()
    out = tmp_path / "report.md"
    mod = _script()
    opened: list[tuple[tuple, dict]] = []
    real_connect = sqlite3.connect
    monkeypatch.setattr(mod.sqlite3, "connect", lambda *a, **kw: opened.append((a, kw)) or real_connect(*a, **kw))
    assert mod.main(["--db", str(db), "--end", "2026-11-24T13:00", "--split", "", "--out", str(out)]) == 0
    monkeypatch.undo()
    text = out.read_text()
    assert "| bybit | BTCUSDT |" in text and "## Candidate higher bars" in text and "pulled_near born within" in text
    assert "## Stream failures" in text
    # the script's own connection is read-only: the same connect arguments refuse a write
    assert len(opened) == 1
    con = sqlite3.connect(*opened[0][0], **opened[0][1])
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("DELETE FROM book_wall_event")
    con.close()
    # default bar: the current minimum of each venue (Bybit on the common 25 BTC); --old-min sets one for every venue
    assert "bybit BTCUSDT 25 (current code)" in text and "| bybit | BTCUSDT | 25 | 50 |" in text
    assert mod.floor_of(("binance", "ETHUSDT"), None) == 375.0 and mod.floor_of(("binance", "ETHUSDT"), {"ETH": 750.0}) == 750.0
    assert mod.floor_of(("binanceusdm", "ETHUSDT"), None) == 750.0 and mod.floor_of(("bybit", "SOLUSDT"), None) is None
    empty = tmp_path / "empty.sqlite"
    con = sqlite3.connect(empty)
    for stmt in MIGRATIONS[41]:
        con.execute(stmt)
    con.close()
    assert mod.main(["--db", str(empty), "--out", str(tmp_path / "e.md")]) == 1
