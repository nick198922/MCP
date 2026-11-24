"""KX-2 calendar pivots: analytic expectations for the pure helpers (registration: IDEA-20261113-x-traders 5.5)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics import kx2_calendar as kc

DAY = kc.DAY_MS


def ms(y: int, m: int, d: int, h: int = 0) -> int:
    return int(datetime(y, m, d, h, tzinfo=UTC).timestamp() * 1000)


def daily(start: int, n: int, growth: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    t = start + DAY * np.arange(n, dtype="int64")
    o = 100.0 * (1.0 + growth) ** np.arange(n)
    return t, o


def test_day_of_month_and_month_index() -> None:
    t = np.array([ms(2024, 2, 29), ms(2024, 3, 1), ms(2025, 12, 31, 23)])
    assert kc.day_of_month(t).tolist() == [29, 1, 31]
    assert kc.month_index(t).tolist() == [(2024 - 1970) * 12 + 1, (2024 - 1970) * 12 + 2, (2025 - 1970) * 12 + 11]


def test_window_return_and_gaps() -> None:
    t, o = daily(ms(2024, 1, 1), 40, growth=0.01)
    assert kc.window_return(t, o, ms(2024, 1, 14), 14) == pytest.approx(1.01**14 - 1)
    assert kc.window_return(t, o, ms(2024, 1, 14), 3) == pytest.approx(1.01**3 - 1)
    gap = np.delete(np.arange(40), 16)  # 2024-01-17 missing
    assert math.isnan(kc.window_return(t[gap], o[gap], ms(2024, 1, 14), 3))
    assert math.isnan(kc.window_return(t, o, ms(2024, 2, 1), 14))  # exit beyond the data


def test_window_return_causal() -> None:
    """A trade whose exit bar is at or before t is unchanged when bars after t change."""
    t, o = daily(ms(2024, 1, 1), 60, growth=0.003)
    before = kc.window_return(t, o, ms(2024, 1, 14), 14)
    o2 = o.copy()
    cut = int(np.searchsorted(t, ms(2024, 1, 28))) + 1
    o2[cut:] *= 5.0
    assert kc.window_return(t, o2, ms(2024, 1, 14), 14) == before
    m = kc.month_day_matrix(t, o, [kc.month_index(np.array([ms(2024, 1, 1)]))[0]], 3)
    m2 = kc.month_day_matrix(t, o2, [kc.month_index(np.array([ms(2024, 1, 1)]))[0]], 3)
    # entries on days 1..24 exit by the 27th, before the change
    assert np.array_equal(m[0, :24], m2[0, :24])


def test_entry_times_and_shift() -> None:
    t, _ = daily(ms(2024, 1, 1), 100)
    e = kc.entry_times(t, 14, ms(2024, 1, 1), ms(2024, 4, 1))
    assert e == [ms(2024, 1, 14), ms(2024, 2, 14), ms(2024, 3, 14)]
    assert kc.entry_times(t, 1, ms(2024, 1, 1), ms(2024, 4, 1), shift_days=1) == [ms(2024, 1, 2), ms(2024, 2, 2), ms(2024, 3, 2)]


def test_funding_and_net_return() -> None:
    f_t = np.array([ms(2024, 1, 1, h) for h in (0, 8, 16)] + [ms(2024, 1, 2, 0)], dtype="int64")
    f_r = np.array([0.001, 0.002, 0.003, 0.004])
    # t0 < time <= t1: the 00:00 funding at entry is excluded, the one at exit included
    assert kc.funding_sum(f_t, f_r, ms(2024, 1, 1), ms(2024, 1, 2)) == pytest.approx(0.009)
    assert kc.funding_sum(f_t, f_r, ms(2019, 1, 1), ms(2019, 1, 15), proxy_before=ms(2019, 9, 10)) == pytest.approx(14 * 3 * 0.0001)
    assert kc.net_return(0.05, "long", 0.002, 0.0015) == pytest.approx(0.05 - 0.0015 - 0.002)
    assert kc.net_return(0.05, "short", 0.002, 0.0015) == pytest.approx(-0.05 - 0.0015 + 0.002)


def test_month_day_matrix_constant_growth() -> None:
    t, o = daily(ms(2024, 1, 1), 120, growth=0.002)
    months = sorted(set(kc.month_index(t[:60]).tolist()))
    m = kc.month_day_matrix(t, o, months, 14)
    assert m.shape == (2, 28)
    assert np.allclose(m, 1.002**14 - 1)


def test_placebo_excludes_target_and_rank_p() -> None:
    sided = np.zeros((50, 28))
    sided[:, 13] = 1.0  # only the target day (14th) pays
    plc = kc.placebo_means(sided, 14, 500, seed=1)
    assert np.all(plc == 0.0)
    assert kc.placebo_p(1.0, plc) == pytest.approx(1.0 / 501)
    assert kc.placebo_p(-1.0, plc) == pytest.approx(1.0)
    same = kc.placebo_means(sided, 14, 500, seed=1)
    assert np.array_equal(plc, same)


def test_mean_ci_and_mde() -> None:
    x = np.array([0.01, 0.03] * 50)
    r = kc.mean_ci(x, 2000, seed=3)
    assert r["n"] == 100
    assert r["m"] == pytest.approx(0.02)
    assert r["lo"] < 0.02 < r["hi"]
    assert r["se"] == pytest.approx(np.std(x, ddof=1) / 10)
    assert r["p"] < 1e-6
    assert kc.z_quantile(0.975) == pytest.approx(1.959964, abs=1e-5)
    # one-sided alpha 0.05 / 4 and power 0.8: (2.2414 + 0.8416) * 0.12 / sqrt(136)
    assert kc.mde80(0.12, 136, 4) == pytest.approx((2.241403 + 0.841621) * 0.12 / math.sqrt(136), rel=1e-5)


def test_funding_for_side_no_short_credit_before_history() -> None:
    f_t = np.array([ms(2019, 9, 10, 8), ms(2019, 9, 10, 16), ms(2019, 9, 11, 0)], dtype="int64")
    f_r = np.array([0.0003, 0.0002, -0.0001])
    start = int(f_t[0])
    t0 = ms(2019, 9, 1)
    # before the funding history: the long pays the proxy for 14 days (42 x 0.01 %), the short gets nothing
    assert kc.funding_for_side(f_t, f_r, t0, t0 + 14 * DAY, "long", start) == pytest.approx(0.0042)
    assert kc.funding_for_side(f_t, f_r, t0, t0 + 14 * DAY, "short", start) == 0.0
    # inside the history: actual points with t0 < t <= t1, the same for both sides (the sign is applied by net_return)
    t0 = ms(2019, 9, 10, 8)
    for side in ("long", "short"):
        assert kc.funding_for_side(f_t, f_r, t0, ms(2019, 9, 11), side, start) == pytest.approx(0.0001)
    assert kc.net_return(0.0, "short", 0.0001, 0.0) == pytest.approx(0.0001)


def test_round_to_hour() -> None:
    t = np.array([1_568_102_400_001, 1_568_131_200_009, 1_568_160_000_000, 1_568_188_799_990], dtype="int64")
    assert kc.round_to_hour(t).tolist() == [1_568_102_400_000, 1_568_131_200_000, 1_568_160_000_000, 1_568_188_800_000]


def test_net_target_per_side() -> None:
    fee = 0.0015
    # long 14 d: 4 % - 0.15 % - 42 x 0.01 % = 3.43 %
    assert kc.net_target(0.04, "long", 14, fee, 0.5) == pytest.approx(0.0343)
    # short 14 d: credit only on the real-funding share
    assert kc.net_target(0.04, "short", 14, fee, 0.0) == pytest.approx(0.0385)
    assert kc.net_target(0.04, "short", 14, fee, 1.0) == pytest.approx(0.0427)
    assert kc.net_target(0.04, "short", 14, fee, 0.5) == pytest.approx(0.0406)
    # 3 d: 9 funding points
    assert kc.net_target(0.02, "long", 3, fee, 0.0) == pytest.approx(0.0176)
