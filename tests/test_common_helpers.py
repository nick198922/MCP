"""scripts/common_helpers.py: the read-only SQLite helpers shared by the report scripts and the dashboard."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import common_helpers as cmn  # noqa: E402


def _store(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE journal (id INTEGER PRIMARY KEY, symbol TEXT)")
    con.execute("INSERT INTO journal (symbol) VALUES ('BTCUSDT')")
    con.execute("CREATE VIEW trade_plan AS SELECT * FROM journal")  # a view is not a table
    con.commit()
    con.close()
    return path


def test_connect_ro_reads_refuses_writes_and_quotes_the_path(tmp_path):
    # '?', '#', '%' and a space would end or mangle an unquoted URI file name (and could drop mode=ro)
    p = _store(tmp_path / "a b?mode=rwc#x%20.sqlite")
    con = cmn.connect_ro(p)
    try:
        assert con.execute("SELECT symbol FROM journal").fetchall() == [("BTCUSDT",)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("INSERT INTO journal (symbol) VALUES ('X')")
    finally:
        con.close()
    assert sorted(x.name for x in tmp_path.iterdir()) == [p.name]  # nothing created next to it


def test_connect_ro_never_creates_a_missing_file(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        cmn.connect_ro(str(tmp_path / "none.sqlite"))
    assert not (tmp_path / "none.sqlite").exists()


def test_connect_ro_passes_uri_and_timeout(tmp_path, monkeypatch):
    seen = {}
    real = sqlite3.connect

    def spy(database, *a, **k):
        seen.update(uri=database, kw=k)
        return real(database, *a, **k)

    monkeypatch.setattr(cmn.sqlite3, "connect", spy)
    cmn.connect_ro(_store(tmp_path / "s.sqlite")).close()
    assert seen["uri"].startswith("file:") and seen["uri"].endswith("?mode=ro")
    assert seen["kw"] == {"uri": True, "timeout": 5}
    cmn.connect_ro(tmp_path / "s.sqlite", timeout=10).close()
    assert seen["kw"]["timeout"] == 10


def test_open_ro_reasons_and_never_raises(tmp_path, monkeypatch):
    assert cmn.open_ro(tmp_path / "none.sqlite") == (None, "missing")
    con, why = cmn.open_ro(_store(tmp_path / "s.sqlite"))
    try:
        assert why is None and con.execute("SELECT count(*) FROM journal").fetchone() == (1,)
    finally:
        con.close()

    def boom(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(cmn.sqlite3, "connect", boom)
    assert cmn.open_ro(tmp_path / "s.sqlite") == (None, "unreadable: OperationalError")


def test_open_ro_permission_error_from_exists():
    class Closed:  # exists() on a path under a directory without +x raises PermissionError
        def exists(self) -> bool:
            raise PermissionError("closed")

    assert cmn.open_ro(Closed()) == (None, "unreadable: PermissionError")


def test_has_table(tmp_path):
    con = cmn.connect_ro(_store(tmp_path / "s.sqlite"))
    try:
        assert cmn.has_table(con, "journal") is True
        assert cmn.has_table(con, "trade_plan") is False  # a view of that name is not a table
        assert cmn.has_table(con, "demo_trade") is False
        assert cmn.has_table(con, "journal' OR '1'='1") is False  # a parameter, not SQL
    finally:
        con.close()
