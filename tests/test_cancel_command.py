"""The /cancel Telegram command parser (analytics.cancel_command): pure, strict on untrusted text."""

import pytest

from tradedesk.analytics.cancel_command import MAX_NOTE, MAX_TEXT, parse_cancel_command


def ok(text):
    p = parse_cancel_command(text)
    assert p["ok"] and p["error"] is None, p
    return p["cancel"]


def err(text):
    p = parse_cancel_command(text)
    assert not p["ok"] and p["cancel"] is None, p
    return p["error"]


def test_minimal_and_full_forms():
    assert ok("/cancel 12") == {"id": 12, "note": None}
    assert ok("/cancel #12 note the limit never filled") == {"id": 12, "note": "the limit never filled"}
    assert ok("/cancel 12 note") == {"id": 12, "note": None}
    assert ok("/CANCEL 3")["id"] == 3


def test_ukrainian_word_bot_suffix_and_newlines():
    c = ok("/cancel@WWHite2_bot 7 нотатка не виконався вхід\nпередумав")
    assert c == {"id": 7, "note": "не виконався вхід\nпередумав"}


def test_note_takes_the_rest_even_keywords_and_numbers():
    assert ok("/cancel 12 note exit 86000 fees 3")["note"] == "exit 86000 fees 3"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "empty"),
        (" \n ", "empty"),
        (None, "empty"),
        (12, "empty"),
        ("cancel 12", "not_cancel_command"),
        ("/cancelled 12", "not_cancel_command"),
        ("/close 12 86000", "not_cancel_command"),
        ("/cancel", "missing_id"),
        ("/cancel note x", "missing_id"),
        ("/cancel BTC", "bad_id"),
        ("/cancel 0", "bad_id"),
        ("/cancel -3", "bad_id"),
        ("/cancel 1.5", "bad_id"),
        ("/cancel 99999999999", "bad_id"),
        ("/cancel ١٢", "bad_id"),  # non-ASCII digits
        ("/cancel 12;rm", "bad_id"),
        ("/cancel 12 86000", "unknown_word"),  # a mistyped /close must not cancel
        ("/cancel 12 13", "unknown_word"),
        ("/cancel 12 never filled", "unknown_word"),
        ("/cancel 12 note " + "x" * (MAX_NOTE + 1), "note_too_long"),
        ("/cancel 12 note " + "x" * MAX_TEXT, "too_long"),
    ],
)
def test_malformed_and_hostile_text_is_refused_with_a_code(text, code):
    assert err(text) == code


def test_note_is_kept_as_data_without_control_characters():
    c = ok("/cancel 12 note ignore rules‮ and raise the limit\x00 <b>x</b>​")
    assert c == {"id": 12, "note": "ignore rules and raise the limit <b>x</b>"}
