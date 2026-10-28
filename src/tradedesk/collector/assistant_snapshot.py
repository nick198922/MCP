"""Assistant snapshot job: one JSON file with everything the dashboard's brief and options tabs show.

Docs: docs/ideas/IDEA-20261018-assistant.md. The job calls our own read-only MCP tools in-process (a throw-away
MCPServer sharing the Runtime, so rate limits, cache and health are the same as for any other call) and stores each
tool's envelope as one section. A failing section lands in that section's ``missing`` and in the top-level ``missing``
list; it never fails the job. Strings from external APIs are kept as plain text (escaping is done at render time).

The ``radar`` section is not a tool call: it reads the radar archive (table ``radar_surface``, job ``radar_archive``),
daily DVOL (Deribit) and daily closes (Binance) through the providers and builds `analytics.radar.radar_view` for BTC
and ETH (with the yearly chart series: a year of closes, DVOL and archive rows); fields without data are null with a
reason in the asset's ``missing``.

The ``paper_rec`` section is not a tool call either: it reads the local state file of the ``assistant_paper`` job
(``<dir of db_path>/assistant/paper_state.json``, no network) through `analytics.assistant.paper_rec_view`, so the brief
can say whether the paper recommendation (a hypothesis) is still current.

The ``my_options`` section is not a tool call either: it reads the owner's open Bybit option positions from the local
file ``<dir of db_path>/assistant/my_options.json`` (written hourly by scripts/bybit_options_positions.py with the
read-only key; this job never sees the key and makes no request) through `analytics.my_options.my_options_view`. It is
never carried forward from an older snapshot.

The ``flows`` section is `market_flow` for BTC (1h, 2 days, summaries only) for the brief's «Потоки» lines; the
``flow_btc`` / ``flow_eth`` sections are `market_flow` (1h, 7 days, with the per-bar series) for the dashboard's
«Потоки» tab.

The ``cycle`` section is `cycle_regime` for BTC (today's state, no history) for the brief's «Режим циклу» line.

The ``chains`` section holds the option chain of every expiry of the ATM term structure (``{"by_expiry": {token:
chain}}``, the same shape as ``chain``) for the options builder's expiry list; the strike window widens with time to expiry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..analytics.assistant import paper_rec_view, pick_expiries, pick_near_expiry
from ..analytics.macro_calendar import build_macro_calendar
from ..analytics.my_options import my_options_view
from ..analytics.radar import FWD_EVENT_KINDS, IV_RANK_DAYS, METHOD, WEEK_DAYS, YEAR_DAYS, radar_view
from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

SCHEMA = 1
SECTION_TIMEOUT_SECONDS = 120.0
VENUE = "binanceusdm"
SPOT_VENUE = "binance"
TICKER_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
CHAIN_WIDTH_PCT = 20.0  # strike window (± % log-moneyness) of the month and near chains, the floor of the others
CHAIN_WIDTH_MAX_PCT = 60.0
FLOW_TAB_DAYS = 7  # market_flow window of the dashboard's «Потоки» tab (1h bars)


def snapshot_path(db_path: str, configured: str) -> Path | None:
    """Configured path, else <dir of db_path>/assistant/snapshot.json; None when neither is usable (in-memory store)."""
    if configured.strip():
        return Path(configured)
    if not db_path or db_path == ":memory:":
        return None
    return Path(db_path).resolve().parent / "assistant" / "snapshot.json"


def _finite(v: Any) -> Any:
    """Replace NaN / infinities with None so the file is strict JSON."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite(x) for x in v]
    return v


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON next to the target and rename it over: a reader never sees a half-written file."""
    payload = _finite(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        os.chmod(tmp, 0o644)  # the dashboard runs as another process
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _failed(provider: str, reason: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "missing": [{"field": "section", "provider": provider, "reason": reason, "detail": detail}],
        "warnings": [],
    }


async def _call(client: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """One tool call -> a snapshot section ({ok, data, missing, warnings}). Never raises."""
    try:
        res = await asyncio.wait_for(client.call_tool(tool, args), SECTION_TIMEOUT_SECONDS)
        if getattr(res, "is_error", False):
            text = res.content[0].text if res.content and hasattr(res.content[0], "text") else "tool error"
            return _failed("tradedesk", "error", f"{tool}: {text[:300]}")
        env = res.structured_content
        if env is None:
            env = json.loads(res.content[0].text)
        if set(env) == {"result"}:
            env = env["result"]
        return {
            "ok": bool(env.get("ok")),
            "data": env.get("data"),
            "missing": env.get("missing") or [],
            "warnings": env.get("warnings") or [],
        }
    except Exception as e:  # noqa: BLE001 - invariant 1: a section never fails the job
        return _failed("tradedesk", "error", f"{tool}: {type(e).__name__}: {e}")


def _expiry_rows(term: dict[str, Any]) -> list[dict[str, Any]]:
    data = term.get("data") if term.get("ok") else None
    return list((data or {}).get("expiries") or [])


def chain_width_pct(days: float) -> float:
    """Strike window of one ``chains`` expiry: 60 % × √(days/365) (about ±1.2σ at 50 % vol), within 20..60 %."""
    return float(min(CHAIN_WIDTH_MAX_PCT, max(CHAIN_WIDTH_PCT, round(60 * math.sqrt(max(days, 0.0) / 365)))))


async def build_chains(client: Any, expiries: list[dict[str, Any]]) -> dict[str, Any]:
    """Section ``chains``: deribit_option_chain of every term-structure expiry, keyed by expiry token. One cached
    Deribit book serves all of them. ok when at least one chain came back; each failed expiry lands in ``missing``."""
    rows = [x for x in expiries if isinstance(x.get("expiry"), str) and isinstance(x.get("days"), (int, float)) and x["days"] > 0]
    if not rows:
        return _failed("tradedesk", "data_unavailable", "no expiries in the ATM term structure")
    args = [{"currency": "BTC", "expiry": x["expiry"], "width_pct": chain_width_pct(x["days"])} for x in rows]
    done = await asyncio.gather(*(_call(client, "deribit_option_chain", a) for a in args))
    by: dict[str, Any] = {}
    missing: list[dict[str, Any]] = []
    for x, sec in zip(rows, done, strict=True):
        if sec["ok"] and isinstance(sec.get("data"), dict):
            by[x["expiry"]] = sec["data"]
        else:
            missing += [{**m, "field": f"chain {x['expiry']}"} for m in sec["missing"] if isinstance(m, dict)]
    if not by:
        return {"ok": False, "data": None, "missing": missing or _failed("tradedesk", "error", "no chain")["missing"], "warnings": []}
    return {"ok": True, "data": {"by_expiry": by}, "missing": missing, "warnings": []}


# ----------------------------------------------------------------------------- radar section

RADAR_ASSETS = ("BTC", "ETH")
RADAR_DVOL_DAYS = IV_RANK_DAYS + WEEK_DAYS + 3  # a year window for today and for a week ago
RADAR_CLOSES = YEAR_DAYS + 5  # daily closes for the year price chart (RV30 needs 31); one Binance request (max 1000)
RADAR_ROW_DAYS = YEAR_DAYS + 3  # archive rows read: the year of skew history (and «now» / the row a week before it)
RADAR_EVENT_DAYS = 400  # seed events beyond the calendar section's horizon, for the longest forward-vol windows
_DAY_MS = 86_400_000
RADAR_WARNINGS = [
    "Radar surface: Deribit marks from option prints of the 6 h before 19:00 UTC (history.deribit.com), one archive row "
    "per day; 'now' is the newest archived day, not the live market, and 'week' the row 7 days earlier.",
    "Skew and smile use Black-76 forward deltas at each strike's own mark IV, not Deribit's premium-adjusted deltas: "
    "levels differ from other sources (docs/library/LIB-20261018-datahawks-options.md), compare changes over time.",
    "IV Rank and percentile: daily Deribit DVOL (a 30-day index, not the ATM of each tenor) over 365 days; the newest "
    "close is the live current day. 'Low IV Rank -> buy options' is not confirmed as an edge by our data (LIB H1).",
    "Realized vol: zero-mean close-to-close of Binance spot daily closes; the newest close is the live current-day price.",
    "Forward vol: total-variance difference of neighbouring expiries' ATM IV; events are matched by date only (seed "
    "times are approximate), so a high window does not prove the market prices that event. Not investment advice.",
    "Yearly series (charts): price = Binance spot daily closes by UTC day (the newest is the live current-day price, so it "
    "differs from the 19:00 UTC Deribit index of 'price'); DVOL = daily closes; skew = the archive's 30-day put skew and "
    "RR 25-delta, as long as the archive goes back (a year only after the backfill). Values rounded to 4-6 significant digits.",
]


async def _radar_dvol(rt: "Runtime", b: Builder, asset: str, now: datetime) -> list[tuple[int, float]]:
    end = int(now.timestamp() * 1000)
    start = end - RADAR_DVOL_DAYS * _DAY_MS
    pts: dict[int, float] = {}
    for _ in range(5):  # Deribit pages with `continuation`
        page = await b.guard(f"radar:dvol:{asset}", "deribit", rt.deribit.volatility_index_data(asset, start, end, 86400))
        if not page:
            break
        for x in page.get("data") or []:
            try:
                pts[int(x[0])] = float(x[4])
            except (TypeError, ValueError, IndexError):
                continue
        cont = page.get("continuation")
        if not cont or int(cont) <= start or int(cont) >= end:
            break
        end = int(cont)
    return sorted(pts.items())


async def _radar_closes(rt: "Runtime", b: Builder, asset: str) -> list[tuple[int, float]]:
    bars = await b.guard(f"radar:ohlcv:{asset}", f"ccxt:{SPOT_VENUE}", rt.exchanges.ohlcv(SPOT_VENUE, f"{asset}/USDT", "1d", RADAR_CLOSES))
    out = []
    for x in bars or []:
        try:
            out.append((int(x[0]), float(x[4])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


async def _radar_rows(rt: "Runtime", b: Builder, asset: str, now: datetime) -> list[dict[str, Any]]:
    if rt.store is None:
        b.miss(f"radar:archive:{asset}", "tradedesk", "data_unavailable", "no SQLite store (TRADEDESK_DB_PATH)")
        return []
    since = (now - timedelta(days=RADAR_ROW_DAYS)).strftime("%Y-%m-%d")
    try:
        await rt.store.init()
        return await rt.store.radar_list(asset, since=since)
    except Exception as e:  # noqa: BLE001 - a broken archive must not fail the snapshot
        b.miss(f"radar:archive:{asset}", "tradedesk", "error", f"{type(e).__name__}: {e}")
        return []


def _radar_events(sections: dict[str, Any], now: datetime, b: Builder) -> list[dict[str, Any]]:
    """The calendar section's events (feed + seed) within its horizon, then the bundled seed beyond it."""
    from ..tools.macro import _load_seed  # noqa: PLC0415 - the same seed file macro_calendar reads

    sec = sections.get("events") or {}
    data = sec.get("data") if sec.get("ok") and isinstance(sec.get("data"), dict) else None
    events = [e for e in (data or {}).get("events") or [] if isinstance(e, dict)]
    cutoff = now + timedelta(days=int((data or {}).get("horizon_days") or 0)) if data else None
    try:
        seed = _load_seed()
    except (FileNotFoundError, ValueError, OSError) as e:
        b.miss("radar:events_seed", "tradedesk", "data_unavailable", f"{type(e).__name__}: {e}")
        return events
    for e in build_macro_calendar([], seed, now, RADAR_EVENT_DAYS)["events"]:
        if cutoff is None or datetime.fromisoformat(e["ts_utc"]) > cutoff:
            events.append(e)
    return events


