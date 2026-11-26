"""Daily CPI snapshot (item 480, IDEA-20261028-pm-cpi-brackets-vs-nowcast-forward): the Cleveland Fed provider, the
pure helpers (analytics/pm_cpi.py), the table pm_cpi_snapshot (schema 44) and the collector job `pm_cpi_snapshot`.

The sample is the live snapshot of 2026-11-26 19:20 UTC, trimmed (tests/fixtures/pm_cpi_snapshot_2026-11-26.json,
`_source`). 26.11.2026 was Thanksgiving: the files' stamp and last daily point are 25.11, so on 26.11 the row is 'stale'.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import sqlite3
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from conftest import State, make_transports
from tradedesk.analytics import collector_health as ch
from tradedesk.analytics.pm_cpi import (
    BOOKS_MAX_TRIES,
    as_listed,
    book_depth,
    books_complete,
    books_paired,
    cf_daily,
    closed_tokens,
    confirms_close,
    is_fresh,
    mark_gone,
    merge_brackets,
    new_points_after,
    newer_or_same,
    nowcast_summary,
    pct,
    phase,
    plan,
    release_target,
    to_confirm,
    with_book,
)
from tradedesk.collector.pm_cpi_snapshot import CONFIRM_MAX, SERIES, collect_pm_cpi_snapshot
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.features import allowed
from tradedesk.runtime import Runtime
from tradedesk.store import SCHEMA_VERSION, Store
from tradedesk.store.schema import MIGRATIONS

FIXTURE = Path(__file__).parent / "fixtures" / "pm_cpi_snapshot_2026-11-26.json"
SAMPLE = json.loads(FIXTURE.read_text())
YEAR, MONTH = SAMPLE["nowcast_year"], SAMPLE["nowcast_month"]
T0 = datetime(2026, 11, 26, 15, 35, tzinfo=UTC)  # Thursday (Thanksgiving), inside the window
DAY = "2026-11-26"
N_BRACKETS = 39  # 12 + 10 + 10 + 7 open brackets, every one with a bid and an ask on the sample


def _with_stamp(charts: list, stamp: str) -> list:
    out = copy.deepcopy(charts)
    for c in out:
        c["chart"]["_comment"] = stamp
    return out


def _append_points(charts: list, target: str, points: list[tuple[str, str]], series: str = "CPI Inflation") -> list:
    """A later copy: daily points added at the end of one target's chart (the other series get an empty value)."""
    out = copy.deepcopy(charts)
    c = next(x for x in out if x["chart"]["subcaption"] == target)
    cats = c["categories"][0]["category"]
    n_pts = sum(1 for x in cats if not x.get("vline"))
    for ds in c["dataset"]:  # the data run is aligned with the non-vline categories
        ds["data"] = ds["data"][:n_pts]
    tail = [x for x in cats if x.get("vline") and cats.index(x) == len(cats) - 1]
    if tail:
        cats.pop()
    for label, value in points:
        cats.append({"label": label})
        for ds in c["dataset"]:
            ds["data"].append({"value": value if ds["seriesname"] == series else ""})
    cats.extend(tail)
    return out


# ----------------------------------------------------------------------------------------------------- pure helpers


def test_nowcast_summary_on_the_live_files():
    y = nowcast_summary(YEAR, DAY)
    assert (y["stamp"], y["last_day"], y["fresh"]) == ("2026-11-25 00:00", "2026-11-25", False)  # holiday: no new point
    assert (y["target"], y["prev_target"]) == ("2026-11", "2026-10")  # 2026-10 has its 'CPI Oct' line, 2026-11 not yet
    assert y["latest"]["target"]["headline"] == pytest.approx(3.79378308246552)  # percent, as published
    assert y["latest"]["target"]["core"] == pytest.approx(2.87325045473827)
    assert y["latest"]["prev"]["headline"] == pytest.approx(3.74183045049836)  # last point before the October release
    assert sorted(y["points"]) == ["2026-10", "2026-11", "2026-9"]
    assert len(y["points"]["2026-11"]["headline"]) == 17 and y["points"]["2026-11"]["headline"][-1][0] == "2026-11-25"
    m = nowcast_summary(MONTH, "2026-11-25")
    assert m["fresh"] is True and m["target"] == "2026-11"
    assert m["latest"]["target"]["headline"] == pytest.approx(-0.0009670665748075)
    assert m["latest"]["target"]["core"] == pytest.approx(0.21114452794251)
    assert m["latest"]["prev"]["core"] == pytest.approx(0.211437433436468)


def test_release_target_and_freshness_rules():
    assert release_target(YEAR) == "2026-11"
    # 10.12: November gets its 'CPI Nov' line and a December chart exists: December becomes the target
    later = copy.deepcopy(YEAR)
    dec = copy.deepcopy(later[-1])
    dec["chart"]["subcaption"] = "2026-12"
    later[-1]["categories"][0]["category"].append({"label": "CPI Nov", "vline": "true"})
    assert release_target([*later, dec]) == "2026-12"
    assert release_target([]) is None and release_target({"x": 1}) is None
    # a malformed chart is left out, not raised
    assert release_target([{"chart": {"subcaption": "bad"}}, *YEAR]) == "2026-11"
    # the stamp convention is not settled: a stamp of the day or a last point of the day both count
    assert is_fresh("2026-12-03 00:00", "2026-12-02", "2026-12-03") and is_fresh("2026-12-02 00:00", "2026-12-03", "2026-12-03")
    assert not is_fresh("2026-12-02 00:00", "2026-12-02", "2026-12-03") and not is_fresh(None, None, "2026-12-03")
    assert newer_or_same({"stamp": "2026-12-03 00:00", "last_day": "2026-12-03"}, {"stamp": "2026-12-02 00:00", "last_day": "2026-12-02"})
    assert not newer_or_same(
        {"stamp": "2026-12-02 00:00", "last_day": "2026-12-02"}, {"stamp": "2026-12-03 00:00", "last_day": "2026-12-03"}
    )
    assert newer_or_same({"stamp": None, "last_day": None}, None)
    # remark 10: a copy without a readable stamp is compared by its last point, not taken for an older one
    stored = {"stamp": "2026-12-02 00:00", "last_day": "2026-12-02"}
    assert newer_or_same({"stamp": None, "last_day": "2026-12-03"}, stored)
    assert newer_or_same({"stamp": "2026-12-03 00:00", "last_day": "2026-12-02"}, stored)  # stamped today, no new point
    assert not newer_or_same({"stamp": "2026-12-02 00:00", "last_day": "2026-12-01"}, stored)  # an older CDN copy
    assert not newer_or_same({"stamp": "2026-12-01 00:00", "last_day": "2026-12-02"}, stored)  # same point, older stamp
    assert not newer_or_same({"stamp": None, "last_day": "2026-12-02"}, stored)  # a tie: the stamped copy stays


def test_bounded_parsers_never_raise():
    # remark 4: values of the files and books that are not numbers (or no sensible one) are None, never an exception
    for bad in (None, "", True, "abc", float("inf"), float("nan"), "1e400", 10**400, {"x": 1}, [1], 250.0):
        assert pct(bad) is None
    assert pct("3.79") == pytest.approx(3.79) and pct(-0.5) == -0.5
    chart = copy.deepcopy(YEAR[-1])
    ds = next(d for d in chart["dataset"] if d["seriesname"] == "CPI Inflation")
    ds["data"][0] = {"value": "Infinity"}
    ds["data"][1] = "not a dict"
    ds["data"][2] = {"value": {"nested": 1}}
    pts = cf_daily(chart, "CPI Inflation")[1]
    assert [v for _, v in pts[:3]] == [None, None, None] and pts[3][1] is not None
    book = {"timestamp": float("inf"), "bids": [{"price": "0.4", "size": "10"}], "asks": []}
    for ts in (float("inf"), float("nan"), 1e30, 10**400, "abc", True, [1]):
        assert with_book({"token": "t"}, book | {"timestamp": ts}, 5)["book_ts_ms"] is None
    assert with_book({"token": "t"}, book | {"timestamp": "1795720848041"}, 5)["book_ts_ms"] == 1795720848041
    d = book_depth({"timestamp": 1e30, "tick_size": "0.01", "min_order_size": "5", "last_trade_price": "Infinity",
                    "bids": [{"price": "0.30", "size": "5"}, {"price": "NaN", "size": "1"}, {"price": "0.32", "size": "1e400"},
                             {"price": True, "size": "1"}, "junk", {"price": "0.31", "size": "2"}],
                    "asks": {"not": "a list"}})  # fmt: skip
    assert d == {"book_ts_ms": None, "tick": 0.01, "min_size": 5.0, "last": None, "bids": [[0.31, 2.0], [0.30, 5.0]], "asks": []}


