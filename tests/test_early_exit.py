import numpy as np
import pytest

from tradedesk.analytics.course_s14_ema import HOUR_MS, MARKET_SIDE, Bars, simulate
from tradedesk.analytics.early_exit import BASE, ExitRule, pick_placebo_times, simulate_managed

T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC
HOLD = 168 * HOUR_MS
E16 = ExitRule(check_ms=16 * HOUR_MS, min_mfe_r=0.5)
BE1 = ExitRule(be_trigger_r=1.0)


def path(o, h, lo, c, t0=T0, step=HOUR_MS) -> Bars:
    n = len(c)
    return Bars(t0 + np.arange(n, dtype=np.int64) * step, *(np.asarray(x, dtype=float) for x in (o, h, lo, c)), step)


def flat(n=200, px=100.0, up=1.0, dn=1.0) -> tuple[list[float], list[float], list[float], list[float]]:
    return [px] * n, [px + up] * n, [px - dn] * n, [px] * n


def random_walk(rng, n=200, px=100.0, vol=0.6):
    c = px + np.cumsum(rng.normal(0, vol, n))
    o = np.concatenate([[px], c[:-1]])
    h = np.maximum(o, c) + np.abs(rng.normal(0, vol / 2, n))
    lo = np.minimum(o, c) - np.abs(rng.normal(0, vol / 2, n))
    return path(o, h, lo, c)


# entry 100, stop 95 (R = 5), target 110 (2R); +0.5R = 102.5, +1R = 105


def test_base_rule_equals_step1_simulate_on_random_paths():
    rng = np.random.default_rng(1)
    for _ in range(300):
        p = random_walk(rng)
        stop = 100 - rng.uniform(1, 6)
        target = 100 + rng.uniform(1, 12)
        a = simulate(p, T0, 100.0, stop, target, HOLD)
        b = simulate_managed(p, T0, 100.0, stop, target, HOLD, BASE)
        assert a is not None and b is not None
        assert (b.exit_kind, b.exit_ms, b.both_touched) == (a.exit_kind, a.exit_ms, a.both_touched)
        assert b.r_gross == pytest.approx(a.r_gross, abs=1e-12)
        assert b.cost_r == pytest.approx(a.cost_r, abs=1e-12)
        assert b.mfe_r == pytest.approx(a.mfe_r, abs=1e-12)


def test_early_exit_at_close_of_the_check_bar():
    o, h, lo, c = flat(up=1.0)  # best high 101 < 102.5
    c[15] = 100.5  # the 16th bar after the entry closes at entry + 16 h
    r = simulate_managed(path(o, h, lo, c), T0, 100.0, 95.0, 110.0, HOLD, E16)
    assert r.exit_kind == "early"
    assert r.exit == 100.5
    assert r.exit_ms == T0 + 16 * HOUR_MS
    assert r.r_gross == pytest.approx(0.1)
    assert r.cost_r == pytest.approx(MARKET_SIDE * (100 + 100.5) / 5)
    assert r.mfe_r == pytest.approx(0.2)


def test_no_early_exit_after_half_r_excursion():
    o, h, lo, c = flat(up=1.0)
    h[5] = 102.5  # reached +0.5R before the check
    r = simulate_managed(path(o, h, lo, c), T0, 100.0, 95.0, 110.0, HOLD, E16)
    assert r.exit_kind == "time"
    assert r.exit_ms == T0 + HOLD


def test_stop_before_the_check_is_a_stop():
    o, h, lo, c = flat()
    lo[10] = 94.0
    r = simulate_managed(path(o, h, lo, c), T0, 100.0, 95.0, 110.0, HOLD, E16)
    assert r.exit_kind == "stop" and r.r_gross == pytest.approx(-1.0)
    assert r.exit_ms == T0 + 11 * HOUR_MS


def test_early_exit_ignores_bars_after_the_check():
    o, h, lo, c = flat()
    base = simulate_managed(path(o, h, lo, c), T0, 100.0, 95.0, 110.0, HOLD, E16)
    rng = np.random.default_rng(7)
    o2, h2, lo2, c2 = (np.array(x) for x in (o, h, lo, c))
    noise = rng.normal(0, 5, 200 - 16)
    c2[16:] = 100 + noise
    h2[16:] = c2[16:] + 20  # would hit the target
    lo2[16:] = c2[16:] - 20  # would hit the stop
    o2[16:] = c2[16:]
    alt = simulate_managed(path(o2, h2, lo2, c2), T0, 100.0, 95.0, 110.0, HOLD, E16)
    assert (alt.exit_kind, alt.exit, alt.exit_ms) == (base.exit_kind, base.exit, base.exit_ms) == ("early", 100.0, T0 + 16 * HOUR_MS)


