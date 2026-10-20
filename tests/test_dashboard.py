"""scripts/dashboard.py: the project dashboard (pure helpers and the page)."""

import sqlite3
import sys
from datetime import datetime
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
        "progress": {
            "done": 30,
            "total": 90,
            "pct": 33.3,
            "groups": [{"label": "Фінал: прогін і обгортка", "done": 0, "total": 5}],
            "cards_left": 40,
            "pace": 8.0,
            "eta_days": 5.0,
        },
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
        "Прогрес проєкту",
        "Фінал: прогін і обгортка",
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
    assert "href='/brief'" in page  # the «Бриф» tab


def test_read_usage_prefers_the_agents_cache_and_throttles_own_calls(tmp_path, monkeypatch):
    import json as _json
    import time as _time

    cache = tmp_path / "usage.json"
    cache.write_text(_json.dumps({"five_hour": {"utilization": 12.0, "resets_at": "x"}, "seven_day": {"utilization": 65.0}}))
    monkeypatch.setattr(db, "USAGE_CACHE", cache)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/.credentials.json").write_text(_json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
    monkeypatch.setattr(db, "HOME", tmp_path)
    calls = []
    monkeypatch.setattr(db.urllib.request, "urlopen", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError("429")))
    now = _time.time()
    assert db.read_usage(now)["seven_day"]["pct"] == 65.0 and calls == []  # fresh cache: no call
    old = now + 1500  # cache 25 min old: one own call (fails), then the cache is still used under an hour
    db._usage_last_call[0] = 0.0
    assert db.read_usage(old)["five_hour"]["pct"] == 12.0 and calls == [1]
    assert db.read_usage(old + 60)["five_hour"]["pct"] == 12.0 and calls == [1]  # throttled: no second call


def test_project_progress_counts_owner_card_steps_and_milestones():
    cards = [
        {"owner": True, "status": "testing", "steps_done": 2, "steps_total": 5},
        {"owner": True, "status": "rejected", "steps_done": 0, "steps_total": 0},
        {"owner": True, "status": "accepted", "steps_done": 0, "steps_total": 0},
        {"owner": False, "status": "accepted", "steps_done": 0, "steps_total": 9},
    ]
    md = "# P\n\n## Основа\n- [x] a\n- [x] b\n\n## Фінал\n- [ ] c\n- [ ] d\n"
    p = db.project_progress(cards, md, pace=2.0)
    assert p["groups"][0]["done"] == 3 and p["groups"][0]["total"] == 7
    assert (p["done"], p["total"]) == (5, 11) and round(p["pct"], 1) == 45.5
    assert p["cards_left"] == 4 and p["eta_days"] == 2.0
    assert db.project_progress(cards, "", pace=0.0)["eta_days"] is None


def test_delivered_pace_counts_recent_deliveries_only():
    now = datetime(2026, 10, 17, 12, 0).timestamp()
    lines = ["2026-10-16_100000\tdelivered\tx", "2026-10-15_100000\tcontinues\ty", "2026-10-01_100000\tdelivered\tz"]
    assert db.delivered_pace(lines, now, days=7) == 1 / 7


# ---------------------------------------------------------------- «Бриф» tab (assistant snapshot)

SNAP_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "assistant_snapshot_2026-10-20.json"


def _snap() -> tuple[dict, float]:
    snap, err = db.load_snapshot(SNAP_FIXTURE)
    assert err is None
    return snap, db.parse_utc(snap["asof_utc"]).timestamp()


def test_brief_renders_the_real_snapshot():
    snap, asof = _snap()
    page = db.render_brief(snap, None, asof + 300)
    for text in (
        "Головне сьогодні",
        "BTC за 120 днів і що закладають опціони",
        "Сценарії на 27.11",
        "Волатильність",
        "Події · час Київ",
        "Funding · річних",
        "Страх і жадібність",
        "16-й перцентиль за рік",
        "5 хв тому",
        "ризик-нейтральні",
        "vrp-real-marks",
        "Не інвестиційна порада",
        "Oct 2026 FOMC",
        "Hyperliquid",
        "беззбиток стреддлу до 23.10",
        "aria-current=page>Бриф",
    ):
        assert text in page, text
    assert page.count("<svg") == 2 and "старший за 30 хв" not in page and "Блок не вдалося" not in page
    if db.KYIV is not None:
        assert "28.10 20:00" in page  # 18:00 UTC FOMC in Kyiv summer time
    assert "<script" not in page and "googleapis" not in page  # no JS needed, no CDN


def test_range_probabilities_match_the_analytics_reference():
    from tradedesk.analytics.options import Smile
    from tradedesk.analytics.probability import prob_above

    snap, _ = _snap()
    F, T, pts = db.chain_smile(snap["sections"]["chain"]["data"])
    smile = Smile(F, T, [k for k, _ in pts], [v for _, v in pts])
    for K in [60000.0, 70000.0, 80000.0, F, 90000.0, 110000.0]:
        assert db.prob_above(pts, F, T, K) == pytest.approx(prob_above(smile, K), abs=1e-9)
    cuts = db.scenario_cuts(F)
    assert cuts == [70000.0, 75000.0, 80000.0, 85000.0, 90000.0, 95000.0]
    bands = db.range_probabilities(pts, F, T, cuts)
    assert len(bands) == 7 and all(b["p"] >= 0 for b in bands)
    assert sum(b["p"] for b in bands) == pytest.approx(1.0)
    assert bands[3]["lo"] == 80000.0 and bands[3]["p"] == max(b["p"] for b in bands)  # the forward's band is the likeliest


