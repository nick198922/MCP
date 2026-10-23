"""Cross-venue daily option quotes (Deribit vs Bybit): parsing, strike selection and the book-crossing measure.

Docs: docs/ideas/IDEA-20261023-bybit-deribit-daily-option-dislocation-forward.md (forward card, measurement only).
Pure functions: no I/O, time is a parameter. The collector `collector/option_xvenue.py` stores raw quotes; the crossing
is computed at read time, so the cost model can change without rewriting history.

Units: option prices are USD per 1 unit of the underlying (Deribit quotes in the coin; the caller converts with the
Deribit index); sizes are base-coin units at the best level; IV decimals (0.45 = 45 %); edges are decimal fractions of
spot (0.0015 = 0.15 %). The registered analysis parameters (``REG_*``, card section «Реєстрація (до збору)», 23.10.2026)
were committed before the collector was switched on; :func:`adjusted_crossing_from_row` is the registered primary measure.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from .instruments import parse_option_name
from .options import black76_greeks

FEE_RATE = 0.0003  # per leg and venue, fraction of the underlying index (card: 0.03 % each side; Bybit's rate unverified)
MIN_DEPTH = 0.1  # base-coin units resting at the touch on both legs (card: >= 0.1 BTC)
TAU_MIN_HOURS = 2.0  # daily expiry window: hours to the 08:00 UTC expiry
TAU_MAX_HOURS = 20.0
STRIKES_EACH_SIDE = 3  # ATM +- 3 strikes (collected); the registered analysis uses ATM +- 1

# Registered before collection (card «Реєстрація (до збору)», 23.10.2026). Changing any of these = exploratory.
REG_THRESHOLD = 0.0015  # net crossing after fees, fraction of the Deribit index
REG_STRIKES_EACH_SIDE = 1  # ATM +- 1 Deribit strike around the Deribit forward
REG_SYNC_MS = 5_000  # |b_ts_ms - d_ts_ms| for the primary count; 5-60 s reported separately
REG_SYNC_LOOSE_MS = 60_000
REG_GAP_SOURCE = "index"  # primary index gap b_index / d_index - 1; sensitivity "underlying"


def num(v: Any) -> float | None:
    """A strictly positive finite float, else None (venues send 0 / "0" / "" for an empty side)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 0 and x != float("inf") else None


def parse_bybit_symbol(symbol: str) -> dict[str, Any] | None:
    """``BTC-24OCT26-110000-C`` (USDC-settled) or ``BTC-24OCT26-110000-C-USDT`` -> base, expiry (08:00 UTC), strike,
    is_call, settle suffix (None without one). None for anything else."""
    parts = symbol.split("-")
    settle = None
    if len(parts) == 5:
        settle, parts = parts[4], parts[:4]
    p = parse_option_name("-".join(parts))
    if p is None:
        return None
    return {"base": p.product, "expiry": p.expiry, "strike": p.strike, "is_call": p.is_call, "settle": settle}


def pick_daily_expiry(
    expiries: Iterable[datetime], now: datetime, tau_min_hours: float = TAU_MIN_HOURS, tau_max_hours: float = TAU_MAX_HOURS
) -> datetime | None:
    """The nearest expiry whose time to expiry lies in [tau_min_hours, tau_max_hours]; None when none does (e.g. between
    06:00 and 10:00 UTC, when today's expiry is too close and tomorrow's too far)."""
    inside = [e for e in set(expiries) if tau_min_hours <= (e - now).total_seconds() / 3600.0 <= tau_max_hours]
    return min(inside) if inside else None


def atm_strikes(strikes: Iterable[float], center: float, each_side: int = STRIKES_EACH_SIDE) -> list[float]:
    """The strike nearest ``center`` (ties: the lower) and ``each_side`` neighbours on each side, ascending."""
    ks = sorted(set(strikes))
    if not ks:
        return []
    i = min(range(len(ks)), key=lambda j: (abs(ks[j] - center), ks[j]))
    return ks[max(0, i - each_side) : i + each_side + 1]


