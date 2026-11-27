"""Forward scanner of IDEA-20261028-pm-cpi-brackets-vs-nowcast-forward (step 2, item 484 of the standing card): read-only,
nothing here places, changes or cancels an order. The rule is the card's «Попередня реєстрація» (п. 2-8); where this
code and that text differ, the text holds and the code is fixed.

Stages (C = ~/.cache/tradedesk-agents/pm_cpi; every stage file is written once, only inside its window):
    uv run python scripts/pm_cpi_forward.py --stage T-7 --release 2026-12-10 --out C/out_fwd_T-7.txt   # record (item 489)
    uv run python scripts/pm_cpi_forward.py --stage T-3 --release 2026-12-10 --out C/out_fwd_T-3.txt   # decision (item 486)
    uv run python scripts/pm_cpi_forward.py --stage T-1 --release 2026-12-10 --out C/out_fwd_T-1.txt   # close, CLV (item 487)
    uv run python scripts/pm_cpi_forward.py --stage proof --release 2026-12-10 --out C/out_fwd_proof.txt  # proof (item 487)
    uv run python scripts/pm_cpi_forward.py --stage result --release 2026-12-10 --out C/out_fwd_result.txt  # (items 487, 493)
    uv run python scripts/pm_cpi_forward.py --stage vrows --line V1 --out C/out_vrows_V1.txt  # V1 / V2 / sigma_A line (485, 488)

Result (item 491, registration п. 7, 8, as amended 27.11): from 15:00 UTC of the release day. It runs the code of R,
the last first-parent state of ``origin/main`` before the publication (08:30 New York time of the release day, 13:30
UTC on 2026-12-10), so its CLV / Brier code knew no outcome; refused when the clone's last fetch is before that moment
or when the scanner of R has no result stage (never HEAD instead). It reads of ``T-3.json`` (main copy, else local)
only the ``REBUILT`` fields and the Gamma part, the job's T-1 row (``close_quote``, the database row applies) and asks
FRED for CPIAUCNS (the published 12-month change, ``yoy_published``) and Gamma ``/markets/{id}`` for the resolution of
every market of the YoY events. It writes ``result.json`` only when every input is final: the CPI published and every
market resolved; otherwise nothing is written, every reason is printed, and the stage is run again later (item 493).
From the cutoff (``result_cutoff``: 15:00 UTC of the release day + 30 days) a market that answered without a
resolution and a CPI that FRED does not have are written under ``absent`` instead (a failed request still waits).
What can never come (no T-3 record, no decision, no T-1 row) is written under ``absent``, never filled in.

Source (п. 5): only the collector's ``pm_cpi_snapshot`` row of day T-d, read from the database with ``mode=ro``
(``verify_pm_cpi_step0.read_rows``); no snapshot directory and no other day is ever read. T-3 adds, from Gamma
(``rt.polymarket``, through ``core.http.Http``), what the row does not keep: ``feesEnabled``, ``feeSchedule``,
``feeType``, ``orderMinSize`` and the rule text of every market of the YoY events (``/markets/{id}``). Every write stage
asks ``/events/{id}`` of every open event of the row for its title (descriptive; ``/markets/{id}`` has no ``events``
field). A failed Gamma request is recorded in the file and the registered fallbacks apply (fee by the row's
``fee_type``, an event without a description taken by its slug). The decision is the pure :func:`decide` on the
decision input kept whole in the file.

Code (п. 7, amended 27.11 by item 484): a write stage runs its code from a tree extracted from git, never from the
working copy: T-3 from M0 (``git log origin/main --first-parent --until=<T-3 day>T21:00:00Z -1``; refused when the
clone's last fetch, the mtime of ``FETCH_HEAD``, is before 21:00 UTC of day T-3), the result from R (above), T-7 and T-1
from HEAD. Every path of that commit's ``CODE_PATHS`` is written by ``git show <sha>:<path>`` into
``~/.cache/tradedesk-agents/tmp/pm_cpi_code_<sha[:12]>/`` and the stage runs in a subprocess with that tree first in
``sys.path``. ``code_dirty`` (empty = clean) lists tree files whose blob differs from the commit's and every imported
``tradedesk.*`` / scanner module that is not one of ``CODE_PATHS`` or not in the tree. From the command line T-7 / T-3 /
T-1 start no earlier than 22:00 UTC of day T-d (``LAUNCH_HOUR_UTC``, item 495; earlier: refused, nothing written), while
the window of a valid record stays ``stage_window`` (from 21:05).

Files: ``~/.local/state/tradedesk-agents/pm_cpi/forward/<release>/<stage>.json`` and the same bytes in the clone's
``docs/forward/pm-cpi/<release>/<stage>.json`` (both created with ``open(..., "x")``; an existing file is never
overwritten). ``--stage proof`` (item 487) checks the merged copy of ``T-3.json`` with git, the database (read-only)
and, by the code of M0 (``code_sha`` must be M0), a recomputation of the decision from the file's input and a rebuild
of the whole record from the database row, the file's Gamma answers and the card in M0; after the first release it
also re-runs the commands of п. 10 (``verify_pm_cpi_step0.py`` of the tree of M0: row export, vcheck, sigma; the V1
snapshot S and the FRED CSV of the sigma_A line besides) for every V1 / V2 / sigma_A journal line the sigma rests on (item 492; a line no command
decided is ``not recomputed``: no verdict, run again); its first line names its arguments, its last line is ``proof
pass`` or ``proof fail: <reason>``. The sigma command runs on the FRED CSV the sigma_A line names (its sha256), from
``docs/forward/pm-cpi/fred/`` of the ref, with ``--vintage`` and no network (review C of item 492, N2).

``--stage vrows --line V1 | V2 | sigma_A`` (dated steps 485, 488; review C of item 492, N1) runs the same commands with
the same classifier (:func:`vrows_repro`) on the rows of the database (read-only) and exits 0 only for a verdict of the
data, printing the journal line to write (sigma_A: the FRED CSV it used is saved under ``docs/forward/pm-cpi/fred/``);
any other end exits 3, nothing is to be written, and the step is run again.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from tradedesk.analytics.option_xvenue import num
from tradedesk.analytics.pm_cpi import (
    ROW_DIGEST_COLUMNS,
    SD_KZ,
    STAGE_DAYS,
    book_why,
    books_of,
    brier_release,
    close_quote,
    clv,
    clv_release,
    digest_preimage,
    fill,
    fred_levels,
    merge_proof_why,
    model_ok,
    nowcast_ts,
    pct,
    pm_resolution,
    resolution_check,
    result_cutoff,
    row_digest,
    row_why,
    rule_signals,
    same_record,
    same_target,
    signal_figures,
    stage_why,
    stage_window,
    taker_fee,
    target_key,
    yoy_published,
)

IDEA = "pm-cpi-brackets-vs-nowcast-forward"
CARD = "docs/ideas/IDEA-20261028-pm-cpi-brackets-vs-nowcast-forward.md"
DOCS_DIR = "docs/forward/pm-cpi"
REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path("~/.local/state/tradedesk-agents/pm_cpi/forward").expanduser()
DEFAULT_TMP = Path("~/.cache/tradedesk-agents/tmp").expanduser()
DEFAULT_DB = Path("/var/lib/tradedesk-mcp/tradedesk.sqlite")  # as verify_pm_cpi_step0.DEFAULT_DB
DEFAULT_REF = "origin/main"
WRITE_STAGES = ("T-7", "T-3", "T-1")
# registration п. 5, sixth part of the amendment of 27.11 (item 494) and item 495: `launch` starts a write stage no
# earlier than this on day T-d. A collector tick started before 21:00 decides its phase first and upserts the day's row
# at its end, so a stage run at 21:05 could read a row still being written. Only `launch` checks it: `stage_window`
# (21:05) still defines a valid record, and the proof never calls `launch`, so no written file is rejected by it.
LAUNCH_HOUR_UTC = 22
RESULT = "result"  # item 491: written from 15:00 UTC of the release day, only once every input is final
FRED_URL = "https://fred.stlouisfed.org"
CPI_SERIES = "CPIAUCNS"  # BLS CPI-U, NSA: the published 12-month change (registration п. 4, A_m; п. 7, 8, result)
FIRST_RELEASE = "2026-12-10"  # registration п. 4: this release is counted with sigma = 0.164 in any case
JOB_SERIES = ("5", "10007", "10008", "11905", "12054")  # collector/pm_cpi_snapshot.SERIES (a test keeps them equal)
RULE_PHRASES = ("12-month", "before seasonal adjustment", "one decimal")  # registration п. 2
SD_KZ_SOURCE = "SD_KZ 0.164: Knotek, Zaman, Economic Commentary 2023-06, Table 1 (CPI, last day of the month)"
SCRIPT_MODULES = ("pm_cpi_forward", "verify_pm_cpi_step0")
# Every file this scanner imports on its way through every stage (scripts and the tradedesk.* modules, packages'
# __init__.py included), as tests/test_pm_cpi_forward.py generates it in a subprocess on the fixture; the test fails when
# this tuple and that list differ. A write stage extracts exactly these paths of its commit (registration п. 7).
CODE_PATHS = (
    "scripts/pm_cpi_forward.py",
    "scripts/verify_pm_cpi_step0.py",
    "src/tradedesk/__init__.py",
    "src/tradedesk/analytics/__init__.py",
    "src/tradedesk/analytics/_text.py",
    "src/tradedesk/analytics/bitcoin.py",
    "src/tradedesk/analytics/chain_metrics.py",
    "src/tradedesk/analytics/evm.py",
    "src/tradedesk/analytics/instruments.py",
    "src/tradedesk/analytics/liquidation_flush.py",
    "src/tradedesk/analytics/option_xvenue.py",
    "src/tradedesk/analytics/options.py",
    "src/tradedesk/analytics/per_asset.py",
    "src/tradedesk/analytics/pm_calibration.py",
    "src/tradedesk/analytics/pm_cpi.py",
    "src/tradedesk/analytics/pm_gap.py",
    "src/tradedesk/analytics/pm_gap_paper.py",
    "src/tradedesk/analytics/pm_modules.py",
    "src/tradedesk/analytics/pm_threshold.py",
    "src/tradedesk/analytics/pm_touch.py",
    "src/tradedesk/analytics/probability.py",
    "src/tradedesk/analytics/session_stop_hunt.py",
    "src/tradedesk/analytics/yields.py",
    "src/tradedesk/collector/__init__.py",
    "src/tradedesk/collector/bad_rows.py",
    "src/tradedesk/collector/pm_cpi_snapshot.py",
    "src/tradedesk/collector/scheduler.py",
    "src/tradedesk/config.py",
    "src/tradedesk/core/__init__.py",
    "src/tradedesk/core/cache.py",
    "src/tradedesk/core/envelope.py",
    "src/tradedesk/core/errors.py",
    "src/tradedesk/core/health.py",
    "src/tradedesk/core/http.py",
    "src/tradedesk/core/ratelimit.py",
    "src/tradedesk/providers/__init__.py",
    "src/tradedesk/providers/alternative_me.py",
    "src/tradedesk/providers/binance_futures.py",
    "src/tradedesk/providers/binance_spot.py",
    "src/tradedesk/providers/binance_vision.py",
    "src/tradedesk/providers/bybit.py",
    "src/tradedesk/providers/clevelandfed.py",
    "src/tradedesk/providers/coinalyze.py",
    "src/tradedesk/providers/coinbase.py",
    "src/tradedesk/providers/coingecko.py",
    "src/tradedesk/providers/defillama.py",
    "src/tradedesk/providers/deribit.py",
    "src/tradedesk/providers/dexscreener.py",
    "src/tradedesk/providers/etherscan.py",
    "src/tradedesk/providers/exchanges.py",
    "src/tradedesk/providers/forexfactory.py",
    "src/tradedesk/providers/goplus.py",
    "src/tradedesk/providers/honeypot_is.py",
    "src/tradedesk/providers/hyperliquid.py",
    "src/tradedesk/providers/mempool.py",
    "src/tradedesk/providers/pendle.py",
    "src/tradedesk/providers/polymarket.py",
    "src/tradedesk/providers/thegraph.py",
    "src/tradedesk/providers/wikimedia.py",
    "src/tradedesk/runtime.py",
    "src/tradedesk/store/__init__.py",
    "src/tradedesk/store/db.py",
    "src/tradedesk/store/schema.py",
)

_OUT: Any = None


def say(*parts: Any) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    if _OUT is not None:
        _OUT.write(line + "\n")
        _OUT.flush()


# ------------------------------------------------------------------------------------------------- pure: the events

_MONTHS = {
    **{n: i + 1 for i, n in enumerate(("january", "february", "march", "april", "may", "june", "july", "august"))},
    **{n: i + 9 for i, n in enumerate(("september", "october", "november", "december"))},
    **{n: i + 1 for i, n in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))},
    "sept": 9,
}


def stage_day(stage: str, release: str) -> str:
    """The ISO day T-d of ``stage`` ('T-7', 'T-3', 'T-1', 'result' = the release day)."""
    return (date.fromisoformat(release) - timedelta(days=STAGE_DAYS[stage])).isoformat()


def target_of_release(release: str) -> str:
    """The target month 'YYYY-M' of the events of a release: the month before the release month (the CPI of month m is
    released in month m + 1; registration п. 2, п. 5 as amended 27.11, item 484)."""
    d = date.fromisoformat(release)
    return f"{d.year - 1}-12" if d.month == 1 else f"{d.year}-{d.month - 1}"


def event_month(slug: Any, end_date: Any) -> str | None:
    """Registration п. 2: the month is the last month word of the slug, the year the one for which ``end_date`` falls
    in the next month; None when the slug has no month word, the end date does not parse or is not in that next month."""
    toks = [_MONTHS[t] for t in str(slug or "").lower().split("-") if t in _MONTHS]
    try:
        end = date.fromisoformat(str(end_date)[:10])
    except ValueError:
        return None
    if not toks:
        return None
    m = toks[-1]
    y, pm = (end.year - 1, 12) if end.month == 1 else (end.year, end.month - 1)
    return f"{y}-{m}" if pm == m else None


def select_events(brackets_: Iterable[Any], row_target: Any) -> list[dict[str, Any]]:
    """Every open event of the row's main set (one entry per ``event_id``, ordered by slug, then id) with whether it is a
    headline YoY event of the release (registration п. 2) and why not: 'series' (not a series of the job), 'not headline
    YoY' (the slug lacks 'inflation' or 'annual', or has 'core': renamed events are not taken), 'month' (no month word
    or the end date is not in the month after it), 'target' (the month is not the row's ``target``)."""
    by: dict[str, list[Mapping[str, Any]]] = {}
    for b in brackets_:
        if isinstance(b, Mapping):
            by.setdefault(str(b.get("event_id")), []).append(b)
    out = []
    for eid, bs in sorted(by.items(), key=lambda kv: (str(kv[1][0].get("event_slug")), kv[0])):
        slug = str(bs[0].get("event_slug") or "")
        s = slug.lower()
        month = event_month(slug, bs[0].get("end_date"))
        if str(bs[0].get("series")) not in JOB_SERIES:
            why = "series"
        elif "inflation" not in s or "annual" not in s or "core" in s:
            why = "not headline YoY"
        elif month is None:
            why = "month"
        elif not same_target(month, row_target):
            why = "target"
        else:
            why = None
        out.append({"event_id": eid, "series": str(bs[0].get("series")), "slug": slug, "end_date": bs[0].get("end_date"),
                    "month": month, "title": None, "n_brackets": len(bs), "taken": why is None, "why": why,
                    "rules": None})  # fmt: skip
    return out


def phrases_of(text: Any) -> dict[str, bool]:
    t = text.lower() if isinstance(text, str) else ""
    return {p: p in t for p in RULE_PHRASES}


def rules_why(descriptions: Iterable[Any]) -> tuple[str, str | None]:
    """(rules state, exclusion reason) of one event from the rule texts Gamma gave for its markets (registration п. 2):
    no text got -> ('unchecked', None), the event is taken by its slug; every text got has every phrase -> ('ok', None);
    else ('missing', "rules: no '<phrase>'")."""
    got = [d for d in descriptions if isinstance(d, str) and d.strip()]
    if not got:
        return "unchecked", None
    for p in RULE_PHRASES:
        if not all(phrases_of(d)[p] for d in got):
            return "missing", f"rules: no '{p}'"
    return "ok", None


def _bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def gamma_fields(market: Any, market_id: Any) -> dict[str, Any]:
    """What the T-3 record keeps of a Gamma ``/markets/{id}`` answer (untrusted text, stored as is; non-finite numbers
    as null): fee fields, ``orderMinSize``, the full rule text with its sha256 and the phrases found; ``closed``,
    ``active``, ``acceptingOrders`` descriptively (the decision reads ``gone_ts`` of the row, registration п. 6)."""
    if not isinstance(market, Mapping) or str(market.get("id")) != str(market_id):
        return {"ok": False, "why": "not this market"}
    desc = market.get("description") if isinstance(market.get("description"), str) else None
    fs = market.get("feeSchedule")
    if isinstance(fs, str):
        try:
            fs = json.loads(fs)
        except (ValueError, RecursionError):
            fs = None
    return strict({
        "ok": True,
        "feesEnabled": _bool(market.get("feesEnabled")),
        "feeSchedule": fs if isinstance(fs, Mapping) else None,
        "feeType": market.get("feeType") if isinstance(market.get("feeType"), str) else None,
        "orderMinSize": num(market.get("orderMinSize")),
        "description": desc,
        "description_sha256": hashlib.sha256(desc.encode()).hexdigest() if desc is not None else None,
        "phrases": phrases_of(desc),
        "closed": _bool(market.get("closed")),
        "active": _bool(market.get("active")),
        "acceptingOrders": _bool(market.get("acceptingOrders")),
    })  # fmt: skip


GAMMA_KEYS = ("ok", "feesEnabled", "feeSchedule", "feeType", "orderMinSize", "description", "description_sha256", "phrases",
              "closed", "active", "acceptingOrders")  # fmt: skip
EVENT_KEYS = ("ok", "title", "slug", "closed", "active")


def event_fields(event: Any, event_id: Any) -> dict[str, Any]:
    """What a stage record keeps of a Gamma ``/events/{id}`` answer (untrusted text, stored as is): title, slug,
    ``closed``, ``active`` (registration п. 2: the title of every open event, descriptive)."""
    if not isinstance(event, Mapping) or str(event.get("id")) != str(event_id):
        return {"ok": False, "why": "not this event"}
    t, s = event.get("title"), event.get("slug")
    return {"ok": True, "title": t if isinstance(t, str) else None, "slug": s if isinstance(s, str) else None,
            "closed": _bool(event.get("closed")), "active": _bool(event.get("active"))}  # fmt: skip


def with_titles(events: list[dict[str, Any]], gamma_events: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``events`` with the title of each from its ``/events/{id}`` answer (None when Gamma gave none)."""
    for e in events:
        g = gamma_events.get(e["event_id"]) or {}
        e["title"] = g.get("title") if g.get("ok") else None
    return events


def gamma_why(rec: Mapping[str, Any], mids: Iterable[str], eids: Iterable[str]) -> list[str]:
    """Problems of the Gamma part of a T-3 record against what the scanner asks (proof п. 7 (2)): the keys of
    ``gamma`` / ``gamma_events`` are exactly the market ids and event ids asked; every answer has the shape the scanner
    writes, its sha256 and phrases follow its text; a failed request is in ``missing`` and only then. The values Gamma
    gave at D are not reproducible: an answer replaced wholesale before ``git add`` is beyond this check."""
    out: list[str] = []
    miss = {str((m or {}).get("field")) for m in rec.get("missing") or [] if isinstance(m, Mapping)}
    for name, got, want, keys, prefix in (("gamma", rec.get("gamma"), mids, GAMMA_KEYS, "market"),
                                          ("gamma_events", rec.get("gamma_events"), eids, EVENT_KEYS, "event")):  # fmt: skip
        if not isinstance(got, Mapping):
            out.append(f"{name} not a mapping")
            continue
        if set(got) != set(want):
            out.append(f"{name} keys ≠ asked ({len(got)} vs {len(set(want))})")
        for k, g in got.items():
            failed = isinstance(g, Mapping) and g.get("ok") is False and g.get("why") == "request failed"
            if failed != (f"{prefix}:{k}" in miss):
                out.append(f"{name} {k}: request failed ≠ missing")
            if not isinstance(g, Mapping):
                out.append(f"{name} {k}: not a mapping")
            elif g.get("ok") is True:
                if tuple(sorted(g)) != tuple(sorted(keys)):
                    out.append(f"{name} {k}: keys")
                elif prefix == "market":
                    d = g.get("description")
                    sha = hashlib.sha256(d.encode()).hexdigest() if isinstance(d, str) else None
                    if g.get("description_sha256") != sha or g.get("phrases") != phrases_of(d):
                        out.append(f"{name} {k}: sha256 / phrases ≠ text")
            elif not (g.get("ok") is False and set(g) == {"ok", "why"}):
                out.append(f"{name} {k}: shape")
    return out


def strict(x: Any) -> Any:
    """``x`` with every non-finite float replaced by None, so it can be written as strict JSON (``allow_nan=False``)."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, Mapping):
        return {k: strict(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [strict(v) for v in x]
    return x


def price(v: Any) -> float | None:
    """A price of the row as a decision input: a finite number, else None (a NaN / inf ask is 'no ask', item 484 and
    fourth review B 481, finding 5)."""
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else None


def main_brackets(row: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [dict(b) for b in (books_of(row) or {}).get("brackets") or [] if isinstance(b, Mapping)]


def unpack_depth(blob: Any) -> Any:
    """``books_depth_z`` decompressed and parsed (NaN / Infinity literals read as null); {'error': ...} when it does not
    decompress or parse, None without a blob."""
    if blob is None:
        return None
    try:
        return json.loads(zlib.decompress(bytes(blob)), parse_constant=lambda _: None)
    except (zlib.error, ValueError, TypeError, RecursionError) as e:
        return {"error": f"{type(e).__name__}"}


def row_state(row: Mapping[str, Any]) -> dict[str, Any]:
    """The ``ROW_DIGEST_COLUMNS`` of the row as stored, ``books_depth_z`` unpacked (descriptive; the digest is checked
    on ``digest_preimage``)."""
    return strict({c: unpack_depth(row.get(c)) if c == "books_depth_z" else row.get(c) for c in ROW_DIGEST_COLUMNS})


def bracket_input(b: Mapping[str, Any], since: int | None, g: Mapping[str, Any] | None) -> dict[str, Any]:
    """One bracket of the decision input: the row's main-set book (bid / ask finite or None), the row's ``fee_type`` and
    the market's own Gamma ``feeSchedule`` / ``feesEnabled`` (None when Gamma gave none), ``why`` from :func:`book_why`."""
    bid, ask = price(b.get("bid")), price(b.get("ask"))
    g = g if g and g.get("ok") else {}
    return {
        "event_id": str(b.get("event_id")), "event_slug": b.get("event_slug"), "series": str(b.get("series")),
        "market_id": str(b.get("market_id")), "token": str(b.get("token")), "title": b.get("title"),
        "bid": bid, "ask": ask, "mid": (bid + ask) / 2 if bid is not None and ask is not None else None,
        "bid_usd": price(b.get("bid_usd")), "ask_usd": price(b.get("ask_usd")), "read_ts": b.get("read_ts"),
        "gone_ts": b.get("gone_ts"), "fee_type": b.get("fee_type"), "fee_schedule": g.get("feeSchedule"),
        "fees_enabled": g.get("feesEnabled"), "why": book_why(b, since),
    }  # fmt: skip


def depth_input(depth: Any, token: str) -> dict[str, Any]:
    """The ask levels ([price, shares], finite only) and ``min_size`` of the token's book in ``books_depth_z`` (main)."""
    d = ((depth or {}).get("main") or {}).get(token) if isinstance(depth, Mapping) else None
    d = d if isinstance(d, Mapping) else {}
    asks = [[price(x[0]), price(x[1])] for x in d.get("asks") or [] if isinstance(x, list | tuple) and len(x) >= 2]
    return {"asks": [x for x in asks if x[0] is not None and x[1] is not None], "min_size": price(d.get("min_size")),
            "read_ts": d.get("read_ts")}  # fmt: skip


def decide(inp: Mapping[str, Any]) -> dict[str, Any]:
    """The decision of T-3 from its input alone (registration п. 6): ``rule_signals`` of the YoY events with mu and sd,
    then ``fill`` of every pick along the ask levels of its book. The same function recomputes it in ``--stage proof``."""
    rs = rule_signals(inp.get("events") or [], inp.get("mu"), inp.get("sd"))
    fills = []
    for r in rs:
        if r["pick"]:
            d = (inp.get("depth") or {}).get(str(r.get("token"))) or {}
            f = fill(d.get("asks") or [], r["q"], r.get("fee_type"), r.get("fee_schedule"), r.get("fees_enabled"), d.get("min_size"))
            fills.append({"market_id": r.get("market_id"), "token": r.get("token"), "title": r.get("title"), **f})
    return {"rule_signals": rs, "fills": fills}


def t3_skip(n_events: int, decision: Mapping[str, Any], mu: Any, sd: Any) -> str | None:
    """Why the release is a «пропуск T−3» after the decision (registration п. 2, 6, 8), None for a valid record: 'no YoY
    event' (no event left after the selection, the rule phrases and the partition), 'model', 'no quotes' (no event with
    a model has a bid or an ask)."""
    if n_events == 0:
        return "no YoY event"
    if not model_ok(mu, sd):
        return "model"
    rs = decision.get("rule_signals") or []
    modelled = {r["event"] for r in rs if r.get("q") is not None}
    if not modelled:
        return "no YoY event"
    if not any(r["event"] in modelled and (r.get("bid") is not None or r.get("ask") is not None) for r in rs):
        return "no quotes"
    return None


def journal_lines(card: str) -> list[str]:
    """The lines of the card's «Форвард-журнал» section."""
    out, inside = [], False
    for line in card.splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## Форвард-журнал"
            continue
        if inside:
            out.append(line)
    return out


def _clean(line: str) -> str:
    """A journal line as compared: list marker, backticks and an optional pasted ``journal:`` prefix dropped."""
    s = line.strip().lstrip("-* ").replace("`", "").strip()
    return s[len("journal:") :].strip() if s.startswith("journal:") else s


def one_line(text: str) -> str:
    """``text`` with every control character (newlines included) replaced by a space: titles in a journal line are
    untrusted text, and a line break would move the sha256 off the line with the key."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f  ]", " ", text)


_DATE = r"(?:\d{4}-\d{2}-\d{2} )?"  # a journal line may start with its day, as the stage lines do


def sigma_for(release: str, journal: Iterable[str]) -> tuple[float | None, str | None, str | None]:
    """(sigma, its source, skip reason) of a release (registration п. 4): 0.164 for 2026-12-10 in any case; later, a
    'V1 · fail' or 'V2 · fail' line -> 0.164 for all; both 'pass' -> the first 'sigma_A · <value>' line of the journal
    in M0, none -> skip 'no sigma_A'; a verdict line missing -> skip 'no sigma_A' too (no V verdict in M0 yet), also
    when the other one is 'fail' (item 492, review C N1: a line is written only from a verdict of the data, so a
    missing line is no verdict; stricter than 0.164). The first line of each kind counts; a line may start with
    'YYYY-MM-DD ' (format of the card's «Форвард-журнал»)."""
    if release <= FIRST_RELEASE:
        return SD_KZ, SD_KZ_SOURCE, None
    v = journal_vrows(journal)
    sig = v["sigma_A"]
    if v["V1"] is None or v["V2"] is None:
        return None, None, "no sigma_A"
    if "fail" in (v["V1"], v["V2"]):
        return SD_KZ, SD_KZ_SOURCE + " (a vintage check failed)", None
    if sig is not None and sig > 0:
        return sig, "sigma_A (first line of the journal in M0)", None
    return None, None, "no sigma_A"


# the FRED CSV a sigma_A line names (review C of item 492, N2): its file name and sha256, as --stage vrows prints them
_CSV = re.compile(r"CPIAUCNS_(\d{8})\.csv sha256 ([0-9a-f]{64})(?![0-9a-f])")


def journal_vrows(journal: Iterable[str]) -> dict[str, Any]:
    """The first 'V1 · pass|fail', 'V2 · pass|fail' and 'sigma_A · <value>' lines of the journal: {'V1': 'pass' |
    'fail' | None, 'V2': …, 'sigma_A': float | None, 'sigma_A_csv': [YYYYMMDD, sha256] of the CSV that first sigma_A
    line names, or None}."""
    v: dict[str, Any] = {"V1": None, "V2": None, "sigma_A": None, "sigma_A_csv": None}
    for line in journal:
        s = _clean(line)
        mv = re.match(rf"^{_DATE}(V1|V2) · (pass|fail)", s)
        if mv and v[mv[1]] is None:
            v[mv[1]] = mv[2]
        ms = re.match(rf"^{_DATE}sigma_A · (\d+(?:\.\d+)?)", s)
        if ms and v["sigma_A"] is None:
            v["sigma_A"] = float(ms[1])
            mc = _CSV.search(s)
            v["sigma_A_csv"] = [mc[1], mc[2]] if mc else None
    return v


def vrows_used(release: str, journal: Iterable[str]) -> dict[str, Any]:
    """The journal lines the sigma of a release rests on (registration п. 4, as amended 27.11 by item 492): none for
    the first release (sigma fixed) and none while a V1 or V2 line is missing (the release is «no sigma_A» by
    :func:`sigma_for`); else the first V1 and V2 lines, and the first sigma_A line (with the CSV it names, ``sigma_A_csv``)
    when both pass. The proof checks each of them against the registered commands of п. 10 (:func:`vrows_check`)."""
    if release <= FIRST_RELEASE:
        return {}
    v = journal_vrows(journal)
    if v["V1"] is None or v["V2"] is None:
        return {}
    used = {"V1": v["V1"], "V2": v["V2"]}
    if v["V1"] == v["V2"] == "pass" and v["sigma_A"] is not None:
        used |= {"sigma_A": v["sigma_A"], "sigma_A_csv": v["sigma_A_csv"]}
    return used


def journal_sha(card: str, release: str, stage: str = "T-3") -> tuple[str | None, str | None]:
    """(sha256, None) from the one journal line '<release> T−3 ...' (its last 64-hex token; a pasted 'journal: '
    prefix is allowed), else (None, why)."""
    keys = (f"{release} {stage}", f"{release} {stage.replace('-', '−')}")
    hits = [line for line in journal_lines(card) if _clean(line).startswith(keys)]
    if len(hits) != 1:
        return None, f"{len(hits)} journal lines of {release} {stage}"
    hexes = re.findall(r"\b[0-9a-f]{64}\b", hits[0])
    return (hexes[-1], None) if hexes else (None, "no sha256 on the journal line")


def clean_quote(q: Mapping[str, Any]) -> dict[str, Any]:
    return {"bid": price(q.get("bid")), "ask": price(q.get("ask")), "why": q.get("why")}


def quotes_of(row: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """{token: close_quote} of every bracket of the row's main set (T-7 and T-1 records: prices only, no decision)."""
    return {str(b.get("token")): clean_quote(close_quote(row, b.get("token"))) for b in main_brackets(row)}


def clv_rows(t3: Mapping[str, Any] | None, row: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """CLV of every pick of a T-3 record at the T-1 row (registration п. 8): mid only from ``close_quote`` of that row;
    a non-executable pick, a missing book or side gives ``clv`` None (never filled in)."""
    out = []
    for f in ((t3 or {}).get("decision") or {}).get("fills") or []:
        q = clean_quote(close_quote(row, f.get("token")))
        c = clv(f.get("p_in"), f.get("shares"), q["bid"], q["ask"]) if f.get("executable") else None
        out.append({"market_id": f.get("market_id"), "token": f.get("token"), "title": f.get("title"),
                    "executable": f.get("executable"), "p_in": f.get("p_in"), "shares": f.get("shares"),
                    "bid": q["bid"], "ask": q["ask"], "why": q["why"] if f.get("executable") else "not executable",
                    "clv": c})  # fmt: skip
    return out


# --------------------------------------------------------------------------------------------- pure: stage payloads


def row_payload(stage: str, release: str, row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Source part of every stage file: the row's day, times, digest and its preimage, the row state."""
    day = stage_day(stage, release)
    if row is None:
        return {"source": {"day": day, "why": "no row"}, "updated_ts": None, "row_digest": None, "digest_preimage": None, "row": None}
    src = {k: row.get(k) for k in ("day", "status", "late", "cf_year_ts", "cf_month_ts", "books_ts", "updated_ts")}
    return {"source": {**src, "nowcast_ts": nowcast_ts(row), "why": None}, "updated_ts": row.get("updated_ts"),
            "row_digest": row_digest(row), "digest_preimage": digest_preimage(row), "row": row_state(row)}  # fmt: skip


def t3_prepare(row: Mapping[str, Any] | None, release: str) -> tuple[str | None, list[dict[str, Any]], list[str]]:
    """(row_why, events, market ids to ask Gamma ``/markets/{id}``): every market of a taken YoY event; no market is
    asked when the row cannot carry the stage (the titles of all events come from ``/events/{id}``, :func:`event_ids`)."""
    why = row_why(row, stage_day("T-3", release), target_of_release(release))
    brs = main_brackets(row)
    events = select_events(brs, (row or {}).get("target"))
    if why is not None:
        return why, events, []
    taken = {e["event_id"] for e in events if e["taken"]}
    mids: list[str] = []
    for b in brs:
        mid = str(b.get("market_id"))
        if str(b.get("event_id")) in taken and mid not in mids:
            mids.append(mid)
    return None, events, mids


def event_ids(events: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(e["event_id"]) for e in events]


def t3_asks(why: str | None, sigma: tuple[float | None, str | None, str | None], mids: list[str]) -> list[str]:
    """The market ids T-3 asks Gamma for: none when the row cannot carry the stage or there is no sigma."""
    return list(mids) if why is None and sigma[0] is not None else []


def t3_payload(
    row: Mapping[str, Any] | None,
    release: str,
    why: str | None,
    events: list[dict[str, Any]],
    gamma: Mapping[str, Any],
    gamma_events: Mapping[str, Any],
    missing: list[Any],
    sigma: tuple[float | None, str | None, str | None],
) -> dict[str, Any]:
    """The decision part of ``T-3.json``: events with their titles and the rule-phrase check, Gamma fields, mu and
    sigma, the decision input, :func:`decide` on it and the skip reason (registration п. 5-8)."""
    sd, sd_src, sd_why = sigma
    mu = pct((row or {}).get("yoy_headline_pct"))
    nowcast = {"mu": mu, "target": (row or {}).get("target"), "cf_last_day_year": (row or {}).get("cf_last_day_year"),
               "cf_fresh": (row or {}).get("cf_fresh"), "sigma": sd, "sigma_source": sd_src}  # fmt: skip
    out = {"events": with_titles(events, gamma_events), "gamma": dict(gamma), "gamma_events": dict(gamma_events),
           "missing": list(missing), "nowcast": nowcast, "decision_input": None, "decision": None,
           "skip": why or sd_why}  # fmt: skip
    brs = main_brackets(row)
    if out["skip"] is not None:
        return out
    since = nowcast_ts(row)
    groups = []
    for e in events:
        if not e["taken"]:
            continue
        mine = [b for b in brs if str(b.get("event_id")) == e["event_id"]]
        e["rules"], rw = rules_why((gamma.get(str(b.get("market_id"))) or {}).get("description") for b in mine)
        if rw is not None:
            e["taken"], e["why"] = False, rw
            continue
        groups.append([bracket_input(b, since, gamma.get(str(b.get("market_id")))) for b in mine])
    depth = unpack_depth((row or {}).get("books_depth_z"))
    inp = {"mu": mu, "sd": sd, "events": groups, "depth": {b["token"]: depth_input(depth, b["token"]) for g in groups for b in g}}
    dec = decide(inp)
    out.update(decision_input=inp, decision=dec, skip=t3_skip(len(groups), dec, mu, sd))
    return out


# Fields of T-3.json that --stage proof rebuilds from the database row (registration п. 7 (2), as amended 27.11 after
# review C of item 484): everything but D, the Gamma answers, ``missing`` and the code fields, which it checks apart
# (``code_sha``, ``M0``, ``CODE_PATHS``, ``code_dirty``). ``modules`` is descriptive and checked by nothing: the record
# is valid only with an empty ``code_dirty``, and the proof runs the tree of M0, not the modules the file names (R4).
REBUILT = ("schema", "idea", "release", "stage", "window", "source", "updated_ts", "row_digest", "digest_preimage", "row",
           "events", "nowcast", "decision_input", "decision", "skip")  # fmt: skip
# Every top-level key of T-3.json as write_stage writes it; the proof takes this tuple from the code of M0 and refuses a
# file with another key set, «mismatch (keys)» (registration п. 7 (7), as amended 27.11 by item 490, R3).
T3_KEYS = tuple(sorted((*REBUILT, "D", "gamma", "gamma_events", "missing", "code_sha", "M0", "CODE_PATHS", "modules", "code_dirty")))


def keys_why(rec: Mapping[str, Any], keys: Iterable[str]) -> str | None:
    """None when the top-level keys of ``rec`` are exactly ``keys``, else the difference ('+extra, -missing')."""
    got, want = set(rec), set(keys)
    diff = [f"+{k}" for k in sorted(got - want)] + [f"-{k}" for k in sorted(want - got)]
    return ", ".join(diff) or None


def t3_record(
    row: Mapping[str, Any] | None,
    release: str,
    card_m0: str,
    answers: Callable[[list[str], list[str]], tuple[Mapping[str, Any], Mapping[str, Any], list[Any]]],
    sigma: tuple[float | None, str | None, str | None] | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """(the ``REBUILT`` fields of a T-3 record, market ids asked, event ids asked) from the row of day T-3, the card's
    text in M0 (its «Форвард-журнал» gives sigma after the first release) and ``answers(mids, eids)`` -> (gamma,
    gamma_events, missing): the live requests at D, the file's recorded answers in the proof. ``sigma``: given only by
    :func:`fallback_record` (set B), else :func:`sigma_for` of the card."""
    rwhy, events, mids = t3_prepare(row, release)
    if sigma is None:
        sigma = sigma_for(release, [] if release <= FIRST_RELEASE else journal_lines(card_m0))
    mids, eids = t3_asks(rwhy, sigma, mids), event_ids(events)
    gamma, gamma_events, missing = answers(mids, eids)
    rec: dict[str, Any] = {"schema": 1, "idea": IDEA, "release": release, "stage": "T-3",
                           "window": [str(x) if x else None for x in stage_window("T-3", release)],
                           **row_payload("T-3", release, row),
                           **t3_payload(row, release, rwhy, events, gamma, gamma_events, missing, sigma)}  # fmt: skip
    return rec, mids, eids


def rebuild(rec: Mapping[str, Any], release: str, row: Mapping[str, Any] | None, card_m0: str) -> dict[str, Any]:
    """The proof's rebuild (run by the code of M0): the ``REBUILT`` fields from the database row, the card in M0 and
    the Gamma answers and ``missing`` of the file ``rec``, plus :func:`gamma_why` of the file's Gamma part."""
    recorded = (rec.get("gamma") or {}, rec.get("gamma_events") or {}, list(rec.get("missing") or []))
    out, mids, eids = t3_record(row, release, card_m0, lambda _m, _e: recorded)
    vrows = vrows_used(release, journal_lines(card_m0))  # item 492: the V / sigma_A lines the proof re-runs
    return {"fields": {k: out.get(k) for k in REBUILT}, "gamma_why": gamma_why(rec, mids, eids), "vrows": vrows, "keys": list(T3_KEYS)}


def b_why(rec: Mapping[str, Any]) -> str | None:
    """'rules' when a T-3 record is procedural for set B by its content (registration п. 8, as amended 27.11 by item
    490, R1): the release is «no YoY event» and a YoY event of it was excluded only by the rule phrases (its ``why`` is
    ``rules: no …``; the phrases are checked only on events the slug selection took). The Gamma text behind that is not
    reproducible from git and the database (п. 7, the proof's limit), so set B rebuilds such a release by п. 8 (в),
    :func:`fallback_record`. None otherwise (set B then follows the proof's verdict and the skip reason)."""
    if rec.get("skip") != "no YoY event":
        return None
    events = [e for e in rec.get("events") or [] if isinstance(e, Mapping)]
    return "rules" if any(str(e.get("why") or "").startswith("rules: ") for e in events) else None


def fallback_record(row: Mapping[str, Any] | None, release: str, card_m0: str) -> dict[str, Any]:
    """The ``REBUILT`` fields of a T-3 record by the registered fallbacks of п. 8 (в), with no Gamma answer: the row of
    day T-3, the fee by the row's ``fee_type``, ``min_size`` from ``books_depth_z``, every event taken by its slug (no
    rule text). Set B takes a procedural skip from here when neither the copy in main nor the local file is there, and a
    release of :func:`b_why` always; run by the code of M0 (``--fallback`` of its tree). ``card_m0`` gives only the
    sigma: a «no sigma_A» release of set B is rebuilt with ``--m0 Mσ``, Mσ the first first-parent commit of main
    whose card gives that release a sigma by ``sigma_for`` of the code of that release's M0 (registration п. 8 (1), as
    amended 27.11 by item 492). With no Mσ and a 'V1 · fail' or 'V2 · fail' line in ``card_m0`` (the other V line not
    there), set B takes 0.164 as the merged п. 4 (б) / п. 8 (1) say (third review C of item 492, Т2): ``sigma_for``
    gives such a release «no sigma_A», and so does the T-3 record and its rebuild; only set B takes 0.164."""
    sigma = None
    if release > FIRST_RELEASE:
        journal = journal_lines(card_m0)
        v = journal_vrows(journal)
        if sigma_for(release, journal)[2] == "no sigma_A" and "fail" in (v["V1"], v["V2"]):
            sigma = (SD_KZ, SD_KZ_SOURCE + " (set B: a vintage check failed, the other V line missing; п. 8 (1))", None)
    out, _, _ = t3_record(row, release, card_m0, lambda _m, _e: ({}, {}, []), sigma)
    return {k: out.get(k) for k in REBUILT}


# ------------------------------------------------------------------------------------------- pure: the result (491)


def t3_part(rec: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What the result stage reads of a T-3 record: the fields the proof rebuilds (``REBUILT``) and its Gamma part;
    nothing else of the file (an extra key cannot reach the result)."""
    if not isinstance(rec, Mapping):
        return None
    return {k: rec.get(k) for k in (*REBUILT, "gamma", "gamma_events")}


def yoy_brackets(t3: Mapping[str, Any] | None, t3_row: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """The brackets (main set of the T-3 row) of every headline YoY event of the release as the slug selection took it:
    its ``why`` None, or 'rules: …' (left out by the rule phrases only, set B «rules», п. 8). The result asks Polymarket
    for the resolution of each of them. Without a T-3 record: the same selection (:func:`select_events`) on the job's
    row of day T-3 ``t3_row`` (set B, п. 8 (в), rebuilds such a release from that row and needs its outcomes)."""
    if t3 is None:
        row = t3_row
        events = select_events(main_brackets(row), (row or {}).get("target"))
    else:
        row = t3.get("row")
        events = [e for e in t3.get("events") or [] if isinstance(e, Mapping)]
    yoy = {str(e.get("event_id")) for e in events if e.get("why") is None or str(e.get("why")).startswith("rules: ")}
    return [b for b in main_brackets(row) if str(b.get("event_id")) in yoy]


def result_fields(market: Any, market_id: Any) -> dict[str, Any]:
    """What ``result.json`` keeps of a Gamma ``/markets/{id}`` answer after the release: the resolution
    (:func:`pm_resolution`) and the rule text then, with its sha256 and the phrases found (registration п. 2: a change of
    the rules shows in the record after the resolution). Untrusted text, stored as is."""
    if not isinstance(market, Mapping) or str(market.get("id")) != str(market_id):
        return {"ok": False, "why": "not this market"}
    desc = market.get("description") if isinstance(market.get("description"), str) else None
    return strict({"ok": True, **pm_resolution(market), "description": desc,
                   "description_sha256": hashlib.sha256(desc.encode()).hexdigest() if desc is not None else None,
                   "phrases": phrases_of(desc)})  # fmt: skip


def cpi_part(raw: bytes | None, release: str) -> dict[str, Any]:
    """The published CPI of the release's target month from the FRED CSV of CPIAUCNS (BLS CPI-U, NSA): the 12-month
    change from the index levels, rounded half-up to 0.1 as BLS prints it (:func:`yoy_published`, as A_m of п. 4).
    ``value`` None with a ``why``: the request failed, the answer has no index level at all ('unreadable', e.g. an
    HTML page: not an answer, like a failed request), or the CSV has levels but not this 12-month change yet ('not
    published'; from :func:`result_cutoff` on, the release is invalid, п. 5)."""
    target = target_of_release(release)
    y, m = target_key(target)
    out: dict[str, Any] = {"target": target, "series": CPI_SERIES, "source": f"{FRED_URL}/graph/fredgraph.csv?id={CPI_SERIES}",
                           "csv_sha256": None, "level": None, "level_12m_before": None, "last_month": None, "value": None,
                           "why": "request failed"}  # fmt: skip
    if raw is None:
        return out
    levels = fred_levels(raw.decode("utf-8", "replace"))
    value = yoy_published(levels).get(target)
    out.update(csv_sha256=hashlib.sha256(raw).hexdigest(), level=levels.get(f"{y:04d}-{m:02d}"),
               level_12m_before=levels.get(f"{y - 1:04d}-{m:02d}"), last_month=max(levels) if levels else None,
               value=value, why=None if value is not None else "not published" if levels else "unreadable")  # fmt: skip
    return out


def result_signals(t3: Mapping[str, Any] | None, row: Mapping[str, Any] | None, payout: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every signal (fill) of the T-3 decision with its T-1 quote, only from ``close_quote`` of the job's T-1 row
    (registration п. 5, 8: on a mismatch of T-1.json with the database row the row applies, so the row is read), its
    CLV and paper result (:func:`signal_figures`; the exit fee at the T-1 bid by the bracket's fee fields in
    ``rule_signals``, as the entry fee, п. 6)."""
    out = []
    dec = (t3 or {}).get("decision") or {}
    fees = {str(r.get("token")): r for r in dec.get("rule_signals") or [] if isinstance(r, Mapping)}
    for f in dec.get("fills") or []:
        q = clean_quote(close_quote(row, f.get("token")))
        y = payout.get(str(f.get("market_id")))
        r = fees.get(str(f.get("token"))) or {}
        out_fee = taker_fee(q["bid"], r.get("fee_type"), r.get("fee_schedule"), r.get("fees_enabled")) if q["bid"] is not None else None
        fig = signal_figures(f, q["bid"], q["ask"], y, out_fee)
        why = "not executable" if f.get("executable") is not True else q["why"] or (None if fig["clv"] else "no side")
        out.append({"market_id": f.get("market_id"), "token": f.get("token"), "title": f.get("title"),
                    "executable": f.get("executable"), "shares": f.get("shares"), "p_in": f.get("p_in"),
                    "cost_usd": f.get("cost_usd"), "fee_usd": f.get("fee_usd"), "bid_t1": q["bid"], "ask_t1": q["ask"],
                    "payout": y, "why": why, **fig})  # fmt: skip
    return out


def result_payload(
    release: str,
    at: datetime,
    t3: Mapping[str, Any] | None,
    t3_src: Mapping[str, Any],
    t3_row: Mapping[str, Any] | None,
    t1_row: Mapping[str, Any] | None,
    t1_src: Mapping[str, Any],
    t1_ok: tuple[bool, bool] | str | None,
    cpi: Mapping[str, Any],
    markets: Mapping[str, Any],
    missing: list[Any],
) -> dict[str, Any]:
    """The content of ``result.json`` (registration п. 7, 8) at D = ``at`` from the T-3 record (:func:`t3_part`), the
    job's T-1 row, the published CPI (:func:`cpi_part`) and Polymarket's answers per market of :func:`yoy_brackets`
    (of the record, or of the database row of day T-3 ``t3_row`` when there is no readable record). ``t1_ok``: the
    :func:`t1_check` pair, 'unreadable …' for a T-1.json that does not read, None without one. ``pending`` lists what
    is not final yet (a request failed, the CPI not published, a market not resolved): the stage writes nothing while
    it is not empty, and it is not part of the file. From :func:`result_cutoff` on, an answer that is not final goes
    to ``absent`` instead (a market not resolved: its event gets no Brier, 'no payout', and its signal no paper
    result; the CPI not published: ``invalid``, п. 5); a failed request (or an unreadable FRED answer) is no answer
    and still waits. ``absent`` also lists what will never come (no T-3 record, no decision, no YoY event, no T-1 row):
    written as is, never filled in."""
    cutoff = result_cutoff(release)
    late = at >= cutoff
    brs = yoy_brackets(t3, t3_row)
    payout = {k: g.get("yes") for k, g in markets.items() if isinstance(g, Mapping) and g.get("state") == "resolved"}
    rows = [{"event_id": str(b.get("event_id")), "market_id": str(b.get("market_id")), "title": b.get("title"),
             "yes": payout.get(str(b.get("market_id")))} for b in brs]  # fmt: skip
    t3_gamma = (t3 or {}).get("gamma") or {}
    res = {}
    for b in brs:
        mid = str(b.get("market_id"))
        g = markets.get(mid) or {"ok": False, "why": "not asked"}
        was = (t3_gamma.get(mid) or {}).get("description_sha256") if isinstance(t3_gamma.get(mid), Mapping) else None
        res[mid] = {**g, "event_id": str(b.get("event_id")), "title": b.get("title"), "token": str(b.get("token")),
                    "rules_changed_since_t3": None if was is None or not g.get("ok") else g.get("description_sha256") != was}  # fmt: skip
    decision = (t3 or {}).get("decision")
    signals = result_signals(t3, t1_row, payout) if decision else []
    brier = brier_release(decision.get("rule_signals") or [], payout) if decision else None
    t3_err = t3_src.get("error") if t3 is None else None
    no_t3 = (f"t3: T-3.json unreadable ({t3_err}; markets from the database row of day T-3)" if t3_err
             else "t3: no T-3 record (markets from the database row of day T-3)")  # fmt: skip
    absent = [x for x, bad in ((no_t3, t3 is None),
                               (f"t3: no decision (skip {(t3 or {}).get('skip')})", t3 is not None and not decision),
                               ("no YoY event (no market asked)", not brs),
                               ("t1: no row of day T-1 (no CLV)", t1_row is None and bool(signals))) if bad]  # fmt: skip
    pending: list[str] = []
    invalid = None
    by = f"by {cutoff.isoformat()}"
    if cpi.get("value") is None:
        if late and cpi.get("why") == "not published":
            absent.append(f"cpi: not published {by} (the release is invalid, registration п. 5)")
            invalid = "CPI not published (registration п. 5)"
        else:
            pending.append(f"cpi: {cpi.get('why')}")
    for m, g in res.items():
        why = g.get("why") if not g.get("ok") else g.get("state")
        if g.get("ok") and g.get("state") == "resolved":
            continue
        if late and why != "request failed":
            absent.append(f"polymarket {m}: not resolved {by} ({why}; its event has no Brier, no payout)")
        else:
            pending.append(f"polymarket {m}: {why}")
    t1c = None if t1_ok is None else {"unreadable": t1_ok} if isinstance(t1_ok, str) else {"row_digest_db": t1_ok[0], "quotes": t1_ok[1]}
    return {
        "cutoff": cutoff.isoformat(), "invalid": invalid,
        "t3_file": dict(t3_src), "t3_skip": (t3 or {}).get("skip"), "t3_row_digest": (t3 or {}).get("row_digest"),
        "markets_from": "T-3 record" if t3 is not None else "database row of day T-3" if t3_row is not None else None,
        "t1_file": dict(t1_src), "t1_check": t1c,
        "t1_source": row_payload("T-1", release, t1_row)["source"], "t1_row_digest": row_digest(t1_row) if t1_row else None,
        "cpi": dict(cpi), "resolution": res, "resolution_check": resolution_check(rows, cpi.get("value")),
        "signals": signals, "clv_rel": clv_release(signals), "brier_t3": brier, "missing": list(missing), "absent": absent,
        "pending": pending,
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------------- git, code


def git(repo: Path, *args: str, binary: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=check, text=not binary)


def m0_of(repo: Path, ref: str, release: str) -> str | None:
    """M0 of a release: the last first-parent state of ``ref`` before 21:00 UTC of day T-3 (registration п. 7)."""
    out = git(repo, "log", ref, "--first-parent", f"--until={stage_day('T-3', release)}T21:00:00Z", "-1", "--format=%H").stdout
    return out.strip() or None


def publication_utc(release: str) -> datetime:
    """The moment of the BLS publication of a release in UTC: 08:30 New York time of the release day (13:30 UTC in
    winter, so on 2026-12-10; 12:30 UTC under daylight saving time), from the release day alone."""
    return datetime.combine(date.fromisoformat(release), time(8, 30), ZoneInfo("America/New_York")).astimezone(UTC)


def r_of(repo: Path, ref: str, release: str) -> str | None:
    """R of a release: the last first-parent state of ``ref`` before the publication (:func:`publication_utc`); the
    result stage runs its code, so its CLV / Brier code was fixed before any outcome (registration п. 7, as amended
    27.11 by item 491 after review C, blocker 1)."""
    until = publication_utc(release).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = git(repo, "log", ref, "--first-parent", f"--until={until}", "-1", "--format=%H").stdout
    return out.strip() or None


def has_result_stage(repo: Path, sha: str) -> bool:
    """Whether the scanner at commit ``sha`` has the result stage (a top-level ``RESULT = "result"``), read with
    ``ast`` (nothing of it runs): when R is older than item 491, the stage is refused, never run by another commit."""
    src = git(repo, "show", f"{sha}:scripts/pm_cpi_forward.py").stdout
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "RESULT" for t in node.targets):
            return isinstance(node.value, ast.Constant) and node.value.value == RESULT
    return False


def safe_path(p: Any) -> bool:
    pp = PurePosixPath(str(p))
    return isinstance(p, str) and bool(p) and not pp.is_absolute() and ".." not in pp.parts


def code_paths_at(repo: Path, sha: str) -> tuple[str, ...]:
    """``CODE_PATHS`` of the scanner at commit ``sha``, read from its source with ``ast`` (nothing of it runs)."""
    src = git(repo, "show", f"{sha}:scripts/pm_cpi_forward.py").stdout
    for node in ast.parse(src).body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "CODE_PATHS" for t in targets) and node.value is not None:
            paths = tuple(ast.literal_eval(node.value))
            if not paths or not all(safe_path(p) for p in paths):
                raise ValueError(f"unusable CODE_PATHS at {sha[:12]}")
            return paths
    raise ValueError(f"no CODE_PATHS in scripts/pm_cpi_forward.py at {sha[:12]}")


def extract(repo: Path, sha: str, paths: Iterable[str], tmp: Path) -> Path:
    """Write ``git show <sha>:<path>`` of every path into a fresh ``<tmp>/pm_cpi_code_<sha[:12]>/``."""
    tree = tmp / f"pm_cpi_code_{sha[:12]}"
    if tree.exists():
        shutil.rmtree(tree)
    for p in paths:
        if not safe_path(p):
            raise ValueError(f"unsafe path {p!r}")
        f = tree / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(git(repo, "show", f"{sha}:{p}", binary=True).stdout)
    return tree


def blob_id(data: bytes) -> str:
    """The git blob id of ``data`` (as ``git hash-object``)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def loaded_modules(tree: Path) -> tuple[list[str], list[str]]:
    """(tree-relative paths, absolute paths outside the tree) of every imported ``tradedesk.*`` and scanner module."""
    root = tree.resolve()
    inside, outside = set(), set()
    for name, mod in list(sys.modules.items()):
        if not (name == "tradedesk" or name.startswith("tradedesk.") or name in SCRIPT_MODULES):
            continue
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        p = Path(f).resolve()
        try:
            inside.add(p.relative_to(root).as_posix())
        except ValueError:
            outside.add(str(p))
    return sorted(inside), sorted(outside)


def code_dirty(repo: Path, sha: str, tree: Path, paths: Iterable[str], loaded: tuple[list[str], list[str]]) -> str:
    """'' when every path of the tree has the blob of ``sha`` and every imported module is one of them, in the tree;
    else one line per finding: 'M <path>' (other bytes), 'D <path>' (missing in the tree or in the commit), 'X <path>'
    (imported but not in CODE_PATHS), 'O <path>' (imported from outside the tree), or why git could not tell."""
    paths = list(paths)
    try:
        ls = git(repo, "ls-tree", "-r", sha, "--", *paths).stdout
    except (subprocess.CalledProcessError, OSError) as e:
        return f"git ls-tree failed: {type(e).__name__}"
    blobs = {}
    for line in ls.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            blobs[path] = parts[2]
    lines = []
    for p in paths:
        f = tree / p
        if p not in blobs or not f.is_file():
            lines.append(f"D {p}")
        elif blob_id(f.read_bytes()) != blobs[p]:
            lines.append(f"M {p}")
    inside, outside = loaded
    lines += [f"X {p}" for p in inside if p not in set(paths)]
    lines += [f"O {p}" for p in outside]
    return "\n".join(lines)


BOOT = (
    "import sys; t = sys.argv[1]; sys.path[:0] = [t + '/src', t + '/scripts']; import pm_cpi_forward as m; sys.exit(m.inner(sys.argv[2:]))"
)


# The step-0 script of the tree (the registered commands of п. 10: row export, vcheck, sigma), for the proof (item 492).
VBOOT = (
    "import sys; t = sys.argv[1]; sys.path[:0] = [t + '/src', t + '/scripts']; import verify_pm_cpi_step0 as v; "
    "sys.argv = ['verify_pm_cpi_step0.py', *sys.argv[2:]]; v.main()"
)


def run_tree(
    tree: Path, args: list[str], repo: Path, env: Mapping[str, str] | None = None, boot: str = BOOT, timeout: float | None = None
) -> subprocess.CompletedProcess:
    """Run the scanner of ``tree`` (``boot``: its step-0 script) in a subprocess, that tree first in ``sys.path``
    (isolated mode, no bytecode); ``timeout`` seconds: ``subprocess.TimeoutExpired`` (the child is killed)."""
    return subprocess.run([sys.executable, "-I", "-B", "-c", boot, str(tree), *args], cwd=repo, capture_output=True,
                          text=True, env=dict(env) if env is not None else None, timeout=timeout)  # fmt: skip


# ------------------------------------------------------------------------------------------------------ the stages


def read_row(db: Path, day: str) -> dict[str, Any] | None:
    """The job's row of ``day``, read-only (``mode=ro``)."""
    from verify_pm_cpi_step0 import read_rows

    return read_rows(db, day)[0]


def default_runtime(retries: int) -> Any:
    from tradedesk.config import Settings
    from tradedesk.runtime import Runtime

    return Runtime(Settings(db_path="", cache_scale=0.0, http_retries=retries))


async def fetch_gamma(mids: list[str], eids: list[str], rt_factory: Callable[[], Any]) -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    """(markets, events, missing): Gamma ``/markets/{id}`` of every market id and ``/events/{id}`` of every event id;
    a failure of any kind is recorded, never raised (the registered fallbacks then apply)."""
    from tradedesk.core.envelope import Builder

    b: Any = Builder()
    out: tuple[dict[str, Any], dict[str, Any]] = ({}, {})
    rt = rt_factory()
    try:
        for kind, ids, call, keep, into in (("market", mids, rt.polymarket.market, gamma_fields, out[0]),
                                            ("event", eids, rt.polymarket.event, event_fields, out[1])):  # fmt: skip
            for i in ids:
                try:
                    data = await b.guard(f"{kind}:{i}", "polymarket:gamma", call(i))
                except Exception as e:  # noqa: BLE001 - recorded; the registration has a fallback for every Gamma field
                    b.miss(f"{kind}:{i}", "polymarket:gamma", "backend_error", f"raised {type(e).__name__}")
                    data = None
                into[i] = keep(data, i) if data is not None else {"ok": False, "why": "request failed"}
    finally:
        await rt.aclose()
    return out[0], out[1], [m.model_dump(mode="json") for m in b.missing]


def gamma_answers(rt_factory: Callable[[], Any]) -> Callable[[list[str], list[str]], tuple[Any, Any, list[Any]]]:
    """The live Gamma requests of a write stage (nothing asked, nothing recorded when there are no ids)."""
    return lambda mids, eids: asyncio.run(fetch_gamma(mids, eids, rt_factory)) if mids or eids else ({}, {}, [])


def default_fred(retries: int) -> Any:
    """The FRED client of the result stage (``core.http.Http``, the public graph endpoint, no key)."""
    from verify_pm_cpi_step0 import UA

    from tradedesk.core.http import Http

    return Http("fred", FRED_URL, rate=0.5, timeout=30.0, retries=retries, headers=UA)


async def fetch_result(
    mids: list[str], rt_factory: Callable[[], Any], fred_factory: Callable[[], Any]
) -> tuple[bytes | None, dict[str, Any], list[Any]]:
    """(the FRED CSV of CPIAUCNS or None, {market id: :func:`result_fields`}, missing): every failure recorded, never
    raised; the result stage then writes nothing and may be run again."""
    from tradedesk.core.envelope import Builder

    b: Any = Builder()
    csv: bytes | None = None
    fred = fred_factory()
    try:
        data = await b.guard(
            "cpi", f"fred:{CPI_SERIES}", fred.get_bytes("/graph/fredgraph.csv", params={"id": CPI_SERIES}, tool="fredgraph")
        )
        csv = bytes(data) if data is not None else None
    except Exception as e:  # noqa: BLE001 - recorded; the stage writes nothing without the CPI
        b.miss("cpi", f"fred:{CPI_SERIES}", "backend_error", f"raised {type(e).__name__}")
    finally:
        await fred.aclose()
    markets: dict[str, Any] = {}
    if mids:
        rt = rt_factory()
        try:
            for i in mids:
                try:
                    m = await b.guard(f"market:{i}", "polymarket:gamma", rt.polymarket.market(i))
                except Exception as e:  # noqa: BLE001 - recorded; the stage writes nothing without every resolution
                    b.miss(f"market:{i}", "polymarket:gamma", "backend_error", f"raised {type(e).__name__}")
                    m = None
                # an HTTP 404 (`data_unavailable`) is an answer, the market is not there: after the cutoff it goes to
                # `absent`; any other failure is no answer and the stage waits (registration п. 7, result (в))
                gone = m is None and b.missing and b.missing[-1].field == f"market:{i}" and b.missing[-1].reason == "data_unavailable"
                markets[i] = result_fields(m, i) if m is not None else {"ok": False, "why": "not found" if gone else "request failed"}
        finally:
            await rt.aclose()
    return csv, markets, [x.model_dump(mode="json") for x in b.missing]


def load_stage(stage: str, release: str, root: Path, docs: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """A stage record: the clone's copy (merged into main) when it reads as a JSON object, else the local file (review
    C of item 491, N1 (b): a clone's copy that does not read falls back to the local file). A file that is there but
    is not a JSON object is never taken for a missing file and never raised (R4): when no copy reads, None with the
    ``error`` of the first. Every other copy there is listed under ``also`` (kind, path, sha256, error), so a copy that
    differs from the one read shows in the record."""
    found: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for kind, p in (("docs", docs / release / f"{stage}.json"), ("local", root / release / f"{stage}.json")):
        if p.is_file():
            raw = p.read_bytes()
            rec: Any = None
            try:
                rec = json.loads(raw)
                err = None if isinstance(rec, dict) else "not an object"
            except (ValueError, RecursionError) as e:
                err = type(e).__name__
            found.append(({"kind": kind, "path": str(p), "sha256": hashlib.sha256(raw).hexdigest(), "error": err},
                          rec if err is None else None))  # fmt: skip
    if not found:
        return None, {"kind": None, "path": None, "sha256": None, "error": None, "also": []}
    src, rec = next((x for x in found if x[1] is not None), found[0])
    return rec, {**src, "also": [s for s, _ in found if s is not src]}


def t1_compare(rec1: Mapping[str, Any] | None, src: Mapping[str, Any], row: Mapping[str, Any] | None) -> tuple[bool, bool] | str | None:
    """:func:`t1_check` of the T-1 record against the database row; 'unreadable (…)' when the file does not read or
    its content breaks the check (it is only compared, so the result is written all the same; R4); None without one."""
    if rec1 is None:
        return f"unreadable ({src['error']})" if src.get("error") else None
    try:
        return t1_check(rec1, row)
    except Exception as e:  # noqa: BLE001 - descriptive: CLV comes from the database row in any case (п. 8)
        return f"unreadable ({type(e).__name__})"


def result_record(release: str, at: datetime, *, db: Path, root: Path, docs: Path, rt_factory: Callable[[], Any],
                  fred_factory: Callable[[], Any]) -> dict[str, Any]:  # fmt: skip
    """The result payload of the release at D = ``at`` (:func:`result_payload`) from the stage files, the database
    (read-only) and the live answers of FRED and Gamma."""
    t3_rec, t3_src = load_stage("T-3", release, root, docs)
    t3 = t3_part(t3_rec)
    t3_row = read_row(db, stage_day("T-3", release)) if t3 is None else None
    t1_row = read_row(db, stage_day("T-1", release))
    t1_rec, t1_src = load_stage("T-1", release, root, docs)
    mids = list(dict.fromkeys(str(b.get("market_id")) for b in yoy_brackets(t3, t3_row)))
    csv, markets, missing = asyncio.run(fetch_result(mids, rt_factory, fred_factory))
    t1_ok = t1_compare(t1_rec, t1_src, t1_row)
    cpi = cpi_part(csv, release)
    return result_payload(release, at, t3, t3_src, t3_row, t1_row, t1_src, t1_ok, cpi, markets, missing)


def out_paths(stage: str, release: str, root: Path, docs: Path) -> tuple[Path, Path]:
    return root / release / f"{stage}.json", docs / release / f"{stage}.json"


def load_t3(release: str, root: Path, docs: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The T-3 record for the T-1 CLV: the clone's copy (merged into main) when there is one, else the local file."""
    for kind, p in (("docs", docs / release / "T-3.json"), ("local", root / release / "T-3.json")):
        if p.is_file():
            raw = p.read_bytes()
            return json.loads(raw), {"kind": kind, "path": str(p), "sha256": hashlib.sha256(raw).hexdigest()}
    return None, {"kind": None, "path": None, "sha256": None}


def dump(rec: Mapping[str, Any]) -> bytes:
    return json.dumps(rec, sort_keys=True, indent=1, ensure_ascii=False, allow_nan=False).encode() + b"\n"


def write_stage(
    stage: str,
    release: str,
    at: datetime,
    *,
    db: Path,
    root: Path,
    docs: Path,
    repo: Path,
    code_sha: str,
    m0: str | None,
    tree: Path,
    rt_factory: Callable[[], Any],
    fred_factory: Callable[[], Any] | None = None,
    r: str | None = None,
) -> int:
    """Write the file of a stage once (registration п. 5, 7): 0 = written; 1 = nothing written (outside the window, a
    file already there, the database unreadable; for the result also an input not final yet, item 491, or a run not
    by the code of R: ``r`` empty or not ``code_sha``)."""
    why = stage_why(stage, release, at)
    if stage not in (*WRITE_STAGES, RESULT) or why is not None:
        start, end = stage_window(stage, release) if stage in STAGE_DAYS else (None, None)
        say(f"not written: {stage} {release} at {at.isoformat()} is outside its window ({why or 'stage'}): {start} .. {end}")
        return 1
    local, copy = out_paths(stage, release, root, docs)
    for p in (local, copy):
        if p.exists():
            say(f"not written: {p} exists (a stage file is written once)")
            return 1
    if stage == RESULT:
        if not r or code_sha != r:
            say(f"not written: {RESULT} {release} runs only by the code of R (code_sha {code_sha or '-'}, R {r or '-'}); "
                "start it with `--stage result`, which extracts the tree of R (registration п. 7, result)")  # fmt: skip
            return 1
        res = result_record(release, at, db=db, root=root, docs=docs, rt_factory=rt_factory,
                            fred_factory=fred_factory or (lambda: default_fred(2)))  # fmt: skip
        pending = res.pop("pending")
        for m in res["missing"]:
            say(f"  missing {m.get('field')}: {m.get('reason')} {m.get('detail') or ''}".rstrip())
        for reason in pending:
            say(f"  not final: {reason}")
        if pending:
            say(f"not written: {RESULT} {release} is not final yet ({len(pending)} inputs above); run the stage again later "
                f"(item 493): the file is written once, when the CPI is published and every market resolved, or from the "
                f"cutoff {res['cutoff']} with what did not come under `absent`")  # fmt: skip
            return 1
        out = {"schema": 1, "idea": IDEA, "release": release, "stage": stage, "D": at.isoformat(),
               "window": [str(x) if x else None for x in stage_window(stage, release)], "db": str(db), **res}  # fmt: skip
        return write_result(out, local, copy, repo=repo, code_sha=code_sha, r=r, tree=tree)
    row = read_row(db, stage_day(stage, release))
    rec: dict[str, Any] = {"schema": 1, "idea": IDEA, "release": release, "stage": stage, "D": at.isoformat(),
                           "window": [str(x) if x else None for x in stage_window(stage, release)],
                           **row_payload(stage, release, row)}  # fmt: skip
    if stage == "T-3":
        card = "" if release <= FIRST_RELEASE else git(repo, "show", f"{code_sha}:{CARD}").stdout
        rec.update(t3_record(row, release, card, gamma_answers(rt_factory))[0])
    else:
        events = select_events(main_brackets(row), (row or {}).get("target"))
        _, gamma_events, missing = gamma_answers(rt_factory)([], event_ids(events))
        rec.update(quotes=quotes_of(row), events=with_titles(events, gamma_events), gamma_events=gamma_events,
                   missing=missing, mu=pct((row or {}).get("yoy_headline_pct")))  # fmt: skip
        if stage == "T-1":
            t3, src = load_t3(release, root, docs)
            rec.update(t3_file=src, clv=clv_rows(t3, row) if t3 is not None else None)
    paths = list(CODE_PATHS)
    loaded = loaded_modules(tree)
    rec.update(code_sha=code_sha, M0=m0 or None, CODE_PATHS=paths, modules=loaded[0],
               code_dirty=code_dirty(repo, code_sha, tree, paths, loaded))  # fmt: skip
    data = dump(rec)
    for p in (local, copy):
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "xb") as fh:
            fh.write(data)
    sha = hashlib.sha256(data).hexdigest()
    report(rec, sha)
    say(f"written {local} and {copy}: sha256 {sha}")
    return 0


def write_result(rec: dict[str, Any], local: Path, copy: Path, *, repo: Path, code_sha: str, r: str, tree: Path) -> int:
    """Write ``result.json`` once, the same bytes to the state directory and the clone's copy (``open(..., "xb")``),
    with the code fields of the stages (registration п. 7; ``R`` in place of ``M0``, ``code_sha`` = R); then the
    owner's summary and the journal line."""
    paths = list(CODE_PATHS)
    loaded = loaded_modules(tree)
    rec.update(code_sha=code_sha, R=r, CODE_PATHS=paths, modules=loaded[0],
               code_dirty=code_dirty(repo, code_sha, tree, paths, loaded))  # fmt: skip
    data = dump(strict(rec))
    for p in (local, copy):
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "xb") as fh:
            fh.write(data)
    sha = hashlib.sha256(data).hexdigest()
    report_result(rec, sha)
    say(f"written {local} and {copy}: sha256 {sha}")
    return 0


def _r(x: Any, nd: int = 4) -> Any:
    return round(x, nd) if isinstance(x, float) else x


def report_result(rec: Mapping[str, Any], sha: str) -> None:
    """The owner's lines of a result record and its line for the card's «Форвард-журнал»."""
    cpi, br, cr = rec.get("cpi") or {}, rec.get("brier_t3") or {}, rec.get("clv_rel") or {}
    pm = "; ".join(f"{e['event_id']} PM {','.join(e['pm_yes']) or '-'} / CPI {','.join(e['cpi_yes']) or '-'}"
                   f"{'' if e['agree'] else ' (differ: the payout of Polymarket counts)'}" for e in rec.get("resolution_check") or [])  # fmt: skip
    for s in rec.get("signals") or []:
        say(f"  signal {s['market_id']} {s['title']}: payout {s['payout']}, paper {_r(s['paper_usd'], 2)} USD, "
            f"CLV {_r((s['clv'] or {}).get('usd'), 2)} USD ({s['why'] or 'ok'})")  # fmt: skip
    for a in rec.get("absent") or []:
        say(f"  absent: {a}")
    inv = f"реліз недійсний: {rec['invalid']} · " if rec.get("invalid") else ""
    journal_line(f"{rec['release']} result · D {rec['D']} · R {str(rec.get('R'))[:12]} · {inv}CPI {cpi.get('target')} {cpi.get('value')} % "
                 f"(FRED {cpi.get('series')}) · {pm or 'no YoY event'} · CLV_rel {_r(cr.get('usd'), 2)} USD (n {cr.get('n')}) · "
                 f"Brier T−3 модель {_r(br.get('model'))} ринок {_r(br.get('market'))} Δ {_r(br.get('delta'))} "
                 f"(подій {br.get('n_events')}) · absent {len(rec.get('absent') or [])} · "
                 f"T−3 {(rec.get('t3_file') or {}).get('sha256') or 'немає'} · sha256 {sha}")  # fmt: skip


def report(rec: Mapping[str, Any], sha: str) -> None:
    """The owner's table of a T-3 record and the line for the card's «Форвард-журнал»; a short line for T-7 / T-1."""
    src, stage = rec.get("source") or {}, rec["stage"]
    where = (
        f"рядок {src.get('day')}/{src.get('updated_ts')}/{(rec.get('row_digest') or '')[:12]}"
        if rec.get("row_digest")
        else f"рядок {src.get('day')}: немає"
    )
    if stage != "T-3":
        say(f"{rec['release']} {stage}: {len(rec.get('quotes') or {})} quotes, clv {rec.get('clv')}")
        journal_line(f"{rec['release']} {stage.replace('-', '−')} · D {rec['D']} · джерело {where} · sha256 {sha}")
        return
    nc, dec = rec.get("nowcast") or {}, rec.get("decision") or {}
    say(f"{rec['release']} T-3: mu {nc.get('mu')} sigma {nc.get('sigma')} ({nc.get('sigma_source')}); skip {rec.get('skip') or '-'}")
    say("  event / bracket        bid     ask       q    edge  why            pick")
    for r in dec.get("rule_signals") or []:
        q = f"{r['q']:.4f}" if r.get("q") is not None else "-"
        e = f"{r['edge']:+.4f}" if r.get("edge") is not None else "-"
        say(
            f"  {r['event']} {str(r.get('title'))[:18]:18} {r.get('bid')!s:>7} {r.get('ask')!s:>7} {q:>7} {e:>7}  {str(r.get('why') or ''):14} {'PICK' if r['pick'] else ''}"
        )
    for f in dec.get("fills") or []:
        say(f"  signal {f['market_id']} {f['title']}: shares {f['shares']:.2f}, p_in {f['p_in']}, cost {f['cost_usd']:.2f} USD, "
            f"fee {f['fee_usd']:.4f}, stop {f['stop']}, executable {f['executable']}")  # fmt: skip
    events = rec.get("events") or []
    n_br = sum(1 for r in dec.get("rule_signals") or [] if r.get("q") is not None)
    sig = "; ".join(
        f"{f['market_id']} {f['title']} q {r['q']:.4f} ask {r['ask']} edge {r['edge']:.4f} шейри {f['shares']:.2f} p_in {f['p_in']} "
        f"вартість {f['cost_usd']:.2f} викон. {'так' if f['executable'] else 'ні'}"
        for f in dec.get("fills") or [] for r in dec.get("rule_signals") or [] if r["pick"] and r["token"] == f["token"]
    )  # fmt: skip
    verdict = f"пропуск T−3: {rec['skip']}" if rec.get("skip") else (sig or "без сигналу")
    journal_line(f"{rec['release']} T−3 · D {rec['D']} · джерело {where} · μ {nc.get('mu')} · σ {nc.get('sigma')} "
                 f"({nc.get('sigma_source')}) · події/діапазони з моделлю {sum(1 for e in events if e['taken'])}/{n_br} · "
                 f"{verdict} · sha256 {sha}")  # fmt: skip


JOURNAL_NOTE = "journal line for the card's «Форвард-журнал» (the next line, as is):"


def journal_line(line: str) -> None:
    """Print the note and, on its own line without any prefix, the journal line (control characters of untrusted
    titles replaced by spaces, so the sha256 stays on the line with the key; review C of item 484, finding 4)."""
    say(JOURNAL_NOTE)
    say(one_line(line))


def fetch_head(repo: Path) -> datetime | None:
    """The mtime of the clone's ``FETCH_HEAD`` (UTC; written by every ``git fetch`` without ``--no-write-fetch-head``),
    None when there is none or it is empty: a failed fetch truncates it first, so its mtime is fresh while
    ``origin/main`` is not (review C of item 490, finding 3)."""
    p = Path(git(repo, "rev-parse", "--git-path", "FETCH_HEAD").stdout.strip())
    p = p if p.is_absolute() else repo / p
    return datetime.fromtimestamp(p.stat().st_mtime, UTC) if p.is_file() and p.stat().st_size > 0 else None


def fetch_why(repo: Path, release: str, bound: datetime | None = None) -> str | None:
    """Why ``origin/main`` of the clone may not show M0 (review C of item 484, finding 5): no ``FETCH_HEAD``, or the
    last fetch was before ``bound`` (default 21:00 UTC of day T-3), so a merge before that boundary may be unseen;
    None otherwise."""
    t = fetch_head(repo)
    bound = bound or datetime.fromisoformat(f"{stage_day('T-3', release)}T21:00:00+00:00")
    if t is None:
        return "no FETCH_HEAD"
    return None if t >= bound else f"last fetch {t.isoformat(timespec='seconds')} is before {bound.isoformat()}"


def t3_journal_why(repo: Path, ref: str, release: str, root: Path, docs: Path) -> str | None:
    """Why the T-3 record the result would read does not count (registration п. 7, result; review C of item 491, N1):
    its sha256 (:func:`load_stage`) must be the one on the single line '<release> T−3 …' of «Форвард-журнал» in the
    card on ``ref`` (:func:`journal_sha`), or there is neither a T-3 file nor such a line. A copy not read (the local
    file beside a readable clone's copy) does not count here; the record lists it under ``also``. None when they
    agree."""
    card = git(repo, "show", f"{ref}:{CARD}", check=False)
    if card.returncode != 0:
        return f"no card on {ref}"
    jsha, jwhy = journal_sha(card.stdout, release)
    if jsha is None and jwhy != f"0 journal lines of {release} T-3":
        return f"the journal of {ref}: {jwhy}"
    _, src = load_stage("T-3", release, root, docs)
    if src["sha256"] == jsha:  # the same sha256, or no file and no line
        return None
    have = f"{src['kind']} T-3.json has sha256 {src['sha256']}" if src["sha256"] else "no T-3.json in the clone or the state directory"
    return f"{have}, the journal of {ref} {'has ' + jsha if jsha else 'has no T-3 line'}"


def launch(
    stage: str,
    release: str,
    now: datetime,
    *,
    repo: Path,
    ref: str,
    db: Path,
    root: Path,
    docs: Path,
    tmp: Path,
    http_retries: int = 2,
    env: Mapping[str, str] | None = None,
    default_db: Path = DEFAULT_DB,
) -> int:
    """A write stage from the command line: refuse early, pick the commit (T-3: M0; result: R; else HEAD), extract its
    CODE_PATHS and run that tree's scanner in a subprocess with ``now`` as D. The result reads only ``default_db``
    (the collector's database; tests supply their own) and only a T-3 record that agrees with the journal of main
    (:func:`t3_journal_why`; review C of item 491, N1 (b)): a written file with other inputs would not count, and it is
    written once. T-7 / T-3 / T-1 are refused before 22:00 UTC of their day (``LAUNCH_HOUR_UTC``, item 495), nothing
    written; the window of a valid record (:func:`stage_why`) is not changed by this."""
    why = stage_why(stage, release, now)
    if stage in WRITE_STAGES and why in (None, "early"):  # a known stage and an aware time: stage_why said so
        start = datetime.combine(date.fromisoformat(stage_day(stage, release)), time(LAUNCH_HOUR_UTC), UTC)
        if now < start:
            end = stage_window(stage, release)[1]
            say(f"not written: {stage} {release} at {now.isoformat()} is before {start.isoformat()} (22:00 UTC of day "
                f"{stage}, registration п. 5, sixth part of the amendment of 27.11: a collector tick started before 21:00 "
                f"may still be writing the day's row); nothing written, run again at or after {start.isoformat()}"
                + (f" and no later than {end.isoformat()}" if end is not None else ""))  # fmt: skip
            return 1
    if why is not None:
        say(f"not written: {stage} {release} at {now.isoformat()} is outside its window ({why}): {stage_window(stage, release)}")
        return 1
    for p in out_paths(stage, release, root, docs):
        if p.exists():
            say(f"not written: {p} exists (a stage file is written once)")
            return 1
    if stage == "T-3":
        if ref != DEFAULT_REF:
            say(f"not written: T-3 takes M0 from {DEFAULT_REF} only (registration п. 7), not from {ref}")
            return 1
        stale = fetch_why(repo, release)
        if stale is not None:
            say(f"not written: {DEFAULT_REF} may be stale ({stale}); run `git fetch origin main` first (registration п. 7)")
            return 1
        sha = m0 = m0_of(repo, ref, release)
        if not sha:
            say(f"not written: no M0 on {ref} before {stage_day('T-3', release)} 21:00 UTC")
            return 1
        r = None
    elif stage == RESULT:
        # registration п. 7, result (review C of item 491, blocker 1): the code of R, never HEAD
        pub = publication_utc(release)
        if ref != DEFAULT_REF:
            say(f"not written: the result takes R from {DEFAULT_REF} only (registration п. 7), not from {ref}")
            return 1
        if db != default_db:
            say(f"not written: the result reads the collector's database {default_db} only (registration п. 7, result), not {db}")
            return 1
        stale = fetch_why(repo, release, pub)
        if stale is not None:
            say(f"not written: {DEFAULT_REF} may be stale ({stale}); run `git fetch origin main` first (registration п. 7)")
            return 1
        sha = r = r_of(repo, ref, release)
        if not sha:
            say(f"not written: no R on {ref} before the publication {pub.isoformat()}")
            return 1
        try:
            has = has_result_stage(repo, sha)
        except (subprocess.CalledProcessError, ValueError, SyntaxError) as e:
            say(f"not written: the scanner of R {sha[:12]} cannot be read ({type(e).__name__})")
            return 1
        if not has:
            say(f"not written: the scanner of R {sha[:12]} (main before the publication {pub.isoformat()}) has no result "
                "stage: item 491 reached main after it. Nothing is run, not HEAD either: the code of the result must be "
                "older than the outcome (registration п. 7, result)")  # fmt: skip
            return 1
        t3why = t3_journal_why(repo, ref, release, root, docs)
        if t3why is not None:
            say(f"not written: the T-3 record the result would read is not the journal's ({t3why}); take the copy of "
                f"{DOCS_DIR}/{release}/T-3.json from {ref} and run again (registration п. 7, result)")  # fmt: skip
            return 1
        m0 = None
    else:
        sha, m0, r = git(repo, "rev-parse", "HEAD").stdout.strip(), None, None
    try:
        tree = extract(repo, sha, code_paths_at(repo, sha), tmp)
    except (subprocess.CalledProcessError, ValueError, OSError, SyntaxError) as e:
        say(f"not written: the scanner's code of {sha[:12]} cannot be extracted ({type(e).__name__}: {str(e)[:200]})")
        return 1
    say(f"code of {sha} ({'M0' if m0 else 'R' if r else 'HEAD'}) extracted to {tree}")
    args = ["--stage", stage, "--release", release, "--at", now.isoformat(), "--code-sha", sha, "--m0", m0 or "",
            "--tree", str(tree), "--repo", str(repo), "--db", str(db), "--root", str(root), "--docs", str(docs),
            "--http-retries", str(http_retries)] + (["--r", r] if r else [])  # fmt: skip
    res = run_tree(tree, args, repo, env)
    for line in res.stdout.splitlines():
        say(line)
    if res.returncode != 0 and res.stderr.strip():
        say("stderr: " + " | ".join(res.stderr.strip().splitlines()[-5:]))
    return res.returncode


# ----------------------------------------------------------------------------------------------------------- proof


def t1_check(rec1: Mapping[str, Any], db_row: Mapping[str, Any] | None) -> tuple[bool, bool]:
    """(row_digest of the T-1 record = its preimage = the DB row, recorded close quotes = recomputed from the preimage
    and from the DB row): fourth review B 481, finding 3; on a mismatch the DB row applies (registration п. 8)."""
    pre = rec1.get("digest_preimage")
    if pre is None:
        dig = rec1.get("row_digest") is None and db_row is None
    else:
        dig = row_digest(pre) == rec1.get("row_digest") and db_row is not None and row_digest(db_row) == rec1.get("row_digest")
    rq = rec1.get("quotes") or {}
    quotes = dig and all(
        same_record(q, clean_quote(close_quote(pre, t))) and same_record(q, clean_quote(close_quote(db_row, t))) for t, q in rq.items()
    )
    return dig, bool(quotes)


def args_why(repo: Path, ref: str, release: str) -> list[str]:
    """Why a proof run is not one with the registered arguments (registration п. 7, as amended 27.11 by item 490, R5):
    a ``--ref`` other than ``origin/main``, or the clone's last fetch (mtime of ``FETCH_HEAD``) before 15:00 UTC of the
    release day, when every merge up to the deadline (12:00 UTC of T-1) may be unseen. Such a run is ``proof fail:
    args`` and does not count; the proof is run again after ``git fetch origin main``."""
    out = [f"ref {ref} is not {DEFAULT_REF}"] if ref != DEFAULT_REF else []
    stale = fetch_why(repo, release, stage_window("result", release)[0])
    return out + ([f"{stale}; run `git fetch origin main` first"] if stale else [])


V_DAYS = ("2026-12-03", "2026-12-09", "2026-12-11")  # rows of V1 (newer side) and of V2 (older, newer), п. 4, 10
SIGMA_BEFORE = "2027-01-01"  # --before of the sigma command of п. 10
V_CALL_SECONDS = 600  # one command of п. 10 in the proof; a call still running then is killed: not recomputed
# The ends of a vcheck call that are verdicts of the data (review C of item 492, blocker 2): 'V pass' with exit 0, and
# with exit 1 a 'V fail' reason of vintage_why on two copies read, a copy whose stored bytes are not JSON, or a check
# that raised on the structure of the copies. Any other end (another reason, an exception that a failing environment
# can raise, a crash, a kill, a timeout, no output) is no verdict: the line is «not recomputed». Stored bytes that are
# not UTF-8 are the data too, as bytes that are not JSON (review C of item 492, N1, the mirror case: S is pinned by its
# sha256, so only the bytes of a database row can end so).
V_DATA_FAIL = re.compile(
    r"V fail: (same stamp|no shared points .+|overwritten .+|no step .+|cannot read a copy \((JSONDecodeError|UnicodeDecodeError): .*"
    r"|check raised (KeyError|IndexError|TypeError|ValueError|AttributeError|ZeroDivisionError): .*)"
)
SIGMA_LINE = re.compile(r"nowcast .+ fred CPIAUCNS_(\d{8})\.csv before (\S+): n (\d+) \(.*\), left out \d+, sigma_A (\S+)")
V_KINDS = ("year", "month")
V_LINES = ("V1", "V2", "sigma_A")
# --stage vrows (steps 485, 488): not before 22:00 UTC of the newest row a line reads. The row of a day is final after
# 21:00 UTC (registration п. 5), but a collector tick that started before 21:00 writes its row only at its end, and a row
# not written yet shows in no updated_ts; a run before would read a row still being written, or no row yet («NO ROW» is
# a verdict of the data), and its line would later be «mismatch» (review C of item 492, the check of the whole path).
# The tick has no wall-clock bound (no timeout in the scheduler, httpx timeouts are per phase); one in which every
# request uses up its retries (2 nowcast files x 3 x 60 s, 5 Gamma listings and /books x 3 x 10 s, up to 20 /markets x 3 x
# 10 s) takes about 20 min, so 22:00 leaves three times that (third review C of item 492, Т3; stricter than 21:05)
V_FROM = {"V1": datetime(2026, 12, 3, 22, 0, tzinfo=UTC), "V2": datetime(2026, 12, 11, 22, 0, tzinfo=UTC),
          "sigma_A": datetime(2026, 12, 11, 22, 0, tzinfo=UTC)}  # fmt: skip
FRED_DIR = f"{DOCS_DIR}/fred"  # the FRED CSV of a sigma_A line, saved by step 488 (review C of item 492, N2)


def vrows_repro(
    used: Mapping[str, Any], tree: Path, repo: Path, db: Path, tmp: Path, env: Mapping[str, str] | None,
    csv: tuple[str, bytes] | None = None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:  # fmt: skip
    """The registered commands of п. 10, run by the step-0 script of ``tree`` (the tree of M0; the clone for --stage
    vrows) on the rows of ``db`` in a new directory of this run (exports, outputs, the FRED cache): ``(got, why,
    notes)``. ``got``: the verdicts of the data, V1 / V2 'pass' (both vcheck calls 'V pass') or 'fail' (a call failed
    on the data: no row, «NOT exported», a 'V fail' of :data:`V_DATA_FAIL`), sigma_A the number the sigma command
    printed (n > 0). ``why``: the lines no command decided, each with the reason (the row could not be read, S is not
    the registered bytes, a call crashed, was killed or timed out, FRED gave no level): no verdict, run again (review C
    of item 492, blockers 1, 2). ``notes``: V1 / V2 the end of each kind ('year: …; month: …'), sigma_A {'text', 'n',
    'csv' (path of the CSV it read), 'nowcast_sha256'}. ``csv`` = (YYYYMMDD, bytes): the sigma command reads these
    bytes with ``--vintage`` and asks no network (the proof; review C of item 492, N2); None: today's CSV from FRED."""
    from verify_pm_cpi_step0 import V1_OLDER, v1_older_why

    tmp.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=tmp, prefix="pm_cpi_vrows_"))  # review C 492, finding 3: never shared
    rows, cache = work / "rows", work / "cache"
    say(f"  V / sigma_A lines: the commands of п. 10 run in {work}")

    def call(name: str, *args: str) -> tuple[int | None, str]:
        out = work / f"out_{name}.txt"
        try:
            rc: int | None = run_tree(tree, ["--stage", *args, "--cache-dir", str(cache), "--out", str(out)], repo, env,
                                      boot=VBOOT, timeout=V_CALL_SECONDS).returncode  # fmt: skip
        except subprocess.TimeoutExpired:
            rc = None
        return rc, out.read_text(encoding="utf-8", errors="replace") if out.is_file() else ""  # Т4: not the locale's

    # each file of a pair: True (exported), False (no row, «NOT exported»: data, п. 4), or why it is not known
    files: dict[str, bool | str] = {}
    absent: dict[str, str] = {}  # the line of the row command that made a file False
    days = ([V_DAYS[0]] if "V1" in used else []) + (list(V_DAYS[1:]) if "V2" in used or "sigma_A" in used else [])
    for d in days:
        rc, text = call(f"row_{d}", "row", "--day", d, "--db", str(db), "--export", str(rows))
        lines = text.splitlines()
        for k in V_KINDS:
            p = rows / f"{d}_nowcast_{k}.json"
            ne = next((x.strip() for x in lines if x.startswith(f"  NOT exported {p.name}: ")), None)
            if f"NO ROW for {d} in {db}" in lines or ne:
                files[str(p)] = False
                absent[str(p)] = ne or f"NO ROW for {d}"
            elif any(x.startswith(f"  exported {p} (") for x in lines) and p.is_file():
                files[str(p)] = True
            else:
                files[str(p)] = f"row {d}: nowcast_{k} neither exported nor absent (exit {rc}; {(lines or ['no output'])[-1][:120]})"
    sw = v1_older_why()
    for k in V_KINDS:
        files[str(V1_OLDER / f"nowcast_{k}.json")] = True if sw is None else f"S: {sw}"
    nc = lambda d, k: str(rows / f"{d}_nowcast_{k}.json")  # noqa: E731 - the file name --export writes
    pairs = {"V1": [(str(V1_OLDER / f"nowcast_{k}.json"), nc(V_DAYS[0], k), k == "year") for k in V_KINDS],
             "V2": [(nc(V_DAYS[1], k), nc(V_DAYS[2], k), False) for k in V_KINDS]}  # fmt: skip
    got: dict[str, Any] = {}
    why: dict[str, str] = {}
    notes: dict[str, Any] = {}
    for v in ("V1", "V2"):
        if v not in used:
            continue
        ends: list[str] = []
        kinds: list[str] = []
        for i, (old, new, step) in enumerate(pairs[v]):
            st = [files[old], files[new]]
            if any(s is False for s in st):
                ends.append("fail")  # п. 4: no row or «NOT exported» fails the check, no other file is taken
                kinds.append(f"{V_KINDS[i]}: " + "; ".join(absent[x] for x in (old, new) if files[x] is False))
            elif any(isinstance(s, str) for s in st):
                ends.append(next(s for s in st if isinstance(s, str)))
            else:
                rc, text = call(f"{v}_{i}", "vcheck", "--capture", old, "--against", new, *(["--step-target", "2026-11"] if step else []))
                last = (text.splitlines() or [""])[-1].rstrip()
                ok, bad = last == "V pass" and rc == 0, rc == 1 and V_DATA_FAIL.fullmatch(last) is not None
                ends.append("pass" if ok else "fail" if bad else f"vcheck {i}: {last[:120] or 'no output'} (exit {rc})")
                kinds.append(f"{V_KINDS[i]}: {last[:200]}")
        if "fail" in ends:
            got[v] = "fail"
        elif all(e == "pass" for e in ends):
            got[v] = "pass"
        else:
            why[v] = "; ".join(e for e in ends if e not in ("pass", "fail"))
        notes[v] = "; ".join(kinds)
    if "sigma_A" in used:
        src = files[nc(V_DAYS[2], "year")]
        if src is True:
            fred = cache / "cleveland" / "fred"
            vintage: list[str] = []
            if csv is not None:  # the CSV the line names, never today's FRED (review C of item 492, N2)
                fred.mkdir(parents=True, exist_ok=True)
                (fred / f"CPIAUCNS_{csv[0]}.csv").write_bytes(csv[1])
                vintage = ["--vintage", csv[0]]
            rc, text = call("sigma", "sigma", "--against", nc(V_DAYS[2], "year"), "--before", SIGMA_BEFORE, *vintage)
            last = (text.splitlines() or [""])[-1].rstrip()
            m = SIGMA_LINE.fullmatch(last)
            if (rc == 0 and m and m[2] == SIGMA_BEFORE and int(m[3]) > 0 and re.fullmatch(r"\d+\.\d+", m[4])
                    and (csv is None or m[1] == csv[0])):  # fmt: skip
                got["sigma_A"] = float(m[4])
                notes["sigma_A"] = {"text": m[4], "n": int(m[3]), "csv": str(fred / f"CPIAUCNS_{m[1]}.csv"),
                                    "nowcast_sha256": hashlib.sha256(Path(nc(V_DAYS[2], "year")).read_bytes()).hexdigest()}  # fmt: skip
            else:  # n 0 / 'sigma_A None' (FRED gave no level, e.g. an HTML page), a failed request, a crash: no verdict
                why["sigma_A"] = f"sigma: {last[-120:] or 'no output'} (exit {rc})"
        else:
            why["sigma_A"] = f"sigma: no nowcast_year of row {V_DAYS[2]}" + (f" ({src})" if isinstance(src, str) else "")
    return got, why, notes


def vrows_check(
    rebuilt: Mapping[str, Any], tree: Path, repo: Path, db: Path, tmp: Path, env: Mapping[str, str] | None, ref: str = DEFAULT_REF
) -> list[str]:
    """Registration п. 4, as amended 27.11 by item 492 (review C of item 484, R2): every V1 / V2 / sigma_A line of the
    journal in M0 the sigma of the release rests on (``vrows`` of the rebuild answer: :func:`vrows_used` by the code of
    M0) must be what the commands of п. 10 give on the same rows (V: the verdict; sigma_A: the number, and the line has
    at most 4 decimals, as the command prints it), else «mismatch». A line no command decided (:func:`vrows_repro`,
    ``why``) gives no verdict: ``not recomputed (...)``, never a pass; a mismatch of another line still stands. No
    ``vrows`` in the answer (M0 before item 492): the check does not apply (п. 9; review C of item 490, blocker 2).
    sigma_A (review C of item 492, N2): the sigma command reads the CSV the line names, ``FRED_DIR/CPIAUCNS_<day>.csv``
    of ``ref``, with ``--vintage`` and no network. A line naming no CSV sha256, or a CSV of ``ref`` with another sha256,
    is «mismatch» (nothing a later run can change); no such file in ``ref`` (or git failing) is «not recomputed»."""
    if "vrows" not in rebuilt:
        say("  V / sigma_A lines: not applicable, M0 before item 492")
        return []
    used = rebuilt["vrows"]
    real = lambda x: isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)  # noqa: E731
    named = lambda x: x is None or (isinstance(x, list) and len(x) == 2 and all(isinstance(s, str) for s in x)  # noqa: E731
                                    and bool(re.fullmatch(r"\d{8}", x[0])) and bool(re.fullmatch(r"[0-9a-f]{64}", x[1])))  # fmt: skip
    if (
        not isinstance(used, Mapping)
        or ("sigma_A" in used) != ("sigma_A_csv" in used)
        or not all(
            k in ("V1", "V2") and x in ("pass", "fail") or k == "sigma_A" and real(x) or k == "sigma_A_csv" and named(x)
            for k, x in used.items()
        )
    ):
        say("  V / sigma_A lines: M0 gives no line map")
        return ["mismatch (V / sigma_A lines: M0 gives no line map)"]
    if not used:
        say("  V / sigma_A lines: none used")
        return []
    lines = {k: used[k] for k in V_LINES if k in used}
    pre: dict[str, str] = {}  # sigma_A lines its CSV decides: mismatch, no command is run
    pre_why: dict[str, str] = {}
    csv = None
    if "sigma_A" in lines:
        c = used["sigma_A_csv"]
        path = f"{FRED_DIR}/CPIAUCNS_{c[0]}.csv" if c else ""
        r = git(repo, "show", f"{ref}:{path}", binary=True, check=False) if c else None
        if c is None:
            pre["sigma_A"] = "the line names no CSV sha256"
        elif r is None or r.returncode != 0:
            pre_why["sigma_A"] = f"no {path} in {ref}"
        elif hashlib.sha256(r.stdout).hexdigest() != c[1]:
            pre["sigma_A"] = f"{path} of {ref} has sha256 {hashlib.sha256(r.stdout).hexdigest()[:12]}…, not the line's"
        else:
            csv = (c[0], r.stdout)
    run = {k: x for k, x in lines.items() if k not in pre and k not in pre_why}
    got, why, _ = vrows_repro(run, tree, repo, db, tmp, env, csv) if run else ({}, {}, {})
    why = {**why, **pre_why}
    say("  V / sigma_A lines of M0 vs the commands of п. 10: "
        + " · ".join(f"{k} {lines[k]} → " + (str(got[k]) if k in got else f"mismatch: {pre[k]}" if k in pre else f"not recomputed: {why[k]}")
                     for k in lines))  # fmt: skip
    bad = [k for k in ("V1", "V2") if k in got and got[k] != used[k]]
    sig = used.get("sigma_A")
    # review C 492, finding 8: sigma_for takes the line's number as it is, so it must be the command's 4-place number
    if sig is not None and ("sigma_A" in pre or round(sig, 4) != sig or ("sigma_A" in got and got["sigma_A"] != sig)):
        bad.append("sigma_A")
    if bad:
        return [f"mismatch (V / sigma_A lines: {', '.join(bad)})"]
    if why:
        return [f"not recomputed (V / sigma_A lines: {'; '.join(f'{k}: {w}' for k, w in why.items())[:300]}; run the proof again)"]
    return []


VROWS_WRITE = 0  # --stage vrows: a verdict of the data; write the printed journal line (and commit the saved CSV)
VROWS_NOTHING = 3  # any other end: write nothing; the last line says whether and when to run again
VROWS_GIT_ADD = "stage the saved CSV with the line (the next line, as is, from the root of the clone):"  # sigma_A only


def vrows_stage(line: str, now: datetime, *, repo: Path, tree: Path, db: Path, tmp: Path, env: Mapping[str, str] | None = None) -> int:
    """``--stage vrows --line V1 | V2 | sigma_A`` (steps 485, 488; review C of item 492, N1, N2): the commands of п. 10
    by :func:`vrows_repro`, the classifier of the proof, run by the step-0 script of ``tree`` on the rows of ``db``
    (read-only). :data:`VROWS_WRITE` only for a verdict of the data: the journal line is printed after
    :data:`JOURNAL_NOTE`, and for sigma_A the FRED CSV it read is saved as ``FRED_DIR/CPIAUCNS_<day>.csv`` of ``repo``
    (created with 'x'; the same bytes already there are kept) and named in the line with its sha256; its ``git add``
    command is printed after :data:`VROWS_GIT_ADD` (third review C of item 492, Т1). Any other end —
    before :data:`V_FROM`, a first line of that kind already in the card of ``repo``, sigma_A while the card's first V1
    / V2 lines are not both 'pass', a line no command decided, any exception — is :data:`VROWS_NOTHING` with the last
    line '<line>: write nothing …'. Writes no forward record and no other file."""
    try:
        return _vrows_stage(line, now, repo=repo, tree=tree, db=db, tmp=tmp, env=env)
    except Exception as e:  # noqa: BLE001 - never a line on an exception
        say(f"{line}: write nothing: not recomputed, run again (raised {type(e).__name__}: {str(e)[:200]})")
        return VROWS_NOTHING


def _vrows_stage(line: str, now: datetime, *, repo: Path, tree: Path, db: Path, tmp: Path, env: Mapping[str, str] | None) -> int:
    say(f"{line} · commands of п. 10 by {tree} · db {db} · at {now.isoformat(timespec='seconds')}")
    if now < V_FROM[line]:
        say(
            f"{line}: write nothing: not before {V_FROM[line]:%Y-%m-%d %H:%M} UTC (the rows it reads are final after 21:00 UTC, once a collector tick begun before has ended); run again then"
        )
        return VROWS_NOTHING
    card = journal_vrows(journal_lines((repo / CARD).read_text(encoding="utf-8")))
    if card[line] is not None:
        say(f"{line}: write nothing: the card already has a {line} line ({card[line]}); the first line counts")
        return VROWS_NOTHING
    if line == "sigma_A" and not card["V1"] == card["V2"] == "pass":
        later = "; run again once both lines are in the card" if None in (card["V1"], card["V2"]) else "; σ is 0.164 (п. 4 (б))"
        say(f"{line}: write nothing: the card's first V1 / V2 lines are {card['V1']} / {card['V2']}, sigma_A is written only "
            f"when both are pass{later}")  # fmt: skip
        return VROWS_NOTHING
    got, why, notes = vrows_repro({line: None}, tree, repo, db, tmp, env)
    if line not in got:
        say(f"{line}: write nothing: not recomputed, run again ({why.get(line, 'no verdict')[:300]})")
        return VROWS_NOTHING
    if line == "sigma_A":
        n = notes["sigma_A"]
        src = Path(n["csv"])
        data = src.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        dest = repo / FRED_DIR / src.name
        if dest.exists() and dest.read_bytes() != data:
            say(f"{line}: write nothing: not recomputed, run again the next UTC day ({dest} has other bytes: FRED changed today)")
            return VROWS_NOTHING
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("xb") as fh:
                fh.write(data)
        say(f"sigma_A: verdict of the data {n['text']} (n {n['n']}); FRED CSV saved as {dest} (commit it with the line)")
        # third review C of item 492, Т1: the CSV must be in the index, else the guard test fails the delivery's make test
        say(VROWS_GIT_ADD)
        say(f"git add {FRED_DIR}/{src.name}")
        text = f"sigma_A · {n['text']} · n {n['n']} · sha256 nowcast {n['nowcast_sha256']} · CSV {src.name} sha256 {sha}"
    else:
        say(f"{line}: verdict of the data {got[line]}")
        text = f"{line} · pass · {notes[line]}" if got[line] == "pass" else f"{line} · fail: {notes[line]}"
    journal_line(text)
    return VROWS_WRITE


def proof(release: str, *, repo: Path, ref: str, db: Path, root: Path, tmp: Path, env: Mapping[str, str] | None = None) -> bool:
    """``--stage proof`` (registration п. 7, item 487): every check printed, the last line ``proof pass`` or ``proof
    fail: <reasons>``; any exception is ``proof fail: raised ...``."""
    try:
        fails = _proof(release, repo, ref, db, root, tmp, env)
    except Exception as e:  # noqa: BLE001 - the verdict line is always printed
        fails = [f"raised {type(e).__name__}: {str(e)[:200]}"]
    say("proof pass" if not fails else "proof fail: " + "; ".join(fails))
    return not fails


def _proof(release: str, repo: Path, ref: str, db: Path, root: Path, tmp: Path, env: Mapping[str, str] | None) -> list[str]:
    path = f"{DOCS_DIR}/{release}/T-3.json"
    deadline = stage_window("T-3", release)[1]
    assert deadline is not None
    head = git(repo, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False).stdout.strip()
    fh = fetch_head(repo)
    say(f"{release} proof args · repo {repo} · ref {ref} = {head or 'unknown'} · db {db} · root {root} · "
        f"FETCH_HEAD {fh.isoformat(timespec='seconds') if fh else 'none'}")  # fmt: skip
    aw = args_why(repo, ref, release)
    say(f"  args {'; '.join(aw) if aw else 'registered'}")
    fails = [f"args ({'; '.join(aw)})"] if aw else []
    fmt = "--format=C %H %cI %ce %cn"
    log = git(repo, "log", ref, "--first-parent", "--name-status", fmt, "--", path).stdout
    why, merge = merge_proof_why(log, deadline)
    when = next((ln.split(" ")[2] for ln in log.splitlines() if merge and ln.startswith(f"C {merge} ")), None)
    say(f"{release} proof · merge {merge or '-'} {when or ''} · merge_proof_why {why}")
    fails += [f"not merged ({why})"] if why else []
    if merge is None:
        return fails
    raw = git(repo, "show", f"{merge}:{path}", binary=True).stdout
    sha = hashlib.sha256(raw).hexdigest()
    jsha, jwhy = journal_sha(git(repo, "show", f"{merge}:{CARD}").stdout, release)
    say(f"  sha256 from git {sha} · journal {jsha or jwhy}")
    if jsha != sha:
        fails.append(f"not merged (sha256 ≠ journal: {jwhy or 'other sha256'})")
    n = sum(1 for ln in git(repo, "log", ref, "--name-status", fmt, "--", path).stdout.splitlines() if ln.startswith("C "))
    say(f"  commits touching the file, all parents: {n}")
    if n != 1:
        fails.append(f"not merged (edited on the way to main: {n} commits)")
    rec = json.loads(raw)
    d = datetime.fromisoformat(rec["D"])
    w = stage_why("T-3", release, d)
    if w is None and when is not None and d > datetime.fromisoformat(when):
        w = "D after the merge"
    say(f"  D {rec['D']} · window {w or 'ok'}")
    if w:
        fails.append(f"window ({w})")
    # Code (registration п. 7, as amended after review C of item 484, blocker 1 (b)): the record was made by M0 itself,
    # so sigma and the selection are those of the card and code in M0; CODE_PATHS is M0's, never the file's.
    m0 = m0_of(repo, ref, release)
    paths = list(code_paths_at(repo, m0)) if m0 else []
    dirty = rec.get("code_dirty")
    code = [] if m0 else ["no M0"]
    if m0 and rec.get("code_sha") != m0:
        code.append("code_sha ≠ M0")
    if m0 and rec.get("M0") != m0:
        code.append("M0 field ≠ M0")
    if m0 and list(rec.get("CODE_PATHS") or []) != paths:
        code.append("CODE_PATHS ≠ those of M0")
    if dirty != "":
        code.append("code_dirty not empty")
    say(f"  code · M0 {m0} · code_sha {rec.get('code_sha')} · CODE_PATHS of M0 {len(paths)} · "
        f"code_dirty {'empty' if dirty == '' else repr(dirty)[:200]}")  # fmt: skip
    if code:
        fails.append(f"code ({', '.join(code)})")
    pre = rec.get("digest_preimage")
    pre_ok = (
        (row_digest(pre) == rec.get("row_digest") and pre.get("day") == stage_day("T-3", release)) if pre else rec.get("row_digest") is None
    )
    db_row = read_row(db, stage_day("T-3", release))
    db_ok = (db_row is not None and row_digest(db_row) == rec.get("row_digest")) if rec.get("row_digest") else db_row is None
    say(f"  row_digest = preimage {'так' if pre_ok else 'ні'} · row_digest = БД {'так' if db_ok else 'ні'}")
    if not pre_ok:
        fails.append("mismatch (row_digest ≠ its preimage)")
    if not db_ok:
        fails.append("mismatch (row_digest ≠ DB row)" if db_row is not None else "mismatch (no DB row)")
    rc_ok, bad, gw, kw, k_na = False, ["not rebuilt (no M0)"], [], "not checked (no rebuild)", False
    rebuilt: dict[str, Any] | None = None
    if m0:
        tree = extract(repo, m0, paths, tmp)
        tmp.mkdir(parents=True, exist_ok=True)
        f = tmp / f"pm_cpi_proof_{release}_{sha[:12]}.json"
        f.write_bytes(raw)
        if rec.get("decision_input") is None:
            rc_ok = rec.get("decision") is None
        else:
            res = run_tree(tree, ["--recompute", str(f)], repo, env)
            rc_ok = res.returncode == 0 and same_record(json.loads(res.stdout), rec.get("decision"))
            if res.returncode != 0:
                say("  recompute stderr: " + " | ".join(res.stderr.strip().splitlines()[-3:]))
        # registration п. 7 (2) as amended: the record rebuilt by M0 from the DB row, the file's Gamma part, the card
        res = run_tree(tree, ["--rebuild", str(f), "--release", release, "--db", str(db), "--repo", str(repo), "--m0", m0], repo, env)
        if res.returncode == 0:
            got = rebuilt = json.loads(res.stdout)
            bad = [k for k, v in got["fields"].items() if not same_record(v, rec.get(k))]
            gw = list(got["gamma_why"])
            # item 490, R3: the key set of the file is the one the scanner of M0 writes. An M0 before item 490 gives no
            # ``keys``, and the check does not apply to its release (registration п. 9: a later change acts from the
            # next release; review C of item 490, blocker 2); only the code of M0 shapes this answer, not the file.
            k_na = "keys" not in got
            kw = None if k_na else keys_why(rec, got["keys"]) if isinstance(got["keys"], list) else "M0 gives no key list"
        else:
            bad = ["rebuild raised"]
            say("  rebuild stderr: " + " | ".join(res.stderr.strip().splitlines()[-3:]))
    say(f"  перерахунок = записаному {'так' if rc_ok else 'ні'} · перебудова з рядка БД = записаному "
        f"{'так' if not bad else 'ні: ' + ', '.join(bad)} · Gamma {'ok' if not gw else '; '.join(gw)[:300]} · "
        f"ключі = M0 {'не застосовно, M0 до пункту 490' if k_na else 'так' if kw is None else 'ні: ' + kw[:200]}")  # fmt: skip
    # review C of item 484 / 490, finding 4: descriptive, the verdict does not change (the evaluation report rebuilds a
    # «rules» release for set B by п. 8 (в) with --fallback of the tree of M0). An M0 before item 490 (no key list in
    # its rebuild) does not know «rules» (п. 9), so the line says so instead of the reading of this code (review C 491, R6)
    try:
        sb = ("not applicable, M0 before item 490 (descriptive)" if k_na
              else "rules (rebuilt by п. 8 (в) for set B; descriptive)" if b_why(rec) == "rules" else "not rules (descriptive)")  # fmt: skip
    except Exception as e:  # noqa: BLE001 - a descriptive line never changes the verdict
        sb = f"unknown ({type(e).__name__}; descriptive)"
    say(f"  set B: {sb}")
    try:  # item 492; review C 492, finding 4: an exception here leaves the other reasons of the proof as they are
        vf = vrows_check(rebuilt, tree, repo, db, tmp, env, ref) if rebuilt is not None else []
    except Exception as e:  # noqa: BLE001 - no verdict on the lines, never a pass
        say(f"  V / sigma_A lines: raised {type(e).__name__}")
        vf = [f"not recomputed (V / sigma_A lines: raised {type(e).__name__}: {str(e)[:200]}; run the proof again)"]
    if kw is not None:
        fails.append("mismatch (keys)")
    if not rc_ok:
        fails.append("mismatch (recompute)")
    if bad:
        fails.append(f"mismatch (rebuild: {', '.join(bad)})")
    if gw:
        fails.append(f"mismatch (gamma: {'; '.join(gw)[:200]})")
    fails += vf
    t1_path = f"{DOCS_DIR}/{release}/T-1.json"
    t1 = git(repo, "show", f"{ref}:{t1_path}", binary=True, check=False)
    raw1 = (
        t1.stdout if t1.returncode == 0 else (root / release / "T-1.json").read_bytes() if (root / release / "T-1.json").is_file() else None
    )
    if raw1 is None:
        say("  T-1: no file (not checked)")
    else:
        dig, quotes = t1_check(json.loads(raw1), read_row(db, stage_day("T-1", release)))
        say(
            f"  T-1 ({'main' if t1.returncode == 0 else 'local'}): row_digest = БД {'так' if dig else 'ні'} · close quotes = recomputed "
            f"{'так' if quotes else 'ні'}" + ("" if dig and quotes else " · on a mismatch the DB row applies (registration п. 8)")
        )
    return fails


# ------------------------------------------------------------------------------------------------------------ main


def inner(argv: list[str]) -> int:
    """Entry point of the extracted tree (:func:`run_tree`): a write stage, or the recomputation / rebuild of the proof,
    or the п. 8 (в) record of set B (:func:`fallback_record`) (JSON on stdout)."""
    ap = argparse.ArgumentParser(prog="pm_cpi_forward (tree)")
    ap.add_argument("--recompute", type=Path)
    ap.add_argument("--rebuild", type=Path)
    ap.add_argument("--fallback", action="store_true")
    ap.add_argument("--stage")
    ap.add_argument("--release")
    ap.add_argument("--at")
    ap.add_argument("--code-sha")
    ap.add_argument("--m0", default="")
    ap.add_argument("--r", default="")
    ap.add_argument("--tree", type=Path)
    ap.add_argument("--repo", type=Path)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--root", type=Path)
    ap.add_argument("--docs", type=Path)
    ap.add_argument("--http-retries", type=int, default=2)
    a = ap.parse_args(argv)
    if a.recompute is not None:
        rec = json.loads(a.recompute.read_bytes())
        print(json.dumps(decide(rec["decision_input"])))
        return 0
    if a.rebuild is not None:
        rec = json.loads(a.rebuild.read_bytes())
        card = git(a.repo, "show", f"{a.m0}:{CARD}").stdout
        print(json.dumps(rebuild(rec, a.release, read_row(a.db, stage_day("T-3", a.release)), card)))
        return 0
    if a.fallback:  # registration п. 8 (в) by the code of this tree (set B; item 490, R1)
        card = git(a.repo, "show", f"{a.m0}:{CARD}").stdout
        print(json.dumps(fallback_record(read_row(a.db, stage_day("T-3", a.release)), a.release, card)))
        return 0
    try:
        return write_stage(a.stage, a.release, datetime.fromisoformat(a.at), db=a.db, root=a.root, docs=a.docs, repo=a.repo,
                           code_sha=a.code_sha, m0=a.m0 or None, tree=a.tree,
                           rt_factory=lambda: default_runtime(a.http_retries),
                           fred_factory=lambda: default_fred(a.http_retries), r=a.r or None)  # fmt: skip
    except Exception as e:  # noqa: BLE001 - nothing written; the stage may be run again inside its window
        say(f"not written: raised {type(e).__name__}: {str(e)[:300]}")
        return 1


def main() -> None:
    global _OUT
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", required=True, choices=[*WRITE_STAGES, RESULT, "proof", "vrows"])
    ap.add_argument("--release", help="ISO day of the BLS release, e.g. 2026-12-10 (every stage but vrows)")
    ap.add_argument("--line", choices=V_LINES, help="vrows: the journal line of step 485 (V1) or 488 (V2, then sigma_A)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="collector database (opened read-only)")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="stage files: <root>/<release>/<stage>.json")
    ap.add_argument("--docs", type=Path, default=REPO / DOCS_DIR, help="the clone's copies: <docs>/<release>/<stage>.json")
    ap.add_argument("--repo", type=Path, default=REPO)
    ap.add_argument("--ref", default=DEFAULT_REF, help="main as git knows it (M0 and the proof)")
    ap.add_argument("--tmp", type=Path, default=DEFAULT_TMP)
    ap.add_argument("--out", type=Path, help="also append the output to this file")
    a = ap.parse_args()
    if a.stage == "vrows":
        if a.line is None or a.release is not None:
            ap.error("--stage vrows takes --line and no --release")
    elif a.release is None:
        ap.error("--release is required")
    else:
        date.fromisoformat(a.release)
    fh = None
    if a.out is not None:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        fh = _OUT = a.out.open("a", encoding="utf-8")
    try:
        if a.stage == "vrows":  # steps 485, 488 (item 492, review C N1): the exit code says whether to write the line
            say(f"# pm_cpi_forward --stage vrows --line {a.line} at {datetime.now(UTC).isoformat(timespec='seconds')}")
            raise SystemExit(vrows_stage(a.line, datetime.now(UTC), repo=a.repo, tree=a.repo, db=a.db, tmp=a.tmp))
        say(f"# pm_cpi_forward --stage {a.stage} --release {a.release} at {datetime.now(UTC).isoformat(timespec='seconds')}")
        if a.stage == "proof":
            ok = proof(a.release, repo=a.repo, ref=a.ref, db=a.db, root=a.root, tmp=a.tmp)
        else:
            ok = launch(a.stage, a.release, datetime.now(UTC), repo=a.repo, ref=a.ref, db=a.db, root=a.root, docs=a.docs, tmp=a.tmp) == 0
    finally:
        if fh is not None:
            fh.close()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
