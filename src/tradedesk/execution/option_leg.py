"""Option leg of the demo trader (owner 23.10, slice 2): the same BTC / ETH setup signal also bought as a long option
("an option instead of a stop"), next to the futures trade, so both legs can be compared on identical signals. Pure
decisions, no I/O, time is a parameter; the orders go through ``demo_trader`` and ``guard.check_option``.

This is a live paired experiment, not a claimed edge: on our data buying options loses the variance risk premium on
average (docs/knowledge/options.md). The premium is the stop, so the option is closed only by its own rules:

- **Trigger:** a futures signal (S14 / S16 / S3) on the BTC or ETH perp whose entry order was sent. Long -> a call,
  short -> a put. It shares the futures signal's daily slot (no extra trade); its premium counts toward the open risk
  and its order toward the orders per hour.
- **Expiry:** the nearest USDT-settled expiry with at least max(``min_dte_days``, ``hold_multiple`` x the setup's time
  exit, the guard's floor) days to go.
- **Strike:** |Black-76 delta| (the venue's mark IV and the expiry's underlying price, r = 0) closest to
  ``target_abs_delta`` within ``min_abs_delta``..``max_abs_delta`` whose book passes the guard's spread filter.
- **Entry:** post-only limit one tick above the bid (still below the ask; on a one-tick book, at the bid), re-quoted
  each minute for ``entry_wait_ms``, then cancelled ('missed' when nothing filled). Sized by ``guard.plan_option``:
  premium + fee <= $100.
- **Exits:** the underlying reaches the setup's target; the setup's time exit; at the latest ``close_before_expiry_ms``
  before expiry. A futures stop-out does not close the option. Close = reduce-only post-only limit at the ask minus a
  tick, re-quoted for ``exit_wait_ms``, then a plain limit at the bid.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..analytics.options import black76_greeks
from .bybit_demo import OptionInstrument, OptionQuote
from .guard import DAY_MS, OPTION_SETTLE, GuardConfig, parse_option_symbol, size_option

MINUTE_MS = 60_000


@dataclass(frozen=True)
class OptionLegConfig:
    """The option leg's rules (owner 23.10). The account limits stay in ``guard.GuardConfig``."""

    bases: tuple[str, ...] = ("BTC", "ETH")  # perps whose signals also get an option leg
    target_abs_delta: float = 0.40
    min_abs_delta: float = 0.30
    max_abs_delta: float = 0.50
    min_dte_days: float = 7.0  # expiry at least this far ...
    hold_multiple: float = 2.0  # ... and at least 2 x the setup's time-exit horizon (4h: 14 days, 1d: 60 days)
    entry_wait_ms: int = 10 * MINUTE_MS  # a resting entry is re-quoted for at most 10 minutes, then cancelled
    exit_wait_ms: int = 5 * MINUTE_MS  # a maker close rests at most 5 minutes, then a limit at the bid
    close_before_expiry_ms: int = DAY_MS  # safety: closed at the latest 1 day before expiry
    target_lookback_bars: int = 10  # 1m bars of the perp read each minute to see the target touched


OPTION_LEG = OptionLegConfig()


def leg_link(futures_link: str, prefix: str = "tdo-") -> str:
    """Link id of the option leg of the futures trade ``futures_link`` ('td-...'): 'tdo-' entry, 'tdx-' maker close,
    'tdg-' its GTC fallback, 'tdb-' close at the bid (Bybit orderLinkId, max 36 chars; room left for '-N')."""
    return (prefix + futures_link[3:])[:33]


def nth_link(link: str, n: int) -> str:
    """The n-th order of one role (a re-placed entry): the first keeps ``link``."""
    return link if n <= 0 else f"{link}-{n}"


def expiry_of(i: OptionInstrument) -> int:
    """The earlier of the venue's delivery time and 08:00 UTC of the date in the symbol (as the guard counts it)."""
    sym = parse_option_symbol(i.symbol)
    return min(i.expiry_ms, sym.expiry_ms) if sym else i.expiry_ms


def min_dte_days(hold_ms: int, cfg: OptionLegConfig = OPTION_LEG, guard: GuardConfig | None = None) -> float:
    floor = guard.option_min_dte_days if guard else 0.0
    return max(cfg.min_dte_days, cfg.hold_multiple * hold_ms / DAY_MS, floor)


