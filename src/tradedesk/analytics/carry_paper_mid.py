"""Paper carry A / B price P&L re-priced at the recorded book mid (item 383a). Pure functions, no I/O.

The paper engine books a trade's price P&L at the 4h bar close of each leg at cycle time (``model_price`` in the
``side`` rows), while the order books in the same rows were taken ~11-18 minutes later. Here the same price formula is
evaluated on the books' ``mid`` (and, descriptively, the depth-walk ``vwap``) of the same open / close sides, so the
difference ``delta = P_mid - P_model`` is a pure price effect, a fraction of the slot notional. Fees and funding stay the
engine's. Venue names mirror ``collector/carry_paper_engine`` (A: spot ``binance`` + perp ``binanceusdm``; B: leg a
``binanceusdm``, leg b ``bybit``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

PERP_VENUE, SPOT_VENUE, BYBIT = "binanceusdm", "binance", "bybit"
DELTA_MATERIAL = 0.001  # 0.10 % of the slot notional per trade (registration 383a)
MIN_TRADES = 10

Leg = Mapping[str, Any]


def leg_price(leg: Leg, key: str) -> float | None:
    """Price of one leg side in the engine's units: ``model_price`` as recorded, book prices x ``contract_multiplier``."""
    v = leg.get(key)
    if not isinstance(v, int | float) or isinstance(v, bool) or not v > 0:
        return None
    return float(v) if key == "model_price" else float(v) * float(leg.get("contract_multiplier") or 1)


def _pair(strategy: str, legs: Sequence[Leg]) -> tuple[Leg | None, Leg | None]:
    """(reference leg, other leg): A (spot, perp) by ``leg``; B (a, b) by venue."""
    if strategy == "A":
        by = {str(x.get("leg")): x for x in legs}
        return by.get("spot"), by.get("perp")
    by = {str(x.get("venue")): x for x in legs}
    return by.get(PERP_VENUE), by.get(BYBIT)


def price_pnl(
    strategy: str, opened: Sequence[Leg], closed: Sequence[Leg], direction: int | None, key_open: str, key_close: str
) -> float | None:
    """Price P&L as a fraction of notional with the engine's formula, open prices from ``key_open``, close from ``key_close``.

    A: ``(s_c - s_o - (p_c - p_o)) / s_o`` (long spot, short perp). B: ``dir * ((a_c - a_o) - (b_c - b_o)) / a_o``.
    None when a price is missing (no book) or B has no direction."""
    r_o, x_o = _pair(strategy, opened)
    r_c, x_c = _pair(strategy, closed)
    if None in (r_o, x_o, r_c, x_c):
        return None
    ro, xo = leg_price(r_o, key_open), leg_price(x_o, key_open)  # type: ignore[arg-type]
    rc, xc = leg_price(r_c, key_close), leg_price(x_c, key_close)  # type: ignore[arg-type]
    if None in (ro, xo, rc, xc):
        return None
    if strategy == "A":
        return (rc - ro - (xc - xo)) / ro  # type: ignore[operator]
    if direction not in (1, -1):
        return None
    return direction * ((rc - ro) - (xc - xo)) / ro  # type: ignore[operator]


def trade_delta(strategy: str, opened: Sequence[Leg], closed: Sequence[Leg], direction: int | None) -> dict[str, float | None]:
    """Engine price P&L, mid and vwap re-pricing and the open / close split of ``delta = P_mid - P_model``."""
    pm = price_pnl(strategy, opened, closed, direction, "model_price", "model_price")
    pmid = price_pnl(strategy, opened, closed, direction, "mid", "mid")
    po = price_pnl(strategy, opened, closed, direction, "mid", "model_price")
    pc = price_pnl(strategy, opened, closed, direction, "model_price", "mid")
    pv = price_pnl(strategy, opened, closed, direction, "vwap", "vwap")

    def d(x: float | None) -> float | None:
        return None if x is None or pm is None else x - pm

    return {"p_model": pm, "p_mid": pmid, "delta": d(pmid), "delta_open": d(po), "delta_close": d(pc), "p_vwap": pv, "delta_vwap": d(pv)}


def open_gap_delta(strategy: str, opened: Sequence[Leg]) -> float | None:
    """Entry-only effect for an open trade: the hedged pair's relative mispricing at entry,
    ``(x_mid / r_mid) / (x_model / r_model) - 1`` (B: b over a, A: perp over spot); the sign is not the P&L sign."""
    r, x = _pair(strategy, opened)
    if r is None or x is None:
        return None
    rm, xm, rb, xb = leg_price(r, "model_price"), leg_price(x, "model_price"), leg_price(r, "mid"), leg_price(x, "mid")
    if None in (rm, xm, rb, xb):
        return None
    return (xb / rb) / (xm / rm) - 1.0  # type: ignore[operator]


def funding_scale(strategy: str, opened: Sequence[Leg]) -> float | None:
    """Factor turning the engine's funding (normalised by the reference leg's model entry price) into one normalised
    by the book mid at entry: ``model_ref / mid_ref`` (reference = spot for A, leg a for B)."""
    r, _ = _pair(strategy, opened)
    if r is None:
        return None
    m, b = leg_price(r, "model_price"), leg_price(r, "mid")
    return None if m is None or b is None else m / b


def select_trades(positions: Sequence[Mapping[str, Any]], side_rows: Sequence[Mapping[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """Trades as of ``now_ms``: opened by then, ``closed`` only when closed by then; open / close legs from ``side`` rows
    (``cycle_ts``, ``payload``) recorded by then. Nothing after ``now_ms`` is read."""
    legs: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for r in side_rows:
        if int(r["cycle_ts"]) <= now_ms:
            p = r.get("payload") or {}
            legs.setdefault((str(p.get("trade_id")), str(p.get("event"))), []).append(p)
    out = []
    for p in positions:
        if int(p["opened_ts"]) > now_ms:
            continue
        tid = str(p["trade_id"])
        closed = p.get("status") == "closed" and p.get("closed_ts") is not None and int(p["closed_ts"]) <= now_ms
        out.append(
            {"trade_id": tid, "symbol": p.get("symbol"), "closed": closed, "opened_ts": int(p["opened_ts"]),
             "closed_ts": int(p["closed_ts"]) if closed else None, "open": legs.get((tid, "open"), []),
             "close": legs.get((tid, "close"), []) if closed else [], "trade": p.get("trade") if closed else None}
        )  # fmt: skip
    return out


def verdict(
    n: int, mean: float | None, lo: float | None, hi: float | None, *, delta: float = DELTA_MATERIAL, min_n: int = MIN_TRADES
) -> str:
    """Registration 383a: ✓ biased (CI excludes 0, |mean| >= delta), ≈ (CI excludes 0, |mean| < delta),
    ✗ not biased (CI inside [-delta, delta]), ◯ otherwise or n < ``min_n``."""
    if n < min_n or mean is None or lo is None or hi is None:
        return "◯"
    if lo > 0 or hi < 0:
        return "✓" if abs(mean) >= delta else "≈"
    if -delta <= lo and hi <= delta:
        return "✗"
    return "◯"
