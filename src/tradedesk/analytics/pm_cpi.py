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

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from statistics import NormalDist
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


def to_confirm(tokens: Iterable[str], stored: Mapping[str, Mapping[str, Any]], limit: int) -> list[str]:
    """The at most ``limit`` of ``tokens`` to ask Gamma ``/markets/{id}`` about this tick (findings 4-5 of review C2,
    item 483): the ones never asked first, then the least recently asked (``confirm_ts`` of the stored bracket in
    ``stored``, unix s), ties in the given order. Every tick asked moves a bracket behind all the others, so brackets
    that never confirm (a failing request, Gamma calling them open) cannot hold the rest back: a candidate is asked
    within ceil(n / limit) ticks, n = the brackets of the row, however the candidate list changes between ticks. A
    bracket that gets a new book loses its ``confirm_ts`` (:func:`merge_brackets`), counts as never asked if it becomes
    a candidate again, and can push that bound back slightly. The state is in the row, so a restart or a new process
    keeps the rotation."""
    order = sorted(tokens, key=lambda t: int((stored.get(t) or {}).get("confirm_ts") or 0))
    return order[: max(int(limit), 0)]


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


# ------------------------------------------------------------------------------ the forward rule (step 1 registration)
# Registered in the card's «Попередня реєстрація» (item 481): headline CPI YoY brackets only. Do not change a constant
# here without a new registration in the card; a change after seeing results is exploratory and labelled so.

SD_KZ = 0.164  # pp: RMSE of the Cleveland headline CPI YoY nowcast, last day of the target month (Knotek-Zaman, EC 2023-06, Table 1)
EDGE_MIN = 0.08  # model probability - ask - taker fee per share, decimal (8 pp)
STAKE_MAX_USD = 50.0  # per bracket (cost incl. fee)
MAX_PICKS = 2  # brackets per release
HALF_TICK = 0.05  # pp: BLS publishes the 12-month change to one decimal
SD_ALT_FACTOR = 1.5  # descriptive q at sd / 1.5 (``q_narrow``) and sd * 1.5 (``q_wide``); never part of the decision
STALE_DAYS = 4  # the row's last YoY nowcast point may be at most this many calendar days before the row's day
# The data of every stage T-d (T-7, T-3, T-1) are the job's row of day T-d as it stands after LATE_END (the job writes
# nothing then: :func:`plan` returns (False, False)) and nothing else: no fallback snapshot, no other day (third review
# B 481, finding 1: whoever takes a snapshot sees the live prices, so a snapshot would let a person choose whether and
# when a number of the verdict is taken). A stage without a usable row is a skip / a missing value, never substituted.
# Columns of a pm_cpi_snapshot row that the decision reads: their digest (:func:`row_digest`) goes into the record.
ROW_DIGEST_COLUMNS = (
    "day", "updated_ts", "status", "late", "target", "cf_year_ts", "cf_month_ts", "cf_year_sha", "cf_month_sha",
    "cf_last_day_year", "cf_fresh", "yoy_headline_pct", "cf_points", "books_paired", "books", "books_early",
    "books_depth_z",
)  # fmt: skip
# Taker fee per share = rate * (p (1 - p)) ** exponent (formula checked against Polymarket's docs on 2026-10-16,
# analytics/negrisk.py); (rate, exponent) of each feeType as Gamma showed it on the CPI markets 2026-11-26 (``feeSchedule``).
FEE_SCHEDULES: dict[str, tuple[float, float]] = {"economics_fees": (0.01, 1.0), "sports_fees_v2": (0.03, 1.0)}

_BRACKET = re.compile(r"^\s*(≤|≥)?\s*(-?\d+(?:\.\d)?)\s*%\s*(\+)?\s*$")


def parse_bracket(title: Any) -> tuple[int | None, int | None] | None:
    """A bracket title -> the published values it wins on, in tenths of a percentage point, inclusive: '3.7%' -> (37, 37),
    '≤3.3%' -> (None, 33), '≥4.4%' and '4.4%+' -> (44, None); None for any other wording ('More than 3.0%', ...)."""
    m = _BRACKET.match(str(title or "").replace("−", "-"))
    if m is None or (m.group(1) == "≥" and m.group(3)) or (m.group(1) == "≤" and m.group(3)):
        return None
    v = round(float(m.group(2)) * 10)
    if m.group(1) == "≤":
        return None, v
    if m.group(1) == "≥" or m.group(3):
        return v, None
    return v, v


def is_partition(intervals: Iterable[tuple[int | None, int | None]]) -> bool:
    """The brackets of one event cover every published value exactly once: an open lower tail, consecutive tenths, an
    open upper tail."""
    iv = sorted(intervals, key=lambda x: -(10**9) if x[0] is None else x[0])
    if len(iv) < 2 or iv[0][0] is not None or iv[-1][1] is not None:
        return False
    for (_, hi), (lo, _) in zip(iv, iv[1:], strict=False):
        if hi is None or lo is None or lo != hi + 1:
            return False
    return True


