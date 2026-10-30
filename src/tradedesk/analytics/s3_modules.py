"""S3 modules (IDEA-20261030-demo-setups-depth, item 7b): pure helpers for the registered modules H10 (partial exit),
H9 (split entry), H1 (touches of the swept edge), H2 (higher-timeframe level at the edge) and H5 (open-interest change
on the sweep bar). Registration: the card's section «Попередня реєстрація модулів S3».

Everything works in the long price space (a short is simulated on mirrored bars, as ``simulate_stop_rule`` does). No
I/O; times are parameters (ms UTC). Every feature of a signal depends only on bars that close at or before the signal
bar's decision (H1: bars before the sweep bar; H2: periods closed by the sweep bar's open; H5: archive observations at
or before the bar's close), which the tests check by changing later data.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.setup_passport import limit_trade
from tradedesk.analytics.stop_management import Fees, Outcome, StopRule, risk_unit, simulate_stop_rule

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
OI_MAX_AGE_MS = 15 * 60_000  # an archive observation older than this at the stamp counts as missing


def unit_draw(key: str, salt: str) -> float:
    """Deterministic U(0, 1) draw per trade key (placebo depths and targets): stable across runs and Python versions."""
    h = hashlib.sha256(f"{salt}|{key}".encode()).digest()
    return (int.from_bytes(h[:8], "big") + 0.5) / 2.0**64


# ---------------------------------------------------------------- H10: partial exit


def runner_rule(entry: float, mid: float, far: float, name: str = "H10") -> StopRule:
    """Rule B of the base (break-even after half the way to ``mid``) expressed for a trade whose target is ``far``."""
    return StopRule(name, be_frac=0.5 * (mid - entry) / (far - entry))


def partial_exit(
    path: Bars, t_in: int, entry: float, stop: float, mid: float, far: float, hold_ms: int, fees: Fees, base: Outcome
) -> tuple[float, Outcome | None]:
    """H10: half of the position is the base trade ``base`` (target ``mid``); the other half lives like the base until
    ``mid`` is touched (same stop, same break-even trigger), then keeps a break-even stop (with fees, from the next bar)
    and targets ``far`` (> mid), same hold. Returns (R of the whole position in the base risk unit, runner outcome)."""
    if not (stop < entry < mid < far):
        raise ValueError("need stop < entry < mid < far")
    run = simulate_stop_rule(path, t_in, entry, stop, far, hold_ms, runner_rule(entry, mid, far), fees=fees)
    if run is None:
        return math.nan, None
    return 0.5 * base.r_net + 0.5 * run.r_net, run


# ---------------------------------------------------------------- H9: split entry


def split_level(entry: float, extreme: float, depth: float) -> float:
    """Limit level ``depth`` of the way from the entry (signal close) down to the sweep extreme (0.5: middle of the
    wick between close and extreme)."""
    return entry - depth * (entry - extreme)


def split_entry(
    path: Bars,
    t_in: int,
    entry: float,
    stop: float,
    target: float,
    level: float,
    rest_ms: int,
    hold_ms: int,
    fees: Fees,
    base: Outcome,
    rule: StopRule,
) -> tuple[float, bool, float]:
    """H9: half of the quantity is the base trade; the other half is a buy limit at ``level`` (stop < level < entry)
    resting from ``t_in`` for ``rest_ms`` but not past the close of the base's exit bar (that bar included:
    pessimistic), filled only through the level (``setup_passport.limit_trade``: a fill bar reaching the stop loses).
    Same quantity: the second half's R is rescaled to the base risk unit; unfilled = 0R. Returns (R of the position,
    filled, R of the second half in the base unit)."""
    if not (stop < level < entry):
        raise ValueError("need stop < level < entry")
    t1 = min(t_in + rest_ms, base.exit_ms)
    out, tk = limit_trade(path, t_in, t1, level, stop, target, hold_ms, rule, fees, True)
    if tk < 0 or out is None:
        return 0.5 * base.r_net, tk >= 0, 0.0
    r2 = out.r_net * risk_unit(level, stop, fees) / risk_unit(entry, stop, fees)
    return 0.5 * base.r_net + 0.5 * r2, True, r2


# ---------------------------------------------------------------- H1: touches of the swept edge


def edge_touches(low: np.ndarray, s: int, w: int, atr_prev: float, tol_atr: float = 0.25, k: int = 2) -> int:
    """Strict fractal lows (lower than ``k`` bars on each side) inside the range window ``[s - w, s - 1]`` whose
    confirming bars end before the sweep bar ``s``, with the low within ``tol_atr * atr_prev`` of the window's low (the
    range edge). Reads ``low[: s]`` only."""
    if s - w < 0 or not (np.isfinite(atr_prev) and atr_prev > 0):
        return 0
    edge = float(np.min(low[s - w : s]))
    n = 0
    for j in range(max(s - w, k), s - k):
        v = low[j]
        if all(low[j - d] > v and low[j + d] > v for d in range(1, k + 1)) and v - edge <= tol_atr * atr_prev:
            n += 1
    return n


# ---------------------------------------------------------------- H2: higher-timeframe levels


def _period_starts(ref_ms: int) -> dict[str, tuple[int, int]]:
    """[start, end) of the previous UTC day, ISO week and calendar month closed at or before ``ref_ms``."""
    d = datetime.fromtimestamp(ref_ms / 1000, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    day0 = d
    week0 = d - timedelta(days=d.weekday())
    month0 = d.replace(day=1)
    pm = (month0 - timedelta(days=1)).replace(day=1)

    def ms(x: datetime) -> int:
        return int(x.timestamp() * 1000)

    return {
        "day": (ms(day0 - timedelta(days=1)), ms(day0)),
        "week": (ms(week0 - timedelta(days=7)), ms(week0)),
        "month": (ms(pm), ms(month0)),
    }


def htf_lows(t: np.ndarray, low: np.ndarray, ref_ms: int, bar_ms: int = HOUR_MS, min_cover: float = 0.5) -> dict[str, float]:
    """Lows of the previous closed UTC day, ISO week (Monday) and calendar month before ``ref_ms`` from 1h bars
    (``t`` open times, sorted); NaN where fewer than ``min_cover`` of the period's bars exist. Reads bars opening
    before the period ends, so nothing at or after the start of ``ref_ms``'s day."""
    out: dict[str, float] = {}
    for name, (a, b) in _period_starts(ref_ms).items():
        i0, i1 = int(np.searchsorted(t, a)), int(np.searchsorted(t, b))
        need = (b - a) / bar_ms
        out[name] = float(np.min(low[i0:i1])) if i1 > i0 and (i1 - i0) >= min_cover * need else math.nan
    return out


