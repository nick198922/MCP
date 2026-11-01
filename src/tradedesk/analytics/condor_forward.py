"""Forward check of the weekly Deribit iron condor H1 / H4 on `option_leg_snapshot` rows (pure functions, no I/O).

Registration: IDEA-20261031-structural-edges-depth, item 8j (docs/ideas/IDEA-20261031-structural-edges-depth.md).
Part (a) compares the executable cost of the condor (sold legs at the bid, bought legs at the ask; on a close the
sides swap) with the backtest's cost model "mark -/+ 1.5 vol points" (`option_condor.exec_legs(model="mark")`).
Prices are Deribit inverse quotes in the coin per 1 coin of underlying, i.e. fractions of spot (x spot = USD).

Reading rule of an entry (`friday_entry`): the currency's rows of that Friday with ``ts`` <= Friday 10:00 UTC, the
H1 roles ``lp sp10 sc10 lc``, the expiry the next Friday 08:00 UTC, every leg within +-1.5 % of spot of its target
(``|dist| <= 0.015``) and the strike order ``lp < sp10 < forward < sc10 < lc``. Rows after the cutoff never matter.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from tradedesk.analytics.put_skew import inverse_price

H1_QTY = {"lp": 1, "sp10": -1, "sc10": -1, "lc": 1}  # +1 bought, -1 sold (1 coin of notional per leg)
BAND = 0.015  # leg within +-1.5 % of spot of its target (item 7 rule)
ENTRY_CUTOFF_HOUR_UTC = 10  # entry snapshot must be taken by Friday 10:00 UTC (8d run note)
EXPIRY_HOUR_UTC = 8
HS = 0.015  # cost model: mark -/+ 1.5 vol points
Z_MDE = 2.8  # MDE = 2.8 * SD / sqrt(n): two-sided 5 %, power 80 %


def _ts(d: date, hour: int) -> int:
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=UTC).timestamp())


def _keep_earliest(legs: dict[str, Mapping[str, Any]], key: str, row: Mapping[str, Any]) -> None:
    """Keep the row with the earliest ``ts`` per key, independent of the iteration order (the store has one row per
    (day, currency, role), but this pure function does not rely on it; ties keep the first row seen)."""
    old = legs.get(key)
    if old is None or int(row.get("ts") or 0) < int(old.get("ts") or 0):
        legs[key] = row


def friday_entry(rows: Iterable[Mapping[str, Any]], friday: date, currency: str) -> tuple[dict[str, Mapping[str, Any]] | None, str]:
    """The H1 entry legs ``{role: row}`` of ``currency`` on ``friday`` by the registered reading rule, or (None, reason).

    Several rows of one role (not possible in the store, PK (day, currency, role)) resolve to the earliest ``ts`` <=
    the cutoff: a later snapshot never replaces the entry. Rows of another expiry are ignored."""
    if friday.weekday() != 4:
        return None, "not a Friday"
    cutoff = _ts(friday, ENTRY_CUTOFF_HOUR_UTC)
    expiry = _ts(friday + timedelta(days=7), EXPIRY_HOUR_UTC)
    day = friday.isoformat()
    legs: dict[str, Mapping[str, Any]] = {}
    for r in rows:
        if r.get("currency") != currency or r.get("day") != day or r.get("role") not in H1_QTY:
            continue
        if int(r.get("ts") or 0) > cutoff or r.get("expiry_ts") != expiry:
            continue
        _keep_earliest(legs, str(r["role"]), r)
    missing = [k for k in H1_QTY if k not in legs]
    if missing:
        return None, "no row: " + " ".join(missing)
    far = [k for k in H1_QTY if abs(float(legs[k].get("dist") or 0.0)) > BAND]
    if far:
        return None, "outside +-1.5 %: " + " ".join(far)
    F = float(legs["sp10"]["forward"])
    K = [float(legs[k]["strike"]) for k in ("lp", "sp10")] + [F] + [float(legs[k]["strike"]) for k in ("sc10", "lc")]
    if not all(a < b for a, b in zip(K, K[1:], strict=False)):
        return None, "strike order"
    return legs, "ok"


def thursday_exit(
    rows: Iterable[Mapping[str, Any]], entry: Mapping[str, Mapping[str, Any]], friday: date, currency: str
) -> tuple[dict[str, Mapping[str, Any]] | None, str]:
    """Buy-back legs ``{role: row}`` of the condor entered on ``friday``: the ``hold_<role>`` rows of the Thursday
    after it (the last daily snapshot before the Friday 08:00 expiry, ~23 h) with ``ts`` <= Thursday 10:00 UTC and the
    entry's instrument and expiry (several rows of one role: the earliest ``ts``); else (None, reason)."""
    thu = friday + timedelta(days=6)
    cutoff = _ts(thu, ENTRY_CUTOFF_HOUR_UTC)
    want = {"hold_" + k: k for k in H1_QTY}
    legs: dict[str, Mapping[str, Any]] = {}
    for r in rows:
        k = want.get(str(r.get("role")))
        if k is None or r.get("currency") != currency or r.get("day") != thu.isoformat() or int(r.get("ts") or 0) > cutoff:
            continue
        if r.get("instrument") == entry[k].get("instrument") and r.get("expiry_ts") == entry[k].get("expiry_ts"):
            _keep_earliest(legs, k, r)
    missing = [k for k in H1_QTY if k not in legs]
    return (None, "no hold row: " + " ".join(missing)) if missing else (legs, "ok")


