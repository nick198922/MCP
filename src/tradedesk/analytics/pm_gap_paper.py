"""Pre-registered paper-position rule and metrics for the Polymarket <-> Deribit gap (IDEA-20261017-polymarket-vs-options-forward).

Pure functions over ``pm_gap_snapshot`` rows (dicts with the table's column names); no I/O, every time is a unix-seconds
parameter. Prices, probabilities, edges and CLV are decimals of the $1 payoff of one share (0.05 = 5 pp). Nothing here
trades: a "position" is a paper record of a snapshot that met the registered rule.

The registration (the card's section "Реєстрація") fixes every number below; changing one is a new, labelled variant.

* Entry: buy one share of a side at its best ask; cost = ask + Polymarket taker fee upper bound
  ``rate * (ask (1 - ask)) ** exponent``. The ask already contains the half-spread (ask = mid + half-spread).
* Net edge of a side = p_win - cost, p_win = the LESS favourable of two readings of the model probability: risk-neutral
  as collected and with implied vol scaled by ``VOL_HAIRCUT_K`` (:func:`vol_scaled_prob`). The options probability is
  risk-neutral (CLAUDE.md invariant 8); this band is a conservative filter, nothing is fitted.
* PRIMARY metric: CLV at +24 h = side mid at the snapshot nearest to entry + 24 h (+-1 h) - entry cost, for positions
  entered >= 26 h before the end. Secondary: CLV at +6 h, CLV to the closing window (end - 2 h ... end - 1 h), P&L.
* Clusters for every confidence interval = ISO week (America/New_York): of the entry for the +6 h / +24 h CLV, of the
  resolution day for the closing CLV, the P&L and the Brier comparison. Resolution day is a sensitivity cluster.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .liquidation_flush import cluster_bootstrap_means
from .options import d1d2, ncdf
from .pm_threshold import PAIRS, standard_rules
from .pm_touch import et_date, fee_per_share
from .probability import prob_touch

HOUR_S = 3600
YEAR_S = 365 * 86_400
Z_POWER = 2.8  # z_{0.975} + z_{0.80}: two-sided 5 %, power 80 %
ASSET_NAME = {"BTC": "bitcoin", "ETH": "ethereum"}
VOL_HAIRCUT_K = 0.92  # conservative filter: implied vol x 0.92 (prior 30-day BTC reports, not fitted here)
BOOT_SEED = 20261018
PRIMARY_H = 24  # hours: the primary CLV horizon
SECONDARY_H = 6  # hours: the secondary short CLV horizon
HORIZON_TOL_S = HOUR_S  # the snapshot for a horizon may be at most 1 h away from entry + horizon
MIN_DAYS = 20  # resolution days needed for any verdict other than 'inconclusive'
MIN_CLUSTERS = 10  # ISO-week clusters needed for any verdict other than 'inconclusive'


@dataclass(frozen=True)
class EntryRule:
    """The registered entry rule. Defaults are the primary variant."""

    threshold: float = 0.05  # net edge after fee and half-spread, decimal of $1
    min_depth_usd: float = 200.0  # USD standing at the best ask of the bought side
    min_ask: float = 0.02
    max_ask: float = 0.98
    min_hours_to_end: float = 2.0  # closing snapshots must also be strictly later than the entry (closing_value)
    max_hours_to_end: float = 14 * 24.0
    # Risk-neutral discount: the side's win probability is the LOWER one over these vol multipliers (1.0 = the risk-
    # neutral probability as collected, VOL_HAIRCUT_K = implied vol scaled to the real-world level of prior reports).
    # A gap must survive both readings. Secondary variants: (1.0,) and (VOL_HAIRCUT_K,).
    vol_ks: tuple[float, ...] = (1.0, VOL_HAIRCUT_K)
    # Book and Deribit surface must be read within this many seconds of each other (applied only when the row carries
    # both ``book_ts`` and ``surface_ts``; older rows without them pass and are counted in the report).
    max_quote_skew_s: float = 60.0


# ----------------------------------------------------------------------------- eligibility


def rules_accepted(description: str | None, asset: str) -> bool:
    """True when the resolution text is one the model can stand for.

    Threshold markets: the standard text (``pm_threshold.standard_rules``) or the wording used since 2026-10-07,
    "Binance 1 minute candle for <PAIR> closing at 12:00 in the ET timezone", with the same strict "Close ... higher than"
    clause. The two differ by one minute of the candle, which does not move a 1-14 day probability. Touch texts are judged
    by the collector (``rules_standard``), so this function is for ``kind='above'`` only.
    """
    name = ASSET_NAME.get(asset, asset)
    if standard_rules(description or "", name):
        return True
    d = (description or "").replace("\n", " ").replace("“", '"').replace("”", '"')
    pair = PAIRS.get(name)
    return bool(
        pair
        and f"Binance 1 minute candle for {pair} closing at 12:00 in the ET timezone" in d
        and '"Close" price higher than the price specified in the title' in d
    )


def accepted_rule_hashes(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """``rules_hash`` values whose text is accepted: ``rules_standard`` = 1, or (threshold markets) :func:`rules_accepted`.

    The full text is stored only when the hash changes, so the decision is taken per hash from any row carrying the text.
    Pass the result explicitly to :func:`eligible` and friends (there is no default: the collector's ``rules_standard``
    alone rejects every current threshold market).
    """
    ok: set[str] = set()
    for r in rows:
        h = r.get("rules_hash")
        if not h:
            continue
        if r.get("rules_standard") == 1:
            ok.add(str(h))
        elif r.get("kind") == "above" and r.get("rules") and rules_accepted(str(r["rules"]), str(r.get("asset"))):
            ok.add(str(h))
    return ok


def resolution_day(end_ts: int) -> str:
    """The America/New_York date of the last second before the market's end (a touch market ending 00:00 ET belongs to
    its last day; a threshold market ending 12:00 ET to its own day). Counts resolution days; sensitivity cluster."""
    return et_date(int(end_ts) - 1).isoformat()


def et_week(ts: int) -> str:
    """ISO week (``YYYY-Www``) of the America/New_York date of ``ts``: the primary cluster key."""
    y, w, _ = et_date(int(ts)).isocalendar()
    return f"{y}-W{w:02d}"


def resolution_week(end_ts: int) -> str:
    """ISO week of :func:`resolution_day`: cluster of the closing CLV, P&L and Brier."""
    return et_week(int(end_ts) - 1)


# ----------------------------------------------------------------------------- the risk-neutral discount


def _g(kind: str, forward: float, strike: float, tau: float, sigma: float) -> float:
    if kind == "above":
        return ncdf(d1d2(forward, strike, tau, sigma)[1])
    return prob_touch(forward, strike, sigma, tau)


def vol_scaled_prob(
    kind: str, p_model: float, iv: float | None, forward: float | None, strike: float, tau_years: float, k: float
) -> float | None:
    """Probability of the event when the volatility is ``k`` x the implied one, everything else kept.

    ``p_k = p_model + g(k sigma) - g(sigma)``, with ``g`` = N(d2) for ``kind='above'`` and the driftless touch formula for
    ``'touch'``, both at the strike's IV ``sigma``. Taking the difference keeps the smile-slope term of ``p_model`` and
    cancels most of the error of using the Deribit index as the forward. ``k = 1`` returns ``p_model`` unchanged. No drift
    adjustment: the real-world drift is not estimable, so it is assumed equal to the forward's. Clipped to [0, 1].
    """
    if k == 1.0:
        return p_model
    if iv is None or forward is None or iv <= 0 or forward <= 0 or strike <= 0 or tau_years <= 0:
        return None
    p = p_model + _g(kind, forward, strike, tau_years, k * iv) - _g(kind, forward, strike, tau_years, iv)
    return float(min(1.0, max(0.0, p)))


def model_prob(row: Mapping[str, Any], k: float = 1.0) -> float | None:
    """The row's model probability of YES under the variant ``k`` (forward = the snapshot's Deribit index ``spot``)."""
    p = row.get("model_prob")
    if p is None:
        return None
    if k == 1.0:
        return float(p)
    tau = (int(row["end_ts"]) - int(row["ts"])) / YEAR_S
    return vol_scaled_prob(str(row["kind"]), float(p), row.get("model_iv"), row.get("spot"), float(row["strike"]), tau, k)


# ----------------------------------------------------------------------------- quotes


def side_book(row: Mapping[str, Any], side: str) -> dict[str, float | None]:
    """Best ask, USD at the best ask and mid of one side. The mid is the side's own book; with one side of that book
    missing it falls back to 1 - the other outcome's mid."""
    o = "no" if side == "yes" else "yes"
    bid, ask = row.get(f"{side}_bid"), row.get(f"{side}_ask")
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    if mid is None:
        ob, oa = row.get(f"{o}_bid"), row.get(f"{o}_ask")
        if ob is not None and oa is not None:
            mid = 1.0 - (ob + oa) / 2.0
    return {"ask": ask, "ask_usd": row.get(f"{side}_ask_usd"), "mid": mid}


