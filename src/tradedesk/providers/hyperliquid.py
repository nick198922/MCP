"""Hyperliquid public info API (no key needed). Funding is HOURLY on Hyperliquid."""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.hyperliquid.xyz"
FUNDING_INTERVAL_HOURS = 1.0


def _f(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def parse_asset_contexts(raw: Any) -> list[dict[str, Any]]:
    """metaAndAssetCtxs -> flat per-coin rows."""
    try:
        meta, ctxs = raw[0], raw[1]
        universe = meta["universe"]
    except (TypeError, KeyError, IndexError) as e:
        raise ProviderError("hyperliquid", "backend_error", f"unexpected metaAndAssetCtxs shape: {e}")
    rows: list[dict[str, Any]] = []
    for u, c in zip(universe, ctxs):
        if u.get("isDelisted"):
            continue
        mark, oi, fund = _f(c.get("markPx")), _f(c.get("openInterest")), _f(c.get("funding"))
        prev = _f(c.get("prevDayPx"))
        rows.append(
            {
                "coin": u["name"],
                "max_leverage": u.get("maxLeverage"),
                "mark_px": mark,
                "oracle_px": _f(c.get("oraclePx")),
                "premium": _f(c.get("premium")),
                "funding_1h": fund,
                "funding_annualized": None if fund is None else fund * 8760.0 / FUNDING_INTERVAL_HOURS,
                "open_interest": oi,
                "open_interest_usd": oi * mark if oi is not None and mark is not None else None,
                "day_volume_usd": _f(c.get("dayNtlVlm")),
                "change_24h_pct": (mark / prev - 1.0) * 100.0 if mark and prev else None,
            }
        )
    return rows


class Hyperliquid:
    name = "hyperliquid"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def _info(self, payload: dict[str, Any], ttl: float) -> Fetched:
        return await self.http.post_json("/info", json=payload, ttl=ttl, tool=f"info/{payload['type']}", args=payload)

    async def asset_contexts(self) -> Fetched:
        f = await self._info({"type": "metaAndAssetCtxs"}, ttl=5)
        return Fetched(parse_asset_contexts(f.data), f.evidence)

    async def funding_history(self, coin: str, start_ms: int, end_ms: int | None = None) -> Fetched:
        payload: dict[str, Any] = {"type": "fundingHistory", "coin": coin.upper(), "startTime": start_ms}
        if end_ms:
            payload["endTime"] = end_ms
        return await self._info(payload, ttl=30)

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int | None = None) -> Fetched:
        req: dict[str, Any] = {"coin": coin.upper(), "interval": interval, "startTime": start_ms}
        if end_ms:
            req["endTime"] = end_ms
        return await self._info({"type": "candleSnapshot", "req": req}, ttl=15)

    async def l2_book(self, coin: str) -> Fetched:
        return await self._info({"type": "l2Book", "coin": coin.upper()}, ttl=1)
