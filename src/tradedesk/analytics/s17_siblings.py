"""S17 sibling signals: several POB retest limits from one breakout candle (same coin, side and candle).

Pure helpers for ``scripts/verify_s17_multi_signal.py`` (registration in the card IDEA-20261030-demo-setups-depth,
item «S17, кілька сигналів однієї свічки пробою», 2026-11-24): group rows by key, rank the siblings of a key by the
distance of their level from the close of the breakout candle, match passport rows to re-derived signals, parse the
close from a live signal comment, compare which sibling two selection rules keep. No I/O; every result for a key
depends only on the rows of that key.
"""

from __future__ import annotations

import math
import re
from collections.abc import Hashable, Sequence

import numpy as np

_CLOSE_RE = re.compile(r"закрилась на (-?[\d,]*\.?\d+(?:[eE][+-]?\d+)?)")


def group_ids(keys: Sequence[Hashable]) -> np.ndarray:
    """Integer id of each row's key, numbered in order of first appearance."""
    seen: dict[Hashable, int] = {}
    return np.array([seen.setdefault(k, len(seen)) for k in keys], dtype=np.int64)


def group_sizes(groups: np.ndarray) -> np.ndarray:
    """Number of rows of each row's group."""
    g = np.asarray(groups, dtype=np.int64)
    if g.size == 0:
        return np.zeros(0, dtype=np.int64)
    return np.bincount(g)[g]


def level_distance(side: str, level: float, close: float) -> float:
    """Distance of a resting entry ``level`` from the ``close`` of the breakout candle as a fraction of the close
    (0.05 = 5 %): positive when the close is beyond the level on the signal's side (above it for a long, below for a
    short), i.e. the limit is still waiting for a retest."""
    if not (close > 0 and math.isfinite(level) and math.isfinite(close)):
        return math.nan
    return (close - level) / close if side == "long" else (level - close) / close


def rank_within(groups: np.ndarray, values: Sequence[float]) -> np.ndarray:
    """Rank of each row's ``value`` inside its group, 1 = smallest; ties share the smallest rank (1, 1, 3); NaN
    values get rank 0 and do not take a place."""
    g = np.asarray(groups, dtype=np.int64)
    v = np.asarray(values, dtype=float)
    out = np.zeros(g.size, dtype=np.int64)
    for gid in np.unique(g):
        idx = np.flatnonzero(g == gid)
        vals = v[idx]
        ok = np.isfinite(vals)
        srt = np.sort(vals[ok])
        for i, x in zip(idx, vals, strict=True):
            if math.isfinite(x):
                out[i] = int(np.searchsorted(srt, x, side="left")) + 1
    return out


def match_subsequence(rows: Sequence[tuple[str, int, float]], sigs: Sequence[tuple[str, int, float]], tol: float = 1e-9) -> np.ndarray:
    """Index into ``sigs`` of each row, where the rows are an ordered subsequence of the signals (a passport row per
    kept signal, in signal order). Keys are (side, entry time, stop fraction); stop fractions equal within ``tol``
    (relative). Greedy: each row takes the next unused matching signal; -1 if none is left."""
    out = np.full(len(rows), -1, dtype=np.int64)
    j = 0
    for i, (sd, t, sp) in enumerate(rows):
        k = j
        while k < len(sigs):
            s2, t2, sp2 = sigs[k]
            if s2 == sd and t2 == t and abs(sp2 - sp) <= tol * max(1.0, abs(sp)):
                break
            k += 1
        if k < len(sigs):
            out[i] = k
            j = k + 1
    return out


def parse_break_close(comment: str) -> float | None:
    """Close of the breakout candle from an S17 signal comment («… ця свічка закрилась на 1,234.5, …»); None if
    absent."""
    m = _CLOSE_RE.search(comment or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def pick_comparison(groups: np.ndarray, keep_a: np.ndarray, keep_b: np.ndarray) -> dict[str, int]:
    """Per group, which row two selection rules keep: groups where both keep exactly one row (``both``), and of those
    the ones with different rows (``differ``); groups kept by only one rule (``only_a``, ``only_b``); groups where a
    rule keeps more than one row (``multi``: not expected with one trade per coin)."""
    g = np.asarray(groups, dtype=np.int64)
    a, b = np.asarray(keep_a, dtype=bool), np.asarray(keep_b, dtype=bool)
    res = {"groups": 0, "both": 0, "differ": 0, "only_a": 0, "only_b": 0, "neither": 0, "multi": 0}
    for gid in np.unique(g):
        idx = np.flatnonzero(g == gid)
        ia, ib = idx[a[idx]], idx[b[idx]]
        res["groups"] += 1
        if ia.size > 1 or ib.size > 1:
            res["multi"] += 1
        elif ia.size == 1 and ib.size == 1:
            res["both"] += 1
            res["differ"] += int(ia[0] != ib[0])
        elif ia.size == 1:
            res["only_a"] += 1
        elif ib.size == 1:
            res["only_b"] += 1
        else:
            res["neither"] += 1
    return res
