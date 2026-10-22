from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars
from tradedesk.analytics.early_exit import ExitRule, simulate_managed
from tradedesk.analytics.stop_management import (
    Fees,
    StopRule,
    Swings,
    cochran_q,
    max_drawdown,
    risk_unit,
    simulate_stop_rule,
    swing_stops,
)

T0 = 1_700_000_000_000 // HOUR_MS * HOUR_MS
A = StopRule("A")
B = StopRule("B", be_frac=0.5)
F = Fees()


def bars(rows: list[tuple[float, float, float, float]], bar_ms: int = HOUR_MS, t0: int = T0) -> Bars:
    a = np.array(rows, dtype=float)
    t = t0 + np.arange(len(rows), dtype=np.int64) * bar_ms
    return Bars(t, a[:, 0], a[:, 1], a[:, 2], a[:, 3], bar_ms)


def r_net(entry: float, stop: float, px: float, exit_fee: float) -> float:
    return ((px - entry) - F.maker * entry - exit_fee * px) / risk_unit(entry, stop)


def test_target_and_stop_costs():
    up = bars([(100, 101, 99.5, 100.5), (100.5, 106, 100, 105)])
    o = simulate_stop_rule(up, T0, 100, 98, 104, 10 * HOUR_MS, A)
    assert o.exit_kind == "target" and o.exit == 104
    assert o.r_net == pytest.approx(r_net(100, 98, 104, F.maker))
    assert risk_unit(100, 98) == pytest.approx(2 + 0.00055 * 198)
    down = bars([(100, 101, 97, 97.5)])
    o = simulate_stop_rule(down, T0, 100, 98, 104, 10 * HOUR_MS, A)
    assert o.exit_kind == "stop" and o.r_net == pytest.approx(r_net(100, 98, 98, F.taker_exit))
    gap = bars([(100, 100.5, 99, 99.5), (97, 97.5, 96, 96.5)])
    o = simulate_stop_rule(gap, T0, 100, 98, 104, 10 * HOUR_MS, A)
    assert o.exit_kind == "stop_gap" and o.exit == 97


def test_stop_wins_when_both_touched_in_one_bar():
    o = simulate_stop_rule(bars([(100, 105, 97, 100)]), T0, 100, 98, 104, 10 * HOUR_MS, A)
    assert o.exit_kind == "stop"


def test_breakeven_half_way_from_next_bar():
    be = 100 * (1 + 2 * 0.00055)
    # bar 0 reaches 102 (half the way to 104) and in the same bar dips to 99.5: the old stop 98 still holds
    p = bars([(100, 102.2, 99.5, 101), (101, 101.5, 99.9, 100)])
    o = simulate_stop_rule(p, T0, 100, 98, 104, 10 * HOUR_MS, B)
    assert o.exit_kind == "be" and o.exit == pytest.approx(be) and o.moved and o.move_ms == T0 + HOUR_MS
    assert o.r_net == pytest.approx(r_net(100, 98, be, F.taker_exit))
    assert simulate_stop_rule(p, T0, 100, 98, 104, 10 * HOUR_MS, A).exit_kind == "data_end"
    # 49 % of the way does not trigger
    q = bars([(100, 101.96, 99.5, 101), (101, 101.5, 99.9, 100)])
    assert not simulate_stop_rule(q, T0, 100, 98, 104, 10 * HOUR_MS, B).moved


def test_triggers_r_and_price_move():
    assert StopRule("D", be_r=1.0).trigger(100, 98, 110) == pytest.approx(102)
    assert StopRule("E", be_move=0.30 / 20).trigger(100, 98, 110) == pytest.approx(101.5)
    assert StopRule("B", be_frac=0.5).trigger(100, 98, 110) == pytest.approx(105)
    # mirrored short: entry -100 (real 100), trigger -100 + 1.5 = real 98.5
    assert StopRule("E", be_move=0.015).trigger(-100, -102, -90) == pytest.approx(-98.5)