def bracket_prob(interval: tuple[int | None, int | None], mu: float, sd: float) -> float:
    """P(BLS publishes a value in ``interval``) when the unrounded 12-month change is Normal(``mu``, ``sd``) (percent):
    the published value is the change rounded to 0.1, so (lo, hi) wins on [lo - 0.05, hi + 0.05)."""
    nd = NormalDist(mu, sd)
    lo, hi = interval
    up = 1.0 if hi is None else nd.cdf(hi / 10 + HALF_TICK)
    down = 0.0 if lo is None else nd.cdf(lo / 10 - HALF_TICK)
    return max(up - down, 0.0)


def taker_fee(price: float, fee_type: Any, schedule: Any = None, enabled: Any = None) -> float | None:
    """Taker fee per share at ``price``. The market's own Gamma fields come first: ``enabled`` (``feesEnabled``) False
    -> 0; a ``schedule`` (``feeSchedule``) with a finite rate > 0 and exponent > 0 -> that schedule (economics markets
    showed rate 0.05 on 2026-10-16, negrisk step 0, and the CPI markets 0.01 on 2026-11-26). Without them, the
    registered rate of ``fee_type`` (``FEE_SCHEDULES``); None for any other or missing type (not eligible)."""
    if enabled is False:
        return 0.0
    rate = num(schedule.get("rate")) if isinstance(schedule, Mapping) else None
    exp = num(schedule.get("exponent")) if isinstance(schedule, Mapping) else None
    if rate is None or exp is None:
        s = FEE_SCHEDULES.get(str(fee_type)) if fee_type is not None else None
        if s is None:
            return None
        rate, exp = s
    return rate * (price * (1.0 - price)) ** exp


def rule_signals(events: Iterable[Iterable[Mapping[str, Any]]], mu: float, sd: float = SD_KZ) -> list[dict[str, Any]]:
    """Every bracket of the headline YoY events of one release (one list of brackets per event) with its model
    probability ``q`` and, when eligible, the net edge ``q - ask - fee(ask)``; ``pick`` marks the at most ``MAX_PICKS``
    signals of the release (edge >= ``EDGE_MIN``; order: edge, then q, then market id; a bracket overlapping an already
    picked one, e.g. in another event of the same release, is skipped). Not eligible (``why``): no usable model (``mu``
    not a finite number or ``sd`` not a finite number > 0: every bracket gets 'model', none gets q), a title of the
    event does not parse or its brackets are not a partition (then no bracket of that event gets q), the bracket was
    left out before (a ``why`` given in the input, e.g. 'unpaired' from :func:`book_why`, is kept), is closed
    (``gone_ts``), has no ask, or an unknown fee type. ``q_narrow`` / ``q_wide`` (sd / and * ``SD_ALT_FACTOR``) are
    descriptive only."""
    ok = model_ok(mu, sd)
    out: list[dict[str, Any]] = []
    for k, ev in enumerate(events):
        rows = [dict(b) for b in ev]
        if ok:
            out += _event_rows(k, rows, float(mu), float(sd))
        else:
            out += [{**b, **_blank(k, parse_bracket(b.get("title"))), "why": "model"} for b in rows]
    cands = sorted((r for r in out if r["edge"] is not None and r["edge"] >= EDGE_MIN - 1e-12),
                   key=lambda r: (-r["edge"], -r["q"], str(r.get("market_id"))))  # fmt: skip
    picked: list[tuple[int | None, int | None]] = []
    for r in cands:
        if len(picked) >= MAX_PICKS:
            break
        if not any(_overlap(r["interval"], p) for p in picked):
            r["pick"] = True
            picked.append(r["interval"])
    return out


def _event_rows(k: int, rows: list[dict[str, Any]], mu: float, sd: float) -> list[dict[str, Any]]:
    ivs = [parse_bracket(b.get("title")) for b in rows]
    part = all(iv is not None for iv in ivs) and is_partition([iv for iv in ivs if iv is not None])
    out = []
    for b, iv in zip(rows, ivs, strict=True):
        r = {**b, **_blank(k, iv)}
        if not part or iv is None:
            r["why"] = "not a partition" if iv is not None else "title"
        else:
            r["q"] = bracket_prob(iv, mu, sd)
            r["q_narrow"] = bracket_prob(iv, mu, sd / SD_ALT_FACTOR)
            r["q_wide"] = bracket_prob(iv, mu, sd * SD_ALT_FACTOR)
            ask = b.get("ask")
            fee = taker_fee(ask, b.get("fee_type"), b.get("fee_schedule"), b.get("fees_enabled")) if ask is not None else None
            if b.get("why"):
                r["why"] = b["why"]
            elif b.get("gone_ts"):
                r["why"] = "closed"
            elif ask is None:
                r["why"] = "no ask"
            elif fee is None:
                r["why"] = "fee type"
            else:
                r.update(fee=fee, edge=r["q"] - ask - fee)
        out.append(r)
    return out


