"""Weekly option trade ideas from Bybit option quotes (owner 14.11: «short, actionable: expiry, strikes, premium, why,
R:R»; Bybit prices, because he trades there).

Pure functions, no I/O, time passed in. Prices are what a taker gets on Bybit: sold legs at the bid, bought legs at the
ask, minus the taker fee (``fee``). Probabilities come from the quotes' deltas, so they are RISK-NEUTRAL hints, not
forecasts. The ideas are hypotheses (no tested edge; the credit structures rest on the volatility premium, confirmed
as IV above realized vol, but not yet as a profit of the traded structure after spreads — docs/knowledge/options.md);
they are logged and settled with ``payoff_at`` at expiry so their record can be judged later.

Units: prices and P/L in USD per 1 unit of the underlying (1 BTC / 1 ETH), IV and probabilities as decimals.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .instruments import parse_expiry_token

DAY_MS = 86_400_000
TAKER_RATE = 0.0003  # Bybit options taker fee: 0.03 % of the underlying per contract …
FEE_CAP = 0.07  # … capped at 7 % of the option price
LOT = {"BTC": 0.01, "ETH": 0.1}  # Bybit minimum order / step (instruments-info, 14.11)


@dataclass(frozen=True)
class Quote:
    expiry_ms: int
    strike: float
    is_call: bool
    bid: float
    ask: float
    mark: float
    iv: float | None
    delta: float | None
    underlying: float


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_bybit_tickers(rows: Any, coin: str) -> list[Quote]:
    """Quotes of ``/v5/market/tickers?category=option`` rows for ``coin`` (symbols like BTC-27NOV26-62000-P-USDT;
    unreadable rows are skipped). A missing bid or ask is 0 (no side to trade)."""
    out: list[Quote] = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        parts = str(r.get("symbol", "")).split("-")
        if len(parts) < 4 or parts[0] != coin or parts[3] not in ("C", "P"):
            continue
        exp = parse_expiry_token(parts[1])
        strike, und = _num(parts[2]), _num(r.get("underlyingPrice"))
        if exp is None or not strike or not und:
            continue
        out.append(
            Quote(
                expiry_ms=int(exp.timestamp() * 1000),
                strike=strike,
                is_call=parts[3] == "C",
                bid=_num(r.get("bid1Price")) or 0.0,
                ask=_num(r.get("ask1Price")) or 0.0,
                mark=_num(r.get("markPrice")) or 0.0,
                iv=_num(r.get("markIv")),
                delta=_num(r.get("delta")),
                underlying=und,
            )
        )
    return out


def fee(underlying: float, price: float) -> float:
    """Taker fee for one contract of one leg, USD."""
    return min(TAKER_RATE * underlying, FEE_CAP * price)


def pick_expiry(
    quotes: list[Quote], now_ms: int, target_days: float = 14, lo: float = 7, hi: float = 24, min_liquid: int = 8
) -> int | None:
    """The expiry nearest ``target_days`` within [lo, hi] days that has at least ``min_liquid`` two-sided quotes."""
    liquid: dict[int, int] = {}
    for q in quotes:
        if q.bid > 0 and q.ask > 0:
            liquid[q.expiry_ms] = liquid.get(q.expiry_ms, 0) + 1
    ok = [e for e, n in liquid.items() if n >= min_liquid and lo <= (e - now_ms) / DAY_MS <= hi]
    return min(ok, key=lambda e: abs((e - now_ms) / DAY_MS - target_days)) if ok else None


@dataclass
class Leg:
    action: str  # "sell" | "buy"
    is_call: bool
    strike: float
    price: float  # bid for a sold leg, ask for a bought one
    delta: float | None


@dataclass
class Idea:
    coin: str
    kind: str  # iron_condor | put_debit_spread | call_debit_spread | long_strangle
    expiry_ms: int
    days: float
    spot: float
    legs: list[Leg]
    net: float  # credit (> 0) or debit (< 0) after fees, USD per unit
    max_profit: float | None  # None = unlimited
    max_loss: float
    breakevens: list[float]
    prob_profit: float | None  # risk-neutral, from deltas
    why: str = ""
    qty: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def reward_to_risk(self) -> float | None:
        return None if self.max_profit is None or self.max_loss <= 0 else self.max_profit / self.max_loss

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reward_to_risk"] = self.reward_to_risk
        return d


def _side(quotes: list[Quote], expiry_ms: int, is_call: bool) -> list[Quote]:
    return sorted((q for q in quotes if q.expiry_ms == expiry_ms and q.is_call == is_call and q.delta is not None), key=lambda q: q.strike)


def _near_delta(qs: list[Quote], target: float, need: str) -> Quote | None:
    """The quote whose |delta| is nearest ``target`` with a tradable ``need`` side ('bid' or 'ask')."""
    ok = [q for q in qs if (q.bid if need == "bid" else q.ask) > 0]
    return min(ok, key=lambda q: abs(abs(q.delta or 0) - target)) if ok else None


def _prob_below(puts: list[Quote], price: float) -> float | None:
    """Risk-neutral P(S_T < price) from put |delta| interpolated across strikes (|put delta| ≈ N(-d1) ≈ P(below))."""
    pts = [(q.strike, abs(q.delta or 0)) for q in puts]
    if len(pts) < 2:
        return None
    if price <= pts[0][0]:
        return pts[0][1]
    if price >= pts[-1][0]:
        return pts[-1][1]
    for (k0, p0), (k1, p1) in zip(pts, pts[1:], strict=False):
        if k0 <= price <= k1:
            return p0 + (p1 - p0) * (price - k0) / (k1 - k0)
    return None


def _days(expiry_ms: int, now_ms: int) -> float:
    return round((expiry_ms - now_ms) / DAY_MS, 1)


def iron_condor(quotes: list[Quote], expiry_ms: int, now_ms: int, coin: str, short_delta: float = 0.15, wing: float = 0.03) -> Idea | None:
    """Sell the ≈``short_delta`` put and call, buy wings about ``wing`` × spot further out (the same width each side)."""
    puts, calls = _side(quotes, expiry_ms, False), _side(quotes, expiry_ms, True)
    if not puts or not calls:
        return None
    spot = puts[0].underlying
    sp = _near_delta([q for q in puts if q.strike < spot], short_delta, "bid")
    sc = _near_delta([q for q in calls if q.strike > spot], short_delta, "bid")
    if sp is None or sc is None:
        return None
    w = wing * spot  # one wing width on both sides, so neither side carries more risk than the other
    lp = min((q for q in puts if q.strike < sp.strike and q.ask > 0), key=lambda q: abs(sp.strike - q.strike - w), default=None)
    lc = min((q for q in calls if q.strike > sc.strike and q.ask > 0), key=lambda q: abs(q.strike - sc.strike - w), default=None)
    if lp is None or lc is None:
        return None
    gross = sp.bid + sc.bid - lp.ask - lc.ask
    credit = gross - sum(fee(spot, p) for p in (sp.bid, sc.bid, lp.ask, lc.ask))
    width = max(sp.strike - lp.strike, lc.strike - sc.strike)
    if credit <= 0 or credit >= width:
        return None
    pop = None
    p_lo, p_hi = _prob_below(puts, sp.strike - credit), _prob_below(puts, sc.strike + credit)
    if p_lo is not None and p_hi is not None:
        pop = max(0.0, p_hi - p_lo)
    return Idea(
        coin=coin,
        kind="iron_condor",
        expiry_ms=expiry_ms,
        days=_days(expiry_ms, now_ms),
        spot=spot,
        legs=[
            Leg("sell", False, sp.strike, sp.bid, sp.delta),
            Leg("buy", False, lp.strike, lp.ask, lp.delta),
            Leg("sell", True, sc.strike, sc.bid, sc.delta),
            Leg("buy", True, lc.strike, lc.ask, lc.delta),
        ],
        net=credit,
        max_profit=credit,
        max_loss=width - credit,
        breakevens=[sp.strike - credit, sc.strike + credit],
        prob_profit=pop,
    )


def debit_spread(
    quotes: list[Quote], expiry_ms: int, now_ms: int, coin: str, bearish: bool, long_delta: float = 0.40, short_delta: float = 0.15
) -> Idea | None:
    """Bear put spread (``bearish``) or bull call spread: buy the ≈``long_delta`` option, sell the ≈``short_delta`` one."""
    puts = _side(quotes, expiry_ms, False)
    qs = puts if bearish else _side(quotes, expiry_ms, True)
    if not qs or not puts:
        return None
    lg = _near_delta(qs, long_delta, "ask")
    if lg is None:
        return None
    further = [q for q in qs if (q.strike < lg.strike if bearish else q.strike > lg.strike)]
    sh = _near_delta(further, short_delta, "bid")
    if sh is None:
        return None
    spot = lg.underlying
    debit = lg.ask - sh.bid + fee(spot, lg.ask) + fee(spot, sh.bid)
    width = abs(lg.strike - sh.strike)
    if debit <= 0 or debit >= width:
        return None
    be = lg.strike - debit if bearish else lg.strike + debit
    p_be = _prob_below(puts, be)
    pop = None if p_be is None else (p_be if bearish else 1 - p_be)
    return Idea(
        coin=coin,
        kind="put_debit_spread" if bearish else "call_debit_spread",
        expiry_ms=expiry_ms,
        days=_days(expiry_ms, now_ms),
        spot=spot,
        legs=[Leg("buy", not bearish, lg.strike, lg.ask, lg.delta), Leg("sell", not bearish, sh.strike, sh.bid, sh.delta)],
        net=-debit,
        max_profit=width - debit,
        max_loss=debit,
        breakevens=[be],
        prob_profit=pop,
        extra={"prob_max_profit": abs(sh.delta or 0)},
    )


def long_strangle(quotes: list[Quote], expiry_ms: int, now_ms: int, coin: str, delta: float = 0.25) -> Idea | None:
    """Buy the ≈``delta`` put and call: pays when the move is bigger than the options price in."""
    puts, calls = _side(quotes, expiry_ms, False), _side(quotes, expiry_ms, True)
    p = _near_delta(puts, delta, "ask")
    c = _near_delta(calls, delta, "ask")
    if p is None or c is None or p.strike > c.strike:
        return None
    spot = p.underlying
    debit = p.ask + c.ask + fee(spot, p.ask) + fee(spot, c.ask)
    lo, hi = p.strike - debit, c.strike + debit
    pl, ph = _prob_below(puts, lo), _prob_below(puts, hi)
    return Idea(
        coin=coin,
        kind="long_strangle",
        expiry_ms=expiry_ms,
        days=_days(expiry_ms, now_ms),
        spot=spot,
        legs=[Leg("buy", False, p.strike, p.ask, p.delta), Leg("buy", True, c.strike, c.ask, c.delta)],
        net=-debit,
        max_profit=None,
        max_loss=debit,
        breakevens=[lo, hi],
        prob_profit=None if pl is None or ph is None else pl + (1 - ph),
    )


def atm_iv(quotes: list[Quote], expiry_ms: int) -> float | None:
    """Mean mark IV of the call and put at the strike nearest the underlying."""
    qs = [q for q in quotes if q.expiry_ms == expiry_ms and q.iv]
    if not qs:
        return None
    spot = qs[0].underlying
    k = min({q.strike for q in qs}, key=lambda s: abs(s - spot))
    ivs = [q.iv for q in qs if q.strike == k and q.iv]
    return sum(ivs) / len(ivs) if ivs else None


def realized_vol(closes: list[float], days: int = 30) -> float | None:
    """Annualised (365) close-to-close volatility of the last ``days`` daily returns."""
    c = [x for x in closes if x and x > 0][-(days + 1) :]
    if len(c) < days + 1:
        return None
    r = [math.log(b / a) for a, b in zip(c, c[1:], strict=False)]
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1) * 365)


def trend(closes: list[float], sma: int = 200, slope_days: int = 20) -> str:
    """'bear' (below a falling SMA), 'bull' (above a rising one) or 'neutral'."""
    c = [x for x in closes if x and x > 0]
    if len(c) < sma + slope_days:
        return "neutral"
    now_sma = sum(c[-sma:]) / sma
    then_sma = sum(c[-sma - slope_days : -slope_days]) / sma
    if c[-1] < now_sma and now_sma < then_sma:
        return "bear"
    if c[-1] > now_sma and now_sma > then_sma:
        return "bull"
    return "neutral"


def size(idea: Idea, risk_usd: float) -> float:
    """Units (BTC / ETH) so the max loss is at most ``risk_usd``, rounded down to Bybit's step; 0 when even one step
    risks more."""
    step = LOT.get(idea.coin, 0.01)
    if idea.max_loss <= 0:
        return 0.0
    n = math.floor(risk_usd / idea.max_loss / step + 1e-9)
    return round(n * step, 4)


def ideas_for(
    coin: str, quotes: list[Quote], closes: list[float], now_ms: int, risk_usd: float = 100.0, target_days: float = 14
) -> tuple[list[Idea], dict[str, Any]]:
    """Up to two ideas for one coin and the context they rest on.

    1. Volatility: options priced above the last 30 days' realized move (ATM IV ≥ RV30) → iron condor (sell the
       premium, risk capped by the wings); priced below (IV < 0.9 × RV30) → long strangle; in between → nothing.
    2. Direction: 200-day trend 'bear' → bear put spread, 'bull' → bull call spread, 'neutral' → nothing.
    """
    ctx: dict[str, Any] = {"coin": coin, "trend": trend(closes), "rv30": realized_vol(closes)}
    exp = pick_expiry(quotes, now_ms, target_days)
    if exp is None:
        ctx["skip"] = "no Bybit expiry with enough two-sided quotes 7-24 days out"
        return [], ctx
    ctx["expiry_ms"] = exp
    ctx["iv"] = iv = atm_iv(quotes, exp)
    ctx["spot"] = next((q.underlying for q in quotes if q.expiry_ms == exp), None)
    rv = ctx["rv30"]
    out: list[Idea] = []
    if iv and rv:
        ratio = iv / rv
        ctx["iv_rv"] = ratio
        if ratio >= 1.0:
            idea = iron_condor(quotes, exp, now_ms, coin)
            if idea:
                idea.why = (
                    f"опціони дорожчі за реальний рух (IV {iv:.0%} проти {rv:.0%} за 30 днів): продаємо премію, ризик обмежений крилами"
                )
                out.append(idea)
        elif ratio < 0.9:
            idea = long_strangle(quotes, exp, now_ms, coin)
            if idea:
                idea.why = f"опціони дешевші за реальний рух (IV {iv:.0%} проти {rv:.0%} за 30 днів): купуємо рух в обидва боки"
                out.append(idea)
    if ctx["trend"] in ("bear", "bull"):
        idea = debit_spread(quotes, exp, now_ms, coin, bearish=ctx["trend"] == "bear")
        if idea:
            idea.why = (
                "ціна нижче 200-денної середньої, а вона падає: ставка на продовження падіння з обмеженим ризиком"
                if ctx["trend"] == "bear"
                else "ціна вище 200-денної середньої, а вона росте: ставка на продовження росту з обмеженим ризиком"
            )
            out.append(idea)
    for idea in out:
        idea.qty = size(idea, risk_usd)
    return out, ctx


def payoff_at(idea: dict[str, Any], price: float) -> float:
    """P/L per unit at expiry for a logged idea (``Idea.to_dict``) settled at ``price``: legs' intrinsic value plus
    the net premium (fees at entry included in ``net``; delivery fees not)."""
    total = float(idea["net"])
    for leg in idea["legs"]:
        k = float(leg["strike"])
        intrinsic = max(price - k, 0.0) if leg["is_call"] else max(k - price, 0.0)
        total += intrinsic if leg["action"] == "buy" else -intrinsic
    return total


def expiry_label(expiry_ms: int) -> str:
    return datetime.fromtimestamp(expiry_ms / 1000, UTC).strftime("%d.%m")
