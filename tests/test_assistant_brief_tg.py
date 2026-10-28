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
    """The server's real URL file (step 23) or a set $TRADEDESK_PANEL_URL must not change these tests."""
    monkeypatch.setattr(ab, "DASHBOARD_URL_PATH", tmp_path / "no_dashboard_url")
    monkeypatch.delenv(ab.PANEL_URL_ENV, raising=False)


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


def test_one_fomc_listed_as_three_rows_is_one_line():
    """forexfactory, snapshot 25.10: FOMC 28.10 as statement + rate + press conference; brief and panel show one row."""
    snap, asof = _snap()
    ev = snap["sections"]["events"]["data"]
    fomc = dict(ev["events"][0], kind="fomc", impact="high", ts_utc="2026-10-28T18:00:00+00:00")
    ev["events"] = [
        {**fomc, "title": "FOMC Statement"},
        {**fomc, "title": "Federal Funds Rate", "forecast": "4.00%"},
        {**fomc, "title": "FOMC Press Conference", "ts_utc": "2026-10-28T18:30:00+00:00"},
        {**fomc, "title": "FOMC Statement", "ts_utc": "2026-10-29T18:00:00+00:00"},  # another day stays
        {**fomc, "kind": "gdp", "title": "Advance GDP q/q"},  # another kind on the same day stays
    ]
    merged = "FOMC Statement · Federal Funds Rate · FOMC Press Conference"
    assert [x["title"] for x in db.merge_same_day(ev["events"])] == [merged, "FOMC Statement", "Advance GDP q/q"]
    assert ev["events"][0]["title"] == "FOMC Statement"  # the snapshot itself is not changed
    text = ab.build_brief(snap, asof + 60)
    assert text.count("Federal Funds Rate") == 1 and merged in text and "29.10" in text  # next 2 events: 28.10 and 29.10
    assert db.block_events(snap).count("Federal Funds Rate") == 1


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
    out = capsys.readouterr().out
    assert "<b>Головне сьогодні</b>" not in out and ab._h(ab.CAVEAT_SHORT) in out  # the short text by default
    assert "[buttons: none (no $TRADEDESK_PANEL_URL" in out
    assert ab.main(["--snapshot", str(SNAP_FIXTURE), "--print", "--no-chart", "--full"]) == 0
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


def test_short_brief_is_a_few_lines():
    snap, asof = _snap()
    text = ab.build_brief(snap, asof + 300, short=True)
    lines = [ln for ln in text.splitlines() if ln]
    assert len(lines) <= 9 and len(text) < 800, text  # header, 2 market lines, ≤ 3 items, range line, ≤ 2 events, 1 caveat
    assert lines[1] == "BTC <b>82 791</b> +0.94 % · ETH <b>2 448</b> +1.92 % <i>за 24 год</i>"
    assert lines[2] == "DVOL <b>37.0</b> · 16-й перцентиль за рік"
    assert "SOL" not in text and "Страх і жадібність" not in text and "IV − RV30" not in text
    for gone in ("<b>Ринок</b>", "<b>Головне сьогодні</b>", "<b>Події", "<b>Funding", "<b>Застереження</b>", "Знімок "):
        assert gone not in text, gone  # no block titles; a fresh snapshot needs no stamp line
    items = [ln for ln in lines if ln.startswith("• <i>") and "FOMC" not in ln and "ризик-нейтральна" not in ln]
    assert len(items) == 3 and "ризик" in items[0] and "Пути 25Δ дорожчі" in items[0]  # risk first
    assert "(1σ) до" not in text  # the month ±1σ item gives way to the range line (same band)
    assert "Рівень волатильності сам по собі не сигнал" in items[2]
    assert len([ln for ln in lines if "<i>ризик-нейтральна</i>" in ln]) == 1
    ev = [ln for ln in lines if "FOMC" in ln]
    assert len(ev) == 1 and "<i>подія</i>" in ev[0] and "через 8 дн" in ev[0] and "час орієнтовний" in ev[0]
    if db.KYIV is not None:
        assert "28.10 20:00 за Києвом — Oct 2026 FOMC" in ev[0]
    assert lines[-1] == f"<i>{ab._h(ab.CAVEAT_SHORT)}</i>"
    for part in ("ризик-нейтральні", "«гіпотеза»", "Не інвестиційна порада"):
        assert part in lines[-1]
    assert text == ab.build_brief(snap, asof + 300, short=True)  # pure


