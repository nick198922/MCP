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
  setups tested live (BACKLOG 0.5 item "в", IDEA-20261024-short-setups-demo step 2). ``s12`` enters at the close of the
  bar that touched the middle (a touch bar already at the stop or closed at the target gives no signal): the demo's rule
  until v7. Since v7 (owner 26.10, ``docs/verification/2026-10-26-prelimit-entries.md``) the demo trades ``s12_rest``: the
  signal is the gap's third candle itself, and its entry is a limit resting at the middle for ``S12_WAIT`` bars
  (indicator ``rest_until_ms``), as in the backtest; same stop and target.
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
- S28 (a valid order block, long only; 4h): ``course_fvg_ob.ob_zones`` with the flag set "valid" (a bearish candle
  engulfed by a bullish one whose body is >= 1 ATR, a bullish FVG right after, a close-BOS up within 20 bars), as the
  primary row B of step 4 (``scripts/verify_course_step4_fvg_ob.py``): the signal is the bar at whose close all three
  flags are known (``arm``), if the top of the bearish candle's body was not touched since the engulfing candle; the
  entry is a limit resting at that level for ``S28_WAIT`` bars (indicator ``rest_until_ms``), stop 1 ATR (of the
  engulfing candle) under it, target 2R. **Rejected by the backtest** (``docs/verification/2026-10-17-course-step4-fvg-ob.md``);
  a detector for the scanner only (opt-in name "S28"), not traded by the demo.
- S17 (the retest of an impulsively broken POB; 1d and 4h, both sides): ``course_pob.pob_events`` with the registered
  main rules of step 5 (``scripts/verify_course_step5_s17.py``, ``Var(tf)``): the POB is the bar with the highest close
  among the 5 before an impulse bar whose low is >= 10 % and >= 3 ATR below that close; the signal is the first close
  above the POB's high within 180 days, if that break is impulsive (opened at or below the POB's low, or a bullish body
  >= 1 ATR); the entry is a limit resting at the POB's high for 90 days (indicator ``rest_until_ms``), stop
  ``course_pob.pob_stop`` "pob" (min(POB low, level - 0.5 ATR) - 0.1 ATR, ATR14 of the break bar), target 2R, held up
  to 20 bars. Shorts: the same on mirrored prices. **Rejected by the backtest** (no better than a random level at the
  same distance, ``docs/verification/2026-10-17-course-step5-s17.md``); opt-in name "S17", not traded by the demo.
  History limit on 4h: the break window is 180 days = 1080 bars of 4h, so with the demo's 1000 bars (minus the
  100-bar warm-up, about 150 days) a POB whose impulse is 150..180 days old gives no live signal although the
  backtest took it: the live detector misses some signals (never adds false ones). The full window needs at least
  1080 + 100 + 14 = 1194 bars of 4h (two pages); 1d is not affected.
- S23 (a proxy of QM without BOS and FVG, as in the backtest; 4h, both sides): the author's QM is sweep of the last
  swing -> BOS with an FVG -> test of the manipulation zone (``docs/knowledge/setups.md``); the backtested
  formalisation, which this detector reproduces, is only the SFP sweep of ``sfp_backtest.walk_forward_sfp`` (step 2
  of ``docs/verification/2026-10-12-sfp-oos-and-15m.md``, ``scripts/verify_sfp_ltf.py``) with the limit at the swept
  level of ``sfp_backtest.ltf_entry`` mode "retest". On the last bar of a 300-bar window, inside the latest range
  (>= 20 bars, width <= 15 %), a candle pierces the last swing low with a wick >= half its range and closes back
  above it; that close back across the swept level is just the definition of the SFP sweep (``detect_sweeps``), not
  the author's break or engulfing of the manipulation's start, and neither the BOS nor the FVG is modelled. The entry
  is a limit resting at the swept level for 16 bars of 15m (``rest_until_ms``), cancelled if the price reaches the
  target first; stop ``ranges.generate_setup`` M2 (level - 2x the overshoot, never closer than 0.1 % beyond the
  wick), target the range middle, a filled trade held at most 10 days from the fill (the backtest's timeout,
  indicator ``max_hold_ms``). Shorts: a sweep of the last swing high. The separate lower-timeframe break of
  structure (``ltf_entry`` mode "confirm") needs 15m bars and was another entry, not this one. The backtest also
  skipped new sweeps of a symbol while its previous trade was open (an entry at the 4h close held up to 60 bars,
  about 10 days, whether or not the retest limit filled); the live detector reports every sweep, and the demo blocks
  a symbol only while a position or a limit lives, so the live signal set is wider than the backtest's sample (a
  demo-vs-backtest comparison must allow for it). **Significantly negative in the backtest**; opt-in name "S23", not
  traded by the demo.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .course_basics import range_events
