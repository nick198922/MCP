"""S40 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b; registration «Попередня реєстрація модулів S40»).

Pure functions, no I/O, real prices:

* ``failed_short`` - the "failed short" detector: a parent short (signal bar r, its stop and target) whose stop is
  closed through upwards within ``max_wait`` bars of the same segment before the parent's target is touched gives a
  long at that close (stop under the lowest low since r minus ``stop_atr`` ATR14, target ``target_r`` R). The mirror
  of ``s39_modules.failed_long``; with the S3 shorts of ``setup_signals.range_all`` as parents it is exactly
  ``setup_signals.s40_all`` (H3 runs it with the S14 / S16 / S23 shorts, H4 with ``max_wait`` 12).
* ``hourly_returns`` / ``aligned`` / ``rolling_beta`` - H1: the beta of a coin on BTC from closed 1h bars before entry.
* ``window_return`` / ``funding_sum`` / ``residual_r`` / ``hedge_cost_r`` - H1: the trade's R net of beta x BTC over
  the holding time, and the cost of an actual BTC short hedge in the trade's R units.
* ``same_coin_times`` - H3's same-coin placebo: random 4h closes of the same coin around the signal.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import numpy as np

from tradedesk.analytics import s39_modules as m9
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars
from tradedesk.analytics.stop_management import Fees, risk_unit

DAY_MS = 24 * HOUR_MS
BETA_WINDOW_MS = 60 * DAY_MS
BETA_MIN_OBS = 720
HEDGE_FEE_SIDE = 0.00055 + 0.0002  # taker + slippage per side of the BTC hedge (registered)

Parent = m9.Parent
Fail = m9.Fail


def failed_short(
    b: Bars,
    pos: np.ndarray,
    atr: np.ndarray,
    parents: Sequence[Parent],
    *,
    max_wait: int = 6,
    stop_atr: float = 0.5,
    target_r: float = 2.0,
    used: set[int] | None = None,
) -> list[Fail]:
    """Longs on the failure of each parent short (real prices): ``s39_modules.failed_long`` on the mirrored bars with
    the parents' levels negated. For bars j = r + 1 .. r + max_wait of the parent's segment the first close above the
    parent's stop is the failure (one long per bar j; a bar in ``used`` - updated in place - gives none); a low at or
    below the parent's target first ends the search. Causal: the long on bar j reads bars r .. j only."""
    m = b.mirrored()
    mp = [Parent(p.r, -p.stop, -p.target, p.tag) for p in parents]
    out = m9.failed_long(m, pos, atr, mp, max_wait=max_wait, stop_atr=stop_atr, target_r=target_r, used=used)
    return [Fail(f.r, f.j, -f.entry, -f.stop, -f.target, f.atr, f.tag) for f in out]


def hourly_returns(t: np.ndarray, c: np.ndarray, bar_ms: int = HOUR_MS) -> np.ndarray:
    """Simple return of each bar over the previous one; NaN for the first bar and after a gap."""
    r = np.full(c.size, np.nan)
    if c.size > 1:
        ok = np.diff(t) == bar_ms
        r[1:] = np.where(ok, c[1:] / c[:-1] - 1.0, np.nan)
    return r


def aligned(t_src: np.ndarray, v_src: np.ndarray, t_dst: np.ndarray) -> np.ndarray:
    """``v_src`` at the times ``t_dst`` (NaN where ``t_src`` has no such time); ``t_src`` ascending."""
    out = np.full(t_dst.size, np.nan)
    if t_src.size == 0:
        return out
    i = np.clip(np.searchsorted(t_src, t_dst), 0, t_src.size - 1)
    hit = t_src[i] == t_dst
    out[hit] = v_src[i[hit]]
    return out


