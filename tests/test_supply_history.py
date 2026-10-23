from datetime import date

import pytest

from tradedesk.analytics.supply_history import (
    GapSummary,
    asof,
    coverage_fraction,
    normalize_perp_base,
    pit_verdict,
    quarter_sample_dates,
    rel_gap,
    resolve_symbol,
    summarize_gaps,
)


@pytest.mark.parametrize(
    ("base", "want"),
    [
        ("1000PEPE", "PEPE"),
        ("1000000BABYDOGE", "BABYDOGE"),
        ("10000LADYS", "LADYS"),
        ("1MBABYDOGE", "BABYDOGE"),
        ("1INCH", "1INCH"),
        ("0G", "0G"),
        ("btc", "BTC"),
    ],
)
def test_normalize_perp_base(base: str, want: str) -> None:
    assert normalize_perp_base(base) == want


def test_quarter_sample_dates_first_sundays() -> None:
    ds = quarter_sample_dates(2021, date(2021, 12, 31))
    assert ds == [date(2021, 1, 3), date(2021, 4, 4), date(2021, 7, 4), date(2021, 10, 3)]
    assert all(d.weekday() == 6 for d in ds)
    assert len(quarter_sample_dates(2021, date(2026, 10, 18))) == 24


def test_coverage_fraction() -> None:
    w = [date(2021, 1, 3), date(2021, 4, 4), date(2021, 7, 4), date(2021, 10, 3)]
    assert coverage_fraction(w, {date(2021, 1, 3), date(2021, 7, 4), date(2020, 1, 1)}) == 0.5
    assert coverage_fraction([], {date(2021, 1, 3)}) is None


def test_rel_gap() -> None:
    assert rel_gap(102.0, 100.0) == pytest.approx(0.02)
    assert rel_gap(98.0, 100.0) == pytest.approx(0.02)
    assert rel_gap(None, 1.0) is None
    assert rel_gap(1.0, 0.0) is None


def test_summarize_gaps() -> None:
    s = summarize_gaps([0.0, 0.01, 0.02, 0.06, 0.2, None])
    assert s.n == 5
    assert s.median == pytest.approx(0.02)
    assert s.share_gt_5pct == pytest.approx(0.4)
    assert s.share_gt_10pct == pytest.approx(0.2)
    assert s.p90 == pytest.approx(0.06 + (0.2 - 0.06) * 0.6)
    assert summarize_gaps([None]).n == 0


def test_resolve_symbol_by_price_then_cap() -> None:
    cands = [{"price": 1.0, "market_cap": 5e9}, {"price": 0.012, "market_cap": 1e7}]
    assert resolve_symbol(cands, 0.0115) == 1
    assert resolve_symbol(cands, 1.1) == 0
    assert resolve_symbol(cands, 50.0) is None  # no candidate within a factor of 2
    assert resolve_symbol(cands, None) == 0
    assert resolve_symbol([], None) is None


def test_resolve_symbol_prefers_real_coin_over_clone_at_same_price() -> None:
    # a bridged "ETH" clone priced 0.1 % closer to the perp than the real ETH must not win
    cands = [{"price": 4000.0, "market_cap": 4.8e11}, {"price": 4001.0, "market_cap": 2e6}]
    assert resolve_symbol(cands, 4001.0) == 0
    assert resolve_symbol([{"price": 1.0, "market_cap": None}, {"price": 1.5, "market_cap": None}], 1.05) == 0


def test_asof_ignores_later_data() -> None:
    # guards the helper kept for a future loader; the stage-0 script does not call asof
    s = {date(2021, 1, 3): 100.0, date(2021, 1, 10): 110.0}
    before = asof(s, date(2021, 1, 9))
    s[date(2021, 1, 17)] = 999.0  # data after t must not change the value known at t
    s[date(2021, 1, 10)] = 5.0
    assert asof(s, date(2021, 1, 9)) == before == 100.0
    assert asof(s, date(2021, 1, 2)) is None


def test_pit_verdict() -> None:
    assert pit_verdict(GapSummary(25, 0.002, 0.01, 0.04, 0.0)) == "pass"
    assert pit_verdict(GapSummary(25, 0.02, 0.05, 0.04, 0.0)) == "fail"
    assert pit_verdict(GapSummary(25, 0.002, 0.2, 0.2, 0.1)) == "fail"
    assert pit_verdict(GapSummary(5, 0.0, 0.0, 0.0, 0.0)) == "unverifiable"