from .course_fvg_ob import BOS_WINDOW, Zone, bos_up, fvg_zones, ob_zones, untouched
from .course_pob import pob_events, pob_stop
from .course_s14_ema import DAY_MS, Bars, Params, align_daily, atr, ema, find_signals, indicators, segment_pos
from .ranges import detect_ranges, detect_sweeps, generate_setup, swing_points
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
S12_REST_UA = (
    "Вхід лімітом, виставленим заздалегідь на середині FVG (з v7, рішення власника 26.10): у бектесті на тих самих правилах "
    "виходу (беззбиток на половині шляху, 7 днів, топ-30, 2020–2026) E[R] net +0.185R проти 0.000R за входом за закриттям "
    "свічки дотику; з вимогою пройти рівень на 0.1 % — +0.154R (docs/verification/2026-10-26-prelimit-entries.md). "
    "Перевага саме FVG над випадковим рівнем не доведена (бектест 17.10)."
)
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

# S28, as registered in step 4 of IDEA-20261016-course-setups-crypto (primary row B of scripts/verify_course_step4_fvg_ob.py)
S28_TIMEFRAMES = ("4h",)  # the registered primary row; 1d was a side row (also rejected)
S28_WAIT = 20  # bars after the arm bar in which the resting limit at the level may fill (the backtest's WAIT_BARS)
S28_STOP_ATR = 1.0  # stop under the level, in ATR14 of the engulfing candle
S28_TARGET_R = 2.0  # target above the level, in units of (level - stop)
S28_WARMUP = 100  # bars of the segment before the arm bar (the backtest's warm-up)
S28_BACKTEST_UA = (
    "Бектест відкинув цей сетап (docs/verification/2026-10-17-course-step4-fvg-ob.md): ліміт від верху тіла валідного OB "
    "на першому тесті, 4h, топ-30 Binance, 2020 … 2025-03: лонги E[R] net −0.044R [−0.125; +0.034], проти випадкового "
    "рівня на тій самій відстані +0.005R; після 2025-03 лонги −0.105R; обидві сторони разом −0.052R [−0.113; +0.009], "
    "на BTC+ETH −0.172R. «Валідні» OB не кращі за решту OB (−0.030R). Ліміт у бектесті виконувався за дотиком "
    "(з вимогою проколу на 0.05 ATR — гірше на ≈ 0.05R), утримання 20 свічок. Доведеної переваги немає."
)

# S17, as registered in step 5 of IDEA-20261016-course-setups-crypto (main rules Var(tf) of scripts/verify_course_step5_s17.py)
S17_TIMEFRAMES = ("1d", "4h")  # 1d: a co-primary row (12h, the other one, is not a scanner timeframe); 4h: a side row
S17_DROP = 0.10  # the impulse: close of the POB minus the low of the impulse bar, as a fraction of the POB's close
S17_LOOK = 5  # the POB is the highest close among this many bars before the impulse bar
S17_IMPULSE_ATR = 3.0  # ... and the impulse is also at least this many ATR14 (of the POB)
S17_BREAK_DAYS = 180  # the break is the first close beyond the POB's high within this many days of the impulse
S17_BREAK_BODY_ATR = 1.0  # an impulsive break: opened at or beyond the POB's low, or a body of this many ATR14 (prev bar)
S17_WAIT_DAYS = 90  # the limit at the POB's high rests this long from the close of the break bar
S17_HOLD_BARS = 20  # a filled trade is held at most this many bars of the timeframe
S17_TARGET_R = 2.0  # target beyond the level, in units of (level - stop)
S17_WARMUP = 100  # bars of the segment before the POB and the break (the backtest's warm-up)
S17_COMMON_UA = (
    "Бектест відкинув цей сетап механічно (docs/verification/2026-10-17-course-step5-s17.md; Binance, BTC, ETH + топ-30, "
    "2020-02 … 2026-06, обидва боки, ліміт на хаї POB 90 діб, стоп за POB, ціль 2R, утримання 20 свічок): ретест POB "
    "поводиться як ретест випадкового рівня на тій самій відстані від ціни. "
)
S17_TAIL_UA = (
    " Лімітки виконувались майже одразу (91–95 %, медіана 0.3–0.5 доби) і за дотиком; з вимогою проколу на 0.05 ATR "
    "E[R] гірший на ≈ 0.04–0.05R. Доведеної переваги немає."
)
S17_ROW_UA = {
    "1d": (
        "1D (основний рядок): E[R] net −0.007R [−0.150; +0.141], проти випадкового рівня −0.083R [−0.236; +0.065]; "
        "після 2025-03 +0.170R на 167 угодах (н. з.). Перевагу +0.10R над випадковим рівнем виключено."
    ),
    "4h": (
        "4h (другорядний рядок): E[R] net +0.036R [−0.029; +0.102], проти випадкового рівня +0.047R [−0.016; +0.114]; "
        "після 2025-03 −0.046R (і проти випадкового рівня −0.046R). На 12h (основний рядок) +0.027R, проти випадкового "
        "рівня +0.014R."
    ),
}
S17_BACKTEST_UA = {tf: S17_COMMON_UA + row + S17_TAIL_UA for tf, row in S17_ROW_UA.items()}

