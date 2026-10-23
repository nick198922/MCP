"""Demo vs backtest (BACKLOG 0.5, owner 23.10, item a): would the backtest of the same setup have produced this closed
demo trade, with the same entry, stop and result in R? So we learn whether the historical tests can be trusted.

Pure functions, no I/O, time is a parameter (``now_ms``: only bars closed by then are used).

Replay of one closed ``demo_trade`` row:
1. **Signal.** The live detector of ``setup_signals`` runs on the same window the live scan saw: the last ``window``
   closed bars of the signal timeframe ending at the signal bar (the demo fetches 1000 bars and drops the forming one).
   The window matters: EMA warm-up and the S14 "one signal per impulse" memory depend on its start.
2. **Path.** The backtest trade enters at the signal-bar close and walks 1h bars with ``stop_management`` conventions
   (maker entry and target, taker stop plus slippage, R = the live risk unit with fees), under the live break-even rule
   (stop to entry plus both taker fees after ``be_frac`` of the way to the target). The order of prices inside an hour
   is unknown, so R is a band: the pessimistic bound moves the stop from the next bar (rule "B"), the optimistic bound
   also inside the trigger bar ("Bx"). ``r_plain`` is the same trade without break-even (the shadow plan's rule). A
   missing 1h bar cuts the path: a trade the path does not resolve is ``pending`` while its horizon has not passed and
   the path reaches ``now_ms``, otherwise ``no_data``.
3. **Same risk units.** The demo's R divides its P&L by the risk from its own fill, the backtest's by the risk from the
   signal close, so the two R are not comparable when the entries differ. The path and exit check compares the demo R
   with the backtest band replayed from the demo fill (``r_bt_fill``, same stop and target as the backtest, so nearly
   the demo's risk unit); ``r_demo_bt`` is the demo R in the backtest's risk units and ``r_diff = r_demo_bt - r_bt`` the
   whole demo-minus-backtest gap (entry plus path). ``entry_edge_r`` is the signed entry difference (+ = the demo
   entered better), ``plan_edge_r`` the part from the demo's planned entry (its own candles); the rest is execution.
4. **Verdict.** ``match``: a signal of the same setup and side on the same bar, the demo fill within ``entry_r`` of the
   backtest entry, stop and target within ``level_r`` (R units of the backtest risk), the demo R within ``r`` of the
   backtest band from the fill. ``no_signal``: the detector gives no such signal on that bar. ``mismatch``: a signal,
   but a side, an entry, a level or the R differs (``reasons``). Undecided: ``pending`` (the backtest trade is still
   open at ``now_ms``), ``no_data`` (bars missing or the path cut), ``unsupported`` (a setup without a candle detector,
   e.g. an option trade).

Caveats: the demo's own candles come from api-demo.bybit.com, the replay from the venue the caller fetched (default
Bybit mainnet); small level differences there are what ``level_r`` absorbs. The demo enters up to 2 hours after the
signal close at a maker quote and starts its hold clock at the fill; the backtest enters at the close. Trades opened
before 22.10 moved the stop at +30 % ROE, not half the way, so their R is compared with today's rule.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from .course_s14_ema import MAX_GAP_MS, Bars
from .setup_signals import DETECTORS, TF_MS, LiveSignal, scan
from .smart_money import t975
from .stop_management import TAKER, Outcome, StopRule, simulate_stop_rule

LIVE_WINDOW = 999  # demo_trader fetches 1000 bars (bybit_demo.ohlcv) and drops the one still forming
BE_FRAC = 0.5  # execution.guard.BE_PATH (owner 22.10); passed in so analytics never imports execution
MIN_TRADES = 30  # owner 23.10: no conclusion before ~30 closed trades
DECIDED = ("match", "no_signal", "mismatch")
VERDICTS = (*DECIDED, "pending", "no_data", "unsupported")
_KIND = {"stop": "stop", "stop_gap": "stop", "be": "breakeven", "be_gap": "breakeven", "target": "target"}


@dataclass(frozen=True)
class Tolerance:
    entry_r: float = 0.25  # |demo fill - backtest entry| / backtest risk (execution.orders.MAX_CHASE_R)
    level_r: float = 0.05  # stop and target differences / backtest risk (tick rounding, demo vs venue candles)
    r: float = 0.30  # demo R outside the backtest band from the demo fill [B, Bx] by more than this


@dataclass(frozen=True)
class DemoTrade:
    link_id: str
    symbol: str
    side: str  # 'long' | 'short'
    setup: str
    timeframe: str
    signal_ts: int  # open time of the signal bar, ms
    entry: float  # planned entry
    fill: float | None
    stop: float
    target: float
    exit: float | None
    r_multiple: float | None  # venue P&L / risk, fees included
    exit_reason: str | None
    opened_ts: int | None
    closed_ts: int | None
    be_ts: int | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> DemoTrade:
        def f(k: str) -> float | None:
            v = row.get(k)
            return None if v is None else float(v)

        def i(k: str) -> int | None:
            v = row.get(k)
            return None if v is None else int(v)

        return cls(
            str(row["link_id"]),
            str(row["symbol"]),
            str(row["side"]),
            str(row["setup"]),
            str(row["timeframe"]),
            int(row["signal_ts"]),
            float(row["entry"]),
            f("fill"),
            float(row["stop"]),
            float(row["target"]),
            f("exit"),
            f("r_multiple"),
            row.get("exit_reason"),
            i("opened_ts"),
            i("closed_ts"),
            i("be_ts"),
        )


@dataclass(frozen=True)
class Replay:
    link_id: str
    symbol: str
    setup: str
    timeframe: str
    side: str
    signal_ts: int
    closed_ts: int | None
    verdict: str  # one of VERDICTS
    reasons: tuple[str, ...] = ()
    note: str = ""
    bt_entry: float | None = None
    bt_stop: float | None = None
    bt_target: float | None = None
    bt_exit: str | None = None  # backtest exit in the demo's words: target | stop | breakeven | time
    demo_exit: str | None = None
    entry_diff_r: float | None = None  # |entry_edge_r|
    entry_edge_r: float | None = None  # (backtest entry - demo fill) / risk for a long, the opposite for a short: + = better
    plan_edge_r: float | None = None  # the same for the demo's planned entry (candle difference, before execution)
    stop_diff_r: float | None = None
    target_diff_r: float | None = None
    r_demo: float | None = None  # the demo's own R: P&L / risk from its fill
    r_demo_bt: float | None = None  # the demo P&L in the backtest's risk units
    r_bt: float | None = None  # break-even from the next 1h bar (pessimistic)
    r_bt_fast: float | None = None  # break-even inside the trigger bar too (optimistic)
    r_plain: float | None = None  # no break-even
    r_bt_fill: float | None = None  # rule B replayed from the demo fill (the path and exit check)
    r_bt_fill_fast: float | None = None  # rule Bx replayed from the demo fill
    r_diff: float | None = None  # r_demo_bt - r_bt (same units: entry plus path)
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        return d


def setup_window(bars: Bars, signal_ts: int, now_ms: int, window: int = LIVE_WINDOW) -> Bars | None:
    """The last ``window`` bars closed by ``now_ms`` that open at or before ``signal_ts``; None when the signal bar is
    not among them (the data do not reach it)."""
    closed = bars.t + bars.bar_ms <= now_ms
    j = int(np.searchsorted(bars.t, signal_ts, side="right"))
    if j == 0 or int(bars.t[j - 1]) != signal_ts or not bool(closed[j - 1]):
        return None
    return bars.slice(max(0, j - window), j)


def backtest_outcome(
    side: str,
    path: Bars,
    entry_ms: int,
    entry: float,
    stop: float,
    target: float,
    hold_ms: int,
    rule: StopRule,
    now_ms: int,
    max_gap_ms: int = MAX_GAP_MS,
) -> Outcome | None:
    """``stop_management.simulate_stop_rule`` in real prices: a short runs on negated prices. Only 1h bars closed by
    ``now_ms`` are walked, so a trade whose horizon has not passed ends as ``data_end``; so does a path with a hole
    longer than ``max_gap_ms`` (``replay`` passes one bar: any missing hour cuts the path)."""
    keep = path.t + path.bar_ms <= now_ms
    p = Bars(path.t[keep], path.o[keep], path.h[keep], path.low[keep], path.c[keep], path.bar_ms)
    if side == "long":
        return simulate_stop_rule(p, entry_ms, entry, stop, target, hold_ms, rule, max_gap_ms=max_gap_ms)
    return simulate_stop_rule(p.mirrored(), entry_ms, -entry, -stop, -target, hold_ms, rule, max_gap_ms=max_gap_ms)


def _risk_unit(entry: float, stop: float) -> float:
    """The live risk per unit of quantity in real prices (``demo_trader._risk``): distance plus both taker fees."""
    return abs(entry - stop) + TAKER * (abs(entry) + abs(stop))


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _signal(win: Bars, t: DemoTrade) -> tuple[LiveSignal | None, LiveSignal | None]:
    """(signal of the same setup and side, signal of the same setup on the other side) on the last bar of ``win``."""
    same = other = None
    for s in scan(win, t.timeframe, (t.setup,)):
        if s.setup != t.setup or s.signal_ts != t.signal_ts:
            continue
        if s.side == t.side:
            same = s
        else:
            other = s
    return same, other


def replay(
    trade: DemoTrade | Mapping[str, Any],
    bars: Bars,
    path: Bars,
    *,
    now_ms: int,
    hold_ms: int,
    be_frac: float = BE_FRAC,
    tol: Tolerance = Tolerance(),
    window: int = LIVE_WINDOW,
) -> Replay:
    """Verdict of one closed demo trade (module docstring). ``bars``: candles of the signal timeframe covering the
    live window; ``path``: 1h candles from the signal-bar close over the hold; ``hold_ms``: the demo's maximum hold for
    the timeframe (``demo_trader.MAX_HOLD_MS``)."""
    t = trade if isinstance(trade, DemoTrade) else DemoTrade.from_row(trade)
    base = {
        "link_id": t.link_id,
        "symbol": t.symbol,
        "setup": t.setup,
        "timeframe": t.timeframe,
        "side": t.side,
        "signal_ts": t.signal_ts,
        "closed_ts": t.closed_ts,
        "demo_exit": t.exit_reason,
        "r_demo": t.r_multiple,
    }
    if t.setup not in DETECTORS or t.timeframe not in TF_MS:
        return Replay(**base, verdict="unsupported", note=f"no candle detector for {t.setup} {t.timeframe}")
    if bars.bar_ms != TF_MS[t.timeframe]:
        return Replay(**base, verdict="no_data", note=f"bars are {bars.bar_ms} ms, the setup needs {t.timeframe}")
    win = setup_window(bars, t.signal_ts, now_ms, window)
    if win is None:
        return Replay(**base, verdict="no_data", note="the signal bar is not in the candles")
    same, other = _signal(win, t)
    if same is None:
        if other is not None:
            return Replay(
                **base,
                verdict="mismatch",
                reasons=("side",),
                note=f"the detector gives a {other.side} {t.setup} on that bar",
                bt_entry=other.entry,
                bt_stop=other.stop,
                bt_target=other.target,
            )
        return Replay(**base, verdict="no_signal", note=f"no {t.side} {t.setup} on that bar in {len(win)} bars")
    risk = abs(same.entry - same.stop)
    fill = t.fill if t.fill is not None else t.entry
    sgn = 1.0 if t.side == "long" else -1.0
    edge = sgn * (same.entry - fill) / risk
    lv = {
        "bt_entry": same.entry,
        "bt_stop": same.stop,
        "bt_target": same.target,
        "entry_diff_r": abs(edge),
        "entry_edge_r": edge,
        "plan_edge_r": sgn * (same.entry - t.entry) / risk,
        "stop_diff_r": abs(t.stop - same.stop) / risk,
        "target_diff_r": abs(t.target - same.target) / risk,
    }
    entry_ms = t.signal_ts + TF_MS[t.timeframe]
    gap = path.bar_ms  # never walk over a missing 1h bar
    rb, rbx = StopRule("B", be_frac=be_frac), StopRule("Bx", be_frac=be_frac, be_same_bar=True)

    def run(entry: float, rule: StopRule) -> Outcome | None:
        return backtest_outcome(t.side, path, entry_ms, entry, same.stop, same.target, hold_ms, rule, now_ms, gap)

    b, bx, a = run(same.entry, rb), run(same.entry, rbx), run(same.entry, StopRule("A"))
    bf, bfx = run(fill, rb), run(fill, rbx)  # None when the fill is at or beyond the stop
    horizon_open = entry_ms + hold_ms > now_ms
    if b is None or bx is None:
        if horizon_open and entry_ms + path.bar_ms > now_ms:
            return Replay(**base, verdict="pending", note="no 1h bar has closed since the signal yet", **lv)
        return Replay(**base, verdict="no_data", note="no 1h path after the signal bar", **lv)
    cut = [o.exit_ms for o in (b, bx, bf, bfx) if o is not None and o.exit_kind == "data_end"]
    if cut:
        end = min(cut)  # close of the last 1h bar walked
        if horizon_open and end + path.bar_ms > now_ms:
            return Replay(**base, verdict="pending", note="the backtest trade is still open", **lv)
        return Replay(**base, verdict="no_data", note=f"the 1h path is cut at {_iso(end)}", **lv)
    rs = {
        "r_bt": b.r_net,
        "r_bt_fast": bx.r_net,
        "r_plain": None if a is None or a.exit_kind == "data_end" else a.r_net,
        "r_bt_fill": None if bf is None else bf.r_net,
        "r_bt_fill_fast": None if bfx is None else bfx.r_net,
        "bt_exit": _KIND.get(b.exit_kind, "time"),
    }
    reasons = []
    if lv["entry_diff_r"] > tol.entry_r:
        reasons.append("entry")
    if lv["stop_diff_r"] > tol.level_r:
        reasons.append("stop")
    if lv["target_diff_r"] > tol.level_r:
        reasons.append("target")
    r_diff = r_demo_bt = None
    if t.r_multiple is None:
        reasons.append("no demo R")
    else:
        r_demo_bt = t.r_multiple * _risk_unit(fill, t.stop) / _risk_unit(same.entry, same.stop)
        r_diff = r_demo_bt - b.r_net
        if bf is not None and bfx is not None:  # a fill beyond the stop is already an 'entry' reason
            lo, hi = sorted((bf.r_net, bfx.r_net))
            if max(lo - t.r_multiple, t.r_multiple - hi, 0.0) > tol.r:
                reasons.append("r")
    return Replay(
        **base,
        verdict="mismatch" if reasons else "match",
        reasons=tuple(reasons),
        r_demo_bt=r_demo_bt,
        r_diff=r_diff,
        **lv,
        **rs,
    )


def summary(rows: Sequence[Replay], *, min_n: int = MIN_TRADES) -> dict[str, Any]:
    """The 'demo vs backtest' numbers of ``rows`` (the caller passes the running total from the first demo trade, so
    trades pending last week are counted once resolved): verdict counts (all and per setup), mismatch reasons, mean R
    of the demo (in the backtest's risk units) and of the backtest over the trades with both, the mean difference with a
    t 95 % CI, the mean signed entry edge, and a Ukrainian conclusion that says 'замало для висновку' below ``min_n``
    decided trades."""
    counts = {k: sum(1 for x in rows if x.verdict == k) for k in VERDICTS}
    decided = sum(counts[k] for k in DECIDED)
    per_setup: dict[str, dict[str, int]] = {}
    for x in rows:
        per_setup.setdefault(x.setup, dict.fromkeys(VERDICTS, 0))[x.verdict] += 1
    both = [x for x in rows if x.r_diff is not None and x.r_bt is not None and x.r_demo_bt is not None]
    diffs = [float(x.r_diff) for x in both if x.r_diff is not None]
    edges = [float(x.entry_edge_r) for x in rows if x.entry_edge_r is not None]
    out: dict[str, Any] = {
        "n": len(rows),
        "decided": decided,
        "counts": counts,
        "per_setup": per_setup,
        "mismatch_reasons": dict(Counter(r for x in rows if x.verdict == "mismatch" for r in x.reasons)),
        "match_share": counts["match"] / decided if decided else None,
        "n_r": len(both),
        "mean_r_demo_bt": statistics.fmean(float(x.r_demo_bt) for x in both if x.r_demo_bt is not None) if both else None,
        "mean_r_bt": statistics.fmean(float(x.r_bt) for x in both if x.r_bt is not None) if both else None,
        "mean_r_diff": statistics.fmean(diffs) if diffs else None,
        "ci95_r_diff": None,
        "n_entry": len(edges),
        "mean_entry_edge_r": statistics.fmean(edges) if edges else None,
        "enough": decided >= min_n,
        "min_n": min_n,
    }
    if len(diffs) >= 2:
        h = t975(len(diffs) - 1) * statistics.stdev(diffs) / len(diffs) ** 0.5
        out["ci95_r_diff"] = [out["mean_r_diff"] - h, out["mean_r_diff"] + h]
    if not out["enough"]:
        out["conclusion"] = f"замало для висновку: звірено {decided} угод, потрібно ≥ {min_n}"
    else:
        ci = out["ci95_r_diff"]
        tail = f" [{ci[0]:+.2f}; {ci[1]:+.2f}]" if ci else ""
        md = out["mean_r_diff"]
        out["conclusion"] = f"збіг із бектестом {out['match_share']:.0%} угод ({counts['match']} з {decided})" + (
            f"; середня різниця R (демо − бектест) {md:+.2f}{tail}" if md is not None else ""
        )
    return out
