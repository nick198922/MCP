"""Descriptive breakdown of a paper carry book's leg-side costs and engine reliability (pure: no I/O, time is a parameter).

Criterion (b) of the paper carry registration (IDEA-20261017-forward-paper-carry) compares the mean actual cost of an
executed leg side (taker fee + |VWAP - mid| / mid on the recorded book) with the mean modelled one (taker + 0.0005),
over sides whose recorded depth covered the slot notional (``carry_report.cost_realism``). This module splits that
same ratio by coin, leg, event (open / close) and size, and splits the reliability criterion (d) (share of cycles on
time and with complete data) by the engine's flags. It changes no criterion: everything here is descriptive.

Inputs are ``cp_log`` / ``cp_cycle`` rows as dicts with ``cycle_ts`` (UTC ms) and a parsed ``payload``; every function
keeps only rows with ``cycle_ts <= as_of_ms``. Costs are decimal fractions of leg notional (0.001 = 0.1 %).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .carry_costs import decompose_side

COST_RATIO_MAX = 1.5  # registered threshold of (b)
SIZE_MULTIPLIERS = (0.0, 0.25, 0.5, 1.0, 2.0)  # re-walk notional = multiplier x recorded slot notional
TOP_K = 2  # "a few coins": the pooled ratio without the TOP_K coins with the largest excess cost
PAYMENT_SLACK_MS = 60_000  # as carry_rules.funding_gap_issue: a payment up to 1 min before the scheduled time counts


def coin_of(payload: Mapping[str, Any]) -> str:
    """Base coin of a side (from the perp symbol of the position, e.g. 'WIF/USDT:USDT' -> 'WIF')."""
    sym = str(payload.get("perp_symbol") or payload.get("symbol") or "?")
    return sym.split("/")[0]


def side_records(rows: Iterable[Mapping[str, Any]], *, as_of_ms: int) -> list[dict[str, Any]]:
    """One flat record per recorded leg side at or before ``as_of_ms``.

    ``actual_cost`` / ``model_cost`` / ``depth_short`` are the engine's own values (the ones (b) uses); the split into
    fee, half spread and impact comes from ``carry_costs.decompose_side`` on the same recorded book.
    """
    out = []
    for r in rows:
        if int(r["cycle_ts"]) > as_of_ms:
            continue
        p = r.get("payload") or {}
        d = decompose_side(dict(p)) if p.get("book") else None
        actual = p.get("actual_cost")
        out.append(
            {
                "cycle_ts": int(r["cycle_ts"]),
                "trade_id": str(p.get("trade_id") or r.get("trade_id")),
                "coin": coin_of(p),
                "leg": str(p.get("leg")),
                "event": str(p.get("event")),
                "notional_usd": float(p.get("notional_usd") or 0.0),
                "has_book": actual is not None,
                "actual_cost": None if actual is None else float(actual),
                "model_cost": float(p["model_cost"]) if p.get("model_cost") is not None else None,
                "depth_short": bool(p.get("depth_short")),
                "spread_bps": p.get("spread_bps"),
                "fee": None if d is None else d["fee"],
                "half_spread": None if d is None else d["half_spread"],
                "impact": None if d is None else d["impact"],
                "payload": p,
            }
        )
    return out


def _full(recs: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Sides that enter the ratio of (b): a book, a modelled cost, depth covering the notional."""
    return [x for x in recs if x["has_book"] and x["model_cost"] and not x["depth_short"]]


def _mean(xs: Sequence[float]) -> float | None:
    return math.fsum(xs) / len(xs) if xs else None