def test_brief_missing_and_corrupt_snapshot(tmp_path, monkeypatch):
    snap, err = db.load_snapshot(tmp_path / "none.json")
    assert (snap, err) == (None, "missing")
    monkeypatch.setattr(db, "SNAPSHOT_PATH", tmp_path / "none.json")
    page = db.brief_page(now=1_800_000_000.0)
    assert "Знімка асистента ще немає" in page and "assistant_snapshot" in page and "Не інвестиційна порада" in page
    for raw in ("{bad", "[1, 2]", '{"sections": []}', ""):
        p = tmp_path / "snap.json"
        p.write_text(raw, encoding="utf-8")
        assert db.load_snapshot(p) == (None, "corrupt")
        monkeypatch.setattr(db, "SNAPSHOT_PATH", p)
        assert "пошкоджений" in db.brief_page(now=1_800_000_000.0)


def test_brief_warns_on_stale_snapshot_and_carried_sections():
    snap, asof = _snap()
    snap["sections"]["chain"]["stale"] = True
    snap["sections"]["chain"]["asof_utc"] = "2026-10-20T18:00:00Z"
    snap["stale"] = ["chain"]
    snap["sections"]["ohlc"] = {
        "ok": False,
        "data": None,
        "missing": [{"reason": "error", "detail": "market_ohlcv: timeout"}],
        "warnings": [],
    }
    snap["missing"] = ["ohlc"]
    page = db.render_brief(snap, None, asof + 2 * 3600)
    assert "старший за 30 хв" in page and "2 год тому" in page
    assert "з попереднього знімка: ланцюжок опціонів" in page and "без даних: свічки BTC" in page
    assert "Свічок BTC у знімку немає" in page and "market_ohlcv: timeout" in page
    assert "Сценарії на 27.11" in page and "Блок не вдалося" not in page  # the carried chain still renders


def test_brief_escapes_hostile_strings_from_the_snapshot():
    snap, asof = _snap()
    s = snap["sections"]
    evil = "<script>alert(1)</script>"
    s["events"]["data"]["events"][0]["title"] = evil
    s["events"]["data"]["next_event"]["title"] = evil
    s["events"]["warnings"] = ["<img src=x onerror=alert(2)>"]
    s["tickers"]["data"]["tickers"][0]["symbol"] = "<img src=x onerror=alert(3)>/USDT:USDT"
    s["funding_btc"]["data"]["venues"][0]["venue"] = "<b>evil</b>"
    s["skew"]["data"]["expiries"][2]["expiry"] = "<i>x</i>"
    s["vrp"]["warnings"] = [evil]
    s["term"]["data"]["shape"] = "<svg onload=alert(4)>"
    s["fng"]["data"]["latest"]["zone"] = "<u>z</u>"
    s["ohlc"] = {"ok": False, "data": None, "missing": [{"detail": "<iframe src=//x.example>"}], "warnings": []}
    snap["missing"] = ["<svg onload=alert(5)>"]
    snap["schema"] = "<b>2</b>"
    page = db.render_brief(snap, None, asof + 60)
    for raw in ("<script", "<img", "<iframe", "<b>evil", "<i>x", "<u>z", "<svg onload", "<b>2"):
        assert raw not in page, raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;iframe" in page and "&lt;b&gt;evil" in page


def test_brief_survives_malformed_sections():
    snap, asof = _snap()
    s = snap["sections"]
    s["tickers"]["data"] = {"tickers": "x"}
    s["ohlc"]["data"]["rows"] = [[1, "a"], None, "z"]
    s["chain"]["data"]["strikes"] = [None, 1, {"strike": "x"}, {"strike": 80000.0, "put": {"iv": float("nan")}}]
    s["term"]["data"]["expiries"] = [{"days": "a"}, 3]
    s["events"]["data"] = {"events": [1, None, {"title": 5}], "next_event": "soon"}
    s["funding_btc"]["data"]["venues"] = 5
    s["vrp"]["data"]["current"] = []
    s["fng"]["data"]["latest"] = "x"
    s["skew"] = "broken"
    snap["expiries"] = None
    snap["asof_utc"] = "yesterday"
    page = db.render_brief(snap, None, asof)
    assert "Час знімка невідомий" in page
    assert "Свічок BTC у знімку немає" in page and "Ланцюжка опціонів у знімку немає" in page
    assert "Funding · річних" in page and "Hyperliquid" in page  # ETH funding is intact
    assert "Сценарії на місячну експірацію" in page  # no expiry label without its data


def test_brief_no_expiry_label_when_the_chain_was_dropped():
    snap, asof = _snap()
    snap["sections"]["chain"] = {"ok": False, "data": None, "missing": [{"detail": "too old"}], "warnings": []}
    page = db.render_brief(snap, None, asof)  # `expiries.month` still says 27NOV26
    assert "Сценарії на місячну експірацію" in page and "Сценарії на 27.11" not in page


def test_expiry_utc_parses_deribit_tokens():
    assert db.expiry_utc("27NOV26").isoformat() == "2026-11-27T08:00:00+00:00"
    assert db.expiry_utc("6NOV26").day == 6
    assert db.expiry_utc("31FEB26") is None and db.expiry_utc("bad") is None and db.expiry_utc(None) is None
