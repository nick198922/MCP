"""Hyperliquid public info API (no key needed). Funding is HOURLY on Hyperliquid."""

from __future__ import annotations

import time
from typing import Any

from ..core.envelope import Evidence, Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.hyperliquid.xyz"
# Leaderboard of every account (one ~40 MB JSON): PnL, ROI and volume by day / week / month / all time.
STATS_URL = "https://stats-data.hyperliquid.xyz"
FUNDING_INTERVAL_HOURS = 1.0
NAMES_TTL_S = 600.0
NAMES_RETRY_S = 60.0


def _f(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def parse_asset_contexts(raw: Any, include_delisted: bool = False) -> list[dict[str, Any]]:
    """metaAndAssetCtxs -> flat per-coin rows. Delisted perps are dropped unless ``include_delisted`` (then their rows
    carry ``delisted: True``; the others ``delisted: False``)."""
    try:
        meta, ctxs = raw[0], raw[1]
        universe = meta["universe"]
    except (TypeError, KeyError, IndexError) as e:
        raise ProviderError("hyperliquid", "backend_error", f"unexpected metaAndAssetCtxs shape: {e}") from e
    rows: list[dict[str, Any]] = []
    for u, c in zip(universe, ctxs, strict=False):
        if u.get("isDelisted") and not include_delisted:
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
                **({"delisted": bool(u.get("isDelisted"))} if include_delisted else {}),
            }
        )
    return rows


def parse_oi_cap(raw: Any) -> list[str]:
    """perpsAtOpenInterestCap -> the tickers whose open interest is at the protocol cap (a JSON array of strings).

    Any other shape is a ``backend_error``: an empty list is a valid answer (no perp at its cap), a reshaped one is not."""
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ProviderError("hyperliquid", "backend_error", f"unexpected perpsAtOpenInterestCap shape: {str(raw)[:80]}")
    return list(raw)


WINDOWS = {"day": "day", "week": "week", "month": "month", "allTime": "all"}


def parse_leaderboard(raw: Any, min_account_usd: float = 0.0) -> list[dict[str, Any]]:
    """Accounts from the stats leaderboard with ``account_value >= min_account_usd``: ``account``, ``name``,
    ``account_value`` and ``pnl_<day|week|month|all>``, ``roi_<…>``, ``vlm_<…>`` (USD; ROI as a decimal). Bad rows are skipped."""
    rows = raw.get("leaderboardRows") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ProviderError("hyperliquid", "backend_error", "unexpected leaderboard shape")
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("ethAddress"):
            continue
        value = _f(r.get("accountValue"))
        if value is None or value < min_account_usd:
            continue
        row: dict[str, Any] = {"account": str(r["ethAddress"]).lower(), "name": r.get("displayName"), "account_value": value}
        perf = r.get("windowPerformances") or []
        for item in perf:
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] in WINDOWS and isinstance(item[1], dict):
                w = WINDOWS[item[0]]
                row[f"pnl_{w}"], row[f"roi_{w}"], row[f"vlm_{w}"] = _f(item[1].get("pnl")), _f(item[1].get("roi")), _f(item[1].get("vlm"))
        out.append(row)
    return out


def parse_positions(state: Any) -> list[dict[str, Any]]:
    """Open perp positions from ``clearinghouseState``: ``market`` (coin), ``side``, ``size`` (coins, unsigned),
    ``entry_px``, ``value_usd``, ``upnl``, ``leverage``, ``liq_px``."""
    if not isinstance(state, dict):
        raise ProviderError("hyperliquid", "backend_error", "unexpected clearinghouseState shape")
    out: list[dict[str, Any]] = []
    for ap in state.get("assetPositions") or []:
        pos = ap.get("position") if isinstance(ap, dict) else None
        if not isinstance(pos, dict):
            continue
        szi = _f(pos.get("szi"))
        if not szi:
            continue
        lev = pos.get("leverage")
        out.append(
            {
                "market": str(pos.get("coin")),
                "side": "long" if szi > 0 else "short",
                "size": abs(szi),
                "entry_px": _f(pos.get("entryPx")),
                "value_usd": _f(pos.get("positionValue")),
                "upnl": _f(pos.get("unrealizedPnl")),
                "leverage": _f(lev.get("value")) if isinstance(lev, dict) else None,
                "liq_px": _f(pos.get("liquidationPx")),
            }
        )
    return out


class Hyperliquid:
    name = "hyperliquid"

    def __init__(self, http: Http, stats: Http | None = None) -> None:
        self.http = http
        self.stats = stats  # stats-data host (leaderboard)
        self._names: dict[str, str] | None = None  # plain state: survives aclose()
        self._names_at = 0.0
        self._names_try_at = -NAMES_RETRY_S

    async def _info(self, payload: dict[str, Any], ttl: float) -> Fetched:
        return await self.http.post_json("/info", json=payload, ttl=ttl, tool=f"info/{payload['type']}", args=payload)

    async def aclose(self) -> None:
        await self.http.aclose()
        if self.stats is not None:
            await self.stats.aclose()

    async def leaderboard(self) -> Fetched:
        """Every account's PnL / ROI / volume by window (raw rows; parse with ``parse_leaderboard``). ~40 MB, not cached."""
        if self.stats is None:
            raise ProviderError(self.name, "config", "stats client not configured")
        return await self.stats.get_json("/Mainnet/leaderboard", tool="leaderboard", args={})

    async def clearinghouse_state(self, user: str) -> Fetched:
        """One account's margin summary and open perp positions (public for every address)."""
        return await self._info({"type": "clearinghouseState", "user": user}, ttl=30)

    async def asset_contexts(self, include_delisted: bool = False) -> Fetched:
        f = await self._info({"type": "metaAndAssetCtxs"}, ttl=5)
        return Fetched(parse_asset_contexts(f.data, include_delisted), f.evidence)

    async def perps_at_oi_cap(self) -> Fetched:
        """Tickers of the main-dex perps whose open interest is at the cap right now (current state only, no history)."""
        f = await self._info({"type": "perpsAtOpenInterestCap"}, ttl=5)
        return Fetched(parse_oi_cap(f.data), f.evidence)

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
        name is the upper-cased input, the old behaviour), ``unavailable`` (universe not loadable; same fallback) or
        ``dex`` (a builder-deployed perp ``<dex>:<NAME>`` such as ``xyz:SP500``, outside the main universe: the dex
        prefix is lower-case on HL, so upper-casing the whole name would make the API answer HTTP 500).
        """
        raw = coin.strip()
        if ":" in raw:
            dex, _, name = raw.partition(":")
            return f"{dex.lower()}:{name.upper()}", "dex"
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
