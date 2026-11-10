"""Smart-money persistence (IDEA-20261015-smart-money-persistence): pure helpers, no I/O.

Account classes from turnover and open perp positions, window PnL from cumulative histories, cross-sectional
persistence metrics (rank IC, top-quintile median spread), power (MDE, periods needed), replay of the
`trader_positions` change log, net position flow per market. Units: USD for PnL / notional, decimals for ROI
(0.10 = 10 %), unix milliseconds for history points unless stated. Time is always a parameter.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from .funding_crowding import spearman

# Pre-registered class thresholds (docs/ideas/IDEA-20261015-smart-money-persistence.md, "Попередня реєстрація").
SYSTEM_MIN_VALUE = 1e9
MM_MIN_TURNOVER = 100.0
MM_MAX_NET_SHARE = 0.25
HEDGER_MAX_TURNOVER = 10.0
HEDGER_MAX_NET_SHARE = -0.8
HEDGER_MAX_LEV = 3.0
MIN_LEV = 0.1
EXCLUDED_CLASSES = frozenset({"system", "mm"})

# Two-sided 97.5 % Student-t quantiles by degrees of freedom (1..30); normal beyond.
_T975 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip
Z80 = 2.8  # z_{0.975} + z_{0.80}: MDE multiplier at 5 % two-sided, 80 % power


def t975(df: int) -> float:
    """Student-t 97.5 % quantile (two-sided 95 % CI); 1.96 for df > 30."""
    if df < 1:
        return float("inf")
    return _T975[df - 1] if df <= 30 else 1.96


# ---------------------------------------------------------------------------------------------------------------------
# Account classes


def exposure(positions: Iterable[Mapping[str, Any]]) -> tuple[float, float]:
    """(gross, net) perp notional in USD from position rows with ``side`` ('long' / 'short') and ``value_usd``."""
    gross = net = 0.0
    for p in positions:
        v = abs(float(p.get("value_usd") or 0.0))
        side = p.get("side")
        if side not in ("long", "short"):
            continue
        gross += v
        net += v if side == "long" else -v
    return gross, net


def classify_account(account_value: float | None, vlm_month: float | None, positions: Sequence[Mapping[str, Any]] | None) -> str:
    """Pre-registered heuristic class of a Hyperliquid account: 'system', 'mm', 'hft_directional', 'hedger',
    'directional', 'idle' or 'unknown' (no account value). ``positions=None`` means positions were not observed."""
    av = account_value or 0.0
    if av <= 0:
        return "unknown"
    if av >= SYSTEM_MIN_VALUE:
        return "system"
    turnover = (vlm_month or 0.0) / av
    if positions is None:
        return "mm" if turnover >= MM_MIN_TURNOVER else "unknown"
    gross, net = exposure(positions)
    lev = gross / av
    share = net / gross if gross > 0 else 0.0
    if turnover >= MM_MIN_TURNOVER:
        return "mm" if (abs(share) <= MM_MAX_NET_SHARE or lev < MIN_LEV) else "hft_directional"
    if lev < MIN_LEV:
        return "idle"
    if turnover < HEDGER_MAX_TURNOVER and share <= HEDGER_MAX_NET_SHARE and lev <= HEDGER_MAX_LEV:
        return "hedger"
    return "directional"


# ---------------------------------------------------------------------------------------------------------------------
# Histories and windows


def finite_num(x: Any) -> float | None:
    """``float(x)`` when it is a finite number, else None: absent, not a number, NaN / ±inf (also the strings "nan" /
    "Infinity") or an integer beyond the float range (``float(10**400)`` raises OverflowError)."""
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


MAX_TS_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z in unix ms: a later point / fill time is a corrupt row
MAX_USD = 1e15  # |USD| of a history point, closedPnl, fee or vlm; beyond it is a corrupt row (two 1e308 sum to inf)


def bounded_num(x: Any, lo: float, hi: float) -> float | None:
    """``finite_num(x)`` when it lies in [lo, hi], else None (1e308 is finite but no Hyperliquid time or USD amount)."""
    v = finite_num(x)
    return v if v is not None and lo <= v <= hi else None


def dropped_note(source: str, dropped: Sequence[str]) -> str | None:
    """One warning line naming the fields of the points dropped from ``source`` (e.g. ``"portfolio 0xabc"``), or None."""
    if not dropped:
        return None
    by_field = ", ".join(f"{f} ×{n}" for f, n in sorted(Counter(dropped).items()))
    return f"{source}: {len(dropped)} point(s) with a field that is not a finite number dropped ({by_field})"


def parse_portfolio(raw: Any, dropped: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Hyperliquid info ``portfolio`` -> {window: {'pnl': [(ms, usd)], 'value': [(ms, usd)], 'vlm': usd}}, points sorted.

    A point whose time or value is absent, not a number, NaN / ±inf or a 300+-digit integer is skipped, as is a time
    outside 0 .. MAX_TS_MS or a value beyond ±MAX_USD (1e308); a present ``vlm`` that is not a finite number in
    0 .. MAX_USD reads as 0 (an absent one too, silently, as before). Each one is appended to ``dropped`` (when given) as ``"<window>.<key>.time"`` / ``".value"`` (or
    ``"<window>.<key>"`` for a point that is not a [time, value] pair; ``"<window>.vlm"``)."""
    out: dict[str, dict[str, Any]] = {}
    bad = dropped if dropped is not None else []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict)):
            continue
        name, body = item

        def pts(key: str, body: dict[str, Any] = body, name: Any = name) -> list[tuple[int, float]]:
            rows = []
            for p in body.get(key) or []:
                if not (isinstance(p, (list, tuple)) and len(p) >= 2):
                    bad.append(f"{name}.{key}")
                    continue
                t, v = bounded_num(p[0], 0, MAX_TS_MS), bounded_num(p[1], -MAX_USD, MAX_USD)
                if t is None or v is None:
                    bad.append(f"{name}.{key}.{'time' if t is None else 'value'}")
                    continue
                rows.append((int(t), v))
            return sorted(rows)

        vlm = bounded_num(body.get("vlm") or 0.0, 0, MAX_USD)  # absent / empty reads as 0, as before
        if vlm is None:
            bad.append(f"{name}.vlm")
        out[str(name)] = {"pnl": pts("pnlHistory"), "value": pts("accountValueHistory"), "vlm": vlm or 0.0}
    return out


