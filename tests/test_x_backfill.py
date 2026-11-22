"""scripts/x_backfill.py: month windows, tweet normalisation, paging, resume and rendering (no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, date, datetime
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


JAN_START, JAN_END = 1735689600, 1738368000  # 2025-01-01 / 2025-02-01 00:00 UTC
DAY = 86400


def _at(i: int, epoch: int) -> dict:
    return _tw(i, created=datetime.fromtimestamp(epoch, UTC).strftime("%a %b %d %H:%M:%S +0000 %Y"))


def _window(params: dict) -> tuple[int, int]:
    q = params["query"]
    return int(q.split("since_time:")[1].split()[0]), int(q.split("until_time:")[1].split()[0])


def _truncating(timeline: list[dict], page: int = 3, inclusive: bool = False):
    """A fake search: tweets in the window newest first, but only ``page`` of them and never a cursor (the API ending
    the paging mid-month). ``inclusive`` also returns tweets at exactly ``until_time``."""
    calls: list[tuple[int, int]] = []

    def epoch(t: dict) -> int:  # parsed here, not with the code under test
        return int(datetime.strptime(t["createdAt"], "%a %b %d %H:%M:%S %z %Y").timestamp())

    def get(params):
        lo, hi = _window(params)
        calls.append((lo, hi))
        hits = [t for t in timeline if lo <= epoch(t) and (epoch(t) <= hi if inclusive else epoch(t) < hi)]
        hits.sort(key=epoch, reverse=True)
        return {"tweets": hits[:page], "has_next_page": False, "next_cursor": ""}

    return get, calls


def test_fetch_month_continues_after_page_without_cursor():
    timeline = [_at(i, JAN_START + (3 + 3 * i) * DAY) for i in range(1, 8)]  # Jan 7 .. Jan 25
    get, calls = _truncating(timeline)
    raw, n, oldest, complete = xb.fetch_month(get, "XO", JAN_START, JAN_END, lambda: None)
    assert sorted(t["id"] for t in raw) == [str(i) for i in range(1, 8)]
    assert complete and oldest == JAN_START + 6 * DAY
    assert n == 4  # 3 + 3 + 1 tweets, then the empty follow-up window
    assert [hi for _, hi in calls] == [JAN_END, JAN_START + 18 * DAY, JAN_START + 9 * DAY, JAN_START + 6 * DAY]
    assert all(lo == JAN_START for lo, _ in calls)


def test_fetch_month_reaching_window_start_needs_no_extra_window():
    get, calls = _truncating([_at(1, JAN_START + 20 * DAY), _at(2, JAN_START + 30)])
    raw, n, oldest, complete = xb.fetch_month(get, "XO", JAN_START, JAN_END, lambda: None)
    assert complete and oldest == JAN_START + 30 and n == 1 and len(calls) == 1


def test_fetch_month_empty_month_is_complete():
    get, calls = _truncating([])
    assert xb.fetch_month(get, "XO", JAN_START, JAN_END, lambda: None) == ([], 1, None, True)


def test_fetch_month_dedupes_overlapping_windows():
    timeline = [_at(i, JAN_START + (3 + 3 * i) * DAY) for i in range(1, 6)]
    get, calls = _truncating(timeline, page=2, inclusive=True)  # every window repeats the previous oldest tweet
    raw, n, oldest, complete = xb.fetch_month(get, "XO", JAN_START, JAN_END, lambda: None)
    ids = [t["id"] for t in raw]
    assert sorted(ids) == ["1", "2", "3", "4", "5"] and len(ids) == len(set(ids))
    assert complete and oldest == JAN_START + 6 * DAY


def test_fetch_month_loop_guards():
    stuck = iter(range(100, 200))  # new ids each time, but never older than the first one
    get = lambda p: {"tweets": [_at(next(stuck), JAN_START + 10 * DAY)], "has_next_page": False}  # noqa: E731
    raw, n, oldest, complete = xb.fetch_month(get, "XO", JAN_START, JAN_END, lambda: None)
    assert not complete and n == 2 and oldest == JAN_START + 10 * DAY

    undated = iter(range(100, 200))
    get = lambda p: {"tweets": [_tw(next(undated), created="garbage")]}  # noqa: E731
    assert xb.fetch_month(get, "XO", JAN_START, JAN_END, lambda: None)[1:] == (1, None, False)

    creep = iter(range(100, 200))  # always one second older: would walk for weeks of requests without the cap
    get = lambda p: {"tweets": [_at(next(creep), _window(p)[1] - 1)], "has_next_page": False}  # noqa: E731
    raw, n, oldest, complete = xb.fetch_month(get, "XO", JAN_START, JAN_END, lambda: None, max_windows=5)
    assert not complete and n == 5 and oldest == JAN_END - 5


def test_backfill_marks_month_done_only_when_complete_and_rerun(root):
    timeline = [_at(i, JAN_START + (3 + 3 * i) * DAY) for i in range(1, 8)]
    get, calls = _truncating(timeline)
    rep = xb.backfill("XO", get, date(2025, 1, 1), date(2025, 2, 10), lambda: None, 1000, log=lambda s: None)
    assert rep["tweets"] == 7 and rep["incomplete"] == [] and rep["unverified"] == []
    state = json.loads((root / "x-XO" / "state.json").read_text())
    assert state["done_months"] == ["2025-01"]
    assert state["oldest"] == {"2025-01": "2025-01-07T00:00:00Z", "2025-02": None}
    calls.clear()
    rep = xb.backfill("XO", get, date(2025, 1, 1), date(2025, 2, 10), lambda: None, 1000, log=lambda s: None)
    assert all(lo == 1738368000 for lo, _ in calls) and rep["new"] == 0  # only February (current month) again

    stuck = iter(range(100, 200))
    get = lambda p: {"tweets": [_at(next(stuck), _window(p)[0] + 10 * DAY)], "has_next_page": False}  # noqa: E731
    rep = xb.backfill("YY", get, date(2025, 1, 1), date(2025, 2, 10), lambda: None, 1000, log=lambda s: None)
    assert rep["incomplete"] == ["2025-01"]
    assert json.loads((root / "x-YY" / "state.json").read_text())["done_months"] == []  # refetched next run


def test_old_state_without_oldest_is_kept_and_reported_unverified(root, capsys):
    d = root / "x-XO"
    d.mkdir()
    old = [xb.normalize(_at(1, JAN_START + 20 * DAY))]
    (d / "tweets.json").write_text(json.dumps(old))
    (d / "state.json").write_text(json.dumps({"done_months": ["2024-12", "2025-01"]}))
    assert xb.main(["XO", "--stat"]) == 0  # no key file needed
    out = json.loads(capsys.readouterr().out)
    cov = out.pop("coverage")
    assert out == {"handle": "XO", "tweets": 1, "done_months": 2, "unverified": ["2024-12", "2025-01"]}
    assert [(x["month"], x["status"], x["empty_days"]) for x in cov["months"]] == [
        ("2024-12", "unverified", 31),
        ("2025-01", "unverified", 30),
    ]

    calls = []

    def get(params):
        calls.append(_window(params))
        return {"tweets": [], "has_next_page": False}

    rep = xb.backfill("XO", get, date(2024, 12, 1), date(2025, 2, 10), lambda: None, 1000, log=lambda s: None)
    assert [lo for lo, _ in calls] == [1738368000]  # the done months are not refetched
    assert rep["unverified"] == ["2024-12", "2025-01"] and rep["tweets"] == 1
    state = json.loads((d / "state.json").read_text())
    assert state["done_months"] == ["2024-12", "2025-01"] and state["oldest"] == {"2025-02": None}
    assert json.loads((d / "tweets.json").read_text()) == old


def test_coverage_report_empty_days_first_last_and_status():
    tweets = [
        xb.normalize(_tw(1, created="Tue Dec 31 10:00:00 +0000 2024")),
        xb.normalize(_tw(2, created="Mon Jan 20 09:00:00 +0000 2025")),
        xb.normalize(_tw(3, created="Sat Jan 25 08:00:00 +0000 2025")),
        xb.normalize(_tw(4, created="Sat Jan 25 21:00:00 +0000 2025")),
        xb.normalize(_tw(5, created="Fri Jan 31 23:59:00 +0000 2025")),
        xb.normalize(_tw(6, created="Mon Feb 03 12:00:00 +0000 2025")),
        xb.normalize(_tw(7, created="garbage")),  # undated: left out
    ]
    state = {"done_months": ["2024-11", "2024-12", "2025-01"], "oldest": {"2024-11": None, "2025-01": "2025-01-20T09:00:00Z"}}
    rep = xb.coverage_report(tweets, state, date(2025, 2, 10))
    by = {x["month"]: x for x in rep["months"]}
    assert list(by) == ["2024-11", "2024-12", "2025-01", "2025-02"]
    nov, dec, jan, feb = by.values()
    assert nov["status"] == "done" and nov["tweets"] == 0 and nov["first"] is None and nov["empty"] == "1-30"
    assert dec["status"] == "unverified" and dec["empty_days"] == 30 and dec["first"] == dec["last"] == "2024-12-31T10:00:00Z"
    assert jan["status"] == "done" and jan["tweets"] == 4 and jan["empty"] == "1-19, 21-24, 26-30"
    assert jan["first"] == "2025-01-20T09:00:00Z" and jan["last"] == "2025-01-31T23:59:00Z"
    assert jan["empty_days"] == 28 and jan["empty_share"] == pytest.approx(28 / 31, abs=1e-3)
    # the current month ends with today: days after it are not holes
    assert feb["status"] == "open" and (feb["from"], feb["to"], feb["days"]) == ("2025-02-01", "2025-02-10", 10)
    assert feb["empty"] == "1-2, 4-10"
    # totals start at the first stored tweet (December): the tweetless November before it is listed, not counted
    assert rep["counted_from"] == "2024-12"
    assert (rep["days"], rep["empty_days"]) == (31 + 31 + 10, 30 + 28 + 9)
    assert rep["empty_share"] == pytest.approx(67 / 72, abs=1e-3)
    assert xb.coverage_report([], {"done_months": [], "oldest": {}}, date(2025, 2, 10)) == {
        "counted_from": None,
        "days": 0,
        "empty_days": 0,
        "empty_share": None,
        "ignored_future": 0,
        "suspect_months": [],
        "bad_oldest": [],
        "months": [],
    }


def test_coverage_report_flags_suspect_done_months_and_counts_future_tweets():
    """A done month whose oldest tweet is mid-month is suspect (paging may have been cut); a done month reaching its
    start, a tweetless done month and an unverified month (no ``oldest``) are not. Tweets after today are counted in
    ``ignored_future`` and left out of the month counts."""
    tweets = [
        xb.normalize(_tw(1, created="Wed Jan 15 06:00:00 +0000 2025")),
        xb.normalize(_tw(2, created="Mon Feb 03 12:00:00 +0000 2025")),
        xb.normalize(_tw(3, created="Mon Mar 10 08:00:00 +0000 2025")),
        xb.normalize(_tw(4, created="Wed Mar 12 08:00:00 +0000 2025")),  # after today (11 March)
        xb.normalize(_tw(5, created="Tue May 06 08:00:00 +0000 2025")),  # a future month
    ]
    state = {
        "done_months": ["2024-11", "2024-12", "2025-01", "2025-02"],
        "oldest": {
            "2024-11": None,  # done, no tweets at all: nothing to judge
            "2025-01": "2025-01-15T06:00:00Z",  # 14.25 days after the start: suspect
            "2025-02": "2025-02-01T23:00:00Z",  # within a day of the start: fine
        },  # 2024-12: done by an older version, no oldest: unverified, not suspect
    }
    rep = xb.coverage_report(tweets, state, date(2025, 3, 11))
    [sus] = rep["suspect_months"]
    assert (sus["month"], sus["oldest"]) == ("2025-01", "2025-01-15T06:00:00Z")
    assert sus["gap_days"] == pytest.approx(14.25, abs=0.06)
    assert rep["ignored_future"] == 2
    by = {x["month"]: x for x in rep["months"]}
    assert by["2024-12"]["status"] == "unverified"
    assert list(by)[-1] == "2025-03" and by["2025-03"]["tweets"] == 1 and by["2025-03"]["last"] == "2025-03-10T08:00:00Z"
    just_over = {"done_months": ["2025-02"], "oldest": {"2025-02": "2025-02-02T00:01:00Z"}}
    assert [x["month"] for x in xb.coverage_report([], just_over, date(2025, 3, 11))["suspect_months"]] == ["2025-02"]
    assert rep["bad_oldest"] == []


def test_coverage_report_lists_unparsable_oldest_instead_of_crashing():
    """A damaged or hand-edited state.json (wrong date format, a number) must not crash --stat: such done months go to
    ``bad_oldest`` and are not judged; a valid month next to them is still flagged as suspect."""
    state = {
        "done_months": ["2025-01", "2025-02", "2025-03"],
        "oldest": {"2025-01": "2025-01-15 06:00", "2025-02": 1738368000, "2025-03": "2025-03-20T00:00:00Z"},
    }
    rep = xb.coverage_report([], state, date(2025, 4, 2))
    assert rep["bad_oldest"] == ["2025-01", "2025-02"]
    assert [x["month"] for x in rep["suspect_months"]] == ["2025-03"]
    assert {x["month"]: x["status"] for x in rep["months"]}["2025-01"] == "done"


def test_truncated_paging_leaves_no_empty_days_in_coverage(root):
    """A tweet every day of January, but the fake API returns 3 per window and never a cursor: the month must be
    exhausted (every day present) before it is marked done, and --stat must show it."""
    daily = [_at(d, JAN_START + (d - 1) * DAY + 12 * 3600) for d in range(1, 32)]
    get, calls = _truncating(daily)
    rep = xb.backfill("XO", get, date(2025, 1, 1), date(2025, 2, 10), lambda: None, 1000, log=lambda s: None)
    assert rep["tweets"] == 31 and rep.get("incomplete") == []
    jan = xb.stat("XO", today=date(2025, 2, 10))["coverage"]["months"][0]
    assert (jan["month"], jan["status"], jan["tweets"], jan["empty_days"]) == ("2025-01", "done", 31, 0)
    assert jan["first"] == "2025-01-01T12:00:00Z" and jan["last"] == "2025-01-31T12:00:00Z"


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
