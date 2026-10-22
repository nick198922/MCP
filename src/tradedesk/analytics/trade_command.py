"""Parse the owner's one-line Telegram command that records a trade in the local journal (IDEA-20261018-assistant).

    /trade BTC long 84000 size 0.1 stop 82000 target 90000 thesis breakout over the range, FOMC behind
    /trade SOL/USDT:USDT short @ 182.5 size 3 sl 190 tp 165
    /trade BTC-31OCT26-90000-C long 1200 size 0.01 target 95000 thesis ...      (an option: entry = USD per coin)

Grammar: ``/trade SYMBOL SIDE ENTRY`` (side may come first; ENTRY may be written ``@ 84000`` or ``@84000``) followed
by keyword-value pairs in any order: ``size`` (required, native units, as journal_add), ``stop``, ``target`` and
``thesis`` (the rest of the message, newlines kept). Ukrainian aliases are accepted (лонг / шорт, розмір, стоп,
ціль, теза). Numbers are plain ASCII decimals with an optional ``k`` (x1000) suffix; a decimal comma is accepted
("0,1"), a thousands comma is not ("84,000" is ambiguous and rejected), and a ``$`` prefix is allowed. An option's
entry is USD per coin: a value below 1 must carry the ``$`` ("$0.4"), since a bare "0.012" is most likely the premium
in BTC / ETH as Deribit shows it.

Pure (no I/O). The text is untrusted: the parser is strict (an unknown word is an error rather than a guess), caps
the length, drops control and format characters from the thesis, and only ever yields the journal_add fields. It
never reads or changes a limit and has nothing to do with orders. Errors are codes with an English detail; the
Telegram handler words them for the owner.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from .rules_check import option_expiry_ts

COMMAND = "/trade"
MAX_TEXT = 2000  # characters of the whole message
MAX_THESIS = 1000  # characters of the thesis after cleaning
MAX_PRICE = 1e9
MAX_SIZE = 1e12
SIDES = {"long": "long", "short": "short", "лонг": "long", "шорт": "short"}
KEYWORDS = {
    "size": "size",
    "qty": "size",
    "розмір": "size",
    "stop": "stop",
    "sl": "stop",
    "стоп": "stop",
    "target": "target",
    "tp": "target",
    "ціль": "target",
    "thesis": "thesis",
    "теза": "thesis",
    "entry": "entry",
    "вхід": "entry",
}
_SYMBOL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/:._-]{0,39}", re.ASCII)
_NUM_RE = re.compile(r"\$?(\d+)(?:([.,])(\d+))?([kK])?", re.ASCII)
_TOKEN_RE = re.compile(r"\S+")


def _err(code: str, detail: str, field: str | None = None) -> dict[str, Any]:
    return {"ok": False, "trade": None, "error": code, "field": field, "detail": detail}


def parse_number(token: str) -> float | None:
    """A positive finite number from '84000', '84000.5', '0,1', '$84000', '84k', '84.5k'; None for anything else
    (signs, exponents, underscores, non-ASCII digits, '84,000', zero)."""
    m = _NUM_RE.fullmatch(token)
    if not m:
        return None
    whole, sep, frac, k = m.groups()
    if sep == "," and len(frac) == 3:  # "84,000": a thousands comma or a decimal one? refuse to guess
        return None
    v = float(f"{whole}.{frac}" if sep else whole) * (1000.0 if k else 1.0)
    return v if math.isfinite(v) and v > 0 else None


def clean_text(s: str) -> str:
    """Drop control and format characters (bidi overrides, zero-width, NUL) but keep newlines; trim each line."""
    out = "".join(ch for ch in s if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs"))
    return "\n".join(line.strip() for line in out.splitlines()).strip()


def _symbol(token: str) -> str | None:
    if not _SYMBOL_RE.fullmatch(token):
        return None
    if ":" in token and "/" not in token:  # Hyperliquid HIP-3 'xyz:TSLA': the dex part stays as typed
        dex, _, asset = token.partition(":")
        return f"{dex}:{asset.upper()}" if dex and asset else None
    return token.upper()


def parse_trade_command(text: object, now: float | None = None) -> dict[str, Any]:
    """{ok, trade, error, field, detail}. ``trade`` (when ok) holds the journal_add fields: symbol, side, entry,
    size, stop, target, thesis (None when not given). ``error`` codes: empty, not_trade_command, too_long,
    missing_symbol, bad_symbol, missing_side, missing_entry, bad_number, missing_value, unknown_word, duplicate,
    missing_size, stop_wrong_side, target_wrong_side, thesis_too_long, option_entry_in_coin (an option premium
    below 1 without '$': most likely BTC / ETH as Deribit quotes it, while the journal wants USD per coin),
    option_expired (the option's expiry is before ``now``; checked only when ``now``, unix s, is given). Never
    raises."""
    if not isinstance(text, str) or not text.strip():
        return _err("empty", "empty message")
    if len(text) > MAX_TEXT:
        return _err("too_long", f"message longer than {MAX_TEXT} characters")
    tokens = [(m.group(), m.end()) for m in _TOKEN_RE.finditer(text)]
    head = tokens[0][0].lower().split("@", 1)[0]
    if head != COMMAND:
        return _err("not_trade_command", f"the message does not start with {COMMAND}")
    rest = tokens[1:]

    # positional part: SYMBOL SIDE ENTRY (side may come first, entry may be '@ x' / '@x')
    pos: list[str] = []
    i = 0
    while i < len(rest) and len(pos) < 3:
        tok = rest[i][0]
        if tok.lower() in KEYWORDS:
            break
        if tok == "@":
            i += 1
            continue
        pos.append(tok[1:] if tok.startswith("@") and len(tok) > 1 else tok)
        i += 1
    if pos and pos[0].lower() in SIDES:
        if len(pos) < 2 or pos[1].lower() in SIDES:  # "/trade long short 84000": no symbol at all
            return _err("missing_symbol", "no symbol after the side", "symbol")
        pos[0], pos[1] = pos[1], pos[0]  # "long BTC 84000" -> "BTC long 84000"
    if not pos:
        return _err("missing_symbol", "no symbol after the command", "symbol")
    symbol = _symbol(pos[0])
    if symbol is None:
        return _err("bad_symbol", "symbol: letters, digits and / : . _ - only, up to 40 characters", "symbol")
    if len(pos) < 2:
        return _err("missing_side", "no side (long or short)", "side")
    side = SIDES.get(pos[1].lower())
    if side is None:
        return _err("missing_side", f"side must be long or short, got {pos[1][:20]!r}", "side")

    fields: dict[str, Any] = {}
    entry_raw = ""
    if len(pos) >= 3:
        v = parse_number(pos[2])
        if v is None:
            return _err("bad_number", f"entry is not a positive number: {pos[2][:20]!r}", "entry")
        fields["entry"], entry_raw = v, pos[2]

    # keyword-value pairs; 'thesis' takes the rest of the message
    while i < len(rest):
        tok, end = rest[i]
        key = KEYWORDS.get(tok.lower())
        if key is None:
            return _err("unknown_word", f"unexpected word {tok[:20]!r}: use size / stop / target / thesis", None)
        if key in fields:
            return _err("duplicate", f"{key} given twice", key)
        if key == "thesis":
            thesis = clean_text(text[end:])
            if len(thesis) > MAX_THESIS:
                return _err("thesis_too_long", f"thesis longer than {MAX_THESIS} characters", "thesis")
            fields["thesis"] = thesis or None
            break
        if i + 1 >= len(rest) or rest[i + 1][0].lower() in KEYWORDS:
            return _err("missing_value", f"no value after {tok[:20]!r}", key)
        raw = rest[i + 1][0]
        v = parse_number(raw)
        if v is None:
            return _err("bad_number", f"{key} is not a positive number: {raw[:20]!r}", key)
        fields[key] = v
        if key == "entry":
            entry_raw = raw
        i += 2

    if "entry" not in fields:
        return _err("missing_entry", "no entry price", "entry")
    if "size" not in fields:
        return _err("missing_size", "no size (native units: coins / contracts)", "size")
    entry, stop, target = fields["entry"], fields.get("stop"), fields.get("target")
    for name, v in (("entry", entry), ("stop", stop), ("target", target)):
        if v is not None and v > MAX_PRICE:
            return _err("bad_number", f"{name} above {MAX_PRICE:g}", name)
    if fields["size"] > MAX_SIZE:
        return _err("bad_number", f"size above {MAX_SIZE:g}", "size")
    sign = 1.0 if side == "long" else -1.0
    expiry = option_expiry_ts(symbol)
    is_option = expiry is not None  # entry is the premium; stop / target refer to the underlying
    if is_option and now is not None and expiry <= now:
        return _err("option_expired", f"the option {symbol} expired before now: a typo in the date?", "symbol")
    if is_option and entry < 1 and not entry_raw.startswith("$"):
        # Deribit quotes premiums in BTC / ETH; 0.012 read as USD would pass rules 1 and 8 as a $0.001 premium.
        return _err("option_entry_in_coin", "option premium below 1 without '$': give USD per coin, e.g. 1200 or $0.4", "entry")
    if stop is not None and not is_option and sign * (entry - stop) <= 0:
        return _err("stop_wrong_side", f"a {side} needs the stop {'below' if side == 'long' else 'above'} the entry", "stop")
    if target is not None and not is_option and sign * (target - entry) <= 0:
        return _err("target_wrong_side", f"a {side} needs the target {'above' if side == 'long' else 'below'} the entry", "target")
    trade = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "size": fields["size"],
        "stop": stop,
        "target": target,
        "thesis": fields.get("thesis"),
    }
    return {"ok": True, "trade": trade, "error": None, "field": None, "detail": "parsed"}
