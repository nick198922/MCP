"""Parse the owner's one-line Telegram command that cancels an open trade in the local journal (IDEA-20261018-assistant).

    /cancel 12
    /cancel #12 note the limit order never filled
    /cancel 12 нотатка не виконався вхід

Grammar: ``/cancel ID`` (ID = the journal id, ``12`` or ``#12``) optionally followed by ``note`` (alias ``нотатка``)
and the rest of the message, newlines kept. A cancelled trade is one that never filled: no exit, no P&L, like
journal_close with ``cancelled=true``. Any other word after the id is an error rather than a guess, so a mistyped
``/close`` (``/cancel 12 86000``) does not cancel.

Pure (no I/O). The text is untrusted: the parser is strict, caps the length, drops control and format characters from
the note (helpers shared with analytics.close_command and analytics.trade_command), and only ever yields the
journal_close fields for a cancel (id, note). It never reads or changes a limit and has nothing to do with orders.
"""

from __future__ import annotations

import re
from typing import Any

from .close_command import _ID_RE, MAX_ID, MAX_NOTE
from .trade_command import MAX_TEXT, clean_text

COMMAND = "/cancel"
NOTE_WORDS = frozenset({"note", "нотатка"})
_TOKEN_RE = re.compile(r"\S+")


def _err(code: str, detail: str, field: str | None = None) -> dict[str, Any]:
    return {"ok": False, "cancel": None, "error": code, "field": field, "detail": detail}


def parse_cancel_command(text: object) -> dict[str, Any]:
    """{ok, cancel, error, field, detail}. ``cancel`` (when ok) holds id (int) and note (None when not given).
    ``error`` codes: empty, not_cancel_command, too_long, missing_id, bad_id, unknown_word, note_too_long. Never raises."""
    if not isinstance(text, str) or not text.strip():
        return _err("empty", "empty message")
    if len(text) > MAX_TEXT:
        return _err("too_long", f"message longer than {MAX_TEXT} characters")
    tokens = [(m.group(), m.end()) for m in _TOKEN_RE.finditer(text)]
    if tokens[0][0].lower().split("@", 1)[0] != COMMAND:
        return _err("not_cancel_command", f"the message does not start with {COMMAND}")
    rest = tokens[1:]
    if not rest or rest[0][0].lower() in NOTE_WORDS:
        return _err("missing_id", "no journal id after the command", "id")
    m = _ID_RE.fullmatch(rest[0][0])
    trade_id = int(m.group(1)) if m else 0
    if not 1 <= trade_id <= MAX_ID:
        return _err("bad_id", f"id must be a positive whole number, got {rest[0][0][:20]!r}", "id")

    note = None
    if len(rest) > 1:
        tok, end = rest[1]
        if tok.lower() not in NOTE_WORDS:
            return _err("unknown_word", f"unexpected word {tok[:20]!r}: only note may follow the id", None)
        note = clean_text(text[end:])
        if len(note) > MAX_NOTE:
            return _err("note_too_long", f"note longer than {MAX_NOTE} characters", "note")
    return {"ok": True, "cancel": {"id": trade_id, "note": note or None}, "error": None, "field": None, "detail": "parsed"}
