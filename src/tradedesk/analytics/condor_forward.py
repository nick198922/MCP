"""Forward check of the weekly Deribit iron condor H1 / H4 on `option_leg_snapshot` rows (pure functions, no I/O).

Registration: IDEA-20261031-structural-edges-depth, item 8j (docs/ideas/IDEA-20261031-structural-edges-depth.md).
Part (a) compares the executable cost of the condor (sold legs at the bid, bought legs at the ask; on a close the
sides swap) with the backtest's cost model "mark -/+ 1.5 vol points" (`option_condor.exec_legs(model="mark")`).
Prices are Deribit inverse quotes in the coin per 1 coin of underlying, i.e. fractions of spot (x spot = USD).

Reading rule of an entry (`friday_entry`): the currency's rows of that Friday with ``ts`` <= Friday 10:00 UTC, the
H1 roles ``lp sp10 sc10 lc``, the expiry the next Friday 08:00 UTC, every leg within +-1.5 % of spot of its target
(``|dist| <= 0.015``) and the strike order ``lp < sp10 < forward < sc10 < lc``. Rows after the cutoff never matter.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from tradedesk.analytics.put_skew import inverse_price

H1_QTY = {"lp": 1, "sp10": -1, "sc10": -1, "lc": 1}  # +1 bought, -1 sold (1 coin of notional per leg)
BAND = 0.015  # leg within +-1.5 % of spot of its target (item 7 rule)
ENTRY_CUTOFF_HOUR_UTC = 10  # entry snapshot must be taken by Friday 10:00 UTC (8d run note)
EXPIRY_HOUR_UTC = 8
HS = 0.015  # cost model: mark -/+ 1.5 vol points
Z_MDE = 2.8  # MDE = 2.8 * SD / sqrt(n): two-sided 5 %, power 80 %


def _ts(d: date, hour: int) -> int:
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=UTC).timestamp())


def _row_ts(row: Mapping[str, Any]) -> int | None:
    """The row's ``ts`` (unix seconds) or None when absent: such a row is skipped, never read as time 0 (it would pass
    every cutoff and win as "the earliest"; review of 8j, residual A)."""
    ts = row.get("ts")
    return None if ts is None or isinstance(ts, bool) else int(ts)


def _keep_earliest(legs: dict[str, Mapping[str, Any]], key: str, row: Mapping[str, Any]) -> None:
    """Keep the row with the earliest ``ts`` per key, independent of the iteration order (the store has one row per
    (day, currency, role), but this pure function does not rely on it; ties keep the first row seen). A row without
    ``ts`` is skipped."""
    ts = _row_ts(row)
    if ts is None:
        return
    old = legs.get(key)
    old_ts = None if old is None else _row_ts(old)
    if old is None or old_ts is None or ts < old_ts:
        legs[key] = row


def friday_entry(rows: Iterable[Mapping[str, Any]], friday: date, currency: str) -> tuple[dict[str, Mapping[str, Any]] | None, str]:
    """The H1 entry legs ``{role: row}`` of ``currency`` on ``friday`` by the registered reading rule, or (None, reason).

    Several rows of one role (not possible in the store, PK (day, currency, role)) resolve to the earliest ``ts`` <=
    the cutoff: a later snapshot never replaces the entry. Rows of another expiry are ignored."""
    if friday.weekday() != 4:
        return None, "not a Friday"
    cutoff = _ts(friday, ENTRY_CUTOFF_HOUR_UTC)
    expiry = _ts(friday + timedelta(days=7), EXPIRY_HOUR_UTC)
    day = friday.isoformat()
    legs: dict[str, Mapping[str, Any]] = {}
    for r in rows:
        if r.get("currency") != currency or r.get("day") != day or r.get("role") not in H1_QTY:
            continue
        ts = _row_ts(r)
        if ts is None or ts > cutoff or r.get("expiry_ts") != expiry:
            continue
        _keep_earliest(legs, str(r["role"]), r)
    missing = [k for k in H1_QTY if k not in legs]
    if missing:
        return None, "no row: " + " ".join(missing)
    far = [k for k in H1_QTY if abs(float(legs[k].get("dist") or 0.0)) > BAND]
    if far:
        return None, "outside +-1.5 %: " + " ".join(far)
    F = float(legs["sp10"]["forward"])
    K = [float(legs[k]["strike"]) for k in ("lp", "sp10")] + [F] + [float(legs[k]["strike"]) for k in ("sc10", "lc")]
    if not all(a < b for a, b in zip(K, K[1:], strict=False)):
        return None, "strike order"
    return legs, "ok"


def thursday_exit(
    rows: Iterable[Mapping[str, Any]], entry: Mapping[str, Mapping[str, Any]], friday: date, currency: str
) -> tuple[dict[str, Mapping[str, Any]] | None, str]:
    """Buy-back legs ``{role: row}`` of the condor entered on ``friday``: the ``hold_<role>`` rows of the Thursday
    after it (the last daily snapshot before the Friday 08:00 expiry, ~23 h) with ``ts`` <= Thursday 10:00 UTC and the
    entry's instrument and expiry (several rows of one role: the earliest ``ts``); else (None, reason)."""
    thu = friday + timedelta(days=6)
    cutoff = _ts(thu, ENTRY_CUTOFF_HOUR_UTC)
    want = {"hold_" + k: k for k in H1_QTY}
    legs: dict[str, Mapping[str, Any]] = {}
    for r in rows:
        k = want.get(str(r.get("role")))
        if k is None or r.get("currency") != currency or r.get("day") != thu.isoformat():
            continue
        ts = _row_ts(r)
        if ts is None or ts > cutoff:
            continue
        if r.get("instrument") == entry[k].get("instrument") and r.get("expiry_ts") == entry[k].get("expiry_ts"):
            _keep_earliest(legs, k, r)
    missing = [k for k in H1_QTY if k not in legs]
    return (None, "no hold row: " + " ".join(missing)) if missing else (legs, "ok")


