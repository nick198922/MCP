"""Public liquidation streams.

MVP covers Binance USD-M futures only: the `!forceOrder@arr` stream carries
every force-close across all perps and does not require a subscribe message,
so the whole thing fits in one WebSocket consumer.

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

BINANCE_WS_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"


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


async def binance_liquidation_stream(*, max_seconds: float = 60.0) -> AsyncIterator[dict[str, Any]]:
    """Async-iterate parsed liquidation rows from Binance for at most max_seconds.

    Imports ``websockets`` lazily so the whole dependency is only loaded when
    the collector actually runs this stream.
    """
    import asyncio  # noqa: PLC0415

    import websockets  # noqa: PLC0415

    deadline = asyncio.get_event_loop().time() + max_seconds
    async with websockets.connect(BINANCE_WS_URL, open_timeout=10, close_timeout=5) as ws:
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
            parsed = parse_binance_force_order(event)
            if parsed is not None:
                yield parsed
