"""Parse the owner's one-line Telegram command that checks a planned trade against his rules before it is placed
(IDEA-20261018-assistant, «Правила власника»). Nothing is recorded: the check only reads the journal.

    /check BTC call premium 80 expiry 36h target 95000 thesis ...       an option purchase, expiry in hours / days
    /check BTC-31OCT26-90000-C premium 80 target 95000 thesis ...       the expiry from the option symbol
    /check SOL long 180 stop 170 target 200 risk 90 thesis ...          a linear trade (perp), risk in USD to the stop
    /check BTC long 84000 size 0.1 stop 82000 target 90000 thesis ...   a /trade line works as it is (except an
                                                                        option sale, which /check refuses)

Grammar: ``/check SYMBOL [SIDE | KIND] [ENTRY]`` (the side or kind word may come first; ENTRY may be ``@ 84000``),
then keyword-value pairs in any order, ``thesis`` taking the rest of the message. SIDE is long / short (лонг /
шорт); KIND is call / put / option (кол / пут / опціон) and makes it an option purchase on the underlying SYMBOL,
as does an option symbol with a date (Deribit / Bybit, 'BTC-31OCT26-90000-C'), which needs no side word. Keywords:
the /trade ones (size, stop, target, thesis, entry and their aliases) plus ``premium`` (премія: USD paid in total),
``risk`` (ризик: USD to the stop) and ``expiry`` (exp, експірація: hours or days to the delivery, '36h', '2d',
'1.5d', '36г', '2д'). Numbers are analytics.trade_command.parse_number (positive ASCII decimals, 'k', '$').

* An option is a purchase only (a sale's risk depends on a stop or wings the line cannot show): ``premium`` (a
  value below 1 only with '$': below that it is a premium in BTC / ETH) or ``entry`` + ``size`` (USD per coin, as
  /trade, a value below 1 only with '$'); ``target`` is the underlying price; ``stop`` and ``risk`` are refused (the
  risk of a bought option is its premium). The expiry comes from the symbol or from ``expiry``, never both; a kind
  word that contradicts a dated symbol's C / P is an error.
* A linear trade needs a side and an entry; ``size`` or ``risk`` (not both), and ``risk`` needs a ``stop`` (the
  size is then risk / |entry - stop|); stop and target on the right side of the entry, as /trade.

Pure (no I/O, time passed in). The text is untrusted: the parser is strict (an unknown word is an error rather than
a guess), caps the length and drops control and format characters (bidi, zero-width, NUL) from the thesis with the
/trade helpers. It yields the fields of a hypothetical journal row (as_journal_row) for analytics.rules_check, never
writes anything, never reads or changes a limit and has nothing to do with orders.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .rules_check import option_expiry_ts
from .trade_command import KEYWORDS as TRADE_KEYWORDS
from .trade_command import MAX_PRICE, MAX_SIZE, MAX_TEXT, MAX_THESIS, SIDES, _symbol, clean_text, parse_number

COMMAND = "/check"
CHECK_ID = "check"  # id of the hypothetical row among the journal's (theirs are integers)
MAX_EXPIRY_HOURS = 366 * 24.0
KEYWORDS = {
    **TRADE_KEYWORDS,
    "premium": "premium",
    "премія": "premium",
    "risk": "risk",
    "ризик": "risk",
    "expiry": "expiry",
    "exp": "expiry",
    "експірація": "expiry",
}
KINDS = {"call": "call", "put": "put", "кол": "call", "пут": "put", "option": None, "опціон": None}
_DAY_UNITS = frozenset({"d", "д", "дн"})
_EXPIRY_RE = re.compile(r"(\d{1,5})(?:[.,](\d{1,3}))?(h|d|г|год|д|дн)", re.ASCII)
_TOKEN_RE = re.compile(r"\S+")


def _err(code: str, detail: str, field: str | None = None) -> dict[str, Any]:
    return {"ok": False, "check": None, "error": code, "field": field, "detail": detail}


def parse_expiry_hours(token: str) -> float | None:
    """Hours from '36h', '2d', '1.5d', '1,5d', '36г', '36год', '2д', '2дн'; None for anything else or not in
    (0, MAX_EXPIRY_HOURS]."""
    m = _EXPIRY_RE.fullmatch(token.lower())
    if not m:
        return None
    whole, frac, unit = m.groups()
    v = float(f"{whole}.{frac}" if frac else whole) * (24.0 if unit in _DAY_UNITS else 1.0)
    return v if math.isfinite(v) and 0 < v <= MAX_EXPIRY_HOURS else None


def _word(tok: str) -> bool:
    w = tok.lower()
    return w in SIDES or w in KINDS


def _right_of(symbol: str) -> str | None:
    parts = symbol.split("-")
    return {"C": "call", "P": "put"}.get(parts[3]) if len(parts) > 3 else None


def parse_check_command(text: object, now: float) -> dict[str, Any]:
    """{ok, check, error, field, detail}. ``check`` (when ok): kind ('option' | 'linear'), symbol, right ('call' |
    'put' | None), side, entry, size (None when not given or derived), premium (USD, an option), risk (USD: the
    premium of an option, risk or size x |entry - stop| of a linear trade, else None), stop, target, thesis,
    expiry_ts (unix s, an option), expiry_from ('symbol' | 'text'). ``now`` is unix s. ``error`` codes: empty,
    not_check_command, too_long, missing_symbol, bad_symbol, missing_side, missing_entry, bad_number, missing_value,
    unknown_word, duplicate, missing_size, stop_wrong_side, target_wrong_side, thesis_too_long,
    option_entry_in_coin, option_premium_in_coin, option_expired, option_sale, option_field, kind_conflict,
    missing_premium, premium_and_entry, missing_expiry, expiry_conflict, bad_expiry, not_an_option, size_and_risk,
    risk_needs_stop. Never raises."""
    if not isinstance(text, str) or not text.strip():
        return _err("empty", "empty message")
    if len(text) > MAX_TEXT:
        return _err("too_long", f"message longer than {MAX_TEXT} characters")
    tokens = [(m.group(), m.end()) for m in _TOKEN_RE.finditer(text)]
    if tokens[0][0].lower().split("@", 1)[0] != COMMAND:
        return _err("not_check_command", f"the message does not start with {COMMAND}")
    rest = tokens[1:]

    # positional part: SYMBOL [SIDE | KIND] [ENTRY]
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
    if pos and _word(pos[0]):
        if len(pos) < 2 or _word(pos[1]):
            return _err("missing_symbol", "no symbol after the side or kind", "symbol")
        pos[0], pos[1] = pos[1], pos[0]  # "long SOL 180" -> "SOL long 180"
    if not pos:
        return _err("missing_symbol", "no symbol after the command", "symbol")
    symbol = _symbol(pos[0])
    if symbol is None:
        return _err("bad_symbol", "symbol: letters, digits and / : . _ - only, up to 40 characters", "symbol")
    sym_expiry = option_expiry_ts(symbol)
    side: str | None = None
    right = _right_of(symbol) if sym_expiry is not None else None
    kind_word = False
    entry_tok: str | None = None
    extra = pos[2:]
    if len(pos) >= 2:
        w = pos[1].lower()
        if w in SIDES:
            side = SIDES[w]
        elif w in KINDS:
            if right is not None and KINDS[w] is not None and KINDS[w] != right:
                return _err("kind_conflict", f"{pos[1][:20]!r} contradicts the option symbol {symbol}: a typo?", "side")
            kind_word = True
            right = right or KINDS[w]
        elif sym_expiry is not None:  # an option symbol needs no side word: this is the entry
            entry_tok = pos[1]
        else:
            return _err("missing_side", f"after the symbol: long / short or call / put, got {pos[1][:20]!r}", "side")
    if extra:
        if entry_tok is not None:
            return _err("unknown_word", f"unexpected word {extra[0][:20]!r}", None)
        entry_tok = extra[0]

    fields: dict[str, Any] = {}
    entry_raw = premium_raw = ""
    if entry_tok is not None:
        v = parse_number(entry_tok)
        if v is None:
            return _err("bad_number", f"entry is not a positive number: {entry_tok[:20]!r}", "entry")
        fields["entry"], entry_raw = v, entry_tok

    # keyword-value pairs; 'thesis' takes the rest of the message
    while i < len(rest):
        tok, end = rest[i]
        key = KEYWORDS.get(tok.lower())
        if key is None:
            return _err("unknown_word", f"unexpected word {tok[:20]!r}", None)
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
        if key == "expiry":
            h = parse_expiry_hours(raw)
            if h is None:
                return _err("bad_expiry", f"expiry: hours or days to the delivery, e.g. 36h or 2d, got {raw[:20]!r}", key)
            fields[key] = h
        else:
            v = parse_number(raw)
            if v is None:
                return _err("bad_number", f"{key} is not a positive number: {raw[:20]!r}", key)
            fields[key] = v
            if key == "entry":
                entry_raw = raw
            elif key == "premium":
                premium_raw = raw
        i += 2

    for name in ("entry", "stop", "target", "premium", "risk"):
        if fields.get(name) is not None and fields[name] > MAX_PRICE:
            return _err("bad_number", f"{name} above {MAX_PRICE:g}", name)
    if fields.get("size") is not None and fields["size"] > MAX_SIZE:
        return _err("bad_number", f"size above {MAX_SIZE:g}", "size")
    entry, size, stop, target = fields.get("entry"), fields.get("size"), fields.get("stop"), fields.get("target")
    base = {"symbol": symbol, "stop": stop, "target": target, "thesis": fields.get("thesis")}

    if sym_expiry is not None or kind_word:
        if side == "short":
            return _err("option_sale", "only an option purchase is checked: a sale's risk needs a stop or wings", "side")
        for k in ("stop", "risk"):
            if k in fields:
                return _err("option_field", f"{k} is not for a bought option: its risk is the premium", k)
        if "premium" in fields:
            if entry is not None or size is not None:
                return _err("premium_and_entry", "give premium (USD in total) or entry + size, not both", "premium")
            premium = fields["premium"]
            if premium < 1 and not premium_raw.startswith("$"):  # Deribit quotes premiums in BTC / ETH
                return _err("option_premium_in_coin", "option premium below 1 without '$': give USD in total", "premium")
        else:
            if entry is None:
                return _err("missing_premium", "no premium (USD paid in total)", "premium")
            if size is None:
                return _err("missing_size", "entry is USD per coin: add size, or give premium in total", "size")
            if entry < 1 and not entry_raw.startswith("$"):
                return _err("option_entry_in_coin", "option entry below 1 without '$': give USD per coin", "entry")
            premium = entry * size
        if sym_expiry is not None:
            if "expiry" in fields:
                return _err("expiry_conflict", "the option symbol already carries the expiry", "expiry")
            if sym_expiry <= now:
                return _err("option_expired", f"the option {symbol} expired before now: a typo in the date?", "symbol")
            expiry_ts, expiry_from = sym_expiry, "symbol"
        else:
            if "expiry" not in fields:
                return _err("missing_expiry", "no expiry: hours or days to the delivery, e.g. expiry 36h", "expiry")
            # from the whole second the row opens at (as_journal_row), so 'expiry 72h' is 72 h, not 72 h + a fraction
            expiry_ts, expiry_from = int(now) + fields["expiry"] * 3600.0, "text"
        check = {
            **base,
            "kind": "option",
            "right": right,
            "side": "long",
            "entry": entry,
            "size": size,
            "premium": premium,
            "risk": premium,
            "expiry_ts": expiry_ts,
            "expiry_from": expiry_from,
        }
        return {"ok": True, "check": check, "error": None, "field": None, "detail": "parsed"}

    if side is None:
        return _err("missing_side", "no side (long or short) or option kind (call / put)", "side")
    for k in ("premium", "expiry"):
        if k in fields:
            return _err("not_an_option", f"{k} is for an option: name it call / put or give an option symbol", k)
    if entry is None:
        return _err("missing_entry", "no entry price", "entry")
    if size is not None and "risk" in fields:
        return _err("size_and_risk", "give size or risk, not both", "risk")
    if "risk" in fields and stop is None:
        return _err("risk_needs_stop", "risk is measured to the stop: add stop", "stop")
    sign = 1.0 if side == "long" else -1.0
    if stop is not None and sign * (entry - stop) <= 0:
        return _err("stop_wrong_side", f"a {side} needs the stop {'below' if side == 'long' else 'above'} the entry", "stop")
    if target is not None and sign * (target - entry) <= 0:
        return _err("target_wrong_side", f"a {side} needs the target {'above' if side == 'long' else 'below'} the entry", "target")
    risk = fields.get("risk")
    if risk is not None:
        size = risk / abs(entry - stop)  # type: ignore[operator]
    elif size is not None and stop is not None:
        risk = size * abs(entry - stop)
    check = {
        **base,
        "kind": "linear",
        "right": None,
        "side": side,
        "entry": entry,
        "size": size,
        "premium": None,
        "risk": risk,
        "expiry_ts": None,
        "expiry_from": None,
    }
    return {"ok": True, "check": check, "error": None, "field": None, "detail": "parsed"}


def as_journal_row(check: dict[str, Any], now: float) -> dict[str, Any]:
    """The planned trade as an open journal row opened at ``now`` (id CHECK_ID), the shape
    analytics.rules_check.trade_checks / open_risk / day_stops read. A bought option given by its total premium is
    one unit at that premium (size x entry = premium); one on the underlying (no dated symbol) is tagged 'option'
    and carries ``expiry_ts``. Never stored."""
    is_opt = check["kind"] == "option"
    if is_opt and check["entry"] is None:
        size, entry = 1.0, check["premium"]
    else:
        size, entry = check["size"], check["entry"]
    return {
        "id": CHECK_ID,
        "opened_ts": int(now),  # as the /trade row (journal_trade_tg), so both give the same statuses
        "closed_ts": None,
        "status": "open",
        "symbol": check["symbol"],
        "side": check["side"],
        "size": size,
        "entry": entry,
        "stop": check["stop"],
        "target": check["target"],
        "thesis": check["thesis"],
        "tags": ["option"] if is_opt else [],
        "expiry_ts": check["expiry_ts"] if is_opt else None,
        "pnl": None,
    }