def test_short_brief_events_and_stale_snapshot():
    snap, asof = _snap()
    ev = snap["sections"]["events"]["data"]
    fomc = ev["events"][0]
    near = {**fomc, "ts_utc": "2026-10-22T09:00:00+00:00", "title": "Near CPI", "source": "feed"}
    past = {**fomc, "ts_utc": "2026-10-20T12:00:00+00:00", "title": "Past PPI"}
    ev["events"] = [past, near, fomc, {**fomc, "title": "Third", "ts_utc": "2026-10-29T18:00:00+00:00"}]
    text = ab.build_brief(snap, asof + 60, short=True)
    assert "Past PPI" not in text and "Third" not in text  # past dropped, at most 2
    line = next(ln for ln in text.splitlines() if "Near CPI" in ln)
    assert "<i>ризик</i>" in line and "менше 48 год" in line and "через 36 год" in line
    ev["events"] = [past]
    assert "Подій з високим впливом на 14 днів немає." in ab.build_brief(snap, asof + 60, short=True)
    ev["events"], ev["next_event"] = [], near  # only next_event: still shown
    assert "Near CPI" in ab.build_brief(snap, asof + 60, short=True)
    snap["missing"] = ["ohlc"]
    text = ab.build_brief(snap, asof + 2 * 3600, short=True)
    assert "<b>Увага: знімок старший за 30 хв</b>" in text and "2 год тому" in text and "Без даних: свічки BTC" in text
    text = ab.build_brief(snap, asof + 7 * 3600, short=True)
    assert "старший за 6 год" in text and "BTC <b>" not in text and "Near CPI" not in text
    assert text.endswith(f"<i>{ab._h(ab.CAVEAT_SHORT)}</i>")
    none = ab.build_brief(None, asof, "missing", short=True)
    assert "Брифу сьогодні немає" in none and none.endswith(f"<i>{ab._h(ab.CAVEAT_SHORT)}</i>")


def test_short_brief_escapes_and_survives_odd_data():
    snap, asof = _snap()
    s = snap["sections"]
    evil = "<a href='https://evil.example'>x</a>"
    s["events"]["data"]["events"][0]["title"] = evil
    db._skew_ref(snap)["expiry"] = "<q>x</q>"
    snap["missing"] = ["<s>m</s>"]
    text = ab.build_brief(snap, asof + 60, short=True)
    for raw in ("<a ", "<q>x", "<s>m"):
        assert raw not in text, raw
    assert "&lt;a href='https://evil.example'&gt;x&lt;/a&gt;" in text and "&lt;q&gt;x&lt;/q&gt;" in text
    s["tickers"]["data"]["tickers"] = [{"symbol": "BTC/USDT", "last": "x"}, 5]
    s["vrp"]["data"]["current"] = "odd"
    s["dvol"] = {"ok": False, "data": None}
    s["events"]["data"]["events"] = [{"ts_utc": "never"}, "x"]
    text = ab.build_brief(snap, asof + 60, short=True)
    assert "BTC <b>" not in text and "DVOL" not in text and "не вдалося" not in text
    assert text.endswith(f"<i>{ab._h(ab.CAVEAT_SHORT)}</i>")


def test_short_brief_has_one_risk_neutral_range_line():
    snap, asof = _snap()
    text = ab.build_brief(snap, asof + 300, short=True)
    rng = [ln for ln in text.splitlines() if "<i>ризик-нейтральна</i>" in ln]
    day = db.kyiv(db.expiry_utc("27NOV26"), "%d.%m")  # '27.11' ('27.11 UTC' without tzdata)
    # month expiry 27NOV26 of the fixture: ±1σ band from em_month, tails from the «Бриф» tab's scenario bands
    assert rng == [f"• <i>ризик-нейтральна</i> · BTC до {day}: ±1σ (±11.5 %, 74.2k – 93.3k) — 71 %; нижче 70k — 8 %; вище 95k — 10 %."]
    F, T, pts = db.chain_smile(snap["sections"]["chain"]["data"])
    bands = db.range_probabilities(pts, F, T, db.scenario_cuts(F))
    assert (bands[0]["hi"], bands[-1]["lo"]) == (70000, 95000)  # the tab's tails: «нижче 70k» 8 %, «вище 95k» 10 %
    assert round(bands[0]["p"] * 100) == 8 and round(bands[-1]["p"] * 100) == 10
    lines = [ln for ln in text.splitlines() if ln]
    i = lines.index(rng[0])
    assert lines[i - 1].startswith("• <i>спокій</i>") and "FOMC" in lines[i + 1]  # after the day's items, before events
    assert len(lines) <= 9  # one line more than the items, one item less (the month ±1σ item)
    for word in ("купуй", "продавай", "рекоменд", "сигнал"):
        assert word not in rng[0].lower(), word  # a market price, not advice
    assert "ризик-нейтральна ·" not in ab.build_brief(snap, asof + 300)  # the long text is unchanged
    assert "±11.5 % (1σ) до" in ab.build_brief(snap, asof + 300)


