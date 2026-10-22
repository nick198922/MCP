"""Stop management for an already-decided trade of the demo trader (IDEA-20261022-demo-stop-management): no move, a
break-even stop after a trigger (a share of the way to the target, a multiple of R, or a price move such as +30 % ROE
at a leverage), or a structural trailing stop under confirmed swing lows of the setup's own timeframe.

Pure functions, no I/O. The trade is a long in price space; a short is passed on negated prices (``Bars.mirrored``),
the swings then come from the mirrored setup bars (a swing low of the negated series is a swing high).

Path conventions (as ``early_exit.simulate_managed``): 1h bars from the entry (the signal-bar close); a bar whose open
is at or beyond the current stop exits at the open (gap), a bar touching the stop exits at the stop, and the stop wins
when the same bar also touches the target. A trigger touched in a bar moves the stop from the next bar (the order inside
a bar is unknown). A swing pivot at setup bar f is known only at the close of bar f + k (``swing_stops``) and moves the
stop from the first 1h bar opening at or after that close. The time exit is the close of the last bar opening before
entry + hold; a data end or a hole longer than ``max_gap_ms`` ends the trade at the last close before it.

Fees are decimal fractions of price per side. The entry and the target are maker limits; every stop (initial, moved)
and the time exit are taker market orders plus slippage. R is the live risk unit of the demo trader: the entry-stop
distance plus the taker fee on both prices (``execution.guard``), so R is a return on the trade's risk, not on capital.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.course_s14_ema import MAX_GAP_MS, Bars

MAKER = 0.0002
TAKER = 0.00055
SLIPPAGE = 0.0002


@dataclass(frozen=True)
class Fees:
    maker: float = MAKER  # entry and target limit, per side
    taker_exit: float = TAKER + SLIPPAGE  # stop / moved stop / time exit, per side
    risk_taker: float = TAKER  # the live risk unit: |entry - stop| + risk_taker * (|entry| + |stop|)
    be_offset: float = 2 * TAKER  # break-even stop = entry * (1 + be_offset) (entry plus both taker fees)

    def scaled(self, k: float) -> Fees:
        """Cost stress: every paid fee and slippage times ``k``; the risk unit and the break-even level stay live."""
        return Fees(self.maker * k, self.taker_exit * k, self.risk_taker, self.be_offset)


@dataclass(frozen=True)
class StopRule:
    name: str
    be_frac: float | None = None  # break-even after a high >= entry + be_frac * (target - entry)
    be_r: float | None = None  # break-even after a high >= entry + be_r * (entry - stop)
    be_move: float | None = None  # break-even after a high >= entry + be_move * |entry| (e.g. 0.30 / leverage)
    trail: bool = False  # structural trailing stop under confirmed swing lows (only tightening)
    use_target: bool = True  # False: no target, exit by a stop or the time exit

    def trigger(self, entry: float, stop: float, target: float) -> float | None:
        if self.be_frac is not None:
            return entry + self.be_frac * (target - entry)
        if self.be_r is not None:
            return entry + self.be_r * (entry - stop)
        if self.be_move is not None:
            return entry + self.be_move * abs(entry)
        return None


@dataclass(frozen=True)
class Swings:
    """Confirmed swing-low stop candidates of one bar series, sorted by ``conf_ms``."""

    pivot_ms: np.ndarray  # open time of the pivot bar
    conf_ms: np.ndarray  # close time of the confirming bar (pivot + k bars): the earliest time the swing is known
    level: np.ndarray  # pivot low minus the buffer

    @classmethod
    def empty(cls) -> Swings:
        z = np.zeros(0, dtype=np.int64)
        return cls(z, z, np.zeros(0))


def swing_stops(b: Bars, atr: np.ndarray, k: int = 2, buf_atr: float = 0.5) -> Swings:
    """Strict fractal lows (lower than ``k`` bars on each side, as ``course_s14_ema.fractal_lows``) of ``b``. The pivot
    at bar f is confirmed at the close of bar f + k; its stop level is low[f] - buf_atr * atr[f + k] (the ATR known at
    the confirmation). Pivots without a finite positive ATR at the confirmation are skipped. Every value depends only on
    bars <= f + k, so the list up to time t does not change when bars after t change."""
    low = b.low
    m = low.size
    if m < 2 * k + 1:
        return Swings.empty()
    core = low[k : m - k]
    ok = np.ones(core.size, dtype=bool)
    for d in range(1, k + 1):
        ok &= low[k - d : m - k - d] > core
        ok &= low[k + d : m - k + d] > core
    f = np.flatnonzero(ok) + k
    a = atr[f + k]
    good = np.isfinite(a) & (a > 0)
    f, a = f[good], a[good]
    return Swings(b.t[f].astype(np.int64), (b.t[f + k] + b.bar_ms).astype(np.int64), low[f] - buf_atr * a)


@dataclass(frozen=True)
class Outcome:
    exit: float
    exit_kind: str  # stop | stop_gap | target | be | be_gap | trail | trail_gap | time | data_end
    moved: bool  # the stop was moved (break-even or trail) before the exit
    move_ms: int  # open time of the first 1h bar with the moved stop active, -1 if never
    r_gross: float
    cost_r: float
    mfe_r: float  # best high before the exit, in R
    entry_ms: int
    exit_ms: int  # close time of the exit bar

    @property
    def r_net(self) -> float:
        return self.r_gross - self.cost_r


def risk_unit(entry: float, stop: float, fees: Fees = Fees()) -> float:
    return (entry - stop) + fees.risk_taker * (abs(entry) + abs(stop))


def simulate_stop_rule(
    path: Bars,
    entry_ms: int,
    entry: float,
    stop: float,
    target: float,
    hold_ms: int,
    rule: StopRule,
    swings: Swings | None = None,
    fees: Fees = Fees(),
    max_gap_ms: int = MAX_GAP_MS,
) -> Outcome | None:
    """Long held from ``entry_ms`` over 1h ``path`` bars opening in [entry_ms, entry_ms + hold_ms) under ``rule``.
    ``swings`` (required for a trailing rule) are the setup-timeframe swing stops; only pivots whose bar opens at or
    after ``entry_ms`` (formed after the signal bar) count. Returns None without a path or with a non-positive risk."""
    if entry - stop <= 0 or len(path) == 0:
        return None
    ru = risk_unit(entry, stop, fees)
    i0 = int(np.searchsorted(path.t, entry_ms))
    i1 = int(np.searchsorted(path.t, entry_ms + hold_ms))
    if i1 <= i0 or int(path.t[i0]) - entry_ms > max_gap_ms:
        return None
    cut = False
    holes = np.flatnonzero(np.diff(path.t[i0:i1]) > max_gap_ms)
    if holes.size:
        i1, cut = i0 + int(holes[0]) + 1, True
    tgt = target if rule.use_target else np.inf
    trig = rule.trigger(entry, stop, target)
    be_stop = entry + fees.be_offset * abs(entry)  # = entry * (1 - offset) of a short in its own (mirrored) price space
    sw = swings if rule.trail else None
    if rule.trail and sw is None:
        raise ValueError("a trailing rule needs swings")
    sj = 0
    if sw is not None:
        sj = int(np.searchsorted(sw.conf_ms, entry_ms, side="right"))  # swings known only after the entry
    cur, how = stop, "stop"
    armed = False  # break-even trigger touched, the move applies from the next bar
    be_done = False
    move_ms = -1
    best = entry
    px, kind, j = float(path.c[i1 - 1]), ("data_end" if cut else "time"), i1 - 1
    for k in range(i0, i1):
        tk = int(path.t[k])
        if armed and not be_done:
            be_done = True
            if be_stop > cur:
                cur, how = be_stop, "be"
                move_ms = tk if move_ms < 0 else move_ms
        if sw is not None:
            while sj < sw.conf_ms.size and sw.conf_ms[sj] <= tk:
                if sw.pivot_ms[sj] >= entry_ms and sw.level[sj] > cur:
                    cur, how = float(sw.level[sj]), "trail"
                    move_ms = tk if move_ms < 0 else move_ms
                sj += 1
        if path.o[k] <= cur:
            px, kind, j = float(path.o[k]), f"{how}_gap", k
            break
        if path.low[k] <= cur:
            px, kind, j = cur, how, k
            break
        if path.h[k] >= tgt:
            px, kind, j = float(tgt), "target", k
            best = max(best, float(tgt))
            break
        best = max(best, float(path.h[k]))
        if trig is not None and not armed and path.h[k] >= trig:
            armed = True
    if kind == "time" and i1 == len(path) and int(path.t[i1 - 1]) + path.bar_ms < entry_ms + hold_ms:
        kind = "data_end"
    fee_out = fees.maker if kind == "target" else fees.taker_exit
    gross = (px - entry) / ru
    cost = (fees.maker * abs(entry) + fee_out * abs(px)) / ru
    return Outcome(px, kind, move_ms >= 0, move_ms, gross, cost, (best - entry) / ru, entry_ms, int(path.t[j]) + path.bar_ms)


# ---------------------------------------------------------------- statistics helpers (pure)


def max_drawdown(r: np.ndarray) -> float:
    """Worst peak-to-trough fall of the cumulative sum of ``r`` (in R), as a non-negative number; the start is a peak."""
    if r.size == 0:
        return 0.0
    eq = np.concatenate([[0.0], np.cumsum(r)])
    return float(np.max(np.maximum.accumulate(eq) - eq))


def cochran_q(means: np.ndarray, ses: np.ndarray) -> tuple[float, int, float]:
    """Cochran's Q of group means with standard errors (inverse-variance weights), its degrees of freedom and I^2."""
    ok = np.isfinite(means) & np.isfinite(ses) & (ses > 0)
    m, s = means[ok], ses[ok]
    if m.size < 2:
        return float("nan"), 0, float("nan")
    w = 1.0 / s**2
    mu = float(np.sum(w * m) / np.sum(w))
    q = float(np.sum(w * (m - mu) ** 2))
    df = int(m.size - 1)
    i2 = max(0.0, (q - df) / q) if q > 0 else 0.0
    return q, df, i2
