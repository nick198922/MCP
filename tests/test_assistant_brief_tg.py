"""The assistant's morning brief for Telegram (scripts/assistant_brief_tg.py) and tg_send.send_photo; no network."""

import io
import json
import re
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import assistant_brief_tg as ab  # noqa: E402
import dashboard as db  # noqa: E402
import tg_send  # noqa: E402

SNAP_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "assistant_snapshot_2026-10-20.json"


def _snap() -> tuple[dict, float]:
    snap, err = db.load_snapshot(SNAP_FIXTURE)
    assert err is None
    return snap, db.parse_utc(snap["asof_utc"]).timestamp()


@pytest.fixture(autouse=True)
def _no_dashboard_url(tmp_path, monkeypatch):
    """The server's real URL file (step 23) must not change these tests."""
    monkeypatch.setattr(ab, "DASHBOARD_URL_PATH", tmp_path / "no_dashboard_url")


def test_brief_on_the_real_snapshot():
    snap, asof = _snap()
    text = ab.build_brief(snap, asof + 300)
    for part in (
        "<b>Бриф асистента",
        "BTC <b>82 791</b> (+0.94 % за 24 год)",
        "ETH <b>2 448</b>",
        "SOL <b>106.69</b> (+2.01 % за 24 год)",
        "Страх і жадібність 55 (жадібність)",
        "DVOL 37.0 — 16-й перцентиль за рік; IV − RV30 +5.2 вп",
        "<b>Головне сьогодні</b>",
        "Опціони закладають ±2.0 % до 23.10 (беззбиток тижневого стреддлу 81.1k – 84.4k)",
        "±11.5 % (1σ) до 27.11: 74.2k – 93.3k",
        "Oct 2026 FOMC",
        "час орієнтовний",
        "BTC: Hyperliquid +10.9 %, Binance +10.8 %",
        "5 хв тому",
        "ризик-нейтральні",
        "«гіпотеза»",
        "(H1) 18.10 відхилено",
        "Не інвестиційна порада",
    ):
        assert part in text, part
    assert "старший за 30 хв" not in text and "не вдалося" not in text
    if db.KYIV is not None:
        assert "28.10 20:00 — Oct 2026 FOMC (високий" in text  # 18:00 UTC in Kyiv summer time
        assert "Знімок 21.10 00:01 за Києвом" in text
    assert len(text) < tg_send.TEXT_LIMIT
    assert text == ab.build_brief(snap, asof + 300)  # pure: same input, same text


def test_brief_warns_when_the_snapshot_is_old():
    snap, asof = _snap()
    snap["missing"] = ["ohlc"]
    snap["stale"] = ["chain"]
    text = ab.build_brief(snap, asof + 2 * 3600)
    assert "<b>Увага: знімок старший за 30 хв</b>" in text and "2 год тому" in text
    assert "Без даних: свічки BTC" in text and "З попереднього знімка: ланцюжок опціонів" in text
    assert "Не інвестиційна порада" in text
    snap["asof_utc"] = None
    assert "час знімка невідомий" in ab.build_brief(snap, asof)


def test_brief_older_than_six_hours_shows_only_the_stamp():
    snap, asof = _snap()
    text = ab.build_brief(snap, asof + 7 * 3600)
    assert "<b>Увага: знімок старший за 6 год</b>" in text and "7 год тому" in text
    assert "старший за 30 хв" not in text
    for gone in ("<b>Ринок</b>", "<b>Головне сьогодні</b>", "<b>Події", "BTC <b>", "FOMC", "через "):
        assert gone not in text, gone
    assert "ризик-нейтральні" in text and "«гіпотеза»" in text and "Не інвестиційна порада" in text
    assert "<b>Ринок</b>" in ab.build_brief(snap, asof + 5 * 3600)  # under 6 h: the full brief with the warning


