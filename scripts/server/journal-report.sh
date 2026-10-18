#!/bin/bash
# From the Mac: the journal report for one venue or all of them (read-only). Usage: bash scripts/server/journal-report.sh [bybit|hyperliquid|binance|all]
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" "cd ~/tradedesk-mcp && git pull -q --ff-only &&
~/.local/bin/uv run -q python scripts/bybit_import.py stats --venue '${1:-all}' --out /var/lib/tradedesk-mcp/journal/report-${1:-all}.md"
