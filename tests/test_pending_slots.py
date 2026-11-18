from tradedesk.analytics.pending_slots import (
    DAY_MS,
    HOUR_MS,
    Evaluation,
    Interval,
    admit_freed,
    count_active,
    fill_by_age,
    freed_upper,
    parse_open_count,
    pending_interval,
    slot_days,
)

T0 = 1_793_404_800_000  # 2026-10-31 00:00 UTC
CUT = T0 + 10 * DAY_MS


def test_pending_interval_fill_missed_alive_and_future():
    assert pending_interval(T0, None, T0 + DAY_MS, T0 + 2 * DAY_MS, CUT) == Interval(T0, T0 + DAY_MS, True)
    assert pending_interval(T0, None, None, T0 + 3 * DAY_MS, CUT) == Interval(T0, T0 + 3 * DAY_MS, False)
    assert pending_interval(None, T0, None, None, CUT) == Interval(T0, CUT, False)
    # a fill or a close at / after the cut-off has not happened yet
    assert pending_interval(T0, None, CUT + 1, None, CUT) == Interval(T0, CUT, False)
    assert pending_interval(T0, None, None, CUT, CUT) == Interval(T0, CUT, False)
    assert pending_interval(None, None, None, None, CUT) is None
    assert pending_interval(CUT, None, None, None, CUT) is None


def test_parse_open_count():
    assert parse_open_count(["already decided", "25 open positions = max"]) == 25
    assert parse_open_count(["stop 12.0% from the entry > 10%"]) is None
    assert parse_open_count([]) is None


def test_count_active_and_slot_days():
    ivs = [Interval(T0, T0 + 2 * DAY_MS, False), Interval(T0 + DAY_MS, T0 + 5 * DAY_MS, True)]
    assert count_active(ivs, T0) == 1
    assert count_active(ivs, T0 + DAY_MS) == 2
    assert count_active(ivs, T0 + 2 * DAY_MS) == 1  # end exclusive
    assert slot_days(ivs, T0, T0 + 10 * DAY_MS) == 6.0
    assert slot_days(ivs, T0 + DAY_MS, T0 + 3 * DAY_MS) == 3.0  # 1 + 2 days of overlap
    assert slot_days(ivs, T0 + 6 * DAY_MS, T0 + 7 * DAY_MS) == 0.0


def _ev(h, lid, n=25, score=2, rr=2.0):
    return Evaluation(T0 + int(h * HOUR_MS), lid, n, score, rr)


def test_freed_upper_needs_enough_resting_entries():
    ivs = [Interval(T0, T0 + 10 * HOUR_MS, False)]
    evs = [_ev(1, "a"), _ev(2, "b", n=26), _ev(12, "c")]
    # a: 25 - 1 < 25 freed; b: 26 - 1 = 25 not; c: no resting entry at hour 12
    assert freed_upper(evs, ivs, 25) == ["a"]


def test_admit_freed_cycle_capacity_and_rank():
    ivs = [Interval(T0, T0 + 100 * HOUR_MS, False)]
    evs = [_ev(1, "low", score=2, rr=1.6), _ev(1, "high", score=3, rr=1.6), _ev(1, "mid", score=2, rr=3.0)]
    assert admit_freed(evs, ivs, 25, None) == ["high"]  # one free place in the cycle, the demo's order
    two = [Interval(T0, T0 + 100 * HOUR_MS, False)] * 2
    assert admit_freed(evs, two, 25, None) == ["high", "mid"]


def test_admit_freed_holding():
    ivs = [Interval(T0, T0 + 100 * HOUR_MS, False)]
    evs = [_ev(1, "a"), _ev(5, "b"), _ev(20, "c")]
    assert admit_freed(evs, ivs, 25, None) == ["a", "b", "c"]  # B: nobody holds the place
    assert admit_freed(evs, ivs, 25, 10 * HOUR_MS) == ["a", "c"]  # C: a holds it until hour 11
    assert admit_freed(evs, ivs, 25, 10**15) == ["a"]  # D: held to the end


def test_admit_freed_causal():
    """Admissions before t do not change when evaluations or interval ends after t change."""
    t = T0 + 10 * HOUR_MS
    ivs = [Interval(T0, T0 + 50 * HOUR_MS, False), Interval(T0 + 2 * HOUR_MS, T0 + 30 * HOUR_MS, True)]
    early = [_ev(1, "a"), _ev(3, "b"), _ev(3, "b2", score=3), _ev(8, "c")]
    late = [_ev(12, "d"), _ev(40, "e")]
    base = [x for x in admit_freed(early + late, ivs, 25, 6 * HOUR_MS) if x in {"a", "b", "b2", "c"}]
    ivs2 = [Interval(T0, T0 + 11 * HOUR_MS, True), Interval(T0 + 2 * HOUR_MS, T0 + 90 * HOUR_MS, False)]
    ivs2.append(Interval(t + HOUR_MS, t + 9 * HOUR_MS, False))
    late2 = [_ev(10.5, "x", score=9), _ev(11, "y")]
    alt = [x for x in admit_freed(early + late2, ivs2, 25, 6 * HOUR_MS) if x in {"a", "b", "b2", "c"}]
    assert base == alt == admit_freed(early, ivs, 25, 6 * HOUR_MS)
    assert freed_upper(early, ivs, 25) == freed_upper(early, ivs2, 25)


def test_fill_by_age():
    ivs = [
        Interval(T0, T0 + DAY_MS // 2, True),  # filled at 0.5 d
        Interval(T0, T0 + 5 * DAY_MS, True),  # filled at 5 d
        Interval(T0, T0 + 2 * DAY_MS, False),  # ended unfilled at 2 d
        Interval(CUT - 4 * DAY_MS, CUT, False),  # resting, age 4 d at the cut-off
    ]
    got = {r["age_ms"] // DAY_MS: r for r in fill_by_age(ivs, [DAY_MS, 3 * DAY_MS, 7 * DAY_MS], CUT)}
    assert got[1] == {"age_ms": DAY_MS, "filled": 1, "unfilled": 3, "censored": 0}
    assert got[3] == {"age_ms": 3 * DAY_MS, "filled": 1, "unfilled": 3, "censored": 0}
    assert got[7] == {"age_ms": 7 * DAY_MS, "filled": 2, "unfilled": 1, "censored": 1}
