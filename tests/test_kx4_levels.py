"""KX-4 levels module (IDEA-20261113-x-traders, item 5, registration 5.4)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics import kx4_levels as kx
from tradedesk.analytics.xo4_vwap import developing_vwap

H = kx.HOUR_MS


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


def hourly(start: str, end: str, seed: int = 1) -> tuple[np.ndarray, ...]:
    t = np.arange(ms(start), ms(end), H, dtype=np.int64)
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, t.size)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * 1.002
    low = np.minimum(o, c) * 0.998
    qv = rng.uniform(1e5, 2e5, t.size)
    return t, o, h, low, c, qv


def test_year_start():
    assert kx.year_start(ms("2026-07-15T13:00")) == ms("2026-01-01T00:00")
    assert kx.year_start(ms("2026-01-01T00:00")) == ms("2026-01-01T00:00")


def test_open_levels_and_dedup():
    t, o, *_ = hourly("2025-12-29T00:00", "2026-06-10T00:00")
    at = {int(x): float(v) for x, v in zip(t, o, strict=True)}
    lv = kx.open_levels(t, o, ms("2026-01-07T05:00"))
    assert lv["YO"] == at[ms("2026-01-01T00:00")]
    assert math.isnan(lv["MO"])  # January: the month open is the year open
    assert lv["WO"] == at[ms("2026-01-05T00:00")]
    lv = kx.open_levels(t, o, ms("2026-01-01T12:00"))  # the week started 2025-12-29, not on the 1st
    assert lv["WO"] == at[ms("2025-12-29T00:00")]
    lv = kx.open_levels(t, o, ms("2026-06-03T10:00"))  # 2026-06-01 is a Monday: the week open is the month open
    assert datetime(2026, 6, 1).weekday() == 0
    assert lv["MO"] == at[ms("2026-06-01T00:00")]
    assert math.isnan(lv["WO"])
    assert lv["YO"] == at[ms("2026-01-01T00:00")]


def test_open_level_missing_first_bar():
    t, o, *_ = hourly("2026-01-01T01:00", "2026-02-10T00:00")  # listed one hour after the year open
    lv = kx.open_levels(t, o, ms("2026-02-04T00:00"))
    assert math.isnan(lv["YO"])
    assert np.isfinite(lv["MO"])


def test_weekly_vwap_analytic_and_matches_xo4():
    t = np.arange(ms("2026-05-04T00:00"), ms("2026-05-06T00:00"), H, dtype=np.int64)  # Monday, 48 hours
    tp = np.where(np.arange(t.size) < 24, 100.0, 200.0)
    vol = np.where(np.arange(t.size) < 24, 1.0, 2.0)
    h, low, c = tp + 1, tp - 1, tp.copy()
    qv = tp * vol
    v = kx.weekly_vwap_at(t, h, low, c, qv, ms("2026-05-06T00:00"))
    assert v == pytest.approx((24 * 100 + 24 * 400) / (24 + 48))
    ref = developing_vwap(t, h, low, c, qv, "week").level[-1]
    assert v == pytest.approx(ref)
    assert math.isnan(kx.weekly_vwap_at(t, h, low, c, qv, ms("2026-05-04T23:00")))  # 23 h < warm-up
    assert kx.weekly_vwap_at(t, h, low, c, qv, ms("2026-05-05T00:00")) == pytest.approx(100.0)


def test_oriented_levels_short_and_long():
    t, o, h, low, c, qv = hourly("2026-04-27T00:00", "2026-05-13T00:00")
    ref = ms("2026-05-07T08:00")
    pw0, pw1 = ms("2026-04-27T00:00"), ms("2026-05-04T00:00")
    wk = (t >= pw0) & (t < pw1)
    lg = kx.oriented_levels(t, o, h, low, c, qv, ref, "long")
    sh = kx.oriented_levels(t, o, h, low, c, qv, ref, "short")
    assert lg["PW"] == pytest.approx(low[wk].min())
    assert sh["PW"] == pytest.approx(-h[wk].max())
    for k in ("WO", "VW"):
        assert sh[k] == pytest.approx(-lg[k])
    assert math.isnan(lg["YO"])  # no bar at 2026-01-01 in this sample
    assert tuple(lg) == kx.LEVELS


def test_swept_confluent_flags():
    lv = {"PW": 99.8, "YO": 100.0, "MO": math.nan, "WO": 100.3, "VW": 110.0}
    sw = kx.swept(95.0, 105.0, lv)
    assert sw == {"PW": True, "YO": True, "MO": False, "WO": True, "VW": False}
    assert kx.swept(95.0, 100.0, {"YO": 100.0})["YO"] is False  # strict: a close at the level is no reclaim
    assert kx.confluent(lv, "PW") and kx.confluent(lv, "YO")
    assert not kx.confluent(lv, "VW")
    assert kx.flags(95.0, 105.0, lv) == (True, True)
    assert kx.flags(99.9, 105.0, lv) == (True, True)  # YO swept, WO within 0.5 % of it
    assert kx.flags(100.1, 105.0, lv) == (False, True)  # only WO swept; PW 99.8 within 0.5 % of WO
    assert kx.flags(100.4, 105.0, lv) == (False, False)
    assert kx.flags(109.0, 111.0, lv) == (True, False)  # VW swept alone


def test_flag_matrix_equals_scalar():
    rng = np.random.default_rng(3)
    n = 400
    lv = 100 + rng.normal(0, 2, (n, len(kx.LEVELS)))
    lv[rng.random(lv.shape) < 0.2] = np.nan
    ext = 100 + rng.normal(-2, 1.5, n)
    close = ext + rng.uniform(0.5, 5, n)
    k1, k2 = kx.flag_matrix(ext, close, lv)
    for i in range(n):
        d = dict(zip(kx.LEVELS, lv[i], strict=True))
        assert (k1[i], k2[i]) == kx.flags(ext[i], close[i], d)
    assert 0 < k1.sum() < n and 0 < k2.sum() < n


def test_near_matrix_h2_rule():
    # columns PW, YO, MO, WO, VW; edge 100, ATR 2 -> «near» means |L - 100| <= 0.6
    lv = np.array(
        [
            [np.nan, 100.5, np.nan, np.nan, np.nan],  # YO near, alone
            [np.nan, np.nan, np.nan, np.nan, 100.7],  # VW 0.35 ATR away: not near
            [100.2, np.nan, np.nan, 100.6, np.nan],  # PW and WO near and within 0.5 % of each other
            [100.2, 101.0, np.nan, np.nan, np.nan],  # PW near, YO 0.8 % away: no confluence
        ]
    )
    edge, a = np.full(4, 100.0), np.array([2.0, 2.0, 2.0, np.nan])
    k1, k2 = kx.near_matrix(edge, a, lv)
    assert k1.tolist() == [True, False, False, False]
    assert k2.tolist() == [False, False, True, False]  # row 4: no ATR -> never near
    _, k2b = kx.near_matrix(edge, np.full(4, 2.0), lv)
    assert k2b.tolist() == [False, False, True, False]


def test_placebo_shift_and_u():
    keys = [f"S|4h|long|{i}" for i in range(50)]
    u = kx.placebo_u(keys, 20, seed=7)
    assert u.shape == (50, 20)
    assert np.all((np.abs(u) >= kx.U_LO) & (np.abs(u) <= kx.U_HI))
    assert np.array_equal(u, kx.placebo_u(keys, 20, seed=7))
    assert not np.array_equal(u, kx.placebo_u(keys, 20, seed=8))
    assert (u > 0).any() and (u < 0).any()
    # u = 0 reproduces the real flags; a common shift keeps a confluent pair confluent
    lv = np.array([[99.8, 100.0, np.nan, 100.3, 110.0]])
    ext, close = np.array([90.0]), np.array([120.0])
    k1, k2 = kx.placebo_flags(ext, close, lv, np.zeros((1, 1)))
    assert (k1[0, 0], k2[0, 0]) == kx.flags(90.0, 120.0, dict(zip(kx.LEVELS, lv[0], strict=True)))
    k1, k2 = kx.placebo_flags(ext, close, lv, np.array([[0.03, -0.03]]))
    assert k2.all()
    # an oriented short (negated levels) scales the same way
    k1s, _ = kx.placebo_flags(
        np.array([-101.0]), np.array([-99.0]), np.array([[np.nan, -100.0, np.nan, np.nan, np.nan]]), np.array([[0.0, 0.03]])
    )
    assert k1s[0].tolist() == [True, False]


def test_edge_distance_and_stats():
    lv = {"YO": 100.0, "VW": 103.0, "PW": math.nan}
    assert kx.edge_distance_atr(101.0, lv, ("YO", "VW", "PW"), 2.0) == pytest.approx(0.5)
    assert math.isnan(kx.edge_distance_atr(101.0, lv, ("PW",), 2.0))
    v = np.array([1.0, 2.0, 3.0, 6.0])
    f = np.array([False, False, True, True])
    assert kx.subgroup_delta(v, f) == pytest.approx(4.5 - 3.0)
    assert math.isnan(kx.subgroup_delta(v, np.zeros(4, dtype=bool)))
    assert kx.placebo_p(1.0, np.array([0.0, 2.0, 0.5, np.nan])) == pytest.approx(2 / 4)


def test_causal_levels_unchanged_by_later_bars():
    t, o, h, low, c, qv = hourly("2025-12-29T00:00", "2026-03-01T00:00", seed=5)
    ref = ms("2026-02-11T16:00")
    base = kx.oriented_levels(t, o, h, low, c, qv, ref, "long")
    later = t >= ref
    o2, h2, l2, c2, q2 = (x.copy() for x in (o, h, low, c, qv))
    o2[later] *= 3
    h2[later] *= 3
    l2[later] *= 0.2
    c2[later] *= 3
    q2[later] *= 50
    for side in ("long", "short"):
        a = kx.oriented_levels(t, o, h, low, c, qv, ref, side)
        b = kx.oriented_levels(t, o2, h2, l2, c2, q2, ref, side)
        k = ~later
        cut = kx.oriented_levels(t[k], o[k], h[k], low[k], c[k], qv[k], ref, side)
        for name in kx.LEVELS:
            assert a[name] == pytest.approx(b[name], nan_ok=True)
            assert a[name] == pytest.approx(cut[name], nan_ok=True)
    assert all(np.isfinite(v) for v in base.values())  # 2026-02-11: every level defined
