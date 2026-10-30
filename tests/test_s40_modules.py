"""S40 modules (item 8 / 7b): the failed-short detector against ``s40_all``, beta / residual analytics, no-lookahead."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics import s40_modules as m
from tradedesk.analytics import setup_signals as ss
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, atr, segment_pos
from tradedesk.analytics.stop_management import Fees, risk_unit

H = HOUR_MS
B4 = 4 * H
T0 = int(datetime(2025, 3, 3, tzinfo=UTC).timestamp() * 1000)  # a Monday 00:00 UTC
F = Fees()


def random_bars(n: int, step: int, seed: int = 1, vol: float = 0.02) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, vol / 2, n))
    lo = np.minimum(o, c) * (1 - rng.uniform(0, vol / 2, n))
    return Bars.from_rows([[T0 + i * step, o[i], h[i], lo[i], c[i]] for i in range(n)], step)


def head(b: Bars, k: int) -> Bars:
    return Bars(b.t[:k], b.o[:k], b.h[:k], b.low[:k], b.c[:k], b.bar_ms)


def s3_short_parents(b: Bars) -> list[m.Parent]:
    return [
        m.Parent(int(np.searchsorted(b.t, s.signal_ts)), s.stop, s.target)
        for s in ss.range_all(b, "4h")
        if s.setup == "S3" and s.side == "short"
    ]


def fails(b: Bars, wait: int = 6, used: set[int] | None = None) -> list[m.Fail]:
    pos = segment_pos(b.t)
    return m.failed_short(b, pos, atr(b, 14, pos), s3_short_parents(b), max_wait=wait, used=used)


# ---------------------------------------------------------------- detector


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_failed_short_with_s3_parents_is_s40_all(seed: int) -> None:
    b = random_bars(3000, B4, seed)
    want = [(s.signal_ts, s.entry, s.stop, s.target) for s in ss.s40_all(b, "4h")]
    got = [(int(b.t[f.j]), f.entry, f.stop, f.target) for f in fails(b)]
    assert len(want) > 0
    assert len(got) == len(want)
    for g, w in zip(got, want, strict=True):
        assert g[0] == w[0]
        assert g[1:] == pytest.approx(w[1:], rel=1e-12)
    for f in fails(b):
        assert f.stop < f.entry < f.target
        assert f.target - f.entry == pytest.approx(2.0 * (f.entry - f.stop))


def test_failed_short_is_causal() -> None:
    b = random_bars(3000, B4, 4)
    full = fails(b, wait=12)
    assert full
    for f in full[:: max(1, len(full) // 8)]:
        cut = fails(head(b, f.j + 1), wait=12)
        assert any(c.j == f.j and c.entry == f.entry and c.stop == f.stop for c in cut)


def test_failed_short_analytic() -> None:
    # parent short at bar 1 (stop 105, target 90); bar 2 low 96; bar 3 closes 106 > 105: long at 106,
    # stop = min(low 1..3) - 0.5 ATR, target 2R
    rows = [[T0, 100, 101, 99, 100], [T0 + B4, 100, 104, 98, 101], [T0 + 2 * B4, 101, 102, 96, 97], [T0 + 3 * B4, 97, 107, 97, 106]]
    b = Bars.from_rows(rows, B4)
    pos = segment_pos(b.t)
    a = np.full(4, 2.0)
    out = m.failed_short(b, pos, a, [m.Parent(1, 105.0, 90.0, "X")])
    assert len(out) == 1
    f = out[0]
    assert (f.r, f.j, f.tag) == (1, 3, "X")
    assert f.entry == 106.0
    assert f.stop == pytest.approx(96.0 - 1.0)
    assert f.target == pytest.approx(106.0 + 2 * 11.0)
    # the parent's target is touched first (low 89 at bar 2): no failure
    rows2 = [r[:] for r in rows]
    rows2[2][3] = 89
    assert m.failed_short(Bars.from_rows(rows2, B4), pos, a, [m.Parent(1, 105.0, 90.0)]) == []
    # bar 3 already used: none
    assert m.failed_short(b, pos, a, [m.Parent(1, 105.0, 90.0)], used={3}) == []


def test_wait_12_adds_only_late_failures() -> None:
    b = random_bars(4000, B4, 5)
    base = fails(b)
    used = {f.j for f in base}
    added = fails(b, wait=12, used=used)
    assert added
    assert all(7 <= f.j - f.r <= 12 for f in added)
    assert not {f.j for f in added} & {f.j for f in base}


# ---------------------------------------------------------------- H1: beta, residual, hedge


def test_rolling_beta_recovers_slope_and_ignores_future() -> None:
    rng = np.random.default_rng(7)
    n = 3000
    t = T0 + np.arange(n) * H
    rb = rng.normal(0, 0.01, n)
    rc = 1.7 * rb + rng.normal(0, 0.002, n)
    t_in = int(t[2000]) + H  # bars 0..2000 closed by t_in
    beta = m.rolling_beta(t, rc, rb, t_in)
    assert beta == pytest.approx(1.7, abs=0.02)
    # data after t_in (bars that close later) changes nothing
    rc2, rb2 = rc.copy(), rb.copy()
    rc2[2001:] = 99.0
    rb2[2001:] = -5.0
    assert m.rolling_beta(t, rc2, rb2, t_in) == beta
    # but the last closed bar does count
    rc3 = rc.copy()
    rc3[2000] += 0.5
    assert m.rolling_beta(t, rc3, rb, t_in) != beta
    # too few observations
    assert np.isnan(m.rolling_beta(t, rc, rb, int(t[500]) + H))


def test_hourly_returns_and_alignment() -> None:
    t = np.array([0, H, 2 * H, 4 * H], dtype=np.int64)
    c = np.array([100.0, 110.0, 99.0, 100.0])
    r = m.hourly_returns(t, c)
    assert np.isnan(r[0]) and np.isnan(r[3])
    assert r[1] == pytest.approx(0.1) and r[2] == pytest.approx(-0.1)
    v = m.aligned(t, r, np.array([H, 3 * H, 9 * H], dtype=np.int64))
    assert v[0] == pytest.approx(0.1) and np.isnan(v[1]) and np.isnan(v[2])


def test_window_return_and_funding() -> None:
    t = T0 + np.arange(10) * H
    c = 100.0 + np.arange(10)
    assert m.window_return(t, c, int(t[2]) + H, int(t[5]) + H) == pytest.approx(105 / 102 - 1)
    assert np.isnan(m.window_return(t, c, int(t[2]) + H, int(t[9]) + 5 * H))
    tf = np.array([0, 8, 16, 24], dtype=np.int64) * H
    rf = np.array([0.0001, 0.0002, -0.0001, 0.0003])
    assert m.funding_sum(tf, rf, 0, 16 * H) == pytest.approx(0.0001)  # (0, 16h]: 8h and 16h
    assert m.funding_sum(tf, rf, 1, 30 * H) == pytest.approx(0.0004)


def test_residual_and_hedge_cost_analytic() -> None:
    e, st = 100.0, 95.0
    ru = risk_unit(e, st, F)
    # a long that made +1R while BTC rose 3 % with beta 1.2: residual = 1 - 1.2 * 0.03 * 100 / ru
    assert m.residual_r(1.0, 1.2, 0.03, e, st, F) == pytest.approx(1.0 - 3.6 / ru)
    assert m.residual_r(0.4, 0.0, 0.5, e, st, F) == pytest.approx(0.4)
    # beta 1, funding +0.1 % received by the short: cost = (2 * 0.00075 - 0.001) * 100 / ru
    assert m.hedge_cost_r(1.0, e, st, 0.001, fees=F) == pytest.approx((0.0015 - 0.001) * 100 / ru)
    assert m.hedge_cost_r(1.0, e, st, 0.0, mult=1.5, fees=F) == pytest.approx(0.00225 * 100 / ru)
    # a negative beta hedges with a long: pays positive funding
    assert m.hedge_cost_r(-0.5, e, st, 0.001, fees=F) == pytest.approx((0.00075 + 0.0005) * 100 / ru)


def test_same_coin_times() -> None:
    t_in = T0 + 10 * B4
    a = m.same_coin_times("SOL|x|1", t_in, 3)
    assert a == m.same_coin_times("SOL|x|1", t_in, 3)
    assert len(set(a)) == 3
    for x in a:
        assert x % B4 == 0
        assert 3 * m.DAY_MS <= abs(x - t_in) <= 30 * m.DAY_MS
