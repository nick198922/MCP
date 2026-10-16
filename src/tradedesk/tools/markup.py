"""chart_markup: the course's hand-drawn chart markup computed from OHLCV (see analytics/markup.py)."""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics import markup as mk
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, timeframe_seconds

FX_PAIR = re.compile(r"^([A-Z]{3})/?([A-Z]{3})$")


def resolve_symbol(exchange: str, symbol: str) -> str:
    """BingX forex perps are listed as NCFX<BASE>2<QUOTE>-USDT; accept 'EURUSD' or 'EUR/USD' for them."""
    m = FX_PAIR.match(symbol.strip().upper())
    if exchange.lower() == "bingx" and m and "USDT" not in symbol.upper():
        return f"NCFX{m.group(1)}2{m.group(2)}/USDT:USDT"
    return symbol


def px(x: float | None) -> float | None:
    """Six significant digits: enough for BTC and for EURUSD alike."""
    return None if x is None else float(f"{float(x):.6g}")


def _bars(rows: list[list[Any]] | None) -> list[dict[str, Any]]:
    return [
        {"ts": int(x[0]), "open": float(x[1]), "high": float(x[2]), "low": float(x[3]), "close": float(x[4]), "volume": float(x[5] or 0.0)}
        for x in rows or []
        if x[4] is not None
    ]


