"""Daily cycle regime of a coin (IDEA-20261028-btc-cycle-regime-gauge, step 1). Pure functions, no I/O; time comes in.

The last closed UTC day gets one of five states, defined in the card before looking at any data (section «Гіпотеза»,
28.10). The thresholds below are pre-registered: do not tune them.

* bottom (capitulation): drawdown from the 365-day high >= 35 %, funding7 <= 0, basis7 < 0, RSI14 <= 35 (all four);
* accumulation / short fuel: |price / SMA365 - 1| <= 10 %, basis7 < 0, funding7 < 8 %/yr, price not at its 30-day low
  (all four);
* overheat: price >= 1.3 x SMA365, funding7 >= 20 %/yr, basis7 > +0.05 %, RSI14 >= 70 (any three of the four);
* growth / decline: every other day, by the side of the price against SMA365. The card names the 30-day slope of SMA365
  too but not what a day with mixed signs is; we label by the price side and report `trend_confirmed` (slope agrees)
  instead of inventing a sixth state.

Precedence when two hold at once: bottom, accumulation, overheat (bottom and accumulation can overlap; overheat
excludes both).

Components (decimals, 0.01 = 1 %; RSI in points 0-100):
* price = spot daily close; sma365 = mean of the last 365 closes; price_to_sma = price / sma365 - 1;
  sma_slope_30d = sma365 / sma365 thirty days earlier - 1;
* drawdown_365d = price / max(last 365 closes) - 1 (<= 0); dist_30d_low = price / min(previous 29 closes) - 1
  (<= 0: the day closed at its 30-day low);
* rsi14 = Wilder's RSI of the daily closes (seeded at the first close given);
* basis7 = mean over the last 7 days of perp close / spot close - 1 (at least 5 of the 7 days);
* funding7 = mean rate of the settlements in the 7 days to the close, simple-annualised (rate x 8760 / interval hours,
  interval = median spacing); the Binance base rate 0.01 %/8 h is 0.1095. When the spacing inside the window varies
  (the venue changed the interval mid-window, or a settlement is missing) it is the sum of the window's rates x 365 / 7
  instead, with a warning (a missing settlement then counts as zero);
* premium7 = mean of the daily premium-index closes (perp vs index), reported only: no rule uses it.

The 365-, 395- and 30-close windows are positional; a window whose closes do not span consecutive calendar days (the
series misses days) gives None for its components, with a warning, instead of silently stretching.

`transitions` ranks the other states by how many components must change, then by the summed size of the moves in
rough typical units (`SCALE`, a presentation choice, not part of the pre-registered rules). It is a greedy estimate:
the changes listed reach the state (`reaches`) on the current numbers, other paths may exist.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .market_flow import funding_interval_hours
from .markup import rsi as wilder_rsi

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
SMA_DAYS = 365
HIGH_DAYS = 365
SLOPE_DAYS = 30
LOW_DAYS = 30
RSI_DAYS = 14
AVG_DAYS = 7
MIN_AVG_DAYS = 5
MIN_SPOT_DAYS = SMA_DAYS + SLOPE_DAYS  # closes needed for SMA365 and its 30-day slope
RSI_WARMUP_DAYS = 60  # extra closes before the window so Wilder's smoothing has settled
SETTLE_SLACK_MS = 60_000  # a settlement stamped a few ms after 00:00 still belongs to the day that closes then
BASE_FUNDING = 0.0001 * 8760 / 8  # Binance base rate 0.01 % per 8 h, simple-annualised: 0.1095
# Code version of the states and components, stored with every journal row (cycle_regime_log.version). Bump it when
# RULES, the precedence or a component formula changes, so rows of different versions are never pooled silently.
VERSION = "cycle-regime-v1"

STATES = ("bottom", "accumulation", "overheat", "growth", "decline")
SPECIAL = STATES[:3]
STATE_UA = {
    "bottom": "дно",
    "accumulation": "накопичення",
    "overheat": "перегрів",
    "growth": "ріст",
    "decline": "падіння",
}

# state -> ((component, op, threshold), ...), how many must hold. Pre-registered in the card (28.10): do not tune.
RULES: dict[str, tuple[tuple[tuple[str, str, float], ...], int]] = {
    "bottom": ((("drawdown_365d", "<=", -0.35), ("funding7", "<=", 0.0), ("basis7", "<", 0.0), ("rsi14", "<=", 35.0)), 4),
    "accumulation": ((("price_to_sma", "|x|<=", 0.10), ("basis7", "<", 0.0), ("funding7", "<", 0.08), ("dist_30d_low", ">", 0.0)), 4),
    "overheat": ((("price_to_sma", ">=", 0.30), ("funding7", ">=", 0.20), ("basis7", ">", 0.0005), ("rsi14", ">=", 70.0)), 3),
    "growth": ((("price_to_sma", ">=", 0.0),), 1),
    "decline": ((("price_to_sma", "<", 0.0),), 1),
}
# Rough typical moves, only to rank the paths in `transitions`.
SCALE = {"drawdown_365d": 0.10, "price_to_sma": 0.10, "funding7": 0.05, "basis7": 0.0005, "rsi14": 10.0, "dist_30d_low": 0.05}
NEGATE = {"<=": ">", "<": ">=", ">=": "<", ">": "<=", "|x|<=": "|x|>", "|x|>": "|x|<="}

_ROW_ERRORS = (KeyError, TypeError, ValueError, OverflowError, IndexError)


def parse_closes(rows: Iterable[Any], *, positive: bool = True) -> dict[int, float]:
    """Binance kline rows [open_ms, open, high, low, close, ...] -> {open_ms: close}. Non-numbers are dropped, and with
    `positive` also closes <= 0 (premium-index klines are signed: pass positive=False)."""
    out: dict[int, float] = {}
    for r in rows or []:
        try:
            t = int(r[0])
            c = float(r[4])
        except _ROW_ERRORS:
            continue
        if math.isfinite(c) and (c > 0 or not positive):
            out[t] = c
    return out


def _mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def _consecutive(opens: Sequence[int], i: int, n: int) -> bool:
    """True when the n bars ending at index i open on n consecutive days (opens are sorted and unique)."""
    return opens[i] - opens[i - n + 1] == (n - 1) * DAY_MS


def _components(
    opens: Sequence[int],
    closes: Sequence[float],
    i: int,
    perp: Mapping[int, float],
    funding: Sequence[tuple[int, float]],
    premium: Mapping[int, float],
    warns: list[str] | None = None,
) -> dict[str, Any]:
    """Components of the day whose bar opens at opens[i] (closes at opens[i] + 1 day), from data up to that close.
    Notes on calendar gaps and on a funding window with varying spacing are appended to `warns` when given."""
    c = closes[: i + 1]
    t_last = opens[i]
    day_end = t_last + DAY_MS
    price = c[-1]
    out: dict[str, Any] = dict.fromkeys(
        (
            "sma365",
            "sma365_30d_ago",
            "price_to_sma",
            "sma_slope_30d",
            "high_365d",
            "drawdown_365d",
            "low_30d_prior",
            "dist_30d_low",
            "rsi14",
            "basis7",
            "basis_last",
            "funding7",
            "funding_interval_hours",
            "premium7",
        )
    )
    out.update({"close": price, "perp_close": perp.get(t_last), "basis_days": 0, "funding_settlements": 0, "premium_days": 0})
    out["premium_negative_days"] = 0
    gaps: list[str] = []  # windows long enough by count whose closes are not consecutive calendar days
    if len(c) >= SMA_DAYS:
        if _consecutive(opens, i, SMA_DAYS):
            sma = _mean(c[-SMA_DAYS:])
            out["sma365"] = sma
            out["price_to_sma"] = price / sma - 1.0
        else:
            gaps.append(f"sma365 / price_to_sma (last {SMA_DAYS} closes)")
    if len(c) >= SMA_DAYS + SLOPE_DAYS:
        if out["sma365"] is not None and _consecutive(opens, i, SMA_DAYS + SLOPE_DAYS):
            prev = _mean(c[-SMA_DAYS - SLOPE_DAYS : -SLOPE_DAYS])
            out["sma365_30d_ago"] = prev
            out["sma_slope_30d"] = out["sma365"] / prev - 1.0
        else:
            gaps.append(f"sma_slope_30d (last {SMA_DAYS + SLOPE_DAYS} closes)")
    if len(c) >= HIGH_DAYS:
        if _consecutive(opens, i, HIGH_DAYS):
            hi = max(c[-HIGH_DAYS:])
            out["high_365d"] = hi
            out["drawdown_365d"] = price / hi - 1.0
        else:
            gaps.append(f"high_365d / drawdown_365d (last {HIGH_DAYS} closes)")
    if len(c) >= LOW_DAYS:
        if _consecutive(opens, i, LOW_DAYS):
            lo = min(c[-LOW_DAYS:-1])
            out["low_30d_prior"] = lo
            out["dist_30d_low"] = price / lo - 1.0
        else:
            gaps.append(f"low_30d_prior / dist_30d_low (last {LOW_DAYS} closes)")
    if gaps and warns is not None:
        warns.append(
            f"Daily spot series misses days in the windows ending {_date(t_last)}: {'; '.join(gaps)} are null, because "
            "their window of N closes spans more than N calendar days."
        )
    out["rsi14"] = wilder_rsi(list(c), RSI_DAYS)

    days = [day_end - k * DAY_MS for k in range(AVG_DAYS, 0, -1)]  # bar opens of the last 7 days
    spot_of = dict(zip(opens[max(0, i - AVG_DAYS - 1) : i + 1], closes[max(0, i - AVG_DAYS - 1) : i + 1], strict=True))
    basis = [perp[t] / spot_of[t] - 1.0 for t in days if t in perp and t in spot_of and perp[t] > 0]
    out["basis_days"] = len(basis)
    if len(basis) >= MIN_AVG_DAYS:
        out["basis7"] = _mean(basis)
    if out["perp_close"] is not None and out["perp_close"] > 0:
        out["basis_last"] = out["perp_close"] / price - 1.0
    prem = [premium[t] for t in days if t in premium]
    out["premium_days"] = len(prem)
    out["premium_negative_days"] = sum(1 for x in prem if x < 0)
    if len(prem) >= MIN_AVG_DAYS:
        out["premium7"] = _mean(prem)

    lo_t, hi_t = day_end - AVG_DAYS * DAY_MS + SETTLE_SLACK_MS, day_end + SETTLE_SLACK_MS
    win = [(t, r) for t, r in funding if lo_t < t <= hi_t]
    out["funding_settlements"] = len(win)
    hrs = funding_interval_hours(win)
    if hrs is not None and len(win) >= 0.5 * AVG_DAYS * 24 / hrs:
        out["funding_interval_hours"] = hrs
        spacing = sorted({round((b - a) / HOUR_MS, 2) for (a, _), (b, _) in zip(win, win[1:], strict=False) if b > a})
        if len(spacing) > 1:
            # mean x 8760 / median interval is wrong when the interval changes inside the window: the sum of what was
            # actually paid over the 7 days, scaled to a year, is right then (but counts a missing settlement as zero)
            out["funding7"] = math.fsum(r for _, r in win) * 365.0 / AVG_DAYS
            if warns is not None:
                warns.append(
                    f"Funding settlement spacing varies in the 7 days to {_date(day_end)} 00:00 UTC "
                    f"({', '.join(f'{h:g} h' for h in spacing)}): the venue changed the interval or a settlement is "
                    "missing; funding7 = sum of the window's rates x 365 / 7 (a missing settlement counts as zero)."
                )
        else:
            out["funding7"] = _mean([r for _, r in win]) * 8760.0 / hrs
    return out


def _ok(value: float, op: str, thr: float) -> bool:
    if op == "<=":
        return value <= thr
    if op == "<":
        return value < thr
    if op == ">=":
        return value >= thr
    if op == ">":
        return value > thr
    if op == "|x|<=":
        return abs(value) <= thr
    return abs(value) > thr  # "|x|>"


def _target(value: float, op: str, thr: float) -> float:
    """The nearest value satisfying `op thr` (the value itself when it already does; strict ops step just past)."""
    if _ok(value, op, thr):
        return value
    eps = 1e-9 * max(1.0, abs(thr))
    if op in ("<=", ">="):
        return thr
    if op == "<":
        return thr - eps
    if op == ">":
        return thr + eps
    sign = 1.0 if value >= 0 else -1.0
    return sign * thr if op == "|x|<=" else sign * (thr + eps)


def evaluate(comps: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Per state: every condition with its value and whether it is met (None without data), how many are met and
    needed, and whether the state holds (True / False / None = undecided for lack of data)."""
    out: dict[str, dict[str, Any]] = {}
    for state, (conds, k) in RULES.items():
        rows, n_true, n_none = [], 0, 0
        for comp, op, thr in conds:
            v = comps.get(comp)
            met = None if v is None else _ok(v, op, thr)
            n_true += met is True
            n_none += met is None
            rows.append({"component": comp, "op": op, "threshold": thr, "value": v, "met": met})
        holds = True if n_true >= k else (None if n_true + n_none >= k else False)
        out[state] = {"holds": holds, "met": n_true, "need": k, "conditions": rows}
    return out