# S23, as backtested in step 2 of docs/verification/2026-10-12-sfp-oos-and-15m.md (scripts/verify_sfp_ltf.py, 4h)
S23_TIMEFRAMES = ("4h",)  # the backtest's main sample (850 signals); 12h was the other one (not a scanner timeframe)
S23_WINDOW = 300  # bars the walk-forward scan sees at each closed bar (walk_forward_sfp window on 4h)
S23_MIN_BARS = 20  # the shortest range
S23_MAX_WIDTH_PCT = 15.0  # widest range, percent of its middle (the ranges module works in percent)
S23_SWING_LOOKBACK = 3  # swing pivots: >= / <= the 3 bars on each side
S23_WICK_RATIO = 0.5  # the piercing wick is at least this share of the sweep candle's range
S23_STOP_MULT = 2.0  # M2 stop: the swept level plus this many overshoots beyond it
S23_STOP_MIN_OFFSET_PCT = 0.1  # ... never closer than this percent beyond the wick extreme
S23_WAIT_MS = 16 * 900_000  # the retest limit waits 16 bars of 15m (verify_sfp_ltf.py --wait-bars)
S23_HOLD_MS = 10 * DAY_MS  # a filled trade is held at most 10 days from the fill (verify_sfp_ltf.py HOLD_15M = 960)
S23_BACKTEST_UA = (
    "Бектест (docs/verification/2026-10-12-sfp-oos-and-15m.md, крок 2; Binance, 20 топ-перпів, 2 роки до 2026-10, "
    "свип на 4h, вхід і виходи на 15m, таймаут 10 діб, комісія 0.1 % за коло): вхід лімітом на знятому рівні значуще "
    "гірший за нуль — −0.142R на сигнал [−0.24; −0.05] (невиконані = 0R), −0.272R на угоду [−0.45; −0.09] "
    "(444 угоди з 850 сигналів, gross −0.159R). Ймовірно, несприятливий відбір: лімітка частіше виконується, коли свип "
    "не хибний і ціна йде далі. Окремий злам структури на 15m (закриття за екстремумом попередньої свічки) нічого не "
    "додав: −0.046R на угоду, як і вхід за закриттям свічки 4h. Бектест пропускав свипи, поки була відкрита попередня "
    "угода тієї самої монети. Доведеної переваги немає."
)


@dataclass(frozen=True)
class LiveSignal:
    setup: str  # 'S14' | 'S16' | 'S3' | 'S12' | 'S17' | 'S23' | 'S28' | 'S38' | 'S39' | 'S40'
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
        pos, zones, ema_at, dclose_at = _s12_zones(bb, max(2, lo - S12_WAIT), n - 1)
        best: dict[int, tuple[Zone, float, float, float]] = {}
        for z in zones:
            i = z.form
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


