"""Weekly option trade ideas from Bybit option quotes (owner 14.11: «short, actionable: expiry, strikes, premium, why,
R:R»; Bybit prices, because he trades there).

Pure functions, no I/O, time passed in. Prices are what a taker gets on Bybit: sold legs at the bid, bought legs at the
ask, minus the taker fee (``fee``). Probabilities come from the quotes' deltas, so they are RISK-NEUTRAL hints, not
forecasts. The ideas are hypotheses (no tested edge; the credit structures rest on the volatility premium, confirmed
as IV above realized vol, but not yet as a profit of the traded structure after spreads — docs/knowledge/options.md);
they are logged and settled with ``payoff_at`` at expiry so their record can be judged later.

Which ideas go out is decided by the rules of the options playbook (``RULES``, docs/knowledge/options-playbook.md;
card IDEA-20261114-options-playbook item 2): a rule is sent only when an MCP tool measures its condition and its
evidence status is not ✗; ideas are ordered by evidence (✓ > ≈ > ◯ > not tested). The rules' inputs are the ``data``
of our MCP tools (deribit_vrp, deribit_atm_term, macro_calendar, skew_percentile), passed in by the caller;
thresholds are the playbook's, not tuned here.

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
    rule: str = ""  # playbook rule code (RULES), e.g. "P3"

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


# --- Options playbook rules (docs/knowledge/options-playbook.md) --------------------------------------------------

PLAYBOOK = "docs/knowledge/options-playbook.md"
EVIDENCE_ORDER = ("✓", "≈", "◯", "не перевірено")  # strongest first; ✗ is never sent
MAX_IDEAS_PER_COIN = 3

# Thresholds, all from the playbook (their sources are named there; changing one goes through item 4 of the card).
DVOL_PCT_HIGH = 0.8  # P2: deribit_vrp current.dvol_percentile_365d
FRONT_MAX_DAYS = 8.0  # P10: the front expiry is at most 8 days out …
MONTH_DAYS = 30.0  # … the monthly one is the expiry nearest 30 days …
MONTH_BAND = (21.0, 45.0)  # … within this band (else not measured)
FRONT_INVERSION = 0.015  # P10: front ATM IV minus monthly ≥ 1.5 vol points
EVENT_KINDS = ("fomc", "cpi")  # P10: macro_calendar kinds that count as the event between the expiries
RR25_FEAR = -0.05  # P15: 25Δ risk reversal at or below −5 vol points …
RR25_TURN = 0.01  # … and the latest value at least 1 point above that minimum …
SKEW_LOOKBACK_DAYS = 3  # … within the last 3 days (skew_percentile lookback)


@dataclass(frozen=True)
class Rule:
    """One playbook rule. ``kind``: 'trade' = a structure priced at Bybit; 'context' = a tag or a risk rule shown
    next to the ideas, never a trade. ``measured`` is False when no MCP tool measures the condition («не вимірюється»)."""

    code: str
    name: str
    condition: str
    construction: str
    status: str  # ✓ | ≈ | ◯ | ✗ | не перевірено
    evidence: str  # the «докази: …» text of the message
    anchor: str  # heading anchor in the playbook
    kind: str = "trade"
    measured: bool = True

    @property
    def link(self) -> str:
        return f"{PLAYBOOK}#{self.anchor}"

    @property
    def sendable(self) -> bool:
        return self.measured and self.status in EVIDENCE_ORDER


RULES: dict[str, Rule] = {
    r.code: r
    for r in (
        Rule(
            "P1",
            "IV вища за реалізовану → залізний кондор",
            "deribit_vrp → current.spread_vs_rv30 ≥ 0 (ATM IV / RV30 ≥ 1.0; поріг — дизайн генератора 14.11)",
            "кондор 7–24 д: продані пут і кол |Δ| ≈ 0.15, крила ≈ 3 % спота далі",
            "✗",
            "✗ розрив IV − RV не прогнозує наступної премії; конструкція 15Δ / +3 % не тестована",
            "p1-iv-вища-за-реалізовану--залізний-кондор",
        ),
        Rule(
            "P2",
            "DVOL у верхніх 20 % за рік",
            "deribit_vrp → current.dvol_percentile_365d ≥ 0.8",
            "контекст-позначка, не окрема угода (тестували хеджований ATM-стреддл 30 д)",
            "◯",
            "◯ на реальних марках +1.43 % [−0.13; +3.45], n = 16, CI містить 0; не підтверджено",
            "p2-рівень-dvol-перцентиль-як-умова-продажу-премії--розміру",
            kind="context",
        ),
        Rule(
            "P3",
            "200-денний тренд → дебетовий спред",
            "денні закриття: ціна нижче падаючої SMA200 (нахил за 20 д) → ведмежий; вище зростаючої → бичачий",
            "bear put / bull call spread 7–24 д: куплена |Δ| ≈ 0.40, продана ≈ 0.15; ризик = дебет",
            "✗",
            "✗ BTC 2021–2026: −0.18 R на дебет [−0.42; −0.01] після витрат, n = 197 / 16 епізодів; IS і OOS мінус",
            "p3-200-денний-тренд--вертикальний-дебетовий-спред",
        ),
        Rule(
            "P4",
            "IV нижча за реалізовану → довгий стренгл",
            "ATM IV / RV30 < 0.9 (дизайн генератора 14.11)",
            "довгий 25Δ стренгл 7–24 д",
            "✗",
            "✗ купівля вола на марках −0.59 % [−2.43; +1.51], n = 27; покупець у середньому платить VRP",
            "p4-iv-нижча-за-реалізовану--довгий-стренгл--відхилено-не-надсилати",
        ),
        Rule(
            "P5",
            "напрямна купівля кол / пут при низькому IV rank",
            "iv_rank → iv_rank ≤ 0.33 і теза власника",
            "кол / пут |Δ| 0.40–0.50",
            "✗",
            "✗ як перевага: покупець у середньому платить VRP",
            "p5-напрямна-купівля-кол--пут-з-низькою-iv-як-знижкою--відхилено-не-надсилати",
        ),
        Rule(
            "P6",
            "вертикальний спред до рівня (S20)",
            "напрямна теза і рівень, який ціна навряд чи пройде (задає людина)",
            "bull call / bear put spread з проданою ногою на рівні",
            "не перевірено",
            "не перевірено; ◯ як правило виразу тези",
            "p6-вертикальний-спред-до-рівня-s20--не-вимірюється-не-для-розсилки",
            measured=False,
        ),
        Rule(
            "P7",
            "захисний пут для спот-позиції (S21)",
            "довгострокова спот-позиція і конкретний ризик (знає лише власник)",
            "1 пут на 1 монету, ATM / OTM, 2–4 тижні; пут-спред дешевший",
            "не перевірено",
            "правило ризику, не джерело прибутку",
            "p7-захисний-пут-для-спот-позиції-s21--хедж",
            kind="context",
            measured=False,
        ),
        Rule(
            "P8",
            "опціон замість стопу",
            "сигнал демо-трейдера (подія в журналі, не поле MCP)",
            "кол / пут |Δ| ≈ 0.40 на премію ≤ $100 поруч із перпом",
            "◯",
            "◯ форвард-тест demo_option_pairs, до 30 пар — замало даних",
            "p8-опціон-замість-стопу-парний-тест-з-2310--не-вимірюється-не-для-розсилки-лише-форвард-тест",
            measured=False,
        ),
        Rule(
            "P9",
            "подія ≤ 48 год: не тримати куплену коротку вегу",
            "macro_calendar → high_impact_within_48h = true",
            "правило ризику: не тримати куплену коротку вегу через подію; експірація ≥ 3–5 днів після події",
            "✓",
            "✓ ціна події: ATM IV у день події 63 % проти 53 %; ◯ як угода",
            "p9-подієві-правила-нічого-не-купувати-перед-подією-на-короткий-строк",
            kind="context",
        ),
        Rule(
            "P10",
            "інверсія фронту перед подією → календар",
            "deribit_atm_term: ATM IV строку ≤ 8 д, що включає FOMC / CPI (macro_calendar), мінус строку ≈ 30 д ≥ +1.5 п.",
            "календар: продати тижневий ATM-стреддл, купити місячний (ціни Bybit розсилка ще не рахує)",
            "не перевірено",
            "не перевірено (H2 LIB DataHawks); сусідні подієві тести ◯",
            "p10-інверсія-фронту--діагональ--календар-s29-h2-lib",
            kind="context",
        ),
        Rule(
            "P11",
            "кондор / метелик на подію",
            "подія ≤ 48 год і завищена IV фронту",
            "кондор / метелик з обмеженим ризиком",
            "не перевірено",
            "не перевірено",
            "p11-закриті-конструкції-на-подію-продаж-кондора--метелика-перед-cpi-fomc--не-перевірено-не-вимірюється-не-для-розсилки",
            measured=False,
        ),
        Rule(
            "P12",
            "продаж 25Δ стренглу з хеджем (премія в крилах)",
            "умови нема (розвідувальна знахідка)",
            "кондор із 25Δ шортами",
            "◯",
            "◯ розвідувально +0.73 % [+0.21; +1.51], n = 40",
            "p12-продаж-стренглу-25δ-з-хеджем-премія-в-крилах--не-вимірюється-не-для-розсилки-лише-гіпотеза-для-пункту-4",
            measured=False,
        ),
        Rule(
            "P13",
            "ведення позиції: продаж далекого страйку, ролл (S30)",
            "куплена позиція в прибутку (поля MCP нема)",
            "продати вищий страйк / закрити частину / перенести страйк",
            "◯",
            "◯ правило ведення, не перевага",
            "p13-ведення-позиції-перебудова-профілю-продаж-далекого-страйку-ролл-s30--не-вимірюється-не-для-розсилки-дисципліна-ведення",
            kind="context",
            measured=False,
        ),
        Rule(
            "P14",
            "GEX / max pain / пін як прогноз",
            "deribit_gamma_exposure, deribit_max_pain, deribit_oi_by_strike",
            "лише опис, не прогноз",
            "✗",
            "✗ GEX не прогнозує діапазон (β = −0.001); ◯ пін",
            "p14-позиціонування-опціонів-як-контекст-gex-max-pain-пін-не-правило-описова-позначка",
            kind="context",
        ),
        Rule(
            "P15",
            "скью RR25 ≤ −5 п. і розворот: «страх на максимумі»",
            "skew_percentile (25Δ, експірація ≈ 30 д, 3 доби): мінімум rr ≤ −0.05 і останнє ≥ мінімум + 0.01",
            "тег режиму, не сигнал (продаж пута після паніки несе ризик VRP-продажу)",
            "◯",
            "◯ розвідка на 13 подіях ETH: не краще за «купити після падіння ≥ 15 %»",
            "p15-скью-rr25-як-контр-індикатор-страху-eth--5--розворот--дно",
            kind="context",
        ),
    )
}
MARKET_WIDE = ("P9",)  # the same for every coin: shown once


@dataclass(frozen=True)
class Check:
    """Whether a rule's condition holds now. ``met`` None = not measured this time (the tool gave no data)."""

    code: str
    met: bool | None
    detail: str


