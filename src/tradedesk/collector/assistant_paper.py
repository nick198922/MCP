"""Assistant paper-journal job: once a day (after 09:00 UTC) record the recommendation method's pick as a paper trade,
and on every run settle the paper trades whose expiry has passed.

Settlement (any source, 'method' and 'manual') runs first on every period, before the 09:00 UTC gate and the "already
decided today" check, through the same code as the tool `assistant_paper_status` (`tools.assistant_paper.settle_due`).
Nothing due: no request. A delivery price not yet published leaves the trade open, silently, for the next period.

Docs: docs/ideas/IDEA-20261018-assistant.md ("Рекомендації", "Частота"). Local bookkeeping only: no orders, nothing sent
anywhere. The trade goes through the same code as the tool `assistant_paper_open` (`tools.assistant_paper.book_paper`),
tagged source = 'method', and rests on an hypothesis: the method showed no edge (docs/verification/2026-10-18-assistant-recommendations.md).

The method sees the registered candidate set: nine structures on the expiry closest to 40 days (band 28-56) and
``long_straddle_14d`` / ``long_strangle25_14d`` on the one closest to 14 days (band 7-21), 11 in all, as in the study.
Known deviations from the study's chain: after a day without a pick the job books the next pick at once (the study waits for
the expiry), and extra trades on triggers overlap the chain; those carry ``trigger`` 'regime' / 'event' and are summarised
apart from the 'chain' trades (first / after an expiry) that are comparable with H1.

Rule (card, "Частота"): a NEW trade is booked only when there is none standing (first run, or the previous one expired), when
the regime (fixed RV30 bucket, with hysteresis: held two decisions or 2 vol points past the edge) changed, or when a new
FOMC / CPI event enters the next 72 hours; and never when the pick equals the standing trade (structure and expiry). Otherwise
the standing trade is kept and the state is written as ``actual`` or ``changed`` (the method prefers another structure but
none of the triggers fired); ``no_longer_actual`` when the method picks nothing today. The state is written to
<directory of db_path>/assistant/paper_state.json for the dashboard.

Calendar gap: when the fetched chain lists expiries below and above 28..56 days but none inside (judged by the listed
instrument names, not by the rows left after dropping those without a forward), the study has no decision that day, so
the job writes the state ``skipped`` (no trade, no error) and carries the regime and the seen events from the last real
decision (`_write_skip`). A skip does not close the day: later periods re-check the chain and make the real decision
once a 28..56-day expiry is listed. An empty chain, expiries on one side only, an in-band expiry without usable rows, a
failed request or a missing 14-day expiry stay errors and the next period retries.

The method's ``mean`` / ``q05`` are fractions of the entry FORWARD (`option_replay.method_summary`); they are stored as
returned, not converted to spot, so ``r_study`` matches the study.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..analytics.macro_calendar import build_macro_calendar
from ..analytics.option_replay import STRUCTURES, build_legs
from ..analytics.rec_method import decide, method_pick
from ..core.envelope import Builder
from .assistant_snapshot import write_atomic
from .bad_rows import note_bad_rows

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

SCHEMA = 1
DECISION_HOUR_UTC = 9  # the study enters at the 09:00 UTC marks
EVENT_KINDS = ("fomc", "cpi")  # the card names FOMC and CPI; NFP is an open question for the owner
EVENT_HORIZON_HOURS = 72.0
SHORT_TARGET_DTE, SHORT_DTE_BAND = 14.0, (7.0, 21.0)  # the study's second expiry (day_structures, "_14d" structures)
SHORT_STRUCTURES = ("long_straddle", "long_strangle25")
HISTORY_VENUE, HISTORY_SYMBOL = "binance", "BTC/USDT"
HISTORY_START_MS = 1_502_928_000_000  # 2017-08-17, the series the method was registered on
DAY_MS = 86_400_000
PAGE = 1000
MAX_PAGES = 8


def state_path(db_path: str) -> Path | None:
    if not db_path or db_path == ":memory:":
        return None
    return Path(db_path).resolve().parent / "assistant" / "paper_state.json"


async def fetch_closes(rt: "Runtime", b: Builder, now: datetime) -> list[float] | None:
    """Binance BTC/USDT daily closes up to the last COMPLETE UTC day, paginated (the venue caps rows per request)."""
    day_start_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    bars: dict[int, float] = {}
    cursor = HISTORY_START_MS
    for _ in range(MAX_PAGES):
        rows = await b.guard(
            "ohlcv:history",
            f"ccxt:{HISTORY_VENUE}",
            rt.exchanges.ohlcv(HISTORY_VENUE, HISTORY_SYMBOL, "1d", PAGE, cursor),
        )
        if rows is None:
            return None  # a half history would shift the regime windows: fail the run instead
        got = [x for x in rows if isinstance(x, (list, tuple)) and len(x) >= 5 and x[4]]
        if not got:
            break
        for x in got:
            bars[int(x[0])] = float(x[4])
        last = int(got[-1][0])
        if last + DAY_MS >= day_start_ms or last < cursor:
            break
        cursor = last + DAY_MS
    closes = [c for t, c in sorted(bars.items()) if t < day_start_ms]
    if len(closes) < 400:
        b.miss("ohlcv:history", f"ccxt:{HISTORY_VENUE}", "data_unavailable", f"{len(closes)} daily bars, need at least 400")
        return None
    return closes


async def upcoming_event(rt: "Runtime", b: Builder, now: datetime) -> str | None:
    """Key ``kind:YYYY-MM-DD`` of the first FOMC / CPI in the next 72 h, or None (also when the feed is down)."""
    this_week, next_week = await asyncio.gather(
        b.guard("calendar:thisweek", "forexfactory", rt.forexfactory.this_week()),
        b.guard("calendar:nextweek", "forexfactory", rt.forexfactory.next_week()),
    )
    feed = [*(this_week or []), *(next_week or [])]
    try:
        from ..tools.macro import _load_seed  # noqa: PLC0415

        seed = _load_seed()
    except (FileNotFoundError, ValueError, OSError):
        seed = []
    cal = build_macro_calendar(feed, seed, now, 4)
    for e in cal["events"]:
        if e["kind"] in EVENT_KINDS and 0 <= e["hours_ahead"] <= EVENT_HORIZON_HOURS:
            return f"{e['kind']}:{e['ts_utc'][:10]}"
    return None


def _seen_events(rows: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for x in rows:
        for part in str(x.get("note") or "").split(";"):
            part = part.strip()
            if part.startswith("event="):
                out.add(part[len("event=") :])
    return out


def _open_risk(rows: list[dict[str, Any]], new: dict[str, Any] | None, now_ts: int) -> float:
    """Total risk_usd of the method trades not yet expired (the new one is not in ``rows``)."""
    total = sum(float(x["risk_usd"]) for x in rows if x["expiry_ts"] > now_ts)
    return round(total + (float(new["risk_usd"]) if new else 0.0), 2)


def _summary_view(s: dict[str, Any] | None) -> dict[str, Any] | None:
    if not s:
        return None
    keys = ("n", "n_indep", "mean", "q05", "win", "ci_lo", "ci_hi", "score")
    return {k: s.get(k) for k in keys}


async def settle_expired(rt: "Runtime", now: datetime) -> tuple[int, str | None]:
    """Settle the due paper trades through the tool's code. Never raises: returns (rows settled, error or None).

    The error is set when the delivery prices could not be fetched (the trades stay open and the next period retries) or
    when the settlement itself raised (store, a row another writer left unreadable); the job raises it after the day's
    decision, so collector health shows a persistent failure. Not errors: a price not yet published (silent), delivery
    rows dropped as malformed (counted in the run's ``rows_dropped`` and logged), a trade with malformed stored legs
    (logged every run; it never settles and stays visible as "expired, not settled" in the checkpoint report).
    """
    from ..tools.assistant_paper import settle_due  # noqa: PLC0415

    assert rt.store is not None
    b = Builder()  # its own: "not yet published" entries must not reach the decision's state file
    try:
        res = await settle_due(rt, b, int(now.timestamp()))
    except Exception as e:  # noqa: BLE001 - a settlement failure must not block the day's decision
        log.warning("assistant paper: settlement failed: %s: %s", type(e).__name__, e)
        return 0, f"settlement failed: {type(e).__name__}: {e}"
    if res["bad_rows"]:
        note_bad_rows(res["bad_rows"])
        log.warning("assistant paper: %d malformed Deribit delivery row(s) dropped", res["bad_rows"])
    if res["bad_legs"]:
        log.warning("assistant paper: rec_paper %s due but not settled: stored legs are malformed", res["bad_legs"])
    if res["settled"]:
        log.info("assistant paper: settled %d expired paper trade(s)", res["settled"])
    if res["failed"]:
        fields = {f"delivery_prices:{c}" for c in res["failed"]}
        detail = "; ".join(f"{m.field}: {m.detail}" for m in b.missing if m.field in fields)
        return res["settled"], f"settlement: no delivery prices, will retry: {detail}"
    return res["settled"], None


async def collect_assistant_paper(rt: "Runtime") -> int:
    """Collector job. Returns the rows written: paper trades settled plus 1 when a new one was booked.

    First settles the due paper trades (every run). Then the day's decision: nothing before 09:00 UTC or when today's
    real decision is already in the state file; a failed decision writes nothing, so the next period retries the same
    day. Today's calendar-gap skip does not close the day: each later period fetches the chain again and, once a
    28..56-day expiry is listed, makes the real decision (against the regime and seen events the skip carried); a failed
    fetch on such a run raises and leaves the skip in place.
    Raises (the scheduler records it in collector health) when the decision's data failed or, after the decision has
    run, when the settlement failed; trades settled before a failed decision stay settled.
    """
    s = rt.settings
    path = state_path(s.db_path)
    if path is None or rt.store is None:
        raise RuntimeError("assistant paper: needs a SQLite store (set TRADEDESK_DB_PATH)")
    now = rt.now()
    await rt.store.init()
    settled, settle_error = await settle_expired(rt, now)
    try:
        booked = await _decide_and_book(rt, now, path)
    except Exception as e:
        if settle_error:
            raise RuntimeError(f"{e}; {settle_error}") from e
        raise
    if settle_error:
        raise RuntimeError(f"assistant paper: {settle_error}")
    return settled + booked


async def _decide_and_book(rt: "Runtime", now: datetime, path: Path) -> int:
    """The day's decision; 1 when a new paper trade was booked, else 0. Raises when its data failed."""
    assert rt.store is not None
    s = rt.settings
    if now.hour < DECISION_HOUR_UTC:
        return 0
    today = now.date().isoformat()
    prev = await asyncio.to_thread(_load_state, path)
    if prev is not None and prev.get("date") == today and prev.get("state") != "skipped":
        return 0  # today's real decision is on file; a skip is re-checked each period (a partial reply can look like a gap)
    # lazy: importing the tool module registers nothing, it only gives the shared booking code
    from ..tools.assistant_paper import DEFAULT_LOT, book_paper, prepare_chain  # noqa: PLC0415

    b = Builder()
    gap: dict[str, Any] = {}
    ctx40 = await prepare_chain(rt, b, "BTC", None, gap=gap)
    if ctx40 is None and gap:
        await _write_skip(rt, now, path, prev, gap)
        return 0
    ctx14 = await prepare_chain(rt, b, "BTC", None, target=SHORT_TARGET_DTE, band=SHORT_DTE_BAND) if ctx40 is not None else None
    closes = await fetch_closes(rt, b, now) if ctx40 is not None and ctx14 is not None else None
    if ctx40 is None or ctx14 is None or closes is None:
        raise RuntimeError("assistant paper: no data, will retry: " + "; ".join(f"{m.field}: {m.detail}" for m in b.missing))
    # the registered candidate set: 9 structures on the ~40 d expiry + two on the ~14 d one (day_structures in the study)
    structures: dict[str, tuple[Any, float]] = {}
    where: dict[str, tuple[str, dict[str, Any]]] = {}  # candidate name -> (structure to book, chain context)
    for name in STRUCTURES:
        built = build_legs(name, ctx40["roles"], ctx40["surf"]["F"], spread_vol=0.01)
        if built is not None:
            structures[name] = (built[1], ctx40["t_years"])
            where[name] = (name, ctx40)
    for base in SHORT_STRUCTURES:
        built = build_legs(base, ctx14["roles"], ctx14["surf"]["F"], spread_vol=0.01)
        if built is not None:
            structures[base + "_14d"] = (built[1], ctx14["t_years"])
            where[base + "_14d"] = (base, ctx14)
    res = method_pick(structures, closes)
    book_as, ctx = where[res["pick"]] if res["pick"] else (None, ctx40)
    event_key = await upcoming_event(rt, b, now)
    rows = [x for x in await rt.store.rec_paper_list(limit=5000) if x["source"] == "method"]
    last = rows[0] if rows else None  # newest first
    day_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    seen = _seen_events(rows) | set((prev or {}).get("events_seen") or [])
    verdict = decide(
        pick=book_as,
        pick_expiry_ts=int(ctx["exp"].timestamp()),
        regime=res["regime"],
        rv30=res["rv30"],
        prev_regime=(prev or {}).get("regime"),
        event_key=event_key,
        last=last,
        standing=rows,
        now_ts=int(now.timestamp()),
        seen_events=seen,
    )
    if event_key:
        seen.add(event_key)  # the event has had its decision (a trade or a deliberate none): do not reuse it
    trade: dict[str, Any] | None = None
    if verdict["action"] == "open" and last is not None and last["opened_ts"] >= day_start:
        verdict = {**verdict, "action": "hold", "reason": "a method trade was already booked today"}
    if verdict["action"] == "open":
        summ = res["summary"]
        note = f"auto; reason={verdict['reason']}; rv30={res['rv30']:.4f}; candidate={res['pick']}" + (
            f"; event={event_key}" if event_key else ""
        )
        trade = await book_paper(
            rt,
            b,
            ctx,
            structure=book_as,
            currency="BTC",
            deposit_usd=s.assistant_paper_deposit_usd,
            risk_pct=s.assistant_paper_risk_pct,
            size_btc=None,
            lot=DEFAULT_LOT["BTC"],
            source="method",
            method_mean_pct=summ["mean"],  # fraction of the entry forward, as returned
            method_q05_pct=summ["q05"],
            regime=res["regime"],
            note=note,
            trigger=verdict["trigger"],
        )
        if trade is None:
            raise RuntimeError("assistant paper: booking failed: " + "; ".join(f"{m.field}: {m.detail}" for m in b.missing))
    state = {
        "schema": SCHEMA,
        "label": "hypothesis",
        "date": today,
        "asof_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state": verdict["state"],
        "action": verdict["action"],
        "reason": verdict["reason"],
        "pick": res["pick"],
        "regime": res["regime"],
        "rv30": res["rv30"],
        "event": event_key,
        "events_seen": sorted(seen),
        "trigger": verdict.get("trigger"),
        "summary_fraction_of_forward": _summary_view(res["summary"]),
        "trade_id": trade["id"] if trade else None,
        "standing_trade": last["id"] if last and last["expiry_ts"] > int(now.timestamp()) else None,
        "open_method_risk_usd": _open_risk(rows, trade, int(now.timestamp())),
        "missing": [{"field": m.field, "detail": m.detail} for m in b.missing],
    }
    await asyncio.to_thread(write_atomic, path, state)
    log.info("assistant paper: %s (%s), pick=%s", verdict["state"], verdict["reason"], res["pick"])
    return 1 if trade else 0


async def _write_skip(rt: "Runtime", now: datetime, path: Path, prev: dict[str, Any] | None, gap: dict[str, Any]) -> None:
    """Calendar gap: the chain lists expiries below and above 28..56 days but none inside, so the registered study makes no
    decision that day (verify_assistant_recs.py skips a day without the 40-day long straddle). Writes today's state
    ``skipped``: no trade, no pick. A skip day is not a decision: ``regime`` / ``rv30`` and ``events_seen`` are carried
    from the previous state (itself a skip carrying the last real decision), so the next real decision's hysteresis
    compares with the last real one and an event inside the gap is not marked seen; ``last_decision_date`` names that
    decision (None when no real decision is on file). The standing trade is read from the store as on any day."""
    from ..tools.assistant_paper import DTE_BAND  # noqa: PLC0415

    assert rt.store is not None
    p = prev or {}
    rows = [x for x in await rt.store.rec_paper_list(limit=5000) if x["source"] == "method"]
    last = rows[0] if rows else None
    now_ts = int(now.timestamp())
    seen = p.get("events_seen")
    regime, rv30 = p.get("regime"), p.get("rv30")
    carried = p.get("last_decision_date") if p.get("state") == "skipped" else p.get("date")
    state = {
        "schema": SCHEMA,
        "label": "hypothesis",
        "date": now.date().isoformat(),
        "asof_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state": "skipped",
        "action": "none",
        "reason": (
            f"calendar gap: no listed expiry within {DTE_BAND[0]:g}..{DTE_BAND[1]:g} days ({gap['below']} at {gap['below_days']:g} d, "
            f"{gap['above']} at {gap['above_days']:g} d); the registered study makes no decision on such a day"
        ),
        "pick": None,
        "regime": regime if isinstance(regime, str) else None,
        "rv30": rv30 if isinstance(rv30, (int, float)) and not isinstance(rv30, bool) else None,
        "event": None,
        "events_seen": sorted({str(x) for x in seen}) if isinstance(seen, list) else [],
        "trigger": None,
        "summary_fraction_of_forward": None,
        "trade_id": None,
        "standing_trade": last["id"] if last and last["expiry_ts"] > now_ts else None,
        "open_method_risk_usd": _open_risk(rows, None, now_ts),
        "gap": gap,
        "last_decision_date": carried if isinstance(carried, str) else None,
        "missing": [],
    }
    await asyncio.to_thread(write_atomic, path, state)
    log.info("assistant paper: skipped (%s)", state["reason"])


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None