def rolling_beta(
    t: np.ndarray,
    rc: np.ndarray,
    rb: np.ndarray,
    t_in: int,
    *,
    bar_ms: int = HOUR_MS,
    window_ms: int = BETA_WINDOW_MS,
    min_obs: int = BETA_MIN_OBS,
) -> float:
    """OLS slope of the coin's bar returns ``rc`` on BTC's ``rb`` (aligned on the opens ``t``) over the bars that
    closed in (t_in - window_ms, t_in]; NaN with fewer than ``min_obs`` pairs. Reads nothing that closes after t_in."""
    i1 = int(np.searchsorted(t, t_in - bar_ms, side="right"))
    i0 = int(np.searchsorted(t, t_in - window_ms - bar_ms, side="right"))
    x, y = rb[i0:i1], rc[i0:i1]
    ok = np.isfinite(x) & np.isfinite(y)
    if int(ok.sum()) < min_obs:
        return float("nan")
    x, y = x[ok], y[ok]
    vx = float(np.mean((x - x.mean()) ** 2))
    if vx <= 0:
        return float("nan")
    return float(np.mean((x - x.mean()) * (y - y.mean())) / vx)


def close_at(t: np.ndarray, c: np.ndarray, close_ms: int, bar_ms: int = HOUR_MS) -> float:
    """Close of the bar that ends exactly at ``close_ms`` (NaN if there is none)."""
    i = int(np.searchsorted(t, close_ms - bar_ms))
    return float(c[i]) if i < t.size and int(t[i]) == close_ms - bar_ms else float("nan")


def window_return(t: np.ndarray, c: np.ndarray, t0: int, t1: int, bar_ms: int = HOUR_MS) -> float:
    """Simple return from the close of the bar ending at t0 to the close of the bar ending at t1."""
    a, b = close_at(t, c, t0, bar_ms), close_at(t, c, t1, bar_ms)
    return b / a - 1.0 if np.isfinite(a) and np.isfinite(b) and a > 0 else float("nan")


def funding_sum(t_f: np.ndarray, r_f: np.ndarray, t0: int, t1: int) -> float:
    """Sum of the funding rates settled in (t0, t1]."""
    i0 = int(np.searchsorted(t_f, t0, side="right"))
    i1 = int(np.searchsorted(t_f, t1, side="right"))
    return float(np.sum(r_f[i0:i1]))


def residual_r(r_net: float, beta: float, btc_ret: float, entry: float, stop: float, fees: Fees = Fees()) -> float:
    """H1: the long's R minus beta x the BTC return over the hold, in the trade's risk units."""
    return r_net - beta * btc_ret * abs(entry) / risk_unit(entry, stop, fees)


def hedge_cost_r(
    beta: float,
    entry: float,
    stop: float,
    funding: float,
    *,
    fee_side: float = HEDGE_FEE_SIDE,
    mult: float = 1.0,
    fees: Fees = Fees(),
) -> float:
    """H1 as a rule: the cost (in R) of a BTC short of beta x the position notional - two taker sides with slippage
    (x ``mult``) minus the funding the short receives (``funding`` = the sum of rates over the hold)."""
    return (abs(beta) * 2.0 * fee_side * mult - beta * funding) * abs(entry) / risk_unit(entry, stop, fees)


def same_coin_times(
    key: str, t_in: int, k: int = 3, *, step_ms: int = 4 * HOUR_MS, window_ms: int = 30 * DAY_MS, min_gap_ms: int = 3 * DAY_MS
) -> list[int]:
    """``k`` distinct step-aligned times within ``window_ms`` of ``t_in`` but at least ``min_gap_ms`` away, by a
    deterministic hash of the trade key (H3's same-coin placebo; stable across runs and Python versions)."""
    base = (t_in // step_ms) * step_ms
    n = window_ms // step_ms
    gap = min_gap_ms // step_ms
    cand = [base + i * step_ms for i in range(-n, n + 1) if abs(i) >= gap]
    out: list[int] = []
    salt = 0
    while len(out) < min(k, len(cand)):
        h = hashlib.sha256(f"same|{key}|{salt}".encode()).digest()
        c = cand[int.from_bytes(h[:8], "big") % len(cand)]
        if c not in out:
            out.append(c)
        salt += 1
    return out