def entry_cost(ask: float, fee_rate: float, fee_exp: float) -> float:
    """Paper fill: best ask plus the taker-fee upper bound per share."""
    return ask + fee_per_share(ask, fee_rate, fee_exp)


# ----------------------------------------------------------------------------- positions


def eligible(row: Mapping[str, Any], rule: EntryRule, accepted_hashes: set[str]) -> bool:
    """Snapshot-level filter shared by positions and the Brier comparison: model note 'ok' (not touched / pre-window /
    wrong side / no expiry), strike inside the quoted strikes, accepted resolution text (``accepted_hashes`` from
    :func:`accepted_rule_hashes`, required), book and surface read within ``max_quote_skew_s``, horizon inside the rule."""
    if row.get("model_note") != "ok" or row.get("model_prob") is None or row.get("model_covered") != 1:
        return False
    if str(row.get("rules_hash")) not in accepted_hashes:
        return False
    bt, st = row.get("book_ts"), row.get("surface_ts")
    if bt is not None and st is not None and abs(float(bt) - float(st)) > rule.max_quote_skew_s:
        return False
    h = (int(row["end_ts"]) - int(row["ts"])) / HOUR_S
    return rule.min_hours_to_end <= h <= rule.max_hours_to_end


def entry_signals(row: Mapping[str, Any], rule: EntryRule, accepted_hashes: set[str]) -> list[dict[str, Any]]:
    """Sides of one snapshot that meet the rule (usually none, at most one in practice: both edges summing to at least
    2 x threshold would need a crossed book)."""
    if not eligible(row, rule, accepted_hashes):
        return []
    ps = [model_prob(row, k) for k in rule.vol_ks]
    if not ps or any(p is None for p in ps):
        return []
    p_lo, p_hi = min(ps), max(ps)  # type: ignore[type-var]
    fr, fe = float(row.get("fee_rate") or 0.0), float(row.get("fee_exp") or 1.0)
    out: list[dict[str, Any]] = []
    for side, p_win in (("yes", p_lo), ("no", 1.0 - p_hi)):  # the less favourable reading of each side
        b = side_book(row, side)
        ask, depth = b["ask"], b["ask_usd"]
        if ask is None or not rule.min_ask <= ask <= rule.max_ask or depth is None or depth < rule.min_depth_usd:
            continue
        cost = entry_cost(ask, fr, fe)
        edge = p_win - cost
        if edge >= rule.threshold:
            ts, end_ts = int(row["ts"]), int(row["end_ts"])
            out.append(
                {
                    "market_id": str(row["market_id"]),
                    "side": side,
                    "ts": ts,
                    "end_ts": end_ts,
                    "asset": row.get("asset"),
                    "kind": row.get("kind"),
                    "day": resolution_day(end_ts),
                    "week_entry": et_week(ts),
                    "week_res": resolution_week(end_ts),
                    "p_model": float(row["model_prob"]),
                    "p_win": p_win,
                    "ask": ask,
                    "cost": cost,
                    "mid_entry": b["mid"],
                    "edge": edge,
                    "depth_usd": depth,
                }
            )
    return out


