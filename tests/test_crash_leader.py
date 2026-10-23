"""tradedesk.analytics.crash_leader: crash events, hold returns with delisting and funding, terciles, beta."""

import math

import numpy as np
import pytest

from tradedesk.analytics.crash_leader import beta, compound_index, detect_events, hold_return, terciles


def test_compound_index_treats_nan_as_flat():
    assert compound_index(np.array([0.1, np.nan, -0.5])) == pytest.approx([1.1, 1.1, 0.55])


def test_detect_events_first_crossing_and_rearm():
    # 1.0 → 0.65 (−35 %: event on day 2), partial bounce to 0.9 (dd −10 %: re-armed), then −35 % again.
    idx = np.array([1.0, 0.8, 0.65, 0.6, 0.9, 0.9, 0.58])
    ev = detect_events(idx, threshold=0.30, lookback=90, rearm=0.15, min_gap=1)
    assert [e.day for e in ev] == [2, 6]
    assert ev[0].peak == 0 and ev[0].drawdown == pytest.approx(-0.35)
    assert ev[1].peak == 0  # the 90-day max is still day 0
    # Without a recovery above −15 % there is no second event.
    flat = detect_events(np.array([1.0, 0.65, 0.6, 0.62, 0.55]), threshold=0.30, lookback=90, rearm=0.15, min_gap=1)
    assert [e.day for e in flat] == [1]


def test_detect_events_respects_min_gap_and_lookback():
    idx = np.array([1.0, 0.65, 0.95, 0.6])
    assert [e.day for e in detect_events(idx, threshold=0.3, rearm=0.15, min_gap=10)] == [1]
    # With a 2-day lookback the old peak drops out of the window: 0.6 / 0.95 = −37 % still counts from day 2.
    ev = detect_events(idx, threshold=0.3, lookback=2, rearm=0.15, min_gap=1)
    assert ev[-1].peak == 2


def test_hold_return_with_funding_and_delisting():
    close = np.array([[100.0], [110.0], [120.0], [np.nan], [np.nan], [np.nan], [200.0]])
    fund = np.array([[0.0], [0.01], [0.02], [np.nan], [np.nan], [np.nan], [0.0]])
    # Delisted after day 2 (3 missing days): closed at 120, the later 200 is ignored; funding 0.03 paid.
    assert hold_return(close, fund, 0, 0, 6) == pytest.approx(0.20 - 0.03)
    assert hold_return(close, fund, 0, 0, 1) == pytest.approx(0.10 - 0.01)
    assert math.isnan(hold_return(close, fund, 0, 3, 2))


def test_terciles_and_beta():
    strong, weak = terciles({0: -0.1, 1: -0.5, 2: 0.2, 3: -0.3, 4: -0.2, 5: -0.4})
    assert strong == [2, 0] and weak == [5, 1]
    x = np.linspace(-0.05, 0.05, 30)
    assert beta(0.8 * x + 0.001, x) == pytest.approx(0.8)
    assert math.isnan(beta(x[:5], x[:5]))
