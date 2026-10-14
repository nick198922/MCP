"""Deribit public REST API (no key needed): options, dated futures, perpetual."""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://www.deribit.com/api/v2"
# Archive host serving the full public trade history (incl. long-expired instruments). Same JSON-RPC
# shape as BASE_URL; used by research scripts only (see docs/verification/2026-10-14-put-skew.md).
HISTORY_URL = "https://history.deribit.com/api/v2"


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

    async def last_trades_by_currency(self, currency: str, kind: str, count: int, include_old: bool) -> Fetched:
        """Recent trades across all instruments of a kind. `block_trade_id` is set for block prints.

        Each trade carries `instrument_name`, `direction` (buy|sell, taker side), `amount` (contracts
        or underlying units depending on instrument), `price` (native: BTC for inverse options,
        USD for linear futures), `mark_price`, `index_price`, `iv`, `timestamp`.
        """
        f = await self._public(
            "get_last_trades_by_currency",
            {"currency": currency.upper(), "kind": kind, "count": count, "include_old": include_old},
            ttl=5,
        )
        trades = (f.data or {}).get("trades") or []
        return Fetched(trades, f.evidence)

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

    async def last_trades_by_currency_and_time(
        self,
        currency: str,
        kind: str,
        start_ms: int,
        end_ms: int,
        *,
        count: int = 1000,
        sorting: str = "asc",
    ) -> Fetched:
        """Trades of all instruments of a kind within [start_ms, end_ms] (ms since epoch), at most `count` (<=1000).

        Returns ``{"trades": [...], "has_more": bool}``. Paginate by moving ``start_ms`` to the last
        trade's timestamp (sorting="asc") and de-duplicating by ``trade_id``. Option trades carry
        ``iv`` in percent (55.1 = 55.1 %), ``price``/``mark_price`` in BTC, ``index_price`` in USD.
        On ``HISTORY_URL`` this also covers expired instruments.
        """
        params = {
            "currency": currency.upper(),
            "kind": kind,
            "start_timestamp": int(start_ms),
            "end_timestamp": int(end_ms),
            "count": int(count),
            "sorting": sorting,
        }
        f = await self._public("get_last_trades_by_currency_and_time", params, ttl=0)
        data = f.data or {}
        return Fetched({"trades": data.get("trades") or [], "has_more": bool(data.get("has_more"))}, f.evidence)

    async def last_trades_by_instrument_and_time(
        self,
        instrument_name: str,
        start_ms: int,
        end_ms: int,
        *,
        count: int = 1000,
        sorting: str = "asc",
    ) -> Fetched:
        """Trades of one instrument within [start_ms, end_ms]; same shape as `last_trades_by_currency_and_time`."""
        params = {
            "instrument_name": instrument_name,
            "start_timestamp": int(start_ms),
            "end_timestamp": int(end_ms),
            "count": int(count),
            "sorting": sorting,
        }
        f = await self._public("get_last_trades_by_instrument_and_time", params, ttl=0)
        data = f.data or {}
        return Fetched({"trades": data.get("trades") or [], "has_more": bool(data.get("has_more"))}, f.evidence)

    async def delivery_prices(self, index_name: str, *, offset: int = 0, count: int = 1000) -> Fetched:
        """Historical delivery (settlement) prices, newest first: ``{"data": [{"date": "YYYY-MM-DD",
        "delivery_price": float}], "records_total": int}``. ``index_name`` e.g. ``"btc_usd"``; USD units."""
        return await self._public("get_delivery_prices", {"index_name": index_name, "offset": int(offset), "count": int(count)}, ttl=60)

    async def funding_rate_history(self, instrument_name: str, start_ms: int, end_ms: int) -> Fetched:
        """Hourly perpetual funding records (www host only; at most 744 per call, so page by <=31 days).

        Each record: ``timestamp`` (ms, end of hour), ``interest_1h`` (funding accrued in that hour,
        decimal of notional; longs pay when positive), ``interest_8h``, ``index_price``, ``prev_index_price``.
        """
        return await self._public(
            "get_funding_rate_history",
            {"instrument_name": instrument_name, "start_timestamp": int(start_ms), "end_timestamp": int(end_ms)},
            ttl=0,
        )