def test_managed_result_ignores_bars_after_the_exit():
    rng = np.random.default_rng(3)
    for rule in (BASE, E16, BE1, ExitRule(check_ms=16 * HOUR_MS, be_trigger_r=1.0)):
        for _ in range(50):
            p = random_walk(rng)
            r = simulate_managed(p, T0, 100.0, 97.0, 104.0, HOLD, rule)
            k = int(np.searchsorted(p.t, r.exit_ms))  # first bar opening at/after the exit bar's close
            c = p.c.copy()
            c[k:] = 50 + rng.normal(0, 30, c.size - k)
            o, h, lo = p.o.copy(), p.h.copy(), p.low.copy()
            o[k:], h[k:], lo[k:] = c[k:], c[k:] + 40, c[k:] - 40  # later bars hit both the stop and the target
            q = Bars(p.t, o, h, lo, c, p.bar_ms)
            r2 = simulate_managed(q, T0, 100.0, 97.0, 104.0, HOLD, rule)
            assert (r2.exit_kind, r2.exit, r2.exit_ms) == (r.exit_kind, r.exit, r.exit_ms)


def test_breakeven_from_the_next_bar():
    o, h, lo, c = flat()
    h[3], lo[3] = 105.0, 98.0  # trigger bar dips below the entry: the old stop still applies there
    o[4], lo[4] = 100.3, 99.5
    r = simulate_managed(path(o, h, lo, c), T0, 100.0, 95.0, 110.0, HOLD, BE1)
    assert r.exit_kind == "be" and r.exit == 100.0
    assert r.r_gross == 0.0
    assert r.cost_r == pytest.approx(MARKET_SIDE * 200 / 5)
    assert r.exit_ms == T0 + 5 * HOUR_MS
    assert r.mfe_r == pytest.approx(1.0)


def test_breakeven_gap_exits_at_the_open():
    o, h, lo, c = flat()
    h[3] = 105.0
    o[4], lo[4] = 99.0, 98.5
    r = simulate_managed(path(o, h, lo, c), T0, 100.0, 95.0, 110.0, HOLD, BE1)
    assert r.exit_kind == "be_gap" and r.exit == 99.0 and r.r_gross == pytest.approx(-0.2)


def test_breakeven_not_triggered_below_the_level_and_target_first():
    o, h, lo, c = flat()
    h[3] = 104.9
    lo[4] = 99.5
    r = simulate_managed(path(o, h, lo, c), T0, 100.0, 95.0, 110.0, HOLD, BE1)
    assert r.exit_kind == "time"
    o, h, lo, c = flat()
    h[3] = 111.0  # crosses the trigger and the target in one bar: target
    r = simulate_managed(path(o, h, lo, c), T0, 100.0, 95.0, 110.0, HOLD, BE1)
    assert r.exit_kind == "target" and r.r_gross == pytest.approx(2.0)


def test_no_check_when_data_end_before_it():
    o, h, lo, c = flat(n=10)
    r = simulate_managed(path(o, h, lo, c), T0, 100.0, 95.0, 110.0, HOLD, E16)
    assert r.exit_kind == "data_end"


def test_short_via_mirror():
    o, h, lo, c = flat()
    c[15] = 99.0
    p = path(o, h, lo, c).mirrored()  # short at 100, stop 105, target 90
    r = simulate_managed(p, T0, -100.0, -105.0, -90.0, HOLD, E16)
    assert r.exit_kind == "early" and r.r_gross == pytest.approx(0.2)


def test_optional_stopping_on_a_martingale():
    """Random entries on a driftless walk: an early exit changes the distribution, not the mean (gross, no fees)."""
    rng = np.random.default_rng(11)
    d = []
    for _ in range(3000):
        n = 60
        steps = rng.choice([-1.0, 1.0], size=n)
        c = 100 + np.cumsum(steps)
        o = np.concatenate([[100.0], c[:-1]])
        p = path(o, np.maximum(o, c), np.minimum(o, c), c)
        hold = n * HOUR_MS
        a = simulate_managed(p, T0, 100.0, 95.0, 110.0, hold, BASE, 0.0, 0.0)
        b = simulate_managed(p, T0, 100.0, 95.0, 110.0, hold, ExitRule(check_ms=16 * HOUR_MS), 0.0, 0.0)
        d.append(b.r_gross - a.r_gross)
    d = np.asarray(d)
    assert np.any(d != 0)
    assert abs(d.mean()) < 4 * d.std() / np.sqrt(d.size)


def test_pick_placebo_times():
    c = np.arange(0, 100, dtype=np.int64) * 10
    rng = np.random.default_rng(5)
    got = pick_placebo_times(c, 500, 50, 3, rng, exclude_ms=500)
    assert got.size == 3 and np.all(np.abs(got - 500) <= 50) and 500 not in got and len(set(got.tolist())) == 3
    again = pick_placebo_times(c, 500, 50, 3, np.random.default_rng(5), exclude_ms=500)
    assert np.array_equal(got, again)
    assert pick_placebo_times(c, 5000, 10, 3, rng).size == 0
    assert pick_placebo_times(c, 500, 10, 5, rng, exclude_ms=500).size == 2
