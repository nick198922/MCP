"""Smart-money forward slices (IDEA-20261015-smart-money-persistence, H1 / H1w / H2 / H3): pure helpers, no I/O.

Rules are fixed in the card: "Попередня реєстрація" (2026-10-17) and "Зріз 1 форварду — уточнення виконання"
(2026-11-25, before any outcome was computed). Units: USD for PnL / notional, decimals for ROI and returns (0.10 = 10 %),
unix seconds for collector timestamps, ``date`` for UTC days. Time (the as-of cut-off) is always a parameter.
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from .funding_crowding import newey_west_se, spearman
from .smart_money import EXCLUDED_CLASSES, classify_account, interp, period_metrics, t975

MIN_VALUE = 500_000.0  # H1 universe floor, USD account value on the formation day
TOP_FRAC = 0.2  # top quintile
TOP_DROP_FRAC = 0.01  # robustness: without the top 1 % of accounts by |formation PnL|
H2_MIN_COINS = 10  # a day with fewer coins with a non-zero signal is skipped
H2_COST_PER_LEG = 0.003  # round trip per leg (taker 0.045 % x 2 + slippage), registered 2026-10-17
H2_IC_MIN, H2_SPREAD_MIN = 0.03, 0.01  # registered H2 thresholds (h = 7): mean IC, net weekly spread
H3_FEE = 0.01  # follower's cost in probability points
H3_P_MIN, H3_P_MAX = 0.03, 0.97
H3_EDGE_MIN = 0.03  # registered H3 threshold, probability points
H3_MIN_MARKETS = 30
# Slice-1 fix round (2026-11-26, before any outcome was seen; card section "Зріз 1 форварду"):
H3_FRESH_TOL = 0.03  # fresh-entry guard: |p_obs - entry_px| <= this (bought just now, not grown in price)
H3_UMA_GRACE_S = 7 * 86400  # main sample: scheduled endDate <= as-of - 7 days (time for UMA resolution)
H3_UNRESOLVED_MAX = 0.05  # > 5 % unresolved past-endDate markets / positions: no H3 decision on the slice
H3_GAMMA_MISSING_MAX = 0.02  # > 2 % tokens / positions without a Gamma market (or endDate): stop before H3
H3_TOP_WALLETS_DROP = 5  # concentration guard: the result must hold without the 5 wallets with most positions
H3_MAX_WALLET_SHARE = 0.25  # concentration guard: no wallet with >= 25 % of the main-sample positions
DECISIVE_LOOKS = 3  # named decisive looks: slices 1-3 (as-of 2026-11-25, 2026-12-16, 2027-01-15)
DECISION_LEVEL = 1.0 - 0.05 / (2 * DECISIVE_LOOKS)  # two-sided CI level: 2 rankings (H3) / 2 routes (H2) x 3 looks
# Review round 2 (R1, 2026-11-26, before any outcome was seen): the named decisive looks as unix s at 00:00 UTC.
# Any other as-of is descriptive only (no H3 'candidate' / 'confirmed', no H2 decision); replication pairs are
# neighbours in this tuple: (1 -> 2), (2 -> 3).
DECISIVE_AS_OF = tuple((date(y, m, d) - date(1970, 1, 1)).days * 86400 for y, m, d in ((2026, 11, 25), (2026, 12, 16), (2027, 1, 15)))
EARLY_CLOSE_S = 86400  # m7 (descriptive): closedTime < endDate - 1 day
Z95 = 1.96
Z80 = 2.8


def z_two_sided(level: float) -> float:
    """Normal quantile for a two-sided confidence ``level`` (0.95 -> 1.96)."""
    return statistics.NormalDist().inv_cdf(0.5 + level / 2.0)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction of the regularized incomplete beta (modified Lentz)."""
    tiny, qab, qap, qam = 1e-300, a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 500):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbt) * _betacf(b, a, 1.0 - x) / b


def t_cdf(x: float, df: int) -> float:
    """Student-t CDF via the regularized incomplete beta."""
    tail = 0.5 * _betainc(df / 2.0, 0.5, df / (df + x * x))
    return 1.0 - tail if x >= 0 else tail


def t_quantile(p: float, df: int) -> float:
    """Student-t quantile for p in (0.5, 1) by bisection on :func:`t_cdf`."""
    lo, hi = 0.0, 1.0
    while t_cdf(hi, df) < p:
        hi *= 2.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------------------------------------------------
# Periods


