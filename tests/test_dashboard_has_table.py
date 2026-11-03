"""scripts/dashboard.py loaders check table existence through common_helpers.has_table: absent tables (and views of
the same name) read as None, a damaged file still reads as 'unreadable' (no network)."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402


def _db(path: Path, *ddl: str) -> Path:
    con = sqlite3.connect(path)
    for s in ddl:
        con.execute(s)
    con.commit()
    con.close()
    return path


def test_load_journal_reads_only_existing_tables(tmp_path):
    p = _db(
        tmp_path / "j.sqlite",
        "CREATE TABLE rec_paper (id INTEGER PRIMARY KEY, opened_ts REAL)",
        "CREATE VIEW journal AS SELECT 1 AS id",  # a view is not a table: skipped exactly as before
    )
    data, err = db.load_journal(p)
    assert err is None
    assert data == {"journal": None, "rec_paper": [], "trade_plan": None, "plan_shadow": 0, "forecast_log": None}


def test_load_pm_gap_without_the_snapshot_table(tmp_path):
    data, err = db.load_pm_gap(_db(tmp_path / "p.sqlite", "CREATE VIEW pm_gap_snapshot AS SELECT 1 AS ts"))
    assert err is None and data == {"ts": None, "rows": None, "text_rows": []}


def test_load_demo_reads_only_existing_tables(tmp_path):
    p = _db(tmp_path / "d.sqlite", "CREATE TABLE demo_option_leg (status TEXT)", "INSERT INTO demo_option_leg VALUES ('open')")
    data, err = db.load_demo(p)
    assert err is None and data == {"trades": None, "audit": None, "option_legs": {"open": 1}}


def test_damaged_file_is_still_unreadable(tmp_path):
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"not a database at all" * 100)
    for load in (db.load_journal, db.load_pm_gap, db.load_demo):
        data, err = load(junk)
        assert data is None and err.startswith("unreadable"), load.__name__
