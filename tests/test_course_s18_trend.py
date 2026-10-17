import numpy as np
import pytest

from tradedesk.analytics.course_s18_trend import (
    DAY_MS,
    HOUR_MS,
    MARKET_SIDE,
    Bars,
    net_return,
    pos_from_seg,
    resample_partial,
    s18_indicators,
    segments,
    signal_days,
    simulate,
)

T0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC


def hourly_from_daily(closes, *, t0=T0, drop_hours=()) -> Bars:
    """24 flat 1h bars per day at the day's close (open of the first hour = previous close)."""
    c = np.repeat(np.asarray(closes, dtype=float), 24)
    o = np.concatenate([[c[0]], c[:-1]])
    t = t0 + np.arange(c.size, dtype=np.int64) * HOUR_MS
    keep = np.ones(c.size, dtype=bool)
    keep[list(drop_hours)] = False
    h, lo = np.maximum(o, c), np.minimum(o, c)
    return Bars(t[keep], o[keep], h[keep], lo[keep], c[keep], HOUR_MS)


def reclaim_series(n_flat=400, n_low=100, n_high=100, low=90.0, high=120.0, tail=()):
    return [100.0] * n_flat + [low] * n_low + [high] * n_high + list(tail)


# ---------------------------------------------------------------- bars


def test_resample_partial_keeps_partial_days_with_last_hour():
    h1 = hourly_from_daily([1.0, 2.0, 3.0], drop_hours=(5, 6, 47))  # day 0 misses 2 hours, day 1 misses 23:00
    d, seg = resample_partial(h1, segments(h1.t), 24, 20)
    assert list(d.t) == [T0, T0 + 2 * DAY_MS]
    assert list(d.c) == [1.0, 3.0]
    assert list(seg) == [0, 0]


def test_segments_and_positions():
    t = np.array([0, 1, 2, 60, 61]) * HOUR_MS
    seg = segments(t)
    assert list(seg) == [0, 0, 0, 1, 1]
    assert list(pos_from_seg(seg)) == [0, 1, 2, 0, 1]


# ---------------------------------------------------------------- signal


def test_signal_on_first_close_above_all_after_lookback():
    h1 = hourly_from_daily(reclaim_series())
    ind = s18_indicators(h1)
    # EMA100 after 100 days at 90 from 100: 90 + 10 * (99/101)**100 ~ 91.4 > 90; all three above the low closes
    assert ind.e100[499] == pytest.approx(90 + 10 * (99 / 101) ** 100, rel=1e-9)
    sig = signal_days(ind, 60)
    assert list(sig) == [500]
    assert list(signal_days(ind, 60, strict=True)) == [500]
    # lookback longer than the time spent not above all: days 200 .. 399 sit at 100 == EMA (not above), so 90 also works
    assert list(signal_days(ind, 90)) == [500]


def test_needs_all_three_emas_defined_in_lookback():
    h1 = hourly_from_daily(reclaim_series(n_flat=150))  # EMA200 is defined from day 199 only
    ind = s18_indicators(h1)
    assert np.isnan(ind.e200[198]) and np.isfinite(ind.e200[199])
    assert list(signal_days(ind, 60)) == []  # lookback 190 .. 249 contains days without EMA 200
    assert list(signal_days(ind, 50)) == [250]  # lookback 200 .. 249 is fully defined


def test_strict_reading_differs_from_lenient():
    # 300 days at 200, 200 days at 100, 40 days at 105: EMA100 ~ 101.8 -> 105 (close above), EMA200 ~ 113 (close
    # below), so the 105 stretch is neither above all nor below all; then a jump to 300 is above all
    c = [200.0] * 300 + [100.0] * 200 + [105.0] * 40 + [300.0] * 5
    ind = s18_indicators(hourly_from_daily(c))
    d = 540
    assert ind.above_all[d]
    stretch = slice(510, 540)
    assert not ind.above_all[stretch].any() and not ind.below_all[stretch].any()
    assert (ind.d.c[stretch] > ind.e100[stretch]).all() and (ind.d.c[stretch] < ind.e200[stretch]).all()
    assert list(signal_days(ind, 30)) == [540]
    assert list(signal_days(ind, 30, strict=True)) == []


