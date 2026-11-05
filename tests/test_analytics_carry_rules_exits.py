"""Direct tests of the carry exit rules and the basis mark (analytics/carry_rules.py; item 105 of the
standing-improvements card): ``carry_basis``, ``carry_exit_due``, ``spread_stop_hit``, ``spread_exit_due``.
They feed the live carry forward through ``carry_step`` / ``spread_step`` and were only tested indirectly.
Expected values are worked out by hand in the comments (analytical references); threshold values are dyadic
so boundary comparisons are exact in floats."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.carry_rules import carry_basis, carry_exit_due, carry_open, spread_exit_due, spread_open, spread_stop_hit

# ------------------------------------------------------------------------------------- carry_basis


def test_carry_basis_fresh_position_is_zero():
    # prices at entry: both moves are 0, even though the perp opened at a premium
    assert carry_basis(carry_open("X", 0, 0.3, 100.0, 101.0, 0.002)) == 0.0


@pytest.mark.parametrize(
    ("spot_last", "perp_last", "expected"),
    [
        # premium widened: ((110 - 100) - (112 - 101)) / 100 = (10 - 11) / 100 = -0.01 (short perp loses more)
        (110.0, 112.0, -0.01),
        # premium narrowed on a fall: ((90 - 100) - (90.5 - 101)) / 100 = (-10 + 10.5) / 100 = +0.005
        (90.0, 90.5, 0.005),
        # equal moves on both legs: (5 - 5) / 100 = 0, the entry premium itself is not counted
        (105.0, 106.0, 0.0),
    ],
)
def test_carry_basis_is_spot_move_minus_perp_move_over_spot_entry(spot_last, perp_last, expected):
    p = carry_open("X", 0, 0.3, 100.0, 101.0, 0.002)
    p.update(spot_last=spot_last, perp_last=perp_last)
    assert carry_basis(p) == pytest.approx(expected, abs=1e-12)


def test_carry_basis_normalised_by_spot_entry_not_last():
    p = carry_open("X", 0, 0.3, 200.0, 202.0, 0.002)
    p.update(spot_last=400.0, perp_last=398.0)
    # ((400 - 200) - (398 - 202)) / 200 = (200 - 196) / 200 = 0.02 (dividing by the last spot would give 0.01)
    assert carry_basis(p) == pytest.approx(0.02, abs=1e-12)


# ------------------------------------------------------------------------------------- carry_exit_due


@pytest.mark.parametrize(
    ("apr", "exit_apr", "due"),
    [
        (0.125, 0.125, True),  # exactly at the exit threshold: closes (<=)
        (math.nextafter(0.125, 1.0), 0.125, False),  # one ulp above: holds
        (math.nextafter(0.125, 0.0), 0.125, True),  # one ulp below: closes
        (0.25, 0.125, False),
        (-0.5, 0.125, True),  # negative funding (short perp pays): closes
        (0.0, 0.0, True),  # zero APR is a value, not "missing"
        (-0.25, -0.125, True),  # negative exit threshold: still <=
        (-0.0625, -0.125, False),
    ],
)
def test_carry_exit_due_threshold(apr, exit_apr, due):
    assert carry_exit_due(apr, exit_apr) is due


def test_carry_exit_due_missing_apr_holds():
    # no trailing APR (data gap) never closes, whatever the threshold
    assert carry_exit_due(None, 0.125) is False
    assert carry_exit_due(None, 10.0) is False


# ------------------------------------------------------------------------------------- spread_stop_hit


def _spread(d: int = 1, max_up: float = 0.0, max_down: float = 0.0) -> dict:
    p = spread_open("Y", 0, d, 0.3, 100.0, 50.0, 0.001)
    p.update(max_up=max_up, max_down=max_down)
    return p


@pytest.mark.parametrize(
    ("max_up", "max_down", "hit"),
    [
        (0.125, 0.0, True),  # up-move exactly at the stop: hit (>=)
        (0.0, 0.125, True),  # down-move exactly at the stop: hit
        (0.0625, 0.09375, False),  # both below the stop
        (0.0625, 0.25, True),  # the larger of the two decides
        (math.nextafter(0.125, 0.0), 0.0, False),  # one ulp below: no stop
    ],
)
@pytest.mark.parametrize("d", [1, -1])
def test_spread_stop_hit_largest_leg_excursion_vs_stop(max_up, max_down, hit, d):
    # the stop does not depend on the direction of the position
    assert spread_stop_hit(_spread(d, max_up, max_down), 0.125) is hit


def test_spread_stop_hit_disabled_when_stop_is_none():
    assert spread_stop_hit(_spread(1, 5.0, 0.99), None) is False


def test_spread_stop_hit_fresh_position_not_stopped():
    assert spread_stop_hit(_spread(1), 0.125) is False


# ------------------------------------------------------------------------------------- spread_exit_due


@pytest.mark.parametrize(
    ("d", "ra", "rb", "exit_apr", "due"),
    [
        # d=+1 (long A / short B): held spread = rb - ra
        (1, 0.125, 0.25, 0.125, True),  # 0.125 <= 0.125: closes at the boundary
        (1, 0.125, 0.375, 0.125, False),  # 0.25 > 0.125: holds
        (1, 0.5, 0.25, 0.0, True),  # spread flipped against the position: -0.25 <= 0
        # d=-1 (short A / long B): held spread = ra - rb
        (-1, 0.5, 0.25, 0.25, True),  # 0.25 <= 0.25
        (-1, 0.5, 0.25, 0.125, False),  # 0.25 > 0.125
        (-1, 0.125, 0.5, 0.0, True),  # flipped: -0.375 <= 0
        # the same rates read with the opposite direction give the opposite sign
        (1, 0.5, 0.25, 0.125, True),  # -0.25 <= 0.125
        (1, 0.0, 0.0, 0.0, True),  # zero rates are values, not "missing": 0 <= 0
    ],
)
def test_spread_exit_due_held_spread_vs_threshold(d, ra, rb, exit_apr, due):
    assert spread_exit_due(_spread(d), ra, rb, exit_apr) is due


@pytest.mark.parametrize(("ra", "rb"), [(None, 0.25), (0.25, None), (None, None)])
@pytest.mark.parametrize("d", [1, -1])
def test_spread_exit_due_missing_rate_holds(ra, rb, d):
    # a missing APR on either leg never closes, even with a threshold every value would pass
    assert spread_exit_due(_spread(d), ra, rb, 10.0) is False
