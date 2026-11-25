"""Replay of the forward paper carry engine's journal with the backtest rule code (criterion (a) of the registration).

Pure (no I/O; time is a parameter). Registration: docs/ideas/IDEA-20261017-forward-paper-carry.md, section
"Реєстрація перевірки (а)". Two checks:

* (a.1) the engine's journal (``cp_cycle.payload`` / ``cp_log`` / ``cp_position``) replayed through ``carry_step`` /
  ``spread_step`` (the code the backtests ``simulate_carry_portfolio`` / ``simulate_perp_spread_portfolio`` run) with
  observations taken from the journal itself and the position state chained by the replay, not read back from the
  engine: every decision and every accounting number must match to 1e-6 relative;
* (a.2) the same step per cycle, from the engine's book before the cycle, with the backtest observers
  (``carry_observers`` / ``spread_observers`` on ``_Market`` / ``_Leg``) over freshly downloaded history; each
  divergence gets one class by a fixed rule (``classify``) on the concrete input differences behind it.

Limit (stated in the registration): the engine calls the same step and observer code, so in both checks the rule logic
itself is equal by construction. The replay tests the plumbing around it: the state carried between cycles, slots with
positions held aside, stopped entries, the journal's completeness and its accounting (a.1), and the live feed's data
against re-downloaded history (a.2). Whether a position *should* have been set aside is not checked by either.

Units: rates, APRs and returns are decimal fractions of slot notional; ``*_usd`` = fraction x slot notional.
"""

from __future__ import annotations

import bisect
import copy
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from .carry import DAY_MS, _Leg, carry_observers, spread_observers
from .carry_rules import (
    DECISION_DELAY_MS,
    FUNDING_STEP_MS,
    carry_basis,
    carry_pnl_now,
    carry_step,
    spread_pnl_now,
    spread_price_pnl,
    spread_step,
)

REL_TOL = 1e-6  # registered: relative tolerance of every accounting number in (a.1)
ABS_TOL = 1e-9  # floor for numbers near zero, as a fraction of slot notional; prices and quantities get no floor
PERP_VENUE, SPOT_VENUE, BYBIT = "binanceusdm", "binance", "bybit"
# Owner decision 2026-11-11: A's new entries stopped from the 2026-11-12 00:00 UTC cycle. Any other cycle with
# ``entries_stopped`` (B at all, A earlier) is a (a.1) mismatch.
ENTRIES_STOP_FROM = {"A": 1_794_441_600_000}
ASIDE_ISSUES = frozenset({"funding_payment_missing", "open_position_data_unavailable"})
FUNDING_DATA_ISSUES = frozenset(
    {"funding_history_incomplete", "funding_rows_dropped", "funding_rows_gap_confirmed", "funding_history_unavailable"}
)
ENGINE_DATA_ISSUES = FUNDING_DATA_ISSUES | {"bars_or_funding_unavailable"}
FUNDING_DIFF_MAX = 2  # (a.2) 'funding_data': at most this many differing payments of the symbol in (t - lookback, t]
STALE_BARS = 2  # (a.2): a recorded price equal to one of the previous 2 downloaded closes = a bar the engine missed
# Observation keys that come from funding data vs from price bars (mark observation dict, entry tuple positions).
_F_MARK = {"A": ("payments", "apr"), "B": ("pay_a", "pay_b", "ra", "rb")}
_P_MARK = {"A": ("perp_high", "spot", "perp"), "B": ("ext_a", "ext_b", "pa", "pb")}
_F_ENTRY = {"A": (0,), "B": (0, 1)}
_P_ENTRY = {"A": (1, 2), "B": (2, 3)}
CLASSES = (
    "data_hold",
    "engine_data_unavailable",
    "not_in_engine_universe",
    "not_in_downloaded_history",
    "not_checked",
    "funding_data",
    "price_data",
    "funding_and_price_data",
    "unexplained",
)


@dataclass(frozen=True)
class Rules:
    """The registered variant as the step functions need it (built from the engine's ``CarryParams`` by ``rules_from``)."""

    strategy: str
    slots: int
    entry_apr: float
    exit_apr: float
    side_cost: float
    slot_notional: float
    capital_factor: float
    lookback_ms: int
    max_entry_gap: float | None = None
    stop_move: float | None = None
    stop_penalty: float = 0.01
    bar_ms: int = 4 * 3_600_000


def rules_from(params: Any) -> Rules:
    """``CarryParams``-like object -> ``Rules`` (side cost as in the engine: fee_1 + fee_2 + 2 x slippage)."""
    return Rules(
        strategy=params.strategy,
        slots=params.slots,
        entry_apr=params.entry_apr,
        exit_apr=params.exit_apr,
        side_cost=params.fee_1 + params.fee_2 + 2 * params.slippage,
        slot_notional=params.slot_notional,
        capital_factor=params.capital_factor,
        lookback_ms=int(params.lookback_days * DAY_MS),
        max_entry_gap=params.max_entry_gap,
        stop_move=params.stop_move,
        stop_penalty=params.stop_penalty,
        bar_ms=params.bar_ms,
    )


def close_enough(x: Any, y: Any, rel: float = REL_TOL, abs_tol: float = ABS_TOL) -> bool:
    """Registered equality of two numbers: |x - y| <= max(abs_tol, rel x max(|x|, |y|)); None equals only None."""
    if x is None or y is None:
        return x is None and y is None
    if isinstance(x, bool) or isinstance(y, bool):
        return bool(x) == bool(y)
    return abs(float(x) - float(y)) <= max(abs_tol, rel * max(abs(float(x)), abs(float(y))))


def _js(v: Any) -> Any:
    return json.loads(v) if isinstance(v, (str, bytes)) else v


