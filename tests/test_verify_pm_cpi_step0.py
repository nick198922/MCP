"""Parsers of scripts/verify_pm_cpi_step0.py: Cleveland Fed nowcast chart JSON, Polymarket CPI rule text, FRED CSV."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_pm_cpi_step0 as m  # noqa: E402


def _chart(target: str, labels: list[str], values: list[str]) -> dict:
    cats = [{"label": x, "vline": "true"} if x.startswith(("CPI ", "PCE ")) else {"label": x} for x in labels]
    return {
        "chart": {"subcaption": target},
        "categories": [{"category": cats}],
        "dataset": [{"seriesname": "CPI Inflation", "data": [{"value": v} for v in values]}],
    }


def test_cf_daily_aligns_values_skipping_vlines_and_rolls_year():
    # December target: points run into January of the next year; vlines carry no data point
    ch = _chart("2025-12", ["12/30", "12/31", "CPI Nov", "01/02", "01/12", "CPI Dec", "01/14"], ["2.5", "2.6", "", "2.7", "2.8"])
    target, pts, vl = m.cf_daily(ch, "CPI Inflation")
    assert target == "2025-12"
    assert pts == [("2025-12-30", 2.5), ("2025-12-31", 2.6), ("2026-01-02", None), ("2026-01-12", 2.7), ("2026-01-14", 2.8)]
    # release label -> date of the last point before the vline
    assert vl == {"CPI Nov": "2025-12-31", "CPI Dec": "2026-01-12"}


def test_cf_daily_missing_series_gives_none_values():
    ch = _chart("2026-3", ["03/02", "03/03"], ["1.0", "1.1"])
    _, pts, _ = m.cf_daily(ch, "Core CPI Inflation")
    assert pts == [("2026-03-02", None), ("2026-03-03", None)]


def test_release_date_and_kind_from_rule_text():
    ev = {
        "markets": [
            {
                "description": "This is a market about inflation over the 12-month period ending October 2026, before seasonal "
                "adjustment ... currently scheduled to be released on November 10, 2026, at 8:30 AM ET."
            }
        ]
    }
    assert m.release_date(ev) == "2026-11-10"
    assert m._kind(ev) == "yoy"
    core = {"markets": [{"description": "one-month percent change in the CPI excluding food and energy ... released on May 2, 2025"}]}
    assert m._kind(core) == "core_mom"
    assert m.release_date({"markets": [{"description": "no date here"}]}) is None


def test_target_month_from_slug_and_end_date():
    # the rule text of this event says 'January 13, 2025' (a year typo); the slug and endDate give December 2025
    assert m.target_month("december-inflation-us-monthly", "2026-01-13") == "2025-12"
    assert m.target_month("september-inflation-annual", "2025-10-15") == "2025-9"
    assert m.target_month("january-inflation-us-annual-lower-brackets-817", "2026-02-11") == "2026-1"
    assert m.target_month("core-cpi-mom-november-2026", "2026-12-10") == "2026-11"
    # 'from nov to dec': the reference month is the last month word
    assert m.target_month("us-inflation-03pt-from-nov-to-dec-2024", "2025-01-15") == "2024-12"
    assert m.target_month("fed-decision-in-march", None) is None
    assert m.target_month("cpi-above-3pct", "2025-01-15") is None


def test_release_days_from_cleveland_markers_and_event_release():
    charts = [
        _chart("2025-9", ["10/14", "10/15", "10/23", "10/24", "CPI Sep"], ["2.9", "2.9", "3.0", ""]),
        # October 2025: no 'CPI Oct' line (never published)
        _chart("2025-10", ["11/12", "11/13", "PCE Oct"], ["3.0", "3.0"]),
        _chart("2026-1", ["02/12", "02/13", "CPI Jan"], ["2.5", ""]),
    ]
    rel = m.cf_release_days(charts)
    # the release-day category is before the line even when its value is empty
    assert rel == {"2025-9": "2025-10-24", "2026-1": "2026-02-13"}
    stale = {"slug": "september-inflation-annual", "endDate": "2025-10-15T12:00:00Z"}
    assert m.event_release(stale, rel) == ("2025-9", "2025-10-24")  # rule text and endDate still say 15.10
    assert m.event_release({"slug": "october-inflation-annual", "endDate": "2025-11-13T12:00:00Z"}, rel) == ("2025-10", None)
    assert m.event_release({"slug": "january-inflation-us-annual", "endDate": "2026-02-11T12:00:00Z"}, rel)[1] == "2026-02-13"


def test_anchor_ts_is_0830_new_york_across_dst():
    from datetime import UTC, datetime

    assert datetime.fromtimestamp(m.anchor_ts("2026-12-10"), UTC).strftime("%Y-%m-%d %H:%M") == "2026-12-10 13:30"
    assert datetime.fromtimestamp(m.anchor_ts("2026-07-15"), UTC).strftime("%Y-%m-%d %H:%M") == "2026-07-15 12:30"


def test_release_coverage_counts_a_release_once_and_needs_every_event():
    full = [{"t7": True, "t3": True, "t1": True}] * 3
    rows = [
        {"kind": "yoy", "release": "2026-02-13", "markets": full},
        # second event of the same release ('...-lower-brackets') misses one bracket at T-3
        {"kind": "yoy", "release": "2026-02-13", "markets": [{"t7": True, "t3": False, "t1": True}]},
        {"kind": "yoy", "release": "2026-03-11", "markets": full},
        {"kind": "mom", "release": "2026-03-11", "markets": []},
    ]
    cov = m.release_coverage(rows)
    assert cov["yoy"] == {"releases": 2, "T-7": 2, "T-3": 1, "T-1": 2}
    assert cov["mom"] == {"releases": 1, "T-7": 0, "T-3": 0, "T-1": 0}


def test_compare_points_finds_an_overwritten_day_and_ignores_new_days():
    old = [_chart("2026-11", ["11/24", "11/25"], ["2.5", "2.6"])]
    same_plus_new_day = [_chart("2026-11", ["11/24", "11/25", "11/27"], ["2.5", "2.6", "2.7"])]
    c, diffs = m.compare_points(old, same_plus_new_day, "CPI Inflation")
    assert (c["pairs"], c["equal"], c["differ"]) == (2, 2, 0) and diffs == []
    rewritten = [_chart("2026-11", ["11/24", "11/25", "11/27"], ["2.5", "2.65", "2.7"])]
    c, diffs = m.compare_points(old, rewritten, "CPI Inflation")
    assert c["differ"] == 1 and diffs[0][1:] == ("2026-11", "2026-11-25")
    assert diffs[0][0] == pytest.approx(0.05)
    c, _ = m.compare_points(old, [], "CPI Inflation")
    assert c["target_missing_new"] == 1


def test_window_moves_counts_distinct_values_in_t7_t1():
    pts = [("2026-11-02", 3.0), ("2026-11-03", 3.1), ("2026-11-04", 3.1), ("2026-11-09", 3.2), ("2026-11-10", None)]
    assert m.window_moves(pts, "2026-11-10") == (3, 2)  # 11-03 .. 11-09
    assert m.window_moves(pts, "2026-11-10", lo=400) == (4, 3)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pm_cpi_snapshot_2026-11-26.json"


def _later_year_copy():
    """The sample's YoY file one week on: headline 2026-11 got 11/27 and 11/30 (a weekly step), stamp 30.11."""
    import copy
    import json

    year = json.loads(FIXTURE.read_text())["nowcast_year"]
    new = copy.deepcopy(year)
    nov = new[-1]
    cats = nov["categories"][0]["category"]
    tail = cats.pop()  # the 'PCE Oct' line closes the chart
    for lab, v in (("11/27", "3.84"), ("11/30", "3.84")):
        cats.append({"label": lab})
        for ds in nov["dataset"]:
            ds["data"].append({"value": v if ds["seriesname"] == "CPI Inflation" else "2.87325045473827"})
    cats.append(tail)
    for c in new:
        c["chart"]["_comment"] = "2026-11-30 00:00"
    return year, new


