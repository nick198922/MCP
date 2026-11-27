"""Forward scanner of IDEA-20261028-pm-cpi-brackets-vs-nowcast-forward (step 2, item 484 of the standing card): read-only,
nothing here places, changes or cancels an order. The rule is the card's «Попередня реєстрація» (п. 2-8); where this
code and that text differ, the text holds and the code is fixed.

Stages (C = ~/.cache/tradedesk-agents/pm_cpi; every stage file is written once, only inside its window):
    uv run python scripts/pm_cpi_forward.py --stage T-7 --release 2026-12-10 --out C/out_fwd_T-7.txt   # record (item 489)
    uv run python scripts/pm_cpi_forward.py --stage T-3 --release 2026-12-10 --out C/out_fwd_T-3.txt   # decision (item 486)
    uv run python scripts/pm_cpi_forward.py --stage T-1 --release 2026-12-10 --out C/out_fwd_T-1.txt   # close, CLV (item 487)
    uv run python scripts/pm_cpi_forward.py --stage proof --release 2026-12-10 --out C/out_fwd_proof.txt  # proof (item 487)

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
clone's last fetch, the mtime of ``FETCH_HEAD``, is before 21:00 UTC of day T-3), the other stages from HEAD. Every path of that commit's ``CODE_PATHS`` is written by ``git show <sha>:<path>`` into
``~/.cache/tradedesk-agents/tmp/pm_cpi_code_<sha[:12]>/`` and the stage runs in a subprocess with that tree first in
``sys.path``. ``code_dirty`` (empty = clean) lists tree files whose blob differs from the commit's and every imported
``tradedesk.*`` / scanner module that is not one of ``CODE_PATHS`` or not in the tree.

Files: ``~/.local/state/tradedesk-agents/pm_cpi/forward/<release>/<stage>.json`` and the same bytes in the clone's
``docs/forward/pm-cpi/<release>/<stage>.json`` (both created with ``open(..., "x")``; an existing file is never
overwritten). ``--stage proof`` (item 487) checks the merged copy of ``T-3.json`` with git, the database (read-only)
and, by the code of M0 (``code_sha`` must be M0), a recomputation of the decision from the file's input and a rebuild
of the whole record from the database row, the file's Gamma answers and the card in M0; its first line names its
arguments, its last line is ``proof pass`` or ``proof fail: <reason>``.
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
import zlib
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from tradedesk.analytics.option_xvenue import num
from tradedesk.analytics.pm_cpi import (
    ROW_DIGEST_COLUMNS,
    SD_KZ,
    STAGE_DAYS,
    book_why,
    books_of,
    close_quote,
    clv,
    digest_preimage,
    fill,
    merge_proof_why,
    model_ok,
    nowcast_ts,
    pct,
    row_digest,
    row_why,
    rule_signals,
    same_record,
    same_target,
    stage_why,
    stage_window,
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
    in M0, none -> skip 'no sigma_A'; a verdict line missing -> skip 'no sigma_A' too (no V verdict in M0 yet). The
    first line of each kind counts; a line may start with 'YYYY-MM-DD ' (format of the card's «Форвард-журнал»)."""
    if release <= FIRST_RELEASE:
        return SD_KZ, SD_KZ_SOURCE, None
    v: dict[str, str | None] = {"V1": None, "V2": None}
    sig = None
    for line in journal:
        s = _clean(line)
        mv = re.match(rf"^{_DATE}(V1|V2) · (pass|fail)", s)
        if mv and v[mv[1]] is None:
            v[mv[1]] = mv[2]
        ms = re.match(rf"^{_DATE}sigma_A · (\d+(?:\.\d+)?)", s)
        if ms and sig is None:
            sig = float(ms[1])
    if "fail" in v.values():
        return SD_KZ, SD_KZ_SOURCE + " (a vintage check failed)", None
    if v["V1"] == v["V2"] == "pass" and sig is not None and sig > 0:
        return sig, "sigma_A (first line of the journal in M0)", None
    return None, None, "no sigma_A"


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
) -> tuple[dict[str, Any], list[str], list[str]]:
    """(the ``REBUILT`` fields of a T-3 record, market ids asked, event ids asked) from the row of day T-3, the card's
    text in M0 (its «Форвард-журнал» gives sigma after the first release) and ``answers(mids, eids)`` -> (gamma,
    gamma_events, missing): the live requests at D, the file's recorded answers in the proof."""
    rwhy, events, mids = t3_prepare(row, release)
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
    return {"fields": {k: out.get(k) for k in REBUILT}, "gamma_why": gamma_why(rec, mids, eids), "keys": list(T3_KEYS)}


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
    release of :func:`b_why` always; run by the code of M0 (``--fallback`` of its tree)."""
    out, _, _ = t3_record(row, release, card_m0, lambda _m, _e: ({}, {}, []))
    return {k: out.get(k) for k in REBUILT}


# ------------------------------------------------------------------------------------------------------- git, code


def git(repo: Path, *args: str, binary: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=check, text=not binary)


def m0_of(repo: Path, ref: str, release: str) -> str | None:
    """M0 of a release: the last first-parent state of ``ref`` before 21:00 UTC of day T-3 (registration п. 7)."""
    out = git(repo, "log", ref, "--first-parent", f"--until={stage_day('T-3', release)}T21:00:00Z", "-1", "--format=%H").stdout
    return out.strip() or None


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


def run_tree(tree: Path, args: list[str], repo: Path, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the scanner of ``tree`` in a subprocess, that tree first in ``sys.path`` (isolated mode, no bytecode)."""
    return subprocess.run([sys.executable, "-I", "-B", "-c", BOOT, str(tree), *args], cwd=repo, capture_output=True,
                          text=True, env=dict(env) if env is not None else None)  # fmt: skip


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
) -> int:
    """Write the file of a stage once (registration п. 5, 7): 0 = written; 1 = nothing written (outside the window, a
    file already there, the database unreadable)."""
    why = stage_why(stage, release, at)
    if stage not in WRITE_STAGES or why is not None:
        start, end = stage_window(stage, release) if stage in STAGE_DAYS else (None, None)
        say(f"not written: {stage} {release} at {at.isoformat()} is outside its window ({why or 'stage'}): {start} .. {end}")
        return 1
    local, copy = out_paths(stage, release, root, docs)
    for p in (local, copy):
        if p.exists():
            say(f"not written: {p} exists (a stage file is written once)")
            return 1
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
) -> int:
    """A write stage from the command line: refuse early, pick the commit (T-3: M0; else HEAD), extract its CODE_PATHS
    and run that tree's scanner in a subprocess with ``now`` as D."""
    why = stage_why(stage, release, now)
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
    else:
        sha, m0 = git(repo, "rev-parse", "HEAD").stdout.strip(), None
    try:
        tree = extract(repo, sha, code_paths_at(repo, sha), tmp)
    except (subprocess.CalledProcessError, ValueError, OSError, SyntaxError) as e:
        say(f"not written: the scanner's code of {sha[:12]} cannot be extracted ({type(e).__name__}: {str(e)[:200]})")
        return 1
    say(f"code of {sha} ({'M0' if m0 else 'HEAD'}) extracted to {tree}")
    args = ["--stage", stage, "--release", release, "--at", now.isoformat(), "--code-sha", sha, "--m0", m0 or "",
            "--tree", str(tree), "--repo", str(repo), "--db", str(db), "--root", str(root), "--docs", str(docs),
            "--http-retries", str(http_retries)]  # fmt: skip
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
            got = json.loads(res.stdout)
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
    if kw is not None:
        fails.append("mismatch (keys)")
    if not rc_ok:
        fails.append("mismatch (recompute)")
    if bad:
        fails.append(f"mismatch (rebuild: {', '.join(bad)})")
    if gw:
        fails.append(f"mismatch (gamma: {'; '.join(gw)[:200]})")
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
                           rt_factory=lambda: default_runtime(a.http_retries))  # fmt: skip
    except Exception as e:  # noqa: BLE001 - nothing written; the stage may be run again inside its window
        say(f"not written: raised {type(e).__name__}: {str(e)[:300]}")
        return 1


def main() -> None:
    global _OUT
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", required=True, choices=[*WRITE_STAGES, "proof"])
    ap.add_argument("--release", required=True, help="ISO day of the BLS release, e.g. 2026-12-10")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="collector database (opened read-only)")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="stage files: <root>/<release>/<stage>.json")
    ap.add_argument("--docs", type=Path, default=REPO / DOCS_DIR, help="the clone's copies: <docs>/<release>/<stage>.json")
    ap.add_argument("--repo", type=Path, default=REPO)
    ap.add_argument("--ref", default=DEFAULT_REF, help="main as git knows it (M0 and the proof)")
    ap.add_argument("--tmp", type=Path, default=DEFAULT_TMP)
    ap.add_argument("--out", type=Path, help="also append the output to this file")
    a = ap.parse_args()
    date.fromisoformat(a.release)
    fh = None
    if a.out is not None:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        fh = _OUT = a.out.open("a")
    try:
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
