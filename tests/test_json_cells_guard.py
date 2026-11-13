"""Invariant 1 guard: JSON cells parsed from the DB never reach an MCP reply unbounded (item 361).

Items 352-358 found one field at a time that a cell nested 300 levels deep parsed fine but failed the MCP serialiser
("Circular reference detected (depth exceeded)", is_error=true). This file replaces the hunt by two checks:

1. Reviewed allowlists, read from the source with `ast` (no import side effects):
   - every json.load(s) call in `store/` and `tools/`;
   - every Store method that returns a decoded JSON cell (it calls json.loads or a module helper that does);
   - every call of such a method from `tools/`, keyed by the enclosing function and its order there.
   A new site fails here until it is reviewed: bound the cell with the `tools/_util.py` helpers (`cap_cell_depth` for
   free-form JSON, `clean_tags` / `clean_row_tags` / `clean_row_rules` / `clean_row_flags` for lists / objects of
   strings) or keep it out of the reply, then add it with the reason.
2. A drive: every reading tool on a store whose every decoded JSON column holds a cell nested 300 levels deep. Each reply
   must not be an error, must be ok, must warn, and must nest no deeper than MAX_CELL_DEPTH plus the envelope.

Not scanned: `collector/` and `execution/` (no MCP reply; the collector modules imported by tools read no decoded cell).
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import NOW, FakeEx, State, make_transports
from test_tool_failure_extra_19 import _deep, _session
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.tools._util import MAX_CELL_DEPTH, json_too_deep

SRC = Path(__file__).resolve().parents[1] / "src" / "tradedesk"
NOW_TS = int(NOW.timestamp())
NOW_MS = NOW_TS * 1000
ENVELOPE_LEVELS = 10  # envelope -> data -> book / list -> row -> cell, with room to spare (the serialiser fails at ~254)

# ---------- 1. reviewed allowlists ----------

# Every json.load / json.loads call in store/ and tools/: "<path under src/tradedesk>::<function>#<n-th in it>".
LOADS_SITES = {
    "store/db.py::_parse_journal_row#1": "journal / positions tags; RecursionError -> []; tools bound them (clean_tags)",
    "store/db.py::_parse_carry_paper_row#1": "carry_paper tags / entry_flags; RecursionError -> []; tools bound them (clean_row_*)",
    "store/db.py::_parse_json_cols#1": "shared decoder; RecursionError -> None; every Store method using it is in STORE_DECODERS",
    "store/db.py::record_trader_positions#1": "not MCP output: the collector's last position set, only diffed; RecursionError -> {}",
    "store/db.py::last_alerts_with_prefix#1": "not MCP output: the alerts job's health state; RecursionError -> None (item 358)",
    "store/db.py::events_list#1": "events.meta; RecursionError leaves the text; see TOOL_CALLS",
    "store/db.py::recent_alerts#1": "alerts.payload; RecursionError leaves the text; see TOOL_CALLS",
    "tools/events.py::events_seed_load#1": "json.load of the bundled package seed, not a DB cell",
    "tools/rules.py::rules_presets_load#1": "json.load of the bundled package presets, not a DB cell",
    "tools/calendar.py::_load_macro#1": "json.load of the bundled package calendar, not a DB cell",
    "tools/macro.py::_load_seed#1": "json.load of the bundled package seed, not a DB cell",
}

# Store methods whose rows carry a decoded JSON cell, and the cells.
STORE_DECODERS = {
    "journal_get": "tags",
    "journal_list": "tags",
    "position_get": "tags",
    "positions_list": "tags",
    "carry_paper_get": "tags, entry_flags",
    "carry_paper_list": "tags, entry_flags",
    "rec_paper_get": "legs",
    "rec_paper_list": "legs",
    "plan_get": "rules, tags",
    "plan_list": "rules, tags",
    "cp_cycle_get": "payload (collector only)",
    "cp_cycles": "payload",
    "cp_positions": "state, trade",
    "cp_logs": "payload",
    "cp_latest_marks": "payload",
    "events_list": "meta",
    "recent_alerts": "payload",
    "last_alerts_with_prefix": "payload (collector only)",
    "radar_list": "smile, expiries (collector only: the assistant snapshot reads them as numbers)",
    "cycle_regime_log_rows": "components (collector only: compares `state`)",
    "exec_audit_list": "payload (execution only)",
    "record_trader_positions": "positions_json (returns a row count, collector only)",
}

CLEAN_TAGS = "shown through clean_tags: a list of strings or skipped (items 352-354)"
PAIR_TAGS = "_clean_pair_tags: tags / entry_flags as lists of strings (items 356, 357)"
PLAN_VIEW = "_listable / _sound: clean_row_tags + clean_row_rules (items 356, 357)"
GOOD_LEGS = "_view shows only well-formed legs of finite numbers (_good_legs, item 356)"

# Every call of a STORE_DECODERS method from tools/: "<file>::<enclosing function>::<method>#<n-th in that function>".
TOOL_CALLS = {
    "events.py::events_list::events_list#1": "meta: cap_cell_depth -> null with a warning (item 358)",
    "events.py::event_study::events_list#1": "not in the reply: only ts is read",
    "history.py::alerts_recent::recent_alerts#1": "payload: cap_cell_depth -> null with a warning (item 358)",
    "paper_carry.py::book_view::cp_cycles#1": "payload: _capped (cap_cell_depth -> {}) (item 358)",
    "paper_carry.py::book_view::cp_latest_marks#1": "payload: _capped -> {}; only MARK_KEYS shown (item 358)",
    "paper_carry.py::book_view::cp_positions#1": "open rows: `state` is not in the reply (entry_apr / fees go through float, dir is compared)",
    "paper_carry.py::book_view::cp_positions#2": "closed rows: `trade` _capped -> None, the trade is left out (item 358)",
    "paper_carry.py::book_view::cp_logs#1": "side payload: _capped -> {}; only cost_realism numbers shown",
    "journal.py::journal_list::journal_list#1": CLEAN_TAGS,
    "journal.py::journal_close::journal_get#1": "not in the reply: status and levels only",
    "journal.py::journal_close::journal_get#2": CLEAN_TAGS,
    "journal.py::journal_stats::journal_list#1": "not in the reply: summary numbers only",
    "positions.py::positions_list::positions_list#1": CLEAN_TAGS,
    "positions.py::positions_close::position_get#1": "not in the reply: status only",
    "positions.py::positions_close::position_get#2": CLEAN_TAGS,
    "positions.py::positions_risk::positions_list#1": "not in the reply: greeks summed, tags unused",
    "trade_plan.py::review_open::plan_list#1": "not in the reply: ids and warnings only",
    "trade_plan.py::review_open::rec_paper_get#1": "not in the reply: settled fields only, legs unused",
    "trade_plan.py::plan_add::rec_paper_get#1": "not in the reply: existence check",
    "trade_plan.py::plan_list::plan_list#1": PLAN_VIEW,
    "trade_plan.py::plan_review::plan_list#1": PLAN_VIEW,
    "trade_plan.py::plan_review::plan_list#2": "not in the reply: count of open shadow plans",
    "trade_plan.py::plan_review::plan_list#3": PLAN_VIEW,
    "trade_plan.py::plan_review::plan_list#4": PLAN_VIEW + "; summary only",
    "trade_plan.py::plan_link::plan_get#1": "not in the reply: existence check",
    "trade_plan.py::plan_link::journal_get#1": "not in the reply: existence check",
    "trade_plan.py::plan_link::plan_get#2": PLAN_VIEW,
    "assistant_paper.py::book_paper::rec_paper_list#1": "not in the reply: ids of duplicates",
    "assistant_paper.py::book_paper::rec_paper_get#1": GOOD_LEGS,
    "assistant_paper.py::assistant_paper_status::rec_paper_list#1": "due trades: settled only when every leg is well formed",
    "assistant_paper.py::assistant_paper_status::rec_paper_list#2": GOOD_LEGS,
    "assistant_paper.py::assistant_paper_status::rec_paper_list#3": GOOD_LEGS + "; summary numbers",
    "carry_paper.py::carry_paper_open::carry_paper_list#1": "not in the reply: ids of duplicates",
    "carry_paper.py::carry_paper_status::carry_paper_get#1": PAIR_TAGS,
    "carry_paper.py::carry_paper_status::carry_paper_list#1": PAIR_TAGS,
    "carry_paper.py::carry_paper_status::carry_paper_list#2": PAIR_TAGS + " for the rows shown; summary numbers",
    "carry_paper.py::carry_paper_close::carry_paper_get#1": PAIR_TAGS + ", before the journal mirror copies tags",
    "carry_paper.py::carry_paper_close::carry_paper_get#2": "reread: the cleaned tags / entry_flags of #1 replace the stored ones",
    "carry_paper.py::carry_paper_close::carry_paper_get#3": "reread: the cleaned tags / entry_flags of #1 replace the stored ones",
}


def _sites(path: Path, match: Callable[[ast.Call], str | None]) -> list[str]:
    """'<function>[::<name>]#<n>' of every call `match` accepts ('' = no name), in source order; n counts per key."""
    found: list[str] = []

    def visit(node: ast.AST, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Call) and (name := match(child)) is not None:
                found.append(f"{fn}::{name}" if name else fn)
            visit(child, fn)

    visit(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    seen: Counter[str] = Counter()
    out = []
    for s in found:
        seen[s] += 1
        out.append(f"{s}#{seen[s]}")
    return out


def _json_load(call: ast.Call) -> str | None:
    """'' for json.load / json.loads (or any `.load(s)` / bare `load(s)` call: a false hit only lengthens the list)."""
    f = call.func
    if (isinstance(f, ast.Attribute) and f.attr in ("load", "loads")) or (isinstance(f, ast.Name) and f.id in ("load", "loads")):
        return ""
    return None


def _called_names(node: ast.AST) -> set[str]:
    """Names of the bare functions `node` calls, plus 'loads' / 'load' for json.loads / json.load."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute) and f.attr in ("load", "loads"):
                out.add(f.attr)
    return out


