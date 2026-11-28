"""Pure helpers on stored liquidation rows (table ``liquidations``). No I/O.

Binance rows written by the old parser (it stored ``l``, the quantity of the order's LAST fill) have ``qty_src``
NULL; the fixed parser (providers/liquidations.py, card IDEA-20261017-doubletop-to-ours, item 4d) marks its rows
``z`` / ``q``. The marker is written by the parser itself, so the boundary is exact whenever the fix was deployed.
"""

from __future__ import annotations

from typing import Any

BINANCE_VENUE = "binanceusdm"  # ccxt id stored in liquidations.venue


def binance_understated(rows: list[dict[str, Any]]) -> int:
    """How many rows are Binance rows written by the old parser, before its fix was deployed (``qty_src`` NULL).

    Those rows hold the last fill ``l`` instead of the order's filled quantity: qty and USD understated several
    times, the USD imbalance biased toward shorts (the l / z share differs by side)."""
    return sum(1 for row in rows if row.get("venue") == BINANCE_VENUE and row.get("qty_src") is None)


def binance_understated_note(n: int | None = None, of: int | None = None) -> str:
    """Caveat for ``binance_understated`` rows; without counts the text is fixed (the alerts job logs a note once)."""
    head = "Binance liquidation rows" if n is None else f"{n} of {of} Binance liquidation rows"
    return (
        f"{head} here were written by the old parser (before its 2026-11-28 fix was deployed; such rows have no "
        "qty_src): they hold only the last fill of each liquidation order (field `l`), so their qty and USD are "
        "understated several times (15-30 % of Coinalyze's coins over 2026-11-13..27) and the USD imbalance is biased "
        "toward shorts (buy); rankings by notional are affected too. These rows cannot be corrected; rows written by "
        "the fixed parser are not affected."
    )
