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


def test_pre_trade_rule_8_zones_and_their_boundaries():
    legs = _strangle(size=0.01)
    zone = lambda h: _pre(legs, now_ts=EXP - h * H)["days_to_expiry"]  # noqa: E731
    # under 24 h: never, whatever the reasons; past the delivery too
    for h in (23.99, 12.0, 0.0, -1.0):
        assert (zone(h)["status"], zone(h)["zone"]) == (rc.VIOLATED, rc.ZONE_BAN), h
    # 24 h and 72 h exactly are the conditional zone; without a thesis it is violated
    for h in (24.0, 48.0, 72.0):
        assert (zone(h)["zone"], zone(h)["status"]) == (rc.ZONE_COND, rc.VIOLATED), h
    assert (zone(72.0 + 1 / 60)["zone"], zone(72.0 + 1 / 60)["status"]) == (rc.ZONE_FREE, rc.OK)
    assert zone(12.0)["limit"] == 1.0 and zone(12.0)["value"] == pytest.approx(0.5)
    # the three checks are rows of their own only in the 1-3 day zone
    assert not {"thesis", "breakeven_move", "iv_vs_rv"} & set(_pre(legs, now_ts=EXP - 12 * H))
    assert not {"thesis", "breakeven_move", "iv_vs_rv"} & set(_pre(legs, now_ts=EXP - 80 * H))
    c = _pre(legs, expiry_ts=None)["days_to_expiry"]
    assert (c["status"], c["zone"]) == (rc.UNKNOWN, None)


def _call(k=80000.0, prem=500.0, iv=0.40, size=0.1):
    return [{**_leg("call", "long", k, prem, size), "iv": iv}]


def test_pre_trade_rule_8_conditions_one_to_three_days():
    # 48 h before the delivery: T = 2/365; RV30 40 % -> 1 sigma = 0.40 * sqrt(2/365) = 2.961 % of spot
    now, sig = EXP - 48 * H, 0.40 * (2 / 365) ** 0.5
    kw = {"now_ts": now, "spot": 80000.0, "rv": 0.40, "thesis": "long: CPI, above 81k", "target": 82000.0}
    # an ATM call for $500: breakeven 80 500, 0.625 % away; IV 40 % <= 1.2 x 40 % = 48 %
    c = _pre(_call(), **kw)
    assert [c[k]["status"] for k in ("thesis", "breakeven_move", "iv_vs_rv")] == [rc.OK, rc.OK, rc.OK]
    assert c["days_to_expiry"]["status"] == rc.OK and c["days_to_expiry"]["zone"] == rc.ZONE_COND
    assert c["breakeven_move"]["value"] == pytest.approx(500 / 80000) and c["breakeven_move"]["limit"] == pytest.approx(sig)
    assert c["iv_vs_rv"]["value"] == pytest.approx(0.40) and c["iv_vs_rv"]["limit"] == pytest.approx(0.48)
    assert "RV30" in c["breakeven_move"]["detail"]
    # (a) no thesis, or a thesis without a target
    for th, tg, why in ((None, 82000.0, "no thesis"), ("  ", None, "no thesis and no target"), ("long", None, "no target")):
        a = _pre(_call(), **{**kw, "thesis": th, "target": tg})
        assert (a["thesis"]["status"], a["thesis"]["detail"], a["days_to_expiry"]["status"]) == (rc.VIOLATED, why, rc.VIOLATED)
    # (b) a far OTM call: breakeven 84 500 is 5.6 % away > 2.96 %; (c) IV 50 % > 48 %
    b = _pre(_call(k=84000.0, prem=500.0), **kw)
    assert b["breakeven_move"]["status"] == rc.VIOLATED and b["breakeven_move"]["value"] == pytest.approx(4500 / 80000)
    assert _pre(_call(iv=0.50), **kw)["iv_vs_rv"]["status"] == rc.VIOLATED
    assert _pre(_call(iv=0.48), **kw)["iv_vs_rv"]["status"] == rc.OK  # 1.2 x RV exactly is within
    # already past the breakeven at spot: no move needed
    assert _pre(_call(k=70000.0, prem=500.0), **kw)["breakeven_move"]["value"] == 0.0
    # a long strangle pays both premiums (2910 per unit): breakevens 77 000 - 2910 and 90 000 + 2910, the nearer counts
    s = _pre([{**lg, "iv": 0.4} for lg in _strangle()], **kw)
    assert s["breakeven_move"]["value"] == pytest.approx(min(80000 - 74090, 92910 - 80000) / 80000)
    # no spot / RV / IV: unknown, and a thesis alone cannot make the zone ok
    u = _pre(_strangle(), **{**kw, "rv": None})
    assert (u["breakeven_move"]["status"], u["iv_vs_rv"]["status"], u["days_to_expiry"]["status"]) == (rc.UNKNOWN, rc.UNKNOWN, rc.UNKNOWN)
    # any failed check wins over unknown ones
    assert _pre(_strangle(), **{**kw, "rv": None, "thesis": None})["days_to_expiry"]["status"] == rc.VIOLATED
    # a leg without a premium: (b) is unknown, not a move computed as if the option were free (critic, 22.10)
    p = _pre(_call(prem=None), **kw)
    assert (p["breakeven_move"]["status"], p["breakeven_move"]["detail"], p["premium"]["status"]) == (
        rc.UNKNOWN,
        "premium unknown",
        rc.UNKNOWN,
    )
    assert p["days_to_expiry"]["status"] == rc.UNKNOWN


