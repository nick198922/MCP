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
import logging
import random
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field, fields
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


# Connection settings. websockets' protocol keepalive pings every 20 s; the pong may take 30 s (default 20) because a
# long collector job can hold the event loop and a late pong is not a dead link (keepalive timeouts, 2026-11-07).
OPEN_TIMEOUT = 10.0
PING_INTERVAL = 20.0
PING_TIMEOUT = 30.0
CLOSE_TIMEOUT = 5.0
# Application heartbeat after this many seconds without a message. OKX drops a connection that has pushed nothing for
# 30 s and wants the text "ping" (docs v5 "Overview / Connect"); Bybit asks for {"op":"ping"} every 20 s (docs v5
# "Connect / How to send the heartbeat packet"). Binance pings us itself.
HEARTBEAT_SECONDS = 20.0
# Reconnect backoff: base x 2^(n-1) for the n-th failure in a row, capped, times a jitter in [0.5, 1). The count
# resets once a connection has stayed up STABLE_SECONDS or until the end of the run window without failing.
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
STABLE_SECONDS = 60.0

log = logging.getLogger("tradedesk.collector")


class StreamError(Exception):
    """The venue answered on the stream with an error (rejected subscription, bad op)."""


@dataclass
class StreamStats:
    """Per-stream counters kept across runs of a collector job; ``system_health`` shows them in ``collector[].stream``.

    ``failures`` counts every failed connection (handshake timeout, keepalive timeout, reset, venue error message);
    ``reconnects`` the connections opened after such a failure. ``last_message_ts`` (any frame, heartbeat replies
    included) against ``last_row_ts`` (last liquidation) separates a quiet market from a dead link. Timestamps are
    epoch seconds from ``clock`` (the collector passes ``rt.now``; providers do not read the wall clock), None without.
    """

    clock: Callable[[], float] | None = field(default=None, repr=False, compare=False)
    connected: bool = False
    connects: int = 0
    reconnects: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_failure: str | None = None
    last_failure_ts: float | None = None
    last_connect_ts: float | None = None
    last_message_ts: float | None = None
    last_row_ts: float | None = None

    def stamp(self) -> float | None:
        return self.clock() if self.clock is not None else None

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "clock"}


def backoff_delay(failures: int, u: float, *, base: float = BACKOFF_BASE, cap: float = BACKOFF_CAP) -> float:
    """Seconds to wait before the next attempt after ``failures`` failures in a row (0 -> no wait).

    ``min(cap, base * 2**(failures - 1)) * (0.5 + u / 2)`` with ``u`` uniform in [0, 1): exponential with jitter, so
    the three streams (and a restarted server) do not reconnect in lockstep.
    """
    if failures <= 0:
        return 0.0
    return min(cap, base * 2.0 ** min(failures - 1, 30)) * (0.5 + 0.5 * u)


ERROR_TEXT_MAX = 200  # venue text is untrusted: bounded before it reaches logs and system_health


