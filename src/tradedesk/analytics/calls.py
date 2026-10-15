"""Forward tracker of trading calls from the owner's library (bloggers' entry zones, targets, stops).

Pure functions: the calls file is parsed from text and every call is evaluated on daily bars passed in by the caller.
Rules, chosen to be conservative where daily bars hide the order of events:

* evaluation starts on the day after the call (the call's own day holds prices from before it was made) and lasts
  ``horizon_days``; only complete daily bars are passed in;
* long: the zone is touched on the first bar whose low reaches ``zone_high``; entry = min(open, zone_high), so a gap
  through the zone fills at the open. Short mirrors it (high reaches ``zone_low``; entry = max(open, zone_low));
* the stop counts from the touch bar itself, the target only from the next bar; a bar that reaches both is a stop;
* ``max_gain`` uses bars after the touch bar, ``max_drawdown`` includes it;
* ``hold_return`` is the baseline of acting at once: from the first bar's open to the last close, same side.

All returns are decimals (0.25 = +25 %) in the direction of the call.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, timedelta

FIELDS = ("id", "date", "source", "asset", "side", "zone_low", "zone_high", "target", "stop", "horizon_days", "symbol", "note")
DEFAULT_HORIZON = 90


@dataclass(frozen=True)
class Call:
    id: str
    day: date
    source: str
    asset: str
    side: str  # "long" | "short"
    zone_low: float
    zone_high: float
    target: float | None = None
    stop: float | None = None
    horizon_days: int = DEFAULT_HORIZON
    symbol: str = ""  # optional "venue:SYMBOL" override, e.g. "bybit:VVV/USDT:USDT" or "hyperliquid:kPEPE"
    note: str = ""

    @property
    def start(self) -> date:
        return self.day + timedelta(days=1)

    @property
    def end(self) -> date:
        """Last day of the evaluation window (inclusive)."""
        return self.day + timedelta(days=self.horizon_days)


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class CallResult:
    status: str  # waiting | expired | open | ended | target | stopped | no_data
    bars: int
    touched_on: date | None = None
    days_to_touch: int | None = None
    entry: float | None = None
    closed_on: date | None = None
    result: float | None = None
    max_gain: float | None = None
    max_drawdown: float | None = None
    hold_return: float | None = None
    last_close: float | None = None
    first_open: float | None = None  # open of the first bar: a price far from the zone hints at a wrong ticker


def _num(raw: str, field: str, errors: list[str], row_id: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        errors.append(f"{row_id}: {field} is not a number: {raw!r}")
        return None
    if v <= 0:
        errors.append(f"{row_id}: {field} must be positive")
        return None
    return v


def parse_calls(text: str) -> tuple[list[Call], list[str]]:
    """Calls from the CSV text (header ``FIELDS``; lines starting with ``#`` are comments) and the rows refused."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    calls: list[Call] = []
    errors: list[str] = []
    seen: set[str] = set()
    for row in csv.DictReader(io.StringIO("\n".join(lines))):
        cid = (row.get("id") or "").strip()
        if not cid or cid in seen:
            errors.append(f"{cid or '?'}: missing or duplicate id")
            continue
        try:
            day = date.fromisoformat((row.get("date") or "").strip())
        except ValueError:
            errors.append(f"{cid}: bad date {row.get('date')!r}")
            continue
        side = (row.get("side") or "").strip().lower()
        if side not in ("long", "short"):
            errors.append(f"{cid}: side must be long or short")
            continue
        n = len(errors)
        lo = _num(row.get("zone_low", ""), "zone_low", errors, cid)
        hi = _num(row.get("zone_high", ""), "zone_high", errors, cid) or lo
        lo = lo or hi
        target = _num(row.get("target", ""), "target", errors, cid)
        stop = _num(row.get("stop", ""), "stop", errors, cid)
        horizon = (row.get("horizon_days") or "").strip()
        if lo is None or hi is None:
            errors.append(f"{cid}: zone_low or zone_high is required")
        elif lo > hi:
            errors.append(f"{cid}: zone_low > zone_high")
        elif side == "long" and ((target and target <= hi) or (stop and stop >= lo)):
            errors.append(f"{cid}: a long needs target above the zone and stop below it")
        elif side == "short" and ((target and target >= lo) or (stop and stop <= hi)):
            errors.append(f"{cid}: a short needs target below the zone and stop above it")
        if horizon and not horizon.isdigit():
            errors.append(f"{cid}: horizon_days must be a whole number of days")
        if len(errors) > n:
            continue
        seen.add(cid)
        calls.append(
            Call(
                id=cid,
                day=day,
                source=(row.get("source") or "").strip(),
                asset=(row.get("asset") or "").strip().upper(),
                side=side,
                zone_low=lo,
                zone_high=hi,
                target=target,
                stop=stop,
                horizon_days=int(horizon) if horizon else DEFAULT_HORIZON,
                symbol=(row.get("symbol") or "").strip(),
                note=(row.get("note") or "").strip(),
            )
        )
    return calls, errors


def _ret(side: str, entry: float, exit_: float) -> float:
    return exit_ / entry - 1.0 if side == "long" else 1.0 - exit_ / entry


def evaluate_call(call: Call, bars: list[Bar], today: date) -> CallResult:
    """Evaluate ``call`` on daily ``bars`` (any order; bars outside the window are ignored). ``today`` is the first day
    without a complete bar: the window is over when ``call.end < today``."""
    window = sorted((b for b in bars if call.start <= b.day <= call.end), key=lambda b: b.day)
    over = call.end < today
    if not window:
        return CallResult(status="waiting" if today <= call.start else "no_data", bars=0)
    long = call.side == "long"
    hold = _ret(call.side, window[0].open, window[-1].close)
    touch = next((i for i, b in enumerate(window) if (b.low <= call.zone_high if long else b.high >= call.zone_low)), None)
    if touch is None:
        return CallResult(
            status="expired" if over else "waiting",
            bars=len(window),
            hold_return=hold,
            last_close=window[-1].close,
            first_open=window[0].open,
        )
    tb = window[touch]
    entry = min(tb.open, call.zone_high) if long else max(tb.open, call.zone_low)
    after = window[touch + 1 :]
    worst = min(b.low for b in window[touch:]) if long else max(b.high for b in window[touch:])
    best = (max(b.high for b in after) if long else min(b.low for b in after)) if after else None
    base = CallResult(
        status="ended" if over else "open",
        bars=len(window),
        touched_on=tb.day,
        days_to_touch=(tb.day - call.day).days,
        entry=entry,
        max_gain=_ret(call.side, entry, best) if best is not None else None,
        max_drawdown=_ret(call.side, entry, worst),
        hold_return=hold,
        last_close=window[-1].close,
        first_open=window[0].open,
    )
    for i, b in enumerate(window[touch:]):
        stop_hit = call.stop is not None and (b.low <= call.stop if long else b.high >= call.stop)
        if stop_hit:
            fill = min(b.open, call.stop) if long else max(b.open, call.stop)
            if i == 0:  # on the touch bar the stop fills at the stop: the open was before the entry
                fill = call.stop
            return _close(base, "stopped", b.day, _ret(call.side, entry, fill))
        if i > 0 and call.target is not None and (b.high >= call.target if long else b.low <= call.target):
            return _close(base, "target", b.day, _ret(call.side, entry, call.target))
    return _close(base, base.status, window[-1].day if over else None, _ret(call.side, entry, window[-1].close))


def _close(r: CallResult, status: str, on: date | None, result: float) -> CallResult:
    return CallResult(
        status=status,
        bars=r.bars,
        touched_on=r.touched_on,
        days_to_touch=r.days_to_touch,
        entry=r.entry,
        closed_on=on,
        result=result,
        max_gain=r.max_gain,
        max_drawdown=r.max_drawdown,
        hold_return=r.hold_return,
        last_close=r.last_close,
        first_open=r.first_open,
    )


def summarize(pairs: list[tuple[Call, CallResult]]) -> dict[str, dict[str, float | int | None]]:
    """Per source: calls, how many were touched, hit the target or the stop, and the mean result of touched calls
    against the mean ``hold_return`` (acting at once) of the same calls."""
    out: dict[str, dict[str, float | int | None]] = {}
    for source in sorted({c.source for c, _ in pairs}):
        rs = [r for c, r in pairs if c.source == source]
        touched = [r for r in rs if r.entry is not None]
        results = [r.result for r in touched if r.result is not None]
        holds = [r.hold_return for r in touched if r.hold_return is not None]
        out[source] = {
            "calls": len(rs),
            "with_data": sum(1 for r in rs if r.status != "no_data"),
            "touched": len(touched),
            "target": sum(1 for r in rs if r.status == "target"),
            "stopped": sum(1 for r in rs if r.status == "stopped"),
            "mean_result": sum(results) / len(results) if results else None,
            "mean_hold": sum(holds) / len(holds) if holds else None,
        }
    return out