def test_breakeven_move_shapes():
    put = [_leg("put", "long", 80000.0, 400.0, 1.0)]
    assert rc.breakeven_move(put, 80000.0) == pytest.approx(400 / 80000)
    # a bear put spread bought for more than its width never profits at expiry
    bad = [_leg("put", "long", 80000.0, 1500.0, 1.0), _leg("put", "short", 79000.0, 100.0, 1.0)]
    assert rc.breakeven_move(bad, 80000.0) is None
    assert rc.breakeven_move([], 80000.0) is None and rc.breakeven_move(put, 0.0) is None
    assert rc.buy_zone(23.9) == rc.ZONE_BAN and rc.buy_zone(24.0) == rc.buy_zone(72.0) == rc.ZONE_COND and rc.buy_zone(72.1) == rc.ZONE_FREE


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


def _at_now_rows():
    # three alt longs still open and a 4th opened at now_ts itself (a /trade from Telegram); an alt closed at that same
    # second is no longer held
    now = D + 6 * H
    return now, [
        _t(1, "SOL", "long", D, None, None, status="open"),
        _t(2, "DOGE", "long", D + H, None, None, status="open"),
        _t(3, "XRP", "long", D + 2 * H, None, None, status="open"),
        _t(4, "AVAX", "long", D + 3 * H, now, -1.0),
        _t(5, "ADA", "long", now, None, None, status="open"),
    ]


def test_a_trade_opened_at_now_still_sees_the_open_alt_longs():
    now, rows = _at_now_rows()
    c = _by_id(rc.trade_checks(rows, now_ts=now))
    assert (c[5]["alt_longs"], c[5]["rules"][2]) == (4, rc.VIOLATED)
    assert c[4]["alt_longs"] == 4 and c[4]["rules"][2] == rc.VIOLATED  # AVAX, opened while SOL, DOGE, XRP were open


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
        # bought 1.83 days before the 10.10 08:00 delivery, $20 premium; an import: no thesis, no target
        _t(1, "BTC-10OCT25-120000-C", "long", D - 20 * H, D + 8 * H, -20.0, BYBIT_OPT, size=0.05, entry=400.0),
        # $150 premium, 30 days to expiry
        _t(2, "ETH-31OCT25-5000-P-USDT", "long", D - 9 * 86400 + 8 * H, D + 2 * H, 30.0, BYBIT_OPT, size=1.0, entry=150.0),
        # no opening time: the days cannot be told; $50 premium is fine
        _t(3, "BTC-17OCT25-110000-P", "long", D + 3 * H, D + 3 * H, -50.0, BYBIT_OPT, size=0.1, entry=500.0),
        # exactly 3 days before delivery: still the 1-3 day zone, and no thesis
        _t(4, "BTC-17OCT25-130000-C", "long", D + 4 * 86400 + 8 * H, D + 5 * 86400, -9.0, BYBIT_OPT, size=0.1, entry=90.0),
        # a sale: rule 8 does not apply, its risk needs a stop
        _t(5, "BTC-17OCT25-100000-P", "short", D + 4 * H, D + 5 * H, 15.0, BYBIT_OPT, size=0.1, entry=300.0),
    ]


def _odte_row():
    # 0DTE: 6 h before the 17.10 delivery, $10 premium
    return _t(6, "BTC-17OCT25-108000-C", "long", D + 7 * 86400 + 2 * H, D + 7 * 86400 + 8 * H, -10.0, BYBIT_OPT, size=0.1, entry=100.0)