def _get(d: Any, *path: str) -> Any:
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def check_dvol_high(vrp: dict[str, Any] | None) -> Check:
    """P2 on ``deribit_vrp`` data."""
    p, dvol = _num(_get(vrp, "current", "dvol_percentile_365d")), _num(_get(vrp, "current", "dvol"))
    if p is None:
        return Check("P2", None, "deribit_vrp не дав перцентиля DVOL")
    lvl = f"DVOL {dvol:.0%}, " if dvol is not None else ""
    return Check("P2", p >= DVOL_PCT_HIGH, f"{lvl}перцентиль за 365 д {p:.2f} (поріг ≥ {DVOL_PCT_HIGH})")


def check_trend(closes: list[float]) -> Check:
    """P3 on daily closes (``trend``)."""
    t = trend(closes)
    word = {"bear": "ведмежий", "bull": "бичачий"}.get(t, "нейтральний")
    return Check("P3", t in ("bear", "bull"), f"200-денний тренд {word}")


def check_event(calendar: dict[str, Any] | None) -> Check:
    """P9 on ``macro_calendar`` data."""
    if not isinstance(calendar, dict) or "high_impact_within_48h" not in calendar:
        return Check("P9", None, "macro_calendar без даних")
    if calendar["high_impact_within_48h"] is not True:
        return Check("P9", False, "подій високого впливу в найближчі 48 год нема")
    events = [e for e in calendar.get("events") or [] if isinstance(e, dict) and _num(e.get("hours_ahead")) is not None]
    ev = min(events, key=lambda e: abs(_num(e["hours_ahead"]) or 0.0), default=None)
    what = "подія високого впливу"
    if ev:
        h = _num(ev["hours_ahead"]) or 0.0
        when = f"через {h:.0f} год" if h >= 0 else f"{-h:.0f} год тому"
        what = f"{str(ev.get('kind') or 'подія').upper()} {when}"
    return Check("P9", True, f"{what}: не тримати куплену коротку вегу через подію")