def _s12_zones(bb: Bars, lo: int, hi: int) -> tuple[np.ndarray, list[Zone], np.ndarray, np.ndarray]:
    """(segment positions, the gaps with third candle in lo .. hi-1 that have the daily trend flag and the warm-up,
    daily EMA21 and daily close aligned to the bars) in the long frame ``bb``."""
    pos = segment_pos(bb.t)
    a_arr = atr(bb, 14, pos)
    d = daily_bars(bb)
    e_d = ema(d.c, S12_TREND_EMA, segment_pos(d.t))
    with np.errstate(invalid="ignore"):
        up_d = (d.c > e_d).astype(float)  # NaN EMA (warm-up) = no trend
    htf_up = align_daily(bb, d, up_d) > 0.5
    zones = [z for z in fvg_zones(bb, pos, a_arr, htf_up, lo=lo, hi=hi) if "trend" in z.arms and pos[z.form] >= S12_WARMUP]
    return pos, zones, align_daily(bb, d, e_d), align_daily(bb, d, d.c)


def s12_rest_all(b: Bars, timeframe: str, lo: int = 0) -> list[LiveSignal]:
    """S12 since v7, decided on bars lo .. n-1: a gap with the daily trend whose third candle is bar i gives a signal on
    bar i; the entry is a limit resting at the gap's middle until ``rest_until_ms`` (the close of bar i + S12_WAIT
    bars), stop 1 ATR beyond the middle, target 2R from it. Causal: the signal on bar i uses bars <= i only."""
    out: list[LiveSignal] = []
    n = len(b)
    if timeframe not in S12_TIMEFRAMES or n < S12_WARMUP + 2:
        return out
    for side, bb in (("long", b), ("short", b.mirrored())):
        _, zones, ema_at, dclose_at = _s12_zones(bb, max(2, lo), n)
        for z in zones:
            i = z.form
            mid = z.levels["mid"]
            stop = mid - S12_STOP_ATR * z.atr
            target = mid + S12_TARGET_R * (mid - stop)
            e, st, tg = (_flip(side, v) for v in (mid, stop, target))
            g1, g2 = _flip(side, z.levels["full"]), _flip(side, z.levels["near"])
            g_lo, g_hi = min(g1, g2), max(g1, g2)
            em, dc = _flip(side, float(ema_at[i])), _flip(side, float(dclose_at[i]))
            until = bb.close_time(i) + S12_WAIT * bb.bar_ms
            word, sgn = ("вище", "−") if side == "long" else ("нижче", "+")
            comment = (
                f"S12 ({timeframe}): новий імбаланс FVG {_px(g_lo)}–{_px(g_hi)} за денним трендом (денне закриття {_px(dc)} "
                f"{word} EMA{S12_TREND_EMA} {_px(em)}). Лімітка заздалегідь на середині FVG {_px(e)}, живе {S12_WAIT} свічок "
                f"({S12_WAIT * bb.bar_ms // 3_600_000} год); стоп {_px(st)} (середина {sgn} {S12_STOP_ATR:g} ATR, ATR14 "
                f"{_px(z.atr)}), ціль {_px(tg)} ({S12_TARGET_R:g}R). {S12_REST_UA} {S12_BACKTEST_UA}"
            )
            ind = {"fvg_low": g_lo, "fvg_high": g_hi, "fvg_mid": e, "atr14": z.atr, "ema21_1d": em, "fvg_ts": float(bb.t[i])}
            out.append(LiveSignal("S12", side, timeframe, int(bb.t[i]), e, st, tg, comment, {**ind, "rest_until_ms": float(until)}))
    return out


def s12_rest(b: Bars, timeframe: str) -> list[LiveSignal]:
    """S12 since v7 on the last closed bar (module docstring)."""
    return s12_rest_all(b, timeframe, lo=len(b) - 1)


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


