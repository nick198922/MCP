"""The assistant's Sunday radar for Telegram (scripts/radar_weekly_tg.py); no network."""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402
import radar_weekly_tg as rw  # noqa: E402
import tg_send  # noqa: E402

from test_dashboard import _radar_snap  # noqa: E402 - the real 18.10 radar section, built as the collector builds it

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_dashboard_url(tmp_path, monkeypatch):
    """The server's real URL file (step 23), key file or a set $TRADEDESK_PANEL_URL must not change these tests."""
    monkeypatch.setattr(rw.ab, "DASHBOARD_URL_PATH", tmp_path / "no_dashboard_url")
    monkeypatch.setattr(db, "KEY_FILE", tmp_path / "no_dashboard.key")
    monkeypatch.delenv(rw.ab.PANEL_URL_ENV, raising=False)


def _asset(snap: dict, asset: str = "BTC") -> dict:
    return snap["sections"]["radar"]["data"]["assets"][asset]


def test_radar_on_the_real_section_is_short():
    snap, t = _radar_snap()
    text = rw.build_radar(snap, t + 300)
    lines = [ln for ln in text.splitlines() if ln]
    assert len(lines) == 7 and len(text) < 1500, text  # title, five lines, caveat (+ one blank line = 8)
    assert lines[0].startswith("<b>Радар тижня · BTC · ") and lines[-1] == f"<i>{rw._h(rw.CAVEAT)}</i>"
    v = _asset(snap)
    for part in (
        "• <i>ціна</i> · BTC −3.0 % за тиждень: 81 093 проти 83 565 (18.10 проти 11.10, 19:00 UTC).",
        "• <i>вола</i> · IV змінилась по-різному: 7 дн +1.9 вп, 30 дн +1.6 вп, 90 дн −0.3 вп",
        "структура висхідна: 7 днів нижче 30 на 2.9 вп.",
        f"• <i>IV Rank</i> · IV Rank {v['iv_rank']['now']['rank'] * 100:.1f} % (тиждень тому {v['iv_rank']['week']['rank'] * 100:.1f} %)",
        "річного діапазону DVOL 33.6–82.6 % (DVOL 18.10)",
        "• <i>скью</i> · Put skew 2.2 вп → 2.6 вп, RR −2.4 вп → −3.7 вп: захист від падіння подорожчав",
        "• <i>події</i> · Найдорожчий тиждень — 30.10–06.11: форвардна вола 42.1 % проти медіани 36.5 %; "
        "на нього припадає: US midterm elections 2026 (election day) 03.11",
    ):
        assert part in text, part
    # the ATM inputs of the card's «Підсумки 18.10» behind the «вола» line
    assert [round(v["atm"]["now"][k] * 100, 1) for k in ("7", "30", "90", "180")] == [34.1, 37.0, 37.1, 38.2]
    assert [round(v["atm"]["week"][k] * 100, 1) for k in ("7", "30", "90", "180")] == [32.2, 35.5, 37.4, 38.4]
    assert f"180 дн {db._dvp(v['atm']['change']['180'])}" in text
    # the same lines as the dashboard's «Поточна ситуація», escaped; nothing re-implemented
    assert [ln for ln in lines if ln.startswith("• ")] == [f"• <i>{lab}</i> · {rw._h(t)}" for _, lab, t in db.radar_situation(v, "BTC")]
    for part in ("Не порада", "гіпотези", "ризик-нейтральні", "H1 відхилено"):
        assert part in rw.CAVEAT, part
    assert "Увага" not in text and "Архіву поверхні" not in text
    assert text == rw.build_radar(snap, t + 300)  # pure


def test_radar_eth_and_unknown_asset():
    snap, t = _radar_snap()
    text = rw.build_radar(snap, t + 300, asset="eth")
    assert "Радар тижня · ETH" in text and "ETH −6.6 % за тиждень" in text and "2 363" in text
    assert "інверсія фронту: 7 днів дорожчі за 30" in text and "Жоден тиждень до 100 днів не виділяється" in text
    assert "Радар тижня · BTC" in rw.build_radar(snap, t + 300, asset="<x>")  # anything else falls back to BTC


