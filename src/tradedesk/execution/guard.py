"""ExecutionGuard: pure limits checked before every demo order (ADR 0005 p. 2, ADR 0007). No I/O, time is a parameter.

The limits live in this file on purpose: ``execution/`` is control plane, so neither a merged agent branch nor a field
of external data can raise them. The demo trader builds a ``Proposal`` with ``plan_order`` and sends it only when
``check`` returns no violations; every decision (sent or refused) is written to the append-only audit log.

Risk is the owner's rule: $100 lost at the stop, Bybit taker fees on both sides included. Leverage does not change that
risk, it only sets the margin (owner 21.10): BTC and ETH 20x, the majors 10x, the rest 5x, capped by the venue's maximum
for the coin. The account is on cross margin (owner 21.10), so liquidation is an account matter: the maintenance margin
of all positions plus the loss if every stop is hit must stay within half of the equity, so the stops always come first.

Options (owner 23.10, slice 1): ``plan_option`` / ``check_option`` allow only buying BTC or ETH USDT-settled options to
open, at least 4 days before expiry, on a book no wider than ``option_max_spread``; premium plus fee is the risk, sized
to $100. They share the account caps (daily trades and entries, orders per hour, open risk, the day's loss) with futures.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

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
    # owner 27.10 (Q-20261025-2): testing the setups comes first, so the day's limits follow the demo trader's slots
    # (2 per setup for 7 setups + a reserve of 3) and do not brake it on their own; the cross-margin checks below stay
    max_trades_per_day: int = 17  # today's entries still at risk (break-even frees a slot), = demo_trader slots
    max_entries_per_day: int = 30  # hard ceiling of entries per UTC day, freed slots included
    max_open_positions: int = 30
    max_open_risk_usd: float = 3000.0  # sum of the losses at the stops of all open positions
    daily_max_loss_usd: float = 1700.0  # realised loss of the UTC day (= a full day of stops); then no entries until 00:00 UTC
    max_margin_share: float = 0.90  # initial margin of all open positions / equity
    max_maint_and_risk_share: float = 0.50  # (maintenance margin of all + loss at all stops) / equity: cross-margin safety
    max_notional_usd: float = 50_000.0  # per order; a tighter stop shrinks the size to this cap (risk below $100)
    max_order_equity_share: float = 1.0  # per order also at most 1x equity (cross margin: a gap fills past the stop)
    max_open_notional_share: float = (
        6.0  # all open positions together at most 6x equity (owner 27.10: slots for tests; 3x bound at ~6 trades)
    )
    min_reward_risk: float = 1.5  # after fees
    max_stop_pct: float = 0.10  # stop farther than 10 % of the entry is a broken setup, not a trade
    orders_per_hour: int = 15  # a 4h bar can bring several setups' entries in one cycle
    # options (owner 23.10, slice 1): buy to open only, premium paid + fee = the risk
    option_bases: tuple[str, ...] = ("BTC", "ETH")
    option_min_dte_days: float = 4.0  # owner's rule: no option buys with 1-3 days to expiry (expiry 08:00 UTC)
    option_max_spread: float = 0.10  # (ask - bid) / mid of the option's book at decision time


DEFAULT = GuardConfig()


def base_of(symbol: str) -> str:
    return symbol.split("/")[0]


def tier_leverage(symbol: str) -> int:
    b = base_of(symbol)
    return LEVERAGE_TOP.get(b, 10 if b in MAJORS else 5)


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
    mmr: float = 0.10  # maintenance margin rate of the venue's first risk tier (decimal); unknown = a cautious 10 %

    @property
    def notional(self) -> float:
        return self.qty * self.entry

    @property
    def margin(self) -> float:
        return self.notional / self.leverage if self.leverage > 0 else math.inf

    @property
    def maintenance(self) -> float:
        return self.notional * self.mmr

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
    trades_today: int = 0  # today's entries still at risk: no break-even stop yet, or closed at a loss
    entries_today: int = 0  # all of today's entries
    orders_last_hour: int = 0
    open_risk_usd: float = 0.0
    open_margin_usd: float = 0.0
    open_maint_usd: float = 0.0  # maintenance margin of the open positions
    open_notional_usd: float = 0.0  # size of the open positions at the entry price
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
    *,
    mmr: float = 0.10,
    venue_max_leverage: float = 100.0,
    tier_limit_usd: float = math.inf,
    equity_usd: float = math.inf,
) -> Proposal:
    """Size for the risk (capped by the order cap and the venue's first risk tier, where ``mmr`` holds) and the leverage
    (the owner's tier, capped by the venue's maximum for the coin)."""
    lev = max(0, min(tier_leverage(symbol), int(venue_max_leverage)))
    cap = min(cfg.max_notional_usd, tier_limit_usd, cfg.max_order_equity_share * equity_usd)
    qty = size_for_risk(entry, stop, cfg.risk_usd, lot_step, min_qty, cap)
    return Proposal(symbol, side, entry, stop, target, qty, lev, setup, link_id, mmr)


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
    if not 1 <= p.leverage <= tier_leverage(p.symbol):
        out.append(f"leverage {p.leverage}x outside 1..{tier_leverage(p.symbol)}x")
    if p.qty <= 0:
        out.append("size is zero (risk too small for the lot step)")
    if p.risk_usd > cfg.risk_usd * 1.0001:
        out.append(f"risk {p.risk_usd:.2f} USD > {cfg.risk_usd:.0f}")
    if p.notional > cfg.max_notional_usd * 1.0001:
        out.append(f"notional {p.notional:,.0f} USD > {cfg.max_notional_usd:,.0f}")
    if s.equity_usd <= 0:
        out.append("equity unknown or zero")
    else:
        if s.open_margin_usd + p.margin > cfg.max_margin_share * s.equity_usd:
            out.append(f"margin {s.open_margin_usd + p.margin:,.0f} USD > {cfg.max_margin_share:.0%} of equity")
        if p.notional > cfg.max_order_equity_share * s.equity_usd * 1.0001:
            out.append(f"notional {p.notional:,.0f} USD > {cfg.max_order_equity_share:g}x equity")
        total = s.open_notional_usd + p.notional
        if total > cfg.max_open_notional_share * s.equity_usd:
            out.append(f"open size {total:,.0f} USD > {cfg.max_open_notional_share:g}x equity")
        out += _cross_margin(p.maintenance, p.risk_usd, s, cfg)
    return out + _account_caps(p.symbol, p.link_id, p.risk_usd, s, cfg)


def _cross_margin(maintenance: float, risk_usd: float, s: State, cfg: GuardConfig) -> list[str]:
    need = s.open_maint_usd + maintenance + s.open_risk_usd + risk_usd
    if need > cfg.max_maint_and_risk_share * s.equity_usd:
        return [f"maintenance + loss at all stops {need:,.0f} USD > {cfg.max_maint_and_risk_share:.0%} of equity (cross margin)"]
    return []


def _account_caps(symbol: str, link_id: str, risk_usd: float, s: State, cfg: GuardConfig) -> list[str]:
    """Caps of the whole demo account, shared by futures and options: one position per symbol, open positions, the
    day's trades and entries, orders per hour, open risk, the day's realised loss, one decision per signal."""
    out: list[str] = []
    if symbol in s.open_symbols:
        out.append(f"already in {symbol}")
    if len(s.open_symbols) >= cfg.max_open_positions:
        out.append(f"{len(s.open_symbols)} open positions = max")
    if s.trades_today >= cfg.max_trades_per_day:
        out.append(f"{s.trades_today} trades at risk today = max")
    if s.entries_today >= cfg.max_entries_per_day:
        out.append(f"{s.entries_today} entries today = max")
    if s.orders_last_hour >= cfg.orders_per_hour:
        out.append(f"{s.orders_last_hour} orders in the last hour = max")
    if s.open_risk_usd + risk_usd > cfg.max_open_risk_usd:
        out.append(f"open risk {s.open_risk_usd:.0f} + {risk_usd:.0f} USD > {cfg.max_open_risk_usd:.0f}")
    if s.day_realised_loss_usd >= cfg.daily_max_loss_usd:
        out.append(f"realised loss today {s.day_realised_loss_usd:.0f} USD >= {cfg.daily_max_loss_usd:.0f}")
    if link_id in s.seen_link_ids:
        out.append("this signal was already decided")
    return out


# ---------------------------------------------------------------- options (owner 23.10, slice 1: plumbing and safety)
# Long options only: buy to open with a post-only limit; the premium paid plus the opening fee is the whole risk (a long
# option cannot lose more), so it is sized for ``risk_usd`` like a futures stop. No selling or writing in this slice.

OPTION_SETTLE = "USDT"  # only USDT-settled Bybit options were checked on the demo (23.10)
OPTION_EXPIRY_HOUR_UTC = 8  # Bybit and Deribit options expire at 08:00 UTC
# Bybit's published non-VIP option fees: taker 0.03 % of the index per contract, capped at 7 % of the option's price.
# The taker rate is used even for post-only entries (maker), so the fee in the risk is an upper bound. Not yet checked
# against a real demo fill.
OPTION_FEE_RATE = 0.0003
OPTION_FEE_CAP = 0.07
_MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}


@dataclass(frozen=True)
class OptionSymbol:
    base: str
    expiry_ms: int  # 08:00 UTC of the date in the symbol
    strike: float
    is_call: bool
    settle: str  # '' for the USDC-settled names without a suffix


def parse_option_symbol(symbol: str) -> OptionSymbol | None:
    """``BTC-30OCT26-90000-C-USDT`` -> OptionSymbol; None for anything else. Pure, no locale (month names fixed)."""
    parts = symbol.split("-")
    if len(parts) not in (4, 5) or parts[3] not in ("C", "P"):
        return None
    d = parts[1]
    try:
        strike = float(parts[2])
        expiry = datetime(2000 + int(d[-2:]), _MONTHS[d[-5:-2]], int(d[:-5]), OPTION_EXPIRY_HOUR_UTC, tzinfo=UTC)
    except (KeyError, ValueError):
        return None
    if not strike > 0:
        return None
    return OptionSymbol(parts[0], int(expiry.timestamp()) * 1000, strike, parts[3] == "C", parts[4] if len(parts) == 5 else "")


def option_fee(price: float, index: float) -> float:
    """Upper bound of the fee per contract (USD): taker rate of the index, capped at a share of the premium."""
    return min(OPTION_FEE_RATE * index, OPTION_FEE_CAP * price) if index > 0 else OPTION_FEE_CAP * price


@dataclass(frozen=True)
class OptionProposal:
    symbol: str  # Bybit symbol, e.g. 'BTC-30OCT26-90000-C-USDT'
    side: str  # 'buy' (to open); anything else is refused in this slice
    price: float  # limit premium per contract, USDT (one contract = one coin of the underlying)
    qty: float  # contracts, floored to the venue's lot step
    expiry_ms: int  # the venue's delivery time
    bid: float  # the option's book at decision time
    ask: float
    index: float  # underlying index price (the fee base)
    setup: str
    link_id: str  # idempotency key sent to the venue as orderLinkId

    @property
    def premium_usd(self) -> float:
        return self.qty * self.price

    @property
    def fee_usd(self) -> float:
        return self.qty * option_fee(self.price, self.index)

    @property
    def risk_usd(self) -> float:
        """The most a long option can lose: the premium paid plus the opening fee."""
        return self.premium_usd + self.fee_usd

    @property
    def spread(self) -> float:
        """(ask - bid) / mid; infinite without a two-sided book."""
        mid = (self.ask + self.bid) / 2
        return (self.ask - self.bid) / mid if self.bid > 0 and self.ask > self.bid else math.inf


def option_dte_days(p: OptionProposal, now_ms: int) -> float:
    """Days to expiry: the earlier of the venue's delivery time and 08:00 UTC of the date in the symbol."""
    sym = parse_option_symbol(p.symbol)
    expiry = min(p.expiry_ms, sym.expiry_ms) if sym else p.expiry_ms
    return (expiry - now_ms) / DAY_MS


def size_option(price: float, index: float, risk_usd: float, lot_step: float, min_qty: float) -> float:
    """Contracts whose premium plus fee is at most ``risk_usd``, floored to ``lot_step``; 0.0 if below ``min_qty``."""
    per = price + option_fee(price, index)
    if price <= 0 or per <= 0 or lot_step <= 0 or risk_usd <= 0:
        return 0.0
    qty = round(math.floor(risk_usd / per / lot_step + 1e-9) * lot_step, 12)
    return qty if qty >= min_qty else 0.0


def plan_option(
    symbol: str,
    price: float,
    expiry_ms: int,
    bid: float,
    ask: float,
    index: float,
    setup: str,
    link_id: str,
    lot_step: float,
    min_qty: float,
    cfg: GuardConfig = DEFAULT,
) -> OptionProposal:
    """A buy-to-open limit at ``price`` sized so that premium plus fee stays within the risk cap."""
    qty = size_option(price, index, cfg.risk_usd, lot_step, min_qty)
    return OptionProposal(symbol, "buy", price, qty, expiry_ms, bid, ask, index, setup, link_id)


def check_option(p: OptionProposal, s: State, cfg: GuardConfig = DEFAULT) -> list[str]:
    """Violations of the limits for an option order; empty = it may be sent. The account caps (daily trades and
    entries, orders per hour, open risk, the day's loss, kill-switch) are the same ones the futures entries use."""
    out: list[str] = []
    if cfg.mode != MODE:
        out.append(f"mode {cfg.mode!r} is not allowed (only {MODE})")
    if s.halted:
        out.append("kill-switch is on")
    if p.side != "buy":
        out.append(f"side {p.side!r}: only buying options to open (no selling or writing)")
    sym = parse_option_symbol(p.symbol)
    if sym is None:
        out.append(f"{p.symbol} is not an option symbol")
    else:
        if sym.base not in cfg.option_bases:
            out.append(f"underlying {sym.base} not allowed (only {', '.join(cfg.option_bases)})")
        if sym.settle != OPTION_SETTLE:
            out.append(f"settlement {sym.settle or 'USDC'!r}: only {OPTION_SETTLE}-settled options")
    dte = option_dte_days(p, s.now_ms)
    if dte < cfg.option_min_dte_days:
        out.append(f"{dte:.1f} days to expiry < {cfg.option_min_dte_days:g} (no option buys with 1-3 DTE)")
    if not (p.bid > 0 and p.ask > p.bid):
        out.append("no two-sided quote")
    elif p.spread > cfg.option_max_spread:
        out.append(f"spread {p.spread:.1%} of mid > {cfg.option_max_spread:.0%}")
    if not p.price > 0:
        out.append("limit price must be positive")
    elif p.ask > 0 and p.price >= p.ask:
        out.append(f"limit {p.price:g} at or above the ask {p.ask:g} would take liquidity (entries are post-only)")
    if not p.qty > 0:  # written so that NaN is refused too
        out.append("size is zero (risk too small for the lot step)")
    if not p.risk_usd <= cfg.risk_usd * 1.0001:
        out.append(f"premium + fee {p.risk_usd:.2f} USD > {cfg.risk_usd:.0f}")
    if not s.equity_usd > 0:
        out.append("equity unknown or zero")
    else:
        out += _cross_margin(0.0, p.risk_usd, s, cfg)  # a long option has no maintenance margin
    return out + _account_caps(p.symbol, p.link_id, p.risk_usd, s, cfg)


def day_start_ms(now_ms: int) -> int:
    return now_ms - now_ms % DAY_MS


def open_risk_usd(positions: Sequence[tuple[float, float, float]]) -> float:
    """Loss if every open position hits its stop, fees included: over (qty, entry, stop)."""
    return sum(abs(q) * (abs(e - st) + TAKER_FEE * (e + st)) for q, e, st in positions)


def breakeven_stop(side: str, entry: float, tick: float = 0.0) -> float:
    """Stop price that loses nothing after both taker fees (the owner's rule: move here half the way to the target), rounded to the
    tick away from the entry (up for a long, down for a short) so it never lands on the losing side."""
    raw = entry * (1 + 2 * TAKER_FEE) if side == "long" else entry * (1 - 2 * TAKER_FEE)
    if tick <= 0:
        return raw
    steps = raw / tick
    return round((math.ceil(steps - 1e-9) if side == "long" else math.floor(steps + 1e-9)) * tick, 12)


def roe(side: str, entry: float, mark: float, leverage: float) -> float:
    """Return on the initial margin, as Bybit shows it (0.30 = +30 %), before fees."""
    if entry <= 0:
        return 0.0
    return (1 if side == "long" else -1) * (mark - entry) / entry * leverage


def path_to_target(side: str, entry: float, target: float, mark: float) -> float:
    """Share of the way from the entry to the target the price has gone (0.5 = halfway; negative = against us)."""
    way = (1 if side == "long" else -1) * (target - entry)
    return (1 if side == "long" else -1) * (mark - entry) / way if way > 0 else 0.0


# Owner 22.10: the stop goes to break-even when the price has gone half the way to the target. It replaced +30 % ROE
# (21.10), which depended on the leverage, not on the setup: 0.75R on BTC at 20x, but 2-5R on 5x coins, beyond most
# targets, so it never fired there. In R it is 0.5 x the setup's target (1.1-1.5R on the 21.10 trades, the course's
# 1-1.5R). Course step 2 (17.10): break-even at 1R / 1.5R changes E[R] little (IS -0.008R / +0.003R, OOS +0.06R / +0.05R).
BE_PATH = 0.5


CRYPTO_KINDS = ("", "innovation")  # Bybit symbolType: '' = crypto, 'innovation' = new crypto listings; no stock/ETF/commodity


def universe(tickers: Sequence[tuple[str, float, str]], size: int = 100, min_turnover_usd: float = 5e6) -> tuple[str, ...]:
    """Top ``size`` crypto USDT linear perpetuals by 24h turnover; stablecoins, thin markets and tokenised stocks, ETFs
    and commodities out (the course setups were checked on crypto only). Input (symbol, turnover, Bybit symbolType)."""
    ok = [
        (sym, t)
        for sym, t, kind in tickers
        if sym.endswith("/USDT:USDT") and kind in CRYPTO_KINDS and base_of(sym) not in STABLES and t >= min_turnover_usd
    ]
    ok.sort(key=lambda x: -x[1])
    return tuple(sym for sym, _ in ok[:size])
