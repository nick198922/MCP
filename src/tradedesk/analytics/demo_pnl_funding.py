"""Funding in the demo trader's recorded P&L (pure: no I/O, time and prices passed in).

Item 475 of docs/ideas/IDEA-20261102-standing-improvements.md, report docs/verification/2026-11-26-demo-pnl-mismatch.md.
``demo_trade.pnl_usd`` is the sum of Bybit's closed-pnl ``closedPnl`` (execution/demo_trader.py ``_sync_trade``);
``fees_usd`` holds only the opening and closing trading fees. That ``closedPnl`` also carries the funding the position
paid or received while open is inferred from the data (on 2026-11-26 public funding rebuilt by this module matched the
gap of all 7 flagged trades within 0.09 USD and left no residual beyond tolerance on the 70 trades above 0.01R), not
taken from venue documentation; docs/verification/2026-11-11-s3-stop-exit-accounting.md already named funding the
probable cause of the P&L remainder. The check in :mod:`.demo_pnl_check` compares ``pnl_usd`` with
``sign · (exit − entry) · qty − fees_usd`` and so flags trades that held through large funding. This module rebuilds the
funding of one trade from public funding events so the gap can be split into funding and an unexplained residual:

    gap      = pnl_usd − price_pnl                        (USD; price_pnl from demo_pnl_check.expected_pnl)
    paid     = Σ sign · rate_i · qty · mark_i over funding times open_ms < ts_i < close_ms
               (USD paid by the position; sign +1 long, −1 short; a long pays a positive rate)
    residual = gap + paid                                  (≈ 0 when funding explains the gap)

Rates are decimals per funding interval (0.0001 = 0.01 %), prices in USD, times in unix ms. The venue's own funding rows
are private (not read here), and the price at the funding time is a public candle price, not the venue's mark, so a
residual within the check's tolerance is "explained", not proven to the cent.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .demo_pnl_check import SIGN, TOL_FRAC, TOL_USD, expected_pnl

VARIANTS = ("journal", "price_flagged", "excl_flagged")  # fixed by the card item before any number was computed


def _num(x: object) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def funding_paid(side: str, qty: float, events: Iterable[tuple[int, float, float]], open_ms: int, close_ms: int) -> float:
    """USD the position paid in funding (negative: received): ``Σ sign · rate · qty · price`` over the events
    ``(ts_ms, rate, price)`` with ``open_ms < ts < close_ms``. Events outside the holding window do not count, so later
    data never changes an earlier trade's number."""
    sign = SIGN[side]
    return sum(sign * rate * qty * px for ts, rate, px in events if open_ms < ts < close_ms)


def close_time(exit_px: float, bars: Sequence[Sequence[float]], lo_ms: int, hi_ms: int) -> int | None:
    """Estimated close time: open time (ms) of the first bar ``[ts, o, h, l, c, ...]`` with ``lo_ms <= ts < hi_ms``
    whose low..high range holds ``exit_px``; None when no bar in the window does."""
    for b in sorted(bars, key=lambda x: x[0]):
        if lo_ms <= b[0] < hi_ms and b[3] <= exit_px <= b[2]:
            return int(b[0])
    return None