def classify(comps: Mapping[str, Any] | None) -> str | None:
    """The state by precedence (bottom, accumulation, overheat, then growth / decline); None when a state that comes
    first cannot be decided for lack of data."""
    if not comps:
        return None
    ev = evaluate(comps)
    for s in STATES:
        if ev[s]["holds"] is None:
            return None
        if ev[s]["holds"]:
            return s
    return None


def _n_met(comps: Mapping[str, Any], state: str) -> int:
    return sum(1 for c, op, thr in RULES[state][0] if _ok(comps[c], op, thr))


def _plan(comps: Mapping[str, Any], target: str) -> dict[str, Any] | None:
    """Changes that move today's components into `target` (greedy: cheapest own conditions first, then break every
    state that takes precedence and still holds, preferring components already being changed). None without data."""
    names = {c for s in STATES for c, _, _ in RULES[s][0]}
    if any(comps.get(c) is None for c in names):
        return None
    conds, k = RULES[target]
    after = {c: float(comps[c]) for c in names}
    steps: list[tuple[str, str, float]] = []
    unmet = [x for x in conds if not _ok(after[x[0]], x[1], x[2])]
    unmet.sort(key=lambda x: abs(_target(after[x[0]], x[1], x[2]) - after[x[0]]) / SCALE[x[0]])
    for c, op, thr in unmet[: max(0, k - (len(conds) - len(unmet)))]:
        after[c] = _target(after[c], op, thr)
        steps.append((c, op, thr))
    for s in SPECIAL[: STATES.index(target)]:
        s_conds, s_k = RULES[s]
        while _n_met(after, s) >= s_k:
            cands = []
            for c, op, thr in s_conds:
                if not _ok(after[c], op, thr):
                    continue
                trial = dict(after)
                trial[c] = _target(after[c], NEGATE[op], thr)
                if _n_met(trial, target) < k:
                    continue  # breaking it this way would undo the target
                fresh = all(x[0] != c for x in steps)
                cands.append((fresh, abs(trial[c] - after[c]) / SCALE[c], c, NEGATE[op], thr))
            if not cands:
                return None
            _, _, c, op, thr = min(cands)
            after[c] = _target(after[c], op, thr)
            steps.append((c, op, thr))
    changes: list[dict[str, Any]] = []
    for c, op, thr in steps:
        row = next((r for r in changes if r["component"] == c), None)
        if row is None:
            row = {"component": c, "value": comps[c], "conditions": []}
            changes.append(row)
        row["conditions"].append({"op": op, "threshold": thr})
        row["to"] = after[c]
    return {
        "state": target,
        "state_ua": STATE_UA[target],
        "n_changes": len(changes),
        "score": math.fsum(abs(r["to"] - r["value"]) / SCALE[r["component"]] for r in changes),
        "changes": changes,
        "reaches": classify(after) == target,
    }


