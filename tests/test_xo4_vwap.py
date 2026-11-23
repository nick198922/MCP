"""XO-4 developing weekly VWAP pivot: analytic levels, signals, exits, funding and causality."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics import xo4_vwap as x4
from tradedesk.analytics.stop_management import Fees
from tradedesk.analytics.xo1_va_rotation import barrier_outcomes

H = x4.HOUR_MS
FEES = Fees(maker=0.00075, taker_exit=0.00075)


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


def test_period_start_week_and_month() -> None:
    t = np.array([ms("2026-11-25T13:00"), ms("2026-11-23T00:00"), ms("2026-11-22T23:00")])
    assert x4.period_start(t, "week").tolist() == [ms("2026-11-23T00:00"), ms("2026-11-23T00:00"), ms("2026-11-16T00:00")]
    assert x4.period_start(t, "month").tolist() == [ms("2026-11-01T00:00"), ms("2026-11-01T00:00"), ms("2026-11-01T00:00")]
    with pytest.raises(ValueError):
        x4.period_start(t, "year")


def test_developing_vwap_analytic_and_reset() -> None:
    # Sunday 23:00, Monday 00:00, Monday 01:00: the Monday bar restarts the weekly VWAP
    t = np.array([ms("2026-11-22T23:00"), ms("2026-11-23T00:00"), ms("2026-11-23T01:00")])
    tp = np.array([50.0, 100.0, 200.0])
    qv = np.array([7.0, 100.0, 400.0])  # base volumes 0.14, 1, 2
    lv = x4.developing_vwap(t, tp, tp, tp, qv, "week")
    assert lv.level[0] == pytest.approx(50.0)
    assert lv.level[1] == pytest.approx(100.0)
    assert lv.level[2] == pytest.approx(500.0 / 3.0)  # (100*1 + 200*2) / 3
    assert lv.elapsed_h.tolist() == [24 * 7, 1, 2]
    assert lv.cover_ok.tolist() == [False, True, True]  # the Sunday bar alone covers 1 of 168 hours


def test_period_open_variant() -> None:
    t = np.array([ms("2026-11-23T00:00"), ms("2026-11-23T01:00"), ms("2026-11-30T01:00")])
    lv = x4.period_open(t, np.array([10.0, 11.0, 12.0]), "week")
    assert lv.level[:2].tolist() == [10.0, 10.0]
    assert np.isnan(lv.level[2])  # the week's first hour is missing


def week_bars(path: list[float], start: str = "2026-11-23T00:00") -> tuple[np.ndarray, ...]:
    """Flat 1h bars (o = h = low = c) with equal quote volume along ``path``."""
    c = np.array(path, dtype=float)
    t = ms(start) + np.arange(c.size, dtype=np.int64) * H
    o = np.concatenate([[c[0]], c[:-1]])
    h, low = np.maximum(o, c), np.minimum(o, c)
    return t, o, h, low, c, np.full(c.size, 1000.0)


def test_short_signal_on_loss_of_vwap_4h_close() -> None:
    # 28 h at 100 (VWAP 100), then 4 h at 101 (closes above), then a 4h block ending at 98 (close below)
    path = [100.0] * 28 + [101.0] * 4 + [100.5, 99.5, 99.0, 98.0]
    t, o, h, low, c, qv = week_bars(path)
    lv = x4.developing_vwap(t, h, low, c, qv)
    atr = np.zeros(c.size)
    sig = x4.find_signals(t, h, low, c, atr, lv, tf_hours=4, buf_atr=0.0)
    assert len(sig) == 1
    s = sig[0]
    assert (s.i, s.side, s.entry, s.stop) == (35, -1, 98.0, 101.0)
    assert s.t_in == int(t[35]) + H
    assert s.level == pytest.approx(lv.level[35])
    assert c[35] < s.level < c[31]


def test_no_signal_before_warmup_or_across_weeks() -> None:
    # the same cross inside the first 24 h of the week gives nothing
    path = [100.0] * 4 + [101.0] * 4 + [100.0, 99.0, 98.0, 97.0]
    t, o, h, low, c, qv = week_bars(path)
    lv = x4.developing_vwap(t, h, low, c, qv)
    assert x4.find_signals(t, h, low, c, np.zeros(c.size), lv, buf_atr=0.0) == []


def test_long_signal_mirror_and_stop_limits() -> None:
    path = [100.0] * 28 + [99.0] * 4 + [99.5, 100.5, 101.0, 102.0]
    t, o, h, low, c, qv = week_bars(path)
    lv = x4.developing_vwap(t, h, low, c, qv)
    sig = x4.find_signals(t, h, low, c, np.zeros(c.size), lv, buf_atr=0.0)
    assert [(s.side, s.entry, s.stop) for s in sig] == [(1, 102.0, 99.0)]
    # a stop farther than max_stop -> no trade
    assert x4.find_signals(t, h, low, c, np.full(c.size, 20.0), lv, buf_atr=0.5) == []


def test_outcomes_invalidation_stop_gap_time() -> None:
    t = ms("2026-11-24T00:00") + np.arange(12, dtype=np.int64) * H  # bar 3 closes a 4h block (04:00)
    c = np.array([98.0, 98.5, 99.0, 100.5, 99.0, 98.0, 97.0, 96.0, 95.0, 94.0, 93.0, 92.0])
    o = np.concatenate([[98.0], c[:-1]])
    h, low = np.maximum(o, c), np.minimum(o, c)
    one = np.array([0])
    # short, entry 98, stop 102, level 100: the 04:00 close 100.5 is back above -> exit at 100.5
    r, ex, kind = x4.vwap_exit_outcomes(
        t, o, h, low, c, one, np.array([-1]), np.array([98.0]), np.array([102.0]), np.array([100.0]), 12, FEES
    )
    ru = 4.0 + FEES.risk_taker * 200.0
    assert kind.tolist() == [x4.KIND_INVALID] and ex.tolist() == [3]
    assert r[0] == pytest.approx((98.0 - 100.5) / ru - (0.00075 * 98.0 + 0.00075 * 100.5) / ru)
    # stop 100.2 is touched inside bar 3 before its close -> the stop wins
    r, ex, kind = x4.vwap_exit_outcomes(
        t, o, h, low, c, one, np.array([-1]), np.array([98.0]), np.array([100.2]), np.array([100.0]), 12, FEES
    )
    assert kind.tolist() == [x4.KIND_STOP] and ex.tolist() == [3]
    # a level far above: no invalidation, held to the last close
    r, ex, kind = x4.vwap_exit_outcomes(
        t, o, h, low, c, one, np.array([-1]), np.array([98.0]), np.array([110.0]), np.array([105.0]), 12, FEES
    )
    assert kind.tolist() == [x4.KIND_TIME] and ex.tolist() == [11]
    # gap: a long whose stop sits above the next open exits at that open
    o2 = o.copy()
    o2[1] = 90.0
    low2 = np.minimum(low, 90.0)
    r, ex, kind = x4.vwap_exit_outcomes(
        t, o2, h, low2, c, np.array([1]), np.array([1]), np.array([98.0]), np.array([95.0]), np.array([97.0]), 6, FEES
    )
    assert kind.tolist() == [x4.KIND_STOP]
    ru2 = 3.0 + FEES.risk_taker * 193.0
    assert r[0] == pytest.approx((90.0 - 98.0) / ru2 - (0.00075 * 98.0 + 0.00075 * 90.0) / ru2)


def test_outcomes_match_barrier_without_invalidation() -> None:
    rng = np.random.default_rng(3)
    n = 400
    t = ms("2026-01-05T00:00") + np.arange(n, dtype=np.int64) * H
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * 1.003
    low = np.minimum(o, c) * 0.997
    i0 = np.array([5, 50, 120, 200, 300])
    side = np.array([1, -1, 1, -1, 1])
    e = c[i0 - 1]
    st = e * (1 - side * 0.03)
    far = e * (1 - side * 10.0)  # level unreachable -> no invalidation
    r, ex, kind = x4.vwap_exit_outcomes(t, o, h, low, c, i0, side, e, st, far, 48, FEES)
    rb, exb, kb = barrier_outcomes(o, h, low, c, i0, side, e, st, e * (1 + side * 100.0), 48, FEES)
    assert np.allclose(r, rb) and ex.tolist() == exb.tolist() and kind.tolist() == kb.tolist()


def test_funding_r_analytic() -> None:
    f_t = np.array([0, 8, 16, 24], dtype=np.int64) * H
    f_r = np.array([0.001, 0.002, -0.001, 0.003])
    t_in = np.array([0, 0, 9 * H], dtype=np.int64)
    t_ex = np.array([16 * H, 7 * H, 24 * H], dtype=np.int64)
    out = x4.funding_r(t_in, t_ex, np.array([1, -1, -1]), np.array([100.0, 100.0, 50.0]), np.array([2.0, 2.0, 1.0]), f_t, f_r)
    # long pays 0.002 - 0.001 on 100 per 2 risk; short with no settlement in (0, 7h]; short receives -0.001 + 0.003 on 50
    assert out == pytest.approx([-0.05, 0.0, 0.1])
    assert (
        x4.funding_r(t_in, t_ex, np.array([1, 1, 1]), np.ones(3), np.ones(3), np.zeros(0, dtype=np.int64), np.zeros(0)).tolist()
        == [0.0] * 3
    )


def test_funding_covered_per_trade() -> None:
    f_t = np.array([100, 108, 116, 124], dtype=np.int64) * H  # history from 100h to 124h
    t_in = np.array([99, 100, 110, 110, 110], dtype=np.int64) * H
    t_ex = np.array([110, 110, 132, 133, 125], dtype=np.int64) * H
    # entry before the first settlement; covered; last 124 >= 132 - 8; 124 < 133 - 8; covered
    assert x4.funding_covered(t_in, t_ex, f_t).tolist() == [False, True, True, False, True]
    assert x4.funding_covered(t_in, t_ex, np.zeros(0, dtype=np.int64)).tolist() == [False] * 5


def test_overlaps_opposite() -> None:
    t_in = np.array([0, 10, 5, 20, 30, 50], dtype=np.int64) * H
    t_ex = np.array([8, 15, 12, 30, 40, 60], dtype=np.int64) * H
    side = np.array([1, 1, -1, -1, 1, 1])
    # long [0,8) meets short [5,12); long [10,15) meets it too; short [20,30) touches long [30,40) only at the edge
    assert x4.overlaps_opposite(t_in, t_ex, side).tolist() == [True, True, True, False, False, False]
    assert x4.overlaps_opposite(t_in, t_ex, np.ones(6)).tolist() == [False] * 6
    # brute force on random intervals
    rng = np.random.default_rng(3)
    a = rng.integers(0, 500, 200)
    b = a + rng.integers(1, 60, 200)
    s = rng.choice([-1, 1], 200)
    brute = [bool(np.any((s == -s[i]) & (a < b[i]) & (b > a[i]))) for i in range(200)]
    assert x4.overlaps_opposite(a, b, s).tolist() == brute


def test_paired_placebo_diff() -> None:
    pl = np.array([[0.5, 1.5, np.nan], [np.nan, np.nan, np.nan], [-1.0, -1.0, 2.0]])
    d = x4.paired_placebo_diff(np.array([2.0, 1.0, 0.0]), pl)
    assert d[0] == pytest.approx(1.0) and np.isnan(d[1]) and d[2] == pytest.approx(0.0)
    with pytest.raises(ValueError):
        x4.paired_placebo_diff(np.zeros(2), pl)


def test_boundary_starts() -> None:
    t = ms("2026-11-23T00:00") + np.arange(30, dtype=np.int64) * H
    t = np.delete(t, 13)
    s = x4.boundary_starts(t, 4)
    assert all(int(t[i]) % (4 * H) == 0 for i in s)
    assert all(t[i + 3] - t[i] == 3 * H for i in s)
    assert 12 not in s.tolist()  # 12:00 start runs into the missing 13:00 bar


def test_causality_levels_and_signals_before_t() -> None:
    rng = np.random.default_rng(7)
    n = 24 * 21
    t = ms("2026-06-01T00:00") + np.arange(n, dtype=np.int64) * H
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.006, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h, low = np.maximum(o, c) * 1.002, np.minimum(o, c) * 0.998
    qv = rng.uniform(1e5, 1e6, n)
    cut = 24 * 10 + 7

    def run(c_: np.ndarray, h_: np.ndarray, l_: np.ndarray, q_: np.ndarray) -> tuple[np.ndarray, list[x4.Signal]]:
        lv = x4.developing_vwap(t[: c_.size], h_, l_, c_, q_)
        return lv.level, x4.find_signals(t[: c_.size], h_, l_, c_, x4.atr_sma(h_, l_, c_), lv)

    lv_a, sig_a = run(c, h, low, qv)
    c2, h2, l2, q2 = c.copy(), h.copy(), low.copy(), qv.copy()
    c2[cut + 1 :] *= 1.3
    h2[cut + 1 :] *= 1.3
    l2[cut + 1 :] *= 1.3
    q2[cut + 1 :] *= 5
    lv_b, sig_b = run(c2, h2, l2, q2)
    lv_c, sig_c = run(c[: cut + 1], h[: cut + 1], low[: cut + 1], qv[: cut + 1])
    assert np.allclose(lv_a[: cut + 1], lv_b[: cut + 1]) and np.allclose(lv_a[: cut + 1], lv_c)
    early = [s for s in sig_a if s.i <= cut]
    assert early, "the random walk should cross the VWAP before the cut"
    assert early == [s for s in sig_b if s.i <= cut] == sig_c
