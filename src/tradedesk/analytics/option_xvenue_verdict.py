"""Registered 30/90-day analysis of the Deribit <-> Bybit daily option crossing (forward card, measurement only).

Docs: docs/ideas/IDEA-20261023-bybit-deribit-daily-option-dislocation-forward.md, sections «Реєстрація (до збору)» and
«Реєстрація 2 (до збору)» (both committed 23.10.2026, before collection). The per-row measure is the registered
:func:`option_xvenue.adjusted_crossing_from_row`; this module only turns rows into snapshot counts, the frequency p with
its day-bootstrap / Wilson intervals, the kill blockers, hourly persistence, episode lifetimes (Kaplan-Meier) and the
paper journal. Pure functions: no I/O; the decision cut-off is a parameter (:func:`decision_cutoff`), never "now".

Units: fractions of the Deribit index (0.0015 = 0.15 %), USD per 1 coin of underlying, times in unix seconds / ms as
named. Journal results are on notional (per 1 BTC), not on capital: the margin on two venues is unknown.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import Any

import numpy as np

from .option_xvenue import (
    FEE_RATE,
    MIN_DEPTH,
    REG_STRIKES_EACH_SIDE,
    REG_SYNC_LOOSE_MS,
    REG_SYNC_MS,
    REG_THRESHOLD,
    adjusted_crossing_from_row,
    atm_strikes,
    num,
    registered_rows,
)

SIDES = ("b_over_d", "d_over_b")
BYBIT_FEE_CAP = 0.07  # official Bybit cap: fee <= 7 % of the option price (card «Специфікація Bybit»)
MAX_ABS_GAP = 0.01  # |b_index / d_index - 1| > 1 % = "index anomaly", outside the main count
BOOT_SEED = 20261023
BOOT_REPS = 10_000
DENOM_MIN_30D = 300
DENOM_MIN_90D = 900
KILL_P_30D = 0.01
PASS_P = 0.05
EPISODE_GAP_S = 150  # a gap between a currency's snapshots above this = unobserved minutes
EPISODE_MAX_UNOBS_S = 300  # > 5 min unobserved between the last crossing and the first clear -> right-censored


def decision_cutoff(first_ts: int, days: int = 30) -> int:
    """End (exclusive, unix s) of the ``days``-day decision sample counted from the first stored row."""
    return int(first_ts) + int(days) * 86_400


def sync_class(row: Mapping[str, Any], measure: str = "quote") -> str | None:
    """Sync class of a row. ``quote`` (primary): |b_ts_ms - d_ts_ms| <= 5 s 'A', <= 60 s 'B', else / unknown 'C'.
    ``recv`` (sensitivity A_r): the same on |b_recv_ms - d_recv_ms|; None when either receive time is NULL (cache or no
    leg), such rows are outside the A_r count and never substituted."""
    keys = ("d_ts_ms", "b_ts_ms") if measure == "quote" else ("d_recv_ms", "b_recv_ms")
    if measure not in ("quote", "recv"):
        raise ValueError(f"unknown sync measure {measure!r}")
    d, b = row.get(keys[0]), row.get(keys[1])
    if d is None or b is None:
        return "C" if measure == "quote" else None
    dt = abs(int(b) - int(d))
    return "A" if dt <= REG_SYNC_MS else "B" if dt <= REG_SYNC_LOOSE_MS else "C"


def abs_dt_ms(row: Mapping[str, Any], measure: str = "quote") -> int | None:
    keys = ("d_ts_ms", "b_ts_ms") if measure == "quote" else ("d_recv_ms", "b_recv_ms")
    d, b = row.get(keys[0]), row.get(keys[1])
    return None if d is None or b is None else abs(int(b) - int(d))


def index_gap(row: Mapping[str, Any]) -> float | None:
    """b_index / d_index - 1, None when either is missing."""
    d, b = num(row.get("d_index")), num(row.get("b_index"))
    return None if d is None or b is None else b / d - 1.0


def row_measure(
    row: Mapping[str, Any],
    *,
    gap_source: str = "index",
    fee_deribit: float = FEE_RATE,
    fee_bybit: float = FEE_RATE,
    bybit_cap: float | None = None,
    min_depth: float = MIN_DEPTH,
    threshold: float = REG_THRESHOLD,
    max_abs_gap: float = MAX_ABS_GAP,
) -> dict[str, Any] | None:
    """One row by the registered measure. None = unmeasurable (incl. an index anomaly |g| > ``max_abs_gap``).

    Otherwise ``{"hit", "sides": {side: {"net_frac", "depth", "depth_ok", "hit"} | None}, "best", "shift_usd"}``.
    ``bybit_cap`` (e.g. 0.07): the Bybit fee per leg becomes min(fee_bybit x b_index, cap x raw Bybit touch price of
    that side: bid for b_over_d, ask for d_over_b), USDT taken as USD; the Deribit fee stays fee_deribit x d_index."""
    g = index_gap(row)
    if g is not None and abs(g) > max_abs_gap:
        return None
    a = adjusted_crossing_from_row(
        dict(row),
        gap_source=gap_source,
        fee_deribit=fee_deribit,
        fee_bybit=0.0 if bybit_cap is not None else fee_bybit,
        min_depth=min_depth,
    )
    if a is None:
        return None
    spot = float(num(row.get("d_index")))  # adjusted_crossing_from_row returned, so d_index is valid
    sides: dict[str, dict[str, Any] | None] = {}
    for side in SIDES:
        s = a[side]
        if s is None:
            sides[side] = None
            continue
        net_frac = s["net_frac"]
        if bybit_cap is not None:
            raw = num(row.get("b_bid" if side == "b_over_d" else "b_ask"))
            b_index = num(row.get("b_index")) or spot
            fee_b = min(fee_bybit * b_index, bybit_cap * raw) if raw is not None else fee_bybit * b_index
            net_frac = s["net_usd"] / spot - fee_b / spot
        hit = bool(s["depth_ok"] and net_frac >= threshold)
        sides[side] = {"net_frac": net_frac, "depth": s["depth"], "depth_ok": s["depth_ok"], "hit": hit}
    ok = [v["net_frac"] for v in sides.values() if v is not None and v["depth_ok"]]
    return {
        "hit": any(v is not None and v["hit"] for v in sides.values()),
        "sides": sides,
        "best": max(ok) if ok else None,
        "shift_usd": a["shift_usd"],
    }


def snapshot_outcome(
    rows: Iterable[Mapping[str, Any]], eligible: Callable[[Mapping[str, Any]], bool], measure: Callable[[Mapping[str, Any]], dict | None]
) -> tuple[bool, bool]:
    """«Від рядка до знімка»: (in the denominator, with a crossing). In the denominator when >= 1 eligible row is
    measurable; with a crossing when >= 1 eligible measurable row hits. Non-eligible rows do not count at all."""
    in_denom = hit = False
    for r in rows:
        if not eligible(r):
            continue
        m = measure(r)
        if m is None:
            continue
        in_denom = True
        hit = hit or m["hit"]
    return in_denom, hit


def decision_rows(rows: Iterable[Mapping[str, Any]], cutoff: int) -> list[Mapping[str, Any]]:
    """Rows of the decision sample: snapshot time ``ts`` strictly before ``cutoff`` (:func:`decision_cutoff`)."""
    return [r for r in rows if int(r["ts"]) < int(cutoff)]


def snapshot_outcomes(
    rows: Iterable[Mapping[str, Any]],
    cutoff: int,
    eligible: Callable[[Mapping[str, Any]], bool],
    measure: Callable[[Mapping[str, Any]], dict | None],
    *,
    strikes: str = "reg",
) -> list[tuple[int, Any, bool, bool]]:
    """``(ts, expiry day, in denominator, crossing)`` per hourly snapshot of ONE currency before ``cutoff``, by
    :func:`snapshot_outcome`. ``strikes``: "reg" = the registered ATM +- 1 (:func:`option_xvenue.registered_rows`),
    "all" = every stored row of the snapshot (descriptive). Rows at/after the cut-off and minute rows never count."""
    if strikes not in ("reg", "all"):
        raise ValueError(f"unknown strike set {strikes!r}")
    snaps: dict[int, list[Mapping[str, Any]]] = {}
    for r in decision_rows(rows, cutoff):
        if r.get("mode", "hourly") == "hourly":
            snaps.setdefault(int(r["ts"]), []).append(r)
    out = []
    for t in sorted(snaps):
        rs = registered_rows(snaps[t]) if strikes == "reg" else snaps[t]
        d, h = snapshot_outcome(rs, eligible, measure)
        out.append((t, snaps[t][0]["expiry_ts"], d, h))
    return out


def snapshot_leg_gaps(rows: Iterable[Mapping[str, Any]], each_side: int = REG_STRIKES_EACH_SIDE) -> dict[str, int]:
    """Leg completeness of ONE currency's snapshot (data integrity, not part of the registered measure).

    ``d_missing`` / ``b_missing``: rows without a Deribit instrument / a Bybit symbol (a row stored with one venue's leg
    only). ``d_missing_atm``: of the missing Deribit legs, those in ATM +- ``each_side`` around the Bybit forward
    (median ``b_underlying`` of the rows with a Bybit leg; the Deribit forward when no Bybit forward is stored), i.e.
    where the registered ATM +- 1 would have looked had the Deribit legs been there."""
    rows = list(rows)
    d_miss = [r for r in rows if not r.get("d_instrument")]
    b_miss = [r for r in rows if not r.get("b_symbol")]
    fwd = [x for x in (num(r.get("b_underlying")) for r in rows if r.get("b_symbol")) if x is not None]
    if not fwd:
        fwd = [x for x in (num(r.get("d_underlying")) for r in rows if r.get("d_instrument")) if x is not None]
    ks = [float(r["strike"]) for r in rows if r.get("strike") is not None]
    atm = set(atm_strikes(ks, statistics.median(fwd), each_side)) if fwd and ks else set()
    d_atm = sum(1 for r in d_miss if r.get("strike") is not None and float(r["strike"]) in atm)
    return {"rows": len(rows), "d_missing": len(d_miss), "b_missing": len(b_miss), "d_missing_atm": d_atm}


def leg_gap_bounds(
    outcomes: Sequence[tuple[Any, Any, bool, bool]], affected: Collection[Any], *, reps: int = BOOT_REPS, seed: int = BOOT_SEED
) -> dict[str, Any]:
    """Sensitivity of p to snapshots with missing legs (added after the critic's review 25.11.2026, not registered).

    ``outcomes``: ``(snapshot key, day, in_denom, hit)`` (:func:`snapshot_outcomes`); ``affected``: keys of the snapshots
    with an incomplete leg. ``without``: :func:`frequency` over the other snapshots. ``worst``: the registered
    denominator unchanged, every affected snapshot that is in it counted as a crossing (an upper bound on what the
    missing legs could have hidden; affected snapshots outside the denominator stay out). ``affected_in_denom``: how
    many affected snapshots the registered denominator contains."""
    aff = set(affected)
    without = frequency([(day, d, h) for key, day, d, h in outcomes if key not in aff], reps=reps, seed=seed)
    n = sum(1 for _, _, d, _ in outcomes if d)
    k = sum(1 for key, _, d, h in outcomes if d and (h or key in aff))
    in_denom = sum(1 for key, _, d, _ in outcomes if d and key in aff)
    return {"without": without, "worst": {"n": n, "k": k, "p": k / n if n else None}, "affected_in_denom": in_denom}


def wilson(p: float, n: float, z: float = 1.959964) -> tuple[float, float] | None:
    """Wilson score interval for an observed share ``p`` on an (effective) sample size ``n``."""
    if n <= 0:
        return None
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(max(p * (1 - p), 0.0) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def frequency(outcomes: Sequence[tuple[Any, bool, bool]], *, reps: int = BOOT_REPS, seed: int = BOOT_SEED) -> dict[str, Any]:
    """p = snapshots with a crossing / snapshots in the denominator, from ``(day, in_denom, hit)`` per snapshot.
    95 % CI: bootstrap over expiry days (resample days with replacement, ratio of sums), and Wilson with n = days."""
    days: dict[Any, list[int]] = {}
    for day, d, h in outcomes:
        c = days.setdefault(day, [0, 0])
        if d:
            c[0] += 1
            c[1] += int(bool(h))
    n = sum(c[0] for c in days.values())
    k = sum(c[1] for c in days.values())
    used = [c for c in days.values() if c[0] > 0]
    out: dict[str, Any] = {"n": n, "k": k, "p": k / n if n else None, "days": len(used), "boot": None, "wilson_days": None}
    if not n:
        return out
    nd = np.array([c[0] for c in used], dtype=float)
    kd = np.array([c[1] for c in used], dtype=float)
    idx = np.random.default_rng(seed).integers(0, len(used), size=(reps, len(used)))
    ps = kd[idx].sum(axis=1) / nd[idx].sum(axis=1)
    out["boot"] = (float(np.percentile(ps, 2.5)), float(np.percentile(ps, 97.5)))
    out["wilson_days"] = wilson(k / n, len(used))
    return out


def verdict_30d(
    main: Mapping[str, Any], fee06: Mapping[str, Any], fwd_g: Mapping[str, Any], recv: Mapping[str, Any], cap: Mapping[str, Any]
) -> dict[str, Any]:
    """30-day branch of «Вирок» with the kill blockers of «Реєстрація 2», п. 1. Each argument is a :func:`frequency`
    result (main = class A, primary fees; fee06 = Bybit 0.06 %; fwd_g = forward gap; recv = A_r; cap = class A with
    the Bybit 7 % cap). Returns ``{"status", "branch", "reason", "blockers"}``; status is the card status to set."""
    if main["n"] < DENOM_MIN_30D:
        return {
            "status": "inconclusive",
            "branch": "collect_to_90d",
            "reason": f"denominator {main['n']} < {DENOM_MIN_30D}",
            "blockers": [],
        }
    p = main["p"]
    if p < KILL_P_30D:
        blockers = []
        if recv["n"] < DENOM_MIN_30D:
            blockers.append({"name": "A_r", "blocks": False, "note": f"мало даних: n = {recv['n']} < {DENOM_MIN_30D}"})
        else:
            blockers.append({"name": "A_r", "blocks": recv["p"] >= KILL_P_30D, "note": f"p = {recv['p']:.4f}, n = {recv['n']}"})
        blockers.append(
            {"name": "cap7", "blocks": cap["p"] is not None and cap["p"] >= KILL_P_30D, "note": f"p = {cap['p']:.4f}, n = {cap['n']}"}
        )
        hit = [b["name"] for b in blockers if b["blocks"]]
        if hit:
            return {
                "status": "inconclusive",
                "branch": "collect_to_90d",
                "reason": "kill blocked by " + ", ".join(hit),
                "blockers": blockers,
            }
        return {
            "status": "rejected",
            "branch": "kill",
            "reason": f"p = {p:.4f} < {KILL_P_30D} and no blocker reaches it",
            "blockers": blockers,
        }
    if all(x["p"] is not None and x["p"] >= PASS_P for x in (main, fee06, fwd_g)):
        return {"status": "forward", "branch": "minute_mode", "reason": "frequency passed; confirmed impossible at 30 days", "blockers": []}
    return {"status": "forward", "branch": "collect_to_90d", "reason": f"p = {p:.4f} between kill and pass", "blockers": []}


def persistence(hits_by_snapshot: Sequence[tuple[int, Any, set]]) -> tuple[int, int]:
    """Hourly «стійкість через 1 год»: ``(ts, expiry, keys hit)`` per snapshot in time order (one currency). Returns
    (crossings that have a next snapshot of the same expiry, of them crossing again on the same key there)."""
    total = again = 0
    for (_ts, exp, keys), nxt in zip(hits_by_snapshot, [*hits_by_snapshot[1:], None], strict=True):
        if nxt is None or nxt[1] != exp:
            continue
        total += len(keys)
        again += len(keys & nxt[2])
    return total, again


def build_episodes(
    obs: Sequence[tuple[int, bool, bool]], *, gap_s: int = EPISODE_GAP_S, max_unobs_s: int = EPISODE_MAX_UNOBS_S
) -> list[dict[str, Any]]:
    """Episodes of ONE key K (currency, strike, type, side) from its observations ``(ts, crossed, opens)`` in time order:
    only valid observations (class A, measurable, inside the tau window); ``opens`` = the row is in the registered
    ATM +- 1. Kind 'M' when the previous observation of K is a clear one <= ``gap_s`` before detection, else 'H'
    (detected late, residual time). An episode starts at a crossing with ``opens``; it ends at the first clear observation when the
    time from the last crossing to it is <= ``max_unobs_s`` (event; ``d_right`` = clear - detection, ``d_left`` = last
    crossing - detection), else it is right-censored at the last crossing, as it is when the observations run out.
    Returns dicts ``{start, kind ('M'|'H'), event, d_right, d_left}`` in seconds."""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    prev: tuple[int, bool, bool] | None = None
    for ts, crossed, opens in obs:
        if cur is None:
            if crossed and opens:
                kind = "M" if prev is not None and not prev[1] and ts - prev[0] <= gap_s else "H"
                cur = {"start": ts, "kind": kind, "last": ts}
        elif crossed:
            cur["last"] = ts
        else:
            if ts - cur["last"] <= max_unobs_s:
                out.append(
                    {
                        "start": cur["start"],
                        "kind": cur["kind"],
                        "event": True,
                        "d_right": ts - cur["start"],
                        "d_left": cur["last"] - cur["start"],
                    }
                )
            else:
                d = cur["last"] - cur["start"]
                out.append({"start": cur["start"], "kind": cur["kind"], "event": False, "d_right": d, "d_left": d})
            cur = None
        prev = (ts, crossed, opens)
    if cur is not None:
        d = cur["last"] - cur["start"]
        out.append({"start": cur["start"], "kind": cur["kind"], "event": False, "d_right": d, "d_left": d})
    return out


def km_median(durations: Sequence[float], events: Sequence[bool]) -> float | None:
    """Kaplan-Meier median: the smallest time where the survival estimate drops to <= 0.5; None if it never does."""
    pairs = sorted(zip(durations, events, strict=True))
    at_risk = len(pairs)
    s = 1.0
    i = 0
    while i < len(pairs):
        t = pairs[i][0]
        d = c = 0
        while i < len(pairs) and pairs[i][0] == t:
            d += int(pairs[i][1])
            c += int(not pairs[i][1])
            i += 1
        if d:
            s *= 1.0 - d / at_risk
            if s <= 0.5:
                return float(t)
        at_risk -= d + c
    return None


def intrinsic(strike: float, spot: float, is_call: bool) -> float:
    return max(0.0, spot - strike) if is_call else max(0.0, strike - spot)


def paper_trade(
    entry: Mapping[str, Any],
    exit_: Mapping[str, Any] | None,
    side: str,
    *,
    max_ask_b: float | None,
    max_ask_d_usd: float | None,
    last_index_d: float,
    last_index_b: float,
    fee: float = FEE_RATE,
    bybit_cap: float | None = None,
) -> dict[str, Any]:
    """One journal trade («Паперовий журнал»): enter at the raw touch (sell the dearer leg at its bid, buy the cheaper at
    its ask), exit at the last snapshot of the expiry (buy back the sold leg at its ask, sell the bought at its bid),
    taker fee ``fee`` x d_index on all four legs (Bybit legs capped at ``bybit_cap`` x raw price when given). A missing
    exit quote: bought leg without a bid sells at 0; sold leg without an ask buys back at max(2 x the largest ask of
    that leg seen in the expiry, intrinsic at that venue's exit index); a missing exit snapshot (``exit_`` None) fills
    both legs that way at ``last_index_*``. Returns USD per 1 coin on notional and ``conservative`` (any such fill)."""
    de = float(entry["d_index"])
    k, call = float(entry["strike"]), entry["opt"] == "C"
    dx = num(exit_.get("d_index")) if exit_ is not None else None
    bx_index = num(exit_.get("b_index")) if exit_ is not None else None
    dx = dx or last_index_d
    bx_index = bx_index or last_index_b

    def fee_b(price: float) -> float:
        base = fee * de if bybit_cap is None else min(fee * (num(entry.get("b_index")) or de), bybit_cap * price)
        return base

    def fee_b_exit(price: float) -> float:
        return fee * dx if bybit_cap is None else min(fee * bx_index, bybit_cap * price)

    conservative = False
    x = exit_ or {}
    if side == "b_over_d":  # short Bybit, long Deribit
        sell_in = float(entry["b_bid"])
        buy_in = float(entry["d_ask"]) * de
        ask_b = num(x.get("b_ask"))
        if ask_b is None:
            conservative = True
            ask_b = max(2.0 * (max_ask_b or 0.0), intrinsic(k, bx_index, call))
        bid_d = num(x.get("d_bid"))
        if bid_d is None:
            conservative = True
            bid_d_usd = 0.0
        else:
            bid_d_usd = bid_d * dx
        gross = (sell_in - ask_b) + (bid_d_usd - buy_in)
        fees = fee_b(sell_in) + fee * de + fee_b_exit(ask_b) + fee * dx
    elif side == "d_over_b":  # short Deribit, long Bybit
        sell_in = float(entry["d_bid"]) * de
        buy_in = float(entry["b_ask"])
        ask_d = num(x.get("d_ask"))
        if ask_d is None:
            conservative = True
            ask_d_usd = max(2.0 * (max_ask_d_usd or 0.0), intrinsic(k, dx, call))
        else:
            ask_d_usd = ask_d * dx
        bid_b = num(x.get("b_bid"))
        if bid_b is None:
            conservative = True
            bid_b = 0.0
        gross = (sell_in - ask_d_usd) + (bid_b - buy_in)
        fees = fee * de + fee_b(buy_in) + fee * dx + fee_b_exit(bid_b)
    else:
        raise ValueError(f"unknown side {side!r}")
    pnl = gross - fees
    return {"pnl_usd": pnl, "pnl_frac": pnl / de, "gross_usd": gross, "fees_usd": fees, "conservative": conservative or exit_ is None}