def test_new_points_after_the_old_stamp():
    later = _with_stamp(_append_points(YEAR, "2026-11", [("11/27", "3.81"), ("11/30", "3.85"), ("12/01", "3.85")]), "2026-12-01 00:00")
    rows = {r["target"]: r for r in new_points_after(YEAR, later)}
    assert list(rows) == ["2026-9", "2026-10", "2026-11"]
    nov = rows["2026-11"]
    assert (nov["after"], nov["n"], nov["distinct"], nov["changed"]) == ("2026-11-25", 3, 2, True)
    assert (nov["first"], nov["last"]) == ("2026-11-27", "2026-12-01")
    assert rows["2026-10"]["n"] == 0 and rows["2026-10"]["changed"] is False  # released in November: no new point
    flat = _append_points(YEAR, "2026-11", [("11/27", "3.79378308246552")])
    assert {r["target"]: (r["n"], r["distinct"], r["changed"]) for r in new_points_after(YEAR, flat)}["2026-11"] == (1, 1, False)
    gone = [c for c in YEAR if c["chart"]["subcaption"] != "2026-9"]
    assert new_points_after(YEAR, gone)[0] == {"target": "2026-9", "after": "2026-11-25", "n": None, "distinct": None,
                                               "changed": None, "first": None, "last": None}  # fmt: skip
    core = {r["target"]: r for r in new_points_after(YEAR, later, "Core CPI Inflation")}
    assert core["2026-11"]["n"] == 0  # only headline got new points in this copy


@pytest.mark.parametrize(
    ("when", "expect"),
    [
        (datetime(2026, 11, 28, 16, 0, tzinfo=UTC), "window"),  # Saturday: every day has a row (blocker 2)
        (datetime(2026, 11, 29, 17, 30, tzinfo=UTC), "late"),  # Sunday
        (datetime(2026, 11, 26, 15, 29, 59, tzinfo=UTC), "before"),
        (datetime(2026, 11, 26, 15, 30, tzinfo=UTC), "window"),
        (datetime(2026, 11, 26, 16, 59, 59, tzinfo=UTC), "window"),
        (datetime(2026, 11, 26, 17, 0, tzinfo=UTC), "late"),
        (datetime(2026, 11, 26, 21, 0, tzinfo=UTC), "after"),
    ],
)
def test_phase(when, expect):
    assert phase(when) == expect


def _done_books(read_ts: int) -> str:
    return json.dumps({"series_listed": list(SERIES),
                       "brackets": [{"token": "t", "book": True, "series": "x", "event_id": "1", "read_ts": read_ts}]})  # fmt: skip


def test_plan_decides_what_a_tick_fetches():
    ts = int(T0.timestamp())
    assert plan(None, T0, SERIES) == (True, True)
    assert plan(None, T0.replace(hour=12), SERIES) == (False, False)
    full = {"cf_year_sha": "a", "cf_month_sha": "b", "cf_last_day_year": "2026-11-26", "cf_last_day_month": "2026-11-26",
            "cf_year_ts": ts, "cf_month_ts": ts, "cf_fetch_ts": ts, "cf_fetches": 1, "cf_tries": 1, "cf_fresh": 1,
            "books": _done_books(ts), "books_tries": 1}  # fmt: skip
    assert plan(full, T0 + timedelta(minutes=50), SERIES) == (False, False)  # complete: nothing more today
    stale = full | {"cf_fresh": 0}
    assert plan(stale, T0 + timedelta(minutes=29), SERIES) == (False, False)  # too early for the retry
    assert plan(stale, T0 + timedelta(minutes=30), SERIES) == (True, False)
    assert plan(stale | {"cf_fetches": 3}, T0 + timedelta(minutes=60), SERIES) == (False, False)  # 3 downloads: stop
    assert plan(stale, T0.replace(hour=17, minute=30), SERIES) == (False, False)  # late phase: no staleness retry
    sat = datetime(2026, 11, 28, 15, 35, tzinfo=UTC)
    sat_row = stale | {"cf_fetch_ts": int(sat.timestamp())}
    assert plan(sat_row, sat + timedelta(minutes=30), SERIES) == (False, False)  # weekend: one download is enough
    no_month = full | {"cf_month_sha": None}
    assert plan(no_month, T0 + timedelta(minutes=10), SERIES) == (True, False)  # a missing file: every tick
    assert plan(no_month, T0.replace(hour=18), SERIES) == (True, False)  # and in the late phase
    assert plan(no_month | {"cf_tries": 12}, T0 + timedelta(minutes=10), SERIES) == (False, False)  # capped
    raw_only = full | {"cf_last_day_month": None}  # kept as raw bytes, could not be parsed: still missing
    assert plan(raw_only, T0 + timedelta(minutes=10), SERIES) == (True, False)
    unlisted = full | {"books": json.dumps({"series_listed": ["10007"], "brackets": [{"token": "t", "book": True, "read_ts": ts}]})}
    assert plan(unlisted, T0 + timedelta(minutes=10), SERIES) == (False, True)
    assert plan(full | {"books": "not json"}, T0 + timedelta(minutes=10), SERIES) == (False, True)
    # blocker 1: a book read before the stored nowcast was downloaded is due again
    assert plan(full | {"cf_month_ts": ts + 60}, T0 + timedelta(minutes=10), SERIES) == (False, True)
    assert plan(full | {"cf_month_ts": ts + 60, "books_tries": 79}, T0 + timedelta(minutes=10), SERIES) == (False, True)
    assert plan(full | {"cf_month_ts": ts + 60, "books_tries": BOOKS_MAX_TRIES}, T0 + timedelta(minutes=10), SERIES) == (False, False)
    # finding 3 of review C2: every 300 s tick from 15:30 to 21:00 fits in the daily attempts
    assert BOOKS_MAX_TRIES >= (21 * 60 - (15 * 60 + 30)) * 60 // 300
    assert not books_complete({"series_listed": list(SERIES), "brackets": []}, SERIES)  # no bracket at all
    assert not books_complete({"series_listed": list(SERIES), "brackets": [{"token": "t", "book": False}]}, SERIES)
    assert not books_complete(json.loads(_done_books(ts)), SERIES, since=ts + 1)


def test_merge_brackets_keeps_a_paired_book_and_supersedes_an_early_one():
    def br(tok, book, bid=None, ev="1", read_ts=100):
        return {"series": "10007", "event_id": ev, "market_id": tok, "token": tok, "book": book, "bid": bid, "read_ts": read_ts}

    old = [br("a", True, 0.4), br("b", False), br("c", True, 0.1), br("e", True, 0.6)]
    new = [br("a", True, 0.5, read_ts=200), br("b", True, 0.2, read_ts=200), br("d", True, 0.3, ev="2", read_ts=200),
           br("e", False, read_ts=0)]  # fmt: skip
    merged, sup = merge_brackets(old, new, since=50, now=200)
    got = {b["token"]: b for b in merged}
    assert got["a"]["bid"] == 0.4 and sup == []  # read after the nowcast (100 >= 50): the first quote of the day stays
    assert got["b"]["book"] is True and got["b"]["bid"] == 0.2  # a gap filled
    assert got["c"]["bid"] == 0.1  # not in the new listing: kept
    assert got["d"]["bid"] == 0.3 and len(got) == 5
    assert got["e"]["bid"] == 0.6  # a new entry without a book never replaces a stored book
    merged, sup = merge_brackets(old, new, since=150, now=200)  # a file stored at 150: books of 100 are early
    got = {b["token"]: b for b in merged}
    assert got["a"]["bid"] == 0.5 and got["a"]["read_ts"] == 200
    assert got["e"]["bid"] == 0.6 and got["c"]["bid"] == 0.1  # the re-read missed them: the early book stays in main
    assert [(x["token"], x["bid"], x["read_ts"], x["superseded_ts"]) for x in sup] == [("a", 0.4, 100, 200)]


# ----------------------------------------------------------------------------------------------------- provider


def _cf_handler(opts: dict, calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        calls.append(name)
        if opts.get("cf_down"):
            return httpx.Response(500, text="down")
        body = opts.get(name)
        if body is None:
            return httpx.Response(404, text="no such file")
        return httpx.Response(200, content=body if isinstance(body, bytes) else json.dumps(body).encode())

    return handler


def _cf_rt(tmp_path, opts: dict, calls: list) -> Runtime:
    tr = make_transports(State())
    tr["clevelandfed"] = httpx.MockTransport(_cf_handler(opts, calls))
    rt = Runtime(Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / "x.sqlite")), transports=tr)
    rt.clevelandfed.http._limiter = TokenBucket(1000.0, 1000)
    rt.now = lambda: T0
    return rt


async def test_provider_returns_raw_bytes_and_charts(tmp_path):
    raw = b"\xef\xbb\xbf" + json.dumps(YEAR).encode()  # a BOM is tolerated
    calls: list = []
    rt = _cf_rt(tmp_path, {"nowcast_year.json": raw, "nowcast_month.json": MONTH}, calls)
    try:
        f = await rt.clevelandfed.nowcast("year")
        assert f.data["raw"] == raw and f.data["charts"] == YEAR and f.evidence.provider == "clevelandfed"
        assert f.data["problem"] is None
        assert (await rt.clevelandfed.nowcast("month")).data["charts"][-1]["chart"]["subcaption"] == "2026-11"
        assert calls == ["nowcast_year.json", "nowcast_month.json"]
        with pytest.raises(ProviderError) as e:
            await rt.clevelandfed.nowcast("quarter")
        assert e.value.reason == "config"
    finally:
        await rt.aclose()


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b"<html>maintenance</html>", "not JSON"),
        ({"charts": []}, "non-empty list"),
        ([], "non-empty list"),
        ([{"chart": {"subcaption": "2013:Q3"}, "categories": [{"category": []}], "dataset": []}], "unexpected nowcast chart shape"),
        ([{"chart": {}}], "unexpected nowcast chart shape"),
    ],
)
async def test_provider_returns_a_bad_body_with_its_problem(tmp_path, body, detail):
    # remark 10: the bytes of a body that arrived but does not parse are returned (the job keeps them), not raised
    rt = _cf_rt(tmp_path, {"nowcast_year.json": body}, [])
    try:
        f = await rt.clevelandfed.nowcast("year")
        assert detail in f.data["problem"]
        assert f.data["raw"] == (body if isinstance(body, bytes) else json.dumps(body).encode())
    finally:
        await rt.aclose()


