"""ExecutionGuard: pure limits checked before every demo order (ADR 0005 p. 2, ADR 0007). No I/O, time is a parameter.

The limits live in this file on purpose: ``execution/`` is control plane, so neither a merged agent branch nor a field
of external data can raise them. The demo trader builds a ``Proposal`` with ``plan_order`` and sends it only when
``check`` returns no violations; every decision (sent or refused) is written to the append-only audit log.

Risk is the owner's rule: $100 lost at the stop, Bybit taker fees on both sides included. Leverage does not change that
risk, it only sets the margin (owner 21.10): BTC and ETH 20x, the majors 10x, the rest 5x, lowered when the stop would
sit beyond the isolated-margin liquidation price.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

MODE = "testnet"  # the only mode this package knows; see execution/__init__.py
DAY_MS = 86_400_000
TAKER_FEE = 0.00055  # Bybit USDT perpetual taker fee, decimal per side (non-VIP)

LEVERAGE_TOP = {"BTC": 20, "ETH": 20}
MAJORS = frozenset("SOL XRP BNB DOGE ADA TRX LINK AVAX TON SUI DOT LTC BCH XLM HBAR NEAR APT UNI ATOM ETC FIL ICP ARB OP".split())
STABLES = frozenset("USDC USDE FDUSD DAI TUSD USD1 PYUSD USDD BUSD RLUSD USTC".split())


@dataclass(frozen=True)
class GuardConfig:
    mode: str = MODE
    venue: str = "bybit-demo"
    risk_usd: float = 100.0  # lost at the stop including fees (the owner's rule: 1 % of ~$10k)
    max_trades_per_day: int = 5  # owner 21.10: at most 5 entries per UTC day, the highest-scored signals
    max_open_positions: int = 15
    max_open_risk_usd: float = 1500.0  # sum of the losses at the stops of all open positions
    daily_max_loss_usd: float = 500.0  # realised loss of the UTC day; then no new entries until 00:00 UTC
    max_margin_share: float = 0.90  # initial margin of all open positions / equity
    max_notional_usd: float = 50_000.0  # per order; a tighter stop shrinks the size to this cap (risk below $100)
    min_reward_risk: float = 1.5  # after fees
    max_stop_pct: float = 0.10  # stop farther than 10 % of the entry is a broken setup, not a trade
    liq_buffer: float = 0.01  # the stop must be at least 1 % of the price before the estimated liquidation
    mmr: float = 0.01  # maintenance margin rate assumed for the liquidation estimate (Bybit base tier 0.5-1 %)
    orders_per_hour: int = 5


DEFAULT = GuardConfig()


def base_of(symbol: str) -> str:
    return symbol.split("/")[0]


def tier_leverage(symbol: str) -> int:
    b = base_of(symbol)
    return LEVERAGE_TOP.get(b, 10 if b in MAJORS else 5)


def safe_leverage(entry: float, stop: float, cfg: GuardConfig = DEFAULT) -> int:
    """Highest whole leverage whose isolated liquidation (about 1/L - mmr from the entry) lies beyond the stop + buffer."""
    if entry <= 0:
        return 0
    need = abs(entry - stop) / entry + cfg.mmr + cfg.liq_buffer
    return int(math.floor(1 / need)) if need > 0 else 0


@dataclass(frozen=True)
class Proposal:
    symbol: str
    side: str  # 'long' | 'short'
    entry: float
    stop: float
    target: float
    qty: float  # base units (coins), rounded down to the venue's lot step
    leverage: int
    setup: str  # e.g. 'S16'
    link_id: str  # idempotency key sent to the venue as orderLinkId

    @property
    def notional(self) -> float:
        return self.qty * self.entry

    @property
    def margin(self) -> float:
        return self.notional / self.leverage if self.leverage > 0 else math.inf

    @property
    def risk_usd(self) -> float:
        """Loss at the stop with both taker fees."""
        return self.qty * (abs(self.entry - self.stop) + TAKER_FEE * (self.entry + self.stop))

    @property
    def reward_risk(self) -> float:
        loss = abs(self.entry - self.stop) + TAKER_FEE * (self.entry + self.stop)
        gain = abs(self.target - self.entry) - TAKER_FEE * (self.entry + self.target)
        return gain / loss if loss > 0 else 0.0


@dataclass(frozen=True)
class State:
    """What the trader knows at decision time (from the venue and its own book)."""

    now_ms: int
    universe: tuple[str, ...] = ()  # today's tradable symbols (top by turnover)
    halted: bool = False  # kill-switch
    open_symbols: tuple[str, ...] = ()
    trades_today: int = 0
    orders_last_hour: int = 0
    open_risk_usd: float = 0.0
    open_margin_usd: float = 0.0
    day_realised_loss_usd: float = 0.0  # positive = loss
    equity_usd: float = 0.0
    seen_link_ids: tuple[str, ...] = ()  # signals already decided (sent, refused or not selected): one shot each


def size_for_risk(entry: float, stop: float, risk_usd: float, lot_step: float, min_qty: float, max_notional: float = math.inf) -> float:
    """Quantity whose loss at the stop with both taker fees is at most ``risk_usd`` and whose notional is at most
    ``max_notional``, floored to ``lot_step``; 0.0 if below ``min_qty``."""
    per_unit = abs(entry - stop) + TAKER_FEE * (entry + stop)
    if per_unit <= 0 or lot_step <= 0 or risk_usd <= 0 or entry <= 0:
        return 0.0
    units = min(risk_usd / per_unit, max_notional / entry)
    qty = round(math.floor(units / lot_step + 1e-9) * lot_step, 12)
    return qty if qty >= min_qty else 0.0


def plan_order(
    symbol: str,
    side: str,
    entry: float,
    stop: float,
    target: float,
    setup: str,
    link_id: str,
    lot_step: float,
    min_qty: float,
    cfg: GuardConfig = DEFAULT,
) -> Proposal:
    lev = max(0, min(tier_leverage(symbol), safe_leverage(entry, stop, cfg)))
    qty = size_for_risk(entry, stop, cfg.risk_usd, lot_step, min_qty, cfg.max_notional_usd)
    return Proposal(symbol, side, entry, stop, target, qty, lev, setup, link_id)


def check(p: Proposal, s: State, cfg: GuardConfig = DEFAULT) -> list[str]:
    """Violations of the limits; empty = the order may be sent."""
    out: list[str] = []
    if cfg.mode != MODE:
        out.append(f"mode {cfg.mode!r} is not allowed (only {MODE})")
    if s.halted:
        out.append("kill-switch is on")
    if p.symbol not in s.universe or not p.symbol.endswith("/USDT:USDT") or base_of(p.symbol) in STABLES:
        out.append(f"{p.symbol} is not in today's universe")
    if p.side not in ("long", "short"):
        out.append(f"side {p.side!r}")
    sign = 1 if p.side == "long" else -1
    if not (sign * (p.target - p.entry) > 0 and sign * (p.entry - p.stop) > 0):
        out.append("levels: stop and target must be on opposite sides of the entry")
    dist = abs(p.entry - p.stop) / p.entry if p.entry > 0 else math.inf
    if dist > cfg.max_stop_pct:
        out.append(f"stop {dist:.1%} from the entry > {cfg.max_stop_pct:.0%}")
    if p.reward_risk < cfg.min_reward_risk:
        out.append(f"reward/risk {p.reward_risk:.2f} after fees < {cfg.min_reward_risk}")
    if p.leverage < 1:
        out.append("no leverage keeps the liquidation beyond the stop")
    elif p.leverage > tier_leverage(p.symbol) or p.leverage > safe_leverage(p.entry, p.stop, cfg):
        out.append(f"leverage {p.leverage}x above the tier or the liquidation limit")
    if p.qty <= 0:
        out.append("size is zero (risk too small for the lot step)")
    if p.risk_usd > cfg.risk_usd * 1.0001:
        out.append(f"risk {p.risk_usd:.2f} USD > {cfg.risk_usd:.0f}")
    if p.notional > cfg.max_notional_usd * 1.0001:
        out.append(f"notional {p.notional:,.0f} USD > {cfg.max_notional_usd:,.0f}")
    if s.equity_usd <= 0:
        out.append("equity unknown or zero")
    elif s.open_margin_usd + p.margin > cfg.max_margin_share * s.equity_usd:
        out.append(f"margin {s.open_margin_usd + p.margin:,.0f} USD > {cfg.max_margin_share:.0%} of equity")
    if p.symbol in s.open_symbols:
        out.append(f"already in {p.symbol}")
    if len(s.open_symbols) >= cfg.max_open_positions:
        out.append(f"{len(s.open_symbols)} open positions = max")
    if s.trades_today >= cfg.max_trades_per_day:
        out.append(f"{s.trades_today} trades today = max")
    if s.orders_last_hour >= cfg.orders_per_hour:
        out.append(f"{s.orders_last_hour} orders in the last hour = max")
    if s.open_risk_usd + p.risk_usd > cfg.max_open_risk_usd:
        out.append(f"open risk {s.open_risk_usd:.0f} + {p.risk_usd:.0f} USD > {cfg.max_open_risk_usd:.0f}")
    if s.day_realised_loss_usd >= cfg.daily_max_loss_usd:
        out.append(f"realised loss today {s.day_realised_loss_usd:.0f} USD >= {cfg.daily_max_loss_usd:.0f}")
    if p.link_id in s.seen_link_ids:
        out.append("this signal was already decided")
    return out


def day_start_ms(now_ms: int) -> int:
    return now_ms - now_ms % DAY_MS


def open_risk_usd(positions: Sequence[tuple[float, float, float]]) -> float:
    """Loss if every open position hits its stop, fees included: over (qty, entry, stop)."""
    return sum(abs(q) * (abs(e - st) + TAKER_FEE * (e + st)) for q, e, st in positions)


def breakeven_stop(side: str, entry: float) -> float:
    """Stop price that loses nothing after both taker fees (the owner's rule: move here at +30 % ROE)."""
    return entry * (1 + 2 * TAKER_FEE) if side == "long" else entry * (1 - 2 * TAKER_FEE)


def roe(side: str, entry: float, mark: float, leverage: float) -> float:
    """Return on the initial margin, as Bybit shows it (0.30 = +30 %), before fees."""
    if entry <= 0:
        return 0.0
    return (1 if side == "long" else -1) * (mark - entry) / entry * leverage


BE_ROE = 0.30  # owner 21.10: at +30 % ROE the stop goes to the entry


def universe(tickers: Sequence[tuple[str, float]], size: int = 100, min_turnover_usd: float = 5e6) -> tuple[str, ...]:
    """Top ``size`` USDT linear perpetuals by 24h turnover, stablecoins and thin markets out; input (symbol, turnover)."""
    ok = [(sym, t) for sym, t in tickers if sym.endswith("/USDT:USDT") and base_of(sym) not in STABLES and t >= min_turnover_usd]
    ok.sort(key=lambda x: -x[1])
    return tuple(sym for sym, _ in ok[:size])
