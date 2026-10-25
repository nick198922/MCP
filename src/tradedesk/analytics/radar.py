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

`radar_view` turns archive rows plus daily DVOL, daily spot closes and calendar events into the snapshot's ``radar``
block of one asset (now / a week ago, IV Rank, IV vs RV, skew, smile, forward vol with events, yearly chart series).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .macro_calendar import clean_text
from .put_skew import DAY_MS, HOUR_MS, build_day, iv_at_put_delta, iv_at_strike
from .vrp import realized_vol

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
WEEK_DAYS = 7
MAX_ROW_AGE_DAYS = 2  # «now» is the newest archive row at most this many days before the latest due day
IV_RANK_DAYS = 365
IV_RANK_MIN_DAYS = 330  # fewer daily DVOL closes in the year window -> no IV Rank
YEAR_DAYS = 365  # the yearly chart series (price, DVOL, skew history)
# forward-vol windows are tagged with these calendar kinds (analytics.macro_calendar); speeches, claims, minutes are not
FWD_EVENT_KINDS = frozenset({"fomc", "cpi", "core_pce", "pce", "nfp", "ppi", "gdp", "election"})
# «hot» window, as in the prototype: >= 5 days long, ending within 100 days, forward vol >= median of those + 3 vol points
HOT_MIN_DAYS = 5.0
HOT_HORIZON_DAYS = 100.0
HOT_MARGIN = 0.03


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


# ----------------------------------------------------------------------------- snapshot view (the assistant's «Радар»)