async def test_provider_http_failure_raises_provider_error(tmp_path):
    rt = _cf_rt(tmp_path, {"cf_down": True}, [])
    try:
        with pytest.raises(ProviderError):
            await rt.clevelandfed.nowcast("month")
    finally:
        await rt.aclose()


# ----------------------------------------------------------------------------------------------------- the job


class Venues:
    """Fake Cleveland Fed and Polymarket serving the sample. Mutable switches: ``cf`` (file name -> body; None in place
    of a body = HTTP 500; bytes are served as they are), ``gamma_down`` (series ids answering 500), ``gamma_empty``
    (series ids answering an empty list), ``closed`` (YES tokens whose market is served as closed), ``clob_down``
    (bool), ``books`` (token -> book; a token left out is not in the answer). Item 482 (round 2): ``omit`` (YES tokens
    whose market is left out of the listing), ``no_token`` (markets listed with an empty ``clobTokenIds``), ``resolved``
    (YES tokens whose ``/markets/{id}`` answers ``closed: true``; ``closed`` too), ``market_down`` (``/markets/{id}``
    answers 500). Item 483: ``down_markets`` (YES tokens whose ``/markets/{id}`` alone answers 500)."""

    def __init__(self) -> None:
        self.cf: dict[str, object] = {"nowcast_year.json": YEAR, "nowcast_month.json": MONTH}
        self.gamma_down: set[str] = set()
        self.gamma_empty: set[str] = set()
        self.closed: set[str] = set()
        self.omit: set[str] = set()
        self.no_token: set[str] = set()
        self.resolved: set[str] = set()
        self.market_down = False
        self.down_markets: set[str] = set()
        self.clob_down = False
        self.calls: list[str] = []
        self.books = {b["asset_id"]: b for b in SAMPLE["books"]}

    def market(self, mid: str) -> httpx.Response:
        self.calls.append(f"market:{mid}")
        if self.market_down:
            return httpx.Response(500, text="down")
        for ev in (e for evs in SAMPLE["events"].values() for e in evs):
            for m in ev["markets"]:
                if str(m["id"]) == mid:
                    tok = json.loads(m["clobTokenIds"])[0]
                    if tok in self.down_markets:
                        return httpx.Response(500, text="down")
                    return httpx.Response(200, json=m | {"closed": tok in self.resolved or tok in self.closed})
        return httpx.Response(404, text="no such market")

    def cleveland(self, req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        self.calls.append(f"cf:{name}")
        body = self.cf.get(name)
        if body is None:
            return httpx.Response(500, text="down")
        return httpx.Response(200, content=body if isinstance(body, bytes) else json.dumps(body).encode())

    def gamma(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/markets/"):
            return self.market(req.url.path[len("/markets/") :])
        sid = req.url.params.get("series_id")
        self.calls.append(f"gamma:{sid}")
        if req.url.path != "/events/keyset" or req.url.params.get("closed") != "false":
            return httpx.Response(404, text="not mocked")
        if sid in self.gamma_down:
            return httpx.Response(500, text="down")
        events = [] if sid in self.gamma_empty else SAMPLE["events"].get(sid, [])
        if self.closed or self.omit or self.no_token:

            def yes(m: dict) -> str:
                return json.loads(m["clobTokenIds"])[0]

            def shut(m: dict) -> dict:
                m = m | {"closed": True} if yes(m) in self.closed else m
                return m | {"clobTokenIds": "[]"} if yes(m) in self.no_token else m

            events = [ev | {"markets": [shut(m) for m in ev["markets"] if yes(m) not in self.omit]} for ev in events]
        return httpx.Response(200, json={"events": events, "next_cursor": None})

    def clob(self, req: httpx.Request) -> httpx.Response:
        self.calls.append("clob:books")
        if req.url.path != "/books" or req.method != "POST":
            return httpx.Response(404, text="not mocked")
        if self.clob_down:
            return httpx.Response(500, text="down")
        want = [x["token_id"] for x in json.loads(req.content)]
        # json.dumps writes Infinity / NaN as JSON literals, as a venue could (httpx's json= refuses them)
        body = json.dumps([self.books[t] for t in want if t in self.books]).encode()
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    def runtime(self, path: Path, clock: list[datetime], **kw) -> Runtime:
        tr = make_transports(State())
        tr["clevelandfed"] = httpx.MockTransport(self.cleveland)
        tr["polymarket:gamma"] = httpx.MockTransport(self.gamma)
        tr["polymarket:clob"] = httpx.MockTransport(self.clob)
        rt = Runtime(Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(path), **kw), transports=tr)
        rt.clevelandfed.http._limiter = TokenBucket(1000.0, 1000)
        rt.polymarket.gamma._limiter = TokenBucket(1000.0, 1000)  # the /markets/{id} confirmations (item 482)
        rt.now = lambda: clock[0]
        return rt

    def n(self, prefix: str) -> int:
        return sum(1 for c in self.calls if c.startswith(prefix))


async def _row(rt: Runtime, day: str = DAY) -> dict:
    row = await rt.store.pm_cpi_row(day)
    assert row is not None
    row["books_obj"] = json.loads(row["books"]) if row["books"] else None
    row["early_obj"] = json.loads(row["books_early"]) if row["books_early"] else None
    row["depth_obj"] = json.loads(zlib.decompress(row["books_depth_z"])) if row["books_depth_z"] else None
    return row


def _today_files() -> dict[str, object]:
    """Cleveland's copies of 26.11 as they would look after the update: a new headline point, stamp 26.11."""
    today = _with_stamp(_append_points(YEAR, "2026-11", [("11/26", "3.80")]), "2026-11-26 00:00")
    return {"nowcast_year.json": today, "nowcast_month.json": _with_stamp(MONTH, "2026-11-26 00:00")}


def _token(series: str, i: int) -> str:
    return json.loads(SAMPLE["events"][series][0]["markets"][i]["clobTokenIds"])[0]


async def test_holiday_row_is_written_stale_with_every_book(tmp_path):
    v, clock = Venues(), [T0]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["late"], r["cf_fresh"], r["cf_tries"], r["cf_fetches"]) == ("stale", 0, 0, 1, 1)
        assert (r["cf_stamp_year"], r["cf_last_day_year"], r["cf_stamp_month"]) == ("2026-11-25 00:00", "2026-11-25", "2026-11-25 00:00")
        assert (r["target"], r["prev_target"]) == ("2026-11", "2026-10")
        assert r["yoy_headline_pct"] == pytest.approx(3.79378308246552) and r["mom_core_pct"] == pytest.approx(0.21114452794251)
        assert r["prev_yoy_headline_pct"] == pytest.approx(3.74183045049836)
        assert json.loads(zlib.decompress(r["cf_year_z"])) == YEAR  # the file as received, all targets
        pts = json.loads(r["cf_points"])
        assert sorted(pts) == ["month", "year"] and pts["year"]["2026-11"]["core"][-1] == ["2026-11-25", pytest.approx(2.87325045473827)]
        assert (r["n_events"], r["n_brackets"], r["n_quoted"], r["missing"]) == (4, N_BRACKETS, N_BRACKETS, None)
        books = r["books_obj"]
        assert books["series_listed"] == list(SERIES)  # series 5 listed too (no open event)
        b37 = next(b for b in books["brackets"] if b["event_slug"] == "november-inflation-us-annual" and b["title"] == "3.7%")
        assert (b37["bid"], b37["ask"], b37["end_date"], b37["fee_type"], b37["book"]) == (
            0.45,
            0.47,
            "2026-12-10T13:30:00Z",
            "economics_fees",
            True,
        )
        assert b37["bid_usd"] == pytest.approx(0.45 * 9.48) and b37["ask_usd"] == pytest.approx(0.47 * 156.94)  # top level, USD
        assert b37["book_ts_ms"] == 1795720850624 and b37["read_ts"] == int(T0.timestamp())
        assert v.n("cf:") == 2 and v.n("gamma:") == len(SERIES) and v.n("clob:") == 1
        assert (r["books_paired"], r["books_early"], r["mom_target"], r["cf_year_ref"]) == (1, None, "2026-11", None)
        assert r["cf_year_sha"] == hashlib.sha256(json.dumps(YEAR).encode()).hexdigest()
        assert books["counts"]["10007"]["brackets"] == 12 and books["counts"]["5"] == {
            "events": 0,
            "brackets": 0,
            "ts": int(T0.timestamp()),
        }
        # remark 7: every level of every book, both sides
        depth = r["depth_obj"]
        assert len(depth["main"]) == N_BRACKETS and depth["early"] == []
        assert sum(len(d["bids"]) + len(d["asks"]) for d in depth["main"].values()) == sum(
            len(bk["bids"]) + len(bk["asks"]) for bk in SAMPLE["books"]
        )
        d37 = depth["main"][b37["token"]]
        assert d37["bids"][0] == [0.45, 9.48] and d37["asks"][0][0] == 0.47 and d37["read_ts"] == b37["read_ts"]

        # 10 min later: the books are complete and the retry of the stale file is not due yet: no request
        clock[0] = T0 + timedelta(minutes=10)
        assert await collect_pm_cpi_snapshot(rt) == 0 and len(v.calls) == 8
        # every 10 min to the end of the window: two more downloads 30 min apart, then nothing; books never re-read
        for minutes in range(20, 90, 10):
            clock[0] = T0 + timedelta(minutes=minutes)
            await collect_pm_cpi_snapshot(rt)
        r = await _row(rt)
        assert (r["status"], r["cf_fetches"], r["cf_tries"], r["n_quoted"]) == ("stale", 3, 3, N_BRACKETS)
        assert v.n("cf:") == 6 and v.n("clob:") == 1
        assert r["created_ts"] == int(T0.timestamp()) and r["late"] == 0
    finally:
        await rt.aclose()


