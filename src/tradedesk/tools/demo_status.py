"""Read-only status of the Bybit demo trader: open positions, closed trades and mean R per setup against the backtest
passport bands (item 409 of docs/ideas/IDEA-20261102-standing-improvements.md).

The numbers are the demo block of ``scripts/checkpoint_report.py``: the same pure functions
(``analytics.checkpoint.demo_block``) over the same ``demo_trade`` rows, read through the store's read accessor
``demo_trade_list``. Nothing under ``execution/`` is imported; no key, no order, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.checkpoint import DEMO, MIN_DEMO, demo_block
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, STORE_ERRORS, drop_corrupt_rows, r, store_failed, store_ready

COMPACT_OPEN = 10  # open positions listed in full by default (newest fills first); the rest only counted
RECENT_CLOSED = 50  # closed trades listed with include_details (newest closes first)
MAX_ROWS = 100_000  # demo_trade rows read (newest first); a read that fills it may leave the oldest trades out
NOTE_CHARS = 600  # a demo note longer than this (a long list of stuck / orphaned / P&L-mismatch trades) is cut by default
# demo_trade columns the analytics or the views read as numbers (a string / inf there drops the row with a warning)
NUMERIC = (
    "signal_ts", "entry", "stop", "target", "qty", "leverage", "risk_usd", "fill", "opened_ts", "exit", "closed_ts",
    "pnl_usd", "r_multiple", "be_ts", "be_stop", "entry_px", "exit_px", "exit_ts", "fees_usd", "rest_until", "gone_ts",
)  # fmt: skip
OPEN_STATUSES = ("open", "closing")  # filled and not closed yet (closing: the exit order is working)

DEMO_WARNING = (
    "Bybit DEMO account (play money, api-demo.bybit.com); read-only view of the demo trader's journal, no orders. R per "
    "trade = venue P&L with fees / risk at the fill (not return on capital). Bands are the frozen backtest passport bands "
    "of the mean R of n trades (docs/CHECKPOINT-2026-11-30.md): i.i.d. and rolling p5..p95, the decision band takes the "
    "wider end of each; a verdict needs >= 30 closed trades of the setup's current version. A consistent verdict is NOT "
    "evidence of an edge: almost every reference passport is itself inconclusive around 0R or below. The bands do not "
    "model the demo's daily slots or its non-random choice among candidates; with 9 setups, even a demo behaving exactly "
    "like its passports puts at least one setup outside its i.i.d. band about 60 % of the time, so one 'no' verdict is a "
    "reason to review the execution, not a conclusion."
)


def _iso(ms: Any) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000, UTC).isoformat(timespec="minutes")
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _pair(x: Any, nd: int = 4) -> list[float | None] | None:
    return None if x is None else [r(v, nd) for v in x]


def _setup_rows(rows: list[dict[str, Any]], details: bool) -> list[dict[str, Any]]:
    """One entry per setup of ``DEMO``: its R row and, for a limit setup, its fill-share row as ``fill``."""
    by_key = {row["key"]: row for row in rows}
    out = []
    for spec in DEMO:
        row = by_key[f"demo:{spec.setup}"]
        band = row["band"]
        item: dict[str, Any] = {
            "setup": spec.setup,
            "versions": sorted(spec.versions) if spec.versions is not None else "all",
            "timeframe": spec.timeframe or "all",
            "n": row["n"],
            "mean_r": r(row["metric"], 4),
            "band": {"n_ref": band["n_ref"], "decision": _pair(band["decision"], 3), "iid": _pair(band["iid"], 3)},
            "verdict": row["verdict"],
            "side": row["side"],
        }
        if details:
            item["band"]["rolling"] = _pair(band["rolling"], 3)
            item |= {"name": row["name"], "source": spec.source, "notes": row["notes"]}
        fill = by_key.get(f"demo:{spec.setup}:fill")
        if fill is not None:
            f: dict[str, Any] = {
                "n": fill["n"],
                "share": r(fill["metric"], 4),
                "passport": fill["band"]["passport"],
                "wilson95": _pair(fill["band"]["wilson"], 4),
                "verdict": fill["verdict"],
                "side": fill["side"],
            }
            if details:
                f["notes"] = fill["notes"]
            item["fill"] = f
        out.append(item)
    return out


def _s12(block: dict[str, Any]) -> dict[str, Any]:
    checks = [
        {
            "name": c["name"],
            "n": c["n"],
            "days": c["days"],
            "mean_r": r(c["mean"], 4),
            "ci95": _pair(c["ci"], 4),
            "n_min": c["n_min"],
            "floor": c["floor"],
            "if_below": c["if_below"],
            "verdict": c["verdict"],
        }
        for c in block["checks"]
    ]
    return {"verdict": block["verdict"], "checks": checks, "other_entry_closed": block["other"]}


def _open_view(t: dict[str, Any], details: bool) -> dict[str, Any]:
    out = {
        "setup": t.get("setup"),
        "symbol": t.get("symbol"),
        "side": t.get("side"),
        "timeframe": t.get("timeframe"),
        "opened": _iso(t.get("opened_ts")),
        "fill": t.get("fill"),
        "stop": t.get("be_stop") if t.get("be_stop") is not None else t.get("stop"),
        "target": t.get("target"),
        "risk_usd": r(t.get("risk_usd"), 2),
    }
    if details:
        out |= {
            "link_id": t.get("link_id"), "status": t.get("status"), "version": t.get("version"), "qty": t.get("qty"),
            "initial_stop": t.get("stop"), "break_even_at": _iso(t.get("be_ts")), "signal_at": _iso(t.get("signal_ts")),
            "entry_kind": t.get("entry_kind"),
        }  # fmt: skip
    return out


def _closed_view(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "link_id": t.get("link_id"), "setup": t.get("setup"), "symbol": t.get("symbol"), "side": t.get("side"),
        "timeframe": t.get("timeframe"), "version": t.get("version"), "opened": _iso(t.get("opened_ts")),
        "closed": _iso(t.get("closed_ts")), "fill": t.get("fill"), "exit": t.get("exit"), "exit_reason": t.get("exit_reason"),
        "r_multiple": r(t.get("r_multiple"), 4), "pnl_usd": r(t.get("pnl_usd"), 2), "fees_usd": r(t.get("fees_usd"), 4),
    }  # fmt: skip


def _cut_note(note: str) -> str:
    """A note longer than NOTE_CHARS cut to at most NOTE_CHARS characters. Its explanatory tail (from the last '; ' on,
    e.g. '; вони не входять у закриті вибірки…') is kept when it fits in a third of the limit; the part before it is cut
    at the last ', ' list separator within the room left (whole trade ids kept), else by characters (no separator in
    the second half of that room). ' …' marks where the list was cut. Known limitation: a '; ' inside the last list
    entry is taken for the tail separator, so the kept tail is then a piece of that entry (the cut still fits the
    limit and ends on a whole item)."""
    if len(note) <= NOTE_CHARS:
        return note
    j = note.rfind("; ")
    tail = note[j:] if j > 0 and len(note) - j <= NOTE_CHARS // 3 else ""
    body = note[: len(note) - len(tail)]
    room = NOTE_CHARS - len(tail) - 2  # 2 = len(" …")
    i = body.rfind(", ", 0, room + 2)  # a separator starting at `room` still leaves a whole item before it
    head = body[:i] if i >= room // 2 else body[:room]
    return head.rstrip() + " …" + tail


def _newest(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda t: t.get(key) if t.get(key) is not None else float("-inf"), reverse=True)


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Status of the Bybit DEMO trader (play money; read-only, no orders, no keys): open positions, closed trades and "
            "the mean R of each setup against its backtest passport band, the same numbers as the demo block of "
            "scripts/checkpoint_report.py (same pure functions, same demo_trade rows of the local store). `setups`: per setup "
            "(S3, S12, S28, S17, S14, S16, S38, S39, S40) the closed trades of its current version `n`, `mean_r` (R = venue "
            "P&L with fees / risk at the fill, per trade, not on capital), the band of the largest computed n' <= n "
            "(`decision` = wider end of the i.i.d. and rolling p5..p95 of the passport's mean R; `iid` shown for reference) "
            "and `verdict`, a Ukrainian label as in the report: early (fewer than "
            f"{MIN_DEMO} closed trades), consistent (inside the decision band) or no (outside it; `side` says below or "
            "above, also in Ukrainian). Limit setups (S28, S17) "
            "also have `fill`: share of placements past their deadline that filled (decimal) against the passport share "
            "with a 95 % Wilson CI. `s12_stop_rule`: the owner's S12 stop rule (v9: n >= 30 and mean below -0.827 -> stop "
            "and review the execution; v4 entry: n >= 100 and below -0.524 -> drop) with a 95 % CI clustered by signal day. "
            "`notes` (Ukrainian): stuck, orphaned and P&L-mismatch trades, 'no' verdicts on 3+ setups; by default a note "
            f"longer than {NOTE_CHARS} characters is cut to that length: the trade list at its last ', ' that fits, so "
            "trade ids stay whole (by characters if no ', ' is in the second half), ' …' where it was cut, the "
            "explanatory tail after the list (from the last '; ', up to a third of the limit) kept; a warning says so. "
            "`open`: filled positions not "
            f"closed yet (stop = break-even stop once moved; prices in USDT), newest {COMPACT_OPEN} by default with counts per "
            "setup. include_details=true adds every open position with more fields, each setup's name, passport source and "
            f"notes (clustered CI, fees vs the backtest model, orphan scenarios), the full text of every demo note, and the "
            f"{RECENT_CLOSED} most recent closed "
            "trades. Consistent is not evidence of an edge (see `warnings`). Needs the store the demo trader writes "
            "(TRADEDESK_DB_PATH)."
        ),
    )
    async def demo_status(
        include_details: Annotated[
            bool,
            Field(
                description=(
                    "Every open position with more fields, per-setup names, passport sources and notes, the full text "
                    f"of every demo note, and the {RECENT_CLOSED} most recent closed trades (default: compact)."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(DEMO_WARNING)
        if not await store_ready(b, rt, "the demo trader's journal"):
            return b.build(None, ok=False)
        try:
            raw = await rt.store.demo_trade_list(limit=MAX_ROWS)
        except STORE_ERRORS as e:
            return store_failed(b, e)
        if len(raw) >= MAX_ROWS:
            b.warn(
                f"the store read stopped at its cap of {MAX_ROWS} demo_trade rows (newest first): older trades may be left "
                "out, so n and mean R may differ from scripts/checkpoint_report.py"
            )
        trades = drop_corrupt_rows(b, raw, "demo_trade", NUMERIC, key="link_id")
        # the report reads ORDER BY signal_ts (NULL first); the store lists newest first
        trades.sort(key=lambda t: (t.get("signal_ts") is not None, t.get("signal_ts") or 0))
        now = rt.now()
        now_ms = int(now.timestamp() * 1000)
        try:
            block = demo_block(trades, now_ms)
            setups = _setup_rows(block["rows"], include_details)
            s12 = _s12(block["s12_stop_rule"])
        except (TypeError, ValueError, KeyError, ArithmeticError) as e:  # a cell no guard foresaw: an answer, never a raise
            b.miss("demo_status", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
            return b.build(None, ok=False)
        by_status: dict[str, int] = {}
        for t in trades:
            by_status[str(t.get("status"))] = by_status.get(str(t.get("status")), 0) + 1
        opened = _newest([t for t in trades if t.get("status") in OPEN_STATUSES], "opened_ts")
        open_by_setup: dict[str, int] = {}
        for t in opened:
            open_by_setup[str(t.get("setup"))] = open_by_setup.get(str(t.get("setup")), 0) + 1
        shown = opened if include_details else opened[:COMPACT_OPEN]
        notes = block["notes"]
        if not include_details and (cut := sum(len(n) > NOTE_CHARS for n in notes)):
            notes = [_cut_note(n) for n in notes]
            b.warn(
                f"compact answer: {cut} long note(s) cut to at most {NOTE_CHARS} characters (the trade list at a ', ' "
                "boundary when one is in the second half of the room, else by characters; ' …' marks the cut, the "
                "explanatory tail after the last '; ' is kept if it fits in a third of the limit); include_details=true "
                "for the full text"
            )
        data: dict[str, Any] = {
            "as_of": now.astimezone(UTC).isoformat(timespec="seconds"),
            "trades_by_status": dict(sorted(by_status.items())),
            "setups": setups,
            "s12_stop_rule": s12,
            "notes": notes,
            "open_count": len(opened),
            "open_by_setup": dict(sorted(open_by_setup.items())),
            "open": [_open_view(t, include_details) for t in shown],
        }
        if include_details:
            closed = _newest([t for t in trades if t.get("status") == "closed"], "closed_ts")
            data["closed_recent"] = [_closed_view(t) for t in closed[:RECENT_CLOSED]]
        elif len(opened) > COMPACT_OPEN:
            b.warn(f"compact answer: the newest {COMPACT_OPEN} of {len(opened)} open positions; include_details=true for all")
        if not trades:
            b.warn("no demo trades in the store: the demo trader writes demo_trade into TRADEDESK_DB_PATH; is it running on this DB?")
        return b.build(data)
