"""Public liquidation streams: Binance / Bybit / OKX.

Binance USD-M (`!forceOrder@arr`) covers all perps and needs no subscribe, but
sends at most the latest force-close per symbol each second (Binance docs), so
cascades are undercounted. Bybit v5 needs an explicit ``subscribe`` for each
``allLiquidation.<symbol>`` channel. OKX v5 streams every SWAP liquidation
through one ``liquidation-orders`` channel (filtered by ``instType``).

Payload shape (Binance docs, 2024-11):

.. code-block:: json

    {
      "e": "forceOrder",
      "E": 1568014460893,
      "o": {
        "s":  "BTCUSDT",
        "S":  "SELL",         // taker side; SELL = long got liquidated
        "o":  "LIMIT",
        "f":  "IOC",
        "q":  "0.014",        // original qty
        "p":  "9910",          // original limit price
        "ap": "9910",          // average fill price
        "X":  "FILLED",
        "l":  "0.014",
        "z":  "0.014",
        "T":  1568014460893
      }
    }

Bybit / OKX will land as follow-ups — each uses a per-symbol subscribe step
with a different envelope.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

# USD-M market streams moved under /market/ (checked 2026-10-17): the old /ws/ path still accepts the connection but
# sends nothing, not even btcusdt@aggTrade.
BINANCE_WS_URL = "wss://fstream.binance.com/market/ws/!forceOrder@arr"
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"


def parse_binance_force_order(event: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one `!forceOrder@arr` payload into our internal shape.

    Returns ``None`` for anything that is not a filled forceOrder with the
    fields we need (symbol, side, qty, price, ts).
    """
    if event.get("e") != "forceOrder":
        return None
    o = event.get("o") or {}
    try:
        symbol = o["s"]
        side_raw = o["S"]
        qty = float(o.get("l") or o.get("z") or o.get("q"))
        price = float(o.get("ap") or o.get("p"))
        ts_ms = int(o.get("T") or event.get("E"))
    except (KeyError, TypeError, ValueError):
        return None
    if qty <= 0 or price <= 0 or ts_ms <= 0:
        return None
    side = "buy" if side_raw == "BUY" else "sell" if side_raw == "SELL" else None
    if side is None:
        return None
    return {
        "ts": ts_ms // 1000,
        "venue": "binanceusdm",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "notional_usd": qty * price,
    }


def parse_bybit_liquidation(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate a Bybit v5 ``allLiquidation.<symbol>`` message.

    Payload (2024-11):

    .. code-block:: json

        {"topic":"allLiquidation.BTCUSDT","type":"snapshot","ts":...,
         "data":[{"T":...,"s":"BTCUSDT","S":"Sell","v":"0.03","p":"40000"}]}

    ``S`` follows the same convention as Binance (``Sell`` = long got
    liquidated). Returns a list because the ``data`` array may hold several
    rows in one snapshot.
    """
    topic = event.get("topic") or ""
    if not isinstance(topic, str) or not topic.startswith("allLiquidation."):
        return []
    rows: list[dict[str, Any]] = []
    for item in event.get("data") or []:
        try:
            symbol = item["s"]
            side_raw = item["S"]
            qty = float(item["v"])
            price = float(item["p"])
            ts_ms = int(item["T"])
        except (KeyError, TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0 or ts_ms <= 0:
            continue
        side = "buy" if side_raw.lower() == "buy" else "sell" if side_raw.lower() == "sell" else None
        if side is None:
            continue
        rows.append(
            {
                "ts": ts_ms // 1000,
                "venue": "bybit",
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "notional_usd": qty * price,
            }
        )
    return rows


def parse_okx_liquidation(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate an OKX v5 ``liquidation-orders`` message.

    Payload (2024-11):

    .. code-block:: json

        {"arg":{"channel":"liquidation-orders","instType":"SWAP"},
         "data":[{"instId":"BTC-USDT-SWAP","details":[
              {"side":"sell","sz":"0.5","bkPx":"40000",
               "posSide":"long","ts":"1703485267706"}]}]}

    ``side`` is the order side that executed the liquidation; ``posSide``
    tells you which position got hit. Our convention still maps directly:
    ``sell`` = long got liquidated, ``buy`` = short got liquidated.
    """
    arg = event.get("arg") or {}
    if arg.get("channel") != "liquidation-orders":
        return []
    rows: list[dict[str, Any]] = []
    for inst in event.get("data") or []:
        inst_id = inst.get("instId") or "?"
        for d in inst.get("details") or []:
            try:
                qty = float(d["sz"])
                price = float(d["bkPx"])
                ts_ms = int(d["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if qty <= 0 or price <= 0 or ts_ms <= 0:
                continue
            raw_side = str(d.get("side") or "").lower()
            side = "buy" if raw_side == "buy" else "sell" if raw_side == "sell" else None
            if side is None:
                continue
            rows.append(
                {
                    "ts": ts_ms // 1000,
                    "venue": "okx",
                    "symbol": inst_id,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "notional_usd": qty * price,
                }
            )
    return rows


async def _ws_consume(url: str, subscribe: dict[str, Any] | None, parser, *, max_seconds: float) -> AsyncIterator[dict[str, Any]]:
    """Shared WS consumer with a bounded run window.

    ``parser`` receives each decoded message; it may return a single row or
    a list of rows (or None / empty for messages that aren't events).
    """
    import asyncio  # noqa: PLC0415

    import websockets  # noqa: PLC0415

    deadline = asyncio.get_event_loop().time() + max_seconds
    async with websockets.connect(url, open_timeout=10, close_timeout=5) as ws:
        if subscribe is not None:
            await ws.send(json.dumps(subscribe))
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                return
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            parsed = parser(event)
            if parsed is None:
                continue
            if isinstance(parsed, dict):
                yield parsed
            else:
                for row in parsed:
                    yield row


async def binance_liquidation_stream(*, max_seconds: float = 60.0) -> AsyncIterator[dict[str, Any]]:
    """Async-iterate parsed liquidation rows from Binance for at most max_seconds."""
    async for row in _ws_consume(BINANCE_WS_URL, None, parse_binance_force_order, max_seconds=max_seconds):
        yield row


async def bybit_liquidation_stream(symbols: list[str], *, max_seconds: float = 60.0) -> AsyncIterator[dict[str, Any]]:
    """Async-iterate parsed liquidation rows from Bybit. Requires an explicit
    list of symbols (Bybit v5 has no "all symbols" channel)."""
    sub = {"op": "subscribe", "args": [f"allLiquidation.{s}" for s in symbols]}
    async for row in _ws_consume(BYBIT_WS_URL, sub, parse_bybit_liquidation, max_seconds=max_seconds):
        yield row


async def okx_liquidation_stream(inst_type: str = "SWAP", *, max_seconds: float = 60.0) -> AsyncIterator[dict[str, Any]]:
    """Async-iterate parsed liquidation rows from OKX for one instType (SWAP /
    FUTURES / MARGIN / OPTION)."""
    sub = {"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": inst_type}]}
    async for row in _ws_consume(OKX_WS_URL, sub, parse_okx_liquidation, max_seconds=max_seconds):
        yield row