def close_window(
    exit_px: float,
    m1: Sequence[Sequence[float]],
    h1: Sequence[Sequence[float]],
    opened_ms: int,
    closed_ms: int,
    last_window_ms: int,
    exit_ts: int | None = None,
) -> tuple[int | None, int, str | None]:
    """Estimated close of a trade: ``(close_ms, end_ms, note)``, funding counts for ``opened_ms < ts < end_ms``.

    First the earliest 1m bar in the last cycle ``[closed_ms − last_window_ms, closed_ms)`` holding ``exit_px`` (not
    before the minute of ``exit_ts``; pass it only for a time exit, which cannot close before its exit order); ``end_ms`` is that
    bar's end. Else (the close was booked later than the last cycle) the earliest 1h bar since the opening hour holding
    ``exit_px``, ``end_ms`` its end. Else ``closed_ms`` itself. The 1h fallback was added after the first run of item 475
    (it resolved HANA and IRYS and four trades in all) and leans to the fewest funding events."""
    m, h = 60_000, 3_600_000
    lo = closed_ms - last_window_ms
    if exit_ts:
        lo = max(lo, int(exit_ts) // m * m)
    close_ms = close_time(exit_px, m1, lo, closed_ms)
    if close_ms is not None:
        return close_ms, close_ms + m, None
    h_ms = close_time(exit_px, h1, opened_ms // h * h, closed_ms)
    if h_ms is not None:
        return h_ms, h_ms + h, "close not in the last cycle: earliest 1h bar holding the exit used"
    return None, closed_ms, "close bar not found: closed_ts used"


def priced_events(funding: Iterable[Sequence[float]], bars: Sequence[Sequence[float]]) -> tuple[list[tuple[int, float, float]], list[int]]:
    """Funding ``(ts_ms, rate)`` pairs with the price at each time (:func:`price_at`): ``(events, dropped_ts)``, where
    ``dropped_ts`` are the times with no bar at or before them (those events are left out of :func:`funding_paid`)."""
    events: list[tuple[int, float, float]] = []
    dropped: list[int] = []
    for ts, rate in funding:
        px = price_at(int(ts), bars)
        if px is None:
            dropped.append(int(ts))
        else:
            events.append((int(ts), float(rate), px))
    return events, dropped


def price_at(ts: int, bars: Sequence[Sequence[float]]) -> float | None:
    """Price at ``ts``: the open of the bar starting at ``ts``, else the close of the latest bar starting before it."""
    best: Sequence[float] | None = None
    for b in bars:
        if b[0] == ts:
            return float(b[1])
        if b[0] < ts and (best is None or b[0] > best[0]):
            best = b
    return float(best[4]) if best is not None else None


def decompose(row: Mapping[str, Any], paid: float | None) -> dict[str, Any] | None:
    """Split a closed row's P&L gap into funding and residual (USD and R of ``risk_usd``). None when the price P&L
    cannot be computed. ``explained``: ``|residual| <= max(TOL_FRAC · notional, TOL_USD)``, the check's own tolerance."""
    price = expected_pnl(row)
    recorded, risk, qty = _num(row.get("pnl_usd")), _num(row.get("risk_usd")), _num(row.get("qty"))
    entry = _num(row.get("fill")) or _num(row.get("entry"))
    if price is None or recorded is None or qty is None or entry is None:
        return None
    gap = recorded - price
    tol = max(TOL_FRAC * abs(entry * qty), TOL_USD)
    out: dict[str, Any] = {
        "id": row.get("link_id"), "setup": row.get("setup"), "symbol": row.get("symbol"), "recorded": recorded,
        "price": price, "gap": gap, "tol": tol, "paid": paid, "residual": None, "explained": None,
        "gap_r": gap / risk if risk else None, "funding_r": (-paid / risk) if (risk and paid is not None) else None,
        "residual_r": None,
    }  # fmt: skip
    if paid is not None:
        out["residual"] = gap + paid
        out["explained"] = abs(gap + paid) <= tol
        out["residual_r"] = (gap + paid) / risk if risk else None
    return out


def variant_rows(trades: Iterable[Mapping[str, Any]], flagged: set[str], variant: str) -> list[dict[str, Any]]:
    """Copies of ``trades`` for one of :data:`VARIANTS`: ``journal`` unchanged; ``price_flagged`` the flagged rows'
    ``r_multiple`` (and ``pnl_usd``) replaced by the price P&L ``sign · (exit − entry) · qty − fees_usd`` over
    ``risk_usd``; ``excl_flagged`` the flagged rows left out. Unflagged rows are never changed."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    out: list[dict[str, Any]] = []
    for t in trades:
        row = dict(t)
        if row.get("link_id") in flagged:
            if variant == "excl_flagged":
                continue
            if variant == "price_flagged":
                price, risk = expected_pnl(row), _num(row.get("risk_usd"))
                if price is not None and risk:
                    row["pnl_usd"], row["r_multiple"] = price, price / risk
        out.append(row)
    return out


def mean_funding_r(parts: Iterable[Mapping[str, Any]]) -> dict[str, tuple[int, float]]:
    """Per setup: ``(n, mean funding R)`` over decomposed rows with a funding number (+ = the position received)."""
    by: dict[str, list[float]] = {}
    for p in parts:
        if p.get("funding_r") is not None:
            by.setdefault(str(p.get("setup")), []).append(float(p["funding_r"]))
    return {k: (len(v), statistics.fmean(v)) for k, v in sorted(by.items())}
