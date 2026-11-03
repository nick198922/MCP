"""S38 audit modules T / D / E (2026-11-03): analytic expectations and no-lookahead checks."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics import s38_audit as m
from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.stop_management import Fees, StopRule, risk_unit

H = m.HOUR_MS
T0 = int(datetime(2025, 3, 3, tzinfo=UTC).timestamp() * 1000)
F = Fees()
NO_BE = StopRule("fixed")
HOLD = 7 * 24 * H


def bars(rows: list[tuple[float, float, float, float]], step: int = H, t0: int = T0) -> Bars:
    return Bars.from_rows([[t0 + i * step, o, h, lo, c] for i, (o, h, lo, c) in enumerate(rows)], step)


# ---------------------------------------------------------------- T


def test_range_touches_counts_bars_near_the_low() -> None:
    lows = [100.0, 95.0, 100.0, 95.4, 96.0, 95.2, 100.0]  # range low 95; ATR 2 -> zone up to 95.5
    rows = [(101.0, 102.0, lo, 101.0) for lo in lows] + [(100.0, 100.0, 90.0, 91.0)]  # bar 7 = breakout s
    b = bars(rows, 4 * H)
    assert m.range_touches(b, 7, 95.0, 2.0, w=7) == 3  # 95.0, 95.4, 95.2 (96.0 is 1 above, > 0.5)
    assert m.range_touches(b, 7, 95.0, 4.0, w=7) == 4  # zone up to 96.0
    assert m.range_touches(b, 7, 95.0, math.nan, w=7) is None
    assert m.range_touches(b, 7, 95.0, 2.0, w=8) is None  # not enough bars before s


def test_range_touches_ignores_the_breakout_bar_and_later() -> None:
    rows = [(101.0, 102.0, 95.0 + 0.1 * (i % 3), 101.0) for i in range(30)] + [(100.0, 100.0, 90.0, 91.0)] * 5
    b = bars(rows, 4 * H)
    before = m.range_touches(b, 30, 95.0, 1.0)
    later = Bars(b.t, b.o, b.h, np.r_[b.low[:30], np.full(5, 95.0)], b.c, b.bar_ms)
    assert m.range_touches(later, 30, 95.0, 1.0) == before == 30


# ---------------------------------------------------------------- D


def test_bar_quote_volume_and_ratio() -> None:
    t = T0 + H * np.arange(8)
    qv = np.array([1.0, 2.0, 3.0, 4.0, 1.0, 1.0, 1.0, 2.0])
    assert m.bar_quote_volume(t, qv, T0, 4 * H) == pytest.approx(10.0)
    assert m.bar_quote_volume(t, qv, T0 + 4 * H, 4 * H) == pytest.approx(5.0)
    gap = np.delete(t, 5)
    assert m.bar_quote_volume(gap, np.delete(qv, 5), T0 + 4 * H, 4 * H) is None  # a missing hour
    assert m.dry_ratio(5.0, 10.0) == pytest.approx(0.5)
    assert m.dry_ratio(5.0, 0.0) is None
    assert m.dry_ratio(None, 10.0) is None


def test_spearman() -> None:
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert m.spearman(x, [10, 20, 30, 40, 50]) == pytest.approx(1.0)
    assert m.spearman(x, [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
    assert m.spearman(x, [1, 1, 2, 2, 3]) == pytest.approx(0.9486833, abs=1e-6)  # ties: average ranks
    assert math.isnan(m.spearman([1.0, 2.0], [1.0, 2.0]))


# ---------------------------------------------------------------- E (long space: trigger above, stop below)


def test_stop_entry_fills_at_trigger_and_hits_target() -> None:
    # trigger 101, stop 99: fill 101 in hour 1, risk ~2, target 105; hour 2 reaches 105
    p = bars([(100.0, 100.5, 99.5, 100.2), (100.2, 101.5, 100.1, 101.2), (101.2, 105.5, 101.0, 105.0)])
    r = m.stop_entry_trade(p, T0, 101.0, 99.0, 8 * H, HOLD, NO_BE, F)
    assert r.status == "filled" and r.fill_ms == T0 + H and r.entry == pytest.approx(101.0)
    ru = risk_unit(101.0, 99.0, F)
    exp = (105.0 - 101.0) / ru - (F.taker_exit * 101.0 + F.maker * 105.0) / ru
    assert r.r_net == pytest.approx(exp)


def test_stop_entry_gap_fills_at_open() -> None:
    p = bars([(100.0, 100.5, 99.5, 100.2), (102.0, 102.5, 101.5, 102.2), (102.2, 102.5, 98.0, 98.5)])
    r = m.stop_entry_trade(p, T0, 101.0, 99.0, 8 * H, HOLD, NO_BE, F)
    assert r.entry == pytest.approx(102.0) and r.outcome is not None and r.outcome.exit_kind == "stop"
    ru = risk_unit(102.0, 99.0, F)
    assert r.r_net == pytest.approx((99.0 - 102.0) / ru - (F.taker_exit * 102.0 + F.taker_exit * 99.0) / ru)


def test_stop_entry_cancel_no_fill_and_pessimistic_fill_hour() -> None:
    p = bars([(100.0, 100.5, 98.5, 99.0), (99.0, 102.0, 98.8, 101.5)])
    assert m.stop_entry_trade(p, T0, 101.0, 99.0, 8 * H, HOLD, NO_BE, F).status == "cancel_stop"
    flat = bars([(100.0, 100.5, 99.5, 100.0)] * 12)
    r = m.stop_entry_trade(flat, T0, 101.0, 99.0, 8 * H, HOLD, NO_BE, F)
    assert r.status == "no_fill" and r.r_net == 0.0
    # trigger and stop in the same hour: a loss at the stop
    both = bars([(100.0, 101.5, 98.5, 100.0), (100.0, 106.0, 99.5, 105.0)])
    r = m.stop_entry_trade(both, T0, 101.0, 99.0, 8 * H, HOLD, NO_BE, F)
    assert r.status == "filled" and r.outcome is not None and r.outcome.exit_kind == "stop"
    # a target inside the fill hour is ignored: the trade runs from the next hour
    tgt = bars([(100.0, 106.0, 99.5, 102.0), (102.0, 102.5, 98.0, 98.5)])
    r = m.stop_entry_trade(tgt, T0, 101.0, 99.0, 8 * H, HOLD, NO_BE, F)
    assert r.outcome is not None and r.outcome.exit_kind == "stop"
    # the order expires after the window: a trigger in hour 8 is not a fill
    late = bars([(100.0, 100.5, 99.5, 100.0)] * 8 + [(100.0, 102.0, 99.8, 101.5)])
    assert m.stop_entry_trade(late, T0, 101.0, 99.0, 8 * H, HOLD, NO_BE, F).status == "no_fill"


def test_stop_entry_unchanged_by_data_after_the_exit() -> None:
    rows = [(100.0, 100.5, 99.5, 100.2), (100.2, 101.5, 100.1, 101.2), (101.2, 105.5, 101.0, 105.0)]
    a = m.stop_entry_trade(bars(rows + [(105.0, 106.0, 104.0, 105.0)] * 5), T0, 101.0, 99.0, 8 * H, HOLD, NO_BE, F)
    b = m.stop_entry_trade(bars(rows + [(50.0, 60.0, 40.0, 55.0)] * 5), T0, 101.0, 99.0, 8 * H, HOLD, NO_BE, F)
    assert a.r_net == pytest.approx(b.r_net)


# ---------------------------------------------------------------- E placebo


def test_delayed_market_trade() -> None:
    p = bars([(100.0, 100.5, 99.5, 100.0), (100.0, 100.5, 99.6, 100.0), (100.0, 104.5, 99.8, 104.0)])
    r = m.delayed_market_trade(p, T0, 1, 98.0, HOLD, NO_BE, F)
    ru = risk_unit(100.0, 98.0, F)
    assert r == pytest.approx(4.0 / ru - (F.taker_exit * 100.0 + F.maker * 104.0) / ru)
    hit = bars([(100.0, 100.5, 97.5, 99.0), (99.0, 100.5, 98.6, 100.0), (100.0, 104.5, 99.8, 104.0)])
    assert m.delayed_market_trade(hit, T0, 1, 98.0, HOLD, NO_BE, F) == 0.0  # the stop was reached before the entry
    assert math.isnan(m.delayed_market_trade(p, T0, 5, 98.0, HOLD, NO_BE, F))  # no bar at that delay


def test_placebo_delay_frequency_and_determinism() -> None:
    r = [1.0] * 8
    assert m.placebo_delay(r, 1.0, [1] * 8, "k") == pytest.approx(1.0)
    assert m.placebo_delay(r, 0.0, [1] * 8, "k") == 0.0
    vals = [m.placebo_delay(r, 0.5, [1] * 8, f"k{i}", n_draw=20) for i in range(200)]
    assert np.mean(vals) == pytest.approx(0.5, abs=0.05)
    assert m.placebo_delay(r, 0.5, [1] * 8, "k7") == m.placebo_delay(r, 0.5, [1] * 8, "k7")
    only3 = [None, None, None, 2.0, None, None, None, None]
    assert m.placebo_delay(only3, 1.0, [0, 0, 0, 1, 0, 0, 0, 0], "z") == pytest.approx(2.0)