async def test_fresh_file_on_the_retry_rereads_every_book_and_keeps_the_early_set(tmp_path):
    # blocker 1: 15:35 yesterday's file + books; 16:05 today's file -> the books are read again in the same tick
    v, clock = Venues(), [T0]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        first = {b["token"]: b for b in (await _row(rt))["books_obj"]["brackets"]}
        v.cf = _today_files()
        t1 = T0 + timedelta(minutes=30)
        clock[0] = t1
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["cf_fresh"], r["cf_fetches"], r["cf_last_day_year"]) == ("complete", 1, 2, "2026-11-26")
        assert r["yoy_headline_pct"] == pytest.approx(3.80) and r["cf_year_ts"] == int(t1.timestamp())
        since = max(r["cf_year_ts"], r["cf_month_ts"])
        main = r["books_obj"]["brackets"]
        assert len(main) == N_BRACKETS and all(b["read_ts"] >= since for b in main) and r["books_paired"] == 1
        early = r["early_obj"]["brackets"]
        assert {b["token"] for b in early} == set(first) and all(b["read_ts"] == int(T0.timestamp()) for b in early)
        assert all(b["superseded_ts"] == int(t1.timestamp()) for b in early)
        assert all({k: x for k, x in b.items() if k != "superseded_ts"} == first[b["token"]] for b in early)
        assert len(r["depth_obj"]["main"]) == N_BRACKETS and len(r["depth_obj"]["early"]) == N_BRACKETS
        assert v.n("clob:") == 2 and v.n("gamma:") == 2 * len(SERIES) and r["missing"] is None
        clock[0] = T0 + timedelta(minutes=70)
        assert await collect_pm_cpi_snapshot(rt) == 0 and v.n("cf:") == 4  # complete: no further download
    finally:
        await rt.aclose()


async def test_failed_reread_keeps_the_early_books_and_the_row_partial(tmp_path):
    v, clock = Venues(), [T0]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        first = (await _row(rt))["books_obj"]["brackets"]
        v.cf, v.clob_down = _today_files(), True
        clock[0] = T0 + timedelta(minutes=30)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["cf_fresh"], r["books_paired"], r["n_quoted"]) == ("partial", 1, 0, N_BRACKETS)
        assert r["books_obj"]["brackets"] == first and r["books_early"] is None  # nothing lost, nothing mixed silently
        assert {m["field"] for m in json.loads(r["missing"])} == {"books:0", "books_paired"}
        assert "39 book(s) read before the nowcast download at 16:05 UTC" in r["missing"]
        # 10 min later the CLOB is back: the re-read pairs the books, the early set is kept
        v.clob_down = False
        t2 = T0 + timedelta(minutes=40)
        clock[0] = t2
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["books_paired"], r["missing"]) == ("complete", 1, None)
        assert {b["read_ts"] for b in r["books_obj"]["brackets"]} == {int(t2.timestamp())}
        assert len(r["early_obj"]["brackets"]) == N_BRACKETS and v.n("cf:") == 4
    finally:
        await rt.aclose()


async def test_partial_reread_fills_the_rest_on_the_next_tick(tmp_path):
    v, clock = Venues(), [T0]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        tok = _token("10007", 4)
        kept = v.books.pop(tok)
        v.cf = _today_files()
        t1 = T0 + timedelta(minutes=30)
        clock[0] = t1
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        main = {b["token"]: b for b in r["books_obj"]["brackets"]}
        assert (r["status"], r["books_paired"]) == ("partial", 0)
        assert main[tok]["read_ts"] == int(T0.timestamp())  # the re-read missed it: its 15:35 book stays in main
        assert sum(1 for b in main.values() if b["read_ts"] == int(t1.timestamp())) == N_BRACKETS - 1
        assert len(r["early_obj"]["brackets"]) == N_BRACKETS - 1
        assert {m["field"] for m in json.loads(r["missing"])} == {"books", "books_paired"}
        v.books[tok] = kept
        clock[0] = T0 + timedelta(minutes=40)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["books_paired"]) == ("complete", 1)
        assert len(r["early_obj"]["brackets"]) == N_BRACKETS and v.n("clob:") == 3
        assert sum(1 for t in r["depth_obj"]["early"] if t["token"] == tok) == 1
    finally:
        await rt.aclose()


async def test_cleveland_down_keeps_the_books_and_a_later_tick_adds_the_nowcast(tmp_path, caplog):
    v, clock = Venues(), [T0]
    v.cf = {"nowcast_year.json": None, "nowcast_month.json": None}
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["cf_fresh"], r["cf_tries"], r["cf_fetches"], r["n_quoted"]) == ("partial", None, 1, 0, N_BRACKETS)
        assert {m["field"] for m in json.loads(r["missing"])} == {"nowcast_year", "nowcast_month"}
        assert "pm_cpi_snapshot 2026-11-26 (window): partial" in caplog.text and "nowcast_year: backend_error" in caplog.text
        # only the YoY file comes back: it is stored, the MoM one is still missing
        v.cf["nowcast_year.json"] = YEAR
        clock[0] = T0 + timedelta(minutes=10)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert r["cf_year_z"] is not None and r["cf_month_z"] is None and r["status"] == "partial"
        assert r["yoy_headline_pct"] == pytest.approx(3.79378308246552) and r["mom_headline_pct"] is None
        v.cf["nowcast_month.json"] = MONTH
        clock[0] = T0 + timedelta(minutes=20)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["cf_fresh"], r["cf_tries"], r["cf_fetches"], r["missing"]) == ("stale", 0, 3, 2, None)
        assert r["mom_headline_pct"] == pytest.approx(-0.0009670665748075)
        # each file stored after the books made them due again: re-read at +10 and +20 min, the earlier reads kept
        assert v.n("clob:") == 3 and r["books_paired"] == 1
        assert {b["read_ts"] for b in r["books_obj"]["brackets"]} == {int(clock[0].timestamp())}
        assert len(r["early_obj"]["brackets"]) == 2 * N_BRACKETS
    finally:
        await rt.aclose()


async def test_books_down_keeps_the_nowcast_and_a_later_tick_adds_the_books(tmp_path):
    v, clock = Venues(), [T0]
    v.clob_down = True
    v.gamma_down = {"11905"}
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["n_brackets"], r["n_quoted"], r["cf_fetches"]) == ("partial", N_BRACKETS - 10, 0, 1)
        assert r["books_obj"]["series_listed"] == ["5", "10007", "10008", "12054"]
        assert not any(b["book"] for b in r["books_obj"]["brackets"])
        assert {m["field"] for m in json.loads(r["missing"])} == {"events:11905", "books:0"}
        # 10 min later the venues are back: the books are filled, the nowcast is not downloaded again (retry not due)
        v.clob_down, v.gamma_down = False, set()
        clock[0] = T0 + timedelta(minutes=10)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["n_brackets"], r["n_quoted"], r["cf_fetches"], r["missing"]) == ("stale", N_BRACKETS, N_BRACKETS, 1, None)
        assert r["books_obj"]["series_listed"] == list(SERIES) and v.n("cf:") == 2
        assert {b["read_ts"] for b in r["books_obj"]["brackets"]} == {int(clock[0].timestamp())}
    finally:
        await rt.aclose()


async def test_partial_books_answer_fills_only_the_gap(tmp_path):
    v, clock = Venues(), [T0]
    lost = SAMPLE["events"]["10007"][0]["markets"][4]  # 3.7%
    tok = json.loads(lost["clobTokenIds"])[0]
    kept = v.books.pop(tok)
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["n_quoted"]) == ("partial", N_BRACKETS - 1)
        assert [m["field"] for m in json.loads(r["missing"])] == ["books"]
        first = {b["token"]: b for b in r["books_obj"]["brackets"]}
        v.books[tok] = kept
        clock[0] = T0 + timedelta(minutes=10)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["n_quoted"]) == ("stale", N_BRACKETS)
        again = {b["token"]: b for b in r["books_obj"]["brackets"]}
        assert again[tok]["read_ts"] == int(clock[0].timestamp())
        assert all(again[t] == b for t, b in first.items() if t != tok)  # the good books of 15:35 untouched
    finally:
        await rt.aclose()


