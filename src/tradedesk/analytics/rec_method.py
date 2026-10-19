"""The assistant's recommendation method as one daily decision (pure, no I/O; IDEA-20261018-assistant).

Same logic as the walk-forward registered in docs/verification/2026-10-18-assistant-recommendations.md: on a day's decision
price the structures, replay each over every past window of its length that started in the same fixed RV30 bucket as
today, and pick the structure with the highest mean / |5 % quantile| among those with mean > 0 and a CI lower bound > 0
(`option_replay.recommend`), else no trade. The caller must pass the SAME candidate set as the registered test (11
structures: nine on the ~40-day expiry and ``long_straddle_14d`` / ``long_strangle25_14d`` on the ~14-day one); this module
does not choose them. ``mean`` and ``q05`` come out as `option_replay.method_summary` returns them: fractions of the ENTRY
FORWARD (not spot); callers store them as they are.

``decide`` is the journal rule "a new trade only when nothing stands, the regime or an event changes":
``actual`` (the standing paper trade is the method's pick, or a fresh one is opened because nothing stood), ``changed`` (the
method now prefers something else, or the regime / an event moved) and ``no_longer_actual`` (the method picks nothing today).
A trade identical to the standing one (structure and expiry) is never opened again. ``trigger`` is 'chain' for the first
trade and for one after an expiry (the walk-forward's chain; also after days without a pick, which the test would skip),
'regime' or 'event' for extra trades that overlap the chain.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .option_replay import Leg, method_summary, recommend, rv_at, window_starts

RV_BUCKETS = (0.25, 0.40, 0.60)  # fixed RV30 buckets of the registered method: <25, 25-40, 40-60, >=60 %
FEE_LEG = 0.0003  # method fee, per leg each way
REGIME_MARGIN = 0.02  # RV30 must clear the bucket edge by 2 vol points to switch the regime at once


def bucket(rv: float | None) -> int | None:
    return None if rv is None else sum(rv >= e for e in RV_BUCKETS)


def regime_label(b: int | None) -> str | None:
    return None if b is None else f"rv30_b{b}"


def method_pick(structures: dict[str, tuple[Sequence[Leg], float]], closes: Sequence[float]) -> dict[str, Any]:
    """The method's pick from ``{name: (legs relative to the forward, T in years)}`` and daily closes known by today.

    ``closes[-1]`` must be the last COMPLETE daily close (yesterday's); returns ``pick`` (name or None), its
    ``summary`` (``mean``, ``q05``, ... as fractions of the entry forward), ``regime`` and ``rv30``. With no usable
    RV30 or a window history too short, ``pick`` is None.
    """
    end = len(closes) - 1
    rv_now = rv_at(closes, end)
    b_now = bucket(rv_now)
    out: dict[str, Any] = {"pick": None, "summary": None, "regime": regime_label(b_now), "rv30": rv_now, "summaries": {}}
    if b_now is None:
        return out
    rv = [rv_at(closes, i) for i in range(len(closes))]
    cache: dict[int, list[int]] = {}
    for name, (rel, t_years) in structures.items():
        h = max(1, round(t_years * 365))
        if h not in cache:
            cache[h] = window_starts(end, h, lambda s: bucket(rv[s]) == b_now)
        out["summaries"][name] = method_summary(rel, closes, h, cache[h], t_years=t_years, fee_per_leg=FEE_LEG)
    pick = recommend(out["summaries"])
    out["pick"] = pick
    out["summary"] = out["summaries"][pick] if pick else None
    return out


def _bucket_of(label: str | None) -> int | None:
    if not label or not label.startswith("rv30_b"):
        return None
    try:
        return int(label[len("rv30_b") :])
    except ValueError:
        return None


def regime_confirmed(live_regime: str | None, regime: str | None, rv30: float | None, prev_regime: str | None) -> bool:
    """Hysteresis: a bucket different from the standing trade's counts as a regime change only if it held on two decisions
    in a row (``prev_regime`` is yesterday's) or RV30 is at least `REGIME_MARGIN` beyond the nearest bucket edge."""
    if regime is None or regime == live_regime:
        return False
    if prev_regime == regime:
        return True
    old, new = _bucket_of(live_regime), _bucket_of(regime)
    if old is None or new is None or rv30 is None:
        return True  # cannot judge the margin: do not suppress
    if new > old:
        return rv30 - RV_BUCKETS[old] >= REGIME_MARGIN
    return RV_BUCKETS[old - 1] - rv30 >= REGIME_MARGIN


def decide(
    *,
    pick: str | None,
    pick_expiry_ts: int | None,
    regime: str | None,
    rv30: float | None = None,
    prev_regime: str | None = None,
    event_key: str | None,
    last: dict[str, Any] | None,
    now_ts: int,
    seen_events: set[str],
    standing: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Whether to book a new paper trade today, why, and which state the standing recommendation is in.

    ``pick`` is the structure to book (base name) on the expiry ``pick_expiry_ts``; ``standing`` is every ``source = 'method'``
    row, newest first (needs ``structure``, ``expiry_ts``, ``regime``, ``trigger``); only the not-yet-expired ones count, and a pick
    equal to ANY of them is never duplicated. ``last`` (newest row or None) is the single-row shorthand when ``standing`` is None. ``event_key`` identifies the upcoming
    high-impact event (None when none); ``seen_events`` are keys already used. Returns ``action`` ('open' | 'hold' | 'none'),
    ``state``, ``reason`` and, for 'open', ``trigger`` ('chain' | 'regime' | 'event').
    """
    history = standing if standing is not None else ([last] if last is not None else [])
    live_rows = [x for x in history if x["expiry_ts"] > now_ts]  # newest first
    live = live_rows[0] if live_rows else None
    if pick is None:
        return {"action": "none", "state": "no_longer_actual", "reason": "the method picks no trade today"}
    if live is None:
        return {"action": "open", "state": "actual", "reason": "expiry" if history else "first", "trigger": "chain"}
    if any(pick == x["structure"] and pick_expiry_ts == x["expiry_ts"] for x in live_rows):
        return {"action": "hold", "state": "actual", "reason": "a standing paper trade is the method's pick (same structure and expiry)"}
    if not any(x.get("trigger") in (None, "chain") for x in live_rows):
        # only triggered trades stand: the chain (comparable with H1) has no live trade of its own, so it continues
        return {"action": "open", "state": "actual", "reason": "expiry", "trigger": "chain"}
    if regime_confirmed(live.get("regime"), regime, rv30, prev_regime):
        return {"action": "open", "state": "changed", "reason": "regime", "trigger": "regime"}
    if event_key is not None and event_key not in seen_events:
        return {"action": "open", "state": "changed", "reason": "event", "trigger": "event"}
    return {
        "action": "hold",
        "state": "changed",
        "reason": "the method prefers another structure; no new trade (no confirmed regime change, no new event, no expiry)",
    }