def test_compare_prints_new_points_after_the_old_stamp(tmp_path, capsys):
    import json

    year, new = _later_year_copy()
    (tmp_path / "old.json").write_text(json.dumps(year))
    (tmp_path / "new.json").write_text(json.dumps(new))
    m.compare_capture(tmp_path / "old.json", tmp_path / "new.json", ("CPI Inflation", "Core CPI Inflation"))
    out = capsys.readouterr().out
    assert "differ': 0" in out and "WARNING: same file stamp" not in out
    assert (
        "CPI Inflation      2026-11  after 2026-11-25: 2 points (2026-11-27 .. 2026-11-30), distinct values 1, new value vs old last: True"
        in out
    )
    assert (
        "Core CPI Inflation 2026-11  after 2026-11-25: 2 points (2026-11-27 .. 2026-11-30), distinct values 1, new value vs old last: False"
        in out
    )
    assert "CPI Inflation      2026-10  after 2026-11-25: 0 points, distinct values 0, new value vs old last: False" in out


def test_snapshot_series_falls_back_to_the_default_list(tmp_path):
    assert m.snapshot_series(tmp_path, ["10007"]) == (["10007"], None)
    sids, warn = m.snapshot_series(tmp_path, [])  # no events/ at all (the cache was emptied)
    assert sids == ["5", "10007", "10008", "11905", "12054"] and warn.startswith("WARNING: no series")
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "10008_open.json").write_text("[]")
    (tmp_path / "events" / "5_open.json").write_text("[]")
    assert m.snapshot_series(tmp_path, []) == (["10008", "5"], None)