def _iso_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _iso_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_ms(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """An archive row in one shape, from a `radar_row` result (``atm`` dict) or a `Store.radar_list` row (``atm_7d`` ...)."""
    atm = row.get("atm")
    if not isinstance(atm, dict):
        atm = {str(d): row.get(f"atm_{d}d") for d in TENORS_DAYS}
    smile = row.get("smile") if isinstance(row.get("smile"), dict) else {}
    day = str(row["day"])
    _day_ms(day)  # ValueError on a malformed day
    return {
        "day": day,
        "ts": int(row["ts"]),
        "spot": _num(row.get("spot")),
        "atm": {str(d): _num(atm.get(str(d))) for d in TENORS_DAYS},
        "put_skew_25": _num(row.get("put_skew_25")),
        "rr_25": _num(row.get("rr_25")),
        "smile": {label: _num(smile.get(label)) for label, _ in SMILE_POINTS},
        "expiries": list(row.get("expiries") or []),
    }


def pick_rows(
    rows: list[dict[str, Any]], now: datetime, miss: Callable[[str, str], None]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(now row, week row): the newest archive row at most `MAX_ROW_AGE_DAYS` before the latest due day, and the row
    exactly `WEEK_DAYS` earlier. A missing one is None and reported through ``miss(field, reason)``."""
    by_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            n = normalize_row(r)
        except (KeyError, TypeError, ValueError):
            continue
        by_day[n["day"]] = n
    due = due_days(now, 1)[0]
    latest, floor = _iso_day(due), _iso_day(due - MAX_ROW_AGE_DAYS * DAY_MS)
    days = sorted(d for d in by_day if floor <= d <= latest)
    if not days:
        miss("surface", f"no radar archive row from {floor} to {latest} (job radar_archive off or behind)")
        return None, None
    now_row = by_day[days[-1]]
    week_day = _iso_day(_day_ms(now_row["day"]) - WEEK_DAYS * DAY_MS)
    week_row = by_day.get(week_day)
    if week_row is None:
        miss("surface_week", f"no radar archive row for {week_day}")
    return now_row, week_row


def iv_rank(series: list[tuple[int, float]], day_ms: int, window_days: int = IV_RANK_DAYS) -> tuple[dict[str, Any] | None, str | None]:
    """IV Rank and IV percentile of the daily DVOL close of the UTC day ``day_ms`` within the ``window_days`` daily closes
    ending that day (inclusive): rank = (x - min) / (max - min), percentile = share of the window strictly below x.

    ``series``: [(day start ms, DVOL close in PERCENT as Deribit publishes it)]; the result is in decimals. Returns
    (value, None) or (None, reason).
    """
    pts = {int(t) // DAY_MS * DAY_MS: float(v) for t, v in series if (_num(v) or 0.0) > 0}
    x = pts.get(day_ms)
    if x is None:
        return None, f"no DVOL close for {_iso_day(day_ms)}"
    win = [v for t, v in pts.items() if day_ms - window_days * DAY_MS < t <= day_ms]
    if len(win) < IV_RANK_MIN_DAYS:
        return None, f"DVOL history: {len(win)} of {window_days} days"
    lo, hi = min(win), max(win)
    if hi <= lo:
        return None, "DVOL flat over the window"
    return {
        "day": _iso_day(day_ms),
        "dvol": x / 100.0,
        "rank": (x - lo) / (hi - lo),
        "percentile": sum(1 for v in win if v < x) / len(win),
        "min": lo / 100.0,
        "max": hi / 100.0,
        "n_days": len(win),
    }, None


def trailing_rv(closes: list[tuple[int, float]], now: datetime, days: int) -> tuple[float | None, str | None]:
    """Zero-mean close-to-close realized vol (`vrp.realized_vol`) over the last ``days`` daily returns up to ``now``.

    ``closes``: [(bar open ms, close)]. The newest bar may be the current day's live price (as in `deribit_vrp`); a gap
    in the window or a newest bar older than yesterday gives (None, reason).
    """
    now_ms = int(now.timestamp() * 1000)
    pts = {int(t) // DAY_MS * DAY_MS: float(c) for t, c in closes if int(t) <= now_ms and (_num(c) or 0.0) > 0}
    tail = sorted(pts.items())[-(days + 1) :]
    if len(tail) < days + 1:
        return None, f"{len(tail)} daily closes, need {days + 1}"
    if tail[-1][0] < now_ms // DAY_MS * DAY_MS - DAY_MS:
        return None, f"newest daily close {_iso_day(tail[-1][0])} is stale"
    if tail[-1][0] - tail[0][0] != days * DAY_MS:
        return None, "gap in the daily closes"
    return realized_vol([c for _, c in tail]), None


def _events(events: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """Calendar events (`analytics.macro_calendar` shape) of the forward-vol kinds as (ms, {ts_utc, kind, title})."""
    out = []
    for e in events:
        if not isinstance(e, dict):
            continue
        kind = clean_text(e.get("kind"), 24).lower()
        if kind not in FWD_EVENT_KINDS:
            continue
        try:
            ts = datetime.fromisoformat(str(e.get("ts_utc")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            continue
        ms = int(ts.timestamp() * 1000)
        out.append((ms, {"ts_utc": _iso_ts(ms), "kind": kind, "title": clean_text(e.get("title"), 120)}))
    out.sort(key=lambda x: x[0])
    # one event per kind and UTC day: a live calendar lists FOMC as statement + rate + press conference; keep the earliest
    seen: set[tuple[str, str]] = set()
    uniq = []
    for ms, x in out:
        key = (x["kind"], x["ts_utc"][:10])
        if key not in seen:
            seen.add(key)
            uniq.append((ms, x))
    return uniq


def forward_vols(expiries: list[Any], events: list[dict[str, Any]], now_ms: int | None = None) -> dict[str, Any]:
    """Forward vol between neighbouring expiries of an archive row, tagged with the calendar events inside each window.

    ``expiries``: the row's [[expiry unix s, days, forward, ATM IV], ...]. Forward variance between expiries 0 and 1 is
    (v1^2 d1 - v0^2 d0) / (d1 - d0); a non-positive one (calendar arbitrage in the marks) gives ``fwd`` None with a
    ``reason``. An event belongs to the window (expiry 0, expiry 1] by its UTC time. Windows ending at or before
    ``now_ms`` (an archive row up to two days old) are dropped. ``hot``: the window is at least `HOT_MIN_DAYS` long,
    ends within `HOT_HORIZON_DAYS` and its forward vol is >= the median of such windows + `HOT_MARGIN` (the
    prototype's rule). Decimal vols; days from the row's 19:00 UTC reference.
    """
    pts = []
    for e in expiries:
        try:
            exp_s, days, iv = int(e[0]), float(e[1]), _num(e[3])
        except (TypeError, ValueError, IndexError):
            continue
        if iv is not None and iv > 0 and days > 0:
            pts.append((days, exp_s, iv))
    pts.sort()
    evs = _events(events)
    windows: list[dict[str, Any]] = []
    for (d0, e0, v0), (d1, e1, v1) in zip(pts, pts[1:], strict=False):
        if d1 <= d0 or (now_ms is not None and e1 * 1000 <= now_ms):
            continue
        w = (v1 * v1 * d1 - v0 * v0 * d0) / (d1 - d0)
        win: dict[str, Any] = {
            "from_utc": _iso_ts(e0 * 1000),
            "to_utc": _iso_ts(e1 * 1000),
            "days": d1 - d0,
            "to_days": d1,
            "fwd": math.sqrt(w) if w > 0 else None,
            "events": [x for ms, x in evs if e0 * 1000 < ms <= e1 * 1000],
            "hot": False,
        }
        if win["fwd"] is None:
            win["reason"] = "non-positive forward variance (calendar arbitrage in the marks)"
        windows.append(win)
    weekly = [w for w in windows if w["fwd"] is not None and w["days"] >= HOT_MIN_DAYS and w["to_days"] <= HOT_HORIZON_DAYS]
    med = None
    if weekly:
        s = sorted(w["fwd"] for w in weekly)
        n = len(s)
        med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        for w in weekly:
            w["hot"] = w["fwd"] >= med + HOT_MARGIN
    return {"windows": windows, "median_weekly": med}


def _sig(v: float, sig: int) -> float:
    """``v`` rounded to ``sig`` significant digits: keeps the snapshot's yearly series short."""
    return float(f"{v:.{sig}g}")


def daily_grid(cols: dict[str, dict[int, float]], end_day_ms: int, days: int = YEAR_DAYS, sig: int = 4) -> dict[str, Any] | None:
    """Compact daily series for a chart: each column's values on the UTC-day grid of the ``days`` days ending
    ``end_day_ms`` (inclusive), None on days without a point, rounded to ``sig`` significant digits. Days empty in
    every column are trimmed at both ends. Returns {"start": "YYYY-MM-DD", <column>: [...], "n": days with a value}
    (day i of a column is ``start`` + i days), or None when no column has a point in the window.
    """
    first = end_day_ms - (days - 1) * DAY_MS
    have = sorted({t for pts in cols.values() for t in pts if first <= t <= end_day_ms})
    if not have:
        return None
    lo, hi = have[0], have[-1]
    out: dict[str, Any] = {"start": _iso_day(lo)}
    for name, pts in cols.items():
        out[name] = [None if (v := pts.get(t)) is None else _sig(v, sig) for t in range(lo, hi + DAY_MS, DAY_MS)]
    out["n"] = len(have)
    return out


def year_series(
    rows: list[dict[str, Any]],
    dvol: list[tuple[int, float]],
    closes: list[tuple[int, float]],
    now: datetime,
    miss: Callable[[str, str], None],
) -> dict[str, Any]:
    """The radar's yearly chart series (`YEAR_DAYS` UTC days ending on ``now``'s day), each `daily_grid` or None with a
    reason through ``miss``:

    * ``price``: daily spot closes (USD; the bar's UTC day; the newest bar may be the live current-day price);
    * ``dvol``: daily DVOL closes as decimals (Deribit publishes percent);
    * ``skew``: the archive rows' 30-day ``put_skew_25`` and ``rr_25`` (decimal vol differences), rows up to the latest
      due day only.
    Points after ``now`` are ignored; non-positive prices and DVOL are dropped.
    """
    now_ms = int(now.timestamp() * 1000)
    today = now_ms // DAY_MS * DAY_MS
    first = _iso_day(today - (YEAR_DAYS - 1) * DAY_MS)
    px = {int(t) // DAY_MS * DAY_MS: float(c) for t, c in closes if int(t) <= now_ms and (_num(c) or 0.0) > 0}
    price = daily_grid({"values": px}, today, sig=6)
    if price is None:
        miss("year_price", f"no daily close from {first} to {_iso_day(today)}")
    dv = {int(t) // DAY_MS * DAY_MS: float(v) / 100.0 for t, v in dvol if int(t) <= now_ms and (_num(v) or 0.0) > 0}
    dvol_s = daily_grid({"values": dv}, today)
    if dvol_s is None:
        miss("year_dvol", f"no daily DVOL from {first} to {_iso_day(today)}")
    due = due_days(now, 1)[0]
    ps: dict[int, float] = {}
    rr: dict[int, float] = {}
    for r in rows:
        try:
            n = normalize_row(r)
        except (KeyError, TypeError, ValueError):
            continue
        t = _day_ms(n["day"])
        if t > due:
            continue
        if n["put_skew_25"] is not None:
            ps[t] = n["put_skew_25"]
        if n["rr_25"] is not None:
            rr[t] = n["rr_25"]
    skew = daily_grid({"put_skew_25": ps, "rr_25": rr}, due)
    if skew is None:
        miss("year_skew", f"no radar archive skew from {_iso_day(due - (YEAR_DAYS - 1) * DAY_MS)} to {_iso_day(due)} (backfill not run)")
    return {"price": price, "dvol": dvol_s, "skew": skew}


def _sub(a: dict[str, Any] | None, b: dict[str, Any] | None, keys: Any) -> dict[str, float | None] | None:
    if a is None or b is None:
        return None
    return {k: _diff(a.get(k), b.get(k)) for k in keys}


def radar_view(
    asset: str,
    rows: list[dict[str, Any]],
    dvol: list[tuple[int, float]],
    closes: list[tuple[int, float]],
    events: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """The «Радар» block of one asset for the assistant snapshot (pure; ``now`` is the snapshot time).

    Inputs: radar archive rows (`radar_row` or `Store.radar_list` shape), daily DVOL [(day ms, percent)], daily spot
    closes [(open ms, USD)], calendar events (`macro_calendar` shape). Every block is computed independently: a field
    that cannot be computed is None and its reason is in ``missing`` ([{field, reason}]); this never raises on gaps.

    * ``price``: Deribit index at 19:00 UTC of the newest archive row and of the row a week earlier, ``change`` a fraction;
    * ``atm``: constant-tenor ATM IV {"7","30","90","180"} now / week / change (decimal vol points);
    * ``iv_rank``: `iv_rank` on the newest daily DVOL close at or before ``now`` and on the close 7 days earlier;
    * ``iv_vs_rv``: 30-day ATM IV of the newest row against 30- and 7-day realized vol of the closes (``premium_*`` =
      IV - RV);
    * ``skew``: 30-day put skew 25-delta and RR 25-delta now / week / change; ``smile``: the 30-day smile now / week;
    * ``forward_vol``: `forward_vols` on the newest row's term structure, without the windows that ended by ``now``;
    * ``year``: `year_series` for the charts (price, DVOL, skew history over the year), compact daily grids.
    """
    missing: list[dict[str, str]] = []

    def miss(field: str, reason: str) -> None:
        missing.append({"field": field, "reason": reason})

    now_row, week_row = pick_rows(rows, now, miss)
    out: dict[str, Any] = {
        "asset": asset,
        "surface": {
            "now_day": now_row["day"] if now_row else None,
            "week_day": week_row["day"] if week_row else None,
            "ref_utc": _iso_ts(now_row["ts"] * 1000) if now_row else None,
            "method": METHOD,
        },
    }
    p_now = now_row["spot"] if now_row else None
    p_week = week_row["spot"] if week_row else None
    out["price"] = {"now": p_now, "week": p_week, "change": p_now / p_week - 1.0 if p_now and p_week else None}
    tenors = [str(d) for d in TENORS_DAYS]
    a_now, a_week = (now_row["atm"] if now_row else None), (week_row["atm"] if week_row else None)
    out["atm"] = {"now": a_now, "week": a_week, "change": _sub(a_now, a_week, tenors)}

    pts = sorted((int(t), float(v)) for t, v in dvol if _num(v) is not None)
    now_ms = int(now.timestamp() * 1000)
    upto = [t // DAY_MS * DAY_MS for t, _ in pts if t <= now_ms]
    rank_now = rank_week = None
    if not upto:
        miss("iv_rank", "no daily DVOL")
    else:
        last = upto[-1]
        rank_now, why = iv_rank(pts, last)
        if why:
            miss("iv_rank", why)
        rank_week, why = iv_rank(pts, last - WEEK_DAYS * DAY_MS)
        if why:
            miss("iv_rank_week", why)
    out["iv_rank"] = {
        "now": rank_now,
        "week": rank_week,
        "change": _diff(rank_now["rank"], rank_week["rank"]) if rank_now and rank_week else None,
    }

    rv30, why30 = trailing_rv(closes, now, 30)
    rv7, why7 = trailing_rv(closes, now, 7)
    if why30:
        miss("rv30", why30)
    if why7:
        miss("rv7", why7)
    iv30 = a_now["30"] if a_now else None
    out["iv_vs_rv"] = {"iv30": iv30, "rv30": rv30, "rv7": rv7, "premium_rv30": _diff(iv30, rv30), "premium_rv7": _diff(iv30, rv7)}

    s_now = {"put_skew_25": now_row["put_skew_25"], "rr_25": now_row["rr_25"]} if now_row else None
    s_week = {"put_skew_25": week_row["put_skew_25"], "rr_25": week_row["rr_25"]} if week_row else None
    out["skew"] = {"now": s_now, "week": s_week, "change": _sub(s_now, s_week, ("put_skew_25", "rr_25"))}
    out["smile"] = {"now": now_row["smile"] if now_row else None, "week": week_row["smile"] if week_row else None}

    out["forward_vol"] = forward_vols(now_row["expiries"], events, now_ms) if now_row else {"windows": [], "median_weekly": None}
    if now_row and not out["forward_vol"]["windows"]:
        miss("forward_vol", "fewer than two expiries with an ATM IV, or every window has ended")
    out["year"] = year_series(rows, dvol, closes, now, miss)
    out["missing"] = missing
    return out
