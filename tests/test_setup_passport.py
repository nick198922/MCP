"""Setup passport analytics (IDEA-20261030-demo-setups-depth): analytic expectations and no look-ahead."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics import setup_passport as sp
from tradedesk.analytics import setup_signals as ss
from tradedesk.analytics.course_s14_ema import DAY_MS, Bars

T0 = 1_700_000_000_000 // DAY_MS * DAY_MS


def _daily(n: int = 300, seed: int = 3) -> Bars:
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-0.01, 0.0, 0.01], size=n // 40 + 1), 40)[:n]
    c = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.02, n)))
    o = np.concatenate([[c[0]], c[:-1]])
    h, low = np.maximum(o, c) * 1.01, np.minimum(o, c) * 0.99
    return Bars((T0 + np.arange(n) * DAY_MS).astype(np.int64), o, h, low, c, DAY_MS)


def test_trend_series_equals_live_trend_dir_on_every_prefix():
    d = _daily()
    tr = sp.trend_series(d)
    assert [int(x) for x in tr] == [ss.trend_dir(d.slice(0, k + 1)) for k in range(len(d))]
    assert {1, -1} <= set(tr.tolist())


def test_trend_series_has_no_lookahead():
    d = _daily()
    a = sp.trend_series(d)
    c2 = d.c.copy()
    c2[200:] *= 3.0
    b = sp.trend_series(Bars(d.t, d.o, np.maximum(d.h, c2), d.low, c2, DAY_MS))
    assert np.array_equal(a[:200], b[:200])


def test_trend_at_uses_the_last_closed_day():
    d = _daily()
    tr = sp.trend_series(d)
    assert sp.trend_at(d, tr, int(d.t[100]) + DAY_MS) == tr[100]  # day 100 closes exactly then
    assert sp.trend_at(d, tr, int(d.t[100]) + DAY_MS - 1) == tr[99]
    assert sp.trend_at(d, tr, int(d.t[0])) == 0


@pytest.mark.parametrize(
    ("side", "rr", "coin", "btc", "is_btc", "want"),
    [("long", 1.6, 1, 1, False, 2), ("short", 3.1, -1, 1, False, 3), ("long", 2.0, 0, 0, False, 1), ("long", 1.0, 1, 1, True, 1)],
)
def test_score_points_matches_live_score(side, rr, coin, btc, is_btc, want):
    assert sp.score_points(side, rr, coin, btc, is_btc) == want
    d = _daily()
    tr = sp.trend_series(d)
    k = next(i for i in range(60, len(d)) if tr[i] == 1)
    up = d.slice(0, k + 1)
    sig = ss.LiveSignal("S3", side, "4h", 0, 1.0, 0.9, 1.2, "")
    live = ss.score(sig, rr, up, up, is_btc).points
    assert live == sp.score_points(side, rr, 1, 1, is_btc)


def test_trailing_turnover_and_groups():
    h_t = np.arange(48, dtype=np.int64) * 3_600_000
    qv = np.full(48, 1e5)
    assert sp.trailing_turnover(48 * 3_600_000, h_t, qv) == pytest.approx(24 * 1e5)
    assert sp.trailing_turnover(10 * 3_600_000, h_t, qv) is None  # only 10 hours before
    qv2 = qv.copy()
    qv2[30:] = 1e9  # hours at or after t do not count
    assert sp.trailing_turnover(30 * 3_600_000, h_t, qv2) == pytest.approx(24 * 1e5)
    assert sp.turnover_group(True, 1.0) == sp.TOP30
    assert sp.turnover_group(False, 5e6) == sp.GE5M
    assert sp.turnover_group(False, 4.99e6) == sp.M1TO5
    assert sp.turnover_group(False, 9e5) == sp.LT1M
    assert sp.turnover_group(False, None) == sp.UNKNOWN


def test_state_before_takes_the_previous_closed_day():
    states = {T0: "growth", T0 + DAY_MS: "decline"}
    assert sp.state_before(T0 + DAY_MS + 8 * 3_600_000, states) == "growth"  # a 4h signal on day 2
    assert sp.state_before(T0 + 2 * DAY_MS, states) == "decline"  # the 1d signal of day 2 closes at its end
    assert sp.state_before(T0 + 5 * DAY_MS, states) == "none"


def test_busy_chain_keeps_one_trade_per_symbol():
    sym = ["A", "A", "B", "A", "A"]
    t_in = np.array([0, 5, 5, 10, 10])
    ex = np.array([10, 7, 6, 20, 12])
    order = np.array([0, 0, 0, 1, 0])
    keep = sp.busy_chain(sym, t_in, ex, order)
    # A@0 busy to 10; A@5 skipped; B kept; at t=10 the order-0 candidate (index 4) wins, index 3 is then busy
    assert keep.tolist() == [True, False, True, False, True]


def test_busy_chain_capped_takes_the_best_two_free_per_bar():
    # bar t=0: A, B, C, D ranked C < A < D < B -> C and A kept; bar t=5: A busy (exit 9) is skipped without using the
    # cap, then E and B (rank order) fill it, F is over the cap and does not occupy F; bar t=10: F free again
    sym = ["A", "B", "C", "D", "A", "E", "B", "F", "F"]
    t_in = np.array([0, 0, 0, 0, 5, 5, 5, 5, 10])
    ex = np.array([9, 3, 4, 4, 20, 20, 20, 20, 30])
    rank = np.array([1, 3, 0, 2, 0, 1, 2, 3, 0])
    keep = sp.busy_chain_capped(sym, t_in, ex, rank, cap=2)
    assert keep.tolist() == [True, False, True, False, False, True, True, False, True]
    # with a large cap it is busy_chain with the rank as order
    assert sp.busy_chain_capped(sym, t_in, ex, rank, cap=99).tolist() == sp.busy_chain(sym, t_in, ex, rank).tolist()


def test_busy_chain_capped_has_no_lookahead():
    rng = np.random.default_rng(7)
    n = 200
    sym = [f"S{k}" for k in rng.integers(0, 12, n)]
    t_in = rng.integers(0, 40, n) * 4
    ex = t_in + rng.integers(1, 30, n)
    rank = rng.random(n)
    base = sp.busy_chain_capped(sym, t_in, ex, rank, cap=2)
    cut = 80
    later = t_in > cut
    ex2, rank2 = ex.copy(), rank.copy()
    ex2[later] += 1000
    rank2[later] = rng.random(int(later.sum()))
    sym2 = [("Z" if lt else s) for s, lt in zip(sym, later, strict=True)]
    other = sp.busy_chain_capped(sym2, t_in, ex2, rank2, cap=2)
    assert (base[~later] == other[~later]).all()


def test_late_entry():
    b = Bars(
        np.array([0, 4, 8, 16], dtype=np.int64), *(np.array([1.0, 1.0, 1.0, 1.0]) for _ in range(3)), np.array([1.0, 1.1, 1.3, 1.0]), 4
    )
    assert sp.late_entry(b, 0, 0.9, 1.2) == (8, 1.1)
    assert sp.late_entry(b, 1, 0.9, 1.2) is None  # the next close is beyond the target
    assert sp.late_entry(b, 2, 0.9, 1.2) is None  # hole after bar 2
    assert sp.late_entry(b, 3, 0.9, 1.2) is None  # no next bar


def test_mean_ci_and_diff_ci():
    rng = np.random.default_rng(0)
    v = rng.normal(0.5, 1.0, 2000)
    cl = np.arange(2000) // 10
    r = sp.mean_ci(v, cl, n_boot=2000)
    assert r["n"] == 2000 and r["k"] == 200
    assert r["lo"] < r["m"] < r["hi"] and r["lo"] > 0.3 and r["p"] < 0.01
    assert r["se"] == pytest.approx(1.0 / math.sqrt(2000), rel=0.25)
    z = sp.mean_ci(rng.normal(0, 1, 2000), cl, n_boot=2000)
    assert z["lo"] < 0 < z["hi"]
    a = np.arange(2000) % 2 == 0
    v2 = np.where(a, v + 1.0, v)
    d = sp.diff_ci(v2, a, ~a, cl, n_boot=2000)
    assert d["lo"] < d["d"] < d["hi"] and d["d"] == pytest.approx(1.0, abs=0.15) and d["lo"] > 0.7
    assert math.isnan(sp.mean_ci([1.0], [0])["lo"])


def test_r_band():
    v = np.array([1.0, -1.0] * 50)
    b = sp.r_band(v, 30, reps=4000)
    assert b["iid_p5"] < 0 < b["iid_p95"] and b["iid_p50"] == pytest.approx(0.0, abs=0.07)
    # i.i.d. mean of 30 draws of +-1: sd = 1/sqrt(30) = 0.183, 5th pct about -0.30
    assert b["iid_p5"] == pytest.approx(-1.645 / math.sqrt(30), abs=0.06)
    assert b["windows"] == 71 and b["roll_p5"] == pytest.approx(0.0) and b["roll_p95"] == pytest.approx(0.0)
    assert math.isnan(sp.r_band(v[:10], 30)["iid_p5"])