async def _db_with_rows(path):
    import hashlib
    import json
    import zlib

    from tradedesk.store import Store

    year, new = _later_year_copy()
    br = {"series": "10007", "event_id": "1", "event_slug": "november-inflation-us-annual", "end_date": "2026-12-10T13:30:00Z",
          "title": "3.7%", "token": "t", "book": True, "bid": 0.45, "ask": 0.47, "bid_usd": 4.27, "ask_usd": 73.76,
          "book_ts_ms": 1796484899000, "read_ts": 1796484900}  # fmt: skip
    books = {"series_listed": ["10007"], "counts": {"10007": {"events": 1, "brackets": 1}}, "brackets": [br]}
    early = {"brackets": [br | {"bid": 0.41, "read_ts": 1796483100, "book_ts_ms": 1796483099000, "superseded_ts": 1796484900}]}
    gone = br | {"token": "u", "title": "3.6%", "read_ts": 1796483100, "gone_ts": 1796484900}  # item 482: its market closed
    depth = {"main": {"t": {"read_ts": 1796484900, "bids": [[0.45, 9.48], [0.44, 10]], "asks": [[0.47, 156.94]]}}, "early": []}
    s = Store(path)
    await s.init()
    try:
        for day, charts, stamp, ref in (
            ("2026-11-26", year, "2026-11-25 00:00", None),
            ("2026-11-28", new, "2026-11-30 00:00", None),
            ("2026-11-29", new, "2026-11-30 00:00", "2026-11-28"),  # a weekend row: the same bytes, a reference only
            ("2026-12-01", new, "2026-11-30 00:00", "2026-11-28"),
        ):
            raw = json.dumps(charts).encode()
            await s.upsert_pm_cpi(
                {
                    "day": day,
                    "created_ts": 1796484600,
                    "updated_ts": 1796484900,
                    "status": "complete",
                    "cf_year_z": None if ref else zlib.compress(raw),
                    "cf_year_sha": hashlib.sha256(raw).hexdigest(),
                    "cf_year_ref": ref,
                    "cf_stamp_year": stamp,
                    "target": "2026-11",
                    "mom_target": "2026-11",
                    "yoy_headline_pct": 3.84,
                    "books": json.dumps(books if day != "2026-11-28" else books | {"brackets": [br, gone]}),
                    "books_paired": 1,
                    "books_early": json.dumps(early) if day == "2026-12-01" else None,
                    "books_depth_z": zlib.compress(json.dumps(depth).encode()),
                    "n_events": 1,
                    "n_brackets": 1,
                    "n_quoted": 1,
                }  # fmt: skip
            )
    finally:
        await s.aclose()


