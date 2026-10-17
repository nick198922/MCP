import numpy as np
import pytest

from tradedesk.analytics.course_pob import pob_events, pob_stop
from tradedesk.analytics.course_s14_ema import DAY_MS, Bars
from tradedesk.analytics.course_s14_ema import atr as atr_series
from tradedesk.analytics.course_s18_trend import pos_from_seg, segments

T0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC


def bars(o, h, lo, c, t=None, bar_ms=DAY_MS) -> Bars:
    n = len(c)
    t = T0 + np.arange(n, dtype=np.int64) * bar_ms if t is None else np.asarray(t, dtype=np.int64)
    return Bars(t, *(np.asarray(x, float) for x in (o, h, lo, c)), bar_ms)


def walk(n: int, seed: int) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.04, n)))
    o = np.concatenate([[100.0], c[:-1]]) * np.exp(rng.normal(0, 0.01, n))
    h = np.maximum(o, c) * np.exp(rng.exponential(0.015, n))
    lo = np.minimum(o, c) * np.exp(-rng.exponential(0.015, n))
    return bars(o, h, lo, c)


def scripted():
    # bars 0-1 flat, bar 2 the POB (close 100, high 101, low 98), bars 3-4 drop to a low of 85 (-15 %),
    # bar 5 quiet, bar 6 closes above 101 with a body of 7 (>= ATR 1): impulsive break, bar 7 holds above
    o = [99, 99, 98.5, 99, 92, 88, 94, 101.5]
    c = [99, 99, 100, 92, 88, 94, 101.5, 102]
    h = [99.5, 99.5, 101, 99.5, 93, 95, 102, 103]
    lo = [98.5, 98.5, 98, 91, 85, 87, 93.5, 100.8]
    return bars(o, h, lo, c)


def test_scripted_pob_impulse_and_break():
    b = scripted()
    pos = pos_from_seg(segments(b.t))
    ev = pob_events(b, pos, np.full(len(b), 1.0), drop=0.10, look=5, atr_mult=3.0, warmup=0)
    assert len(ev) == 1
    e = ev[0]
    assert (e.p, e.q, e.k) == (2, 4, 6)  # 100 - 85 = 15 >= 10 % and >= 3 ATR first on bar 4
    assert e.drop == pytest.approx(0.15)
    assert e.impulsive and e.confirm
    assert e.levels == {"wick": 101.0, "body": 100.0, "mid": 99.5}
    assert e.low == 98.0


def test_thresholds_atr_warmup_and_non_impulsive():
    b = scripted()
    pos = pos_from_seg(segments(b.t))
    one = np.full(len(b), 1.0)
    assert pob_events(b, pos, one, drop=0.16, warmup=0) == []  # the drop is 15 %
    assert pob_events(b, pos, np.full(len(b), 6.0), warmup=0) == []  # 15 < 3 * 6
    assert pob_events(b, pos, one, warmup=3) == []  # the POB is bar 2 of its segment
    e = pob_events(b, pos, np.full(len(b), 8.0), atr_mult=1.0, warmup=0)[0]
    assert e.k == 6 and e.impulsive  # body 7.5 < ATR 8, but it opened at 94 <= low 98: crossed the whole POB
    o = b.o.copy()
    o[6] = 99.0
    e = pob_events(bars(o, b.h, b.low, b.c), pos, np.full(len(b), 8.0), atr_mult=1.0, warmup=0)[0]
    assert e.k == 6 and not e.impulsive  # body 2.5 < ATR 8 and open 99 > low 98
    assert pob_events(bars(o, b.h, b.low, b.c), pos, np.full(len(b), 2.0), warmup=0)[0].impulsive  # 2.5 >= 2
    e = pob_events(b, pos, one, break_bars=1, warmup=0)[0]
    assert e.k == -1 and not e.impulsive


