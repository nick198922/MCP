"""Weekly DESCRIPTIVE report of the Polymarket <-> Deribit paper test, or (once, at T0 + 126 days) its verdict.

    uv run python scripts/pm_gap_paper_report.py [--db PATH] [--out PATH] [--t0 UNIX_S] [--now UNIX_S] [--print] [--telegram]
    uv run python scripts/pm_gap_paper_report.py --verdict ...     # refuses to run before T0 + 126 days

Reads ``pm_gap_snapshot`` and ``pm_gap_outcome`` from the collector DB through a READ-ONLY connection (``mode=ro``: no
migration, no write, no network) and applies the registration of
docs/ideas/IDEA-20261017-polymarket-vs-options-forward.md with the pure ``analytics/pm_gap_report.py``. The weekly report
shows only what registration section 1 allows (counts, resolution days and weeks, CLV source shares, SD per position,
skew and read-time shares); the mean of the main metric, its CI, the P&L and the Brier comparison exist only in
``--verdict`` mode. T0 = the first snapshot unless ``--t0`` is given. Output: ``--out``, else $TRADEDESK_PM_GAP_REPORT,
else /var/lib/tradedesk-mcp/paper/pm_gap_REPORT.md (outside git). ``--telegram`` sends the text to the group's ``agents``
topic through scripts/tg_send.py. Paper positions only: nothing here trades.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tradedesk.analytics.pm_gap_report import (  # noqa: E402
    final_metrics,
    render_verdict,
    render_weekly,
    verdict_ts,
    weekly_descriptive,
)

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DEFAULT_OUT = "/var/lib/tradedesk-mcp/paper/pm_gap_REPORT.md"
ENV_OUT = "TRADEDESK_PM_GAP_REPORT"
# Only the columns the rules and metrics read (the table grows by ~5 000 rows a day; the full rows would waste memory).
COLS = (
    "ts market_id asset kind direction strike end_ts yes_bid yes_ask yes_ask_usd no_bid no_ask no_ask_usd fee_rate fee_exp "
    "rules rules_hash rules_standard model_prob model_note model_iv model_covered spot book_ts surface_ts model_forward"
).split()


def load(db: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(snapshot rows, outcome rows) through a read-only connection; columns a DB of an older schema lacks are None."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(pm_gap_snapshot)")}
        if not have:
            return [], []
        sel = [c for c in COLS if c in have]
        rows = [dict(zip(sel, r, strict=True)) for r in con.execute(f"SELECT {', '.join(sel)} FROM pm_gap_snapshot ORDER BY ts, market_id")]
        for r in rows:
            for c in COLS:
                r.setdefault(c, None)
        outcomes: list[dict[str, Any]] = []
        if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pm_gap_outcome'").fetchone():
            cur = con.execute("SELECT * FROM pm_gap_outcome")
            names = [d[0] for d in cur.description]
            outcomes = [dict(zip(names, r, strict=True)) for r in cur]
        return rows, outcomes
    finally:
        con.close()


def build(db: str, now_ts: int, t0: int | None, want_verdict: bool) -> tuple[int, str]:
    """(exit code, text). Exit 2 and a refusal text for ``--verdict`` before T0 + 126 days."""
    rows, outcomes = load(db)
    if not rows:
        return 1, "no pm_gap_snapshot rows: the collector has not stored anything yet\n"
    start = int(t0) if t0 else min(int(r["ts"]) for r in rows)
    if want_verdict:
        if now_ts < verdict_ts(start):
            when = datetime.fromtimestamp(verdict_ts(start), UTC).isoformat(timespec="minutes")
            return 2, f"refused: the verdict is computed once, not before T0 + 126 days ({when}); use the weekly report\n"
        return 0, render_verdict(final_metrics(rows, outcomes, now_ts, start))
    return 0, render_weekly(weekly_descriptive(rows, outcomes, now_ts, start))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help="collector SQLite DB (opened read-only)")
    ap.add_argument("--out", default="", help=f"output Markdown (default ${ENV_OUT} or {DEFAULT_OUT})")
    ap.add_argument("--t0", type=int, default=0, help="T0 in unix seconds (default: the first snapshot)")
    ap.add_argument("--now", type=int, default=0, help="report time in unix seconds (default: now)")
    ap.add_argument("--verdict", action="store_true", help="the one-time verdict; refused before T0 + 126 days")
    ap.add_argument("--print", action="store_true", help="also print the text")
    ap.add_argument("--telegram", action="store_true", help="send the text to the Telegram group (topic agents)")
    a = ap.parse_args(argv)
    code, text = build(a.db, a.now or int(time.time()), a.t0 or None, a.verdict)
    if code:
        print(text, end="", file=sys.stderr)
        return code
    out = Path(a.out or os.environ.get(ENV_OUT) or DEFAULT_OUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text)} bytes)")
    if a.print:
        print(text)
    if a.telegram:
        from tg_send import md_to_html, send  # noqa: PLC0415

        if not send(md_to_html(text), topic="agents", silent=True):
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
