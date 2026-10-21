"""Scoring of the assistant's trade plans on OHLCV candles (local bookkeeping only, no orders).

Pure functions, no I/O; the current time is passed in. A plan is a side, an entry, a stop, a target and a horizon
(``expires_ts``). It is resolved on complete candles of one timeframe that open at or after the plan was created (the
candle the plan was written in is skipped: we cannot tell which of its prints came after the plan).

Conventions (all paper, on candle highs / lows, no fill guarantee):
- Fill. 'market': filled at ``entry`` at creation. 'limit': long fills when a candle's low <= entry, short when its high
  >= entry, at ``entry`` (a gap through the limit would fill better; we keep ``entry``). 'stop': long fills when the high
  >= entry, short when the low <= entry, at the worse of ``entry`` and the candle open (a gap through a stop fills worse).
- Exit. After the fill, the first candle whose low (long) / high (short) reaches the stop exits at the stop, or at the
  open when the candle gapped past it; the first candle reaching the target exits at the target. A candle that touches
  both is scored as the stop (the intra-candle order is unknown; conservative).
- The fill candle. Its stop touch counts (for a limit entry the price must pass the entry before the stop; for a stop
  entry the order is unknown and the worse case is taken); its target touch does not (it may have printed before the
  fill). So a limit / stop plan can never reach its target inside the candle it was filled in.
- Horizon. Only candles that close by ``expires_ts`` are used. A filled plan with no touch is 'expired' at the close of
  the last such candle; an unfilled one is 'not_filled' (R None). A plan whose horizon passed stays open until the candle
  ending at the horizon is in the data.
- R = side * (exit - fill) / |entry - stop| minus a flat taker fee on both sides (``fee`` * (fill + exit) / |entry -
  stop|), so R is net of fees and in units of the planned risk. No slippage beyond the gap rules, no funding.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .smart_money import t975

TAKER_FEE = 0.0005  # per side, decimal; a typical USDT-perp taker fee (Binance / Bybit VIP0)
OUTCOMES = ("target", "stop", "expired", "not_filled")
VERDICTS = ("enter", "wait", "skip")


def validate_levels(side: str, entry: float, stop: float, target: float) -> str | None:
    """Error text when the stop is not on the losing side of the entry or the target not on the winning side, else None."""
    if side not in ("long", "short"):
        return f"side must be 'long' or 'short', got {side!r}"
    if min(entry, stop, target) <= 0:
        return "entry, stop and target must be positive prices"
    if side == "long" and not stop < entry < target:
        return f"long plan needs stop < entry < target, got stop {stop:g}, entry {entry:g}, target {target:g}"
    if side == "short" and not target < entry < stop:
        return f"short plan needs target < entry < stop, got target {target:g}, entry {entry:g}, stop {stop:g}"
    return None


def geometry(entry: float, stop: float, target: float, fee: float = TAKER_FEE) -> dict[str, float]:
    """Risk and reward distances as fractions of the entry, reward:risk, and the R a round trip of fees costs."""
    risk = abs(entry - stop)
    reward = abs(target - entry)
    return {
        "risk_pct": risk / entry,
        "reward_pct": reward / entry,
        "rr": reward / risk,
        "fee_r_round_trip": fee * 2 * entry / risk,
    }


def r_multiple(side: str, entry: float, stop: float, fill: float, exit_: float, fee: float = TAKER_FEE) -> float:
    """Net R: signed move from the fill to the exit over the planned risk |entry - stop|, minus taker fees on both sides."""
    sign = 1.0 if side == "long" else -1.0
    risk = abs(entry - stop)
    return (sign * (exit_ - fill) - fee * (fill + exit_)) / risk


def _bars(rows: Iterable[Sequence[Any]]) -> list[tuple[int, float, float, float, float]]:
    """ccxt rows [ts_ms, o, h, l, c, ...] -> sorted unique (ts_ms, o, h, l, c); malformed rows are dropped."""
    out: dict[int, tuple[int, float, float, float, float]] = {}
    for x in rows:
        try:
            ts, o, h, lo, c = int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])
        except (TypeError, ValueError, IndexError):
            continue
        if min(o, h, lo, c) > 0 and h >= lo:
            out[ts] = (ts, o, h, lo, c)
    return [out[k] for k in sorted(out)]


def resolve_plan(
    plan: Mapping[str, Any], rows: Iterable[Sequence[Any]], *, tf_ms: int, now_ts: int, fee: float = TAKER_FEE
) -> dict[str, Any]:
    """Score one plan on candles (see the module docstring for the conventions).

    ``plan`` needs side, entry_type, entry, stop, target, created_ts, expires_ts (unix seconds). Returns ``status``
    ('open' | 'resolved'), ``outcome``, ``filled_ts``, ``fill``, ``resolved_ts``, ``exit``, ``r_result`` (net R) and
    ``r_gross``; for an open plan the fill fields are set once the entry was touched.
    """
    side, etype = str(plan["side"]), str(plan["entry_type"])
    entry, stop, target = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    created_ms, exp_ms, now_ms = int(plan["created_ts"]) * 1000, int(plan["expires_ts"]) * 1000, int(now_ts) * 1000
    long = side == "long"
    start = -(-created_ms // tf_ms) * tf_ms  # first candle opening at or after creation
    horizon = min(exp_ms, now_ms)
    window = [b for b in _bars(rows) if b[0] >= start and b[0] + tf_ms <= horizon]

    out: dict[str, Any] = {"status": "open", "outcome": None, "filled_ts": None, "fill": None, "resolved_ts": None, "exit": None}
    out.update(r_result=None, r_gross=None)
    filled = etype == "market"
    fill = entry if filled else None
    if filled:
        out.update(filled_ts=int(plan["created_ts"]), fill=entry)

    def done(outcome: str, exit_: float | None, ts_ms: int) -> dict[str, Any]:
        out.update(status="resolved", outcome=outcome, exit=exit_, resolved_ts=ts_ms // 1000)
        if fill is not None and exit_ is not None:
            sign = 1.0 if long else -1.0
            out.update(r_gross=sign * (exit_ - fill) / abs(entry - stop), r_result=r_multiple(side, entry, stop, fill, exit_, fee))
        return out

    for ts, o, h, lo, _c in window:
        end = ts + tf_ms
        stop_hit = lo <= stop if long else h >= stop
        if fill is None:
            if etype == "limit":
                touched, px = (lo <= entry, entry) if long else (h >= entry, entry)
            else:  # stop entry: a gap through it fills at the open
                touched, px = (h >= entry, max(entry, o)) if long else (lo <= entry, min(entry, o))
            if not touched:
                continue
            fill = px
            out.update(filled_ts=ts // 1000, fill=px)
            if stop_hit:  # the fill candle: the stop counts (worse case), the target does not
                return done("stop", stop, end)
            continue
        if stop_hit:  # a candle touching both is the stop
            return done("stop", min(stop, o) if long else max(stop, o), end)
        if (h >= target) if long else (lo <= target):
            return done("target", target, end)

    if now_ms < exp_ms:
        return out
    last_needed = (exp_ms // tf_ms) * tf_ms - tf_ms  # open of the last candle that closes by the horizon
    if last_needed >= start and (not window or window[-1][0] < last_needed):
        return out  # the horizon passed, but its last candle is not in the data yet
    if fill is None:
        return done("not_filled", None, exp_ms)
    return done("expired", window[-1][4] if window else fill, exp_ms)


def _stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """n plans, fills, outcome counts, hit rate (targets / filled), win rate (R > 0) and mean / median R with a t 95 % CI."""
    filled = [x for x in rows if x.get("outcome") in ("target", "stop", "expired")]
    rs = [float(x["r_result"]) for x in filled if x.get("r_result") is not None]
    s: dict[str, Any] = {
        "n": len(rows),
        "n_filled": len(filled),
        "outcomes": {k: sum(1 for x in rows if x.get("outcome") == k) for k in OUTCOMES},
        "hit_rate": sum(1 for x in filled if x["outcome"] == "target") / len(filled) if filled else None,
        "win_rate": sum(1 for v in rs if v > 0) / len(rs) if rs else None,
        "mean_r": statistics.fmean(rs) if rs else None,
        "median_r": statistics.median(rs) if rs else None,
        "ci95_mean_r": None,
    }
    if len(rs) >= 2:
        h = t975(len(rs) - 1) * statistics.stdev(rs) / len(rs) ** 0.5
        s["ci95_mean_r"] = [s["mean_r"] - h, s["mean_r"] + h]
    return s


def summarize_plans(plans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Resolved plans grouped by verdict (enter / wait / skip; wait and skip are counterfactual: 'would it have worked?')
    and by tag within verdict. Plans of different verdicts are never pooled."""
    done = [x for x in plans if x.get("status") == "resolved"]
    by_verdict = {v: _stats([x for x in done if x.get("verdict") == v]) for v in VERDICTS}
    by_tag: dict[str, dict[str, Any]] = {}
    for tag in sorted({t for x in done for t in (x.get("tags") or [])}):
        tagged = [x for x in done if tag in (x.get("tags") or [])]
        by_tag[tag] = {v: _stats(g) for v in VERDICTS if (g := [x for x in tagged if x.get("verdict") == v])}
    return {"by_verdict": by_verdict, "by_tag": by_tag}