def test_each_pob_counts_once_and_segments_cut_the_break():
    b = scripted()
    pos = pos_from_seg(segments(b.t))
    ev = pob_events(b, pos, np.full(len(b), 1.0), look=3, warmup=0)
    assert [x.p for x in ev] == [2]  # bar 4 drops 15 % below bar 2's close, seen once
    t = b.t.copy()
    t[6:] += 10 * DAY_MS  # a hole before the break bar starts a new segment
    b2 = bars(b.o, b.h, b.low, b.c, t)
    pos2 = pos_from_seg(segments(b2.t))
    ev2 = pob_events(b2, pos2, np.full(len(b), 1.0), warmup=0)
    assert ev2[0].p == 2 and ev2[0].k == -1


def test_short_is_the_mirror():
    b = scripted()
    m = bars(-b.o, -b.low, -b.h, -b.c)  # a pump of 15 % after a POB, then a close below it
    pos = pos_from_seg(segments(b.t))
    e = pob_events(m.mirrored(), pos, np.full(len(b), 1.0), warmup=0)[0]
    assert (e.p, e.q, e.k) == (2, 4, 6) and e.drop == pytest.approx(0.15)


def test_drop_mirrors_into_a_rise_of_the_same_size():
    # flat at 100 (ties: the latest bar is the POB), then a high of 112 (+12 %) on bar 4; bar 3's 105 is only +5 %
    o = [100, 100, 100, 100, 101]
    c = [100, 100, 100, 100, 104]
    h = [100.5, 100.5, 100.5, 105, 112]
    lo = [99.5, 99.5, 99.5, 99.5, 100.5]
    b = bars(o, h, lo, c)
    pos = pos_from_seg(segments(b.t))
    ev = pob_events(b.mirrored(), pos, np.full(5, 1.0), warmup=0)
    assert [(e.p, e.q) for e in ev] == [(3, 4)]
    assert ev[0].drop == pytest.approx(0.12)
    assert ev[0].high == -99.5  # mirrored frame: the POB's top is minus its real low


def test_pob_stop():
    assert pob_stop(101.0, 98.0, 1.0) == pytest.approx(97.9)  # behind the POB
    assert pob_stop(101.0, 100.9, 1.0) == pytest.approx(100.4)  # thin POB: at least 0.6 ATR below the level
    assert pob_stop(101.0, 98.0, 2.0, "atr") == pytest.approx(99.0)
    with pytest.raises(ValueError):
        pob_stop(101.0, 98.0, 1.0, "x")


@pytest.mark.parametrize("seed", [41, 42, 43])
def test_events_known_by_t_do_not_change_when_later_bars_change(seed):
    b = walk(900, seed)
    pos = pos_from_seg(segments(b.t))
    t = 500
    rng = np.random.default_rng(seed + 7)
    c2 = b.c.copy()
    c2[t + 1 :] *= np.exp(rng.normal(0, 0.1, c2.size - t - 1))
    o2 = b.o.copy()
    o2[t + 1 :] *= np.exp(rng.normal(0, 0.1, c2.size - t - 1))
    h2, l2 = b.h.copy(), b.low.copy()
    h2[t + 1 :] = np.maximum(o2[t + 1 :], c2[t + 1 :]) * 1.01
    l2[t + 1 :] = np.minimum(o2[t + 1 :], c2[t + 1 :]) * 0.99
    b2 = bars(o2, h2, l2, c2)
    res = []
    for bb in (b, b2):
        a = atr_series(bb, 14, pos)
        for side in (bb, bb.mirrored()):
            ev = pob_events(side, pos, a, drop=0.06, atr_mult=1.5, break_bars=60, warmup=20)
            known = {(e.p, e.q, e.high, e.low, e.drop) for e in ev if e.q <= t}
            broken = {(e.p, e.k, e.impulsive) for e in ev if 0 < e.k <= t}
            unbroken_by_t = {e.p for e in ev if e.q <= t and not 0 < e.k <= t}
            conf = {(e.p, e.confirm) for e in ev if 0 < e.k and e.k + 1 <= t}
            res.append((known, broken, unbroken_by_t, conf))
    assert res[0] == res[2] and res[1] == res[3]
    assert len(res[0][0]) > 10 and len(res[0][1]) > 3
