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
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

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


@dataclass(frozen=True)
class OptionInstrument:
    """One Bybit option (/v5/market/instruments-info?category=option). One contract = one coin of the underlying."""

    symbol: str  # e.g. 'BTC-30OCT26-90000-C-USDT'
    base: str
    settle: str
    is_call: bool
    strike: float
    expiry_ms: int  # the venue's deliveryTime (08:00 UTC)
    tick: float  # premium tick, USDT
    lot_step: float  # contracts
    min_qty: float


@dataclass(frozen=True)
class OptionQuote:
    """Top of the option's book (/v5/market/tickers?category=option); prices in USDT, IV as decimals (0.45 = 45 %).
    A side without an order reads 0."""

    symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    mark: float
    bid_iv: float
    ask_iv: float
    mark_iv: float
    index: float  # underlying index price
    underlying: float  # the venue's underlying (forward) price for this expiry


OPTION_PAGES = 20  # pagination guard for the option lists (one page is 1000 instruments or 50 orders)


def _num(x: Any) -> float:
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _on_step(x: float, step: float, up: bool = False) -> str:
    """``x`` floored (or ceiled) to ``step``, as the decimal string Bybit expects (no float noise)."""
    d, s = Decimal(str(x)), Decimal(str(step))
    n = (d / s).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return format((n * s).normalize(), "f")


def _option_instrument(x: dict[str, Any]) -> OptionInstrument:
    lot = x.get("lotSizeFilter") or {}
    parts = str(x["symbol"]).split("-")
    return OptionInstrument(
        symbol=str(x["symbol"]),
        base=str(x.get("baseCoin") or parts[0]),
        settle=str(x.get("settleCoin") or ""),
        is_call=str(x.get("optionsType")) == "Call",
        strike=float(parts[2]),
        expiry_ms=int(x["deliveryTime"]),
        tick=float((x.get("priceFilter") or {})["tickSize"]),
        lot_step=float(lot["qtyStep"]),
        min_qty=float(lot.get("minOrderQty") or lot["qtyStep"]),
    )


def _option_quote(t: dict[str, Any]) -> OptionQuote:
    return OptionQuote(
        symbol=str(t["symbol"]),
        bid=_num(t.get("bid1Price")),
        ask=_num(t.get("ask1Price")),
        bid_size=_num(t.get("bid1Size")),
        ask_size=_num(t.get("ask1Size")),
        mark=_num(t.get("markPrice")),
        bid_iv=_num(t.get("bid1Iv")),
        ask_iv=_num(t.get("ask1Iv")),
        mark_iv=_num(t.get("markIv")),
        index=_num(t.get("indexPrice")),
        underlying=_num(t.get("underlyingPrice")),
    )