async def test_restart_inside_the_window_neither_duplicates_nor_loses_the_day(tmp_path):
    v, clock = Venues(), [T0]
    v.clob_down = True
    path = tmp_path / "db.sqlite"
    rt = v.runtime(path, clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
    finally:
        await rt.aclose()
    # the service restarts (a merge): a new runtime on the same file continues the day's row
    v.clob_down = False
    clock[0] = T0 + timedelta(minutes=3)
    rt2 = v.runtime(path, clock)
    try:
        assert await collect_pm_cpi_snapshot(rt2) == 1
        r = await _row(rt2)
        assert (r["status"], r["n_quoted"], r["created_ts"], r["cf_fetches"]) == ("stale", N_BRACKETS, int(T0.timestamp()), 1)
        clock[0] = T0 + timedelta(minutes=5)
        assert await collect_pm_cpi_snapshot(rt2) == 0  # another restart right after: nothing due
    finally:
        await rt2.aclose()
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM pm_cpi_snapshot").fetchone()[0] == 1
    con.close()


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 11, 26, 15, 29, tzinfo=UTC),
        datetime(2026, 11, 26, 21, 5, tzinfo=UTC),
        datetime(2026, 11, 28, 15, 29, tzinfo=UTC),  # Saturday before the window
    ],
)
async def test_outside_the_window_makes_no_request(tmp_path, when):
    v, clock = Venues(), [when]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 0 and v.calls == []
    finally:
        await rt.aclose()


async def test_weekend_rows_are_written_once_and_reference_the_unchanged_file(tmp_path):
    # blocker 2: a row every day; on Sat/Sun one download, and bytes equal to Friday's are not stored again
    v = Venues()
    fri = datetime(2026, 11, 27, 15, 35, tzinfo=UTC)
    clock = [fri]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        f = await _row(rt, "2026-11-27")
        assert f["cf_year_z"] is not None and f["cf_year_ref"] is None and f["status"] == "stale"
        for day, name in (("2026-11-28", "Saturday"), ("2026-11-29", "Sunday")):
            start = datetime.fromisoformat(day + "T15:35:00+00:00")
            before = v.n("cf:")
            for minutes in range(0, 90, 10):  # every tick of the window
                clock[0] = start + timedelta(minutes=minutes)
                await collect_pm_cpi_snapshot(rt)
            r = await _row(rt, day)
            assert v.n("cf:") - before == 2, name  # one download of each file, no 30 min retry
            assert (r["status"], r["cf_fetches"], r["n_quoted"], r["books_paired"]) == ("stale", 1, N_BRACKETS, 1), name
            for kind in ("year", "month"):
                assert r[f"cf_{kind}_z"] is None and r[f"cf_{kind}_ref"] == "2026-11-27", name
                assert r[f"cf_{kind}_sha"] == f[f"cf_{kind}_sha"], name
            assert r["cf_stamp_year"] == "2026-11-25 00:00" and r["yoy_headline_pct"] == pytest.approx(3.79378308246552)
        # a new file on Monday is stored again
        v.cf = {"nowcast_year.json": _with_stamp(YEAR, "2026-11-30 00:00"), "nowcast_month.json": MONTH}
        clock[0] = datetime(2026, 11, 30, 15, 35, tzinfo=UTC)
        assert await collect_pm_cpi_snapshot(rt) == 1
        m = await _row(rt, "2026-11-30")
        assert m["cf_year_z"] is not None and m["cf_year_ref"] is None and m["cf_month_ref"] == "2026-11-27"
        assert await rt.store.pm_cpi_file_source("2026-12-01", "year") == ("2026-11-30", m["cf_year_sha"])
        assert await rt.store.pm_cpi_file_source("2026-12-01", "month") == ("2026-11-27", f["cf_month_sha"])
    finally:
        await rt.aclose()


async def test_an_empty_listing_of_a_main_series_is_a_gap(tmp_path):
    # remark 3: 0 open brackets in 10007 must not give 'stale' / 'complete' with an empty missing
    v, clock = Venues(), [T0]
    v.gamma_empty = {"10007"}
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["n_brackets"]) == ("partial", N_BRACKETS - 12)
        assert "10007" not in r["books_obj"]["series_listed"] and "5" in r["books_obj"]["series_listed"]
        assert r["books_obj"]["counts"]["10007"]["brackets"] == 0
        miss = json.loads(r["missing"])
        assert [m["field"] for m in miss] == ["events:10007"] and "0 open brackets" in miss[0]["detail"]
        v.gamma_empty = set()
        clock[0] = T0 + timedelta(minutes=10)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["n_brackets"], r["missing"]) == ("stale", N_BRACKETS, None)
    finally:
        await rt.aclose()


async def test_garbled_book_values_never_kill_the_tick(tmp_path):
    # remark 4: Infinity (json.loads accepts it), a huge number, text or a bool in the book time and levels
    v, clock = Venues(), [T0]
    toks = [_token("10007", i) for i in range(5)]
    for tok, bad in zip(toks, (float("inf"), 1e30, "abc", True, float("nan")), strict=True):
        v.books[tok] = v.books[tok] | {"timestamp": bad, "bids": [{"price": "Infinity", "size": "1"}, *v.books[tok]["bids"]]}
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        main = {b["token"]: b for b in r["books_obj"]["brackets"]}
        assert r["status"] == "stale" and all(main[t]["book_ts_ms"] is None for t in toks)
        assert all(all(0 < p < 1 for p, _ in r["depth_obj"]["main"][t]["bids"]) for t in toks)
    finally:
        await rt.aclose()


async def test_mom_values_follow_the_yoy_target(tmp_path):
    # remark 5: the MoM file already has the 'CPI Nov' line and a December chart; the YoY one not yet
    mom = copy.deepcopy(MONTH)
    mom[-1]["categories"][0]["category"].append({"label": "CPI Nov", "vline": "true"})
    dec = copy.deepcopy(mom[-1])
    dec["chart"]["subcaption"] = "2026-12"
    dec["categories"][0]["category"] = [c for c in dec["categories"][0]["category"] if not c.get("vline")]
    for ds in dec["dataset"]:
        ds["data"] = [{"value": "0.5"} for _ in ds["data"]]
    v, clock = Venues(), [T0]
    v.cf["nowcast_month.json"] = [*mom, dec]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["target"], r["mom_target"]) == ("2026-11", "2026-12")
        assert r["mom_headline_pct"] == pytest.approx(-0.0009670665748075)  # of 2026-11, as the YoY values
        pts = json.loads(r["cf_points"])["month"]
        assert pts["2026-12"]["headline"][-1][1] == 0.5  # the MoM file's own target is kept too
        miss = json.loads(r["missing"])
        assert [m["field"] for m in miss] == ["mom_target"] and "differs from the YoY file's 2026-11" in miss[0]["detail"]
        assert r["status"] == "stale"  # the row is usable; missing says the targets differ
    finally:
        await rt.aclose()


async def test_an_unparseable_file_keeps_its_bytes_and_the_books(tmp_path):
    # remark 10: the bytes and the books stay in the row, the file is marked in missing; a good file replaces it
    v, clock = Venues(), [T0]
    html = b"<html>maintenance</html>"
    v.cf["nowcast_year.json"] = html
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert zlib.decompress(r["cf_year_z"]) == html and r["cf_last_day_year"] is None and r["cf_stamp_year"] is None
        assert (r["status"], r["cf_fresh"], r["n_quoted"]) == ("partial", None, N_BRACKETS)
        assert r["target"] == "2026-11" and r["yoy_headline_pct"] is None  # target from the MoM file meanwhile
        miss = json.loads(r["missing"])
        assert [m["field"] for m in miss] == ["nowcast_year"] and "raw file kept" in miss[0]["detail"]
        # 10 min later the file parses, without a _comment stamp: it is stored, not rejected as older
        nostamp = copy.deepcopy(YEAR)
        for c in nostamp:
            c["chart"].pop("_comment", None)
        v.cf["nowcast_year.json"] = nostamp
        clock[0] = T0 + timedelta(minutes=10)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["cf_stamp_year"], r["cf_last_day_year"], r["status"], r["missing"]) == (None, "2026-11-25", "stale", None)
        assert json.loads(zlib.decompress(r["cf_year_z"])) == nostamp
    finally:
        await rt.aclose()


async def test_a_missed_window_is_filled_late_and_marked(tmp_path):
    v, clock = Venues(), [T0.replace(hour=18, minute=10)]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["late"], r["n_quoted"]) == ("stale", 1, N_BRACKETS)
        clock[0] = T0.replace(hour=19)
        assert await collect_pm_cpi_snapshot(rt) == 0  # stale, but no staleness retry after the window
    finally:
        await rt.aclose()


