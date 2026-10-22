"""Helpers for the out-of-sample test of the structural trail without a target (IDEA-20261022-trail-no-target): the
rank band of a point-in-time volume ranking, the funding paid over a holding interval in R, and tail trimming.

Pure functions, no I/O; time is a parameter. Rates are decimal fractions (0.0001 = 0.01 % per funding interval).
The trade simulator itself is ``stop_management.simulate_stop_rule`` (not duplicated here).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np

from tradedesk.analytics.liquidation_flush import base_of

EIGHT_H_MS = 8 * 3_600_000
MINUTE_MS = 60_000


def dedup_by_base(ranked: Sequence[str]) -> list[str]:
    """Keep the first symbol of each base (``base_of``), in rank order: 1000PEPEUSDT and PEPEUSDT count once."""
    seen: set[str] = set()
    out: list[str] = []
    for s in ranked:
        b = base_of(s)
        if b in seen:
            continue
        seen.add(b)
        out.append(s)
    return out


def rank_band(ranked: Sequence[str], lo: int, hi: int, exclude: Iterable[str] = ()) -> dict[str, int]:
    """Members of 1-based ranks ``lo``..``hi`` of the base-deduplicated ``ranked`` list as {symbol: rank}, without the
    symbols in ``exclude`` and without symbols whose base is the base of an excluded symbol (the ranks are not shifted:
    an excluded symbol keeps its place)."""
    ex = set(exclude)
    ex_bases = {base_of(s) for s in ex}
    out: dict[str, int] = {}
    for i, s in enumerate(dedup_by_base(ranked), 1):
        if i < lo:
            continue
        if i > hi:
            break
        if s in ex or base_of(s) in ex_bases:
            continue
        out[s] = i
    return out


def funding_paid(
    fund_t: np.ndarray,
    fund_rate: np.ndarray,
    entry_ms: int,
    exit_ms: int,
    side: int,
    flat_8h: float = 0.0001,
) -> tuple[float, str]:
    """Funding paid per unit of notional by a position held over (``entry_ms``, ``exit_ms``) as a decimal fraction
    (positive = paid, negative = received), and the source used.

    ``side`` = +1 for a long (pays a positive rate), -1 for a short. Funding times are floored to the minute (venue
    stamps carry a few ms of jitter) and count when strictly inside the holding interval. When the history does not
    cover the entry (``fund_t`` empty or starting after ``entry_ms``), the flat assumption ``flat_8h`` per 8 h against
    the position is used instead (source ``flat``). Only funding times before ``exit_ms`` are read."""
    if exit_ms <= entry_ms:
        return 0.0, "none"
    if fund_t.size == 0 or int(fund_t[0]) - int(fund_t[0]) % MINUTE_MS > entry_ms:
        return flat_paid(entry_ms, exit_ms, flat_8h), "flat"
    tf = fund_t - fund_t % MINUTE_MS
    m = (tf > entry_ms) & (tf < exit_ms)
    return float(side * np.sum(fund_rate[m])), "actual"


def flat_paid(entry_ms: int, exit_ms: int, flat_8h: float = 0.0001) -> float:
    """Flat funding against the position: ``flat_8h`` per 8 h held, pro rata."""
    return max(0, exit_ms - entry_ms) / EIGHT_H_MS * flat_8h


def drop_best(r: np.ndarray, share: float) -> np.ndarray:
    """Boolean mask that removes the best ``ceil(share * n)`` values of ``r`` (ties broken by position, stable)."""
    n = r.size
    k = int(np.ceil(share * n)) if n else 0
    keep = np.ones(n, dtype=bool)
    if k:
        keep[np.argsort(-r, kind="stable")[:k]] = False
    return keep
