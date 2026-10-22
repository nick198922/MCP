"""execution/orders.py: when the demo trader waits, re-quotes, goes to the market or gives up (pure, no I/O)."""

from __future__ import annotations

import pytest

from tradedesk.execution import orders
from tradedesk.execution.guard import TAKER_FEE


def _entry(**kw):
    base = dict(
        side="long",
        planned=100.0,
        stop=98.0,
        target=106.0,
        plan_qty=48.0,
        order_px=99.99,
        order_live=True,
        filled=0.0,
        bid=99.99,
        ask=100.01,
        age_ms=60_000,
        risk_usd=100.0,
        lot_step=0.1,
        min_qty=0.1,
        min_reward_risk=1.5,
    )
    return orders.entry_step(**{**base, **kw})


def test_quotes_join_our_side_of_the_book():
    assert orders.maker_quote("long", 99.0, 101.0) == 99.0 and orders.maker_quote("short", 99.0, 101.0) == 101.0
    assert orders.maker_quote("long", 99.0, 101.0, closing=True) == 101.0  # a long exit sells at the ask
    assert orders.taker_quote("short", 99.0, 101.0, closing=True) == 101.0  # a short exit buys the ask


def test_resting_best_quote_waits_and_a_better_price_still_waits():
    assert _entry().action == "wait"
    assert _entry(bid=99.5, ask=99.52).action == "wait"  # the price came to us: the order fills or stays best


def test_quote_moving_away_is_followed_with_a_smaller_size():
    s = _entry(bid=100.2, ask=100.22)  # +0.11R
    assert s.action == "requote" and s.price == 100.2 and s.qty < 48.0
    assert s.qty * (100.2 - 98 + TAKER_FEE * (100.2 + 98)) <= 100.0  # still $100 at the stop
    assert _entry(bid=100.2, ask=100.22, filled=47.0).qty == 47.0  # never below the filled part


def test_running_away_or_through_the_stop_cancels():
    assert _entry(bid=100.6, ask=100.62).action == "cancel"  # +0.31R
    s = _entry(bid=97.9, ask=97.95, order_live=False)
    assert s.action == "cancel" and "invalidated" in s.reason


def test_timeout_goes_to_the_market_only_while_reward_risk_holds():
    s = _entry(age_ms=orders.ENTRY_WAIT_MS, bid=100.1, ask=100.12, filled=10.0)
    assert s.action == "market" and 0 < s.qty < 48.0 - 10.0 + 1e-9
    assert _entry(order_live=False).action == "market"  # post-only cancelled by the venue, price unchanged
    tight = _entry(age_ms=orders.ENTRY_WAIT_MS, target=103.2)  # R:R after taker fees < 1.5
    assert tight.action == "cancel" and "reward/risk" in tight.reason


def test_short_mirror():
    s = orders.entry_step(
        side="short", planned=100.0, stop=102.0, target=94.0, plan_qty=48.0, order_px=100.01, order_live=True, filled=0.0,
        bid=99.8, ask=99.82, age_ms=60_000, risk_usd=100.0, lot_step=0.1, min_qty=0.1, min_reward_risk=1.5,
    )  # fmt: skip
    assert s.action == "requote" and s.price == 99.82


def test_reward_risk_matches_the_guard():
    from tradedesk.execution.guard import Proposal

    p = Proposal("X/USDT:USDT", "long", 100.0, 98.0, 106.0, 1.0, 5, "S14", "l")
    assert orders.reward_risk_at("long", 100.0, 98.0, 106.0) == pytest.approx(p.reward_risk)


@pytest.mark.parametrize(
    ("kw", "action"),
    [
        (dict(order_px=0.0), "requote"),  # first step: rest a limit at the ask
        (dict(order_px=100.5), "wait"),  # our ask is still best
        (dict(order_px=100.8), "requote"),  # the ask fell under our order
        (dict(order_px=100.5, age_ms=orders.EXIT_WAIT_MS), "market"),
        (dict(order_px=100.5, order_live=False), "market"),  # post-only cancelled
        (dict(order_px=0.0, bid=98.4, ask=98.42), "market"),  # within 0.25R of the stop
    ],
)
def test_time_exit_steps(kw, action):
    base = dict(side="long", entry=100.0, stop=98.0, order_px=0.0, order_live=True, bid=100.48, ask=100.5, age_ms=0)
    s = orders.exit_step(**{**base, **kw})
    assert s.action == action
    if action == "requote":
        assert s.price == 100.5