def complete_periods(start: date, length_days: int, as_of_day: date) -> list[tuple[date, date]]:
    """Non-overlapping periods (d, d + length) with d = start + k·length whose end-day snapshot lies strictly before the
    as-of day (the end day's daily snapshot is taken during that day, so it is complete only once the day is over)."""
    out: list[tuple[date, date]] = []
    d = start
    while d + timedelta(days=length_days) < as_of_day:
        out.append((d, d + timedelta(days=length_days)))
        d += timedelta(days=length_days)
    return out


def matured_days(first: date, last: date, h: int, as_of_day: date) -> list[date]:
    """Signal days d in [first, last] whose outcome (UTC daily close of d to close of d + h; the close of day x is at
    x + 1 00:00) is known before ``as_of_day`` 00:00, i.e. d + h + 1 <= as_of_day."""
    out: list[date] = []
    d = first
    while d <= last and d + timedelta(days=h + 1) <= as_of_day:
        out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# H1 / H1w


def h1_class(row: Mapping[str, Any], positions: Sequence[Mapping[str, Any]] | None) -> str:
    """Registered class at the formation day from the snapshot row (account value, month volume) and the positions
    observed at that day (None: not observed; then turnover >= 100 reads as 'mm', as in stage 0)."""
    return classify_account(row.get("account_value"), row.get("vlm_month"), positions)


def h1_universe(
    snap: Mapping[str, Mapping[str, Any]], classes: Mapping[str, str], signal_field: str, min_value: float = MIN_VALUE
) -> list[str]:
    """Accounts with account_value >= ``min_value`` on the formation day, a finite signal, and a class outside
    system / mm (sorted for determinism)."""
    out = []
    for a, r in snap.items():
        av, sig = r.get("account_value"), r.get(signal_field)
        if av is None or av < min_value or sig is None or not math.isfinite(sig):
            continue
        if classes.get(a, "unknown") in EXCLUDED_CLASSES:
            continue
        out.append(a)
    return sorted(out)


def leaderboard_zero_quality(snap: Mapping[str, Mapping[str, Any]], min_value: float = MIN_VALUE) -> dict[str, int]:
    """Data-quality row of one Hyperliquid leaderboard snapshot (descriptive only: filters nothing, enters no rule).

    The week window lies inside the month window and the day window inside the week, so a zero PnL on the longer
    window with a non-zero PnL on the shorter one is impossible for a real account (review 2026-11-26). Counted over
    accounts with ``account_value >= min_value``; a missing (None) or non-finite value is neither zero nor non-zero."""

    def num(x: Any) -> float | None:
        return x if isinstance(x, int | float) and math.isfinite(x) else None

    n = month0 = week0 = month0_week_nz = week0_day_nz = 0
    for r in snap.values():
        av = num(r.get("account_value"))
        if av is None or av < min_value:
            continue
        n += 1
        m, w, dd = num(r.get("pnl_month")), num(r.get("pnl_week")), num(r.get("pnl_day"))
        month0 += m == 0
        week0 += w == 0
        month0_week_nz += m == 0 and w is not None and w != 0
        week0_day_nz += w == 0 and dd is not None and dd != 0
    return {"n": n, "month0": month0, "week0": week0, "month0_week_nz": month0_week_nz, "week0_day_nz": week0_day_nz}


def portfolio_change(pf: Mapping[str, Mapping[str, Any]], start_ms: int, end_ms: int) -> float | None:
    """PnL change (USD) over [start_ms, end_ms] from a parsed ``portfolio`` (outcome window only, both edges in the
    past): the finest window ('week', 'month', 'allTime') whose pnl history covers both edges, linear interpolation."""
    for w in ("week", "month", "allTime"):
        hist = (pf.get(w) or {}).get("pnl") or []
        a, b = interp(hist, start_ms), interp(hist, end_ms)
        if a is not None and b is not None:
            return b - a
    return None


def h1_outcomes(
    universe: Sequence[str],
    snap_start: Mapping[str, Mapping[str, Any]],
    snap_end: Mapping[str, Mapping[str, Any]],
    pnl_field: str,
    fallback_pnl: Mapping[str, float | None],
) -> tuple[dict[str, float], dict[str, str]]:
    """ROI over the evaluation window per account: ``snap_end[a][pnl_field] / account_value(start)``; an account
    absent from the end snapshot (or with no PnL there) takes ``fallback_pnl[a]`` (``portfolio``) when present. Returns
    (roi by account, source by account: 'snapshot' | 'portfolio' | 'missing')."""
    roi: dict[str, float] = {}
    src: dict[str, str] = {}
    for a in universe:
        av = snap_start[a]["account_value"]
        e = snap_end.get(a)
        pnl = e.get(pnl_field) if e else None
        if pnl is not None and math.isfinite(pnl):
            roi[a], src[a] = pnl / av, "snapshot"
            continue
        fb = fallback_pnl.get(a)
        if fb is not None and math.isfinite(fb):
            roi[a], src[a] = fb / av, "portfolio"
        else:
            src[a] = "missing"
    return roi, src


