from datetime import UTC, date, datetime

import numpy as np
import pytest

from tradedesk.analytics.session_stop_hunt import (
    BAR_MS,
    MAKER,
    MARKET_SIDE,
    Bars,
    cluster_bootstrap_diff,
    design_effect,
    find_setup,
    fractal_low_indices,
    is_fractal_low,
    london_session,
    new_york_session,
    simulate,
)

DAY = date(2024, 1, 15)  # winter: Frankfurt 07:00 UTC, London 08:00 UTC, window end 11:00 UTC


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


def utc(x: int) -> str:
    return datetime.fromtimestamp(x / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def make_bars(window: list[tuple[float, float, float, float]], after: tuple[float, float, float, float] = (97.5, 97.6, 97.4, 97.5)) -> Bars:
    """A day before 2024-01-15 flat at 100, a range [97, 101] from 00:00 to 07:00 UTC, the given window bars from
    07:00, then ``after`` bars up to 2024-01-16 06:00 UTC."""
    t0, end = ms("2024-01-14T00:00"), ms("2024-01-16T06:00")
    t = np.arange(t0, end, BAR_MS, dtype=np.int64)
    o, h, lo, c = (np.full(t.size, v) for v in (100.0, 100.2, 99.8, 100.0))
    i_r = int((ms("2024-01-15T00:00") - t0) // BAR_MS)
    h[i_r + 10] = 101.0
    lo[i_r + 40] = 97.0
    i_w = int((ms("2024-01-15T07:00") - t0) // BAR_MS)
    for k, (oo, hh, ll, cc) in enumerate(window):
        o[i_w + k], h[i_w + k], lo[i_w + k], c[i_w + k] = oo, hh, ll, cc
    rest = slice(i_w + len(window), t.size)
    o[rest], h[rest], lo[rest], c[rest] = after
    return Bars(t, o, h, lo, c)


# window bars (open, high, low, close): fractal low 100.0 at k=2, sweep of 101 at k=5 (extreme 101.5), back below 101
# at k=6, BOS close 99.95 < 100.0 at k=7, then down to 96.9 at k=9
SHORT = [
    (100.4, 100.5, 100.2, 100.4),
    (100.4, 100.6, 100.1, 100.3),
    (100.3, 100.7, 100.0, 100.6),
    (100.6, 100.8, 100.3, 100.7),
    (100.7, 100.9, 100.4, 100.8),
    (100.8, 101.5, 100.8, 101.3),
    (101.3, 101.2, 100.5, 100.8),
    (100.8, 100.9, 99.9, 99.95),
    (99.95, 99.9, 98.5, 98.6),
    (98.6, 98.7, 96.9, 97.5),
]
STOP = 101.5 * 1.0005
RISK = STOP - 99.95


def mirror(b: Bars, axis: float = 200.0) -> Bars:
    return Bars(b.t, axis - b.o, axis - b.low, axis - b.h, axis - b.c)


def test_london_session_follows_dst():
    w = london_session(DAY)
    assert (utc(w.range_start), utc(w.range_end), utc(w.window_end), utc(w.exit_at)) == (
        "2024-01-15 00:00",
        "2024-01-15 07:00",
        "2024-01-15 11:00",
        "2024-01-15 16:00",
    )
    s = london_session(date(2024, 7, 15))
    assert (utc(s.range_end), utc(s.window_end)) == ("2024-07-15 06:00", "2024-07-15 10:00")
    p = london_session(DAY, 6)
    assert (utc(p.range_start), utc(p.range_end), utc(p.window_end), utc(p.exit_at)) == (
        "2024-01-15 06:00",
        "2024-01-15 13:00",
        "2024-01-15 17:00",
        "2024-01-15 22:00",
    )


def test_new_york_session_and_dst_mismatch_week():
    s = new_york_session(DAY)
    assert (utc(s.range_start), utc(s.range_end), utc(s.window_end), utc(s.exit_at)) == (
        "2024-01-15 08:00",
        "2024-01-15 13:00",
        "2024-01-15 17:30",
        "2024-01-15 21:00",
    )
    m = new_york_session(date(2024, 3, 15))  # US on summer time, UK not yet
    assert (utc(m.range_start), utc(m.range_end)) == ("2024-03-15 08:00", "2024-03-15 12:00")


def test_fractal_vectorised_matches_scalar():
    rng = np.random.default_rng(1)
    low = np.round(rng.normal(size=300), 1)  # rounding creates ties, which are not fractals
    fast = set(fractal_low_indices(low).tolist())
    slow = {k for k in range(low.size) if is_fractal_low(low, k)}
    assert fast == slow and fast


def test_short_setup_and_e1_targets():
    b = make_bars(SHORT)
    st, space, why = find_setup(b, london_session(DAY))
    assert why == "ok" and st is not None and space is not None
    assert st.side == -1
    assert (st.range_high, st.range_low) == (101.0, 97.0)
    assert st.extreme == 101.5 and st.manip_start == 100.0
    assert st.entry == 99.95
    assert st.stop == pytest.approx(STOP)
    assert st.bos_idx - st.sweep_idx == 2

    t1, why = simulate(st, space, "E1", "T1")
    assert why == "ok" and t1 is not None
    assert t1.exit_kind == "target" and t1.exit == 97.0
    assert t1.r_gross == pytest.approx((99.95 - 97.0) / RISK)
    assert t1.cost_r == pytest.approx(MARKET_SIDE * (99.95 + 97.0) / RISK)
    assert t1.r_net == pytest.approx(t1.r_gross - t1.cost_r)

    t2, _ = simulate(st, space, "E1", "T2")
    assert t2 is not None and t2.exit_kind == "time"  # 2R = 96.75 is never reached; the 15:55 bar closes at 97.5
    assert t2.r_gross == pytest.approx((99.95 - 97.5) / RISK)
    assert space.t[t2.exit_idx] == ms("2024-01-15T15:55")


def test_long_is_the_mirror_of_short():
    b = make_bars(SHORT)
    st, space, _ = find_setup(b, london_session(DAY))
    lst, lspace, why = find_setup(mirror(b), london_session(DAY))
    assert why == "ok" and lst is not None and st is not None
    assert lst.side == 1
    for ek, tk in (("E1", "T1"), ("E1", "T2")):
        a, _ = simulate(st, space, ek, tk)
        m, _ = simulate(lst, lspace, ek, tk)
        assert a is not None and m is not None and a.exit_kind == m.exit_kind
        # the 0.05 % buffer is taken of 98.5 instead of 101.5, so R differs by ~0.1 %
        assert m.r_gross == pytest.approx(a.r_gross, rel=2e-3)
    # the long stop sits 0.05 % below the swept low 200 - 101.5 = 98.5
    assert -lst.stop == pytest.approx(98.5 * (1 - 0.0005))


def test_e2_limit_fill_and_unfilled():
    b = make_bars(SHORT)
    st, space, _ = find_setup(b, london_session(DAY))
    assert st is not None
    t, why = simulate(st, space, "E2", "T2")
    assert t is None and why == "e2_unfilled"  # price never returns to (100.0 + 101.5) / 2

    w = list(SHORT)
    w[8] = (99.95, 100.8, 99.9, 100.5)  # retraces to the 100.75 limit, then falls
    w[9] = (100.5, 100.6, 96.0, 96.5)
    st, space, _ = find_setup(make_bars(w), london_session(DAY))
    assert st is not None
    t, why = simulate(st, space, "E2", "T2")
    risk = STOP - 100.75
    assert why == "ok" and t is not None and t.entry == 100.75
    assert t.exit_kind == "target" and t.exit == pytest.approx(100.75 - 2 * risk)
    assert t.r_gross == pytest.approx(2.0)
    assert t.cost_r == pytest.approx((MAKER * 100.75 + MARKET_SIDE * t.exit) / risk)


def test_bar_touching_stop_and_target_counts_as_stop():
    w = list(SHORT)
    w[8] = (99.95, 101.6, 96.5, 98.0)
    st, space, _ = find_setup(make_bars(w), london_session(DAY))
    assert st is not None
    t, _ = simulate(st, space, "E1", "T1")
    assert t is not None and t.exit_kind == "stop" and t.both_touched
    assert t.r_gross == pytest.approx(-1.0)


def test_gap_through_stop_exits_at_open():
    w = list(SHORT)
    w[8] = (102.0, 102.5, 101.8, 102.2)
    st, space, _ = find_setup(make_bars(w), london_session(DAY))
    assert st is not None
    t, _ = simulate(st, space, "E1", "T1")
    assert t is not None and t.exit_kind == "stop_gap" and t.r_gross == pytest.approx((99.95 - 102.0) / RISK)


def test_t1_closer_than_one_r_is_skipped():
    b = make_bars(SHORT)
    b.low[b.low == 97.0] = 99.5  # range low 99.5: only 0.45 below the entry, less than 1R
    st, space, _ = find_setup(b, london_session(DAY))
    assert st is not None
    assert simulate(st, space, "E1", "T1") == (None, "t1_lt_1r")
    t2, why = simulate(st, space, "E1", "T2")
    assert why == "ok" and t2 is not None


def test_skip_reasons():
    both = list(SHORT)
    both[0] = (100.0, 101.2, 96.5, 100.0)
    assert find_setup(make_bars(both), london_session(DAY))[2] == "both_first"

    flat = [(100.0, 100.2, 99.8, 100.0)] * 3
    assert find_setup(make_bars(flat, after=(100.0, 100.2, 99.8, 100.0)), london_session(DAY))[2] == "no_sweep"

    late = SHORT[:7] + [(100.8, 100.9, 100.5, 100.7)] * 13 + [(100.7, 100.8, 99.0, 99.5)]  # BOS 75 min after the extreme
    assert find_setup(make_bars(late), london_session(DAY))[2] == "no_bos_60m"

    b = make_bars(SHORT)
    keep = b.t != ms("2024-01-15T12:00")
    g = Bars(b.t[keep], b.o[keep], b.h[keep], b.low[keep], b.c[keep])
    assert find_setup(g, london_session(DAY))[2] == "gap"


def test_new_extreme_after_window_voids_setup():
    # sweep at 10:55 (last window bar), extreme extended at 11:00 (outside the window) before any BOS
    w = [(100.4, 100.5, 100.2, 100.4)] * 47 + [(100.4, 101.2, 100.3, 100.9), (100.9, 101.6, 100.8, 100.9)]
    assert find_setup(make_bars(w), london_session(DAY))[2] == "new_extreme_after_window"


def test_no_lookahead_after_exit_or_bos():
    s = london_session(DAY)
    b = make_bars(SHORT)
    st, space, _ = find_setup(b, s)
    ref = simulate(st, space, "E1", "T1")[0]
    # change everything after the time exit: identical setup and trade
    later = b.t >= s.exit_at
    b2 = Bars(b.t, b.o.copy(), b.h.copy(), b.low.copy(), b.c.copy())
    b2.h[later] += 50
    b2.low[later] -= 50
    st2, space2, _ = find_setup(b2, s)
    assert st2 == st and simulate(st2, space2, "E1", "T1")[0] == ref
    # change everything after the BOS bar: the setup (entry, stop, target) is unchanged
    after_bos = b.t > space.t[st.bos_idx]
    b3 = Bars(b.t, b.o.copy(), b.h.copy(), b.low.copy(), b.c.copy())
    b3.h[after_bos] = 100.7
    b3.low[after_bos] = 100.6
    st3, _, _ = find_setup(b3, s)
    assert st3 == st


def test_design_effect_and_diff_bootstrap():
    assert design_effect([1, 1, 1], 0.7) == 1.0
    assert design_effect([3, 3], 0.7) == pytest.approx(1 + 2 * 0.7)
    a = [1.0, 2.0, 3.0, 4.0]
    days = ["d1", "d2", "d3", "d4"]
    r = cluster_bootstrap_diff(a, days, [x - 0.5 for x in a], days, n_boot=2000)
    assert r["diff"] == pytest.approx(0.5)
    assert r["ci"] == pytest.approx((0.5, 0.5))  # paired by date: every resample keeps the constant shift
    z = cluster_bootstrap_diff(a, days, a, days, n_boot=500)
    assert z["diff"] == 0.0