def _clip(text: str, limit: int = ERROR_TEXT_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bybit_error(event: Any) -> str | None:
    if isinstance(event, dict) and event.get("success") is False:
        return _clip(f"bybit {event.get('op')}: {event.get('ret_msg')}")
    return None


def _okx_error(event: Any) -> str | None:
    if isinstance(event, dict) and event.get("event") == "error":
        return _clip(f"okx error {event.get('code')}: {event.get('msg')}")
    return None


async def _ws_consume(
    url: str,
    subscribe: dict[str, Any] | None,
    parser,
    *,
    max_seconds: float,
    stats: StreamStats | None = None,
    heartbeat: str | None = None,
    error_of: Callable[[Any], str | None] | None = None,
    connect: Callable[..., Any] | None = None,
    backoff_base: float = BACKOFF_BASE,
    backoff_cap: float = BACKOFF_CAP,
    stable_seconds: float = STABLE_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[dict[str, Any]]:
    """Shared WS consumer for a bounded run window that survives disconnects.

    ``parser`` receives each decoded message; it may return a single row or a list of rows (or None / empty for
    messages that aren't events). A dropped connection, a failed handshake or an error message (``error_of``) is
    counted in ``stats`` and logged; the consumer waits ``backoff_delay`` and reconnects (and resubscribes) until the
    window ends. The wait is capped at half the window, so every run makes at least one attempt. The run raises the
    last error only when no connection opened in the whole window, so the scheduler still counts a venue that is
    down as a failed run. Any other exception (a parser bug) is not a dropped link and propagates.
    ``heartbeat`` is sent after ``heartbeat_seconds`` without a message.
    """
    import asyncio  # noqa: PLC0415

    import websockets  # noqa: PLC0415
    from websockets.exceptions import WebSocketException  # noqa: PLC0415

    if connect is None:
        connect = websockets.connect
    # Only network, protocol and venue errors mean a dropped link; a parser bug still fails the run.
    stream_errors = (OSError, TimeoutError, WebSocketException, StreamError)
    st = stats if stats is not None else StreamStats()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    cap = min(backoff_cap, max_seconds / 2)
    opened = False
    last_exc: Exception | None = None
    while True:
        delay = backoff_delay(st.consecutive_failures, random.random(), base=backoff_base, cap=cap)
        if loop.time() + delay >= deadline and (opened or last_exc is not None):
            break
        if delay > 0:
            await asyncio.sleep(delay)
        opened_at: float | None = None
        try:
            async with connect(
                url, open_timeout=OPEN_TIMEOUT, ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT, close_timeout=CLOSE_TIMEOUT
            ) as ws:
                opened, opened_at = True, loop.time()
                st.connected, st.connects, st.last_connect_ts = True, st.connects + 1, st.stamp()
                if st.consecutive_failures:
                    st.reconnects += 1
                if subscribe is not None:
                    await ws.send(json.dumps(subscribe))
                while True:
                    now = loop.time()
                    if st.consecutive_failures and now - opened_at >= stable_seconds:
                        st.consecutive_failures = 0
                    remaining = deadline - now
                    if remaining <= 0:
                        # Up until the end of the window without a failure counts as stable too: with the default
                        # 60 s window a connection never lives stable_seconds inside one run (2026-11-09 review).
                        st.consecutive_failures = 0
                        return
                    wait = remaining if heartbeat is None else min(remaining, heartbeat_seconds)
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=wait)
                    except TimeoutError:
                        if heartbeat is not None and deadline - loop.time() > 0:
                            await ws.send(heartbeat)
                        continue
                    st.last_message_ts = st.stamp()
                    try:
                        event = json.loads(raw)
                    except (TypeError, ValueError):
                        continue  # OKX answers "ping" with a plain "pong"
                    err = error_of(event) if error_of is not None else None
                    if err is not None:
                        raise StreamError(err)
                    parsed = parser(event)
                    if not parsed:
                        continue
                    for row in [parsed] if isinstance(parsed, dict) else parsed:
                        st.last_row_ts = st.last_message_ts
                        yield row
        except stream_errors as e:  # network / protocol / venue error: count it, back off, reconnect
            last_exc = e
            if opened_at is not None and loop.time() - opened_at >= stable_seconds:
                st.consecutive_failures = 0  # it had been stable: start the backoff over
            st.failures += 1
            st.consecutive_failures += 1
            st.last_failure, st.last_failure_ts = f"{type(e).__name__}: {e}", st.stamp()
            log.warning(
                "liquidation stream %s failed (%d in a row): %s",
                url,
                st.consecutive_failures,
                st.last_failure,
                extra={"stream": url, "error": st.last_failure},
            )
        finally:
            st.connected = False
    if not opened and last_exc is not None:
        raise last_exc


async def binance_liquidation_stream(*, max_seconds: float = 60.0, stats: StreamStats | None = None) -> AsyncIterator[dict[str, Any]]:
    """Async-iterate parsed liquidation rows from Binance for at most max_seconds."""
    async for row in _ws_consume(BINANCE_WS_URL, None, parse_binance_force_order, max_seconds=max_seconds, stats=stats):
        yield row


async def bybit_liquidation_stream(
    symbols: list[str], *, max_seconds: float = 60.0, stats: StreamStats | None = None
) -> AsyncIterator[dict[str, Any]]:
    """Async-iterate parsed liquidation rows from Bybit. Requires an explicit
    list of symbols (Bybit v5 has no "all symbols" channel)."""
    sub = {"op": "subscribe", "args": [f"allLiquidation.{s}" for s in symbols]}
    async for row in _ws_consume(
        BYBIT_WS_URL,
        sub,
        parse_bybit_liquidation,
        max_seconds=max_seconds,
        stats=stats,
        heartbeat=json.dumps({"op": "ping"}),
        error_of=_bybit_error,
    ):
        yield row


async def okx_liquidation_stream(
    specs: Mapping[str, tuple[float, bool]], inst_type: str = "SWAP", *, max_seconds: float = 60.0, stats: StreamStats | None = None
) -> AsyncIterator[dict[str, Any]]:
    """Async-iterate parsed liquidation rows from OKX for one contract instType (SWAP / FUTURES). ``specs`` comes
    from ``okx_contract_specs`` on the same instType; liquidations of instruments not in it are dropped."""
    sub = {"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": inst_type}]}
    async for row in _ws_consume(
        OKX_WS_URL,
        sub,
        lambda ev: parse_okx_liquidation(ev, specs),
        max_seconds=max_seconds,
        stats=stats,
        heartbeat="ping",
        error_of=_okx_error,
    ):
        yield row
