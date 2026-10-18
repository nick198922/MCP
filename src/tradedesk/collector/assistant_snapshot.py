"""Assistant snapshot job: one JSON file with everything the dashboard's brief and options tabs show.

Docs: docs/ideas/IDEA-20261018-assistant.md. The job calls our own read-only MCP tools in-process (a throw-away
MCPServer sharing the Runtime, so rate limits, cache and health are the same as for any other call) and stores each
tool's envelope as one section. A failing section lands in that section's ``missing`` and in the top-level ``missing``
list; it never fails the job. Strings from external APIs are kept as plain text (escaping is done at render time).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..analytics.assistant import pick_expiries

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

SCHEMA = 1
SECTION_TIMEOUT_SECONDS = 120.0
VENUE = "binanceusdm"
SPOT_VENUE = "binance"
TICKER_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]


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
            "upbit": ("upbit_notices", {"days": 14, "max_pages": 3}),
        }
        names = list(plan)
        results = await asyncio.gather(*(_call(c, *plan[n]) for n in names))
        sections = dict(zip(names, results, strict=True))

        week, month = pick_expiries(_expiry_rows(sections["term"]))
        dependent: dict[str, tuple[str, dict[str, Any]] | None] = {
            "em_week": ("deribit_expected_move", {"currency": "BTC", "expiry": week}) if week else None,
            "em_month": ("deribit_expected_move", {"currency": "BTC", "expiry": month}) if month else None,
            "chain": ("deribit_option_chain", {"currency": "BTC", "expiry": month, "width_pct": 20}) if month else None,
        }
        dnames = list(dependent)
        calls = [_call(c, *dependent[n]) if dependent[n] else None for n in dnames]
        done = await asyncio.gather(*(x for x in calls if x is not None))
        it = iter(done)
        for n, cl in zip(dnames, calls, strict=True):
            sections[n] = (
                next(it) if cl is not None else _failed("tradedesk", "data_unavailable", "no suitable expiry in the ATM term structure")
            )
    asof = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s in sections.values():
        s["asof_utc"] = asof
    return {
        "schema": SCHEMA,
        "asof_utc": asof,
        "currency": "BTC",
        "expiries": {"week": week, "month": month},
        "sections": sections,
        "missing": [n for n, s in sections.items() if not s["ok"]],
        "stale": [],
    }


MAX_STALE_HOURS = 6.0  # a carried-forward section older than this is dropped (stays in `missing`)


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
    dependent = {"em_week": "week", "em_month": "month", "chain": "month"}
    for name in list(snap["missing"]):
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

    Fails (so the scheduler records it) only when no section has data or no path is configured: an all-empty
    snapshot would overwrite the last useful one.
    """
    s = rt.settings
    path = snapshot_path(s.db_path, s.assistant_snapshot_path)
    if path is None:
        raise RuntimeError("assistant snapshot: set TRADEDESK_ASSISTANT_SNAPSHOT_PATH (the store is in memory)")
    snap = await build_snapshot(rt)
    ok = len(snap["sections"]) - len(snap["missing"])
    if ok == 0:
        raise RuntimeError("assistant snapshot: every section failed, keeping the previous file")
    prev = await asyncio.to_thread(_load_previous, path)
    carry_forward(snap, prev)
    if snap["missing"] or snap["stale"]:
        log.warning("assistant snapshot: without data: %s; stale: %s", ", ".join(snap["missing"]), ", ".join(snap["stale"]))
    await asyncio.to_thread(write_atomic, path, snap)
    return ok
