"""Deribit public REST API (no key needed): options, dated futures, perpetual."""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://www.deribit.com/api/v2"


class Deribit:
    name = "deribit"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def _public(self, method: str, params: dict[str, Any], ttl: float) -> Fetched:
        f = await self.http.get_json(f"/public/{method}", params=params, ttl=ttl, tool=f"public/{method}", args=params)
        body = f.data
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            msg = f"{err.get('code')}: {err.get('message')}"
            # 13020 = instrument_not_found, -32602 = invalid params
            reason = "data_unavailable" if err.get("code") in (13020, -32602) else "backend_error"
            raise ProviderError(self.name, reason, msg)
        if not isinstance(body, dict) or "result" not in body:
            raise ProviderError(self.name, "backend_error", "unexpected response shape")
        return Fetched(body["result"], f.evidence)

    async def book_summary(self, currency: str, kind: str) -> Fetched:
        """All instruments of a kind ('option' | 'future') in one call: mark/IV/OI/volume."""
        return await self._public("get_book_summary_by_currency", {"currency": currency.upper(), "kind": kind}, ttl=10)

    async def ticker(self, instrument_name: str) -> Fetched:
        """Single instrument incl. exchange-computed greeks, bid/ask IV (used to cross-check our maths)."""
        return await self._public("ticker", {"instrument_name": instrument_name}, ttl=3)

    async def volatility_index_data(self, currency: str, start_ms: int, end_ms: int, resolution: int) -> Fetched:
        """Deribit's DVOL time series. resolution must be one of 60, 3600, 43200, 86400 seconds.

        Returns ``{"data": [[ts_ms, open, high, low, close], ...], "continuation": ...}`` where close
        is the DVOL value in annualised-percent units (e.g. 45.3 means 45.3%).
        """
        return await self._public(
            "get_volatility_index_data",
            {"currency": currency.upper(), "start_timestamp": start_ms, "end_timestamp": end_ms, "resolution": resolution},
            ttl=30,
        )
