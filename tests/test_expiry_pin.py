"""Tests for analytics/expiry_pin.py (mirror-strike pinning statistic)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import numpy as np
import pytest

from tradedesk.analytics.expiry_pin import (
    attach_placebo,
    balanced_mean,
    cluster_bootstrap,
    holm,
    in_band,
    iso_week,
    mde,
    mirror_delta,
    ols_cluster,
    pick_k_star,
    pin_row,
    strike_volume,
    summarize_boot,
)
from tradedesk.analytics.gex_flow import FlowBook

T0 = int(datetime(2024, 3, 7, 9, tzinfo=UTC).timestamp() * 1000)
EXP = int(datetime(2024, 3, 8, 8, tzinfo=UTC).timestamp() * 1000)


def tr(name: str, amount: float, ts: int, index: float = 60000.0, direction: str = "buy") -> dict:
    return {"instrument_name": name, "amount": amount, "timestamp": ts, "index_price": index, "direction": direction, "iv": 50.0}


def sample_trades() -> list[dict]:
    return [
        tr("BTC-8MAR24-61000-C", 5.0, T0 - 5000, 60100.0),
        tr("BTC-8MAR24-61000-P", 2.0, T0 - 4000, 60200.0, "sell"),
        tr("BTC-8MAR24-59000-P", 6.0, T0 - 3000, 60300.0),
        tr("BTC-9MAR24-70000-C", 50.0, T0 - 2000, 60400.0),  # other expiry: ignored for K*
        tr("BTC-8MAR24-60000-C", 1.0, T0 - 1000, 60500.0),
    ]


def test_mirror_delta_analytic() -> None:
    # K* 2 % above spot, settle 1 % above: |s - M| = 3 %, |s - K*| = 1 % -> Delta = 0.02 = 2 * clip(0.01, -0.02, 0.02)
    assert mirror_delta(102.0, 100.0, 101.0) == pytest.approx(0.02)
    # settle beyond K*: Delta saturates at 2 d
    assert mirror_delta(102.0, 100.0, 110.0) == pytest.approx(0.04)
    # settle on the mirror side: symmetric negative
    assert mirror_delta(102.0, 100.0, 99.0) == pytest.approx(-0.02)
    # K* below spot, settle at spot: 0
    assert mirror_delta(98.0, 100.0, 100.0) == pytest.approx(0.0)
    # symmetry: swapping K* and M flips the sign
    assert mirror_delta(98.0, 100.0, 101.5) == pytest.approx(-mirror_delta(102.0, 100.0, 101.5))


def test_mirror_delta_zero_mean_under_symmetric_moves() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(0.0, 0.03, 200_000)
    d = np.asarray([mirror_delta(103.0, 100.0, 100.0 * (1 + u)) for u in x[:20_000]])
    assert abs(d.mean()) < 0.001
    assert np.allclose(d, 2 * np.clip(x[:20_000], -0.03, 0.03))


def test_strike_volume_and_k_star() -> None:
    book = FlowBook()
    book.add(sample_trades(), T0)
    vol = strike_volume(book, EXP)
    assert vol == {61000.0: 7.0, 59000.0: 6.0, 60000.0: 1.0}
    k, share = pick_k_star(vol, book.spot)  # type: ignore[misc]
    assert k == 61000.0 and share == pytest.approx(0.5)


def test_pick_k_star_tie_nearest_spot_then_lower() -> None:
    assert pick_k_star({100.0: 3.0, 104.0: 3.0}, 103.0) == (104.0, 0.5)
    assert pick_k_star({100.0: 3.0, 104.0: 3.0}, 102.0) == (100.0, 0.5)
    assert pick_k_star({}, 100.0) is None


def test_pin_row_fields() -> None:
    book = FlowBook()
    book.add(sample_trades(), T0)
    r = pin_row(book, T0, EXP)
    assert r is not None
    assert r.spot == 60500.0 and r.spot_age_ms == 1000
    assert r.k_star == 61000.0 and r.k_atm == 60000.0 and r.n_strikes == 3 and r.total == pytest.approx(14.0)
    assert pin_row(book, T0, EXP + 86_400_000).k_star == 70000.0  # type: ignore[union-attr]
    assert pin_row(book, T0, EXP + 2 * 86_400_000) is None


def test_pin_row_no_lookahead() -> None:
    """Prints after t0 are carried, not applied: K* at t0 is unchanged whatever comes later."""
    base = FlowBook()
    base.add(sample_trades(), T0)
    later = [tr("BTC-8MAR24-59000-P", 1000.0, T0 + 1, 70000.0), tr("BTC-8MAR24-65000-C", 500.0, T0 + 10)]
    book = FlowBook()
    carried = book.add(sample_trades() + later, T0)
    assert len(carried) == 2
    assert pin_row(book, T0, EXP) == pin_row(base, T0, EXP)
    book.add(carried, T0 + 100)
    with pytest.raises(ValueError):
        pin_row(book, T0, EXP)


def test_attach_placebo_uses_previous_day_only() -> None:
    rows = [{"d": "2024-03-05", "k_star": 1.0}, {"d": "2024-03-06", "k_star": 2.0}, {"d": "2024-03-08", "k_star": 3.0}]
    assert attach_placebo(rows) == [None, 1.0, None]
    changed = rows[:2] + [{"d": "2024-03-08", "k_star": 99.0}]
    assert attach_placebo(changed)[:2] == attach_placebo(rows)[:2]


def test_in_band() -> None:
    assert in_band(100.5, 100.0, 0.005, 0.06)
    assert not in_band(100.4, 100.0, 0.005, 0.06)
    assert not in_band(93.0, 100.0, 0.005, 0.06)


def test_balanced_mean_cancels_side_drift() -> None:
    v = [0.01, 0.01, 0.01, -0.01]
    a = [True, True, True, False]
    assert balanced_mean(v, a) == pytest.approx(0.0)
    assert np.mean(v) == pytest.approx(0.005)
    assert math.isnan(balanced_mean([1.0], [True]))


def test_cluster_bootstrap_and_summary() -> None:
    v = np.asarray([1.0, 1.0, 3.0, 3.0])
    b = cluster_bootstrap(lambda ix: float(v[ix].mean()), ["a", "a", "b", "b"], n_boot=4000, seed=3)
    # two clusters with means 1 and 3: bootstrap means in {1, 2, 3} with probabilities 1/4, 1/2, 1/4
    assert set(np.unique(b)) <= {1.0, 2.0, 3.0}
    assert b.mean() == pytest.approx(2.0, abs=0.05)
    assert b.std() == pytest.approx(math.sqrt(0.5), abs=0.05)
    s = summarize_boot(2.0, b)
    assert s["lo"] == 1.0 and s["hi"] == 3.0
    assert 0.0 < s["p"] < 0.01


def test_holm_and_mde() -> None:
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert holm([0.5]) == [0.5]
    assert mde(0.001) == pytest.approx(0.0028)


def test_ols_cluster_coefficients_and_singleton_clusters() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=500)
    y = 0.5 + 2.0 * x + rng.normal(scale=0.1, size=500)
    X = np.column_stack([np.ones(500), x])
    beta, se = ols_cluster(y, X, list(range(500)))
    assert beta == pytest.approx([0.5, 2.0], abs=0.02)
    assert se[1] == pytest.approx(0.1 / math.sqrt(500), rel=0.2)


def test_iso_week() -> None:
    assert iso_week(date(2024, 3, 8)) == "2024-W10"
    assert iso_week(date(2021, 1, 1)) == "2020-W53"
