"""Trade plans of the assistant: log every plan (enter / wait / skip) and score it later on candles.

Local bookkeeping only: nothing is sent to any venue. A plan is written by `plan_add` when the assistant answers a
"trade plan" request (prompt `pre_trade_checklist`, step 10), whatever its verdict, and is resolved on OHLCV candles by
`plan_review` or the hourly collector job `plan_resolve` (same code: `review_open`). Conventions of the scoring are in
`analytics/trade_plan.py`.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.demo_option_pairs import paired_report
from ..analytics.trade_plan import TAKER_FEE, geometry, resolve_plan, summarize_plans, usable_expires, validate_levels
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import (
    READ_ONLY,
    STORE_ERRORS,
    drop_corrupt_rows,
    finite_float,
    finite_json,
    first_nonfinite,
    r,
    reject_bad_id,
    store_failed,
    store_num,
    store_ready,
)

PLAN_WARNING = (
    "Paper scoring of the assistant's plans on candle highs / lows (local store only, no orders): a forward record of the "
    "assistant's judgment, not a proven edge and not a fill guarantee. R is net of a flat taker fee of 0.05 % per side "
    "on the price of the underlying; no other slippage than the gap rules, no funding. A candle that touches both stop and "
    "target counts as the stop. 'wait' and 'skip' plans are scored counterfactually (would the plan have worked?). Few "
    "plans: the statistics are descriptive, not evidence."
)
TIMEFRAMES = (("1h", 3_600_000), ("4h", 14_400_000), ("1d", 86_400_000))  # tried in this order per market
PAGE = 1000
MAX_PAGES = 12
MIN_HORIZON_S = 2 * 3600  # guarantees one complete 1h candle inside the window
MAX_HORIZON_S = 180 * 86400
RULE_KEYS = {str(i) for i in range(1, 9)}  # the owner's 8 rules (docs/knowledge/trading-strategy.md)
STALE_S = 86400
SQLITE_INT_MAX = 2**63 - 1

SHADOW_TAG = "src:demo-shadow"  # execution/demo_trader.py: one plan per setup signal, not the assistant's judgment
# Numeric columns `_view` reads (expires_ts has its own guard, `_expires_view`).
PLAN_NUMS = ("created_ts", "entry", "stop", "target", "filled_ts", "fill", "resolved_ts", "exit", "r_result")
MAX_LEVEL_RATIO = 1000.0  # a plan level this many times away from the market's price is not a plan (review_open skips it)
# |R| beyond this is no result of real prices; several r_result ~1e308 overflowed the summary's mean (item 299).
MAX_ABS_R = 1e9


def _iso(ts: int | None) -> str | None:
    """ISO-8601 UTC of unix seconds; the raw number as a string when it is out of the datetime range (e.g. ms passed)."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return str(ts)


