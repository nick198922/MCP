"""execution/guard.py: limits before every demo order (pure, no network)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tradedesk.execution import guard as g
from tradedesk.execution.guard import DEFAULT, GuardConfig, State, check, plan_order

NOW = 1_792_600_000_000
BTC = "BTC/USDT:USDT"
UNI = (BTC, "ETH/USDT:USDT", "SOL/USDT:USDT", "WIF/USDT:USDT")
P = plan_order(BTC, "long", 100_000.0, 98_000.0, 105_000.0, "S14", "td-S14-1", 0.001, 0.001)
S = State(now_ms=NOW, universe=UNI, equity_usd=10_000.0)


def test_sizing_includes_both_taker_fees_and_floors_to_the_lot_step():
    q = g.size_for_risk(100_000, 98_000, 100, 0.001, 0.001)
    per_unit = 2000 + g.TAKER_FEE * 198_000
    assert q == 0.047 and q * per_unit <= 100 < (q + 0.001) * per_unit
    assert g.size_for_risk(100.0, 99.5, 100, 1, 1, max_notional=10_000) == 100.0  # 164 by risk, shrunk to the cap
    assert g.size_for_risk(100_000, 50_000, 100, 0.001, 0.01) == 0.0  # below the minimum lot
    assert g.size_for_risk(100.0, 100.0, 100, 0.01, 0.01) > 0  # fees alone still bound the size
    assert P.risk_usd <= 100.0


def test_leverage_tiers_capped_by_the_venue():
    tiers = tuple(g.tier_leverage(s) for s in (BTC, "ETH/USDT:USDT", "SOL/USDT:USDT", "WIF/USDT:USDT"))
    assert tiers == (20, 20, 10, 5)
    assert P.leverage == 20
    wide = plan_order(BTC, "long", 100_000.0, 95_500.0, 115_000.0, "S14", "x", 0.001, 0.001)
    assert wide.leverage == 20  # cross margin: a wide stop does not lower the leverage, the account limit guards it
    alt = plan_order("WIF/USDT:USDT", "short", 2.0, 2.1, 1.7, "S3", "y", 1, 1)
    assert alt.leverage == 5 and alt.margin == pytest.approx(alt.notional / 5)
    capped = plan_order("WIF/USDT:USDT", "short", 2.0, 2.1, 1.7, "S3", "y", 1, 1, venue_max_leverage=3)
    assert capped.leverage == 3  # the venue allows only 3x on this coin


def test_size_stays_inside_the_first_risk_tier():
    p = plan_order(BTC, "long", 100_000.0, 99_900.0, 100_500.0, "S14", "x", 0.001, 0.001, mmr=0.005, tier_limit_usd=20_000)
    assert p.notional <= 20_000 and p.maintenance == pytest.approx(p.notional * 0.005)


def test_size_caps_tied_to_equity():
    tight = plan_order(BTC, "long", 100_000.0, 99_800.0, 101_000.0, "S14", "x", 0.001, 0.001, equity_usd=10_000.0)
    assert tight.notional <= 10_000.0  # 0.2 % stop: $100 risk would be ~$38k, shrunk to 1x equity
    assert any("1x equity" in v for v in check(replace(tight, qty=0.2), S))
    assert any("open size" in v for v in check(P, replace(S, open_notional_usd=56_000.0)))
    assert not any("open size" in v for v in check(P, replace(S, open_notional_usd=50_000.0)))


def test_cross_margin_account_limit():
    # maintenance of the open positions + the loss at all stops must stay within half of the equity
    assert check(P, replace(S, open_maint_usd=4_000.0, open_risk_usd=900.0)) != []
    assert any("cross margin" in v for v in check(P, replace(S, open_maint_usd=4_000.0, open_risk_usd=900.0)))
    assert not any("cross margin" in v for v in check(P, replace(S, open_maint_usd=1_000.0, open_risk_usd=900.0)))


def test_a_sane_proposal_passes():
    assert check(P, S) == []


@pytest.mark.parametrize(
    ("p", "s", "needle"),
    [
        (P, replace(S, halted=True), "kill-switch"),
        (replace(P, symbol="PEPE/USDT:USDT"), S, "universe"),
        (replace(P, symbol="USDC/USDT:USDT"), replace(S, universe=(*UNI, "USDC/USDT:USDT")), "universe"),
        (replace(P, stop=101_000.0), S, "opposite sides"),
        (replace(P, target=99_000.0), S, "opposite sides"),
        (replace(P, stop=85_000.0, qty=0.006, leverage=1), S, "from the entry"),
        (replace(P, target=102_500.0), S, "reward/risk"),
        (replace(P, qty=0.0), S, "size is zero"),
        (replace(P, qty=0.06), S, "risk"),
        (replace(P, qty=0.6, stop=99_950.0, target=100_500.0), S, "notional"),
        (replace(P, leverage=25), S, "leverage 25x"),
        (replace(P, leverage=0), S, "leverage 0x"),
        (P, replace(S, equity_usd=0.0), "equity unknown"),
        (P, replace(S, open_margin_usd=8_900.0), "margin"),
        (P, replace(S, open_symbols=(BTC,)), "already in"),
        (P, replace(S, open_symbols=tuple(f"s{i}" for i in range(DEFAULT.max_open_positions))), "open positions = max"),
        (P, replace(S, trades_today=DEFAULT.max_trades_per_day), "trades at risk today = max"),
        (P, replace(S, entries_today=DEFAULT.max_entries_per_day), "entries today = max"),
        (P, replace(S, orders_last_hour=DEFAULT.orders_per_hour), "orders in the last hour"),
        (P, replace(S, open_risk_usd=DEFAULT.max_open_risk_usd - 50.0), "open risk"),
        (P, replace(S, day_realised_loss_usd=DEFAULT.daily_max_loss_usd), "realised loss today"),
        (P, replace(S, seen_link_ids=("td-S14-1",)), "already decided"),
    ],
)
def test_each_limit_refuses(p, s, needle):
    assert any(needle in v for v in check(p, s)), check(p, s)


def test_only_testnet_mode_exists():
    assert any("not allowed" in v for v in check(P, S, GuardConfig(mode="live")))
    assert DEFAULT.mode == g.MODE == "testnet" and DEFAULT.venue == "bybit-demo" and DEFAULT.max_trades_per_day == 19


def test_short_levels_mirror():
    short = plan_order(BTC, "short", 100_000.0, 102_000.0, 95_000.0, "S14", "s", 0.001, 0.001)
    assert check(short, S) == []


def test_path_to_target():
    assert g.path_to_target("long", 100.0, 106.0, 103.0) == pytest.approx(0.5)
    assert g.path_to_target("short", 100.0, 94.0, 97.0) == pytest.approx(0.5)
    assert g.path_to_target("long", 100.0, 106.0, 99.0) < 0
    assert g.path_to_target("long", 100.0, 100.0, 101.0) == 0.0  # no way to go: never triggers


def test_breakeven_and_roe():
    assert g.breakeven_stop("long", 100.0) == pytest.approx(100.11) and g.breakeven_stop("short", 100.0) == pytest.approx(99.89)
    assert g.breakeven_stop("long", 100.0, 0.5) == 100.5 and g.breakeven_stop("short", 100.0, 0.5) == 99.5  # away from entry
    assert g.breakeven_stop("long", 1.0, 0.0001) == pytest.approx(1.0011)
    assert g.roe("long", 100.0, 101.5, 20) == pytest.approx(0.30)  # 1.5 % at 20x = +30 % ROE
    assert g.roe("short", 100.0, 94.0, 5) == pytest.approx(0.30)  # 6 % at 5x
    assert g.roe("long", 100.0, 99.0, 10) < 0


def test_universe_top_crypto_by_turnover():
    rows = [
        ("BTC/USDT:USDT", 5e9, ""),
        ("USDC/USDT:USDT", 9e9, ""),
        ("THIN/USDT:USDT", 1e6, ""),
        ("ETH/USDT:USDT", 3e9, ""),
        ("BTC/USDC:USDC", 8e9, ""),
        ("BTC/USDT:USDT-261226", 7e9, ""),
        ("NVDA/USDT:USDT", 6e9, "stock"),
        ("SOXL/USDT:USDT", 6e9, "ETF"),
        ("XAU/USDT:USDT", 6e9, "commodity"),
        ("NEW/USDT:USDT", 6e8, "innovation"),
        ("ODD/USDT:USDT", 6e8, "?"),
    ]
    rows += [(f"A{i}/USDT:USDT", 2e8 - i, "") for i in range(150)]
    u = g.universe(rows)
    assert u[:3] == ("BTC/USDT:USDT", "ETH/USDT:USDT", "NEW/USDT:USDT") and len(u) == 100
    bad = (
        "USDC/USDT:USDT",
        "THIN/USDT:USDT",
        "BTC/USDC:USDC",
        "BTC/USDT:USDT-261226",
        "NVDA/USDT:USDT",
        "SOXL/USDT:USDT",
        "XAU/USDT:USDT",
        "ODD/USDT:USDT",
    )
    assert not any(x in u for x in bad)


def test_helpers():
    assert g.day_start_ms(NOW) % 86_400_000 == 0 and NOW - g.day_start_ms(NOW) < 86_400_000
    assert g.open_risk_usd([(0.05, 100_000, 98_000)]) == pytest.approx(0.05 * (2000 + g.TAKER_FEE * 198_000))


# ---------------------------------------------------------------- options (slice 1)
# numbers from tests/fixtures/bybit_demo_options_btc_2026-10-23.json (Bybit demo, 2026-10-23 15:58 UTC)
OPT_NOW = 1_792_771_111_675
OPT = "BTC-6NOV26-96000-C-USDT"
OPT_EXPIRY = 1_793_952_000_000  # deliveryTime: 2026-11-06 08:00 UTC
INDEX = 81_704.14
OP = g.plan_option(OPT, 65.0, OPT_EXPIRY, 65.0, 70.0, INDEX, "probe", "tdo-1", 0.01, 0.01)
OS = State(now_ms=OPT_NOW, equity_usd=10_000.0)


def test_option_symbol_parsing():
    s = g.parse_option_symbol(OPT)
    assert s is not None and (s.base, s.strike, s.is_call, s.settle) == ("BTC", 96_000.0, True, "USDT")
    assert s.expiry_ms == OPT_EXPIRY  # 08:00 UTC, same as the venue's deliveryTime
    p = g.parse_option_symbol("ETH-30OCT26-3000-P")
    assert p is not None and not p.is_call and p.settle == "" and p.expiry_ms == 1_793_347_200_000
    for bad in (
        "BTCUSDT",
        "BTC/USDT:USDT",
        "BTC-31FEB26-90000-C-USDT",
        "BTC-6XYZ26-90000-C-USDT",
        "BTC-6NOV26-x-C-USDT",
        "BTC-6NOV26-90000-F",
    ):
        assert g.parse_option_symbol(bad) is None, bad


def test_option_sizing_premium_plus_fee_is_the_risk():
    # cheap option: the fee is 7 % of the premium (below 0.03 % of the index)
    assert g.option_fee(65.0, INDEX) == pytest.approx(0.07 * 65.0)
    assert OP.qty == 1.43 and OP.risk_usd == pytest.approx(1.43 * 65.0 * 1.07) and OP.risk_usd <= 100.0
    assert (OP.qty + 0.01) * 65.0 * 1.07 > 100.0  # floored: one more lot would break the cap
    # near the money: the fee is 0.03 % of the index
    assert g.option_fee(1_500.0, INDEX) == pytest.approx(0.0003 * INDEX)
    assert g.size_option(1_500.0, INDEX, 100.0, 0.01, 0.01) == 0.06
    assert g.size_option(20_000.0, INDEX, 100.0, 0.01, 0.01) == 0.0  # one minimum lot already costs more than $100
    assert g.size_option(0.0, INDEX, 100.0, 0.01, 0.01) == 0.0
    # no index: the premium cap alone, still an upper bound of the fee
    assert g.option_fee(1_500.0, 0.0) == g.option_fee(1_500.0, float("nan")) == pytest.approx(0.07 * 1_500.0)
    assert OP.side == "buy" and OP.spread == pytest.approx(5 / 67.5)


def test_a_sane_option_proposal_passes():
    assert g.check_option(OP, OS) == []
    assert g.check_option(replace(OP, symbol="ETH-6NOV26-4000-C-USDT"), OS) == []  # ETH is allowed too
    assert g.check_option(OP, replace(OS, now_ms=OPT_EXPIRY - 4 * g.DAY_MS)) == []  # exactly 4 days: allowed


@pytest.mark.parametrize(
    ("p", "s", "needle"),
    [
        (replace(OP, side="sell"), OS, "only buying options to open"),
        (replace(OP, side="short"), OS, "only buying options to open"),
        (replace(OP, symbol="SOL-6NOV26-200-C-USDT"), OS, "underlying SOL not allowed"),
        (replace(OP, symbol="BTC-6NOV26-96000-C"), OS, "only USDT-settled"),
        (replace(OP, symbol="BTCUSDT"), OS, "not an option symbol"),
        (OP, replace(OS, now_ms=OPT_EXPIRY - int(3.9 * g.DAY_MS)), "no option buys with 1-3 DTE"),
        (OP, replace(OS, now_ms=OPT_EXPIRY - g.DAY_MS), "no option buys with 1-3 DTE"),
        (OP, replace(OS, now_ms=OPT_EXPIRY + 1), "no option buys with 1-3 DTE"),
        # the date in the symbol is earlier than the delivery time given: the earlier one counts
        (replace(OP, symbol="BTC-24OCT26-96000-C-USDT"), OS, "no option buys with 1-3 DTE"),
        (replace(OP, bid=0.0), OS, "no two-sided quote"),
        (replace(OP, bid=75.0), OS, "no two-sided quote"),
        (replace(OP, bid=15.0, ask=45.0, price=15.0), OS, "spread 100.0% of mid > 10%"),
        (replace(OP, price=70.0), OS, "would take liquidity"),
        (replace(OP, price=0.0), OS, "limit price must be positive"),
        (replace(OP, qty=0.0), OS, "size is zero"),
        (replace(OP, qty=2.0), OS, "premium + fee"),
        (replace(OP, qty=float("nan")), OS, "size is zero"),
        (replace(OP, price=float("nan")), OS, "limit price must be positive"),
        (OP, replace(OS, equity_usd=0.0), "equity unknown"),
        (OP, replace(OS, equity_usd=150.0), "cross margin"),
        (OP, replace(OS, halted=True), "kill-switch"),
        (OP, replace(OS, open_symbols=(OPT,)), "already in"),
        (OP, replace(OS, open_symbols=tuple(f"s{i}" for i in range(DEFAULT.max_open_positions))), "open positions = max"),
        # the daily caps are shared with the futures entries (same State fields, same messages)
        (OP, replace(OS, trades_today=DEFAULT.max_trades_per_day), "trades at risk today = max"),
        (OP, replace(OS, entries_today=DEFAULT.max_entries_per_day), "entries today = max"),
        (OP, replace(OS, orders_last_hour=DEFAULT.orders_per_hour), "orders in the last hour"),
        (OP, replace(OS, open_risk_usd=DEFAULT.max_open_risk_usd - 50.0), "open risk"),
        (OP, replace(OS, day_realised_loss_usd=DEFAULT.daily_max_loss_usd), "realised loss today"),
        (OP, replace(OS, seen_link_ids=("tdo-1",)), "already decided"),
    ],
)
def test_each_option_limit_refuses(p, s, needle):
    assert any(needle in v for v in g.check_option(p, s)), g.check_option(p, s)


def test_option_guard_only_in_testnet_mode_and_config_defaults():
    assert any("not allowed" in v for v in g.check_option(OP, OS, GuardConfig(mode="live")))
    assert DEFAULT.option_bases == ("BTC", "ETH") and DEFAULT.option_min_dte_days == 4.0 and DEFAULT.option_max_spread == 0.10
    tight = GuardConfig(option_max_spread=0.05)
    assert any("spread" in v for v in g.check_option(OP, OS, tight))  # 7.4 % > 5 %
