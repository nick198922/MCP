#!/bin/bash
# From the Mac: import the owner's Hyperliquid perp trades (public fills by wallet address) into the journal on the
# server and print the statistics. Usage: bash scripts/server/hl-import.sh 0xADDRESS
# The address is passed on the command line only (not stored in git). Safe to re-run: trips already in the journal are skipped.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" "cd ~/tradedesk-mcp && git pull -q --ff-only &&
~/.local/bin/uv run -q python scripts/hyperliquid_import.py import '${1:?wallet address}' &&
~/.local/bin/uv run -q python scripts/hyperliquid_import.py stats --out /var/lib/tradedesk-mcp/journal/hyperliquid-report.md"