def _term_rows(term: dict[str, Any] | None) -> list[tuple[float, float, str]]:
    """(days, atm_iv, expiry token) of ``deribit_atm_term`` data, nearest first."""
    out = []
    for x in _get(term, "expiries") or []:
        d, iv = _num(_get(x, "days")), _num(_get(x, "atm_iv"))
        if d is not None and iv is not None and d > 0 and isinstance(_get(x, "expiry"), str):
            out.append((d, iv, x["expiry"]))
    return sorted(out)


def _month(rows: list[tuple[float, float, str]]) -> tuple[float, float, str] | None:
    lo, hi = MONTH_BAND
    ok = [x for x in rows if lo <= x[0] <= hi]
    return min(ok, key=lambda x: abs(x[0] - MONTH_DAYS)) if ok else None


def month_expiry_iso(term: dict[str, Any] | None) -> str | None:
    """ISO expiry (as the collector stores it) of the ``deribit_atm_term`` expiry nearest 30 days; the input of
    ``skew_percentile`` for P15."""
    m = _month(_term_rows(term))
    exp = parse_expiry_token(m[2]) if m else None
    return exp.isoformat() if exp else None


def check_front_inversion(term: dict[str, Any] | None, calendar: dict[str, Any] | None) -> Check:
    """P10 on ``deribit_atm_term`` and ``macro_calendar`` data (both measure days / hours from now)."""
    rows = _term_rows(term)
    month = _month(rows)
    if month is None or not isinstance(calendar, dict) or not isinstance(calendar.get("events"), list):
        return Check("P10", None, "немає строкової структури або календаря")
    days = [
        h / 24
        for e in calendar["events"]
        if isinstance(e, dict) and e.get("kind") in EVENT_KINDS and (h := _num(e.get("hours_ahead"))) is not None and h >= 0
    ]
    ev = min((d for d in days if d < FRONT_MAX_DAYS), default=None)
    if ev is None:
        return Check("P10", False, f"FOMC / CPI у найближчі {FRONT_MAX_DAYS:.0f} д нема")
    front = next((x for x in rows if ev < x[0] <= FRONT_MAX_DAYS), None)
    if front is None:
        return Check("P10", False, f"немає експірації ≤ {FRONT_MAX_DAYS:.0f} д після події")
    diff = front[1] - month[1]
    return Check(
        "P10",
        diff >= FRONT_INVERSION,
        f"ATM IV {front[2]} {front[1]:.1%} мінус {month[2]} {month[1]:.1%} = {diff * 100:+.1f} п. (поріг +{FRONT_INVERSION * 100:.1f})",
    )