def _overlap(a: tuple[int | None, int | None], b: tuple[int | None, int | None]) -> bool:
    lo = max(x for x in (a[0], b[0], -(10**9)) if x is not None)
    hi = min(x for x in (a[1], b[1], 10**9) if x is not None)
    return lo <= hi


def _blank(k: int, iv: tuple[int | None, int | None] | None) -> dict[str, Any]:
    return {"event": k, "interval": iv, "q": None, "q_narrow": None, "q_wide": None, "fee": None, "edge": None,
            "why": None, "pick": False}  # fmt: skip


def _finite(x: Any) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def model_ok(mu: Any, sd: Any) -> bool:
    """The model can be evaluated: ``mu`` a finite number (percent; negative allowed) and ``sd`` a finite number > 0."""
    return _finite(mu) and _finite(sd) and sd > 0


def row_why(row: Mapping[str, Any] | None, day: str, target: str) -> str | None:
    """None when the pm_cpi_snapshot row of ``day`` (ISO, T-d) can carry the stage, else the reason recorded:
    'no row'; 'status' (not complete / stale / partial); 'target' (the row's target is not the events' month);
    'no yoy nowcast' (no readable ``nowcast_year.json`` or ``yoy_headline_pct`` empty: a row whose only readable file
    is the MoM one has no mu); 'nowcast stale' (the last YoY point is more than ``STALE_DAYS`` calendar days before
    ``day``, or after it); 'unpaired' (complete / stale with ``books_paired`` != 1). A partial row passes here, and its
    brackets are admitted one by one (:func:`book_why`)."""
    if row is None:
        return "no row"
    if row.get("status") not in ("complete", "stale", "partial"):
        return "status"
    if not same_target(row.get("target"), target):
        return "target"
    if not readable(row, "year") or pct(row.get("yoy_headline_pct")) is None:
        return "no yoy nowcast"
    try:
        gap = (date.fromisoformat(day) - date.fromisoformat(str(row.get("cf_last_day_year"))[:10])).days
    except ValueError:
        return "no yoy nowcast"
    if not 0 <= gap <= STALE_DAYS:
        return "nowcast stale"
    if row.get("status") != "partial" and int(row.get("books_paired") or 0) != 1:
        return "unpaired"
    return None


def same_target(a: Any, b: Any) -> bool:
    """Two target months are the same month whatever the zero padding ('2027-1' as the row and the Cleveland charts
    write it, '2027-01' as a slug-derived month may be written); an unparseable side is never the same."""
    try:
        return target_key(str(a)) == target_key(str(b))
    except ValueError:
        return False


def book_why(bracket: Mapping[str, Any], since: int | None) -> str | None:
    """A bracket of the row's main set is admitted (None) when its book was read at or after ``since`` (the row's
    :func:`nowcast_ts`); 'no book' without a book, 'unpaired' for a book read before the nowcast download."""
    if not bracket.get("book"):
        return "no book"
    return None if since is not None and paired(bracket, since) else "unpaired"


def close_quote(row: Mapping[str, Any] | None, token: Any) -> dict[str, Any]:
    """The T-1 quote of the YES ``token`` of a signal, for :func:`clv` (registration п. 5, 8): only from the main set
    ``books`` of the job's T-1 row as it stands after LATE_END; there is no fallback snapshot and no other day. The
    nowcast checks of :func:`row_why` do not apply (no decision is taken at T-1), and neither does the pairing of the
    book with the nowcast download: the bracket counts when ``book_why(bracket, 0)`` is None, i.e. it has a book.
    -> {"bid", "ask", "why"}: why 'no row', 'no book' (token not in the main set, or without a book) or None; a side
    of an admitted book may still be None (then :func:`clv` gives None)."""
    if row is None:
        return {"bid": None, "ask": None, "why": "no row"}
    if token not in (None, ""):
        for b in (books_of(row) or {}).get("brackets") or []:
            if isinstance(b, Mapping) and str(b.get("token")) == str(token):
                why = book_why(b, 0)
                if why is None:
                    return {"bid": b.get("bid"), "ask": b.get("ask"), "why": None}
                break
    return {"bid": None, "ask": None, "why": "no book"}