def open_positions(rows: Iterable[Mapping[str, Any]], rule: EntryRule, accepted_hashes: set[str]) -> list[dict[str, Any]]:
    """Paper positions: the FIRST snapshot (by ts) at which a (market, side) meets the rule; one position per market
    and side, held to resolution, one share each. Causal: a position depends only on its own snapshot and on the
    absence of an earlier qualifying one."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for r in sorted(rows, key=lambda x: (int(x["ts"]), str(x["market_id"]))):
        for s in entry_signals(r, rule, accepted_hashes):
            key = (s["market_id"], s["side"])
            if key not in seen:
                seen.add(key)
                out.append(s)
    return out


def delayed_entry(
    pos: Mapping[str, Any],
    market_rows: Sequence[Mapping[str, Any]],
    rule: EntryRule,
    accepted_hashes: set[str],
    max_delay_s: int = 2 * HOUR_S,
) -> dict[str, Any] | None:
    """Stress for stale quotes: re-enter a position at the NEXT snapshot of its market (ts in (entry, entry +
    ``max_delay_s``]) at that snapshot's ask, only if the side still meets the rule there; else None (dropped)."""
    t0 = int(pos["ts"])
    nxt = sorted((r for r in market_rows if t0 < int(r["ts"]) <= t0 + max_delay_s), key=lambda r: int(r["ts"]))
    if not nxt:
        return None
    for s in entry_signals(nxt[0], rule, accepted_hashes):
        if s["side"] == pos["side"]:
            return {**s, "delayed_from": t0}
    return None


