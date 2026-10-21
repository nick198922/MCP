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
    assert any("open size" in v for v in check(P, replace(S, open_notional_usd=26_000.0)))
    assert not any("open size" in v for v in check(P, replace(S, open_notional_usd=20_000.0)))


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
        (P, replace(S, open_symbols=tuple(f"s{i}" for i in range(15))), "open positions = max"),
        (P, replace(S, trades_today=5), "trades today = max"),
        (P, replace(S, orders_last_hour=5), "orders in the last hour"),
        (P, replace(S, open_risk_usd=1450.0), "open risk"),
        (P, replace(S, day_realised_loss_usd=500.0), "realised loss today"),
        (P, replace(S, seen_link_ids=("td-S14-1",)), "already decided"),
    ],
)
def test_each_limit_refuses(p, s, needle):
    assert any(needle in v for v in check(p, s)), check(p, s)


def test_only_testnet_mode_exists():
    assert any("not allowed" in v for v in check(P, S, GuardConfig(mode="live")))
    assert DEFAULT.mode == g.MODE == "testnet" and DEFAULT.venue == "bybit-demo" and DEFAULT.max_trades_per_day == 5


def test_short_levels_mirror():
    short = plan_order(BTC, "short", 100_000.0, 102_000.0, 95_000.0, "S14", "s", 0.001, 0.001)
    assert check(short, S) == []


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
