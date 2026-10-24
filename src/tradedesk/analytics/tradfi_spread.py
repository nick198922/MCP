"""TradFi perps (Binance): quoted spread and the early cost per round trip (pure functions).

Used on the rows of collector/tradfi_spread.py (IDEA-20261024-tradfi-perp-offhours-reopen, early kill (b)). No I/O;
every time is passed in. Spreads and costs are decimals (0.001 = 0.1 %).

Cost per round trip on one NYSE session (BACKLOG «Ранній кіл»): taker fee 2 x 0.045 % + the FULL quoted spread at the
entry (open - 5 min = 09:25 ET) + the FULL quoted spread at the exit (open + 60 min = 10:30 ET). A full spread per leg is
deliberately conservative (a taker crossing a symmetric book pays half the spread against the mid per leg) and covers
best-level slippage only: a size larger than the best level pays more (depth is not collected here; see the
``bookDepth`` archive). Times come from the NYSE calendar of `analytics/tradfi_offhours.py` (zoneinfo, DST-safe); the
same ET times are used for commodity perps, whose own venues keep other hours.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from .tradfi_offhours import MIN_MS, nyse_sessions

TAKER_FEE = 0.00045  # Binance USD-M taker, per side (decimal)
ROUND_TRIP_FEE = 2 * TAKER_FEE  # 0.09 %
ENTRY_BEFORE_OPEN_MIN = 5  # 09:25 ET
EXIT_AFTER_OPEN_MIN = 60  # 10:30 ET
MAX_STALE_MS = 5 * MIN_MS  # a quote older than this at the target time counts as not observed
TRADFI_CONTRACT = "TRADIFI_PERPETUAL"
TRADFI_UNDERLYINGS = ("EQUITY", "COMMODITY")  # EQUITY = US stocks/ETFs; HK_/KR_/CN_EQUITY, FX, PREMARKET excluded
QUOTE = "USDT"


def is_tradfi_perp(info: Mapping[str, Any]) -> bool:
    """A USDT-quoted TradFi perp on a US equity/ETF or a commodity (one ``exchangeInfo`` symbol entry). Same universe as
    ``scripts/verify_tradfi_offhours_reopen.py`` (``universe``)."""
    return (
        info.get("contractType") == TRADFI_CONTRACT
        and info.get("underlyingType") in TRADFI_UNDERLYINGS
        and str(info.get("symbol") or "").endswith(QUOTE)
    )


def relative_spread(bid: float | None, ask: float | None) -> float | None:
    """Quoted spread (ask - bid) / mid, mid = (ask + bid) / 2. None when a side is missing, non-positive or the book is
    crossed (ask < bid)."""
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (ask - bid) / ((ask + bid) / 2.0)


def spread_at(quotes: Sequence[tuple[int, float | None, float | None]], t_ms: int, *, max_stale_ms: int = MAX_STALE_MS) -> float | None:
    """As-of quoted spread at ``t_ms``: the last quote with time <= ``t_ms`` (nothing after ``t_ms`` is read).

    ``quotes`` are ``(time_ms, bid, ask)`` sorted by time. None if there is no such quote, it is older than
    ``max_stale_ms`` (a gap in the collection) or its spread is undefined (see :func:`relative_spread`)."""
    i = bisect_right(quotes, t_ms, key=lambda q: q[0]) - 1
    if i < 0 or t_ms - quotes[i][0] > max_stale_ms:
        return None
    return relative_spread(quotes[i][1], quotes[i][2])


def round_trip_cost(entry_spread: float, exit_spread: float, *, fee: float = ROUND_TRIP_FEE) -> float:
    """Cost of one round trip as a fraction of notional: ``fee`` + full entry spread + full exit spread."""
    return fee + entry_spread + exit_spread


def session_costs(
    quotes: Sequence[tuple[int, float | None, float | None]],
    start: date,
    end: date,
    *,
    fee: float = ROUND_TRIP_FEE,
    max_stale_ms: int = MAX_STALE_MS,
) -> list[dict[str, Any]]:
    """Round-trip cost of one symbol on every NYSE session with ``start <= day <= end``.

    ``quotes``: ``(time_ms, bid, ask)`` of that symbol sorted by time (e.g. the collector's ``ts * 1000`` or
    ``book_ms`` with ``bid``/``ask``). One dict per session: ``day``, ``entry_ms`` / ``exit_ms`` (09:25 / 10:30 ET),
    ``entry_spread``, ``exit_spread`` and ``cost`` (None when either spread is not observed)."""
    out = []
    for s in nyse_sessions(start, end):
        t_in = s.open_ms - ENTRY_BEFORE_OPEN_MIN * MIN_MS
        t_out = s.open_ms + EXIT_AFTER_OPEN_MIN * MIN_MS
        e = spread_at(quotes, t_in, max_stale_ms=max_stale_ms)
        x = spread_at(quotes, t_out, max_stale_ms=max_stale_ms)
        cost = round_trip_cost(e, x, fee=fee) if e is not None and x is not None else None
        out.append({"day": s.day, "entry_ms": t_in, "exit_ms": t_out, "entry_spread": e, "exit_spread": x, "cost": cost})
    return out
