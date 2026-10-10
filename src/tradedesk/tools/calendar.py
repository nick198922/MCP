from __future__ import annotations

import asyncio
import json
from importlib import resources
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.calendar import classify_expiry, filter_macro, funding_schedule
from ..analytics.instruments import parse_option_name
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY


def _load_macro() -> dict[str, Any]:
    with resources.files("tradedesk.data").joinpath("macro_events.json").open("r", encoding="utf-8") as f:
        return json.load(f)


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Upcoming calendar: Deribit option expiries classified daily / weekly / monthly / quarterly "
            "(all 08:00 UTC), funding payout timestamps across venues (Binance/Bybit/OKX every 8h, "
            "Hyperliquid hourly) within hours_ahead, and macro events (FOMC, CPI, NFP) from a static "
            "list that is updated by hand. Useful for a pre-trade / pre-market scan — not a forecast."
        ),
    )
    async def calendar_expiries(
        currencies: Annotated[list[str] | None, Field(description="Deribit option currencies to query (default: BTC, ETH).")] = None,
        horizon_days: Annotated[int, Field(ge=1, le=180, description="Expiry and macro horizon in days.")] = 30,
        funding_hours_ahead: Annotated[int, Field(ge=1, le=72, description="How far ahead to project funding payout times.")] = 24,
    ) -> dict[str, Any]:
        b = Builder()
        now = rt.now()
        currencies = [c.upper() for c in (currencies or ["BTC", "ETH"])]
        horizon_end = now.timestamp() + horizon_days * 86400

        async def one(cur: str) -> dict[str, Any] | None:
            rows = await b.guard(f"expiries:{cur}", "deribit", rt.deribit.book_summary(cur, "option"))
            if not rows:
                return None
            seen: dict[str, int] = {}
            for r in rows:
                p = parse_option_name(r.get("instrument_name", ""))
                if p is None or p.expiry.timestamp() > horizon_end or p.expiry < now:
                    continue
                seen[p.expiry.isoformat()] = seen.get(p.expiry.isoformat(), 0) + 1
            out = []
            for iso, count in sorted(seen.items()):
                from datetime import datetime  # noqa: PLC0415

                exp = datetime.fromisoformat(iso)
                out.append(
                    {
                        "currency": cur,
                        "expiry": iso,
                        "days_to_expiry": round((exp - now).total_seconds() / 86400, 3),
                        "kind": classify_expiry(exp, now),
                        "instruments_count": count,
                    }
                )
            return {"currency": cur, "expiries": out}

        results = await asyncio.gather(*[one(c) for c in currencies])
        exp_by_cur = {r["currency"]: r["expiries"] for r in results if r}

        try:
            macro = filter_macro(_load_macro().get("events", []), now, horizon_days)
        except (FileNotFoundError, ValueError, OSError) as e:
            b.miss("macro", "tradedesk", "data_unavailable", str(e))
            macro = []

        data = {
            "now": now.isoformat(),
            "horizon_days": horizon_days,
            "deribit_expiries": exp_by_cur,
            "funding_schedule": funding_schedule(now, funding_hours_ahead),
            "macro_events": macro,
        }
        if not exp_by_cur and not macro:
            return b.build(None, ok=False)
        return b.build(data)
