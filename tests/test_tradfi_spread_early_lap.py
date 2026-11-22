"""Early kill (b) of IDEA-20261024-tradfi-perp-offhours-reopen: pure rule (analytics/tradfi_spread.early_kill) and the
runner scripts/verify_tradfi_spread_early_lap.py on a small in-memory store (no network)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tradedesk.analytics.tradfi_spread import covered_sessions, early_kill
from tradedesk.store.schema import MIGRATIONS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import verify_tradfi_spread_early_lap as script  # noqa: E402

DAYS = [date(2026, 10, 1) + timedelta(days=i) for i in range(20)]  # plain labels: the rule never reads the calendar


def _costs(levels: dict[str, float], days=DAYS) -> dict[str, dict[date, float | None]]:
    return {n: dict.fromkeys(days, c) for n, c in levels.items()}


# ----------------------------------------------------------------------------- pure rule


def test_reject_at_exactly_70_percent_of_names():
    c = _costs({f"F{i}": 0.0025 for i in range(7)} | {f"C{i}": 0.0012 for i in range(3)})
    r = early_kill(c, DAYS)
    assert r["n_evaluable"] == 10 and r["n_fail"] == 7 and r["share_fail"] == pytest.approx(0.7)
    assert r["decision"] == "reject"
    assert r["n_cheap"] == 3 and r["share_cheap"] == pytest.approx(0.3)
    assert r["median_of_medians"] == pytest.approx(0.0025)


def test_continue_below_70_percent_and_threshold_is_inclusive():
    c = _costs({f"F{i}": 0.0020 for i in range(6)} | {f"C{i}": 0.0019 for i in range(4)})  # 0.20 % counts as failing
    r = early_kill(c, DAYS)
    assert r["n_fail"] == 6 and r["decision"] == "continue"
    assert r["median_of_medians"] == pytest.approx(0.002)  # 5th and 6th of ten sorted medians are both 0.20 %


def test_median_per_name_over_covered_days():
    c = _costs({"A": 0.001, "B": 0.001})
    for i, d in enumerate(DAYS):
        c["A"][d] = 0.001 if i < 11 else 0.004  # 11 cheap, 9 dear: median cheap
    r = early_kill(c, DAYS)
    assert r["per_name"]["A"]["median"] == pytest.approx(0.001) and r["decision"] == "continue"


def test_wait_when_fewer_than_20_covered_sessions():
    c = _costs({"A": 0.003, "B": 0.003})
    for d in DAYS[:2]:
        c["A"][d] = c["B"][d] = None  # nobody observed: two sessions not covered
    r = early_kill(c, DAYS)
    assert len(r["covered_sessions"]) == 18 and r["decision"] == "wait"
    assert r["share_fail"] == pytest.approx(1.0)  # still reported


def test_session_coverage_needs_half_of_the_names():
    c = _costs({"A": 0.003, "B": 0.003, "C": 0.003, "D": 0.003})
    c["A"][DAYS[0]] = c["B"][DAYS[0]] = None  # 2 of 4 observed: covered
    c["A"][DAYS[1]] = c["B"][DAYS[1]] = c["C"][DAYS[1]] = None  # 1 of 4: not covered
    cov = covered_sessions(c, DAYS)
    assert DAYS[0] in cov and DAYS[1] not in cov and len(cov) == 19


def test_name_evaluable_with_75_percent_of_covered_days_and_set_half_rule():
    c = _costs({"A": 0.003, "B": 0.003, "C": 0.003, "D": 0.003})
    for d in DAYS[:6]:
        c["A"][d] = None  # 14 / 20 < 15: not evaluable
    for d in DAYS[:5]:
        c["B"][d] = None  # 15 / 20: evaluable
    r = early_kill(c, DAYS)
    assert not r["per_name"]["A"]["evaluable"] and r["per_name"]["B"]["evaluable"]
    assert r["n_evaluable"] == 3 and r["decision"] == "reject"

    # every session covered (6 of 8 names observed each day); E..H have 10 / 20 days: not evaluable; 4 / 8 = 50 % decides
    c2 = _costs({"A": 0.003, "B": 0.003, "C": 0.003, "D": 0.003})
    for n, part in (("E", DAYS[:10]), ("F", DAYS[:10]), ("G", DAYS[10:]), ("H", DAYS[10:])):
        c2[n] = {d: (0.001 if d in part else None) for d in DAYS}
    r2 = early_kill(c2, DAYS)
    assert len(r2["covered_sessions"]) == 20 and r2["n_evaluable"] == 4 and r2["decision"] == "reject"
    c2["I"] = {}  # a name with no rows at all: 4 of 9 evaluable < 50 %, sessions still covered (6 / 9)
    r3 = early_kill(c2, DAYS)
    assert len(r3["covered_sessions"]) == 20 and r3["n_evaluable"] == 4 and r3["decision"] == "wait"


# ----------------------------------------------------------------------------- runner on a small store


def _ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


def _store(rows: list[tuple]) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    for sql in MIGRATIONS[29]:
        con.execute(sql)
    con.executemany(
        "INSERT INTO tradfi_spread (ts, symbol, bid, ask, mark_price, index_price, book_ms, premium_ms) VALUES (?,?,?,?,?,?,?,?)", rows
    )
    return con


def _row(ms: int, sym: str, bid: float | None, ask: float | None) -> tuple:
    mid = (bid + ask) / 2 if bid and ask else None
    return (ms // 1000, sym, bid, ask, mid, mid, ms - 1000, ms)


# Mon 2026-10-26 (EDT): 09:25 ET = 13:25 UTC, 10:30 ET = 14:30 UTC. Mon 2026-11-02 (EST): 14:25 / 15:30 UTC.
BASE = [
    _row(_ms(2026, 10, 26, 13, 24), "AAPLUSDT", 99.95, 100.05),  # 0.1 %
    _row(_ms(2026, 10, 26, 13, 26), "AAPLUSDT", 90.0, 110.0),  # after 09:25: not the entry quote
    _row(_ms(2026, 10, 26, 14, 30), "AAPLUSDT", 99.9, 100.1),  # 0.2 %
    _row(_ms(2026, 11, 2, 14, 25), "AAPLUSDT", 99.8, 100.2),  # 0.4 %
    _row(_ms(2026, 11, 2, 15, 29), "AAPLUSDT", 99.7, 100.3),  # 0.6 %
    _row(_ms(2026, 10, 26, 13, 19), "SPYUSDT", 99.0, 101.0),  # 6 min before 09:25: stale, not observed
    _row(_ms(2026, 10, 26, 14, 30), "SPYUSDT", 99.9, 100.1),
    _row(_ms(2026, 10, 26, 13, 25), "XAUUSDT", 99.0, 101.0),  # not in the set
]


def _rows(con: sqlite3.Connection, cutoff: str = "2026-11-03T00:00"):
    cut = script.parse_cutoff(cutoff)
    sess = script.registered_sessions(date(2026, 10, 26), date(2026, 11, 2), cut)
    q, sanity = script.load_quotes(con, script.window_times(sess), {"AAPLUSDT", "SPYUSDT"}, cut)
    return script.cost_rows(q, ["AAPLUSDT", "SPYUSDT"], sess), sess, sanity, q


def test_runner_costs_at_0925_and_1030_et_across_dst():
    rows, sess, sanity, q = _rows(_store(BASE))
    assert len(sess) == 6 and sess[0].day == date(2026, 10, 26) and sess[-1].day == date(2026, 11, 2)
    a = rows["AAPLUSDT"]
    assert a[date(2026, 10, 26)]["cost"] == pytest.approx(0.0009 + 0.001 + 0.002)
    assert a[date(2026, 11, 2)]["cost"] == pytest.approx(0.0009 + 0.004 + 0.006)
    assert a[date(2026, 10, 27)]["cost"] is None
    assert rows["SPYUSDT"][date(2026, 10, 26)]["cost"] is None  # stale entry quote
    assert "XAUUSDT" not in q and sanity["rows"] == 5  # the 13:19 SPY row is outside every window
    half = script.costs_of(rows, half=True)
    assert half["AAPLUSDT"][date(2026, 10, 26)] == pytest.approx(0.0009 + 0.0015)


def test_runner_ignores_rows_after_each_time_and_after_the_cutoff():
    before, *_ = _rows(_store(BASE))
    later = [
        _row(_ms(2026, 10, 26, 13, 25) + 1000, "AAPLUSDT", 50.0, 150.0),  # 1 s after the entry time
        _row(_ms(2026, 10, 26, 14, 30) + 1000, "AAPLUSDT", 50.0, 150.0),
        _row(_ms(2026, 11, 2, 15, 30), "AAPLUSDT", 50.0, 150.0),  # exactly at exit, but cutoff below excludes it
    ]
    after, *_ = _rows(_store(BASE + later[:2]))
    assert after == before
    a, *_ = _rows(_store(BASE + later), cutoff="2026-11-02T15:30")  # the 11-02 exit is no longer before the cutoff
    assert date(2026, 11, 2) not in a["AAPLUSDT"]


def test_smoke_prints_no_cost_and_frozen_set(tmp_path, capsys):
    assert len(script.G3) == 78 and len(set(script.G3)) == 78 and script.LEVERAGED <= set(script.G3)
    db = tmp_path / "t.sqlite"
    con = sqlite3.connect(db)
    for sql in MIGRATIONS[29]:
        con.execute(sql)
    con.executemany("INSERT INTO tradfi_spread VALUES (?,?,?,?,?,?,?,?)", BASE)
    con.commit()
    con.close()
    assert script.main(["--stage", "smoke", "--db", str(db), "--out", str(tmp_path / "o.txt")]) == 0
    out = capsys.readouterr().out
    assert "sessions registered: 20 (2026-10-26 .. 2026-11-20)" in out
    assert "%" not in out  # coverage only: no spread, no cost
    assert script.main(["--stage", "run", "--db", str(db), "--cutoff", "2026-11-21T00:00"]) == 2
    assert script.main(["--stage", "smoke", "--db", str(tmp_path / "none.sqlite")]) == 1


def test_run_stage_on_small_store_reports_wait():
    rows, sess, _, _ = _rows(_store(BASE))
    lines, data = script.run(rows, sess)
    assert lines[0].startswith("PRIMARY (registered)") and data["primary"]["decision"] == "wait"
