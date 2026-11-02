"""Checkpoint 2026-11-30 in one report: every forward test at a date, its registered backtest band and a label
«рано» / «узгоджується» / «ні» (docs/CHECKPOINT-2026-11-30.md; item 2 of IDEA-20261102-standing-improvements).

    uv run python scripts/checkpoint_report.py [--db PATH] [--date YYYY-MM-DD] [--out FILE]

``--date``: the state at the end of that UTC day (default: now; never later than the wall clock). Reads the collector
DB through read-only connections (``mode=ro``: no migration, no write), no network, no keys; nothing here places,
changes or cancels an order and nothing under ``execution/`` is imported. The rules live in the pure
``tradedesk.analytics.checkpoint``. Forwards under a no-peeking embargo (Polymarket v1 / v2-g, the condor P&L) show
counters only, and the condor's ``option_leg_snapshot`` is not read before its registered T0 (2026-11-06 10:00 UTC).
The text (Ukrainian) is printed and, with ``--out``, written to a file.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tradedesk.analytics.carry_rules import DECISION_DELAY_MS, carry_side_cost  # noqa: E402
from tradedesk.analytics.checkpoint import build, condor_final_ms, render  # noqa: E402
from tradedesk.analytics.condor_forward import EARLIEST_T0, ENTRY_CUTOFF_HOUR_UTC, cost_fridays  # noqa: E402
from tradedesk.collector.carry_paper_engine import PARAMS_A  # noqa: E402

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
BENCH = ROOT / "docs" / "paper" / "bench.json"
CARRY_KINDS = ("side", "funding", "mark", "close", "event")


def connect_ro(db: str) -> sqlite3.Connection:
    """A read-only connection; raises ``sqlite3.Error`` / ``FileNotFoundError`` when the DB is not there."""
    if not Path(db).exists():
        raise FileNotFoundError(db)
    con = sqlite3.connect(f"file:{urllib.parse.quote(str(Path(db)))}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _json(x: Any) -> Any:
    if not isinstance(x, str):
        return x
    try:
        return json.loads(x)
    except ValueError:
        return None


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def load_demo(con: sqlite3.Connection) -> list[dict[str, Any]]:
    if "demo_trade" not in _tables(con):
        return []
    return [dict(r) for r in con.execute("SELECT * FROM demo_trade ORDER BY signal_ts")]


def load_carry(con: sqlite3.Connection, strategy: str) -> dict[str, Any]:
    names = _tables(con)
    out: dict[str, Any] = {"positions": [], "logs": {k: [] for k in CARRY_KINDS}, "cycles": []}
    if "cp_position" in names:
        for r in con.execute("SELECT * FROM cp_position WHERE strategy = ? ORDER BY opened_ts, trade_id", (strategy,)):
            d = dict(r)
            d["state"], d["trade"] = _json(d.get("state")) or {}, _json(d.get("trade"))
            out["positions"].append(d)
    if "cp_log" in names:
        q = "SELECT cycle_ts, kind, trade_id, payload FROM cp_log WHERE strategy = ? AND kind = ? ORDER BY id"
        for k in CARRY_KINDS:
            out["logs"][k] = [{**dict(r), "payload": _json(r["payload"]) or {}} for r in con.execute(q, (strategy, k))]
    if "cp_cycle" in names:
        q = "SELECT strategy, cycle_ts, status, finished_ms, payload FROM cp_cycle WHERE strategy = ? ORDER BY cycle_ts"
        out["cycles"] = [{**dict(r), "payload": _json(r["payload"]) or {}} for r in con.execute(q, (strategy,))]
    return out


def load_inputs(db: str, now_ms: int) -> dict[str, Any]:
    """Everything ``checkpoint.build`` needs; a part that cannot be read is empty and named in ``errors`` (never raises)."""
    inp: dict[str, Any] = {
        "errors": [],
        "side_cost_a": carry_side_cost(PARAMS_A.fee_1, PARAMS_A.fee_2, PARAMS_A.slippage),
        "decision_delay_ms": DECISION_DELAY_MS,
    }
    try:
        bench = json.loads(BENCH.read_text(encoding="utf-8"))
        inp["bench"] = {s: bench[s]["bench"] for s in ("A", "B")}
    except (OSError, ValueError, KeyError) as e:
        inp["errors"].append(f"бенч carry не прочитано ({type(e).__name__}): критерій (в) — ◯")
    try:
        con = connect_ro(db)
    except (sqlite3.Error, OSError) as e:
        inp["errors"].append(f"БД {db} не відкрито ({type(e).__name__})")
        return inp
    try:
        for what, fn in (("demo_trade", lambda: load_demo(con)), ("carry", lambda: {s: load_carry(con, s) for s in ("A", "B")})):
            try:
                inp["demo_trades" if what == "demo_trade" else "carry"] = fn()
            except sqlite3.Error as e:
                inp["errors"].append(f"{what}: запит не вдався ({type(e).__name__})")
    finally:
        con.close()
    try:
        import pm_gap_paper_report  # noqa: PLC0415 - its own read-only loader

        inp["pm_rows"], inp["pm_outcomes"] = pm_gap_paper_report.load(db)
    except sqlite3.Error as e:
        inp["errors"].append(f"pm_gap: запит не вдався ({type(e).__name__})")
    cut = datetime(EARLIEST_T0.year, EARLIEST_T0.month, EARLIEST_T0.day, ENTRY_CUTOFF_HOUR_UTC, tzinfo=UTC)
    now = datetime.fromtimestamp(now_ms / 1000, UTC)
    if now >= cut:  # the registration forbids reading the condor snapshots before T0
        try:
            import condor_forward_costs as cfc  # noqa: PLC0415 - its read-only T0 detection and loader

            t0 = cfc.detect_t0(Path(db), now)
            inp["condor_t0"] = t0
            if t0 is not None:
                final = now_ms >= condor_final_ms(t0)  # hold_* (Thursday) rows only once part (a) may be decided
                fridays = [
                    f for f in cost_fridays(t0) if final or datetime(f.year, f.month, f.day, ENTRY_CUTOFF_HOUR_UTC, tzinfo=UTC) <= now
                ]
                inp["condor_rows"] = cfc.load_rows(Path(db), fridays, with_hold=final) if fridays else []
        except sqlite3.Error as e:
            inp["errors"].append(f"option_leg_snapshot: запит не вдався ({type(e).__name__})")
    return inp


def at_date(s: str | None, wall_ms: int) -> int:
    """End of the UTC day ``s`` (YYYY-MM-DD) in ms, never later than ``wall_ms``; None = ``wall_ms``."""
    if not s:
        return wall_ms
    end = int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000) + 86_400_000 - 1
    return min(end, wall_ms)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help="collector SQLite DB (opened read-only)")
    ap.add_argument("--date", help="state at the end of this UTC day, YYYY-MM-DD (default: now; capped at the wall clock)")
    ap.add_argument("--out", help="also write the text to this file")
    a = ap.parse_args(argv)
    now_ms = at_date(a.date, int(time.time() * 1000))
    text = render(build(load_inputs(a.db, now_ms), now_ms))
    print(text, end="")
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