def test_short_brief_range_line_without_data():
    snap, asof = _snap()
    del snap["sections"]["chain"]
    snap["missing"] = ["chain"]
    text = ab.build_brief(snap, asof + 300, short=True)
    assert "ризик-нейтральна</i>" not in text and "Без даних: ланцюжок опціонів" in text  # said once, in the stamp
    assert "±11.5 % (1σ) до" in text and "не вдалося" not in text  # the month ±1σ item is back
    snap, _ = _snap()
    snap["sections"]["em_month"] = {"ok": False, "data": None}
    snap["missing"] = ["em_month"]
    text = ab.build_brief(snap, asof + 300, short=True)
    assert "ризик-нейтральна</i>" not in text and "Без даних: очікуваний рух (місяць)" in text
    odd = (
        ("em_month", "expiry", "26DEC26"),  # band of another expiry than the chain: no mixed line
        ("em_month", "sigma_lo", None),
        ("em_month", "sigma_hi", 1.0),  # hi below lo
        ("chain", "expiry", "<b>27NOV26</b>"),  # hostile / unparsable expiry: dropped, never printed
        ("chain", "strikes", "x"),
        ("chain", "forward", -1),
    )
    for sec, key, val in odd:
        snap, _ = _snap()
        data = snap["sections"][sec]["data"]
        (data["expiries"][0] if sec == "em_month" else data)[key] = val
        text = ab.build_brief(snap, asof + 300, short=True)
        assert "ризик-нейтральна</i>" not in text and "не вдалося" not in text and "<b>27NOV26" not in text, (sec, key)
        assert text.endswith(f"<i>{ab._h(ab.CAVEAT_SHORT)}</i>")


def test_short_brief_range_line_drops_a_tail_inside_the_band():
    snap, asof = _snap()
    snap["sections"]["em_month"]["data"]["expiries"][0]["sigma_lo"] = 65000.0  # wider than the tab's 70k cut
    rng = [ln for ln in ab.build_brief(snap, asof + 300, short=True).splitlines() if "ризик-нейтральна</i>" in ln]
    assert len(rng) == 1 and "65k – 93.3k" in rng[0] and "нижче" not in rng[0] and "вище 95k — 10 %" in rng[0]


def _with_paper(snap: dict, **data) -> dict:
    """The fixture (20.10, before the section existed) with a `paper_rec` section as collector.assistant_snapshot writes it."""
    base = {"label": "hypothesis", "state": "actual", "action": "hold", "trigger": None, "pick": "short_strangle25"}
    base |= {"asof_utc": "2026-10-20T09:05:00Z", "age_hours": 14.9, "trade_id": None, "standing_trade": 3}
    snap["sections"]["paper_rec"] = {"ok": True, "data": base | data, "missing": [], "warnings": []}
    return snap


