from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from mcp.types import ToolAnnotations

from ..analytics.instruments import expiry_token, parse_user_expiry
from ..analytics.options import json_num as json_num  # re-exported: one rule for "a JSON number" in tools and analytics
from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

# All Phase 0-1 tools only read public market data.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

# What a query on the local SQLite store raises when the file is locked, unreadable, on a full disk or gone.
STORE_ERRORS = (sqlite3.Error, OSError)

T = TypeVar("T")


async def store_ready(b: Builder, rt: Runtime, feature: str) -> bool:
    """True when the local store is usable. Otherwise records `missing: store` and returns False (invariant 1):
    reason `config` when it is switched off (empty db_path), `backend_error` when `Store.init()` raised."""
    if rt.store is None:
        b.miss("store", "tradedesk", "config", f"db_path is empty; set TRADEDESK_DB_PATH to enable {feature}")
        return False
    try:
        await rt.store.init()
    except Exception as e:  # noqa: BLE001 - an unusable store is reported in `missing`, never raised
        b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
        return False
    return True


def store_failed(b: Builder, e: BaseException) -> dict[str, Any]:
    """Envelope for a store query that raised one of STORE_ERRORS: `missing: store` (backend_error), ok=false."""
    b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
    return b.build(None, ok=False)


def store_num(x: Any) -> float | None:
    """A finite number read from the store as a float, else None. SQLite keeps whatever another writer put in a REAL
    column (a string, a blob, inf), so tools that sum or compare stored values skip such a cell instead of raising
    (invariant 1)."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except OverflowError:
        return None
    return v if math.isfinite(v) else None


def finite_json(x: Any) -> bool:
    """True when `x` serialises to strict JSON: no NaN / +-inf float and no blob anywhere (a string "inf" is just text)."""
    try:
        json.dumps(x, allow_nan=False)
    except (ValueError, TypeError):
        return False
    return True


def _has_nonfinite(v: Any) -> bool:
    """True when NaN / +-inf sits anywhere in the parsed JSON `v`; iterative, so any nesting depth is safe."""
    stack = [v]
    while stack:
        x = stack.pop()
        if isinstance(x, float) and not math.isfinite(x):
            return True
        if isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            stack.extend(x.values())
    return False


def clean_tags(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[Any], list[Any]]:
    """(kept rows, ids skipped for NaN / Infinity in tags, ids whose tags were normalised). positions_add / journal_add
    store a JSON list of strings; another writer can leave anything. A row with NaN / Infinity is skipped (item 296); any
    other non-list or a list with non-strings is shown as its string items ("x" -> ["x"], 5 / {...} -> []), because a
    nested value 300 levels deep failed the whole reply in the MCP serialiser (items 352, 353). NULL / JSON null stays
    None (no tags). A row whose other cells are not strict JSON is skipped too (counted with the NaN ones)."""
    kept: list[dict[str, Any]] = []
    nonfinite: list[Any] = []
    fixed: list[Any] = []
    for p in rows:
        tags = p.get("tags")
        if not (tags is None or (isinstance(tags, list) and all(isinstance(t, str) for t in tags))):
            if _has_nonfinite(tags):
                nonfinite.append(p.get("id"))
                continue
            if isinstance(tags, str):
                p["tags"] = [tags]
            elif isinstance(tags, list):
                p["tags"] = [t for t in tags if isinstance(t, str)]
            else:
                p["tags"] = []
            fixed.append(p.get("id"))
        if not finite_json(p):
            nonfinite.append(p.get("id"))
            continue
        kept.append(p)
    return kept, nonfinite, fixed


def clean_row_tags(rows: list[dict[str, Any]]) -> tuple[list[Any], list[Any]]:
    """`clean_tags` on the `tags` cell alone, in place: no row is dropped and the other cells are not checked (tools whose
    rows have their own checks, item 356). Returns (ids whose tags hold NaN / Infinity, left as stored for the caller to
    handle; ids whose tags were normalised to their string items)."""
    nonfinite: list[Any] = []
    fixed: list[Any] = []
    for p in rows:
        kept, bad, norm = clean_tags([{"tags": p.get("tags")}])
        if bad:
            nonfinite.append(p.get("id"))
        elif norm:
            p["tags"] = kept[0]["tags"]
            fixed.append(p.get("id"))
    return nonfinite, fixed


def _clean_row_cell(rows: list[dict[str, Any]], col: str, as_dict: bool) -> tuple[list[Any], list[Any]]:
    """Normalise the JSON cell `col` of every row in place to a dict of strings (`as_dict`) or a list of strings. NULL /
    JSON null stays None. A dict keeps only its string values; a list keeps its string items; a bare string becomes
    [s] (lists only); anything else (a number, a bool, the other container type) becomes {} / []. Returns (ids whose
    cell holds NaN / Infinity, left as stored for the caller to handle; ids whose cell was normalised)."""
    nonfinite: list[Any] = []
    fixed: list[Any] = []
    for p in rows:
        v = p.get(col)
        if v is None:
            continue
        if as_dict:
            if isinstance(v, dict) and all(isinstance(x, str) for x in v.values()):
                continue
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            continue
        if _has_nonfinite(v):
            nonfinite.append(p.get("id"))
            continue
        if as_dict:
            p[col] = {str(k): x for k, x in v.items() if isinstance(x, str)} if isinstance(v, dict) else {}
        elif isinstance(v, str):
            p[col] = [v]
        else:
            p[col] = [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
        fixed.append(p.get("id"))
    return nonfinite, fixed


def clean_row_rules(rows: list[dict[str, Any]]) -> tuple[list[Any], list[Any]]:
    """`rules` of trade plans (plan_add writes a JSON object rule -> 'pass' / 'fail' / 'warn') as a dict of strings, in
    place; see `_clean_row_cell`. A nested value 300 levels deep failed the whole reply in the MCP serialiser (item 357)."""
    return _clean_row_cell(rows, "rules", as_dict=True)


def clean_row_flags(rows: list[dict[str, Any]]) -> tuple[list[Any], list[Any]]:
    """`entry_flags` of carry paper pairs (carry_paper_open writes a JSON list of scan flags) as a list of strings, in
    place; see `_clean_row_cell`. A nested value 300 levels deep failed the whole reply in the MCP serialiser (item 357)."""
    return _clean_row_cell(rows, "entry_flags", as_dict=False)


# Nesting levels a free-form stored JSON cell (events.meta, alerts.payload, cp_* payloads) may have in a reply. The MCP
# serialiser (pydantic_core.to_json) fails the whole tool result at about 254 levels from the envelope root ("Circular
# reference detected (depth exceeded)", item 358); real cells nest a few levels and the envelope adds under ten.
MAX_CELL_DEPTH = 64


def json_too_deep(v: Any, limit: int = MAX_CELL_DEPTH) -> bool:
    """True when the parsed JSON `v` nests lists / dicts more than `limit` levels (a scalar is 0, [] is 1, [[]] is 2);
    iterative, so any nesting depth is safe."""
    stack = [(v, 0)]
    while stack:
        x, d = stack.pop()
        if isinstance(x, (list, dict)):
            if d + 1 > limit:
                return True
            stack.extend((y, d + 1) for y in (x.values() if isinstance(x, dict) else x))
    return False


def cap_cell_depth(rows: list[dict[str, Any]], col: str, key: str = "id", as_dict: bool = False) -> list[Any]:
    """Replace the JSON cell `col` of every row nested deeper than MAX_CELL_DEPTH, in place, by None (or {} when
    `as_dict`: callers that read keys from it); returns the `key` of the rows changed, for the caller's warning. A cell
    300 levels deep failed the whole reply in the MCP serialiser (item 358); shallower cells pass unchanged. The type
    checks of `_clean_row_cell` (item 357) do not fit here: these cells are free-form JSON."""
    capped: list[Any] = []
    for p in rows:
        if json_too_deep(p.get(col)):
            p[col] = {} if as_dict else None
            capped.append(p.get(key))
    return capped


def shown_int(v: int) -> str:
    """`v` as text for a message. An integer of 101+ digits is not echoed: str() of one beyond 4300 digits raises
    ValueError (and so did the message of a tool refusing it), and a 400-digit echo helps nobody."""
    return str(v) if -(10**100) < v < 10**100 else "an integer beyond +-1e100"


def reject_bad_ts(b: Builder, field: str, ts: int | None) -> dict[str, Any] | None:
    """ok=false envelope when unix seconds `ts` fit no datetime (10**20 also overflows the SQLite INTEGER binding and
    raised out of the tool; a millisecond value is beyond the year 9999) or are negative (before 1970: no trade time),
    else None."""
    if ts is None:
        return None
    try:
        datetime.fromtimestamp(ts, UTC)
    except (ValueError, OverflowError, OSError):
        b.miss(field, "tradedesk", "invalid_input", f"{field}={shown_int(ts)} is outside the datetime range (unix seconds expected)")
        return b.build(None, ok=False)
    if ts < 0:
        b.miss(field, "tradedesk", "invalid_input", f"{field}={ts} is negative (before 1970); unix seconds >= 0 expected")
        return b.build(None, ok=False)
    return None


SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1


def reject_bad_id(b: Builder, **ids: int | None) -> dict[str, Any] | None:
    """ok=false envelope (`invalid_input`) when a row id is outside SQLite INTEGER (-2**63 .. 2**63 - 1), else None.
    10**20 and -10**20 raised OverflowError out of the store lookup; no such row can exist. The value is not echoed
    (str() of a 5000-digit int raises)."""
    for field, v in ids.items():
        if v is not None and not (SQLITE_INT_MIN <= v <= SQLITE_INT_MAX):
            msg = f"{field} is beyond the SQLite INTEGER range (-2**63 .. 2**63 - 1); no such row exists"
            b.miss(field, "tradedesk", "invalid_input", msg)
            return b.build(None, ok=False)
    return None


def drop_corrupt_rows(
    b: Builder,
    rows: list[dict[str, Any]],
    what: str,
    numeric: tuple[str, ...],
    required: tuple[str, ...] = (),
    key: str = "id",
    flags: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """`rows` without the ones another writer corrupted, with a warning naming them by `key` (invariant 1). A row is
    dropped when any cell is a blob (an invalid UTF-8 blob fails the JSON reply of the whole tool), a `numeric` cell is
    neither NULL nor a finite number (a string, inf), a `required` cell is not a finite number, or a `flags` cell is not
    a bool (the store turns only a stored 0 / 1 into one). Valid rows pass unchanged."""
    kept: list[dict[str, Any]] = []
    bad: list[Any] = []
    for row in rows:
        ok = (
            not any(isinstance(v, (bytes, bytearray, memoryview)) for v in row.values())
            and all(row.get(k) is None or store_num(row.get(k)) is not None for k in numeric)
            and all(store_num(row.get(k)) is not None for k in required)
            and all(isinstance(row.get(k), bool) for k in flags)
        )
        if ok:
            kept.append(row)
        else:
            bad.append(row.get(key))
    if bad:
        shown = [i if isinstance(i, int) and not isinstance(i, bool) else None for i in bad[:10]]
        flag_txt = f", or a non-0/1 flag {', '.join(flags)}" if flags else ""
        b.warn(
            f"{len(bad)} stored {what} rows skipped: a blob, or a non-numeric / non-finite {', '.join(numeric)}{flag_txt}; {key} {shown}"
        )
    return kept


def warn_incomplete_range(b: Builder, field: str, got: dict[str, Any]) -> None:
    """Warnings for a ``{"rows", "complete"[, "truncated", "dropped_rows"]}`` range result. ``dropped_rows > 0`` (ccxt
    funding: rows that are not a dict or whose timestamp is missing or not an integer) gets its own warning with the
    count. The paging warning is kept when ``truncated`` is set, or when ``complete`` is False and no drop explains it
    (providers without ``truncated`` mean "paging stopped early" by ``complete=False``). Complete results: no warning."""
    raw = got.get("dropped_rows")
    dropped = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0
    if dropped:
        b.warn(
            f"{field}: {dropped} malformed rows dropped (not an object, or the timestamp missing or not an integer); "
            "the series may have holes."
        )
    if got.get("truncated") is True or (not got.get("complete", True) and not dropped):
        b.warn(f"{field}: history incomplete (paging stopped early); the earliest part of the window may be missing.")


def parsed_rows(rows: Any, parse: Callable[[Any], T]) -> list[T]:
    """`parse` applied to every row of a venue answer. A row it cannot parse (a field that is null, a string or a list
    where a number belongs, or an infinity: TypeError / ValueError / KeyError / IndexError / OverflowError) is dropped,
    as rows with an absent field already are, so a wrong-type field never escapes the tool (invariant 1). Well-formed
    rows parse exactly as before."""
    out: list[T] = []
    for row in rows or []:
        try:
            out.append(parse(row))
        except (TypeError, ValueError, KeyError, IndexError, OverflowError):
            continue
    return out


MAX_BAR_TS_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z in unix ms: a later bar time fits no datetime
MAX_BAR_PRICE = 1e12  # far above any listed quote price; 1e308 in a bar is garbage, not a price
MAX_FUNDING_RATE = 1.0  # 100 % per interval, far beyond any venue's cap; a 1e308 rate overflowed the carry to inf
MAX_BAR_VOLUME = 1e20  # far above any bar's volume (a 1e-9 meme coin trades ~1e15 a day); sums of 1e308 overflow to inf


def bar_row(x: Any) -> list[Any] | None:
    """A ccxt OHLCV row ``[ts_ms, open, high, low, close(, volume)]`` fit to show and compute on, else None.

    The timestamp must be an integer unix ms in 0 .. year 9999 (an integral float counts), every price a finite number
    in (0, MAX_BAR_PRICE], the volume (when the row has one) null or a finite number in [0, MAX_BAR_VOLUME]. A string,
    bool, NaN, +-inf, 1e308 or an integer beyond the float range fails, so one bad bar cannot raise out of a tool or
    turn its reply into NaN / Infinity (invariant 1). ccxt hands out numbers: a numeric string is not a price here
    (venues that send strings parse them first, see `hl_bar_row`). Valid rows come back as ``[int, float x4(, v)]``."""
    if not isinstance(x, (list, tuple)) or len(x) < 5:
        return None
    ts = x[0]
    if isinstance(ts, float) and ts.is_integer():  # False for NaN / inf
        ts = int(ts)
    if not isinstance(ts, int) or isinstance(ts, bool) or not 0 <= ts <= MAX_BAR_TS_MS:
        return None
    prices = [store_num(v) for v in x[1:5]]
    if any(p is None or not 0 < p <= MAX_BAR_PRICE for p in prices):
        return None
    out: list[Any] = [ts, *prices]
    if len(x) >= 6:
        vol = x[5] if x[5] is None else store_num(x[5])
        if x[5] is not None and (vol is None or not 0 <= vol <= MAX_BAR_VOLUME):
            return None
        out.append(vol)
    return out


def hl_bar_row(c: Any) -> list[Any] | None:
    """A Hyperliquid candle ``{t, o, h, l, c, v}`` (prices and volume as decimal strings) as a `bar_row`, else None."""
    if not isinstance(c, dict):
        return None
    try:
        row = [c["t"], *(float(c[k]) for k in ("o", "h", "l", "c", "v"))]
    except (KeyError, TypeError, ValueError, OverflowError):  # OverflowError: float() of a 400-digit JSON integer
        return None
    return bar_row(row)


def bar_rows(raw: Any, parse: Callable[[Any], list[Any] | None] = bar_row) -> tuple[list[list[Any]], int]:
    """(rows that `parse` keeps, how many it dropped) for a venue's bar answer; an answer that is not a list has none."""
    rows = raw if isinstance(raw, list | tuple) else []
    kept = [p for x in rows if (p := parse(x)) is not None]
    return kept, len(rows) - len(kept)


