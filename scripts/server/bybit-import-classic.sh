#!/bin/bash
# From the Mac: import the pre-upgrade (classic account) closed P&L from Bybit into the journal. Usage:
#   bash scripts/server/bybit-import-classic.sh SYMBOLS [FROM] [TO]   (SYMBOLS: comma-separated, or "journal")
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" "cd ~/tradedesk-mcp && git pull -q --ff-only &&
~/.local/bin/uv run -q python scripts/bybit_import.py import-classic --symbols '${1:?symbols}' --from '${2:-2024-10-19}' --to '${3:-2025-05-01}'"
