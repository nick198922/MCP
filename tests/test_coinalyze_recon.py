"""Pure reconciliation helpers of the Coinalyze-vs-venue check (analytics/coinalyze_recon.py)."""

from __future__ import annotations

import pytest

from tradedesk.analytics.coinalyze_recon import (
    DAY,
    best_key,
    compare_pairs,
    compare_series,
    coverage,
    funding_candidates_by_day,
    informative_days,
    liquidation_sum_check,
    mismatch_breakdown,
    settlements_per_day,
    verdict,
    within,
)

D0 = 1_795_651_200  # 2026-11-26 00:00 UTC


def test_coverage_counts_gaps_duplicates_and_misaligned():
    ts = [D0, D0 + DAY, D0 + DAY, D0 + 3 * DAY, D0 + 3 * DAY + 60]
    c = coverage(ts, last_settled_day=D0 + 3 * DAY)
    assert c["rows"] == 5 and c["distinct"] == 4 and c["duplicates"] == 1
    assert c["first"] == D0 and c["last"] == D0 + 3 * DAY + 60
    assert c["missing"] == [D0 + 2 * DAY] and c["missing_days"] == 1
    assert c["misaligned"] == 1 and c["has_last_settled"] is True
    assert coverage([D0], last_settled_day=D0 + DAY)["has_last_settled"] is False


def test_coverage_empty():
    c = coverage([], last_settled_day=D0)
    assert c["rows"] == 0 and c["first"] is None and c["has_last_settled"] is False


def test_funding_candidates_three_settlements_a_day():
    # 8 h venue: settlements at 00, 08, 16 UTC of D and 00 of D+1
    s = [(D0, 0.0001), (D0 + 8 * 3600, 0.0003), (D0 + 16 * 3600, -0.0002), (D0 + DAY, 0.00005)]
    c = funding_candidates_by_day(s)
    d = c[D0]
    assert d["first_in_day"] == 0.0001 and d["last_in_day"] == -0.0002
    assert d["max_in_day"] == 0.0003 and d["min_in_day"] == -0.0002
    assert d["mean_in_day"] == pytest.approx(0.0002 / 3)
    assert d["next_first"] == 0.00005
    # the day before D only has next_first (D's first settlement); D+1 has its own but no next
    assert c[D0 - DAY] == {"next_first": 0.0001}
    assert "next_first" not in c[D0 + DAY] and c[D0 + DAY]["last_in_day"] == 0.00005


def test_funding_candidates_prev_last_and_repeated_timestamp():
    s = [(D0 - DAY + 16 * 3600, 0.0004), (D0, 0.0001), (D0, 0.0002), (D0 + 8 * 3600, 0.0003)]
    c = funding_candidates_by_day(s)
    # the repeated 00:00 stamp keeps the last rate given
    assert c[D0]["first_in_day"] == 0.0002 and c[D0]["mean_in_day"] == pytest.approx(0.00025)
    assert c[D0]["prev_last"] == 0.0004 and "prev_last" not in c[D0 - DAY]
    # the day after the last settlement only carries prev_last
    assert c[D0 + DAY] == {"prev_last": 0.0003}


def test_settlements_per_day():
    ts = [D0, D0 + 8 * 3600, D0 + 16 * 3600, D0 + 16 * 3600, D0 + DAY, D0 + 2 * DAY + 3600]
    assert settlements_per_day(ts) == {1: 2, 3: 1}
    assert settlements_per_day([]) == {}


def test_informative_days():
    cands = {
        D0: {"first_in_day": 0.0001, "last_in_day": 0.0003, "max_in_day": 0.0003},  # last == max: not informative
        D0 + DAY: {"first_in_day": 0.0001, "last_in_day": 0.0002, "max_in_day": 0.0004},
        D0 + 2 * DAY: {"first_in_day": 0.0001},  # no last_in_day: not counted
    }
    r = informative_days(cands, [D0, D0 + DAY, D0 + 2 * DAY, D0 + 9 * DAY], "last_in_day", 2e-8)
    assert r == {"n": 2, "informative": 1}


def test_mismatch_breakdown_prev_last_base_and_range():
    cands = {
        # matches the expected first_in_day
        D0: {"first_in_day": 0.0002, "max_in_day": 0.0003, "min_in_day": 0.0001, "prev_last": 0.0005},
        # carried rate, distinguishable: prev_last 0.0005 is neither 1e-4 nor the day's first; above the day's max
        D0 + DAY: {"first_in_day": 0.0002, "max_in_day": 0.0003, "min_in_day": 0.0002, "prev_last": 0.0005},
        # prev_last == base rate: "carried" and "base" cannot be told apart
        D0 + 2 * DAY: {"first_in_day": 0.0003, "max_in_day": 0.0003, "min_in_day": 0.0003, "prev_last": 0.0001},
        # neither: some other value, inside the day's range
        D0 + 3 * DAY: {"first_in_day": 0.0001, "max_in_day": 0.0004, "min_in_day": 0.0001, "prev_last": 0.0007},
    }
    ours = {D0: 0.0002, D0 + DAY: 0.0005, D0 + 2 * DAY: 0.0001, D0 + 3 * DAY: 0.0002, D0 + 5 * DAY: 1.0}
    r = mismatch_breakdown(ours, cands, "first_in_day", 2e-8)
    assert r["n"] == 4 and r["mismatch"] == 3
    assert r["eq_prev_last"] == 2 and r["eq_base"] == 1 and r["eq_neither"] == 1
    assert r["above_day_max"] == 1 and r["below_day_min"] == 1
    assert r["distinguishing"] == 2 and r["distinguishing_eq_prev_last"] == 1 and r["distinguishing_eq_base"] == 0
    assert r["by_year"] == {2026: 3}


