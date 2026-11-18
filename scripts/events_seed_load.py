"""Load the bundled macro-events seed into the local store (a one-off action; the former MCP tool events_seed_load).

    uv run python scripts/events_seed_load.py [--db PATH]

The seed (src/tradedesk/data/events_seed.json): FOMC / CPI / NFP 2024-2027 and US election day 2026-11-03 (kind
'election', a nominal 12:00 UTC). Idempotent: rows with the same (ts, kind, label) are skipped, so a rerun inserts
nothing. 2026 Fed dates in the seed are PROJECTED; check them against the official Fed / BLS calendars. Writes only the
`events` table of the SQLite store (default: $TRADEDESK_DB_PATH if set, else the service database
/var/lib/tradedesk-mcp/tradedesk.sqlite; the service gets its path from /etc/tradedesk-mcp.env, not from the shell);
the target path goes to stderr. No network, no orders. Prints the same envelope the tool returned. Removed from the MCP
server on 2026-11-18 (owner's decision Q-20261114-1 «Б2», docs/changes/2026-11-18-mcp-remove-six.md).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys

from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.tools.events import events_seed_load

SERVICE_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"  # as scripts/binance_import.py, scripts/collector_health.py


async def run(db: str) -> dict:
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=db))
    try:
        return await events_seed_load(rt)
    finally:
        await rt.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default = os.environ.get("TRADEDESK_DB_PATH") or SERVICE_DB
    ap.add_argument("--db", default=default, help=f"SQLite path (default: TRADEDESK_DB_PATH or {SERVICE_DB})")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    print(f"store: {os.path.abspath(a.db) if a.db else '(none: --db is empty)'}", file=sys.stderr)
    env = asyncio.run(run(a.db))
    print(json.dumps(env, ensure_ascii=False, indent=1, default=str))
    return 0 if env["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