def h1_period(
    universe: Sequence[str],
    signal: Mapping[str, float],
    roi: Mapping[str, float],
    *,
    missing_as: float | None = None,
    drop_top_frac: float = 0.0,
) -> dict[str, Any]:
    """IC and D for one period. Accounts without an outcome are dropped, or get ``missing_as`` (sensitivity −1).
    ``drop_top_frac`` > 0 removes the top fraction of accounts by |signal| (formation PnL) first."""
    names = [a for a in universe if a in roi or missing_as is not None]
    if drop_top_frac > 0 and names:
        k = int(math.ceil(len(names) * drop_top_frac))
        top = set(sorted(names, key=lambda a: (-abs(signal[a]), a))[:k])
        names = [a for a in names if a not in top]
    sig = [signal[a] for a in names]
    out = [roi.get(a, missing_as) for a in names]
    return period_metrics(sig, [float(x) for x in out], TOP_FRAC)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------------------------------
# Tracked accounts, positions replay


def tracked_from_snapshot(snap: Mapping[str, Mapping[str, Any]], top_n: int, min_value: float | None = None) -> set[str]:
    """The collector's watch list from one snapshot: top ``top_n`` by pnl_month and top ``top_n`` by pnl_all (among
    accounts worth >= ``min_value`` when given; an unknown value passes, as ``COALESCE(account_value, min)`` in
    ``Store.tracked_traders``) — the collector's list recomputed from one snapshot."""
    rows = [(a, r) for a, r in snap.items() if min_value is None or r.get("account_value") is None or r["account_value"] >= min_value]
    out: set[str] = set()
    for f in ("pnl_month", "pnl_all"):
        ranked = sorted((x for x in rows if x[1].get(f) is not None), key=lambda x: (-x[1][f], x[0]))
        out.update(a for a, _ in ranked[:top_n])
    return out


class SnapshotsAt:
    """Point-in-time view of daily snapshots ``{day: {account: row with 'ts'}}``: at time ``t`` (unix s) the visible
    snapshot is the latest day whose stored rows were **all** written before ``t`` (its last row ts < t), whole.
    The store keeps only the last version of a row (``INSERT OR REPLACE`` on reruns), so "that day's rows with
    ts < t" of a day still being rewritten would be a partial, biased snapshot; the whole earlier day is complete and
    uses nothing written at or after ``t``. It can lag the collector's own list (which read the day's early version)
    by up to a day, never lead it."""

    def __init__(self, snaps: Mapping[date, Mapping[str, Mapping[str, Any]]]) -> None:
        self._snaps = snaps
        self._days = sorted(d for d in snaps if snaps[d])
        self._last = [max(int(r["ts"]) for r in snaps[d].values()) for d in self._days]
        self._mono = all(a <= b for a, b in zip(self._last, self._last[1:], strict=False))

    def key(self, t: int) -> date | None:
        """The visible day; None before the first complete snapshot."""
        if self._mono:
            i = bisect.bisect_left(self._last, t) - 1
            return self._days[i] if i >= 0 else None
        cand = [d for d, f in zip(self._days, self._last, strict=True) if f < t]
        return max(cand) if cand else None

    def rows(self, t: int) -> tuple[date | None, Mapping[str, Mapping[str, Any]]]:
        d = self.key(t)
        return (None, {}) if d is None else (d, self._snaps[d])


class WatchListAt:
    """The collector's watch list (top ``top_n`` by pnl_month ∪ by pnl_all) at time ``t``, rebuilt only from a
    snapshot written entirely before ``t`` (:class:`SnapshotsAt`; cached by day)."""

    def __init__(self, snaps: Mapping[date, Mapping[str, Mapping[str, Any]]], top_n: int, min_value: float | None = None) -> None:
        self.view, self.top_n, self.min_value = SnapshotsAt(snaps), top_n, min_value
        self._cache: dict[date, frozenset[str]] = {}

    def at(self, t: int) -> frozenset[str]:
        k = self.view.key(t)
        if k is None:
            return frozenset()
        if k not in self._cache:
            _, rows = self.view.rows(t)
            self._cache[k] = frozenset(tracked_from_snapshot(rows, self.top_n, self.min_value))
        return self._cache[k]


