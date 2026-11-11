from __future__ import annotations

import math

import pytest

from tradedesk.analytics import s3_1d_audit as a

D = a.DAY_MS


def test_full_stop_kinds() -> None:
    assert a.is_full_stop("stop") and a.is_full_stop("stop_gap")
    assert not any(a.is_full_stop(k) for k in ("be", "be_gap", "target", "time", "data_end", None))


def test_binom_upper_tail_analytic() -> None:
    assert a.binom_upper_tail(0.5, 9, 9) == pytest.approx(1 / 512)
    assert a.binom_upper_tail(0.6, 9, 9) == pytest.approx(0.6**9)
    assert a.binom_upper_tail(0.3, 2, 1) == pytest.approx(1 - 0.7**2)
    assert a.binom_upper_tail(0.3, 5, 0) == 1.0
    assert a.binom_upper_tail(0.3, 5, 6) == 0.0
    with pytest.raises(ValueError):
        a.binom_upper_tail(1.2, 5, 1)


def test_day_sizes_ignores_signals_after_cutoff() -> None:
    base = [0, 3_600_000, D + 5, D + 7, 2 * D + 1]
    assert a.day_sizes(base, 2 * D) == [2, 2]
    # signals at or after the cutoff never change the clusters before it
    assert a.day_sizes([*base, 2 * D, 3 * D, D + 9], 2 * D) == [3, 2]
    assert a.day_sizes(base + [5 * D, 9 * D], 2 * D) == a.day_sizes(base, 2 * D)


def test_week_buckets_and_windows() -> None:
    w = 7 * D
    assert a.day_sizes([0, D, 6 * D, w, w + D], 10 * w, bucket_ms=w) == [3, 2]
    r = a.all_true_windows([1, 1, 1, 0, 1, 1, 1, 1], 3)
    assert r["windows"] == 6 and r["count"] == 3 and r["share"] == pytest.approx(0.5)
    assert a.all_true_windows([1, 1], 3)["windows"] == 0
    # appending later trades never changes the windows that end before them
    assert a.all_true_windows([1, 1, 1, 0, 1, 1, 1, 1, 0, 0], 3)["count"] == 3


def test_cluster_all_stop_prob_hypergeometric() -> None:
    r = a.cluster_all_stop_prob([4], [2], [2])
    assert r["p"] == pytest.approx(1 / 6)  # C(2,2)/C(4,2)
    r = a.cluster_all_stop_prob([4, 2], [2, 2], [2, 1])
    assert r["q"][0] == pytest.approx((1 / 6 + 1) / 2)
    assert r["q"][1] == pytest.approx((0.5 + 1.0) / 2)
    assert r["p"] == pytest.approx(7 / 12 * 0.75)
    assert r["eligible"] == [2, 2]
    # a size larger than every day: undefined
    r = a.cluster_all_stop_prob([2, 3], [1, 3], [4])
    assert math.isnan(r["p"]) and r["eligible"] == [0]
    # one-trade days reduce to the share of stop days (the i.i.d. binomial with p = share)
    r = a.cluster_all_stop_prob([1] * 10, [1] * 6 + [0] * 4, [1] * 3)
    assert r["p"] == pytest.approx(0.6**3)


def test_first_touch_and_causality() -> None:
    t = [0, 1, 2, 3, 4]
    hi = [10.0, 10.5, 11.0, 10.2, 12.0]
    lo = [9.5, 9.6, 9.0, 8.5, 9.9]
    assert a.first_touch(t, hi, lo, "long", 0, 9.0) == 2
    assert a.first_touch(t, hi, lo, "long", 3, 9.0) == 3  # bars before t0 are skipped
    assert a.first_touch(t, hi, lo, "short", 0, 11.0) == 2
    assert a.first_touch(t, hi, lo, "long", 0, 8.0) is None
    # changing the bars after the first touch never changes it
    assert a.first_touch(t, hi, [9.5, 9.6, 9.0, 1.0, 1.0], "long", 0, 9.0) == 2


def test_by_quantile() -> None:
    t = a.by_quantile([1, 2, 3, 4, 5, 6, 7, 8], [0, 0, 1, 1, 0, 1, 1, 1], q=4)
    assert [x["n"] for x in t] == [2, 2, 2, 2]
    assert [x["mean"] for x in t] == [0, 1, 0.5, 1]
    assert t[0]["lo"] == 1 and t[-1]["hi"] == 8
    assert a.by_quantile([], [], 4) == []