# ----------------------------------------------------------------------------- closing line value and P&L


def closing_value(market_rows: Sequence[Mapping[str, Any]], side: str, end_ts: int, entry_ts: int) -> tuple[float | None, str]:
    """Closing line of a side from snapshots strictly after ``entry_ts``: the mid of the latest one in
    [end - 2 h, end - 1 h] ('close'); else the latest in [end - 6 h, end - 1 h] ('late'); else (None, 'missing').
    The settled outcome is never used here (that would put P&L into the CLV)."""
    for lo, src in ((end_ts - 2 * HOUR_S, "close"), (end_ts - 6 * HOUR_S, "late")):
        best: tuple[int, float] | None = None
        for r in market_rows:
            t = int(r["ts"])
            if t > entry_ts and lo <= t <= end_ts - HOUR_S:
                m = side_book(r, side)["mid"]
                if m is not None and (best is None or t > best[0]):
                    best = (t, m)
        if best is not None:
            return best[1], src
    return None, "missing"


def value_after(market_rows: Sequence[Mapping[str, Any]], side: str, t_target: int, tol_s: int = HORIZON_TOL_S) -> float | None:
    """Side mid of the snapshot nearest to ``t_target`` within ``tol_s`` (ties: the earlier one)."""
    best: tuple[int, int, float] | None = None
    for r in market_rows:
        t = int(r["ts"])
        if abs(t - t_target) <= tol_s:
            m = side_book(r, side)["mid"]
            if m is not None:
                key = (abs(t - t_target), t)
                if best is None or key < best[:2]:
                    best = (key[0], key[1], m)
    return None if best is None else best[2]


def horizon_clv(pos: Mapping[str, Any], market_rows: Sequence[Mapping[str, Any]], hours: int) -> tuple[float | None, str]:
    """CLV at a fixed horizon: side mid nearest to entry + ``hours`` (+-1 h) - entry cost.

    Defined only for positions entered >= ``hours`` + 2 h before the end (the horizon point then precedes the closing
    window); else (None, 'n/a'). A defined position without a snapshot near the horizon is (None, 'missing')."""
    ts, end_ts = int(pos["ts"]), int(pos["end_ts"])
    if end_ts - ts < (hours + 2) * HOUR_S:
        return None, "n/a"
    v = value_after(market_rows, str(pos["side"]), ts + hours * HOUR_S)
    return (None, "missing") if v is None else (v - float(pos["cost"]), "ok")


def position_metrics(pos: Mapping[str, Any], market_rows: Sequence[Mapping[str, Any]], outcome_yes: int | None) -> dict[str, Any]:
    """CLV and P&L of one paper position, per share of $1 payoff.

    * ``clv_24h`` (PRIMARY) / ``clv_6h`` (secondary) = :func:`horizon_clv`, with ``src_24h`` / ``src_6h``.
    * ``clv`` (secondary) = closing value - entry cost; ``clv_gross`` = closing value - entry mid; ``close_src`` says where
      the closing value came from (:func:`closing_value`; 'missing' leaves both None).
    * ``pnl`` = payoff (1 if the side won) - entry cost, on the $1 notional of one share; ``ret_capital`` = pnl / cost.
      None without an outcome.
    """
    side, cost, end_ts, ts = str(pos["side"]), float(pos["cost"]), int(pos["end_ts"]), int(pos["ts"])
    close, src = closing_value(market_rows, side, end_ts, ts)
    c24, s24 = horizon_clv(pos, market_rows, PRIMARY_H)
    c6, s6 = horizon_clv(pos, market_rows, SECONDARY_H)
    mid_entry = pos.get("mid_entry")
    won = None if outcome_yes is None else (outcome_yes if side == "yes" else 1 - outcome_yes)
    return {
        **pos,
        "close": close,
        "close_src": src,
        "clv": None if close is None else close - cost,
        "clv_gross": None if close is None or mid_entry is None else close - float(mid_entry),
        "clv_24h": c24,
        "src_24h": s24,
        "clv_6h": c6,
        "src_6h": s6,
        "won": won,
        "pnl": None if won is None else won - cost,
        "ret_capital": None if won is None else (won - cost) / cost,
    }