async def build_radar(rt: "Runtime", sections: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The `radar` section: `analytics.radar.radar_view` for BTC and ETH. Never raises (invariant 1)."""
    try:
        b = Builder()
        events = _radar_events(sections, now, b)
        assets: dict[str, Any] = {}
        for asset in RADAR_ASSETS:
            rows, dvol, closes = await asyncio.gather(
                _radar_rows(rt, b, asset, now), _radar_dvol(rt, b, asset, now), _radar_closes(rt, b, asset)
            )
            assets[asset] = radar_view(asset, rows, dvol, closes, events, now)
        ok = any(
            a["price"]["now"] is not None or a["iv_rank"]["now"] is not None or a["iv_vs_rv"]["rv30"] is not None for a in assets.values()
        )
        return {
            "ok": ok,
            "data": {"method": METHOD, "event_kinds": sorted(FWD_EVENT_KINDS), "assets": assets},
            "missing": [m.model_dump() for m in b.missing],
            "warnings": [*RADAR_WARNINGS, *b.warnings],
        }
    except Exception as e:  # noqa: BLE001 - invariant 1: a section never fails the job
        return _failed("tradedesk", "error", f"radar: {type(e).__name__}: {e}")


PAPER_WARNINGS = [
    "Paper recommendation = a hypothesis, not advice: the method showed no edge (H1 rejected 18.10, "
    "docs/verification/2026-10-18-assistant-recommendations.md); the paper journal (rec_paper) tests it, no orders.",
    "State of the job assistant_paper's last daily decision (after 09:00 UTC), not a live view: actual = the standing paper "
    "trade is the method's pick or a new one was booked; changed = the method prefers another structure or a regime / "
    "FOMC-CPI trigger booked a new one; no_longer_actual = the method picks nothing.",
]


def build_paper_rec(db_path: str, now: datetime) -> dict[str, Any]:
    """The `paper_rec` section from the assistant_paper job's local state file. Never raises (invariant 1)."""
    try:
        from .assistant_paper import state_path  # noqa: PLC0415 - that module imports this one (write_atomic)

        path = state_path(db_path)
        if path is None:
            return _failed("tradedesk", "data_unavailable", "paper_rec: no SQLite store (TRADEDESK_DB_PATH), no paper journal")
        data, why = paper_rec_view(_load_previous(path), now)
        if data is None:
            return _failed("tradedesk", "data_unavailable", f"paper_rec: {why}")
        return {"ok": True, "data": data, "missing": [], "warnings": list(PAPER_WARNINGS)}
    except Exception as e:  # noqa: BLE001 - invariant 1: a section never fails the job
        return _failed("tradedesk", "error", f"paper_rec: {type(e).__name__}: {e}")


MY_OPTIONS_WARNINGS = [
    "The owner's open Bybit option positions, fetched with the read-only key by scripts/bybit_options_positions.py "
    "(hourly user timer tradedesk-bybit-options) and valued at that file's own time, not live; this job only reads the file.",
    "Chance of profit = risk-neutral probability that the leg ends above / below its breakeven at expiry (Black-76, r = 0, "
    "the leg's Bybit mark IV, flat, fees ignored), per leg: spreads are not combined. The market's price, not a forecast; "
    "not advice.",
    "Theta = Black-76 theta per calendar day at the mark IV x signed size, USD (a long pays it); P&L = Bybit's unrealised P&L at the mark.",
]


def my_options_path(db_path: str) -> Path | None:
    """<dir of db_path>/assistant/my_options.json (written by scripts/bybit_options_positions.py); None for an in-memory store."""
    if not db_path or db_path == ":memory:":
        return None
    return Path(db_path).resolve().parent / "assistant" / "my_options.json"


def build_my_options(db_path: str, now: datetime) -> dict[str, Any]:
    """The `my_options` section from the local positions file (no network, no key here). Never raises (invariant 1)."""
    try:
        path = my_options_path(db_path)
        if path is None:
            return _failed("bybit", "data_unavailable", "my_options: no SQLite store (TRADEDESK_DB_PATH), no positions file")
        data, why = my_options_view(_load_previous(path), now)
        if data is None:
            return _failed("bybit", "data_unavailable", f"my_options: {why}")
        extra = [f"Partial Bybit fetch, positions may be missing: {e}" for e in data["errors"]]
        return {"ok": True, "data": data, "missing": [], "warnings": [*MY_OPTIONS_WARNINGS, *extra]}
    except Exception as e:  # noqa: BLE001 - invariant 1: a section never fails the job
        return _failed("tradedesk", "error", f"my_options: {type(e).__name__}: {e}")


async def build_snapshot(rt: "Runtime") -> dict[str, Any]:
    """Collect all sections. Pure orchestration over tool calls; the file is written by the caller."""
    from mcp import Client  # noqa: PLC0415
    from mcp.server.mcpserver import MCPServer  # noqa: PLC0415

    from ..tools import register_all  # noqa: PLC0415

    server = MCPServer("tradedesk-assistant-snapshot")
    register_all(server, rt)
    now = rt.now()
    async with Client(server) as c:
        plan: dict[str, tuple[str, dict[str, Any]]] = {
            "tickers": ("market_tickers", {"exchange": VENUE, "symbols": TICKER_SYMBOLS}),
            "ohlc": ("market_ohlcv", {"exchange": SPOT_VENUE, "symbol": "BTC/USDT", "timeframe": "1d", "limit": 120}),
            "fng": ("sentiment_fear_greed", {"days": 30}),
            "dvol": ("deribit_dvol", {"currency": "BTC", "hours_back": 192, "resolution": 3600}),
            # 1000 days: by-DVOL-level stats need 365 d of lookback plus 30 d per trade
            "vrp": ("deribit_vrp", {"currency": "BTC", "history_days": 1000}),
            "term": ("deribit_atm_term", {"currency": "BTC"}),
            "skew": ("deribit_skew_term", {"currency": "BTC"}),
            "funding_btc": ("perps_funding_compare", {"base": "BTC"}),
            "funding_eth": ("perps_funding_compare", {"base": "ETH"}),
            "events": ("macro_calendar", {"horizon_days": 14}),
            # the brief's 3 «Потоки» lines (IDEA-20261028-market-flow-reader): summaries only, no per-bar series
            "flows": ("market_flow", {"base": "BTC", "timeframe": "1h", "days": 2, "include_series": False}),
            # the dashboard's «Потоки» tab: a week of hourly bars per coin (charts), summaries and the quadrant
            "flow_btc": ("market_flow", {"base": "BTC", "timeframe": "1h", "days": FLOW_TAB_DAYS, "include_series": True}),
            "flow_eth": ("market_flow", {"base": "ETH", "timeframe": "1h", "days": FLOW_TAB_DAYS, "include_series": True}),
            # the brief's «Режим циклу» line (IDEA-20261028-btc-cycle-regime-gauge): today's state only, no history
            "cycle": ("cycle_regime", {"base": "BTC", "history_days": 0}),
        }
        names = list(plan)
        results = await asyncio.gather(*(_call(c, *plan[n]) for n in names))
        sections = dict(zip(names, results, strict=True))

        week, month = pick_expiries(_expiry_rows(sections["term"]))
        near = pick_near_expiry(_expiry_rows(sections["term"]))
        dependent: dict[str, tuple[str, dict[str, Any]] | None] = {
            "em_week": ("deribit_expected_move", {"currency": "BTC", "expiry": week}) if week else None,
            "em_month": ("deribit_expected_move", {"currency": "BTC", "expiry": month}) if month else None,
            "chain": ("deribit_option_chain", {"currency": "BTC", "expiry": month, "width_pct": 20}) if month else None,
            # the options builder's nearer expiry (rule 8's 1-3 day zone); the same cached Deribit book as `chain`
            "chain_near": ("deribit_option_chain", {"currency": "BTC", "expiry": near, "width_pct": 20}) if near else None,
        }
        dnames = list(dependent)
        calls = [_call(c, *dependent[n]) if dependent[n] else None for n in dnames]
        done = await asyncio.gather(*(x for x in calls if x is not None))
        it = iter(done)
        for n, cl in zip(dnames, calls, strict=True):
            sections[n] = (
                next(it) if cl is not None else _failed("tradedesk", "data_unavailable", "no suitable expiry in the ATM term structure")
            )
        sections["chains"] = await build_chains(c, _expiry_rows(sections["term"]))  # the builder's full expiry list
    sections["radar"] = await build_radar(rt, sections, now)
    sections["paper_rec"] = await asyncio.to_thread(build_paper_rec, rt.settings.db_path, now)
    sections["my_options"] = await asyncio.to_thread(build_my_options, rt.settings.db_path, now)
    asof = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s in sections.values():
        s["asof_utc"] = asof
    return {
        "schema": SCHEMA,
        "asof_utc": asof,
        "currency": "BTC",
        "expiries": {"week": week, "month": month, "near": near},
        "sections": sections,
        "missing": [n for n, s in sections.items() if not s["ok"]],
        "stale": [],
    }


MAX_STALE_HOURS = 6.0  # a carried-forward section older than this is dropped (stays in `missing`)
OFFLINE_SECTIONS = frozenset({"radar", "paper_rec", "my_options"})  # may have data without the network: not counted by the all-failed check
NO_CARRY = frozenset({"my_options"})  # the owner's positions may be closed since: no older copy, the file's own age limit rules


def _age_hours(old: Any, new: str) -> float:
    """Hours between two `%Y-%m-%dT%H:%M:%SZ` stamps; infinity if the old one is unreadable."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return (datetime.strptime(new, fmt) - datetime.strptime(str(old), fmt)).total_seconds() / 3600
    except ValueError:
        return float("inf")


def carry_forward(snap: dict[str, Any], prev: dict[str, Any] | None) -> None:
    """Replace a failed section by the previous file's good one (own `asof_utc`, `stale: true`, new failure in `missing`).

    Only the same expiry is carried for expiry-dependent sections, so the brief never mixes expiries.
    """
    if not isinstance(prev, dict) or not isinstance(prev.get("sections"), dict):
        return
    dependent = {"em_week": "week", "em_month": "month", "chain": "month", "chain_near": "near"}
    for name in list(snap["missing"]):
        if name in NO_CARRY:
            continue
        old = prev["sections"].get(name)
        if not isinstance(old, dict) or not old.get("ok"):
            continue
        old_asof = old.get("asof_utc") or prev.get("asof_utc")
        if _age_hours(old_asof, snap["asof_utc"]) > MAX_STALE_HOURS:
            continue  # too old to be useful: better an honest gap in `missing`
        key = dependent.get(name)
        if key:
            old_exp = (prev.get("expiries") or {}).get(key)
            new_exp = snap["expiries"].get(key)
            if old_exp is None or (new_exp is not None and new_exp != old_exp):
                continue  # the term structure moved on to another expiry: the old data would be mislabelled
            snap["expiries"][key] = old_exp  # term structure unavailable now: keep the expiry the old data belongs to
        sec = snap["sections"][name]
        snap["sections"][name] = {
            "ok": True,
            "data": old.get("data"),
            "missing": sec["missing"],
            "warnings": old.get("warnings") or [],
            "asof_utc": old_asof,
            "stale": True,
        }
        snap["missing"].remove(name)
        snap["stale"].append(name)


def _load_previous(path: Path) -> dict[str, Any] | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


async def collect_assistant_snapshot(rt: "Runtime") -> int:
    """Collector job: build the snapshot and write it atomically. Returns the number of sections that have data.

    Fails (so the scheduler records it) only when no tool section has data (``radar`` and ``paper_rec`` do not count:
    they come from local files) or no path is configured: an all-empty snapshot would overwrite the last useful one.
    """
    s = rt.settings
    path = snapshot_path(s.db_path, s.assistant_snapshot_path)
    if path is None:
        raise RuntimeError("assistant snapshot: set TRADEDESK_ASSISTANT_SNAPSHOT_PATH (the store is in memory)")
    snap = await build_snapshot(rt)
    ok = len(snap["sections"]) - len(snap["missing"])
    # `radar` can be ok from the local archive alone, so it does not count: with the network down every tool
    # section fails and the previous file must stay (the brief then says the snapshot is old)
    if not any(s["ok"] for n, s in snap["sections"].items() if n not in OFFLINE_SECTIONS):
        raise RuntimeError("assistant snapshot: every network section failed, keeping the previous file")
    prev = await asyncio.to_thread(_load_previous, path)
    carry_forward(snap, prev)
    if snap["missing"] or snap["stale"]:
        log.warning("assistant snapshot: without data: %s; stale: %s", ", ".join(snap["missing"]), ", ".join(snap["stale"]))
    await asyncio.to_thread(write_atomic, path, snap)
    return ok