def _store_decoders() -> set[str]:
    """Store methods (in store/*.py classes) that call json.load(s) or a module function that does, transitively."""
    decoders = {"load", "loads"}
    funcs: dict[str, ast.AST] = {}
    methods: dict[str, ast.AST] = {}
    for path in sorted((SRC / "store").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs[node.name] = node
            elif isinstance(node, ast.ClassDef):
                methods.update({m.name: m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))})
    changed = True
    while changed:  # a helper calling a decoding helper decodes too
        changed = False
        for name, node in funcs.items():
            if name not in decoders and _called_names(node) & decoders:
                decoders.add(name)
                changed = True
    return {name for name, node in methods.items() if _called_names(node) & decoders}


def test_every_json_load_site_in_store_and_tools_is_reviewed():
    found = []
    for sub in ("store", "tools"):
        for path in sorted((SRC / sub).glob("*.py")):
            rel = f"{sub}/{path.name}"
            found += [f"{rel}::{s}" for s in _sites(path, _json_load)]
    assert sorted(found) == sorted(LOADS_SITES), (
        f"new (not reviewed): {sorted(set(found) - set(LOADS_SITES))}; gone: {sorted(set(LOADS_SITES) - set(found))}. "
        "Bound the parsed cell before it reaches a reply (tools/_util.py: cap_cell_depth, clean_tags, clean_row_*) or "
        "keep it out of the reply, then add the site with the reason."
    )