def deribit_quote(t: dict[str, Any]) -> dict[str, Any]:
    """Normalise a Deribit ``public/ticker`` result: prices in the coin, IV percent -> decimal."""

    def iv(k: str) -> float | None:
        x = num(t.get(k))
        return x / 100.0 if x is not None else None

    ts = t.get("timestamp")
    return {
        "bid": num(t.get("best_bid_price")),
        "ask": num(t.get("best_ask_price")),
        "bid_size": num(t.get("best_bid_amount")),
        "ask_size": num(t.get("best_ask_amount")),
        "bid_iv": iv("bid_iv"),
        "ask_iv": iv("ask_iv"),
        "mark_iv": iv("mark_iv"),
        "index": num(t.get("index_price")),
        "underlying": num(t.get("underlying_price")),
        "ts_ms": int(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None,
    }


def bybit_quote(r: dict[str, Any], ts_ms: int | None) -> dict[str, Any]:
    """Normalise a Bybit v5 option ticker row (strings): prices USD, IV already decimal; ``ts_ms`` is the response time."""
    return {
        "bid": num(r.get("bid1Price")),
        "ask": num(r.get("ask1Price")),
        "bid_size": num(r.get("bid1Size")),
        "ask_size": num(r.get("ask1Size")),
        "bid_iv": num(r.get("bid1Iv")),
        "ask_iv": num(r.get("ask1Iv")),
        "mark_iv": num(r.get("markIv")),
        "index": num(r.get("indexPrice")),
        "underlying": num(r.get("underlyingPrice")),
        "ts_ms": ts_ms,
    }


def _side(bid: float | None, bid_size: float | None, ask: float | None, ask_size: float | None, spot: float, fee: float, depth: float):
    if bid is None or ask is None:
        return None
    gross = bid - ask
    net = gross - 2.0 * fee * spot  # one taker fee on each venue
    size = min(bid_size or 0.0, ask_size or 0.0)
    return {"gross_usd": gross, "net_usd": net, "net_frac": net / spot, "depth": size, "depth_ok": size >= depth}


def crossing(
    *,
    d_bid: float | None,
    d_ask: float | None,
    d_bid_size: float | None,
    d_ask_size: float | None,
    b_bid: float | None,
    b_ask: float | None,
    b_bid_size: float | None,
    b_ask_size: float | None,
    spot: float,
    fee_rate: float = FEE_RATE,
    min_depth: float = MIN_DEPTH,
) -> dict[str, Any]:
    """Book crossing of the same option on Deribit (d) and Bybit (b), all prices in USD per 1 unit of the underlying.

    ``b_over_d``: sell at the Bybit bid, buy at the Deribit ask (bid_B - ask_D); ``d_over_b``: bid_D - ask_B. Each side
    is ``{gross_usd, net_usd, net_frac, depth, depth_ok}`` or None when a quote is missing: net = gross minus one taker
    fee of ``fee_rate`` x spot on each venue; ``net_frac`` = net / spot (decimal); ``depth`` = the smaller resting size
    of the two touched levels (base-coin units), ``depth_ok`` when >= ``min_depth``. ``best`` = the larger ``net_frac``
    among sides with ``depth_ok`` (None if neither). Fees ignore the venues' premium caps (12.5 % of the option price on
    Deribit), so for cheap options the fee is overstated: conservative. Entry only; an exit pays two more spreads.
    """
    if not spot or spot <= 0:
        raise ValueError("spot must be positive")
    bd = _side(b_bid, b_bid_size, d_ask, d_ask_size, spot, fee_rate, min_depth)
    db = _side(d_bid, d_bid_size, b_ask, b_ask_size, spot, fee_rate, min_depth)
    ok = [s["net_frac"] for s in (bd, db) if s is not None and s["depth_ok"]]
    return {"b_over_d": bd, "d_over_b": db, "best": max(ok) if ok else None}


def crossing_from_row(row: dict[str, Any], *, fee_rate: float = FEE_RATE, min_depth: float = MIN_DEPTH) -> dict[str, Any] | None:
    """:func:`crossing` of one stored ``option_xvenue_snapshot`` row: Deribit prices (coin) converted with the Deribit
    index, which is also the spot. None when the row has no Deribit index.

    Adds ``index_gap`` = b_index / d_index - 1 (None if either is missing) and ``dt_ms`` = b_ts_ms - d_ts_ms. Bybit
    options settle in USDT on its own index, Deribit on the BTC-USD index: a few bp of index gap shift every Bybit call
    up and put down by about delta x gap x spot, which looks like a dislocation but is the settlement basis (live
    23.10.2026 ~03:00 UTC, two runs: gap +0.14..0.15 % on BTC and ETH, legs 0.6-2.9 s apart). The analysis must handle both before counting crossings.
    """
    spot = num(row.get("d_index"))
    if spot is None:
        return None

    def usd(k: str) -> float | None:
        x = num(row.get(k))
        return x * spot if x is not None else None

    b_index = num(row.get("b_index"))
    d_ts, b_ts = row.get("d_ts_ms"), row.get("b_ts_ms")
    extra = {
        "index_gap": b_index / spot - 1.0 if b_index is not None else None,
        "dt_ms": int(b_ts) - int(d_ts) if d_ts is not None and b_ts is not None else None,
    }
    return extra | crossing(
        d_bid=usd("d_bid"),
        d_ask=usd("d_ask"),
        d_bid_size=num(row.get("d_bid_size")),
        d_ask_size=num(row.get("d_ask_size")),
        b_bid=num(row.get("b_bid")),
        b_ask=num(row.get("b_ask")),
        b_bid_size=num(row.get("b_bid_size")),
        b_ask_size=num(row.get("b_ask_size")),
        spot=spot,
        fee_rate=fee_rate,
        min_depth=min_depth,
    )


def adjusted_crossing_from_row(
    row: dict[str, Any],
    *,
    gap_source: str = REG_GAP_SOURCE,
    fee_deribit: float = FEE_RATE,
    fee_bybit: float = FEE_RATE,
    min_depth: float = MIN_DEPTH,
) -> dict[str, Any] | None:
    """Registered primary measure: :func:`crossing` after removing the Bybit/Deribit index gap from Bybit's quotes.

    A Bybit option on the USDT index above Deribit's USD index by ``gap`` is, to first order, the Deribit option plus
    delta x gap x F (homogeneity of the option price in forward and strike: (1 + g) C(F, K / (1 + g)) ~ C + g F delta).
    So both Bybit quotes are shifted by ``shift_usd`` = -delta x gap x F before crossing. delta: Black-76 (r = 0) with
    F = ``d_underlying`` (else ``d_index``), T = ``tau_hours`` / (24 x 365), sigma = ``d_mark_iv`` (else ``b_mark_iv``).
    ``gap_source``: "index" -> b_index / d_index - 1 (primary; also absorbs the spot move between the legs' read times),
    "underlying" -> b_underlying / d_underlying - 1 (sensitivity), "none" -> no shift (raw, descriptive only).
    Fees may differ per venue: net = gross - (fee_deribit + fee_bybit) x spot. The second-order error 0.5 x gamma x
    (g F)^2 is ~ $3 per BTC at g = 0.15 %, tau = 10 h, IV 40 %, against a threshold of ~ $120.
    Returns None when the row cannot be measured (no Deribit index, or no gap / delta for a non-"none" source); else
    :func:`crossing`'s fields plus ``gap``, ``delta``, ``shift_usd`` and ``dt_ms``.
    """
    spot = num(row.get("d_index"))
    if spot is None:
        return None
    if gap_source == "none":
        gap, delta, shift = 0.0, None, 0.0
    else:
        if gap_source == "index":
            a, b = num(row.get("b_index")), spot
        elif gap_source == "underlying":
            a, b = num(row.get("b_underlying")), num(row.get("d_underlying"))
        else:
            raise ValueError(f"unknown gap_source {gap_source!r}")
        fwd = num(row.get("d_underlying")) or spot
        sigma = num(row.get("d_mark_iv")) or num(row.get("b_mark_iv"))
        strike, tau = num(row.get("strike")), num(row.get("tau_hours"))
        if a is None or b is None or sigma is None or strike is None or tau is None or row.get("opt") not in ("C", "P"):
            return None
        gap = a / b - 1.0
        delta = black76_greeks(fwd, strike, tau / (24.0 * 365.0), sigma, row["opt"] == "C")["delta"]
        shift = -delta * gap * fwd
        if not math.isfinite(shift):
            return None

    def usd(k: str) -> float | None:
        x = num(row.get(k))
        return x * spot if x is not None else None

    def bybit(k: str) -> float | None:
        x = num(row.get(k))
        return x + shift if x is not None else None

    d_ts, b_ts = row.get("d_ts_ms"), row.get("b_ts_ms")
    extra = {
        "gap": gap,
        "delta": delta,
        "shift_usd": shift,
        "dt_ms": int(b_ts) - int(d_ts) if d_ts is not None and b_ts is not None else None,
    }
    return extra | crossing(
        d_bid=usd("d_bid"),
        d_ask=usd("d_ask"),
        d_bid_size=num(row.get("d_bid_size")),
        d_ask_size=num(row.get("d_ask_size")),
        b_bid=bybit("b_bid"),
        b_ask=bybit("b_ask"),
        b_bid_size=num(row.get("b_bid_size")),
        b_ask_size=num(row.get("b_ask_size")),
        spot=spot,
        fee_rate=(fee_deribit + fee_bybit) / 2.0,  # crossing() charges 2 x fee_rate x spot
        min_depth=min_depth,
    )