def test_brief_counts_down_from_now_and_drops_past_events():
    snap, asof = _snap()
    ev = snap["sections"]["events"]["data"]
    fomc = dict(ev["events"][0])
    past = {**fomc, "ts_utc": "2026-10-20T12:00:00+00:00", "title": "Past CPI", "hours_ahead": 3.0}
    ev["events"] = [past, fomc]
    # 28.10 18:00 UTC is ~189 h after the snapshot's own hours_ahead; 5 days later ~69 h (2.9 days) remain. The stamp
    # is moved too, so the file is fresh and only the stored countdowns are old.
    later = asof + 5 * 86400
    snap["asof_utc"] = datetime.fromtimestamp(later - 300, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017
    text = ab.build_brief(snap, later)
    assert "Past CPI" not in text
    assert "через 3 дн" in text and "через 8 дн" not in text
    ev["next_event"] = past
    text = ab.build_brief(snap, asof + 60)
    assert "Past CPI" not in text and "Подій з високим впливом" not in text  # a past next_event is not shown
    near = {**fomc, "ts_utc": "2026-10-22T09:00:00+00:00", "title": "Near FOMC"}
    ev["next_event"], ev["high_impact_within_48h"] = near, False  # the snapshot's flag aged; now it is < 48 h
    text = ab.build_brief(snap, asof + 60)
    assert "Near FOMC" in text and "менше 48 год" in text


def test_brief_without_tzdata_does_not_claim_kyiv_time(monkeypatch):
    snap, asof = _snap()
    monkeypatch.setattr(db, "KYIV", None)
    text = ab.build_brief(snap, asof + 300)
    assert "за Києвом" not in text and "UTC" in text and "<b>Події · час UTC</b>" in text


def test_long_brief_keeps_the_caveats():
    snap, asof = _snap()
    ev = snap["sections"]["events"]["data"]
    ev["events"] = [{**ev["events"][0], "title": "E" * 100 + str(i)} for i in range(5)]
    snap["missing"] = [f"section_{i}_" + "m" * 20 for i in range(120)]  # one very long line
    snap["stale"] = [f"section_{i}_" + "s" * 20 for i in range(120)]
    for venue in snap["sections"]["funding_btc"]["data"]["venues"]:
        venue["venue"] = "V" * 40
    text = ab.build_brief(snap, asof + 60)
    assert len(text) <= tg_send.TEXT_LIMIT and tg_send.clip(text) == text  # nothing left for send() to cut
    assert text.endswith(ab._h(ab.CAVEATS[-1]))
    assert all(ab._h(c) in text for c in ab.CAVEATS)
    assert "…" not in ab.build_brief(_snap()[0], asof + 60).splitlines()  # a normal brief is not cut


def test_brief_without_a_snapshot(tmp_path):
    snap, err = db.load_snapshot(tmp_path / "none.json")
    text = ab.build_brief(snap, 1_800_000_000.0, err)
    assert "Брифу сьогодні немає" in text and "assistant_snapshot" in text
    assert "«гіпотеза»" in text and "ризик-нейтральні" in text and "Не інвестиційна порада" in text
    bad = tmp_path / "bad.json"
    bad.write_text("{bad", encoding="utf-8")
    snap, err = db.load_snapshot(bad)
    assert "пошкоджений" in ab.build_brief(snap, 1_800_000_000.0, err)
    assert "&lt;x&gt;" in ab.build_brief(None, 1_800_000_000.0, "<x>")


def test_brief_escapes_hostile_strings():
    snap, asof = _snap()
    s = snap["sections"]
    evil = "<a href='https://evil.example'>x</a>"
    s["events"]["data"]["events"][0]["title"] = evil
    s["events"]["data"]["next_event"]["title"] = evil
    s["events"]["data"]["events"][0]["impact"] = "<b>hi</b>"
    s["funding_btc"]["data"]["venues"][0]["venue"] = "<i>evil</i>"
    db._skew_ref(snap)["expiry"] = "<q>x</q>"  # the row the brief actually quotes (expiry cut to 12 chars)
    s["fng"]["data"]["latest"]["zone"] = "<u>z</u>"
    snap["missing"] = ["<s>m</s>"]
    text = ab.build_brief(snap, asof + 60)
    for raw in ("<a ", "<b>hi", "<i>evil", "<q>x", "<u>z", "<s>m"):
        assert raw not in text, raw
    for escaped in (
        "&lt;a href='https://evil.example'&gt;x&lt;/a&gt;",
        "&lt;b&gt;hi&lt;/b&gt;",
        "&lt;i&gt;evil&lt;/i&gt;",
        "&lt;q&gt;x&lt;/q&gt;",
        "&lt;u&gt;z&lt;/u&gt;",
        "&lt;s&gt;m&lt;/s&gt;",
    ):
        assert escaped in text, escaped  # each hostile string did reach the output, escaped


def test_brief_survives_malformed_sections():
    snap, asof = _snap()
    s = snap["sections"]
    s["tickers"]["data"]["tickers"] = [{"symbol": "BTC/USDT", "last": "x"}, 5, None]
    s["fng"]["data"]["latest"] = [1]
    s["vrp"]["data"]["current"] = "odd"
    s["events"]["data"]["events"] = [{"ts_utc": "never"}, "x"]
    s["events"]["data"]["next_event"] = {"title": None}
    s["funding_eth"] = {"ok": False, "data": None, "missing": [], "warnings": []}
    s["skew"]["data"]["expiries"] = [{"rr": "bad"}]
    del s["em_week"]
    text = ab.build_brief(snap, asof + 60)
    assert "<b>Головне сьогодні</b>" in text and "Не інвестиційна порада" in text
    assert "ETH:" not in text and "BTC: Hyperliquid" in text


def test_chart_is_skipped_without_matplotlib(monkeypatch):
    snap, _ = _snap()
    monkeypatch.setitem(sys.modules, "matplotlib", None)  # import -> ImportError
    assert ab.chart_png(snap) is None
    assert ab.chart_png(None) is None


def test_chart_png_when_matplotlib_is_there():
    pytest.importorskip("matplotlib")
    snap, _ = _snap()
    png = ab.chart_png(snap)
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_main_preview_sends_nothing(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("must not send")

    monkeypatch.setattr(tg_send, "send", boom)
    monkeypatch.setattr(tg_send, "send_photo", boom)
    snap, _ = _snap()
    asof = ab.db.parse_utc(snap["asof_utc"]).timestamp()
    monkeypatch.setattr(ab.time, "time", lambda: asof + 300)  # the fixture must stay fresh, whatever today's date
    assert ab.main(["--snapshot", str(SNAP_FIXTURE), "--print", "--no-chart"]) == 0
    assert "<b>Головне сьогодні</b>" in capsys.readouterr().out
    assert ab.main(["--snapshot", str(SNAP_FIXTURE), "--out", str(tmp_path / "o"), "--no-chart"]) == 0
    assert "Бриф асистента" in (tmp_path / "o" / "brief.html").read_text(encoding="utf-8")


def test_main_sends_text_and_reports_a_failed_send(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or True)
    monkeypatch.setattr(tg_send, "send_photo", lambda *a, **k: sent.append(("photo", k)) or True)
    monkeypatch.setattr(ab, "chart_png", lambda snap: b"\x89PNG fake")
    assert ab.main(["--snapshot", str(SNAP_FIXTURE)]) == 0
    assert sent[0][1]["topic"] == "alerts" and "Бриф асистента" in sent[0][0] and sent[1][0] == "photo"
    sent.clear()
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append(text) or False)  # Telegram down / not configured
    assert ab.main(["--snapshot", str(tmp_path / "none.json")]) == 1
    assert len(sent) == 1 and "Брифу сьогодні немає" in sent[0]  # no photo after a failed text


def test_send_photo_builds_multipart_and_never_needs_a_token_here(monkeypatch):
    assert tg_send.send_photo(b"png", "x", env={}) is False
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps({"ok": True}).encode()

    def fake_urlopen(req, timeout):
        seen["url"], seen["body"], seen["ct"] = req.full_url, req.data, req.headers["Content-type"]
        return Resp()

    monkeypatch.setattr(tg_send.urllib.request, "urlopen", fake_urlopen)
    env = {"TELEGRAM_BOT_TOKEN": "1:a", "TELEGRAM_CHAT_ID": "-100", "TELEGRAM_TOPIC_ALERTS": "7"}
    assert tg_send.send_photo(b"\x89PNGdata", "<b>cap</b>", topic="alerts", env=env) is True
    assert seen["url"].endswith("/sendPhoto") and seen["ct"].startswith("multipart/form-data; boundary=")
    body = seen["body"]
    assert b'name="chat_id"\r\n\r\n-100' in body and b'name="message_thread_id"\r\n\r\n7' in body
    assert b"\x89PNGdata" in body and b'filename="chart.png"' in body and b"<b>cap</b>" in body


def _http_error(req, description: str) -> urllib.error.HTTPError:
    body = io.BytesIO(json.dumps({"ok": False, "description": description}).encode())
    return urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, body)