def journal_from_rows(
    cycles: Iterable[dict[str, Any]], logs: Iterable[dict[str, Any]], positions: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Rows of ``cp_cycle`` / ``cp_log`` (in id order) / ``cp_position`` of ONE strategy -> journal with parsed JSON."""
    cyc = sorted(({**c, "payload": _js(c.get("payload")) or {}} for c in cycles), key=lambda c: int(c["cycle_ts"]))
    return {
        "cycles": cyc,
        "logs": [{**r, "payload": _js(r.get("payload")) or {}} for r in logs],
        "positions": [{**p, "state": _js(p.get("state")) or {}, "trade": _js(p.get("trade"))} for p in positions],
    }


@dataclass
class LogIndex:
    funding: dict[tuple[int, str], list[dict[str, Any]]] = field(default_factory=dict)
    mark: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    close: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    side: dict[tuple[int, str, str], list[dict[str, Any]]] = field(default_factory=dict)
    gap_confirmed: set[tuple[int, str]] = field(default_factory=set)  # (cycle_ts, trade id) of funding_gap_confirmed events
    count: dict[tuple[str, int, str], int] = field(default_factory=dict)  # rows per (kind, cycle_ts, trade id)


def index_logs(logs: Iterable[dict[str, Any]]) -> LogIndex:
    ix = LogIndex()
    for r in logs:
        k, c, tid, pl = r["kind"], int(r["cycle_ts"]), r.get("trade_id"), r["payload"]
        ix.count[(k, c, tid)] = ix.count.get((k, c, tid), 0) + 1
        if k == "funding":
            ix.funding.setdefault((c, tid), []).append(pl)
        elif k == "mark":
            ix.mark[(c, tid)] = pl
        elif k == "close":
            ix.close[(c, tid)] = pl
        elif k == "side":
            ix.side.setdefault((c, tid, pl.get("event")), []).append(pl)
        elif k == "event" and pl.get("type") == "funding_gap_confirmed" and tid:
            ix.gap_confirmed.add((c, tid))
    return ix


def aside_symbols(payload: dict[str, Any]) -> set[str]:
    """Held positions the engine set aside this cycle (not marked, not settled, slot kept)."""
    return {f["symbol"] for f in payload.get("flags") or [] if f.get("issue") in ASIDE_ISSUES and f.get("symbol")}


def _flag_issues(payload: dict[str, Any], sym: str) -> set[str]:
    return {f.get("issue") for f in payload.get("flags") or [] if f.get("symbol") == sym}


def _norm(strategy: str, d: dict[str, Any]) -> dict[str, Any]:
    action = d["action"]
    kind = ("stop" if d.get("reason") == "stop" else "signal") if action == "close" else d.get("reason") if action == "hold" else "entry"
    return {
        "symbol": d["symbol"],
        "action": action,
        "kind": kind,
        "dir": d.get("dir"),
        "value": d.get("apr") if strategy == "A" else d.get("spread"),
    }


def decisions_of(strategy: str, decisions: Iterable[dict[str, Any]], aside: Iterable[str] = ()) -> dict[tuple[str, str], dict[str, Any]]:
    """Decisions keyed by (symbol, action); a position set aside is a 'hold' of kind 'aside'. One symbol can have a close
    and an open in the same cycle (B: a stop, then a re-entry), never two of one action (``duplicate_decisions``)."""
    out = {(n["symbol"], n["action"]): n for n in (_norm(strategy, d) for d in decisions)}
    for s in aside:
        out[(s, "hold")] = {"symbol": s, "action": "hold", "kind": "aside", "dir": None, "value": None}
    return out


def duplicate_decisions(decisions: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    """(symbol, action) pairs that occur more than once in a raw decision list (``decisions_of`` would fold them)."""
    seen: dict[tuple[str, str], int] = {}
    for d in decisions:
        k = (d["symbol"], d["action"])
        seen[k] = seen.get(k, 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def compare_decisions(
    engine: dict[tuple[str, str], dict[str, Any]], replay: dict[tuple[str, str], dict[str, Any]], *, strict: bool
) -> list[dict[str, Any]]:
    """One row per (symbol, action) in either set. Match: both present, same direction and close kind (stop / signal);
    ``strict`` (a.1) also needs the same hold reason and the decision's APR / spread within the registered tolerance."""
    rows = []
    for key in sorted(set(engine) | set(replay)):
        e, r = engine.get(key), replay.get(key)
        ok = e is not None and r is not None and e["dir"] == r["dir"]
        if ok and (strict or e["action"] == "close"):
            ok = e["kind"] == r["kind"]
        if ok and strict:
            ok = close_enough(e["value"], r["value"])
        rows.append({"symbol": key[0], "action": key[1], "engine": e, "replay": r, "match": ok})
    return rows


# ---------- observations ----------


def _side_price(sides: list[dict[str, Any]], strategy: str, leg: str) -> float | None:
    """Model price of one leg from the side rows of a close: A legs 'spot' / 'perp', B legs by venue ('a' / 'b')."""
    for s in sides:
        if strategy == "A" and s.get("leg") == leg:
            return s.get("model_price")
        if strategy == "B" and s.get("venue") == (PERP_VENUE if leg == "a" else BYBIT):
            return s.get("model_price")
    return None


def recorded_observations(strategy: str, payload: dict[str, Any], cycle_ts: int, tids: dict[str, str], ix: LogIndex) -> dict[str, Any]:
    """What the engine's journal says each observation was at this cycle.

    ``entry[sym]``: the entry tuple of every recorded candidate (A ``(apr, spot, perp)``, B ``(apr_a, apr_b, price_a,
    price_b)``). ``mark[sym]`` for a held symbol with a mark or close row: the mark observation dict (payments with the
    price the engine used, prices after the mark, trailing APR) and ``extremes``: the cumulative maxima the engine
    recorded (bar extremes are not in the journal, so these are taken as given). ``missing``: held symbols with neither.
    """
    cands = {c["symbol"]: c for c in payload.get("candidates") or []}
    entry: dict[str, tuple[Any, ...]] = {}
    for s, c in cands.items():
        entry[s] = (c.get("trailing_apr"), c.get("spot"), c.get("perp")) if strategy == "A" else (
            c.get("apr_a"), c.get("apr_b"), c.get("price_a"), c.get("price_b"))  # fmt: skip
    mark: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for sym, tid in tids.items():
        m, cl = ix.mark.get((cycle_ts, tid)), ix.close.get((cycle_ts, tid))
        sides = ix.side.get((cycle_ts, tid, "close"), [])
        pays = ix.funding.get((cycle_ts, tid), [])
        c = cands.get(sym) or {}

        def pay(venue: str, pays: list[dict[str, Any]] = pays) -> list[tuple[int, float, Any]]:
            return [(int(x["payment_ts"]), float(x["rate"]), x.get("mark")) for x in pays if x.get("venue") == venue]

        if strategy == "A":
            if m is not None:
                px, ext = (m.get("spot"), m.get("perp")), {"max_runup": m.get("max_runup")}
            elif cl is not None:
                px, ext = (_side_price(sides, "A", "spot"), _side_price(sides, "A", "perp")), {"max_runup": cl.get("max_runup")}
            else:
                missing.append(sym)
                continue
            mark[sym] = {
                "payments": pay(PERP_VENUE), "perp_high": None, "spot": px[0], "perp": px[1], "apr": c.get("trailing_apr"),
                "extremes": ext,
            }  # fmt: skip
        else:
            src = m if m is not None else cl
            if src is None:
                missing.append(sym)
                continue
            px = (m.get("a"), m.get("b")) if m is not None else (_side_price(sides, "B", "a"), _side_price(sides, "B", "b"))
            mark[sym] = {
                "pay_a": pay(PERP_VENUE), "pay_b": pay(BYBIT), "ext_a": None, "ext_b": None, "pa": px[0], "pb": px[1],
                "ra": c.get("apr_a"), "rb": c.get("apr_b"), "extremes": {"max_up": src.get("max_up"), "max_down": src.get("max_down")},
            }  # fmt: skip
    return {"entry": entry, "mark": mark, "missing": missing}


def _empty_mark(strategy: str) -> dict[str, Any]:
    keys = _F_MARK[strategy] + _P_MARK[strategy]
    return {k: ([] if k.startswith("pay") else None) for k in keys}


def make_observers(
    strategy: str,
    rec: dict[str, Any] | None,
    *,
    base_mark: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    base_entry: Callable[[str], tuple[Any, ...]] | None = None,
    use_funding: bool = True,
    use_prices: bool = True,
    fund_entry: Callable[[str], tuple[Any, ...] | None] | None = None,
) -> tuple[Callable[[str, dict[str, Any]], dict[str, Any]], Callable[[str], tuple[Any, ...]]]:
    """Observation callbacks for one cycle's step: the backtest's (``base_*``) with the funding and / or price parts
    replaced by the recorded ones (``rec``) where the journal has them. (a.1): no base, both parts recorded.
    ``fund_entry(sym)``: funding part of the entry tuple from the engine's stored funding history, used for symbols
    without a recorded candidate when ``use_funding``. Recorded extremes are applied to the position before the step
    marks it (``max_runup`` / ``max_up`` / ``max_down`` = max(own, recorded)), since bar extremes are not journaled."""
    rec = rec or {"entry": {}, "mark": {}}
    n_entry = 3 if strategy == "A" else 4

    def mark(sym: str, p: dict[str, Any]) -> dict[str, Any]:
        o = dict(_empty_mark(strategy))
        if base_mark is not None:
            try:
                o.update(base_mark(sym, p))
            except KeyError:  # symbol absent from the downloaded history: nothing observed
                pass
        r = rec["mark"].get(sym)
        if r is not None:
            if use_funding:
                o.update({k: r[k] for k in _F_MARK[strategy]})
            if use_prices:
                o.update({k: r[k] for k in _P_MARK[strategy]})
                for k, v in (r.get("extremes") or {}).items():
                    if v is not None:
                        p[k] = max(p[k], float(v))
        return o

    def entry(sym: str) -> tuple[Any, ...]:
        out: list[Any] = [None] * n_entry
        if base_entry is not None:
            try:
                out = list(base_entry(sym))
            except KeyError:
                pass
        r = rec["entry"].get(sym)
        if use_funding:
            src = r if r is not None else (fund_entry(sym) if fund_entry is not None else None)
            if src is not None:
                f = list(src) if len(src) == n_entry else [None] * n_entry
                if len(src) != n_entry:  # fund_entry gives the funding part only
                    for i, v in zip(_F_ENTRY[strategy], src, strict=False):
                        f[i] = v
                for i in _F_ENTRY[strategy]:
                    out[i] = f[i]
        if use_prices and r is not None:
            for i in _P_ENTRY[strategy]:
                out[i] = r[i]
        return tuple(out)

    return mark, entry


def run_step(
    rules: Rules,
    book: dict[str, dict[str, Any]],
    t: int,
    universe: Iterable[str],
    mark: Callable[[str, dict[str, Any]], dict[str, Any]],
    entry: Callable[[str], tuple[Any, ...]],
    *,
    slots: int | None = None,
) -> dict[str, Any]:
    """One step of the backtest code on ``book`` (mutated, as in the backtest)."""
    kw: dict[str, Any] = {
        "slots": rules.slots if slots is None else slots,
        "entry_apr": rules.entry_apr,
        "exit_apr": rules.exit_apr,
        "side_cost": rules.side_cost,
    }
    if rules.strategy == "A":
        return carry_step(book, t, universe, mark, entry, **kw)
    kw.update(max_entry_gap=rules.max_entry_gap, stop_move=rules.stop_move, stop_penalty=rules.stop_penalty)
    return spread_step(book, t, universe, mark, entry, **kw)


# ---------- (a.1) ----------


def funding_legs(rows: dict[tuple[str, str], list[tuple[int, float]]], bar_ms: int = 4 * 3_600_000) -> dict[tuple[str, str], _Leg]:
    """``{(venue, symbol): [(ts, rate)]}`` -> funding-only ``_Leg`` per key (APR and payment lookups of the backtest)."""
    return {k: _Leg({"funding": [{"ts_ms": ts, "rate": r} for ts, r in v], "bars": []}, bar_ms) for k, v in rows.items()}


class _Tally:
    def __init__(self) -> None:
        self.total = 0
        self.matched = 0
        self.exempt: dict[str, int] = {}

    def add(self, ok: bool) -> None:
        self.total += 1
        self.matched += ok

    def skip(self, reason: str) -> None:
        self.exempt[reason] = self.exempt.get(reason, 0) + 1

    def out(self) -> dict[str, Any]:
        return {"total": self.total, "matched": self.matched, "exempt": sum(self.exempt.values()), "exempt_by_reason": self.exempt}


def _want_sides(S: str, p: dict[str, Any], event: str) -> dict[str, tuple[float, str]]:
    """Expected (model price, side) per leg key (A: 'spot' / 'perp'; B: venue) of an open or a close."""
    opening = event == "open"
    if S == "A":
        if opening:
            return {"spot": (p["spot_e"], "buy"), "perp": (p["perp_e"], "sell")}
        return {"spot": (p["spot_last"], "sell"), "perp": (p["perp_last"], "buy")}
    long_a = p["dir"] == 1
    if opening:
        return {PERP_VENUE: (p["a_e"], "buy" if long_a else "sell"), BYBIT: (p["b_e"], "sell" if long_a else "buy")}
    return {PERP_VENUE: (p["a_last"], "sell" if long_a else "buy"), BYBIT: (p["b_last"], "buy" if long_a else "sell")}


def replay_a1(
    rules: Rules,
    journal: dict[str, Any],
    *,
    as_of_ms: int,
    funding_rows: dict[tuple[str, str], list[tuple[int, float]]] | None = None,
    keep_books: bool = False,
    entries_stop_from: dict[str, int] | None = None,
) -> dict[str, Any]:
    """(a.1): replay every recorded cycle with ``cycle_ts <= as_of_ms`` through the backtest step on recorded inputs.

    The book starts empty at the first cycle and is chained by the replay (the engine's stored state is never read
    back). Per done cycle: positions the engine set aside are set aside, ``slots`` minus their number are filled,
    a cycle with an allowed ``entries_stopped`` (``entries_stop_from``: strategy -> first cycle_ts; default the owner's
    decision) ranks nothing. Checked: ``decision_ts`` = cycle_ts + DECISION_DELAY_MS, every decision (strict, no
    duplicates), the accounting of every mark, close, open / close side and funding row, the journal's completeness
    and each ``cp_position`` row; with ``funding_rows`` (``cp_funding`` as ``{(venue, symbol): [(ts, rate)]}``) also
    each recorded candidate APR and each settled payment set against the stored history (exempt only when the cycle
    flags a funding-data problem for the symbol, or the trade has a ``funding_gap_confirmed`` event IN THAT CYCLE).

    ``status``: 'fail' on any mismatch; else 'unverifiable' when a check is empty (no done cycle, no decision, no
    accounting number, no stored funding, an empty (a.1-F) / (a.1-P)) or the ``as_of_ms`` cycle is not recorded; else
    'pass'. ``passed`` = status 'pass'. ``keep_books``: also return the book before each cycle (input of (a.2)).
    """
    S, n = rules.strategy, rules.slot_notional
    stop_from = ENTRIES_STOP_FROM if entries_stop_from is None else entries_stop_from
    ix = index_logs(journal["logs"])
    legs = funding_legs(funding_rows, rules.bar_ms) if funding_rows is not None else None
    venues = (PERP_VENUE,) if S == "A" else (PERP_VENUE, BYBIT)
    book: dict[str, dict[str, Any]] = {}
    tids: dict[str, str] = {}
    entry_px: dict[str, tuple[float, int]] = {}  # trade id -> (entry price of the funding denominator, direction)
    dec, acc, apr_chk, pay_chk = _Tally(), _Tally(), _Tally(), _Tally()
    mism: list[dict[str, Any]] = []
    per_sha: dict[str, dict[str, list[int]]] = {}
    status: dict[str, int] = {}
    by_action: dict[str, list[int]] = {}
    books: dict[int, dict[str, Any]] = {}
    trades = {"opened": 0, "closed": 0}
    replay_trades: dict[str, dict[str, Any]] = {}  # trade id -> {"open_t", "close_t"}
    aside_log: list[dict[str, Any]] = []
    unstopped: list[int] = []
    cycles = [c for c in journal["cycles"] if int(c["cycle_ts"]) <= as_of_ms]
    cyc_ts = [int(c["cycle_ts"]) for c in cycles]
    done_ts = {int(c["cycle_ts"]) for c in cycles if c["status"] == "done"}
    grid_holes = [x for x in range(cyc_ts[0], as_of_ms + 1, FUNDING_STEP_MS) if x not in set(cyc_ts)] if cyc_ts else []
    rows_by_cycle: dict[int, set[str]] = {}
    for mc, tid in list(ix.mark) + list(ix.close):
        rows_by_cycle.setdefault(mc, set()).add(tid)
    used_sides: set[tuple[int, str, str]] = set()
    # funding_payment_missing per symbol (for the annotation of an (a.1-F) / (a.1-P) mismatch, never an exemption)
    pm_cycles: dict[str, list[int]] = {}
    for c in cycles:
        for f in c["payload"].get("flags") or []:
            if f.get("issue") == "funding_payment_missing" and f.get("symbol"):
                pm_cycles.setdefault(f["symbol"], []).append(int(c["cycle_ts"]))

    def miss(c_ts: int, kind: str, sym: str | None, fld: str, e: Any, r: Any, **extra: Any) -> None:
        mism.append({"cycle_ts": c_ts, "kind": kind, "symbol": sym, "field": fld, "engine": e, "replay": r, **extra})

    def later_missing(sym: str, c_ts: int) -> bool:
        return any(x > c_ts for x in pm_cycles.get(sym, ()))

    # journal rows that belong to no done cycle, and duplicated / conflicting mark and close rows
    for (k, c_ts, tid), cnt in sorted(ix.count.items(), key=lambda x: (x[0][1], x[0][0], str(x[0][2]))):
        if c_ts > as_of_ms or k == "event":
            continue
        if c_ts not in done_ts:
            miss(c_ts, "journal", tid, f"{k}_row_outside_done_cycle", cnt, 0)
        if k in ("mark", "close") and cnt > 1:
            miss(c_ts, "journal", tid, f"duplicate_{k}_row", cnt, 1)
    for c_ts, tid in ix.mark:
        if c_ts <= as_of_ms and (c_ts, tid) in ix.close:
            miss(c_ts, "journal", tid, "mark_and_close_row", "both", "one")

    for c in cycles:
        c_ts = int(c["cycle_ts"])
        status[c["status"]] = status.get(c["status"], 0) + 1
        if c["status"] != "done":
            continue
        pl = c["payload"]
        sha = str(c.get("git_sha"))
        ps = per_sha.setdefault(sha, {"cycles": [0], "decisions": [0, 0], "accounting": [0, 0]})
        ps["cycles"][0] += 1
        t = c_ts + DECISION_DELAY_MS
        if pl.get("decision_ts") != t:  # look-ahead guard on the engine itself
            miss(c_ts, "cycle", None, "decision_ts", pl.get("decision_ts"), t)
        stopped = pl.get("entries_stopped")
        stop_ok = bool(stopped) and S in stop_from and c_ts >= stop_from[S]
        if stopped and not stop_ok:
            miss(c_ts, "cycle", None, "entries_stopped_not_allowed", stopped, None)
        elif not stopped and S in stop_from and c_ts >= stop_from[S]:
            unstopped.append(c_ts)
        for f in pl.get("flags") or []:
            if f.get("issue") in ASIDE_ISSUES and f.get("symbol"):
                aside_log.append(
                    {
                        "cycle_ts": c_ts,
                        "symbol": f["symbol"],
                        "issue": f["issue"],
                        "detail": f.get("detail"),
                        "gap_cycles": f.get("gap_cycles"),
                    }
                )
        for k in duplicate_decisions(pl.get("decisions") or []):
            miss(c_ts, "decision", k[0], f"duplicate_{k[1]}", 2, 1)
        if keep_books:
            books[c_ts] = {"book": copy.deepcopy(book), "tids": dict(tids)}
        aside = aside_symbols(pl) & set(book)
        live = {s: p for s, p in book.items() if s not in aside}
        rec = recorded_observations(S, pl, c_ts, {s: tids[s] for s in live}, ix)
        for s in rec["missing"]:
            miss(c_ts, "journal", s, "mark_or_close_row", None, "expected")
        mark, entry = make_observers(S, rec)
        flagged = {s: sorted(_flag_issues(pl, s) & FUNDING_DATA_ISSUES) for s in set(book) | set(rec["entry"])}
        flagged = {s: v for s, v in flagged.items() if v}

        def checked_mark(
            sym: str, p: dict[str, Any], _m: Any = mark, _c: int = c_ts, _t: int = t, _fl: dict[str, list[str]] = flagged
        ) -> dict[str, Any]:
            o = _m(sym, p)
            if legs is not None:
                for v, key in zip(venues, ("payments",) if S == "A" else ("pay_a", "pay_b"), strict=True):
                    leg = legs.get((v, sym))
                    want = leg.payments(int(p["last"]), _t) if leg else []
                    got = [(ts, r) for ts, r, _ in o[key]]
                    ok = [x[0] for x in want] == [x[0] for x in got] and all(
                        close_enough(a[1], b[1]) for a, b in zip(want, got, strict=True)
                    )
                    if not ok and sym in _fl:
                        pay_chk.skip("flag:" + ",".join(_fl[sym]))
                        continue
                    if not ok and (_c, tids.get(sym)) in ix.gap_confirmed:  # only the cycle the event was written in
                        pay_chk.skip("funding_gap_confirmed")
                        continue
                    pay_chk.add(ok)
                    if not ok:
                        miss(
                            _c, "payments_vs_cp_funding", sym, v, [x[0] for x in got], [x[0] for x in want],
                            later_payment_missing=later_missing(sym, _c),
                        )  # fmt: skip
            return o

        universe = [] if stop_ok else sorted(rec["entry"])
        live_tids = {tids[s] for s in live}
        res = run_step(rules, live, t, universe, checked_mark, entry, slots=rules.slots - len(aside))
        replay = decisions_of(S, res["decisions"], aside)
        engine = decisions_of(S, pl.get("decisions") or [], aside_symbols(pl))
        for row in compare_decisions(engine, replay, strict=True):
            dec.add(row["match"])
            ps["decisions"][0] += 1
            ps["decisions"][1] += row["match"]
            ba = by_action.setdefault(row["action"], [0, 0])
            ba[0] += 1
            ba[1] += row["match"]
            if not row["match"]:
                miss(c_ts, "decision", row["symbol"], row["action"], row["engine"], row["replay"])

        def chk(
            sym: str | None, kind: str, fld: str, e: Any, r: Any, exact: bool = False, abs_tol: float = ABS_TOL,
            _c: int = c_ts, _ps: Any = ps,
        ) -> None:  # fmt: skip
            ok = (e == r) if exact else close_enough(e, r, abs_tol=abs_tol)
            acc.add(ok)
            _ps["accounting"][0] += 1
            _ps["accounting"][1] += ok
            if not ok:
                miss(_c, kind, sym, fld, e, r)

        def chk_sides(sym: str, tid: str, p: dict[str, Any], event: str, _c: int = c_ts) -> None:
            sides = ix.side.get((_c, tid, event), [])
            used_sides.add((_c, tid, event))
            want = _want_sides(S, p, event)
            if len(sides) != 2:
                miss(_c, event, sym, "side_rows", len(sides), 2)
                acc.add(False)
            for s in sides:
                w = want.get(s.get("leg") if S == "A" else s.get("venue"))
                if w is None:
                    miss(_c, event, sym, "side_leg", s.get("leg"), None)
                    acc.add(False)
                    continue
                chk(sym, event, "model_price", s.get("model_price"), w[0], abs_tol=0.0)
                chk(sym, event, "side", s.get("side"), w[1], exact=True)
                if event == "open":
                    chk(sym, event, "qty", s.get("qty"), n / w[0], abs_tol=0.0)

        # funding rows: signed USD of each payment the engine journaled this cycle
        for (fc, tid), rows in ix.funding.items():
            if fc != c_ts:
                continue
            if tid not in entry_px:
                miss(c_ts, "funding_row", tid, "unknown_trade", tid, None)
                acc.add(False)
                continue
            e_px, d = entry_px[tid]
            for x in rows:
                sign = 1 if S == "A" else (-d if x.get("venue") == PERP_VENUE else d)
                chk(tid, "funding_row", "usd", x.get("usd"), sign * float(x["rate"]) * float(x["mark"]) / e_px * n)
        closed_syms = set()
        for tr in res["closed"]:
            sym = tr["symbol"]
            closed_syms.add(sym)
            p_closed = book[sym]
            tid = tids.pop(sym)
            trades["closed"] += 1
            replay_trades[tid]["close_t"] = t
            chk_sides(sym, tid, p_closed, "close")
            e = ix.close.get((c_ts, tid))
            if e is None:
                miss(c_ts, "close", sym, "close_row", None, "expected")
                acc.add(False)
                continue
            replay_trades[tid]["close_row"] = e
            second = "basis" if S == "A" else "price_pnl"
            nums = ["funding", second, "fees", "pnl"] + (
                ["entry_apr", "max_runup", "max_premium_rise"] if S == "A" else ["entry_spread", "max_up", "max_down", "max_gap"]
            )
            for k in nums:
                chk(sym, "close", k, e.get(k), tr[k])
            for k in ("open_ts", "close_ts") + (() if S == "A" else ("dir", "stopped")):
                chk(sym, "close", k, e.get(k), tr[k], exact=True)
            chk(sym, "close", "exit_reason", e.get("exit_reason"), "stop" if tr.get("stopped") else "signal", exact=True)
            for k, v in (("funding_usd", tr["funding"]), ("price_usd", tr[second]), ("costs_usd", -tr["fees"]), ("pnl_usd", tr["pnl"])):
                chk(sym, "close", k, e.get(k), v * n)
            chk(sym, "close", "return_on_capital", e.get("return_on_capital"), tr["pnl"] / rules.capital_factor)
        for sym in res["opened"]:
            tid = f"{S}:{sym}:{c_ts}"
            tids[sym] = tid
            trades["opened"] += 1
            replay_trades[tid] = {"open_t": t, "close_t": None}
            p = live[sym]
            book[sym] = p
            entry_px[tid] = (p["spot_e"], 1) if S == "A" else (p["a_e"], p["dir"])
            chk_sides(sym, tid, p, "open")
        for sym in list(book):
            if sym in closed_syms and sym not in res["opened"]:
                del book[sym]
        for sym, p in live.items():
            if sym in res["opened"] or sym in closed_syms:
                continue
            e = ix.mark.get((c_ts, tids[sym]))
            if e is None:
                continue  # counted in rec["missing"]
            if S == "A":
                vals = {
                    "funding_usd": p["funding"] * n, "basis_usd": carry_basis(p) * n, "costs_usd": -p["fees"] * n,
                    "pnl_usd": carry_pnl_now(p) * n, "max_premium_rise": p["max_premium_rise"], "max_runup": p["max_runup"],
                }  # fmt: skip
            else:
                vals = {
                    "funding_usd": p["funding"] * n, "price_usd": spread_price_pnl(p) * n, "costs_usd": -p["fees"] * n,
                    "pnl_usd": spread_pnl_now(p) * n, "max_gap": p["max_gap"], "max_up": p["max_up"], "max_down": p["max_down"],
                }  # fmt: skip
            for k, v in vals.items():
                chk(sym, "mark", k, e.get(k), v)
        # mark / close rows of trades the replay did not mark this cycle (unknown, set aside, or already closed)
        for tid in rows_by_cycle.get(c_ts, ()):
            if tid not in live_tids:
                miss(c_ts, "journal", tid, "row_of_unknown_trade", tid, None)
                acc.add(False)
        if legs is not None:
            for sym, obs in rec["entry"].items():
                want: list[Any] = []
                for v in venues:
                    leg = legs.get((v, sym))
                    want.append(leg.apr(t, rules.lookback_ms) if leg else None)
                got = list(obs[: len(venues)])
                ok = all(close_enough(a, b) for a, b in zip(got, want, strict=True))
                if not ok and sym in flagged:
                    apr_chk.skip("flag:" + ",".join(flagged[sym]))
                    continue
                apr_chk.add(ok)
                if not ok:
                    miss(c_ts, "apr_vs_cp_funding", sym, "apr", got, want, later_payment_missing=later_missing(sym, c_ts))
    # side rows no replayed open / close accounts for (an orphan, or an open / close the replay did not make)
    for key in sorted(ix.side, key=lambda k: (k[0], str(k[1]), str(k[2]))):
        if key[0] <= as_of_ms and key not in used_sides:
            miss(key[0], "journal", key[1], f"orphan_{key[2]}_side_rows", len(ix.side[key]), 0)
            acc.add(False)
    # cp_position: one row per replayed trade, status at as_of, the closed trade's record = its close row
    t_last = as_of_ms + DECISION_DELAY_MS
    pos_by = {p["trade_id"]: p for p in journal.get("positions") or []}
    for tid, info in replay_trades.items():
        p = pos_by.get(tid)
        if p is None:
            miss(info["open_t"], "position", tid, "cp_position_row", None, "expected")
            acc.add(False)
            continue
        ok = int(p.get("opened_ts") or -1) == info["open_t"]
        if info["close_t"] is None:
            ok = ok and (p.get("status") == "open" or (p.get("closed_ts") is not None and int(p["closed_ts"]) > t_last))
        else:
            ok = ok and p.get("status") == "closed" and int(p.get("closed_ts") or -1) == info["close_t"]
            ok = ok and p.get("trade") == info.get("close_row")
        acc.add(ok)
        if not ok:
            miss(info["open_t"], "position", tid, "cp_position", {k: p.get(k) for k in ("status", "opened_ts", "closed_ts")}, info)
    for tid, p in pos_by.items():
        if int(p.get("opened_ts") or 0) <= t_last and tid not in replay_trades:
            miss(int(p.get("opened_ts") or 0), "position", tid, "cp_position_without_replayed_trade", p.get("status"), None)
            acc.add(False)
    unverifiable = []
    if not status.get("done"):
        unverifiable.append("no_done_cycle")
    if dec.total == 0:
        unverifiable.append("no_decision")
    if acc.total == 0:
        unverifiable.append("no_accounting_number")
    if legs is None:
        unverifiable.append("no_cp_funding")
    else:
        if apr_chk.total == 0:
            unverifiable.append("apr_vs_cp_funding_empty")
        if pay_chk.total == 0:
            unverifiable.append("payments_vs_cp_funding_empty")
    if as_of_ms not in set(cyc_ts):
        unverifiable.append("as_of_cycle_not_recorded")
    if as_of_ms % FUNDING_STEP_MS:
        unverifiable.append("as_of_not_on_the_8h_grid")
    st = "fail" if mism else ("unverifiable" if unverifiable else "pass")
    out = {
        "strategy": S,
        "as_of_ms": as_of_ms,
        "cycles": status,
        "grid_holes": grid_holes,
        "decisions": {**dec.out(), "by_action": by_action},
        "accounting": acc.out(),
        "apr_vs_cp_funding": apr_chk.out() if legs is not None else None,
        "payments_vs_cp_funding": pay_chk.out() if legs is not None else None,
        "per_git_sha": per_sha,
        "trades": {**trades, "open_at_as_of": len(book)},
        "aside": aside_log,
        "cycles_after_stop_without_flag": unstopped,
        "mismatches": mism,
        "unverifiable": unverifiable,
        "status": st,
        "passed": st == "pass",
    }
    if keep_books:
        out["books"] = books
    return out


# ---------- (a.2) ----------


@dataclass
class A2Data:
    """Inputs of (a.2). ``markets``: downloaded backtest objects with funding AND bars (A ``{sym: _Market}``, B ``{sym:
    (_Leg, _Leg)}``); ``funding``: downloaded funding per ``(venue, symbol)`` (also symbols whose bars failed);
    ``engine``: the engine's stored ``cp_funding`` per ``(venue, symbol)``; ``listed``: symbols listed now on every leg
    venue (None: unknown, so a symbol missing from the download is 'not_checked', never 'delisted')."""

    markets: dict[str, Any]
    funding: dict[tuple[str, str], _Leg]
    engine: dict[tuple[str, str], _Leg]
    listed: set[str] | None = None


def a2_data(
    markets: dict[str, Any],
    funding_rows: dict[tuple[str, str], list[tuple[int, float]]],
    engine_rows: dict[tuple[str, str], list[tuple[int, float]]],
    *,
    listed: set[str] | None = None,
    bar_ms: int = 4 * 3_600_000,
) -> A2Data:
    return A2Data(markets, funding_legs(funding_rows, bar_ms), funding_legs(engine_rows, bar_ms), listed)


def _venues(strategy: str) -> tuple[str, ...]:
    return (PERP_VENUE,) if strategy == "A" else (PERP_VENUE, BYBIT)


def payment_diffs(dl: _Leg | None, eng: _Leg | None, t: int, lookback_ms: int) -> dict[str, Any]:
    """Payments in (t - lookback, t]: downloaded but not stored by the engine, stored but not downloaded, and common
    ones whose rate differs by more than 1e-6 relative (``(ts, engine, downloaded)``)."""
    if dl is None:
        return {"downloaded": False}
    d = dict(dl.payments(t - lookback_ms, t))
    e = dict(eng.payments(t - lookback_ms, t)) if eng is not None else {}
    return {
        "downloaded": True,
        "missing_in_engine": sorted(set(d) - set(e)),
        "extra_in_engine": sorted(set(e) - set(d)),
        "rate_diff": [(ts, e[ts], d[ts]) for ts in sorted(set(d) & set(e)) if not close_enough(e[ts], d[ts], abs_tol=0.0)],
    }


def funding_facts(strategy: str, data: A2Data, sym: str, t: int, lookback_ms: int) -> dict[str, Any]:
    """Concrete funding differences of ``sym`` at ``t`` (all legs) and whether they may explain a divergence:
    1 .. FUNDING_DIFF_MAX differing payments, not systematic (>= 2 rate differences in one constant ratio)."""
    legs = {v: payment_diffs(data.funding.get((v, sym)), data.engine.get((v, sym)), t, lookback_ms) for v in _venues(strategy)}
    got = all(x["downloaded"] for x in legs.values())
    n_diff = sum(len(x[k]) for x in legs.values() if x["downloaded"] for k in ("missing_in_engine", "extra_in_engine", "rate_diff"))
    ratios = [e / d for x in legs.values() if x["downloaded"] for _, e, d in x["rate_diff"] if d]
    systematic = len(ratios) >= 2 and all(close_enough(r, ratios[0], abs_tol=0.0) for r in ratios)
    return {
        "legs": legs,
        "n_diff": n_diff,
        "systematic": systematic,
        "explains": got and 1 <= n_diff <= FUNDING_DIFF_MAX and not systematic,
    }


def _closes(strategy: str, obj: Any, leg: str) -> tuple[list[int], list[float], int]:
    if strategy == "A":
        return (obj.s_close_t, obj.s_close, obj.bar_ms) if leg == "spot" else (obj.p_close_t, obj.p_close, obj.bar_ms)
    x = obj[0] if leg == "a" else obj[1]
    return x.t, x.close, x.bar_ms


def price_facts(strategy: str, obj: Any, recorded: Iterable[tuple[str, Any]], t: int) -> dict[str, Any]:
    """Recorded prices (``(leg, price)``; A legs 'spot' / 'perp', B 'a' / 'b') against the close of the downloaded bar
    the backtest reads at ``t`` (relative 1e-6). A differing price equal to one of the previous ``STALE_BARS`` downloaded
    closes is ``stale`` (the engine missed the latest bar); no downloaded bar at t is ``download_gap``."""
    out: dict[str, Any] = {"same": 0, "diff": [], "stale": [], "download_gap": []}
    for leg, x in recorded:
        if x is None:
            continue
        times, closes, bar_ms = _closes(strategy, obj, leg)
        i = bisect.bisect_right(times, t + 60_000) - 1
        cur = closes[i] if i >= 0 and t - times[i] <= bar_ms else None
        if cur is None:
            out["download_gap"].append(leg)
        elif close_enough(x, cur, abs_tol=0.0):
            out["same"] += 1
        elif any(close_enough(x, c, abs_tol=0.0) for c in closes[max(0, i - STALE_BARS) : i]):
            out["stale"].append((leg, x, cur))
        else:
            out["diff"].append((leg, x, cur))
    out["explains"] = bool(out["diff"]) and not out["stale"] and not out["download_gap"]
    return out


def extreme_facts(strategy: str, obj: Any, p: dict[str, Any], recorded: dict[str, Any] | None, t: int) -> dict[str, Any]:
    """The downloaded cumulative extreme since the entry (bars closing in (open_ts, t]) against the one the engine
    recorded at this cycle. ``missed``: downloaded higher (the engine missed a bar: never a data revision);
    ``above``: recorded higher."""
    out: dict[str, Any] = {"missed": [], "above": [], "downloaded": {}}
    if not recorded:
        return out
    o = int(p["open_ts"])
    if strategy == "A":
        hi = obj.perp_high(o, t)
        dl = {"max_runup": max(0.0, hi / p["perp_e"] - 1.0) if hi is not None else 0.0}
    else:
        ups, downs = [0.0], [0.0]
        for leg, e_key in ((obj[0], "a_e"), (obj[1], "b_e")):
            ex = leg.extremes(o, t)
            if ex:
                ups.append(ex[0] / p[e_key] - 1.0)
                downs.append(1.0 - ex[1] / p[e_key])
        dl = {"max_up": max(ups), "max_down": max(downs)}
    out["downloaded"] = dl
    for k, v in dl.items():
        r = recorded.get(k)
        if r is None or close_enough(v, r):
            continue
        (out["missed"] if v > float(r) else out["above"]).append((k, r, v))
    return out


def classify(row: dict[str, Any], payload: dict[str, Any], facts: dict[str, Any]) -> str:
    """The registered class of one (a.2) divergence, first rule that applies (order of ``CLASSES``).

    ``facts``: ``in_engine_universe`` (every leg has a stored payment at or before t), ``new_listing`` (on each leg the
    engine lacks, the download has no payment at or before t - lookback; None when that funding was not downloaded),
    ``downloaded`` (funding and bars of the symbol downloaded), ``listed`` (listed now on every leg venue; None
    unknown), ``resolved_by`` ({'funding' | 'prices' | 'both': bool}: the divergence disappears with the engine's
    funding / prices / both), ``funding`` / ``prices`` / ``extremes`` (``funding_facts`` / ``price_facts`` /
    ``extreme_facts``). A divergence is never explained by a re-run alone: a data class also needs the concrete input
    difference, a missed extreme or a stale bar is always 'unexplained', our own failed download is 'not_checked'."""
    sym = row["symbol"]
    if sym in aside_symbols(payload):
        return "data_hold"
    issues = _flag_issues(payload, sym)
    # a provider failure explains a divergence only when it names the symbol (e.g. 'ohlcv:bybit:X/USDT:USDT'); a
    # venue-wide one ('markets:...', 'funding_intervals:...') does not explain every decision of the cycle
    named = any(f.get("issue") == "provider_missing" and sym in str(f.get("field") or "") for f in payload.get("flags") or [])
    if issues & ENGINE_DATA_ISSUES or named:
        return "engine_data_unavailable"
    if not facts.get("in_engine_universe", True):
        nl = facts.get("new_listing")
        return "not_checked" if nl is None else "not_in_engine_universe" if nl else "unexplained"
    if not facts.get("downloaded", True):
        return "not_in_downloaded_history" if facts.get("listed") is False else "not_checked"
    ex, pr = facts.get("extremes") or {}, facts.get("prices") or {}
    if ex.get("missed") or pr.get("stale"):
        return "unexplained"
    f_ok = bool((facts.get("funding") or {}).get("explains"))
    p_ok = bool(pr.get("explains")) and not ex.get("above")
    rb = facts.get("resolved_by") or {}
    if rb.get("funding") and f_ok:
        return "funding_data"
    if rb.get("prices") and p_ok:
        return "price_data"
    if rb.get("both") and f_ok and p_ok:
        return "funding_and_price_data"
    return "unexplained"


def excluded_symbols(payload: dict[str, Any]) -> set[str]:
    return {x["symbol"] for x in payload.get("excluded") or [] if x.get("symbol")}


def excluded_truncated(payload: dict[str, Any]) -> bool:
    """The engine wrote only the first 500 exclusions: (a.2) cannot remove the rest, so the cycle is not checked."""
    return sum((payload.get("excluded_counts") or {}).values()) > len(payload.get("excluded") or [])


def _engine_apr(data: A2Data, strategy: str, sym: str, t: int, lookback_ms: int) -> tuple[Any, ...]:
    return tuple(data.engine[(v, sym)].apr(t, lookback_ms) if (v, sym) in data.engine else None for v in _venues(strategy))


def _new_listing(data: A2Data, strategy: str, sym: str, t: int, lookback_ms: int) -> tuple[bool, bool | None]:
    """(in the engine's universe at t, new listing on the legs it lacks; None when such a leg was not downloaded)."""
    lacking = [v for v in _venues(strategy) if not ((e := data.engine.get((v, sym))) and e.f_ts and e.f_ts[0] <= t)]
    if not lacking:
        return True, False
    new = True
    for v in lacking:
        d = data.funding.get((v, sym))
        if d is None:
            return False, None
        new = new and not (d.f_ts and d.f_ts[0] <= t - lookback_ms)
    return False, new


def replay_a2_cycle(rules: Rules, cycle: dict[str, Any], before: dict[str, Any], ix: LogIndex, data: A2Data) -> list[dict[str, Any]]:
    """(a.2) for one done cycle: the backtest step from the engine's book before the cycle (``before`` from
    ``replay_a1(keep_books=True)``) on downloaded data. Positions the engine set aside this cycle keep their slot and are
    neither marked nor closed in that step (as in (a.1)); what the backtest alone would have done with each of them is
    run separately (a difference there is 'data_hold'). The engine's recorded exclusions are removed from the universe;
    no ranking when the engine's entries were stopped. Rows as in ``compare_decisions`` (not strict) plus ``class`` and
    ``facts`` (the input differences behind the class) for each divergence."""
    S, c_ts, pl, lb = rules.strategy, int(cycle["cycle_ts"]), cycle["payload"], rules.lookback_ms
    t = c_ts + DECISION_DELAY_MS
    obs = carry_observers(data.markets, lb) if S == "A" else spread_observers(data.markets, lb)
    base_mark, base_entry = partial(obs[0], at=t), partial(obs[1], at=t)

    def fund_entry(sym: str) -> tuple[Any, ...]:
        return _engine_apr(data, S, sym, t, lb)

    book = before["book"]
    rec = recorded_observations(S, pl, c_ts, {s: before["tids"][s] for s in book if s in before["tids"]}, ix)
    aside = aside_symbols(pl) & set(book)
    excl = excluded_symbols(pl)
    universe = [] if pl.get("entries_stopped") else sorted((set(data.markets) | set(rec["entry"])) - excl - aside)

    n_entry = 3 if S == "A" else 4

    def run(use_f: bool, use_p: bool, roots: frozenset[str] = frozenset()) -> dict[tuple[str, str], dict[str, Any]]:
        mark, entry = make_observers(
            S, rec if (use_f or use_p) else None, base_mark=base_mark, base_entry=base_entry, use_funding=use_f, use_prices=use_p,
            fund_entry=fund_entry if use_f else None,
        )  # fmt: skip
        if roots:  # only the root symbols see what the engine recorded (nothing recorded: not rankable, as for the engine)
            mark_r, _ = make_observers(S, rec, base_mark=base_mark, base_entry=base_entry)
            mark_d, entry_d = mark, entry

            def mark(sym: str, p: dict[str, Any]) -> dict[str, Any]:
                return (mark_r if sym in roots else mark_d)(sym, p)

            def entry(sym: str) -> tuple[Any, ...]:
                if sym in roots:
                    return tuple(rec["entry"].get(sym) or (None,) * n_entry)
                return entry_d(sym)

        live = {s: copy.deepcopy(p) for s, p in book.items() if s not in aside}
        res = run_step(rules, live, t, universe, mark, entry, slots=rules.slots - len(aside))
        held = {s: copy.deepcopy(p) for s, p in book.items() if s in aside}
        alone = run_step(rules, held, t, [], mark, entry, slots=0) if held else {"decisions": []}
        return decisions_of(S, res["decisions"] + alone["decisions"])

    engine = decisions_of(S, pl.get("decisions") or [], aside_symbols(pl))
    rows = compare_decisions(engine, run(False, False), strict=False)
    if all(r["match"] for r in rows):
        return [{**r, "class": None} for r in rows]
    variants = {k: {(r["symbol"], r["action"]): r["match"] for r in compare_decisions(engine, run(f, p), strict=False)}
                for k, f, p in (("funding", True, False), ("prices", False, True), ("both", True, True))}  # fmt: skip
    out = []
    for r in rows:
        if r["match"]:
            out.append({**r, "class": None})
            continue
        sym, key = r["symbol"], (r["symbol"], r["action"])
        in_uni, new = _new_listing(data, S, sym, t, lb)
        obj = data.markets.get(sym)
        recp: list[tuple[str, Any]] = []
        e = rec["entry"].get(sym)
        m = rec["mark"].get(sym)
        legs = ("spot", "perp") if S == "A" else ("a", "b")
        if e is not None:
            recp += list(zip(legs, e[1:3] if S == "A" else e[2:4], strict=True))
        if m is not None:
            recp += list(zip(legs, (m["spot"], m["perp"]) if S == "A" else (m["pa"], m["pb"]), strict=True))
        facts = {
            "in_engine_universe": in_uni,
            "new_listing": new,
            "downloaded": obj is not None,
            "listed": None if data.listed is None else sym in data.listed,
            # gone from the variant's comparison (neither side has it) or matched there: the divergence disappears
            "resolved_by": {k: v.get(key, True) for k, v in variants.items()},
            "funding": funding_facts(S, data, sym, t, lb),
            "prices": price_facts(S, obj, recp, t) if obj is not None else None,
            "extremes": extreme_facts(S, obj, book[sym], (m or {}).get("extremes"), t) if obj is not None and sym in book else None,
        }
        out.append({**r, "class": classify(r, pl, facts), "facts": facts})
    # Slot competition: an 'unexplained' OPEN of a symbol with no own input difference (in the engine's universe,
    # downloaded, no differing payment, no differing / stale / missing price, no extreme difference) took or lost the
    # slot of a symbol whose divergence this cycle has another class (a root). Mechanical test: the step re-run with
    # ONLY the roots on the engine's recorded observations (download for every other symbol) removes it. It then gets
    # the root's class (the first in CLASSES order when roots differ) and ``facts["slot_competition"]`` lists the roots.
    roots = frozenset(r["symbol"] for r in out if r["class"] not in (None, "unexplained"))
    cands = [r for r in out if r["class"] == "unexplained" and r["action"] == "open" and _no_own_difference(r["facts"])]
    if roots and cands:
        again = {(x["symbol"], x["action"]): x["match"] for x in compare_decisions(engine, run(False, False, roots), strict=False)}
        root_cls = min((r["class"] for r in out if r["symbol"] in roots and r["class"]), key=CLASSES.index)
        for r in cands:
            if again.get((r["symbol"], r["action"]), True):
                r["class"], r["facts"]["slot_competition"] = root_cls, sorted(roots)
    return out


def _no_own_difference(f: dict[str, Any]) -> bool:
    pf, ef = f["prices"] or {}, f["extremes"] or {}
    return (
        f["in_engine_universe"]
        and f["downloaded"]
        and not f["funding"]["n_diff"]
        and not (pf.get("diff") or pf.get("stale") or pf.get("download_gap"))
        and not (ef.get("missed") or ef.get("above"))
    )


def funding_integrity(
    engine_rows: dict[tuple[str, str], list[tuple[int, float]]], dl_rows: dict[tuple[str, str], list[tuple[int, float]]], top: int = 10
) -> dict[str, Any]:
    """Descriptive (a.2) table: the engine's stored ``cp_funding`` against the download, per (venue, symbol) inside the
    overlap of both spans: payments missing in the engine, extra in the engine, rates differing > 1e-6 relative."""
    tot = {"compared": 0, "missing_in_engine": 0, "extra_in_engine": 0, "rate_diff": 0, "symbols_with_difference": 0}
    worst: list[tuple[int, str, str, int, int, int]] = []
    for key in sorted(set(engine_rows) & set(dl_rows)):
        e, d = dict(engine_rows[key]), dict(dl_rows[key])
        if not e or not d:
            continue
        lo, hi = max(min(e), min(d)), min(max(e), max(d))
        e = {k: v for k, v in e.items() if lo <= k <= hi}
        d = {k: v for k, v in d.items() if lo <= k <= hi}
        m, x = len(set(d) - set(e)), len(set(e) - set(d))
        r = sum(not close_enough(e[k], d[k], abs_tol=0.0) for k in set(d) & set(e))
        tot["compared"] += 1
        tot["missing_in_engine"] += m
        tot["extra_in_engine"] += x
        tot["rate_diff"] += r
        if m or x or r:
            tot["symbols_with_difference"] += 1
            worst.append((m + x + r, key[0], key[1], m, x, r))
    tot["only_in_engine"] = len(set(engine_rows) - set(dl_rows))
    tot["only_in_download"] = len(set(dl_rows) - set(engine_rows))
    tot["worst"] = [list(w[1:]) for w in sorted(worst, reverse=True)[:top]]
    return tot


def summarize_a2(rows: list[dict[str, Any]], skipped_cycles: int) -> dict[str, Any]:
    """Registered (a.2) numbers: match rate over all decisions, over open/close only, divergences by class. A
    'not_checked' divergence (our download failed) makes (a.2) incomplete (◯), an 'unexplained' one fails it."""
    oc = [r for r in rows if r["action"] in ("open", "close")]
    by_class: dict[str, int] = {}
    for r in rows:
        if not r["match"]:
            by_class[r["class"]] = by_class.get(r["class"], 0) + 1
    total, matched = len(rows), sum(r["match"] for r in rows)
    rate = matched / total if total else None
    out = {
        "decisions": total,
        "matched": matched,
        "rate": rate,
        "open_close": len(oc),
        "open_close_matched": sum(r["match"] for r in oc),
        "open_close_rate": (sum(r["match"] for r in oc) / len(oc)) if oc else None,
        "by_action": {
            a: [sum(r["action"] == a for r in rows), sum(r["action"] == a and r["match"] for r in rows)] for a in ("open", "close", "hold")
        },
        "divergences_by_class": by_class,
        "skipped_cycles": skipped_cycles,
        "passed": rate is not None and rate >= 0.90 and by_class.get("unexplained", 0) == 0,
    }
    if by_class.get("not_checked"):
        out["incomplete"] = True
        out.setdefault("incomplete_reasons", []).append(f"{by_class['not_checked']} divergences on symbols our download lacks")
    return out


def verdict(a1: dict[str, Any] | None, a2: dict[str, Any] | None) -> str:
    """Registered mapping for one book: '✓' / '≈' / '✗' / '◯' (see the card's registration of check (a)).

    '✗' as soon as a bug is seen: a (a.1) mismatch or an 'unexplained' (a.2) divergence, also when another part is
    incomplete. '◯': (a.1) unverifiable (an empty check, the as-of cycle not recorded), (a.2) not run or incomplete,
    or no open / close decision to rate. Only '✓' means (a) passed; '≈' is at most "agreed in part"."""
    if a1 is None:
        return "◯"
    st = a1.get("status", "pass" if a1.get("passed") else "fail")
    if st == "fail":
        return "✗"
    if a2 is not None and (a2.get("divergences_by_class") or {}).get("unexplained", 0):
        return "✗"
    if st != "pass" or a2 is None or a2.get("rate") is None or a2.get("incomplete"):
        return "◯"
    if not a2["passed"]:
        return "✗"
    oc = a2.get("open_close_rate")
    if oc is None:
        return "◯"
    return "✓" if oc >= 0.90 else "≈"
