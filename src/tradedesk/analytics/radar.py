"""Options radar: the daily Deribit surface row behind the assistant's «Радар» (docs/ideas/IDEA-20261018-assistant.md).

Pure functions, no I/O; time is a parameter (epoch ms, UTC). The surface is `put_skew.build_day` on Deribit option
trade prints of the 6 hours before 19:00 UTC (history.deribit.com), so the live archive and the one-year backfill use
the same method. Units and conventions (same as `put_skew`):
  * vols are decimals (0.45 = 45 %); skew and risk reversal are vol differences in decimals (0.027 = 2.7 vol points);
  * T in years = days / 365 from the reference time to the 08:00 UTC expiry; spot = Deribit index, USD;
  * ATM = mark IV at K = F of the expiry (linear in ln K/F between fresh strikes, flat beyond);
  * constant tenor (7 / 30 / 90 / 180 days): total variance sigma^2 T linear in T between the two expiries around the
    tenor, no extrapolation (None when the tenor is not bracketed);
  * deltas are Black-76 forward deltas at each strike's own mark IV, NOT Deribit's premium-adjusted deltas; a
    25-delta call is put delta -0.75. The skew levels may therefore differ from other sources (LIB-20261018: the
    @DataHawks put skew does not match ours); compare changes over time, not levels.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .put_skew import DAY_MS, HOUR_MS, build_day, iv_at_put_delta, iv_at_strike

REF_HOUR_UTC = 19
WINDOW_HOURS = 6
METHOD = "deribit-trades-6h-19utc-v1"
TENORS_DAYS = (7, 30, 90, 180)
SKEW_TENOR_DAYS = 30
# smile points at the skew tenor: label -> put delta (None = ATM at the forward)
SMILE_POINTS: tuple[tuple[str, float | None], ...] = (
    ("10P", -0.10),
    ("25P", -0.25),
    ("35P", -0.35),
    ("ATM", None),
    ("35C", -0.65),
    ("25C", -0.75),
    ("10C", -0.90),
)


def ref_ms(day_ms: int) -> int:
    """Reference time of a UTC day: 19:00 UTC."""
    return day_ms + REF_HOUR_UTC * HOUR_MS


def window_ms(day_ms: int) -> tuple[int, int]:
    """Trade window [19:00 - 6 h, 19:00] UTC whose prints build the day's surface."""
    t = ref_ms(day_ms)
    return t - WINDOW_HOURS * HOUR_MS, t


def build_surface(trades: list[dict[str, Any]], day_ms: int, product: str) -> dict[str, Any] | None:
    """`put_skew.build_day` at 19:00 UTC with marks no older than the 6 h window (no prints after 19:00 are used)."""
    return build_day(trades, day_ms, ref_hour=float(REF_HOUR_UTC), max_mark_age_h=float(WINDOW_HOURS), product=product)


def atm_iv(exp: dict[str, Any]) -> float | None:
    return iv_at_strike(exp, exp["F"])[0]


def constant_tenor_iv(snap: dict[str, Any], target_days: float, iv_of: Callable[[dict[str, Any]], float | None]) -> float | None:
    """IV at a constant tenor: ``iv_of(expiry)`` per expiry, total variance linear in T between the expiries around it.

    Expiries where ``iv_of`` is None are skipped; None when the tenor is not bracketed or the variance is not positive.
    """
    pts = []
    for x in snap["expiries"].values():
        v = iv_of(x)
        if v is not None and x["T"] > 0:
            pts.append((x["T"], v))
    pts.sort()
    tt = target_days / 365.0
    lo = [p for p in pts if p[0] <= tt]
    hi = [p for p in pts if p[0] >= tt]
    if not lo or not hi:
        return None
    (t0, v0), (t1, v1) = lo[-1], hi[0]
    if t1 == t0:
        return v0
    w = v0 * v0 * t0 + (v1 * v1 * t1 - v0 * v0 * t0) * (tt - t0) / (t1 - t0)
    return math.sqrt(w / tt) if w > 0 else None


def smile_at(snap: dict[str, Any], target_days: float = SKEW_TENOR_DAYS) -> dict[str, float | None]:
    """IV at the smile points (10/25/35-delta puts, ATM, 35/25/10-delta calls) at a constant tenor."""
    out: dict[str, float | None] = {}
    for label, pd in SMILE_POINTS:
        if pd is None:
            out[label] = constant_tenor_iv(snap, target_days, atm_iv)
        else:
            out[label] = constant_tenor_iv(snap, target_days, lambda x, pd=pd: iv_at_put_delta(x, pd))
    return out


def _diff(a: float | None, b: float | None) -> float | None:
    return a - b if a is not None and b is not None else None


def radar_row(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    """The archive row of one day and asset from a `build_surface` result (None when there is no surface).

    ``atm``: {tenor days: IV}; ``put_skew_25`` = IV(25-delta put) - ATM and ``rr_25`` = IV(25-delta call) -
    IV(25-delta put), both at 30 days; ``smile``: the 30-day smile points; ``expiries``: [[expiry unix s, days,
    forward USD, ATM IV], ...] sorted by expiry (the term structure the constant tenors come from).
    """
    if snap is None or not snap.get("expiries"):
        return None
    smile = smile_at(snap)
    expiries = []
    n_prints = 0
    for e in sorted(snap["expiries"]):
        x = snap["expiries"][e]
        n_prints += int(x.get("n_prints", 0))
        expiries.append([int(e) // 1000, x["T"] * 365.0, x["F"], atm_iv(x)])
    day_ms = int(snap["day"])
    return {
        "day": datetime.fromtimestamp(day_ms / 1000, UTC).strftime("%Y-%m-%d"),
        "ts": int(snap["t_ms"]) // 1000,
        "spot": float(snap["spot"]),
        "atm": {str(d): constant_tenor_iv(snap, d, atm_iv) for d in TENORS_DAYS},
        "put_skew_25": _diff(smile["25P"], smile["ATM"]),
        "rr_25": _diff(smile["25C"], smile["25P"]),
        "smile": smile,
        "expiries": expiries,
        "n_prints": n_prints,
        "method": METHOD,
    }


LAG_MINUTES = 60.0


def due_days(now: datetime, lookback_days: int, lag_minutes: float = LAG_MINUTES) -> list[int]:
    """UTC day starts (epoch ms) of the last ``lookback_days`` days whose 19:00 UTC reference has passed by ``lag_minutes``.

    Newest first. The lag (default 60 min, i.e. from 20:00 UTC) lets history.deribit.com ingest the prints just before
    19:00: a stored row is never rewritten (INSERT OR IGNORE on (day, asset)), so an archive that lags at write time
    would freeze an incomplete window. Nothing is lost by waiting: the row is fixed at 19:00 whenever it is computed.
    """
    now_ms = int(now.timestamp() * 1000)
    today = now_ms // DAY_MS * DAY_MS
    last = today if now_ms >= ref_ms(today) + int(lag_minutes * 60_000) else today - DAY_MS
    return [last - i * DAY_MS for i in range(max(lookback_days, 0))]
