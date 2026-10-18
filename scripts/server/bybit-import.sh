#!/bin/bash
# From the Mac: import the owner's closed Bybit positions (last 2 years) into the journal on the server and print the
# statistics. Runs as tradedesk; needs the read-only key from scripts/server/bybit-key.sh. Safe to re-run: records
# already in the journal are skipped. The report stays on the server, outside git (private trading record).
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'cd ~/tradedesk-mcp && git pull -q --ff-only &&
~/.local/bin/uv run -q python scripts/bybit_import.py import &&
~/.local/bin/uv run -q python scripts/bybit_import.py stats --out /var/lib/tradedesk-mcp/journal/bybit-report.md'
