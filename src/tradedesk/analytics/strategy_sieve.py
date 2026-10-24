"""Strategy sieve (IDEA-20261024-strategy-sieve, step 1): a cheap, uniform test of a formalised public strategy.

A strategy is a spec module in ``analytics.sieve_specs`` with one pure function, bars -> ``Signal`` list (side, entry,
stop, optional target, time exit). The sieve trades every signal on 1h paths with the costs and management of the
demo stop-management study (``analytics.stop_management``: maker entry and target, taker stop and time exit plus
slippage, R = net P&L over the live risk unit), one position per symbol, and judges the result:

- entry: always the close of the 1h bar that ends at the signal time ``t_ms``. A spec entry price that differs from
  that close is replaced by it (the stop and target keep their fractions of the entry), and the runner refuses such a
  spec (``stamp_violations``): a limit order below the market is not a signal of this type;
- funding: charged over the holding time on the side of the trade, from the venue history when it covers the entry,
  else the repository stress of 0.01 % per 8 h against the position (``trail_no_target.funding_paid``); R is after
  funding everywhere (signal, placebo, reference management);
- periods by entry time: IS < 2025-03-01 <= OOS < 2026-03-01 <= hidden. The hidden period is opened once per strategy,
  only after it passed IS and OOS (the runner enforces it with the canonical registry);
- placebo: for each real trade, ``PLACEBO_K`` random entries of the same side, with the same stop and target distances
  (fractions of price), hold and management, on the same symbol at random allowed 1h closes of the same period,
  averaged (``simulate_signals`` explains why not on the signal's own date). Part of the pass rule: the paired
  difference signal - placebo must have a day-clustered 95 % CI above 0 in IS and a positive mean in OOS;
- multiplicity: Holm over the registry, with at least ``N_PLAN`` = 100 tests (the card plans 100 strategies), so the
  first run faces the same bar as the hundredth. The tested p of a run is max(p of E[R] > 0, p of signal - placebo > 0)
  in IS (both nulls have to fall);
- pass = E[R] IS >= +0.05R net of costs and funding, day-clustered 95 % CI above 0, signal - placebo CI above 0, Holm
  p < 0.05, OOS mean > 0 and OOS signal - placebo mean > 0;
- power: MDE = (z_{1 - 0.05/N_eff} + z_0.80) x the day-clustered SE (80 % power at the corrected one-sided level); a
  strategy that did not pass, whose MDE is above 0.05R and whose CI still reaches +0.05R is "too few trades".

Pure functions, no I/O; time comes in as data (bar times, entry times), never from a clock. Prices are in real space;
a short is simulated on negated prices (``Bars.mirrored``), as in the stop-management study.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import NormalDist
from typing import Any

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, Bars
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means, holm
from tradedesk.analytics.stop_management import Fees, Outcome, StopRule, risk_unit, simulate_stop_rule
from tradedesk.analytics.trail_no_target import funding_paid


def _ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


START_MS = _ms("2020-02-01T00:00")  # first entry of the point-in-time universe
IS_END_MS = _ms("2025-03-01T00:00")
HIDDEN_MS = _ms("2026-03-01T00:00")  # OOS ends here; the hidden period starts
MIN_EFFECT = 0.05  # R net per trade
ALPHA = 0.05
POWER = 0.80
N_PLAN = 100  # the card's plan: 100 strategies; the Holm family never counts fewer tests
N_BOOT = 10_000
SEED = 20261024
PASS, FAIL, FAIL_OOS, UNDERPOWERED = "пройшла", "ні", "ні (OOS)", "замало угод"
MANAGEMENTS = ("strategy", "B")
PLACEBO_K = 5  # placebo entries per trade, averaged (one draw adds as much noise as the signal itself)
PLACEBO_TRIES = 5  # random hours tried per draw until one has a path
FUND_8H = 0.0001  # funding stress, 0.01 % per 8 h against the position, when the history does not cover the entry
ENTRY_TOL = 1e-9  # relative: a spec entry further than this from the bar close is not "at the close"
SIDES = ("long", "short")
PERIODS = ("is", "oos", "hidden")


def mde_z(n_eff: int = N_PLAN, alpha: float = ALPHA, power: float = POWER) -> float:
    """z of the one-sided corrected level alpha / n_eff plus z of the power: 4.13 for 100 tests."""
    nd = NormalDist()
    return nd.inv_cdf(1 - alpha / max(1, n_eff)) + nd.inv_cdf(power)


MDE_Z = mde_z(N_PLAN)


def n_effective(n_registry: int, n_plan: int = N_PLAN) -> int:
    """Size of the Holm family: the registry (this run included), but never fewer than ``n_plan``."""
    return max(int(n_registry), n_plan)


@dataclass(frozen=True)
class Signal:
    """One entry decided at the close of a bar: ``t_ms`` is that close (the entry time) and ``entry`` its close price.
    Prices in real space; a long needs stop < entry < target, a short stop > entry > target. ``target`` None: exit by
    the stop or the time exit."""

    t_ms: int
    side: str  # long | short
    entry: float
    stop: float
    target: float | None
    hold_ms: int

    def valid(self) -> bool:
        vals = (self.entry, self.stop) + (() if self.target is None else (self.target,))
        if self.side not in SIDES or self.hold_ms <= 0 or not all(math.isfinite(v) for v in vals):
            return False
        s = 1.0 if self.side == "long" else -1.0
        if not s * self.stop < s * self.entry:
            return False
        return self.target is None or s * self.entry < s * self.target

    def long_space(self) -> tuple[float, float, float]:
        """(entry, stop, target) of the same trade as a long on negated prices for a short; no target -> +inf."""
        s = 1.0 if self.side == "long" else -1.0
        return s * self.entry, s * self.stop, (math.inf if self.target is None else s * self.target)


def period_of(t_ms: int) -> str:
    if t_ms < IS_END_MS:
        return "is"
    return "oos" if t_ms < HIDDEN_MS else "hidden"


def stop_rule(management: str) -> StopRule:
    """``strategy``: the spec's own stop, target and time exit, nothing moved (rule A of the study); ``B``: the live
    demo rule, break-even after half the way to the target (from the next 1h bar). B never triggers without a target."""
    if management == "strategy":
        return StopRule("A")
    if management == "B":
        return StopRule("B", be_frac=0.5)
    raise ValueError(f"unknown management {management!r}, expected one of {MANAGEMENTS}")


def symbol_seed(symbol: str, seed: int = SEED) -> int:
    """Deterministic per-symbol seed (independent of worker order and of Python's hash randomisation)."""
    return (seed * 1_000_003 + zlib.crc32(symbol.encode())) % (2**32)


# ---------------------------------------------------------------- trades


@dataclass(frozen=True)
class Trade:
    t_ms: int
    day: int  # UTC day number of the entry (the bootstrap cluster; the placebo of a trade shares it)
    period: str  # is | oos | hidden
    side: str
    tf_ms: int
    r_net: float  # under the chosen management, after costs and funding
    cost_r: float  # fees and slippage, R
    fund_r: float  # funding paid over the hold (negative = received), R
    fund_src: str  # actual | flat (stress) | none (no funding passed)
    exit_kind: str
    exit_ms: int
    r_alt: float  # the other management on the same entry, after its own funding (reference only)
    r_stress: float  # taker entry and every cost x 2 (Fees.scaled(2)), after funding (reference only)
    stop_frac: float  # |entry - stop| / entry
    target_frac: float  # |target - entry| / entry, inf without a target
    entry_gap: float  # |spec entry - bar close| / close; > ENTRY_TOL means the spec entry was replaced
    placebo_r: float  # mean of the placebo entries (same symbol, period, side, fractions, hold, funding); nan if none
    placebo_n: int  # placebo entries in that mean


def placebo_pool(h1: Bars, allowed: Callable[[int], bool]) -> dict[str, np.ndarray]:
    """Indices of the 1h bars whose close is an allowed entry time, per period (``is`` / ``oos`` / ``hidden``).
    ``allowed`` is evaluated once per UTC day of the close (at its 00:00): exact for a monthly universe and period
    bounds at midnight, which is what the runner passes."""
    close = h1.t + h1.bar_ms
    if close.size == 0:
        return {p: np.zeros(0, dtype=np.int64) for p in PERIODS}
    day = close // DAY_MS
    uniq, inv = np.unique(day, return_inverse=True)
    ok = np.array([allowed(int(d) * DAY_MS) for d in uniq], dtype=bool)[inv]
    per = np.where(close < IS_END_MS, 0, np.where(close < HIDDEN_MS, 1, 2))
    return {p: np.flatnonzero(ok & (per == i)) for i, p in enumerate(PERIODS)}


def funding_r(
    funding: tuple[np.ndarray, np.ndarray] | None, entry_ms: int, exit_ms: int, side: str, entry: float, stop: float, fees: Fees
) -> tuple[float, str]:
    """Funding over (entry_ms, exit_ms) in R of the trade (``entry`` / ``stop`` in long space): the venue history when
    it covers the entry, else the stress ``FUND_8H`` per 8 h against the position. ``funding`` None: no funding (0)."""
    if funding is None:
        return 0.0, "none"
    paid, src = funding_paid(funding[0], funding[1], entry_ms, exit_ms, 1 if side == "long" else -1, FUND_8H)
    return paid * abs(entry) / risk_unit(entry, stop, fees), src


def entry_outcome(
    path: Bars, k: int, stop_frac: float, target_frac: float, hold_ms: int, rule: StopRule, fees: Fees = Fees()
) -> tuple[Outcome, float, float] | None:
    """A long on ``path`` (already mirrored for a short) entered at the close of 1h bar ``k``, with the stop and the
    target at the given fractions of the entry price and the same hold. Returns (outcome, entry, stop) or None."""
    e = float(path.c[k])
    a = abs(e)
    st = e - stop_frac * a
    tg = e + target_frac * a if math.isfinite(target_frac) else math.inf
    o = simulate_stop_rule(path, int(path.t[k]) + path.bar_ms, e, st, tg, hold_ms, rule, fees=fees)
    return None if o is None else (o, e, st)


def _stress_extra(e: float, st: float, fees: Fees) -> float:
    """Extra cost in R of ``Fees.scaled(2)`` with a taker entry, on top of the base cost (subtracted with it once
    more): 2 * (maker + out) -> 2 * (taker_exit + out) per unit, i.e. cost + 2 * (taker_exit - maker) * |e| / ru."""
    return 2 * (fees.taker_exit - fees.maker) * abs(e) / risk_unit(e, st, fees)


def simulate_signals(
    h1: Bars,
    signals: Sequence[tuple[int, Signal]],
    management: str,
    allowed: Callable[[int], bool] = lambda t: True,
    rng: np.random.Generator | None = None,
    fees: Fees = Fees(),
    funding: tuple[np.ndarray, np.ndarray] | None = None,
) -> list[Trade]:
    """Trade ``(tf_ms, Signal)`` pairs of one symbol on its 1h bars ``h1``, one position at a time: a signal while the
    previous trade is open (entry < its exit) is skipped. Order: entry time, then timeframe, then side (as the
    stop-management study). Invalid signals, entries ``allowed`` rejects (universe, period), signals whose time is not
    the close of a 1h bar and entries without a path are skipped. The entry is the close of the 1h bar ending at
    ``t_ms``; a spec entry that differs is replaced by it, the stop and target keeping their fractions of the spec
    entry (``Trade.entry_gap`` records it). ``funding`` (times, rates per settlement) charges funding on every trade.

    With ``rng`` every trade gets ``PLACEBO_K`` placebo twins, averaged: the same side, stop and target fractions,
    hold, rule and funding, entered at random allowed 1h closes of the same symbol in the same period
    (``placebo_pool``). Why not the same UTC date as the signal: the date is chosen by the price action that made the
    signal. An entry earlier that day lives through that move (for a sweep, the fall to the sweep low): on S3 dates of
    BTCUSDT such entries lost about 0.2R gross, while random dates gave about 0. And a window right after the signal is
    no placebo for long holds (a shift of hours barely changes a 20-day trade). A random time of the same period asks
    the useful question: does the timing beat being in the market on the same side?"""
    rule = stop_rule(management)
    alt = stop_rule("B" if management == "strategy" else "strategy")
    paths = {"long": h1, "short": h1.mirrored()}
    close_t = h1.t + h1.bar_ms
    pool = placebo_pool(h1, allowed) if rng is not None else {}
    order = sorted(signals, key=lambda x: (x[1].t_ms, x[0], SIDES.index(x[1].side) if x[1].side in SIDES else 9))
    out: list[Trade] = []
    busy = -1
    for tf_ms, sg in order:
        if not sg.valid() or sg.t_ms < busy or not allowed(sg.t_ms):
            continue
        k = int(np.searchsorted(close_t, sg.t_ms))
        if k >= close_t.size or int(close_t[k]) != sg.t_ms:
            continue
        path = paths[sg.side]
        e0, st0, tg0 = sg.long_space()
        stop_frac = (e0 - st0) / abs(e0)
        target_frac = (tg0 - e0) / abs(e0) if math.isfinite(tg0) else math.inf
        bar_close = float(path.c[k])
        gap = abs(e0 - bar_close) / abs(bar_close)
        if gap <= ENTRY_TOL:
            # the spec entry is the close: use its prices as given (no rounding through the fractions)
            o = simulate_stop_rule(path, sg.t_ms, e0, st0, tg0, sg.hold_ms, rule, fees=fees)
            e, st = e0, st0
            o2 = simulate_stop_rule(path, sg.t_ms, e0, st0, tg0, sg.hold_ms, alt, fees=fees) if o is not None else None
        else:
            res = entry_outcome(path, k, stop_frac, target_frac, sg.hold_ms, rule, fees)
            o, e, st = (None, e0, st0) if res is None else res
            res2 = entry_outcome(path, k, stop_frac, target_frac, sg.hold_ms, alt, fees) if o is not None else None
            o2 = None if res2 is None else res2[0]
        if o is None:
            continue
        busy = o.exit_ms
        fr, fsrc = funding_r(funding, sg.t_ms, o.exit_ms, sg.side, e, st, fees)
        r_alt = math.nan
        if o2 is not None:
            r_alt = o2.r_net - funding_r(funding, sg.t_ms, o2.exit_ms, sg.side, e, st, fees)[0]
        period = period_of(sg.t_ms)
        pr, pn = math.nan, 0
        cand = pool.get(period)
        if rng is not None and cand is not None and cand.size:
            vals = []
            for _ in range(PLACEBO_K):
                for _ in range(PLACEBO_TRIES):
                    j = int(cand[rng.integers(cand.size)])
                    res = entry_outcome(path, j, stop_frac, target_frac, sg.hold_ms, rule, fees)
                    if res is not None:
                        po, pe, pst = res
                        vals.append(po.r_net - funding_r(funding, po.entry_ms, po.exit_ms, sg.side, pe, pst, fees)[0])
                        break
            if vals:
                pr, pn = float(np.mean(vals)), len(vals)
        r_net = o.r_net - fr
        out.append(
            Trade(
                t_ms=sg.t_ms,
                day=sg.t_ms // DAY_MS,
                period=period,
                side=sg.side,
                tf_ms=int(tf_ms),
                r_net=r_net,
                cost_r=o.cost_r,
                fund_r=fr,
                fund_src=fsrc,
                exit_kind=o.exit_kind,
                exit_ms=o.exit_ms,
                r_alt=r_alt,
                r_stress=r_net - o.cost_r - _stress_extra(e, st, fees),
                stop_frac=stop_frac,
                target_frac=target_frac,
                entry_gap=gap,
                placebo_r=pr,
                placebo_n=pn,
            )
        )
    return out


# ---------------------------------------------------------------- causality


def stamp_violations(bars: Bars, sigs: Sequence[Signal]) -> list[str]:
    """Signals whose time is not the close of a bar of ``bars`` or whose entry is not that bar's close (relative
    tolerance ``ENTRY_TOL``). Empty = every signal is an entry at a bar close."""
    close_t = bars.t + bars.bar_ms
    bad: list[str] = []
    for s in sigs:
        k = int(np.searchsorted(close_t, s.t_ms))
        if k >= close_t.size or int(close_t[k]) != s.t_ms:
            bad.append(f"signal at {s.t_ms} ({s.side}): not the close time of a {bars.bar_ms // 3_600_000}h bar")
            continue
        c = float(bars.c[k])
        if not math.isfinite(s.entry) or abs(s.entry - c) > ENTRY_TOL * abs(c):
            bad.append(f"signal at {s.t_ms} ({s.side}): entry {s.entry:.10g} is not the bar close {c:.10g}")
    return bad


def _same_signals(a: Sequence[Signal], b: Sequence[Signal]) -> bool:
    """Equal lists up to order and a relative price tolerance (a prefix recomputation may differ in the last bits)."""
    if len(a) != len(b):
        return False

    def close(x: float | None, y: float | None) -> bool:
        if x is None or y is None:
            return x is None and y is None
        return abs(x - y) <= 1e-9 * max(1.0, abs(x), abs(y))

    for s, t in zip(sorted(a, key=_sig_key), sorted(b, key=_sig_key), strict=True):
        if (s.t_ms, s.side, s.hold_ms) != (t.t_ms, t.side, t.hold_ms):
            return False
        if not (close(s.entry, t.entry) and close(s.stop, t.stop) and close(s.target, t.target)):
            return False
    return True


def causality_violations(
    fn: Callable[..., Sequence[Signal]],
    bars: Bars,
    params: Mapping[str, Any] | None = None,
    cuts: Sequence[int] = (),
    *,
    n_sample: int = 200,
    n_last: int = 20,
    seed: int = SEED,
) -> list[str]:
    """Look-ahead checks of a spec on ``bars``; returns a description per violation (empty = none found).

    1. Every signal is stamped with a bar close and enters at that close (``stamp_violations``).
    2. Per signal: for a signal time T (all of them, or the last ``n_last`` plus a random ``n_sample`` of the rest),
       the spec run on the bars closing at or before T must give exactly the signals it gives at T on all bars. This
       catches a decision taken on a later bar and stamped earlier (back-dating) as well as signals that a later bar
       removes (repainting).
    3. At each ``cut``: signals up to the close of bar ``cut - 1`` are the same on all bars and on ``bars[:cut]``."""
    kw = dict(params or {})
    full = list(fn(bars, **kw))
    bad = stamp_violations(bars, full)
    close_t = bars.t + bars.bar_ms
    closes = set(close_t.tolist())
    times = sorted({s.t_ms for s in full if s.t_ms in closes})
    pick = times
    if len(times) > n_sample + n_last:
        head, tail = times[: len(times) - n_last], times[len(times) - n_last :]
        rng = np.random.default_rng(seed)
        pick = sorted(int(x) for x in rng.choice(np.asarray(head, dtype=np.int64), size=n_sample, replace=False)) + tail
    for t in pick:
        k = int(np.searchsorted(close_t, t, side="right"))
        a = [s for s in full if s.t_ms == t]
        b = [s for s in fn(bars.slice(0, k), **kw) if s.t_ms == t]
        if not _same_signals(a, b):
            bad.append(f"signal time {t}: {len(a)} signals with all bars vs {len(b)} with bars closing up to it")
    for cut in cuts:
        if not 0 < cut < len(bars):
            continue
        edge = int(bars.t[cut - 1]) + bars.bar_ms
        a = [s for s in full if s.t_ms <= edge]
        b = [s for s in fn(bars.slice(0, cut), **kw) if s.t_ms <= edge]
        if not _same_signals(a, b):
            bad.append(f"cut at bar {cut} (close {edge}): {len(a)} signals with all bars vs {len(b)} with bars up to the cut")
    return bad


def _sig_key(s: Signal) -> tuple[Any, ...]:
    return (s.t_ms, s.side, s.entry, s.stop, -1.0 if s.target is None else s.target, s.hold_ms)


# ---------------------------------------------------------------- statistics


@dataclass(frozen=True)
class Summary:
    n: int
    dates: int
    mean: float
    sd: float
    lo: float  # day-clustered bootstrap 95 % CI of the mean
    hi: float
    se: float  # SD of the bootstrap means
    p: float  # one-sided p of mean > 0 (normal approximation with the bootstrap SE)
    mde: float  # MDE_Z * se (N_PLAN tests), R; inf when it cannot be estimated
    win: float = math.nan  # share of trades with R net > 0


def p_one_sided(mean: float, se: float) -> float:
    """P(mean <= 0) under a normal with the bootstrap SE; continuous, so it resolves the small p a large registry needs
    (a bootstrap share cannot go below 1 / (n_boot + 1))."""
    if not (math.isfinite(mean) and math.isfinite(se)):
        return math.nan
    if se <= 0:
        return 0.0 if mean > 0 else 1.0
    return 0.5 * math.erfc(mean / se / math.sqrt(2))


def summarize(r: Sequence[float], day: Sequence[int], *, n_boot: int = N_BOOT, seed: int = SEED) -> Summary:
    """Mean R and its day-clustered bootstrap CI, SE, one-sided p and MDE. Non-finite values are dropped."""
    v = np.asarray(r, dtype=float)
    d = np.asarray(day)
    ok = np.isfinite(v)
    v, d = v[ok], d[ok]
    n = int(v.size)
    dates = int(np.unique(d).size)
    if n == 0:
        return Summary(0, 0, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan, math.inf)
    mean = float(v.mean())
    sd = float(v.std())
    win = float(np.mean(v > 0))
    if dates < 2:
        return Summary(n, dates, mean, sd, math.nan, math.nan, math.nan, math.nan, math.inf, win)
    b = cluster_bootstrap_means(v.tolist(), d.tolist(), n_boot=n_boot, seed=seed)
    se = float(b.std())
    return Summary(
        n, dates, mean, sd, float(np.quantile(b, 0.025)), float(np.quantile(b, 0.975)), se, p_one_sided(mean, se), MDE_Z * se, win
    )


def holm_with_registry(p_new: float, prior: Sequence[float], n_plan: int = N_PLAN) -> float:
    """Holm-adjusted p of the new run among every registered run (missing p count as 1), the family padded with p = 1
    up to ``n_plan`` tests: the first run faces the same bar as the hundredth, and the bar keeps rising past it."""
    ps = [p if isinstance(p, float | int) and math.isfinite(p) else 1.0 for p in [*prior, p_new]]
    ps += [1.0] * max(0, n_plan - len(ps))
    return holm(ps)[len(prior)]


def p_threshold(n_runs: int, alpha: float = ALPHA) -> float:
    """The strictest Holm step, alpha / N_eff: what the best p of the registry has to beat."""
    return alpha / n_effective(n_runs)


def _finite_gt(x: float, bound: float) -> bool:
    return math.isfinite(x) and x > bound


def verdict(
    s_is: Summary,
    s_oos: Summary,
    d_is: Summary,
    d_oos: Summary,
    p_holm: float,
    *,
    n_eff: int = N_PLAN,
    min_effect: float = MIN_EFFECT,
    alpha: float = ALPHA,
) -> str:
    """``d_is`` / ``d_oos``: the paired signal - placebo differences.

    ``пройшла``: E[R] IS >= min_effect, CI lo > 0, signal - placebo CI lo > 0 in IS, Holm p < alpha, OOS mean > 0 and
    OOS signal - placebo mean > 0. ``ні (OOS)``: IS met the effect and both CIs but OOS has the opposite sign (of the
    mean or of the difference). ``замало угод``: the IS MDE at the corrected level (``mde_z(n_eff)``) is above
    min_effect and the CI still reaches min_effect. ``ні``: everything else."""
    effect = math.isfinite(s_is.mean) and s_is.mean >= min_effect
    is_ok = s_is.n > 0 and effect and _finite_gt(s_is.lo, 0.0) and _finite_gt(d_is.lo, 0.0)
    oos_ok = s_oos.n > 0 and _finite_gt(s_oos.mean, 0.0) and d_oos.n > 0 and _finite_gt(d_oos.mean, 0.0)
    holm_ok = math.isfinite(p_holm) and p_holm < alpha
    if is_ok and oos_ok and holm_ok:
        return PASS
    if is_ok and not oos_ok:
        return FAIL_OOS
    mde = mde_z(n_eff, alpha) * s_is.se if math.isfinite(s_is.se) else math.inf
    ruled_out = math.isfinite(s_is.hi) and s_is.hi < min_effect
    if mde > min_effect and not ruled_out:
        return UNDERPOWERED
    return FAIL


def revealed_before(rows: Sequence[Mapping[str, Any]], strategy: str, spec_hash: str = "") -> Mapping[str, Any] | None:
    """The registry row in which ``strategy`` already opened the hidden period, if any (one reveal per strategy). A row
    with the same spec content hash counts as the same strategy (a renamed copy does not get a second reveal)."""
    for row in rows:
        if str(row.get("revealed", "")).strip() not in ("1", "True", "true"):
            continue
        if row.get("strategy") == strategy or (spec_hash and row.get("spec_hash") == spec_hash):
            return row
    return None
