"""Direct tests for analytics/sentiment.py (Fear & Greed summary).

Expected values are computed by hand on synthetic series. Day d is ts = d * 86400.
Series A: days 0..30, value = day + 20 (20..50), so latest = 50 on day 30.
  7d window (anchor day 30, cutoff day 23): days 23..30 = 43..50 -> mean 46.5
  30d window (cutoff day 0): all 31 values 20..50 -> mean 35
  change_7d = 50 - 43 = 7, change_30d = 50 - 20 = 30
  zones: 20..24 extreme_fear (5), 25..44 fear (20), 45..50 neutral (6) of 31
"""

from __future__ import annotations

import pytest

from tradedesk.analytics.sentiment import _norm, _zone, summarise_fear_greed

DAY = 86_400


def _row(day: int, value, cls: str | None = None) -> dict:
    r = {"value": str(value), "timestamp": str(day * DAY)}
    if cls is not None:
        r["value_classification"] = cls
    return r


def _series_a() -> list[dict]:
    return [_row(d, d + 20) for d in range(31)]


@pytest.mark.parametrize(
    ("value", "zone"),
    [
        (0, "extreme_fear"),
        (24, "extreme_fear"),
        (25, "fear"),
        (44, "fear"),
        (45, "neutral"),
        (54, "neutral"),
        (55, "greed"),
        (74, "greed"),
        (75, "extreme_greed"),
        (100, "extreme_greed"),
        (101, "neutral"),  # outside every band: fallback
    ],
)
def test_zone_boundaries(value, zone):
    assert _zone(value) == zone


def test_norm_skips_bad_rows_and_sorts():
    rows = [
        _row(5, 60),
        {"value": "50"},  # no timestamp
        {"timestamp": "100"},  # no value
        {"value": "abc", "timestamp": "100"},
        {"value": None, "timestamp": "100"},
        _row(1, 101),  # above range
        _row(2, -1),  # below range
        _row(3, 0),  # lower edge is valid
        _row(4, 100),  # upper edge is valid
    ]
    out = _norm(rows)
    assert [r["value"] for r in out] == [0, 100, 60]
    assert [r["ts"] for r in out] == [3 * DAY, 4 * DAY, 5 * DAY]


def test_norm_classification_lowercased_or_derived():
    out = _norm([_row(1, 10, "Extreme Fear"), _row(2, 80), _row(3, 50, "")])
    assert out[0]["classification"] == "extreme_fear"
    assert out[1]["classification"] == "extreme_greed"  # derived from value
    assert out[2]["classification"] == "neutral"  # empty string falls back to the zone


def test_empty_and_all_invalid_input():
    assert summarise_fear_greed([]) == {"count": 0}
    assert summarise_fear_greed([{"value": "x", "timestamp": "1"}, {}]) == {"count": 0}


def test_series_a_summary():
    out = summarise_fear_greed(_series_a())
    assert out["count"] == 31
    assert out["latest"] == {"value": 50, "classification": "neutral", "ts": 30 * DAY, "zone": "neutral"}
    assert out["mean_7d"] == pytest.approx(46.5)
    assert out["mean_30d"] == pytest.approx(35.0)
    assert (out["min"], out["max"]) == (20, 50)
    assert out["change_7d"] == 7
    assert out["change_30d"] == 30
    assert out["zone_share"] == {
        "extreme_fear": 0.1613,  # 5/31
        "fear": 0.6452,  # 20/31
        "neutral": 0.1935,  # 6/31
        "greed": 0.0,
        "extreme_greed": 0.0,
    }
    assert len(out["series"]) == 31
    assert out["series"][0] == {"ts": 0, "value": 20, "zone": "extreme_fear"}
    assert out["series"][-1] == {"ts": 30 * DAY, "value": 50, "zone": "neutral"}


def test_unsorted_input_gives_same_result():
    rows = _series_a()
    assert summarise_fear_greed(list(reversed(rows))) == summarise_fear_greed(rows)


def test_now_ts_ahead_of_data_empties_short_window():
    # anchor = day 40: 7d cutoff day 33 -> no rows; 30d cutoff day 10 -> days 10..30 (30..50)
    out = summarise_fear_greed(_series_a(), now_ts=40 * DAY)
    assert out["mean_7d"] is None
    assert out["mean_30d"] == pytest.approx(40.0)
    assert out["change_7d"] == 0  # the latest row itself is "7d ago"
    assert out["change_30d"] == 20  # 50 - value on day 10 (30)


def test_short_history_has_no_changes():
    out = summarise_fear_greed([_row(10, 100, "Extreme Greed")])
    assert out["count"] == 1
    assert out["latest"]["zone"] == "extreme_greed"
    assert out["latest"]["classification"] == "extreme_greed"
    assert out["mean_7d"] == 100 and out["mean_30d"] == 100
    assert out["change_7d"] is None  # no row at or before anchor - 7d
    assert out["zone_share"]["extreme_greed"] == 1.0


def test_two_rows_inside_a_week_have_no_deltas():
    out = summarise_fear_greed([_row(10, 40), _row(12, 60)])
    assert out["change_7d"] is None
    assert out["change_30d"] is None
    assert out["mean_7d"] == pytest.approx(50.0)
    assert out["latest"]["zone"] == "greed"
