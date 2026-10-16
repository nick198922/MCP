"""Polymarket YES-calibration research: pure analytics (no network)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics.pm_calibration import (
    basket,
    bootstrap_ci,
    category_of,
    classify_market,
    cluster_bootstrap_ci,
    cluster_means,
    devig_event,
    eligible_at,
    end_matches_slug,
    is_automated,
    iso_ts,
    jump_proximity,
    lag_profile,
    mde,
    no_return,
    reliability,
    resolved_outcome,
    slug_dates,
    stratified_event_sample,
)
from tradedesk.analytics.pm_threshold import quote_at


def _ts(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp())


def _event(**kw):
    ev = {"id": "9", "slug": "will-x-happen", "tags": ["politics"], "series": [], "enableNegRisk": False}
    ev.update(kw)
    return ev


def _market(**kw):
    m = {
        "id": "1",
        "slug": "will-x-happen",
        "conditionId": "0xabc",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "outcomePrices": '["0", "1"]',
        "endDate": "2025-11-03T20:00:00Z",
        "acceptingOrdersTimestamp": "2025-10-01T00:00:00Z",
        "closedTime": "2025-11-03 22:15:00+00",
        "volumeNum": 1234.5,
        "enableOrderBook": True,
        "feesEnabled": False,
    }
    m.update(kw)
    return m


# ----------------------------------------------------------------------------- metadata


def test_iso_ts_formats():
    assert iso_ts("2025-12-04T00:00:00Z") == _ts(2025, 12, 4)
    assert iso_ts("2021-12-05 20:37:01+00") == _ts(2021, 12, 5, 20, 37, 1)
    assert iso_ts("2025-12-04T00:00:00.5Z") == _ts(2025, 12, 4)
    assert iso_ts("") is None and iso_ts(None) is None and iso_ts("nope") is None


def test_resolved_outcome():
    assert resolved_outcome('["1", "0"]') == 1
    assert resolved_outcome('["0", "1"]') == 0
    assert resolved_outcome('["0.0000004", "0.9999996"]') == 0  # AMM-era rounding
    assert resolved_outcome('["0.5", "0.5"]') is None  # 50/50
    assert resolved_outcome('["0.2", "0.8"]') is None  # unresolved
    assert resolved_outcome(None) is None and resolved_outcome('["1"]') is None


def test_category_priority_and_automated():
    assert category_of(["sports", "politics"]) == "sports"
    assert category_of(["esports", "sports"]) == "esports"
    assert category_of(["trump", "economy"]) == "economics"  # economics precedes politics
    assert category_of(["hide-from-new"]) == "other"
    assert is_automated(["crypto", "up-or-down"], [])
    assert is_automated(["crypto"], ["15m"])
    assert not is_automated(["crypto", "bitcoin"], ["daily"])  # "Will BTC ETF ..." style events stay
    assert not is_automated(["sports"], ["daily"])


def test_slug_dates_and_end_match():
    assert slug_dates("nba-lal-bos-2025-11-03") == [(2025, 11, 3)]
    assert slug_dates("will-x-happen-by-june-30") == [(None, 6, 30)]
    assert slug_dates("fed-decision-in-december-2025") == []  # month without a day is not a date
    assert slug_dates("x-on-march-1st-2026") == [(2026, 3, 1)]
    end = _ts(2025, 11, 4, 3)  # evening US game, endDate after midnight UTC
    assert end_matches_slug("nba-lal-bos-2025-11-03", end) is True
    assert end_matches_slug("nba-lal-bos-2025-10-20", end) is False
    assert end_matches_slug("will-x-happen-by-november-3", end) is True
    assert end_matches_slug("no-date-here", end) is None


def test_classify_ok_and_fields():
    c = classify_market(_event(), _market())
    assert c["status"] == "ok"
    assert c["yes_no"] and c["outcome"] == 0 and c["yes_token"] == "111"
    assert c["T"] == _ts(2025, 11, 3, 20) and c["closed"] == _ts(2025, 11, 3, 22, 15)
    assert c["category"] == "politics" and c["year"] == 2025 and c["fee_rate"] == 0.0


def test_classify_rejections_in_order():
    assert classify_market(_event(tags=["up-or-down"]), _market())["status"] == "automated"
    assert classify_market(_event(), _market(clobTokenIds='["1"]'))["status"] == "tokens"
    assert classify_market(_event(), _market(outcomePrices='["0.5", "0.5"]'))["status"] == "unresolved"
    assert classify_market(_event(), _market(endDate=None))["status"] == "no_end"
    assert classify_market(_event(), _market(slug="game-2025-10-01"))["status"] == "slug_date"
    assert classify_market(_event(), _market(closedTime=None))["status"] == "no_times"
    named = classify_market(_event(tags=["sports"]), _market(outcomes='["Lakers", "Celtics"]', outcomePrices='["1", "0"]'))
    assert named["status"] == "ok" and not named["yes_no"] and named["outcome"] == 1


def test_classify_sports_anchor_is_game_start_not_end_date():
    # real Gamma pattern: endDate = game day + 7 days; gameStartTime is the scheduled event
    m = _market(slug="nba-mia-mil-2025-01-23", endDate="2025-01-31T00:30:00Z", gameStartTime="2025-01-24 00:30:00+00")
    c = classify_market(_event(slug="nba-mia-mil-2025-01-23", tags=["sports", "nba"]), m)
    assert c["status"] == "ok" and c["anchor"] == "game_start" and c["T"] == _ts(2025, 1, 24, 0, 30) and c["slug_dated"]
    # without gameStartTime the same market fails the slug check (endDate a week after the slug date)
    m2 = _market(slug="nba-mia-mil-2025-01-23", endDate="2025-01-31T00:30:00Z")
    assert classify_market(_event(slug="nba-mia-mil-2025-01-23", tags=["sports"]), m2)["status"] == "slug_date"


def test_classify_reads_fee_schedule():
    c = classify_market(_event(), _market(feesEnabled=True, feeSchedule={"rate": 0.25, "exponent": 2}))
    assert (c["fee_rate"], c["fee_exp"]) == (0.25, 2.0)


def test_eligible_at_uses_only_state_at_t0():
    c = classify_market(_event(), _market())
    t0 = c["T"] - 4 * 3600
    assert eligible_at(c, t0)
    # closed before t0 -> not in the sample; closed after t0 but before T -> stays (no selection on outcome)
    early = dict(c, closed=t0 - 1)
    assert not eligible_at(early, t0)
    mid = dict(c, closed=t0 + 600)
    assert eligible_at(mid, t0)
    late_listing = dict(c, start=t0 - 3600)
    assert not eligible_at(late_listing, t0)


# ----------------------------------------------------------------------------- no lookahead


def test_quote_at_t0_ignores_data_after_cutoff():
    t0 = _ts(2025, 11, 3, 16)
    pts = [{"t": t0 - 600, "p": 0.48}, {"t": t0 - 120, "p": 0.51}]
    base = quote_at(pts, t0)
    assert base == (0.51, t0 - 120)
    # anything after t0 - 60 s (including a resolution jump) must not change q
    later = [*pts, {"t": t0 - 30, "p": 0.99}, {"t": t0 + 60, "p": 1.0}, {"t": t0 + 7200, "p": 0.0}]
    assert quote_at(later, t0) == base
    assert quote_at([{"t": t0 - 1000, "p": 0.5}], t0) is None  # older than 15 min


def test_eligibility_and_basket_do_not_depend_on_later_closed_time_beyond_t0():
    c = classify_market(_event(), _market())
    t0 = c["T"] - 4 * 3600
    for closed in (t0 + 1, c["T"], c["T"] + 30 * 86400):
        assert eligible_at(dict(c, closed=closed), t0)


# ----------------------------------------------------------------------------- statistics


def test_basket_bounds():
    assert basket(0.40) and basket(0.60) and basket(0.5)
    assert not basket(0.399) and not basket(0.601)


def test_cluster_means_equal_weight():
    rows = [("e1", 1.0), ("e1", 0.0), ("e2", 0.3)]
    assert cluster_means(rows) == pytest.approx([0.5, 0.3])


def test_devig_event():
    d = devig_event({"a": 0.5, "b": 0.3, "c": 0.25})
    assert sum(d.values()) == pytest.approx(1.0)
    assert d["a"] == pytest.approx(0.5 / 1.05)
    assert devig_event({"a": 0.0}) is None


def test_no_return_on_capital():
    # q = 0.45 -> NO entry 0.56; NO wins -> (1 - 0.56) / 0.56
    r = no_return(0.45, 0)
    assert r["entry"] == pytest.approx(0.56)
    assert r["ret"] == pytest.approx(0.44 / 0.56)
    assert no_return(0.45, 1)["ret"] == pytest.approx(-1.0)
    # last NO print above the quote-implied entry is used (worse fill)
    assert no_return(0.45, 0, last_no_px=0.58)["entry"] == pytest.approx(0.58)
    # fee upper bound: 0.07 * 0.56 * 0.44 per share
    f = no_return(0.45, 0, fee_rate=0.07)
    assert f["fee"] == pytest.approx(0.07 * 0.56 * 0.44)
    assert f["ret"] == pytest.approx((1 - 0.56 - f["fee"]) / (0.56 + f["fee"]))


def test_bootstrap_ci_and_mde():
    vals = [0.0, 1.0] * 500  # mean 0.5, sd 0.5 -> se ~ 0.0158, CI ~ +-0.031
    b = bootstrap_ci(vals)
    assert b["mean"] == pytest.approx(0.5)
    assert b["lo"] == pytest.approx(0.5 - 1.96 * 0.5 / math.sqrt(1000), abs=0.006)
    assert b["hi"] == pytest.approx(0.5 + 1.96 * 0.5 / math.sqrt(1000), abs=0.006)
    assert bootstrap_ci(vals) == b  # seeded
    assert bootstrap_ci([])["mean"] is None and bootstrap_ci([0.2])["lo"] is None
    assert mde(0.5, 2000) == pytest.approx(2.8 * 0.5 / math.sqrt(2000))  # 3.13 pp, as in the card


def test_cluster_bootstrap_keeps_event_estimate_and_widens_ci():
    # one event per date: identical to the event bootstrap (same seed, same draws)
    vals = [0.0, 1.0] * 50
    rows = [(f"d{i:03d}", f"d{i:03d}", v) for i, v in enumerate(vals)]
    a, b = cluster_bootstrap_ci(rows), bootstrap_ci(vals)
    assert (a["mean"], a["lo"], a["hi"]) == pytest.approx((b["mean"], b["lo"], b["hi"]))
    # the estimate is the mean of event means (markets averaged within the event first), not the mean of date means
    rows = [("A", "e1", 0.0), ("A", "e1", 0.0), ("A", "e2", 0.0), ("A", "e3", 0.0), ("B", "e4", 1.0), ("B", "e4", 1.0)]
    r = cluster_bootstrap_ci(rows)
    assert r["mean"] == pytest.approx(0.25) and r["n"] == 4 and r["n_clusters"] == 2
    assert cluster_means([(o, v) for o, _, v in rows]) == [0.0, 1.0]  # date weighting would give 0.5
    # perfectly correlated within a date: 20 dates x 50 events -> the CI reflects 20 clusters, not 1000 events
    rows = [(f"d{d:02d}", f"d{d:02d}e{e}", float(d % 2)) for d in range(20) for e in range(50)]
    r = cluster_bootstrap_ci(rows)
    assert r["mean"] == pytest.approx(0.5)
    assert r["lo"] == pytest.approx(0.5 - 1.96 * 0.5 / math.sqrt(20), abs=0.04)
    assert r["hi"] == pytest.approx(0.5 + 1.96 * 0.5 / math.sqrt(20), abs=0.04)
    assert cluster_bootstrap_ci([])["mean"] is None and cluster_bootstrap_ci([("A", "e", 0.3)])["lo"] is None


def test_lag_profile_and_jump_proximity():
    yes = "Y"
    # prints: YES 0.40 at t=100, NO 0.40 (YES-eq 0.60) at t=200; points stamped at the print times
    trades = [{"timestamp": 100, "price": 0.40, "asset": "Y"}, {"timestamp": 200, "price": 0.40, "asset": "N"}]
    pts = [{"t": 150, "p": 0.40}, {"t": 230, "p": 0.60}]
    lp = lag_profile(pts, trades, yes, (-60, 0, 60))
    assert lp[-60] == pytest.approx([0.2])  # t=150 has no print <= 90; t=230 sees 0.40 (print <= 170)
    assert lp[0] == pytest.approx([0.0, 0.0])
    assert lp[60] == pytest.approx([0.2, 0.0])  # t=150 sees the 0.60 print at 200 <= 210
    jp = jump_proximity([{"t": 170, "p": 0.41}, {"t": 210, "p": 0.59}], trades, yes)
    assert jp["before_0_60"] == [False] and jp["after_0_60"] == [True]
    assert jp["before_60_120"] == [] and jp["before_120_300"] == []


def test_stratified_event_sample_is_proportional_and_deterministic():
    strata = {f"e{i}": ("sports|2025-01" if i < 800 else "politics|2025-01") for i in range(1000)}
    s = stratified_event_sample(strata, 0.25)
    assert len([e for e in s if strata[e].startswith("sports")]) == 200
    assert len([e for e in s if strata[e].startswith("politics")]) == 50
    assert s == stratified_event_sample(dict(reversed(list(strata.items()))), 0.25)  # order-independent
    assert stratified_event_sample(strata, 1.0) == set(strata)


def test_reliability_table():
    rows = [(0.42, 1), (0.48, 0), (0.55, 0), (0.60, 1)]
    t = reliability(rows, [0.40, 0.50, 0.60])
    assert t[0]["n"] == 2 and t[0]["mean_q"] == pytest.approx(0.45) and t[0]["freq"] == pytest.approx(0.5)
    assert t[1]["n"] == 2  # 0.60 falls in the last, closed bucket
    assert t[1]["gap"] == pytest.approx(0.575 - 0.5)
