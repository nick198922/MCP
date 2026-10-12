from __future__ import annotations

import pytest

from tradedesk.analytics.carry import (
    DAY_MS,
    funding_persistence,
    pearson,
    simulate_cash_and_carry,
    trailing_apr,
)

H8 = 8 * 3_600_000


def _events(rates, spot=100.0, perp=100.0, step=H8):
    return [{"ts_ms": i * step, "rate": r, "spot": spot, "perp": perp} for i, r in enumerate(rates)]


def test_trailing_apr_uses_real_time_not_interval_count():
    # 21 events of 0.0001 over 7 days at 8h -> sum 0.0021 per 7d -> APR 0.0021 * 365 / 7.
    ev = _events([0.0001] * 22)
    assert trailing_apr(ev, 21, 7.0) == pytest.approx(0.0021 * 365 / 7)
    # Same daily income paid hourly gives the same APR.
    ev_h = _events([0.0001 / 8] * (24 * 7 + 1), step=3_600_000)
    assert trailing_apr(ev_h, 24 * 7, 7.0) == pytest.approx(0.0021 * 365 / 7)


def test_always_on_collects_funding_minus_fees():
    ev = _events([0.0001] * 40)
    res = simulate_cash_and_carry(ev, rule="always", lookback_days=1.0, spot_fee=0.001, perp_fee=0.0005)
    (t,) = res["trades"]
    # Opens at the first event >= 1 day (k=3), earns funding at k=4..39 -> 36 payments.
    assert t["funding"] == pytest.approx(36 * 0.0001)
    assert t["fees"] == pytest.approx(2 * 0.0015)
    assert t["basis"] == pytest.approx(0.0)
    assert t["open_at_end"] is True
    assert res["stats"]["total_return"] == pytest.approx(36 * 0.0001 - 0.003)


def test_basis_pnl_is_minus_premium_change():
    ev = _events([0.0] * 10)
    ev[-1]["perp"] = 101.0  # premium widens by 1 % against the short perp
    res = simulate_cash_and_carry(ev, rule="always", lookback_days=1.0, spot_fee=0.0, perp_fee=0.0)
    assert res["stats"]["basis"] == pytest.approx(-0.01)


def test_negative_funding_is_paid_by_the_short():
    ev = _events([-0.0002] * 10)
    res = simulate_cash_and_carry(ev, rule="always", lookback_days=1.0, spot_fee=0.0, perp_fee=0.0)
    assert res["stats"]["funding"] < 0


def test_threshold_rule_enters_and_exits_with_hysteresis():
    rates = [0.0] * 9 + [0.0005] * 12 + [-0.0005] * 12 + [0.0] * 6
    res = simulate_cash_and_carry(_events(rates), rule="threshold", lookback_days=1.0, entry_apr=0.10, exit_apr=0.0)
    assert res["stats"]["n_trades"] == 1
    t = res["trades"][0]
    assert t["open_ts"] == 9 * H8  # first event where trailing 1d APR >= 10 %
    assert t["open_at_end"] is False
    assert res["stats"]["time_in_market"] < 1.0


def test_decisions_ignore_future_events():
    rates = [0.0005] * 20
    a = simulate_cash_and_carry(_events(rates + [0.0] * 10), rule="threshold", lookback_days=1.0)
    b = simulate_cash_and_carry(_events(rates + [0.01] * 10), rule="threshold", lookback_days=1.0)
    assert a["trades"][0]["open_ts"] == b["trades"][0]["open_ts"]


