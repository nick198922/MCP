"""Tests for analytics.xs_volume (analytic references, no network)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics.funding_crowding import Bars
from tradedesk.analytics.xs_factors import partial_rank_ic
from tradedesk.analytics.xs_volume import (
    amihud,
    dollar_volume,
    features_at,
    partial_ic_products,
    partial_residuals,
    placebo_corr_mean,
    quantile_spread,
    shift_placebo_mean_ics,
    volume_shock,
)


def make_bars(closes: dict[int, float], qv: dict[int, float]) -> Bars:
    return Bars(dict(closes), dict(closes), dict(closes), dict(closes), dict(qv))


def geometric(days: range, growth: float = 0.1, qv: float = 2e6) -> Bars:
    c = {d: 100.0 * (1.0 + growth) ** i for i, d in enumerate(days)}
    return make_bars(c, dict.fromkeys(days, qv))


def test_amihud_constant_return_and_volume():
    b = geometric(range(0, 100))
    assert amihud(b, 60) == pytest.approx(0.1 / 2e6, rel=1e-12)


def test_amihud_uses_abs_return_and_skips_zero_volume():
    closes = {d: (100.0 if d % 2 == 0 else 90.0) for d in range(0, 50)}
    qv = dict.fromkeys(range(0, 50), 1e6)
    qv[45] = 0.0  # skipped
    b = make_bars(closes, qv)
    # days 31..49 (excluding 45): returns alternate -0.1 (odd d) and +1/9 (even d)
    exp = [abs(closes[d] / closes[d - 1] - 1.0) / 1e6 for d in range(20, 50) if d != 45]
    assert amihud(b, 50) == pytest.approx(sum(exp) / len(exp), rel=1e-12)


def test_amihud_min_days():
    b = geometric(range(40, 60))  # 19 returns before day 60
    assert amihud(b, 60) is None
    assert amihud(b, 60, min_days=19) == pytest.approx(0.1 / 2e6)


def test_volume_shock_doubling():
    qv = {d: (2.0 if d >= 93 else 1.0) for d in range(0, 100)}
    b = make_bars(dict.fromkeys(range(0, 100), 1.0), qv)
    assert volume_shock(b, 100) == pytest.approx(math.log(2.0))
    assert volume_shock(make_bars({}, {}), 100) is None


def test_volume_shock_needs_baseline():
    qv = dict.fromkeys(range(70, 100), 1.0)  # baseline 70..92 = 23 days < 30
    assert volume_shock(make_bars(dict.fromkeys(range(70, 100), 1.0), qv), 100) is None


def test_dollar_volume():
    b = geometric(range(0, 40), qv=1e6)
    assert dollar_volume(b, 40) == pytest.approx(math.log(1e6))
    assert dollar_volume(geometric(range(30, 40)), 40) is None


def test_features_point_in_time():
    """Features at t do not change when bars at or after t change."""
    b = geometric(range(0, 200), growth=0.02)
    before = features_at(b, 150)
    c2 = dict(b.close)
    q2 = dict(b.qv)
    for d in range(150, 200):
        c2[d] *= 3.0
        q2[d] *= 50.0
    after = features_at(make_bars(c2, q2), 150)
    assert before == after
    assert all(v is not None for v in before.values())


def test_shift_placebo_zero_shift_is_ic():
    syms = [f"S{i}" for i in range(30)]
    sig = [{s: float(i) for i, s in enumerate(syms)} for _ in range(4)]
    ret = [{s: float(i) for i, s in enumerate(syms)} for _ in range(4)]
    assert shift_placebo_mean_ics(sig, ret, [0])[0] == pytest.approx(1.0)
    # reversed signal in every other week: shift 1 pairs opposite weeks
    sig2 = [{s: (float(i) if w % 2 == 0 else -float(i)) for i, s in enumerate(syms)} for w in range(4)]
    ret2 = [{s: (float(i) if w % 2 == 0 else -float(i)) for i, s in enumerate(syms)} for w in range(4)]
    assert shift_placebo_mean_ics(sig2, ret2, [1])[0] == pytest.approx(-1.0)
    assert math.isnan(shift_placebo_mean_ics(sig, ret, [0], min_common=31)[0])


def test_shift_placebo_length_mismatch():
    with pytest.raises(ValueError):
        shift_placebo_mean_ics([{}], [], [0])


def test_partial_products_mean_is_partial_ic():
    rng = np.random.default_rng(1)
    x, y, c = rng.normal(size=80), rng.normal(size=80), rng.normal(size=80)
    y = y + 0.5 * x + 0.3 * c
    pr = partial_ic_products(x, y, [c])
    assert float(pr.mean()) == pytest.approx(partial_rank_ic(x, y, [c]), abs=1e-12)
    a, b = partial_residuals(x, y, [c])
    assert float(np.sum(a * a)) == pytest.approx(1.0)
    assert float(a @ b) == pytest.approx(partial_rank_ic(x, y, [c]), abs=1e-12)


def test_placebo_corr_mean_null():
    rng = np.random.default_rng(2)
    pairs = []
    for _ in range(20):
        a, b = partial_residuals(rng.normal(size=60), rng.normal(size=60), [])
        pairs.append((a, b))
    perm = placebo_corr_mean(pairs, 400, seed=3)
    assert perm.shape == (400,)
    assert abs(float(perm.mean())) < 0.01
    # SD of a mean of 20 permuted correlations of n=60 is about 1/sqrt(59 * 20)
    assert float(perm.std()) == pytest.approx(1.0 / math.sqrt(59 * 20), rel=0.25)


def test_quantile_spread():
    xs = [float(i) for i in range(10)]
    assert quantile_spread(xs, xs, q=5) == pytest.approx(8.0)
    assert quantile_spread(xs, [-x for x in xs], q=5) == pytest.approx(-8.0)
    with pytest.raises(ValueError):
        quantile_spread(xs[:5], xs[:5], q=5)
