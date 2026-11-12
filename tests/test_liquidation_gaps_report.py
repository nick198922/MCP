"""scripts/liquidation_gaps_report.py: liquidation stream holes vs quiet hours, on a synthetic in-memory DB."""

from __future__ import annotations

import math
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