def fill(
    asks: Iterable[Any],
    q: float,
    fee_type: Any = None,
    schedule: Any = None,
    enabled: Any = None,
    min_size: Any = None,
    stake: float = STAKE_MAX_USD,
) -> dict[str, Any]:
    """Paper entry of one picked bracket along its ask levels ([price, shares], any order; the best is taken first):
    levels are taken while ``q - a - fee(a) >= EDGE_MIN`` at the level's price a and the cost with fee stays within
    ``stake`` USD (the last level partly; shares are not rounded). -> shares, cost_usd (with fee), fee_usd, p_in (share-
    weighted price without fee; None when nothing was taken), levels taken, ``stop`` ('edge' | 'stake' | 'book' |
    'fee type') and ``executable`` (shares >= the book's ``min_size`` in shares; without a usable min_size, shares > 0).
    Levels that are not 0 < price < 1 with a finite size > 0 are skipped."""
    lv = sorted(
        ([float(p), float(s)] for p, s in (x for x in asks if isinstance(x, list | tuple) and len(x) >= 2)
         if _finite(p) and _finite(s) and 0 < p < 1 and s > 0),
        key=lambda x: x[0],
    )  # fmt: skip
    shares = cost = fee_usd = notional = 0.0
    taken, stop = 0, "book"
    for a, s in lv:
        f = taker_fee(a, fee_type, schedule, enabled)
        if f is None:
            stop = "fee type"
            break
        if q - a - f < EDGE_MIN - 1e-12:
            stop = "edge"
            break
        room = stake - cost
        if room <= 1e-9:
            stop = "stake"
            break
        n = min(s, room / (a + f))
        shares, cost, fee_usd, notional, taken = shares + n, cost + n * (a + f), fee_usd + n * f, notional + n * a, taken + 1
        if n < s:
            stop = "stake"
            break
    ms_ = float(min_size) if _finite(min_size) and min_size > 0 else None
    return {
        "shares": shares,
        "cost_usd": cost,
        "fee_usd": fee_usd,
        "p_in": notional / shares if shares > 0 else None,
        "levels": taken,
        "stop": stop,
        "executable": shares >= ms_ - 1e-9 if ms_ is not None else shares > 0,
    }


def clv(p_in: Any, shares: Any, bid1: Any, ask1: Any) -> dict[str, float] | None:
    """Closing-line value of an executable signal at the T-1 mid of the same token: ``usd`` = shares * (mid - p_in)
    (the primary unit, USD per signal), ``per_share`` = mid - p_in (price units on a 1 USD payout); None when a side
    of the T-1 book or the entry is missing (never filled in)."""
    if not all(_finite(x) for x in (p_in, shares, bid1, ask1)):
        return None
    mid = (bid1 + ask1) / 2
    return {"mid": mid, "per_share": mid - p_in, "usd": shares * (mid - p_in)}


def _jsonable(x: Any) -> Any:
    if isinstance(x, bytes | bytearray | memoryview):
        return {"sha256": hashlib.sha256(bytes(x)).hexdigest()}
    raise TypeError(f"not JSON: {type(x).__name__}")


def sha256_json(obj: Any) -> str:
    """sha256 (hex) of ``obj`` as canonical JSON (sorted keys, no spaces, UTF-8; bytes as their own sha256); NaN / inf
    raise ValueError, so a record that cannot be written as strict JSON has no digest."""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=_jsonable)
    return hashlib.sha256(text.encode()).hexdigest()


def digest_preimage(row: Mapping[str, Any]) -> dict[str, Any]:
    """What :func:`row_digest` hashes, as plain JSON: the ``ROW_DIGEST_COLUMNS`` of the row as stored (a missing column
    is null), each blob as ``{"sha256": <hex of its stored bytes>}``. The stage file keeps it, so the digest can be
    recomputed from the file alone: ``row_digest(digest_preimage(row)) == row_digest(row)``."""
    out: dict[str, Any] = {}
    for c in ROW_DIGEST_COLUMNS:
        v = row.get(c)
        out[c] = _jsonable(v) if isinstance(v, bytes | bytearray | memoryview) else v
    return out


def row_digest(row: Mapping[str, Any]) -> str:
    """Digest of the row columns the decision reads (``ROW_DIGEST_COLUMNS``, as stored; a missing column is null);
    a :func:`digest_preimage` gives the same digest as the row it came from."""
    return sha256_json(digest_preimage(row))