def test_radar_with_null_fields_and_no_archive():
    snap, t = _radar_snap(keep={"2026-10-18"})  # no row a week earlier
    text = rw.build_radar(snap, t + 60)
    assert "BTC 81 093 (18.10, 19:00 UTC); порівняти з тижнем тому нема з чим." in text
    assert "IV змінилась" not in text and "Put skew — → 2.6 вп, RR — → −3.7 вп: порівняти з тижнем тому нема з чим." in text
    empty, t = _radar_snap(keep=set())  # the archive job is off: only DVOL-based fields
    text = rw.build_radar(empty, t + 60)
    items = [ln for ln in text.splitlines() if ln.startswith("• ")]
    assert len(items) == 1 and items[0].startswith("• <i>IV Rank</i>") and "Архіву поверхні ще немає" in text
    a = _asset(empty)
    for k in list(a):
        a[k] = None
    text = rw.build_radar(empty, t + 60)
    assert "Поточну ситуацію скласти нема з чого" in text and "Архіву поверхні ще немає" in text
    assert text.endswith(f"<i>{rw._h(rw.CAVEAT)}</i>")


def test_radar_without_section_asset_or_snapshot(tmp_path):
    snap, t = _radar_snap()
    del snap["sections"]["radar"]["data"]["assets"]["ETH"]
    assert "Даних ETH у секції радару немає." in rw.build_radar(snap, t + 60, asset="ETH")
    del snap["sections"]["radar"]
    text = rw.build_radar(snap, t + 60)
    assert "Радару немає: секції «радар» у знімку ще немає" in text and "• " not in text
    snap, err = db.load_snapshot(tmp_path / "none.json")
    text = rw.build_radar(snap, t, err)
    assert "Радару сьогодні немає" in text and "assistant_snapshot" in text and text.endswith(f"<i>{rw._h(rw.CAVEAT)}</i>")
    bad = tmp_path / "bad.json"
    bad.write_text("{bad", encoding="utf-8")
    snap, err = db.load_snapshot(bad)
    assert "пошкоджений" in rw.build_radar(snap, t, err)
    assert "&lt;x&gt;" in rw.build_radar(None, t, "<x>")


def test_radar_stale_snapshot():
    snap, t = _radar_snap()
    text = rw.build_radar(snap, t + 2 * 3600)
    assert "<b>Увага: знімок старший за 30 хв</b>" in text and "2 год тому" in text and "81 093" in text
    text = rw.build_radar(snap, t + 7 * 3600)
    assert "<b>Увага: знімок старший за 6 год</b>" in text and "7 год тому" in text
    assert "81 093" not in text and "• " not in text and text.endswith(f"<i>{rw._h(rw.CAVEAT)}</i>")
    snap["asof_utc"] = None
    assert "час знімка невідомий" in rw.build_radar(snap, t)


def test_radar_escapes_snapshot_text_and_survives_odd_data(monkeypatch):
    snap, t = _radar_snap()
    a = _asset(snap)
    hot = next(w for w in a["forward_vol"]["windows"] if w["hot"])
    evil = "<a href='https://evil.example'>x</a>"
    hot["events"][0]["title"] = evil
    a["surface"]["now_day"] = "<b>2026-10-18"  # not a date to the formatter: shown as «—», never raw
    text = rw.build_radar(snap, t + 60)
    assert "<a " not in text and "&lt;a href='https://evil.example'&gt;x&lt;/a&gt;" in text
    assert "<b>2026" not in text
    a["price"], a["atm"], a["skew"] = "odd", [1], {"now": 5}
    text = rw.build_radar(snap, t + 60)
    assert "IV Rank" in text and text.endswith(f"<i>{rw._h(rw.CAVEAT)}</i>")

    def boom(*_a, **_k):
        raise TypeError("bad")

    monkeypatch.setattr(db, "radar_situation", boom)
    text = rw.build_radar(snap, t + 60)
    assert "Радар не вдалося зібрати: несподіваний формат даних (TypeError)." in text and "Не порада" in text


