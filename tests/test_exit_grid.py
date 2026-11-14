"""Exit grid (IDEA-20261114-exit-grid): analytic references for E (split) and F (ATR trail), the worst-case order
inside an hour, and no look-ahead in the structural level D and in the results."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from tradedesk.analytics import exit_grid as eg
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, atr, segment_pos
from tradedesk.analytics.stop_management import Fees, StopRule, simulate_stop_rule

ZERO = Fees(0.0, 0.0, 0.0, 0.0)  # no costs: R is exactly (exit - entry) / (entry - stop)
E0, S0 = 100.0, 90.0  # 1R = 10


def bars(ohlc: list[tuple[float, float, float, float]], t0: int = 0) -> Bars:
    return Bars.from_rows([[t0 + k * HOUR_MS, o, h, lo, c] for k, (o, h, lo, c) in enumerate(ohlc)], HOUR_MS)


# ---------------------------------------------------------------- E: half at 1R, half at 3R with break-even


def test_split_bar_touching_stop_and_1r_closes_both_halves_at_the_stop() -> None:
    p = bars([(100, 111, 89, 95), (95, 140, 94, 130)])
    o = eg.simulate_split(p, 0, E0, S0, 10 * HOUR_MS, ZERO)
    assert o is not None
    assert o.first.exit_kind == "stop" and o.second.exit_kind == "stop"
    assert o.r_net == pytest.approx(-1.0)


def test_split_1r_then_3r() -> None:
    p = bars([(100, 111, 95, 108), (108, 131, 105, 129)])
    o = eg.simulate_split(p, 0, E0, S0, 10 * HOUR_MS, ZERO)
    assert o is not None
    assert (o.first.exit_kind, o.second.exit_kind) == ("target", "target")
    assert o.r_net == pytest.approx(0.5 * 1.0 + 0.5 * 3.0)


def test_split_1r_then_break_even() -> None:
    # the first bar reaches 1R and dips to 95 (> stop): break-even only from the next bar, which comes back to 100
    p = bars([(100, 111, 95, 108), (108, 115, 99, 101), (101, 140, 100, 139)])
    o = eg.simulate_split(p, 0, E0, S0, 10 * HOUR_MS, ZERO)
    assert o is not None
    assert o.second.exit_kind == "be"
    assert o.r_net == pytest.approx(0.5 * 1.0 + 0.5 * 0.0)


def test_split_costs_are_per_half() -> None:
    f = Fees()
    p = bars([(100, 111, 95, 108), (108, 131, 105, 129)])
    o = eg.simulate_split(p, 0, E0, S0, 10 * HOUR_MS, f)
    a = simulate_stop_rule(p, 0, E0, S0, 110.0, 10 * HOUR_MS, StopRule("A"), fees=f)
    b = simulate_stop_rule(p, 0, E0, S0, 130.0, 10 * HOUR_MS, StopRule("x", be_r=1.0), fees=f)
    assert o is not None and a is not None and b is not None
    assert o.r_net == pytest.approx(0.5 * a.r_net + 0.5 * b.r_net)
    assert o.cost_r == pytest.approx(0.5 * a.cost_r + 0.5 * b.cost_r)


# ---------------------------------------------------------------- F: 3 x ATR trail after 1R


def test_trail_reference_path_tightens_only() -> None:
    # atr 5 -> trail 15 under HH. bar0 activates (high 112 >= 110) and dips to 96: the trail (97) is not active yet.
    # bar1 stop 97; bar2 stop 105 (HH 120); bar3 HH still 120 -> stop stays 105, the low 104 exits at 105 (+0.5R)
    p = bars([(100, 112, 96, 110), (110, 120, 105, 118), (118, 119, 110, 112), (112, 113, 104, 106)])
    o = eg.simulate_atr_trail(p, 0, E0, S0, 10 * HOUR_MS, atr=5.0, fees=ZERO)
    assert o is not None
    assert o.exit_kind == "trail" and o.exit == pytest.approx(105.0)
    assert o.r_net == pytest.approx(0.5)
    assert o.move_ms == HOUR_MS


def test_trail_never_below_the_initial_stop() -> None:
    # atr 10 -> HH - 30 = 82 < 90: the stop stays at 90
    p = bars([(100, 112, 96, 110), (110, 111, 91, 95), (95, 96, 89, 90)])
    o = eg.simulate_atr_trail(p, 0, E0, S0, 10 * HOUR_MS, atr=10.0, fees=ZERO)
    assert o is not None
    assert o.exit_kind == "stop" and o.r_net == pytest.approx(-1.0) and not o.moved


def test_trail_worst_order_activation_and_stop_in_one_bar() -> None:
    p = bars([(100, 125, 89, 120), (120, 130, 119, 129)])
    o = eg.simulate_atr_trail(p, 0, E0, S0, 10 * HOUR_MS, atr=5.0, fees=ZERO)
    assert o is not None
    assert o.exit_kind == "stop" and o.r_net == pytest.approx(-1.0)


def test_trail_gap_exits_at_the_open() -> None:
    p = bars([(100, 112, 96, 110), (110, 120, 105, 118), (100, 101, 95, 96)])
    o = eg.simulate_atr_trail(p, 0, E0, S0, 10 * HOUR_MS, atr=5.0, fees=ZERO)
    assert o is not None
    assert o.exit_kind == "trail_gap" and o.exit == pytest.approx(100.0) and o.r_net == pytest.approx(0.0)


def test_trail_time_exit_and_fees() -> None:
    f = Fees()
    p = bars([(100, 105, 95, 104), (104, 106, 101, 103), (103, 200, 102, 199)])
    o = eg.simulate_atr_trail(p, 0, E0, S0, 2 * HOUR_MS, atr=5.0, fees=f)
    assert o is not None
    assert o.exit_kind == "time" and o.exit == pytest.approx(103.0)
    ru = (E0 - S0) + f.risk_taker * (E0 + S0)
    assert o.r_net == pytest.approx((103.0 - E0) / ru - (f.maker * E0 + f.taker_exit * 103.0) / ru)


def test_trail_short_on_mirrored_prices() -> None:
    # short at 100, stop 110: the mirrored path is the long reference path above
    real = bars(
        [(-o, -lo, -h, -c) for o, h, lo, c in [(100, 112, 96, 110), (110, 120, 105, 118), (118, 119, 110, 112), (112, 113, 104, 106)]]
    )
    o = eg.simulate_atr_trail(real.mirrored(), 0, E0, S0, 10 * HOUR_MS, atr=5.0, fees=ZERO)
    assert o is not None and o.r_net == pytest.approx(0.5)


def test_trail_needs_a_positive_atr() -> None:
    p = bars([(100, 112, 96, 110)])
    assert eg.simulate_atr_trail(p, 0, E0, S0, HOUR_MS, atr=math.nan) is None
    assert eg.simulate_atr_trail(p, 0, E0, S0, HOUR_MS, atr=0.0) is None


# ---------------------------------------------------------------- targets, limits, cap


def test_targets() -> None:
    assert eg.r_target(E0, S0, 1.5) == pytest.approx(115.0)
    assert eg.floored_target(E0, S0, 104.0) == pytest.approx(110.0)  # closer than 1R -> floor
    assert eg.floored_target(E0, S0, 137.0) == pytest.approx(137.0)
    assert eg.floored_target(E0, S0, math.nan) == pytest.approx(110.0)


def test_limit_start_matches_the_passport_rule() -> None:
    p = bars([(105, 106, 99, 101), (101, 102, 100, 101), (101, 102, 89, 95)])
    assert eg.limit_start(p, 0, S0, 10 * HOUR_MS) == (HOUR_MS, 9 * HOUR_MS)  # fill bar above the stop: next hour
    assert eg.limit_start(p, 2, S0, 10 * HOUR_MS) == (2 * HOUR_MS, 10 * HOUR_MS)  # fill bar touches the stop


def test_cap_per_bar_keeps_the_best_ranked() -> None:
    t = np.array([5, 5, 5, 7, 5])
    rank = np.array([3.0, 0.0, 2.0, 9.0, 1.0])
    assert eg.cap_per_bar(t, rank, 2).tolist() == [False, True, False, True, True]


def test_mde80() -> None:
    assert eg.mde80(0.1) == pytest.approx(0.28)


# ---------------------------------------------------------------- no look-ahead


def _random_path(seed: int, n: int = 200) -> list[tuple[float, float, float, float]]:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.01, n))
    lo = np.minimum(o, c) * (1 - rng.uniform(0, 0.01, n))
    return list(zip(o, h, lo, c, strict=True))


def test_level_d_and_results_before_t_ignore_later_bars() -> None:
    rows = _random_path(1)
    cut = 120
    changed = rows[:cut] + [(o * 1.3, h * 1.5, lo * 0.6, c * 0.9) for o, h, lo, c in rows[cut:]]
    a, b = bars(rows), bars(changed)
    t_fill = cut * HOUR_MS  # the fill hour and everything after it differ
    assert eg.window_high(a, 10 * HOUR_MS, t_fill) == eg.window_high(b, 10 * HOUR_MS, t_fill)
    assert eg.window_high(a, 10 * HOUR_MS, t_fill + HOUR_MS) != eg.window_high(b, 10 * HOUR_MS, t_fill + HOUR_MS)
    n_same = 0
    for k in range(0, 100, 3):
        e = float(rows[k][3])
        st = e * 0.985
        for sim in (
            lambda p, e=e, st=st, k=k: eg.simulate_atr_trail(p, k * HOUR_MS, e, st, 300 * HOUR_MS, atr=0.8, fees=Fees()),
            lambda p, e=e, st=st, k=k: eg.simulate_split(p, k * HOUR_MS, e, st, 300 * HOUR_MS, Fees()),
        ):
            x, y = sim(a), sim(b)
            assert x is not None and y is not None
            ex = x.first.exit_ms if isinstance(x, eg.SplitOutcome) else x.exit_ms
            ex = max(ex, x.second.exit_ms) if isinstance(x, eg.SplitOutcome) else ex
            if ex <= t_fill:  # ended before the change: must be identical
                n_same += 1
                assert x.r_net == y.r_net and x.exit_kind == y.exit_kind
    assert n_same > 10


def _verify_script() -> Any:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import verify_exit_grid as vg

    return vg


def test_d_level_s3_is_the_opposite_bound_in_trade_space() -> None:
    vg = _verify_script()
    ind = {"range_high": 120.0, "range_low": 80.0, "impulse_extreme": 130.0}
    p = bars([(100, 101, 99, 100)] * 5)
    long = SimpleNamespace(side="long", indicators=ind, signal_ts=0)
    short = SimpleNamespace(side="short", indicators=ind, signal_ts=0)
    assert vg.d_level("S3", long, 1.0, p, 4 * HOUR_MS, -1) == 120.0
    # short: prices negated, the target is the range low (80) -> -80 in the mirrored (long) space
    lvl = vg.d_level("S3", short, -1.0, p.mirrored(), 4 * HOUR_MS, -1)
    assert lvl == -80.0
    # short entry 100, stop 110 (trade space -100 / -110): the low 80 is 2R away, above the 1R floor
    assert eg.floored_target(-100.0, -110.0, lvl) == -80.0
    assert vg.d_level("S14", long, 1.0, p, 4 * HOUR_MS, -1) == 130.0
    assert vg.d_level("S17", long, 1.0, p, 4 * HOUR_MS, -1) is None


def test_d_level_s12_window_starts_at_the_first_fvg_bar_and_ends_before_the_fill_hour() -> None:
    vg = _verify_script()
    bar_ms = 4 * HOUR_MS
    sig_ts = 10 * HOUR_MS  # the third FVG bar opens at 10h, the first at 10h - 2 * 4h = 2h
    t_fill = 14 * HOUR_MS
    rows = [(100.0, 100.0 + k, 99.0 - k, 100.0) for k in range(20)]
    rows[1] = (100.0, 500.0, 50.0, 100.0)  # opens 1h, one hour before the window: must not be read
    rows[14] = (100.0, 600.0, 40.0, 100.0)  # the fill hour: must not be read
    p = bars(rows)
    long = SimpleNamespace(side="long", indicators={}, signal_ts=sig_ts)
    short = SimpleNamespace(side="short", indicators={}, signal_ts=sig_ts)
    assert vg.d_level("S12", long, 1.0, p, bar_ms, t_fill) == 113.0  # bars 2h..13h
    # short: the highest high of the mirrored path is minus the lowest low of bars 2h..13h (99 - 13 = 86)
    assert vg.d_level("S12", short, -1.0, p.mirrored(), bar_ms, t_fill) == -86.0
    rows[2] = (100.0, 300.0, 60.0, 100.0)  # the first bar of the window is read
    assert vg.d_level("S12", long, 1.0, bars(rows), bar_ms, t_fill) == 300.0


def test_atr_at_the_signal_bar_ignores_later_bars() -> None:
    rows = _random_path(2, 80)
    changed = rows[:50] + [(o * 2, h * 2.5, lo * 0.5, c) for o, h, lo, c in rows[50:]]
    a, b = bars(rows), bars(changed)
    xa, xb = atr(a, 14, segment_pos(a.t)), atr(b, 14, segment_pos(b.t))
    assert np.array_equal(xa[:50], xb[:50], equal_nan=True)
    assert np.isfinite(xa[49])