def same_record(a: Any, b: Any, rel: float = 1e-9) -> bool:
    """The recomputed decision equals the recorded one (proof check of item 487, third review B 481, finding 2):
    the same JSON structure, mappings with the same keys, lists and tuples of the same length in the same order (a
    tuple written to JSON comes back as a list), equal strings, None and bools; numbers equal within ``rel`` relative
    (abs 1e-12), so float last bits between Python versions do not matter. A bool never equals a number. Two NaN are
    equal (fourth review B 481, finding 5: a NaN in a recorded output must not turn a lawful record into a mismatch)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=1e-12)
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return set(a) == set(b) and all(same_record(a[k], b[k], rel) for k in a)
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(same_record(x, y, rel) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


# Stage windows (registration п. 5, 7): the scanner writes the file of stage T-d only from STAGE_FROM on day T-d (the
# day's row is final after LATE_END; a tick started before 21:00 may still write updated_ts a little after it) and,
# for the decision stage T-3, no later than T3_UNTIL on day T-1 (before the T-1 window of the job opens at 15:30);
# the result only from RESULT_FROM on the release day. T-7 and T-1 are records from final rows: no end.
STAGE_DAYS = {"T-7": 7, "T-3": 3, "T-1": 1, "result": 0}
STAGE_FROM = time(21, 5)
T3_UNTIL = time(12, 0)
RESULT_FROM = time(15, 0)


def stage_window(stage: str, release: str) -> tuple[datetime, datetime | None]:
    """(first moment, last moment or None) in UTC at which the scanner may write the file of ``stage`` ('T-7', 'T-3',
    'T-1', 'result') of the release on ``release`` (ISO day); KeyError for another stage."""
    d = STAGE_DAYS[stage]
    rel = date.fromisoformat(release)
    if stage == "result":
        return datetime.combine(rel, RESULT_FROM, UTC), None
    start = datetime.combine(rel - timedelta(days=d), STAGE_FROM, UTC)
    end = datetime.combine(rel - timedelta(days=1), T3_UNTIL, UTC) if stage == "T-3" else None
    return start, end


def stage_why(stage: str, release: str, now: datetime) -> str | None:
    """None when the scanner may write the file of ``stage`` at ``now`` (timezone-aware), else why not: 'stage'
    (unknown stage or release day), 'naive time', 'early' (before :func:`stage_window`), 'late' (after its end). A
    file created outside the window is not a record: for T-3 the release is a «пропуск T−3»."""
    if now.tzinfo is None or now.utcoffset() is None:
        return "naive time"
    try:
        start, end = stage_window(stage, release)
    except (KeyError, ValueError):
        return "stage"
    if now < start:
        return "early"
    if end is not None and now > end:
        return "late"
    return None


GITHUB_COMMITTER = ("GitHub", "noreply@github.com")


def merge_proof_why(log_text: str, deadline: datetime) -> tuple[str | None, str | None]:
    """Proof that a stage file reached main in time (registration п. 7), from the output of
    ``git log origin/main --first-parent --name-status --format='C %H %cI %ce %cn' -- <path>`` (newest first; on the
    first-parent chain each merge is diffed against the previous main). -> (None, merge sha) when exactly one commit
    of that chain touches the path, it adds it ('A'), GitHub made it (committer ``GITHUB_COMMITTER``: a PR merge;
    main takes nothing else, ruleset ``protect-main``) and its committer date is at or before ``deadline``; else
    (reason, sha or None): 'not merged', 'changed after' (a later commit modified or deleted it), 'not added', 'not a
    GitHub merge', 'late', 'unreadable'."""
    commits: list[list[Any]] = []
    for line in log_text.splitlines():
        if line.startswith("C "):
            parts = line.split(" ", 4)
            if len(parts) < 5:
                return "unreadable", None
            commits.append([parts[1], parts[2], parts[3], parts[4].strip(), []])
        elif line.strip() and commits:
            commits[-1][4].append(line.split("\t", 1)[0].strip())
        elif line.strip():
            return "unreadable", None
    if not commits:
        return "not merged", None
    if len(commits) > 1:
        return "changed after", commits[-1][0]
    sha, when, email, name, status = commits[0]
    if status != ["A"]:
        return "not added", sha
    if (name, email) != GITHUB_COMMITTER:
        return "not a GitHub merge", sha
    try:
        t = datetime.fromisoformat(when)
    except ValueError:
        return "unreadable", sha
    if t.tzinfo is None or deadline.tzinfo is None:
        return "unreadable", sha
    return (None, sha) if t <= deadline else ("late", sha)


def vintage_why(old: Any, new: Any, step_target: str | None = None, series: Iterable[str] = (HEADLINE, CORE)) -> str | None:
    """Vintage check V1 / V2 (registration п. 4) between two stored copies of one Cleveland nowcast file: None = pass;
    else the first reason: 'same stamp' (no update in between, or a stamp is unreadable: equal values would prove
    nothing), 'no shared points <series>', 'overwritten <series> <target> <day>' (a daily point both copies have
    differs by 1e-9 or more), 'no step <target>' (``step_target`` given and the newer copy has no new value of the
    headline after the older copy's stamp for that target, :func:`new_points_after`)."""
    so, sn = file_stamp(old), file_stamp(new)
    if so is None or sn is None or so == sn:
        return "same stamp"
    o, n = _charts(old), _charts(new)
    for s in series:
        pairs = 0
        for t, ch in o.items():
            if t not in n:
                continue
            dn = dict(cf_daily(n[t], s)[1])
            for d, v in cf_daily(ch, s)[1]:
                w = dn.get(d)
                if v is None or w is None:
                    continue
                pairs += 1
                if abs(w - v) >= 1e-9:
                    return f"overwritten {s} {t} {d}"
        if pairs == 0:
            return f"no shared points {s}"
    if step_target is not None:
        r = next((x for x in new_points_after(old, new, HEADLINE) if same_target(x["target"], step_target)), None)
        if r is None or not r["changed"]:
            return f"no step {step_target}"
    return None


