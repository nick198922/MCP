from __future__ import annotations

import pytest

from tradedesk.analytics.carry_bybit_inventory import (
    DAY_MS,
    STEP_MS,
    active_days,
    cycles,
    first_entry_cycle,
    price_level_dev,
    slot_estimate,
    trailing_apr,
    unit_inventory,
)

T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC


def pays(lo_day: int, hi_day: int, rate: float, step: int = STEP_MS) -> list[tuple[int, float]]:
    return [(t, rate) for t in range(T0 + lo_day * DAY_MS, T0 + hi_day * DAY_MS, step)]


def bars(lo_day: int, hi_day: int, close: float = 1.0, vol: float = 10.0) -> list[tuple[int, float, float]]:
    return [(T0 + d * DAY_MS, close, vol) for d in range(lo_day, hi_day)]


def test_trailing_apr_analytic() -> None:
    p = pays(-30, 30, 0.0001)  # 3 payments a day of 1 bp
    # (t - 7d; t] at a payment time holds 21 payments: 21e-4 * 365 / 7
    assert trailing_apr(p, T0) == pytest.approx(21e-4 * 365 / 7)
    assert trailing_apr(p, T0 - 25 * DAY_MS) is None  # no full 7-day history
    assert trailing_apr([], T0) is None


def test_hourly_funding_sums_to_same_apr() -> None:
    p8 = pays(-30, 1, 0.0003)
    p1 = pays(-30, 1, 0.0003 / 8, step=3_600_000)
    assert trailing_apr(p1, T0) == pytest.approx(trailing_apr(p8, T0))


def test_active_days_drops_zero_volume_and_cycles() -> None:
    d = active_days([(T0, 1.0, 0.0), (T0 + DAY_MS, 2.0, 5.0)])
    assert d == {T0 + DAY_MS: 2.0}
    assert cycles(T0 + 1, T0 + DAY_MS) == [T0 + STEP_MS, T0 + 2 * STEP_MS, T0 + DAY_MS]


def test_price_level_dev_with_multiplier() -> None:
    perp = {T0: 1000 * 0.0011, T0 + DAY_MS: 1000 * 0.0010}
    spot = {T0: 0.0011, T0 + DAY_MS: 0.0010}
    assert price_level_dev(perp, spot, 1000, T0, T0 + DAY_MS) == pytest.approx(0.0)
    assert price_level_dev(perp, spot, 1, T0, T0 + DAY_MS) == pytest.approx(999.0)
    assert price_level_dev(perp, {}, 1, T0, T0) is None


def test_first_entry_needs_both_legs_and_threshold() -> None:
    p = pays(-30, 30, 0.0003)  # APR = 63e-4 * 365/7 = 0.3285 >= 0.20
    perp = active_days(bars(-30, 30))
    spot = active_days(bars(-30, 2))  # spot gone after day 1
    assert first_entry_cycle(p, T0, T0 + 10 * DAY_MS, perp, spot) == T0
    assert first_entry_cycle(p, T0 + 3 * DAY_MS, T0 + 10 * DAY_MS, perp, spot) is None
    assert first_entry_cycle(pays(-30, 30, 0.0001), T0, T0 + 10 * DAY_MS, perp, spot) is None


def test_unit_inventory_spot_states() -> None:
    perp = bars(-200, 20)
    hot = pays(-200, 20, 0.0003)
    u = unit_inventory(T0, perp_bars=perp, spot_bars=bars(-200, 20), funding=hot)
    assert u["spot"] == "yes" and u["hot"] is True and u["entry_after"] == T0
    assert u["first_leg_gone"] == "same_day"
    assert unit_inventory(T0, perp_bars=perp, spot_bars=None, funding=hot)["spot"] == "unmeasured"
    assert unit_inventory(T0, perp_bars=perp, spot_bars=[], funding=hot)["spot"] == "unmeasured"
    assert unit_inventory(T0, perp_bars=perp, spot_bars=[], funding=hot, spot_empty_means_absent=True)["spot"] == "no"
    coll = unit_inventory(T0, perp_bars=perp, spot_bars=bars(-200, 20, close=2.0), funding=hot)
    assert coll["spot"] == "collision" and coll["hot"] is None


def test_unit_inventory_leg_order_and_cold() -> None:
    u = unit_inventory(T0, perp_bars=bars(-200, 3), spot_bars=bars(-200, 20), funding=pays(-200, 3, 0.00005))
    assert u["first_leg_gone"] == "perp" and u["hot"] is False and u["entry_after"] is None
    assert u["perp_end"] == T0 + 2 * DAY_MS


def test_unit_inventory_is_causal_before_announcement() -> None:
    """Counters known at the announcement (spot, hot, max APR before) ignore data after it."""
    base = pays(-200, 0, 0.0001)  # APR 0.104: cold before the announcement
    a = unit_inventory(T0, perp_bars=bars(-200, 20), spot_bars=bars(-200, 20), funding=base + pays(0, 20, 0.0001))
    b = unit_inventory(T0, perp_bars=bars(-200, 20), spot_bars=bars(-200, 20), funding=base + pays(0, 20, 0.01))
    for k in ("spot", "hot", "max_apr_before", "level_dev"):
        assert a[k] == b[k]
    assert a["entry_after"] is None and b["entry_after"] is not None


def test_unit_inventory_ignores_bars_after_announcement_day() -> None:
    """Announcement mid-day: bars of later days (prices, volumes, gaps) do not move the counters known at t_a.

    The bar of the day containing t_a is still used (daily granularity: part of it lies after t_a).
    """
    t_a = T0 + DAY_MS // 2
    f = pays(-200, 20, 0.0003)  # APR 0.33: hot before the announcement
    a = unit_inventory(t_a, perp_bars=bars(-200, 20), spot_bars=bars(-200, 20), funding=f)
    b = unit_inventory(t_a, perp_bars=bars(-200, 1) + bars(1, 20, close=3.0), spot_bars=bars(-200, 1) + bars(5, 20, vol=0.0), funding=f)
    for k in ("spot", "hot", "max_apr_before", "level_dev"):
        assert a[k] == b[k]
    assert a["spot"] == "yes" and a["hot"] is True
    # The announcement-day bar (open T0 <= t_a) does count: only it near t_a keeps the spot "yes".
    c = unit_inventory(t_a, perp_bars=bars(-200, 20), spot_bars=bars(-200, -8) + bars(0, 1), funding=f)
    assert c["spot"] == "yes"


def test_entry_after_is_causal_in_funding() -> None:
    """A huge payment after the first entry cycle does not move it."""
    p = pays(-30, 30, 0.0003)
    perp = spot = active_days(bars(-30, 30))
    t = first_entry_cycle(p, T0, T0 + 10 * DAY_MS, perp, spot)
    q = [(ts, r * (100 if ts > t else 1)) for ts, r in p]
    assert first_entry_cycle(q, T0, T0 + 10 * DAY_MS, perp, spot) == t


def test_slot_estimate_ratios() -> None:
    e = slot_estimate(16, 26)
    assert e == {"ban_events": 2.0, "removed_trades": 10.0, "iii_events": 3.0}