def transitions(comps: Mapping[str, Any] | None, current: str | None) -> list[dict[str, Any]]:
    """The other states, nearest first (fewest changed components, then the smallest summed move in SCALE units)."""
    if not comps or current is None:
        return []
    plans = [p for s in STATES if s != current and (p := _plan(comps, s)) is not None]
    return sorted(plans, key=lambda p: (p["n_changes"], p["score"]))


# --------------------------------------------------------------------------------------------- Ukrainian one-liner

_NAME_UA = {
    "drawdown_365d": "просадка від 365-денного максимуму",
    "price_to_sma": "ціна до SMA365",
    "funding7": "funding7",
    "basis7": "базис7",
    "rsi14": "RSI14",
    "dist_30d_low": "відрив від 30-денного мінімуму",
}
_OP_UA = {"<=": "≤", "<": "<", ">=": "≥", ">": ">", "|x|<=": "≤", "|x|>": ">"}


def _pct(x: float, nd: int = 0, *, sign: bool = True) -> str:
    s = ("+" if x >= 0 else "−") if sign else ("−" if x < 0 else "")
    return f"{s}{abs(x) * 100:.{nd}f} %"


def _val(comp: str, x: float) -> str:
    if comp == "rsi14":
        return f"{x:.0f}"
    if comp == "basis7":
        return _pct(x, 2)
    return _pct(x, 0, sign=comp != "funding7")