def s28_all(b: Bars, timeframe: str, lo: int = 0) -> list[LiveSignal]:
    """S28 longs decided on bars lo .. n-1 (the full-history form of ``s28``): every bullish order block whose flag set
    "valid" (impulse, FVG after, close-BOS after) completes on bar j = ``arm``, with the level (top of the bearish
    candle's body) untouched on the bars after the engulfing candle up to j and j past the warm-up; the entry is a
    limit resting at the level until ``rest_until_ms`` (the close of j + S28_WAIT bars), stop 1 ATR of the engulfing
    candle under it, target 2R. Causal: the signal on bar j uses bars <= j only (``ob_zones`` arms a flag on the bar
    that decides it; ``bos_up`` and the ATR are causal)."""
    out: list[LiveSignal] = []
    n = len(b)
    if timeframe not in S28_TIMEFRAMES or n < S28_WARMUP + 2:
        return out
    pos = segment_pos(b.t)
    a_arr = atr(b, 14, pos)
    bos = bos_up(b, pos)
    # the latest flag of a zone with engulfing candle e is known by e + BOS_WINDOW - 1, so earlier zones cannot arm in lo..
    for z in ob_zones(b, pos, a_arr, bos, lo=max(1, lo - BOS_WINDOW)):
        j = z.arms.get("valid")
        if j is None or j < max(lo, 0) or j >= n or pos[j] < S28_WARMUP or not np.isfinite(a_arr[j]):
            continue
        level = z.levels["body"]
        if not untouched(b, z.form, j, level) or float(b.c[j]) < level:
            continue  # not the first test any more, or the price is already inside the zone
        stop = level - S28_STOP_ATR * z.atr
        target = level + S28_TARGET_R * (level - stop)
        i, e = z.meta["i"], z.form
        k_bos = z.arms["bos"]
        until = b.close_time(j) + S28_WAIT * b.bar_ms
        body = float(b.c[e] - b.o[e])
        when = "на свічці поглинання" if k_bos == e else f"через {k_bos - e} св. після поглинання"
        comment = (
            f"S28 ({timeframe}): валідний ордер-блок — ведмежа свічка (тіло {_px(float(b.c[i]))}–{_px(level)}) поглинута "
            f"бичачою з тілом {_px(body)} (≥ ATR), після неї бичачий FVG і BOS (закриття {_px(float(b.c[k_bos]))} вище "
            f"фрактального хаю {when}); рівень {_px(level)} ще не тестувався. "
            f"Лімітка заздалегідь на верху тіла OB {_px(level)}, живе {S28_WAIT} свічок "
            f"({S28_WAIT * b.bar_ms // 3_600_000} год); стоп {_px(stop)} (рівень − {S28_STOP_ATR:g} ATR, ATR14 {_px(z.atr)}), "
            f"ціль {_px(target)} ({S28_TARGET_R:g}R). {S28_BACKTEST_UA}"
        )
        out.append(
            LiveSignal(
                "S28",
                "long",
                timeframe,
                int(b.t[j]),
                level,
                stop,
                target,
                comment,
                {
                    "ob_low": float(b.low[i]),
                    "ob_high": float(b.h[i]),
                    "ob_body_low": float(b.c[i]),
                    "ob_body_high": level,
                    "atr14": z.atr,
                    "ob_ts": float(b.t[i]),
                    "bos_ts": float(b.t[k_bos]),
                    "rest_until_ms": float(until),
                },
            )
        )
    out.sort(key=lambda s: s.signal_ts)
    return out


def s28(b: Bars, timeframe: str) -> list[LiveSignal]:
    """S28 on the last closed bar (module docstring)."""
    return s28_all(b, timeframe, lo=len(b) - 1)


