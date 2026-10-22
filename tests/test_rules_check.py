"""The owner's rules (docs/knowledge/trading-strategy.md, approved 18.10) on a planned option structure and on journal
rows shaped as scripts/{bybit,binance,hyperliquid}_import.py and journal_add write them; expectations by hand."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tradedesk.analytics import rules_check as rc
from tradedesk.analytics.strategy import payoff_extremes

H = 3600.0
D = datetime(2025, 10, 10, tzinfo=UTC).timestamp()  # Friday, ISO week 2025-W41
BYBIT = '["bybit", "import", "linear"]'
BINANCE = '["binance", "import", "usdm"]'
HL = '["hyperliquid", "import", "perp"]'
BYBIT_OPT = '["bybit", "import", "option"]'


def _t(i, symbol, side, opened, closed, pnl, tags=HL, size=1.0, entry=1.0, stop=None, target=None, status="closed"):
    return {
        "id": i,
        "symbol": symbol,
        "side": side,
        "opened_ts": int(opened),
        "closed_ts": int(closed) if closed is not None else None,
        "status": status,
        "pnl": pnl,
        "size": size,
        "entry": entry,
        "stop": stop,
        "target": target,
        "tags": tags,
    }


def _by_id(checks):
    return {c["id"]: c for c in checks}


# ---------------------------------------------------------------- symbols


def test_option_expiry_and_base_asset_from_real_symbols():
    assert rc.option_expiry_ts("BTC-31OCT26-90000-C") == datetime(2026, 10, 31, 8, tzinfo=UTC).timestamp()
    assert rc.option_expiry_ts("ETH-3OCT25-4000-P-USDT") == datetime(2025, 10, 3, 8, tzinfo=UTC).timestamp()
    for bad in ("BTCUSDT", "BTC-31FOO26-1-C", "BTC-31FEB26-90000-C", None, "BTC-PERPETUAL"):
        assert rc.option_expiry_ts(bad) is None, bad
    for sym, base in (
        ("SOLUSDT", "SOL"),
        ("SOL/USDT:USDT", "SOL"),
        ("1000PEPEUSDT", "1000PEPE"),
        ("HYPE", "HYPE"),
        ("ETHUSDC", "ETH"),
        ("BTCUSD", "BTC"),
        ("SOL-PERP", "SOL"),
        ("xyz:TSLA", "TSLA"),  # Hyperliquid HIP-3: the asset after the dex, not the dex
    ):
        assert rc.base_asset(sym) == base, sym
    assert rc.is_hip3("xyz:TSLA") and not rc.is_hip3("SOL/USDT:USDT") and not rc.is_hip3("SOLUSDT")
    assert rc.iso_week(D) == "2025-W41" and rc.utc_day(D + 23 * H) == "2025-10-10"


# ---------------------------------------------------------------- before a trade


NOW = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()
EXP = datetime(2026, 11, 27, 8, tzinfo=UTC).timestamp()  # 27NOV26


def _leg(kind, side, k, prem, size=0.1):
    return {"kind": kind, "side": side, "size": size, "strike": k, "entry_premium": prem}


def _strangle(side="long", size=0.1):
    return [_leg("call", side, 90000.0, 1450.0, size), _leg("put", side, 77000.0, 1460.0, size)]


def _pre(legs, **kw):
    kw.setdefault("now_ts", NOW)
    kw.setdefault("expiry_ts", EXP)
    return {c["key"]: c for c in rc.pre_trade_option(legs, **kw)}


def test_pre_trade_long_strangle_against_the_owners_one_percent():
    # 0.1 BTC of the 25Δ strangle: (1450 + 1460) * 0.1 = $291 paid, which is also the max loss; limit $100 each
    c = _pre(_strangle())
    assert c["premium"]["status"] == rc.VIOLATED and c["premium"]["value"] == pytest.approx(291.0) and c["premium"]["limit"] == 100.0
    assert c["risk"]["status"] == rc.VIOLATED and c["risk"]["value"] == pytest.approx(291.0) and c["risk"]["rule"] == 1
    assert c["days_to_expiry"]["status"] == rc.OK and c["days_to_expiry"]["value"] == pytest.approx(37 + 20 / 24)
    # 0.03 BTC: $87.30 fits both
    c = _pre(_strangle(size=0.03))
    assert (c["premium"]["status"], c["risk"]["status"]) == (rc.OK, rc.OK) and c["premium"]["value"] == pytest.approx(87.3)
    # a bigger deposit moves both limits: $291 is 1 % of $29 100
    c = _pre(_strangle(), deposit_usd=29_100.0)
    assert (c["premium"]["status"], c["risk"]["status"]) == (rc.OK, rc.OK)


def test_pre_trade_days_to_expiry_boundary():
    legs = _strangle(size=0.01)
    assert _pre(legs, now_ts=EXP - 3 * 86400)["days_to_expiry"]["status"] == rc.VIOLATED  # exactly 3 days
    assert _pre(legs, now_ts=EXP - 3 * 86400 - 60)["days_to_expiry"]["status"] == rc.OK
    assert _pre(legs, now_ts=EXP - 0.5 * 86400)["days_to_expiry"]["status"] == rc.VIOLATED  # 0DTE is worse, not exempt
    assert _pre(legs, expiry_ts=None)["days_to_expiry"]["status"] == rc.UNKNOWN


def test_pre_trade_sales_and_credit_condor():
    c = _pre(_strangle("short"))
    assert c["risk"]["status"] == rc.VIOLATED and c["risk"]["value"] is None and "unbounded" in c["risk"]["detail"]
    assert c["premium"]["status"] == c["days_to_expiry"]["status"] == rc.NA
    # iron condor: credit 0.1 * (1450 + 1460 - 700 - 800) = $141; max loss = 0.1 * 5000 wing - 141 = $359
    condor = [*_strangle("short"), _leg("call", "long", 95000.0, 700.0), _leg("put", "long", 72000.0, 800.0)]
    c = _pre(condor, now_ts=EXP - 86400)
    assert c["premium"]["status"] == rc.NA and c["premium"]["value"] == pytest.approx(-141.0)
    assert c["days_to_expiry"]["status"] == rc.NA  # a sale near expiry is not "buying"
    assert c["risk"]["status"] == rc.VIOLATED and c["risk"]["value"] == pytest.approx(359.0)
    assert c["risk"]["value"] == pytest.approx(-payoff_extremes(condor)["max_loss"])


def test_pre_trade_unknown_inputs():
    c = _pre(_strangle(), deposit_usd=0.0)
    assert c["risk"]["status"] == c["premium"]["status"] == rc.UNKNOWN
    legs = _strangle()
    legs[0]["entry_premium"] = None
    assert _pre(legs)["premium"]["status"] == rc.UNKNOWN
    assert {x["status"] for x in rc.pre_trade_option([], now_ts=NOW, expiry_ts=EXP)} == {rc.NA}


# ---------------------------------------------------------------- the journal


def _day_stop_rows():
    return [
        _t(1, "ETHUSDT", "long", D + 9 * H, D + 9 * H, -50.0, BYBIT),  # Bybit: no opening time
        _t(2, "SOLUSDT", "short", D + 9.5 * H, D + 10 * H, -30.0, BINANCE),  # 2nd loss in a row: stop at 10:00
        _t(3, "DOGE", "long", D + 11 * H, D + 12 * H, 40.0),  # opened after the stop
        _t(4, "XRPUSDT", "long", D + 13 * H, D + 13 * H, -20.0, BYBIT),  # after the stop, judged on its close time
        _t(5, "BNBUSDT", "short", D + 8 * H, D + 14 * H, 10.0, BINANCE),  # opened before the stop
        _t(6, "SOLUSDT", "long", D + 25 * H, D + 26 * H, -5.0, BINANCE),  # next UTC day: a new day
    ]


def test_day_stop_after_two_losses_in_a_row():
    stops = rc.day_stops(_day_stop_rows())
    assert stops == {"2025-10-10": {"ts": D + 10 * H, "reason": "streak"}}
    c = _by_id(rc.trade_checks(_day_stop_rows(), now_ts=D + 30 * H))
    assert [i for i in c if c[i]["rules"][4] == rc.VIOLATED] == [3, 4]
    assert c[4]["approx"] is True and c[3]["approx"] is False and c[5]["rules"][4] == rc.OK


def test_day_stop_on_the_day_loss_and_its_boundary():
    # loss, win, loss: never 2 in a row, but −150 + 20 − 80 = −210 <= −$200
    rows = [
        _t(1, "SOLUSDT", "long", D + H, D + 2 * H, -150.0, BINANCE),
        _t(2, "SOLUSDT", "long", D + 3 * H, D + 4 * H, 20.0, BINANCE),
        _t(3, "SOLUSDT", "long", D + 5 * H, D + 6 * H, -80.0, BINANCE),
        _t(4, "SOLUSDT", "long", D + 7 * H, D + 8 * H, 5.0, BINANCE),
    ]
    assert rc.day_stops(rows) == {"2025-10-10": {"ts": D + 6 * H, "reason": "day_loss"}}
    exact = [_t(1, "SOLUSDT", "long", D + H, D + 2 * H, -200.0, BINANCE)]
    assert rc.day_stops(exact)["2025-10-10"]["reason"] == "day_loss"  # −2 % exactly is the stop
    assert rc.day_stops([_t(1, "SOLUSDT", "long", D + H, D + 2 * H, -199.0, BINANCE)]) == {}
    # a bigger deposit raises the bar; two losses in a row still stop the day
    assert rc.day_stops(rows, deposit_usd=20_000.0) == {}
    both = [_t(1, "A", "long", D + H, D + 2 * H, -150.0), _t(2, "B", "long", D + 3 * H, D + 4 * H, -60.0)]
    assert rc.day_stops(both)["2025-10-10"] == {"ts": D + 4 * H, "reason": "both"}


def _alt_rows():
    return [
        _t(1, "SOL", "long", D, D + 10 * H, -300.0),
        _t(2, "DOGE", "long", D + 1 * H, D + 10 * H, -400.0),
        _t(3, "XRP", "long", D + 2 * H, D + 10 * H, -500.0),
        _t(4, "SOL", "long", D + 3 * H, D + 4 * H, -10.0),  # more of an alt already held: not a new long
        _t(5, "ADA", "long", D + 5 * H, D + 6 * H, -600.0),  # 4th distinct alt long
        _t(6, "ETH", "long", D + 5.5 * H, D + 6 * H, 1.0),  # a major
        _t(7, "PEPE", "short", D + 5.5 * H, D + 6 * H, 1.0),  # a short
        _t(8, "WIF", "long", D + 6.5 * H, None, None, status="open"),  # open: SOL, DOGE, XRP still held -> 4th
        _t(9, "AVAX", "long", D + 10 * H, D + 11 * H, 2.0),  # SOL, DOGE, XRP closed at 10:00; WIF still open
        _t(10, "LINKUSDT", "long", D + 12 * H, D + 12 * H, 3.0, BYBIT),  # no opening time
    ]


def test_simultaneous_alt_longs():
    c = _by_id(rc.trade_checks(_alt_rows(), now_ts=D + 20 * H))
    assert {i: c[i]["alt_longs"] for i in (1, 2, 3, 4, 5, 8, 9)} == {1: 1, 2: 2, 3: 3, 4: 3, 5: 4, 8: 4, 9: 2}
    assert [i for i in c if c[i]["rules"][2] == rc.VIOLATED] == [5, 8]
    assert c[6]["rules"][2] == c[7]["rules"][2] == rc.NA and c[10]["rules"][2] == rc.UNKNOWN and c[10]["alt_longs"] is None


def _hip3_rows():
    return [
        _t(1, "SOL", "long", D, D + 10 * H, 1.0),
        _t(2, "DOGE", "long", D + 1 * H, D + 10 * H, 1.0),
        _t(3, "xyz:TSLA", "long", D + 2 * H, D + 10 * H, 1.0),  # HIP-3 stock: not a crypto alt
        _t(4, "xyz:NVDA", "long", D + 3 * H, D + 10 * H, 1.0),  # same dex, another asset
        _t(5, "XRP", "long", D + 4 * H, D + 10 * H, 1.0),  # the 3rd crypto alt: within the rule
    ]


def test_hip3_symbols_are_left_out_of_the_alt_long_count():
    c = _by_id(rc.trade_checks(_hip3_rows(), now_ts=D + 20 * H))
    assert c[3]["rules"][2] == c[4]["rules"][2] == rc.UNKNOWN and c[3]["alt_longs"] is None and c[4]["alt_longs"] is None
    assert c[5]["alt_longs"] == 3 and c[5]["rules"][2] == rc.OK
    assert rc.weekly_violations(_hip3_rows(), now_ts=D + 20 * H)[0]["rules"][2] == {
        "status": rc.OK,
        "violated": 0,
        "checked": 3,
        "unknown": 2,
    }


def _option_rows():
    return [
        # bought 1.83 days before the 10.10 08:00 delivery, $20 premium
        _t(1, "BTC-10OCT25-120000-C", "long", D - 20 * H, D + 8 * H, -20.0, BYBIT_OPT, size=0.05, entry=400.0),
        # $150 premium, 30 days to expiry
        _t(2, "ETH-31OCT25-5000-P-USDT", "long", D - 9 * 86400 + 8 * H, D + 2 * H, 30.0, BYBIT_OPT, size=1.0, entry=150.0),
        # no opening time: the days cannot be told; $50 premium is fine
        _t(3, "BTC-17OCT25-110000-P", "long", D + 3 * H, D + 3 * H, -50.0, BYBIT_OPT, size=0.1, entry=500.0),
        # exactly 3 days before delivery
        _t(4, "BTC-17OCT25-130000-C", "long", D + 4 * 86400 + 8 * H, D + 5 * 86400, -9.0, BYBIT_OPT, size=0.1, entry=90.0),
        # a sale: rule 8 does not apply, its risk needs a stop
        _t(5, "BTC-17OCT25-100000-P", "short", D + 4 * H, D + 5 * H, 15.0, BYBIT_OPT, size=0.1, entry=300.0),
    ]


def test_option_buys_rule_8_and_their_premium_as_rule_1():
    c = _by_id(rc.trade_checks(_option_rows(), now_ts=D + 10 * 86400))
    assert (c[1]["late_buy"], c[1]["big_premium"], c[1]["rules"][8], c[1]["rules"][1]) == (True, False, rc.VIOLATED, rc.OK)
    assert (c[2]["late_buy"], c[2]["big_premium"], c[2]["rules"][8], c[2]["rules"][1]) == (False, True, rc.VIOLATED, rc.VIOLATED)
    assert (c[3]["late_buy"], c[3]["big_premium"], c[3]["rules"][8]) == (None, False, rc.UNKNOWN)
    assert c[4]["late_buy"] is True and c[4]["rules"][8] == rc.VIOLATED
    assert c[5]["rules"][8] == rc.NA and c[5]["rules"][1] == rc.UNKNOWN
    assert all(c[i]["rules"][2] == rc.NA and c[i]["rules"][5] == rc.NA for i in c)


def test_stop_and_target_from_journal_add():
    rows = [
        _t(1, "BTCUSDT", "long", D, D + H, 20.0, '["setup:breakout"]', size=0.01, entry=80000.0, stop=79000.0, target=82000.0),
        _t(2, "BTCUSDT", "long", D + 2 * H, D + 3 * H, 50.0, "[]", size=0.2, entry=80000.0, stop=79000.0, target=81000.0),
        _t(3, "ETHUSDT", "short", D + 4 * H, D + 5 * H, 5.0, "[]", size=1.0, entry=4000.0, stop=4050.0),
        _t(4, "ETHUSDT", "short", D + 6 * H, D + 7 * H, 5.0, BYBIT),
    ]
    c = _by_id(rc.trade_checks(rows, now_ts=D + 10 * H))
    assert (c[1]["rules"][1], c[1]["rules"][5]) == (rc.OK, rc.OK)  # risk $10, R:R 2.0 exactly
    assert (c[2]["rules"][1], c[2]["rules"][5]) == (rc.VIOLATED, rc.VIOLATED)  # risk $200, R:R 1
    assert (c[3]["rules"][1], c[3]["rules"][5]) == (rc.OK, rc.UNKNOWN)  # risk $50, no target
    assert (c[4]["rules"][1], c[4]["rules"][5]) == (rc.UNKNOWN, rc.UNKNOWN)  # an import: no stop


def test_weekly_violations_newest_first_with_counts():
    rows = _day_stop_rows() + [
        {**r, "id": 100 + r["id"], "opened_ts": r["opened_ts"] + 7 * 86400, "closed_ts": r["closed_ts"] and r["closed_ts"] + 7 * 86400}
        for r in _alt_rows()
    ]
    rows += [{"id": 999, "symbol": "X", "side": "long", "opened_ts": int(D), "status": "cancelled", "tags": "[]"}]  # skipped
    w = rc.weekly_violations(rows, now_ts=D + 8 * 86400 + 20 * H)
    assert [x["week"] for x in w] == ["2025-W42", "2025-W41"]
    new, old = w
    # 2025-W41: the day-stop rows; DOGE (+40) opened after the stop; Bybit XRP (−20) only on its close time: kept
    # apart from the exact count, in rule 4 and in the totals
    assert old["n_trades"] == 6 and old["days_stopped"] == 1 and old["approx"] == 1
    assert old["rules"][4] == {"status": rc.VIOLATED, "violated": 1, "checked": 5, "unknown": 0}
    assert old["n_violating"] == 1 and old["pnl_violating"] == pytest.approx(40.0)
    assert old["n_violating_approx"] == 1 and old["pnl_violating_approx"] == pytest.approx(-20.0)
    assert old["rules"][8]["status"] == rc.NA and old["rules"][5]["status"] == rc.UNKNOWN
    # 2025-W42: the alt longs, shifted a week (as 10.10.2025: they fall together). ADA (−600) and the open WIF broke
    # rule 2; SOL −10 then ADA −600 at 06:00 stop the day ('both'), so WIF and AVAX (+2) broke rule 4 as well, and
    # Bybit LINK (+3) only on its close time
    assert new["rules"][2] == {"status": rc.VIOLATED, "violated": 2, "checked": 7, "unknown": 1}
    assert new["rules"][4] == {"status": rc.VIOLATED, "violated": 2, "checked": 9, "unknown": 0}
    assert new["days_stopped"] == 1 and new["approx"] == 1 and new["max_alt_longs"] == 4
    assert new["n_violating"] == 3 and new["pnl_violating"] == pytest.approx(-598.0)
    assert new["n_violating_approx"] == 1 and new["pnl_violating_approx"] == pytest.approx(3.0)
    assert new["late_buys"] == new["big_premiums"] == 0
    assert rc.weekly_violations([], now_ts=D) == []


def test_approximate_day_stop_alone_is_not_an_exact_violation():
    rows = [
        _t(1, "SOLUSDT", "long", D + 1 * H, D + 2 * H, -60.0, BINANCE),
        _t(2, "SOLUSDT", "long", D + 3 * H, D + 4 * H, -60.0, BINANCE),  # 2 losses in a row: stop at 04:00
        _t(3, "ADAUSDT", "long", D + 5 * H, D + 5 * H, -30.0, BYBIT),  # Bybit, closed after the stop: approximate only
        # Bybit option bought after the stop with a $300 premium: rule 8 is exact, so the trade counts as exact
        _t(4, "BTC-31OCT25-120000-C", "long", D + 6 * H, D + 6 * H, -300.0, BYBIT_OPT, size=0.1, entry=3000.0),
    ]
    c = _by_id(rc.trade_checks(rows, now_ts=D + 20 * H))
    assert c[3]["approx"] and c[3]["rules"][4] == rc.VIOLATED and c[4]["approx"] and c[4]["big_premium"]
    (w,) = rc.weekly_violations(rows, now_ts=D + 20 * H)
    assert w["rules"][4] == {"status": rc.OK, "violated": 0, "checked": 2, "unknown": 0} and w["approx"] == 2
    assert (w["n_violating"], w["n_violating_approx"]) == (1, 1)
    assert w["pnl_violating"] == pytest.approx(-300.0) and w["pnl_violating_approx"] == pytest.approx(-30.0)
    # no approximate trade: the approximate P&L is None, not 0
    (w,) = rc.weekly_violations(rows[:2], now_ts=D + 20 * H)
    assert (w["n_violating"], w["n_violating_approx"], w["pnl_violating"], w["pnl_violating_approx"]) == (0, 0, None, None)


def test_weekly_option_counts_and_a_stop_week_without_openings():
    w = {x["week"]: x for x in rc.weekly_violations(_option_rows(), now_ts=D + 10 * 86400)}
    assert w["2025-W41"]["late_buys"] == 1 and w["2025-W41"]["big_premiums"] == 0
    assert w["2025-W41"]["rules"][8] == {"status": rc.VIOLATED, "violated": 1, "checked": 1, "unknown": 1}
    assert w["2025-W40"]["big_premiums"] == 1 and w["2025-W42"]["late_buys"] == 1
    # a trade opened on Sunday closes on Monday with a −$250 loss: the stop day is in the next week, with no openings
    sun = datetime(2025, 10, 12, 20, tzinfo=UTC).timestamp()
    w = rc.weekly_violations([_t(1, "SOL", "long", sun, sun + 6 * H, -250.0)], now_ts=sun + 86400)
    assert [(x["week"], x["n_trades"], x["days_stopped"]) for x in w] == [("2025-W42", 0, 1), ("2025-W41", 1, 0)]
    assert all(r["status"] == rc.NA for r in w[0]["rules"].values())
