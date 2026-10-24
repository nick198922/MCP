"""Pure parts of scripts/research/vault_sizing.py: leverage paths, mechanical rebalancing, look-ahead-free equity."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "research"))
import vault_sizing as vs  # noqa: E402

DAY = vs.DAY_MS
T0 = 1_760_000_000_000 // DAY * DAY  # a UTC midnight


def test_nav_path_compounds_and_floors_at_zero():
    nav = vs.nav_path(np.array([2.0, 2.0, 3.0]), np.array([0.10, -0.05, -0.50]))
    assert nav[0] == pytest.approx(1.2)
    assert nav[1] == pytest.approx(1.2 * 0.9)
    assert nav[2] == 0.0  # 3x on −50 % wipes the account out and it stays out


def test_buy_and_hold_equals_levered_price_move():
    # Hold 2x from day 0 without re-levering: equity = 1 + 2 × (P_end / P_0 − 1).
    ret = np.array([0.10, -0.20, 0.05])
    nav = vs.rebalanced_nav(2.0, ret, every=10**9)
    assert nav[-1] == pytest.approx(1 + 2 * (np.prod(1 + ret) - 1))


def test_daily_rebalance_matches_constant_path():
    ret = np.array([0.03, -0.04, 0.02, 0.01])
    assert vs.rebalanced_nav(3.0, ret, every=1) == pytest.approx(vs.nav_path(np.full(4, 3.0), ret))


def test_stats_on_known_path():
    s = vs.stats(np.array([1.5, 0.75, 1.2]))
    assert s["ret"] == pytest.approx(0.2)
    assert s["dd"] == pytest.approx(0.5)
    assert s["calmar"] == pytest.approx(0.4)


def test_shift_percentile_ranks_a_lucky_path_at_the_top():
    # Leverage high exactly on the up days: no circular shift does better.
    ret = np.array([0.05, -0.05] * 10)
    lev = np.array([3.0, 1.0] * 10)
    actual = vs.stats(vs.nav_path(lev, ret))["ret"]
    pct, vals = vs.shift_percentile(lev, ret, actual, "ret")
    assert len(vals) == 19
    assert pct > 0.7


def test_ledger_flows_signs():
    ledger = [
        {"time": T0 + 5, "delta": {"type": "vaultDeposit", "usdc": "100"}},
        {"time": T0 + 1, "delta": {"type": "vaultCreate", "usdc": "50"}},
        {"time": T0 + 9, "delta": {"type": "vaultWithdraw", "netWithdrawnUsd": "30", "requestedUsd": "31"}},
        {"time": T0 + 9, "delta": {"type": "accountClassTransfer", "usdc": "999"}},
    ]
    assert vs.ledger_flows(ledger) == [(T0 + 1, 50.0), (T0 + 5, 100.0), (T0 + 9, -30.0)]


def test_anchored_equity_uses_no_future_information():
    av = [(T0 + 23 * 3_600_000, 1000.0), (T0 + 7 * DAY, 5000.0)]  # the second official point lies in the future
    day_pnl = {T0 + DAY: 10.0, T0 + 2 * DAY: 20.0, T0 + 3 * DAY: 40.0}
    flows = [(T0 + 2 * DAY + 5, 100.0), (T0 + 3 * DAY + 5, -60.0)]
    eq = vs.anchored_equity(av, flows, day_pnl, [T0 + DAY, T0 + 2 * DAY, T0 + 3 * DAY])
    assert eq[T0 + DAY] == pytest.approx(1000.0)
    assert eq[T0 + 2 * DAY] == pytest.approx(1000 + 10 + 50)  # half of the same-day deposit
    assert eq[T0 + 3 * DAY] == pytest.approx(1000 + 10 + 20 + 100 - 30)
    assert T0 not in vs.anchored_equity(av, flows, day_pnl, [T0])  # before the first official point