def test_every_store_method_returning_a_decoded_json_cell_is_reviewed():
    found = _store_decoders()
    assert found == set(STORE_DECODERS), (
        f"new (not reviewed): {sorted(found - set(STORE_DECODERS))}; gone: {sorted(set(STORE_DECODERS) - found)}"
    )


def test_every_tool_call_of_a_decoding_store_method_is_reviewed():
    decoders = _store_decoders()

    def match(call: ast.Call) -> str | None:
        f = call.func
        return f.attr if isinstance(f, ast.Attribute) and f.attr in decoders else None

    found = []
    for path in sorted((SRC / "tools").glob("*.py")):
        found += [f"{path.name}::{s}" for s in _sites(path, match)]
    assert sorted(found) == sorted(TOOL_CALLS), (
        f"new (not reviewed): {sorted(set(found) - set(TOOL_CALLS))}; gone: {sorted(set(TOOL_CALLS) - set(found))}. "
        "A decoded JSON cell that goes into the reply must be bounded first (item 361)."
    )


# ---------- 2. drive: every decoded column 300 levels deep ----------

DEEP_CELLS = [_deep(300), '{"a": ' * 300 + "1" + "}" * 300]
DEEP_IDS = ["deep_list300", "deep_object300"]
CYCLE_TS = NOW_MS - 3_600_000
TRADE = {
    "symbol": "1000PEPE/USDT:USDT",
    "exit_reason": "signal",
    "days": 10.0,
    "funding_usd": 12.0,
    "price_usd": -1.0,
    "costs_usd": 4.0,
    "pnl_usd": 7.0,
    "return_on_notional": 0.0035,
    "open_ts": CYCLE_TS - 10 * 86_400_000,
    "close_ts": CYCLE_TS,
}

