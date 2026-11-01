"""Weekly DESCRIPTIVE report of the Polymarket <-> Deribit paper test, or (once, at T0 + 126 days) its verdict.

    uv run python scripts/pm_gap_paper_report.py [--db PATH] [--out PATH] [--t0 UNIX_S] [--now UNIX_S] [--print] [--telegram]
    uv run python scripts/pm_gap_paper_report.py --verdict [--db PATH] [--out PATH]   # refused before T0 + 126 days
    uv run python scripts/pm_gap_paper_report.py --verdict-v2g [--db PATH] [--out PATH]   # v2-g's own test, once

``--verdict`` takes no ``--now`` / ``--t0`` (refused if given): it uses the wall clock and the registered T0
(``pm_gap_report.T0_TS``), and refuses when the DB's first snapshot is not that T0. The gate cannot be moved from the
command line; the weekly mode keeps both flags (it shows no mean). In ``--verdict`` every v2-g row (the v1 split
included) is cut to entries before ``T0_V2G_TS``: those belong to v2-g's own forward test
(docs/ideas/IDEA-20261101-polymarket-v2g-forward.md), whose one-time verdict is ``--verdict-v2g`` (same rules for the
flags; gate max(T0 v2-g + 126 days, v1's verdict); refused when the DB has no snapshot at ``T0_V2G_TS``).

Reads ``pm_gap_snapshot`` and ``pm_gap_outcome`` from the collector DB through a READ-ONLY connection (``mode=ro``: no
migration, no write, no network) and applies the registration of
docs/ideas/IDEA-20261017-polymarket-vs-options-forward.md with the pure ``analytics/pm_gap_report.py``. The weekly report
shows only what registration section 1 allows (counts, resolution days and weeks, CLV source shares, SD per position,
skew and read-time shares); the mean of the main metric, its CI, the P&L and the Brier comparison exist only in
``--verdict`` mode, together with the descriptive stress and robustness checks of registration sections 6 and 12
(thresholds 3 / 8 pp, k separately, depth >= $500, entry +1 cent, next-snapshot entry, period halves, BTC vs ETH with a
within-week permutation, by kind, resolution-day clusters, closing CLV from 'close' only). Both modes append the labelled
variant v2-g (forward x (1 + g), g = binance_last / spot - 1 of the same snapshot; IDEA-20261031-structural-edges-depth
item 8a) as separate rows under the same limits: weekly = counts, entry edge, SD and the v1 positions whose edge held only
thanks to the g shift; CLV, P&L and Brier of v2-g only in ``--verdict``. v1 and its verdict are unchanged.
Weekly T0 = the first snapshot unless ``--t0`` is given. Output: ``--out``, else $TRADEDESK_PM_GAP_REPORT,
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
    T0_TS,
    T0_V2G_TS,
    final_metrics,
    final_metrics_v2g,
    final_metrics_v2g_own,
    render_verdict,
    render_verdict_v2g,
    render_verdict_v2g_own,
    render_weekly,
    render_weekly_v2g,
    verdict_ts,
    verdict_ts_v2g,
    weekly_descriptive,
    weekly_v2g,
)

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DEFAULT_OUT = "/var/lib/tradedesk-mcp/paper/pm_gap_REPORT.md"
ENV_OUT = "TRADEDESK_PM_GAP_REPORT"
# Only the columns the rules and metrics read (the table grows by ~5 000 rows a day; the full rows would waste memory).
COLS = (
    "ts market_id asset kind direction strike end_ts yes_bid yes_ask yes_ask_usd no_bid no_ask no_ask_usd fee_rate fee_exp "
    "rules rules_hash rules_standard model_prob model_note model_iv model_covered spot book_ts surface_ts model_forward "
    "binance_last"
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


NO_ROWS = "no pm_gap_snapshot rows: the collector has not stored anything yet\n"


def build_weekly(db: str, now_ts: int, t0: int | None) -> tuple[int, str]:
    """(exit code, text) of the descriptive weekly report; ``t0`` None = the first snapshot. From the registered
    T0 v2-g on, the v2-g block reports v2-g's own period (from ``T0_V2G_TS``, whatever ``t0`` is); before it, the
    labelled comparison on v1's period (item 8a)."""
    rows, outcomes = load(db)
    if not rows:
        return 1, NO_ROWS
    start = int(t0) if t0 else min(int(r["ts"]) for r in rows)
    if now_ts >= T0_V2G_TS:
        d = weekly_v2g(rows, outcomes, now_ts, T0_V2G_TS, v1_t0=T0_TS)
    else:
        d = weekly_v2g(rows, outcomes, now_ts, start)
    return 0, render_weekly(weekly_descriptive(rows, outcomes, now_ts, start)) + "\n" + render_weekly_v2g(d)