def _expires_view(ts: Any) -> dict[str, Any]:
    """``expires`` of a stored plan as ISO-8601 UTC. A stored value that is not usable unix seconds (text, a number in
    ms or out of the datetime range, NaN) gives ``expires=None`` plus ``expires_raw``, the raw value as text (cut to
    64 chars). Never raises; plan_add's validation keeps such values out, this only guards rows written elsewhere."""
    if ts is None:
        return {"expires": None}
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        try:
            return {"expires": datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")}
        except (ValueError, OverflowError, OSError):
            pass
    return {"expires": None, "expires_raw": str(ts)[:64]}


def _view(p: dict[str, Any]) -> dict[str, Any]:
    g = geometry(float(p["entry"]), float(p["stop"]), float(p["target"]))
    out = {
        "id": p["id"],
        "created": _iso(p["created_ts"]),
        "exchange": p["exchange"],
        "symbol": p["symbol"],
        "instrument": p["instrument"],
        "side": p["side"],
        "entry_type": p["entry_type"],
        "entry": p["entry"],
        "stop": p["stop"],
        "target": p["target"],
        "rr": r(g["rr"], 2),
        "risk_pct": r(g["risk_pct"], 5),
        **_expires_view(p["expires_ts"]),
        "verdict": p["verdict"],
        "thesis": p["thesis"],
        "rules": p["rules"],
        "tags": p["tags"],
        "rec_paper_id": p["rec_paper_id"],
        "journal_id": p["journal_id"],
        "status": p["status"],
        "filled": _iso(p["filled_ts"]),
        "fill": p["fill"],
    }
    if p["status"] == "resolved":
        out.update(
            outcome=p["outcome"],
            resolved=_iso(p["resolved_ts"]),
            exit=p["exit"],
            r_result=r(p["r_result"], 3),
            timeframe=p["timeframe"],
            note=p["resolve_note"],
        )
    return out


def _listable(b: Builder, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`_view` of the stored plans that another writer did not corrupt (see `_sound`)."""
    return [v for _, v in _sound(b, rows)]


def _sound(b: Builder, rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(row, `_view`) of the stored plans that another writer did not corrupt. A string / blob / inf in a level, a
    timestamp or the result (TypeError / ValueError in `_view`, or an invalid UTF-8 blob failing the reply), entry 0 or
    entry = stop (ZeroDivisionError in `geometry`), a reward:risk that overflows, an |r_result| over MAX_ABS_R (several
    ~1e308 raised OverflowError in the summary's mean, item 299), or NaN / Infinity in the JSON of `tags` / `rules`: the
    plan is skipped with a warning naming its id (invariant 1). plan_add validates its levels, so its own rows always
    pass."""
    views: list[tuple[dict[str, Any], dict[str, Any]]] = []
    bad: list[Any] = []
    for p in drop_corrupt_rows(b, rows, "plan", PLAN_NUMS, required=("created_ts", "entry", "stop", "target")):
        if p["entry"] <= 0 or p["entry"] == p["stop"] or (p["r_result"] is not None and abs(p["r_result"]) > MAX_ABS_R):
            bad.append(p["id"])
            continue
        v = _view(p)
        if v["rr"] is None or v["risk_pct"] is None or not math.isfinite(v["rr"]) or not math.isfinite(v["risk_pct"]) or not finite_json(v):
            bad.append(p["id"])
            continue
        views.append((p, v))
    if bad:
        b.warn(
            f"{len(bad)} stored plan rows skipped: entry <= 0, entry equal to stop, a reward:risk or an R result out of "
            f"range, or NaN / Infinity in tags / rules; id {bad[:10]}"
        )
    return views


def _unscorable(p: dict[str, Any]) -> str | None:
    """Why a stored open plan cannot be scored on candles, else None. plan_add and the demo trader's shadow plans always
    pass; this guards rows another writer left with a blob, an unknown side / entry type, a level that is text (also
    "NaN"), inf, <= 0 or equal to the stop, or a created / expires time that is no usable unix seconds (text, inf, 1e20).
    Before item 296 such a plan raised out of `resolve_plan` / the candle query (ValueError, ZeroDivisionError,
    OverflowError) and failed plan_review and the hourly plan_resolve job for every other plan, or was scored as garbage."""
    if any(isinstance(v, (bytes, bytearray, memoryview)) for v in p.values()):
        return "a blob in a column"
    if p["side"] not in ("long", "short") or p["entry_type"] not in ("market", "limit", "stop"):
        return "side / entry_type is not one plan_add writes"
    entry, stop, target = (store_num(p[k]) for k in ("entry", "stop", "target"))
    if entry is None or stop is None or target is None or min(entry, stop, target) <= 0:
        return "entry / stop / target is not a positive finite number"
    if entry == stop:
        return "entry equals stop (zero risk)"
    for k in ("created_ts", "expires_ts"):
        if _expires_view(p[k])["expires"] is None:
            return f"{k} is not usable unix seconds"
    return None


def _rec_unusable(rec: dict[str, Any], plan: dict[str, Any]) -> str | None:
    """Why a settled rec_paper row cannot score its linked option plan, else None. assistant_paper_status always writes
    usable values; this guards rows another writer left with text / a blob / inf / 1e308 / 1e20 in settled_ts or
    opened_ts (int() raised ValueError / OverflowError, or the store binding OverflowError, out of plan_review and the
    plan_resolve job before item 299), or such a delivery_price / r_worst_case (written into the plan as garbage). A
    delivery price more than MAX_LEVEL_RATIO times away from the plan's entry (the underlying's price) is garbage too."""
    for k in ("settled_ts", "opened_ts"):
        if _expires_view(rec[k])["expires"] is None:
            return f"{k} is not usable unix seconds"
    px = rec["delivery_price"]
    if px is not None:
        v, entry = store_num(px), store_num(plan["entry"])
        if v is None or v <= 0 or (entry is not None and entry > 0 and not entry / MAX_LEVEL_RATIO <= v <= entry * MAX_LEVEL_RATIO):
            return f"delivery_price is not a positive finite number within {MAX_LEVEL_RATIO:g}x of the plan's entry"
    rw = rec["r_worst_case"]
    if rw is not None and ((v := store_num(rw)) is None or abs(v) > MAX_ABS_R):
        return f"r_worst_case is not a finite number within +-{MAX_ABS_R:g}"
    return None


def _far_from(p: dict[str, Any], price: float | None) -> bool:
    """True when a level is more than MAX_LEVEL_RATIO times away from the market's last close (1e308 written by another
    writer: such a plan would be scored, e.g. 'target' with R -1, or overflow R to inf)."""
    if price is None:
        return False
    return any(not price / MAX_LEVEL_RATIO <= float(p[k]) <= price * MAX_LEVEL_RATIO for k in ("entry", "stop", "target"))


def _open_ms(x: Any) -> int | None:
    """A candle's open time as int ms; None when it is None, text, a bool, NaN, inf or a 300+-digit integer (int() of
    such a value raised out of plan_review and the plan_resolve job)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        return int(finite_float(x))
    except (TypeError, ValueError, OverflowError):
        return None


async def _candles(rt: Runtime, b: Builder, exchange: str, symbol: str, since_ms: int, until_ms: int) -> tuple[str, int, list[Any]] | None:
    """Candles of one market from ``since_ms`` to ``until_ms``: 1h first, then 4h, then 1d when a timeframe gives nothing.

    Pages forward with an upper bound per page (some venues answer with the latest bars otherwise); the cursor follows
    the last returned candle, so venues that cap rows below PAGE are paged too. Failures go into ``b.missing``.
    """
    for tf, tf_ms in TIMEFRAMES:
        rows: list[Any] = []
        cursor = (since_ms // tf_ms) * tf_ms
        ok = True
        for page in range(MAX_PAGES):
            until = min(cursor + PAGE * tf_ms - 1, until_ms)
            got = await b.guard(
                f"ohlcv:{exchange}:{symbol}:{tf}", f"ccxt:{exchange}", rt.exchanges.ohlcv(exchange, symbol, tf, PAGE, cursor, until)
            )
            if got is None:
                ok = page > 0
                if ok:
                    b.warn(f"{exchange} {symbol} {tf}: a later candle page failed; plans past it wait for the next review")
                break
            shaped = [x for x in got if isinstance(x, (list, tuple)) and len(x) >= 5] if isinstance(got, list) else []
            stamped = [(ts, x) for x in shaped if (ts := _open_ms(x[0])) is not None]
            if len(stamped) < len(shaped):
                b.warn(
                    f"ccxt:{exchange} {symbol} {tf}: {len(shaped) - len(stamped)} candle(s) with an open time that is not a "
                    "finite number dropped"
                )
            good = [x for _, x in stamped]
            rows += good
            last = max((ts for ts, _ in stamped), default=None)
            nxt = last + tf_ms if last is not None and last + tf_ms <= until else until + 1
            if nxt > until_ms or nxt <= cursor:
                break
            cursor = nxt
        if ok and rows:
            return tf, tf_ms, rows
    return None


async def review_open(rt: Runtime, b: Builder) -> list[int]:
    """Resolve every open plan that the data allows; returns the ids resolved now. Never raises on provider failures."""
    assert rt.store is not None
    now_ts = int(rt.now().timestamp())
    open_rows = await rt.store.plan_list(status="open", limit=5000)
    resolved: list[int] = []
    markets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for p in open_rows:
        rid = p["rec_paper_id"]
        if rid is not None:
            if isinstance(rid, bool) or not isinstance(rid, int) or not 1 <= rid <= SQLITE_INT_MAX:
                # text raised ValueError in int(), 1e308 OverflowError in the lookup (before item 296)
                b.warn(f"plan {p['id']} skipped: stored rec_paper_id is not a row id")
                continue
            rec = await rt.store.rec_paper_get(rid)
            if rec is None:
                b.miss(f"plan:{p['id']}", "tradedesk", "data_unavailable", f"linked rec_paper #{p['rec_paper_id']} not found")
            elif rec["status"] == "settled":
                if (why := _rec_unusable(rec, p)) is not None:
                    b.warn(f"plan {p['id']} skipped: its linked rec_paper #{rid} has a stored {why}")
                    continue
                n = await rt.store.plan_resolve(
                    p["id"],
                    outcome="expired",
                    resolved_ts=int(rec["settled_ts"]),
                    filled_ts=int(rec["opened_ts"]),
                    fill=None,
                    exit_=rec["delivery_price"],
                    r_result=rec["r_worst_case"],
                    timeframe=None,
                    note=f"scored by rec_paper #{rec['id']}: r_worst_case = P&L / worst-loss USD with Deribit fees; exit = delivery price",
                )
                if n:
                    resolved.append(p["id"])
            continue  # a linked option plan is settled with its paper trade (assistant_paper_status), never on candles
        if (why := _unscorable(p)) is not None:
            # a warning, not `missing`: the collector job raises on `missing`, which would fail it every hour for one bad row
            b.warn(f"plan {p['id']} skipped: stored {why}")
            continue
        markets[(p["exchange"], p["symbol"])].append(p)
    for (exchange, symbol), plans in markets.items():
        since_ms = min(int(p["created_ts"]) for p in plans) * 1000
        until_ms = min(now_ts, max(usable_expires(p["expires_ts"]) or 0 for p in plans)) * 1000
        got = await _candles(rt, b, exchange, symbol, since_ms, until_ms)
        if got is None:
            continue  # in `missing`; the plans stay open
        tf, tf_ms, rows = got
        closes = [c for x in rows if (c := store_num(x[4])) is not None and c > 0]
        last_close = closes[-1] if closes else None
        for p in plans:
            if _far_from(p, last_close):
                b.warn(f"plan {p['id']} skipped: a stored level is over {MAX_LEVEL_RATIO:g}x away from the market price")
                continue
            try:
                res = resolve_plan(p, rows, tf_ms=tf_ms, now_ts=now_ts, fee=TAKER_FEE)
            except (TypeError, ValueError, ZeroDivisionError, OverflowError) as e:  # never fail the other plans on one row
                b.warn(f"plan {p['id']} skipped: scoring failed ({type(e).__name__})")
                continue
            if first_nonfinite(res) is not None:
                b.warn(f"plan {p['id']} skipped: its levels overflow the R arithmetic (inf / NaN)")
                continue
            if res["status"] == "resolved":
                note = "scored on the underlying's stop / target, not on the option's P&L" if p["instrument"] == "option" else None
                n = await rt.store.plan_resolve(
                    p["id"],
                    outcome=res["outcome"],
                    resolved_ts=res["resolved_ts"],
                    filled_ts=res["filled_ts"],
                    fill=res["fill"],
                    exit_=res["exit"],
                    r_result=res["r_result"],
                    timeframe=tf,
                    note=note,
                )
                if n:
                    resolved.append(p["id"])
            elif res["fill"] is not None and p["fill"] is None:
                await rt.store.plan_set_fill(p["id"], filled_ts=res["filled_ts"], fill=res["fill"], timeframe=tf)
    return resolved


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,  # plan_add writes to the LOCAL store only — no orders, no external effects
        description=(
            "Log a trade plan the assistant gave (ALWAYS, whatever the verdict: 'enter' = checklist ✅, 'wait' = ⏳, 'skip' = ❌), "
            "so plans are scored later on candles by plan_review. Local store only: no order is sent. `exchange` + `symbol` "
            "name the ccxt market whose candles score the plan (options: the underlying, e.g. binance BTC/USDT). Prices in "
            "quote currency. `entry_type`: 'market' = filled at `entry` at this moment (pass the current price), 'limit' / "
            "'stop' = filled when price first touches `entry`. Long needs stop < entry < target, short target < entry < stop. "
            "`expires_ts` (unix seconds) is the plan horizon: 2 hours to 180 days ahead; the creation time is always now. "
            "`rules`: the owner's rules 1-8 -> 'pass' / 'fail' / 'warn'. Tags: 'src:own', 'idea:<slug>' when the plan relies "
            "on an idea card, 'setup:S3', 'event:cpi' ... Option plans: book the structure with assistant_paper_open first "
            "and pass `rec_paper_id` (then the plan is scored by that paper trade); without it the plan is scored on the "
            "underlying's stop / target. Returns the id, reward:risk `rr`, the risk distance `risk_pct` and the R a round "
            "trip of taker fees costs (decimals)."
        ),
    )
    async def plan_add(
        exchange: Annotated[
            str, Field(min_length=2, max_length=30, description="ccxt exchange id for the candles, e.g. 'binance', 'bybit'.")
        ],
        symbol: Annotated[
            str, Field(min_length=3, max_length=40, description="ccxt symbol, e.g. 'BTC/USDT:USDT' (perp) or 'BTC/USDT' (spot).")
        ],
        instrument: Annotated[Literal["perp", "spot", "option"], Field(description="What the plan trades.")],
        side: Annotated[Literal["long", "short"], Field(description="Direction of the plan.")],
        entry_type: Annotated[Literal["market", "limit", "stop"], Field(description="How the entry fills (see the description).")],
        entry: Annotated[float, Field(gt=0, description="Entry price, quote currency.")],
        stop: Annotated[float, Field(gt=0, description="Stop price, quote currency (the price that cancels the thesis).")],
        target: Annotated[float, Field(gt=0, description="Target price, quote currency.")],
        expires_ts: Annotated[int, Field(description="Plan horizon, unix seconds; 2 h to 180 days from now.")],
        verdict: Annotated[Literal["enter", "wait", "skip"], Field(description="Checklist verdict: enter (✅), wait (⏳), skip (❌).")],
        thesis: Annotated[str | None, Field(max_length=2000, description="Why the plan and what cancels it.")] = None,
        rules: Annotated[
            dict[str, Literal["pass", "fail", "warn"]] | None,
            Field(description="Owner's rules '1'..'8' -> 'pass' | 'fail' | 'warn' (docs/knowledge/trading-strategy.md)."),
        ] = None,
        tags: Annotated[
            list[str] | None, Field(max_length=20, description="Free-form tags, e.g. ['src:own', 'idea:<slug>', 'setup:S3'].")
        ] = None,
        rec_paper_id: Annotated[
            int | None, Field(ge=1, description="Option plans: id of the assistant_paper_open row that books the structure.")
        ] = None,
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(PLAN_WARNING)
        if (bad := reject_bad_id(b, rec_paper_id=rec_paper_id)) is not None:  # 10**20 raised OverflowError out of the lookup
            return bad
        if not await store_ready(b, rt, "the journal"):
            return b.build(None, ok=False)
        try:
            now_ts = int(rt.now().timestamp())
            err = validate_levels(side, entry, stop, target)
            if err is None and not now_ts + MIN_HORIZON_S <= expires_ts <= now_ts + MAX_HORIZON_S:
                err = f"expires_ts must be 2 h to 180 days ahead (now {_iso(now_ts)}, got {_iso(expires_ts) if expires_ts > 0 else expires_ts})"
            if err is None and rules and not set(rules) <= RULE_KEYS:
                err = f"rules keys must be '1'..'8', got {sorted(set(rules) - RULE_KEYS)}"
            if err is None and rec_paper_id is not None and instrument != "option":
                err = "rec_paper_id links option plans only (instrument='option')"
            if err is not None:
                b.miss("plan", "tradedesk", "invalid_input", err)
                return b.build(None, ok=False)
            if rec_paper_id is not None and await rt.store.rec_paper_get(rec_paper_id) is None:
                b.miss("rec_paper_id", "tradedesk", "invalid_input", f"rec_paper #{rec_paper_id} not found")
                return b.build(None, ok=False)
            clean_tags = [t.strip() for t in tags or [] if t and t.strip()]
            plan_id = await rt.store.plan_insert(
                created_ts=now_ts,
                exchange=exchange.lower(),
                symbol=symbol,
                instrument=instrument,
                side=side,
                entry_type=entry_type,
                entry=entry,
                stop=stop,
                target=target,
                expires_ts=expires_ts,
                verdict=verdict,
                thesis=thesis,
                rules=rules or {},
                tags=clean_tags,
                rec_paper_id=rec_paper_id,
            )
            g = geometry(entry, stop, target)
            if g["rr"] < 2:
                b.warn(f"reward:risk {g['rr']:.2f} < 2 (owner's rule 5)")
            failed = sorted(k for k, v in (rules or {}).items() if v == "fail")
            if verdict == "enter" and failed:
                b.warn(f"verdict 'enter' with failed rules {failed}")
            if instrument == "option" and rec_paper_id is None:
                b.warn("option plan without rec_paper_id: it will be scored on the underlying's stop / target, not on the option's P&L")
            return b.build(
                {
                    "id": plan_id,
                    "created": _iso(now_ts),
                    "expires": _iso(expires_ts),
                    "verdict": verdict,
                    "rr": r(g["rr"], 3),
                    "risk_pct": r(g["risk_pct"], 5),
                    "reward_pct": r(g["reward_pct"], 5),
                    "fee_r_round_trip": r(g["fee_r_round_trip"], 4),
                }
            )
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "List logged trade plans, newest first. Filters: `status` (open / resolved), `verdict` (enter / wait / skip), "
            "`tag` (exact tag), `symbol`. Each plan shows its levels, reward:risk `rr`, risk distance `risk_pct` (decimal), "
            "fill, and once resolved: outcome (target / stop / expired / not_filled), exit and net R `r_result`."
        ),
    )
    async def plan_list(
        status: Annotated[Literal["open", "resolved"] | None, Field(description="Optional status filter.")] = None,
        verdict: Annotated[Literal["enter", "wait", "skip"] | None, Field(description="Optional verdict filter.")] = None,
        tag: Annotated[str | None, Field(description="Optional single-tag filter, e.g. 'idea:<slug>'.")] = None,
        symbol: Annotated[str | None, Field(description="Optional ccxt symbol filter.")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Max rows.")] = 100,
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "the journal"):
            return b.build(None, ok=False)
        try:
            rows = await rt.store.plan_list(status=status, verdict=verdict, tag=tag, symbol=symbol, limit=limit)
            plans = _listable(b, rows)
            return b.build({"count": len(plans), "plans": plans})
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,  # resolving writes to the LOCAL store only — no orders
        description=(
            "Score the assistant's trade plans. First resolves every open plan the candles allow: OHLCV from the plan's "
            "exchange (1h candles; 4h, then 1d when 1h gives nothing), from the first complete candle after the plan was "
            "written up to its horizon. Fill: market at entry at creation; limit / stop on the first touch of the entry. "
            "Then the first touch of stop or target; a candle touching both counts as the stop; on the fill candle only the "
            "stop counts. No touch by the horizon: 'expired' at the last candle's close; never filled: 'not_filled' (R "
            "null). R = signed move / |entry - stop| minus a flat 0.05 % taker fee per side. Option plans linked to a "
            "rec_paper row are scored by that paper trade once it settles (r_worst_case); unlinked ones on the "
            "underlying. Returns the ids resolved now, the open plans and a summary by verdict (enter / wait / skip; "
            "wait and skip are counterfactual) and by tag within verdict: n, fills, outcomes, hit rate (targets / "
            "filled), win rate (R > 0), mean and median R with a t 95 % CI on the mean. A venue failure goes into "
            "`missing` and its plans stay open. The demo trader's shadow plans (tag src:demo-shadow, one per setup signal) "
            "are resolved too but kept out of `open` and `summary` (they are not the assistant's judgment); their own "
            "summary is in `demo_shadow` (compare demo:taken with demo:skipped). `demo_option_pairs`: the demo trader's "
            "BTC / ETH signals traded both as futures and as a long option (premium = the stop): n pairs with both legs "
            "closed, mean R of each leg (futures R = P&L / risk at the stop, option R = P&L / premium + fee), mean of "
            "option R - futures R with a 95 % t CI, and how often the option outlived a futures stop-out and closed at "
            "the target; under 30 pairs the verdict says too little data (verdict text is Ukrainian). Buying options loses the variance risk premium on average: "
            "this is a live paired test, not a claimed edge. Paper scoring on candle highs / lows: not a fill guarantee, a forward "
            "record of the assistant's judgment, not proven edge."
        ),
    )
    async def plan_review() -> dict[str, Any]:
        b = Builder()
        b.warn(PLAN_WARNING)
        if not await store_ready(b, rt, "the journal"):
            return b.build(None, ok=False)
        try:
            resolved_now = await review_open(rt, b)
            now_ts = int(rt.now().timestamp())
            open_now = await rt.store.plan_list(status="open", exclude_tag=SHADOW_TAG, limit=5000)
            open_shadow = await rt.store.plan_list(status="open", tag=SHADOW_TAG, limit=100_000)
            stale = [
                p["id"]
                for p in open_now
                if (e := usable_expires(p["expires_ts"])) is not None and e + STALE_S < now_ts and p["rec_paper_id"] is None
            ]
            if stale:
                b.warn(f"plans {stale} are past their horizon by more than a day and still open: candles missing for their market")
            done = await rt.store.plan_list(status="resolved", exclude_tag=SHADOW_TAG, limit=5000)
            shadow = await rt.store.plan_list(status="resolved", tag=SHADOW_TAG, limit=100_000)
            # Rows another writer corrupted are left out with a warning: `_view` / the stats raised on them (item 296).
            open_views = _listable(b, open_now)
            done_ok = _sound(b, done)
            summary = summarize_plans([p for p, _ in done_ok])
            shadow_summary = summarize_plans([p for p, _ in _sound(b, shadow)])

            def rnd(s: dict[str, Any]) -> dict[str, Any]:
                out = dict(s)
                for k in ("hit_rate", "win_rate", "mean_r", "median_r"):
                    out[k] = r(out[k], 3)
                out["ci95_mean_r"] = [r(x, 3) for x in out["ci95_mean_r"]] if out["ci95_mean_r"] else None
                return out

            data = {
                "resolved_now": resolved_now,
                "open": open_views,
                "summary": {
                    "by_verdict": {v: rnd(s) for v, s in summary["by_verdict"].items()},
                    "by_tag": {t: {v: rnd(s) for v, s in g.items()} for t, g in summary["by_tag"].items()},
                },
                "recent_resolved": [v for _, v in done_ok[:50]],
                "demo_shadow": {
                    "open": len(open_shadow),
                    "by_verdict": {v: rnd(s) for v, s in shadow_summary["by_verdict"].items()},
                    "by_tag": {t: {v: rnd(s) for v, s in g.items()} for t, g in shadow_summary["by_tag"].items()},
                },
                "demo_option_pairs": paired_report(
                    await rt.store.demo_trade_list(limit=100_000), await rt.store.demo_option_list(limit=100_000)
                ),
            }
            return b.build(data)
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,  # writes locally only
        description=(
            "Link a logged plan to the owner's real trade in `journal` (when he actually traded the plan), so the plan's "
            "paper score and the real result can be compared. Both ids must exist."
        ),
    )
    async def plan_link(
        id: Annotated[int, Field(ge=1, description="Plan id from plan_add.")],
        journal_id: Annotated[int, Field(ge=1, description="Trade id from journal_add.")],
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "the journal"):
            return b.build(None, ok=False)
        try:
            # An id beyond SQLite INTEGER (10**20) raised OverflowError out of the lookup; no such row can exist.
            if (
                id > SQLITE_INT_MAX
                or journal_id > SQLITE_INT_MAX
                or await rt.store.plan_get(id) is None
                or await rt.store.journal_get(journal_id) is None
            ):
                b.miss("plan_link", "tradedesk", "invalid_input", f"plan #{id} or journal trade #{journal_id} not found")
                return b.build(None, ok=False)
            await rt.store.plan_link_journal(id, journal_id)
            row = await rt.store.plan_get(id)
            views = _listable(Builder(), [row]) if row else []
            if row is not None and not views:
                # The link reads no stored level, so it is written; but a corrupt row raised in `_view` (or failed the reply)
                # after the write, and inf / NaN came back as Infinity / NaN: the row is then left out of the reply.
                b.warn(f"plan #{id} was linked to journal trade #{journal_id}, but its stored row is malformed, so it is not returned")
                return b.build({"id": id, "journal_id": journal_id})
            return b.build(views[0] if views else None)
        except STORE_ERRORS as e:
            return store_failed(b, e)