def check_skew_fear(skew: dict[str, Any] | None) -> Check:
    """P15 on ``skew_percentile`` data (rr25 of the ≈ 30-day expiry over the last 3 days)."""
    lo, last = _num(_get(skew, "rr", "min")), _num(_get(skew, "rr", "latest"))
    if lo is None or last is None:
        return Check("P15", None, "skew_percentile без історії колектора")
    met = lo <= RR25_FEAR and last - lo >= RR25_TURN
    return Check("P15", met, f"RR25 мінімум за 3 доби {lo * 100:+.1f} п., зараз {last * 100:+.1f} п.")


def rank_ideas(ideas: list[Idea], limit: int = MAX_IDEAS_PER_COIN) -> list[Idea]:
    """Sendable ideas only (rule measured, status not ✗), strongest evidence first, at most ``limit``."""
    ok = [i for i in ideas if i.rule in RULES and RULES[i.rule].sendable]
    return sorted(ok, key=lambda i: EVIDENCE_ORDER.index(RULES[i.rule].status))[:limit]


def ideas_for(
    coin: str,
    quotes: list[Quote],
    closes: list[float],
    now_ms: int,
    risk_usd: float = 100.0,
    target_days: float = 14,
    market: dict[str, Any] | None = None,
) -> tuple[list[Idea], dict[str, Any]]:
    """Up to ``MAX_IDEAS_PER_COIN`` ideas for one coin by the playbook rules, and the context they rest on.

    ``market``: the ``data`` of MCP tools for this coin — ``vrp`` (deribit_vrp), ``term`` (deribit_atm_term),
    ``calendar`` (macro_calendar), ``skew`` (skew_percentile of the ≈ 30-day expiry, 3-day lookback); a missing one
    leaves its rules unmeasured. Trade rules: P3 (200-day trend → debit spread) is still built and checked, but it
    is ✗ since 14.11 (docs/verification/2026-11-14-p3-trend-debit-spread.md), so ``rank_ideas`` drops it. Context
    rules (tags, risk rules; never a trade): P2, P9, P10, P15 — their met checks are listed in ``ctx['notes']``.
    ✗ rules (P1 IV ≥ RV condor, P3, P4 IV < RV strangle, P5) and unmeasured ones are never sent.
    """
    m = market or {}
    checks = [
        check_trend(closes),
        check_dvol_high(m.get("vrp")),
        check_event(m.get("calendar")),
        check_front_inversion(m.get("term"), m.get("calendar")),
        check_skew_fear(m.get("skew")),
    ]
    ctx: dict[str, Any] = {
        "coin": coin,
        "trend": trend(closes),
        "rv30": realized_vol(closes),
        "checks": {c.code: {"met": c.met, "detail": c.detail} for c in checks},
        "notes": [c.code for c in checks if c.met and RULES[c.code].kind == "context" and RULES[c.code].sendable],
    }
    exp = pick_expiry(quotes, now_ms, target_days)
    if exp is None:
        ctx["skip"] = "no Bybit expiry with enough two-sided quotes 7-24 days out"
        return [], ctx
    ctx["expiry_ms"] = exp
    ctx["iv"] = iv = atm_iv(quotes, exp)
    ctx["spot"] = next((q.underlying for q in quotes if q.expiry_ms == exp), None)
    if iv and ctx["rv30"]:
        ctx["iv_rv"] = iv / ctx["rv30"]  # context only: IV ≥ RV as a sell signal is ✗ (P1)
    out: list[Idea] = []
    if ctx["checks"]["P3"]["met"]:
        idea = debit_spread(quotes, exp, now_ms, coin, bearish=ctx["trend"] == "bear")
        if idea:
            idea.rule = "P3"
            idea.why = (
                "ціна нижче 200-денної середньої, а вона падає: ставка на продовження падіння з обмеженим ризиком"
                if ctx["trend"] == "bear"
                else "ціна вище 200-денної середньої, а вона росте: ставка на продовження росту з обмеженим ризиком"
            )
            out.append(idea)
    out = rank_ideas(out)
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


