"""Live signals of the course setups for the demo trader (ADR 0007): "is there a setup on the last CLOSED bar?".

Pure functions, no I/O, time is a parameter. Each adapter runs an existing, already verified detector over the closed
bars up to now and keeps only an event decided on the last one. A live run cannot look ahead (no later bars exist), and
the detectors are causal, so a signal here is the same one the backtest would have taken at that bar
(``tests/test_setup_signals.py`` checks it against the full-history detector).

Shorts are the long logic on negated prices (``Bars.mirrored``), as in the detectors themselves. Every signal carries a
plain-language comment for the owner (Ukrainian: it is shown to him in Telegram and in the journal) with the indicators
and their values, so each demo trade says which setup it was and why.

Rules taken from the verification runs (status of each setup: docs/knowledge/setups.md):
- S14 (EMA 13/21/34 trend, sweep of a fractal low in the EMA zone): ``course_s14_ema.find_signals(variant="sweep")``,
  stop 0.5 ATR under the signal bar, target the impulse high if >= 1.5R away, else 2R.
- S16 (deviation: 3..10 closes below a 30-bar range, then a close back in): ``course_basics.range_events`` kind "dev",
  stop 0.5 ATR under the lowest low of the deviation, target the range top.
- S3 (one-bar sweep of the range low and a close back in): the same detector, kind "sweep", stop 0.5 ATR under the sweep
  low, target the range middle (the course: take profit at the first level).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .course_basics import range_events
from .course_s14_ema import Bars, Params, atr, find_signals, indicators, segment_pos

S14_PARAMS = Params()
RANGE_W = 30
RANGE_WIDTH_ATR = 6.0
STOP_ATR = 0.5
TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


@dataclass(frozen=True)
class LiveSignal:
    setup: str  # 'S14' | 'S16' | 'S3'
    side: str  # 'long' | 'short'
    timeframe: str
    signal_ts: int  # open time of the signal bar, ms UTC
    entry: float  # close of the signal bar
    stop: float
    target: float
    comment: str
    indicators: dict[str, float] = field(default_factory=dict)

    @property
    def rr(self) -> float:
        risk = abs(self.entry - self.stop)
        return abs(self.target - self.entry) / risk if risk > 0 else 0.0


def closed_bars(rows: Sequence[Sequence[Any]], timeframe: str, now_ms: int) -> Bars:
    """Bars from ccxt OHLCV rows [ts, o, h, l, c, ...], without the bar still forming at ``now_ms``."""
    bar_ms = TF_MS[timeframe]
    keep = [r[:5] for r in rows if len(r) >= 5 and int(r[0]) + bar_ms <= now_ms]
    return Bars.from_rows(keep, bar_ms)


def _px(x: float) -> str:
    return f"{x:,.6g}" if abs(x) < 1000 else f"{x:,.0f}"


def _flip(side: str, x: float) -> float:
    return x if side == "long" else -x


def s14(b: Bars, timeframe: str) -> list[LiveSignal]:
    out: list[LiveSignal] = []
    n = len(b)
    if n < S14_PARAMS.warmup + 2:
        return out
    for side, bb in (("long", b), ("short", b.mirrored())):
        ind = indicators(bb, S14_PARAMS)
        for s in find_signals(bb, ind, S14_PARAMS, variant="sweep", lo=n - 1, hi=n):
            e, st, tg = (_flip(side, v) for v in (s.entry, s.stop, s.target))
            ef, em, es = (_flip(side, float(v[n - 2])) for v in (ind.e_fast, ind.e_mid, ind.e_slow))
            a = float(ind.atr[n - 1])
            high = _flip(side, s.high_ref)
            trend = "висхідний" if side == "long" else "низхідний"
            comment = (
                f"S14 ({timeframe}): тренд {trend} — {S14_PARAMS.trend_bars}+ закриттів за EMA21, EMA21 росте в бік тренду; "
                f"імпульс до {_px(high)} ≥ {S14_PARAMS.impulse_atr:g} ATR. Свічка зняла фрактал у зоні EMA13–EMA34 "
                f"(EMA13 {_px(ef)}, EMA21 {_px(em)}, EMA34 {_px(es)}) і закрилась за EMA34. "
                f"Вхід {_px(e)} за закриттям, стоп {_px(st)} (за свічкою + {S14_PARAMS.stop_atr:g} ATR, ATR14 {_px(a)}), "
                f"ціль {_px(tg)} ({'хай імпульсу' if s.target_kind == 'high' else '2R'})."
            )
            out.append(
                LiveSignal(
                    "S14",
                    side,
                    timeframe,
                    int(bb.t[n - 1]),
                    e,
                    st,
                    tg,
                    comment,
                    {"ema13": ef, "ema21": em, "ema34": es, "atr14": a, "impulse_extreme": high},
                )
            )
    return out


def range_setups(b: Bars, timeframe: str) -> list[LiveSignal]:
    """S16 (deviation) and S3 (one-bar sweep) on the last closed bar, both sides."""
    out: list[LiveSignal] = []
    n = len(b)
    if n < RANGE_W + 20:
        return out
    for side, bb in (("long", b), ("short", b.mirrored())):
        pos = segment_pos(bb.t)
        a_arr = atr(bb, 14, pos)
        for ev in range_events(bb, pos, a_arr, w=RANGE_W, width_atr=RANGE_WIDTH_ATR):
            if ev.r != n - 1:
                continue
            a = float(a_arr[n - 1])
            if not (np.isfinite(a) and a > 0):
                continue
            entry = float(bb.c[n - 1])
            stop = ev.ld - STOP_ATR * a
            target = ev.tr if ev.kind == "dev" else ev.mid
            e, st, tg = (_flip(side, v) for v in (entry, stop, target))
            broken, opposite, mid, ext = (_flip(side, v) for v in (ev.lr, ev.tr, ev.mid, ev.ld))
            lo, hi = min(broken, opposite), max(broken, opposite)
            edge = f"{'низ' if side == 'long' else 'верх'} {_px(broken)}"
            if ev.kind == "dev":
                k = ev.r - ev.s
                what = f"S16 ({timeframe}): девіація — {k} закриттів за межею ({edge}) рейнджу {RANGE_W} свічок {_px(lo)}–{_px(hi)}, потім закриття назад у рейндж."
                aim = f"ціль {_px(tg)} (протилежна межа)"
                setup = "S16"
            else:
                what = f"S3 ({timeframe}): свип — свічка проколола {edge} рейнджу {RANGE_W} свічок {_px(lo)}–{_px(hi)} і закрилась назад."
                aim = f"ціль {_px(tg)} (середина рейнджу)"
                setup = "S3"
            comment = (
                f"{what} Ширина рейнджу ≤ {RANGE_WIDTH_ATR:g} ATR, закриття ще не дійшло до середини {_px(mid)}. "
                f"Вхід {_px(e)} за закриттям, стоп {_px(st)} (екстремум {_px(ext)} + {STOP_ATR:g} ATR, ATR14 {_px(a)}), {aim}."
            )
            out.append(
                LiveSignal(
                    setup,
                    side,
                    timeframe,
                    int(bb.t[n - 1]),
                    e,
                    st,
                    tg,
                    comment,
                    {"range_low": lo, "range_high": hi, "range_mid": mid, "extreme": ext, "atr14": a},
                )
            )
    return out


DETECTORS = {"S14": s14, "S16": range_setups, "S3": range_setups}


def scan(b: Bars, timeframe: str, setups: Sequence[str] = ("S14", "S16", "S3")) -> list[LiveSignal]:
    """All signals on the last closed bar for the requested setups (each detector runs once)."""
    seen: set[Any] = set()
    out: list[LiveSignal] = []
    for name in setups:
        fn = DETECTORS[name]
        if fn in seen:
            continue
        seen.add(fn)
        out += [s for s in fn(b, timeframe) if s.setup in setups]
    return out