def interp(history: Sequence[tuple[int, float]], t: int) -> float | None:
    """Linear interpolation of a sorted (time, value) series at ``t``; None outside the covered span."""
    if not history or t < history[0][0] or t > history[-1][0]:
        return None
    lo, hi = 0, len(history) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if history[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    (t0, v0), (t1, v1) = history[lo], history[hi]
    if t <= t0 or t1 == t0:
        return v0 if t <= t0 else v1
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def window_change(history: Sequence[tuple[int, float]], start: int, end: int) -> float | None:
    """Change of a cumulative series over [start, end] by linear interpolation; None if not covered.

    Look-ahead warning: at an edge inside a sparse segment the interpolated value uses the point *after* the edge, so a
    formation window ending at ``end`` sees data after ``end``. Use :func:`step_change` for point-in-time signals."""
    a, b = interp(history, start), interp(history, end)
    return None if a is None or b is None else b - a


def step_at(history: Sequence[tuple[int, float]], t: int) -> float | None:
    """Point-in-time value of a sorted (time, value) series: the last point with time <= ``t``; None before the first
    point or after the last one (not covered). Never uses data after ``t``."""
    if not history or t < history[0][0] or t > history[-1][0]:
        return None
    lo, hi = 0, len(history) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if history[mid][0] <= t:
            lo = mid
        else:
            hi = mid - 1
    return history[lo][1]


def step_change(history: Sequence[tuple[int, float]], start: int, end: int) -> float | None:
    """Point-in-time change of a cumulative series over [start, end]: ``step_at(end) − step_at(start)``. Adjacent
    windows share the edge point, so formation and evaluation never overlap; the effective window is shifted back to
    the last observed points (up to one sampling step)."""
    a, b = step_at(history, start), step_at(history, end)
    return None if a is None or b is None else b - a


def sd_ci95(sd: float, n: int) -> tuple[float, float]:
    """95 % CI of a standard deviation estimated from ``n`` values (chi-square, df = n − 1, Wilson-Hilferty quantiles)."""
    df = n - 1
    if df < 1 or not math.isfinite(sd):
        return float("nan"), float("nan")

    def chi2(z: float) -> float:
        a = 2.0 / (9.0 * df)
        return df * max(1.0 - a + z * math.sqrt(a), 1e-9) ** 3

    return sd * math.sqrt(df / chi2(1.959964)), sd * math.sqrt(df / chi2(-1.959964))


def window_edges(end: int, length: int, count: int) -> list[int]:
    """``count + 1`` ascending edges of back-to-back windows of ``length`` ending at ``end``."""
    return [end - length * k for k in range(count, -1, -1)]


def realized_pnl_windows(
    fills: Iterable[Mapping[str, Any]],
    edges: Sequence[int],
    coverage_start: int,
    coverage_end: int | None = None,
    dropped: list[str] | None = None,
) -> list[float | None]:
    """Realized PnL ``Σ(closedPnl − fee)`` (USD) of Hyperliquid fills in each window [edges[i], edges[i+1]); None for a
    window starting before ``coverage_start`` or ending after ``coverage_end`` (the span the fill history is complete
    over; ``coverage_end=None`` means complete up to now, e.g. not truncated).

    A fill that is not a mapping, or whose ``time`` is absent / not a finite number / a 300+-digit integer / outside
    0 .. MAX_TS_MS, or whose present ``closedPnl`` / ``fee`` is not a finite number within ±MAX_USD (NaN / ±inf /
    1e308 / 300+ digits / text), is skipped and its field
    appended to ``dropped`` (when given): ``"fill"``, ``"time"``, ``"closedPnl"``, ``"fee"``. An absent or empty
    ``closedPnl`` / ``fee`` counts as 0, as before."""
    sums = [0.0] * (len(edges) - 1)
    bad = dropped if dropped is not None else []
    for f in fills:
        if not isinstance(f, Mapping):
            bad.append("fill")
            continue
        tf = bounded_num(f.get("time"), 0, MAX_TS_MS)
        pnl = bounded_num(f.get("closedPnl") or 0.0, -MAX_USD, MAX_USD)
        fee = bounded_num(f.get("fee") or 0.0, -MAX_USD, MAX_USD)
        if tf is None or pnl is None or fee is None:
            bad.append("time" if tf is None else "closedPnl" if pnl is None else "fee")
            continue
        t, v = int(tf), pnl - fee
        for i in range(len(sums)):
            if edges[i] <= t < edges[i + 1]:
                sums[i] += v
                break
    return [
        None if edges[i] < coverage_start or (coverage_end is not None and edges[i + 1] > coverage_end) else sums[i]
        for i in range(len(sums))
    ]


# ---------------------------------------------------------------------------------------------------------------------
# Persistence metrics and power


def period_metrics(signal: Sequence[float], outcome: Sequence[float], top_frac: float = 0.2) -> dict[str, float | int | None]:
    """One formation -> evaluation period: n, Spearman IC(signal, outcome), and D = median outcome of the top
    ``top_frac`` by signal minus the median of the rest. None metrics when n < 10."""
    n = len(signal)
    if n != len(outcome):
        raise ValueError("signal and outcome differ in length")
    if n < 10:
        return {"n": n, "ic": None, "d": None, "top_median": None, "rest_median": None}
    order = sorted(range(n), key=lambda i: signal[i], reverse=True)
    k = max(1, int(round(n * top_frac)))
    top = [outcome[i] for i in order[:k]]
    rest = [outcome[i] for i in order[k:]]
    tm, rm = statistics.median(top), statistics.median(rest)
    return {"n": n, "ic": spearman(signal, outcome), "d": tm - rm, "top_median": tm, "rest_median": rm}


def mean_t_ci(values: Sequence[float]) -> dict[str, float | int | None]:
    """Mean, SD and 95 % Student-t CI across independent periods (cluster = period)."""
    v = [x for x in values if x is not None and math.isfinite(x)]
    n = len(v)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "lo": None, "hi": None}
    m = statistics.fmean(v)
    if n < 2:
        return {"n": 1, "mean": m, "sd": None, "lo": None, "hi": None}
    sd = statistics.stdev(v)
    h = t975(n - 1) * sd / math.sqrt(n)
    return {"n": n, "mean": m, "sd": sd, "lo": m - h, "hi": m + h}


