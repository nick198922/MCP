"""Legs of the weekly Deribit condor, picked from a live book summary (IDEA-20261031-structural-edges-depth, item 8d).

Pure functions, no I/O; time is a parameter. The collector `collector/option_legs.py` stores one row per leg a day so
the condor modules H1 / H4 of docs/verification/2026-10-31-options-modules.md can be checked forward: in the trade-print
cache only ~60 % of Fridays had fresh marks on all four legs. Units as in `analytics/option_condor.py`: IV decimal
(0.45 = 45 %), T in years = days / 365, Black-76 with r = 0 on the expiry's forward, Deribit expiries 08:00 UTC; option
prices (bid / ask / mark) are Deribit's, in the coin per 1 coin of underlying (x spot = USD).

Legs of the expiry (the nearest Friday 08:00 UTC after the snapshot, i.e. on a Friday from 09:00 the next week's):
  * ``atm_p`` / ``atm_c``: the listed strike nearest the forward;
  * ``sp25`` / ``sc25`` and ``sp10`` / ``sc10``: the listed strike nearest the strike whose Black-76 delta at the
    expiry's mark smile is -0.25 / +0.25 and -0.10 / +0.10 (`option_condor.strike_for_delta`, the H1 rule);
  * ``lp`` / ``lc``: H1's wings, target min(0.85 S, K_sp10 - 0.05 S) / max(1.15 S, K_sc10 + 0.05 S) (S = index);
  * ``lp15`` / ``lc15``: plain wings at 0.85 S / 1.15 S (the 25-delta condor's wings; often the same strike as lp / lc).
Every row keeps the target strike and ``dist`` = (strike - target) / S, so the registered "nearest strike within
+-1.5 % of spot" band is applied at read time, not here.

Held legs (Saturday to Thursday): besides the re-picked legs above (the ageing series' smile), the instruments picked
at the last Friday entry are quoted again under ``hold_<role>`` (`hold_legs`), so the path of the condor opened on
Friday is kept (stops, early buy-back, in-week drawdown, exit cost at bid / ask). A hold row keeps the Friday leg's
instrument, strike, target and floor; spot, forward, tau, quotes and delta are the day's.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .instruments import parse_option_name
from .option_condor import strike_for_delta
from .option_xvenue import num
from .options import black76_greeks, year_fraction

SNAPSHOT_HOUR_UTC = 9  # same reference hour as the backtest's day surfaces (put_skew.build_day ref_hour = 9)
WING = 0.15  # wings at +-15 % of spot (H1)
FRIDAY = 4  # datetime.weekday()
EXPIRY_HOUR_UTC = 8
ROLES = ("lp15", "lp", "sp10", "sp25", "atm_p", "atm_c", "sc25", "sc10", "lc", "lc15")
ROLE_OPT = {"lp15": "P", "lp": "P", "sp10": "P", "sp25": "P", "atm_p": "P", "atm_c": "C", "sc25": "C", "sc10": "C", "lc": "C", "lc15": "C"}
SHORT_DELTAS = {"sp10": (-0.10, False), "sc10": (0.10, True), "sp25": (-0.25, False), "sc25": (0.25, True)}


def snapshot_day(now: datetime) -> str | None:
    """UTC date 'YYYY-MM-DD' whose snapshot is due at ``now`` (from 09:00 UTC on), else None (before 09:00)."""
    now = now.astimezone(UTC)
    return now.strftime("%Y-%m-%d") if now.hour >= SNAPSHOT_HOUR_UTC else None


def weekly_expiry(now: datetime) -> datetime:
    """The nearest Friday 08:00 UTC strictly after ``now`` (on a Friday after 08:00: the next Friday)."""
    now = now.astimezone(UTC)
    d = now.replace(hour=EXPIRY_HOUR_UTC, minute=0, second=0, microsecond=0) + timedelta(days=(FRIDAY - now.weekday()) % 7)
    return d if d > now else d + timedelta(days=7)


def entry_day(now: datetime) -> str | None:
    """UTC date of the Friday entry whose condor is still open at ``now`` (Saturday to Thursday: the previous Friday);
    None on a Friday (that week's condor expired at 08:00, the new one is entered today)."""
    now = now.astimezone(UTC)
    back = (now.weekday() - FRIDAY) % 7
    return (now - timedelta(days=back)).strftime("%Y-%m-%d") if back else None


def hold_legs(entry_rows: list[Mapping[str, Any]], expiry: datetime) -> list[dict[str, Any]]:
    """Legs ``hold_<role>`` from the stored Friday entry rows of one currency (``Store.option_leg_rows``) whose expiry is
    ``expiry``; other rows (another expiry, hold rows) are ignored. Leg shape as in `plan_legs`, ROLES order."""
    exp_ts = int(expiry.timestamp())
    by_role = {r["role"]: r for r in entry_rows if r.get("role") in ROLE_OPT and r.get("expiry_ts") == exp_ts}
    return [
        {
            "role": f"hold_{role}",
            "opt": r["opt"],
            "target": r["target"],
            "strike": r["strike"],
            "instrument": r["instrument"],
            "floor": None if r.get("floor") is None else bool(r["floor"]),
        }
        for role in ROLES
        if (r := by_role.get(role)) is not None
    ]


def _nonneg(v: Any) -> float | None:
    """A finite float >= 0 (open interest, volume: 0 is a real value here), else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if 0 <= x < float("inf") else None


def expiry_surface(summary: Any, currency: str, expiry: datetime, now: datetime) -> dict[str, Any] | None:
    """The expiry's smile from a Deribit ``get_book_summary_by_currency?kind=option`` answer.

    Returns ``{"F", "T", "spot", "strikes": {K: {"mark_iv"}}, "names": {(K, "C"|"P"): instrument}, "rows": {name: row}}``
    or None when the expiry is absent or has no forward / index / mark IV. F = median ``underlying_price`` and spot =
    median ``estimated_delivery_price`` (the index) over the expiry's rows; the smile IV of a strike is the mark IV of
    its out-of-the-money option (put below F, call at or above), else of the other one.
    """
    rows: dict[str, dict[str, Any]] = {}
    names: dict[tuple[float, str], str] = {}
    for r in summary if isinstance(summary, list) else []:
        p = parse_option_name(str(r.get("instrument_name", ""))) if isinstance(r, dict) else None
        if p is None or p.product != currency or p.expiry != expiry:
            continue
        rows[p.raw] = r
        names[(p.strike, "C" if p.is_call else "P")] = p.raw
    fwd = [x for x in (num(r.get("underlying_price")) for r in rows.values()) if x is not None]
    idx = [x for x in (num(r.get("estimated_delivery_price")) for r in rows.values()) if x is not None]
    T = year_fraction(expiry, now)
    if not fwd or not idx or T <= 0:
        return None
    F = statistics.median(fwd)
    strikes: dict[float, dict[str, float]] = {}
    for K in sorted({k for k, _ in names}):
        first, second = ("P", "C") if K < F else ("C", "P")
        for t in (first, second):
            iv = num(rows[names[(K, t)]].get("mark_iv")) if (K, t) in names else None
            if iv is not None:
                strikes[K] = {"mark_iv": iv / 100.0}
                break
    if not strikes:
        return None
    return {"F": F, "T": T, "spot": statistics.median(idx), "strikes": strikes, "names": names, "rows": rows}


def _nearest(names: Mapping[tuple[float, str], str], target: float, opt: str) -> float | None:
    ks = [k for k, t in names if t == opt]
    return min(ks, key=lambda k: (abs(k - target), k)) if ks else None


def plan_legs(exp: dict[str, Any], wing: float = WING) -> tuple[list[dict[str, Any]], list[str]]:
    """(legs, roles that could not be placed). A leg: ``{"role", "opt", "target", "strike", "instrument", "floor"}``;
    ``floor`` (lp / lc only) is True when H1's w/3 width floor, not the +-15 % level, set the wing target."""
    F, S, names = exp["F"], exp["spot"], exp["names"]
    targets: dict[str, float | None] = {"atm_p": F, "atm_c": F, "lp15": (1.0 - wing) * S, "lc15": (1.0 + wing) * S}
    for role, (delta, is_call) in SHORT_DELTAS.items():
        targets[role] = strike_for_delta(exp, delta, is_call)
    strike: dict[str, float | None] = {}
    for role in ("atm_p", "atm_c", "sp10", "sc10", "sp25", "sc25", "lp15", "lc15"):
        t = targets[role]
        strike[role] = _nearest(names, t, ROLE_OPT[role]) if t is not None else None
    floor: dict[str, bool] = {}
    if strike["sp10"] is not None:
        targets["lp"] = min((1.0 - wing) * S, strike["sp10"] - wing / 3.0 * S)
        floor["lp"] = targets["lp"] < (1.0 - wing) * S * (1.0 - 1e-9)
    if strike["sc10"] is not None:
        targets["lc"] = max((1.0 + wing) * S, strike["sc10"] + wing / 3.0 * S)
        floor["lc"] = targets["lc"] > (1.0 + wing) * S * (1.0 + 1e-9)
    for role in ("lp", "lc"):
        t = targets.get(role)
        strike[role] = _nearest(names, t, ROLE_OPT[role]) if t is not None else None
    legs, lost = [], []
    for role in ROLES:
        K = strike.get(role)
        if K is None:
            lost.append(role)
            continue
        opt = ROLE_OPT[role]
        legs.append(
            {"role": role, "opt": opt, "target": targets[role], "strike": K, "instrument": names[(K, opt)], "floor": floor.get(role)}
        )
    return legs, lost


def leg_row(
    leg: Mapping[str, Any], exp: Mapping[str, Any], ticker: Mapping[str, Any] | None, *, currency: str, expiry: datetime, now: datetime
) -> dict[str, Any]:
    """One stored row (``Store.OPTION_LEG_COLUMNS``). Quotes come from the instrument's ``public/ticker`` when it answered
    (``src = 'ticker'``: bid / ask with their sizes and IVs), else from its book-summary row (``src = 'summary'``: bid /
    ask / mark / mark IV only). Deribit sends 0 for an empty side: stored as NULL. ``delta`` is Black-76 at the row's
    mark IV on the expiry's forward (r = 0), the same convention as the backtest; NULL without a mark IV."""
    s = exp["rows"].get(leg["instrument"], {})
    if ticker is not None:
        q = {
            "src": "ticker",
            "bid": num(ticker.get("best_bid_price")),
            "ask": num(ticker.get("best_ask_price")),
            "mark": num(ticker.get("mark_price")),
            "mark_iv": num(ticker.get("mark_iv")),
            "bid_iv": num(ticker.get("bid_iv")),
            "ask_iv": num(ticker.get("ask_iv")),
            "bid_size": num(ticker.get("best_bid_amount")),
            "ask_size": num(ticker.get("best_ask_amount")),
            "quote_ts_ms": ticker.get("timestamp"),
            "open_interest": _nonneg(ticker.get("open_interest")),
            "volume": _nonneg((ticker.get("stats") or {}).get("volume")) if isinstance(ticker.get("stats"), dict) else None,
        }
    else:
        q = {
            "src": "summary",
            "bid": num(s.get("bid_price")),
            "ask": num(s.get("ask_price")),
            "mark": num(s.get("mark_price")),
            "mark_iv": num(s.get("mark_iv")),
            "bid_iv": None,
            "ask_iv": None,
            "bid_size": None,
            "ask_size": None,
            "quote_ts_ms": s.get("creation_timestamp"),
            "open_interest": _nonneg(s.get("open_interest")),
            "volume": _nonneg(s.get("volume")),
        }
    for k in ("mark_iv", "bid_iv", "ask_iv"):  # Deribit IVs are percent
        q[k] = q[k] / 100.0 if q[k] is not None else None
    ts = q["quote_ts_ms"]
    q["quote_ts_ms"] = int(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None
    K, S = leg["strike"], exp["spot"]
    is_call = leg["opt"] == "C"
    delta = black76_greeks(exp["F"], K, exp["T"], q["mark_iv"], is_call)["delta"] if q["mark_iv"] else None
    return {
        "currency": currency,
        "role": leg["role"],
        "expiry_ts": int(expiry.timestamp()),
        "tau_days": (expiry - now).total_seconds() / 86400.0,
        "spot": S,
        "forward": exp["F"],
        "opt": leg["opt"],
        "instrument": leg["instrument"],
        "target": leg["target"],
        "strike": K,
        "dist": (K - leg["target"]) / S,
        "floor": None if leg.get("floor") is None else int(leg["floor"]),
        "delta": delta,
        **q,
    }