def leg_cost(row: Mapping[str, Any], qty: int, action: str = "open", hs: float = HS) -> tuple[float, float] | None:
    """(actual, model) cost of one leg vs its mark, in the coin per coin (fraction of spot), both >= 0 when sane.

    ``action`` "open": qty < 0 sells at the bid, qty > 0 buys at the ask; "close" swaps the sides (a sold leg is bought
    back at the ask). actual = |executable - mark|; model = |inverse_price(iv -/+ hs) - inverse_price(iv)| on the row's
    forward and tau (sell at mark IV - hs, buy at mark IV + hs). None if a needed quote is missing or the src is not a
    ticker (the book summary has no IVs per side and no sizes)."""
    sell = (qty < 0) == (action == "open")
    px = row.get("bid") if sell else row.get("ask")
    mark, iv = row.get("mark"), row.get("mark_iv")
    if row.get("src") != "ticker" or px is None or mark is None or not iv:
        return None
    F, K, T, call = float(row["forward"]), float(row["strike"]), float(row["tau_days"]) / 365.0, row["opt"] == "C"
    base = inverse_price(F, K, T, float(iv), call)
    shifted = inverse_price(F, K, T, max(float(iv) - hs, 1e-6) if sell else float(iv) + hs, call)
    actual = (float(mark) - float(px)) if sell else (float(px) - float(mark))
    return actual, abs(base - shifted)


def condor_cost(legs: Mapping[str, Mapping[str, Any]], action: str = "open", hs: float = HS) -> dict[str, float] | None:
    """Summed (actual, model) cost of the four H1 legs, fractions of spot; None if any leg lacks its quote."""
    a = m = 0.0
    for role, q in H1_QTY.items():
        c = leg_cost(legs[role], q, action, hs)
        if c is None:
            return None
        a, m = a + c[0], m + c[1]
    return {"actual": a, "model": m, "ratio": a / m if m > 0 else math.nan}


def ratio_of_sums(actual: Sequence[float], model: Sequence[float]) -> float:
    s = sum(model)
    return sum(actual) / s if s > 0 else math.nan


def ratio_ci(actual: Sequence[float], model: Sequence[float], *, n_boot: int = 10_000, seed: int = 20261031) -> tuple[float, float]:
    """Percentile 95 % CI of the ratio of sums, i.i.d. bootstrap over weeks."""
    n = len(actual)
    rng = random.Random(seed)
    rs = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        r = ratio_of_sums([actual[i] for i in idx], [model[i] for i in idx])
        if not math.isnan(r):
            rs.append(r)
    rs.sort()
    return rs[int(0.025 * (len(rs) - 1))], rs[int(0.975 * (len(rs) - 1))]