# --- Settlement of the ideas journal (card IDEA-20261114-options-playbook item 3) -----------------------------------

MIN_SETTLED = 30  # a rule's record is judged only from this many settled ideas (card: «висновки лише від 30»)
MIN_CLUSTERS = 10  # ... and from this many distinct expiries (ideas of one expiry are one cluster, not independent)
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def bybit_option_symbol(coin: str, expiry_ms: int, strike: float, is_call: bool) -> str:
    """Bybit USDT option symbol, e.g. BTC-7NOV26-70000-C-USDT (the day is not zero-padded, as Bybit lists it)."""
    d = datetime.fromtimestamp(expiry_ms / 1000, UTC)
    k = f"{strike:.0f}" if float(strike).is_integer() else f"{strike:g}"
    return f"{coin}-{d.day}{_MONTHS[d.month - 1]}{d.year % 100:02d}-{k}-{'C' if is_call else 'P'}-USDT"


def parse_bybit_delivery(body: Any, expiry_ms: int) -> float | None:
    """Delivery price of ``/v5/market/delivery-price?category=option&symbol=…`` (USD), or None when the body has none
    or its ``deliveryTime`` is not ``expiry_ms`` (the price of another expiry is never used)."""
    for r in _get(body, "result", "list") or []:
        if not isinstance(r, dict):
            continue
        px, t = _num(r.get("deliveryPrice")), _num(r.get("deliveryTime"))
        if px is not None and px > 0 and (t is None or int(t) == int(expiry_ms)):
            return px
    return None