def test_short_brief_has_one_paper_recommendation_line():
    snap, asof = _snap()
    plain = [ln for ln in ab.build_brief(snap, asof + 300, short=True).splitlines() if ln]
    text = ab.build_brief(_with_paper(snap), asof + 300, short=True)
    lines = [ln for ln in text.splitlines() if ln]
    assert len(lines) == len(plain) + 1 <= 10, text  # exactly one extra line, the brief stays ~10 lines
    paper = [ln for ln in lines if "Паперова рекомендація" in ln]
    day = db.kyiv(db.parse_utc("2026-10-20T09:05:00Z"), "%d.%m")  # '20.10' ('20.10 UTC' without tzdata)
    assert paper == [
        f"• <i>гіпотеза</i> · Паперова рекомендація актуальна: продаж стренглу 25Δ, ~40 дн — чинна паперова угода (рішення {day})."
    ]
    i = lines.index(paper[0])
    assert "FOMC" in lines[i + 1] and lines[i - 1].startswith("• <i>")  # after the day's items, before the events
    raw = text.splitlines()
    assert raw[raw.index(paper[0]) - 1] != ""  # same group as the items: no blank line before it
    assert lines[-1] == f"<i>{ab._h(ab.CAVEAT_SHORT)}</i>"  # «гіпотеза», H1 rejected, not advice
    cases = {
        ("changed", "hold", None, "long_straddle_14d"): "змінилась: метод тепер обрав би купівля стреддлу ATM, ~14 дн; чинну угоду лишено",
        ("changed", "open", "regime", "iron_condor"): "змінилась: нова паперова угода через зміну режиму — залізний кондор 25/10Δ",
        ("changed", "open", "event", "long_strangle25_14d"): "нова паперова угода перед FOMC / CPI — купівля стренглу 25Δ, ~14 дн",
        ("actual", "open", "chain", "put_debit_spread"): "актуальна: нова паперова угода — пут-дебетовий спред, ~40 дн (рішення",
        ("no_longer_actual", "none", None, None): "більше не актуальна: метод сьогодні не обирає жодної структури",
        ("actual", "hold", None, "new_structure_x"): "актуальна: new_structure_x — чинна паперова угода",
    }
    for (state, action, trigger, pick), part in cases.items():
        out = ab.build_brief(_with_paper(snap, state=state, action=action, trigger=trigger, pick=pick), asof + 300, short=True)
        assert part in out, (state, part)
        assert out.count("Паперова рекомендація") == 1
    full = ab.build_brief(_with_paper(snap), asof + 300)
    assert "Паперова рекомендація актуальна" in full  # the long text has it too, under «Головне сьогодні»


def test_brief_without_paper_recommendation_data_omits_the_line():
    snap, asof = _snap()
    assert "Паперова" not in ab.build_brief(snap, asof + 300, short=True)  # no section at all (old snapshot)
    _with_paper(snap)
    snap["sections"]["paper_rec"] = {"ok": False, "data": None, "missing": [{"detail": "no paper state file yet"}]}
    snap["missing"] = ["paper_rec"]
    text = ab.build_brief(snap, asof + 300, short=True)
    assert "Паперова" not in text and "Без даних: паперова рекомендація" in text  # said once, briefly, in the stamp
    for odd in ({"state": "weird"}, {"state": None}, "x"):
        snap["sections"]["paper_rec"] = {"ok": True, "data": odd}
        out = ab.build_brief(snap, asof + 300, short=True)
        assert "Паперова рекомендація" not in out and "не вдалося" not in out and "<i>гіпотеза</i>" not in out
    _with_paper(snap, asof_utc="bad", pick=5)
    out = ab.build_brief(snap, asof + 300, short=True)
    assert "Паперова рекомендація актуальна (" not in out and "• <i>гіпотеза</i> · Паперова рекомендація актуальна." in out


MY_TICKERS = json.loads((Path(__file__).resolve().parent / "fixtures" / "bybit_option_tickers_btc_2026-10-26.json").read_text())


def _with_my_options(snap: dict, positions: list[dict]) -> dict:
    """The fixture with a `my_options` section built the way the collector builds it (analytics.my_options on the
    positions file; ticker values from the live Bybit sample 26.10, positions at 20.10 21:00 UTC)."""
    from tradedesk.analytics.my_options import my_options_view

    state = {
        "schema": 1, "source": "bybit", "asof_utc": "2026-10-20T20:50:00Z", "status": "ok", "detail": "", "errors": [],
        "positions": positions, "tickers": {t["symbol"]: t for t in MY_TICKERS["list"]},
    }  # fmt: skip
    data, why = my_options_view(state, db.parse_utc(snap["asof_utc"]))
    assert why is None
    snap["sections"]["my_options"] = {"ok": True, "data": data, "missing": [], "warnings": []}
    return snap


def _leg(symbol: str, side: str, size: str, avg: str, upnl: str) -> dict:
    return {"symbol": symbol, "side": side, "size": size, "avgPrice": avg, "markPrice": None, "unrealisedPnl": upnl}