def _swing(s: dict[str, Any]) -> dict[str, Any]:
    return {"price": px(s["price"]), "ts_ms": s["ts"]}


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Chart markup the DoubleTop course draws by hand, computed from a venue's OHLCV: EMA groups (Trend 13/21/34 on "
            "the chart timeframe, 'Daily' EMA 100/200 on 4h, 'Weekly' EMA 100/200 on 1d), anchored VWAP with a 1-sigma band, "
            "volume profile (POC/VAH/VAL over the window), 3-candle swings and market structure (BOS only on a candle CLOSE beyond "
            "a swing; a wick beyond with the close back inside is a sweep / fake BOS), untaken liquidity above and below and equal "
            "highs/lows pools, fair value gaps with their fill state (open/partial/balanced at 0.5/filled), order blocks with the "
            "course's validity flags (impulsive engulf, FVG after, BOS after) and state (untested/tested/broken = breaker candidate), "
            "premium/discount and OTE 0.618-0.786 on the leg of the latest BOS, ATR(14), RSI(14), and for timeframes up to 1h the "
            "Asian range (00:00 UTC to the Frankfurt open) and whether the Frankfurt/London hunt window swept it (setup S25). "
            "Prices are in the quote currency; percentages are not used. BingX forex perps accept 'EURUSD' (mapped to "
            "NCFXEUR2USD/USDT:USDT). Descriptive levels, not signals: our tests of mechanical SMC rules found no edge after costs."
        ),
    )
    async def chart_markup(
        exchange: Annotated[str, Field(description="ccxt exchange id, e.g. 'binanceusdm', 'bybit', 'bingx' (forex perps).")],
        symbol: Annotated[str, Field(description="ccxt unified symbol, e.g. 'BTC/USDT:USDT'; on bingx also 'EURUSD'.")],
        timeframe: Annotated[str, Field(description="Chart timeframe, e.g. '15m', '1h', '4h', '1d'.")] = "4h",
        limit: Annotated[int, Field(ge=60, le=1000, description="Bars on the chart timeframe.")] = 300,
        vwap_anchor: Annotated[
            Literal["day", "week", "month", "quarter", "year"], Field(description="VWAP anchor period (the course uses quarter on 4h).")
        ] = "quarter",
        htf_emas: Annotated[bool, Field(description="Also fetch 4h and 1d bars for the Daily/Weekly EMA groups.")] = True,
    ) -> dict[str, Any]:
        b = Builder()
        try:
            tf_sec = timeframe_seconds(timeframe)
        except ValueError as e:
            b.miss("timeframe", "tradedesk", "config", str(e))
            return b.build(None, ok=False)
        sym = resolve_symbol(exchange, symbol)
        provider = f"ccxt:{exchange}"
        calls = [b.guard("ohlcv", provider, rt.exchanges.ohlcv(exchange, sym, timeframe, limit))]
        if htf_emas:
            calls.append(b.guard("ohlcv_4h", provider, rt.exchanges.ohlcv(exchange, sym, "4h", 220)))
            calls.append(b.guard("ohlcv_1d", provider, rt.exchanges.ohlcv(exchange, sym, "1d", 220)))
        results = await asyncio.gather(*calls)
        bars = _bars(results[0])
        if len(bars) < 30:
            if results[0] is not None:
                b.miss("ohlcv", provider, "data_unavailable", f"{len(bars)} bars, need at least 30")
            return b.build(None, ok=False)
        h4 = [x["close"] for x in _bars(results[1])] if htf_emas else None
        d1 = [x["close"] for x in _bars(results[2])] if htf_emas else None

        price = bars[-1]["close"]
        atr = mk.atr(bars)
        swings = mk.fractals(bars)
        st = mk.structure(bars, swings)
        gaps = mk.fvgs(bars)
        obs = mk.order_blocks(bars, gaps, st["events"], atr)
        tol = 0.1 * atr if atr else price * 0.0005
        vwap = mk.anchored_vwap(bars, vwap_anchor)
        vp = mk.volume_profile(bars)
        dr = mk.dealing_range(bars, st["last_bos"])

        def side_of(lo: float, hi: float) -> str:
            return "above" if price > hi else "below" if price < lo else "inside"

        open_gaps = [g for g in gaps if g["state"] != "filled"]
        live_obs = [o for o in obs if o["state"] != "broken" and o["score"] >= 2]
        breakers = [o for o in obs if o["state"] == "broken" and o["bos_after"]]

        def gap_view(g: dict[str, Any]) -> dict[str, Any]:
            return {
                "type": g["type"],
                "bottom": px(g["bottom"]),
                "top": px(g["top"]),
                "mid": px(g["mid"]),
                "state": g["state"],
                "ts_ms": g["ts"],
            }

        def ob_view(o: dict[str, Any]) -> dict[str, Any]:
            return {
                "type": o["type"],
                "zone_low": px(o["zone_low"]),
                "zone_high": px(o["zone_high"]),
                "body_low": px(o["body_low"]),
                "body_high": px(o["body_high"]),
                "flags": {"impulsive": o["impulsive"], "fvg_after": o["fvg_after"], "bos_after": o["bos_after"]},
                "state": o["state"],
                "ts_ms": o["ts"],
            }

        def dist(lo: float, hi: float) -> float:
            return 0.0 if lo <= price <= hi else min(abs(price - lo), abs(price - hi))

        eqh = [p for p in mk.equal_levels(st["untaken_highs"], tol) if p["high"] > price]
        eql = [p for p in mk.equal_levels(st["untaken_lows"], tol) if p["low"] < price]
        data: dict[str, Any] = {
            "exchange": exchange,
            "symbol": sym,
            "timeframe": timeframe,
            "bars": len(bars),
            "last_ts_ms": bars[-1]["ts"],
            "price": px(price),
            "atr14": px(atr),
            "rsi14": None if (v := mk.rsi([x["close"] for x in bars])) is None else round(v, 1),
            "emas": {
                k: (None if g is None else {**{n: px(v) for n, v in g.items() if n != "price_vs"}, "price_vs": g["price_vs"]})
                for k, g in mk.ema_levels(price, [x["close"] for x in bars], h4, d1).items()
            },
            "vwap": None
            if vwap is None
            else {
                "anchor": vwap["anchor"],
                "since_ms": vwap["start_ms"],
                "vwap": px(vwap["vwap"]),
                "upper_1sd": px(vwap["upper_1sd"]),
                "lower_1sd": px(vwap["lower_1sd"]),
                "price_vs": side_of(vwap["lower_1sd"], vwap["upper_1sd"]),
            },
            "volume_profile": None
            if vp is None
            else {"poc": px(vp["poc"]), "vah": px(vp["vah"]), "val": px(vp["val"]), "price_vs_value_area": side_of(vp["val"], vp["vah"])},
            "structure": {
                "trend": st["trend"],
                "last_bos": None
                if not st["last_bos"]
                else {"type": st["last_bos"]["type"], "level": px(st["last_bos"]["level"]), "ts_ms": st["last_bos"]["ts"]},
                "recent_events": [{"type": e["type"], "level": px(e["level"]), "ts_ms": e["ts"]} for e in st["events"][-8:]],
            },
            "liquidity": {
                "above": [_swing(s) for s in mk.nearest(st["untaken_highs"], price, "above")],
                "below": [_swing(s) for s in mk.nearest(st["untaken_lows"], price, "below")],
                "equal_highs": [
                    {"low": px(p["low"]), "high": px(p["high"]), "touches": p["touches"]} for p in sorted(eqh, key=lambda p: p["low"])[:3]
                ],
                "equal_lows": [
                    {"low": px(p["low"]), "high": px(p["high"]), "touches": p["touches"]} for p in sorted(eql, key=lambda p: -p["high"])[:3]
                ],
            },
            "fvg": {
                "above": [gap_view(g) for g in sorted((g for g in open_gaps if g["bottom"] > price), key=lambda g: g["bottom"])[:3]],
                "below": [gap_view(g) for g in sorted((g for g in open_gaps if g["top"] < price), key=lambda g: -g["top"])[:3]],
                "at_price": [gap_view(g) for g in open_gaps if g["bottom"] <= price <= g["top"]][:2],
            },
            "order_blocks": [ob_view(o) for o in sorted(live_obs, key=lambda o: dist(o["zone_low"], o["zone_high"]))[:4]],
            "breakers": [ob_view(o) for o in sorted(breakers, key=lambda o: dist(o["zone_low"], o["zone_high"]))[:3]],
            "dealing_range": None
            if dr is None
            else {
                "direction": dr["direction"],
                "low": px(dr["low"]),
                "high": px(dr["high"]),
                "levels": {k: px(v) for k, v in dr["levels"].items()},
                "price_zone": dr["price_zone"],
                "in_ote": dr["in_ote"],
            },
        }
        if tf_sec <= 3600:
            sess = mk.asia_session(bars, bars[-1]["ts"])
            data["asia_session"] = (
                None
                if sess is None
                else {
                    **{k: px(v) for k, v in sess.items() if k in ("asia_high", "asia_low")},
                    **{k: v for k, v in sess.items() if k not in ("asia_high", "asia_low")},
                }
            )
        if htf_emas and (h4 is None or d1 is None or len(h4) < 200 or len(d1) < 200):
            b.warn("Daily/Weekly EMA 200 needs 200 bars on 4h/1d; a young listing returns null for it.")
        b.warn("Descriptive levels, not signals: mechanical SMC rules showed no edge after costs in our tests (docs/knowledge/smc.md).")
        if sym.startswith("NCFX") or sym.startswith("NC"):
            b.warn(
                "BingX TradFi perp: a synthetic market quoted from external feeds, funding every 8h; with taker ~0.05 % a side "
                "the round trip is ~10 pips on EURUSD, more than the 4-9 pip stops of the course's forex examples."
            )
        return b.build(data)
