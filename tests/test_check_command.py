"""The /check Telegram command parser (analytics.check_command): pure, strict on untrusted text, time passed in."""

from datetime import UTC, datetime

import pytest

from tradedesk.analytics import rules_check as rc
from tradedesk.analytics.check_command import CHECK_ID, as_journal_row, parse_check_command, parse_expiry_hours
from tradedesk.analytics.trade_command import parse_trade_command

NOW = datetime(2026, 10, 25, 10, tzinfo=UTC).timestamp()


def ok(text):
    p = parse_check_command(text, now=NOW)
    assert p["ok"], p
    return p["check"]


def test_an_option_purchase_on_the_underlying_with_hours_to_expiry():
    c = ok("/check BTC call premium 80 expiry 36h target 95k thesis пробій‮ діапазону")
    assert (c["kind"], c["symbol"], c["right"], c["side"], c["premium"], c["risk"]) == ("option", "BTC", "call", "long", 80.0, 80.0)
    assert c["expiry_ts"] == NOW + 36 * 3600 and c["expiry_from"] == "text" and c["target"] == 95000.0
    assert c["thesis"] == "пробій діапазону"  # the bidi override is dropped
    assert ok("/check put BTC премія 120 експірація 2д")["expiry_ts"] == NOW + 48 * 3600  # kind first, Ukrainian words


def test_an_option_symbol_carries_the_expiry_and_a_trade_line_works_as_it_is():
    c = ok("/check BTC-27OCT26-90000-C premium 80 target 95000")
    assert c["expiry_from"] == "symbol" and c["expiry_ts"] == rc.option_expiry_ts("BTC-27OCT26-90000-C") and c["right"] == "call"
    line = "BTC-27OCT26-90000-C long 1200 size 0.01 target 95000 thesis чому"
    c = ok("/check " + line)
    t = parse_trade_command("/trade " + line, now=NOW)["trade"]
    assert (c["entry"], c["size"], c["premium"]) == (t["entry"], t["size"], 12.0)
    assert ok("/check BTC-27OCT26-90000-P 1200 size 0.01")["right"] == "put"  # no side word needed for an option symbol


def test_a_linear_trade_by_risk_or_by_size():
    c = ok("/check SOL long 180 stop 170 target 200 risk 90")
    assert (c["kind"], c["side"], c["entry"], c["stop"], c["risk"]) == ("linear", "long", 180.0, 170.0, 90.0)
    assert c["size"] == pytest.approx(9.0)
    c = ok("/check BTC short @ 84000 sl 85000 size 0.05")
    assert c["risk"] == pytest.approx(50.0) and c["target"] is None
    c = ok("/check BTC long 84000")
    assert c["risk"] is None and c["size"] is None and c["stop"] is None


def test_the_journal_row_is_what_rules_check_reads():
    r = as_journal_row(ok("/check BTC call premium 80 expiry 36h"), NOW)
    assert (r["id"], r["status"], r["opened_ts"], r["size"], r["entry"], r["tags"]) == (CHECK_ID, "open", NOW, 1.0, 80.0, ["option"])
    [c] = rc.trade_checks([r], now_ts=NOW)
    assert c["buy_zone"] == rc.ZONE_COND and c["rules"][1] == rc.OK and c["rules"][8] == rc.VIOLATED  # no thesis
    lin = as_journal_row(ok("/check SOL long 180 stop 170 risk 90"), NOW)
    assert lin["tags"] == [] and lin["expiry_ts"] is None and lin["size"] == pytest.approx(9.0)


@pytest.mark.parametrize(
    ("token", "hours"),
    [("36h", 36.0), ("2d", 48.0), ("1.5d", 36.0), ("1,5d", 36.0), ("36г", 36.0), ("36год", 36.0), ("2дн", 48.0), ("2D", 48.0)],
)
def test_expiry_tokens(token, hours):
    assert parse_expiry_hours(token) == hours


