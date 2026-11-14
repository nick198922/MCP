import math

import pytest

from tradedesk.analytics import sample_pace as sp

W = sp.WEEK_MS


def test_weekly_counts_bins_zero_weeks_and_bounds():
    start = 1_000 * W
    ts = [start - 1, start, start + 1, start + W + 5, start + 3 * W - 1, start + 3 * W]
    assert sp.weekly_counts(ts, start, 3) == [2, 1, 1]
    assert sp.weekly_counts([], start, 2) == [0, 0]
    with pytest.raises(ValueError):
        sp.weekly_counts(ts, start, -1)


def test_weekly_counts_causal():
    """Counts of weeks before t do not change when events after t change."""
    start = 0
    early = [1, W + 2, 2 * W + 3]
    a = sp.weekly_counts(early + [5 * W + 1], start, 8)
    b = sp.weekly_counts(early + [4 * W, 6 * W + 9, 7 * W + 1, 7 * W + 2], start, 8)
    assert a[:4] == b[:4] == [1, 1, 1, 0]


def test_weekly_rate_t_interval():
    r = sp.weekly_rate([1, 3] * 5)
    assert r["weeks"] == 10 and r["total"] == 20 and r["mean"] == pytest.approx(2.0)
    # sd sqrt(10/9), se sd / sqrt(10) = 1/3, t(0.975, 9) = 2.2622 (Cornish-Fisher, error < 1e-3 for df >= 5)
    half = 2.2622 / 3
    assert r["lo"] == pytest.approx(2.0 - half, abs=0.01)
    assert r["hi"] == pytest.approx(2.0 + half, abs=0.01)


def test_poisson_sf():
    assert sp.poisson_sf(0, 3.0) == 1.0
    assert sp.poisson_sf(1, 2.0) == pytest.approx(1 - math.exp(-2.0))
    assert sp.poisson_sf(2, 1.0) == pytest.approx(1 - 2 * math.exp(-1.0))
    assert sp.poisson_sf(3, 0.0) == 0.0


@pytest.mark.parametrize(
    ("k", "lo", "hi"),
    [(0, 0.0, 3.6889), (1, 0.02532, 5.5716), (6, 2.2019, 13.0595)],  # chi2 quantiles / 2 (Garwood)
)
def test_poisson_ci_garwood(k, lo, hi):
    a, b = sp.poisson_ci(k)
    assert a == pytest.approx(lo, abs=2e-4)
    assert b == pytest.approx(hi, abs=2e-4)


def test_days_to_reach_and_prob_reach():
    assert sp.days_to_reach(30, 6, 0.5) == pytest.approx(48.0)
    assert sp.days_to_reach(30, 31, 0.5) == 0.0
    assert math.isinf(sp.days_to_reach(30, 6, 0.0))
    assert sp.prob_reach(1, 0.25, 4.0) == pytest.approx(1 - math.exp(-1.0))
    assert sp.prob_reach(5, 1.0, 0.0) == 0.0