def replay_checkpoints(
    rows: Iterable[Mapping[str, Any]], delta_start: int | None, checkpoints: Sequence[int], rebaseline: Iterable[int] = ()
) -> Iterator[tuple[int, dict[str, dict[str, dict[str, Any]]]]]:
    """Incremental :func:`smart_money.replay_positions` over ``rows`` sorted by ts: yields (checkpoint, state as of
    that ts inclusive) for each checkpoint in ascending order. A run in ``rebaseline`` is read as a full set for every
    account in it (e.g. the first Polymarket v2 run, whose rows are a schema artefact, not trades)."""
    cps = sorted(set(checkpoints))
    reb = set(rebaseline)
    state: dict[str, dict[str, dict[str, Any]]] = {}
    baselined: set[str] = set()
    i = 0
    group: list[Mapping[str, Any]] = []
    key: tuple[int, str] | None = None

    def apply(g: list[Mapping[str, Any]], ts: int, account: str) -> None:
        full = delta_start is None or ts < delta_start or account not in baselined or ts in reb
        if delta_start is not None and ts >= delta_start:
            baselined.add(account)
        cur = {} if full else dict(state.get(account, {}))
        for r in g:
            side, market = str(r.get("side") or ""), str(r.get("market") or "")
            if side == "flat" or not market:
                continue
            if side == "closed":
                cur.pop(market, None)
            else:
                cur[market] = dict(r)
        state[account] = cur

    for r in rows:
        ts, acc = int(r["ts"]), str(r["account"])
        while i < len(cps) and ts > cps[i]:
            if key is not None and key[0] <= cps[i]:
                apply(group, *key)
                group, key = [], None
            yield cps[i], {a: dict(p) for a, p in state.items()}
            i += 1
        if key is not None and key != (ts, acc):
            apply(group, *key)
            group = []
        key = (ts, acc)
        group.append(r)
    if key is not None:
        apply(group, *key)
    while i < len(cps):
        yield cps[i], {a: dict(p) for a, p in state.items()}
        i += 1


