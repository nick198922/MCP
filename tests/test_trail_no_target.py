import numpy as np
import pytest

from tradedesk.analytics.trail_no_target import (
    EIGHT_H_MS,
    dedup_by_base,
    drop_best,
    flat_paid,
    funding_paid,
    rank_band,
)


def test_dedup_by_base_keeps_first_of_each_base():
    assert dedup_by_base(["BTCUSDT", "1000PEPEUSDT", "PEPEUSDT", "ETHUSDT"]) == ["BTCUSDT", "1000PEPEUSDT", "ETHUSDT"]


def test_rank_band_ranks_after_dedup_and_excludes_without_shifting():
    ranked = ["A1USDT", "1000A1USDT", "B2USDT", "C3USDT", "D4USDT", "E5USDT"]
    # dedup: A1(1) B2(2) C3(3) D4(4) E5(5)
    assert rank_band(ranked, 2, 4) == {"B2USDT": 2, "C3USDT": 3, "D4USDT": 4}
    assert rank_band(ranked, 2, 4, exclude=["C3USDT"]) == {"B2USDT": 2, "D4USDT": 4}
    # an excluded base removes its other ticker too
    assert rank_band(["X9USDT", "B2USDT", "1000C3USDT"], 2, 3, exclude=["C3USDT"]) == {"B2USDT": 2}


def test_rank_band_short_list():
    assert rank_band(["A1USDT", "B2USDT"], 31, 100) == {}


def test_flat_paid_pro_rata():
    assert flat_paid(0, 3 * EIGHT_H_MS) == pytest.approx(0.0003)
    assert flat_paid(0, EIGHT_H_MS // 2, 0.0002) == pytest.approx(0.0001)
    assert flat_paid(10, 5) == 0.0


def test_funding_paid_sign_interval_and_jitter():
    t = np.array([0, EIGHT_H_MS + 7, 2 * EIGHT_H_MS + 12, 3 * EIGHT_H_MS], dtype=np.int64)
    rate = np.array([0.001, 0.0001, 0.0003, -0.0002])
    # entry exactly at a funding time: that payment is not counted; exit exactly at one: not counted either
    paid, src = funding_paid(t, rate, 0, 3 * EIGHT_H_MS, side=1)
    assert src == "actual"
    assert paid == pytest.approx(0.0004)
    paid_s, _ = funding_paid(t, rate, 0, 3 * EIGHT_H_MS, side=-1)
    assert paid_s == pytest.approx(-0.0004)


def test_funding_paid_falls_back_to_flat_before_history():
    t = np.array([10 * EIGHT_H_MS], dtype=np.int64)
    paid, src = funding_paid(t, np.array([0.01]), 0, 2 * EIGHT_H_MS, side=-1)
    assert src == "flat"
    assert paid == pytest.approx(0.0002)
    paid, src = funding_paid(np.zeros(0, dtype=np.int64), np.zeros(0), 0, EIGHT_H_MS, side=1)
    assert (paid, src) == (pytest.approx(0.0001), "flat")


def test_funding_paid_no_lookahead():
    """Funding after the exit does not change the result."""
    t = np.arange(0, 10 * EIGHT_H_MS, EIGHT_H_MS, dtype=np.int64)
    rate = np.full(t.size, 0.0001)
    a, _ = funding_paid(t, rate, 0, 4 * EIGHT_H_MS + 1, side=1)
    rate2 = rate.copy()
    rate2[5:] = 0.05
    b, _ = funding_paid(t, rate2, 0, 4 * EIGHT_H_MS + 1, side=1)
    assert a == b == pytest.approx(0.0004)


def test_drop_best():
    r = np.array([1.0, 5.0, -1.0, 5.0, 0.0])
    assert drop_best(r, 0.2).tolist() == [True, False, True, True, True]
    assert drop_best(r, 0.4).tolist() == [True, False, True, False, True]
    assert drop_best(np.zeros(0), 0.05).size == 0
