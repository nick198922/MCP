"""Dealer gamma reconstructed from Deribit taker flow and next-day range / autocorrelation tests.

IDEA-20261025-gex-taker-flow-range. Pure functions and in-memory state only (no I/O); every time is a parameter.

Conventions: one Deribit BTC option contract = 1 BTC; the taker side of a print is ``direction``; dealer
inventory of a series = minus the cumulative signed taker flow ``Q`` (buy = +amount). Gamma is Black-76 with
r = 0 on one spot ``S`` at the reference time ``t0`` (no per-series stale forward), ``T`` = ms to the 08:00 UTC
expiry / 365 days. Dealer gamma exposure ``G = sum(-Q_i * Gamma_i * S^2 / 100)`` is USD of delta change per
1 % move of the underlying. IV inputs are decimals (0.45 = 45 %).
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cache
from typing import Any

import numpy as np

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
YEAR_MS = 365 * DAY_MS
_MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
_NAME = re.compile(r"^BTC-(\d{1,2})([A-Z]{3})(\d{2})-(\d+)-([CP])$")
_SQRT_2PI = math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class OptionSeries:
    expiry_ms: int  # 08:00 UTC of the expiry date
    strike: float
    is_call: bool


@cache
def parse_option(name: str) -> OptionSeries | None:
    """``BTC-8MAR24-68000-C`` -> OptionSeries; None for anything else (futures, USDC-linear, malformed)."""
    m = _NAME.match(name)
    if not m or m.group(2) not in _MON:
        return None
    exp = datetime(2000 + int(m.group(3)), _MON[m.group(2)], int(m.group(1)), 8, tzinfo=UTC)
    return OptionSeries(int(exp.timestamp() * 1000), float(m.group(4)), m.group(5) == "C")


def b76_gamma(F: float, K: float, T: float, sigma: float) -> float:
    """Black-76 gamma (r = 0) per unit of the underlying: phi(d1) / (F sigma sqrt T); 0 outside the domain."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return 0.0
    st = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * st * st) / st
    return math.exp(-0.5 * d1 * d1) / _SQRT_2PI / (F * st)


@dataclass
class SeriesState:
    q: float = 0.0  # cumulative signed taker flow, BTC (buy +)
    gross: float = 0.0  # cumulative traded amount, BTC
    iv: float = 0.0  # IV of the latest print, decimal
    iv_ts: int = -1  # its timestamp, ms


@dataclass
class FlowBook:
    """Cumulative taker flow per series, fed in time order. ``max_amount``: drop prints with amount >= it (blocks)."""

    max_amount: float | None = None
    series: dict[str, SeriesState] = field(default_factory=dict)
    spot: float = 0.0  # index_price of the latest print of any series
    spot_ts: int = -1
    last_ts: int = -1  # latest applied print
    n_added: int = 0
    n_block_skipped: int = 0
    n_unparsed: int = 0

    def add(self, trades: Iterable[Mapping[str, Any]], upto_ms: int) -> list[Mapping[str, Any]]:
        """Apply prints with ``timestamp <= upto_ms``; return the later ones unapplied (the caller carries them)."""
        later: list[Mapping[str, Any]] = []
        for t in trades:
            ts = int(t["timestamp"])
            if ts > upto_ms:
                later.append(t)
            else:
                self._apply(t, ts)
        return later

    def _apply(self, t: Mapping[str, Any], ts: int) -> None:
        name = str(t.get("instrument_name") or "")
        if parse_option(name) is None:
            self.n_unparsed += 1
            return
        self.last_ts = max(self.last_ts, ts)
        ip = t.get("index_price")
        if ip and ts >= self.spot_ts:
            self.spot, self.spot_ts = float(ip), ts
        amt = float(t.get("amount") or 0.0)
        if self.max_amount is not None and amt >= self.max_amount:
            self.n_block_skipped += 1
            return
        st = self.series.get(name)
        if st is None:
            st = self.series[name] = SeriesState()
        st.q += amt if t.get("direction") == "buy" else -amt
        st.gross += amt
        iv = t.get("iv")
        if iv is not None and 0.0 < float(iv) < 500.0 and ts >= st.iv_ts:
            st.iv, st.iv_ts = float(iv) / 100.0, ts
        self.n_added += 1

    def drop_expired(self, t_ms: int) -> int:
        """Forget series whose expiry is at or before ``t_ms``; returns how many were dropped."""
        dead = [n for n in self.series if parse_option(n).expiry_ms <= t_ms]  # type: ignore[union-attr]
        for n in dead:
            del self.series[n]
        return len(dead)


def interp_flat(curve: Sequence[tuple[float, float]], x: float) -> float:
    """Linear interpolation on sorted (x, y) points with flat extrapolation."""
    xs = [p[0] for p in curve]
    i = bisect.bisect_left(xs, x)
    if i <= 0:
        return curve[0][1]
    if i >= len(curve):
        return curve[-1][1]
    (x0, y0), (x1, y1) = curve[i - 1], curve[i]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