def test_radar_buttons(tmp_path):
    url = tmp_path / "dashboard_url"
    assert rw.radar_buttons(url, environ={}, key_file=tmp_path / "nokey") == []  # no step 23, no env
    url.write_text("http://tradedesk.example.ts.net/", encoding="utf-8")
    assert rw.radar_buttons(url, environ={}, key_file=tmp_path / "nokey") == []  # only https
    url.write_text("https://tradedesk.tail1234.ts.net\n", encoding="utf-8")
    assert rw.radar_buttons(url, environ={}, key_file=tmp_path / "nokey") == [
        ("Радар BTC", "https://tradedesk.tail1234.ts.net/radar?asset=BTC"),
        ("Радар ETH", "https://tradedesk.tail1234.ts.net/radar?asset=ETH"),
    ]
    key = tmp_path / "dashboard.env"
    key.write_text("DASHBOARD_KEY=abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
    b = rw.radar_buttons(url, environ={}, key_file=key)
    assert b[1] == ("Радар ETH", "https://tradedesk.tail1234.ts.net/radar?asset=ETH&k=abcdefghijklmnopqrstuvwxyz")
    assert db.parse_radar_query(b[1][1].split("?", 1)[1]) == {"asset": "ETH"}  # the page reads the asset next to k


def test_main_preview_sends_nothing(tmp_path, monkeypatch, capsys):
    snap, t = _radar_snap()
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snap), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("must not send")

    monkeypatch.setattr(tg_send, "send", boom)
    monkeypatch.setattr(rw.time, "time", lambda: t + 300)
    assert rw.main(["--snapshot", str(path), "--print"]) == 0
    out = capsys.readouterr().out
    assert "81 093" in out and "[buttons: none (no $TRADEDESK_PANEL_URL" in out
    assert rw.main(["--snapshot", str(path), "--print", "--asset", "eth"]) == 0
    assert "ETH −6.6 %" in capsys.readouterr().out
    assert rw.main(["--snapshot", str(path), "--out", str(tmp_path / "o")]) == 0
    assert "Радар тижня" in (tmp_path / "o" / "radar.html").read_text(encoding="utf-8")


def test_main_sends_text_with_buttons_and_reports_a_failed_send(tmp_path, monkeypatch):
    snap, t = _radar_snap()
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    sent = []
    monkeypatch.setenv(rw.ab.PANEL_URL_ENV, "https://tradedesk.tail1234.ts.net/")
    monkeypatch.setattr(rw.time, "time", lambda: t + 300)
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append((text, k)) or True)
    assert rw.main(["--snapshot", str(path)]) == 0
    text, kw = sent[0]
    assert "81 093" in text and kw["topic"] == "alerts" and kw["per_row"] == rw.BUTTONS_PER_ROW
    kb = tg_send.payload("1", text, buttons=kw["buttons"], per_row=kw["per_row"])["reply_markup"]["inline_keyboard"]
    assert [[b["text"] for b in row] for row in kb] == [["Радар BTC", "Радар ETH"]]
    sent.clear()
    monkeypatch.setattr(tg_send, "send", lambda text, **k: sent.append(text) or False)  # Telegram down / not configured
    assert rw.main(["--snapshot", str(tmp_path / "none.json")]) == 1
    assert len(sent) == 1 and "Радару сьогодні немає" in sent[0]


def test_flag_path_matches_between_the_unit_and_step_25():
    unit = (ROOT / "deploy" / "user-units" / "tradedesk-radar-weekly.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "user-units" / "tradedesk-radar-weekly.timer").read_text(encoding="utf-8")
    step = (ROOT / "scripts" / "server" / "25-radar-weekly-timer.sh").read_text(encoding="utf-8")
    cond = re.search(r"^ConditionPathExists=(\S+)$", unit, re.M).group(1)
    d = re.search(r"^DIR=(\S+)$", step, re.M).group(1)
    flag = re.search(r'^FLAG="\$DIR/(\S+)"$', step, re.M).group(1)
    assert cond == f"{d}/{flag}" and flag != "brief-telegram.on"  # its own switch, independent of the morning brief
    assert "scripts/radar_weekly_tg.py" in unit and re.search(r"^OnCalendar=Sun \*-\*-\* 19:30:00 Europe/Kyiv$", timer, re.M)
    assert "runuser" not in step and "sudo -" not in step and '"$(id -un)" == "$SVC"' in step  # runs as tradedesk
