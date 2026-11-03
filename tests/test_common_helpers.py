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


def test_backup_db_copies_through_a_quoted_read_only_source(tmp_path):
    # a space and '?' in both paths: the source is opened read-only by its real name, nothing else is created
    (tmp_path / "src dir").mkdir()
    src = _store(tmp_path / "src dir" / "a b?mode=rwc.sqlite")
    dst = tmp_path / "out dir?x" / "new" / "copy b?.sqlite"  # parent dirs created
    cmn.backup_db(src, dst)
    con = cmn.connect_ro(dst)
    try:
        assert con.execute("SELECT symbol FROM journal").fetchall() == [("BTCUSDT",)]
        assert cmn.has_table(con, "journal")
    finally:
        con.close()
    assert sorted(x.name for x in src.parent.iterdir()) == [src.name]
    assert sorted(x.name for x in dst.parent.iterdir()) == [dst.name]
    stale = sqlite3.connect(dst)
    stale.execute("INSERT INTO journal (symbol) VALUES ('STALE')")
    stale.commit()
    stale.close()
    cmn.backup_db(str(src), dst)  # a str source works and an existing copy is replaced, not merged
    con = cmn.connect_ro(dst)
    try:
        assert con.execute("SELECT count(*) FROM journal").fetchone() == (1,)
    finally:
        con.close()


def test_backup_db_missing_source_raises_and_creates_nothing(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        cmn.backup_db(tmp_path / "no ne?.sqlite", tmp_path / "out" / "copy.sqlite")
    assert not (tmp_path / "no ne?.sqlite").exists()
    assert not (tmp_path / "out" / "copy.sqlite").exists()


def test_backup_db_closes_both_connections_on_failure(tmp_path, monkeypatch):
    closed = []
    real = sqlite3.connect

    class Con:
        def __init__(self, c):
            self.c = c

        def backup(self, other):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            closed.append(self)
            self.c.close()

    src = _store(tmp_path / "s.sqlite")
    monkeypatch.setattr(cmn.sqlite3, "connect", lambda *a, **k: Con(real(*a, **k)))
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        cmn.backup_db(src, tmp_path / "c.sqlite")
    assert len(closed) == 2


def test_has_table(tmp_path):
    con = cmn.connect_ro(_store(tmp_path / "s.sqlite"))
    try:
        assert cmn.has_table(con, "journal") is True
        assert cmn.has_table(con, "trade_plan") is False  # a view of that name is not a table
        assert cmn.has_table(con, "demo_trade") is False
        assert cmn.has_table(con, "journal' OR '1'='1") is False  # a parameter, not SQL
    finally:
        con.close()


def test_rss_mb_is_peak_rss_in_mb(monkeypatch):
    seen = []

    class Usage:
        ru_maxrss = 1537  # KiB on Linux

    def fake(who):
        seen.append(who)
        return Usage()

    monkeypatch.setattr(cmn.resource, "getrusage", fake)
    v = cmn.rss_mb()
    assert isinstance(v, float) and v == 1537 / 1024.0  # same value the '/ 1024.0' copies gave
    assert f"{v:.0f}" == "2"
    assert seen == [cmn.resource.RUSAGE_SELF]


def test_rss_mb_real_process():
    assert cmn.rss_mb() > 0


@pytest.mark.parametrize(
    "name",
    ["verify_negrisk_step0", "verify_pm_touch", "verify_unlock_schedules", "verify_free_supply_history"],
)
def test_scripts_use_the_shared_rss_mb(name):
    # item 36 of the standing-improvements card: the local copies are gone, the script logs through common_helpers
    mod = __import__(name)
    assert not hasattr(mod, "rss_mb") and not hasattr(mod, "resource")
    assert mod.cmn.rss_mb is cmn.rss_mb