def test_short_on_mirrored_prices():
    real = bars([(100, 100.5, 97.8, 98), (98, 98.2, 99.0 - 1.2, 98)])
    m = real.mirrored()
    o = simulate_stop_rule(m, T0, -100, -102, -96, 10 * HOUR_MS, StopRule("B", be_frac=0.5))
    # real: short 100, stop 102, target 96; bar 0 low 97.8 <= 98 (half way): BE = 100 * (1 - 0.0011) from bar 1
    assert o.moved and o.exit_kind == "data_end"
    be_real = 100 * (1 - 2 * 0.00055)
    p2 = bars([(100, 100.5, 97.8, 98), (98, 99.95, 97.9, 99.5)]).mirrored()
    o2 = simulate_stop_rule(p2, T0, -100, -102, -96, 10 * HOUR_MS, StopRule("B", be_frac=0.5))
    assert o2.exit_kind == "be" and -o2.exit == pytest.approx(be_real)
    ru = (102 - 100) + 0.00055 * 202
    assert o2.r_net == pytest.approx(((100 - be_real) - 0.0002 * 100 - 0.00075 * be_real) / ru)


def test_equals_early_exit_with_its_fees():
    rng = np.random.default_rng(3)
    for _ in range(200):
        c = 100 + np.cumsum(rng.normal(0, 1, 60))
        o = np.concatenate([[100], c[:-1]])
        h = np.maximum(o, c) + rng.uniform(0, 1, 60)
        lo = np.minimum(o, c) - rng.uniform(0, 1, 60)
        p = Bars(T0 + np.arange(60) * HOUR_MS, o, h, lo, c, HOUR_MS)
        fz = Fees(maker=0.0007, taker_exit=0.0007, risk_taker=0.0, be_offset=0.0)
        for rule, er in ((A, ExitRule()), (StopRule("D", be_r=1.0), ExitRule(be_trigger_r=1.0))):
            a = simulate_stop_rule(p, T0, 100, 97, 106, 40 * HOUR_MS, rule, fees=fz)
            b = simulate_managed(p, T0, 100, 97, 106, 40 * HOUR_MS, er)
            assert a.r_net == pytest.approx(b.r_net) and a.exit_ms == b.exit_ms


def test_swing_confirmed_k_bars_later():
    # 4h bars: pivot low at bar 2 (low 95), bars 3 and 4 higher: known at the close of bar 4
    tf = bars(
        [(100, 101, 97, 100), (100, 101, 96, 99), (99, 100, 95, 99), (99, 101, 96, 100), (100, 102, 97, 101), (101, 103, 98, 102)],
        4 * HOUR_MS,
    )
    atr = np.full(6, 2.0)
    sw = swing_stops(tf, atr, k=2, buf_atr=0.5)
    assert list(sw.pivot_ms) == [T0 + 2 * 4 * HOUR_MS]
    assert list(sw.conf_ms) == [T0 + 5 * 4 * HOUR_MS]
    assert sw.level[0] == pytest.approx(95 - 1.0)


def test_trailing_stop_active_after_confirmation_only():
    sw = Swings(np.array([T0 + 2 * HOUR_MS]), np.array([T0 + 5 * HOUR_MS]), np.array([99.0]))
    rows = [(100, 100.5, 99.5, 100)] * 5 + [(100, 100.5, 98.8, 99.5)] + [(99.5, 100, 99.2, 99.6)] * 3
    p = bars(rows)
    c = StopRule("C", trail=True)
    o = simulate_stop_rule(p, T0, 100, 98, 104, 20 * HOUR_MS, c, swings=sw)
    assert o.exit_kind == "trail" and o.exit == 99.0 and o.move_ms == T0 + 5 * HOUR_MS
    # a pivot formed before the entry does not count; a looser level does not loosen the stop
    early = Swings(np.array([T0 - HOUR_MS]), np.array([T0 + HOUR_MS]), np.array([99.0]))
    assert simulate_stop_rule(p, T0, 100, 98, 104, 20 * HOUR_MS, c, swings=early).exit_kind == "data_end"
    loose = Swings(np.array([T0 + HOUR_MS]), np.array([T0 + 2 * HOUR_MS]), np.array([97.0]))
    assert not simulate_stop_rule(p, T0, 100, 98, 104, 20 * HOUR_MS, c, swings=loose).moved
    with pytest.raises(ValueError):
        simulate_stop_rule(p, T0, 100, 98, 104, 20 * HOUR_MS, c)