# ----------------------------------------------------------------------------- Brier: model vs Polymarket


def brier_by_market(
    rows: Iterable[Mapping[str, Any]],
    outcomes: Mapping[str, int],
    rule: EntryRule,
    accepted_hashes: set[str],
    k: float = 1.0,
) -> list[dict[str, Any]]:
    """Per resolved market: mean Brier of the model (vol multiplier ``k``; 1.0 = risk-neutral as collected, the primary)
    and of the Polymarket YES mid over its eligible snapshots (each market weighs 1, whatever its number of snapshots).
    ``diff`` = model - Polymarket (> 0: Polymarket more accurate)."""
    acc: dict[str, dict[str, Any]] = {}
    for r in rows:
        mid_id = str(r["market_id"])
        y = outcomes.get(mid_id)
        if y is None or not eligible(r, rule, accepted_hashes):
            continue
        p = model_prob(r, k)
        q = side_book(r, "yes")["mid"]
        if p is None or q is None:
            continue
        a = acc.setdefault(
            mid_id,
            {
                "market_id": mid_id,
                "day": resolution_day(int(r["end_ts"])),
                "week_res": resolution_week(int(r["end_ts"])),
                "asset": r.get("asset"),
                "kind": r.get("kind"),
                "bm": 0.0,
                "bp": 0.0,
                "n": 0,
            },
        )
        a["bm"] += (p - y) ** 2
        a["bp"] += (q - y) ** 2
        a["n"] += 1
    out = []
    for a in acc.values():
        bm, bp = a["bm"] / a["n"], a["bp"] / a["n"]
        out.append({**a, "bm": bm, "bp": bp, "diff": bm - bp})
    return sorted(out, key=lambda x: (x["day"], x["market_id"]))


# ----------------------------------------------------------------------------- inference and power


def cluster_mean_ci(
    values: Sequence[float],
    clusters: Sequence[Any],
    days: Sequence[Any] | None = None,
    *,
    n_boot: int = 10_000,
    seed: int = BOOT_SEED,
) -> dict[str, Any]:
    """Pooled (position-weighted) mean with a percentile 95 % CI from a bootstrap over whole clusters (ISO weeks).
    ``days`` (resolution days, optional) only adds their count ``n_days`` for the minimum-sample rule of :func:`verdict`."""
    n, k = len(values), len(set(clusters))
    nd = len(set(days)) if days is not None else None
    if n == 0:
        return {"n": 0, "n_clusters": 0, "n_days": nd, "mean": None, "lo": None, "hi": None, "se": None}
    mean = float(np.mean(values))
    if k < 2:
        return {"n": n, "n_clusters": k, "n_days": nd, "mean": mean, "lo": None, "hi": None, "se": None}
    b = cluster_bootstrap_means(values, clusters, n_boot=n_boot, seed=seed)
    return {
        "n": n,
        "n_clusters": k,
        "n_days": nd,
        "mean": mean,
        "lo": float(np.quantile(b, 0.025)),
        "hi": float(np.quantile(b, 0.975)),
        "se": float(np.std(b, ddof=1)),
    }


def mde(sd_cluster: float, n_clusters: int, k: float = Z_POWER) -> float:
    """Minimum detectable mean: k x SD of cluster means / sqrt(number of clusters)."""
    return k * sd_cluster / math.sqrt(n_clusters) if n_clusters > 0 else math.inf