def test_option_buys_rule_8_and_their_premium_as_rule_1():
    c = _by_id(rc.trade_checks([*_option_rows(), _odte_row()], now_ts=D + 10 * 86400))
    assert (c[1]["late_buy"], c[1]["big_premium"], c[1]["rules"][8], c[1]["rules"][1]) == (True, False, rc.VIOLATED, rc.OK)
    assert c[1]["buy_zone"] == rc.ZONE_COND
    assert c[1]["buy_conditions"] == {"thesis": rc.VIOLATED, "breakeven_move": rc.UNKNOWN, "iv_vs_rv": rc.UNKNOWN}
    assert (c[2]["late_buy"], c[2]["big_premium"], c[2]["rules"][8], c[2]["rules"][1]) == (False, True, rc.VIOLATED, rc.VIOLATED)
    assert (c[2]["buy_zone"], c[2]["buy_conditions"]) == (rc.ZONE_FREE, None)
    assert (c[3]["late_buy"], c[3]["big_premium"], c[3]["rules"][8], c[3]["buy_zone"]) == (None, False, rc.UNKNOWN, None)
    assert c[4]["late_buy"] is True and c[4]["rules"][8] == rc.VIOLATED and c[4]["buy_zone"] == rc.ZONE_COND
    assert c[5]["rules"][8] == rc.NA and c[5]["rules"][1] == rc.UNKNOWN and c[5]["buy_zone"] is None
    assert (c[6]["buy_zone"], c[6]["late_buy"], c[6]["rules"][8], c[6]["buy_conditions"]) == (rc.ZONE_BAN, True, rc.VIOLATED, None)
    assert all(c[i]["rules"][2] == rc.NA and c[i]["rules"][5] == rc.NA for i in c)


def test_journal_rule_8_one_to_three_days_with_a_thesis():
    # journal_add with a thesis and a target, 48 h before the delivery: (a) holds, but the journal has no spot or IV at
    # the opening, so (b) and (c) cannot be told: unknown, not ok
    own = _t(
        1, "BTC-17OCT25-110000-C", "long", D + 5 * 86400 + 8 * H, D + 6 * 86400, 5.0, '["option"]', size=0.1, entry=500.0, target=115000.0
    )
    row = {**own, "thesis": "CPI on 16.10: a move up past 112k"}
    (c,) = rc.trade_checks([row], now_ts=D + 10 * 86400)
    assert c["buy_conditions"] == {"thesis": rc.OK, "breakeven_move": rc.UNKNOWN, "iv_vs_rv": rc.UNKNOWN}
    assert (c["late_buy"], c["rules"][8]) == (None, rc.UNKNOWN)
    # without the target, or an import's placeholder thesis, (a) fails
    (c,) = rc.trade_checks([{**row, "target": None}], now_ts=D + 10 * 86400)
    assert (c["late_buy"], c["rules"][8]) == (True, rc.VIOLATED)
    imp = {**row, "tags": BYBIT_OPT, "thesis": "імпорт з Bybit: теза не записана"}
    (c,) = rc.trade_checks([imp], now_ts=D + 10 * 86400)
    assert c["buy_conditions"]["thesis"] == rc.VIOLATED and c["rules"][8] == rc.VIOLATED


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


def test_open_risk_sums_rule_1_risk_of_the_open_trades():
    btc = _t(1, "BTCUSDT", "long", D, None, None, "[]", size=0.05, entry=84000.0, stop=82000.0, status="open")  # $100
    opt = _t(2, "BTC-17OCT25-110000-C", "long", D, None, None, '["option"]', size=0.1, entry=800.0, status="open")  # $80
    done = _t(3, "SOLUSDT", "long", D, D + H, -50.0, "[]", size=10.0, entry=200.0, stop=180.0)  # closed: not open risk
    r = rc.open_risk([btc, opt, done])
    assert r == {"status": rc.OK, "total": pytest.approx(180.0), "limit": 300.0, "n_open": 2, "n_unknown": 0}
    # an open import without a stop: its risk cannot be told, so the total is a floor
    imp = _t(4, "SOLUSDT", "long", D, None, None, BYBIT, status="open")
    r = rc.open_risk([btc, opt, done, imp])
    assert (r["status"], r["n_open"], r["n_unknown"]) == (rc.UNKNOWN, 3, 1)
    # the known part alone over $300 is a violation whatever the unknown rows hold; exactly $300 is within the rule
    big = _t(5, "ETHUSDT", "short", D, None, None, "[]", size=1.0, entry=4000.0, stop=4150.0, status="open")  # $150
    assert rc.open_risk([btc, opt, imp, big])["status"] == rc.VIOLATED
    edge = _t(6, "ETHUSDT", "short", D, None, None, "[]", size=1.0, entry=4000.0, stop=4120.0, status="open")  # $120
    assert rc.open_risk([btc, opt, edge]) == {"status": rc.OK, "total": pytest.approx(300.0), "limit": 300.0, "n_open": 3, "n_unknown": 0}
    assert rc.open_risk([done])["status"] == rc.NA and rc.open_risk([btc], deposit_usd=2_000.0)["status"] == rc.VIOLATED


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