# ------------------------------------------------------------------ archive SD (registration п. 4, after V1 and V2)


def yoy_published(levels: Mapping[str, Any]) -> dict[str, float]:
    """{'YYYY-MM': CPI-U NSA index level (CPIAUCNS, as text or number)} -> {'YYYY-M' target: 12-month % change rounded
    half-up to 0.1, as BLS prints it}. A month without its own level or the level 12 months before is left out
    (October 2025 was never published, so the targets 2025-10 and 2026-10 have no value)."""
    lv: dict[tuple[int, int], Decimal] = {}
    for k, v in levels.items():
        try:
            y, m = (int(x) for x in str(k)[:7].split("-"))
            d = Decimal(str(v))
        except (ValueError, InvalidOperation):
            continue
        if d.is_finite() and d > 0:
            lv[(y, m)] = d
    out = {}
    for (y, m), d in sorted(lv.items()):
        prev = lv.get((y - 1, m))
        if prev is not None:
            out[f"{y}-{m}"] = float((100 * (d / prev - 1)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return out


def archive_errors(data: Any, published: Mapping[str, float], before: str, first: str = "2014-1") -> list[dict[str, Any]]:
    """Nowcast errors of the headline YoY for every target m from ``first`` whose release (the 'CPI <Mon>' line of its
    chart) is before ``before`` (ISO; the T-3 day of the release that switches): N_m = the last headline point dated on
    or before release - 3 days, A_m = ``published[m]``; error = A_m - N_m (pp). A target without the release line (no
    CPI, as 2025-10), without such a point or without A_m is listed with ``error`` None and a ``why``."""
    out = []
    for target, ch in _charts(data).items():
        if target_key(target) < target_key(first):
            continue
        _, pts, vl = cf_daily(ch, HEADLINE)
        rel = vl.get(f"CPI {MONTHS[target_key(target)[1] - 1]}")
        r: dict[str, Any] = {"target": target, "release": rel, "day": None, "nowcast": None, "actual": None, "error": None}
        if rel is None:
            out.append({**r, "why": "no release"})
            continue
        if rel >= before:
            continue
        cut = (date.fromisoformat(rel) - timedelta(days=3)).isoformat()
        day, val = next(((d, v) for d, v in reversed(pts) if d <= cut and v is not None), (None, None))
        a = published.get(target)
        r.update(day=day, nowcast=val, actual=a)
        if val is None:
            out.append({**r, "why": "no point"})
        elif a is None:
            out.append({**r, "why": "no actual"})
        else:
            out.append({**r, "error": a - val, "why": None})
    return out


def sigma_archive(errors: Iterable[Mapping[str, Any]]) -> tuple[float | None, int]:
    """(sqrt(mean(error^2)), n) over the rows with an error: no bias correction, no floor, no weights; (None, 0)
    without any."""
    e = [float(r["error"]) for r in errors if _finite(r.get("error"))]
    return (math.sqrt(sum(x * x for x in e) / len(e)), len(e)) if e else (None, 0)


# ------------------------------------------------------------- the result (registration п. 7, 8; item 491)
# Written in main before the release of 2026-12-10, so the CLV / Brier code was fixed without knowing any outcome.


def fred_levels(csv_text: Any) -> dict[str, str]:
    """A FRED graph CSV (a header line, then 'YYYY-MM-DD,value') -> {'YYYY-MM': the value as text}, for
    :func:`yoy_published`; an empty or '.' value (October 2025: never published) and a line that does not read are
    left out."""
    out: dict[str, str] = {}
    if not isinstance(csv_text, str):
        return out
    for line in csv_text.splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0]) and parts[1] not in ("", "."):
            out[parts[0][:7]] = parts[1]
    return out


RESULT_CUTOFF_DAYS = 30  # registration п. 7, result (item 491, review C, blocker 2): from RESULT_FROM of release + 30 days
# The only payouts of a resolved YES / NO market of Polymarket: YES won, NO won, or the 50 / 50 split (review C of item
# 491, blocker 3: last trading prices such as 0.9995 / 0.0005 can stand in ``outcomePrices`` of a market that reads
# 'resolved' and are not a payout).
PAYOUTS = ((1.0, 0.0), (0.0, 1.0), (0.5, 0.5))


