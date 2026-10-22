"""Forward paper engine for the two carry strategies (bookkeeping only: NO orders, no keys, read-only market data).

One cycle runs after each 8h funding payment (00/08/16 UTC, decision time +5 min). It applies the rules of
``analytics/carry_rules.py`` (the same ``carry_step`` / ``spread_step`` the backtests use, fed by the same
``carry_observers`` / ``spread_observers``), opens and closes paper positions, marks the open ones and writes the
pre-registered record to SQLite (``cp_cycle`` / ``cp_position`` / ``cp_log``) and, for closed trades, to the journal
with tags ``cc-paper`` (A) / ``pp-paper`` (B). Registration: docs/ideas/IDEA-20261017-forward-paper-carry.md.

A cycle's data comes from a ``CarryFeed`` (live: ``carry_paper_feed.LiveFeed`` through the providers; tests: a fixture
feed), so the equivalence test can drive the engine over a historical segment and compare it with the backtest.
Units: rates, APRs, returns are decimal fractions; ``*_usd`` fields are fractions x ``slot_notional``.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Protocol

from ..analytics.carry import DAY_MS, _Leg, _Market, carry_observers, spread_observers
from ..analytics.carry_rules import (
    DECISION_DELAY_MS,
    FUNDING_STEP_MS,
    carry_pnl_now,
    carry_side_cost,
    carry_step,
    cycle_grid,
    funding_gap_issue,
    leg_execution,
    spread_pnl_now,
    spread_price_pnl,
    spread_side_cost,
    spread_step,
)
from ..analytics.journal import outcome_of
from ..analytics.journal import pnl as journal_pnl

ON_TIME_MS = 30 * 60_000  # registered reliability bar: cycle executed within 30 min of the payment
BAR_MS = 4 * 3_600_000
GAP_CONFIRM_CYCLES = 3  # held cycles with a persisting funding gap before it is confirmed and the position settles
PERP_VENUE, SPOT_VENUE, BYBIT = "binanceusdm", "binance", "bybit"


@dataclass(frozen=True)
class CarryParams:
    """Registered variant. A: ``fee_1`` spot, ``fee_2`` perp. B: ``fee_1`` Binance perp, ``fee_2`` Bybit perp."""

    strategy: str  # "A" | "B"
    tag: str  # journal tag
    entry_apr: float
    fee_1: float
    fee_2: float
    capital_factor: float  # capital per unit of notional: A spot + 2x perp margin = 1.5, B 2x on each leg = 1.0
    slots: int = 5
    lookback_days: float = 7.0
    exit_apr: float = 0.0
    slippage: float = 0.0005
    slot_notional: float = 2000.0  # USD per leg (portfolio notional 10 000 / 5 slots)
    max_entry_gap: float | None = None
    stop_move: float | None = None
    stop_penalty: float = 0.01
    leverage: float = 2.0
    bar_ms: int = BAR_MS

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:12]


PARAMS_A = CarryParams("A", "cc-paper", entry_apr=0.20, fee_1=0.001, fee_2=0.0005, capital_factor=1.5)
PARAMS_B = CarryParams("B", "pp-paper", entry_apr=0.10, fee_1=0.0005, fee_2=0.00055, capital_factor=1.0, max_entry_gap=0.02, stop_move=0.5)
PARAMS = {"A": PARAMS_A, "B": PARAMS_B}


@dataclass
class FeedData:
    """What a cycle may know at the decision time.

    ``markets``: backtest shape (A: ``{sym: {funding, spot, perp}}``, B: ``{sym: {a, b}}`` with ``bars``).
    ``current``: A ``{sym: {rate, interval_hours, mark, index, next_funding_ts, cap, floor}}``; B the same under
    ``{"a": ..., "b": ...}``. ``spot_symbols``: A ``{sym: (spot symbol, quantity multiplier)}``.
    """

    markets: dict[str, dict[str, Any]]
    excluded: list[dict[str, Any]] = field(default_factory=list)
    current: dict[str, Any] = field(default_factory=dict)
    spot_symbols: dict[str, tuple[str, int]] = field(default_factory=dict)
    universe_size: int = 0
    flags: list[dict[str, Any]] = field(default_factory=list)


class CarryFeed(Protocol):
    async def load(self, params: CarryParams, decision_ts: int, open_positions: list[dict[str, Any]]) -> FeedData: ...

    async def book(self, venue: str, symbol: str) -> dict[str, Any] | None: ...


@lru_cache(maxsize=1)
def git_sha() -> str:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, read-only
            ["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5, check=False
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _base(sym: str) -> str:
    return sym.split("/")[0]


def _window(f_ts: list[int], t: int, lookback_ms: int) -> tuple[int, int | None]:
    """(number of payments in (t - lookback, t], timestamp of the last payment at or before t)."""
    hi, lo = bisect.bisect_right(f_ts, t), bisect.bisect_right(f_ts, t - lookback_ms)
    return hi - lo, (f_ts[hi - 1] if hi else None)


def _interval_hours(f_ts: list[int], t: int) -> float | None:
    hi = bisect.bisect_right(f_ts, t)
    if hi < 2:
        return None
    return round((f_ts[hi - 1] - f_ts[hi - 2]) / 3_600_000, 2)


def _known(iv: Any) -> bool:
    return all(x is not None for x in (iv if isinstance(iv, list) else [iv]))


def _funding_issue(params: CarryParams, p: dict[str, Any], obj: Any, cur: dict[str, Any], t: int, cycle_ts: int) -> str | None:
    """Funding-history check for one held position (see ``funding_gap_issue``); both legs for B."""
    pairs = [(obj.f_ts, cur)] if params.strategy == "A" else [(obj[0].f_ts, cur.get("a") or {}), (obj[1].f_ts, cur.get("b") or {})]
    for f_ts, c in pairs:
        # Expected interval: the exchange's own report; else the history up to `last` (already settled, so not holey in
        # the window under test); else the smaller of the rest. Never the window's own spacing: a hole would inflate it.
        last = int(p["last"])
        iv = c.get("interval_hours") or _interval_hours(f_ts, last)
        if not iv:
            known = [x for x in (_interval_hours(f_ts, t),) if x]
            iv = min(known) if known else None
        issue = funding_gap_issue(f_ts, last, t, cycle_ts, iv)
        if issue:
            return issue
    return None


def _missing_payments(params: CarryParams, p: dict[str, Any], obj: Any, cur: dict[str, Any], t: int, cycle_ts: int) -> int:
    """Payments expected over ``(last, cycle_ts]`` but absent from the history (largest shortfall over B's two legs)."""
    pairs = [(obj.f_ts, cur)] if params.strategy == "A" else [(obj[0].f_ts, cur.get("a") or {}), (obj[1].f_ts, cur.get("b") or {})]
    last, worst = int(p["last"]), 0
    for f_ts, c in pairs:
        iv = c.get("interval_hours") or _interval_hours(f_ts, last) or 8.0
        # ``last`` is a decision time (payment + 5 min), payments fall on cycle_ts - k * interval: count those after ``last``
        expected = int((cycle_ts + 60_000 - last) // (iv * 3_600_000)) + 1
        found = sum(1 for x in f_ts if last < x <= t)
        worst = max(worst, expected - found)
    return max(worst, 0)


def _trade_id(strategy: str, sym: str, open_ts: int) -> str:
    return f"{strategy}:{sym}:{open_ts}"


def _legs(params: CarryParams, p: dict[str, Any], data: FeedData, event: str) -> list[dict[str, Any]]:
    """The two leg sides of a position at ``event`` ('open' | 'close'): venue, symbol, side, fee, model price."""
    sym = p["symbol"]
    opening = event == "open"
    if params.strategy == "A":
        spot_symbol, mult = data.spot_symbols.get(sym, (None, 1))
        cur = data.current.get(sym) or {}
        return [
            {
                "leg": "spot", "venue": SPOT_VENUE, "symbol": spot_symbol, "multiplier": mult, "side": "buy" if opening else "sell",
                "fee": params.fee_1, "model_price": p["spot_e"] if opening else p["spot_last"], "mark": None, "index": None,
            },
            {
                "leg": "perp", "venue": PERP_VENUE, "symbol": sym, "multiplier": 1, "side": "sell" if opening else "buy",
                "fee": params.fee_2, "model_price": p["perp_e"] if opening else p["perp_last"], "mark": cur.get("mark"),
                "index": cur.get("index"),
            },
        ]  # fmt: skip
    cur_a, cur_b = (data.current.get(sym) or {}).get("a") or {}, (data.current.get(sym) or {}).get("b") or {}
    long_a = p["dir"] == 1
    a = {
        "venue": PERP_VENUE, "symbol": sym, "multiplier": 1, "fee": params.fee_1, "mark": cur_a.get("mark"), "index": cur_a.get("index"),
        "model_price": p["a_e"] if opening else p["a_last"],
    }  # fmt: skip
    b = {
        "venue": BYBIT, "symbol": sym, "multiplier": 1, "fee": params.fee_2, "mark": cur_b.get("mark"), "index": cur_b.get("index"),
        "model_price": p["b_e"] if opening else p["b_last"],
    }  # fmt: skip

    def side(is_long: bool) -> str:
        return ("buy" if is_long else "sell") if opening else ("sell" if is_long else "buy")

    return [
        {**a, "leg": "long" if long_a else "short", "side": side(long_a)},
        {**b, "leg": "short" if long_a else "long", "side": side(not long_a)},
    ]


def _funding_rows(
    params: CarryParams, p: dict[str, Any], paid: dict[str, Any], data: FeedData, trade_id: str, cycle_ts: int
) -> list[dict[str, Any]]:
    """One record per funding payment accrued this cycle, signed USD from the paper position's side."""
    out: list[dict[str, Any]] = []
    n = params.slot_notional
    cur = data.current.get(p["symbol"]) or {}
    if params.strategy == "A":
        for ts, rate, price in paid["payments"]:
            px = price or paid["fallback_perp"]
            out.append(
                {
                    "trade_id": trade_id, "leg": "perp", "venue": PERP_VENUE, "payment_ts": ts, "rate": rate,
                    "interval_hours": cur.get("interval_hours"), "mark": px, "usd": rate * px / p["spot_e"] * n,
                }
            )  # fmt: skip
        return [{"strategy": "A", "cycle_ts": cycle_ts, "kind": "funding", "trade_id": trade_id, "payload": x} for x in out]
    for key, venue, sign, ck in (("pay_a", PERP_VENUE, -p["dir"], "a"), ("pay_b", BYBIT, p["dir"], "b")):
        for ts, rate, price in paid[key]:
            px = price or paid["fallback_" + ck]
            out.append(
                {
                    "trade_id": trade_id, "leg": "long" if sign < 0 else "short", "venue": venue, "payment_ts": ts, "rate": rate,
                    "interval_hours": (cur.get(ck) or {}).get("interval_hours"), "mark": px, "usd": sign * rate * px / p["a_e"] * n,
                }
            )  # fmt: skip
    return [{"strategy": "B", "cycle_ts": cycle_ts, "kind": "funding", "trade_id": trade_id, "payload": x} for x in out]


def _mark_payload(params: CarryParams, p: dict[str, Any], apr_now: float | None, data: FeedData, exit_signal: bool) -> dict[str, Any]:
    n = params.slot_notional
    if params.strategy == "A":
        basis = (p["spot_last"] - p["spot_e"] - (p["perp_last"] - p["perp_e"])) / p["spot_e"]
        liq = p["perp_e"] * (1 + 1 / params.leverage)  # short perp liquidation at 2x ignoring MMR (ladder MMR is a backlog item)
        return {
            "spot": p["spot_last"], "perp": p["perp_last"], "basis_perp_over_spot": p["perp_last"] / p["spot_last"] - 1.0,
            "max_runup": p["max_runup"], "max_premium_rise": p["max_premium_rise"], "dist_to_liq_2x": liq / p["perp_last"] - 1.0,
            "funding_usd": p["funding"] * n, "basis_usd": basis * n, "costs_usd": -p["fees"] * n, "pnl_usd": carry_pnl_now(p) * n,
            "trailing_apr": apr_now, "exit_signal": exit_signal,
        }  # fmt: skip
    return {
        "a": p["a_last"], "b": p["b_last"], "gap_a_over_b": p["a_last"] / p["b_last"] - 1.0, "max_up": p["max_up"],
        "max_down": p["max_down"], "max_gap": p["max_gap"], "dist_to_stop": (params.stop_move or 0.0) - max(p["max_up"], p["max_down"]),
        "funding_usd": p["funding"] * n, "price_usd": spread_price_pnl(p) * n, "costs_usd": -p["fees"] * n, "pnl_usd": spread_pnl_now(p) * n,
        "held_spread_apr": apr_now, "exit_signal": exit_signal,
    }  # fmt: skip


def _trade_record(params: CarryParams, tr: dict[str, Any], trade_id: str, reason: str) -> dict[str, Any]:
    n = params.slot_notional
    second = "basis" if params.strategy == "A" else "price_pnl"
    return {
        **tr,
        "trade_id": trade_id,
        "exit_reason": reason,
        "slot_notional": n,
        "days": (tr["close_ts"] - tr["open_ts"]) / DAY_MS,
        "modelled_stop_ts": tr["close_ts"] if tr.get("stopped") else None,
        "funding_usd": tr["funding"] * n,
        "price_usd": tr[second] * n,
        "costs_usd": -tr["fees"] * n,
        "pnl_usd": tr["pnl"] * n,
        "return_on_notional": tr["pnl"],
        "return_on_capital": tr["pnl"] / params.capital_factor,
    }


def _journal_row(params: CarryParams, rec: dict[str, Any]) -> dict[str, Any]:
    sym = rec["symbol"]
    label = f"CC:{_base(sym)}" if params.strategy == "A" else f"PP:{_base(sym)}:{PERP_VENUE}/{BYBIT}"
    ret = rec["return_on_notional"]
    j_pnl = journal_pnl("long", params.slot_notional, 1.0, 1.0 + ret)
    return {
        "opened_ts": rec["open_ts"] // 1000,
        "closed_ts": rec["close_ts"] // 1000,
        "symbol": label,
        "side": "long",
        "size": params.slot_notional,
        "entry": 1.0,
        "exit": 1.0 + ret,
        "thesis": (
            f"Forward paper carry {params.strategy} ({params.tag}) {sym}: {rec['exit_reason']}, funding {rec['funding_usd']:+.2f}, "
            f"{'basis' if params.strategy == 'A' else 'price'} {rec['price_usd']:+.2f}, costs {rec['costs_usd']:+.2f} USD on a "
            f"{params.slot_notional:g} USD slot; engine trade {rec['trade_id']}. Entry 1.0 / exit 1 + return encodes the return on notional."
        ),
        "tags": [params.tag],
        "outcome": outcome_of(j_pnl),
        "pnl": j_pnl,
    }


async def _side_rows(
    feed: CarryFeed, params: CarryParams, p: dict[str, Any], data: FeedData, event: str, trade_id: str, cycle_ts: int
) -> list[dict[str, Any]]:
    rows = []
    for leg in _legs(params, p, data, event):
        try:
            book = await feed.book(leg["venue"], leg["symbol"]) if leg["symbol"] else None
        except Exception:  # noqa: BLE001 - a missing book is recorded, never fatal
            book = None
        ex = leg_execution(book, leg["side"], params.slot_notional, fee=leg["fee"], model_slippage=params.slippage, cycle_ts=cycle_ts)
        price = leg["model_price"]
        payload = {
            "trade_id": trade_id, "event": event, "leg": leg["leg"], "venue": leg["venue"], "symbol": leg["symbol"],
            "contract_multiplier": leg["multiplier"], "qty": params.slot_notional / price if price else None, "model_price": price,
            "mark": leg["mark"], "index": leg["index"], "perp_symbol": p["symbol"], **ex,
        }  # fmt: skip
        rows.append({"strategy": params.strategy, "cycle_ts": cycle_ts, "kind": "side", "trade_id": trade_id, "payload": payload})
    return rows


def _candidates(
    params: CarryParams, t: int, lookback_ms: int, res: dict[str, Any], open_syms: set[str], objs: dict[str, Any], data: FeedData
) -> list[dict[str, Any]]:
    out = []
    ranked_syms = {r[1] for r in res["ranked"]}
    for sym in sorted(ranked_syms | open_syms):
        if sym not in objs:
            continue
        cur = data.current.get(sym) or {}
        if params.strategy == "A":
            m = objs[sym]
            n, last = _window(m.f_ts, t, lookback_ms)
            out.append(
                {
                    "symbol": sym, "trailing_apr": m.apr(t, lookback_ms), "n_payments_window": n, "last_payment_ts": last,
                    "current_rate": cur.get("rate"), "interval_hours": cur.get("interval_hours") or _interval_hours(m.f_ts, t),
                    "cap": cur.get("cap"), "floor": cur.get("floor"), "spot": m.spot(t), "perp": m.perp(t),
                    "rank": next((i + 1 for i, r in enumerate(res["ranked"]) if r[1] == sym), None), "open": sym in open_syms,
                }
            )  # fmt: skip
        else:
            a, b = objs[sym]
            na, la = _window(a.f_ts, t, lookback_ms)
            nb, lb = _window(b.f_ts, t, lookback_ms)
            ra, rb = a.apr(t, lookback_ms), b.apr(t, lookback_ms)
            pa, pb = a.price(t), b.price(t)
            out.append(
                {
                    "symbol": sym, "apr_a": ra, "apr_b": rb, "spread": None if ra is None or rb is None else rb - ra,
                    "n_payments_window": [na, nb], "last_payment_ts": [la, lb], "current_rate": [(cur.get("a") or {}).get("rate"), (cur.get("b") or {}).get("rate")],
                    "interval_hours": [(cur.get("a") or {}).get("interval_hours") or _interval_hours(a.f_ts, t), (cur.get("b") or {}).get("interval_hours") or _interval_hours(b.f_ts, t)],
                    "cap": [(cur.get("a") or {}).get("cap"), (cur.get("b") or {}).get("cap")], "price_a": pa, "price_b": pb,
                    "gap": pa / pb - 1.0 if pa and pb else None,
                    "rank": next((i + 1 for i, r in enumerate(res["ranked"]) if r[1] == sym), None), "open": sym in open_syms,
                }
            )  # fmt: skip
    return out


async def run_cycle(
    store: Any,
    feed: CarryFeed,
    params: CarryParams,
    cycle_ts: int,
    *,
    clock: Callable[[], int],
) -> dict[str, Any]:
    """Run (and persist) one cycle for ``params.strategy`` scheduled at ``cycle_ts`` (UTC ms). Never raises.

    Returns ``{"status": "done" | "exists" | "failed", ...}``. A failed attempt is logged (``cp_log`` kind 'event') and
    the cycle stays unwritten so the next job tick retries it until a later cycle makes it a recorded skip.
    """
    started = clock()
    strat = params.strategy
    try:
        if await store.cp_cycle_get(strat, cycle_ts) is not None:
            return {"status": "exists", "cycle_ts": cycle_ts}
        t = cycle_ts + DECISION_DELAY_MS
        lookback_ms = int(params.lookback_days * DAY_MS)
        rows = await store.cp_positions(strat, "open")
        open_pos = {r["state"]["symbol"]: r["state"] for r in rows}
        ids = {r["state"]["symbol"]: r["trade_id"] for r in rows}
        data = await feed.load(params, t, rows)
        if params.strategy == "A":
            objs: dict[str, Any] = {
                s: _Market(d, params.bar_ms) for s, d in data.markets.items() if d.get("funding") and d.get("spot") and d.get("perp")
            }
            mark_at, entry_at = carry_observers(objs, lookback_ms)
            side_cost = carry_side_cost(params.fee_1, params.fee_2, params.slippage)
        else:
            objs = {
                s: (_Leg(d["a"], params.bar_ms), _Leg(d["b"], params.bar_ms))
                for s, d in data.markets.items()
                if d.get("a", {}).get("funding") and d.get("b", {}).get("funding") and d["a"].get("bars") and d["b"].get("bars")
            }
            mark_at, entry_at = spread_observers(objs, lookback_ms)
            side_cost = spread_side_cost(params.fee_1, params.fee_2, params.slippage)
        flags = list(data.flags)
        aside = {s: open_pos.pop(s) for s in list(open_pos) if s not in objs}
        for s in aside:
            flags.append({"symbol": s, "issue": "open_position_data_unavailable"})
        # Settling a position moves its ``last`` to ``t``: a funding payment absent from the loaded history would be lost
        # for good. Such a position is held (not settled, ``last`` stays) and flagged; the next cycle settles it in full.
        incomplete = {x["symbol"] for x in data.flags if x.get("issue") == "funding_history_incomplete" and x.get("symbol")}
        # R2: the feed re-reads the window of a held symbol on the next cycle (``gap_cycles`` marks it); if the payment is
        # still absent after GAP_CONFIRM_CYCLES cycles the venue will not publish it, so the position settles with the
        # flag ``funding_gap_confirmed`` and the shortfall is written as an event (a held position must not hang forever).
        held: list[dict[str, Any]] = []
        confirmed: list[dict[str, Any]] = []
        for s in list(open_pos):
            issue = _funding_issue(params, open_pos[s], objs[s], data.current.get(s) or {}, t, cycle_ts)
            gaps = int(open_pos[s].get("gap_cycles") or 0)
            if s in incomplete:
                # Our own fetch was incomplete (page limit, 429/418): hold without counting towards the confirmation, or
                # a rate-limited feed would settle the position and lose published payments for good.
                aside[s] = open_pos.pop(s)
                held.append({"symbol": s, "issue": "funding_payment_missing", "detail": "funding_history_incomplete", "gap_cycles": gaps})
                continue
            if issue is None:
                if gaps:
                    open_pos[s] = {k: v for k, v in open_pos[s].items() if k != "gap_cycles"}
                continue
            gaps += 1
            if gaps >= GAP_CONFIRM_CYCLES:
                missing = _missing_payments(params, open_pos[s], objs[s], data.current.get(s) or {}, t, cycle_ts)
                confirmed.append(
                    {"symbol": s, "issue": "funding_gap_confirmed", "detail": issue, "cycles": gaps, "missing_payments": missing}
                )
                open_pos[s] = {k: v for k, v in open_pos[s].items() if k != "gap_cycles"}
                continue
            aside[s] = {**open_pos.pop(s), "gap_cycles": gaps}
            held.append({"symbol": s, "issue": "funding_payment_missing", "detail": issue, "gap_cycles": gaps})
        flags += held + confirmed
        confirmed_by_sym = {c["symbol"]: c for c in confirmed}
        tradable = [s for s in objs if s not in aside]  # a held-aside position must not be re-opened as a new entry
        before = dict(open_pos)
        before_last = {s: p.get("last") for s, p in before.items()}
        paid: dict[str, dict[str, Any]] = {}

        def mark_obs(sym: str, p: dict[str, Any]) -> dict[str, Any]:
            o = mark_at(sym, p, t)
            fb = {"fallback_perp": p["perp_last"]} if strat == "A" else {"fallback_a": p["a_last"], "fallback_b": p["b_last"]}
            paid[sym] = {**o, **fb}
            return o

        if strat == "A":
            res = carry_step(
                open_pos, t, tradable, mark_obs, partial(entry_at, at=t), slots=params.slots - len(aside), entry_apr=params.entry_apr,
                exit_apr=params.exit_apr, side_cost=side_cost,
            )  # fmt: skip
        else:
            res = spread_step(
                open_pos, t, tradable, mark_obs, partial(entry_at, at=t), slots=params.slots - len(aside), entry_apr=params.entry_apr,
                exit_apr=params.exit_apr, side_cost=side_cost, max_entry_gap=params.max_entry_gap, stop_move=params.stop_move,
                stop_penalty=params.stop_penalty,
            )  # fmt: skip
        reasons = {d["symbol"]: d["reason"] for d in res["decisions"] if d["action"] == "close"}
        logs: list[dict[str, Any]] = [
            {
                "strategy": strat,
                "cycle_ts": cycle_ts,
                "kind": "event",
                "trade_id": ids.get(h["symbol"]),
                "payload": {"type": "funding_payment_missing", **h},
            }
            for h in held
        ]
        logs += [
            {
                "strategy": strat,
                "cycle_ts": cycle_ts,
                "kind": "event",
                "trade_id": ids.get(c["symbol"]),
                "payload": {"type": "funding_gap_confirmed", **c, "last": before_last.get(c["symbol"]), "settled_at": t},
            }
            for c in confirmed
        ]
        positions: list[dict[str, Any]] = []
        for tr in res["closed"]:
            sym = tr["symbol"]
            p, tid = before[sym], ids[sym]
            reason = "stop" if reasons[sym] == "stop" else "signal"
            rec = _trade_record(params, tr, tid, reason)
            if sym in confirmed_by_sym:
                rec["funding_gap_confirmed"] = confirmed_by_sym[sym]["missing_payments"]
            logs += _funding_rows(params, p, paid[sym], data, tid, cycle_ts)
            logs += await _side_rows(feed, params, p, data, "close", tid, cycle_ts)
            logs.append({"strategy": strat, "cycle_ts": cycle_ts, "kind": "close", "trade_id": tid, "payload": rec})
            positions.append(
                {
                    "trade_id": tid, "strategy": strat, "symbol": sym, "status": "closed", "opened_ts": tr["open_ts"],
                    "closed_ts": tr["close_ts"], "slot_notional": params.slot_notional, "state": p, "trade": rec,
                    "exit_reason": reason, "journal": _journal_row(params, rec),
                }
            )  # fmt: skip
        decisions_by_sym = {d["symbol"]: d for d in res["decisions"]}
        for sym, p in open_pos.items():
            if sym in res["opened"]:
                tid = _trade_id(strat, sym, cycle_ts)
                positions.append(
                    {
                        "trade_id": tid, "strategy": strat, "symbol": sym, "status": "open", "opened_ts": t,
                        "slot_notional": params.slot_notional, "state": p,
                    }
                )  # fmt: skip
                logs += await _side_rows(feed, params, p, data, "open", tid, cycle_ts)
                continue
            tid = ids[sym]
            d = decisions_by_sym.get(sym, {})
            apr_now = d.get("apr") if strat == "A" else d.get("spread")
            logs += _funding_rows(params, p, paid[sym], data, tid, cycle_ts)
            logs.append(
                {
                    "strategy": strat, "cycle_ts": cycle_ts, "kind": "mark", "trade_id": tid,
                    "payload": _mark_payload(params, p, apr_now, data, exit_signal=False),
                }
            )  # fmt: skip
            positions.append(
                {
                    "trade_id": tid, "strategy": strat, "symbol": sym, "status": "open", "opened_ts": p["open_ts"],
                    "slot_notional": params.slot_notional, "state": p,
                }
            )  # fmt: skip
        for sym, p in aside.items():
            positions.append(
                {
                    "trade_id": ids[sym], "strategy": strat, "symbol": sym, "status": "open", "opened_ts": p["open_ts"],
                    "slot_notional": params.slot_notional, "state": p,
                }
            )  # fmt: skip
        # Interval change on a held symbol is an exchange event worth keeping. Compare with the last cycle that ran: a
        # skipped cycle has an empty payload and would hide the change.
        prev = await store.cp_cycles(strat, 1, status="done")
        prev_iv = ((prev[0].get("payload") or {}).get("intervals") or {}) if prev else {}
        intervals: dict[str, Any] = {}
        for sym in list(open_pos) + list(aside):
            cur = data.current.get(sym) or {}
            iv = (
                cur.get("interval_hours")
                if strat == "A"
                else [(cur.get("a") or {}).get("interval_hours"), (cur.get("b") or {}).get("interval_hours")]
            )
            intervals[sym] = iv
            if sym in prev_iv and _known(prev_iv[sym]) and _known(iv) and prev_iv[sym] != iv:
                logs.append(
                    {
                        "strategy": strat, "cycle_ts": cycle_ts, "kind": "event", "trade_id": ids.get(sym),
                        "payload": {"type": "funding_interval_change", "symbol": sym, "old": prev_iv[sym], "new": iv},
                    }
                )  # fmt: skip
        excluded_counts: dict[str, int] = {}
        for x in data.excluded:
            excluded_counts[x["reason"]] = excluded_counts.get(x["reason"], 0) + 1
        finished = clock()
        payload = {
            "decision_ts": t,
            "universe_size": data.universe_size,
            "with_data": len(objs),
            "excluded_counts": excluded_counts,
            "excluded": data.excluded[:500],
            "candidates": _candidates(params, t, lookback_ms, res, set(before) | set(res["opened"]), objs, data),
            "ranked": [list(r) for r in res["ranked"][:30]],
            "free_slots": res["free_slots"],
            "decisions": res["decisions"],
            "flags": flags,
            "intervals": intervals,
            "late_ms": started - cycle_ts,
            "finish_late_ms": finished - cycle_ts,
            "on_time": finished - cycle_ts <= ON_TIME_MS,  # registered criterion (d) counts the cycle's finish, not its start
            "complete": not flags,
        }
        cycle = {
            "strategy": strat, "cycle_ts": cycle_ts, "status": "done", "started_ms": started, "finished_ms": finished,
            "git_sha": git_sha(), "params_hash": params.hash(), "payload": payload,
        }  # fmt: skip
        if not await store.cp_cycle_commit(cycle, positions, logs):
            return {"status": "exists", "cycle_ts": cycle_ts}
        return {
            "status": "done",
            "cycle_ts": cycle_ts,
            "opened": res["opened"],
            "closed": [x["symbol"] for x in res["closed"]],
            "flags": len(flags),
        }
    except Exception as e:  # noqa: BLE001 - the engine never raises; the attempt is journaled
        err = f"{type(e).__name__}: {str(e)[:200]}"
        try:
            await store.cp_log_add(
                [
                    {
                        "strategy": strat,
                        "cycle_ts": cycle_ts,
                        "kind": "event",
                        "trade_id": None,
                        "payload": {"type": "cycle_failed", "error": err, "at_ms": started},
                    }
                ]
            )
        except Exception:  # noqa: BLE001
            pass
        return {"status": "failed", "cycle_ts": cycle_ts, "error": err}


async def _skip_reason(store: Any, strategy: str, cycle_ts: int) -> str:
    """'failed: N attempts, last error: ...' when ``run_cycle`` tried this cycle and failed; else 'missed: ...'."""
    events = await store.cp_logs(strategy, "event", cycle_ts=cycle_ts, limit=None)
    fails = [e["payload"] for e in events if (e.get("payload") or {}).get("type") == "cycle_failed"]
    if not fails:
        return "missed: engine did not run in time"
    return f"failed: {len(fails)} attempts, last error: {fails[-1].get('error')}"


async def run_due(store: Any, feed: CarryFeed, params: CarryParams, *, clock: Callable[[], int]) -> dict[str, Any]:
    """Run the latest due cycle; cycles missed since the last recorded one become recorded skips (with the reason:
    'failed: N attempts, last error: ...' when attempts were journaled as ``cycle_failed``, else 'missed: ...')."""
    now = clock()
    cur = cycle_grid(now)
    last = await store.cp_cycles(params.strategy, 1)
    if last and last[0]["cycle_ts"] >= cur:
        return {"status": "idle", "cycle_ts": cur}
    if last:
        ts = last[0]["cycle_ts"] + FUNDING_STEP_MS
        while ts < cur:
            await store.cp_cycle_commit(
                {
                    "strategy": params.strategy,
                    "cycle_ts": ts,
                    "status": "skipped",
                    "reason": await _skip_reason(store, params.strategy, ts),
                    "started_ms": now,
                    "finished_ms": now,
                    "git_sha": git_sha(),
                    "params_hash": params.hash(),
                    "payload": {},
                },
                [],
                [],
            )
            ts += FUNDING_STEP_MS
    return await run_cycle(store, feed, params, cur, clock=clock)