async def test_an_older_download_does_not_replace_the_stored_file(tmp_path):
    v, clock = Venues(), [T0]
    v.cf = {"nowcast_year.json": YEAR, "nowcast_month.json": None}
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        await collect_pm_cpi_snapshot(rt)
        # a CDN copy of the day before comes back for the YoY file while the MoM file is retried
        v.cf = {"nowcast_year.json": _with_stamp(YEAR, "2026-11-24 00:00"), "nowcast_month.json": MONTH}
        clock[0] = T0 + timedelta(minutes=10)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert r["cf_stamp_year"] == "2026-11-25 00:00" and r["cf_year_ts"] == int(T0.timestamp())
        assert r["cf_month_z"] is not None
    finally:
        await rt.aclose()


async def test_no_store_and_no_open_event_never_raise(tmp_path, caplog):
    v, clock = Venues(), [T0]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        rt.store = None
        assert await collect_pm_cpi_snapshot(rt) == 0 and v.calls == []
    finally:
        await rt.aclose()
    empty = {sid: [] for sid in SAMPLE["events"]}
    v2 = Venues()
    v2.gamma = lambda req: httpx.Response(200, json={"events": empty.get(req.url.params.get("series_id"), []), "next_cursor": None})  # type: ignore[method-assign]
    rt2 = v2.runtime(tmp_path / "db2.sqlite", [T0])
    try:
        assert await collect_pm_cpi_snapshot(rt2) == 1
        r = await _row(rt2)
        assert (r["status"], r["n_brackets"]) == ("partial", 0)
        fields = [m["field"] for m in json.loads(r["missing"])]
        assert fields == [f"events:{sid}" for sid in ("10007", "10008", "11905", "12054")] + ["brackets"]
        assert r["books_obj"]["series_listed"] == ["5"]
    finally:
        await rt2.aclose()


def test_job_off_by_default_switch_allow_listed_and_cadence_clamped(tmp_path, monkeypatch):
    s = Settings()
    assert s.collect_pm_cpi_enabled is False and s.collect_pm_cpi_seconds == 600
    assert allowed("TRADEDESK_COLLECT_PM_CPI_ENABLED") and allowed("TRADEDESK_COLLECT_PM_CPI_SECONDS")
    monkeypatch.setenv("TRADEDESK_COLLECT_PM_CPI_ENABLED", "1")
    assert Settings.from_env().collect_pm_cpi_enabled is True
    features = (Path(__file__).resolve().parents[1] / "deploy" / "features.env").read_text()
    assert "\nTRADEDESK_COLLECT_PM_CPI_ENABLED=1\n" in features

    def period(**kw) -> int | None:
        rt = Runtime(Settings(collector_enabled=True, db_path=str(tmp_path / "s.sqlite"), **kw))
        rt.now = lambda: T0
        job = next((j for j in build_default_scheduler(rt).jobs if j.name == "pm_cpi_snapshot"), None)
        return None if job is None else job.period_seconds

    assert period() is None
    assert period(collect_pm_cpi_enabled=True) == 600
    assert period(collect_pm_cpi_enabled=True, collect_pm_cpi_seconds=7200) == 1800  # always hits the 90 min window
    assert period(collect_pm_cpi_enabled=True, collect_pm_cpi_seconds=5) == 300  # finding 15: at least 5 min
    assert period(collect_pm_cpi_enabled=True, collect_pm_cpi_seconds=60) == 300


def test_health_spec_expects_every_day():
    spec = next(s for s in ch.SPECS if s.name == "pm_cpi_snapshot")
    # remark 8 / finding 13: a file counts only when it was read (raw bytes that do not parse have no last daily point)
    assert "cf_last_day_year IS NOT NULL" in spec.where and "cf_last_day_month IS NOT NULL" in spec.where
    mon = int(datetime(2026, 11, 23, tzinfo=UTC).timestamp())
    now = int(datetime(2026, 12, 1, 18, tzinfo=UTC).timestamp())  # Tuesday after the window
    got = [datetime.fromtimestamp(b, UTC).strftime("%a %d") for b in ch.expected_buckets(spec, mon, now)]
    assert got == ["Mon 23", "Tue 24", "Wed 25", "Thu 26", "Fri 27", "Sat 28", "Sun 29", "Mon 30", "Tue 01"]
    early = int(datetime(2026, 12, 1, 16, tzinfo=UTC).timestamp())  # Tuesday inside the window: not due yet
    assert len(ch.expected_buckets(spec, mon, early)) == 8
    day = 86_400
    stamps = [mon + i * day for i in range(9)]
    r = ch.series_health(spec, "", stamps, stamps[0], stamps[-1], now)
    assert (r["status"], r["share"], r["gaps"]) == ("ok", 1.0, 0)
    weekdays_only = [mon + i * day for i in (0, 1, 2, 3, 4, 7, 8)]
    r = ch.series_health(spec, "", weekdays_only, weekdays_only[0], weekdays_only[-1], now)
    # a missing weekend is a gap now (T-3 / T-1 can fall on it): 5 of the 7 window days
    assert (r["gaps"], r["share"]) == (1, pytest.approx(5 / 7))
    r = ch.series_health(spec, "", stamps[:-1], stamps[0], stamps[-2], now)
    assert r["trailing_gap_s"] == day and r in ch.alarms([r])  # one missed day is named in the brief


async def test_late_is_set_only_when_the_late_phase_brought_something(tmp_path):
    # remark 9
    v, clock = Venues(), [T0]
    v.clob_down = True
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        clock[0] = T0.replace(hour=17, minute=10)
        assert await collect_pm_cpi_snapshot(rt) == 1  # tried after 17:00, the CLOB still down
        r = await _row(rt)
        assert (r["late"], r["status"]) == (0, "partial")
        v.clob_down = False
        clock[0] = T0.replace(hour=17, minute=20)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["late"], r["status"], r["n_quoted"]) == (1, "stale", N_BRACKETS)
    finally:
        await rt.aclose()


async def test_migration_44_adds_the_table_to_a_v43_store(tmp_path):
    path = tmp_path / "v43.sqlite"
    con = sqlite3.connect(path)
    con.execute("PRAGMA synchronous=OFF")
    for v in sorted(MIGRATIONS):
        if v <= 43:
            for stmt in MIGRATIONS[v]:
                con.execute(stmt)
    con.execute("PRAGMA user_version = 43")
    con.commit()
    con.close()
    s = Store(path)
    await s.init()
    try:
        assert await s.version() == SCHEMA_VERSION >= 44
        row = {"day": DAY, "created_ts": 1, "updated_ts": 2, "status": "partial", "books": "{}"}
        assert await s.upsert_pm_cpi(row) == 1
        assert await s.upsert_pm_cpi(row | {"created_ts": 99, "updated_ts": 3, "status": "stale", "n_quoted": 5}) == 1
        got = await s.pm_cpi_row(DAY)
        assert (got["created_ts"], got["updated_ts"], got["status"], got["n_quoted"], got["late"]) == (1, 3, "stale", 5, 0)
        assert await s.pm_cpi_row("2026-11-27") is None
    finally:
        await s.aclose()


# ----------------------------------------------------------------------------------------------------- item 482


def test_mark_gone_leaves_a_closed_bracket_out_of_the_pair_checks():
    # finding 12 of review C, blocker 1 of review C2: only proof of a close marks a bracket gone, absence does not;
    # a bracket listed open again is back
    def br(tok, series="10007", read_ts=100):
        return {"series": series, "event_id": "1", "token": tok, "book": True, "read_ts": read_ts}

    old = [br("a", read_ts=200), br("b"), br("c", series="10008"), br("d", read_ts=200) | {"gone_ts": 150}]
    got = {b["token"]: b for b in mark_gone(old, ["b"], ["a", "d"], 300)}
    assert got["b"]["gone_ts"] == 300 and got["b"]["read_ts"] == 100 and got["b"]["book"] is True  # its book is kept
    assert "gone_ts" not in got["c"]  # not listed, not known closed: live
    assert "gone_ts" not in got["a"] and "gone_ts" not in got["d"]  # listed (again)
    assert mark_gone([br("b") | {"gone_ts": 150}], ["b"], [], 300)[0]["gone_ts"] == 150  # the first sight is kept
    books = {"series_listed": list(SERIES), "brackets": [br("a", read_ts=200), br("b") | {"gone_ts": 300}]}
    assert books_complete(books, SERIES, since=150) and books_paired(books, 150) == 1
    assert not books_complete(books | {"brackets": [br("b") | {"gone_ts": 300}]}, SERIES)  # nothing still listed
    assert books_paired(books | {"brackets": [br("b") | {"gone_ts": 300}]}, 150) is None


