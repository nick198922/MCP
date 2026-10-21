"""Bybit Demo Trading client (api-demo.bybit.com, play money) for the demo trader. The only module that places orders.

ccxt does the V5 signing. Every private call first checks that ccxt is in demo mode and that every API address resolves
exactly to the demo host, so a mistake in configuration cannot reach a real account (the demo key is unknown there
anyway). Keys come from ~/.config/tradedesk/bybit-demo.env (0600, scripts/server/bybit_demo_key.py); error texts go
through ``BybitDemo.safe_error``, which removes the literal key and secret as well as the usual patterns
(``core.health.redact``). Orders carry an ``orderLinkId`` (one per signal), so a retry cannot double a position: Bybit
refuses a second order with the same link id, and an order whose answer was lost is found again by that id.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..core.health import redact

ENV_FILE = Path.home() / ".config" / "tradedesk" / "bybit-demo.env"
DEMO_HOST = "api-demo.bybit.com"
CROSS = "REGULAR_MARGIN"  # Bybit unified account: cross margin
CLOSED_PNL_WINDOW_MS = 7 * 86_400_000 - 60_000  # Bybit closed-pnl: start..end at most 7 days


class DemoOnlyError(RuntimeError):
    pass


def load_keys(path: Path = ENV_FILE) -> tuple[str, str] | None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    env = dict(line.split("=", 1) for line in lines if "=" in line)
    key, secret = env.get("BYBIT_DEMO_API_KEY", "").strip(), env.get("BYBIT_DEMO_API_SECRET", "").strip()
    return (key, secret) if key and secret else None


@dataclass(frozen=True)
class MarketInfo:
    symbol: str
    market_id: str  # venue id, e.g. 'BTCUSDT'
    lot_step: float
    min_qty: float
    tick: float


@dataclass(frozen=True)
class RiskTier:
    """The venue's lowest risk tier of a symbol (public /v5/market/risk-limit)."""

    mmr: float  # maintenance margin rate, decimal
    max_leverage: float
    limit_usd: float  # position value up to which this tier holds


def _make_exchange(key: str, secret: str) -> Any:
    import ccxt.async_support as ccxt  # noqa: PLC0415

    ex = ccxt.bybit({"apiKey": key, "secret": secret, "enableRateLimit": True, "options": {"defaultType": "swap"}})
    ex.enable_demo_trading(True)
    return ex


def is_refusal(e: BaseException) -> bool:
    """True when the venue answered and refused (the order surely does not exist); False for network errors and
    anything else, where the order may have been placed and must be looked up by its link id."""
    import ccxt  # noqa: PLC0415

    return isinstance(e, ccxt.ExchangeError) and not isinstance(e, ccxt.NetworkError)


