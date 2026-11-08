"""Closed demo trades whose recorded P&L disagrees with their prices (pure: no I/O, numbers passed in).

Item 140 of docs/ideas/IDEA-20261102-standing-improvements.md. The demo trader sums the venue's closed-pnl rows into
``pnl_usd`` (fees included) but takes the exit price from the first row only (execution/demo_trader.py), so a trade
closed in parts (POWR S12 in docs/verification/2026-11-02-demo-execution-quality.md: +1.16R between the P&L and the
prices) carries an exit price and reason that describe only part of the position. Such rows are named in
scripts/checkpoint_report.py so the owner knows which R values to look at; nothing here changes a row.

For a ``closed`` ``demo_trade`` row the expected P&L is ``sign · (exit − entry) · qty − fees_usd``: ``sign`` is +1 for
``long`` and −1 for ``short``, ``entry`` is the average fill (``fill``) or, without one, the planned ``entry``, and
``fees_usd`` is subtracted only when the row has it. A row is flagged when ``|pnl_usd − expected|`` exceeds
``max(TOL_FRAC · entry · qty, TOL_USD)``. Rows missing ``pnl_usd``, ``exit``, ``qty`` or both entry prices, or with
another side, are not checked (counted by :func:`unchecked`). A disagreement is a sign to look, not proof of a wrong
R: for a partial close the summed P&L is the venue's and is likely right, the exit price is what is off.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

TOL_FRAC = 0.005  # 0.5 % of the entry notional
TOL_USD = 0.5  # floor of the tolerance, USD
SIGN = {"long": 1.0, "short": -1.0}


def _num(x: object) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def _entry(row: Mapping[str, Any]) -> float | None:
    """The average fill or, without one, the planned entry price."""
    fill = _num(row.get("fill"))
    return fill if fill is not None else _num(row.get("entry"))


def expected_pnl(row: Mapping[str, Any]) -> float | None:
    """``sign · (exit − entry) · qty − fees_usd`` (USD) of one closed row; None when a needed field is missing."""
    sign = SIGN.get(row.get("side"))  # type: ignore[arg-type]
    entry, exit_, qty = _entry(row), _num(row.get("exit")), _num(row.get("qty"))
    if sign is None or entry is None or exit_ is None or qty is None:
        return None
    return sign * (exit_ - entry) * qty - (_num(row.get("fees_usd")) or 0.0)


def _checked(row: Mapping[str, Any]) -> tuple[float, float, float] | None:
    """``(recorded, expected, notional)`` in USD of a ``closed`` row the check can judge; None for any other row.
    The one place that decides which rows are checked: :func:`pnl_mismatches` and :func:`unchecked` both use it."""
    if row.get("status") != "closed":
        return None
    recorded, expected = _num(row.get("pnl_usd")), expected_pnl(row)
    entry, qty = _entry(row), _num(row.get("qty"))
    if recorded is None or expected is None or entry is None or qty is None:  # expected set implies entry and qty set
        return None
    return recorded, expected, abs(entry * qty)


def pnl_mismatches(rows: Iterable[Mapping[str, Any]], tol_frac: float = TOL_FRAC, tol_usd: float = TOL_USD) -> list[dict[str, Any]]:
    """The checked ``closed`` rows whose ``pnl_usd`` is off the price P&L by more than ``max(tol_frac · notional,
    tol_usd)``, largest gap first. Each: ``id`` (link_id), ``setup``, ``symbol``, ``recorded`` and ``expected`` (USD),
    ``gap`` (recorded − expected, USD), ``tol`` (USD)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        checked = _checked(r)
        if checked is None:
            continue
        recorded, expected, notional = checked
        tol = max(tol_frac * notional, tol_usd)
        gap = recorded - expected
        if abs(gap) > tol:
            out.append(
                {
                    "id": r.get("link_id"), "setup": r.get("setup"), "symbol": r.get("symbol"), "recorded": recorded,
                    "expected": expected, "gap": gap, "tol": tol,
                }
            )  # fmt: skip
    return sorted(out, key=lambda t: -abs(t["gap"]))


def unchecked(rows: Iterable[Mapping[str, Any]]) -> int:
    """How many ``closed`` rows lack a field the check needs (P&L, exit, qty, an entry price, a known side)."""
    return sum(1 for r in rows if r.get("status") == "closed" and _checked(r) is None)
