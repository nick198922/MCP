"""Install the bundled DSL rule presets into the local store (a one-off action; the former MCP tool rules_presets_load).

    uv run python scripts/rules_presets_load.py [--only NAME ...] [--enable-disabled] [--db PATH]

The presets (src/tradedesk/data/rules_presets.json): funding extremes, funding z-score, HL <-> Deribit crypto-carry,
liquidation cascades + imbalance. Every expression is compiled before insert; idempotent via UNIQUE(name), existing rules
are skipped. `--only` picks preset names; `--enable-disabled` enables presets flagged `enabled: false` in the seed. The
thresholds are starting points picked by the model, NOT backtested or research-sourced: tune them before trusting the
alerts. Writes only the `rules` table of the SQLite store (default: $TRADEDESK_DB_PATH if set, else the service
database /var/lib/tradedesk-mcp/tradedesk.sqlite, which the alerts job reads; the service gets its path from
/etc/tradedesk-mcp.env, not from the shell); the target path goes to stderr. No network, no orders. Prints the same
envelope the tool returned. Removed from the MCP server on 2026-11-18 (owner's decision Q-20261114-1 «Б2»,
docs/changes/2026-11-18-mcp-remove-six.md).
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
from tradedesk.tools.rules import rules_presets_load

SERVICE_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"  # as scripts/binance_import.py, scripts/collector_health.py
CAVEAT = "preset thresholds are NOT backtested or research-sourced; tune them against your own history before trusting the alerts"


async def run(db: str, only: list[str] | None, enable_disabled: bool) -> dict:
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=db))
    try:
        return await rules_presets_load(rt, only=only, enable_disabled=enable_disabled)
    finally:
        await rt.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", nargs="+", help="install just these preset names")
    ap.add_argument("--enable-disabled", action="store_true", help="enable presets whose seed `enabled` is false")
    default = os.environ.get("TRADEDESK_DB_PATH") or SERVICE_DB
    ap.add_argument("--db", default=default, help=f"SQLite path (default: TRADEDESK_DB_PATH or {SERVICE_DB})")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    print(f"store: {os.path.abspath(a.db) if a.db else '(none: --db is empty)'}", file=sys.stderr)
    env = asyncio.run(run(a.db, a.only, a.enable_disabled))
    print(json.dumps(env, ensure_ascii=False, indent=1, default=str))
    print(f"note: {CAVEAT}", file=sys.stderr)
    return 0 if env["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