class BybitDemo:
    def __init__(self, key: str, secret: str, factory: Callable[[str, str], Any] = _make_exchange) -> None:
        self.ex = factory(key, secret)
        self._secrets = tuple(s for s in (key, secret) if len(s) >= 6)
        self._markets: dict[str, Any] = {}
        self._tiers: dict[str, RiskTier] = {}

    def safe_error(self, e: BaseException) -> str:
        text = f"{type(e).__name__}: {e}"
        for s in self._secrets:
            text = text.replace(s, "***")
        return redact(text, limit=500)

    def _demo_only(self) -> None:
        opts = getattr(self.ex, "options", {}) or {}
        api = (getattr(self.ex, "urls", {}) or {}).get("api")
        urls = list(api.values()) if isinstance(api, dict) else [api]
        host = getattr(self.ex, "hostname", "bybit.com")
        hosts = {urlparse(str(u).replace("{hostname}", host)).hostname for u in urls if u}
        if opts.get("enableDemoTrading") is not True or hosts != {DEMO_HOST}:
            raise DemoOnlyError(f"refusing a private call: the client does not point only at {DEMO_HOST}")

    async def aclose(self) -> None:
        await self.ex.close()

    async def _load(self) -> dict[str, Any]:
        if not self._markets:
            self._markets = await self.ex.load_markets()
        return self._markets

    async def market(self, symbol: str) -> MarketInfo:
        m = (await self._load())[symbol]
        lot = float(m["precision"]["amount"])
        min_qty = float(((m.get("limits") or {}).get("amount") or {}).get("min") or lot)
        return MarketInfo(symbol, m["id"], lot, min_qty, float(m["precision"]["price"]))

    async def turnovers(self) -> list[tuple[str, float, str]]:
        """(symbol, 24h turnover in USDT, Bybit symbolType) of every linear perpetual (public data). symbolType is ''
        for crypto, else 'innovation', 'stock', 'ETF', 'commodity', ...; an unknown market gets '?' (left out)."""
        markets = await self._load()
        out = []
        for sym, t in (await self.ex.fetch_tickers(None, {"category": "linear"})).items():
            turnover = (t.get("info") or {}).get("turnover24h") or t.get("quoteVolume") or 0
            m = markets.get(sym)
            kind = str((m.get("info") or {}).get("symbolType") or "") if m else "?"
            out.append((sym, float(turnover), kind))
        return out

    async def risk_tier(self, symbol: str) -> RiskTier:
        """Maintenance margin rate, max leverage and size limit of the symbol's lowest risk tier (public data)."""
        if symbol not in self._tiers:
            m = await self.market(symbol)
            r = await self.ex.publicGetV5MarketRiskLimit({"category": "linear", "symbol": m.market_id})
            rows = ((r or {}).get("result") or {}).get("list") or []
            rows = [x for x in rows if x.get("symbol") == m.market_id] or rows
            if not rows:
                raise ValueError(f"no risk tier for {m.market_id}")
            low = next((x for x in rows if str(x.get("isLowestRisk")) == "1"), min(rows, key=lambda x: int(x.get("id") or 0)))
            self._tiers[symbol] = RiskTier(float(low["maintenanceMargin"]), float(low["maxLeverage"]), float(low["riskLimitValue"]))
        return self._tiers[symbol]

    async def ohlcv(self, symbol: str, timeframe: str, limit: int = 1000) -> list[list[float]]:
        return await self.ex.fetch_ohlcv(symbol, timeframe, limit=limit)

    async def last_price(self, symbol: str) -> float:
        t = await self.ex.fetch_ticker(symbol)
        return float(t["last"])

    async def equity_usd(self) -> float:
        self._demo_only()
        r = await self.ex.privateGetV5AccountWalletBalance({"accountType": "UNIFIED"})
        rows = ((r or {}).get("result") or {}).get("list") or [{}]
        return float(rows[0].get("totalEquity") or 0.0)

    async def positions(self) -> dict[str, dict[str, Any]]:
        """Open positions by ccxt symbol (non-zero size only)."""
        self._demo_only()
        out = {}
        for p in await self.ex.fetch_positions(None, {"category": "linear", "settleCoin": "USDT"}):
            if float(p.get("contracts") or 0) > 0:
                out[p["symbol"]] = p
        return out

    async def margin_mode(self) -> str:
        """The unified account's margin mode: 'REGULAR_MARGIN' (cross), 'ISOLATED_MARGIN' or 'PORTFOLIO_MARGIN'."""
        self._demo_only()
        r = await self.ex.privateGetV5AccountInfo()
        return str(((r or {}).get("result") or {}).get("marginMode") or "")

    async def ensure_cross(self, change: bool = True) -> str:
        """Cross margin for the whole account (owner 21.10); switches only when it is something else. Returns the mode
        found before any change."""
        mode = await self.margin_mode()
        if mode != CROSS and change:
            await self.ex.set_margin_mode("cross", "BTC/USDT:USDT")  # account level on a unified account
        return mode

    async def prepare(self, symbol: str, leverage: int) -> None:
        """``leverage`` on the symbol (both sides); Bybit's 'not modified' answer is fine."""
        self._demo_only()
        try:
            await self.ex.set_leverage(leverage, symbol)
        except Exception as e:  # noqa: BLE001 - Bybit answers an error when the value is already set
            if not any(x in str(e).lower() for x in ("not modified", "110043", "same", "unchanged")):
                raise

    async def place_bracket(self, symbol: str, side: str, qty: float, stop: float, target: float, link_id: str) -> dict[str, Any]:
        """Market entry with the stop-loss and take-profit attached to the position (Bybit tpslMode Full)."""
        self._demo_only()
        return await self.ex.create_order(
            symbol,
            "market",
            "buy" if side == "long" else "sell",
            qty,
            None,
            {
                "orderLinkId": link_id,
                "positionIdx": 0,
                "stopLoss": {"triggerPrice": stop, "type": "market"},
                "takeProfit": {"triggerPrice": target, "type": "market"},
            },
        )

    async def order_by_link(self, market_id: str, link_id: str) -> dict[str, Any] | None:
        """The venue's raw order row for ``link_id`` (open or history), or None if Bybit has no such order."""
        self._demo_only()
        for call in (self.ex.privateGetV5OrderRealtime, self.ex.privateGetV5OrderHistory):
            r = await call({"category": "linear", "symbol": market_id, "orderLinkId": link_id})
            rows = ((r or {}).get("result") or {}).get("list") or []
            if rows:
                return rows[0]
        return None

    async def set_stop(self, symbol: str, stop: float) -> dict[str, Any]:
        """Move the position's stop-loss (Bybit /v5/position/trading-stop, full position), price in the venue's format."""
        self._demo_only()
        m = await self.market(symbol)
        return await self.ex.privatePostV5PositionTradingStop(
            {
                "category": "linear",
                "symbol": m.market_id,
                "stopLoss": self.ex.price_to_precision(symbol, stop),
                "tpslMode": "Full",
                "positionIdx": 0,
            }
        )

    async def close_position(self, symbol: str, side: str, qty: float, link_id: str) -> dict[str, Any]:
        """Reduce-only market order that flattens the position (time exit)."""
        self._demo_only()
        return await self.ex.create_order(
            symbol, "market", "sell" if side == "long" else "buy", qty, None, {"reduceOnly": True, "orderLinkId": link_id}
        )

    async def closed_pnl(self, market_id: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """Bybit /v5/position/closed-pnl rows for one symbol in [start, end] (at most 7 days; fees included)."""
        self._demo_only()
        start_ms = max(start_ms, end_ms - CLOSED_PNL_WINDOW_MS)
        r = await self.ex.privateGetV5PositionClosedPnl(
            {"category": "linear", "symbol": market_id, "startTime": start_ms, "endTime": end_ms, "limit": 50}
        )
        return list(((r or {}).get("result") or {}).get("list") or [])
