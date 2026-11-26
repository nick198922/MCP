"""Daily CPI snapshot: the Cleveland Fed inflation nowcast and the books of the open Polymarket CPI brackets.

Docs: docs/ideas/IDEA-20261028-pm-cpi-brackets-vs-nowcast-forward.md (forward record T-7 / T-3 / T-1 of every CPI
release), item 480 of docs/ideas/IDEA-20261102-standing-improvements.md. Polymarket keeps no history of its books, so a
day without this row is lost for good; the nowcast copy of the day also shows whether Cleveland rewrites past daily
points (the vintage condition of item 479). Read-only, no key; nothing here trades.

One row per UTC day, weekends included (a T-3 / T-1 entry can fall on one), in ``pm_cpi_snapshot`` (schema 44), built
in the window 15:30-17:00 UTC (the files update at ~10:00 ET = 15:00 UTC on working days). The stored row is the job's
state, so a service restart inside the window neither duplicates the row (key = day) nor loses it. Each tick
(``analytics.pm_cpi.plan``):

* nowcast (``rt.clevelandfed``, both files): fetched when a file is missing or unreadable, and on Mon-Fri again 30 min
  after the last download while the files are not this day's (stamp or last daily point), at most 3 downloads a day; a
  weekend or a US holiday therefore ends as ``status = 'stale'`` with the books written, not as a missing day. A
  readable download replaces the stored file unless it is older or the same bytes; a failed one keeps what is stored;
  one that does not parse is kept as raw bytes (marked in ``missing``) only while nothing is stored for its kind. Bytes
  equal (sha256) to the latest earlier row's stored copy are not stored again: ``cf_*_ref`` names that row's day. A
  download with the same stamp and points as today's stored copy but other bytes replaces it, except when those bytes
  are any earlier row's copy (A -> B -> A keeps B, also with a raw page stored in between: the rewrite item 479 looks
  for).
* books (``rt.polymarket``: Gamma ``/events/keyset`` per series, CLOB ``POST /books`` of the YES tokens): fetched while a
  series was not listed yet today (a main series answering 0 open brackets does not count as listed), a bracket has no
  book, or a book was read before the stored nowcast was downloaded. That last case is the pairing rule (review C of
  item 480, blocker 1): when a tick stores a file (today's copy on the retry), every book read before it is read again
  in the same tick; the earlier book moves to ``books_early`` with its own ``read_ts`` and ``superseded_ts`` and is
  never dropped, even when the re-read fails (then the main set keeps it and the row stays ``partial``). A bracket gets
  ``gone_ts`` (in ``books``) only on proof that its market closed: ``closed: true`` in its series' listing, or, when it
  is missing from a complete listing and the CLOB gave no book of it either, Gamma ``/markets/{id}`` saying ``closed``
  (``CONFIRM_MAX`` requests a tick at most). A gone bracket keeps its book but is left out of the pair checks and is not
  asked again. Missing from the listing alone proves nothing (a short Gamma answer, a market without its token): the
  bracket stays live, its stored token is read in the same ``/books``, and a book not got lands in ``missing``. Every
  level of every book, both sides, is kept compressed in ``books_depth_z``.
* after 17:00 UTC (until 21:00) only what is still missing is fetched, and the row gets ``late = 1`` when that brought
  a file or a book.

``status = 'complete'`` / ``'stale'`` only with both files readable and the book of every live (not gone) bracket of
the main set read at or after their download (``books_paired = 1``). A provider failure never raises: it lands in the row's ``missing`` (and the log)
and is retried on the next tick. Off by default: TRADEDESK_COLLECT_PM_CPI_ENABLED=1.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import zlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..analytics.pm_cpi import (
    as_listed,
    book_depth,
    books_complete,
    books_due,
    books_of,
    books_paired,
    brackets,
    closed_tokens,
    confirms_close,
    file_key,
    is_fresh,
    latest_value,
    live,
    mark_gone,
    merge_brackets,
    newer_or_same,
    nowcast_summary,
    nowcast_ts,
    paired,
    phase,
    plan,
    prev_target,
    quoted,
    readable,
    with_book,
)
from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

# Polymarket series of the US CPI bracket events (scripts/verify_pm_cpi_step0.py --stage discover, 26.11): 10007 headline
# YoY, 10008 headline MoM, 11905 core YoY, 12054 core MoM; 5 is the older inflation series (no open event on 26.11).
SERIES = ("5", "10007", "10008", "11905", "12054")
# A listing of 0 open brackets in one of these is a gap (a failed or cached answer), not a fact: not counted as listed,
# asked again next tick (review C of item 480, remark 3). Between a release and the next event it can also be true.
MAIN_SERIES = ("10007", "10008", "11905", "12054")
BOOK_CHUNK = 50  # tokens per CLOB POST /books (as in collector/pm_gap.py)
# Gamma /markets/{id} requests a tick at most (blocker 1 of review C2): asked only for a live bracket that is missing
# from an answered listing of its series and got no book in the same tick; closed -> gone, never asked again. 20 covers
# one release (4 events, 39 brackets on 26.11) in two ticks; a day costs at most CONFIRM_MAX x BOOKS_MAX_TRIES, and only
# while Gamma keeps calling open a market it does not list and the CLOB has no book of it.
CONFIRM_MAX = 20
MAX_TICK_SECONDS = 1800  # the scheduler clamps the cadence to this, so the 90 min window always gets ticks
# ... and to at least this (finding 15 of review C): a nowcast file is stored by about the 19th tick of the day at the
# latest (every tick while a file is missing, at most CF_MAX_TRIES = 12; once both are readable, only retries inside the
# window: 90 min / 300 s = 18 ticks), so the books attempts still allow the re-read in that tick, with room for extra
# ticks after a restart; BOOKS_MAX_TRIES = 80 also covers every 300 s tick to 21:00 (66) when the books stay due
# (finding 3 of review C2: 40 ran out at 18:45). With 60 s ticks the attempts ran out at ~16:10, before a file of 16:30.
MIN_TICK_SECONDS = 300
FILES = ("year", "month")
ZLIB_LEVEL = 9


def _ts(rt: "Runtime") -> int:
    return int(rt.now().timestamp())


def _hm(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M")


def _blank(day: str, ts: int) -> dict[str, Any]:
    return {"day": day, "created_ts": ts, "status": "partial", "late": 0, "cf_tries": 0, "cf_fetches": 0, "books_tries": 0,
            "n_events": 0, "n_brackets": 0, "n_quoted": 0}  # fmt: skip


def _obj(raw: Any) -> dict[str, Any]:
    """A JSON object column of the row (our own data) decoded; {} when absent."""
    try:
        v = json.loads(raw) if isinstance(raw, str | bytes) else None
    except (ValueError, RecursionError):
        return {}
    return v if isinstance(v, dict) else {}


def _stored_summary(row: dict[str, Any], kind: str) -> dict[str, Any] | None:
    if not readable(row, kind):
        return None
    return {"stamp": row.get(f"cf_stamp_{kind}"), "last_day": row.get(f"cf_last_day_{kind}")}


async def _keep_file(rt: "Runtime", row: dict[str, Any], kind: str, raw: bytes, sha: str) -> None:
    """Put one downloaded file in the row: compressed, or only as a reference to the latest earlier row that stores the
    same bytes (weekends, holidays). The summary columns are cleared; the caller fills them when the file parsed."""
    src = await rt.store.pm_cpi_file_source(row["day"], kind)
    if src is not None and src[1] == sha:
        row[f"cf_{kind}_z"], row[f"cf_{kind}_ref"] = None, src[0]
    else:
        row[f"cf_{kind}_z"] = await asyncio.to_thread(zlib.compress, raw, ZLIB_LEVEL)
        row[f"cf_{kind}_ref"] = None
    row[f"cf_{kind}_sha"], row[f"cf_{kind}_ts"] = sha, _ts(rt)
    row[f"cf_stamp_{kind}"] = row[f"cf_last_day_{kind}"] = None


async def _nowcast(rt: "Runtime", b: Builder, row: dict[str, Any], day: str) -> bool:
    """Fetch both files and store each one as the module docstring says. True = a stored file changed this tick."""
    row["cf_tries"] = int(row.get("cf_tries") or 0) + 1
    points = _obj(row.get("cf_points"))
    got = changed = False
    for kind in FILES:
        data = await b.guard(f"nowcast_{kind}", "clevelandfed", rt.clevelandfed.nowcast(kind))
        if not data:
            continue
        got = True
        raw = bytes(data["raw"])
        sha = hashlib.sha256(raw).hexdigest()
        s, why = None, data.get("problem")
        if why is None:
            try:
                s = await asyncio.to_thread(nowcast_summary, data["charts"], day)
            except Exception as e:  # noqa: BLE001 - an unreadable file is recorded, its bytes and the books still kept
                why = f"unreadable charts: {type(e).__name__}: {e}"
        if s is not None and (s["target"] is None or s["last_day"] is None):
            s, why = None, "no readable target chart"
        if s is None:
            if row.get(f"cf_{kind}_sha") is None:
                await _keep_file(rt, row, kind, raw, sha)
                points.pop(kind, None)
                changed = True
                b.miss(f"nowcast_{kind}", "clevelandfed", "backend_error", f"{why}; raw file kept ({len(raw)} bytes)")
            elif readable(row, kind):
                b.warn(f"nowcast_{kind}: unreadable download ({why}), kept the stored copy")
            else:
                b.miss(f"nowcast_{kind}", "clevelandfed", "backend_error", f"{why}; the first unreadable copy is kept")
            continue
        if sha == row.get(f"cf_{kind}_sha") and readable(row, kind):
            continue  # the same bytes as stored: cf_<kind>_ts keeps their first download
        old = _stored_summary(row, kind)
        if not newer_or_same(s, old):
            b.warn(f"nowcast_{kind}: download older than the stored copy (stamp {s['stamp']}), kept the stored one")
            continue
        if old is not None and file_key(s) == file_key(old):
            # same stamp and points, other bytes than stored: when they are an earlier row's copy (A -> B -> A), the
            # stored B is the rewrite item 479 looks for; a reference to A would drop it (review C, finding 14). Any
            # earlier row, not only the latest one storing bytes: that can be a raw page (review C2, finding 2)
            seen = await rt.store.pm_cpi_sha_day(row["day"], kind, sha)
            if seen is not None:
                b.warn(f"nowcast_{kind}: download equals the copy of {seen} under the same stamp, kept today's other bytes")
                continue
        await _keep_file(rt, row, kind, raw, sha)
        row[f"cf_stamp_{kind}"], row[f"cf_last_day_{kind}"] = s["stamp"], s["last_day"]
        row["target" if kind == "year" else "mom_target"] = s["target"]
        points[kind] = s["points"]
        changed = True
    if got:
        row["cf_fetches"] = int(row.get("cf_fetches") or 0) + 1
        row["cf_fetch_ts"] = _ts(rt)
    row["cf_points"] = json.dumps(points, separators=(",", ":")) if points else None
    return changed


def _values(b: Builder, row: dict[str, Any]) -> None:
    """The row's target and its eight values from the stored points. MoM values are those of the YoY file's target
    (remark 5 of review C): when the MoM file's own target differs, ``missing`` says so and its points stay in
    ``cf_points``."""
    pts = _obj(row.get("cf_points"))
    if not readable(row, "year"):
        row["target"] = row.get("mom_target") if readable(row, "month") else None
    t = row.get("target")
    pt = prev_target(t) if t else None
    row["prev_target"] = pt
    for pre, kind in (("yoy", "year"), ("mom", "month")):
        for k in ("headline", "core"):
            row[f"{pre}_{k}_pct"] = latest_value(pts.get(kind), t, k)
            row[f"prev_{pre}_{k}_pct"] = latest_value(pts.get(kind), pt, k)
    mt = row.get("mom_target")
    if t and readable(row, "month") and mt != t:
        has = t in (pts.get("month") or {})
        b.miss("mom_target", "clevelandfed", "data_unavailable",
               f"MoM file's release target {mt} differs from the YoY file's {t}: mom_*_pct are for {t}"
               + ("" if has else f" and the MoM file has no {t} chart (NULL)"))  # fmt: skip


def _fresh(row: dict[str, Any], day: str) -> int | None:
    if not all(readable(row, k) for k in FILES):
        return None
    return int(all(is_fresh(row.get(f"cf_stamp_{k}"), row.get(f"cf_last_day_{k}"), day) for k in FILES))


async def _books(rt: "Runtime", b: Builder, row: dict[str, Any], since: int) -> bool:
    """List the open events of every series, read the YES books of the brackets that are not paired with the stored
    nowcast yet (no book, or read before ``since``), merge into the main set. True = at least one book was read."""
    row["books_tries"] = int(row.get("books_tries") or 0) + 1
    old = books_of(row) or {}
    listed = set(old.get("series_listed") or [])
    counts = dict(old.get("counts") or {})
    fresh: dict[str, dict[str, Any]] = {}
    shut: set[str] = set()  # YES tokens known closed this tick: closed: true in a listing, or confirmed by /markets/{id}
    answered: set[str] = set()  # series whose listing this tick is complete, empty or not
    for sid in SERIES:
        data = await b.guard(f"events:{sid}", "polymarket:gamma", rt.polymarket.events_all({"series_id": sid, "closed": "false"}))
        if data is None:
            continue
        if not data.get("complete"):
            b.miss(f"events:{sid}", "polymarket:gamma", "data_unavailable", f"listing not finished after {data.get('pages')} pages")
            continue
        events = data.get("rows") or []
        answered.add(sid)
        shut |= closed_tokens(events)
        got = brackets(sid, events)
        counts[sid] = {"events": len(events), "brackets": len(got), "ts": _ts(rt)}
        if not got and sid in MAIN_SERIES:
            b.miss(f"events:{sid}", "polymarket:gamma", "data_unavailable",
                   f"series {sid}: 0 open brackets ({len(events)} open events), asked again next tick")  # fmt: skip
            continue
        listed.add(sid)
        for x in got:
            fresh.setdefault(x["token"], x)
    main = {str(x["token"]): x for x in old.get("brackets") or []}
    # blocker 1 of review C2: a live stored bracket missing from this tick's listing (a short or failed Gamma answer, a
    # market listed without its token, an event gone after the release) is not taken for closed: its stored token goes
    # into the same /books; only positive evidence (shut) makes it gone
    absent = {t: as_listed(x) for t, x in main.items() if t not in fresh and t not in shut and not x.get("gone_ts")}
    cand = {**fresh, **absent}
    tokens = [t for t in cand if not paired(main.get(t), since)]
    books: dict[str, dict[str, Any]] = {}
    read: dict[str, int] = {}
    for i in range(0, len(tokens), BOOK_CHUNK):
        part = tokens[i : i + BOOK_CHUNK]
        data = await b.guard(f"books:{i // BOOK_CHUNK}", "polymarket:clob", rt.polymarket.books(part))
        ts = _ts(rt)
        for bk in data or []:
            if isinstance(bk, dict) and bk.get("asset_id") is not None and str(bk["asset_id"]) in part:
                books[str(bk["asset_id"])] = bk
                read[str(bk["asset_id"])] = ts
    # absent from an answered listing and no book either: ask Gamma about that market (at most CONFIRM_MAX a tick);
    # only closed: true for this very token marks it gone, a failed or other answer keeps it live and asked next tick
    ask = [t for t in tokens if t in absent and t not in books and absent[t]["series"] in answered]
    for t in ask[:CONFIRM_MAX]:
        mid = str(absent[t].get("market_id"))
        if confirms_close(await b.guard(f"closed:{mid}", "polymarket:gamma", rt.polymarket.market(mid)), t):
            shut.add(t)
    if len(ask) > CONFIRM_MAX:
        b.warn(f"{len(ask)} bracket(s) to confirm closed, {CONFIRM_MAX} asked this tick, the rest next tick")
    lost = [t for t in tokens if t not in books and t not in shut]
    if lost and not any(m.field.startswith("books:") for m in b.missing):
        out = sum(1 for t in lost if t in absent)
        b.miss("books", "polymarket:clob", "data_unavailable", f"{len(lost)} token(s) not in the /books answer"
               + (f" ({out} of them not in the listing either, not confirmed closed: kept, asked next tick)" if out else ""))  # fmt: skip
    if any(t in books for t in absent):
        b.warn(f"{sum(1 for t in absent if t in books)} bracket(s) not in this tick's listing, read by their stored token")
    new = [with_book(cand[t], books.get(t), read.get(t, 0)) for t in tokens]
    merged, superseded = merge_brackets(main.values(), new, since, _ts(rt))
    # finding 12 of review C: a closed bracket cannot be read again; it keeps its book but is out of the pair checks,
    # so the row is not held 'partial' (and Gamma asked every tick) until 21:00
    merged = mark_gone(merged, shut, fresh, _ts(rt))
    gone = [x for x in merged if x.get("gone_ts") and not (main.get(str(x["token"])) or {}).get("gone_ts")]
    if gone:
        b.warn(f"{len(gone)} bracket(s) closed, their books kept: {[x.get('title') for x in gone][:6]}")
    try:
        depth = _obj(zlib.decompress(row["books_depth_z"])) if row.get("books_depth_z") else {}
    except zlib.error:  # never seen; the stored blob is then left as it is rather than overwritten
        depth = None
        b.miss("books_depth", "store", "backend_error", "stored books_depth_z does not decompress: depth of this tick not stored")
    d_main, d_early = dict((depth or {}).get("main") or {}), list((depth or {}).get("early") or [])
    if superseded:
        early = books_of(row, "books_early") or {}
        row["books_early"] = json.dumps({"brackets": [*(early.get("brackets") or []), *superseded]}, separators=(",", ":"))
        for x in superseded:
            d = d_main.pop(str(x["token"]), None)
            if d is not None:
                d_early.append({"token": str(x["token"]), "superseded_ts": x["superseded_ts"], **d})
    by_tok = {str(x["token"]): x for x in merged}
    for t, bk in books.items():
        if by_tok.get(t, {}).get("read_ts") == read[t]:
            d_main[t] = {"read_ts": read[t], **book_depth(bk)}
    if depth is not None and (d_main or d_early):
        blob = json.dumps({"main": d_main, "early": d_early}, separators=(",", ":")).encode()
        row["books_depth_z"] = zlib.compress(blob, ZLIB_LEVEL)
    if not merged:
        b.miss("brackets", "polymarket:gamma", "data_unavailable", "no open CPI bracket in any listed series")
    row["books"] = json.dumps({"series_listed": sorted(listed, key=int), "counts": counts, "brackets": merged}, separators=(",", ":"))
    row["n_brackets"] = len(merged)
    row["n_events"] = len({x["event_id"] for x in merged})
    row["n_quoted"] = quoted(merged)
    if books:
        row["books_ts"] = max(read.values())
    return bool(books)


async def collect_pm_cpi_snapshot(rt: "Runtime") -> int:
    """Collector job. Returns 1 when the day's row was written or completed this tick, 0 otherwise (outside the window,
    nothing due, no store). Never raises on a provider failure."""
    if rt.store is None:
        log.warning("pm_cpi_snapshot: no SQLite store (set TRADEDESK_DB_PATH), nothing recorded")
        return 0
    now = rt.now()
    ph = phase(now)
    if ph not in ("window", "late"):
        return 0
    await rt.store.init()
    day = now.date().isoformat()
    stored = await rt.store.pm_cpi_row(day)
    want_cf, want_books = plan(stored, now, SERIES)
    if not (want_cf or want_books):
        return 0
    row = dict(stored) if stored is not None else _blank(day, _ts(rt))
    b = Builder()
    changed = await _nowcast(rt, b, row, day) if want_cf else False
    # a file stored this tick makes the books read before it due again: read them all now (blocker 1 of review C)
    read_any = await _books(rt, b, row, nowcast_ts(row) or 0) if books_due(row, SERIES) else False
    _values(b, row)
    since = nowcast_ts(row)
    books = books_of(row)
    row["cf_fresh"] = _fresh(row, day)
    row["books_paired"] = books_paired(books, since)
    if row["books_paired"] == 0 and since is not None:
        early = [x for x in live((books or {}).get("brackets") or []) if x.get("book") and not paired(x, since)]
        b.miss("books_paired", "polymarket:clob", "data_unavailable",
               f"{len(early)} book(s) read before the nowcast download at {_hm(since)} UTC are still the main set's: "
               "the re-read did not reach them")  # fmt: skip
    ok_books = books_complete(books, SERIES, since or 0)
    row["status"] = "partial" if row["cf_fresh"] is None or not ok_books else ("complete" if row["cf_fresh"] else "stale")
    row["late"] = int(bool(row.get("late")) or (ph == "late" and (changed or read_any)))
    row["updated_ts"] = _ts(rt)
    row["missing"] = json.dumps([m.model_dump() for m in b.missing], ensure_ascii=False) if b.missing else None
    await rt.store.upsert_pm_cpi(row)
    msg = "; ".join([f"{m.field}: {m.reason} {m.detail}".strip() for m in b.missing[:6]] + b.warnings[:2])
    log.log(
        logging.WARNING if b.missing else logging.INFO,
        "pm_cpi_snapshot %s (%s): %s, nowcast stamp %s / %s, target %s, brackets %d (quoted %d, paired %s)%s",
        day, ph, row["status"], row.get("cf_stamp_year"), row.get("cf_stamp_month"), row.get("target"),
        row["n_brackets"], row["n_quoted"], row["books_paired"], f"; missing: {msg}" if msg else "",
    )  # fmt: skip
    return 1