def mde(sd: float, n: int, k: float = Z80) -> float:
    """Minimum detectable mean (5 % two-sided, 80 % power) from ``n`` independent periods with SD ``sd``."""
    return k * sd / math.sqrt(n) if n > 0 else float("inf")


def periods_needed(sd: float, effect: float, k: float = Z80) -> int:
    """Independent periods needed so that the MDE is at most ``effect``."""
    if effect <= 0:
        return 10**9
    return max(2, math.ceil((k * sd / effect) ** 2))


def ic_null_sd(n_names: int) -> float:
    """SD of a cross-sectional rank IC over ``n_names`` under the null (no common shocks): 1/sqrt(n - 1)."""
    return 1.0 / math.sqrt(n_names - 1) if n_names > 1 else float("inf")


# ---------------------------------------------------------------------------------------------------------------------
# trader_positions change log


def detect_delta_start(run_ts: Sequence[int], closed_ts: Iterable[int]) -> int | None:
    """Schema v12 cutover from the log itself: the run before the first run that contains a 'closed' row (the first
    v12 run is a baseline and cannot contain closes). None if no 'closed' row exists yet."""
    runs = sorted(set(run_ts))
    first_closed = min(closed_ts, default=None)
    if first_closed is None:
        return None
    before = [t for t in runs if t < first_closed]
    return before[-1] if before else first_closed