def _cond_ua(comp: str, op: str, thr: float) -> str:
    if comp == "dist_30d_low":
        return "ціна на 30-денному мінімумі" if op == "<=" else "ціна вище 30-денного мінімуму"
    if comp == "price_to_sma" and thr == 0.0:
        return "ціна ≥ SMA365" if op == ">=" else "ціна < SMA365"
    if op in ("|x|<=", "|x|>"):
        return f"|ціна / SMA365 − 1| {_OP_UA[op]} {_val('funding7', thr)}"
    return f"{_NAME_UA[comp]} {_OP_UA[op]} {_val(comp, thr)}"


def line_ua(comps: Mapping[str, Any] | None, state: str | None, closest: Mapping[str, Any] | None) -> str | None:
    """«Режим циклу: накопичення (базис −0.05 %, funding 6 % < 11 %, +4 % над SMA365); зміниться, якщо: funding7 ≥ 8 %».
    The tail lists what would end the current state (the nearest path) without naming the next state: in the brief a
    named next state reads like a forecast (critic 28.10). `closest` / `transitions` keep the name for the tool."""
    if not comps or state is None:
        return None
    parts: list[str] = []

    def put(comp: str) -> None:
        v = comps.get(comp)
        if v is None:
            return
        if comp == "basis7":
            parts.append(f"базис {_pct(v, 2)}")
        elif comp == "funding7":
            parts.append(f"funding {_val('funding7', v)} {'<' if v < BASE_FUNDING else '≥'} {_val('funding7', BASE_FUNDING)}")
        elif comp == "price_to_sma":
            parts.append(f"{_pct(v)} {'над' if v >= 0 else 'під'} SMA365")
        elif comp == "rsi14":
            parts.append(f"RSI14 {v:.0f}")
        elif comp == "drawdown_365d":
            parts.append(f"просадка {_pct(v)}")
        elif comp == "sma_slope_30d":
            parts.append(f"SMA365 за 30 дн {_pct(v, 1)}")

    order = {
        "bottom": ("drawdown_365d", "funding7", "basis7", "rsi14"),
        "accumulation": ("basis7", "funding7", "price_to_sma"),
        "overheat": ("price_to_sma", "funding7", "basis7", "rsi14"),
        "growth": ("price_to_sma", "sma_slope_30d"),
        "decline": ("price_to_sma", "sma_slope_30d"),
    }[state]
    for comp in order:
        put(comp)
    text = f"Режим циклу: {STATE_UA[state]}" + (f" ({', '.join(parts)})" if parts else "")
    if closest:
        need = [_cond_ua(r["component"], x["op"], x["threshold"]) for r in closest["changes"] for x in r["conditions"]]
        text += f"; зміниться, якщо: {', '.join(need[:3])}" + (" …" if len(need) > 3 else "")
    return text