LEGS = [
    _leg("BTC-27NOV26-81000-C-USDT", "Buy", "0.1", "3000", "57.3"),
    _leg("BTC-27NOV26-70000-P-USDT", "Sell", "0.2", "900", "-44.8"),
    _leg("BTC-27NOV26-77000-P-USDT", "Buy", "0.05", "2100", "-8.8"),
    _leg("BTC-27NOV26-90000-C-USDT", "Buy", "0.01", "500", "-1.2"),  # no ticker: no theta, no chance
]


def test_short_brief_shows_my_bybit_options_total_and_at_most_three():
    snap, asof = _snap()
    plain = [ln for ln in ab.build_brief(snap, asof + 300, short=True).splitlines() if ln]
    text = ab.build_brief(_with_my_options(snap, LEGS), asof + 300, short=True)
    lines = [ln for ln in text.splitlines() if ln]
    mine = [ln for ln in lines if "<i>мої опціони</i>" in ln or ln.startswith("  ◦ ")]
    assert len(mine) == 4 and len(lines) == len(plain) + 4, text  # one total line + 3 of the 4 positions
    when = db.kyiv(db.parse_utc("2026-10-20T20:50:00Z"), "%H:%M")
    d = snap["sections"]["my_options"]["data"]
    assert mine[0] == (
        f"• <i>мої опціони</i> · Мої опціони Bybit (4, на {when}): P&amp;L +$2.50, тета {ab._usd(d['theta_usd_day'])}/день; "
        "шанс прибутку — ризик-нейтральний, не порада."
    )
    assert mine[1].startswith("  ◦ BTC-27NOV26-81000-C-USDT лонг 0.1: P&amp;L +$57, тета −$")
    assert mine[2].startswith("  ◦ BTC-27NOV26-70000-P-USDT шорт 0.2: P&amp;L −$45, тета +$")
    assert "90000-C" not in text  # the 4th by |P&L| is left to Bybit
    for ln in mine[1:]:
        assert re.search(r"шанс прибутку \d+ %, \d+\.\d дн$", ln), ln
    assert (ab._usd(0.537), ab._usd(-0.537), ab._usd(9.999), ab._usd(12.4), ab._usd(None)) == ("+$0.54", "−$0.54", "+$10.00", "+$12", "—")
    i = lines.index(mine[0])
    assert "FOMC" in lines[i + 4] and text.splitlines()[text.splitlines().index(mine[0]) - 1] == ""  # own group, before events
    assert lines[-1] == f"<i>{ab._h(ab.CAVEAT_SHORT)}</i>"
    for word in ("купуй", "продавай", "закрий", "рекоменд"):
        assert word not in "\n".join(mine).lower(), word
    assert "<b>Мої опціони Bybit</b>" in ab.build_brief(snap, asof + 300)  # the long text has its own block


def test_brief_without_my_options_data_says_so_once_in_the_stamp():
    snap, asof = _snap()
    assert "мої опціони" not in ab.build_brief(snap, asof + 300, short=True)  # old snapshot without the section
    snap["sections"]["my_options"] = {"ok": False, "data": None, "missing": [{"detail": "my_options: the Bybit request failed"}]}
    snap["missing"] = ["my_options"]
    text = ab.build_brief(snap, asof + 300, short=True)
    assert "<i>мої опціони</i>" not in text and "Без даних: мої опціони Bybit" in text
    snap, _ = _snap()
    text = ab.build_brief(_with_my_options(snap, []), asof + 300, short=True)  # no open positions: nothing to say
    assert "мої опціони" not in text and "Без даних" not in text
    for odd in ({"positions": "x"}, {"positions": [5, None]}, "x", {"positions": [{"size": "a", "days": "b"}]}):
        snap["sections"]["my_options"] = {"ok": True, "data": odd}
        out = ab.build_brief(snap, asof + 300, short=True)
        assert "не вдалося" not in out and out.endswith(f"<i>{ab._h(ab.CAVEAT_SHORT)}</i>")


def test_brief_escapes_my_options_text():
    snap, asof = _snap()
    _with_my_options(snap, LEGS[:1])
    snap["sections"]["my_options"]["data"]["positions"][0]["symbol"] = "<a href='https://evil.example'>x</a>"
    snap["sections"]["my_options"]["data"]["positions"][0]["side"] = "<b>long</b>"
    for short in (True, False):
        text = ab.build_brief(snap, asof + 300, short=short)
        assert "<a " not in text and "<b>long" not in text and "&lt;a href='https://evil.example'&gt;x&lt;/a&gt; ?" in text