def test_signals_before_t_do_not_depend_on_later_data():
    rng = np.random.default_rng(1)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, 1500)))
    a = s18_indicators(hourly_from_daily(c))
    sa = signal_days(a, 60)
    assert sa.size > 0
    cut = int(sa[len(sa) // 2])
    c2 = c.copy()
    c2[cut + 1 :] = c2[cut + 1 :] * np.exp(rng.normal(0, 0.2, c2.size - cut - 1))
    b = s18_indicators(hourly_from_daily(c2))
    sb = signal_days(b, 60)
    assert list(sa[sa <= cut]) == list(sb[sb <= cut])
    np.testing.assert_allclose(a.e100[: cut + 1], b.e100[: cut + 1])
    np.testing.assert_allclose(a.e4[: cut + 1], b.e4[: cut + 1])


def test_gap_restarts_indicators():
    c = reclaim_series()
    h1 = hourly_from_daily(c)
    keep = (h1.t < T0 + 450 * DAY_MS) | (h1.t >= T0 + 453 * DAY_MS)  # 72 h hole inside the low stretch
    h1 = Bars(h1.t[keep], h1.o[keep], h1.h[keep], h1.low[keep], h1.c[keep], HOUR_MS)
    ind = s18_indicators(h1)
    assert ind.dseg.max() == 1
    assert signal_days(ind, 60).size == 0  # the new segment has no EMA 200 yet


# ---------------------------------------------------------------- trades


def test_trade_exits_on_close_below_ema100():
    c = reclaim_series(tail=[80.0] * 5)
    ind = s18_indicators(hourly_from_daily(c))
    d = 500
    entry_ms = int(ind.d.t[d]) + DAY_MS
    with np.errstate(invalid="ignore"):
        flag = ind.d.c < ind.e100
    tr = simulate(ind.d, ind.dseg, entry_ms, float(ind.d.c[d]), int(ind.dseg[d]), exit_flag=flag)
    assert tr is not None
    assert tr.kind == "ema" and tr.exit == 80.0
    assert tr.exit_ms == int(ind.d.t[600]) + DAY_MS
    assert tr.gross == pytest.approx(80 / 120 - 1)
    assert tr.cost == pytest.approx(MARKET_SIDE * (1 + 80 / 120))
    assert tr.hold_days == pytest.approx(100)


def test_trade_open_end_and_data_end():
    ind = s18_indicators(hourly_from_daily(reclaim_series()))
    d = 500
    entry_ms = int(ind.d.t[d]) + DAY_MS
    flag = np.zeros(len(ind.d), dtype=bool)
    last = int(ind.d.t[-1]) + DAY_MS
    tr = simulate(ind.d, ind.dseg, entry_ms, 120.0, 0, exit_flag=flag, end_ms=last)
    assert tr is not None and tr.kind == "open_end" and tr.gross == pytest.approx(0.0)
    tr2 = simulate(ind.d, ind.dseg, entry_ms, 120.0, 0, exit_flag=flag, end_ms=last + 30 * DAY_MS)
    assert tr2 is not None and tr2.kind == "data_end"


def test_trade_stops_at_segment_hole():
    d = Bars(np.array([0, 1, 2, 10]) * DAY_MS, np.ones(4), np.ones(4) * 2, np.ones(4) * 0.5, np.array([1.0, 1.5, 1.2, 9.0]), DAY_MS)
    seg = segments(d.t)
    tr = simulate(d, seg, DAY_MS, 1.0, 0, exit_flag=np.zeros(4, dtype=bool))
    assert tr is not None and tr.kind == "data_end" and tr.exit == 1.2


def test_stop_gap_and_stop_and_time():
    t = np.arange(4) * DAY_MS
    d = Bars(
        t,
        np.array([10.0, 10.0, 7.0, 9.0]),
        np.array([11.0, 11.0, 8.0, 9.0]),
        np.array([9.0, 9.5, 6.0, 9.0]),
        np.array([10.0, 10.0, 7.5, 9.0]),
        DAY_MS,
    )
    seg = np.zeros(4, dtype=np.int64)
    tr = simulate(d, seg, DAY_MS, 10.0, 0, stop=8.0)  # bar 1 low 9.5 > 8; bar 2 opens at 7 <= 8
    assert tr is not None and tr.kind == "stop_gap" and tr.exit == 7.0
    tr = simulate(d, seg, DAY_MS, 10.0, 0, stop=9.6)  # bar 1 low 9.5 <= 9.6
    assert tr is not None and tr.kind == "stop" and tr.exit == 9.6
    tr = simulate(d, seg, 0, 10.0, 0, until_ms=2 * DAY_MS)  # time exit at the close of bar 1
    assert tr is not None and tr.kind == "time" and tr.exit == 10.0 and tr.hold_days == pytest.approx(2)


def test_net_return_formula():
    g, c = net_return(100.0, 110.0)
    assert g == pytest.approx(0.10)
    assert c == pytest.approx(0.0007 * 2.1)


def test_no_path_returns_none():
    d = Bars(np.array([0]) * DAY_MS, np.ones(1), np.ones(1), np.ones(1), np.ones(1), DAY_MS)
    assert simulate(d, np.zeros(1, dtype=np.int64), DAY_MS, 1.0, 0) is None
