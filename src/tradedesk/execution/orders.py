"""Order types of the demo trader (owner 22.10): enter and take profit with limit orders (maker fee), go to the market
only when the situation asks for it. Pure decisions, no I/O, time is a parameter.

- **Entry:** a post-only limit at the best quote on our side (long: bid, short: ask), re-quoted each minute while the
  price stays within ``MAX_CHASE_R`` of the planned entry, for at most ``ENTRY_WAIT_MS``. Then the rest goes in at
  the market only if the price is still that close and the reward/risk after taker fees still passes the guard; else the
  rest is cancelled (the signal is 'missed' when nothing filled). The size only shrinks: a re-quote farther from the stop
  is resized for the same $100 risk.
- **Target:** a resting reduce-only limit at the target, placed once the entry is complete (maker when it fills).
- **Stop and break-even stop:** the venue's stop-market, unchanged: an invalidated idea must leave at once.
- **Time exit:** no hurry, so a post-only limit at the best quote on the exit side, re-quoted for ``EXIT_WAIT_MS``, then
  the market; at once at the market when the price is within ``DANGER_R`` of the stop (better a taker fee than the stop).

Costs (Bybit USDT perpetuals, non-VIP): taker 0.055 %, maker 0.02 % a side. At the demo's sizes (≈ $4–8k notional per
$100 risk) a maker entry and target save ≈ $3–5 a trade (0.03–0.05R). The price: limit entries miss some of the
fastest moves (those are often the winners); ``demo_shadow`` (taken plans resolved at the planned entry) measures it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .guard import TAKER_FEE, size_for_risk

MAKER_FEE = 0.0002  # Bybit USDT perpetual maker fee, decimal per side (non-VIP)
ENTRY_WAIT_MS = 10 * 60_000  # a maker entry rests at most 10 minutes (re-quoted each minute)
EXIT_WAIT_MS = 5 * 60_000  # a time exit rests at most 5 minutes before the market order
MAX_CHASE_R = 0.25  # re-quote / market fallback only while the price is within 0.25R of the planned entry
DANGER_R = 0.25  # a time exit goes to the market at once when the price is within 0.25R of the stop


@dataclass(frozen=True)
class Step:
    action: str  # 'wait' | 'requote' | 'market' | 'cancel'
    price: float = 0.0  # limit price of a re-quote
    qty: float = 0.0  # total order qty of a re-quote (filled part included) or the market qty
    reason: str = ""


def _sign(side: str) -> int:
    return 1 if side == "long" else -1


def maker_quote(side: str, bid: float, ask: float, closing: bool = False) -> float:
    """Best price that rests on the book: a buy joins the bid, a sell the ask (long entry and short exit buy)."""
    buy = (side == "long") != closing
    return bid if buy else ask


def taker_quote(side: str, bid: float, ask: float, closing: bool = False) -> float:
    buy = (side == "long") != closing
    return ask if buy else bid


def reward_risk_at(side: str, entry: float, stop: float, target: float) -> float:
    """Reward/risk after taker fees on both sides (the guard's measure) for an entry at ``entry``."""
    s = _sign(side)
    loss = s * (entry - stop) + TAKER_FEE * (entry + stop)
    gain = s * (target - entry) - TAKER_FEE * (entry + target)
    return gain / loss if loss > 0 else 0.0


def entry_step(
    *,
    side: str,
    planned: float,
    stop: float,
    target: float,
    plan_qty: float,
    order_px: float,
    order_live: bool,
    filled: float,
    bid: float,
    ask: float,
    age_ms: int,
    risk_usd: float,
    lot_step: float,
    min_qty: float,
    min_reward_risk: float,
) -> Step:
    """Next step of a resting maker entry (``order_live`` False: the venue cancelled it, e.g. post-only would take)."""
    s = _sign(side)
    r_unit = abs(planned - stop)
    taker = taker_quote(side, bid, ask)
    maker = maker_quote(side, bid, ask)
    if r_unit <= 0 or s * (taker - stop) <= 0:
        return Step("cancel", reason="price at the stop before the fill: the idea is invalidated")
    ran = s * (taker - planned) / r_unit  # how far the price ran toward the target, in R
    if ran > MAX_CHASE_R:
        return Step("cancel", reason=f"price ran {ran:.2f}R from the planned entry (> {MAX_CHASE_R}R)")
    if order_live and age_ms < ENTRY_WAIT_MS:
        if s * (maker - order_px) <= 0:
            return Step("wait")  # the order is still the best quote on our side (or better)
        qty = max(filled, min(plan_qty, size_for_risk(maker, stop, risk_usd, lot_step, min_qty)))
        return Step("requote", maker, qty, "the quote moved away")
    if reward_risk_at(side, taker, stop, target) < min_reward_risk:
        return Step("cancel", reason=f"reward/risk at the market {reward_risk_at(side, taker, stop, target):.2f} < {min_reward_risk}")
    total = min(plan_qty, size_for_risk(taker, stop, risk_usd, lot_step, min_qty))
    rest = round(total - filled, 12)
    if rest < max(min_qty, lot_step) - 1e-12:
        return Step("cancel", reason="the filled part is the whole size")
    return Step("market", qty=rest, reason="not filled in time; the price is still near the plan")


def exit_step(*, side: str, entry: float, stop: float, order_px: float, order_live: bool, bid: float, ask: float, age_ms: int) -> Step:
    """Next step of a calm (time) exit: ``order_px`` 0 = no exit order yet."""
    s = _sign(side)
    r_unit = abs(entry - stop)
    taker = taker_quote(side, bid, ask, closing=True)
    if r_unit > 0 and s * (taker - stop) <= DANGER_R * r_unit:
        return Step("market", reason=f"price within {DANGER_R}R of the stop")
    if age_ms >= EXIT_WAIT_MS or (order_px and not order_live):
        return Step("market", reason="the limit exit did not fill in time")
    maker = maker_quote(side, bid, ask, closing=True)
    if not order_px or s * (order_px - maker) > 0:  # a long sells at the ask: re-quote when the ask fell below the order
        return Step("requote", maker, reason="no exit order yet" if not order_px else "the quote moved away")
    return Step("wait")