def leg_cost(row: Mapping[str, Any], qty: int, action: str = "open", hs: float = HS) -> tuple[float, float] | None:
    """(actual, model) cost of one leg vs its mark, in the coin per coin (fraction of spot), both >= 0 when sane.

    ``action`` "open": qty < 0 sells at the bid, qty > 0 buys at the ask; "close" swaps the sides (a sold leg is bought
    back at the ask). actual = |executable - mark|; model = |inverse_price(iv -/+ hs) - inverse_price(iv)| on the row's
    forward and tau (sell at mark IV - hs, buy at mark IV + hs). None if a needed quote is missing or the src is not a
    ticker (the book summary has no IVs per side and no sizes)."""
    sell = (qty < 0) == (action == "open")
    px = row.get("bid") if sell else row.get("ask")
    mark, iv = row.get("mark"), row.get("mark_iv")
    if row.get("src") != "ticker" or px is None or mark is None or not iv:
        return None
    F, K, T, call = float(row["forward"]), float(row["strike"]), float(row["tau_days"]) / 365.0, row["opt"] == "C"
    base = inverse_price(F, K, T, float(iv), call)
    shifted = inverse_price(F, K, T, max(float(iv) - hs, 1e-6) if sell else float(iv) + hs, call)
    actual = (float(mark) - float(px)) if sell else (float(px) - float(mark))
    return actual, abs(base - shifted)


def condor_cost(legs: Mapping[str, Mapping[str, Any]], action: str = "open", hs: float = HS) -> dict[str, float] | None:
    """Summed (actual, model) cost of the four H1 legs, fractions of spot; None if any leg lacks its quote."""
    a = m = 0.0
    for role, q in H1_QTY.items():
        c = leg_cost(legs[role], q, action, hs)
        if c is None:
            return None
        a, m = a + c[0], m + c[1]
    return {"actual": a, "model": m, "ratio": a / m if m > 0 else math.nan}


def ratio_of_sums(actual: Sequence[float], model: Sequence[float]) -> float:
    s = sum(model)
    return sum(actual) / s if s > 0 else math.nan


def ratio_ci(actual: Sequence[float], model: Sequence[float], *, n_boot: int = 10_000, seed: int = 20261031) -> tuple[float, float]:
    """Percentile 95 % CI of the ratio of sums, i.i.d. bootstrap over weeks."""
    n = len(actual)
    rng = random.Random(seed)
    rs = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        r = ratio_of_sums([actual[i] for i in idx], [model[i] for i in idx])
        if not math.isnan(r):
            rs.append(r)
    rs.sort()
    return rs[int(0.025 * (len(rs) - 1))], rs[int(0.975 * (len(rs) - 1))]


def cost_verdict(lo: float, hi: float, n_valid: int, n_min: int = 6) -> str:
    """Registered decision of part (a): '◯' if fewer than ``n_min`` valid entries; 'conservative' if the CI of
    actual / model lies below 1; 'optimistic' if above 1; else 'consistent'."""
    if n_valid < n_min:
        return "◯"
    if hi < 1.0:
        return "conservative"
    if lo > 1.0:
        return "optimistic"
    return "consistent"


def mde(sd: float, n: int, k: float = Z_MDE) -> float:
    return k * sd / math.sqrt(n)


def power(sd: float, n: int, effect: float, z_alpha: float = 1.959964) -> float:
    """Two-sided test power at a true mean ``effect`` (normal approximation, the far tail ignored); ``z_alpha`` is the
    two-sided critical value (default alpha = 0.05; 2.2414 for alpha = 0.025)."""
    return 0.5 * math.erfc(-(effect / (sd / math.sqrt(n)) - z_alpha) / math.sqrt(2.0))


def weeks_for_mde(sd: float, effect: float, k: float = Z_MDE) -> int:
    """Smallest n with MDE(sd, n) <= effect."""
    return math.ceil(round((k * sd / effect) ** 2, 9))
