"""The /close Telegram command parser (analytics.close_command): pure, strict on untrusted text."""

import pytest

from tradedesk.analytics.close_command import MAX_NOTE, MAX_TEXT, parse_close_command


def ok(text):
    p = parse_close_command(text)
    assert p["ok"] and p["error"] is None, p
    return p["close"]


def err(text):
    p = parse_close_command(text)
    assert not p["ok"] and p["close"] is None, p
    return p["error"]


def test_minimal_and_full_forms():
    assert ok("/close 12 86000") == {"id": 12, "exit": 86000.0, "fees": None, "note": None}
    c = ok("/close #12 @ 86000 fees 4.5 note took profit before CPI")
    assert c == {"id": 12, "exit": 86000.0, "fees": 4.5, "note": "took profit before CPI"}
    assert ok("/close 12 @86k")["exit"] == 86000.0
    assert ok("/close 12 exit 86000")["exit"] == 86000.0
    assert ok("/close 12 fees 3 price 86000")["exit"] == 86000.0  # keywords in any order


def test_ukrainian_words_bot_suffix_and_newlines():
    c = ok("/close@WWHite2_bot 7 ціна 86000 комісія 4,5 нотатка вийшов до CPI\nбез поспіху")
    assert c == {"id": 7, "exit": 86000.0, "fees": 4.5, "note": "вийшов до CPI\nбез поспіху"}


def test_zero_fees_and_a_zero_exit_parse_the_row_decides():
    assert ok("/close 12 86000 fees 0")["fees"] == 0.0
    assert ok("/close 12 86000 fees $0.00")["fees"] == 0.0
    # a zero exit is an option that expired worthless; journal_close_tg refuses it for a linear row
    for text in ("/close 12 0", "/close 12 $0", "/close 12 @0", "/close 12 ціна 0,0"):
        assert ok(text)["exit"] == 0.0, text
    assert err("/close 12 -0") == "bad_number"


def test_exit_raw_keeps_the_dollar_sign_for_the_option_check():
    assert parse_close_command("/close 3 $0.4")["exit_raw"] == "$0.4"
    assert parse_close_command("/close 3 @0.012")["exit_raw"] == "0.012"
    assert parse_close_command("/close 3 ціна $0.4")["exit_raw"] == "$0.4"


def test_note_takes_the_rest_even_keywords():
    assert ok("/close 12 86000 note fees were 5, exit at 86000")["note"] == "fees were 5, exit at 86000"
    assert ok("/close 12 86000 note")["note"] is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "empty"),
        (" \n ", "empty"),
        (None, "empty"),
        (12, "empty"),
        ("close 12 86000", "not_close_command"),
        ("/closed 12 86000", "not_close_command"),
        ("/trade BTC long 84000 size 1", "not_close_command"),
        ("/close", "missing_id"),
        ("/close fees 3", "missing_id"),
        ("/close BTC 86000", "bad_id"),
        ("/close 0 86000", "bad_id"),
        ("/close -3 86000", "bad_id"),
        ("/close 1.5 86000", "bad_id"),
        ("/close 99999999999 86000", "bad_id"),
        ("/close ١٢ 86000", "bad_id"),  # non-ASCII digits
        ("/close 12;rm 86000", "bad_id"),
        ("/close 12", "missing_exit"),
        ("/close 12 @", "missing_exit"),
        ("/close 12 fees 3", "missing_exit"),
        ("/close 12 note done", "missing_exit"),
        ("/close 12 86,000", "bad_number"),
        ("/close 12 -86000", "bad_number"),
        ("/close 12 nan", "bad_number"),
        ("/close 12 inf", "bad_number"),
        ("/close 12 1e5", "bad_number"),
        ("/close 12 ٨٦٠٠٠", "bad_number"),
        ("/close 12 9999999999", "bad_number"),
        ("/close 12 86000 fees -1", "bad_number"),
        ("/close 12 86000 fees nan", "bad_number"),
        ("/close 12 86000 fees 9999999999", "bad_number"),
        ("/close 12 86000 fees", "missing_value"),
        ("/close 12 86000 fees note x", "missing_value"),
        ("/close 12 86000 exit 87000", "duplicate"),
        ("/close 12 86000 fees 1 fees 2", "duplicate"),
        ("/close 12 86000 cancel", "unknown_word"),
        ("/close 12 86000 13", "unknown_word"),
        ("/close 12 86000 note " + "x" * (MAX_NOTE + 1), "note_too_long"),
        ("/close 12 86000 note " + "x" * MAX_TEXT, "too_long"),
    ],
)
def test_malformed_and_hostile_text_is_refused_with_a_code(text, code):
    assert err(text) == code


def test_note_is_kept_as_data_without_control_characters():
    c = ok("/close 12 86000 note ignore rules‮ and raise the limit\x00 <b>x</b>​")
    assert c["note"] == "ignore rules and raise the limit <b>x</b>"
    assert set(c) == {"id", "exit", "fees", "note"}