def replay_positions(
    rows: Iterable[Mapping[str, Any]], delta_start: int | None, until: int | None = None
) -> dict[str, dict[str, dict[str, Any]]]:
    """Rebuild every account's open positions from the `trader_positions` log up to ``until`` (inclusive).

    Before ``delta_start`` (schema < 12) each (account, ts) group is the full set ('flat' = empty). From
    ``delta_start`` on, an account's first group is a full baseline and later groups are changes: a row replaces the
    position of its market, side 'closed' removes it. Returns {account: {market: row}}."""
    groups: dict[tuple[int, str], list[Mapping[str, Any]]] = {}
    for r in rows:
        ts = int(r["ts"])
        if until is not None and ts > until:
            continue
        groups.setdefault((ts, str(r["account"])), []).append(r)
    state: dict[str, dict[str, dict[str, Any]]] = {}
    baselined: set[str] = set()
    for ts, account in sorted(groups):
        g = groups[(ts, account)]
        full = delta_start is None or ts < delta_start or account not in baselined
        if delta_start is not None and ts >= delta_start:
            baselined.add(account)
        cur = {} if full else dict(state.get(account, {}))
        for r in g:
            side = str(r.get("side") or "")
            market = str(r.get("market") or "")
            if side == "flat" or not market:
                continue
            if side == "closed":
                cur.pop(market, None)
            else:
                cur[market] = dict(r)
        state[account] = cur
    return state


def signed_notional(p: Mapping[str, Any]) -> float:
    """Signed USD notional of a Hyperliquid position row (long +, short −)."""
    v = abs(float(p.get("value_usd") or 0.0))
    return v if p.get("side") == "long" else -v if p.get("side") == "short" else 0.0


def mark_price(p: Mapping[str, Any]) -> float | None:
    size = float(p.get("size") or 0.0)
    return abs(float(p.get("value_usd") or 0.0)) / size if size else None


