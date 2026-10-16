"""Replay of one trading call on hourly (or any intraday) bars: limit entry, stop, first target, time exit, in R.

Pure functions; the caller passes bars and the call's moment. Rules, chosen to be conservative where a bar hides the
order of events:

* the replay starts on the first bar that opens at or after ``start_ms`` (the bar holding the call's moment also holds
  prices from before it was made);
* entry is a resting limit at ``entry`` (for a zone the caller passes its near edge). Long: filled on the first bar
  whose low reaches it, at ``min(open, entry)`` (a gap through the limit fills at the open); short mirrors it.
  ``entry=None`` enters at the open of the first bar (a market order);
* the stop counts from the fill bar itself, at ``min(open, stop)`` for a long (the stop on the fill bar fills at the
  stop); the target only from the next bar, at the target; a bar that reaches both is a stop;
* the position is closed at the close of the last bar opening within ``hold_ms`` of the fill bar if neither was reached
  (``time``), or at the last bar given if the data end earlier (``open``); no fill within ``fill_ms`` → ``no_fill``;
* R = move in the direction of the call / the planned risk |entry − stop| (the size is set by the plan, so a better fill
  through a gap loses less than 1R at the stop); for a market entry the planned entry is the fill. ``cost_frac`` is the
  round-trip cost as a fraction of price (0.0014 = taker 0.05 % + slippage 0.02 % per side), converted to R the same way;
* ``paper_*`` replays the same fill with the stop ignored (a «paper» stop the author did not place): exit at the target
  or at the time limit; ``target_after_stop`` tells whether the target was reached within the hold window after the stop;
* ``be_at_r`` (optional management): once a bar after the fill bar reaches +``be_at_r`` R, the stop moves to the fill
  price from the next bar on (``breakeven`` if it is then hit).

Returns are decimals in the direction of the call (0.05 = +5 %).
"""

from __future__ import annotations

from dataclasses import dataclass

HOUR_MS = 3_600_000


@dataclass(frozen=True)
class Bar:
    t: int  # open time, ms
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Replay:
    status: str  # no_data | no_fill | stop | breakeven | target | time | open
    fill_t: int | None = None
    fill: float | None = None
    exit_t: int | None = None
    exit: float | None = None
    ret: float | None = None  # gross return of the trade, decimal
    r_gross: float | None = None
    r_net: float | None = None
    mfe_r: float | None = None  # best excursion before the exit, in R (fill bar excluded)
    mae_r: float | None = None  # worst excursion before the exit, in R (fill bar included)
    bars_to_exit: int | None = None
    paper_status: str | None = None  # target | time | open, with the stop ignored
    paper_ret: float | None = None
    paper_r: float | None = None
    paper_mae_r: float | None = None
    target_after_stop: bool | None = None


def _ret(long: bool, entry: float, px: float) -> float:
    return px / entry - 1.0 if long else 1.0 - px / entry


def replay(
    bars: list[Bar],
    *,
    side: str,
    start_ms: int,
    entry: float | None,
    stop: float | None,
    target: float | None,
    fill_ms: int = 72 * HOUR_MS,
    hold_ms: int = 168 * HOUR_MS,
    cost_frac: float = 0.0014,
    be_at_r: float | None = None,
) -> Replay:
    """Replay a call on ``bars`` (any order, bars may differ in length, e.g. 1m bars of the call's hour followed by 1h
    bars; bars before ``start_ms`` are ignored)."""
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")
    long = side == "long"
    w = sorted((b for b in bars if b.t >= start_ms), key=lambda b: b.t)
    if not w:
        return Replay(status="no_data")
    if entry is None:
        fi, fill = 0, w[0].open
    else:
        window = [b for b in w if b.t < start_ms + fill_ms]
        fi = next((i for i, b in enumerate(window) if (b.low <= entry if long else b.high >= entry)), None)
        if fi is None:
            return Replay(status="no_fill" if w[-1].t >= start_ms + fill_ms else "open")
        fill = min(w[fi].open, entry) if long else max(w[fi].open, entry)
    planned = fill if entry is None else entry
    if stop is not None and (stop >= min(fill, planned) if long else stop <= max(fill, planned)):
        stop = None  # a stop on the wrong side of the entry cannot be honoured; treat the call as having none
    risk = abs(planned - stop) if stop is not None else None
    end = w[fi].t + hold_ms
    held = [b for b in w[fi:] if b.t < end]  # the fill bar and the bars that open within the hold window
    covered = w[-1].t >= end  # the data reach past the hold window

    def r_of(ret: float) -> float | None:
        return ret * fill / risk if risk else None  # ret * fill = the price move per unit

    status, exit_px, exit_i, cur = None, None, None, stop
    trigger = None
    if be_at_r is not None and risk:
        trigger = fill + be_at_r * risk if long else fill - be_at_r * risk
    for i, b in enumerate(held):
        if cur is not None and (b.low <= cur if long else b.high >= cur):
            exit_px = cur if i == 0 else (min(b.open, cur) if long else max(b.open, cur))
            status, exit_i = ("stop" if cur == stop else "breakeven"), i
            break
        if i > 0 and target is not None and (b.high >= target if long else b.low <= target):
            status, exit_px, exit_i = "target", target, i
            break
        if trigger is not None and i > 0 and (b.high >= trigger if long else b.low <= trigger):
            cur, trigger = fill, None  # the stop moves to the fill price from the next bar
    if status is None:
        exit_i = len(held) - 1
        exit_px = held[exit_i].close
        status = "time" if covered else "open"
    path = held[: exit_i + 1]
    after = path[1:]
    best = (max(b.high for b in after) if long else min(b.low for b in after)) if after else None
    worst = min(b.low for b in path) if long else max(b.high for b in path)
    ret = _ret(long, fill, exit_px)

    # the same fill with the stop ignored
    p_status, p_px, p_i = None, None, None
    for i, b in enumerate(held):
        if i > 0 and target is not None and (b.high >= target if long else b.low <= target):
            p_status, p_px, p_i = "target", target, i
            break
    if p_status is None:
        p_i = len(held) - 1
        p_px = held[p_i].close
        p_status = "time" if covered else "open"
    p_worst = min(b.low for b in held[: p_i + 1]) if long else max(b.high for b in held[: p_i + 1])
    p_ret = _ret(long, fill, p_px)
    cost_r = r_of(cost_frac)
    return Replay(
        status=status,
        fill_t=w[fi].t,
        fill=fill,
        exit_t=held[exit_i].t,
        exit=exit_px,
        ret=ret,
        r_gross=r_of(ret),
        r_net=r_of(ret) - cost_r if risk else None,
        mfe_r=r_of(_ret(long, fill, best)) if best is not None else None,
        mae_r=r_of(_ret(long, fill, worst)),
        bars_to_exit=exit_i,
        paper_status=p_status,
        paper_ret=p_ret,
        paper_r=r_of(p_ret),
        paper_mae_r=r_of(_ret(long, fill, p_worst)),
        target_after_stop=(p_status == "target") if status == "stop" and target is not None else None,
    )
