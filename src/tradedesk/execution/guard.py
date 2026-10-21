"""ExecutionGuard: pure limits checked before every order (ADR 0005 p. 2). No I/O, time comes as a parameter.

The limits live in this file on purpose: ``execution/`` is control plane, so neither a merged agent branch nor a field
of external data can raise them. The demo trader builds a ``Proposal``, sizes it with ``size_for_risk`` and sends it only
when ``check`` returns no violations; every decision (sent or refused) is written to the append-only audit log.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

MODE = "testnet"  # the only mode this package knows; see execution/__init__.py
DAY_MS = 86_400_000


@dataclass(frozen=True)
class GuardConfig:
    mode: str = MODE
    venue: str = "bybit-demo"
    symbols: tuple[str, ...] = (
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "XRP/USDT:USDT",
        "BNB/USDT:USDT",
        "DOGE/USDT:USDT",
    )
    risk_usd: float = 100.0  # per trade at the stop, the owner's real rule (1 % of ~$10k)
    max_notional_usd: float = 10_000.0  # per order; a tight stop must not turn $100 of risk into a huge position
    max_leverage: float = 5.0
    max_open_positions: int = 5
    max_orders_per_hour: int = 6
    daily_max_loss_usd: float = 300.0  # realised + open at the stops, UTC day; then no new entries until 00:00 UTC
    min_reward_risk: float = 1.5  # target distance / stop distance, after fees
    max_stop_pct: float = 0.10  # stop farther than 10 % of the entry is a broken setup, not a trade


DEFAULT = GuardConfig()


@dataclass(frozen=True)
class Proposal:
    symbol: str
    side: str  # 'long' | 'short'
    entry: float
    stop: float
    target: float
    qty: float  # base units (coins), already rounded to the venue's lot step
    setup: str  # e.g. 'S16'
    link_id: str  # idempotency key sent to the venue as orderLinkId


@dataclass(frozen=True)
class State:
    """What the trader knows at decision time (from the venue and the audit log)."""

    now_ms: int
    halted: bool = False  # kill-switch
    open_symbols: tuple[str, ...] = ()
    orders_last_hour: int = 0
    day_loss_usd: float = 0.0  # positive number = loss today (realised + open risk at the stops)
    equity_usd: float = 0.0
    sent_link_ids: tuple[str, ...] = field(default_factory=tuple)


def size_for_risk(entry: float, stop: float, risk_usd: float, lot_step: float, min_qty: float) -> float:
    """Quantity whose loss at the stop is at most ``risk_usd``, floored to ``lot_step``; 0.0 if below ``min_qty``."""
    dist = abs(entry - stop)
    if dist <= 0 or lot_step <= 0 or risk_usd <= 0:
        return 0.0
    steps = math.floor(risk_usd / dist / lot_step + 1e-9)
    qty = round(steps * lot_step, 12)
    return qty if qty >= min_qty else 0.0


def check(p: Proposal, s: State, cfg: GuardConfig = DEFAULT) -> list[str]:
    """Violations of the limits; empty = the order may be sent."""
    out: list[str] = []
    if cfg.mode != MODE:
        out.append(f"mode {cfg.mode!r} is not allowed (only {MODE})")
    if s.halted:
        out.append("kill-switch is on")
    if p.symbol not in cfg.symbols:
        out.append(f"{p.symbol} is not in the allow-list")
    if p.side not in ("long", "short"):
        out.append(f"side {p.side!r}")
    sign = 1 if p.side == "long" else -1
    if not (sign * (p.target - p.entry) > 0 and sign * (p.entry - p.stop) > 0):
        out.append("levels: stop and target must be on opposite sides of the entry")
    dist = abs(p.entry - p.stop)
    if p.entry > 0 and dist / p.entry > cfg.max_stop_pct:
        out.append(f"stop {dist / p.entry:.1%} from the entry > {cfg.max_stop_pct:.0%}")
    if dist > 0 and abs(p.target - p.entry) / dist < cfg.min_reward_risk:
        out.append(f"reward/risk {abs(p.target - p.entry) / dist:.2f} < {cfg.min_reward_risk}")
    if p.qty <= 0:
        out.append("size is zero (risk too small for the lot step)")
    risk = p.qty * dist
    if risk > cfg.risk_usd * 1.0001:
        out.append(f"risk {risk:.2f} USD > {cfg.risk_usd:.0f}")
    notional = p.qty * p.entry
    if notional > cfg.max_notional_usd:
        out.append(f"notional {notional:,.0f} USD > {cfg.max_notional_usd:,.0f}")
    if s.equity_usd > 0 and notional / s.equity_usd > cfg.max_leverage:
        out.append(f"leverage {notional / s.equity_usd:.1f}x > {cfg.max_leverage:.0f}x")
    if p.symbol in s.open_symbols:
        out.append(f"already in {p.symbol}")
    if len(s.open_symbols) >= cfg.max_open_positions:
        out.append(f"{len(s.open_symbols)} open positions = max")
    if s.orders_last_hour >= cfg.max_orders_per_hour:
        out.append(f"{s.orders_last_hour} orders in the last hour = max")
    if s.day_loss_usd + cfg.risk_usd > cfg.daily_max_loss_usd:
        out.append(f"daily loss {s.day_loss_usd:.0f} + this risk would pass {cfg.daily_max_loss_usd:.0f} USD")
    if p.link_id in s.sent_link_ids:
        out.append("this signal was already sent")
    return out


def day_start_ms(now_ms: int) -> int:
    return now_ms - now_ms % DAY_MS


def open_risk_usd(positions: Sequence[tuple[float, float, float]]) -> float:
    """Loss if every open position hits its stop: sum of qty * |entry - stop| over (qty, entry, stop)."""
    return sum(abs(q) * abs(e - st) for q, e, st in positions)
