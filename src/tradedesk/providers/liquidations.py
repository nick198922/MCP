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

Our side convention is the taker side of the force order: ``sell`` = a long got
liquidated, ``buy`` = a short got liquidated. Bybit reports the *position* side
instead (``Buy`` = a long got liquidated, docs v5 "All Liquidation"), so its
side is flipped. OKX swap/futures ``sz`` is in contracts, converted to coins
with the instrument's ``ctVal`` x ``ctMult`` (``okx_contract_specs``). Prices:
Binance average fill, Bybit bankruptcy price, OKX "liquidation mark price"
(near the mark).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

# USD-M market streams moved under /market/ (checked 2026-10-17): the old /ws/ path still accepts the connection but
# sends nothing, not even btcusdt@aggTrade.
BINANCE_WS_URL = "wss://fstream.binance.com/market/ws/!forceOrder@arr"
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
# OKX notice on subscribe (2026-10-28): "WebSocket port 8443 will be retired soon. Please migrate to port 443."
OKX_WS_URL = "wss://ws.okx.com:443/ws/v5/public"


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

    Payload (docs v5 "All Liquidation", checked 2026-10-28):

    .. code-block:: json

        {"topic":"allLiquidation.ROSEUSDT","type":"snapshot","ts":1739502303204,
         "data":[{"T":1739502302929,"s":"ROSEUSDT","S":"Sell","v":"20000","p":"0.04499"}]}

    ``S`` is the **position** side: ``Buy`` = a long got liquidated (docs), the
    opposite of Binance's taker side, so it maps to our ``sell``. ``v`` is in
    the base coin on the linear (USDT/USDC) stream, ``p`` is the bankruptcy
    price. Returns a list because ``data`` may hold several rows.
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
        # Position side -> taker side of the force order: a liquidated long (Buy) is closed by a sell.
        side = {"buy": "sell", "sell": "buy"}.get(str(side_raw).lower())
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


def _pos_float(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def okx_contract_specs(instruments: list[Any]) -> dict[str, tuple[float, bool]]:
    """``instId -> (size per contract, inverse)`` from OKX ``/api/v5/public/instruments`` rows (SWAP / FUTURES).

    Size per contract is ``ctVal x ctMult`` (``ctMult`` empty = 1). Linear contracts must be valued in the base coin
    (``ctValCcy`` = first part of ``instId``, e.g. BTC-USDT-SWAP 0.01 BTC); inverse ones in USD (BTC-USD-SWAP 100 USD).
    Rows that do not fit (spot/margin rows without ``ctVal``, other value currencies) are left out, so their
    liquidations are dropped rather than stored with a wrong size.
    """
    specs: dict[str, tuple[float, bool]] = {}
    for ins in instruments:
        if not isinstance(ins, dict):
            continue
        inst_id = str(ins.get("instId") or "")
        parts = inst_id.split("-")
        ct_val = _pos_float(ins.get("ctVal"))
        ct_mult = _pos_float(ins.get("ctMult") or 1)
        if len(parts) < 2 or ct_val is None or ct_mult is None:
            continue
        ct_type, ccy = ins.get("ctType"), ins.get("ctValCcy")
        if ct_type == "linear" and ccy == parts[0]:
            specs[inst_id] = (ct_val * ct_mult, False)
        elif ct_type == "inverse" and ccy == "USD":
            specs[inst_id] = (ct_val * ct_mult, True)
    return specs


def parse_okx_liquidation(event: dict[str, Any], specs: Mapping[str, tuple[float, bool]]) -> list[dict[str, Any]]:
    """Translate an OKX v5 ``liquidation-orders`` message.

    Payload (docs v5 "Liquidation orders channel", checked 2026-10-28):

    .. code-block:: json

        {"arg":{"channel":"liquidation-orders","instType":"SWAP"},
         "data":[{"instId":"IOST-USDT-SWAP","instType":"SWAP","uly":"IOST-USDT","details":[
              {"bkLoss":"0","bkPx":"0.007831","ccy":"","posSide":"short","side":"buy",
               "sz":"13","ts":"1692266434010"}]}]}

    ``side`` is the order side that closed the position, which is our convention (``sell`` = a long got
    liquidated). ``posSide`` is ``long``/``short`` in hedge mode (must agree with ``side``, else the detail is
    dropped) or ``net``. ``sz`` is in **contracts** for SWAP/FUTURES: ``specs`` (``okx_contract_specs``) turns it
    into coins (linear: sz x ctVal; inverse: USD = sz x ctVal, coins = USD / price). Instruments missing from
    ``specs`` are dropped. ``bkPx`` is, per the docs, the "liquidation mark price" at which the position went to the
    liquidation account; live 2026-10-28 it sat inside the minute's mark range (unlike Bybit's bankruptcy price).
    """
    arg = event.get("arg") or {}
    if arg.get("channel") != "liquidation-orders":
        return []
    rows: list[dict[str, Any]] = []
    for inst in event.get("data") or []:
        inst_id = inst.get("instId") or "?"
        spec = specs.get(inst_id)
        if spec is None:
            continue
        per_contract, inverse = spec
        for d in inst.get("details") or []:
            try:
                contracts = float(d["sz"])
                price = float(d["bkPx"])
                ts_ms = int(d["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if contracts <= 0 or price <= 0 or ts_ms <= 0:
                continue
            side = {"buy": "buy", "sell": "sell"}.get(str(d.get("side") or "").lower())
            pos_side = str(d.get("posSide") or "").lower()
            if side is None or (pos_side == "long" and side != "sell") or (pos_side == "short" and side != "buy"):
                continue
            if inverse:
                notional = contracts * per_contract
                qty = notional / price
            else:
                qty = contracts * per_contract
                notional = qty * price
            rows.append(
                {
                    "ts": ts_ms // 1000,
                    "venue": "okx",
                    "symbol": inst_id,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "notional_usd": notional,
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


async def okx_liquidation_stream(
    specs: Mapping[str, tuple[float, bool]], inst_type: str = "SWAP", *, max_seconds: float = 60.0
) -> AsyncIterator[dict[str, Any]]:
    """Async-iterate parsed liquidation rows from OKX for one contract instType (SWAP / FUTURES). ``specs`` comes
    from ``okx_contract_specs`` on the same instType; liquidations of instruments not in it are dropped."""
    sub = {"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": inst_type}]}
    async for row in _ws_consume(OKX_WS_URL, sub, lambda ev: parse_okx_liquidation(ev, specs), max_seconds=max_seconds):
        yield row
