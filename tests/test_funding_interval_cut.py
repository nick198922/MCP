import math

import pytest

from tradedesk.analytics.funding_interval_cut import (
    DAY_MS,
    HOUR_MS,
    Candidate,
    IntervalChange,
    bar_index_containing,
    classify,
    dedup_changes,
    design_effect,
    detect_interval_changes,
    funding_pnl,
    iso_week,
    match_controls,
    sd,
    side_for,
    window_returns,
)

T0 = 1_700_000_000_000 - (1_700_000_000_000 % (4 * HOUR_MS))


def schedule(*segments):
    """Rows from (n_rows, interval_h, rate) segments, each row ``interval_h`` after the previous one."""
    rows, t = [], T0
    for n, h, rate in segments:
        for _ in range(n):
            t += int(h * HOUR_MS)
            rows.append([float(t), float(h), rate])
    return rows


def test_cut_known_at_first_short_settlement():
    rows = schedule((5, 4, -0.001), (1, 4, -0.02), (6, 1, -0.01))
    ch = detect_interval_changes(rows)
    assert len(ch) == 1
    c = ch[0]
    assert (c.h_prev, c.h_new, c.kind) == (4.0, 1.0, "cut")
    assert c.t_label == c.t_known == int(rows[6][0])
    assert c.trigger_rate == -0.02
    assert (c.t_known - int(rows[5][0])) == HOUR_MS


def test_label_ahead_of_schedule_is_confirmed_one_row_later():
    # the first row with the new 4h label still comes 8h after the previous settlement (label is forward-looking)
    rows = schedule((4, 8, 0.0001))
    t = rows[-1][0] + 8 * HOUR_MS
    rows.append([t, 4.0, 0.005])
    for _ in range(3):
        t += 4 * HOUR_MS
        rows.append([t, 4.0, 0.004])
    (c,) = detect_interval_changes(rows)
    assert c.t_label == int(rows[4][0])
    assert c.t_known == int(rows[5][0])
    assert c.trigger_rate == 0.0001


def test_lengthening_and_unconfirmed():
    rows = schedule((3, 4, -0.02), (5, 1, -0.003))
    t = rows[-1][0] + HOUR_MS
    rows.append([t, 4.0, -0.001])  # label 4h but only 1h gap
    rows.append([t + 4 * HOUR_MS, 4.0, -0.0005])
    ch = detect_interval_changes(rows)
    assert [c.kind for c in ch] == ["cut", "lengthen"]
    assert ch[1].t_known == int(rows[-1][0])
    # a lone mislabelled 4h row on an unchanged 8h schedule is never confirmed as a cut
    bad = schedule((3, 8, 0.0001))
    bad.append([bad[-1][0] + 8 * HOUR_MS, 4.0, 0.0001])
    bad.append([bad[-1][0] + 8 * HOUR_MS, 8.0, 0.0001])
    assert [c.kind for c in detect_interval_changes(bad)] == ["lengthen"]


def test_causality_truncation_after_t_known_does_not_change_event():
    rows = schedule((10, 8, 0.0001), (1, 8, -0.02), (30, 4, -0.004), (2, 4, -0.03), (40, 1, -0.01), (20, 4, -0.0001))
    full = detect_interval_changes(rows)
    assert len(full) == 3
    times = [int(r[0]) for r in rows]
    for c in full:
        j = times.index(c.t_known)
        cut = detect_interval_changes(rows[: j + 1])
        assert cut[-1] == c
        # changing anything after t_known (future rates, future intervals) leaves the event unchanged
        alt = [list(r) for r in rows]
        for r in alt[j + 1 :]:
            r[2] = 0.05
            r[1] = 8.0 if r[1] == 1.0 else r[1]
        assert c in detect_interval_changes(alt)