def result_cutoff(release: str) -> datetime:
    """The end of the wait of the result stage (registration п. 7, as amended 27.11 by item 491): ``RESULT_CUTOFF_DAYS``
    days after the first moment of its window (15:00 UTC of the release day), from the release day alone. From then on
    a market that has answered without a resolution is written under ``absent`` (its event gets no Brier, 'no payout')
    and a CPI that FRED does not have makes the release invalid (п. 5)."""
    return stage_window("result", release)[0] + timedelta(days=RESULT_CUTOFF_DAYS)


def pm_resolution(market: Any) -> dict[str, Any]:
    """Polymarket's resolution of one YES / NO market from its Gamma ``/markets/{id}`` answer (registration п. 7, 8:
    the payout of Polymarket counts). -> {"state", "yes", "closed", "uma_status"}: 'resolved' when ``closed`` is True,
    ``umaResolutionStatus`` is 'resolved', ``outcomes`` are ['Yes', 'No'] and ``outcomePrices`` one of ``PAYOUTS``
    ([1, 0], [0, 1] or [0.5, 0.5]); then ``yes`` = the payout of a YES share in USD (1, 0 or 0.5). Else ``yes`` None
    and the state 'open' (not closed), 'pending' (closed, the UMA status not 'resolved') or 'unreadable' (another
    shape, including prices that are not a payout: not final, the stage waits)."""
    if not isinstance(market, Mapping):
        return {"state": "unreadable", "yes": None, "closed": None, "uma_status": None}
    closed, status = market.get("closed"), market.get("umaResolutionStatus")
    base = {"closed": closed if isinstance(closed, bool) else None, "uma_status": status if isinstance(status, str) else None}
    if closed is not True:
        return {"state": "open", "yes": None, **base}
    if status != "resolved":
        return {"state": "pending", "yes": None, **base}
    outs, prices = json_list(market.get("outcomes")), json_list(market.get("outcomePrices"))
    p = [_payout(x) for x in prices or []]
    if [str(o).strip().lower() for o in outs or []] != ["yes", "no"] or tuple(p) not in PAYOUTS:
        return {"state": "unreadable", "yes": None, **base}
    return {"state": "resolved", "yes": p[0], **base}


