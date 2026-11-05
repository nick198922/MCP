"""scripts/dashboard.py, tab «Система»: the «Здоров'я колекторів» block (rules from analytics/collector_health.py)."""

import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from tradedesk.analytics import collector_health as ch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402

import test_dashboard as tdb  # noqa: E402

HOUR = 3600
NOW = int(datetime(2026, 11, 2, 8, 30, tzinfo=UTC).timestamp())
# pm_gap alone: the jobs the collector runs by default count unless switched off
PM = {"TRADEDESK_PM_GAP_ENABLED": "1"} | {s.flag: "0" for s in ch.SPECS if s.default_on}
ICON = "<span class='info' tabindex='0' role='button'"


def _pm_db(path: Path, hours_missing: range = range(0)) -> Path:
    """pm_gap_snapshot with one row a minute into every UTC hour of the last 8 days, except `hours_missing` (hours back
    from the last finished one)."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE pm_gap_snapshot (ts INTEGER)")
    last = NOW // HOUR * HOUR - HOUR  # 07:00, the last bucket due by 08:30
    con.executemany("INSERT INTO pm_gap_snapshot VALUES (?)", [(last - k * HOUR + 60,) for k in range(8 * 24) if k not in hours_missing])
    con.commit()
    con.close()
    return path


def _page(h: dict | None) -> str:
    return db.render({**tdb._project_snap(), "health": h})


def _section(page: str) -> str:
    m = re.search(r"<section class='wide'><h2>Здоров&#x27;я колекторів.*?</section>", page, re.S)
    assert m, "block missing"
    return m.group(0)


def test_all_snapshots_present_is_ok_and_folded(tmp_path):
    h = db.load_collector_health(_pm_db(tmp_path / "ok.sqlite"), NOW, PM)
    assert h["error"] is None and [r["status"] for r in h["rows"]] == ["ok"]
    r = h["rows"][0]
    assert (r["present"], r["expected"], r["gaps"]) == (167, 167, 0)  # 26.10 09:00 .. 02.11 07:00 UTC, both included
    sec = _section(_page(h))
    assert ICON in sec and "Усі серії в нормі (1 з 1)" in sec and "<details><summary>" in sec  # nothing to see: folded
    assert "<td>pm_gap</td>" in sec and "100 %" in sec and "167/167" in sec and "02.11 07:01 UTC" in sec


def test_a_hole_below_90_percent_is_flagged_and_open(tmp_path):
    h = db.load_collector_health(_pm_db(tmp_path / "gap.sqlite", range(30, 50)), NOW, PM)  # 20 h missing two days ago
    r = h["rows"][0]
    assert r["status"] == "low" and (r["present"], r["gaps"], r["longest_gap_s"]) == (147, 1, 20 * HOUR)
    sec = _section(_page(h))
    assert "Потребують уваги: 1 з 1 серій" in sec and "<details open>" in sec
    assert "pm_gap <b class='bad'>· нижче 90 %</b>" in sec and "<span class='bad'>88 %</span>" in sec  # 147/167
    assert "147/167" in sec
    assert "1, найдовша 20 год з 31.10 06:00 UTC" in sec


def test_a_fresh_gap_is_marked_even_above_90_percent(tmp_path):
    h = db.load_collector_health(_pm_db(tmp_path / "stale.sqlite", range(0, 4)), NOW, PM)
    assert h["rows"][0]["status"] == "stale"
    sec = _section(_page(h))
    assert "· свіжа дірка" in sec and "зараз немає 4 год" in sec and "<details open>" in sec


def test_empty_db_missing_table_and_missing_file(tmp_path):
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).execute("CREATE TABLE pm_gap_snapshot (ts INTEGER)").connection.close()
    h = db.load_collector_health(empty, NOW, PM)
    assert [r["status"] for r in h["rows"]] == ["never"]
    sec = _section(_page(h))
    assert "· жодного запису" in sec and "Потребують уваги: 1 з 1" in sec

    bare = tmp_path / "bare.sqlite"
    sqlite3.connect(bare).close()
    h = db.load_collector_health(bare, NOW, PM)
    assert h["rows"][0]["status"] == "error"
    assert "· не перевірено" in _section(_page(h)) and "запит не вдався: OperationalError" in _section(_page(h))

    h = db.load_collector_health(tmp_path / "none.sqlite", NOW, PM)
    assert h["rows"] == [] and h["error"] == "БД не відкрито (FileNotFoundError)"
    assert "Не перевірено: БД не відкрито (FileNotFoundError)." in _section(_page(h))
    assert not (tmp_path / "none.sqlite").exists()  # read-only: the missing DB is not created


def test_no_switch_on_never_checked_and_escaping():
    assert "не ввімкнено жодного колектора" in _section(_page({"rows": [], "error": None, "window_days": 7, "at": NOW}))
    assert "Ще не перевірялось" in _section(_page(None))
    spec = ch.SPECS[0]
    evil = ch.error_health(spec, "<b>x</b>", "<script>alert(1)</script>")
    page = _page({"rows": [evil], "error": "", "window_days": 7, "at": NOW})
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "pm_gap:&lt;b&gt;x" in page
    assert "Не перевірено: &lt;script&gt;." in _section(_page({"rows": [], "error": "<script>", "at": NOW}))


def test_real_features_env_never_raises_on_an_empty_db(tmp_path):
    bare = tmp_path / "bare.sqlite"
    sqlite3.connect(bare).close()
    h = db.load_collector_health(bare, NOW)  # deploy/features.env of the repo
    expected = sum(len(s.groups) for s in ch.enabled_specs(db._health_module().load_switches()))
    assert h["error"] is None and len(h["rows"]) == expected > 0
    assert all(r["status"] == "error" for r in h["rows"])


def test_cache_recomputes_at_most_every_ten_minutes(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "load_collector_health", lambda p, now: calls.append(now) or {"rows": [], "error": None, "at": now})
    monkeypatch.setattr(db, "_health_cache", {})
    db.collector_health_cached(1000.0)
    db.collector_health_cached(1000.0 + db.HEALTH_EVERY_S - 1)
    db.collector_health_cached(1000.0 + db.HEALTH_EVERY_S)
    assert calls == [1000.0, 1000.0 + db.HEALTH_EVERY_S]