def build_verdict(db: str) -> tuple[int, str]:
    """(exit code, text) of the one-time verdict, on the wall clock and the registered T0 only (no parameter can move
    the gate). Exit 2 with a refusal before T0 + 126 days or when the DB's first snapshot is not the registered T0."""
    now_ts = int(time.time())
    if now_ts < verdict_ts(T0_TS):
        when = datetime.fromtimestamp(verdict_ts(T0_TS), UTC).isoformat(timespec="minutes")
        return 2, f"refused: the verdict is computed once, not before T0 + 126 days ({when}); use the weekly report\n"
    rows, outcomes = load(db)
    if not rows:
        return 1, NO_ROWS
    first = min(int(r["ts"]) for r in rows)
    if first != T0_TS:
        return 2, f"refused: the DB's first snapshot is {first}, not the registered T0 {T0_TS}; wrong or altered database\n"
    v2g = render_verdict_v2g(final_metrics_v2g(rows, outcomes, now_ts, T0_TS, entry_before=T0_V2G_TS))
    return 0, render_verdict(final_metrics(rows, outcomes, now_ts, T0_TS)) + "\n" + v2g


def build_verdict_v2g(db: str) -> tuple[int, str]:
    """(exit code, text) of the one-time verdict of v2-g's own forward test, on the wall clock and the registered
    ``T0_V2G_TS`` / ``T0_TS`` only. Exit 2 with a refusal before max(T0 v2-g + 126 days, v1's verdict), when the DB's
    first snapshot is not v1's T0 (another database) or when it has no snapshot at T0 v2-g."""
    now_ts = int(time.time())
    gate = verdict_ts_v2g(T0_V2G_TS, T0_TS)
    if now_ts < gate:
        when = datetime.fromtimestamp(gate, UTC).isoformat(timespec="minutes")
        return 2, f"refused: the v2-g verdict is computed once, not before max(T0 v2-g + 126 days, v1's verdict) ({when})\n"
    rows, outcomes = load(db)
    if not rows:
        return 1, NO_ROWS
    first = min(int(r["ts"]) for r in rows)
    if first != T0_TS:
        return 2, f"refused: the DB's first snapshot is {first}, not the registered T0 {T0_TS}; wrong or altered database\n"
    if not any(int(r["ts"]) == T0_V2G_TS for r in rows):
        return 2, f"refused: the DB has no snapshot at the registered T0 v2-g {T0_V2G_TS}; wrong or altered database\n"
    return 0, render_verdict_v2g_own(final_metrics_v2g_own(rows, outcomes, now_ts, T0_V2G_TS, T0_TS))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help="collector SQLite DB (opened read-only)")
    ap.add_argument("--out", default="", help=f"output Markdown (default ${ENV_OUT} or {DEFAULT_OUT})")
    ap.add_argument("--t0", type=int, default=0, help="weekly mode only: T0 in unix seconds (default: the first snapshot)")
    ap.add_argument("--now", type=int, default=0, help="weekly mode only: report time in unix seconds (default: now)")
    ap.add_argument("--verdict", action="store_true", help="the one-time verdict; refused before T0 + 126 days")
    ap.add_argument(
        "--verdict-v2g",
        action="store_true",
        help="the one-time verdict of v2-g's own test; refused before max(T0 v2-g + 126 days, v1's verdict)",
    )
    ap.add_argument("--print", action="store_true", help="also print the text")
    ap.add_argument("--telegram", action="store_true", help="send the text to the Telegram group (topic agents)")
    a = ap.parse_args(argv)
    if a.verdict and a.verdict_v2g:
        print("refused: --verdict and --verdict-v2g are separate one-time runs; give one", file=sys.stderr)
        return 2
    if (a.verdict or a.verdict_v2g) and (a.now or a.t0):
        print("refused: --verdict / --verdict-v2g use the wall clock and the registered T0; --now / --t0 are not allowed", file=sys.stderr)
        return 2
    if a.verdict:
        code, text = build_verdict(a.db)
    elif a.verdict_v2g:
        code, text = build_verdict_v2g(a.db)
    else:
        code, text = build_weekly(a.db, a.now or int(time.time()), a.t0 or None)
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