# tool, arguments, a warning substring naming the bounded cell
DRIVE = [
    ("journal_list", {}, "had tags that were not a list of strings"),
    ("positions_list", {}, "had tags that were not a list of strings"),
    ("plan_list", {}, "had rules that were not an object of strings"),
    ("carry_paper_status", {"include_closed": True}, "had entry_flags that were not a list of strings"),
    ("assistant_paper_status", {}, "have stored legs that are not a list"),
    ("events_list", {}, "nested deeper than"),
    ("alerts_recent", {}, "nested deeper than"),
    ("paper_carry_status", {"strategy": "A", "include_closed": True}, "nested deeper than"),
]


async def _fill(rt: Runtime, deep: str) -> None:
    """One valid row per table through the store's own writers, then every decoded JSON column set to `deep`."""
    s, c = rt.store, rt.store.conn
    jid = await s.journal_insert(opened_ts=NOW_TS - 600, symbol="BTC/USDT:USDT", side="long", size=1.0, entry=100.0, stop=90.0, tags=["a"])
    pid = await s.position_insert(
        opened_ts=NOW_TS - 600,
        instrument="BTC/USDT:USDT",
        kind="perp",
        side="long",
        size=1.0,
        entry=100.0,
        mark=100.0,
        delta=1.0,
        gamma=None,
        vega=None,
        theta=None,
        tags=["a"],
    )
    plan = await s.plan_insert(
        created_ts=NOW_TS - 600,
        exchange="binance",
        symbol="BTC/USDT:USDT",
        instrument="perp",
        side="long",
        entry_type="limit",
        entry=100.0,
        stop=90.0,
        target=130.0,
        expires_ts=NOW_TS + 86_400,
        verdict="wait",
        rules={"r1": "pass"},
        tags=["a"],
    )
    pair = await s.carry_paper_insert(
        opened_ts=NOW_TS - 3 * 86_400,
        base="BTC",
        symbol="BTC/USDT:USDT",
        long_venue="binanceusdm",
        short_venue="bybit",
        qty=0.01,
        notional_usd=1000.0,
        long_entry=100_000.0,
        short_entry=100_050.0,
        entry_trailing_spread_apr=0.12,
        entry_current_spread_apr=0.1,
        leverage=2.0,
        stop_move=0.5,
        lookback_days=7,
        exit_apr=0.0,
        entry_flags=["x"],
        entry_tradable=True,
        tags=["a"],
    )
    rec = await s.rec_paper_insert(
        opened_ts=NOW_TS - 86_400,
        currency="BTC",
        structure="long_strangle25",
        expiry_ts=NOW_TS + 30 * 86_400,  # not due: no delivery price is fetched
        spot=100_000.0,
        forward=100_100.0,
        t_years=0.1,
        legs=[[90_000.0, False, 1.0, 0.5, 1200.0]],
        premium_per_btc=1200.0,
        entry_fees_per_btc=30.0,
        max_loss_per_btc=1230.0,
        risk_basis="max_loss",
        size_btc=0.1,
        deposit_usd=100_000.0,
        risk_pct=1.0,
        risk_usd=123.0,
        within_risk=True,
        source="manual",
    )
    alert = await s.insert_alert(rule_id="r1", ts=NOW_TS - 3600, payload={}, channel="log", delivered=True)
    for sql, args in (
        ("UPDATE journal SET tags = ? WHERE id = ?", (deep, jid)),
        ("UPDATE positions SET tags = ? WHERE id = ?", (deep, pid)),
        ("UPDATE trade_plan SET rules = ?, tags = ? WHERE id = ?", (deep, deep, plan)),
        (
            "UPDATE carry_paper SET tags = ?, entry_flags = ?, status = 'closed', closed_ts = ? WHERE id = ?",
            (deep, deep, NOW_TS - 60, pair),
        ),
        ("UPDATE rec_paper SET legs = ? WHERE id = ?", (deep, rec)),
        ("UPDATE alerts SET payload = ? WHERE id = ?", (deep, alert)),
        ("INSERT INTO events (ts, kind, label, impact, meta) VALUES (?, 'fomc', 'x', 'high', ?)", (NOW_TS - 3600, deep)),
        (
            "INSERT INTO cp_cycle (strategy, cycle_ts, status, payload) VALUES ('A', ?, 'done', ?)",
            (CYCLE_TS, '{"on_time": true, "entries_stopped": ' + deep + "}"),
        ),
        (
            "INSERT INTO cp_position (trade_id, strategy, symbol, status, opened_ts, slot_notional, state) VALUES ('A:1', 'A', 'WIF/USDT:USDT', 'open', ?, 2000, ?)",
            (CYCLE_TS - 86_400_000, '{"entry_apr": 0.3, "fees": 0.001, "extra": ' + deep + "}"),
        ),
        (
            "INSERT INTO cp_position (trade_id, strategy, symbol, status, opened_ts, closed_ts, slot_notional, state, trade)"
            " VALUES ('A:2', 'A', '1000PEPE/USDT:USDT', 'closed', ?, ?, 2000, '{}', ?)",
            (TRADE["open_ts"], CYCLE_TS, json.dumps(TRADE)[:-1] + ', "note": ' + deep + "}"),
        ),
        ("INSERT INTO cp_log (strategy, cycle_ts, kind, trade_id, payload) VALUES ('A', ?, 'side', 'A:1', ?)", (CYCLE_TS, deep)),
        (
            "INSERT INTO cp_log (strategy, cycle_ts, kind, trade_id, payload) VALUES ('A', ?, 'mark', 'A:1', ?)",
            (CYCLE_TS, '{"pnl_usd": 1.0, "funding_usd": ' + deep + "}"),
        ),
    ):
        await c.execute(sql, args)
    await c.commit()


