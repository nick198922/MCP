"""Tests for the step-9 S1 analytics (spot alts near the daily SMA 100 / 200 support)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics.course_s1_spot import (
    DAY_MS,
    closes_below,
    event_minus_placebo,
    first_close_above,
    first_touch_events,
    forward_exit,
    h1_verdict,
    is_swing_low,
    net_return,
    one_at_a_time,
    pick_matched,
    reclaim_events,
    rolling_extreme,
    seg_end,
    seg_sma,
    seg_start,
    segments,
    sideways_after_drop,
    state_onsets,
    uptrend_state,
    xs_median_rel,
)


def _days(n: int, start: int = 0) -> np.ndarray:
    return (np.arange(n, dtype=np.int64) + start) * DAY_MS


# ----------------------------------------------------------------------------- segments and indicators


def test_segments_split_on_gaps_longer_than_two_days() -> None:
    t = np.array([0, 1, 2, 4, 5, 9, 10], dtype=np.int64) * DAY_MS  # gap of 2 days keeps, gap of 4 days splits
    seg = segments(t)
    assert seg.tolist() == [0, 0, 0, 0, 0, 1, 1]
    assert seg_start(seg).tolist() == [0, 0, 0, 0, 0, 5, 5]
    assert seg_end(seg).tolist() == [4, 4, 4, 4, 4, 6, 6]


def test_seg_sma_restarts_in_each_segment() -> None:
    x = np.arange(8, dtype=float)
    seg = np.array([0, 0, 0, 0, 0, 1, 1, 1])
    out = seg_sma(x, seg, 3)
    assert np.isnan(out[:2]).all()
    assert out[2:5].tolist() == pytest.approx([1.0, 2.0, 3.0])
    assert np.isnan(out[5:7]).all()
    assert out[7] == pytest.approx(6.0)


def test_rolling_extreme_respects_segments() -> None:
    x = np.array([1, 5, 2, 3, 9, 1, 4], dtype=float)
    seg = np.array([0, 0, 0, 0, 1, 1, 1])
    mx = rolling_extreme(x, seg, 3, how="max")
    mn = rolling_extreme(x, seg, 3, how="min")
    assert np.isnan(mx[:2]).all() and np.isnan(mx[4:6]).all()
    assert mx[2] == 5 and mx[3] == 5 and mx[6] == 9
    assert mn[3] == 2 and mn[6] == 1


# ----------------------------------------------------------------------------- H1


def _bull_then_drop() -> tuple[np.ndarray, np.ndarray]:
    c = np.concatenate([np.full(200, 100.0), 100.0 * 1.02 ** np.arange(1, 81), np.zeros(60)])
    for i in range(280, 340):
        c[i] = c[i - 1] * 0.97
    return c, c * 0.99


def _brute_first_touch(c: np.ndarray, lo: np.ndarray, ratio: float, lookback: int) -> list[int]:
    s1 = np.array([c[i - 99 : i + 1].mean() if i >= 99 else np.nan for i in range(c.size)])
    s2 = np.array([c[i - 199 : i + 1].mean() if i >= 199 else np.nan for i in range(c.size)])
    out = []
    for t in range(1, c.size):
        if not (np.isfinite(s2[t]) and lo[t] <= s1[t] and s1[t] > s2[t] and np.isfinite(s2[t - 1]) and c[t - 1] > s2[t - 1]):
            continue
        strong = [j for j in range(max(0, t - lookback), t) if np.isfinite(s1[j]) and c[j] / s1[j] >= ratio]
        if not strong:
            continue
        k = strong[-1]
        if all(lo[j] > s1[j] for j in range(k + 1, t)):
            out.append(t)
    return out


def test_first_touch_matches_the_definition_on_a_bull_run_and_drop() -> None:
    c, lo = _bull_then_drop()
    seg = np.zeros(c.size, dtype=np.int64)
    s1, s2 = seg_sma(c, seg, 100), seg_sma(c, seg, 200)
    ev = first_touch_events(c, lo, s1, s1, s2, seg, ratio=1.6, lookback=120)
    expected = _brute_first_touch(c, lo, 1.6, 120)
    assert ev.tolist() == expected
    assert len(expected) == 1
    t = expected[0]
    assert lo[t] <= s1[t] and lo[t - 1] > s1[t - 1]  # the first bar whose low reaches the SMA 100
    # a higher strength threshold than the run reached gives no event
    assert first_touch_events(c, lo, s1, s1, s2, seg, ratio=10.0).size == 0
    # the "down" branch needs sma100 <= sma200, which fails here
    assert first_touch_events(c, lo, s1, s1, s2, seg, ratio=1.6, trend="down").size == 0


def test_first_touch_is_not_repeated_without_a_new_strong_close() -> None:
    c, lo = _bull_then_drop()
    lo2 = lo.copy()
    seg = np.zeros(c.size, dtype=np.int64)
    s1, s2 = seg_sma(c, seg, 100), seg_sma(c, seg, 200)
    t = int(first_touch_events(c, lo, s1, s1, s2, seg)[0])
    # an earlier touch after the strong run (bar t - 3 dips to the SMA) moves the event there, and t is no event
    lo2[t - 3] = s1[t - 3] * 0.999
    ev = first_touch_events(c, lo2, s1, s1, s2, seg)
    assert t not in ev.tolist()
    assert ev.tolist() == [t - 3]


def test_first_touch_without_strength_filter_needs_quiet_bars() -> None:
    n = 400
    c = np.full(n, 100.0) * 1.003 ** np.arange(n)
    lo = c * 0.995
    seg = np.zeros(n, dtype=np.int64)
    s1, s2 = seg_sma(c, seg, 100), seg_sma(c, seg, 200)
    lo[300] = s1[300] * 0.99
    lo[310] = s1[310] * 0.99
    ev = first_touch_events(c, lo, s1, s1, s2, seg, ratio=None, quiet=30)
    assert ev.tolist() == [300]  # 310 had a touch 10 bars earlier


def test_first_touch_is_causal() -> None:
    rng = np.random.default_rng(1)
    n = 900
    c = 100 * np.exp(np.cumsum(rng.normal(0.002, 0.05, n)))
    lo = c * np.exp(-np.abs(rng.normal(0, 0.03, n)))
    seg = np.zeros(n, dtype=np.int64)

    def run(cc: np.ndarray, ll: np.ndarray) -> np.ndarray:
        s1, s2 = seg_sma(cc, seg, 100), seg_sma(cc, seg, 200)
        return first_touch_events(cc, ll, s1, s1, s2, seg, ratio=1.3)

    base = run(c, lo)
    assert base.size > 0
    for cut in (400, 600, 750):
        c2, lo2 = c.copy(), lo.copy()
        c2[cut + 1 :] *= 3.0
        lo2[cut + 1 :] = c2[cut + 1 :] * 0.5
        alt = run(c2, lo2)
        assert base[base <= cut].tolist() == alt[alt <= cut].tolist()


def test_uptrend_state_and_helpers() -> None:
    c = np.array([10.0, 12.0, 11.0, 9.0, 13.0])
    s1 = np.array([np.nan, 11.0, 11.0, 11.0, 11.0])
    s2 = np.array([np.nan, 10.0, 10.0, 12.0, 10.0])
    seg = np.zeros(5, dtype=np.int64)
    up = uptrend_state(c, s1, s2, seg)
    assert up.tolist() == [False, False, True, False, False]  # bar 4: previous close 9 < sma200 12
    assert uptrend_state(c, s1, s2, seg, trend="down").tolist() == [False, False, False, True, False]
    ma = np.full(5, 11.5)
    assert first_close_above(c, ma, seg, 0, within=10) == 1
    assert first_close_above(c, ma, seg, 1, within=2) == -1
    assert closes_below(c, ma, seg, 1, within=1) is True
    assert closes_below(c, ma, seg, 4, within=3) is None


# ----------------------------------------------------------------------------- holding and returns


def test_forward_exit_and_data_end() -> None:
    t = np.concatenate([_days(10), _days(5, start=20)])  # gap 9 -> 20 splits
    seg = segments(t)
    j, de = forward_exit(t, seg, 3)
    assert j[0] == 3 and not de[0]
    assert j[8] == 9 and not de[8]  # target day 11, the segment ends on day 9: within the 2-day tolerance
    j5, de5 = forward_exit(t, seg, 5)
    assert j5[8] == 9 and de5[8]  # target day 13: the segment ended before it
    assert j[9] == -1  # last bar of the segment: no later bar
    assert j[10] == 13 and not de[10]
    t2 = np.array([0, 1, 2, 4, 5], dtype=np.int64) * DAY_MS  # day 3 missing inside the segment
    j2, de2 = forward_exit(t2, segments(t2), 3)
    assert j2[0] == 2 and not de2[0]  # last bar with open <= day 3, one day short: still a time exit


def test_net_return_costs() -> None:
    assert net_return(100.0, 110.0) == pytest.approx(0.10 - 0.0007 * 2.1)
    assert net_return(100.0, 100.0, cost=0.001) == pytest.approx(-0.002)
    arr = net_return(np.array([100.0, 50.0]), np.array([50.0, 100.0]))
    assert arr.tolist() == pytest.approx([-0.5 - 0.0007 * 1.5, 1.0 - 0.0007 * 3.0])


def test_one_at_a_time() -> None:
    assert one_at_a_time([0, 5, 10, 12, 30], [10, 15, 20, 40, 50]).tolist() == [True, False, True, False, True]


# ----------------------------------------------------------------------------- H2


def _reclaim_series(n_below: int) -> tuple[np.ndarray, np.ndarray]:
    n = 60
    c = np.full(n, 108.0)
    lo = np.full(n, 106.0)
    for f in (10, 20, 30):
        lo[f] = 100.0
    lo[20] = 101.0  # within 3 % of 100
    for i in range(41, 41 + n_below):
        c[i] = 95.0
        lo[i] = 94.0 - 0.01 * i  # falling lows: no swing low inside the losing run
    r = 41 + n_below
    c[r] = 101.0
    lo[r] = 99.0
    lo[r + 1 :] = 100.0 + np.arange(n - r - 1)
    return c, lo


def test_reclaim_after_five_closes_below_a_triple_support() -> None:
    c, lo = _reclaim_series(6)
    seg = np.zeros(c.size, dtype=np.int64)
    assert is_swing_low(lo, seg, 30) and not is_swing_low(lo, seg, 31)
    ev = reclaim_events(c, lo, seg)
    assert ev == [(47, 100.0, 30)]
    # four closes below is not a loss, so no reclaim
    c4, lo4 = _reclaim_series(4)
    assert reclaim_events(c4, lo4, seg) == []
    # with only two touches required the level from bar 20 also qualifies, and the reported level is the highest
    ev2 = reclaim_events(c, lo, seg, touches=2)
    assert [e[0] for e in ev2] == [47] and ev2[0][1] == 101.0


def test_reclaim_is_causal() -> None:
    c, lo = _reclaim_series(6)
    seg = np.zeros(c.size, dtype=np.int64)
    base = reclaim_events(c, lo, seg)
    c2, lo2 = c.copy(), lo.copy()
    c2[48:] = 50.0
    lo2[48:] = 40.0
    alt = reclaim_events(c2, lo2, seg)
    assert [e for e in alt if e[0] <= 47] == base


# ----------------------------------------------------------------------------- H3


def test_sideways_after_drop_and_onsets() -> None:
    c = np.concatenate([np.full(200, 100.0), np.full(60, 80.0)])
    seg = np.zeros(c.size, dtype=np.int64)
    st = sideways_after_drop(c, seg)
    assert not st[:229].any()
    assert st[229:].all()
    assert state_onsets(st).tolist() == [229]
    # causal: the state at bar 229 does not change when later closes change
    c2 = c.copy()
    c2[230:] = 200.0
    assert sideways_after_drop(c2, seg)[229]


def test_xs_median_rel() -> None:
    assert xs_median_rel([1.1, 0.9, 1.0, 1.2, 0.8], 1.0) == pytest.approx(0.0)
    assert xs_median_rel([1.1, 0.9, 1.0, 1.2, 0.8], 1.25) == pytest.approx(0.8 - 1.0)
    assert math.isnan(xs_median_rel([1.0, np.nan, 1.0], 1.0))


# ----------------------------------------------------------------------------- placebo and verdict


def test_pick_matched_window_exclusion_and_seed() -> None:
    days = np.repeat(np.arange(100), 3)
    ok = np.ones(days.size, dtype=bool)
    ok[::2] = False
    a = pick_matched(np.random.default_rng(7), days, ok, 50, window=5, k=4, exclude=151)
    b = pick_matched(np.random.default_rng(7), days, ok, 50, window=5, k=4, exclude=151)
    assert a.tolist() == b.tolist() and a.size == 4
    assert np.all(np.abs(days[a] - 50) <= 5) and ok[a].all() and 151 not in a.tolist()
    assert pick_matched(np.random.default_rng(7), days, np.zeros(days.size, dtype=bool), 50).size == 0


def test_event_minus_placebo() -> None:
    x = event_minus_placebo([0.1, 0.2, 0.3], [0.0, 0.1, 0.5], [0, 0, 2])
    assert x[0] == pytest.approx(0.05) and math.isnan(x[1]) and x[2] == pytest.approx(-0.2)


def test_h1_verdict_rules() -> None:
    good = {
        "n_dates": 150,
        "mde": 0.08,
        "diff": 0.08,
        "diff_ci_day": (0.02, 0.14),
        "diff_ci_month": (0.01, 0.15),
        "mean_ci_day": (0.01, 0.2),
        "mean_ci_month": (0.005, 0.2),
        "p_holm": 0.01,
        "oos_dates": 30,
        "oos_mean": 0.03,
        "oos_diff": 0.02,
    }
    assert h1_verdict(**good) == "confirmed"
    assert h1_verdict(**{**good, "n_dates": 80}) == "inconclusive"
    assert h1_verdict(**{**good, "mde": 0.12}) == "inconclusive"
    assert h1_verdict(**{**good, "diff": 0.04}) == "rejected"
    assert h1_verdict(**{**good, "diff_ci_day": (-0.01, 0.17)}) == "rejected"
    assert h1_verdict(**{**good, "p_holm": 0.2}) == "inconclusive"
    assert h1_verdict(**{**good, "oos_diff": -0.01}) == "inconclusive"
    assert h1_verdict(**{**good, "mean_ci_month": (-0.01, 0.2)}) == "inconclusive"
