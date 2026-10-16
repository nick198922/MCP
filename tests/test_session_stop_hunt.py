from datetime import UTC, date, datetime

import numpy as np
import pytest

from tradedesk.analytics.session_stop_hunt import (
    BAR_MS,
    CRYPTO,
    MAKER,
    MARKET_SIDE,
    Bars,
    Costs,
    cluster_bootstrap_diff,
    cost_r,
    design_effect,
    find_setup,
    fractal_low_indices,
    fx_execution,
    fx_skip_reason,
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


# ---------------------------------------------------------------- forex execution (IDEA-20261016-session-stop-hunt-fx)
# Same SHORT pattern, read as bid bars, with a "pip" of 0.01 and a constant spread of 0.05 (5 pips).

PIP = 0.01
FX = fx_execution(PIP, 5.0)  # 1 pip buffer, 0.7 pip commission, 0.2 pip slippage on E1 entry and stop exits
FX_STOP = 101.5 + 0.05 + 0.01  # short: beyond the bid extreme + spread (the ask that hits it) + 1 pip
FX_RISK = FX_STOP - 99.95


def test_fx_short_sells_bid_and_exits_on_ask():
    st, space, why = find_setup(make_bars(SHORT), london_session(DAY), FX)
    assert why == "ok" and st is not None and space is not None
    assert st.entry == 99.95 and st.stop == pytest.approx(FX_STOP)
    t, _ = simulate(st, space, "E1", "T1", FX)
    # target 97.0 is hit when the ask (bid low 96.9 + 0.05) reaches it
    assert t is not None and t.exit_kind == "target" and t.exit == 97.0
    assert t.r_gross == pytest.approx((99.95 - 97.0) / FX_RISK)
    assert t.cost_r == pytest.approx((0.7 + 0.2) * PIP / FX_RISK)  # no slippage on a target exit

    w = list(SHORT)
    w[9] = (98.6, 98.7, 96.97, 97.5)  # bid reaches 97.0 but the ask (97.02) does not
    st, space, _ = find_setup(make_bars(w), london_session(DAY), FX)
    assert st is not None and space is not None
    t, _ = simulate(st, space, "E1", "T1", FX)
    assert t is not None and t.exit_kind == "time" and t.exit == pytest.approx(97.5 + 0.05)  # buys back at the ask
    zero = fx_execution(PIP, 0.0)
    st0, space0, _ = find_setup(make_bars(w), london_session(DAY), zero)
    assert st0 is not None and space0 is not None
    t0, _ = simulate(st0, space0, "E1", "T1", zero)
    assert t0 is not None and t0.exit_kind == "target"


def test_fx_short_stop_triggers_on_ask():
    w = list(SHORT)
    w[8] = (99.95, 101.52, 99.5, 100.0)  # ask high 101.57 >= stop 101.56
    st, space, _ = find_setup(make_bars(w), london_session(DAY), FX)
    assert st is not None and space is not None
    t, _ = simulate(st, space, "E1", "T1", FX)
    assert t is not None and t.exit_kind == "stop" and t.r_gross == pytest.approx(-1.0)
    assert t.cost_r == pytest.approx((0.7 + 0.2 + 0.2) * PIP / FX_RISK)
    w[8] = (99.95, 101.505, 99.5, 100.0)  # ask high 101.555 < stop
    st, space, _ = find_setup(make_bars(w), london_session(DAY), FX)
    assert st is not None and space is not None
    t, _ = simulate(st, space, "E1", "T1", FX)
    assert t is not None and t.exit_kind != "stop"


def test_fx_long_buys_ask_and_exits_on_bid():
    st, space, why = find_setup(mirror(make_bars(SHORT)), london_session(DAY), FX)
    assert why == "ok" and st is not None and space is not None and st.side == 1
    # real prices: BOS bid close 100.05, entry at the ask 100.10, stop 1 pip below the swept bid low 98.5
    assert -st.entry == pytest.approx(100.10) and -st.stop == pytest.approx(98.49)
    t, _ = simulate(st, space, "E1", "T1", FX)
    assert t is not None and t.exit_kind == "target" and -t.exit == pytest.approx(103.0)  # bid high 103.1 >= 103
    assert t.r_gross == pytest.approx((103.0 - 100.10) / (100.10 - 98.49))


def test_fx_e2_long_limit_needs_the_ask():
    w = list(SHORT)
    w[8] = (99.95, 100.78, 99.9, 100.5)  # mirrored: bid low 99.22 above the buy limit 99.25 - spread = 99.20
    w[9] = (100.5, 100.6, 96.0, 96.5)
    b = mirror(make_bars(w))
    st, space, _ = find_setup(b, london_session(DAY), FX)
    assert st is not None and space is not None
    assert simulate(st, space, "E2", "T2", FX) == (None, "e2_unfilled")
    zero = fx_execution(PIP, 0.0)
    st0, space0, _ = find_setup(b, london_session(DAY), zero)
    assert st0 is not None and space0 is not None
    t0, why = simulate(st0, space0, "E2", "T2", zero)
    assert why == "ok" and t0 is not None and -t0.entry == pytest.approx(99.25)


def test_fx_bingx_costs_and_crypto_defaults():
    st, space, _ = find_setup(make_bars(SHORT), london_session(DAY), FX)
    assert st is not None and space is not None
    t, _ = simulate(st, space, "E1", "T1", FX)
    assert t is not None
    assert cost_r(t, Costs(0.0007, 0.0007, 0.0007)) == pytest.approx(0.0007 * (99.95 + 97.0) / FX_RISK)
    # the default execution is the crypto card's: same setup and trade as without the argument
    b = make_bars(SHORT)
    st_c, sp_c, _ = find_setup(b, london_session(DAY), CRYPTO)
    assert st_c is not None and sp_c is not None
    assert st_c == find_setup(b, london_session(DAY))[0]
    tc = simulate(st_c, sp_c, "E1", "T1")[0]
    assert tc is not None and simulate(st_c, sp_c, "E1", "T1", CRYPTO)[0] == tc
    assert cost_r(tc, Costs()) == pytest.approx(MARKET_SIDE * (99.95 + 97.0) / RISK)


def test_fx_skip_reasons():
    s = london_session(DAY)
    b = make_bars(SHORT)
    vol = np.ones(b.t.size)
    assert fx_skip_reason(b, vol, s) is None
    assert fx_skip_reason(b, np.zeros(b.t.size), s) == "zero_volume"
    flat = Bars(b.t, b.o.copy(), b.h.copy(), b.low.copy(), b.c.copy())
    rng = (b.t >= s.range_start) & (b.t < s.range_end)
    idx = np.flatnonzero(rng)[: int(rng.sum() * 0.6)]
    flat.h[idx] = flat.o[idx]
    flat.low[idx] = flat.o[idx]
    flat.c[idx] = flat.o[idx]
    assert fx_skip_reason(flat, vol, s) == "flat_range"
    keep = b.t != ms("2024-01-15T12:00")
    g = Bars(b.t[keep], b.o[keep], b.h[keep], b.low[keep], b.c[keep])
    assert fx_skip_reason(g, vol[keep], s) == "gap"


def test_fx_no_lookahead_after_exit():
    s = london_session(DAY)
    b = make_bars(SHORT)
    vol = np.ones(b.t.size)
    st, space, _ = find_setup(b, s, FX)
    assert st is not None and space is not None
    variants = (("E1", "T1"), ("E1", "T2"), ("E2", "T1"))
    ref = [simulate(st, space, ek, tk, FX)[0] for ek, tk in variants]
    later = b.t >= s.exit_at
    b2 = Bars(b.t, b.o.copy(), b.h.copy(), b.low.copy(), b.c.copy())
    b2.h[later] += 50
    b2.low[later] -= 50
    v2 = vol.copy()
    v2[later] = 0.0
    assert fx_skip_reason(b2, v2, s) is None
    st2, space2, _ = find_setup(b2, s, FX)
    assert st2 == st and space2 is not None
    assert [simulate(st2, space2, ek, tk, FX)[0] for ek, tk in variants] == ref
