"""Trade plans of the assistant: log every plan (enter / wait / skip) and score it later on candles.

Local bookkeeping only: nothing is sent to any venue. A plan is written by `plan_add` when the assistant answers a
"trade plan" request (prompt `pre_trade_checklist`, step 10), whatever its verdict, and is resolved on OHLCV candles by
`plan_review` or the hourly collector job `plan_resolve` (same code: `review_open`). Conventions of the scoring are in
`analytics/trade_plan.py`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.trade_plan import TAKER_FEE, geometry, resolve_plan, summarize_plans, validate_levels
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r
from .journal import _store_guard

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

SHADOW_TAG = "src:demo-shadow"  # execution/demo_trader.py: one plan per setup signal, not the assistant's judgment


def _iso(ts: int | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


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
        "expires": _iso(p["expires_ts"]),
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
            good = [x for x in got if isinstance(x, (list, tuple)) and len(x) >= 5 and x[0] is not None] if isinstance(got, list) else []
            rows += good
            last = max((int(x[0]) for x in good), default=None)
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
        if p["rec_paper_id"] is not None:
            rec = await rt.store.rec_paper_get(int(p["rec_paper_id"]))
            if rec is None:
                b.miss(f"plan:{p['id']}", "tradedesk", "data_unavailable", f"linked rec_paper #{p['rec_paper_id']} not found")
            elif rec["status"] == "settled":
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
        markets[(p["exchange"], p["symbol"])].append(p)
    for (exchange, symbol), plans in markets.items():
        since_ms = min(int(p["created_ts"]) for p in plans) * 1000
        until_ms = min(now_ts, max(int(p["expires_ts"]) for p in plans)) * 1000
        got = await _candles(rt, b, exchange, symbol, since_ms, until_ms)
        if got is None:
            continue  # in `missing`; the plans stay open
        tf, tf_ms, rows = got
        for p in plans:
            res = resolve_plan(p, rows, tf_ms=tf_ms, now_ts=now_ts, fee=TAKER_FEE)
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
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
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
        await rt.store.init()
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
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        rows = await rt.store.plan_list(status=status, verdict=verdict, tag=tag, symbol=symbol, limit=limit)
        return b.build({"count": len(rows), "plans": [_view(p) for p in rows]})

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
            "summary is in `demo_shadow` (compare demo:taken with demo:skipped). Paper scoring on candle highs / lows: not a fill guarantee, a forward "
            "record of the assistant's judgment, not proven edge."
        ),
    )
    async def plan_review() -> dict[str, Any]:
        b = Builder()
        b.warn(PLAN_WARNING)
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        resolved_now = await review_open(rt, b)
        now_ts = int(rt.now().timestamp())
        open_now = await rt.store.plan_list(status="open", exclude_tag=SHADOW_TAG, limit=5000)
        open_shadow = await rt.store.plan_list(status="open", tag=SHADOW_TAG, limit=100_000)
        stale = [p["id"] for p in open_now if p["expires_ts"] + STALE_S < now_ts and p["rec_paper_id"] is None]
        if stale:
            b.warn(f"plans {stale} are past their horizon by more than a day and still open: candles missing for their market")
        done = await rt.store.plan_list(status="resolved", exclude_tag=SHADOW_TAG, limit=5000)
        shadow = await rt.store.plan_list(status="resolved", tag=SHADOW_TAG, limit=100_000)
        summary = summarize_plans(done)
        shadow_summary = summarize_plans(shadow)

        def rnd(s: dict[str, Any]) -> dict[str, Any]:
            out = dict(s)
            for k in ("hit_rate", "win_rate", "mean_r", "median_r"):
                out[k] = r(out[k], 3)
            out["ci95_mean_r"] = [r(x, 3) for x in out["ci95_mean_r"]] if out["ci95_mean_r"] else None
            return out

        data = {
            "resolved_now": resolved_now,
            "open": [_view(p) for p in open_now],
            "summary": {
                "by_verdict": {v: rnd(s) for v, s in summary["by_verdict"].items()},
                "by_tag": {t: {v: rnd(s) for v, s in g.items()} for t, g in summary["by_tag"].items()},
            },
            "recent_resolved": [_view(p) for p in done[:50]],
            "demo_shadow": {
                "open": len(open_shadow),
                "by_verdict": {v: rnd(s) for v, s in shadow_summary["by_verdict"].items()},
                "by_tag": {t: {v: rnd(s) for v, s in g.items()} for t, g in shadow_summary["by_tag"].items()},
            },
        }
        return b.build(data)

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
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        if await rt.store.plan_get(id) is None or await rt.store.journal_get(journal_id) is None:
            b.miss("plan_link", "tradedesk", "invalid_input", f"plan #{id} or journal trade #{journal_id} not found")
            return b.build(None, ok=False)
        await rt.store.plan_link_journal(id, journal_id)
        row = await rt.store.plan_get(id)
        return b.build(_view(row) if row else None)
