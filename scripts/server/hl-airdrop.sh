#!/bin/bash
# From the Mac: tag Hyperliquid journal trades closed before a date as airdrop farming (kept, left out of statistics).
# Usage: bash scripts/server/hl-airdrop.sh YYYY-MM-DD
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" "cd ~/tradedesk-mcp && git pull -q --ff-only &&
~/.local/bin/uv run -q python scripts/hyperliquid_import.py mark-airdrop --before '${1:?date}'"
