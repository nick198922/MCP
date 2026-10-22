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
  "total open risk <= 3 %" half needs stops.
* 4 — day stop: after 2 losing trades in a row or a day's realized P&L <= -2 % of the deposit, no new trade that
  day. UTC day and closing order as in scripts/journal_rules.py that produced the rule's effect, but "in a row" is
  stricter than that script, which stopped the day after any 2 losses. A trade opened after the stop moment of its
  day violates it. A row without an opening time stands in with its close time: such a flag is approximate (the
  trade may have been opened long before the stop), counted in ``approx`` and kept apart from exact violations in
  the weekly totals. The "48 h pause after a big-loss day" half has no threshold in the rule: not checked.
* 5 — target >= 2 x the stop distance (R:R >= 2): rows with both stop and target, else ``unknown``.
* 8 — options: no buying with <= 3 days to expiry (Deribit/Bybit delivery 08:00 UTC, the date in the symbol);
  premium paid <= 1 % of the deposit per trade. A structure that nets a credit buys nothing on balance (``na``).
  Journal: bought option rows (side long); the days need a real opening time, else ``unknown``.

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

from .strategy import payoff_extremes

OK, VIOLATED, UNKNOWN, NA = "ok", "violated", "unknown", "na"
DEPOSIT_USD = 10_000.0
RISK_PCT = 1.0  # rule 1, % of the deposit
MAX_ALT_LONGS = 3  # rule 2
DAY_LOSS_PCT = 2.0  # rule 4, % of the deposit
DAY_LOSING_STREAK = 2  # rule 4
MIN_RR = 2.0  # rule 5
PREMIUM_PCT = 1.0  # rule 8, % of the deposit
MIN_BUY_DAYS = 3.0  # rule 8: buying with this many days or fewer to expiry violates it
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


# ---------------------------------------------------------------- before a trade: an option structure


def pre_trade_option(
    legs: Sequence[Mapping[str, Any]],
    *,
    now_ts: float,
    expiry_ts: float | None,
    deposit_usd: float = DEPOSIT_USD,
    risk_pct: float = RISK_PCT,
    premium_pct: float = PREMIUM_PCT,
    min_buy_days: float = MIN_BUY_DAYS,
) -> list[dict[str, Any]]:
    """Rules 1 and 8 on a planned option structure (legs in the analytics.strategy format: kind 'call' | 'put',
    side 'long' | 'short', size, strike, entry_premium in USD per unit). One dict per check:
    {rule, key, status, value, limit, detail}; ``key`` is 'risk' (max loss at expiry, USD), 'premium' (net premium
    paid, USD) or 'days_to_expiry' (days from ``now_ts`` to ``expiry_ts``). Rule 8 is about paying premium: a
    structure that nets a credit (a sale, an iron condor) gives ``na`` for both of its checks."""
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
    if not bought or (debit is not None and debit <= 0):
        why = "nothing bought" if not bought else "net premium received"
        out.append({"rule": 8, "key": "premium", "status": NA, "value": debit, "limit": limit, "detail": why})
        out.append({"rule": 8, "key": "days_to_expiry", "status": NA, "value": None, "limit": min_buy_days, "detail": why})
        return out
    if debit is None or limit is None:
        out.append({"rule": 8, "key": "premium", "status": UNKNOWN, "value": debit, "limit": limit, "detail": "premium or deposit unknown"})
    else:
        st = OK if debit <= limit * (1 + _EPS) else VIOLATED
        out.append({"rule": 8, "key": "premium", "status": st, "value": debit, "limit": limit, "detail": "net premium paid"})

    if _num(expiry_ts) is None:
        out.append(
            {"rule": 8, "key": "days_to_expiry", "status": UNKNOWN, "value": None, "limit": min_buy_days, "detail": "expiry unknown"}
        )
    else:
        days = (float(expiry_ts) - float(now_ts)) / DAY_S  # type: ignore[arg-type]
        st = VIOLATED if days <= min_buy_days else OK
        out.append(
            {
                "rule": 8,
                "key": "days_to_expiry",
                "status": st,
                "value": days,
                "limit": min_buy_days,
                "detail": "buying this close to expiry",
            }
        )
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
    min_buy_days: float = MIN_BUY_DAYS,
) -> list[dict[str, Any]]:
    """Each open or closed journal row (pass the owner's real trades: no airdrop, no *-paper rows) with the status of
    rules 1, 2, 4, 5 and 8: {id, week, opened_ts, pnl, rules: {n: status}, approx: rule 4 judged on the close time,
    alt_longs: distinct alt longs open right after this one opened (rule 2, or None), late_buy, big_premium}.
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
        if long_opt:
            r[1] = UNKNOWN if premium is None else OK if premium <= risk_cap * (1 + _EPS) else VIOLATED
        elif x["option"] or None in (x["stop"], x["size"], x["entry"]):
            r[1] = UNKNOWN
        else:
            r[1] = OK if x["size"] * abs(x["entry"] - x["stop"]) <= risk_cap * (1 + _EPS) else VIOLATED
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
        late = big = None
        if long_opt:
            big = None if premium is None else premium > prem_cap * (1 + _EPS)
            late = None if x["expiry"] is None or not x["open_known"] else (x["expiry"] - x["opened"]) / DAY_S <= min_buy_days
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
                "late_buy": late,
                "big_premium": big,
            }
        )
    return out


def _alt_long_counts(xs: list[dict[str, Any]], now_ts: float, cap: int = MAX_ALT_LONGS) -> dict[int, tuple[int, bool]]:
    """Rule 2 sweep over alt longs with a real opening time: {row index: (distinct alt longs open right after it
    opened, it opened a new alt beyond ``cap``)}. A second row of an alt already open adds no new long."""
    alts = sorted(
        (
            x
            for x in xs
            if not x["option"] and x["side"] == "long" and x["base"] and x["base"] not in MAJORS and x["open_known"] and not x["hip3"]
        ),
        key=lambda x: (x["opened"], x["i"]),
    )
    active: list[tuple[float, str]] = []
    out: dict[int, tuple[int, bool]] = {}
    for x in alts:
        t = x["opened"]
        active = [(end, b) for end, b in active if end > t]
        held = {b for _, b in active}
        new = x["base"] not in held
        n = len(held) + (1 if new else 0)
        out[x["i"]] = (n, new and n > cap)
        end = x["closed"] if x["closed"] is not None else max(float(now_ts), t)
        active.append((end, x["base"]))
    return out


def weekly_violations(rows: Iterable[Mapping[str, Any]], *, now_ts: float, **params: Any) -> list[dict[str, Any]]:
    """Rule violations by ISO week of the opening (UTC), newest first: {week, n_trades, rules: {n: {status, violated,
    checked, unknown}}, days_stopped, approx, max_alt_longs, late_buys, big_premiums, n_violating, pnl_violating,
    n_violating_approx, pnl_violating_approx}.
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
