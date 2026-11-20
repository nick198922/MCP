"""OI-cap events and gates G1/G2 (analytics/hl_oi_cap_events.py) and the step-1 script on a synthetic in-memory DB."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from tradedesk.analytics import hl_oi_cap_events as ev
from tradedesk.store.schema import MIGRATIONS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import verify_hl_oi_cap_gates as script  # noqa: E402

H = ev.HOUR_S
T0 = 1_700_000_000 - 1_700_000_000 % H  # an hour boundary


def series(coin: str, capped_hours: set[int], n_hours: int, funding: float = 1e-5) -> list[tuple]:
    """Hourly rows of one coin over n_hours from T0; at_cap on the given hour indexes."""
    return [(T0 + i * H, coin, int(i in capped_hours), funding) for i in range(n_hours)]


def test_run_of_six_after_warmup_is_one_event_with_t0_at_sixth_hour():
    rows = series("AAA", set(range(30, 40)), 100)
    out = ev.events(rows, first_ts=T0, cutoff=T0 + 100 * H)
    assert [(e.coin, e.start_ts, e.t0) for e in out] == [("AAA", T0 + 30 * H, T0 + 35 * H)]


def test_five_hours_is_not_an_event_and_a_missing_hour_breaks_the_run():
    short = series("AAA", set(range(30, 35)), 100)
    assert ev.events(short, first_ts=T0, cutoff=T0 + 100 * H) == []
    holed = [r for r in series("BBB", set(range(30, 40)), 100) if r[0] != T0 + 33 * H]  # 30-32 and 34-39
    out = ev.events(holed, first_ts=T0, cutoff=T0 + 100 * H)
    assert [e.start_ts for e in out] == [T0 + 34 * H]


def test_warmup_and_cutoff_exclude_events():
    rows = series("AAA", set(range(10, 20)), 100)  # starts inside the 24 h warm-up
    assert ev.events(rows, first_ts=T0, cutoff=T0 + 100 * H) == []
    late = series("AAA", set(range(95, 100)) | {100}, 101)  # sixth hour 100 is at the cutoff
    assert ev.events(late, first_ts=T0, cutoff=T0 + 100 * H) == []


def test_24h_rule_blocks_a_reentry_and_none_drops_it():
    # run 30-35 (6 h, ends at hour 35, last hour covers 35-36); second run starts 36 + 24 = 60 -> fresh; 59 -> not
    fresh = series("AAA", set(range(30, 36)) | set(range(60, 66)), 120)
    assert len(ev.events(fresh, first_ts=T0, cutoff=T0 + 120 * H)) == 2
    blocked = series("AAA", set(range(30, 36)) | set(range(59, 65)), 120)
    assert len(ev.events(blocked, first_ts=T0, cutoff=T0 + 120 * H)) == 1
    assert len(ev.events(blocked, first_ts=T0, cutoff=T0 + 120 * H, lookback_s=None)) == 2


def test_a_short_run_does_not_block_the_next_event():
    rows = series("AAA", set(range(30, 33)) | set(range(40, 46)), 100)
    assert [e.start_ts for e in ev.events(rows, first_ts=T0, cutoff=T0 + 100 * H)] == [T0 + 40 * H]


def test_funding_is_taken_at_t0():
    rows = series("AAA", set(range(30, 40)), 100, funding=-1e-5)
    rows = [(t, c, a, 2e-5 if t == T0 + 35 * H else f) for t, c, a, f in rows]
    (e,) = ev.events(rows, first_ts=T0, cutoff=T0 + 100 * H)
    assert e.funding == pytest.approx(2e-5)


def test_events_before_t_do_not_depend_on_rows_after_t():
    rows = series("AAA", set(range(30, 40)), 200) + series("BBB", set(range(150, 170)), 200)
    t = T0 + 100 * H
    before = ev.events(rows, first_ts=T0, cutoff=t)
    changed = [(ts, c, 1 if ts >= t else a, f * 7 if ts >= t else f) for ts, c, a, f in rows]
    assert ev.events(changed, first_ts=T0, cutoff=t) == before
    full = ev.events(rows, first_ts=T0, cutoff=T0 + 200 * H)
    assert [e for e in full if e.t0 < t] == before


def test_gates_rates_shares_and_weeks():
    cutoff = T0 + 24 * H + 2 * ev.WEEK_S  # exposure = exactly two weeks
    evs = [ev.Event(f"C{i}", 0, cutoff - (i + 1) * H, 1e-5 if i % 2 else -1e-5) for i in range(16)]
    g = ev.gates(evs, first_ts=T0, cutoff=cutoff)
    assert g["exposure_weeks"] == pytest.approx(2.0)
    assert g["rate_per_week"] == pytest.approx(8.0) and g["g1_pass"]
    assert g["share_pos"] == pytest.approx(0.5) and g["g2_pass"] and g["pass"]
    assert g["per_week_back"] == {0: 16}
    g15 = ev.gates(evs[:15], first_ts=T0, cutoff=cutoff)
    assert not g15["g1_pass"]
    all_pos = [ev.Event(e.coin, 0, e.t0, 1e-5) for e in evs]
    assert not ev.gates(all_pos, first_ts=T0, cutoff=cutoff)["g2_pass"]
    zero = [ev.Event(e.coin, 0, e.t0, 0.0 if i < 12 else 1e-5) for i, e in enumerate(evs)]  # 4/16 = 0.25: pass
    assert ev.gates(zero, first_ts=T0, cutoff=cutoff)["share_pos"] == pytest.approx(0.25)
    assert ev.gates(zero, first_ts=T0, cutoff=cutoff)["g2_pass"]
    empty = ev.gates([], first_ts=T0, cutoff=cutoff)
    assert empty["n"] == 0 and empty["share_pos"] is None and not empty["g2_pass"]


def test_wilson_interval():
    lo, hi = ev.wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    assert ev.wilson(0, 0) is None


def test_coverage_counts_missing_hours_and_longest_gap():
    hours = [T0 + i * H for i in range(10) if i not in (3, 4, 7)]
    assert ev.coverage(hours, T0, T0 + 10 * H) == {"expected": 10, "stored": 7, "missing": 3, "longest_gap_h": 2}


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(MIGRATIONS[31][0])
    rows = series("AAA", set(range(30, 40)), 60, funding=1e-5) + series("BBB", set(range(40, 47)), 60, funding=-1e-5)
    conn.executemany(
        "INSERT INTO hl_oi_cap_snapshot (ts, coin, at_cap, oi_usd, funding, mark, oracle, vol24h, fetched_ts)"
        " VALUES (?, ?, ?, 1e6, ?, 1.0, 1.0, 1e7, ?)",
        [(t, c, a, f, t + 60) for t, c, a, f in rows],
    )
    return conn


def test_script_smoke_reports_coverage_only(db):
    rows = script.load(db, T0 + 60 * H)
    lines, data = script.smoke(rows, T0 + 60 * H)
    assert data["rows"] == 120 and data["coins"] == 2 and data["missing"] == 0 and data["expected"] == 60
    assert "at_cap" not in "\n".join(lines) and "подій" not in "\n".join(lines).lower()


def test_script_run_counts_events_and_gates(db):
    cutoff = T0 + 60 * H
    rows = script.load(db, cutoff)
    lines, data = script.run(rows, cutoff)
    assert [e["coin"] for e in data["events"]] == ["AAA", "BBB"]
    assert data["gates"]["funding_pos"] == 1 and data["gates"]["share_pos"] == pytest.approx(0.5)
    assert data["gates"]["rate_per_week"] == pytest.approx(2 / (36 / 168))  # 2 events in 36 h of exposure: 9.3/week
    assert data["gates"]["g1_pass"] and data["gates"]["g2_pass"]


def test_script_main_on_missing_db(tmp_path, capsys):
    assert script.main(["--stage", "smoke", "--db", str(tmp_path / "none.sqlite")]) == 2
    assert "недоступна" in capsys.readouterr().out