ENV = {"TELEGRAM_BOT_TOKEN": "1:a", "TELEGRAM_CHAT_ID": "-100", "TELEGRAM_TOPIC_ALERTS": "7"}


def test_send_photo_retries_without_a_gone_topic(monkeypatch, capsys):
    bodies = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps({"ok": True}).encode()

    def fake_urlopen(req, timeout):
        bodies.append(req.data)
        if len(bodies) == 1:
            raise _http_error(req, "Bad Request: message thread not found")
        return Resp()

    monkeypatch.setattr(tg_send.urllib.request, "urlopen", fake_urlopen)
    assert tg_send.send_photo(b"png", "cap", topic="alerts", env=ENV) is True
    assert len(bodies) == 2
    assert b'name="message_thread_id"' in bodies[0] and b'name="message_thread_id"' not in bodies[1]
    assert "1:a" not in capsys.readouterr().err


def test_send_photo_failure_never_prints_the_token(monkeypatch, capsys):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        raise _http_error(req, "Bad Request: wrong file")

    monkeypatch.setattr(tg_send.urllib.request, "urlopen", fake_urlopen)
    assert tg_send.send_photo(b"png", "cap", topic="alerts", env=ENV) is False
    assert len(calls) == 1 and "1:a" in calls[0]  # the token is only in the request URL
    err = capsys.readouterr().err
    assert "wrong file" in err and "1:a" not in err and "api.telegram.org" not in err


