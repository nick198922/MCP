"""Macro-event calendar stored locally plus the event-study aggregator that
runs windowed statistics across every stored event of a given kind.

Pattern follows the DoubleTop FOMC write-up: pull a before/after window of
OHLCV around each event, compute one scalar per event (direction, close_vs_t0,
max_up, max_dd, volatility), then report hit rate / median / p25-p75 together
with the sample size and a ``small_sample_flag`` so the caller can never miss
the caveat.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib import resources
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.event_study import MEASURES
from ..analytics.event_study import event_study as _event_study
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import (
    MAX_CELL_DEPTH,
    READ_ONLY,
    STORE_ERRORS,
    cap_cell_depth,
    drop_corrupt_rows,
    r,
    shown_int,
    store_failed,
    store_num,
    store_ready,
)
from ._util import finite_json as _finite_json

# 2000-01-01T00:00:00Z in unix milliseconds: an events_add ts_ms below it is almost certainly unix seconds.
MIN_TS_MS = 946_684_800_000


def _ts_ok(ts: int) -> bool:
    """True when unix seconds `ts` fit a datetime (a ts_ms sent in microseconds, or 10**18, does not)."""
    try:
        datetime.fromtimestamp(ts, UTC)
    except (ValueError, OverflowError, OSError):
        return False
    return True


def _event_ts(e: dict[str, Any]) -> int | None:
    """Unix seconds of a stored event (a REAL cell truncated, as int() did before), or None when the cell is not a finite
    number (a string, a blob or inf from another writer) or falls outside the datetime range."""
    v = store_num(e.get("ts"))
    return int(v) if v is not None and _ts_ok(int(v)) else None


def _bar_ok(x: dict[str, Any]) -> bool:
    """True when a stored bar has a numeric timestamp and finite open / high / low / close."""
    return all(store_num(x.get(k)) is not None for k in ("ts", "open", "high", "low", "close"))


def _seed_row_ok(x: Any) -> bool:
    """True when a seed row is an object with a finite in-range `ts` (unix seconds), text `kind` / `label` and a strict
    JSON `meta`. A ts of 1e308 / Infinity raised OverflowError out of the insert; other junk counted as already present."""
    if not isinstance(x, dict) or _event_ts(x) is None or not _finite_json(x.get("meta")):
        return False
    return all(isinstance(x.get(k), str) and x.get(k) for k in ("kind", "label"))


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Add a macro / scheduled event to the local calendar. Pass either `ts_ms` (unix "
            "milliseconds, UTC; a value before 2000-01-01, i.e. < 946684800000, is taken for unix "
            "seconds and rejected as invalid_input) or `date` + `time_utc` (ISO 'YYYY-MM-DD' and 'HH:MM'). `kind` is "
            "free-form (fomc / cpi / nfp / halving / listing / …). Idempotent: duplicates with "
            "the same (ts, kind, label) are ignored."
        ),
    )
    async def events_add(
        kind: Annotated[
            str,
            Field(
                min_length=1,
                max_length=40,
                description="Free-form event kind, e.g. 'fomc', 'cpi', 'nfp'; event_study filters on it, so reuse existing kinds.",
            ),
        ],
        label: Annotated[
            str, Field(min_length=1, max_length=200, description="Human-readable label, e.g. 'FOMC rate decision'. Part of the dedup key.")
        ],
        impact: Annotated[
            Literal["low", "medium", "high"], Field(description="Your expected market impact; a subjective tag used only for filtering.")
        ] = "medium",
        ts_ms: Annotated[
            int | None,
            Field(description="Unix milliseconds UTC (not seconds; values before 2000-01-01 are rejected). Use this OR date+time_utc."),
        ] = None,
        date: Annotated[str | None, Field(description="ISO date 'YYYY-MM-DD' (UTC).")] = None,
        time_utc: Annotated[str | None, Field(description="'HH:MM' UTC. Defaults to 13:30.")] = None,
        meta: Annotated[dict[str, Any] | None, Field(description="Arbitrary JSON extras.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "events"):
            return b.build(None, ok=False)
        try:
            if ts_ms is not None:
                ts = int(ts_ms) // 1000
                if not _ts_ok(ts):
                    b.miss(
                        "ts_ms",
                        "tradedesk",
                        "invalid_input",
                        f"ts_ms={shown_int(ts_ms)} is outside the datetime range (unix milliseconds expected)",
                    )
                    return b.build(None, ok=False)
                if ts_ms < MIN_TS_MS:
                    b.miss(
                        "ts_ms",
                        "tradedesk",
                        "invalid_input",
                        f"ts_ms={ts_ms} is before 2000-01-01: ts_ms must be milliseconds (got seconds?)",
                    )
                    return b.build(None, ok=False)
            elif date:
                tt = (time_utc or "13:30").strip()
                try:
                    dt = datetime.strptime(f"{date} {tt}", "%Y-%m-%d %H:%M")
                except ValueError:
                    b.miss("date", "tradedesk", "data_unavailable", "could not parse date+time_utc")
                    return b.build(None, ok=False)
                ts = int(dt.replace(tzinfo=rt.now().tzinfo).timestamp())
            else:
                b.miss("ts", "tradedesk", "data_unavailable", "provide ts_ms OR date (+time_utc)")
                return b.build(None, ok=False)
            if not _finite_json(meta):  # stored as NaN / Infinity, it came back non-finite in every events_list
                b.miss("meta", "tradedesk", "invalid_input", "meta holds NaN or inf (only finite JSON numbers are stored)")
                return b.build(None, ok=False)
            row_id = await rt.store.event_insert(ts=ts, kind=kind, label=label, impact=impact, meta=meta)
            return b.build({"id": row_id, "inserted": row_id > 0, "ts": ts, "kind": kind, "label": label})
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "List events from the local calendar. Filter by `kind`, `impact`, and a UTC date "
            "window (`since_date` / `until_date` as 'YYYY-MM-DD'). Ordered oldest-first."
        ),
    )
    async def events_list(
        kind: Annotated[str | None, Field(description="Filter by kind (e.g. 'fomc').")] = None,
        impact: Annotated[Literal["low", "medium", "high"] | None, Field(description="Filter by impact.")] = None,
        since_date: Annotated[str | None, Field(description="'YYYY-MM-DD' (UTC) lower bound.")] = None,
        until_date: Annotated[str | None, Field(description="'YYYY-MM-DD' (UTC) upper bound.")] = None,
        limit: Annotated[int, Field(ge=1, le=5000, description="Maximum number of events to return (1-5000).")] = 500,
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "events"):
            return b.build(None, ok=False)
        try:
            since_ts = until_ts = None
            if since_date:
                try:
                    since_ts = int(datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=rt.now().tzinfo).timestamp())
                except ValueError:
                    b.miss("since_date", "tradedesk", "data_unavailable", "invalid date")
                    return b.build(None, ok=False)
            if until_date:
                try:
                    until_ts = int(datetime.strptime(until_date + " 23:59", "%Y-%m-%d %H:%M").replace(tzinfo=rt.now().tzinfo).timestamp())
                except ValueError:
                    b.miss("until_date", "tradedesk", "data_unavailable", "invalid date")
                    return b.build(None, ok=False)
            rows = await rt.store.events_list(kind=kind, impact=impact, since_ts=since_ts, until_ts=until_ts, limit=limit)
            # A blob cell (an invalid UTF-8 one failed the whole reply), a non-numeric ts or a NaN / Infinity meta from
            # another writer: the row is skipped with a warning; an all-malformed window is not "no events".
            stored = len(rows)
            rows = drop_corrupt_rows(b, rows, "event", ("ts",), required=("ts",))
            deep = cap_cell_depth(rows, "meta")  # 300 levels failed the MCP serialiser of the whole reply (item 358)
            if deep:
                b.warn(f"{len(deep)} stored event rows had meta nested deeper than {MAX_CELL_DEPTH} levels; shown as null; id {deep[:10]}")
            nonfinite = [e.get("id") for e in rows if not _finite_json(e.get("meta"))]
            if nonfinite:
                b.warn(f"{len(nonfinite)} stored event rows skipped: NaN / Infinity in meta; id {nonfinite[:10]}")
                rows = [e for e in rows if _finite_json(e.get("meta"))]
            if stored and not rows:
                b.miss("events", "store", "data_unavailable", f"all {stored} matching stored events are malformed")
                return b.build({"count": 0, "events": []}, ok=False)
            return b.build({"count": len(rows), "events": rows})
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Load the bundled historical events seed (FOMC / CPI / NFP 2024-2027, plus US election "
            "day 2026-11-03 as kind 'election' with a nominal 12:00 UTC time) into the "
            "local store. Idempotent — rows with the same (ts, kind, label) are ignored. 2026 "
            "Fed dates in the seed are PROJECTED; verify against the official Fed/BLS calendars."
        ),
    )
    async def events_seed_load() -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "events"):
            return b.build(None, ok=False)
        try:
            try:
                with resources.files("tradedesk.data").joinpath("events_seed.json").open("r", encoding="utf-8") as f:
                    seed = json.load(f)
            except (FileNotFoundError, ValueError, OSError) as e:
                b.miss("seed", "tradedesk", "data_unavailable", f"{type(e).__name__}: {e}")
                return b.build(None, ok=False)
            # A seed that is not {"events": [...]} raised AttributeError / TypeError; rows that cannot be stored are skipped.
            raw = seed.get("events") if isinstance(seed, dict) else None
            if not isinstance(raw, list) or not raw:
                b.miss("seed", "tradedesk", "data_unavailable", "the seed is not an object with a non-empty `events` list")
                return b.build(None, ok=False)
            rows = [x for x in raw if _seed_row_ok(x)]
            if len(rows) < len(raw):
                b.warn(f"{len(raw) - len(rows)} seed rows skipped: not an object with an in-range ts, text kind / label, strict JSON meta")
            if not rows:
                b.miss("seed", "tradedesk", "data_unavailable", f"all {len(raw)} seed rows are malformed")
                return b.build(None, ok=False)
            before = await rt.store.event_count()
            inserted = await rt.store.events_insert_many(rows)
            total = await rt.store.event_count()
            b.warn("2026 Fed dates in the seed are projected; verify against the official calendar")
            return b.build(
                {
                    "candidates": len(rows),
                    "inserted": inserted,
                    "already_present": len(rows) - inserted,
                    "events_before": before,
                    "events_after": total,
                    "meta": seed.get("_meta"),
                }
            )
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Event-study: for every stored event of `event_kind`, measure one scalar over the "
            "window [event_ts − before_minutes, event_ts + after_minutes] using the chosen "
            "`measure` and return distribution stats across the sample. "
            "Needs ohlcv_hist populated for (venue, symbol, timeframe) AND events already in the "
            "local store. Measures: 'direction' (1 if close_after ≥ close_t0; hit_rate = share "
            "of ups), 'close_vs_t0' (signed return), 'max_up' (max intra-window up excursion ≥ 0),"
            " 'max_dd' (max intra-window drawdown ≤ 0), 'volatility' (window-level realised "
            "stdev of log-returns). Units: close_vs_t0, max_up and max_dd are decimal returns relative to "
            "close(t0) (0.01 = 1 %); 'volatility' is a decimal, the per-bar stdev of log-returns times "
            "sqrt(bars in the window), NOT annualised; 'direction' values are 0/1 and hit_rate is a "
            "fraction 0-1. mean/median/p25/p75/min/max are in the measure's own unit. "
            "Reports n_sample and small_sample_flag=true when n<20. "
            "Hit rates and medians are DESCRIPTIVE statistics on a finite sample — they are not "
            "forecasts; always eyeball n_sample and the regime window before trading on them."
        ),
    )
    async def event_study(
        venue: Annotated[str, Field(description="ccxt venue id (must match ohlcv_hist).")],
        symbol: Annotated[str, Field(description="ccxt symbol (must match ohlcv_hist).")],
        timeframe: Annotated[str, Field(description="Timeframe stored in ohlcv_hist.")] = "1h",
        event_kind: Annotated[str, Field(description="Event kind in the local events table (e.g. 'fomc', 'cpi', 'nfp').")] = "fomc",
        measure: Annotated[str, Field(description=f"One of: {', '.join(MEASURES)}.")] = "direction",
        before_minutes: Annotated[
            float, Field(ge=0, le=10_080, description="Window start, minutes before each event (0-10080, i.e. up to 7 days).")
        ] = 60,
        after_minutes: Annotated[
            float, Field(gt=0, le=10_080, description="Window end, minutes after each event (up to 10080, i.e. 7 days).")
        ] = 240,
        since_date: Annotated[str | None, Field(description="UTC 'YYYY-MM-DD' lower bound on event dates.")] = None,
        until_date: Annotated[str | None, Field(description="UTC 'YYYY-MM-DD' upper bound.")] = None,
        impact: Annotated[Literal["low", "medium", "high"] | None, Field(description="Optional impact filter.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "events"):
            return b.build(None, ok=False)
        try:
            if measure not in MEASURES:
                b.miss("measure", "tradedesk", "data_unavailable", f"measure must be one of {MEASURES}")
                return b.build(None, ok=False)

            since_ts = until_ts = None
            if since_date:
                try:
                    since_ts = int(datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=rt.now().tzinfo).timestamp())
                except ValueError:
                    b.miss("since_date", "tradedesk", "data_unavailable", "invalid date")
                    return b.build(None, ok=False)
            if until_date:
                try:
                    until_ts = int(datetime.strptime(until_date + " 23:59", "%Y-%m-%d %H:%M").replace(tzinfo=rt.now().tzinfo).timestamp())
                except ValueError:
                    b.miss("until_date", "tradedesk", "data_unavailable", "invalid date")
                    return b.build(None, ok=False)

            events = await rt.store.events_list(kind=event_kind, impact=impact, since_ts=since_ts, until_ts=until_ts, limit=5000)
            bad = [e for e in events if _event_ts(e) is None]
            if bad:
                # Rows stored before events_add validated ts_ms (or by another writer): they would break the bar query and the ISO dates.
                b.warn(
                    f"skipped {len(bad)} stored event(s) with a non-integer timestamp or one outside the datetime range, "
                    f"ids {[e['id'] for e in bad][:10]}"
                )
                events = [e for e in events if _event_ts(e) is not None]
            if not events:
                b.miss("events", "store", "data_unavailable", f"no stored events for kind={event_kind!r} in the window")
                return b.build(None, ok=False)

            # Pull a single OHLCV span covering every event with margin.
            margin_ms = int(max(before_minutes, after_minutes) * 60_000 * 2)
            ev_ts_ms = [int(e["ts"]) * 1000 for e in events]
            bars_lo = min(ev_ts_ms) - margin_ms
            bars_hi = max(ev_ts_ms) + margin_ms
            stored = await rt.store.ohlcv_range(venue=venue, symbol=symbol, timeframe=timeframe, since_ts=bars_lo, until_ts=bars_hi)
            # A bar whose timestamp is not an integer or whose price is not a finite number (another writer) would raise in
            # the window search or the measure (invariant 1): it is skipped, the window spans its neighbours.
            bars = [x for x in stored if _bar_ok(x)]
            if len(bars) < len(stored):
                b.warn(f"{len(stored) - len(bars)} stored bars with a non-integer timestamp or a non-numeric / non-finite price skipped")
            if len(bars) < 5:
                b.miss(
                    "ohlcv_hist",
                    "store",
                    "data_unavailable",
                    f"only {len(bars)} bars stored for {venue} {symbol} {timeframe} in the event window — enable the ohlcv_hist collector and let it backfill",
                )
                return b.build(None, ok=False)

            res = _event_study(bars, ev_ts_ms, before_minutes=before_minutes, after_minutes=after_minutes, measure=measure)
            stats = res["stats"]
            if stats["small_sample_flag"]:
                b.warn(f"small sample: n_sample={stats['n_sample']}; stats are noisy")

            # Round where sensible, keep hit_rate / returns in decimal form.
            rounded_stats = {k: (r(v, 6) if isinstance(v, float) else v) for k, v in stats.items()}
            return b.build(
                {
                    "venue": venue,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "event_kind": event_kind,
                    "measure": measure,
                    "before_minutes": before_minutes,
                    "after_minutes": after_minutes,
                    "stats": rounded_stats,
                    "n_events_requested": res["n_events_requested"],
                    "n_events_evaluated": res["n_events_evaluated"],
                    "per_event": [
                        {
                            "ts_ms": pe["ts_ms"],
                            "iso": datetime.fromtimestamp(pe["ts_ms"] / 1000, tz=rt.now().tzinfo).isoformat(),
                            "value": r(pe["value"], 6) if pe["value"] is not None else None,
                        }
                        for pe in res["per_event"]
                    ],
                }
            )
        except STORE_ERRORS as e:
            return store_failed(b, e)