@dataclass(frozen=True)
class GammaSnapshot:
    t0_ms: int
    spot: float
    gex: float  # dealer gamma, USD per 1 % move
    gex_call: float
    gex_put: float
    n_live: int  # live series with Q != 0
    n_own_iv: int
    n_interp_iv: int
    n_excluded: int  # no fresh IV in the series or its expiry
    live_abs_q: float
    excluded_abs_q: float
    profile: dict[float, float]  # strike -> dealer gamma (calls + puts) of ``profile_expiry_ms``
    profile_strikes: tuple[float, ...]  # all strikes of that expiry seen in the flow


def dealer_gamma(
    book: FlowBook, t0_ms: int, *, iv_max_age_ms: int, spot: float | None = None, profile_expiry_ms: int | None = None
) -> GammaSnapshot:
    """Dealer gamma at ``t0_ms`` from the book's flow (series expiring at or before t0 are out).

    sigma of a series = its latest print IV if not older than ``iv_max_age_ms``; else interpolated (linear in
    ln K/S, flat outside) from the fresh series of the same expiry; else the series is excluded and counted.
    Raises ValueError if the book holds prints after ``t0_ms`` (look-ahead guard).
    """
    if book.last_ts > t0_ms:
        raise ValueError("book contains prints after t0")
    S = book.spot if spot is None else spot
    if S <= 0:
        raise ValueError("no spot")
    live: list[tuple[OptionSeries, SeriesState]] = []
    fresh: dict[int, dict[float, list[float]]] = {}
    for name, st in book.series.items():
        s = parse_option(name)
        if s is None or s.expiry_ms <= t0_ms:
            continue
        live.append((s, st))
        if st.iv_ts >= 0 and t0_ms - st.iv_ts <= iv_max_age_ms:
            fresh.setdefault(s.expiry_ms, {}).setdefault(s.strike, []).append(st.iv)
    curves = {e: sorted((math.log(k / S), sum(v) / len(v)) for k, v in d.items()) for e, d in fresh.items()}
    g_call = g_put = abs_q = abs_x = 0.0
    n_live = n_own = n_int = n_x = 0
    prof: dict[float, float] = {}
    strikes: set[float] = set()
    for s, st in live:
        if profile_expiry_ms is not None and s.expiry_ms == profile_expiry_ms:
            strikes.add(s.strike)
        if st.q == 0.0:
            continue
        n_live += 1
        abs_q += abs(st.q)
        if st.iv_ts >= 0 and t0_ms - st.iv_ts <= iv_max_age_ms:
            sigma = st.iv
            n_own += 1
        elif s.expiry_ms in curves:
            sigma = interp_flat(curves[s.expiry_ms], math.log(s.strike / S))
            n_int += 1
        else:
            n_x += 1
            abs_x += abs(st.q)
            continue
        g = -st.q * b76_gamma(S, s.strike, (s.expiry_ms - t0_ms) / YEAR_MS, sigma) * S * S / 100.0
        if s.is_call:
            g_call += g
        else:
            g_put += g
        if profile_expiry_ms is not None and s.expiry_ms == profile_expiry_ms:
            prof[s.strike] = prof.get(s.strike, 0.0) + g
    return GammaSnapshot(t0_ms, S, g_call + g_put, g_call, g_put, n_live, n_own, n_int, n_x, abs_q, abs_x, prof, tuple(sorted(strikes)))


# ----------------------------------------------------------------------------- bars: controls and response


def rv_log(bars: Mapping[int, Sequence[float]], t0_ms: int, days: int, *, min_frac: float = 0.9) -> float | None:
    """ln realized vol (sqrt of summed squared 1h close-to-close log returns) over ``days`` ending at t0.

    ``bars``: open_ms -> (o, h, l, c). Uses only bars opening before t0 (closed by t0); a missing return is
    filled by scaling the sum (None if fewer than ``min_frac`` of the returns exist).
    """
    n = 24 * days
    rs: list[float] = []
    for j in range(n):
        o = t0_ms - (n - j) * HOUR_MS
        b, p = bars.get(o), bars.get(o - HOUR_MS)
        if b is not None and p is not None and b[3] > 0 and p[3] > 0:
            rs.append(math.log(b[3] / p[3]))
    if len(rs) < min_frac * n:
        return None
    ss = sum(r * r for r in rs) * n / len(rs)
    return 0.5 * math.log(ss) if ss > 0 else None


def next_day(bars: Mapping[int, Sequence[float]], t0_ms: int, hours: int = 24) -> dict[str, Any] | None:
    """Response over [t0, t0 + hours): ln Parkinson vol from the hourly high/low and the hourly close-to-close
    returns (the first one against the close of the bar opening at t0 - 1h). None if any bar is missing."""
    prev = bars.get(t0_ms - HOUR_MS)
    if prev is None:
        return None
    hl2 = 0.0
    rets: list[float] = []
    last_c = prev[3]
    for h in range(hours):
        b = bars.get(t0_ms + h * HOUR_MS)
        if b is None or b[2] <= 0 or b[3] <= 0:
            return None
        hl2 += math.log(b[1] / b[2]) ** 2
        rets.append(math.log(b[3] / last_c))
        last_c = b[3]
    if hl2 <= 0:
        return None
    return {"y": 0.5 * math.log(hl2 / (4.0 * math.log(2.0))), "rets": rets}