def pick_expiry(
    instruments: Iterable[OptionInstrument], now_ms: int, hold_ms: int, cfg: OptionLegConfig = OPTION_LEG, guard: GuardConfig | None = None
) -> int | None:
    """The nearest USDT-settled expiry (ms) with at least ``min_dte_days`` to go; None when there is none."""
    need = min_dte_days(hold_ms, cfg, guard)
    ok = sorted({expiry_of(i) for i in instruments if i.settle == OPTION_SETTLE and (expiry_of(i) - now_ms) / DAY_MS >= need})
    return ok[0] if ok else None


def leg_delta(i: OptionInstrument, q: OptionQuote, now_ms: int) -> float | None:
    """Black-76 delta (r = 0 on the expiry's underlying price, T = days / 365, the venue's mark IV); None without data."""
    t = (expiry_of(i) - now_ms) / DAY_MS / 365.0
    if t <= 0 or not q.mark_iv > 0 or not q.underlying > 0:
        return None
    return black76_greeks(q.underlying, i.strike, t, q.mark_iv, i.is_call)["delta"]


def book_spread(bid: float, ask: float) -> float:
    """(ask - bid) / mid; infinite without a two-sided book (as ``guard.OptionProposal.spread``)."""
    return (ask - bid) / ((ask + bid) / 2) if bid > 0 and ask > bid else math.inf


@dataclass(frozen=True)
class StrikePick:
    inst: OptionInstrument
    quote: OptionQuote
    delta: float


def pick_strike(
    instruments: Iterable[OptionInstrument],
    quotes: dict[str, OptionQuote],
    expiry_ms: int,
    is_call: bool,
    now_ms: int,
    cfg: OptionLegConfig = OPTION_LEG,
    max_spread: float = 0.10,
) -> tuple[StrikePick | None, str]:
    """(pick, '') or (None, why). The strike of ``expiry_ms`` whose |delta| is closest to ``target_abs_delta`` and whose
    book passes ``max_spread``; else the next closest within ``min_abs_delta``..``max_abs_delta``."""
    kind = "call" if is_call else "put"
    cands = []
    for i in instruments:
        q = quotes.get(i.symbol)
        if q is None or i.is_call != is_call or expiry_of(i) != expiry_ms or i.settle != OPTION_SETTLE:
            continue
        d = leg_delta(i, q, now_ms)
        if d is None or not cfg.min_abs_delta <= abs(d) <= cfg.max_abs_delta:
            continue
        cands.append((abs(abs(d) - cfg.target_abs_delta), i.strike, StrikePick(i, q, d)))
    if not cands:
        return None, f"no {kind} with |delta| {cfg.min_abs_delta:g}-{cfg.max_abs_delta:g} on this expiry"
    cands.sort(key=lambda x: (x[0], x[1]))
    for _, _, pick in cands:
        if book_spread(pick.quote.bid, pick.quote.ask) <= max_spread:
            return pick, ""
    return None, f"{len(cands)} {kind}(s) with |delta| {cfg.min_abs_delta:g}-{cfg.max_abs_delta:g}, none with a spread <= {max_spread:.0%}"


def _on_tick(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 10) if tick > 0 else x


def entry_price(bid: float, ask: float, tick: float) -> float:
    """Buy limit one tick above the bid, kept below the ask (a one-tick book: at the bid); 0 without a two-sided book."""
    if not (bid > 0 and ask > bid):
        return 0.0
    px = _on_tick(bid + tick, tick)
    return px if px < ask - 1e-9 else bid


def close_price(bid: float, ask: float, tick: float) -> float:
    """Sell limit one tick below the ask, kept above the bid (a one-tick book: at the ask); 0 without a two-sided book."""
    if not (bid > 0 and ask > bid):
        return 0.0
    px = _on_tick(ask - tick, tick)
    return px if px > bid + 1e-9 else ask


@dataclass(frozen=True)
class LegStep:
    action: str  # entry: wait | requote | replace | cancel | complete;  close: wait | place | requote | to_bid
    price: float = 0.0
    qty: float = 0.0  # total size of the order (entry) or 0
    reason: str = ""


