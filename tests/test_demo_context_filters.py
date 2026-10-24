from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.demo_context_filters import (
    bar_at_close,
    boot_p_two_sided,
    btc_block_flags,
    cap_per_group,
    cluster_boot_group_sums,
    cluster_sizes,
    daily_sums,
    last_closed,
    new_extreme_flags,
    repeat_flags,
    score_points,
    trend_dirs,
)
from tradedesk.analytics.setup_signals import LiveSignal, score, trend_dir

H4 = 4 * 3_600_000


def bars_from_close(c: np.ndarray, bar_ms: int = H4, t0: int = 1_600_000_000_000 // H4 * H4) -> Bars:
    c = np.asarray(c, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) + 0.5
    lo = np.minimum(o, c) - 0.5
    t = t0 + bar_ms * np.arange(c.size, dtype=np.int64)
    return Bars(t, o, h, lo, c, bar_ms)


def walk(n: int, seed: int = 1) -> Bars:
    rng = np.random.default_rng(seed)
    return bars_from_close(100 + np.cumsum(rng.normal(0, 1, n)))


def test_new_extreme_flags_analytic() -> None:
    low = np.array([5.0, 4.0, 3.0, 6.0, 2.0])
    high = np.array([1.0, 2.0, 2.0, 3.0, 2.5])
    nl, nh = new_extreme_flags(low, high, 2)
    assert nl.tolist() == [False, False, True, False, True]
    # high[2]=2 is not strictly above max(1, 2); high[3]=3 > 2; high[4]=2.5 < 3
    assert nh.tolist() == [False, False, False, True, False]


def test_new_extreme_flags_respect_segments() -> None:
    low = np.array([5.0, 4.0, 3.0, 2.0])
    pos = np.array([0, 1, 0, 1])  # a gap restarts the segment at bar 2
    nl, _ = new_extreme_flags(low, low, 1, pos)
    assert nl.tolist() == [False, True, False, True]
    nl2, _ = new_extreme_flags(low, low, 2, pos)
    assert not nl2.any()


def test_btc_block_flags_lag() -> None:
    b = bars_from_close(np.array([10, 11, 12, 13, 9, 14, 15, 16, 17, 18.0]))
    bl, bs = btc_block_flags(b, n=3, lag=1)
    # bar 4 (close 9) makes a new 3-bar low; with lag 1 bars 4 and 5 block longs
    assert np.flatnonzero(bl).tolist() == [4, 5]
    bl0, _ = btc_block_flags(b, n=3, lag=0)
    assert np.flatnonzero(bl0).tolist() == [4]
    # new highs: bar 3 (13), bar 5 (14 > 13), 6, 7, 8, 9; with lag 1 also the bar after each
    assert bs[3] and bs[4] and bs[5] and not bs[2]


def test_bar_lookup() -> None:
    b = walk(10)
    t_close = int(b.t[3]) + H4
    assert bar_at_close(b, t_close) == 3
    assert bar_at_close(b, t_close + 1) == -1
    assert last_closed(b, t_close) == 3
    assert last_closed(b, t_close - 1) == 2
    assert last_closed(b, int(b.t[0])) == -1


def test_trend_dirs_match_live_trend_dir() -> None:
    b = walk(140, seed=7)
    td = trend_dirs(b)
    for i in range(1, len(b)):
        assert td[i] == trend_dir(b.slice(0, i + 1)), i
    assert set(td.tolist()) <= {-1, 0, 1}
    assert (td != 0).any()


def _sig(side: str) -> LiveSignal:
    return LiveSignal("S3", side, "4h", 0, 100.0, 95.0 if side == "long" else 105.0, 110.0 if side == "long" else 90.0, "")


def test_score_points_match_live_score() -> None:
    up = bars_from_close(np.linspace(100, 200, 100), bar_ms=86_400_000)
    dn = bars_from_close(np.linspace(200, 100, 100), bar_ms=86_400_000)
    trend = {None: 0, "up": 1, "dn": -1}
    bars = {None: None, "up": up, "dn": dn}
    for side in ("long", "short"):
        for coin in (None, "up", "dn"):
            for btc in (None, "up", "dn"):
                for rr in (1.0, 2.0, 2.5, 3.0, 4.0):
                    for is_btc in (False, True):
                        live = score(_sig(side), rr, bars[coin], bars[btc], is_btc).points
                        assert score_points(trend[coin], trend[btc], side, rr, is_btc) == live


def test_repeat_flags_analytic() -> None:
    ev = [10, 20, 45, 50]
    assert repeat_flags(ev, 30).tolist() == [False, True, True, True]
    assert repeat_flags(ev, 5).tolist() == [False, False, False, True]
    # an event exactly m bars earlier counts, m + 1 does not
    assert repeat_flags([0, 30], 30).tolist() == [False, True]
    assert repeat_flags([0, 31], 30).tolist() == [False, False]
    assert repeat_flags([5, 5], 30).tolist() == [False, False]
    assert repeat_flags([], 30).size == 0


def test_cluster_cap() -> None:
    keys = [(1, "long")] * 4 + [(1, "short"), (2, "long")]
    assert cluster_sizes(keys).tolist() == [4, 4, 4, 4, 1, 1]
    sc = [1, 3, 3, 0, 0, 0]
    rr = [5.0, 1.5, 2.0, 9.0, 1.0, 1.0]
    keep = cap_per_group(keys, sc, rr, 2)
    # score 3 twice (rr 2.0 before 1.5); the high-rr score-1 and score-0 rows are dropped; singletons kept
    assert keep.tolist() == [False, True, True, False, True, True]


def test_daily_sums() -> None:
    u, s = daily_sums(np.array([1.0, 2.0, -1.0]), np.array([5, 3, 5]))
    assert u.tolist() == [3, 5]
    assert np.allclose(s, [2.0, 0.0])


def test_cluster_boot_group_sums() -> None:
    v = np.array([1.0, 1.0, -2.0, -2.0, 1.0, 7.0])
    g = np.array([0, 0, 1, 1, 0, -1])  # the last row is outside the groups
    cl = np.array([1, 1, 1, 2, 3, 3])
    s, c = cluster_boot_group_sums(v, g, cl, 2, n_boot=400, seed=3)
    with np.errstate(invalid="ignore", divide="ignore"):
        m0, m1 = s[:, 0] / c[:, 0], s[:, 1] / c[:, 1]
    assert np.allclose(m0[np.isfinite(m0)], 1.0)
    assert np.allclose(m1[np.isfinite(m1)], -2.0)
    # 3 clusters drawn per replicate: group-0 rows per draw are 2 (cluster 1), 0 (cluster 2) or 1 (cluster 3)
    assert c[:, 0].min() >= 0 and c[:, 0].max() <= 6
    # group-1 rows: 1 in cluster 1 and 1 in cluster 2, so the count is the number of draws of clusters 1 and 2
    assert set(np.unique(c[:, 1]).tolist()) <= {0.0, 1.0, 2.0, 3.0}
    # the same seed gives the same draw
    s2, _ = cluster_boot_group_sums(v, g, cl, 2, n_boot=400, seed=3)
    assert np.array_equal(s, s2)


def test_cluster_boot_mean_centres_on_sample_mean() -> None:
    rng = np.random.default_rng(0)
    v = rng.normal(0.3, 1.0, 3000)
    cl = np.repeat(np.arange(600), 5)
    s, c = cluster_boot_group_sums(v, np.zeros(v.size, dtype=np.int64), cl, 1, n_boot=2000, seed=1)
    m = s[:, 0] / c[:, 0]
    assert abs(m.mean() - v.mean()) < 0.01
    assert 0.5 * v.std() / np.sqrt(v.size) < m.std() < 2.0 * v.std() / np.sqrt(v.size)


def test_boot_p() -> None:
    assert boot_p_two_sided(np.full(99, 1.0)) == 0.02
    assert boot_p_two_sided(np.concatenate([np.full(50, -1.0), np.full(50, 1.0)])) == 1.0


def test_causality_flags_do_not_see_later_bars() -> None:
    """Changing bars after t does not change any decision at or before t."""
    b = walk(400, seed=11)
    t = 250
    rng = np.random.default_rng(5)
    c2 = b.c.copy()
    c2[t + 1 :] = c2[t + 1 :] + np.cumsum(rng.normal(0, 5, c2.size - t - 1))
    b2 = bars_from_close(c2, t0=int(b.t[0]))
    assert np.array_equal(b.c[: t + 1], b2.c[: t + 1])
    for n in (3, 6, 24):
        for lag in (0, 1):
            f1 = btc_block_flags(b, n, lag)
            f2 = btc_block_flags(b2, n, lag)
            for a1, a2 in zip(f1, f2, strict=True):
                assert np.array_equal(a1[: t + 1], a2[: t + 1])
    assert np.array_equal(trend_dirs(b)[: t + 1], trend_dirs(b2)[: t + 1])
    # the prefix alone gives the same flags as the full series
    assert np.array_equal(btc_block_flags(b.slice(0, t + 1), 6, 1)[0], btc_block_flags(b, 6, 1)[0][: t + 1])
    ev1 = [10, 40, 60, 240, 249, 300, 330]
    ev2 = [10, 40, 60, 240, 249, 251, 252, 390]
    r1, r2 = repeat_flags(ev1, 30), repeat_flags(ev2, 30)
    assert r1[:5].tolist() == r2[:5].tolist()