def net_flow(
    before: Mapping[str, Mapping[str, Mapping[str, Any]]],
    after: Mapping[str, Mapping[str, Mapping[str, Any]]],
    accounts: Iterable[str],
) -> dict[str, float]:
    """Per market: Σ over ``accounts`` of the change in signed size valued at the later mark (USD), so that pure price
    moves do not count as flow. A closed position is valued at the mark of its last observation. Accounts missing from
    ``before`` are skipped (no baseline: their whole book is not a change)."""
    flow: dict[str, float] = {}
    for a in accounts:
        if a not in before:
            continue
        b, f = before[a], after.get(a, {})
        for m in set(b) | set(f):
            pb, pa = b.get(m), f.get(m)
            sb = (float(pb.get("size") or 0.0) * (1 if pb.get("side") == "long" else -1)) if pb else 0.0
            sa = (float(pa.get("size") or 0.0) * (1 if pa.get("side") == "long" else -1)) if pa else 0.0
            px = mark_price(pa) if pa else (mark_price(pb) if pb else None)
            if px is None or sa == sb:
                continue
            flow[m] = flow.get(m, 0.0) + (sa - sb) * px
    return flow


def position_changes(
    before: Mapping[str, Mapping[str, Mapping[str, Any]]], after: Mapping[str, Mapping[str, Mapping[str, Any]]], accounts: Iterable[str]
) -> list[dict[str, Any]]:
    """Positions opened, closed, flipped, increased or reduced between two replayed states, per (account, market):
    ``kind``, ``side`` (after; before for a close), ``size_before``, ``size_after``, ``value_usd`` (after; before for a
    close), ``delta_usd`` (traded size valued at that mark; a flip counts both legs) and ``title``. Accounts missing from ``before`` are skipped (no baseline to compare with)."""
    out: list[dict[str, Any]] = []
    for a in accounts:
        if a not in before:
            continue
        b, f = before[a], after.get(a, {})
        for m in sorted(set(b) | set(f)):
            pb, pa = b.get(m), f.get(m)
            sb, sa = float(pb.get("size") or 0.0) if pb else 0.0, float(pa.get("size") or 0.0) if pa else 0.0
            if pb and pa and pb.get("side") == pa.get("side"):
                if math.isclose(sa, sb, rel_tol=1e-4):
                    continue
                kind = "increased" if sa > sb else "reduced"
            elif pb and pa:
                kind = "flipped"
            else:
                kind = "opened" if pa else "closed"
            ref = pa or pb or {}
            px = mark_price(ref) or 0.0
            moved = sa + sb if kind == "flipped" else abs(sa - sb)
            out.append(
                {
                    "account": a,
                    "market": m,
                    "kind": kind,
                    "side": ref.get("side"),
                    "size_before": sb,
                    "size_after": sa,
                    "value_usd": abs(float(ref.get("value_usd") or 0.0)),
                    "delta_usd": moved * px,
                    "title": ref.get("title"),
                }
            )
    return out


def weekly_points(by_day: Mapping[str, float | None], latest: str, max_weeks: int = 52) -> list[float]:
    """Non-overlapping weekly values (e.g. ``pnl_week`` of daily snapshots) at ``latest``, ``latest`` − 7 days, …;
    stops at the first missing week."""
    d = date.fromisoformat(latest)
    out: list[float] = []
    for _ in range(max_weeks):
        v = by_day.get(d.isoformat())
        if v is None:
            break
        out.append(float(v))
        d -= timedelta(days=7)
    return out


def net_book(state: Mapping[str, Mapping[str, Mapping[str, Any]]], accounts: Iterable[str]) -> dict[str, dict[str, float]]:
    """Per market: long, short and net USD notional and the number of ``accounts`` holding it."""
    out: dict[str, dict[str, float]] = {}
    for a in accounts:
        for m, p in state.get(a, {}).items():
            v = signed_notional(p)
            if v == 0:
                continue
            row = out.setdefault(m, {"long": 0.0, "short": 0.0, "net": 0.0, "holders": 0})
            row["long" if v > 0 else "short"] += abs(v)
            row["net"] += v
            row["holders"] += 1
    return out