def first_run_at_or_after(run_ts: Sequence[int], t: int, max_lag: int) -> int | None:
    """The first run ts in [t, t + max_lag] (seconds) from a sorted list; None if there is none (a gap)."""
    lo, hi = 0, len(run_ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if run_ts[mid] < t:
            lo = mid + 1
        else:
            hi = mid
    return run_ts[lo] if lo < len(run_ts) and run_ts[lo] <= t + max_lag else None


# ---------------------------------------------------------------------------------------------------------------------
# H2


def strong_accounts(
    snap: Mapping[str, Mapping[str, Any]], classes: Mapping[str, str], tracked: Iterable[str], top_frac: float = TOP_FRAC
) -> set[str]:
    """Tracked accounts in the top ``top_frac`` of the H1 universe of the day by pnl_month."""
    uni = h1_universe(snap, classes, "pnl_month")
    k = max(1, int(round(len(uni) * top_frac))) if uni else 0
    top = set(sorted(uni, key=lambda a: (-snap[a]["pnl_month"], a))[:k])
    return top & set(tracked)


def relative_return(closes: Mapping[date, float], btc: Mapping[date, float], d: date, h: int) -> float | None:
    """Simple return of a coin from the UTC daily close of ``d`` to the close of ``d + h`` minus BTC's over the same
    span (decimal); None when any close is missing or not positive."""
    e = d + timedelta(days=h)
    c0, c1, b0, b1 = closes.get(d), closes.get(e), btc.get(d), btc.get(e)
    if not all(x is not None and x > 0 for x in (c0, c1, b0, b1)):
        return None
    return (c1 / c0 - 1.0) - (b1 / b0 - 1.0)  # type: ignore[operator]


def h2_day(signal: Mapping[str, float], outcome: Mapping[str, float | None], cost_per_leg: float = H2_COST_PER_LEG) -> dict[str, Any]:
    """One signal day: coins with a non-zero signal and a known outcome; rank IC, and the long top-quintile minus
    short bottom-quintile mean spread gross and net of two legs' round-trip costs. None metrics below H2_MIN_COINS."""
    coins = sorted(c for c, s in signal.items() if s != 0 and outcome.get(c) is not None)
    n = len(coins)
    if n < H2_MIN_COINS:
        return {"n": n, "ic": None, "spread": None, "spread_net": None}
    s = [signal[c] for c in coins]
    o = [float(outcome[c]) for c in coins]  # type: ignore[arg-type]
    order = sorted(range(n), key=lambda i: (-s[i], coins[i]))
    k = max(1, int(round(n * TOP_FRAC)))
    top = statistics.fmean(o[i] for i in order[:k])
    bottom = statistics.fmean(o[i] for i in order[-k:])
    spread = top - bottom
    return {"n": n, "ic": spearman(s, o), "spread": spread, "spread_net": spread - 2 * cost_per_leg}


def nw_summary(values: Sequence[float], lag: int) -> dict[str, Any]:
    """Mean of a daily series, Newey-West (Bartlett) SE with ``lag``, normal 95 % CI and MDE = 2.8·SE."""
    v = [x for x in values if x is not None and math.isfinite(x)]
    n = len(v)
    if n < 2:
        return {"n": n, "mean": v[0] if v else None, "se": None, "lo": None, "hi": None, "mde": None}
    m = statistics.fmean(v)
    se = newey_west_se(v, lag)
    return {"n": n, "mean": m, "se": se, "lo": m - Z95 * se, "hi": m + Z95 * se, "mde": Z80 * se}


def h2_decision(
    ic: Mapping[str, Any],
    spread_net: Mapping[str, Any],
    gate: bool,
    level: float = DECISION_LEVEL,
    *,
    look: int | None,
    earlier_decided: bool | None,
) -> dict[str, Any]:
    """Registered H2 rule for the primary h = 7 (summaries from :func:`nw_summary`): no gate -> 'inconclusive';
    gate passed -> 'confirmed' if mean IC >= 0.03 with the lower CI bound > 0, or the net spread >= 0.01 with the
    lower bound > 0; else 'rejected'. CI bounds at the multiplicity-corrected two-sided ``level`` (2 routes x 3 looks).

    R1 (decisive looks only): ``look`` from :func:`decisive_look`; ``earlier_decided`` — whether an earlier decisive
    look already gave 'confirmed' / 'rejected' (None = unknown: no accepted prior). The decision is taken on the first
    decisive look with the gate passed, so 'descriptive' when the as-of is not decisive, when an earlier look decided,
    or when a look >= 2 has no accepted prior (unknown history)."""
    z = z_two_sided(level)

    def lo(s: Mapping[str, Any]) -> float | None:
        return None if s.get("mean") is None or s.get("se") is None else s["mean"] - z * s["se"]

    ic_lo, sp_lo = lo(ic), lo(spread_net)
    ic_ok = ic.get("mean") is not None and ic_lo is not None and ic["mean"] >= H2_IC_MIN and ic_lo > 0
    sp_ok = spread_net.get("mean") is not None and sp_lo is not None and spread_net["mean"] >= H2_SPREAD_MIN and sp_lo > 0
    verdict = "inconclusive" if not gate else ("confirmed" if ic_ok or sp_ok else "rejected")
    if look is None or earlier_decided is True or (look >= 2 and earlier_decided is None):
        verdict = "descriptive"
    return {"verdict": verdict, "level": level, "ic_lo": ic_lo, "spread_lo": sp_lo, "ic_ok": ic_ok, "spread_ok": sp_ok}


def decisive_look(as_of_s: int | None) -> int | None:
    """1-based index of a named decisive look (:data:`DECISIVE_AS_OF`, exact 00:00 UTC), else None."""
    return DECISIVE_AS_OF.index(as_of_s) + 1 if as_of_s in DECISIVE_AS_OF else None


def registration_fingerprint(extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The registered parameters a decision depends on (rounded, JSON-stable); a prior must carry the same."""
    own = {
        "decisive_as_of": list(DECISIVE_AS_OF),
        "decision_level": round(DECISION_LEVEL, 10),
        "h2": [H2_MIN_COINS, H2_COST_PER_LEG, H2_IC_MIN, H2_SPREAD_MIN],
        "h3": [H3_FEE, H3_P_MIN, H3_P_MAX, H3_EDGE_MIN, H3_MIN_MARKETS, H3_FRESH_TOL, H3_UMA_GRACE_S, H3_UNRESOLVED_MAX,
               H3_GAMMA_MISSING_MAX, H3_TOP_WALLETS_DROP, H3_MAX_WALLET_SHARE],
        "h1": [MIN_VALUE, TOP_FRAC, TOP_DROP_FRAC],
    }  # fmt: skip
    out = {**own, **(dict(extra) if extra else {})}
    return json.loads(json.dumps(out, sort_keys=True))


def check_prior(prior: Mapping[str, Any] | None, as_of_s: int, fingerprint: Mapping[str, Any]) -> str | None:
    """R1: reason to refuse a prior slice (None = accepted or no prior). ``prior`` from the previous slice's full JSON:
    {"as_of_s", "prior_as_of_s", "stopped", "registration", "status": {ranking: status}}. Accepted only when the current
    as-of is decisive look k >= 2, the prior's as-of is look k - 1, the prior's H3 was not stopped by the Gamma check,
    neither ranking's prior status is 'descriptive' or missing, the registered parameters are the same, and a prior
    that is itself look >= 2 was run with an accepted prior of its own look - 1 (its window is a replication window)."""
    if prior is None:
        return None
    look = decisive_look(as_of_s)
    if look is None:
        return "the current as-of is not a decisive look"
    p_look = decisive_look(prior.get("as_of_s"))
    if p_look is None or p_look != look - 1:
        return "the prior's as-of is not the previous decisive look"
    if prior.get("stopped"):
        return "the prior's H3 was stopped by the Gamma data check"
    st = prior.get("status") or {}
    if any(st.get(rk) in (None, "descriptive") for rk in ("overall", "crypto")):
        return "the prior has a descriptive or missing H3 status"
    if prior.get("registration") != fingerprint:
        return "the prior was run with different registered parameters"
    if p_look >= 2 and decisive_look(prior.get("prior_as_of_s")) != p_look - 1:
        return "the prior (look >= 2) was not run with the prior of its own previous look"
    return None


def early_close(end_ts: int | None, close_ts: int | None) -> bool:
    """m7 (descriptive only): the market closed more than a day before its *current* scheduled ``endDate``."""
    return end_ts is not None and close_ts is not None and close_ts < end_ts - EARLY_CLOSE_S


# ---------------------------------------------------------------------------------------------------------------------
# H3


def new_positions(
    rows: Iterable[Mapping[str, Any]],
    delta_start: int | None,
    until: int,
    rebaseline: Iterable[int] = (),
    rebaseline_from: int | None = None,
) -> list[dict[str, Any]]:
    """Polymarket tokens that appear in a wallet's book after its baseline, up to ``until`` (inclusive). ``rows``
    sorted by ts. Baselines (full sets, no events): the wallet's first observation, a run in ``rebaseline``, and the
    wallet's first observation at or after ``rebaseline_from`` (per wallet: a wallet the first Polymarket v2 run did
    not poll gets its v2 baseline at its own first v2 observation). Each item: ts, account, market (token id), side
    (outcome name), size, value_usd, entry_px, title, ``p_obs`` = value_usd / size, ``n_before`` = positions in the
    wallet's book at the previous observation, ``first_pair`` = the (wallet, token) pair was never seen in any earlier
    observation of the wallet, baselines included (a token that drops out of the collector's top-100 / >= $100 set
    and comes back is a repeat, not a new position)."""
    reb = set(rebaseline)
    book: dict[str, set[str]] = {}
    baselined: set[str] = set()
    rebased: set[str] = set()
    seen: dict[str, set[str]] = {}
    out: list[dict[str, Any]] = []
    groups: dict[tuple[int, str], list[Mapping[str, Any]]] = {}
    order: list[tuple[int, str]] = []
    for r in rows:
        ts = int(r["ts"])
        if ts > until:
            break
        k = (ts, str(r["account"]))
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    for ts, acc in order:
        g = groups[(ts, acc)]
        reb_now = ts in reb or (rebaseline_from is not None and ts >= rebaseline_from and acc not in rebased)
        if rebaseline_from is not None and ts >= rebaseline_from:
            rebased.add(acc)
        full = delta_start is None or ts < delta_start or acc not in baselined or reb_now
        first = acc not in baselined and acc not in book
        if delta_start is not None and ts >= delta_start:
            baselined.add(acc)
        prev = book.get(acc, set())
        cur = set() if full else set(prev)
        pairs = seen.setdefault(acc, set())
        for r in g:
            side, market = str(r.get("side") or ""), str(r.get("market") or "")
            if side == "flat" or not market:
                continue
            if side == "closed":
                cur.discard(market)
                continue
            cur.add(market)
            was_seen = market in pairs
            pairs.add(market)
            if first or reb_now or market in prev:
                continue
            size, val = r.get("size"), r.get("value_usd")
            p = (val / size) if size and val is not None and size > 0 else None
            out.append(
                {
                    "ts": ts,
                    "account": acc,
                    "market": market,
                    "side": side,
                    "size": size,
                    "value_usd": val,
                    "entry_px": r.get("entry_px"),
                    "title": r.get("title"),
                    "p_obs": p,
                    "n_before": len(prev),
                    "first_pair": not was_seen,
                }
            )
        book[acc] = cur
    return out


def is_fresh_entry(p_obs: float | None, entry_px: float | None, tol: float = H3_FRESH_TOL) -> bool:
    """Fresh-entry guard: the wallet's average entry price is within ``tol`` of the observed price, i.e. the position
    was just bought rather than crossing the collector's $100 floor after a price rise."""
    return p_obs is not None and entry_px is not None and math.isfinite(entry_px) and abs(p_obs - entry_px) <= tol + 1e-12


def _parse_iso_s(t_raw: Any) -> int | None:
    from datetime import datetime  # noqa: PLC0415

    if not isinstance(t_raw, str) or not t_raw:
        return None
    s = t_raw.strip().replace(" ", "T").replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return int(dt.timestamp()) if dt.tzinfo else None


def gamma_end_ts(market: Mapping[str, Any]) -> int | None:
    """Scheduled end (unix s) of a Gamma market: ``endDate`` only (not the actual close time, which depends on the
    outcome for "X by date T" markets)."""
    return _parse_iso_s(market.get("endDate"))


def h3_sample(e_ts: int, end_ts: int | None, close_ts: int | None, as_of_s: int, end_after_s: int | None = None) -> str:
    """Sample of one new position by the market's *scheduled* end (slice-1 fix round, before outcomes):
    'no_end_date'; 'seen_at_or_after_end' (observed at or after the scheduled end: resolution arbitrage, not a
    signal); 'closed_before_observation'; 'main' (endDate <= as-of − 7 days, and > ``end_after_s`` when given: the
    replication window after the previous decisive slice); 'earlier_slice' (endDate <= ``end_after_s``);
    'end_in_grace' (as-of − 7 days < endDate <= as-of); 'end_after_as_of' (descriptive only, e.g. closed early)."""
    if end_ts is None:
        return "no_end_date"
    if e_ts >= end_ts:
        return "seen_at_or_after_end"
    if close_ts is not None and close_ts <= e_ts:
        return "closed_before_observation"
    if end_ts <= as_of_s - H3_UMA_GRACE_S:
        if end_after_s is not None and end_ts <= end_after_s:
            return "earlier_slice"
        return "main"
    return "end_in_grace" if end_ts <= as_of_s else "end_after_as_of"


def h3_evaluate(
    rows: Sequence[Mapping[str, Any]],
    *,
    unresolved_share: float | None,
    level: float = DECISION_LEVEL,
) -> dict[str, Any]:
    """The H3 decision criterion on one ranking's main sample. ``rows``: resolved positions with keys edge, cluster,
    account, n_before, tracked_before, fresh. Criterion met when all hold (CI at the corrected two-sided ``level``):
    registered (mean >= +0.03, lower bound > 0, >= 30 markets); mean > 0 and lower bound > 0 in each guard subsample
    (< 100 positions before, tracked before, fresh entry) and without the 5 wallets with most positions; no wallet
    with >= 25 % of positions; unresolved share <= 5 %. ``excluded`` (upper bound < +0.03) is an interpretation only."""

    def ci(s: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return cluster_mean_ci([r["edge"] for r in s], [r["cluster"] for r in s], level=level)

    def pos(c: Mapping[str, Any]) -> bool:
        return c["mean"] is not None and c["lo"] is not None and c["mean"] > 0 and c["lo"] > 0

    main = ci(rows)
    reg = main["mean"] is not None and main["lo"] is not None and main["mean"] >= H3_EDGE_MIN and main["lo"] > 0
    reg = reg and main["clusters"] >= H3_MIN_MARKETS
    guards = {
        "n_before_lt100": ci([r for r in rows if r["n_before"] < 100]),
        "tracked_before": ci([r for r in rows if r["tracked_before"]]),
        "fresh_entry": ci([r for r in rows if r["fresh"]]),
    }
    counts = Counter(r["account"] for r in rows)
    top = {a for a, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:H3_TOP_WALLETS_DROP]}
    guards["without_top5_wallets"] = ci([r for r in rows if r["account"] not in top])
    max_share = max(counts.values()) / len(rows) if rows else None
    conc_ok = max_share is not None and max_share < H3_MAX_WALLET_SHARE
    unres_ok = unresolved_share is not None and unresolved_share <= H3_UNRESOLVED_MAX
    met = bool(reg and all(pos(g) for g in guards.values()) and conc_ok and unres_ok)
    return {
        "level": level,
        "main": main,
        "registered": reg,
        "guards": guards,
        "guards_ok": {k: pos(g) for k, g in guards.items()},
        "max_wallet_share": max_share,
        "concentration_ok": conc_ok,
        "unresolved_share": unresolved_share,
        "decidable": unres_ok,
        "criterion_met": met,
        "excluded": unres_ok and main["hi"] is not None and main["hi"] < H3_EDGE_MIN,
    }


def h3_status(criterion_met: bool, decidable: bool, prior_met: bool | None, *, look: int | None) -> str:
    """Pre-stated rule for repeated looks (slice-1 fix round): 'confirmed' only by replication — the criterion met at
    decisive slice k (``prior_met``) and again on the positions new to slice k + 1; a single slice gives at most
    'candidate'. 'undecided' when the slice is not decidable (> 5 % unresolved).

    R1: ``look`` from :func:`decisive_look` ('descriptive' when None); look 1 takes no prior; a look >= 2 without an
    accepted prior of look k - 1 is 'descriptive' (its main sample overlaps the earlier look's); ``prior_met`` must
    come from a prior accepted by :func:`check_prior`."""
    if look is None or (look >= 2 and prior_met is None):
        return "descriptive"
    if look == 1 and prior_met is not None:
        raise ValueError("decisive look 1 takes no prior")
    if not decidable:
        return "undecided"
    if prior_met is None:
        return "candidate" if criterion_met else "not_candidate"
    return "confirmed" if (prior_met and criterion_met) else "not_confirmed"


def gamma_close_ts(market: Mapping[str, Any]) -> int | None:
    """Close time (unix s) of a Gamma market: ``closedTime``, else ``umaEndDate``, else ``endDate``; None if absent
    or without a time zone."""
    return _parse_iso_s(market.get("closedTime") or market.get("umaEndDate") or market.get("endDate"))


def gamma_resolution(market: Mapping[str, Any], token_id: str, as_of_s: int) -> tuple[float | None, str]:
    """Outcome (1.0 / 0.0) of ``token_id`` from a Gamma market object if it resolved by ``as_of_s`` (unix s), else
    (None, reason). Resolved = ``closed`` and the token's outcome price is exactly 0 or 1; the close time
    (``closedTime``, else ``umaEndDate`` / ``endDate``) must be <= as-of."""
    import json  # noqa: PLC0415

    def parse_list(x: Any) -> list[Any]:
        if isinstance(x, str):
            try:
                x = json.loads(x)
            except ValueError:
                return []
        return list(x) if isinstance(x, list) else []

    ids = [str(t) for t in parse_list(market.get("clobTokenIds"))]
    prices = parse_list(market.get("outcomePrices"))
    if token_id not in ids or len(prices) != len(ids):
        return None, "token_not_in_market"
    if not market.get("closed"):
        return None, "open"
    t_s = gamma_close_ts(market)
    if t_s is None:
        return None, "no_close_time"
    if t_s > as_of_s:
        return None, "closed_after_as_of"
    try:
        p = float(prices[ids.index(token_id)])
    except (TypeError, ValueError):
        return None, "bad_price"
    if p not in (0.0, 1.0):
        return None, "not_binary_resolved"
    return p, "resolved"


def h3_edge(outcome: float, p: float, fee: float = H3_FEE) -> float:
    """Follower's edge of one new position: outcome − price − fee (probability points)."""
    return outcome - p - fee


def cochran_q(means: Sequence[float], ses: Sequence[float]) -> dict[str, Any]:
    """Heterogeneity of group means with standard errors: Cochran's Q (inverse-variance weights), df, I² and the
    pooled mean. Groups with a non-positive or missing SE are skipped."""
    pairs = [(m, s) for m, s in zip(means, ses, strict=True) if m is not None and s is not None and s > 0 and math.isfinite(s)]
    k = len(pairs)
    if k < 2:
        return {"k": k, "q": None, "df": None, "i2": None, "pooled": None}
    w = [1.0 / (s * s) for _, s in pairs]
    pooled = sum(wi * m for wi, (m, _) in zip(w, pairs, strict=True)) / sum(w)
    q = sum(wi * (m - pooled) ** 2 for wi, (m, _) in zip(w, pairs, strict=True))
    df = k - 1
    return {"k": k, "q": q, "df": df, "i2": max(0.0, (q - df) / q) if q > 0 else 0.0, "pooled": pooled}


def cluster_mean_ci(values: Sequence[float], clusters: Sequence[str], level: float = 0.95) -> dict[str, Any]:
    """Mean with a cluster-robust SE (CR1: G/(G−1) correction) and a two-sided Student-t CI at ``level`` (default
    95 %) with G − 1 df."""
    n = len(values)
    if n != len(clusters):
        raise ValueError("values and clusters differ in length")
    groups = sorted(set(clusters))
    g = len(groups)
    if n == 0:
        return {"n": 0, "clusters": 0, "mean": None, "se": None, "lo": None, "hi": None}
    m = statistics.fmean(values)
    if g < 2:
        return {"n": n, "clusters": g, "mean": m, "se": None, "lo": None, "hi": None}
    sums: dict[str, float] = {}
    for x, c in zip(values, clusters, strict=True):
        sums[c] = sums.get(c, 0.0) + (x - m)
    var = (g / (g - 1)) * sum(s * s for s in sums.values()) / (n * n)
    se = math.sqrt(var)
    q = t975(g - 1) if abs(level - 0.95) < 1e-12 else t_quantile(0.5 + level / 2.0, g - 1)
    h = q * se
    return {"n": n, "clusters": g, "mean": m, "se": se, "lo": m - h, "hi": m + h}
