"""Monthly evaluation of the cycle regime journal: «state -> BTC return over the next 30 / 90 days».

    uv run python scripts/cycle_regime_eval.py [--db PATH] [--base BTC] [--now ISO] [--out PATH]

Reads ``cycle_regime_log`` from the collector DB through a READ-ONLY connection (``mode=ro``: no migration, no write,
no network) and applies the pure ``analytics/cycle_regime_journal.py`` (episodes and days per state, forward returns
only over elapsed horizons, the unconditional baseline). Prints the Ukrainian markdown table; ``--out`` also writes it
to a file. Descriptive only: the card (IDEA-20261028-btc-cycle-regime-gauge) judges the journal after 12 months.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.cycle_regime_journal import evaluate_journal, render_ua

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"


def load(db: str, base: str) -> list[dict[str, Any]]:
    """Journal rows of ``base`` (date, state, close, version), oldest first; [] when the table does not exist yet."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cycle_regime_log'").fetchone():
            return []
        cur = con.execute("SELECT date, state, close, version FROM cycle_regime_log WHERE base = ? ORDER BY date", (base,))
        return [dict(zip(("date", "state", "close", "version"), r, strict=True)) for r in cur]
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help=f"collector SQLite (default {DEFAULT_DB})")
    ap.add_argument("--base", default="BTC")
    ap.add_argument("--now", help="report time, ISO UTC (default: now)")
    ap.add_argument("--out", type=Path, help="also write the markdown here")
    a = ap.parse_args(argv)
    now = datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    try:
        rows = load(a.db, a.base.upper())
    except sqlite3.Error as e:
        print(f"cannot read {a.db}: {e}", file=sys.stderr)
        return 1
    text = render_ua(evaluate_journal(rows, now), a.base.upper())
    print(text, end="")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
