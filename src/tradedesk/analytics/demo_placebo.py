"""Demo trade vs a placebo on the same candle (IDEA-20261030-demo-setups-depth, «S38 / S39 / S40 проти плацебо тієї
самої свічки»): pure helpers, no I/O; times are ms UTC and passed in.

A closed demo trade of a range setup (S38 / S39 short, S40 long) is set against K placebo trades: random top-100 coins
(``s38_modules.draw_coins``, signal coin excluded) entered on the close of the same signal-timeframe candle, on the same
side, with the stop at the same distance in ATR(14, Wilder) as the demo's planned stop, the target at ``target_r`` R,
the same fee model, break-even rule and maximum hold. Δ = demo R − mean placebo R. The demo's own R comes from the
venue (``demo_trade.r_multiple``); the placebo R is simulated (``stop_management.simulate_stop_rule``) on 1h candles.

Causality: the ATR and the placebo entry read only bars that open at or before the signal bar; the outcome reads only
1h bars from the signal-bar close to the exit. Nothing after a trade's exit changes its R.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means
from tradedesk.analytics.stop_management import Fees, Outcome, StopRule, simulate_stop_rule

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
ATR_N = 14
Z80 = 1.96 + 0.84  # MDE at 80 % power, two-sided 5 %

__all__ = [
    "ATR_N",
    "PlaceboOutcome",
    "atr_at",
    "stop_atr_mult",
    "placebo_levels",
    "simulate_side",
    "placebo_outcome",
    "trade_delta",
    "summarize",
    "verdict",
    "TERMINAL",
    "select_signal_window",
    "week_key",
    "t_cdf",
    "t_ppf",
    "cluster_t",
    "verdict_v2",
]

# Demo statuses after which a row never changes again (anything else, e.g. 'open', blocks a signal_ts window).
TERMINAL = frozenset({"closed", "missed", "rejected", "orphaned"})


def atr_at(bars: Bars, signal_ts: int, n: int = ATR_N) -> float | None:
    """Wilder ATR(``n``) at the bar opening at ``signal_ts`` from bars that open at or before it (gaps restart the
    segment, as the live detectors do); None when the bar is absent or the ATR is not yet seeded."""
    j = int(np.searchsorted(bars.t, signal_ts, side="right"))
    if j == 0 or int(bars.t[j - 1]) != signal_ts:
        return None
    b = bars.slice(0, j)
    a = atr(b, n, segment_pos(b.t))
    v = float(a[-1]) if a.size else math.nan
    return v if math.isfinite(v) and v > 0 else None


def stop_atr_mult(entry: float, stop: float, atr_value: float) -> float | None:
    """|entry − stop| in ATR units; None for a non-positive ATR or distance."""
    d = abs(entry - stop)
    if not (atr_value > 0) or not (d > 0) or not math.isfinite(d / atr_value):
        return None
    return d / atr_value


def placebo_levels(side: str, entry: float, k_atr: float, atr_value: float, target_r: float = 2.0) -> tuple[float, float, float]:
    """(entry, stop, target) of a placebo: stop ``k_atr`` ATR away against the side, target ``target_r`` R in favour."""
    d = k_atr * atr_value
    if side == "long":
        return entry, entry - d, entry + target_r * d
    if side == "short":
        return entry, entry + d, entry - target_r * d
    raise ValueError(f"side must be 'long' or 'short', got {side!r}")


def simulate_side(
    side: str,
    path: Bars,
    entry_ms: int,
    entry: float,
    stop: float,
    target: float,
    hold_ms: int,
    rule: StopRule,
    now_ms: int,
    fees: Fees = Fees(),
    max_gap_ms: int = HOUR_MS,
) -> Outcome | None:
    """``simulate_stop_rule`` in real prices (a short runs on mirrored bars) over 1h bars closed by ``now_ms``."""
    keep = path.t + path.bar_ms <= now_ms
    p = Bars(path.t[keep], path.o[keep], path.h[keep], path.low[keep], path.c[keep], path.bar_ms)
    if side == "long":
        return simulate_stop_rule(p, entry_ms, entry, stop, target, hold_ms, rule, fees=fees, max_gap_ms=max_gap_ms)
    return simulate_stop_rule(p.mirrored(), entry_ms, -entry, -stop, -target, hold_ms, rule, fees=fees, max_gap_ms=max_gap_ms)


@dataclass(frozen=True)
class PlaceboOutcome:
    r: float | None  # net R (None: missing)
    status: str  # ok | open (horizon not passed by now: marked at the last closed 1h bar) | no_bar | no_atr | no_path | gap
    exit_kind: str | None = None


def placebo_outcome(
    side: str,
    bars_tf: Bars,
    path_1h: Bars,
    signal_ts: int,
    k_atr: float,
    hold_ms: int,
    rule: StopRule,
    now_ms: int,
    *,
    target_r: float = 2.0,
    fees: Fees = Fees(),
) -> PlaceboOutcome:
    """R of one placebo coin: entry on the close of its bar opening at ``signal_ts`` (``bars_tf``), stop ``k_atr`` of
    its own ATR, target ``target_r`` R, path ``path_1h`` from the bar close. A path that ends before the horizon
    while the horizon has passed (a hole or a delisting) is ``gap`` (missing); one whose horizon is still ahead of
    ``now_ms`` is ``open`` and keeps its mark-to-market R."""
    j = int(np.searchsorted(bars_tf.t, signal_ts, side="right"))
    if j == 0 or int(bars_tf.t[j - 1]) != signal_ts or int(bars_tf.t[j - 1]) + bars_tf.bar_ms > now_ms:
        return PlaceboOutcome(None, "no_bar")
    a = atr_at(bars_tf, signal_ts)
    if a is None:
        return PlaceboOutcome(None, "no_atr")
    entry_ms = signal_ts + bars_tf.bar_ms
    e, st, tg = placebo_levels(side, float(bars_tf.c[j - 1]), k_atr, a, target_r)
    out = simulate_side(side, path_1h, entry_ms, e, st, tg, hold_ms, rule, now_ms, fees)
    if out is None:
        return PlaceboOutcome(None, "no_path")
    if out.exit_kind == "data_end":
        if entry_ms + hold_ms <= now_ms:
            return PlaceboOutcome(None, "gap", out.exit_kind)
        return PlaceboOutcome(out.r_net, "open", out.exit_kind)
    return PlaceboOutcome(out.r_net, "ok", out.exit_kind)


def trade_delta(r_demo: float | None, placebo_r: Sequence[float | None]) -> tuple[float | None, int]:
    """(demo R − mean of the available placebo R, number of placebos used); None without the demo R or any placebo."""
    got = [float(x) for x in placebo_r if x is not None and math.isfinite(float(x))]
    if r_demo is None or not math.isfinite(float(r_demo)) or not got:
        return None, len(got)
    return float(r_demo) - sum(got) / len(got), len(got)


def summarize(values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261030) -> dict[str, Any]:
    """n, clusters, mean, SD, cluster-bootstrap (by ``clusters``, e.g. the signal day) 95 % CI and SE, MDE80 = 2.8 SE,
    one-sided bootstrap p for mean > 0 and for mean < 0. Statistics are None with fewer than 2 clusters."""
    x = np.asarray(values, dtype=float)
    n, k = int(x.size), len(set(clusters))
    out: dict[str, Any] = {"n": n, "clusters": k, "mean": float(x.mean()) if n else None, "sd": float(x.std(ddof=1)) if n > 1 else None}
    out.update(ci=None, se=None, mde80=None, p_gt0=None, p_lt0=None)
    if n and k >= 2:
        b = cluster_bootstrap_means(list(x), list(clusters), n_boot=n_boot, seed=seed)
        se = float(np.std(b, ddof=1))
        out.update(
            ci=[float(np.quantile(b, 0.025)), float(np.quantile(b, 0.975))],
            se=se,
            mde80=Z80 * se,
            p_gt0=float((np.sum(b <= 0) + 1) / (b.size + 1)),
            p_lt0=float((np.sum(b >= 0) + 1) / (b.size + 1)),
        )
    return out


def verdict(s: dict[str, Any], *, min_n: int = 30, mde_max: float = 0.30, band: float = 0.30, p_holm: float | None = None) -> str:
    """Registered rule. n < ``min_n`` -> 'descriptive'; MDE80 > ``mde_max`` (or no CI) -> '◯'; CI lower bound > 0 and
    (when given) the Holm-adjusted one-sided p < 0.05 -> '✓'; CI upper bound < 0 -> '✗'; CI inside (−band, +band) ->
    '≈'; otherwise '◯'."""
    if s.get("n", 0) < min_n:
        return "descriptive"
    ci, mde = s.get("ci"), s.get("mde80")
    if ci is None or mde is None or mde > mde_max:
        return "◯"
    lo, hi = ci
    if lo > 0 and (p_holm is None or p_holm < 0.05):
        return "✓"
    if hi < 0:
        return "✗"
    if -band < lo and hi < band:
        return "≈"
    return "◯"


# ---------------------------------------------------------------- protocol v2 (S39 re-run, registration 2026-11-25)


def select_signal_window(
    rows: Sequence[dict[str, Any]], setups: Sequence[str], signal_before: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the rows of ``setups`` signalled before ``signal_before`` (ms UTC) into (closed with a ``closed_ts``,
    other terminal rows, blocking rows: not terminal, or closed without ``closed_ts``). The window is usable only with no
    blocking rows; then the selection depends on rows signalled before the boundary only, never on later ones."""
    closed, other, block = [], [], []
    for r in rows:
        if r.get("setup") not in setups or int(r["signal_ts"]) >= signal_before:
            continue
        st = str(r.get("status"))
        if st == "closed" and r.get("closed_ts") is not None:
            closed.append(r)
        elif st in TERMINAL and st != "closed":
            other.append(r)
        else:
            block.append(r)
    return closed, other, block