def test_compliance_summary_counts_unknown_and_approximate_apart():
    rows = [
        _t(1, "BTCUSDT", "long", D, D + H, 20.0, '["setup:breakout"]', size=0.01, entry=80000.0, stop=79000.0, target=82000.0),
        _t(2, "BTCUSDT", "long", D + 2 * H, D + 3 * H, 50.0, "[]", size=0.2, entry=80000.0, stop=79000.0, target=81000.0),
        _t(3, "ETHUSDT", "short", D + 4 * H, D + 5 * H, 5.0, "[]", size=1.0, entry=4000.0, stop=4050.0),
        _t(4, "ETHUSDT", "short", D + 6 * H, D + 7 * H, 5.0, BYBIT),
    ]
    # 1: every rule that applies held (risk $10, R:R 2); 2: risk $200 and R:R 1; 3: no target (rule 5 unknown);
    # 4: an import without a stop (rules 1 and 5 unknown): unknown is not compliant and stays out of the share
    s = rc.compliance_summary(rows, now_ts=D + 10 * H)
    assert s == {
        "n_trades": 4,
        "n_checked": 2,
        "n_ok": 1,
        "n_violated": 1,
        "n_unknown": 2,
        "n_approx": 0,
        "share_ok": 0.5,
        "enough": False,
    }
    # thresholds pass through to trade_checks: on $20 000 trade 2 risks 1 % exactly, but R:R 1 still breaks rule 5
    s = rc.compliance_summary(rows, now_ts=D + 10 * H, deposit_usd=20_000.0, min_checked=2)
    assert (s["n_ok"], s["n_violated"], s["enough"]) == (1, 1, True)
    # the approximate rule 4 alone is neither ok nor an exact violation; the $300 option (rule 8) is exact
    approx = [
        _t(1, "SOLUSDT", "long", D + 1 * H, D + 2 * H, -60.0, BINANCE),
        _t(2, "SOLUSDT", "long", D + 3 * H, D + 4 * H, -60.0, BINANCE),
        _t(3, "ADAUSDT", "long", D + 5 * H, D + 5 * H, -30.0, BYBIT),
        _t(4, "BTC-31OCT25-120000-C", "long", D + 6 * H, D + 6 * H, -300.0, BYBIT_OPT, size=0.1, entry=3000.0),
    ]
    s = rc.compliance_summary(approx, now_ts=D + 20 * H)
    assert (s["n_checked"], s["n_ok"], s["n_violated"], s["n_unknown"], s["n_approx"], s["share_ok"]) == (1, 0, 1, 2, 1, 0.0)
    # the classes add up to the trades; nothing checked gives no share, not 0 %
    assert s["n_ok"] + s["n_violated"] + s["n_unknown"] + s["n_approx"] == s["n_trades"] == 4
    assert rc.compliance_summary(rows[3:], now_ts=D + 10 * H)["share_ok"] is None
    assert rc.compliance_summary([], now_ts=D) == {
        "n_trades": 0,
        "n_checked": 0,
        "n_ok": 0,
        "n_violated": 0,
        "n_unknown": 0,
        "n_approx": 0,
        "share_ok": None,
        "enough": False,
    }


def test_weekly_option_counts_and_a_stop_week_without_openings():
    w = {x["week"]: x for x in rc.weekly_violations([*_option_rows(), _odte_row()], now_ts=D + 10 * 86400)}
    assert w["2025-W41"]["late_buys"] == 1 and w["2025-W41"]["big_premiums"] == 0
    assert (w["2025-W41"]["buys_under_24h"], w["2025-W41"]["buys_cond_failed"]) == (0, 1)
    assert w["2025-W41"]["rules"][8] == {"status": rc.VIOLATED, "violated": 1, "checked": 1, "unknown": 1}
    assert w["2025-W40"]["big_premiums"] == 1 and w["2025-W42"]["late_buys"] == 2
    assert (w["2025-W42"]["buys_under_24h"], w["2025-W42"]["buys_cond_failed"]) == (1, 1)
    # a trade opened on Sunday closes on Monday with a −$250 loss: the stop day is in the next week, with no openings
    sun = datetime(2025, 10, 12, 20, tzinfo=UTC).timestamp()
    w = rc.weekly_violations([_t(1, "SOL", "long", sun, sun + 6 * H, -250.0)], now_ts=sun + 86400)
    assert [(x["week"], x["n_trades"], x["days_stopped"]) for x in w] == [("2025-W42", 0, 1), ("2025-W41", 1, 0)]
    assert all(r["status"] == rc.NA for r in w[0]["rules"].values())
