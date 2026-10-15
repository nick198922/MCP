import pytest

from tradedesk.analytics.token_unlocks import (
    DAY_S,
    Section,
    circulating,
    parse_sections,
    placebo_days,
    select_events,
    slippage_for_volume,
    stopped_short_return,
    unlocks_by_day,
)


def doc(sections: dict[str, list[tuple[int, float]]], categories: dict[str, list[str]]) -> dict:
    return {
        "documentedData": {
            "data": [{"label": lab, "data": [{"timestamp": d * DAY_S, "unlocked": u} for d, u in pts]} for lab, pts in sections.items()]
        },
        "categories": categories,
    }


def cliff_schedule(start: int, end: int, every: int, size: float) -> list[tuple[int, float]]:
    """Cumulative series with a cliff of ``size`` on start, start + every, ... (daily points)."""
    out, cum = [], 0.0
    for d in range(start, end + 1):
        if d >= start and (d - start) % every == 0:
            cum += size
        out.append((d, cum))
    return out


def test_parse_sections_categories_and_lookup():
    em = doc(
        {"Team": [(10, 0.0), (11, 50.0), (11, 60.0), (20, 70.0)], "DAO Treasury": [(10, 1000.0)], "Public": [(5, 100.0)]},
        {"insiders": ["team"], "noncirculating": ["DAO treasury"]},
    )
    secs = {s.label: s for s in parse_sections(em)}
    assert secs["Team"].category == "insiders"
    assert secs["DAO Treasury"].category == "noncirculating"
    assert secs["Public"].category == "uncategorized"
    t = secs["Team"]
    assert t.points == ((10, 0.0), (11, 60.0), (20, 70.0))  # same-day points collapse to the last
    assert t.at(9) == 0.0 and t.at(11) == 60.0 and t.at(19) == 60.0 and t.at(25) == 70.0
    # treasury is excluded from circulating
    assert circulating(list(secs.values()), 15) == pytest.approx(160.0)
    assert parse_sections({}) == []


def test_dense_matches_point_lookup():
    from tradedesk.analytics.token_unlocks import _dense

    s = Section("A", "insiders", ((5, 1.0), (8, 3.0), (9, 4.0), (20, 10.0)))
    assert _dense(s, 0, 25) == [s.at(d) for d in range(0, 26)]
    assert _dense(s, 10, 12) == [4.0, 4.0, 4.0]


def test_unlocks_by_day_separates_cliffs_from_linear():
    linear = Section("Eco", "uncategorized", tuple((d, 1.0 * (d - 99)) for d in range(100, 201)))  # +1/day
    cliff = Section("Investors", "privateSale", ((100, 0.0), (150, 500.0)))
    treas = Section("Treasury", "noncirculating", ((100, 0.0), (150, 9999.0)))
    ub = unlocks_by_day([linear, cliff, treas], first_day=101, last_day=200)
    assert ub[150].amount == pytest.approx(501.0)
    assert ub[150].cliff == pytest.approx(500.0)
    assert ub[150].insiders == pytest.approx(500.0)
    assert ub[150].labels == ("Investors",)
    assert ub[120].amount == pytest.approx(1.0) and ub[120].cliff == 0.0


def test_select_events_threshold_denominator_at_entry_and_no_overlap():
    base = Section("Public", "publicSale", ((0, 1000.0),))
    vest = Section("Team", "insiders", tuple(cliff_schedule(100, 300, 30, 30.0)))  # 30 tokens every 30 days
    secs = [base, vest]
    ub = unlocks_by_day(secs, first_day=1, last_day=300)
    evs = select_events(ub, lambda d: circulating(secs, d), min_frac=0.02, first_day=0, last_day=300)
    # circ on the entry of the first cliff (day 90) is 1000 -> 3 % ; later cliffs shrink relative to supply
    assert [e.day for e in evs][:2] == [100, 130]
    first = evs[0]
    assert first.entry == 90 and first.exit == 102
    assert first.circ == pytest.approx(1000.0) and first.frac == pytest.approx(0.03)
    # 2.9 % threshold: day 100 (3 %) qualifies, day 130 (30 / 1030 = 2.91 %) also, day 160 (30/1060 = 2.83 %) not
    evs = select_events(ub, lambda d: circulating(secs, d), min_frac=0.029, first_day=0, last_day=300)
    assert [e.day for e in evs] == [100, 130]
    # weekly cliffs: overlapping windows are skipped
    weekly = Section("Team", "insiders", tuple(cliff_schedule(100, 200, 7, 50.0)))
    ub2 = unlocks_by_day([base, weekly], first_day=1, last_day=200)
    ev2 = select_events(ub2, lambda d: circulating([base, weekly], d), min_frac=0.02, first_day=0, last_day=200)
    days = [e.day for e in ev2]
    assert all(b - 10 > a + 2 for a, b in zip(days, days[1:], strict=False))
    # tradable filter
    ev3 = select_events(ub, lambda d: circulating(secs, d), min_frac=0.02, first_day=0, last_day=300, tradable=lambda e, t: t > 120)
    assert ev3[0].day == 130


def test_point_in_time_selection_does_not_depend_on_data_after_entry():
    """Changing the schedule only after day ``cut`` leaves every selected event with T <= cut unchanged.

    The event size on T is the schedule's (forecast) increment on T and the denominator is the schedule at T-10.
    This test does not vary data between T-10 and T; it only checks that later schedule rows do not leak back."""
    base = Section("Public", "publicSale", ((0, 1000.0),))
    vest = cliff_schedule(100, 400, 30, 30.0)
    secs_a = [base, Section("Team", "insiders", tuple(vest))]
    cut = 220
    # B: identical up to day `cut`, then a different (larger, irregular) schedule afterwards
    vest_b = [(d, u) for d, u in vest if d <= cut] + [(d, u + 500.0 * (d - cut)) for d, u in vest if d > cut]
    secs_b = [base, Section("Team", "insiders", tuple(vest_b))]

    def run(secs):
        ub = unlocks_by_day(secs, first_day=1, last_day=400)
        return select_events(ub, lambda d: circulating(secs, d), min_frac=0.02, first_day=0, last_day=400)

    ea = [e for e in run(secs_a) if e.day <= cut]
    eb = [e for e in run(secs_b) if e.day <= cut]
    assert ea == eb and len(ea) >= 2


def test_placebo_days_avoid_all_unlock_windows():
    blocked = [100, 130]
    ds = placebo_days(range(50, 200), blocked, step=13)
    for d in ds:
        for b in blocked:
            assert d + 2 < b - 10 or d - 10 > b + 2
    assert all(b - a >= 13 for a, b in zip(ds, ds[1:], strict=False))
    assert ds[0] == 50


def test_stopped_short_return():
    closes = {0: 100.0, 1: 105.0, 2: 90.0, 3: 95.0}
    highs = {1: 110.0, 2: 131.0, 3: 96.0}
    assert stopped_short_return(highs, closes, 0, 3, 0.30) == (pytest.approx(0.30), 2)
    r, d = stopped_short_return(highs, closes, 0, 3, 0.50)
    assert d == 3 and r == pytest.approx(-0.05)
    assert stopped_short_return(highs, {}, 0, 3, 0.3) is None


def test_slippage_buckets():
    assert slippage_for_volume(None) == 0.0015
    assert slippage_for_volume(1e6) == 0.0015
    assert slippage_for_volume(1e7) == 0.0010
    assert slippage_for_volume(1e8) == 0.0005
