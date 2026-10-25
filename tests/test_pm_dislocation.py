"""Tests for analytics/pm_dislocation.py (IDEA-20261025-pm-dislocation-reversion)."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.pm_dislocation import (
    confirmed_by_prints,
    detect_dislocations,
    flow_class,
    last_print,
    mde_clustered,
    notional,
    placebo_times,
    reversion,
    vwap,
    yes_prints,
)

STEP = 600


def series(prices: list[float], t0: int = 1_000_000) -> list[tuple[int, float]]:
    return [(t0 + i * STEP, p) for i, p in enumerate(prices)]


def test_detector_fires_at_first_crossing_with_start_point():
    pts = series([0.50, 0.50, 0.51, 0.53, 0.56, 0.60, 0.62])
    ev = detect_dislocations(pts)
    assert len(ev) == 1
    e = ev[0]
    assert e["sign"] == 1
    assert e["t_end"] == pts[4][0]  # 0.56 - 0.50 = 0.06 >= 0.05 first at index 4
    assert e["p_start"] == pytest.approx(0.50)
    assert e["t_start"] == pts[0][0]


def test_detector_down_move_and_window_limit():
    # a slow drift of 6 pp over 2 h never exceeds 5 pp inside any 60-min window
    slow = series([0.50 - 0.005 * i for i in range(13)])
    assert detect_dislocations(slow) == []
    fast = series([0.50, 0.49, 0.47, 0.44])
    ev = detect_dislocations(fast)
    assert len(ev) == 1 and ev[0]["sign"] == -1


def test_detector_price_band_and_dedupe():
    # start below 0.10 is not allowed
    assert detect_dislocations(series([0.05, 0.12])) == []
    # two jumps 2 h apart: the second is inside the 6 h de-dup window
    pts = series([0.50, 0.57] + [0.57] * 11 + [0.64] + [0.64] * 40 + [0.71])
    ev = detect_dislocations(pts)
    assert len(ev) == 2
    assert ev[1]["t_end"] - ev[0]["t_end"] >= 6 * 3600


def test_detector_causal_past_unchanged_by_future():
    base = [0.50, 0.50, 0.56, 0.56, 0.56] + [0.56] * 40
    a = detect_dislocations(series(base + [0.56] * 10))
    b = detect_dislocations(series(base + [0.20, 0.90, 0.30, 0.95, 0.10, 0.60, 0.20, 0.80, 0.30, 0.70]))
    cut = 1_000_000 + len(base) * STEP
    assert [e for e in a if e["t_end"] < cut] == [e for e in b if e["t_end"] < cut]


def test_yes_prints_mapping_and_vwap():
    rows = [
        {"timestamp": 10, "asset": "Y", "side": "BUY", "price": 0.60, "size": 100, "proxyWallet": "a"},
        {"timestamp": 20, "asset": "N", "side": "BUY", "price": 0.30, "size": 100, "proxyWallet": "b"},
        {"timestamp": 30, "asset": "N", "side": "SELL", "price": 0.35, "size": 10, "proxyWallet": "c"},
        {"timestamp": 40, "asset": "Y", "side": "HOLD", "price": 0.5, "size": 1},
    ]
    pr = yes_prints(rows, "Y")
    assert [x["dir"] for x in pr] == [1, -1, 1]
    assert pr[1]["p"] == pytest.approx(0.70)
    assert pr[1]["usd"] == pytest.approx(30.0)
    # vwap weights by USD: (0.6*60 + 0.7*30 + 0.65*3.5) / 93.5
    assert vwap(pr, 0, 100) == pytest.approx((0.6 * 60 + 0.7 * 30 + 0.65 * 3.5) / 93.5)
    assert vwap(pr, 50, 60) is None
    assert notional(pr, 10, 30) == pytest.approx(90.0)  # [10, 30)
    assert last_print(pr, 25) == (20, pytest.approx(0.70))
    assert last_print(pr, 5) is None


def _p(t, d, usd, w):
    return {"t": t, "p": 0.5, "usd": usd, "dir": d, "wallet": w}


def test_flow_class_concentrated_and_two_sided_excluded():
    prints = [_p(1, 1, 700, "whale"), _p(2, 1, 100, "x"), _p(3, 1, 100, "y")]
    prints += [_p(4, 1, 500, "bot"), _p(5, -1, 500, "bot")]  # two-sided wallet: not pressure
    prints += [_p(6, 1, 10, f"r{i}") for i in range(10)]
    fc = flow_class(prints, 0, 10, 1)
    assert fc["cls"] == "C"
    assert fc["n_two_sided"] == 1
    assert fc["D"] == pytest.approx(1000.0)
    assert fc["top_share"] == pytest.approx(0.90)


def test_flow_class_broad_mixed_none():
    broad = [_p(i, 1, 10, f"w{i}") for i in range(25)]
    assert flow_class(broad, 0, 100, 1)["cls"] == "B"
    mixed = [_p(i, 1, 10, f"w{i}") for i in range(10)]  # top-3 share 30 %, only 10 wallets
    assert flow_class(mixed, 0, 100, 1)["cls"] == "mixed"
    against = [_p(1, -1, 100, "a")]
    assert flow_class(against, 0, 100, 1)["cls"] == "none"
    # outside the window nothing counts
    assert flow_class(broad, 1000, 2000, 1)["cls"] == "none"


def test_confirmation_and_reversion():
    assert confirmed_by_prints(0.50, 0.56, 1)
    assert not confirmed_by_prints(0.50, 0.53, 1)
    assert confirmed_by_prints(0.50, 0.44, -1)
    assert not confirmed_by_prints(None, 0.6, 1)
    assert reversion(1, 0.60, 0.57) == pytest.approx(0.03)
    assert reversion(-1, 0.40, 0.43) == pytest.approx(0.03)
    assert reversion(1, 0.60, 0.65) == pytest.approx(-0.05)


def test_mde_clustered_analytic():
    r = mde_clustered(0.06, [1] * 1200)
    assert r["n_eff"] == pytest.approx(1200)
    assert r["mde"] == pytest.approx(2.8 * 0.06 / math.sqrt(1200))
    # 100 clusters of 4: n_eff = 400^2 / (100 * 16) = 100
    assert mde_clustered(0.06, [4] * 100)["n_eff"] == pytest.approx(100)
    assert mde_clustered(0.06, [])["mde"] == math.inf


def test_placebo_times_avoid_events_and_moves():
    pts = series([0.5] * 100)
    ev = [{"t_end": pts[50][0]}]
    ts = placebo_times(pts, ev, n=10, seed=1)
    assert len(ts) == 10
    assert all(abs(t - pts[50][0]) >= 6 * 3600 for t in ts)
    jumpy = series([0.5, 0.6] * 50)
    assert placebo_times(jumpy, [], n=5, seed=1) == []