def entry_leg_step(
    *,
    order_px: float,
    order_live: bool,
    held: float,
    plan_qty: float,
    bid: float,
    ask: float,
    tick: float,
    index: float,
    age_ms: int,
    risk_usd: float,
    lot_step: float,
    min_qty: float,
    halted: bool,
    cfg: OptionLegConfig = OPTION_LEG,
) -> LegStep:
    """Next step of a resting option entry. ``held``: the venue's position in this option (what has filled).
    A re-placed order (the venue cancelled a post-only one) is only sent while nothing has filled, so one order is
    ever partly filled."""
    if held >= plan_qty - 1e-9:
        return LegStep("complete", reason="filled")
    if halted:
        return LegStep("cancel", reason="kill-switch is on: no new option entries")
    if age_ms >= cfg.entry_wait_ms:
        return LegStep("cancel", reason=f"not filled in {cfg.entry_wait_ms // MINUTE_MS} min")
    px = entry_price(bid, ask, tick)
    if px <= 0:
        return LegStep("wait", reason="no two-sided quote")
    # a dearer re-quote shrinks the size so the whole leg stays within the premium cap (fills so far were cheaper)
    total = min(plan_qty, size_option(px, index, risk_usd, lot_step, min_qty))
    if total <= held + 1e-12:
        return LegStep("cancel", reason="the filled part is the whole size at the new price")
    if order_live:
        if bid <= order_px + 1e-9:
            return LegStep("wait")  # our order is the best bid (or the bid fell below it)
        return LegStep("requote", px, total, "outbid")
    if held > 0:
        return LegStep("cancel", reason="the venue cancelled the rest of the order")
    return LegStep("replace", px, total, "the venue cancelled the post-only order")


def exit_reason(
    *, is_call: bool, target: float, extreme: float, now_ms: int, time_exit_ms: int, expiry_ms: int, cfg: OptionLegConfig = OPTION_LEG
) -> str | None:
    """Why the option leg closes now: 'target' (the underlying reached the setup's target: ``extreme`` is the highest
    price since the fill for a call, the lowest for a put), 'time' (the setup's time exit), 'expiry' (a day before
    expiry); None = hold. A futures stop-out is not a reason: the premium is the stop."""
    if extreme > 0 and (extreme >= target if is_call else extreme <= target):
        return "target"
    if now_ms >= time_exit_ms:
        return "time"
    if expiry_ms - now_ms <= cfg.close_before_expiry_ms:
        return "expiry"
    return None


def underlying_extreme(is_call: bool, bars: Sequence[Sequence[float]], since_ms: int, last: float) -> float:
    """Highest high (call) / lowest low (put) of the 1m ``bars`` (ccxt rows) that end after ``since_ms``, and ``last``."""
    xs = [float(b[2] if is_call else b[3]) for b in bars if b[0] + MINUTE_MS > since_ms]
    if last > 0:
        xs.append(last)
    xs = [x for x in xs if x > 0]
    if not xs:
        return 0.0
    return max(xs) if is_call else min(xs)


def close_leg_step(
    *,
    phase: str,
    order_px: float,
    order_live: bool,
    order_rejected: bool,
    bid: float,
    ask: float,
    tick: float,
    age_ms: int,
    cfg: OptionLegConfig = OPTION_LEG,
) -> LegStep:
    """Next step of an option close. ``phase`` 'maker': reduce-only post-only limit at the ask minus a tick (or its GTC
    fallback), re-quoted for ``exit_wait_ms``; then 'bid': a plain reduce-only limit at the bid, followed down."""
    if phase == "maker":
        px = close_price(bid, ask, tick)
        if age_ms >= cfg.exit_wait_ms:
            return LegStep("to_bid", bid, reason="the maker close did not fill in time")
        if order_rejected and px > 0:
            return LegStep("fallback", px, reason="the venue rejected the post-only reduce-only order")
        if order_px and not order_live:
            return LegStep("to_bid", bid, reason="the maker close left the book")
        if px <= 0:
            return LegStep("to_bid", bid, reason="no two-sided quote") if bid > 0 else LegStep("wait", reason="no bid")
        if not order_px:
            return LegStep("place", px, reason="close starts")
        if ask < order_px - 1e-9:
            return LegStep("requote", px, reason="undercut")
        return LegStep("wait")
    if bid <= 0:
        return LegStep("wait", reason="no bid")
    if not order_px or not order_live:
        return LegStep("place", bid, reason="sell at the bid")
    if bid < order_px - 1e-9:
        return LegStep("requote", bid, reason="the bid fell")
    return LegStep("wait")


def fills_total(rows: Iterable[dict[str, Any]]) -> tuple[float, float, float]:
    """(qty, value, fee) of Bybit /v5/execution/list rows of type Trade (USDT; fee as the venue charged it)."""
    q = v = f = 0.0
    for r in rows:
        if str(r.get("execType") or "Trade") != "Trade":
            continue
        eq, px = _num(r.get("execQty")), _num(r.get("execPrice"))
        q += eq
        v += eq * px
        f += _num(r.get("execFee"))
    return q, v, f


def _num(x: Any) -> float:
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0