def test_persistence_pairs_and_pearson():
    rates = [0.0001 * (1 + (i // 21) % 3) for i in range(21 * 9)]
    pairs = funding_persistence(_events(rates), lookback_days=7, horizon_days=7, step_days=1)
    assert pairs
    assert all(p[0] > 0 and p[1] > 0 for p in pairs)
    assert pearson([(1, 2), (2, 4), (3, 6)]) == pytest.approx(1.0)
    assert pearson([(1, 1), (1, 1)]) is None


def test_drawdown_tracks_basis_mark_to_market():
    ev = _events([0.0] * 10)
    ev[5]["perp"] = 102.0
    res = simulate_cash_and_carry(ev, rule="always", lookback_days=1.0, spot_fee=0.0, perp_fee=0.0)
    assert res["stats"]["max_drawdown"] == pytest.approx(0.02)
    assert DAY_MS == 86_400_000


def test_carry_snapshot_statuses_and_breakeven():
    from tradedesk.analytics.carry import carry_snapshot

    now = 40 * DAY_MS
    hot = [{"ts_ms": now - i * H8, "rate": 0.0002} for i in range(120)]  # 0.0006/day -> 21.9 % APR
    s = carry_snapshot(hot, now_ms=now, lookback_days=7, entry_apr=0.10, exit_apr=0.0, roundtrip_fee=0.003)
    assert s["trailing_apr"] == pytest.approx(0.0006 * 365)
    assert s["trailing_30d_apr"] == pytest.approx(0.0006 * 365)
    assert s["status"] == "above_entry"
    assert s["positive_share"] == pytest.approx(1.0)
    assert s["breakeven_days"] == pytest.approx(0.003 / 0.0006)
    cold = [{"ts_ms": now - i * H8, "rate": -0.0001} for i in range(30)]
    c = carry_snapshot(cold, now_ms=now)
    assert c["status"] == "below_exit"
    assert c["breakeven_days"] is None
    assert c["trailing_30d_apr"] is None  # only 10 days of history
    mid = [{"ts_ms": now - i * H8, "rate": 0.00003} for i in range(30)]
    assert carry_snapshot(mid, now_ms=now)["status"] == "between"


def test_carry_snapshot_short_history_scales_over_covered_span():
    from tradedesk.analytics.carry import carry_snapshot

    now = 10 * DAY_MS
    rows = [{"ts_ms": now - i * H8, "rate": 0.0001} for i in range(6)]  # 6 payments over ~1.67 days
    s = carry_snapshot(rows, now_ms=now, lookback_days=7)
    assert s["coverage_days"] == pytest.approx(2.0)  # 6 payments x 8h
    assert s["trailing_apr"] == pytest.approx(0.0006 * 365 / 2.0)


def test_carry_snapshot_ignores_future_rows_and_empty():
    from tradedesk.analytics.carry import carry_snapshot

    assert carry_snapshot([], now_ms=0) == {"count": 0}
    rows = [{"ts_ms": DAY_MS * 9, "rate": 0.001}, {"ts_ms": DAY_MS * 11, "rate": 0.5}]
    assert carry_snapshot(rows, now_ms=DAY_MS * 10)["count"] == 1


H4 = 4 * 3_600_000


def _market(rates, price=100.0, perp_high_at=None, high=None, start=0):
    funding = [{"ts_ms": start + i * H8, "rate": r} for i, r in enumerate(rates)]
    n_bars = len(rates) * 2 + 2
    spot = [{"ts": start + i * H4, "close": price} for i in range(n_bars)]
    perp = [{"ts": start + i * H4, "close": price, "high": price} for i in range(n_bars)]
    if perp_high_at is not None:
        perp[perp_high_at]["high"] = high
    return {"funding": funding, "spot": spot, "perp": perp}


def test_portfolio_picks_highest_apr_and_respects_slots():
    from tradedesk.analytics.carry import simulate_carry_portfolio

    mk = {"A": _market([0.0010] * 60), "B": _market([0.0005] * 60), "C": _market([0.0])}
    res = simulate_carry_portfolio(mk, slots=1, lookback_days=1, entry_apr=0.2, exit_apr=0.0, spot_fee=0, perp_fee=0, slippage=0)
    assert {t["symbol"] for t in res["trades"]} == {"A"}
    res2 = simulate_carry_portfolio(mk, slots=2, lookback_days=1, entry_apr=0.2, exit_apr=0.0, spot_fee=0, perp_fee=0, slippage=0)
    assert {t["symbol"] for t in res2["trades"]} == {"A", "B"}


def test_portfolio_income_and_costs_per_slot():
    from tradedesk.analytics.carry import simulate_carry_portfolio

    mk = {"A": _market([0.001] * 30)}
    res = simulate_carry_portfolio(mk, slots=2, lookback_days=1, entry_apr=0.2, spot_fee=0.001, perp_fee=0.0005, slippage=0.0005)
    (t,) = res["trades"]
    # Opens 5 min after the first grid point once 1 day of history exists, then earns every later payment.
    paid = sum(1 for e in mk["A"]["funding"] if e["ts_ms"] > t["open_ts"])
    assert t["funding"] == pytest.approx(0.001 * paid)
    assert t["fees"] == pytest.approx(2 * (0.001 + 0.0005 + 2 * 0.0005))
    assert res["stats"]["total_return"] == pytest.approx(0.5 * (t["funding"] - t["fees"]))


def test_portfolio_records_runup_and_exits_on_rule():
    from tradedesk.analytics.carry import simulate_carry_portfolio

    rates = [0.001] * 12 + [-0.001] * 12 + [0.0] * 6
    mk = {"A": _market(rates, perp_high_at=16, high=150.0)}
    res = simulate_carry_portfolio(mk, slots=1, lookback_days=1, entry_apr=0.2, exit_apr=0.0, spot_fee=0, perp_fee=0, slippage=0)
    t = res["trades"][0]
    assert t["open_at_end"] is False
    assert t["max_runup"] == pytest.approx(0.5)


def test_portfolio_ignores_future_data():
    from tradedesk.analytics.carry import simulate_carry_portfolio

    base = [0.001] * 20
    a = simulate_carry_portfolio({"A": _market(base + [0.0] * 20), "B": _market([0.0005] * 40)}, slots=1, lookback_days=1)
    b = simulate_carry_portfolio({"A": _market(base + [0.0] * 20), "B": _market([0.0005] * 20 + [0.05] * 20)}, slots=1, lookback_days=1)
    assert a["trades"][0]["open_ts"] == b["trades"][0]["open_ts"]
    assert a["trades"][0]["symbol"] == b["trades"][0]["symbol"] == "A"