def test_proof_of_a_close_from_the_listing_and_from_gamma_markets():
    # blocker 1 of review C2: closed: true in a listing, or a /markets/{id} answer for this very token; real Gamma
    # answers of 2026-11-03 (tests/fixtures/polymarket_touch_closed_markets_2026-11-04.json, both closed)
    real = json.loads((Path(__file__).parent / "fixtures" / "polymarket_touch_closed_markets_2026-11-04.json").read_text())
    m = real["markets"]["632870"]
    tok = json.loads(m["clobTokenIds"])[0]
    assert confirms_close(m, tok)
    assert not confirms_close(m, json.loads(m["clobTokenIds"])[1])  # the NO token: another answer, no proof
    assert not confirms_close(m | {"closed": False}, tok) and not confirms_close(m | {"closed": "true"}, tok)
    assert not confirms_close(m | {"clobTokenIds": "[]"}, tok) and not confirms_close(None, tok) and not confirms_close([m], tok)
    events = [{"markets": [m, m | {"closed": False, "clobTokenIds": '["x", "y"]'}, "junk"]}, "junk", {"markets": None}]
    assert closed_tokens(events) == {tok}
    stored = {"series": "10007", "event_id": "1", "token": "t", "market_id": "7", "title": "3.7%", "book": True, "bid": 0.4,
              "read_ts": 5, "gone_ts": 9}  # fmt: skip
    assert as_listed(stored) == {"series": "10007", "event_id": "1", "event_slug": None, "end_date": None, "market_id": "7",
                                 "title": "3.7%", "fee_type": None, "token": "t", "book": False}  # fmt: skip


async def test_a_bracket_that_left_the_listing_does_not_hold_the_row_partial(tmp_path):
    # finding 12: 15:35 yesterday's file + 39 books; a bracket closes; 16:05 today's file -> 38 books re-read, the row
    # is complete, the closed bracket keeps its 15:35 book with gone_ts, and Gamma is not asked again
    v, clock = Venues(), [T0]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        tok = _token("10007", 4)  # 3.7 %
        v.cf, v.closed = _today_files(), {tok}
        t1 = T0 + timedelta(minutes=30)
        clock[0] = t1
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert (r["status"], r["books_paired"], r["missing"], r["n_brackets"]) == ("complete", 1, None, N_BRACKETS)
        main = {b["token"]: b for b in r["books_obj"]["brackets"]}
        assert (main[tok]["gone_ts"], main[tok]["read_ts"], main[tok]["book"]) == (int(t1.timestamp()), int(T0.timestamp()), True)
        assert sum(1 for b in main.values() if "gone_ts" in b) == 1
        assert len(r["early_obj"]["brackets"]) == N_BRACKETS - 1 and tok in r["depth_obj"]["main"]
        gamma = v.n("gamma:")
        for minutes in (40, 50, 60):
            clock[0] = T0 + timedelta(minutes=minutes)
            assert await collect_pm_cpi_snapshot(rt) == 0
        assert v.n("gamma:") == gamma
    finally:
        await rt.aclose()


async def test_health_counts_a_day_only_with_both_files_read(tmp_path):
    # finding 13: 25.11 stored, 26.11 the same bytes (references), 27.11 both files an HTML page: kept raw, not a day
    v, clock = Venues(), [datetime(2026, 11, 25, 15, 35, tzinfo=UTC)]
    path = tmp_path / "db.sqlite"
    rt = v.runtime(path, clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        clock[0] = T0
        assert await collect_pm_cpi_snapshot(rt) == 1
        assert (await _row(rt))["cf_year_ref"] == "2026-11-25"
        html = b"<html>are you a robot?</html>"
        v.cf = {"nowcast_year.json": html, "nowcast_month.json": html}
        clock[0] = datetime(2026, 11, 27, 15, 35, tzinfo=UTC)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt, "2026-11-27")
        assert r["cf_year_sha"] is not None and r["cf_month_sha"] is not None and r["n_quoted"] == N_BRACKETS
        assert (r["status"], r["yoy_headline_pct"]) == ("partial", None)
    finally:
        await rt.aclose()
    spec = next(s for s in ch.SPECS if s.name == "pm_cpi_snapshot")
    con = sqlite3.connect(path)
    try:
        got = [d for (d,) in con.execute(f"SELECT day FROM pm_cpi_snapshot WHERE {spec.where} ORDER BY day")]
    finally:
        con.close()
    assert got == ["2026-11-25", "2026-11-26"]


async def test_same_stamp_a_b_a_keeps_todays_other_bytes(tmp_path):
    # finding 14: 25.11 stores A; 26.11 15:35 brings B (same stamp and last point, a past point rewritten); 16:05 A
    # again: B stays stored in the 26.11 row (not replaced by a reference to A), and no book is re-read
    b_year = copy.deepcopy(YEAR)
    sep = next(c for c in b_year if c["chart"]["subcaption"] == "2026-9")
    next(d for d in sep["dataset"] if d["seriesname"] == "CPI Inflation")["data"][0] = {"value": "3.01"}
    v, clock = Venues(), [datetime(2026, 11, 25, 15, 35, tzinfo=UTC)]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        v.cf["nowcast_year.json"] = b_year
        clock[0] = T0
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert json.loads(zlib.decompress(r["cf_year_z"])) == b_year and r["cf_year_ref"] is None
        v.cf["nowcast_year.json"] = YEAR
        clock[0] = T0 + timedelta(minutes=30)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        assert r["cf_fetches"] == 2 and r["cf_year_ref"] is None
        assert json.loads(zlib.decompress(r["cf_year_z"])) == b_year
        assert r["cf_year_sha"] == hashlib.sha256(json.dumps(b_year).encode()).hexdigest()
        assert r["cf_year_ts"] == int(T0.timestamp()) and r["books_early"] is None and v.n("clob:") == 2
        assert (r["status"], r["books_paired"]) == ("stale", 1)
    finally:
        await rt.aclose()


async def test_books_attempts_outlast_a_late_file_at_the_shortest_tick(tmp_path):
    # finding 15: the job ticks at the scheduler's cadence for a 60 s setting; series 12054 empty all day keeps the
    # books due every tick; today's file comes on the third download (16:30): its tick still re-reads the books
    sched = Runtime(Settings(collector_enabled=True, collect_pm_cpi_enabled=True, collect_pm_cpi_seconds=60,
                             db_path=str(tmp_path / "s.sqlite")))  # fmt: skip
    sched.now = lambda: T0
    step = next(j for j in build_default_scheduler(sched).jobs if j.name == "pm_cpi_snapshot").period_seconds
    v, start = Venues(), datetime(2026, 11, 26, 15, 30, tzinfo=UTC)
    v.gamma_empty = {"12054"}
    clock = [start]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        while clock[0] < start.replace(hour=17):
            if clock[0] >= start.replace(hour=16, minute=25):
                v.cf = _today_files()
            await collect_pm_cpi_snapshot(rt)
            clock[0] += timedelta(seconds=step)
        r = await _row(rt)
        assert (r["cf_fresh"], r["cf_fetches"], r["status"]) == (1, 3, "partial")  # 12054 never listed: partial
        assert r["cf_year_ts"] == int(start.replace(hour=16, minute=30).timestamp())
        assert r["books_paired"] == 1 and r["books_tries"] < 40
        assert {b["read_ts"] for b in r["books_obj"]["brackets"]} == {r["cf_year_ts"]}
    finally:
        await rt.aclose()


# ------------------------------------------------------------------------------------- item 482, round 2 (review C2)


@pytest.mark.parametrize("how", ["omit", "no_token"])
async def test_one_listing_without_live_brackets_still_rereads_them_on_the_file_tick(tmp_path, how):
    # blocker 1 (S1): on the tick that stores today's file, one Gamma answer lacks 5 live markets of 10007 (left out,
    # or listed without a token). Absence is no proof of a close: their stored tokens go into the same /books, every
    # book pairs with the file, no gone_ts; the books came back, so no /markets/{id} request is needed
    v, clock = Venues(), [T0]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        toks = {_token("10007", i) for i in range(5)}
        v.cf = _today_files()
        setattr(v, how, toks)
        t1 = T0 + timedelta(minutes=30)
        clock[0] = t1
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        main = {b["token"]: b for b in r["books_obj"]["brackets"]}
        assert (r["status"], r["books_paired"], r["missing"], r["n_brackets"]) == ("complete", 1, None, N_BRACKETS)
        assert not any("gone_ts" in b for b in main.values())
        assert all(main[t]["read_ts"] == int(t1.timestamp()) and main[t]["book"] for t in toks)
        assert main[_token("10007", 0)]["title"] == SAMPLE["events"]["10007"][0]["markets"][0]["groupItemTitle"]
        assert len(r["early_obj"]["brackets"]) == N_BRACKETS and v.n("market:") == 0
        setattr(v, how, set())
        clock[0] = T0 + timedelta(minutes=40)
        assert await collect_pm_cpi_snapshot(rt) == 0  # complete: nothing due
    finally:
        await rt.aclose()


async def test_a_bracket_without_a_book_dropped_by_the_listing_is_still_read(tmp_path):
    # blocker 1 (S2): a bracket got no book at 15:35; the next listing lacks it. It stays live: its stored token is
    # asked again, Gamma /markets/{id} says it is open, the row stays partial with 'books' in missing until the book comes
    v, clock = Venues(), [T0]
    tok = _token("10007", 4)
    kept = v.books.pop(tok)
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        v.omit = {tok}
        clock[0] = T0 + timedelta(minutes=10)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        b = {x["token"]: x for x in r["books_obj"]["brackets"]}[tok]
        assert (r["status"], b["book"], "gone_ts" in b) == ("partial", False, False)
        miss = json.loads(r["missing"])
        assert [m["field"] for m in miss] == ["books"] and "not in the listing" in miss[0]["detail"]
        assert v.n("market:") == 1
        v.books[tok] = kept
        t2 = T0 + timedelta(minutes=20)
        clock[0] = t2
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        b = {x["token"]: x for x in r["books_obj"]["brackets"]}[tok]
        assert (r["status"], r["books_paired"], r["missing"]) == ("stale", 1, None)
        assert (b["read_ts"], "gone_ts" in b) == (int(t2.timestamp()), False)
        assert v.n("market:") == 1  # the book came back: no confirmation asked
    finally:
        await rt.aclose()