def test_funding_candidates_millisecond_stamps_floor_to_the_day():
    # Binance fundingTime carries a few ms; the caller passes ms // 1000, which stays inside the day
    c = funding_candidates_by_day([((D0 * 1000 + 4) // 1000, 0.0001)])
    assert c[D0]["first_in_day"] == 0.0001


def test_funding_candidates_of_a_day_ignore_later_data():
    base = [(D0 + h * 3600, 0.0001 * (h + 1)) for h in (0, 8, 16)] + [(D0 + DAY, 0.0009)]
    later = [(D0 + 2 * DAY + h * 3600, 0.05) for h in (0, 8, 16)]
    a = funding_candidates_by_day(base)[D0]
    b = funding_candidates_by_day(base + later)[D0]
    assert a == b


def test_compare_pairs_scale_detects_percent():
    pairs = [(0.01, 0.0001), (0.02, 0.0002), (-0.005, -0.00005)]
    r = compare_pairs(pairs, abs_tol=2e-8)
    assert r["n"] == 3 and r["n_match"] == 0 and r["scale"] == pytest.approx(100.0)
    ok = compare_pairs([(a / 100, b) for a, b in pairs], abs_tol=2e-8)
    assert ok["share"] == 1.0 and ok["scale"] == pytest.approx(1.0)


def test_compare_pairs_relative_tolerance_and_empty():
    r = compare_pairs([(101.0, 100.0), (103.0, 100.0)], abs_tol=0.0, rel_tol=0.01)
    assert r["n_match"] == 1 and r["max_abs_diff"] == pytest.approx(3.0)
    e = compare_pairs([], abs_tol=1.0)
    assert e["n"] == 0 and e["share"] is None and e["scale"] is None


def test_within_relative_tolerance_is_on_the_reference():
    assert within(101.0, 100.0, 0.0, 0.01) and not within(101.5, 100.0, 0.0, 0.01)
    # relative to b (the reference): |99 - 100| = 1 <= 0.01 * 100, but |100 - 99| = 1 > 0.01 * 99
    assert within(99.0, 100.0, 0.0, 0.01) and not within(100.0, 99.0, 0.0, 0.01)
    assert within(0.5, 0.0, 0.5, 0.01) and not within(0.6, 0.0, 0.5, 0.01)


def test_compare_series_offset():
    ours = {D0: 0.5, D0 + DAY: 0.6}
    ref = {D0 + DAY: 0.5, D0 + 2 * DAY: 0.6, D0: 0.9}
    same_day = compare_series(ours, ref, 0, abs_tol=1e-9)
    next_day = compare_series(ours, ref, DAY, abs_tol=1e-9)
    assert same_day["n"] == 2 and same_day["n_match"] == 0
    assert next_day["share"] == 1.0 and next_day["first"] == D0 and next_day["last"] == D0 + DAY
    assert best_key({"same": same_day, "next": next_day}) == "next"
    assert best_key({}) is None


def test_verdict_thresholds():
    assert verdict({"n": 100, "share": 0.99}) == "✓"
    assert verdict({"n": 100, "share": 0.95}) == "≈"
    assert verdict({"n": 100, "share": 0.5}) == "✗"
    assert verdict({"n": 29, "share": 1.0}) == "◯"
    assert verdict({"n": 0, "share": None}) == "◯"


def test_liquidation_sum_check():
    a = [{"ts": D0, "long": 1.0, "short": 2.0}, {"ts": D0 + DAY, "long": 5.0, "short": 0.0}]
    b = [{"ts": D0, "long": 10.0, "short": 20.0}]
    tot = [{"ts": D0, "long": 11.0, "short": 22.0}, {"ts": D0 + DAY, "long": 5.0, "short": 0.0}]
    r = liquidation_sum_check(tot, [a, b], abs_tol=1e-9)
    assert r["n"] == 1 and r["n_match"] == 1 and r["only_total"] == 1 and r["only_parts"] == 0
    bad = [{"ts": D0, "long": 11.0, "short": 25.0}]
    r2 = liquidation_sum_check(bad, [a, b], abs_tol=1e-9)
    assert r2["n_match"] == 0 and r2["max_abs_diff"] == pytest.approx(3.0)
    r3 = liquidation_sum_check(tot, [a, b], abs_tol=1e-9, skip_ts=[D0])
    assert r3["n"] == 0 and r3["share"] is None