def h2_day_stat(rets: Sequence[float], v0: float) -> float:
    """Mean lag-1 product of hourly returns normalised by an hourly variance ``v0`` known at t0 (condition 6)."""
    pr = [rets[i] * rets[i - 1] for i in range(1, len(rets))]
    return sum(pr) / len(pr) / v0


def expiry_flags(d: date) -> tuple[bool, bool]:
    """(monthly, quarterly): is d + 1 day the last Friday of its month (of Mar/Jun/Sep/Dec) — the 08:00 expiry
    inside the 24 h window that starts at d 09:00 UTC. Weekly Fridays are absorbed by day-of-week dummies."""
    e = d + timedelta(days=1)
    last_fri = e.weekday() == 4 and (e + timedelta(days=7)).month != e.month
    return last_fri, last_fri and e.month in (3, 6, 9, 12)


# ----------------------------------------------------------------------------- statistics


def trailing_z(values: Sequence[float], window: int, min_obs: int) -> list[float | None]:
    """z of each value against the ``window`` values strictly before it (no look-ahead)."""
    out: list[float | None] = []
    for i, v in enumerate(values):
        past = values[max(0, i - window) : i]
        if len(past) < min_obs:
            out.append(None)
            continue
        m = sum(past) / len(past)
        sd = math.sqrt(sum((x - m) ** 2 for x in past) / (len(past) - 1))
        out.append((v - m) / sd if sd > 0 else None)
    return out


def sign_persistence(neg: Sequence[bool], days_per_year: float = 365.25) -> dict[str, float]:
    """Share of True, number of switches, switches per year, lag-1 autocorrelation of the indicator and the
    AR(1) effective sample size n (1 - rho) / (1 + rho)."""
    n = len(neg)
    x = np.asarray(neg, dtype=float)
    ch = int(np.sum(x[1:] != x[:-1]))
    xc = x - x.mean()
    den = float(np.sum(xc * xc))
    rho = float(np.sum(xc[1:] * xc[:-1]) / den) if den > 0 else 1.0
    neff = n * (1 - rho) / (1 + rho) if rho > -1 else float(n)
    return {
        "n": n,
        "share": float(x.mean()),
        "changes": ch,
        "changes_per_year": ch / (n / days_per_year),
        "rho1": rho,
        "n_eff": max(1.0, min(float(n), neff)),
    }


def hac_se_indicator(x: Sequence[float], Z: Sequence[Sequence[float]], e: Sequence[float], lag: int) -> float:
    """Newey-West (Bartlett) SE of the coefficient on ``x`` in an OLS of y on [Z, x] under H0 (beta = 0).

    ``e`` are residuals of y on Z alone (no x), so the SE is computed without estimating beta (power gate).
    Frisch-Waugh: x~ = x residualised on Z; Var = sum_k w_k sum_t x~_t e_t x~_{t-k} e_{t-k} / (sum x~^2)^2.
    """
    Zm = np.asarray(Z, dtype=float)
    xv = np.asarray(x, dtype=float)
    xt = xv - Zm @ np.linalg.lstsq(Zm, xv, rcond=None)[0]
    u = xt * np.asarray(e, dtype=float)
    s = float(u @ u)
    for k in range(1, lag + 1):
        s += 2.0 * (1.0 - k / (lag + 1)) * float(u[k:] @ u[:-k])
    return math.sqrt(max(s, 0.0)) / float(xt @ xt)


def permute_within(x: Sequence[float], groups: Sequence[Any], rng: np.random.Generator) -> np.ndarray:
    """Shuffle ``x`` within each group (e.g. calendar month), keeping each group's values."""
    out = np.asarray(x, dtype=float).copy()
    idx: dict[Any, list[int]] = {}
    for i, g in enumerate(groups):
        idx.setdefault(g, []).append(i)
    for ii in idx.values():
        a = np.asarray(ii)
        out[a] = out[rng.permutation(a)]
    return out


def pin_distance(
    profile: Mapping[float, float], strikes: Sequence[float], spot: float, settle: float, band: float = 0.05
) -> tuple[float, float] | None:
    """H4: (|settle - K*| / spot, mean |settle - K| / spot over strikes within +-band of spot), where K* is the
    in-band strike with the largest positive dealer gamma. None if no in-band strike has positive gamma."""
    cand = [k for k in strikes if abs(k / spot - 1.0) <= band]
    pos = [(g, k) for k, g in profile.items() if g > 0 and abs(k / spot - 1.0) <= band]
    if not cand or not pos:
        return None
    k_star = max(pos)[1]
    return abs(settle - k_star) / spot, sum(abs(settle - k) for k in cand) / len(cand) / spot
