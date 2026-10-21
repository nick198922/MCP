"""execution/guard.py: limits before every demo order (pure, no network)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tradedesk.execution.guard import DEFAULT, GuardConfig, Proposal, State, check, day_start_ms, open_risk_usd, size_for_risk

NOW = 1_792_600_000_000
P = Proposal("BTC/USDT:USDT", "long", entry=100_000.0, stop=98_000.0, target=104_000.0, qty=0.05, setup="S14", link_id="td-S14-1")
S = State(now_ms=NOW, equity_usd=50_000.0)


def test_sizing_risks_at_most_the_budget_and_floors_to_the_lot_step():
    assert size_for_risk(100_000, 98_000, 100, 0.001, 0.001) == 0.05  # 0.05 * 2000 = 100 USD
    assert size_for_risk(100_000, 97_000, 100, 0.001, 0.001) == 0.033  # 0.0333.. floored, never above the budget
    assert size_for_risk(1.0, 0.9, 100, 1, 1) == 1000.0
    assert size_for_risk(100_000, 50_000, 100, 0.001, 0.01) == 0.0  # 0.002 < the minimum lot 0.01 -> no trade
    assert size_for_risk(100.0, 100.0, 100, 0.01, 0.01) == 0.0  # zero stop distance


def test_a_sane_proposal_passes():
    assert check(P, S) == []


@pytest.mark.parametrize(
    ("p", "s", "needle"),
    [
        (P, replace(S, halted=True), "kill-switch"),
        (replace(P, symbol="PEPE/USDT:USDT"), S, "allow-list"),
        (replace(P, stop=101_000.0), S, "opposite sides"),
        (replace(P, target=99_000.0), S, "opposite sides"),
        (replace(P, stop=85_000.0, qty=0.006), S, "from the entry"),
        (replace(P, target=102_000.0), S, "reward/risk"),
        (replace(P, qty=0.0), S, "size is zero"),
        (replace(P, qty=0.06), S, "risk 120.00 USD"),
        (replace(P, qty=0.2, stop=99_900.0, target=100_400.0), S, "notional"),
        (P, replace(S, equity_usd=500.0), "leverage"),
        (P, replace(S, open_symbols=("BTC/USDT:USDT",)), "already in"),
        (P, replace(S, open_symbols=("a", "b", "c", "d", "e")), "open positions = max"),
        (P, replace(S, orders_last_hour=6), "orders in the last hour"),
        (P, replace(S, day_loss_usd=250.0), "daily loss"),
        (P, replace(S, sent_link_ids=("td-S14-1",)), "already sent"),
    ],
)
def test_each_limit_refuses(p, s, needle):
    assert any(needle in v for v in check(p, s)), check(p, s)


def test_only_testnet_mode_exists():
    assert any("not allowed" in v for v in check(P, S, GuardConfig(mode="live")))
    assert DEFAULT.mode == "testnet" and DEFAULT.venue == "bybit-demo"


def test_short_levels_mirror():
    short = replace(P, side="short", stop=102_000.0, target=96_000.0)
    assert check(short, S) == []


def test_helpers():
    assert day_start_ms(NOW) % 86_400_000 == 0 and NOW - day_start_ms(NOW) < 86_400_000
    assert open_risk_usd([(0.05, 100_000, 98_000), (-10, 2.0, 2.1)]) == pytest.approx(101.0)
