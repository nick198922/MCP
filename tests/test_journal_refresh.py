import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dashboard as db  # noqa: E402
import journal_refresh as jr  # noqa: E402

ADDR = "0x" + "ab" * 20


def _db(path: Path, rows: list[str]) -> str:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE journal (id INTEGER PRIMARY KEY, tags TEXT)")
    con.executemany("INSERT INTO journal (tags) VALUES (?)", [(t,) for t in rows])
    con.commit()
    con.close()
    return str(path)


def test_count_rows_by_venue_and_import_tag(tmp_path):
    d = _db(tmp_path / "j.sqlite", ['["bybit", "import", "linear"]', '["bybit-demo", "auto"]', '["hyperliquid", "import"]'])
    assert jr.count_rows(d, "bybit") == 1
    assert jr.count_rows(d, "hyperliquid") == 1
    assert jr.count_rows(str(tmp_path / "missing.sqlite"), "bybit") is None


def test_refresh_runs_both_venues_and_writes_status(tmp_path, monkeypatch):
    d = _db(tmp_path / "j.sqlite", ['["bybit", "import"]'])
    monkeypatch.setattr(jr, "STATE", tmp_path / "st")
    monkeypatch.setattr(jr, "STATUS", tmp_path / "st" / "s.json")
    addr_file = tmp_path / "hl"
    addr_file.write_text(ADDR + "\n")
    monkeypatch.setattr(jr, "HL_ADDRESS_FILE", addr_file)
    calls = []

    class P:
        def __init__(self, rc):
            self.returncode, self.stderr = rc, "Traceback\nhttpx.ConnectError: secret-ish detail"

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "bybit_import.py" in cmd[1]:
            con = sqlite3.connect(d)
            con.execute("""INSERT INTO journal (tags) VALUES ('["bybit", "import"]')""")
            con.commit()
            con.close()
            return P(0)
        return P(1)

    monkeypatch.setattr(jr.subprocess, "run", fake_run)
    s = jr.refresh(30, d)
    assert calls[0][-2:] == ["--days", "30"] and calls[1][-1] == ADDR
    assert s["venues"]["bybit"] == {"ok": True, "added": 1, "error": ""}
    assert s["venues"]["hyperliquid"]["ok"] is False
    assert s["venues"]["hyperliquid"]["error"] == "код виходу 1 (httpx.ConnectError)"  # class only, never the message
    assert s["state"] == "error"
    assert json.loads((tmp_path / "st" / "s.json").read_text())["state"] == "error"


def test_refresh_without_hl_address_skips_it(tmp_path, monkeypatch):
    d = _db(tmp_path / "j.sqlite", [])
    monkeypatch.setattr(jr, "STATE", tmp_path)
    monkeypatch.setattr(jr, "STATUS", tmp_path / "s.json")
    monkeypatch.setattr(jr, "HL_ADDRESS_FILE", tmp_path / "none")

    class P:
        returncode, stderr = 0, ""

    monkeypatch.setattr(jr.subprocess, "run", lambda cmd, **kw: P())
    s = jr.refresh(30, d)
    assert s["venues"]["hyperliquid"]["skipped"] is True
    assert s["state"] == "done"


def test_set_hl_address_validates(tmp_path, monkeypatch):
    f = tmp_path / "a" / "hl"
    monkeypatch.setattr(jr, "HL_ADDRESS_FILE", f)
    assert jr.main(["--set-hl-address", "0x123"]) == 2
    assert jr.main(["--set-hl-address", ADDR]) == 0
    assert jr.hl_address() == ADDR


def test_start_refresh_once_and_not_too_often():
    now = time.time()
    spawned = []
    assert db.start_journal_refresh(now, {}, spawn=spawned.append) == "started"
    assert spawned and spawned[0][-1].endswith("scripts/journal_refresh.py")
    running = {"state": "running", "started": now - 30}
    assert db.start_journal_refresh(now, running, spawn=spawned.append) == "running"
    crashed = {"state": "running", "started": now - db.REFRESH_STALE_S - 5}
    assert db.start_journal_refresh(now, crashed, spawn=spawned.append) == "started"
    recent = {"state": "done", "started": now - 40, "finished": now - 20}
    assert db.start_journal_refresh(now, recent, spawn=spawned.append) == "too_soon"
    assert len(spawned) == 2


def test_journal_page_shows_button_and_status():
    now = time.time()
    data = {"journal": [], "rec_paper": None, "trade_plan": None}
    page = db.render_journal(data, None, now, {"page": 1}, None, {})
    assert "action='/journal/refresh'" in page and "Оновити угоди" in page
    run = db.render_journal(data, None, now, {"page": 1}, None, {"state": "running", "started": now - 5, "venues": {"bybit": {"ok": True}}})
    assert "disabled" in run and "location.replace('/journal')" in run and "Bybit ✓" in run
    done = {
        "state": "error",
        "started": now - 60,
        "finished": now - 50,
        "venues": {"bybit": {"ok": True, "added": 3}, "hyperliquid": {"ok": False, "added": 0, "error": "<x>"}},
    }
    page = db.render_journal(data, None, now, {"page": 1}, None, done)
    assert "Bybit: +3" in page and "Hyperliquid: помилка — &lt;x&gt;" in page
    odd = db.render_journal(data, None, now, {"page": 1}, None, {"state": "done", "venues": "junk"})
    assert "Оновити угоди" in odd
