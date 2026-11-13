"""scripts/x_backfill.py: month windows, tweet normalisation, paging, resume and rendering (no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("x_backfill", _SCRIPTS / "x_backfill.py")
xb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xb)


def _tw(i: int, created: str = "Tue Dec 10 07:00:30 +0000 2024", **kw) -> dict:
    return {"id": str(i), "createdAt": created, "text": f"t{i}", "author": {"userName": "XO"}, **kw}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(xb, "ROOT", tmp_path)
    return tmp_path


def test_month_windows_cover_range_newest_first():
    w = xb.month_windows(date(2024, 11, 13), date(2025, 1, 5))
    assert [x[0] for x in w] == ["2025-01", "2024-12", "2024-11"]
    assert w[-1][1] == 1731456000  # 2024-11-13 00:00 UTC, not the 1st
    assert w[0][2] == 1736035200  # 2025-01-05 00:00 UTC
    assert all(lo < hi for _, lo, hi in w)
    assert w[1][2] == w[0][1]  # contiguous


def test_parse_created_formats():
    assert xb.parse_created("Tue Dec 10 07:00:30 +0000 2024") == "2024-12-10T07:00:30Z"
    assert xb.parse_created("2024-12-10T07:00:30.000Z") == "2024-12-10T07:00:30Z"
    assert xb.parse_created("garbage") == ""


def test_media_urls_from_any_known_place():
    ext = {"extendedEntities": {"media": [{"media_url_https": "https://pbs.twimg.com/media/a.jpg"}]}}
    assert xb.media_urls(ext) == ["https://pbs.twimg.com/media/a.jpg"]
    assert xb.media_urls({"entities": {"media": [{"media_url_https": "https://x/b.jpg"}], "urls": []}}) == ["https://x/b.jpg"]
    assert xb.media_urls({"entities": {"urls": []}}) == []
    assert xb.media_urls({"media": [{"media_url_https": "http://insecure"}]}) == []


def test_normalize_keeps_reply_quote_and_retweet():
    t = xb.normalize(
        _tw(
            5,
            isReply=True,
            inReplyToUsername="Trader_XO",
            inReplyToId=4,
            quoted_tweet=_tw(3, text="old call"),
            retweeted_tweet=None,
        )
    )
    assert t["is_reply"] and t["reply_to_user"] == "Trader_XO" and t["reply_to_id"] == "4"
    assert t["quoted"]["id"] == "3" and t["quoted"]["author"] == "XO" and t["retweet"] is None


def test_fetch_window_pages_until_no_next_and_stops_on_repeated_cursor():
    pages = [
        {"tweets": [_tw(1), _tw(2)], "has_next_page": True, "next_cursor": "c1"},
        {"tweets": [_tw(3)], "has_next_page": True, "next_cursor": "c2"},
        {"tweets": [_tw(4)], "has_next_page": False, "next_cursor": ""},
    ]
    seen = []

    def get(params):
        seen.append(params)
        return pages[len(seen) - 1]

    raw, n = xb.fetch_window(get, "XO", 10, 20, lambda: None)
    assert [t["id"] for t in raw] == ["1", "2", "3", "4"] and n == 3
    assert seen[0] == {"query": "from:XO since_time:10 until_time:20", "queryType": "Latest", "cursor": ""}
    assert seen[1]["cursor"] == "c1"

    loop = {"tweets": [_tw(9)], "has_next_page": True, "next_cursor": "same"}
    raw, n = xb.fetch_window(lambda p: loop, "XO", 10, 20, lambda: None)
    assert n == 2  # the repeated cursor ends the window instead of looping forever


def test_backfill_resumes_and_refetches_current_month(root):
    calls = []

    def get(params):
        calls.append(params["query"])
        return {"tweets": [_tw(len(calls))], "has_next_page": False}

    rep = xb.backfill("XO", get, date(2025, 1, 1), date(2025, 3, 10), lambda: None, 1000, log=lambda s: None)
    assert rep["requests"] == 3 and rep["tweets"] == 3
    state = json.loads((root / "x-XO" / "state.json").read_text())
    assert state["done_months"] == ["2025-01", "2025-02"]  # March is the current month: not marked done
    calls.clear()
    xb.backfill("XO", get, date(2025, 1, 1), date(2025, 3, 10), lambda: None, 1000, log=lambda s: None)
    assert len(calls) == 1 and "since_time:1740787200" in calls[0]  # only March again


def test_backfill_budget_guard(root):
    get = lambda p: {"tweets": [_tw(int(p["query"].split("since_time:")[1].split()[0]))], "has_next_page": False}  # noqa: E731
    rep = xb.backfill("XO", get, date(2025, 1, 1), date(2025, 6, 1), lambda: None, 2, log=lambda s: None)
    assert rep["new"] == 2 and rep["stopped"]


def test_render_marks_own_thread_and_replies(root):
    tweets = {
        "1": xb.normalize(_tw(1, text="BTC long 60k, stop 58k")),
        "2": xb.normalize(_tw(2, created="Wed Dec 11 07:00:30 +0000 2024", isReply=True, inReplyToUsername="xo")),
        "3": xb.normalize(_tw(3, created="Mon Jan 06 07:00:30 +0000 2025", isReply=True, inReplyToUsername="someone")),
    }
    (root / "x-XO").mkdir()
    xb.render("XO", tweets)
    md = (root / "x-XO" / "tweets.md").read_text()
    assert "BTC long 60k" in md and "продовження власного треду" in md and "відповідь @someone" in md
    assert sorted(p.name for p in (root / "x-XO" / "parts").iterdir()) == ["2024-Q4.md", "2025-Q1.md"]


def test_download_photos_skips_failures(root):
    tweets = {"1": {"id": "1", "media": ["https://a/1.jpg", "https://a/bad.jpg"]}}

    def fetch(url):
        if "bad" in url:
            raise OSError("404")
        return b"jpg"

    assert xb.download_photos("XO", tweets, fetch) == 1
    assert tweets["1"]["photo_files"] == ["1-1.jpg"]


def test_main_without_key_exits_1(root, monkeypatch, tmp_path):
    monkeypatch.setattr(xb, "KEY_FILE", tmp_path / "none.env")
    assert xb.main(["XO"]) == 1
