"""Bybit Demo Trading client (api-demo.bybit.com, play money) for the demo trader. The only module that places orders.

ccxt does the V5 signing. Every private call first checks that the client points at the demo host, so a mistake in
configuration cannot reach a real account (the real host is api.bybit.com and the demo key is unknown there anyway).
Keys come from ~/.config/tradedesk/bybit-demo.env (0600, scripts/server/bybit_demo_key.py) and are never logged: errors
are passed through ``core.health.redact``. Orders carry an ``orderLinkId`` (one per signal), so a retry cannot double a
position: Bybit refuses a second order with the same link id.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.health import redact

ENV_FILE = Path.home() / ".config" / "tradedesk" / "bybit-demo.env"
DEMO_HOST = "api-demo.bybit.com"


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


def _make_exchange(key: str, secret: str) -> Any:
    import ccxt.async_support as ccxt  # noqa: PLC0415

    ex = ccxt.bybit({"apiKey": key, "secret": secret, "enableRateLimit": True, "options": {"defaultType": "swap"}})
    ex.enable_demo_trading(True)
    return ex


class BybitDemo:
    def __init__(self, key: str, secret: str, factory: Callable[[str, str], Any] = _make_exchange) -> None:
        self.ex = factory(key, secret)
        self._markets: dict[str, Any] = {}

    def _demo_only(self) -> None:
        api = self.ex.urls.get("api")
        urls = list(api.values()) if isinstance(api, dict) else [api]
        if not urls or not all(DEMO_HOST.split(".")[0] in str(u) for u in urls):
            raise DemoOnlyError(f"refusing a private call: the client does not point at {DEMO_HOST}")

    async def aclose(self) -> None:
        await self.ex.close()

    async def market(self, symbol: str) -> MarketInfo:
        if not self._markets:
            self._markets = await self.ex.load_markets()
        m = self._markets[symbol]
        lot = float(m["precision"]["amount"])
        return MarketInfo(
            symbol, m["id"], lot, float((m.get("limits") or {}).get("amount", {}).get("min") or lot), float(m["precision"]["price"])
        )

    async def ohlcv(self, symbol: str, timeframe: str, limit: int = 1000) -> list[list[float]]:
        return await self.ex.fetch_ohlcv(symbol, timeframe, limit=limit)

    async def last_price(self, symbol: str) -> float:
        t = await self.ex.fetch_ticker(symbol)
        return float(t["last"])

    async def equity_usd(self) -> float:
        self._demo_only()
        bal = await self.ex.fetch_balance({"type": "unified"})
        info = ((bal.get("info") or {}).get("result") or {}).get("list") or [{}]
        eq = info[0].get("totalEquity")
        return float(eq) if eq not in (None, "") else float((bal.get("total") or {}).get("USDT") or 0.0)

    async def positions(self) -> dict[str, dict[str, Any]]:
        """Open positions by ccxt symbol (non-zero size only)."""
        self._demo_only()
        out = {}
        for p in await self.ex.fetch_positions(None, {"category": "linear", "settleCoin": "USDT"}):
            if float(p.get("contracts") or 0) > 0:
                out[p["symbol"]] = p
        return out

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

    async def close_position(self, symbol: str, side: str, qty: float, link_id: str) -> dict[str, Any]:
        """Reduce-only market order that flattens the position (time exit)."""
        self._demo_only()
        return await self.ex.create_order(
            symbol, "market", "sell" if side == "long" else "buy", qty, None, {"reduceOnly": True, "orderLinkId": link_id}
        )

    async def closed_pnl(self, market_id: str, since_ms: int) -> list[dict[str, Any]]:
        """Bybit /v5/position/closed-pnl rows for one symbol since ``since_ms`` (fees included in closedPnl)."""
        self._demo_only()
        r = await self.ex.privateGetV5PositionClosedPnl({"category": "linear", "symbol": market_id, "startTime": since_ms, "limit": 50})
        return list(((r or {}).get("result") or {}).get("list") or [])


def safe_error(e: BaseException) -> str:
    return redact(f"{type(e).__name__}: {e}", limit=500)