# ------------------------------------------------------------------------------------------------------- entry point


def _date(open_ms: int) -> str:
    return datetime.fromtimestamp(open_ms / 1000, UTC).date().isoformat()


def cycle_regime(
    spot: Mapping[int, float],
    perp: Mapping[int, float],
    funding: Iterable[tuple[int, float]],
    premium: Mapping[int, float],
    now: datetime,
    history_days: int = 0,
) -> dict[str, Any]:
    """State of the last closed UTC day before `now` with its components, every rule, the other states nearest first
    (`transitions`, `closest`), a Ukrainian one-liner and the state of each of the last `history_days` closed days
    (oldest first). Inputs: daily closes keyed by bar open (ms) of spot, perp and the premium index; funding
    settlements (ts_ms, rate per interval). `state` is None when the day's spot bar is missing or a component that
    decides it has no data (`undecided_by`). `warnings` holds the last day's notes on calendar gaps in the spot series
    and on a funding window with varying settlement spacing."""
    day_end = int(now.timestamp() * 1000) // DAY_MS * DAY_MS
    opens = sorted(spot)
    closes = [spot[t] for t in opens]
    idx = {t: i for i, t in enumerate(opens)}
    fpts = sorted(funding)
    warns: list[str] = []

    def at(end: int, notes: list[str] | None = None) -> dict[str, Any] | None:
        i = idx.get(end - DAY_MS)
        return None if i is None else _components(opens, closes, i, perp, fpts, premium, notes)

    comps = at(day_end, warns)
    state = classify(comps)
    trans = transitions(comps, state)
    closest = trans[0] if trans else None
    trend = trend_ok = None
    if comps and comps["price_to_sma"] is not None:
        trend = "growth" if comps["price_to_sma"] >= 0 else "decline"
        slope = comps["sma_slope_30d"]
        trend_ok = None if slope is None else (slope > 0 if trend == "growth" else slope < 0)
    undecided: list[str] = []
    if comps is None:
        undecided = ["close"]
    elif state is None:
        undecided = sorted({r["component"] for e in evaluate(comps).values() for r in e["conditions"] if r["met"] is None})
    history = []
    for k in range(history_days - 1, -1, -1):
        end = day_end - k * DAY_MS
        c = at(end)
        history.append({"date": _date(end - DAY_MS), "state": classify(c), "close": c["close"] if c else None})
    return {
        "date": _date(day_end - DAY_MS),
        "close_utc": datetime.fromtimestamp(day_end / 1000, UTC).isoformat(timespec="seconds"),
        "state": state,
        "state_ua": STATE_UA.get(state) if state else None,
        "undecided_by": undecided,
        "trend": trend,
        "trend_confirmed": trend_ok,
        "components": comps,
        "rules": evaluate(comps) if comps else None,
        "closest": closest,
        "transitions": trans,
        "line_ua": line_ua(comps, state, closest),
        "history": history,
        "warnings": warns,
    }
