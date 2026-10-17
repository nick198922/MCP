"""scripts/dashboard.py: the project dashboard (pure helpers and the page)."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402

H = 3600.0


def test_usage_eta_from_samples_after_the_last_reset():
    now = 100 * 86400.0
    samples = [(now - 30 * H, 90.0), (now - 26 * H, 3.0), (now - 20 * H, 10.0), (now - 2 * H, 28.0), (now, 30.0)]
    t = db.usage_eta(samples, now, 97.0)
    # within the last 24 h: 10 % at -20 h, 30 % now -> 24 % a day; 67 points left -> 67 h
    assert t["rate_per_day"] == pytest.approx(24.0)
    assert t["eta"] == pytest.approx(now + 67 / 24 * 86400)
    assert db.usage_eta([(now, 50.0)], now, 97.0) == {"rate_per_day": None, "eta": None}
    assert db.usage_eta([(now - 10 * H, 98.0), (now, 98.5)], now, 97.0)["eta"] is None


def test_history_rows_newest_first_without_markup():
    lines = [
        "2026-10-17_005046\tdelivered\t✅ **Агенти здали крок:** `agent/x`\t⏱ 0 год 49 хв · ліміт",
        "2026-10-17_014721\terror\t❌ збій",
        "broken line",
    ]
    rows = db.history_rows(lines)
    assert [r["status"] for r in rows] == ["error", "delivered"]
    assert rows[1]["when"] == "17.10 00:50" and rows[1]["text"] == "✅ Агенти здали крок: agent/x" and rows[1]["cost"].startswith("⏱")


def test_table_freshness_reads_seconds_millis_and_days(tmp_path):
    p = tmp_path / "t.sqlite"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE funding_hist (ts INTEGER)")
    con.execute("CREATE TABLE ohlcv_hist (ts INTEGER)")
    con.execute("CREATE TABLE trader_snapshot (day TEXT)")
    now = 1_800_000_000.0
    con.execute("INSERT INTO funding_hist VALUES (?)", (int(now - 600),))
    con.execute("INSERT INTO ohlcv_hist VALUES (?)", (int((now - 10 * 86400) * 1000),))
    con.execute("INSERT INTO trader_snapshot VALUES ('2027-01-15')")
    con.commit()
    con.close()
    rows = {r["table"]: r for r in db.table_freshness(p, now)}
    assert rows["funding_hist"]["ok"] is True and rows["funding_hist"]["age_s"] == pytest.approx(600)
    assert rows["ohlcv_hist"]["ok"] is False and rows["funding_hist"]["rows"] == 1
    assert "trader_snapshot" in rows and "basis_hist" not in rows
    assert rows["trader_snapshot"]["age_s"] == pytest.approx(8 * H)  # 2027-01-15 08:00 UTC; a day row counts from midnight
    assert db.table_freshness(tmp_path / "none.sqlite", now) == []


def test_render_has_all_sections_and_escapes():
    snap = {
        "generated_at": 1_800_000_000.0,
        "agents": {
            "line": "🟢 Працюють над кроком з 10:10",
            "activity": "квант (10:47) працює: «<script>»",
            "work": ("agent/2026-10-17-x", 2),
            "parked": [("agent/2026-10-17-fx", "17.10 20:30")],
            "awaiting_review": ["agent/2026-10-17-y"],
            "counts_24h": {"delivered": 11, "error": 1},
            "history": [{"when": "17.10 08:31", "status": "delivered", "text": "здано", "cost": "⏱ 1 год"}],
            "progress": ["**10:47 квант:** Running stage 0"],
            "doctor": "",
        },
        "usage": {
            "five_hour": {"pct": 66.0, "resets_at": "2026-10-17T09:10:00+00:00"},
            "seven_day": {"pct": 85.0, "resets_at": "2026-10-20T17:00:00+00:00"},
            "trend": {"rate_per_day": 18.6, "eta": 1_800_100_000.0, "eta_owner_only": None, "limit": 97.0, "owner_only_at": 80.0},
        },
        "cards": [
            {
                "name": "IDEA-20261017-forward-paper-carry",
                "title": "паперовий carry",
                "status": "accepted",
                "verdict": "",
                "owner": True,
                "order": 2,
                "report": "",
            },
            {
                "name": "IDEA-20261016-session-stop-hunt",
                "title": "SSH",
                "status": "rejected",
                "verdict": "−0.214R",
                "owner": True,
                "order": None,
                "report": "",
            },
        ],
        "commits": [["abc1234", "17.10 12:00", "Owner's decisions"]],
        "questions": ["Q-1: ключ?"],
        "services": [{"label": "MCP", "unit": "tradedesk-mcp", "state": "active"}],
        "machine": {
            "mem_total": 12 * 1024**3,
            "mem_used": 1024**3,
            "load": ["0.1", "0.1", "0.1"],
            "uptime_s": 86400.0,
            "disk_used": 22 * 1024**3,
            "disk_total": 193 * 1024**3,
            "db_bytes": 100 * 1024**2,
            "backup_age_s": 3600.0,
        },
        "tables": [{"label": "funding", "table": "funding_hist", "rows": 12345, "age_s": 600.0, "ok": True}],
    }
    page = db.render(snap)
    for text in (
        "Агенти зараз",
        "Ліміт Claude",
        "Черга",
        "Результати перевірок",
        "Кроки агентів",
        "Сервер",
        "Колектор даних",
        "Останні зміни в main",
        "Питання агентів до тебе",
        "паперовий carry",
        "12 345",
        "agent/2026-10-17-y",
    ):
        assert text in page
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "http-equiv='refresh'" in page and 'class="bar"' in page