def test_brief_escapes_the_paper_recommendation_pick():
    snap, asof = _snap()
    evil = "<a href='https://evil.example'>x</a>"
    for short in (True, False):
        text = ab.build_brief(_with_paper(snap, pick=evil, state="changed", action="hold"), asof + 300, short=short)
        assert "<a " not in text and "&lt;a href='https://evil.example'&gt;x&lt;/a&gt;" in text


def test_panel_url_from_env_or_step_23_file(tmp_path):
    url = tmp_path / "dashboard_url"
    assert ab.dashboard_buttons(url, key_file=tmp_path / "nokey", environ={}) == []  # no step 23, no env
    bad_urls = (
        "http://tradedesk.example.ts.net/",
        "https://a b/",
        "",
        "https://user:pw@tradedesk.ts.net/",
        "https://tradedesk.ts.net/?token=1",
        "https://tradedesk.ts.net/#x",
        "https:///nohost",
        "javascript:alert(1)",
        'https://x.ts.net/"><b>',
    )
    for bad in bad_urls:
        url.write_text(bad, encoding="utf-8")
        assert ab.dashboard_buttons(url, key_file=tmp_path / "nokey", environ={}) == [], bad
        assert ab.dashboard_buttons(tmp_path / "none", key_file=tmp_path / "nokey", environ={ab.PANEL_URL_ENV: bad}) == [], bad
    url.write_text("https://tradedesk.tail1234.ts.net\n", encoding="utf-8")
    buttons = ab.dashboard_buttons(url, key_file=tmp_path / "nokey", environ={})
    assert [b[0] for b in buttons] == ["Бриф", "Опціони", "Радар", "Журнал", "Polymarket"]
    assert buttons[2] == ("Радар", "https://tradedesk.tail1234.ts.net/radar")
    env = {ab.PANEL_URL_ENV: " https://panel.tail9.ts.net/ "}  # the env var wins over the file
    assert ab.dashboard_buttons(url, key_file=tmp_path / "nokey", environ=env)[0] == ("Бриф", "https://panel.tail9.ts.net/brief")
    assert ab.dashboard_buttons(url, key_file=tmp_path / "nokey", environ={ab.PANEL_URL_ENV: ""})[0][1].startswith(
        "https://tradedesk.tail1234"
    )
    assert (
        ab.dashboard_buttons(url, key_file=tmp_path / "nokey", environ={ab.PANEL_URL_ENV: "http://x"}) == []
    )  # set but unusable: no buttons


def test_main_sends_buttons_in_rows(monkeypatch):
    snap, asof = _snap()
    sent = []
    monkeypatch.setenv(ab.PANEL_URL_ENV, "https://tradedesk.tail1234.ts.net/")
    monkeypatch.setattr(ab.db, "KEY_FILE", Path("/nonexistent/dashboard.key"))  # not the machine's real key
    monkeypatch.setattr(ab.time, "time", lambda: asof + 300)
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or True)
    assert ab.main(["--snapshot", str(SNAP_FIXTURE), "--no-chart"]) == 0
    text, kw = sent[0]
    assert ab._h(ab.CAVEAT_SHORT) in text and "<b>Головне сьогодні</b>" not in text
    assert len(kw["buttons"]) == len(ab.PAGES) and kw["per_row"] == ab.BUTTONS_PER_ROW
    kb = tg_send.payload("1", text, buttons=kw["buttons"], per_row=kw["per_row"])["reply_markup"]["inline_keyboard"]
    assert [[b["text"] for b in row] for row in kb] == [["Бриф", "Опціони", "Радар"], ["Журнал", "Polymarket"]]
    assert kb[1][1]["url"] == "https://tradedesk.tail1234.ts.net/polymarket"


def test_main_without_a_panel_url_sends_text_without_buttons(monkeypatch):
    snap, asof = _snap()
    sent = []
    monkeypatch.setattr(ab.time, "time", lambda: asof + 300)
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or True)
    assert ab.main(["--snapshot", str(SNAP_FIXTURE), "--no-chart"]) == 0
    text, kw = sent[0]
    assert kw["buttons"] == [] and ab._h(ab.CAVEAT_SHORT) in text
    assert "reply_markup" not in tg_send.payload("1", text, buttons=kw["buttons"], per_row=kw["per_row"])