def _payout(v: Any) -> float | None:
    """A payout price of ``outcomePrices`` ('0', '1', '0.5', or a number) as a finite float, else None."""
    if isinstance(v, bool) or not isinstance(v, str | int | float):
        return None
    try:
        x = float(v)
    except (ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def _tenths(value: Any) -> int | None:
    return round(float(value) * 10) if _finite(value) else None


def contains(interval: Any, value: Any) -> bool:
    """The bracket ``interval`` (:func:`parse_bracket`: tenths of a point, inclusive, None = open) holds the published
    12-month change ``value`` (percent, one decimal)."""
    v = _tenths(value)
    if v is None or not isinstance(interval, list | tuple) or len(interval) != 2:
        return False
    lo, hi = interval
    return (lo is None or lo <= v) and (hi is None or v <= hi)


def resolution_check(brackets_: Iterable[Mapping[str, Any]], cpi: Any) -> list[dict[str, Any]]:
    """Per event of ``brackets_`` ({"event_id", "market_id", "title", "yes"}: ``yes`` = Polymarket's YES payout) the
    market ids Polymarket paid in full (``pm_yes``, payout 1) and the ids whose title holds the published value ``cpi``
    (``cpi_yes``); ``agree`` when both are the same single market. Registration п. 7: when they differ, the payout of
    Polymarket counts, and both values are written."""
    by: dict[str, list[Mapping[str, Any]]] = {}
    for b in brackets_:
        by.setdefault(str(b.get("event_id")), []).append(b)
    out = []
    for eid in sorted(by):
        bs = by[eid]
        pm = [str(b.get("market_id")) for b in bs if _finite(b.get("yes")) and b["yes"] >= 1.0 - 1e-9]
        bl = [str(b.get("market_id")) for b in bs if contains(parse_bracket(b.get("title")), cpi)]
        out.append({"event_id": eid, "pm_yes": pm, "cpi_yes": bl, "agree": len(pm) == 1 and pm == bl})
    return out


def market_prob(bid: Any, ask: Any) -> float:
    """The market's probability of one bracket for the Brier score (registration п. 8), before the normalisation: the
    mid; ask / 2 without a bid, (bid + 1) / 2 without an ask; 0 without any quote."""
    b = float(bid) if _finite(bid) else None
    a = float(ask) if _finite(ask) else None
    if b is not None and a is not None:
        return (b + a) / 2
    if a is not None:
        return a / 2
    if b is not None:
        return (b + 1) / 2
    return 0.0


def brier_release(rows: Iterable[Mapping[str, Any]], payout: Mapping[str, Any]) -> dict[str, Any]:
    """Brier at T-3 of the model and of the market (registration п. 8) over every event of the release with a model.
    ``rows``: the ``rule_signals`` of the T-3 decision (``event``, ``market_id``, ``q``, ``bid``, ``ask``; a row with
    ``q`` None has no model and is left out); ``payout``: {market_id: Polymarket's YES payout} (the outcome y). Per
    event: the market vector p of :func:`market_prob` normalised to a sum of 1, Brier = sum (p_i - y_i)^2, the model
    the same with q. An event without any quote (every p 0) is in neither score ('no quotes'), an event with a bracket
    without a payout neither ('no payout'). The release: the mean over the events counted, ``delta`` = model - market
    (ΔBrier < 0: the model is better); None without an event counted. ``market_raw`` (not normalised) is descriptive."""
    by: dict[Any, list[Mapping[str, Any]]] = {}
    for r in rows:
        if isinstance(r, Mapping) and _finite(r.get("q")):
            by.setdefault(r.get("event"), []).append(r)
    events = []
    for k in sorted(by, key=str):
        rs = by[k]
        p = [market_prob(r.get("bid"), r.get("ask")) for r in rs]
        y = [payout.get(str(r.get("market_id"))) for r in rs]
        e: dict[str, Any] = {"event": k, "n_brackets": len(rs), "model": None, "market": None, "market_raw": None, "why": None}
        if not all(_finite(v) for v in y):
            e["why"] = "no payout"
        elif sum(p) <= 0:
            e["why"] = "no quotes"
        else:
            s = sum(p)
            e["model"] = sum((float(r["q"]) - float(v)) ** 2 for r, v in zip(rs, y, strict=True))
            e["market"] = sum((pi / s - float(v)) ** 2 for pi, v in zip(p, y, strict=True))
            e["market_raw"] = sum((pi - float(v)) ** 2 for pi, v in zip(p, y, strict=True))
        events.append(e)
    used = [e for e in events if e["why"] is None]
    n = len(used)
    model = sum(e["model"] for e in used) / n if n else None
    market = sum(e["market"] for e in used) / n if n else None
    raw = sum(e["market_raw"] for e in used) / n if n else None
    return {"events": events, "n_events": n, "model": model, "market": market,
            "delta": model - market if model is not None and market is not None else None, "market_raw": raw}  # fmt: skip


def signal_figures(fill_: Mapping[str, Any], bid1: Any, ask1: Any, payout: Any, exit_fee: Any = None) -> dict[str, Any]:
    """CLV and the paper result of one signal (registration п. 8) from its ``fill`` (``shares``, ``p_in``,
    ``cost_usd`` with fee, ``fee_usd``, ``executable``), the T-1 quote of its token (:func:`close_quote`),
    Polymarket's YES payout and ``exit_fee``, the taker fee per share of a sale at the T-1 bid (:func:`taker_fee` with
    the bracket's fee fields). ``clv`` (:func:`clv`; USD per signal is the primary unit) only for an executable
    signal; descriptive: ``usd_per_cost`` = CLV$ / cost, ``net_usd`` = CLV$ - the entry fee, ``exit_fee_usd`` = shares
    x ``exit_fee``, ``exit_usd`` = shares x (bid T-1 - p_in) - the entry fee - the exit fee (п. 8 «виконуваний вихід
    … − комісія»: both taker fees of the round trip), ``paper_usd`` = shares x payout - cost (net of the entry fee; no
    fee at resolution). Any of them None when an input is missing (never filled in)."""
    sh, p_in, cost, fee = (fill_.get(k) for k in ("shares", "p_in", "cost_usd", "fee_usd"))
    c = clv(p_in, sh, bid1, ask1) if fill_.get("executable") is True else None
    ok = c is not None and _finite(cost) and _finite(fee)
    out_fee = sh * exit_fee if ok and _finite(exit_fee) else None
    return {
        "clv": c,
        "usd_per_cost": c["usd"] / cost if ok and cost > 0 else None,
        "net_usd": c["usd"] - fee if ok else None,
        "exit_fee_usd": out_fee,
        "exit_usd": sh * (bid1 - p_in) - fee - out_fee if out_fee is not None else None,
        "paper_usd": sh * payout - cost if all(_finite(x) for x in (sh, payout, cost)) else None,
    }


def clv_release(figures: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """CLV_rel of a release (registration п. 8): the sum of CLV$ over its executable signals with a T-1 mid, and their
    number; ``usd`` None when there is none (such a release does not enter the mean CLV)."""
    v = [float(f["clv"]["usd"]) for f in figures if isinstance(f.get("clv"), Mapping) and _finite(f["clv"].get("usd"))]
    return {"usd": sum(v) if v else None, "n": len(v)}
