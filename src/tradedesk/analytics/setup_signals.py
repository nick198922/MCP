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
- S12 (first retest of a fair value gap with the daily trend; 4h only): ``course_fvg_ob.fvg_zones`` with the flag set
  "trend" (daily close above its EMA21 at the gap's third candle), the first bar within 20 bars whose low reaches the
  gap's middle; stop 1 ATR (of the gap bar) under the middle, target 2R from the middle. **Rejected by the backtest**
  (``docs/verification/2026-10-17-course-step4-fvg-ob.md``) and traded on the demo only because the owner wants rejected
  setups tested live (BACKLOG 0.5 item "в", IDEA-20261024-short-setups-demo step 2). The backtest rests a limit at the middle;
  here the entry is the close of the bar that touched it, and a touch bar that already reached the stop (or closed at the
  target) gives no signal.
- S38 and S39 (trend-continuation shorts; 4h, shorts only): the step-1 detectors of IDEA-20261024-short-setups-demo,
  ``short_setups.breakout_retest`` (A) and ``short_setups.failed_sweep`` (B) on mirrored prices, with their registered
  parameters (``docs/verification/2026-10-24-short-setups-demo.md``). S38: a close below the S3 range low, then within
  6 bars the first bar whose high comes back to 0.25 ATR of the broken low decides: a close below it is a short at that
  close, stop 0.5 ATR over the retest bar's high, target 2R. S39: a live S3 long (sweep of the range low) whose stop is
  closed below within 6 bars, before the range middle was reached: a short at that close, stop 0.5 ATR over the highest
  high since the sweep, target 2R. Neither has a proven edge (S38 ✗ by the card's kill rule, the OOS sign is opposite;
  S39 ◯, too little data, and a random short of the same coin and BTC regime earned about as much); they are on the
  demo because the owner wants them tested live (card step 3: IS E[R] >= -0.05R). A-long failed that condition and is
  not traded.
- S40 (B-long; 4h): ``short_setups.failed_sweep`` on real prices, the mirror of S39: a live S3 short (sweep of the range
  high) whose stop is closed above within 6 bars, before the range middle was reached: a long at that close, stop
  0.5 ATR under the lowest low since the sweep, target 2R. The step-1 backtest: IS +0.056R [-0.041; +0.153], OOS
  +0.170R [-0.057; +0.402], against the placebo both CI contain 0: no proven edge; on the demo by the owner's answer
  to Q-20261025-3.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .course_basics import range_events
from .course_fvg_ob import Zone, fvg_zones
from .course_s14_ema import DAY_MS, Bars, Params, align_daily, atr, ema, find_signals, indicators, segment_pos
from .short_setups import breakout_retest, failed_sweep

S14_PARAMS = Params()
RANGE_W = 30
RANGE_WIDTH_ATR = 6.0
STOP_ATR = 0.5
TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

# S12, as registered in step 4 of IDEA-20261016-course-setups-crypto (scripts/verify_course_step4_fvg_ob.py)
S12_TIMEFRAMES = ("4h",)  # the trend flag of the backtest (daily EMA21) exists on 4h only
S12_WAIT = 20  # bars after the gap's third candle in which the first test counts (the backtest's limit wait)
S12_STOP_ATR = 1.0  # stop under the gap's middle, in ATR14 of the gap's third candle
S12_TARGET_R = 2.0  # target above the middle, in units of (middle - stop)
S12_WARMUP = 100  # bars of the segment before the gap (the backtest's warm-up)
S12_TREND_EMA = 21  # daily close vs its EMA21: the backtest's higher-timeframe trend
S12_BACKTEST_UA = (
    "Бектест відкинув цей сетап (docs/verification/2026-10-17-course-step4-fvg-ob.md): ліміт від середини FVG за денним "
    "трендом, 4h, 2020–2025 (до 2025-03): E[R] net +0.031R [−0.010; +0.071], на 0.036R гірше за випадковий рівень на тій "
    "самій відстані; після 2025-03 −0.016R. Демо торгує інше правило: вхід за закриттям свічки дотику (не ліміт на "
    "середині), утримання до 7 днів з беззбитком на половині шляху (у бектесті 20 свічок), відбір за балом (перевагу "
    "мають закриття біля середини або глибше), тож R демо напряму з бектестовим не порівнюється. "
    "На демо — перевірка наживо, не доведена перевага."
)

# S38 / S39, as registered in step 1 of IDEA-20261024-short-setups-demo (scripts/verify_short_setups.py, "base")
SHORT_TIMEFRAMES = ("4h",)  # the registered main sample; 1d was a side row (A 237 trades, B 5: its stops exceed 10 %)
SHORT_WAIT = 6  # bars after the breakout (A) or the sweep (B) in which the retest / the failure counts
S38_TOUCH_ATR = 0.25  # A: the retest bar's high comes back to within this many ATR14 of the broken range low
SHORT_STOP_ATR = 0.5  # stop over the retest high (A) or the highest high since the sweep (B), in ATR14 of the entry bar
SHORT_TARGET_R = 2.0  # target below the entry, in units of the price risk (stop - entry)
SHORT_PLACEBO_UA = (
    "Випадковий шорт того самого символу в тому самому режимі BTC (плацебо) у бектесті теж заробляв, тож результат "
    "демо порівнювати з плацебо, а не з нулем. Бектест: Binance, топ-30, вхід мейкером за закриттям завжди виконаний; "
    "демо: Bybit, топ-100, відбір за балом. На демо — перевірка наживо (рішення власника), не доведена перевага."
)
S38_BACKTEST_UA = (
    "Бектест кроку 1 (docs/verification/2026-10-24-short-setups-demo.md; 4h, 2020-02 … 2026-09, вихід як на демо: "
    "беззбиток на половині шляху, 7 днів): шорти в IS (до 2025-03) E[R] net +0.105R [+0.013; +0.200], в OOS −0.062R "
    "[−0.186; +0.061] — знак протилежний, тож за критерієм картки сетап відкинуто; проти плацебо IS +0.054R, OOS −0.131R. "
    + SHORT_PLACEBO_UA
)
S39_BACKTEST_UA = (
    "Бектест кроку 1 (docs/verification/2026-10-24-short-setups-demo.md; 4h, 2020-02 … 2026-09, вихід як на демо: "
    "беззбиток на половині шляху, 7 днів): шорти в IS (до 2025-03) E[R] net +0.177R [+0.003; +0.350], в OOS +0.060R "
    "[−0.163; +0.280]; проти плацебо IS +0.051R, OOS −0.068R (CI містять 0), без 10 найкращих дат IS −0.036R: "
    "даних замало для висновку. Стопи широкі (медіана 7 %). " + SHORT_PLACEBO_UA
)
S40_BACKTEST_UA = (
    "Бектест кроку 1 (docs/verification/2026-10-24-short-setups-demo.md; 4h, 2020-02 … 2026-09, вихід як на демо: "
    "беззбиток на половині шляху, 7 днів): лонги детектора B в IS (до 2025-03) E[R] net +0.056R [−0.041; +0.153], "
    "в OOS +0.170R [−0.057; +0.402]; проти плацебо IS +0.041R, OOS +0.126R (CI містять 0): доведеної переваги немає. "
    "Бектест: Binance, топ-30, вхід мейкером за закриттям завжди виконаний; демо: Bybit, топ-100, відбір за балом. "
    "На демо — перевірка наживо (рішення власника 25.10, Q-20261025-3), не доведена перевага."
)


@dataclass(frozen=True)
class LiveSignal:
    setup: str  # 'S14' | 'S16' | 'S3' | 'S12' | 'S38' | 'S39' | 'S40'
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
        # the whole window, not only the last bar: the detector keeps one signal per impulse ("used"), and that memory
        # needs the earlier bars; only a signal decided on the last bar is returned
        for s in (x for x in find_signals(bb, ind, S14_PARAMS, variant="sweep") if x.idx == n - 1):
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
                f"ціль {_px(tg)} ({('хай' if side == 'long' else 'лоу') + ' імпульсу' if s.target_kind == 'high' else '2R'})."
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


def daily_bars(b: Bars) -> Bars:
    """Complete UTC days of intraday bars ``b`` (every slot of the day present, first at 00:00 UTC): open of the first
    bar, highest high, lowest low, close of the last bar. An incomplete day (the one still running, or a hole) is
    dropped, so a day exists only once its last bar has closed."""
    per = DAY_MS // b.bar_ms if 0 < b.bar_ms <= DAY_MS and DAY_MS % b.bar_ms == 0 else 0
    if per == 0 or len(b) == 0:
        z = np.zeros(0)
        return Bars(z.astype(np.int64), z, z, z, z, DAY_MS)
    key = b.t // DAY_MS
    starts = np.flatnonzero(np.concatenate([[True], key[1:] != key[:-1]]))
    ends = np.concatenate([starts[1:], [key.size]]) - 1
    full = (ends - starts + 1 == per) & (b.t[starts] == key[starts] * DAY_MS) & (b.t[ends] == key[starts] * DAY_MS + DAY_MS - b.bar_ms)
    hi = np.maximum.reduceat(b.h, starts)[full]
    lo = np.minimum.reduceat(b.low, starts)[full]
    s, e = starts[full], ends[full]
    return Bars(key[s] * DAY_MS, b.o[s].copy(), hi, lo, b.c[e].copy(), DAY_MS)


def _first_test(b: Bars, pos: np.ndarray, i: int, level: float) -> int:
    """First bar j in i+1 .. i+S12_WAIT (same segment, no hole) whose low reaches ``level``; -1 if none."""
    for j in range(i + 1, min(i + S12_WAIT, len(b) - 1) + 1):
        if pos[j] != pos[i] + (j - i):
            return -1
        if b.low[j] <= level:
            return j
    return -1


def s12_all(b: Bars, timeframe: str, lo: int = 0) -> list[LiveSignal]:
    """S12 signals decided on bars lo .. n-1, both sides (the full-history form of ``s12``). Causal: a signal on bar j
    uses bars <= j only (the gap, its trend flag from the last daily close at or before the gap, the ATR and the first
    test). At most one signal per bar and side: the most recent gap whose middle was first tested on that bar."""
    out: list[LiveSignal] = []
    n = len(b)
    if timeframe not in S12_TIMEFRAMES or n < S12_WARMUP + 2:
        return out
    for side, bb in (("long", b), ("short", b.mirrored())):
        pos = segment_pos(bb.t)
        a_arr = atr(bb, 14, pos)
        d = daily_bars(bb)
        e_d = ema(d.c, S12_TREND_EMA, segment_pos(d.t))
        with np.errstate(invalid="ignore"):
            up_d = (d.c > e_d).astype(float)  # NaN EMA (warm-up) = no trend
        htf_up = align_daily(bb, d, up_d) > 0.5
        ema_at, dclose_at = align_daily(bb, d, e_d), align_daily(bb, d, d.c)
        best: dict[int, tuple[Zone, float, float, float]] = {}
        for z in fvg_zones(bb, pos, a_arr, htf_up, lo=max(2, lo - S12_WAIT), hi=n - 1):
            i = z.form
            if "trend" not in z.arms or pos[i] < S12_WARMUP:
                continue
            level = z.levels["mid"]
            j = _first_test(bb, pos, i, level)
            if j < max(lo, 0):
                continue
            stop = level - S12_STOP_ATR * z.atr
            target = level + S12_TARGET_R * (level - stop)
            if not (bb.low[j] > stop and bb.c[j] < target):
                continue  # the backtest trade is already stopped (or the bar closed at the target): nothing to enter
            if j not in best or i > best[j][0].form:
                best[j] = (z, level, stop, target)
        for j in sorted(best):
            z, level, stop, target = best[j]
            i = z.form
            e, st, tg, mid = (_flip(side, v) for v in (float(bb.c[j]), stop, target, level))
            g1, g2 = _flip(side, z.levels["full"]), _flip(side, z.levels["near"])
            g_lo, g_hi = min(g1, g2), max(g1, g2)
            em, dc = _flip(side, float(ema_at[i])), _flip(side, float(dclose_at[i]))
            word, sgn = ("вище", "−") if side == "long" else ("нижче", "+")
            comment = (
                f"S12 ({timeframe}): перший ретест імбалансу — FVG {_px(g_lo)}–{_px(g_hi)} ({j - i} св. тому), "
                f"за денним трендом (денне закриття {_px(dc)} {word} EMA{S12_TREND_EMA} {_px(em)}). "
                f"Свічка дійшла до середини FVG {_px(mid)} і закрилась на {_px(e)}. "
                f"Вхід {_px(e)} за закриттям, стоп {_px(st)} (середина {sgn} {S12_STOP_ATR:g} ATR, ATR14 {_px(z.atr)}), "
                f"ціль {_px(tg)} ({S12_TARGET_R:g}R від середини). {S12_BACKTEST_UA}"
            )
            out.append(
                LiveSignal(
                    "S12",
                    side,
                    timeframe,
                    int(bb.t[j]),
                    e,
                    st,
                    tg,
                    comment,
                    {"fvg_low": g_lo, "fvg_high": g_hi, "fvg_mid": mid, "atr14": z.atr, "ema21_1d": em, "fvg_ts": float(bb.t[i])},
                )
            )
    return out


def s12(b: Bars, timeframe: str) -> list[LiveSignal]:
    """S12 on the last closed bar (module docstring)."""
    return s12_all(b, timeframe, lo=len(b) - 1)


def _range_at(b: Bars, s: int) -> tuple[float, float]:
    """(low, high) of the RANGE_W bars before bar ``s`` (the S3 range of a breakout or sweep at ``s``), real prices."""
    return float(b.low[s - RANGE_W : s].min()), float(b.h[s - RANGE_W : s].max())


def s38_all(b: Bars, timeframe: str, lo: int = 0) -> list[LiveSignal]:
    """S38 shorts decided on bars lo .. n-1 (the full-history form of ``s38``): ``short_setups.breakout_retest`` on the
    mirrored bars with the registered parameters. Causal: the signal on bar j uses bars <= j only."""
    out: list[LiveSignal] = []
    n = len(b)
    if timeframe not in SHORT_TIMEFRAMES or n < RANGE_W + 20:
        return out
    m = b.mirrored()
    pos = segment_pos(m.t)
    a_arr = atr(m, 14, pos)
    sigs = breakout_retest(
        m,
        pos,
        a_arr,
        w=RANGE_W,
        width_atr=RANGE_WIDTH_ATR,
        max_wait=SHORT_WAIT,
        touch_atr=S38_TOUCH_ATR,
        stop_atr=SHORT_STOP_ATR,
        target_r=SHORT_TARGET_R,
    )
    for sg in sigs:
        if sg.i < lo:
            continue
        s, j = sg.start, sg.i
        r_lo, r_hi = _range_at(b, s)
        e, st, tg, level = -sg.entry, -sg.stop, -sg.target, -sg.level
        comment = (
            f"S38 ({timeframe}): пробій рейнджу вниз і ретест — свічка {j - s} св. тому закрилась на {_px(float(b.c[s]))}, "
            f"нижче мінімуму {_px(level)} рейнджу {RANGE_W} свічок {_px(r_lo)}–{_px(r_hi)} (ширина ≤ {RANGE_WIDTH_ATR:g} ATR); "
            f"ця свічка повернулась до пробитого рівня (хай {_px(float(b.h[j]))} ≥ рівень − {S38_TOUCH_ATR:g} ATR) "
            f"і закрилась нижче нього. Вхід {_px(e)} за закриттям, стоп {_px(st)} (хай ретесту + {SHORT_STOP_ATR:g} ATR, "
            f"ATR14 {_px(sg.atr)}), ціль {_px(tg)} ({SHORT_TARGET_R:g}R). {S38_BACKTEST_UA}"
        )
        out.append(
            LiveSignal(
                "S38",
                "short",
                timeframe,
                int(b.t[j]),
                e,
                st,
                tg,
                comment,
                {
                    "range_low": r_lo,
                    "range_high": r_hi,
                    "retest_high": float(b.h[j]),
                    "atr14": sg.atr,
                    "breakout_ts": float(b.t[s]),
                },
            )
        )
    return out


def s38(b: Bars, timeframe: str) -> list[LiveSignal]:
    """S38 on the last closed bar (module docstring)."""
    return s38_all(b, timeframe, lo=len(b) - 1)


def s39_all(b: Bars, timeframe: str, lo: int = 0) -> list[LiveSignal]:
    """S39 shorts decided on bars lo .. n-1 (the full-history form of ``s39``): ``short_setups.failed_sweep`` on the
    mirrored bars (its S3 shorts there are the S3 longs of ``b``) with the registered parameters. Causal: the signal on
    bar j uses bars <= j only."""
    out: list[LiveSignal] = []
    n = len(b)
    if timeframe not in SHORT_TIMEFRAMES or n < RANGE_W + 20:
        return out
    m = b.mirrored()
    pos = segment_pos(m.t)
    a_arr = atr(m, 14, pos)
    sigs = failed_sweep(
        m, pos, a_arr, w=RANGE_W, width_atr=RANGE_WIDTH_ATR, max_wait=SHORT_WAIT, stop_atr=SHORT_STOP_ATR, target_r=SHORT_TARGET_R
    )
    for sg in sigs:
        if sg.i < lo:
            continue
        r, j = sg.start, sg.i
        r_lo, r_hi = _range_at(b, r)
        e, st, tg, s3_stop = -sg.entry, -sg.stop, -sg.target, -sg.level
        mid = (r_lo + r_hi) / 2
        comment = (
            f"S39 ({timeframe}): провалений свип — {j - r} св. тому свічка проколола низ {_px(r_lo)} рейнджу {RANGE_W} свічок "
            f"{_px(r_lo)}–{_px(r_hi)} до {_px(float(b.low[r]))} і закрилась назад (лонг S3, його стоп {_px(s3_stop)}); "
            f"до середини рейнджу {_px(mid)} ціна не дійшла, а ця свічка закрилась нижче стопу S3. "
            f"Вхід {_px(e)} за закриттям, стоп {_px(st)} (найвищий хай від свипу + {SHORT_STOP_ATR:g} ATR, ATR14 {_px(sg.atr)}), "
            f"ціль {_px(tg)} ({SHORT_TARGET_R:g}R). {S39_BACKTEST_UA}"
        )
        out.append(
            LiveSignal(
                "S39",
                "short",
                timeframe,
                int(b.t[j]),
                e,
                st,
                tg,
                comment,
                {
                    "range_low": r_lo,
                    "range_high": r_hi,
                    "range_mid": mid,
                    "s3_stop": s3_stop,
                    "atr14": sg.atr,
                    "sweep_ts": float(b.t[r]),
                },
            )
        )
    return out


def s39(b: Bars, timeframe: str) -> list[LiveSignal]:
    """S39 on the last closed bar (module docstring)."""
    return s39_all(b, timeframe, lo=len(b) - 1)


def s40_all(b: Bars, timeframe: str, lo: int = 0) -> list[LiveSignal]:
    """S40 longs decided on bars lo .. n-1 (the full-history form of ``s40``): ``short_setups.failed_sweep`` on the
    real bars (the long form of detector B: S3 shorts that fail upwards) with the registered parameters. Causal: the
    signal on bar j uses bars <= j only."""
    out: list[LiveSignal] = []
    n = len(b)
    if timeframe not in SHORT_TIMEFRAMES or n < RANGE_W + 20:
        return out
    pos = segment_pos(b.t)
    a_arr = atr(b, 14, pos)
    sigs = failed_sweep(
        b, pos, a_arr, w=RANGE_W, width_atr=RANGE_WIDTH_ATR, max_wait=SHORT_WAIT, stop_atr=SHORT_STOP_ATR, target_r=SHORT_TARGET_R
    )
    for sg in sigs:
        if sg.i < lo:
            continue
        r, j = sg.start, sg.i
        r_lo, r_hi = _range_at(b, r)
        e, st, tg, s3_stop = sg.entry, sg.stop, sg.target, sg.level
        mid = (r_lo + r_hi) / 2
        comment = (
            f"S40 ({timeframe}): провалений свип угору — {j - r} св. тому свічка проколола верх {_px(r_hi)} рейнджу {RANGE_W} свічок "
            f"{_px(r_lo)}–{_px(r_hi)} до {_px(float(b.h[r]))} і закрилась назад (шорт S3, його стоп {_px(s3_stop)}); "
            f"до середини рейнджу {_px(mid)} ціна не дійшла, а ця свічка закрилась вище стопу S3. "
            f"Вхід {_px(e)} за закриттям, стоп {_px(st)} (найнижчий лоу від свипу − {SHORT_STOP_ATR:g} ATR, ATR14 {_px(sg.atr)}), "
            f"ціль {_px(tg)} ({SHORT_TARGET_R:g}R). {S40_BACKTEST_UA}"
        )
        out.append(
            LiveSignal(
                "S40",
                "long",
                timeframe,
                int(b.t[j]),
                e,
                st,
                tg,
                comment,
                {
                    "range_low": r_lo,
                    "range_high": r_hi,
                    "range_mid": mid,
                    "s3_stop": s3_stop,
                    "atr14": sg.atr,
                    "sweep_ts": float(b.t[r]),
                },
            )
        )
    return out


def s40(b: Bars, timeframe: str) -> list[LiveSignal]:
    """S40 on the last closed bar (module docstring)."""
    return s40_all(b, timeframe, lo=len(b) - 1)


DETECTORS = {"S14": s14, "S16": range_setups, "S3": range_setups, "S12": s12, "S38": s38, "S39": s39, "S40": s40}


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


# ---------------------------------------------------------------- confidence score (owner 21.10: at most 5 trades a day)


def trend_dir(b: Bars) -> int:
    """+1 up / -1 down / 0 flat on the last closed bar: close vs EMA34 and the slope of EMA21 (the S14 ribbon)."""
    if len(b) < 60:
        return 0
    pos = segment_pos(b.t)
    e21, e34 = ema(b.c, 21, pos), ema(b.c, 34, pos)
    if not (np.isfinite(e21[-1]) and np.isfinite(e21[-2]) and np.isfinite(e34[-1])):
        return 0
    if b.c[-1] > e34[-1] and e21[-1] > e21[-2]:
        return 1
    if b.c[-1] < e34[-1] and e21[-1] < e21[-2]:
        return -1
    return 0


@dataclass(frozen=True)
class Score:
    points: int
    reasons: tuple[str, ...]  # Ukrainian, shown to the owner


def score(sig: LiveSignal, rr_after_fees: float, htf: Bars | None, btc_htf: Bars | None, is_btc: bool) -> Score:
    """One point per independent confirmation, all from candles: the daily trend of the coin agrees, the daily trend of
    BTC agrees (alts), the reward/risk after fees is at least 2, and at least 3. Ties are broken by reward/risk."""
    want = 1 if sig.side == "long" else -1
    pts, why = 0, []
    t = trend_dir(htf) if htf is not None else 0
    if t == want:
        pts += 1
        why.append("денний тренд монети в бік угоди")
    if not is_btc:
        bt = trend_dir(btc_htf) if btc_htf is not None else 0
        if bt == want:
            pts += 1
            why.append("денний тренд BTC в бік угоди")
    if rr_after_fees >= 2:
        pts += 1
        why.append(f"R:R {rr_after_fees:.1f} ≥ 2")
    if rr_after_fees >= 3:
        pts += 1
        why.append("R:R ≥ 3")
    return Score(pts, tuple(why))
