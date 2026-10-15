"""Hyperliquid public info API (no key needed). Funding is HOURLY on Hyperliquid."""

from __future__ import annotations

import time
from typing import Any

from ..core.envelope import Evidence, Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.hyperliquid.xyz"
FUNDING_INTERVAL_HOURS = 1.0
NAMES_TTL_S = 600.0
NAMES_RETRY_S = 60.0


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
        raise ProviderError("hyperliquid", "backend_error", f"unexpected metaAndAssetCtxs shape: {e}") from e
    rows: list[dict[str, Any]] = []
    for u, c in zip(universe, ctxs, strict=False):
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
        self._names: dict[str, str] | None = None  # plain state: survives aclose()
        self._names_at = 0.0
        self._names_try_at = -NAMES_RETRY_S

    async def _info(self, payload: dict[str, Any], ttl: float) -> Fetched:
        return await self.http.post_json("/info", json=payload, ttl=ttl, tool=f"info/{payload['type']}", args=payload)

    async def asset_contexts(self) -> Fetched:
        f = await self._info({"type": "metaAndAssetCtxs"}, ttl=5)
        return Fetched(parse_asset_contexts(f.data), f.evidence)

    async def _name_table(self) -> dict[str, str] | None:
        """lower -> exact name for the FULL universe (delisted included); cached, refreshed at most every 10 min.

        A failed fetch is not retried for 60 s so a broken endpoint does not add a request per call.
        """
        now = time.monotonic()
        if self._names is not None and now - self._names_at < NAMES_TTL_S:
            return self._names
        if now - self._names_try_at < NAMES_RETRY_S:
            return self._names
        self._names_try_at = now
        try:
            f = await self._info({"type": "metaAndAssetCtxs"}, ttl=5)
            table = {u["name"].lower(): u["name"] for u in f.data[0]["universe"]}
        except (ProviderError, TypeError, KeyError, IndexError, AttributeError):
            return self._names  # stale table (if any) beats nothing
        if table:
            self._names, self._names_at = table, now
        return self._names

    async def lookup_coin(self, coin: str) -> tuple[str, str]:
        """Resolve ``coin`` to the exact HL name (case-sensitive: ``kPEPE``, not ``KPEPE``). Never raises.

        Returns ``(name, status)``: ``exact`` (matched the universe), ``unknown`` (universe loaded, no match;
        name is the upper-cased input, the old behaviour) or ``unavailable`` (universe not loadable; same fallback).
        """
        raw = coin.strip()
        table = await self._name_table()
        if table is None:
            return raw.upper(), "unavailable"
        hit = table.get(raw.lower())
        return (hit, "exact") if hit else (raw.upper(), "unknown")

    async def resolve_coin(self, coin: str) -> str:
        return (await self.lookup_coin(coin))[0]

    async def funding_history(self, coin: str, start_ms: int, end_ms: int | None = None) -> Fetched:
        payload: dict[str, Any] = {"type": "fundingHistory", "coin": await self.resolve_coin(coin), "startTime": start_ms}
        if end_ms:
            payload["endTime"] = end_ms
        return await self._info(payload, ttl=30)

    async def funding_history_range(self, coin: str, start_ms: int, end_ms: int, *, max_pages: int = 40) -> Fetched:
        """Hourly funding from ``start_ms`` to ``end_ms``; the API returns at most 500 rows per call, so walk forward.

        Returns ``{"rows": [...], "complete": bool}``; ``complete`` is False when ``max_pages`` was hit or a
        later page failed (rows so far are kept). A failure on the first page raises.
        """
        coin = await self.resolve_coin(coin)
        rows: dict[int, dict[str, Any]] = {}
        start, pages, latency, cached, complete = start_ms, 0, 0, True, True
        while start < end_ms:
            if pages >= max_pages:
                complete = False
                break
            try:
                f = await self.funding_history(coin, start, end_ms)
            except ProviderError:
                if pages == 0:
                    raise
                complete = False
                break
            pages += 1
            latency += f.evidence.latency_ms or 0
            cached = cached and f.evidence.cached
            batch = [x for x in (f.data or []) if isinstance(x, dict) and x.get("time") is not None]
            for x in batch:
                rows[int(x["time"])] = x
            if len(batch) < 500:
                break
            nxt = max(int(x["time"]) for x in batch) + 1
            if nxt <= start:
                break
            start = nxt
        ev = Evidence(
            provider="hyperliquid",
            tool="info/fundingHistory",
            args={"coin": coin, "start_ms": start_ms, "end_ms": end_ms, "pages": pages},
            latency_ms=latency,
            cached=cached and pages > 0,
        )
        return Fetched({"rows": [rows[k] for k in sorted(rows) if k <= end_ms], "complete": complete}, ev)

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int | None = None) -> Fetched:
        req: dict[str, Any] = {"coin": await self.resolve_coin(coin), "interval": interval, "startTime": start_ms}
        if end_ms:
            req["endTime"] = end_ms
        return await self._info({"type": "candleSnapshot", "req": req}, ttl=15)

    async def l2_book(self, coin: str) -> Fetched:
        return await self._info({"type": "l2Book", "coin": await self.resolve_coin(coin)}, ttl=1)

    async def recent_trades(self, coin: str) -> Fetched:
        return await self._info({"type": "recentTrades", "coin": await self.resolve_coin(coin)}, ttl=2)