def _settleable(idea: Any) -> bool:
    """A logged idea has what ``payoff_at`` and R need: numeric net and max loss > 0, legs with a strike, a type and a
    buy / sell action."""
    if not isinstance(idea, dict) or _num(idea.get("net")) is None or not (_num(idea.get("max_loss")) or 0) > 0:
        return False
    legs = idea.get("legs")
    if not isinstance(legs, list) or not legs:
        return False
    return all(
        isinstance(g, dict)
        and _num(g.get("strike")) is not None
        and isinstance(g.get("is_call"), bool)
        and g.get("action") in ("buy", "sell")
        for g in legs
    )


def settle_idea(idea: Any, price: Any, now_ms: int) -> dict[str, Any] | None:
    """Settlement fields of a logged idea (``Idea.to_dict``) at the delivery ``price``, or None when it cannot be
    settled yet (``now_ms`` before ``expiry_ms``), the price is not a positive number or the record is malformed.

    ``pnl_per_unit`` = ``payoff_at`` (intrinsic value of the legs plus the logged net premium, entry fees included;
    Bybit's delivery fee is NOT deducted, so ITM results are slightly optimistic); ``pnl_usd`` = that times the logged
    size ``qty`` (None when the idea could not be sized under the risk); ``r`` = ``pnl_per_unit`` / logged ``max_loss``
    (−1 = the whole risk lost)."""
    px, exp = _num(price), _num(idea.get("expiry_ms")) if isinstance(idea, dict) else None
    if px is None or px <= 0 or exp is None or now_ms < exp or not _settleable(idea):
        return None
    pnl = payoff_at(idea, px)
    if not math.isfinite(pnl):
        return None
    qty = _num(idea.get("qty")) or 0.0
    return {
        "status": "settled",
        "settled_ms": int(now_ms),
        "delivery_price": px,
        "pnl_per_unit": pnl,
        "pnl_usd": pnl * qty if qty > 0 else None,
        "r": pnl / float(idea["max_loss"]),
    }


def rule_summary(records: Any) -> dict[str, dict[str, Any]]:
    """Per playbook rule, over settled journal records: ``n``, ``sum_r``, ``mean_r`` (per idea), ``win_share``
    (R > 0), ``n_clusters`` and ``verdict``.

    Ideas sharing an expiry are not independent (BTC and ETH ideas of one Saturday share it, and BTC/ETH move
    together), so the verdict is cluster-level: one cluster per ``expiry_ms`` (records without one form a single
    cluster), its value the mean R of its ideas; ``cluster_mean_r`` is the mean of those and ``se_r`` their standard
    error. The verdict is None below ``MIN_SETTLED`` ideas or ``MIN_CLUSTERS`` expiries (``se_r`` and
    ``cluster_mean_r`` then None too); from there 'plus' / 'minus' when ``cluster_mean_r`` is more than 2 ``se_r``
    from 0, else 'flat'. Records without a rule are grouped under '—'; malformed ones skipped. Rules come in
    ``RULES`` order."""
    rs: dict[str, list[float]] = {}
    clusters: dict[str, dict[int | None, list[float]]] = {}
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict) or rec.get("status") != "settled":
            continue
        r = _num(rec.get("r"))
        if r is not None:
            code = str(rec.get("rule") or "—")
            rs.setdefault(code, []).append(r)
            exp = _num(rec.get("expiry_ms"))
            clusters.setdefault(code, {}).setdefault(int(exp) if exp is not None else None, []).append(r)
    order = list(RULES)
    out: dict[str, dict[str, Any]] = {}
    for code in sorted(rs, key=lambda c: (order.index(c) if c in RULES else len(order), c)):
        x = rs[code]
        n, mean = len(x), sum(x) / len(x)
        cm = [sum(v) / len(v) for v in clusters[code].values()]
        g = len(cm)
        verdict, se, cmean = None, None, None
        if n >= MIN_SETTLED and g >= MIN_CLUSTERS:
            cmean = sum(cm) / g
            se = math.sqrt(sum((v - cmean) ** 2 for v in cm) / (g - 1) / g)
            verdict = "plus" if cmean - 2 * se > 0 else "minus" if cmean + 2 * se < 0 else "flat"
        out[code] = {
            "n": n,
            "n_clusters": g,
            "sum_r": sum(x),
            "mean_r": mean,
            "cluster_mean_r": cmean,
            "se_r": se,
            "win_share": sum(v > 0 for v in x) / n,
            "verdict": verdict,
        }
    return out