def s17_all(b: Bars, timeframe: str, lo: int = 0) -> list[LiveSignal]:
    """S17 signals decided on bars lo .. n-1, both sides (the full-history form of ``s17``): every POB event of
    ``course_pob.pob_events`` (registered main rules) whose impulsive break k is in lo .. n-1, past the warm-up and with
    a finite ATR; the entry is a limit resting at the POB's high (the low for shorts) until ``rest_until_ms`` (the close
    of k + 90 days), stop ``pob_stop`` "pob" with ATR14 of k, target 2R. Causal: the POB is known at the close of its
    impulse bar and the break at the close of k, each from bars up to it (``tests/test_course_pob.py``); the ATR is
    causal, so the signal on bar k uses bars <= k only. Needs >= 1194 bars of 4h for the full 180-day break window
    (module docstring); with fewer, older POBs are missed."""
    out: list[LiveSignal] = []
    n = len(b)
    if timeframe not in S17_TIMEFRAMES or n < S17_WARMUP + 2:
        return out
    pos = segment_pos(b.t)
    a_arr = atr(b, 14, pos)  # true ranges do not change on mirrored prices
    brk_bars = S17_BREAK_DAYS * DAY_MS // b.bar_ms
    for side, bb in (("long", b), ("short", b.mirrored())):
        evs = pob_events(
            bb,
            pos,
            a_arr,
            drop=S17_DROP,
            look=S17_LOOK,
            atr_mult=S17_IMPULSE_ATR,
            break_bars=brk_bars,
            imp_atr=S17_BREAK_BODY_ATR,
            warmup=S17_WARMUP,
        )
        for e in evs:
            k = e.k
            if k < max(lo, 0) or not e.impulsive or pos[k] < S17_WARMUP or not np.isfinite(a_arr[k]):
                continue
            a = float(a_arr[k])
            level = e.levels["wick"]
            if float(bb.c[k]) < level:
                continue  # the price inside the POB (the backtest's check; the break closes beyond the level)
            stop = pob_stop(level, e.low, a, "pob")
            target = level + S17_TARGET_R * (level - stop)
            p, q = e.p, e.q
            lv, st, tg = (_flip(side, v) for v in (level, stop, target))
            x1, x2 = _flip(side, e.high), _flip(side, e.low)
            p_lo, p_hi = min(x1, x2), max(x1, x2)
            ext, c_p, c_k = (_flip(side, float(v)) for v in (bb.low[q], bb.c[p], bb.c[k]))
            until = b.close_time(k) + S17_WAIT_DAYS * DAY_MS
            how = "відкрилась по той бік POB" if bb.o[k] <= bb.low[p] else f"тіло ≥ {S17_BREAK_BODY_ATR:g} ATR"
            if side == "long":
                move, edge, lim, rule = "падінням", "вище хаю", "хаї", "мін(лоу POB, рівень − 0.5 ATR) − 0.1 ATR"
            else:
                move, edge, lim, rule = "ростом", "нижче лоу", "лоу", "макс(хай POB, рівень + 0.5 ATR) + 0.1 ATR"
            comment = (
                f"S17 ({timeframe}): ретест імпульсно пробитого POB — свічка POB {_px(p_lo)}–{_px(p_hi)} (закриття "
                f"{_px(c_p)}) перед {move} на {e.drop:.1%} до {_px(ext)} (≥ {S17_DROP:.0%} і ≥ {S17_IMPULSE_ATR:g} ATR); "
                f"через {k - q} св. після імпульсу ця свічка закрилась на {_px(c_k)}, {edge} POB ({how}). "
                f"Лімітка заздалегідь на {lim} POB {_px(lv)}, живе {S17_WAIT_DAYS} діб; стоп {_px(st)} (за POB: {rule}, "
                f"ATR14 {_px(a)}), ціль {_px(tg)} ({S17_TARGET_R:g}R), утримання до {S17_HOLD_BARS} свічок. "
                f"{S17_BACKTEST_UA[timeframe]}"
            )
            out.append(
                LiveSignal(
                    "S17",
                    side,
                    timeframe,
                    int(b.t[k]),
                    lv,
                    st,
                    tg,
                    comment,
                    {
                        "pob_low": p_lo,
                        "pob_high": p_hi,
                        "pob_ts": float(b.t[p]),
                        "impulse_ts": float(b.t[q]),
                        "impulse_move": e.drop,
                        "atr14": a,
                        "rest_until_ms": float(until),
                    },
                )
            )
    out.sort(key=lambda s: s.signal_ts)
    return out


def s17(b: Bars, timeframe: str) -> list[LiveSignal]:
    """S17 on the last closed bar (module docstring)."""
    return s17_all(b, timeframe, lo=len(b) - 1)


def _ohlc_rows(b: Bars, i: int, j: int) -> list[dict[str, Any]]:
    """Bars i .. j-1 as the OHLC dicts of ``analytics/ranges.py`` (the layout ``walk_forward_sfp`` runs on)."""
    return [
        {"ts": int(b.t[k]), "open": float(b.o[k]), "high": float(b.h[k]), "low": float(b.low[k]), "close": float(b.c[k])}
        for k in range(i, j)
    ]