def test_payload_button_rows_and_https_only():
    btn = [(f"b{i}", f"https://x/{i}") for i in range(8)] + [("bad", "http://x")]
    assert [len(r) for r in tg_send.payload("1", "t", buttons=btn)["reply_markup"]["inline_keyboard"]] == [1] * 6
    rows = tg_send.payload("1", "t", buttons=btn, per_row=4)["reply_markup"]["inline_keyboard"]
    assert [len(r) for r in rows] == [4, 2] and all(b["url"].startswith("https://") for r in rows for b in r)
    assert tg_send.payload("1", "t", buttons=btn, per_row=0)["reply_markup"]["inline_keyboard"][0] == [{"text": "b0", "url": "https://x/0"}]


def test_dashboard_buttons_carry_the_key(tmp_path):
    url = tmp_path / "dashboard_url"
    url.write_text("https://tradedesk.tail1234.ts.net/\n")
    key = tmp_path / "dashboard.env"
    key.write_text("DASHBOARD_KEY=abcdefghijklmnopqrstuvwxyz\n")
    assert ab.dashboard_buttons(url, environ={}, key_file=key)[0] == (
        "Бриф",
        "https://tradedesk.tail1234.ts.net/brief?k=abcdefghijklmnopqrstuvwxyz",
    )


def _flows_section(**s24) -> dict:
    """A `flows` section in market_flow's shape (numbers from the live call of 28.10, BTC 1h)."""
    summ = {
        "hours": 24,
        "price_change": 0.008683,
        "oi_change": -0.021832,
        "funding_annualized_mean": 0.096295,
        "liq_long": 207_000_000.0,
        "liq_short": 74_000_000.0,
        "premium_adj_mean": -0.000444,
    } | s24
    sentence = (
        "За 24 год: ціна +0.9 %, OI −2.2 % (у монетах); ф'ючерси продають (−175 млн $), спот купує (+175 млн $) → ріст на закритті шортів."
    )
    return {"ok": True, "data": {"summary": {"24h": summ}, "sentence_ua": sentence}, "missing": [], "warnings": []}


def test_brief_flows_block_long_and_short():
    snap, asof = _snap()
    snap["sections"]["flows"] = _flows_section()
    text = ab.build_brief(snap, asof + 300)
    assert "<b>Потоки BTC · описово, не сигнал</b>" in text
    assert "ф'ючерси продають (−175 млн $), спот купує (+175 млн $) → ріст на закритті шортів." in text
    assert "Funding +9.6 % річних (середнє за 24 год); ліквідації за 24 год: лонги 207 млн $, шорти 74 млн $" in text
    assert "Премія Coinbase до медіани 30 дн: −4.4 б.п. (середнє за 24 год) · описово, не сигнал" in text
    short = ab.build_brief(snap, asof + 300, short=True)
    assert "• <i>потоки</i> · За 24 год: ціна +0.9 %" in short and "Премія Coinbase" in short
    assert len(short) < tg_send.TEXT_LIMIT
    # without liquidations (no Coinalyze key) and without the premium: fewer parts, the label stays
    snap["sections"]["flows"] = _flows_section(liq_long=None, premium_adj_mean=None)
    lines = ab._flows(snap, asof)
    assert lines[1] == "Funding +9.6 % річних (середнє за 24 год)" and lines[2].startswith("Описово, не сигнал")


def test_brief_flows_block_absent_or_odd():
    snap, asof = _snap()
    assert "Потоки" not in ab.build_brief(snap, asof + 300)  # the 20.10 snapshot has no flows section
    for odd in ({"ok": True, "data": {"summary": "x"}}, {"ok": True, "data": {"summary": {"24h": {"liq_long": "a"}}}}, {"ok": False}):
        snap["sections"]["flows"] = odd
        assert isinstance(ab._flows(snap, asof), list)
        assert "Не інвестиційна порада" in ab.build_brief(snap, asof + 300)
    snap["sections"]["flows"] = {"ok": True, "data": {"summary": {"24h": {"premium_adj_mean": "<b>"}}, "sentence_ua": "<script>"}}
    assert "<script>" not in ab.build_brief(snap, asof + 300)