@pytest.mark.parametrize("token", ["36", "0h", "-2d", "2w", "1e3h", "367d", "２d", "2 d", "nanh", "1.2345d"])
def test_bad_expiry_tokens(token):
    assert parse_expiry_hours(token) is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "empty"),
        ("   ", "empty"),
        (None, "empty"),
        ("/check" + " x" * 1000, "too_long"),
        ("check BTC long 1", "not_check_command"),
        ("/checks BTC long 1", "not_check_command"),
        ("/trade BTC long 84000 size 0.1", "not_check_command"),
        ("/check", "missing_symbol"),
        ("/check long call", "missing_symbol"),
        ("/check BT<C long 1", "bad_symbol"),
        ("/check BTC 84000", "missing_side"),
        ("/check BTC long", "missing_entry"),
        ("/check BTC long 84,000", "bad_number"),
        ("/check BTC long nan", "bad_number"),
        ("/check BTC long 84000 stop", "missing_value"),
        ("/check BTC long 84000 maybe 1", "unknown_word"),
        ("/check BTC long 84000 stop 83000 stop 82000", "duplicate"),
        ("/check BTC long 84000 stop 85000", "stop_wrong_side"),
        ("/check BTC short 84000 target 85000", "target_wrong_side"),
        ("/check BTC long 84000 size 1 risk 90 stop 83000", "size_and_risk"),
        ("/check BTC long 84000 risk 90", "risk_needs_stop"),
        ("/check BTC long 84000 premium 80", "not_an_option"),
        ("/check BTC long 84000 expiry 2d", "not_an_option"),
        ("/check BTC call expiry 2d", "missing_premium"),
        ("/check BTC call 1200 expiry 2d", "missing_size"),
        ("/check BTC call premium 80", "missing_expiry"),
        ("/check BTC call premium 80 expiry soon", "bad_expiry"),
        ("/check BTC call premium 80 expiry 2d stop 80000", "option_field"),
        ("/check BTC call premium 80 expiry 2d risk 80", "option_field"),
        ("/check BTC call 1200 size 0.01 premium 12 expiry 2d", "premium_and_entry"),
        ("/check BTC-27OCT26-90000-C short premium 80", "option_sale"),
        ("/check BTC-27OCT26-90000-C premium 80 expiry 2d", "expiry_conflict"),
        ("/check BTC-24OCT26-90000-C premium 80", "option_expired"),
        ("/check BTC-27OCT26-90000-C long 0.012 size 1", "option_entry_in_coin"),
        ("/check BTC-31OCT26-90000-C premium 0.012", "option_premium_in_coin"),  # a Deribit premium in BTC
        ("/check BTC call premium 0.015 expiry 5d", "option_premium_in_coin"),
        ("/check BTC-31OCT26-90000-C put premium 80", "kind_conflict"),
        ("/check call BTC-31OCT26-90000-P premium 80", "kind_conflict"),
        ("/check BTC call premium 80 expiry 2d thesis " + "x" * 1001, "thesis_too_long"),
    ],
)
def test_errors(text, code):
    p = parse_check_command(text, now=NOW)
    assert not p["ok"] and p["error"] == code and p["check"] is None


def test_a_premium_below_one_needs_a_dollar_and_a_kind_word_must_match_the_symbol():
    assert ok("/check BTC-31OCT26-90000-C premium $0.5")["premium"] == 0.5
    assert ok("/check BTC call premium $0.8 expiry 5d")["premium"] == 0.8
    assert ok("/check BTC-31OCT26-90000-C premium 1")["premium"] == 1.0
    assert ok("/check BTC-31OCT26-90000-C call premium 80")["right"] == "call"
    assert ok("/check BTC-31OCT26-90000-P option premium 80")["right"] == "put"
    assert ok("/check BTC put premium 80 expiry 5d")["right"] == "put"  # no dated symbol: nothing to contradict


def test_the_journal_row_opens_at_the_whole_second_as_the_trade_row():
    p = parse_check_command("/check BTC call premium 80 expiry 72h target 95000 thesis x", now=NOW + 0.6)
    r = as_journal_row(p["check"], NOW + 0.6)
    assert r["opened_ts"] == int(NOW) and isinstance(r["opened_ts"], int)
    assert r["expiry_ts"] - r["opened_ts"] == 72 * 3600  # 'expiry 72h' stays on the inclusive 72 h edge
    [c] = rc.trade_checks([r], now_ts=NOW + 0.6)
    assert c["buy_zone"] == rc.ZONE_COND


def test_the_expired_check_uses_the_time_passed_in():
    text = "/check BTC-27OCT26-90000-C premium 80"
    assert parse_check_command(text, now=NOW)["ok"]
    assert parse_check_command(text, now=NOW + 3 * 86400)["error"] == "option_expired"