def cost_verdict(lo: float, hi: float, n_valid: int, n_min: int = 6) -> str:
    """Registered decision of part (a): '◯' if fewer than ``n_min`` valid entries; 'conservative' if the CI of
    actual / model lies below 1; 'optimistic' if above 1; else 'consistent'."""
    if n_valid < n_min:
        return "◯"
    if hi < 1.0:
        return "conservative"
    if lo > 1.0:
        return "optimistic"
    return "consistent"


# ---- part (a) report: costs only (rule "звіт (а) друкує лише витрати", review of 8j, residual B) -------------------
# The report never carries a Thursday mark / spot / credit / leg price, a delivery price or a P&L: the Thursday quotes
# ~23 h before expiry nearly fix the week's P&L of part (b). Only the keys below may appear in `cost_report`; the
# Thursday buy-back (a2) is reported pooled over the weeks only (no per-week rows: a per-week model cost would show
# how close spot is to the strikes).
COST_WEEKS = 8  # registered sample: the Fridays T0 ... T0 + 7 weeks
EARLIEST_T0 = date(2026, 11, 6)  # the earliest T0 the 8j registration allows; no snapshot is read before it
REPORT_KEYS = frozenset(
    {
        "currency", "fridays", "n_fridays", "interim", "entry", "exit",  # top level
        "n_valid", "n_weeks", "skips", "skip_counts", "actual_mean", "model_mean", "R", "ci", "verdict",  # a part
        "half_spread_vp", "small_size", "weeks", "credit_share",
        "n", "median", "mean",  # half_spread_vp[role]
        "n_legs", "n_known", "n_small", "share",  # small_size
        "friday", "actual", "model", "ratio",  # weeks[i] (entry only), credit_share
        *H1_QTY,
    }
)  # fmt: skip


def report_keys(x: Any) -> set[str]:
    """All dict keys anywhere inside ``x`` (nested dicts / lists)."""
    out: set[str] = set()
    stack = [x]
    while stack:
        v = stack.pop()
        if isinstance(v, Mapping):
            out.update(str(k) for k in v)
            stack.extend(v.values())
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
    return out


def foreign_keys(rep: Any) -> set[str]:
    """Keys of a report outside ``REPORT_KEYS``: the run-time guard of the whitelist (empty = clean)."""
    return report_keys(rep) - REPORT_KEYS


