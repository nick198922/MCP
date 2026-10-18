#!/bin/bash
# From the Mac: build round trips from the downloaded Binance history files, add them to the journal and print the
# statistics for Binance and for all venues (read-only key; reports stay on the server, outside git).
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'cd ~/tradedesk-mcp && git pull -q --ff-only && U=~/.local/bin/uv &&
$U run -q python scripts/binance_import.py --env /etc/tradedesk-binance.env import &&
$U run -q python scripts/bybit_import.py stats --venue binance --out /var/lib/tradedesk-mcp/journal/report-binance.md &&
$U run -q python scripts/bybit_import.py stats --venue all --out /var/lib/tradedesk-mcp/journal/report-all.md'
