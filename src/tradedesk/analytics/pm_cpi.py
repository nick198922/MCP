"""Cleveland Fed inflation nowcast files and Polymarket CPI bracket books: pure helpers of the daily snapshot job
(collector/pm_cpi_snapshot.py) and of scripts/verify_pm_cpi_step0.py. No I/O; time is passed in.

Docs: docs/ideas/IDEA-20261028-pm-cpi-brackets-vs-nowcast-forward.md, item 480 of the standing card. The nowcast files
(``nowcast_year.json`` = YoY, ``nowcast_month.json`` = MoM) are lists of FusionCharts charts, one per target month
(``chart.subcaption`` 'YYYY-M'); each chart has the daily nowcasts of that month in ``dataset`` ('CPI Inflation' =
headline, 'Core CPI Inflation' = core), aligned with the non-vline ``categories`` ('MM/DD'). A vline 'CPI <Mon>' marks
the BLS release of a month. Values are **percent, as published** (3.79 = 3.79 %), the same unit the Polymarket brackets
are worded in ('3.7%'); they are not converted to decimals here because this is an archive of a third-party file. A
value that is not a finite number within +-100 % is read as no value (:func:`pct`), so a garbled point never raises.

The window of the job: every UTC day, weekends included (a forward entry T-3 / T-1 falls on a weekend for some releases;
on weekends and US holidays the stamp stays old and the row is kept, marked stale) 15:30-17:00 UTC, after the Cleveland
update (~10:00 ET). A day missed in the window is filled late (17:00-21:00 UTC, ``late`` = 1) for what is still
missing: Polymarket books are not archived anywhere.

Pairing (review C of item 480, blocker 1): the books of the row's main set are the pair of its nowcast only when every
one was read at or after the download of both stored files (:func:`nowcast_ts`). A file stored later (today's copy on
the 30 min retry) makes the books read before it due again: they are re-read in the same tick, and a superseded book is
kept in the row's early set (``books_early``), never dropped.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from typing import Any

from .option_xvenue import ms, num
from .pm_calibration import json_list
from .pm_gap import top_of_book

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
HEADLINE = "CPI Inflation"
CORE = "Core CPI Inflation"
SERIES = {"headline": HEADLINE, "core": CORE}
NEWEST = 3  # target charts whose daily points the row keeps in readable form (plus the release target and the one before)

WINDOW_START = time(15, 30)
WINDOW_END = time(17, 0)
LATE_END = time(21, 0)
CF_MAX_FETCHES = 3  # successful nowcast downloads a day: the first plus up to two when the file is not today's yet
CF_RETRY_S = 1800  # a stale file is asked again at the earliest this long after the last download
CF_MAX_TRIES = 12  # nowcast attempts a day, failed ones included (a 7.7 MB file each)
# listing + books attempts a day (window and late phase): 80 covers every tick at the shortest cadence (300 s) from
# 15:30 to 21:00 (66 ticks, finding 3 of review C2; 40 ran out at 18:45 when the books stayed due). One attempt is 5
# Gamma listings + 1 CLOB /books (+ the /markets/{id} confirmations, collector CONFIRM_MAX at most).
BOOKS_MAX_TRIES = 80
MAX_PCT = 100.0  # |nowcast| above this (percent) is garbage, not an inflation rate
MAX_SIZE = 1e12  # shares at one book level (as option_xvenue.MAX_NUM)


def pct(v: Any) -> float | None:
    """A nowcast value (percent, as published) as a finite float within +-MAX_PCT, else None: empty, text, a bool,
    NaN / inf, a 300+ digit integer (float() raises OverflowError) or a value no inflation rate has."""
    if v is None or isinstance(v, bool) or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) and abs(x) <= MAX_PCT else None


def cf_daily(chart: dict[str, Any], series: str) -> tuple[str, list[tuple[str, float | None]], dict[str, str]]:
    """One target-month chart -> (target 'YYYY-M', [(ISO date, value)], {release label: ISO date of the last
    nowcast point before that vline}). Labels are 'MM/DD' without a year: the year is the target year, plus one
    when the label month is far below the target month (Dec target -> Jan/Feb points). A malformed value is None
    (:func:`pct`); a malformed chart (subcaption, label) raises, and :func:`_charts` leaves that chart out."""
    target = chart["chart"]["subcaption"]
    ty, tm = (int(x) for x in target.split("-"))
    cats = chart["categories"][0]["category"]
    ds = next((d for d in chart.get("dataset") or [] if isinstance(d, dict) and d.get("seriesname") == series), None)
    vals = (ds or {}).get("data") or []
    vals = vals if isinstance(vals, list) else []
    pts: list[tuple[str, float | None]] = []
    vlines: dict[str, str] = {}
    j = 0  # data points align with non-vline categories
    for c in cats:
        lab = c.get("label", "")
        if c.get("vline"):
            if pts:
                vlines.setdefault(lab, pts[-1][0])
            continue
        mm, dd = (int(x) for x in lab.split("/"))
        y = ty + 1 if mm < tm - 6 else ty
        v = vals[j].get("value") if j < len(vals) and isinstance(vals[j], dict) else None
        j += 1
        pts.append((f"{y:04d}-{mm:02d}-{dd:02d}", pct(v)))
    return target, pts, vlines


def target_key(target: str) -> tuple[int, int]:
    y, m = (int(x) for x in target.split("-"))
    return y, m


def prev_target(target: str) -> str:
    y, m = target_key(target)
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1}"


def _charts(data: Any) -> dict[str, dict[str, Any]]:
    """{target: chart} of the charts whose points parse (a malformed chart is left out, not raised)."""
    out: dict[str, dict[str, Any]] = {}
    for ch in data if isinstance(data, list) else []:
        try:
            t = cf_daily(ch, HEADLINE)[0]
            target_key(t)
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            continue
        out[t] = ch
    return dict(sorted(out.items(), key=lambda kv: target_key(kv[0])))


def file_stamp(data: Any) -> str | None:
    """``chart._comment`` of the last chart ('2026-11-25 00:00' on 26.11), as text; None when absent."""
    try:
        s = data[-1]["chart"].get("_comment")
    except (KeyError, TypeError, IndexError, AttributeError):
        return None
    return str(s) if s not in (None, "") else None


def stamp_day(stamp: str | None) -> str | None:
    """The ISO day at the start of a stamp ('2026-11-25 00:00' -> '2026-11-25'); None when it is not a date."""
    try:
        return date.fromisoformat(str(stamp)[:10]).isoformat()
    except ValueError:
        return None


def _points(chart: dict[str, Any], series: str) -> list[tuple[str, float]]:
    return [(d, v) for d, v in cf_daily(chart, series)[1] if v is not None]


def last_point_day(data: Any, series: str = HEADLINE) -> str | None:
    """The latest date with a value of ``series`` over all charts of a file."""
    days = [p[-1][0] for ch in _charts(data).values() if (p := _points(ch, series))]
    return max(days) if days else None


def release_target(data: Any, newest: int = NEWEST) -> str | None:
    """The target month of the next CPI release: the oldest of the ``newest`` latest charts that has no 'CPI <Mon>'
    line of its own month yet (that line appears at the release); the newest chart when all have it."""
    charts = _charts(data)
    keys = list(charts)[-newest:]
    for t in keys:
        label = f"CPI {MONTHS[target_key(t)[1] - 1]}"
        if label not in cf_daily(charts[t], HEADLINE)[2]:
            return t
    return keys[-1] if keys else None


def is_fresh(stamp: str | None, last_day: str | None, day: str) -> bool:
    """Updated on ``day``: its stamp or its last daily point is that day. Both are read because the stamp convention
    was seen only once (26.11, a US holiday: stamp and last point both 25.11); see the change entry of item 480."""
    return stamp_day(stamp) == day or last_day == day


def nowcast_summary(data: Any, day: str) -> dict[str, Any]:
    """What the row keeps of one nowcast file: stamp, last daily point, freshness, the release target and the month
    before, their latest headline / core values, and the daily points (headline and core) of the newest charts plus
    those two months: ``points`` = {target: {"headline": [[day, value], ...], "core": [...]}}."""
    charts = _charts(data)
    stamp = file_stamp(data)
    last = last_point_day(data)
    tgt = release_target(data)
    prev = prev_target(tgt) if tgt else None
    keep = [t for t in charts if t in set(list(charts)[-NEWEST:]) | {tgt, prev}]
    points = {t: {k: [[d, v] for d, v in _points(charts[t], s)] for k, s in SERIES.items()} for t in keep}
    return {
        "stamp": stamp,
        "last_day": last,
        "fresh": is_fresh(stamp, last, day),
        "target": tgt,
        "prev_target": prev,
        "latest": {
            "target": {k: latest_value(points, tgt, k) for k in SERIES},
            "prev": {k: latest_value(points, prev, k) for k in SERIES},
        },
        "points": points,
    }


def latest_value(points: Mapping[str, Any] | None, target: str | None, series: str) -> float | None:
    """The last daily value of ``series`` ('headline' | 'core') of ``target`` in a ``points`` map of
    :func:`nowcast_summary` (as stored in the row's ``cf_points``); None when that target or series has no point."""
    p = ((points or {}).get(target or "") or {}).get(series) or []
    return p[-1][1] if p else None


def newer_or_same(new: Mapping[str, Any], old: Mapping[str, Any] | None) -> bool:
    """A fresh download replaces the stored one unless it is older: a cache or CDN serving yesterday's copy after
    today's must not overwrite it. Compared by (the later of stamp day and last point day, last point day, stamp day),
    so a copy without a readable stamp (``_comment`` gone) but with a newer point is not taken for an older one (review
    C of item 480, remark 10); only a tie on the days falls back to the stamp."""
    return old is None or file_key(new) >= file_key(old)


def file_key(s: Mapping[str, Any]) -> tuple[str, str, str]:
    """The order of :func:`newer_or_same`: (the later of stamp day and last point day, last point day, stamp day)."""
    last, sd = s.get("last_day") or "", stamp_day(s.get("stamp")) or ""
    return max(sd, last), last, sd


def new_points_after(old: Any, new: Any, series: str = HEADLINE, recent: int = NEWEST) -> list[dict[str, Any]]:
    """For the ``recent`` newest target months of the older copy of a nowcast file: the newer copy's daily points dated
    after the older copy's stamp day (its last point of that target when the stamp is unreadable), how many distinct
    values they have (rounded to 1e-6) and whether any differs from the older copy's last value of that target. A
    weekly step of headline shows as ``changed``; ``n`` None = the target is not in the newer copy."""
    o, n = _charts(old), _charts(new)
    cut = stamp_day(file_stamp(old))
    out = []
    for t in list(o)[-recent:]:
        op = _points(o[t], series)
        after = cut or (op[-1][0] if op else "")
        row: dict[str, Any] = {"target": t, "after": after, "n": None, "distinct": None, "changed": None, "first": None, "last": None}
        if t in n:
            np_ = [(d, v) for d, v in _points(n[t], series) if d > after]
            last_old = op[-1][1] if op else None
            row.update(
                n=len(np_),
                distinct=len({round(v, 6) for _, v in np_}),
                changed=any(last_old is None or abs(v - last_old) >= 1e-9 for _, v in np_),
                first=np_[0][0] if np_ else None,
                last=np_[-1][0] if np_ else None,
            )
        out.append(row)
    return out


# ----------------------------------------------------------------------------------------------------- the window


def phase(now: datetime) -> str:
    """'before' (< 15:30 UTC), 'window' (15:30-17:00), 'late' (17:00-21:00) or 'after'; every day of the week."""
    t = now.time()
    if t < WINDOW_START:
        return "before"
    if t < WINDOW_END:
        return "window"
    return "late" if t < LATE_END else "after"


def readable(row: Mapping[str, Any] | None, kind: str) -> bool:
    """The row holds a parsed nowcast file of ``kind`` ('year' | 'month'): bytes stored (or referenced) and its last
    daily point read. A file kept only as raw bytes (it could not be parsed) does not count."""
    r = row or {}
    return r.get(f"cf_{kind}_sha") is not None and r.get(f"cf_last_day_{kind}") is not None


def nowcast_ts(row: Mapping[str, Any] | None) -> int | None:
    """When the nowcast the row pairs with its books was downloaded: the later ``cf_*_ts`` of the readable files (each
    the first download of the stored bytes); None without a readable file."""
    ts = [int((row or {}).get(f"cf_{k}_ts") or 0) for k in ("year", "month") if readable(row, k)]
    return max(ts) if ts else None


def paired(bracket: Mapping[str, Any] | None, since: int) -> bool:
    """The bracket's book was read at or after ``since`` (unix s)."""
    return bracket is not None and bool(bracket.get("book")) and int(bracket.get("read_ts") or 0) >= since


def live(brackets_: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The brackets not known to be closed: without ``gone_ts`` (:func:`mark_gone`)."""
    return [b for b in brackets_ if not b.get("gone_ts")]


def closed_tokens(events: Iterable[Any]) -> set[str]:
    """YES tokens of the markets a listing shows with ``closed: true`` (the open events of a series can hold closed
    markets): positive evidence of a close, unlike a market that is simply not in the answer."""
    out = set()
    for ev in events:
        for m in (ev.get("markets") or []) if isinstance(ev, dict) else []:
            if isinstance(m, dict) and m.get("closed") is True and (tok := yes_token(m)) is not None:
                out.add(tok)
    return out


def confirms_close(market: Any, token: str) -> bool:
    """A Gamma ``/markets/{id}`` answer says the market of YES token ``token`` is closed: ``closed`` is exactly True and
    its YES token is ``token``. Anything else (open, another market, no token, not a dict) is no proof."""
    return isinstance(market, Mapping) and market.get("closed") is True and yes_token(market) == str(token)


def as_listed(bracket: Mapping[str, Any]) -> dict[str, Any]:
    """A stored bracket as :func:`brackets` lists it (ids, title, end date, fee type, token), without its book: what
    the job asks the CLOB for when the bracket is missing from this tick's listing but not known to be closed."""
    keys = ("series", "event_id", "event_slug", "end_date", "market_id", "title", "fee_type", "token")
    return {**{k: bracket.get(k) for k in keys}, "book": False}


def mark_gone(
    brackets_: Iterable[Mapping[str, Any]], closed: Iterable[str], listed_tokens: Iterable[str], now: int
) -> list[dict[str, Any]]:
    """Mark the brackets known to be closed (finding 12 of review C, blocker 1 of review C2): a bracket whose token is
    in ``closed`` (a listing showed it ``closed: true``, or Gamma ``/markets/{id}`` said so: :func:`closed_tokens`,
    :func:`confirms_close`) gets ``gone_ts`` = ``now`` (the first tick that saw it; kept later); one listed open again
    (``listed_tokens``) loses it. Being absent from a listing is no proof and marks nothing. A gone bracket keeps its
    book in the main set but is left out of the pair checks (:func:`books_complete`, :func:`books_paired`)."""
    shut, tokens = {str(t) for t in closed}, {str(t) for t in listed_tokens}
    out = []
    for b in brackets_:
        x = dict(b)
        tok = str(x.get("token"))
        if tok in tokens:
            x.pop("gone_ts", None)
        elif tok in shut and not x.get("gone_ts"):
            x["gone_ts"] = now
        out.append(x)
    return out


def books_complete(books: Mapping[str, Any] | None, series: Iterable[str], since: int = 0) -> bool:
    """Every series listed at least once today, at least one live bracket, and the book of every live bracket read at
    or after ``since`` (the nowcast download, :func:`nowcast_ts`; 0 = any time). Gone (closed) brackets are left out."""
    if not books:
        return False
    brackets = live(books.get("brackets") or [])
    listed = set(series) <= set(books.get("series_listed") or [])
    return listed and bool(brackets) and all(paired(b, since) for b in brackets)


def books_paired(books: Mapping[str, Any] | None, since: int | None) -> int | None:
    """1 = every live bracket with a book was read at or after ``since``, 0 = not, None = no nowcast file or no such
    book. Gone (closed) brackets (:func:`mark_gone`) are left out."""
    with_book = [b for b in live((books or {}).get("brackets") or []) if b.get("book")]
    if since is None or not with_book:
        return None
    return int(all(paired(b, since) for b in with_book))


def books_due(row: Mapping[str, Any] | None, series: Iterable[str]) -> bool:
    """The books are read this tick: a series not listed, no bracket, a bracket without its book or with a book read
    before the nowcast download (blocker 1 of review C), at most ``BOOKS_MAX_TRIES`` attempts a day."""
    if row is None:
        return True
    return int(row.get("books_tries") or 0) < BOOKS_MAX_TRIES and not books_complete(books_of(row), series, nowcast_ts(row) or 0)


def plan(row: Mapping[str, Any] | None, now: datetime, series: Iterable[str]) -> tuple[bool, bool]:
    """(fetch the nowcast files, fetch the books) for this tick, from the stored row of the day (None = none yet).

    Window: a missing or unreadable file is fetched every tick (at most ``CF_MAX_TRIES`` attempts a day); on Mon-Fri a
    file that is not today's is fetched again ``CF_RETRY_S`` after the last download (``cf_fetch_ts``), up to
    ``CF_MAX_FETCHES`` downloads (on weekends Cleveland does not update: one download is enough); the books by
    :func:`books_due`. Late phase: only what is still missing (no staleness retry). Outside both: nothing."""
    ph = phase(now)
    if ph not in ("window", "late"):
        return False, False
    if row is None:
        return True, True
    ts = int(now.timestamp())
    tries = int(row.get("cf_tries") or 0)
    if not (readable(row, "year") and readable(row, "month")):
        cf = tries < CF_MAX_TRIES
    else:
        cf = (
            ph == "window"
            and now.weekday() < 5
            and not row.get("cf_fresh")
            and int(row.get("cf_fetches") or 0) < CF_MAX_FETCHES
            and tries < CF_MAX_TRIES
            and ts - int(row.get("cf_fetch_ts") or 0) >= CF_RETRY_S
        )
    return cf, books_due(row, series)


def books_of(row: Mapping[str, Any] | None, column: str = "books") -> dict[str, Any] | None:
    """The row's ``books`` column ({"series_listed": [...], "counts": {...}, "brackets": [...]}) decoded (or another JSON
    object column, e.g. ``books_early``); None when absent or unreadable."""
    raw = (row or {}).get(column)
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        v = json.loads(raw) if isinstance(raw, str | bytes) else None
    except (ValueError, RecursionError):
        return None
    return v if isinstance(v, dict) else None


# ----------------------------------------------------------------------------------------------------- the books


def yes_token(market: Mapping[str, Any]) -> str | None:
    toks = json_list(market.get("clobTokenIds")) or []
    return str(toks[0]) if toks and toks[0] not in (None, "") else None


def brackets(series_id: str, events: Iterable[Any]) -> list[dict[str, Any]]:
    """One entry per open bracket (market not closed, with a YES token) of the open events of one series: ids, title,
    the event's ``endDate`` and the market's fee type. Text fields are untrusted data, stored as is."""
    out = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        for m in ev.get("markets") or []:
            if not isinstance(m, dict) or m.get("closed"):
                continue
            tok = yes_token(m)
            if tok is None:
                continue
            out.append(
                {
                    "series": str(series_id),
                    "event_id": str(ev.get("id")),
                    "event_slug": ev.get("slug"),
                    "end_date": ev.get("endDate"),
                    "market_id": str(m.get("id")),
                    "title": m.get("groupItemTitle"),
                    "fee_type": m.get("feeType"),
                    "token": tok,
                    "book": False,
                }
            )
    return out


def with_book(bracket: Mapping[str, Any], book: Mapping[str, Any] | None, read_ts: int) -> dict[str, Any]:
    """The bracket with its YES book top (``analytics.pm_gap.top_of_book``: price, size in shares, size x price in USD at
    the best level), the venue's book time (``book_ts_ms``, unix ms via ``option_xvenue.ms``: NaN, inf, text or a time
    past year 9999 is None, never an exception) and when it was read (``read_ts``, unix s). ``book`` None
    = not in the answer: the bracket stays without a book (retried on the next tick)."""
    if book is None:
        return dict(bracket)
    top = top_of_book(dict(book))
    return {**bracket, **top, "book": True, "book_ts_ms": ms(book.get("timestamp")), "read_ts": read_ts}


def book_depth(book: Mapping[str, Any]) -> dict[str, Any]:
    """Every level of a CLOB book, both sides, as [price, size in shares] (bids best first, asks best first), plus the
    venue's book time (``book_ts_ms``), tick size, minimum order size and last trade price. Levels outside 0 < price < 1
    or 0 < size <= MAX_SIZE, and values that are not finite numbers, are left out; nothing here raises."""

    def levels(rows: Any, best_first_desc: bool) -> list[list[float]]:
        out = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, Mapping):
                continue
            p, s = num(r.get("price")), num(r.get("size"))
            if p is not None and s is not None and p < 1.0 and s <= MAX_SIZE:
                out.append([p, s])
        return sorted(out, key=lambda x: x[0], reverse=best_first_desc)

    last = num(book.get("last_trade_price"))
    return {
        "book_ts_ms": ms(book.get("timestamp")),
        "tick": num(book.get("tick_size")),
        "min_size": num(book.get("min_order_size")),
        "last": last if last is not None and last <= 1.0 else None,
        "bids": levels(book.get("bids"), True),
        "asks": levels(book.get("asks"), False),
    }


def merge_brackets(
    old: Iterable[Mapping[str, Any]], new: Iterable[Mapping[str, Any]], since: int = 0, now: int = 0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Union by token -> (the main set, the superseded books). A stored bracket whose book was read at or after
    ``since`` (the nowcast download) keeps it; one stored without a book, or with a book read before ``since``, takes a
    new book, and the old book goes to the superseded list with ``superseded_ts`` = ``now`` (the row's early set:
    never dropped); a new entry without a book never replaces a stored one; new brackets are added; brackets missing
    from the new listing stay."""
    merged: dict[str, dict[str, Any]] = {str(b["token"]): dict(b) for b in old}
    superseded: list[dict[str, Any]] = []
    for b in new:
        tok = str(b["token"])
        prev = merged.get(tok)
        if prev is None:
            merged[tok] = dict(b)
        elif b.get("book") and not paired(prev, since):
            if prev.get("book"):
                superseded.append({**prev, "superseded_ts": now})
            merged[tok] = dict(b)
    return sorted(merged.values(), key=lambda b: (b["series"], b["event_id"], str(b.get("market_id")))), superseded


def quoted(brackets_: Iterable[Mapping[str, Any]]) -> int:
    """Brackets with both a best bid and a best ask."""
    return sum(1 for b in brackets_ if b.get("bid") is not None and b.get("ask") is not None)