def _rows(r: Any) -> tuple[list[dict[str, Any]], str]:
    res = (r or {}).get("result") or {}
    return list(res.get("list") or []), unquote(str(res.get("nextPageCursor") or ""))


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

    async def quote(self, symbol: str) -> tuple[float, float]:
        """Best bid and ask (public ticker)."""
        t = await self.ex.fetch_ticker(symbol)
        return float(t["bid"]), float(t["ask"])

    async def place_bracket(
        self, symbol: str, side: str, qty: float, stop: float, target: float | None, link_id: str, price: float | None = None
    ) -> dict[str, Any]:
        """Entry with the stop-loss attached to the position (Bybit tpslMode Full; stop-market). ``price`` given: a
        post-only limit (maker; the venue cancels it rather than let it take). ``target`` None: no take-profit attached
        (the demo trader rests a reduce-only limit there once the entry is complete)."""
        self._demo_only()
        params: dict[str, Any] = {"orderLinkId": link_id, "positionIdx": 0, "stopLoss": {"triggerPrice": stop, "type": "market"}}
        if target is not None:
            params["takeProfit"] = {"triggerPrice": target, "type": "market"}
        if price is not None:
            params["postOnly"] = True
        return await self.ex.create_order(
            symbol, "limit" if price is not None else "market", "buy" if side == "long" else "sell", qty, price, params
        )

    async def place_reduce_limit(self, symbol: str, side: str, qty: float, price: float, link_id: str, post_only: bool) -> dict[str, Any]:
        """Reduce-only limit that closes (part of) the position ``side``: the resting target or a calm exit."""
        self._demo_only()
        params: dict[str, Any] = {"reduceOnly": True, "orderLinkId": link_id, "positionIdx": 0}
        if post_only:
            params["postOnly"] = True
        return await self.ex.create_order(symbol, "limit", "sell" if side == "long" else "buy", qty, price, params)

    async def amend(self, symbol: str, link_id: str, price: float, qty: float | None = None) -> dict[str, Any]:
        """Re-quote an open order (Bybit /v5/order/amend by orderLinkId; ``qty`` is the total, filled part included)."""
        self._demo_only()
        m = await self.market(symbol)
        req = {"category": "linear", "symbol": m.market_id, "orderLinkId": link_id, "price": self.ex.price_to_precision(symbol, price)}
        if qty is not None:
            req["qty"] = self.ex.amount_to_precision(symbol, qty)
        return await self.ex.privatePostV5OrderAmend(req)

    async def cancel(self, symbol: str, link_id: str) -> bool:
        """Cancel an open order by orderLinkId; False when the venue no longer has it open (filled or cancelled)."""
        self._demo_only()
        m = await self.market(symbol)
        try:
            await self.ex.privatePostV5OrderCancel({"category": "linear", "symbol": m.market_id, "orderLinkId": link_id})
        except Exception as e:  # noqa: BLE001 - 110001: order does not exist or is too late to cancel
            if "110001" in str(e) or "not exist" in str(e).lower():
                return False
            raise
        return True

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

    async def set_take_profit(self, symbol: str, target: float) -> dict[str, Any]:
        """Market take-profit on the whole position (the fallback when the resting target limit is refused)."""
        self._demo_only()
        m = await self.market(symbol)
        return await self.ex.privatePostV5PositionTradingStop(
            {
                "category": "linear",
                "symbol": m.market_id,
                "takeProfit": self.ex.price_to_precision(symbol, target),
                "tpslMode": "Full",
                "positionIdx": 0,
            }
        )

    async def close_position(self, symbol: str, side: str, qty: float, link_id: str) -> dict[str, Any]:
        """Reduce-only market order that flattens the position (time exit when the limit did not fill or near the stop)."""
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

    # ------------------------------------------------------------ options (owner 23.10, slice 1)
    # Raw V5 calls with category 'option' on Bybit symbols ('BTC-30OCT26-90000-C-USDT'): ccxt's option markets are not
    # loaded (defaultType swap). Every call, public ones included, first checks the demo host. Only long options: the
    # one opening order is a post-only limit buy; the close is a reduce-only limit sell.

    async def _pages(self, call: Callable[[dict[str, Any]], Any], req: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(OPTION_PAGES):
            rows, cursor = _rows(await call({**req, "cursor": cursor} if cursor else req))
            out += rows
            if not cursor or not rows:
                break
        return out

    async def option_instruments(self, base: str) -> list[OptionInstrument]:
        """Tradable options on ``base`` (BTC, ETH): strike, call/put, expiry, tick and lot (public data)."""
        self._demo_only()
        rows = await self._pages(self.ex.publicGetV5MarketInstrumentsInfo, {"category": "option", "baseCoin": base, "limit": 1000})
        return [_option_instrument(x) for x in rows if x.get("status") == "Trading"]

    async def option_quotes(self, base: str) -> dict[str, OptionQuote]:
        """Book tops, mark and IV of every option on ``base``, by symbol (public data)."""
        self._demo_only()
        r = await self.ex.publicGetV5MarketTickers({"category": "option", "baseCoin": base})
        return {q.symbol: q for q in (_option_quote(t) for t in _rows(r)[0])}

    async def option_quote(self, symbol: str) -> OptionQuote:
        """Book top, mark and IV of one option (public data)."""
        self._demo_only()
        rows = _rows(await self.ex.publicGetV5MarketTickers({"category": "option", "symbol": symbol}))[0]
        row = next((t for t in rows if t.get("symbol") == symbol), None)
        if row is None:
            raise ValueError(f"no ticker for {symbol}")
        return _option_quote(row)

    async def place_option_buy(self, inst: OptionInstrument, qty: float, price: float, link_id: str) -> dict[str, Any]:
        """Post-only limit buy to open (maker; the venue cancels it rather than let it take). ``price`` is floored to
        the tick and ``qty`` to the lot step, so the order never pays or buys more than planned."""
        self._demo_only()
        q, px = _on_step(qty, inst.lot_step), _on_step(price, inst.tick)
        if float(q) < inst.min_qty or float(px) <= 0:
            raise ValueError(f"option order below the venue's minimum: qty {q}, price {px}")
        req = {
            "category": "option",
            "symbol": inst.symbol,
            "side": "Buy",
            "orderType": "Limit",
            "qty": q,
            "price": px,
            "timeInForce": "PostOnly",
            "orderLinkId": link_id,
        }
        return await self.ex.privatePostV5OrderCreate(req)

    async def amend_option(self, inst: OptionInstrument, link_id: str, price: float, qty: float | None = None) -> dict[str, Any]:
        """Re-quote an open option order by orderLinkId (``qty`` is the total, filled part included)."""
        self._demo_only()
        req = {"category": "option", "symbol": inst.symbol, "orderLinkId": link_id, "price": _on_step(price, inst.tick)}
        if qty is not None:
            req["qty"] = _on_step(qty, inst.lot_step)
        return await self.ex.privatePostV5OrderAmend(req)

    async def cancel_option(self, symbol: str, link_id: str) -> bool:
        """Cancel an open option order by orderLinkId; False when the venue no longer has it open."""
        self._demo_only()
        try:
            await self.ex.privatePostV5OrderCancel({"category": "option", "symbol": symbol, "orderLinkId": link_id})
        except Exception as e:  # noqa: BLE001 - 110001: order does not exist or is too late to cancel
            if "110001" in str(e) or "not exist" in str(e).lower():
                return False
            raise
        return True

    async def option_open_orders(self, base: str) -> list[dict[str, Any]]:
        """The venue's raw rows of every open option order on ``base``."""
        self._demo_only()
        return await self._pages(self.ex.privateGetV5OrderRealtime, {"category": "option", "baseCoin": base, "limit": 50})

    async def option_order_by_link(self, symbol: str, link_id: str) -> dict[str, Any] | None:
        """The venue's raw option order row for ``link_id`` (open or history), or None if Bybit has no such order."""
        self._demo_only()
        for call in (self.ex.privateGetV5OrderRealtime, self.ex.privateGetV5OrderHistory):
            rows = _rows(await call({"category": "option", "symbol": symbol, "orderLinkId": link_id}))[0]
            if rows:
                return rows[0]
        return None

    async def option_positions(self, bases: tuple[str, ...] = ("BTC", "ETH")) -> dict[str, dict[str, Any]]:
        """Open option positions by Bybit symbol (non-zero size only), raw rows."""
        self._demo_only()
        out = {}
        for base in bases:
            for p in await self._pages(self.ex.privateGetV5PositionList, {"category": "option", "baseCoin": base, "limit": 200}):
                if _num(p.get("size")) > 0:
                    out[str(p["symbol"])] = p
        return out

    async def option_executions(self, symbol: str, link_id: str) -> list[dict[str, Any]]:
        """The venue's raw fills of one option order (/v5/execution/list by orderLinkId, last 7 days): execQty,
        execPrice, execFee (the fee actually charged, USDT), execType."""
        self._demo_only()
        return await self._pages(
            self.ex.privateGetV5ExecutionList, {"category": "option", "symbol": symbol, "orderLinkId": link_id, "limit": 100}
        )

    async def close_option(self, inst: OptionInstrument, qty: float, price: float, link_id: str, post_only: bool = True) -> dict[str, Any]:
        """Reduce-only limit sell that closes (part of) a long option; never opens a short. ``price`` is ceiled to the
        tick (a sell never goes cheaper than asked), ``qty`` floored to the lot step."""
        self._demo_only()
        q, px = _on_step(qty, inst.lot_step), _on_step(price, inst.tick, up=True)
        if float(q) <= 0 or float(px) <= 0:
            raise ValueError(f"option close below the venue's minimum: qty {q}, price {px}")
        req = {
            "category": "option",
            "symbol": inst.symbol,
            "side": "Sell",
            "orderType": "Limit",
            "qty": q,
            "price": px,
            "reduceOnly": True,
            "timeInForce": "PostOnly" if post_only else "GTC",
            "orderLinkId": link_id,
        }
        return await self.ex.privatePostV5OrderCreate(req)
