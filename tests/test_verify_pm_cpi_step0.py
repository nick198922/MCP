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


def test_fred_mom_and_prev_months():
    csv = "observation_date,CPILFESL\n2024-01-01,100.0\n2024-02-01,101.0\n2024-03-01,.\n2024-04-01,102.01\n"
    mom = m._fred_mom(csv)
    assert mom["2024-02"] == pytest.approx(1.0)
    # no monthly change across a missing month (it would be a 2-month move)
    assert "2024-03" not in mom and "2024-04" not in mom
    assert "2024-01" not in mom
    assert m._prev_months("2025-02", 3) == ["2025-01", "2024-12", "2024-11"]