@pytest.mark.parametrize("deep", DEEP_CELLS, ids=DEEP_IDS)
async def test_every_reading_tool_bounds_deep_json_cells(tmp_path, deep):
    rt = Runtime(
        Settings(http_retries=0, cache_scale=0.0, db_path=str(tmp_path / "guard.sqlite")),
        transports=make_transports(State()),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    try:
        await rt.store.init()
        await _fill(rt, deep)
        s = rt.store
        # the store really hands these cells over 300 levels deep: the bound below is the tools' work
        stored = {
            "journal.tags": (await s.journal_list())[0]["tags"],
            "positions.tags": (await s.positions_list())[0]["tags"],
            "trade_plan.rules": (await s.plan_list())[0]["rules"],
            "trade_plan.tags": (await s.plan_list())[0]["tags"],
            "carry_paper.tags": (await s.carry_paper_list())[0]["tags"],
            "carry_paper.entry_flags": (await s.carry_paper_list())[0]["entry_flags"],
            "rec_paper.legs": (await s.rec_paper_list())[0]["legs"],
            "events.meta": (await s.events_list())[0]["meta"],
            "alerts.payload": (await s.recent_alerts())[0]["payload"],
            "cp_cycle.payload": (await s.cp_cycles("A"))[0]["payload"],
            "cp_log.mark": (await s.cp_latest_marks("A"))[0]["payload"],
            "cp_log.side": (await s.cp_logs("A", "side"))[0]["payload"],
            "cp_position.trade": (await s.cp_positions("A", "closed"))[0]["trade"],
            "cp_position.state": (await s.cp_positions("A", "open"))[0]["state"],
        }
        assert [k for k, v in stored.items() if not json_too_deep(v)] == []
        async with _session(rt) as call:  # `call` fails on is_error (the serialiser's "depth exceeded")
            for tool, args, warning in DRIVE:
                env = await call(tool, **args)
                assert env["ok"] is True, (tool, env["missing"])
                assert not json_too_deep(env, MAX_CELL_DEPTH + ENVELOPE_LEVELS), tool
                assert any(warning in w for w in env["warnings"]), (tool, env["warnings"])
    finally:
        await rt.aclose()


async def test_record_trader_positions_survives_a_state_cell_too_deep_to_parse(tmp_path):
    # json.loads raised RecursionError on ~10000 levels: the collector job failed on every tick for that account.
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=str(tmp_path / "tp.sqlite")), transports=make_transports(State()))
    try:
        await rt.store.init()
        await rt.store.conn.execute(
            "INSERT INTO trader_position_state (venue, account, digest, ts, positions_json) VALUES ('hyperliquid', '0xabc', 'x', 1, ?)",
            (_deep(20_000),),
        )
        await rt.store.conn.commit()
        pos = [{"market": "BTC", "side": "long", "size": 1.0}]
        assert await rt.store.record_trader_positions(NOW_TS, "hyperliquid", "0xabc", pos) == 1  # read as no previous set
        assert await rt.store.record_trader_positions(NOW_TS + 60, "hyperliquid", "0xabc", pos) == 0  # stored state is valid again
    finally:
        await rt.aclose()
