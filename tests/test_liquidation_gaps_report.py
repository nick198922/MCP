"""scripts/liquidation_gaps_report.py: liquidation stream holes vs quiet hours, on a synthetic in-memory DB."""

from __future__ import annotations

import math
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradedesk.config import Settings
from tradedesk.store.schema import MIGRATIONS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import liquidation_gaps_report as script  # noqa: E402

H = 3600
T0 = (1_700_000_000 // H) * H
HOURS = 72
CUTOFF = T0 + 48 * H + 30 * 60  # pre: full hours 0..47, post: 49..70
HOLE, QUIET = 20, 10  # Bybit silent in both; Binance busy in HOLE, nearly silent in QUIET


def _schema(con: sqlite3.Connection) -> None:
    for stmts in MIGRATIONS.values():
        for stmt in stmts:
            if "liquidations" in stmt and "CREATE" in stmt:
                con.execute(stmt)


def _rows() -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    for k in range(HOURS * 60):
        ts, hour, minute = T0 + k * 60, k // 60, k % 60
        if hour != QUIET or minute < 5:
            rows.append((ts, "binanceusdm", "BTCUSDT"))
        if hour not in (HOLE, QUIET):
            rows.append((ts, "bybit", "BTCUSDT"))
        rows.append((ts, "okx", "BTC-USDT-SWAP"))
    # A Binance-only coin: counted in "all Binance", never in the OKX reference.
    rows += [(T0 + HOLE * H + 10 + i, "binanceusdm", "JCTUSDT") for i in range(100)]
    return rows


def _fill(con: sqlite3.Connection) -> None:
    _schema(con)
    con.executemany(
        "INSERT INTO liquidations (ts, venue, symbol, side, qty, price, notional_usd) VALUES (?, ?, ?, 'sell', 1, 1, 1)", _rows()
    )
    con.commit()


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    _fill(c)
    yield c
    c.close()


def test_pure_helpers():
    ts = [10, 20, 20, 50, 3600 + 5]
    assert script.count_between(ts, 10, 50) == 2  # strictly inside
    assert script.count_between(ts, 60, 10) == 0
    assert script.hourly(ts, 0, 2) == [4, 1]
    assert script.largest_gaps(ts, 0, 10_000, 2) == [(50, 3605), (20, 50)]
    assert script.poisson_cdf(0, 2.0) == pytest.approx(math.exp(-2))
    assert script.poisson_cdf(1, 1.0) == pytest.approx(2 * math.exp(-1))
    assert script.poisson_cdf(3, 0.0) == 1.0


def test_long_pause_rate_at_the_same_activity():
    lo, cutoff, hi = T0, T0 + 10 * H, T0 + 15 * H
    cut = [(T0 + 2 * H + 30 * 60, T0 + 2 * H + 50 * 60), (T0 + 5 * H + 10 * 60, T0 + 5 * H + 30 * 60)]
    target = [t for t in range(lo, hi + 1, 60) if not any(a < t < b for a, b in cut)]
    ref = list(range(lo + 1, hi, 60))
    r = script.long_pause_test(target, ref, lo, cutoff, hi, 15)
    # 2 long pauses in 10 pre hours -> 0.2/hour; 5 post hours at the same activity -> 1.0 expected, 0 seen.
    assert (r["pre"], r["pre_hours"], r["post"], r["post_hours"]) == (2, 10, 0, 5)
    assert r["expected_post"] == pytest.approx(1.0)
    assert r["p_le"] == pytest.approx(math.exp(-1))


def test_report_flags_the_hole_and_lists_the_quiet_hour(con):
    code, text = script.build(con, CUTOFF, None, 2, ("BTCUSDT",), 60.0, 8.0)
    assert code == 0
    bybit = text.split("## okx")[0]
    # N = median hourly Binance BTCUSDT count = 60: the HOLE hour (60) is a hole, the QUIET hour (5) is not.
    assert "N = 60 reference rows/hour" in bybit
    assert "before cutoff: 48 full hours" in bybit
    assert "zero hours 2: probable holes (reference >= N) 1, quiet or unclear 1" in bybit
    assert f"hole? {script._t(T0 + HOLE * H)} reference 60" in bybit
    assert f"quiet? {script._t(T0 + QUIET * H)} reference 5" in bybit
    # Both 61-minute pauses; ties go by start time. All Binance in the HOLE pause includes the Binance-only coin.
    assert f"pause 61 min from {script._t(T0 + QUIET * H - 60)}: reference 5," in bybit
    assert f"pause 61 min from {script._t(T0 + HOLE * H - 60)}: reference 60," in bybit and "all Binance 160" in bybit
    assert "after cutoff: 22 full hours" in bybit and "zero hours 0" in bybit.split("after cutoff")[1]
    assert "pauses >= 60 min: before 2 in 48 h; after 0 in 22 h" in bybit
    assert "before 1, zero 1; after 0, zero 0" in bybit
    okx = text.split("## okx")[1]
    assert "vs binanceusdm, 1 symbols OKX also lists" in okx  # JCTUSDT is not on OKX
    assert "zero hours 0" in okx and "hole?" not in okx


def test_min_ref_override_turns_the_quiet_hour_into_a_hole(con):
    _, text = script.build(con, CUTOFF, 5, 0, ("BTCUSDT",), 60.0, 8.0)
    assert "zero hours 2: probable holes (reference >= N) 2, quiet or unclear 0" in text


def test_cutoff_outside_the_window_and_empty_db(con):
    code, text = script.build(con, T0 - H, None, 1, ("BTCUSDT",), 60.0, 8.0)
    assert code == 1 and "outside the common window" in text
    empty = sqlite3.connect(":memory:")
    _schema(empty)
    assert script.build(empty, CUTOFF, None, 1, ("BTCUSDT",), 60.0, 8.0)[0] == 1
    empty.close()


def test_connection_journal_classifies_zero_hours(con):
    # Collector journal (schema 40) for Bybit from hour 5 on; a third silent Bybit hour (30) on a busy Binance.
    for stmt in MIGRATIONS[40]:
        con.execute(stmt)
    con.execute("DELETE FROM liquidations WHERE venue = 'bybit' AND ts >= ? AND ts < ?", (T0 + 30 * H, T0 + 31 * H))
    journal = [(5, 3600.0, 200), (QUIET, 3500.0, 40), (HOLE, 1200.0, 3), (30, 2700.0, 50)]
    con.executemany(
        "INSERT INTO liquidation_stream_hour (exchange, hour_ts, connected_s, frames) VALUES ('bybit', ?, ?, ?)",
        [(T0 + h * H, c, f) for h, c, f in journal],
    )
    con.commit()
    code, text = script.build(con, CUTOFF, None, 2, ("BTCUSDT",), 60.0, 8.0)
    assert code == 0
    bybit, okx = text.split("## okx")
    assert "zero hours 3: probable holes (reference >= N) 2, quiet or unclear 1" in bybit
    # Deployed cycle (600 s windows + 5 s pause): expected 3600 * 600 / 605 = 3570 s an hour.
    assert "collector windows 600 s + pause 5 s -> expected connected 3570 s an hour" in text
    assert "connection journal (3 of the zero hours): quiet market 1, drop 1, unclear 1" in bybit
    q = f"quiet? {script._t(T0 + QUIET * H)} reference 5; connected 97% of the hour (98% of the expected), frames 40: quiet market"
    assert q in bybit
    assert f"hole? {script._t(T0 + HOLE * H)} reference 60; connected 33% of the hour (34% of the expected), frames 3: drop" in bybit
    assert f"hole? {script._t(T0 + 30 * H)} reference 60; connected 75% of the hour (76% of the expected), frames 50: unclear" in bybit
    assert "connection journal" not in okx  # no OKX rows in the journal: as before


def test_connection_notes_edges():
    e = script.EXPECTED_S
    journal = {9: (1200.0, 2), 10: (3600.0, 0), 12: (3300.0, 7)}
    notes = script.connection_notes([8, 9, 10, 11, 12], journal)
    # The journal's first hour is partial (the service started inside it): not covered, reported as before.
    assert 8 not in notes and 9 not in notes
    assert notes[10] == (1.0, 1.0, 0, "unclear")  # connected all hour but not one frame (not even a heartbeat reply)
    assert notes[11] == (0.0, 0.0, 0, "drop")  # covered, no row: the collector had no connection at all
    assert notes[12][3] == "quiet market" and notes[12][1] == pytest.approx(3300.0 / e)
    assert script.classify(0.5 * e, 5) == "unclear" and script.classify(0.5 * e - 1, 5) == "drop"
    assert script.connection_notes([10], {}) == {} and script.connection_notes([10], None) == {}
    bare = sqlite3.connect(":memory:")
    assert script.load_stream_hours(bare, "bybit") == {}  # a DB from before schema 40
    bare.close()


@pytest.mark.parametrize("window, restart", [(script.DEFAULT_WINDOW_S, script.DEFAULT_RESTART_S), (60.0, 5.0)])
def test_quiet_threshold_is_inclusive_at_90_percent_of_the_expected_time(window, restart):
    e = script.expected_connected_s(window, restart)
    assert script.classify(0.9 * e, 1, e) == "quiet market"  # >= (a > would say unclear)
    assert script.classify(0.9 * e - 1, 1, e) == "unclear"  # (a 0.8 threshold would say quiet market)
    assert script.classify(0.9 * e, 0, e) == "unclear"  # no frame at all


def test_healthy_hours_at_the_real_cycle_are_a_quiet_market():
    # Deployed: 600 s windows + 5 s pause; a ~1 s handshake per window -> 599 of every 606 s, 98.8 % of the hour.
    e = script.expected_connected_s(script.DEFAULT_WINDOW_S, script.DEFAULT_RESTART_S)
    assert e == pytest.approx(3600 * 600 / 605)
    assert script.classify(3600 * 599 / 606, 3, e) == "quiet market"
    # Code default 60 s windows: the same 1 s handshake gives 3218 s = 89.4 % of the hour (below 90 % of 3600, so a
    # healthy hour would have read as unclear), but 96.8 % of the 3323 s the cycle allows.
    e60 = script.expected_connected_s(60.0, 5.0)
    assert e60 == pytest.approx(3600 * 60 / 65)
    healthy = 3600 * 59 / 66
    assert healthy < 0.9 * H and script.classify(healthy, 3, e60) == "quiet market"
    assert script.classify(55 * 59.5, 3, e60) == "quiet market"  # raw 90.9 %
    assert script.classify(0.88 * H, 3, e60) == "quiet market"  # raw 88 % = 95.3 % of the expected
    assert script.classify(0.4 * e60, 3, e60) == "drop"


def test_default_cycle_matches_the_deployed_collector():
    # The report's defaults must follow deploy/features.env (window) and the code default (pause, not overridden there).
    env = Path(__file__).resolve().parent.parent / "deploy" / "features.env"
    lines = [ln.strip() for ln in env.read_text(encoding="utf-8").splitlines()]
    window = [ln.split("=", 1)[1] for ln in lines if ln.startswith("TRADEDESK_COLLECT_LIQUIDATIONS_WINDOW_SECONDS=")]
    assert [float(w) for w in window] == [script.DEFAULT_WINDOW_S]
    assert not any(ln.startswith("TRADEDESK_COLLECT_LIQUIDATIONS_RESTART_SECONDS=") for ln in lines)
    assert script.DEFAULT_RESTART_S == Settings().collect_liquidations_restart_seconds


def test_cycle_flags_change_the_expected_time(tmp_path, con):
    for stmt in MIGRATIONS[40]:
        con.execute(stmt)
    # Hour 5 starts the journal; QUIET (hour 10) connected 3218 s: 98 % of E at 600 + 5, 97 % at 60 + 5.
    con.executemany(
        "INSERT INTO liquidation_stream_hour (exchange, hour_ts, connected_s, frames) VALUES ('bybit', ?, ?, ?)",
        [(T0 + 5 * H, 3600.0, 9), (T0 + QUIET * H, 3218.0, 9)],
    )
    con.commit()
    _, text = script.build(con, CUTOFF, None, 0, ("BTCUSDT",), 60.0, 8.0, 60.0, 5.0)
    assert "collector windows 60 s + pause 5 s -> expected connected 3323 s an hour" in text
    assert "connected 89% of the hour (97% of the expected), frames 9: quiet market" in text
    db, out = tmp_path / "liq.sqlite", tmp_path / "report.txt"
    file_con = sqlite3.connect(db)
    con.backup(file_con)
    file_con.close()
    iso = datetime.fromtimestamp(CUTOFF, UTC).isoformat()
    args = ["--db", str(db), "--cutoff", iso, "--bybit-symbols", "BTCUSDT", "--top", "0", "--out", str(out)]
    assert script.main([*args, "--window-seconds", "60", "--restart-seconds", "5"]) == 0
    assert out.read_text(encoding="utf-8") == text
    with pytest.raises(SystemExit):
        script.main(["--db", str(tmp_path / "x.sqlite"), "--window-seconds", "0"])


def test_main_reads_read_only_and_writes_out(tmp_path, con):
    db = tmp_path / "liq.sqlite"
    file_con = sqlite3.connect(db)
    _fill(file_con)
    file_con.close()
    before = db.read_bytes()
    out, out_naive = tmp_path / "report.txt", tmp_path / "naive.txt"
    iso = datetime.fromtimestamp(CUTOFF, UTC).isoformat()
    assert script.main(["--db", str(db), "--cutoff", iso, "--bybit-symbols", "btcusdt", "--top", "2", "--out", str(out)]) == 0
    naive = iso.removesuffix("+00:00")  # no offset = UTC
    assert script.main(["--db", str(db), "--cutoff", naive, "--bybit-symbols", "BTCUSDT", "--top", "2", "--out", str(out_naive)]) == 0
    expected = script.build(con, CUTOFF, None, 2, ("BTCUSDT",), 60.0, 8.0)[1]
    assert out.read_text(encoding="utf-8") == expected == out_naive.read_text(encoding="utf-8")
    assert db.read_bytes() == before  # mode=ro: nothing written
