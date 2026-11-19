"""Bybit option chain (BTC / ETH) from the v5 option tickers: per expiry and strike, the call and put top of book with
mid, spread and mark IV, for the `bybit_option_chain` tool.

Pure functions: no I/O, ``now`` is a parameter. Input: the ``result.list`` rows of GET
/v5/market/tickers?category=option&baseCoin=<COIN> (numbers as strings), the answer the collector ``option_xvenue``
and scripts/option_ideas_tg.py read through ``Exchanges.public_get``. Symbols are parsed by
``option_xvenue.parse_bybit_symbol`` and quotes normalised by ``option_xvenue.bybit_quote``, so the chain and the
collector read a row the same way. Only one settle currency is kept (USDT, as the collector does): a strike listed in
two settle currencies is never mixed.

Units: prices in USDT per 1 coin of the underlying (one Bybit option contract = 1 BTC / 1 ETH), treated as USD; IV
decimals (0.45 = 45 %); ``spread_iv`` = ask IV - bid IV, decimal (0.01 = 1 vol point); ``spread_pct`` =
(ask - bid) / mid in percent; open interest in coins; ``days`` = calendar days to the 08:00 UTC expiry.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from .instruments import expiry_token
from .option_xvenue import atm_strikes, bybit_quote, num, parse_bybit_symbol

SETTLE = "USDT"

Chain = dict[datetime, dict[float, dict[str, dict[str, Any]]]]  # expiry -> strike -> {"C": row, "P": row}


def _finite(v: Any) -> float | None:
    """A finite float (any sign, zero included) from a number or a numeric string, else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def parse_chain(rows: Any, coin: str, settle: str = SETTLE) -> tuple[Chain, list[str]]:
    """``coin``'s ticker rows by expiry / strike / type, only symbols settled in ``settle`` (``BTC-6NOV26-80000-C-USDT``).
    Also returns the other settle suffixes seen, sorted ("none (USDC)" for a symbol without one), for a warning.
    Rows that are not a dict or whose symbol does not parse (or has a non-finite / non-positive strike) are skipped."""
    chain: Chain = {}
    other: set[str] = set()
    for r in rows if isinstance(rows, list) else []:
        p = parse_bybit_symbol(str(r.get("symbol", ""))) if isinstance(r, dict) else None
        if p is None or p["base"] != coin or not (math.isfinite(p["strike"]) and p["strike"] > 0):  # "inf" / "nan" parse as floats
            continue
        if p["settle"] != settle:
            other.add(p["settle"] or "none (USDC)")
            continue
        chain.setdefault(p["expiry"], {}).setdefault(p["strike"], {})["C" if p["is_call"] else "P"] = r
    return chain, sorted(other)


def days_to(expiry: datetime, now: datetime) -> float:
    return (expiry - now).total_seconds() / 86400.0


def select_expiries(expiries: Iterable[datetime], now: datetime, min_days: float, max_days: float) -> list[datetime]:
    """Expiries with ``min_days <= days <= max_days`` (inclusive), nearest first."""
    return sorted(e for e in set(expiries) if min_days <= days_to(e, now) <= max_days)


def leg(row: dict[str, Any], details: bool = False) -> dict[str, Any]:
    """One option's quote. ``mid`` and ``spread_pct`` need both sides; ``spread_iv`` needs both IVs (Bybit sends IV "0"
    when it has none for a side, e.g. no bid or a bid below intrinsic, which ``num`` reads as None). ``oi`` in coins.
    ``details`` adds the sizes at the touch (coins), bid / ask IV, mark price, delta and the symbol."""
    q = bybit_quote(row, None)
    bid, ask = q["bid"], q["ask"]
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    oi = _finite(row.get("openInterest"))
    out: dict[str, Any] = {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread_pct": (ask - bid) / mid * 100.0 if mid else None,
        "spread_iv": q["ask_iv"] - q["bid_iv"] if q["ask_iv"] is not None and q["bid_iv"] is not None else None,
        "mark_iv": q["mark_iv"],
        "oi": oi if oi is not None and oi >= 0 else None,
    }
    if details:
        out.update(
            bid_size=q["bid_size"],
            ask_size=q["ask_size"],
            bid_iv=q["bid_iv"],
            ask_iv=q["ask_iv"],
            mark=num(row.get("markPrice")),
            delta=_finite(row.get("delta")),
            symbol=row.get("symbol") if isinstance(row.get("symbol"), str) else None,
        )
    return out


def _median(xs: Iterable[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return statistics.median(vals) if vals else None


def index_price(chain: Chain) -> float | None:
    """Median Bybit index price over every row of the chain (the same index for every expiry)."""
    return _median(num(r.get("indexPrice")) for by_k in chain.values() for side in by_k.values() for r in side.values())


def expiry_view(
    strikes: dict[float, dict[str, dict[str, Any]]], expiry: datetime, now: datetime, each_side: int | None, details: bool = False
) -> dict[str, Any]:
    """One expiry: days, Bybit's underlying price for it (median ``underlyingPrice`` of its rows, USD), the ATM strike
    (nearest that underlying; ties: the lower), ATM mark IV (mean of the call and put mark IV there, decimal), call / put
    open interest over ALL its strikes (coins), and the strike rows: the ATM strike and ``each_side`` neighbours on each
    side, or every strike when ``each_side`` is None. Without any underlying price the strikes are centred on the
    median strike and ``atm_strike`` / ``atm_iv`` are None."""
    rows = [r for side in strikes.values() for r in side.values()]
    underlying = _median(num(r.get("underlyingPrice")) for r in rows)
    ks = sorted(strikes)
    atm = atm_strikes(ks, underlying, 0)[0] if underlying is not None and ks else None
    center = underlying if underlying is not None else (statistics.median(ks) if ks else 0.0)
    shown = ks if each_side is None else atm_strikes(ks, center, each_side)
    atm_ivs = [num(strikes[atm][t].get("markIv")) for t in ("C", "P") if t in strikes[atm]] if atm is not None else []
    atm_iv = _median(atm_ivs)

    def oi_sum(t: str) -> float:
        return sum(x for k in ks if t in strikes[k] for x in [_finite(strikes[k][t].get("openInterest"))] if x is not None and x >= 0)

    return {
        "expiry": expiry_token(expiry),
        "days": days_to(expiry, now),
        "underlying": underlying,
        "atm_strike": atm,
        "atm_iv": atm_iv,
        "oi_calls": oi_sum("C"),
        "oi_puts": oi_sum("P"),
        "n_strikes": len(ks),
        "strikes": [
            {
                "strike": k,
                "call": leg(strikes[k]["C"], details) if "C" in strikes[k] else None,
                "put": leg(strikes[k]["P"], details) if "P" in strikes[k] else None,
            }
            for k in shown
        ],
    }