def json_safe(x: Any) -> Any:
    """``x`` with every NaN / inf float replaced by None, so that ``json.dumps(..., allow_nan=False)`` succeeds."""
    if isinstance(x, float):
        return None if math.isnan(x) or math.isinf(x) else x
    if isinstance(x, Mapping):
        return {k: json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [json_safe(v) for v in x]
    return x


def cost_fridays(t0: date, n: int = COST_WEEKS) -> list[date]:
    """The registered Fridays of part (a): T0 ... T0 + (n - 1) weeks."""
    return [t0 + timedelta(days=7 * k) for k in range(n)]


def _quote_skip(legs: Mapping[str, Mapping[str, Any]], action: str) -> str:
    bad = []
    for role, q in H1_QTY.items():
        if leg_cost(legs[role], q, action) is None:
            bad.append(f"{role}({'summary' if legs[role].get('src') != 'ticker' else 'no side'})")
    return "no quote: " + " ".join(bad)


def _half_spread_vp(row: Mapping[str, Any]) -> float | None:
    b, a = row.get("bid_iv"), row.get("ask_iv")
    return None if b is None or a is None else (float(a) - float(b)) / 2.0 * 100.0


def _exec_size(row: Mapping[str, Any], qty: int, action: str) -> float | None:
    """Size at the best level of the side the leg trades on (sell -> bid, buy -> ask; a close swaps them)."""
    s = row.get("bid_size") if (qty < 0) == (action == "open") else row.get("ask_size")
    return None if s is None else float(s)


def _cost_part(
    ok: list[tuple[date, Mapping[str, Mapping[str, Any]]]],
    skips: list[tuple[str, str]],
    action: str,
    n_weeks: int,
    n_boot: int,
    seed: int,
    n_min: int,
) -> dict[str, Any]:
    costs = [condor_cost(legs, action) for _, legs in ok]
    act = [c["actual"] for c in costs if c]
    mod = [c["model"] for c in costs if c]
    n = len(act)
    lo, hi = ratio_ci(act, mod, n_boot=n_boot, seed=seed) if n else (math.nan, math.nan)
    half = {}
    for role in H1_QTY:
        v = [h for _, legs in ok if (h := _half_spread_vp(legs[role])) is not None]
        half[role] = {"n": len(v), "median": statistics.median(v) if v else math.nan, "mean": statistics.fmean(v) if v else math.nan}
    sizes = [s for _, legs in ok for role, q in H1_QTY.items() if (s := _exec_size(legs[role], q, action)) is not None]
    n_small = sum(1 for s in sizes if s < 1.0)
    counts: dict[str, int] = {}
    for _, why in skips:
        counts[why] = counts.get(why, 0) + 1
    part: dict[str, Any] = {
        "n_valid": n,
        "n_weeks": n_weeks,
        "skips": [list(s) for s in skips],
        "skip_counts": sorted([k, v] for k, v in counts.items()),  # [reason, count]: reasons are data, not keys
        "actual_mean": statistics.fmean(act) if n else math.nan,
        "model_mean": statistics.fmean(mod) if n else math.nan,
        "R": ratio_of_sums(act, mod) if n else math.nan,
        "ci": [lo, hi],
        "verdict": cost_verdict(lo, hi, n, n_min),
        "half_spread_vp": half,
        "small_size": {"n_legs": 4 * n, "n_known": len(sizes), "n_small": n_small, "share": n_small / len(sizes) if sizes else math.nan},
    }
    if action == "open":  # entry quotes are known at entry and do not fix the P&L: per-week rows and the credit share
        part["weeks"] = [
            {"friday": f.isoformat(), "actual": c["actual"], "model": c["model"], "ratio": c["ratio"]}
            for (f, _), c in zip(ok, costs, strict=True)
            if c
        ]
        credit = sum(-q * float(legs[role]["mark"]) for _, legs in ok for role, q in H1_QTY.items())
        part["credit_share"] = {
            "actual": sum(act) / credit if credit > 0 else math.nan,
            "model": sum(mod) / credit if credit > 0 else math.nan,
        }
    return part


def cost_report(
    rows: Iterable[Mapping[str, Any]],
    fridays: Sequence[date],
    currency: str,
    *,
    n_boot: int = 10_000,
    seed: int = 20261031,
    n_min: int = 6,
    interim: bool = False,
) -> dict[str, Any]:
    """Part (a) of the 8j registration on ``option_leg_snapshot`` rows: (a1) entry cost (primary), (a2) Thursday
    buy-back cost of the entered condor (second), each R = sum actual / sum model with its bootstrap CI and the
    registered verdict; half-spreads in vol points by role, the share of legs whose best-level size is < 1 coin,
    for (a1) the cost as a share of the entry credit at the mark; counters and skip reasons. Costs are fractions of
    spot per 1 coin of notional per leg. A week enters (a2) only if its (a1) entry is valid. Only ``REPORT_KEYS``.

    ``interim=True`` (a look before the sample ends): (a2) is neither computed nor returned (no ``exit`` key, the
    Thursday rows are not even read: with k weeks done the pooled (a2) is the week's Thursday model cost, i.e. how
    close spot is to the strikes, and two looks a week apart give the new week by subtraction), and the (a1) verdict
    is "—" (one decision rule, after the 8 weeks)."""
    rows = [r for r in rows if r.get("currency") == currency]
    if interim:
        rows = [r for r in rows if not str(r.get("role", "")).startswith("hold_")]
    entry_ok: list[tuple[date, Mapping[str, Mapping[str, Any]]]] = []
    exit_ok: list[tuple[date, Mapping[str, Mapping[str, Any]]]] = []
    entry_skip: list[tuple[str, str]] = []
    exit_skip: list[tuple[str, str]] = []
    for f in fridays:
        legs, why = friday_entry(rows, f, currency)
        if legs is not None and condor_cost(legs, "open") is None:
            legs, why = None, _quote_skip(legs, "open")
        if legs is None:
            entry_skip.append((f.isoformat(), why))
            exit_skip.append((f.isoformat(), "no valid entry"))
            continue
        entry_ok.append((f, legs))
        if interim:
            continue
        ex, why = thursday_exit(rows, legs, f, currency)
        if ex is not None and condor_cost(ex, "close") is None:
            ex, why = None, _quote_skip(ex, "close")
        if ex is None:
            exit_skip.append((f.isoformat(), why))
            continue
        exit_ok.append((f, ex))
    n = len(fridays)
    rep: dict[str, Any] = {
        "currency": currency,
        "fridays": [f.isoformat() for f in fridays],
        "n_fridays": n,
        "interim": interim,
        "entry": _cost_part(entry_ok, entry_skip, "open", n, n_boot, seed, n_min),
    }
    if interim:
        rep["entry"]["verdict"] = "—"
    else:
        rep["exit"] = _cost_part(exit_ok, exit_skip, "close", n, n_boot, seed, n_min)
    return rep


def _pct(x: float, nd: int = 3) -> str:
    return "—" if x is None or math.isnan(x) else f"{100 * x:.{nd}f}"


def _num(x: float, nd: int = 2) -> str:
    return "—" if x is None or math.isnan(x) else f"{x:.{nd}f}"


def format_cost_report(rep: Mapping[str, Any], label: str) -> list[str]:
    """Markdown lines of one currency's part (a) report: only what ``cost_report`` holds (costs, never prices)."""
    fr = rep["fridays"]
    span = f"{fr[0]} … {fr[-1]}" if fr else "—"
    L = [f"### {rep['currency']} ({label}), Fridays {span} ({rep['n_fridays']})", ""]
    L.append("| part | valid / weeks | actual, % spot (mean) | model, % spot (mean) | R = Σ actual / Σ model | 95 % CI | verdict |")
    L.append("|---|---|---|---|---|---|---|")
    parts = [("(a1) entry, primary", rep["entry"])]
    if "exit" in rep:  # absent in an interim look: (a2) is not computed before the sample ends
        parts.append(("(a2) Thursday buy-back", rep["exit"]))
    for name, p in parts:
        lo, hi = p["ci"]
        L.append(
            f"| {name} | {p['n_valid']} / {p['n_weeks']} | {_pct(p['actual_mean'])} | {_pct(p['model_mean'])} | "
            f"{_num(p['R'])} | [{_num(lo)}; {_num(hi)}] | {p['verdict']} |"
        )
    cs = rep["entry"]["credit_share"]
    L += ["", f"(a1) cost as a share of the entry credit at the mark: actual {_num(cs['actual'])}, model {_num(cs['model'])}", ""]
    L.append("| part | half-spread (ask_iv - bid_iv)/2, vp: " + " | ".join(H1_QTY) + " | legs with size known | size < 1 coin | share |")
    L.append("|---|" + "---|" * (len(H1_QTY) + 3))
    for name, p in parts:
        h = p["half_spread_vp"]
        s = p["small_size"]
        cells = " | ".join(f"{_num(h[r]['median'])} (n {h[r]['n']})" for r in H1_QTY)
        L.append(f"| {name} (median) | {cells} | {s['n_known']} / {s['n_legs']} | {s['n_small']} | {_num(s['share'])} |")
    L += ["", "| (a1) Friday | actual, % spot | model, % spot | ratio |", "|---|---|---|---|"]
    L += [f"| {w['friday']} | {_pct(w['actual'])} | {_pct(w['model'])} | {_num(w['ratio'])} |" for w in rep["entry"]["weeks"]]
    L += ["", "| part | Friday | skip reason |", "|---|---|---|"]
    for name, p in parts:
        L += [f"| {name} | {d} | {why} |" for d, why in p["skips"]]
    for name, p in parts:
        L.append(f"skip counts {name}: " + (", ".join(f"{k}: {v}" for k, v in p["skip_counts"]) or "none"))
    if "exit" not in rep:
        L.append("(a2) Thursday buy-back: not computed in an interim look (only after the last buy-back of the sample)")
    L.append("")
    return L


def mde(sd: float, n: int, k: float = Z_MDE) -> float:
    return k * sd / math.sqrt(n)


def power(sd: float, n: int, effect: float, z_alpha: float = 1.959964) -> float:
    """Two-sided test power at a true mean ``effect`` (normal approximation, the far tail ignored); ``z_alpha`` is the
    two-sided critical value (default alpha = 0.05; 2.2414 for alpha = 0.025)."""
    return 0.5 * math.erfc(-(effect / (sd / math.sqrt(n)) - z_alpha) / math.sqrt(2.0))


def weeks_for_mde(sd: float, effect: float, k: float = Z_MDE) -> int:
    """Smallest n with MDE(sd, n) <= effect."""
    return math.ceil(round((k * sd / effect) ** 2, 9))
