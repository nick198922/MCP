"""Weekly COUNTERS-ONLY report of the ``assistant_paper`` forward journal (item 284 of
docs/ideas/IDEA-20261102-standing-improvements.md).

    uv run python scripts/assistant_paper_report.py [--db PATH] [--out PATH] [--now UNIX_S] [--print] [--dry-run] [--telegram]

Shows whether the journal is alive and has no holes: method trades (all / new in the last 7 days), standing, expired
and not yet settled, stretches longer than a day without a standing method trade, and the job's last daily decision
from its state file. NO P&L, CLV or results: ``rec_paper`` is read without its P&L columns and only the whitelisted
keys of the state file are kept. The loaders are the checkpoint report's (``scripts/checkpoint_report.py``: a READ-ONLY
connection ``mode=ro``, no migration, no write, no network) and the counters its pure
``tradedesk.analytics.checkpoint.assistant_paper_forward``, so both reports count the same way.

Output: ``--out``, else $TRADEDESK_ASSISTANT_PAPER_REPORT, else /var/lib/tradedesk-mcp/paper/assistant_paper_REPORT.md
(outside git). ``--dry-run`` prints the text and writes / sends nothing; ``--print`` also prints it; ``--telegram``
sends it to the group's ``agents`` topic through scripts/tg_send.py. Exit 1 when the DB or the ``rec_paper`` table
cannot be read (the text still names it), 3 when the Telegram send fails. Paper rows only: nothing here trades.
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

import checkpoint_report as cr  # noqa: E402

from tradedesk.analytics.checkpoint import assistant_paper_forward  # noqa: E402

DEFAULT_OUT = "/var/lib/tradedesk-mcp/paper/assistant_paper_REPORT.md"
ENV_OUT = "TRADEDESK_ASSISTANT_PAPER_REPORT"
WEEK_S = 7 * 86_400


def load(db: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[str]]:
    """(rec_paper rows without P&L columns, whitelisted state file or None, errors); never raises."""
    errors: list[str] = []
    state = cr.load_paper_state(db)
    try:
        con = cr.connect_ro(db)
    except (sqlite3.Error, OSError) as e:
        return [], state, [f"БД {db} не відкрито ({type(e).__name__})"]
    try:
        if "rec_paper" not in cr._tables(con):
            errors.append("rec_paper: таблиці немає в БД (старіша схема чи інша БД)")
            return [], state, errors
        return cr.load_rec_paper(con), state, errors
    except sqlite3.Error as e:
        return [], state, [f"rec_paper: запит не вдався ({type(e).__name__})"]
    finally:
        con.close()


def build(db: str, now_s: int) -> tuple[int, str]:
    """(exit code, Ukrainian Markdown text): 0, or 1 when the DB / table could not be read."""
    rows, state, errors = load(db)
    row = assistant_paper_forward(rows, state, now_s * 1000)
    new = sum(
        1 for r in rows if r.get("source") == "method" and isinstance(r.get("opened_ts"), int) and now_s - WEEK_S < r["opened_ts"] <= now_s
    )
    now = datetime.fromtimestamp(now_s, UTC)
    lines = [
        f"# assistant_paper: лічильники форвард-журналу на {now:%Y-%m-%d %H:%M} UTC",
        "",
        "Лише лічильники й стан завдання, без P&L, CLV і результатів. БД прочитано лише на читання, без мережі.",
        "",
    ]
    lines += [f"- {k}: {v}" for k, v in row["counters"].items()]
    lines.append(f"- нових угод методу за 7 днів: {new}")
    if errors:
        lines += ["", "## Помилки читання", ""] + [f"- {e}" for e in errors]
    lines += ["", "## Примітки", ""] + [f"- {n}" for n in row["notes"]]
    return (1 if errors else 0), "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=cr.DEFAULT_DB, help="collector SQLite DB (opened read-only)")
    ap.add_argument("--out", default="", help=f"output Markdown (default ${ENV_OUT} or {DEFAULT_OUT})")
    ap.add_argument("--now", type=int, default=0, help="report time in unix seconds (default: now)")
    ap.add_argument("--print", action="store_true", help="also print the text")
    ap.add_argument("--dry-run", action="store_true", help="print the text only: no file, no Telegram")
    ap.add_argument("--telegram", action="store_true", help="send the text to the Telegram group (topic agents)")
    a = ap.parse_args(argv)
    code, text = build(a.db, a.now or int(time.time()))
    if a.dry_run:
        print(text, end="")
        return code
    out = Path(a.out or os.environ.get(ENV_OUT) or DEFAULT_OUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text)} bytes)")
    if a.print:
        print(text, end="")
    if a.telegram:
        from tg_send import md_to_html, send  # noqa: PLC0415

        if not send(md_to_html(text), topic="agents", silent=True):
            return 3
    return code


if __name__ == "__main__":
    sys.exit(main())