def test_row_stage_reads_the_job_row_read_only(tmp_path, capsys):
    import asyncio
    import json

    db = tmp_path / "td.sqlite"
    asyncio.run(_db_with_rows(db))
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    assert m.stage_row(db, "2026-12-01", tmp_path / "export") is True
    out = capsys.readouterr().out
    assert "row 2026-12-01: status complete, late 0" in out
    assert "target 2026-11: YoY headline 3.84" in out
    assert "books (main set): series listed ['10007'], events 1, brackets 1, with bid and ask 1" in out
    assert "paired with the nowcast 1" in out and "same bytes as row 2026-11-28" in out
    # remark 11: the venue's book time next to the read time; the early set with its own times
    assert "book 15:34:59 UTC  read 15:35:00 UTC" in out
    assert "books (early set, read before a nowcast file stored later): 1 books" in out
    assert "book 15:04:59 UTC  read 15:05:00 UTC  superseded 15:35:00 UTC" in out
    assert "full books:" in out and "main 1 books / 3 levels, early 0 books" in out
    # the earlier row is the weekend one (same bytes): resolved through its reference, nothing to compare
    assert "vintage nowcast_year: row 2026-11-29 has the same bytes (sha256): no update in between" in out
    assert "vintage nowcast_month: a copy is missing" in out
    assert json.loads((tmp_path / "export" / "2026-12-01_nowcast_year.json").read_text())[-1]["chart"]["_comment"] == "2026-11-30 00:00"
    assert m.stage_row(db, "2026-11-28") is True
    out = capsys.readouterr().out
    assert "vintage nowcast_year: row 2026-11-26 (stamp '2026-11-25 00:00') vs row 2026-11-28" in out
    assert "new value vs old last: True" in out
    assert "read 15:05:00 UTC  closed (seen 15:35:00 UTC), not in the pair check" in out  # item 482, finding 12
    assert out.count("not in the pair check") == 1
    # mode=ro: the database file is not written (SQLite may still create its -shm / -wal side files to read a WAL file)
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    assert m.stage_row(db, "2026-12-02") is False and "NO ROW for 2026-12-02" in capsys.readouterr().out
    assert m.stage_row(tmp_path / "missing.sqlite", "2026-12-01") is False
    assert "cannot read pm_cpi_snapshot" in capsys.readouterr().out


def test_fred_mom_and_prev_months():
    csv = "observation_date,CPILFESL\n2024-01-01,100.0\n2024-02-01,101.0\n2024-03-01,.\n2024-04-01,102.01\n"
    mom = m._fred_mom(csv)
    assert mom["2024-02"] == pytest.approx(1.0)
    # no monthly change across a missing month (it would be a 2-month move)
    assert "2024-03" not in mom and "2024-04" not in mom
    assert "2024-01" not in mom
    assert m._prev_months("2025-02", 3) == ["2025-01", "2024-12", "2024-11"]


def test_stage_sigma_reads_the_cached_cpi_and_the_given_nowcast_file(tmp_path, capsys):
    # the archive SD of the registration (card п. 4); no network when the CSV vintage is cached
    import json

    fred = tmp_path / "cleveland" / "fred"
    fred.mkdir(parents=True)
    csv = "observation_date,CPIAUCNS\n2023-01-01,100.0\n2024-01-01,103.05\n2023-02-01,100\n2024-02-01,.\n"
    (fred / "CPIAUCNS_20241201.csv").write_text(csv)
    cats = [{"label": "02/05"}, {"label": "02/10"}, {"vline": "true", "label": "CPI Jan"}]
    chart = {"chart": {"subcaption": "2024-1"}, "categories": [{"category": cats}],
             "dataset": [{"seriesname": "CPI Inflation", "data": [{"value": 3.0}, {"value": 3.2}]}]}  # fmt: skip
    nc = tmp_path / "nowcast_year.json"
    nc.write_text(json.dumps([chart]))
    m.stage_sigma(tmp_path, nc, "2024-12-01", vintage="20241201")
    out = capsys.readouterr().out
    # release 2024-02-10 -> point on or before 02-07 is 02/05 (3.0); A = 3.05 rounded half-up = 3.1
    assert "n 1 (2024-1..2024-1)" in out and "sigma_A 0.1" in out