def finite_float(x: Any) -> float:
    """`float(x)` that raises ValueError for NaN and +-inf as well (JSON `NaN` / `Infinity` arrive as floats, and
    float() accepts the strings "nan" / "Infinity"), so `parsed_rows` drops such a row like any malformed one instead
    of letting one non-finite funding rate poison a mean or a sum of payments."""
    v = float(x)
    if not math.isfinite(v):
        raise ValueError(f"non-finite number: {x!r}")
    return v


def first_nonfinite(x: Any, path: str = "") -> str | None:
    """Path of the first NaN / +-inf number in `x` (walks dicts and lists; a bool or a string is not a number), else None.

    The schema of a float parameter lets NaN and +-inf through (the strings "inf" / "nan" / "1e400" coerce to them, and
    so does the JSON number 1e400), so calculator tools check their arguments and their result with this before
    answering: a non-finite number in the result would show up as NaN / Infinity in the text and as null in the
    structured content (invariant 1: an answer, never a silent garbage number)."""
    if isinstance(x, float):
        return None if math.isfinite(x) else (path or "value")
    if isinstance(x, int) and not isinstance(x, bool):
        try:
            return None if math.isfinite(float(x)) else (path or "value")
        except OverflowError:  # an int beyond the float range (e.g. 10**400 inside a leg dict)
            return path or "value"
    if isinstance(x, dict):
        items = x.items()
    elif isinstance(x, list | tuple):
        items = enumerate(x)
    else:
        return None
    for k, v in items:
        if isinstance(v, str):
            try:
                if not math.isfinite(float(v)):
                    return f"{path}.{k}" if path else str(k)
            except (ValueError, OverflowError):
                pass
            continue
        bad = first_nonfinite(v, f"{path}.{k}" if path else str(k))
        if bad is not None:
            return bad
    return None