def test_dedup_quiet_window_and_listing_age():
    mk = lambda day, hp, h: IntervalChange(T0 + day * DAY_MS, T0 + day * DAY_MS, hp, h, -0.02)  # noqa: E731
    cs = [mk(20, 4, 1), mk(25, 1, 4), mk(30, 4, 1), mk(40, 1, 4), mk(50, 4, 1), mk(60, 1, 4)]
    kept = dedup_changes(cs, first_row_ms=T0)
    # cut d20 kept; lengthen d25 kept (no earlier lengthening); cut d30 blocked by d25; lengthen d40 kept (d25 is
    # 15 days earlier); cut d50 blocked by d40; lengthen d60 kept (d40 is 20 days earlier)
    got = [(round((c.t_known - T0) / DAY_MS), c.kind) for c in kept]
    assert got == [(20, "cut"), (25, "lengthen"), (40, "lengthen"), (60, "lengthen")]
    assert dedup_changes([mk(20, 4, 1), mk(33, 4, 1)], first_row_ms=T0) == [mk(20, 4, 1)]
    assert dedup_changes([mk(10, 4, 1)], first_row_ms=T0) == []
    assert len(dedup_changes([mk(10, 4, 1)], first_row_ms=None)) == 1


def test_classify_and_side():
    c = lambda r, hp=4.0, h=1.0: IntervalChange(0, 0, hp, h, r)  # noqa: E731
    assert classify(c(-0.02)) == "H1" and side_for(c(-0.02)) == 1
    assert classify(c(0.003)) == "H2" and side_for(c(0.003)) == -1
    assert classify(c(-0.0005)) == "H3a"
    assert classify(c(-0.02, 1.0, 4.0)) == "H3b"


def test_bar_index_and_window_returns():
    opens = [T0 + i * DAY_MS for i in range(10)]
    assert bar_index_containing(opens, T0 + 3 * DAY_MS + 5, DAY_MS) == 3
    assert bar_index_containing(opens, T0 - 1, DAY_MS) is None
    assert bar_index_containing(opens, T0 + 10 * DAY_MS + 1, DAY_MS) is None
    closes = [100.0, 100.0, 80.0, 50.0, 55.0, 60.0, 75.0]
    w = window_returns(closes, 3, back=(1, 3), fwd=(3,))
    assert w == {"b1": pytest.approx(-0.375), "b3": pytest.approx(-0.5), "f3": pytest.approx(0.5)}
    assert window_returns(closes, 5, back=(1,), fwd=(3,)) is None
    assert window_returns(closes, 2, back=(3,), fwd=(1,)) is None


def test_funding_pnl_sign_and_window():
    rows = [[T0 + i * HOUR_MS, 1.0, -0.01] for i in range(1, 6)]
    # long receives 1 % per settlement while the rate is -1 %; window (T0+1h, T0+4h] has 3 settlements
    assert funding_pnl(rows, T0 + HOUR_MS, T0 + 4 * HOUR_MS, 1) == pytest.approx(0.03)
    assert funding_pnl(rows, T0 + HOUR_MS, T0 + 4 * HOUR_MS, -1) == pytest.approx(-0.03)


def test_match_controls_nearest_with_cap():
    pool = [
        Candidate(f"S{i}", b1, b3, 0.0)
        for i, (b1, b3) in enumerate([(-0.1, -0.3), (-0.12, -0.31), (0.0, -0.36), (-0.1, -0.2), (-0.11, -0.29)])
    ]
    got = match_controls(-0.1, -0.3, pool, k=3)
    assert [c.key for c in got] == ["S0", "S4", "S1"]
    assert all(abs(c.b3 + 0.3) <= 0.05 for c in match_controls(-0.1, -0.3, pool, k=10))
    # S2 (|Δb3| = 6 pp) and S3 (10 pp) are outside the 5 pp cap
    assert [c.key for c in match_controls(-0.1, -0.3, pool, k=10)] == ["S0", "S4", "S1"]
    assert [c.key for c in match_controls(-0.1, -0.3, pool, k=10, max_db3=0.07)] == ["S0", "S4", "S1", "S2"]


def test_stats_helpers():
    assert sd([1.0, 2.0, 3.0, 4.0]) == pytest.approx(math.sqrt(5 / 3))
    assert sd([1.0]) is None
    # iid case: cluster SE equal to sd/sqrt(n) -> design effect 1
    assert design_effect(0.5, 1.0, 4) == pytest.approx(1.0)
    assert design_effect(None, 1.0, 4) is None
    assert iso_week(1_700_000_000_000) == "2023-W46"