GOLD_BASES = frozenset({"XAUT", "PAXG"})  # tokenised gold: its beta is not the crypto market factor of the mechanism


def is_gold(symbol: str) -> bool:
    """True for a tokenised-gold symbol ('XAUT/USDT:USDT', 'PAXG', ...), by its base asset."""
    return symbol.split("/")[0].upper() in GOLD_BASES


def placebo_horizon_end(rows: Sequence[dict[str, Any]], tf_ms: dict[str, int], hold: Any) -> int | None:
    """Latest placebo horizon over ``rows``: max(signal_ts + bar length + ``hold(setup, timeframe)``) in ms, None for no
    rows. Before it, some placebo of the window is still ``open`` (marked to market)."""
    ends = [int(r["signal_ts"]) + tf_ms[r["timeframe"]] + int(hold(r["setup"], r["timeframe"])) for r in rows]
    return max(ends) if ends else None


def week_key(ts_ms: int) -> int:
    """ISO-week bucket (Monday 00:00 … Sunday 24:00 UTC) of a ms timestamp: 1970-01-01 was a Thursday."""
    return (int(ts_ms) // DAY_MS + 3) // 7


def t_cdf(t: float, df: int) -> float:
    """Exact Student-t CDF for an integer ``df`` >= 1 (Abramowitz & Stegun 26.7.3-4, finite series)."""
    if df < 1:
        raise ValueError("df must be >= 1")
    th = math.atan(abs(t) / math.sqrt(df))
    s, c2 = math.sin(th), math.cos(th) ** 2
    if df % 2 == 1:
        acc, term = 0.0, math.cos(th)
        for j in range(1, df - 1, 2):  # powers 1, 3, …, df - 2
            acc += term
            term *= (j + 1) / (j + 2) * c2
        a = 2 / math.pi * (th + s * acc) if df > 1 else 2 * th / math.pi
    else:
        acc, term = 0.0, 1.0
        for j in range(0, df - 1, 2):  # powers 0, 2, …, df - 2
            acc += term
            term *= (j + 1) / (j + 2) * c2
        a = s * acc
    return 0.5 + 0.5 * a if t >= 0 else 0.5 - 0.5 * a


def t_ppf(p: float, df: int) -> float:
    """Student-t quantile by bisection on :func:`t_cdf` (|error| < 1e-9)."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    if p < 0.5:
        return -t_ppf(1 - p, df)
    lo, hi = 0.0, 1.0
    while t_cdf(hi, df) < p:
        hi *= 2
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if t_cdf(mid, df) < p else (lo, mid)
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2


def cluster_t(values: Sequence[float], clusters: Sequence[Any]) -> dict[str, Any]:
    """Cluster-robust (CR1) inference on the mean: SE² = G/(G−1) · Σ_g (Σ_{i∈g} (x_i − x̄))² / n², t with G − 1
    degrees of freedom: 95 % CI, one-sided p for mean > 0, MDE80 = (t_{0.975} + t_{0.80}) · SE. None below 2 clusters."""
    x = [float(v) for v in values]
    n = len(x)
    groups: dict[Any, float] = {}
    out: dict[str, Any] = {"n": n, "clusters": len(set(clusters)), "mean": sum(x) / n if n else None}
    out.update(se=None, df=None, ci=None, p_gt0=None, mde80=None)
    g = out["clusters"]
    if n == 0 or g < 2:
        return out
    mu = out["mean"]
    for v, c in zip(x, clusters, strict=True):
        groups[c] = groups.get(c, 0.0) + (v - mu)
    se = math.sqrt(g / (g - 1) * sum(s * s for s in groups.values())) / n
    df = g - 1
    q = t_ppf(0.975, df)
    p = (1 - t_cdf(mu / se, df)) if se > 0 else (0.0 if mu > 0 else 1.0)
    out.update(se=se, df=df, ci=[mu - q * se, mu + q * se], p_gt0=p, mde80=(q + t_ppf(0.80, df)) * se)
    return out


def verdict_v2(
    boot: dict[str, Any],
    ct: dict[str, Any],
    *,
    min_n: int = 30,
    min_clusters: int = 10,
    mde_max: float = 0.30,
    band: float = 0.30,
    p_holm: float | None,
) -> str:
    """Rule of the 2026-11-25 registration, on the day-clustered bootstrap ``boot`` (:func:`summarize`) and the
    day-clustered CR1 t ``ct`` (:func:`cluster_t`) of the same values. n < ``min_n`` -> 'descriptive'; fewer than
    ``min_clusters`` clusters, a missing CI, or max(MDE80 of both) > ``mde_max`` -> '◯'; both lower bounds > 0 and the
    Holm-adjusted p < 0.05 -> '✓'; both upper bounds < 0 -> '✗'; both CIs inside (−band, +band) -> '≈'; otherwise '◯'.
    ``p_holm`` is required: None (the setup was not in the Holm family) never gives '✓' (amendment 2026-11-24)."""
    if boot.get("n", 0) < min_n:
        return "descriptive"
    if boot.get("clusters", 0) < min_clusters or boot.get("ci") is None or ct.get("ci") is None:
        return "◯"
    if max(boot["mde80"], ct["mde80"]) > mde_max:
        return "◯"
    (bl, bh), (tl, th) = boot["ci"], ct["ci"]
    if bl > 0 and tl > 0 and p_holm is not None and p_holm < 0.05:
        return "✓"
    if bh < 0 and th < 0:
        return "✗"
    if -band < min(bl, tl) and max(bh, th) < band:
        return "≈"
    return "◯"