def reject_nonfinite(b: Builder, **named: Any) -> dict[str, Any] | None:
    """ok=false envelope (`missing: input`, reason invalid_input) when an argument holds NaN / +-inf / an int beyond the
    float range, else None. Strings inside lists and dicts count when float() reads them as non-finite ("inf")."""
    bad = first_nonfinite(named)
    if bad is None:
        return None
    b.miss("input", "tradedesk", "invalid_input", f"{bad} is not a finite number (NaN, inf or beyond the float range)")
    return b.build(None, ok=False)


def reject_nonfinite_result(b: Builder, data: Any) -> dict[str, Any] | None:
    """ok=false envelope when the computed result holds NaN / +-inf (finite but extreme inputs that overflow), else None."""
    bad = first_nonfinite(data)
    if bad is None:
        return None
    b.miss("result", "tradedesk", "invalid_input", f"{bad} came out NaN or inf: the inputs are outside the model's numeric range")
    return b.build(None, ok=False)


def r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(float(x), nd)


def find_expiry(by_expiry: dict[datetime, Any], text: str | None) -> tuple[datetime | None, str | None]:
    """Resolve a user-supplied expiry ('26MAR27' or '2027-03-26'). Returns (expiry, error_text)."""
    available = ", ".join(expiry_token(e) for e in by_expiry)
    if not text:
        return None, f"expiry is required. Available: {available}"
    day = parse_user_expiry(text)
    if day is None:
        return None, f"cannot parse expiry '{text}' (use e.g. 26MAR27 or 2027-03-26). Available: {available}"
    for e in by_expiry:
        if e.date() == day:
            return e, None
    return None, f"no listed expiry on {day.isoformat()}. Available: {available}"


def timeframe_seconds(tf: str) -> float:
    """Seconds in one ccxt timeframe: '1m', '4h', '1d', '1w' and '1M' (a month, taken as 30 days).

    Raises ValueError for anything else; tools catch it and report `missing: timeframe` instead of raising.
    """
    unit = tf[-1:]
    # ccxt spells minutes 'm' and months 'M'; decide the month before lower-casing.
    mult = 30 * 86400 if unit == "M" else {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit.lower())
    if mult is None or not tf[:-1].isdigit():
        raise ValueError(f"bad timeframe '{tf}'")
    return int(tf[:-1]) * mult