def cost_block(recs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Ratio of means (as in ``cost_realism``) and the mean components over the full-depth sides of ``recs``."""
    full = _full(recs)
    actual = _mean([x["actual_cost"] for x in full])
    model = _mean([x["model_cost"] for x in full])
    comp = {k: _mean([x[k] for x in full if x[k] is not None]) for k in ("fee", "half_spread", "impact")}
    spreads = [float(x["spread_bps"]) for x in full if x["spread_bps"] is not None]
    return {
        "sides": len(recs),
        "with_book": sum(1 for x in recs if x["has_book"]),
        "n": len(full),
        "depth_short": sum(1 for x in recs if x["has_book"] and x["depth_short"]),
        "mean_actual": actual,
        "mean_model": model,
        "ratio": actual / model if actual is not None and model else None,
        "excess_sum": math.fsum(x["actual_cost"] - x["model_cost"] for x in full),
        "mean_spread_bps": _mean(spreads),
        **{f"mean_{k}": v for k, v in comp.items()},
    }


def by_group(recs: Sequence[Mapping[str, Any]], key: Callable[[Mapping[str, Any]], Any]) -> dict[Any, dict[str, Any]]:
    """``cost_block`` per group of ``key(record)``, groups in sorted order."""
    groups: dict[Any, list[Mapping[str, Any]]] = {}
    for x in recs:
        groups.setdefault(key(x), []).append(x)
    return {g: cost_block(groups[g]) for g in sorted(groups, key=str)}


def concentration(recs: Sequence[Mapping[str, Any]], *, top_k: int = TOP_K, min_sides: int = 2) -> dict[str, Any]:
    """Is the excess cost a few coins or all of them?

    Coins ranked by their summed excess (actual - model) over full-depth sides. Returns the pooled ratio, the ratio
    without the top 1..``top_k`` coins, the leave-one-coin-out ratios, the share of the total excess in the top
    ``top_k``, and how many coins with >= ``min_sides`` full-depth sides have a ratio above ``COST_RATIO_MAX``.
    """
    per = by_group(recs, lambda x: x["coin"])
    ranked = sorted((c for c in per if per[c]["n"]), key=lambda c: (-per[c]["excess_sum"], c))
    total_excess = math.fsum(per[c]["excess_sum"] for c in ranked)
    without = {}
    for k in range(1, top_k + 1):
        drop = set(ranked[:k])
        without[k] = cost_block([x for x in recs if x["coin"] not in drop])["ratio"]
    loo = {c: cost_block([x for x in recs if x["coin"] != c])["ratio"] for c in ranked}
    eligible = [c for c in ranked if per[c]["n"] >= min_sides]
    above = [c for c in eligible if (per[c]["ratio"] or 0.0) > COST_RATIO_MAX]
    return {
        "pooled": cost_block(recs)["ratio"],
        "ranked": ranked,
        "top_excess_share": math.fsum(per[c]["excess_sum"] for c in ranked[:top_k]) / total_excess if total_excess > 0 else None,
        "without_top": without,
        "leave_one_out": loo,
        "eligible": len(eligible),
        "above_threshold": len(above),
    }


def concentration_reading(conc: Mapping[str, Any], *, top_k: int = TOP_K) -> str:
    """Pre-registered reading (2026-11-10): 'few_coins' if the ratio without the top ``top_k`` coins is <= 1.5;
    'broad' if it stays > 1.5 and more than half of the eligible coins are above 1.5; else 'mixed'."""
    r = (conc.get("without_top") or {}).get(top_k)
    if r is None:
        return "unknown"
    if r <= COST_RATIO_MAX:
        return "few_coins"
    if conc.get("eligible") and conc["above_threshold"] > conc["eligible"] / 2:
        return "broad"
    return "mixed"


def size_curve(recs: Sequence[Mapping[str, Any]], multipliers: Sequence[float] = SIZE_MULTIPLIERS) -> list[dict[str, Any]]:
    """Ratio of means when every side with a book is re-walked on ``m`` x its recorded notional.

    ``m = 0`` is the top of the book (fee + half spread). Sides whose recorded depth does not cover the re-walked
    notional are counted in ``depth_short`` and left out of the ratio, as in (b). The model cost does not depend on size.
    """
    out = []
    for m in multipliers:
        act, mod, short = [], [], 0
        for x in recs:
            if not x["has_book"] or not x["model_cost"]:
                continue
            if m == 0:
                if x["half_spread"] is None:
                    continue
                cost, dshort = x["fee"] + x["half_spread"], False
            else:
                d = decompose_side(dict(x["payload"]), notional_usd=m * x["notional_usd"])
                if d is None:
                    continue
                cost, dshort = d["actual_cost"], d["depth_short"]
            if dshort:
                short += 1
                continue
            act.append(cost)
            mod.append(x["model_cost"])
        a, mo = _mean(act), _mean(mod)
        out.append(
            {"multiplier": m, "n": len(act), "depth_short": short, "mean_actual": a, "ratio": a / mo if a is not None and mo else None}
        )
    return out


def _between_ss(values: Sequence[float], labels: Sequence[str]) -> float:
    grand = math.fsum(values) / len(values)
    groups: dict[str, list[float]] = {}
    for v, lab in zip(values, labels, strict=True):
        groups.setdefault(lab, []).append(v)
    return math.fsum(len(g) * (math.fsum(g) / len(g) - grand) ** 2 for g in groups.values())


def coin_permutation(recs: Sequence[Mapping[str, Any]], *, n_perm: int = 10_000, seed: int = 20261110) -> dict[str, Any]:
    """Heterogeneity of the per-side ratio actual / model across coins: the between-coin sum of squares against a
    permutation of coin labels within leg x event strata (a coin keeps its leg / event mix). One-sided p."""
    full = _full(recs)
    if len({x["coin"] for x in full}) < 2:
        return {"n": len(full), "coins": len({x["coin"] for x in full}), "p": None}
    vals = [x["actual_cost"] / x["model_cost"] for x in full]
    labels = [x["coin"] for x in full]
    strata: dict[tuple[str, str], list[int]] = {}
    for i, x in enumerate(full):
        strata.setdefault((x["leg"], x["event"]), []).append(i)
    obs = _between_ss(vals, labels)
    rng = random.Random(seed)
    ge = 0
    for _ in range(n_perm):
        perm = list(labels)
        for idx in strata.values():
            lab = [labels[i] for i in idx]
            rng.shuffle(lab)
            for i, lab_i in zip(idx, lab, strict=True):
                perm[i] = lab_i
        if _between_ss(vals, perm) >= obs - 1e-15:
            ge += 1
    return {"n": len(full), "coins": len(set(labels)), "between_ss": obs, "p": (1 + ge) / (1 + n_perm), "n_perm": n_perm}


def time_split(recs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Ratio of the full-depth sides before / from the median cycle (by side count), and the persistence of coins:
    coins with full-depth sides in both halves; with >= 5 of them, the top quarter by first-half per-side ratio
    against the rest in the second half (else ``persistence`` None)."""
    full = sorted(_full(recs), key=lambda x: x["cycle_ts"])
    if len(full) < 2:
        return {"cut_ts": None, "first": cost_block(full), "second": cost_block([]), "persistence": None}
    cut = full[len(full) // 2]["cycle_ts"]
    first = [x for x in full if x["cycle_ts"] < cut]
    second = [x for x in full if x["cycle_ts"] >= cut]
    both = sorted({x["coin"] for x in first} & {x["coin"] for x in second})
    pers = None
    if len(both) >= 5:
        r1 = by_group([x for x in first if x["coin"] in both], lambda x: x["coin"])
        ranked = sorted(both, key=lambda c: (-(r1[c]["ratio"] or 0.0), c))
        top = set(ranked[: math.ceil(len(both) / 4)])
        pers = {
            "coins": len(both),
            "top": sorted(top),
            "top_second_ratio": cost_block([x for x in second if x["coin"] in top])["ratio"],
            "rest_second_ratio": cost_block([x for x in second if x["coin"] in set(both) - top])["ratio"],
        }
    return {"cut_ts": cut, "first": cost_block(first), "second": cost_block(second), "coins_both": len(both), "persistence": pers}


# ---------------------------------------------------------------------------------------------------- reliability (d)


def _flag_key(f: Mapping[str, Any]) -> str:
    return str(f.get("issue")) + (f" / {f['detail']}" if isinstance(f.get("detail"), str) else "")


def reliability(cycles: Iterable[Mapping[str, Any]], *, as_of_ms: int) -> dict[str, Any]:
    """Criterion (d) split by the engine's flags: the share of cycles on time and complete (as ``checkpoint``), the
    incomplete cycles by issue / detail, by coin and by UTC day, and for each issue / detail the share (d) would have
    without it (descriptive, not a criterion: the registered rule counts every flag)."""
    cyc = [c for c in cycles if int(c["cycle_ts"]) <= as_of_ms]
    done = [c for c in cyc if c.get("status") == "done"]
    on_time = [c for c in done if (c.get("payload") or {}).get("on_time")]
    good = [c for c in on_time if (c.get("payload") or {}).get("complete")]
    by_issue: dict[str, int] = {}
    by_coin: dict[str, dict[str, int]] = {}
    by_day: dict[str, int] = {}
    flag_rows: list[dict[str, Any]] = []
    incomplete_keys: list[set[str]] = []
    for c in done:
        p = c.get("payload") or {}
        if p.get("complete"):
            continue
        flags = [f for f in p.get("flags") or [] if isinstance(f, Mapping)]
        keys = {_flag_key(f) for f in flags} or {"?"}
        if p.get("on_time"):
            incomplete_keys.append(keys)
        for k in keys:
            by_issue[k] = by_issue.get(k, 0) + 1
        day = datetime.fromtimestamp(int(c["cycle_ts"]) / 1000, UTC).strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0) + 1
        seen: set[tuple[str, str]] = set()
        for f in flags:
            coin = str(f.get("symbol") or "-").split("/")[0]
            k = _flag_key(f)
            flag_rows.append({"cycle_ts": int(c["cycle_ts"]), "symbol": f.get("symbol"), "coin": coin, "key": k})
            if (coin, k) not in seen:
                seen.add((coin, k))
                by_coin.setdefault(coin, {})[k] = by_coin.setdefault(coin, {}).get(k, 0) + 1
    n = len(cyc)
    without = {
        k: (len(good) + sum(1 for ks in incomplete_keys if ks == {k})) / n if n else None for k in sorted(by_issue)
    }  # cycles whose ONLY flag is k would count as complete
    return {
        "cycles": n,
        "done": len(done),
        "on_time": len(on_time),
        "good": len(good),
        "share_good": len(good) / n if n else None,
        "share_on_time": len(on_time) / n if n else None,
        "incomplete_by_issue": dict(sorted(by_issue.items())),
        "incomplete_by_coin": {c: dict(sorted(v.items())) for c, v in sorted(by_coin.items())},
        "incomplete_by_day": dict(sorted(by_day.items())),
        "share_good_without": without,
        "flags": flag_rows,
    }


def payment_published(
    flags: Iterable[Mapping[str, Any]], funding_ts: Mapping[str, Sequence[int]], *, interval_ms: int = 8 * 3_600_000
) -> list[dict[str, Any]]:
    """For each flag 'scheduled_payment_missing' (a payment due at the cycle time absent from the history then): was a
    payment with ``cycle_ts - 1 min <= ts < cycle_ts + interval_ms`` stored later (``funding_ts``: symbol -> stored payment
    times from the engine's cache)? 'published_later' / 'never_stored'; plus the median gap between the symbol's stored
    payments in the 3 days before the cycle (its funding interval then, hours; None with < 2 payments)."""
    out = []
    for f in flags:
        if "scheduled_payment_missing" not in str(f.get("key")):
            continue
        t = int(f["cycle_ts"])
        ts = sorted(funding_ts.get(str(f.get("symbol")), ()))
        hit = any(t - PAYMENT_SLACK_MS <= x < t + interval_ms for x in ts)
        prior = [x for x in ts if t - 3 * 86_400_000 <= x < t]
        gaps = sorted(b - a for a, b in zip(prior, prior[1:], strict=False))
        med = gaps[len(gaps) // 2] / 3_600_000 if gaps else None
        out.append({**f, "status": "published_later" if hit else "never_stored", "interval_h_before": med})
    return out
