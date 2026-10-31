"""Item 10d: follow-up exit of a filled limit without a confirmation (analytics/limit_followup.py)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.limit_followup import confirm_bar, followup_trade, placebo_contrast
from tradedesk.analytics.stop_management import Fees, StopRule

H = 3_600_000
RULE_B = StopRule("B", be_frac=0.5)
F = Fees()
LEVEL, STOP, TARGET = 100.0, 95.0, 110.0
HOLD = 7 * 24 * H


def bars(rows: list[tuple[float, float, float, float]]) -> Bars:
    return Bars.from_rows([[i * H, o, h, lo, c] for i, (o, h, lo, c) in enumerate(rows)], H)


def base_rows() -> list[tuple[float, float, float, float]]:
    # bar 0: the limit fills (low 99.5 < 100), high 101.5; bars 1..6 stay under 101.5 and above the stop, no
    # break-even trigger (105); bar 6 closes at 99.0; afterwards the price runs to the target
    rows = [(101.0, 101.5, 99.5, 100.5)]
    rows += [(100.5, 101.0, 98.0, 99.5)] * 5
    rows += [(99.5, 100.5, 98.5, 99.0)]
    rows += [(99.0, 104.0, 98.8, 103.5), (103.5, 111.0, 103.0, 110.5)]
    rows += [(110.5, 111.0, 110.0, 110.5)] * 10
    return rows


def run(rows: list[tuple[float, float, float, float]]):
    return followup_trade(bars(rows), 0, 20 * H, LEVEL, STOP, TARGET, HOLD, RULE_B, F)


def test_exit_at_close_of_sixth_bar_without_confirmation() -> None:
    fu = run(base_rows())
    assert fu is not None
    assert fu.k == 0 and fu.m == -1 and fu.open_at_end and fu.early
    assert fu.base.exit_kind == "target"
    ru = (LEVEL - STOP) + F.risk_taker * (LEVEL + STOP)
    want = (99.0 - LEVEL) / ru - (F.maker * LEVEL + F.taker_exit * 99.0) / ru
    assert fu.mod.exit_kind == "time"
    assert fu.mod.exit == pytest.approx(99.0)
    assert fu.mod.r_net == pytest.approx(want, abs=1e-12)
    assert fu.mod.exit_ms == 7 * H  # close of bar k + 6


def test_confirmation_keeps_the_base_trade() -> None:
    rows = base_rows()
    rows[3] = (100.0, 102.5, 99.8, 102.0)  # close above the level and above the fill bar's high (101.5)
    fu = run(rows)
    assert fu is not None and fu.m == 3 and not fu.early
    assert fu.mod == fu.base


def test_close_above_level_but_not_above_fill_high_is_no_confirmation() -> None:
    rows = base_rows()
    rows[2] = (100.0, 101.4, 99.8, 101.2)  # > level, < high[k] = 101.5
    assert run(rows).early  # type: ignore[union-attr]


def test_confirmation_after_the_window_does_not_count() -> None:
    rows = base_rows()
    rows[7] = (99.0, 104.0, 98.8, 103.5)  # bar k + 7 closes above: too late
    fu = run(rows)
    assert fu is not None and fu.m == -1 and fu.early


def test_stop_inside_the_window_leaves_the_base() -> None:
    rows = base_rows()
    rows[4] = (99.5, 100.0, 94.0, 96.0)
    fu = run(rows)
    assert fu is not None and fu.base.exit_kind == "stop"
    assert not fu.open_at_end and not fu.early and fu.mod == fu.base


def test_pessimistic_fill_hour_leaves_the_base() -> None:
    rows = base_rows()
    rows[0] = (101.0, 101.5, 94.5, 96.0)  # the fill bar also reaches the stop
    fu = run(rows)
    assert fu is not None and fu.base.exit_kind == "stop" and fu.mod == fu.base and not fu.early


def test_unfilled_is_none() -> None:
    rows = [(r[0] + 10, r[1] + 10, r[2] + 10, r[3] + 10) for r in base_rows()]
    assert run(rows) is None


def test_no_look_ahead_decision_uses_bars_up_to_window_end() -> None:
    a = run(base_rows())
    for later in ((99.0, 99.5, 90.0, 91.0), (99.0, 130.0, 98.0, 125.0)):
        rows = base_rows()
        for i in range(7, len(rows)):
            rows[i] = later
        b = run(rows)
        assert b is not None and a is not None
        assert (b.early, b.m, b.open_at_end) == (a.early, a.m, a.open_at_end)
        assert b.mod == a.mod  # the early exit does not depend on bars after k + 6
    p = bars(base_rows())
    assert confirm_bar(p, 0, LEVEL) == -1


def test_confirm_bar_ignores_bars_after_window_and_handles_holes() -> None:
    rows = base_rows()
    rows[5] = (100.0, 103.0, 99.9, 102.0)
    p = bars(rows)
    assert confirm_bar(p, 0, LEVEL) == 5
    keep = np.array([i != 2 for i in range(len(p))])  # a missing 1h bar inside the window
    q = Bars(p.t[keep], p.o[keep], p.h[keep], p.low[keep], p.c[keep], H)
    assert confirm_bar(q, 0, LEVEL) == 4  # index in the holed path, same bar by time (t = 5 h)
    assert int(q.t[4]) == 5 * H


S_LEVEL, S_STOP, S_TARGET = 100.0, 105.0, 90.0  # a short limit in real prices


def short_rows() -> list[tuple[float, float, float, float]]:
    # bar 0: the sell limit fills (high 100.5 > 100), low 98.5; bars 1..6 stay above 98.5 and under the stop,
    # no break-even trigger (95); bar 6 closes at 101.0; afterwards the price falls to the target
    rows = [(99.0, 100.5, 98.5, 99.5)]
    rows += [(99.5, 102.0, 99.0, 100.5)] * 5
    rows += [(100.5, 101.5, 99.5, 101.0)]
    rows += [(101.0, 101.2, 96.0, 96.5), (96.5, 97.0, 89.0, 89.5)]
    rows += [(89.5, 90.0, 89.0, 89.5)] * 10
    return rows


def run_short(rows: list[tuple[float, float, float, float]]):
    # the runner's convention: a short is the long of the mirrored path with negated levels
    p = bars(rows).mirrored()
    return followup_trade(p, 0, 20 * H, -S_LEVEL, -S_STOP, -S_TARGET, HOLD, RULE_B, F)


def test_short_exits_at_close_of_sixth_bar_without_confirmation() -> None:
    fu = run_short(short_rows())
    assert fu is not None
    assert fu.k == 0 and fu.m == -1 and fu.open_at_end and fu.early
    assert fu.base.exit_kind == "target"
    ru = (S_STOP - S_LEVEL) + F.risk_taker * (S_LEVEL + S_STOP)
    want = (S_LEVEL - 101.0) / ru - (F.maker * S_LEVEL + F.taker_exit * 101.0) / ru
    assert fu.mod.exit_kind == "time"
    assert fu.mod.exit == pytest.approx(-101.0)  # mirrored price of the real close 101.0
    assert fu.mod.r_net == pytest.approx(want, abs=1e-12)
    assert fu.mod.exit_ms == 7 * H


def test_short_close_below_level_but_not_below_fill_low_is_no_confirmation() -> None:
    rows = short_rows()
    rows[2] = (100.0, 100.2, 98.6, 98.8)  # < level, > low[k] = 98.5
    fu = run_short(rows)
    assert fu is not None and fu.m == -1 and fu.early


def test_short_close_below_level_and_below_fill_low_confirms() -> None:
    rows = short_rows()
    rows[3] = (100.0, 100.2, 97.5, 98.0)  # < level and < low[k] = 98.5
    fu = run_short(rows)
    assert fu is not None and fu.m == 3 and not fu.early
    assert fu.mod == fu.base


def test_placebo_contrast_analytic() -> None:
    d = np.array([-0.5, 0.3, 0.0, 0.2])
    early = np.array([True, False, False, False])
    open_ = np.array([True, True, False, True])
    x = placebo_contrast(d, early, open_)
    q = 1 / 3
    assert x == pytest.approx([-0.5 * (1 - q), 0.3 * (-q), 0.0, 0.2 * (-q)])
    assert x.mean() == pytest.approx((d * early).mean() - q * (d * open_).mean())