def htf_match(edge: float, levels: dict[str, float], atr_prev: float, tol_atr: float = 0.3) -> tuple[bool, float]:
    """(any level within ``tol_atr * atr_prev`` of the edge, distance to the nearest level in ATR; NaN if none)."""
    ds = [abs(edge - v) for v in levels.values() if np.isfinite(v)]
    if not ds or not (np.isfinite(atr_prev) and atr_prev > 0):
        return False, math.nan
    d = min(ds) / atr_prev
    return d <= tol_atr, d


FRESH, NEAR, FAR = "fresh", "near", "far"


def edge_class(d_atr: float | None, tol_atr: float = 0.3, eps: float = 1e-9) -> str | None:
    """Item 10a (registration «Попередня реєстрація 10a»): the swept edge *is* the previous day / week / month
    extreme (``FRESH``: distance < ``eps``), a level lies within ``tol_atr`` ATR (``NEAR``), or every level is farther
    (``FAR``); None when the distance is undefined (no level or ATR). ``d_atr`` is ``htf_match``'s distance (rounded
    to 4 decimals in the runners), so ``FRESH`` means the edge equals a level to 5e-5 ATR."""
    if d_atr is None or not math.isfinite(d_atr):
        return None
    if d_atr < eps:
        return FRESH
    return NEAR if d_atr <= tol_atr else FAR


# ---------------------------------------------------------------- H5: open-interest change on the sweep bar


def oi_at(obs_ms: np.ndarray, oi: np.ndarray, t: int, max_age_ms: int = OI_MAX_AGE_MS) -> float:
    """OI of the last observation at or before ``t`` (``obs_ms`` sorted observation times), NaN if it is older than
    ``max_age_ms`` or there is none."""
    j = int(np.searchsorted(obs_ms, t, side="right")) - 1
    if j < 0 or t - int(obs_ms[j]) > max_age_ms or not (np.isfinite(oi[j]) and oi[j] > 0):
        return math.nan
    return float(oi[j])


def oi_change(obs_ms: np.ndarray, oi: np.ndarray, t_open: int, t_close: int, max_age_ms: int = OI_MAX_AGE_MS) -> float:
    """OI(close) / OI(open) - 1 of one bar, NaN without both observations."""
    a, b = oi_at(obs_ms, oi, t_open, max_age_ms), oi_at(obs_ms, oi, t_close, max_age_ms)
    return b / a - 1 if np.isfinite(a) and np.isfinite(b) else math.nan


def quintile_edges(x: np.ndarray) -> np.ndarray:
    """The 20/40/60/80th percentiles of the finite values of ``x`` (the IS reference)."""
    v = x[np.isfinite(x)]
    return np.quantile(v, [0.2, 0.4, 0.6, 0.8]) if v.size else np.full(4, math.nan)


def quintile_of(x: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """0 .. 4 by ``edges`` (bottom quintile 0), -1 where ``x`` is not finite."""
    q = np.searchsorted(edges, x, side="right").astype(np.int64)
    q[~np.isfinite(x)] = -1
    return q


# ---------------------------------------------------------------- statistics


def one_sided_p(m: float, p_two: float) -> float:
    """One-sided p for H1: mean > 0 from a two-sided bootstrap p."""
    if not (np.isfinite(m) and np.isfinite(p_two)):
        return math.nan
    return p_two / 2 if m > 0 else 1 - p_two / 2


def subgroup_placebo(values: np.ndarray, flag: np.ndarray, n_draw: int = 2000, seed: int = 20261030) -> tuple[float, np.ndarray]:
    """Observed mean(values | flag) - mean(values) and that difference for ``n_draw`` random subgroups of the same
    size (without replacement)."""
    v = np.asarray(values, dtype=float)
    f = np.asarray(flag, dtype=bool)
    k = int(f.sum())
    if k == 0 or k == v.size:
        return math.nan, np.zeros(0)
    mu = float(v.mean())
    obs = float(v[f].mean()) - mu
    rng = np.random.default_rng(seed)
    pl = np.array([v[rng.choice(v.size, k, replace=False)].mean() - mu for _ in range(n_draw)])
    return obs, pl


def weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    w = np.asarray(weights, dtype=float)
    return float(np.sum(w * np.asarray(values, dtype=float)) / np.sum(w)) if np.sum(w) > 0 else math.nan