def test_flag_path_matches_between_the_unit_and_step_22():
    root = Path(__file__).resolve().parent.parent
    unit = (root / "deploy" / "user-units" / "tradedesk-assistant-brief.service").read_text(encoding="utf-8")
    step = (root / "scripts" / "server" / "22-assistant-brief-timer.sh").read_text(encoding="utf-8")
    cond = re.search(r"^ConditionPathExists=(\S+)$", unit, re.M).group(1)
    d = re.search(r"^DIR=(\S+)$", step, re.M).group(1)
    flag = re.search(r'^FLAG="\$DIR/(\S+)"$', step, re.M).group(1)
    assert cond == f"{d}/{flag}"
    assert "runuser" not in step and "sudo bash" not in step and '"$(id -un)" == "$SVC"' in step  # runs as tradedesk


def test_short_brief_with_dashboard_buttons(tmp_path, monkeypatch):
    snap, _ = _snap()
    now = ab.db.parse_utc(snap["asof_utc"]).timestamp() + 300
    full, short = ab.build_brief(snap, now), ab.build_brief(snap, now, short=True)
    assert "<b>Події" in full and "<b>Funding" in full
    assert "<b>Події" not in short and "<b>Funding" not in short and "Додатний funding" not in short
    assert "<b>Головне сьогодні</b>" in short and all(ab._h(c) in short for c in ab.CAVEATS)
    url = tmp_path / "dashboard_url"
    assert ab.dashboard_buttons(url) == []  # no step 23 yet
    for bad in ("http://tradedesk.example.ts.net/", "https://a b/", ""):
        url.write_text(bad, encoding="utf-8")
        assert ab.dashboard_buttons(url) == []
    url.write_text("https://tradedesk.tail1234.ts.net\n", encoding="utf-8")
    assert ab.dashboard_buttons(url)[1] == ("Радар", "https://tradedesk.tail1234.ts.net/radar")
    sent = []
    monkeypatch.setattr(ab, "DASHBOARD_URL_PATH", url)
    monkeypatch.setattr(ab.time, "time", lambda: now)
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or True)
    assert ab.main(["--snapshot", str(SNAP_FIXTURE), "--no-chart"]) == 0
    text, kw = sent[0]
    assert "<b>Події" not in text and len(kw["buttons"]) == len(ab.PAGES)
    assert tg_send.payload("1", text, buttons=kw["buttons"])["reply_markup"]["inline_keyboard"][0][0]["text"] == "Бриф"
