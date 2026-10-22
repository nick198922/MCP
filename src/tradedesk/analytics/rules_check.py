"""The owner's trading rules (docs/knowledge/trading-strategy.md, «Правила власника», approved 2026-10-18) checked
before an option trade and, week by week, on the journal (IDEA-20261018-assistant). Pure: no I/O, time passed in.

Statuses: ``ok``, ``violated``, ``unknown`` (the data cannot tell) and ``na`` (the rule does not apply: nothing
bought, no trade of that kind). Amounts are USD; the deposit defaults to the card's ≈ $10 000 (1 % = $100).

Rules covered:

* 1 — risk per trade <= 1 % of the deposit. Option structure: the exact max loss at expiry (unbounded = violated:
  it needs a stop on the exchange or protective wings). Journal: size x |entry - stop| of a trade with a recorded
  stop, the premium paid for a bought option; venue imports carry no stop, so their linear rows are ``unknown``.
* 2 — at most 3 alt longs open at once (alt = any base asset but BTC and ETH, options left out); opening a 4th
  distinct alt long violates it. Needs a real opening time (opened_ts < closed_ts, or still open): Bybit linear
  imports have none (opened_ts = closed_ts) and are ``unknown``. Hyperliquid HIP-3 symbols ('dex:ASSET', e.g.
  'xyz:TSLA': stocks, gold) are not crypto alts the rule is about: ``unknown`` and left out of the count. The
  "total open risk <= 3 %" half (open_risk, on the trades open now) sums rule 1's risk and needs stops.
* 4 — day stop: after 2 losing trades in a row or a day's realized P&L <= -2 % of the deposit, no new trade that
  day. UTC day and closing order as in scripts/journal_rules.py that produced the rule's effect, but "in a row" is
  stricter than that script, which stopped the day after any 2 losses. A trade opened after the stop moment of its
  day violates it. A row without an opening time stands in with its close time: such a flag is approximate (the
  trade may have been opened long before the stop), counted in ``approx`` and kept apart from exact violations in
  the weekly totals. The "48 h pause after a big-loss day" half has no threshold in the rule: not checked.
* 5 — target >= 2 x the stop distance (R:R >= 2): rows with both stop and target, else ``unknown``.
* 8 — options (owner, 22.10, Q-20261022-1): premium paid <= 1 % of the deposit per trade, and by the time from the
  purchase to the delivery (Deribit/Bybit 08:00 UTC, the date in the symbol) three zones: under 24 h — never buy
  (``violated``); 24-72 h (both ends included) — only when all three checks hold: (a) a recorded thesis (direction,
  reason and a target), (b) the move from spot to the nearest breakeven at expiry <= 1 sigma of the move at
  realized vol over the time left (RV x sqrt(T/365); RV = RV30, 30 daily closes, zero mean, x365, as the forecast
  log's RV30 model), (c) the option's IV <= 1.2 x RV; any check failed = ``violated``, a check without data =
  ``unknown``; over 72 h — the premium limit only. The 1-sigma and 1.2x thresholds are a starting choice, not a
  proven edge. A structure that nets a credit buys nothing on balance (``na``). Journal: bought option rows (side
  long); the time needs a real opening time, else ``unknown``. Imports carry neither a thesis nor IV: (a) fails
  without a thesis and a target (an 'import' row's placeholder thesis does not count), and the journal has no spot
  or IV at the opening, so (b) and (c) stay ``unknown``.

Rules 3, 6 and 7 need data the journal has no column for (liquidation price, the reason for a long, stop moves).
The journal check judges every week against today's deposit, not the deposit of that week.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .strategy import payoff_curve, payoff_extremes

OK, VIOLATED, UNKNOWN, NA = "ok", "violated", "unknown", "na"
DEPOSIT_USD = 10_000.0
RISK_PCT = 1.0  # rule 1, % of the deposit
MAX_ALT_LONGS = 3  # rule 2
OPEN_RISK_PCT = 3.0  # rule 2, total risk of the open trades, % of the deposit
DAY_LOSS_PCT = 2.0  # rule 4, % of the deposit
DAY_LOSING_STREAK = 2  # rule 4
MIN_RR = 2.0  # rule 5
PREMIUM_PCT = 1.0  # rule 8, % of the deposit
BUY_BAN_HOURS = 24.0  # rule 8: buying with less than this many hours to expiry violates it, whatever the reasons
BUY_COND_HOURS = 72.0  # rule 8: from BUY_BAN_HOURS up to this many hours (inclusive) only with all three checks
BE_MAX_SIGMA = 1.0  # rule 8 (b): breakeven move <= this many 1-sigma moves at RV over the time left
IV_MAX_RV = 1.2  # rule 8 (c): the bought option's IV <= this x RV
ZONE_BAN, ZONE_COND, ZONE_FREE = "under_24h", "1_3_days", "over_3_days"  # rule 8 zones by the time to expiry
MAJORS = frozenset({"BTC", "ETH"})
RULES = (1, 2, 4, 5, 8)
DAY_S = 86400.0
_EPS = 1e-9
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_OPTION_RE = re.compile(r"([A-Z0-9]+)-(\d{1,2})([A-Z]{3})(\d{2})-\d+(?:[.d]\d+)?-[CP](?:-[A-Z]+)?")
_QUOTES = ("USDT", "USDC", "BUSD", "USD", "PERP")


def _num(x: object) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def option_expiry_ts(symbol: object) -> float | None:
    """Delivery time (unix s, 08:00 UTC) of an option symbol such as 'BTC-31OCT26-90000-C' (Deribit, Bybit; an
    optional settle suffix like '-USDT'); None for anything else."""
    m = _OPTION_RE.fullmatch(str(symbol or "").upper())
    if not m or m.group(3) not in _MONTHS:
        return None
    try:
        return datetime(2000 + int(m.group(4)), _MONTHS[m.group(3)], int(m.group(2)), 8, tzinfo=UTC).timestamp()
    except ValueError:
        return None


def is_hip3(symbol: object) -> bool:
    """A Hyperliquid HIP-3 symbol 'dex:ASSET' (e.g. 'xyz:TSLA'); a ccxt 'SOL/USDT:USDT' is not one."""
    s = str(symbol or "")
    return ":" in s and "/" not in s


def base_asset(symbol: object) -> str:
    """Base asset of a perp / spot symbol: 'SOLUSDT', 'SOL/USDT:USDT', 'SOL-PERP', 'SOL' -> 'SOL'; the asset after
    the dex of a HIP-3 symbol: 'xyz:TSLA' -> 'TSLA'."""
    s = str(symbol or "").upper().strip()
    s = s.split(":")[-1] if is_hip3(s) else s.split("/")[0].split(":")[0]
    s = s.split("-")[0] if "-" in s else s
    for q in _QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def _tags(raw: object) -> list[str]:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, RecursionError):
        return []
    return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def iso_week(ts: float) -> str:
    """ISO week of a unix timestamp (UTC), e.g. '2025-W41'."""
    y, w, _ = datetime.fromtimestamp(ts, UTC).isocalendar()
    return f"{y}-W{w:02d}"


def _status(violated: int, checked: int, unknown: int) -> str:
    if violated:
        return VIOLATED
    if checked:
        return OK
    return UNKNOWN if unknown else NA


def buy_zone(hours: float, ban_hours: float = BUY_BAN_HOURS, cond_hours: float = BUY_COND_HOURS) -> str:
    """Rule 8 zone of a purchase ``hours`` before the delivery: ZONE_BAN (< ban_hours, also past the delivery),
    ZONE_COND (ban_hours..cond_hours, both inclusive) or ZONE_FREE (> cond_hours)."""
    return ZONE_BAN if hours < ban_hours else ZONE_COND if hours <= cond_hours else ZONE_FREE


def thesis_check(thesis: object, target: object) -> tuple[str, str]:
    """Rule 8 (a): (status, detail); a thesis (direction and reason, non-empty text) and a target price both needed."""
    has_thesis = isinstance(thesis, str) and bool(thesis.strip())
    t = _num(target)
    has_target = t is not None and t > 0
    if has_thesis and has_target:
        return OK, "thesis and target recorded"
    miss = [w for w, ok in (("thesis", has_thesis), ("target", has_target)) if not ok]
    return VIOLATED, "no " + " and no ".join(miss)


def conditions_status(*statuses: str) -> str:
    """Rule 8 in the 1-3 day zone: any check failed = violated, else any without data = unknown, else ok."""
    return VIOLATED if VIOLATED in statuses else UNKNOWN if UNKNOWN in statuses else OK


def breakeven_move(legs: Sequence[Mapping[str, Any]], spot: float) -> float | None:
    """Smallest relative move from ``spot`` (fraction of it) to a price where the option legs' payoff at expiry
    turns strictly positive: 0.0 when it already is at ``spot``, None when it never is."""
    opts = [dict(lg) for lg in legs if lg.get("kind") in ("call", "put") and (_num(lg.get("size")) or 0.0) > 0]
    if not opts or spot <= 0:
        return None
    if payoff_curve(opts, [spot])[0] > 0:
        return 0.0
    kinks = [0.0] + sorted({float(lg["strike"]) for lg in opts})
    values = payoff_curve(opts, kinks)
    bes = []
    for i in range(1, len(kinks)):
        v0, v1 = values[i - 1], values[i]
        if (v0 > 0) != (v1 > 0):
            bes.append(kinks[i - 1] + (0.0 - v0) * (kinks[i] - kinks[i - 1]) / (v1 - v0))
    slope = sum((1.0 if lg.get("side") == "long" else -1.0) * float(lg.get("size") or 0.0) for lg in opts if lg["kind"] == "call")
    last = values[-1]
    if (last > 0) != (slope > 0 or (slope == 0 and last > 0)):
        bes.append(kinks[-1] - last / slope)
    return min(abs(b - spot) for b in bes) / spot if bes else None


# ---------------------------------------------------------------- before a trade: an option structure


def pre_trade_option(
    legs: Sequence[Mapping[str, Any]],
    *,
    now_ts: float,
    expiry_ts: float | None,
    deposit_usd: float = DEPOSIT_USD,
    risk_pct: float = RISK_PCT,
    premium_pct: float = PREMIUM_PCT,
    spot: float | None = None,
    rv: float | None = None,
    thesis: str | None = None,
    target: float | None = None,
    ban_hours: float = BUY_BAN_HOURS,
    cond_hours: float = BUY_COND_HOURS,
    be_max_sigma: float = BE_MAX_SIGMA,
    iv_max_rv: float = IV_MAX_RV,
) -> list[dict[str, Any]]:
    """Rules 1 and 8 on a planned option structure (legs in the analytics.strategy format: kind 'call' | 'put',
    side 'long' | 'short', size, strike, entry_premium in USD per unit, optional iv as a decimal). One dict per
    check: {rule, key, status, value, limit, detail}; ``key`` is 'risk' (max loss at expiry, USD), 'premium' (net
    premium paid, USD) or 'days_to_expiry' (days from ``now_ts`` to ``expiry_ts``; limit = the never-buy floor in
    days; plus ``zone``). In the 1-3 day zone three more rows follow, and 'days_to_expiry' carries their combined
    status: 'thesis' (value: thesis and target recorded), 'breakeven_move' (value: move from ``spot`` to the nearest
    breakeven at expiry, limit: be_max_sigma x ``rv`` x sqrt(days/365), both fractions of spot) and 'iv_vs_rv'
    (value: the highest IV among the bought legs, limit: iv_max_rv x ``rv``; decimals). ``rv`` is the trailing
    realized vol (decimal, RV30). Rule 8 is about paying premium: a structure that nets a credit (a sale, an iron
    condor) gives ``na`` for its checks."""
    dep = _num(deposit_usd)
    dep = dep if dep is not None and dep > 0 else None
    opts = [lg for lg in legs if lg.get("kind") in ("call", "put") and (_num(lg.get("size")) or 0.0) > 0]
    bought = [lg for lg in opts if lg.get("side") == "long"]
    out: list[dict[str, Any]] = []

    budget = dep * risk_pct / 100.0 if dep else None
    if not opts:
        out.append({"rule": 1, "key": "risk", "status": NA, "value": None, "limit": budget, "detail": "no option legs"})
    else:
        ext = payoff_extremes(list(legs))
        if ext["max_loss"] is None:
            out.append(
                {
                    "rule": 1,
                    "key": "risk",
                    "status": VIOLATED,
                    "value": None,
                    "limit": budget,
                    "detail": "loss unbounded at expiry: needs a stop on the exchange or protective wings",
                }
            )
        else:
            loss = max(0.0, -ext["max_loss"])
            st = UNKNOWN if budget is None else OK if loss <= budget * (1 + _EPS) else VIOLATED
            out.append({"rule": 1, "key": "risk", "status": st, "value": loss, "limit": budget, "detail": "max loss at expiry"})

    limit = dep * premium_pct / 100.0 if dep else None
    prems = [_num(lg.get("entry_premium")) for lg in opts]
    debit = None
    if bought and all(p is not None for p in prems):
        debit = sum((1.0 if lg.get("side") == "long" else -1.0) * float(lg["size"]) * float(p) for lg, p in zip(opts, prems, strict=True))  # type: ignore[arg-type]
    floor = ban_hours / 24.0
    if not bought or (debit is not None and debit <= 0):
        why = "nothing bought" if not bought else "net premium received"
        out.append({"rule": 8, "key": "premium", "status": NA, "value": debit, "limit": limit, "detail": why})
        out.append({"rule": 8, "key": "days_to_expiry", "status": NA, "value": None, "limit": floor, "zone": None, "detail": why})
        return out
    if debit is None or limit is None:
        out.append({"rule": 8, "key": "premium", "status": UNKNOWN, "value": debit, "limit": limit, "detail": "premium or deposit unknown"})
    else:
        st = OK if debit <= limit * (1 + _EPS) else VIOLATED
        out.append({"rule": 8, "key": "premium", "status": st, "value": debit, "limit": limit, "detail": "net premium paid"})

    if _num(expiry_ts) is None:
        out.append(
            {"rule": 8, "key": "days_to_expiry", "status": UNKNOWN, "value": None, "limit": floor, "zone": None, "detail": "expiry unknown"}
        )
        return out
    hours = (float(expiry_ts) - float(now_ts)) / 3600.0  # type: ignore[arg-type]
    days = hours / 24.0
    zone = buy_zone(hours, ban_hours, cond_hours)
    row = {"rule": 8, "key": "days_to_expiry", "value": days, "limit": floor, "zone": zone}
    if zone == ZONE_BAN:
        out.append({**row, "status": VIOLATED, "detail": "under 24 h to expiry: never buy"})
        return out
    if zone == ZONE_FREE:
        out.append({**row, "status": OK, "detail": "over 3 days to expiry: the premium limit only"})
        return out

    th_st, th_why = thesis_check(thesis, target)
    a = {"rule": 8, "key": "thesis", "status": th_st, "value": th_st == OK, "limit": None, "detail": th_why}
    s, r = _num(spot), _num(rv)
    s = s if s is not None and s > 0 else None
    r = r if r is not None and r > 0 else None
    rv_why = "RV30: 30 daily closes, zero mean, x365 (the forecast log's RV30 model)"
    if debit is None:  # a leg without a premium: the payoff would treat it as free and understate the move
        b = {"rule": 8, "key": "breakeven_move", "status": UNKNOWN, "value": None, "limit": None, "detail": "premium unknown"}
    elif s is None or r is None:
        b = {"rule": 8, "key": "breakeven_move", "status": UNKNOWN, "value": None, "limit": None, "detail": "spot or RV unknown"}
    else:
        move, cap = breakeven_move(opts, s), be_max_sigma * r * math.sqrt(days / 365.0)
        if move is None:
            b_st, b_why = VIOLATED, "no breakeven: the structure never profits at expiry"
        else:
            b_st = OK if move <= cap * (1 + _EPS) else VIOLATED
            b_why = f"move to the nearest breakeven vs {be_max_sigma:g} sigma at RV over the time left; {rv_why}"
        b = {"rule": 8, "key": "breakeven_move", "status": b_st, "value": move, "limit": cap, "detail": b_why}
    ivs = [_num(lg.get("iv")) for lg in bought]
    if r is None or not ivs or any(v is None or v <= 0 for v in ivs):
        c = {"rule": 8, "key": "iv_vs_rv", "status": UNKNOWN, "value": None, "limit": None, "detail": "option IV or RV unknown"}
    else:
        iv = max(v for v in ivs if v is not None)
        cap_iv = iv_max_rv * r
        c_st = OK if iv <= cap_iv * (1 + _EPS) else VIOLATED
        c = {
            "rule": 8,
            "key": "iv_vs_rv",
            "status": c_st,
            "value": iv,
            "limit": cap_iv,
            "detail": f"highest bought IV vs {iv_max_rv:g} x RV; {rv_why}",
        }
    st = conditions_status(a["status"], b["status"], c["status"])
    out.append({**row, "status": st, "detail": "1-3 days to expiry: only with all three checks below"})
    out.extend((a, b, c))
    return out


# ---------------------------------------------------------------- the journal: per trade, then by ISO week


def _normalize(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, t in enumerate(rows):
        status = t.get("status")
        opened = _num(t.get("opened_ts"))
        if status not in ("open", "closed") or opened is None:
            continue
        closed = _num(t.get("closed_ts")) if status == "closed" else None
        if status == "closed" and closed is None:
            closed = opened
        tags = _tags(t.get("tags"))
        symbol = str(t.get("symbol") or "")
        exp = option_expiry_ts(symbol)
        is_opt = exp is not None or "option" in tags
        out.append(
            {
                "i": i,
                "id": t.get("id"),
                "opened": opened,
                "closed": closed,
                "open_known": status == "open" or (closed is not None and closed > opened),
                "side": t.get("side"),
                "base": "" if is_opt else base_asset(symbol),
                "hip3": is_hip3(symbol),
                "option": is_opt,
                "expiry": exp,
                "size": _num(t.get("size")),
                "entry": _num(t.get("entry")),
                "stop": _num(t.get("stop")),
                "target": _num(t.get("target")),
                # an import writes a placeholder ("теза не записана"), not the owner's thesis
                "thesis": None if "import" in tags else t.get("thesis"),
                "pnl": _num(t.get("pnl")) if status == "closed" else None,
            }
        )
    return out


def day_stops(
    rows: Iterable[Mapping[str, Any]],
    *,
    deposit_usd: float = DEPOSIT_USD,
    day_loss_pct: float = DAY_LOSS_PCT,
    streak: int = DAY_LOSING_STREAK,
) -> dict[str, dict[str, Any]]:
    """Rule 4: {UTC day: {ts, reason}} for the days the stop was hit: the close (unix s) after which the day had
    ``streak`` losing trades in a row (closing order) or a realized P&L <= -day_loss_pct % of the deposit;
    reason 'streak' | 'day_loss' | 'both'."""
    return _day_stops(_normalize(rows), deposit_usd * day_loss_pct / 100.0, streak)


def _day_stops(xs: list[dict[str, Any]], day_loss: float, streak: int) -> dict[str, dict[str, Any]]:
    closed = sorted((x for x in xs if x["closed"] is not None and x["pnl"] is not None), key=lambda x: (x["closed"], x["i"]))
    stops: dict[str, dict[str, Any]] = {}
    run: dict[str, tuple[int, float]] = {}
    for x in closed:
        day = utc_day(x["closed"])
        if day in stops:
            continue
        n, cum = run.get(day, (0, 0.0))
        n = n + 1 if x["pnl"] < 0 else 0
        cum += x["pnl"]
        run[day] = (n, cum)
        hit_streak, hit_loss = n >= streak, cum <= -day_loss + _EPS
        if hit_streak or hit_loss:
            stops[day] = {"ts": x["closed"], "reason": "both" if hit_streak and hit_loss else "streak" if hit_streak else "day_loss"}
    return stops


def trade_checks(
    rows: Iterable[Mapping[str, Any]],
    *,
    now_ts: float,
    deposit_usd: float = DEPOSIT_USD,
    risk_pct: float = RISK_PCT,
    max_alt_longs: int = MAX_ALT_LONGS,
    day_loss_pct: float = DAY_LOSS_PCT,
    streak: int = DAY_LOSING_STREAK,
    min_rr: float = MIN_RR,
    premium_pct: float = PREMIUM_PCT,
    ban_hours: float = BUY_BAN_HOURS,
    cond_hours: float = BUY_COND_HOURS,
) -> list[dict[str, Any]]:
    """Each open or closed journal row (pass the owner's real trades: no airdrop, no *-paper rows) with the status of
    rules 1, 2, 4, 5 and 8: {id, week, opened_ts, pnl, rules: {n: status}, approx: rule 4 judged on the close time,
    alt_longs: distinct alt longs open right after this one opened (rule 2, or None), buy_zone, buy_conditions,
    late_buy, big_premium}. For a bought option: ``buy_zone`` is the rule 8 zone of the opening (None without a real
    opening time or expiry), ``buy_conditions`` the three checks in the 1-3 day zone ({thesis, breakeven_move,
    iv_vs_rv}: statuses; the journal has no spot or IV at the opening, so the last two are ``unknown``), ``late_buy``
    True when the timing broke the rule (under 24 h, or 1-3 days with a check failed), None when it cannot tell.
    ``week`` is the ISO week (UTC) of the opening; open trades count up to ``now_ts``."""
    xs = _normalize(rows)
    risk_cap = deposit_usd * risk_pct / 100.0
    prem_cap = deposit_usd * premium_pct / 100.0
    stops = _day_stops(xs, deposit_usd * day_loss_pct / 100.0, streak)
    alt_n = _alt_long_counts(xs, now_ts)
    out = []
    for x in xs:
        r: dict[int, str] = {}
        long_opt = x["option"] and x["side"] == "long"
        premium = x["size"] * x["entry"] if long_opt and x["size"] is not None and x["entry"] is not None else None
        # rule 1: a bought option risks its premium; anything else needs a recorded stop
        risk = _risk(x)
        r[1] = UNKNOWN if risk is None else OK if risk <= risk_cap * (1 + _EPS) else VIOLATED
        # rule 2
        if x["option"] or x["side"] != "long" or x["base"] in MAJORS or not x["base"]:
            r[2] = NA
        elif x["hip3"] or x["i"] not in alt_n:
            r[2] = UNKNOWN
        else:
            r[2] = VIOLATED if alt_n[x["i"]][1] else OK
        # rule 4
        stop = stops.get(utc_day(x["opened"]))
        r[4] = VIOLATED if stop is not None and x["opened"] > stop["ts"] else OK
        # rule 5
        if x["option"]:
            r[5] = NA
        elif None in (x["stop"], x["target"], x["entry"]) or x["entry"] == x["stop"]:
            r[5] = UNKNOWN
        else:
            r[5] = OK if abs(x["target"] - x["entry"]) / abs(x["entry"] - x["stop"]) >= min_rr - _EPS else VIOLATED
        # rule 8
        late = big = zone = conds = None
        if long_opt:
            big = None if premium is None else premium > prem_cap * (1 + _EPS)
            if x["expiry"] is not None and x["open_known"]:
                zone = buy_zone((x["expiry"] - x["opened"]) / 3600.0, ban_hours, cond_hours)
            if zone == ZONE_COND:
                conds = {"thesis": thesis_check(x["thesis"], x["target"])[0], "breakeven_move": UNKNOWN, "iv_vs_rv": UNKNOWN}
                cst = conditions_status(*conds.values())
                late = True if cst == VIOLATED else None if cst == UNKNOWN else False
            elif zone is not None:
                late = zone == ZONE_BAN
            r[8] = VIOLATED if late or big else UNKNOWN if late is None or big is None else OK
        else:
            r[8] = NA
        out.append(
            {
                "id": x["id"],
                "week": iso_week(x["opened"]),
                "opened_ts": x["opened"],
                "pnl": x["pnl"],
                "rules": r,
                "approx": r[4] == VIOLATED and not x["open_known"],
                "alt_longs": alt_n[x["i"]][0] if x["i"] in alt_n else None,
                "buy_zone": zone,
                "buy_conditions": conds,
                "late_buy": late,
                "big_premium": big,
            }
        )
    return out


def _risk(x: Mapping[str, Any]) -> float | None:
    """Rule 1 risk (USD) of a normalized row: the premium of a bought option, size x |entry - stop| of anything else
    with a recorded stop; None when the row cannot tell (no stop, a sold option)."""
    if x["option"] and x["side"] == "long":
        return x["size"] * x["entry"] if x["size"] is not None and x["entry"] is not None else None
    if x["option"] or None in (x["stop"], x["size"], x["entry"]):
        return None
    return x["size"] * abs(x["entry"] - x["stop"])


def open_risk(
    rows: Iterable[Mapping[str, Any]], *, deposit_usd: float = DEPOSIT_USD, open_risk_pct: float = OPEN_RISK_PCT
) -> dict[str, Any]:
    """Rule 2, first half: the total risk of the trades open now (status 'open'; pass the owner's real trades) <=
    open_risk_pct % of the deposit. {status, total: USD over the open trades whose rule 1 risk is known, limit,
    n_open, n_unknown: open trades without a stop (or a sold option)}. ``violated`` once the known part alone is over
    the limit, else ``unknown`` while any open risk is unknown, else ``ok``; ``na`` with nothing open. The risk is to
    the recorded stop: a stop moved since, slippage or a gap are not seen."""
    xs = [x for x in _normalize(rows) if x["closed"] is None]
    risks = [_risk(x) for x in xs]
    total = sum(v for v in risks if v is not None)
    limit = deposit_usd * open_risk_pct / 100.0
    n_unknown = sum(1 for v in risks if v is None)
    if not xs:
        st = NA
    elif total > limit * (1 + _EPS):
        st = VIOLATED
    else:
        st = UNKNOWN if n_unknown else OK
    return {"status": st, "total": total, "limit": limit, "n_open": len(xs), "n_unknown": n_unknown}


def _alt_long_counts(xs: list[dict[str, Any]], now_ts: float, cap: int = MAX_ALT_LONGS) -> dict[int, tuple[int, bool]]:
    """Rule 2 sweep over alt longs with a real opening time: {row index: (distinct alt longs open right after it
    opened, it opened a new alt beyond ``cap``)}. A second row of an alt already open adds no new long. A closed trade
    is held until its close (exclusive), an open one up to ``now_ts`` inclusive, so a trade opened at ``now_ts`` (a
    /trade from Telegram) still sees the open ones."""
    alts = sorted(
        (
            x
            for x in xs
            if not x["option"] and x["side"] == "long" and x["base"] and x["base"] not in MAJORS and x["open_known"] and not x["hip3"]
        ),
        key=lambda x: (x["opened"], x["i"]),
    )
    active: list[tuple[float, str, bool]] = []
    out: dict[int, tuple[int, bool]] = {}
    for x in alts:
        t = x["opened"]
        active = [(end, b, still) for end, b, still in active if end > t or (still and end >= t)]
        held = {b for _, b, _ in active}
        new = x["base"] not in held
        n = len(held) + (1 if new else 0)
        out[x["i"]] = (n, new and n > cap)
        still_open = x["closed"] is None
        active.append((max(float(now_ts), t) if still_open else x["closed"], x["base"], still_open))
    return out


def weekly_violations(rows: Iterable[Mapping[str, Any]], *, now_ts: float, **params: Any) -> list[dict[str, Any]]:
    """Rule violations by ISO week of the opening (UTC), newest first: {week, n_trades, rules: {n: {status, violated,
    checked, unknown}}, days_stopped, approx, max_alt_longs, late_buys, buys_under_24h, buys_cond_failed,
    big_premiums, n_violating, pnl_violating, n_violating_approx, pnl_violating_approx}. Rule 8: ``late_buys`` =
    ``buys_under_24h`` (never buy) + ``buys_cond_failed`` (1-3 days with a check failed).
    ``checked`` = trades the rule applies to with enough data, ``unknown`` = applies but the data cannot tell.
    Exact and approximate are kept apart: rule 4's ``violated`` / ``checked`` leave out the flags judged on the close
    time (``approx`` counts those); ``n_violating`` / ``pnl_violating`` are the trades with at least one exact
    violation and their closed P&L, ``n_violating_approx`` / ``pnl_violating_approx`` the trades whose only
    violation is an approximate rule 4 (P&L sums are None when no such trade has a P&L).
    ``params`` are trade_checks' thresholds (deposit_usd, risk_pct, ...)."""
    rows = list(rows)
    checks = trade_checks(rows, now_ts=now_ts, **params)
    dep = float(params.get("deposit_usd", DEPOSIT_USD))
    stops = day_stops(
        rows, deposit_usd=dep, day_loss_pct=params.get("day_loss_pct", DAY_LOSS_PCT), streak=params.get("streak", DAY_LOSING_STREAK)
    )
    weeks: dict[str, list[dict[str, Any]]] = {}
    for c in checks:
        weeks.setdefault(c["week"], []).append(c)
    for s in stops.values():  # a stop hit by a trade opened the week before still shows in its own week
        weeks.setdefault(iso_week(s["ts"]), [])
    out = []
    for wk in sorted(weeks, reverse=True):
        cs = weeks[wk]
        rules = {}
        for n in RULES:
            st = [c["rules"][n] for c in cs if not (n == 4 and c["approx"])]  # rule 4 on the close time: apart
            v, k, u = st.count(VIOLATED), st.count(OK) + st.count(VIOLATED), st.count(UNKNOWN)
            rules[n] = {"status": _status(v, k, u), "violated": v, "checked": k, "unknown": u}
        bad = [c for c in cs if _exact_violation(c)]
        bad_approx = [c for c in cs if c["approx"] and not _exact_violation(c)]
        pnls = [c["pnl"] for c in bad if c["pnl"] is not None]
        pnls_approx = [c["pnl"] for c in bad_approx if c["pnl"] is not None]
        alts = [c["alt_longs"] for c in cs if c["alt_longs"] is not None]
        out.append(
            {
                "week": wk,
                "n_trades": len(cs),
                "rules": rules,
                "days_stopped": sum(1 for s in stops.values() if iso_week(s["ts"]) == wk),
                "approx": sum(1 for c in cs if c["approx"]),
                "max_alt_longs": max(alts) if alts else None,
                "late_buys": sum(1 for c in cs if c["late_buy"]),
                "buys_under_24h": sum(1 for c in cs if c["buy_zone"] == ZONE_BAN),
                "buys_cond_failed": sum(1 for c in cs if c["buy_zone"] == ZONE_COND and c["late_buy"]),
                "big_premiums": sum(1 for c in cs if c["big_premium"]),
                "n_violating": len(bad),
                "pnl_violating": sum(pnls) if pnls else None,
                "n_violating_approx": len(bad_approx),
                "pnl_violating_approx": sum(pnls_approx) if pnls_approx else None,
            }
        )
    return out


def _exact_violation(c: Mapping[str, Any]) -> bool:
    """A trade_checks row breaks a rule for sure: any violation but a rule 4 judged on the close time."""
    return any(st == VIOLATED and not (n == 4 and c["approx"]) for n, st in c["rules"].items())
