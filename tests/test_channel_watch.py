import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import channel_watch as cw  # noqa: E402

PAGE = (Path(__file__).parent / "fixtures" / "tg_channel_datahawks_2026-10-23.html").read_text(encoding="utf-8")


def test_parse_page_reads_text_photos_and_long_video():
    posts = cw.parse_page(PAGE, "DataHawks")
    assert [p["id"] for p in posts] == [1004, 1138, 1139]
    radar = posts[-1]
    assert radar["date"] == "2026-10-18T18:14:46+00:00"
    assert radar["url"] == "https://t.me/DataHawks/1139"
    assert "ОПЦИОННЫЙ РАДАР | SPX" in radar["text"]
    assert "S&P 500" in radar["text"] and "<b>" not in radar["text"]
    assert len(radar["photos"]) == 1 and radar["photos"][0].startswith("https://cdn")
    assert radar["videos"] == []
    review = posts[0]
    assert review["videos"] == [{"duration_s": 34 * 60 + 54, "src": "", "too_big": True}]
    assert "Еженедельный обзор" in review["text"]


def test_html_to_text_keeps_lines_and_links():
    frag = 'a<br/>b <a href="https://x.org/p">тут</a> <a href="https://y.org">https://y.org</a> &amp; c'
    assert cw.html_to_text(frag) == "a\nb тут (https://x.org/p) https://y.org & c"


def test_parse_duration():
    assert cw.parse_duration("34:54") == 2094
    assert cw.parse_duration("1:02:03") == 3723
    assert cw.parse_duration("") is None and cw.parse_duration("x") is None


def test_video_plan_server_only_for_short_files_here():
    assert cw.video_plan({"videos": []}) == ""
    assert cw.video_plan({"videos": [{"duration_s": 300, "file": "/v/1.mp4"}]}) == "server"
    assert cw.video_plan({"videos": [{"duration_s": 2094, "file": "/v/1.mp4"}]}) == "mac"
    assert cw.video_plan({"videos": [{"duration_s": 300, "file": ""}]}) == "mac"


def test_usage_blocked():
    assert cw.usage_blocked(None) == ""
    assert cw.usage_blocked({"five_hour": {"utilization": 40}, "seven_day": {"utilization": 60}}) == ""
    assert cw.usage_blocked({"five_hour": {"utilization": 97.2}, "seven_day": {"utilization": 60}}) == "five_hour=97%"


def test_notice_escapes_text_and_asks_for_the_mac():
    post = {
        "channel": "DataHawks",
        "id": 1200,
        "date": "2026-10-24T10:00:00+00:00",
        "text": "<b>BTC</b> & ETH\nдалі",
        "photos": ["u1", "u2"],
        "videos": [{"duration_s": 2094, "file": ""}],
        "documents": [],
    }
    msg = cw.notice(post)
    assert "&lt;b&gt;BTC&lt;/b&gt; &amp; ETH" in msg
    assert "фото: 2" in msg and "відео 34:54" in msg
    assert "потрібен Mac" in msg and "tg_live_video.sh" in msg and "bash -s DataHawks 1200" in msg


def test_fetch_new_pages_back_until_known(monkeypatch):
    first = "".join(f'<div data-post="Ch/{i}"><time datetime="d{i}"></time></div>' for i in range(21, 41))
    older = "".join(f'<div data-post="Ch/{i}"><time datetime="d{i}"></time></div>' for i in range(1, 21))
    seen = []

    def get(url, timeout=30):
        seen.append(url)
        return (older if "before=21" in url else first).encode()

    new = cw.fetch_new("Ch", 15, get)
    assert [p["id"] for p in new] == list(range(16, 41))
    assert seen == ["https://t.me/s/Ch", "https://t.me/s/Ch?before=21"]


def test_store_saves_text_photos_and_status(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "PRIVATE", tmp_path)
    post = cw.parse_page(PAGE, "DataHawks")[-1]
    stored = cw.store(post, get=lambda url, timeout=30: b"\xff\xd8jpg")
    d = tmp_path / "library" / "courses" / "DataHawks" / "live" / "1139"
    assert (d / "photo-1.jpg").read_bytes() == b"\xff\xd8jpg"
    assert "ОПЦИОННЫЙ РАДАР" in (d / "post.md").read_text(encoding="utf-8")
    assert json.loads((d / "post.json").read_text(encoding="utf-8"))["status"] == "new"
    assert stored["video_plan"] == ""


def test_long_video_without_file_waits_for_the_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "PRIVATE", tmp_path)
    post = cw.store(cw.parse_page(PAGE, "DataHawks")[0], get=lambda url, timeout=30: b"x")
    assert post["video_plan"] == "mac"
    assert cw.make_transcripts(post) is False  # no video file, no segments from the Mac yet


def test_analyze_waits_while_usage_is_high(monkeypatch):
    monkeypatch.setattr(cw, "read_usage", lambda: {"seven_day": {"utilization": 98}})
    monkeypatch.setattr(cw, "prepare_clone", lambda branch: pytest.fail("no clone while usage is high"))
    assert cw.analyze("DataHawks").startswith("usage seven_day=98%")


def test_analyze_with_nothing_new_does_not_start_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "PRIVATE", tmp_path)
    monkeypatch.setattr(cw, "read_usage", lambda: None)
    monkeypatch.setattr(cw, "ask_claude", lambda *a: pytest.fail("no claude without posts"))
    assert cw.analyze("DataHawks") == "nothing to analyse"


def test_instructions_exist_and_keep_channel_text_as_data():
    rules = (cw.REPO_DIR / "docs" / "agents" / "channel-watch.md").read_text(encoding="utf-8")
    assert "untrusted data" in rules and "calls.csv" in rules
    assert "WebFetch" in cw.DENIED and "Read(~/.config/tradedesk/**)" in cw.DENIED
