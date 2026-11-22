"""Carry B criterion (e): is a leg-price gap > 0.5 % an engine defect or a market property (item 383, descriptive).

Pure functions on the engine's candidate rows (``cp_cycle.candidates``: ``price_a`` Binance USD-M, ``price_b`` Bybit,
per cycle) and independent venue bars (``[open_ms, open, high, low, close, volume]``). The registration (card
IDEA-20261102-standing-improvements, «Попередня реєстрація — пункт 383») fixes the thresholds below before the data:

- R1 frozen leg: one leg's price equal over >= ``FROZEN_ROWS`` consecutive rows while the other changes, or the venue's
  bar behind a row has zero volume;
- R2 foreign bar: a row's leg price differs from the venue's own bar closing at the cycle by > ``BAR_TOL`` (relative);
- R4 units: the two prices differ by more than ``RATIO_MAX`` times;
- market subclass by 1m closes over the holding: |median a/b - 1| > ``GAP`` = persistent, else close noise.

A row with a leg price but no venue 4h bar (or a zero close) behind it cannot be checked for R2; such rows are counted
(``no_bar``) and, when no defect flag is found, the verdict is ◯ «R2 unknown» instead of a market class (item 383b).
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

BAR_MS = 4 * 3_600_000
DECISION_DELAY_MS = 300_000  # the engine decides at cycle + 5 min (analytics.carry_rules.DECISION_DELAY_MS)
GAP = 0.005  # criterion (e): |median gap| over the holding (the registered collision metric)
BAR_TOL = 0.001
FROZEN_ROWS = 3
RATIO_MAX = 1.5


def cycle_bar_open(cycle_ts: int, bar_ms: int = BAR_MS) -> int:
    """Open time of the bar that closes last at or before the engine's decision time (cycle + 5 min): the bar the
    engine's price of the row should come from."""
    t = cycle_ts + DECISION_DELAY_MS
    return (t // bar_ms) * bar_ms - bar_ms


def median_gap(gaps: Sequence[float | None]) -> float | None:
    """Signed median of the gaps (None ignored), as the registered metric (``abs`` is taken by the caller)."""
    g = [x for x in gaps if x is not None]
    return statistics.median(g) if g else None


def longest_equal_run(values: Sequence[float | None]) -> int:
    """Longest run of equal consecutive non-None values (1 for any single value, 0 when all are None)."""
    best = run = 0
    prev: float | None = None
    for v in values:
        if v is None:
            run, prev = 0, None
            continue
        run = run + 1 if prev is not None and v == prev else 1
        prev = v
        best = max(best, run)
    return best


def row_checks(
    rows: Sequence[Mapping[str, Any]], bars_a: Mapping[int, Sequence[float]], bars_b: Mapping[int, Sequence[float]]
) -> list[dict[str, Any]]:
    """Per candidate row (``cycle_ts``, ``price_a``, ``price_b``): the venues' own bars closing at the cycle
    (``bars_x`` open_ms -> [open, high, low, close, volume]), the relative deviation of each engine price from that
    close, the bar volumes and the independent gap. A missing bar leaves its fields None."""
    out = []
    for r in rows:
        k = cycle_bar_open(int(r["cycle_ts"]))
        pa, pb = r.get("price_a"), r.get("price_b")
        ba, bb = bars_a.get(k), bars_b.get(k)
        ca = float(ba[3]) if ba else None
        cb = float(bb[3]) if bb else None
        out.append(
            {
                "cycle_ts": int(r["cycle_ts"]), "bar_open": k, "price_a": pa, "price_b": pb,
                "gap": pa / pb - 1.0 if pa and pb else None,
                "close_a": ca, "close_b": cb, "vol_a": float(ba[4]) if ba else None, "vol_b": float(bb[4]) if bb else None,
                "dev_a": pa / ca - 1.0 if pa and ca else None, "dev_b": pb / cb - 1.0 if pb and cb else None,
                "gap_indep": ca / cb - 1.0 if ca and cb else None,
            }
        )  # fmt: skip
    return out


def minute_gap(m1_a: Mapping[int, float], m1_b: Mapping[int, float], start_ms: int, end_ms: int) -> dict[str, Any]:
    """1m close ratio a/b - 1 over the minutes in [start_ms, end_ms) where both venues have a bar: n, signed median,
    share of minutes with |gap| > ``GAP``, and the same median over the minutes that close a 4h bar (``:59`` of
    hours 3, 7, ...) for contrast."""
    gaps = [m1_a[k] / m1_b[k] - 1.0 for k in sorted(m1_a) if start_ms <= k < end_ms and m1_b.get(k) and m1_a[k]]
    closing = [
        m1_a[k] / m1_b[k] - 1.0 for k in sorted(m1_a) if start_ms <= k < end_ms and m1_b.get(k) and m1_a[k] and (k + 60_000) % BAR_MS == 0
    ]
    return {
        "n": len(gaps),
        "median": statistics.median(gaps) if gaps else None,
        "share_over": sum(abs(g) > GAP for g in gaps) / len(gaps) if gaps else None,
        "n_closing": len(closing),
        "median_closing": statistics.median(closing) if closing else None,
    }


def no_bar_rows(checks: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Rows per leg with an engine price but no usable venue 4h bar (missing or zero close): R2 is unknown there."""
    return {leg: sum(1 for c in checks if c[f"price_{leg}"] and not c[f"close_{leg}"]) for leg in ("a", "b")}


def classify(checks: Sequence[Mapping[str, Any]], minute: Mapping[str, Any]) -> dict[str, Any]:
    """Registered verdict of one entry on «engine defect?»: ✓ defect explains the failure, ≈ defect that does not,
    ✗ market («persistent» / «close noise»), ◯ no rows or no independent bars, or no flag but some rows without a
    4h bar («R2 unknown»: a foreign bar could hide there). ``no_bar`` counts such rows per leg and ``r2_unknown`` is
    True when any exist; a ✓ / ≈ verdict then keeps its class with an «R2 unknown on N rows» suffix."""
    no_bar = no_bar_rows(checks)
    r2_unknown = any(no_bar.values())
    if not checks or not any(c["gap_indep"] is not None for c in checks):
        return {"verdict": "◯", "cls": "no data", "flags": [], "no_bar": no_bar, "r2_unknown": r2_unknown}
    flags = []
    for leg in ("a", "b"):
        other = "b" if leg == "a" else "a"
        vals = [c[f"price_{leg}"] for c in checks]
        if longest_equal_run(vals) >= FROZEN_ROWS and len({c[f"price_{other}"] for c in checks}) > 1:
            flags.append(f"R1 frozen {leg}")
        if any(c[f"vol_{leg}"] == 0 for c in checks):
            flags.append(f"R1 zero volume {leg}")
        if any(c[f"dev_{leg}"] is not None and abs(c[f"dev_{leg}"]) > BAR_TOL for c in checks):
            flags.append(f"R2 foreign bar {leg}")
    if any(c["price_a"] and c["price_b"] and max(c["price_a"], c["price_b"]) / min(c["price_a"], c["price_b"]) > RATIO_MAX for c in checks):
        flags.append("R4 units")
    eng = median_gap([c["gap"] for c in checks])
    ind = median_gap([c["gap_indep"] for c in checks])
    out: dict[str, Any] = {
        "flags": flags, "median_engine": eng, "median_indep": ind, "minute_median": minute.get("median"),
        "no_bar": no_bar, "r2_unknown": r2_unknown,
    }  # fmt: skip
    if flags:
        ok = ind is not None and abs(ind) <= GAP
        cls = "engine" if ok else "engine, does not explain"
        if r2_unknown:
            cls += f"; R2 unknown on {no_bar['a']}/{no_bar['b']} rows (a/b) without a 4h bar"
        return out | {"verdict": "✓" if ok else "≈", "cls": cls}
    if r2_unknown:
        return out | {"verdict": "◯", "cls": f"R2 unknown: {no_bar['a']}/{no_bar['b']} rows (a/b) without a 4h bar"}
    mm = minute.get("median")
    if mm is None:
        return out | {"verdict": "✗", "cls": "market (1m unknown)"}
    return out | {"verdict": "✗", "cls": "market, persistent" if abs(mm) > GAP else "market, close noise"}
