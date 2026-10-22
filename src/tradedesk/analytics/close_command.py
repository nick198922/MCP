"""Parse the owner's one-line Telegram command that closes an open trade in the local journal (IDEA-20261018-assistant).

    /close 12 86000
    /close #12 @ 86000 fees 4.5 note took profit before CPI
    /close 12 ціна 86000 комісія 4,5 нотатка вийшов до CPI

Grammar: ``/close ID EXIT`` (ID = the journal id, ``12`` or ``#12``; EXIT may be written ``@ 86000``, ``@86000`` or
after the keyword ``exit`` / ``price`` / ``ціна``) followed by keyword-value pairs in any order: ``fees`` (USD paid to
open and close, ``0`` allowed; aliases ``fee``, ``комісія``) and ``note`` (the rest of the message, newlines kept;
alias ``нотатка``). Numbers follow analytics.trade_command.parse_number: plain ASCII decimals, optional ``k`` and
``$``, a decimal comma but never a thousands one. For an option the exit is USD per coin like its entry; whether a bare
value below 1 is a coin premium can only be judged against the stored row, so ``exit_raw`` is returned as typed. An exit
of ``0`` (an option that expired worthless) parses as 0.0; only the stored row tells whether it is an option, so the
caller refuses it for a linear trade.

Pure (no I/O). The text is untrusted: the parser is strict (an unknown word is an error rather than a guess), caps the
length, drops control and format characters from the note, and only ever yields the journal_close fields (id, exit,
note) plus fees. It never reads or changes a limit and has nothing to do with orders.
"""

from __future__ import annotations

import re
from typing import Any

from .trade_command import MAX_PRICE, MAX_TEXT, clean_text, parse_number

COMMAND = "/close"
MAX_NOTE = 1000  # characters of the note after cleaning
MAX_ID = 10**9
KEYWORDS = {
    "exit": "exit",
    "price": "exit",
    "ціна": "exit",
    "вихід": "exit",
    "fees": "fees",
    "fee": "fees",
    "комісія": "fees",
    "note": "note",
    "нотатка": "note",
}
_ID_RE = re.compile(r"#?(\d{1,10})", re.ASCII)
_ZERO_RE = re.compile(r"\$?0+(?:[.,]0+)?", re.ASCII)
_TOKEN_RE = re.compile(r"\S+")


def _err(code: str, detail: str, field: str | None = None) -> dict[str, Any]:
    return {"ok": False, "close": None, "exit_raw": None, "error": code, "field": field, "detail": detail}


def parse_close_command(text: object) -> dict[str, Any]:
    """{ok, close, exit_raw, error, field, detail}. ``close`` (when ok) holds id (int), exit (USD per unit, as the
    entry was recorded; 0.0 allowed here, valid only for an option row), fees (USD, None when not given) and note (None when not given); ``exit_raw`` is the exit token
    as typed (without '@'). ``error`` codes: empty, not_close_command, too_long, missing_id, bad_id, missing_exit,
    bad_number, missing_value, unknown_word, duplicate, note_too_long. Never raises."""
    if not isinstance(text, str) or not text.strip():
        return _err("empty", "empty message")
    if len(text) > MAX_TEXT:
        return _err("too_long", f"message longer than {MAX_TEXT} characters")
    tokens = [(m.group(), m.end()) for m in _TOKEN_RE.finditer(text)]
    if tokens[0][0].lower().split("@", 1)[0] != COMMAND:
        return _err("not_close_command", f"the message does not start with {COMMAND}")
    rest = tokens[1:]
    if not rest or rest[0][0].lower() in KEYWORDS:
        return _err("missing_id", "no journal id after the command", "id")
    m = _ID_RE.fullmatch(rest[0][0])
    trade_id = int(m.group(1)) if m else 0
    if not 1 <= trade_id <= MAX_ID:
        return _err("bad_id", f"id must be a positive whole number, got {rest[0][0][:20]!r}", "id")

    fields: dict[str, Any] = {}
    exit_raw = ""
    i = 1
    if i < len(rest) and rest[i][0] == "@":
        i += 1
    if i < len(rest) and rest[i][0].lower() not in KEYWORDS:
        tok = rest[i][0]
        exit_raw = tok[1:] if tok.startswith("@") and len(tok) > 1 else tok
        v = 0.0 if _ZERO_RE.fullmatch(exit_raw) else parse_number(exit_raw)
        if v is None:
            return _err("bad_number", f"exit is not a positive number: {exit_raw[:20]!r}", "exit")
        fields["exit"] = v
        i += 1

    # keyword-value pairs; 'note' takes the rest of the message
    while i < len(rest):
        tok, end = rest[i]
        key = KEYWORDS.get(tok.lower())
        if key is None:
            return _err("unknown_word", f"unexpected word {tok[:20]!r}: use fees / note", None)
        if key in fields:
            return _err("duplicate", f"{key} given twice", key)
        if key == "note":
            note = clean_text(text[end:])
            if len(note) > MAX_NOTE:
                return _err("note_too_long", f"note longer than {MAX_NOTE} characters", "note")
            fields["note"] = note or None
            break
        if i + 1 >= len(rest) or rest[i + 1][0].lower() in KEYWORDS:
            return _err("missing_value", f"no value after {tok[:20]!r}", key)
        raw = rest[i + 1][0]
        if _ZERO_RE.fullmatch(raw):
            v = 0.0  # "fees 0" is a real answer; a zero exit is only valid for an option (checked by the caller)
        else:
            v = parse_number(raw)
        if v is None:
            return _err("bad_number", f"{key} is not a positive number: {raw[:20]!r}", key)
        fields[key] = v
        if key == "exit":
            exit_raw = raw
        i += 2

    if "exit" not in fields:
        return _err("missing_exit", "no exit price", "exit")
    for name in ("exit", "fees"):
        if fields.get(name) is not None and fields[name] > MAX_PRICE:
            return _err("bad_number", f"{name} above {MAX_PRICE:g}", name)
    close = {"id": trade_id, "exit": fields["exit"], "fees": fields.get("fees"), "note": fields.get("note")}
    return {"ok": True, "close": close, "exit_raw": exit_raw, "error": None, "field": None, "detail": "parsed"}