def s23_all(b: Bars, timeframe: str, lo: int = 0) -> list[LiveSignal]:
    """S23 signals decided on bars lo .. n-1, both sides (the full-history form of ``s23``): at each bar t, exactly the
    check of ``sfp_backtest.walk_forward_sfp`` on the window of the last S23_WINDOW bars ending at t (latest range,
    a sweep inside it on the window's last bar, ``generate_setup`` M2 with no R:R floor), without its one-position skip
    (module docstring: the live set is wider than the backtest's sample). A proxy of QM without BOS and FVG. The entry
    is ``ltf_entry`` "retest": a limit resting at the swept level until ``rest_until_ms``, stop the M2 stop, target
    the range middle (TP1), held at most ``max_hold_ms`` (10 days) from the fill. Causal: the signal on bar t uses the
    window ending at t only."""
    out: list[LiveSignal] = []
    n = len(b)
    if timeframe not in S23_TIMEFRAMES or n < S23_WINDOW:
        return out
    first = max(lo, S23_WINDOW - 1)
    off = first - S23_WINDOW + 1
    rows = _ohlc_rows(b, off, n)
    for t in range(first, n):
        bar = rows[t - off]
        hi, low, op, cl = bar["high"], bar["low"], bar["open"], bar["close"]
        rng = hi - low
        if rng <= 0 or ((hi - max(op, cl)) / rng < S23_WICK_RATIO and (min(op, cl) - low) / rng < S23_WICK_RATIO):
            continue  # no wick long enough: no sweep on this bar (the same test as detect_sweeps, done first: cheap)
        win = rows[t - off - S23_WINDOW + 1 : t - off + 1]
        ranges = detect_ranges(win, min_bars=S23_MIN_BARS, max_width_pct=S23_MAX_WIDTH_PCT)
        if not ranges or ranges[-1]["end_idx"] != len(win) - 1:
            continue  # the window's last bar is not inside the latest range
        sweeps = detect_sweeps(win, swing_points(win, lookback=S23_SWING_LOOKBACK), wick_ratio=S23_WICK_RATIO)
        if not sweeps or sweeps[-1]["idx"] != len(win) - 1:
            continue
        r, sw = ranges[-1], sweeps[-1]
        setup = generate_setup(r, sw, stop_mult=S23_STOP_MULT, stop_min_offset_pct=S23_STOP_MIN_OFFSET_PCT, min_rr=0.0)
        if setup is None:
            continue  # the close is already beyond the range middle (or zero risk): the backtest takes no trade
        side = str(setup["direction"])
        level, stop, target = float(sw["swept_level"]), float(setup["stop"]), float(setup["tp1"])
        bottom, top = float(r["bottom"]), float(r["top"])
        ext = float(sw["candle_low"] if side == "long" else sw["candle_high"])
        until = b.close_time(t) + S23_WAIT_MS
        if side == "long":
            swing, back, sgn = "свінг-лоу", "вище", "−"
        else:
            swing, back, sgn = "свінг-хай", "нижче", "+"
        comment = (
            f"S23 ({timeframe}): SFP-ретест (проксі QM, без BOS) — свічка зняла {swing} {_px(level)} до {_px(ext)} і "
            f"закрилась назад {back}, на {_px(cl)} (тінь ≥ {S23_WICK_RATIO:.0%} свічки), у рейнджі "
            f"{_px(bottom)}–{_px(top)} ({r['n_bars']} св., ширина {r['width_pct']:.1f} % ≤ {S23_MAX_WIDTH_PCT:g} %). "
            f"Це лише SFP-свип, як у бектесті: BOS і FVG автора не моделюються. Лімітка на знятому рівні {_px(level)}, "
            f"живе {S23_WAIT_MS // 900_000} свічок 15m ({S23_WAIT_MS // 3_600_000} год); скасувати, якщо "
            f"ціна раніше дійде до цілі. Стоп {_px(stop)} (рівень {sgn} {S23_STOP_MULT:g}× прокол, не ближче "
            f"{S23_STOP_MIN_OFFSET_PCT:g} % за тінню), ціль {_px(target)} (середина рейнджу), утримання до "
            f"{S23_HOLD_MS // DAY_MS} діб від входу. {S23_BACKTEST_UA}"
        )
        out.append(
            LiveSignal(
                "S23",
                side,
                timeframe,
                int(b.t[t]),
                level,
                stop,
                target,
                comment,
                {
                    "swept_level": level,
                    "sweep_extreme": ext,
                    "range_low": bottom,
                    "range_high": top,
                    "range_mid": target,
                    "rest_until_ms": float(until),
                    "max_hold_ms": float(S23_HOLD_MS),
                },
            )
        )
    return out


def s23(b: Bars, timeframe: str) -> list[LiveSignal]:
    """S23 on the last closed bar (module docstring)."""
    return s23_all(b, timeframe, lo=len(b) - 1)


DETECTORS = {
    "S14": s14,
    "S16": range_setups,
    "S3": range_setups,
    "S12": s12_rest,
    "S17": s17,
    "S23": s23,
    "S28": s28,
    "S38": s38,
    "S39": s39,
    "S40": s40,
}


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