def test_no_target_rule_ignores_target():
    p = bars([(100, 106, 99.5, 105), (105, 107, 104, 106)])
    o = simulate_stop_rule(p, T0, 100, 98, 104, 2 * HOUR_MS, StopRule("C2", trail=True, use_target=False), swings=Swings.empty())
    assert o.exit_kind in ("time", "data_end") and o.exit == 106


def test_no_lookahead():
    """Bars after time t change neither the swings known by t nor any decision before t."""
    rng = np.random.default_rng(11)
    n = 400
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = np.concatenate([[100], c[:-1]])
    h = np.maximum(o, c) + rng.uniform(0, 1, n)
    lo = np.minimum(o, c) - rng.uniform(0, 1, n)
    p = Bars(T0 + np.arange(n) * HOUR_MS, o, h, lo, c, HOUR_MS)
    from tradedesk.analytics.course_s14_ema import atr, resample, segment_pos

    tf = resample(p, 4)
    a = atr(tf, 14, segment_pos(tf.t))
    sw = swing_stops(tf, a)
    cut = T0 + 200 * HOUR_MS
    # change everything after cut
    k = int(np.searchsorted(p.t, cut))
    c2 = c.copy()
    c2[k:] = c[k:] * 0.5 + 300
    o2, h2, lo2 = o.copy(), h.copy(), lo.copy()
    o2[k:] = c2[k - 1 : -1]
    h2[k:] = np.maximum(o2[k:], c2[k:]) + 0.3
    lo2[k:] = np.minimum(o2[k:], c2[k:]) - 0.3
    p2 = Bars(p.t, o2, h2, lo2, c2, HOUR_MS)
    tf2 = resample(p2, 4)
    sw2 = swing_stops(tf2, atr(tf2, 14, segment_pos(tf2.t)))
    m1, m2 = sw.conf_ms <= cut, sw2.conf_ms <= cut
    assert np.array_equal(sw.conf_ms[m1], sw2.conf_ms[m2])
    assert np.allclose(sw.level[m1], sw2.level[m2])
    # outcomes of trades that end before cut are unchanged
    same = 0
    for e in range(20, 150, 3):
        em = T0 + e * HOUR_MS
        ent = float(p.o[e])
        for rule in (A, B, StopRule("C", trail=True)):
            r1 = simulate_stop_rule(p, em, ent, ent - 3, ent + 6, 300 * HOUR_MS, rule, swings=sw)
            if r1.exit_ms > cut:
                continue
            r2 = simulate_stop_rule(p2, em, ent, ent - 3, ent + 6, 300 * HOUR_MS, rule, swings=sw2)
            assert (r1.exit_kind, r1.exit_ms, r1.move_ms) == (r2.exit_kind, r2.exit_ms, r2.move_ms)
            assert r1.r_net == pytest.approx(r2.r_net)
            same += 1
    assert same > 30


def test_fee_stress_and_helpers():
    s = Fees().scaled(2)
    assert s.maker == pytest.approx(0.0004) and s.taker_exit == pytest.approx(0.0015)
    assert s.risk_taker == 0.00055 and s.be_offset == pytest.approx(0.0011)
    assert max_drawdown(np.array([1.0, -1.0, -2.0, 0.5, 3.0, -1.0])) == pytest.approx(3.0)
    assert max_drawdown(np.array([-1.0, 2.0])) == pytest.approx(1.0)
    q, df, i2 = cochran_q(np.array([0.0, 2.0]), np.array([1.0, 1.0]))
    assert (q, df) == (pytest.approx(2.0), 1) and i2 == pytest.approx(0.5)
    q, df, i2 = cochran_q(np.array([1.0, 1.0, 1.0]), np.array([1.0, 2.0, 3.0]))
    assert q == pytest.approx(0.0) and i2 == 0.0