def power_stage0(
    values: Sequence[float], clusters: Sequence[Any], projected_clusters: int, *, n_boot: int = 2_000, seed: int = BOOT_SEED
) -> dict[str, Any]:
    """Power numbers WITHOUT the mean: SD per position, SD of cluster means, positions per cluster, and the MDE of the
    SAME estimator that is tested (position-weighted pooled mean, cluster bootstrap): MDE = 2.8 x bootstrap SE of the
    centred values, scaled by sqrt(clusters now / ``projected_clusters``). The mean is subtracted first and never returned."""
    by: dict[Any, list[float]] = defaultdict(list)
    for v, c in zip(values, clusters, strict=True):
        by[c].append(float(v))
    n, k = len(values), len(by)
    if n < 2 or k < 2:
        return {
            "n": n,
            "n_clusters": k,
            "sd_position": None,
            "sd_cluster": None,
            "per_cluster": None,
            "se": None,
            "mde_now": None,
            "mde_projected": None,
        }
    x = np.asarray(values, dtype=float)
    xc = x - x.mean()
    se = float(np.std(cluster_bootstrap_means(list(xc), list(clusters), n_boot=n_boot, seed=seed), ddof=1))
    return {
        "n": n,
        "n_clusters": k,
        "sd_position": float(np.std(x, ddof=1)),
        "sd_cluster": float(np.std([float(np.mean(v)) for v in by.values()], ddof=1)),
        "per_cluster": n / k,
        "se": se,
        "mde_now": Z_POWER * se,
        "mde_projected": Z_POWER * se * math.sqrt(k / projected_clusters) if projected_clusters > 0 else math.inf,
        "projected_clusters": projected_clusters,
    }


def power_gate(mde_projected: float | None, threshold: float = 0.05, kill_level: float = 0.02) -> str:
    """Descriptive power label: 'full' (MDE <= kill level: both confirm and kill reachable), 'partial'
    (kill level < MDE <= entry threshold), 'underpowered' (MDE > threshold, or not computable). Changes nothing."""
    if mde_projected is None or not math.isfinite(mde_projected) or mde_projected > threshold:
        return "underpowered"
    return "full" if mde_projected <= kill_level else "partial"


def _enough(s: Mapping[str, Any], min_days: int, min_clusters: int) -> bool:
    return (
        s.get("lo") is not None
        and s.get("mean") is not None
        and (s.get("n_days") or 0) >= min_days
        and (s.get("n_clusters") or 0) >= min_clusters
    )


def verdict(
    primary: Mapping[str, Any],
    pnl: Mapping[str, Any],
    brier_diff: Mapping[str, Any],
    *,
    min_days: int = MIN_DAYS,
    min_clusters: int = MIN_CLUSTERS,
    kill_level: float = 0.02,
) -> tuple[str, str]:
    """Registered decision rule, taken ONCE at the final horizon, on dicts from :func:`cluster_mean_ci` (with ``n_days``).

    Every branch needs its own summary to have >= ``min_days`` resolution days and >= ``min_clusters`` week clusters.
    In order:

    1. 'confirmed' when the primary CLV CI > 0, the P&L is known and not significantly negative (CI upper > 0) and the
       Brier comparison is not significantly in Polymarket's favour; if the Brier CI is > 0 instead: 'inconclusive'
       with reason 'conflict_brier';
    2. 'rejected' (reason 'clv') when the primary mean <= 0 and its CI upper < ``kill_level``;
    3. 'rejected' (reason 'brier') when Polymarket is more accurate (Brier diff CI lower > 0) and the CLV is not confirmed;
    4. else 'inconclusive'.

    Returns (verdict, reason)."""
    p_ok = _enough(primary, min_days, min_clusters)
    b_ok = _enough(brier_diff, min_days, min_clusters)
    brier_against = b_ok and brier_diff["lo"] > 0
    if p_ok and primary["lo"] > 0:
        if pnl.get("hi") is None or pnl["hi"] <= 0:
            return "inconclusive", "pnl_contradicts"
        if brier_against:
            return "inconclusive", "conflict_brier"
        return "confirmed", "clv"
    if p_ok and primary["mean"] <= 0 and primary["hi"] < kill_level:
        return "rejected", "clv"
    if brier_against:
        return "rejected", "brier"
    if not p_ok and not b_ok:
        return "inconclusive", "min_sample"
    return "inconclusive", "ci_spans"
