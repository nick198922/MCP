"""Course setup S17 (IDEA-20261016-course-setups-crypto, step 5): the retest of an impulsively broken POB.

A POB ("point of breakdown") is the last candle before a sharp impulse. Everything is written in the "long frame": the
POB precedes a drop, the price later closes back above the POB (the break) and a buy limit waits at the top of the POB
for the retest. The opposite setup (a POB before a pump, broken downwards, a short on the retest) is the same logic on
negated prices (``Bars.mirrored``); the drop threshold uses ``|close|`` so it mirrors into a rise of the same size.

Pure functions, no I/O. Bars are ``course_s14_ema.Bars``; ``pos`` is the position of each bar inside its data segment
(``course_s18_trend.pos_from_seg``). Detection is causal: a POB is known at the close of its impulse bar ``q`` and uses
bars <= q only; the break and its flags are known at the close of the break bar ``k`` and use bars <= k only;
``confirm`` is known at ``k + 1`` (``tests/test_course_pob.py`` checks that later bars do not change them). The trade
itself is ``course_fvg_ob.limit_long`` (step 4) and looks forward by design.

Prices are in quote currency; thresholds are decimal fractions (0.10 = 10 %).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars

__all__ = ["PobEvent", "pob_events", "pob_stop"]


@dataclass(frozen=True)
class PobEvent:
    """A POB at bar ``p`` found by the impulse bar ``q``; ``k`` is the first close above ``high[p]`` within the break
    window (-1: none). ``impulsive``: open[k] <= low[p] or the bullish body of k >= ``imp_atr`` * ATR[k-1].
    ``confirm``: close[k+1] > high[p] as well (known at k + 1). ``drop``: (close[p] - low[q]) / |close[p]|."""

    p: int
    q: int
    k: int
    impulsive: bool
    confirm: bool
    high: float
    low: float
    body_top: float
    drop: float

    @property
    def levels(self) -> dict[str, float]:
        return {"wick": self.high, "body": self.body_top, "mid": (self.high + self.low) / 2}


def pob_events(
    b: Bars,
    pos: np.ndarray,
    atr: np.ndarray,
    *,
    drop: float = 0.10,
    look: int = 5,
    atr_mult: float = 3.0,
    break_bars: int = 180,
    imp_atr: float = 1.0,
    warmup: int = 100,
) -> list[PobEvent]:
    """POBs before drops and their first break upwards.

    On bar q, p is the bar with the highest close among q - look .. q - 1 of the same segment (the later one on ties).
    It is an impulse if close[p] - low[q] >= drop * |close[p]| and >= atr_mult * ATR[p]; each p counts once, on its
    first such q, and only if p is at least the ``warmup``-th bar of its segment. The break k is the first bar in
    q + 1 .. q + break_bars of the same segment with close[k] > high[p]."""
    n = len(b)
    out: list[PobEvent] = []
    seen: set[int] = set()
    c, h, lo, o = b.c, b.h, b.low, b.o
    for q in range(1, n):
        back = int(min(look, pos[q]))
        if back < 1:
            continue
        w = c[q - back : q]
        p = q - back + (back - 1 - int(np.argmax(w[::-1])))  # latest maximum
        if p in seen or pos[p] < warmup:
            continue
        a = float(atr[p])
        if not np.isfinite(a) or a <= 0:
            continue
        d = float(c[p] - lo[q])
        if d < drop * abs(float(c[p])) or d < atr_mult * a:
            continue
        seen.add(p)
        hp = float(h[p])
        k = -1
        last = min(q + break_bars, n - 1)
        # same segment: positions keep counting up by one
        seg_end = q
        while seg_end + 1 <= last and pos[seg_end + 1] == pos[seg_end] + 1:
            seg_end += 1
        if seg_end > q:
            hit = np.flatnonzero(c[q + 1 : seg_end + 1] > hp)
            if hit.size:
                k = q + 1 + int(hit[0])
        imp = conf = False
        if k > 0:
            ak = float(atr[k - 1])
            imp = bool(o[k] <= lo[p] or (np.isfinite(ak) and c[k] - o[k] >= imp_atr * ak))
            conf = bool(k + 1 < n and pos[k + 1] == pos[k] + 1 and c[k + 1] > hp)
        out.append(PobEvent(p, q, k, imp, conf, hp, float(lo[p]), float(max(o[p], c[p])), d / abs(float(c[p]))))
    return out


def pob_stop(level: float, pob_low: float, atr_k: float, mode: str = "pob") -> float:
    """Stop of the retest long: ``pob`` — min(low[p], level - 0.5 ATR) - 0.1 ATR (the aggressive stop behind the POB,
    at least 0.6 ATR below the level); ``atr`` — level - 1 ATR (as in step 4)."""
    if mode == "atr":
        return level - atr_k
    if mode != "pob":
        raise ValueError(f"unknown stop mode {mode!r}")
    return min(pob_low, level - 0.5 * atr_k) - 0.1 * atr_k