async def test_an_event_gone_from_the_listing_is_gone_only_once_gamma_says_closed(tmp_path):
    # blocker 1: the 10007 event leaves the listing entirely (release day) and the CLOB has no book of it. A failed
    # /markets/{id} keeps its 12 brackets live (partial, recorded in missing); on the next tick Gamma says closed: they
    # get gone_ts, keep their 15:35 books, and the row is complete
    v, clock = Venues(), [T0]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        toks = {_token("10007", i) for i in range(12)}
        v.cf, v.gamma_empty, v.market_down = _today_files(), {"10007"}, True
        for t in toks:
            v.books.pop(t)
        clock[0] = T0 + timedelta(minutes=30)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        main = {b["token"]: b for b in r["books_obj"]["brackets"]}
        assert (r["status"], r["books_paired"], r["n_brackets"]) == ("partial", 0, N_BRACKETS)
        assert not any("gone_ts" in b for b in main.values())
        fields = [m["field"] for m in json.loads(r["missing"])]
        assert {"events:10007", "books", "books_paired"} <= set(fields)
        assert sum(1 for f in fields if f.startswith("closed:")) == 12
        asked = v.n("market:")
        v.market_down, v.resolved = False, toks
        t2 = T0 + timedelta(minutes=40)
        clock[0] = t2
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt)
        main = {b["token"]: b for b in r["books_obj"]["brackets"]}
        assert (r["status"], r["books_paired"], r["n_brackets"]) == ("complete", 1, N_BRACKETS)
        assert {t for t, b in main.items() if "gone_ts" in b} == toks
        want = (int(t2.timestamp()), int(T0.timestamp()), True)
        assert all((main[t]["gone_ts"], main[t]["read_ts"], main[t]["book"]) == want for t in toks)
        assert [m["field"] for m in json.loads(r["missing"])] == ["events:10007"]  # the empty main series, said honestly
        assert v.n("market:") - asked == 12
        clock[0] = T0 + timedelta(minutes=50)
        assert await collect_pm_cpi_snapshot(rt) == 0
    finally:
        await rt.aclose()


# ------------------------------------------------------------------------- item 483 (findings 4-5 of review C2, round 2)


def test_to_confirm_asks_every_candidate_in_turn_as_the_list_changes():
    assert to_confirm(["a", "b", "c"], {"b": {"confirm_ts": 5}}, 2) == ["a", "c"]  # never asked first, listing order
    assert to_confirm(["a", "b"], {"a": {"confirm_ts": 9}, "b": {"confirm_ts": 5}}, 5) == ["b", "a"]  # oldest first
    assert to_confirm(["a"], {}, 0) == [] and to_confirm([], {}, 20) == []
    # 45 candidates, the first 20 never confirm and the others close when asked; new ones appear ahead of them in the
    # listing order on ticks 2 and 4, the closed ones leave: none waits ceil(n / limit) = 3 ticks or more (n = 55)
    stuck = {str(i) for i in range(20)}
    cands, stored, wait, seen = [str(i) for i in range(45)], {}, {}, set()
    for tick in range(1, 13):
        if tick in (2, 4):
            cands = [f"n{tick}.{i}" for i in range(5)] + cands
        seen |= set(cands)
        asked = to_confirm(cands, stored, CONFIRM_MAX)
        assert len(asked) == min(CONFIRM_MAX, len(cands)) and len(set(asked)) == len(asked)
        for t in cands:
            wait[t] = 0 if t in asked else wait.get(t, 0) + 1
            assert wait[t] < 3, (tick, t)
        stored |= {t: {"confirm_ts": tick} for t in asked}
        cands = [t for t in cands if t in stuck or t not in asked]
    assert set(cands) == stuck and seen <= set(stored)  # every candidate was asked, only the stuck ones are left


@pytest.mark.parametrize("how", ["down", "open"])
async def test_close_confirmations_rotate_past_brackets_that_never_confirm(tmp_path, caplog, how):
    # findings 4-5 of review C2: every bracket leaves the listing (all series answer empty) and the CLOB has no book.
    # The first CONFIRM_MAX in the stored order never confirm (/markets/{id} 500, or Gamma calls them open), the other
    # 19 are closed. At most CONFIRM_MAX requests a tick, and the next tick asks the 19: they get gone_ts and keep their
    # 15:35 books; the 20 stay live (no gone_ts without proof), so the row stays partial
    v, clock = Venues(), [T0]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        stored = (await _row(rt))["books_obj"]["brackets"]
        order, by_mid = [b["token"] for b in stored], {b["market_id"]: b["token"] for b in stored}
        assert len(order) == N_BRACKETS > CONFIRM_MAX
        stuck, rest = set(order[:CONFIRM_MAX]), set(order[CONFIRM_MAX:])
        # today's file at 16:05 makes every 15:35 book due again (the re-read finds none)
        v.cf, v.gamma_empty, v.books, v.resolved = _today_files(), set(SERIES), {}, rest
        v.down_markets = stuck if how == "down" else set()
        asked: list[set[str]] = []
        for k in (1, 2, 3):
            n0 = len(v.calls)
            clock[0] = T0 + timedelta(minutes=20 + 10 * k)
            with caplog.at_level(logging.INFO, logger="tradedesk.collector"):
                assert await collect_pm_cpi_snapshot(rt) == 1
            calls = [c[len("market:") :] for c in v.calls[n0:] if c.startswith("market:")]
            assert len(calls) <= CONFIRM_MAX and len(set(calls)) == len(calls)
            asked.append({by_mid[m] for m in calls})
            r = await _row(rt)
            main = {b["token"]: b for b in r["books_obj"]["brackets"]}
            gone = {t for t, b in main.items() if b.get("gone_ts")}
            assert r["status"] == "partial" and not gone & stuck  # the stuck ones are never taken for closed
            if k == 1:
                assert asked[0] == stuck and not gone
                assert all(main[t]["confirm_ts"] == int(clock[0].timestamp()) for t in stuck)
            if k == 2:
                assert rest <= asked[1] and len(asked[1]) == CONFIRM_MAX  # the 19 never asked, then the oldest stuck one
                assert gone == rest and all(main[t]["book"] and main[t]["read_ts"] == int(T0.timestamp()) for t in rest)
        assert asked[2] == stuck and r["n_brackets"] == N_BRACKETS  # the closed ones are not asked again
        assert f"{N_BRACKETS} bracket(s) to confirm closed, {CONFIRM_MAX} asked this tick, the rest on the next ticks" in caplog.text
    finally:
        await rt.aclose()


async def test_a_b_a_keeps_b_when_the_latest_stored_earlier_copy_is_raw(tmp_path):
    # finding 2 of review C2 (S3): 25.11 A, 26.11 an HTML page kept raw, 27.11 15:35 B (same stamp and points), 16:05 A:
    # A is an earlier row's copy (25.11), so B stays in the 27.11 row
    b_year = copy.deepcopy(YEAR)
    sep = next(c for c in b_year if c["chart"]["subcaption"] == "2026-9")
    next(d for d in sep["dataset"] if d["seriesname"] == "CPI Inflation")["data"][0] = {"value": "3.01"}
    v, clock = Venues(), [datetime(2026, 11, 25, 15, 35, tzinfo=UTC)]
    rt = v.runtime(tmp_path / "db.sqlite", clock)
    try:
        assert await collect_pm_cpi_snapshot(rt) == 1
        html = b"<html>x</html>"
        v.cf = {"nowcast_year.json": html, "nowcast_month.json": html}
        clock[0] = T0
        assert await collect_pm_cpi_snapshot(rt) == 1
        v.cf = {"nowcast_year.json": b_year, "nowcast_month.json": MONTH}
        clock[0] = datetime(2026, 11, 27, 15, 35, tzinfo=UTC)
        assert await collect_pm_cpi_snapshot(rt) == 1
        v.cf = {"nowcast_year.json": YEAR, "nowcast_month.json": MONTH}
        clock[0] = datetime(2026, 11, 27, 16, 5, tzinfo=UTC)
        assert await collect_pm_cpi_snapshot(rt) == 1
        r = await _row(rt, "2026-11-27")
        assert r["cf_fetches"] == 2 and r["cf_year_ref"] is None
        assert json.loads(zlib.decompress(r["cf_year_z"])) == b_year
        assert r["cf_year_sha"] == hashlib.sha256(json.dumps(b_year).encode()).hexdigest()
        a_sha = hashlib.sha256(json.dumps(YEAR).encode()).hexdigest()
        assert await rt.store.pm_cpi_sha_day("2026-11-27", "year", a_sha) == "2026-11-25"
        assert await rt.store.pm_cpi_sha_day("2026-11-25", "year", a_sha) is None
    finally:
        await rt.aclose()
