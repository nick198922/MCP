"""Post-deploy check of item 8a: WebSocket streams over IPv4 (standing-improvements card, «8a-перевірка»).

The rules were registered before the deploy in docs/changes/2026-11-25-ws-stream-stalls.md, sections «Критерій перевірки
після деплою» and «Умова відкату»; this module only implements them, it sets no threshold of its own. Pure functions over
the hourly connection journals (``liquidation_stream_hour`` by exchange, ``book_stream_hour`` by stream) and over a
``system_health`` answer; the clock (``now``, unix s) is always passed in.

Journal shape: ``{stream: {hour_ts: (connected_s, failures)}}`` with the liquidation exchanges ('binance', 'bybit',
'okx') and the book streams ('binance:depth', 'bybit:book', ...) side by side.

Readings where the registered text leaves room (the conservative one is taken, i.e. the one that checks more):
- rollback (a), hourly ratios: every clean hour from H (not only from H + 2 h, where the verdict window starts);
- "Binance <= 0.2 failures/h in the same hours" (a rollback is not one when Binance fails too): for (a) ratios each
  flagged hour on its own (not pooled), for (a) health the hour of the reading and the hour before it, for (b) its 12
  hours;
- a clean hour without a row of the checked stream had no connection: connected 0 s, failures 0;
- the 168-hour window, like the 48-hour one, is extended hour by hour until it has its 126 clean hours;
- an invalid window restarts at the second hour after the last hour of that window with Binance failures (any hour,
  clean or not), with the same rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

HOUR = 3600
COMPLETE_LAG_S = 15 * 60  # an hour counts once hour_ts + 3600 <= now - 15 min (its last 600-s window is written)
RESTART_CONNECTED_S = 3300.0  # Binance liquidations below this (or no row) = a restart hour
WINDOW_HOURS, MIN_CLEAN = 48, 36
EXTENDED_HOURS, MIN_CLEAN_EXTENDED = 168, 126
WINDOW_OFFSET_HOURS = 2  # the window starts at H + 2 h
CONFIRM_RATE, CONFIRM_SHARE, REFUTE_RATE = 0.3, 0.2, 1.5
CONTROL_RATE = 0.2  # Binance failures/h above this = a shared network or venue incident
RATIO_MIN = 0.9  # rollback (a): connected_s below 0.9 x the reference stream of the same clean hour
HEALTH_MAX_CONSECUTIVE = 5  # rollback (a): consecutive_failures >= 5
B_HOURS, B_NOT_BEFORE_HOURS = 12, 14  # rollback (b): the first 12 clean hours from H + 2 h, not before H + 14 h
B_LIMITS = {"bybit": 3.9, "okx": 4.5}  # rollback (b): failures/h above these (1.5 x the base)

LIQ_REF = "binance"
TARGETS = ("bybit", "okx")
BOOK_REF = "binance:depth"
BOOK_TARGETS = ("bybit:book", "coinbase:book")
BOOK_DESCRIPTIVE = ("bybit:book", "coinbase:book", "binance:depth", "binance:trades", "binance_spot:book")
HEALTH_STREAMS = (("liquidations:bybit", None), ("liquidations:okx", None), ("book_walls:coinbase", "book"))

Journal = Mapping[str, Mapping[int, tuple[float, int]]]


def complete(hour_ts: int, now: float) -> bool:
    """The hour is over and its last collector window is written: hour_ts + 3600 <= now - 15 min."""
    return hour_ts + HOUR <= now - COMPLETE_LAG_S


def is_restart(binance: Mapping[int, tuple[float, int]], hour_ts: int) -> bool:
    """A restart hour: the Binance liquidations row is missing or has connected_s < 3300."""
    row = binance.get(hour_ts)
    return row is None or row[0] < RESTART_CONNECTED_S


def clean_hours(journal: Journal, start: int, end: int, now: float) -> list[int]:
    """Complete hours in [start, end) that are neither a restart hour nor the hour right after one."""
    binance = journal.get(LIQ_REF, {})
    return [h for h in range(start, end, HOUR) if complete(h, now) and not is_restart(binance, h) and not is_restart(binance, h - HOUR)]


def rate(stream: Mapping[int, tuple[float, int]], hours: list[int]) -> dict[str, Any]:
    """Failures per hour (sum / hours) and the share of hours with a failure; None rates without hours."""
    f = [stream.get(h, (0.0, 0))[1] for h in hours]
    n = len(hours)
    return {
        "hours": n,
        "failures": sum(f),
        "rate": sum(f) / n if n else None,
        "share": sum(1 for x in f if x > 0) / n if n else None,
    }


def _ratio(journal: Journal, target: str, ref: str, h: int) -> float | None:
    r = journal.get(ref, {}).get(h)
    if r is None or r[0] <= 0:
        return None
    return journal.get(target, {}).get(h, (0.0, 0))[0] / r[0]


def rollback_a_hours(journal: Journal, hours: list[int]) -> dict[str, Any]:
    """Rollback (a), journal part: a clean hour where Bybit / OKX liquidations are connected < 0.9 x Binance, or
    ``bybit:book`` / ``coinbase:book`` < 0.9 x ``binance:depth``. Each flagged hour is exempt on its own when Binance
    liquidations fail > 0.2/h in that hour (shared incident); ``triggered`` if any flagged hour is not exempt. Hours
    without a ``binance:depth`` row cannot be checked and are listed."""
    pairs = [(t, LIQ_REF) for t in TARGETS] + [(t, BOOK_REF) for t in BOOK_TARGETS]
    low: list[tuple[int, str, float]] = []
    no_ref: list[tuple[int, str]] = []
    minimum: dict[str, float | None] = {t: None for t, _ in pairs}
    for h in hours:
        for t, ref in pairs:
            x = _ratio(journal, t, ref, h)
            if x is None:
                no_ref.append((h, t))
                continue
            minimum[t] = x if minimum[t] is None else min(minimum[t], x)
            if x < RATIO_MIN:
                low.append((h, t, x))
    flagged = sorted({h for h, _, _ in low})
    binance = journal.get(LIQ_REF, {})
    control = rate(binance, flagged)
    # The exemption holds hour by hour: one hour is a shared incident only if Binance itself failed > 0.2/h in it,
    # i.e. at least once. Pooling the flagged hours would let one Binance failure excuse several clean outages.
    exempt = [x for x in flagged if rate(binance, [x])["rate"] > CONTROL_RATE]
    left = [x for x in flagged if x not in exempt]
    return {
        "hours": len(hours),
        "min_ratio": minimum,
        "low": low,
        "no_reference": no_ref,
        "binance_in_flagged": control,
        "exempt_hours": exempt,
        "shared_incident": bool(exempt),
        "triggered": bool(left),
    }


def health_streams(data: Mapping[str, Any]) -> dict[str, dict[str, Any] | None]:
    """``consecutive_failures`` and ``peer`` of the three checked streams from a ``system_health`` ``data`` object
    (``collector[].stream``, the Coinbase book under ``stream.book``); None for a stream the answer does not have."""
    jobs = {j.get("name"): j for j in data.get("collector") or [] if isinstance(j, Mapping)}
    out: dict[str, dict[str, Any] | None] = {}
    for name, sub in HEALTH_STREAMS:
        st = (jobs.get(name) or {}).get("stream")
        if sub and isinstance(st, Mapping):
            st = st.get(sub)
        out[name] = (
            {"consecutive_failures": int(st.get("consecutive_failures") or 0), "peer": st.get("peer")} if isinstance(st, Mapping) else None
        )
    return out


def rollback_a_health(streams: Mapping[str, dict[str, Any] | None], binance_rate: float | None) -> dict[str, Any]:
    """Rollback (a), ``system_health`` part: ``consecutive_failures`` >= 5 in any checked stream, exempt when Binance
    fails > 0.2/h in the same hours (``binance_rate``). Also the deploy check: ``peer`` present and without ':'."""
    hits = sorted(n for n, s in streams.items() if s and s["consecutive_failures"] >= HEALTH_MAX_CONSECUTIVE)
    absent = sorted(n for n, s in streams.items() if s is None)
    not_ipv4 = sorted(n for n, s in streams.items() if s and (not s.get("peer") or ":" in str(s["peer"])))
    shared = bool(hits) and (binance_rate or 0.0) > CONTROL_RATE
    return {
        "consecutive_hits": hits,
        "absent": absent,
        "not_ipv4": not_ipv4,
        "deploy_confirmed": not absent and not not_ipv4,
        "binance_rate": binance_rate,
        "shared_incident": shared,
        "triggered": bool(hits) and not shared,
    }


def rollback_b(journal: Journal, h: int, now: float) -> dict[str, Any]:
    """Rollback (b), once: the first 12 clean hours from H + 2 h (not before H + 14 h), Bybit > 3.9 or OKX > 4.5
    failures/h; exempt when Binance fails > 0.2/h in those hours. Before that: ``pending`` with the hours so far and
    ``certain`` = streams whose failures so far already exceed the 12-hour limit."""
    start = h + WINDOW_OFFSET_HOURS * HOUR
    hours = clean_hours(journal, start, int(now) // HOUR * HOUR + HOUR, now)[:B_HOURS]
    ready = len(hours) == B_HOURS and now >= h + B_NOT_BEFORE_HOURS * HOUR
    per = {t: rate(journal.get(t, {}), hours) for t in TARGETS}
    control = rate(journal.get(LIQ_REF, {}), hours)
    over = [t for t in TARGETS if per[t]["failures"] > B_LIMITS[t] * B_HOURS]
    shared = (control["rate"] or 0.0) > CONTROL_RATE
    return {
        "ready": ready,
        "hours": hours,
        "per": per,
        "binance": control,
        "over": over if ready else [],
        "certain": [] if ready else over,
        "shared_incident": ready and bool(over) and shared,
        "triggered": ready and bool(over) and not shared,
    }


def _window(journal: Journal, start: int, length: int, min_clean: int, now: float) -> dict[str, Any]:
    """One verdict window [start, start + length h), extended hour by hour until it has ``min_clean`` clean hours.
    Rates are computed only when ``ready``: the registered end (+ 15 min) has passed and the clean hours are there."""
    end = start + length * HOUR
    clean = clean_hours(journal, start, end, now)
    while len(clean) < min_clean and complete(end, now):
        end += HOUR
        clean = clean_hours(journal, start, end, now)
    ready = complete(start + (length - 1) * HOUR, now) and len(clean) >= min_clean
    res: dict[str, Any] = {
        "start": start,
        "end": end,
        "length_hours": length,
        "min_clean": min_clean,
        "clean": len(clean),
        "earliest": start + length * HOUR + COMPLETE_LAG_S,
        "ready": ready,
    }
    if not ready:
        return res
    control = rate(journal.get(LIQ_REF, {}), clean)
    res["binance"] = control
    if (control["rate"] or 0.0) > CONTROL_RATE:
        binance = journal.get(LIQ_REF, {})
        last = max(x for x in range(start, end, HOUR) if binance.get(x, (0.0, 0))[1] > 0)
        res.update(outcome="invalid", new_start=last + 2 * HOUR)
        return res
    per = {t: rate(journal.get(t, {}), clean) for t in TARGETS}
    res["per"] = per
    res["books"] = {s: rate(journal.get(s, {}), clean) for s in BOOK_DESCRIPTIVE}
    if all(p["rate"] <= CONFIRM_RATE and p["share"] <= CONFIRM_SHARE for p in per.values()):
        res["outcome"] = "confirmed"
    elif all(p["rate"] >= REFUTE_RATE for p in per.values()):
        res["outcome"] = "refuted"
    else:
        res["outcome"] = "between"
    return res


def verdict(journal: Journal, h: int, now: float, max_windows: int = 50) -> dict[str, Any]:
    """The full verdict: the 48-hour window [H + 2 h, H + 50 h) with >= 36 clean hours; between the thresholds -> the
    168-hour window [H + 2 h, H + 170 h) with >= 126, still between -> ``partial effect``; Binance > 0.2/h -> the
    window is invalid and a new one starts. ``state`` is ``wait`` (nothing computed yet: no rates in the answer),
    ``extend to 168 h`` (48 h between, the long window not ready), or the final outcome."""
    start = h + WINDOW_OFFSET_HOURS * HOUR
    invalid: list[dict[str, Any]] = []
    for _ in range(max_windows):
        w = _window(journal, start, WINDOW_HOURS, MIN_CLEAN, now)
        if not w["ready"]:
            return {"state": "wait", "window": w, "invalid": invalid}
        if w["outcome"] == "invalid":
            invalid.append(w)
            start = w["new_start"]
            continue
        if w["outcome"] != "between":
            return {"state": w["outcome"], "window": w, "invalid": invalid}
        w2 = _window(journal, start, EXTENDED_HOURS, MIN_CLEAN_EXTENDED, now)
        if not w2["ready"]:
            return {"state": "extend to 168 h", "window": w, "extended": w2, "invalid": invalid}
        if w2["outcome"] == "invalid":
            invalid.append(w2)
            start = w2["new_start"]
            continue
        state = "partial effect" if w2["outcome"] == "between" else w2["outcome"]
        return {"state": state, "window": w, "extended": w2, "invalid": invalid}
    return {"state": "wait", "window": None, "invalid": invalid}
